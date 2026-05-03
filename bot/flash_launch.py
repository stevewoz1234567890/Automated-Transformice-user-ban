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

import ctypes
import hashlib
import logging
import os
import re
import subprocess
import sys
import unicodedata
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

# BM_CLICK / WM_CLOSE paths in :func:`dismiss_flash_error_dialogs_no_mouse` (session totals for log.txt + reports).
_as_dismiss_closed_total = 0
_as_dismiss_incorrect_version_total = 0
_as_dismiss_unique_fp_total = 0
_as_dismiss_seen_fp: set[str] = set()
_as_dismiss_stats_lock = threading.Lock()
# Same AS error body dismissed again (session): milestone logs for root-cause persistence.
_as_fp_repeat_in_session: dict[str, int] = {}
# Monotonic time of last successful AS/Adobe error dismiss per slot — correlated with MAIN
# clean-eof in ban_proxy when ``BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE`` is enabled.
_last_as_dismiss_mono_by_slot: dict[str, float] = {}
_last_as_dismiss_detail_by_slot: dict[str, str] = {}
_as_dismiss_mono_lock = threading.Lock()


def issue1_as_main_correlation_window_sec() -> float:
    """
    Seconds: if MAIN closes within this window after an AS-dismiss, diagnostics append ``issue1_near_as_dismiss=``.
    Set ``BOT_ISSUE1_AS_MAIN_CORR_WINDOW_SEC=0`` to disable (not recommended when hunting PARTL-as_sweep).
    """
    raw = (os.environ.get("BOT_ISSUE1_AS_MAIN_CORR_WINDOW_SEC") or "").strip()
    if not raw:
        return 12.0
    try:
        v = float(raw)
    except ValueError:
        return 12.0
    return max(0.0, min(120.0, v))


def last_as_dismiss_detail_for_slot(slot_label: str) -> str:
    """Last correlation tail from :func:`record_as_dismiss_monotonic_for_slot` (Flash UI action). Empty if unset."""
    lab = (slot_label or "").strip()
    if not lab:
        return ""
    with _as_dismiss_mono_lock:
        return _last_as_dismiss_detail_by_slot.get(lab, "")


def _dismiss_action_corr(
    *,
    dismiss_context: str | None,
    operator_phase: str | None,
    pid: int,
    meth: str,
    dlg_hwnd: int,
    extra: str = "",
    incorrect_version: bool = False,
) -> str:
    ctx = dismiss_context or "default"
    ph = operator_phase if operator_phase else "?"
    x = " ".join((extra or "").replace("\r", " ").replace("\n", " ").split())
    if len(x) > 120:
        x = x[:117] + "…"
    iv_bit = "1" if incorrect_version else "0"
    base = (
        f"ctx={ctx} phase={ph} pid={pid} dlg={dlg_hwnd} meth={meth} iv={iv_bit}"
    )
    return f"{base} {x}".strip() if x else base


