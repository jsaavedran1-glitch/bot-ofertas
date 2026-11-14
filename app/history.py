"""'¿Descuento real?': the bot's own price history of a deal, as a chart and a verdict.

Stores show an "antes" price, never their price history; the bot has seen the price on every scan. That makes
this format original content (what Meta's recommendations favor) and useful around Black Friday, when "antes"
prices are often raised days before. Only facts from our observations: no forecasts, no claims about intent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageOps

from app.copywriter import trend_tags
from app.image_renderer import (GREEN, MUTED, NAVY, PANEL, POST_CANVAS, RED, WHITE, YELLOW, MissingProductImage,
                                OfferImageRenderer, gradient, photo_variant)
from app.models import DealObservation, DiscountEvidence, display_name, money, store_name

BOGOTA = ZoneInfo("America/Bogota")
DAYS = 30
MIN_DAYS, MIN_SPAN = 7, 7  # observed on 7+ different days spread over a week or more, or the chart says too little
REAL_BELOW_USUAL = 0.90  # "real": the lowest we've seen and 10%+ under the usual price
GOOD_BELOW_USUAL = 0.90  # "good": 10%+ under the usual price
INFLATED_ABOVE_SEEN = 1.05  # the store's "antes" is 5%+ above anything we saw
INFLATED_NEAR_USUAL = 0.97  # ... while today's price is about the usual one


@dataclass(frozen=True)
class PriceHistory:
    points: tuple[tuple[date, int], ...]  # lowest price seen each day, oldest first, today last
    usual: int  # median of the daily prices before today
    low: int  # lowest before today
    high: int  # highest before today

    @property
    def first(self) -> date:
        return self.points[0][0]


@dataclass(frozen=True)
class Verdict:
    kind: str  # "real" | "bueno" | "inflado"
    title: str
    detail: str


def build_history(rows: list[tuple[datetime, int]], deal: DealObservation, today: date | None = None) -> PriceHistory | None:
    """Daily lowest prices from (observed_at, price) rows plus today's price; None when there's too little data."""
    today = today or deal.observed_at.astimezone(BOGOTA).date()
    daily: dict[date, int] = {}
    for when, price in rows:
        day = when.astimezone(BOGOTA).date()
        if today - timedelta(days=DAYS) <= day < today:
            daily[day] = min(price, daily.get(day, price))
    if len(daily) < MIN_DAYS or (today - min(daily)).days < MIN_SPAN:
        return None
    before = [daily[d] for d in sorted(daily)]
    points = tuple(sorted(daily.items())) + ((today, deal.price_minor),)
    return PriceHistory(points=points, usual=int(median(before)), low=min(before), high=max(before))


def judge(deal: DealObservation, history: PriceHistory) -> Verdict | None:
    """What our data says about today's price; None when it's an ordinary price (no post)."""
    now, cur = deal.price_minor, deal.currency
    usual, days = money(history.usual, cur), (history.points[-1][0] - history.first).days
    claimed = deal.original_price_minor if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL else None
    if claimed and claimed > history.high * INFLATED_ABOVE_SEEN and now >= history.usual * INFLATED_NEAR_USUAL:
        return Verdict("inflado", "DESCUENTO INFLADO",
                       f"El 'antes' de {money(claimed, cur)} no lo vimos en {days} días; lo habitual fue {usual}.")
    if now <= history.low and now <= history.usual * REAL_BELOW_USUAL:
        return Verdict("real", "DESCUENTO REAL",
                       f"Es el precio más bajo que hemos visto en {days} días; lo habitual fue {usual}.")
    if now <= history.usual * GOOD_BELOW_USUAL:
        pct = round(100 * (1 - now / history.usual))
        return Verdict("bueno", "BUEN PRECIO",
                       f"Está {pct}% por debajo de su precio habitual ({usual}); el mínimo fue {money(history.low, cur)}.")
    return None


def history_caption(deal: DealObservation, history: PriceHistory, verdict: Verdict, link_in_comment: bool,
                    affiliate_disclosure: bool = True) -> str:
    cur, name, store = deal.currency, display_name(deal), store_name(deal.source)
    emoji = {"real": "✅", "bueno": "👍", "inflado": "⚠️"}[verdict.kind]
    lines = [
        f"📉 ¿Es real el descuento de {name} en {store}?",
        "",
        f"Seguimos su precio desde el {history.first:%d/%m}:",
        f"• Precio habitual: {money(history.usual, cur)}",
        f"• Mínimo que vimos: {money(history.low, cur)}",
        f"• Hoy: {money(deal.price_minor, cur)}",
    ]
    if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL and deal.original_price_minor:
        lines.append(f"• 'Antes' según la tienda: {money(deal.original_price_minor, cur)}")
    lines += ["", f"{emoji} {verdict.title.capitalize()}: {verdict.detail}", ""]
    lines.append("👇 Link en el primer comentario" if link_in_comment else f"👉 {deal.url}")
    checked = deal.observed_at.astimezone(BOGOTA)
    small = f"ℹ️ Datos de nuestro seguimiento de precios; verificado el {checked:%d/%m a las %H:%M}, puede cambiar."
    if affiliate_disclosure and deal.affiliate:
        small += " Enlace de afiliado: podemos recibir una comisión sin costo para ti."
    lines += ["", small, "", "¿Revisas el historial de precios antes de comprar en promociones? 🤔", ""]
    lines.append(" ".join(f"#OjoAlPrecio #HistorialDePrecios {trend_tags('hist' + deal.external_id, 2)}".split()))
    return "\n".join(lines)


def _short(value: int, currency: str) -> str:
    """'$1,2 M' / '$849 mil' for chart labels."""
    if currency != "COP":
        return money(value, currency)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f} M".replace(".", ",").replace(",0 M", " M")
    return f"${round(value / 1000)} mil"


class HistoryRenderer(OfferImageRenderer):
    """4:5 card: product, the 30-day price line with the store's 'antes' and our minimum, and the verdict."""

    COLORS = {"real": GREEN, "bueno": "#4DA3FF", "inflado": "#FFA51F"}
    EMOJI = {"real": "✅", "bueno": "👍", "inflado": "⚠️"}

    def render_history(self, deal: DealObservation, history: PriceHistory, verdict: Verdict, name: str,
                       product_image_bytes: bytes | None = None) -> Path:
        from app.quality import photo_issue
        from app.store_colors import store_color, text_on

        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        brand = store_color(deal.source, deal.url, self.session)
        gradient(draw, POST_CANVAS, photo_variant(deal.external_id)[0], brand)
        cur = deal.currency

        # Header: store chip + the question
        store = store_name(deal.source).upper()
        chip_font = self._fit_font(draw, store, 360, 38, 24)
        chip_w = int(draw.textlength(store, font=chip_font)) + 60
        draw.rounded_rectangle((48, 44, 48 + chip_w, 116), radius=36, fill=brand or WHITE)
        draw.text((48 + chip_w // 2, 80), store, anchor="mm", font=chip_font, fill=text_on(brand, NAVY, WHITE))
        draw.text((48 + chip_w + 26, 80), "¿DESCUENTO REAL?", anchor="lm",
                  font=self._fit_font(draw, "¿DESCUENTO REAL?", W - chip_w - 130, 46, 30, weight="Black"), fill=YELLOW)

        # Product: photo tile + name + today's price
        product = self._load_product_image(product_image_bytes or self._download(deal.image_url))
        if product is None:
            raise MissingProductImage("La oferta no tiene foto del producto; no se publica el historial.")
        tile = (48, 150, 318, 420)
        if issue := photo_issue(product.size, (tile[2] - tile[0] - 30, tile[3] - tile[1] - 30)):
            raise MissingProductImage(f"{issue[0].upper()}{issue[1:]}; no se publica el historial.")
        draw.rounded_rectangle(tile, radius=34, fill=WHITE)
        photo = ImageOps.contain(product, (tile[2] - tile[0] - 30, tile[3] - tile[1] - 30), Image.Resampling.LANCZOS)
        canvas.paste(photo, ((tile[0] + tile[2] - photo.width) // 2, (tile[1] + tile[3] - photo.height) // 2),
                     photo if photo.mode == "RGBA" else None)
        nfont, nlines = self._fit_lines(draw, display_name(deal), W - 360 - 48, max_lines=3, start=44, minimum=30,
                                        weight="Bold")
        y = 162
        for line in nlines:
            draw.text((350, y), line, font=nfont, fill=WHITE)
            y += int(nfont.size * 1.15)
        draw.text((350, 408), "HOY", anchor="ls", font=self._font(30, weight="Black"), fill=MUTED)
        price = money(deal.price_minor, cur).replace(" COP", "")
        draw.text((440, 412), price, anchor="ls", font=self._fit_font(draw, price, W - 440 - 48, 76, 44, weight="Black"),
                  fill=YELLOW)

        # Chart
        box = (48, 452, W - 48, 950)
        draw.rounded_rectangle(box, radius=36, fill=PANEL)
        draw.text((box[0] + 34, box[1] + 42), "Así se movió el precio", anchor="lm", font=self._font(32, weight="Black"),
                  fill=WHITE)
        days = (history.points[-1][0] - history.first).days
        draw.text((box[2] - 34, box[1] + 42), f"últimos {days} días", anchor="rm", font=self._font(26), fill=MUTED)
        self._chart(canvas, draw, deal, history, (box[0] + 150, box[1] + 100, box[2] - 40, box[3] - 70))

        # Stat tiles
        stats = (("HABITUAL", history.usual, WHITE), ("MÍNIMO", history.low, GREEN), ("HOY", deal.price_minor, YELLOW))
        tw = (W - 96 - 2 * 20) // 3
        for i, (label, value, color) in enumerate(stats):
            x = 48 + i * (tw + 20)
            draw.rounded_rectangle((x, 974, x + tw, 1080), radius=26, fill=PANEL)
            draw.text((x + tw // 2, 1004), label, anchor="mm", font=self._font(24, weight="Black"), fill=MUTED)
            text = money(value, cur).replace(" COP", "")
            draw.text((x + tw // 2, 1048), text, anchor="mm", font=self._fit_font(draw, text, tw - 30, 40, 24, weight="Black"),
                      fill=color)

        # Verdict
        color = self.COLORS[verdict.kind]
        draw.rounded_rectangle((48, 1100, W - 48, 1212), radius=34, fill=color)
        self._paste_emoji(canvas, (78, 1130), self.EMOJI[verdict.kind], 52)
        draw.text((150, 1134), verdict.title, anchor="lm", font=self._font(44, weight="Black"), fill=NAVY)
        dfont, dlines = self._fit_lines(draw, verdict.detail.replace(" COP", ""), W - 96 - 130, max_lines=1, start=24, minimum=17,
                                        weight="Bold")
        draw.text((150, 1180), dlines[0], anchor="lm", font=dfont, fill=NAVY)
        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output

    def _chart(self, canvas, draw, deal: DealObservation, history: PriceHistory, area) -> None:
        x0, y0, x1, y1 = area
        cur = deal.currency
        claimed = deal.original_price_minor if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL else None
        values = [p for _, p in history.points] + ([claimed] if claimed else [])
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * 0.12, hi * 0.02)
        lo, hi = lo - 1.6 * pad, hi + pad  # room under the minimum line for its label
        start, span = history.first, max(1, (history.points[-1][0] - history.first).days)

        def X(day: date) -> float:
            return x0 + (x1 - x0) * (day - start).days / span

        def Y(value: float) -> float:
            return y1 - (y1 - y0) * (value - lo) / (hi - lo)

        label_font = self._font(22, weight="SemiBold")
        for i in range(4):  # grid with price labels
            value = lo + (hi - lo) * i / 3
            y = Y(value)
            draw.line((x0, y, x1, y), fill="#24406A", width=2)
            draw.text((x0 - 16, y), _short(int(value), cur), anchor="rm", font=label_font, fill=MUTED)
        for day, anchor in ((start, "lm"), (history.points[-1][0], "rm")):
            text = "Hoy" if day == history.points[-1][0] else f"{day:%d/%m}"
            draw.text((X(day), y1 + 34), text, anchor=anchor, font=label_font, fill=MUTED)

        def dashed(y: float, color: str, label: str, below: bool = False) -> None:
            for x in range(int(x0), int(x1), 26):
                draw.line((x, y, min(x + 14, x1), y), fill=color, width=4)
            font = self._font(22, weight="Black")
            w = int(draw.textlength(label, font=font)) + 24
            top = y + 8 if below else y - 34
            draw.rounded_rectangle((x0 + 8, top, x0 + 8 + w, top + 28), radius=12, fill=color)
            draw.text((x0 + 20, top + 14), label, anchor="lm", font=font, fill=NAVY)

        if claimed:
            dashed(Y(claimed), RED, f"'Antes' según la tienda {_short(claimed, cur)}")
        # Every earlier price sits on or above the minimum, so its label goes under the line, clear of it.
        dashed(Y(history.low), GREEN, f"Mínimo {_short(history.low, cur)}", below=True)

        # Step line: the price holds until the next observation
        line = []
        for (day, price), nxt in zip(history.points, history.points[1:] + ((None, None),)):
            line.append((X(day), Y(price)))
            if nxt[0] is not None:
                line.append((X(nxt[0]), Y(price)))
        draw.line(line, fill=YELLOW, width=7, joint="curve")
        tx, ty = X(history.points[-1][0]), Y(deal.price_minor)
        draw.ellipse((tx - 16, ty - 16, tx + 16, ty + 16), fill=YELLOW, outline=NAVY, width=5)


__all__ = ["HistoryRenderer", "PriceHistory", "Verdict", "build_history", "history_caption", "judge"]
