"""REPL command handlers and dispatch registry."""

import os
import subprocess
import sys
import time

from claude_kr.state import config, SessionState, clean_env
from claude_kr.tokens import fmt_tokens
from claude_kr.ui import C, dim, error, success, render_markdown, SpinnerContext
from claude_kr.clipboard import get_clipboard_image
from claude_kr.ollama import _ollama_available, _ollama_list_models
from claude_kr.executor import execute_streaming, process_image_turn
from claude_kr.translation import translate
from claude_kr.menus import interactive_tool_selector


# ── Command Handlers ────────────────────────────────────────────────────────
# Each handler: def cmd_xxx(state: SessionState, args: str) -> bool
#   Returns True  → continue REPL loop
#   Returns False → break REPL loop


def cmd_exit(state: SessionState, args: str) -> bool:
    dim("세션을 종료합니다.")
    return False


def cmd_help(state: SessionState, args: str) -> bool:
    print(f"  {C.BOLD}━━━ claude-kr 도움말 ━━━{C.RESET}")
    print(f"  {C.DIM}/ 를 입력하면 명령어 목록이 표시됩니다{C.RESET}")
    print()
    print(f"  {C.BOLD}기본 명령어{C.RESET}")
    print(f"    {C.CYAN}/help{C.RESET}          도움말 표시")
    print(f"    {C.CYAN}/exit{C.RESET}          종료")
    print(f"    {C.CYAN}/clear{C.RESET}         대화 기록 초기화 (= /reset)")
    print()
    print(f"  {C.BOLD}세션 관리{C.RESET}")
    print(f"    {C.CYAN}/model{C.RESET} [이름]   작업 모델 변경")
    print(f"    {C.CYAN}/ollama{C.RESET}         번역 백엔드 변경 (claude/ollama)")
    print(f"    {C.CYAN}/rename{C.RESET} [이름]  세션 이름 변경")
    print(f"    {C.CYAN}/compact{C.RESET} [지시] 대화 컨텍스트 압축")
    print(f"    {C.CYAN}/cost{C.RESET}          토큰 사용량 표시")
    print(f"    {C.CYAN}/stats{C.RESET}         세션 통계 시각화")
    print(f"    {C.CYAN}/copy{C.RESET}          마지막 응답 클립보드 복사")
    print(f"    {C.CYAN}/export{C.RESET} [파일]  대화 내역 파일 저장")
    print()
    print(f"  {C.BOLD}프로젝트{C.RESET}")
    print(f"    {C.CYAN}/init{C.RESET}          CLAUDE.md 초기화")
    print(f"    {C.CYAN}/memory{C.RESET}        CLAUDE.md 편집")
    print(f"    {C.CYAN}/config{C.RESET}        Claude Code 설정")
    print(f"    {C.CYAN}/doctor{C.RESET}        설치 상태 점검")
    print()
    print(f"  {C.BOLD}권한{C.RESET}")
    print(f"    {C.CYAN}/allow{C.RESET} [도구]   허용 도구 변경")
    print(f"    {C.CYAN}/yolo{C.RESET}          전체 허용 모드")
    print(f"    {C.CYAN}/debug{C.RESET}         디버그 모드 토글")
    print()
    print(f"  {C.BOLD}이미지{C.RESET}")
    print(f"    {C.CYAN}/img{C.RESET} [질문]     클립보드 이미지 분석")
    print(f"    {C.DIM}이미지 파일 드래그앤드롭 → 자동 감지{C.RESET}")
    print()
    print(f"  {C.BOLD}특수 입력{C.RESET}")
    print(f"    {C.DIM}raw:<텍스트>     번역 없이 직접 전송{C.RESET}")
    print()
    return True


def cmd_cost(state: SessionState, args: str) -> bool:
    s = state.stats
    total = s.input_tokens + s.output_tokens
    print(f"  {C.BOLD}━━━ 세션 사용량 ━━━{C.RESET}")
    print(f"    턴:    {s.turn_count}회")
    print(f"    입력:  {fmt_tokens(s.input_tokens)}")
    print(f"    출력:  {fmt_tokens(s.output_tokens)}")
    if s.cache_read_tokens > 0:
        print(f"    캐시:  {fmt_tokens(s.cache_read_tokens)}")
    print(f"    총합:  {fmt_tokens(total)}")
    if s.tool_count > 0:
        print(f"    도구:  {s.tool_count}회")
    if s.thinking_count > 0:
        print(f"    생각:  {s.thinking_count}회")
    print(f"    비용:  ${s.total_cost_usd:.4f}")
    print()
    return True


