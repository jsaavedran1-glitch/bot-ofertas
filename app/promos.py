"""Daily "promo del día": best 3-4 deals of one brand at one Colombian store, in a single post."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageOps

from app.image_renderer import CANVAS, OfferImageRenderer
from app.models import DealObservation, money, store_name

# Brand -> search terms. The brand is rotated by day so consecutive days feature different brands.
BRANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Electrolux", ("nevera", "lavadora")),
    ("Samsung", ("televisor", "nevera")),
    ("LG", ("televisor", "nevera", "lavadora")),
    ("PlayStation", ("consola",)),
    ("Mabe", ("nevera", "lavadora")),
    ("Kalley", ("televisor",)),
    ("Haceb", ("nevera", "lavadora")),
    ("Xbox", ("consola",)),
)
PROMO_STORES = ("exito", "alkosto", "ktronix")
MIN_ITEMS, MAX_ITEMS = 3, 4
MIN_DISCOUNT_PCT, MIN_SAVINGS_COP = 20, 20_000
REPEAT_DAYS = 7
NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
COLORS = {"negro", "negra", "blanco", "blanca", "gris", "plata", "plateado", "azul", "rojo", "inox", "grafito", "titanio", "silver", "black", "white"}


def _variant_key(title: str) -> str:
    """Title without color words and model suffix letters, so color variants collapse to one card."""
    words = [w for w in title.lower().split() if w not in COLORS]
    return " ".join(w.rstrip("abcdefghijklmnopqrstuvwxyz") if any(c.isdigit() for c in w) else w for w in words)


@dataclass(frozen=True)
class Promo:
    store: str
    brand: str
    deals: tuple[DealObservation, ...]

    @property
    def key(self) -> str:
        return f"{self.store}:{self.brand.lower()}"

    @property
    def max_pct(self) -> int:
        return max(d.discount_pct for d in self.deals)


def _brand_deals(source, brand: str, terms: tuple[str, ...]) -> list[DealObservation]:
    seen: dict[str, DealObservation] = {}
    for term in terms:
        try:
            found = source.search(f"{term} {brand}")
        except Exception:  # one failing store/term must not stop the promo
            continue
        for d in found:
            if (
                brand.lower() in d.title.lower()
                and d.discount_pct >= MIN_DISCOUNT_PCT
                and d.original_price_minor - d.price_minor >= MIN_SAVINGS_COP
            ):
                seen.setdefault(_variant_key(d.title), d)
    return sorted(seen.values(), key=lambda d: -d.discount_pct)


def find_promo(sources: dict, recently_posted, day_index: int) -> Promo | None:
    """First brand (rotated by day) with >= MIN_ITEMS deals at some store not featured in REPEAT_DAYS."""
    for offset in range(len(BRANDS)):
        brand, terms = BRANDS[(day_index + offset) % len(BRANDS)]
        options = []
        for store in PROMO_STORES:
            if store not in sources or recently_posted(f"{store}:{brand.lower()}", REPEAT_DAYS):
                continue
            deals = _brand_deals(sources[store], brand, terms)
            if len(deals) >= MIN_ITEMS:
                options.append(Promo(store, brand, tuple(deals[:MAX_ITEMS])))
        if options:
            return max(options, key=lambda p: (len(p.deals), sum(d.discount_pct for d in p.deals)))
    return None


def promo_links(promo: Promo) -> str:
    return "\n".join(f"{n} {d.url}" for n, d in zip(NUMBERS, promo.deals))


def promo_copy(promo: Promo, now: datetime | None = None, link_in_comment: bool = False) -> str:
    local = (now or datetime.now(ZoneInfo("America/Bogota"))).astimezone(ZoneInfo("America/Bogota"))
    store = store_name(promo.store)
    lines = [f"🔥 Promo del día en {store}: {promo.brand} hasta -{promo.max_pct}%", ""]
    for n, d in zip(NUMBERS, promo.deals):
        lines += [
            f"{n} {d.title}",
            f"   💥 {money(d.price_minor, 'COP')} (antes {money(d.original_price_minor, 'COP')}, -{d.discount_pct}%)",
        ]
        lines += [""] if link_in_comment else [f"   👉 {d.url}", ""]
    if link_in_comment:
        lines += ["👇 Links de cada producto en el primer comentario", ""]
    lines += [
        f"Verificado el {local:%d/%m/%Y a las %H:%M}. Precios sujetos a cambios y disponibilidad en {store}.",
        "",
        "¿Cuál te llevarías? Cuéntanos 👇",
        "",
        f"#OjoAlPrecio #promo #descuentos #{promo.store} #{promo.brand.lower()} #Colombia",
    ]
    return "\n".join(lines)


class PromoRenderer(OfferImageRenderer):
    ORANGE, WHITE, MUTED, NAVY, PANEL = "#FFB11B", "#FFFFFF", "#A9B5C5", "#071426", "#0E223A"

    def render_promo(self, promo: Promo, name: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        canvas = Image.new("RGB", CANVAS, self.NAVY)
        draw = ImageDraw.Draw(canvas)
        for y in range(CANVAS[1]):
            r = y / CANVAS[1]
            draw.line((0, y, CANVAS[0], y), fill=(7 + int(8 * r), 20 + int(18 * r), 38 + int(26 * r)))

        draw.rounded_rectangle((42, 30, 1038, 118), radius=26, fill=self.PANEL)
        store = store_name(promo.store).upper()
        draw.text((540, 74), store, anchor="mm", font=self._fit_font(draw, store, 900, 44, 28), fill=self.ORANGE)
        title = f"PROMO DEL DÍA: {promo.brand.upper()}"
        draw.text((540, 160), title, anchor="mm", font=self._fit_font(draw, title, 900, 50, 30), fill=self.WHITE)
        sub = f"¡Hasta -{promo.max_pct}% de descuento!"
        draw.text((540, 214), sub, anchor="mm", font=self._font(36, bold=True), fill=self.ORANGE)
        self._paste_emoji(canvas, (60, 136), "🔥", 48)
        self._paste_emoji(canvas, (972, 136), "🔥", 48)

        slots = [(50, 255), (550, 255), (50, 575), (550, 575)]
        if len(promo.deals) == 3:
            slots[2] = (300, 575)
        for (x, y), deal in zip(slots, promo.deals):
            self._card(canvas, draw, deal, x, y)

        draw.rounded_rectangle((42, 910, 1038, 1040), radius=28, fill=self.PANEL)
        if self._logo_footer:
            lw, lh = self._logo_footer.size
            canvas.paste(self._logo_footer, (72, 975 - lh // 2), self._logo_footer)
        draw.text((1008, 950), "OJO AL PRECIO", anchor="rm", font=self._font(28, bold=True), fill=self.WHITE)
        draw.text((1008, 995), "Síguenos y activa las notificaciones", anchor="rm", font=self._font(24, bold=True), fill=self.MUTED)

        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output

    def _card(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, deal: DealObservation, x: int, y: int) -> None:
        w, h = 480, 300
        draw.rounded_rectangle((x, y, x + w, y + h), radius=26, fill=self.WHITE)
        photo = self._load_product_image(self._download(deal.image_url))
        if photo is not None:
            photo = ImageOps.contain(photo, (190, 190), Image.Resampling.LANCZOS)
            canvas.paste(photo, (x + 20 + (190 - photo.width) // 2, y + 55 + (190 - photo.height) // 2), photo)
        font, lines = self._fit_lines(draw, deal.title, 240, max_lines=3, start=22, minimum=16)
        ty = y + 28
        for line in lines:
            draw.text((x + 222, ty), line, font=font, fill=self.NAVY)
            ty += font.size + 4
        before = money(deal.original_price_minor, "COP")
        bfont = self._fit_font(draw, before, 240, 22, 16)
        draw.text((x + 222, y + 190), before, font=bfont, fill="#64748B")
        bw = draw.textlength(before, font=bfont)
        draw.line((x + 222, y + 202, x + 222 + bw, y + 202), fill="#FF6B6B", width=3)
        now = money(deal.price_minor, "COP")
        draw.text((x + 222, y + 222), now, font=self._fit_font(draw, now, 245, 34, 22), fill="#E08E00")
        badge = f"-{deal.discount_pct}%"
        bfont2 = self._font(28, bold=True)
        bwid = int(draw.textlength(badge, font=bfont2)) + 30
        draw.rounded_rectangle((x + 12, y + 12, x + 12 + bwid, y + 56), radius=20, fill=self.ORANGE)
        draw.text((x + 12 + bwid // 2, y + 34), badge, anchor="mm", font=bfont2, fill=self.NAVY)
