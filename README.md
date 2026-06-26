# claude-auth-shift

> **[English](#english) | [한국어](#한국어)**

---

<a name="english"></a>

## English

Multi-account manager for Claude Code. Switch between Claude accounts automatically when you hit rate limits, and monitor usage in the status bar — all without leaving your editor.

Ships three commands:

| Command | Purpose |
|---------|---------|
| `cswap` | Manual account switching and management |
| `cshift` | Auto-switch via Stop hook when quota hits threshold |
| `cshift-hud` | Live status bar showing limits, session, context, and per-account usage |

### Requirements

- Python 3.12+
- Claude Code (CLI or VS Code extension)
- [ccusage](https://github.com/ryoppippi/ccusage) — `npm install -g ccusage` (required for `cshift` and `cshift-hud`)

### Installation

```bash
# Recommended — uv tool (isolated environment)
uv tool install git+https://github.com/AX-Surfers/claude-auth-shift.git

# or pipx
pipx install git+https://github.com/AX-Surfers/claude-auth-shift.git
```

Then run the one-shot setup:

```bash
cshift-setup
```

This installs `ccusage` via npm and wires `cshift` + `cshift-hud` into `~/.claude/settings.json` automatically. Safe to re-run.

Verify:

```bash
which cswap cshift cshift-hud
```

---

### cswap — Account Management

#### Add accounts

Log in to Claude Code with your first account, then register it:

```bash
cswap --add-account
```

Repeat for every additional account.

For API-key (`sk-ant-api…`) accounts:

```bash
cswap --add-token sk-ant-api03-...
```

#### List accounts

```bash
cswap --list
cswap --list --json    # machine-readable
```

#### Switch accounts

```bash
cswap --switch                       # rotate to next account
cswap --switch --strategy best       # pick account with most quota remaining
cswap --switch-to 2                  # by number
cswap --switch-to user@example.com   # by email
```

After switching, restart Claude Code (or reopen the VS Code extension tab) for the new account to take effect immediately. On macOS the Keychain cache expires on its own within a few minutes.

#### Run as a specific account (session mode)

```bash
cswap run 2                 # open Claude Code as account 2 in this terminal only
cswap run user@example.com
```

Other terminals and the VS Code extension keep using the default account — two accounts can work in parallel.

#### Check status

```bash
cswap --status
cswap --status --json
```

---

### cshift — Auto-Switch on Usage Limit

`cshift` runs as a Claude Code **Stop hook** and automatically switches to the account with the most remaining quota before you hit a rate-limit wall.

#### Setup

1. Install [ccusage](https://github.com/ryoppippi/ccusage):
   ```bash
   npm install -g ccusage
   ```

2. Add to `~/.claude/settings.json`:
   ```json
   {
     "hooks": {
       "Stop": [
         {
           "matcher": "",
           "hooks": [{ "type": "command", "command": "cshift" }]
         }
       ]
     }
   }
   ```

#### Configuration — `~/.claude/cshift.json`

```json
{
  "pct_threshold": 90,
  "cooldown_minutes": 30,
  "enabled": true
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `pct_threshold` | `90` | Switch when active account exceeds this % |
| `cooldown_minutes` | `30` | Minimum minutes between auto-switches |
| `enabled` | `true` | Set to `false` to disable without removing the hook |

---

### cshift-hud — Status Bar

`cshift-hud` is a zero-dependency status bar command that shows OAuth rate limits, session duration, context window usage, and per-account quota — all in one line.

**Example output:**
```
5h:84%(4h7m) wk:11%  |  session:32m  |  ctx:34%  |  🟢#1*:34%  ⚪#2:?
```

- **`5h`** — % of the 5-hour rolling rate limit consumed (from Anthropic OAuth API)
- **`wk`** — % of the weekly rate limit consumed
- **`(4h7m)`** — time until the limit resets
- **`session:32m`** — current session duration
- **`ctx:34%`** — context window fill level
- **`🟢#1*:34%`** — account 1 (active `*`), 34% of 5-hour quota used
- **`⚪#2:?`** — account 2, usage data unavailable

Color coding applies to all percentage values:

| Color | Threshold |
|-------|-----------|
| 🟢 Green | < 70% |
| 🟡 Yellow | 70 – 89% |
| 🔴 Red | ≥ 90% |
| ⚪ Gray | Unavailable |

#### Setup

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

No Node.js, no OMC, no extra configuration needed.

#### How it works

`cshift-hud` uses a cache-first design so the status bar never blocks a turn:

1. **Hot path** — reads and prints the last cached line immediately (~100 ms).
2. **Background** — spawns `cshift-hud --refresh` which fetches `cswap --list`, `cswap --status`, `ccusage blocks --active`, and the Anthropic OAuth usage API concurrently, then writes the result to `~/.claude/cshift-hud/status.txt`.

OAuth data is cached for 5 minutes to avoid API rate limits. If the API returns 429, the last successful result is reused until it expires.

#### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `CSHIFT_HUD_TTL` | `30` | Seconds between background refreshes |
| `CSHIFT_HUD_CACHE_DIR` | `~/.claude/cshift-hud` | Override cache directory |

---

### Complete settings.json example

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
| `~/.claude/.credentials.json` | OAuth tokens (managed by Claude Code) |
| `~/.claude/cshift.json` | cshift configuration |
| `~/.claude/cshift-hud/` | HUD cache files |
| `~/.cshift-cooldown.json` | Auto-switch cooldown state |

---

### License

MIT

---
---

<a name="한국어"></a>

## 한국어

Claude Code 다중 계정 관리 도구입니다. 사용량 한도에 도달했을 때 자동으로 계정을 전환하고, 상태 바에서 사용량을 실시간으로 확인할 수 있습니다.

세 가지 명령어를 제공합니다:

| 명령어 | 용도 |
|--------|------|
| `cswap` | 수동 계정 전환 및 관리 |
| `cshift` | 사용량 임계치 도달 시 Stop 훅을 통한 자동 전환 |
| `cshift-hud` | 사용 한도·세션·컨텍스트·계정별 사용량을 상태 바에 표시 |

### 요구 사항

- Python 3.12 이상
- Claude Code (CLI 또는 VS Code 확장)
- [ccusage](https://github.com/ryoppippi/ccusage) — `npm install -g ccusage` (`cshift`, `cshift-hud` 사용 시 필요)

### 설치

```bash
# 권장 — uv tool (격리된 환경)
uv tool install git+https://github.com/AX-Surfers/claude-auth-shift.git

# 또는 pipx
pipx install git+https://github.com/AX-Surfers/claude-auth-shift.git
```

설치 후 원클릭 설정:

```bash
cshift-setup
```

ccusage를 npm으로 설치하고, `cshift` 훅과 `cshift-hud` 상태 바를 `~/.claude/settings.json`에 자동으로 추가합니다. 여러 번 실행해도 안전합니다.

설치 확인:

```bash
which cswap cshift cshift-hud
```

---

### cswap — 계정 관리

#### 계정 추가

첫 번째 계정으로 Claude Code에 로그인한 후 등록합니다:

```bash
cswap --add-account
```

추가 계정마다 반복합니다.

API 키(`sk-ant-api…`) 계정의 경우:

```bash
cswap --add-token sk-ant-api03-...
```

#### 계정 목록 확인

```bash
cswap --list
cswap --list --json    # JSON 형식
```

#### 계정 전환

```bash
cswap --switch                       # 다음 계정으로 순환 전환
cswap --switch --strategy best       # 잔여 사용량이 가장 많은 계정으로 전환
cswap --switch-to 2                  # 번호로 지정
cswap --switch-to user@example.com   # 이메일로 지정
```

전환 후 Claude Code를 재시작(또는 VS Code 확장 탭 새로고침)하면 즉시 적용됩니다. macOS에서는 Keychain 캐시가 몇 분 내에 자동 만료됩니다.

#### 특정 계정으로 실행 (세션 모드)

```bash
cswap run 2                 # 현재 터미널에서만 계정 2로 Claude Code 실행
cswap run user@example.com
```

다른 터미널과 VS Code 확장은 기본 계정을 유지해, 두 계정을 동시에 사용할 수 있습니다.

#### 상태 확인

```bash
cswap --status
cswap --status --json
```

---

### cshift — 사용량 한도 자동 전환

`cshift`는 Claude Code **Stop 훅**으로 동작하며, 사용량이 설정한 임계치에 도달하면 잔여 사용량이 가장 많은 계정으로 자동 전환합니다.

#### 설정

1. [ccusage](https://github.com/ryoppippi/ccusage) 설치:
   ```bash
   npm install -g ccusage
   ```

2. `~/.claude/settings.json`에 추가:
   ```json
   {
     "hooks": {
       "Stop": [
         {
           "matcher": "",
           "hooks": [{ "type": "command", "command": "cshift" }]
         }
       ]
     }
   }
   ```

#### 설정 파일 — `~/.claude/cshift.json`

```json
{
  "pct_threshold": 90,
  "cooldown_minutes": 30,
  "enabled": true
}
```

| 키 | 기본값 | 설명 |
|----|--------|------|
| `pct_threshold` | `90` | 이 % 초과 시 계정 전환 |
| `cooldown_minutes` | `30` | 자동 전환 최소 간격 (분) |
| `enabled` | `true` | `false`로 설정하면 훅을 제거하지 않고도 비활성화 |

---

### cshift-hud — 상태 바

`cshift-hud`는 별도 의존성 없이 OAuth 사용 한도, 세션 시간, 컨텍스트 윈도우 사용률, 계정별 사용량을 한 줄로 표시하는 상태 바 명령어입니다.

**표시 예시:**
```
5h:84%(4h7m) wk:11%  |  session:32m  |  ctx:34%  |  🟢#1*:34%  ⚪#2:?
```

- **`5h`** — 5시간 롤링 사용 한도 소비율 (Anthropic OAuth API 기준)
- **`wk`** — 주간 사용 한도 소비율
- **`(4h7m)`** — 한도 초기화까지 남은 시간
- **`session:32m`** — 현재 세션 경과 시간
- **`ctx:34%`** — 컨텍스트 윈도우 사용률
- **`🟢#1*:34%`** — 계정 1 (활성 `*`), 5시간 사용량의 34% 소비
- **`⚪#2:?`** — 계정 2, 사용량 데이터 없음

모든 퍼센트 값에 색상 코드가 적용됩니다:

| 색상 | 임계치 |
|------|--------|
| 🟢 초록 | 70% 미만 |
| 🟡 노랑 | 70 – 89% |
| 🔴 빨강 | 90% 이상 |
| ⚪ 회색 | 데이터 없음 |

#### 설정

`~/.claude/settings.json`에 추가:

```json
{
  "statusLine": {
    "type": "command",
    "command": "cshift-hud"
  }
}
```

Node.js, OMC, 별도 설정 불필요.

#### 동작 방식

`cshift-hud`는 캐시 우선 방식으로 동작해 상태 바가 응답을 지연시키지 않습니다:

1. **핫 패스** — 마지막으로 캐시된 줄을 즉시 출력 (~100ms).
2. **백그라운드** — 캐시가 만료되면 `cshift-hud --refresh`를 분리 프로세스로 실행. `cswap --list`, `cswap --status`, `ccusage blocks --active`, Anthropic OAuth API를 동시에 호출하고 결과를 `~/.claude/cshift-hud/status.txt`에 저장.

OAuth 데이터는 5분간 캐시해 API 속도 제한 내에서 유지됩니다. API가 429를 반환하면 마지막 성공 결과를 만료될 때까지 재사용합니다.

#### 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `CSHIFT_HUD_TTL` | `30` | 백그라운드 새로 고침 간격 (초) |
| `CSHIFT_HUD_CACHE_DIR` | `~/.claude/cshift-hud` | 캐시 디렉터리 오버라이드 |

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
| `~/.claude/.credentials.json` | OAuth 토큰 (Claude Code가 관리) |
| `~/.claude/cshift.json` | cshift 설정 |
| `~/.claude/cshift-hud/` | HUD 캐시 파일 |
| `~/.cshift-cooldown.json` | 자동 전환 쿨다운 상태 |

---

### 라이선스

MIT
