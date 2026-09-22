from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
import hashlib


class DiscountEvidence(str, Enum):
    OFFICIAL_ORIGINAL = "official_original"
    OWN_HISTORY = "own_history"
    NONE = "none"


EXPONENTS = {"COP": 0, "USD": 2}


def to_minor(value: object, currency: str) -> int:
    code = currency.upper()
    if code not in EXPONENTS:
        raise ValueError(f"Moneda no soportada: {code}")
    multiplier = Decimal(10) ** EXPONENTS[code]
    return int((Decimal(str(value)) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(value: int, currency: str) -> Decimal:
    return Decimal(value) / (Decimal(10) ** EXPONENTS[currency.upper()])


def money(value: int, currency: str) -> str:
    amount = from_minor(value, currency)
    if currency == "COP":
        return f"${amount:,.0f} COP".replace(",", ".")
    return f"US${amount:,.2f}"


STORE_NAMES = {"mercadolibre": "Mercado Libre", "exito": "Éxito"}


def store_name(source: str) -> str:
    return STORE_NAMES.get(source, source.replace("_", " ").title())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DealObservation:
    source: str
    external_id: str
    title: str
    price_minor: int
    currency: str
    url: str
    image_url: str = ""
    original_price_minor: int | None = None
    evidence: DiscountEvidence = DiscountEvidence.NONE
    available: bool = True
    affiliate: bool = False
    shipping_note: str = ""
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", self.source.strip().lower())
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "title", " ".join(self.title.split())[:300])
        object.__setattr__(self, "shipping_note", " ".join(self.shipping_note.split())[:240])
        if self.observed_at.tzinfo is None:
            object.__setattr__(self, "observed_at", self.observed_at.replace(tzinfo=timezone.utc))

    @property
    def discount_pct(self) -> int:
        reference = self.original_price_minor
        if not reference or reference <= self.price_minor:
            return 0
        value = Decimal(reference - self.price_minor) * 100 / Decimal(reference)
        return int(value.quantize(Decimal("1"), rounding=ROUND_DOWN))

    @property
    def candidate_id(self) -> str:
        payload = f"{self.source}:{self.external_id}:{self.currency}:{self.price_minor}:{self.original_price_minor}:{self.evidence.value}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def with_history_reference(self, reference_minor: int) -> "DealObservation":
        return replace(
            self,
            original_price_minor=reference_minor,
            evidence=DiscountEvidence.OWN_HISTORY,
        )


@dataclass(frozen=True)
class Candidate:
    id: str
    deal: DealObservation
    score: int
    status: str
    created_at: datetime
