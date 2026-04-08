"""
Launch one Flash standalone + TFMProxyLoader.swf per account with the matching proxy port.

Patches the bundled **ZWS** ``TFMProxyLoader.swf`` so the hardcoded ``localhost:11801`` becomes
``127.0.0.1:<proxy_port>`` (upstream ignores URL parameters). Caches per-port SWFs under
``tmp/loader_patch/``. Clicks the Transformice entry with a **real** mouse_event (Flash ignores
most Posted WM_* clicks). Optional env: ``FLASH_LOADER_CLICK_FRAC_X``, ``FLASH_LOADER_CLICK_FRAC_Y``
(0–1, default 0.5 / 0.55), ``FLASH_WIN_CLICK_RETRIES`` (default 10; retries when a window opens behind another).

Each account can use a different ``proxy_port`` and local ``bind_ip``: the loader URL includes
``host`` / ``proxyHost`` so the client targets the same address the caseus proxy listens on.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from . import tfm_swf_port_patch

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_flash_paths(root: Path | None = None) -> tuple[Path, Path]:
    root = root or _repo_root()
    env_fp = (os.environ.get("FLASH_PLAYER_EXE") or "").strip()
    env_swf = (os.environ.get("TFM_PROXY_SWF") or "").strip()
    flash = Path(env_fp) if env_fp else root / "flashplayer_32_sa_debug.exe"
    swf = Path(env_swf) if env_swf else root / "TFMProxyLoader.swf"
    return flash, swf


def flash_launch_files_present(root: Path | None = None) -> bool:
    flash, swf = resolve_flash_paths(root)
    return flash.is_file() and swf.is_file()


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


def _win_find_toplevel_hwnd(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER — skip owned popups
            return True
        found.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        return None
    if len(found) == 1:
        return found[0]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    best = found[0]
    best_area = 0
    for hwnd in found:
        rc = RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rc)):
            area = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
            if area > best_area:
                best_area = area
                best = hwnd
    return best


def _win_best_click_hwnd(hwnd: int) -> int:
    """Prefer the largest visible child (Flash stage); else the top-level HWND."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    rc = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return hwnd
    pw = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
    best_hwnd = hwnd
    best_area = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_child(ch, _lp):
        nonlocal best_area, best_hwnd
        if not user32.IsWindowVisible(ch):
            return True
        crc = RECT()
        if not user32.GetClientRect(ch, ctypes.byref(crc)):
            return True
        area = max(0, crc.right - crc.left) * max(0, crc.bottom - crc.top)
        if area > best_area:
            best_area = area
            best_hwnd = int(ch)
        return True

    user32.EnumChildWindows(hwnd, enum_child, 0)
    if best_area >= pw * 0.25 and best_hwnd != hwnd:
        return best_hwnd
    return hwnd


