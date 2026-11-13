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
                self.assertEqual(output.size, (1080, 1350))
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


class InsightTests(unittest.TestCase):
    def deal(self, **extra):
        return DealObservation(
            source="alkosto", external_id="I1", title="Audífonos Sony", price_minor=349_900,
            original_price_minor=999_900, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="COP", url="https://example.com/i1", **extra,
        )

    def test_rating_shows_only_when_well_rated_with_enough_reviews(self):
        from app.models import rating_text
        self.assertEqual(rating_text(self.deal(rating=4.85, reviews=1648)), "4,8 (1.648 opiniones)")
        self.assertIsNone(rating_text(self.deal(rating=4.9, reviews=3)))
        self.assertIsNone(rating_text(self.deal(rating=3.6, reviews=500)))
        self.assertIsNone(rating_text(self.deal()))

    def test_caption_mentions_lowest_price_and_rating_only_when_true(self):
        text = facebook_copy(self.deal(rating=4.8, reviews=85, lowest_30d=True), None, True)
        self.assertIn("precio más bajo", text.lower())
        self.assertIn("⭐ 4,8 (85 opiniones) en Alkosto", text)
        plain = facebook_copy(self.deal(), None, True)
        self.assertNotIn("más bajo", plain.lower())
        self.assertNotIn("⭐", plain)

    def test_reel_opens_with_lowest_price_and_big_tickets_with_savings(self):
        from app.reel_renderer import reel_hook
        self.assertEqual(reel_hook(self.deal(lowest_30d=True), "$349.900")[1], ["PRECIO MÁS BAJO", "EN 30 DÍAS"])
        self.assertIn("$650.000", " ".join(reel_hook(self.deal(), "$349.900", "$650.000")[1]))

    def test_reel_frame_keeps_the_9_16_canvas(self):
        from app.reel_renderer import ReelRenderer
        buffer = BytesIO()
        Image.new("RGB", (300, 300), "red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            frame = ReelRenderer(Path(directory)).render_frame(
                self.deal(rating=4.8, reviews=85, lowest_30d=True), 4.9, product_image_bytes=buffer.getvalue())
            self.assertEqual(frame.size, (1080, 1920))


