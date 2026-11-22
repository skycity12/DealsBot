"""Split one run's deals between Telegram and Facebook with NO overlap."""


def split_deals(deals, n_telegram=10, n_facebook=10, log=print):
    """Return (telegram_deals, facebook_deals); no ASIN appears in both.

    - deals are ranked by discount strength (internal use only) and handed out
      alternately (1 -> Telegram, 2 -> Facebook, 3 -> Telegram ...) so neither
      platform gets all the best ones;
    - at most n_telegram + n_facebook deals are used;
    - fewer deals than that: split them evenly (Telegram gets the odd one) and
      log it. Nothing is topped up from previously posted deals.
    """
    seen, uniq = set(), []
    for d in sorted(deals, key=lambda d: (-d.get("discount", 0), d["asin"])):
        if d["asin"] in seen:
            continue
        seen.add(d["asin"])
        uniq.append(d)

    total = n_telegram + n_facebook
    uniq = uniq[:total]
    if len(uniq) < total:
        n_telegram = (len(uniq) + 1) // 2
        n_facebook = len(uniq) - n_telegram
        log(f"[split] only {len(uniq)} deals (< {total}): "
            f"{n_telegram} Telegram / {n_facebook} Facebook")

    tg, fb = [], []
    for i, d in enumerate(uniq):
        prefer_tg = i % 2 == 0
        if prefer_tg and len(tg) >= n_telegram:
            prefer_tg = False
        elif not prefer_tg and len(fb) >= n_facebook:
            prefer_tg = True
        (tg if prefer_tg else fb).append(d)
    return tg, fb
