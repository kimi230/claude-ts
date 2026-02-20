"""Terminal input with bracketed paste detection."""

import os
import select
import sys
import termios
import tty
import unicodedata

from claude_kr.ui import C


def _char_width(c: str) -> int:
    """Display width of a character (2 for CJK fullwidth, 1 otherwise)."""
    w = unicodedata.east_asian_width(c)
    return 2 if w in ("W", "F") else 1


def _read_esc_seq(fd: int) -> bytes:
    """After ESC byte received, read the rest of the escape sequence."""
    seq = b"\x1b"
    while True:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            break
        b = os.read(fd, 1)
        if not b:
            break
        seq += b
        # CSI sequences end with a byte in 0x40-0x7E range (after at least [ + param)
        if len(seq) >= 3 and 0x40 <= seq[-1] <= 0x7E:
            break
    return seq


def read_input(prompt_str: str, slash_handler=None) -> tuple[str, bool]:
    """Read input with bracketed paste detection (raw terminal mode).

    slash_handler: optional callable(fd, prompt_str) -> str|None
        Called when '/' is typed as the first character.
    Returns (text, is_paste).
    Raises EOFError on Ctrl+D, KeyboardInterrupt on Ctrl+C.
    """
    sys.stdout.write(prompt_str)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?2004h")   # enable bracketed paste
        sys.stdout.flush()
        tty.setraw(fd)

        typed: list[str] = []      # characters the user types (echoed)
        paste_parts: list[str] = []  # accumulated pasted content (not echoed)
        is_paste = False
        in_paste = False

        while True:
            b = os.read(fd, 1)
            if not b:
                raise EOFError

            byte = b[0]

            # ── Escape sequences ──
            if byte == 0x1B:
                seq = _read_esc_seq(fd)
                if seq == b"\x1b[200~":
                    in_paste = True
                    is_paste = True
                elif seq == b"\x1b[201~":
                    in_paste = False
                    # Show paste label inline, keep cursor on same line
                    paste_text = "".join(paste_parts)
                    n_lines = paste_text.count("\n") + 1
                    n_chars = len(paste_text)
                    if n_lines > 1:
                        info = f"{n_lines} lines, {n_chars} chars"
                    else:
                        info = f"{n_chars} chars"
                    label = f"{C.DIM}[Pasted text \u00b7 {info}]{C.RESET} "
                    sys.stdout.write(label)
                    sys.stdout.flush()
                # Arrow keys / other sequences: ignore
                continue

            # ── Control characters ──
            if byte == 3:          # Ctrl+C
                raise KeyboardInterrupt
            if byte == 4:          # Ctrl+D
                if not typed and not paste_parts:
                    raise EOFError
                continue
            if byte == 13:         # CR (Enter)
                if in_paste:
                    paste_parts.append("\n")
                else:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    break
                continue
            if byte == 10:         # LF
                if in_paste:
                    paste_parts.append("\n")
                continue
            if byte == 127 or byte == 8:   # Backspace / DEL
                if in_paste:
                    if paste_parts:
                        paste_parts.pop()
                elif typed:
                    removed = typed.pop()
                    w = _char_width(removed)
                    sys.stdout.write("\b \b" * w)
                    sys.stdout.flush()
                continue
            if byte < 0x20:        # Other control chars
                continue

            # ── Regular character (UTF-8 aware) ──
            if byte < 0x80:
                char = chr(byte)
            elif byte < 0xC0:
                continue           # stray continuation byte
            elif byte < 0xE0:
                char = (b + os.read(fd, 1)).decode("utf-8", errors="replace")
            elif byte < 0xF0:
                char = (b + os.read(fd, 2)).decode("utf-8", errors="replace")
            else:
                char = (b + os.read(fd, 3)).decode("utf-8", errors="replace")

            if in_paste:
                paste_parts.append(char)
            else:
                # Slash command trigger: "/" as first character
                if char == "/" and not typed and slash_handler:
                    result = slash_handler(fd, prompt_str)
                    if result is not None:
                        sys.stdout.write(f"{prompt_str}/{result}\r\n")
                        sys.stdout.flush()
                        return (result, False)
                    # Cancelled — continue with clean prompt
                    continue
                typed.append(char)
                sys.stdout.write(char)
                sys.stdout.flush()

        # ── Fallback: detect multi-line paste without bracket support ──
        if not is_paste:
            while select.select([fd], [], [], 0.05)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                extra = chunk.decode("utf-8", errors="replace")
                extra = extra.replace("\r\n", "\n").replace("\r", "\n")
                paste_parts.append(extra)
                is_paste = True

        # ── Combine pasted + typed text ──
        pasted = "".join(paste_parts).strip()
        typed_str = "".join(typed).strip()
        if pasted and typed_str:
            result = pasted + "\n\n" + typed_str
        elif pasted:
            result = pasted
        else:
            result = typed_str

        return (result, is_paste)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
