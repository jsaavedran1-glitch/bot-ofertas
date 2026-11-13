from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

APPLIANCE_QUERIES = ("televisor", "consola", "audifonos", "freidora de aire", "parlante", "smartwatch", "celular")
GROCERY_QUERIES = ("detergente", "cafe", "aceite", "arroz", "atun", "cerveza", "papel higienico")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _session(session: requests.Session | None) -> requests.Session:
    session = session or requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
    session.headers["User-Agent"] = _UA
    return session


def _deal(source: str, external_id: str, title: str, price: float, before: float, url: str, image: str,
          rating: float | None = None, reviews: int | None = None) -> DealObservation | None:
    if not (title and url and image) or price <= 0 or before <= price:
        return None
    return DealObservation(
        source=source,
        external_id=external_id,
        title=title.strip(),
        price_minor=to_minor(price, "COP"),
        original_price_minor=to_minor(before, "COP"),
        evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
        currency="COP",
        url=url,
        image_url=image,
        available=True,
        shipping_note="Envío según destino",
        observed_at=datetime.now(timezone.utc),
        rating=rating,
        reviews=reviews,
    )


class _StoreSource(DealSource):
    def __init__(
        self, source_name: str, queries: tuple[str, ...] = APPLIANCE_QUERIES, per_query: int = 20,
        min_savings_cop: int | None = None,
    ) -> None:
        self.source_name = source_name
        self.queries = queries
        self.per_query = per_query
        self.min_savings_cop = min_savings_cop  # None = use the global MIN_SAVINGS_COP

    def fetch(self) -> list[DealObservation]:
        deals: dict[str, DealObservation] = {}  # keyed by title: color variants share it
        errors = 0
        for query in self.queries:
            try:
                found = self.search(query)
            except (requests.RequestException, ValueError, KeyError, TypeError):
                errors += 1
                continue
            for deal in found:
                deals.setdefault(deal.title.lower(), deal)
        if not deals and errors == len(self.queries):
            raise SourceError(f"{self.source_name} no respondió en ninguna búsqueda.")
        return list(deals.values())

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        if deal.source != self.source_name:
            return None
        try:
            return self._lookup(deal.external_id)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None

    def search(self, query: str) -> list[DealObservation]:
        raise NotImplementedError

    def _lookup(self, external_id: str) -> DealObservation | None:
        raise NotImplementedError


def _vtex_title(product: dict) -> str:
    """Readable name: Nike's "Name | Description | Colour" title when present, else the product name
    (plus its category when the name alone is just a model, e.g. "Nike Zenvy - Leggings")."""
    title = (product.get("productTitle") or "").strip()
    if "|" in title:
        parts = [p.strip() for p in title.split("|") if p.strip() and "www." not in p and not p.strip().endswith(" Co")]
        if parts:
            return " - ".join(parts)
    name = (product.get("productName") or "").strip()
    categories = product.get("categories") or []
    if len(name.split()) <= 3 and categories:
        leaf = categories[0].strip("/").split("/")[-1]
        if leaf and leaf.lower() not in name.lower():
            return f"{name} - {leaf}"
    return name


