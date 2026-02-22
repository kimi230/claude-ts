"""ANSI colors, output helpers, markdown rendering, spinner."""

from __future__ import annotations

import sys
import threading

from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.theme import Theme

from claude_ts.state import config


# ── Rich console ────────────────────────────────────────────────────────────

_theme = Theme({"markdown.heading": "bold cyan"})
console = Console(theme=_theme, highlight=False)


def render_markdown(text: str):
    """Render markdown text to the terminal."""
    console.print(
        Padding(Markdown(text), (0, 0, 0, 2)),
        width=min(console.width, 100),
    )


# ── ANSI Colors ─────────────────────────────────────────────────────────────

class C:
    CYAN    = "\033[1;36m"
    DIM     = "\033[2m"
    YELLOW  = "\033[1;33m"
    GREEN   = "\033[1;32m"
    RED     = "\033[1;31m"
    BOLD    = "\033[1m"
    BLUE    = "\033[1;34m"
    MAGENTA = "\033[1;35m"
    RESET   = "\033[0m"


# ── Output helpers ──────────────────────────────────────────────────────────

def dim(msg: str):
    print(f"  {C.DIM}{msg}{C.RESET}", flush=True)


def error(msg: str):
    from claude_ts.state import _s
    print(f"  {C.RED}[{_s('err_prefix', 'Error')}] {msg}{C.RESET}", file=sys.stderr, flush=True)


def success(msg: str):
    print(f"  {C.GREEN}{msg}{C.RESET}", flush=True)


def dbg(msg: str):
    if config.debug:
        print(f"{C.YELLOW}[DEBUG] {msg}{C.RESET}", flush=True)


def dbg_block(label: str, content: str):
    if config.debug:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        print(f"{C.YELLOW}── {label} ──{C.RESET}", flush=True)
        print(f"{C.DIM}{preview}{C.RESET}", flush=True)


# ── Spinner ─────────────────────────────────────────────────────────────────

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SpinnerContext:
    """Animated spinner for blocking operations (translation, startup)."""

    def __init__(self, msg: str):
        self.msg = msg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()

    def _run(self):
        idx = 0
        while not self._stop.is_set():
            ch = SPINNER[idx % len(SPINNER)]
            sys.stdout.write(f"\r  {C.DIM}{ch} {self.msg}{C.RESET}\033[K")
            sys.stdout.flush()
            idx += 1
            self._stop.wait(0.08)
