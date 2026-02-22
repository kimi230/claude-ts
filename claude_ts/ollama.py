"""Ollama local model helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request

from claude_ts.ui import error
from claude_ts.state import _s


def _ollama_available() -> bool:
    """Check if the ollama CLI is installed."""
    return shutil.which("ollama") is not None


def _ollama_list_models() -> list[str]:
    """Return list of locally available ollama model names."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.strip().splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _ollama_generate(prompt: str, model: str) -> str | None:
    """Call Ollama's /api/generate endpoint (non-streaming)."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "").strip() or None
    except urllib.error.URLError as e:
        error(f"{_s('err_ollama_connect', 'Ollama server connection failed')}: {e.reason}")
        return None
    except json.JSONDecodeError:
        error(_s("err_ollama_parse", "Ollama response parse failed"))
        return None
    except TimeoutError:
        error(_s("err_ollama_timeout", "Ollama response timeout (120s)"))
        return None
