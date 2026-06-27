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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_swap.paths import get_credentials_path

# ---------------------------------------------------------------------------
# ANSI colour constants (matching OMC HUD)
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

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
_OAUTH_CACHE_TTL = 15 * 60  # Reuse OAuth result for 15 minutes before re-fetching
_OAUTH_STALE_TTL = 60 * 60  # Keep stale data for up to 1 hour on 429/errors


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

def _visible_len(s: str) -> int:
    """Return visible character width of *s* (ANSI escape codes stripped)."""
    return len(_ANSI_RE.sub("", s))


def _fit_to_terminal(line: str) -> str:
    """Adapt the status line to the current terminal width.

    Drops lower-priority middle segments (left-to-right) while always keeping
    the first segment (prefix + rate limits) and the last segment (account bar).
    Falls back to the original line when terminal width cannot be determined.
    """
    try:
        width = shutil.get_terminal_size().columns
    except Exception:
        return line
    if width <= 0 or _visible_len(line) <= width:
        return line
    sep = "  |  "
    parts = line.split(sep)
    while len(parts) > 2 and _visible_len(sep.join(parts)) > width:
        parts.pop(1)  # drop leftmost middle segment
    return sep.join(parts)


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


def _read_codex_rate_limits_today() -> dict | None:
    """Read today's Codex rate limits from the most recent JSONL session file.

    Scans ~/.codex/sessions/YYYY/MM/DD/*.jsonl for event_msg/token_count entries
    and returns the last rate_limits dict found (most recent usage snapshot).
    """
    try:
        today = datetime.date.today()
        sessions_dir = (
            Path.home() / ".codex" / "sessions"
            / str(today.year) / f"{today.month:02d}" / f"{today.day:02d}"
        )
        if not sessions_dir.exists():
            return None
        files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            last_rl = None
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "event_msg":
                            payload = d.get("payload", {})
                            if payload.get("type") == "token_count":
                                rl = payload.get("rate_limits")
                                if rl and isinstance(rl.get("primary"), dict):
                                    last_rl = rl
                    except Exception:  # noqa: BLE001
                        pass
            if last_rl:
                return last_rl
    except Exception:  # noqa: BLE001
        pass
    return None


