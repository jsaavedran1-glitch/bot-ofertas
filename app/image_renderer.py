from __future__ import annotations

import hashlib

from decimal import Decimal
from functools import lru_cache
from io import BytesIO
import ipaddress
from pathlib import Path
import socket
from urllib.parse import urljoin, urlparse
import warnings
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.models import DealObservation, DiscountEvidence, display_name, from_minor, money, rating_text, store_name, to_minor


CANVAS = (1080, 1080)  # square: promo grid
POST_CANVAS = (1080, 1350)  # 4:5 vertical: fills ~20% more of the phone screen in the feed
NAVY, PANEL, WHITE, MUTED = "#08182F", "#0E2647", "#FFFFFF", "#AFC0D6"
YELLOW, RED, GREEN = "#FFD21F", "#FF3B3B", "#3DDC84"
# Background gradients (top, bottom) that rotate per product so the feed doesn't look like one template
# (Meta's support flagged "repetitive format"). Yellow, red and white accents read well on all of them.
THEMES = (
    ((8, 24, 48), (18, 46, 88)),     # navy (the brand's)
    ((34, 10, 56), (86, 28, 110)),   # purple
    ((4, 36, 44), (8, 92, 94)),      # teal
    ((26, 10, 14), (92, 22, 34)),    # wine
)


def gradient(draw, size: tuple[int, int], theme: int) -> None:
    top, bottom = THEMES[theme % len(THEMES)]
    w, h = size
    for y in range(h):
        r = y / h
        draw.line((0, y, w, y), fill=tuple(int(a + (b - a) * r) for a, b in zip(top, bottom)))


