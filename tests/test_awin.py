from datetime import datetime, timedelta, timezone
import gzip
import unittest

import requests

from app.models import DiscountEvidence
from app.sources.awin import AwinFeedSource
from app.sources.base import SourceError

KEY = "SECRETAPIKEY123"
FEED_URL = f"https://productdata.awin.com/datafeed/download/apikey/{KEY}/fid/1/format/csv/compression/gzip/"

CSV = """aw_deep_link,product_name,aw_product_id,merchant_image_url,large_image,search_price,store_price,product_price_old,rrp_price,currency,in_stock,stock_status
https://www.awin1.com/pclick.php?p=1&a=3154846&m=2,  Tenis Adidas Ultraboost 5  ,1001,https://img.adidas.co/ub.jpg,https://img.adidas.co/ub_large.jpg,399950,,799900,,COP,1,
https://www.awin1.com/pclick.php?p=2&a=3154846&m=2,Camiseta Adidas Essentials,1002,https://img.adidas.co/tee.jpg,,89950,,99950,,COP,1,
https://www.awin1.com/pclick.php?p=3&a=3154846&m=2,Sudadera agotada,1003,https://img.adidas.co/hoodie.jpg,,150000,,300000,,COP,0,
http://www.awin1.com/pclick.php?p=4&a=3154846&m=2,Enlace sin https,1004,https://img.adidas.co/x.jpg,,100000,,250000,,COP,1,
https://www.awin1.com/pclick.php?p=5&a=3154846&m=2,Gorra Adidas,1005,http://img.adidas.co/cap.jpg,,50000,,,120000,,yes,
https://www.awin1.com/pclick.php?p=6&a=3154846&m=2,Medias sin descuento,1006,https://img.adidas.co/socks.jpg,,30000,,30000,30000,COP,1,
https://www.awin1.com/pclick.php?p=7&a=3154846&m=2,Balón por estado,1007,https://img.adidas.co/ball.jpg,,,90000,180000,,COP,,in stock
"""


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} for url: {FEED_URL}")
            error.response = self
            raise error


class _Session:
    def __init__(self, content=CSV.encode(), status=200, error=None):
        self.content, self.status, self.error = content, status, error
        self.headers, self.calls = {}, 0

    def mount(self, *a):
        pass

    def get(self, url, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return _Resp(self.content, self.status)


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class AwinFeedTests(unittest.TestCase):
    def test_parses_in_stock_https_discounts_best_first(self):
        source = AwinFeedSource(FEED_URL, session=_Session(), now=_Clock())
        deals = source.fetch()
        self.assertEqual([d.external_id for d in deals], ["1005", "1001", "1007", "1002"])  # 58, 50, 50, 10 %
        ub = deals[1]
        self.assertEqual((ub.source, ub.title, ub.currency), ("adidas", "Tenis Adidas Ultraboost 5", "COP"))
        self.assertEqual((ub.price_minor, ub.original_price_minor, ub.discount_pct), (399950, 799900, 50))
        self.assertEqual(ub.evidence, DiscountEvidence.OFFICIAL_ORIGINAL)
        self.assertTrue(ub.affiliate)
        self.assertEqual(ub.image_url, "https://img.adidas.co/ub_large.jpg")  # large image preferred
        self.assertTrue(ub.url.startswith("https://www.awin1.com/"))         # tracking deep link kept
        ball = next(d for d in deals if d.external_id == "1007")
        self.assertEqual(ball.price_minor, 90000)                              # store_price fallback
        cap = next(d for d in deals if d.external_id == "1005")
        self.assertEqual((cap.original_price_minor, cap.image_url), (120000, ""))  # rrp used; http image dropped
        ids = {d.external_id for d in deals}
        self.assertNotIn("1003", ids)  # out of stock
        self.assertNotIn("1004", ids)  # http deep link
        self.assertNotIn("1006", ids)  # no real discount

    def test_discount_floor_and_item_cap(self):
        source = AwinFeedSource(FEED_URL, session=_Session(), now=_Clock(), min_discount_pct=40, max_items=2)
        self.assertEqual([d.external_id for d in source.fetch()], ["1005", "1001"])
        self.assertIsNone(AwinFeedSource(FEED_URL).min_savings_cop)  # None: the global MIN_SAVINGS_COP applies

    def test_gzip_feed(self):
        source = AwinFeedSource(FEED_URL, session=_Session(gzip.compress(CSV.encode())), now=_Clock())
        self.assertEqual(len(source.fetch()), 4)

    def test_revalidate_uses_the_cache_for_30_minutes(self):
        session, clock = _Session(), _Clock()
        source = AwinFeedSource(FEED_URL, session=session, now=clock)
        deal = next(d for d in source.fetch() if d.external_id == "1001")
        clock.now += timedelta(minutes=20)
        self.assertEqual(source.revalidate(deal).price_minor, deal.price_minor)
        self.assertEqual(session.calls, 1)
        clock.now += timedelta(minutes=15)
        session.content = CSV.replace("1001,https://img.adidas.co/ub.jpg,https://img.adidas.co/ub_large.jpg,399950,,799900,,COP,1",
                                      "1001,https://img.adidas.co/ub.jpg,https://img.adidas.co/ub_large.jpg,399950,,799900,,COP,0").encode()
        self.assertIsNone(source.revalidate(deal))  # cache expired, now out of stock
        self.assertEqual(session.calls, 2)
        other = deal.__class__(**{**deal.__dict__, "source": "nike"})
        self.assertIsNone(source.revalidate(other))

    def test_errors_never_reveal_the_feed_url_or_key(self):
        failures = [
            _Session(status=403),
            _Session(error=requests.ConnectionError(f"Max retries exceeded with url: {FEED_URL}")),
            _Session(b"\x1f\x8bbroken"),
            _Session(b"<html>login</html>"),
        ]
        for session in failures:
            source = AwinFeedSource(FEED_URL, session=session, now=_Clock())
            with self.assertRaises(SourceError) as ctx:
                source.fetch()
            message = str(ctx.exception)
            self.assertNotIn(KEY, message)
            self.assertNotIn("productdata.awin.com", message)
            self.assertIsNone(ctx.exception.__cause__)
            self.assertNotIn(KEY, repr(source))


if __name__ == "__main__":
    unittest.main()
