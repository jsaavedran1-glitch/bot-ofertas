"""Brand color of the store a deal comes from, read from the store's own homepage.

The post then carries that color (store chip, frame around the product, a tint in the background), so
an Alkosto deal doesn't look like a Falabella one. The homepage is read only when STORE_COLOR_LOOKUP is
"true" (the workflow sets it); otherwise, and whenever the page gives no usable color, a known color
for the store is used, or None (the post keeps its rotating theme).
"""
from __future__ import annotations

import colorsys
import os
import re
from collections import Counter
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, UnidentifiedImageError

RGB = tuple[int, int, int]
# Fallbacks when the homepage gives nothing usable (Éxito and Falabella per their logos).
KNOWN = {
    "exito": "#F4D90C",
    "falabella": "#AAD500",
    "amazon": "#FF9900",
    "mercadolibre": "#FFE600",
    "nike": "#111111",
}
_META = (
    re.compile(r"""<meta[^>]+name=["'](?:theme-color|msapplication-TileColor)["'][^>]*content=["'](#[0-9a-f]{3,6})""", re.I),
    re.compile(r"""<meta[^>]+content=["'](#[0-9a-f]{3,6})["'][^>]*name=["'](?:theme-color|msapplication-TileColor)""", re.I),
)
_ICON = re.compile(r"""<link[^>]+rel=["'][^"']*icon[^"']*["'][^>]*href=["']([^"']+)""", re.I)
_HEADERS = {"User-Agent": "Mozilla/5.0 (OjoAlPrecio bot; +https://www.facebook.com/profile.php?id=61594722597967)"}
_cache: dict[str, RGB | None] = {}


def hex_rgb(value: str) -> RGB:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _usable(color: RGB | None) -> RGB | None:
    """A brand color, not page chrome: saturated enough and not white, or a clear black."""
    if color is None:
        return None
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in color))
    if l < 0.12:
        return color  # black brands (e.g. Nike)
    if l > 0.93 or s < 0.25:
        return None
    return color


def dominant(image: Image.Image) -> RGB | None:
    """Most common saturated color of a logo or icon, ignoring transparent, white and grey pixels."""
    img = image.convert("RGBA").resize((48, 48))
    counts: Counter = Counter()
    for r, g, b, a in img.getdata():
        if a < 128:
            continue
        if _usable((r, g, b)) is None or max(r, g, b) < 30:
            continue
        counts[(r // 16 * 16 + 8, g // 16 * 16 + 8, b // 16 * 16 + 8)] += 1
    return counts.most_common(1)[0][0] if counts else None


def _from_homepage(home: str, session: requests.Session) -> RGB | None:
    html = session.get(home, headers=_HEADERS, timeout=(4, 8)).text[:400_000]
    for pattern in _META:
        match = pattern.search(html)
        if match and _usable(hex_rgb(match.group(1))):
            return hex_rgb(match.group(1))
    icon = _ICON.search(html)
    for href in ([icon.group(1)] if icon else []) + ["/favicon.ico"]:
        try:
            resp = session.get(urljoin(home, href), headers=_HEADERS, timeout=(4, 8))
            if resp.ok and len(resp.content) < 2_000_000:
                color = dominant(Image.open(BytesIO(resp.content)))
                if color:
                    return color
        except (requests.RequestException, UnidentifiedImageError, OSError, ValueError):
            continue
    return None


def store_color(source: str, url: str = "", session: requests.Session | None = None) -> RGB | None:
    """The store's brand color, looked up once per run."""
    if source in _cache:
        return _cache[source]
    color = None
    parsed = urlparse(url)
    if os.getenv("STORE_COLOR_LOOKUP", "").lower() == "true" and parsed.scheme == "https" and parsed.netloc:
        try:
            color = _from_homepage(f"https://{parsed.netloc}/", session or requests.Session())
        except (requests.RequestException, ValueError):
            color = None
    if color is None and source in KNOWN:
        color = hex_rgb(KNOWN[source])
    _cache[source] = color
    return color


def luminance(color: RGB) -> float:
    """Perceived brightness 0..1 (decides dark or light text on the color)."""
    r, g, b = (c / 255 for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_on(color: RGB | None, dark: str, light: str) -> str:
    return dark if color is None or luminance(color) > 0.5 else light


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))
