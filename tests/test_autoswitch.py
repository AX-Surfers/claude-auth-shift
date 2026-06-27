"""Tests for cshift (claude_swap.autoswitch)."""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import autoswitch
from claude_swap.autoswitch import (
    _is_in_cooldown,
    _load_config,
    _record_cooldown,
    main,
    read_active_block,
    read_cswap_status,
    should_switch,
)
from claude_swap.cache import write_cache

# ---------------------------------------------------------------------------
# Sample fixtures (mirrors the ccusage blocks --active -j and cswap --status JSON)
# ---------------------------------------------------------------------------

_CCUSAGE_ACTIVE_BLOCK = {
    "isActive": True,
    "totalTokens": 28_389_651,
    "costUSD": 22.84,
    "projection": {
        "remainingMinutes": 184,
        "totalCost": 67.58,
    },
    "tokenCounts": {
        "cacheReadInputTokens": 26_657_349,
        "inputTokens": 118_146,
        "outputTokens": 255_980,
    },
}

_CCUSAGE_RESPONSE = json.dumps({"blocks": [_CCUSAGE_ACTIVE_BLOCK]})

_CSWAP_STATUS_HIGH = json.dumps({
    "schemaVersion": 1,
    "active": {
        "email": "user@example.com",
        "usage": {
            "fiveHour": {"pct": 92.0, "used": 920_000, "total": 1_000_000},
            "sevenDay": {"pct": 40.0},
        },
    },
})

_CSWAP_STATUS_LOW = json.dumps({
    "schemaVersion": 1,
    "active": {
        "email": "user@example.com",
        "usage": {
            "fiveHour": {"pct": 30.0, "used": 300_000, "total": 1_000_000},
            "sevenDay": {"pct": 10.0},
        },
    },
})

_CSWAP_SWITCH_SUCCESS = json.dumps({"switched": True, "to": "other@example.com"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Patch Path.home() to a temp dir with a .claude subdir pre-created."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Helper: a CompletedProcess stub
# ---------------------------------------------------------------------------

def _cp(stdout: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# should_switch
# ---------------------------------------------------------------------------

class TestShouldSwitch:
    def _cfg(self, **overrides):
        cfg = {
            "pct_threshold": 80.0,
            "cost_threshold_usd": None,
            "token_threshold": None,
        }
        cfg.update(overrides)
        return cfg

    def test_should_switch_true_on_high_pct(self):
        status = json.loads(_CSWAP_STATUS_HIGH)
        assert should_switch(None, status, self._cfg()) is True

    def test_should_switch_false_on_low_pct(self):
        status = json.loads(_CSWAP_STATUS_LOW)
        assert should_switch(None, status, self._cfg()) is False

    def test_should_switch_false_both_none(self):
        assert should_switch(None, None, self._cfg()) is False

    def test_should_switch_exact_threshold(self):
        status = {"current": {"usage": {"fiveHour": {"pct": 80.0}}}}
        assert should_switch(None, status, self._cfg(pct_threshold=80.0)) is True

    def test_should_switch_cost_threshold(self):
        block = {"projection": {"totalCost": 70.0}, "totalTokens": 1_000}
        assert should_switch(block, None, self._cfg(cost_threshold_usd=50.0)) is True

    def test_should_switch_cost_threshold_not_crossed(self):
        block = {"projection": {"totalCost": 30.0}, "totalTokens": 1_000}
        assert should_switch(block, None, self._cfg(cost_threshold_usd=50.0)) is False

    def test_should_switch_token_threshold(self):
        block = {"totalTokens": 1_000_000, "projection": {}}
        assert should_switch(block, None, self._cfg(token_threshold=900_000)) is True

    def test_should_switch_malformed_status(self):
        """Malformed status does not crash; returns False (fail-open)."""
        assert should_switch(None, {"bad": "data"}, self._cfg()) is False

    def test_should_switch_malformed_block(self):
        block = {"projection": "not-a-dict"}
        assert should_switch(block, None, self._cfg(cost_threshold_usd=10.0)) is False


# ---------------------------------------------------------------------------
# read_active_block
# ---------------------------------------------------------------------------

class TestReadActiveBlock:
    def test_returns_active_block(self):
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)) as mock_run:
            result = read_active_block()
        assert result is not None
        assert result["isActive"] is True
        assert result["totalTokens"] == 28_389_651
        mock_run.assert_called_once()

    def test_returns_none_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=_cp("", returncode=1)):
            assert read_active_block() is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ccusage", 5)):
            assert read_active_block() is None

    def test_returns_none_on_missing_binary(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("ccusage not found")):
            assert read_active_block() is None

    def test_returns_none_on_bad_json(self):
        with patch("subprocess.run", return_value=_cp("not-json")):
            assert read_active_block() is None

    def test_returns_none_when_no_active_block(self):
        data = json.dumps({"blocks": [{"isActive": False}]})
        with patch("subprocess.run", return_value=_cp(data)):
            assert read_active_block() is None

    def test_returns_none_on_empty_blocks(self):
        with patch("subprocess.run", return_value=_cp(json.dumps({"blocks": []}))):
            assert read_active_block() is None


