"""
Steam / Electron client integration for Transformice.

The Steam version of Transformice (app 659960) ships in two flavours:

- **Default branch** — Adobe AIR runtime.  Internally loads the same
  ``Transformice.swf`` and connects via raw TCP.  Can be proxied the
  same way as the Flash projector: place ``TFMProxyLoader.swf`` in the
  game directory and use ``tfm-proxy-loader`` to redirect traffic.

- **Electron beta branch** — Chromium shell.  Can be automated through
  ``--remote-debugging-port`` + Selenium / Puppeteer / CDP.

Both branches connect to the same game servers and use the same protocol.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def find_steam_tfm_dir() -> Path | None:
    """Locate the Transformice Steam install directory."""
    env = (os.environ.get("BOT_STEAM_GAME_DIR") or os.environ.get("TFM_STEAM_DIR") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p

    if os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Steam" / "steamapps" / "common" / "Transformice",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Steam" / "steamapps" / "common" / "Transformice",
        ]
    else:
        candidates = [
            Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Transformice",
            Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Transformice",
        ]

    for c in candidates:
        if c.is_dir():
            return c
    return None


def detect_steam_branch(game_dir: Path) -> str:
    """Detect whether the install is the AIR or Electron branch."""
    # Electron builds have package.json + node_modules / resources/app
    if (game_dir / "package.json").is_file():
        return "electron"
    if (game_dir / "resources" / "app").is_dir():
        return "electron"
    # AIR builds have META-INF/AIR or .air files
    if (game_dir / "META-INF").is_dir():
        return "air"
    # Look for characteristic executables
    for f in game_dir.iterdir():
        if f.suffix.lower() == ".exe":
            name_l = f.name.lower()
            if "electron" in name_l or "nw" in name_l:
                return "electron"
    return "air"


def find_steam_executable(game_dir: Path) -> Path | None:
    """Find the main game executable inside the Steam directory."""
    candidates = [
        game_dir / "Transformice.exe",
        game_dir / "transformice.exe",
    ]
    for p in game_dir.iterdir():
        if p.is_file() and p.suffix.lower() == ".exe" and "transformice" in p.name.lower():
            candidates.append(p)

    for c in candidates:
        if c.is_file():
            return c
    return None


def describe_steam_setup(game_dir: Path | None = None) -> dict:
    """Gather diagnostic info about the Steam Transformice installation."""
    if game_dir is None:
        game_dir = find_steam_tfm_dir()

    info: dict = {
        "steam_dir_found": game_dir is not None,
        "steam_dir": str(game_dir) if game_dir else None,
    }

    if game_dir is None:
        return info

    info["branch"] = detect_steam_branch(game_dir)
    exe = find_steam_executable(game_dir)
    info["executable"] = str(exe) if exe else None

    info["swf_files"] = [str(s) for s in sorted(game_dir.rglob("*.swf"))]
    info["has_backup"] = (game_dir / "Transformice.swf.bak").is_file()

    if (game_dir / "package.json").is_file():
        try:
            pkg = json.loads((game_dir / "package.json").read_text(encoding="utf-8"))
            info["electron_package_name"] = pkg.get("name", "?")
            info["electron_version"] = pkg.get("version", "?")
        except (OSError, json.JSONDecodeError):
            pass

    return info
