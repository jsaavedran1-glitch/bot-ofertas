"""Daily "Top 5 ofertas de hoy": the best approved deals as one multi-photo (carousel) post."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.copywriter import trend_tags
from app.models import Candidate, DiscountEvidence, from_minor, money, store_name, to_minor, variant_key

NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
MAX_ITEMS, MIN_ITEMS, MAX_PER_STORE = 5, 3, 2


def pick_top(candidates: list[Candidate]) -> list[Candidate]:
    """Highest-scored candidates (already sorted by score), at most MAX_PER_STORE per store for variety."""
    picked: list[Candidate] = []
    per_store: dict[str, int] = {}
    keys: set[str] = set()
    for c in candidates:
        if c.deal.evidence == DiscountEvidence.GIFT_CARD_VALUE:
            continue  # "pay X, get Y of balance" needs its own wording: single posts only
        key = variant_key(c.deal.title)
        if per_store.get(c.deal.source, 0) >= MAX_PER_STORE or key in keys:
            continue
        keys.add(key)
        picked.append(c)
        per_store[c.deal.source] = per_store.get(c.deal.source, 0) + 1
        if len(picked) == MAX_ITEMS:
            break
    return picked


def _price(minor: int, currency: str, rate: Decimal | None) -> str:
    if currency == "USD" and rate:
        return money(to_minor(from_minor(minor, "USD") * rate, "COP"), "COP")
    return money(minor, currency)


def top_copy(items: list[Candidate], rate: Decimal | None, link_in_comment: bool, now: datetime | None = None) -> str:
    local = (now or datetime.now(ZoneInfo("America/Bogota"))).astimezone(ZoneInfo("America/Bogota"))
    lines = [f"🔥 TOP {len(items)} OFERTAS DE HOY 🔥", "Las mejores que encontramos hoy, desliza 👉", ""]
    for n, c in zip(NUMBERS, items):
        d = c.deal
        lines.append(f"{n} {d.title} · {store_name(d.source)}")
        before = f" (antes {_price(d.original_price_minor, d.currency, rate)})" if d.original_price_minor else ""
        lines.append(f"   💥 {_price(d.price_minor, d.currency, rate)}{before} -{d.discount_pct}%")
        if not link_in_comment:
            lines.append(f"   👉 {d.url}")
        lines.append("")
    if link_in_comment:
        lines += ["👇 Links de cada oferta en el primer comentario", ""]
    lines += [
        f"Verificado el {local:%d/%m/%Y a las %H:%M}. Precios sujetos a cambios.",
        "",
        "¿Cuál de estas te sirve más? 👇",
        "",
        " ".join(f"#OjoAlPrecio #ofertas #descuentos #Colombia {trend_tags('top' + local.date().isoformat())}".split()),
    ]
    return "\n".join(lines)


def top_links(items: list[Candidate]) -> str:
    from app.links import post_ref, tracked

    return "\n".join(f"{n} {tracked(c.deal.url, post_ref('top5', c.id))}" for n, c in zip(NUMBERS, items))
