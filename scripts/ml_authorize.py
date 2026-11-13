"""One-time Mercado Libre user authorization.

Usage: python scripts/ml_authorize.py <CODE> <REDIRECT_URI>

Exchanges the OAuth code for a user token, checks that /highlights works with it, and seeds the
encrypted, rotating refresh token in the database (the bot renews it from there). Prints no secrets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys

import requests
from cryptography.fernet import Fernet
from dotenv import dotenv_values

sys.path.insert(0, ".")
from app.database import Database  # noqa: E402

API = "https://api.mercadolibre.com"


def main(code: str, redirect_uri: str) -> int:
    env = dotenv_values(".env")
    key = env.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        print("Falta TOKEN_ENCRYPTION_KEY en .env")
        return 2
    r = requests.post(f"{API}/oauth/token", data={
        "grant_type": "authorization_code", "client_id": env["ML_CLIENT_ID"],
        "client_secret": env["ML_CLIENT_SECRET"], "code": code.strip(), "redirect_uri": redirect_uri.strip(),
    }, headers={"Accept": "application/json"}, timeout=20)
    payload = r.json()
    if not r.ok or "refresh_token" not in payload:
        print(f"Mercado Libre rechazó el código (HTTP {r.status_code}): {payload.get('message') or payload.get('error')}")
        return 1
    access, refresh = payload["access_token"], payload["refresh_token"]
    h = {"Authorization": f"Bearer {access}"}
    checks = {
        "highlights": requests.get(f"{API}/highlights/MCO/category/MCO1051", headers=h, timeout=20).status_code,
        "usuario": requests.get(f"{API}/users/me", headers=h, timeout=20).status_code,
    }
    print("Comprobación con token de usuario:", checks)
    cipher = Fernet(key.encode())
    db = Database(env["DATABASE_URL"])
    expires = datetime.now(timezone.utc) + timedelta(seconds=max(int(payload.get("expires_in", 21600)) - 60, 60))
    with db._transaction() as conn:
        conn.execute(db._sql("DELETE FROM integration_credentials WHERE provider='mercadolibre'"))
        conn.execute(
            db._sql(
                "INSERT INTO integration_credentials(provider, access_token_enc, refresh_token_enc, expires_at, updated_at)"
                " VALUES('mercadolibre', ?, ?, ?, ?)"
            ),
            (cipher.encrypt(access.encode()).decode(), cipher.encrypt(refresh.encode()).decode(),
             expires.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    print("Token de usuario guardado (cifrado en la base). highlights OK" if checks["highlights"] == 200
          else "Token guardado, pero highlights sigue bloqueado para esta app.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
