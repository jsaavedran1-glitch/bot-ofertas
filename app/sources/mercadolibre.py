from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

SITE = "MCO"
BASE = "https://api.mercadolibre.com"

CATEGORIES = [
    "MCO1144",  # Computación
    "MCO1051",  # Electrónica
    "MCO1574",  # Celulares y Smartphones
    "MCO1132",  # Videojuegos y Consolas
    "MCO1500",  # Electrodomésticos
    "MCO1648",  # Herramientas
    "MCO1276",  # Deportes y Fitness
    "MCO1367",  # Hogar y Muebles
    "MCO5726",  # Belleza y Cuidado Personal
]


class AuthenticatedMercadoLibreSource(DealSource):
    """Resolve the rotating token only when this source is actually used."""

    source_name = "mercadolibre"

    def __init__(self, queries: tuple[str, ...], limit: int, token_provider: Callable[[], str]) -> None:
        self.queries = queries
        self.limit = limit
        self.token_provider = token_provider

    def _source(self) -> "MercadoLibreSource":
        return MercadoLibreSource(self.limit, self.token_provider())

    def fetch(self) -> list[DealObservation]:
        try:
            return self._source().fetch()
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError("No fue posible autenticar Mercado Libre.") from exc

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        try:
            return self._source().revalidate(deal)
        except Exception:
            return None


class MercadoLibreSource(DealSource):
    source_name = "mercadolibre"

    def __init__(
        self,
        limit_per_category: int = 20,
        access_token: str = "",
        session: requests.Session | None = None,
    ) -> None:
        if not access_token:
            raise SourceError("Mercado Libre requiere ML_ACCESS_TOKEN o credenciales OAuth configuradas.")
        self.limit = min(max(limit_per_category, 1), 50)
        self.access_token = access_token
        self.session = session or requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def fetch(self) -> list[DealObservation]:
        deals: dict[str, DealObservation] = {}
        category_errors = 0
        for cat in CATEGORIES:
            try:
                resp = self.session.get(
                    f"{BASE}/highlights/{SITE}/category/{cat}",
                    headers=self._h(),
                    timeout=(5, 20),
                )
                resp.raise_for_status()
                prod_ids = [x["id"] for x in resp.json().get("content", []) if x.get("id")]
            except (requests.RequestException, ValueError, KeyError):
                category_errors += 1
                continue

            for prod_id in prod_ids[: self.limit]:
                if prod_id in deals:
                    continue
                deal = self._fetch_product(prod_id)
                if deal:
                    deals[prod_id] = deal

        if not deals and category_errors == len(CATEGORIES):
            raise SourceError("Mercado Libre no respondió en ninguna categoría.")

        return list(deals.values())

    def _fetch_product(self, prod_id: str) -> DealObservation | None:
        try:
            r_items = self.session.get(
                f"{BASE}/products/{prod_id}/items",
                headers=self._h(),
                timeout=(5, 20),
            )
            r_items.raise_for_status()
            items = r_items.json().get("results", [])
        except (requests.RequestException, ValueError):
            return None

        # Only new items with MercadoPago
        items = [
            i for i in items
            if i.get("condition") == "new"
            and i.get("accepts_mercadopago", True)
            and i.get("price", 0) > 0
            and i.get("original_price") is not None
            and i["original_price"] > i["price"]
        ]
        if not items:
            return None

        best = min(items, key=lambda x: x["price"])
        item_id = best.get("item_id") or best.get("id") or ""
        price = best["price"]
        orig = best["original_price"]
        currency = str(best.get("currency_id", "COP")).upper()
        if currency not in {"COP", "USD"}:
            return None

        try:
            price_minor = to_minor(price, currency)
            orig_minor = to_minor(orig, currency)
        except (ValueError, TypeError):
            return None

        if orig_minor <= price_minor:
            return None

        # Get product metadata (title + image)
        title, image_url = prod_id, ""
        try:
            r_prod = self.session.get(
                f"{BASE}/products/{prod_id}",
                headers=self._h(),
                timeout=(5, 15),
            )
            r_prod.raise_for_status()
            prod = r_prod.json()
            title = str(prod.get("name") or prod_id).strip()
            pics = prod.get("pictures") or [{}]
            image_url = str(pics[0].get("url") or "")
        except (requests.RequestException, ValueError):
            pass

        # Permalink: use item URL if available, else product catalog URL
        url = f"https://www.mercadolibre.com.co/p/{prod_id}"
        if item_id:
            try:
                r_item = self.session.get(
                    f"{BASE}/items/{item_id}",
                    headers=self._h(),
                    timeout=(5, 15),
                )
                r_item.raise_for_status()
                item_data = r_item.json()
                pl = item_data.get("permalink") or ""
                if pl.startswith("http"):
                    url = pl
                if not image_url:
                    image_url = str(item_data.get("secure_thumbnail") or item_data.get("thumbnail") or "")
            except (requests.RequestException, ValueError):
                pass

        shipping = best.get("shipping") or {}
        free_shipping = bool(shipping.get("free_shipping"))

        return DealObservation(
            source="mercadolibre",
            external_id=prod_id,
            title=title,
            price_minor=price_minor,
            original_price_minor=orig_minor,
            evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency=currency,
            url=url,
            image_url=image_url,
            available=True,
            shipping_note="Envío gratis" if free_shipping else "Envío según destino",
            observed_at=datetime.now(timezone.utc),
        )

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        if deal.source != self.source_name:
            return None
        return self._fetch_product(deal.external_id)
