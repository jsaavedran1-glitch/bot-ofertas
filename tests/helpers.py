from __future__ import annotations

from pathlib import Path

from app.config import Settings


def settings_for(directory: str) -> Settings:
    root = Path(directory)
    return Settings(
        database_url=f"sqlite:///{root / 'test.db'}",
        post_mode="review",
        min_discount_pct=20,
        min_savings_cop=20_000,
        max_posts_per_day=2,
        candidate_max_age_hours=6,
        repost_cooldown_days=7,
        history_min_observations=3,
        history_min_span_hours=48,
        ml_queries=("audifonos",),
        ml_limit_per_query=10,
        ml_client_id="",
        ml_client_secret="",
        ml_access_token="",
        ml_refresh_token="",
        token_encryption_key="",
        partner_feed_path=str(root / "missing.json"),
        usd_cop_rate="4000",
        fb_page_id="",
        fb_page_token="",
        meta_graph_api_version="v26.0",
        generated_dir=root / "generated",
        affiliate_disclosure=True,
    )
