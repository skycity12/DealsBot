import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main_integration import MainBase  # noqa: E402

M = {"split_mode": "mirror", "max_alerts_per_day": 100, "fb_max_per_day": 100}


class TestMirrorMode(MainBase):
    def posted(self, platform):
        return re.findall(rf"\[post\]\[{platform}\] (\w+)", self.out)

    def test_every_telegram_post_also_goes_to_facebook(self):
        st = self.run_main({**M, "telegram_per_run": 10})
        tg, fb = self.posted("telegram"), self.posted("facebook")
        self.assertEqual(len(tg), 10)
        self.assertEqual(sorted(tg), sorted(fb))                        # same deals on both
        self.assertIn("mirror mode: 10 deals -> Telegram", self.out)
        self.assertEqual(len(st["posted"]["facebook"]), 10)

    def test_facebook_daily_cap_limits_only_facebook(self):
        self.run_main({**M, "telegram_per_run": 10, "fb_max_per_day": 4})
        tg, fb = self.posted("telegram"), self.posted("facebook")
        self.assertEqual((len(tg), len(fb)), (10, 4))
        self.assertTrue(set(fb) <= set(tg))
        self.assertIn("(mirror mode)", self.out)

    def test_what_telegram_defers_never_reaches_facebook(self):
        self.run_main({**M, "telegram_per_run": 10, "max_alerts_per_day": 3})
        self.assertEqual(sorted(self.posted("telegram")), sorted(self.posted("facebook")))
        self.assertEqual(len(self.posted("facebook")), 3)

    def test_same_posts_not_repeated_in_the_next_run(self):
        self.run_main({**M, "telegram_per_run": 10})
        first = set(self.posted("telegram"))
        self.run_main({**M, "telegram_per_run": 10})
        second = set(self.posted("telegram"))
        self.assertFalse(first & second)
        self.assertEqual(second, set(self.posted("facebook")))

    def test_idle_when_telegram_is_closed_even_if_facebook_is_open(self):
        self.run_main({**M, "max_alerts_per_day": 3, "telegram_per_run": 10})
        calls = []

        def fetch(url, retries=4):
            calls.append(url)
            return ""

        self.run_main({**M, "max_alerts_per_day": 3}, fetch=fetch)
        self.assertEqual(calls, [])
        self.assertIn("[idle]", self.out)

    def test_default_mode_is_still_the_split(self):
        self.run_main()
        self.assertFalse(set(self.posted("telegram")) & set(self.posted("facebook")))


if __name__ == "__main__":
    unittest.main()
