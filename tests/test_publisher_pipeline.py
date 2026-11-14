from pathlib import Path
import tempfile
import unittest

import requests

from io import BytesIO

from PIL import Image

from app.database import Database
from app.image_renderer import OfferImageRenderer
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
        self.calls = []

    def post(self, url, **kwargs):
        if self.call is None:
            self.call = (url, kwargs)
        self.calls.append((url, kwargs))
        if self.failure:
            raise self.failure
        return FakeMetaResponse()


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (400, 400), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class PhotoRenderer(OfferImageRenderer):
    def __init__(self, output_dir, photo=True):
        super().__init__(output_dir)
        self.photo = photo

    def _download(self, url):
        return _png() if self.photo else None


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
            currency="COP", url="https://example.com/P", image_url="https://example.com/P.png",
        )
        self.db.create_candidate(self.deal, 40)
        self.pipeline = OfferPipeline(
            self.settings, self.db, [SameSource(self.deal)], rate_provider=FixedRate(),
            renderer=PhotoRenderer(self.settings.generated_dir),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_candidate_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, "aprobada"):
            self.pipeline.publish(self.deal.candidate_id)

    def test_mock_publish_uses_multipart_and_records_id(self):
        from dataclasses import replace
        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.settings = replace(self.settings, link_in_comment=True)
        session = RecordingSession()
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        post_id = self.pipeline.publish(self.deal.candidate_id, publisher)
        self.assertEqual(post_id, "page_456")
        self.assertIn("/v26.0/page/photos", session.call[0])
        self.assertIn("source", session.call[1]["files"])
        self.assertIn("caption", session.call[1]["data"])
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "published")
        self.assertNotIn("example.com/P", session.call[1]["data"]["caption"])
        comment_url, comment = session.calls[1]
        self.assertIn("/v26.0/page_456/comments", comment_url)
        self.assertIn("https://example.com/P", comment["data"]["message"])
        self.assertEqual(self.db.posts_today(), 1)

    def test_reseen_candidate_is_refreshed_and_stale_ones_expire(self):
        from dataclasses import replace
        from datetime import timedelta
        from app.models import utc_now
        self.db.decide(self.deal.candidate_id, "approved")
        old = replace(self.deal, observed_at=utc_now() - timedelta(hours=10))
        other = replace(old, external_id="Q")
        self.db.create_candidate(other, 40)
        self.db.decide(other.candidate_id, "approved")
        self.assertFalse(self.db.create_candidate(replace(self.deal, observed_at=utc_now()), 40))
        self.assertEqual(self.db.expire_stale(6), 1)
        self.assertEqual(self.db.get_candidate(other.candidate_id).status, "expired")
        # Seen again by a later scan: back to pending so automatic mode approves it.
        self.db.create_candidate(replace(other, observed_at=utc_now()), 40)
        self.assertEqual(self.db.get_candidate(other.candidate_id).status, "pending")
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "approved")

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
            currency="COP", url="https://example.com/P", image_url="https://example.com/P.png",
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
            self.settings, fresh_db, [FakeDealSource(self.deal)], rate_provider=FixedRate(),
            renderer=PhotoRenderer(self.settings.generated_dir),
        )
        report = pipeline.scan()
        self.assertEqual(report.candidates_created, [self.deal.candidate_id])
        results = pipeline.dry_run(report.candidates_created)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].status, "pending")
        self.assertTrue(results[0][1].is_file())

    def test_deal_without_photo_is_not_published_nor_counted(self):
        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.renderer = PhotoRenderer(self.settings.generated_dir, photo=False)
        session = RecordingSession()
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        with self.assertRaisesRegex(ValueError, "foto"):
            self.pipeline.publish(self.deal.candidate_id, publisher)
        self.assertIsNone(session.call)
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "failed")

    def test_scan_skips_deals_without_image_url(self):
        from dataclasses import replace
        fresh_db = Database(f"sqlite:///{self.temp.name}/noimg.db")
        fresh_db.initialize()
        no_photo = replace(self.deal, external_id="NOIMG", image_url="")
        pipeline = OfferPipeline(self.settings, fresh_db, [FakeDealSource(no_photo)], rate_provider=FixedRate())
        self.assertEqual(pipeline.scan().candidates_created, [])


