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
    _email_short,
    _fetch_ccusage_blocks,
    _fetch_cshift_data,
    _fetch_oauth_usage,
    _format_reset_time,
    _get_session_minutes,
    _is_stale,
    _pct_bar,
    _read_cache,
    _read_codex_rate_limits_today,
    _read_oauth_cache,
    _refresh,
    _release_lock,
    _render_active_prefix,
    _render_codex,
    _render_context,
    _render_session,
    _visible_len,
    _fit_to_terminal,
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
    _KEY = "testkey1234567"

    def test_miss_when_empty(self, isolated_cache):
        assert _read_oauth_cache(self._KEY) is None

    def test_hit_within_ttl(self, isolated_cache):
        data = {"five_hour_pct": 50.0, "weekly_pct": 10.0}
        _write_oauth_cache(data, self._KEY)
        assert _read_oauth_cache(self._KEY) == data

    def test_miss_when_expired(self, isolated_cache, monkeypatch):
        data = {"five_hour_pct": 50.0}
        _write_oauth_cache(data, self._KEY)
        monkeypatch.setattr(_hud, "_OAUTH_CACHE_TTL", 0.0)
        monkeypatch.setattr(_hud, "_OAUTH_STALE_TTL", 0.0)
        time.sleep(0.01)
        assert _read_oauth_cache(self._KEY) is None

    def test_stale_returned_when_allow_stale(self, isolated_cache, monkeypatch):
        data = {"five_hour_pct": 50.0}
        _write_oauth_cache(data, self._KEY)
        monkeypatch.setattr(_hud, "_OAUTH_CACHE_TTL", 0.0)
        time.sleep(0.01)
        assert _read_oauth_cache(self._KEY) is None
        assert _read_oauth_cache(self._KEY, allow_stale=True) == data

    def test_miss_when_key_mismatch(self, isolated_cache):
        data = {"five_hour_pct": 50.0}
        _write_oauth_cache(data, self._KEY)
        assert _read_oauth_cache("differentkey") is None

    def test_fetch_uses_cache(self, isolated_cache):
        from claude_swap.hud import _token_key
        fake_token = "fake-oauth-token"
        data = {"five_hour_pct": 77.0, "weekly_pct": 20.0,
                "five_hour_resets_at": None, "weekly_resets_at": None}
        _write_oauth_cache(data, _token_key(fake_token))
        with patch.object(_hud, "_get_access_token", return_value=fake_token):
            result = _fetch_oauth_usage()
        assert result == data

    def test_fetch_returns_stale_on_429(self, isolated_cache, monkeypatch):
        import urllib.error
        from claude_swap.hud import _token_key
        fake_token = "fake-oauth-token"
        stale_data = {"five_hour_pct": 60.0, "weekly_pct": 15.0,
                      "five_hour_resets_at": None, "weekly_resets_at": None}
        _write_oauth_cache(stale_data, _token_key(fake_token))
        monkeypatch.setattr(_hud, "_OAUTH_CACHE_TTL", 0.0)
        time.sleep(0.01)

        def _raise_429(*_a, **_kw):
            raise urllib.error.HTTPError(None, 429, "Too Many Requests", {}, None)

        with patch.object(_hud, "_get_access_token", return_value=fake_token):
            with patch("urllib.request.urlopen", side_effect=_raise_429):
                result = _fetch_oauth_usage()
        assert result == stale_data


# ---------------------------------------------------------------------------
# _render_session / _render_context
# ---------------------------------------------------------------------------

class TestEmailShort:
    def test_standard_email(self):
        assert _email_short("seungryeol.kim@jocodingax.ai") == "jocodingax"

    def test_org_email(self):
        assert _email_short("contact@surfersclub.org") == "surfersclub"

    def test_no_at_sign(self):
        result = _email_short("invalid")
        assert result == "invalid"


