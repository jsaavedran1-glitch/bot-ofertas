from __future__ import annotations

import re
import unicodedata

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


def rating_text(deal: "DealObservation") -> str | None:
    """'4,8 (648 opiniones)' when the store's rating is worth showing (4+ stars, 5+ reviews)."""
    if deal.rating is None or deal.reviews is None or deal.reviews < 5 or deal.rating < 4.0:
        return None
    return f"{deal.rating:.1f}".replace(".", ",") + f" ({deal.reviews:,} opiniones)".replace(",", ".")


def store_name(source: str) -> str:
    return STORE_NAMES.get(source, source.replace("_", " ").title())


_COLORS = {
    "negro", "negra", "blanco", "blanca", "gris", "plata", "plateado", "plateada", "azul", "rojo", "roja", "rosado",
    "rosada", "rosa", "verde", "morado", "morada", "lila", "beige", "dorado", "dorada", "grafito", "titanio", "crema",
    "black", "white", "silver", "gray", "grey", "blue", "pink", "inox", "cafe", "naranja", "amarillo", "amarilla",
}
_MODEL_CODE = re.compile(r"^(?=(?:.*\d){2})(?=(?:.*[A-Za-z]){2})[A-Za-z0-9][A-Za-z0-9/._-]{5,}$")
_METRIC = re.compile(r"\s+\d+(?:[.,]\d+)?\s*cm\b", re.IGNORECASE)


def variant_key(title: str) -> str:
    """Same product in another colour, or the same catalogue item at Alkosto and Ktronix, gets the same key
    (e.g. "Audífonos SONY WH-CH520 Negro" / "... Azul"). Sizes and model numbers still tell products apart."""
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[|/,;:()\[\]+]+", " ", text)
    return " ".join(w for w in text.split() if w.strip(".-") not in _COLORS)


def display_title(title: str) -> str:
    """Shorter product name for images: no model codes, metric duplicates or trailing color.
    The caption keeps the full title (it is what people search for)."""
    text = re.sub(r'"\s*pulgadas\b', '"', title, flags=re.IGNORECASE)
    if '"' in text:
        text = _METRIC.sub("", text)
    words = [w for w in text.split() if not _MODEL_CODE.match(w.strip("(),"))]
    while len(words) > 3 and words[-1].lower().strip(".,") in _COLORS:
        words.pop()
    clean = " ".join(words).strip(" ,-+/")
    return clean if len(clean) >= 8 else title


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AiCopy:
    """Words written by Claude for one post (see app/ai.py). Never prices: those come from our data."""
    short_name: str | None = None  # Spanish product name for images
    title_es: str | None = None  # full title in Spanish (Amazon titles arrive in English)
    hook: str | None = None
    points: tuple[str, ...] = ()
    question: str | None = None
    hashtags: tuple[str, ...] = ()  # chosen by Claude from the finished image; #OjoAlPrecio is always added


def display_name(deal: "DealObservation") -> str:
    """Product name for images and hooks: Claude's Spanish short name, else the cleaned store title."""
    if deal.ai and deal.ai.short_name:
        return deal.ai.short_name
    return display_title(deal.title)


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
    rating: float | None = None  # store's average review score (0-5)
    reviews: int | None = None  # number of reviews behind `rating`
    lowest_30d: bool = False  # set at publish time from our own price history; not stored
    ai: AiCopy | None = None  # set at publish time by Claude when ANTHROPIC_API_KEY is configured; not stored

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
