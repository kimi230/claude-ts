"""Image detection, clipboard access, stdin draining."""

import os
import select
import subprocess
import sys
import tempfile

from claude_kr.ui import error


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".svg"}


def detect_image_path(text: str) -> str | None:
    """If text looks like a dragged image file path, return the cleaned path."""
    # Terminal drag-and-drop may quote or escape paths
    path = text.strip().strip("'\"")
    # Handle backslash-escaped spaces (e.g. /path/to/my\ image.png)
    path = path.replace("\\ ", " ")
    if os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMAGE_EXTS:
        return path
    return None


def get_clipboard_image() -> str | None:
    """Check macOS clipboard for image data and save to temp file. Returns path or None."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        info = result.stdout
        has_png = "\u00abclass PNGf\u00bb" in info
        has_tiff = "\u00abclass TIFF\u00bb" in info
        if not has_png and not has_tiff:
            return None

        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", prefix="claude-kr-img-", delete=False,
        )
        tmp_path = tmp.name
        tmp.close()

        fmt = "\u00abclass PNGf\u00bb" if has_png else "\u00abclass TIFF\u00bb"
        save_result = subprocess.run(
            [
                "osascript",
                "-e", f"set img_data to the clipboard as {fmt}",
                "-e", f'set fp to open for access POSIX file "{tmp_path}" with write permission',
                "-e", "write img_data to fp",
                "-e", "close access fp",
            ],
            capture_output=True, text=True, timeout=10,
        )

        if save_result.returncode == 0 and os.path.getsize(tmp_path) > 0:
            return tmp_path

        os.unlink(tmp_path)
        return None
    except Exception:
        return None


def drain_stdin() -> list[str]:
    """Read remaining buffered lines from stdin (catches multi-line paste)."""
    lines = []
    try:
        while select.select([sys.stdin], [], [], 0.05)[0]:
            line = sys.stdin.readline()
            if line:
                lines.append(line.rstrip("\n"))
            else:
                break
    except (OSError, ValueError):
        pass
    return lines
