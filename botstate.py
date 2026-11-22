"""Durable bot state (NOT seen.json, which stays untouched).

Two separate files so that dry-run can never change production state:
  bot_state.json         production (only used when everything is live)
  bot_state.dryrun.json  simulation / candidates collected while in dry-run

Writes are atomic (temp file + os.replace) and guarded by a lock file."""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

PROD_FILE = "bot_state.json"
DRY_FILE = "bot_state.dryrun.json"
LOCK_STALE_SECONDS = 30 * 60


class LockBusy(Exception):
    pass


def state_path(base_dir, live):
    return Path(base_dir) / (PROD_FILE if live else DRY_FILE)


def empty_state():
    st = {
        "version": 1,
        "candidates": {},   # asin -> name/category/days/... (never prices)
        "daily": {"date": None, "count": 0, "fb_count": 0},
        "posted": {"telegram": {}, "facebook": {}},   # asin -> ISO time (dry-run too)
        "facebook": {"token_check": None, "alert_date": None},
        "weekly": {
            "weeks": {},              # week_key -> steps/status/message ids
            "current_digest": None,   # {"message_id", "week"}
            "current_poll": None,     # {"message_id", "poll_id", "week"}
            "poll_results": [],       # final stopPoll results, counts only
            "fake_seq": 900000000,    # fake ids used by dry-run simulation
        },
    }
    # simulated weekly runs live in their own section so a dry-run can never
    # mark a real week as done or leave fake message ids behind
    st["weekly_sim"] = json.loads(json.dumps(st["weekly"]))
    return st


def load(path):
    p = Path(path)
    st = empty_state()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"{p.name} is unreadable ({e}); refusing to overwrite it")
        st.update({k: v for k, v in raw.items() if k not in ("weekly", "weekly_sim")})
        st["weekly"].update(raw.get("weekly", {}))
        st["weekly_sim"].update(raw.get("weekly_sim", {}))
    return st


def save(path, state):
    p = Path(path)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


@contextmanager
def locked(path, stale_seconds=LOCK_STALE_SECONDS):
    """Exclusive lock around read-modify-write of one state file."""
    lock = Path(str(path) + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            age = 0
        if age < stale_seconds:
            raise LockBusy(f"{lock.name} is held (age {age:.0f}s)")
        lock.unlink(missing_ok=True)          # stale lock from a dead run
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)
