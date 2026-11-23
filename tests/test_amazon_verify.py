import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import amazon_verify as av  # noqa: E402

PAD = "<p>" + "x" * 2100 + "</p>"


class TestVerify(unittest.TestCase):
    def test_amazon(self):
        self.assertEqual(av.classify_product_page("<div>Ships from Amazon</div>" + PAD).verdict, "amazon")
        self.assertEqual(av.classify_product_page("<div>الشحن من: أمازون</div>" + PAD).verdict, "amazon")

    def test_third_party(self):
        h = '<a id="sellerProfileTriggerId">Shop</a><div>Ships from Shop</div>' + PAD
        r = av.classify_product_page(h)
        self.assertEqual((r.verdict, r.seller), ("third_party", "Shop"))

    def test_blocked_and_small_and_unknown(self):
        self.assertTrue(av.classify_product_page("<html>Robot Check</html>" + PAD).blocked)
        self.assertEqual(av.classify_product_page("<html></html>").verdict, "unknown")
        self.assertEqual(av.classify_product_page("<p>nothing useful</p>" + PAD).verdict, "unknown")
        self.assertFalse(av.looks_blocked(""))


if __name__ == "__main__":
    unittest.main()
