from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlparse

from app.models import DealObservation, DiscountEvidence


def validate_observation(deal: DealObservation) -> tuple[bool, str]:
    if not deal.source or not deal.external_id or not deal.title.strip():
        return False, "identidad incompleta"
    if deal.currency not in {"COP", "USD"}:
        return False, "moneda no soportada"
    if deal.price_minor <= 0:
        return False, "precio inválido"
    parsed = urlparse(deal.url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False, "URL no HTTPS"
    if not deal.available:
        return False, "sin inventario"
    if deal.original_price_minor is not None:
        if deal.original_price_minor <= deal.price_minor:
            return False, "referencia no supera el precio"
        if deal.evidence == DiscountEvidence.NONE:
            return False, "referencia sin evidencia"
    return True, "ok"


def savings_cop(deal: DealObservation, usd_cop_rate: Decimal | None) -> int:
    if not deal.original_price_minor:
        return 0
    saving_minor = deal.original_price_minor - deal.price_minor
    if deal.currency == "COP":
        return saving_minor
    if usd_cop_rate is None:
        return 0
    saving_usd = Decimal(saving_minor) / Decimal(100)
    return int((saving_usd * usd_cop_rate).quantize(Decimal("1"), rounding=ROUND_DOWN))


def qualifies(
    deal: DealObservation,
    min_discount_pct: int,
    min_savings_cop: int,
    usd_cop_rate: Decimal | None,
) -> bool:
    return (
        deal.evidence in {DiscountEvidence.OFFICIAL_ORIGINAL, DiscountEvidence.OWN_HISTORY}
        and deal.discount_pct >= min_discount_pct
        and savings_cop(deal, usd_cop_rate) >= min_savings_cop
    )


def score(deal: DealObservation) -> int:
    evidence_bonus = 20 if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL else 10
    return min(100, deal.discount_pct + evidence_bonus)
