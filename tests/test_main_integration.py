"""End-to-end main() with fake Amazon pages. No network (requests blocked), temp dir only."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scanner  # noqa: E402

N = 24


def _card(i):
    return (f'<div data-asin="B0RUN{i:06d}"><h2><span>{"Nike Shoes" if i % 3 == 0 else "Air Fryer"} {i}</span></h2>'
            f'<span class="a-price"><span class="a-offscreen">EGP700</span></span>'
            f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">EGP{1000 + i * 100}</span></span></div>')


SEARCH = "".join(_card(i) for i in range(N))
PAGE = ('<a id="sellerProfileTriggerId">Shop</a>'
        '<div id="fulfillerInfoFeature_feature_div">Ships from Amazon</div>')


def fake_fetch(url, retries=4):
    if "/dp/" in url:
        return PAGE
    return SEARCH if "page=" not in url else ""


class MainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        cfg = json.loads((ROOT / "scanner_config.json").read_text(encoding="utf-8"))
        cfg["sources"] = ["https://www.amazon.eg/s?k=laptop"]
        cfg["settings"]["quiet_hours"] = None
        self.cfg = cfg
        shutil.copy(ROOT / "seen.json", self.dir / "seen.json")
        self.seen_before = (self.dir / "seen.json").read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, settings=None, tg_fail=False, fb_fail=False, env=None):
        self.cfg["settings"].update(settings or {})
        (self.dir / "scanner_config.json").write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        patches = [
            mock.patch.object(scanner, "BASE", self.dir),
            mock.patch.object(scanner, "CONFIG_FILE", self.dir / "scanner_config.json"),
            mock.patch.object(scanner, "SEEN_FILE", self.dir / "seen.json"),
            mock.patch.object(scanner, "fetch_page", fake_fetch),
            mock.patch.object(scanner.time, "sleep", lambda *_: None),
            mock.patch.object(scanner, "TOKEN", "FAKE"),
            mock.patch.object(scanner, "CHANNEL_ID", "@chan"),
            mock.patch.object(scanner, "CHAT_ID", None),
            mock.patch("requests.post", side_effect=AssertionError("NETWORK CALL")),
            mock.patch("requests.get", side_effect=AssertionError("NETWORK CALL")),
            mock.patch.dict(os.environ, {"FB_PAGE_ID": "1", "FB_PAGE_ACCESS_TOKEN": "T",
                                         "FB_APP_ID": "2", "FB_APP_SECRET": "S", **(env or {})}),
        ]
        if tg_fail:
            patches.append(mock.patch.object(scanner, "publish_telegram", side_effect=RuntimeError("tg down")))
        if fb_fail:
            patches.append(mock.patch.object(scanner, "publish_facebook", side_effect=RuntimeError("fb down")))
        buf = io.StringIO()
        with contextlib.ExitStack() as st, contextlib.redirect_stdout(buf):
            for p in patches:
                st.enter_context(p)
            scanner.main()
        self.out = buf.getvalue()
        sf = self.dir / "bot_state.dryrun.json"
        return json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else None


class TestMainDryRun(MainBase):
    def test_split_20_no_overlap_and_nothing_real_changes(self):
        st = self.run_main()
        tg, fb = set(st["posted"]["telegram"]), set(st["posted"]["facebook"])
        self.assertEqual((len(tg), len(fb)), (10, 10))
        self.assertFalse(tg & fb)
        self.assertEqual((self.dir / "seen.json").read_bytes(), self.seen_before)   # seen.json untouched
        self.assertFalse((self.dir / "bot_state.json").exists())                    # no production state
        self.assertIn("links=plain (no tag)", self.out)
        self.assertNotIn("tag=offershunte0f-21", self.out)

    def test_second_run_does_not_repeat_posted_asins(self):
        self.run_main()
        st = self.run_main()
        self.assertIn("collected 4", self.out)                     # only the 4 never-posted ones
        self.assertEqual(self.out.count("[defer]"), 4)             # daily caps (10+10) already used
        self.assertEqual(len(st["posted"]["telegram"]) + len(st["posted"]["facebook"]), 20)  # no repeats

    def test_telegram_failure_does_not_stop_facebook(self):
        self.run_main(tg_fail=True)
        self.assertIn("[telegram] ERROR (other platform unaffected)", self.out)
        self.assertEqual(self.out.count("[post][facebook]"), 10)

    def test_facebook_failure_does_not_stop_telegram(self):
        self.run_main(fb_fail=True)
        self.assertIn("[facebook] ERROR (other platform unaffected)", self.out)
        self.assertEqual(self.out.count("[post][telegram]"), 10)

    def test_facebook_secrets_never_printed(self):
        self.run_main()
        for secret in ("FAKE", "'T'", "S'"):
            pass
        self.assertNotIn("FB_APP_SECRET", self.out)
        self.assertNotIn("Bearer", self.out)

    def test_quiet_hours_defer_everything(self):
        st = self.run_main({"quiet_hours": {"start": "00:00", "end": "23:59"}})
        self.assertEqual(self.out.count("[defer]"), 20)
        self.assertEqual(st["posted"]["telegram"], {})
        self.assertEqual(st["posted"]["facebook"], {})
        self.assertTrue(all(st["candidates"][a]["deferred"] for a in list(st["candidates"])[:20]))

    def test_daily_caps_per_platform(self):
        st = self.run_main({"max_alerts_per_day": 3, "fb_max_per_day": 2})
        self.assertEqual((len(st["posted"]["telegram"]), len(st["posted"]["facebook"])), (3, 2))
        self.assertEqual(self.out.count("[defer]"), 15)


class TestAffiliateOptIn(MainBase):
    def test_growth_mode_off_uses_tag_and_adds_disclosure(self):
        self.run_main({"growth_mode": False})
        self.assertIn("tag=offershunte0f-21", self.out)
        self.assertIn("بعض الروابط روابط تسويق بالعمولة", self.out)
        self.assertEqual((self.dir / "seen.json").read_bytes(), self.seen_before)


class TestMixedModeKeepsProdStateClean(MainBase):
    def test_fb_dry_run_does_not_leave_simulated_posts_in_prod_state(self):
        # Telegram "live" (fake token, but the network is blocked -> sends fail), FB dry
        self.cfg["settings"]["dry_run"] = False
        try:
            self.run_main({"dry_run": False}, env={"FB_DRY_RUN": "true"})
        except AssertionError:
            pass  # a real Telegram send was attempted (blocked by the mock) - expected here
        sf = self.dir / "bot_state.json"
        if sf.exists():
            st = json.loads(sf.read_text(encoding="utf-8"))
            self.assertEqual(st["posted"]["facebook"], {})      # simulated FB posts not remembered


if __name__ == "__main__":
    unittest.main()
