"""
Build ``file:///…TFMProxyLoader.swf?…`` URLs for ``LoginPacket.loader_url`` (packet login).

In headless/manual mode the bot does not start Flash Player — an external client must open the MAIN
TCP connection to each proxy port.  In **UI mode** (``--ui`` / ``BOT_UI_AUTO_LAUNCH_FLASH``), the
bot resolves a Flash standalone projector and launches one window per slot so the user can see the
game UI while the proxy handles login automatically.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import tfm_swf_port_patch

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_loader_swf(root: Path | None = None) -> Path:
    root = root or _repo_root()
    env_swf = (os.environ.get("TFM_PROXY_SWF") or "").strip()
    return Path(env_swf) if env_swf else root / "TFMProxyLoader.swf"


def _loader_document_url(
    swf: Path,
    *,
    main_port: int,
    satellite_port: int,
    policy_port: int | None,
    connect_host: str,
) -> str:
    """Query string becomes ``loaderInfo.parameters`` (forks can read host / port / satellite / policy)."""
    uri = swf.resolve().as_uri()
    h = connect_host.strip() or "127.0.0.1"
    q = [
        f"host={h}",
        f"proxyHost={h}",
        f"port={int(main_port)}",
        f"satellite={int(satellite_port)}",
    ]
    if policy_port is not None:
        pp = int(policy_port)
        q.append(f"policy_port={pp}")
        q.append(f"policyPort={pp}")
    return uri + "?" + "&".join(q)


AccountRow = dict[str, object]


def loader_document_url_for_row(row: AccountRow, root: Path | None = None) -> str | None:
    """
    ``file:///...swf?host=...`` document URL for ``LoginPacket.loader_url``.
    Returns ``None`` only if the SWF path is missing.
    """
    root = root or _repo_root()
    swf = _resolve_loader_swf(root)
    if not swf.is_file():
        return None

    port = int(row["proxy_port"])
    sat_raw = row.get("_flash_satellite_port")
    satellite = int(sat_raw) if sat_raw is not None else port + 10_000
    pol_raw = row.get("_flash_policy_port")
    policy = int(pol_raw) if pol_raw is not None else None
    ch_raw = row.get("_flash_connect_host")
    connect_host = str(ch_raw).strip() if ch_raw is not None else "127.0.0.1"
    patch_host = tfm_swf_port_patch.nine_char_connect_host(connect_host)

    swf_arg = swf
    no_patch = (os.environ.get("TFM_NO_SWF_PATCH") or "").strip().lower() in ("1", "true", "yes")
    if not no_patch:
        try:
            head = swf.read_bytes()[:3]
            if head == b"ZWS":
                cache_dir = root / "tmp" / "loader_patch"
                swf_arg = tfm_swf_port_patch.build_patched_loader_swf(
                    swf,
                    port=port,
                    connect_host=connect_host,
                    cache_dir=cache_dir,
                )
        except Exception:
            swf_arg = swf

    return _loader_document_url(
        swf_arg,
        main_port=port,
        satellite_port=satellite,
        policy_port=policy,
        connect_host=patch_host,
    )


# ---------------------------------------------------------------------------
# UI mode: resolve and launch Flash standalone player
# ---------------------------------------------------------------------------

#: Candidate Flash standalone player filenames, checked in order in the repo root.
_FLASH_SA_CANDIDATES = [
    # Non-debug standalone (preferred for UI — no extra console noise)
    "flashplayer_32_sa.exe",
    "flashplayer_sa.exe",
    "flash_player_sa.exe",
    "FlashPlayer.exe",
    "flashplayer.exe",
    # Debug standalone (also works for UI, just prints trace output)
    "flashplayer_32_sa_debug.exe",
    "flashplayer_sa_debug.exe",
    # Linux / macOS equivalents
    "flashplayer_32_sa",
    "flashplayer_sa",
    "flashplayer",
    "flash_player_sa",
    "FlashPlayer",
]


def resolve_flash_player(root: Path | None = None) -> Path | None:
    """
    Locate a Flash standalone projector for UI mode.

    Search order:
    1. ``BOT_UI_FLASH_PLAYER_PATH`` env var (explicit path set by the user).
    2. Generic ``FLASHPLAYER`` / ``FLASH_STANDALONE`` env vars.
    3. Debug projector env vars (``FLASHPLAYER_DEBUG`` / ``FLASH_DEBUG_STANDALONE``).
    4. Well-known filenames in the repo root (non-debug preferred over debug).

    Returns ``None`` when no executable is found.
    """
    root = root or _repo_root()

    for env_key in (
        "BOT_UI_FLASH_PLAYER_PATH",
        "FLASHPLAYER",
        "FLASH_STANDALONE",
        "FLASHPLAYER_DEBUG",
        "FLASH_DEBUG_STANDALONE",
    ):
        env = (os.environ.get(env_key) or "").strip()
        if env:
            pe = Path(env).expanduser()
            if pe.is_file():
                return pe
            logger.debug("resolve_flash_player: %s=%r not found as file, skipping.", env_key, env)

    for name in _FLASH_SA_CANDIDATES:
        cand = root / name
        if cand.is_file():
            return cand

    return None


def launch_flash_player_for_slot(
    loader_url: str,
    flash_exe: Path,
    *,
    label: str = "",
) -> "subprocess.Popen[bytes] | None":
    """
    Launch a Flash standalone projector window for one bot slot.

    The projector receives the ``file:///…TFMProxyLoader.swf?…`` URL so that it connects
    to the local proxy and the proxy injects the login packet automatically.

    Returns the ``Popen`` handle (the caller should keep it alive or poll it), or
    ``None`` when the launch fails.
    """
    slot_tag = f" (slot {label})" if label else ""
    logger.info("UI-mode: launching Flash Player%s  →  %s", slot_tag, loader_url)
    try:
        proc = subprocess.Popen(
            [str(flash_exe), loader_url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("UI-mode: Flash Player%s started (pid %s).", slot_tag, proc.pid)
        return proc
    except OSError as exc:
        logger.error(
            "UI-mode: failed to launch Flash Player%s (%s): %s",
            slot_tag,
            flash_exe,
            exc,
        )
        return None


def launch_flash_players_for_slots(
    slot_infos: "list[tuple[str, str]]",
    flash_exe: Path,
    *,
    stagger_sec: float = 1.0,
) -> "list[subprocess.Popen[bytes]]":
    """
    Launch one Flash standalone window per slot and return all live ``Popen`` handles.

    ``slot_infos`` is a list of ``(label, loader_url)`` pairs.  Slots whose
    ``loader_url`` is empty are skipped with a warning.

    ``stagger_sec`` adds a pause between consecutive launches to avoid Flash
    windows all hammering the proxy at the exact same moment.
    """
    procs: list[subprocess.Popen] = []
    for i, (label, loader_url) in enumerate(slot_infos):
        if not loader_url:
            logger.warning(
                "UI-mode: slot %s has no loader URL (TFMProxyLoader.swf missing or TFM_PROXY_SWF "
                "not set). Skipping Flash launch for this slot.",
                label,
            )
            continue
        proc = launch_flash_player_for_slot(loader_url, flash_exe, label=label)
        if proc is not None:
            procs.append(proc)
        if stagger_sec > 0 and i < len(slot_infos) - 1:
            time.sleep(stagger_sec)
    return procs
