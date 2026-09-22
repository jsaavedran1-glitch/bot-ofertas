"""
Renueva el ML_ACCESS_TOKEN via client_credentials y lo escribe en .env (local)
o imprime para que GitHub Actions lo capture como variable de entorno.
"""
import os, requests, sys

resp = requests.post(
    "https://api.mercadolibre.com/oauth/token",
    data={
        "grant_type":    "client_credentials",
        "client_id":     os.environ["ML_CLIENT_ID"],
        "client_secret": os.environ["ML_CLIENT_SECRET"],
    },
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    timeout=15,
)

if resp.status_code != 200:
    print(f"[ML] Error renovando token: {resp.status_code} {resp.text}", file=sys.stderr)
    sys.exit(1)

token = resp.json()["access_token"]

# En local: actualiza .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    lines = open(env_path).readlines()
    lines = [l for l in lines if not l.startswith("ML_ACCESS_TOKEN=")]
    lines.append(f"ML_ACCESS_TOKEN={token}\n")
    open(env_path, "w").writelines(lines)
    print("[ML] Token renovado y guardado en .env")
else:
    # En GitHub Actions: exportar como variable de entorno para el siguiente step
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with open(github_env, "a") as f:
            f.write(f"ML_ACCESS_TOKEN={token}\n")
    print("[ML] Token renovado")