def _win_force_foreground(hwnd: int) -> None:
    """Best-effort focus so the following synthetic mouse events hit this window."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0

    if fg_tid and fg_tid != cur_tid:
        user32.AttachThreadInput(fg_tid, cur_tid, True)
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(fg_tid, cur_tid, False)


def _win_resolve_click_hwnd(
    flash_pid: int | None,
    hwnd: int,
    *,
    resolve_pid: bool = True,
) -> int | None:
    """Prefer a fresh top-level HWND for ``flash_pid``; fall back to ``hwnd``."""
    if resolve_pid and flash_pid is not None:
        h = _win_find_toplevel_hwnd(flash_pid)
        if h is not None:
            return h
    return hwnd if hwnd else None


def _win_click_retries_from_env() -> int:
    raw = (os.environ.get("FLASH_WIN_CLICK_RETRIES") or "").strip()
    if not raw:
        return 10
    try:
        n = int(raw)
    except ValueError:
        return 10
    return max(1, min(n, 40))


def _win_click_client_fraction(
    hwnd: int,
    *,
    frac_x: float,
    frac_y: float,
    double_click: bool = False,
    debug_label: str | None = None,
    flash_pid: int | None = None,
    resolve_pid: bool = True,
) -> bool:
    """
    Real cursor + mouse_event (not PostMessage). Flash Player standalone ignores most
    Posted WM_* to the HWND; it expects user-level input like a normal click.

    Restores/focuses the window *before* measuring the client rect so a 2nd Flash instance
    behind another window does not yield 0-size rects or failed ClientToScreen. Retries when
    ``flash_pid`` is set (or env ``FLASH_WIN_CLICK_RETRIES`` > 1).
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    fx = min(1.0, max(0.0, float(frac_x)))
    fy = min(1.0, max(0.0, float(frac_y)))
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    retries = _win_click_retries_from_env()
    last_fail = "unknown"

    for attempt in range(retries):
        hwnd_cur = _win_resolve_click_hwnd(flash_pid, hwnd, resolve_pid=resolve_pid)
        if hwnd_cur is None:
            last_fail = "no HWND"
            time.sleep(0.1 + attempt * 0.02)
            continue
        _win_force_foreground(hwnd_cur)
        time.sleep(0.08 + attempt * 0.03)
        rc_top = RECT()
        if not user32.GetClientRect(hwnd_cur, ctypes.byref(rc_top)):
            last_fail = "GetClientRect(toplevel) failed"
            time.sleep(0.1)
            continue
        top_area = max(0, rc_top.right - rc_top.left) * max(0, rc_top.bottom - rc_top.top)
        # Small popups (ActionScript errors): use toplevel client rect; largest child is often the text view.
        if top_area < 120_000:
            target = hwnd_cur
        else:
            target = _win_best_click_hwnd(hwnd_cur)
        rc = RECT()
        if not user32.GetClientRect(target, ctypes.byref(rc)):
            last_fail = "GetClientRect failed"
            time.sleep(0.1)
            continue
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w < 8 or h < 8:
            last_fail = f"client too small ({w}x{h})"
            time.sleep(0.1)
            continue
        cx = int(w * fx)
        cy = int(h * fy)
        pt = POINT(cx, cy)
        if not user32.ClientToScreen(target, ctypes.byref(pt)):
            last_fail = "ClientToScreen failed"
            time.sleep(0.1)
            continue

        user32.SetCursorPos(pt.x, pt.y)
        time.sleep(0.06)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if double_click:
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.03)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if debug_label:
            logger.info(
                "FLASH_LOGIN_DEBUG %s: target_hwnd=%s toplevel_hwnd=%s client=(%s,%s) screen=(%s,%s) "
                "frac=(%.4f,%.4f) client_size=%sx%s attempt=%s/%s",
                debug_label,
                target,
                hwnd_cur,
                cx,
                cy,
                pt.x,
                pt.y,
                fx,
                fy,
                w,
                h,
                attempt + 1,
                retries,
            )
        return True

    logger.warning(
        "Client-area click failed after %s attempts: %s (toplevel_hwnd=%s flash_pid=%s resolve_pid=%s)",
        retries,
        last_fail,
        hwnd,
        flash_pid,
        resolve_pid,
    )
    return False


def _loader_click_fractions_from_env() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return (
        _f("FLASH_LOADER_CLICK_FRAC_X", 0.5),
        _f("FLASH_LOADER_CLICK_FRAC_Y", 0.55),
    )


def _wait_hwnd_for_pid(pid: int, *, timeout_sec: float = 20.0, poll_sec: float = 0.25) -> int | None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        h = _win_find_toplevel_hwnd(pid)
        if h is not None:
            return h
        time.sleep(poll_sec)
    return None


AccountRow = dict[str, object]


def loader_document_url_for_row(row: AccountRow, root: Path | None = None) -> str | None:
    """
    Same ``file:///...swf?host=...`` document URL as ``launch_one_flash_loader`` uses (for ``LoginPacket.loader_url``).
    Returns ``None`` only if the SWF path is missing.
    """
    root = root or _repo_root()
    _, swf = resolve_flash_paths(root)
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


