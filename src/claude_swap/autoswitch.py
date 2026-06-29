"""cshift — Claude Code account switcher and manager.

Serves three roles:
  1. Stop-hook auto-switcher: runs on every Claude Code turn end, switches
     accounts when usage thresholds are crossed.
  2. Account manager: full CRUD for managed accounts (add, remove, list,
     export, import, TUI, purge, upgrade) via ClaudeAccountSwitcher directly.
  3. Status signal source: read_cshift_status() used by cshift-hud.

Key design properties (stop-hook path):
- Fail-open: any error → exit 0; the hook never blocks Claude Code.
- Debounced: file-based cooldown prevents more than one switch per window.
- Cooldown fast-path: checked before any library or subprocess call.
- Native: all operations call ClaudeAccountSwitcher directly; no cshift subprocess.
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

# Hard timeout for ccusage subprocess (the only external binary we still call).
_SUBPROCESS_TIMEOUT = 5  # seconds
# Allow more time for the actual switch (network round-trip to Anthropic usage API).
_SWITCH_TIMEOUT = 20  # seconds

# Default configuration values.
_DEFAULTS: dict = {
    "enabled": True,
    "pct_threshold": 90.0,       # fiveHour.pct from account status
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
    val = os.environ.get("CSHIFT_GUARD_ENABLED")
    if val is not None:
        cfg["enabled"] = val.lower() not in ("0", "false", "no", "off")

    for env_var, key, cast in (
        ("CSHIFT_GUARD_PCT", "pct_threshold", float),
        ("CSHIFT_GUARD_COST_USD", "cost_threshold_usd", float),
        ("CSHIFT_GUARD_TOKENS", "token_threshold", int),
        ("CSHIFT_GUARD_COOLDOWN", "cooldown_minutes", float),
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


def read_cshift_status() -> dict | None:
    """Read current account status via ClaudeAccountSwitcher library.

    Returns None on any error. Used as the primary signal for threshold
    checks because it reflects the real Anthropic subscription quota.
    """
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: PLC0415
        return ClaudeAccountSwitcher().status(json_output=True)
    except Exception:  # noqa: BLE001
        return None


def should_switch(block: dict | None, status: dict | None, cfg: dict) -> bool:
    """Return True when any configured threshold is crossed.

    Trigger logic (any enabled threshold exceeded → True):
    - ``pct_threshold``: ``active.usage.fiveHour.pct`` from account status
    - ``cost_threshold_usd``: ``projection.totalCost`` from ccusage block
    - ``token_threshold``: ``totalTokens`` from ccusage block

    Returns False (fail-open) when all signals are None or data is malformed.
    """
    triggered = False

    # Primary signal: subscription pct (real Anthropic quota).
    # Accept "active" or "current" key for schema compatibility.
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
# Switch action (stop-hook path)
# ---------------------------------------------------------------------------

def _do_switch() -> bool:
    """Switch to the best account via ClaudeAccountSwitcher library.

    Returns True if the switch succeeded or was a no-op, False on error.
    Captures output silently (stop-hook must not write to stdout/stderr).
    """
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: PLC0415
        payload = ClaudeAccountSwitcher().switch(strategy="best", json_output=True)
        _append_log(json.dumps(payload) if payload else "", "", 0)
        _bust_hud_cache()
        return True
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
# `cshift run <account>` subcommand
# ---------------------------------------------------------------------------

def _run_command(argv: list[str]) -> None:
    """Handle `cshift run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built. On POSIX this execs claude
    and never returns; on Windows it exits with claude's return code.
    """
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1:]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog="cshift run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cshift run 2
  cshift run user@example.com
  cshift run 2 --no-share
  cshift run 2 -- --resume
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL", help="Account to run (number or email)")
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents "
            "from ~/.claude into the session profile"
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(head)

    try:
        from claude_swap.exceptions import ClaudeSwitchError  # noqa: PLC0415
        from claude_swap.session import SessionManager  # noqa: PLC0415
        from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: PLC0415

        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32" and os.geteuid() == 0 and not switcher._is_running_in_container():
            print("Error: Do not run as root (unless in a container)", file=sys.stderr)
            sys.exit(1)
        SessionManager(switcher).run(args.account, tail, share=not args.no_share)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:  # noqa: C901
    """Entry point for the ``cshift`` console script.

    When called with no arguments (from the Claude Code Stop hook), runs the
    auto-switch logic and always exits 0. When called with management flags,
    dispatches to ClaudeAccountSwitcher and exits with the appropriate code.
    """
    effective = sys.argv[1:] if argv is None else list(argv)
    if effective and effective[0] == "run":
        _run_command(effective[1:])
        return  # only reachable in tests where exec/exit is mocked

    parser = argparse.ArgumentParser(
        prog="cshift",
        description="Claude Code account switcher and manager.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cshift                                    # stop-hook: auto-switch when usage is high
  cshift --check                            # inspect thresholds without switching
  cshift --switch                           # manually rotate to next account
  cshift --switch --strategy best           # switch to account with most quota
  cshift --switch-to 2                      # switch to account #2
  cshift --switch-to user@example.com       # switch by email
  cshift --add-account                      # register current Claude session
  cshift --add-token sk-ant-oat01-...       # register OAuth setup-token
  cshift --add-token sk-ant-api03-...       # register managed API key
  cshift --add-token - --slot 3             # read token from stdin
  cshift --list                             # list managed accounts
  cshift --status                           # show current account usage
  cshift --remove-account 2                 # remove account #2
  cshift --export backup.cswap              # export all accounts
  cshift --import backup.cswap              # import accounts
  cshift --tui                              # interactive arrow-key menu
  cshift --purge                            # remove all cshift/claude-swap data
  cshift --upgrade                          # self-upgrade to latest version
  cshift run 2                              # run account 2 in this terminal only
  cshift run 2 -- --resume                  # forward args after '--' to claude
        """,
    )

    # --- Global flags ---
    # Lazy import __version__ only when needed (stop-hook fast path skips this).
    try:
        from claude_swap import __version__ as _v  # noqa: PLC0415
        _ver_str = f"%(prog)s {_v}"
    except Exception:  # noqa: BLE001
        _ver_str = "%(prog)s"
    parser.add_argument("--version", action="version", version=_ver_str)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate thresholds and report without switching (stop-hook mode).")
    parser.add_argument("--check", action="store_true",
                        help="Print current usage signals and threshold evaluation, then exit.")

    # --- Switch flags ---
    parser.add_argument("--switch", action="store_true",
                        help="Rotate to the next (or best) account.")
    parser.add_argument("--switch-to", metavar="NUM|EMAIL",
                        help="Switch to a specific account by number or email.")
    parser.add_argument("--strategy", choices=["best", "next-available"],
                        metavar="{best,next-available}",
                        help="Account selection strategy for --switch.")

    # --- Account management flags ---
    parser.add_argument("--add-account", action="store_true",
                        help="Add current Claude account to managed accounts.")
    parser.add_argument("--add-token", metavar="TOKEN", nargs="?", const="",
                        help="Register an OAuth setup-token or API key. Pass '-' to read from stdin.")
    parser.add_argument("--remove-account", metavar="NUM|EMAIL",
                        help="Remove account by number or email.")
    parser.add_argument("--slot", metavar="N", type=int,
                        help="Slot number for --add-account or --add-token.")
    parser.add_argument("--email", metavar="EMAIL",
                        help="Email address for --add-token.")

    # --- List / status flags ---
    parser.add_argument("--list", action="store_true", dest="list_accounts",
                        help="List all managed accounts.")
    parser.add_argument("--token-status", action="store_true",
                        help="Show OAuth token expiry state (use with --list).")
    parser.add_argument("--status", action="store_true",
                        help="Show current account usage status.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON (use with --list, --status, --switch, --switch-to).")

    # --- Export / import flags ---
    parser.add_argument("--export", metavar="PATH",
                        help="Export accounts to file (use '-' for stdout).")
    parser.add_argument("--import", dest="import_", metavar="PATH",
                        help="Import accounts from file (use '-' for stdin).")
    parser.add_argument("--account", metavar="NUM|EMAIL",
                        help="Limit export to one account (use with --export).")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing accounts during --import.")
    parser.add_argument("--full", action="store_true",
                        help="Include full ~/.claude.json in --export (default: oauthAccount only).")

    # --- Other ---
    parser.add_argument("--tui", action="store_true",
                        help="Launch interactive arrow-key account menu.")
    parser.add_argument("--purge", action="store_true",
                        help="Remove all claude-swap data from the system.")
    parser.add_argument("--upgrade", action="store_true",
                        help="Upgrade claude-swap to the latest version on PyPI.")

    args = parser.parse_args(argv)

    # --- Validation ---
    if args.token_status and not args.list_accounts:
        parser.error("--token-status can only be used with --list")
    if args.json and not (args.list_accounts or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with --list, --status, --switch, or --switch-to")
    if args.json and args.token_status:
        parser.error("--token-status cannot be combined with --json")
    if args.strategy is not None and not (args.switch or args.switch_to):
        parser.error("--strategy can only be used with --switch or --switch-to")
    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with --add-account or --add-token")
    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with --add-token")
    if args.account is not None and not args.export:
        parser.error("--account can only be used with --export")
    if args.force and not args.import_:
        parser.error("--force can only be used with --import")
    if args.full and not args.export:
        parser.error("--full can only be used with --export")
    if args.export and args.import_:
        parser.error("--export and --import are not allowed together")

    # --- Determine whether this is an explicit management command ---
    is_management = bool(
        args.upgrade or args.list_accounts or args.status or
        args.add_account or args.add_token is not None or args.remove_account or
        args.switch or args.switch_to or args.export or args.import_ or
        args.tui or args.purge
    )

    if is_management:
        # Lazy imports: keep stop-hook startup path free of heavy modules.
        from claude_swap import __version__  # noqa: PLC0415
        from claude_swap.exceptions import ClaudeSwitchError  # noqa: PLC0415
        from claude_swap.json_output import error_envelope  # noqa: PLC0415
        from claude_swap.printer import dimmed, error, muted  # noqa: PLC0415
        from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: PLC0415

        # Self-upgrade runs before switcher init.
        if args.upgrade:
            from claude_swap.update_check import run_self_upgrade  # noqa: PLC0415
            try:
                sys.exit(run_self_upgrade())
            except KeyboardInterrupt:
                print(f"\n{dimmed('Upgrade cancelled')}")
                sys.exit(130)

        payload: dict | None = None
        try:
            switcher = ClaudeAccountSwitcher(debug=args.debug)
            if sys.platform != "win32" and os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run as root (unless in a container)")
                sys.exit(1)

            if args.add_account:
                switcher.add_account(slot=args.slot)
                _bust_hud_cache()
            elif args.add_token is not None:
                switcher.add_account_from_token(
                    token=args.add_token, email=args.email, slot=args.slot
                )
                _bust_hud_cache()
            elif args.remove_account:
                switcher.remove_account(args.remove_account)
                _bust_hud_cache()
            elif args.list_accounts:
                payload = switcher.list_accounts(
                    show_token_status=args.token_status,
                    json_output=args.json,
                )
            elif args.switch or args.switch_to:
                if args.switch_to:
                    payload = switcher.switch_to(args.switch_to, json_output=args.json)
                else:
                    payload = switcher.switch(strategy=args.strategy, json_output=args.json)
                _record_cooldown()
                _bust_hud_cache()
            elif args.status:
                payload = switcher.status(json_output=args.json)
            elif args.export:
                from claude_swap.transfer import export_accounts  # noqa: PLC0415
                export_accounts(switcher, args.export, account=args.account, full=args.full)
            elif args.import_:
                from claude_swap.transfer import import_accounts  # noqa: PLC0415
                import_accounts(switcher, args.import_, force=args.force)
            elif args.tui:
                try:
                    from claude_swap.tui import run as tui_run  # noqa: PLC0415
                except ImportError:
                    error(
                        "TUI mode requires the 'curses' module. "
                        "On Windows, install with: pip install windows-curses"
                    )
                    sys.exit(1)
                sys.exit(tui_run(switcher))
            elif args.purge:
                switcher.purge()

        except ClaudeSwitchError as exc:
            if args.json:
                print(json.dumps(error_envelope(exc), indent=2))
            else:
                error(f"Error: {exc}")
            sys.exit(1)
        except KeyboardInterrupt:
            print(
                f"\n{dimmed('Operation cancelled')}",
                file=sys.stderr if args.json else sys.stdout,
            )
            sys.exit(130)

        if args.json and payload is not None:
            print(json.dumps(payload, indent=2))

        if not args.purge and not args.upgrade and not args.json:
            from claude_swap.update_check import check_for_update  # noqa: PLC0415
            msg = check_for_update(__version__)
            if msg:
                print(f"\n{muted(msg)}", file=sys.stderr)
        return

    # --- Stop-hook path (no management command given) ---
    try:
        _run(args)
    except Exception:  # noqa: BLE001
        pass  # fail-open: absorb any unexpected error silently
    sys.exit(0)


def _run(args: argparse.Namespace) -> None:  # noqa: C901
    cfg = _load_config()

    if not cfg.get("enabled", True):
        return

    # Fast-path: skip all work if within the cooldown window.
    if not (args.dry_run or args.check) and _is_in_cooldown(cfg):
        return

    block = read_active_block()
    status = read_cshift_status()
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
            print(f"  fiveHour         : {pct:.1f}%")
        except (KeyError, TypeError):
            print("  fiveHour         : unavailable (parse error)")
    else:
        print("  account status   : unavailable")

    print(f"  decision         : {'SWITCH' if triggered else 'no-op'}")
    if dry_run and triggered:
        print("  [dry-run] would run: cshift --switch --strategy best")
