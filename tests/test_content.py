from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from PIL import Image

import main
from app.ai import ClaudeHelper
from app.content import (ContentPost, ContentRenderer, NewsItem, content_caption, load_tips, news_post,
                         next_tip, parse_feed)
from app.database import Database
from tests.helpers import settings_for
from tests.test_ai import FakeClient

NOW = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>PlayStation Plus: juegos de diciembre</title><link>https://blog.example/ps-plus?utm_source=rss</link>
<description>&lt;p&gt;Tres juegos llegan el 2 de diciembre.&lt;/p&gt;</description><pubDate>Fri, 13 Nov 2026 14:00:00 GMT</pubDate></item>
<item><title>Sin enlace seguro</title><link>http://blog.example/x</link><pubDate>Fri, 13 Nov 2026 14:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Alerta por tiendas falsas</title><link href="https://sic.example/alerta"/>
<summary>La entidad pide revisar el NIT antes de pagar.</summary><updated>2026-11-13T10:00:00Z</updated></entry></feed>"""

TIP = ContentPost("dato", "rne", "Cómo frenar las llamadas de publicidad", ("Inscribe tu número en el RNE", "Es gratis"),
                  "¿Te llaman mucho?", ("#consumidor", "#Colombia"), "CRC", "https://crc.example/rne")


class FakePublisher:
    def __init__(self):
        self.photos, self.comments, self.stories = [], [], []

    def publish_story(self, image):
        self.stories.append(image)
        return f"story_{len(self.stories)}"

    def publish_photo(self, image, caption):
        self.photos.append((image, caption))
        return f"post_{len(self.photos)}"

    def comment(self, post_id, message):
        self.comments.append((post_id, message))


class FakeFeeds:
    def __init__(self, items):
        self.items, self.skipped = items, None

    def recent(self, skip):
        self.skipped = skip
        return [i for i in self.items if i.link not in skip]


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_for(self.temp.name)
        self.db = Database(self.settings.database_url)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_feeds_parse_rss_and_atom_keeping_only_https_links(self):
        rss = parse_feed("PlayStation Blog", RSS)
        self.assertEqual([i.title for i in rss], ["PlayStation Plus: juegos de diciembre"])
        self.assertEqual(rss[0].link, "https://blog.example/ps-plus")             # tracking removed
        self.assertEqual(rss[0].summary, "Tres juegos llegan el 2 de diciembre.")  # html stripped
        atom = parse_feed("SIC", ATOM)
        self.assertEqual((atom[0].link, atom[0].published.hour), ("https://sic.example/alerta", 10))

    def test_tips_rotate_and_rest_two_weeks(self):
        other = ContentPost("dato", "retracto", "Retracto", ("a", "b"), "", (), "SIC", "https://x")
        self.assertEqual(next_tip([TIP, other], {}, now=NOW), TIP)                          # never posted: file order
        self.assertEqual(next_tip([TIP, other], {"rne": NOW - timedelta(days=1)}, now=NOW), other)
        recent = {"rne": NOW - timedelta(days=2), "retracto": NOW - timedelta(days=3)}
        self.assertIsNone(next_tip([TIP, other], recent, now=NOW))                          # all too recent

    def test_the_bundled_tip_bank_is_complete(self):
        tips = load_tips(today=date(2026, 11, 13))
        self.assertGreaterEqual(len(tips), 15)
        self.assertEqual(len({t.key for t in tips}), len(tips))
        for t in tips:
            # The Senate's law pages only answer over http; everything else is https.
            self.assertTrue(t.url.startswith(("https://", "http://www.secretariasenado.gov.co/")), t.key)
            self.assertTrue(2 <= len(t.points) <= 3 and all(len(p) <= 95 for p in t.points), t.key)
            self.assertLessEqual(len(t.title), 70, t.key)

    def test_dated_tips_expire(self):
        keys = lambda day: {t.key for t in load_tips(today=day)}
        self.assertIn("black-friday-2026-fechas", keys(date(2026, 11, 30)))
        self.assertNotIn("black-friday-2026-fechas", keys(date(2026, 12, 1)))

    def test_claude_news_text_cannot_invent_numbers(self):
        item = parse_feed("PlayStation Blog", RSS)[0]
        good = {"indice": 0, "titular": "Llegan los juegos de PS Plus de diciembre",
                "puntos": ["Son tres juegos para suscriptores", "Estarán desde el 2 de diciembre"],
                "pregunta": "¿Cuál juegas primero?", "hashtags": ["#PlayStation", "#PSPlus"]}
        picked = ClaudeHelper(client=FakeClient(good)).pick_news([item])
        self.assertEqual(picked[1]["points"][1], "Estarán desde el 2 de diciembre")
        invented = dict(good, puntos=["Cuestan 9 dólares", "Estarán desde el 2 de diciembre"])
        self.assertIsNone(ClaudeHelper(client=FakeClient(invented)).pick_news([item]))
        self.assertIsNone(ClaudeHelper(client=FakeClient(dict(good, indice=-1))).pick_news([item]))

    def test_card_and_caption_carry_the_source(self):
        image = ContentRenderer(Path(self.temp.name)).render_content(TIP, "dato_test")
        with Image.open(image) as img:
            self.assertEqual(img.size, (1080, 1350))
        caption = content_caption(TIP)
        self.assertIn("💡 Cómo frenar", caption)
        self.assertIn("📌 Fuente: CRC (el enlace está en los comentarios 👇)", caption)
        self.assertTrue(caption.endswith("#OjoAlPrecio #consumidor #Colombia"))

    def test_publishing_alternates_tip_and_news_and_links_the_source(self):
        item = NewsItem("SIC", "Alerta por tiendas falsas en Black Friday", "Revise el NIT antes de pagar.",
                        "https://sic.example/alerta", NOW)
        texts = {"indice": 0, "titular": "Ojo con las tiendas falsas en Black Friday",
                 "puntos": ["Revisa el NIT de la tienda", "Hazlo antes de pagar"], "pregunta": "", "hashtags": []}
        pipeline = SimpleNamespace(ai=ClaudeHelper(client=FakeClient(texts)))
        settings = self.settings.__class__(**{**self.settings.__dict__, "link_in_comment": True})
        publisher, feeds = FakePublisher(), FakeFeeds([item])
        self.assertTrue(main.publish_content(settings, self.db, pipeline, feeds, [TIP], publisher))   # first: tip
        self.assertTrue(main.publish_content(settings, self.db, pipeline, feeds, [TIP], publisher))   # then: news
        self.assertEqual([k for k, _, _ in self.db.content_history()], ["dato", "noticia"])
        self.assertEqual(publisher.comments[1], ("post_2", "📌 Fuente: https://sic.example/alerta"))
        self.assertTrue(self.db.content_published_this_hour())
        # Third time: the tip rests two weeks and the news item was used, so nothing is posted.
        self.assertFalse(main.publish_content(settings, self.db, pipeline, feeds, [TIP], publisher))
        self.assertEqual(feeds.skipped, {"https://sic.example/alerta"})

    def test_only_the_hours_first_post_goes_to_the_story(self):
        item = NewsItem("SIC", "Alerta por tiendas falsas", "Revise el NIT antes de pagar.", "https://sic.example/a", NOW)
        texts = {"indice": 0, "titular": "Ojo con las tiendas falsas", "puntos": ["Revisa el NIT", "Antes de pagar"],
                 "pregunta": "", "hashtags": []}
        pipeline = SimpleNamespace(ai=ClaudeHelper(client=FakeClient(texts)))
        settings = self.settings.__class__(**{**self.settings.__dict__, "stories": True, "stories_per_hour": 1})
        publisher = FakePublisher()
        main.publish_content(settings, self.db, pipeline, FakeFeeds([item]), [TIP], publisher)   # 1st of the hour
        main.publish_content(settings, self.db, pipeline, FakeFeeds([item]), [TIP], publisher)   # 2nd: no story
        self.assertEqual(len(publisher.photos), 2)
        self.assertEqual(len(publisher.stories), 1)
        with Image.open(publisher.stories[0]) as story:
            self.assertEqual(story.size, (1080, 1920))
        off = self.settings.__class__(**{**self.settings.__dict__, "stories": False})
        self.db.record_content("dato", "x", "post_x")  # (nothing else this hour matters when stories are off)
        main.post_story(off, self.db, publisher, publisher.photos[0][0], "h", "p")
        self.assertEqual(len(publisher.stories), 1)


if __name__ == "__main__":
    unittest.main()
