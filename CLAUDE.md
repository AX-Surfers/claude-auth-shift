# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**claude-auth-shift** (`claude-swap`) is a multi-account manager for Claude Code. It ships three CLI entrypoints:

| Command | Module | Purpose |
|---------|--------|---------|
| `cswap` | `cli.py` → `switcher.py` | Manual account management and switching |
| `cshift` | `autoswitch.py` | Stop-hook auto-switcher (runs on every Claude Code turn end) |
| `cshift-hud` | `hud.py` | Status bar command (cache-first, must return in <100 ms) |
| `cshift-setup` | `setup_cmd.py` | One-shot installer that wires settings.json |
| `/cshift [N]` | `~/.claude/commands/cshift.md` | User-scope slash command: switch to account N, or rotate |

## Commands

```bash
# Install in dev mode (with uv)
uv sync

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/test_switcher.py

# Run a single test by name
uv run pytest tests/test_switcher.py::test_add_account -v

# Run without touching real keychain (all tests do this by default via autouse fixture)
uv run pytest
```

## Architecture

### Module responsibilities

- **`switcher.py`** — `ClaudeAccountSwitcher` is the core orchestrator: account CRUD, switch logic, usage-aware strategy (`best` / `next-available`), and session lifecycle. Large file (~2500 lines); contains the `_perform_switch` critical path.
- **`credentials.py`** — `CredentialStore` owns credential I/O: macOS Keychain vs file routing, per-process capability cache, `.enc`-wins backup reconciliation. Intentional leaf: never imports `switcher.py`.
- **`oauth.py`** — Token refresh, usage API calls (`fetch_usage_for_account`), headroom computation. Stateless helper used by `switcher.py` and `hud.py`.
- **`autoswitch.py`** (`cshift`) — Fail-open Stop hook. Reads `cshift.json` config, checks cooldown file, delegates all switching to `cswap --switch --strategy best` via subprocess.
- **`hud.py`** (`cshift-hud`) — Cache-first status bar. Hot path prints cached `status.txt` and exits; background refresh runs `ClaudeAccountSwitcher` directly (no subprocess), reads `~/.codex/sessions/YYYY/MM/DD/*.jsonl` directly for Codex rate-limit data, and only uses subprocess for `ccusage blocks --active` (API-key fallback). Adapts output to terminal width by dropping middle segments when narrow.
- **`cache.py`** — Generic JSON cache helpers (`read_cache` / `write_cache`) used by `autoswitch.py` (cooldown) and `hud.py` (OAuth cache).
- **`session.py`** — Session-mode profile management: isolated `~/.claude` dirs per account for `cswap run`, stale-marker protocol, session PID tracking.
- **`paths.py`** — Centralizes all path resolution: respects `CLAUDE_CONFIG_DIR` and `XDG_DATA_HOME` env vars.
- **`migrations.py`** — One-time data migrations (e.g. Windows Credential Manager → files, legacy backup dir relocation).
- **`locking.py`** — `FileLock`: non-reentrant file lock used around credential writes. Important: never re-acquire within the same process context.

### Data flow for a switch

1. `cswap --switch` → `cli.py:main()` → `switcher.ClaudeAccountSwitcher.switch()`
2. Reads `sequence.json` (account registry) from `<backup_root>/sequence.json`
3. Reads live identity from `~/.claude.json` (`oauthAccount.emailAddress` + `organizationUuid`)
4. If strategy is `best`: fetches usage via `oauth.fetch_usage_for_account()` (parallel, cached 15 s in `cache/usage.json`), selects target
5. `_perform_switch(target)` under `FileLock`:
   - Backs up current account's credentials + config
   - Writes target account's credentials to active store (macOS Keychain or `~/.claude/.credentials.json`)
   - Writes target config to `~/.claude.json`

### Credential storage backends

Active credential (what Claude Code reads):
- **macOS**: `security` CLI → keychain service `"Claude Code-credentials"` / `"Claude Code"` (for API keys)
- **Linux/WSL/Windows**: `~/.claude/.credentials.json`

Per-account backup credentials (what cswap manages):
- **macOS**: `security` CLI → keychain service `"claude-swap"` (account key = `"account-{N}-{email}"`)
- **All platforms fallback**: encrypted files in `<backup_root>/credentials/`

### Account identity

Accounts are keyed by composite `(email, organizationUuid)` — the same email can be registered as both a personal (`""`) and org account. The `sequence.json` registry at `<backup_root>/sequence.json` is the source of truth.

### Test structure

`conftest.py` has two `autouse` fixtures that apply globally:
- `_isolate_real_home`: redirects `$HOME` to a throwaway dir so tests never touch the real `~/.claude`
- `block_real_keychain`: replaces `macos_keychain` with an in-memory `_KeychainStore` and injects a fake `keyring` module

Tests needing real keychain access are marked `@pytest.mark.no_keychain_fake`.

### Key data files at runtime

| Path | Contents |
|------|----------|
| `~/.claude.json` | Claude Code's active account (`oauthAccount`) and config |
| `~/.claude/.credentials.json` | Active OAuth token (Linux/WSL/Windows) |
| `~/.claude/cshift.json` | cshift thresholds and cooldown config |
| `~/.claude/cshift-hud/` | HUD cache files (`status.txt`, `oauth_cache.json`) |
| `<backup_root>/sequence.json` | Account registry and sequence |
| `<backup_root>/credentials/` | Per-account backup credential files |
| `<backup_root>/configs/` | Per-account backup `.claude.json` snapshots |

`<backup_root>` = `~/.claude-swap-backup` (macOS/Windows) or `$XDG_DATA_HOME/claude-swap` (Linux).
