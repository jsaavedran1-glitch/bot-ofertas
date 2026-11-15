from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo
import sqlite3

from app.models import Candidate, DealObservation, DiscountEvidence, utc_now, variant_key


VALID_STATUSES = {"pending", "approved", "publishing", "published", "rejected", "failed", "unknown", "expired"}


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
        self._pg = None  # one reused PostgreSQL connection per run; a new one per query exhausts the pooler
        if not self.is_postgres and not url.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL debe usar sqlite:/// o postgresql://.")
        if not self.is_postgres:
            self.sqlite_path = url.removeprefix("sqlite:///")
            if self.sqlite_path != ":memory:":
                Path(self.sqlite_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise DatabaseError("Instala psycopg[binary] para usar PostgreSQL.") from exc
            if self._pg is None or self._pg.closed or self._pg.broken:
                # prepare_threshold=None: Supabase's pooler (Supavisor) can stall on server-side prepared statements.
                self._pg = psycopg.connect(
                    self.url, row_factory=dict_row, autocommit=True, connect_timeout=15, prepare_threshold=None
                )
            return self._pg
        conn = sqlite3.connect(self.sqlite_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.is_postgres else statement

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[Any]:
        conn = self._connect()
        if self.is_postgres:
            with conn.transaction():
                yield conn
            return
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self._connect()
        if self.is_postgres:
            yield conn
            return
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def initialize(self) -> None:
        identity = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        timestamp_type = "TIMESTAMPTZ" if self.is_postgres else "TEXT"
        statements = [
            f"""
            CREATE TABLE IF NOT EXISTS products (
              source TEXT NOT NULL,
              external_id TEXT NOT NULL,
              title TEXT NOT NULL,
              url TEXT NOT NULL,
              image_url TEXT NOT NULL DEFAULT '',
              currency TEXT NOT NULL,
              last_price_minor BIGINT NOT NULL,
              available INTEGER NOT NULL,
              updated_at {timestamp_type} NOT NULL,
              PRIMARY KEY (source, external_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS price_observations (
              id {identity},
              source TEXT NOT NULL,
              external_id TEXT NOT NULL,
              price_minor BIGINT NOT NULL,
              currency TEXT NOT NULL,
              observed_at {timestamp_type} NOT NULL,
              UNIQUE(source, external_id, price_minor, observed_at)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS candidates (
              id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              external_id TEXT NOT NULL,
              title TEXT NOT NULL,
              price_minor BIGINT NOT NULL,
              original_price_minor BIGINT,
              currency TEXT NOT NULL,
              evidence TEXT NOT NULL,
              url TEXT NOT NULL,
              image_url TEXT NOT NULL DEFAULT '',
              affiliate INTEGER NOT NULL DEFAULT 0,
              shipping_note TEXT NOT NULL DEFAULT '',
              observed_at {timestamp_type} NOT NULL,
              score INTEGER NOT NULL,
              status TEXT NOT NULL,
              reserved_day TEXT,
              reserved_at {timestamp_type},
              created_at {timestamp_type} NOT NULL,
              updated_at {timestamp_type} NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS approvals (
              id {identity},
              candidate_id TEXT NOT NULL,
              actor TEXT NOT NULL,
              decision TEXT NOT NULL,
              created_at {timestamp_type} NOT NULL,
              FOREIGN KEY(candidate_id) REFERENCES candidates(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS publications (
              id {identity},
              candidate_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              external_post_id TEXT NOT NULL,
              published_at {timestamp_type} NOT NULL,
              local_day TEXT NOT NULL,
              UNIQUE(candidate_id, platform),
              UNIQUE(platform, external_post_id),
              FOREIGN KEY(candidate_id) REFERENCES candidates(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS publication_errors (
              id {identity},
              candidate_id TEXT NOT NULL,
              error_kind TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at {timestamp_type} NOT NULL,
              FOREIGN KEY(candidate_id) REFERENCES candidates(id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS integration_credentials (
              provider TEXT PRIMARY KEY,
              access_token_enc TEXT NOT NULL,
              refresh_token_enc TEXT NOT NULL,
              expires_at {timestamp_type} NOT NULL,
              updated_at {timestamp_type} NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS promo_publications (
              id {identity},
              promo_key TEXT NOT NULL,
              external_post_id TEXT NOT NULL,
              published_at {timestamp_type} NOT NULL,
              local_day TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS content_publications (
              id {identity},
              kind TEXT NOT NULL,
              content_key TEXT NOT NULL,
              external_post_id TEXT NOT NULL,
              published_at {timestamp_type} NOT NULL,
              local_day TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS post_metrics (
              post_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              source TEXT NOT NULL,
              published_at {timestamp_type} NOT NULL,
              reactions INTEGER NOT NULL,
              comments INTEGER NOT NULL,
              shares INTEGER NOT NULL,
              updated_at {timestamp_type} NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS media_assets (
              name TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              data {"BYTEA" if self.is_postgres else "BLOB"} NOT NULL,
              updated_at {timestamp_type} NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status, score)",
            "CREATE INDEX IF NOT EXISTS idx_observations_item ON price_observations(source, external_id, observed_at)",
            "CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(platform, local_day)",
        ]
        with self._transaction() as conn:
            for statement in statements:
                conn.execute(statement)
        self._add_column("candidates", "rating", "REAL")
        self._add_column("candidates", "reviews", "INTEGER")
        for column in ("views", "viewers", "plays"):  # from Meta insights; NULL while the token can't read them
            self._add_column("post_metrics", column, "INTEGER")

    def put_media(self, name: str, kind: str, data: bytes) -> None:
        """Store a private audio file (music/effects whose license forbids publishing it in the repo)."""
        with self._transaction() as conn:
            conn.execute(self._sql("DELETE FROM media_assets WHERE name=?"), (name,))
            conn.execute(
                self._sql("INSERT INTO media_assets(name, kind, data, updated_at) VALUES(?, ?, ?, ?)"),
                (name, kind, data, utc_now().isoformat()),
            )

    def export_media(self, folder: Path) -> dict[str, Path]:
        """Write every stored audio file to folder/<kind>/<name>; returns {kind: folder}."""
        with self._connection() as conn:
            rows = conn.execute("SELECT name, kind, data FROM media_assets").fetchall()
        folders: dict[str, Path] = {}
        for row in rows:
            target = folder / Path(row["kind"]).name
            target.mkdir(parents=True, exist_ok=True)
            path = target / Path(row["name"]).name
            data = bytes(row["data"])
            if not path.exists() or path.stat().st_size != len(data):
                path.write_bytes(data)
            folders[row["kind"]] = target
        return folders

    def _add_column(self, table: str, column: str, kind: str) -> None:
        """Idempotent ALTER TABLE ... ADD COLUMN (SQLite has no IF NOT EXISTS for columns)."""
        with self._transaction() as conn:
            if self.is_postgres:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
            elif column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def is_lowest_price(self, deal: DealObservation, days: int = 30, min_tracked_days: int = 14) -> bool:
        """True only if we've tracked the item for `min_tracked_days`+ and its price was higher at some point."""
        since = (utc_now() - timedelta(days=days)).isoformat()
        sql = self._sql(
            "SELECT MIN(price_minor) AS lo, MAX(price_minor) AS hi, MIN(observed_at) AS first"
            " FROM price_observations WHERE source=? AND external_id=? AND currency=? AND observed_at>=?"
        )
        with self._connection() as conn:
            row = self._dict(conn.execute(sql, (deal.source, deal.external_id, deal.currency, since)).fetchone())
        if not row or row["lo"] is None:
            return False
        first = row["first"] if isinstance(row["first"], datetime) else datetime.fromisoformat(str(row["first"]).replace("Z", "+00:00"))
        return (
            utc_now() - first >= timedelta(days=min_tracked_days)
            and deal.price_minor <= int(row["lo"])
            and int(row["hi"]) > deal.price_minor
        )

    def price_series(self, deal: DealObservation, days: int = 30) -> list[tuple[datetime, int]]:
        """(observed_at, price) of every scan of this item in its currency over the last `days` days."""
        since = (utc_now() - timedelta(days=days + 1)).isoformat()
        sql = self._sql(
            "SELECT observed_at, price_minor FROM price_observations"
            " WHERE source=? AND external_id=? AND currency=? AND observed_at>=? ORDER BY observed_at"
        )
        with self._connection() as conn:
            rows = conn.execute(sql, (deal.source, deal.external_id, deal.currency, since)).fetchall()
        out = []
        for row in rows:
            when = row["observed_at"]
            if isinstance(when, str):
                when = datetime.fromisoformat(when.replace("Z", "+00:00"))
            out.append((when, int(row["price_minor"])))
        return out

    def products_to_track(self, limit: int, days: int = 30) -> list[Candidate]:
        """Deals (in pesos) found in the last `days` days whose price hasn't been checked yet today (Bogotá),
        best score first, one per product: the daily re-check that builds a continuous price history."""
        local = utc_now().astimezone(ZoneInfo("America/Bogota"))
        today = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        since = (utc_now() - timedelta(days=days)).isoformat()
        sql = self._sql(
            "SELECT c.* FROM candidates c WHERE c.created_at>=? AND c.currency='COP' AND NOT EXISTS ("
            " SELECT 1 FROM price_observations o WHERE o.source=c.source AND o.external_id=c.external_id"
            " AND o.observed_at>=?) ORDER BY c.score DESC, c.created_at DESC LIMIT ?"
        )
        with self._connection() as conn:
            rows = conn.execute(sql, (since, today, limit * 3)).fetchall()
        out, seen = [], set()
        for row in rows:
            candidate = self._candidate(row)
            key = (row["source"], row["external_id"])
            if candidate and key not in seen:
                seen.add(key)
                out.append(candidate)
        return out[:limit]

    def first_observation(self) -> str:
        with self._connection() as conn:
            row = conn.execute("SELECT MIN(observed_at) AS first FROM price_observations").fetchone()
        return str(row["first"]) if row and row["first"] else "—"

    def history_published_this_hour(self) -> bool:
        local = utc_now().astimezone(ZoneInfo("America/Bogota"))
        start = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        sql = self._sql("SELECT 1 FROM publications WHERE platform='facebook_historial' AND published_at>=? LIMIT 1")
        with self._connection() as conn:
            return conn.execute(sql, (start,)).fetchone() is not None

    def upsert_product(self, deal: DealObservation) -> None:
        now = utc_now().isoformat()
        sql = self._sql(
            """
            INSERT INTO products(source, external_id, title, url, image_url, currency,
              last_price_minor, available, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
              title=excluded.title, url=excluded.url, image_url=excluded.image_url,
              currency=excluded.currency, last_price_minor=excluded.last_price_minor,
              available=excluded.available, updated_at=excluded.updated_at
            """
        )
        with self._transaction() as conn:
            conn.execute(sql, (deal.source, deal.external_id, deal.title, deal.url, deal.image_url,
                               deal.currency, deal.price_minor, int(deal.available), now))

    def record_observation(self, deal: DealObservation) -> None:
        sql = self._sql(
            """
            INSERT INTO price_observations(source, external_id, price_minor, currency, observed_at)
            VALUES(?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """
        )
        with self._transaction() as conn:
            conn.execute(sql, (deal.source, deal.external_id, deal.price_minor, deal.currency,
                               deal.observed_at.astimezone(timezone.utc).isoformat()))

    def historical_reference(
        self,
        deal: DealObservation,
        min_observations: int,
        min_span_hours: int,
    ) -> int | None:
        sql = self._sql(
            """
            SELECT price_minor, observed_at FROM price_observations
            WHERE source=? AND external_id=? AND currency=?
            ORDER BY observed_at ASC
            """
        )
        with self._connection() as conn:
            rows = conn.execute(sql, (deal.source, deal.external_id, deal.currency)).fetchall()
        if len(rows) < min_observations:
            return None
        timestamps = [datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00")) for row in rows]
        distinct_days = {stamp.astimezone(timezone.utc).date() for stamp in timestamps}
        if len(distinct_days) < min_observations:
            return None
        span = max(timestamps) - min(timestamps)
        if span < timedelta(hours=min_span_hours):
            return None
        reference = int(median([int(row["price_minor"]) for row in rows]))
        return reference if reference > deal.price_minor else None

    def was_published_recently(self, source: str, external_id: str, days: int) -> bool:
        since = (utc_now() - timedelta(days=days)).isoformat()
        sql = self._sql(
            """
            SELECT 1 FROM publications p JOIN candidates c ON c.id=p.candidate_id
            WHERE c.source=? AND c.external_id=? AND p.published_at>=? LIMIT 1
            """
        )
        with self._connection() as conn:
            return conn.execute(sql, (source, external_id, since)).fetchone() is not None

    def published_variant_keys(self, days: int) -> set[str]:
        """Variant keys (see models.variant_key) of everything published in the last `days` days."""
        since = (utc_now() - timedelta(days=days)).isoformat()
        sql = self._sql("SELECT c.title FROM publications p JOIN candidates c ON c.id=p.candidate_id WHERE p.published_at>=?")
        with self._connection() as conn:
            rows = conn.execute(sql, (since,)).fetchall()
        return {variant_key(row["title"]) for row in rows}

    def create_candidate(self, deal: DealObservation, candidate_score: int) -> bool:
        now = utc_now().isoformat()
        sql = self._sql(
            """
            INSERT INTO candidates(id, source, external_id, title, price_minor,
              original_price_minor, currency, evidence, url, image_url, affiliate,
              shipping_note, observed_at, score, status, created_at, updated_at, rating, reviews)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """
        )
        with self._transaction() as conn:
            cursor = conn.execute(sql, (
                deal.candidate_id, deal.source, deal.external_id, deal.title, deal.price_minor,
                deal.original_price_minor, deal.currency, deal.evidence.value, deal.url,
                deal.image_url, int(deal.affiliate), deal.shipping_note,
                deal.observed_at.astimezone(timezone.utc).isoformat(), candidate_score, now, now,
                deal.rating, deal.reviews,
            ))
            if cursor.rowcount == 1:
                return True
            # Seen again: refresh pending/approved ones, and bring expired ones back to pending.
            cursor = conn.execute(
                self._sql(
                    "UPDATE candidates SET observed_at=?, url=?, image_url=?, affiliate=?, rating=?, reviews=?, updated_at=?,"
                    " status=CASE WHEN status='expired' THEN 'pending' ELSE status END"
                    " WHERE id=? AND status IN ('pending','approved','expired')"
                ),
                (deal.observed_at.astimezone(timezone.utc).isoformat(), deal.url, deal.image_url,
                 int(deal.affiliate), deal.rating, deal.reviews, now, deal.candidate_id),
            )
            return False

    def posts_today(self) -> int:
        local_day = utc_now().astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        with self._connection() as conn:
            row = conn.execute(self._sql("SELECT COUNT(*) AS total FROM publications WHERE local_day=?"), (local_day,)).fetchone()
        return int(row["total"])

    def posts_today_from(self, source: str) -> int:
        """Today's (Bogotá) own posts of one store's deals: photos and reels, not Top 5 carousel slots."""
        local_day = utc_now().astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        sql = self._sql(
            "SELECT COUNT(*) AS total FROM publications p JOIN candidates c ON c.id=p.candidate_id"
            " WHERE p.local_day=? AND c.source=? AND p.platform IN ('facebook','facebook_reel')"
        )
        with self._connection() as conn:
            row = conn.execute(sql, (local_day, source)).fetchone()
        return int(row["total"])

    def minutes_since_last_post(self) -> float | None:
        sql = ("SELECT MAX(published_at) AS last FROM (SELECT published_at FROM publications"
               " UNION ALL SELECT published_at FROM promo_publications"
               " UNION ALL SELECT published_at FROM content_publications) AS posts")
        with self._connection() as conn:
            last = conn.execute(sql).fetchone()["last"]
        if last is None:
            return None
        if isinstance(last, str):
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (utc_now() - last).total_seconds() / 60

    def posts_this_hour(self) -> int:
        """Facebook posts since the start of the current Bogotá hour (a Top 5 carousel counts once)."""
        local = utc_now().astimezone(ZoneInfo("America/Bogota"))
        start = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        sql = self._sql(
            # A carousel's rows share one timestamp. No LIKE '%': psycopg would read % as a placeholder.
            "SELECT (SELECT COUNT(CASE WHEN platform<>'facebook_top' THEN 1 END)"
            " + COUNT(DISTINCT CASE WHEN platform='facebook_top' THEN published_at END)"
            " FROM publications WHERE published_at>=?)"
            " + (SELECT COUNT(*) FROM promo_publications WHERE published_at>=?)"
            " + (SELECT COUNT(*) FROM content_publications WHERE published_at>=?) AS total"
        )
        with self._connection() as conn:
            return int(conn.execute(sql, (start, start, start)).fetchone()["total"])

    def record_content(self, kind: str, content_key: str, external_post_id: str) -> None:
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                self._sql("INSERT INTO content_publications(kind, content_key, external_post_id, published_at, local_day)"
                          " VALUES(?, ?, ?, ?, ?)"),
                (kind, content_key, external_post_id, now.isoformat(),
                 now.astimezone(ZoneInfo("America/Bogota")).date().isoformat()),
            )

    def content_history(self) -> list[tuple[str, str, datetime]]:
        """(kind, key, published_at) of every tip and news post, oldest first."""
        sql = "SELECT kind, content_key, published_at FROM content_publications ORDER BY published_at"
        with self._connection() as conn:
            rows = conn.execute(sql).fetchall()
        out = []
        for row in rows:
            when = row["published_at"]
            if isinstance(when, str):
                when = datetime.fromisoformat(when.replace("Z", "+00:00"))
            out.append((row["kind"], row["content_key"], when))
        return out

    def posts_since(self, since: datetime) -> list[tuple[str, str, str, datetime]]:
        """(post_id, kind, source, published_at) of every Facebook post since `since` (a Top 5 once)."""
        cutoff = since.astimezone(timezone.utc).isoformat()
        queries = (
            "SELECT p.external_post_id AS post_id, p.platform AS kind, c.source AS source, p.published_at AS at"
            " FROM publications p JOIN candidates c ON c.id = p.candidate_id WHERE p.published_at>=?",
            "SELECT external_post_id AS post_id, 'promo' AS kind, promo_key AS source, published_at AS at"
            " FROM promo_publications WHERE published_at>=?",
            "SELECT external_post_id AS post_id, kind, 'contenido' AS source, published_at AS at"
            " FROM content_publications WHERE published_at>=?",
        )
        kinds = {"facebook": "oferta", "facebook_reel": "reel", "facebook_top": "top5", "facebook_historial": "historial"}
        posts: dict[str, tuple[str, str, str, datetime]] = {}
        with self._connection() as conn:
            for sql in queries:
                for row in conn.execute(self._sql(sql), (cutoff,)).fetchall():
                    post_id = str(row["post_id"]).split(":")[0]  # Top 5 rows carry ":n"
                    kind = kinds.get(row["kind"], row["kind"])
                    source = "varias" if kind == "top5" else str(row["source"]).split(":")[0]
                    when = row["at"]
                    if isinstance(when, str):
                        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
                    posts.setdefault(post_id, (post_id, kind, source, when))
        return sorted(posts.values(), key=lambda p: p[3])

    def save_metrics(self, rows: list[tuple[str, str, str, datetime, int, int, int]]) -> None:
        now = utc_now().isoformat()
        with self._transaction() as conn:
            for post_id, kind, source, at, reactions, comments, shares in rows:
                conn.execute(
                    self._sql(
                        "INSERT INTO post_metrics(post_id, kind, source, published_at, reactions, comments, shares, updated_at)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(post_id) DO UPDATE SET reactions=excluded.reactions,"
                        " comments=excluded.comments, shares=excluded.shares, updated_at=excluded.updated_at"
                    ),
                    (post_id, kind, source, at.astimezone(timezone.utc).isoformat(), reactions, comments, shares, now),
                )

    def save_views(self, views: dict[str, dict]) -> int:
        """Views, accounts and reel plays for posts already in post_metrics; a missing value keeps the old one."""
        updated = 0
        with self._transaction() as conn:
            for post_id, row in views.items():
                cursor = conn.execute(
                    self._sql("UPDATE post_metrics SET views=COALESCE(?, views), viewers=COALESCE(?, viewers),"
                              " plays=COALESCE(?, plays) WHERE post_id=?"),
                    (row.get("views"), row.get("viewers"), row.get("plays"), post_id),
                )
                updated += max(0, cursor.rowcount or 0)
        return updated

    def metrics_between(self, start: datetime, end: datetime) -> list[dict]:
        sql = self._sql("SELECT * FROM post_metrics WHERE published_at>=? AND published_at<? ORDER BY published_at")
        with self._connection() as conn:
            rows = [self._dict(r) for r in conn.execute(
                sql, (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()]
        for row in rows:
            if isinstance(row["published_at"], str):
                row["published_at"] = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        return rows

    def publish_errors_between(self, start: datetime, end: datetime) -> list[str]:
        sql = self._sql("SELECT message FROM publication_errors WHERE created_at>=? AND created_at<? ORDER BY created_at")
        with self._connection() as conn:
            rows = conn.execute(sql, (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()
        return [str(r["message"]) for r in rows]

    def new_candidates_between(self, start: datetime, end: datetime) -> dict[str, int]:
        sql = self._sql("SELECT source, COUNT(*) AS n FROM candidates WHERE created_at>=? AND created_at<? GROUP BY source")
        with self._connection() as conn:
            rows = conn.execute(sql, (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()
        return {str(r["source"]): int(r["n"]) for r in rows}

    def content_published_this_hour(self) -> bool:
        local = utc_now().astimezone(ZoneInfo("America/Bogota"))
        start = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        with self._connection() as conn:
            row = conn.execute(self._sql("SELECT 1 FROM content_publications WHERE published_at>=?"
                                         " AND kind IN ('dato', 'noticia') LIMIT 1"), (start,)).fetchone()
        return row is not None

    def top_published_today(self) -> bool:
        local_day = utc_now().astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        sql = self._sql("SELECT 1 FROM publications WHERE platform='facebook_top' AND local_day=? LIMIT 1")
        with self._connection() as conn:
            return conn.execute(sql, (local_day,)).fetchone() is not None

    def record_top(self, candidate_ids: list[str], post_id: str) -> None:
        """Mark the carousel's deals as published (one row each; the suffix keeps external ids unique)."""
        now = utc_now()
        local_day = now.astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        with self._transaction(immediate=True) as conn:
            for i, cid in enumerate(candidate_ids, 1):
                conn.execute(
                    self._sql("UPDATE candidates SET status='published', updated_at=? WHERE id=? AND status='approved'"),
                    (now.isoformat(), cid),
                )
                conn.execute(
                    self._sql(
                        "INSERT INTO publications(candidate_id, platform, external_post_id, published_at, local_day)"
                        " VALUES(?, 'facebook_top', ?, ?, ?) ON CONFLICT(candidate_id, platform) DO NOTHING"
                    ),
                    (cid, f"{post_id}:{i}", now.isoformat(), local_day),
                )

    def promo_published_today(self) -> bool:
        local_day = utc_now().astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        with self._connection() as conn:
            return conn.execute(self._sql("SELECT 1 FROM promo_publications WHERE local_day=? LIMIT 1"), (local_day,)).fetchone() is not None

    def promo_posted_within(self, promo_key: str, days: int) -> bool:
        since = (utc_now() - timedelta(days=days)).isoformat()
        sql = self._sql("SELECT 1 FROM promo_publications WHERE promo_key=? AND published_at>=? LIMIT 1")
        with self._connection() as conn:
            return conn.execute(sql, (promo_key, since)).fetchone() is not None

    def record_promo(self, promo_key: str, external_post_id: str) -> None:
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                self._sql("INSERT INTO promo_publications(promo_key, external_post_id, published_at, local_day) VALUES(?, ?, ?, ?)"),
                (promo_key, external_post_id, now.isoformat(), now.astimezone(ZoneInfo("America/Bogota")).date().isoformat()),
            )

    def reel_published_this_hour(self) -> bool:
        local = utc_now().astimezone(ZoneInfo("America/Bogota"))
        hour_start = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        sql = self._sql("SELECT 1 FROM publications WHERE platform='facebook_reel' AND published_at>=? LIMIT 1")
        with self._connection() as conn:
            return conn.execute(sql, (hour_start,)).fetchone() is not None

    def expire_stale(self, max_age_hours: int) -> int:
        cutoff = (utc_now() - timedelta(hours=max_age_hours)).isoformat()
        with self._transaction() as conn:
            cursor = conn.execute(
                self._sql("UPDATE candidates SET status='expired', updated_at=? WHERE status='approved' AND observed_at < ?"),
                (utc_now().isoformat(), cutoff),
            )
            return cursor.rowcount

    def _candidate(self, row: Any) -> Candidate | None:
        data = self._dict(row)
        if not data:
            return None
        observed = datetime.fromisoformat(str(data["observed_at"]).replace("Z", "+00:00"))
        created = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
        deal = DealObservation(
            source=data["source"], external_id=data["external_id"], title=data["title"],
            price_minor=int(data["price_minor"]), original_price_minor=(
                int(data["original_price_minor"]) if data["original_price_minor"] is not None else None
            ), currency=data["currency"], evidence=DiscountEvidence(data["evidence"]),
            url=data["url"], image_url=data["image_url"], affiliate=bool(data["affiliate"]),
            shipping_note=data["shipping_note"], observed_at=observed,
            rating=float(data["rating"]) if data.get("rating") is not None else None,
            reviews=int(data["reviews"]) if data.get("reviews") is not None else None,
        )
        return Candidate(id=data["id"], deal=deal, score=int(data["score"]),
                         status=data["status"], created_at=created)

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        sql = self._sql("SELECT * FROM candidates WHERE id=?")
        with self._connection() as conn:
            return self._candidate(conn.execute(sql, (candidate_id,)).fetchone())

    def list_candidates(self, status: str = "pending", limit: int = 100) -> list[Candidate]:
        if status not in VALID_STATUSES:
            raise ValueError(f"Estado inválido: {status}")
        sql = self._sql("SELECT * FROM candidates WHERE status=? ORDER BY score DESC, created_at ASC LIMIT ?")
        with self._connection() as conn:
            rows = conn.execute(sql, (status, limit)).fetchall()
        return [candidate for row in rows if (candidate := self._candidate(row))]

    def decide(self, candidate_id: str, decision: str, actor: str = "user") -> bool:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Decisión inválida.")
        now = utc_now().isoformat()
        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                self._sql("UPDATE candidates SET status=?, updated_at=? WHERE id=? AND status IN ('pending','failed')"),
                (decision, now, candidate_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                self._sql("INSERT INTO approvals(candidate_id, actor, decision, created_at) VALUES(?, ?, ?, ?)"),
                (candidate_id, actor, decision, now),
            )
            return True

    def reserve_for_publish(self, candidate_id: str, daily_limit: int) -> bool:
        now = utc_now()
        local_day = now.astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        with self._transaction(immediate=True) as conn:
            count_row = conn.execute(
                self._sql("SELECT COUNT(*) AS total FROM candidates WHERE reserved_day=? AND status IN ('publishing','published','unknown')"),
                (local_day,),
            ).fetchone()
            if int(count_row["total"]) >= daily_limit:
                return False
            cursor = conn.execute(
                self._sql(
                    """
                    UPDATE candidates SET status='publishing', reserved_day=?, reserved_at=?, updated_at=?
                    WHERE id=? AND status='approved'
                    """
                ),
                (local_day, now.isoformat(), now.isoformat(), candidate_id),
            )
            return cursor.rowcount == 1

    def record_publication(self, candidate_id: str, external_post_id: str, platform: str = "facebook") -> None:
        now = utc_now()
        local_day = now.astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        with self._transaction(immediate=True) as conn:
            conn.execute(
                self._sql(
                    """
                    INSERT INTO publications(candidate_id, platform, external_post_id, published_at, local_day)
                    VALUES(?, ?, ?, ?, ?) ON CONFLICT(candidate_id, platform) DO NOTHING
                    """
                ),
                (candidate_id, platform, external_post_id, now.isoformat(), local_day),
            )
            conn.execute(
                self._sql("UPDATE candidates SET status='published', updated_at=? WHERE id=? AND status='publishing'"),
                (now.isoformat(), candidate_id),
            )

    def record_publish_error(self, candidate_id: str, message: str, ambiguous: bool) -> None:
        now = utc_now().isoformat()
        status = "unknown" if ambiguous else "failed"
        safe_message = message.replace("\n", " ")[:500]
        with self._transaction(immediate=True) as conn:
            conn.execute(
                self._sql("UPDATE candidates SET status=?, updated_at=? WHERE id=? AND status='publishing'"),
                (status, now, candidate_id),
            )
            conn.execute(
                self._sql("INSERT INTO publication_errors(candidate_id, error_kind, message, created_at) VALUES(?, ?, ?, ?)"),
                (candidate_id, "ambiguous" if ambiguous else "known_failure", safe_message, now),
            )

    def rotate_integration_credentials(
        self,
        provider: str,
        callback: Callable[[dict[str, Any] | None], dict[str, str]],
    ) -> dict[str, str]:
        """Serialize token refresh and atomically store the encrypted replacement pair."""
        with self._transaction(immediate=True) as conn:
            if self.is_postgres:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (provider,))
            suffix = " FOR UPDATE" if self.is_postgres else ""
            row = conn.execute(
                self._sql(f"SELECT * FROM integration_credentials WHERE provider=?{suffix}"),
                (provider,),
            ).fetchone()
            state = callback(self._dict(row))
            now = utc_now().isoformat()
            conn.execute(
                self._sql(
                    """
                    INSERT INTO integration_credentials(provider, access_token_enc, refresh_token_enc, expires_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                      access_token_enc=excluded.access_token_enc,
                      refresh_token_enc=excluded.refresh_token_enc,
                      expires_at=excluded.expires_at,
                      updated_at=excluded.updated_at
                    """
                ),
                (provider, state["access_token_enc"], state["refresh_token_enc"], state["expires_at"], now),
            )
            return state
