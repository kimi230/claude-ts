"""Translation engine: Korean ↔ English via Claude or Ollama."""

import re
import subprocess
import sys

from claude_kr.state import config, clean_env, MAX_CONTEXT_TURNS, MAX_CONTEXT_CHARS
from claude_kr.ui import C, error
from claude_kr.ollama import _ollama_generate


# ── Translation Prompts ─────────────────────────────────────────────────────

KR_TO_EN_PROMPT = """\
Please translate the Korean text inside <translate> tags into clear, precise English.

Guidelines:
- Preserve technical terms, file paths, variable names, and code snippets exactly as-is
- Preserve code blocks and inline code without modification
- If a short command or phrase, keep the translation concise
- If <context> is provided, use it to resolve pronouns/references (해당, 그것, 이거, 위에서 말한) into specific English nouns
- Output only the English translation
"""

KR_TO_EN_PROMPT_SUFFIX_CTX = """
<context>
{context}
</context>
<translate>
{text}
</translate>
"""

KR_TO_EN_PROMPT_SUFFIX = """
<translate>
{text}
</translate>
"""

EN_TO_KR_PROMPT = """\
Please translate the following English text into natural, fluent Korean.

Guidelines:
- Keep these items in their original form (do not translate):
  * Code blocks, inline code, file paths, variable/function/class names
  * CLI commands, flags, URLs, email addresses
  * Error messages, log output, JSON, YAML, or structured data
- Preserve all markdown formatting, line breaks, indentation, and structure
- Common tech terms stay in English: API, JWT, middleware, hook, commit, push, pull,
  merge, branch, deploy, config, schema, endpoint, token, session, REPL, CLI, SDK,
  refactor, debug, lint, build, test, mock, stub, callback, async, sync, etc.
- Output only the Korean translation

Text to translate:
---
"""


# ── Link Handling ───────────────────────────────────────────────────────────

_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')


def _shield_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace markdown links with code-like placeholders to survive translation."""
    links: list[tuple[str, str]] = []

    def _repl(m: re.Match) -> str:
        i = len(links)
        links.append((m.group(1), m.group(2)))
        return f"`[LINK:{i}]`"

    return _MD_LINK_RE.sub(_repl, text), links


def _unshield_links(text: str, links: list[tuple[str, str]]) -> str:
    """Restore markdown links from placeholders."""
    for i, (title, url) in enumerate(links):
        # Try with backticks first, then without (haiku may strip them)
        text = text.replace(f"`[LINK:{i}]`", f"[{title}]({url})")
        text = text.replace(f"[LINK:{i}]", f"[{title}]({url})")
    return text


# ── Helpers ─────────────────────────────────────────────────────────────────

def contains_korean(text: str) -> bool:
    return bool(re.search(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]", text))


def _build_context_block(conversation_context: list[dict[str, str]]) -> str:
    """Build a short context summary from recent conversation turns."""
    if not conversation_context:
        return ""
    lines = []
    for i, turn in enumerate(conversation_context[-MAX_CONTEXT_TURNS:]):
        u = turn.get("user", "")
        a = turn.get("assistant", "")
        if u:
            u_short = u[:150] + ("..." if len(u) > 150 else "")
            lines.append(f"Q{i+1}: {u_short}")
        if a:
            a_short = a[:150] + ("..." if len(a) > 150 else "")
            lines.append(f"A{i+1}: {a_short}")
    block = "\n".join(lines)
    if len(block) > MAX_CONTEXT_CHARS:
        block = block[-MAX_CONTEXT_CHARS:]
    return block


# ── Translation Engine ──────────────────────────────────────────────────────

def translate(text: str, direction: str,
              conversation_context: list[dict[str, str]] | None = None) -> str:
    """Translate text via claude -p --model haiku (stdin-based)."""
    # Protect markdown links from being mangled during en→kr translation
    shielded_links: list[tuple[str, str]] = []
    if direction == "en2kr":
        text, shielded_links = _shield_links(text)

    if direction == "kr2en":
        ctx = _build_context_block(conversation_context or [])
        if ctx:
            prompt = KR_TO_EN_PROMPT + KR_TO_EN_PROMPT_SUFFIX_CTX.format(
                context=ctx, text=text
            )
        else:
            prompt = KR_TO_EN_PROMPT + KR_TO_EN_PROMPT_SUFFIX.format(text=text)
    else:
        prompt = EN_TO_KR_PROMPT + text

    # ── Ollama backend ──
    if config.translate_backend == "ollama" and config.ollama_model:
        translated = _ollama_generate(prompt, config.ollama_model)
        if translated:
            if shielded_links:
                translated = _unshield_links(translated, shielded_links)
            return translated
        error("Ollama 번역 실패 — 원문 반환")
        return text

    # ── Claude backend (default) ──
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", config.translate_model],
            input=prompt,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            translated = result.stdout.strip()
            if shielded_links:
                translated = _unshield_links(translated, shielded_links)
            return translated
        else:
            error(f"번역 실패 (exit: {result.returncode})")
            if config.debug and result.stderr:
                print(f"{C.DIM}{result.stderr}{C.RESET}", file=sys.stderr)
            return text
    except subprocess.TimeoutExpired:
        error("번역 시간 초과 (120초)")
        return text
    except FileNotFoundError:
        error("claude 명령어를 찾을 수 없습니다.")
        sys.exit(1)
