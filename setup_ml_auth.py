"""
Obtiene el access_token de MercadoLibre.

En MercadoLibre DevCenter, la URL de redireccion debe ser exactamente:
  https://localhost

Cuando el navegador muestre error de conexion despues de aceptar,
copia la URL completa de la barra de direcciones y pegala aqui.
"""

import webbrowser, requests, urllib.parse, os, sys

APP_ID        = input("APP_ID de MercadoLibre: ").strip()
CLIENT_SECRET = input("CLIENT_SECRET: ").strip()
REDIRECT_URI  = "https://localhost"

auth_url = (
    f"https://auth.mercadolibre.com.co/authorization"
    f"?response_type=code&client_id={APP_ID}&redirect_uri={REDIRECT_URI}"
)

print(f"\nAbriendo navegador...")
webbrowser.open(auth_url)

print("""
Pasos:
 1. Acepta los permisos en el navegador
 2. El navegador mostrara un error (no puede conectar a localhost) - ESO ES NORMAL
 3. Copia TODA la URL de la barra de direcciones
    Ejemplo: https://localhost/?code=TG-XXXXXXX-123456789
 4. Pegala aqui abajo
""")

raw_url = input("Pega la URL completa: ").strip()

parsed = urllib.parse.urlparse(raw_url)
params = urllib.parse.parse_qs(parsed.query)
code   = params.get("code", [None])[0]

if not code:
    print(f"No se encontro 'code' en la URL. URL recibida: {raw_url}")
    sys.exit(1)

print(f"\nCodigo obtenido. Intercambiando por token...")

resp = requests.post(
    "https://api.mercadolibre.com/oauth/token",
    data={
        "grant_type":    "authorization_code",
        "client_id":     APP_ID,
        "client_secret": CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
    },
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)

if resp.status_code != 200:
    print(f"Error {resp.status_code}: {resp.text}")
    sys.exit(1)

data          = resp.json()
access_token  = data["access_token"]
refresh_token = data["refresh_token"]

print(f"\n✓ Token obtenido correctamente")

env_path = os.path.join(os.path.dirname(__file__), ".env")
existing = []
if os.path.exists(env_path):
    with open(env_path) as f:
        existing = [l for l in f.readlines() if not any(
            l.startswith(k) for k in
            ["ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_ACCESS_TOKEN", "ML_REFRESH_TOKEN"]
        )]

with open(env_path, "w") as f:
    f.writelines(existing)
    f.write(f"ML_CLIENT_ID={APP_ID}\n")
    f.write(f"ML_CLIENT_SECRET={CLIENT_SECRET}\n")
    f.write(f"ML_ACCESS_TOKEN={access_token}\n")
    f.write(f"ML_REFRESH_TOKEN={refresh_token}\n")

print("Tokens guardados en .env")
print("Ya puedes correr: python3 main.py --test")