class TestRenderActivePrefix:
    def test_basic(self):
        result = _render_active_prefix(2, "user@jocodingax.ai", 85.0)
        assert result is not None
        assert "#2" in result
        assert "jocodingax" in result

    def test_none_num_returns_none(self):
        assert _render_active_prefix(None, "user@example.com", 50.0) is None

    def test_red_at_high_usage(self):
        result = _render_active_prefix(1, "a@b.com", 95.0)
        assert "\x1b[31m" in result  # RED

    def test_no_email_falls_back_to_number(self):
        result = _render_active_prefix(1, None, 50.0)
        assert "#1" in result


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

_FAR_FUTURE_TS = 1_900_000_000  # Unix timestamp far in the future (year ~2030)


class TestReadCodexRateLimitsToday:
    def _sessions_dir(self, base: Path) -> Path:
        today = datetime.date.today()
        d = (
            base / ".codex" / "sessions"
            / str(today.year) / f"{today.month:02d}" / f"{today.day:02d}"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_rl_entry(self, path: Path, pct: float) -> None:
        entry = json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"used_percent": pct, "resets_at": _FAR_FUTURE_TS},
                },
            },
        })
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def test_none_when_no_sessions_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _read_codex_rate_limits_today() is None

    def test_reads_rate_limits_from_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        d = self._sessions_dir(tmp_path)
        self._write_rl_entry(d / "session.jsonl", 42.0)
        result = _read_codex_rate_limits_today()
        assert result is not None
        assert result["primary"]["used_percent"] == 42.0

    def test_returns_last_entry_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        d = self._sessions_dir(tmp_path)
        f = d / "session.jsonl"
        self._write_rl_entry(f, 10.0)
        self._write_rl_entry(f, 42.0)
        result = _read_codex_rate_limits_today()
        assert result is not None
        assert result["primary"]["used_percent"] == 42.0  # last entry wins

    def test_skips_non_token_count_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        d = self._sessions_dir(tmp_path)
        f = d / "session.jsonl"
        f.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "message"}}) + "\n"
            + json.dumps({"type": "other"}) + "\n"
        )
        assert _read_codex_rate_limits_today() is None

    def test_skips_entries_without_primary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        d = self._sessions_dir(tmp_path)
        entry = json.dumps({
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {"secondary": {}}},
        })
        (d / "session.jsonl").write_text(entry + "\n")
        assert _read_codex_rate_limits_today() is None


class TestRenderCodex:
    def test_none(self):
        assert _render_codex(None) is None

    def test_green_below_70_pct(self):
        rl = {"primary": {"used_percent": 20.0, "resets_at": _FAR_FUTURE_TS}}
        result = _render_codex(rl)
        assert result is not None
        assert "20%" in result
        assert "\x1b[32m" in result  # GREEN

    def test_yellow_at_70_pct(self):
        rl = {"primary": {"used_percent": 70.0, "resets_at": _FAR_FUTURE_TS}}
        assert "\x1b[33m" in _render_codex(rl)  # YELLOW

    def test_red_at_90_pct(self):
        rl = {"primary": {"used_percent": 90.0, "resets_at": _FAR_FUTURE_TS}}
        assert "\x1b[31m" in _render_codex(rl)  # RED

    def test_label_present(self):
        rl = {"primary": {"used_percent": 5.0, "resets_at": _FAR_FUTURE_TS}}
        assert "codex:" in _render_codex(rl)

    def test_no_primary_returns_none(self):
        assert _render_codex({"secondary": {"used_percent": 50.0}}) is None

    def test_no_used_percent_returns_none(self):
        assert _render_codex({"primary": {"resets_at": _FAR_FUTURE_TS}}) is None

    def test_includes_reset_time_when_in_future(self):
        rl = {"primary": {"used_percent": 50.0, "resets_at": _FAR_FUTURE_TS}}
        result = _render_codex(rl)
        assert result is not None
        assert "h" in result  # countdown string


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
# Shared test data for _build_status_line tests
# ---------------------------------------------------------------------------

