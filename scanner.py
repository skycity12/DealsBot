#!/usr/bin/env python3
"""Amazon.eg deal scanner: scans listing/search pages and alerts on huge
discounts or suspiciously low prices (possible pricing errors)."""
import json
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "scanner_config.json"
SEEN_FILE = BASE / "seen.json"

from html import escape as html_escape
from io import BytesIO
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")          # optional private copy
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")    # e.g. @my_deals_channel
DOMAIN = "https://www.amazon.eg"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def parse_price(text):
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_cards(html):
    """Extract (asin, title, price, list_price) from a search/listing page."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div[data-asin]"):
        asin = (card.get("data-asin") or "").strip()
        if not asin:
            continue
        price_el = card.select_one(
            'span.a-price:not([data-a-strike="true"]) span.a-offscreen'
        )
        list_el = card.select_one('span.a-price[data-a-strike="true"] span.a-offscreen')
        price = parse_price(price_el.get_text()) if price_el else None
        list_price = parse_price(list_el.get_text()) if list_el else None
        if not price:
            continue
        title_el = card.select_one("h2")
        img_el = card.select_one("img.s-image") or card.select_one("img")
        # Amazon's search h2 is sometimes just the brand ("كاسيو"); the image alt
        # and the h2's aria-label usually hold the full product name. Keep the longest.
        candidates = []
        if title_el:
            candidates.append(title_el.get_text(" ", strip=True))
            candidates.append((title_el.get("aria-label") or "").strip())
        if img_el:
            candidates.append((img_el.get("alt") or "").strip())
        title = max(candidates, key=len, default="") or asin
        image = img_el.get("src") if img_el else None
        items.append((asin, title, price, list_price, image))
    return items


SESSION = requests.Session()
SESSION.headers.update(
    {
        **HEADERS,
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
)


def fetch_page(url, retries=4):
    """GET with retries: Amazon often answers 503/429 to bursts of requests."""
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=30)
        except requests.RequestException as e:
            last = f"network error: {e}"
        else:
            if r.status_code == 200:
                low = r.text.lower()
                if "captcha" in low and "api-services-support" in low:
                    last = "CAPTCHA (blocked)"
                else:
                    return r.text
            else:
                last = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break  # not worth retrying (404 etc.)
        if attempt < retries - 1:
            wait = (2 ** attempt) * 8 + random.uniform(2, 8)  # ~10s, ~22s, ~40s
            print(f"   retry {attempt + 1} after {wait:.0f}s ({last})")
            time.sleep(wait)
    raise RuntimeError(last)


AMAZON_NAME = r"(amazon|أمازون|امازون)"
SOLD_BY = r"(sold by|يباع بواسطة|تباع بواسطة|البائع)"


def seller_is_amazon(html):
    """True if the product page says the seller is Amazon itself
    (not just 'fulfilled by Amazon'). None if it can't be determined."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("#sellerProfileTriggerId")
    if link:
        return bool(re.search(AMAZON_NAME, link.get_text(), re.I))
    parts = []
    for sel in (
        "#merchantInfoFeature_feature_div",
        "#merchant-info",
        "#tabular-buybox",
        "#shipsFromSoldByInsideBuyBox_feature_div",
    ):
        parts += [el.get_text(" ", strip=True) for el in soup.select(sel)]
    blob = re.sub(r"[ً-ْ]", "", " ".join(parts))  # drop Arabic diacritics
    if not blob.strip():
        return None
    return bool(re.search(SOLD_BY + r"[^A-Za-z؀-ۿ]{0,6}" + AMAZON_NAME, blob, re.I))


def page_title(html):
    """Full product name from a product page (#productTitle)."""
    el = BeautifulSoup(html, "html.parser").select_one("#productTitle")
    return el.get_text(" ", strip=True) if el else None


def is_deal(price, list_price, s):
    """Return the discount percentage if this is a crazy deal, else None."""
    if not list_price or list_price <= price:
        return None
    if list_price < s["min_list_price"]:
        return None
    discount = (1 - price / list_price) * 100
    return discount if discount >= s["min_discount"] else None


def product_link(asin, s):
    link = f"{DOMAIN}/dp/{asin}"
    tag = s.get("affiliate_tag")
    return f"{link}?tag={tag}" if tag else link


DEFAULT_TAGS = {
    "laptop": "#لابتوب #كمبيوتر",
    "mobile": "#موبايل #هواتف",
    "phone": "#موبايل #هواتف",
    "tv": "#شاشات #تلفزيون",
    "headphones": "#سماعات",
    "watch": "#ساعات",
    "air fryer": "#اير_فراير #اجهزة_منزلية",
    "perfume": "#عطور",
    "playstation": "#بلايستيشن #جيمنج",
    "shoes": "#احذية",
    "clothes": "#ملابس",
    "kitchen": "#مطبخ",
    "toys": "#العاب",
    "beauty": "#جمال #عناية",
    "home appliances": "#اجهزة_منزلية",
}