def _issue1_as_dismiss_action_log_enabled() -> bool:
    return (os.environ.get("BOT_ISSUE1_AS_DISMISS_LOG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def record_as_dismiss_monotonic_for_slot(
    slot_label: str,
    *,
    detail: str = "",
) -> None:
    """Record dismiss time and optional correlation string for MAIN teardown diagnostics."""
    lab = (slot_label or "").strip()
    if not lab:
        return
    with _as_dismiss_mono_lock:
        _last_as_dismiss_mono_by_slot[lab] = time.monotonic()
        if detail:
            t = " ".join(detail.replace("\r", " ").replace("\n", " ").split())
            clipped = t[:440] + ("…" if len(t) > 440 else "")
            _last_as_dismiss_detail_by_slot[lab] = clipped
            if _issue1_as_dismiss_action_log_enabled():
                logger.warning(
                    "ISSUE1_AS_DISMISS_ACTION slot=%s %s",
                    lab,
                    t[:560] + ("…" if len(t) > 560 else ""),
                )


def last_as_dismiss_monotonic_for_slot(slot_label: str) -> float | None:
    lab = (slot_label or "").strip()
    if not lab:
        return None
    with _as_dismiss_mono_lock:
        return _last_as_dismiss_mono_by_slot.get(lab)


def _record_as_dismiss_close(*, incorrect_version: bool) -> tuple[int, int]:
    global _as_dismiss_closed_total, _as_dismiss_incorrect_version_total
    with _as_dismiss_stats_lock:
        _as_dismiss_closed_total += 1
        if incorrect_version:
            _as_dismiss_incorrect_version_total += 1
        return _as_dismiss_closed_total, _as_dismiss_incorrect_version_total


def as_error_dismiss_session_snapshot() -> dict[str, int]:
    with _as_dismiss_stats_lock:
        return {
            "actionscript_error_dialogs_closed": _as_dismiss_closed_total,
            "incorrect_version_dialogs": _as_dismiss_incorrect_version_total,
            "actionscript_error_unique_fingerprints": _as_dismiss_unique_fp_total,
        }


def _flash_dialog_aggregate_body_text(top_hwnd: int, user32: object) -> str:
    """
    Adobe Flash Player ActionScript error dialogs often put the stack trace in an ``Edit`` or
    ``RichEdit20W`` **nested under child ``#32770`` panels**, not only as direct children.

    Older code used a single ``EnumChildWindows`` on the top dialog and only read ``Static``,
    so logs showed ``body='Error de ActionScript:'`` (locale header only) — hiding **#2048 / #2044**
    lines needed to fix upstream literals / sandbox issues.
    """
    import ctypes
    from ctypes import wintypes

    WM_GETTEXT = 0x000D
    WM_GETTEXTLENGTH = 0x000E
    parts: list[str] = []

    def _maybe_append_piece_for_hwnd(ch: int) -> None:
        cls_buf = ctypes.create_unicode_buffer(96)
        user32.GetClassNameW(ch, cls_buf, 96)
        cls = cls_buf.value.lower()
        piece = ""
        if cls == "static":
            tb = ctypes.create_unicode_buffer(16384)
            user32.GetWindowTextW(ch, tb, 16384)
            piece = (tb.value or "").strip()
        elif cls == "edit" or cls.startswith("richedit"):
            try:
                ln = int(user32.SendMessageW(ch, WM_GETTEXTLENGTH, 0, 0))
            except (TypeError, ValueError, OSError):
                ln = 0
            ln = max(0, min(int(ln), 32767))
            if ln <= 0:
                tb = ctypes.create_unicode_buffer(16384)
                user32.GetWindowTextW(ch, tb, 16384)
                piece = (tb.value or "").strip()
            else:
                buf = ctypes.create_unicode_buffer(ln + 4)
                user32.SendMessageW(ch, WM_GETTEXT, ln + 1, ctypes.addressof(buf))
                piece = (buf.value or "").strip()
        if piece:
            parts.append(piece)

    def _walk_dialog_tree(parent: int) -> None:
        _maybe_append_piece_for_hwnd(parent)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_one_level(child, _lp):
            _walk_dialog_tree(int(child))
            return True

        user32.EnumChildWindows(parent, _enum_one_level, 0)

    _walk_dialog_tree(top_hwnd)
    merged = " | ".join(parts)
    return merged.strip()


def _as_error_normalize_for_fp(text: str) -> str:
    s = " ".join((text or "").split())
    return s[:12000]


def _maybe_log_first_seen_as_fingerprint(
    *,
    body_text: str,
    slot_label: str,
    pid: int,
    hwnd: int,
) -> None:
    """Once per distinct dialog body (session): WARNING with enough text to identify AS fault lines."""
    global _as_dismiss_unique_fp_total
    norm = _as_error_normalize_for_fp(body_text)
    if len(norm) < 12:
        return
    fp = hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()[:16]
    with _as_dismiss_stats_lock:
        if fp in _as_dismiss_seen_fp:
            _as_fp_repeat_in_session[fp] = _as_fp_repeat_in_session.get(fp, 0) + 1
            rpt = _as_fp_repeat_in_session[fp]
            # Same underlying ActionScript fault still firing — dismiss spam will not fix it.
            if rpt in (5, 15, 40, 100, 200, 500):
                suf = ""
                try:
                    from .tfm_loader_alignment import (
                        get_last_alignment_summary,
                        issue1_alignment_log_fragment,
                    )

                    alf = issue1_alignment_log_fragment(get_last_alignment_summary())
                    if alf:
                        suf = f" — {alf}"
                    else:
                        su = get_last_alignment_summary()
                        if su and su.get("url_hints_strict_mismatch_vs_config"):
                            suf = " — ISSUE1_LOADER_HINT=embedded_URL_versions_contradict_TFM_SECRETS_GAME_VERSION"
                except Exception:
                    pass
                logger.warning(
                    "ActionScript error duplicate #%d fingerprint=%s slot=%s pid=%s hwnd=%s — "
                    "same dialog body repeating; prioritize TFM_PROXY_SWF / game version alignment "
                    "(BOT_TFM_PROXY_LOADER_DOWNLOAD_URL fetch source, copy known-good loader+secrets) "
                    "%s",
                    rpt,
                    fp,
                    slot_label,
                    pid,
                    hwnd,
                    suf,
                )
            return
        _as_dismiss_seen_fp.add(fp)
        _as_dismiss_unique_fp_total = len(_as_dismiss_seen_fp)
    try:
        prev_cap = int((os.environ.get("FLASH_ERROR_FIRST_FP_PREVIEW_CHARS") or "1400").strip())
    except ValueError:
        prev_cap = 1400
    prev_cap = max(200, min(12000, prev_cap))
    preview = norm[:prev_cap].replace("\r", " ").replace("\n", " │ ")
    logger.warning(
        "ActionScript error fingerprint=%s slot=%s pid=%s hwnd=%s (first time this session) — %s",
        fp,
        slot_label,
        pid,
        hwnd,
        preview,
    )
    low = norm.lower()
    if "#2048" in low or "error #2048" in low or "securityerror #2048" in low:
        logger.warning(
            "ActionScript root-cause hint fingerprint=%s: Flash security/sandbox Error #2048 — patched "
            "file:// loader must avoid bare upstream IP literals (see bot/tfm_swf_port_patch.py).",
            fp,
        )
    elif "#2044" in low or "error #2044" in low:
        logger.warning(
            "ActionScript root-cause hint fingerprint=%s: Error #2044 IO failure — loader/SWF URL or XML "
            "socket path often misaligned with live Transformice.",
            fp,
        )
    try:
        from .issue1_forensic import log_as_error_slot_banner

        log_as_error_slot_banner(slot_label, pid, fp)
    except Exception:
        logger.debug("ISSUE1_AS_FIRST_FP banner failed", exc_info=True)
    if (os.environ.get("FLASH_ERROR_LOG_FULL_BODY_FIRST_FP") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ) and len(norm) > prev_cap:
        more = norm[prev_cap:].replace("\r", " ").replace("\n", " │ ")
        more_cap = min(10000, len(more))
        logger.warning(
            "ActionScript error fingerprint=%s (continuation, FLASH_ERROR_LOG_FULL_BODY_FIRST_FP) — %s",
            fp,
            more[:more_cap] + ("…" if len(more) > more_cap else ""),
        )


def _flash_error_dismiss_verbose_info_cap() -> int:
    """First N closes per session at INFO; later duplicates at DEBUG (set 0 → always DEBUG except incorrect-version)."""
    try:
        raw = (os.environ.get("FLASH_ERROR_DISMISS_VERBOSE_INFO_CAP") or "").strip()
        cap = int(raw or "12")
    except ValueError:
        cap = 12
    if cap <= 0:
        return 0
    return max(3, min(250, cap))


def _flash_dismiss_body_log_chars() -> int:
    """Max chars of AS dialog body in dismiss log lines (raise for root-cause hunting; default 1600)."""
    try:
        n = int((os.environ.get("FLASH_ERROR_DISMISS_BODY_LOG_CHARS") or "1600").strip())
    except ValueError:
        n = 1600
    return max(120, min(8000, n))


def _as_dismiss_log_closed(
    *,
    incorrect_version: bool,
    sess_tot: int,
    fmt: str,
    args: tuple[object, ...],
) -> None:
    cap = _flash_error_dismiss_verbose_info_cap()
    verbose = incorrect_version or (cap > 0 and sess_tot <= cap)
    (logger.info if verbose else logger.debug)(fmt, *args)


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


def _win_force_foreground(hwnd: int) -> bool:
    """
    Best-effort focus so the following synthetic mouse events hit this window.

    Returns True if ``hwnd`` ends up as the foreground window. After a few Flash
    launches the Windows foreground-lock silently blocks ``SetForegroundWindow``
    from the bot's process and a system-level click (``mouse_event``) then lands on
    whatever app does own the foreground (e.g. the IDE running the bot). To get
    around that, we:

      1. Lower the ``ForegroundLockTimeout`` for this call via ``SystemParametersInfo``.
      2. Inject a benign synthetic key event via ``keybd_event`` — Windows treats
         that as user input, which resets the foreground-lock timer for the current
         thread so the following ``SetForegroundWindow`` call is honoured.
      3. Fall back to ``AttachThreadInput(fg_tid, cur_tid)`` + ``SetForegroundWindow`` +
         ``BringWindowToTop`` as the previous implementation did.
      4. Verify with ``GetForegroundWindow()`` and retry up to 3 times.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    SPIF_SENDCHANGE = 0x02
    try:
        user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE
        )
    except OSError:
        pass

    def _is_foreground(h: int) -> bool:
        return int(user32.GetForegroundWindow() or 0) == int(h)

    for _ in range(3):
        if _is_foreground(hwnd):
            return True

        # Synthetic key stroke: pretends the user just pressed a key, which resets
        # the foreground-lock counter so SetForegroundWindow from a background
        # process is accepted on the very next call. VK_MENU (ALT) is the common
        # choice; we down+up it so no keyboard state leaks.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except OSError:
            pass

        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            if user32.AttachThreadInput(fg_tid, cur_tid, True):
                attached = True
        try:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)

        if _is_foreground(hwnd):
            return True
        time.sleep(0.05)

    return _is_foreground(hwnd)


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
        is_fg = _win_force_foreground(hwnd_cur)
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

        # Check which window actually sits under the target pixel *right now*. If
        # it's not our Flash hwnd (or a descendant), a synthetic cursor click will
        # go to whatever app owns that pixel (e.g. the IDE). In that case we must
        # use PostMessage so the WM_LBUTTONDOWN is delivered to Flash directly.
        user32.WindowFromPoint.argtypes = [POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd_at_point_raw = user32.WindowFromPoint(POINT(pt.x, pt.y))
        hwnd_at_point = int(hwnd_at_point_raw) if hwnd_at_point_raw else 0
        if hwnd_at_point:
            root_raw = user32.GetAncestor(hwnd_at_point_raw, 2)  # GA_ROOT
            root_at_point = int(root_raw) if root_raw else hwnd_at_point
        else:
            root_at_point = 0
        point_belongs_to_flash = (
            hwnd_at_point == target
            or hwnd_at_point == hwnd_cur
            or root_at_point == hwnd_cur
            or root_at_point == target
        )

        WM_MOUSEMOVE = 0x0200
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        MK_LBUTTON = 0x0001
        lparam = (cy & 0xFFFF) << 16 | (cx & 0xFFFF)

        # Always post the click directly to the Flash HWND (client-relative coords)
        # so we guarantee the WM_LBUTTON* reaches Flash regardless of whether some
        # other app stole the foreground between force_foreground() and now.
        try:
            user32.PostMessageW(target, WM_MOUSEMOVE, 0, lparam)
            user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
            time.sleep(0.03)
            user32.PostMessageW(target, WM_LBUTTONUP, 0, lparam)
            if double_click:
                time.sleep(0.05)
                user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
                time.sleep(0.03)
                user32.PostMessageW(target, WM_LBUTTONUP, 0, lparam)
        except OSError as e:
            last_fail = f"PostMessage failed: {e}"
            time.sleep(0.1)
            continue

        # Also fire a real cursor click, but only when the pixel under the target
        # truly belongs to the Flash window — otherwise we'd be clicking on the IDE
        # behind it. Flash accepts either event, so one of the two paths will land.
        if is_fg and point_belongs_to_flash:
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
            click_mode = "PostMessage+mouse_event"
        else:
            click_mode = "PostMessage-only"

        if debug_label:
            logger.debug(
                "FLASH_LOGIN_DEBUG %s: target_hwnd=%s toplevel_hwnd=%s client=(%s,%s) screen=(%s,%s) "
                "frac=(%.4f,%.4f) client_size=%sx%s attempt=%s/%s mode=%s fg=%s "
                "hwnd_at_point=%s root_at_point=%s flash_under_cursor=%s",
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
                click_mode,
                is_fg,
                hwnd_at_point,
                root_at_point,
                point_belongs_to_flash,
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
    doc_log = doc if len(doc) <= 500 else doc[:500] + "..."
    t_launch = time.monotonic()
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

    # Single consolidated launch line (was 3 separate INFO lines before). Full
    # SWF URL goes to DEBUG so the INFO log stays scannable per slot.
    logger.info(
        "Flash launched slot %s PID=%s swf_patched=%s connect=%s:%s sat=%s policy=%s bind_ip=%r",
        label or "?",
        p.pid,
        swf_arg != swf,
        patch_host,
        port,
        satellite,
        policy,
        bind_ip or "(not set)",
    )
    logger.debug(
        "Slot %s Flash launch detail: exe=%s cwd=%s swf_arg=%s",
        label or "?", flash, root, doc_log,
    )

    if click_transformice:
        hwnd = _wait_hwnd_for_pid(p.pid)
        if hwnd is None:
            logger.warning(
                "Slot %s: no HWND for Flash PID %s after window wait; click Transformice manually.",
                label, p.pid,
            )
        else:
            fx, fy = _loader_click_fractions_from_env()
            logger.debug(
                "Slot %s: Flash HWND=%s, sleeping %.2fs before loader click at (%.2f, %.2f)",
                label, hwnd, post_open_delay_sec, fx, fy,
            )
            time.sleep(post_open_delay_sec)
            click_dt = time.monotonic() - t_launch
            if _win_click_client_fraction(hwnd, frac_x=fx, frac_y=fy, flash_pid=p.pid):
                logger.info(
                    "Slot %s: Flash HWND=%s — clicked Transformice button at (%.2f, %.2f) +%.2fs after launch",
                    label, hwnd, fx, fy, click_dt,
                )
            else:
                logger.warning(
                    "Slot %s: Flash HWND=%s — loader click FAILED; click Transformice manually.",
                    label, hwnd,
                )
    else:
        logger.info("Slot %s: auto-click disabled (PID=%s); click Transformice manually.", label, p.pid)

    time.sleep(0.45)
    exit_code = p.poll()
    if exit_code is not None:
        logger.error(
            "Slot %s: Flash process exited immediately (PID=%s exit_code=%s). "
            "Check SWF path, trust cfg, or run flashplayer from a console for errors.",
            label, p.pid, exit_code,
        )
    # (Note: the "still running" INFO line was removed — absence of the error
    # above already implies success, so logging it on every slot was noise.)

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
        logger.debug("FLASH_LOGIN_DEBUG slot %s: no visible top-level HWNDs for pid=%s", slot_label, pid)
        return
    for h, title, cls in found:
        logger.debug(
            "FLASH_LOGIN_DEBUG slot %s: pid=%s hwnd=%s class=%r title=%r",
            slot_label,
            pid,
            h,
            cls[:120],
            (title or "")[:120],
        )


# --- Windows UI language detection (preferred list from GetUserPreferredUILanguages) ----------------------------
def _windows_preferred_ui_language_tags() -> tuple[str, ...]:
    """
    Preferred Windows UI language tags in order (BCP‑47): e.g. ``('de-DE', 'en-US')``.
    Uses ``GetUserPreferredUILanguages(MUI_LANGUAGE_NAME)`` so it follows *Display language*,
    not only regional/date formats.
    """
    if sys.platform != "win32":
        return ()
    try:
        from ctypes import wintypes

        MUI_LANGUAGE_NAME = 0x8
        k32 = ctypes.windll.kernel32
        num_langs = ctypes.c_ulong(0)
        cch = ctypes.c_ulong(0)
        if not k32.GetUserPreferredUILanguages(
            ctypes.c_ulong(MUI_LANGUAGE_NAME), ctypes.byref(num_langs), None, ctypes.byref(cch)
        ):
            return ()
        buflen = max(4, min(int(cch.value), 2048))
        buf = ctypes.create_unicode_buffer(buflen)
        cch_fill = ctypes.c_ulong(buflen)
        if not k32.GetUserPreferredUILanguages(
            ctypes.c_ulong(MUI_LANGUAGE_NAME),
            ctypes.byref(num_langs),
            ctypes.cast(buf, wintypes.LPWSTR),
            ctypes.byref(cch_fill),
        ):
            return ()
        blob = ctypes.string_at(ctypes.addressof(buf), ctypes.sizeof(buf))
        decoded = blob.decode("utf-16-le", errors="replace")
        nonempty = tuple(s for s in decoded.split("\0") if s)
        nl = max(0, min(int(num_langs.value), len(nonempty)))
        return nonempty[:nl] if nl else nonempty
    except Exception:
        logger.debug("GetUserPreferredUILanguages failed", exc_info=True)
        return ()


_LANG_PRIMARY_TAGS: dict[int, str] = {
    # Win32 PRIMARYLANGID (= LANGID & 0x03FF): heuristic logging only — not exhaustive.
    4: "zh",
    7: "de",
    9: "en",
    10: "es",
    12: "fr",
    16: "it",
    17: "ja",
    18: "ko",
    21: "pl",
    22: "pt",
    24: "ro",
    25: "ru",
    26: "hr",
}


def flash_error_windows_ui_language_log_fragment() -> str:
    tags = _windows_preferred_ui_language_tags()
    if tags:
        prim = "|".join(t.split("-", 1)[0].strip().lower() for t in tags[:4] if t)
        safe = "|".join(tags[:6])
        return f"Detected Windows UI langs (first-checked)={safe!s} PRIMARY={prim!s}"
    if sys.platform == "win32":
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        pr = lid & 0x03FF
        tag = _LANG_PRIMARY_TAGS.get(pr, hex(pr))
        return f"No preferred UI-lang list — LANGID_PRIMARY={tag} (fallback 0x{lid:04X})"
    return "Detected Windows UI langs=n/a (not win32)"


def _win_button_label_normalize(raw: str) -> str:
    """Strip mnemonic ``&``, case-fold, accent-fold, squash whitespace."""
    t = (raw or "").replace("&", "").strip().lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return " ".join(t.split())


# Multilingual synonyms for FP #2044 / #2048 style Win32 ``Button`` captions.
# Ranking: lower is stronger preference; Continue-like stays high (3+) so allow_continue guards apply.
_FLASH_DISMISS_ALL_WORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("dismiss", "all"),
    ("ignore", "all"),
    ("omitir", "todo"),
    ("descartar", "todo"),
    ("ignorar", "todo"),
    ("cerrar", "todo"),
    ("ignorar", "tudo"),
    ("dispensar", "todo"),
    ("dispensar", "tudo"),
    ("ignorer", "tout"),
    ("tout", "ignorer"),
    ("schließen", "alle"),
    ("schliessen", "alle"),
    ("ignorieren", "alle"),
    ("ausblenden", "alle"),
    ("chiudi", "tutto"),
    ("ignora", "tutto"),
    ("chiudi", "tutti"),
    ("pomiń", "wszystko"),
    ("pomin", "wszystko"),
    ("pomiń", "wszystkie"),
    ("pomin", "wszystkie"),
    ("закрити", "все"),
    ("игнорировать", "все"),
    ("пропустить", "все"),
)

_FLASH_DISMISS_ALL_EXACT = frozenset[str](
    {
        "dismiss all",
        "ignore all",
        "omitir todo",
        "ignorar todo",
        "ignorar todas",
        "descartar todo",
        "cerrar todo",
        "cerrar todas",
        "dispensar tudo",
        "ignorar tudo",
        "ignorer tout",
        "tout ignorer",
        "fermer tout",
        "alle schließen",
        "alle schliessen",
        "alle ignorieren",
        "alle ausblenden",
        "alle verwerfen",
        "alles ignorieren",
        "alles schließen",
        "alles schliessen",
        "alles overslaan",
        "chiudi tutto",
        "chiudi tutti",
        "ignora tutto",
        "ignora tutti",
        "pomiń wszystkie",
        "pomin wszystkie",
        "pomiń wszystko",
        "pomin wszystko",
        "zamknij wszystko",
        "закрыть все",
        "игнорировать все",
        "пропустить все",
        "关闭全部",
        "全部关闭",
        "全部忽略",
        "忽略全部",
        "すべて無視",
        "すべてを無視",
        "すべて閉じる",
    }
)


def _accent_fold_spaces(s: str) -> str:
    t = unicodedata.normalize("NFD", (s or "").lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return "".join(ch for ch in t if not ch.isspace())


def _looks_like_continue_in_label(label_norm: str) -> bool:
    """Cheap substring traps so compound dismiss labels are not mistaken for rank-1 dismiss."""
    if not label_norm:
        return False
    lf = label_norm.casefold().replace("&", "").lower().strip()
    if "continue" in lf:
        return True
    for frag in ("continuar", "continuer", "continuare", "prosseguir"):
        if frag in lf:
            return True
    if lf == "weiter" or lf == "fortfahren":
        return True
    prod_cy = "\u043f\u0440\u043e\u0434\u043e\u043b\u0436"
    if prod_cy in lf or prod_cy in label_norm.casefold():
        return True
    lf_ns = _accent_fold_spaces(label_norm)
    for needle in ("fortfahren", "weiter"):
        needle_ns = "".join(ch for ch in needle if not ch.isspace())
        if needle_ns and needle_ns in lf_ns:
            return True
    if prod_cy.replace(" ", "") in lf_ns:
        return True
    continue_markers_cn_jp_ko_ar = ("继续", "\u7d9a\u3051\u308b", "\u7d9a\u884c", "\uacc4\uad6d", "\u0645\u062a\u0627\u0628\u0639\u0629")
    for marker in continue_markers_cn_jp_ko_ar:
        if marker in label_norm:
            return True
    return False


_FLASH_DISMISS_SINGLE_EXACT = frozenset[str](
    {
        "dismiss",
        "ignore",
        "ignorer",
        "ignorar",
        "omitir",
        "discard",
        "abbruch",
    }
)

_FLASH_OK_CLICK_EXACT = frozenset[str](
    {
        "ok",
        "okay",
        "aceptar",
        "cerrar",
        "close",
        "yes",
        "sí",
        "si",
        "oui",
        "ja",
        "sim",
        "да",
        "はい",
        "确定",
        "確認",
        "閉じる",
        "\u9589\u3058\u308b",
    }
)

_FLASH_CONTINUE_CLICK_NON_EN = frozenset[str](
    {
        "continuar",
        "continuer",
        "continuare",
        "weiter",
        "fortfahren",
        "proceed",
        "prosseguir",
        "далее",
        "продолжить",
        "继续",
        "\u7d9a\u3051\u308b",
        "\u7d9a\u884c",
        "\uacc4\uad6d",
        "\ub2e4\uc74c",
        "\u0645\u062a\u0627\u0628\u0639\u0629",
    }
)


_RE_FLASH_SA_PROJECTOR_TITLE = re.compile(r"^adobe flash player\s+\d+\s*$", re.IGNORECASE)

# Adobe Flash Player *settings* local-storage prompt (often es-ES: "Permitir" / "Denegar").
# Must not collide with standalone projector "Adobe Flash Player 32" or bare AS-error title.
_FLASH_LSO_DENY_LABELS_EXACT: frozenset[str] = frozenset(
    {
        "denegar",
        "deny",
        "refuser",
        "ablehnen",
        "cancelar",
        "cancel",
        "nein",
        "não",
        "nie",
        "dont allow",
        "don't allow",
    }
)

_FLASH_LSO_ALLOW_LABELS_EXACT: frozenset[str] = frozenset(
    {
        "permitir",
        "allow",
        "zulassen",
        "erlauben",
        "accepter",
        "autoriser",
        "autorizar",
        "aceitar",
        "consentir",
        "toestaan",
        "consenti",
        "accetta",
        "oui",
        "yes",
        "sí",
        "si",
        "ja",
        "sim",
    }
)


def _flash_title_suggests_player_settings_dialog(title: str) -> bool:
    tl = (title or "").strip().lower()
    if not tl:
        return False
    if _RE_FLASH_SA_PROJECTOR_TITLE.match(tl):
        return False
    if "flash" not in tl:
        return False
    chrome = (
        "settings",
        "configuración",
        "configuracion",
        "einstellungen",
        "paramètres",
        "parametres",
        "impostazioni",
        "instellingen",
        "instelling",
        "configuração",
        "configuracao",
        "asetukset",
        "privacy",
    )
    return any(k in tl for k in chrome)


def _flash_body_suggests_local_storage_permission(body: str) -> bool:
    bl = (body or "").lower()
    return any(
        s in bl
        for s in (
            "almacenamiento local",
            "almacenar información",
            "almacenar informacion",
            "local storage",
            "store information on your computer",
            "computer to store information",
            "computer pour stocker des informations",
            "lokaler speicher",
            "lokale speicherung",
            "opslag op uw computer",
            "computer localmente",
            "informações no seu computador",
            "información en su equipo",
            "¿permitir",
        )
    )


def _flash_local_storage_pair_hint_from_buttons(buttons: list[int], user32: object) -> bool:
    """True if captions look like a paired Allow/Deny row (localized)."""
    found_allow = False
    found_deny = False
    buf = ctypes.create_unicode_buffer(256)
    for b in buttons:
        user32.GetWindowTextW(int(b), buf, 256)
        ln = _win_button_label_normalize(buf.value)
        if ln in _FLASH_LSO_ALLOW_LABELS_EXACT or ln.startswith("allow "):
            found_allow = True
        elif ln in _FLASH_LSO_DENY_LABELS_EXACT or "don't allow" in ln:
            found_deny = True
        if found_allow and found_deny:
            return True
    return False


def _pick_flash_local_storage_allow_buttonhwnd(
    buttons: list[int],
    user32: object,
) -> tuple[int | None, str]:
    """Prefer *Permitir* / *Allow*; never BM_CLICK deny/cancel equivalents."""
    candidates: list[tuple[int, int, str]] = []
    buf = ctypes.create_unicode_buffer(256)
    for prio, btn in enumerate(buttons):
        user32.GetWindowTextW(btn, buf, 256)
        lab = buf.value.strip()
        ln = _win_button_label_normalize(lab)
        if not ln:
            continue
        if ln in _FLASH_LSO_DENY_LABELS_EXACT or "don't allow" in ln or "do not allow" in ln:
            continue
        if ln in _FLASH_LSO_ALLOW_LABELS_EXACT:
            return int(btn), lab
        if any(ln.startswith(pref) for pref in ("allow", "permit", "permite")):
            candidates.append((prio, int(btn), lab))
        elif "consentir" in ln or "consenti" in ln:
            candidates.append((prio + 50, int(btn), lab))
    if not candidates:
        return None, ""
    candidates.sort(key=lambda t: t[0])
    _prio, hwnd, lbl = candidates[0]
    return hwnd, lbl


def _flash_matches_dismiss_all(ln: str) -> bool:
    if not ln:
        return False
    if ln in _FLASH_DISMISS_ALL_EXACT:
        return True
    for a, b in _FLASH_DISMISS_ALL_WORD_PAIRS:
        if a in ln and b in ln:
            return True
    return False


def _flash_error_dismiss_button_rank(label_norm: str) -> int | None:
    """
    Lower is better. ``None`` = do not use this button for auto-dismiss.
    Prefers multilingual *Dismiss all* equivalents over Continue.
    """
    if not label_norm:
        return None
    if _flash_matches_dismiss_all(label_norm):
        return 0
    if (
        label_norm in _FLASH_DISMISS_SINGLE_EXACT
        or (
            not _looks_like_continue_in_label(label_norm)
            and "dismiss" in label_norm
        )
    ):
        return 1
    if label_norm in _FLASH_OK_CLICK_EXACT:
        return 2
    if label_norm in _FLASH_CONTINUE_CLICK_NON_EN:
        return 3
    if label_norm == "continue":
        return 4
    return None


def flash_dialog_fallback_bmclick_labels(*, allow_continue: bool) -> set[str]:
    """
    Labels we may BM_CLICK via the fallback exact-label loop — mirrors multilingual rank 0–2 (+ optional continue).
    """
    out = set(_FLASH_DISMISS_ALL_EXACT | _FLASH_DISMISS_SINGLE_EXACT | _FLASH_OK_CLICK_EXACT)
    if allow_continue:
        out |= {"continue"}
        out |= _FLASH_CONTINUE_CLICK_NON_EN
    return out


def _env_flag_false_by_default(name: str) -> bool:
    """
    Intentionally ``False`` by default: unset / empty → False.

    ``FLASH_ERROR_DISMISS_ALLOW_CONTINUE`` and ``FLASH_ERROR_DISMISS_USE_WMCLOSE`` use this
    so the historical accident (auto-clicking *Continuar* and posting ``WM_CLOSE``) is off
    unless the operator enables it.
    """
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    return False


def _env_flag_true_by_default(name: str) -> bool:
    """
    ``True`` when unset. Use ``0``/``false``/``no``/``off`` to disable.
    Suits * farm defaults * where the safe behavior is to opt *out* (e.g. sole Continue click).
    """
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _flash_error_esc_use_foreground() -> bool:
    """Unset / ``true``: try real Esc via ``keybd_event`` after ``SetForegroundWindow`` (like a human)."""
    v = (os.environ.get("FLASH_ERROR_DISMISS_ESC_USE_FOREGROUND") or "true").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _flash_error_esc_post_poll_sec() -> float:
    """Sleep after Esc bursts before polling whether Adobe's HWND is gone."""
    raw = (os.environ.get("FLASH_ERROR_ESC_POST_POLL_MS") or "").strip()
    try:
        ms = float(raw) if raw else 180.0
    except ValueError:
        ms = 180.0
    return max(0.0, min(900.0, ms)) / 1000.0


def _adobe_actionscript_dialog_may_remain(hwnd: int, user32: object) -> bool:
    """True if *hwnd* still looks like an open Adobe dialog (False ⇒ Esc likely dismissed it)."""
    if not hwnd:
        return False
    try:
        if not bool(user32.IsWindow(hwnd)):
            return False
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return True


def _want_flash_local_storage_permission_autoclick(
    *,
    title: str,
    body_text: str,
    buttons: list[int],
    user32: object,
) -> bool:
    """
    Adobe Player *settings* privacy prompt (“Almacenamiento local … Permitir”).
    Separate from standalone projector and ActionScript compile/runtime errors.
    """
    if not _env_flag_true_by_default("FLASH_LOCAL_STORAGE_PERMISSION_DISMISS"):
        return False
    if not buttons:
        return False
    if not _flash_title_suggests_player_settings_dialog(title):
        return False
    if _flash_body_suggests_local_storage_permission(body_text):
        return True
    return _flash_local_storage_pair_hint_from_buttons(buttons, user32)


def _try_escape_adobe_actionscript_dialog(hwnd: int, user32: object) -> None:
    """
    Dismiss Adobe's ActionScript/security error dialogs the same way a user does: Esc.
    Prefer ``SetForegroundWindow`` + ``keybd_event``; fall back to ``PostMessage`` pairs to the HWND.
    """
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    VK_ESCAPE = 0x1B
    KEYEVENTF_KEYUP = 0x0002

    def esc_post_once() -> None:
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_ESCAPE, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_ESCAPE, 0)

    def esc_keybd_once() -> None:
        user32.keybd_event(VK_ESCAPE, 0, 0, 0)
        user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)

    if _flash_error_esc_use_foreground():
        try:
            if _win_force_foreground(hwnd):
                time.sleep(0.04)
                esc_keybd_once()
                time.sleep(0.055)
                esc_keybd_once()
                return
        except Exception:
            logger.debug("Adobe AS dismiss: foreground Esc keybd failed", exc_info=True)
    esc_post_once()
    time.sleep(0.045)
    esc_post_once()


