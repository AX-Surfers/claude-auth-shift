"""
cshift-setup — one-shot setup for cshift and cshift-hud.

Installs ccusage via npm and wires cshift + cshift-hud into
~/.claude/settings.json. Safe to re-run; skips steps already done.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from claude_swap.paths import get_claude_config_home, get_global_config_path


def _settings_path() -> Path:
    return get_claude_config_home() / "settings.json"


def _cshift_config_path() -> Path:
    return get_claude_config_home() / "cshift.json"

_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def _ok(msg: str) -> None:
    print(f"{_GREEN}✓{_RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"{_YELLOW}!{_RESET} {msg}")


def _err(msg: str) -> None:
    print(f"{_RED}✗{_RESET} {msg}")


# ---------------------------------------------------------------------------
# Step 1 — ccusage
# ---------------------------------------------------------------------------

def _install_ccusage() -> bool:
    if shutil.which("ccusage"):
        _ok("ccusage already installed")
        return True

    if not shutil.which("npm"):
        _err(
            "npm not found — install Node.js from https://nodejs.org/ "
            "then run cshift-setup again"
        )
        return False

    print("Installing ccusage via npm…")
    result = subprocess.run(
        ["npm", "install", "-g", "ccusage"],
        check=False,
    )
    if result.returncode == 0:
        _ok("ccusage installed")
        return True

    _err("ccusage installation failed — run `npm install -g ccusage` manually")
    return False


# ---------------------------------------------------------------------------
# Step 2 — ~/.claude/settings.json
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict) -> None:
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _patch_stop_hook(settings: dict) -> bool:
    """Add the cshift Stop hook if not already present. Returns True if changed."""
    hooks = settings.setdefault("hooks", {})
    stop_entries: list = hooks.setdefault("Stop", [])

    already = any(
        h.get("command") == "cshift"
        for entry in stop_entries
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )
    if already:
        _ok("cshift Stop hook already present")
        return False

    stop_entries.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": "cshift", "timeout": 30}],
    })
    _ok("Added cshift to Stop hooks")
    return True


def _patch_status_line(settings: dict) -> bool:
    """Set statusLine to cshift-hud if not already set. Returns True if changed."""
    current = settings.get("statusLine", {})
    if isinstance(current, dict) and current.get("command") == "cshift-hud":
        _ok("statusLine already set to cshift-hud")
        return False

    if current and not (isinstance(current, dict) and not current.get("command")):
        _warn(
            f"statusLine is currently set to "
            f"'{current.get('command', current)}' — overwriting with cshift-hud"
        )

    settings["statusLine"] = {"type": "command", "command": "cshift-hud"}
    _ok("Set statusLine to cshift-hud")
    return True


def _setup_settings() -> None:
    settings = _load_settings()
    changed = _patch_stop_hook(settings)
    changed |= _patch_status_line(settings)
    if changed:
        _save_settings(settings)


# ---------------------------------------------------------------------------
# Step 3 — ~/.claude/cshift.json
# ---------------------------------------------------------------------------

def _setup_cshift_config() -> None:
    config_path = _cshift_config_path()
    if config_path.exists():
        _ok(f"{config_path} already exists")
        return
    config_path.write_text(
        json.dumps({"pct_threshold": 90, "cooldown_minutes": 30, "enabled": True}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    _ok("Created ~/.claude/cshift.json with defaults")


# ---------------------------------------------------------------------------
# Step 4 — ~/.claude/commands/cshift.md (slash command)
# ---------------------------------------------------------------------------

_SLASH_COMMAND_CONTENT = """\
Switch Claude Code account. Pass a number to switch to that account, or omit to rotate to the next one.

Examples:
- `/cshift` → rotate to next account (`cshift --switch`)
- `/cshift 2` → switch to account #2 (`cshift --switch-to 2`)

$ARGUMENTS

```bash
if [ -z "$ARGUMENTS" ]; then
  cshift --switch
else
  cshift --switch-to $ARGUMENTS
fi
```
"""


def _setup_slash_command() -> None:
    commands_dir = get_claude_config_home() / "commands"
    command_path = commands_dir / "cshift.md"
    if command_path.exists():
        _ok("/cshift slash command already installed")
        return
    commands_dir.mkdir(parents=True, exist_ok=True)
    command_path.write_text(_SLASH_COMMAND_CONTENT, encoding="utf-8")
    _ok("Installed /cshift slash command")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Step 5 — auto-register current logged-in account
# ---------------------------------------------------------------------------

def _detect_logged_in_email() -> str | None:
    """Return the email of the currently logged-in Claude Code account, or None."""
    try:
        config_path = get_global_config_path()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        email = data.get("oauthAccount", {}).get("emailAddress", "")
        return email or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _auto_add_account() -> None:
    """Register the current account if one is logged in and none are registered yet."""
    email = _detect_logged_in_email()
    if not email:
        _warn("No logged-in account detected — run 'cshift --add-account' manually after logging in")
        return

    result = subprocess.run(
        ["cshift", "--list", "--json"],
        capture_output=True,
        text=True,
    )
    try:
        accounts = json.loads(result.stdout) if result.returncode == 0 else []
    except json.JSONDecodeError:
        accounts = []

    already = any(
        acc.get("email") == email
        for acc in (accounts if isinstance(accounts, list) else [])
    )
    if already:
        _ok(f"Account already registered: {email}")
        return

    add_result = subprocess.run(
        ["cshift", "--add-account"],
        capture_output=True,
        text=True,
    )
    if add_result.returncode == 0:
        _ok(add_result.stdout.strip() or f"Registered account: {email}")
    else:
        _warn(f"Could not auto-register account — run 'cshift --add-account' manually")


def main(argv: list[str] | None = None) -> None:
    print("cshift-setup\n")

    ccusage_ok = _install_ccusage()
    _setup_settings()
    _setup_cshift_config()
    _setup_slash_command()
    _auto_add_account()

    print()
    if ccusage_ok:
        print("Setup complete. Restart Claude Code for the hook and status bar to take effect.")
    else:
        print(
            "Setup partially complete. Install Node.js, run `npm install -g ccusage`, "
            "then restart Claude Code."
        )

    sys.exit(0 if ccusage_ok else 1)
