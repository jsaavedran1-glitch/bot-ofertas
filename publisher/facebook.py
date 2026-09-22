import os, requests
from datetime import datetime

PAGE_ID = os.getenv("FB_PAGE_ID", "1385298638000301")
TOKEN   = os.getenv("FB_PAGE_TOKEN", "")
API     = "https://graph.facebook.com/v21.0"

def _format_post(deal: dict) -> str:
    now = datetime.now().strftime("%d/%m a las %I:%M %p")

    if deal["currency"] == "COP":
        return (
            f"🔥 Oferta encontrada en {deal['source']}\n\n"
            f"🛍️ {deal['title']}\n\n"
            f"Antes: ${deal['orig_price']:,.0f} COP\n"
            f"Ahora: 💥 ${deal['price']:,.0f} COP\n"
            f"📉 {deal['discount']}% de descuento\n"
            f"≈ ${deal['price_usd']:.2f} USD\n\n"
            f"👉 {deal['url']}\n\n"
            f"⏰ Precio verificado el {now}. Puede cambiar según inventario.\n"
            f"#OjoAlPrecio #OfertasColombia #Descuentos"
        )
    else:
        rate = deal.get("usd_rate", 4200)
        cop_approx = deal["price"] * rate
        orig_cop   = deal["orig_price"] * rate
        return (
            f"⚡ Oferta {deal['source']}\n\n"
            f"🛍️ {deal['title']}\n\n"
            f"Antes: US${deal['orig_price']:.2f}\n"
            f"Ahora: 💥 US${deal['price']:.2f} · {deal['discount']}% menos\n"
            f"≈ ${cop_approx:,.0f} COP (antes ~${orig_cop:,.0f} COP)\n\n"
            f"👉 {deal['url']}\n\n"
            f"⚠️ Valor en COP aproximado; envío e impuestos se confirman en {deal['source']}.\n"
            f"🔗 Enlace de afiliado — podemos recibir comisión sin costo adicional para ti.\n"
            f"#OjoAlPrecio #Amazon #Descuentos"
        )

def post(deal: dict):
    """Publica en la página. Retorna el post_id o None si falla."""
    message = _format_post(deal)
    payload = {"message": message, "access_token": TOKEN}

    if deal.get("image_url"):
        # Publicar con imagen
        resp = requests.post(
            f"{API}/{PAGE_ID}/photos",
            data={**payload, "url": deal["image_url"], "caption": message},
            timeout=15,
        )
    else:
        resp = requests.post(f"{API}/{PAGE_ID}/feed", data=payload, timeout=15)

    data = resp.json()
    if "id" in data:
        post_id = data["id"]
        print(f"[FB] Publicado: {post_id}")
        return post_id
    else:
        print(f"[FB] Error: {data}")
        return None

def test_connection() -> bool:
    resp = requests.get(
        f"{API}/{PAGE_ID}",
        params={"fields": "name,fan_count", "access_token": TOKEN},
        timeout=10,
    )
    data = resp.json()
    if "name" in data:
        print(f"[FB] Conectado a pagina: {data['name']} ({data.get('fan_count',0)} seguidores)")
        return True
    print(f"[FB] Error de conexion: {data}")
    return False
