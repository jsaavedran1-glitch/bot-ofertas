from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import os
from zoneinfo import ZoneInfo

from app.models import DealObservation, DiscountEvidence, from_minor, money, store_name


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
    short = _short(deal.title)
    now, before, saved = _prices(deal, usd_cop_rate)
    pct = deal.discount_pct
    if deal.evidence != DiscountEvidence.OFFICIAL_ORIGINAL:
        # Discount vs. our own price history: never claim a store "before" price.
        return _pick(deal, [
            f"📉 {short} está {pct}% por debajo de su precio típico: {now}",
            f"👀 {short} hoy a {now}, {pct}% menos de lo usual",
        ])
    hooks = [
        f"🔥 -{pct}% en {short}: ahora {now}",
        f"🚨 ¡Bajó de precio! {short} de {before} a {now}" if before else f"🚨 ¡Bajó de precio! {short} a {now}",
        f"💥 {short} a solo {now} (antes {before})" if before else f"💥 {short} a solo {now}",
        f"💰 Ahorra {saved} en {short}" if saved else f"💰 {short} a {now}",
        f"🏷️ {short} con {pct}% de descuento: {now}",
    ]
    return _pick(deal, hooks)


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
}


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
    lines = [_hook(deal, usd_cop_rate), ""]
    if before and deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
        lines.append(f"✅ Antes {approx}{before} → Ahora {approx}{now} (ahorras {saved})")
    elif before:
        lines.append(f"✅ Precio típico observado {approx}{before} → Hoy {approx}{now} (-{deal.discount_pct}%)")
    else:
        lines.append(f"✅ Precio: {approx}{now}")
    lines.append(f"🛍️ {deal.title}")
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
    lines.extend(["", _pick(deal, _QUESTIONS, salt="q"), ""])
    tags = f"#OjoAlPrecio #ofertas {_SOURCE_TAGS.get(deal.source, '')} {trend_tags(deal.external_id, 2)}"
    lines.append(" ".join(tags.split()))
    return "\n".join(lines)
