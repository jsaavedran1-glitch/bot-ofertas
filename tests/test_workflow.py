from pathlib import Path
import unittest


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_safe_by_default(self):
        text = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")
        self.assertIn("contents: read", text)
        self.assertIn("concurrency:", text)
        self.assertIn("timeout-minutes:", text)
        self.assertIn("python -m unittest", text)
        self.assertNotIn("actions/cache@", text)
        self.assertIn('POST_MODE: "review"', text)
        self.assertNotIn("FB_APP_SECRET", text)
