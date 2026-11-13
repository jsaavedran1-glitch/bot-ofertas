"""Posts that are not deals: "Dato útil" (verified tips for shoppers) and "Noticia" (official news, summarized).

On the competitor's page these got 10 to 100 times the reactions of a plain offer. Tips come from a bank
checked against official sources (assets/content/datos_utiles.json); news come from official/brand feeds
(assets/content/fuentes_noticias.json) and Claude only rewrites what the chosen item says. Both carry
their source, and the link goes in the first comment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw

from app.image_renderer import MUTED, NAVY, POST_CANVAS, WHITE, YELLOW, OfferImageRenderer

CONTENT_DIR = Path(__file__).parent.parent / "assets" / "content"
TIPS_PATH = CONTENT_DIR / "datos_utiles.json"
FEEDS_PATH = CONTENT_DIR / "fuentes_noticias.json"
NEWS_MAX_AGE_HOURS = 36
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ContentPost:
    kind: str  # "dato" or "noticia"
    key: str
    title: str
    points: tuple[str, ...]
    question: str
    hashtags: tuple[str, ...]
    source: str
    url: str


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    summary: str
    link: str
    published: datetime


def load_tips(path: Path = TIPS_PATH, today: date | None = None) -> list[ContentPost]:
    """The tip bank, minus tips past their "vigente_hasta" date (e.g. this year's Black Friday dates)."""
    if not path.is_file():
        return []
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("America/Bogota")).date()
    tips = []
    for t in json.loads(path.read_text(encoding="utf-8")):
        if t.get("vigente_hasta") and date.fromisoformat(t["vigente_hasta"]) < today:
            continue
        tips.append(ContentPost("dato", t["key"], t["titulo"], tuple(t["puntos"]), t.get("pregunta", ""),
                                tuple(t.get("hashtags", ())), t["fuente"], t["url"]))
    return tips


