"""Read-only export for analysis: every post with its counts, deal details and Meta insights, page insights and
operations (errors, new candidates). Nothing is published or changed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from app.metrics import fetch_page_stats

BOGOTA = ZoneInfo("America/Bogota")
# Meta renamed and retired several insights over 2025-26; each one is tried and kept only if it answers.
POST_METRICS = ("post_media_view", "post_total_media_view_unique", "post_impressions_unique", "post_impressions",
                "post_clicks", "post_reactions_by_type_total")
VIDEO_METRICS = ("total_video_views", "total_video_views_unique", "total_video_avg_time_watched",
                 "post_video_views", "blue_reels_play_count", "post_impressions_unique")
PAGE_METRICS = ("page_media_view", "page_total_media_view_unique", "page_impressions_unique", "page_impressions",
                "page_post_engagements", "page_follows", "page_daily_follows_unique", "page_daily_unfollows_unique",
                "page_views_total", "page_fans")


def _get(session: requests.Session, url: str, params: dict) -> dict:
    try:
        payload = session.get(url, params=params, timeout=(5, 30)).json()
    except (requests.RequestException, ValueError) as exc:
        return {"error": {"message": type(exc).__name__}}
    return payload if isinstance(payload, dict) else {"error": {"message": "respuesta no válida"}}


def _err(payload: dict) -> str:
    e = payload.get("error") or {}
    return f"{e.get('code', '')} {e.get('message', '')}".strip()[:160]


def _working(session, base: str, object_id: str, metrics, token: str, extra: dict | None = None) -> tuple[list, dict]:
    ok, failed = [], {}
    for m in metrics:
        p = _get(session, f"{base}/{object_id}/insights", {"metric": m, "access_token": token, **(extra or {})})
        if "error" in p:
            failed[m] = _err(p)
        else:
            ok.append(m)
    return ok, failed


def _values(payload: dict) -> dict:
    out = {}
    for item in payload.get("data", []):
        vals = item.get("values") or []
        out[item.get("name")] = vals[-1].get("value") if vals else None
    return out


def _deal_rows(database, since: datetime) -> dict[str, dict]:
    sql = database._sql(
        "SELECT p.external_post_id AS post_id, p.platform, c.source, c.title, c.price_minor, c.original_price_minor,"
        " c.currency, c.score, c.affiliate, c.rating, c.reviews, c.observed_at FROM publications p"
        " JOIN candidates c ON c.id = p.candidate_id WHERE p.published_at>=?")
    rows = {}
    with database._connection() as conn:
        for r in conn.execute(sql, (since.astimezone(timezone.utc).isoformat(),)).fetchall():
            r = dict(r)
            pid = str(r.pop("post_id")).split(":")[0]
            orig, price = r.get("original_price_minor"), r.get("price_minor")
            r["discount_pct"] = round(100 * (orig - price) / orig) if orig and price and orig > price else None
            r["observed_at"] = str(r["observed_at"])
            rows.setdefault(pid, r)
    return rows


def _keys(database, table: str, column: str, since: datetime) -> dict[str, str]:
    sql = database._sql(f"SELECT external_post_id, {column} AS k FROM {table} WHERE published_at>=?")
    with database._connection() as conn:
        return {str(r["external_post_id"]): str(r["k"]) for r in
                conn.execute(sql, (since.astimezone(timezone.utc).isoformat(),)).fetchall()}


def _ops(database, since: datetime) -> dict:
    cutoff = since.astimezone(timezone.utc).isoformat()
    with database._connection() as conn:
        errors = [dict(r) for r in conn.execute(database._sql(
            "SELECT e.created_at, e.error_kind, e.message, c.source FROM publication_errors e"
            " LEFT JOIN candidates c ON c.id = e.candidate_id WHERE e.created_at>=? ORDER BY e.created_at"), (cutoff,)).fetchall()]
        created = [dict(r) for r in conn.execute(database._sql(
            "SELECT source, created_at, status, score FROM candidates WHERE created_at>=?"), (cutoff,)).fetchall()]
        statuses = [dict(r) for r in conn.execute(database._sql(
            "SELECT status, source, COUNT(*) AS n FROM candidates GROUP BY status, source")).fetchall()]
    for row in errors + created:
        row["created_at"] = str(row["created_at"])
    return {"errors": errors, "candidates_created": created, "candidate_status": statuses}


def export_stats(settings, database, days: int = 30, path: Path = Path("generated") / "estadisticas.json",
                 session: requests.Session | None = None) -> Path:
    session = session or requests.Session()
    token, version, page = settings.fb_page_token, settings.meta_graph_api_version, settings.fb_page_id
    base = f"https://graph.facebook.com/{version}"
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    ours = database.posts_since(since)
    counts = fetch_page_stats(page, token, version, since - timedelta(hours=1), session=session, max_pages=40)
    database.save_metrics([(pid, kind, src, at, *(counts[pid][k] for k in ("reactions", "comments", "shares")))
                           for pid, kind, src, at in ours if pid in counts])
    deals = _deal_rows(database, since)
    promos = _keys(database, "promo_publications", "promo_key", since)
    contents = _keys(database, "content_publications", "content_key", since)

    sample_post = next((pid for pid, kind, *_ in ours if kind != "reel"), None)
    sample_reel = next((pid for pid, kind, *_ in ours if kind == "reel"), None)
    post_ok, post_failed = _working(session, base, sample_post, POST_METRICS, token) if sample_post else ([], {})
    reel_ok, reel_failed = _working(session, base, sample_reel, VIDEO_METRICS, token) if sample_reel else ([], {})

    posts = []
    for pid, kind, src, at in ours:
        c = counts.get(pid)
        row = {"post_id": pid, "kind": kind, "source": src,
               "published_at": at.astimezone(BOGOTA).isoformat(timespec="minutes"),
               "found_on_page": c is not None, **(c or {"reactions": None, "comments": None, "shares": None})}
        row.update(deals.get(pid, {}))
        if pid in promos:
            row["promo_key"] = promos[pid]
        if pid in contents:
            row["content_key"] = contents[pid]
        wanted = reel_ok if kind == "reel" else post_ok
        if wanted:
            p = _get(session, f"{base}/{pid}/insights", {"metric": ",".join(wanted), "access_token": token})
            row["insights"] = _values(p) if "error" not in p else {"error": _err(p)}
        posts.append(row)

    page_ok, page_failed = _working(session, base, page, PAGE_METRICS, token, {"period": "day"})
    page_days: dict[str, dict] = {}
    if page_ok:
        p = _get(session, f"{base}/{page}/insights", {"metric": ",".join(page_ok), "period": "day",
                 "since": int(since.timestamp()), "until": int(now.timestamp()), "access_token": token})
        for item in p.get("data", []):
            for v in item.get("values", []):
                page_days.setdefault(v.get("end_time", "")[:10], {})[item.get("name")] = v.get("value")
    fields = _get(session, f"{base}/{page}", {"fields": "name,followers_count,fan_count", "access_token": token})
    stories = _get(session, f"{base}/{page}/stories", {"since": int(since.timestamp()), "limit": 100, "access_token": token})

    data = {
        "generated_at": now.astimezone(BOGOTA).isoformat(timespec="minutes"), "days": days,
        "page": {k: v for k, v in fields.items() if k != "id"},
        "page_insights_daily": dict(sorted(page_days.items())),
        "stories": stories.get("data", []) if "error" not in stories else {"error": _err(stories)},
        "metrics_available": {"post": post_ok, "reel": reel_ok, "page": page_ok},
        "metrics_unavailable": {"post": post_failed, "reel": reel_failed, "page": page_failed},
        "posts": posts,
        "operations": _ops(database, since),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"Exportación: {len(posts)} publicaciones, {len(page_days)} días de la página -> {path}")
    return path
