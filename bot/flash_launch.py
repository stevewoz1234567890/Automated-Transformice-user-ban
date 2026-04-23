"""
Launch one Flash standalone + TFMProxyLoader.swf per account with the matching proxy port.

Patches the bundled **ZWS** ``TFMProxyLoader.swf`` so the hardcoded ``localhost:11801`` becomes
``127.0.0.1:<proxy_port>`` (upstream ignores URL parameters). Caches per-port SWFs under
``tmp/loader_patch/``. Clicks the Transformice entry with a **real** mouse_event (Flash ignores
most Posted WM_* clicks). Optional env: ``FLASH_LOADER_CLICK_FRAC_X``, ``FLASH_LOADER_CLICK_FRAC_Y``
(0–1, default 0.5 / 0.55), ``FLASH_WIN_CLICK_RETRIES`` (default 10; retries when a window opens behind another).

After the first MAIN TCP connection (see ``FLASH_AUTO_LOGIN_UI`` in ``.env``), optional automation
can click **Dismiss all** (tunable fractions), then focus the login fields and type credentials
via ``SendInput`` (Unicode). Tune with ``FLASH_LOGIN_*`` environment variables in ``.env`` (or set attributes on the settings object the CLI loads).

Each account can use a different ``proxy_port`` and local ``bind_ip``: the loader URL includes
``host`` / ``proxyHost`` so the client targets the same address the caseus proxy listens on.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence

from . import tfm_swf_port_patch

logger = logging.getLogger(__name__)

# Serialize Flash dismiss/click automation: periodic error-dismiss poll must not run while
# run_flash_login_ui holds the mouse/keyboard (otherwise no LoginPacket — focus stolen).
_flash_ui_locks: dict[int, threading.Lock] = {}
_flash_ui_locks_mutex = threading.Lock()


def _flash_ui_lock(pid: int) -> threading.Lock:
    with _flash_ui_locks_mutex:
        if pid not in _flash_ui_locks:
            _flash_ui_locks[pid] = threading.Lock()
        return _flash_ui_locks[pid]


def try_acquire_flash_ui(pid: int | None) -> bool:
    """Non-blocking: True if this thread now owns the Flash UI lock for ``pid``."""
    if pid is None or pid <= 0:
        return True
    return _flash_ui_lock(pid).acquire(blocking=False)


def release_flash_ui(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    try:
        _flash_ui_lock(pid).release()
    except RuntimeError:
        pass


@contextmanager
def exclusive_flash_ui(pid: int | None):
    """Block until Flash UI is free, then hold the lock for the whole login / dismiss sequence."""
    if pid is None or pid <= 0:
        yield
        return
    lock = _flash_ui_lock(pid)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


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


def _win_enum_top_hwnds_for_pid(pid: int, *, min_client_area: int = 400) -> list[int]:
    """
    Visible top-level HWNDs for this process, **including** owned popups (ActionScript error dialogs).

    ``_win_find_toplevel_hwnd`` skips GW_OWNER windows and picks the largest rect — that misses small
    error dialogs. Sorted by ascending client area so smaller dialogs are clicked before the main stage.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, int]] = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        rc = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
            return True
        area = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
        if area < min_client_area:
            return True
        found.append((int(hwnd), area))
        return True

    user32.EnumWindows(enum_proc, 0)
    found.sort(key=lambda x: x[1])
    return [h for h, _ in found]


def _win_client_area(hwnd: int) -> int:
    """Client-area pixels for a HWND (0 if unavailable)."""
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
        return 0
    return max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)