class ReelSession:
    """Answers the three Reels calls: start, binary upload, finish."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = FakeMetaResponse()
        if "upload_phase" in str(kwargs.get("data")) and kwargs["data"].get("upload_phase") == "start":
            response.json = lambda: {"video_id": "vid_789"}
        else:
            response.json = lambda: {"success": True}
        return response


class FakeReelRenderer:
    def __init__(self, directory):
        self.directory = Path(directory)

    def render_reel(self, deal, name, usd_cop_rate=None, link_in_comment=False, product_image_bytes=None):
        path = self.directory / f"{name}.mp4"
        path.write_bytes(b"fake-mp4")
        return path

    def render_story(self, deal, name, usd_cop_rate=None, product_image_bytes=None):
        path = self.directory / f"{name}.png"
        path.write_bytes(b"fake-png")
        return path


class ReelPublishTests(unittest.TestCase):
    setUp = PublisherPipelineTests.setUp
    tearDown = PublisherPipelineTests.tearDown

    def test_reel_uses_three_phase_upload_and_is_recorded_as_reel(self):
        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.reel_renderer = FakeReelRenderer(self.temp.name)
        session = ReelSession()
        publisher = FacebookPublisher("page", "secret-token", "v26.0", session)
        self.assertFalse(self.db.reel_published_this_hour())
        video_id = self.pipeline.publish(self.deal.candidate_id, publisher, as_reel=True)
        self.assertEqual(video_id, "vid_789")
        start, upload, finish = session.calls
        self.assertIn("/page/video_reels", start[0])
        self.assertIn("rupload.facebook.com/video-upload/v26.0/vid_789", upload[0])
        self.assertEqual(upload[1]["headers"]["file_size"], "8")
        self.assertEqual(finish[1]["data"]["upload_phase"], "finish")
        self.assertIn("#reels", finish[1]["data"]["description"])
        self.assertTrue(self.db.reel_published_this_hour())
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "published")


class SourceSavingsFloorTests(unittest.TestCase):
    def test_source_can_lower_min_savings_for_groceries(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(directory)
            db = Database(settings.database_url)
            db.initialize()
            coffee = DealObservation(
                source="exito", external_id="CAFE", title="Café 454 g", price_minor=15_000,
                original_price_minor=20_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
                currency="COP", url="https://example.com/cafe", image_url="https://example.com/cafe.png",
            )
            strict = FakeDealSource(coffee)
            self.assertEqual(OfferPipeline(settings, db, [strict], rate_provider=FixedRate()).scan().candidates_created, [])
            relaxed = FakeDealSource(replace(coffee, external_id="CAFE2"))
            relaxed.min_savings_cop = 3000
            self.assertEqual(len(OfferPipeline(settings, db, [relaxed], rate_provider=FixedRate()).scan().candidates_created), 1)


class StorySession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = FakeMetaResponse()
        data = kwargs.get("data") or {}
        if url.endswith("/photo_stories"):
            response.json = lambda: {"success": True, "post_id": "story_9"}
        elif data.get("published") == "false":
            response.json = lambda: {"id": "unpublished_1"}
        return response


class StoryTests(unittest.TestCase):
    setUp = PublisherPipelineTests.setUp
    tearDown = PublisherPipelineTests.tearDown

    def _publish(self, renderer):
        from dataclasses import replace
        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.settings = replace(self.settings, stories=True)
        self.pipeline.reel_renderer = renderer
        session = StorySession()
        post_id = self.pipeline.publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", session))
        return post_id, session

    def test_photo_post_is_followed_by_a_story(self):
        post_id, session = self._publish(FakeReelRenderer(self.temp.name))
        self.assertEqual(post_id, "page_456")
        urls = [u for u, _ in session.calls]
        self.assertTrue(urls[0].endswith("/page/photos"))
        self.assertEqual(session.calls[1][1]["data"], {"published": "false"})
        self.assertTrue(urls[2].endswith("/page/photo_stories"))
        self.assertEqual(session.calls[2][1]["data"], {"photo_id": "unpublished_1"})

    def test_story_failure_does_not_undo_the_post(self):
        from app.image_renderer import MissingProductImage

        class Broken(FakeReelRenderer):
            def render_story(self, *args, **kwargs):
                raise MissingProductImage("sin foto")

        post_id, session = self._publish(Broken(self.temp.name))
        self.assertEqual(post_id, "page_456")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "published")


class VideoStoryTests(unittest.TestCase):
    setUp = PublisherPipelineTests.setUp
    tearDown = PublisherPipelineTests.tearDown

    def test_reel_is_mirrored_as_video_story(self):
        from dataclasses import replace
        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.settings = replace(self.settings, stories=True)
        self.pipeline.reel_renderer = FakeReelRenderer(self.temp.name)
        session = ReelSession()
        self.pipeline.publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", session), as_reel=True)
        urls = [u for u, _ in session.calls]
        self.assertEqual(sum(u.endswith("/page/video_reels") for u in urls), 2)   # reel start + finish
        self.assertEqual(sum(u.endswith("/page/video_stories") for u in urls), 2)  # story start + finish
        self.assertEqual(sum("rupload.facebook.com" in u for u in urls), 2)

    def test_minutes_since_last_post(self):
        self.assertIsNone(self.db.minutes_since_last_post())
        self.db.record_promo("alkosto:lg", "post_9")
        self.assertLess(self.db.minutes_since_last_post(), 1)


class CarouselSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = FakeMetaResponse()
        n = len(self.calls)
        if url.endswith("/feed"):
            response.json = lambda: {"id": "page_top_1"}
        elif (kwargs.get("data") or {}).get("published") == "false":
            response.json = lambda n=n: {"id": f"photo_{n}"}
        else:
            response.json = lambda: {"id": "c1"}
        return response


class TopFiveTests(unittest.TestCase):
    setUp = PublisherPipelineTests.setUp
    tearDown = PublisherPipelineTests.tearDown

    def _approved(self, n, source):
        from dataclasses import replace
        ids = []
        for i in range(n):
            d = replace(self.deal, source=source, external_id=f"{source}{i}", price_minor=50_000 + i,
                        title=f"Producto {source} {i}")
            self.db.create_candidate(d, 90 - i)
            self.db.decide(d.candidate_id, "approved")
            ids.append(d.candidate_id)
        return ids

    def test_pick_top_caps_two_per_store(self):
        from app.top5 import pick_top
        self._approved(4, "alkosto")
        self._approved(4, "amazon")
        self._approved(1, "ktronix")
        picked = pick_top(self.db.list_candidates("approved"))
        self.assertEqual(len(picked), 5)
        sources = [c.deal.source for c in picked]
        self.assertTrue(all(sources.count(s) <= 2 for s in set(sources)))

    def test_carousel_posts_once_with_attached_photos_and_marks_published(self):
        from dataclasses import replace

        class AnySource(SameSource):
            source_name = "alkosto"
            source_names = {"alkosto", "amazon", "ktronix"}

            def revalidate(self, deal):
                return deal

        ids = self._approved(2, "alkosto") + self._approved(2, "amazon") + self._approved(1, "ktronix")
        self.pipeline.sources = [AnySource(self.deal)]
        self.pipeline.settings = replace(self.settings, link_in_comment=True)
        session = CarouselSession()
        post_id, items = self.pipeline.publish_top(
            self.db.list_candidates("approved"), FacebookPublisher("page", "t", "v26.0", session)
        )
        self.assertEqual(post_id, "page_top_1")
        self.assertEqual(len(items), 5)
        feed = [kw for u, kw in session.calls if u.endswith("/feed")]
        self.assertEqual(len(feed), 1)
        self.assertEqual(sum(k.startswith("attached_media") for k in feed[0]["data"]), 6)  # cover + 5
        self.assertIn("TOP 5", feed[0]["data"]["message"])
        self.assertTrue(any(u.endswith("/page_top_1/comments") for u, _ in session.calls))
        self.assertTrue(self.db.top_published_today())
        self.assertTrue(all(self.db.get_candidate(i).status == "published" for i in ids))

    def test_too_few_deals_publishes_nothing(self):
        self._approved(2, "alkosto")
        session = CarouselSession()
        self.assertIsNone(self.pipeline.publish_top(self.db.list_candidates("approved"), FacebookPublisher("page", "t", "v26.0", session)))
        self.assertEqual(session.calls, [])


class TelegramSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeMetaResponse()


class TelegramTests(unittest.TestCase):
    setUp = PublisherPipelineTests.setUp
    tearDown = PublisherPipelineTests.tearDown

    def test_published_deal_is_mirrored_to_telegram_with_link(self):
        from app.publishers.telegram import TelegramPublisher
        self.db.decide(self.deal.candidate_id, "approved")
        tg = TelegramSession()
        self.pipeline.telegram = TelegramPublisher("SECRET-BOT-TOKEN", "@ojoalprecio", tg)
        self.pipeline.publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", RecordingSession()))
        url, kwargs = tg.calls[0]
        self.assertTrue(url.endswith("/sendPhoto"))
        self.assertEqual(kwargs["data"]["chat_id"], "@ojoalprecio")
        self.assertIn('href="https://example.com/P"', kwargs["data"]["caption"])

    def test_telegram_failure_never_leaks_token_or_breaks_publish(self):
        from app.publishers.telegram import TelegramPublisher

        class Down(TelegramSession):
            def post(self, url, **kwargs):
                raise requests.ConnectionError(url)

        self.db.decide(self.deal.candidate_id, "approved")
        self.pipeline.telegram = TelegramPublisher("SECRET-BOT-TOKEN", "@c", Down())
        with self.assertRaises(RuntimeError) as raised:
            self.pipeline.telegram.send_photo(Path(__file__), "x")
        self.assertNotIn("SECRET-BOT-TOKEN", str(raised.exception))
        self.assertEqual(self.pipeline.publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", RecordingSession())), "page_456")

    def test_captions_escape_html(self):
        from dataclasses import replace
        from app.publishers.telegram import deal_caption, top_caption
        d = replace(self.deal, title="TV <55\" & más>")
        self.assertIn("TV &lt;55&quot; &amp; más&gt;", deal_caption(d, None))
        self.assertLessEqual(len(top_caption([d] * 5, None)), 1024)
