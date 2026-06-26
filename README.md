# claude-auth-shift

> **[English](#english) | [한국어](#한국어)**

---

<a name="english"></a>

## English

Multi-account manager for Claude Code. Automatically switches accounts when you hit rate limits, and shows live usage in the status bar — without leaving your editor.

| Command | Purpose |
|---------|---------|
| `cswap` | Manual account switching and management |
| `cshift` | Stop hook: auto-switch when quota crosses threshold |
| `cshift-hud` | Status bar showing limits, session, context, and per-account usage |
| `cshift-setup` | One-shot installer that wires everything into `settings.json` |
| `/cshift [N]` | Slash command: switch to account N, or rotate if no number given |

### Requirements

- Python 3.12+
- Claude Code (CLI, VS Code, or JetBrains extension)
- [ccusage](https://github.com/ryoppippi/ccusage) — `npm install -g ccusage` (for billing block fallback on API-key accounts)

### Installation

```bash
# Recommended
uv tool install git+https://github.com/AX-Surfers/claude-auth-shift.git

# or pipx
pipx install git+https://github.com/AX-Surfers/claude-auth-shift.git
```

Then run the one-shot setup:

```bash
cshift-setup
```

This installs `ccusage` via npm and wires `cshift` and `cshift-hud` into `~/.claude/settings.json`. Safe to re-run.

---

### cswap — Account Management

```bash
# Register the currently logged-in Claude Code account
cswap --add-account

# Register an API-key account
cswap --add-token sk-ant-api03-...

# List all accounts
cswap --list
cswap --list --json

# Switch accounts
cswap --switch                       # rotate to next
cswap --switch --strategy best       # pick account with most quota remaining
cswap --switch-to 2                  # by number
cswap --switch-to user@example.com   # by email

# Run Claude Code as a specific account in this terminal only
cswap run 2

# Show current account usage
cswap --status
cswap --status --json
```

After switching, restart Claude Code (or reopen the VS Code tab) to pick up the new credentials immediately. On macOS the Keychain cache auto-expires within a few minutes.

---

### cshift — Auto-Switch on Usage Limit

`cshift` runs as a Claude Code **Stop hook** and switches to the account with the most remaining quota before you hit a wall.

Wire it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "cshift", "timeout": 30 }]
      }
    ]
  }
}
```

Configure in `~/.claude/cshift.json`:

```json
{
  "pct_threshold": 90,
  "cooldown_minutes": 30,
  "enabled": true
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `pct_threshold` | `90` | Switch when active account exceeds this % of 5h quota |
| `cooldown_minutes` | `30` | Minimum minutes between auto-switches |
| `enabled` | `true` | Set to `false` to disable without removing the hook |

---

### cshift-hud — Status Bar

Live status bar for Claude Code. Shows OAuth rate limits, session duration, context fill, Codex usage, and per-account quota in one line.

```
[#1 jocodingax]  5h:84%(4h7m) wk:11%  |  session:32m  |  ctx:34%  |  codex:1%  |  🟢#1*:34%  ⚪#2:?
```

| Segment | Meaning |
|---------|---------|
| `[#1 jocodingax]` | Active account and domain label |
| `5h:84%(4h7m)` | 5-hour rolling rate limit used; resets in 4h 7m |
| `wk:11%` | Weekly rate limit used |
| `session:32m` | Current session duration |
| `ctx:34%` | Context window fill |
| `codex:1%` | Codex CLI 5h rate limit used (OpenAI rate limit %) |
| `🟢#1*:34%` | Account 1 (active `*`), 34% of 5h quota used |
| `⚪#2:?` | Account 2, usage unavailable |

Color thresholds: green < 70%, yellow 70–89%, red ≥ 90%.

Wire it in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

The HUD uses a cache-first design: the hot path prints the last cached line immediately (<100 ms), and a background process refreshes data every 30 s. Data sources:

- Claude OAuth quota (`api.anthropic.com/api/oauth/usage`) — direct HTTP, cached 15 min
- Per-account usage — reads `ClaudeAccountSwitcher` directly (no subprocess)
- Codex usage — reads `~/.codex/sessions/YYYY/MM/DD/*.jsonl` directly (no subprocess)
- API-key billing blocks — `ccusage blocks --active` (only fallback for API-key accounts)

| Env var | Default | Description |
|---------|---------|-------------|
| `CSHIFT_HUD_TTL` | `30` | Seconds between background refreshes |
| `CSHIFT_HUD_CACHE_DIR` | `~/.claude/cshift-hud` | Override cache directory |

---

### /cshift — Slash Command

A Claude Code slash command for quick manual switching. Available in all projects as a user-scope command (`~/.claude/commands/cshift.md`).

```
/cshift      → rotates to the next account (cswap --switch)
/cshift 2    → switches to account #2 (cswap --switch-to 2)
```

---

### Complete settings.json

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "cshift", "timeout": 30 }]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

