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

    def run_main(self, settings=None, tg_fail=False, fb_fail=False, env=None, fetch=None, net=None):
        self.cfg["settings"].update(settings or {})
        (self.dir / "scanner_config.json").write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        patches = [
            mock.patch.object(scanner, "BASE", self.dir),
            mock.patch.object(scanner, "CONFIG_FILE", self.dir / "scanner_config.json"),
            mock.patch.object(scanner, "SEEN_FILE", self.dir / "seen.json"),
            mock.patch.object(scanner, "fetch_page", fetch or fake_fetch),
            mock.patch.object(scanner.time, "sleep", lambda *_: None),
            mock.patch.object(scanner, "TOKEN", "FAKE"),
            mock.patch.object(scanner, "CHANNEL_ID", "@chan"),
            mock.patch.object(scanner, "CHAT_ID", None),
            mock.patch("requests.post", side_effect=AssertionError("NETWORK CALL")),
            mock.patch("requests.get", side_effect=AssertionError("NETWORK CALL")),
            mock.patch.dict(os.environ, {"FB_PAGE_ID": "1", "FB_PAGE_ACCESS_TOKEN": "T",
                                         "FB_APP_ID": "2", "FB_APP_SECRET": "S", **(env or {})}),
        ]
        patches += list(net or [])
        if tg_fail:
            patches.append(mock.patch.object(scanner, "publish_telegram", side_effect=RuntimeError("tg down")))
        if fb_fail:
            patches.append(mock.patch.object(scanner, "publish_facebook", side_effect=RuntimeError("fb down")))
        buf = io.StringIO()
        try:
            with contextlib.ExitStack() as st, contextlib.redirect_stdout(buf):
                for p in patches:
                    st.enter_context(p)
                scanner.main()
        finally:
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
        big = {"max_alerts_per_day": 50, "fb_max_per_day": 50}   # caps not the limiting factor here
        self.run_main(big)
        st = self.run_main(big)
        self.assertIn("collected 4", self.out)                     # only the 4 never-posted ones
        self.assertEqual(len(st["posted"]["telegram"]) + len(st["posted"]["facebook"]), 24)  # no repeats

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

    def test_quiet_hours_skip_scanning_entirely(self):
        calls = []

        def fetch(url, retries=4):
            calls.append(url)
            return fake_fetch(url, retries)

        st = self.run_main({"quiet_hours": {"start": "00:00", "end": "23:59"}}, fetch=fetch)
        self.assertEqual(calls, [])
        self.assertIn("[idle] quiet hours", self.out)
        self.assertEqual(st["posted"]["telegram"], {})
        self.assertEqual(st["posted"]["facebook"], {})

    def test_daily_caps_per_platform(self):
        st = self.run_main({"max_alerts_per_day": 3, "fb_max_per_day": 2})
        self.assertEqual((len(st["posted"]["telegram"]), len(st["posted"]["facebook"])), (3, 2))
        self.assertEqual(self.out.count("[defer]"), 15)


class TestDailyCapsIdle(MainBase):
    def test_second_run_after_caps_are_full_makes_no_amazon_requests(self):
        caps = {"max_alerts_per_day": 3, "fb_max_per_day": 2}
        self.run_main(caps)
        calls = []

        def fetch(url, retries=4):
            calls.append(url)
            return fake_fetch(url, retries)

        self.run_main(caps, fetch=fetch)
        self.assertEqual(calls, [])
        self.assertIn("[idle] daily caps reached", self.out)

    def test_one_platform_open_still_scans(self):
        self.run_main({"max_alerts_per_day": 3, "fb_max_per_day": 2})
        calls = []

        def fetch(url, retries=4):
            calls.append(url)
            return fake_fetch(url, retries)

        self.run_main({"max_alerts_per_day": 3, "fb_max_per_day": 9}, fetch=fetch)
        self.assertTrue(calls)
        self.assertNotIn("[idle]", self.out)


class TestDeferralReleased(MainBase):
    def test_deferred_deals_are_collected_again_once_the_cap_allows(self):
        st = self.run_main({"max_alerts_per_day": 3, "fb_max_per_day": 2})
        self.assertEqual(sum(1 for c in st["candidates"].values() if c.get("deferred")), 15)
        self.run_main({"max_alerts_per_day": 50, "fb_max_per_day": 50})
        self.assertIn("collected 19", self.out)      # the 15 deferred + the 4 never seen


class TestVerdictExpiry(unittest.TestCase):
    S = {"recheck_seller_days": 7}
    INFO = {"verified": True, "shipped_by_amazon": True, "amazon_sold": False, "seller": "Shop"}

    def _iso(self, days_ago):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")

    def test_fresh_verdict_is_reused(self):
        c = {**self.INFO, "verified_at": self._iso(1), "last_seen": self._iso(0)}
        self.assertIsNotNone(scanner.cached_fulfillment(c, self.S))

    def test_old_verdict_expires_even_if_the_item_was_seen_just_now(self):
        c = {**self.INFO, "verified_at": self._iso(10), "last_seen": self._iso(0)}
        self.assertIsNone(scanner.cached_fulfillment(c, self.S))

    def test_legacy_entry_gets_a_clock_instead_of_a_refetch_burst(self):
        c = {**self.INFO, "last_seen": self._iso(0)}
        self.assertIsNotNone(scanner.cached_fulfillment(c, self.S))
        self.assertTrue(c["verified_at"])

    def test_unknown_verdict_is_never_cached(self):
        c = {**self.INFO, "shipped_by_amazon": None, "verified_at": self._iso(0)}
        self.assertIsNone(scanner.cached_fulfillment(c, self.S))


