from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
from zoneinfo import ZoneInfo

from app.models import DealObservation, DiscountEvidence, from_minor, money


def _hook(deal: DealObservation) -> str:
    title = deal.title
    price = money(deal.price_minor, deal.currency)
    orig = money(deal.original_price_minor, deal.currency) if deal.original_price_minor else ""
    pct = deal.discount_pct
    saved = money(deal.original_price_minor - deal.price_minor, deal.currency) if deal.original_price_minor else ""
    hooks = [
        f"🔥 ¡Bajó de precio! {title} ahora por {price}",
        f"🚨 Alerta de oferta: {title} con {pct}% menos",
        f"💥 De {orig} a solo {price}" if orig else f"💥 {title} ahora por {price}",
        f"💰 Ahorra {saved} en este {title}" if saved else f"💰 {title} a precio increíble",
        f"🏷️ Oferta verificada: {title} con {pct}% de descuento",
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
    "woot": "#woot #amazon",
}


def _pick(deal: DealObservation, options: list[str], salt: str = "") -> str:
    idx = int(hashlib.md5((salt + deal.external_id).encode()).hexdigest(), 16) % len(options)
    return options[idx]


def facebook_copy(
    deal: DealObservation,
    usd_cop_rate: Decimal | None,
    include_affiliate_disclosure: bool = True,
    link_in_comment: bool = False,
) -> str:
    source = deal.source.replace("_", " ").title()
    lines = [_hook(deal), "", f"🛍️ {deal.title}", f"🏪 {source}", ""]
    if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL and deal.original_price_minor:
        lines.extend(
            [
                f"Antes: {money(deal.original_price_minor, deal.currency)}",
                f"Ahora: 💥 {money(deal.price_minor, deal.currency)}",
                f"📉 {deal.discount_pct}% de descuento comprobado",
            ]
        )
    elif deal.evidence == DiscountEvidence.OWN_HISTORY and deal.original_price_minor:
        lines.extend(
            [
                f"Precio típico observado: {money(deal.original_price_minor, deal.currency)}",
                f"Precio actual: 💥 {money(deal.price_minor, deal.currency)}",
                f"📉 {deal.discount_pct}% por debajo del precio típico observado",
            ]
        )
    else:
        lines.append(f"Precio actual: {money(deal.price_minor, deal.currency)}")
    if deal.currency == "USD" and usd_cop_rate is not None:
        cop = (from_minor(deal.price_minor, "USD") * usd_cop_rate).quantize(Decimal("1"))
        lines.append(f"≈ ${cop:,.0f} COP".replace(",", "."))
    if deal.shipping_note:
        lines.append(f"🚚 {deal.shipping_note}")
    link_line = "👇 Link de la oferta en el primer comentario" if link_in_comment else f"👉 {deal.url}"
    lines.extend(["", link_line, ""])
    local_time = deal.observed_at.astimezone(ZoneInfo("America/Bogota"))
    lines.append(f"Verificado el {local_time.strftime('%d/%m/%Y a las %H:%M')}. Precio sujeto a cambios.")
    if deal.currency == "USD":
        lines.append("El valor en COP es aproximado; envío e impuestos se confirman en la tienda.")
    if deal.affiliate and include_affiliate_disclosure:
        if deal.source == "amazon":
            lines.append("Como Asociado de Amazon, ganamos con las compras que cumplen los requisitos.")
        else:
            lines.append("Enlace afiliado: podemos recibir una comisión sin costo adicional para ti.")
    lines.extend(["", _pick(deal, _QUESTIONS, salt="q"), ""])
    source_tags = _SOURCE_TAGS.get(deal.source, "")
    lines.append(f"#OjoAlPrecio #descuentos #ofertas {source_tags} #tecnologia #Colombia".replace("  ", " "))
    return "\n".join(lines)
