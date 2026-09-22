from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo
import sqlite3

from app.models import Candidate, DealObservation, DiscountEvidence, utc_now


VALID_STATUSES = {"pending", "approved", "publishing", "published", "rejected", "failed", "unknown"}


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
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
            return psycopg.connect(self.url, row_factory=dict_row)
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
        try:
            if self.is_postgres:
                conn.execute("BEGIN")
            else:
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
            "CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status, score)",
            "CREATE INDEX IF NOT EXISTS idx_observations_item ON price_observations(source, external_id, observed_at)",
            "CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(platform, local_day)",
        ]
        with self._transaction() as conn:
            for statement in statements:
                conn.execute(statement)

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

    def create_candidate(self, deal: DealObservation, candidate_score: int) -> bool:
        now = utc_now().isoformat()
        sql = self._sql(
            """
            INSERT INTO candidates(id, source, external_id, title, price_minor,
              original_price_minor, currency, evidence, url, image_url, affiliate,
              shipping_note, observed_at, score, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(id) DO NOTHING
            """
        )
        with self._transaction() as conn:
            cursor = conn.execute(sql, (
                deal.candidate_id, deal.source, deal.external_id, deal.title, deal.price_minor,
                deal.original_price_minor, deal.currency, deal.evidence.value, deal.url,
                deal.image_url, int(deal.affiliate), deal.shipping_note,
                deal.observed_at.astimezone(timezone.utc).isoformat(), candidate_score, now, now,
            ))
            if cursor.rowcount == 1:
                return True
            # Seen again: keep the pending/approved candidate fresh so it stays publishable.
            conn.execute(
                self._sql(
                    "UPDATE candidates SET observed_at=?, url=?, image_url=?, affiliate=?, updated_at=?"
                    " WHERE id=? AND status IN ('pending','approved')"
                ),
                (deal.observed_at.astimezone(timezone.utc).isoformat(), deal.url, deal.image_url,
                 int(deal.affiliate), now, deal.candidate_id),
            )
            return False

    def last_published_source(self) -> str | None:
        sql = self._sql(
            "SELECT c.source FROM publications p JOIN candidates c ON c.id=p.candidate_id "
            "ORDER BY p.published_at DESC LIMIT 1"
        )
        with self._connection() as conn:
            row = self._dict(conn.execute(sql).fetchone())
        return row["source"] if row else None

    def published_today(self, source: str) -> int:
        local_day = utc_now().astimezone(ZoneInfo("America/Bogota")).date().isoformat()
        sql = self._sql(
            "SELECT COUNT(*) AS total FROM publications p JOIN candidates c ON c.id=p.candidate_id"
            " WHERE c.source=? AND p.local_day=?"
        )
        with self._connection() as conn:
            return int(conn.execute(sql, (source, local_day)).fetchone()["total"])

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
                self._sql("UPDATE candidates SET status='rejected', updated_at=? WHERE status='approved' AND observed_at < ?"),
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
