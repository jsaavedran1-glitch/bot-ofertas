# Ojo al Precio

Bot de ofertas para una Página de Facebook. Detecta precios verificables,
mantiene historial, genera una pieza cuadrada de 1080 × 1080 y publica mediante
la API oficial de Meta. El modo predeterminado es `review`: nada se publica sin
aprobación.

## Garantías importantes

- Nunca convierte la mediana de otros vendedores en un supuesto precio “Antes”.
- Un descuento usa `regular_amount`/precio original oficial del mismo ítem o
  historial propio suficiente. El historial se describe como “precio típico”.
- Cada candidato conserva una fotografía inmutable del precio y del enlace.
- La reserva transaccional y el límite diario usan el día de Bogotá.
- Un timeout durante la publicación queda en estado `unknown`; no se reintenta
  automáticamente porque Meta podría haber aceptado la foto.
- Amazon y Temu solo se incorporan mediante feeds/API de afiliado autorizados.

## Instalación local

Requiere Python 3.11.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m unittest discover -s tests -v
python main.py demo
```

`demo` genera una imagen y un copy ficticios, crea un candidato pendiente y
realiza cero llamadas de publicación.

## Comandos

```bash
python main.py scan
python main.py list --status pending
python main.py approve ID
python main.py reject ID
python main.py render ID
python main.py publish ID
python main.py run --dry-run
python main.py run --automatic
```

`publish` solo acepta candidatos aprobados y recientes. `run --automatic`
aprueba y publica candidatos nuevos; úsalo únicamente después de validar varios
ciclos en `review`.

## Mercado Libre

El bot usa OAuth Authorization Code/refresh token. `client_credentials` no es
válido. El refresh token inicial se toma del entorno una vez; después cada par
rotado se cifra con `TOKEN_ENCRYPTION_KEY` y se guarda atómicamente en la base.

Genera una clave estable una sola vez:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Guárdala como secreto. Si la pierdes no podrás recuperar los tokens cifrados y
deberás autorizar la aplicación de nuevo.

## Feed autorizado para Amazon, Temu u otras tiendas

Configura `PARTNER_FEED_PATH` con un `.json` o `.csv`. Un JSON válido luce así:

```json
[
  {
    "source": "amazon",
    "id": "SKU-123",
    "title": "Teclado mecánico",
    "price": "39.99",
    "original_price": "69.99",
    "original_price_verified": true,
    "currency": "USD",
    "url": "https://enlace-afiliado.example/producto",
    "image_url": "https://cdn-autorizado.example/producto.jpg",
    "available": true,
    "affiliate": true,
    "shipping_note": "Envío e impuestos se confirman en la tienda"
  }
]
```

Sin `original_price_verified=true`, el bot conserva la observación para crear
historial pero no anuncia un descuento oficial.

## Supabase/PostgreSQL

En GitHub Actions configura `DATABASE_URL` con el **Session Pooler** de Supabase
y `sslmode=require`. Copia la URL exacta desde Supabase → Connect; no inventes
el host ni el usuario. SQLite es solamente para desarrollo local.

Secretos mínimos para Actions:

- `DATABASE_URL`
- `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REFRESH_TOKEN`
- `TOKEN_ENCRYPTION_KEY`
- `FB_PAGE_ID`, `FB_PAGE_TOKEN`

`ML_ACCESS_TOKEN` es opcional como bootstrap local. El publicador no necesita
`FB_APP_ID` ni `FB_APP_SECRET`.

## GitHub Actions

El workflow ejecuta pruebas antes del bot, impide solapamientos y exige
PostgreSQL. La programación está pausada hasta que se habilite explícitamente.
La variable de repositorio `POST_MODE` debe permanecer en `review`; cambiarla a
`automatic` activa publicaciones en los ciclos programados.

Antes de activar:

1. Ejecuta dos ciclos en `review` y comprueba que no duplican candidatos.
2. Revisa las imágenes y los copies.
3. Ejecuta una publicación real de un solo candidato aprobado.
4. Solo entonces establece `POST_MODE=automatic` y habilita el workflow.

## Seguridad

`.env`, bases locales, feeds, imágenes y logs están ignorados por Git. Revoca
cualquier PAT o token que se haya mostrado en conversaciones anteriores. Nunca
pegues credenciales en incidencias, mensajes, commits o capturas.
