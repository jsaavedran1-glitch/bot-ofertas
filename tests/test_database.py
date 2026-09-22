from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from app.database import Database
from app.models import DealObservation, DiscountEvidence


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{self.temp.name}/offers.db")
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def deal(self, external_id="A", price=80_000, observed_at=None):
        return DealObservation(
            source="test", external_id=external_id, title="Oferta",
            price_minor=price, original_price_minor=100_000,
            evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
            url=f"https://example.com/{external_id}",
            observed_at=observed_at or datetime.now(timezone.utc),
        )

    def test_candidate_deduplicates_and_claims_once(self):
        deal = self.deal()
        self.assertTrue(self.db.create_candidate(deal, 40))
        self.assertFalse(self.db.create_candidate(deal, 40))
        self.assertTrue(self.db.decide(deal.candidate_id, "approved"))
        self.assertTrue(self.db.reserve_for_publish(deal.candidate_id, 2))
        self.assertFalse(self.db.reserve_for_publish(deal.candidate_id, 2))

    def test_daily_limit_counts_reserved_candidates(self):
        first, second = self.deal("A"), self.deal("B")
        for deal in (first, second):
            self.db.create_candidate(deal, 40)
            self.db.decide(deal.candidate_id, "approved")
        self.assertTrue(self.db.reserve_for_publish(first.candidate_id, 1))
        self.assertFalse(self.db.reserve_for_publish(second.candidate_id, 1))

    def test_history_needs_distinct_days_and_span(self):
        now = datetime.now(timezone.utc)
        current = self.deal("H", 70_000, now)
        for days, price in ((3, 100_000), (2, 90_000), (1, 95_000)):
            previous = DealObservation(
                source="test", external_id="H", title="Histórico", price_minor=price,
                currency="COP", url="https://example.com/H",
                observed_at=now - timedelta(days=days),
            )
            self.db.record_observation(previous)
        self.assertEqual(self.db.historical_reference(current, 3, 48), 95_000)

    def test_known_failure_can_be_manually_reapproved(self):
        deal = self.deal()
        self.db.create_candidate(deal, 40)
        self.db.decide(deal.candidate_id, "approved")
        self.db.reserve_for_publish(deal.candidate_id, 2)
        self.db.record_publish_error(deal.candidate_id, "fallo", ambiguous=False)
        self.assertEqual(self.db.get_candidate(deal.candidate_id).status, "failed")
        self.assertTrue(self.db.decide(deal.candidate_id, "approved"))


class PublishQueueTests(unittest.TestCase):
    def test_allowed_sources_and_reels_only_mercadolibre_amazon(self):
        import tempfile
        from main import publish_queues
        from app.models import DealObservation, DiscountEvidence
        with tempfile.TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/q.db")
            db.initialize()
            ids = {}
            for source in ("mercadolibre", "amazon", "woot", "exito"):
                deal = DealObservation(
                    source=source, external_id=f"{source}{len(ids)}", title="X", price_minor=5000,
                    original_price_minor=10000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                    currency="USD", url="https://example.com", image_url="https://example.com/i.png",
                )
                db.create_candidate(deal, 50)
                db.decide(deal.candidate_id, "approved")
                ids.setdefault(source, []).append(deal.candidate_id)

            def sources(queues):
                return {db.get_candidate(q[0]).deal.source for q in queues}

            self.assertEqual(sources(publish_queues(db)), {"mercadolibre", "amazon", "exito"})
            self.assertEqual(sources(publish_queues(db, as_reel=True)), {"mercadolibre", "amazon"})
