"""
CMD entry: multi-slot local proxies, /room on all clients, then /ban on every live slot (burst by default).

Prerequisite: one Transformice + tfm-proxy-loader per ``proxy_port``. Row ``bind_ip`` is for
Proxifier unless ``PROXY_LISTEN_USE_ACCOUNT_BIND_IP`` is True and that IP exists on this machine.

On Windows, Flash opens **one game at a time**: after each window opens, the bot waits for that
slot's ``LoginSuccess`` before starting the next. The loader URL includes ``host``, ``port``,
``satellite``, and ``policy`` (see ``flash_launch``). A shared Flash socket-policy server runs on
port 10801 unless disabled in ``.env`` (``BOT_SHARED_FLASH_SOCKET_POLICY_PORT``). ``TFMProxyLoader.cfg`` is written and read back for
confirmation.

When ``FLASH_AUTO_LOGIN_UI`` is True in ``.env`` (or ``BOT_FLASH_AUTO_LOGIN_UI``), the bot can automate **Dismiss all** /
**Continue** and username/password entry (see ``flash_launch``). Default trigger is
``FLASH_LOGIN_TRIGGER = "main_tcp"``: after the first **MAIN TCP** accept, it dismisses dialogs ASAP,
then ``FLASH_LOGIN_AFTER_MAIN_DELAY_SEC`` for the login form, then typing. Use ``"after_launch"`` for
a fixed delay from Flash start instead. Use ``--no-flash-auto-login`` to disable.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import concurrent.futures
import json
import logging
import random
import os
import sys
from datetime import datetime
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from colorama import init as colorama_init

from .ban_proxy import (
    BanBotProxy,
    ensure_flash_trust_config,
    get_operator_phase,
    set_operator_phase,
    start_shared_flash_socket_policy_thread,
)
from . import flash_launch
from . import tfm_loader_alignment
from . import tfm_startup_refresh
from . import tfm_swf_port_patch
from .portutil import ensure_port_free_or_kill_same_bot, tcp_port_is_free
from .trace_log import asyncio_trace_install, reset_trace_session, trace_step

logger = logging.getLogger(__name__)

# caseus.Proxy defaults use main 11801, satellite 12801, policy 10801 (+1000 / -1000).
# Those defaults are shared by every slot unless overridden — only one process can bind.
# Use larger offsets so derived ports stay unique for typical proxy_port ranges; all values
# (main + satellite + policy) must be disjoint across slots (see _assign_listen_ports).
SATELLITE_PORT_OFFSET = 10_000
SOCKET_POLICY_PORT_OFFSET = 10_000
_PORT_SCAN_SPAN = 50_000
_MIN_AUX_PORT = 1024


def _pick_free_port(preferred: int, used: set[int], *, role: str, label: str) -> int:
    """Use ``preferred`` if unused and free; otherwise scan forward (Windows may occupy derived ports)."""
    start = max(_MIN_AUX_PORT, preferred)
    for p in range(start, start + _PORT_SCAN_SPAN):
        if p in used:
            continue
        if tcp_port_is_free(p):
            if p != preferred:
                logger.info(
                    "Slot %s: %s port %s was busy or reserved; using %s",
                    label,
                    role,
                    preferred,
                    p,
                )
            return p
    msg = (
        f"Slot {label}: no free {role} TCP port in [{start}, {start + _PORT_SCAN_SPAN}). "
        "Close other programs or change proxy_port values in .env (BOT_ACCOUNTS_JSON)."
    )
    logger.error(msg)
    raise SystemExit(msg)


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _load_accounts_module():
    from .env_config import load_config_from_env

    cfg = load_config_from_env(_repo_root())
    accounts = getattr(cfg, "ACCOUNTS", None)
    if not accounts:
        logger.error("ACCOUNTS is empty (set BOT_ACCOUNTS_JSON in the repo root .env).")
        raise SystemExit(1)
    return cfg


@dataclass
class SlotState:
    label: str
    port: int
    satellite_port: int = 0
    policy_port: int | None = None
    proxy_bind_host: str | None = None
    login_success_event: threading.Event = field(default_factory=threading.Event)
    # Set by BanBotProxy when a MAIN TCP session ends without LoginSuccess (handshake killed).
    # Lets login waits exit promptly instead of sleeping until BOT_RETRY_LOGIN_TIMEOUT_SEC.
    login_aborted_event: threading.Event = field(default_factory=threading.Event)
    proxy: BanBotProxy | None = None
    loop: asyncio.AbstractEventLoop | None = None
    thread: threading.Thread | None = None
    error: str | None = None
    # Set before Flash connects; used by FLASH_AUTO_LOGIN_UI (dismiss + type login).
    flash_pid: int | None = None
    flash_username: str = ""
    flash_password: str = ""
    # Set when launch_one_flash_loader finished (HWND + optional Transformice click). MAIN TCP can
    # arrive earlier than this; main_tcp auto-login waits so typing targets the real login screen.
    flash_loader_ready_event: threading.Event = field(default_factory=threading.Event)
    # Same file:///…swf URL as Flash (for optional PACKET_AUTO_LOGIN LoginPacket.loader_url).
    packet_loader_url: str = ""
    # Set when this slot's proxy accepts MAIN TCP (Flash reached the proxy).
    flash_main_tcp_seen: bool = False
    # Retry bookkeeping (used by ``_retry_partl_slots``):
    #   attempts_used    — number of relaunches we've already issued for this slot
    #                      after the initial sequential login phase. Capped by
    #                      BOT_RETRY_MAX_ATTEMPTS so a permanently flagged
    #                      account can't loop forever.
    #   bind_ip_pool     — ordered list of bind_ip values to cycle through across
    #                      retries: starts with [row['bind_ip']] + row['extra_bind_ips'].
    #                      Drained on each retry; once exhausted, retry will
    #                      borrow from the global BOT_SPARE_BIND_IPS pool (if any).
    #   current_bind_ip  — bind_ip currently associated with this slot (the value
    #                      stamped on the Flash launch line and consumed by Proxifier
    #                      rules). Initialised to row['bind_ip'].
    attempts_used: int = 0
    bind_ip_pool: list[str] = field(default_factory=list)
    current_bind_ip: str = ""


def _assign_listen_ports(
    states: list[SlotState],
    *,
    shared_flash_policy_port: int | None,
) -> None:
    """Set satellite port; Flash policy is either one shared port (TFMProxyLoader default) or per-slot."""
    used: set[int] = {s.port for s in states}
    if shared_flash_policy_port is not None:
        used.add(shared_flash_policy_port)

    for s in states:
        preferred_sat = s.port + SATELLITE_PORT_OFFSET
        s.satellite_port = _pick_free_port(preferred_sat, used, role="satellite", label=s.label)
        used.add(s.satellite_port)

        if shared_flash_policy_port is not None:
            s.policy_port = None
        else:
            preferred_pol = s.port - SOCKET_POLICY_PORT_OFFSET
            pol = _pick_free_port(preferred_pol, used, role="policy", label=s.label)
            s.policy_port = pol
            used.add(pol)

    triples = [(s.port, s.satellite_port, s.policy_port) for s in states]
    flat = [p for t in triples for p in t if p is not None]
    if len(flat) != len(set(flat)):
        dup = [p for p, n in Counter(flat).items() if n > 1]
        msg = (
            "Listen port collision after assignment (should not happen). "
            f"Duplicates: {dup!r}"
        )
        logger.error(msg)
        raise SystemExit(msg)


# When ``_CONSOLE_QUIET`` is set, the *console* (stderr) handler suppresses
# every record below ERROR. The file handler is unaffected — log.txt still
# captures everything for post-mortem. We use this around interactive
# ``input()`` calls so that asynchronous proxy chatter (e.g. WARNING "MAIN
# session ended" lines from the post-login keepalive loops) does not stomp on
# the prompt while the operator is typing a room name or nickname.
_CONSOLE_QUIET = threading.Event()


class _ConsoleQuietFilter(logging.Filter):
    """Drop sub-ERROR records from the console while ``_CONSOLE_QUIET`` is set."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if _CONSOLE_QUIET.is_set() and record.levelno < logging.ERROR:
            return False
        return True


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    debug_trace = (os.environ.get("BOT_DEBUG_TRACE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    console_debug = (os.environ.get("BOT_DEBUG_TRACE_CONSOLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    raw_level = (os.environ.get("BOT_LOG_LEVEL") or "").strip().upper()
    want_debug = debug_trace or raw_level == "DEBUG"

    fmt_verbose = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        "%H:%M:%S",
    )
    fmt_simple = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fmt = fmt_verbose if want_debug else fmt_simple

    # INFO on root: third-party libraries stay at INFO unless explicitly tweaked below.
    root.setLevel(logging.INFO)

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)
    stderr_h.setLevel(logging.DEBUG if (want_debug and console_debug) else logging.INFO)
    stderr_h.addFilter(_ConsoleQuietFilter())
    root.addHandler(stderr_h)

    log_path = _repo_root() / "log.txt"
    file_h = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    file_h.setFormatter(fmt)
    file_h.setLevel(logging.DEBUG)
    root.addHandler(file_h)

    logging.getLogger("bot").setLevel(logging.DEBUG if want_debug else logging.INFO)

    if (os.environ.get("BOT_ASYNCIO_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on"):
        logging.getLogger("asyncio").setLevel(logging.DEBUG)

    logging.info("Logging to %s", log_path)
    if debug_trace:
        logging.info(
            "BOT_DEBUG_TRACE=true — session timeline uses [trace #N | phase | slot]; "
            "full bot DEBUG goes to log.txt; stderr stays INFO unless BOT_DEBUG_TRACE_CONSOLE=true.",
        )


class _quiet_console:
    """Context manager: silence stderr log handler (file logging unaffected).

    Use around ``input()`` so async proxy WARNINGs (e.g. ``MAIN session ended``)
    don't shred the prompt. log.txt still receives every record.
    """

    def __enter__(self) -> "_quiet_console":
        _CONSOLE_QUIET.set()
        _flush_log_handlers()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _CONSOLE_QUIET.clear()
        _flush_log_handlers()


def _flush_log_handlers() -> None:
    """Ensure stderr/file log lines appear before the next ``input()`` (stdout) on Windows and pipes."""
    for h in logging.getLogger().handlers:
        s = getattr(h, "stream", None)
        if s is not None and hasattr(s, "flush"):
            try:
                s.flush()
            except OSError:
                pass
    try:
        sys.stderr.flush()
    except OSError:
        pass
    try:
        sys.stdout.flush()
    except OSError:
        pass


def _flash_login_hook_factory(state: SlotState, cfg: object):
    """Runs in a thread pool when the first MAIN TCP connection is accepted for this slot."""

    def _hook() -> None:
        try:
            # Proxy thread can accept MAIN TCP while the main thread is still in launch_one_flash_loader
            # (HWND wait / Transformice click). Wait so dismiss + paste hit the login form, not the loader.
            wait_sec = float(
                getattr(cfg, "FLASH_LOGIN_WAIT_LOADER_READY_SEC", 180.0) or 180.0
            )
            wait_sec = max(5.0, min(wait_sec, 600.0))
            if not state.flash_loader_ready_event.wait(timeout=wait_sec):
                logger.warning(
                    "Slot %s: flash_loader_ready_event timed out (%.0fs) — auto-login may hit wrong UI",
                    state.label,
                    wait_sec,
                )
            logger.info(
                "Slot %s: main_tcp hook running (flash_pid=%s)",
                state.label,
                state.flash_pid,
            )
            flash_launch.run_flash_login_ui(
                pid=state.flash_pid,
                username=state.flash_username,
                password=state.flash_password,
                slot_label=state.label,
                cfg=cfg,
                trigger="main_tcp",
            )
        except Exception:
            logger.exception("Slot %s: Flash auto-login UI failed", state.label)

    return _hook


def _run_slot_async(
    state: SlotState,
    cfg: object,
    flash_auto_login_ui: bool,
    flash_login_main_tcp_hook: bool,
) -> None:
    async def _run():
        try:
            asyncio_trace_install()
            trace_step(
                logger,
                "slot_async",
                "enter asyncio runner main_port=%s satellite=%s policy=%s auto_login_hook=%s",
                state.port,
                state.satellite_port,
                state.policy_port,
                bool(flash_auto_login_ui and flash_login_main_tcp_hook),
                slot=state.label,
            )
            hook = (
                _flash_login_hook_factory(state, cfg)
                if (flash_auto_login_ui and flash_login_main_tcp_hook)
                else None
            )

            def _on_main_tcp() -> None:
                state.flash_main_tcp_seen = True

            proxy = BanBotProxy(
                host_address=state.proxy_bind_host,
                host_main_port=state.port,
                host_satellite_port=state.satellite_port,
                host_socket_policy_port=state.policy_port,
                slot_label=state.label,
                account_bind_ip=state.current_bind_ip or "",
                login_success_event=state.login_success_event,
                login_aborted_event=state.login_aborted_event,
                on_first_main_connection=hook,
                on_main_tcp_accepted=_on_main_tcp,
                verbose_login_flow=bool(
                    getattr(cfg, "PROXY_VERBOSE_LOGIN_FLOW", True)
                ),
                log_all_main_packets=bool(
                    getattr(cfg, "PROXY_LOG_ALL_MAIN_PACKETS", False)
                ),
                packet_auto_login=bool(getattr(cfg, "PACKET_AUTO_LOGIN", False)),
                packet_login_username=state.flash_username,
                packet_login_password=state.flash_password,
                packet_login_loader_url=state.packet_loader_url,
                packet_login_delay_sec=float(
                    getattr(cfg, "PACKET_LOGIN_DELAY_SEC", 0.35) or 0.35
                ),
                packet_login_start_room=str(
                    getattr(cfg, "PACKET_LOGIN_START_ROOM", "") or ""
                ),
                main_keepalive_interval_sec=float(
                    getattr(cfg, "MAIN_KEEPALIVE_INTERVAL_SEC", 15.0) or 0.0
                ),
            )
            state.proxy = proxy
            trace_step(
                logger,
                "slot_async",
                "calling BanBotProxy.startup() main_port=%s",
                state.port,
                slot=state.label,
            )
            await proxy.startup()
            trace_step(
                logger,
                "slot_async",
                "BanBotProxy.startup() returned; calling on_start()",
                slot=state.label,
            )
            state.loop = asyncio.get_running_loop()
            await proxy.on_start()
        except Exception as e:
            state.error = str(e)
            logger.exception("Slot %s crashed", state.label)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def start_all_slots(
    states: list[SlotState],
    *,
    this_exe: Path,
    allow_kill: bool,
    cfg: object,
    flash_auto_login_ui: bool,
    flash_login_main_tcp_hook: bool,
) -> None:
    trace_step(
        logger,
        "slots",
        "start_all_slots n=%s flash_auto_login_ui=%s main_tcp_hook=%s",
        len(states),
        flash_auto_login_ui,
        flash_login_main_tcp_hook,
    )
    for s in states:
        port_roles: list[tuple[str, int]] = [
            ("main", s.port),
            ("satellite", s.satellite_port),
        ]
        if s.policy_port is not None:
            port_roles.append(("policy", s.policy_port))
        for role, p in port_roles:
            if not ensure_port_free_or_kill_same_bot(p, this_exe=this_exe, allow_kill=allow_kill):
                msg = (
                    f"{role} port {p} (slot {s.label}) is in use. "
                    "Free it or change proxy_port in .env (BOT_ACCOUNTS_JSON)."
                )
                logger.error(msg)
                raise SystemExit(msg)

    ensure_flash_trust_config()

    for s in states:
        t = threading.Thread(
            target=_run_slot_async,
            args=(s, cfg, flash_auto_login_ui, flash_login_main_tcp_hook),
            name=f"tfm-ban-{s.port}",
            daemon=True,
        )
        s.thread = t
        t.start()

    time.sleep(1.0)


def _wait_for_game_clients(
    states: list[SlotState],
    timeout_sec: float = 600.0,
    *,
    auto_flash_launched: bool = False,
) -> None:
    # If every slot already reported login success (sequential flash launch), proceed immediately.
    if all(s.login_success_event.is_set() for s in states):
        logger.info("All %d slot(s) logged in — proceeding.", len(states))
        return

    # Sequential Flash launch already did a per-slot login wait (FLASH_LOGIN_TIMEOUT_SEC,
    # default 120s) and moved on. Any slot that still isn't logged in at this point has
    # been given up on (Flash window closed when BOT_FLASH_CLOSE_ON_LOGIN_FAIL=true, or
    # st.error populated) — blocking here another 600s will never resurrect it, it just
    # spams "Still waiting: N/M slots logged in...". Log a terse summary and proceed so
    # the user can ban with the slots that did come up.
    if auto_flash_launched:
        ready = sum(1 for s in states if s.login_success_event.is_set())
        failed = [s.label for s in states if not s.login_success_event.is_set()]
        logger.info(
            "Sequential Flash launch finished: %d/%d logged in; skipping post-launch wait "
            "(failed slots %s will not recover without re-running). Continuing with ready slots.",
            ready,
            len(states),
            failed or "[none]",
        )
        return

    if not auto_flash_launched:
        logger.info(
            "Start %s game client(s); point each tfm-proxy-loader at its port from .env (BOT_ACCOUNTS_JSON).",
            len(states),
        )

    logger.info("Waiting for all %d client(s) to connect (auto-detect)...", len(states))
    _flush_log_handlers()

    deadline = time.monotonic() + timeout_sec
    next_log = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if all(s.login_success_event.is_set() for s in states):
            logger.info("All %d slot(s) logged in — proceeding.", len(states))
            return
        if all(s.proxy and (s.proxy.main_clients or getattr(s.proxy, "satellite_clients", None)) for s in states):
            logger.info("All configured slots have a connected client.")
            return
        if time.monotonic() >= next_log:
            ready = sum(1 for s in states if s.login_success_event.is_set())
            logger.info("Still waiting: %d/%d slots logged in...", ready, len(states))
            next_log = time.monotonic() + 15.0
        time.sleep(0.4)
    logger.info(
        "Timeout waiting for connections - continuing anyway (some /room or /ban may fail).",
    )


def _run_coro_on_slot(state: SlotState, coro):
    loop = state.loop
    if loop is None:
        logger.error("Slot %s: event loop not ready", state.label)
        return False
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=30)


