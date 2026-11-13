"""Product feeds from Awin advertisers (first one: Adidas CO, publisher 3154846).

Awin's "Create-a-Feed" gives one CSV (often gzip-compressed) with the advertiser's whole catalogue. The
download URL carries the publisher's API key, so it lives in a secret and never appears in errors or logs.
Prices come from the advertiser itself: the old/list price it publishes is the reference, and the deep link
already has the affiliate tracking. Awin terms only allow promoting offers that are in the feed.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import gzip
import io
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.models import EXPONENTS, DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

CACHE_MINUTES = 30
MAX_ITEMS = 200  # best discounts only: a full catalogue would flood the database every run
_IN_STOCK = {"1", "yes", "true", "y", "in stock", "instock", "in_stock"}


def _price(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def _https(value: str | None) -> str:
    url = (value or "").strip()
    return url if url.startswith("https://") else ""


def _in_stock(row: dict) -> bool:
    flag = (row.get("in_stock") or "").strip().lower()
    status = (row.get("stock_status") or "").strip().lower()
    if flag:
        return flag in _IN_STOCK
    return status in _IN_STOCK


class AwinFeedSource(DealSource):
    def __init__(self, feed_url: str, source_name: str = "adidas", session: requests.Session | None = None,
                 now=None, min_discount_pct: int | None = None, min_savings_cop: int | None = None,
                 max_items: int = MAX_ITEMS) -> None:
        self._feed_url = feed_url  # contains the API key: never print it
        self.source_name = source_name
        self.source_names = {source_name}
        self.min_discount_pct = min_discount_pct  # None = use the global MIN_DISCOUNT_PCT
        self.min_savings_cop = min_savings_cop  # None = use the global MIN_SAVINGS_COP
        self.max_items = max_items
        self.session = session or requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.5,
                                                                     status_forcelist=(429, 500, 502, 503, 504))))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._cache: tuple[datetime, dict[str, DealObservation]] | None = None

    def __repr__(self) -> str:  # keep the key out of tracebacks and debug prints
        return f"AwinFeedSource(source_name={self.source_name!r})"

    def fetch(self) -> list[DealObservation]:
        deals = sorted(self._catalogue().values(), key=lambda d: d.discount_pct, reverse=True)
        floor = self.min_discount_pct or 0
        return [d for d in deals if d.discount_pct >= floor][: self.max_items]

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        """The same product, still in stock, from the feed (downloaded at most every 30 minutes)."""
        if deal.source != self.source_name:
            return None
        try:
            return self._catalogue().get(deal.external_id)
        except SourceError:
            return None

    def _catalogue(self) -> dict[str, DealObservation]:
        now = self.now()
        if self._cache and now - self._cache[0] < timedelta(minutes=CACHE_MINUTES):
            return self._cache[1]
        items = self._parse(self._download())
        self._cache = (now, items)
        return items

    def _download(self) -> bytes:
        if not self._feed_url.startswith("https://"):
            raise SourceError(f"{self.source_name}: la URL del feed de Awin no es https.")
        try:
            response = self.session.get(self._feed_url, timeout=(10, 120))
            response.raise_for_status()
            data = response.content
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "")
            raise SourceError(f"Awin ({self.source_name}) no respondió: {type(exc).__name__} {status}".rstrip()) from None
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                raise SourceError(f"Awin ({self.source_name}): el feed comprimido está dañado.") from None
        return data

    def _parse(self, data: bytes) -> dict[str, DealObservation]:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        try:
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or "aw_product_id" not in reader.fieldnames:
                raise SourceError(f"Awin ({self.source_name}): el feed no tiene el formato esperado.")
            items: dict[str, DealObservation] = {}
            for row in reader:
                deal = self._row(row)
                if deal and deal.external_id not in items:
                    items[deal.external_id] = deal
        except csv.Error:
            raise SourceError(f"Awin ({self.source_name}): no se pudo leer el feed.") from None
        return items

    def _row(self, row: dict) -> DealObservation | None:
        external_id = (row.get("aw_product_id") or "").strip()
        title = " ".join((row.get("product_name") or "").split())
        url = _https(row.get("aw_deep_link"))
        if not external_id or not title or not url or not _in_stock(row):
            return None
        price = _price(row.get("search_price")) or _price(row.get("store_price"))
        if price is None:
            return None
        original = next((p for p in (_price(row.get("product_price_old")), _price(row.get("rrp_price"))) if p and p > price), None)
        if original is None:
            return None
        currency = (row.get("currency") or "COP").strip().upper() or "COP"
        if currency not in EXPONENTS:
            return None
        image = _https(row.get("large_image")) or _https(row.get("merchant_image_url")) or _https(row.get("aw_image_url"))
        try:
            return DealObservation(
                source=self.source_name,
                external_id=external_id,
                title=title,
                price_minor=to_minor(price, currency),
                original_price_minor=to_minor(original, currency),
                evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                currency=currency,
                url=url,
                image_url=image,
                available=True,
                affiliate=True,
                shipping_note="Envío según destino",
                observed_at=self.now(),
            )
        except (ValueError, ArithmeticError) as exc:
            print(f"Advertencia Awin ({self.source_name}): producto {external_id} omitido ({type(exc).__name__})",
                  file=sys.stderr)
            return None
