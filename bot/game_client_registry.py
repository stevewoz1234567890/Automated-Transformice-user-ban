"""
Registry of known Transformice game client access methods.

The game can be reached through several paths — each uses the same server
protocol but loads the SWF (or native binary) differently:

1. **Flash standalone (Adobe AIR / projector)** — the approach the bot uses
   today.  Requires a Flash projector EXE and a proxy-loader SWF.
2. **Official SWF URLs** — two known loader endpoints served by
   transformice.com (ChargeurTransformice / TransformiceChargeur).  Only
   usable with a Flash Player or Ruffle.
3. **Standalone EXE** — official ``Transformice.exe`` (Adobe AIR wrapper)
   from ``transformice.com/Transformice.exe``.
4. **Steam client** — free on Steam; an Electron + embedded Chromium shell
   that loads the same SWF internally.  Automation via remote-debugging-port.
5. **Ruffle (browser)** — community Rust-based Flash emulator; needs a
   websockify TCP-to-WS bridge.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class ClientKind(enum.Enum):
    FLASH_PROJECTOR = "flash_projector"
    OFFICIAL_SWF_LOADER = "official_swf_loader"
    STANDALONE_EXE = "standalone_exe"
    STEAM = "steam"
    RUFFLE = "ruffle"


@dataclass(frozen=True)
class SwfEndpoint:
    """A remote SWF URL that can be loaded to start the game."""

    url: str
    label: str
    notes: str = ""


@dataclass(frozen=True)
class ClientMethod:
    kind: ClientKind
    label: str
    description: str
    available: bool | None = None  # None = not yet probed
    swf_endpoints: tuple[SwfEndpoint, ...] = ()
    extra: dict = field(default_factory=dict)


KNOWN_SWF_ENDPOINTS: tuple[SwfEndpoint, ...] = (
    SwfEndpoint(
        url="http://www.transformice.com/TransformiceChargeur.swf",
        label="TransformiceChargeur",
        notes="Primary loader — historically the default browser embed URL.",
    ),
    SwfEndpoint(
        url="http://www.transformice.com/ChargeurTransformice.swf",
        label="ChargeurTransformice",
        notes="Alternate loader — same game, different SWF filename.",
    ),
    SwfEndpoint(
        url="http://www.transformice.com/Transformice.swf",
        label="Transformice.swf",
        notes="Direct game SWF (may redirect or 404 depending on era).",
    ),
)

STANDALONE_EXE_URL = "http://www.transformice.com/Transformice.exe"

STEAM_APP_ID = "659960"
STEAM_BETA_BRANCH_ELECTRON = "electron"

TFMPROXYLOADER_GITHUB = "friedkeenan/tfm-proxy-loader"
RUFFLE_PROJECT_URL = "https://github.com/ruffle-rs/ruffle"
TFM_BROWSER_PROJECT_URL = "https://github.com/extremq/tfm-browser"


def _steam_install_path() -> Path | None:
    """Try common Steam library locations for the Transformice app."""
    if os.name != "nt":
        candidates = [
            Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Transformice",
            Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Transformice",
        ]
    else:
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Steam" / "steamapps" / "common" / "Transformice",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Steam" / "steamapps" / "common" / "Transformice",
        ]
    env = (os.environ.get("TFM_STEAM_DIR") or "").strip()
    if env:
        candidates.insert(0, Path(env).expanduser())
    for p in candidates:
        if p.is_dir():
            return p
    return None


def _standalone_exe_local_path(repo_root: Path) -> Path | None:
    """Check for a local ``Transformice.exe`` in the repo root or ``tmp/``."""
    for cand in (repo_root / "Transformice.exe", repo_root / "tmp" / "Transformice.exe"):
        if cand.is_file():
            return cand
    return None


def _ruffle_binary() -> Path | None:
    """Look for a ``ruffle`` binary on PATH or common install locations."""
    import shutil

    found = shutil.which("ruffle")
    if found:
        return Path(found)
    home_ruffle = Path.home() / ".ruffle" / "ruffle"
    if home_ruffle.is_file():
        return home_ruffle
    if os.name == "nt":
        for d in (Path.home() / "ruffle", Path(r"C:\ruffle")):
            exe = d / "ruffle.exe"
            if exe.is_file():
                return exe
    return None


def discover_available_methods(repo_root: Path) -> list[ClientMethod]:
    """Return a list of every known access method with ``available`` pre-filled where detectable locally."""
    from .flash_launch import resolve_flash_paths

    methods: list[ClientMethod] = []

    # 1. Flash projector (current approach)
    flash_exe, proxy_swf = resolve_flash_paths(repo_root)
    flash_ok = flash_exe.is_file() and proxy_swf.is_file()
    methods.append(
        ClientMethod(
            kind=ClientKind.FLASH_PROJECTOR,
            label="Flash projector + TFMProxyLoader",
            description=(
                f"Adobe Flash standalone ({flash_exe.name}) loading "
                f"the proxy-loader SWF ({proxy_swf.name}).  "
                "This is the bot's current default path."
            ),
            available=flash_ok,
            extra={
                "flash_exe": str(flash_exe),
                "proxy_swf": str(proxy_swf),
                "flash_exe_exists": flash_exe.is_file(),
                "proxy_swf_exists": proxy_swf.is_file(),
            },
        )
    )

    # 2. Official SWF endpoints (need HTTP probe — marked None for now)
    methods.append(
        ClientMethod(
            kind=ClientKind.OFFICIAL_SWF_LOADER,
            label="Official SWF endpoints",
            description=(
                "Load the game from transformice.com SWF URLs.  "
                "Requires a Flash player or Ruffle to consume the SWF."
            ),
            available=None,
            swf_endpoints=KNOWN_SWF_ENDPOINTS,
        )
    )

    # 3. Standalone EXE
    local_exe = _standalone_exe_local_path(repo_root)
    methods.append(
        ClientMethod(
            kind=ClientKind.STANDALONE_EXE,
            label="Standalone Transformice.exe (Adobe AIR)",
            description=(
                "Official ``Transformice.exe`` — an Adobe AIR wrapper.  "
                f"Download from {STANDALONE_EXE_URL}. "
                "Bypasses the browser but still uses Flash internally."
            ),
            available=local_exe is not None,
            extra={
                "local_path": str(local_exe) if local_exe else None,
                "download_url": STANDALONE_EXE_URL,
            },
        )
    )

    # 4. Steam
    steam_dir = _steam_install_path()
    methods.append(
        ClientMethod(
            kind=ClientKind.STEAM,
            label="Steam client (Electron / AIR)",
            description=(
                f"Free on Steam (app {STEAM_APP_ID}).  "
                f"The default branch is Adobe AIR; the '{STEAM_BETA_BRANCH_ELECTRON}' "
                "beta branch ships an Electron build.  "
                "Automation possible via --remote-debugging-port on the Electron build."
            ),
            available=steam_dir is not None,
            extra={
                "steam_dir": str(steam_dir) if steam_dir else None,
                "app_id": STEAM_APP_ID,
                "electron_beta": STEAM_BETA_BRANCH_ELECTRON,
            },
        )
    )

    # 5. Ruffle
    ruffle_bin = _ruffle_binary()
    methods.append(
        ClientMethod(
            kind=ClientKind.RUFFLE,
            label="Ruffle (Rust Flash emulator)",
            description=(
                "Community Flash replacement written in Rust.  "
                "Can load SWFs natively but needs a websockify bridge for TCP sockets.  "
                f"See {TFM_BROWSER_PROJECT_URL} for a browser-based setup."
            ),
            available=ruffle_bin is not None,
            extra={
                "ruffle_binary": str(ruffle_bin) if ruffle_bin else None,
                "project_url": RUFFLE_PROJECT_URL,
                "tfm_browser_url": TFM_BROWSER_PROJECT_URL,
            },
        )
    )

    return methods


def log_discovered_methods(methods: Sequence[ClientMethod]) -> None:
    """Pretty-print the discovery results to the logger."""
    logger.info("=== Transformice game-client access methods ===")
    for m in methods:
        tag = {True: "YES", False: "NO", None: "UNKNOWN"}[m.available]
        logger.info("  [%s] %s — %s", tag, m.label, m.kind.value)
        if m.swf_endpoints:
            for ep in m.swf_endpoints:
                logger.info("        SWF: %s (%s)", ep.url, ep.label)
        for k, v in m.extra.items():
            if v is not None:
                logger.info("        %s = %s", k, v)
    logger.info("===============================================")
