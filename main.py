from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from itertools import zip_longest
import math
import sys
import time
from zoneinfo import ZoneInfo

from app.config import Settings
from app.events import CountdownRenderer, active_event, countdown, countdown_caption
from app.metrics import ENOUGH_INTERACTIONS, MetricsError, PostStats, daily_report, fetch_page_stats, weekly_report
from app.content import ContentRenderer, NewsFeeds, content_caption, load_tips, news_post, next_tip
from app.database import Database
from app.models import DealObservation, DiscountEvidence, store_name, to_minor, variant_key
from app.pipeline import OfferPipeline
from app.reel_renderer import ReelRenderer
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
from app.sources import PartnerFeedSource
from app.sources.amazon_rss import AmazonRssSource
from app.sources.colombia_stores import colombia_store_sources
from app.sources.newegg_cards import NeweggPlayStationCardSource
from app.sources.awin import AwinFeedSource
from app.promos import PROMO_STORES, PromoRenderer, find_promo, promo_copy, promo_links


def build_runtime(settings: Settings, include_sources: bool = True) -> tuple[Database, OfferPipeline]:
    database = Database(settings.database_url)
    database.initialize()
    sources = []
    # Mercado Libre is off: since 2026 its API no longer returns search, best sellers or prices to
    # uncertified apps. Its sources (app/sources/mercadolibre.py, ml_affiliates.py) can be re-added here.
    if include_sources:
        sources.append(AmazonRssSource(min_discount_pct=settings.min_discount_pct))
        sources.extend(colombia_store_sources())
        sources.append(NeweggPlayStationCardSource())  # only when Newegg has a PSN gift card promo
        if settings.awin_adidas_feed_url:  # Awin feed (Adidas CO), once the programme approves us
            sources.append(AwinFeedSource(settings.awin_adidas_feed_url, "adidas"))
        sources.append(PartnerFeedSource(settings.partner_feed_path))
    return database, OfferPipeline(settings, database, sources)


# Every 14 posts: 4 Amazon, 2 Falabella (Adidas, Nike, Puma, New Balance), 2 Nike, 2 Alkosto, 1 Ktronix,
# 1 Tennis, 1 Éxito and 1 Newegg (PlayStation cards, only when there is a promo). A source with nothing
# approved is skipped.
PUBLISH_PATTERN = (
    "amazon", "newegg", "falabella", "alkosto", "nike", "amazon", "ktronix", "falabella",
    "amazon", "tennis", "alkosto", "nike", "amazon", "exito", "adidas",
)
REEL_SOURCES = {"amazon", "exito", "alkosto", "ktronix", "falabella", "nike", "tennis", "adidas"}


def publish_queues(database: Database, as_reel: bool = False, cooldown_days: int = 7) -> list[list[str]]:
    """Approved candidates grouped by source, ordered by today's position in PUBLISH_PATTERN.
    One candidate per product: other colours and the same item in another store are left out, as are
    products posted in the last `cooldown_days` days."""
    seen = database.published_variant_keys(cooldown_days)
    by_source: dict[str, list[str]] = {}
    for c in database.list_candidates(status="approved"):  # best score first: the best variant wins
        key = variant_key(c.deal.title)
        if key in seen:
            continue
        seen.add(key)
        by_source.setdefault(c.deal.source, []).append(c.id)
    start = database.posts_today() % len(PUBLISH_PATTERN)
    order: list[str] = []
    for source in PUBLISH_PATTERN[start:] + PUBLISH_PATTERN[:start]:
        if source in by_source and source not in order and (not as_reel or source in REEL_SOURCES):
            order.append(source)
    return [by_source[s] for s in order]


