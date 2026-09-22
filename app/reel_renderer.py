from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import hashlib
import subprocess

from PIL import Image, ImageDraw, ImageOps

from app.image_renderer import MissingProductImage, OfferImageRenderer
from app.models import DealObservation, from_minor, money, store_name, to_minor

W, H, FPS, SECONDS = 1080, 1920, 30, 9
ORANGE, WHITE, MUTED, NAVY, PANEL = "#FFB11B", "#FFFFFF", "#A9B5C5", "#071426", "#0E223A"
CARD = (90, 250, 990, 1090)
LOWER = (0, 1280, W, 1790)  # area where the phases swap
PHASES = ((0.0, 2.0), (2.0, 5.2), (5.2, SECONDS))
FADE = 0.25
STORY_MOMENT = 4.9  # seconds into the reel: before price struck through, new price shown


def _ease_out(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _pop(t: float, start: float, length: float = 0.35, lo: float = 0.6) -> float:
    """Scale factor from `lo` that overshoots slightly then settles at 1."""
    p = min(max((t - start) / length, 0.0), 1.0)
    return lo + (1.1 - lo) * _ease_out(p) - 0.1 * p


def reel_hook(deal: DealObservation, price: str) -> tuple[str, list[str]]:
    """Rotating opener for the first 2 seconds; picked per product so reposts stay consistent."""
    pct = deal.discount_pct
    options = [
        ("🚨", ["¡PRECIO DE", "LOCURA!"]),
        ("🤔", ["¿PAGARÍAS", f"{price} POR ESTO?"]),
        ("🔥", ["¡BAJÓ", f"{pct}%!"]),
        ("👀", ["NO LO COMPRES", "SIN VER ESTO"]),
        ("⏰", ["OFERTA DE HOY", f"-{pct}%"]),
    ]
    return options[int(hashlib.md5(f"reel{deal.external_id}".encode()).hexdigest(), 16) % len(options)]


class ReelRenderer(OfferImageRenderer):
    """Renders a 9:16 deal video (MP4, H.264) from the same data as the photo post."""

    def render_reel(
        self,
        deal: DealObservation,
        name: str,
        usd_cop_rate: Decimal | None = None,
        product_image_bytes: bytes | None = None,
        link_in_comment: bool = False,
    ) -> Path:
        base, photo, texts, in_cop = self._prepare(deal, usd_cop_rate, product_image_bytes, link_in_comment)
        output = self.output_dir / f"{name}.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(output),
        ]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
            for i in range(FPS * SECONDS):
                t = i / FPS
                proc.stdin.write(self._frame(base, photo, texts, t, in_cop, deal).tobytes())
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError("ffmpeg no pudo generar el reel.")
        return output

    def render_story(
        self,
        deal: DealObservation,
        name: str,
        usd_cop_rate: Decimal | None = None,
        product_image_bytes: bytes | None = None,
    ) -> Path:
        """9:16 still for Page stories: the reel's before/now price moment plus a pointer to the post."""
        base, photo, texts, in_cop = self._prepare(deal, usd_cop_rate, product_image_bytes, False)
        frame = self._frame(base, photo, texts, STORY_MOMENT, in_cop, deal)
        draw = ImageDraw.Draw(frame)
        text, font = "Mira la oferta completa en nuestra página", self._font(34, bold=True)
        x = W // 2 + 30 - int(draw.textlength(text, font=font)) // 2
        draw.text((x, 1745), text, anchor="lm", font=font, fill=WHITE)
        self._paste_emoji(frame, (x - 62, 1722), "👀", 46)
        output = self.output_dir / f"{name}.png"
        frame.save(output, format="PNG", optimize=True)
        return output

    def render_story_from_image(self, image_path: Path, name: str, headline: str) -> Path:
        """Wrap a square post image (e.g. the promo grid) in a 9:16 story frame."""
        frame = Image.new("RGB", (W, H), NAVY)
        draw = ImageDraw.Draw(frame)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(7 + int(8 * r), 20 + int(18 * r), 38 + int(26 * r)))
        draw.text((W // 2, 330), headline, anchor="mm", font=self._fit_font(draw, headline, 980, 64, 36), fill=ORANGE)
        with Image.open(image_path) as square:
            post = square.convert("RGB").resize((1020, 1020), Image.Resampling.LANCZOS)
        frame.paste(post, (30, 450))
        text, font = "Mira la promo completa en nuestra página", self._font(36, bold=True)
        x = W // 2 + 30 - int(draw.textlength(text, font=font)) // 2
        draw.text((x, 1600), text, anchor="lm", font=font, fill=WHITE)
        self._paste_emoji(frame, (x - 64, 1576), "👀", 48)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"{name}.png"
        frame.save(output, format="PNG", optimize=True)
        return output

    def _prepare(self, deal, usd_cop_rate, product_image_bytes, link_in_comment):
        product = self._load_product_image(product_image_bytes or self._download(deal.image_url))
        if product is None:
            raise MissingProductImage("La oferta no tiene foto del producto; no se genera el video ni la historia.")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        in_cop = deal.currency == "USD" and bool(usd_cop_rate)

        def shown(minor: int) -> str:
            if in_cop:
                return money(to_minor(from_minor(minor, "USD") * usd_cop_rate, "COP"), "COP")
            return money(minor, deal.currency)

        base = self._base(deal)
        photo = ImageOps.contain(product, (CARD[2] - CARD[0] - 80, CARD[3] - CARD[1] - 80), Image.Resampling.LANCZOS)
        emoji, hook_lines = reel_hook(deal, shown(deal.price_minor).replace(" COP", ""))
        texts = {
            "hook": self._hook_layer(emoji, hook_lines),
            "badge": self._badge(f"-{deal.discount_pct}%"),
            "before": self._text_layer(shown(deal.original_price_minor), 52, "#D3D9E2") if deal.original_price_minor else None,
            "now": self._text_layer(shown(deal.price_minor), 96, ORANGE),
            "cta": self._cta("Link de la oferta en los comentarios" if link_in_comment else "Link de la oferta en la descripción"),
        }
        return base, photo, texts, in_cop

    # ---------- static layers ----------

    def _base(self, deal: DealObservation) -> Image.Image:
        img = Image.new("RGB", (W, H), NAVY)
        draw = ImageDraw.Draw(img)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(7 + int(8 * r), 20 + int(18 * r), 38 + int(26 * r)))
        draw.rounded_rectangle((60, 80, W - 60, 190), radius=30, fill=PANEL)
        store = store_name(deal.source).upper()
        draw.text((W // 2, 135), store, anchor="mm", font=self._fit_font(draw, store, 900, 56, 34), fill=ORANGE)
        draw.rounded_rectangle(CARD, radius=44, fill=WHITE)
        font, lines = self._fit_lines(draw, deal.title, 920, max_lines=2, start=46, minimum=30)
        y = 1125
        for line in lines:
            draw.text((W // 2, y), line, anchor="mt", font=font, fill=WHITE)
            y += font.size + 8
        # footer
        draw.rounded_rectangle((60, 1800, W - 60, 1880), radius=26, fill=PANEL)
        if self._logo:
            lw, lh = self._logo.size
            img.paste(self._logo, (90, 1840 - lh // 2), self._logo)
        draw.text((W - 90, 1840), "OJO AL PRECIO", anchor="rm", font=self._font(32, bold=True), fill=WHITE)
        return img

    def _text_layer(self, text: str, size: int, fill: str, emoji: str = "") -> Image.Image:
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font = self._fit_font(probe, text, 900 - (size if emoji else 0), size, 36)
        tw = int(probe.textlength(text, font=font))
        esz = int(font.size * 1.1) if emoji else 0
        layer = Image.new("RGBA", (tw + esz + (20 if emoji else 0) + 20, font.size + 40), (0, 0, 0, 0))
        if emoji:
            self._paste_emoji(layer, (10, 12), emoji, esz)
        ImageDraw.Draw(layer).text((esz + (30 if emoji else 10), 20), text, font=font, fill=fill)
        return layer

    def _hook_layer(self, emoji: str, lines: list[str]) -> Image.Image:
        layer = Image.new("RGBA", (980, 470), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        self._paste_emoji(layer, (490 - 60, 0), emoji, 120)
        font = min((self._fit_font(d, line, 940, 104, 56) for line in lines), key=lambda f: f.size)
        for i, line in enumerate(lines):
            d.text((490, 215 + i * (font.size + 24)), line, anchor="mm", font=font, fill=ORANGE if i else WHITE,
                   stroke_width=3, stroke_fill=NAVY)
        return layer

    def _badge(self, text: str) -> Image.Image:
        font = self._font(84, bold=True)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        w = int(probe.textlength(text, font=font)) + 90
        layer = Image.new("RGBA", (w, 140), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, 0, w - 1, 139), radius=40, fill=ORANGE)
        d.text((w // 2, 70), text, anchor="mm", font=font, fill=NAVY)
        return layer

    def _cta(self, link_text: str) -> Image.Image:
        layer = Image.new("RGBA", (960, 500), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, 0, 959, 499), radius=44, fill=PANEL, outline=ORANGE, width=4)
        head = self._font(62, bold=True)
        d.text((480, 70), "¡NO TE PIERDAS", anchor="mm", font=head, fill=ORANGE)
        d.text((480, 140), "NINGUNA OFERTA!", anchor="mm", font=head, fill=ORANGE)
        body = self._font(34, bold=True)
        d.text((480, 215), "Sigue la página y activa", anchor="mm", font=body, fill=WHITE)
        d.text((480, 258), "las notificaciones", anchor="mm", font=body, fill=WHITE)
        btn = self._font(40, bold=True)
        bw = int(d.textlength("SEGUIR OJO AL PRECIO", font=btn)) + 90
        d.rounded_rectangle((480 - bw // 2, 305, 480 + bw // 2, 395), radius=45, fill=ORANGE)
        d.text((480, 350), "SEGUIR OJO AL PRECIO", anchor="mm", font=btn, fill=NAVY)
        d.text((480, 450), link_text, anchor="mm", font=self._font(30, bold=True), fill=MUTED)
        return layer

    # ---------- per frame ----------

    def _frame(self, base, photo, texts, t: float, in_cop: bool, deal: DealObservation) -> Image.Image:
        frame = base.copy()
        # Ken Burns on the product photo
        zoom = 1.0 + 0.10 * (t / SECONDS)
        pw, ph = int(photo.width * zoom), int(photo.height * zoom)
        z = photo.resize((pw, ph), Image.Resampling.BILINEAR)
        cx, cy = (CARD[0] + CARD[2]) // 2, (CARD[1] + CARD[3]) // 2
        box_w, box_h = CARD[2] - CARD[0] - 40, CARD[3] - CARD[1] - 40
        crop = z.crop(((pw - min(pw, box_w)) // 2, (ph - min(ph, box_h)) // 2,
                       (pw + min(pw, box_w)) // 2, (ph + min(ph, box_h)) // 2))
        frame.paste(crop, (cx - crop.width // 2, cy - crop.height // 2), crop)
        badge = texts["badge"]
        s = _pop(t, 0.3)
        b = badge.resize((int(badge.width * s), int(badge.height * s)), Image.Resampling.BILINEAR)
        frame.paste(b, (CARD[2] - 20 - b.width // 2 - badge.width // 2, CARD[1] - 40 + (badge.height - b.height) // 2), b)

        lower = Image.new("RGBA", (LOWER[2] - LOWER[0], LOWER[3] - LOWER[1]), (0, 0, 0, 0))
        for idx, (start, end) in enumerate(PHASES):
            if not start <= t < end:
                continue
            alpha = 1.0
            if idx > 0:
                alpha = min(alpha, (t - start) / FADE)
            if idx < len(PHASES) - 1:
                alpha = min(alpha, (end - t) / FADE)
            if alpha <= 0:
                continue
            layer = self._phase(idx, t - start, texts, in_cop)
            if alpha < 1:
                layer.putalpha(layer.getchannel("A").point(lambda a, k=alpha: int(a * k)))
            lower.alpha_composite(layer)
        frame.paste(lower, (LOWER[0], LOWER[1]), lower)
        return frame

    def _phase(self, idx: int, lt: float, texts, in_cop: bool) -> Image.Image:
        size = (LOWER[2] - LOWER[0], LOWER[3] - LOWER[1])
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        cx = size[0] // 2

        def place(img: Image.Image, center_y: int, scale: float = 1.0) -> None:
            if scale != 1.0:
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.BILINEAR)
            layer.alpha_composite(img, (cx - img.width // 2, center_y - img.height // 2))

        if idx == 0:
            place(texts["hook"], 250, _pop(lt, 0.0, lo=0.9))
        elif idx == 1:
            d = ImageDraw.Draw(layer)
            d.text((cx, 40), "ANTES", anchor="mm", font=self._font(34, bold=True), fill=MUTED)
            if texts["before"] is not None:
                before = texts["before"]
                place(before, 110)
                progress = _ease_out((lt - 0.4) / 0.5)
                if progress > 0:
                    x0 = cx - before.width // 2 + 10
                    d.line((x0, 112, x0 + int((before.width - 20) * progress), 112), fill="#FF6B6B", width=7)
            if lt >= 1.0:
                d.text((cx, 215), "AHORA", anchor="mm", font=self._font(38, bold=True), fill=WHITE)
                place(texts["now"], 310, _pop(lt, 1.0))
                if in_cop:
                    d.text((cx, 400), "valor aproximado en pesos", anchor="mm", font=self._font(28), fill=MUTED)
        else:
            pulse = 1.0 + 0.03 * abs(((lt * 2) % 2) - 1)
            place(texts["cta"], 255, _pop(lt, 0.1) * pulse if lt < 0.45 else pulse)
        return layer
