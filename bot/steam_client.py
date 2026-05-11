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
import shutil
import subprocess
import time
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


def find_swf_in_steam_dir(game_dir: Path) -> list[Path]:
    """List all SWF files bundled with the Steam install."""
    return sorted(game_dir.rglob("*.swf"))


def inject_proxy_loader_into_steam(
    game_dir: Path,
    proxy_loader_swf: Path,
    *,
    backup: bool = True,
) -> bool:
    """
    Replace the game's ``Transformice.swf`` with ``TFMProxyLoader.swf`` so all
    traffic routes through the local proxy.

    This is the same technique used by ``friedkeenan/tfm-proxy-loader``:
    the Steam client loads whatever SWF it finds at its expected path.
    """
    target = game_dir / "Transformice.swf"
    if not proxy_loader_swf.is_file():
        logger.error("Proxy loader SWF not found: %s", proxy_loader_swf)
        return False

    if backup and target.is_file():
        bak = target.with_suffix(".swf.bak")
        if not bak.is_file():
            try:
                shutil.copy2(target, bak)
                logger.info("Backed up original SWF → %s", bak)
            except OSError as e:
                logger.warning("Could not backup %s: %s", target, e)

    try:
        shutil.copy2(proxy_loader_swf, target)
        logger.info("Injected proxy loader into Steam dir: %s → %s", proxy_loader_swf.name, target)
        return True
    except OSError as e:
        logger.error("Failed to inject proxy loader: %s", e)
        return False


def restore_original_swf(game_dir: Path) -> bool:
    """Restore the original ``Transformice.swf`` from backup."""
    target = game_dir / "Transformice.swf"
    bak = target.with_suffix(".swf.bak")
    if not bak.is_file():
        logger.warning("No backup found at %s", bak)
        return False
    try:
        shutil.copy2(bak, target)
        logger.info("Restored original SWF from backup: %s", target)
        return True
    except OSError as e:
        logger.error("Failed to restore SWF: %s", e)
        return False


def launch_steam_game(
    game_dir: Path | None = None,
    *,
    use_steam_protocol: bool = True,
    electron_debug_port: int | None = None,
) -> subprocess.Popen | None:
    """
    Launch Transformice through Steam or directly.

    ``use_steam_protocol=True`` uses ``steam://rungameid/659960`` which
    respects Steam's overlay and account linking.

    For Electron builds, ``electron_debug_port`` enables Chrome DevTools
    Protocol automation.
    """
    if use_steam_protocol:
        steam_exe = shutil.which("steam")
        if os.name == "nt":
            for candidate in (
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Steam" / "steam.exe",
                Path(os.environ.get("ProgramFiles", "")) / "Steam" / "steam.exe",
            ):
                if candidate.is_file():
                    steam_exe = str(candidate)
                    break

        if not steam_exe:
            logger.error("Steam executable not found")
            return None

        cmd = [steam_exe, "-applaunch", "659960"]
        if electron_debug_port:
            cmd.extend(["--", f"--remote-debugging-port={electron_debug_port}"])

        logger.info("Launching via Steam: %s", " ".join(cmd))
        try:
            return subprocess.Popen(cmd)
        except OSError as e:
            logger.error("Steam launch failed: %s", e)
            return None

    # Direct launch (no Steam overlay)
    if game_dir is None:
        game_dir = find_steam_tfm_dir()
    if game_dir is None:
        logger.error("Cannot find Transformice Steam directory")
        return None

    exe = find_steam_executable(game_dir)
    if exe is None:
        logger.error("No Transformice executable in %s", game_dir)
        return None

    cmd = [str(exe)]
    if electron_debug_port:
        cmd.append(f"--remote-debugging-port={electron_debug_port}")

    logger.info("Direct launch: %s", " ".join(cmd))
    try:
        return subprocess.Popen(cmd, cwd=str(game_dir))
    except OSError as e:
        logger.error("Direct launch failed: %s", e)
        return None


def wait_for_electron_debug_ready(port: int, timeout: float = 30.0) -> bool:
    """Poll the Chrome DevTools Protocol endpoint until it responds."""
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                data = json.loads(resp.read())
                logger.info(
                    "Electron debug ready: browser=%s protocol=%s",
                    data.get("Browser", "?"),
                    data.get("Protocol-Version", "?"),
                )
                return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(1.0)
    logger.warning("Electron debug port %d did not respond within %ss", port, timeout)
    return False


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

    swfs = find_swf_in_steam_dir(game_dir)
    info["swf_files"] = [str(s) for s in swfs]
    info["has_backup"] = (game_dir / "Transformice.swf.bak").is_file()

    if (game_dir / "package.json").is_file():
        try:
            pkg = json.loads((game_dir / "package.json").read_text(encoding="utf-8"))
            info["electron_package_name"] = pkg.get("name", "?")
            info["electron_version"] = pkg.get("version", "?")
        except (OSError, json.JSONDecodeError):
            pass

    return info
