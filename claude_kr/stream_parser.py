"""Stream parser for Claude Code stream-json events with live agent tree."""

import difflib
import json
import sys
import threading
import time

from claude_kr.state import config
from claude_kr.tokens import estimate_tokens, fmt_tokens
from claude_kr.ui import C, SPINNER, dbg


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
        self.seen_tool_ids: set[str] = set()  # dedup: verbose mode resends full content
        self.rendered_lines: int = 0

        # Thinking state
        self.thinking_count: int = 0

        # Token usage tracking
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.cache_creation_tokens: int = 0
        self.total_cost_usd: float = 0.0

        # Live status line (spinner)
        self.status: str = ""
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

        if len(details) > self.MAX_DETAIL_LINES:
            total = len(details)
            details = details[:self.MAX_DETAIL_LINES]
            details.append(f"{C.DIM}  ... +{total - self.MAX_DETAIL_LINES} more{C.RESET}")

        if details:
            removed = sum(1 for d in details if d.startswith(C.RED))
            added = sum(1 for d in details if d.startswith(C.GREEN))
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
        details = [f"{C.DIM}(새 파일, {total} lines){C.RESET}"]
        for line in lines[:4]:
            details.append(f"{C.GREEN}  {line[:70]}{C.RESET}")
        if total > 4:
            details.append(f"{C.DIM}  ...{C.RESET}")
        return details

    def _set_status(self, text: str):
        """Update status line and re-render."""
        self.status = text
        if self.header_printed and not config.debug:
            self._rerender()
            self._start_spin_timer()

    def _start_spin_timer(self):
        """Start (or restart) the periodic spinner refresh timer."""
        self._stop_spin_timer()
        if self.status:
            t = threading.Timer(0.1, self._spin_tick)
            t.daemon = True
            t.start()
            self._spin_timer = t

    def _stop_spin_timer(self):
        """Cancel the spinner refresh timer."""
        if self._spin_timer is not None:
            self._spin_timer.cancel()
            self._spin_timer = None

    def _spin_tick(self):
        """Called periodically to animate the spinner."""
        with self._spin_lock:
            if self.status and self.header_printed and not config.debug:
                self._rerender()
        # Schedule next tick
        if self.status:
            t = threading.Timer(0.1, self._spin_tick)
            t.daemon = True
            t.start()
            self._spin_timer = t

    def _rerender(self):
        """Erase previous tree output and redraw from current state."""
        with self._spin_lock:
            if self.rendered_lines > 0:
                sys.stdout.write(f"\033[{self.rendered_lines}A\033[J")
                sys.stdout.flush()
            lines = self._build_tree_lines()
            for line in lines:
                print(line, flush=True)
            self.rendered_lines = len(lines)

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

        parts = [f"도구 {self.tool_count}회"]
        if self.thinking_count > 0:
            parts.append(f"생각 {self.thinking_count}회")
        if self.task_counter > 0:
            parts.append(f"서브에이전트 {self.task_counter}개")
        if self.sub_tool_count > 0:
            parts.append(f"서브 도구 {self.sub_tool_count}회")
        summary = ", ".join(parts)

        # Token usage line
        token_line = ""
        total = self.input_tokens + self.output_tokens
        if total > 0:
            token_parts = [
                f"입력 {fmt_tokens(self.input_tokens)}",
                f"출력 {fmt_tokens(self.output_tokens)}",
            ]
            if self.cache_read_tokens > 0:
                token_parts.append(f"캐시 {fmt_tokens(self.cache_read_tokens)}")
            cost_str = ""
            if self.total_cost_usd > 0:
                cost_str = f" · ${self.total_cost_usd:.4f}"
            token_line = (
                f"  {C.DIM}{self.PIPE}   "
                f"📊 토큰: {' / '.join(token_parts)} "
                f"(총 {fmt_tokens(total)}{cost_str}){C.RESET}"
            )

        print(f"  {C.DIM}{self.PIPE}{C.RESET}", flush=True)
        if token_line:
            print(token_line, flush=True)
        print(f"  {C.DIM}{self.END} ✅ 완료 ({summary}){C.RESET}", flush=True)

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
            self._on_assistant_message(event)
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
        self.active_blocks[index] = {
            "type": block.get("type", ""),
            "name": block.get("name", ""),
            "id": block.get("id", ""),
            "json_parts": [],
            "text_parts": [],
            "t0": time.time(),
        }

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
            if last_line:
                preview = last_line[:55] + ("..." if len(last_line) > 55 else "")
                self.status = f'⏺ "{preview}"'

    def _on_block_stop(self, event: dict, parent_id: str | None = None):
        index = event.get("index", -1)
        block = self.active_blocks.pop(index, None)
        if not block:
            return

        if block["type"] == "thinking":
            text = "".join(block["text_parts"])
            elapsed = time.time() - block.get("t0", time.time())
            if text.strip():
                self.thinking_count += 1
                est_tokens = estimate_tokens(text)
                # Add thinking node to tree
                self.root_items.append({
                    "type": "tool", "icon": "⏺",
                    "label": f"생각 ({fmt_tokens(est_tokens)} tokens)",
                    "details": [],
                    "status": "done", "t0": block.get("t0", time.time()),
                    "elapsed": elapsed,
                })
                if not config.debug:
                    self._rerender()
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

    def _on_assistant_message(self, event: dict):
        """Handle complete assistant message (--verbose mode)."""
        message = event.get("message", {})
        model = message.get("model", "")
        parent_id = event.get("parent_tool_use_id")

        # Collect token usage from this message
        if "usage" in message:
            self._collect_usage(message["usage"])

        # New assistant turn → all previously running tools have completed
        self._mark_running_done()

        if model and not parent_id:
            self._print_header(model)

        # Update status based on who is active
        if self.header_printed:
            if parent_id:
                idx = self.task_index.get(parent_id)
                if idx is not None:
                    num = self.root_items[idx]["num"]
                    desc = self.root_items[idx].get("desc", "")
                    self._set_status(f"#{num} 작업 중... ({desc})")
                else:
                    self._set_status("서브에이전트 작업 중...")
            else:
                self._set_status("생각 중...")

    def _display_tool(
        self, name: str, input_data: dict, tool_id: str, parent_id: str | None
    ):
        """Update tree state and re-render (or append in debug mode)."""
        # Dedup: --verbose mode resends the entire message content on each
        # update, so the same tool_use block arrives multiple times.
        if tool_id and tool_id in self.seen_tool_ids:
            return
        if tool_id:
            self.seen_tool_ids.add(tool_id)

        self.tool_count += 1
        icon = TOOL_ICONS.get(name, "🔧")

        # ── Update tree state ──
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

        # ── Update status ──
        if parent_id is None:
            if name == "Task":
                self.status = f"#{self.task_counter} 서브에이전트 시작..."
            else:
                self.status = f"{name} 실행 중..."
        else:
            idx = self.task_index.get(parent_id)
            tnum = self.root_items[idx]["num"] if idx is not None else "?"
            self.status = f"#{tnum} {name} 실행 중..."

        # ── Render ──
        if config.debug:
            self._debug_print_tool(name, input_data, icon, parent_id)
        else:
            self._rerender()
            self._start_spin_timer()

    def get_final_text(self) -> str:
        self._stop_spin_timer()
        return "\n".join(self.text_parts)
