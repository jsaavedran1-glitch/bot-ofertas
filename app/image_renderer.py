from __future__ import annotations

from decimal import Decimal
from io import BytesIO
import ipaddress
from pathlib import Path
import socket
from urllib.parse import urljoin, urlparse
import warnings

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.models import DealObservation, DiscountEvidence, from_minor, money, to_minor


CANVAS = (1080, 1080)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
Image.MAX_IMAGE_PIXELS = 25_000_000

_LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.png"
_ICON_PATH = Path(__file__).parent.parent / "assets" / "icon.png"


class MissingProductImage(ValueError):
    pass


class OfferImageRenderer:
    def __init__(self, output_dir: Path, session: requests.Session | None = None) -> None:
        self.output_dir = output_dir
        self.session = session or requests.Session()
        self._logo: Image.Image | None = self._load_logo((260, 70))
        self._logo_footer: Image.Image | None = self._load_logo((420, 96))

    @staticmethod
    def _load_logo(max_size: tuple[int, int] = (260, 70)) -> "Image.Image | None":
        try:
            img = Image.open(_LOGO_PATH).convert("RGBA")
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            return img
        except (OSError, UnidentifiedImageError):
            return None

    def render(
        self,
        deal: DealObservation,
        candidate_id: str,
        product_image_bytes: bytes | None = None,
        usd_cop_rate: Decimal | None = None,
        require_image: bool = False,
    ) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        canvas = Image.new("RGB", CANVAS, "#071426")
        draw = ImageDraw.Draw(canvas)
        for y in range(CANVAS[1]):
            ratio = y / CANVAS[1]
            color = (7 + int(8 * ratio), 20 + int(18 * ratio), 38 + int(26 * ratio))
            draw.line((0, y, CANVAS[0], y), fill=color)

        orange = "#FFB11B"
        white = "#FFFFFF"
        muted = "#A9B5C5"
        draw.rounded_rectangle((42, 38, 1038, 136), radius=28, fill="#0E223A")
        source_display = "MERCADO LIBRE" if deal.source == "mercadolibre" else deal.source.replace("_", " ").upper()
        src_font = self._fit_font(draw, source_display, 900, 46, 30)
        draw.text((540, 87), source_display, anchor="mm", font=src_font, fill=orange)

        # Left card: title at top, product image below
        draw.rounded_rectangle((42, 166, 582, 864), radius=36, fill="#FFFFFF")
        title_font, title_lines = self._fit_lines(draw, deal.title, 488, max_lines=3, start=34, minimum=22)
        ty = 186
        for line in title_lines:
            draw.text((65, ty), line, font=title_font, fill="#071426")
            ty += title_font.size + 7
        title_bottom = ty + 4

        product = self._load_product_image(product_image_bytes or self._download(deal.image_url))
        if product is None and require_image:
            raise MissingProductImage("La oferta no tiene foto del producto; no se publica.")
        img_area_top = title_bottom
        img_center_y = img_area_top + (864 - img_area_top) // 2
        if product is None:
            self._draw_follow_cta(canvas, draw, (70, img_area_top + 20, 554, 854))
        else:
            product = ImageOps.contain(product, (470, 864 - img_area_top - 20), Image.Resampling.LANCZOS)
            x = 312 - product.width // 2
            y = img_center_y - product.height // 2
            canvas.paste(product, (x, y), product if product.mode == "RGBA" else None)

        # Right side: badge + pricing (no title)
        badge = f"-{deal.discount_pct}%"
        draw.rounded_rectangle((625, 181, 994, 293), radius=30, fill=orange)
        draw.text((809, 237), badge, anchor="mm", font=self._font(64, bold=True), fill="#071426")

        # Emoji overlays
        ef64 = self._emoji_font(64)
        ef48 = self._emoji_font(48)
        if ef64:
            draw.text((1000, 181), "🔥", font=ef64, embedded_color=True)
        if ef48:
            draw.text((625, 340), "⚡", font=ef48, embedded_color=True)

        in_cop = deal.currency == "USD" and bool(usd_cop_rate)

        def shown(minor: int) -> str:
            if in_cop:
                return money(to_minor(from_minor(minor, "USD") * usd_cop_rate, "COP"), "COP")
            return money(minor, deal.currency)

        label = "PRECIO ACTUAL"
        draw.text((680, 390), label, font=self._font(23, bold=True), fill=muted)
        price_text = shown(deal.price_minor)
        price_font = self._fit_font(draw, price_text, 330, 64, 40)
        draw.text((680, 430), price_text, font=price_font, fill=orange)
        if in_cop:
            usd_text = f"{money(deal.price_minor, 'USD')} · valor aprox. en COP"
            draw.text((680, 508), usd_text, font=self._fit_font(draw, usd_text, 340, 24, 18), fill=muted)

        if deal.original_price_minor:
            if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
                reference_label = "ANTES"
            else:
                reference_label = "PRECIO TÍPICO OBSERVADO"
            draw.text((680, 585), reference_label, font=self._font(20, bold=True), fill=muted)
            reference = shown(deal.original_price_minor)
            ref_font = self._fit_font(draw, reference, 360, 38, 28)
            draw.text((680, 621), reference, font=ref_font, fill="#D3D9E2")
            if deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
                ref_w = draw.textlength(reference, font=ref_font)
                draw.line((680, 643, 680 + ref_w, 643), fill="#FF6B6B", width=4)
            if ef48:
                draw.text((625, 581), "💰", font=ef48, embedded_color=True)

        if product is not None:
            self._draw_follow_cta(canvas, draw, (625, 690, 1038, 872), compact=True)

        draw.rounded_rectangle((42, 900, 1038, 1038), radius=28, fill="#0E223A")
        if self._logo_footer:
            lw, lh = self._logo_footer.size
            canvas.paste(self._logo_footer, (72, 969 - lh // 2), self._logo_footer)
            txt_x = 72 + lw + 24
        else:
            txt_x = 72
        draw.text((txt_x, 925), "OJO AL PRECIO", font=self._font(26, bold=True), fill=white)
        tag_w = max(300, 990 - txt_x)
        tag_font, tag_lines = self._fit_lines(draw, "Descuentos limitados cada día, ¡corre y aprovecha la oferta!", tag_w, max_lines=2, start=26, minimum=18)
        ty = 963
        for line in tag_lines:
            draw.text((txt_x, ty), line, font=tag_font, fill=muted)
            ty += tag_font.size + 4
        if ef48:
            draw.text((1000, 945), "🔥", font=ef48, embedded_color=True)

        output = self.output_dir / f"{candidate_id}.png"
        temporary = self.output_dir / f".{candidate_id}.tmp.png"
        canvas.save(temporary, format="PNG", optimize=True, compress_level=9)
        temporary.replace(output)
        return output

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
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
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
                return image.convert("RGBA")
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            return None

    _EMOJI_SIZES = (160, 96, 64, 48, 40, 32, 20)
    _EMOJI_FONT_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"

    @classmethod
    def _emoji_font(cls, size: int) -> ImageFont.FreeTypeFont | None:
        best = min(cls._EMOJI_SIZES, key=lambda s: abs(s - size))
        try:
            return ImageFont.truetype(cls._EMOJI_FONT_PATH, best)
        except OSError:
            return None

    @staticmethod
    def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        names = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    def _fit_font(self, draw: ImageDraw.ImageDraw, text: str, width: int, start: int, minimum: int):
        for size in range(start, minimum - 1, -2):
            font = self._font(size, bold=True)
            if draw.textlength(text, font=font) <= width:
                return font
        return self._font(minimum, bold=True)

    def _fit_lines(self, draw, text: str, width: int, max_lines: int, start: int, minimum: int):
        words = text.split()
        for size in range(start, minimum - 1, -2):
            font = self._font(size, bold=True)
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
        font = self._font(minimum, bold=True)
        lines = words[:max_lines]
        if len(words) > max_lines:
            lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
        return font, lines

    def _draw_follow_cta(
        self, canvas: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], compact: bool = False
    ) -> None:
        x1, y1, x2, y2 = box
        cx, width = (x1 + x2) // 2, x2 - x1 - (30 if compact else 50)
        orange, white, muted = "#FFB11B", "#FFFFFF", "#A9B5C5"
        if compact:
            draw.rounded_rectangle(box, radius=26, fill="#0E223A", outline=orange, width=3)
        else:
            draw.rounded_rectangle(box, radius=34, fill="#0E223A")

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
