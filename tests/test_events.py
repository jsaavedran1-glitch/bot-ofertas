from dataclasses import replace
from datetime import date, datetime
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image

import main
from app.config import _parse_hourly
from app.copywriter import facebook_copy
from app.database import Database
from app.events import EVENTS, active_event, countdown, countdown_caption, event_tag
from app.models import DealObservation, DiscountEvidence
from tests.helpers import settings_for
from tests.test_content import FakePublisher
from tests.test_schedule import FakeDb

DEAL = DealObservation(source="alkosto", external_id="1", title="Freidora de Aire Kalley 4.5 Litros",
                       price_minor=19_990_000, original_price_minor=52_990_000,
                       evidence=DiscountEvidence.OFFICIAL_ORIGINAL, currency="COP", url="https://alkosto.com/x")


class EventTests(unittest.TestCase):
    def test_calendar(self):
        self.assertIsNone(active_event(date(2026, 11, 26)))
        self.assertEqual(active_event(date(2026, 11, 27)).name, "Black Friday")
        self.assertEqual(active_event(date(2026, 11, 29)).name, "Black Friday")    # the whole weekend
        self.assertEqual(active_event(date(2026, 11, 30)).name, "Cyber Monday")
        self.assertIsNone(active_event(date(2026, 12, 1)))
        self.assertEqual(event_tag(date(2026, 11, 13)), "OFERTA DEL DÍA")
        self.assertEqual(event_tag(date(2026, 11, 28)), "BLACK FRIDAY")
        self.assertIsNone(countdown(date(2026, 11, 19)))
        self.assertEqual(countdown(date(2026, 11, 20))[1], 7)
        self.assertEqual(countdown(date(2026, 11, 26))[1], 1)
        self.assertEqual(countdown(date(2026, 11, 27))[1], 0)                      # "hoy es"
        self.assertIsNone(countdown(date(2026, 11, 28)))
        self.assertEqual(countdown(date(2026, 11, 30))[0].name, "Cyber Monday")    # "hoy es Cyber Monday"

    def test_captions_during_the_event(self):
        with patch("app.events.today_bogota", return_value=date(2026, 11, 27)):
            text = facebook_copy(DEAL, None)
        self.assertTrue(text.startswith("🖤 Black Friday: "), text.splitlines()[0])
        self.assertTrue(text.rstrip().endswith("#OjoAlPrecio #BlackFriday #alkosto"))
        with patch("app.events.today_bogota", return_value=date(2026, 11, 13)):
            self.assertFalse(facebook_copy(DEAL, None).startswith("🖤"))
        one = countdown_caption(EVENTS[0], 1, "Alkosto")
        self.assertIn("Faltan 1 día para el Black Friday (viernes 27 de noviembre)", one)

    def test_more_posts_per_hour_only_on_event_days(self):
        with tempfile.TemporaryDirectory() as d:
            s = replace(settings_for(d), hourly_posts=_parse_hourly("17:5"), event_boost=1.3)
        bogota = ZoneInfo("America/Bogota")
        self.assertEqual(main.posts_due(s, FakeDb(), datetime(2026, 11, 13, 17, 40, tzinfo=bogota)), 5)
        self.assertEqual(main.posts_due(s, FakeDb(), datetime(2026, 11, 27, 17, 40, tzinfo=bogota)), 6)   # round(6.5)

    def test_countdown_posts_once_a_day(self):
        with tempfile.TemporaryDirectory() as d:
            settings = settings_for(d)
            db = Database(settings.database_url)
            db.initialize()
            publisher = FakePublisher()
            self.assertTrue(main.publish_countdown(settings, db, date(2026, 11, 22), publisher))
            self.assertFalse(main.publish_countdown(settings, db, date(2026, 11, 22), publisher))  # already today
            self.assertFalse(main.publish_countdown(settings, db, date(2026, 11, 10), publisher))  # too early
            image, caption = publisher.photos[0]
            with Image.open(image) as img:
                self.assertEqual(img.size, (1080, 1350))
            self.assertIn("Faltan 5 días", caption)
            self.assertEqual(db.content_history()[0][:2], ("evento", "black-friday-2026:2026-11-22"))


if __name__ == "__main__":
    unittest.main()
