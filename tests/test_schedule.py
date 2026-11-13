from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import main
from app.config import _parse_hourly
from app.database import Database
from tests.helpers import settings_for

BOGOTA = ZoneInfo("America/Bogota")
PLAN = "8:3,9:5,10:4,11:4,12:5,13:4,14:4,15:4,16:4,17:5,18:5,19:5,20:4,21:4,22:3"


def at(hour, minute):
    return datetime(2026, 11, 13, hour, minute, tzinfo=BOGOTA)


class FakeDb:
    def __init__(self, this_hour=0, since=None, reel_done=False):
        self.this_hour, self.since, self.reel_done = this_hour, since, reel_done

    def posts_this_hour(self):
        return self.this_hour

    def minutes_since_last_post(self):
        return self.since

    def promo_published_today(self):
        return True

    def top_published_today(self):
        return True

    def reel_published_this_hour(self):
        return self.reel_done


class FakePipeline:
    def __init__(self, db, fail=()):
        self.db, self.fail, self.calls = db, set(fail), []

    def publish(self, candidate_id, as_reel=False):
        self.calls.append((candidate_id, as_reel))
        if candidate_id in self.fail:
            raise ValueError("sin foto")
        if as_reel:
            self.db.reel_done = True
        return f"post_{candidate_id}"


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds))
        self.now += seconds

    def __call__(self):
        return self.now


class HourlyPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(settings_for(self.temp.name), hourly_posts=_parse_hourly(PLAN),
                                max_posts_per_day=70, min_minutes_between_posts=4, reel_hours=frozenset({19}))

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_is_half_the_competitor_volume_weighted_to_daytime(self):
        plan = _parse_hourly(PLAN)
        self.assertEqual(sum(plan), 63)
        self.assertEqual(sum(plan[9:17]), 34)
        self.assertEqual(sum(plan[17:23]), 26)
        self.assertEqual(plan[7], 0)
        with self.assertRaises(ValueError):
            _parse_hourly("24:2")

    def test_each_half_hour_run_makes_its_share(self):
        s = self.settings
        self.assertEqual(main.posts_due(s, FakeDb(), at(10, 1)), 2)      # first half: half of 4
        self.assertEqual(main.posts_due(s, FakeDb(2), at(10, 31)), 2)    # second half: the rest
        self.assertEqual(main.posts_due(s, FakeDb(1), at(19, 2)), 2)     # 5 -> 3 in the first half
        self.assertEqual(main.posts_due(s, FakeDb(4), at(13, 45)), 0)    # quota already met
        self.assertEqual(main.posts_due(s, FakeDb(), at(23, 5)), 0)
        legacy = replace(s, hourly_posts=())
        self.assertEqual(main.posts_due(legacy, FakeDb(), at(17, 1)), 1)  # without the plan: one per run

    def test_posts_are_spread_over_the_half_hour(self):
        self.assertEqual(main.spacing_minutes(at(17, 1), 1), 0)
        self.assertEqual(main.spacing_minutes(at(17, 1), 4), 7)
        self.assertEqual(main.spacing_minutes(at(17, 27), 3), 3)          # never closer than 3 minutes
        self.assertEqual(main.spacing_minutes(at(8, 0), 1), 0)

    def test_run_publishes_spaced_reel_first_then_photos(self):
        db, clock = FakeDb(), FakeClock()
        pipeline = FakePipeline(db, fail={"a2"})
        with patch.object(main, "publish_queues", return_value=[["a1", "a2", "a3"], ["b1", "b2"]]):
            done = main.publish_slot(self.settings, db, pipeline, now=at(19, 1), sleep=clock.sleep, clock=clock)
        self.assertEqual(done, 3)                                         # 5 at 19h -> 3 in the first half
        self.assertEqual(pipeline.calls[0], ("a1", True))                # the hour's reel goes first
        self.assertTrue(all(not reel for _, reel in pipeline.calls[1:]))
        self.assertEqual([c for c, _ in pipeline.calls].count("a2"), 1)  # a failed deal is not retried
        self.assertEqual([s for s in clock.sleeps if s], [560, 560])     # one every 9 1/3 minutes

    def test_failed_reel_falls_back_to_photos(self):
        db, clock = FakeDb(), FakeClock()
        pipeline = FakePipeline(db, fail={"a1"})
        with patch.object(main, "publish_queues", return_value=[["a1", "a2"], ["b1"]]):
            done = main.publish_slot(self.settings, db, pipeline, now=at(19, 31), sleep=clock.sleep, clock=clock)
        self.assertEqual(done, 2)                                         # 5 due, only two other deals left
        self.assertEqual(pipeline.calls, [("a1", True), ("b1", False), ("a2", False)])

    def test_waits_out_the_minimum_gap_instead_of_skipping(self):
        db, clock = FakeDb(since=1.5), FakeClock()
        with patch.object(main, "publish_queues", return_value=[["a1"]]):
            main.publish_slot(self.settings, db, FakePipeline(db), now=at(8, 0), sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.sleeps[0], 150)

    def test_posts_this_hour_counts_a_carousel_once(self):
        db = Database(self.settings.database_url)
        db.initialize()
        db.record_promo("alkosto:lg", "promo_1")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(f"{self.temp.name}/test.db") as conn:  # no foreign keys: no candidate rows needed
            conn.executemany("INSERT INTO publications(candidate_id, platform, external_post_id, published_at, local_day)"
                             " VALUES(?, 'facebook_top', ?, ?, 'x')", [(f"c{i}", f"top_9:{i}", now) for i in (1, 2, 3)])
            conn.execute("INSERT INTO publications(candidate_id, platform, external_post_id, published_at, local_day)"
                         " VALUES('old', 'facebook', 'p_old', '2026-01-01T00:00:00+00:00', 'x')")
        self.assertEqual(db.posts_this_hour(), 2)  # promo + carousel; last January's post doesn't count


if __name__ == "__main__":
    unittest.main()
