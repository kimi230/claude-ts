"""Image detection, clipboard access, stdin draining."""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import tempfile

from claude_ts.ui import error, dbg


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".svg"}


def _clean_path(candidate: str) -> str:
    """Clean a dragged/pasted path string."""
    path = candidate.strip().strip("'\"")
    path = path.replace("\\ ", " ")
    return path


def _try_image_path(candidate: str) -> str | None:
    """Try to resolve a single candidate string as an image file path.

    Returns the path if it has an image extension AND either:
    - The file currently exists on disk, OR
    - It looks like a valid absolute path (for volatile temp files that may
      have been cleaned up by macOS before we could check).
    """
    path = _clean_path(candidate)
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS:
        return None
    # File exists — definite match
    if os.path.isfile(path):
        return path
    # File gone but path looks like a valid absolute image path
    # (e.g. macOS temp screenshot already cleaned up)
    if path.startswith("/") and "/" in path[1:]:
        return path
    return None


def detect_image_path(text: str) -> str | None:
    """If text looks like a dragged image file path, return the cleaned path.

    Handles multi-line input where the path may be on the first line
    (e.g. pasted path + typed question separated by newlines).
    """
    # Try the whole text first (single-line drag-and-drop)
    result = _try_image_path(text)
    if result:
        return result
    # Try each line (paste + typed question combo)
    for line in text.split("\n"):
        result = _try_image_path(line)
        if result:
            return result
    return None


def stabilize_image_path(path: str) -> str:
    """Copy image to a stable temp location if it's in a volatile macOS directory.

    macOS screenshot previews (drag from floating thumbnail) are stored in
    /var/folders/.../TemporaryItems/NSIRD_screencaptureui_*/ and get cleaned up
    within seconds. Copy to our own temp file to prevent loss.
    """
    volatile_markers = ("TemporaryItems", "NSIRD_screencaptureui")
    if any(m in path for m in volatile_markers):
        ext = os.path.splitext(path)[1] or ".png"
        stable = tempfile.NamedTemporaryFile(
            suffix=ext, prefix="claude-ts-img-", delete=False,
        )
        stable_path = stable.name
        stable.close()
        try:
            shutil.copy2(path, stable_path)
            return stable_path
        except (OSError, shutil.Error):
            # Original file already gone — clean up empty temp file
            try:
                os.unlink(stable_path)
            except OSError:
                pass
            return path
    return path


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
            suffix=".png", prefix="claude-ts-img-", delete=False,
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
    except Exception as e:
        dbg(f"get_clipboard_image failed: {e}")
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
