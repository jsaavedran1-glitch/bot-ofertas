import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.coupons import COUPONS_PATH, CouponRenderer, load_coupon_post

BOGOTA = ZoneInfo("America/Bogota")


class CouponPostTests(unittest.TestCase):
    def write(self, **extra) -> Path:
        data = {
            "id": "t", "store": "Mercado Libre", "caption": "Cupones #MercadoLibre",
            "coupons": [{"code": "ABC", "benefit": "15% OFF", "minimum": "$80.000"}], **extra,
        }
        path = Path(tempfile.mkdtemp()) / "cupones.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_repo_file_loads_and_renders(self):
        post = load_coupon_post(COUPONS_PATH)
        image = CouponRenderer(Path(tempfile.mkdtemp())).render_coupons(post)
        self.assertTrue(image.is_file())

    def test_due_slot_window(self):
        post = load_coupon_post(self.write(schedule=["2026-11-14 10:00"]))
        at = lambda h, m=0: datetime(2026, 11, 14, h, m, tzinfo=BOGOTA)
        self.assertIsNone(post.due_slot(at(9, 59), set()))
        self.assertEqual(post.due_slot(at(10, 0), set()), "t@2026-11-14 10:00")
        self.assertEqual(post.due_slot(at(12, 30), set()), "t@2026-11-14 10:00")
        self.assertIsNone(post.due_slot(at(13, 0), set()))  # too late: skipped, not posted at night
        self.assertIsNone(post.due_slot(at(10, 30), {"t@2026-11-14 10:00"}))  # already published

    def test_no_schedule_never_due(self):
        post = load_coupon_post(self.write())
        self.assertIsNone(post.due_slot(datetime(2026, 11, 14, 10, tzinfo=BOGOTA), set()))

    def test_bad_time_rejected(self):
        with self.assertRaises(ValueError):
            load_coupon_post(self.write(schedule=["mañana 10am"]))


if __name__ == "__main__":
    unittest.main()
