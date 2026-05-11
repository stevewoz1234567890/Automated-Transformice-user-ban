"""
Ruffle-based game client for Transformice.

Ruffle is a Rust-built Flash Player emulator that can load SWF files
natively in a desktop window or in a browser via WASM.

**Key limitation**: Ruffle's ActionScript 3 support is incomplete as of
2025–2026.  Transformice is a complex AS3 game, so Ruffle may not render
all features correctly or may crash on certain frames.

**Networking**: browsers cannot open raw TCP sockets, so the community
project ``tfm-browser`` uses a **websockify** bridge (TCP → WebSocket)
plus a resource proxy.  The desktop Ruffle player has experimental raw
socket support but may still need the bridge depending on version.

This module provides helpers to:
  - Locate or download a Ruffle binary.
  - Start Ruffle with a given SWF (official endpoint or proxy loader).
  - Optionally start a websockify bridge for socket support.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_RUFFLE_RELEASE_API = "https://api.github.com/repos/ruffle-rs/ruffle/releases/latest"
_WEBSOCKIFY_DEFAULT_PORT = 8080


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


def launch_ruffle_with_swf(
    swf_path_or_url: str,
    *,
    ruffle_binary: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen | None:
    """
    Launch the Ruffle desktop player with a SWF file or URL.

    ``swf_path_or_url`` can be a local file path or an HTTP URL.
    """
    binary = ruffle_binary or find_ruffle_binary()
    if binary is None:
        logger.error(
            "Ruffle binary not found.  Install Ruffle desktop from "
            "https://ruffle.rs/downloads or set BOT_RUFFLE_BINARY_PATH."
        )
        return None

    cmd = [str(binary), swf_path_or_url]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("Launching Ruffle: %s", " ".join(cmd))
    try:
        return subprocess.Popen(cmd)
    except OSError as e:
        logger.error("Ruffle launch failed: %s", e)
        return None


def find_websockify() -> Path | None:
    """Locate ``websockify`` on PATH (Python package or standalone)."""
    found = shutil.which("websockify")
    if found:
        return Path(found)
    return None


def start_websockify_bridge(
    *,
    listen_port: int | None = None,
    target_host: str = "51.38.60.113",
    target_port: int = 11801,
) -> subprocess.Popen | None:
    """
    Start a websockify bridge: WebSocket on ``listen_port`` → TCP to game server.

    Browsers (and some Ruffle builds) use WebSocket instead of raw TCP.
    ``websockify`` bridges the two protocols.

    Install: ``pip install websockify`` or use the standalone binary.
    """
    ws_bin = find_websockify()
    if ws_bin is None:
        logger.error(
            "websockify not found.  Install with: pip install websockify  "
            "or download from https://github.com/novnc/websockify"
        )
        return None

    port = listen_port or int(
        os.environ.get("BOT_RUFFLE_WEBSOCKIFY_PORT") or _WEBSOCKIFY_DEFAULT_PORT
    )

    cmd = [
        str(ws_bin),
        f"0.0.0.0:{port}",
        f"{target_host}:{target_port}",
    ]

    logger.info("Starting websockify bridge: ws://0.0.0.0:%d → %s:%d", port, target_host, target_port)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1.0)
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
            logger.error("websockify exited immediately: %s", err)
            return None
        logger.info("websockify bridge running (pid %d)", proc.pid)
        return proc
    except OSError as e:
        logger.error("websockify launch failed: %s", e)
        return None


def download_official_swf(
    url: str,
    dest: Path,
    *,
    timeout: float = 30.0,
) -> bool:
    """Download an official SWF endpoint to a local file."""
    req = urllib.request.Request(url, headers={"User-Agent": "TFM-RuffleClient/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        logger.error("SWF download failed from %s: %s", url, e)
        return False

    if len(body) < 1000 or body[:3] not in (b"FWS", b"CWS", b"ZWS"):
        logger.error("Response from %s is not a valid SWF (%d bytes, sig=%r)", url, len(body), body[:3])
        return False

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        logger.info("Downloaded SWF (%d bytes) → %s", len(body), dest)
        return True
    except OSError as e:
        logger.error("Could not write SWF to %s: %s", dest, e)
        return False


def describe_ruffle_setup() -> dict:
    """Gather diagnostic info about Ruffle availability."""
    binary = find_ruffle_binary()
    info: dict = {
        "ruffle_found": binary is not None,
        "ruffle_path": str(binary) if binary else None,
        "ruffle_version": ruffle_version(binary) if binary else None,
        "websockify_found": find_websockify() is not None,
        "websockify_path": str(find_websockify()) if find_websockify() else None,
    }
    return info
