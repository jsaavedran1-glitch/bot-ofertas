from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import hashlib
import math
import subprocess

from PIL import Image, ImageDraw, ImageOps

from app.image_renderer import GREEN, MUTED, NAVY, PANEL, RED, WHITE, YELLOW, MissingProductImage, OfferImageRenderer
from app.models import DealObservation, DiscountEvidence, display_title, from_minor, money, store_name, to_minor
from app.scoring import price_cop

W, H, FPS, SECONDS = 1080, 1920, 30, 9
MUSIC_DIR = Path(__file__).parent.parent / "assets" / "music"
SFX_DIR = Path(__file__).parent.parent / "assets" / "sfx"
MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg"}
# Effects synced to the animation: sticker pops, swipe into the price, coins when the new price lands,
# ding with the savings pill, swipe into the call to action.
SFX_CUES = (("pop", 0.3), ("whoosh", 1.85), ("monedas", 3.0), ("notificacion", 3.6), ("whoosh", 5.05))
BIG_TICKET_COP = 400_000  # from here on, "ahorras $X" sells better than "-X%"


def music_track(seed: str, folder: Path | None = None) -> Path | None:
    """Royalty-free track for this reel (rotated by product), or None to keep the reel silent."""
    folder = folder or MUSIC_DIR
    tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT) if folder.is_dir() else []
    if not tracks:
        return None
    return tracks[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(tracks)]


def sfx_cues(folder: Path | None = None) -> list[tuple[Path, float]]:
    """(file, second) for each effect present in the folder; empty when there are none."""
    folder = folder or SFX_DIR
    if not folder.is_dir():
        return []
    files = {p.stem.lower(): p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT}
    return [(files[name], at) for name, at in SFX_CUES if name in files]


def audio_args(track: Path | None, cues: list[tuple[Path, float]], first_input: int = 1) -> list[str]:
    """ffmpeg inputs + filter for the reel soundtrack: looped music under timed effects."""
    inputs = ["-stream_loop", "-1", "-i", str(track)] if track else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    music_gain = "0.6" if cues else "0.7"
    parts = [f"[{first_input}:a]aresample=44100,aformat=channel_layouts=stereo,atrim=duration={SECONDS},"
             f"volume={music_gain},afade=t=in:d=0.4,afade=t=out:st={SECONDS - 1}:d=1[m]"]
    labels = ["[m]"]
    for i, (path, at) in enumerate(cues, start=first_input + 1):
        inputs += ["-i", str(path)]
        parts.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo,adelay=delays={int(at * 1000)}:all=1,volume=0.9[s{i}]")
        labels.append(f"[s{i}]")
    if len(labels) > 1:
        parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        parts.append("[m]anull[a]")
    return [*inputs, "-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]"]


# Key content stays inside Meta's Reels safe zone (Facebook draws its buttons and caption over the
# bottom and right edges) and inside the 4:5 crop the feed shows (rows ~285-1635).
HEADER_Y = 300
CARD = (65, 395, 1015, 1015)
TITLE_Y = 1035
LOWER = (0, 1140, W, 1600)  # area where the phases swap
FOOTER = (65, 1650, 1015, 1730)
BURST_CENTER, BURST_R, BURST_BOX = (W - 170, 440), 112, 300
PHASES = ((0.0, 2.0), (2.0, 5.2), (5.2, SECONDS))
FADE = 0.25
STORY_MOMENT = 4.9  # seconds into the reel: before price struck through, new price and savings shown


