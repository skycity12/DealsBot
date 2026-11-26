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
from datetime import datetime, timezone
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

import amazon_verify
import botstate
import categories as cats
import facebook_formatter as fbf
import facebook_publisher as fbp
import promos
import splitter
import weekly

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")          # optional private copy
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")    # e.g. @my_deals_channel
DOMAIN = "https://www.amazon.eg"
DRY_RUN = True   # global lock; main() sets it from settings.dry_run (default: locked)

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
                # broad robot-check detection: amazon.eg has several block-page
                # variants, all served with HTTP 200 (the old two-string test
                # let most of them through and the parser then saw garbage)
                if amazon_verify.looks_blocked(r.text):
                    raise RuntimeError("CAPTCHA (blocked)")   # never retry a block
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


SHIPS_FROM = (
    r"(ships from|shipped by|fulfilled by|يشحن من|الشحن من|يتم الشحن من|"
    r"تشحن من|شحن من|يتم الشحن بواسطة|الشحن بواسطة|شحن بواسطة)"
)


def _buybox_blob(soup):
    parts = []
    for sel in (
        "#merchantInfoFeature_feature_div",
        "#merchant-info",
        "#tabular-buybox",
        "#shipsFromSoldByInsideBuyBox_feature_div",
        "#fulfillerInfoFeature_feature_div",
    ):
        parts += [el.get_text(" ", strip=True) for el in soup.select(sel)]
    return re.sub(r"[ً-ْ]", "", " ".join(parts))  # drop Arabic diacritics


def _seller_name(soup):
    """Name of the (third-party) seller, or None if it can't be read."""
    link = soup.select_one("#sellerProfileTriggerId")
    if link and link.get_text(strip=True):
        return link.get_text(" ", strip=True)
    for row in soup.select("#tabular-buybox .tabular-buybox-container"):
        label = row.select_one(".tabular-buybox-label")
        value = row.select_one(".tabular-buybox-text")
        if label and value and re.search(SOLD_BY, label.get_text(" ", strip=True), re.I):
            return value.get_text(" ", strip=True) or None
    return None


class PageBlocked(Exception):
    """Amazon served a robot-check/captcha page (HTTP 200, fake content)."""


def fulfillment_info(html):
    """What the product page says about who SELLS and who SHIPS the item.
    Returns {"amazon_sold": bool|None, "seller": str|None,
             "shipped_by_amazon": bool|None}. None = couldn't be determined.
    Raises PageBlocked when Amazon served a robot-check page.

    Two layers: the structured buybox selectors (precise when the layout
    matches) plus amazon_verify's full-page phrase match, which survives
    Amazon moving/renaming the buybox containers - the Nov 23 outage: every
    page fetched fine, no container matched, every deal skipped 'not shipped
    by Amazon (or unknown)'."""
    verdict = amazon_verify.classify_product_page(html)
    if verdict.blocked:
        raise PageBlocked(verdict.reason)
    amazon_sold = seller_is_amazon(html)
    soup = BeautifulSoup(html, "html.parser")
    blob = _buybox_blob(soup)
    if re.search(SHIPS_FROM + r"[^A-Za-z؀-ۿ]{0,6}" + AMAZON_NAME, blob, re.I):
        shipped = True            # structured buybox says Amazon ships
    elif re.search(SHIPS_FROM, blob, re.I):
        shipped = False           # a "ships from" line exists and it isn't Amazon
    elif verdict.verdict == "amazon":
        shipped = True            # full-page phrase match (buybox moved/renamed)
    elif verdict.verdict == "third_party":
        shipped = False
    else:
        shipped = None            # no shipping info at all -> unknown
    if amazon_sold is True:
        shipped = True            # Amazon sells it, so Amazon ships it
    return {
        "amazon_sold": amazon_sold,
        "seller": None if amazon_sold else (_seller_name(soup) or verdict.seller),
        "shipped_by_amazon": shipped,
    }