def _slot_status_label(s: SlotState) -> tuple[str, str]:
    """
    Return (tag, human-readable reason) for a slot. Tag is one of:
      OK    — logged in AND connection alive (ready for /ban)
      PARTL — logged in but upstream connection dropped
      NO_LG — MAIN TCP accepted but LoginSuccessPacket never arrived
      NO_TCP— Flash launched but never reached the proxy (main TCP)
      CRASH — slot thread raised an exception
      DOWN  — proxy never started
    """
    if s.error:
        return ("CRASH", s.error[:60])
    if s.proxy is None:
        return ("DOWN ", "proxy not started")
    if not s.flash_main_tcp_seen:
        return ("NO_TCP", "Flash never connected to proxy")
    if not s.login_success_event.is_set():
        return ("NO_LG", "MAIN TCP ok but no LoginSuccessPacket")
    if s.proxy._main_write_conn() is None:
        # Pull the last-close summary off the proxy if available — gives
        # PARTL rows a per-slot cause (clean-eof, ConnectionResetError, ...)
        # plus how long the slot survived after login. Avoids forcing the
        # operator to scroll through the WARNING stream to figure out what
        # happened to which slot.
        reason = getattr(s.proxy, "_main_last_close_reason", None)
        since_login = getattr(s.proxy, "_main_last_close_since_login_sec", None)
        if reason and since_login is not None:
            base = f"upstream closed: {reason}@{since_login:.1f}s after login"
        elif reason:
            base = f"upstream closed: {reason}"
        else:
            base = "login ok but upstream closed"
        diag = getattr(s.proxy, "_main_last_close_diag", None) or ""
        ds = str(diag).strip()
        if ds and ds != "no_extra_pattern":
            if len(ds) > 240:
                ds = ds[:237] + "..."
            return ("PARTL", f"{base} | {ds}")
        return ("PARTL", base)
    return ("OK   ", "ready")


def _effective_flash_stagger_sec(n_slots: int, stagger_from_env: float) -> float:
    """POST-login delay before opening the *next* Flash client (sequential farm launch)."""
    stagger_after = float(stagger_from_env)
    if n_slots >= 8:
        _auto = min(7.0, 0.38 * max(0, n_slots - 1))
        stagger_after = max(stagger_after, _auto)
    if n_slots >= 12:
        stagger_after = max(stagger_after, 4.25)
    if n_slots >= 14:
        stagger_after = max(stagger_after, 5.0)
    return stagger_after


def _extract_phase_from_main_diag(diag: str | None) -> str:
    """Parse ``phase=`` from MAIN close diagnostic (grep-friendly aggregate)."""
    if not diag:
        return "?"
    for part in diag.split("|"):
        part_st = part.strip()
        if part_st.startswith("phase="):
            return part_st.split("=", 1)[1].strip() or "?"
    return "?"


def _fmt_counter_short(c: Counter, *, limit: int = 8) -> str:
    if not c:
        return "(none)"
    return ";".join(f"{k}:{v}" for k, v in c.most_common(limit))


def _partl_aggregate_line(states: list[SlotState]) -> str:
    """
    One-line histogram of **PARTL** rows: close reason × operator_phase at MAIN end.
    Compare across ``Login phase diagnostics`` vs session reports under ``logs/``.
    """
    reasons: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    n = 0
    for s in states:
        if _slot_status_label(s)[0] != "PARTL":
            continue
        n += 1
        p = s.proxy
        if p is None:
            reasons["no_proxy"] += 1
            continue
        rr = getattr(p, "_main_last_close_reason", None)
        reasons[str(rr) if rr is not None else "unknown_reason"] += 1
        diag = getattr(p, "_main_last_close_diag", None) or ""
        phases[_extract_phase_from_main_diag(diag)] += 1
    if n <= 0:
        return ""
    return (
        f"n_PARTL={n} close_reason[{_fmt_counter_short(reasons)}] "
        f"phase_at_MAIN_end[{_fmt_counter_short(phases)}]"
    )


def _last_main_close_aggregate_line(states: list[SlotState]) -> str:
    """
    Histogram over **all** slots with a proxy — for upstream-dead snapshots when every
    slot lost its writer (helps compare severity vs prior ``logs/*.md`` reports).
    """
    reasons: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    missing = 0
    for s in states:
        p = s.proxy
        if p is None:
            missing += 1
            continue
        rr = getattr(p, "_main_last_close_reason", None)
        if rr is None:
            reasons["no_logged_close_yet"] += 1
            continue
        reasons[str(rr)] += 1
        diag = getattr(p, "_main_last_close_diag", None) or ""
        phases[_extract_phase_from_main_diag(diag)] += 1
    bits: list[str] = [_fmt_counter_short(reasons), _fmt_counter_short(phases)]
    if missing:
        bits.append(f"no_proxy={missing}")
    if not reasons and missing == len(states):
        return "no proxy on any slot"
    return (
        "last_MAIN_close_reason[" + bits[0] + "] phase_at_MAIN_end[" + bits[1] + "]"
        + ((" | " + bits[2]) if len(bits) > 2 else "")
    )


def _log_login_phase_diagnostics(
    states: list[SlotState],
    *,
    n_slots: int,
    stagger_effective: float,
    stagger_from_env: float,
    phase: str = "after_login_phase",
) -> None:
    """
    Single INFO line after the slot table: PARTL/OK counts, effective stagger, loader snapshot,
    ActionScript dismiss counters (helps explain mass PARTL without scrolling WARNINGs).
    """
    partl_n = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
    ok_n = sum(1 for s in states if _slot_status_label(s)[0] == "OK   ")
    snap = flash_launch.as_error_dismiss_session_snapshot()
    al = tfm_loader_alignment.get_last_alignment_summary()
    al_bits: list[str] = []
    if al:
        al_bits.append(f"literal_version_in_swf={al.get('literal_version_bytes_in_swf_payload')}")
        ver = al.get("loader_version_embedding_verifiable")
        if ver is not None:
            al_bits.append(f"loader_embed_verifiable={ver}")
        uh = al.get("url_style_version_hints")
        if uh:
            al_bits.append(f"url_hints={uh!r}")
        if al.get("url_hints_strict_mismatch_vs_config"):
            al_bits.append("url_hint_mismatch_vs_TFM_SECRETS=yes")
        gv = al.get("tfm_secrets_game_version")
        if gv:
            al_bits.append(f"cfg_game_version={gv!r}")
    extra = " ".join(al_bits) if al_bits else "alignment=not_logged_yet"
    logger.info(
        "Login phase diagnostics [%s]: n_slots=%d OK=%d PARTL=%d | stagger_effective=%.1fs "
        "(BOT_UI_FLASH_LAUNCH_STAGGER_SEC=%.1fs; auto min for 8+ slots may apply) | "
        "AS_dialogs_closed=%d AS_unique_fp=%d incorrect_version_dialogs=%d | %s",
        phase,
        n_slots,
        ok_n,
        partl_n,
        stagger_effective,
        stagger_from_env,
        snap.get("actionscript_error_dialogs_closed", 0),
        snap.get("actionscript_error_unique_fingerprints", 0),
        snap.get("incorrect_version_dialogs", 0),
        extra,
    )
    agg = _partl_aggregate_line(states)
    if partl_n > 0 and agg:
        logger.info("Login phase PARTL aggregate [%s]: %s", phase, agg)


def _slot_status_lines(states: list[SlotState], *, title: str = "SLOT STATUS") -> list[str]:
    """Build the same table text as :func:`print_slot_status` (no I/O)."""
    banner = "=" * 110
    lines = [banner, f"  {title}", banner]
    counts = {"OK   ": 0, "PARTL": 0, "NO_LG": 0, "NO_TCP": 0, "CRASH": 0, "DOWN ": 0}
    now = time.monotonic()
    for s in states:
        tag, reason = _slot_status_label(s)
        counts[tag] = counts.get(tag, 0) + 1
        nick = "-"
        conn = "-"
        liveness = "-"
        age = ""
        proxy = s.proxy
        if proxy is not None:
            nick = (getattr(proxy, "_own_username", None) or "").strip() or "-"
            n_main = len(proxy.main_clients or [])
            n_sat = len(getattr(proxy, "satellite_clients", None) or [])
            conn = f"m={n_main} s={n_sat}"
            pm = getattr(proxy, "_auto_pong_sent_main", 0)
            ps = getattr(proxy, "_auto_pong_sent_satellite", 0)
            ka = getattr(proxy, "_keepalive_sent_main", 0)
            liveness = f"pong={pm}/{ps} ka={ka}"
            login_mono = getattr(proxy, "_login_success_mono", None)
            if login_mono is not None:
                age = f"{now - login_mono:5.1f}s"
        mark = "[  OK  ]" if tag == "OK   " else "[ FAIL ]"
        lines.append(
            f"  {mark} slot {s.label:>3s} [{tag}] "
            f"{(s.flash_username or '-'):<24s} nick={nick:<20s} port={s.port:<5d} "
            f"conn={conn:<7s} {liveness:<16s} login_age={age:<6s} {reason}"
        )
    lines.append(banner)
    summary = " | ".join(f"{k.strip()}={v}" for k, v in counts.items() if v)
    lines.append(f"  Totals: {summary}  (of {len(states)} slot(s))")
    lines.append(banner)
    nicks: list[str] = []
    for s in states:
        proxy = s.proxy
        n = (getattr(proxy, "_own_username", None) or "").strip() if proxy else ""
        if n:
            nicks.append(f"slot{s.label}={n}")
    if nicks:
        lines.append("  (Quick ref) " + " | ".join(nicks))
    return lines


