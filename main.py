"""
Bot de Alertas - Ojo al Precio | Bogotá
Monitorea ofertas de MercadoLibre y las publica en Facebook.

Uso:
  python main.py            # corre el scheduler continuo
  python main.py --test     # prueba la conexion y busca 1 oferta
  python main.py --post     # busca y publica ahora (sin esperar)
"""

import os, sys, argparse
from dotenv import load_dotenv

load_dotenv()

from db import init_db, already_published, mark_published, upsert_oferta, record_price
from sources.mercadolibre import get_deals
from publisher.facebook import post, test_connection

MAX_PER_RUN  = int(os.getenv("MAX_POSTS_PER_DAY", 8)) // 8  # aprox por ciclo
COOLDOWN     = int(os.getenv("REPOST_COOLDOWN_DAYS", 7))
INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", 60))
POST_MODE    = os.getenv("POST_MODE", "manual")

def run_cycle(dry_run: bool = False):
    print("\n[BOT] Iniciando ciclo de busqueda...")
    deals = get_deals()
    print(f"[BOT] {len(deals)} ofertas encontradas antes de filtros")

    # Ordenar por score descendente
    deals.sort(key=lambda d: d["score"], reverse=True)

    published_count = 0
    for deal in deals:
        if published_count >= MAX_PER_RUN:
            break
        if already_published(deal["id"], COOLDOWN):
            continue

        upsert_oferta(deal)
        record_price(deal["id"], deal["price"], deal["currency"])

        print(f"\n[OFERTA] {deal['title']}")
        print(f"  Precio:    ${deal['price']:,.0f} {deal['currency']}")
        print(f"  Descuento: {deal['discount']}%")
        print(f"  Score:     {deal['score']}")
        print(f"  URL:       {deal['url']}")

        if dry_run:
            print("  [dry-run] No se publica")
            published_count += 1
            continue

        if POST_MODE == "auto":
            post_id = post(deal)
            if post_id:
                mark_published(deal["id"], "facebook", post_id)
                published_count += 1
        else:
            # Modo manual: imprimir para revisión
            print("  [manual] Revisa y ejecuta: python main.py --post-id", deal["id"])

    print(f"\n[BOT] Ciclo terminado. Publicadas: {published_count}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",  action="store_true", help="Prueba conexion")
    parser.add_argument("--post",  action="store_true", help="Ejecuta un ciclo ahora")
    parser.add_argument("--dry",   action="store_true", help="Busca pero no publica")
    args = parser.parse_args()

    init_db()

    if args.test:
        print("[BOT] Probando conexion a Facebook...")
        ok = test_connection()
        if ok:
            print("[BOT] Buscando una oferta de prueba...")
            run_cycle(dry_run=True)
        return

    if args.post:
        run_cycle(dry_run=args.dry)
        return

    # Scheduler continuo
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler()
    scheduler.add_job(run_cycle, "interval", minutes=INTERVAL_MIN)
    print(f"[BOT] Scheduler iniciado. Ciclo cada {INTERVAL_MIN} minutos.")
    print(f"[BOT] Modo: {POST_MODE} | Descuento minimo: {os.getenv('MIN_DISCOUNT_PCT')}%")
    run_cycle()  # primer ciclo inmediato
    scheduler.start()

if __name__ == "__main__":
    main()