_LIST_TWO_ACCOUNTS = {
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
}

_LIST_ACTIVE_NULL = {
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
}

_CCUSAGE_ACTIVE = {
    "blocks": [{
        "isActive": True,
        "costUSD": 7.79,
        "projection": {"remainingMinutes": 150, "totalCost": 58.43},
    }]
}  # elapsed = 300 - 150 = 150 min → 50%


def _run_build(
    list_data,
    *,
    ccusage=None,
    codex_rl=None,
    oauth=None,
    stdin_data=None,
):
    """Run _build_status_line with all data-fetching functions mocked."""
    with patch("claude_swap.hud._fetch_cshift_data", return_value=list_data):
        with patch("claude_swap.hud._fetch_oauth_usage", return_value=oauth):
            with patch("claude_swap.hud._read_codex_rate_limits_today", return_value=codex_rl):
                with patch("claude_swap.hud._fetch_ccusage_blocks", return_value=ccusage):
                    return _build_status_line(stdin_data)


# ---------------------------------------------------------------------------
# _build_status_line
# ---------------------------------------------------------------------------

class TestBuildStatusLine:
    def test_two_accounts_normal(self):
        result = _run_build(_LIST_TWO_ACCOUNTS)
        assert "#1*:18%" in result
        assert "#2:100%" in result
        assert "🟢" in result
        assert "🔴" in result
        assert "#1 example" in result

    def test_active_account_null_usage_shows_question(self):
        result = _run_build(_LIST_ACTIVE_NULL)
        assert "#1*:?" in result
        assert "#2:100%" in result

    def test_no_accounts_returns_fallback(self):
        result = _run_build({"accounts": []})
        assert "no accounts" in result

    def test_none_list_data_returns_fallback(self):
        result = _run_build(None)
        assert "no accounts" in result

    def test_api_key_account_uses_ccusage_elapsed_pct(self):
        result = _run_build(_LIST_ACTIVE_NULL, ccusage=_CCUSAGE_ACTIVE)
        assert "#1*:50%" in result

    def test_ccusage_pct_not_used_when_list_pct_available(self):
        result = _run_build(_LIST_TWO_ACCOUNTS, ccusage=_CCUSAGE_ACTIVE)
        assert "#1*:18%" in result
        assert "#1*:50%" not in result

    def test_single_account(self):
        single = {
            "activeAccountNumber": 1,
            "accounts": [{"number": 1, "email": "a@example.com",
                          "active": True, "usageStatus": "ok",
                          "usage": {"fiveHour": {"pct": 5.0}}}],
        }
        result = _run_build(single)
        assert "#1*:5%" in result

    def test_oauth_5h_and_weekly_as_leading_segment(self):
        oauth = {
            "five_hour_pct": 84.0,
            "weekly_pct": 11.0,
            "five_hour_resets_at": None,
            "weekly_resets_at": None,
        }
        result = _run_build(_LIST_TWO_ACCOUNTS, oauth=oauth)
        assert "5h:" in result
        assert "84%" in result
        assert "wk:" in result
        assert "11%" in result
        # 5h/wk segment must appear before account bar
        assert result.index("5h:") < result.index("#1*")
        assert "#1*:18%" in result

    def test_oauth_5h_absent_when_no_oauth(self):
        result = _run_build(_LIST_TWO_ACCOUNTS, oauth=None)
        assert "5h:" not in result
        assert "wk:" not in result

    def test_codex_at_end_after_account_bar(self):
        codex_rl = {"primary": {"used_percent": 50.0, "resets_at": _FAR_FUTURE_TS}}
        result = _run_build(_LIST_TWO_ACCOUNTS, codex_rl=codex_rl)
        assert "codex:" in result
        assert "50%" in result
        # codex segment must come after the account bar
        account_pos = result.index("#1*")
        codex_pos = result.index("codex:")
        assert codex_pos > account_pos

    def test_codex_absent_when_no_data(self):
        result = _run_build(_LIST_TWO_ACCOUNTS)
        assert "codex:" not in result

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
        result = _run_build(_LIST_TWO_ACCOUNTS, stdin_data=stdin_data)
        assert "session:" in result
        assert "m" in result
        assert "ctx:" in result
        assert "42%" in result