def _win_hwnds_title_contains(pid: int, substr: str) -> list[int]:
    """Top-level visible HWNDs for ``pid`` whose window title contains ``substr`` (case-insensitive)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    needle = (substr or "").strip().lower()
    if not needle:
        return []
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if needle in (buf.value or "").lower():
            out.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    return out


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
            minimize_raw = (os.environ.get("FLASH_MINIMIZE_AFTER_OPEN") or "").strip().lower()
            minimize_after = minimize_raw in ("1", "true", "yes", "on")
            if _win_click_client_fraction(hwnd, frac_x=fx, frac_y=fy, flash_pid=p.pid):
                logger.info(
                    "Sent mouse click to HWND=%s (slot %s) for Transformice button "
                    "(if MAIN TCP never connects, try frac_y=0.45–0.65 or click manually)",
                    hwnd,
                    label,
                )
                if minimize_after:
                    import ctypes
                    SW_MINIMIZE = 6
                    ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
                    logger.info("Minimized Flash window HWND=%s (slot %s)", hwnd, label)
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


def _cfg_float(cfg: object, env_name: str, default: float) -> float:
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    v = getattr(cfg, env_name, None)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return default


def _cfg_bool(cfg: object, env_name: str, default: bool) -> bool:
    raw = (os.environ.get(env_name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    v = getattr(cfg, env_name, None)
    if isinstance(v, bool):
        return v
    return default


def _cfg_dismiss_extra_y(cfg: object) -> tuple[float, ...]:
    """Extra client Y fractions for ActionScript / debug error dialogs (buttons mid-window)."""
    v = getattr(cfg, "FLASH_LOGIN_DISMISS_FRAC_Y_EXTRA", None)
    if isinstance(v, (list, tuple)) and len(v) > 0:
        out: list[float] = []
        for x in v:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                continue
        if out:
            return tuple(out)
    return (0.62, 0.54)


class _WinSendInput:
    """Lazy-init SendInput ctypes (correct argtypes on x64) + INPUT struct."""

    INPUT = None
    user32 = None
    kernel32 = None


def _ensure_sendinput():
    import ctypes
    from ctypes import wintypes

    if _WinSendInput.INPUT is not None:
        return

    # wintypes.ULONG_PTR is missing on some Python builds (e.g. 3.14+); use pointer-sized unsigned.
    ULONG_PTR = getattr(wintypes, "ULONG_PTR", None)
    if ULONG_PTR is None:
        ULONG_PTR = ctypes.c_size_t

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    kernel32.GetLastError.argtypes = ()
    kernel32.GetLastError.restype = wintypes.DWORD

    _WinSendInput.INPUT = INPUT
    _WinSendInput.user32 = user32
    _WinSendInput.kernel32 = kernel32


def _win_send_unicode_text(
    text: str,
    *,
    slot_label: str,
    debug: bool,
) -> tuple[int, int]:
    """Send Unicode via SendInput. Returns (chars_sent, failures)."""
    import ctypes

    if not text:
        return (0, 0)
    _ensure_sendinput()
    INPUT = _WinSendInput.INPUT
    user32 = _WinSendInput.user32
    kernel32 = _WinSendInput.kernel32
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    ok = 0
    fail = 0
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            logger.warning(
                "Slot %s: skipping non-BMP character U+%X in SendInput",
                slot_label,
                code,
            )
            fail += 1
            continue
        for up in (False, True):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = 0
            inp.u.ki.wScan = code
            inp.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
            inp.u.ki.time = 0
            inp.u.ki.dwExtraInfo = 0
            n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            if n != 1:
                err = int(kernel32.GetLastError())
                if debug:
                    logger.warning(
                        "Slot %s: SendInput unicode failed n=%s err=%s char=%r",
                        slot_label,
                        n,
                        err,
                        ch,
                    )
                fail += 1
                break
        else:
            ok += 1
    if debug:
        logger.info(
            "Slot %s: SendInput unicode summary: chars_ok=%s failures=%s",
            slot_label,
            ok,
            fail,
        )
    elif fail > 0:
        logger.warning(
            "Slot %s: SendInput unicode chars_ok=%s failures=%s (FLASH_LOGIN_DEBUG=1 for per-char)",
            slot_label,
            ok,
            fail,
        )
    return (ok, fail)


def _win_send_vk_tab_enter(vk: int, *, slot_label: str, debug: bool) -> bool:
    import ctypes

    _ensure_sendinput()
    INPUT = _WinSendInput.INPUT
    user32 = _WinSendInput.user32
    kernel32 = _WinSendInput.kernel32
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    for keyup in (False, True):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk & 0xFFFF
        inp.u.ki.wScan = 0
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
        inp.u.ki.time = 0
        inp.u.ki.dwExtraInfo = 0
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            err = int(kernel32.GetLastError())
            if debug:
                logger.warning(
                    "Slot %s: SendInput VK=0x%02X keyup=%s failed n=%s err=%s",
                    slot_label,
                    vk,
                    keyup,
                    n,
                    err,
                )
            return False
    return True


def _win_send_ctrl_vk(vk: int, *, slot_label: str, debug: bool) -> bool:
    import ctypes

    VK_CONTROL = 0x11  # left/right control for SendInput key combos

    _ensure_sendinput()
    INPUT = _WinSendInput.INPUT
    user32 = _WinSendInput.user32
    kernel32 = _WinSendInput.kernel32
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    def one(v: int, keyup: bool) -> bool:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = v & 0xFFFF
        inp.u.ki.wScan = 0
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
        inp.u.ki.time = 0
        inp.u.ki.dwExtraInfo = 0
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            if debug:
                logger.warning(
                    "Slot %s: SendInput ctrl combo failed vk=0x%02X keyup=%s err=%s",
                    slot_label,
                    v,
                    keyup,
                    int(kernel32.GetLastError()),
                )
            return False
        return True

    if not one(VK_CONTROL, False):
        return False
    if not one(vk, False):
        return False
    if not one(vk, True):
        return False
    if not one(VK_CONTROL, True):
        return False
    return True


def _clipboard_set_unicode_text(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    GMEM_MOVEABLE = 0x0002
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        raw = (text + "\0").encode("utf-16-le")
        size = len(raw)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(ptr, raw, size)
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _win_clipboard_paste_field(
    *,
    text: str,
    slot_label: str,
    field_label: str,
    debug: bool,
    clip_pause: float,
    retry: bool,
    retry_gap: float,
) -> None:
    """Ctrl+A, set clipboard, Ctrl+V. Optional second pass reduces truncated paste in Flash."""
    if not _win_send_ctrl_vk(0x41, slot_label=slot_label, debug=debug):
        logger.warning("Slot %s: Ctrl+A (%s) failed", slot_label, field_label)
    time.sleep(max(0.0, clip_pause))
    _clipboard_set_unicode_text(text)
    time.sleep(max(0.0, clip_pause))
    if not _win_send_ctrl_vk(0x56, slot_label=slot_label, debug=debug):
        logger.warning("Slot %s: Ctrl+V (%s) failed", slot_label, field_label)
    if retry and text:
        time.sleep(max(0.0, retry_gap))
        if debug:
            logger.info(
                "Slot %s: %s clipboard second pass (Ctrl+A, Ctrl+V) — mitigates partial paste/typos",
                slot_label,
                field_label,
            )
        if not _win_send_ctrl_vk(0x41, slot_label=slot_label, debug=debug):
            logger.warning("Slot %s: Ctrl+A (%s) retry failed", slot_label, field_label)
        time.sleep(max(0.0, clip_pause))
        _clipboard_set_unicode_text(text)
        time.sleep(max(0.0, clip_pause))
        if not _win_send_ctrl_vk(0x56, slot_label=slot_label, debug=debug):
            logger.warning("Slot %s: Ctrl+V (%s) retry failed", slot_label, field_label)


def _win_flash_focus_stage(hwnd_top: int) -> int:
    """Foreground + SetFocus on largest child (Flash stage). Returns HWND focused."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    target = _win_best_click_hwnd(hwnd_top)
    _win_force_foreground(hwnd_top)
    time.sleep(0.05)
    user32.SetFocus(target)
    return int(target)


