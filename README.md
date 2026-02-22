# claude-ts

Multilingual translation proxy for Claude Code. Translates user input to English and Claude's output back to your language in real-time, keeping Claude Code's working context in English for optimal performance.

```
User (any language) → Haiku (translate → EN) → Claude Code (EN) → Haiku (EN → translate) → User (any language)
```

## Supported Languages

| Code | Language |
|------|----------|
| `ko` | 한국어 (Korean) |
| `ja` | 日本語 (Japanese) |
| `zh` | 中文 (Chinese) |
| `th` | ไทย (Thai) |
| `hi` | हिन्दी (Hindi) |
| `ar` | العربية (Arabic) |
| `bn` | বাংলা (Bengali) |
| `ru` | Русский (Russian) |

## Install

```bash
pip install git+https://github.com/kimi230/claude-kr.git

# Optional: accurate token counting
pip install tiktoken
```

Requires [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code).

## Usage

```bash
# Interactive REPL — language is selected on first run
claude-ts

# Single prompt
claude-ts "이 프로젝트 구조 설명해줘"

# Specify language
claude-ts --lang ja "このプロジェクトを説明して"
claude-ts --lang zh "解释这个项目"

# Specify model
claude-ts -m opus "복잡한 리팩토링 해줘"

# All permissions
claude-ts --yolo "전체 허용 모드로 작업"

# Use local Ollama for translation (instead of Haiku)
claude-ts --ollama gemma3:4b "로컬 번역 사용"

# Debug mode
claude-ts --debug "번역 과정 확인"
```

## CLI Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `prompt` | | | Prompt in your language (empty = REPL) |
| `--model` | `-m` | default | Work model (opus, sonnet, haiku) |
| `--translate-model` | `-t` | haiku | Translation model |
| `--lang` | | auto | Language code (ko, ja, zh, th, hi, ar, bn, ru) |
| `--ollama` | | | Use Ollama model for translation |
| `--debug` | | off | Debug mode |
| `--allow` | | | Allowed tools (`"Edit Write Bash"`) |
| `--yolo` | | off | Skip all permission checks |

## Slash Commands

Type `/` in REPL to open an interactive menu with arrow-key navigation and type-to-filter.

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/cost` | Token usage and cost |
| `/model` | Change model (interactive) |
| `/lang` | Change language |
| `/img` | Analyze clipboard image |
| `/allow` | Change tool permissions (checkbox) |
| `/debug` | Toggle debug mode |
| `/reset` | Start new session |
| `/yolo` | Allow all tools |
| `/export` | Save conversation as markdown |
| `/copy` | Copy last response to clipboard |
| `/stats` | Session statistics |
| `/compact` | Compact conversation context |
| `/config` | Open Claude Code settings |
| `/init` | Initialize CLAUDE.md |
| `/memory` | Edit CLAUDE.md |
| `/ollama` | Switch translation backend (claude/ollama) |
| `/rename` | Rename session |
| `/doctor` | Check installation health |
| `/exit` | Exit |

## Special Input

| Input | Behavior |
|-------|----------|
| `raw:<text>` | Send without translation |
| English input | Auto-detected, translation skipped |
| Drag & drop image | Auto-detected, prompts for question |
| `/img [question]` | Clipboard image + question |
| Multi-line paste | Auto-detected (bracketed paste) |

## Agent Tree

Real-time visualization of Claude Code's tool execution:

```
🤖 Orchestrator [opus]
│
├── ⏺ Thinking (1.2K tokens) ✓ 3.2s
├── 🔍 Glob: **/*.ts ✓
├── 📄 Read: src/main.ts ✓ 0.3s
├── 🔀 #1 [sonnet] API analysis
│   ├── 🌐 WebSearch: REST API patterns ✓ 2.1s
│   └── 📄 Read: docs/api.md ✓
├── ✏️  Edit: src/main.ts ✓
│      (+3/-1 lines)
│      - const old = "value"
│      + const new = "updated"
├── ⚡ Bash: npm test ✓ 5.4s
│
│   📊 Tokens: Input 12.3K / Output 3.4K / Cache 8.1K (Total 15.7K · $0.0234)
└── ✅ Done (6 tools, 1 thinking, 1 sub-agent)
```

- Real-time spinner animation
- Tool-specific icons
- Elapsed time per tool
- Edit diff preview
- Auto-collapse for repeated tools (`Grep ×12 ✓`)
- Nested sub-agent tree display

## Translation Engine

- Uses last 3 turns of conversation context to resolve pronouns and references accurately
- Preserves code blocks, inline code, file paths, CLI commands, and URLs
- Protects markdown links with placeholders during translation
- Keeps technical terms (API, JWT, middleware, etc.) in English
- Supports local Ollama models as translation backend

## Dependencies

- **Required**: `rich`, [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- **Optional**: `tiktoken` (accurate token counting)

## License

MIT
