from pathlib import Path
import tempfile
import unittest

import requests

from app.database import Database
from app.models import DealObservation, DiscountEvidence
from app.pipeline import OfferPipeline
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
from tests.helpers import settings_for


class FakeMetaResponse:
    ok = True
    status_code = 200

    def json(self):
        return {"id": "photo_123", "post_id": "page_456"}


class RecordingSession:
    def __init__(self, failure=None):
        self.failure = failure
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        if self.failure:
            raise self.failure
        return FakeMetaResponse()


class FixedRate:
    def usd_to_cop(self):
        return None


class SameSource:
    source_name = "test"

    def __init__(self, deal):
        self.deal = deal

    def fetch(self):
        return [self.deal]

    def revalidate(self, deal):
        return self.deal


class FakeDealSource(SameSource):
    def fetch(self):
        return [self.deal]


class PublisherPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_for(self.temp.name)
        self.db = Database(self.settings.database_url)
        self.db.initialize()
        self.deal = DealObservation(
            source="test", external_id="P", title="Producto", price_minor=80_000,
            original_price_minor=100_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="COP", url="https://example.com/P",
        )
        self.db.create_candidate(self.deal, 40)
        self.pipeline = OfferPipeline(
            self.settings, self.db, [SameSource(self.deal)], rate_provider=FixedRate()
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_candidate_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, "aprobada"):
            self.pipeline.publish(self.deal.candidate_id)

    def test_mock_publish_uses_multipart_and_records_id(self):
        self.db.decide(self.deal.candidate_id, "approved")
        session = RecordingSession()
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        post_id = self.pipeline.publish(self.deal.candidate_id, publisher)
        self.assertEqual(post_id, "page_456")
        self.assertIn("/v26.0/page/photos", session.call[0])
        self.assertIn("source", session.call[1]["files"])
        self.assertIn("caption", session.call[1]["data"])
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "published")

    def test_timeout_is_ambiguous_and_not_retried(self):
        self.db.decide(self.deal.candidate_id, "approved")
        session = RecordingSession(requests.Timeout("secret-token"))
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        with self.assertRaises(FacebookPublishError) as raised:
            self.pipeline.publish(self.deal.candidate_id, publisher)
        self.assertTrue(raised.exception.ambiguous)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "unknown")

    def test_price_change_invalidates_approval_before_meta_call(self):
        self.db.decide(self.deal.candidate_id, "approved")
        changed = DealObservation(
            source="test", external_id="P", title="Producto", price_minor=90_000,
            original_price_minor=100_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
            currency="COP", url="https://example.com/P",
        )
        self.pipeline.sources = [SameSource(changed)]
        session = RecordingSession()
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        with self.assertRaisesRegex(ValueError, "cambió"):
            self.pipeline.publish(self.deal.candidate_id, publisher)
        self.assertIsNone(session.call)
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "approved")

    def test_end_to_end_dry_run_creates_png_without_meta(self):
        fresh_db = Database(f"sqlite:///{self.temp.name}/dry.db")
        fresh_db.initialize()
        pipeline = OfferPipeline(
            self.settings, fresh_db, [FakeDealSource(self.deal)], rate_provider=FixedRate()
        )
        report = pipeline.scan()
        self.assertEqual(report.candidates_created, [self.deal.candidate_id])
        results = pipeline.dry_run(report.candidates_created)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].status, "pending")
        self.assertTrue(results[0][1].is_file())
