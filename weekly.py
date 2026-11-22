"""Weekly job: «أفضل عروض الأسبوع» digest + pin handling + weekly poll.

DRY-RUN FIRST. Nothing here talks to Telegram unless `live=True`, which the
caller only sets when ALL of these hold: global dry_run=false, weekly_live=true,
a bot token and a channel id are present. Otherwise a SimClient only logs what
it WOULD do and a separate state file (bot_state.dryrun.json) is used, so
production state is never touched.

No price / discount / stock appears anywhere in this module: candidates only
keep name, ASIN, category, seller info and the days they were caught."""
import json
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape as html_escape
from zoneinfo import ZoneInfo

import requests

import botstate
import categories as cats

# ---------------------------------------------------------------- time helpers


def tz_of(name):
    return ZoneInfo(name or "Africa/Cairo")


def _hm(text):
    h, m = str(text).split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(now_local, quiet):
    """True if now (aware, local) is inside quiet_hours {"start","end"}.
    Supports windows that wrap past midnight (e.g. 22:00-06:00)."""
    if not quiet:
        return False
    start, end = _hm(quiet["start"]), _hm(quiet["end"])
    t = now_local.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def scheduled_time(now_local, weekday, hour, minute):
    """Most recent scheduled moment (aware, local wall-clock 'weekday HH:MM')
    that is <= now. Built from local wall time, so DST is handled by zoneinfo."""
    tz = now_local.tzinfo
    delta = (now_local.weekday() - weekday) % 7
    d = now_local.date() - timedelta(days=delta)
    cand = datetime.combine(d, dtime(hour, minute), tzinfo=tz)
    if cand.astimezone(timezone.utc) > now_local.astimezone(timezone.utc):
        cand = datetime.combine(d - timedelta(days=7), dtime(hour, minute), tzinfo=tz)
    return cand


def week_key(scheduled_local):
    iso = scheduled_local.date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def check_due(now_local, wcfg, live):
    """-> (due, scheduled_local, week_key). Live runs may catch up for
    catch_up_hours after the scheduled time; dry-run only simulates inside a
    short window so CI logs are not spammed."""
    sched = scheduled_time(
        now_local, wcfg.get("weekday", 3), wcfg.get("hour", 21), wcfg.get("minute", 0)
    )
    elapsed_min = (
        now_local.astimezone(timezone.utc) - sched.astimezone(timezone.utc)
    ).total_seconds() / 60
    window = (
        wcfg.get("catch_up_hours", 6) * 60 if live else wcfg.get("dry_run_window_minutes", 30)
    )
    return 0 <= elapsed_min < window, sched, week_key(sched)


# ------------------------------------------------------------ candidate memory


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_candidate(asin, name, category, now_local):
    day = now_local.date().isoformat()
    return {
        "asin": asin,
        "name": name,
        "category": category,
        "first_seen": _iso(now_local),
        "last_seen": _iso(now_local),
        "days": [day],
        "verified": None,           # None = product page not checked yet
        "amazon_sold": None,
        "shipped_by_amazon": None,
        "seller": None,
        "deferred": False,          # waits for the digest instead of its own post
    }


def touch(cands, asin, name, category, now_local):
    c = cands.get(asin)
    if c is None:
        c = cands[asin] = new_candidate(asin, name, category, now_local)
    c["last_seen"] = _iso(now_local)
    day = now_local.date().isoformat()
    if day not in c["days"]:
        c["days"].append(day)
    return c


def mark_verified(c, info, full_name, category):
    c["verified"] = True
    c["amazon_sold"] = info.get("amazon_sold")
    c["shipped_by_amazon"] = info.get("shipped_by_amazon")
    c["seller"] = info.get("seller")
    if full_name:
        c["name"] = full_name
    c["category"] = category


def merge_candidates(disk, mem):
    """Merge this run's candidate entries into those on disk (union of days,
    newest last_seen, verified info from whichever has it)."""
    for asin, m in mem.items():
        d = disk.get(asin)
        if d is None:
            disk[asin] = m
            continue
        d["days"] = sorted(set(d.get("days", [])) | set(m.get("days", [])))[-14:]
        d["last_seen"] = max(d.get("last_seen", ""), m.get("last_seen", ""))
        if m.get("verified"):
            for k in ("name", "category", "amazon_sold", "shipped_by_amazon", "seller", "verified"):
                d[k] = m.get(k)
        d["deferred"] = bool(d.get("deferred") or m.get("deferred"))


