"""Facebook post text. Facebook cuts long posts after the first lines
("See more"), so the first TWO lines are the hook: a catchy line + the product.

Growth mode: no price / discount / saving by default (facebook_show_prices=false),
because those numbers are scraped from Amazon pages. Flip the setting only when
the prices come from an approved source (e.g. the Product Advertising API)."""
import re
from html import unescape

HOOKS = (
    "🔥 لقطة جديدة من أمازون مصر",
    "👀 ده لفت نظرنا على أمازون مصر",
    "🛍 اكتشاف النهاردة من صيّاد العروض",
)


def _hook(asin):
    return HOOKS[sum(map(ord, asin)) % len(HOOKS)]


def _seller_line(info):
    if not info or info.get("amazon_sold"):
        return "🏪 البائع: أمازون ✅ · 📦 الشحن من أمازون"
    name = info.get("seller") or "بائع تاني"
    return f"🏪 البائع: {name} (مش أمازون) · 📦 الشحن من أمازون"


def format_facebook_post(deal, settings, affiliate_on, disclosure):
    """deal: {"asin","title","link","cat":{"hashtag"},"info", optional price fields}.
    Returns plain text (Facebook captions are not HTML)."""
    title = unescape(deal["title"]).strip()
    if len(title) > 110:
        title = title[:107].rstrip() + "..."
    lines = [_hook(deal["asin"]), title, ""]          # lines 1-2 = the hook

    if settings.get("facebook_show_prices") and deal.get("price") and deal.get("list_price"):
        saved = deal["list_price"] - deal["price"]
        lines += [
            f"✅ السعر دلوقتي: {deal['price']:,.0f} جنيه",
            f"❌ قبل كده: {deal['list_price']:,.0f} جنيه",
            f"📉 خصم {deal['discount']:.0f}% · وفّرت {saved:,.0f} جنيه",
            "",
        ]

    lines += [_seller_line(deal.get("info")), "", f"🛒 اشتري من هنا: {deal['link']}", ""]
    tags = [deal["cat"]["hashtag"]] if deal.get("cat") else []
    extra = settings.get("fb_hashtags", "#عروض_امازون #خصومات")
    lines.append(" ".join(tags + [extra]).strip())
    if affiliate_on and disclosure:
        lines += ["", disclosure]      # only when the links really carry the tag
    return "\n".join(lines)


_TAG = re.compile(r"</?(?:b|i|u|s|a|code|pre|em|strong)(?:\s[^>]*)?>", re.I)


def telegram_html_to_plain(caption_html):
    """The Telegram caption (HTML: <b>, <s>, &amp;) as plain text for Facebook, which
    does not render HTML. Same lines, same order, same emoji and numbers."""
    return unescape(_TAG.sub("", caption_html)).strip()


def mirror_telegram_post(caption_html, affiliate_on, disclosure):
    """Facebook post identical to the Telegram one (the caption already carries the
    buy link, because Facebook has no inline button). The disclosure line is added
    only when the links really carry the affiliate tag."""
    text = telegram_html_to_plain(caption_html)
    if affiliate_on and disclosure and disclosure.strip() not in text:   # Telegram caption may carry it already
        text += "\n\n" + disclosure
    return text
