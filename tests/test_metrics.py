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
from app.metrics import NO_VIEWS, MetricsError, PostStats, daily_report, fetch_page_stats, fetch_views, weekly_report
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
        return Resp(self.pages.pop(0) if self.pages else {"data": []})


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

    def test_slot_collects_at_its_hours_and_reports_every_morning(self):
        with patch.object(main, "collect_metrics") as collect, patch.object(main, "write_report") as write, \
                patch.object(main, "build_report", return_value="semanal"), \
                patch.object(main, "build_daily_report", return_value="diario") as daily:
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 13, 5, tzinfo=BOGOTA))   # Friday 13 h
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 13, 35, tzinfo=BOGOTA))  # second half: no
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 13, 10, 5, tzinfo=BOGOTA))   # not its hour
            self.assertEqual(collect.call_count, 1)
            write.assert_not_called()
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 14, 7, 2, tzinfo=BOGOTA))    # Saturday 7 h
            write.assert_called_once_with("diario", main.DAILY_PATH)
            self.assertEqual(daily.call_args.args[3].isoformat(), "2026-11-13")                    # yesterday
            main.metrics_slot(self.settings, self.db, datetime(2026, 11, 16, 7, 2, tzinfo=BOGOTA))    # Monday 7 h
            self.assertEqual([c.args[0] for c in write.call_args_list], ["diario", "diario", "semanal"])

    def test_daily_report_covers_operation_and_waits_for_data_before_advice(self):
        from datetime import date
        from types import SimpleNamespace
        settings = replace(self.settings, hourly_posts=tuple([0] * 8 + [3] + [0] * 15))
        day = date(2026, 11, 12)
        at = lambda h: datetime(2026, 11, 12, h, 5, tzinfo=BOGOTA)
        self.db.save_metrics([("615_1", "oferta", "alkosto", at(9), 2, 0, 0), ("615_2", "dato", "contenido", at(9), 5, 1, 0)])
        with sqlite3.connect(f"{self.temp.name}/test.db") as conn:
            conn.execute("INSERT INTO publication_errors(candidate_id, error_kind, message, created_at) VALUES"
                         " ('c9', 'known_failure', 'La oferta cambió desde su aprobación; escanéala', ?)", (at(10).isoformat(),))
        asked = []
        ai = SimpleNamespace(recommend=lambda report: asked.append(report) or ["Publica más datos útiles a las 9 h"])
        published = [("615_1", "oferta", "alkosto", at(9)), ("615_2", "dato", "contenido", at(9))]
        with patch.object(main, "publish_queues", return_value=[["a", "b"], ["c"]]), \
                patch.object(self.db, "posts_since", return_value=published):
            report = main.build_daily_report(settings, self.db, SimpleNamespace(ai=ai), day)
        self.assertIn("- Publicadas: 1 oferta con foto, 1 dato útil", report)
        self.assertIn("# Informe diario · jueves 12/11/2026", report)
        self.assertIn("**2 de 3 publicaciones**", report)
        self.assertIn("- Fallos: 1 (1 × La oferta cambió desde su aprobación; escanéala)", report)
        self.assertIn("- Cola: 3 productos distintos listos para publicar (alcanza para ~1,0 días)", report)
        self.assertIn("Todavía no hay suficientes reacciones", report)   # 8 interactions < 15
        self.assertEqual(asked, [])
        self.db.save_metrics([("615_3", "noticia", "contenido", at(9), 9, 0, 0)])
        with patch.object(main, "publish_queues", return_value=[]), patch.object(self.db, "posts_since", return_value=published):
            report = main.build_daily_report(settings, self.db, SimpleNamespace(ai=ai), day)
        self.assertIn("## Recomendaciones (Claude)\n\n- Publica más datos útiles a las 9 h", report)

def seen(pid, views=None, viewers=None, target=None):
    data = {"id": pid}
    if views is not None:
        data["insights"] = {"data": [{"name": "post_media_view", "values": [{"value": views}]},
                                     {"name": "post_total_media_view_unique", "values": [{"value": viewers}]}]}
    if target:
        data["attachments"] = {"data": [{"target": {"id": target}}]}
    return data


class ViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(settings_for(self.temp.name), fb_page_id=PAGE, fb_page_token="secret-token")
        self.db = Database(self.settings.database_url)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_views_accounts_and_reel_plays(self):
        session = FakeSession([
            {"data": [seen("615_1", 40, 31), seen("615_2", 12, 9, target="9001")]},
            {"data": [{"name": "blue_reels_play_count", "values": [{"value": 25}]},
                      {"name": "post_impressions_unique", "values": [{"value": 20}]}]},
        ])
        views, problem = fetch_views(PAGE, "secret-token", "v26.0", datetime.now(timezone.utc), ["9001"], session)
        self.assertIsNone(problem)
        self.assertEqual(views["615_1"], {"views": 40, "viewers": 31, "plays": None})
        self.assertEqual(views["9001"], {"views": 12, "viewers": 9, "plays": 25})   # the reel's video id
        self.assertIn("insights.metric(post_media_view,post_total_media_view_unique)", session.calls[0][1]["fields"])
        self.assertTrue(session.calls[1][0].endswith("/9001/video_insights"))

    def test_empty_insights_say_why_and_errors_hide_the_token(self):
        session = FakeSession([{"data": [seen("615_1"), seen("615_2")]}, {"error": {"code": 10, "message": "x"}}])
        views, problem = fetch_views(PAGE, "secret-token", "v26.0", datetime.now(timezone.utc), ["9001", "9002"], session)
        self.assertEqual((views, problem), ({}, NO_VIEWS))
        self.assertEqual(len(session.calls), 2)   # one failed reel is enough: the others aren't asked
        session = FakeSession([{"error": {"code": 100, "message": "bad secret-token"}}])
        views, problem = fetch_views(PAGE, "secret-token", "v26.0", datetime.now(timezone.utc), (), session)
        self.assertIn("código 100", problem)
        self.assertNotIn("secret-token", problem)

    def test_collect_keeps_views_and_the_report_ranks_by_them(self):
        from datetime import date
        day = date(2026, 11, 12)
        at = lambda h: datetime(2026, 11, 12, h, 5, tzinfo=BOGOTA)
        self.db.save_metrics([("615_1", "oferta", "alkosto", at(9), 0, 0, 0), ("615_2", "oferta", "amazon", at(18), 1, 0, 0),
                              ("9001", "reel", "nike", at(13), 0, 0, 0)])
        self.assertEqual(self.db.save_views({"615_1": {"views": 40, "viewers": 31}, "615_2": {"views": 4, "viewers": 3},
                                             "9001": {"views": 12, "viewers": 9, "plays": 25}, "nope": {"views": 1}}), 3)
        self.db.save_views({"615_1": {"views": None, "viewers": None, "plays": None}})   # a gap never erases data
        self.db.save_metrics([("615_1", "oferta", "alkosto", at(9), 2, 0, 0)])             # reactions refresh keeps views
        rows = self.db.metrics_between(at(0), at(23))
        posts = main._stats(rows)
        self.assertEqual([(p.post_id, p.reactions, p.views) for p in posts], [("615_1", 2, 40), ("9001", 0, 12), ("615_2", 1, 4)])
        report = daily_report(posts, posts, {"planned": 3}, day, {"alkosto": "Alkosto", "amazon": "Amazon", "nike": "Nike"})
        self.assertIn("**18,7 vistas por post** · 14,3 cuentas por post · 56 vistas en total", report)
        self.assertIn("- Reels: 25 reproducciones en 1 reel (25,0 por reel)", report)
        self.assertIn("Vistas por post | Cuentas por post |", report)
        self.assertLess(report.index("| Alkosto |"), report.index("| Amazon |"))   # ranked by views per post
        self.assertIn("1. **40 vistas** · 31 cuentas · 2 interacciones · Oferta (foto) · Alkosto · 09:05", report)
        self.assertNotIn("sin datos", report)

    def test_report_says_when_there_are_no_views(self):
        from datetime import date
        at = datetime(2026, 11, 12, 9, 5, tzinfo=BOGOTA)
        posts = [PostStats("615_1", "oferta", "alkosto", at, 1)]
        report = daily_report(posts, posts, {"planned": 1}, date(2026, 11, 12), {})
        self.assertIn("- Vistas y cuentas: sin datos. Meta no entrega vistas", report)
        self.assertNotIn("Vistas por post", report)
        self.assertIn("## Las 3 con más interacciones", report)


class TokenCheckTests(unittest.TestCase):
    def test_lists_missing_permissions_instagram_and_views_without_the_token(self):
        from app.token_check import check_token
        session = FakeSession([
            {"data": {"type": "PAGE", "is_valid": True, "expires_at": 0,
                      "scopes": ["pages_show_list", "pages_manage_posts", "pages_read_engagement"]}},
            {"name": "Ojo al Precio", "fan_count": 155, "followers_count": 155},
            {"data": [{"id": "615_1"}, {"id": "615_2"}]},
        ])
        lines = check_token(PAGE, "secret-token", "v26.0", session)
        text = "\n".join(lines)
        self.assertIn("Token de página · válido · no vence", text)
        self.assertIn("OK  pages_manage_posts", text)
        self.assertIn("FALTA read_insights", text)
        self.assertIn("FALTA instagram_content_publish", text)
        self.assertIn("Instagram: ninguna cuenta profesional vinculada", text)
        self.assertIn("Vistas: Meta responde vacío", text)
        self.assertNotIn("secret-token", text)

    def test_linked_instagram_and_readable_views(self):
        from app.token_check import check_token
        session = FakeSession([
            {"error": {"code": 190, "message": "no secret-token"}},
            {"name": "Ojo al Precio", "fan_count": 155, "instagram_business_account": {"id": "17", "username": "ojoalprecio.co"}},
            {"data": [seen("615_1", 10, 8), seen("615_2")]},
        ])
        text = "\n".join(check_token(PAGE, "secret-token", "v26.0", session))
        self.assertIn("Meta no deja ver la lista con este token (código 190)", text)
        self.assertIn("Instagram: vinculada @ojoalprecio.co", text)
        self.assertIn("Vistas: se leen (1 de 2", text)
        self.assertNotIn("secret-token", text)


if __name__ == "__main__":
    unittest.main()


class AdviceTests(unittest.TestCase):
    def test_advice_cannot_invent_numbers(self):
        from app.ai import ClaudeHelper
        from tests.test_ai import FakeClient
        report = "Las ofertas de las 9 h tuvieron 12 reacciones."
        advice = {"recomendaciones": ["Mantén las 9 h para ofertas: tuvieron 12 reacciones", "Sube a 40 posts diarios"]}
        self.assertEqual(ClaudeHelper(client=FakeClient(advice)).recommend(report),
                         ["Mantén las 9 h para ofertas: tuvieron 12 reacciones"])