def prune_candidates(cands, now_local, keep_days=14):
    cutoff = _iso(now_local - timedelta(days=keep_days))
    for asin in [a for a, c in cands.items() if c.get("last_seen", "") < cutoff]:
        del cands[asin]


def flush_scan_state(path, mem_cands, daily_date, daily_added, now_local, extra=None):
    """Persist what the scan collected under lock: candidates, daily counters and
    (extra) the per-platform posted log + Facebook meta. seen.json is not touched."""
    extra = extra or {}
    with botstate.locked(path):
        st = botstate.load(path)
        merge_candidates(st["candidates"], mem_cands)
        prune_candidates(st["candidates"], now_local)
        if st["daily"].get("date") != daily_date:
            st["daily"] = {"date": daily_date, "count": 0, "fb_count": 0}
        st["daily"]["count"] += daily_added
        st["daily"]["fb_count"] = st["daily"].get("fb_count", 0) + extra.get("fb_added", 0)
        for platform, items in (extra.get("posted") or {}).items():
            st["posted"].setdefault(platform, {}).update(items)
        cutoff = _iso(now_local - timedelta(days=30))
        for platform in st["posted"]:
            st["posted"][platform] = {a: t for a, t in st["posted"][platform].items() if t >= cutoff}
        st["facebook"].update(extra.get("facebook_meta") or {})
        botstate.save(path, st)


def daily_count(state, daily_date, platform="telegram"):
    d = state.get("daily", {})
    key = "fb_count" if platform == "facebook" else "count"
    return d.get(key, 0) if d.get("date") == daily_date else 0


# --------------------------------------------------------------------- ranking


def score(c, now_utc, wcfg):
    r = wcfg.get("ranking", {})
    w = {"amazon_seller": 3, "days_caught": 2, "recency": 1, **r.get("weights", {})}
    cap = max(1, r.get("days_caught_cap", 7))
    window = max(1, wcfg.get("window_days", 7))
    days = min(len(c.get("days", [])), cap) / cap
    last = datetime.fromisoformat(c["last_seen"])
    age_days = max(0.0, (now_utc - last).total_seconds() / 86400)
    recency = max(0.0, 1 - age_days / window)
    amazon = 1 if c.get("amazon_sold") else 0
    return w["amazon_seller"] * amazon + w["days_caught"] * days + w["recency"] * recency


def select_items(cands, now_local, wcfg):
    """Top products of the last window_days: only verified 'shipped by Amazon'
    items, ranked by the score defined in scanner_config.json -> weekly.ranking.
    Ties break on ASIN so the result is deterministic."""
    now_utc = now_local.astimezone(timezone.utc)
    window = timedelta(days=wcfg.get("window_days", 7))
    pool = []
    for c in cands.values():
        if c.get("shipped_by_amazon") is not True or not c.get("name"):
            continue
        if now_utc - datetime.fromisoformat(c["last_seen"]) > window:
            continue
        pool.append((-score(c, now_utc, wcfg), c["asin"], c))
    pool.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in pool[: wcfg.get("max_items", 7)]]


# --------------------------------------------------------------------- content


def _trim(text, n):
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def hashtag_for(name, categories):
    for c in categories:
        if c["name"] == name:
            return c["hashtag"]
    return cats.default_category(categories)["hashtag"]


def build_digest(items, link_fn, wcfg, categories, handle="", disclosure=True):
    """Numbered list: name + category hashtag + buy link. No price/discount/stock.
    The affiliate disclosure is the LAST line."""
    lines = [wcfg.get("title", "🏆 أفضل عروض الأسبوع"), ""]
    for i, it in enumerate(items, 1):
        link = html_escape(link_fn(it["asin"]), quote=True)
        lines += [
            f"{i}. <b>{html_escape(_trim(it['name'], 90), quote=False)}</b>",
            f"   {hashtag_for(it.get('category'), categories)}",
            f'   🛒 <a href="{link}">لينك الشراء</a>',
            "",
        ]
    if handle:
        lines += [f"📢 {html_escape(handle, quote=False)}", ""]
    if disclosure:   # only when the links really carry the affiliate tag
        lines.append(html_escape(wcfg["disclosure"], quote=False))
    else:
        while lines and lines[-1] == "":
            lines.pop()
    return "\n".join(lines)