def tags_for(source, s):
    """Hashtags based on the search keyword of the source URL."""
    kw = parse_qs(urlparse(source).query).get("k", [""])[0].lower()
    tags = {**DEFAULT_TAGS, **s.get("hashtags", {})}
    for key, val in tags.items():
        if key in kw:
            return f"{val} #عروض_أمازون"
    return "#عروض_أمازون"


def headline(discount, s):
    if discount >= s["suspicious_discount"]:
        return (
            "🚨🚨 <b>خصم غير طبيعي - ممكن يكون خطأ في السعر!</b> 🚨🚨\n"
            "اطلبه بسرعة قبل ما يتصلح"
        )
    if discount >= 75:
        return "🔥🔥🔥 <b>عرض نااار</b> 🔥🔥🔥"
    if discount >= 60:
        return "🔥🔥 <b>خصم قوي</b> 🔥🔥"
    return "💰 <b>عرض حلو</b>"


def build_caption(title, price, list_price, discount, s, tags):
    t = html_escape(title if len(title) <= 130 else title[:127].rstrip() + "...", quote=False)
    saved = list_price - price
    lines = [
        headline(discount, s),
        "",
        f"🛍 <b>{t}</b>",
        "",
        f"❌ كان: <s>{list_price:,.0f} جنيه</s>",
        f"✅ بقى: <b>{price:,.0f} جنيه</b>",
        f"📉 خصم <b>{discount:.0f}%</b>  ·  💸 وفّرت <b>{saved:,.0f} جنيه</b>",
        "🏪 البائع: أمازون",
        "",
        tags,
        "",
        "⏱️ السعر وقت النشر وممكن يتغير في أي لحظة",
    ]
    if s.get("channel_footer"):
        lines += ["", html_escape(s["channel_footer"], quote=False)]
    return "\n".join(lines)


# ---------- product image card ----------
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
]


def _font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def big_image_url(url):
    """Amazon thumbnails carry a size suffix (._AC_UY218_.); ask for a big one."""
    return re.sub(r"\._[A-Za-z0-9_,%-]+_\.", "._AC_SL1000_.", url)


def download_image(url):
    if not url:
        return None
    for u in dict.fromkeys((big_image_url(url), url)):
        try:
            r = requests.get(u, headers=HEADERS, timeout=20)
            if r.ok and r.content:
                return Image.open(BytesIO(r.content)).convert("RGB")
            print(f"[!] image HTTP {r.status_code} for {u}")
        except Exception as e:
            print(f"[!] image download failed: {e}")
    return None


# Brand palette (matches the fisherman logo)
SEA_DEEP, SEA, SKY = "#0A5F99", "#2FB2E6", "#7FD0FF"
SUN, ORANGE, GREEN, PURPLE, RED, NAVY = "#FFD23F", "#FF9F1C", "#2ECC71", "#8E5BFF", "#E63946", "#0F1B2D"
SOFT = "#CFE9FA"
LOGO_FILE = BASE / "logo.png"


