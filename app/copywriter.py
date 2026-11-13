from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import os
from zoneinfo import ZoneInfo

from app.models import DealObservation, DiscountEvidence, display_name, from_minor, money, rating_text, store_name


def _short(title: str, limit: int = 45) -> str:
    if len(title) <= limit:
        return title
    return title[:limit].rsplit(" ", 1)[0].rstrip(",.-") + "…"


def _prices(deal: DealObservation, usd_cop_rate: Decimal | None) -> tuple[str, str, str]:
    """(now, before, saved) formatted for Colombian readers: COP when a USD rate is known."""
    def fmt(minor: int) -> str:
        if deal.currency == "USD" and usd_cop_rate is not None:
            cop = (from_minor(minor, "USD") * usd_cop_rate).quantize(Decimal("1"))
            return f"${cop:,.0f}".replace(",", ".")
        return money(minor, deal.currency).replace(" COP", "")
    before = deal.original_price_minor
    return fmt(deal.price_minor), fmt(before) if before else "", fmt(before - deal.price_minor) if before else ""


def _hook(deal: DealObservation, usd_cop_rate: Decimal | None = None) -> str:
    """First line: product + price + discount, short enough to show before "ver más"."""
    short = _short(display_name(deal))
    now, before, saved = _prices(deal, usd_cop_rate)
    pct = deal.discount_pct
    if deal.evidence == DiscountEvidence.GIFT_CARD_VALUE:
        value = f"US${from_minor(deal.original_price_minor, 'USD'):,.0f}"
        paid = f"US${from_minor(deal.price_minor, 'USD'):,.0f}"
        return _pick(deal, [
            f"🎮 {short}: pagas {paid} y recibes {value} de saldo",
            f"🎮 ¡Saldo extra en PlayStation! {value} por solo {paid}",
        ])
    if deal.lowest_30d:
        return _pick(deal, [
            f"🔻 ¡Precio más bajo en 30 días! {short} a {now}",
            f"🔻 {short} nunca estuvo tan barato este mes: {now}",
        ])
    if deal.evidence != DiscountEvidence.OFFICIAL_ORIGINAL:
        # Discount vs. our own price history: never claim a store "before" price.
        return _pick(deal, [
            f"📉 {short} está {pct}% por debajo de su precio típico: {now}",
            f"👀 {short} hoy a {now}, {pct}% menos de lo usual",
        ])
    if saved and _price_cop(deal, usd_cop_rate) >= BIG_TICKET_COP:
        # "Rule of 100": on expensive items the amount saved feels bigger than the percentage.
        hooks = [
            f"💰 Ahorra {saved} en {short}",
            f"🚨 ¡{saved} menos! {short} ahora a {now}",
            f"💥 {short}: de {before} a {now}",
        ]
    else:
        hooks = [
            f"🔥 -{pct}% en {short}: ahora {now}",
            f"🏷️ {short} con {pct}% de descuento: {now}",
            f"🚨 ¡Bajó {pct}%! {short} a solo {now}",
        ]
    return _pick(deal, hooks)


BIG_TICKET_COP = 400_000  # ~US$100
ENGLISH_SOURCES = {"amazon", "newegg"}  # their titles arrive in English


def _price_cop(deal: DealObservation, usd_cop_rate: Decimal | None) -> int:
    if deal.currency == "USD":
        return int(from_minor(deal.price_minor, "USD") * (usd_cop_rate or Decimal("4000")))
    return deal.price_minor


_QUESTIONS = [
    "¿Lo comprarías a este precio? 🤔",
    "¿Qué te parece este descuento? Cuéntanos 👇",
    "¿Lo necesitas o ya tienes uno? 👀",
    "¿Crees que es buen precio? 💬",
    "¿Qué producto quieres que busquemos en oferta? 🔎",
]

_SOURCE_TAGS = {
    "mercadolibre": "#mercadolibre #cupones",
    "amazon": "#amazon #amazonfinds",
    "exito": "#exito #almacenesexito",
    "alkosto": "#alkosto",
    "ktronix": "#ktronix",
    "falabella": "#falabella #moda",
    "nike": "#nike #nikecolombia",
    "tennis": "#tennis #moda",
    "newegg": "#playstation #psn",
    "adidas": "#adidas #adidascolombia",
}


def trending_hashtags() -> tuple[str, ...]:
    """This week's tags from TRENDING_TAGS (Facebook's Inspiration Hub), offered to Claude to pick from."""
    return tuple(f"#{t.strip().lstrip('#').replace(' ', '')}" for t in os.getenv("TRENDING_TAGS", "").split(",") if t.strip())