def cmd_clear(state: SessionState, args: str) -> bool:
    state.reset()
    success(f"새 세션 시작: {state.session_uuid[:8]}...")
    print()
    return True


def cmd_copy(state: SessionState, args: str) -> bool:
    if not state.last_assistant_response:
        error("복사할 응답이 없습니다.")
    else:
        try:
            subprocess.run(
                ["pbcopy"],
                input=state.last_assistant_response,
                text=True,
                timeout=5,
            )
            preview = state.last_assistant_response[:60].replace("\n", " ")
            success(f"클립보드에 복사됨: \"{preview}...\"")
        except FileNotFoundError:
            error("pbcopy를 찾을 수 없습니다 (macOS 전용)")
        except subprocess.TimeoutExpired:
            error("클립보드 복사 시간 초과")
    print()
    return True


def cmd_export(state: SessionState, args: str) -> bool:
    if not state.conversation_history:
        error("저장할 대화 내역이 없습니다.")
        print()
        return True
    if args:
        export_path = args.strip()
    else:
        name_part = state.session_name or state.session_uuid[:8]
        ts_part = time.strftime("%Y%m%d-%H%M%S")
        export_path = f"claude-kr-{name_part}-{ts_part}.md"
    try:
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(f"# Claude-KR 대화 기록\n\n")
            f.write(f"- 세션: {state.session_name or state.session_uuid[:8]}\n")
            f.write(f"- 날짜: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"- 모델: {config.main_model or 'default'}\n")
            f.write(f"- 턴: {state.stats.turn_count}회\n\n---\n\n")
            for entry in state.conversation_history:
                role = "사용자" if entry["role"] == "user" else "Claude"
                f.write(f"## {role} ({entry['ts']})\n\n")
                f.write(entry["text"] + "\n\n---\n\n")
        success(f"대화 내역 저장: {export_path}")
    except OSError as e:
        error(f"파일 저장 실패: {e}")
    print()
    return True


def cmd_rename(state: SessionState, args: str) -> bool:
    if args:
        state.session_name = args.strip()
        if state.session_name:
            success(f"세션 이름 변경: {state.session_name}")
        else:
            dim("이름이 비어있어 변경하지 않았습니다.")
    else:
        try:
            new_name = input(
                f"  \001{C.DIM}\002새 세션 이름: \001{C.RESET}\002"
            ).strip()
            if new_name:
                state.session_name = new_name
                success(f"세션 이름 변경: {state.session_name}")
            else:
                dim("이름이 비어있어 변경하지 않았습니다.")
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
    dim("대화 컨텍스트 압축 중...")
    en_output = execute_streaming(compact_prompt, state)
    if en_output and en_output.strip():
        with SpinnerContext("결과 번역 중..."):
            kr_output = translate(en_output, "en2kr")
        print()
        render_markdown(kr_output)
        print()
        success("컨텍스트 압축 완료")
    else:
        error("압축 실패")
    print()
    return True


def cmd_doctor(state: SessionState, args: str) -> bool:
    dim("Claude Code 설치 상태 점검 중...")
    try:
        result = subprocess.run(
            ["claude", "doctor"],
            capture_output=True, text=True, timeout=30,
            env=clean_env(),
        )
        if result.stdout.strip():
            print()
            print(result.stdout)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)
    except FileNotFoundError:
        error("claude 명령어를 찾을 수 없습니다.")
    except subprocess.TimeoutExpired:
        error("시간 초과 (30초)")

    # Ollama status
    if _ollama_available():
        models = _ollama_list_models()
        print(f"  {C.GREEN}✓{C.RESET} Ollama 설치됨 — 모델 {len(models)}개")
    else:
        print(f"  {C.DIM}– Ollama 미설치{C.RESET}")
    print()
    return True


def cmd_config(state: SessionState, args: str) -> bool:
    dim("Claude Code 설정을 열고 있습니다...")
    try:
        subprocess.run(
            ["claude", "config"],
            timeout=60,
            env=clean_env(),
        )
    except FileNotFoundError:
        error("claude 명령어를 찾을 수 없습니다.")
    except subprocess.TimeoutExpired:
        error("시간 초과")
    except KeyboardInterrupt:
        pass
    print()
    return True


