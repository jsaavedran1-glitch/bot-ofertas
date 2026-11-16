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


def _parse_hourly(value: str) -> tuple[int, ...]:
    """'8:2,9:4,17:8' -> 24 counts, posts to make in each Bogotá hour. Empty = not used."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return ()
    counts = [0] * 24
    for part in parts:
        hour, _, posts = part.partition(":")
        if not 0 <= int(hour) <= 23 or int(posts) < 0:
            raise ValueError("POSTS_PER_HOUR usa hora:publicaciones, con horas entre 0 y 23.")
        counts[int(hour)] = int(posts)
    return tuple(counts)


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
    top_hours: frozenset[int] = frozenset()
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    min_minutes_between_posts: int = 0
    content_hours: frozenset[int] = frozenset()  # CONTENT_HOURS: the hour's first post is a tip or a news item
    history_hours: frozenset[int] = frozenset()  # HISTORY_HOURS: one "¿Descuento real?" price-history post
    instagram_photos_per_hour: int = 1  # INSTAGRAM_PHOTOS_PER_HOUR: photo posts copied to Instagram each hour (reels: all)
    instagram_kinds: frozenset[str] = frozenset()  # INSTAGRAM_KINDS: what is also posted to the linked Instagram
    track_per_run: int = 0  # TRACK_PER_RUN: recent deals whose price is re-checked each run (price history)
    hourly_posts: tuple[int, ...] = ()
    awin_adidas_feed_url: str = ""  # AWIN_ADIDAS_FEED_URL (secret: it carries the Awin API key)
    stories_per_hour: int = 1  # STORIES_PER_HOUR: only the hour's first N posts also go to the story
    event_boost: float = 1.0  # EVENT_BOOST: posts per hour multiplied by this on Black Friday / Cyber Monday
    countdown_hour: int = 8  # COUNTDOWN_HOUR: "faltan N días" goes out as this hour's first post
    metrics_hours: frozenset[int] = frozenset({7, 13, 23})  # METRICS_HOURS: when to refresh post reactions  # POSTS_PER_HOUR: when set, it decides how many posts go out each hour
    anthropic_api_key: str = ""
    ai_enabled: bool = True
    ai_review: bool = True
    store_max_per_day: tuple[tuple[str, int], ...] = ()  # STORE_MAX_PER_DAY "falabella:6,tennis:4"
    amazon_max_per_day: int = 8  # AMAZON_MAX_PER_DAY: Amazon/DealNews posts (photos and reels) per Bogotá day

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
        hourly = _parse_hourly(os.getenv("POSTS_PER_HOUR", ""))
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
            publish_hours=(
                frozenset(h for h, n in enumerate(hourly) if n) if hourly else _parse_hours(os.getenv("PUBLISH_HOURS", ""))
            ),
            link_in_comment=_as_bool(os.getenv("LINK_IN_COMMENT"), False),
            reel_hours=frozenset(int(h) for h in os.getenv("REEL_HOURS", "").split(",") if h.strip()),
            stories=_as_bool(os.getenv("STORIES"), False),
            promo_hours=frozenset(int(h) for h in os.getenv("PROMO_HOURS", "").split(",") if h.strip()),
            top_hours=frozenset(int(h) for h in os.getenv("TOP_HOURS", "").split(",") if h.strip()),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            min_minutes_between_posts=int(os.getenv("MIN_MINUTES_BETWEEN_POSTS", "0")),
            hourly_posts=hourly,
            awin_adidas_feed_url=os.getenv("AWIN_ADIDAS_FEED_URL", "").strip(),
            stories_per_hour=int(os.getenv("STORIES_PER_HOUR", "1")),
            event_boost=float(os.getenv("EVENT_BOOST", "1")),
            countdown_hour=int(os.getenv("COUNTDOWN_HOUR", "8")),
            metrics_hours=frozenset(int(h) for h in os.getenv("METRICS_HOURS", "7,13,23").split(",") if h.strip()),
            content_hours=frozenset(int(h) for h in os.getenv("CONTENT_HOURS", "").split(",") if h.strip()),
            history_hours=frozenset(int(h) for h in os.getenv("HISTORY_HOURS", "").split(",") if h.strip()),
            instagram_photos_per_hour=int(os.getenv("INSTAGRAM_PHOTOS_PER_HOUR", "1")),
            instagram_kinds=frozenset(k.strip() for k in os.getenv("INSTAGRAM_KINDS", "").split(",") if k.strip()),
            track_per_run=int(os.getenv("TRACK_PER_RUN", "0")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            ai_review=_as_bool(os.getenv("AI_REVIEW"), True),
            ai_enabled=_as_bool(os.getenv("AI_ENABLED"), True),
            store_max_per_day=tuple((k.strip(), int(v)) for k, _, v in (
                part.partition(":") for part in os.getenv("STORE_MAX_PER_DAY", "").split(",") if ":" in part)),
            amazon_max_per_day=int(os.getenv("AMAZON_MAX_PER_DAY", "8")),
        )
