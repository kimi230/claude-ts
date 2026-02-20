"""Slim REPL loop (~120 lines)."""

import os
import sys
import time

from claude_kr.state import config, SessionState
from claude_kr.ui import C, dim, success
from claude_kr.clipboard import drain_stdin, detect_image_path
from claude_kr.terminal import read_input
from claude_kr.menus import slash_menu_raw, interactive_command_menu, ask_permission_mode
from claude_kr.commands import dispatch
from claude_kr.executor import process_image_turn, process_turn


def repl():
    state = SessionState()

    print(f"  {C.BOLD}━━━ Claude Code (한국어 모드) ━━━{C.RESET}")
    if config.translate_backend == "ollama":
        translate_label = f"ollama:{config.ollama_model}"
    else:
        translate_label = config.translate_model
    print(
        f"  {C.DIM}번역: {translate_label} | "
        f"작업: {config.main_model or 'default'} | "
        f"스트리밍: ON{C.RESET}"
    )
    print(f"  {C.DIM}세션: {state.session_uuid[:8]}...{C.RESET}")
    print()

    # Ask permission mode if not set via CLI flags
    if not config.allowed_tools and not config.dangerously_skip_permissions:
        ask_permission_mode()

    perm_label = (
        "전체 허용" if config.dangerously_skip_permissions
        else f"허용: {config.allowed_tools}" if config.allowed_tools
        else "읽기 전용"
    )
    print(f"  {C.DIM}권한: {perm_label}{C.RESET}")
    print(f"  {C.DIM}/ 를 입력하면 명령어 목록이 표시됩니다{C.RESET}")
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
                    size_kb = os.path.getsize(img) / 1024
                    success(f"  🖼  대기 중 이미지 감지: {os.path.basename(img)} ({size_kb:.0f}KB)")
                    try:
                        img_q = input(
                            f"  \001{C.DIM}\002질문 (Enter=설명 요청): \001{C.RESET}\002"
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n  {C.DIM}취소됨{C.RESET}")
                        print()
                        continue
                    try:
                        process_image_turn(img, img_q, state)
                    except KeyboardInterrupt:
                        print(f"\n  {C.DIM}작업 중단됨{C.RESET}")
                        print()
                    continue
                else:
                    # Show queued text and pre-fill for confirmation
                    dim(f"대기 입력 감지: {queued_text[:60]}...")
                    try:
                        confirm = input(
                            f"  \001{C.DIM}\002전송하려면 Enter, 취소는 n: \001{C.RESET}\002"
                        ).strip()
                        if confirm.lower() in ("n", "no", "취소"):
                            print(f"  {C.DIM}취소됨{C.RESET}")
                            print()
                            continue
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n  {C.DIM}취소됨{C.RESET}")
                        print()
                        continue
                    try:
                        process_turn(queued_text, state)
                    except KeyboardInterrupt:
                        print(f"\n  {C.DIM}작업 중단됨{C.RESET}")
                        print()
                    continue

        try:
            user_input, is_paste = read_input(
                f"{C.CYAN}  >{C.RESET} ",
                slash_handler=slash_menu_raw,
            )
        except EOFError:
            print()
            dim("세션을 종료합니다.")
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
                print(f"  {C.DIM}한번 더 누르면 종료됩니다{C.RESET}")
                continue
            else:
                print()
                dim("세션을 종료합니다.")
                break

        # Reset on successful input
        ctrl_c_count = 0

        if not user_input:
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

        # ── Image file path detection (drag-and-drop) ──
        dragged_path = detect_image_path(user_input)
        if dragged_path is not None:
            size_kb = os.path.getsize(dragged_path) / 1024
            success(f"  🖼  이미지 감지: {os.path.basename(dragged_path)} ({size_kb:.0f}KB)")
            try:
                img_question = input(
                    f"  \001{C.DIM}\002질문 (Enter=설명 요청): \001{C.RESET}\002"
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {C.DIM}취소됨{C.RESET}")
                print()
                continue

            try:
                process_image_turn(dragged_path, img_question, state)
            except KeyboardInterrupt:
                print(f"\n  {C.DIM}작업 중단됨{C.RESET}")
                print()
            continue

        # ── Process Turn ──
        try:
            process_turn(user_input, state)
        except KeyboardInterrupt:
            print(f"\n  {C.DIM}작업 중단됨{C.RESET}")
            print()
