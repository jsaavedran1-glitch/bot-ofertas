from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from app.copywriter import facebook_copy
from app.models import DiscountEvidence
from app.scoring import qualifies
from app.sources.newegg_cards import NeweggPlayStationCardSource

FEED = """<?xml version="1.0"?>
<rss xmlns:dn="https://www.dealnews.com/ns/rss/1.0.htm" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>$290 in Sony PlayStation Store Gift Cards for $250 + email delivery</title>
<link>https://www.dealnews.com/290-in-Sony-Play-Station-Store-Gift-Cards/22321591.html?iref=rss</link>
<pubDate>Wed, 28 Oct 2026 05:19:59 -0400</pubDate>
<description>Newegg is adding a free $40 PlayStation eGift Card. Use code "PSN40OFF" at checkout.</description>
<dn:retailer>Newegg</dn:retailer><media:content url="https://d.dlnws.com/1/psn.jpeg?h=161&amp;w=250"/></item>
<item><title>Nintendo eShop $200 Gift Card for $168 + email delivery</title>
<link>https://www.dealnews.com/Nintendo/22376574.html</link><pubDate>Wed, 28 Oct 2026 05:00:00 -0400</pubDate>
<description>x</description><dn:retailer>Newegg</dn:retailer></item>
<item><title>$100 PlayStation Store Gift Card for $90 + email delivery</title>
<link>https://www.dealnews.com/PSN100/22000001.html</link><pubDate>Mon, 19 Oct 2026 05:00:00 -0400</pubDate>
<description>old</description><dn:retailer>Newegg</dn:retailer></item>
</channel></rss>"""


class _Resp:
    content = FEED.encode()

    def raise_for_status(self):
        pass


class _Session:
    headers = {}

    def mount(self, *a):
        pass

    def get(self, url, **kw):
        return _Resp()


def _source(day=29):
    return NeweggPlayStationCardSource(session=_Session(), now=lambda: datetime(2026, 10, day, 12, tzinfo=timezone.utc))


class NeweggCardTests(unittest.TestCase):
    def test_only_fresh_playstation_cards_with_balance_code_and_region(self):
        deals = _source().fetch()
        self.assertEqual(len(deals), 1)  # Nintendo is not PlayStation; the $100 card is 10 days old
        deal = deals[0]
        self.assertEqual((deal.price_minor, deal.original_price_minor, deal.evidence), (25000, 29000, DiscountEvidence.GIFT_CARD_VALUE))
        self.assertEqual(deal.title, "$290 in Sony PlayStation Store Gift Cards")
        self.assertIn("código PSN40OFF", deal.shipping_note)
        self.assertIn("cuentas PSN de Estados Unidos", deal.shipping_note)
        self.assertTrue(deal.image_url.endswith("?h=600&w=600"))
        self.assertTrue(qualifies(deal, 5, 10_000, Decimal("3900")))

    def test_stale_promos_are_not_revalidated(self):
        deal = _source().fetch()[0]
        self.assertEqual(_source().revalidate(deal).price_minor, deal.price_minor)
        self.assertIsNone(_source(day=31).revalidate(deal))  # more than 72 hours later

    def test_post_says_pay_and_balance_never_a_former_price(self):
        deal = _source().fetch()[0]
        text = facebook_copy(deal, Decimal("3900"), True, True)
        self.assertIn("Pagas US$250.00", text)
        self.assertIn("recibes US$290.00 de saldo", text)
        self.assertNotIn("Antes", text)
        self.assertIn("🎮 Con el código PSN40OFF", text)
        from app.image_renderer import OfferImageRenderer
        with tempfile.TemporaryDirectory() as directory:
            from io import BytesIO
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (300, 200), "blue").save(buf, format="PNG")
            path = OfferImageRenderer(Path(directory)).render(deal, "psn", product_image_bytes=buf.getvalue(),
                                                              usd_cop_rate=Decimal("3900"))
            self.assertTrue(path.exists())
