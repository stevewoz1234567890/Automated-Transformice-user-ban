"""Obtain ``caseus.Secrets`` via TFMSecretsLeaker.swf and the Flash debug projector."""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_LEAKER_URL = (
    "https://github.com/friedkeenan/tfm-secrets-leaker/releases/download/v1.5.32/"
    "TFMSecretsLeaker.swf"
)


def leaker_swf_path(root: Path) -> Path:
    return root / "tmp" / "TFMSecretsLeaker.swf"


def resolve_flash_debugger(root: Path) -> Path | None:
    env = (os.environ.get("FLASHPLAYER_DEBUG") or os.environ.get("FLASH_DEBUG_STANDALONE") or "").strip()
    if env:
        pe = Path(env).expanduser()
        if pe.is_file():
            return pe
    cand = root / "flashplayer_32_sa_debug.exe"
    if cand.is_file():
        return cand
    return None


def ensure_leaker_swf(root: Path) -> Path | None:
    path = leaker_swf_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            logger.info("Downloading TFMSecretsLeaker.swf to %s", path)
            urllib.request.urlretrieve(_LEAKER_URL, path)
        return path
    except OSError as e:
        logger.warning("Could not cache leaker SWF: %s", e)
        return None


def try_load_secrets_via_leaker(root: Path):
    """Return ``Secrets`` from the leaker SWF, or ``None`` if Flash / download is unavailable."""
    from caseus import Secrets

    flash = resolve_flash_debugger(root)
    if flash is None:
        logger.debug(
            "No Flash debug projector: place flashplayer_32_sa_debug.exe in %s or set FLASHPLAYER_DEBUG.",
            root,
        )
        return None
    leaker = ensure_leaker_swf(root)
    if leaker is None:
        return None
    try:
        logger.info("Running TFMSecretsLeaker.swf with %s", flash)
        return Secrets.load_from_leaker_swf(leaker, debug_standalone=str(flash))
    except Exception as e:
        logger.warning("caseus.load_from_leaker_swf failed: %s", e)
        return None