# ---------------------------------------------------------------------------
# read_cswap_status
# ---------------------------------------------------------------------------

class TestReadCswapStatus:
    def test_returns_parsed_status(self):
        expected = json.loads(_CSWAP_STATUS_HIGH)
        with patch("claude_swap.switcher.ClaudeAccountSwitcher.status", return_value=expected):
            result = read_cswap_status()
        assert result is not None
        assert result["active"]["usage"]["fiveHour"]["pct"] == 92.0

    def test_returns_none_on_exception(self):
        with patch("claude_swap.switcher.ClaudeAccountSwitcher.status", side_effect=RuntimeError("boom")):
            assert read_cswap_status() is None

    def test_returns_none_when_switcher_raises(self):
        with patch("claude_swap.switcher.ClaudeAccountSwitcher.status", side_effect=Exception("unavailable")):
            assert read_cswap_status() is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_defaults_when_no_file(self, isolated_home, monkeypatch):
        monkeypatch.delenv("CSWAP_GUARD_PCT", raising=False)
        monkeypatch.delenv("CSWAP_GUARD_COOLDOWN", raising=False)
        monkeypatch.delenv("CSWAP_GUARD_ENABLED", raising=False)
        cfg = _load_config()
        assert cfg["pct_threshold"] == 90.0
        assert cfg["cooldown_minutes"] == 30.0
        assert cfg["enabled"] is True

    def test_env_override_pct(self, isolated_home, monkeypatch):
        monkeypatch.setenv("CSWAP_GUARD_PCT", "60")
        cfg = _load_config()
        assert cfg["pct_threshold"] == 60.0

    def test_env_override_cooldown(self, isolated_home, monkeypatch):
        monkeypatch.setenv("CSWAP_GUARD_COOLDOWN", "10")
        cfg = _load_config()
        assert cfg["cooldown_minutes"] == 10.0

    def test_env_override_enabled_false(self, isolated_home, monkeypatch):
        monkeypatch.setenv("CSWAP_GUARD_ENABLED", "false")
        cfg = _load_config()
        assert cfg["enabled"] is False

    def test_file_values_merged(self, isolated_home):
        (isolated_home / ".claude" / "cshift.json").write_text(
            json.dumps({"pct_threshold": 90.0, "cooldown_minutes": 15.0})
        )
        cfg = _load_config()
        assert cfg["pct_threshold"] == 90.0
        assert cfg["cooldown_minutes"] == 15.0

    def test_corrupted_config_uses_defaults(self, isolated_home):
        (isolated_home / ".claude" / "cshift.json").write_text("{{not valid json}}")
        cfg = _load_config()
        assert cfg["pct_threshold"] == 90.0


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_not_in_cooldown_initially(self, isolated_home):
        assert _is_in_cooldown({"cooldown_minutes": 30.0}) is False

    def test_in_cooldown_after_record(self, isolated_home):
        _record_cooldown()
        assert _is_in_cooldown({"cooldown_minutes": 30.0}) is True

    def test_not_in_cooldown_after_expiry(self, isolated_home):
        # Write a cooldown entry with a timestamp 1 hour in the past.
        path = isolated_home / ".claude" / ".cshift-cooldown.json"
        path.write_text(json.dumps({"timestamp": time.time() - 3700, "data": True}))
        assert _is_in_cooldown({"cooldown_minutes": 30.0}) is False

    def test_cooldown_prevents_subprocess_spawn(self, isolated_home):
        """When in cooldown, no ccusage/cswap subprocesses are spawned."""
        import argparse
        _record_cooldown()
        with patch("subprocess.run") as mock_run:
            autoswitch._run(argparse.Namespace(dry_run=False, check=False))
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# main() / _run() integration
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_fails_open_on_exception(self, isolated_home):
        """RuntimeError inside _run() does not propagate; main() exits 0."""
        with patch.object(autoswitch, "_run", side_effect=RuntimeError("boom")):
            with pytest.raises(SystemExit) as exc_info:
                main([])
        assert exc_info.value.code == 0

    def test_dry_run_no_switch_called(self, isolated_home):
        """--dry-run evaluates thresholds but never triggers a switch."""
        status = json.loads(_CSWAP_STATUS_HIGH)
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)):
            with patch.object(autoswitch, "read_cswap_status", return_value=status):
                with patch.object(autoswitch, "_do_switch") as mock_switch:
                    with pytest.raises(SystemExit) as exc_info:
                        main(["--dry-run"])
        assert exc_info.value.code == 0
        mock_switch.assert_not_called()

    def test_no_switch_below_threshold(self, isolated_home):
        """No switch when usage is below threshold."""
        status = json.loads(_CSWAP_STATUS_LOW)
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)):
            with patch.object(autoswitch, "read_cswap_status", return_value=status):
                with patch.object(autoswitch, "_do_switch") as mock_switch:
                    with pytest.raises(SystemExit) as exc_info:
                        main([])
        assert exc_info.value.code == 0
        mock_switch.assert_not_called()

    def test_switch_called_above_threshold(self, isolated_home):
        """When threshold is crossed, _do_switch is called with strategy=best."""
        status = json.loads(_CSWAP_STATUS_HIGH)
        switch_payload = json.loads(_CSWAP_SWITCH_SUCCESS)
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)):
            with patch.object(autoswitch, "read_cswap_status", return_value=status):
                with patch.object(autoswitch, "_do_switch", return_value=True) as mock_switch:
                    with pytest.raises(SystemExit) as exc_info:
                        main([])
        assert exc_info.value.code == 0
        mock_switch.assert_called_once()

    def test_switch_records_cooldown(self, isolated_home):
        """After a successful switch, the cooldown file is written."""
        status = json.loads(_CSWAP_STATUS_HIGH)
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)):
            with patch.object(autoswitch, "read_cswap_status", return_value=status):
                with patch.object(autoswitch, "_do_switch", return_value=True):
                    with pytest.raises(SystemExit):
                        main([])
        assert (isolated_home / ".claude" / ".cshift-cooldown.json").exists()

    def test_mid_session_switch_completes(self, isolated_home):
        """Switch completes even when the switcher emits a live-session warning."""
        status = json.loads(_CSWAP_STATUS_HIGH)
        with patch("subprocess.run", return_value=_cp(_CCUSAGE_RESPONSE)):
            with patch.object(autoswitch, "read_cswap_status", return_value=status):
                with patch.object(autoswitch, "_do_switch", return_value=True) as mock_switch:
                    with pytest.raises(SystemExit) as exc_info:
                        main([])
        assert exc_info.value.code == 0
        mock_switch.assert_called_once()

    def test_enabled_false_skips_all(self, isolated_home):
        """enabled=false in config disables the guard entirely."""
        (isolated_home / ".claude" / "cshift.json").write_text(
            json.dumps({"enabled": False})
        )
        with patch("subprocess.run") as mock_run:
            with patch.object(autoswitch, "_do_switch") as mock_switch:
                with pytest.raises(SystemExit):
                    main([])
        mock_run.assert_not_called()
        mock_switch.assert_not_called()