def publish_promo(settings: Settings, database: Database, pipeline: OfferPipeline) -> bool:
    """Post today's brand promo if one qualifies. Returns True when something was published."""
    sources = {s.source_name: s for s in pipeline.sources if s.source_name in PROMO_STORES}
    promo = find_promo(sources, database.promo_posted_within, datetime.now(ZoneInfo("America/Bogota")).toordinal())
    if promo is None:
        print("Promo del día: ninguna marca con suficientes ofertas.")
        return False
    image = PromoRenderer(settings.generated_dir).render_promo(promo, f"promo_{promo.key.replace(':', '_')}")
    publisher = FacebookPublisher(settings.fb_page_id, settings.fb_page_token, settings.meta_graph_api_version)
    post_id = publisher.publish_photo(image, promo_copy(promo, link_in_comment=settings.link_in_comment))
    database.record_promo(promo.key, post_id)
    if settings.link_in_comment:
        try:
            publisher.comment(post_id, promo_links(promo))
        except FacebookPublishError as exc:
            print(f"Advertencia promo: {exc}", file=sys.stderr)
    post_story(settings, database, publisher, image, "¡NO TE LO PIERDAS!", "Mira la promo completa en nuestra página")
    print(f"Promo del día publicada ({promo.key}, {len(promo.deals)} productos): {post_id}")
    return True


def post_story(settings: Settings, database: Database, publisher: FacebookPublisher, image: Path, headline: str,
               pointer: str) -> None:
    """Mirror a just-published image post to the Page story, if it is among the hour's first STORIES_PER_HOUR.
    A story is a bonus: failures only warn."""
    if not settings.stories or database.posts_this_hour() > settings.stories_per_hour:
        return
    try:
        story = ReelRenderer(settings.generated_dir).render_story_from_image(image, f"story_{image.stem}", headline, pointer)
        publisher.publish_story(story)
    except (FacebookPublishError, OSError) as exc:
        print(f"Advertencia historia: {exc}", file=sys.stderr)


def publish_content(settings: Settings, database: Database, pipeline: OfferPipeline,
                    feeds: NewsFeeds | None = None, tips=None, publisher: FacebookPublisher | None = None) -> bool:
    """A "Dato útil" or a "Noticia", alternating (each falls back to the other). True when one went out."""
    history = database.content_history()
    order = ("noticia", "dato") if history and history[-1][0] == "dato" else ("dato", "noticia")
    post = None
    for kind in order:
        if kind == "dato":
            post = next_tip(load_tips() if tips is None else tips, {key: when for k, key, when in history if k == "dato"})
        elif pipeline.ai is not None:  # news need Claude to choose and summarize
            items = (feeds or NewsFeeds()).recent({key for k, key, _ in history if k == "noticia"})
            picked = pipeline.ai.pick_news(items)
            post = news_post(items[picked[0]], picked[1]) if picked else None
        if post:
            break
    if post is None:
        print("Contenido: ningún dato ni noticia disponible; se publica una oferta.")
        return False
    name = f"{post.kind}_{hashlib.sha1(post.key.encode()).hexdigest()[:12]}"
    image = ContentRenderer(settings.generated_dir).render_content(post, name)
    publisher = publisher or FacebookPublisher(settings.fb_page_id, settings.fb_page_token, settings.meta_graph_api_version)
    post_id = publisher.publish_photo(image, content_caption(post, settings.link_in_comment))
    database.record_content(post.kind, post.key, post_id)
    if settings.link_in_comment:
        try:
            publisher.comment(post_id, f"📌 Fuente: {post.url}")
        except FacebookPublishError as exc:
            print(f"Advertencia contenido: {exc}", file=sys.stderr)
    if post.kind == "dato":
        post_story(settings, database, publisher, image, "¡ESTO TE SIRVE!", "Míralo completo en nuestra página")
    else:
        post_story(settings, database, publisher, image, "ÚLTIMA NOTICIA", "La fuente está en nuestra página")
    print(f"{'Dato útil' if post.kind == 'dato' else 'Noticia'} publicado ({post.source}): {post_id}")
    return True


REPORT_PATH = Path("generated") / "informe_semanal.md"


