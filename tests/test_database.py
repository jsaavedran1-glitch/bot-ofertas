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

    def test_lowest_price_needs_two_weeks_of_history_and_a_real_drop(self):
        now = datetime.now(timezone.utc)
        for days, price in ((10, 95_000), (5, 90_000)):
            self.db.record_observation(self.deal(price=price, observed_at=now - timedelta(days=days)))
        self.assertFalse(self.db.is_lowest_price(self.deal(price=80_000)))  # only 10 days tracked
        self.db.record_observation(self.deal(price=99_000, observed_at=now - timedelta(days=20)))
        self.assertTrue(self.db.is_lowest_price(self.deal(price=80_000)))
        self.assertFalse(self.db.is_lowest_price(self.deal(price=91_000)))  # 90.000 seen 5 days ago

    def test_rating_is_stored_with_the_candidate(self):
        from dataclasses import replace
        deal = replace(self.deal(), rating=4.7, reviews=120)
        self.db.create_candidate(deal, 40)
        stored = [c for c in self.db.list_candidates("pending") if c.deal.external_id == "A"][0].deal
        self.assertEqual((stored.rating, stored.reviews), (4.7, 120))

    def test_private_audio_round_trips_through_the_database(self):
        from pathlib import Path
        self.db.put_media("pista.m4a", "musica", b"abc")
        self.db.put_media("pista.m4a", "musica", b"abcd")  # replacing keeps one copy
        self.db.put_media("pop.m4a", "efectos", b"xy")
        folders = self.db.export_media(Path(self.temp.name) / "media")
        self.assertEqual((folders["musica"] / "pista.m4a").read_bytes(), b"abcd")
        self.assertEqual((folders["efectos"] / "pop.m4a").read_bytes(), b"xy")

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
    def test_pattern_prioritizes_amazon_and_skips_mercadolibre(self):
        import tempfile
        from main import publish_queues
        from app.models import DealObservation, DiscountEvidence
        with tempfile.TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/q.db")
            db.initialize()
            ids = {}
            for source in ("mercadolibre", "amazon", "woot", "exito"):
                deal = DealObservation(
                    source=source, external_id=f"{source}{len(ids)}", title=f"Producto {source}", price_minor=5000,
                    original_price_minor=10000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                    currency="USD", url="https://example.com", image_url="https://example.com/i.png",
                )
                db.create_candidate(deal, 50)
                db.decide(deal.candidate_id, "approved")
                ids.setdefault(source, []).append(deal.candidate_id)

            def order(queues):
                return [db.get_candidate(q[0]).deal.source for q in queues]

            # Mercado Libre and Woot are no longer published
            self.assertEqual(order(publish_queues(db)), ["amazon", "exito"])
            self.assertEqual(order(publish_queues(db, as_reel=True)), ["amazon", "exito"])
            cid = ids["amazon"][0]
            db.reserve_for_publish(cid, 15)
            db.record_publication(cid, "post_0")
            self.assertEqual(order(publish_queues(db)), ["exito"])


class VariantTests(unittest.TestCase):
    def test_colours_and_stores_collapse_but_sizes_do_not(self):
        from app.models import variant_key
        sony = "Audífonos de Diadema SONY Inalámbricos Bluetooth On Ear WH-CH520 {}"
        self.assertEqual(len({variant_key(sony.format(c)) for c in ("Negro", "Azul", "Rosado", "Crema")}), 1)
        self.assertEqual(variant_key("Reloj KALLEY K-SWR4 35mm Rosado|Blanco"), variant_key("Reloj KALLEY K-SWR4 35mm Negro"))
        self.assertNotEqual(variant_key("Freidora de Aire KALLEY 3.5Litros K-MAF35 Negro"),
                            variant_key("Freidora de Aire KALLEY 4.5 Litros K-MAF45 Negro"))

    def test_queue_keeps_one_variant_and_skips_recently_posted_products(self):
        from main import publish_queues
        with tempfile.TemporaryDirectory() as directory:
            db = Database(f"sqlite:///{directory}/v.db")
            db.initialize()
            title = "Audífonos SONY WH-CH520 {}"
            deals = [
                DealObservation(source=src, external_id=f"{src}{color}", title=title.format(color), price_minor=price,
                                original_price_minor=200_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                currency="COP", url=f"https://example.com/{src}{color}")
                for src, color, price in (("alkosto", "Negro", 90_000), ("ktronix", "Azul", 95_000), ("alkosto", "Rosado", 99_000))
            ]
            other = DealObservation(source="ktronix", external_id="F1", title="Freidora KALLEY 3.5 L", price_minor=99_000,
                                    original_price_minor=200_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                    currency="COP", url="https://example.com/F1")
            for deal, score in zip(deals + [other], (60, 50, 40, 30)):
                db.create_candidate(deal, score)
                db.decide(deal.candidate_id, "approved")
            queued = [cid for queue in publish_queues(db) for cid in queue]
            self.assertEqual(sorted(queued), sorted([deals[0].candidate_id, other.candidate_id]))
            # Once one colour is posted, the others stay out of the queue for the cooldown.
            db.reserve_for_publish(deals[0].candidate_id, 10)
            db.record_publication(deals[0].candidate_id, "post_1")
            queued = [cid for queue in publish_queues(db) for cid in queue]
            self.assertEqual(queued, [other.candidate_id])

    def test_top_five_never_repeats_a_product(self):
        from app.models import Candidate
        from app.top5 import pick_top
        def cand(i, src, title):
            deal = DealObservation(source=src, external_id=str(i), title=title, price_minor=1000 + i,
                                   original_price_minor=5000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                                   currency="COP", url=f"https://example.com/{i}")
            from app.models import utc_now
            return Candidate(id=deal.candidate_id, deal=deal, score=100 - i, status="approved", created_at=utc_now())
        picked = pick_top([cand(0, "alkosto", "Parlante JBL Go 4 Negro"), cand(1, "ktronix", "Parlante JBL Go 4 Azul"),
                           cand(2, "amazon", "Echo Dot")])
        self.assertEqual([c.deal.title for c in picked], ["Parlante JBL Go 4 Negro", "Echo Dot"])
