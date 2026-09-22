from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from app.database import Database
from app.models import DealObservation, DiscountEvidence
from app.promos import BRANDS, PromoRenderer, find_promo, promo_copy


def _deal(title, price, before, n):
    return DealObservation(
        source="alkosto", external_id=str(n), title=title, price_minor=price, original_price_minor=before,
        evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
        url=f"https://www.alkosto.com/p/{n}", image_url=f"https://cdn/{n}.webp",
    )


class FakeStore:
    def __init__(self, deals):
        self.deals = deals
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.deals


ELECTROLUX = [
    _deal("Lavadora ELECTROLUX 12 Kilos EWIW12F6USVG Gris", 1_049_900, 2_399_900, 1),
    _deal("Lavadora ELECTROLUX 9.5 Kilos EWIW95F6USVG Gris", 979_900, 2_199_900, 2),
    _deal("Lavadora ELECTROLUX 9.5 Kilos EWIW95F6USVW Blanco", 959_900, 2_099_900, 3),  # color variant of #2
    _deal("Nevera ELECTROLUX No Frost 321 L", 1_649_900, 2_749_900, 4),
    _deal("Nevera SAMSUNG 300 L", 1_000_000, 2_000_000, 5),                             # other brand
    _deal("Vinera ELECTROLUX 8 botellas", 390_000, 400_000, 6),                         # too small a discount
]


class PromoTests(unittest.TestCase):
    def test_picks_brand_of_the_day_dedupes_variants_and_filters(self):
        store = FakeStore(ELECTROLUX)
        promo = find_promo({"alkosto": store}, lambda key, days: False, day_index=0)
        self.assertEqual((promo.store, promo.brand), ("alkosto", "Electrolux"))
        titles = [d.title for d in promo.deals]
        self.assertEqual(len(titles), 3)
        self.assertEqual(sum("9.5 Kilos" in t for t in titles), 1)
        self.assertFalse(any("SAMSUNG" in t or "Vinera" in t for t in titles))
        self.assertIn("nevera Electrolux", store.queries)

    def test_skips_store_brand_posted_recently_and_needs_three_items(self):
        store = FakeStore(ELECTROLUX)
        recent = lambda key, days: key == "alkosto:electrolux"
        promo = find_promo({"alkosto": store}, recent, day_index=0)
        # Electrolux is blocked; the next brands only find the lone Samsung fridge (< 3 items).
        self.assertIsNone(promo)
        self.assertEqual(len(BRANDS), 8)

    def test_copy_lists_every_product_with_price_and_link(self):
        promo = find_promo({"alkosto": FakeStore(ELECTROLUX)}, lambda k, d: False, day_index=0)
        text = promo_copy(promo)
        self.assertTrue(text.startswith("🔥 Promo del día en Alkosto: Electrolux hasta -56%"))
        for d in promo.deals:
            self.assertIn(d.url, text)
        self.assertIn("#alkosto #electrolux", text)

    def test_render_is_1080_square_png(self):
        buffer = BytesIO()
        Image.new("RGB", (200, 200), "gray").save(buffer, format="PNG")

        class Offline(PromoRenderer):
            def _download(self, url):
                return buffer.getvalue()

        promo = find_promo({"alkosto": FakeStore(ELECTROLUX)}, lambda k, d: False, day_index=0)
        with tempfile.TemporaryDirectory() as directory:
            path = Offline(Path(directory)).render_promo(promo, "p")
            with Image.open(path) as img:
                self.assertEqual(img.size, (1080, 1080))

    def test_database_tracks_daily_and_recent_promos(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/promo.db")
            db.initialize()
            self.assertFalse(db.promo_published_today())
            db.record_promo("alkosto:electrolux", "post_1")
            self.assertTrue(db.promo_published_today())
            self.assertTrue(db.promo_posted_within("alkosto:electrolux", 7))
            self.assertFalse(db.promo_posted_within("exito:electrolux", 7))
