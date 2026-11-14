from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
import sys
import time

from app.ai import ClaudeHelper, claude_helper, merge_copy
from app.config import Settings
from app.copywriter import facebook_copy, trending_hashtags
from app.database import Database
from app.image_renderer import MissingProductImage, OfferImageRenderer
from app.reel_renderer import ReelRenderer
from app.models import Candidate, DiscountEvidence, utc_now, variant_key
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
from app.publishers.instagram import InstagramPublisher, InstagramPublishError
from app.links import post_ref, tracked
from app.quality import ensure_recommendable, names_of
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
        ai: ClaudeHelper | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.sources = sources
        self.renderer = renderer or OfferImageRenderer(settings.generated_dir)
        self.rate_provider = rate_provider or ExchangeRateProvider(settings.usd_cop_rate)
        self.reel_renderer = reel_renderer or ReelRenderer(settings.generated_dir)
        self.ai = ai if ai is not None else (claude_helper(settings.anthropic_api_key) if settings.ai_enabled else None)
        self.telegram = (
            TelegramPublisher(settings.telegram_bot_token, settings.telegram_chat_id)
            if settings.telegram_bot_token and settings.telegram_chat_id else None
        )

    def scan(self) -> ScanReport:
        report = ScanReport()
        usd_rate = self.rate_provider.usd_to_cop()
        # Another colour (or the other store) of something already posted is not a new deal for followers.
        posted_variants = self.database.published_variant_keys(self.settings.repost_cooldown_days)
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
                ) or variant_key(deal.title) in posted_variants:
                    continue
                if not deal.image_url:
                    continue
                if self.database.create_candidate(deal, score(deal, usd_rate)):
                    report.candidates_created.append(deal.candidate_id)
        return report

    def _with_insights(self, candidate: Candidate) -> Candidate:
        """Attach facts computed from our own data (lowest price in 30 days) and, when Claude is configured,
        its Spanish product name and copy, before rendering."""
        deal = replace(candidate.deal, lowest_30d=self.database.is_lowest_price(candidate.deal))
        if self.ai:
            deal = replace(deal, ai=self.ai.write_names(deal))
        return replace(candidate, deal=deal)

    def _analyze(self, candidate: Candidate, image: Path | None = None) -> Candidate:
        """Claude looks at the finished image: a clear problem (wrong photo, broken text) stops the post,
        otherwise it writes the hook, benefits, question and hashtags from what it sees."""
        if not self.ai:
            return candidate
        if image is None:
            rate = self.rate_provider.usd_to_cop()
            image = self.renderer.render(candidate.deal, candidate.id, usd_cop_rate=rate, require_image=True)
        approved, problem, copy = self.ai.analyze_post(image, candidate.deal, trending_hashtags())
        if not approved and self.settings.ai_review:
            raise ValueError(f"Claude detuvo la publicación: {problem}")
        return replace(candidate, deal=replace(candidate.deal, ai=merge_copy(candidate.deal.ai, copy)))

    def _instagram(self) -> InstagramPublisher | None:
        """The linked Instagram account, looked up once per run; None when cross-posting is off or not set up."""
        if not self.settings.instagram_kinds:
            return None
        if not hasattr(self, "_ig"):
            self._ig = InstagramPublisher.for_page(
                self.settings.fb_page_id, self.settings.fb_page_token, self.settings.meta_graph_api_version)
            if self._ig is None:
                print("Instagram: el token no ve una cuenta de Instagram vinculada a la página; no se publica allí.")
        return self._ig

    def _to_instagram(self, kind: str, publisher: FacebookPublisher, caption: str, video: Path | None = None) -> None:
        """Bonus copy on Instagram of what just went out on Facebook (INSTAGRAM_KINDS); failures only warn."""
        if kind not in self.settings.instagram_kinds:
            return
        instagram = self._instagram()
        if instagram is None:
            return
        try:
            if video is not None:
                media_id = instagram.publish_reel(video, caption)
            else:
                media_id = instagram.publish_image(publisher.photo_source(publisher.last_photo_id), caption)
            print(f"Instagram ({kind}): {media_id}")
        except (InstagramPublishError, FacebookPublishError, OSError) as exc:
            print(f"Advertencia Instagram: {exc}", file=sys.stderr)

    def _review_reel(self, candidate: Candidate, rate) -> None:
        """Claude checks the reel's own screens (opener, details, price, call to action), not only the photo."""
        if not self.ai:
            return
        frames = self.reel_renderer.review_frames(candidate.deal, usd_cop_rate=rate,
                                                  link_in_comment=self.settings.link_in_comment)
        approved, problem = self.ai.review_frames(frames, candidate.deal)
        if not approved and self.settings.ai_review:
            raise ValueError(f"Claude detuvo el reel: {problem}")

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
                candidate = self._analyze(candidate)
                rate = self.rate_provider.usd_to_cop()
                caption = facebook_copy(
                    candidate.deal, rate, self.settings.affiliate_disclosure, self.settings.link_in_comment
                ) + " #reels"
                ensure_recommendable(caption, names_of([candidate.deal]))
                self._review_reel(candidate, rate)
                self._load_media()
                video = self.reel_renderer.render_reel(
                    candidate.deal, f"reel_{candidate.id}", usd_cop_rate=rate,
                    link_in_comment=self.settings.link_in_comment,
                )
                post_id = publisher.publish_reel(video, caption)
            else:
                rate = self.rate_provider.usd_to_cop()
                image = self.renderer.render(candidate.deal, candidate.id, usd_cop_rate=rate, require_image=True)
                candidate = self._analyze(candidate, image)
                caption = facebook_copy(
                    candidate.deal, rate, self.settings.affiliate_disclosure, self.settings.link_in_comment
                )
                ensure_recommendable(caption, names_of([candidate.deal]))
                post_id = publisher.publish_photo(image, caption)
        except FacebookPublishError as exc:
            self.database.record_publish_error(candidate_id, str(exc), exc.ambiguous)
            raise
        except (MissingProductImage, ValueError) as exc:
            self.database.record_publish_error(candidate_id, str(exc), False)
            raise
        except Exception:
            self.database.record_publish_error(candidate_id, "Fallo local antes de publicar.", False)
            raise
        self.database.record_publication(candidate_id, post_id, "facebook_reel" if as_reel else "facebook")
        if as_reel:
            self._to_instagram("reel", publisher, caption, video=video)
        else:
            self._to_instagram("oferta", publisher, caption)
        if self.settings.link_in_comment:
            # Reels are still processing right after upload and may reject comments for a few seconds.
            attempts = 4 if as_reel else 1
            for attempt in range(attempts):
                try:
                    url = tracked(candidate.deal.url, post_ref("reel" if as_reel else "oferta", candidate.id))
                    publisher.comment(post_id, f"👉 Aquí la oferta: {url}")
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
        if self.story_due():
            # The hour's first post(s) are mirrored to the Page story (public). A story is a bonus: failures only warn.
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

    def story_due(self) -> bool:
        """Stories on, and the post just published is among the hour's first STORIES_PER_HOUR."""
        return self.settings.stories and self.database.posts_this_hour() <= self.settings.stories_per_hour

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
        caption = top_copy(items, rate, self.settings.link_in_comment)
        ensure_recommendable(caption, names_of([c.deal for c in items]))
        post_id = publisher.publish_carousel(images, caption)
        self.database.record_top([c.id for c in items], post_id)
        if self.settings.link_in_comment:
            try:
                publisher.comment(post_id, top_links(items))
            except FacebookPublishError as exc:
                print(f"Advertencia top 5: {exc}", file=sys.stderr)
        if self.story_due():
            try:
                story = self.reel_renderer.render_story_from_image(
                    images[0], f"story_{images[0].stem}", "TOP 5 DE HOY", "Desliza el carrusel en nuestra página")
                publisher.publish_story(story)
            except (FacebookPublishError, OSError) as exc:
                print(f"Advertencia historia top 5: {exc}", file=sys.stderr)
        if self.telegram:
            try:
                self.telegram.send_album(images, top_caption([c.deal for c in items], rate))
            except Exception as exc:
                print(f"Advertencia Telegram top 5: {exc}", file=sys.stderr)
        return post_id, items

    def history_options(self, candidates: list[Candidate]) -> list[tuple[Candidate, object, object]]:
        """(candidate, history, verdict) for fresh COP deals whose own price history says something, best first:
        the strongest real discounts and the most inflated "antes" prices."""
        from app.history import build_history, judge

        options = []
        max_age = timedelta(hours=self.settings.candidate_max_age_hours)
        for c in candidates:
            if c.deal.currency != "COP" or utc_now() - c.deal.observed_at > max_age:
                continue
            history = build_history(self.database.price_series(c.deal), c.deal)
            verdict = judge(c.deal, history) if history else None
            if verdict is None:
                continue
            if verdict.kind == "inflado":
                strength = 1 - history.high / c.deal.original_price_minor
            else:
                strength = (1 - c.deal.price_minor / history.usual) * (1.0 if verdict.kind == "real" else 0.6)
            options.append((strength, c, history, verdict))
        options.sort(key=lambda o: o[0], reverse=True)
        return [(c, h, v) for _, c, h, v in options]

    def publish_history(self, candidates: list[Candidate], publisher: FacebookPublisher | None = None,
                        prefer: str | None = None, dry_run: bool = False) -> tuple[str, Candidate] | None:
        """One "¿Descuento real?" post: the deal's 30-day price line from our own scans and the verdict.
        `prefer` ("real"/"inflado") alternates the angle. None when no deal has enough history to say something."""
        from app.history import HistoryRenderer, history_caption

        options = self.history_options(candidates)
        if prefer:
            options.sort(key=lambda o: o[2].kind != prefer)  # stable: keeps strength order inside each group
        renderer = getattr(self, "history_renderer", None) or HistoryRenderer(
            self.settings.generated_dir, session=getattr(self.renderer, "session", None))
        for c, history, verdict in options[:5]:
            fresh = self._revalidate(c)
            if not (fresh and fresh.available and fresh.price_minor == c.deal.price_minor and fresh.url == c.deal.url):
                continue
            if dry_run:
                image = renderer.render_history(c.deal, history, verdict, f"historial_{c.id}")
                print(f"Vista previa historial: {image.resolve()}\n"
                      f"{history_caption(c.deal, history, verdict, self.settings.link_in_comment)}\n")
                return "dry-run", c
            if not self.database.reserve_for_publish(c.id, self.settings.max_posts_per_day):
                continue
            c = self._with_insights(c)
            try:
                publisher = publisher or FacebookPublisher(
                    self.settings.fb_page_id, self.settings.fb_page_token, self.settings.meta_graph_api_version)
                image = renderer.render_history(c.deal, history, verdict, f"historial_{c.id}")
                caption = history_caption(c.deal, history, verdict, self.settings.link_in_comment,
                                          self.settings.affiliate_disclosure)
                ensure_recommendable(caption, names_of([c.deal]))
                post_id = publisher.publish_photo(image, caption)
            except FacebookPublishError as exc:
                self.database.record_publish_error(c.id, str(exc), exc.ambiguous)
                raise
            except (MissingProductImage, ValueError) as exc:
                self.database.record_publish_error(c.id, str(exc), False)
                print(f"Historial {c.id} descartado: {exc}", file=sys.stderr)
                continue
            self.database.record_publication(c.id, post_id, "facebook_historial")
            self._to_instagram("historial", publisher, caption)
            if self.settings.link_in_comment:
                try:
                    publisher.comment(post_id, f"👉 Aquí la oferta: {tracked(c.deal.url, post_ref('historial', c.id))}")
                except FacebookPublishError as exc:
                    print(f"Advertencia historial: {exc}", file=sys.stderr)
            if self.story_due():
                try:
                    story = self.reel_renderer.render_story_from_image(
                        image, f"story_{image.stem}", "¿DESCUENTO REAL?", "Mira el historial en nuestra página")
                    publisher.publish_story(story)
                except (FacebookPublishError, OSError) as exc:
                    print(f"Advertencia historia historial: {exc}", file=sys.stderr)
            return post_id, c
        return None

    def _load_media(self) -> None:
        """Reels audio licensed only for Meta products lives in the database, never in the public repo."""
        if getattr(self, "_media_loaded", False):
            return
        self._media_loaded = True
        try:
            folders = self.database.export_media(self.settings.generated_dir / "media")
        except Exception as exc:  # no audio is better than no reel
            print(f"Advertencia audio de reels: {exc}", file=sys.stderr)
            return
        if "musica" in folders:
            self.reel_renderer.music_dir = folders["musica"]
        if "efectos" in folders:
            self.reel_renderer.sfx_dir = folders["efectos"]

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
