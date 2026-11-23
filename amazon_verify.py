"""Robust "Ships from Amazon" detection for amazon.eg product pages.

Why this module exists
----------------------
On Nov 23 the scanner went silent: 2095 items scanned, ~every ASIN skipped as
"not shipped by Amazon (or unknown)". Product pages came back HTTP 200 (only
one page outright failed), so the fetches "worked" - but the old structured
parser found none of its buybox containers. Three ways that happens, all
invisible to the old code:

  1. Amazon serves a "Robot Check" page with HTTP 200 (the old captcha test
     required two very specific strings, other variants slipped through);
  2. Amazon renamed/moved the buybox containers the parser selects on;
  3. The Arabic/English template differs from the one the parser knew.

Detection here is phrase-based over the whole page text (Arabic + English),
with an explicit "blocked" verdict for robot-check pages, so a container
rename can never again turn every page into a silent "unknown".

Contract
--------
    looks_blocked(html)               -> bool   (used by fetch_page retries)
    result = classify_product_page(html)
    result.verdict   -> "amazon" | "third_party" | "unknown" | "blocked"
    result.seller    -> seller display name if found, else None
    result.reason    -> short string for logs

Policy (implemented in scanner.py):
  - "blocked"     -> stop fetching product pages this run, alert the owner.
  - "amazon"      -> eligible (shipped_by_amazon=True).
  - "third_party" -> skip while require_shipped_by_amazon is on.
  - "unknown"     -> skip, counted; after a few strikes the ASIN is not
                     re-fetched every run (this is what hammered /dp/ pages
                     and got the bot blocked).
"""
from dataclasses import dataclass
from html import unescape
import re

# --- robot-check markers (both locales, several page variants) --------------

ROBOT_MARKERS = (
    "robot check",
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "api-services-support@amazon.com",
    "/errors/validatecaptcha",
    "أدخل الأحرف التي تظهر في الصورة",
    "لست روبوت",
)

# --- ships-from phrases (page text, tags stripped, lowercased) --------------

SHIPS_FROM_AMAZON = (
    "ships from amazon",
    "ship from amazon",
    "fulfilled by amazon",
    "شحن من أمازون",
    "يشحن من أمازون",
    "يُشحن من أمازون",
    "الشحن من أمازون",
    "تشحن من أمازون",
    "يتم الشحن من أمازون",
    "شحن بواسطة أمازون",
    "الشحن بواسطة أمازون",
    "مُشحن من أمازون",
)

SOLD_BY_AMAZON = (
    "sold by amazon",
    "sold by amazon.eg",
    "يباع بواسطة أمازون",
    "يباع من أمازون",
    "يباع عن طريق أمازون",
    "البائع: amazon",
    "البائع amazon.eg",
)

# A third-party seller link/block: old id, current id, help-link pattern, JSON.
THIRD_PARTY_RE = re.compile(
    r'(sellerProfileTriggerId|/sp\?|/gp/help/seller/at-a-glance[^"]*seller=|'
    r'"merchantId"|"merchantID"|merchant_id)',
    re.IGNORECASE,
)

SELLER_NAME_RE = re.compile(
    r'(?:sellerProfileTriggerId[^>]*>\s*([^<]{2,80}?)\s*<|'
    r'يباع\s+(?:بواسطة|من|عن طريق)\s*</span>[^>]*>\s*([^<]{2,80}?)\s*<|'
    r'"sellerName"\s*:\s*"([^"\\]{2,80})")',
    re.IGNORECASE,
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

DIAC_RE = re.compile(r"[\u064b-\u0652]")   # Arabic harakat: pages vary in them
ALEF_RE = re.compile(r"[\u0623\u0625\u0622]")  # أ إ آ -> ا : spelling varies


def _norm_ar(t):
    return ALEF_RE.sub("\u0627", DIAC_RE.sub("", t))

# Phrases are matched with a flexible gap: "الشحن من: أمازون" and
# "ships from\nAmazon" must match the same way "شحن من أمازون" does.
def _phrase_re(phrase):
    parts = _norm_ar(phrase).split()
    gap = r"[^a-z\u0600-\u06ff]{0,6}"   # punctuation/whitespace between words
    return re.compile(gap.join(re.escape(p) for p in parts), re.IGNORECASE)


SHIPS_FROM_AMAZON_RE = [_phrase_re(p) for p in SHIPS_FROM_AMAZON]
SOLD_BY_AMAZON_RE = [_phrase_re(p) for p in SOLD_BY_AMAZON]


@dataclass
class PageVerdict:
    verdict: str           # amazon | third_party | unknown | blocked
    seller: str | None
    reason: str

    @property
    def blocked(self):
        return self.verdict == "blocked"


def _text(html):
    """Lowercased page text: tags stripped, entities decoded, whitespace folded."""
    t = TAG_RE.sub(" ", html)
    t = unescape(t)
    t = _norm_ar(t)                   # diacritics + alef variants (like the old parser)
    return WS_RE.sub(" ", t).lower()


def looks_blocked(html):
    """True when Amazon served a robot-check/captcha page instead of content.
    Broader than the old 'captcha + api-services-support' test: amazon.eg has
    several block-page variants, all answered with HTTP 200."""
    if not html:
        return False
    low = html.lower()
    return any(m.lower() in low for m in ROBOT_MARKERS)


def classify_product_page(html):
    if not html or len(html) < 2000:
        return PageVerdict("unknown", None,
                           f"page too small ({0 if not html else len(html)} bytes)")

    if looks_blocked(html):
        return PageVerdict("blocked", None, "robot check / captcha page")

    text = _text(html)

    for rx, m in zip(SHIPS_FROM_AMAZON_RE, SHIPS_FROM_AMAZON):
        if rx.search(text):
            return PageVerdict("amazon", "Amazon", f"matched ships-from: {m!r}")

    for rx, m in zip(SOLD_BY_AMAZON_RE, SOLD_BY_AMAZON):
        if rx.search(text):
            return PageVerdict("amazon", "Amazon", f"matched sold-by: {m!r}")

    seller = None
    sm = SELLER_NAME_RE.search(html)
    if sm:
        seller = next((g.strip() for g in sm.groups() if g and g.strip()), None)

    if THIRD_PARTY_RE.search(html):
        return PageVerdict("third_party", seller, "third-party seller block present")

    return PageVerdict("unknown", seller,
                       "no fulfilment phrase found (template changed?)")
