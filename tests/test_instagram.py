import tempfile
import unittest
from pathlib import Path

from app.publishers.instagram import InstagramPublisher, InstagramPublishError, instagram_caption


class Response:
    def __init__(self, payload, ok=True, status=200):
        self.payload, self.ok, self.status_code = payload, ok, status

    def json(self):
        return self.payload


class FakeGraph:
    """Answers the Instagram container, upload, status and publish calls."""

    def __init__(self, statuses=("IN_PROGRESS", "FINISHED")):
        self.posts, self.gets, self.statuses = [], [], list(statuses)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/media"):
            return Response({"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/c1"})
        if url.endswith("/media_publish"):
            return Response({"id": "ig_media_9"})
        return Response({"success": True})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if kwargs.get("params", {}).get("fields") == "instagram_business_account":
            return Response({"instagram_business_account": {"id": "1784"}, "id": "page"})
        return Response({"status_code": self.statuses.pop(0) if self.statuses else "FINISHED"})


class CaptionTests(unittest.TestCase):
    def test_links_point_to_the_bio(self):
        self.assertIn("🔗 Link en nuestra bio", instagram_caption("Oferta\n👇 Link en el primer comentario"))
        self.assertNotIn("https://", instagram_caption("Oferta\n👉 https://example.com/x"))


class PublishTests(unittest.TestCase):
    def test_reel_goes_through_resumable_upload_and_waits_for_processing(self):
        graph = FakeGraph()
        ig = InstagramPublisher.for_page("page", "tok", "v26.0", graph)
        ig.poll_seconds = 0
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "r.mp4"
            video.write_bytes(b"12345")
            self.assertEqual(ig.publish_reel(video, "Hola\n👇 Link en el primer comentario"), "ig_media_9")
        container, upload, publish = graph.posts
        self.assertEqual(container[1]["data"]["media_type"], "REELS")
        self.assertEqual(container[1]["data"]["upload_type"], "resumable")
        self.assertIn("Link en nuestra bio", container[1]["data"]["caption"])
        self.assertEqual(upload[0], "https://rupload.facebook.com/ig-api-upload/v26.0/c1")
        self.assertEqual(upload[1]["headers"]["file_size"], "5")
        self.assertEqual(publish[1]["data"]["creation_id"], "c1")

    def test_photo_uses_the_public_url(self):
        graph = FakeGraph(statuses=("FINISHED",))
        ig = InstagramPublisher("1784", "tok", "v26.0", graph)
        ig.publish_image("https://scontent.fbcdn.net/x.jpg", "Hola")
        self.assertEqual(graph.posts[0][1]["data"]["image_url"], "https://scontent.fbcdn.net/x.jpg")

    def test_processing_error_raises(self):
        ig = InstagramPublisher("1784", "tok", "v26.0", FakeGraph(statuses=("ERROR",)), poll_seconds=0)
        with self.assertRaises(InstagramPublishError):
            ig.publish_image("https://scontent.fbcdn.net/x.jpg", "Hola")

    def test_page_without_instagram_gives_none(self):
        class NoAccount(FakeGraph):
            def get(self, url, **kwargs):
                return Response({"id": "page"})
        self.assertIsNone(InstagramPublisher.for_page("page", "tok", "v26.0", NoAccount()))


class PipelineTests(unittest.TestCase):
    def test_reel_is_copied_to_instagram_and_failures_only_warn(self):
        from dataclasses import replace
        from app.publishers.facebook import FacebookPublisher
        from tests.test_publisher_pipeline import FakeReelRenderer, PublisherPipelineTests, ReelSession

        case = PublisherPipelineTests()
        case.setUp()
        try:
            case.db.decide(case.deal.candidate_id, "approved")
            pipeline = case.pipeline
            pipeline.settings = replace(pipeline.settings, instagram_kinds=frozenset({"reel"}))
            pipeline.reel_renderer = FakeReelRenderer(case.temp.name)

            class Recorder:
                calls = []

                def publish_reel(self, video, caption):
                    self.calls.append(caption)
                    raise InstagramPublishError("caído")

            pipeline._ig = Recorder()
            video_id = pipeline.publish(case.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", ReelSession()),
                                        as_reel=True)
            self.assertEqual(video_id, "vid_789")
            self.assertEqual(len(Recorder.calls), 1)
            self.assertEqual(case.db.get_candidate(case.deal.candidate_id).status, "published")
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