def collect_metrics(settings: Settings, database: Database, days: int = 8, session=None) -> int:
    """Refresh reactions, comments and shares of the last `days` days of posts. Returns how many were updated."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    ours = database.posts_since(since)
    if not ours:
        return 0
    counts = fetch_page_stats(settings.fb_page_id, settings.fb_page_token, settings.meta_graph_api_version,
                              since - timedelta(hours=1), session=session)
    rows = [(pid, kind, source, at, *(counts[pid][k] for k in ("reactions", "comments", "shares")))
            for pid, kind, source, at in ours if pid in counts]
    database.save_metrics(rows)
    print(f"Métricas: {len(rows)} de {len(ours)} publicaciones actualizadas.")
    return len(rows)


def build_report(database: Database, end: datetime | None = None, days: int = 7) -> str:
    end = end or datetime.now(timezone.utc)
    start, before = end - timedelta(days=days), end - timedelta(days=2 * days)

    def stats(rows):
        return [PostStats(r["post_id"], r["kind"], r["source"], r["published_at"], r["reactions"], r["comments"],
                          r["shares"]) for r in rows]

    week, previous = stats(database.metrics_between(start, end)), stats(database.metrics_between(before, start))
    names = {p.source: store_name(p.source) for p in week}
    return weekly_report(week, start, end, previous, names)


DAILY_PATH = Path("generated") / "informe_diario.md"


def planned_posts(settings: Settings, day) -> int:
    boost = settings.event_boost if active_event(day) else 1.0
    return sum(round(q * boost) for q in settings.hourly_posts) if settings.hourly_posts else settings.max_posts_per_day


def _stats(rows) -> list[PostStats]:
    return [PostStats(r["post_id"], r["kind"], r["source"], r["published_at"], r["reactions"], r["comments"], r["shares"])
            for r in rows]


def build_daily_report(settings: Settings, database: Database, pipeline: OfferPipeline | None, day) -> str:
    """Yesterday's (or `day`'s) report; Claude adds suggestions only once there are enough reactions to compare."""
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/Bogota"))
    end = start + timedelta(days=1)
    posts, week = _stats(database.metrics_between(start, end)), _stats(database.metrics_between(end - timedelta(days=7), end))
    ops = {
        "planned": planned_posts(settings, day),
        "errors": database.publish_errors_between(start, end),
        "new_by_source": database.new_candidates_between(start, end),
        "queue": sum(len(q) for q in publish_queues(database, False, settings.repost_cooldown_days)),
        "published_kinds": [kind for _, kind, _, at in database.posts_since(start) if at < end],
    }
    names = {src: store_name(src) for src in {p.source for p in posts} | set(ops["new_by_source"])}
    report = daily_report(posts, week, ops, day, names)
    engaged = sum(p.interactions for p in week)
    ai = getattr(pipeline, "ai", None)
    if engaged < ENOUGH_INTERACTIONS:
        report += (f"## Recomendaciones\n\nTodavía no hay suficientes reacciones para comparar horas o tiendas "
                   f"({engaged} interacciones en 7 días; hacen falta {ENOUGH_INTERACTIONS}). Por ahora el informe sirve "
                   f"para vigilar la operación.\n")
    elif ai is not None:
        tips = ai.recommend(report)
        if tips:
            report += "## Recomendaciones (Claude)\n\n" + "\n".join(f"- {t}" for t in tips) + "\n"
    return report


def write_report(report: str, path: Path = REPORT_PATH) -> None:
    """Saved for the workflow, which opens it as a GitHub issue, and shown in the run's summary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)


def metrics_slot(settings: Settings, database: Database, now: datetime, pipeline: OfferPipeline | None = None) -> None:
    """At METRICS_HOURS (first run of the hour) refresh the counts. At 7 h write yesterday's report, and on
    Mondays the weekly one too."""
    if now.hour not in settings.metrics_hours or now.minute >= 30:
        return
    try:
        collect_metrics(settings, database)
    except MetricsError as exc:
        print(f"Advertencia métricas: {exc}", file=sys.stderr)
        return
    if now.hour == 7:
        write_report(build_daily_report(settings, database, pipeline, now.date() - timedelta(days=1)), DAILY_PATH)
        print("Informe diario listo.")
        if now.weekday() == 0:
            write_report(build_report(database))
            print("Informe semanal listo.")