def _session_report_enabled() -> bool:
    v = (os.environ.get("BOT_SESSION_REPORT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _write_session_report_markdown(
    *,
    repo: Path,
    states: list[SlotState],
    cfg: object,
    ban_rounds: list[dict[str, object]],
    session_started_wall: float,
    session_exit_reason: str,
    flash_auto_launched: bool,
    log_txt_path: Path,
) -> Path | None:
    """Append a timestamped session summary under ``logs/`` (markdown)."""
    if not _session_report_enabled():
        return None
    logs_dir = repo / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Session report: cannot create logs directory %s (%s)", logs_dir, e)
        return None
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = logs_dir / f"session_report_{ts}.md"
    ended = time.time()
    duration_s = max(0.0, ended - session_started_wall)
    start_str = datetime.fromtimestamp(session_started_wall).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.fromtimestamp(ended).strftime("%Y-%m-%d %H:%M:%S")
    ok_now = sum(1 for s in states if _slot_status_label(s)[0] == "OK   ")
    partl_now = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
    lines: list[str] = [
        "# Ban bot session report\n",
        "\n",
        f"- **Started (local)**: {start_str}\n",
        f"- **Ended (local)**: {end_str}\n",
        f"- **Duration**: {duration_s:.1f}s\n",
        f"- **Exit reason**: {session_exit_reason}\n",
        f"- **Slots configured**: {len(states)}\n",
        f"- **Slots OK at report time**: {ok_now}\n",
        f"- **Slots PARTL at report time**: {partl_now}\n",
        f"- **Flash auto-launch**: {'yes' if flash_auto_launched else 'no'}\n",
        f"- **Full trace log**: `{log_txt_path}`\n",
        "\n",
        "## Configuration snapshot\n",
        "\n",
    ]
    for key in (
        "PACKET_AUTO_LOGIN",
        "PROXY_LISTEN_USE_ACCOUNT_BIND_IP",
        "BAN_BURST_MODE",
        "PLAYER_LIST_JOIN_LEADER_ONLY",
        "PRE_BAN_ROOM_JOIN_STAGGER_SEC",
        "FLASH_STAGGER_AFTER_LOGIN_SEC",
        "UPSTREAM_WAIT_SEC",
        "BAN_QUORUM_REPORTS",
    ):
        try:
            lines.append(f"- **{key}**: `{getattr(cfg, key, '—')}`\n")
        except Exception:
            lines.append(f"- **{key}**: (unavailable)\n")
    _al_md = tfm_loader_alignment.get_last_alignment_report_md()
    if _al_md:
        lines.extend(_al_md)
    else:
        lines.extend(
            [
                "## Client / loader alignment\n\n",
                "*Not captured (non-Windows host, or Flash/SWF files missing at startup).*\n\n",
            ]
        )
    _as_snap = flash_launch.as_error_dismiss_session_snapshot()
    lines.append("## ActionScript error dismiss (session totals)\n\n")
    lines.append(f"- **Dialogs closed (tracked)**: `{_as_snap['actionscript_error_dialogs_closed']}`\n")
    lines.append(
        f"- **Distinct ActionScript dialog bodies (fingerprints)**: `{_as_snap.get('actionscript_error_unique_fingerprints', 0)}`\n"
    )
    lines.append(
        f"- **Incorrect-version dialog bodies (tracked)**: `{_as_snap['incorrect_version_dialogs']}`\n\n"
    )
    agg_end = _partl_aggregate_line(states)
    lines.append("## PARTL / MAIN close aggregate (report time)\n\n")
    if agg_end:
        lines.append(f"- **Histogram**: `{agg_end}`\n")
        lines.append(
            "- **`close_reason`**: last MAIN TCP teardown label from the proxy; dominant **`clean-eof`** matches "
            "sessions in `logs/*` reports (stagger/AS/load).\n"
        )
        lines.append(
            "- `phase_at_MAIN_end`: `get_operator_phase()` when MAIN closed (**`as_sweep`** correlates "
            "with ActionScript dismiss pass in some sessions — compare `logs/` session reports).\n\n"
        )
    else:
        lines.append("*No PARTL slots at report time.*\n\n")

    snap_line = _last_main_close_aggregate_line(states)
    if snap_line:
        lines.append(f"## Last MAIN close — all slots (histogram)\n\n- `{snap_line}`\n\n")

    lines.append("\n## Ban rounds\n\n")
    if not ban_rounds:
        lines.append("*(no ban rounds completed this session)*\n\n")
    else:
        lines.append(
            "| # | Target | OK | Send failed | Skipped | Live @ send | Round s | Quorum |\n"
            "|---|--------|----|-------------|---------|-------------|---------|--------|\n"
        )
        for i, br in enumerate(ban_rounds, 1):
            q_rep = br.get("quorum_reports", 11)
            q_ok = br.get("quorum_met", False)
            q_cell = f"{'met' if q_ok else 'not met'} ({q_rep} typical)"
            lines.append(
                f"| {i} | `{br.get('target_user', '')}` | {br.get('ok_count', '')} | "
                f"{br.get('fail_send', '')} | {br.get('skip_count', '')} | "
                f"{br.get('live_slots_at_send', '')} | "
                f"{float(br.get('round_seconds', 0) or 0):.1f} | {q_cell} |\n"
            )
        lines.append("\n")
    lines.append("## Final slot status\n\n")
    lines.append("```text\n")
    lines.extend(line + "\n" for line in _slot_status_lines(states, title="SLOT STATUS AT SESSION END"))
    lines.append("```\n")
    lines.append(
        "\n*Session reports go under `logs/session_report_*.md`. "
        "Set `BOT_SESSION_REPORT=0` to disable.*\n"
    )
    try:
        path.write_text("".join(lines), encoding="utf-8")
    except OSError as e:
        logger.warning("Session report: write failed %s (%s)", path, e)
        return None
    return path


def print_slot_status(states: list[SlotState], *, title: str = "SLOT STATUS") -> None:
    """
    Print a clearly-formatted per-slot status table to stdout *and* the log so
    the user can immediately see which slots are not working.
    """
    lines = _slot_status_lines(states, title=title)
    for line in lines:
        print(line, flush=True)
        logger.info(line)
    _flush_log_handlers()


def _taskkill_all_flash_windows(reason: str = "shutdown") -> None:
    """
    Windows-only nuclear fallback: ``taskkill /F /FI "WINDOWTITLE eq Adobe Flash Player*"``.

    Closes every visible Adobe Flash Player projector window in one shot, regardless
    of which PID owns it. Used as a safety net when:
      * the per-slot WM_CLOSE/TerminateProcess walk in :func:`close_all_flash_windows`
        leaves stragglers (Flash projector occasionally ignores WM_CLOSE during a
        modal "Adobe Flash Player" error dialog), or
      * the operator hits Ctrl-C a second time while cleanup is still mid-flight
        and we never want them to be staring at 14 zombie projector windows.

    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        return
    import subprocess
    cmd = ['taskkill', '/F', '/FI', 'WINDOWTITLE eq Adobe Flash Player*']
    try:
        # Hide the taskkill child console. Do NOT use capture_output/Pipe stdout+stderr:
        # subprocess.communicate() spawns helper threads and can raise RuntimeError
        # ("can't create new thread") during interpreter shutdown. DEVNULL avoids pipes.
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cp = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
            startupinfo=startupinfo,
        )
        if cp.returncode == 0:
            logger.info(
                "Flash cleanup: taskkill 'WINDOWTITLE eq Adobe Flash Player*' OK (%s).",
                reason,
            )
        elif cp.returncode == 128:
            # ERROR: The search filter cannot be recognized — happens when zero matching
            # processes exist. Treat as success: nothing to clean.
            logger.info(
                "Flash cleanup: taskkill found no Adobe Flash Player windows (%s).",
                reason,
            )
        else:
            logger.info(
                "Flash cleanup: taskkill rc=%s (%s).",
                cp.returncode, reason,
            )
    except FileNotFoundError:
        logger.debug("taskkill not on PATH; skipping nuclear Flash shutdown cleanup.")
    except subprocess.TimeoutExpired:
        logger.warning("Flash cleanup: taskkill timed out (%s)", reason)
    except RuntimeError as e:
        msg = str(e).lower()
        if (
            "can't create new thread" in msg
            or "can't start new thread" in msg
            or "interpreter shutdown" in msg
        ):
            logger.warning(
                "Flash cleanup: taskkill skipped (%s) — interpreter shutdown: %s",
                reason,
                e,
            )
        else:
            raise
    except Exception:
        logger.exception("Flash cleanup: taskkill raised during shutdown")


def close_all_flash_windows(states: list[SlotState], cfg: object | None = None) -> None:
    """
    Close every Flash projector window this bot session launched.

    Called from the ``main()`` ``finally`` block so the user never has to
    hand-close 14 Flash windows after the bot finishes (or crashes). Mirrors
    the per-slot close used when a login times out: WM_CLOSE first, then
    ``TerminateProcess`` after ``FLASH_CLOSE_GRACE_SEC`` if needed.

    A second Ctrl-C during the per-slot walk falls through to the
    ``taskkill /F /FI "WINDOWTITLE eq Adobe Flash Player*"`` fallback at the
    end so the operator never ends up with zombie projector windows even when
    they impatient-cancel cleanup.
    """
    grace = float(getattr(cfg, "FLASH_CLOSE_GRACE_SEC", 2.0)) if cfg is not None else 2.0
    live = [s for s in states if s.flash_pid and flash_launch.flash_pid_is_alive(s.flash_pid)]
    if not live:
        logger.info("Flash cleanup: no live Flash processes to close.")
        # Still run the nuclear fallback in case stale Flash windows from a previous
        # crashed run linger (no PID known to us, but the WINDOWTITLE filter still
        # nukes them). Cheap on Windows; no-op elsewhere.
        _taskkill_all_flash_windows(reason="no live PIDs known")
        _flush_log_handlers()
        return
    logger.info("Flash cleanup: closing %d Flash window(s) for slots %s...",
                len(live), [s.label for s in live])
    interrupted = False
    for s in live:
        pid = s.flash_pid
        try:
            flash_launch.close_flash_window(pid, s.label, grace_sec=grace)
        except KeyboardInterrupt:
            # Operator slammed Ctrl-C again — bail out of the per-slot walk and
            # go straight to taskkill below so they aren't waiting on per-PID grace.
            logger.info(
                "Flash cleanup: second Ctrl-C — skipping remaining per-slot closes "
                "and falling through to taskkill nuke."
            )
            interrupted = True
            break
        except Exception:
            logger.exception("Slot %s: close_flash_window raised during shutdown", s.label)
        s.flash_pid = None
    # Always run the nuclear fallback at the end. If the per-slot walk succeeded,
    # taskkill returns "no Adobe Flash Player windows" (rc=128) and we just log
    # that. If anything was left behind — modal error dialog blocking WM_CLOSE,
    # second-Ctrl-C abort, projector hung in DwmFlush — taskkill kills it dead.
    _taskkill_all_flash_windows(
        reason="post per-slot cleanup" if not interrupted else "Ctrl-C during cleanup",
    )
    logger.info("Flash cleanup: done.")
    _flush_log_handlers()


# --------------------------------------------------------------------------- #
# PARTL retry — relaunch failed Flash tabs (optionally with a different bind_ip)
# --------------------------------------------------------------------------- #
#
# After the initial sequential login phase, ``_retry_partl_slots`` walks every
# slot whose status is PARTL (logged in but upstream connection dropped) and
# attempts to bring it back: close the dead Flash tab, reset proxy session
# state, optionally swap to the next bind_ip from the slot's pool / the global
# spare pool, and relaunch Flash. Up to BOT_RETRY_MAX_ATTEMPTS attempts per
# slot, with BOT_RETRY_DELAY_SEC between the close and the relaunch.
#
# IMPORTANT: bind_ip rotation only changes the WAN exit IP if Proxifier (or the
# multi-NIC bind path enabled by PROXY_LISTEN_USE_ACCOUNT_BIND_IP=true) is
# wired to map each bind_ip to a distinct upstream egress. Otherwise rotating
# bind_ip is just relabelling — useful for diagnostics, but the server still
# sees the same public IP and is likely to kick again. The retry without a
# rotation is still worth running because it can recover from transient kicks
# (server-side rate-limit windows, missed first-pong races) where the same IP
# does work on the second try.
# --------------------------------------------------------------------------- #


_GLOBAL_SPARE_BIND_IPS: list[str] | None = None


def _load_global_spare_bind_ips() -> list[str]:
    """
    Parse ``BOT_SPARE_BIND_IPS`` once into an ordered list of unique IPs.
    Format: comma- or whitespace-separated. Drained as PARTL retries borrow.
    """
    global _GLOBAL_SPARE_BIND_IPS
    if _GLOBAL_SPARE_BIND_IPS is not None:
        return _GLOBAL_SPARE_BIND_IPS
    raw = (os.environ.get("BOT_SPARE_BIND_IPS") or "").strip()
    if not raw:
        _GLOBAL_SPARE_BIND_IPS = []
        return _GLOBAL_SPARE_BIND_IPS
    parts = [p.strip() for p in raw.replace(";", ",").replace(" ", ",").split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    _GLOBAL_SPARE_BIND_IPS = out
    if out:
        logger.info(
            "Retry: %d spare bind_ip(s) available from BOT_SPARE_BIND_IPS pool: %s",
            len(out), out,
        )
    return _GLOBAL_SPARE_BIND_IPS


def _pick_next_bind_ip_for_retry(st: SlotState, used_ips: set[str]) -> str | None:
    """
    Return the next bind_ip the slot should use on its next retry, or ``None``
    if no fresh IP is available (caller may still relaunch with the current IP).

    Resolution order:
      1. Slot's own ``bind_ip_pool`` (from row['bind_ip'] + row['extra_bind_ips'])
      2. Global ``BOT_SPARE_BIND_IPS`` pool (drained per borrow)

    ``used_ips`` is updated in place so the global pool isn't double-issued
    across slots within the same retry pass.
    """
    while st.bind_ip_pool:
        cand = st.bind_ip_pool.pop(0)
        if cand and cand != st.current_bind_ip and cand not in used_ips:
            used_ips.add(cand)
            return cand
    spares = _load_global_spare_bind_ips()
    while spares:
        cand = spares.pop(0)
        if cand and cand != st.current_bind_ip and cand not in used_ips:
            used_ips.add(cand)
            return cand
    return None


def _reset_slot_state_for_retry(st: SlotState) -> None:
    """
    Wipe per-session events on the slot and per-MAIN-session counters on the
    underlying proxy so a fresh MAIN TCP accept looks like a brand-new session
    instead of being interpreted as a reconnect.
    """
    st.login_success_event.clear()
    st.login_aborted_event.clear()
    st.flash_main_tcp_seen = False
    st.flash_loader_ready_event.clear()
    st.error = None
    proxy = st.proxy
    if proxy is None:
        return
    # Reset proxy-side per-MAIN-session flags on the asyncio loop (thread-safe). Required
    # for PACKET_AUTO_LOGIN (_packet_login_sent) and for FLASH main_tcp UI hook
    # (_first_main_hook_done) so a relaunched Flash session is not treated as a duplicate.
    loop = st.loop
    if loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                proxy.reset_packet_auto_login_for_reconnect(), loop
            )
            fut.result(timeout=5.0)
        except Exception:
            logger.debug(
                "Slot %s: reset_packet_auto_login_for_reconnect failed (falling back to in-place clear)",
                st.label, exc_info=True,
            )
            try:
                t = getattr(proxy, "_packet_login_task", None)
                if t is not None and not t.done():
                    t.cancel()
                proxy._packet_login_task = None
                proxy._packet_login_sent = False
                proxy._handshake_auth_token = None
                proxy._main_handshake_mono = None
                proxy._first_main_hook_done = False
            except Exception:
                pass
    # Session-level fields populated by ``new_main_connection``'s finally
    # block. Clearing them means the post-retry status table won't display
    # the previous session's close reason once a fresh MAIN session opens.
    try:
        proxy._login_success_mono = None
        proxy._main_tcp_mono = None
        proxy._main_last_close_reason = None
        proxy._main_last_close_alive_sec = None
        proxy._main_last_close_since_login_sec = None
        proxy._main_last_close_diag = None
        proxy._sat_redirect_logged = False
        proxy._main_recent_from_server.clear()
        proxy._main_recent_from_client.clear()
        # Counters: zero per-session pong / keepalive numbers so the post-retry
        # status table reports the *new* session's liveness instead of the
        # stale figures from the kicked session.
        proxy._auto_pong_sent_main = 0
        proxy._auto_pong_sent_satellite = 0
        proxy._keepalive_sent_main = 0
        # ``_main_keepalive_started`` is sticky — once set, ``_start_main_keepalive``
        # short-circuits even if the previous keepalive task exited (its
        # ``_main_keepalive_loop`` returns when MAIN upstream is gone). Clear
        # it so the next LoginSuccess on the new MAIN session restarts the
        # heartbeat instead of running pong-only.
        proxy._main_keepalive_started = False
        proxy._main_keepalive_task = None
    except Exception:
        logger.debug("Slot %s: proxy state reset hit unexpected attr", st.label, exc_info=True)


def _close_flash_for_slot_retry(st: SlotState, cfg: object) -> None:
    """Graceful close of the slot's Flash window before relaunch (no-op if dead)."""
    pid = st.flash_pid
    if not pid:
        return
    try:
        if not flash_launch.flash_pid_is_alive(pid):
            st.flash_pid = None
            return
    except Exception:
        # If the liveness probe blows up, still try to close so we don't
        # leave a zombie behind on retry — close_flash_window is itself
        # defensive about a missing PID.
        pass
    grace = float(getattr(cfg, "FLASH_CLOSE_GRACE_SEC", 2.0))
    try:
        flash_launch.close_flash_window(pid, st.label, grace_sec=grace)
    except Exception:
        logger.exception("Slot %s: close_flash_window raised during retry", st.label)
    st.flash_pid = None


def _build_flash_row_for_retry(
    *,
    raw_row: dict,
    st: SlotState,
    shared_flash_policy_port: int | None,
) -> dict:
    """Reproduce the per-slot ``flash_row`` shape that the initial launch loop builds."""
    flash_row = dict(raw_row)
    flash_row["_flash_satellite_port"] = st.satellite_port
    flash_row["_flash_policy_port"] = (
        shared_flash_policy_port
        if shared_flash_policy_port is not None
        else st.policy_port
    )
    flash_row["_flash_connect_host"] = st.proxy_bind_host if st.proxy_bind_host else "127.0.0.1"
    if st.current_bind_ip:
        flash_row["bind_ip"] = st.current_bind_ip
    return flash_row


def _wait_for_login_simple(st: SlotState, login_timeout: float) -> bool:
    """Poll for ``login_success_event`` up to ``login_timeout`` seconds with periodic INFO."""
    deadline = time.monotonic() + login_timeout
    next_log = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        # Wait in small slices so the periodic-log clock stays accurate.
        if st.login_success_event.wait(timeout=1.0):
            return True
        if st.login_aborted_event.is_set():
            logger.info(
                "Retry: slot %s — MAIN session ended before LoginSuccess; stopping wait "
                "(see MAIN session ended WARNING above).",
                st.label,
            )
            return False
        now = time.monotonic()
        if now >= next_log:
            left = max(0.0, deadline - now)
            logger.info(
                "Retry: slot %s still waiting for login (~%.0fs left, MAIN_seen=%s, aborted=%s)",
                st.label,
                left,
                "yes" if st.flash_main_tcp_seen else "no",
                "yes" if st.login_aborted_event.is_set() else "no",
            )
            next_log = now + 15.0
    return False


def _retry_partl_slots(
    states: list[SlotState],
    raw_accounts: list[dict],
    cfg: object,
    args,
    *,
    shared_flash_policy_port: int | None,
) -> None:
    """
    Walk PARTL slots after the initial login phase and attempt to bring each
    one back online by closing its Flash tab and relaunching (optionally with
    a different bind_ip). See module-level docstring above for the wiring
    needed for bind_ip rotation to actually change the WAN exit IP.
    """
    if not _env_bool("BOT_RETRY_FAILED_SLOTS", default=True):
        logger.info(
            "Retry: BOT_RETRY_FAILED_SLOTS=false — skipping PARTL relaunch pass.",
        )
        return
    try:
        max_attempts = int(os.environ.get("BOT_RETRY_MAX_ATTEMPTS", "3"))
    except ValueError:
        max_attempts = 3
    max_attempts = max(0, min(max_attempts, 10))
    if max_attempts == 0:
        logger.info("Retry: BOT_RETRY_MAX_ATTEMPTS=0 — disabled.")
        return
    try:
        retry_delay = float(os.environ.get("BOT_RETRY_DELAY_SEC", "3.0"))
    except ValueError:
        retry_delay = 3.0
    retry_delay = max(0.0, min(retry_delay, 30.0))
    try:
        retry_login_timeout = float(os.environ.get("BOT_RETRY_LOGIN_TIMEOUT_SEC", "120.0"))
    except ValueError:
        retry_login_timeout = 120.0
    retry_login_timeout = max(15.0, min(retry_login_timeout, 600.0))
    try:
        between_slot = float(getattr(cfg, "RETRY_BETWEEN_SLOT_SEC", 2.0) or 0.0)
    except (TypeError, ValueError):
        between_slot = 2.0
    between_slot = max(0.0, min(30.0, between_slot))
    if between_slot > 0:
        logger.info(
            "Retry: %.1fs between each PARTL relaunch in a round (BOT_RETRY_BETWEEN_SLOT_SEC) — "
            "reduces Flash/CPU stampede on slots that are still OK.",
            between_slot,
        )

    set_operator_phase("partl_retry")
    try:
        # Map slot label -> raw account row so we can rebuild flash_row each attempt.
        row_by_label: dict[str, dict] = {}
        for row in raw_accounts:
            lbl = str(row.get("label", "") or "").strip()
            if lbl:
                row_by_label[lbl] = row

        spares = _load_global_spare_bind_ips()
        used_ips_global: set[str] = {
            s.current_bind_ip for s in states if s.current_bind_ip
        }
        # Track which spare IPs have been issued so the same one doesn't get
        # handed to two failing slots in the same pass.
        used_spares: set[str] = set()

        for round_num in range(1, max_attempts + 1):
            partl_slots = [s for s in states if _slot_status_label(s)[0] == "PARTL"]
            if not partl_slots:
                logger.info("Retry: no PARTL slot remaining — relaunch loop done.")
                return
            eligible = [s for s in partl_slots if s.attempts_used < max_attempts]
            if not eligible:
                logger.info(
                    "Retry: round %d — %d PARTL slot(s) remain but all hit BOT_RETRY_MAX_ATTEMPTS=%d; giving up.",
                    round_num, len(partl_slots), max_attempts,
                )
                break
            logger.info(
                "Retry round %d/%d: relaunching %d PARTL slot(s) %s%s",
                round_num,
                max_attempts,
                len(eligible),
                [s.label for s in eligible],
                f" (global spares left: {len(spares)})" if spares else "",
            )

            for ei, st in enumerate(eligible):
                row = row_by_label.get(st.label)
                if row is None:
                    logger.warning(
                        "Retry: slot %s has no matching account row — skipping.", st.label,
                    )
                    continue

                new_ip = _pick_next_bind_ip_for_retry(st, used_spares | used_ips_global)
                if new_ip:
                    logger.info(
                        "Retry: slot %s switching bind_ip %r -> %r (attempt %d/%d)",
                        st.label, st.current_bind_ip or "(none)", new_ip,
                        st.attempts_used + 1, max_attempts,
                    )
                    # Free the old IP so it can be reused by a later round; keep
                    # the new one out of the global eligible set for the rest of
                    # this pass.
                    used_ips_global.discard(st.current_bind_ip)
                    st.current_bind_ip = new_ip
                    used_ips_global.add(new_ip)
                else:
                    logger.info(
                        "Retry: slot %s no fresh bind_ip available — relaunching with current %r (attempt %d/%d)",
                        st.label, st.current_bind_ip or "(none)",
                        st.attempts_used + 1, max_attempts,
                    )

                _close_flash_for_slot_retry(st, cfg)
                _reset_slot_state_for_retry(st)
                if retry_delay > 0:
                    time.sleep(retry_delay)

                flash_row = _build_flash_row_for_retry(
                    raw_row=row, st=st,
                    shared_flash_policy_port=shared_flash_policy_port,
                )
                try:
                    proc = flash_launch.launch_one_flash_loader(
                        flash_row,
                        root=_repo_root(),
                        click_transformice=not args.launch_flash_no_click,
                        on_flash_pid=lambda pid, _st=st: setattr(_st, "flash_pid", pid),
                        post_open_delay_sec=float(
                            getattr(cfg, "FLASH_LOADER_POST_OPEN_DELAY_SEC", 1.15)
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Retry: slot %s — launch_one_flash_loader raised; skipping this attempt.",
                        st.label,
                    )
                    st.attempts_used += 1
                    continue

                if proc is None:
                    logger.warning(
                        "Retry: slot %s — Flash launcher returned no process; counting attempt and moving on.",
                        st.label,
                    )
                    st.attempts_used += 1
                    continue

                st.flash_loader_ready_event.set()
                ok = _wait_for_login_simple(st, retry_login_timeout)
                st.attempts_used += 1
                if ok:
                    tag, _ = _slot_status_label(st)
                    logger.info(
                        "Retry: slot %s — relaunch login %s (status=%s, attempt %d/%d)",
                        st.label,
                        "succeeded" if tag == "OK   " else "succeeded (still PARTL — server kicked again)",
                        tag.strip(), st.attempts_used, max_attempts,
                    )
                else:
                    logger.warning(
                        "Retry: slot %s — no LoginSuccess within %.0fs (attempt %d/%d).",
                        st.label, retry_login_timeout, st.attempts_used, max_attempts,
                    )
                    # Close the Flash window we just opened so the next round (or
                    # the caller's eventual cleanup) starts from a clean slate.
                    _close_flash_for_slot_retry(st, cfg)
                if between_slot > 0 and ei + 1 < len(eligible):
                    time.sleep(between_slot)
    finally:
        set_operator_phase("idle")

    print_slot_status(states, title="SLOT STATUS AFTER RETRY")
    try:
        _sf_retry = float(getattr(cfg, "FLASH_STAGGER_AFTER_LOGIN_SEC", 0.5))
        _log_login_phase_diagnostics(
            states,
            n_slots=len(states),
            stagger_effective=_effective_flash_stagger_sec(len(states), _sf_retry),
            stagger_from_env=_sf_retry,
            phase="after_partl_retry",
        )
    except Exception:
        logger.debug("PARTL retry diagnostics log failed", exc_info=True)


def _env_bool(name: str, *, default: bool) -> bool:
    """Tiny helper: read ``name`` from env, accepting common true/false spellings."""
    raw = (os.environ.get(name, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _live_write_slots(states: list[SlotState]) -> list[SlotState]:
    """Slots whose proxy has a usable upstream writer (``BanBotProxy._main_write_conn()``: MAIN or satellite)."""
    return [s for s in states if s.proxy and s.proxy._main_write_conn() is not None]


def _log_upstream_dead_snapshot(states: list[SlotState], purpose: str) -> None:
    """
    When no slot has an upstream writer, log each proxy's last MAIN close (or missing proxy).

    Makes ``0/%d slots with write path`` post-mortems self-contained in ``log.txt``.
    """
    if not states:
        return
    logger.warning(
        "Upstream dead snapshot (%s) — %d slot(s); last-known MAIN state per slot:",
        purpose,
        len(states),
    )
    agg = _last_main_close_aggregate_line(states)
    if agg:
        logger.warning("Upstream dead snapshot aggregate (%s): %s", purpose, agg)
        ce = sum(
            1
            for s in states
            if s.proxy and getattr(s.proxy, "_main_last_close_reason", None) == "clean-eof"
        )
        if ce >= max(3, len(states) // 2):
            logger.warning(
                "Upstream hint (%s): %d/%s last closes are clean-eof — typical mix: stagger/AS/load "
                "during login (**FIX_TFM_LOADER** / raise BOT_UI_FLASH_LAUNCH_STAGGER_SEC); "
                "BOT_PRE_BAN_* only affects room phase.",
                purpose,
                ce,
                len(states),
            )
    for s in states:
        p = s.proxy
        if p is None:
            logger.warning("  slot %s: no proxy", s.label)
            continue
        wr = p._main_write_conn()
        if wr is not None:
            logger.warning("  slot %s: has write path (unexpected during dead snapshot)", s.label)
            continue
        reason = getattr(p, "_main_last_close_reason", None) or "?"
        since = getattr(p, "_main_last_close_since_login_sec", None)
        alive = getattr(p, "_main_last_close_alive_sec", None)
        diag = getattr(p, "_main_last_close_diag", None) or ""
        if len(diag) > 200:
            diag = diag[:197] + "..."
        post = f"since_login={since:.1f}s" if isinstance(since, (int, float)) else "since_login=n/a"
        life = f"alive={alive:.1f}s" if isinstance(alive, (int, float)) else "alive=n/a"
        logger.warning(
            "  slot %s: reason=%s | %s | %s | diag=%s",
            s.label,
            reason,
            post,
            life,
            diag,
        )


def _upstream_wait_sec(cfg: object | None) -> float:
    """``BOT_UPSTREAM_WAIT_SEC`` / :attr:`UPSTREAM_WAIT_SEC` — clamped; 0 disables waiting."""
    if cfg is not None:
        try:
            v = float(getattr(cfg, "UPSTREAM_WAIT_SEC", 20.0) or 20.0)
        except (TypeError, ValueError):
            v = 20.0
        return max(0.0, min(300.0, v))
    raw = (os.environ.get("BOT_UPSTREAM_WAIT_SEC") or "").strip()
    if raw:
        try:
            return max(0.0, min(300.0, float(raw)))
        except ValueError:
            pass
    return 20.0


def _wait_for_upstream_slots(
    states: list[SlotState],
    *,
    max_wait_sec: float,
    purpose: str,
) -> list[SlotState]:
    """
    Return slots with a live write path, optionally blocking until at least one appears or *max_wait_sec* elapses.
    """
    live = _live_write_slots(states)
    if live:
        return live
    if max_wait_sec <= 0:
        if states:
            _log_upstream_dead_snapshot(states, purpose)
        return live
    ok_tab = sum(1 for s in states if _slot_status_label(s)[0] == "OK   ")
    partl_tab = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
    logger.info(
        "Upstream wait: begin — purpose=%s | live_MAIN_or_sat_writes=0/%d "
        "| table_OK=%d table_PARTL=%d | max_wait=%.0fs (**OK** slots can still lack a writer "
        "if MAIN already closed).",
        purpose,
        len(states),
        ok_tab,
        partl_tab,
        max_wait_sec,
    )
    deadline = time.monotonic() + max_wait_sec
    t0 = time.monotonic()
    next_log = t0 + 2.0
    while time.monotonic() < deadline:
        time.sleep(0.4)
        live = _live_write_slots(states)
        if live:
            logger.info(
                "Upstream wait (%.1fs): %d slot(s) have a write path (MAIN or satellite) — %s",
                time.monotonic() - t0,
                len(live),
                purpose,
            )
            return live
        now = time.monotonic()
        if now >= next_log:
            logger.info(
                "Upstream wait: 0/%d slots with write path (%.0fs / %.0fs) — %s",
                len(states),
                now - t0,
                max_wait_sec,
                purpose,
            )
            next_log = now + 2.0
    logger.warning(
        "Upstream wait: after %.0fs still no slot with MAIN/sat write path — %s",
        max_wait_sec,
        purpose,
    )
    live = _live_write_slots(states)
    if not live and states:
        _log_upstream_dead_snapshot(states, purpose)
    return live


def fetch_room_list(
    states: list[SlotState],
    *,
    game_modes: tuple[int, ...] = (1, 2, 3, 5, 9),
    timeout_sec: float = 6.0,
    max_slot_attempts: int = 3,
    upstream_wait_sec: float = 0.0,
) -> list[tuple[str, int]]:
    """
    Request room lists for *game_modes* from one or more live slots and return
    ``[(room_name, num_players), ...]`` sorted by name.

    If the first slot's MAIN is slow or already dying, the same request is retried
    on additional live slots (up to *max_slot_attempts*) so the menu is less often empty.

    Game mode ints: 1=Transformice, 2=Bootcamp, 3=Vanilla, 5=Racing, 9=Module.
    """
    live = _wait_for_upstream_slots(
        states,
        max_wait_sec=upstream_wait_sec,
        purpose="room list request",
    )
    if not live:
        logger.warning("No live slot available to fetch room list (no MAIN/sat write path).")
        return []

    attempts = min(len(live), max(1, int(max_slot_attempts)))
    t0 = time.monotonic()
    logger.info(
        "Room list fetch: phase=%s try_slots=%s live_total=%d modes=%s timeout_per_round=%.1fs",
        get_operator_phase(),
        [s.label for s in live[:attempts]],
        len(live),
        game_modes,
        timeout_sec,
    )
    for bi in range(attempts):
        slot = live[bi]
        proxy = slot.proxy
        proxy.known_rooms.clear()
        proxy._room_list_ready.clear()

        for gm_int in game_modes:
            try:
                _run_coro_on_slot(slot, proxy.request_room_list(gm_int))
            except Exception as exc:
                logger.debug(
                    "fetch_room_list: slot %s request mode %s failed: %s",
                    slot.label,
                    gm_int,
                    exc,
                )
            time.sleep(0.15)

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if proxy._room_list_ready.is_set():
                time.sleep(0.8)  # let remaining responses arrive
                break
            time.sleep(0.2)

        if proxy.known_rooms:
            if bi > 0:
                logger.info(
                    "Room list: using data from slot %s (after %d empty attempt(s) on other slot(s)).",
                    slot.label,
                    bi,
                )
            rooms = sorted(proxy.known_rooms.items(), key=lambda x: x[0].lower())
            elapsed = time.monotonic() - t0
            n_after = len(_live_write_slots(states))
            logger.info(
                "Room list fetch OK: rooms=%d elapsed=%.2fs live_slots_after=%d/%d source_slot=%s",
                len(rooms),
                elapsed,
                n_after,
                len(states),
                slot.label,
            )
            return rooms
        if bi + 1 < attempts:
            logger.info(
                "Room list: no rooms from slot %s — trying next live slot…",
                slot.label,
            )

    elapsed = time.monotonic() - t0
    n_after = len(_live_write_slots(states))
    logger.warning(
        "Room list: still empty after %d slot attempt(s) — you can type a room name manually.",
        attempts,
    )
    logger.info(
        "Room list fetch empty: elapsed=%.2fs live_slots_after=%d/%d",
        elapsed,
        n_after,
        len(states),
    )
    return []


def pre_ban_dismiss_flash_dialogs(states: list[SlotState], cfg: object) -> None:
    """
    One-shot dismiss of Win32 **Continue** / error buttons on all Flash PIDs
    (Spanish *Continuar*, etc.) before room list / /ban. Cheap and reduces stuck popups.
    """
    if sys.platform != "win32" or not bool(getattr(cfg, "BAN_PRE_ROUND_DISMISS_FLASH", True)):
        return
    total = 0
    for s in states:
        pid = s.flash_pid
        if not pid or not flash_launch.flash_pid_is_alive(pid):
            continue
        try:
            total += flash_launch.dismiss_flash_error_dialogs_no_mouse(pid, s.label)
        except Exception:
            logger.debug("pre-ban dismiss: slot %s failed", s.label, exc_info=True)
    if total:
        logger.info(
            "ActionScript error dismiss: pre-round pass closed or clicked %d Flash error "
            "dialog control(s) (ActionScript/Adobe popups, security errors, etc.).",
            total,
        )


def post_login_actionscript_error_sweep(states: list[SlotState]) -> None:
    """
    After all slots report login, #2044 / #2048 popups can still appear a few seconds late
    on the last-opened clients. The background poller may stop before those HWNDs exist;
    this runs several passes with short delays so **Dismiss All** is applied to stragglers.
    Default pass count (when env unset) is 3; increase via BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_PASSES if needed.
    """
    if sys.platform != "win32":
        return
    raw = (os.environ.get("BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_PASSES") or "").strip()
    try:
        n_passes = int(raw) if raw else 3
    except ValueError:
        n_passes = 6
    n_passes = max(1, min(20, n_passes))
    raw_d = (os.environ.get("BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_DELAY_SEC") or "").strip()
    try:
        delay = float(raw_d) if raw_d else 0.55
    except ValueError:
        delay = 0.55
    delay = max(0.05, min(3.0, delay))
    raw_lead = (os.environ.get("BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_LEAD_SEC") or "").strip()
    try:
        lead_sec = float(raw_lead) if raw_lead else 1.5
    except ValueError:
        lead_sec = 1.5
    lead_sec = max(0.0, min(10.0, lead_sec))

    logger.info(
        "ActionScript error dismiss: post-login sweep %d pass(es), %.2fs between passes, "
        "%.2fs lead delay — catching late ActionScript error windows (often the last slots).",
        n_passes,
        delay,
        lead_sec,
    )
    ok_before = sum(1 for s in states if _slot_status_label(s)[0] == "OK   ")
    partl_before = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
    if lead_sec:
        time.sleep(lead_sec)
    total = 0
    for p in range(n_passes):
        if p:
            time.sleep(delay)
        for s in states:
            pid = s.flash_pid
            if not pid or not flash_launch.flash_pid_is_alive(pid):
                continue
            if not flash_launch.try_acquire_flash_ui(pid):
                continue
            try:
                total += flash_launch.dismiss_flash_error_dialogs_no_mouse(pid, s.label)
            except Exception:
                logger.debug("post-login sweep: slot %s failed", s.label, exc_info=True)
            finally:
                flash_launch.release_flash_ui(pid)
    ok_after = sum(1 for s in states if _slot_status_label(s)[0] == "OK   ")
    partl_after = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
    if total:
        logger.info(
            "ActionScript error dismiss: post-login sweep finished — closed or clicked %d "
            "error dialog(s) in this sweep.",
            total,
        )
    else:
        logger.info(
            "ActionScript error dismiss: post-login sweep finished — no extra dialogs to close "
            "(already handled during login, or no ActionScript popups).",
        )
    logger.info(
        "ActionScript sweep health: table OK %d -> %d | PARTL %d -> %d (dialogs closed=%d)",
        ok_before,
        ok_after,
        partl_before,
        partl_after,
        total,
    )
    if ok_after < ok_before or partl_after > partl_before:
        logger.warning(
            "ActionScript sweep: slot health worsened after dismiss (OK %d -> %d, PARTL %d -> %d). "
            "Check WARNINGs with operator_phase=as_sweep; prefer fixing TFM_PROXY_SWF / secrets "
            "over relying on sweep alone.",
            ok_before,
            ok_after,
            partl_before,
            partl_after,
        )


def _input_nonempty(prompt: str, *, what: str = "your answer") -> str:
    """
    Read from stdin until a non-empty line. Empty input often happens if an ActionScript
    / Flash error dialog or the game has focus; avoid treating Enter as a valid room/name.

    Wrapped in :class:`_quiet_console` so that proxy ``WARNING``/``INFO`` lines from
    background slots (e.g. post-login ``MAIN session ended ... clean-eof``) do not
    interrupt the prompt. The full traffic is still captured in ``log.txt``.
    """
    with _quiet_console():
        while True:
            s = input(prompt).strip()
            if s:
                return s
            print(
                "\n[!] Empty line — not accepted. Dismiss any Flash 'Adobe Flash Player' error "
                f"on top of a client, then click this console and type {what}.\n",
                flush=True,
            )


def _pick_room(states: list[SlotState], cfg: object) -> str:
    """
    Fetch available rooms from the game server, print a numbered list, and let
    the user pick by number or type a name directly.
    """
    set_operator_phase("room_list")
    try:
        _flush_log_handlers()
        logger.info("Fetching room list from game server...")
        _flush_log_handlers()

        rooms = fetch_room_list(
            states,
            timeout_sec=float(getattr(cfg, "ROOM_LIST_TIMEOUT_SEC", 10.0) or 10.0),
            max_slot_attempts=int(getattr(cfg, "ROOM_LIST_MAX_SLOT_ATTEMPTS", 3) or 3),
            upstream_wait_sec=_upstream_wait_sec(cfg),
        )

        if rooms:
            print(f"\nAvailable rooms ({len(rooms)} total):", flush=True)
            for i, (name, players) in enumerate(rooms, 1):
                print(f"  {i:3d}. {name:<30s}  [{players} players]", flush=True)
            print(flush=True)
            _flush_log_handlers()
            print(
                "\n---\n"
                ">>> Interactive step: enter room below. (Proxy logs are throttled; if the console is busy,\n"
                "    look for the line `Enter room number` — or scroll to the end.)\n"
                "---\n",
                flush=True,
            )
            choice = _input_nonempty(
                "Enter room number or room name (e.g. *Racing1): ",
                what="a room number or name",
            )
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(rooms):
                    chosen = rooms[idx][0]
                    logger.info("Room selected by number %s: %r", choice, chosen)
                    return chosen
            logger.info("Room entered directly: %r", choice)
            return choice
        else:
            logger.info("No room list received — enter room name manually.")
            _flush_log_handlers()
            print(
                "\n---\n"
                ">>> Interactive step: target room. Proxy heartbeats are logged infrequently; type below.\n"
                "---\n",
                flush=True,
            )
            return _input_nonempty(
                "Target room (text after /room, e.g. *Racing1): ",
                what="the room name",
            )
    finally:
        set_operator_phase("idle")


def _collect_player_list_via_join(
    states: list[SlotState],
    room: str,
    *,
    timeout_sec: float = 12.0,
    stagger_sec: float = 0.15,
    leader_only: bool = True,
    upstream_wait_sec: float = 0.0,
) -> tuple[list[str], str | None]:
    """
    Join *room* to receive ``SetPlayerListPacket`` and return player names.

    **leader_only (default):** only the first live slot sends ``JoinRoomPacket``.
    Simultaneous joins on every slot trigger parallel ``ChangeSatelliteServerPacket`` /
    sat migrations; the game server often drops most MAIN sessions (clean-eof within
    ~1s). The full room list still arrives on that one connection.

    **leader_only false:** legacy behavior — join all live slots (staggered by
    *stagger_sec*), merge names from any slot that got a list version bump.

    Returns ``(player_names, lead_slot_label)``. *lead_slot_label* is the label of
    the slot that already joined when *leader_only* is true; otherwise ``None`` (all
    joined here). The caller should pass *lead_slot_label* to
    ``_pre_ban_stagger_join_others`` so other slots are joined in a second phase
    with a larger stagger before /ban.
    """
    live = _wait_for_upstream_slots(
        states,
        max_wait_sec=upstream_wait_sec,
        purpose="player list / JoinRoom",
    )
    if not live:
        logger.warning(
            "Player list: no slot has a live upstream write path (MAIN or satellite; all PARTL or down) — "
            "cannot join %r to collect names. Fix upstream disconnects, increase "
            "BOT_UPSTREAM_WAIT_SEC, or relaunch Flash.",
            room,
        )
        return [], None

    if leader_only:
        leader = live[0]
        vb = leader.proxy._player_list_version
        try:
            _run_coro_on_slot(leader, leader.proxy.join_room(room))
        except Exception as exc:
            logger.debug("join_room leader slot %s failed: %s", leader.label, exc)
        logger.info(
            "Player list: leader-only JoinRoom for name list (BOT_PLAYER_LIST_JOIN_LEADER_ONLY) — "
            "slot %s JoinRoom %r; other live slots will join in the pre-ban stagger pass so "
            "we do not hit the server with parallel sat migrations during list build.",
            leader.label,
            room,
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if leader.proxy._player_list_version != vb:
                time.sleep(0.8)  # let UpdatePlayerListPackets accumulate
                break
            time.sleep(0.2)
        names = list(leader.proxy.known_players.keys())
        if not names:
            logger.warning(
                "Player list: no SetPlayerListPacket within %.1fs for %r (leader slot %s) — "
                "room name wrong, or MAIN died during join. Try BOT_PLAYER_LIST_COLLECT_TIMEOUT_SEC, "
                "or set BOT_PLAYER_LIST_JOIN_LEADER_ONLY=false (may drop connections).",
                timeout_sec,
                room,
                leader.label,
            )
        return sorted(names, key=str.lower), leader.label

    versions_before = {id(s.proxy): s.proxy._player_list_version for s in live}

    for s in live:
        try:
            _run_coro_on_slot(s, s.proxy.join_room(room))
        except Exception as exc:
            logger.debug("join_room slot %s failed: %s", s.label, exc)
        time.sleep(stagger_sec)

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if any(s.proxy._player_list_version != versions_before[id(s.proxy)] for s in live):
            time.sleep(0.8)  # let UpdatePlayerListPackets accumulate
            break
        time.sleep(0.2)

    all_players: set[str] = set()
    for s in live:
        if s.proxy._player_list_version != versions_before[id(s.proxy)]:
            all_players.update(s.proxy.known_players.keys())
    if not all_players:
        logger.warning(
            "Player list: no SetPlayerListPacket within %.1fs for %r from %d live slot(s) — "
            "MAIN may drop mid-join, or room name wrong. Try BOT_PLAYER_LIST_COLLECT_TIMEOUT_SEC, "
            "or enter nickname manually.",
            timeout_sec,
            room,
            len(live),
        )
    return sorted(all_players, key=str.lower), None


def _pre_ban_stagger_join_others(
    states: list[SlotState],
    room: str,
    cfg: object,
    leader_label: str | None,
) -> None:
    """
    Move every *other* live slot into *room* with ``PRE_BAN_ROOM_JOIN_STAGGER_SEC`` between
    ``JoinRoomPacket`` calls. The leader (see ``_collect_player_list_via_join``) already
    joined for the name list; this avoids a thundering herd during list collection.
    If *leader_label* is None (multi-join list mode), this is a no-op — everyone
    already joined in that phase.
    """
    if not leader_label:
        return
    live = [s for s in states if s.proxy and s.proxy._main_write_conn() is not None]
    rest = [s for s in live if s.label != leader_label]
    if not rest:
        return
    delay = float(getattr(cfg, "PRE_BAN_ROOM_JOIN_STAGGER_SEC", 1.5) or 0.0)
    delay = max(0.0, min(60.0, delay))
    logger.info(
        "Pre-ban room sync: %d other slot(s) will JoinRoom %r with %.1fs between each (leader=%s) "
        "[BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC] — spaces JoinRoom to limit simultaneous sat migrations "
        "before /ban; without spacing, many slots joining at once can still drop MAIN (clean-eof).",
        len(rest),
        room,
        delay,
        leader_label,
    )
    for i, s in enumerate(rest):
        if i and delay > 0:
            time.sleep(delay)
        try:
            _run_coro_on_slot(s, s.proxy.join_room(room))
        except Exception as exc:
            logger.warning(
                "Pre-ban: JoinRoom on slot %s failed: %s",
                s.label,
                exc,
            )


def _show_and_pick_player(states: list[SlotState], room: str, cfg: object | None = None) -> str:
    """
    Join *room* (leader slot by default, or all slots if configured), show the player
    list, then after you choose a target, move other slots into the room with a safe
    stagger before /ban.
    """
    set_operator_phase("player_list")
    try:
        _flush_log_handlers()
        logger.info("Collecting player list for %r...", room)
        _flush_log_handlers()

        if cfg is not None:
            pl_to = float(getattr(cfg, "PLAYER_LIST_COLLECT_TIMEOUT_SEC", 25.0) or 25.0)
            leader_only = bool(getattr(cfg, "PLAYER_LIST_JOIN_LEADER_ONLY", True))
            room_stagger = float(getattr(cfg, "ROOM_STAGGER_SEC", 0.15) or 0.15)
        else:
            _r = (os.environ.get("BOT_PLAYER_LIST_COLLECT_TIMEOUT_SEC") or "").strip()
            pl_to = float(_r) if _r else 25.0
            _lo = (os.environ.get("BOT_PLAYER_LIST_JOIN_LEADER_ONLY") or "").strip().lower()
            leader_only = _lo not in ("0", "false", "no", "off") if _lo else True
            _rs = (os.environ.get("BOT_ROOM_STAGGER_SEC") or "").strip()
            room_stagger = float(_rs) if _rs else 0.15
        pl_to = max(3.0, min(120.0, pl_to))
        room_stagger = max(0.0, min(10.0, room_stagger))

        players, lead_label = _collect_player_list_via_join(
            states,
            room,
            timeout_sec=pl_to,
            stagger_sec=room_stagger,
            leader_only=leader_only,
            upstream_wait_sec=_upstream_wait_sec(cfg) if cfg is not None else _upstream_wait_sec(None),
        )

        if players:
            print(f"\nPlayers in {room!r} ({len(players)} total):", flush=True)
            for i, name in enumerate(players, 1):
                print(f"  {i:3d}. {name}", flush=True)
            print(flush=True)
            _flush_log_handlers()
            print(
                "\n---\n"
                ">>> Interactive step: pick player — enter below. (You may need to scroll past proxy logs.)\n"
                "---\n",
                flush=True,
            )
            choice = _input_nonempty(
                "Enter player number or nickname (e.g. Zizao#0000): ",
                what="a player number or nickname",
            )
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(players):
                    chosen = players[idx]
                    logger.info("Player selected by number %s: %r", choice, chosen)
                    if cfg is not None:
                        _pre_ban_stagger_join_others(states, room, cfg, lead_label)
                    return chosen
            logger.info("Player entered directly: %r", choice)
            if cfg is not None:
                _pre_ban_stagger_join_others(states, room, cfg, lead_label)
            return choice
        else:
            logger.info("No player list received — enter nickname manually.")
            _flush_log_handlers()
            print(
                "\n---\n"
                ">>> Interactive step: target user — type nickname below.\n"
                "---\n",
                flush=True,
            )
            target = _input_nonempty(
                "Target user (nickname#tag, e.g. Zizao#0000): ",
                what="the nickname#tag",
            )
            if cfg is not None:
                _pre_ban_stagger_join_others(states, room, cfg, lead_label)
            return target
    finally:
        set_operator_phase("idle")


def send_ban_to_all(states: list[SlotState], target_user: str, cfg) -> None:
    """
    /ban on every slot with a **live upstream write path** (MAIN or satellite).

    Transformice normally requires **several distinct /ban reports** in the room (default **11**;
    configurable via ``BOT_BAN_QUORUM_REPORTS`` — see ``docs/BAN_QUORUM_TRANSFORMICE.md``). The bot
    warns when live slots or successful sends fall below that threshold.

    Default **burst** mode (``BOT_BAN_BURST_MODE``): with 2+ live slots, each ``send_ban_command``
    is scheduled immediately (no long sleeps between), then we await results. That prevents
    later slots from losing MAIN during the old random 1–2s inter-send delay.

    Set ``BOT_BAN_BURST_MODE=false`` to restore staggered sends (``BOT_BAN_DELAY_MIN/MAX_SEC``).

    ``BOT_BAN_PRESEND_STABILIZE_SEC`` (default 0.35s): optional sleep after the initial live-upstream
    snapshot, then re-snapshots so /ban uses connections that finished sat migration after /room.
    Set to 0 to disable.

    Slots with no connection at snapshot time are skipped (not counted as send failures).

    Returns a summary dict (ok/fail/skip counts, timing) for :func:`_write_session_report_markdown`.
    """
    set_operator_phase("ban")
    try:
        return _send_ban_to_all_body(states, target_user, cfg)
    finally:
        set_operator_phase("idle")


def _send_ban_to_all_body(states: list[SlotState], target_user: str, cfg) -> dict[str, object]:
    dmin = float(getattr(cfg, "BAN_DELAY_MIN_SEC", 1.0))
    dmax = float(getattr(cfg, "BAN_DELAY_MAX_SEC", 2.0))
    burst = bool(getattr(cfg, "BAN_BURST_MODE", True))

    def _can_ban(s: SlotState) -> bool:
        return bool(s.proxy and s.proxy._main_write_conn() is not None)

    wait_sec = _upstream_wait_sec(cfg)
    active = _live_write_slots(states)
    if not active and wait_sec > 0:
        active = _wait_for_upstream_slots(
            states,
            max_wait_sec=wait_sec,
            purpose="ban round",
        )
    try:
        stabilize = float(getattr(cfg, "BAN_PRESEND_STABILIZE_SEC", 0.35) or 0.0)
    except (TypeError, ValueError):
        stabilize = 0.35
    stabilize = max(0.0, min(5.0, stabilize))
    if stabilize > 0 and active:
        n_before = len(active)
        time.sleep(stabilize)
        active = _live_write_slots(states)
        n_after = len(active)
        if n_after != n_before:
            logger.info(
                "Ban round: pre-send stabilize %.2fs — live slot count %d → %d (MAIN/sat write path).",
                stabilize, n_before, n_after,
            )

    skipped_no_conn = [s for s in states if s.proxy is not None and not _can_ban(s)]
    inactive = [s for s in states if s.proxy is None]
    quorum = int(getattr(cfg, "BAN_QUORUM_REPORTS", 11) or 11)
    quorum = max(1, min(64, quorum))

    if skipped_no_conn:
        logger.info(
            "Ban round: %d slot(s) with no upstream (PARTL) — skipped; sending from %d live slot(s).",
            len(skipped_no_conn),
            len(active),
        )
        ntot = len(states)
        if ntot >= 4 and len(skipped_no_conn) * 2 >= ntot and len(active) < max(2, ntot // 4):
            logger.warning(
                "Ban round: most slots are PARTL (no live upstream) — /ban is degraded. "
                "Mitigate login overlap: set BOT_UI_FLASH_LAUNCH_STAGGER_SEC high enough (see startup "
                "auto floor for 8+ slots), fix ActionScript/loader; after login, use leader-only + "
                "BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC for room join, not the root cause of early PARTL.",
            )
    else:
        logger.info("Ban round: sending from %d slot(s) with live upstream.", len(active))

    if active and len(active) < quorum:
        logger.warning(
            "Ban quorum: only %d live slot(s) can send /ban; typical in-room requirement is %d distinct "
            "reports (BOT_BAN_QUORUM_REPORTS). Target may remain unbanned until more mice /ban — see "
            "docs/BAN_QUORUM_TRANSFORMICE.md.",
            len(active),
            quorum,
        )

    if active:
        order_h = ", ".join(s.label for s in active)
        logger.info(
            "Ban round: send order %s — mode=%s",
            order_h,
            "burst (schedule all, then await)" if burst and len(active) > 1 else "sequential",
        )

    results: list[tuple[str, bool, str, float, str]] = []
    t_round = time.monotonic()
    live_at_send = len(active)

    def _do_one_slot(s: SlotState) -> tuple[bool, str, float]:
        t_slot = time.monotonic()
        try:
            ok = _run_coro_on_slot(s, s.proxy.send_ban_command(target_user))
            reason = "sent" if ok else "send returned False"
        except Exception as exc:
            ok = False
            reason = f"{type(exc).__name__}: {exc}"
        dt_ms = (time.monotonic() - t_slot) * 1000.0
        logger.debug(
            "[slot %s] /ban %r -> %s (%.1fms)",
            s.label, target_user, "sent" if ok else "FAILED", dt_ms,
        )
        return ok, reason, dt_ms

    if burst and len(active) > 1:
        # Schedule every coroutine before awaiting any: avoids N×(1–2s) window where last slots' MAIN dies.
        scheduled: list[tuple[SlotState, concurrent.futures.Future]] = []
        for s in active:
            if not _can_ban(s):
                logger.warning(
                    "Ban round: slot %s — no write path at schedule time (~%.2fs into round, "
                    "after live snapshot). Skipping (reason=upstream_lost_before_schedule).",
                    s.label, time.monotonic() - t_round,
                )
                results.append(
                    (s.label, False, "upstream lost before schedule (burst)", 0.0, "fail")
                )
                continue
            if s.loop is None:
                logger.error("Ban round: slot %s — no event loop", s.label)
                results.append(
                    (s.label, False, "event loop not ready", 0.0, "fail")
                )
                continue
            fut = asyncio.run_coroutine_threadsafe(
                s.proxy.send_ban_command(target_user), s.loop
            )
            scheduled.append((s, fut))
        for s, fut in scheduled:
            t_wait = time.monotonic()
            try:
                ok = fut.result(timeout=30)
                reason = "sent" if ok else "send returned False"
            except Exception as exc:
                ok = False
                reason = f"{type(exc).__name__}: {exc}"
            dt_ms = (time.monotonic() - t_wait) * 1000.0
            results.append((s.label, bool(ok), reason, dt_ms, "ok" if ok else "fail"))
            logger.debug(
                "[slot %s] /ban %r -> %s (await %.1fms)",
                s.label, target_user, "sent" if ok else "FAILED", dt_ms,
            )
    else:
        for i, s in enumerate(active):
            if not _can_ban(s):
                logger.warning(
                    "Ban round: slot %s — upstream gone before send (position %d/%d, ~%.2fs into round). "
                    "Tip: set BOT_BAN_BURST_MODE=true (default) to avoid long stagger gaps, or lower "
                    "BOT_BAN_DELAY_*.",
                    s.label, i + 1, len(active), time.monotonic() - t_round,
                )
                results.append(
                    (s.label, False, "upstream lost before send (stagger)", 0.0, "fail")
                )
                continue
            ok, reason, dt_ms = _do_one_slot(s)
            results.append((s.label, bool(ok), reason, dt_ms, "ok" if ok else "fail"))
            if i < len(active) - 1:
                time.sleep(random.uniform(dmin, dmax))

    for s in skipped_no_conn:
        results.append((s.label, False, "no upstream (skipped)", 0.0, "skip"))
    for s in inactive:
        results.append((s.label, False, "proxy not started", 0.0, "skip"))

    ok_count = sum(1 for _, o, _, _, _ in results if o)
    skip_count = sum(1 for *_, k in results if k == "skip")
    fail_send = sum(1 for _, o, _, _, k in results if not o and k != "skip")
    total_dt = time.monotonic() - t_round
    banner = "=" * 88
    lines = [
        banner,
        f"  BAN RESULTS for {target_user!r}   (OK: {ok_count}  /  send failed: {fail_send}  /  "
        f"skipped: {skip_count}  of {len(results)}; round {total_dt:.1f}s)",
        f"  Quorum: typical in-room reports needed = {quorum} (BOT_BAN_QUORUM_REPORTS); "
        f"this round OK sends = {ok_count} — {'meets' if ok_count >= quorum else 'below'} threshold — "
        f"see docs/BAN_QUORUM_TRANSFORMICE.md",
        banner,
    ]
    for label, ok, reason, dt_ms, kind in results:
        if kind == "skip":
            mark = "[SKIP ]"
        else:
            mark = "[  OK  ]" if ok else "[ FAIL ]"
        lat = f"{dt_ms:6.1f}ms" if dt_ms else "      -"
        lines.append(f"  {mark}  slot {label:>3s}  [{lat}]  -> {reason}")
    lines.append(banner)
    for line in lines:
        print(line, flush=True)
        logger.info(line)
    _flush_log_handlers()
    quorum_met = ok_count >= quorum
    if ok_count > 0 and not quorum_met:
        logger.warning(
            "Ban quorum: only %d successful /ban send(s); typical in-room requirement is %d (BOT_BAN_QUORUM_REPORTS). "
            "The sanction may still be pending until enough distinct mice contribute — docs/BAN_QUORUM_TRANSFORMICE.md.",
            ok_count,
            quorum,
        )
    elif quorum_met:
        logger.info(
            "Ban quorum: OK sends (%d) >= configured quorum (%d) — meets typical distinct-report expectation.",
            ok_count,
            quorum,
        )
    logger.info(
        "Ban round complete: target=%r ok=%d send_fail=%d skipped=%d elapsed=%.1fs",
        target_user, ok_count, fail_send, skip_count, total_dt,
    )
    return {
        "target_user": target_user,
        "ok_count": ok_count,
        "fail_send": fail_send,
        "skip_count": skip_count,
        "total_slots": len(results),
        "round_seconds": total_dt,
        "burst_mode": burst,
        "live_slots_at_send": live_at_send,
        "quorum_reports": quorum,
        "quorum_met": quorum_met,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transformice multi-slot /room + /ban bot (local proxy).")
    p.add_argument(
        "--no-kill-stale",
        action="store_true",
        help="Do not kill processes already listening on configured proxy ports.",
    )
    p.add_argument(
        "--launch-flash-no-click",
        action="store_true",
        help="Auto-launch Flash but do not send a Transformice click (click manually in each window).",
    )
    p.add_argument(
        "--no-flash-auto-login",
        action="store_true",
        help="Disable dismiss-all + username/password typing after MAIN TCP (see .env / BOT_FLASH_AUTO_LOGIN_UI).",
    )
    p.add_argument(
        "--skip-net-check",
        action="store_true",
        help=(
            "Skip startup reachability checks (DNS, multi-port TCP, HTTP_PROXY notes) and do not fail fast. "
            "Not recommended for diagnosing PC vs mobile / hotspot differences."
        ),
    )
    return p.parse_args(argv)


def _apply_known_good_parity_mode_env_defaults() -> None:
    """
    ``BOT_KNOWN_GOOD_PARITY_MODE=true`` sets baseline defaults (setdefault only — .env wins if explicit).

    Runs before network preflight so strict multi-port probe applies on this process start.
    """
    from .env_setup import env_truthy

    if not env_truthy("BOT_KNOWN_GOOD_PARITY_MODE"):
        return
    os.environ.setdefault("BOT_BASELINE_MAX_SLOTS", "3")
    os.environ.setdefault("BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS", "true")
    os.environ.setdefault("BOT_PARITY_STARTUP_REMINDERS", "true")
    logger.info(
        "[parity] BOT_KNOWN_GOOD_PARITY_MODE=true — applied defaults: BOT_BASELINE_MAX_SLOTS=3, "
        "BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true, BOT_PARITY_STARTUP_REMINDERS=true (unless overridden in .env).",
    )


def _log_parity_workflow_reminders(cfg: object) -> None:
    """Structured checklist — matches README “Proving parity on another PC”."""
    strict = bool(getattr(cfg, "NET_PREFLIGHT_REQUIRE_ALL_PORTS", False))
    n_acc = len(getattr(cfg, "ACCOUNTS", []) or [])
    tfm_swf = (os.environ.get("TFM_PROXY_SWF") or "").strip()
    swf_note = (
        "TFM_PROXY_SWF points at a custom path — copy that file too."
        if tfm_swf
        else "TFM_PROXY_SWF unset — use repo-root TFMProxyLoader.swf."
    )
    logger.info(
        "[parity] Baseline checklist: (1) Copy full TFM_SECRETS_* block + TFMProxyLoader.swf from a working PC (%s) "
        "(2) One stable uplink; in log.txt grep `[probe] SUMMARY:` lines for `accepted TCP` "
        "(strict all-port preflight=%s via BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS). "
        "(3) Effective BOT_ACCOUNTS_JSON rows=%s (BOT_BASELINE_MAX_SLOTS). "
        "(4) Route upstream game TCP in Proxifier/split-VPN for exe=%r.",
        swf_note,
        strict,
        n_acc,
        sys.executable,
    )


def _cfg_shared_flash_policy_port(cfg) -> int | None:
    """Port where TFMProxyLoader expects ``xmlsocket://localhost:...`` (upstream uses 10801)."""
    raw = getattr(cfg, "SHARED_FLASH_SOCKET_POLICY_PORT", 10801)
    if raw is None or raw is False:
        return None
    p = int(raw)
    return p if p > 0 else None


def main(argv: list[str] | None = None) -> None:
    colorama_init()
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            reconf = getattr(_stream, "reconfigure", None)
            if reconf is not None:
                try:
                    reconf(encoding="utf-8", errors="replace")
                except OSError:
                    pass
    args = _parse_args(argv)
    _apply_known_good_parity_mode_env_defaults()
    _configure_logging()
    reset_trace_session()
    trace_step(logger, "main", "CLI session begin argv_summary skip_net_check=%s", args.skip_net_check)
    tfm_startup_refresh.run_flash_startup_refresh(_repo_root())
    session_wall_start = time.time()
    log_txt_path = _repo_root() / "log.txt"
    ban_summaries: list[dict[str, object]] = []
    session_exit_reason = "finished"
    from .run_checklist import prompt_run_checklist

    prompt_run_checklist()
    if not args.skip_net_check:
        trace_step(logger, "main", "run_network_preflight() starting")
        from .net_preflight import run_network_preflight

        run_network_preflight()
        trace_step(logger, "main", "run_network_preflight() finished OK")
    else:
        logger.warning(
            "[preflight] Skipped (--skip-net-check): no DNS/multi-port/HTTP-proxy env probe; "
            "misconfigured networks may fail later with PARTL or timeouts.",
        )
        trace_step(logger, "main", "network preflight skipped (--skip-net-check)")
    cfg = _load_accounts_module()
    trace_step(
        logger,
        "main",
        "config loaded accounts=%s baseline_max_slots=%s",
        len(getattr(cfg, "ACCOUNTS", []) or []),
        int(getattr(cfg, "BASELINE_MAX_SLOTS", 0) or 0),
    )

    tfm_swf_port_patch.maybe_purge_legacy_loader_patch_cache(_repo_root())

    if sys.platform == "win32" and flash_launch.flash_launch_files_present(_repo_root()):
        tfm_loader_alignment.log_client_asset_alignment(_repo_root())
        tfm_loader_alignment.log_operator_live_game_alignment_reminders()

    this_exe = Path(sys.executable).resolve()
    allow_kill = not args.no_kill_stale

    proxy_bind = getattr(cfg, "PROXY_BIND_HOST", None)
    if isinstance(proxy_bind, str):
        proxy_bind = proxy_bind.strip() or None

    raw_accounts = list(cfg.ACCOUNTS)
    baseline_max = int(getattr(cfg, "BASELINE_MAX_SLOTS", 0) or 0)
    if baseline_max > 0:
        n_full = len(raw_accounts)
        if n_full > baseline_max:
            logger.warning(
                "BOT_BASELINE_MAX_SLOTS=%s: using only the first %s of %s account row(s) "
                "(known-good parity baseline). Set BOT_BASELINE_MAX_SLOTS=0 to use the full list.",
                baseline_max,
                baseline_max,
                n_full,
            )
            raw_accounts = raw_accounts[:baseline_max]
        cfg.ACCOUNTS = raw_accounts
        try:
            os.environ["BOT_ACCOUNTS_JSON"] = json.dumps(
                raw_accounts,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception:
            logger.debug("Could not refresh BOT_ACCOUNTS_JSON env after baseline slice", exc_info=True)

    if bool(getattr(cfg, "PARITY_STARTUP_REMINDERS", False)) or baseline_max > 0:
        _log_parity_workflow_reminders(cfg)
    # False: listen on PROXY_BIND_HOST or all interfaces; row bind_ip is Proxifier reference only.
    # True only if each bind_ip is assigned to a NIC on *this* machine (otherwise bind() fails).
    use_account_bind_ip = getattr(cfg, "PROXY_LISTEN_USE_ACCOUNT_BIND_IP", False)
    states: list[SlotState] = []
    seen_ports: set[int] = set()
    seen_ips: set[str] = set()
    for i, row in enumerate(raw_accounts):
        port = int(row["proxy_port"])
        if port in seen_ports:
            msg = f"Duplicate proxy_port: {port}"
            logger.error(msg)
            raise SystemExit(msg)
        seen_ports.add(port)
        ip = str(row.get("bind_ip", "")).strip()
        if ip:
            if ip in seen_ips:
                msg = (
                    f"Duplicate bind_ip {ip!r} - each account needs a unique IP (Proxifier mapping)."
                )
                logger.error(msg)
                raise SystemExit(msg)
            seen_ips.add(ip)
        label = str(row.get("label", i + 1))
        if use_account_bind_ip:
            slot_listen: str | None = ip if ip else proxy_bind
        else:
            slot_listen = proxy_bind
        # Build the slot's bind_ip_pool from the row's primary bind_ip plus any
        # extra_bind_ips. The current bind_ip starts as row['bind_ip'] (so the
        # initial Flash launch sees the same value it always has). Subsequent
        # values are consumed by ``_retry_partl_slots`` on PARTL recovery.
        extras_raw = row.get("extra_bind_ips") or []
        if isinstance(extras_raw, str):
            extras = [
                p.strip()
                for p in extras_raw.replace(";", ",").replace(" ", ",").split(",")
                if p.strip()
            ]
        else:
            try:
                extras = [str(x).strip() for x in extras_raw if str(x).strip()]
            except TypeError:
                extras = []
        # Skip the primary in extras to avoid issuing the same IP twice.
        extras = [e for e in extras if e and e != ip]
        states.append(
            SlotState(
                label=label,
                port=port,
                proxy_bind_host=slot_listen,
                current_bind_ip=ip,
                bind_ip_pool=list(extras),
            )
        )

    focus_pump_stop: threading.Event | None = None
    # Stopped after login phase; while running it hammers all Flash PIDs and spams logs during
    # "Fetching room list" / room name input.
    flash_dismiss_poll_stop: threading.Event | None = None

    # Belt-and-suspenders cleanup: register an atexit hook as soon as ``states``
    # exists so Flash windows still get closed if the user hits Ctrl-C during
    # the long login phase (before the ban-loop try/finally wraps things).
    # The hook reads ``s.flash_pid`` live, so slots launched later are covered.
    def _atexit_close_flash(_states: list[SlotState] = states, _cfg: object = cfg) -> None:
        try:
            close_all_flash_windows(_states, _cfg)
        except Exception:
            logger.exception("atexit Flash cleanup raised")
    atexit.register(_atexit_close_flash)

    if not use_account_bind_ip and any(str(row.get("bind_ip", "")).strip() for row in raw_accounts):
        logger.info(
            "PROXY_LISTEN_USE_ACCOUNT_BIND_IP is False: per-row bind_ip is ignored for listening "
            "(use it in Proxifier only). Proxies use distinct ports; Flash connects via "
            "127.0.0.1 unless PROXY_BIND_HOST is set."
        )

    shared_flash_policy_port = _cfg_shared_flash_policy_port(cfg)
    policy_bind_host = str(
        getattr(cfg, "FLASH_SOCKET_POLICY_BIND_HOST", "127.0.0.1") or "127.0.0.1",
    ).strip() or "127.0.0.1"

    if shared_flash_policy_port is not None:
        for s in states:
            if s.port == shared_flash_policy_port:
                msg = (
                    f"Slot {s.label}: proxy_port {s.port} equals SHARED_FLASH_SOCKET_POLICY_PORT "
                    f"({shared_flash_policy_port}); use a different main port or disable the shared "
                    "policy (set SHARED_FLASH_SOCKET_POLICY_PORT = None)."
                )
                logger.error(msg)
                raise SystemExit(msg)
        if not ensure_port_free_or_kill_same_bot(
            shared_flash_policy_port,
            this_exe=this_exe,
            allow_kill=allow_kill,
        ):
            msg = (
                f"Shared Flash socket-policy port {shared_flash_policy_port} is in use "
                "(TFMProxyLoader needs this, or set BOT_SHARED_FLASH_SOCKET_POLICY_PORT=none in .env "
                "for per-slot policy ports)."
            )
            logger.error(msg)
            raise SystemExit(msg)

    _assign_listen_ports(states, shared_flash_policy_port=shared_flash_policy_port)

    if shared_flash_policy_port is not None:
        start_shared_flash_socket_policy_thread(
            port=shared_flash_policy_port,
            bind_host=policy_bind_host,
        )
        time.sleep(0.35)

    for s in states:
        pol = (
            shared_flash_policy_port
            if shared_flash_policy_port is not None
            else s.policy_port
        )
        logger.info(
            "Slot %s: listen_host=%r main=%s satellite=%s flash_policy=%s",
            s.label,
            s.proxy_bind_host,
            s.port,
            s.satellite_port,
            pol,
        )

    if not args.skip_net_check:
        from .net_preflight import log_proxy_listen_vs_upstream

        log_proxy_listen_vs_upstream(
            cfg=cfg,
            states=states,
            raw_accounts=raw_accounts,
            shared_flash_policy_port=shared_flash_policy_port,
        )

    for s, row in zip(states, raw_accounts):
        s.flash_username = str(row.get("username", "") or "")
        s.flash_password = str(row.get("password", "") or "")

    for row, st in zip(raw_accounts, states):
        row_dict = dict(row)
        row_dict["_flash_satellite_port"] = st.satellite_port
        row_dict["_flash_policy_port"] = (
            shared_flash_policy_port
            if shared_flash_policy_port is not None
            else st.policy_port
        )
        row_dict["_flash_connect_host"] = st.proxy_bind_host if st.proxy_bind_host else "127.0.0.1"
        st.packet_loader_url = flash_launch.loader_document_url_for_row(row_dict, _repo_root()) or ""

    cfg_flash_auto = bool(getattr(cfg, "FLASH_AUTO_LOGIN_UI", False))
    if args.no_flash_auto_login:
        cfg_flash_auto = False
    packet_auto_login_active = bool(getattr(cfg, "PACKET_AUTO_LOGIN", False))
    if packet_auto_login_active:
        cfg_flash_auto = False
        logger.info(
            "PACKET_AUTO_LOGIN=True — FLASH_AUTO_LOGIN_UI disabled (login is sent as LoginPacket by the proxy)",
        )

    _ap_raw = (os.environ.get("BOT_AUTO_PONG", "") or "").strip().lower()
    if _ap_raw in ("", "both", "1", "true", "yes", "on"):
        _ap_mode = "both"
    elif _ap_raw in ("0", "flash", "forward", "off", "no", "false"):
        _ap_mode = "flash"
    elif _ap_raw in ("swallow", "proxy", "proxy_only"):
        _ap_mode = "swallow"
    else:
        _ap_mode = "both"
    if _ap_mode == "flash" and packet_auto_login_active:
        logger.warning(
            "BOT_AUTO_PONG=%r resolves to 'flash' (forward-only) but PACKET_AUTO_LOGIN=True — "
            "Flash never enters its post-login state, so it will NOT pong forwarded "
            "PingPackets and every slot will be closed by the server (clean-eof, pong=0/0). "
            "Set BOT_AUTO_PONG=both (or remove the line from .env).",
            _ap_raw,
        )
    else:
        logger.info(
            "BOT_AUTO_PONG=%s — %s",
            _ap_mode,
            {
                "both":    "proxy ponges AND forwards ping to Flash (recommended; required for PACKET_AUTO_LOGIN).",
                "flash":   "forward-only (Flash must pong; only viable with the UI login flow).",
                "swallow": "proxy-pong only, ping NOT forwarded to Flash.",
            }[_ap_mode],
        )

    def _flash_login_trigger(c) -> str:
        t = str(getattr(c, "FLASH_LOGIN_TRIGGER", "after_launch") or "after_launch").strip().lower()
        if t in ("main_tcp", "main", "tcp"):
            return "main_tcp"
        return "after_launch"

    flash_login_trigger = _flash_login_trigger(cfg)
    flash_login_main_tcp_hook = cfg_flash_auto and flash_login_trigger == "main_tcp"

    trace_step(logger, "main", "starting proxy listener threads n_slots=%s", len(states))
    start_all_slots(
        states,
        this_exe=this_exe,
        allow_kill=allow_kill,
        cfg=cfg,
        flash_auto_login_ui=cfg_flash_auto,
        flash_login_main_tcp_hook=flash_login_main_tcp_hook,
    )
    trace_step(logger, "main", "proxy listener threads spawned (asyncio.run per slot thread)")

    auto_flash = (
        sys.platform == "win32"
        and bool(getattr(cfg, "UI_AUTO_LAUNCH_FLASH", True))
        and flash_launch.flash_launch_files_present(_repo_root())
    )
    if sys.platform == "win32" and not getattr(cfg, "UI_AUTO_LAUNCH_FLASH", True):
        logger.info("BOT_UI_AUTO_LAUNCH_FLASH=false — skipping Flash auto-launch; start game clients manually.")
        _flush_log_handlers()
    # main_tcp auto-login waits on flash_loader_ready_event; without auto-launch there is no loader phase.
    if not auto_flash:
        for s in states:
            s.flash_loader_ready_event.set()

    if auto_flash:
        flash_accounts: list[dict[str, object]] = []
        for row, st in zip(raw_accounts, states):
            d = dict(row)
            d["_flash_satellite_port"] = st.satellite_port
            d["_flash_policy_port"] = (
                shared_flash_policy_port
                if shared_flash_policy_port is not None
                else st.policy_port
            )
            d["_flash_connect_host"] = st.proxy_bind_host if st.proxy_bind_host else "127.0.0.1"
            flash_accounts.append(d)
        login_timeout = float(getattr(cfg, "FLASH_SLOT_LOGIN_TIMEOUT_SEC", 900.0))
        n_flash_slots = len(flash_accounts)
        _stagger_from_env = float(getattr(cfg, "FLASH_STAGGER_AFTER_LOGIN_SEC", 0.5))
        # Opening many projectors in quick succession backs up the CPU and TFM; overlapping
        # logins + sat migrations is the primary driver of "early slot PARTL" in large farms
        # (op_hint in logs often fires during this phase — PRE_BAN/leader is for the room phase).
        stagger_after = _effective_flash_stagger_sec(n_flash_slots, _stagger_from_env)
        trace_step(logger, "main", "Flash UI launch phase start n_slots=%s stagger=%.2fs", n_flash_slots, stagger_after)
        if n_flash_slots >= 8 and stagger_after > _stagger_from_env + 0.01:
            logger.info(
                "Flash launch: %d slots — post-login delay before opening the next client is %.1fs "
                "(env BOT_UI_FLASH_LAUNCH_STAGGER_SEC was %.1fs; added automatic minimum for 8+ slots "
                "(0.38*(n-1) capped at 7s, floors 4.25s@12+ and 5s@14+); override by raising env above %.1f).",
                n_flash_slots,
                stagger_after,
                _stagger_from_env,
                stagger_after,
            )
        elif n_flash_slots > 1:
            logger.info(
                "Flash launch: post-login delay before next client = %.1fs (BOT_UI_FLASH_LAUNCH_STAGGER_SEC).",
                stagger_after,
            )

        if sys.platform == "win32":
            logger.info(flash_launch.flash_error_dismiss_policy_log_line())
            # Visible contract for operators grepping log.txt: parallel JoinRoom to many
            # slots can overload sat migration and drop MAIN; defaults mitigate that.
            _lo = bool(getattr(cfg, "PLAYER_LIST_JOIN_LEADER_ONLY", True))
            _pb = float(getattr(cfg, "PRE_BAN_ROOM_JOIN_STAGGER_SEC", 1.5) or 0.0)
            logger.info(
                "MAIN / JoinRoom stability: default BOT_PLAYER_LIST_JOIN_LEADER_ONLY=%s and "
                "BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC=%.1fs — keep both to limit simultaneous sat "
                "migrations (mass clean-eof). Server kicks, idle, and other causes can still end MAIN. "
                "Sole-Continuar auto-dismiss (FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION) can end a bad "
                "AS error like a human click; that is still usually better than a stuck dialog. "
                "Recurring ActionScript on many slots: fix SWF/loader; mass PARTL at login: raise "
                "BOT_UI_FLASH_LAUNCH_STAGGER_SEC (auto floor applies for 8+ slots).",
                _lo,
                _pb,
            )
            if (os.environ.get("BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE") or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                logger.info(
                    "ROOT_CAUSE logging: BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE=true — each MAIN end logs "
                    "ROOT_CAUSE_MAIN_CLOSE (Flash→proxy TCP snapshot, srv/cli packet rings, errno/winerror, "
                    "sec_since_as_dismiss). For longer AS traces set FLASH_ERROR_FIRST_FP_PREVIEW_CHARS, "
                    "FLASH_ERROR_LOG_FULL_BODY_FIRST_FP, FLASH_ERROR_DISMISS_BODY_LOG_CHARS, "
                    "BOT_PROXY_MAIN_PACKET_RING."
                )

        # Start global dismiss poller BEFORE launching any Flash windows so
        # error dialogs are caught from the very first slot onwards.
        # Default 1.25s: ActionScript / #2044 / #2048 error dialogs (often one per slot) need
        # periodic dismiss; set to 0 to disable. See dismiss_flash_error_dialogs_no_mouse in flash_launch.
        _pd_raw_early = (os.environ.get("FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC") or "").strip()
        try:
            if _pd_raw_early:
                _pd_early = float(_pd_raw_early)
            else:
                _pd_early = 1.25
        except ValueError:
            _pd_early = 1.25
        if _pd_early > 0 and sys.platform == "win32":
            flash_dismiss_poll_stop = threading.Event()
            _dismiss_stop = flash_dismiss_poll_stop

            def _global_dismiss_worker_early(
                _states: list[SlotState] = states,
                _pd: float = _pd_early,
            ) -> None:
                while not _dismiss_stop.is_set():
                    # Wait in one shot so we exit promptly when login phase ends.
                    if _dismiss_stop.wait(timeout=_pd):
                        break
                    for _s in _states:
                        if _dismiss_stop.is_set():
                            return
                        pid = _s.flash_pid
                        if not pid or not flash_launch.flash_pid_is_alive(pid):
                            continue
                        if not flash_launch.try_acquire_flash_ui(pid):
                            continue
                        try:
                            # Logs each pass from flash_launch (scan start / skip reasons at DEBUG;
                            # each close at INFO; pass summary if anything closed).
                            flash_launch.dismiss_flash_error_dialogs_no_mouse(pid, _s.label)
                        except Exception:
                            logger.debug(
                                "Slot %s: periodic ActionScript/Flash error dismiss failed",
                                _s.label, exc_info=True
                            )
                        finally:
                            flash_launch.release_flash_ui(pid)

            threading.Thread(
                target=_global_dismiss_worker_early,
                daemon=True,
                name="flash-err-dismiss-global",
            ).start()
            logger.info(
                "ActionScript error dismiss: background poll every %.2fs during login only "
                "(stops when the initial login batch finishes, before PARTL retry; set "
                "FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC=0 to disable).",
                _pd_early,
            )

        # Rotate which Flash window is briefly foreground: background Flash throttles
        # Anticheat/ActionScript; this keeps all tiled slots responsive during login.
        _fpump_raw = (os.environ.get("BOT_FLASH_FOCUS_PUMP", "1") or "").strip().lower()
        if (
            sys.platform == "win32"
            and len(states) > 1
            and bool(getattr(cfg, "FLASH_MINIMIZE_AFTER_OPEN", False))
            and _fpump_raw not in ("0", "false", "no", "off")
        ):
            _, focus_pump_stop = flash_launch.start_flash_focus_pump_thread()
            logger.info(
                "Flash focus-pump: BOT_FLASH_FOCUS_PUMP_MS (default 600) — "
                "rotates foreground across tiled clients during login only; stops after login phase "
                "so the terminal stays usable.",
            )

        for idx, (flash_row, st) in enumerate(zip(flash_accounts, states), start=1):
            st.login_success_event.clear()
            st.login_aborted_event.clear()
            st.flash_main_tcp_seen = False
            st.flash_loader_ready_event.clear()
            logger.info(
                "Opening game %s/%s (slot %s); waiting up to %ss for login before next client.",
                idx,
                len(flash_accounts),
                st.label,
                login_timeout,
            )
            try:
                proc = flash_launch.launch_one_flash_loader(
                    flash_row,
                    root=_repo_root(),
                    click_transformice=not args.launch_flash_no_click,
                    on_flash_pid=lambda pid, st=st: setattr(st, "flash_pid", pid),
                    post_open_delay_sec=float(
                        getattr(cfg, "FLASH_LOADER_POST_OPEN_DELAY_SEC", 1.15)
                    ),
                )
            finally:
                st.flash_loader_ready_event.set()
            if proc is None:
                st.flash_pid = None
            if (
                cfg_flash_auto
                and flash_login_trigger == "main_tcp"
                and st.flash_pid is not None
            ):
                dm = float(getattr(cfg, "FLASH_LOGIN_AFTER_MAIN_DELAY_SEC", 1.0))
                logger.info(
                    "Slot %s: FLASH auto-login on first MAIN TCP: dismiss ASAP, then %.2fs before username (FLASH_LOGIN_TRIGGER=main_tcp)",
                    st.label,
                    max(0.0, dm),
                )
            if (
                cfg_flash_auto
                and flash_login_trigger == "after_launch"
                and st.flash_pid is not None
            ):
                after_launch_sec = float(getattr(cfg, "FLASH_LOGIN_AFTER_LAUNCH_SEC", 12.0))

                def _run_login_ui_later(
                    *,
                    _st: SlotState = st,
                    _cfg: object = cfg,
                    _delay: float = after_launch_sec,
                ) -> None:
                    time.sleep(max(0.0, _delay))
                    try:
                        flash_launch.run_flash_login_ui(
                            pid=_st.flash_pid,
                            username=_st.flash_username,
                            password=_st.flash_password,
                            slot_label=_st.label,
                            cfg=_cfg,
                            trigger="after_launch",
                        )
                    except Exception:
                        logger.exception(
                            "Slot %s: FLASH_LOGIN_TRIGGER=after_launch login UI failed",
                            _st.label,
                        )

                threading.Thread(
                    target=_run_login_ui_later,
                    daemon=True,
                    name=f"flash-login-ui-{st.label}",
                ).start()
                logger.info(
                    "Slot %s: scheduled FLASH auto-login in %.1fs (after_launch; tune FLASH_LOGIN_AFTER_LAUNCH_SEC)",
                    st.label,
                    max(0.0, after_launch_sec),
                )
            poll_sec = float(getattr(cfg, "FLASH_LOGIN_WAIT_POLL_SEC", 45.0))
            poll_sec = max(10.0, min(poll_sec, 120.0))
            deadline = time.monotonic() + login_timeout
            got_login = False
            verbose = bool(getattr(cfg, "PROXY_VERBOSE_LOGIN_FLOW", True))
            packet_login = bool(getattr(cfg, "PACKET_AUTO_LOGIN", False))
            if packet_login:
                logger.debug(
                    "Slot %s: PACKET_AUTO_LOGIN — waiting for LoginSuccessPacket (proxy injects credentials automatically).",
                    st.label,
                )
            elif not cfg_flash_auto:
                logger.info(
                    "Slot %s: manual login — type username/password and submit in Flash. "
                    "Watch log for HandshakeResponse, LoginPacket (password redacted), "
                    "AccountError, Captcha, ChangeSatelliteServer; OK line = LoginSuccess.",
                    st.label,
                )
            elif verbose:
                logger.info(
                    "Slot %s: PROXY_VERBOSE_LOGIN_FLOW=True — extra login-phase packet lines in log.",
                    st.label,
                )

            # Fast early retry clicks: the initial post_open_delay click (default ~1.15s) frequently
            # lands before the loader SWF has drawn the Transformice button, especially on later slots
            # when earlier Flash instances are still consuming CPU. The main poll loop below only
            # retries every FLASH_LOGIN_WAIT_POLL_SEC (default 45s, floor 10s), which is way too slow
            # to recover. Do a few aggressive re-clicks in the first ~20s while MAIN TCP is still
            # missing so slots without bind_ip (which can't rely on Proxifier warming the port) still
            # reach the proxy.
            if (
                st.flash_pid is not None
                and not args.launch_flash_no_click
                and not st.flash_main_tcp_seen
                and not st.login_success_event.is_set()
            ):
                early_interval = float(
                    getattr(cfg, "FLASH_LOADER_EARLY_RETRY_INTERVAL_SEC", 3.0)
                )
                early_retries = int(getattr(cfg, "FLASH_LOADER_EARLY_RETRY_COUNT", 5))
                early_interval = max(0.5, min(early_interval, 15.0))
                early_retries = max(0, min(early_retries, 20))
                frac_x_early = float(getattr(cfg, "FLASH_LOADER_CLICK_FRAC_X", 0.50))
                frac_y_early = float(getattr(cfg, "FLASH_LOADER_CLICK_FRAC_Y", 0.55))
                for attempt in range(1, early_retries + 1):
                    deadline_early = time.monotonic() + early_interval
                    while time.monotonic() < deadline_early:
                        if (
                            st.flash_main_tcp_seen
                            or st.login_success_event.is_set()
                        ):
                            break
                        time.sleep(0.2)
                    if st.flash_main_tcp_seen or st.login_success_event.is_set():
                        # MAIN TCP accept already logged the arrival; no need to repeat.
                        logger.debug(
                            "Slot %s: MAIN TCP observed during early retry window (before attempt %d/%d)",
                            st.label, attempt, early_retries,
                        )
                        break
                    logger.info(
                        "Slot %s: no MAIN TCP after ~%.1fs — re-clicking Transformice "
                        "loader [early retry %d/%d]",
                        st.label,
                        early_interval * attempt,
                        attempt, early_retries,
                    )
                    # Dump every visible top-level window belonging to this Flash PID
                    # — if a Flash error popup / "Restricted content" dialog appeared,
                    # we want to see it so we know clicks need to target it, not the
                    # main loader window. Keep the "just 1 window" case quiet since
                    # it's the expected normal path.
                    try:
                        windows = flash_launch.list_flash_windows(st.flash_pid)
                        if len(windows) != 1:
                            logger.warning(
                                "Slot %s: flash_pid=%s has %d top-level window(s): %s",
                                st.label, st.flash_pid, len(windows),
                                ", ".join(
                                    f"HWND={w['hwnd']} title={w['title']!r} "
                                    f"size={w['width']}x{w['height']}"
                                    for w in windows
                                ) or "(none)",
                            )
                        else:
                            logger.debug(
                                "Slot %s: flash_pid=%s one window (normal) HWND=%s title=%r size=%dx%d",
                                st.label, st.flash_pid,
                                windows[0]["hwnd"], windows[0]["title"],
                                windows[0]["width"], windows[0]["height"],
                            )
                    except Exception:
                        logger.exception(
                            "Slot %s: list_flash_windows raised", st.label
                        )
                    # Try several candidate positions on each retry: the Transformice
                    # entry in the loader has shifted between builds (sometimes ~0.55y,
                    # sometimes ~0.45y, sometimes lower on screens that render a cached
                    # "Continue" button). Hammering a small cluster is far cheaper than
                    # waiting for the 45s slow-poll retry to land on the right pixel.
                    click_positions = [
                        (frac_x_early, frac_y_early),
                        (frac_x_early, 0.45),
                        (frac_x_early, 0.65),
                        (frac_x_early, 0.50),
                        (frac_x_early, 0.60),
                    ]
                    for fx_c, fy_c in click_positions:
                        if (
                            st.flash_main_tcp_seen
                            or st.login_success_event.is_set()
                        ):
                            break
                        flash_launch.click_transformice_in_loader(
                            st.flash_pid, st.label,
                            frac_x=fx_c, frac_y=fy_c,
                        )
                        time.sleep(0.15)

            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                chunk = min(poll_sec, left)
                if st.login_success_event.wait(timeout=chunk):
                    got_login = True
                    # "OK  [slot X] logged in as …" from ban_proxy already covers this;
                    # a second line would just double the noise.
                    logger.debug("Slot %s reported login success; proceeding.", st.label)
                    if bool(getattr(cfg, "FLASH_MINIMIZE_AFTER_OPEN", False)) and st.flash_pid:
                        # NOTE: minimize currently moves the window off-screen instead of
                        # using SW_MINIMIZE — see minimize_flash_window docstring for why.
                        # If env var BOT_FLASH_DIAG_KEEP_ONSCREEN=1, skip the hide entirely
                        # so we can diagnose whether the upstream-drop is caused by window
                        # throttling or by something else (server idle kick, Flash watchdog).
                        if os.environ.get("BOT_FLASH_DIAG_KEEP_ONSCREEN", "").strip().lower() in ("1", "true", "yes"):
                            logger.info(
                                "Slot %s: BOT_FLASH_DIAG_KEEP_ONSCREEN=1 — leaving Flash window visible for diagnosis",
                                st.label,
                            )
                        else:
                            flash_launch.minimize_flash_window(st.flash_pid, st.label)
                    break
                if st.login_aborted_event.is_set():
                    logger.info(
                        "Slot %s: MAIN session closed before LoginSuccess — "
                        "(clean-eof during handshake is common with stagger overload or unstable links).",
                        st.label,
                    )
                    break
                if st.flash_main_tcp_seen:
                    extra_verbose = (
                        " With PROXY_VERBOSE_LOGIN_FLOW: expect a [MAIN→srv] LoginPacket after you submit; "
                        "if none appears, auto-login may be too early or submit missed — try "
                        "FLASH_LOGIN_AFTER_SUBMIT_EXTRA_SEC / FLASH_LOGIN_SECOND_SUBMIT_CLICK in .env."
                        if verbose
                        else ""
                    )
                    # Specific hint for the "incorrect version" symptom: Flash has a MAIN TCP
                    # connection to the proxy but never sent SystemInformationPacket (the
                    # trigger for PACKET_AUTO_LOGIN). That typically means the loader SWF hit
                    # an error BEFORE sending the handshake — most commonly a "incorrect
                    # version" / rate-limit screen when the game server refuses rapid-fire
                    # logins from the same public IP. Tell the user how to recover so they
                    # don't keep staring at "Still waiting..." lines without guidance.
                    logger.info(
                        "Still waiting slot %s (~%.0fs left): no LoginSuccess yet. "
                        "MAIN TCP already connected — Flash reached 127.0.0.1:%s but never sent "
                        "SystemInformationPacket. If Flash shows 'incorrect version' or a blank "
                        "screen here, the game server is rate-limiting logins or the loader "
                        "failed its self-check; increase BOT_UI_FLASH_LAUNCH_STAGGER_SEC (try 3-5s), "
                        "re-run TFM_SECRETS_GAME_VERSION dump, or re-run the bot so only the "
                        "failing slots retry.%s",
                        st.label,
                        max(0.0, deadline - time.monotonic()),
                        st.port,
                        extra_verbose,
                    )
                else:
                    logger.info(
                        "Still waiting slot %s (~%.0fs left): no LoginSuccess / OK line yet. "
                        "If the log never shows 'MAIN TCP accept' for this slot, Flash is not connecting "
                        "to 127.0.0.1:%s — open tmp/loader_patch/*_zwsflen.swf for this port (not raw "
                        "TFMProxyLoader.swf), check firewall, or click Transformice in the loader."
                        "%s",
                        st.label,
                        max(0.0, deadline - time.monotonic()),
                        st.port,
                        (
                            " With PROXY_VERBOSE_LOGIN_FLOW, look for AccountError / captcha lines above."
                            if verbose
                            else ""
                        ),
                    )
                    # Retry the Transformice loader click in case the first one missed.
                    if st.flash_pid and args.launch_flash_no_click is False:
                        frac_x = float(getattr(cfg, "FLASH_LOADER_CLICK_FRAC_X", 0.50))
                        frac_y = float(getattr(cfg, "FLASH_LOADER_CLICK_FRAC_Y", 0.55))
                        logger.info(
                            "Slot %s: retrying Transformice loader click at (%.2f, %.2f)",
                            st.label, frac_x, frac_y,
                        )
                        flash_launch.click_transformice_in_loader(
                            st.flash_pid, st.label, frac_x=frac_x, frac_y=frac_y
                        )
            if not got_login:
                if st.login_aborted_event.is_set():
                    logger.warning(
                        "Slot %s: no LoginSuccess — MAIN closed during handshake (see WARNING above). "
                        "Not a full %ss idle wait; continuing to next slot.",
                        st.label,
                        login_timeout,
                    )
                else:
                    logger.warning(
                        "Slot %s: no login success within %ss — finish manually or fix loader/proxy; continuing.",
                        st.label,
                        login_timeout,
                    )
                if (
                    bool(getattr(cfg, "FLASH_CLOSE_ON_LOGIN_FAIL", True))
                    and st.flash_pid
                    and flash_launch.flash_pid_is_alive(st.flash_pid)
                ):
                    logger.info(
                        "Slot %s: closing stale Flash window PID=%s "
                        "(BOT_FLASH_CLOSE_ON_LOGIN_FAIL=true).",
                        st.label, st.flash_pid,
                    )
                    try:
                        flash_launch.close_flash_window(
                            st.flash_pid, st.label,
                            grace_sec=float(
                                getattr(cfg, "FLASH_CLOSE_GRACE_SEC", 2.0)
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "Slot %s: close_flash_window raised", st.label,
                        )
                    st.flash_pid = None
                if st.error is None:
                    st.error = f"no login within {login_timeout}s"
            time.sleep(max(0.0, stagger_after))
    _wait_for_game_clients(states, auto_flash_launched=auto_flash)

    print_slot_status(states, title="SLOT STATUS AFTER LOGIN PHASE")

    if auto_flash:
        try:
            _log_login_phase_diagnostics(
                states,
                n_slots=n_flash_slots,
                stagger_effective=stagger_after,
                stagger_from_env=_stagger_from_env,
            )
        except Exception:
            logger.debug("Login phase diagnostics failed", exc_info=True)

    if auto_flash and len(states) >= 4:
        _partl = sum(1 for s in states if _slot_status_label(s)[0] == "PARTL")
        if _partl * 2 >= len(states) and _partl > 0:
            logger.warning(
                "Login phase: %d/%d PARTL — primary drivers: (1) many Flash instances + MAIN/sat overlap "
                "(raise BOT_UI_FLASH_LAUNCH_STAGGER_SEC above the auto floor; see stagger_effective in "
                "`Login phase diagnostics`); (2) recurring ActionScript / loader-runtime errors "
                "(fix TFM_PROXY_SWF + TFM_SECRETS_GAME_VERSION; check literal_version_in_swf in diagnostics). "
                "BOT_PRE_BAN / leader-only apply to room join, not early PARTL.",
                _partl,
                len(states),
            )

    # Focus pump + dismiss poller must stop as soon as the *initial* sequential login
    # batch finishes — not after PARTL retry or the post-login sweep. Retry can run for
    # many minutes (per-slot relaunch + BOT_RETRY_LOGIN_TIMEOUT_SEC); if the pump keeps
    # cycling SetForegroundWindow across all Flash HWNDs, the console never receives
    # keyboard input and Ctrl+C appears broken.
    if focus_pump_stop is not None:
        focus_pump_stop.set()
        logger.info(
            "Flash focus-pump stopped after login phase — console input and other windows work normally.",
        )
    if flash_dismiss_poll_stop is not None:
        flash_dismiss_poll_stop.set()
        logger.info(
            "ActionScript error dismiss: background poll stopped after login phase — you can use "
            "the room list and type the room name without the bot scanning Flash windows every few seconds.",
        )

    # Try to revive PARTL slots: relaunch each failed Flash tab (optionally with a
    # different bind_ip from the slot's pool / BOT_SPARE_BIND_IPS) before we ask
    # the user to pick a room. Only runs when Flash was auto-launched — without
    # auto-launch we have no PID to close and nothing to relaunch.
    if auto_flash:
        try:
            _retry_partl_slots(
                states,
                raw_accounts,
                cfg,
                args,
                shared_flash_policy_port=shared_flash_policy_port,
            )
        except Exception:
            logger.exception("PARTL retry pass raised; continuing with current slot states.")

    # Late #2044/#2048 boxes (commonly the last 3 Flash clients) often appear *after* the
    # "logged in" line but before the next poll; run a multi-pass dismiss (poller is
    # already off — see block above) to catch stragglers.
    if auto_flash and sys.platform == "win32":
        set_operator_phase("as_sweep")
        try:
            post_login_actionscript_error_sweep(states)
        finally:
            set_operator_phase("idle")

    # Wrap the ban loop in try/finally so every Flash projector window the bot
    # launched gets closed on the way out — normal exit ("n" to the prompt),
    # Ctrl-C, or an uncaught exception. Without this the user ends up with
    # ~14 Flash windows to hand-close every session.
    try:
        while True:
            pre_ban_dismiss_flash_dialogs(states, cfg)
            room = _pick_room(states, cfg)
            logger.info("Target room: %r", room)
            if not room:
                logger.info("Empty room; try again.")
                continue

            target = _show_and_pick_player(states, room, cfg)
            logger.info("Target user: %r", target)
            if not target:
                logger.info("Empty user; try again.")
                continue

            ban_summaries.append(send_ban_to_all(states, target, cfg))

            _flush_log_handlers()
            with _quiet_console():
                again = input('Ban someone else? (y/n): ').strip().lower()
            logger.info("Ban someone else? answered: %r", again)
            if again not in ("y", "yes"):
                break

        logger.info("Exiting (proxy threads stop when you close this process).")
    except KeyboardInterrupt:
        session_exit_reason = "keyboard_interrupt"
        logger.info("Interrupted by user (Ctrl-C); closing Flash windows before exit.")
    finally:
        try:
            report_path = _write_session_report_markdown(
                repo=_repo_root(),
                states=states,
                cfg=cfg,
                ban_rounds=ban_summaries,
                session_started_wall=session_wall_start,
                session_exit_reason=session_exit_reason,
                flash_auto_launched=auto_flash,
                log_txt_path=log_txt_path,
            )
            if report_path:
                logger.info("Session report: %s", report_path)
        except Exception:
            logger.exception("Session report failed")
        if focus_pump_stop is not None:
            focus_pump_stop.set()
        if flash_dismiss_poll_stop is not None:
            flash_dismiss_poll_stop.set()
        try:
            close_all_flash_windows(states, cfg)
        except KeyboardInterrupt:
            # Second/third Ctrl-C — operator wants out NOW. Skip the per-slot
            # walk entirely and go straight to taskkill so we still don't leak
            # 14 Flash windows.
            logger.info(
                "Flash cleanup: Ctrl-C again — running taskkill nuke directly."
            )
            try:
                _taskkill_all_flash_windows(reason="Ctrl-C escaped close_all_flash_windows")
            except Exception:
                logger.exception("Flash cleanup: taskkill fallback raised")
        except Exception:
            logger.exception("Flash cleanup raised during shutdown")
            try:
                _taskkill_all_flash_windows(reason="exception escaped close_all_flash_windows")
            except Exception:
                logger.exception("Flash cleanup: taskkill fallback raised")


if __name__ == "__main__":
    main()
