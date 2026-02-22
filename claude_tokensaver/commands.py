"""REPL command handlers and dispatch registry."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import termios
import threading
import time

from claude_tokensaver.state import config, SessionState, clean_env, list_session_records, _s, available_languages, load_language, save_user_config
from claude_tokensaver.tokens import fmt_tokens
from claude_tokensaver.ui import C, dim, error, success, render_markdown, SpinnerContext
from claude_tokensaver.clipboard import get_clipboard_image
from claude_tokensaver.ollama import _ollama_available, _ollama_list_models
from claude_tokensaver.executor import execute_streaming, process_image_turn
from claude_tokensaver.translation import translate
from claude_tokensaver.menus import interactive_tool_selector


def _run_cancellable(cmd: list[str], timeout: int = 300,
                     spinner_msg: str = ""
                     ) -> tuple[str, str, int] | None:
    """Run a subprocess that can be cancelled with Ctrl+C or ESC.

    Returns (stdout, stderr, returncode) or None if cancelled/timed out.
    """
    done = threading.Event()
    cancelled = threading.Event()
    result_box: dict = {}

    def _run():
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=clean_env(), start_new_session=True,
            )
            result_box["proc"] = p
            stdout, stderr = p.communicate()
            result_box["stdout"] = stdout
            result_box["stderr"] = stderr
            result_box["rc"] = p.returncode
        except Exception as e:
            result_box["error"] = str(e)
        finally:
            done.set()

    # 터미널을 non-canonical 모드로 (Ctrl+C/ESC 직접 감지)
    fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
        new_attrs = list(old_attrs)
        new_attrs[3] = new_attrs[3] & ~(termios.ECHO | termios.ICANON | termios.ISIG)
        new_attrs[6][termios.VMIN] = 0
        new_attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)
        terminal_modified = True
    except (termios.error, OSError, ValueError):
        terminal_modified = False

    def _key_monitor():
        while not cancelled.is_set() and not done.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.15)
                if not r:
                    continue
                data = os.read(fd, 64)
                if not data:
                    continue
                if 3 in data or 27 in data:
                    cancelled.set()
                    return
            except OSError:
                return

    if terminal_modified:
        threading.Thread(target=_key_monitor, daemon=True).start()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # 프로세스 시작 대기
    for _ in range(50):
        if "proc" in result_box:
            break
        time.sleep(0.1)
    proc = result_box.get("proc")

    deadline = time.time() + timeout
    try:
        with SpinnerContext(spinner_msg or _s("msg_running", "Running... (Ctrl+C/ESC to cancel)")):
            while not done.is_set():
                if cancelled.is_set() or time.time() > deadline:
                    break
                done.wait(0.3)
    finally:
        if terminal_modified:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    if cancelled.is_set():
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait(timeout=3)
        dim(_s("msg_cancelled", "Cancelled"))
        return None
    if not done.is_set():
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait(timeout=3)
        error(f"{_s('err_timeout', 'Timeout')} ({timeout}s)")
        return None
    if "error" in result_box:
        error(f"{_s('err_exec_failed', 'Execution failed')}: {result_box['error']}")
        return None
    return (result_box.get("stdout", ""),
            result_box.get("stderr", ""),
            result_box.get("rc", 1))


# ── Command Handlers ────────────────────────────────────────────────────────
# Each handler: def cmd_xxx(state: SessionState, args: str) -> bool
#   Returns True  → continue REPL loop
#   Returns False → break REPL loop


def cmd_exit(state: SessionState, args: str) -> bool:
    dim(_s("msg_session_end", "Ending session."))
    return False


def cmd_help(state: SessionState, args: str) -> bool:
    print(f"  {C.BOLD}━━━ claude-tokensaver help ━━━{C.RESET}")
    print(f"  {C.DIM}{_s('label_slash_hint', 'Type / to see command list')}{C.RESET}")
    print()
    print(f"  {C.BOLD}{_s('label_basic_commands', 'Basic Commands')}{C.RESET}")
    print(f"    {C.CYAN}/help{C.RESET}          {_s('cmd_help', 'Show help')}")
    print(f"    {C.CYAN}/exit{C.RESET}          {_s('cmd_exit', 'Exit')}")
    print(f"    {C.CYAN}/clear{C.RESET}         {_s('cmd_clear', 'Clear conversation')} (= /reset)")
    print()
    print(f"  {C.BOLD}{_s('label_session_mgmt', 'Session')}{C.RESET}")
    print(f"    {C.CYAN}/resume{C.RESET}         {_s('cmd_resume', 'Resume session')}")
    print(f"    {C.CYAN}/model{C.RESET}          {_s('cmd_model', 'Change model')}")
    print(f"    {C.CYAN}/ollama{C.RESET}         {_s('cmd_ollama', 'Change translate backend')}")
    print(f"    {C.CYAN}/rename{C.RESET}         {_s('cmd_rename', 'Rename session')}")
    print(f"    {C.CYAN}/compact{C.RESET}        {_s('cmd_compact', 'Compact context')}")
    print(f"    {C.CYAN}/cost{C.RESET}          {_s('cmd_cost', 'Token usage')}")
    print(f"    {C.CYAN}/stats{C.RESET}         {_s('cmd_stats', 'Session stats')}")
    print(f"    {C.CYAN}/copy{C.RESET}          {_s('cmd_copy', 'Copy last response')}")
    print(f"    {C.CYAN}/export{C.RESET}         {_s('cmd_export', 'Export conversation')}")
    print()
    print(f"  {C.BOLD}{_s('label_project', 'Project')}{C.RESET}")
    print(f"    {C.CYAN}/init{C.RESET}          {_s('cmd_init', 'Init CLAUDE.md')}")
    print(f"    {C.CYAN}/memory{C.RESET}        {_s('cmd_memory', 'Edit CLAUDE.md')}")
    print(f"    {C.CYAN}/doctor{C.RESET}        {_s('cmd_doctor', 'Check installation')}")
    print()
    print(f"  {C.BOLD}{_s('label_permissions', 'Permissions')}{C.RESET}")
    print(f"    {C.CYAN}/allow{C.RESET}          {_s('cmd_allow', 'Change tool permissions')}")
    print(f"    {C.CYAN}/yolo{C.RESET}          {_s('cmd_yolo', 'YOLO mode')}")
    print(f"    {C.CYAN}/debug{C.RESET}         {_s('cmd_debug', 'Toggle debug')}")
    print()
    print(f"  {C.BOLD}{_s('label_image', 'Image')}{C.RESET}")
    print(f"    {C.CYAN}/img{C.RESET}            {_s('cmd_img', 'Clipboard image')}")
    print(f"    {C.DIM}{_s('label_drag_hint', 'Drag & drop image file → auto detect')}{C.RESET}")
    print()
    print(f"  {C.BOLD}{_s('label_special_input', 'Special Input')}{C.RESET}")
    print(f"    {C.DIM}{_s('label_raw_hint', 'raw:<text>     send without translation')}{C.RESET}")
    print()
    return True


def cmd_cost(state: SessionState, args: str) -> bool:
    s = state.stats
    total = s.input_tokens + s.output_tokens
    print(f"  {C.BOLD}━━━ {_s('label_session_usage', 'Session Usage')} ━━━{C.RESET}")
    print(f"    {_s('label_turns', 'Turns')}:    {s.turn_count}")
    print(f"    {_s('label_input', 'Input')}:  {fmt_tokens(s.input_tokens)}")
    print(f"    {_s('label_output', 'Output')}:  {fmt_tokens(s.output_tokens)}")
    if s.cache_read_tokens > 0:
        print(f"    {_s('label_cache', 'Cache')}:  {fmt_tokens(s.cache_read_tokens)}")
    print(f"    {_s('label_total', 'Total')}:  {fmt_tokens(total)}")
    if s.tool_count > 0:
        print(f"    {_s('label_tools', 'Tools')}:  {s.tool_count}")
    if s.thinking_count > 0:
        print(f"    {_s('label_thinking', 'Thinking')}:  {s.thinking_count}")
    print(f"    {_s('label_cost', 'Cost')}:  ${s.total_cost_usd:.4f}")
    print()
    return True


def cmd_clear(state: SessionState, args: str) -> bool:
    state.reset()
    success(f"{_s('msg_new_session', 'New session')}: {state.session_uuid[:8]}...")
    print()
    return True


def cmd_copy(state: SessionState, args: str) -> bool:
    if not state.last_assistant_response:
        error(_s("err_no_copy_content", "No response to copy."))
    else:
        try:
            subprocess.run(
                ["pbcopy"],
                input=state.last_assistant_response,
                text=True,
                timeout=5,
            )
            preview = state.last_assistant_response[:60].replace("\n", " ")
            success(f"{_s('msg_clipboard_copied', 'Copied to clipboard')}: \"{preview}...\"")
        except FileNotFoundError:
            error(_s("err_pbcopy_not_found", "pbcopy not found (macOS only)"))
        except subprocess.TimeoutExpired:
            error(_s("err_clipboard_timeout", "Clipboard copy timed out"))
    print()
    return True


def cmd_export(state: SessionState, args: str) -> bool:
    if not state.conversation_history:
        error(_s("err_no_history", "No conversation to export."))
        print()
        return True
    if args:
        export_path = args.strip()
    else:
        name_part = state.session_name or state.session_uuid[:8]
        ts_part = time.strftime("%Y%m%d-%H%M%S")
        export_path = f"claude-ts-{name_part}-{ts_part}.md"
    try:
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(f"# Claude-TokenSaver conversation log\n\n")
            f.write(f"- {_s('label_session', 'Session')}: {state.session_name or state.session_uuid[:8]}\n")
            f.write(f"- {_s('label_date', 'Date')}: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- {_s('label_model', 'Model')}: {config.main_model or 'default'}\n")
            f.write(f"- {_s('label_turns', 'Turns')}: {state.stats.turn_count}\n\n---\n\n")
            for entry in state.conversation_history:
                role = _s("label_user", "User") if entry["role"] == "user" else "Claude"
                f.write(f"## {role} ({entry['ts']})\n\n")
                f.write(entry["text"] + "\n\n---\n\n")
        success(f"{_s('msg_export_saved', 'Conversation exported')}: {export_path}")
    except OSError as e:
        error(f"{_s('err_file_save_failed', 'File save failed')}: {e}")
    print()
    return True


def cmd_rename(state: SessionState, args: str) -> bool:
    if args:
        state.session_name = args.strip()
        if state.session_name:
            success(f"{_s('msg_session_renamed', 'Session renamed')}: {state.session_name}")
        else:
            dim(_s("err_name_empty", "Name is empty, not changed."))
    else:
        try:
            new_name = input(
                f"  \001{C.DIM}\002{_s('prompt_new_session_name', 'New session name')}: \001{C.RESET}\002"
            ).strip()
            if new_name:
                state.session_name = new_name
                success(f"{_s('msg_session_renamed', 'Session renamed')}: {state.session_name}")
            else:
                dim(_s("err_name_empty", "Name is empty, not changed."))
        except (EOFError, KeyboardInterrupt):
            print()
    print()
    return True


def cmd_compact(state: SessionState, args: str) -> bool:
    instructions = args.strip()
    compact_prompt = (
        "Summarize our conversation so far concisely. "
        "Focus on key decisions, code changes, and current state."
    )
    if instructions:
        compact_prompt += f" Pay special attention to: {instructions}"
    dim(_s("msg_compacting", "Compacting context..."))
    en_output = execute_streaming(compact_prompt, state)
    if en_output and en_output.strip():
        with SpinnerContext(_s("msg_translating_result", "Translating result...")):
            kr_output = translate(en_output, "en2kr")
        print()
        render_markdown(kr_output)
        print()
        success(_s("msg_compact_done", "Context compacted"))
    else:
        error(_s("err_compact_failed", "Compaction failed"))
    print()
    return True


def cmd_doctor(state: SessionState, args: str) -> bool:
    try:
        subprocess.run(["claude", "doctor"], env=clean_env())
    except FileNotFoundError:
        error(_s("claude_not_found", "claude command not found."))
    except KeyboardInterrupt:
        pass

    # Ollama status
    if _ollama_available():
        models = _ollama_list_models()
        print(f"  {C.GREEN}✓{C.RESET} {_s('msg_ollama_installed', 'Ollama installed — models')} {len(models)}")
    else:
        print(f"  {C.DIM}– {_s('msg_ollama_not_installed', 'Ollama not installed')}{C.RESET}")
    print()
    return True



def cmd_init(state: SessionState, args: str) -> bool:
    claude_md = os.path.join(os.getcwd(), "CLAUDE.md")
    if os.path.exists(claude_md):
        dim(f"{_s('msg_claudemd_exists', 'CLAUDE.md already exists')}: {claude_md}")
        try:
            confirm = input(
                f"  \001{C.DIM}\002{_s('prompt_overwrite', 'Overwrite? (y/N)')}: \001{C.RESET}\002"
            ).strip()
            if confirm.lower() not in ("y", "yes"):
                dim(_s("msg_cancelled", "Cancelled"))
                print()
                return True
        except (EOFError, KeyboardInterrupt):
            print()
            return True
    result = _run_cancellable(
        ["claude", "-p", "--model", "haiku",
         "Look at the files in this directory and generate a concise CLAUDE.md. "
         "Include: project name, tech stack, build/test commands, key conventions. "
         "Output ONLY the markdown content."],
        timeout=300,
        spinner_msg=_s("msg_claudemd_generating", "Generating CLAUDE.md... (Ctrl+C/ESC to cancel)"),
    )
    if result is not None:
        stdout, stderr, rc = result
        if rc == 0 and stdout.strip():
            with open(claude_md, "w", encoding="utf-8") as f:
                f.write(stdout.strip() + "\n")
            success(f"{_s('msg_claudemd_done', 'CLAUDE.md generated')}: {claude_md}")
        else:
            stderr_msg = stderr.strip() if stderr else ""
            error(f"{_s('err_claudemd_gen_failed', 'CLAUDE.md generation failed')}{': ' + stderr_msg if stderr_msg else ''}")
    print()
    return True


def cmd_memory(state: SessionState, args: str) -> bool:
    claude_md = os.path.join(os.getcwd(), "CLAUDE.md")
    if not os.path.exists(claude_md):
        error(_s("err_claudemd_not_found", "CLAUDE.md not found. Run /init first."))
        print()
        return True
    editor = os.environ.get("EDITOR", "vim")
    dim(f"{editor} {_s('msg_opening_editor', 'opening CLAUDE.md...')}")
    if editor in ("vim", "vi", "nvim"):
        dim(f"  {_s('msg_editor_vim_hint', ':wq Enter — save & quit | :q! Enter — quit without saving')}")
    try:
        subprocess.run([editor, claude_md])
        success(_s("msg_claudemd_edited", "CLAUDE.md edited"))
    except FileNotFoundError:
        error(f"{_s('err_editor_not_found', 'Editor not found')}: {editor}")
        dim(_s("err_set_editor", "Set the EDITOR environment variable."))
    except KeyboardInterrupt:
        pass
    print()
    return True


def cmd_stats(state: SessionState, args: str) -> bool:
    s = state.stats
    total = s.input_tokens + s.output_tokens
    elapsed_min = (time.time() - state.session_start_time) / 60
    print(f"  {C.BOLD}━━━ {_s('label_session_stats', 'Session Stats')} ━━━{C.RESET}")
    print()
    name_str = state.session_name if state.session_name else state.session_uuid[:8]
    print(f"  {C.CYAN}{_s('label_session', 'Session')}{C.RESET}   {name_str}")
    print(f"  {C.CYAN}{_s('label_time', 'Time')}{C.RESET}   {elapsed_min:.1f}min")
    print(f"  {C.CYAN}{_s('label_model', 'Model')}{C.RESET}   {config.main_model or 'default'}")
    if config.translate_backend == "ollama":
        print(f"  {C.CYAN}{_s('label_translate', 'Translate')}{C.RESET}   ollama:{config.ollama_model}")
    else:
        print(f"  {C.CYAN}{_s('label_translate', 'Translate')}{C.RESET}   {config.translate_model}")
    print()
    print(f"  {C.BOLD}{_s('label_token_usage', 'Token Usage')}{C.RESET}")
    # Bar chart — input/output on their own scale (percentage of total)
    max_val = max(s.input_tokens, s.output_tokens, 1)
    in_bar = int(s.input_tokens / max_val * 20)
    out_bar = int(s.output_tokens / max_val * 20)
    in_pct = int(s.input_tokens / total * 100) if total > 0 else 0
    out_pct = int(s.output_tokens / total * 100) if total > 0 else 0
    print(f"    {_s('label_input', 'Input')}  {in_pct:>3d}%  {C.BLUE}{'█' * in_bar}{'░' * (20 - in_bar)}{C.RESET}  {fmt_tokens(s.input_tokens)}")
    print(f"    {_s('label_output', 'Output')}  {out_pct:>3d}%  {C.GREEN}{'█' * out_bar}{'░' * (20 - out_bar)}{C.RESET}  {fmt_tokens(s.output_tokens)}")
    if s.cache_read_tokens > 0:
        print(f"    {_s('label_cache', 'Cache')}        {C.DIM}[{fmt_tokens(s.cache_read_tokens)} cached]{C.RESET}")
    print(f"    {_s('label_total', 'Total')}        {fmt_tokens(total)}")
    print()
    print(f"  {C.BOLD}{_s('label_activity', 'Activity')}{C.RESET}")
    print(f"    {_s('label_turns', 'Turns')}:        {s.turn_count}")
    print(f"    {_s('label_tool_usage', 'Tool usage')}: {s.tool_count}")
    print(f"    {_s('label_thinking', 'Thinking')}:      {s.thinking_count}")
    print(f"    {_s('label_conv_history', 'History')}: {len(state.conversation_history)}")
    print(f"    {_s('label_cost', 'Cost')}:      ${s.total_cost_usd:.4f}")
    print()
    return True


def cmd_debug(state: SessionState, args: str) -> bool:
    config.debug = not config.debug
    label = "ON" if config.debug else "OFF"
    print(f"  {C.YELLOW}{_s('label_debug_mode', 'Debug mode')}: {label}{C.RESET}")
    print()
    return True


def cmd_model(state: SessionState, args: str) -> bool:
    if args:
        config.main_model = args.strip()
        if config.main_model == "default":
            config.main_model = ""
        success(f"{_s('msg_model_changed', 'Model changed')}: {config.main_model or 'default'}")
    else:
        # Interactive model selection
        models = ["default", "opus", "sonnet", "haiku"]
        current = config.main_model or "default"
        print(f"  {C.DIM}{_s('label_current_model', 'Current model')}: {current}{C.RESET}")
        for i, m in enumerate(models):
            marker = f"{C.GREEN}*{C.RESET}" if m == current else " "
            print(f"    {marker} {i+1}) {m}")
        try:
            choice = input(
                f"  \001{C.DIM}\002{_s('prompt_select', 'Select')} [1-{len(models)}]: \001{C.RESET}\002"
            ).strip()
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                config.main_model = "" if models[idx] == "default" else models[idx]
                success(f"{_s('msg_model_changed', 'Model changed')}: {config.main_model or 'default'}")
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
    print()
    return True


def cmd_ollama(state: SessionState, args: str) -> bool:
    current_marker = f"← {_s('label_current', 'current')}"
    options: list[tuple[str, str]] = [
        ("claude", f"claude (haiku) {current_marker if config.translate_backend == 'claude' else ''}"),
    ]
    if _ollama_available():
        models = _ollama_list_models()
        if models:
            for m in models:
                current = (
                    current_marker
                    if config.translate_backend == "ollama" and config.ollama_model == m
                    else ""
                )
                options.append(("ollama:" + m, f"ollama:{m} {current}"))
        else:
            dim(_s("msg_ollama_no_models", "Ollama installed but no models. Run: ollama pull <model>"))
    else:
        dim(_s("msg_ollama_install_hint", "Ollama not installed. Visit https://ollama.com to install."))

    print(f"  {C.BOLD}{_s('label_translate_backend', 'Translation Backend')}{C.RESET}")
    for i, (key, label) in enumerate(options):
        print(f"    {C.CYAN}{i+1}{C.RESET}) {label}")
    try:
        choice = input(
            f"  \001{C.DIM}\002{_s('prompt_select', 'Select')} [1-{len(options)}]: \001{C.RESET}\002"
        ).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            key = options[idx][0]
            if key == "claude":
                config.translate_backend = "claude"
                config.ollama_model = ""
                save_user_config({"translate_backend": "claude", "ollama_model": ""})
                success(f"{_s('msg_translate_backend_changed', 'Translation backend')}: claude (haiku)")
            else:
                model_name = key.split(":", 1)[1]
                config.translate_backend = "ollama"
                config.ollama_model = model_name
                save_user_config({"translate_backend": "ollama", "ollama_model": model_name})
                success(f"{_s('msg_translate_backend_changed', 'Translation backend')}: ollama:{model_name}")
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
    print()
    return True


def cmd_allow(state: SessionState, args: str) -> bool:
    if args:
        config.allowed_tools = args.strip()
        config.dangerously_skip_permissions = False
        success(f"{_s('msg_tool_changed', 'Allowed tools changed')}: {config.allowed_tools}")
    else:
        print()
        tools = interactive_tool_selector()
        if tools:
            config.allowed_tools = tools
            config.dangerously_skip_permissions = False
            success(f"{_s('msg_tool_changed', 'Allowed tools changed')}: {tools}")
        else:
            dim(_s("msg_no_tools", "No tools allowed — read-only mode"))
    print()
    return True


def cmd_img(state: SessionState, args: str) -> bool:
    dim(f"📋 {_s('msg_checking_clipboard', 'Checking clipboard for image...')}")
    img_path = get_clipboard_image()
    if img_path is None:
        error(_s("err_no_clipboard_image", "No image in clipboard. (Copy a screenshot first)"))
        print()
        return True

    size_kb = os.path.getsize(img_path) / 1024
    success(f"  {_s('msg_image_saved', 'Image saved')}: {os.path.basename(img_path)} ({size_kb:.0f}KB)")

    try:
        process_image_turn(img_path, args.strip(), state)
    except KeyboardInterrupt:
        print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
        print()
    try:
        os.unlink(img_path)
    except OSError:
        pass
    return True


def cmd_yolo(state: SessionState, args: str) -> bool:
    config.dangerously_skip_permissions = True
    config.allowed_tools = ""
    success(_s("msg_yolo_activated", "YOLO mode activated"))
    print()
    return True


def cmd_resume(state: SessionState, args: str) -> bool:
    records = list_session_records()
    if not records:
        error(_s("err_no_sessions", "No saved sessions."))
        print()
        return True

    # Show at most 10 recent sessions
    records = records[:10]
    print(f"  {C.BOLD}━━━ {_s('label_recent_sessions', 'Recent Sessions')} ━━━{C.RESET}")
    for i, r in enumerate(records):
        name = r.get("name") or r["uuid"][:8]
        model = r.get("model", "?")
        turns = r.get("turns", 0)
        updated = r.get("updated", "?")
        cwd = r.get("cwd", "")
        preview = r.get("preview", "")
        # Shorten cwd
        cwd_short = cwd.replace(os.path.expanduser("~"), "~") if cwd else ""
        marker = f"{C.GREEN}*{C.RESET}" if r["uuid"] == state.session_uuid else " "
        print(
            f"  {marker} {C.CYAN}{i+1}{C.RESET}) {name}  "
            f"{C.DIM}{model} · {turns} {_s('label_turns', 'Turns')} · {updated} · {cwd_short}{C.RESET}"
        )
        if preview:
            # Truncate and show first line of user's question
            preview_line = preview.replace("\n", " ").strip()
            if len(preview_line) > 60:
                preview_line = preview_line[:57] + "..."
            print(f"       {C.DIM}💬 {preview_line}{C.RESET}")
    print()

    try:
        choice = input(
            f"  \001{C.DIM}\002{_s('prompt_select', 'Select')} [1-{len(records)}]: \001{C.RESET}\002"
        ).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(records):
            r = records[idx]
            state.session_uuid = r["uuid"]
            state.session_name = r.get("name", "")
            state.turn_count = r.get("turns", 1)  # > 0 so --resume is used
            state.session_start_time = time.time()
            success(f"{_s('msg_session_restored', 'Session restored')}: {r.get('name') or r['uuid'][:8]}")
        else:
            dim(_s("err_invalid_choice", "Invalid choice"))
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
    print()
    return True


def cmd_lang(state: SessionState, arg: str) -> bool:
    """Change language interactively."""
    langs = available_languages()
    if not langs:
        error("No language configs found.")
        return True

    print()
    print(f"  {C.BOLD}{_s('label_lang_select', 'Select language')}:{C.RESET}")
    for i, lang in enumerate(langs, 1):
        marker = " ◀" if lang["code"] == config.language else ""
        print(f"    {C.BOLD}{i}.{C.RESET} {lang['name']} ({lang['name_en']}){C.DIM}{marker}{C.RESET}")
    print()

    try:
        choice = input(f"  {C.DIM}{_s('prompt_select', 'Select')}> {C.RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
        print()
        return True

    if not choice:
        return True

    selected = None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(langs):
            selected = langs[idx]
    except ValueError:
        for lang in langs:
            if lang["code"] == choice.lower():
                selected = lang
                break

    if not selected:
        error(_s("err_invalid_choice", "Invalid choice"))
        print()
        return True

    config.language = selected["code"]
    load_language(selected["code"])
    save_user_config({"language": selected["code"]})
    success(f"  {_s('msg_lang_changed', 'Language changed')}: {selected['name']} ({selected['name_en']})")
    print()
    return True


# ── Command Registry ────────────────────────────────────────────────────────
# {primary_name: (handler, {aliases})}

COMMAND_REGISTRY: dict[str, tuple] = {
    "exit":    (cmd_exit,    {"quit", "종료", ":q"}),
    "help":    (cmd_help,    {"도움말"}),
    "cost":    (cmd_cost,    {"비용"}),
    "clear":   (cmd_clear,   {"reset", "리셋"}),
    "copy":    (cmd_copy,    set()),
    "export":  (cmd_export,  set()),
    "rename":  (cmd_rename,  set()),
    "compact": (cmd_compact, set()),
    "doctor":  (cmd_doctor,  set()),
    "init":    (cmd_init,    set()),
    "memory":  (cmd_memory,  set()),
    "stats":   (cmd_stats,   set()),
    "debug":   (cmd_debug,   {"디버그"}),
    "model":   (cmd_model,   set()),
    "ollama":  (cmd_ollama,  set()),
    "allow":   (cmd_allow,   set()),
    "img":     (cmd_img,     {"이미지", "image"}),
    "yolo":    (cmd_yolo,    set()),
    "resume":  (cmd_resume,  {"이어하기"}),
    "lang":    (cmd_lang,    {"언어", "language"}),
}


def dispatch(state: SessionState, user_input: str) -> bool | None:
    """Dispatch a command.

    Returns True  → continue REPL loop
    Returns False → break REPL loop
    Returns None  → not a recognized command, caller should process as normal input
    """
    parts = user_input.split(maxsplit=1)
    cmd = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    for key, (handler, aliases) in COMMAND_REGISTRY.items():
        if cmd == key or cmd in aliases:
            return handler(state, args)

    return None
