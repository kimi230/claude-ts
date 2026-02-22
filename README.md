# claude-ts

Multilingual translation proxy for Claude Code.

## Why?

Claude Code works best in English. When you interact in other languages, you waste tokens on multilingual overhead and get worse results — Claude spends context on translating instead of reasoning.

**claude-ts** solves this by adding a cheap translation layer: your input gets translated to English before reaching Claude Code, and the response gets translated back. Claude Code always works in English internally, so it reasons better and uses fewer tokens.

The translation uses Haiku (or a local Ollama model), which costs a fraction of what Opus/Sonnet costs. You get native-language UX without the performance penalty.

```
You (any language) → Haiku/Ollama (→ EN) → Claude Code (EN context) → Haiku/Ollama (→ your language) → You
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
pip install claude-ts
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

# Specify work model (passed to Claude Code)
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
| `/clear` | Start new session |
| `/yolo` | Allow all tools |
| `/export` | Save conversation as markdown |
| `/copy` | Copy last response to clipboard |
| `/stats` | Session statistics |
| `/compact` | Compact conversation context |
| `/init` | Initialize CLAUDE.md |
| `/memory` | Edit CLAUDE.md |
| `/ollama` | Switch translation backend (claude/ollama) |
| `/rename` | Rename session |
| `/resume` | Resume previous session |
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

## Using Ollama for Translation

By default, translation uses Claude Haiku (API calls). You can use a local Ollama model instead — completely free, no API costs for translation.

**Setup:**

1. Install Ollama: https://ollama.com
2. Pull a model: `ollama pull gemma3:4b`
3. Use it:

```bash
# Via CLI flag (saved automatically for next time)
claude-ts --ollama gemma3:4b

# Or switch inside REPL
/ollama
```

The setting persists in `~/.claude-ts/config.json` — once set, you don't need the flag again.

**Recommended models**: `gemma3:4b` (fast, good quality), `gemma3:12b` (better quality)

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
