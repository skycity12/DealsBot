"""Offline tests (no network, no Telegram, no Amazon). Run:  python -m unittest discover -s tests -v"""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import botstate  # noqa: E402
import categories as cats  # noqa: E402
import weekly  # noqa: E402

CAIRO = ZoneInfo("Africa/Cairo")
CONFIG = json.loads((ROOT / "scanner_config.json").read_text(encoding="utf-8"))
CATS = CONFIG["categories"]
LINK = lambda asin: f"https://www.amazon.eg/dp/{asin}?tag=offershunte0f-21"  # noqa: E731


def cai(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CAIRO)


class FakeClient:
    """Records calls; can fail chosen methods. Behaves like a live bot."""

    def __init__(self, fail=None, perms=None):
        self.calls, self.fail, self.n = [], dict(fail or {}), 1000
        self.perms = perms or {"status": "administrator", "can_post_messages": True,
                               "can_edit_messages": True}
        self.ws = None

    def bind(self, ws):
        self.ws = ws

    def call(self, method, payload):
        weekly._guard(method, payload)
        self.calls.append((method, copy.deepcopy(payload)))
        if self.fail.get(method, 0) > 0:
            self.fail[method] -= 1
            raise weekly.TelegramError(f"{method}: boom")
        self.n += 1
        if method == "getMe":
            return {"id": 7}
        if method == "getChatMember":
            return dict(self.perms)
        if method == "sendMessage":
            return {"message_id": self.n}
        if method == "sendPoll":
            return {"message_id": self.n, "poll": {"id": f"p{self.n}"}}
        if method == "stopPoll":
            return {"total_voter_count": 5, "is_closed": True,
                    "options": [{"text": "لبس", "voter_count": 3, "voters": ["secret"]},
                                {"text": "جزم", "voter_count": 2}]}
        return True

    def names(self):
        return [m for m, _ in self.calls]


def seeded_state(path, now, n=8, shipped=True):
    st = botstate.load(path)
    for i in range(n):
        c = weekly.new_candidate(f"B0TEST{i:04d}", f"Product number {i} حذاء", "جزم", now)
        c.update(verified=True, amazon_sold=(i % 2 == 0), shipped_by_amazon=shipped)
        c["days"] = [(now.date() - timedelta(days=d)).isoformat() for d in range(i % 5)] or [now.date().isoformat()]
        st["candidates"][c["asin"]] = c
    botstate.save(path, st)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sf = self.dir / "bot_state.json"
        self.cfg = copy.deepcopy(CONFIG)
        self.settings = {"timezone": "Africa/Cairo", "channel_id": "@chan"}
        # Thursday 2026-11-26 21:05 Cairo is inside the weekly window
        self.now = cai(2026, 11, 26, 21, 5)

    def tearDown(self):
        self.tmp.cleanup()

    def run_job(self, client, live=True, now=None):
        return weekly.run_weekly(self.cfg, self.settings, state_file=self.sf, live=live,
                                 client=client, link_fn=LINK, now=now or self.now,
                                 handle="@OffersHunter2026", log=lambda *_: None)


class TestCategories(unittest.TestCase):
    def test_config_valid(self):
        cats.validate(CATS)

    def test_examples(self):
        c = lambda t: cats.classify(t, CATS)["hashtag"]  # noqa: E731
        self.assertEqual(c("كفر حماية لموبايل سامسونج"), "#اكسسوارات_موبايل")   # accessory before phone
        self.assertEqual(c("Samsung Galaxy A55 5G Smartphone"), "#موبايلات")
        self.assertEqual(c("Tefal Air Fryer 4L"), "#أجهزة_منزلية")
        self.assertEqual(c("Nike Running Shoes men"), "#جزم")
        self.assertEqual(c("تيشيرت قطن رجالي"), "#لبس")
        self.assertEqual(c("Fantasy novel"), "#متنوع")                         # 'fan' must not match
        self.assertEqual(c(""), "#متنوع")

    def test_validate_rejects_bad(self):
        with self.assertRaises(ValueError):
            cats.validate([{"name": "x", "hashtag": "x"}])
        with self.assertRaises(ValueError):
            cats.validate([{"name": "x", "hashtag": "#x"}])   # no default