def trend_tags(seed: str, count: int = 3) -> str:
    """`count` hashtags from TRENDING_TAGS (comma list, e.g. from Facebook's Inspiration Hub), varied per post."""
    tags = [t.strip().lstrip("#").replace(" ", "") for t in os.getenv("TRENDING_TAGS", "").split(",") if t.strip()]
    if not tags:
        return ""
    start = int(hashlib.md5(f"trend{seed}".encode()).hexdigest(), 16) % len(tags)
    picked = [tags[(start + i) % len(tags)] for i in range(min(count, len(tags)))]
    return " ".join(f"#{t}" for t in picked)


def _pick(deal: DealObservation, options: list[str], salt: str = "") -> str:
    idx = int(hashlib.md5((salt + deal.external_id).encode()).hexdigest(), 16) % len(options)
    return options[idx]


def facebook_copy(
    deal: DealObservation,
    usd_cop_rate: Decimal | None,
    include_affiliate_disclosure: bool = True,
    link_in_comment: bool = False,
) -> str:
    """Hook first (visible before "ver más"), then the key numbers, link, question; small print last."""
    now, before, saved = _prices(deal, usd_cop_rate)
    approx = "≈ " if deal.currency == "USD" and usd_cop_rate is not None else ""
    ai = deal.ai
    from app.events import active_event

    event = active_event()
    hook = _hook(deal, usd_cop_rate)
    if event:  # "🖤 Black Friday: -62% en ..." replaces the hook's own emoji
        hook = f"🖤 {event.name}: {hook.split(' ', 1)[-1]}"
    lines = [hook]
    if ai and ai.hook:
        lines.append(ai.hook)
    lines.append("")
    if before and deal.evidence == DiscountEvidence.GIFT_CARD_VALUE:
        value = f"US${from_minor(deal.original_price_minor, 'USD'):,.2f}"
        extra = f"US${from_minor(deal.original_price_minor - deal.price_minor, 'USD'):,.2f}"
        paid = f"US${from_minor(deal.price_minor, 'USD'):,.2f}"
        lines.append(f"✅ Pagas {paid} ({approx}{now}) y recibes {value} de saldo: {extra} extra")
    elif before and deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
        lines.append(f"✅ Antes {approx}{before} → Ahora {approx}{now} (ahorras {saved})")
    elif before:
        lines.append(f"✅ Precio típico observado {approx}{before} → Hoy {approx}{now} (-{deal.discount_pct}%)")
    else:
        lines.append(f"✅ Precio: {approx}{now}")
    if deal.lowest_30d:
        lines.append("🔻 Es el precio más bajo que hemos visto en los últimos 30 días")
    stars = rating_text(deal)
    if stars:
        lines.append(f"⭐ {stars} en {store_name(deal.source)}")
    # Amazon titles arrive in English: show Claude's Spanish version; store titles stay as they are.
    title = ai.title_es if ai and ai.title_es and deal.source in ENGLISH_SOURCES else deal.title
    lines.append(f"🛍️ {title}")
    lines.extend(f"✔️ {point}" for point in (ai.points if ai else ()))
    if deal.evidence == DiscountEvidence.GIFT_CARD_VALUE:
        lines.append(f"🏪 {store_name(deal.source)}")
        lines.append(f"🎮 {deal.shipping_note}")
    else:
        lines.append(f"🏪 {store_name(deal.source)}" + (f" · 🚚 {deal.shipping_note}" if deal.shipping_note else ""))
    lines.append("👇 Link en el primer comentario" if link_in_comment else f"👉 {deal.url}")
    lines.append("")
    local_time = deal.observed_at.astimezone(ZoneInfo("America/Bogota"))
    small = [f"Verificado el {local_time.strftime('%d/%m/%Y a las %H:%M')}. Precio sujeto a cambios."]
    if deal.currency == "USD":
        dollars = f"US${from_minor(deal.price_minor, 'USD'):,.2f}"
        small.append(f"Precio en tienda: {dollars}. Valor en pesos aproximado; envío e impuestos se confirman al pagar.")
    if deal.affiliate and include_affiliate_disclosure:
        small.append("Como Asociado de Amazon, ganamos con las compras que cumplen los requisitos." if deal.source == "amazon"
                     else "Enlace afiliado: podemos recibir una comisión sin costo adicional para ti.")
    lines.append("ℹ️ " + " ".join(small))
    lines.extend(["", ai.question if ai and ai.question else _pick(deal, _QUESTIONS, salt="q"), ""])
    # Few, specific hashtags reach further on Facebook than a long list: the brand tag plus two.
    store_tag = _SOURCE_TAGS.get(deal.source, "").split(" ")[0]
    if event:  # the event's tag first: it's what people search those days
        second = ai.hashtags[0] if ai and ai.hashtags else store_tag
        tags = f"#OjoAlPrecio {event.hashtags[0]} {second}"
    elif ai and ai.hashtags:
        tags = "#OjoAlPrecio " + " ".join(ai.hashtags)
    else:
        tags = f"#OjoAlPrecio {store_tag} {trend_tags(deal.external_id, 1)}"
    lines.append(" ".join(tags.split()))
    return "\n".join(lines)
