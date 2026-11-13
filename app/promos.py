"""Daily "promo del día": best 3-4 deals of one brand at one Colombian store, in a single post."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageOps

from app.image_renderer import MUTED, NAVY, POST_CANVAS, RED, WHITE, YELLOW, OfferImageRenderer
from app.copywriter import trend_tags
from app.models import DealObservation, display_title, money, store_name

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
        " ".join(f"#OjoAlPrecio #promo #descuentos #{promo.store} #{promo.brand.lower()} #Colombia {trend_tags(promo.key)}".split()),
    ]
    return "\n".join(lines)


class PromoRenderer(OfferImageRenderer):
    """4:5 "promo del día": brand headline, discount burst and a 2x2 grid of product cards."""

    def render_promo(self, promo: Promo, name: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(8 + int(10 * r), 24 + int(22 * r), 48 + int(40 * r)))

        store = store_name(promo.store).upper()
        chip_font = self._fit_font(draw, store, 420, 40, 26)
        chip_w = int(draw.textlength(store, font=chip_font)) + 64
        draw.rounded_rectangle((48, 44, 48 + chip_w, 120), radius=38, fill=WHITE)
        draw.text((48 + chip_w // 2, 82), store, anchor="mm", font=chip_font, fill=NAVY)
        self._paste_emoji(canvas, (48 + chip_w + 22, 60), "🔥", 44)
        draw.text((48 + chip_w + 76, 82), "PROMO DEL DÍA", anchor="lm", font=self._font(34, weight="Black"), fill=YELLOW)
        brand = promo.brand.upper()
        draw.text((52, 200), brand, anchor="lm", font=self._fit_font(draw, brand, 680, 116, 56, weight="Black"), fill=WHITE)
        draw.text((56, 288), "Lo mejor de la marca hoy", anchor="lm", font=self._font(28, weight="SemiBold"), fill=MUTED)
        self._burst(draw, (W - 170, 210), 122, f"-{promo.max_pct}%")
        draw.text((W - 170, 70), "HASTA", anchor="mm", font=self._font(30, weight="Black"), fill=YELLOW)

        cw, ch, gap, top = 480, 424, 24, 330
        slots = [(48, top), (48 + cw + gap, top), (48, top + ch + gap), (48 + cw + gap, top + ch + gap)]
        if len(promo.deals) == 3:
            slots[2] = ((W - cw) // 2, top + ch + gap)
        for (x, y), deal in zip(slots, promo.deals):
            self._card(canvas, draw, deal, x, y, cw, ch)

        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output

    def _card(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, deal: DealObservation, x: int, y: int, w: int, h: int) -> None:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=32, fill=WHITE)
        photo = self._load_product_image(self._download(deal.image_url))
        if photo is not None:
            photo = ImageOps.contain(photo, (w - 60, 196), Image.Resampling.LANCZOS)
            canvas.paste(photo, (x + (w - photo.width) // 2, y + 26 + (196 - photo.height) // 2), photo)
        pct = f"-{deal.discount_pct}%"
        pfont = self._font(30, weight="Black")
        pw = int(draw.textlength(pct, font=pfont)) + 34
        draw.rounded_rectangle((x + 16, y + 16, x + 16 + pw, y + 64), radius=24, fill=RED)
        draw.text((x + 16 + pw // 2, y + 40), pct, anchor="mm", font=pfont, fill=WHITE)
        font, lines = self._fit_lines(draw, display_title(deal.title), w - 48, max_lines=2, start=26, minimum=20)
        ty = y + 236
        for line in lines:
            draw.text((x + 24, ty), line, font=font, fill=NAVY)
            ty += font.size + 6
        before = money(deal.original_price_minor, "COP").replace(" COP", "")
        bfont = self._font(26, weight="SemiBold")
        draw.text((x + 24, y + h - 104), "Antes " + before, font=bfont, fill="#5B6B80")
        bx = x + 24 + draw.textlength("Antes ", font=bfont)
        draw.line((bx, y + h - 89, bx + draw.textlength(before, font=bfont), y + h - 89), fill=RED, width=3)
        now = money(deal.price_minor, "COP").replace(" COP", "")
        draw.text((x + 22, y + h - 68), now, font=self._fit_font(draw, now, w - 48, 52, 30, weight="Black"), fill=NAVY)
