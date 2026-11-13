"""Mercado Libre deals picked by hand from Mercado Libre Afiliados.

Mercado Libre stopped exposing prices to uncertified apps, so the person chooses the deals in the
affiliate panel and lists them in a CSV with columns: link, precio, antes[, titulo, foto, hasta].
Title and photo are filled from the public catalog when the link points to a catalog product.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Callable

import requests

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError

_PRODUCT_RE = re.compile(r"/p/(MCO\d+)|\b(MCO-?\d{6,})\b", re.IGNORECASE)


def _pesos(value: str) -> int:
    digits = re.sub(r"[^\d]", "", str(value).split(",")[0])
    if not digits:
        raise ValueError("precio vacío")
    return int(digits)


class MercadoLibreAffiliateSource(DealSource):
    source_name = "mercadolibre"
    source_names = {"mercadolibre"}
    min_discount_pct = 10  # hand-picked by the person: trust their choice over the global 35%

    def __init__(self, path: str, token_provider: Callable[[], str] | None = None,
                 session: requests.Session | None = None) -> None:
        self.path = Path(path)
        self.token_provider = token_provider
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; OjoAlPrecio/1.0)")
        self._cache: dict[str, DealObservation] = {}

    def fetch(self) -> list[DealObservation]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(handle)]
        deals = []
        for number, row in enumerate(rows, start=2):
            if not row.get("link"):
                continue
            hasta = row.get("hasta")
            if hasta and date.fromisoformat(hasta) < date.today():
                continue
            try:
                deals.append(self._parse(row))
            except (ValueError, KeyError) as exc:
                raise SourceError(f"Fila {number} de {self.path.name}: {exc}") from exc
        self._cache = {d.external_id: d for d in deals}
        return deals

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        if deal.source != self.source_name:
            return None
        if not self._cache:
            self.fetch()
        return self._cache.get(deal.external_id)

    def _parse(self, row: dict) -> DealObservation:
        link = row["link"]
        if not link.startswith("https://"):
            raise ValueError("el link debe empezar por https://")
        price, before = _pesos(row["precio"]), _pesos(row["antes"])
        if before <= price:
            raise ValueError("'antes' debe ser mayor que 'precio'")
        product_id = self._product_id(link)
        title, photo = row.get("titulo", ""), row.get("foto", "")
        if product_id and not (title and photo):
            catalog_title, catalog_photo = self._catalog(product_id)
            title, photo = title or catalog_title, photo or catalog_photo
        if not title:
            raise ValueError("falta 'titulo' (el link no es de un producto de catálogo)")
        return DealObservation(
            source=self.source_name,
            external_id=product_id or "af_" + hashlib.sha256(link.encode()).hexdigest()[:16],
            title=title,
            price_minor=to_minor(price, "COP"),
            original_price_minor=to_minor(before, "COP"),
            evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="COP",
            url=link,
            image_url=photo,
            available=True,
            affiliate=True,
            shipping_note="Envío según destino",
            observed_at=datetime.now(timezone.utc),
        )

    def _product_id(self, link: str) -> str | None:
        match = _PRODUCT_RE.search(link)
        if not match:
            # Affiliate short links (mercadolibre.com/sec/...) redirect to the product page.
            try:
                response = self.session.head(link, allow_redirects=True, timeout=(5, 15))
                match = _PRODUCT_RE.search(response.url)
            except requests.RequestException:
                return None
        if not match:
            return None
        return (match.group(1) or match.group(2)).upper().replace("-", "")

    def _catalog(self, product_id: str) -> tuple[str, str]:
        headers = {"Authorization": f"Bearer {self.token_provider()}"} if self.token_provider else {}
        try:
            r = self.session.get(f"https://api.mercadolibre.com/products/{product_id}", headers=headers, timeout=(5, 15))
            if not r.ok:
                return "", ""
            data = r.json()
        except (requests.RequestException, ValueError):
            return "", ""
        pictures = data.get("pictures") or [{}]
        return str(data.get("name") or "").strip(), str(pictures[0].get("url") or "")
