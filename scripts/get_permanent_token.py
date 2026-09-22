#!/usr/bin/env python3
"""
Convierte un Short-Lived User Token en un Page Token permanente.

Uso:
    python3 scripts/get_permanent_token.py

El script lee FB_APP_ID, FB_APP_SECRET y FB_PAGE_ID del .env,
pide el Short-Lived User Token por stdin (no queda en historial),
y escribe el nuevo FB_PAGE_TOKEN al .env sin imprimirlo.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key

ENV_PATH = Path(__file__).parent.parent / ".env"
GRAPH = "https://graph.facebook.com/v26.0"


def main() -> int:
    env = dotenv_values(ENV_PATH)
    app_id = env.get("FB_APP_ID", "")
    app_secret = env.get("FB_APP_SECRET", "")
    page_id = env.get("FB_PAGE_ID", "")

    if not all([app_id, app_secret, page_id]):
        print("ERROR: Faltan FB_APP_ID, FB_APP_SECRET o FB_PAGE_ID en .env", file=sys.stderr)
        return 1

    print("Pega tu Short-Lived User Token del Graph API Explorer:")
    short_token = getpass.getpass("Token: ").strip()
    if not short_token:
        print("Token vacío, saliendo.", file=sys.stderr)
        return 1

    # Step 1: exchange for long-lived user token (60 days)
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    }, timeout=15)
    if not r.ok:
        print(f"ERROR al obtener Long-Lived User Token: {r.text}", file=sys.stderr)
        return 1
    long_user_token = r.json().get("access_token", "")
    expires_in = r.json().get("expires_in", "?")
    print(f"Long-Lived User Token obtenido (expira en {expires_in}s ≈ 60 días).")

    # Step 2: get permanent page token from /me/accounts
    r2 = requests.get(f"{GRAPH}/me/accounts", params={
        "access_token": long_user_token,
        "fields": "id,name,access_token",
    }, timeout=15)
    if not r2.ok:
        print(f"ERROR al obtener cuentas: {r2.text}", file=sys.stderr)
        return 1

    pages = r2.json().get("data", [])
    match = next((p for p in pages if p["id"] == page_id), None)
    if not match:
        names = ", ".join(f"{p['name']} ({p['id']})" for p in pages)
        print(f"ERROR: página {page_id} no encontrada. Páginas disponibles: {names}", file=sys.stderr)
        return 1

    page_token = match["access_token"]
    set_key(str(ENV_PATH), "FB_PAGE_TOKEN", page_token)
    print(f"✓ Page Token permanente guardado en .env para '{match['name']}'.")
    print("  Este token NO expira mientras no cambies tu contraseña de Facebook")
    print("  ni revoques el acceso de la app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
