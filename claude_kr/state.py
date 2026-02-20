"""Global configuration and session state."""

import os
import time
import uuid


class Config:
    translate_model: str = "haiku"
    main_model: str = ""
    debug: bool = False
    allowed_tools: str = ""          # e.g. "Edit Write Bash"
    dangerously_skip_permissions: bool = False
    translate_backend: str = "claude"  # "claude" or "ollama"
    ollama_model: str = ""             # e.g. "gemma3:4b"


config = Config()


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
        self.turn_count: int = 0
        self.stats: SessionStats = SessionStats()
        self.conversation_context: list[dict[str, str]] = []
        self.conversation_history: list[dict[str, str]] = []
        self.session_name: str = ""
        self.last_assistant_response: str = ""
        self.session_start_time: float = time.time()

    def reset(self):
        self.session_uuid = str(uuid.uuid4())
        self.turn_count = 0
        self.stats.reset()
        self.conversation_context.clear()
        self.conversation_history.clear()
        self.last_assistant_response = ""
        self.session_name = ""
        self.session_start_time = time.time()


def clean_env() -> dict:
    """Environment without CLAUDECODE to prevent nested-session error."""
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
