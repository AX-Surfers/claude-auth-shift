"""
cshift-hud — Claude Code statusLine showing OAuth rate limits, session/context info,
and per-account cswap quota.

Claude Code statusLine protocol:
  - Claude Code pipes JSON to stdin: {"session_id": "...", "transcript_path": "...",
    "context_window": {"used_percentage": 34, ...}, ...}
  - Reads first line of stdout as the status bar text
  - Called on every render; must return quickly

Hot path: print from cache file immediately, exit.
Background: refresh data when cache is stale (default TTL: 30 s).

Output format (ANSI colours):
  5h:GREEN84%DIM(4h7m) wk:GREEN11%  |  session:GREENXm | ctx:GREENX%  |  🟢#1*:30%  🟢#2:0%

Setup (~/.claude/settings.json):
    {
      "statusLine": {"type": "command", "command": "cshift-hud"}
    }

Config env vars:
    CSHIFT_HUD_TTL          Refresh interval in seconds (default: 30)
    CSHIFT_HUD_CACHE_DIR    Override cache directory
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI colour constants (matching OMC HUD)
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TTL = float(os.environ.get("CSHIFT_HUD_TTL", "30"))
_SUBPROCESS_TIMEOUT = 5  # seconds per cswap call
_OAUTH_TIMEOUT = 8        # seconds for OAuth API call
_LOCK_STALE_SECS = 60

_env_cache = os.environ.get("CSHIFT_HUD_CACHE_DIR")
_CACHE_DIR: Path = Path(_env_cache) if _env_cache else Path.home() / ".claude" / "cshift-hud"
_STATUS_FILE: Path = _CACHE_DIR / "status.txt"
_LOCK_FILE: Path = _CACHE_DIR / "refresh.lock"
_STDIN_CACHE_FILE: Path = _CACHE_DIR / "stdin_cache.json"
_OAUTH_CACHE_FILE: Path = _CACHE_DIR / "oauth_cache.json"

_BLOCK_MINUTES = 5 * 60  # Claude Code's 5-hour billing block
_OAUTH_CACHE_TTL = 5 * 60  # Reuse OAuth result for 5 minutes before re-fetching


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _read_cache() -> str:
    try:
        return _STATUS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_cache(line: str) -> None:
    _ensure_dir()
    try:
        _STATUS_FILE.write_text(line + "\n", encoding="utf-8")
    except OSError:
        pass


def _is_stale() -> bool:
    try:
        return time.time() - _STATUS_FILE.stat().st_mtime > _TTL
    except OSError:
        return True


def _save_stdin_cache(data: dict) -> None:
    _ensure_dir()
    try:
        _STDIN_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _load_stdin_cache() -> dict:
    try:
        return json.loads(_STDIN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct_bar(pct: float | None) -> str:
    """Colour emoji for fiveHour usage percentage (used in account bar)."""
    if pct is None:
        return "⚪"
    if pct >= 90:
        return "🔴"
    if pct >= 70:
        return "🟡"
    return "🟢"


def _ansi_color(pct: float | None) -> str:
    """ANSI colour code for a percentage value."""
    if pct is None:
        return _DIM
    if pct >= 90:
        return _RED
    if pct >= 70:
        return _YELLOW
    return _GREEN


def _format_reset_time(resets_at_iso: str | None) -> str | None:
    """Format an ISO timestamp as a human-readable countdown.

    Returns '3h42m', '2d5h', or None if the timestamp is in the past/invalid.
    """
    if not resets_at_iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        total_secs = (dt - now).total_seconds()
        if total_secs <= 0:
            return None
        total_mins = int(total_secs / 60)
        hours = total_mins // 60
        days = hours // 24
        if days > 0:
            return f"{days}d{hours % 24}h"
        return f"{hours}h{total_mins % 60}m"
    except Exception:  # noqa: BLE001
        return None


def _render_limits(oauth: dict | None) -> str | None:
    """Render 5h/wk limits in OMC HUD style with ANSI colours.

    Returns None when oauth data is unavailable.
    """
    if not oauth:
        return None
    fh_pct = oauth.get("five_hour_pct")
    if fh_pct is None:
        return None

    fh = max(0, min(100, round(fh_pct)))
    color = _ansi_color(fh_pct)
    reset_str = _format_reset_time(oauth.get("five_hour_resets_at"))
    if reset_str:
        fh_part = f"5h:{color}{fh}%{_RESET}{_DIM}({reset_str}){_RESET}"
    else:
        fh_part = f"5h:{color}{fh}%{_RESET}"

    parts = [fh_part]

    wk_pct = oauth.get("weekly_pct")
    if wk_pct is not None:
        wk = max(0, min(100, round(wk_pct)))
        wk_color = _ansi_color(wk_pct)
        wk_reset = _format_reset_time(oauth.get("weekly_resets_at"))
        if wk_reset:
            parts.append(f"{_DIM}wk:{_RESET}{wk_color}{wk}%{_RESET}{_DIM}({wk_reset}){_RESET}")
        else:
            parts.append(f"{_DIM}wk:{_RESET}{wk_color}{wk}%{_RESET}")

    return " ".join(parts)


def _render_session(minutes: int | None) -> str | None:
    """Render session duration with ANSI colour."""
    if minutes is None:
        return None
    color = _RED if minutes > 120 else _YELLOW if minutes > 60 else _GREEN
    return f"session:{color}{minutes}m{_RESET}"


def _render_context(pct: int | None) -> str | None:
    """Render context window usage with ANSI colour."""
    if pct is None:
        return None
    color = _RED if pct >= 90 else _YELLOW if pct >= 70 else _GREEN
    return f"ctx:{color}{pct}%{_RESET}"


# ---------------------------------------------------------------------------
# OAuth / Anthropic usage API
# ---------------------------------------------------------------------------

def _get_access_token() -> str | None:
    """Read the Claude Code OAuth access token from ~/.claude/.credentials.json."""
    try:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            return None
        expires_at = oauth.get("expiresAt")
        if expires_at and float(expires_at) <= time.time() * 1000:
            return None
        return token
    except Exception:  # noqa: BLE001
        return None


def _read_oauth_cache() -> dict | None:
    """Return cached OAuth result if it exists and is within TTL."""
    try:
        raw = json.loads(_OAUTH_CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - raw.get("ts", 0) < _OAUTH_CACHE_TTL:
            return raw.get("data")
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_oauth_cache(data: dict) -> None:
    _ensure_dir()
    try:
        _OAUTH_CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "data": data}), encoding="utf-8"
        )
    except OSError:
        pass


def _fetch_oauth_usage() -> dict | None:
    """Fetch 5-hour and weekly usage from api.anthropic.com/api/oauth/usage.

    Returns a cached result within the TTL; makes a live API call otherwise.
    Returns dict with five_hour_pct, weekly_pct (0-100 floats), and optional
    five_hour_resets_at / weekly_resets_at ISO strings, or None on failure.
    """
    cached = _read_oauth_cache()
    if cached is not None:
        return cached

    token = _get_access_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=_OAUTH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}
        fh_util = fh.get("utilization")
        sd_util = sd.get("utilization")
        result = {
            "five_hour_pct": float(fh_util) * 100 if fh_util is not None else None,
            "weekly_pct": float(sd_util) * 100 if sd_util is not None else None,
            "five_hour_resets_at": fh.get("resets_at"),
            "weekly_resets_at": sd.get("resets_at"),
        }
        _write_oauth_cache(result)
        return result
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_session_minutes(transcript_path: str | None) -> int | None:
    """Compute session duration in minutes from transcript first timestamp."""
    if not transcript_path:
        return None
    try:
        p = Path(transcript_path)
        if not p.exists():
            return None
        first_ts: str | None = None
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = entry.get("timestamp")
                if ts:
                    first_ts = ts
                    break
        if not first_ts:
            return None
        start = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, int((now - start).total_seconds() / 60))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# cswap data fetching
# ---------------------------------------------------------------------------

def _elapsed_pct_from_ccusage(ccusage_data: dict | None) -> float | None:
    """Compute % of the active 5-hour block elapsed from ccusage remainingMinutes.

    Used as a fallback for API-key accounts that have no subscription quota.
    Returns None when ccusage data is unavailable.
    """
    try:
        blocks = (ccusage_data or {}).get("blocks") or []
        block = next((b for b in blocks if b.get("isActive")), None)
        if block is None and blocks:
            block = blocks[0]
        if block is None:
            return None
        remaining = (block.get("projection") or {}).get("remainingMinutes")
        if remaining is None:
            return None
        elapsed = _BLOCK_MINUTES - float(remaining)
        return max(0.0, min(100.0, elapsed / _BLOCK_MINUTES * 100))
    except Exception:  # noqa: BLE001
        return None


def _build_status_line(stdin_data: dict | None = None) -> str:
    """Fetch cswap, ccusage, and OAuth data; return the full formatted status line."""
    stdin_data = stdin_data or {}

    # Spawn subprocess calls concurrently
    try:
        list_proc = subprocess.Popen(
            ["cswap", "--list", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:  # noqa: BLE001
        return "⚪ no accounts"
    try:
        status_proc: subprocess.Popen[str] | None = subprocess.Popen(
            ["cswap", "--status", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:  # noqa: BLE001
        status_proc = None
    try:
        ccusage_proc: subprocess.Popen[str] | None = subprocess.Popen(
            ["ccusage", "blocks", "--active", "-j"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:  # noqa: BLE001
        ccusage_proc = None

    # Fetch OAuth usage in a background thread (parallel with subprocess waits)
    oauth_result: list[dict | None] = [None]

    def _oauth_worker() -> None:
        oauth_result[0] = _fetch_oauth_usage()

    oauth_thread = threading.Thread(target=_oauth_worker, daemon=True)
    oauth_thread.start()

    # Collect subprocess results
    list_data: dict | None = None
    status_data: dict | None = None
    ccusage_data: dict | None = None
    for proc, key in (
        (list_proc, "list"),
        (status_proc, "status"),
        (ccusage_proc, "ccusage"),
    ):
        if proc is None:
            continue
        try:
            stdout, _ = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
            data = json.loads(stdout)
            if key == "list":
                list_data = data
            elif key == "status":
                status_data = data
            else:
                ccusage_data = data
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Wait for OAuth thread
    oauth_thread.join(timeout=_OAUTH_TIMEOUT + 1)
    oauth = oauth_result[0]

    if not list_data or not list_data.get("accounts"):
        return "⚪ no accounts"

    active_num = list_data.get("activeAccountNumber")

    # Priority 1: subscription quota % from cswap --status (OAuth accounts)
    active_pct: float | None = None
    try:
        a = ((status_data or {}).get("active") or {})
        fh = (a.get("usage") or {}).get("fiveHour") or {}
        v = fh.get("pct")
        if v is not None:
            active_pct = float(v)
    except Exception:
        pass

    # Priority 2: 5h block elapsed % from ccusage (API-key accounts fallback)
    ccusage_pct = _elapsed_pct_from_ccusage(ccusage_data)

    account_parts: list[str] = []
    for acc in list_data["accounts"]:
        num = acc.get("number")
        is_active = num == active_num
        pct: float | None = None

        if is_active:
            pct = active_pct
            if pct is None:
                try:
                    pct = float(acc["usage"]["fiveHour"]["pct"])
                except (KeyError, TypeError):
                    pass
            if pct is None:
                pct = ccusage_pct
        else:
            try:
                pct = float(acc["usage"]["fiveHour"]["pct"])
            except (KeyError, TypeError):
                pass

        bar = _pct_bar(pct)
        label = f"{pct:.0f}%" if pct is not None else "?"
        star = "*" if is_active else ""
        account_parts.append(f"{bar}#{num}{star}:{label}")

    account_bar = "  ".join(account_parts)

    # Parse stdin context and session data
    ctx_pct: int | None = None
    try:
        used_pct = stdin_data.get("context_window", {}).get("used_percentage")
        if used_pct is not None:
            ctx_pct = int(round(float(used_pct)))
    except Exception:
        pass

    session_minutes = _get_session_minutes(stdin_data.get("transcript_path"))

    # Assemble the display line
    meta_parts: list[str] = []
    limits_str = _render_limits(oauth)
    if limits_str:
        meta_parts.append(limits_str)
    session_str = _render_session(session_minutes)
    if session_str:
        meta_parts.append(session_str)
    ctx_str = _render_context(ctx_pct)
    if ctx_str:
        meta_parts.append(ctx_str)

    if meta_parts:
        return "  |  ".join(meta_parts) + "  |  " + account_bar
    return account_bar


# ---------------------------------------------------------------------------
# Refresh worker
# ---------------------------------------------------------------------------

def _acquire_lock() -> bool:
    """Atomic lock acquisition; clears stale locks first. Returns True if acquired."""
    _ensure_dir()
    try:
        if _LOCK_FILE.exists():
            if time.time() - _LOCK_FILE.stat().st_mtime > _LOCK_STALE_SECS:
                _LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except OSError:
        return False


def _release_lock() -> None:
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _refresh() -> None:
    """Background worker: fetch all data and update the status cache."""
    if not _acquire_lock():
        return
    try:
        stdin_data = _load_stdin_cache()
        line = _build_status_line(stdin_data)
        _write_cache(line)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _release_lock()


def _spawn_refresh() -> None:
    """Detach a background refresh process so the hot path returns immediately."""
    binary = shutil.which("cshift-hud")
    cmd = [binary, "--refresh"] if binary else [sys.executable, "-m", "claude_swap.hud", "--refresh"]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``cshift-hud`` console script."""
    parser = argparse.ArgumentParser(prog="cshift-hud", add_help=False)
    parser.add_argument("--refresh", action="store_true",
                        help="Run background data refresh (internal use).")
    args, _ = parser.parse_known_args(argv)

    if args.refresh:
        _refresh()
        return

    # Consume and cache stdin — Claude Code sends session JSON here.
    stdin_data: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            stdin_data = json.loads(raw)
    except Exception:  # noqa: BLE001
        pass

    _save_stdin_cache(stdin_data)

    # Hot path: output cached value immediately.
    line = _read_cache()
    sys.stdout.write((line or "⚪ cshift: loading...") + "\n")
    sys.stdout.flush()

    # Kick off background refresh if the cache is stale.
    if _is_stale():
        _spawn_refresh()
