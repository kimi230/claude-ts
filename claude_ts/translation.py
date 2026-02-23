"""Translation engine: source language ↔ English via Claude or Ollama."""

from __future__ import annotations

import re
import subprocess
import sys

from claude_ts.state import (
    config, clean_env, load_language, detect_language, get_ui_string,
    MAX_CONTEXT_TURNS, MAX_CONTEXT_CHARS,
)
from claude_ts.ui import C, error
from claude_ts.ollama import _ollama_generate


# ── Translation Prompt Suffixes (language-agnostic) ─────────────────────────

_TO_EN_SUFFIX_CTX = """
<context>
{context}
</context>
<translate>
{text}
</translate>
"""

_TO_EN_SUFFIX = """
<translate>
{text}
</translate>
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
        text = text.replace(f"`[LINK:{i}]`", f"[{title}]({url})")
        text = text.replace(f"[LINK:{i}]", f"[{title}]({url})")
    return text


# ── Helpers ─────────────────────────────────────────────────────────────────

def contains_target_language(text: str) -> bool:
    """Check if text contains characters from the configured language."""
    if not config.language:
        # Fallback: Korean detection for backwards compatibility
        return bool(re.search(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]", text))
    try:
        lang_data = load_language(config.language)
        return bool(re.search(lang_data["detect_regex"], text))
    except (FileNotFoundError, KeyError):
        return False




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


def _get_prompts() -> tuple[str, str]:
    """Get (to_en_prompt, from_en_prompt) from language config."""
    if not config.language:
        # Should not happen after init, but fallback to Korean
        config.language = "ko"
    lang_data = load_language(config.language)
    return lang_data["to_en_prompt"], lang_data["from_en_prompt"]


# ── Translation Engine ──────────────────────────────────────────────────────

def translate(text: str, direction: str,
              conversation_context: list[dict[str, str]] | None = None) -> str:
    """Translate text via claude -p --model haiku (stdin-based)."""
    to_en_prompt, from_en_prompt = _get_prompts()

    # Protect markdown links from being mangled during en→target translation
    shielded_links: list[tuple[str, str]] = []
    if direction == "en2kr":
        text, shielded_links = _shield_links(text)

    if direction == "kr2en":
        ctx = _build_context_block(conversation_context or [])
        if ctx:
            prompt = to_en_prompt + _TO_EN_SUFFIX_CTX.format(
                context=ctx, text=text
            )
        else:
            prompt = to_en_prompt + _TO_EN_SUFFIX.format(text=text)
    else:
        prompt = from_en_prompt + f"\n<translate>\n{text}\n</translate>"

    # ── Ollama backend ──
    if config.translate_backend == "ollama" and config.ollama_model:
        if direction == "kr2en":
            ollama_system = to_en_prompt
        else:
            ollama_system = from_en_prompt
        translated = _ollama_generate(text, config.ollama_model, system=ollama_system)
        if translated:
            if shielded_links:
                translated = _unshield_links(translated, shielded_links)
            return translated
        error(get_ui_string("translation_failed", "Translation failed — returning original"))
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
            error(f"{get_ui_string('translation_failed', 'Translation failed')} (exit: {result.returncode})")
            if config.debug and result.stderr:
                print(f"{C.DIM}{result.stderr}{C.RESET}", file=sys.stderr)
            return text
    except subprocess.TimeoutExpired:
        error(get_ui_string("translation_timeout", "Translation timeout (120s)"))
        return text
    except FileNotFoundError:
        error(get_ui_string("claude_not_found", "claude command not found"))
        sys.exit(1)
