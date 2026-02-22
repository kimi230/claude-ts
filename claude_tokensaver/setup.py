"""First-run language selection flow."""

import sys

from claude_tokensaver.state import available_languages, save_user_config, config
from claude_tokensaver.ui import C


def select_language() -> str:
    """Interactive language selection. Returns language code."""
    langs = available_languages()
    if not langs:
        print(f"{C.RED}No language configs found.{C.RESET}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"{C.CYAN}{C.BOLD}🌐 Select your language:{C.RESET}")
    print()
    for i, lang in enumerate(langs, 1):
        print(f"  {C.BOLD}{i}.{C.RESET} {lang['name']} ({lang['name_en']})")
    print()

    while True:
        try:
            choice = input(f"  {C.DIM}>{C.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not choice:
            continue

        # Accept number or language code
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(langs):
                selected = langs[idx]
                break
        except ValueError:
            # Try matching by code
            for lang in langs:
                if lang["code"] == choice.lower():
                    selected = lang
                    break
            else:
                print(f"  {C.RED}Invalid choice. Enter 1-{len(langs)} or a language code.{C.RESET}")
                continue
            break

        print(f"  {C.RED}Invalid choice. Enter 1-{len(langs)} or a language code.{C.RESET}")

    # Save to config
    save_user_config({"language": selected["code"]})
    config.language = selected["code"]

    print(f"  {C.GREEN}✓{C.RESET} {selected['name']} ({selected['name_en']}) — saved to ~/.claude-tokensaver/config.json")
    print()
    return selected["code"]
