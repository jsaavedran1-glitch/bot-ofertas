from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from app.copywriter import facebook_copy
from app.image_renderer import OfferImageRenderer
from app.models import DealObservation, DiscountEvidence


class RendererTests(unittest.TestCase):
    def test_private_image_address_is_rejected(self):
        with self.assertRaises(ValueError):
            OfferImageRenderer._validate_public_https("https://127.0.0.1/image.png")

    def test_render_is_1080_png_with_long_title_and_missing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            deal = DealObservation(
                source="tienda", external_id="LONG", title="Producto increíble " * 18,
                price_minor=4_283_132, original_price_minor=5_500_000,
                evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
                url="https://example.com/long",
            )
            path = OfferImageRenderer(Path(directory)).render(deal, "long")
            with Image.open(path) as output:
                self.assertEqual(output.size, (1080, 1080))
                self.assertEqual(output.format, "PNG")

    def test_corrupt_product_image_uses_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            deal = DealObservation(
                source="tienda", external_id="BAD", title="Producto",
                price_minor=80_000, original_price_minor=100_000,
                evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
                url="https://example.com/bad",
            )
            path = OfferImageRenderer(Path(directory)).render(deal, "bad", b"not-an-image")
            self.assertTrue(path.is_file())

    def test_affiliate_disclosure_is_conditional(self):
        deal = DealObservation(
            source="amazon", external_id="SKU", title="Producto", price_minor=3999,
            original_price_minor=6999, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="USD", url="https://example.com/sku", affiliate=False,
        )
        self.assertNotIn("Enlace afiliado", facebook_copy(deal, None, True))


class CopyAndConfigTests(unittest.TestCase):
    def test_caption_moves_link_to_comment_and_tags_by_source(self):
        deal = DealObservation(
            source="alkosto", external_id="W1", title="Parlante", price_minor=90000,
            original_price_minor=159000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="COP", url="https://example.com/w1",
        )
        self.assertIn("https://example.com/w1", facebook_copy(deal, None, True))
        text = facebook_copy(deal, None, True, link_in_comment=True)
        self.assertNotIn("https://example.com/w1", text)
        self.assertIn("primer comentario", text)
        self.assertIn("#alkosto", text)
        self.assertNotIn("#mercadolibre", text)
        self.assertIn("?", text.split("#OjoAlPrecio")[0].strip().splitlines()[-1])

    def test_usd_image_renders_with_cop_rate(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as directory:
            deal = DealObservation(
                source="amazon", external_id="A1", title="Mouse", price_minor=1400,
                original_price_minor=2800, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                currency="USD", url="https://example.com/a1",
            )
            path = OfferImageRenderer(Path(directory)).render(deal, "a1", b"x", usd_cop_rate=Decimal("4000"))
            self.assertTrue(path.is_file())

    def test_publish_hours_parsing(self):
        from app.config import _parse_hours
        self.assertEqual(_parse_hours("7-9,12-14,19-22"), frozenset({7, 8, 12, 13, 19, 20, 21}))
        self.assertEqual(_parse_hours(""), frozenset(range(24)))
        with self.assertRaises(ValueError):
            _parse_hours("20-25")


class EmojiTests(unittest.TestCase):
    def test_emoji_is_drawn_when_a_color_emoji_font_exists(self):
        from app.image_renderer import _emoji_font
        if _emoji_font() is None:
            self.skipTest("sin fuente de emojis en este sistema")
        canvas = Image.new("RGB", (80, 80), "black")
        OfferImageRenderer._paste_emoji(canvas, (10, 10), "🔥", 48)
        colors = {canvas.getpixel((x, y)) for x in range(10, 58, 4) for y in range(10, 58, 4)}
        self.assertGreater(len(colors), 3)


class ReelRenderTests(unittest.TestCase):
    def test_reel_is_a_9_second_vertical_mp4(self):
        import shutil, subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg no instalado")
        from app.reel_renderer import ReelRenderer
        buffer = BytesIO()
        Image.new("RGB", (300, 300), "red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            deal = DealObservation(
                source="mercadolibre", external_id="R1", title="Audífonos", price_minor=119_900,
                original_price_minor=189_900, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                currency="COP", url="https://example.com/r1",
            )
            path = ReelRenderer(Path(directory)).render_reel(deal, "r1", product_image_bytes=buffer.getvalue())
            info = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height:format=duration", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertEqual(info[0], "1080,1920")
            self.assertAlmostEqual(float(info[1]), 9.0, delta=0.2)


class PromoStoryTests(unittest.TestCase):
    def test_square_image_becomes_vertical_story(self):
        from app.reel_renderer import ReelRenderer
        with tempfile.TemporaryDirectory() as directory:
            square = Path(directory) / "promo.png"
            Image.new("RGB", (1080, 1080), "white").save(square)
            path = ReelRenderer(Path(directory)).render_story_from_image(square, "s", "PROMO DEL DÍA: LG")
            with Image.open(path) as img:
                self.assertEqual(img.size, (1080, 1920))
