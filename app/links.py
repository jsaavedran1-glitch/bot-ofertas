"""Per-post tags on affiliate links, so the network's own report says which post brought each click and sale.

Awin links accept `clickref` (shown in Awin's click and transaction reports). No redirects of our own: Meta's
recommendation rules look badly on link cloaking, and the store links stay exactly what the stores gave us.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def tracked(url: str, ref: str) -> str:
    """`url` with `ref` as Awin's clickref when it is an Awin link; any other link is returned unchanged."""
    parts = urlsplit(url)
    if not parts.hostname or not parts.hostname.endswith("awin1.com"):
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "clickref"]
    query.append(("clickref", ref[:50]))
    return urlunsplit(parts._replace(query=urlencode(query)))


def post_ref(kind: str, candidate_id: str) -> str:
    """'reel-1a2b3c4d': the kind of post and the deal, short enough for every network's sub-id field."""
    return f"{kind}-{candidate_id[:8]}"
