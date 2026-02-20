"""CLI entry point: argparse and main()."""

import argparse
import readline  # noqa: F401 — imported for side-effect (enables line editing)
import subprocess
import sys

from claude_kr.state import config, SessionState, clean_env
from claude_kr.ui import dim, error
from claude_kr.ollama import _ollama_available, _ollama_list_models
from claude_kr.executor import process_turn
from claude_kr.repl import repl


HELP_EPILOG = """\
REPL commands (type / for interactive menu):
  /help          Show help
  /clear         Clear conversation (= /reset)
  /compact       Compact conversation context
  /config        Open Claude Code settings
  /copy          Copy last response to clipboard
  /cost          Show token usage
  /doctor        Check installation health
  /export [file] Export conversation to file
  /init          Initialize CLAUDE.md
  /memory        Edit CLAUDE.md
  /model [name]  Change work model
  /ollama        Switch translation backend (claude/ollama)
  /rename [name] Rename session
  /stats         Show session statistics
  /img [질문]     Clipboard image analysis
  /allow [tools] Change allowed tools
  /debug         Toggle debug mode
  /yolo          Allow all tools
  /exit          Exit

Special input:
  raw:<text>     Send without translation
  Drag & drop image files for auto-detection

Examples:
  claude-kr "이 프로젝트 구조 설명해줘"
  claude-kr --model sonnet "간단한 유틸 함수 만들어줘"
  claude-kr --debug "src/auth.ts에 JWT 검증 추가해줘"
  claude-kr --yolo "파일 수정 권한 걱정 없이 작업해줘"
  claude-kr --allow "Edit Write Bash" "코드 수정해줘"
  claude-kr --ollama gemma3:4b "로컬 모델로 번역"
"""


def main():
    parser = argparse.ArgumentParser(
        description="claude-kr — Korean ↔ English wrapper for Claude Code (streaming)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument("prompt", nargs="*", help="한국어 프롬프트 (없으면 REPL 모드)")
    parser.add_argument("-m", "--model", default="", help="작업 모델 (default: claude default)")
    parser.add_argument(
        "-t", "--translate-model", default="haiku", help="번역 모델 (default: haiku)"
    )
    parser.add_argument("--debug", action="store_true", help="디버그 모드")
    parser.add_argument(
        "--allow", default="", help="허용 도구 (e.g. \"Edit Write Bash\")"
    )
    parser.add_argument(
        "--yolo", action="store_true",
        help="모든 권한 검사 건너뛰기 (--dangerously-skip-permissions)"
    )
    parser.add_argument(
        "--ollama", metavar="MODEL", default="",
        help="Ollama 모델로 번역 (e.g. gemma3:4b)"
    )

    args = parser.parse_args()

    config.main_model = args.model
    config.translate_model = args.translate_model
    config.debug = args.debug
    config.allowed_tools = args.allow
    config.dangerously_skip_permissions = args.yolo

    # Ollama backend setup
    if args.ollama:
        if not _ollama_available():
            error("Ollama가 설치되지 않았습니다. https://ollama.com 에서 설치하세요.")
            sys.exit(1)
        available = _ollama_list_models()
        if args.ollama not in available:
            error(f"Ollama 모델 '{args.ollama}'을(를) 찾을 수 없습니다.")
            if available:
                dim(f"사용 가능한 모델: {', '.join(available)}")
            else:
                dim("설치된 모델이 없습니다. ollama pull <model>로 다운로드하세요.")
            sys.exit(1)
        config.translate_backend = "ollama"
        config.ollama_model = args.ollama

    # Verify claude is available
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            timeout=10,
            env=clean_env(),
        )
    except FileNotFoundError:
        error("claude 명령어를 찾을 수 없습니다.")
        error("설치: https://docs.anthropic.com/en/docs/claude-code")
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