def cmd_init(state: SessionState, args: str) -> bool:
    claude_md = os.path.join(os.getcwd(), "CLAUDE.md")
    if os.path.exists(claude_md):
        dim(f"CLAUDE.md가 이미 존재합니다: {claude_md}")
        try:
            confirm = input(
                f"  \001{C.DIM}\002덮어쓰시겠습니까? (y/N): \001{C.RESET}\002"
            ).strip()
            if confirm.lower() not in ("y", "yes"):
                dim("취소됨")
                print()
                return True
        except (EOFError, KeyboardInterrupt):
            print()
            return True
    dim("CLAUDE.md 초기화 중...")
    try:
        result = subprocess.run(
            ["claude", "-p", "Generate a CLAUDE.md file for this project. "
             "Analyze the project structure and create appropriate guidelines. "
             "Output ONLY the markdown content for CLAUDE.md."],
            capture_output=True, text=True, timeout=60,
            env=clean_env(),
        )
        if result.returncode == 0 and result.stdout.strip():
            with open(claude_md, "w", encoding="utf-8") as f:
                f.write(result.stdout.strip() + "\n")
            success(f"CLAUDE.md 생성 완료: {claude_md}")
        else:
            error("CLAUDE.md 생성 실패")
    except subprocess.TimeoutExpired:
        error("시간 초과 (60초)")
    print()
    return True


def cmd_memory(state: SessionState, args: str) -> bool:
    claude_md = os.path.join(os.getcwd(), "CLAUDE.md")
    if not os.path.exists(claude_md):
        error(f"CLAUDE.md가 없습니다. /init으로 먼저 생성하세요.")
        print()
        return True
    editor = os.environ.get("EDITOR", "vim")
    dim(f"{editor}로 CLAUDE.md를 열고 있습니다...")
    try:
        subprocess.run([editor, claude_md])
        success("CLAUDE.md 편집 완료")
    except FileNotFoundError:
        error(f"에디터를 찾을 수 없습니다: {editor}")
        dim("EDITOR 환경변수를 설정하세요.")
    except KeyboardInterrupt:
        pass
    print()
    return True


def cmd_stats(state: SessionState, args: str) -> bool:
    s = state.stats
    total = s.input_tokens + s.output_tokens
    elapsed_min = (time.time() - state.session_start_time) / 60
    print(f"  {C.BOLD}━━━ 세션 통계 ━━━{C.RESET}")
    print()
    name_str = state.session_name if state.session_name else state.session_uuid[:8]
    print(f"  {C.CYAN}세션{C.RESET}   {name_str}")
    print(f"  {C.CYAN}시간{C.RESET}   {elapsed_min:.1f}분")
    print(f"  {C.CYAN}모델{C.RESET}   {config.main_model or 'default'}")
    if config.translate_backend == "ollama":
        print(f"  {C.CYAN}번역{C.RESET}   ollama:{config.ollama_model}")
    else:
        print(f"  {C.CYAN}번역{C.RESET}   {config.translate_model}")
    print()
    print(f"  {C.BOLD}토큰 사용량{C.RESET}")
    # Bar chart
    max_val = max(s.input_tokens, s.output_tokens, 1)
    in_bar = int(s.input_tokens / max_val * 20)
    out_bar = int(s.output_tokens / max_val * 20)
    print(f"    입력  {C.BLUE}{'█' * in_bar}{'░' * (20 - in_bar)}{C.RESET} {fmt_tokens(s.input_tokens)}")
    print(f"    출력  {C.GREEN}{'█' * out_bar}{'░' * (20 - out_bar)}{C.RESET} {fmt_tokens(s.output_tokens)}")
    if s.cache_read_tokens > 0:
        cache_bar = int(s.cache_read_tokens / max_val * 20)
        print(f"    캐시  {C.YELLOW}{'█' * cache_bar}{'░' * (20 - cache_bar)}{C.RESET} {fmt_tokens(s.cache_read_tokens)}")
    print(f"    총합  {fmt_tokens(total)}")
    print()
    print(f"  {C.BOLD}활동{C.RESET}")
    print(f"    턴:        {s.turn_count}회")
    print(f"    도구 사용: {s.tool_count}회")
    print(f"    생각:      {s.thinking_count}회")
    print(f"    대화 기록: {len(state.conversation_history)}건")
    print(f"    비용:      ${s.total_cost_usd:.4f}")
    print()
    return True


def cmd_debug(state: SessionState, args: str) -> bool:
    config.debug = not config.debug
    label = "ON" if config.debug else "OFF"
    print(f"  {C.YELLOW}디버그 모드: {label}{C.RESET}")
    print()
    return True