def publish_countdown(settings: Settings, database: Database, today, publisher: FacebookPublisher | None = None) -> bool:
    """"Faltan N días para el Black Friday" (and "Hoy es ..." on the day), once per day."""
    due = countdown(today)
    if due is None:
        return False
    event, left = due
    key = f"{event.key}:{today.isoformat()}"
    if any(k == key for _, k, _ in database.content_history()):
        return False
    image = CountdownRenderer(settings.generated_dir).render_countdown(event, left, f"cuenta_{event.key}_{left}")
    stores = "Amazon, Alkosto, Ktronix, Éxito, Falabella, Nike y Tennis"
    publisher = publisher or FacebookPublisher(settings.fb_page_id, settings.fb_page_token, settings.meta_graph_api_version)
    post_id = publisher.publish_photo(image, countdown_caption(event, left, stores))
    database.record_content("evento", key, post_id)
    post_story(settings, database, publisher, image, f"¡SE VIENE EL {event.tag}!" if left else f"¡HOY ES {event.tag}!",
               "Activa las notificaciones de la página")
    print(f"{event.name}: {'hoy es' if left == 0 else f'faltan {left} días'} publicado: {post_id}")
    return True


def posts_due(settings: Settings, database: Database, now: datetime) -> int:
    """Posts this run should make. With POSTS_PER_HOUR, the run in the first half of the hour makes half of
    that hour's posts and the one in the second half the rest (runs come every 30 minutes)."""
    if not settings.hourly_posts:
        return settings.max_posts_per_run
    quota = settings.hourly_posts[now.hour]
    if quota and active_event(now.date()):
        quota = round(quota * settings.event_boost)
    due = quota if now.minute >= 30 else math.ceil(quota / 2)
    return max(0, due - database.posts_this_hour())


def spacing_minutes(now: datetime, posts: int) -> float:
    """Spread this run's posts over what is left of its half hour: one every 3 to 12 minutes."""
    if posts <= 1:
        return 0.0
    left = 30 - now.minute % 30 - 1
    return max(3.0, min(12.0, left / posts))


