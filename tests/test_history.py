import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.history import HistoryRenderer, build_history, history_caption, judge
from app.models import DealObservation, DiscountEvidence
from app.quality import caption_issues, names_of

NOW = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)


def deal(price, original=None, evidence=DiscountEvidence.OFFICIAL_ORIGINAL, **kw):
    return DealObservation(source="falabella", external_id="H1", title="Freidora de aire Oster 5 L", price_minor=price,
                           original_price_minor=original, evidence=evidence, currency="COP",
                           url="https://example.com/h1", image_url="https://example.com/h1.png", observed_at=NOW, **kw)


def rows(prices, days_back=28):
    return [(NOW - timedelta(days=days_back - i), p) for i, p in enumerate(prices) if p]


STEADY = [340_000, 339_000, None, 345_000, 340_000, 338_000, None, 340_000, 342_000, 340_000, 339_000, 341_000, 340_000]


class VerdictTests(unittest.TestCase):
    def test_too_little_history_says_nothing(self):
        self.assertIsNone(build_history(rows([340_000] * 5), deal(300_000, 500_000)))
        self.assertIsNone(build_history(rows([340_000] * 6, days_back=6), deal(300_000, 500_000)))
        self.assertIsNotNone(build_history(rows([340_000] * 7, days_back=7), deal(300_000, 500_000)))

    def test_inflated_antes(self):
        d = deal(339_900, 599_900)
        h = build_history(rows(STEADY), d)
        v = judge(d, h)
        self.assertEqual(v.kind, "inflado")
        self.assertIn("$599.900", v.detail)

    def test_real_discount_is_the_lowest_and_well_below_usual(self):
        d = deal(279_900, 599_900)
        v = judge(d, build_history(rows(STEADY), d))
        self.assertEqual(v.kind, "real")

    def test_good_price_below_usual_but_not_the_lowest(self):
        prices = STEADY[:-1] + [270_000]
        d = deal(300_000, 349_900)
        v = judge(d, build_history(rows(prices), d))
        self.assertEqual(v.kind, "bueno")

    def test_ordinary_price_gets_no_post(self):
        d = deal(340_000, 360_000)
        self.assertIsNone(judge(d, build_history(rows(STEADY), d)))

    def test_history_discounts_never_claim_inflation(self):
        d = deal(339_900, 599_900, evidence=DiscountEvidence.OWN_HISTORY)
        self.assertIsNone(judge(d, build_history(rows(STEADY), d)))

    def test_caption_is_factual_and_recommendable(self):
        d = deal(339_900, 599_900, affiliate=True)
        h = build_history(rows(STEADY), d)
        caption = history_caption(d, h, judge(d, h), link_in_comment=True)
        self.assertIn("Precio habitual: $340.000 COP", caption)
        self.assertIn("'Antes' según la tienda: $599.900 COP", caption)
        self.assertIn("comisión", caption)
        self.assertEqual(caption_issues(caption, names_of([d])), [])


class RenderTests(unittest.TestCase):
    def test_renders_a_4_5_card(self):
        buffer = BytesIO()
        Image.new("RGB", (600, 600), "white").save(buffer, format="PNG")
        d = deal(279_900, 599_900)
        h = build_history(rows(STEADY), d)
        with tempfile.TemporaryDirectory() as directory:
            renderer = HistoryRenderer(Path(directory))
            path = renderer.render_history(d, h, judge(d, h), "h", product_image_bytes=buffer.getvalue())
            with Image.open(path) as img:
                self.assertEqual(img.size, (1080, 1350))


class PipelineTests(unittest.TestCase):
    def test_publishes_the_history_post_and_marks_the_deal(self):
        from app.database import Database
        from app.pipeline import OfferPipeline
        from app.publishers.facebook import FacebookPublisher
        from tests.helpers import settings_for
        from tests.test_publisher_pipeline import FixedRate, PhotoRenderer, RecordingSession, SameSource

        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(directory)
            db = Database(settings.database_url)
            db.initialize()
            from app.models import utc_now
            d = DealObservation(source="test", external_id="H1", title="Freidora de aire Oster 5 L",
                                price_minor=339_900, original_price_minor=599_900,
                                evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
                                url="https://example.com/h1", image_url="https://example.com/h1.png")
            for i, price in enumerate([p for p in STEADY if p]):
                from dataclasses import replace
                db.record_observation(replace(d, price_minor=price, observed_at=utc_now() - timedelta(days=26 - 2 * i)))
            db.create_candidate(d, 40)
            db.decide(d.candidate_id, "approved")
            pipeline = OfferPipeline(settings, db, [SameSource(d)], rate_provider=FixedRate(),
                                     renderer=PhotoRenderer(settings.generated_dir))

            class Renderer(HistoryRenderer):
                def _download(self, url):
                    buffer = BytesIO()
                    Image.new("RGB", (600, 600), "white").save(buffer, format="PNG")
                    return buffer.getvalue()

            pipeline.history_renderer = Renderer(settings.generated_dir)
            session = RecordingSession()
            result = pipeline.publish_history(db.list_candidates(status="approved"),
                                              FacebookPublisher("page", "t", "v26.0", session))
            self.assertIsNotNone(result)
            self.assertIn("¿Es real el descuento", session.call[1]["data"]["caption"])
            self.assertEqual(db.get_candidate(d.candidate_id).status, "published")
            self.assertTrue(db.history_published_this_hour())


if __name__ == "__main__":
    unittest.main()


class TrackingTests(unittest.TestCase):
    def test_recent_deals_get_one_price_check_a_day(self):
        from app.database import Database
        from app.pipeline import OfferPipeline
        from tests.helpers import settings_for
        from tests.test_publisher_pipeline import FixedRate, PhotoRenderer, SameSource

        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(directory)
            db = Database(settings.database_url)
            db.initialize()
            d = DealObservation(source="test", external_id="T1", title="Licuadora Oster", price_minor=199_900,
                                original_price_minor=299_900, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                currency="COP", url="https://example.com/t1", image_url="https://example.com/t1.png",
                                observed_at=datetime.now(timezone.utc) - timedelta(days=2))
            db.record_observation(d)
            db.create_candidate(d, 30)
            pipeline = OfferPipeline(settings, db, [SameSource(d)], rate_provider=FixedRate(),
                                     renderer=PhotoRenderer(settings.generated_dir))
            self.assertEqual(pipeline.track_prices(10), 1)
            self.assertEqual(len(db.price_series(d)), 2)
            self.assertEqual(pipeline.track_prices(10), 0)  # already checked today