class TestTime(unittest.TestCase):
    def test_quiet_hours(self):
        q = {"start": "00:00", "end": "09:00"}
        self.assertTrue(in_q(cai(2026, 11, 22, 0, 0), q))
        self.assertTrue(in_q(cai(2026, 11, 22, 8, 59), q))
        self.assertFalse(in_q(cai(2026, 11, 22, 9, 0), q))
        self.assertFalse(in_q(cai(2026, 11, 22, 23, 59), q))
        wrap = {"start": "22:00", "end": "06:00"}
        self.assertTrue(in_q(cai(2026, 11, 22, 23, 0), wrap))
        self.assertTrue(in_q(cai(2026, 11, 22, 5, 0), wrap))
        self.assertFalse(in_q(cai(2026, 11, 22, 12, 0), wrap))
        self.assertFalse(in_q(cai(2026, 11, 22, 3, 0), None))

    def test_dst_handled_by_zoneinfo_not_fixed_utc(self):
        w = CONFIG["weekly"]
        winter = weekly.scheduled_time(cai(2027, 1, 14, 22, 0), w["weekday"], w["hour"], w["minute"])
        summer = weekly.scheduled_time(cai(2027, 7, 15, 22, 0), w["weekday"], w["hour"], w["minute"])
        self.assertEqual((winter.hour, summer.hour), (21, 21))                 # always 21:00 Cairo
        self.assertEqual(winter.astimezone(timezone.utc).hour, 19)              # UTC+2
        self.assertEqual(summer.astimezone(timezone.utc).hour, 18)              # UTC+3 (DST)

    def test_due_window_and_week_key(self):
        w = CONFIG["weekly"]
        self.assertFalse(weekly.check_due(cai(2026, 11, 26, 20, 59), w, True)[0])
        due, sched, key = weekly.check_due(cai(2026, 11, 26, 21, 0), w, True)
        self.assertTrue(due)
        self.assertEqual(sched.weekday(), 3)
        self.assertEqual(key, "2026-W48")
        self.assertTrue(weekly.check_due(cai(2026, 11, 27, 2, 0), w, True)[0])    # live catch-up
        self.assertFalse(weekly.check_due(cai(2026, 11, 27, 4, 0), w, True)[0])
        self.assertFalse(weekly.check_due(cai(2026, 11, 26, 21, 40), w, False)[0])  # dry window short
        self.assertTrue(weekly.check_due(cai(2026, 11, 26, 21, 10), w, False)[0])
        # after midnight the key is still the scheduled Thursday's week
        self.assertEqual(weekly.check_due(cai(2026, 11, 27, 1, 0), w, True)[2], "2026-W48")


def in_q(now, q):
    return weekly.in_quiet_hours(now, q)


class TestRankingAndDigest(Base):
    def test_ranking_deterministic_and_eligibility(self):
        seeded_state(self.sf, self.now, n=9)
        st = botstate.load(self.sf)
        st["candidates"]["B0TEST0003"]["shipped_by_amazon"] = False        # excluded
        st["candidates"]["B0TEST0004"]["last_seen"] = weekly._iso(self.now - timedelta(days=9))  # too old
        w = self.cfg["weekly"]
        a = weekly.select_items(st["candidates"], self.now, w)
        b = weekly.select_items(copy.deepcopy(st["candidates"]), self.now, w)
        self.assertEqual([c["asin"] for c in a], [c["asin"] for c in b])
        self.assertLessEqual(len(a), 7)
        ids = {c["asin"] for c in a}
        self.assertNotIn("B0TEST0003", ids)
        self.assertNotIn("B0TEST0004", ids)
        self.assertTrue(a[0]["amazon_sold"])   # Amazon-sold + more days ranks first

    def test_digest_content_rules(self):
        seeded_state(self.sf, self.now, n=8)
        st = botstate.load(self.sf)
        items = weekly.select_items(st["candidates"], self.now, self.cfg["weekly"])
        text = weekly.build_digest(items, LINK, self.cfg["weekly"], CATS, "@OffersHunter2026")
        for bad in ("جنيه", "EGP", "%", "خصم", "كان:", "بقى", "مخزون", "خطأ", "السعر", "price", "discount"):
            self.assertNotIn(bad, text)
        self.assertEqual(text.count("tag=offershunte0f-21"), len(items))
        self.assertIn("#جزم", text)
        self.assertIn("1. <b>", text)
        self.assertTrue(text.rstrip().endswith(self.cfg["weekly"]["disclosure"]))   # disclosure last
        self.assertLessEqual(len(text), 4096)


