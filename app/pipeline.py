from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
import sys
import time

from app.config import Settings
from app.copywriter import facebook_copy
from app.database import Database
from app.image_renderer import MissingProductImage, OfferImageRenderer
from app.reel_renderer import ReelRenderer
from app.models import Candidate, DiscountEvidence, utc_now
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
from app.publishers.telegram import TelegramPublisher, deal_caption, top_caption
from app.rates import ExchangeRateProvider
from app.scoring import qualifies, score, validate_observation
from app.sources.base import DealSource, SourceError


@dataclass
class ScanReport:
    observations: int = 0
    candidates_created: list[str] = field(default_factory=list)
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


class OfferPipeline:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        sources: list[DealSource],
        renderer: OfferImageRenderer | None = None,
        rate_provider: ExchangeRateProvider | None = None,
        reel_renderer: ReelRenderer | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.sources = sources
        self.renderer = renderer or OfferImageRenderer(settings.generated_dir)
        self.rate_provider = rate_provider or ExchangeRateProvider(settings.usd_cop_rate)
        self.reel_renderer = reel_renderer or ReelRenderer(settings.generated_dir)
        self.telegram = (
            TelegramPublisher(settings.telegram_bot_token, settings.telegram_chat_id)
            if settings.telegram_bot_token and settings.telegram_chat_id else None
        )

    def scan(self) -> ScanReport:
        report = ScanReport()
        usd_rate = self.rate_provider.usd_to_cop()
        for source in self.sources:
            try:
                observations = source.fetch()
            except SourceError as exc:
                report.errors.append(str(exc))
                continue
            for observed in observations:
                valid, _ = validate_observation(observed)
                if not valid:
                    report.rejected += 1
                    continue
                reference = None
                if observed.evidence == DiscountEvidence.NONE:
                    reference = self.database.historical_reference(
                        observed,
                        self.settings.history_min_observations,
                        self.settings.history_min_span_hours,
                    )
                self.database.upsert_product(observed)
                self.database.record_observation(observed)
                report.observations += 1
                deal = observed.with_history_reference(reference) if reference else observed
                min_savings = getattr(source, "min_savings_cop", None) or self.settings.min_savings_cop
                min_pct = getattr(source, "min_discount_pct", None) or self.settings.min_discount_pct
                if not qualifies(deal, min_pct, min_savings, usd_rate):
                    continue
                if self.database.was_published_recently(
                    deal.source, deal.external_id, self.settings.repost_cooldown_days
                ):
                    continue
                if not deal.image_url:
                    continue
                if self.database.create_candidate(deal, score(deal, usd_rate)):
                    report.candidates_created.append(deal.candidate_id)
        return report

    def _with_insights(self, candidate: Candidate) -> Candidate:
        """Attach facts computed from our own data (e.g. lowest price in 30 days) before rendering."""
        return replace(candidate, deal=replace(candidate.deal, lowest_30d=self.database.is_lowest_price(candidate.deal)))

    def render(self, candidate: Candidate, require_image: bool = False) -> tuple[Path, str]:
        rate = self.rate_provider.usd_to_cop()
        image = self.renderer.render(candidate.deal, candidate.id, usd_cop_rate=rate, require_image=require_image)
        caption = facebook_copy(
            candidate.deal, rate, self.settings.affiliate_disclosure, self.settings.link_in_comment
        )
        return image, caption

    def publish(self, candidate_id: str, publisher: FacebookPublisher | None = None, as_reel: bool = False) -> str:
        candidate = self.database.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"No existe la oferta {candidate_id}.")
        if candidate.status != "approved":
            raise ValueError("La oferta debe estar aprobada antes de publicarse.")
        if utc_now() - candidate.deal.observed_at > timedelta(hours=self.settings.candidate_max_age_hours):
            raise ValueError("La oferta está desactualizada; vuelve a escanearla antes de publicar.")
        fresh = self._revalidate(candidate)
        if fresh is None:
            raise ValueError("No fue posible revalidar el precio y la disponibilidad del ítem exacto.")
        changed = (
            not fresh.available
            or fresh.price_minor != candidate.deal.price_minor
            or fresh.currency != candidate.deal.currency
            or fresh.url != candidate.deal.url
        )
        if candidate.deal.evidence == DiscountEvidence.OFFICIAL_ORIGINAL:
            changed = changed or (
                fresh.original_price_minor != candidate.deal.original_price_minor
                or fresh.evidence != DiscountEvidence.OFFICIAL_ORIGINAL
            )
        if changed:
            raise ValueError("La oferta cambió desde su aprobación; escanéala y apruébala nuevamente.")
        if not self.database.reserve_for_publish(candidate_id, self.settings.max_posts_per_day):
            raise ValueError("No se pudo reservar la oferta o se alcanzó el límite diario.")
        candidate = self._with_insights(candidate)
        try:
            publisher = publisher or FacebookPublisher(
                self.settings.fb_page_id,
                self.settings.fb_page_token,
                self.settings.meta_graph_api_version,
            )
            if as_reel:
                rate = self.rate_provider.usd_to_cop()
                video = self.reel_renderer.render_reel(
                    candidate.deal, f"reel_{candidate.id}", usd_cop_rate=rate,
                    link_in_comment=self.settings.link_in_comment,
                )
                caption = facebook_copy(
                    candidate.deal, rate, self.settings.affiliate_disclosure, self.settings.link_in_comment
                ) + " #reels"
                post_id = publisher.publish_reel(video, caption)
            else:
                image, caption = self.render(candidate, require_image=True)
                post_id = publisher.publish_photo(image, caption)
        except FacebookPublishError as exc:
            self.database.record_publish_error(candidate_id, str(exc), exc.ambiguous)
            raise
        except MissingProductImage as exc:
            self.database.record_publish_error(candidate_id, str(exc), False)
            raise
        except Exception:
            self.database.record_publish_error(candidate_id, "Fallo local antes de publicar.", False)
            raise
        self.database.record_publication(candidate_id, post_id, "facebook_reel" if as_reel else "facebook")
        if self.settings.link_in_comment:
            # Reels are still processing right after upload and may reject comments for a few seconds.
            attempts = 4 if as_reel else 1
            for attempt in range(attempts):
                try:
                    publisher.comment(post_id, f"👉 Aquí la oferta: {candidate.deal.url}")
                    break
                except FacebookPublishError as exc:
                    if attempt == attempts - 1:
                        print(f"Advertencia {candidate_id}: {exc}", file=sys.stderr)
                    else:
                        time.sleep(15)
        if self.telegram:
            # Telegram is a bonus channel: failures only warn.
            try:
                rate = self.rate_provider.usd_to_cop()
                square = self.renderer.render(candidate.deal, candidate.id, usd_cop_rate=rate, require_image=True)
                self.telegram.send_photo(square, deal_caption(candidate.deal, rate))
            except Exception as exc:
                print(f"Advertencia Telegram {candidate_id}: {exc}", file=sys.stderr)
        if self.settings.stories:
            # Every post is mirrored to the Page story (public). A story is a bonus: failures only warn.
            try:
                if as_reel:
                    publisher.publish_video_story(video)
                else:
                    story = self.reel_renderer.render_story(
                        candidate.deal, f"story_{candidate.id}", usd_cop_rate=self.rate_provider.usd_to_cop()
                    )
                    publisher.publish_story(story)
            except (FacebookPublishError, MissingProductImage, OSError) as exc:
                print(f"Advertencia historia {candidate_id}: {exc}", file=sys.stderr)
        return post_id

    def publish_top(self, candidates: list[Candidate], publisher: FacebookPublisher | None = None) -> tuple[str, list[Candidate]] | None:
        """Revalidate, render and post the day's Top 5 carousel. Returns (post_id, items) or None if too few."""
        from app.top5 import MIN_ITEMS, pick_top, top_copy, top_links

        still_valid = []
        for c in candidates:
            fresh = self._revalidate(c)
            if fresh and fresh.available and fresh.price_minor == c.deal.price_minor and fresh.url == c.deal.url:
                still_valid.append(c)
        items = [self._with_insights(c) for c in pick_top(still_valid)]
        if len(items) < MIN_ITEMS:
            return None
        rate = self.rate_provider.usd_to_cop()
        images, kept = [], []
        for c in items:
            try:
                images.append(self.renderer.render(c.deal, f"top_{c.id}", usd_cop_rate=rate, require_image=True,
                                                   tag=f"TOP {len(kept) + 1} DE HOY"))
                kept.append(c)
            except MissingProductImage:
                continue
        items = kept
        if len(items) < MIN_ITEMS:
            return None
        cover = getattr(self.renderer, "render_top_cover", None)
        if cover:
            images.insert(0, cover([c.deal for c in items], f"top_cover_{items[0].id}", usd_cop_rate=rate))
        publisher = publisher or FacebookPublisher(
            self.settings.fb_page_id, self.settings.fb_page_token, self.settings.meta_graph_api_version
        )
        post_id = publisher.publish_carousel(images, top_copy(items, rate, self.settings.link_in_comment))
        self.database.record_top([c.id for c in items], post_id)
        if self.settings.link_in_comment:
            try:
                publisher.comment(post_id, top_links(items))
            except FacebookPublishError as exc:
                print(f"Advertencia top 5: {exc}", file=sys.stderr)
        if self.telegram:
            try:
                self.telegram.send_album(images, top_caption([c.deal for c in items], rate))
            except Exception as exc:
                print(f"Advertencia Telegram top 5: {exc}", file=sys.stderr)
        return post_id, items

    def _revalidate(self, candidate: Candidate):
        for source in self.sources:
            source_names = getattr(source, "source_names", {source.source_name})
            if candidate.deal.source in source_names or "partner_feed" in source_names:
                fresh = source.revalidate(candidate.deal)
                if fresh is not None:
                    return fresh
        return None

    def dry_run(self, candidate_ids: list[str]) -> list[tuple[Candidate, Path, str]]:
        results: list[tuple[Candidate, Path, str]] = []
        for candidate_id in candidate_ids:
            candidate = self.database.get_candidate(candidate_id)
            if candidate:
                candidate = self._with_insights(candidate)
                image, caption = self.render(candidate)
                results.append((candidate, image, caption))
        return results