def flash_error_dismiss_policy_log_line() -> str:
    """One line for logs: proves FLASH_ERROR_DISMISS_* env (which build/flags are active)."""
    ac = _env_flag_false_by_default("FLASH_ERROR_DISMISS_ALLOW_CONTINUE")
    wm = _env_flag_false_by_default("FLASH_ERROR_DISMISS_USE_WMCLOSE")
    sole = _env_flag_true_by_default("FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION")
    wl = flash_error_windows_ui_language_log_fragment()
    lso = _env_flag_true_by_default("FLASH_LOCAL_STORAGE_PERMISSION_DISMISS")
    return (
        "ActionScript error dismiss: policy "
        f"[{wl}] "
        f"FLASH_LOCAL_STORAGE_PERMISSION_DISMISS={lso} "
        f"FLASH_ERROR_DISMISS_ALLOW_CONTINUE={ac} "
        f"FLASH_ERROR_DISMISS_USE_WMCLOSE={wm} "
        f"FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION={sole} "
        f"FLASH_ERROR_DISMISS_ESC_USE_FOREGROUND={_flash_error_esc_use_foreground()} "
        "— Adobe Error de ActionScript / #2048 modals honor Esc: we send keyboard Esc "
        "(and only post WM_CLOSE if the dialog is still open). "
        "Flash Player privacy (Local Storage) settings windows: BM_CLICK Permitir/Allow when "
        "FLASH_LOCAL_STORAGE_PERMISSION_DISMISS is true (default). "
        "Button matching is multilingual (not English-only); Windows UI langs are logged first for support. "
        "Continuar/Continue is only auto-clicked when ALLOW_CONTINUE is true, or when the "
        "dialog has no Dismiss/OK and CONTINUE_IF_SOLE_OPTION is true (default). "
        "Post-login sweep and login-phase Flash error polling use BOT_POST_LOGIN_AS_SWEEP_USE_WMCLOSE "
        "and BOT_POST_LOGIN_AS_SWEEP_ADOBE_ESCAPE_WMCLOSE_ONLY (default: Esc first, WM_CLOSE fallback; "
        "no BM_CLICK on Descartar/Dismiss; BOT_POST_LOGIN_AS_SWEEP_CONTINUE_IF_SOLE_OPTION for sole-Continuar). "
    )