def publish_slot(settings: Settings, database: Database, pipeline: OfferPipeline,
                 now: datetime | None = None, sleep=time.sleep, clock=time.monotonic) -> int:
    """Publish what this run owes: the day's promo or Top 5 when their hour comes, then deals (a reel first in
    reel hours), spaced out instead of all at once. Returns how many posts went out."""
    now = now or datetime.now(ZoneInfo("America/Bogota"))
    due = posts_due(settings, database, now)
    if due <= 0:
        print(f"Cuota de las {now.hour}h ya cumplida.")
        return 0
    since = database.minutes_since_last_post()
    if since is not None and since < settings.min_minutes_between_posts:
        if not settings.hourly_posts:
            print(f"Última publicación hace {since:.0f} min; se espera al menos {settings.min_minutes_between_posts}.")
            return 0
        sleep((settings.min_minutes_between_posts - since) * 60)
    gap = spacing_minutes(now, due) * 60
    start = clock()
    published = 0

    def wait_turn() -> None:
        if published:
            sleep(max(0.0, start + published * gap - clock()))

    if now.hour == settings.countdown_hour and countdown(now.date()):
        try:
            published += publish_countdown(settings, database, now.date())
        except (FacebookPublishError, OSError) as exc:
            print(f"Falló la cuenta regresiva: {exc}", file=sys.stderr)
    if published < due and now.hour in settings.promo_hours and not database.promo_published_today():
        wait_turn()
        try:
            published += publish_promo(settings, database, pipeline)
        except (FacebookPublishError, OSError) as exc:
            print(f"Falló la promo del día: {exc}", file=sys.stderr)
    if published < due and now.hour in settings.top_hours and not database.top_published_today():
        wait_turn()
        try:
            result = pipeline.publish_top(database.list_candidates(status="approved", limit=60))
            if result:
                print(f"Top {len(result[1])} del día publicado: {result[0]}")
                published += 1
            else:
                print("Top 5: no hay suficientes ofertas válidas; se publica un post normal.")
        except (FacebookPublishError, OSError) as exc:
            print(f"Falló el top 5: {exc}", file=sys.stderr)
    if published < due and now.hour in settings.content_hours and not database.content_published_this_hour():
        wait_turn()
        try:
            published += publish_content(settings, database, pipeline)
        except (FacebookPublishError, OSError, ValueError) as exc:
            print(f"Falló el dato/noticia: {exc}", file=sys.stderr)
    tried: set[str] = set()
    reel_failed = False
    while published < due and len(tried) < due * 4:
        as_reel = (now.hour in settings.reel_hours and not reel_failed
                   and not database.reel_published_this_hour())
        queues = publish_queues(database, as_reel, settings.repost_cooldown_days)
        if as_reel and not queues:
            as_reel, queues = False, publish_queues(database, False, settings.repost_cooldown_days)
        # Round-robin across stores, skipping deals that already failed in this run.
        order = [cid for row in zip_longest(*queues) for cid in row if cid and cid not in tried]
        if not order:
            print("No quedan ofertas aprobadas para publicar.")
            break
        wait_turn()
        candidate_id = order[0]
        tried.add(candidate_id)
        try:
            post_id = pipeline.publish(candidate_id, as_reel=as_reel)
            print(f"{'Reel publicado' if as_reel else 'Publicada'} {candidate_id}: {post_id}")
            published += 1
        except Exception as exc:
            print(f"Falló {candidate_id}: {exc}", file=sys.stderr)
            reel_failed = reel_failed or as_reel  # one failed reel: photos for the rest of the run
    print(f"Publicaciones en esta ejecución: {published} de {due}.")
    return published


def show_candidates(database: Database, status: str) -> None:
    rows = database.list_candidates(status)
    if not rows:
        print(f"No hay ofertas con estado '{status}'.")
        return
    for candidate in rows:
        deal = candidate.deal
        print(
            f"{candidate.id} | {candidate.status} | {deal.discount_pct}% | "
            f"{deal.title} | {deal.source}"
        )
        print(f"  {deal.url}")


def demo(database: Database, pipeline: OfferPipeline) -> None:
    deal = DealObservation(
        source="tienda_demo",
        external_id="DEMO-001",
        title="Audífonos inalámbricos con cancelación de ruido y batería de larga duración",
        price_minor=to_minor("119900", "COP"),
        original_price_minor=to_minor("189900", "COP"),
        evidence=DiscountEvidence.OFFICIAL_ORIGINAL,
        currency="COP",
        url="https://example.com/oferta-demo",
        shipping_note="Envío e impuestos se confirman en la tienda",
        observed_at=datetime.now(timezone.utc),
    )
    database.upsert_product(deal)
    database.record_observation(deal)
    database.create_candidate(deal, 57)
    candidate = database.get_candidate(deal.candidate_id)
    if candidate is None:
        raise RuntimeError("No se pudo crear la oferta de demostración.")
    image, caption = pipeline.render(candidate)
    print(f"Imagen dry-run: {image.resolve()}")
    print("\nCopy dry-run:\n")
    print(caption)
    print("\nCero publicaciones reales realizadas.")


