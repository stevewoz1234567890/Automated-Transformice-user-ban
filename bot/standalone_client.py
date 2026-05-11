"""
Standalone ``Transformice.exe`` client detection.

The official standalone is an Adobe AIR wrapper that bundles Flash and
the game SWF, downloadable from ``transformice.com/Transformice.exe``.
This module provides helpers to locate, download, and gather diagnostic
info about the standalone client.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

OFFICIAL_DOWNLOAD_URL = "http://www.transformice.com/Transformice.exe"


def find_standalone_exe(repo_root: Path) -> Path | None:
    """Look for ``Transformice.exe`` in env, repo root, or tmp/."""
    env = (os.environ.get("BOT_STANDALONE_EXE_PATH") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    candidates = [
        repo_root / "Transformice.exe",
        repo_root / "tmp" / "Transformice.exe",
    ]
    if os.name == "nt":
        dl = Path.home() / "Downloads" / "Transformice.exe"
        candidates.append(dl)

    for c in candidates:
        if c.is_file():
            return c
    return None


def download_standalone(dest: Path, *, timeout: float = 120.0) -> bool:
    """Download the official ``Transformice.exe`` standalone."""
    logger.info("Downloading standalone from %s → %s", OFFICIAL_DOWNLOAD_URL, dest)
    req = urllib.request.Request(
        OFFICIAL_DOWNLOAD_URL,
        headers={"User-Agent": "TFM-StandaloneClient/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        logger.error("Standalone download failed: %s", e)
        return False

    if len(body) < 10_000:
        logger.error("Downloaded file too small (%d bytes) — probably not the real EXE", len(body))
        return False

    # Basic PE check (MZ header)
    if body[:2] != b"MZ":
        logger.warning("Downloaded file does not start with MZ header — may not be a Windows PE")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        logger.info("Standalone saved (%d bytes) → %s", len(body), dest)
        return True
    except OSError as e:
        logger.error("Could not write standalone to %s: %s", dest, e)
        return False


def ensure_standalone(repo_root: Path) -> Path | None:
    """Find or download the standalone EXE."""
    existing = find_standalone_exe(repo_root)
    if existing:
        logger.info("Standalone found: %s", existing)
        return existing

    auto_dl = (os.environ.get("BOT_STANDALONE_AUTO_DOWNLOAD") or "false").strip().lower()
    if auto_dl not in ("1", "true", "yes", "on"):
        logger.info(
            "Standalone not found and auto-download disabled.  "
            "Set BOT_STANDALONE_AUTO_DOWNLOAD=true or place Transformice.exe in the repo root."
        )
        return None

    dest = repo_root / "tmp" / "Transformice.exe"
    if download_standalone(dest):
        return dest
    return None


def describe_standalone_setup(repo_root: Path) -> dict:
    """Gather diagnostic info about standalone availability."""
    exe = find_standalone_exe(repo_root)
    info: dict = {
        "standalone_found": exe is not None,
        "standalone_path": str(exe) if exe else None,
        "download_url": OFFICIAL_DOWNLOAD_URL,
        "auto_download": (
            os.environ.get("BOT_STANDALONE_AUTO_DOWNLOAD") or "false"
        ).strip().lower() in ("1", "true", "yes", "on"),
    }
    if exe and exe.is_file():
        info["file_size"] = exe.stat().st_size
    return info
