"""Stream parser for Claude Code stream-json events with live agent tree."""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import threading
import time
import unicodedata

from claude_tokensaver.state import config, _s
from claude_tokensaver.tokens import estimate_tokens, fmt_tokens
from claude_tokensaver.ui import C, SPINNER, dbg


_ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _char_width(ch: str) -> int:
    """Return the terminal display width of a single character.

    Emojis and East Asian wide characters occupy 2 terminal cells.
    Variation selectors and zero-width joiners occupy 0.
    """
    cp = ord(ch)
    # Control characters
    if cp < 32 or cp == 127:
        return 0
    # Zero-width: variation selectors, ZWJ, zero-width space, soft hyphen
    if 0xFE00 <= cp <= 0xFE0F or cp in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD):
        return 0
    # Supplemental symbols & emoji blocks (U+1F000+): virtually all 2 cells
    if cp >= 0x1F000:
        return 2
    # East Asian Width: Wide and Fullwidth (CJK, emoji with W property)
    # This correctly handles ⚡ U+26A1 (W), ✅ U+2705 (W), 📄 etc.
    # while leaving ✓ U+2713 (N) and box drawing │├└ (A) as 1 cell
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ('W', 'F'):
        return 2
    return 1


def _display_width(text: str) -> int:
    """Return terminal display width of text, excluding ANSI escapes."""
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", text))


def _truncate_line(text: str, cols: int) -> str:
    """Truncate a line so its display width fits within `cols` terminal cells.

    Properly accounts for wide characters (emojis, CJK) that occupy
    2 terminal cells.  Preserves ANSI escape sequences.
    """
    if cols <= 0:
        return text
    width = 0
    i = 0
    last_good = 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            i = m.end()
            last_good = i
            continue
        w = _char_width(text[i])
        if width + w > cols:
            return text[:last_good] + C.RESET
        width += w
        i += 1
        last_good = i
    return text


TOOL_ICONS = {
    "Bash":      "⚡",
    "Read":      "📄",
    "Edit":      "✏️ ",
    "Write":     "📝",
    "Glob":      "🔍",
    "Grep":      "🔎",
    "Task":      "🔀",
    "WebFetch":  "🌐",
    "WebSearch": "🌐",
    "NotebookEdit": "📓",
}


def short_model(model: str) -> str:
    """claude-opus-4-6 → opus, claude-sonnet-4-6 → sonnet, etc."""
    if "opus" in model:
        return "opus"
    if "sonnet" in model:
        return "sonnet"
    if "haiku" in model:
        return "haiku"
    return model.split("-")[0] if model else "?"


def tool_summary(name: str, input_data: dict) -> str:
    """One-line summary of a tool invocation."""
    if name == "Bash":
        cmd = input_data.get("command", "")
        return cmd[:70] + ("..." if len(cmd) > 70 else "")
    elif name in ("Read", "Write", "Edit"):
        fp = input_data.get("file_path", "")
        # Shorten long paths: keep last 2 components
        parts = fp.split("/")
        return "/".join(parts[-2:]) if len(parts) > 3 else fp
    elif name == "Glob":
        return input_data.get("pattern", "")
    elif name == "Grep":
        pat = input_data.get("pattern", "")
        path = input_data.get("path", "")
        return f"/{pat}/" + (f" in {path}" if path else "")
    elif name == "Task":
        return input_data.get("description", "")
    elif name in ("WebFetch", "WebSearch"):
        return input_data.get("url", input_data.get("query", ""))
    return str(input_data)[:60]


