from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
from zoneinfo import ZoneInfo

from app.config import Settings
from app.database import Database
from app.models import DealObservation, DiscountEvidence, to_minor, variant_key
from app.pipeline import OfferPipeline
from app.publishers.facebook import FacebookPublisher, FacebookPublishError
from app.sources import PartnerFeedSource
from app.sources.amazon_rss import AmazonRssSource
from app.sources.colombia_stores import colombia_store_sources
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
        sources.append(PartnerFeedSource(settings.partner_feed_path))
    return database, OfferPipeline(settings, database, sources)


# Every 13 posts: 4 Amazon, 2 Falabella (Adidas, Nike, Puma, New Balance), 2 Nike, 2 Alkosto, 1 Ktronix,
# 1 Tennis, 1 Éxito. A source with nothing approved is skipped.
PUBLISH_PATTERN = (
    "amazon", "falabella", "alkosto", "nike", "amazon", "ktronix", "falabella",
    "amazon", "tennis", "alkosto", "nike", "amazon", "exito",
)
REEL_SOURCES = {"amazon", "exito", "alkosto", "ktronix", "falabella", "nike", "tennis"}


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
    if settings.stories:
        try:
            story = pipeline.reel_renderer.render_story_from_image(
                image, f"story_{image.stem}", "¡NO TE LO PIERDAS!"
            )
            publisher.publish_story(story)
        except (FacebookPublishError, OSError) as exc:
            print(f"Advertencia historia promo: {exc}", file=sys.stderr)
    print(f"Promo del día publicada ({promo.key}, {len(promo.deals)} productos): {post_id}")
    return True


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
    """Claude's copy for real queued deals, side by side with the store titles. Publishes nothing."""
    from dataclasses import replace
    from app.ai import claude_helper
    from app.copywriter import facebook_copy
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
    example = None
    for n, c in enumerate(picked, start=1):
        ai = helper.write_copy(c.deal)
        print(f"\n[{n}] {store_name(c.deal.source)} · -{c.deal.discount_pct}%")
        print(f"  Título de la tienda: {c.deal.title}")
        if ai is None:
            print("  Claude: sin respuesta válida (se usaría la plantilla)")
            continue
        print(f"  Nombre corto:   {ai.short_name}")
        print(f"  Título español: {ai.title_es}")
        print(f"  Gancho:         {ai.hook}")
        for point in ai.points:
            print(f"  Beneficio:      {point}")
        print(f"  Pregunta:       {ai.question}")
        if n <= 3:  # also try the visual review on a few rendered posts
            try:
                image = pipeline.renderer.render(replace(c.deal, ai=ai), f"preview_{c.id}", usd_cop_rate=rate, require_image=True)
                approved, problem = helper.review_image(image, c.deal)
                print(f"  Revisión visual: {'aprobada' if approved else 'RECHAZADA - ' + problem}")
            except Exception as exc:
                print(f"  Revisión visual: no se pudo generar la imagen ({type(exc).__name__})")
        if example is None:
            example = replace(c.deal, ai=ai)
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
    preview = commands.add_parser("ai-preview", help="Ver los textos de Claude para ofertas reales, sin publicar")
    preview.add_argument("--limit", type=int, default=20)
    return root


def main() -> int:
    args = parser().parse_args()
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
                hour = datetime.now(ZoneInfo("America/Bogota")).hour
                if hour not in settings.publish_hours:
                    print(f"Fuera de horario de publicación ({hour}h Bogotá); solo se escaneó.")
                    return 0
                since = database.minutes_since_last_post()
                if since is not None and since < settings.min_minutes_between_posts:
                    print(f"Última publicación hace {since:.0f} min; se espera al menos {settings.min_minutes_between_posts}.")
                    return 0
                if hour in settings.promo_hours and not database.promo_published_today():
                    try:
                        if publish_promo(settings, database, pipeline):
                            return 0
                    except (FacebookPublishError, OSError) as exc:
                        print(f"Falló la promo del día: {exc}", file=sys.stderr)
                if hour in settings.top_hours and not database.top_published_today():
                    try:
                        result = pipeline.publish_top(database.list_candidates(status="approved", limit=60))
                        if result:
                            print(f"Top {len(result[1])} del día publicado: {result[0]}")
                            return 0
                        print("Top 5: no hay suficientes ofertas válidas; se publica un post normal.")
                    except (FacebookPublishError, OSError) as exc:
                        print(f"Falló el top 5: {exc}", file=sys.stderr)
                as_reel = hour in settings.reel_hours and not database.reel_published_this_hour()
                queues = publish_queues(database, as_reel, settings.repost_cooldown_days)
                published = 0
                while queues and published < settings.max_posts_per_run:
                    next_queues = []
                    for q in queues:
                        if published >= settings.max_posts_per_run:
                            break
                        candidate_id = q.pop(0)
                        try:
                            post_id = pipeline.publish(candidate_id, as_reel=as_reel)
                            print(f"{'Reel publicado' if as_reel else 'Publicada'} {candidate_id}: {post_id}")
                            published += 1
                        except Exception as exc:
                            print(f"Falló {candidate_id}: {exc}", file=sys.stderr)
                        if q:
                            next_queues.append(q)
                    queues = next_queues
            else:
                print("Modo review: usa list, approve y publish para revisar cada oferta.")
        elif args.command == "demo":
            demo(database, pipeline)
        elif args.command == "ai-preview":
            return ai_preview(settings, database, pipeline, args.limit)
        return 0
    except (ValueError, FacebookPublishError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