def ai_preview(settings: Settings, database: Database, pipeline: OfferPipeline, limit: int) -> int:
    """What Claude would do with real queued deals: Spanish names, the analysis of the finished image,
    the caption text and the hashtags. Publishes nothing."""
    from dataclasses import replace
    from app.ai import claude_helper, merge_copy
    from app.copywriter import facebook_copy, trending_hashtags
    from app.models import store_name

    helper = claude_helper(settings.anthropic_api_key)
    if helper is None:
        print("Falta ANTHROPIC_API_KEY o el paquete anthropic.", file=sys.stderr)
        return 2
    by_source: dict[str, list] = {}
    for c in database.list_candidates("approved", limit=200) + database.list_candidates("pending", limit=200):
        by_source.setdefault(c.deal.source, []).append(c)
    picked = []
    while len(picked) < limit and any(by_source.values()):
        for queue in by_source.values():
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    rate = pipeline.rate_provider.usd_to_cop()
    trending = trending_hashtags()
    example = None
    for n, c in enumerate(picked, start=1):
        deal = replace(c.deal, ai=helper.write_names(c.deal))
        print(f"\n[{n}] {store_name(deal.source)} · -{deal.discount_pct}%")
        print(f"  Título de la tienda: {deal.title}")
        if deal.ai:
            print(f"  Nombre en la imagen: {deal.ai.short_name}")
            print(f"  Título en español:  {deal.ai.title_es}")
        try:
            image = pipeline.renderer.render(deal, f"preview_{c.id}", usd_cop_rate=rate, require_image=True)
        except Exception as exc:
            print(f"  Imagen: no se pudo generar ({type(exc).__name__})")
            continue
        approved, problem, copy = helper.analyze_post(image, deal, trending)
        print(f"  Revisión de la imagen: {'aprobada' if approved else 'RECHAZADA - ' + problem}")
        if copy:
            print(f"  Gancho:    {copy.hook}")
            for point in copy.points:
                print(f"  Beneficio: {point}")
            print(f"  Pregunta:  {copy.question}")
            print(f"  Hashtags:  #OjoAlPrecio {' '.join(copy.hashtags)}")
            if example is None:
                example = replace(deal, ai=merge_copy(deal.ai, copy))
    if example is not None:
        print("\n--- Ejemplo de texto completo del post ---\n")
        print(facebook_copy(example, rate, settings.affiliate_disclosure, settings.link_in_comment))
    print(f"\nVista previa de {len(picked)} ofertas. No se publicó nada.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Bot Ojo al Precio para Facebook")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("scan", help="Buscar y guardar ofertas candidatas")
    listing = commands.add_parser("list", help="Listar ofertas")
    listing.add_argument("--status", default="pending", choices=(
        "pending", "approved", "publishing", "published", "rejected", "failed", "unknown", "expired"
    ))
    for name in ("approve", "reject", "render", "publish"):
        command = commands.add_parser(name)
        command.add_argument("candidate_id")
        if name == "publish":
            command.add_argument("--reel", action="store_true", help="Publicar como Reel")
    run = commands.add_parser("run", help="Ejecutar un ciclo")
    modes = run.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--automatic", action="store_true")
    commands.add_parser("demo", help="Generar imagen y copy ficticios sin publicar")
    report = commands.add_parser("informe", help="Informe de reacciones, comentarios y compartidos por tipo, tienda y hora")
    report.add_argument("--days", type=int, default=7)
    report.add_argument("--collect", action="store_true", help="Actualizar las métricas desde Facebook antes")
    report.add_argument("--save", action="store_true", help="Guardarlo para publicarlo como issue en GitHub")
    report.add_argument("--diario", action="store_true", help="Informe de un día (hoy hasta ahora, o --ayer)")
    report.add_argument("--ayer", action="store_true", help="Con --diario: el día de ayer completo")
    preview = commands.add_parser("ai-preview", help="Ver los textos de Claude para ofertas reales, sin publicar")
    preview.add_argument("--limit", type=int, default=20)
    coupons = commands.add_parser("cupones", help="Publicar los cupones de assets/content/cupones.json")
    coupons.add_argument("--dry-run", action="store_true", help="Solo genera la imagen y muestra el texto")
    return root


