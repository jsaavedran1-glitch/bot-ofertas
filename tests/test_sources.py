from pathlib import Path
import json
import tempfile
import unittest

from app.models import DiscountEvidence
from app.sources.mercadolibre import MercadoLibreSource
from app.sources.partner_feed import PartnerFeedSource


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def mount(self, *_):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class SourceTests(unittest.TestCase):
    def test_mercadolibre_uses_official_original_price_from_items_api(self):
        # highlights returns product IDs
        highlights = {"content": [{"id": "MCO_PROD1", "type": "PRODUCT"}]}
        # /products/{id}/items returns items with official original_price
        items_resp = {"results": [{
            "item_id": "MCO123", "price": 80_000, "original_price": 100_000,
            "condition": "new", "accepts_mercadopago": True, "currency_id": "COP",
            "shipping": {"free_shipping": True},
        }]}
        # /products/{id} for title + image
        product_meta = {"name": "Producto de Prueba", "pictures": [{"url": "https://img.ml/foto.jpg"}]}
        # /items/{item_id} for permalink
        item_detail = {"permalink": "https://articulo.mercadolibre.com.co/MCO123", "secure_thumbnail": ""}
        # One category (MCO1144) only; remaining categories return empty content
        from app.sources.mercadolibre import CATEGORIES
        empty_cats = [FakeResponse({"content": []})] * (len(CATEGORIES) - 1)
        session = FakeSession([
            FakeResponse(highlights),          # highlights MCO1144
            FakeResponse(items_resp),          # /products/MCO_PROD1/items
            FakeResponse(product_meta),        # /products/MCO_PROD1
            FakeResponse(item_detail),         # /items/MCO123
            *empty_cats,
        ])
        deals = MercadoLibreSource(20, "token-de-prueba", session).fetch()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].external_id, "MCO_PROD1")
        self.assertEqual(deals[0].url, "https://articulo.mercadolibre.com.co/MCO123")
        self.assertEqual(deals[0].evidence, DiscountEvidence.OFFICIAL_ORIGINAL)
        self.assertEqual(deals[0].discount_pct, 20)

    def test_mercadolibre_rejects_items_without_official_original_price(self):
        highlights = {"content": [{"id": "MCO_PROD2", "type": "PRODUCT"}]}
        # Items without original_price should be skipped
        items_resp = {"results": [{
            "item_id": "MCO456", "price": 80_000, "original_price": None,
            "condition": "new", "accepts_mercadopago": True, "currency_id": "COP",
        }]}
        from app.sources.mercadolibre import CATEGORIES
        empty_cats = [FakeResponse({"content": []})] * (len(CATEGORIES) - 1)
        session = FakeSession([
            FakeResponse(highlights),
            FakeResponse(items_resp),
            *empty_cats,
        ])
        deals = MercadoLibreSource(20, "token-de-prueba", session).fetch()
        self.assertEqual(deals, [])

    def test_partner_feed_requires_explicit_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_text(json.dumps([{
                "source": "amazon", "id": "SKU", "title": "Producto", "price": "40",
                "original_price": "80", "original_price_verified": False,
                "currency": "USD", "url": "https://example.com/producto",
            }]), encoding="utf-8")
            deal = PartnerFeedSource(str(path)).fetch()[0]
            self.assertEqual(deal.evidence, DiscountEvidence.NONE)
            self.assertIsNone(deal.original_price_minor)


class DealNewsSourceTests(unittest.TestCase):
    def _item(self, retailer, title):
        import xml.etree.ElementTree as ET
        return ET.fromstring(f"""<item xmlns:dealnews="https://www.dealnews.com/ns/rss/1.0.htm"
            xmlns:media="http://search.yahoo.com/mrss/">
          <title>{title}</title>
          <link>https://www.dealnews.com/Some-Deal/22206491.html?iref=rss</link>
          <description>Now $9, down from $21.99.</description>
          <dealnews:retailer>{retailer}</dealnews:retailer>
          <media:content url="https://d.dlnws.com/1/x.jpg?h=125&amp;w=103"/>
        </item>""")

    def test_amazon_deal_gets_tagged_search_link_and_woot_is_ignored(self):
        from app.sources.amazon_rss import AmazonRssSource
        src = AmazonRssSource()
        amazon = src._parse_item(self._item("Amazon", "Granicell AA Batteries 16-Pack for $9"))
        self.assertTrue(amazon.url.startswith("https://www.amazon.com/s?"))
        self.assertIn("tag=ojoalprecio10-20", amazon.url)
        self.assertIn("Granicell", amazon.url)
        self.assertTrue(amazon.affiliate)
        self.assertIn("h=600", amazon.image_url)
        self.assertIsNone(src._parse_item(self._item("Woot! An Amazon Company", "Refurb Bose Speaker for $9")))


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def mount(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _Resp(self.payload)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Resp(self.payload)


def _vtex_product(pid, name, seller_id, price, before):
    return {
        "productId": pid, "productName": name, "link": f"https://www.exito.com/{pid}/p",
        "items": [{"images": [{"imageUrl": f"https://img/{pid}.jpg"}], "sellers": [{
            "sellerId": seller_id,
            "commertialOffer": {"Price": price, "ListPrice": before, "IsAvailable": True, "AvailableQuantity": 5},
        }]}],
    }


class ColombiaStoreTests(unittest.TestCase):
    def test_exito_keeps_only_own_seller_discounts_and_dedupes_variants(self):
        from app.sources.colombia_stores import VtexStoreSource
        payload = [
            _vtex_product("1", "Freidora MIDEA 4.5 L", "1", 199900, 289900),
            _vtex_product("2", "Freidora MIDEA 4.5 L", "1", 199900, 289900),  # color variant
            _vtex_product("3", "Mini Proyector", "16475", 64500, 185400),     # marketplace
            _vtex_product("4", "Nevera sin descuento", "1", 900000, 900000),
        ]
        session = _Session(payload)
        deals = VtexStoreSource("exito", "https://www.exito.com", session=session, queries=("freidora",)).fetch()
        self.assertEqual([(d.source, d.title, d.currency) for d in deals], [("exito", "Freidora MIDEA 4.5 L", "COP")])
        self.assertEqual(deals[0].discount_pct, 31)
        self.assertIn("/api/catalog_system/pub/products/search?ft=freidora", session.calls[0])

    def test_alkosto_hit_becomes_deal_and_lookup_filters_by_object_id(self):
        from app.sources.colombia_stores import AlgoliaStoreSource
        hit = {
            "objectID": "7705946474559", "name_text_es": "TV KALLEY 60", "instockflag_boolean": True,
            "discountprice_double": 1699900.0, "baseprice_cop_string": 3899900.0,
            "url_es_string": "/tv-kalley-60/p/7705946474559", "img-750wx750h_string": "https://cdn/x.webp",
        }
        session = _Session({"hits": [hit]})
        src = AlgoliaStoreSource("alkosto", "alkostoIndexAlgoliaPRD", "https://www.alkosto.com", session=session, queries=("tv",))
        deal = src.fetch()[0]
        self.assertEqual(deal.url, "https://www.alkosto.com/tv-kalley-60/p/7705946474559")
        self.assertEqual(deal.discount_pct, 56)
        self.assertEqual(src.revalidate(deal).price_minor, deal.price_minor)
        url, kwargs = session.calls[-1]
        self.assertIn("/indexes/alkostoIndexAlgoliaPRD/query", url)
        self.assertIn("objectID", kwargs["json"]["params"])
        self.assertEqual(kwargs["headers"]["Referer"], "https://www.alkosto.com/")