def photo_variant(external_id: str) -> tuple[int, bool, int]:
    """(theme, discount burst on the left, card corner radius) for a product, stable across renders."""
    h = int(hashlib.md5(f"look{external_id}".encode()).hexdigest(), 16)
    return h % len(THEMES), bool((h // len(THEMES)) % 2), (44, 18, 70)[(h // (2 * len(THEMES))) % 3]
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
Image.MAX_IMAGE_PIXELS = 25_000_000

_LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.png"
_ICON_PATH = Path(__file__).parent.parent / "assets" / "icon.png"
# Montserrat (SIL Open Font License, assets/fonts/OFL.txt): one variable file, every weight.
_BRAND_FONT = Path(__file__).parent.parent / "assets" / "fonts" / "Montserrat-Variable.ttf"


@lru_cache(maxsize=512)
def brand_font(size: int, weight: str = "ExtraBold") -> ImageFont.FreeTypeFont:
    """Montserrat at a named weight (SemiBold, Bold, ExtraBold, Black); DejaVu/Arial if it is missing."""
    try:
        font = ImageFont.truetype(str(_BRAND_FONT), size)
        font.set_variation_by_name(weight)
        return font
    except (OSError, ValueError):
        bold = weight not in ("Regular", "Medium", "SemiBold")
        for name in (["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)


def trim_product(image: Image.Image, tolerance: int = 14, pad: float = 0.035) -> Image.Image:
    """Crop the empty margin around a product photo and turn its near-white backdrop pure white,
    so the product fills the card and no grey box shows against the white card."""
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    r, g, b = ImageChops.difference(flat, Image.new("RGB", flat.size, (255, 255, 255))).split()
    distance = ImageChops.lighter(ImageChops.lighter(r, g), b)
    bbox = distance.point(lambda v: 255 if v > tolerance else 0).getbbox()
    w, h = flat.size
    corners = [distance.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if bbox is None or max(corners) > tolerance:
        return rgba  # blank, or a full-bleed photo with its own backdrop: leave it alone
    flat = flat.point(lambda v: 255 if v >= 255 - tolerance else v)
    margin = int(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * pad)
    box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin), min(w, bbox[2] + margin), min(h, bbox[3] + margin))
    return flat.crop(box).convert("RGBA")


class MissingProductImage(ValueError):
    pass


# Color emoji fonts only ship fixed bitmap sizes: Apple 160px (macOS), Noto 109px (Linux/GitHub Actions).
_EMOJI_FONTS = (
    ("/System/Library/Fonts/Apple Color Emoji.ttc", 160),
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109),
)


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    for path, size in _EMOJI_FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


class OfferImageRenderer:
    def __init__(self, output_dir: Path, session: requests.Session | None = None) -> None:
        self.output_dir = output_dir
        self.session = session or requests.Session()
        self._logo: Image.Image | None = self._load_logo((260, 70))
        self._logo_footer: Image.Image | None = self._load_logo((420, 96))
        self._icon: Image.Image | None = self._load_icon()

    @staticmethod
    def _load_logo(max_size: tuple[int, int] = (260, 70)) -> "Image.Image | None":
        try:
            img = Image.open(_LOGO_PATH).convert("RGBA")
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            return img
        except (OSError, UnidentifiedImageError):
            return None

    @staticmethod
    def _load_icon() -> "Image.Image | None":
        try:
            return Image.open(_ICON_PATH).convert("RGBA")
        except (OSError, UnidentifiedImageError):
            return None

    def _brand(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, x: int, cy: int, size: int = 60) -> int:
        """Round eye icon + OJO AL PRECIO wordmark; returns the x where it ends."""
        if self._icon:
            icon = self._icon.copy()
            icon.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas.paste(icon, (x, cy - icon.height // 2), icon)
            x += icon.width + 14
        font = self._font(int(size * 0.5), weight="Black")
        draw.text((x, cy), "OJO AL", anchor="lm", font=font, fill=WHITE)
        x += int(draw.textlength("OJO AL ", font=font))
        draw.text((x, cy), "PRECIO", anchor="lm", font=font, fill=YELLOW)
        return x + int(draw.textlength("PRECIO", font=font))

    def render(
        self,
        deal: DealObservation,
        candidate_id: str,
        product_image_bytes: bytes | None = None,
        usd_cop_rate: Decimal | None = None,
        require_image: bool = False,
        tag: str | None = None,
    ) -> Path:
        """Vertical 4:5 deal card: big product photo, discount burst, huge price, savings pill."""
        if tag is None:
            from app.events import event_tag  # "BLACK FRIDAY" on event days, "OFERTA DEL DÍA" otherwise

            tag = event_tag()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        theme, burst_left, radius = photo_variant(deal.external_id)
        burst_left = burst_left and not deal.lowest_30d  # the "lowest in 30 days" ribbon owns the top-left corner
        gradient(draw, POST_CANVAS, theme)

        product = self._load_product_image(product_image_bytes or self._download(deal.image_url))
        if product is None and require_image:
            raise MissingProductImage("La oferta no tiene foto del producto; no se publica.")

        # Header: store chip + tagline
        store = store_name(deal.source).upper()
        chip_font = self._fit_font(draw, store, 420, 40, 26)
        chip_w = int(draw.textlength(store, font=chip_font)) + 64
        draw.rounded_rectangle((48, 44, 48 + chip_w, 120), radius=38, fill=WHITE)
        draw.text((48 + chip_w // 2, 82), store, anchor="mm", font=chip_font, fill=NAVY)
        self._paste_emoji(canvas, (48 + chip_w + 22, 60), "🔥", 44)
        draw.text((48 + chip_w + 76, 82), tag, anchor="lm", font=self._font(34, weight="Black"), fill=YELLOW)

        # Product card
        card = (48, 150, W - 48, 735)
        draw.rounded_rectangle(card, radius=radius, fill=WHITE)
        if product is None:
            self._draw_follow_cta(canvas, draw, (card[0] + 30, card[1] + 30, card[2] - 30, card[3] - 30))
        else:
            # leave the bottom strip of the card for the "verified" stamp
            product = ImageOps.contain(product, (card[2] - card[0] - 120, card[3] - card[1] - 130), Image.Resampling.LANCZOS)
            cx, cy = (card[0] + card[2]) // 2, (card[1] + card[3]) // 2 - 28
            canvas.paste(product, (cx - product.width // 2, cy - product.height // 2), product if product.mode == "RGBA" else None)
        self._stamp(canvas, draw, deal, card)
        self._insight_badges(canvas, draw, deal, card)

        # Discount burst overlapping one of the card's top corners
        self._burst(draw, (160 if burst_left else W - 160, 232), 128, f"-{deal.discount_pct}%")

        # Title
        title_font, title_lines = self._fit_lines(draw, display_name(deal), W - 140, max_lines=2, start=44, minimum=30)
        ty = 758
        for line in title_lines:
            draw.text((W // 2, ty), line, anchor="ma", font=title_font, fill=WHITE)
            ty += title_font.size + 10

        # Gift card promos stay in dollars: "pay US$250, get US$290" only reads right in one currency.
        in_cop = deal.currency == "USD" and bool(usd_cop_rate) and deal.evidence != DiscountEvidence.GIFT_CARD_VALUE

        def cop(minor: int) -> int:
            return to_minor(from_minor(minor, "USD") * usd_cop_rate, "COP") if in_cop else minor

        currency = "COP" if in_cop else deal.currency
        price_y = ty + 8
        gift = deal.evidence == DiscountEvidence.GIFT_CARD_VALUE
        draw.text((W // 2, price_y), "PAGAS" if gift else "AHORA", anchor="ma", font=self._font(30, bold=True), fill=MUTED)
        price = money(cop(deal.price_minor), currency).replace(" COP", "")
        price_font = self._fit_font(draw, price, W - 160, 136, 70, weight="Black")
        draw.text((W // 2, price_y + 36), price, anchor="ma", font=price_font, fill=YELLOW)
        y = price_y + 36 + price_font.size + 14

        if deal.original_price_minor and gift:
            # Gift card promos: the reference is the balance received, in the card's own currency.
            value = f"US${from_minor(deal.original_price_minor, 'USD'):,.0f}"
            line = f"RECIBES {value} EN SALDO"
            draw.text((W // 2, y + 4), line, anchor="ma", font=self._fit_font(draw, line, W - 200, 40, 28), fill=WHITE)
            y += 60
            extra = f"US${from_minor(deal.original_price_minor - deal.price_minor, 'USD'):,.0f}"
            pill = f"GANAS {extra} DE SALDO EXTRA"
            pfont = self._fit_font(draw, pill, W - 200, 40, 28)
            pw = int(draw.textlength(pill, font=pfont)) + 70
            draw.rounded_rectangle((W // 2 - pw // 2, y, W // 2 + pw // 2, y + 62), radius=31, fill=GREEN)
            draw.text((W // 2, y + 31), pill, anchor="mm", font=pfont, fill=NAVY)
            y += 76
            region = "Válida solo en cuentas PSN de EE. UU."
            if usd_cop_rate:
                pesos = money(to_minor(from_minor(deal.price_minor, "USD") * usd_cop_rate, "COP"), "COP").replace(" COP", "")
                region += f" · ≈ {pesos} pesos"
            draw.text((W // 2, y), region, anchor="ma", font=self._fit_font(draw, region, W - 160, 26, 18, weight="Black"), fill=YELLOW)
            y += 36
        elif deal.original_price_minor:
            label = "ANTES " if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL else "PRECIO TÍPICO "
            before = money(cop(deal.original_price_minor), currency).replace(" COP", "")
            bfont = self._font(40, bold=True)
            lw, bw = draw.textlength(label, font=self._font(30, bold=True)), draw.textlength(before, font=bfont)
            x0 = W // 2 - int(lw + bw) // 2
            draw.text((x0, y + 6), label, font=self._font(30, bold=True), fill=MUTED)
            draw.text((x0 + lw, y), before, font=bfont, fill="#D3D9E2")
            if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
                draw.line((x0 + lw, y + 24, x0 + lw + bw, y + 24), fill=RED, width=5)
            y += 60
            saved = money(cop(deal.original_price_minor) - cop(deal.price_minor), currency).replace(" COP", "")
            pill = f"AHORRAS {saved}"
            pfont = self._fit_font(draw, pill, W - 200, 40, 28)
            pw = int(draw.textlength(pill, font=pfont)) + 70
            draw.rounded_rectangle((W // 2 - pw // 2, y, W // 2 + pw // 2, y + 62), radius=31, fill=GREEN)
            draw.text((W // 2, y + 31), pill, anchor="mm", font=pfont, fill=NAVY)
            y += 76
        if in_cop:
            note = f"Precio aprox. en pesos · US${from_minor(deal.price_minor, 'USD'):,.2f}"
            draw.text((W // 2, y), note, anchor="ma", font=self._font(24, bold=True), fill=MUTED)

        self._footer(canvas, draw, W, H)

        output = self.output_dir / f"{candidate_id}.png"
        temporary = self.output_dir / f".{candidate_id}.tmp.png"
        canvas.save(temporary, format="PNG", optimize=True, compress_level=9)
        temporary.replace(output)
        return output

    def render_top_cover(self, deals: list[DealObservation], name: str, usd_cop_rate: Decimal | None = None,
                         when: "datetime | None" = None) -> Path:
        """First slide of the Top 5 carousel: the whole list at a glance, so the feed preview sells the swipe."""
        from datetime import datetime
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(8 + int(10 * r), 24 + int(22 * r), 48 + int(40 * r)))
        local = (when or datetime.now(ZoneInfo("America/Bogota"))).astimezone(ZoneInfo("America/Bogota"))
        days = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
        months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                  "octubre", "noviembre", "diciembre")
        self._paste_emoji(canvas, (48, 52), "🔥", 92)
        draw.text((156, 98), f"TOP {len(deals)}", anchor="lm", font=self._font(112, weight="Black"), fill=YELLOW)
        draw.text((52, 196), "OFERTAS DE HOY", anchor="lm", font=self._font(64, weight="Black"), fill=WHITE)
        draw.text((54, 252), f"{days[local.weekday()].capitalize()} {local.day} de {months[local.month - 1]}",
                  anchor="lm", font=self._font(30, weight="SemiBold"), fill=MUTED)
        hint = "Desliza"
        hfont = self._font(30, weight="Black")
        draw.rounded_rectangle((W - 48 - int(draw.textlength(hint, font=hfont)) - 110, 222, W - 48, 282), radius=30, fill=YELLOW)
        draw.text((W - 124, 252), hint, anchor="rm", font=hfont, fill=NAVY)
        self._paste_emoji(canvas, (W - 112, 232), "👉", 40)

        in_cop = bool(usd_cop_rate)
        row_h, gap, y = 166, 14, 312
        for n, deal in enumerate(deals[:5], start=1):
            draw.rounded_rectangle((48, y, W - 48, y + row_h), radius=30, fill=WHITE)
            draw.ellipse((68, y + row_h // 2 - 34, 136, y + row_h // 2 + 34), fill=YELLOW)
            draw.text((102, y + row_h // 2), str(n), anchor="mm", font=self._font(40, weight="Black"), fill=NAVY)
            photo = self._load_product_image(self._download(deal.image_url))
            if photo is not None:
                photo = ImageOps.contain(photo, (150, row_h - 28), Image.Resampling.LANCZOS)
                canvas.paste(photo, (156 + (150 - photo.width) // 2, y + (row_h - photo.height) // 2), photo)
            pct = f"-{deal.discount_pct}%"
            pfont = self._font(34, weight="Black")
            pw = int(draw.textlength(pct, font=pfont)) + 36
            draw.rounded_rectangle((W - 72 - pw, y + row_h // 2 - 30, W - 72, y + row_h // 2 + 30), radius=30, fill=RED)
            draw.text((W - 72 - pw // 2, y + row_h // 2), pct, anchor="mm", font=pfont, fill=WHITE)
            text_w = W - 72 - pw - 24 - 326
            tfont, lines = self._fit_lines(draw, display_name(deal), text_w, max_lines=1, start=28, minimum=22)
            draw.text((326, y + 30), lines[0], font=tfont, fill=NAVY)
            draw.text((326, y + 70), store_name(deal.source), font=self._font(22, weight="SemiBold"), fill="#5B6B80")
            minor = deal.price_minor
            if deal.currency == "USD" and in_cop:
                minor, currency = to_minor(from_minor(minor, "USD") * usd_cop_rate, "COP"), "COP"
            else:
                currency = deal.currency
            price = money(minor, currency).replace(" COP", "")
            draw.text((324, y + 100), price, font=self._fit_font(draw, price, text_w, 44, 28, weight="Black"), fill=NAVY)
            y += row_h + gap

        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output

    def _footer(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, W: int, H: int) -> None:
        draw.rounded_rectangle((48, H - 118, W - 48, H - 40), radius=39, fill=PANEL)
        end = self._brand(canvas, draw, 66, H - 79, size=60)
        follow = "Síguenos y activa las notificaciones"
        font = self._fit_font(draw, follow, W - 48 - 90 - end - 40, 26, 18, weight="SemiBold")
        draw.text((W - 124, H - 79), follow, anchor="rm", font=font, fill=MUTED)
        self._paste_emoji(canvas, (W - 112, H - 100), "🔔", 42)

    def _stamp(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, deal: DealObservation, card) -> None:
        """Real urgency: when the price was checked (no fake countdowns). Bottom-left of the card."""
        checked = deal.observed_at.astimezone(ZoneInfo("America/Bogota")).strftime("%H:%M")
        stamp = f"Precio verificado hoy {checked}"
        sfont = self._font(24, bold=True)
        sw = int(draw.textlength(stamp, font=sfont))
        draw.rounded_rectangle((card[0] + 24, card[3] - 58, card[0] + 24 + sw + 74, card[3] - 18), radius=20, fill="#EEF2F7")
        self._paste_emoji(canvas, (card[0] + 34, card[3] - 54), "⏰", 32)
        draw.text((card[0] + 74, card[3] - 38), stamp, anchor="lm", font=sfont, fill="#334155")

    def _insight_badges(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, deal: DealObservation, card) -> None:
        """Lowest-price ribbon (top-left) and store rating pill (bottom-right), only when true."""
        if deal.lowest_30d:
            text, font = "PRECIO MÁS BAJO EN 30 DÍAS", self._font(28, bold=True)
            w = int(draw.textlength(text, font=font)) + 92
            draw.rounded_rectangle((card[0] + 24, card[1] + 24, card[0] + 24 + w, card[1] + 80), radius=28, fill=YELLOW)
            self._paste_emoji(canvas, (card[0] + 38, card[1] + 32), "🔻", 40)
            draw.text((card[0] + 86, card[1] + 52), text, anchor="lm", font=font, fill=NAVY)
        stars = rating_text(deal)
        if stars:
            font = self._font(26, bold=True)
            w = int(draw.textlength(stars, font=font)) + 78
            x1 = card[2] - 24
            draw.rounded_rectangle((x1 - w, card[3] - 58, x1, card[3] - 18), radius=20, fill="#FFF4CC")
            self._paste_emoji(canvas, (x1 - w + 12, card[3] - 54), "⭐", 32)
            draw.text((x1 - w + 52, card[3] - 38), stars, anchor="lm", font=font, fill="#7A5A00")

    def _burst(self, draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, text: str) -> None:
        """Red starburst sticker with the discount."""
        import math
        cx, cy = center
        points = []
        for i in range(32):
            r = radius if i % 2 == 0 else radius * 0.84
            a = math.pi * 2 * i / 32 - math.pi / 2
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        draw.polygon([(x + 6, y + 8) for x, y in points], fill="#04101F")
        draw.polygon(points, fill=RED)
        font = self._fit_font(draw, text, int(radius * 1.45), 92, 50)
        draw.text((cx, cy - 12), text, anchor="mm", font=font, fill=WHITE)
        draw.text((cx, cy + font.size // 2 + 8), "DCTO", anchor="mm", font=self._font(28, bold=True), fill=WHITE)

    def _download(self, url: str) -> bytes | None:
        if not url:
            return None
        current = url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                self._validate_public_https(current)
                response = self.session.get(current, stream=True, allow_redirects=False, timeout=(5, 15))
                if response.is_redirect or response.is_permanent_redirect:
                    target = response.headers.get("Location")
                    response.close()
                    if not target:
                        return None
                    current = urljoin(current, target)
                    continue
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if mime not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
                    response.close()
                    return None
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared > MAX_IMAGE_BYTES:
                    response.close()
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        response.close()
                        return None
                    chunks.append(chunk)
                response.close()
                return b"".join(chunks)
        except (requests.RequestException, ValueError, OSError):
            return None
        return None

    @staticmethod
    def _validate_public_https(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("URL de imagen no permitida")
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        for result in addresses:
            ip = ipaddress.ip_address(result[4][0])
            if not ip.is_global:
                raise ValueError("La imagen apunta a una red privada")

    @staticmethod
    def _load_product_image(data: bytes | None) -> Image.Image | None:
        if not data:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(BytesIO(data))
                image.verify()
                image = Image.open(BytesIO(data))
                image = ImageOps.exif_transpose(image)
                return trim_product(image)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            return None

    @staticmethod
    def _paste_emoji(canvas: Image.Image, xy: tuple[int, int], char: str, size: int) -> None:
        font = _emoji_font()
        if font is None:
            return
        glyph = Image.new("RGBA", (font.size * 2, font.size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((0, 0), char, font=font, embedded_color=True)
        bbox = glyph.getbbox()
        if not bbox:
            return
        glyph = glyph.crop(bbox)
        glyph.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas.paste(glyph, xy, glyph)

    @staticmethod
    def _font(size: int, bold: bool = False, weight: str | None = None) -> ImageFont.FreeTypeFont:
        return brand_font(size, weight or ("ExtraBold" if bold else "SemiBold"))

    def _fit_font(self, draw: ImageDraw.ImageDraw, text: str, width: int, start: int, minimum: int,
                  weight: str = "ExtraBold"):
        for size in range(start, minimum - 1, -2):
            font = self._font(size, weight=weight)
            if draw.textlength(text, font=font) <= width:
                return font
        return self._font(minimum, weight=weight)

    def _fit_lines(self, draw, text: str, width: int, max_lines: int, start: int, minimum: int,
                   weight: str = "Bold"):
        words = text.split()
        for size in range(start, minimum - 1, -2):
            font = self._font(size, weight=weight)
            lines: list[str] = []
            current = ""
            for word in words:
                trial = f"{current} {word}".strip()
                if draw.textlength(trial, font=font) <= width:
                    current = trial
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)
            if len(lines) <= max_lines:
                return font, lines
        # Still too long at the smallest size: wrap and end the last line with an ellipsis.
        font = self._font(minimum, weight=weight)
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        if len(lines) > max_lines:
            last = lines[max_lines - 1]
            while last and draw.textlength(last + "…", font=font) > width:
                last = last[:-1].rstrip()
            lines = lines[: max_lines - 1] + [last.rstrip(" ,.-") + "…"]
        return font, lines

    def _draw_follow_cta(
        self, canvas: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], compact: bool = False
    ) -> None:
        x1, y1, x2, y2 = box
        cx, width = (x1 + x2) // 2, x2 - x1 - (30 if compact else 50)
        orange, white, muted = YELLOW, WHITE, MUTED
        if compact:
            draw.rounded_rectangle(box, radius=26, fill=PANEL, outline=orange, width=3)
        else:
            draw.rounded_rectangle(box, radius=34, fill=PANEL)

        icon = None
        if not compact:
            icon_size = max(60, min(140, (y2 - y1) // 4))
            try:
                icon = Image.open(_ICON_PATH).convert("RGBA")
                icon.thumbnail((icon_size, icon_size), Image.Resampling.LANCZOS)
            except (OSError, UnidentifiedImageError):
                icon = None

        head_font, head = self._fit_lines(
            draw, "¡NO TE PIERDAS NINGUNA OFERTA!", width, max_lines=2, start=26 if compact else 40, minimum=18
        )
        body_font, body = self._fit_lines(
            draw, "Sigue la página y activa las notificaciones", width, max_lines=2,
            start=17 if compact else 24, minimum=14,
        )
        button_text = "SEGUIR OJO AL PRECIO"
        button_font = self._fit_font(draw, button_text, width - 60, 20 if compact else 24, 14)
        foot_font, foot = self._fit_lines(draw, "¿Te sirvió? ¡Reacciona y compártela!", width, max_lines=2, start=20, minimum=16)
        if compact:
            foot = []

        gap = 8 if compact else 18
        button_h = button_font.size + (20 if compact else 30)
        blocks = [
            (icon.height if icon else 0),
            len(head) * (head_font.size + 6),
            len(body) * (body_font.size + 6),
            button_h,
            len(foot) * (foot_font.size + 4),
        ]
        blocks = [b for b in blocks if b]
        y = y1 + max(10, ((y2 - y1) - sum(blocks) - gap * (len(blocks) - 1)) // 2)

        if icon:
            canvas.paste(icon, (cx - icon.width // 2, y), icon)
            y += icon.height + gap
        for lines, font, fill, step in ((head, head_font, orange, 6), (body, body_font, white, 6)):
            for line in lines:
                draw.text((cx, y), line, anchor="mt", font=font, fill=fill)
                y += font.size + step
            y += gap - step
        bw = draw.textlength(button_text, font=button_font) + 60
        draw.rounded_rectangle((cx - bw / 2, y, cx + bw / 2, y + button_h), radius=button_h // 2, fill=orange)
        draw.text((cx, y + button_h / 2), button_text, anchor="mm", font=button_font, fill="#071426")
        y += button_h + gap
        for line in foot:
            draw.text((cx, y), line, anchor="mt", font=foot_font, fill=muted)
            y += foot_font.size + 4
