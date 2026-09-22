from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
from zoneinfo import ZoneInfo

from app.config import Settings
from app.database import Database
from app.ml_auth import MercadoLibreAuthError, MercadoLibreTokenManager
from app.models import DealObservation, DiscountEvidence, to_minor
from app.pipeline import OfferPipeline
from app.publishers.facebook import FacebookPublishError
from app.sources import AuthenticatedMercadoLibreSource, PartnerFeedSource
from app.sources.amazon_rss import AmazonRssSource


def build_runtime(settings: Settings, include_sources: bool = True) -> tuple[Database, OfferPipeline]:
    database = Database(settings.database_url)
    database.initialize()
    sources = []
    if include_sources and (settings.ml_access_token or settings.ml_refresh_token):
        manager = MercadoLibreTokenManager(settings, database)
        sources.append(
            AuthenticatedMercadoLibreSource(
                (), settings.ml_limit_per_query, manager.access_token
            )
        )
    if include_sources:
        sources.append(AmazonRssSource(min_discount_pct=settings.min_discount_pct))
        sources.append(PartnerFeedSource(settings.partner_feed_path))
    return database, OfferPipeline(settings, database, sources)


REEL_SOURCES = {"mercadolibre", "amazon"}
DAILY_CAP_BY_SOURCE = {"woot": 1}


def publish_queues(database: Database, as_reel: bool) -> list[list[str]]:
    """Approved candidates grouped by source, in round-robin order for this run."""
    by_source: dict[str, list[str]] = {}
    for c in database.list_candidates(status="approved"):
        by_source.setdefault(c.deal.source, []).append(c.id)
    for source, cap in DAILY_CAP_BY_SOURCE.items():
        if source in by_source and database.published_today(source) >= cap:
            del by_source[source]
    if as_reel:
        by_source = {s: q for s, q in by_source.items() if s in REEL_SOURCES}
    # Source published last goes to the back so consecutive runs alternate.
    last = database.last_published_source()
    return [q for s, q in by_source.items() if s != last] + ([by_source[last]] if last in by_source else [])


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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Bot Ojo al Precio para Facebook")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("scan", help="Buscar y guardar ofertas candidatas")
    listing = commands.add_parser("list", help="Listar ofertas")
    listing.add_argument("--status", default="pending", choices=(
        "pending", "approved", "publishing", "published", "rejected", "failed", "unknown"
    ))
    for name in ("approve", "reject", "render", "publish"):
        command = commands.add_parser(name)
        command.add_argument("candidate_id")
    run = commands.add_parser("run", help="Ejecutar un ciclo")
    modes = run.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--automatic", action="store_true")
    commands.add_parser("demo", help="Generar imagen y copy ficticios sin publicar")
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
            post_id = pipeline.publish(args.candidate_id)
            print(f"Publicación creada: {post_id}")
        elif args.command == "run":
            report = pipeline.scan()
            ids = report.candidates_created
            print(f"Candidatas nuevas: {len(ids)}")
            if args.dry_run:
                for _, image, caption in pipeline.dry_run(ids):
                    print(f"\nImagen: {image.resolve()}\n{caption}\n")
            elif args.automatic:
                # Auto-approve new candidates
                for candidate_id in ids:
                    database.decide(candidate_id, "approved", actor="system")
                expired = database.expire_stale(settings.candidate_max_age_hours)
                if expired:
                    print(f"Vencidas: {expired}")
                hour = datetime.now(ZoneInfo("America/Bogota")).hour
                if hour not in settings.publish_hours:
                    print(f"Fuera de horario de publicación ({hour}h Bogotá); solo se escaneó.")
                    return 0
                as_reel = hour in settings.reel_hours and not database.reel_published_this_hour()
                queues = publish_queues(database, as_reel)
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
        return 0
    except (ValueError, MercadoLibreAuthError, FacebookPublishError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