class StreamParser:
    """
    Parses Claude Code stream-json events and renders a live agent tree.
    Uses ANSI cursor control to re-render in-place, grouping each
    sub-agent's tools directly under its parent Task node.

    Output example:
        🤖 Orchestrator [opus]
        │
        ├── ⚡ Bash: ls /tmp
        ├── 🔀 #1 [sonnet] Fetch article 1
        │   ├── 🌐 WebSearch: BBC top story
        │   └── 📄 Read: article.txt
        ├── 🔀 #2 [sonnet] Fetch article 2
        │   └── 🌐 WebSearch: Reuters tech
        ├── ✏️  Edit: main.py
        │
        └── ✅ 완료 (도구 7회, 서브에이전트 2개)
    """

    PIPE   = "│"
    BRANCH = "├──"
    END    = "└──"

    def __init__(self):
        self.text_parts: list[str] = []
        self.active_blocks: dict[int, dict] = {}

        # Counters
        self.tool_count: int = 0
        self.sub_tool_count: int = 0

        # Agent tree state
        self.main_model: str = ""
        self.header_printed: bool = False
        self.task_counter: int = 0

        # Grouped tree: ordered root-level items
        self.root_items: list[dict] = []
        self.task_index: dict[str, int] = {}  # tool_use_id → root_items index
        self.seen_tool_ids: set[str] = set()  # dedup tool_use blocks by ID
        self._seen_message_ids: set[str] = set()  # dedup entire assistant messages
        self.rendered_lines: int = 0

        # Thinking state — single summary node (defensive search approach)
        self.thinking_count: int = 0
        self._thinking_total_tokens: int = 0
        self._thinking_start: float = 0.0  # when current thinking block started
        self._thinking_total_elapsed: float = 0.0  # accumulated thinking time
        self._seen_thinking_prefixes: set[str] = set()  # dedup thinking blocks by content prefix

        # Token usage tracking
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.cache_creation_tokens: int = 0
        self.total_cost_usd: float = 0.0

        # Live status line (spinner)
        self.status: str = ""
        self._status_base: str = ""  # base status text (without elapsed time)
        self._status_start: float = 0.0  # when current status was set
        self._last_status_render: float = 0.0  # throttle: last time status triggered re-render
        self.spin_idx: int = 0
        self._spin_lock = threading.RLock()
        self._spin_timer: threading.Timer | None = None

    # ── Header (printed once, not part of re-render) ──

    def _print_header(self, model: str):
        if self.header_printed:
            return
        self.main_model = model
        m = short_model(model)
        print(f"  {C.BOLD}🤖 Orchestrator [{m}]{C.RESET}", flush=True)
        print(f"  {C.DIM}{self.PIPE}{C.RESET}", flush=True)
        self.header_printed = True

    # ── Token usage ──

    def _collect_usage(self, usage: dict):
        """Accumulate token usage from an event."""
        if not isinstance(usage, dict):
            return
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        self.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

    # ── Tool status tracking ──

    def _mark_running_done(self):
        """Mark all currently running tools/tasks as done."""
        now = time.time()
        for item in self.root_items:
            if item.get("status") == "running":
                item["status"] = "done"
                item["elapsed"] = now - item.get("t0", now)
            if item["type"] == "task":
                for child in item.get("children", []):
                    if child.get("status") == "running":
                        child["status"] = "done"
                        child["elapsed"] = now - child.get("t0", now)

    def _find_thinking_node(self) -> dict | None:
        """Find existing thinking summary node by icon. Defensive approach."""
        for item in self.root_items:
            if item["type"] == "tool" and item.get("icon") == "⏺":
                return item
        return None

    def _thinking_label(self) -> str:
        """Build the thinking summary label from current stats."""
        if self.thinking_count > 1:
            return (
                f"{_s('label_thinking_node', 'Thinking')}: "
                f"{fmt_tokens(self._thinking_total_tokens)} tokens "
                f"({self.thinking_count}×)"
            )
        return (
            f"{_s('label_thinking_node', 'Thinking')}: "
            f"{fmt_tokens(self._thinking_total_tokens)} tokens"
        )

    MAX_THINKING_PREVIEW = 6

    def _make_thinking_preview(self, text: str) -> list[str]:
        """Generate preview lines from thinking content."""
        raw_lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if not raw_lines:
            return []
        details = []
        for line in raw_lines[:self.MAX_THINKING_PREVIEW]:
            truncated = line[:80] + ("..." if len(line) > 80 else "")
            details.append(f"{C.DIM}  {truncated}{C.RESET}")
        if len(raw_lines) > self.MAX_THINKING_PREVIEW:
            details.append(f"{C.DIM}  ... +{len(raw_lines) - self.MAX_THINKING_PREVIEW} lines{C.RESET}")
        return details

    def _add_thinking_to_tree(self, text: str, t0: float, elapsed: float):
        """Add thinking to the tree as a SINGLE summary node.

        Uses _seen_thinking_prefixes for dedup (streaming + verbose events).
        Uses defensive search (_find_thinking_node) to guarantee only one node.

        Key subtlety: verbose mode progressively re-sends the same thinking
        block with increasingly longer text (e.g. 50 chars, then 100, then
        200).  Each progressive update has a different prefix, so prefix-based
        dedup alone fails.  We solve this by checking whether any existing
        prefix is a substring of the new one (progressive growth of same
        block) — in that case we UPDATE the existing node without inflating
        thinking_count.

        Must be called under _spin_lock.
        """
        prefix = text.strip()[:200]
        if prefix in self._seen_thinking_prefixes:
            return False  # exact match — already counted

        # Check if this is a progressive update of an existing thinking block:
        # the new (longer) prefix starts with an already-seen shorter prefix.
        is_progressive_update = False
        for seen in self._seen_thinking_prefixes:
            if prefix.startswith(seen) or seen.startswith(prefix):
                is_progressive_update = True
                break
        self._seen_thinking_prefixes.add(prefix)

        est_tokens = estimate_tokens(text)

        if is_progressive_update:
            # Update token estimate to the larger value, don't inflate count
            self._thinking_total_tokens = max(self._thinking_total_tokens, est_tokens)
            if elapsed > 0:
                self._thinking_total_elapsed = max(self._thinking_total_elapsed, elapsed)
        else:
            # Genuinely new thinking block
            self.thinking_count += 1
            self._thinking_total_tokens += est_tokens
            self._thinking_total_elapsed += elapsed

        label = self._thinking_label()
        preview = self._make_thinking_preview(text)
        existing = self._find_thinking_node()

        if existing:
            existing["label"] = label
            existing["elapsed"] = self._thinking_total_elapsed
            existing["details"] = preview
        else:
            self.root_items.append({
                "type": "tool", "icon": "⏺",
                "label": label,
                "details": preview,
                "status": "done", "t0": t0, "elapsed": self._thinking_total_elapsed,
            })

        return True  # tree updated

    def _status_suffix(self, item: dict, spin_ch: str) -> str:
        """Return a status suffix string for a tree item."""
        st = item.get("status", "")
        if st == "done":
            elapsed = item.get("elapsed", 0)
            if elapsed >= 1:
                return f" {C.GREEN}✓{C.RESET}{C.DIM} {elapsed:.1f}s{C.RESET}"
            return f" {C.GREEN}✓{C.RESET}"
        if st == "running":
            return f" {C.DIM}{spin_ch}{C.RESET}"
        return ""

    # ── Tree rendering (ANSI cursor-based re-render) ──

    COLLAPSE_THRESHOLD = 4  # auto-collapse runs of ≥ this many same-name tools

    @staticmethod
    def _tool_name(item: dict) -> str:
        """Extract tool name from label for grouping."""
        label = item.get("label", "")
        return label.split(":")[0].strip() if ":" in label else ""

    def _group_consecutive(self, items: list[dict]) -> list[tuple[str, list[dict]]]:
        """Group consecutive same-type tool items. Returns [(key, [items])]."""
        groups: list[tuple[str, list[dict]]] = []
        for item in items:
            if item["type"] != "tool":
                groups.append(("_single", [item]))
                continue
            name = self._tool_name(item)
            if groups and groups[-1][0] == name and name:
                groups[-1][1].append(item)
            else:
                groups.append((name, [item]))
        return groups

    def _render_collapsed(
        self, name: str, items: list[dict], spin_ch: str, prefix: str = "  ",
    ) -> str:
        """Render a collapsed group line."""
        icon = items[0]["icon"]
        count = len(items)
        done = sum(1 for i in items if i.get("status") == "done")
        running = count - done

        if running > 0:
            status = f" {C.DIM}{spin_ch} ({done}/{count}){C.RESET}"
        elif done == count:
            total_elapsed = sum(i.get("elapsed", 0) for i in items)
            if total_elapsed >= 1:
                status = f" {C.GREEN}✓{C.RESET}{C.DIM} {total_elapsed:.1f}s{C.RESET}"
            else:
                status = f" {C.GREEN}✓{C.RESET}"
        else:
            status = ""

        return (
            f"{prefix}{C.DIM}{self.BRANCH} {icon} {name} "
            f"{C.CYAN}×{count}{C.RESET}{status}"
        )

    def _build_tree_lines(self) -> list[str]:
        """Build display lines from current tree state + status spinner."""
        spin_ch = SPINNER[self.spin_idx % len(SPINNER)]
        self.spin_idx += 1
        lines = []

        groups = self._group_consecutive(self.root_items)
        for key, items in groups:
            # Collapsed group
            if key != "_single" and len(items) >= self.COLLAPSE_THRESHOLD:
                lines.append(self._render_collapsed(key, items, spin_ch))
                continue

            # Individual items
            for item in items:
                suffix = self._status_suffix(item, spin_ch)
                if item["type"] == "tool":
                    lines.append(
                        f"  {C.DIM}{self.BRANCH} {item['icon']} {item['label']}{C.RESET}{suffix}"
                    )
                    for detail in item.get("details", []):
                        lines.append(f"  {C.DIM}{self.PIPE}{C.RESET}      {detail}")
                elif item["type"] == "task":
                    lines.append(
                        f"  {C.DIM}{self.BRANCH} {item['icon']} "
                        f"{C.CYAN}#{item['num']}{C.RESET} "
                        f"{C.DIM}[{item['model']}] {item['desc']}{C.RESET}{suffix}"
                    )
                    # Collapse sub-agent children too
                    children = item.get("children", [])
                    child_groups = self._group_consecutive(
                        [{"type": "tool", **c} for c in children],
                    )
                    flat_idx = 0
                    for ckey, citems in child_groups:
                        if ckey != "_single" and len(citems) >= self.COLLAPSE_THRESHOLD:
                            flat_idx += len(citems)
                            is_last = flat_idx >= len(children)
                            conn = self.END if is_last else self.BRANCH
                            icon = citems[0]["icon"]
                            count = len(citems)
                            done = sum(1 for c in citems if c.get("status") == "done")
                            running = count - done
                            if running > 0:
                                st = f" {C.DIM}{spin_ch} ({done}/{count}){C.RESET}"
                            elif done == count:
                                te = sum(c.get("elapsed", 0) for c in citems)
                                st = f" {C.GREEN}✓{C.RESET}{C.DIM} {te:.1f}s{C.RESET}" if te >= 1 else f" {C.GREEN}✓{C.RESET}"
                            else:
                                st = ""
                            lines.append(
                                f"  {C.DIM}{self.PIPE}   {conn} {icon} {ckey} "
                                f"{C.CYAN}×{count}{C.RESET}{st}"
                            )
                        else:
                            for ci in citems:
                                flat_idx += 1
                                is_last = flat_idx >= len(children)
                                conn = self.END if is_last else self.BRANCH
                                csuffix = self._status_suffix(ci, spin_ch)
                                lines.append(
                                    f"  {C.DIM}{self.PIPE}   {conn} {ci['icon']} "
                                    f"{ci['label']}{C.RESET}{csuffix}"
                                )
                                for detail in ci.get("details", []):
                                    pipe2 = "    " if is_last else f"{self.PIPE}   "
                                    lines.append(
                                        f"  {C.DIM}{self.PIPE}   {pipe2}{C.RESET} {detail}"
                                    )

        # Spinner status line at the bottom
        if self.status:
            lines.append(f"  {C.DIM}  {spin_ch} {self.status}{C.RESET}")
        return lines

    # ── Diff details for Edit/Write tools ──

    MAX_DETAIL_LINES = 10

    def _make_tool_details(self, name: str, input_data: dict) -> list[str]:
        """Generate preview lines for Edit/Write tools."""
        if name == "Edit":
            return self._make_edit_diff(input_data)
        if name == "Write":
            return self._make_write_preview(input_data)
        return []

    def _make_edit_diff(self, input_data: dict) -> list[str]:
        """Generate color diff from Edit's old_string/new_string."""
        old_s = input_data.get("old_string", "")
        new_s = input_data.get("new_string", "")
        if not old_s and not new_s:
            return []

        old_lines = old_s.splitlines()
        new_lines = new_s.splitlines()
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=1))

        details = []
        for d in diff:
            if d.startswith("---") or d.startswith("+++"):
                continue
            if d.startswith("@@"):
                continue
            if d.startswith("-"):
                details.append(f"{C.RED}- {d[1:][:70]}{C.RESET}")
            elif d.startswith("+"):
                details.append(f"{C.GREEN}+ {d[1:][:70]}{C.RESET}")

        if details:
            # Count before truncation for accurate stats
            removed = sum(1 for d in details if d.startswith(C.RED))
            added = sum(1 for d in details if d.startswith(C.GREEN))

            if len(details) > self.MAX_DETAIL_LINES:
                total = len(details)
                details = details[:self.MAX_DETAIL_LINES]
                details.append(f"{C.DIM}  ... +{total - self.MAX_DETAIL_LINES} more{C.RESET}")

            header = f"{C.DIM}({C.GREEN}+{added}{C.DIM}/{C.RED}-{removed}{C.DIM} lines){C.RESET}"
            details.insert(0, header)

        return details

    def _make_write_preview(self, input_data: dict) -> list[str]:
        """Generate preview for Write tool."""
        content = input_data.get("content", "")
        if not content:
            return []
        lines = content.splitlines()
        total = len(lines)
        details = [f"{C.DIM}({_s('label_new_file', 'new file')}, {total} lines){C.RESET}"]
        for line in lines[:4]:
            details.append(f"{C.GREEN}  {line[:70]}{C.RESET}")
        if total > 4:
            details.append(f"{C.DIM}  ...{C.RESET}")
        return details

    def start_waiting_spinner(self):
        """Show a waiting spinner before any events arrive."""
        self._set_status(_s("msg_waiting", "Waiting for response... (ESC to cancel)"))

    def stop_waiting_spinner(self):
        """Stop the waiting spinner."""
        if _s("msg_waiting", "Waiting") in self.status:
            self.status = ""
            self._stop_spin_timer()

    def _set_status(self, text: str):
        """Update status text. Does NOT re-render — spin timer handles display."""
        self._status_base = text
        self._status_start = time.time()
        self.status = text
        if self.header_printed and not config.debug:
            # If no timer running yet, kick one off
            if self._spin_timer is None:
                self._start_spin_timer()

    def _start_spin_timer(self):
        """Start (or restart) the periodic spinner refresh timer."""
        self._stop_spin_timer()
        if self.status:
            t = threading.Timer(0.12, self._spin_tick)
            t.daemon = True
            t.start()
            self._spin_timer = t

    def _stop_spin_timer(self):
        """Cancel the spinner refresh timer."""
        with self._spin_lock:
            if self._spin_timer is not None:
                self._spin_timer.cancel()
                self._spin_timer = None

    def _spin_tick(self):
        """Called periodically. Updates ONLY the status line — no cursor-up."""
        with self._spin_lock:
            if self.status and self.header_printed and not config.debug:
                # Auto-append elapsed time for long waits
                if self._status_base and self._status_start:
                    elapsed = time.time() - self._status_start
                    if elapsed >= 3 and "⏺" not in self._status_base:
                        self.status = f"{self._status_base} ({elapsed:.0f}s)"
                self._update_status_line()
            # Schedule next tick inside lock to prevent race with _stop_spin_timer
            if self.status:
                t = threading.Timer(0.12, self._spin_tick)
                t.daemon = True
                t.start()
                self._spin_timer = t

    def _update_status_line(self):
        """Overwrite ONLY the last line (status). No cursor-up, no ghost lines.

        This is the key fix: spinner ticks and status changes never touch
        the tree lines above. Only _rerender() does full cursor-up redraw,
        and that only happens on actual tree mutations (new tool, etc).
        """
        if self.rendered_lines < 1:
            return
        spin_ch = SPINNER[self.spin_idx % len(SPINNER)]
        self.spin_idx += 1
        status_line = f"  {C.DIM}  {spin_ch} {self.status}{C.RESET}"
        try:
            cols = os.get_terminal_size().columns
        except (OSError, ValueError):
            cols = 80
        status_line = _truncate_line(status_line, cols)
        # \033[A = up 1, \r = start of line, \033[K = clear to end
        sys.stdout.write(f"\033[A\r\033[K{status_line}\n")
        sys.stdout.flush()

    def _rerender(self):
        """Full tree redraw. Only called when tree structure actually changes.

        Uses single write for atomicity.  Lines are truncated to terminal
        width (in display cells, not characters) to prevent wrapping.

        rendered_lines tracks PHYSICAL lines (accounting for any residual
        wrapping from wide characters) so cursor-up always goes back far
        enough to fully erase the previous render.
        """
        with self._spin_lock:
            lines = self._build_tree_lines()
            try:
                cols = os.get_terminal_size().columns
            except (OSError, ValueError):
                cols = 80
            buf = []
            if self.rendered_lines > 0:
                buf.append(f"\033[{self.rendered_lines}A\033[J")
            physical = 0
            for line in lines:
                truncated = _truncate_line(line, cols)
                buf.append(truncated + "\n")
                # Count physical lines: even after truncation, measure actual
                # display width in case _char_width underestimates some chars
                w = _display_width(truncated)
                physical += max(1, -(-w // cols))  # ceiling division
            if buf:
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
            self.rendered_lines = physical

    def _debug_print_tool(self, name: str, input_data: dict, icon: str,
                          parent_id: str | None):
        """Append-only tool display for debug mode (cursor control disabled)."""
        if parent_id is None:
            if name == "Task":
                desc = input_data.get("description", "?")
                model = input_data.get("model", "sonnet")
                print(
                    f"  {C.DIM}{self.BRANCH} {icon} "
                    f"{C.CYAN}#{self.task_counter}{C.RESET} "
                    f"{C.DIM}[{model}] {desc}{C.RESET}",
                    flush=True,
                )
            else:
                summary = tool_summary(name, input_data)
                print(
                    f"  {C.DIM}{self.BRANCH} {icon} {name}: {summary}{C.RESET}",
                    flush=True,
                )
        else:
            idx = self.task_index.get(parent_id)
            tnum = self.root_items[idx]["num"] if idx is not None else "?"
            summary = tool_summary(name, input_data)
            print(
                f"  {C.DIM}{self.PIPE}  #{tnum} · {icon} {name}: {summary}{C.RESET}",
                flush=True,
            )

    def _print_footer(self):
        # Mark all remaining running tools as done, then render final state
        self._mark_running_done()
        self._stop_spin_timer()
        self.status = ""
        if not config.debug:
            self._rerender()

        parts = [f"{_s('label_footer_tools', 'Tools')} {self.tool_count}"]
        if self.thinking_count > 0:
            parts.append(f"{_s('label_footer_thinking', 'Thinking')} {self.thinking_count}")
        if self.task_counter > 0:
            parts.append(f"{_s('label_footer_subagents', 'Sub-agents')} {self.task_counter}")
        if self.sub_tool_count > 0:
            parts.append(f"{_s('label_footer_subtools', 'Sub-tools')} {self.sub_tool_count}")
        summary = ", ".join(parts)

        # Token usage line
        token_line = ""
        total = self.input_tokens + self.output_tokens
        if total > 0:
            token_parts = [
                f"{_s('label_input', 'Input')} {fmt_tokens(self.input_tokens)}",
                f"{_s('label_output', 'Output')} {fmt_tokens(self.output_tokens)}",
            ]
            if self.cache_read_tokens > 0:
                token_parts.append(f"{_s('label_cache', 'Cache')} {fmt_tokens(self.cache_read_tokens)}")
            cost_str = ""
            if self.total_cost_usd > 0:
                cost_str = f" · ${self.total_cost_usd:.4f}"
            token_line = (
                f"  {C.DIM}{self.PIPE}   "
                f"📊 {_s('label_footer_tokens', 'Tokens')}: {' / '.join(token_parts)} "
                f"({_s('label_total', 'Total')} {fmt_tokens(total)}{cost_str}){C.RESET}"
            )

        print(f"  {C.DIM}{self.PIPE}{C.RESET}", flush=True)
        if token_line:
            print(token_line, flush=True)
        print(f"  {C.DIM}{self.END} ✅ {_s('label_footer_done', 'Done')} ({summary}){C.RESET}", flush=True)

    # ── Event handling ──

    def feed_line(self, raw_line: str):
        raw_line = raw_line.strip()
        if not raw_line:
            return

        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            dbg(f"[non-json] {raw_line[:120]}")
            return

        if data.get("type") == "stream_event":
            event = data.get("event", {})
            parent_id = data.get("parent_tool_use_id")
        else:
            event = data
            parent_id = data.get("parent_tool_use_id")

        etype = event.get("type", "")

        if config.debug and etype not in ("ping",):
            preview = json.dumps(event, ensure_ascii=False)
            if len(preview) > 200:
                preview = preview[:200] + "..."
            dbg(f"[{etype}] {preview}")

        if etype == "content_block_start":
            self._on_block_start(event)
        elif etype == "content_block_delta":
            self._on_block_delta(event)
        elif etype == "content_block_stop":
            self._on_block_stop(event, parent_id)
        elif etype == "assistant":
            self._on_assistant_message(event, parent_id)
        elif etype == "result":
            result_text = event.get("result", "")
            if result_text:
                self.text_parts = [result_text]
            # result event carries final usage and cost
            if "usage" in event:
                self._collect_usage(event["usage"])
            if "total_cost_usd" in event:
                self.total_cost_usd = event["total_cost_usd"]
        elif etype == "message_start":
            msg = event.get("message", {})
            if "usage" in msg:
                self._collect_usage(msg["usage"])
        elif etype == "message_delta":
            if "usage" in event:
                self._collect_usage(event["usage"])
            delta = event.get("delta", {})
            if "usage" in delta:
                self._collect_usage(delta["usage"])

    def _on_block_start(self, event: dict):
        index = event.get("index", -1)
        block = event.get("content_block", {})
        now = time.time()
        self.active_blocks[index] = {
            "type": block.get("type", ""),
            "name": block.get("name", ""),
            "id": block.get("id", ""),
            "json_parts": [],
            "text_parts": [],
            "t0": now,
        }
        if block.get("type") == "thinking":
            self._thinking_start = now

    def _on_block_delta(self, event: dict):
        index = event.get("index", -1)
        delta = event.get("delta", {})
        dtype = delta.get("type", "")
        block = self.active_blocks.get(index)
        if not block:
            return
        if dtype == "text_delta":
            block["text_parts"].append(delta.get("text", ""))
        elif dtype == "input_json_delta":
            block["json_parts"].append(delta.get("partial_json", ""))
        elif dtype == "thinking_delta":
            text = delta.get("thinking", "")
            block["text_parts"].append(text)
            # Live preview of thinking in status line
            full = "".join(block["text_parts"])
            last_line = full.rstrip().rsplit("\n", 1)[-1].strip()
            elapsed = time.time() - (self._thinking_start or block.get("t0", time.time()))
            elapsed_str = f"{elapsed:.0f}s"
            if last_line:
                preview = last_line[:50] + ("..." if len(last_line) > 50 else "")
                self._set_status(f'⏺ ({elapsed_str}) "{preview}"')
            else:
                self._set_status(f"⏺ {_s('msg_thinking', 'Thinking...')} ({elapsed_str})")

    def _on_block_stop(self, event: dict, parent_id: str | None = None):
        index = event.get("index", -1)
        block = self.active_blocks.pop(index, None)
        if not block:
            return

        if block["type"] == "thinking":
            # Skip sub-agent thinking — only display main agent's
            if parent_id is not None:
                return
            text = "".join(block["text_parts"])
            elapsed = time.time() - block.get("t0", time.time())
            if text.strip():
                if not config.debug:
                    self._stop_spin_timer()
                with self._spin_lock:
                    added = self._add_thinking_to_tree(text, block.get("t0", time.time()), elapsed)
                    if added and not config.debug:
                        self._rerender()
                if not config.debug:
                    self._start_spin_timer()
        elif block["type"] == "text":
            text = "".join(block["text_parts"])
            if text.strip():
                self.text_parts.append(text)
        elif block["type"] == "tool_use":
            name = block["name"]
            raw_json = "".join(block["json_parts"])
            try:
                input_data = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                input_data = {}
            self._display_tool(name, input_data, block.get("id", ""), parent_id)

    def _on_assistant_message(self, event: dict, parent_id: str | None = None):
        """Handle complete assistant message (--verbose mode).

        Verbose mode progressively re-broadcasts the same message as new
        content blocks complete, all sharing the same msg_id.  We dedup
        usage collection by msg_id, but always process content blocks
        using block-level dedup:
          - tool_use blocks: dedup by tool_id (seen_tool_ids)
          - thinking blocks: dedup by content prefix (_add_thinking_to_tree)

        Old messages from previous turns are also re-sent in verbose mode;
        block-level dedup handles those correctly.
        """
        message = event.get("message", {})
        model = message.get("model", "")

        # ── Message-level dedup (usage only) ──
        # --verbose mode progressively re-broadcasts the same message as
        # new content blocks complete, all sharing the same msg_id.
        # We dedup only usage collection here; content blocks use their
        # own dedup: tool_id for tool_use, prefix for thinking.
        msg_id = message.get("id", "")
        is_new_msg = True
        if msg_id:
            if msg_id in self._seen_message_ids:
                is_new_msg = False
            else:
                self._seen_message_ids.add(msg_id)

        # Collect token usage only once per msg_id
        if is_new_msg and "usage" in message:
            self._collect_usage(message["usage"])

        # Normalize: treat empty string as None (main agent)
        if not parent_id:
            parent_id = None

        if model and parent_id is None:
            self._print_header(model)

        content = message.get("content", [])
        if not isinstance(content, list) or not content:
            if self.header_printed:
                self._set_status(_s("msg_thinking", "Thinking..."))
            return

        # Stop spin timer to prevent race with tree mutations
        self._stop_spin_timer()

        added = False  # track if we added anything new

        with self._spin_lock:
            now = time.time()
            for block in content:
                btype = block.get("type", "")

                if btype == "tool_use":
                    tool_id = block.get("id", "")
                    # Dedup by tool_id — skip if already in tree
                    if tool_id and tool_id in self.seen_tool_ids:
                        continue
                    if tool_id:
                        self.seen_tool_ids.add(tool_id)

                    name = block.get("name", "")
                    input_data = block.get("input", {})
                    icon = TOOL_ICONS.get(name, "🔧")
                    details = self._make_tool_details(name, input_data)

                    if parent_id is None:
                        self.tool_count += 1
                        if name == "Task":
                            self.task_counter += 1
                            desc = input_data.get("description", "unknown task")
                            sub_model = input_data.get("model", "sonnet")
                            self.root_items.append({
                                "type": "task", "icon": icon,
                                "num": self.task_counter, "model": sub_model,
                                "desc": desc, "tool_id": tool_id, "children": [],
                                "status": "done", "t0": now, "elapsed": 0,
                            })
                            self.task_index[tool_id] = len(self.root_items) - 1
                        else:
                            summary = tool_summary(name, input_data)
                            self.root_items.append({
                                "type": "tool", "icon": icon,
                                "label": f"{name}: {summary}",
                                "details": details,
                                "status": "done", "t0": now, "elapsed": 0,
                            })
                        added = True
                    else:
                        self.sub_tool_count += 1
                        summary = tool_summary(name, input_data)
                        child = {"icon": icon, "label": f"{name}: {summary}",
                                 "details": details,
                                 "status": "done", "t0": now, "elapsed": 0}
                        idx = self.task_index.get(parent_id)
                        if idx is not None and idx < len(self.root_items):
                            self.root_items[idx]["children"].append(child)
                            added = True

                    # Debug mode: append-only print (non-debug uses _rerender below)
                    if config.debug:
                        self._debug_print_tool(name, input_data, icon, parent_id)

                elif btype == "thinking":
                    if parent_id is not None:
                        continue  # skip sub-agent thinking in tree
                    text = block.get("thinking", "")
                    if not text:
                        continue
                    if self._add_thinking_to_tree(text, now, 0):
                        added = True

            # Rerender if something changed
            if added and self.header_printed and not config.debug:
                last_running = None
                for item in reversed(self.root_items):
                    if item["type"] == "task":
                        last_running = item
                        break
                if last_running:
                    self.status = (
                        f"#{last_running['num']} "
                        f"{_s('msg_working_on', 'working...')} ({last_running['desc']})")
                else:
                    self.status = _s("msg_thinking", "Thinking...")
                self._status_base = self.status
                self._status_start = time.time()
                self._rerender()

        # Restart spin timer
        if self.header_printed and not config.debug and self.status:
            self._start_spin_timer()

    def _display_tool(
        self, name: str, input_data: dict, tool_id: str, parent_id: str | None
    ):
        """Update tree state and re-render (or append in debug mode).

        All tree mutations + rerender are done under _spin_lock to prevent
        the spin timer from rendering a partial state.
        """
        # Dedup: --verbose mode resends the entire message content on each
        # update, so the same tool_use block arrives multiple times.
        if tool_id and tool_id in self.seen_tool_ids:
            return

        if config.debug:
            # Debug mode: append-only, no cursor control needed
            if tool_id:
                self.seen_tool_ids.add(tool_id)
            self.tool_count += 1
            icon = TOOL_ICONS.get(name, "🔧")
            if name == "Task":
                self.task_counter += 1
            self._debug_print_tool(name, input_data, icon, parent_id)
            return

        self._stop_spin_timer()

        with self._spin_lock:
            if tool_id:
                self.seen_tool_ids.add(tool_id)
            self.tool_count += 1
            icon = TOOL_ICONS.get(name, "🔧")
            details = self._make_tool_details(name, input_data)
            now = time.time()

            if parent_id is None:
                if name == "Task":
                    self.task_counter += 1
                    desc = input_data.get("description", "unknown task")
                    sub_model = input_data.get("model", "sonnet")
                    self.root_items.append({
                        "type": "task", "icon": icon,
                        "num": self.task_counter, "model": sub_model,
                        "desc": desc, "tool_id": tool_id, "children": [],
                        "status": "running", "t0": now,
                    })
                    self.task_index[tool_id] = len(self.root_items) - 1
                else:
                    summary = tool_summary(name, input_data)
                    self.root_items.append({
                        "type": "tool", "icon": icon,
                        "label": f"{name}: {summary}",
                        "details": details,
                        "status": "running", "t0": now,
                    })
            else:
                self.sub_tool_count += 1
                summary = tool_summary(name, input_data)
                child = {"icon": icon, "label": f"{name}: {summary}",
                         "details": details,
                         "status": "running", "t0": now}
                idx = self.task_index.get(parent_id)
                if idx is not None and idx < len(self.root_items):
                    self.root_items[idx]["children"].append(child)
                else:
                    self.root_items.append({
                        "type": "tool", "icon": icon,
                        "label": f"[sub] {name}: {summary}",
                        "details": details,
                        "status": "running", "t0": now,
                    })

            # Update status
            if parent_id is None:
                if name == "Task":
                    self.status = f"#{self.task_counter} {_s('msg_subagent_start', 'Sub-agent starting...')}"
                else:
                    self.status = f"{name} {_s('msg_tool_running', 'running...')}"
            else:
                idx = self.task_index.get(parent_id)
                tnum = self.root_items[idx]["num"] if idx is not None else "?"
                self.status = f"#{tnum} {name} {_s('msg_tool_running', 'running...')}"

            self._status_base = self.status
            self._status_start = time.time()
            self._rerender()

        self._start_spin_timer()

    def get_final_text(self) -> str:
        self._stop_spin_timer()
        return "\n".join(self.text_parts)
