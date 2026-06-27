"""Auto-switch accounts when usage thresholds are crossed.

Exposes ``cshift``, a lightweight console script intended to be wired
into Claude Code's ``Stop`` hook.  On each turn boundary it checks whether
the active billing block is approaching a configured threshold; when it is,
it delegates the actual account selection and credential swap to
``cswap --switch --strategy best``.

Key design properties
- Fail-open: any error (missing binary, timeout, parse failure) → exit 0.
- Debounced: a file-based cooldown prevents more than one switch per window.
- Cooldown fast-path: cooldown is checked *before* spawning any subprocess.
- Single source of truth: account selection is always delegated to cswap.
- No modification to claude-swap core (switcher.py / credentials.py / cli.py).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_swap.cache import MISSING, read_cache, write_cache
from claude_swap.paths import get_claude_config_home

# Hard timeout for all subprocesses spawned by the guard.
_SUBPROCESS_TIMEOUT = 5  # seconds
# Allow more time for the actual switch (network round-trip to Anthropic usage API).
_SWITCH_TIMEOUT = 20  # seconds

# Default configuration values.
_DEFAULTS: dict = {
    "enabled": True,
    "pct_threshold": 90.0,       # fiveHour.pct from cswap --status --json
    "cost_threshold_usd": None,  # optional: trigger on ccusage cost projection
    "token_threshold": None,     # optional: trigger on ccusage totalTokens
    "cooldown_minutes": 30.0,
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _cooldown_path() -> Path:
    return get_claude_config_home() / ".cshift-cooldown.json"


def _log_path() -> Path:
    return get_claude_config_home() / "cshift.log"


def _config_path() -> Path:
    return get_claude_config_home() / "cshift.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Return merged config: defaults <- file <- env overrides."""
    cfg = dict(_DEFAULTS)

    path = _config_path()
    if path.exists():
        try:
            file_cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(file_cfg, dict):
                cfg.update(file_cfg)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass  # corrupted config -> use defaults

    _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: dict) -> None:
    """Apply CSWAP_GUARD_* env vars onto cfg in place."""
    val = os.environ.get("CSWAP_GUARD_ENABLED")
    if val is not None:
        cfg["enabled"] = val.lower() not in ("0", "false", "no", "off")

    for env_var, key, cast in (
        ("CSWAP_GUARD_PCT", "pct_threshold", float),
        ("CSWAP_GUARD_COST_USD", "cost_threshold_usd", float),
        ("CSWAP_GUARD_TOKENS", "token_threshold", int),
        ("CSWAP_GUARD_COOLDOWN", "cooldown_minutes", float),
    ):
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                cfg[key] = cast(raw)  # type: ignore[operator]
            except (ValueError, TypeError):
                pass


# ---------------------------------------------------------------------------
# Cooldown (backed by cache.py read_cache / write_cache)
# ---------------------------------------------------------------------------

def _is_in_cooldown(cfg: dict) -> bool:
    """Return True if a switch occurred within the configured cooldown window."""
    ttl = float(cfg.get("cooldown_minutes", _DEFAULTS["cooldown_minutes"])) * 60
    result = read_cache(_cooldown_path(), ttl=ttl)
    return result is not MISSING


