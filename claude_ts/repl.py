"""Slim REPL loop (~120 lines)."""

import os
import sys
import time

from claude_ts.state import config, SessionState, _s
from claude_ts.ui import C, dim, error, success
from claude_ts.clipboard import drain_stdin, detect_image_path, get_clipboard_image, stabilize_image_path
from claude_ts.terminal import read_input
from claude_ts.menus import slash_menu_raw, interactive_command_menu, ask_permission_mode
from claude_ts.commands import dispatch
from claude_ts.executor import process_image_turn, process_turn


def repl():
    state = SessionState()

    print(f"  {C.BOLD}━━━ {_s('label_banner_title', 'Claude Code')} ━━━{C.RESET}")
    if config.translate_backend == "ollama":
        translate_label = f"ollama:{config.ollama_model}"
    else:
        translate_label = config.translate_model
    print(
        f"  {C.DIM}{_s('label_translate', 'Translate')}: {translate_label} | "
        f"{_s('label_task_model', 'Task')}: {config.main_model or 'default'} | "
        f"{_s('label_streaming', 'Streaming: ON')}{C.RESET}"
    )
    print(f"  {C.DIM}{_s('label_session', 'Session')}: {state.session_uuid[:8]}...{C.RESET}")
    print()

    # Ask permission mode if not set via CLI flags
    if not config.allowed_tools and not config.dangerously_skip_permissions:
        ask_permission_mode()

    perm_label = (
        _s("label_perm_full", "Full access") if config.dangerously_skip_permissions
        else f"{_s('label_perm_allowed', 'Allowed')}: {config.allowed_tools}" if config.allowed_tools
        else _s("label_perm_readonly", "Read-only")
    )
    print(f"  {C.DIM}{_s('label_permission', 'Permission')}: {perm_label}{C.RESET}")
    print(f"  {C.DIM}{_s('label_slash_hint', 'Type / to see command list')}{C.RESET}")
    print()

    last_ctrl_c = 0.0
    ctrl_c_count = 0
    CTRL_C_WINDOW = 2.0  # seconds

    while True:
        # ── Pick up input buffered during execution (drag-and-drop etc.) ──
        queued = drain_stdin()
        if queued:
            queued_text = "\n".join(queued).strip()
            if queued_text:
                # Check if it's a dragged image file
                img = detect_image_path(queued_text)
                if img is not None:
                    orig_img = img
                    img = stabilize_image_path(img)
                    if img != orig_img:
                        state.track_temp_file(img)
                    if not os.path.isfile(img):
                        dim(_s("msg_image_volatile", "Temp file gone, checking clipboard..."))
                        clip_img = get_clipboard_image()
                        if clip_img:
                            state.track_temp_file(clip_img)
                            img = clip_img
                        else:
                            error(_s("err_image_gone",
                                "Image file removed by macOS. Use Cmd+Shift+Ctrl+4 to copy screenshot to clipboard, then paste."))
                            print()
                            continue
                    size_kb = os.path.getsize(img) / 1024
                    success(f"  🖼  {_s('msg_queued_image', 'Queued image detected')} ({size_kb:.0f}KB)")
                    try:
                        img_q = input(
                            f"  \001{C.DIM}\002{_s('prompt_question', 'Question (Enter=describe)')}: \001{C.RESET}\002"
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
                        print()
                        continue
                    try:
                        process_image_turn(img, img_q, state)
                    except KeyboardInterrupt:
                        print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
                        print()
                    continue
                else:
                    # Show queued text and pre-fill for confirmation
                    dim(f"{_s('msg_queued_input', 'Queued input detected')}: {queued_text[:60]}...")
                    try:
                        confirm = input(
                            f"  \001{C.DIM}\002{_s('msg_send_confirm', 'Press Enter to send, n to cancel')}: \001{C.RESET}\002"
                        ).strip()
                        if confirm.lower() in ("n", "no", "취소"):
                            print(f"  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
                            print()
                            continue
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
                        print()
                        continue
                    try:
                        process_turn(queued_text, state)
                    except KeyboardInterrupt:
                        print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
                        print()
                    continue

        try:
            user_input, is_paste = read_input(
                f"{C.CYAN}  >{C.RESET} ",
                slash_handler=slash_menu_raw,
            )
        except EOFError:
            print()
            dim(_s("msg_session_end", "Ending session."))
            break
        except KeyboardInterrupt:
            now = time.time()
            if now - last_ctrl_c > CTRL_C_WINDOW:
                ctrl_c_count = 1
            else:
                ctrl_c_count += 1
            last_ctrl_c = now

            if ctrl_c_count < 2:
                print()
                continue
            elif ctrl_c_count < 3:
                print()
                print(f"  {C.DIM}{_s('msg_press_once_more', 'Press once more to exit')}{C.RESET}")
                continue
            else:
                print()
                dim(_s("msg_session_end", "Ending session."))
                break

        # Reset on successful input
        ctrl_c_count = 0

        # ── Clipboard image on empty paste (Cmd+V screenshot) ──
        if is_paste and not user_input.strip():
            clip_img = get_clipboard_image()
            if clip_img:
                state.track_temp_file(clip_img)
                size_kb = os.path.getsize(clip_img) / 1024
                success(f"  🖼  {_s('msg_clipboard_image', 'Clipboard image detected')} ({size_kb:.0f}KB)")
                try:
                    img_question = input(
                        f"  \001{C.DIM}\002{_s('prompt_question', 'Question (Enter=describe)')}: \001{C.RESET}\002"
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    print(f"\n  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
                    print()
                    continue
                try:
                    process_image_turn(clip_img, img_question, state)
                except KeyboardInterrupt:
                    print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
                    print()
                continue

        if not user_input:
            continue

        # ── Image file path detection (BEFORE slash stripping) ──
        # Must run before "/" stripping because paths like /var/folders/...
        # would lose the leading "/" and fail detection.
        dragged_path = detect_image_path(user_input)
        if dragged_path is not None:
            # Extract question from remaining text BEFORE stabilizing
            # (need original path for replacement matching)
            img_question = user_input
            for variant in [dragged_path, dragged_path.replace(" ", "\\ ")]:
                img_question = img_question.replace(variant, "")
            img_question = img_question.strip().strip("'\"").strip()

            # Stabilize volatile macOS temp paths (screenshot preview drag)
            stable_path = stabilize_image_path(dragged_path)
            if stable_path != dragged_path:
                state.track_temp_file(stable_path)
            if not os.path.isfile(stable_path):
                # File already gone — try clipboard image as fallback
                dim(_s("msg_image_volatile", "Temp file gone, checking clipboard..."))
                clip_img = get_clipboard_image()
                if clip_img:
                    state.track_temp_file(clip_img)
                    stable_path = clip_img
                else:
                    error(_s("err_image_gone",
                        "Image file removed by macOS. Use Cmd+Shift+Ctrl+4 to copy screenshot to clipboard, then paste."))
                    print()
                    continue
            size_kb = os.path.getsize(stable_path) / 1024
            success(f"  🖼  {_s('msg_image_detected', 'Image detected')} ({size_kb:.0f}KB)")
            if not img_question:
                try:
                    img_question = input(
                        f"  \001{C.DIM}\002{_s('prompt_question', 'Question (Enter=describe)')}: \001{C.RESET}\002"
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    print(f"\n  {C.DIM}{_s('msg_cancelled', 'Cancelled')}{C.RESET}")
                    print()
                    continue
            else:
                dim(f"→ {img_question[:80]}")
            dragged_path = stable_path

            try:
                process_image_turn(dragged_path, img_question, state)
            except KeyboardInterrupt:
                print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
                print()
            continue

        # ── Slash command menu (standalone) ──
        if user_input == "/":
            cmd = interactive_command_menu()
            if cmd is None:
                continue
            user_input = cmd

        # Strip leading / for direct slash commands (e.g. /cost, /help)
        if user_input.startswith("/") and not user_input.startswith("//"):
            user_input = user_input[1:]

        # ── Command dispatch ──
        result = dispatch(state, user_input)
        if result is True:
            continue
        if result is False:
            break

        # ── Process Turn ──
        try:
            process_turn(user_input, state)
        except KeyboardInterrupt:
            print(f"\n  {C.DIM}{_s('msg_task_interrupted', 'Task interrupted')}{C.RESET}")
            print()

    # ── Session cleanup ──
    state.cleanup_temp_files()