class TestWeeklyDryRun(Base):
    def test_dry_run_never_touches_network_or_prod_state(self):
        dry = botstate.state_path(self.dir, False)
        seeded_state(dry, self.now)
        self.sf = dry
        with mock.patch("requests.post", side_effect=AssertionError("network used")):
            r = self.run_job(weekly.SimClient(None, lambda *_: None), live=False)
        self.assertEqual(r, "complete")
        self.assertFalse((self.dir / "bot_state.json").exists())
        st = botstate.load(dry)
        self.assertEqual(st["weekly_sim"]["weeks"]["2026-W48"]["status"], "complete")
        self.assertEqual(st["weekly"]["weeks"], {})              # real section untouched

    def test_gate_logic_in_scanner_expression(self):
        # same expression scanner.main uses: weekly_live needs EVERY condition
        gate = lambda dry, wl, tok, ch: (not dry) and bool(wl) and bool(tok) and bool(ch)  # noqa: E731
        self.assertFalse(gate(True, True, "t", "c"))     # global dry-run wins
        self.assertFalse(gate(False, False, "t", "c"))   # weekly_live=false
        self.assertFalse(gate(False, True, None, "c"))
        self.assertTrue(gate(False, True, "t", "c"))
        self.assertIs(CONFIG["settings"]["dry_run"], True)
        self.assertIs(CONFIG["settings"]["weekly_live"], False)


class TestWeeklyLive(Base):
    def setUp(self):
        super().setUp()
        seeded_state(self.sf, self.now)

    def test_full_run_order_and_payloads(self):
        st = botstate.load(self.sf)
        st["weekly"]["current_digest"] = {"message_id": 111, "week": "2026-W47"}
        st["weekly"]["current_poll"] = {"message_id": 222, "poll_id": "old", "week": "2026-W47",
                                        "options": ["لبس", "جزم"]}
        botstate.save(self.sf, st)
        c = FakeClient()
        self.assertEqual(self.run_job(c), "complete")
        self.assertEqual(c.names(), ["getMe", "getChatMember", "sendMessage", "pinChatMessage",
                                     "unpinChatMessage", "stopPoll", "sendPoll"])
        d = dict(c.calls)
        self.assertEqual(d["unpinChatMessage"], {"chat_id": "@chan", "message_id": 111})
        self.assertEqual(d["stopPoll"], {"chat_id": "@chan", "message_id": 222})
        p = d["sendPoll"]
        self.assertIs(p["is_anonymous"], True)
        self.assertEqual(p["type"], "regular")
        self.assertIs(p["allows_multiple_answers"], True)
        self.assertEqual([o["text"] for o in p["options"]], [c_["name"] for c_ in CATS])
        self.assertNotIn("allow_paid_broadcast", json.dumps(c.calls))
        self.assertNotIn("unpinAllChatMessages", c.names())
        st = botstate.load(self.sf)
        self.assertEqual(st["weekly"]["current_digest"]["message_id"], dict(c.calls)["pinChatMessage"]["message_id"])
        res = st["weekly"]["poll_results"][0]
        self.assertEqual(res["total_voter_count"], 5)
        self.assertNotIn("voters", json.dumps(res))          # no personal data kept
        self.assertEqual(st["weekly"]["current_poll"]["poll_id"][0], "p")

    def test_same_week_not_sent_twice(self):
        c = FakeClient()
        self.assertEqual(self.run_job(c), "complete")
        n = len(c.calls)
        self.assertEqual(self.run_job(c, now=self.now + timedelta(minutes=30)), "already_complete")
        self.assertEqual(len(c.calls), n)

    def test_partial_failure_pin_then_recovery_without_resending(self):
        c = FakeClient(fail={"pinChatMessage": 1})
        self.assertEqual(self.run_job(c), "step_failed:pin_digest")
        self.assertEqual(c.names().count("sendMessage"), 1)
        self.assertNotIn("unpinChatMessage", c.names())       # never unpin before pin succeeded
        self.assertEqual(self.run_job(c, now=self.now + timedelta(minutes=30)), "complete")
        self.assertEqual(c.names().count("sendMessage"), 1)   # digest NOT duplicated
        self.assertEqual(c.names().count("sendPoll"), 1)

    def test_pin_gives_up_old_digest_stays_and_poll_still_sent(self):
        st = botstate.load(self.sf)
        st["weekly"]["current_digest"] = {"message_id": 111, "week": "2026-W47"}
        botstate.save(self.sf, st)
        c = FakeClient(fail={"pinChatMessage": 99})
        t = self.now
        outcomes = []
        for i in range(3):
            outcomes.append(self.run_job(c, now=t + timedelta(minutes=30 * i)))
        self.assertEqual(outcomes[-1], "complete")
        self.assertNotIn("unpinChatMessage", c.names())       # old pin untouched
        self.assertEqual(c.names().count("sendMessage"), 1)

    def test_missing_permissions_abort_before_any_send(self):
        c = FakeClient(perms={"status": "administrator", "can_post_messages": True,
                              "can_edit_messages": False})
        for i in range(3):
            self.run_job(c, now=self.now + timedelta(minutes=30 * i))
        self.assertNotIn("sendMessage", c.names())
        self.assertNotIn("sendPoll", c.names())
        st = botstate.load(self.sf)
        self.assertEqual(st["weekly"]["weeks"]["2026-W48"]["status"], "needs_attention")

    def test_interrupted_send_is_not_retried_blindly(self):
        st = botstate.load(self.sf)
        wk = weekly._new_week("2026-W48", weekly.scheduled_time(self.now, 3, 21, 0))
        wk["steps"]["check_permissions"]["status"] = "done"
        wk["steps"]["send_digest"]["status"] = "in_progress"   # process died mid-send
        wk["digest_text"] = "x"
        st["weekly"]["weeks"]["2026-W48"] = wk
        botstate.save(self.sf, st)
        c = FakeClient()
        self.assertEqual(self.run_job(c), "needs_manual_check")
        self.assertEqual(c.calls, [])

    def test_protected_messages_never_unpinned(self):
        self.cfg["weekly"]["protected_message_ids"] = {"intro": 111, "index": 112}
        st = botstate.load(self.sf)
        st["weekly"]["current_digest"] = {"message_id": 111, "week": "x"}
        botstate.save(self.sf, st)
        c = FakeClient()
        self.assertEqual(self.run_job(c), "complete")
        self.assertNotIn("unpinChatMessage", c.names())

    def test_insufficient_items_posts_nothing(self):
        self.sf.unlink()
        seeded_state(self.sf, self.now, n=3)
        c = FakeClient()
        self.assertEqual(self.run_job(c), "skipped_insufficient_items")
        self.assertEqual(c.calls, [])

    def test_lock_busy_skips(self):
        with botstate.locked(self.sf):
            c = FakeClient()
            self.assertEqual(self.run_job(c), "locked")
            self.assertEqual(c.calls, [])

    def test_stale_lock_is_recovered(self):
        lock = Path(str(self.sf) + ".lock")
        lock.write_text("dead")
        old = datetime.now().timestamp() - 3 * 3600
        import os
        os.utime(lock, (old, old))
        with botstate.locked(self.sf):
            pass
        self.assertFalse(lock.exists())