def _record_cooldown() -> None:
    """Persist a cooldown marker so the next N minutes of checks are skipped."""
    try:
        write_cache(_cooldown_path(), True)
    except OSError:
        pass  # non-fatal


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def read_active_block() -> dict | None:
    """Run ``ccusage blocks --active -j`` and return the active block dict.

    Returns None on any error: binary missing, non-zero exit, timeout, invalid
    JSON, or no active block found.
    """
    try:
        result = subprocess.run(
            ["ccusage", "blocks", "--active", "-j"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for block in data.get("blocks", []):
            if block.get("isActive"):
                return block
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:  # noqa: BLE001
        return None


def read_cswap_status() -> dict | None:
    """Run ``cswap --status --json`` and return the parsed payload.

    Returns None on any error.  Used as the primary (most accurate) signal
    because it reflects the real Anthropic subscription quota.
    """
    try:
        result = subprocess.run(
            ["cswap", "--status", "--json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception:  # noqa: BLE001
        return None


def should_switch(block: dict | None, status: dict | None, cfg: dict) -> bool:
    """Return True when any configured threshold is crossed.

    Trigger logic (any enabled threshold exceeded → True):
    - ``pct_threshold``: ``active.usage.fiveHour.pct`` from cswap status (``current`` accepted as legacy fallback)
    - ``cost_threshold_usd``: ``projection.totalCost`` from ccusage block
    - ``token_threshold``: ``totalTokens`` from ccusage block

    Returns False (fail-open) when all signals are None or data is malformed.
    """
    triggered = False

    # Primary signal: cswap subscription pct (real Anthropic quota).
    # cswap --status --json uses "active" key; accept "current" as a fallback
    # for older schema versions.
    pct_threshold = cfg.get("pct_threshold")
    if pct_threshold is not None and status is not None:
        try:
            account = status.get("active") or status.get("current") or status
            pct = account["usage"]["fiveHour"]["pct"]
            if pct >= pct_threshold:
                triggered = True
        except (KeyError, TypeError):
            pass

    # Corroborating signal: ccusage projected cost.
    cost_threshold = cfg.get("cost_threshold_usd")
    if cost_threshold is not None and block is not None:
        try:
            if block["projection"]["totalCost"] >= cost_threshold:
                triggered = True
        except (KeyError, TypeError):
            pass

    # Corroborating signal: ccusage total tokens.
    token_threshold = cfg.get("token_threshold")
    if token_threshold is not None and block is not None:
        try:
            if block["totalTokens"] >= token_threshold:
                triggered = True
        except (KeyError, TypeError):
            pass

    return triggered


# ---------------------------------------------------------------------------
# Switch action
# ---------------------------------------------------------------------------

def _do_switch() -> bool:
    """Run ``cswap --switch --strategy best --json`` (default-login path).

    Returns True if the switch succeeded or was a no-op (already on best
    account), False on error.
    """
    try:
        result = subprocess.run(
            ["cswap", "--switch", "--strategy", "best", "--json"],
            capture_output=True,
            text=True,
            timeout=_SWITCH_TIMEOUT,
        )
        _append_log(result.stdout, result.stderr, result.returncode)
        if result.returncode == 0:
            _bust_hud_cache()
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        _append_log("", str(exc), 1)
        return False


def _bust_hud_cache() -> None:
    """Invalidate the cshift-hud status cache after a successful switch (best-effort)."""
    binary = shutil.which("cshift-hud")
    if not binary:
        return
    try:
        subprocess.Popen(
            [binary, "--bust"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _append_log(stdout: str, stderr: str, returncode: int) -> None:
    """Append a timestamped JSON line to the guard log (best-effort)."""
    entry = json.dumps({
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "rc": returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    })
    try:
        log_file = _log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cmd_list() -> None:
    """Print account list via cswap --list."""
    try:
        result = subprocess.run(
            ["cswap", "--list"],
            timeout=_SUBPROCESS_TIMEOUT,
        )
        sys.exit(result.returncode)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"cshift: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_status() -> None:
    """Print account status via cswap --status."""
    try:
        result = subprocess.run(
            ["cswap", "--status"],
            timeout=_SUBPROCESS_TIMEOUT,
        )
        sys.exit(result.returncode)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"cshift: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_switch(account: str | None) -> None:
    """Switch accounts via cswap --switch / --switch-to."""
    if account:
        cmd = ["cswap", "--switch-to", account]
    else:
        cmd = ["cswap", "--switch", "--strategy", "best"]
    try:
        result = subprocess.run(cmd, timeout=_SWITCH_TIMEOUT)
        if result.returncode == 0:
            _record_cooldown()
            _bust_hud_cache()
        sys.exit(result.returncode)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"cshift: {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``cshift`` console script.

    When called with no arguments (from the Claude Code Stop hook), always
    exits 0 so the hook never blocks. Manual subcommands exit with the
    underlying cswap return code.
    """
    parser = argparse.ArgumentParser(
        prog="cshift",
        description="Manage Claude Code account switching.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate thresholds and report without switching accounts.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print current usage signals and threshold evaluation, then exit.",
    )
    parser.add_argument(
        "--switch",
        action="store_true",
        help="Switch to the best available account (or use --account N).",
    )
    parser.add_argument(
        "--account",
        metavar="N",
        help="Account number to switch to (used with --switch).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_accounts",
        help="List all configured accounts.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current account usage status.",
    )
    args = parser.parse_args(argv)

    if args.list_accounts:
        _cmd_list()
        return

    if args.status:
        _cmd_status()
        return

    if args.switch or args.account:
        _cmd_switch(args.account)
        return

    try:
        _run(args)
    except Exception:  # noqa: BLE001
        pass  # fail-open: absorb any unexpected error silently
    sys.exit(0)


def _run(args: argparse.Namespace) -> None:  # noqa: C901
    cfg = _load_config()

    if not cfg.get("enabled", True):
        return

    # Fast-path: skip all subprocesses if within the cooldown window.
    if not (args.dry_run or args.check) and _is_in_cooldown(cfg):
        return

    block = read_active_block()
    status = read_cswap_status()
    triggered = should_switch(block, status, cfg)

    if args.check or args.dry_run:
        _print_check(block, status, triggered, cfg, dry_run=args.dry_run)
        return

    if not triggered:
        return

    switched = _do_switch()
    if switched:
        _record_cooldown()


def _print_check(
    block: dict | None,
    status: dict | None,
    triggered: bool,
    cfg: dict,
    *,
    dry_run: bool,
) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}cshift check")
    print(f"  pct_threshold    : {cfg.get('pct_threshold')}%")
    print(f"  cost_threshold   : {cfg.get('cost_threshold_usd')} USD")
    print(f"  token_threshold  : {cfg.get('token_threshold')}")
    print(f"  cooldown         : {cfg.get('cooldown_minutes')} min")

    if block:
        proj = block.get("projection") or {}
        print(
            f"  ccusage block    : totalTokens={block.get('totalTokens')}, "
            f"costUSD={block.get('costUSD', 0.0):.2f}, "
            f"projCost={proj.get('totalCost', 'N/A')}"
        )
    else:
        print("  ccusage block    : unavailable")

    if status:
        try:
            account = status.get("active") or status.get("current") or status
            pct = account["usage"]["fiveHour"]["pct"]
            print(f"  cswap fiveHour   : {pct:.1f}%")
        except (KeyError, TypeError):
            print("  cswap fiveHour   : unavailable (parse error)")
    else:
        print("  cswap status     : unavailable")

    print(f"  decision         : {'SWITCH' if triggered else 'no-op'}")
    if dry_run and triggered:
        print("  [dry-run] would run: cswap --switch --strategy best --json")