def cached_fulfillment(cand, s):
    """Fulfillment verdict already stored in bot_state from an earlier run.
    Reusing it is what keeps the bot from hammering /dp/ pages every run (the
    hammering is what gets it blocked). An item is re-checked only after it
    stops appearing in the search results for `recheck_seller_days`
    (last_seen stops being refreshed), so a seller change is picked up."""
    if not cand or not cand.get("verified"):
        return None
    if cand.get("shipped_by_amazon") is None:
        return None
    # Age of the VERDICT itself (verified_at, written only when the product page
    # was really fetched). last_seen cannot be used: weekly.touch() refreshes it
    # just before this call, so the age was always ~0 and a verdict never expired.
    if not cand.get("verified_at"):
        # legacy entry from before verified_at existed: start its clock now
        # (spreads re-checks over the week instead of one big burst)
        cand["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not _recent_iso(cand.get("verified_at"), s.get("recheck_seller_days", 7)):
        return None
    return {
        "amazon_sold": cand.get("amazon_sold"),
        "seller": cand.get("seller"),
        "shipped_by_amazon": cand.get("shipped_by_amazon"),
    }


def page_price(html):
    """Current price shown on the product page (None if not readable)."""
    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        ".priceToPay .a-offscreen",
        "#corePrice_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    ):
        el = soup.select_one(sel)
        if el:
            p = parse_price(el.get_text())
            if p:
                return p
    return None


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


def affiliate_on(s):
    """Links carry the affiliate tag only when a tag is set AND growth_mode is off."""
    return bool(s.get("affiliate_tag")) and not s.get("growth_mode")


def product_link(asin, s):
    link = f"{DOMAIN}/dp/{asin}"
    tag = s.get("affiliate_tag")   # the tag itself is never changed or removed from config
    return f"{link}?tag={tag}" if affiliate_on(s) else link


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


def seller_lines(seller, old_price_warning=True):
    """Caption lines saying clearly who sells the item. seller=None keeps the
    old behaviour (sold by Amazon)."""
    if not seller or seller.get("amazon_sold"):
        return ["🏪 البائع: أمازون ✅ · 📦 الشحن من أمازون"]
    name = html_escape(seller.get("seller") or "بائع تاني", quote=False)
    return [
        f"🏪 البائع: <b>{name}</b> (مش أمازون)",
        "📦 الشحن من أمازون",
        *(["⚠️ تنبيه: ده مش أمازون اللي بايع القطعة، والسعر القديم من عند البائع وممكن يكون مبالغ فيه"]
          if old_price_warning else []),   # a bundle deal has no old price to warn about
    ]


def build_caption(title, price, list_price, discount, s, tags, seller=None, category=None,
                  buy_link=None, promo=None, channel_link=None):
    t = html_escape(title if len(title) <= 130 else title[:127].rstrip() + "...", quote=False)
    if list_price is None:        # promotion-only deal: no price discount to show
        head = "🎁 <b>عرض باقة</b>"
        price_lines = [f"✅ السعر: <b>{price:,.0f} جنيه</b>"]
    else:
        head = headline(discount, s)
        price_lines = [
            f"❌ كان: <s>{list_price:,.0f} جنيه</s>",
            f"✅ بقى: <b>{price:,.0f} جنيه</b>",
            f"📉 خصم <b>{discount:.0f}%</b>  ·  💸 وفّرت <b>{list_price - price:,.0f} جنيه</b>",
        ]
    promo_lines = []
    if promo:
        promo_lines = [f"🎁 العرض: <b>{html_escape(promo['label'], quote=False)}</b>"
                       f" (يعني خصم حوالي {promo['effective']:.0f}%)"]
    lines = [
        head,
        "",
        f"🛍 <b>{t}</b>",
        "",
        *price_lines,
        *promo_lines,
        *seller_lines(seller, list_price is not None),
        *(["", f"🛒 اشتري الآن: {buy_link}"] if buy_link else []),   # Facebook has no button
        "",
        tags,
        "",
        "⏱️ السعر وقت النشر وممكن يتغير في أي لحظة",
    ]
    if affiliate_on(s):   # links carry the tag -> the post must say so (never in growth mode)
        lines += ["", html_escape(s.get("affiliate_disclosure") or DEFAULT_DISCLOSURE, quote=False)]
    if s.get("channel_footer"):
        lines += ["", html_escape(s["channel_footer"], quote=False)]
        if channel_link:
            lines += [f"👉 {channel_link}"]
    elif channel_link:
        lines += ["", f"📢 قناتنا على تليجرام: {channel_link}"]
    if category:
        lines += ["", category["hashtag"]]   # category hashtag = last, visible line
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
    if DRY_RUN:
        print("[dry-run] Would post (nothing sent):\n" + caption)
        return "none"
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
SCRIPT_VERSION = "2026-11-23-v8 (v6 + verdict expiry, idle when closed, seen.json untouched in dry-run, no CAPTCHA retry)"


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
    raw.setdefault("unknown", {})   # asin -> {"n": strikes, "t": last failure}
    cutoff = now - 60 * 86400  # forget things older than 60 days
    raw["asins"] = {k: v for k, v in raw["asins"].items() if v.get("t", 0) > cutoff}
    raw["fps"] = {k: t for k, t in raw["fps"].items() if t > cutoff}
    ucut = now - 7 * 86400     # unknown-seller strikes fade after a week
    raw["unknown"] = {k: v for k, v in raw["unknown"].items() if v.get("t", 0) > ucut}
    return raw


def save_seen(seen):
    out = {k: v for k, v in seen.items() if not (k == "unknown" and not v)}
    SEEN_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


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


def page_hint(html):
    """Short description of a page that held no products (for the log)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else "(no title)"
        text = soup.get_text(" ", strip=True)[:140]       # what the page actually says
        return f"{title[:60] or '(no title)'} | text: {text or '(no visible text)'}"
    except Exception:
        return "(unreadable)"


DEFAULT_DISCLOSURE = (
    "💡 بعض الروابط روابط تسويق بالعمولة. "
    "As an Amazon Associate I earn from qualifying purchases."
)


def alert_owner(text, live):
    """Private heads-up to the owner (TELEGRAM_CHAT_ID), never to the channel."""
    if live and TOKEN and CHAT_ID:
        _post("sendMessage", {"chat_id": CHAT_ID, "text": text})
    else:
        print(f"[alert] (dry-run / no private chat) would tell the owner: {text}")


def bump(ctx, key, n=1):
    """One counter per skip/outcome, printed as the end-of-run summary so a
    silent run explains itself in one line."""
    st = ctx.setdefault("stats", {})
    st[key] = st.get(key, 0) + n


def _recent_iso(ts, days):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days < days
    except (TypeError, ValueError):
        return False


def already_posted(asin, price, seen, posted, s):
    """Per-platform dedupe. Telegram: seen.json (unchanged system, price-drop rule
    kept) + the dry-run simulation log. Facebook: its own log. A deal posted on
    either platform within repost_after_days is not offered to the other one."""
    prev = seen["asins"].get(asin)
    if (
        prev
        and is_recent(prev["t"], s["repost_after_days"])
        and price > prev["price"] * (1 - s["repost_min_drop"])
    ):
        return True
    for platform in ("telegram", "facebook"):
        if _recent_iso(posted.get(platform, {}).get(asin), s["repost_after_days"]):
            return True
    return False


def publish_telegram(deals, ctx):
    s, tz, seen = ctx["s"], ctx["tz"], ctx["seen"]
    failures = 0
    for d in deals:
        if failures >= 3:
            print("[telegram] stopping after 3 failures")
            break
        now_l = datetime.now(tz)
        if (
            weekly.in_quiet_hours(now_l, s.get("quiet_hours"))
            or ctx["base_tg"] + ctx["tg_added"] >= s["max_alerts_per_day"]
        ):
            d["cand"]["deferred"] = True
            bump(ctx, "deferred")
            print(f"[defer] {d['asin']} (telegram): quiet hours / daily cap -> "
                  f"posts in the first run after 09:00 / under the cap")
            continue
        caption = build_caption(
            d["title"], d["price"], d["list_price"], d["discount"], d["s_deal"],
            tags_for(d["source"], s), d["info"], d["cat"], promo=d.get("promo"),
        )
        # dry-run: no image download/upload at all; a promotion-only deal has no
        # price discount to draw, so it goes out with the product photo instead
        card = None if (DRY_RUN or d.get("promo_only")) else make_card_bytes(
            d["image"], d["discount"], d["price"], d["list_price"], d["s_deal"]
        )
        mode = send_deal(caption, d["image"], card, d["link"], s.get("channel_handle", ""))
        print(f"[post][telegram] {d['asin']} mode={mode} | {d['title'][:60]}")
        if DRY_RUN:   # simulated: remember only in the dry-run log, never in seen.json
            d["cand"]["deferred"] = False
            ctx["posted_new"]["telegram"][d["asin"]] = weekly._iso(now_l)
            ctx["tg_added"] += 1
            continue
        if mode in ("failed", "none"):
            failures += 1   # not remembered, will retry next run
            continue
        d["cand"]["deferred"] = False   # posted: no longer waiting on anything
        now = time.time()
        seen["asins"][d["asin"]] = {"price": d["price"], "t": now}
        seen["fps"][d["fp"]] = now
        save_seen(seen)     # remember immediately, even if the run dies later
        ctx["tg_added"] += 1
        lo, hi = s["delay_between_posts"]
        wait = random.uniform(lo, hi)
        print(f"   waiting {wait:.0f}s before the next one")
        time.sleep(wait)


def publish_facebook(deals, ctx):
    s, tz = ctx["s"], ctx["tz"]
    if not deals:
        return
    creds, fb_dry, today = ctx["fb_creds"], ctx["fb_dry"], ctx["today"]
    meta = ctx["st0"].get("facebook", {})

    # token health: once per day, before the first post of the day
    # only a GOOD result is remembered for the day: a bad one is re-checked every
    # run, so fixing the token takes effect immediately instead of tomorrow
    chk = meta.get("token_check") or {}
    if chk.get("date") == today and chk.get("valid") is True:
        valid = True
    else:
        res = fbp.validate_facebook_token(creds, s, fb_dry, print)
        valid = res["valid"]
        if not fb_dry and valid:   # a simulated or failed check is never cached
            ctx["fb_meta"]["token_check"] = {"date": today, "valid": True, "error_code": None}
        if res.get("reason"):
            print(f"[facebook] token check: {res['reason']}")
        if res.get("missing_scopes"):
            print(f"[facebook] WARNING token lacks scopes: {res['missing_scopes']}")

    def stop_for_bad_token():
        print("[facebook] token invalid/expired -> Facebook posting stopped (Telegram unaffected)")
        if not fb_dry:   # forget any cached "good" verdict
            ctx["fb_meta"]["token_check"] = {"date": today, "valid": None, "error_code": 190}
        if meta.get("alert_date") != today:
            alert_owner("⚠️ توكن صفحة الفيسبوك محتاج يتجدد - نشر الفيسبوك واقف والتليجرام شغال عادي.",
                        not fb_dry)
            ctx["fb_meta"]["alert_date"] = today

    if not valid:
        stop_for_bad_token()
        return

    disclosure = s.get("affiliate_disclosure") or DEFAULT_DISCLOSURE
    for d in deals:
        now_l = datetime.now(tz)
        if (
            weekly.in_quiet_hours(now_l, s.get("quiet_hours"))
            or ctx["base_fb"] + ctx["fb_added"] >= s["fb_max_per_day"]
        ):
            d["cand"]["deferred"] = True
            bump(ctx, "deferred")
            print(f"[defer] {d['asin']} (facebook): quiet hours / daily cap -> "
                  f"posts in the first run after 09:00 / under the cap")
            continue
        card = None
        handle = (s.get("channel_handle") or "").lstrip("@")
        channel_link = f"https://t.me/{handle}" if handle and s.get("facebook_promote_channel", True) else None
        if s.get("facebook_mirror_telegram", True):
            # identical to the Telegram post (same caption lines, prices included,
            # same branded card); only HTML is stripped and the buy link is a line
            caption = build_caption(
                d["title"], d["price"], d["list_price"], d["discount"], d["s_deal"],
                tags_for(d["source"], s), d["info"], d["cat"], buy_link=d["link"],
                promo=d.get("promo"), channel_link=channel_link,
            )
            text = fbf.mirror_telegram_post(caption, affiliate_on(s), disclosure)
            if not fb_dry and not d.get("promo_only"):   # dry-run: no image download at all
                card = make_card_bytes(d["image"], d["discount"], d["price"],
                                       d["list_price"], d["s_deal"])
        else:
            text = fbf.format_facebook_post(d, s, affiliate_on(s), disclosure)
        mode = fbp.post_deal_to_facebook(d, text, s, creds, None, fb_dry, print, image_bytes=card)
        print(f"[post][facebook] {d['asin']} mode={mode} | {d['title'][:60]}")
        if mode == "token_error":
            stop_for_bad_token()
            return
        # ONE attempt per deal: even a failed/unclear post is remembered so it is
        # never retried (a retry could double-post). Never moved to Telegram.
        # (a simulated post is remembered only in the dry-run state file, so it can
        # never block a real Telegram/Facebook post when another platform is live)
        if not fb_dry or ctx["record_sim"]:
            ctx["posted_new"]["facebook"][d["asin"]] = weekly._iso(now_l)
            if mode != "failed":
                ctx["fb_added"] += 1
        if mode != "failed":
            d["cand"]["deferred"] = False   # posted: no longer waiting on anything
        if not fb_dry:
            lo, hi = s["fb_delay_between_posts"]
            time.sleep(random.uniform(lo, hi))


def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    s = {
        "min_discount": 30,
        "suspicious_discount": 90,
        "min_list_price": 1000,
        "pages_per_source": 3,
        "telegram_per_run": 10,     # per run: 10 for Telegram + 10 for Facebook,
        "fb_per_run": 10,           # never the same ASIN on both
        "only_sold_by_amazon": False,        # True = Amazon as seller only
        "require_shipped_by_amazon": True,   # never post items not shipped by Amazon
        "max_product_checks": 80,            # cap product-page lookups per run
        "recheck_seller_days": 7,            # re-verify fulfillment after this long out of the search results
        "unknown_strikes_skip": 3,           # stop re-fetching an ASIN whose seller stays unreadable
        "max_consecutive_empty_pages": 4,    # stop the run if Amazon keeps returning empty pages
        "delay_between_posts": [30, 40],
        "fb_delay_between_posts": [5, 10],
        "dry_run": True,            # GLOBAL LOCK: nothing is published until false
        "weekly_live": False,       # weekly digest/pin/poll; also needs dry_run=false
        "growth_mode": True,        # links WITHOUT the affiliate tag (tag stays in config)
        "facebook_show_prices": False,   # only used when facebook_mirror_telegram is false
        "promo_deals": "log",              # off | log (detect + log only) | on (publish them)
        "promo_min_effective_discount": 25,  # buy1get1=50, buy2get1=33, buy1+50%off 2nd=25
        "facebook_mirror_telegram": True,
        "facebook_promote_channel": True,   # Facebook posts also carry the Telegram channel link  # Facebook post = the Telegram post (prices included)
        "fb_graph_version": "v27.0",
        "fb_max_per_day": 10,
        "timezone": "Africa/Cairo",
        "max_alerts_per_day": 10,   # daily cap on individual Telegram posts
        "quiet_hours": None,        # {"start": "00:00", "end": "09:00"} = no posting
        "repost_after_days": 14,   # never repost the same product before this...
        "repost_min_drop": 0.10,   # ...unless its price fell by at least this much
        **config.get("settings", {}),
    }
    global DRY_RUN
    DRY_RUN = bool(s["dry_run"])
    categories = config.get("categories") or [cats.default_category([])]
    cats.validate(categories)
    config["categories"] = categories
    tz = weekly.tz_of(s["timezone"])

    fb_creds = fbp.creds_from_env()
    fb_dry = DRY_RUN or fbp.fb_dry_run() or not fbp.creds_complete(fb_creds, need_app=True)
    if not DRY_RUN and not fbp.fb_dry_run() and fb_dry:
        print("[facebook] FB_DRY_RUN=false but FB_* secrets are incomplete -> staying in dry-run")
    tg_live = (not DRY_RUN) and bool(TOKEN) and bool(CHANNEL_ID or CHAT_ID)
    weekly_live = (not DRY_RUN) and bool(s["weekly_live"]) and bool(TOKEN) and bool(CHANNEL_ID)
    any_live = tg_live or weekly_live or not fb_dry
    state_file = botstate.state_path(BASE, any_live)   # prod vs dry-run state
    print(f"scanner version: {SCRIPT_VERSION}")
    print(
        f"mode: dry_run={DRY_RUN} telegram_live={tg_live} facebook_live={not fb_dry} "
        f"weekly_live={weekly_live} growth_mode={s['growth_mode']} state={state_file.name}"
    )
    print(
        f"limits: per_run telegram={s['telegram_per_run']} facebook={s['fb_per_run']} | "
        f"daily telegram={s['max_alerts_per_day']} facebook={s['fb_max_per_day']} | "
        f"quiet={s['quiet_hours']} tz={s['timezone']} | min_discount={s['min_discount']}"
    )
    print(
        f"handle={s.get('channel_handle') or '(none)'} logo={'yes' if LOGO_FILE.exists() else 'MISSING'} "
        f"channel={'set' if CHANNEL_ID else 'NOT SET'} links={'affiliate-tagged' if affiliate_on(s) else 'plain (no tag)'}"
    )
    seller_cache = {}
    live_cache = {}
    title_cache = {}
    checks = 0
    seen = load_seen()
    st0 = botstate.load(state_file)
    cands = {a: dict(c) for a, c in st0["candidates"].items()}   # no prices inside
    today = datetime.now(tz).date().isoformat()
    ctx = {
        "s": s, "tz": tz, "seen": seen, "st0": st0, "today": today,
        "fb_creds": fb_creds, "fb_dry": fb_dry,
        "base_tg": weekly.daily_count(st0, today),
        "base_fb": weekly.daily_count(st0, today, "facebook"),
        "record_sim": not any_live,
        "tg_added": 0, "fb_added": 0,
        "posted_new": {"telegram": {}, "facebook": {}}, "fb_meta": {},
    }
    print(f"loaded memory: {len(seen['asins'])} products already posted")

    per_run_total = s["telegram_per_run"] + s["fb_per_run"]
    pending = []     # deals collected this run (up to 20), published after the scan
    scanned, failures, pages_tried = 0, 0, 0
    empty_run, stop_all, stop_checks = 0, False, False
    promo_logged = set()
    # Both platforms closed (quiet hours or daily cap reached): scanning would only
    # produce deals that are deferred anyway, so do not touch Amazon at all.
    quiet_now = weekly.in_quiet_hours(datetime.now(tz), s.get("quiet_hours"))
    tg_closed = quiet_now or ctx["base_tg"] >= s["max_alerts_per_day"]
    fb_closed = quiet_now or ctx["base_fb"] >= s["fb_max_per_day"]
    if tg_closed and fb_closed:
        stop_all = True
        why = "quiet hours" if quiet_now else "daily caps reached"
        print(f"[idle] {why} on both platforms -> no Amazon requests this run")
    for source in config["sources"]:
        if len(pending) >= per_run_total or stop_all:
            break
        for page in range(1, s["pages_per_source"] + 1):
            if len(pending) >= per_run_total:
                break
            url = paged(source, page)
            pages_tried += 1
            try:
                html = fetch_page(url)
            except Exception as e:
                print(f"[x] {url}: {e}")
                failures += 1
                if "CAPTCHA" in str(e):    # a block page counts like an empty page
                    empty_run += 1
                    if empty_run >= s["max_consecutive_empty_pages"]:
                        print(f"[stop] {empty_run} blocked pages in a row -> stopping this run")
                        stop_all = True
                        break
                time.sleep(random.uniform(4, 8))
                continue
            cards = parse_cards(html)
            if not cards:
                # HTTP was fine but there are no products: usually an Amazon block /
                # robot-check / consent page, not real results. Count it as a failure.
                empty_run += 1
                failures += 1
                print(
                    f"[warn] {url} -> 0 items (page title: {page_hint(html)!r}, "
                    f"{len(html)} bytes) - probably a block page, not real results"
                )
                if empty_run >= s["max_consecutive_empty_pages"]:
                    print(
                        f"[stop] {empty_run} empty pages in a row -> stopping this run "
                        f"so we do not hammer Amazon. Try again later."
                    )
                    stop_all = True
                    break
                time.sleep(random.uniform(4, 8))
                continue
            empty_run = 0
            with_list = [(p, l) for _, _, p, l, _ in cards if l and l > p]
            best = max((round((1 - p / l) * 100) for p, l in with_list), default=0)
            print(
                f"[ok] {url} -> {len(cards)} items, "
                f"{len(with_list)} with discount, best {best}%"
            )
            promo_map = promos.card_promos(html) if s["promo_deals"] != "off" else {}
            for pa, pr in promo_map.items():
                if pa not in promo_logged:
                    promo_logged.add(pa)
                    bump(ctx, "promo_seen")
                    print(f"[promo] {pa}: {pr['kind']} effective={pr['effective']:g}% "
                          f"| text={pr['text']!r} | mode={s['promo_deals']}")
            for asin, title, price, list_price, image in cards:
                scanned += 1
                promo = promo_map.get(asin) if s["promo_deals"] == "on" else None
                if promo and promo["effective"] < s["promo_min_effective_discount"]:
                    promo = None
                discount = is_deal(price, list_price, s)
                promo_only = False
                if discount is None:
                    if promo is None:
                        continue
                    promo_only, discount = True, promo["effective"]   # a bundle deal, no list price
                cand = weekly.touch(
                    cands, asin, title, cats.classify(title, categories)["name"], datetime.now(tz)
                )
                if cand.get("deferred"):
                    # a quiet-hours / daily-cap deferral is temporary, not a life
                    # sentence: once posting is allowed again (after 09:00 Cairo,
                    # or under the cap) the deal is collected normally and the
                    # caps are re-checked at publish time. Before this fix a
                    # deferred deal waited for the weekly digest forever.
                    now_l = datetime.now(tz)
                    if (
                        weekly.in_quiet_hours(now_l, s.get("quiet_hours"))
                        or ctx["base_tg"] + ctx["tg_added"] >= s["max_alerts_per_day"]
                    ):
                        bump(ctx, "skip_deferred_waiting")
                        continue
                    cand["deferred"] = False
                if already_posted(asin, price, seen, st0["posted"], s):
                    continue  # posted on a platform recently (no real new price drop)
                if len(pending) >= per_run_total or any(p["asin"] == asin for p in pending):
                    continue
                info = None
                s_deal = s
                if s["only_sold_by_amazon"] or s["require_shipped_by_amazon"]:
                    if asin not in seller_cache:
                        # reuse the verdict stored in bot_state: only NEW items cost
                        # a product-page fetch (was: every item, every run -> block)
                        cached_info = cached_fulfillment(cands.get(asin), s)
                        if cached_info is not None:
                            seller_cache[asin] = cached_info
                    if asin not in seller_cache:
                        u = seen["unknown"].get(asin)
                        if (
                            u and u.get("n", 0) >= s["unknown_strikes_skip"]
                            and is_recent(u.get("t", 0), 7)
                        ):
                            bump(ctx, "skip_seller_unknown_persistent")
                            continue  # unreadable page 3+ times: stop hammering it
                        if stop_checks:
                            bump(ctx, "skip_checks_stopped")
                            continue  # Amazon is blocking pages: do not make it worse
                        if checks >= s["max_product_checks"]:
                            bump(ctx, "skip_check_budget")
                            continue  # check budget spent; try again next run
                        checks += 1
                        try:
                            time.sleep(random.uniform(3, 6))
                            prod_html = fetch_page(f"{DOMAIN}/dp/{asin}")
                            seller_cache[asin] = fulfillment_info(prod_html)
                            live_cache[asin] = page_price(prod_html)
                            full = page_title(prod_html)
                            if full:
                                title_cache[asin] = full
                            if seller_cache[asin]["shipped_by_amazon"] is not None:
                                seen["unknown"].pop(asin, None)
                                # persist the verdict (yes AND no) so later runs
                                # reuse it instead of fetching the page again
                                weekly.mark_verified(
                                    cands[asin], seller_cache[asin], full,
                                    cats.classify(full or title, categories)["name"],
                                )
                                cands[asin]["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        except PageBlocked as e:
                            stop_checks = True
                            bump(ctx, "blocked_pages")
                            print(f"[stop] {asin}: product page blocked ({e}) - "
                                  f"no more seller checks this run")
                            seller_cache[asin] = None
                        except Exception as e:
                            print(f"[x] seller check {asin}: {e}")
                            if "CAPTCHA" in str(e):
                                stop_checks = True
                                bump(ctx, "blocked_pages")
                            seller_cache[asin] = None
                    info = seller_cache.get(asin)
                    if not info:
                        bump(ctx, "skip_seller_fetch_failed")
                        print(f"[skip] {asin}: seller/shipping unknown")
                        continue
                    if s["only_sold_by_amazon"] and info["amazon_sold"] is not True:
                        bump(ctx, "skip_not_sold_by_amazon")
                        print(f"[skip] {asin}: not sold by Amazon")
                        continue
                    if s["require_shipped_by_amazon"] and info["shipped_by_amazon"] is not True:
                        if info["shipped_by_amazon"] is None:
                            u = seen["unknown"].setdefault(asin, {"n": 0, "t": 0})
                            u["n"] += 1
                            u["t"] = time.time()
                            bump(ctx, "skip_seller_unknown")
                        else:
                            bump(ctx, "skip_third_party_shipping")
                        print(f"[skip] {asin}: not shipped by Amazon (or unknown)")
                        continue
                    title = title_cache.get(asin, title)
                    # the product page price is the truth; the search page can be stale
                    live = live_cache.get(asin)
                    if live and abs(live - price) / price > 0.03:
                        print(f"[price] {asin}: page {live:g} != search {price:g} -> using page price")
                        price = live
                        if not promo_only:
                            discount = is_deal(price, list_price, s)
                            if discount is None:
                                print(f"[skip] {asin}: no longer a deal at the page price")
                                continue
                    if info["amazon_sold"] is not True:
                        # third-party list prices are set by the seller and often
                        # inflated: never label them "possible price error"
                        s_deal = {**s, "suspicious_discount": 1000}
                cat = cats.classify(title, categories)
                if info is not None:   # passed the seller/shipping checks
                    weekly.mark_verified(cand, info, title_cache.get(asin), cat["name"])
                fp = fingerprint(title, price)
                if fp in seen["fps"] and is_recent(seen["fps"][fp], s["repost_after_days"]):
                    print(f"[dup] {asin}: same product/variant already posted -> skipped")
                    continue
                pending.append({
                    "asin": asin, "title": title, "price": price, "list_price": list_price,
                    "discount": discount, "image": image, "link": product_link(asin, s),
                    "cat": cat, "info": info, "source": source, "fp": fp,
                    "s_deal": s_deal, "cand": cand,
                    "promo": promo, "promo_only": promo_only,
                })
            time.sleep(random.uniform(4, 9))

    # ---- split (no overlap), then one publisher per platform: a failure in one
    # never stops the other, and a failed deal is never moved to the other platform
    tg_list, fb_list = splitter.split_deals(
        pending, s["telegram_per_run"], s["fb_per_run"]
    )
    print(f"[split] collected {len(pending)} deals -> Telegram {len(tg_list)} / Facebook {len(fb_list)}")
    for name, fn, lst in (("telegram", publish_telegram, tg_list), ("facebook", publish_facebook, fb_list)):
        try:
            fn(lst, ctx)
        except Exception:
            print(f"[{name}] ERROR (other platform unaffected):\n" + traceback.format_exc())

    if not DRY_RUN:     # dry-run never rewrites seen.json
        save_seen(seen)
    try:   # collected candidates + counters (separate file; seen.json untouched)
        weekly.flush_scan_state(
            state_file, cands, today, ctx["tg_added"], datetime.now(tz),
            extra={"posted": ctx["posted_new"], "fb_added": ctx["fb_added"],
                   "facebook_meta": ctx["fb_meta"]},
        )
        client = weekly.make_client(weekly_live, TOKEN, print)
        status = weekly.run_weekly(
            config, {**s, "channel_id": CHANNEL_ID}, state_file=state_file,
            live=weekly_live, client=client, link_fn=lambda a: product_link(a, s),
            handle=s.get("channel_handle", ""), log=print,
        )
        print(f"[weekly] status: {status}")
    except Exception:  # the weekly job must never break the scan
        print("[weekly] ERROR (scan unaffected):\n" + traceback.format_exc())
    print(
        f"Scanned {scanned} items, collected {len(pending)}, "
        f"telegram {ctx['tg_added']}, facebook {ctx['fb_added']}, {failures} failed pages"
    )
    # one-line diagnosis: a silent run now says WHY (seller checks, blocks,
    # defers, caps) instead of leaving you to guess from 2000 skip lines
    stats = ctx.get("stats", {})
    if stats:
        print("run summary: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    if stats.get("blocked_pages"):
        alert_owner(
            "⚠️ أمازون بدأ يحظر صفحات المنتجات (captcha) - البوت وقف فحص البائعين "
            "لباقي التشغيلة. لو تكرر ده، قلل عدد التشغيلات في اليوم.",
            tg_live or not fb_dry,
        )
    if (
        not DRY_RUN and not pending
        and (stats.get("skip_seller_unknown") or stats.get("blocked_pages")
             or stats.get("skip_check_budget"))
    ):
        alert_owner(
            "⚠️ التشغيلة دي من غير أي عرض جديد، والسبب فحص البائع/الشحن مش فلتر "
            "الخصومات - راجع سطر run summary في اللوج.",
            tg_live or not fb_dry,
        )
    if pages_tried and failures == pages_tried:
        sys.exit(1)


if __name__ == "__main__":
    main()