def adobe_actionscript_escape_wmclose_kw_from_env() -> dict[str, bool]:
    """
    Keyword args for ``dismiss_flash_error_dialogs_no_mouse`` when closing **Adobe** ActionScript
    error dialogs: Esc (keyboard, after foreground when enabled) then optional WM_CLOSE if the
    modal is still open — no localized BM_CLICK on Dismiss / Descartar.

    Shared by the post-login sweep and the Flash error poll during login
    (``dismiss_correlation_context=login_phase_poll``).
    """
    return {
        "use_wmclose_override": _env_flag_true_by_default("BOT_POST_LOGIN_AS_SWEEP_USE_WMCLOSE"),
        "adobe_escape_wmclose_only": _env_flag_true_by_default(
            "BOT_POST_LOGIN_AS_SWEEP_ADOBE_ESCAPE_WMCLOSE_ONLY"
        ),
    }


def post_login_sweep_dismiss_kw() -> dict[str, bool]:
    """
    Dismiss overrides for ``post_login_actionscript_error_sweep``.

    ``continue_if_sole_option`` applies only here; Adobe Esc / WM_CLOSE fallback flags come from
    ``adobe_actionscript_escape_wmclose_kw_from_env()`` (same as login-phase polling).

    Default: **no BM_CLICK** on Adobe ActionScript dialogs — **Esc** closes the modal when possible;
    **WM_CLOSE** only if Esc did not tear down the HWND (some builds need the fallback).

    Localized *Descartar todo* / *Dismiss all* BM_CLICK paths still collapse MAIN clean-eof in logs
    (see ``operator_phase=as_sweep``). Set ``BOT_POST_LOGIN_AS_SWEEP_ADOBE_ESCAPE_WMCLOSE_ONLY=false``
    to restore BM_CLICK ranked buttons during the sweep.
    """
    c_raw = (os.environ.get("BOT_POST_LOGIN_AS_SWEEP_CONTINUE_IF_SOLE_OPTION") or "").strip().lower()
    continue_sole = c_raw in ("1", "true", "yes", "on")

    return {
        "continue_if_sole_option": continue_sole,
        **adobe_actionscript_escape_wmclose_kw_from_env(),
    }


