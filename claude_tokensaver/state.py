"""Global configuration and session state."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

BASE_DIR = os.path.join(os.path.expanduser("~"), ".claude-tokensaver")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BUNDLED_LANGUAGES_DIR = os.path.join(os.path.dirname(__file__), "languages")


class Config:
    translate_model: str = "haiku"
    main_model: str = ""
    debug: bool = False
    allowed_tools: str = ""          # e.g. "Edit Write Bash"
    dangerously_skip_permissions: bool = False
    translate_backend: str = "claude"  # "claude" or "ollama"
    ollama_model: str = ""             # e.g. "gemma3:4b"
    language: str = ""                 # language code, e.g. "ko", "th"


config = Config()


# ── Language Config ─────────────────────────────────────────────────────────

_lang_cache: dict | None = None
_lang_cache_all: dict[str, dict] = {}  # multi-language cache for detect_language
_lang_lock = threading.Lock()


def available_languages() -> list[dict]:
    """List all bundled language configs, sorted by code."""
    langs = []
    if not os.path.isdir(BUNDLED_LANGUAGES_DIR):
        return langs
    for fname in sorted(os.listdir(BUNDLED_LANGUAGES_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(BUNDLED_LANGUAGES_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
                langs.append({"code": data["code"], "name": data["name"], "name_en": data["name_en"]})
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return langs


def load_language(code: str) -> dict:
    """Load language config by code. Returns the full language dict."""
    global _lang_cache
    with _lang_lock:
        if _lang_cache and _lang_cache.get("code") == code:
            return _lang_cache

        path = os.path.join(BUNDLED_LANGUAGES_DIR, f"{code}.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Language config not found: {code}")

        with open(path, encoding="utf-8") as f:
            _lang_cache = json.load(f)
        return _lang_cache


def detect_language(text: str) -> str | None:
    """Detect language from text using bundled regex patterns. Returns code or None."""
    for lang in available_languages():
        code = lang["code"]
        try:
            # Use multi-language cache to avoid repeated file reads
            if code not in _lang_cache_all:
                _lang_cache_all[code] = load_language(code)
            data = _lang_cache_all[code]
            if re.search(data["detect_regex"], text):
                return code
        except (FileNotFoundError, KeyError):
            continue
    return None


def get_ui_string(key: str, fallback: str = "") -> str:
    """Get a localized UI string for the current language."""
    if not config.language:
        return fallback
    try:
        data = load_language(config.language)
        return data.get("ui_strings", {}).get(key, fallback)
    except FileNotFoundError:
        return fallback


# Short alias for get_ui_string — use across all modules
_s = get_ui_string


def load_user_config() -> dict:
    """Load user config from ~/.claude-tokensaver/config.json."""
    if not os.path.isfile(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_config(data: dict):
    """Save user config to ~/.claude-tokensaver/config.json (atomic write)."""
    import tempfile
    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        existing = load_user_config()
    except Exception:
        existing = {}
    existing.update(data)
    # Write to temp file then atomically rename to prevent data loss on crash
    fd, tmp_path = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def init_language():
    """Initialize language from saved config. Returns True if configured."""
    user_cfg = load_user_config()
    lang = user_cfg.get("language", "")
    if lang:
        config.language = lang
        return True
    return False


def init_translation_backend():
    """Initialize translation backend from saved config."""
    user_cfg = load_user_config()
    backend = user_cfg.get("translate_backend", "")
    model = user_cfg.get("ollama_model", "")
    if backend:
        config.translate_backend = backend
    if model:
        config.ollama_model = model


MAX_CONTEXT_TURNS = 3
MAX_CONTEXT_CHARS = 600


class SessionStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.turn_count: int = 0
        self.tool_count: int = 0
        self.thinking_count: int = 0
        self.total_cost_usd: float = 0.0


class SessionState:
    def __init__(self):
        self.session_uuid: str = str(uuid.uuid4())
        self.stats: SessionStats = SessionStats()
        self._turn_count_override: int | None = None  # for /resume
        self.conversation_context: list[dict[str, str]] = []
        self.conversation_history: list[dict[str, str]] = []
        self.session_name: str = ""
        self.first_input: str = ""
        self.last_assistant_response: str = ""
        self.session_start_time: float = time.time()
        self.temp_files: list[str] = []

    @property
    def turn_count(self) -> int:
        """Single source of truth for turn count."""
        if self._turn_count_override is not None:
            return self._turn_count_override
        return self.stats.turn_count

    @turn_count.setter
    def turn_count(self, value: int):
        """Set turn count override (used by /resume)."""
        self._turn_count_override = value

    def track_temp_file(self, path: str):
        """Register a temp file for cleanup on session end."""
        if path and path not in self.temp_files:
            self.temp_files.append(path)

    def cleanup_temp_files(self):
        """Delete all tracked temp files."""
        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_files.clear()

    def reset(self):
        self.cleanup_temp_files()
        self.session_uuid = str(uuid.uuid4())
        self._turn_count_override = None
        self.stats.reset()
        self.conversation_context.clear()
        self.conversation_history.clear()
        self.last_assistant_response = ""
        self.session_name = ""
        self.first_input = ""
        self.session_start_time = time.time()


def clean_env() -> dict:
    """Environment without CLAUDECODE to prevent nested-session error."""
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def save_session_record(state: "SessionState"):
    """Save session metadata to disk for /resume."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    record = {
        "uuid": state.session_uuid,
        "name": state.session_name,
        "model": config.main_model or "default",
        "turns": state.turn_count,
        "cost": state.stats.total_cost_usd,
        "cwd": os.getcwd(),
        "preview": state.first_input[:80] if state.first_input else "",
        "started": time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(state.session_start_time),
        ),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = os.path.join(SESSIONS_DIR, f"{state.session_uuid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)


def list_session_records() -> list[dict]:
    """List saved session records, newest first."""
    if not os.path.isdir(SESSIONS_DIR):
        return []
    records = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fname), encoding="utf-8") as f:
                records.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    records.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return records
