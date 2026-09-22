from decimal import Decimal
import unittest

from app.copywriter import facebook_copy
from app.models import DealObservation, DiscountEvidence, money, to_minor
from app.scoring import qualifies, validate_observation


class ModelsAndScoringTests(unittest.TestCase):
    def deal(self, evidence=DiscountEvidence.OFFICIAL_ORIGINAL, original=100_000, price=80_000):
        return DealObservation(
            source="mercadolibre", external_id="MCO1", title="Producto",
            price_minor=price, original_price_minor=original, evidence=evidence,
            currency="COP", url="https://articulo.mercadolibre.com.co/MCO-1",
        )

    def test_exact_twenty_percent_qualifies(self):
        deal = self.deal()
        self.assertEqual(deal.discount_pct, 20)
        self.assertTrue(qualifies(deal, 20, 20_000, Decimal("4000")))

    def test_unverified_reference_is_rejected(self):
        deal = self.deal(evidence=DiscountEvidence.NONE)
        valid, reason = validate_observation(deal)
        self.assertFalse(valid)
        self.assertIn("evidencia", reason)

    def test_competitor_or_missing_reference_cannot_claim_discount(self):
        deal = self.deal(evidence=DiscountEvidence.NONE, original=None)
        self.assertFalse(qualifies(deal, 20, 1, Decimal("4000")))
        copy = facebook_copy(deal, Decimal("4000"))
        self.assertNotIn("Antes:", copy)
        self.assertNotIn("% de descuento", copy)

    def test_history_wording_never_says_antes(self):
        deal = self.deal(evidence=DiscountEvidence.OWN_HISTORY)
        copy = facebook_copy(deal, None)
        self.assertIn("Precio típico observado", copy)
        self.assertNotIn("Antes:", copy)

    def test_money_uses_integer_minor_units(self):
        self.assertEqual(to_minor("39.99", "USD"), 3999)
        self.assertEqual(money(119_900, "COP"), "$119.900 COP")
