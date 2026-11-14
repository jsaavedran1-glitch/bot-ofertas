"""How our own posts do: reactions, comments and shares per post, views and accounts reached when the token can
read insights, and the daily and weekly reports by type, store and hour.

Counts come from the page's /posts edge (one request per 100 posts; the `ids` parameter is gone in Graph
v26). Reels appear there as a post whose attachment points to the reel's video id, which is the id we
store. Comments written by the page itself (the link we leave under every post) are not counted.

Views (post_media_view) and accounts (post_total_media_view_unique) come from the same edge with
`insights.metric(...)`; reel plays from /{video-id}/video_insights. Both need the read_insights permission on
the Page token; without it Meta answers with empty data, and the report says so instead of showing zeros.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BOGOTA = ZoneInfo("America/Bogota")
FIELDS = ("created_time,attachments{target{id}},reactions.summary(total_count).limit(0),"
          "comments.limit(50){from{id}},shares")
KIND_NAMES = {"oferta": "Oferta (foto)", "reel": "Reel", "promo": "Promo del día", "top5": "Top 5",
              "dato": "Dato útil", "noticia": "Noticia", "evento": "Cuenta regresiva", "cupones": "Cupones",
              "historial": "¿Descuento real?"}
PLURALS = {"oferta": ("oferta con foto", "ofertas con foto"), "reel": ("reel", "reels"),
           "promo": ("promo del día", "promos del día"), "top5": ("Top 5", "Top 5"), "dato": ("dato útil", "datos útiles"),
           "noticia": ("noticia", "noticias"), "evento": ("cuenta regresiva", "cuentas regresivas"),
           "cupones": ("publicación de cupones", "publicaciones de cupones"),
           "historial": ("historial de precio", "historiales de precio")}
BANDS = ((0, 9, "antes de 9 h"), (9, 12, "9–12 h"), (12, 17, "12–17 h"), (17, 20, "17–20 h"), (20, 24, "20–24 h"))


class MetricsError(RuntimeError):
    pass


@dataclass(frozen=True)
class PostStats:
    post_id: str
    kind: str
    source: str
    published_at: datetime
    reactions: int = 0
    comments: int = 0
    shares: int = 0
    views: int | None = None     # times shown (post_media_view); None = Meta gave nothing
    viewers: int | None = None   # distinct accounts (post_total_media_view_unique)
    plays: int | None = None     # reels only: plays (blue_reels_play_count)

    @property
    def interactions(self) -> int:
        return self.reactions + self.comments + self.shares


VIEW_METRICS = "post_media_view,post_total_media_view_unique"
REEL_METRICS = "blue_reels_play_count,post_impressions_unique"
NO_VIEWS = ("Meta no entrega vistas ni cuentas con el token actual (le falta el permiso read_insights o la página "
            "no tiene acceso a estadísticas). Comprueba el token con el modo «permisos».")


def _insight_values(payload: dict) -> dict[str, int]:
    out = {}
    for item in (payload or {}).get("data", []) or []:
        values = item.get("values") or []
        value = values[-1].get("value") if values else None
        if isinstance(value, (int, float)):
            out[item.get("name")] = int(value)
    return out


def fetch_views(page_id: str, token: str, version: str, since: datetime, reel_ids=(),
                session: requests.Session | None = None, max_pages: int = 10) -> tuple[dict[str, dict], str | None]:
    """({id: {"views", "viewers", "plays"}}, problem). `problem` explains why there are no views; never the token."""
    session = session or requests.Session()
    url = f"https://graph.facebook.com/{version}/{page_id}/posts"
    params: dict | None = {"fields": f"attachments{{target{{id}}}},insights.metric({VIEW_METRICS})", "limit": 100,
                           "since": int(since.timestamp()), "access_token": token}
    views: dict[str, dict] = {}
    seen = 0
    for _ in range(max_pages):
        try:
            payload = session.get(url, params=params, timeout=(5, 30)).json()
        except (requests.RequestException, ValueError) as exc:
            return views, f"Meta no respondió al leer vistas ({type(exc).__name__})."
        if not isinstance(payload, dict) or "error" in payload:
            code = (payload.get("error") or {}).get("code") if isinstance(payload, dict) else ""
            return views, f"Meta rechazó la lectura de vistas (código {code}). {NO_VIEWS}"
        for post in payload.get("data", []):
            seen += 1
            values = _insight_values(post.get("insights"))
            if not values:
                continue
            row = {"views": values.get("post_media_view"), "viewers": values.get("post_total_media_view_unique"),
                   "plays": None}
            views[post["id"]] = row
            for attachment in post.get("attachments", {}).get("data", []):
                target = attachment.get("target", {}).get("id")
                if target:
                    views.setdefault(target, dict(row))
        url = payload.get("paging", {}).get("next")
        params = None
        if not url:
            break
    for reel in reel_ids:
        try:
            payload = session.get(f"https://graph.facebook.com/{version}/{reel}/video_insights",
                                  params={"metric": REEL_METRICS, "access_token": token}, timeout=(5, 30)).json()
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(payload, dict) or "error" in payload:
            break  # the same answer for every reel (usually the permission): don't ask again for each one
        values = _insight_values(payload)
        if "blue_reels_play_count" in values:
            row = views.setdefault(reel, {"views": None, "viewers": None, "plays": None})
            row["plays"] = values["blue_reels_play_count"]
            if row["viewers"] is None:
                row["viewers"] = values.get("post_impressions_unique")
    if seen and not views:
        return views, NO_VIEWS
    return views, None


def fetch_page_stats(page_id: str, token: str, version: str, since: datetime,
                     session: requests.Session | None = None, max_pages: int = 10) -> dict[str, dict[str, int]]:
    """{id: counts} for the page's posts since `since`, keyed by post id and also by the reel's video id."""
    session = session or requests.Session()
    url = f"https://graph.facebook.com/{version}/{page_id}/posts"
    params: dict | None = {"fields": FIELDS, "limit": 100, "since": int(since.timestamp()), "access_token": token}
    stats: dict[str, dict[str, int]] = {}
    for _ in range(max_pages):
        try:
            payload = session.get(url, params=params, timeout=(5, 30)).json()
        except (requests.RequestException, ValueError) as exc:
            raise MetricsError(f"Meta no respondió ({type(exc).__name__})") from None
        if not isinstance(payload, dict) or "error" in payload:
            code = (payload.get("error") or {}).get("code") if isinstance(payload, dict) else ""
            raise MetricsError(f"Meta rechazó la lectura de métricas (código {code})")
        for post in payload.get("data", []):
            comments = post.get("comments", {}).get("data", [])
            counts = {
                "reactions": int(post.get("reactions", {}).get("summary", {}).get("total_count") or 0),
                "comments": sum(1 for c in comments if c.get("from", {}).get("id") != page_id),
                "shares": int(post.get("shares", {}).get("count") or 0),
            }
            stats[post["id"]] = counts
            for attachment in post.get("attachments", {}).get("data", []):
                target = attachment.get("target", {}).get("id")
                if target:
                    stats.setdefault(target, counts)
        url = payload.get("paging", {}).get("next")  # already carries every parameter
        params = None
        if not url:
            break
    return stats


def _n(count: int, word: str) -> str:
    """'1 compartido', '3 compartidos', '1 publicación', '2 publicaciones'."""
    if count == 1:
        return f"1 {word}"
    return f"{count} {word[:-2] + 'ones' if word.endswith('ón') else word + 's'}"


def _avg(total: int, n: int) -> str:
    return f"{total / n:.1f}".replace(".", ",") if n else "0"


def _mean(posts: list[PostStats], field: str) -> float | None:
    """Average of `field` over the posts Meta gave it for; None when it gave it for none."""
    values = [getattr(p, field) for p in posts if getattr(p, field) is not None]
    return sum(values) / len(values) if values else None


def _fmt(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}".replace(".", ",")


def has_views(posts: list[PostStats]) -> bool:
    return any(p.views is not None for p in posts)


def _table(title: str, groups: dict[str, list[PostStats]]) -> list[str]:
    """Counts per group. With views, two more columns and the groups ranked by views per post (the signal);
    without them, by interactions per post."""
    with_views = has_views([p for posts in groups.values() for p in posts])
    head, rule = "| | Posts | Reacciones | Comentarios | Compartidos | Interacciones por post |", "|---|---:|---:|---:|---:|---:|"
    if with_views:
        head, rule = head + " Vistas por post | Cuentas por post |", rule + "---:|---:|"
        ranked = sorted(groups.items(), key=lambda kv: -(_mean(kv[1], "views") or 0))
    else:
        ranked = sorted(groups.items(), key=lambda kv: -sum(p.interactions for p in kv[1]) / max(1, len(kv[1])))
    lines = [f"## {title}", "", head, rule]
    for name, posts in ranked:
        r, c, s = (sum(getattr(p, f) for p in posts) for f in ("reactions", "comments", "shares"))
        row = f"| {name} | {len(posts)} | {r} | {c} | {s} | {_avg(r + c + s, len(posts))} |"
        if with_views:
            row += f" {_fmt(_mean(posts, 'views'))} | {_fmt(_mean(posts, 'viewers'))} |"
        lines.append(row)
    return lines + [""]


def _views_line(posts: list[PostStats], week: list[PostStats] | None = None) -> str:
    line = (f"**{_fmt(_mean(posts, 'views'))} vistas por post** · {_fmt(_mean(posts, 'viewers'))} cuentas por post · "
            f"{sum(p.views or 0 for p in posts)} vistas en total")
    if week is not None:
        line += f" (últimos 7 días: {_fmt(_mean(week, 'views'))} vistas por post)"
    return line


def _reels_line(posts: list[PostStats]) -> str | None:
    reels = [p for p in posts if p.plays is not None]
    if not reels:
        return None
    total = sum(p.plays for p in reels)
    return f"- Reels: {total} reproducciones en {_n(len(reels), 'reel')} ({_fmt(total / len(reels))} por reel)"


def weekly_report(posts: list[PostStats], start: datetime, end: datetime,
                  previous: list[PostStats] | None = None, store_names: dict[str, str] | None = None) -> str:
    """Markdown for the week: totals, then by type, store and hour band, and the five best posts."""
    names = store_names or {}
    start_l, end_l = start.astimezone(BOGOTA), (end - timedelta(seconds=1)).astimezone(BOGOTA)
    total = sum(p.interactions for p in posts)
    lines = [f"# Informe semanal · Ojo al Precio",
             "",
             f"Del {start_l:%d/%m} al {end_l:%d/%m/%Y} · **{_n(len(posts), 'publicación')}** · "
             f"{_n(sum(p.reactions for p in posts), 'reacción')} · {_n(sum(p.comments for p in posts), 'comentario')} · "
             f"{_n(sum(p.shares for p in posts), 'compartido')} · **{_avg(total, len(posts))} interacciones por post**"]
    if previous:
        lines.append(f"Semana anterior: {_n(len(previous), 'publicación')}, "
                     f"{_avg(sum(p.interactions for p in previous), len(previous))} interacciones por post.")
    if has_views(posts):
        lines.append(_views_line(posts) + (f"; semana anterior: {_fmt(_mean(previous, 'views'))} vistas por post"
                                           if previous and has_views(previous) else "") + ".")
        reels = _reels_line(posts)
        if reels:
            lines.append(reels[2:] + ".")
    lines += ["", "_No cuentan los comentarios del propio bot (el enlace bajo cada post)._", ""]
    if not posts:
        return "\n".join(lines + ["Sin publicaciones en la semana."]) + "\n"

    by_kind: dict[str, list[PostStats]] = defaultdict(list)
    by_store: dict[str, list[PostStats]] = defaultdict(list)
    by_band: dict[str, list[PostStats]] = defaultdict(list)
    for p in posts:
        by_kind[KIND_NAMES.get(p.kind, p.kind)].append(p)
        if p.kind in {"oferta", "reel", "promo", "historial"}:
            by_store[names.get(p.source, p.source.capitalize())].append(p)
        hour = p.published_at.astimezone(BOGOTA).hour
        by_band[next(label for lo, hi, label in BANDS if lo <= hour < hi)].append(p)
    lines += _table("Por tipo", by_kind) + _table("Por tienda (ofertas, reels y promos)", by_store)
    lines += _table("Por franja horaria (Bogotá)", by_band)
    days = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
    if has_views(posts):
        lines += ["## Las 5 con más vistas", ""]
        best = sorted(posts, key=lambda p: (-(p.views or 0), -p.interactions))[:5]
    else:
        lines += ["## Las 5 con más interacciones", ""]
        best = sorted(posts, key=lambda p: (-p.interactions, -p.reactions))[:5]
    for i, p in enumerate(best, 1):
        local = p.published_at.astimezone(BOGOTA)
        where = names.get(p.source, p.source) if p.kind in {"oferta", "reel", "promo"} else ""
        seen = f"{p.views or 0} vistas, {p.viewers or 0} cuentas, " if p.views is not None else ""
        lines.append(f"{i}. **{p.views if p.views is not None else p.interactions}** ({seen}"
                     f"{_n(p.reactions, 'reacción')}, {_n(p.comments, 'comentario')}, {_n(p.shares, 'compartido')})"
                     f" · {KIND_NAMES.get(p.kind, p.kind)}{' · ' + where if where else ''}"
                     f" · {days[local.weekday()]} {local:%d %H:%M} · https://www.facebook.com/{p.post_id}")
    return "\n".join(lines) + "\n"


ENOUGH_INTERACTIONS = 15  # below this in 7 days, differences between hours or stores are noise


def _groups(posts: list[PostStats], names: dict[str, str]):
    by_kind: dict[str, list[PostStats]] = defaultdict(list)
    by_store: dict[str, list[PostStats]] = defaultdict(list)
    by_band: dict[str, list[PostStats]] = defaultdict(list)
    for p in posts:
        by_kind[KIND_NAMES.get(p.kind, p.kind)].append(p)
        if p.kind in {"oferta", "reel", "promo", "historial"}:
            by_store[names.get(p.source, p.source.capitalize())].append(p)
        hour = p.published_at.astimezone(BOGOTA).hour
        by_band[next(label for lo, hi, label in BANDS if lo <= hour < hi)].append(p)
    return by_kind, by_store, by_band


def daily_report(posts: list[PostStats], week: list[PostStats], ops: dict, day, names: dict[str, str] | None = None) -> str:
    """One day: operation first (planned vs published, failures, new deals, queue), then engagement."""
    names = names or {}
    days = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    total = sum(p.interactions for p in posts)
    published = ops.get("published_kinds")
    published = list(published) if published is not None else [p.kind for p in posts]
    lines = [f"# Informe diario · {days[day.weekday()]} {day:%d/%m/%Y}", "",
             f"**{len(published)} de {ops.get('planned', '?')} publicaciones** · {_n(sum(p.reactions for p in posts), 'reacción')} · "
             f"{_n(sum(p.comments for p in posts), 'comentario')} · {_n(sum(p.shares for p in posts), 'compartido')} · "
             f"**{_avg(total, len(posts))} interacciones por post** (últimos 7 días: "
             f"{_avg(sum(p.interactions for p in week), len(week))})"]
    if has_views(posts):
        lines.append(_views_line(posts, week))
    lines += ["", "## Operación", ""]
    kinds = Counter(published)
    lines.append("- Publicadas: " + (", ".join(
        f"{n} {PLURALS.get(k, (k, k))[0 if n == 1 else 1]}" for k, n in kinds.most_common()) or "ninguna"))
    if len(posts) < len(published):
        lines.append(f"- Con métricas: {len(posts)} (las demás se leen en la próxima actualización)")
    if posts and not has_views(posts):
        lines.append(f"- Vistas y cuentas: sin datos. {NO_VIEWS}")
    reels = _reels_line(posts)
    if reels:
        lines.append(reels)
    errors = ops.get("errors") or []
    if errors:
        common = Counter(e.split(":")[0][:70] for e in errors).most_common(4)
        lines.append(f"- Fallos: {len(errors)} (" + "; ".join(f"{n} × {msg}" for msg, n in common) + ")")
    else:
        lines.append("- Fallos: ninguno")
    new = ops.get("new_by_source") or {}
    if new:
        lines.append("- Ofertas nuevas encontradas: " + ", ".join(
            f"{names.get(s, s.capitalize())} {n}" for s, n in sorted(new.items(), key=lambda kv: -kv[1])))
    queue, per_day = ops.get("queue"), ops.get("planned")
    if queue is not None:
        days_left = f" (alcanza para ~{queue / per_day:.1f} días)".replace(".", ",") if per_day else ""
        lines.append(f"- Cola: {queue} productos distintos listos para publicar{days_left}")
    lines.append("")
    if posts:
        by_kind, by_store, by_band = _groups(posts, names)
        lines += _table("Por tipo", by_kind) + _table("Por tienda", by_store) + _table("Por franja horaria", by_band)
        if has_views(posts):
            lines += ["## Las 3 con más vistas", ""]
            best = sorted(posts, key=lambda p: (-(p.views or 0), -p.interactions))[:3]
        else:
            lines += ["## Las 3 con más interacciones", ""]
            best = sorted(posts, key=lambda p: (-p.interactions, -p.reactions))[:3]
        for i, p in enumerate(best, 1):
            local = p.published_at.astimezone(BOGOTA)
            score = (f"**{p.views or 0} vistas** · {p.viewers or 0} cuentas · {p.interactions} interacciones"
                     if p.views is not None else f"**{p.interactions}**")
            lines.append(f"{i}. {score} · {KIND_NAMES.get(p.kind, p.kind)} · "
                         f"{names.get(p.source, p.source)} · {local:%H:%M} · https://www.facebook.com/{p.post_id}")
        lines.append("")
    return "\n".join(lines) + "\n"