def poll_options(config):
    p = config.get("weekly", {}).get("poll", {})
    if p.get("options_source", "categories") == "categories":
        opts = [c["name"] for c in config["categories"]]
    else:
        opts = list(p.get("options", []))
    return opts


def validate_poll(question, options):
    """Conservative Telegram limits (question <=300, option <=100, 2-10 options)."""
    if not (1 <= len(question) <= 300):
        raise ValueError("poll question must be 1-300 chars")
    if not (2 <= len(options) <= 10):
        raise ValueError("poll needs 2-10 options")
    for o in options:
        if not (1 <= len(o) <= 100):
            raise ValueError(f"poll option must be 1-100 chars: {o!r}")


# --------------------------------------------------------------------- clients

ALLOWED_METHODS = {
    "getMe", "getChatMember", "sendMessage", "pinChatMessage",
    "unpinChatMessage", "sendPoll", "stopPoll",
}
FORBIDDEN_KEYS = {"allow_paid_broadcast"}   # paid broadcast is never enabled


class TelegramError(Exception):
    pass


def _guard(method, payload):
    if method not in ALLOWED_METHODS:       # e.g. unpinAllChatMessages is refused
        raise TelegramError(f"method not allowed: {method}")
    bad = FORBIDDEN_KEYS & set(payload)
    if bad:
        raise TelegramError(f"forbidden parameter: {sorted(bad)}")
    if method == "unpinChatMessage" and not payload.get("message_id"):
        # without message_id Telegram unpins the most recent pin: never allow
        raise TelegramError("unpinChatMessage requires an explicit message_id")


class TelegramClient:
    def __init__(self, token, log=print):
        self.token, self.log = token, log

    def bind(self, weekly_state):
        pass

    def call(self, method, payload):
        _guard(method, payload)
        r = requests.post(
            f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=30
        )
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}, not JSON")
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
        return data["result"]


class SimClient:
    """Dry-run client: prints what would be sent, never uses the network."""

    def __init__(self, weekly_state=None, log=print):
        self.ws, self.log = weekly_state, log

    def bind(self, weekly_state):
        self.ws = weekly_state

    def _next(self):
        self.ws["fake_seq"] += 1
        return self.ws["fake_seq"]

    def call(self, method, payload):
        _guard(method, payload)
        shown = {k: v for k, v in payload.items() if k != "text"}
        self.log(f"[weekly][dry-run] would call {method} {json.dumps(shown, ensure_ascii=False)}")
        if method == "getMe":
            return {"id": 1, "is_bot": True}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True,
                    "can_edit_messages": True}
        if method == "sendMessage":
            return {"message_id": self._next()}
        if method == "sendPoll":
            n = self._next()
            return {"message_id": n, "poll": {"id": f"sim-poll-{n}"}}
        if method == "stopPoll":
            cur = self.ws.get("current_poll") or {}
            return {"id": cur.get("poll_id"), "is_closed": True, "total_voter_count": 0,
                    "options": [{"text": t, "voter_count": 0} for t in cur.get("options", [])]}
        return True   # pin / unpin


# ------------------------------------------------------------------------- job

# (name, critical, hard requirements, soft "run after" steps)
STEPS = [
    ("check_permissions", True, (), ()),
    ("send_digest", True, ("check_permissions",), ()),
    ("pin_digest", False, ("send_digest",), ()),
    ("unpin_old_digest", False, ("pin_digest",), ()),
    ("stop_old_poll", False, ("check_permissions",), ()),
    ("send_poll", True, ("send_digest",), ("stop_old_poll",)),
]
NON_IDEMPOTENT = {"send_digest", "send_poll"}   # a blind retry would duplicate
RESOLVED = {"done", "skipped", "gave_up"}


