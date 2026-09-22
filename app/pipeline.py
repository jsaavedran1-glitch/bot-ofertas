from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
import sys

from app.config import Settings
from app.copywriter import facebook_copy
from app.database import Database
from app.image_renderer import MissingProductImage, OfferImageRenderer
from app.reel_renderer import ReelRenderer
from app.models import Candidate, DiscountEvidence, utc_now
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
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
                if not qualifies(
                    deal,
                    self.settings.min_discount_pct,
                    self.settings.min_savings_cop,
                    usd_rate,
                ):
                    continue
                if self.database.was_published_recently(
                    deal.source, deal.external_id, self.settings.repost_cooldown_days
                ):
                    continue
                if not deal.image_url:
                    continue
                if self.database.create_candidate(deal, score(deal)):
                    report.candidates_created.append(deal.candidate_id)
        return report

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
            try:
                publisher.comment(post_id, f"👉 Aquí la oferta: {candidate.deal.url}")
            except FacebookPublishError as exc:
                print(f"Advertencia {candidate_id}: {exc}", file=sys.stderr)
        return post_id

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
                image, caption = self.render(candidate)
                results.append((candidate, image, caption))
        return results
