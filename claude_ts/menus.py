"""Slash command menus and interactive selectors."""

from __future__ import annotations

import os
import select
import sys
import termios
import tty

from claude_ts.ui import C
from claude_ts.terminal import _read_esc_seq
from claude_ts.state import _s


def get_slash_commands() -> list[tuple[str, str]]:
    """Return slash commands with localized descriptions."""
    return [
        ("help",    _s("cmd_help", "Show help")),
        ("clear",   _s("cmd_clear", "Clear conversation")),
        ("compact", _s("cmd_compact", "Compact context")),
        ("copy",    _s("cmd_copy", "Copy last response")),
        ("cost",    _s("cmd_cost", "Token usage")),
        ("doctor",  _s("cmd_doctor", "Check installation")),
        ("export",  _s("cmd_export", "Export conversation")),
        ("init",    _s("cmd_init", "Init CLAUDE.md")),
        ("memory",  _s("cmd_memory", "Edit CLAUDE.md")),
        ("model",   _s("cmd_model", "Change model")),
        ("ollama",  _s("cmd_ollama", "Change translate backend")),
        ("rename",  _s("cmd_rename", "Rename session")),
        ("stats",   _s("cmd_stats", "Session stats")),
        ("img",     _s("cmd_img", "Clipboard image")),
        ("allow",   _s("cmd_allow", "Change tool permissions")),
        ("resume",  _s("cmd_resume", "Resume session")),
        ("lang",    _s("cmd_lang", "Change language")),
        ("debug",   _s("cmd_debug", "Toggle debug")),
        ("reset",   _s("cmd_reset", "New session")),
        ("yolo",    _s("cmd_yolo", "YOLO mode")),
        ("exit",    _s("cmd_exit", "Exit")),
    ]


def _filter_commands(q: str) -> list[tuple[str, str]]:
    commands = get_slash_commands()
    if not q:
        return list(commands)
    ql = q.lower()
    return [(c, d) for c, d in commands if ql in c or ql in d]


def slash_menu_raw(fd: int, prompt_str: str) -> str | None:
    """Inline slash command menu in raw terminal mode.

    Called from read_input() when '/' is typed as the first character,
    or from interactive_command_menu() as the unified core implementation.
    Shows a filterable, arrow-navigable menu below the input line.
    Returns the selected command name, or None if cancelled.
    """
    query = ""
    cursor_idx = 0
    filtered = list(get_slash_commands())
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
            sys.stdout.write(f"    {C.DIM}({_s('label_no_match', 'No matching command')}){C.RESET}")

        sys.stdout.write(
            f"\r\n  {C.DIM}{_s('label_nav_hint', '↑↓ Navigate · Enter Select · Esc Cancel')}{C.RESET}"
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
    descs = [
        _s("label_tool_edit", "Edit files"),
        _s("label_tool_write", "Create files"),
        _s("label_tool_bash", "Run commands"),
        _s("label_tool_read", "Read files"),
        _s("label_tool_glob", "Search files"),
        _s("label_tool_grep", "Search content"),
        _s("label_tool_websearch", "Web search"),
        _s("label_tool_webfetch", "Fetch web pages"),
        _s("label_tool_task", "Sub-agent"),
        _s("label_tool_notebook", "Edit notebooks"),
    ]
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
        print(f"  {ptr} {C.BOLD}[{_s('label_done', 'Done')}]{C.RESET} {C.DIM}({count}{_s('label_selected_count', ' selected')}){C.RESET}", flush=True)
        print(flush=True)
        print(f"  {C.DIM}{_s('label_nav_hint_short', '↑↓ Navigate · Enter Select/Done')}{C.RESET}", flush=True)

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
    from claude_ts.state import config

    print(f"  {C.BOLD}{_s('label_tool_permission', 'Tool Permission Setup')}{C.RESET}")
    print(f"    {C.CYAN}1{C.RESET}) {_s('label_select_allow', 'Selective — choose tools to allow')}")
    print(f"    {C.CYAN}2{C.RESET}) {_s('label_full_allow', 'Full access — allow all tools')} {C.GREEN}({_s('label_recommended', 'Recommended')}){C.RESET}")
    print()

    try:
        choice = input(f"\001{C.CYAN}\002{_s('prompt_select_choice', 'Select [1/2]')}\001{C.RESET}\002 ({_s('prompt_default', 'default')}: 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    if choice == "2":
        config.dangerously_skip_permissions = True
        from claude_ts.ui import success
        success(f"  {_s('msg_full_allow_mode', 'Full access (--dangerously-skip-permissions)')}")
    else:
        print()
        tools = interactive_tool_selector()
        if tools:
            config.allowed_tools = tools
            from claude_ts.ui import success
            success(f"  {_s('msg_allowed_tools', 'Allowed tools')}: {tools}")
        else:
            from claude_ts.ui import dim
            dim(f"  {_s('msg_no_tools', 'No tools allowed — read-only mode')}")

    print()
