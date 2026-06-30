"""Tests for cshift-setup (claude_swap.setup_cmd)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import setup_cmd as _sc
from claude_swap.setup_cmd import (
    _auto_add_account,
    _detect_logged_in_email,
    _install_ccusage,
    _load_settings,
    _patch_status_line,
    _patch_stop_hook,
    _save_settings,
    _setup_cshift_config,
    _setup_settings,
    _setup_slash_command,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(_sc, "get_claude_config_home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# _install_ccusage
# ---------------------------------------------------------------------------

class TestInstallCcusage:
    def test_already_installed(self, capsys):
        with patch("shutil.which", return_value="/usr/local/bin/ccusage"):
            result = _install_ccusage()
        assert result is True
        assert "already" in capsys.readouterr().out

    def test_npm_missing(self, capsys):
        with patch("shutil.which", return_value=None):
            result = _install_ccusage()
        assert result is False
        assert "npm" in capsys.readouterr().out

    def test_npm_install_success(self, capsys):
        def _which(cmd):
            return None if cmd == "ccusage" else "/usr/bin/npm"

        with patch("shutil.which", side_effect=_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                result = _install_ccusage()
        assert result is True
        mock_run.assert_called_once()
        assert "ccusage" in mock_run.call_args[0][0]

    def test_npm_install_failure(self, capsys):
        def _which(cmd):
            return None if cmd == "ccusage" else "/usr/bin/npm"

        with patch("shutil.which", side_effect=_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                result = _install_ccusage()
        assert result is False
        assert "failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _patch_stop_hook
# ---------------------------------------------------------------------------

class TestPatchStopHook:
    def test_adds_hook_when_absent(self):
        settings = {}
        changed = _patch_stop_hook(settings)
        assert changed is True
        stop = settings["hooks"]["Stop"]
        assert any(
            h.get("command") == "cshift"
            for entry in stop
            for h in entry.get("hooks", [])
        )

    def test_skips_when_already_present(self):
        settings = {
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "cshift"}]}]
            }
        }
        changed = _patch_stop_hook(settings)
        assert changed is False
        # Only one entry, not duplicated
        assert len(settings["hooks"]["Stop"]) == 1

    def test_preserves_existing_hooks(self):
        existing = {"type": "command", "command": "other-hook"}
        settings = {
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [existing]}]
            }
        }
        _patch_stop_hook(settings)
        stop = settings["hooks"]["Stop"]
        commands = [h.get("command") for e in stop for h in e.get("hooks", [])]
        assert "other-hook" in commands
        assert "cshift" in commands


# ---------------------------------------------------------------------------
# _patch_status_line
# ---------------------------------------------------------------------------

class TestPatchStatusLine:
    def test_sets_when_absent(self):
        settings = {}
        changed = _patch_status_line(settings)
        assert changed is True
        assert settings["statusLine"]["command"] == "cshift-hud"

    def test_skips_when_already_set(self):
        settings = {"statusLine": {"type": "command", "command": "cshift-hud"}}
        changed = _patch_status_line(settings)
        assert changed is False

    def test_overwrites_different_command(self, capsys):
        settings = {"statusLine": {"type": "command", "command": "other-hud"}}
        changed = _patch_status_line(settings)
        assert changed is True
        assert settings["statusLine"]["command"] == "cshift-hud"
        assert "overwriting" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _setup_settings
# ---------------------------------------------------------------------------

class TestSetupSettings:
    def test_creates_settings_from_scratch(self, isolated_paths):
        _setup_settings()
        settings = json.loads((isolated_paths / "settings.json").read_text())
        assert settings["statusLine"]["command"] == "cshift-hud"
        stop = settings["hooks"]["Stop"]
        assert any(
            h.get("command") == "cshift"
            for e in stop for h in e.get("hooks", [])
        )

    def test_merges_into_existing_settings(self, isolated_paths):
        existing = {
            "hooks": {
                "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "qa"}]}]
            },
            "theme": "dark"
        }
        (isolated_paths / "settings.json").write_text(json.dumps(existing))
        _setup_settings()
        settings = json.loads((isolated_paths / "settings.json").read_text())
        # Original content preserved
        assert settings["theme"] == "dark"
        assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "qa"
        # New content added
        assert settings["statusLine"]["command"] == "cshift-hud"

    def test_idempotent(self, isolated_paths):
        _setup_settings()
        _setup_settings()  # second run should not duplicate hooks
        settings = json.loads((isolated_paths / "settings.json").read_text())
        stop = settings["hooks"]["Stop"]
        cshift_count = sum(
            1 for e in stop for h in e.get("hooks", []) if h.get("command") == "cshift"
        )
        assert cshift_count == 1


# ---------------------------------------------------------------------------
# _setup_cshift_config
# ---------------------------------------------------------------------------

class TestSetupCshiftConfig:
    def test_creates_with_defaults(self, isolated_paths):
        _setup_cshift_config()
        config = json.loads((isolated_paths / "cshift.json").read_text())
        assert config["pct_threshold"] == 90
        assert config["cooldown_minutes"] == 30
        assert config["enabled"] is True

    def test_does_not_overwrite_existing(self, isolated_paths):
        (isolated_paths / "cshift.json").write_text(json.dumps({"pct_threshold": 70}))
        _setup_cshift_config()
        config = json.loads((isolated_paths / "cshift.json").read_text())
        assert config["pct_threshold"] == 70


# ---------------------------------------------------------------------------
# _setup_slash_command
# ---------------------------------------------------------------------------

class TestSetupSlashCommand:
    def test_creates_command_file(self, isolated_paths):
        _setup_slash_command()
        command_path = isolated_paths / "commands" / "cshift.md"
        assert command_path.exists()
        content = command_path.read_text()
        assert "/cshift" in content
        assert "cshift --switch" in content

    def test_creates_commands_directory(self, isolated_paths):
        assert not (isolated_paths / "commands").exists()
        _setup_slash_command()
        assert (isolated_paths / "commands").is_dir()

    def test_does_not_overwrite_existing(self, isolated_paths):
        commands_dir = isolated_paths / "commands"
        commands_dir.mkdir()
        command_path = commands_dir / "cshift.md"
        command_path.write_text("custom content")
        _setup_slash_command()
        assert command_path.read_text() == "custom content"

    def test_idempotent(self, isolated_paths):
        _setup_slash_command()
        _setup_slash_command()
        assert (isolated_paths / "commands" / "cshift.md").exists()


# ---------------------------------------------------------------------------
# _detect_logged_in_email / _auto_add_account
# ---------------------------------------------------------------------------

class TestDetectLoggedInEmail:
    def test_returns_email_when_logged_in(self, tmp_path, monkeypatch):
        config = {"oauthAccount": {"emailAddress": "user@example.com"}}
        config_file = tmp_path / ".claude.json"
        config_file.write_text(json.dumps(config))
        monkeypatch.setattr(_sc, "get_global_config_path", lambda: config_file)
        assert _detect_logged_in_email() == "user@example.com"

    def test_returns_none_when_no_oauth(self, tmp_path, monkeypatch):
        config_file = tmp_path / ".claude.json"
        config_file.write_text(json.dumps({}))
        monkeypatch.setattr(_sc, "get_global_config_path", lambda: config_file)
        assert _detect_logged_in_email() is None

    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_sc, "get_global_config_path", lambda: tmp_path / "missing.json")
        assert _detect_logged_in_email() is None


class TestAutoAddAccount:
    def test_registers_when_logged_in_and_no_accounts(self, capsys, monkeypatch):
        monkeypatch.setattr(_sc, "_detect_logged_in_email", lambda: "user@example.com")
        list_result = type("R", (), {"returncode": 0, "stdout": "[]"})()
        add_result = type("R", (), {"returncode": 0, "stdout": "Added Account 1: user@example.com"})()
        calls = iter([list_result, add_result])
        with patch("subprocess.run", side_effect=lambda *a, **kw: next(calls)):
            _auto_add_account()
        assert "user@example.com" in capsys.readouterr().out

    def test_skips_when_already_registered(self, capsys, monkeypatch):
        monkeypatch.setattr(_sc, "_detect_logged_in_email", lambda: "user@example.com")
        accounts = json.dumps([{"email": "user@example.com"}])
        list_result = type("R", (), {"returncode": 0, "stdout": accounts})()
        with patch("subprocess.run", return_value=list_result) as mock_run:
            _auto_add_account()
        assert mock_run.call_count == 1  # only --list, no --add-account
        assert "already" in capsys.readouterr().out

    def test_warns_when_not_logged_in(self, capsys, monkeypatch):
        monkeypatch.setattr(_sc, "_detect_logged_in_email", lambda: None)
        with patch("subprocess.run") as mock_run:
            _auto_add_account()
        mock_run.assert_not_called()
        assert "manually" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_exits_0_when_ccusage_ok(self, isolated_paths):
        with patch("shutil.which", return_value="/usr/bin/ccusage"):
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == 0

    def test_exits_1_when_ccusage_missing(self, isolated_paths, capsys):
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit) as exc:
                main([])
        assert exc.value.code == 1

    def test_settings_written_regardless_of_ccusage(self, isolated_paths):
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                main([])
        settings = json.loads((isolated_paths / "settings.json").read_text())
        assert settings["statusLine"]["command"] == "cshift-hud"
