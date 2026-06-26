"""Tests for cshift-hud (claude_swap.hud)."""
from __future__ import annotations

import datetime
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import hud as _hud
from claude_swap.hud import (
    _acquire_lock,
    _build_status_line,
    _elapsed_pct_from_ccusage,
    _fetch_oauth_usage,
    _format_reset_time,
    _get_session_minutes,
    _is_stale,
    _pct_bar,
    _read_cache,
    _read_oauth_cache,
    _refresh,
    _release_lock,
    _render_context,
    _render_limits,
    _render_session,
    _write_cache,
    _write_oauth_cache,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Redirect all cache paths to a temp dir."""
    monkeypatch.setattr(_hud, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(_hud, "_STATUS_FILE", tmp_path / "status.txt")
    monkeypatch.setattr(_hud, "_LOCK_FILE", tmp_path / "refresh.lock")
    monkeypatch.setattr(_hud, "_STDIN_CACHE_FILE", tmp_path / "stdin_cache.json")
    monkeypatch.setattr(_hud, "_OAUTH_CACHE_FILE", tmp_path / "oauth_cache.json")
    return tmp_path


# ---------------------------------------------------------------------------
# _pct_bar
# ---------------------------------------------------------------------------

class TestPctBar:
    def test_none(self):
        assert _pct_bar(None) == "⚪"

    def test_above_90(self):
        assert _pct_bar(90.0) == "🔴"
        assert _pct_bar(100.0) == "🔴"

    def test_70_to_89(self):
        assert _pct_bar(70.0) == "🟡"
        assert _pct_bar(89.9) == "🟡"

    def test_below_70(self):
        assert _pct_bar(0.0) == "🟢"
        assert _pct_bar(69.9) == "🟢"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class TestCache:
    def test_read_missing(self, isolated_cache):
        assert _read_cache() == ""

    def test_round_trip(self, isolated_cache):
        _write_cache("🟢#1*:18%  🔴#2:100%")
        assert _read_cache() == "🟢#1*:18%  🔴#2:100%"

    def test_write_strips_trailing_newline(self, isolated_cache):
        _write_cache("hello\n")
        assert _read_cache() == "hello"

    def test_is_stale_missing(self, isolated_cache):
        assert _is_stale() is True

    def test_is_stale_fresh(self, isolated_cache):
        _write_cache("line")
        assert _is_stale() is False

    def test_is_stale_old(self, isolated_cache, monkeypatch):
        _write_cache("line")
        monkeypatch.setattr(_hud, "_TTL", 0.0)
        time.sleep(0.01)
        assert _is_stale() is True


# ---------------------------------------------------------------------------
# _format_reset_time
# ---------------------------------------------------------------------------

class TestFormatResetTime:
    def test_none(self):
        assert _format_reset_time(None) is None

    def test_past(self):
        assert _format_reset_time("2020-01-01T00:00:00Z") is None

    def test_hours_and_minutes(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4, minutes=7)
        result = _format_reset_time(future.isoformat())
        assert result is not None
        assert "h" in result
        assert "m" in result

    def test_days(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2, hours=5)
        result = _format_reset_time(future.isoformat())
        assert result is not None
        assert "d" in result

    def test_zero_minutes(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        result = _format_reset_time(future.isoformat())
        assert result is not None
        assert "h" in result


# ---------------------------------------------------------------------------
# _render_limits
# ---------------------------------------------------------------------------

class TestOAuthCache:
    def test_miss_when_empty(self, isolated_cache):
        assert _read_oauth_cache() is None

    def test_hit_within_ttl(self, isolated_cache):
        data = {"five_hour_pct": 50.0, "weekly_pct": 10.0}
        _write_oauth_cache(data)
        assert _read_oauth_cache() == data

    def test_miss_when_expired(self, isolated_cache, monkeypatch):
        data = {"five_hour_pct": 50.0}
        _write_oauth_cache(data)
        monkeypatch.setattr(_hud, "_OAUTH_CACHE_TTL", 0.0)
        time.sleep(0.01)
        assert _read_oauth_cache() is None

    def test_fetch_uses_cache(self, isolated_cache):
        data = {"five_hour_pct": 77.0, "weekly_pct": 20.0,
                "five_hour_resets_at": None, "weekly_resets_at": None}
        _write_oauth_cache(data)
        result = _fetch_oauth_usage()
        assert result == data


class TestRenderLimits:
    def test_none(self):
        assert _render_limits(None) is None

    def test_no_five_hour(self):
        assert _render_limits({"weekly_pct": 10.0}) is None

    def test_five_hour_only(self):
        result = _render_limits({"five_hour_pct": 50.0})
        assert result is not None
        assert "5h:" in result
        assert "50%" in result

    def test_five_hour_with_weekly(self):
        result = _render_limits({"five_hour_pct": 84.0, "weekly_pct": 11.0})
        assert "5h:" in result
        assert "84%" in result
        assert "wk:" in result
        assert "11%" in result

    def test_with_reset_time(self):
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4, minutes=7)
        result = _render_limits({
            "five_hour_pct": 84.0,
            "five_hour_resets_at": future.isoformat(),
        })
        assert result is not None
        assert "4h" in result

    def test_red_at_90(self):
        result = _render_limits({"five_hour_pct": 90.0})
        assert "\x1b[31m" in result  # RED

    def test_yellow_at_70(self):
        result = _render_limits({"five_hour_pct": 70.0})
        assert "\x1b[33m" in result  # YELLOW

    def test_green_below_70(self):
        result = _render_limits({"five_hour_pct": 50.0})
        assert "\x1b[32m" in result  # GREEN


# ---------------------------------------------------------------------------
# _render_session / _render_context
# ---------------------------------------------------------------------------

class TestRenderSession:
    def test_none(self):
        assert _render_session(None) is None

    def test_healthy(self):
        result = _render_session(30)
        assert "session:" in result
        assert "30m" in result
        assert "\x1b[32m" in result  # GREEN

    def test_warning(self):
        result = _render_session(90)
        assert "\x1b[33m" in result  # YELLOW

    def test_critical(self):
        result = _render_session(150)
        assert "\x1b[31m" in result  # RED


class TestRenderContext:
    def test_none(self):
        assert _render_context(None) is None

    def test_healthy(self):
        result = _render_context(30)
        assert "ctx:" in result
        assert "30%" in result
        assert "\x1b[32m" in result  # GREEN

    def test_warning(self):
        result = _render_context(75)
        assert "\x1b[33m" in result  # YELLOW

    def test_critical(self):
        result = _render_context(95)
        assert "\x1b[31m" in result  # RED


# ---------------------------------------------------------------------------
# _get_session_minutes
# ---------------------------------------------------------------------------

class TestGetSessionMinutes:
    def test_none_path(self):
        assert _get_session_minutes(None) is None

    def test_missing_file(self):
        assert _get_session_minutes("/tmp/nonexistent_transcript_xyz.jsonl") is None

    def test_parses_first_timestamp(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=45)
        transcript.write_text(
            json.dumps({"type": "attachment", "timestamp": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")}) + "\n"
        )
        result = _get_session_minutes(str(transcript))
        assert result is not None
        assert 44 <= result <= 46  # ~45 minutes

    def test_skips_entries_without_timestamp(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        transcript.write_text(
            json.dumps({"type": "last-prompt"}) + "\n"
            + json.dumps({"type": "attachment", "timestamp": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")}) + "\n"
        )
        result = _get_session_minutes(str(transcript))
        assert result is not None
        assert 9 <= result <= 11


# ---------------------------------------------------------------------------
# Helpers: fake subprocess.Popen for cswap calls
# ---------------------------------------------------------------------------

_LIST_TWO_ACCOUNTS = json.dumps({
    "schemaVersion": 1,
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1,
            "email": "a@example.com",
            "active": True,
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 18.0}, "sevenDay": {"pct": 5.0}},
        },
        {
            "number": 2,
            "email": "b@example.com",
            "active": False,
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 100.0}, "sevenDay": {"pct": 11.0}},
        },
    ],
})

_STATUS_ACTIVE = json.dumps({
    "schemaVersion": 1,
    "active": {
        "number": 1,
        "email": "a@example.com",
        "usageStatus": "ok",
        "usage": {"fiveHour": {"pct": 18.0}, "sevenDay": {"pct": 5.0}},
    },
})

_STATUS_NULL = json.dumps({
    "schemaVersion": 1,
    "active": {
        "number": 1,
        "email": "a@example.com",
        "usageStatus": "unavailable",
        "usage": None,
    },
})


def _fake_popen(list_stdout: str, status_stdout: str, ccusage_stdout: str = "{}"):
    """Return a Popen factory that serves list/status/ccusage output by args."""
    def _factory(cmd, **kwargs):
        m = MagicMock()
        if "--list" in cmd:
            stdout = list_stdout
        elif "--status" in cmd:
            stdout = status_stdout
        else:
            stdout = ccusage_stdout
        m.communicate.return_value = (stdout, "")
        m.kill.return_value = None
        return m
    return _factory


_LIST_ACTIVE_NULL = json.dumps({
    "schemaVersion": 1,
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1,
            "email": "a@example.com",
            "active": True,
            "usageStatus": "unavailable",
            "usage": None,
        },
        {
            "number": 2,
            "email": "b@example.com",
            "active": False,
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 100.0}, "sevenDay": {"pct": 11.0}},
        },
    ],
})


_CCUSAGE_ACTIVE = json.dumps({
    "blocks": [{
        "isActive": True,
        "costUSD": 7.79,
        "projection": {"remainingMinutes": 150, "totalCost": 58.43},
    }]
})  # elapsed = 300 - 150 = 150 min → 50%


def _error_popen(cmd, **kwargs):
    m = MagicMock()
    m.communicate.side_effect = subprocess.TimeoutExpired(cmd, 5)
    m.kill.return_value = None
    return m


# ---------------------------------------------------------------------------
# _build_status_line
# ---------------------------------------------------------------------------

class TestBuildStatusLine:
    def test_two_accounts_normal(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                result = _build_status_line()
        assert "#1*:18%" in result
        assert "#2:100%" in result
        assert "🟢" in result
        assert "🔴" in result

    def test_active_account_null_usage_shows_question(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_ACTIVE_NULL, _STATUS_NULL)):
                result = _build_status_line()
        assert "#1*:?" in result
        assert "#2:100%" in result

    def test_no_accounts_returns_fallback(self):
        empty = json.dumps({"accounts": []})
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(empty, "{}")):
                result = _build_status_line()
        assert "no accounts" in result

    def test_cswap_timeout_returns_fallback(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_error_popen):
                result = _build_status_line()
        assert "no accounts" in result

    def test_cswap_missing_returns_fallback(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=FileNotFoundError("cswap")):
                result = _build_status_line()
        assert "no accounts" in result

    def test_status_pct_takes_priority_over_list(self):
        status_50 = json.dumps({
            "schemaVersion": 1,
            "active": {"number": 1, "email": "a@example.com",
                       "usageStatus": "ok",
                       "usage": {"fiveHour": {"pct": 50.0}}},
        })
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, status_50)):
                result = _build_status_line()
        assert "#1*:50%" in result

    def test_api_key_account_uses_ccusage_elapsed_pct(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen",
                       side_effect=_fake_popen(_LIST_ACTIVE_NULL, _STATUS_NULL, _CCUSAGE_ACTIVE)):
                result = _build_status_line()
        assert "#1*:50%" in result

    def test_ccusage_pct_not_used_when_cswap_pct_available(self):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen",
                       side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE, _CCUSAGE_ACTIVE)):
                result = _build_status_line()
        assert "#1*:18%" in result
        assert "#1*:50%" not in result

    def test_single_account(self):
        single = json.dumps({
            "activeAccountNumber": 1,
            "accounts": [{"number": 1, "email": "a@example.com",
                          "active": True, "usageStatus": "ok",
                          "usage": {"fiveHour": {"pct": 5.0}}}],
        })
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(single, _STATUS_ACTIVE)):
                result = _build_status_line()
        assert "#1*:5%" in result or "#1*:18%" in result

    def test_oauth_limits_included_when_available(self):
        oauth = {
            "five_hour_pct": 84.0,
            "weekly_pct": 11.0,
            "five_hour_resets_at": None,
            "weekly_resets_at": None,
        }
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=oauth):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                result = _build_status_line()
        assert "5h:" in result
        assert "84%" in result
        assert "wk:" in result
        assert "#1*:18%" in result

    def test_session_and_ctx_included_from_stdin(self, tmp_path):
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({"type": "attachment", "timestamp": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")}) + "\n"
        )
        stdin_data = {
            "transcript_path": str(transcript),
            "context_window": {"used_percentage": 42},
        }
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                result = _build_status_line(stdin_data)
        assert "session:" in result
        assert "m" in result
        assert "ctx:" in result
        assert "42%" in result


# ---------------------------------------------------------------------------
# _refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_writes_cache(self, isolated_cache):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                _refresh()
        assert _read_cache() != ""

    def test_releases_lock_on_success(self, isolated_cache):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                _refresh()
        assert not _hud._LOCK_FILE.exists()

    def test_releases_lock_on_error(self, isolated_cache):
        with patch.object(_hud, "_build_status_line", side_effect=RuntimeError("boom")):
            _refresh()
        assert not _hud._LOCK_FILE.exists()

    def test_lock_prevents_concurrent_refresh(self, isolated_cache):
        _acquire_lock()
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)) as mock_popen:
                _refresh()
        mock_popen.assert_not_called()
        _release_lock()

    def test_uses_stdin_cache(self, isolated_cache, tmp_path):
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({"type": "attachment", "timestamp": start.strftime("%Y-%m-%dT%H:%M:%S.000Z")}) + "\n"
        )
        _hud._save_stdin_cache({
            "transcript_path": str(transcript),
            "context_window": {"used_percentage": 55},
        })
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=None):
            with patch("subprocess.Popen", side_effect=_fake_popen(_LIST_TWO_ACCOUNTS, _STATUS_ACTIVE)):
                _refresh()
        cached = _read_cache()
        assert "session:" in cached
        assert "ctx:" in cached
        assert "55%" in cached


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_hot_path_prints_cache(self, isolated_cache, capsys):
        _write_cache("🟢#1*:18%  🔴#2:100%")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            with patch.object(_hud, "_is_stale", return_value=False):
                with patch.object(_hud, "_spawn_refresh") as mock_spawn:
                    main([])
        captured = capsys.readouterr()
        assert "🟢#1*:18%" in captured.out
        mock_spawn.assert_not_called()

    def test_cold_path_prints_loading(self, isolated_cache, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            with patch.object(_hud, "_spawn_refresh"):
                main([])
        captured = capsys.readouterr()
        assert "loading" in captured.out

    def test_stale_cache_spawns_refresh(self, isolated_cache):
        _write_cache("stale line")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            with patch.object(_hud, "_is_stale", return_value=True):
                with patch.object(_hud, "_spawn_refresh") as mock_spawn:
                    main([])
        mock_spawn.assert_called_once()

    def test_refresh_flag_calls_refresh(self, isolated_cache):
        with patch.object(_hud, "_refresh") as mock_refresh:
            main(["--refresh"])
        mock_refresh.assert_called_once()

    def test_stdin_read_error_is_silent(self, isolated_cache, capsys):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.side_effect = OSError("stdin gone")
            with patch.object(_hud, "_spawn_refresh"):
                main([])  # must not raise
        captured = capsys.readouterr()
        assert captured.out  # still outputs something

    def test_stdin_saved_to_cache(self, isolated_cache):
        stdin_payload = {"context_window": {"used_percentage": 33}}
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = json.dumps(stdin_payload)
            with patch.object(_hud, "_is_stale", return_value=False):
                with patch.object(_hud, "_spawn_refresh"):
                    main([])
        saved = _hud._load_stdin_cache()
        assert saved.get("context_window", {}).get("used_percentage") == 33
