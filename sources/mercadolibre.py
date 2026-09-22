"""
MercadoLibre Colombia — detector de ofertas sin depender de original_price.

Estrategia: para cada producto destacado, compara el precio más barato
contra la mediana de todos los vendedores. Si está ≥20% abajo, es oferta.
"""
import os, statistics, requests
from .exchange import usd_to_cop

BASE = "https://api.mercadolibre.com"
SITE = "MCO"

CATEGORIES = [
    "MCO1144",  # Computación
    "MCO1051",  # Electrónica
    "MCO1574",  # Celulares
    "MCO1500",  # Electrodomésticos
    "MCO5726",  # Belleza
    "MCO1367",  # Hogar
    "MCO1276",  # Deportes
]

def _h():
    t = os.getenv("ML_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {t}"} if t else {}

def get_deals() -> list[dict]:
    rate   = usd_to_cop()
    min_dc = int(os.getenv("MIN_DISCOUNT_PCT", 20))
    deals  = []
    seen   = set()

    for cat in CATEGORIES:
        try:
            r = requests.get(
                f"{BASE}/highlights/{SITE}/category/{cat}",
                headers=_h(), timeout=10,
            )
            r.raise_for_status()
            prod_ids = [x["id"] for x in r.json().get("content", []) if x.get("id")]
        except Exception as e:
            print(f"[ML] highlights {cat}: {e}")
            continue

        for prod_id in prod_ids:
            if prod_id in seen:
                continue
            seen.add(prod_id)

            try:
                pr = requests.get(
                    f"{BASE}/products/{prod_id}/items",
                    headers=_h(), timeout=10,
                )
                pr.raise_for_status()
                items = pr.json().get("results", [])
            except Exception:
                continue

            # Solo items nuevos con ML Pago
            items = [i for i in items
                     if i.get("condition") == "new"
                     and i.get("accepts_mercadopago")
                     and i.get("price", 0) > 0]
            if len(items) < 3:
                continue

            prices = sorted(i["price"] for i in items)
            median = statistics.median(prices)
            best   = items[min(range(len(items)), key=lambda x: items[x]["price"])]
            best_price = best["price"]

            if median <= 0 or best_price >= median:
                continue
            discount = round((1 - best_price / median) * 100)
            if discount < min_dc:
                continue

            # Obtener nombre del producto
            try:
                pd = requests.get(f"{BASE}/products/{prod_id}", headers=_h(), timeout=8).json()
                title = pd.get("name", prod_id)
                pics  = pd.get("pictures", [{}])
                image = pics[0].get("url", "") if pics else ""
                permalink = pd.get("permalink") or f"https://www.mercadolibre.com.co/p/{prod_id}"
            except Exception:
                title, image, permalink = prod_id, "", ""

            ships_free = best.get("shipping", {}).get("free_shipping", False)

            deals.append({
                "id":          f"ml_{prod_id}_{best['item_id']}",
                "source":      "MercadoLibre",
                "title":       title,
                "url":         permalink,
                "price":       best_price,
                "orig_price":  median,
                "discount":    discount,
                "currency":    "COP",
                "price_usd":   round(best_price / rate, 2),
                "usd_rate":    rate,
                "image_url":   image,
                "category":    cat,
                "ships_to_co": ships_free,
                "score":       discount + (10 if ships_free else 0),
            })
            print(f"[ML] Oferta: {title[:50]} | -{discount}% | ${best_price:,.0f} vs mediana ${median:,.0f}")

    return deals