def _ease_out(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _pop(t: float, start: float, length: float = 0.35, lo: float = 0.6) -> float:
    """Scale factor from `lo` that overshoots slightly then settles at 1."""
    p = min(max((t - start) / length, 0.0), 1.0)
    return lo + (1.1 - lo) * _ease_out(p) - 0.1 * p


def reel_hook(deal: DealObservation, price: str, saved: str | None = None) -> tuple[str, list[str]]:
    """Opener for the first 2 seconds; a true "lowest in 30 days" wins, otherwise rotated per product."""
    if deal.lowest_30d:
        return "🔻", ["PRECIO MÁS BAJO", "EN 30 DÍAS"]
    pct = deal.discount_pct
    options = [
        ("🚨", ["¡PRECIO DE", "LOCURA!"]),
        ("🤔", ["¿PAGARÍAS", f"{price} POR ESTO?"]),
        ("🔥", ["¡BAJÓ", f"{pct}%!"]),
        ("👀", ["NO LO COMPRES", "SIN VER ESTO"]),
        ("⏰", ["OFERTA DE HOY", f"-{pct}%"]),
    ]
    if saved:
        options = [("💰", ["¡AHORRA", f"{saved}!"]), ("🤑", ["TE AHORRAS", saved])]
    return options[int(hashlib.md5(f"reel{deal.external_id}".encode()).hexdigest(), 16) % len(options)]


class ReelRenderer(OfferImageRenderer):
    """Renders a 9:16 deal video (MP4, H.264) with the same look as the photo post."""

    music_dir: Path | None = None  # set by the pipeline when the private audio comes from the database
    sfx_dir: Path | None = None

    def render_reel(
        self,
        deal: DealObservation,
        name: str,
        usd_cop_rate: Decimal | None = None,
        product_image_bytes: bytes | None = None,
        link_in_comment: bool = False,
    ) -> Path:
        base, photo, texts = self._prepare(deal, usd_cop_rate, product_image_bytes, link_in_comment)
        output = self.output_dir / f"{name}.mp4"
        audio = audio_args(music_track(deal.external_id, self.music_dir), sfx_cues(self.sfx_dir))
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
            *audio,
            "-t", str(SECONDS), "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
        ]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
            for i in range(FPS * SECONDS):
                proc.stdin.write(self._frame(base, photo, texts, i / FPS).tobytes())
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError("ffmpeg no pudo generar el reel.")
        return output

    def render_frame(self, deal: DealObservation, t: float, usd_cop_rate: Decimal | None = None,
                     product_image_bytes: bytes | None = None, link_in_comment: bool = False) -> Image.Image:
        """One frame of the reel (for previews)."""
        base, photo, texts = self._prepare(deal, usd_cop_rate, product_image_bytes, link_in_comment)
        return self._frame(base, photo, texts, t)

    def render_story(
        self,
        deal: DealObservation,
        name: str,
        usd_cop_rate: Decimal | None = None,
        product_image_bytes: bytes | None = None,
    ) -> Path:
        """9:16 still for Page stories: the reel's price moment plus a pointer to the post."""
        base, photo, texts = self._prepare(deal, usd_cop_rate, product_image_bytes, False)
        frame = self._frame(base, photo, texts, STORY_MOMENT)
        self._story_pointer(frame, "Mira la oferta completa en nuestra página", FOOTER)
        output = self.output_dir / f"{name}.png"
        frame.save(output, format="PNG", optimize=True)
        return output

    def render_story_from_image(self, image_path: Path, name: str, headline: str) -> Path:
        """Wrap a square post image (e.g. the promo grid) in a 9:16 story frame."""
        frame = self._background()
        draw = ImageDraw.Draw(frame)
        draw.text((W // 2, 250), headline, anchor="mm", font=self._fit_font(draw, headline, 980, 72, 36, weight="Black"), fill=YELLOW)
        with Image.open(image_path) as post_image:
            post = ImageOps.contain(post_image.convert("RGB"), (1000, 1250), Image.Resampling.LANCZOS)
        frame.paste(post, (W // 2 - post.width // 2, 330))
        self._story_pointer(frame, "Mira la promo completa en nuestra página", (65, 1620, W - 65, 1700))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"{name}.png"
        frame.save(output, format="PNG", optimize=True)
        return output

    def _story_pointer(self, frame: Image.Image, text: str, box: tuple[int, int, int, int]) -> None:
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=YELLOW)
        font = self._fit_font(draw, text, box[2] - box[0] - 140, 36, 24)
        cy = (box[1] + box[3]) // 2
        x = (box[0] + box[2]) // 2 + 30 - int(draw.textlength(text, font=font)) // 2
        draw.text((x, cy), text, anchor="lm", font=font, fill=NAVY)
        self._paste_emoji(frame, (x - 60, cy - 23), "👀", 46)

    def _prepare(self, deal, usd_cop_rate, product_image_bytes, link_in_comment):
        product = self._load_product_image(product_image_bytes or self._download(deal.image_url))
        if product is None:
            raise MissingProductImage("La oferta no tiene foto del producto; no se genera el video ni la historia.")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        in_cop = deal.currency == "USD" and bool(usd_cop_rate)
        currency = "COP" if in_cop else deal.currency

        def cop(minor: int) -> int:
            return to_minor(from_minor(minor, "USD") * usd_cop_rate, "COP") if in_cop else minor

        def shown(minor: int) -> str:
            return money(minor, currency).replace(" COP", "")

        official = deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL
        before = cop(deal.original_price_minor) if deal.original_price_minor else None
        now = cop(deal.price_minor)
        saved = shown(before - now) if before else None
        big = official and saved and (price_cop(deal, usd_cop_rate) or 0) >= BIG_TICKET_COP
        emoji, hook_lines = reel_hook(deal, shown(now), saved if big else None)

        photo = ImageOps.contain(product, (CARD[2] - CARD[0] - 80, CARD[3] - CARD[1] - 150), Image.Resampling.LANCZOS)
        texts = {
            "hook": self._hook_layer(emoji, hook_lines),
            "burst": self._burst_layer(f"-{deal.discount_pct}%"),
            "card": self._card_overlay(deal),
            "before_label": "ANTES" if official else "PRECIO TÍPICO",
            "before": self._text_layer(shown(before), 52, "#D3D9E2") if before else None,
            "strike": official,
            "now": self._text_layer(shown(now), 132, YELLOW, max_width=820, minimum=60, weight="Black"),
            "saved": self._pill(f"AHORRAS {saved}") if saved else None,
            "note": f"Precio aprox. en pesos · US${from_minor(deal.price_minor, 'USD'):,.2f}" if in_cop else None,
            "cta": self._cta("Link de la oferta en los comentarios" if link_in_comment else "Link de la oferta en la descripción"),
        }
        return self._base(deal), photo, texts

    # ---------- static layers ----------

    def _background(self) -> Image.Image:
        img = Image.new("RGB", (W, H), NAVY)
        draw = ImageDraw.Draw(img)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(8 + int(10 * r), 24 + int(22 * r), 48 + int(40 * r)))
        return img

    def _base(self, deal: DealObservation) -> Image.Image:
        img = self._background()
        draw = ImageDraw.Draw(img)
        # Brand on top (visible full screen; the feed's 4:5 crop starts below it)
        size = 84
        width = (size + 14 if self._icon else 0) + int(draw.textlength("OJO AL PRECIO", font=self._font(size // 2, weight="Black")))
        self._brand(img, draw, W // 2 - width // 2, 180, size=size)
        # Store chip + tagline, as in the photo post
        store = store_name(deal.source).upper()
        chip_font = self._fit_font(draw, store, 330, 40, 26)
        chip_w = int(draw.textlength(store, font=chip_font)) + 64
        draw.rounded_rectangle((CARD[0], HEADER_Y, CARD[0] + chip_w, HEADER_Y + 72), radius=36, fill=WHITE)
        draw.text((CARD[0] + chip_w // 2, HEADER_Y + 36), store, anchor="mm", font=chip_font, fill=NAVY)
        self._paste_emoji(img, (CARD[0] + chip_w + 22, HEADER_Y + 14), "🔥", 44)
        draw.text((CARD[0] + chip_w + 76, HEADER_Y + 36), "OFERTA DEL DÍA", anchor="lm",
                  font=self._font(34, weight="Black"), fill=YELLOW)
        draw.rounded_rectangle(CARD, radius=48, fill=WHITE)
        font, lines = self._fit_lines(draw, display_title(deal.title), 900, max_lines=2, start=44, minimum=30)
        y = TITLE_Y
        for line in lines:
            draw.text((W // 2, y), line, anchor="ma", font=font, fill=WHITE)
            y += font.size + 8
        # Footer (under Facebook's caption when full screen; nothing essential here)
        draw.rounded_rectangle(FOOTER, radius=40, fill=PANEL)
        follow = "Síguenos y activa las notificaciones"
        ffont = self._fit_font(draw, follow, 760, 32, 22, weight="Bold")
        x = W // 2 - 28 - int(draw.textlength(follow, font=ffont)) // 2
        draw.text((x, (FOOTER[1] + FOOTER[3]) // 2), follow, anchor="lm", font=ffont, fill=WHITE)
        self._paste_emoji(img, (x + int(draw.textlength(follow, font=ffont)) + 14, FOOTER[1] + 17), "🔔", 46)
        return img

    def _card_overlay(self, deal: DealObservation) -> Image.Image:
        """Verified-time stamp, rating and lowest-price badges, drawn over the moving photo."""
        cw, ch = CARD[2] - CARD[0], CARD[3] - CARD[1]
        layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        self._stamp(layer, draw, deal, (0, 0, cw, ch))
        self._insight_badges(layer, draw, deal, (0, 0, cw, ch))
        return layer

    def _burst_layer(self, text: str) -> Image.Image:
        layer = Image.new("RGBA", (BURST_BOX, BURST_BOX), (0, 0, 0, 0))
        self._burst(ImageDraw.Draw(layer), (BURST_BOX // 2, BURST_BOX // 2), BURST_R, text)
        return layer.rotate(-8, resample=Image.Resampling.BICUBIC)

    def _text_layer(self, text: str, size: int, fill: str, max_width: int = 900, minimum: int = 36,
                    weight: str = "ExtraBold") -> Image.Image:
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font = self._fit_font(probe, text, max_width, size, minimum, weight=weight)
        layer = Image.new("RGBA", (int(probe.textlength(text, font=font)) + 20, font.size + 40), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((10, 20), text, font=font, fill=fill)
        return layer

    def _pill(self, text: str) -> Image.Image:
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font = self._fit_font(probe, text, 760, 44, 28)
        w = int(probe.textlength(text, font=font)) + 80
        layer = Image.new("RGBA", (w, 72), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, 0, w - 1, 71), radius=36, fill=GREEN)
        d.text((w // 2, 36), text, anchor="mm", font=font, fill=NAVY)
        return layer

    def _hook_layer(self, emoji: str, lines: list[str]) -> Image.Image:
        layer = Image.new("RGBA", (940, 420), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        self._paste_emoji(layer, (470 - 55, 0), emoji, 110)
        font = min((self._fit_font(d, line, 880, 104, 56, weight="Black") for line in lines), key=lambda f: f.size)
        for i, line in enumerate(lines):
            d.text((470, 190 + i * (font.size + 22)), line, anchor="mm", font=font, fill=YELLOW if i else WHITE,
                   stroke_width=4, stroke_fill=NAVY)
        return layer

    def _cta(self, link_text: str) -> Image.Image:
        layer = Image.new("RGBA", (900, 430), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((0, 0, 899, 429), radius=44, fill=PANEL, outline=YELLOW, width=4)
        head = self._font(58, weight="Black")
        d.text((450, 62), "¡NO TE PIERDAS", anchor="mm", font=head, fill=YELLOW)
        d.text((450, 128), "NINGUNA OFERTA!", anchor="mm", font=head, fill=YELLOW)
        body = self._font(32, bold=True)
        d.text((450, 192), "Sigue la página y activa", anchor="mm", font=body, fill=WHITE)
        d.text((450, 232), "las notificaciones", anchor="mm", font=body, fill=WHITE)
        btn = self._font(40, bold=True)
        bw = int(d.textlength("SEGUIR OJO AL PRECIO", font=btn)) + 90
        d.rounded_rectangle((450 - bw // 2, 272, 450 + bw // 2, 356), radius=42, fill=YELLOW)
        d.text((450, 314), "SEGUIR OJO AL PRECIO", anchor="mm", font=btn, fill=NAVY)
        font = self._font(28, bold=True)
        x = 450 + 24 - int(d.textlength(link_text, font=font)) // 2
        d.text((x, 395), link_text, anchor="lm", font=font, fill=MUTED)
        self._paste_emoji(layer, (x - 48, 376), "👇", 38)
        return layer

    # ---------- per frame ----------

    def _frame(self, base: Image.Image, photo: Image.Image, texts: dict, t: float) -> Image.Image:
        frame = base.copy()
        # Ken Burns on the product photo, above the card's bottom strip (stamp and rating)
        zoom = 1.0 + 0.08 * (t / SECONDS)
        pw, ph = int(photo.width * zoom), int(photo.height * zoom)
        z = photo.resize((pw, ph), Image.Resampling.BILINEAR)
        box_w, box_h = CARD[2] - CARD[0] - 40, CARD[3] - CARD[1] - 110
        crop = z.crop(((pw - min(pw, box_w)) // 2, (ph - min(ph, box_h)) // 2,
                       (pw + min(pw, box_w)) // 2, (ph + min(ph, box_h)) // 2))
        cx, cy = (CARD[0] + CARD[2]) // 2, (CARD[1] + CARD[3] - 70) // 2
        frame.paste(crop, (cx - crop.width // 2, cy - crop.height // 2), crop)
        frame.paste(texts["card"], CARD[:2], texts["card"])

        # Discount burst: pops in, then breathes
        if t >= 0.3:
            s = _pop(t, 0.3, lo=0.3)
            if t > 1.0:
                s *= 1 + 0.035 * math.sin(2 * math.pi * 1.2 * (t - 1.0))
            burst = texts["burst"]
            b = burst.resize((int(burst.width * s), int(burst.height * s)), Image.Resampling.BILINEAR)
            frame.paste(b, (BURST_CENTER[0] - b.width // 2, BURST_CENTER[1] - b.height // 2), b)

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
            layer = self._phase(idx, t - start, texts)
            if alpha < 1:
                layer.putalpha(layer.getchannel("A").point(lambda a, k=alpha: int(a * k)))
            lower.alpha_composite(layer)
        frame.paste(lower, (LOWER[0], LOWER[1]), lower)
        return frame

    def _phase(self, idx: int, lt: float, texts: dict) -> Image.Image:
        size = (LOWER[2] - LOWER[0], LOWER[3] - LOWER[1])
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        cx = size[0] // 2

        def place(img: Image.Image, center_y: int, scale: float = 1.0) -> None:
            if scale != 1.0:
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.BILINEAR)
            layer.alpha_composite(img, (cx - img.width // 2, center_y - img.height // 2))

        if idx == 0:
            place(texts["hook"], 225, _pop(lt, 0.0, lo=0.9))
        elif idx == 1:
            d = ImageDraw.Draw(layer)
            if texts["before"] is not None:
                d.text((cx, 25), texts["before_label"], anchor="mm", font=self._font(32, bold=True), fill=MUTED)
                before = texts["before"]
                place(before, 80)
                progress = _ease_out((lt - 0.4) / 0.5) if texts["strike"] else 0
                if progress > 0:
                    x0 = cx - before.width // 2 + 10
                    d.line((x0, 84, x0 + int((before.width - 20) * progress), 84), fill=RED, width=7)
            if lt >= 1.0:
                d.text((cx, 152), "AHORA", anchor="mm", font=self._font(38, bold=True), fill=WHITE)
                place(texts["now"], 245, _pop(lt, 1.0))
            if lt >= 1.6:
                if texts["saved"] is not None:
                    place(texts["saved"], 370, _pop(lt, 1.6))
                if texts["note"]:
                    d.text((cx, 432), texts["note"], anchor="mm", font=self._font(26, bold=True), fill=MUTED)
        else:
            pulse = 1.0 + 0.03 * abs(((lt * 2) % 2) - 1)
            place(texts["cta"], 230, _pop(lt, 0.1) * pulse if lt < 0.45 else pulse)
        return layer
