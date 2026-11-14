"""Manual coupon posts: a list of store codes (written by hand in a JSON file) as one 4:5 image + caption."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from app.image_renderer import MUTED, NAVY, POST_CANVAS, RED, WHITE, YELLOW, OfferImageRenderer

COUPONS_PATH = Path(__file__).resolve().parent.parent / "assets" / "content" / "cupones.json"
MAX_COUPONS = 5


@dataclass(frozen=True)
class Coupon:
    code: str
    benefit: str  # "$50.000 OFF" or "15% OFF"
    minimum: str  # "$200.000"
    tag: str = ""  # optional short flag, e.g. "Por agotarse"


@dataclass(frozen=True)
class CouponPost:
    id: str
    store: str
    coupons: tuple[Coupon, ...]
    caption: str
    note: str = ""


def load_coupon_post(path: Path = COUPONS_PATH) -> CouponPost:
    data = json.loads(path.read_text(encoding="utf-8"))
    coupons = tuple(Coupon(**c) for c in data["coupons"])
    if not 1 <= len(coupons) <= MAX_COUPONS:
        raise ValueError(f"El archivo de cupones debe tener entre 1 y {MAX_COUPONS} cupones.")
    if not data.get("caption", "").strip():
        raise ValueError("Falta el texto (caption) de la publicación de cupones.")
    return CouponPost(id=data["id"], store=data["store"], coupons=coupons,
                      caption=data["caption"].strip(), note=data.get("note", ""))


class CouponRenderer(OfferImageRenderer):
    """Navy 4:5 card: store chip, headline and one ticket per code (code, benefit, minimum purchase)."""

    def render_coupons(self, post: CouponPost) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(8 + int(10 * r), 24 + int(22 * r), 48 + int(40 * r)))

        store = post.store.upper()
        chip_font = self._fit_font(draw, store, 520, 38, 26)
        chip_w = int(draw.textlength(store, font=chip_font)) + 64
        draw.rounded_rectangle((48, 44, 48 + chip_w, 118), radius=37, fill=YELLOW)
        draw.text((48 + chip_w // 2, 81), store, anchor="mm", font=chip_font, fill=NAVY)
        self._paste_emoji(canvas, (48, 150), "🎟️", 92)
        draw.text((160, 196), "CUPONES", anchor="lm", font=self._font(104, weight="Black"), fill=WHITE)
        draw.text((54, 286), "Copia el código y aplícalo al pagar", anchor="lm",
                  font=self._font(32, weight="SemiBold"), fill=MUTED)

        n = len(post.coupons)
        top, bottom, gap = 330, H - 210, 18
        row_h = min(190, (bottom - top - gap * (n - 1)) // n)
        y = top + (bottom - top - (row_h * n + gap * (n - 1))) // 2
        for coupon in post.coupons:
            self._ticket(draw, coupon, 48, y, W - 48, y + row_h)
            y += row_h + gap

        if post.note:
            nfont = self._fit_font(draw, post.note, W - 96, 24, 18, weight="SemiBold")
            draw.text((W // 2, H - 158), post.note, anchor="mm", font=nfont, fill=MUTED)
        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"cupones_{post.id}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output

    def _ticket(self, draw: ImageDraw.ImageDraw, coupon: Coupon, x1: int, y1: int, x2: int, y2: int) -> None:
        h = y2 - y1
        split = x1 + 560
        draw.rounded_rectangle((x1, y1, x2, y2), radius=28, fill=WHITE)
        # Ticket notches and dashed cut between the code and the benefit
        for cy in (y1, y2):
            draw.ellipse((split - 18, cy - 18, split + 18, cy + 18), fill=NAVY)
        for dy in range(y1 + 26, y2 - 26, 22):
            draw.line((split, dy, split, dy + 11), fill="#C7D2E0", width=3)

        draw.text((x1 + 36, y1 + 34), "CÓDIGO", font=self._font(22, weight="ExtraBold"), fill="#5B6B80")
        code_font = self._fit_font(draw, coupon.code, split - x1 - 72, 66, 36, weight="Black")
        draw.text((x1 + 34, y1 + h // 2 + 14), coupon.code, anchor="lm", font=code_font, fill=NAVY)
        if coupon.tag:
            tfont = self._font(22, weight="ExtraBold")
            tw = int(draw.textlength(coupon.tag.upper(), font=tfont)) + 28
            tx = x1 + 36 + int(draw.textlength("CÓDIGO", font=self._font(22, weight="ExtraBold"))) + 16
            draw.rounded_rectangle((tx, y1 + 28, tx + tw, y1 + 62), radius=17, fill=RED)
            draw.text((tx + tw // 2, y1 + 45), coupon.tag.upper(), anchor="mm", font=tfont, fill=WHITE)

        cx = (split + x2) // 2
        bfont = self._fit_font(draw, coupon.benefit, x2 - split - 48, 58, 30, weight="Black")
        draw.text((cx, y1 + h // 2 - 16), coupon.benefit, anchor="mm", font=bfont, fill=RED)
        minimum = f"Compra mínima {coupon.minimum}"
        mfont = self._fit_font(draw, minimum, x2 - split - 40, 24, 18, weight="Bold")
        draw.text((cx, y1 + h // 2 + 36), minimum, anchor="mm", font=mfont, fill="#334155")