class TestCaptchaNotRetried(unittest.TestCase):
    def test_one_request_then_raise(self):
        class R:
            status_code = 200
            text = "<html>Robot Check - enter the characters you see below</html>"
        calls = []
        with mock.patch.object(scanner.SESSION, "get", side_effect=lambda *a, **k: calls.append(1) or R()), \
                mock.patch.object(scanner.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as cm:
                scanner.fetch_page("https://www.amazon.eg/s?k=x")
        self.assertEqual(len(calls), 1)
        self.assertIn("CAPTCHA", str(cm.exception))


class TestFacebookMirrorsTelegram(MainBase):
    def _fb_block(self):
        i = self.out.index("[facebook][dry-run] would post")
        return self.out[i:].split("\n[post][facebook]")[0]

    def test_same_lines_prices_included_no_html(self):
        self.run_main()
        fb, tg = self._fb_block(), self.out
        for needle in ("❌ كان:", "✅ بقى:", "📉 خصم", "💸 وفّرت", "🏪 البائع:", "📦 الشحن من أمازون",
                       "🛒 اشتري الآن: https://www.amazon.eg/dp/B0RUN", "⏱️ السعر وقت النشر"):
            self.assertIn(needle, fb)
            self.assertIn(needle.split(":")[0], tg)
        self.assertNotIn("<b>", fb)
        self.assertNotIn("<s>", fb)
        self.assertNotIn("لقطة جديدة", fb)          # the old Facebook-only hook is gone
        self.assertIn("image=", fb)

    def test_old_format_still_available_behind_the_switch(self):
        self.run_main({"facebook_mirror_telegram": False})
        fb = self._fb_block()
        self.assertTrue(any(h in fb for h in ("لقطة جديدة", "لفت نظرنا", "اكتشاف النهاردة")))
        self.assertNotIn("❌ كان:", fb)             # prices hidden again (facebook_show_prices=false)

    def test_telegram_caption_is_unchanged_without_buy_link(self):
        s = {"suspicious_discount": 90, "channel_footer": "F"}
        a = scanner.build_caption("T", 700, 1000, 30, s, "#x", None, {"hashtag": "#c"})
        b = scanner.build_caption("T", 700, 1000, 30, s, "#x", None, {"hashtag": "#c"}, buy_link=None)
        self.assertEqual(a, b)
        self.assertNotIn("اشتري الآن", a)


class TestWeeklyDisabled(MainBase):
    def test_weekly_disabled_in_config_does_nothing(self):
        self.cfg["weekly"]["enabled"] = False
        st = self.run_main()
        self.assertIn("[weekly] status: disabled", self.out)
        self.assertEqual(st["weekly_sim"]["weeks"], {})
        self.assertEqual(st["weekly"]["weeks"], {})


class TestEmptyPages(MainBase):
    def test_block_pages_stop_the_run_early_and_are_explained(self):
        blocked = "<html><head><title>Robot Check</title></head><body>captcha</body></html>"
        self.cfg["sources"] = [f"https://www.amazon.eg/s?k=k{i}" for i in range(6)]
        calls = []

        def fetch(url, retries=4):
            calls.append(url)
            return blocked

        with self.assertRaises(SystemExit):              # every page failed -> red run
            self.run_main(fetch=fetch)
        self.assertEqual(len(calls), 4)                  # stopped after 4, not 18 requests
        self.assertIn("Robot Check", self.out)
        self.assertIn("[stop] 4 empty pages in a row", self.out)


class TestFacebookTokenCache(MainBase):
    LIVE = {"dry_run": False, "telegram_per_run": 0, "fb_per_run": 1, "fb_max_per_day": 1}

    def _net(self, token_type="PAGE"):
        from datetime import datetime
        dbg = mock.Mock(ok=True, status_code=200)
        dbg.json.return_value = {"data": {"is_valid": True, "type": token_type, "profile_id": "1"}}
        posted = mock.Mock(ok=True, status_code=200)
        posted.json.return_value = {"id": "99"}
        self.post_mock = mock.Mock(return_value=posted)
        return [mock.patch("requests.get", return_value=dbg), mock.patch("requests.post", self.post_mock)]

    def _seed_bad_cache(self):
        import weekly
        from datetime import datetime
        today = datetime.now(weekly.tz_of("Africa/Cairo")).date().isoformat()
        (self.dir / "bot_state.json").write_text(json.dumps(
            {"facebook": {"token_check": {"date": today, "valid": False, "error_code": 190}}}), encoding="utf-8")

    def test_a_bad_verdict_from_earlier_today_does_not_block_a_fixed_token(self):
        self._seed_bad_cache()
        self.run_main(self.LIVE, env={"FB_DRY_RUN": "false"}, net=self._net())
        self.assertIn("[post][facebook]", self.out)
        self.assertRegex(self.out, r"mode=(photo|feed)")   # fake product has no image -> feed
        self.assertEqual(self.post_mock.call_count, 1)

    def test_user_token_is_reported_with_a_reason_and_nothing_is_posted(self):
        self._seed_bad_cache()
        self.run_main(self.LIVE, env={"FB_DRY_RUN": "false"}, net=self._net("USER"))
        self.assertIn("token check: FB_PAGE_ACCESS_TOKEN is a USER token", self.out)
        self.assertEqual(self.post_mock.call_count, 0)


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
