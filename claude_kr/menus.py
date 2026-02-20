"""Slash command menus and interactive selectors."""

import os
import select
import sys
import termios
import tty

from claude_kr.ui import C
from claude_kr.terminal import _read_esc_seq


SLASH_COMMANDS = [
    ("help",    "도움말 표시"),
    ("clear",   "대화 기록 초기화"),
    ("compact", "대화 컨텍스트 압축"),
    ("config",  "Claude Code 설정"),
    ("copy",    "마지막 응답 복사"),
    ("cost",    "토큰 사용량"),
    ("doctor",  "설치 상태 점검"),
    ("export",  "대화 내역 저장"),
    ("init",    "CLAUDE.md 초기화"),
    ("memory",  "CLAUDE.md 편집"),
    ("model",   "모델 변경"),
    ("ollama",  "번역 백엔드 변경"),
    ("rename",  "세션 이름 변경"),
    ("stats",   "세션 통계 시각화"),
    ("img",     "클립보드 이미지"),
    ("allow",   "도구 권한 변경"),
    ("debug",   "디버그 모드 토글"),
    ("reset",   "새 세션 시작"),
    ("yolo",    "전체 허용 모드"),
    ("exit",    "종료"),
]


def _filter_commands(q: str) -> list[tuple[str, str]]:
    if not q:
        return list(SLASH_COMMANDS)
    ql = q.lower()
    return [(c, d) for c, d in SLASH_COMMANDS if ql in c or ql in d]


def slash_menu_raw(fd: int, prompt_str: str) -> str | None:
    """Inline slash command menu in raw terminal mode.

    Called from read_input() when '/' is typed as the first character,
    or from interactive_command_menu() as the unified core implementation.
    Shows a filterable, arrow-navigable menu below the input line.
    Returns the selected command name, or None if cancelled.
    """
    query = ""
    cursor_idx = 0
    filtered = list(SLASH_COMMANDS)
    rendered_h = 0  # lines rendered below input line

    def _menu_h() -> int:
        return max(len(filtered), 1) + 1  # items (or 1 empty msg) + help line

    def _draw(first: bool = False):
        nonlocal rendered_h
        h = _menu_h()

        if first:
            # Create scroll space so menu is visible at bottom of terminal
            sys.stdout.write(("\r\n") * h)
            sys.stdout.write(f"\033[{h}A")
            # Now at input line, col 0
        else:
            # Move up from end of menu to input line
            if rendered_h > 0:
                sys.stdout.write(f"\033[{rendered_h}A")
            sys.stdout.write("\r")

        # Rewrite input line
        sys.stdout.write(f"{prompt_str}/{query}\033[K")

        # Move to first menu line and clear everything below
        sys.stdout.write("\n\033[J")

        # Render menu items
        for i, (cmd, desc) in enumerate(filtered):
            if i > 0:
                sys.stdout.write("\r\n")
            if i == cursor_idx:
                sys.stdout.write(
                    f"  {C.CYAN}›{C.RESET} {C.BOLD}/{cmd:<10}{C.RESET} {desc}"
                )
            else:
                sys.stdout.write(
                    f"    {C.DIM}/{cmd:<10} {desc}{C.RESET}"
                )

        if not filtered:
            sys.stdout.write(f"    {C.DIM}(일치하는 명령 없음){C.RESET}")

        sys.stdout.write(
            f"\r\n  {C.DIM}↑↓ 이동 · Enter 선택 · Esc 취소{C.RESET}"
        )
        sys.stdout.flush()
        rendered_h = h

    def _erase():
        """Remove menu, leave cursor at col 0 of (cleared) input line."""
        nonlocal rendered_h
        if rendered_h > 0:
            sys.stdout.write(f"\033[{rendered_h}A")
        sys.stdout.write("\r\033[J")
        sys.stdout.flush()
        rendered_h = 0

    # Initial render (shows "/" on input line + menu below)
    _draw(first=True)

    try:
        while True:
            b = os.read(fd, 1)
            if not b:
                raise EOFError
            byte = b[0]

            # Escape sequences (arrows, Esc)
            if byte == 0x1B:
                seq = _read_esc_seq(fd)
                if seq == b"\x1b":  # plain Esc → cancel
                    _erase()
                    sys.stdout.write(f"{prompt_str}\033[K")
                    sys.stdout.flush()
                    return None
                if len(seq) >= 3 and seq[1:2] == b"[":
                    if seq[2:3] == b"A" and filtered:   # Up
                        cursor_idx = (cursor_idx - 1) % len(filtered)
                    elif seq[2:3] == b"B" and filtered: # Down
                        cursor_idx = (cursor_idx + 1) % len(filtered)
                    _draw()
                continue

            if byte == 3:  # Ctrl-C → cancel
                _erase()
                sys.stdout.write(f"{prompt_str}\033[K")
                sys.stdout.flush()
                return None

            if byte in (13, 10):  # Enter → select
                selected = None
                if filtered and 0 <= cursor_idx < len(filtered):
                    selected = filtered[cursor_idx][0]
                _erase()
                # Caller (read_input) will display the selected command
                return selected

            if byte in (127, 8):  # Backspace
                if query:
                    query = query[:-1]
                    filtered = _filter_commands(query)
                    cursor_idx = min(cursor_idx, max(len(filtered) - 1, 0))
                    _draw()
                else:
                    # No query left → cancel (removes the "/")
                    _erase()
                    sys.stdout.write(f"{prompt_str}\033[K")
                    sys.stdout.flush()
                    return None
                continue

            if byte < 0x20:  # Other control chars
                continue

            if 0x20 <= byte < 0x7F:  # Printable ASCII → filter
                query += chr(byte)
                filtered = _filter_commands(query)
                cursor_idx = 0
                _draw()
                continue

    except (EOFError, KeyboardInterrupt):
        _erase()
        sys.stdout.write(f"{prompt_str}\033[K")
        sys.stdout.flush()
        return None