# ---------------------------------------------------------------------------
# _refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def _patch_data(self):
        """Patch all data-fetching functions for refresh tests."""
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(patch("claude_swap.hud._fetch_cshift_data", return_value=_LIST_TWO_ACCOUNTS))
        stack.enter_context(patch("claude_swap.hud._fetch_oauth_usage", return_value=None))
        stack.enter_context(patch("claude_swap.hud._read_codex_rate_limits_today", return_value=None))
        stack.enter_context(patch("claude_swap.hud._fetch_ccusage_blocks", return_value=None))
        return stack

    def test_writes_cache(self, isolated_cache):
        with self._patch_data():
            _refresh()
        assert _read_cache() != ""

    def test_releases_lock_on_success(self, isolated_cache):
        with self._patch_data():
            _refresh()
        assert not _hud._LOCK_FILE.exists()

    def test_releases_lock_on_error(self, isolated_cache):
        with patch.object(_hud, "_build_status_line", side_effect=RuntimeError("boom")):
            _refresh()
        assert not _hud._LOCK_FILE.exists()

    def test_lock_prevents_concurrent_refresh(self, isolated_cache):
        _acquire_lock()
        with patch("claude_swap.hud._fetch_cshift_data") as mock_fetch:
            _refresh()
        mock_fetch.assert_not_called()
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
        with self._patch_data():
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


# ---------------------------------------------------------------------------
# Terminal adaptive rendering
# ---------------------------------------------------------------------------

class TestVisibleLen:
    def test_plain_string(self):
        assert _visible_len("hello") == 5

    def test_ansi_stripped(self):
        s = "\x1b[32m50%\x1b[0m"
        assert _visible_len(s) == 3

    def test_dim_reset(self):
        s = "\x1b[2mcodex:\x1b[0m\x1b[32m1%\x1b[0m"
        assert _visible_len(s) == 8  # "codex:1%"

    def test_empty(self):
        assert _visible_len("") == 0

    def test_emoji(self):
        # emoji count as their raw codepoint characters, not display columns
        assert _visible_len("🟢ok") == 3  # one emoji + 2 chars


class TestFitToTerminal:
    _SEP = "  |  "

    def _make_line(self, *segments):
        return self._SEP.join(segments)

    def test_short_line_unchanged(self):
        line = self._make_line("prefix 5h:10%", "session:5m", "account:50%")
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=200)):
            result = _fit_to_terminal(line)
        assert result == line

    def test_drops_middle_segment_when_narrow(self):
        prefix = "A" * 30
        middle = "B" * 30
        suffix = "C" * 30
        line = self._make_line(prefix, middle, suffix)
        # width smaller than full line but big enough for prefix + sep + suffix
        width = len(prefix) + len(self._SEP) + len(suffix) + 1
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=width)):
            result = _fit_to_terminal(line)
        assert middle not in result
        assert prefix in result
        assert suffix in result

    def test_always_keeps_first_and_last(self):
        first = "prefix:5h:99%"
        last = "🔴#1*:100%"
        line = self._make_line(first, "session:5m", "ctx:80%", "codex:50%", last)
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=20)):
            result = _fit_to_terminal(line)
        assert first in result
        assert last in result

    def test_terminal_size_error_returns_original(self):
        line = "some status line"
        with patch("shutil.get_terminal_size", side_effect=OSError):
            result = _fit_to_terminal(line)
        assert result == line

    def test_zero_width_returns_original(self):
        line = "some status line"
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=0)):
            result = _fit_to_terminal(line)
        assert result == line
