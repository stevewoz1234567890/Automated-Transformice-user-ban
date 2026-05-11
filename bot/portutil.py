"""TCP port helpers so multiple bot instances can bind without conflicts."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def tcp_port_is_free(port: int, host: str = "") -> bool:
    """True if we can bind to ``host:port`` (empty host = all interfaces, same as asyncio server)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _local_endpoint_port(local_addr: str) -> str | None:
    """Extract port from netstat local address (e.g. ``0.0.0.0:11801``, ``[::]:11801``)."""
    if ":" not in local_addr:
        return None
    return local_addr.rsplit(":", 1)[-1]


def pids_listening_on_tcp_port(port: int) -> list[int]:
    """PIDs with a TCP listening socket on ``port`` (best-effort; platform-specific)."""
    if sys.platform == "win32":
        return _pids_listening_on_tcp_port_windows(port)
    if sys.platform.startswith("linux"):
        return _pids_listening_on_tcp_port_linux(port)
    return []


def _pids_listening_on_tcp_port_windows(port: int) -> list[int]:
    cp = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        r = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=cp,
        )
    except OSError:
        return []
    want = str(port)
    pids: set[int] = set()
    for line in r.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        ep = _local_endpoint_port(local)
        if ep != want:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return list(pids)


def _pids_listening_on_tcp_port_linux(port: int) -> list[int]:
    try:
        r = subprocess.run(
            ["lsof", "-ti", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError:
        return []
    pids: set[int] = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return list(pids)


def process_executable_path(pid: int) -> str | None:
    """Filesystem path of the main executable for ``pid``, or None."""
    if sys.platform == "win32":
        return _process_executable_path_windows(pid)
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    return None


def _process_executable_path_windows(pid: int) -> str | None:
    cp = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=cp,
        )
    except OSError:
        return None
    path = (r.stdout or "").strip()
    return path or None


def _paths_same_executable(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except OSError:
        return a.resolve().as_posix().lower() == b.resolve().as_posix().lower()


def try_kill_same_bot_on_port(port: int, *, this_exe: Path) -> bool:
    """
    If ``port`` is held only by process(es) running the same executable as ``this_exe``,
    terminate them so this instance can bind. Returns True if any such process was killed.
    """
    pids = pids_listening_on_tcp_port(port)
    if not pids:
        return False
    targets: list[int] = []
    for pid in pids:
        if pid == os.getpid():
            continue
        other = process_executable_path(pid)
        if not other:
            continue
        if _paths_same_executable(Path(other), this_exe):
            targets.append(pid)
    if not targets:
        return False
    killed = False
    for pid in targets:
        try:
            if sys.platform == "win32":
                cp = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=30,
                    creationflags=cp,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            killed = True
            logger.info("Stopped previous bot instance (pid %s) using port %s.", pid, port)
        except OSError as e:
            logger.warning("Could not stop pid %s on port %s: %s", pid, port, e)
    if killed:
        time.sleep(0.5)
    return killed


def try_kill_all_listeners_on_port(port: int) -> bool:
    """
    Terminate every process listening on TCP ``port`` (except the current pid).
    Use after same-exe kill when the port is still busy (e.g. path lookup failed).
    Returns True if any process was terminated.
    """
    pids = [p for p in pids_listening_on_tcp_port(port) if p != os.getpid()]
    if not pids:
        return False
    killed = False
    for pid in pids:
        exe = process_executable_path(pid)
        label = exe or f"pid={pid}"
        try:
            if sys.platform == "win32":
                cp = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=30,
                    creationflags=cp,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            killed = True
            logger.info("Stopped process using port %s: %s (%s)", port, pid, label)
        except OSError as e:
            logger.warning("Could not stop pid %s on port %s: %s", pid, port, e)
    if killed:
        time.sleep(0.5)
    return killed


def ensure_port_free_or_kill_same_bot(
    port: int,
    *,
    this_exe: Path,
    allow_kill: bool = True,
) -> bool:
    """
    Return True if ``port`` is free after optional kills. First tries processes running the
    same executable as this bot; if the port is still busy, terminates any remaining listener
    on that port (so the proxy can bind).
    """
    if tcp_port_is_free(port):
        return True
    if not allow_kill:
        return False
    try_kill_same_bot_on_port(port, this_exe=this_exe)
    if tcp_port_is_free(port):
        return True
    try_kill_all_listeners_on_port(port)
    return tcp_port_is_free(port)