def interactive_command_menu() -> str | None:
    """Interactive slash command menu (standalone, manages its own raw mode).

    Delegates to slash_menu_raw after entering raw terminal mode.
    """
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        result = slash_menu_raw(fd, f"  {C.CYAN}❯{C.RESET} ")
        return result
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def interactive_tool_selector() -> str:
    """Interactive checkbox selector for allowed tools using arrow keys + Enter."""
    tools = ["Edit", "Write", "Bash", "Read", "Glob", "Grep",
             "WebSearch", "WebFetch", "Task", "NotebookEdit"]
    descs = ["파일 수정", "파일 생성", "명령어 실행", "파일 읽기", "파일 검색", "내용 검색",
             "웹 검색", "웹 페이지 읽기", "서브에이전트", "노트북 수정"]
    selected = [False] * len(tools)
    cursor = 0
    done_idx = len(tools)          # index of [완료] button
    total_items = len(tools) + 1   # tools + done button
    total_lines = total_items + 2  # items + blank + help line

    def render(first: bool = False):
        if not first:
            sys.stdout.write(f"\033[{total_lines}A\033[J")
        for i, tool in enumerate(tools):
            check = f"{C.GREEN}✓{C.RESET}" if selected[i] else " "
            ptr = f"{C.CYAN}›{C.RESET}" if i == cursor else " "
            print(f"  {ptr} [{check}] {tool:<6}  {C.DIM}{descs[i]}{C.RESET}", flush=True)
        # Done button
        ptr = f"{C.CYAN}›{C.RESET}" if cursor == done_idx else " "
        count = sum(selected)
        print(f"  {ptr} {C.BOLD}[완료]{C.RESET} {C.DIM}({count}개 선택){C.RESET}", flush=True)
        print(flush=True)
        print(f"  {C.DIM}↑↓ 이동 · Enter 선택/완료{C.RESET}", flush=True)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    render(first=True)

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # Escape sequence (arrow keys)
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":  # ↑
                        cursor = (cursor - 1) % total_items
                    elif ch3 == "B":  # ↓
                        cursor = (cursor + 1) % total_items
            elif ch in ("\r", "\n", " "):
                if cursor == done_idx:
                    break
                else:
                    selected[cursor] = not selected[cursor]
            elif ch == "\x03":  # Ctrl+C
                break
            else:
                continue

            # Temporarily restore terminal for rendering
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            render()
            tty.setraw(fd)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # Clear selector UI
    sys.stdout.write(f"\033[{total_lines}A\033[J")
    sys.stdout.flush()

    chosen = [t for t, s in zip(tools, selected) if s]
    return " ".join(chosen)


def ask_permission_mode():
    """Ask user about tool permissions at startup."""
    from claude_kr.state import config

    print(f"  {C.BOLD}도구 권한 설정{C.RESET}")
    print(f"    {C.CYAN}1{C.RESET}) 선택 허용 — 허용할 도구를 직접 선택")
    print(f"    {C.CYAN}2{C.RESET}) 전체 허용 — 모든 도구 자동 허용 {C.GREEN}(추천){C.RESET}")
    print()

    try:
        choice = input(f"\001{C.CYAN}\002선택 [1/2]\001{C.RESET}\002 (기본: 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    if choice == "2":
        config.dangerously_skip_permissions = True
        from claude_kr.ui import success
        success("  전체 허용 (--dangerously-skip-permissions)")
    else:
        print()
        tools = interactive_tool_selector()
        if tools:
            config.allowed_tools = tools
            from claude_kr.ui import success
            success(f"  허용 도구: {tools}")
        else:
            from claude_kr.ui import dim
            dim("  도구 허용 없음 — 읽기 전용 모드")

    print()
