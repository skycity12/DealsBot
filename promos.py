"""Bundle / multi-buy promotions found in the text of an Amazon search card.

Examples (Arabic + English): "Buy 1 get 1 free", "Buy 2 get 1 free",
"Buy 1 get 50% off the second", "Save 10% on 2". Detection is TEXT based over
pages the bot already fetches (no extra requests). It has not been validated
against real amazon.eg pages, so scanner.py runs it in "log" mode by default.

find_promo(text) -> None | {"kind","buy","get","pct","effective","label","text"}
  kind: "free"     buy X get Y free       effective = Y/(X+Y)
        "pct_next" buy X, P% off the next effective = P/(X+1)
        "pct_all"  save P% when buying N  effective = P
"""
import re

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٪", "0123456789%")
_DIAC = re.compile("[ً-ْـ]")
_WORDS = {"one": 1, "two": 2, "three": 3, "واحد": 1, "وحده": 1, "اتنين": 2, "اثنين": 2,
          "اثنتين": 2, "تلاته": 3, "ثلاثه": 3}
NUM = r"(\d{1,2}|one|two|three|واحد|وحده|اتنين|اثنين|اثنتين|تلاته|ثلاثه)"
ORDINAL = re.compile(r"second|2nd|next|another|additional|ثاني|تاني|التاني|الثاني|التاليه|اضافي")

# order matters: percent patterns before the "free" ones
RE_FREE_EN = re.compile(rf"buy\s+{NUM}\s*(?:items?\s*)?,?\s*(?:and\s+)?get\s+{NUM}\s*(?:more\s+|additional\s+)?(?:items?\s*)?free")
RE_FREE_AR = re.compile(
    rf"اشتر[يى]?\s*{NUM}\s*(?:قطع[هة]?|حبه|حبات)?\s*[,،و]?\s*(?:و\s*)?(?:احصل على|احصل علي|احصل|خد|اخد)\s*{NUM}"
    rf"\s*(?:قطع[هة]?|حبه|حبات)?\s*(?:اضافي[هة]?\s*)?مجان")
RE_PCT_BUY = re.compile(rf"(?:buy\s+{NUM}|اشتر[يى]?\s*{NUM})[^%\d]{{0,40}}?(\d{{1,2}})\s*%")
RE_PCT_ALL = re.compile(
    r"save\s+(\d{1,2})\s*%\s*(?:when you buy|on)\s+(\d{1,2})|"
    r"وفر\s*(\d{1,2})\s*%\s*(?:عند شراء|علي شراء|على شراء)\s*(\d{1,2})")


def _norm(text):
    t = _DIAC.sub("", (text or "").translate(_DIGITS)).lower()
    t = re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t)


def _n(tok):
    return int(tok) if tok.isdigit() else _WORDS.get(tok)


def _first(groups):
    return next((g for g in groups if g), None)


def _out(kind, buy, get, pct, eff, text):
    if kind == "free":
        label = f"اشتري {buy} وخد {get} مجانًا"
    elif kind == "pct_next":
        label = f"اشتري {buy} وخد خصم {pct}% على القطعة اللي بعدها"
    else:
        label = f"وفّر {pct}% لما تشتري {buy}"
    return {"kind": kind, "buy": buy, "get": get, "pct": pct,
            "effective": round(eff, 1), "label": label, "text": text.strip()[:80]}


def find_promo(text):
    t = _norm(text)
    if not t:
        return None
    m = RE_PCT_ALL.search(t)
    if m:
        g = m.groups()
        pct, n = int(_first((g[0], g[2]))), int(_first((g[1], g[3])))
        if 5 <= pct <= 90 and 2 <= n <= 6:
            return _out("pct_all", n, None, pct, pct, m.group(0))
    m = RE_PCT_BUY.search(t)
    if m:
        buy, pct = _n(_first(m.groups()[:2]) or ""), int(m.group(3))
        tail = t[m.start(): m.end() + 40]
        on_next = buy and re.search(rf"(?:on|على|علي)\s*(?:the\s*)?{buy + 1}\b", tail)  # "buy 1 get 50% on 2"
        if buy and 1 <= buy <= 5 and 5 <= pct <= 90 and (ORDINAL.search(tail) or on_next):
            return _out("pct_next", buy, None, pct, pct / (buy + 1), m.group(0))
        if buy and 2 <= buy <= 6 and 5 <= pct <= 90:
            return _out("pct_all", buy, None, pct, pct, m.group(0))
    for rx in (RE_FREE_EN, RE_FREE_AR):
        m = rx.search(t)
        if m:
            x, y = _n(m.group(1)), _n(m.group(2))
            if x and y and 1 <= x <= 5 and 1 <= y <= 5:
                return _out("free", x, y, None, y / (x + y) * 100, m.group(0))
    return None


def card_promos(html):
    """{asin: promo} for every search-result card whose own text holds a promotion."""
    from bs4 import BeautifulSoup
    out = {}
    for card in BeautifulSoup(html, "html.parser").select("div[data-asin]"):
        asin = (card.get("data-asin") or "").strip()
        if asin and asin not in out:
            p = find_promo(card.get_text(" ", strip=True))
            if p:
                out[asin] = p
    return out