def dismiss_flash_error_dialogs_no_mouse(
    pid: int,
    slot_label: str,
    *,
    continue_if_sole_option: bool | None = None,
    use_wmclose_override: bool | None = None,
    adobe_escape_wmclose_only: bool | None = None,
    dismiss_correlation_context: str | None = None,
    log_operator_phase: str | None = None,
) -> int:
    """
    Dismiss Flash ActionScript/security error dialogs for *pid* **without moving
    the mouse cursor**.

    Adobe-titled ActionScript dialogs (Spanish *Error de ActionScript*, #2048, etc.)
    are closed like a manual user would: keyboard **Esc** after a best-effort
    foreground (``FLASH_ERROR_DISMISS_ESC_USE_FOREGROUND``); we only send
    ``WM_CLOSE`` if the dialog HWND stays visible afterward (see sweep flags).

    Other paths rank child ``Button`` controls and send ``BM_CLICK`` (*Dismiss All*, …),
    falling back to PostMessage Escape and optional ``FLASH_ERROR_DISMISS_USE_WMCLOSE``.

    *dismiss_correlation_context* tags the caller (e.g. ``post_login_sweep``, ``login_phase_poll``)
    for
    ``issue1_near_as_dismiss=`` on MAIN teardown. *log_operator_phase* should be
    ``get_operator_phase()`` from the CLI when possible.

    **Continue / Continuar (default: do not auto-click when Dismiss exists).**  On many
    TFM+Flash setups the only button is *Continuar*; ``BM_CLICK`` on it can end the AS
    session and drop MAIN — the same as a manual click.  Use
    ``FLASH_ERROR_DISMISS_ALLOW_CONTINUE=true`` to always prefer Continuar when it is
    ranked against Dismiss.  When the dialog offers **only** Continuar/Continue (no
    Dismiss/OK/Aceptar), ``FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION`` defaults to
    **true** so Escape-only no longer leaves a blocking modal that breaks multi-slot runs.

    The previous 120_000 px² cap skipped typical #2044/#2048 error
    windows with a large text area; those are handled by title heuristics.

    The standalone projector window is titled e.g. ``Adobe Flash Player 32`` and has no
    Win32 ``Button`` children — it must not be treated as the ActionScript error dialog.
    """
    if sys.platform != "win32" or pid <= 0:
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    BM_CLICK = 0x00F5
    WM_CLOSE = 0x0010
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    VK_ESCAPE = 0x1B
    allow_continue = _env_flag_false_by_default("FLASH_ERROR_DISMISS_ALLOW_CONTINUE")
    if use_wmclose_override is None:
        use_wmclose = _env_flag_false_by_default("FLASH_ERROR_DISMISS_USE_WMCLOSE")
    else:
        use_wmclose = bool(use_wmclose_override)
    if continue_if_sole_option is None:
        continue_if_sole = _env_flag_true_by_default("FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION")
    else:
        continue_if_sole = bool(continue_if_sole_option)
    sweep_adobe_esc_wmclose_only = (
        bool(adobe_escape_wmclose_only) if adobe_escape_wmclose_only is not None else False
    )
    DISMISS_LABELS = flash_dialog_fallback_bmclick_labels(allow_continue=allow_continue)

    def _corr(
        meth: str,
        dlg_hwnd: int,
        *,
        extra: str = "",
        incorrect_version: bool = False,
    ) -> str:
        return _dismiss_action_corr(
            dismiss_context=dismiss_correlation_context,
            operator_phase=log_operator_phase,
            pid=pid,
            meth=meth,
            dlg_hwnd=dlg_hwnd,
            extra=extra,
            incorrect_version=incorrect_version,
        )
    # ActionScript / securityError dialogs from Flash Player (large client area is normal).
    _MAX_SMALL_POPUP_AREA = 120_000
    _MAX_ADOBE_ERR_AREA = 2_500_000

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    def _is_flash_standalone_main_window_title(t: str) -> bool:
        """
        Projector top-level is typically ``Adobe Flash Player 32`` (digits = SA version).
        The official ActionScript error dialog is titled ``Adobe Flash Player`` without
        that trailing version suffix — that distinction prevented killing the main stage.
        """
        tl = (t or "").strip().lower()
        return bool(re.match(r"^adobe flash player\s+\d+\s*$", tl))

    def _is_adobe_flashplayer_error_title(title: str) -> bool:
        tl = (title or "").strip().lower()
        if _is_flash_standalone_main_window_title(title or ""):
            return False
        if "adobe flash player" in tl:
            return True
        if "flash player" in tl and "actionscript" in tl:
            return True
        if ("reproductor" in tl or "reprodutor" in tl or "lecteur" in tl or "spieler" in tl) and (
            "flash" in tl
        ):
            return True
        return False

    # Collect all visible top-level windows for this pid
    top_hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_top(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            top_hwnds.append(int(hwnd))
        return True

    user32.EnumWindows(_enum_top, 0)

    logger.debug(
        "ActionScript error dismiss: scan start slot=%s pid=%s visible_toplevel_windows=%d",
        slot_label,
        pid,
        len(top_hwnds),
    )

    def _try_escape_on_dialog(top_hwnd: int) -> None:
        """Legacy PostMessage Esc (non‑Adobe fallback when we avoid foreground tricks)."""
        user32.PostMessageW(top_hwnd, WM_KEYDOWN, VK_ESCAPE, 0)
        user32.PostMessageW(top_hwnd, WM_KEYUP, VK_ESCAPE, 0)

    clicked = 0
    for top in top_hwnds:
        # Skip minimized windows — GetClientRect returns 0×0 for them, which would
        # pass the area filter falsely and cause WM_CLOSE to be sent to a live slot.
        if user32.IsIconic(top):
            logger.debug(
                "ActionScript error dismiss: skip slot=%s hwnd=%s (minimized)",
                slot_label, top,
            )
            continue
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(top, title_buf, 512)
        title = title_buf.value or ""
        adobe_err = _is_adobe_flashplayer_error_title(title)

        rc = RECT()
        if not user32.GetClientRect(top, ctypes.byref(rc)):
            logger.debug(
                "ActionScript error dismiss: skip slot=%s hwnd=%s (no client rect)",
                slot_label, top,
            )
            continue
        area = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
        if area == 0:
            logger.debug(
                "ActionScript error dismiss: skip slot=%s hwnd=%s (zero area)",
                slot_label, top,
            )
            continue
        if not adobe_err and area >= _MAX_SMALL_POPUP_AREA:
            logger.debug(
                "ActionScript error dismiss: skip slot=%s hwnd=%s (area=%d >= %d, not Adobe-titled "
                "— likely main game window, not a small error popup)",
                slot_label, top, area, _MAX_SMALL_POPUP_AREA,
            )
            continue
        if adobe_err and area > _MAX_ADOBE_ERR_AREA:
            logger.debug(
                "ActionScript error dismiss: skip slot=%s hwnd=%s (area=%d > %d, Adobe-titled but "
                "huge — refusing WM_CLOSE for safety)",
                slot_label, top, area, _MAX_ADOBE_ERR_AREA,
            )
            continue  # main stage should not use this title; stay safe

        logger.debug(
            "ActionScript error dismiss: candidate slot=%s pid=%s hwnd=%s adobe_titled=%s area=%d title=%r",
            slot_label,
            pid,
            top,
            adobe_err,
            area,
            (title or "")[:120],
        )
        # Enumerate Button controls recursively — Adobe often nests the button row inside a panel.
        buttons: list[int] = []

        def _collect_buttons_recursive(parent_wnd: int) -> None:
            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum_child_btn(ch, _lp):
                ch_i = int(ch)
                cls_buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(ch, cls_buf, 64)
                if cls_buf.value.lower() == "button":
                    buttons.append(ch_i)
                _collect_buttons_recursive(ch_i)
                return True

            user32.EnumChildWindows(parent_wnd, _enum_child_btn, 0)

        _collect_buttons_recursive(top)

        body_text = _flash_dialog_aggregate_body_text(top, user32)

        # Flash Player privacy / Local Storage modal (Spanish "Permitir", etc.). Must run before AS-error
        # Esc handling — that title substring also matches generic adobe_err heuristics.
        if buttons and _want_flash_local_storage_permission_autoclick(
            title=title,
            body_text=body_text,
            buttons=buttons,
            user32=user32,
        ):
            allow_hwnd, allow_cap = _pick_flash_local_storage_allow_buttonhwnd(buttons, user32)
            if allow_hwnd is None:
                _bc: list[str] = []
                _bb = ctypes.create_unicode_buffer(256)
                for _bh in buttons:
                    user32.GetWindowTextW(_bh, _bb, 256)
                    _bc.append((_bb.value or "").strip())
                logger.warning(
                    "Flash Player local-storage dialog detected but no Permitir/Allow match — slot=%s "
                    "hwnd=%s title=%r captions=%r",
                    slot_label,
                    top,
                    (title or "")[:120],
                    _bc,
                )
                continue

            user32.SendMessageW(allow_hwnd, BM_CLICK, 0, 0)
            sess_tot, sess_bad = _record_as_dismiss_close(incorrect_version=False)
            _as_dismiss_log_closed(
                incorrect_version=False,
                sess_tot=sess_tot,
                fmt=(
                    "Flash local-storage permit: clicked slot=%s pid=%s dlg=%s (BM_CLICK Allow %r) "
                    "title=%r | session_total=%d incorrect_version_total=%d"
                ),
                args=(
                    slot_label,
                    pid,
                    top,
                    allow_cap,
                    (title or "")[:100],
                    sess_tot,
                    sess_bad,
                ),
            )
            clicked += 1
            record_as_dismiss_monotonic_for_slot(
                slot_label,
                detail=_corr(
                    "BM_CLICK_flash_local_storage_allow",
                    top,
                    extra=f"allow_cap={allow_cap!r}",
                ),
            )
            continue

        _maybe_log_first_seen_as_fingerprint(
            body_text=body_text,
            slot_label=slot_label,
            pid=pid,
            hwnd=top,
        )
        body_norm = body_text.lower()
        looks_like_incorrect_version = (
            "incorrect version" in body_norm
            or "wrong version" in body_norm
            or "version incorrect" in body_norm
        )

        if logger.isEnabledFor(logging.DEBUG) and buttons:
            _caps: list[str] = []
            for _b in buttons:
                _t = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(_b, _t, 256)
                _caps.append((_t.value or "").strip())
            logger.debug(
                "ActionScript error dismiss: child Button captions slot=%s hwnd=%s: %r",
                slot_label, top, _caps,
            )

        # Sweep + login-phase poll: never BM_CLICK Descartar/Dismiss/etc. Same as manual play: Esc
        # closes Adobe's modal; WM_CLOSE remains a fallback only if the HWND stays visible (#2048 / es-ES).
        if sweep_adobe_esc_wmclose_only and adobe_err and buttons:
            _try_escape_adobe_actionscript_dialog(top, user32)
            time.sleep(_flash_error_esc_post_poll_sec())
            if not _adobe_actionscript_dialog_may_remain(top, user32):
                sess_tot, sess_bad = _record_as_dismiss_close(
                    incorrect_version=looks_like_incorrect_version,
                )
                _as_dismiss_log_closed(
                    incorrect_version=looks_like_incorrect_version,
                    sess_tot=sess_tot,
                    fmt=(
                        "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s "
                        "(method=Esc keyboard — modal dismissed without WM_CLOSE) "
                        "area=%d title=%r body=%r "
                        "| session_total=%d incorrect_version_total=%d"
                    ),
                    args=(
                        slot_label,
                        pid,
                        top,
                        area,
                        (title or "")[:80],
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                        sess_tot,
                        sess_bad,
                    ),
                )
                if looks_like_incorrect_version:
                    logger.warning(
                        "Slot %s: Flash dialog reported INCORRECT GAME VERSION — re-dump "
                        "TFM_SECRETS_GAME_VERSION (and re-patch the loader SWF if stale). "
                        "incorrect_version_total_this_session=%d Body: %r",
                        slot_label,
                        sess_bad,
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                    )
                clicked += 1
                record_as_dismiss_monotonic_for_slot(
                    slot_label,
                    detail=_corr(
                        "Escape_keyboard_only_adobe_esc",
                        top,
                        incorrect_version=looks_like_incorrect_version,
                    ),
                )
                continue

            if use_wmclose:
                user32.PostMessageW(top, WM_CLOSE, 0, 0)
                sess_tot, sess_bad = _record_as_dismiss_close(
                    incorrect_version=looks_like_incorrect_version,
                )
                _as_dismiss_log_closed(
                    incorrect_version=looks_like_incorrect_version,
                    sess_tot=sess_tot,
                    fmt=(
                        "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s "
                        "(method=Esc then WM_CLOSE — modal stayed open after Esc) "
                        "area=%d title=%r body=%r "
                        "| session_total=%d incorrect_version_total=%d"
                    ),
                    args=(
                        slot_label,
                        pid,
                        top,
                        area,
                        (title or "")[:80],
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                        sess_tot,
                        sess_bad,
                    ),
                )
                if looks_like_incorrect_version:
                    logger.warning(
                        "Slot %s: Flash dialog reported INCORRECT GAME VERSION — re-dump "
                        "TFM_SECRETS_GAME_VERSION (and re-patch the loader SWF if stale). "
                        "incorrect_version_total_this_session=%d Body: %r",
                        slot_label,
                        sess_bad,
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                    )
                clicked += 1
                record_as_dismiss_monotonic_for_slot(
                    slot_label,
                    detail=_corr(
                        "Escape+WM_CLOSE_adobe_esc",
                        top,
                        incorrect_version=looks_like_incorrect_version,
                    ),
                )
            else:
                logger.warning(
                    "ActionScript error dismiss: Adobe AS slot=%s — Esc sent but HWND still visible "
                    "and BOT_POST_LOGIN_AS_SWEEP_USE_WMCLOSE=false (no WM_CLOSE fallback). hwnd=%s",
                    slot_label,
                    top,
                )
            continue

        if not buttons:
            # Main Flash stage is large and has **no** Win32 "Button" children — the SWF draws UI.
            # The real #2044 / #2048 ActionScript error dialog always exposes Dismiss/Continue as
            # child Button controls. Never WM_CLOSE here or we quit the whole projector (see log:
            # hwnd was the main "Adobe Flash Player 32" window).
            if area >= _MAX_SMALL_POPUP_AREA:
                logger.debug(
                    "ActionScript error dismiss: skip slot=%s hwnd=%s (no Win32 Button children, "
                    "area=%d — main Flash stage; ActionScript error dialogs have Button children). "
                    "title=%r",
                    slot_label, top, area, (title or "")[:100],
                )
                continue
            if adobe_err:
                _try_escape_adobe_actionscript_dialog(top, user32)
            else:
                _try_escape_on_dialog(top)
            logger.debug(
                "ActionScript error dismiss: tiny top-level slot=%s hwnd=%s (area=%d) no Buttons — "
                "%s Esc (never WM_CLOSE without BM_CLICK target). adobe_titled=%s title=%r",
                slot_label,
                top,
                area,
                "keyboard" if adobe_err else "PostMessage",
                adobe_err,
                (title or "")[:80],
            )
            continue

        best_btn: int | None = None
        best_rank = 99
        best_lbl: str = ""
        for btn in buttons:
            txt = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(btn, txt, 256)
            label_norm = _win_button_label_normalize(txt.value)
            r = _flash_error_dismiss_button_rank(label_norm)
            if r is not None and not allow_continue and r >= 3:
                # Rank 3=Continuar, 4=continue — these often end the AS session and drop MAIN
                # (same as the operator clicking "Continuar" manually).  Only auto-click if
                # FLASH_ERROR_DISMISS_ALLOW_CONTINUE is true; prefer Dismiss/OK/escape otherwise.
                r = None
            if r is not None and r < best_rank:
                best_rank = r
                best_btn = btn
                best_lbl = txt.value.strip()
            elif r is None and label_norm in DISMISS_LABELS and best_rank > 5:
                best_rank = 5
                best_btn = btn
                best_lbl = txt.value.strip()

        if best_btn is None and continue_if_sole:
            unfiltered: list[tuple[int, int, str]] = []
            for btn2 in buttons:
                t2 = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(btn2, t2, 256)
                ln2 = _win_button_label_normalize(t2.value)
                ru = _flash_error_dismiss_button_rank(ln2)
                if ru is not None:
                    unfiltered.append((btn2, ru, t2.value.strip()))
            if unfiltered and not any(ru in (0, 1, 2) for _, ru, _ in unfiltered):
                conts = [(b, ru, lab) for b, ru, lab in unfiltered if ru in (3, 4)]
                if conts:
                    conts.sort(key=lambda t: t[1])
                    bb, rrk, bll = conts[0]
                    best_btn = bb
                    best_rank = rrk
                    best_lbl = bll
                    logger.info(
                        "ActionScript error dismiss: slot=%s pid=%s hwnd=%s — only Continuar/Continue "
                        "(no Dismiss/OK); BM_CLICK (FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION) "
                        "label=%r (same as a manual click can end the AS error; usually better than a stuck dialog).",
                        slot_label, pid, top, best_lbl,
                    )

        if best_btn is not None:
            user32.SendMessageW(best_btn, BM_CLICK, 0, 0)
            sess_tot, sess_bad = _record_as_dismiss_close(
                incorrect_version=looks_like_incorrect_version,
            )
            _as_dismiss_log_closed(
                incorrect_version=looks_like_incorrect_version,
                sess_tot=sess_tot,
                fmt=(
                    "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s (method=BM_CLICK "
                    "ranked button %r rank=%s) area=%d title=%r body=%r "
                    "| session_total=%d incorrect_version_total=%d"
                ),
                args=(
                    slot_label,
                    pid,
                    top,
                    best_lbl,
                    best_rank,
                    area,
                    (title or "")[:80],
                    (body_text or "")[: _flash_dismiss_body_log_chars()],
                    sess_tot,
                    sess_bad,
                ),
            )
            if looks_like_incorrect_version:
                logger.warning(
                    "Slot %s: Flash dialog reported INCORRECT GAME VERSION — the SWF/loader's "
                    "embedded version no longer matches what the live TFM server expects. "
                    "Re-dump TFM_SECRETS_GAME_VERSION (and the loader SWF if you patched a stale "
                    "client). incorrect_version_total_this_session=%d Body: %r",
                    slot_label,
                    sess_bad,
                    (body_text or "")[: _flash_dismiss_body_log_chars()],
                )
            clicked += 1
            record_as_dismiss_monotonic_for_slot(
                slot_label,
                detail=_corr(
                    f"BM_CLICK_ranked_rank{best_rank}",
                    top,
                    extra=f"btn_cap={best_lbl!r}",
                    incorrect_version=looks_like_incorrect_version,
                ),
            )
            continue

        for btn in buttons:
            txt = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(btn, txt, 256)
            label = _win_button_label_normalize(txt.value)
            if label in DISMISS_LABELS:
                user32.SendMessageW(btn, BM_CLICK, 0, 0)
                sess_tot, sess_bad = _record_as_dismiss_close(
                    incorrect_version=looks_like_incorrect_version,
                )
                _as_dismiss_log_closed(
                    incorrect_version=looks_like_incorrect_version,
                    sess_tot=sess_tot,
                    fmt=(
                        "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s (method=BM_CLICK "
                        "exact label %r) area=%d body=%r "
                        "| session_total=%d incorrect_version_total=%d"
                    ),
                    args=(
                        slot_label,
                        pid,
                        top,
                        txt.value.strip(),
                        area,
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                        sess_tot,
                        sess_bad,
                    ),
                )
                if looks_like_incorrect_version:
                    logger.warning(
                        "Slot %s: Flash dialog reported INCORRECT GAME VERSION — re-dump "
                        "TFM_SECRETS_GAME_VERSION (and re-patch the loader SWF if stale). "
                        "incorrect_version_total_this_session=%d Body: %r",
                        slot_label,
                        sess_bad,
                        (body_text or "")[: _flash_dismiss_body_log_chars()],
                    )
                clicked += 1
                record_as_dismiss_monotonic_for_slot(
                    slot_label,
                    detail=_corr(
                        "BM_CLICK_exact",
                        top,
                        extra=f"btn_cap={txt.value.strip()!r}",
                        incorrect_version=looks_like_incorrect_version,
                    ),
                )
                break
        else:
            if adobe_err or area < _MAX_SMALL_POPUP_AREA:
                if adobe_err:
                    _try_escape_adobe_actionscript_dialog(top, user32)
                    time.sleep(_flash_error_esc_post_poll_sec())
                    closed_by_esc = not _adobe_actionscript_dialog_may_remain(top, user32)
                else:
                    _try_escape_on_dialog(top)
                    closed_by_esc = False
                if adobe_err and closed_by_esc:
                    sess_tot, sess_bad = _record_as_dismiss_close(
                        incorrect_version=looks_like_incorrect_version,
                    )
                    _as_dismiss_log_closed(
                        incorrect_version=looks_like_incorrect_version,
                        sess_tot=sess_tot,
                        fmt=(
                            "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s "
                            "(method=Esc keyboard; buttons unmatched / rank skip) "
                            "area=%d title=%r body=%r "
                            "| session_total=%d incorrect_version_total=%d"
                        ),
                        args=(
                            slot_label,
                            pid,
                            top,
                            area,
                            (title or "")[:80],
                            (body_text or "")[: _flash_dismiss_body_log_chars()],
                            sess_tot,
                            sess_bad,
                        ),
                    )
                    clicked += 1
                    record_as_dismiss_monotonic_for_slot(
                        slot_label,
                        detail=_corr(
                            "Escape_keyboard_nomatch_buttons",
                            top,
                            incorrect_version=looks_like_incorrect_version,
                        ),
                    )
                elif adobe_err and use_wmclose:
                    user32.PostMessageW(top, WM_CLOSE, 0, 0)
                    sess_tot, sess_bad = _record_as_dismiss_close(
                        incorrect_version=looks_like_incorrect_version,
                    )
                    _as_dismiss_log_closed(
                        incorrect_version=looks_like_incorrect_version,
                        sess_tot=sess_tot,
                        fmt=(
                            "ActionScript error dismiss: closed slot=%s pid=%s hwnd=%s "
                            "(method=Esc then WM_CLOSE; buttons unmatched — modal still open after Esc) "
                            "area=%d title=%r body=%r "
                            "| session_total=%d incorrect_version_total=%d"
                        ),
                        args=(
                            slot_label,
                            pid,
                            top,
                            area,
                            (title or "")[:80],
                            (body_text or "")[: _flash_dismiss_body_log_chars()],
                            sess_tot,
                            sess_bad,
                        ),
                    )
                    clicked += 1
                    record_as_dismiss_monotonic_for_slot(
                        slot_label,
                        detail=_corr(
                            "Escape+WM_CLOSE_nomatch_buttons",
                            top,
                            incorrect_version=looks_like_incorrect_version,
                        ),
                    )
                elif adobe_err and not use_wmclose:
                    logger.warning(
                        "ActionScript error dismiss: slot=%s pid=%s hwnd=%s — no safe button; "
                        "Esc left dialog visible and WM_CLOSE disabled. "
                        "(Try FLASH_ERROR_DISMISS_ALLOW_CONTINUE=true, or "
                        "FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION=true when Continue is the "
                        "sole option — default is on). area=%d title=%r",
                        slot_label,
                        pid,
                        top,
                        area,
                        (title or "")[:80],
                    )
                else:
                    logger.debug(
                        "ActionScript error dismiss: no matching button label; Escape only "
                        "slot=%s hwnd=%s (area=%d)",
                        slot_label,
                        top,
                        area,
                    )

    if clicked:
        # When only one dialog closed, the per-dialog "closed slot=... title=..."
        # line above already covers it — the summary just doubles the noise.
        # Keep INFO only for the multi-close case (rare and useful).
        if clicked > 1:
            logger.info(
                "ActionScript error dismiss: pass summary slot=%s pid=%s total_closed=%d this scan",
                slot_label, pid, clicked,
            )
        else:
            logger.debug(
                "ActionScript error dismiss: pass summary slot=%s pid=%s total_closed=%d this scan",
                slot_label, pid, clicked,
            )
    return clicked


def click_transformice_in_loader(
    pid: int,
    slot_label: str,
    *,
    frac_x: float = 0.50,
    frac_y: float = 0.55,
) -> bool:
    """
    Re-send the Transformice button click to the Flash window owned by *pid*.
    Safe to call mid-wait when no MAIN TCP accept has been seen yet.
    Returns True if the click was sent.
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    hwnd = _win_find_toplevel_hwnd(pid)
    if hwnd is None:
        logger.debug("click_transformice_in_loader: no HWND for PID %s (slot %s)", pid, slot_label)
        return False
    _win_force_foreground(hwnd)
    import time as _time
    _time.sleep(0.15)
    result = _win_click_client_fraction(hwnd, frac_x=frac_x, frac_y=frac_y, debug_label=slot_label)
    if result:
        logger.debug(
            "Slot %s: retry click sent to HWND=%s (%.2f, %.2f)",
            slot_label, hwnd, frac_x, frac_y,
        )
    return result


_FLASH_TILE_SLOT_INDEX: dict[str, int] = {}
_FLASH_TILE_LOCK = threading.Lock()

# HWNDs registered after tiling; the focus-pump thread cycles foreground across them so
# Flash's ActionScript / Anticheat keep running (background windows still throttle).
_FLASH_FOCUS_PUMP: list[tuple[int, str]] = []
_FLASH_FOCUS_PUMP_LOCK = threading.Lock()


def _try_bring_hwnd_to_foreground(hwnd: int) -> bool:
    """Best-effort SetForegroundWindow using AttachThreadInput (see Win32 Q67164 pattern)."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.IsWindow(int(hwnd)):
        return False
    if int(user32.GetForegroundWindow() or 0) == int(hwnd):
        return True
    current_tid = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = 0
    if fg:
        fg_tid = int(user32.GetWindowThreadProcessId(fg, None))
    try:
        if fg_tid and fg_tid != int(current_tid):
            user32.AttachThreadInput(fg_tid, int(current_tid), True)
        user32.BringWindowToTop(int(hwnd))
        ok = bool(user32.SetForegroundWindow(int(hwnd)))
        if fg_tid and fg_tid != int(current_tid):
            user32.AttachThreadInput(fg_tid, int(current_tid), False)
        return ok
    except OSError:
        if fg_tid and fg_tid != int(current_tid):
            try:
                user32.AttachThreadInput(fg_tid, int(current_tid), False)
            except OSError:
                pass
        return False


def register_flash_focus_pump_window(hwnd: int, slot_label: str) -> None:
    """Register a tiled Flash top-level *hwnd* for the global focus-rotation loop."""
    if sys.platform != "win32" or hwnd <= 0:
        return
    with _FLASH_FOCUS_PUMP_LOCK:
        _FLASH_FOCUS_PUMP.append((int(hwnd), str(slot_label)))


def _focus_pump_snapshot() -> list[tuple[int, str]]:
    with _FLASH_FOCUS_PUMP_LOCK:
        return list(_FLASH_FOCUS_PUMP)


def run_flash_focus_pump(*, stop: threading.Event, per_slot_ms: float) -> None:
    """
    While *stop* is not set, cycle through registered HWNDs: briefly make each
    foreground so Flash's event loop and Anticheat code run at full rate.
    """
    per = max(15.0, float(per_slot_ms)) / 1000.0
    idle = 0.05
    while not stop.is_set():
        rows = _focus_pump_snapshot()
        if not rows:
            if stop.wait(timeout=idle):
                break
            continue
        for hwnd, _label in rows:
            if stop.is_set():
                break
            if not _try_bring_hwnd_to_foreground(hwnd):
                # Fallback: nudge z-order (non-activating) — some builds accept this when
                # SetForegroundWindow is blocked by focus rules.
                try:
                    import ctypes
                    u = ctypes.windll.user32
                    if u.IsWindow(int(hwnd)):
                        u.ShowWindow(int(hwnd), 4)  # SW_SHOWNOACTIVATE
                except OSError:
                    pass
            time.sleep(per)
        if not stop.is_set():
            stop.wait(timeout=idle)


def start_flash_focus_pump_thread() -> tuple[threading.Thread, threading.Event]:
    """Start daemon *run_flash_focus_pump*; returns (thread, stop_event)."""
    raw = (os.environ.get("BOT_FLASH_FOCUS_PUMP_MS") or "").strip()
    try:
        # Default 600ms/slot: 90ms × many slots hogs the foreground and freezes out the terminal.
        per_ms = float(raw) if raw else 600.0
    except ValueError:
        per_ms = 600.0
    stop = threading.Event()
    t = threading.Thread(
        target=run_flash_focus_pump,
        kwargs={"stop": stop, "per_slot_ms": per_ms},
        daemon=True,
        name="flash-focus-pump",
    )
    t.start()
    return t, stop


def _next_tile_index(slot_label: str) -> int:
    """Allocate a stable zero-based tile index for *slot_label* (first-come-first-served)."""
    with _FLASH_TILE_LOCK:
        if slot_label in _FLASH_TILE_SLOT_INDEX:
            return _FLASH_TILE_SLOT_INDEX[slot_label]
        idx = len(_FLASH_TILE_SLOT_INDEX)
        _FLASH_TILE_SLOT_INDEX[slot_label] = idx
        return idx


def _disable_process_throttling(pid: int, slot_label: str) -> bool:
    """
    Disable Windows 10/11 background power-throttling (EcoQoS) for *pid* and
    raise it to HIGH priority.

    Windows 10 1709+ applies **process power throttling** ("EcoQoS" / Efficiency
    Mode) to any process whose windows are not in the foreground. That throttles
    CPU/timer resolution and is the *real* reason Flash's ``AnticheatPacket``
    responder goes silent 15-30 s after login even when the Flash window is
    visible and un-occluded (tiling alone doesn't rescue it — packet trails
    show a sudden buffered-event flush at -0.2 s right when ``WM_CLOSE`` wakes
    the process up).

    Fix (per MS docs, NtSetInformationProcess / ProcessPowerThrottling):

      * ``SetProcessInformation(ProcessPowerThrottling, DISABLE_THROTTLING)``
        turns EcoQoS off for this process regardless of its window state.
      * ``SetPriorityClass(HIGH_PRIORITY_CLASS)`` keeps the scheduler from
        starving Flash when the OS is under load from 14 concurrent instances.

    Both calls silently no-op on older Windows that don't support them.
    Returns True on any successful call.
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_SET_LIMITED_INFORMATION = 0x2000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    desired = PROCESS_SET_INFORMATION | PROCESS_SET_LIMITED_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION

    hproc = kernel32.OpenProcess(desired, False, int(pid))
    if not hproc:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else "?"
        logger.warning(
            "Slot %s: OpenProcess(SET_INFORMATION|SET_LIMITED_INFORMATION) failed for PID %s (err=%s) — "
            "cannot disable EcoQoS; Flash may throttle",
            slot_label, pid, err,
        )
        return False

    any_ok = False
    try:
        HIGH_PRIORITY_CLASS = 0x00000080
        if kernel32.SetPriorityClass(hproc, HIGH_PRIORITY_CLASS):
            any_ok = True
        else:
            err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else "?"
            logger.debug(
                "Slot %s: SetPriorityClass(HIGH) failed for PID %s (err=%s)",
                slot_label, pid, err,
            )

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", wintypes.ULONG),
                ("ControlMask", wintypes.ULONG),
                ("StateMask", wintypes.ULONG),
            ]

        ProcessPowerThrottling = 4
        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4

        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = (
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        )
        state.StateMask = 0

        set_info = getattr(kernel32, "SetProcessInformation", None)
        if set_info is not None:
            ok = set_info(
                hproc,
                ProcessPowerThrottling,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
            if ok:
                any_ok = True
            else:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else "?"
                logger.debug(
                    "Slot %s: SetProcessInformation(PowerThrottling=off) failed for PID %s (err=%s) — "
                    "older Windows build",
                    slot_label, pid, err,
                )
        if any_ok:
            # Rationale ("keeps Anticheat responder running...") is logged once at
            # CLI startup; per-slot just confirms the system call landed.
            logger.info(
                "Slot %s: Flash PID %s priority=HIGH, EcoQoS throttling=off",
                slot_label, pid,
            )
    finally:
        kernel32.CloseHandle(hproc)
    return any_ok


def minimize_flash_window(pid: int, slot_label: str) -> bool:
    """
    Shrink + tile the Flash Player window for *pid* to a small, non-overlapping
    cell at the top of the primary monitor.

    Background — why not SW_MINIMIZE or off-screen:

    Flash Player's standalone projector throttles its internal event loop to
    ~2 Hz whenever the OS considers the window "not visible". That trips in
    three situations:

      1. Window is minimized (``SW_MINIMIZE``).
      2. Window is moved fully off-screen (``SetWindowPos`` to negative coords).
      3. Window is **fully occluded** by another top-level window.

    When throttled Flash stops responding to server ``AnticheatPacket``
    challenges within 10–20 s, the Transformice server then kicks the TCP
    session with a clean EOF, and ``/ban`` fails with "no main connection"
    (see ``BanBotProxy._main_keepalive_loop`` / "MAIN session ended" exit logs).

    With 14 Flash windows stacked on top of each other only *one* is
    un-occluded, so the other 13 throttle — which is exactly what the logs
    showed even with ``BOT_FLASH_DIAG_KEEP_ONSCREEN=1``. Moving them off-screen
    made every single one "invisible" and all of them throttled.

    Fix: put every Flash window in its **own** tiny on-screen rectangle so no
    two overlap. Each window is visible, non-occluded, and Flash's throttling
    heuristic never fires — but the 80×60 tiles are small enough that the
    overall footprint is barely noticeable (14 tiles → 1 row of ~1120×60 pixels
    at the top of the primary display).

    ``close_flash_window`` still finds the HWND by PID and can send ``WM_CLOSE``
    normally because the window remains on-screen and visible.

    Returns True if the window was found and repositioned.
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    hwnd = _win_find_toplevel_hwnd(pid)
    if hwnd is None:
        logger.debug("minimize_flash_window: no HWND for PID %s (slot %s)", pid, slot_label)
        return False

    user32 = ctypes.windll.user32

    TILE_W = int(os.environ.get("BOT_FLASH_TILE_W", "80"))
    TILE_H = int(os.environ.get("BOT_FLASH_TILE_H", "60"))
    TILE_W = max(32, TILE_W)
    TILE_H = max(32, TILE_H)

    screen_w = int(user32.GetSystemMetrics(0)) or 1920
    screen_h = int(user32.GetSystemMetrics(1)) or 1080

    cols = max(1, screen_w // TILE_W)
    idx = _next_tile_index(str(slot_label))
    col = idx % cols
    row = idx // cols

    x = col * TILE_W
    y = row * TILE_H
    if y + TILE_H > screen_h:
        y = max(0, screen_h - TILE_H - (row * 2))

    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_ASYNCWINDOWPOS = 0x4000
    HWND_BOTTOM = 1

    ok = bool(
        user32.SetWindowPos(
            hwnd,
            HWND_BOTTOM,
            x,
            y,
            TILE_W,
            TILE_H,
            SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
        )
    )
    if not ok:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else "?"
        logger.warning(
            "Slot %s: SetWindowPos tile failed for HWND=%s (err=%s); "
            "leaving window in place — do NOT SW_MINIMIZE or move off-screen "
            "(both throttle Flash's event loop and drop MAIN TCP)",
            slot_label, hwnd, err,
        )
        return False

    # Long rationale (why we tile instead of SW_MINIMIZE / off-screen) is in the
    # docstring above; keep the per-slot log tight.
    logger.info(
        "Slot %s: tiled HWND=%s to (%d,%d) %dx%d [row=%d col=%d]",
        slot_label, hwnd, x, y, TILE_W, TILE_H, row, col,
    )
    # Tiling alone isn't enough — Windows 10+ power-throttles background
    # processes regardless of window visibility. Disable EcoQoS + raise
    # priority so Flash's Anticheat responder keeps running.
    _disable_process_throttling(pid, slot_label)
    if (os.environ.get("BOT_FLASH_FOCUS_PUMP", "1").strip().lower()
            not in ("0", "false", "no", "off")):
        register_flash_focus_pump_window(int(hwnd), slot_label)
    return True


def close_flash_window(
    pid: int,
    slot_label: str,
    *,
    grace_sec: float = 2.0,
    force_terminate: bool = True,
) -> bool:
    """
    Close the Flash projector window for *pid*.

    Tries a graceful close first (``WM_CLOSE`` to every top-level window owned by the PID),
    waits up to ``grace_sec`` for the process to exit, then (if ``force_terminate``) calls
    ``TerminateProcess`` so stale Flash tabs never linger after a failed login.

    Returns True if the process is no longer alive when the call returns.
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    WM_CLOSE = 0x0010

    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        owner_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if int(owner_pid.value) == int(pid) and user32.IsWindowVisible(hwnd):
            hwnds.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except OSError:
        pass

    for h in hwnds:
        try:
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
        except OSError:
            continue

    if hwnds:
        logger.info(
            "Slot %s: sent WM_CLOSE to %d Flash window(s) (PID %s)",
            slot_label, len(hwnds), pid,
        )

    deadline = time.monotonic() + max(0.0, grace_sec)
    while time.monotonic() < deadline:
        if not flash_pid_is_alive(pid):
            logger.info("Slot %s: Flash PID %s exited gracefully", slot_label, pid)
            return True
        time.sleep(0.1)

    if not force_terminate:
        return not flash_pid_is_alive(pid)

    PROCESS_TERMINATE = 0x0001
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        logger.warning(
            "Slot %s: could not open Flash PID %s for TerminateProcess (already gone?)",
            slot_label, pid,
        )
        return not flash_pid_is_alive(pid)
    try:
        if kernel32.TerminateProcess(h, 1):
            logger.info("Slot %s: force-terminated Flash PID %s", slot_label, pid)
        else:
            logger.warning(
                "Slot %s: TerminateProcess failed for Flash PID %s", slot_label, pid
            )
    finally:
        kernel32.CloseHandle(h)

    time.sleep(0.2)
    return not flash_pid_is_alive(pid)


def list_flash_windows(pid: int) -> list[dict]:
    """
    Enumerate every visible top-level window owned by ``pid``.

    Used as a diagnostic when a slot's Flash never opens its MAIN TCP: a secondary
    window (Error #2048 popup, Flash Player debug "Restricted content", user's
    "Continue" dialog, etc.) can steal the loader's click target, and seeing all
    top-level windows for the PID makes that obvious in the log.
    """
    if sys.platform != "win32" or pid <= 0:
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG), ("top", wintypes.LONG),
            ("right", wintypes.LONG), ("bottom", wintypes.LONG),
        ]

    results: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        owner_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if int(owner_pid.value) != int(pid):
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        rc = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            w = max(0, rc.right - rc.left)
            h = max(0, rc.bottom - rc.top)
        else:
            w = h = 0
        results.append({
            "hwnd": int(hwnd),
            "title": buf.value,
            "width": w,
            "height": h,
        })
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except OSError:
        pass
    return results


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
