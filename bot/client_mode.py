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


def preflight_client_mode(repo_root: Path, kind: ClientKind) -> bool:
    """
    Check that the chosen client mode has its prerequisites met.
    Returns True if ready, False with logged errors if not.
    """
    if kind == ClientKind.FLASH_PROJECTOR:
        from .flash_launch import resolve_flash_paths
        flash_exe, proxy_swf = resolve_flash_paths(repo_root)
        ok = True
        if not flash_exe.is_file():
            logger.error(
                "[preflight] Flash projector not found: %s  "
                "Place flashplayer_32_sa_debug.exe in the repo root or set FLASH_PLAYER_EXE.",
                flash_exe,
            )
            ok = False
        if not proxy_swf.is_file():
            logger.error(
                "[preflight] Proxy loader SWF not found: %s  "
                "Set TFM_PROXY_SWF or ensure TFMProxyLoader.swf is fetched at startup.",
                proxy_swf,
            )
            ok = False
        return ok

    if kind == ClientKind.STANDALONE_EXE:
        from .standalone_client import ensure_standalone
        exe = ensure_standalone(repo_root)
        if exe is None:
            logger.error(
                "[preflight] Standalone EXE not found.  Place Transformice.exe in the "
                "repo root, set BOT_STANDALONE_EXE_PATH, or enable BOT_STANDALONE_AUTO_DOWNLOAD."
            )
            return False
        logger.info("[preflight] Standalone EXE ready: %s", exe)
        return True

    if kind == ClientKind.STEAM:
        from .steam_client import find_steam_tfm_dir, detect_steam_branch, find_steam_executable
        game_dir = find_steam_tfm_dir()
        if game_dir is None:
            logger.error(
                "[preflight] Steam Transformice directory not found.  "
                "Install from Steam (app 659960) or set BOT_STEAM_GAME_DIR."
            )
            return False
        branch = detect_steam_branch(game_dir)
        exe = find_steam_executable(game_dir)
        logger.info("[preflight] Steam client ready: dir=%s branch=%s exe=%s", game_dir, branch, exe)
        return True

    if kind == ClientKind.RUFFLE:
        from .ruffle_client import find_ruffle_binary
        binary = find_ruffle_binary()
        if binary is None:
            logger.error(
                "[preflight] Ruffle binary not found.  Install from https://ruffle.rs/downloads "
                "or set BOT_RUFFLE_BINARY_PATH."
            )
            return False
        logger.info("[preflight] Ruffle binary ready: %s", binary)
        return True

    logger.error("[preflight] Unsupported client kind: %s", kind)
    return False


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