class VtexStoreSource(_StoreSource):
    """Stores on VTEX (Éxito, Nike Colombia, Tennis) expose a public catalog search at /api/catalog_system/pub.
    With `order="OrderByBestDiscountDESC"` and an empty query it lists the store's biggest discounts."""

    def __init__(self, source_name: str, base_url: str, session: requests.Session | None = None,
                 order: str | None = None, **kwargs) -> None:
        super().__init__(source_name, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.session = _session(session)
        self.order = order

    def _get(self, params: str) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/api/catalog_system/pub/products/search?{params}", timeout=(5, 25))
        resp.raise_for_status()
        return resp.json()

    def _parse(self, product: dict) -> DealObservation | None:
        # First size/colour in stock sold by the store itself (VTEX seller "1"); marketplace sellers
        # inflate list prices.
        for item in product.get("items") or []:
            seller = next((s for s in item.get("sellers", []) if s.get("sellerId") == "1"), None)
            offer = seller["commertialOffer"] if seller else {}
            if offer.get("IsAvailable") and offer.get("AvailableQuantity", 0) > 0:
                images = item.get("images") or [{}]
                return _deal(
                    self.source_name, str(product["productId"]), _vtex_title(product),
                    float(offer.get("Price") or 0), float(offer.get("ListPrice") or 0),
                    product.get("link", ""), images[0].get("imageUrl", ""),
                )
        return None

    def search(self, query: str) -> list[DealObservation]:
        params = {"ft": query} if query else {}
        params.update({"_from": 0, "_to": self.per_query - 1})
        if self.order:
            params["O"] = self.order
        products = self._get(urlencode(params))
        return [d for p in products if (d := self._parse(p))]

    def _lookup(self, external_id: str) -> DealObservation | None:
        products = self._get(urlencode({"fq": f"productId:{external_id}"}))
        return self._parse(products[0]) if products else None


class AlgoliaStoreSource(_StoreSource):
    """Alkosto/Ktronix search through Algolia with a public search-only key published on their sites."""

    APP_ID = "QX5IPS1B1Q"
    SEARCH_KEY = "7a8800d62203ee3a9ff1cdf74f99b268"

    def __init__(self, source_name: str, index: str, base_url: str, session: requests.Session | None = None, **kwargs) -> None:
        super().__init__(source_name, **kwargs)
        self.index = index
        self.base_url = base_url.rstrip("/")
        self.session = _session(session)

    def _query(self, params: dict) -> list[dict]:
        resp = self.session.post(
            f"https://{self.APP_ID}-dsn.algolia.net/1/indexes/{self.index}/query",
            headers={
                "X-Algolia-Application-Id": self.APP_ID,
                "X-Algolia-API-Key": self.SEARCH_KEY,
                "Referer": f"{self.base_url}/",
            },
            json={"params": urlencode(params)},
            timeout=(5, 20),
        )
        resp.raise_for_status()
        return resp.json()["hits"]

    def _parse(self, hit: dict) -> DealObservation | None:
        if not hit.get("instockflag_boolean"):
            return None
        price = float(hit.get("discountprice_double") or hit.get("lowestprice_double") or 0)
        before = float(hit.get("baseprice_cop_string") or hit.get("pricevalue_cop_double") or 0)
        path = hit.get("url_es_string", "")
        return _deal(
            self.source_name, str(hit["objectID"]), hit.get("name_text_es", ""), price, before,
            f"{self.base_url}{path}" if path else "", hit.get("img-750wx750h_string", ""),
            rating=float(hit["averagescore_double"]) if hit.get("averagescore_double") else None,
            reviews=int(hit["totalreviews_int"]) if hit.get("totalreviews_int") else None,
        )

    def search(self, query: str) -> list[DealObservation]:
        return [d for h in self._query({"query": query, "hitsPerPage": self.per_query}) if (d := self._parse(h))]

    def _lookup(self, external_id: str) -> DealObservation | None:
        hits = self._query({"query": "", "filters": f'objectID:"{external_id}"', "hitsPerPage": 1})
        return self._parse(hits[0]) if hits else None


_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _cop(value) -> float:
    """'249.900' (or ['249.900']) -> 249900.0"""
    if isinstance(value, list):
        value = value[0] if value else ""
    digits = re.sub(r"[^\d]", "", str(value).split(",")[0])
    return float(digits) if digits else 0.0


class FalabellaBrandSource(_StoreSource):
    """Falabella Colombia brand pages (Adidas, Nike, Puma, New Balance). Falabella's own filters keep only
    items with 40%+ discount sold by Falabella itself (no marketplace sellers). The page is heavy, so
    each scan reads one brand in turn: every brand is refreshed every couple of hours."""

    BASE = "https://www.falabella.com.co/falabella-co"
    FILTERS = "f.range.derived.variant.discount=40%25+dcto+y+m%C3%A1s&f.derived.variant.sellerId=FALABELLA"

    def __init__(self, brands: tuple[str, ...] = ("ADIDAS", "NIKE", "PUMA", "NEW BALANCE"),
                 session: requests.Session | None = None, clock=time.time, **kwargs) -> None:
        super().__init__("falabella", queries=brands, **kwargs)
        self.session = _session(session)
        self.clock = clock

    def fetch(self) -> list[DealObservation]:
        brand = self.queries[int(self.clock() // 1800) % len(self.queries)]  # one brand per half hour
        try:
            return self.search(brand)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise SourceError(f"Falabella no respondió ({brand}): {type(exc).__name__}") from None

    def _page(self, url: str) -> dict:
        resp = self.session.get(url, timeout=(5, 30))
        resp.raise_for_status()
        match = _NEXT_DATA.search(resp.text)
        if not match:
            raise ValueError("página sin datos de producto")
        return json.loads(match.group(1))["props"]["pageProps"]

    def search(self, brand: str) -> list[DealObservation]:
        page = self._page(f"{self.BASE}/brand/{quote(brand)}?{self.FILTERS}")
        return [d for r in page.get("results") or [] if (d := self._parse_result(r))]

    def _parse_result(self, r: dict) -> DealObservation | None:
        if (r.get("sellerName") or "").strip().lower() != "falabella":
            return None
        prices = {p.get("type"): _cop(p.get("price")) for p in r.get("prices") or []}
        title = (r.get("displayName") or "").strip()
        brand = (r.get("brand") or "").strip().title()
        if brand and brand.lower() not in title.lower():
            title = f"{title} {brand}"
        # The discount belongs to one variant (colour/size): keep its SKU and link straight to it.
        sku = str(r.get("skuId") or "")
        url = r.get("url", "")
        if sku and url and not url.rstrip("/").endswith(sku):
            url = f"{url.rstrip('/')}/{sku}"
        return _deal("falabella", f"{r.get('productId')}-{sku}" if sku else str(r.get("productId")), title,
                     prices.get("internetPrice", 0), prices.get("normalPrice", 0), url, (r.get("mediaUrls") or [""])[0])

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        if deal.source != self.source_name:
            return None
        try:
            data = self._page(deal.url)["productData"]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None
        sku = deal.external_id.split("-")[-1] if "-" in deal.external_id else str(data.get("currentVariant") or "")
        variant = next((v for v in data.get("variants") or [] if str(v.get("id")) == sku), None)
        if variant is None or not variant.get("isPurchaseable") or data.get("isOutOfStock"):
            return None
        sellers = {(o.get("sellerName") or "").lower() for o in variant.get("offerings") or []}
        if sellers and "falabella" not in sellers:
            return None
        prices = {p.get("type"): _cop(p.get("price")) for p in variant.get("prices") or []}
        fresh = _deal("falabella", deal.external_id, deal.title, prices.get("internetPrice", 0),
                      prices.get("normalPrice", 0), deal.url, deal.image_url)
        return fresh


def colombia_store_sources() -> list[DealSource]:
    return [
        # Grocery savings are a few thousand pesos, so Éxito gets a lower savings floor.
        VtexStoreSource(
            "exito", "https://www.exito.com", per_query=50,
            queries=APPLIANCE_QUERIES + GROCERY_QUERIES, min_savings_cop=3000,
        ),
        # Alkosto search is fuzzy (grocery words return headphones), so only appliance terms.
        AlgoliaStoreSource("alkosto", "alkostoIndexAlgoliaPRD", "https://www.alkosto.com", queries=APPLIANCE_QUERIES, per_query=40),
        AlgoliaStoreSource("ktronix", "ktronixIndexAlgoliaPRD", "https://www.ktronix.com", queries=APPLIANCE_QUERIES, per_query=40),
        # Fashion and sportswear: one request each, the store's biggest discounts first.
        VtexStoreSource("nike", "https://www.nike.com.co", queries=("",), per_query=50, order="OrderByBestDiscountDESC"),
        VtexStoreSource("tennis", "https://www.tennis.com.co", queries=("",), per_query=50, order="OrderByBestDiscountDESC"),
        FalabellaBrandSource(),
    ]
