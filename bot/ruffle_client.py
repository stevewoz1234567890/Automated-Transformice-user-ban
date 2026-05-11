"""
Ruffle-based game client detection for Transformice.

Ruffle is a Rust-built Flash Player emulator.  This module provides
helpers to locate a Ruffle binary and gather diagnostic info.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def find_ruffle_binary() -> Path | None:
    """Search PATH and common locations for a ``ruffle`` desktop player."""
    env = (os.environ.get("BOT_RUFFLE_BINARY_PATH") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    found = shutil.which("ruffle")
    if found:
        return Path(found)

    home = Path.home()
    extra_candidates: list[Path] = []
    if os.name == "nt":
        extra_candidates = [
            home / "ruffle" / "ruffle.exe",
            Path(r"C:\ruffle\ruffle.exe"),
            home / "Downloads" / "ruffle" / "ruffle.exe",
            home / ".ruffle" / "ruffle.exe",
        ]
    else:
        extra_candidates = [
            home / ".ruffle" / "ruffle",
            home / "ruffle" / "ruffle",
            Path("/usr/local/bin/ruffle"),
        ]

    for c in extra_candidates:
        if c.is_file():
            return c
    return None


def ruffle_version(binary: Path) -> str | None:
    """Get Ruffle version string from the binary."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or result.stderr.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def describe_ruffle_setup() -> dict:
    """Gather diagnostic info about Ruffle availability."""
    binary = find_ruffle_binary()
    ws_bin = shutil.which("websockify")
    info: dict = {
        "ruffle_found": binary is not None,
        "ruffle_path": str(binary) if binary else None,
        "ruffle_version": ruffle_version(binary) if binary else None,
        "websockify_found": ws_bin is not None,
        "websockify_path": ws_bin,
    }
    return info