def _new_week(key, sched):
    return {
        "week": key,
        "scheduled_for": sched.isoformat(),
        "status": "in_progress",
        "steps": {n: {"status": "pending", "attempts": 0} for n, *_ in STEPS},
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_weekly(config, settings, *, state_file, live, client, link_fn,
               now=None, handle="", log=print):
    """Run the weekly job if it is due. Returns a short status string."""
    wcfg = config.get("weekly", {})
    if not wcfg.get("enabled", True):
        return "disabled"
    now = now or datetime.now(tz_of(settings.get("timezone")))
    due, sched, key = check_due(now, wcfg, live)
    if not due:
        return "not_due"
    tag = "[weekly]" if live else "[weekly][dry-run]"
    try:
        with botstate.locked(state_file):
            return _run_locked(config, settings, state_file, live, client,
                               link_fn, now, sched, key, handle, log, tag)
    except botstate.LockBusy as e:
        log(f"{tag} skipped: {e}")
        return "locked"


def _run_locked(config, settings, state_file, live, client, link_fn,
                now, sched, key, handle, log, tag):
    wcfg = config["weekly"]
    categories = config["categories"]
    chat = settings.get("channel_id") or settings.get("channel_handle")
    state = botstate.load(state_file)
    ws = state["weekly" if live else "weekly_sim"]
    client.bind(ws)
    wk = ws["weeks"].setdefault(key, _new_week(key, sched))
    protected = {v for v in (wcfg.get("protected_message_ids") or {}).values() if v}

    def save():
        botstate.save(state_file, state)

    if wk["status"] in ("complete", "skipped_insufficient_items"):
        log(f"{tag} {key}: already {wk['status']}, nothing to do")
        return "already_" + wk["status"]
    if wk["status"] == "needs_attention":
        log(f"{tag} {key}: a critical step kept failing; fix the cause and clear this "
            f"week's state to retry (see steps[].error in the state file)")
        return "needs_attention"
    if wk["status"] == "needs_manual_check":
        log(f"{tag} {key}: needs manual check (an earlier send may or may not have "
            f"reached the channel) - not retrying automatically")
        return "needs_manual_check"

    # crash recovery: a step left "in_progress" by a dead run
    for name, st in wk["steps"].items():
        if st["status"] == "in_progress":
            if name in NON_IDEMPOTENT:
                st["status"], wk["status"] = "uncertain", "needs_manual_check"
                save()
                log(f"{tag} {key}: step {name} was interrupted; it may already be "
                    f"in the channel. Check manually, then clear this week's state.")
                return "needs_manual_check"
            st["status"] = "pending"      # safe to repeat (read-only / idempotent)

    # choose and FREEZE the digest content once, so retries post the same text
    if "digest_text" not in wk:
        items = select_items(state["candidates"], now, wcfg)
        if len(items) < wcfg.get("min_items", 5):
            wk["status"] = "skipped_insufficient_items"
            wk["note"] = f"only {len(items)} eligible products"
            save()
            log(f"{tag} {key}: only {len(items)} eligible products "
                f"(need {wcfg.get('min_items', 5)}); nothing posted")
            return "skipped_insufficient_items"
        wk["items"] = [{"asin": i["asin"], "name": i["name"], "category": i["category"]}
                       for i in items]
        affiliate_on = bool(settings.get("affiliate_tag")) and not settings.get("growth_mode")
        wk["digest_text"] = build_digest(items, link_fn, wcfg, categories, handle, affiliate_on)
        save()
    log(f"{tag} {key}: digest text:\n{wk['digest_text']}")

    max_attempts = wcfg.get("max_attempts", 3)
    for name, critical, hard, soft in STEPS:
        st = wk["steps"][name]
        if st["status"] in RESOLVED:
            continue
        # hard dependencies
        stop = False
        for r in hard:
            rs = wk["steps"][r]["status"]
            if rs in ("done", "skipped"):
                continue
            if rs == "gave_up" and not critical:
                st.update(status="skipped", note=f"dependency {r} gave up")
                save()
                stop = True
                break
            log(f"{tag} {key}: {name} waits for {r} ({rs})")
            return "waiting"
        if stop:
            continue
        for r in soft:
            if wk["steps"][r]["status"] not in RESOLVED:
                log(f"{tag} {key}: {name} waits for {r}")
                return "waiting"

        st.update(status="in_progress", attempts=st["attempts"] + 1, ts=_now_iso())
        st.pop("error", None)
        save()
        try:
            outcome = _do_step(name, wk, ws, wcfg, config, chat, client, protected, log, tag)
        except (TelegramError, ValueError) as e:
            st["error"] = str(e)[:300]
            if st["attempts"] >= max_attempts:
                st["status"] = "gave_up" if not critical else "failed"
                if critical:
                    wk["status"] = "needs_attention"
            else:
                st["status"] = "failed"
            save()
            log(f"{tag} {key}: step {name} FAILED ({st['attempts']}/{max_attempts}): {e}")
            if critical or st["status"] == "failed":
                return "step_failed:" + name
            continue
        st.update(status=outcome.get("status", "done"), ts=_now_iso())
        if "note" in outcome:
            st["note"] = outcome["note"]
        save()
        log(f"{tag} {key}: step {name} -> {st['status']}")

    if wk["steps"]["send_digest"]["status"] == "done" and \
            wk["steps"]["send_poll"]["status"] == "done":
        wk["status"] = "complete"
    save()
    return wk["status"]


def _do_step(name, wk, ws, wcfg, config, chat, client, protected, log, tag):
    if name == "check_permissions":
        me = client.call("getMe", {})
        m = client.call("getChatMember", {"chat_id": chat, "user_id": me["id"]})
        missing = []
        if m.get("status") != "administrator":
            missing.append("administrator")
        if not m.get("can_post_messages"):
            missing.append("can_post_messages")
        if wcfg.get("pin", True) and not m.get("can_edit_messages"):
            missing.append("can_edit_messages (needed to pin in a channel)")
        if missing:
            raise TelegramError("bot is missing: " + ", ".join(missing))
        return {}

    if name == "send_digest":
        res = client.call("sendMessage", {
            "chat_id": chat, "text": wk["digest_text"], "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })
        wk["digest_message_id"] = res["message_id"]
        return {}

    if name == "pin_digest":
        if not wcfg.get("pin", True):
            return {"status": "skipped", "note": "pin disabled in config"}
        cur = ws.get("current_digest")
        wk["previous_digest_message_id"] = cur["message_id"] if cur else None
        client.call("pinChatMessage", {
            "chat_id": chat, "message_id": wk["digest_message_id"],
            "disable_notification": True,
        })
        ws["current_digest"] = {"message_id": wk["digest_message_id"], "week": wk["week"]}
        return {}

    if name == "unpin_old_digest":
        prev = wk.get("previous_digest_message_id")
        if not prev:
            return {"status": "skipped", "note": "no previous digest recorded"}
        if prev == wk.get("digest_message_id"):
            return {"status": "skipped", "note": "previous digest is the new one"}
        if prev in protected:
            log(f"{tag} refusing to unpin protected message {prev} (intro/index)")
            return {"status": "skipped", "note": "protected message"}
        client.call("unpinChatMessage", {"chat_id": chat, "message_id": prev})
        return {}

    if name == "stop_old_poll":
        cur = ws.get("current_poll")
        if not cur:
            return {"status": "skipped", "note": "no previous poll recorded"}
        try:
            res = client.call("stopPoll", {"chat_id": chat, "message_id": cur["message_id"]})
        except TelegramError as e:
            if "closed" in str(e).lower():
                res = None
            else:
                raise
        ws["poll_results"].append({
            "week": cur.get("week"), "poll_id": cur.get("poll_id"),
            "message_id": cur["message_id"], "stopped_in_week": wk["week"],
            # counts only: the poll is anonymous and no voter data is kept
            "total_voter_count": (res or {}).get("total_voter_count"),
            "options": [{"text": o["text"], "voter_count": o["voter_count"]}
                        for o in (res or {}).get("options", [])],
        })
        ws["current_poll"] = None
        return {}

    if name == "send_poll":
        p = wcfg.get("poll", {})
        options = poll_options(config)
        question = p.get("question", "تحب أجيب عروض إيه الأسبوع الجاي؟")
        validate_poll(question, options)
        res = client.call("sendPoll", {
            "chat_id": chat, "question": question,
            "options": [{"text": o} for o in options],
            "is_anonymous": True, "type": "regular",
            "allows_multiple_answers": bool(p.get("allows_multiple_answers", True)),
        })
        wk["poll"] = {"message_id": res["message_id"], "poll_id": res["poll"]["id"]}
        ws["current_poll"] = {"message_id": res["message_id"], "poll_id": res["poll"]["id"],
                              "week": wk["week"], "options": options}
        return {}

    raise ValueError(f"unknown step {name}")


def make_client(live, token, log=print):
    return TelegramClient(token, log) if live else SimClient(None, log)
