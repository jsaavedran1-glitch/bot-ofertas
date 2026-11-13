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
NAMES = {"nombre_corto": "Cafetera espresso Philips Baristina",
         "titulo_es": "Cafetera espresso Philips Baristina de 16 bar"}
ANALYSIS = {
    "aprobada": True, "problema": "",
    "gancho": "☕ Tu espresso de cafetería en casa, en segundos",
    "beneficios": ["Muele y prepara el café al instante", "Presión de 16 bar para buena crema"],
    "pregunta": "¿Eres de espresso o de tinto? ☕",
    "hashtags": ["#cafe", "#BlackFriday"],
}


class ClaudeNamesTests(unittest.TestCase):
    def test_request_uses_sonnet_5_5_without_thinking_and_a_json_schema(self):
        client = FakeClient(NAMES)
        names = ClaudeHelper(client=client).write_names(AMAZON)
        call = client.calls[0]
        self.assertEqual(call["model"], MODEL)
        self.assertEqual(MODEL, "claude-sonnet-5-5")
        self.assertEqual(call["thinking"], {"type": "between_tools"})
        self.assertEqual(call["output_config"]["effort"], "low")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual((call["betas"], call["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertIn(AMAZON.title, call["messages"][0]["content"])
        self.assertEqual(names.short_name, "Cafetera espresso Philips Baristina")

    def test_long_names_are_cut_at_a_whole_word(self):
        long_name = dict(NAMES, nombre_corto="Cafetera espresso Philips Baristina con molino integrado y espumador")
        names = ClaudeHelper(client=FakeClient(long_name)).write_names(AMAZON)
        self.assertLessEqual(len(names.short_name), 50)
        self.assertTrue("Cafetera espresso Philips Baristina con molino integrado".startswith(names.short_name))
        nike = replace(AMAZON, title="Nike Air Zoom Pegasus 41 running")
        names = ClaudeHelper(client=FakeClient(dict(NAMES, nombre_corto="Tenis de running para hombre Pegasus de Nike con amortiguación"))).write_names(nike)
        self.assertEqual(names.short_name, "Tenis de running para hombre Pegasus de Nike")

    def test_refusal_or_api_error_falls_back_to_templates(self):
        self.assertIsNone(ClaudeHelper(client=FakeClient(NAMES, stop_reason="refusal")).write_names(AMAZON))
        self.assertIsNone(ClaudeHelper(client=FakeClient(error=RuntimeError("down"))).write_names(AMAZON))


class ClaudeAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "post.png"
        Image.new("RGB", (1080, 1350), "navy").save(self.image)

    def tearDown(self):
        self.temp.cleanup()

    def test_writes_caption_text_and_hashtags_from_the_image(self):
        client = FakeClient(ANALYSIS)
        approved, _, copy = ClaudeHelper(client=client).analyze_post(self.image, AMAZON, ("#BlackFriday", "#Navidad"))
        self.assertTrue(approved)
        self.assertEqual(copy.hook, "☕ Tu espresso de cafetería en casa, en segundos")
        self.assertEqual(copy.hashtags, ("#cafe", "#BlackFriday"))
        content = client.calls[0]["messages"][0]["content"]
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertIn("#BlackFriday, #Navidad", content[1]["text"])

    def test_prices_invented_numbers_and_bad_hashtags_are_dropped(self):
        bad = dict(ANALYSIS, gancho="🔥 ¡60% menos hoy!", pregunta="¿La comprarías a $300.000?",
                   beneficios=["Prepara 40 tazas al día", "Presión de 16 bar"],
                   hashtags=["#ofertas", "#OjoAlPrecio", "dos palabras", "#ahorra50", "cafetera", "#cafe", "#extra"])
        _, _, copy = ClaudeHelper(client=FakeClient(bad)).analyze_post(self.image, AMAZON)
        self.assertIsNone(copy.hook)
        self.assertIsNone(copy.question)
        self.assertEqual(copy.points, ("Presión de 16 bar",))  # 16 is in the title, 40 is not
        self.assertEqual(copy.hashtags, ("#cafetera", "#cafe"))

    def test_rejection_blocks_and_unavailable_claude_approves(self):
        rejected = dict(ANALYSIS, aprobada=False, problema="La foto es de una nevera, no de una cafetera")
        approved, problem, copy = ClaudeHelper(client=FakeClient(rejected)).analyze_post(self.image, AMAZON)
        self.assertEqual((approved, copy), (False, None))
        self.assertIn("nevera", problem)
        self.assertEqual(ClaudeHelper(client=FakeClient(error=RuntimeError("x"))).analyze_post(self.image, AMAZON),
                         (True, "", None))


class ClaudeCaptionTests(unittest.TestCase):
    def test_caption_uses_claude_text_spanish_title_and_three_hashtags_but_our_prices(self):
        from app.ai import merge_copy
        names = AiCopy(short_name="Cafetera espresso Philips Baristina", title_es="Cafetera espresso Philips Baristina de 16 bar")
        text = AiCopy(hook="☕ Tu espresso de cafetería en casa", points=("Muele y prepara al instante",),
                      question="¿Espresso o tinto?", hashtags=("#cafe", "#BlackFriday"))
        deal = replace(AMAZON, ai=merge_copy(names, text))
        caption = facebook_copy(deal, None, True)
        self.assertIn("☕ Tu espresso de cafetería en casa", caption.splitlines()[1])
        self.assertIn("🛍️ Cafetera espresso Philips Baristina de 16 bar", caption)
        self.assertIn("✔️ Muele y prepara al instante", caption)
        self.assertIn("¿Espresso o tinto?", caption)
        self.assertEqual(caption.splitlines()[-1], "#OjoAlPrecio #cafe #BlackFriday")
        self.assertIn("US$80.00", caption)  # price lines still come from our data
        self.assertEqual(display_name(deal), "Cafetera espresso Philips Baristina")

    def test_without_claude_only_three_hashtags(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"TRENDING_TAGS": "BlackFriday,Navidad"}):
            tags = facebook_copy(AMAZON, None, True).splitlines()[-1].split()
        self.assertEqual(tags[:2], ["#OjoAlPrecio", "#amazon"])
        self.assertEqual(len(tags), 3)

    def test_store_titles_in_spanish_are_kept(self):
        alkosto = replace(AMAZON, source="alkosto", currency="COP", title="Freidora de Aire MIDEA 4.5 L",
                          ai=AiCopy(title_es="Freidora de aire Midea de 4.5 L"))
        self.assertIn("🛍️ Freidora de Aire MIDEA 4.5 L", facebook_copy(alkosto, None, True))


class StubClaude:
    def __init__(self, approve=True):
        self.approve, self.reviewed = approve, []

    def write_names(self, deal):
        return AiCopy(short_name="Producto de prueba")

    def analyze_post(self, image, deal, trending=()):
        self.reviewed.append(image)
        if not self.approve:
            return False, "foto equivocada", None
        return True, "", AiCopy(hook="✨ Lo que buscabas", question="¿Te lo llevas?", hashtags=("#tenis",))


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
        self.assertIn("#OjoAlPrecio #tenis", caption)
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
