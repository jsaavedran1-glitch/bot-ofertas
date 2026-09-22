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