def launch_one_flash_loader(
    row: AccountRow,
    *,
    root: Path | None = None,
    post_open_delay_sec: float = 1.15,
    click_transformice: bool = True,
    on_flash_pid: Callable[[int], None] | None = None,
) -> subprocess.Popen | None:
    """
    Start a single Flash projector + loader for one account row.

    Expects ``_flash_satellite_port``, ``_flash_policy_port``, and ``_flash_connect_host`` when
    produced by ban_cli.
    """
    if sys.platform != "win32":
        logger.warning("Automatic Flash launch is only implemented on Windows.")
        return None

    root = root or _repo_root()
    flash, swf = resolve_flash_paths(root)
    if not flash.is_file():
        logger.error("Flash player not found: %s", flash)
        return None
    if not swf.is_file():
        logger.error("TFMProxyLoader.swf not found: %s", swf)
        return None

    label = str(row.get("label", ""))
    port = int(row["proxy_port"])
    bind_ip = str(row.get("bind_ip", "") or "").strip()
    sat_raw = row.get("_flash_satellite_port")
    satellite = int(sat_raw) if sat_raw is not None else port + 10_000
    pol_raw = row.get("_flash_policy_port")
    policy = int(pol_raw) if pol_raw is not None else None
    ch_raw = row.get("_flash_connect_host")
    connect_host = str(ch_raw).strip() if ch_raw is not None else "127.0.0.1"
    patch_host = tfm_swf_port_patch.nine_char_connect_host(connect_host)
    if patch_host != connect_host:
        logger.info(
            "Slot %s: SWF patch uses host %r (not %r); 9-char limit for embedded connection string.",
            label or "?",
            patch_host,
            connect_host,
        )

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
            else:
                logger.warning(
                    "TFMProxyLoader is not ZWS (%r); cannot binary-patch port — URL params only (often ignored).",
                    head,
                )
        except Exception as e:
            logger.error(
                "SWF port patch failed (%s); falling back to unpatched loader (connection will likely fail).",
                e,
            )

    doc = _loader_document_url(
        swf_arg,
        main_port=port,
        satellite_port=satellite,
        policy_port=policy,
        connect_host=patch_host,
    )
    logger.info(
        "Launching Flash [slot %s] swf_patched=%s connect=%s:%s satellite=%s flash_policy=%s account_bind_ip=%r",
        label or "?",
        swf_arg != swf,
        patch_host,
        port,
        satellite,
        policy,
        bind_ip or "(not set)",
    )
    doc_log = doc if len(doc) <= 500 else doc[:500] + "..."
    try:
        p = subprocess.Popen(
            [str(flash), doc],
            cwd=str(root),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as e:
        logger.error("Could not start Flash for slot %s: %s", label, e)
        return None

    if on_flash_pid is not None:
        try:
            on_flash_pid(p.pid)
        except Exception as e:
            logger.warning("on_flash_pid failed for slot %s: %s", label, e)

    logger.info(
        "Flash started slot %s PID=%s exe=%s cwd=%s",
        label or "?",
        p.pid,
        flash,
        root,
    )
    logger.info("Flash SWF argument (truncated): %s", doc_log)

    if click_transformice:
        logger.info("Waiting for Flash window (PID %s, slot %s)...", p.pid, label or "?")
        hwnd = _wait_hwnd_for_pid(p.pid)
        if hwnd is None:
            logger.warning(
                "No HWND for Flash PID %s (slot %s); click Transformice manually in that window.",
                p.pid,
                label,
            )
        else:
            fx, fy = _loader_click_fractions_from_env()
            logger.info(
                "Flash HWND=%s for PID=%s (slot %s); sleeping %ss then real mouse click "
                "at (frac_x=%.2f, frac_y=%.2f) — set FLASH_LOADER_CLICK_FRAC_X/Y to adjust",
                hwnd,
                p.pid,
                label,
                post_open_delay_sec,
                fx,
                fy,
            )
            time.sleep(post_open_delay_sec)
            if _win_click_client_fraction(hwnd, frac_x=fx, frac_y=fy, flash_pid=p.pid):
                logger.info(
                    "Sent mouse click to HWND=%s (slot %s) for Transformice button "
                    "(if MAIN TCP never connects, try frac_y=0.45–0.65 or click manually)",
                    hwnd,
                    label,
                )
            else:
                logger.warning(
                    "Loader click failed for HWND=%s slot %s; click Transformice manually.",
                    hwnd,
                    label,
                )
    else:
        logger.info("Auto-click disabled; click Transformice manually (slot %s PID=%s)", label, p.pid)

    time.sleep(0.45)
    exit_code = p.poll()
    if exit_code is not None:
        logger.error(
            "Flash process exited immediately (slot %s PID=%s exit_code=%s). "
            "Check SWF path, trust cfg, or run flashplayer from a console for errors.",
            label,
            p.pid,
            exit_code,
        )
    else:
        logger.info("Flash process still running after startup (slot %s PID=%s)", label, p.pid)

    return p


def launch_flash_loaders_for_accounts(
    accounts: Sequence[AccountRow],
    *,
    root: Path | None = None,
    stagger_sec: float = 1.25,
    post_open_delay_sec: float = 1.15,
    click_transformice: bool = True,
) -> list[subprocess.Popen]:
    """Launch every account without waiting for login (legacy / tests). Prefer sequential flow in ban_cli."""
    procs: list[subprocess.Popen] = []
    for row in accounts:
        p = launch_one_flash_loader(
            row,
            root=root,
            post_open_delay_sec=post_open_delay_sec,
            click_transformice=click_transformice,
        )
        if p is not None:
            procs.append(p)
        time.sleep(stagger_sec)
    return procs
