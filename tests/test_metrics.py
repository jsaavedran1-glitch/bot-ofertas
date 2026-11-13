from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import main
from app.database import Database
from app.metrics import MetricsError, PostStats, fetch_page_stats, weekly_report
from tests.helpers import settings_for

BOGOTA = ZoneInfo("America/Bogota")
PAGE = "615"


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return Resp(self.pages.pop(0))


def post(pid, reactions=0, comments=(), shares=0, target=None):
    data = {"id": pid, "reactions": {"summary": {"total_count": reactions}},
            "comments": {"data": [{"id": f"c{i}", "from": {"id": who}} for i, who in enumerate(comments)]}}
    if shares:
        data["shares"] = {"count": shares}
    if target:
        data["attachments"] = {"data": [{"target": {"id": target}}]}
    return data


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(settings_for(self.temp.name), fb_page_id=PAGE, fb_page_token="secret-token")
        self.db = Database(self.settings.database_url)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_counts_follow_paging_map_reels_and_skip_our_own_comments(self):
        session = FakeSession([
            {"data": [post("615_1", 4, comments=(PAGE, "u1", "u2"), shares=1)], "paging": {"next": "https://graph/next"}},
            {"data": [post("615_2", 7, comments=(PAGE,), target="9001")]},
        ])
        stats = fetch_page_stats(PAGE, "secret-token", "v26.0", datetime.now(timezone.utc), session)
        self.assertEqual(stats["615_1"], {"reactions": 4, "comments": 2, "shares": 1})
        self.assertEqual(stats["9001"], {"reactions": 7, "comments": 0, "shares": 0})  # the reel's video id
        self.assertEqual(session.calls[1], ("https://graph/next", None))

    def test_errors_never_show_the_token(self):
        session = FakeSession([{"error": {"code": 190, "message": "bad token secret-token"}}])
        with self.assertRaises(MetricsError) as ctx:
            fetch_page_stats(PAGE, "secret-token", "v26.0", datetime.now(timezone.utc), session)
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_collect_saves_every_kind_of_post(self):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(f"{self.temp.name}/test.db") as conn:
            conn.execute("INSERT INTO candidates(id, source, external_id, title, price_minor, currency, evidence, url,"
                         " status, score, observed_at, created_at, updated_at) VALUES('c1', 'alkosto', 'x', 'TV',"
                         " 1, 'COP', 'official_original', 'https://a', 'published', 1, ?, ?, ?)", (now, now, now))
            conn.execute("INSERT INTO publications(candidate_id, platform, external_post_id, published_at, local_day)"
                         " VALUES('c1', 'facebook_reel', '9001', ?, 'x')", (now,))
        self.db.record_promo("nike:air", "615_5")
        self.db.record_content("dato", "rne", "615_6")
        session = FakeSession([{"data": [post("615_5", 2), post("615_6", 9, comments=("u1",)), post("615_7", 1, target="9001")]}])
        self.assertEqual(main.collect_metrics(self.settings, self.db, session=session), 3)
        rows = {r["post_id"]: r for r in self.db.metrics_between(datetime.now(timezone.utc) - timedelta(days=1),
                                                                 datetime.now(timezone.utc) + timedelta(minutes=1))}
        self.assertEqual((rows["9001"]["kind"], rows["9001"]["source"], rows["9001"]["reactions"]), ("reel", "alkosto", 1))
        self.assertEqual((rows["615_5"]["kind"], rows["615_5"]["source"]), ("promo", "nike"))
        self.assertEqual((rows["615_6"]["kind"], rows["615_6"]["comments"]), ("dato", 1))

    def test_report_ranks_types_stores_and_hours(self):
        end = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        at = lambda d, h: datetime(2026, 11, d, h, tzinfo=BOGOTA)
        week = [PostStats("615_1", "oferta", "alkosto", at(10, 9), 1), PostStats("615_2", "dato", "contenido", at(11, 12), 20, 3, 2),
                PostStats("9001", "reel", "nike", at(12, 21), 0)]
        report = weekly_report(week, end - timedelta(days=7), end, [PostStats("1", "oferta", "x", at(3, 9))],
                               {"alkosto": "Alkosto", "nike": "Nike"})
        self.assertIn("**3 publicaciones** · 21 reacciones · 3 comentarios · 2 compartidos", report)
        self.assertIn("Semana anterior: 1 publicación", report)
        self.assertLess(report.index("| Dato útil |"), report.index("| Oferta (foto) |"))   # best type first
        self.assertIn("| 20–24 h | 1 | 0 |", report)
        self.assertIn("1. **25** (20 reacciones, 3 comentarios, 2 compartidos) · Dato útil", report)

    def test_slot_collects_at_its_hours_and_reports_on_monday_morning(self):
        with patch.object(main, "collect_metrics") as collect, patch.object(main, "write_report") as write, \
                patch.object(main, "build_report", return_value="informe"):
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 13, 5, tzinfo=BOGOTA))   # Friday 13 h
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 13, 35, tzinfo=BOGOTA))  # second half: no
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 10, 5, tzinfo=BOGOTA))   # not its hour
            self.assertEqual(collect.call_count, 1)
            write.assert_not_called()
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 16, 7, 2, tzinfo=BOGOTA))    # Monday 7 h
            write.assert_called_once_with("informe")


if __name__ == "__main__":
    unittest.main()
