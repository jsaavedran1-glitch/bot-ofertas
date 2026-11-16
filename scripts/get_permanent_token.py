#!/usr/bin/env python3
"""
Convierte un Short-Lived User Token en un Page Token permanente.

Uso:
    python3 scripts/get_permanent_token.py

El script lee FB_APP_ID, FB_APP_SECRET y FB_PAGE_ID del .env,
pide el Short-Lived User Token por stdin (no queda en historial),
y escribe el nuevo FB_PAGE_TOKEN al .env sin imprimirlo. Revisa que el token tenga los
permisos que usa el bot (read_insights, Instagram) y, si confirmas, lo guarda también como
secret FB_PAGE_TOKEN en GitHub (por stdin: nunca aparece en pantalla ni en el historial).
"""
from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key

ENV_PATH = Path(__file__).parent.parent / ".env"
GRAPH = "https://graph.facebook.com/v26.0"
REPO = "jsaavedran1-glitch/bot-ofertas"
NEEDED = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "pages_manage_engagement",
          "read_insights", "instagram_basic", "instagram_content_publish")


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

    # Permissions actually granted (the Explorer checkboxes)
    perms = requests.get(f"{GRAPH}/me/permissions", params={"access_token": long_user_token}, timeout=15).json()
    granted = {p["permission"] for p in perms.get("data", []) if p.get("status") == "granted"}
    missing = [p for p in NEEDED if p not in granted]
    if missing:
        print("⚠️  Al token le faltan estos permisos: " + ", ".join(missing))
        print("    Vuelve al Graph API Explorer, márcalos en 'Permissions', genera otro token y corre esto de nuevo.")
        if "read_insights" in missing:
            return 1
    else:
        print("✓ Permisos completos, incluido read_insights.")

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
        # Pages in a business portfolio (or the "new Pages experience") often don't show in /me/accounts,
        # but the page itself still hands out its token to an admin.
        direct = requests.get(f"{GRAPH}/{page_id}", params={"access_token": long_user_token,
                                                           "fields": "id,name,access_token"}, timeout=15).json()
        if direct.get("access_token"):
            match = direct
    if not match:
        debug = requests.get(f"{GRAPH}/debug_token", params={"input_token": long_user_token,
                                                             "access_token": f"{app_id}|{app_secret}"}, timeout=15).json()
        scopes = {g.get("scope"): g.get("target_ids", []) for g in debug.get("data", {}).get("granular_scopes", [])}
        names = ", ".join(f"{p['name']} ({p['id']})" for p in pages) or "ninguna"
        print(f"ERROR: el token no tiene acceso a la página {page_id}. Páginas que ve: {names}.", file=sys.stderr)
        print(f"  Páginas autorizadas por permiso: {scopes or 'sin detalle'}", file=sys.stderr)
        print("  Solución: en el Graph API Explorer pulsa de nuevo 'Generate Access Token' y, en la ventana de", file=sys.stderr)
        print("  Facebook, elige 'Editar acceso' (o 'Edit previous settings') y marca la página Ojo al Precio.", file=sys.stderr)
        print("  Si la página está en un portafolio comercial, agrega también el permiso business_management.", file=sys.stderr)
        return 1

    page_token = match["access_token"]
    set_key(str(ENV_PATH), "FB_PAGE_TOKEN", page_token)
    print(f"✓ Page Token permanente guardado en .env para '{match['name']}'.")
    print("  Este token NO expira mientras no cambies tu contraseña de Facebook")
    print("  ni revoques el acceso de la app.")
    if input("¿Guardarlo también en GitHub (secret FB_PAGE_TOKEN) para el bot? [s/n] ").strip().lower().startswith("s"):
        done = subprocess.run(["gh", "secret", "set", "FB_PAGE_TOKEN", "--repo", REPO], input=page_token, text=True,
                              capture_output=True)
        print("✓ Secret FB_PAGE_TOKEN actualizado en GitHub." if done.returncode == 0
              else f"ERROR con gh: {done.stderr.strip()[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
