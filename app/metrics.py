"""How our own posts do: reactions, comments and shares per post, and a weekly report by type, store and hour.

Counts come from the page's /posts edge (one request per 100 posts; the `ids` parameter is gone in Graph
v26). Reels appear there as a post whose attachment points to the reel's video id, which is the id we
store. Comments written by the page itself (the link we leave under every post) are not counted.
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

    @property
    def interactions(self) -> int:
        return self.reactions + self.comments + self.shares


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


def _table(title: str, groups: dict[str, list[PostStats]]) -> list[str]:
    lines = [f"## {title}", "", "| | Posts | Reacciones | Comentarios | Compartidos | Interacciones por post |",
             "|---|---:|---:|---:|---:|---:|"]
    ranked = sorted(groups.items(), key=lambda kv: -sum(p.interactions for p in kv[1]) / max(1, len(kv[1])))
    for name, posts in ranked:
        r, c, s = (sum(getattr(p, f) for p in posts) for f in ("reactions", "comments", "shares"))
        lines.append(f"| {name} | {len(posts)} | {r} | {c} | {s} | {_avg(r + c + s, len(posts))} |")
    return lines + [""]


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
    lines += ["## Las 5 con más interacciones", ""]
    days = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
    for i, p in enumerate(sorted(posts, key=lambda p: (-p.interactions, -p.reactions))[:5], 1):
        local = p.published_at.astimezone(BOGOTA)
        where = names.get(p.source, p.source) if p.kind in {"oferta", "reel", "promo"} else ""
        lines.append(f"{i}. **{p.interactions}** ({_n(p.reactions, 'reacción')}, {_n(p.comments, 'comentario')}, "
                     f"{_n(p.shares, 'compartido')})"
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
             f"{_avg(sum(p.interactions for p in week), len(week))})", "", "## Operación", ""]
    kinds = Counter(published)
    lines.append("- Publicadas: " + (", ".join(
        f"{n} {PLURALS.get(k, (k, k))[0 if n == 1 else 1]}" for k, n in kinds.most_common()) or "ninguna"))
    if len(posts) < len(published):
        lines.append(f"- Con métricas: {len(posts)} (las demás se leen en la próxima actualización)")
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
        lines += ["## Las 3 con más interacciones", ""]
        for i, p in enumerate(sorted(posts, key=lambda p: (-p.interactions, -p.reactions))[:3], 1):
            local = p.published_at.astimezone(BOGOTA)
            lines.append(f"{i}. **{p.interactions}** · {KIND_NAMES.get(p.kind, p.kind)} · "
                         f"{names.get(p.source, p.source)} · {local:%H:%M} · https://www.facebook.com/{p.post_id}")
        lines.append("")
    return "\n".join(lines) + "\n"
