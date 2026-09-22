from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.models import DealObservation, DiscountEvidence, to_minor
from app.sources.base import DealSource, SourceError


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí"}


class PartnerFeedSource(DealSource):
    """Read an authorized affiliate/partner JSON or CSV feed from disk."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.source_name = "partner_feed"

    def fetch(self) -> list[DealObservation]:
        if not self.path.exists():
            return []
        records = self._read_records()
        deals: list[DealObservation] = []
        for number, raw in enumerate(records, start=1):
            try:
                deals.append(self._parse(raw))
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError(f"Registro {number} inválido en {self.path}.") from exc
        return deals

    def _read_records(self) -> list[dict[str, Any]]:
        suffix = self.path.suffix.lower()
        if suffix == ".json":
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SourceError(f"{self.path} no contiene JSON válido.") from exc
            if not isinstance(data, list):
                raise SourceError("El feed JSON debe ser una lista.")
            return [row for row in data if isinstance(row, dict)]
        if suffix == ".csv":
            with self.path.open(newline="", encoding="utf-8-sig") as handle:
                return list(csv.DictReader(handle))
        raise SourceError("El feed autorizado debe ser .json o .csv.")

    @staticmethod
    def _parse(raw: dict[str, Any]) -> DealObservation:
        currency = str(raw["currency"]).upper()
        price_minor = to_minor(raw["price"], currency)
        original_minor = None
        evidence = DiscountEvidence.NONE
        if raw.get("original_price") not in (None, "") and _truthy(raw.get("original_price_verified")):
            value = to_minor(raw["original_price"], currency)
            if value > price_minor:
                original_minor = value
                evidence = DiscountEvidence.OFFICIAL_ORIGINAL
        observed = raw.get("observed_at")
        observed_at = datetime.fromisoformat(str(observed).replace("Z", "+00:00")) if observed else datetime.now(timezone.utc)
        return DealObservation(
            source=str(raw["source"]),
            external_id=str(raw["id"]),
            title=str(raw["title"]),
            price_minor=price_minor,
            original_price_minor=original_minor,
            evidence=evidence,
            currency=currency,
            url=str(raw["url"]),
            image_url=str(raw.get("image_url") or ""),
            available=_truthy(raw.get("available", True)),
            affiliate=_truthy(raw.get("affiliate", False)),
            shipping_note=str(raw.get("shipping_note") or "Envío e impuestos según la tienda"),
            observed_at=observed_at,
        )

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        for fresh in self.fetch():
            if fresh.source == deal.source and fresh.external_id == deal.external_id:
                return fresh
        return None
