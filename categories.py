"""Hashtag categories (Telegram has no sections inside one channel).

Categories live in scanner_config.json -> "categories", checked IN ORDER; the
first one whose keyword appears in the product NAME wins. Anything unclear
falls back to the category flagged "default": true (متنوع)."""
import re

_DIACRITICS = re.compile(r"[ً-ْـ]")  # tashkeel + tatweel


def normalize(text):
    t = _DIACRITICS.sub("", text or "").lower()
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def _matches(keyword, text):
    kw = normalize(keyword)
    if not kw:
        return False
    if re.fullmatch(r"[a-z0-9 \-]+", kw):
        # latin keyword: whole word, optional plural ("shoe" matches "shoes",
        # but "fan" does not match "fantasy")
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?:s|es)?(?![a-z0-9])", text) is not None
    return kw in text


def default_category(categories):
    for c in categories:
        if c.get("default"):
            return c
    return {"name": "متنوع", "hashtag": "#متنوع", "keywords": [], "default": True}


def classify(title, categories):
    """Return the category dict for a product name."""
    text = normalize(title)
    for c in categories:
        if c.get("default"):
            continue
        if any(_matches(k, text) for k in c.get("keywords", [])):
            return c
    return default_category(categories)


def validate(categories):
    """Raise ValueError on a broken category list (checked at startup)."""
    if not categories:
        raise ValueError("categories is empty")
    seen = set()
    defaults = 0
    for c in categories:
        for key in ("name", "hashtag"):
            if not c.get(key):
                raise ValueError(f"category missing '{key}': {c}")
        if not c["hashtag"].startswith("#") or " " in c["hashtag"]:
            raise ValueError(f"bad hashtag: {c['hashtag']!r}")
        if c["name"] in seen:
            raise ValueError(f"duplicate category name: {c['name']}")
        seen.add(c["name"])
        defaults += 1 if c.get("default") else 0
    if defaults != 1:
        raise ValueError("exactly one category must have default=true")
