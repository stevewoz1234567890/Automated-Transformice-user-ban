"""
Resolve which game-client mode to use and wire up the launch path.

``BOT_GAME_CLIENT_MODE`` selects the client:

  - ``flash_projector`` (default) — existing Flash + TFMProxyLoader path.
  - ``standalone_exe``            — official ``Transformice.exe`` (AIR).
  - ``steam``                     — launch via Steam (AIR or Electron).
  - ``ruffle``                    — Ruffle desktop + optional websockify.

Each mode still connects through the local ``BanBotProxy`` — only the
**game front-end** changes.  The proxy, headless login, and ban logic
stay the same.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .game_client_registry import ClientKind

logger = logging.getLogger(__name__)


def resolve_client_mode() -> ClientKind:
    """Read ``BOT_GAME_CLIENT_MODE`` and return the corresponding enum."""
    raw = (os.environ.get("BOT_GAME_CLIENT_MODE") or "flash_projector").strip().lower()
    mapping = {
        "flash_projector": ClientKind.FLASH_PROJECTOR,
        "flash": ClientKind.FLASH_PROJECTOR,
        "standalone_exe": ClientKind.STANDALONE_EXE,
        "standalone": ClientKind.STANDALONE_EXE,
        "steam": ClientKind.STEAM,
        "ruffle": ClientKind.RUFFLE,
    }
    kind = mapping.get(raw)
    if kind is None:
        logger.warning(
            "Unknown BOT_GAME_CLIENT_MODE=%r — falling back to flash_projector.  "
            "Valid modes: %s",
            raw,
            ", ".join(sorted(mapping.keys())),
        )
        return ClientKind.FLASH_PROJECTOR
    logger.info("Game client mode: %s (BOT_GAME_CLIENT_MODE=%r)", kind.value, raw)
    return kind


def maybe_run_startup_probe(repo_root: Path) -> None:
    """If ``BOT_PROBE_GAME_CLIENTS_AT_STARTUP`` is truthy, run the full probe."""
    raw = (os.environ.get("BOT_PROBE_GAME_CLIENTS_AT_STARTUP") or "false").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return
    logger.info("Running game-client probe at startup (BOT_PROBE_GAME_CLIENTS_AT_STARTUP=true)...")
    try:
        from .probe_clients_cli import run_probe
        run_probe(repo_root)
    except Exception:
        logger.exception("Startup client probe failed (non-fatal)")
