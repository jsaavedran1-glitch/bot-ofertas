from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

APPLIANCE_QUERIES = ("televisor", "nevera", "consola")
GROCERY_QUERIES = ("detergente", "cafe", "aceite", "arroz", "atun", "cerveza", "papel higienico")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _session(session: requests.Session | None) -> requests.Session:
    session = session or requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))))
    session.headers["User-Agent"] = _UA
    return session


def _deal(source: str, external_id: str, title: str, price: float, before: float, url: str, image: str) -> DealObservation | None:
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
                found = self._search(query)
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

    def _search(self, query: str) -> list[DealObservation]:
        raise NotImplementedError

    def _lookup(self, external_id: str) -> DealObservation | None:
        raise NotImplementedError


class VtexStoreSource(_StoreSource):
    """Stores on VTEX (Éxito) expose a public catalog search at /api/catalog_system/pub."""

    def __init__(self, source_name: str, base_url: str, session: requests.Session | None = None, **kwargs) -> None:
        super().__init__(source_name, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.session = _session(session)

    def _get(self, params: str) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/api/catalog_system/pub/products/search?{params}", timeout=(5, 25))
        resp.raise_for_status()
        return resp.json()

    def _parse(self, product: dict) -> DealObservation | None:
        item = product["items"][0]
        # Only the store's own offer (VTEX seller "1"); marketplace sellers inflate list prices.
        seller = next((s for s in item.get("sellers", []) if s.get("sellerId") == "1"), None)
        if seller is None:
            return None
        offer = seller["commertialOffer"]
        if not offer.get("IsAvailable") or offer.get("AvailableQuantity", 0) <= 0:
            return None
        images = item.get("images") or [{}]
        return _deal(
            self.source_name, str(product["productId"]), product.get("productName", ""),
            float(offer.get("Price") or 0), float(offer.get("ListPrice") or 0),
            product.get("link", ""), images[0].get("imageUrl", ""),
        )

    def _search(self, query: str) -> list[DealObservation]:
        products = self._get(urlencode({"ft": query, "_from": 0, "_to": self.per_query - 1}))
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
        )

    def _search(self, query: str) -> list[DealObservation]:
        return [d for h in self._query({"query": query, "hitsPerPage": self.per_query}) if (d := self._parse(h))]

    def _lookup(self, external_id: str) -> DealObservation | None:
        hits = self._query({"query": "", "filters": f'objectID:"{external_id}"', "hitsPerPage": 1})
        return self._parse(hits[0]) if hits else None


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
    ]