def _render_codex(rate_limits: dict | None) -> str | None:
    """Render Codex 5h rate limit usage from OpenAI Codex JSONL rate_limits data."""
    if not rate_limits:
        return None
    primary = rate_limits.get("primary") or {}
    used_pct = primary.get("used_percent")
    if used_pct is None:
        return None
    pct = float(used_pct)
    color = _RED if pct >= 90 else _YELLOW if pct >= 70 else _GREEN
    pct_str = f"{round(pct)}%"
    resets_at = primary.get("resets_at")
    reset_str = None
    if resets_at:
        try:
            dt = datetime.datetime.fromtimestamp(float(resets_at), tz=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            total_secs = (dt - now).total_seconds()
            if total_secs > 0:
                total_mins = int(total_secs / 60)
                hours = total_mins // 60
                reset_str = f"{hours}h{total_mins % 60}m"
        except Exception:  # noqa: BLE001
            pass
    if reset_str:
        return f"{_DIM}codex:{_RESET}{color}{pct_str}{_RESET}{_DIM}({reset_str}){_RESET}"
    return f"{_DIM}codex:{_RESET}{color}{pct_str}{_RESET}"


def _email_short(email: str) -> str:
    """Extract a compact label from an email address.

    'seungryeol.kim@jocodingax.ai' -> 'jocodingax'
    'contact@surfersclub.org'      -> 'surfersclub'
    """
    try:
        domain = email.split("@", 1)[1]
        parts = domain.split(".")
        return parts[-2] if len(parts) >= 2 else parts[0]
    except Exception:  # noqa: BLE001
        return email


def _render_active_prefix(active_num: int | None, email: str | None, pct: float | None) -> str | None:
    """Render '[#N label]' prefix showing the currently active account."""
    if active_num is None:
        return None
    label = _email_short(email) if email else f"#{active_num}"
    color = _ansi_color(pct)
    return f"{_DIM}[{_RESET}{color}#{active_num} {label}{_RESET}{_DIM}]{_RESET}"


# ---------------------------------------------------------------------------
# OAuth / Anthropic usage API
# ---------------------------------------------------------------------------

def _get_access_token() -> str | None:
    """Read the Claude Code OAuth access token from the active credential store."""
    try:
        creds_path = get_credentials_path()
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


def _token_key(token: str) -> str:
    """Return a short hash of the token for cache keying (not the token itself)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _read_oauth_cache(key: str, *, allow_stale: bool = False) -> dict | None:
    """Return cached OAuth result if it exists and matches key.

    Within TTL: always returned.
    Beyond TTL but within stale window: returned only when allow_stale=True
    (used as fallback on 429 / network errors).
    """
    try:
        raw = json.loads(_OAUTH_CACHE_FILE.read_text(encoding="utf-8"))
        if raw.get("key") != key:
            return None
        age = time.time() - raw.get("ts", 0)
        if age < _OAUTH_CACHE_TTL:
            return raw.get("data")
        if allow_stale and age < _OAUTH_STALE_TTL:
            return raw.get("data")
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_oauth_cache(data: dict, key: str) -> None:
    _ensure_dir()
    try:
        _OAUTH_CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "key": key, "data": data}), encoding="utf-8"
        )
    except OSError:
        pass


def _fetch_oauth_usage() -> dict | None:
    """Fetch 5-hour and weekly usage from api.anthropic.com/api/oauth/usage.

    Cache is keyed by a hash of the active OAuth token so switching accounts
    immediately invalidates the cached data for the previous account.

    Returns dict with five_hour_pct, weekly_pct (0-100 floats), and optional
    five_hour_resets_at / weekly_resets_at ISO strings, or None on failure.
    """
    token = _get_access_token()
    if not token:
        return None

    key = _token_key(token)
    cached = _read_oauth_cache(key)
    if cached is not None:
        return cached

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
            "five_hour_pct": float(fh_util) if fh_util is not None else None,
            "weekly_pct": float(sd_util) if sd_util is not None else None,
            "five_hour_resets_at": fh.get("resets_at"),
            "weekly_resets_at": sd.get("resets_at"),
        }
        _write_oauth_cache(result, key)
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Rate-limited: serve stale cached data rather than showing nothing.
            return _read_oauth_cache(key, allow_stale=True)
        return None
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
# Data fetching — direct library/file access (no CLI subprocess wrapping)
# ---------------------------------------------------------------------------

def _fetch_cswap_data() -> dict | None:
    """Read accounts list and per-account usage directly from claude_swap library.

    Imports ClaudeAccountSwitcher at call time (lazy import, background-only path)
    so the hot path never pays the import cost.
    """
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: PLC0415
        switcher = ClaudeAccountSwitcher()
        accounts_info = switcher._build_accounts_info()
        usages = switcher._collect_usage(accounts_info)
        return switcher._build_list_payload(accounts_info, usages)
    except Exception:  # noqa: BLE001
        return None


def _fetch_ccusage_blocks() -> dict | None:
    """Fetch ccusage billing blocks for API-key account elapsed-% fallback."""
    try:
        proc = subprocess.Popen(
            ["ccusage", "blocks", "--active", "-j"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        stdout, _ = proc.communicate(timeout=_SUBPROCESS_TIMEOUT)
        return json.loads(stdout)
    except Exception:  # noqa: BLE001
        return None


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

    # Fetch all data sources concurrently — no subprocess CLI wrapping.
    with ThreadPoolExecutor(max_workers=4) as executor:
        cswap_future = executor.submit(_fetch_cswap_data)
        oauth_future = executor.submit(_fetch_oauth_usage)
        codex_future = executor.submit(_read_codex_rate_limits_today)
        ccusage_future = executor.submit(_fetch_ccusage_blocks)

    # All futures complete before the 'with' block exits (shutdown waits).
    list_data = cswap_future.result()
    oauth = oauth_future.result()
    codex_rl = codex_future.result()
    ccusage_data = ccusage_future.result()

    if not list_data or not list_data.get("accounts"):
        return "⚪ no accounts"

    active_num = list_data.get("activeAccountNumber")

    # Extract active account identity and subscription quota from list data.
    active_pct: float | None = None
    active_email: str | None = None
    try:
        active_acc = next(
            acc for acc in list_data["accounts"]
            if acc.get("number") == active_num or acc.get("active")
        )
        active_email = active_acc.get("email")
        active_pct = float(active_acc["usage"]["fiveHour"]["pct"])
    except (StopIteration, KeyError, TypeError):
        pass

    # Fallback: elapsed % from ccusage blocks (API-key accounts have no OAuth quota).
    ccusage_pct = _elapsed_pct_from_ccusage(ccusage_data)

    account_parts: list[str] = []
    for acc in list_data["accounts"]:
        num = acc.get("number")
        is_active = num == active_num or (active_num is None and bool(acc.get("active")))
        pct: float | None = None

        if is_active:
            pct = active_pct if active_pct is not None else ccusage_pct
            active_pct = pct  # keep resolved value for prefix coloring
        else:
            try:
                pct = float(acc["usage"]["fiveHour"]["pct"])
            except (KeyError, TypeError):
                pass

        bar = _pct_bar(pct)
        label = f"{pct:.0f}%" if pct is not None else "?"
        star = "*" if is_active else ""
        entry = f"{bar}#{num}{star}:{label}"

        if is_active and oauth:
            fh_pct = oauth.get("five_hour_pct")
            if fh_pct is not None:
                fh = max(0, min(100, round(fh_pct)))
                color = _ansi_color(fh_pct)
                reset_str = _format_reset_time(oauth.get("five_hour_resets_at"))
                if reset_str:
                    entry += f" {_DIM}5H:{_RESET}{color}{fh}%{_RESET}{_DIM}({reset_str}){_RESET}"
                else:
                    entry += f" {_DIM}5H:{_RESET}{color}{fh}%{_RESET}"

        account_parts.append(entry)

    account_bar = "  ".join(account_parts)

    # Parse stdin context and session data.
    ctx_pct: int | None = None
    try:
        used_pct = stdin_data.get("context_window", {}).get("used_percentage")
        if used_pct is not None:
            ctx_pct = int(round(float(used_pct)))
    except Exception:  # noqa: BLE001
        pass

    session_minutes = _get_session_minutes(stdin_data.get("transcript_path"))

    # Assemble the display line.
    prefix = _render_active_prefix(active_num, active_email, active_pct)

    body_parts: list[str] = []
    session_str = _render_session(session_minutes)
    if session_str:
        body_parts.append(session_str)
    ctx_str = _render_context(ctx_pct)
    if ctx_str:
        body_parts.append(ctx_str)
    body_parts.append(account_bar)
    codex_str = _render_codex(codex_rl)
    if codex_str:
        body_parts.append(codex_str)

    body = "  |  ".join(body_parts)
    return (prefix + "  " + body) if prefix else body


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


def bust_cache() -> None:
    """Invalidate status cache and spawn an immediate background refresh.

    Call this after an account switch so the next HUD render shows fresh data.
    """
    try:
        _STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    _spawn_refresh()


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
    parser.add_argument("--bust", action="store_true",
                        help="Invalidate cache and trigger immediate background refresh.")
    args, _ = parser.parse_known_args(argv)

    if args.bust:
        bust_cache()
        return

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
    output = line or "⚪ cshift: loading..."
    sys.stdout.write(_fit_to_terminal(output) + "\n")
    sys.stdout.flush()

    # Kick off background refresh if the cache is stale.
    if _is_stale():
        _spawn_refresh()
