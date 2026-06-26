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


_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_CSHIFT_CONFIG_PATH = Path.home() / ".claude" / "cshift.json"

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
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
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
    if _CSHIFT_CONFIG_PATH.exists():
        _ok("~/.claude/cshift.json already exists")
        return
    _CSHIFT_CONFIG_PATH.write_text(
        json.dumps({"pct_threshold": 90, "cooldown_minutes": 30, "enabled": True}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    _ok("Created ~/.claude/cshift.json with defaults")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    print("cshift-setup\n")

    ccusage_ok = _install_ccusage()
    _setup_settings()
    _setup_cshift_config()

    print()
    if ccusage_ok:
        print("Setup complete. Restart Claude Code for the hook and status bar to take effect.")
    else:
        print(
            "Setup partially complete. Install Node.js, run `npm install -g ccusage`, "
            "then restart Claude Code."
        )

    sys.exit(0 if ccusage_ok else 1)
