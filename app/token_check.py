"""Read-only check of the Page token: which permissions it carries, whether an Instagram account is linked and
whether Meta hands over views. Prints plain lines for the workflow log; never the token itself."""
from __future__ import annotations

import requests

# What each permission is for in this bot (Graph API v26).
NEEDED = (
    ("pages_show_list", "ver la página"),
    ("pages_manage_posts", "publicar en la página"),
    ("pages_read_engagement", "leer reacciones, comentarios y publicaciones"),
    ("read_insights", "leer vistas y cuentas (estadísticas)"),
    ("instagram_basic", "ver la cuenta de Instagram vinculada"),
    ("instagram_content_publish", "publicar en Instagram"),
)


def _get(session: requests.Session, url: str, params: dict) -> dict:
    try:
        payload = session.get(url, params=params, timeout=(5, 30)).json()
    except (requests.RequestException, ValueError) as exc:
        return {"error": {"code": type(exc).__name__}}
    return payload if isinstance(payload, dict) else {"error": {"code": "respuesta no válida"}}


def _code(payload: dict) -> str:
    return str((payload.get("error") or {}).get("code", ""))


def check_token(page_id: str, token: str, version: str, session: requests.Session | None = None) -> list[str]:
    session = session or requests.Session()
    base = f"https://graph.facebook.com/{version}"
    lines: list[str] = []

    debug = _get(session, f"{base}/debug_token", {"input_token": token, "access_token": token})
    data = debug.get("data") if isinstance(debug.get("data"), dict) else None
    if data is None:
        lines.append(f"Permisos: Meta no deja ver la lista con este token (código {_code(debug)}); "
                     "se comprueban con las pruebas de abajo.")
    else:
        kind = {"PAGE": "de página", "USER": "de usuario"}.get(str(data.get("type", "")).upper(), str(data.get("type", "?")))
        expires = data.get("expires_at")
        lines.append(f"Token {kind} · {'válido' if data.get('is_valid') else 'NO válido'} · "
                     f"{'no vence' if not expires else 'vence (timestamp ' + str(expires) + ')'}")
        scopes = set(data.get("scopes") or [])
        for scope, purpose in NEEDED:
            lines.append(f"  {'OK ' if scope in scopes else 'FALTA'} {scope}: {purpose}")

    page = _get(session, f"{base}/{page_id}", {
        "fields": "name,fan_count,followers_count,instagram_business_account{id,username}", "access_token": token})
    if "error" in page:
        lines.append(f"Página: no se pudo leer (código {_code(page)}).")
    else:
        lines.append(f"Página: {page.get('name', '?')} · {page.get('fan_count', '?')} Me gusta · "
                     f"{page.get('followers_count', '?')} seguidores")
        if isinstance(page.get("fan_count"), int) and page["fan_count"] < 100:
            lines.append("  Meta solo da estadísticas de página con 100 o más Me gusta.")
        account = page.get("instagram_business_account") or {}
        lines.append(f"Instagram: vinculada @{account.get('username') or account.get('id')}" if account else
                     "Instagram: ninguna cuenta profesional vinculada a la página (o el token no tiene instagram_basic).")

    probe = _get(session, f"{base}/{page_id}/posts", {
        "fields": "insights.metric(post_media_view,post_total_media_view_unique)", "limit": 5, "access_token": token})
    if "error" in probe:
        lines.append(f"Vistas: Meta rechazó la lectura (código {_code(probe)}).")
    else:
        posts = probe.get("data") or []
        read = [p for p in posts if (p.get("insights") or {}).get("data")]
        if not posts:
            lines.append("Vistas: no hay publicaciones para probar.")
        elif read:
            lines.append(f"Vistas: se leen ({len(read)} de {len(posts)} publicaciones recientes con datos).")
        else:
            lines.append("Vistas: Meta responde vacío (sin el permiso read_insights pasa esto).")
    return lines