---

### Data locations

| Path | Contents |
|------|----------|
| `~/.claude.json` | Active account config (managed by Claude Code) |
| `~/.claude/.credentials.json` | OAuth token (Linux/WSL/Windows) |
| `~/.claude/cshift.json` | cshift and cshift-hud config |
| `~/.claude/cshift-hud/` | HUD cache files |
| `~/.claude-swap-backup/` | Per-account credential and config backups (macOS/Windows) |
| `$XDG_DATA_HOME/claude-swap/` | Backup root on Linux |

---

### License

MIT

---
---

<a name="한국어"></a>

## 한국어

Claude Code 다중 계정 관리 도구입니다. 사용량 한도 도달 시 자동 전환하고, 상태 바에서 실시간으로 사용량을 확인할 수 있습니다.

| 명령어 | 용도 |
|--------|------|
| `cswap` | 수동 계정 전환 및 관리 |
| `cshift` | Stop 훅: 사용량 임계치 초과 시 자동 전환 |
| `cshift-hud` | 사용 한도·세션·컨텍스트·Codex 사용량을 상태 바에 표시 |
| `cshift-setup` | `settings.json` 자동 설정 원클릭 설치 |
| `/cshift [N]` | 슬래시 커맨드: N번 계정으로 전환 또는 인자 없으면 로테이트 |

### 요구 사항

