"""Facebook Page publisher (Graph API). Dry-run by default.

Secrets come from the environment ONLY (never from code / the repo):
  FB_PAGE_ID  FB_PAGE_ACCESS_TOKEN  FB_APP_ID  FB_APP_SECRET
  FB_DRY_RUN  (default "true"; only "false"/"0"/"no" turns live posting on)
Tokens are sent in the Authorization header and scrubbed from every log line.
One attempt per deal: a failed/unclear post is never retried (no double posts)."""
import os

import requests

GRAPH = "https://graph.facebook.com"
AUTH_ERROR_CODES = {190}          # invalid / expired / revoked token


def creds_from_env(env=None):
    env = os.environ if env is None else env
    return {
        "page_id": env.get("FB_PAGE_ID", "").strip(),
        "page_token": env.get("FB_PAGE_ACCESS_TOKEN", "").strip(),
        "app_id": env.get("FB_APP_ID", "").strip(),
        "app_secret": env.get("FB_APP_SECRET", "").strip(),
    }


def creds_complete(creds, need_app=False):
    keys = ["page_id", "page_token"] + (["app_id", "app_secret"] if need_app else [])
    return all(creds.get(k) for k in keys)


def fb_dry_run(env=None):
    env = os.environ if env is None else env
    return str(env.get("FB_DRY_RUN", "true")).strip().lower() not in ("false", "0", "no")


def _base(settings):
    return f"{GRAPH}/{settings.get('fb_graph_version', 'v27.0')}"


def scrub(text, creds):
    out = str(text)
    for k in ("page_token", "app_secret", "app_id"):
        v = (creds or {}).get(k)
        if v:
            out = out.replace(v, "***")
    if creds and creds.get("app_id") and creds.get("app_secret"):
        out = out.replace(f"{creds['app_id']}|{creds['app_secret']}", "***")
    return out


def _error_of(resp):
    try:
        return (resp.json() or {}).get("error") or {}
    except ValueError:
        return {}


def post_deal_to_facebook(deal, text, settings, creds=None, image_path=None,
                          dry_run=True, log=print, image_bytes=None):
    """Publish ONE deal. Returns 'dry-run' | 'photo' | 'feed' | 'token_error' | 'failed'."""
    image_url = deal.get("image")
    if dry_run:
        log("[facebook][dry-run] would post "
            f"{'photo' if (image_path or image_url) else 'feed'} to the page "
            f"(image={'yes' if (image_path or image_url) else 'no'}):\n{text}")
        return "dry-run"

    creds = creds or {}
    if not creds_complete(creds):
        log("[facebook] FAILED: FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN missing")
        return "failed"
    headers = {"Authorization": f"Bearer {creds['page_token']}"}
    base, page = _base(settings), creds["page_id"]

    def call(path, data, files=None):
        try:
            return requests.post(f"{base}/{page}/{path}", data=data, files=files,
                                 headers=headers, timeout=45)
        except requests.RequestException as e:
            log(f"[facebook] {path} network error: {scrub(e, creds)}")
            return None

    if image_bytes or image_path or image_url:
        if image_bytes:
            r = call("photos", {"caption": text},
                     files={"source": ("deal.jpg", image_bytes, "image/jpeg")})
        elif image_path:
            with open(image_path, "rb") as f:
                r = call("photos", {"caption": text}, files={"source": f})
        else:
            r = call("photos", {"caption": text, "url": image_url})
        if r is None:                      # timeout: may or may not have posted -> NO retry
            return "failed"
        if r.ok and r.json().get("id"):
            return "photo"
        err = _error_of(r)
        if err.get("code") in AUTH_ERROR_CODES:
            log("[facebook] token rejected (code 190)")
            return "token_error"
        log(f"[facebook] photo post failed: {scrub(err.get('message', r.status_code), creds)}")
        if not err:                        # no clear API verdict -> don't risk a double post
            return "failed"
        # a clear API error (e.g. bad image): fall through to ONE feed post below

    r = call("feed", {"message": text, "link": deal.get("link", "")})
    if r is None:
        return "failed"
    if r.ok and r.json().get("id"):
        return "feed"
    err = _error_of(r)
    if err.get("code") in AUTH_ERROR_CODES:
        return "token_error"
    log(f"[facebook] feed post failed: {scrub(err.get('message', r.status_code), creds)}")
    return "failed"


def validate_facebook_token(creds, settings, dry_run=True, log=print):
    """debug_token check. Returns {"valid": bool, "error_code": int|None,
    "simulated": bool, "missing_scopes": [..]}. Call once per day before posting."""
    if dry_run:
        log("[facebook][dry-run] would validate the page token with debug_token")
        return {"valid": True, "error_code": None, "simulated": True, "missing_scopes": []}
    if not creds_complete(creds, need_app=True):
        return {"valid": False, "error_code": None, "simulated": False,
                "missing_scopes": [], "reason": "missing credentials"}
    try:
        r = requests.get(
            f"{_base(settings)}/debug_token",
            # documented form: app access token = APP_ID|APP_SECRET (GitHub masks
            # secrets in logs, and scrub() removes them from our own messages)
            params={"input_token": creds["page_token"],
                    "access_token": f"{creds['app_id']}|{creds['app_secret']}"},
            timeout=30,
        )
    except requests.RequestException as e:
        log(f"[facebook] debug_token network error: {scrub(e, creds)}")
        # unknown, not proven invalid: do not disable the platform on a network blip
        return {"valid": True, "error_code": None, "simulated": False,
                "missing_scopes": [], "reason": "network error, assumed valid"}
    err = _error_of(r)
    try:
        data = (r.json().get("data") if r.ok else None) or {}
    except ValueError:
        data = {}
    if not r.ok or not data:
        # The CHECK itself failed (wrong request, bad app credentials, outage...).
        # That is not proof that the page token is bad: only code 190 is. For
        # anything else keep going; the post itself returns 190 if the token is bad.
        code = err.get("code")
        msg = scrub(err.get("message", f"HTTP {r.status_code}"), creds)
        if code in AUTH_ERROR_CODES:
            return {"valid": False, "error_code": code, "simulated": False,
                    "missing_scopes": [], "reason": msg}
        return {"valid": True, "error_code": code, "simulated": False, "missing_scopes": [],
                "reason": f"debug_token could not be used (code {code}: {msg}); "
                          f"not treated as invalid"}
    inner = data.get("error") or {}
    code = inner.get("code")
    valid = bool(data.get("is_valid")) and code not in AUTH_ERROR_CODES
    need = {"pages_manage_posts", "pages_read_engagement", "pages_show_list"}
    scopes = set(data.get("scopes") or [])
    missing = sorted(need - scopes) if scopes else []
    reason = None if valid else scrub(inner.get("message", "is_valid=false"), creds)
    # posting needs a PAGE token whose profile is the page; a USER token is a common mix-up
    ttype = str(data.get("type") or "").upper()
    if valid and ttype and ttype != "PAGE":
        valid, reason = False, (
            f"FB_PAGE_ACCESS_TOKEN is a {ttype} token; a PAGE token is required "
            f"(copy the page's access_token from me/accounts)")
    elif valid and data.get("profile_id") and str(data["profile_id"]) != str(creds.get("page_id")):
        valid, reason = False, (
            f"token belongs to profile {data['profile_id']}, not FB_PAGE_ID {creds.get('page_id')}")
    return {"valid": valid, "error_code": code, "simulated": False,
            "missing_scopes": missing, "reason": reason}
