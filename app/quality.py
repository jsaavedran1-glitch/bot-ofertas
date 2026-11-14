"""Checks before publishing, against what Meta's Recommendation Guidelines don't recommend.

Meta allows but may not recommend clickbait, engagement bait (asking for reactions, comments, shares or tags)
and low-quality or unoriginal content. The bot's own templates avoid these; these checks catch what Claude
writes and what a store sends (product photos so small they look pixelated once enlarged on the card).
"""
from __future__ import annotations

import re

ENGAGEMENT_BAIT = (
    r"\b(comenta|escribe|responde)\s+[\"“'«]?(s[ií]|yo|quiero|am[eé]n|listo|info|precio)\b",
    r"\betiqu[eé]ta(lo|la|le)?\s+a\b", r"\bmenciona a\b",
    r"\bcomp[aá]rte(lo|la)\b", r"\bcomparte (esto|esta|este|con)\b",
    r"\bdale (like|me gusta|coraz[oó]n|amor)\b", r"\breacciona\b", r"\bvota (con|por)\b",
    r"\bsi (te gusta|quieres|lo quieres)\b[^.?!\n]{0,40}\b(like|comenta|comparte|reacciona|escribe)\b",
)
CLICKBAIT = (
    r"\bno (lo |la )?compres sin\b", r"\bno (lo )?vas a creer\b", r"\bno creer[aá]s\b",
    r"\bte (va a |van a )?sorprender\b", r"\bprecios? de locura\b", r"\b[uú]ltima oportunidad\b",
    r"\bse acaba(n)? ya\b", r"\bcorre antes de que\b", r"\bnadie te (lo )?dice\b",
)
MAX_UPSCALE = 2.5  # a store photo enlarged more than this on the card looks pixelated


def caption_issues(text: str, ignore: tuple[str, ...] = ()) -> list[str]:
    """Why a text could be judged engagement bait or clickbait; empty when it's fine.
    `ignore` removes product names first, so a title like "Impresora de etiqueta a color" doesn't count."""
    low = text.lower()
    for name in ignore:
        if name:
            low = low.replace(name.lower(), " ")
    issues = [f"pide interacción: «{m.group(0)}»" for p in ENGAGEMENT_BAIT if (m := re.search(p, low))]
    return issues + [f"clickbait: «{m.group(0)}»" for p in CLICKBAIT if (m := re.search(p, low))]


def photo_issue(size: tuple[int, int], box: tuple[int, int]) -> str | None:
    """Why the product photo would look bad once fitted into `box`; None when it's sharp enough."""
    scale = min(box[0] / size[0], box[1] / size[1])
    if scale > MAX_UPSCALE:
        return f"la foto del producto es muy pequeña ({size[0]}×{size[1]} px) y se vería pixelada"
    return None


def names_of(deals) -> tuple[str, ...]:
    """Product titles and short names shown in a caption, to skip them in caption_issues."""
    from app.models import display_name

    return tuple(n for d in deals for n in (d.title, display_name(d)))


def ensure_recommendable(caption: str, ignore: tuple[str, ...] = ()) -> None:
    """Stops a post whose text Meta wouldn't recommend; the slot moves on to the next post."""
    if issues := caption_issues(caption, ignore):
        raise ValueError("Texto no recomendable para Meta: " + "; ".join(issues))
