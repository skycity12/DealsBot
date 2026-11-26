"""Facebook / split / growth-mode tests. Offline: requests is always mocked."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import facebook_formatter as fbf  # noqa: E402
import facebook_publisher as fbp  # noqa: E402
import splitter  # noqa: E402

CONFIG = json.loads((ROOT / "scanner_config.json").read_text(encoding="utf-8"))
S = CONFIG["settings"]
CREDS = {"page_id": "123", "page_token": "PAGE-SECRET-TOKEN", "app_id": "42", "app_secret": "APP-SECRET"}


def deal(i, disc=50):
    return {"asin": f"B0SPLIT{i:04d}", "title": f"Product {i}", "discount": disc,
            "link": f"https://www.amazon.eg/dp/B0SPLIT{i:04d}", "image": "https://img/x.jpg",
            "cat": {"name": "جزم", "hashtag": "#جزم"}, "info": {"amazon_sold": True},
            "price": 100.0, "list_price": 200.0}


class FakeResp:
    def __init__(self, ok=True, body=None, status=200):
        self.ok, self._b, self.status_code = ok, body or {}, status

    def json(self):
        return self._b


class TestSplit(unittest.TestCase):
    def test_20_deals_10_10_no_overlap_alternating(self):
        deals = [deal(i, disc=90 - i) for i in range(20)]
        tg, fb = splitter.split_deals(deals, 10, 10)
        self.assertEqual((len(tg), len(fb)), (10, 10))
        self.assertFalse({d["asin"] for d in tg} & {d["asin"] for d in fb})
        self.assertEqual(tg[0]["asin"], "B0SPLIT0000")     # strongest -> Telegram
        self.assertEqual(fb[0]["asin"], "B0SPLIT0001")     # next -> Facebook
        self.assertEqual(sum(d["discount"] for d in tg) - sum(d["discount"] for d in fb), 10)   # alternating keeps quality close

    def test_fewer_than_20_splits_evenly_and_logs(self):
        logs = []
        tg, fb = splitter.split_deals([deal(i) for i in range(7)], 10, 10, log=logs.append)
        self.assertEqual((len(tg), len(fb)), (4, 3))
        self.assertTrue(any("only 7" in l for l in logs))

    def test_more_than_20_uses_20_and_duplicates_dropped(self):
        deals = [deal(i) for i in range(25)] + [deal(0)]
        tg, fb = splitter.split_deals(deals, 10, 10)
        self.assertEqual(len(tg) + len(fb), 20)
        asins = [d["asin"] for d in tg + fb]
        self.assertEqual(len(asins), len(set(asins)))

    def test_uneven_quota_is_respected(self):
        tg, fb = splitter.split_deals([deal(i) for i in range(20)], 12, 8)
        self.assertEqual((len(tg), len(fb)), (12, 8))


class TestFormatter(unittest.TestCase):
    def test_no_prices_by_default_and_first_two_lines_are_the_hook(self):
        txt = fbf.format_facebook_post(deal(1), S, affiliate_on=False, disclosure="D")
        lines = txt.splitlines()
        self.assertTrue(lines[0].startswith(("🔥", "👀", "🛍")))
        self.assertEqual(lines[1], "Product 1")
        for bad in ("جنيه", "خصم", "السعر", "%", "وفّرت", "100", "200"):
            self.assertNotIn(bad, txt)
        self.assertIn("#جزم #عروض_امازون #خصومات", txt)
        self.assertNotIn("D", txt.splitlines()[-1])          # no disclosure without the tag

    def test_disclosure_only_when_affiliate_links(self):
        on = fbf.format_facebook_post(deal(1), S, True, "DISCLOSURE LINE")
        self.assertTrue(on.endswith("DISCLOSURE LINE"))
        off = fbf.format_facebook_post(deal(1), S, False, "DISCLOSURE LINE")
        self.assertNotIn("DISCLOSURE LINE", off)

    def test_prices_only_when_explicitly_enabled(self):
        txt = fbf.format_facebook_post(deal(1), {**S, "facebook_show_prices": True}, False, "")
        self.assertIn("100 جنيه", txt)

    def test_third_party_seller_is_named(self):
        d = deal(1)
        d["info"] = {"amazon_sold": False, "seller": "TechStore"}
        self.assertIn("TechStore (مش أمازون)", fbf.format_facebook_post(d, S, False, ""))


class TestPublisher(unittest.TestCase):
    def test_dry_run_default_and_env(self):
        self.assertTrue(fbp.fb_dry_run({}))
        self.assertTrue(fbp.fb_dry_run({"FB_DRY_RUN": "true"}))
        self.assertTrue(fbp.fb_dry_run({"FB_DRY_RUN": "garbage"}))
        self.assertFalse(fbp.fb_dry_run({"FB_DRY_RUN": "false"}))

    def test_dry_run_makes_no_request(self):
        with mock.patch("requests.post", side_effect=AssertionError), \
             mock.patch("requests.get", side_effect=AssertionError):
            self.assertEqual(fbp.post_deal_to_facebook(deal(1), "txt", S, CREDS, dry_run=True, log=lambda *_: None), "dry-run")
            self.assertTrue(fbp.validate_facebook_token(CREDS, S, True, lambda *_: None)["valid"])

    def test_photo_post_uses_pinned_version_header_and_no_token_in_url(self):
        with mock.patch("requests.post", return_value=FakeResp(True, {"id": "1"})) as p:
            m = fbp.post_deal_to_facebook(deal(1), "txt", S, CREDS, dry_run=False, log=lambda *_: None)
        self.assertEqual(m, "photo")
        url = p.call_args[0][0]
        self.assertEqual(url, "https://graph.facebook.com/v27.0/123/photos")
        self.assertNotIn("PAGE-SECRET-TOKEN", url)
        self.assertEqual(p.call_args[1]["headers"]["Authorization"], "Bearer PAGE-SECRET-TOKEN")
        self.assertEqual(p.call_args[1]["data"]["url"], "https://img/x.jpg")

    def test_no_image_goes_to_feed(self):
        d = deal(1); d["image"] = None
        with mock.patch("requests.post", return_value=FakeResp(True, {"id": "1"})) as p:
            self.assertEqual(fbp.post_deal_to_facebook(d, "t", S, CREDS, dry_run=False, log=lambda *_: None), "feed")
        self.assertTrue(p.call_args[0][0].endswith("/123/feed"))
        self.assertEqual(p.call_args[1]["data"]["link"], d["link"])

    def test_clear_api_error_falls_back_to_one_feed_post(self):
        seq = [FakeResp(False, {"error": {"code": 100, "message": "bad image"}}, 400),
               FakeResp(True, {"id": "2"})]
        with mock.patch("requests.post", side_effect=seq) as p:
            self.assertEqual(fbp.post_deal_to_facebook(deal(1), "t", S, CREDS, dry_run=False, log=lambda *_: None), "feed")
        self.assertEqual(p.call_count, 2)

    def test_timeout_is_never_retried(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.Timeout("slow")) as p:
            self.assertEqual(fbp.post_deal_to_facebook(deal(1), "t", S, CREDS, dry_run=False, log=lambda *_: None), "failed")
        self.assertEqual(p.call_count, 1)                    # one attempt only: no double post

    def test_code_190_reports_token_error(self):
        r = FakeResp(False, {"error": {"code": 190, "message": "expired"}}, 400)
        with mock.patch("requests.post", return_value=r):
            self.assertEqual(fbp.post_deal_to_facebook(deal(1), "t", S, CREDS, dry_run=False, log=lambda *_: None), "token_error")

    def test_validate_token_cases(self):
        ok = FakeResp(True, {"data": {"is_valid": True, "scopes": ["pages_manage_posts", "pages_read_engagement", "pages_show_list"]}})
        with mock.patch("requests.get", return_value=ok) as g:
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertTrue(r["valid"])
        self.assertEqual(r["missing_scopes"], [])
        self.assertNotIn("SECRET", g.call_args[0][0])        # secrets only in the header
        bad = FakeResp(True, {"data": {"is_valid": False, "error": {"code": 190, "message": "Session has expired"}}})
        with mock.patch("requests.get", return_value=bad):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertEqual((r["valid"], r["error_code"]), (False, 190))
        self.assertIn("expired", r["reason"])                  # the reason is reported
        import requests
        with mock.patch("requests.get", side_effect=requests.ConnectionError("net")):
            self.assertTrue(fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)["valid"])  # blip != invalid

    def test_unusable_check_is_not_treated_as_invalid_token(self):
        # a 400 that is NOT code 190 (bad request shape / app credentials) must not stop Facebook
        resp = FakeResp(False, {"error": {"code": 100, "message": "Invalid parameter PAGE-SECRET-TOKEN"}}, 400)
        with mock.patch("requests.get", return_value=resp):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertTrue(r["valid"])
        self.assertIn("could not be used", r["reason"])
        self.assertNotIn("PAGE-SECRET-TOKEN", r["reason"])
        resp = FakeResp(False, {"error": {"code": 190, "message": "Invalid OAuth access token"}}, 400)
        with mock.patch("requests.get", return_value=resp):
            self.assertFalse(fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)["valid"])

    def test_user_token_and_wrong_page_are_reported_clearly(self):
        user = FakeResp(True, {"data": {"is_valid": True, "type": "USER", "scopes": []}})
        with mock.patch("requests.get", return_value=user):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertFalse(r["valid"])
        self.assertIn("USER token", r["reason"])
        other = FakeResp(True, {"data": {"is_valid": True, "type": "PAGE", "profile_id": "999"}})
        with mock.patch("requests.get", return_value=other):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertFalse(r["valid"])
        self.assertIn("not FB_PAGE_ID", r["reason"])
        good = FakeResp(True, {"data": {"is_valid": True, "type": "PAGE", "profile_id": "123"}})
        with mock.patch("requests.get", return_value=good):
            self.assertTrue(fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)["valid"])

    def test_scrub_removes_secrets(self):
        out = fbp.scrub("failed for PAGE-SECRET-TOKEN and 42|APP-SECRET", CREDS)
        self.assertNotIn("PAGE-SECRET-TOKEN", out)
        self.assertNotIn("APP-SECRET", out)


class TestConfigDefaults(unittest.TestCase):
    def test_locks_and_numbers(self):
        self.assertIs(S["dry_run"], True)
        self.assertIs(S["growth_mode"], True)
        self.assertEqual((S["telegram_per_run"], S["fb_per_run"]), (10, 10))
        self.assertEqual(S["fb_graph_version"], "v27.0")
        self.assertEqual(S["affiliate_tag"], "offershunte0f-21")     # tag itself untouched
        self.assertIs(S["facebook_show_prices"], False)


class TestGrowthModeLinks(unittest.TestCase):
    def test_plain_links_until_growth_mode_off(self):
        import scanner
        self.assertNotIn("tag=", scanner.product_link("B0X", S))
        self.assertIn("tag=offershunte0f-21", scanner.product_link("B0X", {**S, "growth_mode": False}))
        self.assertFalse(scanner.affiliate_on(S))
        self.assertTrue(scanner.affiliate_on({**S, "growth_mode": False}))

    def test_weekly_digest_disclosure_follows_the_tag(self):
        import weekly
        w = CONFIG["weekly"]
        items = [{"asin": "B0X", "name": "n", "category": "جزم"}]
        link = lambda a: f"https://www.amazon.eg/dp/{a}"  # noqa: E731
        no = weekly.build_digest(items, link, w, CONFIG["categories"], "@h", disclosure=False)
        self.assertNotIn("Amazon Associate", no)
        yes = weekly.build_digest(items, link, w, CONFIG["categories"], "@h", disclosure=True)
        self.assertTrue(yes.rstrip().endswith(w["disclosure"]))


if __name__ == "__main__":
    unittest.main()


class TestMirrorAndCard(unittest.TestCase):
    def test_html_to_plain(self):
        h = "🔥 <b>خصم قوي</b>\n❌ كان: <s>1,000 جنيه</s>\n🛍 <b>A &amp; B</b>"
        self.assertEqual(fbf.telegram_html_to_plain(h), "🔥 خصم قوي\n❌ كان: 1,000 جنيه\n🛍 A & B")

    def test_disclosure_only_when_links_carry_the_tag(self):
        self.assertNotIn("DISC", fbf.mirror_telegram_post("x", False, "DISC"))
        self.assertTrue(fbf.mirror_telegram_post("x", True, "DISC").endswith("DISC"))

    def test_card_bytes_are_uploaded_as_the_photo(self):
        with mock.patch("requests.post", return_value=FakeResp(True, {"id": "1"})) as m:
            mode = fbp.post_deal_to_facebook(deal(1), "txt", S, CREDS, dry_run=False,
                                             log=lambda *_: None, image_bytes=b"JPEGDATA")
        self.assertEqual(mode, "photo")
        kw = m.call_args.kwargs
        self.assertTrue(m.call_args.args[0].endswith("/photos"))
        self.assertEqual(kw["files"]["source"], ("deal.jpg", b"JPEGDATA", "image/jpeg"))


class TestSplitUnequalQuotas(unittest.TestCase):
    def _split(self, n, nt, nf):
        deals = [{"asin": f"A{i}", "discount": 50 - i} for i in range(n)]
        tg, fb = splitter.split_deals(deals, nt, nf, log=lambda *_: None)
        self.assertFalse({d["asin"] for d in tg} & {d["asin"] for d in fb})   # never on both
        return len(tg), len(fb)

    def test_equal_quotas_keep_the_even_split(self):
        self.assertEqual([self._split(n, 10, 10) for n in (1, 2, 3, 4, 11)],
                         [(1, 0), (1, 1), (2, 1), (2, 2), (6, 5)])

    def test_ten_to_one_keeps_telegram_fed(self):
        self.assertEqual([self._split(n, 10, 1) for n in (1, 2, 3, 4, 6, 10, 11, 15)],
                         [(1, 0), (1, 1), (2, 1), (3, 1), (5, 1), (9, 1), (10, 1), (10, 1)])


class TestAppCredentialSignatureError(unittest.TestCase):
    def test_signature_error_means_wrong_app_secret_not_a_bad_page_token(self):
        resp = FakeResp(False, {"error": {"message": "Invalid OAuth access token signature.",
                                          "type": "OAuthException", "code": 190}}, status=400)
        with mock.patch("requests.get", return_value=resp):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertTrue(r["valid"])                       # posting is not stopped for it
        self.assertEqual(r["error_code"], 190)
        self.assertIn("FB_APP_SECRET", r["reason"])

    def test_a_real_expired_page_token_is_still_invalid(self):
        resp = FakeResp(False, {"error": {"message": "Error validating access token: Session has expired",
                                          "code": 190}}, status=400)
        with mock.patch("requests.get", return_value=resp):
            r = fbp.validate_facebook_token(CREDS, S, False, lambda *_: None)
        self.assertFalse(r["valid"])