class TestClientGuards(unittest.TestCase):
    def test_forbidden_methods_and_params(self):
        for bad in ("unpinAllChatMessages", "deleteMessage", "setMessageReaction"):
            with self.assertRaises(weekly.TelegramError):
                weekly._guard(bad, {"chat_id": 1})
        with self.assertRaises(weekly.TelegramError):
            weekly._guard("sendMessage", {"chat_id": 1, "allow_paid_broadcast": True})
        with self.assertRaises(weekly.TelegramError):
            weekly._guard("unpinChatMessage", {"chat_id": 1})          # no message_id

    def test_poll_validation(self):
        weekly.validate_poll("ok?", ["a", "b"])
        with self.assertRaises(ValueError):
            weekly.validate_poll("q", ["only one"])
        with self.assertRaises(ValueError):
            weekly.validate_poll("q" * 301, ["a", "b"])

    def test_poll_options_follow_categories(self):
        self.assertEqual(weekly.poll_options(CONFIG),
                         ["اكسسوارات موبايل", "موبايلات", "أجهزة منزلية", "جزم", "لبس", "متنوع"])


class TestCandidateMemory(Base):
    def test_no_price_fields_and_merge(self):
        c = weekly.touch({}, "B0AAAAAAAA", "Shoes", "جزم", self.now)
        self.assertFalse({"price", "list_price", "discount", "stock"} & set(c))
        disk = {"B0AAAAAAAA": copy.deepcopy(c)}
        mem = {"B0AAAAAAAA": copy.deepcopy(c)}
        mem["B0AAAAAAAA"]["days"].append("2026-11-27")
        mem["B0AAAAAAAA"]["deferred"] = True
        weekly.merge_candidates(disk, mem)
        self.assertEqual(len(disk["B0AAAAAAAA"]["days"]), 2)
        self.assertTrue(disk["B0AAAAAAAA"]["deferred"])

    def test_flush_daily_counter_resets_by_date(self):
        weekly.flush_scan_state(self.sf, {}, "2026-11-22", 4, self.now)
        weekly.flush_scan_state(self.sf, {}, "2026-11-22", 3, self.now)
        self.assertEqual(weekly.daily_count(botstate.load(self.sf), "2026-11-22"), 7)
        self.assertEqual(weekly.daily_count(botstate.load(self.sf), "2026-11-23"), 0)


class TestCaption(unittest.TestCase):
    def test_category_hashtag_is_last_line(self):
        import scanner
        cat = cats.classify("Nike shoes", CATS)
        cap = scanner.build_caption("Nike shoes", 500, 1000, 50, CONFIG["settings"], "#x", None, cat)
        self.assertEqual(cap.strip().splitlines()[-1], "#جزم")


if __name__ == "__main__":
    unittest.main()
