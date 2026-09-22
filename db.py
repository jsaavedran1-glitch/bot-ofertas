import sqlite3, os, hashlib
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "ofertas.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ofertas (
            id          TEXT PRIMARY KEY,
            source      TEXT NOT NULL,
            title       TEXT NOT NULL,
            url         TEXT NOT NULL,
            price       REAL NOT NULL,
            orig_price  REAL NOT NULL,
            discount    INTEGER NOT NULL,
            currency    TEXT NOT NULL DEFAULT 'COP',
            price_usd   REAL,
            usd_rate    REAL,
            image_url   TEXT,
            category    TEXT,
            score       INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_history (
            item_id     TEXT NOT NULL,
            price       REAL NOT NULL,
            currency    TEXT NOT NULL,
            seen_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS published (
            item_id     TEXT NOT NULL,
            platform    TEXT NOT NULL,
            published_at TEXT NOT NULL,
            fb_post_id  TEXT,
            PRIMARY KEY (item_id, platform)
        );

        CREATE INDEX IF NOT EXISTS idx_history_item ON price_history(item_id);
        CREATE INDEX IF NOT EXISTS idx_published_item ON published(item_id);
        """)

def record_price(item_id: str, price: float, currency: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_history(item_id, price, currency, seen_at) VALUES(?,?,?,?)",
            (item_id, price, currency, datetime.utcnow().isoformat())
        )

def get_price_history(item_id: str, days: int = 30) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT price FROM price_history WHERE item_id=? AND seen_at>? ORDER BY seen_at",
            (item_id, since)
        ).fetchall()
    return [r["price"] for r in rows]

def already_published(item_id: str, cooldown_days: int = 7) -> bool:
    since = (datetime.utcnow() - timedelta(days=cooldown_days)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM published WHERE item_id=? AND published_at>?",
            (item_id, since)
        ).fetchone()
    return row is not None

def mark_published(item_id: str, platform: str, fb_post_id: str = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO published(item_id, platform, published_at, fb_post_id) VALUES(?,?,?,?)",
            (item_id, platform, datetime.utcnow().isoformat(), fb_post_id)
        )

def upsert_oferta(deal: dict):
    with get_conn() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO ofertas
        (id, source, title, url, price, orig_price, discount, currency,
         price_usd, usd_rate, image_url, category, score, created_at)
        VALUES(:id,:source,:title,:url,:price,:orig_price,:discount,:currency,
               :price_usd,:usd_rate,:image_url,:category,:score,:created_at)
        """, {**deal, "created_at": datetime.utcnow().isoformat()})
