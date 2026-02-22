"""CLI entry point: argparse and main()."""

import argparse
import readline  # noqa: F401 — imported for side-effect (enables line editing)
import subprocess
import sys

from claude_ts.state import config, SessionState, clean_env, init_language, init_translation_backend, get_ui_string, save_user_config
from claude_ts.ui import dim, error
from claude_ts.ollama import _ollama_available, _ollama_list_models
from claude_ts.executor import process_turn
from claude_ts.repl import repl
from claude_ts.setup import select_language


HELP_EPILOG = """\
REPL commands (type / for interactive menu):
  /help          Show help
  /clear         Clear conversation (= /reset)
  /compact       Compact conversation context
  /copy          Copy last response to clipboard
  /cost          Show token usage
  /doctor        Check installation health
  /export [file] Export conversation to file
  /init          Initialize CLAUDE.md
  /lang          Change language
  /memory        Edit CLAUDE.md
  /model [name]  Change work model
  /ollama        Switch translation backend (claude/ollama)
  /rename [name] Rename session
  /stats         Show session statistics
  /img [question] Clipboard image analysis
  /allow [tools] Change allowed tools
  /debug         Toggle debug mode
  /yolo          Allow all tools
  /exit          Exit

Special input:
  raw:<text>     Send without translation
  Drag & drop image files for auto-detection

Examples:
  claude-ts "explain this project structure"
  claude-ts --model sonnet "make a simple utility function"
  claude-ts --lang th "สวัสดี"
  claude-ts --yolo "work without permission checks"
  claude-ts --allow "Edit Write Bash" "edit the code"
  claude-ts --ollama gemma3:4b "translate with local model"
"""


def main():
    parser = argparse.ArgumentParser(
        description="claude-ts — Multilingual translation proxy for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument("prompt", nargs="*", help="Prompt in your language (empty = REPL mode)")
    parser.add_argument("-m", "--model", default="opus", help="Work model (default: opus)")
    parser.add_argument(
        "-t", "--translate-model", default="haiku", help="Translation model (default: haiku)"
    )
    parser.add_argument(
        "--lang", default="", help="Language code (e.g. ko, th, hi, ar, ru, ja, zh)"
    )
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    parser.add_argument(
        "--allow", default="", help="Allowed tools (e.g. \"Edit Write Bash\")"
    )
    parser.add_argument(
        "--yolo", action="store_true",
        help="Skip all permission checks (--dangerously-skip-permissions)"
    )
    parser.add_argument(
        "--ollama", metavar="MODEL", default="",
        help="Use Ollama model for translation (e.g. gemma3:4b)"
    )

    args = parser.parse_args()

    config.main_model = args.model
    config.translate_model = args.translate_model
    config.debug = args.debug
    config.allowed_tools = args.allow
    config.dangerously_skip_permissions = args.yolo

    # ── Language initialization ──
    if args.lang:
        # Explicit --lang flag overrides saved config
        config.language = args.lang
    elif not init_language():
        # No saved config — first-run setup
        if sys.stdin.isatty():
            select_language()
        else:
            # Non-interactive: default to Korean for backwards compatibility
            config.language = "ko"

    # Load saved translation backend (ollama settings persist across restarts)
    if not args.ollama:
        init_translation_backend()

    # Ollama backend setup (CLI flag overrides saved config)
    if args.ollama:
        if not _ollama_available():
            error(get_ui_string("ollama_not_installed", "Ollama is not installed. https://ollama.com"))
            sys.exit(1)
        available = _ollama_list_models()
        if args.ollama not in available:
            error(get_ui_string("ollama_model_not_found", f"Ollama model '{args.ollama}' not found."))
            if available:
                dim(f"Available models: {', '.join(available)}")
            else:
                dim(get_ui_string("no_models_installed", "No models installed. Run: ollama pull <model>"))
            sys.exit(1)
        config.translate_backend = "ollama"
        config.ollama_model = args.ollama
        save_user_config({"translate_backend": "ollama", "ollama_model": args.ollama})

    # Verify claude is available
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            timeout=10,
            env=clean_env(),
        )
    except FileNotFoundError:
        error(get_ui_string("claude_not_found", "claude command not found"))
        error("Install: https://docs.anthropic.com/en/docs/claude-code")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        pass

    # Single-turn mode
    if args.prompt:
        state = SessionState()
        process_turn(" ".join(args.prompt), state)
        return

    # Interactive REPL mode
    repl()