def next_tip(tips: list[ContentPost], last_posted: dict[str, datetime], cooldown_days: int = 14,
             now: datetime | None = None) -> ContentPost | None:
    """Tips never posted come first (in file order), then the one posted longest ago; none from the last
    `cooldown_days` days."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=cooldown_days)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ready = [t for t in tips if last_posted.get(t.key, oldest) < cutoff]
    return min(ready, key=lambda t: last_posted.get(t.key, oldest)) if ready else None


def _text(element: ET.Element | None) -> str:
    raw = unescape("".join(element.itertext()) if element is not None else "")
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(raw)).split())


def _date(value: str) -> datetime | None:
    value = (value or "").strip()
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_feed(name: str, content: bytes) -> list[NewsItem]:
    """RSS 2.0 or Atom entries with title, link and date."""
    root = ET.fromstring(content)
    items = []
    for node in list(root.iter("item")) + list(root.iter(f"{_ATOM}entry")):
        title = _text(node.find("title")) or _text(node.find(f"{_ATOM}title"))
        link = (node.findtext("link") or "").strip()
        if not link:
            atom_link = node.find(f"{_ATOM}link")
            link = atom_link.get("href", "") if atom_link is not None else ""
        summary = (_text(node.find("description")) or _text(node.find(f"{_ATOM}summary"))
                   or _text(node.find(f"{_ATOM}content")))
        when = _date(node.findtext("pubDate") or node.findtext(f"{_ATOM}published")
                     or node.findtext(f"{_ATOM}updated") or "")
        if title and link.startswith("https://") and when:
            items.append(NewsItem(name, title, summary, link.split("?utm_")[0], when))
    return items


class NewsFeeds:
    def __init__(self, path: Path = FEEDS_PATH, session: requests.Session | None = None, now=None) -> None:
        self.feeds = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "OjoAlPrecioBot/1.0 (+https://www.facebook.com/profile.php?id=61594722597967)"
        self.now = now or (lambda: datetime.now(timezone.utc))

    def recent(self, skip: set[str]) -> list[NewsItem]:
        """Fresh items from every feed, newest first, leaving out links already posted. A failing feed is skipped."""
        cutoff = self.now() - timedelta(hours=NEWS_MAX_AGE_HOURS)
        items: list[NewsItem] = []
        for feed in self.feeds:
            try:
                response = self.session.get(feed["url"], timeout=(5, 15))
                response.raise_for_status()
                items += [i for i in parse_feed(feed["name"], response.content) if i.published >= cutoff and i.link not in skip]
            except (requests.RequestException, ET.ParseError) as exc:
                print(f"Advertencia noticias ({feed['name']}): {type(exc).__name__}")
        return sorted(items, key=lambda i: i.published, reverse=True)[:20]


def news_post(item: NewsItem, texts: dict) -> ContentPost:
    return ContentPost("noticia", item.link, texts["title"], tuple(texts["points"]), texts.get("question", ""),
                       tuple(texts.get("hashtags", ())), item.source, item.link)


def content_caption(post: ContentPost, link_in_comment: bool = True) -> str:
    icon = "💡" if post.kind == "dato" else "📢"
    lines = [f"{icon} {post.title}", ""]
    lines += [f"✔️ {p}" for p in post.points]
    where = "el enlace está en los comentarios 👇" if link_in_comment else post.url
    lines += ["", f"📌 Fuente: {post.source} ({where})"]
    if post.question:
        lines += ["", f"💬 {post.question}"]
    tags = " ".join(("#OjoAlPrecio",) + post.hashtags[:2])
    return "\n".join(lines + ["", tags])


class ContentRenderer(OfferImageRenderer):
    """4:5 text card in the brand style: tag, big headline, two or three points, source and footer."""

    def render_content(self, post: ContentPost, name: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        W, H = POST_CANVAS
        canvas = Image.new("RGB", POST_CANVAS, NAVY)
        draw = ImageDraw.Draw(canvas)
        for y in range(H):
            r = y / H
            draw.line((0, y, W, y), fill=(8 + int(10 * r), 24 + int(22 * r), 48 + int(40 * r)))

        self._brand(canvas, draw, 52, 92, size=72)
        tag, emoji = ("DATO ÚTIL", "💡") if post.kind == "dato" else ("NOTICIA", "📢")
        tfont = self._font(34, weight="Black")
        tw = int(draw.textlength(tag, font=tfont))
        draw.rounded_rectangle((52, 168, 52 + tw + 112, 236), radius=34, fill=YELLOW)
        self._paste_emoji(canvas, (68, 180), emoji, 44)
        draw.text((124, 202), tag, anchor="lm", font=tfont, fill=NAVY)

        hfont, lines = self._fit_lines(draw, post.title, W - 104, max_lines=4, start=82, minimum=52, weight="Black")
        y = 286
        line_h = int(hfont.size * 1.12)
        for line in lines:
            draw.text((52, y), line, font=hfont, fill=WHITE)
            y += line_h
        draw.rounded_rectangle((52, y + 18, 212, y + 30), radius=6, fill=YELLOW)
        y += 72

        space = H - 150 - 70 - y  # down to the source line above the footer
        n = max(1, len(post.points[:3]))
        box_h = min(210, (space - 24 * (n - 1)) // n)
        for i, point in enumerate(post.points[:3], start=1):
            draw.rounded_rectangle((52, y, W - 52, y + box_h), radius=30, fill=WHITE)
            draw.ellipse((78, y + box_h // 2 - 32, 142, y + box_h // 2 + 32), fill=YELLOW)
            draw.text((110, y + box_h // 2), str(i), anchor="mm", font=self._font(36, weight="Black"), fill=NAVY)
            for size in range(40, 25, -2):  # largest size whose lines fit inside the box with margin
                pfont, plines = self._fit_lines(draw, point, W - 52 - 172 - 40, max_lines=3, start=size, minimum=size,
                                                weight="Bold")
                ph = int(pfont.size * 1.18)
                if ph * len(plines) <= box_h - 44:
                    break
            ty = y + (box_h - ph * len(plines)) // 2
            for pl in plines:
                draw.text((172, ty), pl, font=pfont, fill=NAVY)
                ty += ph
            y += box_h + 24

        short = re.sub(r"\s*\([^)]*\)", "", post.source)  # "(Secretaría General del Senado)" stays in the caption
        sfont, slines = self._fit_lines(draw, f"Fuente: {short}", W - 108, max_lines=1, start=28, minimum=20,
                                        weight="SemiBold")
        draw.text((54, H - 172), slines[0], anchor="lm", font=sfont, fill=MUTED)
        self._footer(canvas, draw, W, H)
        output = self.output_dir / f"{name}.png"
        canvas.save(output, format="PNG", optimize=True)
        return output


__all__ = ["ContentPost", "ContentRenderer", "NewsFeeds", "NewsItem", "content_caption", "load_tips",
           "news_post", "next_tip", "parse_feed"]