- Python 3.12 이상
- Claude Code (CLI, VS Code, 또는 JetBrains 확장)
- [ccusage](https://github.com/ryoppippi/ccusage) — `npm install -g ccusage` (API 키 계정의 billing block 폴백용)

### 설치

```bash
# 권장
uv tool install git+https://github.com/AX-Surfers/claude-auth-shift.git

# 또는 pipx
pipx install git+https://github.com/AX-Surfers/claude-auth-shift.git
```

원클릭 설정:

```bash
cshift-setup
```

ccusage를 npm으로 설치하고, `cshift` 훅과 `cshift-hud` 상태 바를 `~/.claude/settings.json`에 자동으로 추가합니다. 여러 번 실행해도 안전합니다.

---

### cswap — 계정 관리

```bash
# 현재 로그인된 Claude Code 계정 등록
cswap --add-account

# API 키 계정 등록
cswap --add-token sk-ant-api03-...

# 계정 목록 확인
cswap --list
cswap --list --json

# 계정 전환
cswap --switch                       # 다음 계정으로 순환
cswap --switch --strategy best       # 잔여 사용량이 가장 많은 계정으로 전환
cswap --switch-to 2                  # 번호로 지정
cswap --switch-to user@example.com   # 이메일로 지정

# 현재 터미널에서만 특정 계정으로 실행
cswap run 2

# 현재 계정 사용량 확인
cswap --status
cswap --status --json
```

전환 후 Claude Code를 재시작(또는 VS Code 탭 새로고침)하면 즉시 적용됩니다. macOS에서는 Keychain 캐시가 몇 분 내에 자동 만료됩니다.

---

### cshift — 사용량 한도 자동 전환

`cshift`는 Claude Code **Stop 훅**으로 동작하며, 사용량이 임계치에 도달하면 잔여 사용량이 가장 많은 계정으로 자동 전환합니다.

`~/.claude/settings.json` 설정:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "cshift", "timeout": 30 }]
      }
    ]
  }
}
```

`~/.claude/cshift.json` 설정:

```json
{
  "pct_threshold": 90,
  "cooldown_minutes": 30,
  "enabled": true
}
```

| 키 | 기본값 | 설명 |
|----|--------|------|
| `pct_threshold` | `90` | 5시간 사용량이 이 % 초과 시 전환 |
| `cooldown_minutes` | `30` | 자동 전환 최소 간격 (분) |
| `enabled` | `true` | `false`로 설정하면 훅 제거 없이 비활성화 |

---

### cshift-hud — 상태 바

Claude Code 상태 바에 OAuth 사용 한도, 세션 시간, 컨텍스트 사용률, Codex CLI 사용량, 계정별 사용량을 한 줄로 표시합니다.

```
[#1 jocodingax]  5h:84%(4h7m) wk:11%  |  session:32m  |  ctx:34%  |  codex:1%  |  🟢#1*:34%  ⚪#2:?
```

| 항목 | 의미 |
|------|------|
| `[#1 jocodingax]` | 활성 계정과 도메인 레이블 |
| `5h:84%(4h7m)` | 5시간 롤링 사용률; 4시간 7분 후 초기화 |
| `wk:11%` | 주간 사용률 |
| `session:32m` | 현재 세션 경과 시간 |
| `ctx:34%` | 컨텍스트 윈도우 사용률 |
| `codex:1%` | Codex CLI 5시간 사용률 (OpenAI 실제 rate limit %) |
| `🟢#1*:34%` | 계정 1 (활성 `*`), 5시간 사용량의 34% 소비 |
| `⚪#2:?` | 계정 2, 사용량 데이터 없음 |

색상 임계치: 초록 < 70%, 노랑 70–89%, 빨강 ≥ 90%.

`~/.claude/settings.json` 설정:

```json
{
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

HUD 동작 방식:
- **핫 패스** — 마지막 캐시 값을 즉시 출력 (<100 ms)
- **백그라운드** — 30초마다 데이터를 갱신해 캐시에 저장

데이터 소스:
- Claude OAuth 한도 (`api.anthropic.com/api/oauth/usage`) — 직접 HTTP, 15분 캐시
- 계정별 사용량 — `ClaudeAccountSwitcher` 직접 호출 (subprocess 없음)
- Codex 사용량 — `~/.codex/sessions/YYYY/MM/DD/*.jsonl` 직접 읽기 (subprocess 없음)
- API 키 billing block — `ccusage blocks --active` (API 키 계정 폴백 전용)

| 환경 변수 | 기본값 | 설명 |
|-----------|--------|------|
| `CSHIFT_HUD_TTL` | `30` | 백그라운드 갱신 간격 (초) |
| `CSHIFT_HUD_CACHE_DIR` | `~/.claude/cshift-hud` | 캐시 디렉터리 오버라이드 |

---

### /cshift — 슬래시 커맨드

빠른 수동 전환을 위한 Claude Code 슬래시 커맨드입니다. 유저 스코프 커맨드(`~/.claude/commands/cshift.md`)로 설치되어 모든 프로젝트에서 사용 가능합니다.

```
/cshift      → 다음 계정으로 로테이트 (cswap --switch)
/cshift 2    → 2번 계정으로 전환 (cswap --switch-to 2)
```

---

### settings.json 전체 예시

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "cshift", "timeout": 30 }]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

---

### 데이터 위치

| 경로 | 내용 |
|------|------|
| `~/.claude.json` | 활성 계정 설정 (Claude Code가 관리) |
| `~/.claude/.credentials.json` | OAuth 토큰 (Linux/WSL/Windows) |
| `~/.claude/cshift.json` | cshift 및 cshift-hud 설정 |
| `~/.claude/cshift-hud/` | HUD 캐시 파일 |
| `~/.claude-swap-backup/` | 계정별 자격증명·설정 백업 (macOS/Windows) |
| `$XDG_DATA_HOME/claude-swap/` | Linux 백업 루트 |

---

### 라이선스

MIT
