import unittest
from pathlib import Path
from dataclasses import replace

from PIL import Image

from app.copywriter import _QUESTIONS, facebook_copy
from app.image_renderer import PHOTO_BOX
from app.models import AiCopy, DealObservation, DiscountEvidence
from app.quality import caption_issues, ensure_recommendable, names_of, photo_issue

DEAL = DealObservation(
    source="alkosto", external_id="Q1", title="Impresora de etiqueta a color Brother", price_minor=200_000,
    original_price_minor=400_000, evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP",
    url="https://example.com/q1", image_url="https://example.com/q1.png",
)


class CaptionTests(unittest.TestCase):
    def test_engagement_bait_and_clickbait_are_caught(self):
        for text in ("Comenta SÍ si lo quieres", "Etiqueta a tu amigo que lo necesita", "¡Compártelo!",
                     "Dale like si te gusta", "Si te gusta, comparte", "NO LO COMPRES SIN VER ESTO",
                     "¡Precio de locura!", "Última oportunidad", "Escribe \"YO\" y te mandamos el link"):
            self.assertTrue(caption_issues(text), text)

    def test_bot_questions_and_captions_pass(self):
        for question in _QUESTIONS:
            self.assertEqual(caption_issues(question), [], question)
        for i in range(30):
            deal = replace(DEAL, external_id=f"Q{i}", lowest_30d=bool(i % 2))
            caption = facebook_copy(deal, None, True, link_in_comment=bool(i % 3))
            self.assertEqual(caption_issues(caption, names_of([deal])), [], caption)

    def test_product_names_are_not_judged(self):
        self.assertTrue(caption_issues(DEAL.title))
        self.assertEqual(caption_issues(f"🔥 {DEAL.title} rebajada", names_of([DEAL])), [])

    def test_ensure_recommendable_stops_the_post(self):
        with self.assertRaisesRegex(ValueError, "no recomendable"):
            ensure_recommendable("🔥 Oferta\n\nComenta SÍ y te lo enviamos")
        ensure_recommendable("🔥 Oferta\n\n¿Qué te parece este descuento? Cuéntanos 👇")

    def test_claude_text_with_bait_falls_back_to_templates(self):
        from app.ai import _valid, _valid_fact
        self.assertIsNone(_valid("👀 No lo compres sin ver esto", "Tenis Nike", 100))
        self.assertIsNone(_valid("¿Lo quieres? Comenta YO", "Tenis Nike", 90))
        self.assertEqual(_valid("👟 Livianos para correr todos los días", "Tenis Nike", 100),
                         "👟 Livianos para correr todos los días")
        self.assertIsNone(_valid_fact("Etiqueta a quien compra en línea", "Nueva norma de compras", 90))


class PhotoTests(unittest.TestCase):
    def test_tiny_photos_are_rejected_tall_products_are_fine(self):
        self.assertIsNotNone(photo_issue((150, 150), PHOTO_BOX))
        self.assertIsNone(photo_issue((600, 600), PHOTO_BOX))
        self.assertIsNone(photo_issue((200, 900), PHOTO_BOX))  # a bottle: its height fills the card

    def test_renderer_refuses_a_pixelated_photo(self):
        import tempfile
        from io import BytesIO
        from app.image_renderer import MissingProductImage, OfferImageRenderer
        buffer = BytesIO()
        Image.new("RGB", (120, 120), "red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MissingProductImage, "pixelada"):
                OfferImageRenderer(Path(directory)).render(DEAL, "q", product_image_bytes=buffer.getvalue(), require_image=True)


class ReelTests(unittest.TestCase):
    def test_reel_openers_are_informative(self):
        from app.reel_renderer import reel_hook
        for i in range(60):
            emoji, lines = reel_hook(replace(DEAL, external_id=f"R{i}"), "$200.000")
            self.assertEqual(caption_issues(" ".join(lines)), [], lines)

    def test_review_covers_each_screen_once(self):
        from app.reel_renderer import PHASE_SETS, review_moments
        for phases in PHASE_SETS:
            moments = review_moments(phases)
            kinds = [next(k for s, e, k in phases if s <= t < e) for t in moments]
            self.assertEqual(sorted(kinds), sorted({k for _, _, k in phases}))

    def test_rejected_reel_screen_stops_the_reel(self):
        from app.publishers.facebook import FacebookPublisher
        from tests.test_ai import ClaudePipelineTests, StubClaude
        from tests.test_publisher_pipeline import FakeReelRenderer, ReelSession

        class Screens(FakeReelRenderer):
            def review_frames(self, deal, usd_cop_rate=None, link_in_comment=False):
                return [Image.new("RGB", (1080, 1920), "navy")]

        class Strict(StubClaude):
            def review_frames(self, frames, deal):
                self.screens = frames
                return False, "texto encimado"

        case = ClaudePipelineTests()
        claude = Strict()
        pipeline = case._pipeline(claude)
        try:
            pipeline.reel_renderer = Screens(case.temp.name)
            session = ReelSession()
            with self.assertRaisesRegex(ValueError, "Claude detuvo el reel: texto encimado"):
                pipeline.publish(case.deal.candidate_id, FacebookPublisher("page", "t", "v26.0", session), as_reel=True)
            self.assertEqual(session.calls, [])
            self.assertEqual(len(claude.screens), 1)
        finally:
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
