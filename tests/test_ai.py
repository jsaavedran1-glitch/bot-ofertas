from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from app.ai import MODEL, ClaudeHelper
from app.copywriter import facebook_copy
from app.models import AiCopy, DealObservation, DiscountEvidence, display_name


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Usage:
    input_tokens, output_tokens = 900, 150


class _Response:
    def __init__(self, payload, stop_reason="end_turn"):
        self.content = [_Block(json.dumps(payload))]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request and returns a canned answer."""

    def __init__(self, payload=None, stop_reason="end_turn", error=None):
        self.calls = []
        self.payload, self.stop_reason, self.error = payload, stop_reason, error
        self.beta = self
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return _Response(self.payload, self.stop_reason)


AMAZON = DealObservation(
    source="amazon", external_id="B0", title="Philips Baristina BAR308/60 Espresso Machine 16 bar",
    price_minor=8_000, original_price_minor=20_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
    currency="USD", url="https://amazon.com/x", affiliate=True,
)
GOOD = {
    "nombre_corto": "Cafetera espresso Philips Baristina",
    "titulo_es": "Cafetera espresso Philips Baristina de 16 bar",
    "gancho": "☕ Tu espresso de cafetería en casa, en segundos",
    "beneficios": ["Muele y prepara el café al instante", "Presión de 16 bar para buena crema"],
    "pregunta": "¿Eres de espresso o de tinto? ☕",
}


class ClaudeCopyTests(unittest.TestCase):
    def test_request_uses_sonnet_5_5_without_thinking_and_a_json_schema(self):
        client = FakeClient(GOOD)
        copy = ClaudeHelper(client=client).write_copy(AMAZON)
        call = client.calls[0]
        self.assertEqual(call["model"], MODEL)
        self.assertEqual(MODEL, "claude-sonnet-5-5")
        self.assertEqual(call["thinking"], {"type": "between_tools"})
        self.assertEqual(call["output_config"]["effort"], "low")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual((call["betas"], call["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertIn(AMAZON.title, call["messages"][0]["content"])
        self.assertEqual(copy.short_name, "Cafetera espresso Philips Baristina")
        self.assertEqual(len(copy.points), 2)

    def test_prices_percentages_and_invented_numbers_are_dropped(self):
        bad = dict(GOOD, gancho="🔥 ¡60% menos hoy!", pregunta="¿La comprarías a $300.000?",
                   beneficios=["Prepara 40 tazas al día", "Presión de 16 bar"])
        copy = ClaudeHelper(client=FakeClient(bad)).write_copy(AMAZON)
        self.assertIsNone(copy.hook)
        self.assertIsNone(copy.question)
        self.assertEqual(copy.points, ("Presión de 16 bar",))  # 16 is in the title, 40 is not

    def test_refusal_or_api_error_falls_back_to_templates(self):
        self.assertIsNone(ClaudeHelper(client=FakeClient(GOOD, stop_reason="refusal")).write_copy(AMAZON))
        self.assertIsNone(ClaudeHelper(client=FakeClient(error=RuntimeError("down"))).write_copy(AMAZON))

    def test_caption_uses_spanish_title_benefits_and_question_but_our_prices(self):
        deal = replace(AMAZON, ai=ClaudeHelper(client=FakeClient(GOOD)).write_copy(AMAZON))
        text = facebook_copy(deal, None, True)
        self.assertIn("☕ Tu espresso de cafetería en casa, en segundos", text.splitlines()[1])
        self.assertIn("🛍️ Cafetera espresso Philips Baristina de 16 bar", text)
        self.assertIn("✔️ Muele y prepara el café al instante", text)
        self.assertIn("¿Eres de espresso o de tinto? ☕", text)
        self.assertIn("US$80.00", text)  # price lines still come from our data
        self.assertEqual(display_name(deal), "Cafetera espresso Philips Baristina")

    def test_store_titles_in_spanish_are_kept(self):
        alkosto = replace(AMAZON, source="alkosto", currency="COP", title="Freidora de Aire MIDEA 4.5 L",
                          ai=AiCopy(title_es="Freidora de aire Midea de 4.5 L"))
        self.assertIn("🛍️ Freidora de Aire MIDEA 4.5 L", facebook_copy(alkosto, None, True))


class ClaudeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "post.png"
        Image.new("RGB", (1080, 1350), "navy").save(self.image)

    def tearDown(self):
        self.temp.cleanup()

    def test_rejection_blocks_and_sends_a_downscaled_image(self):
        client = FakeClient({"aprobada": False, "problema": "La foto es de una nevera, no de una cafetera"})
        approved, problem = ClaudeHelper(client=client).review_image(self.image, AMAZON)
        self.assertFalse(approved)
        self.assertIn("nevera", problem)
        block = client.calls[0]["messages"][0]["content"][0]
        self.assertEqual(block["source"]["media_type"], "image/jpeg")

    def test_review_approves_when_claude_is_unavailable(self):
        self.assertEqual(ClaudeHelper(client=FakeClient(error=RuntimeError("down"))).review_image(self.image, AMAZON), (True, ""))


class StubClaude:
    def __init__(self, approve=True):
        self.approve, self.reviewed = approve, []

    def write_copy(self, deal):
        return AiCopy(short_name="Producto de prueba", hook="✨ Lo que buscabas", question="¿Te lo llevas?")

    def review_image(self, image, deal):
        self.reviewed.append(image)
        return (True, "") if self.approve else (False, "foto equivocada")


class ClaudePipelineTests(unittest.TestCase):
    def _pipeline(self, ai):
        from app.database import Database
        from app.pipeline import OfferPipeline
        from tests.helpers import settings_for
        from tests.test_publisher_pipeline import FixedRate, PhotoRenderer, SameSource
        self.temp = tempfile.TemporaryDirectory()
        settings = settings_for(self.temp.name)
        self.db = Database(settings.database_url)
        self.db.initialize()
        self.deal = DealObservation(
            source="test", external_id="P", title="Producto", price_minor=80_000, original_price_minor=100_000,
            evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP", url="https://example.com/P",
            image_url="https://example.com/P.png",
        )
        self.db.create_candidate(self.deal, 40)
        self.db.decide(self.deal.candidate_id, "approved")
        return OfferPipeline(settings, self.db, [SameSource(self.deal)], rate_provider=FixedRate(),
                             renderer=PhotoRenderer(settings.generated_dir), ai=ai)

    def tearDown(self):
        self.temp.cleanup()

    def test_approved_post_carries_claude_copy(self):
        from app.publishers.facebook import FacebookPublisher
        from tests.test_publisher_pipeline import RecordingSession
        claude = StubClaude()
        session = RecordingSession()
        self._pipeline(claude).publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", session))
        caption = session.call[1]["data"]["caption"]
        self.assertIn("✨ Lo que buscabas", caption)
        self.assertIn("¿Te lo llevas?", caption)
        self.assertEqual(len(claude.reviewed), 1)

    def test_rejected_image_is_not_published(self):
        from app.publishers.facebook import FacebookPublisher
        from tests.test_publisher_pipeline import RecordingSession
        session = RecordingSession()
        pipeline = self._pipeline(StubClaude(approve=False))
        with self.assertRaisesRegex(ValueError, "foto equivocada"):
            pipeline.publish(self.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", session))
        self.assertIsNone(session.call)
        self.assertEqual(self.db.get_candidate(self.deal.candidate_id).status, "failed")