def publish_coupons(dry_run: bool) -> int:
    """Manual coupon post: no database or deal sources needed, only the Page credentials."""
    from app.coupons import CouponRenderer, load_coupon_post
    from app.publishers.facebook import FacebookPublisher

    post = load_coupon_post()
    image = CouponRenderer(Path(os.getenv("GENERATED_DIR", "generated"))).render_coupons(post)
    print(f"Imagen: {image.resolve()}\n{post.caption}\n")
    if dry_run:
        print("Vista previa de cupones. No se publicó nada.")
        return 0
    publisher = FacebookPublisher(
        os.getenv("FB_PAGE_ID", ""), os.getenv("FB_PAGE_TOKEN", ""), os.getenv("META_GRAPH_API_VERSION", "v26.0")
    )
    print(f"Publicación de cupones creada: {publisher.publish_photo(image, post.caption)}")
    return 0


def main() -> int:
    args = parser().parse_args()
    if args.command == "cupones":
        try:
            return publish_coupons(args.dry_run)
        except (ValueError, FacebookPublishError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    try:
        settings = Settings.from_env()
        database, pipeline = build_runtime(settings, include_sources=args.command in {"scan", "run", "publish"})
        if args.command == "scan":
            report = pipeline.scan()
            print(f"Observaciones: {report.observations}; candidatas nuevas: {len(report.candidates_created)}")
            for error in report.errors:
                print(f"Advertencia: {error}", file=sys.stderr)
        elif args.command == "list":
            show_candidates(database, args.status)
        elif args.command in {"approve", "reject"}:
            decision = "approved" if args.command == "approve" else "rejected"
            if not database.decide(args.candidate_id, decision):
                print("No se pudo cambiar el estado de esa oferta.", file=sys.stderr)
                return 2
            print(f"Oferta {args.candidate_id}: {decision}.")
        elif args.command == "render":
            candidate = database.get_candidate(args.candidate_id)
            if not candidate:
                print("Oferta no encontrada.", file=sys.stderr)
                return 2
            image, caption = pipeline.render(candidate)
            print(image.resolve())
            print(caption)
        elif args.command == "publish":
            post_id = pipeline.publish(args.candidate_id, as_reel=args.reel)
            print(f"{'Reel' if args.reel else 'Publicación'} creado: {post_id}")
        elif args.command == "run":
            report = pipeline.scan()
            ids = report.candidates_created
            print(f"Candidatas nuevas: {len(ids)}")
            if args.dry_run:
                for _, image, caption in pipeline.dry_run(ids):
                    print(f"\nImagen: {image.resolve()}\n{caption}\n")
            elif args.automatic:
                # Auto-approve everything pending: new deals and expired ones seen again.
                for candidate in database.list_candidates("pending", limit=1000):
                    database.decide(candidate.id, "approved", actor="system")
                expired = database.expire_stale(settings.candidate_max_age_hours)
                if expired:
                    print(f"Vencidas: {expired}")
                metrics_slot(settings, database, datetime.now(ZoneInfo("America/Bogota")), pipeline)
                hour = datetime.now(ZoneInfo("America/Bogota")).hour
                if hour not in settings.publish_hours:
                    print(f"Fuera de horario de publicación ({hour}h Bogotá); solo se escaneó.")
                    return 0
                publish_slot(settings, database, pipeline)
            else:
                print("Modo review: usa list, approve y publish para revisar cada oferta.")
        elif args.command == "demo":
            demo(database, pipeline)
        elif args.command == "informe":
            if args.collect:
                collect_metrics(settings, database, days=max(args.days, 7) + 1)
            if args.diario:
                day = datetime.now(ZoneInfo("America/Bogota")).date() - timedelta(days=1 if args.ayer else 0)
                report = build_daily_report(settings, database, pipeline, day)
            else:
                report = build_report(database, days=args.days)
            print(report)
            if args.save:
                write_report(report, DAILY_PATH if args.diario else REPORT_PATH)
        elif args.command == "ai-preview":
            return ai_preview(settings, database, pipeline, args.limit)
        return 0
    except (ValueError, FacebookPublishError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
