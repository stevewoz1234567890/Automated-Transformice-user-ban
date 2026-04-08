"""
CMD entry: multi-slot local proxies, /room on all clients, then staggered /ban.

Prerequisite: one Transformice + tfm-proxy-loader per ``proxy_port``. Row ``bind_ip`` is for
Proxifier unless ``PROXY_LISTEN_USE_ACCOUNT_BIND_IP`` is True and that IP exists on this machine.

The proxy always injects ``LoginPacket`` after ``SystemInformationPacket`` using credentials from
``bot/config.py`` (see ``ban_proxy``).

On Windows, Flash opens **one game at a time**: after each window opens, the bot waits for that
slot's ``LoginSuccess`` before starting the next. The loader URL includes ``host``, ``port``,
``satellite``, and ``policy`` (see ``flash_launch``). A shared Flash socket-policy server runs on
port 10801 unless disabled in config. ``TFMProxyLoader.cfg`` is written and read back for
confirmation. Use ``--no-launch-flash`` to skip auto-launch.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from colorama import init as colorama_init

from .ban_proxy import (
    BanBotProxy,
    ensure_flash_trust_config,
    start_shared_flash_socket_policy_thread,
)
from . import flash_launch
from .portutil import ensure_port_free_or_kill_same_bot, tcp_port_is_free

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
        "Close other programs or change proxy_port values in config."
    )
    logger.error(msg)
    raise SystemExit(msg)


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _load_accounts_module():
    if getattr(sys, "frozen", False):
        path = _repo_root() / "bot" / "config.py"
        if not path.is_file():
            logger.error(
                "Missing %s. Create bot/config.py beside this program (same layout as the repo).",
                path,
            )
            raise SystemExit(1)
        spec = importlib.util.spec_from_file_location("bot.config", path)
        if spec is None or spec.loader is None:
            logger.error("Could not load config from %s", path)
            raise SystemExit(1)
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
    else:
        try:
            import bot.config as cfg  # type: ignore
        except ImportError as e:
            logger.error(
                "Missing bot.config. Create bot/config.py with ACCOUNTS (see README).",
            )
            raise SystemExit(1) from e
    accounts = getattr(cfg, "ACCOUNTS", None)
    if not accounts:
        logger.error("config.ACCOUNTS is empty.")
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
    proxy: BanBotProxy | None = None
    loop: asyncio.AbstractEventLoop | None = None
    thread: threading.Thread | None = None
    error: str | None = None
    flash_pid: int | None = None
    flash_username: str = ""
    flash_password: str = ""
    # file:///…swf URL for LoginPacket.loader_url (same as patched loader).
    packet_loader_url: str = ""


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


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    log_path = _repo_root() / "log.txt"
    file_h = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    logging.info("Logging to %s", log_path)


def _run_slot_async(
    state: SlotState,
    cfg: object,
) -> None:
    async def _run():
        try:
            proxy = BanBotProxy(
                host_address=state.proxy_bind_host,
                host_main_port=state.port,
                host_satellite_port=state.satellite_port,
                host_socket_policy_port=state.policy_port,
                slot_label=state.label,
                login_success_event=state.login_success_event,
                verbose_login_flow=bool(
                    getattr(cfg, "PROXY_VERBOSE_LOGIN_FLOW", True)
                ),
                log_all_main_packets=bool(
                    getattr(cfg, "PROXY_LOG_ALL_MAIN_PACKETS", False)
                ),
                packet_login_username=state.flash_username,
                packet_login_password=state.flash_password,
                packet_login_loader_url=state.packet_loader_url,
                packet_login_delay_sec=float(
                    getattr(cfg, "PACKET_LOGIN_DELAY_SEC", 0.35) or 0.35
                ),
                packet_login_start_room=str(
                    getattr(cfg, "PACKET_LOGIN_START_ROOM", "") or ""
                ),
            )
            state.proxy = proxy
            await proxy.startup()
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
) -> None:
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
                    "Free it or change proxy_port in config."
                )
                logger.error(msg)
                raise SystemExit(msg)

    ensure_flash_trust_config()

    for s in states:
        t = threading.Thread(
            target=_run_slot_async,
            args=(s, cfg),
            name=f"tfm-ban-{s.port}",
            daemon=True,
        )
        s.thread = t
        t.start()

    time.sleep(1.0)


def _wait_for_all_slots_logged_in(states: list[SlotState], cfg: object) -> None:
    """Block until every slot has received LoginSuccessPacket (or timeout)."""
    timeout_sec = float(getattr(cfg, "ALL_SLOTS_LOGIN_TIMEOUT_SEC", 7200.0) or 7200.0)
    timeout_sec = max(60.0, timeout_sec)
    n = len(states)
    logger.info(
        "Waiting until all %s slot(s) report login success (timeout %.0fs)...",
        n,
        timeout_sec,
    )
    deadline = time.monotonic() + timeout_sec
    poll_sec = 2.0
    while time.monotonic() < deadline:
        pending = [
            s
            for s in states
            if s.proxy is not None and not s.login_success_event.is_set()
        ]
        if not pending:
            logger.info("All %s slot(s) logged in.", n)
            return
        time.sleep(poll_sec)
    labels = [
        s.label
        for s in states
        if s.proxy is not None and not s.login_success_event.is_set()
    ]
    logger.warning(
        "Timeout waiting for login — missing slot(s): %s. Continuing anyway.",
        ", ".join(labels) if labels else "(none)",
    )


def _run_coro_on_slot(state: SlotState, coro):
    loop = state.loop
    if loop is None:
        logger.error("Slot %s: event loop not ready", state.label)
        return False
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=30)


def run_ban_round(states: list[SlotState], room: str, target_user: str, cfg) -> None:
    stagger = float(getattr(cfg, "ROOM_STAGGER_SEC", 0.15))
    dmin = float(getattr(cfg, "BAN_DELAY_MIN_SEC", 1.0))
    dmax = float(getattr(cfg, "BAN_DELAY_MAX_SEC", 2.0))

    for s in states:
        if s.proxy is None:
            continue
        ok = _run_coro_on_slot(s, s.proxy.send_room_command(room))
        logger.info("[slot %s] /room -> %s", s.label, "sent" if ok else "FAILED")
        time.sleep(stagger)

    time.sleep(0.5)

    active = [s for s in states if s.proxy is not None]
    for i, s in enumerate(active):
        ok = _run_coro_on_slot(s, s.proxy.send_ban_command(target_user))
        logger.info(
            "[slot %s] /ban %r -> %s",
            s.label,
            target_user,
            "sent" if ok else "FAILED",
        )
        if i < len(active) - 1:
            time.sleep(random.uniform(dmin, dmax))

    logger.info("All accounts finished sending /ban commands for this round.")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transformice multi-slot /room + /ban bot (local proxy).")
    p.add_argument(
        "--no-kill-stale",
        action="store_true",
        help="Do not kill processes already listening on configured proxy ports.",
    )
    p.add_argument(
        "--no-launch-flash",
        action="store_true",
        help="Do not auto-start flashplayer_32_sa_debug.exe + TFMProxyLoader.swf per account (Windows).",
    )
    p.add_argument(
        "--launch-flash-no-click",
        action="store_true",
        help="Auto-launch Flash but do not send a Transformice click (click manually in each window).",
    )
    return p.parse_args(argv)


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
    _configure_logging()
    args = _parse_args(argv)
    cfg = _load_accounts_module()

    this_exe = Path(sys.executable).resolve()
    allow_kill = not args.no_kill_stale

    proxy_bind = getattr(cfg, "PROXY_BIND_HOST", None)
    if isinstance(proxy_bind, str):
        proxy_bind = proxy_bind.strip() or None

    raw_accounts = cfg.ACCOUNTS
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
        states.append(SlotState(label=label, port=port, proxy_bind_host=slot_listen))

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
                "(TFMProxyLoader needs this, or set SHARED_FLASH_SOCKET_POLICY_PORT = None in config "
                "for legacy per-slot policy ports)."
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

    start_all_slots(
        states,
        this_exe=this_exe,
        allow_kill=allow_kill,
        cfg=cfg,
    )

    auto_flash = (
        sys.platform == "win32"
        and not args.no_launch_flash
        and flash_launch.flash_launch_files_present(_repo_root())
    )

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
        stagger_after = float(getattr(cfg, "FLASH_STAGGER_AFTER_LOGIN_SEC", 0.5))
        for idx, (flash_row, st) in enumerate(zip(flash_accounts, states), start=1):
            st.login_success_event.clear()
            logger.info(
                "Opening game %s/%s (slot %s); waiting up to %ss for login before next client.",
                idx,
                len(flash_accounts),
                st.label,
                login_timeout,
            )
            proc = flash_launch.launch_one_flash_loader(
                flash_row,
                root=_repo_root(),
                click_transformice=not args.launch_flash_no_click,
                on_flash_pid=lambda pid, st=st: setattr(st, "flash_pid", pid),
                post_open_delay_sec=float(
                    getattr(cfg, "FLASH_LOADER_POST_OPEN_DELAY_SEC", 1.15)
                ),
            )
            if proc is None:
                st.flash_pid = None
            poll_sec = float(getattr(cfg, "FLASH_LOGIN_WAIT_POLL_SEC", 45.0))
            poll_sec = max(10.0, min(poll_sec, 120.0))
            deadline = time.monotonic() + login_timeout
            got_login = False
            verbose = bool(getattr(cfg, "PROXY_VERBOSE_LOGIN_FLOW", True))
            if verbose:
                logger.info(
                    "Slot %s: PROXY_VERBOSE_LOGIN_FLOW=True — extra login-phase packet lines in log.",
                    st.label,
                )
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                chunk = min(poll_sec, left)
                if st.login_success_event.wait(timeout=chunk):
                    got_login = True
                    logger.info("Slot %s reported login success; proceeding.", st.label)
                    break
                logger.info(
                    "Still waiting slot %s (~%.0fs left): no LoginSuccess yet. "
                    "Ensure loader connects to 127.0.0.1:%s (patched SWF)."
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
            if not got_login:
                logger.warning(
                    "Slot %s: no login success within %ss — finish manually or fix loader/proxy; continuing.",
                    st.label,
                    login_timeout,
                )
            time.sleep(max(0.0, stagger_after))
    _wait_for_all_slots_logged_in(states, cfg)

    while True:
        room = input("Target room (text after /room, e.g. *Racing1): ").strip()
        logger.info("Target room entered: %r", room)
        if not room:
            logger.info("Empty room; try again.")
            continue
        target = input("Target user (nickname#tag, e.g. adrian#8912): ").strip()
        logger.info("Target user entered: %r", target)
        if not target:
            logger.info("Empty user; try again.")
            continue

        run_ban_round(states, room, target, cfg)

        again = input('Ban someone else? (y/n): ').strip().lower()
        logger.info("Ban someone else? answered: %r", again)
        if again not in ("y", "yes"):
            break

    logger.info("Exiting (proxy threads stop when you close this process).")


if __name__ == "__main__":
    main()
