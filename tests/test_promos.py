import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import promos as P  # noqa: E402
from test_main_integration import MainBase, PAGE  # noqa: E402


class TestFindPromo(unittest.TestCase):
    def check(self, text, kind, eff):
        r = P.find_promo(text)
        self.assertIsNotNone(r, text)
        self.assertEqual((r["kind"], round(r["effective"])), (kind, eff), text)

    def test_buy_get_free(self):
        self.check("Buy 1, get 1 free", "free", 50)
        self.check("buy one get one free", "free", 50)
        self.check("Buy 2 get 1 free", "free", 33)
        self.check("اشتري 1 واحصل على 1 مجانًا", "free", 50)
        self.check("اشتري ٢ و احصل على ١ مجانا", "free", 33)
        self.check("اشتري واحد وخد واحد مجانا", "free", 50)

    def test_percent_off_the_next_one(self):
        self.check("Buy 1 get 50% off the second item", "pct_next", 25)
        self.check("Buy 1 get 50% off on 2", "pct_next", 25)
        self.check("اشتري 1 واحصل على خصم 50% على القطعة الثانية", "pct_next", 25)

    def test_quantity_discount_applies_to_all(self):
        self.check("Save 10% on 2 select item(s)", "pct_all", 10)
        self.check("Buy 2, get 10% off", "pct_all", 10)
        self.check("وفر 10% عند شراء 2", "pct_all", 10)

    def test_not_promotions(self):
        for t in ("Save 5% with coupon", "Buy now, get free delivery", "1,299 EGP", "", None,
                  "buy 9 get 9 free"):
            self.assertIsNone(P.find_promo(t), t)


def card(asin, price, promo_text):
    return (f'<div data-asin="{asin}"><h2><span>Air Fryer {asin}</span></h2>'
            f'<span class="a-price"><span class="a-offscreen">EGP{price}</span></span>'
            f'<span class="promo">{promo_text}</span></div>')


SEARCH = "".join([
    card("B0PROMO001", 700, "Buy 1, get 1 free"),
    card("B0PROMO002", 500, "اشتري 2 واحصل على 1 مجانًا"),
    card("B0PROMO003", 900, "Buy 1 get 50% off on 2"),
    card("B0PROMO004", 300, "Save 5% with coupon"),
])


def fetch(url, retries=4):
    if "/dp/" in url:
        return PAGE
    return SEARCH if "page=" not in url else ""


class TestPromoModes(MainBase):
    def run_promo(self, **settings):
        return self.run_main(settings, fetch=fetch)

    def test_default_is_log_only_nothing_is_published(self):
        st = self.run_promo()
        self.assertEqual(self.out.count("[promo] B0PROMO"), 3)
        self.assertIn("mode=log", self.out)
        self.assertIn("collected 0", self.out)
        self.assertEqual(st["posted"]["telegram"], {})
        self.assertEqual(st["posted"]["facebook"], {})

    def test_off_does_not_even_log(self):
        self.run_promo(promo_deals="off")
        self.assertNotIn("[promo]", self.out)
        self.assertIn("collected 0", self.out)

    def test_on_publishes_the_bundle_deals_without_fake_prices(self):
        st = self.run_promo(promo_deals="on")
        self.assertIn("collected 3", self.out)
        self.assertIn("🎁 عرض باقة", self.out)
        self.assertIn("🎁 العرض: <b>اشتري 1 وخد 1 مجانًا</b>", self.out)
        self.assertIn("🎁 العرض: اشتري 2 وخد 1 مجانًا", self.out)          # Facebook copy, plain text
        self.assertNotIn("السعر القديم", self.out)                         # nothing to warn about
        self.assertNotIn("❌ كان:", self.out)                               # no list price exists
        self.assertNotIn("B0PROMO004", self.out.split("[split]")[1])      # a coupon is not a bundle
        self.assertEqual(len(st["posted"]["telegram"]) + len(st["posted"]["facebook"]), 3)

    def test_min_effective_discount_filters(self):
        self.run_promo(promo_deals="on", promo_min_effective_discount=30)
        self.assertIn("collected 2", self.out)          # buy1get50%on2 (25%) is below 30


if __name__ == "__main__":
    unittest.main()


class TestSkipEvidence(unittest.TestCase):
    def test_skip_reason_shows_what_the_buybox_said(self):
        import scanner
        third = ('<div id="merchant-info">Ships from Fast Shop Sold by Fast Shop</div>'
                 '<a id="sellerProfileTriggerId">Fast Shop</a>' + "<p>" + "x" * 2100 + "</p>")
        info = scanner.fulfillment_info(third)
        self.assertIs(info["shipped_by_amazon"], False)
        self.assertIn("Ships from Fast Shop", info["evidence"])
        fba = ('<div id="merchant-info">Ships from Amazon Sold by Fast Shop</div>'
               '<a id="sellerProfileTriggerId">Fast Shop</a>' + "<p>" + "x" * 2100 + "</p>")
        info = scanner.fulfillment_info(fba)
        self.assertIs(info["shipped_by_amazon"], True)          # third-party seller, shipped by Amazon: allowed
        self.assertIs(info["amazon_sold"], False)
        self.assertEqual(info["seller"], "Fast Shop")


class TestDisclosureOptOut(unittest.TestCase):
    def test_blank_config_removes_line_missing_key_keeps_default(self):
        import scanner
        self.assertEqual(scanner.disclosure_text({"affiliate_disclosure": ""}), "")
        self.assertEqual(scanner.disclosure_text({}), scanner.DEFAULT_DISCLOSURE)
        self.assertEqual(scanner.disclosure_text({"affiliate_disclosure": "x"}), "x")
