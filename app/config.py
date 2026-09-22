from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí"}


def _parse_hours(value: str) -> frozenset[int]:
    """'7-9,12-14,19-22' -> {7, 8, 12, 13, 19, 20, 21} (end exclusive). Empty = all day."""
    hours: set[int] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        start, _, end = part.partition("-")
        hours.update(range(int(start), int(end)) if end else {int(start)})
    if any(h < 0 or h > 23 for h in hours):
        raise ValueError("PUBLISH_HOURS debe usar horas entre 0 y 23.")
    return frozenset(hours or range(24))


@dataclass(frozen=True)
class Settings:
    database_url: str
    post_mode: str
    min_discount_pct: int
    min_savings_cop: int
    max_posts_per_day: int
    max_posts_per_run: int
    candidate_max_age_hours: int
    repost_cooldown_days: int
    history_min_observations: int
    history_min_span_hours: int
    ml_queries: tuple[str, ...]
    ml_limit_per_query: int
    ml_client_id: str
    ml_client_secret: str
    ml_access_token: str
    ml_refresh_token: str
    token_encryption_key: str
    partner_feed_path: str
    usd_cop_rate: str
    fb_page_id: str
    fb_page_token: str
    meta_graph_api_version: str
    generated_dir: Path
    affiliate_disclosure: bool
    publish_hours: frozenset[int] = frozenset(range(24))
    link_in_comment: bool = False
    reel_hours: frozenset[int] = frozenset()
    stories: bool = False
    promo_hours: frozenset[int] = frozenset()
    min_minutes_between_posts: int = 0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("POST_MODE", "review").strip().lower()
        mode = {"manual": "review", "auto": "automatic"}.get(mode, mode)
        if mode not in {"review", "automatic"}:
            raise ValueError("POST_MODE debe ser 'review' o 'automatic'.")
        queries = tuple(
            item.strip()
            for item in os.getenv(
                "ML_QUERIES",
                "audifonos bluetooth,portatil,celular,televisor,electrodomesticos",
            ).split(",")
            if item.strip()
        )
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///ofertas.db").strip(),
            post_mode=mode,
            min_discount_pct=int(os.getenv("MIN_DISCOUNT_PCT", "20")),
            min_savings_cop=int(os.getenv("MIN_SAVINGS_COP", "20000")),
            max_posts_per_day=int(os.getenv("MAX_POSTS_PER_DAY", "5")),
            max_posts_per_run=int(os.getenv("MAX_POSTS_PER_RUN", "1")),
            candidate_max_age_hours=int(os.getenv("CANDIDATE_MAX_AGE_HOURS", "6")),
            repost_cooldown_days=int(os.getenv("REPOST_COOLDOWN_DAYS", "7")),
            history_min_observations=int(os.getenv("HISTORY_MIN_OBSERVATIONS", "3")),
            history_min_span_hours=int(os.getenv("HISTORY_MIN_SPAN_HOURS", "168")),
            ml_queries=queries,
            ml_limit_per_query=int(os.getenv("ML_LIMIT_PER_QUERY", "30")),
            ml_client_id=os.getenv("ML_CLIENT_ID", "").strip(),
            ml_client_secret=os.getenv("ML_CLIENT_SECRET", "").strip(),
            ml_access_token=os.getenv("ML_ACCESS_TOKEN", "").strip(),
            ml_refresh_token=os.getenv("ML_REFRESH_TOKEN", "").strip(),
            token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", "").strip(),
            partner_feed_path=os.getenv("PARTNER_FEED_PATH", "feeds/deals.json").strip(),
            usd_cop_rate=os.getenv("USD_COP_RATE", "").strip(),
            fb_page_id=os.getenv("FB_PAGE_ID", "").strip(),
            fb_page_token=os.getenv("FB_PAGE_TOKEN", "").strip(),
            meta_graph_api_version=os.getenv("META_GRAPH_API_VERSION", "v26.0").strip(),
            generated_dir=Path(os.getenv("GENERATED_DIR", "generated")),
            affiliate_disclosure=_as_bool(os.getenv("AFFILIATE_DISCLOSURE"), True),
            publish_hours=_parse_hours(os.getenv("PUBLISH_HOURS", "")),
            link_in_comment=_as_bool(os.getenv("LINK_IN_COMMENT"), False),
            reel_hours=frozenset(int(h) for h in os.getenv("REEL_HOURS", "").split(",") if h.strip()),
            stories=_as_bool(os.getenv("STORIES"), False),
            promo_hours=frozenset(int(h) for h in os.getenv("PROMO_HOURS", "").split(",") if h.strip()),
            min_minutes_between_posts=int(os.getenv("MIN_MINUTES_BETWEEN_POSTS", "0")),
        )
