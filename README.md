# claude-kr

Claude Code를 한국어로 사용하기 위한 래퍼. 입출력을 실시간 번역하되, Claude Code의 작업 컨텍스트는 영어로만 유지하여 성능 저하 없이 한국어 인터페이스를 제공한다.

```
사용자(한국어) → haiku(한→영) → Claude Code(영어) → haiku(영→한) → 사용자(한국어)
```

## 설치

```bash
# 의존성
pip install rich
pip install tiktoken  # 선택: 정확한 토큰 추정

# claude CLI 필요
# https://docs.anthropic.com/en/docs/claude-code

# 설치
curl -o ~/.local/bin/claude-kr https://raw.githubusercontent.com/kimi230/claude-kr/main/claude-kr
chmod +x ~/.local/bin/claude-kr
```

## 사용법

```bash
# REPL 모드 (대화형)
claude-kr

# 단일 질문
claude-kr "이 프로젝트 구조 설명해줘"

# 모델 지정
claude-kr -m opus "복잡한 리팩토링 해줘"
claude-kr -m sonnet "간단한 유틸 함수 만들어줘"

# 권한 설정
claude-kr --yolo "전체 허용 모드로 작업"
claude-kr --allow "Edit Write Bash" "코드 수정해줘"

# 디버그
claude-kr --debug "번역 과정 확인하면서 작업"
```

## CLI 옵션

| 옵션 | 단축 | 기본값 | 설명 |
|------|------|--------|------|
| `prompt` | | | 한국어 프롬프트 (없으면 REPL) |
| `--model` | `-m` | default | 작업 모델 (opus, sonnet, haiku) |
| `--translate-model` | `-t` | haiku | 번역 모델 |
| `--debug` | | off | 디버그 모드 |
| `--allow` | | | 허용 도구 (`"Edit Write Bash"`) |
| `--yolo` | | off | 전체 권한 허용 |

## 슬래시 명령어

`/`를 입력하면 화살표 키로 탐색하고 타이핑으로 필터링할 수 있는 메뉴가 즉시 표시된다.

| 명령어 | 설명 |
|--------|------|
| `/help` | 도움말 |
| `/cost` | 토큰 사용량 및 비용 |
| `/model` | 모델 변경 (인터랙티브 선택) |
| `/img` | 클립보드 이미지 분석 |
| `/allow` | 도구 권한 변경 (체크박스 선택) |
| `/debug` | 디버그 모드 토글 |
| `/reset` | 새 세션 시작 |
| `/yolo` | 전체 허용 모드 |
| `/export` | 대화 내역 마크다운 저장 |
| `/copy` | 마지막 응답 클립보드 복사 |
| `/stats` | 세션 통계 시각화 |
| `/compact` | 컨텍스트 압축 |
| `/config` | Claude Code 설정 열기 |
| `/init` | CLAUDE.md 초기화 |
| `/memory` | CLAUDE.md 편집 |
| `/rename` | 세션 이름 변경 |
| `/doctor` | 설치 상태 확인 |
| `/exit` | 종료 |

## 특수 입력

| 입력 | 동작 |
|------|------|
| `raw:<텍스트>` | 번역 없이 직접 전송 |
| 영어 입력 | 자동 감지, 번역 생략 |
| 이미지 드래그앤드롭 | 자동 감지 → 질문 입력 |
| `/img [질문]` | 클립보드 이미지 + 질문 |
| 멀티라인 붙여넣기 | 자동 감지 (bracketed paste) |

## 에이전트 트리

Claude Code의 도구 실행 과정을 실시간 트리로 시각화한다.

```
🤖 Orchestrator [opus]
│
├── ⏺ 생각 (1.2K tokens) ✓ 3.2s
├── 🔍 Glob: **/*.ts ✓
├── 📄 Read: src/main.ts ✓ 0.3s
├── 🔀 #1 [sonnet] API 분석
│   ├── 🌐 WebSearch: REST API patterns ✓ 2.1s
│   └── 📄 Read: docs/api.md ✓
├── ✏️  Edit: src/main.ts ✓
│      (+3/-1 lines)
│      - const old = "value"
│      + const new = "updated"
├── ⚡ Bash: npm test ✓ 5.4s
│
│   📊 토큰: 입력 12.3K / 출력 3.4K / 캐시 8.1K (총 15.7K · $0.0234)
└── ✅ 완료 (도구 6회, 생각 1회, 서브에이전트 1개)
```

- 실시간 스피너 애니메이션
- 도구별 아이콘 (⚡📄✏️📝🔍🔎🔀🌐📓)
- 완료 시간 표시
- Edit 도구의 diff 미리보기
- 동일 도구 4회 이상 연속 시 자동 접기 (`Grep ×12 ✓`)
- 서브에이전트 트리 중첩 표시

## 클릭 가능한 링크

응답에 포함된 URL과 도메인이 터미널에서 클릭 가능하다 (OSC 8).

- `https://...` 풀 URL
- `naver.com`, `github.com` 등 bare 도메인
- `[Title](URL)` 마크다운 링크 (번역 과정에서 보호)

지원 터미널: iTerm2, macOS Terminal (Ventura+), Warp, Windows Terminal

## 권한 시스템

REPL 시작 시 도구 권한을 설정한다:

1. **선택 허용** — 화살표 키 + 체크박스로 허용할 도구 선택
2. **전체 허용** — 모든 도구 자동 허용 (`--yolo`)

런타임에 `/allow`로 변경하거나 `/yolo`로 전환 가능.

## 번역 엔진

- 최근 3턴의 대화 컨텍스트를 활용하여 "해당", "그것", "이거" 등의 지시어를 정확히 번역
- 코드 블록, 인라인 코드, 파일 경로, CLI 명령어, URL 등은 번역하지 않음
- 마크다운 링크는 번역 전 플레이스홀더로 보호 후 복원
- 기술 용어(API, JWT, middleware 등)는 영어 유지

## 의존성

- **필수**: `rich`, `claude` CLI
- **선택**: `tiktoken` (정확한 토큰 카운트)

## 라이선스

MIT