def _win_log_visible_windows_for_pid(pid: int, slot_label: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        found.append((int(hwnd), title.value, cls.value))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        logger.info("FLASH_LOGIN_DEBUG slot %s: no visible top-level HWNDs for pid=%s", slot_label, pid)
        return
    for h, title, cls in found:
        logger.info(
            "FLASH_LOGIN_DEBUG slot %s: pid=%s hwnd=%s class=%r title=%r",
            slot_label,
            pid,
            h,
            cls[:120],
            (title or "")[:120],
        )


def flash_pid_is_alive(pid: int) -> bool:
    """True if Windows process ``pid`` is still running."""
    if sys.platform != "win32" or pid <= 0:
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        STILL_ACTIVE = 259
        return int(code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


def dismiss_flashplayer_actionscript_dialogs(
    pid: int | None,
    slot_label: str,
    cfg: object,
    *,
    pre_dismiss_sec: float | None = None,
    debug: bool | None = None,
    quiet: bool = False,
) -> None:
    """
    Click Dismiss All / error-dialog taps on Adobe Flash Player top-level windows for this pid.

    Used by auto-login and optionally by a background poll when ActionScript securityError #2048
    appears (Flash blocks loading from public hosts; tune FLASH_LOGIN_* dismiss fractions if needed).
    """
    if sys.platform != "win32":
        return
    if pid is None or pid <= 0:
        return

    step = _cfg_float(cfg, "FLASH_LOGIN_STEP_DELAY_SEC", 0.04)
    dismiss_x = _cfg_float(cfg, "FLASH_LOGIN_DISMISS_FRAC_X", 0.5)
    dismiss_y = _cfg_float(cfg, "FLASH_LOGIN_DISMISS_FRAC_Y", 0.82)
    dismiss_taps = int(_cfg_float(cfg, "FLASH_LOGIN_DISMISS_TAPS", 2.0))
    dismiss_taps = max(1, min(8, dismiss_taps))
    dismiss_extra_y = _cfg_dismiss_extra_y(cfg)
    flash_err_x = _cfg_float(cfg, "FLASH_LOGIN_FLASHPLAYER_ERROR_DISMISS_FRAC_X", 0.30)
    flash_err_y = _cfg_float(cfg, "FLASH_LOGIN_FLASHPLAYER_ERROR_DISMISS_FRAC_Y", 0.88)
    dismiss_double = _cfg_bool(cfg, "FLASH_LOGIN_DISMISS_DOUBLE_CLICK", False)
    if pre_dismiss_sec is None:
        pre_dismiss_sec = _cfg_float(cfg, "FLASH_LOGIN_PRE_DISMISS_SEC", 0.0)
    if debug is None:
        debug = _cfg_bool(cfg, "FLASH_LOGIN_DEBUG", False)
    dismiss_pause = step * 0.22
    dismiss_between_taps = step * 0.18

    if not quiet:
        logger.info(
            "Slot %s: waiting %.2fs for Adobe Flash Player / error dialogs before dismiss",
            slot_label,
            max(0.0, pre_dismiss_sec),
        )
    time.sleep(max(0.0, pre_dismiss_sec))
    by_area = _win_enum_top_hwnds_for_pid(pid)
    titled = _win_hwnds_title_contains(pid, "flash player")
    dismiss_hwnds = list(dict.fromkeys(titled + by_area))
    hwnd = _win_find_toplevel_hwnd(pid)
    if not dismiss_hwnds and hwnd is not None:
        dismiss_hwnds = [hwnd]
    if not dismiss_hwnds:
        return
    # The main game window is also titled "Adobe Flash Player …" — do not run error-dialog taps on
    # it or we scramble the login form (no LoginPacket). Only small popups (ActionScript errors).
    max_dialog_area = int(_cfg_float(cfg, "FLASH_DISMISS_MAX_DIALOG_CLIENT_AREA", 120_000.0))
    max_dialog_area = max(8_000, min(max_dialog_area, 500_000))
    before_ct = len(dismiss_hwnds)
    dismiss_hwnds = [h for h in dismiss_hwnds if _win_client_area(h) < max_dialog_area]
    if not dismiss_hwnds:
        if not quiet:
            logger.info(
                "Slot %s: skip dismiss — no error-dialog HWND under %s px² client area "
                "(main game stage is never dismiss-clicked)",
                slot_label,
                max_dialog_area,
            )
        return
    if not quiet and before_ct > len(dismiss_hwnds):
        logger.info(
            "Slot %s: dismiss limited to %s small window(s) (excluded %s large; max area %s px²)",
            slot_label,
            len(dismiss_hwnds),
            before_ct - len(dismiss_hwnds),
            max_dialog_area,
        )
    if not quiet:
        logger.info(
            "Slot %s: dismiss on %s top-level window(s) (%s title match \"flash player\"); smallest first",
            slot_label,
            len(dismiss_hwnds),
            len(titled),
        )
    for hi, h_dismiss in enumerate(dismiss_hwnds):
        _win_click_client_fraction(
            h_dismiss,
            frac_x=flash_err_x,
            frac_y=flash_err_y,
            double_click=False,
            debug_label=f"{slot_label}/flashplayer-dismiss-all/w{hi}" if debug else None,
            flash_pid=None,
            resolve_pid=False,
        )
        time.sleep(0.02 if hi + 1 < len(dismiss_hwnds) else max(0.02, step * 0.2))
    for tap in range(dismiss_taps):
        off = (tap - (dismiss_taps // 2)) * 0.07
        fy = min(1.0, max(0.0, dismiss_y + off))
        for hi, h_dismiss in enumerate(dismiss_hwnds):
            _win_click_client_fraction(
                h_dismiss,
                frac_x=dismiss_x,
                frac_y=fy,
                double_click=dismiss_double,
                debug_label=f"{slot_label}/dismiss#{tap + 1}/w{hi}" if debug else None,
                flash_pid=None,
                resolve_pid=False,
            )
            time.sleep(dismiss_pause)
        time.sleep(dismiss_between_taps)
    for extra_y in dismiss_extra_y:
        ey = min(1.0, max(0.0, float(extra_y)))
        for hi, h_dismiss in enumerate(dismiss_hwnds):
            _win_click_client_fraction(
                h_dismiss,
                frac_x=dismiss_x,
                frac_y=ey,
                double_click=False,
                debug_label=f"{slot_label}/dismiss-aserr/w{hi}" if debug else None,
                flash_pid=None,
                resolve_pid=False,
            )
            time.sleep(dismiss_between_taps)
    if not quiet:
        logger.info(
            "Slot %s: dismiss done (Dismiss All ≈(%.2f,%.2f) each window; primary y≈%.2f + extra %s)",
            slot_label,
            flash_err_x,
            flash_err_y,
            dismiss_y,
            dismiss_extra_y,
        )


def run_flash_login_ui(
    *,
    pid: int | None,
    username: str,
    password: str,
    slot_label: str,
    cfg: object,
    trigger: str = "after_launch",
) -> None:
    """
    Dismiss / Continue dialogs and type login (Windows).

    Prefer ``trigger="main_tcp"`` (from ``ban_cli`` when ``FLASH_LOGIN_TRIGGER`` is ``main_tcp``):
    dismiss dialogs first (``FLASH_LOGIN_PRE_DISMISS_SEC`` is usually 0), then
    ``FLASH_LOGIN_AFTER_MAIN_DELAY_SEC`` pauses for the login form before typing.
    ``FLASH_LOGIN_BEFORE_USERNAME_EXTRA_SEC`` / ``FLASH_LOGIN_BEFORE_SUBMIT_SEC`` add pauses before
    field click and submit. Enable ``FLASH_LOGIN_TIMING_LOG`` for phase timestamps vs proxy logs.
    Use ``after_launch`` for a fixed timer from Flash start instead.
    """
    if sys.platform != "win32":
        return

    if pid is None or pid <= 0:
        logger.warning("Slot %s: auto-login skipped (invalid flash pid)", slot_label)
        return
    user = (username or "").strip()
    if not user:
        logger.info("Slot %s: auto-login skipped (empty username)", slot_label)
        return

    with exclusive_flash_ui(pid):
        _run_flash_login_ui_inner(
            pid=pid,
            username=user,
            password=password or "",
            slot_label=slot_label,
            cfg=cfg,
            trigger=trigger,
        )


def _run_flash_login_ui_inner(
    *,
    pid: int,
    username: str,
    password: str,
    slot_label: str,
    cfg: object,
    trigger: str,
) -> None:
    """Body of ``run_flash_login_ui`` while ``exclusive_flash_ui(pid)`` is held."""
    import ctypes

    user = username
    debug = _cfg_bool(cfg, "FLASH_LOGIN_DEBUG", False)
    step = _cfg_float(cfg, "FLASH_LOGIN_STEP_DELAY_SEC", 0.04)
    skip_dismiss = _cfg_bool(cfg, "FLASH_LOGIN_SKIP_DISMISS", False)
    use_clipboard = _cfg_bool(cfg, "FLASH_LOGIN_USE_CLIPBOARD", True)

    user_x = _cfg_float(cfg, "FLASH_LOGIN_USERNAME_FRAC_X", 0.38)
    user_y = _cfg_float(cfg, "FLASH_LOGIN_USERNAME_FRAC_Y", 0.44)
    submit_x = _cfg_float(cfg, "FLASH_LOGIN_SUBMIT_FRAC_X", 0.38)
    submit_y = _cfg_float(cfg, "FLASH_LOGIN_SUBMIT_FRAC_Y", 0.58)
    pre_dismiss_sec = _cfg_float(cfg, "FLASH_LOGIN_PRE_DISMISS_SEC", 0.0)
    use_submit_click = _cfg_bool(cfg, "FLASH_LOGIN_USE_SUBMIT_CLICK", True)
    use_enter_submit = True  # always: backup submit after click

    clip_pause = _cfg_float(cfg, "FLASH_LOGIN_CLIPBOARD_INTER_KEY_SEC", 0.05)
    pre_clip = _cfg_float(cfg, "FLASH_LOGIN_PRE_CLIPBOARD_SEC", 0.05)
    retry_gap = _cfg_float(cfg, "FLASH_LOGIN_CLIPBOARD_RETRY_GAP_SEC", 0.08)
    retry_user_clip = _cfg_bool(cfg, "FLASH_LOGIN_USERNAME_CLIPBOARD_RETRY", True)
    retry_pw_clip = _cfg_bool(cfg, "FLASH_LOGIN_PASSWORD_CLIPBOARD_RETRY", True)
    after_user_paste = _cfg_float(cfg, "FLASH_LOGIN_AFTER_USERNAME_PASTE_SEC", 0.06)
    after_pw_paste = _cfg_float(cfg, "FLASH_LOGIN_AFTER_PASSWORD_PASTE_SEC", 0.05)
    after_submit_extra = _cfg_float(cfg, "FLASH_LOGIN_AFTER_SUBMIT_EXTRA_SEC", 0.15)
    second_submit = _cfg_bool(cfg, "FLASH_LOGIN_SECOND_SUBMIT_CLICK", True)
    second_submit_delay = _cfg_float(cfg, "FLASH_LOGIN_SECOND_SUBMIT_DELAY_SEC", 0.25)

    settle = _cfg_float(cfg, "FLASH_LOGIN_AFTER_LAUNCH_SETTLE_SEC", 0.15)
    after_main = _cfg_float(cfg, "FLASH_LOGIN_AFTER_MAIN_DELAY_SEC", 2.0)
    before_username_extra = _cfg_float(cfg, "FLASH_LOGIN_BEFORE_USERNAME_EXTRA_SEC", 0.35)
    before_submit = _cfg_float(cfg, "FLASH_LOGIN_BEFORE_SUBMIT_SEC", 0.35)
    timing_log = _cfg_bool(cfg, "FLASH_LOGIN_TIMING_LOG", True)
    gap = max(0.02, min(0.1, float(step) * 1.2))

    _t0 = time.monotonic()

    def _phase(msg: str) -> None:
        if timing_log:
            logger.info(
                "Slot %s: FLASH_LOGIN_TIMING +%.3fs %s",
                slot_label,
                time.monotonic() - _t0,
                msg,
            )

    _phase("run_flash_login_ui start")

    trig = (trigger or "after_launch").strip().lower()
    is_main_tcp = trig in ("main_tcp", "main", "tcp")
    hwnd: int | None = None

    logger.info(
        "Slot %s: run_flash_login_ui trigger=%s pid=%s user_len=%s password_set=%s",
        slot_label,
        trig,
        pid,
        len(user),
        bool((password or "").strip()),
    )

    if is_main_tcp:
        # MAIN TCP already accepted: get HWND, dismiss error dialogs ASAP, then pause for login form.
        logger.info("Slot %s: waiting for Flash HWND (timeout 20s) pid=%s", slot_label, pid)
        hwnd = _wait_hwnd_for_pid(pid, timeout_sec=20.0)
        if hwnd is None:
            logger.warning("Slot %s: auto-login: no Flash HWND for pid %s", slot_label, pid)
            return
        logger.info(
            "Slot %s: FLASH auto-login (main_tcp) HWND ok; dismiss first, then %.2fs before username pid=%s",
            slot_label,
            max(0.0, after_main),
            pid,
        )
    else:
        logger.info(
            "Slot %s: FLASH auto-login (after_launch) settle %.2fs pid=%s",
            slot_label,
            max(0.0, settle),
            pid,
        )
        time.sleep(max(0.0, settle))
        logger.info("Slot %s: waiting for Flash HWND (timeout 20s) pid=%s", slot_label, pid)
        hwnd = _wait_hwnd_for_pid(pid, timeout_sec=20.0)
        if hwnd is None:
            logger.warning("Slot %s: auto-login: no Flash HWND for pid %s", slot_label, pid)
            return

    if debug:
        fg = ctypes.windll.user32.GetForegroundWindow() if sys.platform == "win32" else 0
        logger.info(
            "FLASH_LOGIN_DEBUG slot %s: pre-focus foreground_hwnd=%s target_hwnd=%s trigger=%s",
            slot_label,
            fg,
            hwnd,
            trig,
        )
        _win_log_visible_windows_for_pid(pid, slot_label)

    # Focusing the main Flash window first hides smaller ActionScript / owned error dialogs — run
    # dismiss taps per-window first, then focus main below for username/password.
    if skip_dismiss:
        _win_force_foreground(hwnd)
        time.sleep(gap * 0.75)
        stage = _win_flash_focus_stage(hwnd)
    else:
        stage = hwnd
    if debug:
        fg2 = ctypes.windll.user32.GetForegroundWindow()
        logger.info(
            "FLASH_LOGIN_DEBUG slot %s: foreground_hwnd=%s stage_hwnd=%s skip_dismiss=%s",
            slot_label,
            fg2,
            stage,
            skip_dismiss,
        )

    if not skip_dismiss:
        dismiss_flashplayer_actionscript_dialogs(
            pid,
            slot_label,
            cfg,
            pre_dismiss_sec=pre_dismiss_sec,
            debug=debug,
            quiet=False,
        )
        _phase("dismiss done")

    if is_main_tcp:
        logger.info(
            "Slot %s: post-dismiss pause %.2fs before username (FLASH_LOGIN_AFTER_MAIN_DELAY_SEC) pid=%s",
            slot_label,
            max(0.0, after_main),
            pid,
        )
        time.sleep(max(0.0, after_main))
        _phase("post-dismiss pause done (FLASH_LOGIN_AFTER_MAIN_DELAY_SEC)")

    hwnd_refresh = _win_find_toplevel_hwnd(pid)
    if hwnd_refresh is not None:
        hwnd = hwnd_refresh

    if not skip_dismiss:
        _win_force_foreground(hwnd)
        time.sleep(gap)
        _win_flash_focus_stage(hwnd)
        time.sleep(gap * 0.75)

    if max(0.0, before_username_extra) > 0:
        _phase("before_username_extra (FLASH_LOGIN_BEFORE_USERNAME_EXTRA_SEC)")
        time.sleep(max(0.0, before_username_extra))

    _phase("click username field")
    _win_click_client_fraction(
        hwnd,
        frac_x=user_x,
        frac_y=user_y,
        debug_label=f"{slot_label}/username" if debug else None,
        flash_pid=pid,
    )
    time.sleep(max(0.04, step * 0.28))
    _win_flash_focus_stage(hwnd)
    time.sleep(max(0.05, pre_clip))

    if use_clipboard:
        _win_clipboard_paste_field(
            text=user,
            slot_label=slot_label,
            field_label="username",
            debug=debug,
            clip_pause=clip_pause,
            retry=retry_user_clip,
            retry_gap=retry_gap,
        )
    else:
        if not _win_send_ctrl_vk(0x41, slot_label=slot_label, debug=debug):
            logger.warning("Slot %s: Ctrl+A (username field) failed", slot_label)
        time.sleep(0.05)
        o, f = _win_send_unicode_text(user, slot_label=slot_label, debug=debug)
        logger.info("Slot %s: SendInput username chars_ok=%s failures=%s", slot_label, o, f)
        if f and o == 0:
            logger.warning(
                "Slot %s: SendInput typed no characters; set FLASH_LOGIN_USE_CLIPBOARD = True",
                slot_label,
            )

    time.sleep(max(step * 0.22, after_user_paste))
    if not _win_send_vk_tab_enter(0x09, slot_label=slot_label, debug=debug):
        logger.warning("Slot %s: Tab to password failed", slot_label)
    time.sleep(max(step * 0.22, after_user_paste * 0.5))

    if use_clipboard:
        _win_clipboard_paste_field(
            text=password or "",
            slot_label=slot_label,
            field_label="password",
            debug=debug,
            clip_pause=clip_pause,
            retry=retry_pw_clip,
            retry_gap=retry_gap,
        )
    else:
        o_pw, f_pw = _win_send_unicode_text(password or "", slot_label=slot_label, debug=debug)
        logger.info("Slot %s: SendInput password chars_ok=%s failures=%s", slot_label, o_pw, f_pw)

    time.sleep(max(step * 0.22, after_pw_paste))
    _phase("after password input")
    if max(0.0, before_submit) > 0:
        _phase("before_submit (FLASH_LOGIN_BEFORE_SUBMIT_SEC)")
        time.sleep(max(0.0, before_submit))

    if use_submit_click:
        if _win_click_client_fraction(
            hwnd,
            frac_x=submit_x,
            frac_y=submit_y,
            debug_label=f"{slot_label}/submit" if debug else None,
            flash_pid=pid,
        ):
            logger.info(
                "Slot %s: clicked submit area (frac %.2f, %.2f)",
                slot_label,
                submit_x,
                submit_y,
            )
        else:
            logger.warning("Slot %s: submit click failed", slot_label)
    if use_enter_submit:
        time.sleep(gap)
        if not _win_send_vk_tab_enter(0x0D, slot_label=slot_label, debug=debug):
            logger.warning("Slot %s: Enter submit failed", slot_label)

    time.sleep(max(0.0, after_submit_extra))
    if second_submit and use_submit_click:
        time.sleep(max(0.0, second_submit_delay))
        logger.info(
            "Slot %s: second submit (FLASH_LOGIN_SECOND_SUBMIT_CLICK) — helps if first submit was too early",
            slot_label,
        )
        _win_force_foreground(hwnd)
        time.sleep(gap)
        _win_flash_focus_stage(hwnd)
        time.sleep(gap)
        if _win_click_client_fraction(
            hwnd,
            frac_x=submit_x,
            frac_y=submit_y,
            debug_label=f"{slot_label}/submit2" if debug else None,
            flash_pid=pid,
        ):
            logger.info(
                "Slot %s: clicked submit area again (frac %.2f, %.2f)",
                slot_label,
                submit_x,
                submit_y,
            )
        if use_enter_submit:
            time.sleep(gap)
            if not _win_send_vk_tab_enter(0x0D, slot_label=slot_label, debug=debug):
                logger.warning("Slot %s: Enter submit (second pass) failed", slot_label)

    _phase(
        "auto-login UI steps finished — LoginPacket may arrive later; compare with "
        "'LoginPacket X.XXs after first MAIN HandshakePacket' in proxy log"
    )
    logger.info(
        "Slot %s: auto-login sequence finished (FLASH_LOGIN_DEBUG=%s use_clipboard=%s)",
        slot_label,
        debug,
        use_clipboard,
    )


def run_flash_login_after_main(
    *,
    pid: int | None,
    username: str,
    password: str,
    slot_label: str,
    cfg: object,
) -> None:
    """Backward-compatible alias: same as ``run_flash_login_ui`` with ``trigger=\"main_tcp\"``."""
    return run_flash_login_ui(
        pid=pid,
        username=username,
        password=password,
        slot_label=slot_label,
        cfg=cfg,
        trigger="main_tcp",
    )


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
