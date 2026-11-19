#!/usr/bin/env python3
"""Amazon.eg deal scanner: scans listing/search pages and alerts on huge
discounts or suspiciously low prices (possible pricing errors)."""
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "scanner_config.json"
SEEN_FILE = BASE / "seen.json"

from html import escape as html_escape

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
        title = title_el.get_text(" ", strip=True) if title_el else asin
        img_el = card.select_one("img.s-image") or card.select_one("img")
        image = img_el.get("src") if img_el else None
        items.append((asin, title, price, list_price, image))
    return items


def fetch_page(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    low = r.text.lower()
    if "captcha" in low and "api-services-support" in low:
        raise RuntimeError("CAPTCHA (blocked)")
    return r.text


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


def build_caption(title, price, list_price, discount, link, s):
    t = html_escape(title[:110])
    lines = [f"🔥 <b>{t}</b>", ""]
    if discount >= s["suspicious_discount"]:
        lines += ["⚠️ <b>خصم غير عادي - ممكن يكون خطأ في السعر، اطلبه بسرعة</b>", ""]
    lines += [
        f"❌ السعر قبل: <s>{list_price:,.0f} جنيه</s>",
        f"✅ السعر دلوقتي: <b>{price:,.0f} جنيه</b>",
        f"📉 خصم <b>{discount:.0f}%</b>",
        "🏪 البائع: أمازون",
        "",
        f'🛒 <a href="{link}">اشتري من هنا</a>',
    ]
    if s.get("channel_footer"):
        lines += ["", html_escape(s["channel_footer"])]
    return "\n".join(lines)


def _post(method, payload):
    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=30
    )
    if not resp.ok:
        print(f"[!] Telegram {method} error:", resp.text[:300])
    return resp.ok


def send_deal(caption, image):
    """Post to the channel (and optionally a private chat) with the product image."""
    if not TOKEN:
        print("[!] Telegram not configured. Would post:\n" + caption)
        return
    targets = [t for t in (CHANNEL_ID, CHAT_ID) if t]
    if not targets:
        print("[!] No Telegram target set. Would post:\n" + caption)
        return
    for chat in targets:
        sent = False
        if image:
            sent = _post(
                "sendPhoto",
                {"chat_id": chat, "photo": image, "caption": caption, "parse_mode": "HTML"},
            )
        if not sent:
            _post(
                "sendMessage",
                {"chat_id": chat, "text": caption, "parse_mode": "HTML"},
            )
        time.sleep(1.5)  # stay under Telegram's rate limits


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
        **config.get("settings", {}),
    }
    seller_cache = {}
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}

    alerts, scanned, failures, pages_tried = 0, 0, 0, 0
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
            print(f"[ok] {url} -> {len(cards)} items")
            for asin, title, price, list_price, image in cards:
                scanned += 1
                discount = is_deal(price, list_price, s)
                if discount is None:
                    continue
                prev = seen.get(asin)
                if prev is not None and price >= prev:
                    continue  # already alerted at this price or lower
                if alerts >= s["max_alerts_per_run"]:
                    continue
                if s["only_sold_by_amazon"]:
                    if asin not in seller_cache:
                        try:
                            time.sleep(random.uniform(3, 6))
                            seller_cache[asin] = seller_is_amazon(
                                fetch_page(f"{DOMAIN}/dp/{asin}")
                            )
                        except Exception as e:
                            print(f"[x] seller check {asin}: {e}")
                            seller_cache[asin] = None
                    if seller_cache[asin] is not True:
                        print(f"[skip] {asin}: not sold by Amazon (or unknown)")
                        continue
                caption = build_caption(
                    title, price, list_price, discount, product_link(asin, s), s
                )
                send_deal(caption, image)
                seen[asin] = price
                alerts += 1
            time.sleep(random.uniform(4, 9))

    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Scanned {scanned} items, sent {alerts} alerts, {failures} failed pages")
    if pages_tried and failures == pages_tried:
        sys.exit(1)


if __name__ == "__main__":
    main()