def cmd_model(state: SessionState, args: str) -> bool:
    if args:
        config.main_model = args.strip()
        if config.main_model == "default":
            config.main_model = ""
        success(f"작업 모델 변경: {config.main_model or 'default'}")
    else:
        # Interactive model selection
        models = ["default", "opus", "sonnet", "haiku"]
        current = config.main_model or "default"
        print(f"  {C.DIM}현재 모델: {current}{C.RESET}")
        for i, m in enumerate(models):
            marker = f"{C.GREEN}*{C.RESET}" if m == current else " "
            print(f"    {marker} {i+1}) {m}")
        try:
            choice = input(
                f"  \001{C.DIM}\002선택 [1-{len(models)}]: \001{C.RESET}\002"
            ).strip()
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                config.main_model = "" if models[idx] == "default" else models[idx]
                success(f"작업 모델 변경: {config.main_model or 'default'}")
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
    print()
    return True


def cmd_ollama(state: SessionState, args: str) -> bool:
    options: list[tuple[str, str]] = [
        ("claude", f"claude (haiku) {'← 현재' if config.translate_backend == 'claude' else ''}"),
    ]
    if _ollama_available():
        models = _ollama_list_models()
        if models:
            for m in models:
                current = (
                    "← 현재"
                    if config.translate_backend == "ollama" and config.ollama_model == m
                    else ""
                )
                options.append(("ollama:" + m, f"ollama:{m} {current}"))
        else:
            dim("Ollama가 설치되었지만 모델이 없습니다. ollama pull <model>로 다운로드하세요.")
    else:
        dim("Ollama가 설치되지 않았습니다. https://ollama.com 에서 설치하세요.")

    print(f"  {C.BOLD}번역 백엔드 선택{C.RESET}")
    for i, (key, label) in enumerate(options):
        print(f"    {C.CYAN}{i+1}{C.RESET}) {label}")
    try:
        choice = input(
            f"  \001{C.DIM}\002선택 [1-{len(options)}]: \001{C.RESET}\002"
        ).strip()
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            key = options[idx][0]
            if key == "claude":
                config.translate_backend = "claude"
                config.ollama_model = ""
                success("번역 백엔드: claude (haiku)")
            else:
                model_name = key.split(":", 1)[1]
                config.translate_backend = "ollama"
                config.ollama_model = model_name
                success(f"번역 백엔드: ollama:{model_name}")
    except (ValueError, EOFError, KeyboardInterrupt):
        print()
    print()
    return True


def cmd_allow(state: SessionState, args: str) -> bool:
    if args:
        config.allowed_tools = args.strip()
        config.dangerously_skip_permissions = False
        success(f"허용 도구 변경: {config.allowed_tools}")
    else:
        print()
        tools = interactive_tool_selector()
        if tools:
            config.allowed_tools = tools
            config.dangerously_skip_permissions = False
            success(f"허용 도구 변경: {tools}")
        else:
            dim("도구 허용 없음 — 읽기 전용 모드")
    print()
    return True


def cmd_img(state: SessionState, args: str) -> bool:
    dim("📋 클립보드에서 이미지 확인 중...")
    img_path = get_clipboard_image()
    if img_path is None:
        error("클립보드에 이미지가 없습니다. (스크린샷을 먼저 복사하세요)")
        print()
        return True

    size_kb = os.path.getsize(img_path) / 1024
    success(f"  이미지 저장: {os.path.basename(img_path)} ({size_kb:.0f}KB)")

    try:
        process_image_turn(img_path, args.strip(), state)
    except KeyboardInterrupt:
        print(f"\n  {C.DIM}작업 중단됨{C.RESET}")
        print()
    try:
        os.unlink(img_path)
    except OSError:
        pass
    return True


def cmd_yolo(state: SessionState, args: str) -> bool:
    config.dangerously_skip_permissions = True
    config.allowed_tools = ""
    success("전체 허용 모드 활성화")
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
    "config":  (cmd_config,  set()),
    "init":    (cmd_init,    set()),
    "memory":  (cmd_memory,  set()),
    "stats":   (cmd_stats,   set()),
    "debug":   (cmd_debug,   {"디버그"}),
    "model":   (cmd_model,   set()),
    "ollama":  (cmd_ollama,  set()),
    "allow":   (cmd_allow,   set()),
    "img":     (cmd_img,     {"이미지", "image"}),
    "yolo":    (cmd_yolo,    set()),
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
