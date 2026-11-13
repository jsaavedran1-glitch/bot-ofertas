import json
import unittest

from app.sources.colombia_stores import FalabellaBrandSource, VtexStoreSource, _vtex_title


class _Resp:
    def __init__(self, payload=None, text=""):
        self.payload, self.text, self.status_code = payload, text, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses, self.urls, self.headers = list(responses), [], {}

    def mount(self, *args):
        pass

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def _next_data(page_props):
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": page_props}})}</script></html>'


def _vtex_product(pid, name, title, items, categories=("/Mujer/Ropa/Leggins/",)):
    return {"productId": pid, "productName": name, "productTitle": title, "categories": list(categories),
            "link": f"https://www.nike.com.co/{pid}/p", "items": items}


def _item(price, list_price, qty, seller="1"):
    return {"images": [{"imageUrl": "https://img/x.jpg"}],
            "sellers": [{"sellerId": seller, "commertialOffer": {"Price": price, "ListPrice": list_price,
                                                                  "AvailableQuantity": qty, "IsAvailable": qty > 0}}]}


class VtexFashionTests(unittest.TestCase):
    def test_titles_use_nike_descriptions_or_the_category(self):
        self.assertEqual(_vtex_title({"productTitle": "Nike Field General | Calzado para mujer | Naranja", "productName": "x"}),
                         "Nike Field General - Calzado para mujer - Naranja")
        self.assertEqual(_vtex_title({"productTitle": "", "productName": "Nike Zenvy", "categories": ["/Mujer/Ropa/Leggins/"]}),
                         "Nike Zenvy - Leggins")
        self.assertEqual(_vtex_title({"productTitle": "Compra camisa en www.tennis.com.co",
                                      "productName": "Camisa negra con transparencia para mujer"}),
                         "Camisa negra con transparencia para mujer")

    def test_biggest_discounts_first_and_first_size_in_stock(self):
        product = _vtex_product("10", "Nike Zenvy", "", [_item(197980, 494950, 0), _item(197980, 494950, 3)])
        session = _Session([_Resp([product])])
        source = VtexStoreSource("nike", "https://www.nike.com.co", session=session, queries=("",), per_query=50,
                                 order="OrderByBestDiscountDESC")
        deal = source.fetch()[0]
        self.assertIn("O=OrderByBestDiscountDESC", session.urls[0])
        self.assertNotIn("ft=", session.urls[0])
        self.assertEqual((deal.source, deal.discount_pct, deal.title), ("nike", 60, "Nike Zenvy - Leggins"))


class FalabellaTests(unittest.TestCase):
    RESULT = {"productId": "73392449", "skuId": "73392456", "displayName": "Sudadera Deportiva Mujer Lifestyle",
              "brand": "PUMA", "sellerName": "Falabella",
              "url": "https://www.falabella.com.co/falabella-co/product/73392449/Saco-Deportivo-Mujer-Puma",
              "mediaUrls": ["https://media.falabella.com.co/x/public"],
              "prices": [{"type": "internetPrice", "price": ["209.990"]}, {"type": "normalPrice", "price": ["349.990"]}]}

    def _listing(self):
        marketplace = dict(self.RESULT, productId="9", skuId="10", sellerName="Otra Tienda")
        return _Resp(text=_next_data({"results": [self.RESULT, marketplace]}))

    def test_only_falabella_sold_items_with_sku_links_one_brand_per_scan(self):
        session = _Session([self._listing()])
        source = FalabellaBrandSource(session=session, clock=lambda: 1800 * 2)  # third slot -> PUMA
        deals = source.fetch()
        self.assertIn("/brand/PUMA?", session.urls[0])
        self.assertIn("sellerId=FALABELLA", session.urls[0])
        self.assertEqual(len(deals), 1)
        deal = deals[0]
        self.assertEqual(deal.title, "Sudadera Deportiva Mujer Lifestyle Puma")
        self.assertTrue(deal.url.endswith("/Saco-Deportivo-Mujer-Puma/73392456"))
        self.assertEqual((deal.price_minor, deal.original_price_minor, deal.discount_pct), (209990, 349990, 40))

    def test_revalidation_checks_the_exact_variant(self):
        deal = FalabellaBrandSource(session=_Session([self._listing()]), clock=lambda: 3600).fetch()[0]
        variants = [
            {"id": "73392450", "isPurchaseable": True, "offerings": [{"sellerName": "FALABELLA"}],
             "prices": [{"type": "internetPrice", "price": ["349.990"]}]},
            {"id": "73392456", "isPurchaseable": True, "offerings": [{"sellerName": "FALABELLA"}],
             "prices": [{"type": "internetPrice", "price": ["209.990"]}, {"type": "normalPrice", "price": ["349.990"]}]},
        ]
        page = _Resp(text=_next_data({"productData": {"variants": variants, "isOutOfStock": False}}))
        fresh = FalabellaBrandSource(session=_Session([page])).revalidate(deal)
        self.assertEqual((fresh.price_minor, fresh.original_price_minor, fresh.url), (209990, 349990, deal.url))
        variants[1]["isPurchaseable"] = False
        page = _Resp(text=_next_data({"productData": {"variants": variants, "isOutOfStock": False}}))
        self.assertIsNone(FalabellaBrandSource(session=_Session([page])).revalidate(deal))