def _logo_circle(size):
    try:
        lg = Image.open(LOGO_FILE).convert("RGB").resize((size, size), Image.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(lg, (0, 0), mask)
    return out


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def make_card(product_img, discount, price, list_price, handle, s):
    """1080x1080 branded card: product photo + big discount badge + prices."""
    W = H = 1080
    bg = Image.new("RGB", (W, H), SEA)
    d = ImageDraw.Draw(bg)
    top, bot = _hex(SEA), _hex(SEA_DEEP)
    for y in range(H):  # sea-blue vertical gradient
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

    panel = (50, 50, 1030, 680)
    d.rounded_rectangle(panel, radius=40, fill="white")
    img = product_img.copy()
    img.thumbnail((panel[2] - panel[0] - 70, panel[3] - panel[1] - 70))
    bg.paste(img, ((panel[0] + panel[2] - img.width) // 2, (panel[1] + panel[3] - img.height) // 2))

    # discount badge (colour by strength)
    if discount >= s["suspicious_discount"]:
        color = PURPLE
    elif discount >= 75:
        color = RED
    else:
        color = ORANGE
    cx, cy, r = 175, 175, 125
    d.ellipse([cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10], fill="white")
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    txt = f"-{discount:.0f}%"
    d.text((cx, cy), txt, font=_font(70 if len(txt) <= 4 else 56), fill="white", anchor="mm")

    # prices
    d.text((60, 715), "NOW", font=_font(34), fill=SOFT, anchor="la")
    d.text((60, 755), f"{price:,.0f} EGP", font=_font(112), fill=SUN, anchor="la")
    old = f"{list_price:,.0f} EGP"
    f_old = _font(50)
    d.text((60, 890), old, font=f_old, fill=SOFT, anchor="la")
    b = d.textbbox((60, 890), old, font=f_old, anchor="la")
    ym = (b[1] + b[3]) // 2
    d.line([(b[0] - 6, ym), (b[2] + 6, ym)], fill=RED, width=6)

    save = f"SAVE {list_price - price:,.0f} EGP"
    f = _font(42)
    sb = d.textbbox((0, 0), save, font=f, anchor="la")
    w, h = sb[2] - sb[0], sb[3] - sb[1]
    x1, y1 = 1020 - w - 60, 885
    d.rounded_rectangle([x1, y1, 1020, y1 + h + 36], radius=30, fill=GREEN)
    d.text(((x1 + 1020) // 2, y1 + (h + 36) // 2), save, font=f, fill=NAVY, anchor="mm")

    # footer: logo + handle
    lg = _logo_circle(88)
    if lg:
        bg.paste(lg, (60, 972), lg)
    if handle:
        d.text((170 if lg else 60, 1016), "@" + handle.lstrip("@"), font=_font(40), fill=SOFT, anchor="lm")

    out = BytesIO()
    bg.save(out, "JPEG", quality=90)
    return out.getvalue()


def make_card_bytes(image_url, discount, price, list_price, s):
    try:
        img = download_image(image_url)
        if img is None:
            print(f"[card] FAILED: could not download product image ({image_url})")
            return None
        data = make_card(img, discount, price, list_price, s.get("channel_handle", ""), s)
        print(f"[card] ok ({len(data) // 1024} KB)")
        return data
    except Exception as e:
        last = traceback.format_exc().strip().splitlines()[-3:]
        print(f"[card] FAILED: {type(e).__name__}: {e} | {' / '.join(x.strip() for x in last)}")
        return None


def _post(method, payload, files=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if files:
        resp = requests.post(url, data=payload, files=files, timeout=60)
    else:
        resp = requests.post(url, json=payload, timeout=30)
    if not resp.ok:
        print(f"[!] Telegram {method} error:", resp.text[:300])
    return resp.ok


def send_deal(caption, image_url, card, link, handle):
    """Post to the channel (and optionally a private chat).
    Returns how the FIRST target was posted: 'card', 'url-photo', 'text',
    'failed' or 'none' (nothing configured)."""
    if not TOKEN:
        print("[!] Telegram not configured. Would post:\n" + caption)
        return "none"
    targets = [t for t in (CHANNEL_ID, CHAT_ID) if t]
    if not targets:
        print("[!] No Telegram target set. Would post:\n" + caption)
        return "none"

    rows = [[{"text": "🛒 اشتري الآن", "url": link}]]
    if handle:
        h = handle.lstrip("@")
        share = (
            f"https://t.me/share/url?url=https://t.me/{h}&text="
            + quote("أقوى عروض أمازون مصر 🔥")
        )
        rows.append([{"text": "📤 شارك القناة مع صحابك", "url": share}])
    markup = {"inline_keyboard": rows}

    first_mode = None
    for chat in targets:
        mode = "failed"
        base = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
        if card and _post(
            "sendPhoto",
            {**base, "reply_markup": json.dumps(markup)},
            files={"photo": ("deal.jpg", card, "image/jpeg")},
        ):
            mode = "card"
        elif image_url and _post(
            "sendPhoto", {**base, "photo": image_url, "reply_markup": markup}
        ):
            mode = "url-photo"
        elif _post(
            "sendMessage",
            {"chat_id": chat, "text": caption, "parse_mode": "HTML", "reply_markup": markup},
        ):
            mode = "text"
        first_mode = first_mode or mode
        time.sleep(1.5)  # stay under Telegram's rate limits
    return first_mode


# ---------- memory of what was already posted ----------
SCRIPT_VERSION = "2026-11-20-v3 (card images + dedupe)"


def load_seen():
    """seen.json = {"asins": {asin: {"price", "t"}}, "fps": {fingerprint: t}}"""
    now = time.time()
    raw = {}
    if SEEN_FILE.exists():
        try:
            raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] seen.json unreadable ({e}); starting empty")
    if "asins" not in raw:  # migrate old flat format {asin: price}
        raw = {
            "asins": {
                k: {"price": v, "t": now}
                for k, v in raw.items()
                if isinstance(v, (int, float))
            },
            "fps": {},
        }
    raw.setdefault("fps", {})
    cutoff = now - 60 * 86400  # forget things older than 60 days
    raw["asins"] = {k: v for k, v in raw["asins"].items() if v.get("t", 0) > cutoff}
    raw["fps"] = {k: t for k, t in raw["fps"].items() if t > cutoff}
    return raw


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def fingerprint(title, price):
    """Same product sold as several variants/ASINs gets the same fingerprint."""
    base = re.sub(r"[^a-z0-9؀-ۿ]+", " ", title.lower()).split()
    return f"{' '.join(base[:8])}|{round(price, -1):.0f}"


def is_recent(t, days):
    return (time.time() - t) < days * 86400


def paged(url, page):
    if page == 1:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    s = {
        "min_discount": 80,
        "suspicious_discount": 90,
        "min_list_price": 1000,
        "pages_per_source": 3,
        "max_alerts_per_run": 15,
        "only_sold_by_amazon": True,
        "delay_between_posts": [30, 40],
        "repost_after_days": 14,   # never repost the same product before this...
        "repost_min_drop": 0.10,   # ...unless its price fell by at least this much
        **config.get("settings", {}),
    }
    print(f"scanner version: {SCRIPT_VERSION}")
    print(
        f"settings: min_discount={s['min_discount']} max_alerts={s['max_alerts_per_run']} "
        f"handle={s.get('channel_handle') or '(none)'} logo={'yes' if LOGO_FILE.exists() else 'MISSING'} "
        f"channel={'set' if CHANNEL_ID else 'NOT SET'}"
    )
    seller_cache = {}
    title_cache = {}
    seen = load_seen()
    print(f"loaded memory: {len(seen['asins'])} products already posted")

    alerts, scanned, failures, pages_tried, post_failures = 0, 0, 0, 0, 0
    for source in config["sources"]:
        for page in range(1, s["pages_per_source"] + 1):
            url = paged(source, page)
            pages_tried += 1
            try:
                html = fetch_page(url)
            except Exception as e:
                print(f"[x] {url}: {e}")
                failures += 1
                time.sleep(random.uniform(4, 8))
                continue
            cards = parse_cards(html)
            with_list = [(p, l) for _, _, p, l, _ in cards if l and l > p]
            best = max((round((1 - p / l) * 100) for p, l in with_list), default=0)
            print(
                f"[ok] {url} -> {len(cards)} items, "
                f"{len(with_list)} with discount, best {best}%"
            )
            for asin, title, price, list_price, image in cards:
                scanned += 1
                discount = is_deal(price, list_price, s)
                if discount is None:
                    continue
                prev = seen["asins"].get(asin)
                if (
                    prev
                    and is_recent(prev["t"], s["repost_after_days"])
                    and price > prev["price"] * (1 - s["repost_min_drop"])
                ):
                    continue  # already posted; no real new price drop
                if alerts >= s["max_alerts_per_run"] or post_failures >= 3:
                    continue
                if s["only_sold_by_amazon"]:
                    if asin not in seller_cache:
                        try:
                            time.sleep(random.uniform(3, 6))
                            prod_html = fetch_page(f"{DOMAIN}/dp/{asin}")
                            seller_cache[asin] = seller_is_amazon(prod_html)
                            full = page_title(prod_html)
                            if full:
                                title_cache[asin] = full
                        except Exception as e:
                            print(f"[x] seller check {asin}: {e}")
                            seller_cache[asin] = None
                    if seller_cache[asin] is not True:
                        print(f"[skip] {asin}: not sold by Amazon (or unknown)")
                        continue
                    title = title_cache.get(asin, title)
                fp = fingerprint(title, price)
                if fp in seen["fps"] and is_recent(seen["fps"][fp], s["repost_after_days"]):
                    print(f"[dup] {asin}: same product/variant already posted -> skipped")
                    continue
                caption = build_caption(
                    title, price, list_price, discount, s, tags_for(source, s)
                )
                card = make_card_bytes(image, discount, price, list_price, s)
                mode = send_deal(
                    caption, image, card, product_link(asin, s), s.get("channel_handle", "")
                )
                print(f"[post] {asin} mode={mode} | {title[:60]}")
                if mode in ("failed", "none"):
                    post_failures += 1  # not remembered, will retry next run
                    continue
                now = time.time()
                seen["asins"][asin] = {"price": price, "t": now}
                seen["fps"][fp] = now
                save_seen(seen)  # remember immediately, even if the run dies later
                alerts += 1
                # space posts out so the channel isn't flooded all at once
                lo, hi = s["delay_between_posts"]
                wait = random.uniform(lo, hi)
                print(f"   waiting {wait:.0f}s before the next one")
                time.sleep(wait)
            time.sleep(random.uniform(4, 9))

    save_seen(seen)
    print(f"Scanned {scanned} items, sent {alerts} alerts, {failures} failed pages")
    if pages_tried and failures == pages_tried:
        sys.exit(1)


if __name__ == "__main__":
    main()