class DesignTests(unittest.TestCase):
    def test_image_title_drops_model_codes_metric_duplicates_and_color(self):
        from app.models import display_title
        self.assertEqual(display_title('TV SAMSUNG 55" Pulgadas 139,7 cm 55U8000H 4K-UHD LED Smart TV Negro'),
                         'TV SAMSUNG 55" 4K-UHD LED Smart TV')
        self.assertEqual(display_title("Consola PS5 Slim 1TB"), "Consola PS5 Slim 1TB")
        self.assertEqual(display_title("Parlante JBL PARTYBOX 130 200W Negro"), "Parlante JBL PARTYBOX 130 200W")

    def test_product_photo_margin_is_trimmed_and_backdrop_whitened(self):
        from app.image_renderer import trim_product
        photo = Image.new("RGB", (750, 750), (253, 253, 253))
        photo.paste((20, 20, 20), (300, 200, 450, 550))
        trimmed = trim_product(photo)
        self.assertLess(trimmed.width, 200)
        self.assertLess(trimmed.height, 400)
        self.assertEqual(trimmed.getpixel((0, 0))[:3], (255, 255, 255))
        full_bleed = Image.new("RGB", (400, 400), (150, 150, 150))
        self.assertEqual(trim_product(full_bleed).size, (400, 400))

    def test_long_unbreakable_title_is_wrapped_with_ellipsis(self):
        from PIL import ImageDraw
        renderer = OfferImageRenderer(Path("."))
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font, lines = renderer._fit_lines(draw, "TV SAMSUNG 65 pulgadas Mini LED Smart TV con IA y barra de sonido incluida",
                                          240, max_lines=2, start=22, minimum=20)
        self.assertEqual(len(lines), 2)
        self.assertGreater(len(lines[0].split()), 1)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(draw.textlength(line, font=font) <= 240 for line in lines))

    def test_top_cover_is_4_5_and_slides_take_a_rank_tag(self):
        buffer = BytesIO()
        Image.new("RGB", (300, 300), "red").save(buffer, format="PNG")

        class Offline(OfferImageRenderer):
            def _download(self, url):
                return buffer.getvalue()

        deals = [DealObservation(source="alkosto", external_id=f"T{i}", title=f"Producto {i}", price_minor=100_000,
                                 original_price_minor=200_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                 currency="COP", url=f"https://example.com/t{i}", image_url="https://img/x")
                 for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            renderer = Offline(Path(directory))
            with Image.open(renderer.render_top_cover(deals, "cover")) as cover:
                self.assertEqual(cover.size, (1080, 1350))
            with Image.open(renderer.render(deals[0], "slide", tag="TOP 1 DE HOY")) as slide:
                self.assertEqual(slide.size, (1080, 1350))


class ReelAudioTests(unittest.TestCase):
    def test_effects_follow_the_animation_and_mix_over_the_music(self):
        import shutil, subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg no instalado")
        import app.reel_renderer as rr
        buffer = BytesIO()
        Image.new("RGB", (300, 300), "red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            sfx = Path(directory) / "efectos"
            sfx.mkdir()
            for name, freq in (("pop", 300), ("monedas", 2000), ("otro", 500)):
                subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.3",
                                str(sfx / f"{name}.wav")], check=True)
            cues = rr.sfx_cues(sfx)
            self.assertEqual([(p.stem, t) for p, t in cues], [("pop", 0.3), ("monedas", 3.0)])
            self.assertIn("adelay=delays=3000", " ".join(rr.audio_args(None, cues)))
            deal = DealObservation(source="alkosto", external_id="S1", title="Parlante", price_minor=45_900,
                                   original_price_minor=119_900, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                   currency="COP", url="https://example.com/s1")
            renderer = rr.ReelRenderer(Path(directory))
            renderer.sfx_dir = sfx
            path = renderer.render_reel(deal, "s1", product_image_bytes=buffer.getvalue())
            out = subprocess.run(["ffmpeg", "-i", str(path), "-af", "atrim=2.9:3.4,volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            self.assertGreater(float(out.split("max_volume:")[1].split("dB")[0]), -30)  # the coins cue sounds at 3 s


class PromoStoryTests(unittest.TestCase):
    def test_square_image_becomes_vertical_story(self):
        from app.reel_renderer import ReelRenderer
        with tempfile.TemporaryDirectory() as directory:
            square = Path(directory) / "promo.png"
            Image.new("RGB", (1080, 1080), "white").save(square)
            path = ReelRenderer(Path(directory)).render_story_from_image(square, "s", "PROMO DEL DÍA: LG")
            with Image.open(path) as img:
                self.assertEqual(img.size, (1080, 1920))


class TrendTagTests(unittest.TestCase):
    def test_three_trending_tags_vary_per_post_and_are_optional(self):
        import os
        from unittest import mock
        from app.copywriter import trend_tags
        with mock.patch.dict(os.environ, {"TRENDING_TAGS": ""}):
            self.assertEqual(trend_tags("X"), "")
        with mock.patch.dict(os.environ, {"TRENDING_TAGS": "descuento, #combo,regalo,Navidad,amor y amistad"}):
            tags = trend_tags("A").split()
            self.assertEqual(len(tags), 3)
            self.assertTrue(all(t.startswith("#") and "##" not in t for t in tags))
            self.assertIn("#amoryamistad", {t for seed in "ABCDEFGHIJ" for t in trend_tags(seed).split()})
            self.assertGreater(len({trend_tags(seed) for seed in "ABCDEFGHIJ"}), 1)


class ReelMusicTests(unittest.TestCase):
    def test_reel_uses_a_music_track_when_one_exists(self):
        import shutil, subprocess
        from unittest import mock
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg no instalado")
        import app.reel_renderer as rr
        buffer = BytesIO()
        Image.new("RGB", (300, 300), "red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            music = Path(directory) / "music"
            music.mkdir()
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                            str(music / "tono.wav")], check=True)
            self.assertEqual(rr.music_track("X", music), music / "tono.wav")
            self.assertIsNone(rr.music_track("X", Path(directory) / "vacia"))
            deal = DealObservation(
                source="alkosto", external_id="M1", title="Parlante", price_minor=45_900,
                original_price_minor=119_900, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                currency="COP", url="https://example.com/m1",
            )
            with mock.patch.object(rr, "MUSIC_DIR", music):
                path = rr.ReelRenderer(Path(directory)).render_reel(deal, "m1", product_image_bytes=buffer.getvalue())
            out = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            mean = float(out.split("mean_volume:")[1].split("dB")[0])
            self.assertGreater(mean, -40)  # silence would be about -91 dB
