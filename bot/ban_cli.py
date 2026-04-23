"""
CMD entry: multi-slot local proxies, /room on all clients, then staggered /ban.

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
                login_success_event=state.login_success_event,
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
    flash_auto_login_ui: bool,
    flash_login_main_tcp_hook: bool,
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
        if all(s.proxy and s.proxy.main_clients for s in states):
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


def fetch_room_list(
    states: list[SlotState],
    *,
    game_modes: tuple[int, ...] = (1, 2, 3, 5, 9),
    timeout_sec: float = 6.0,
) -> list[tuple[str, int]]:
    """
    Request room lists for *game_modes* from the first live slot and return
    ``[(room_name, num_players), ...]`` sorted by name.

    Game mode ints: 1=Transformice, 2=Bootcamp, 3=Vanilla, 5=Racing, 9=Module.
    """
    live = [s for s in states if s.proxy and s.proxy.main_clients]
    if not live:
        logger.warning("No live slot available to fetch room list.")
        return []

    slot = live[0]
    proxy = slot.proxy
    proxy.known_rooms.clear()
    proxy._room_list_ready.clear()

    for gm_int in game_modes:
        try:
            _run_coro_on_slot(slot, proxy.request_room_list(gm_int))
        except Exception as exc:
            logger.debug("fetch_room_list: request mode %s failed: %s", gm_int, exc)
        time.sleep(0.15)

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proxy._room_list_ready.is_set():
            time.sleep(0.8)  # let remaining responses arrive
            break
        time.sleep(0.2)

    rooms = sorted(proxy.known_rooms.items(), key=lambda x: x[0].lower())
    return rooms


def _pick_room(states: list[SlotState]) -> str:
    """
    Fetch available rooms from the game server, print a numbered list, and let
    the user pick by number or type a name directly.
    """
    _flush_log_handlers()
    logger.info("Fetching room list from game server...")
    _flush_log_handlers()

    rooms = fetch_room_list(states)

    if rooms:
        print(f"\nAvailable rooms ({len(rooms)} total):", flush=True)
        for i, (name, players) in enumerate(rooms, 1):
            print(f"  {i:3d}. {name:<30s}  [{players} players]", flush=True)
        print(flush=True)
        _flush_log_handlers()
        choice = input("Enter room number or room name (e.g. *Racing1): ").strip()
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
        return input("Target room (text after /room, e.g. *Racing1): ").strip()


def _collect_player_list_via_join(
    states: list[SlotState],
    room: str,
    *,
    timeout_sec: float = 12.0,
) -> list[str]:
    """
    Send ``JoinRoomPacket`` to the first live slot's upstream main server and
    wait for a new ``SetPlayerListPacket``.  This properly triggers the server
    to send the player list for the target room.

    Returns a sorted list of unique usernames.
    """
    live = [s for s in states if s.proxy and s.proxy.main_clients]
    if not live:
        return []

    slot = live[0]
    proxy = slot.proxy
    version_before = proxy._player_list_version

    try:
        _run_coro_on_slot(slot, proxy.join_room(room))
    except Exception as exc:
        logger.warning("_collect_player_list_via_join: join_room failed: %s", exc)
        return []

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proxy._player_list_version != version_before:
            time.sleep(0.8)  # let UpdatePlayerListPackets accumulate
            break
        time.sleep(0.2)

    if proxy._player_list_version == version_before:
        return []

    return sorted(proxy.known_players.keys(), key=str.lower)


def _show_and_pick_player(states: list[SlotState], room: str) -> str:
    """
    Use ``JoinRoomPacket`` to join *room* with the first slot, show the player
    list, and let the user pick a target by number or type a nickname directly.
    """
    _flush_log_handlers()
    logger.info("Collecting player list for %r...", room)
    _flush_log_handlers()

    players = _collect_player_list_via_join(states, room)

    if players:
        print(f"\nPlayers in {room!r} ({len(players)} total):", flush=True)
        for i, name in enumerate(players, 1):
            print(f"  {i:3d}. {name}", flush=True)
        print(flush=True)
        _flush_log_handlers()
        choice = input("Enter player number or nickname (e.g. Zizao#0000): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(players):
                chosen = players[idx]
                logger.info("Player selected by number %s: %r", choice, chosen)
                return chosen
        logger.info("Player entered directly: %r", choice)
        return choice
    else:
        logger.info("No player list received — enter nickname manually.")
        _flush_log_handlers()
        return input("Target user (nickname#tag, e.g. Zizao#0000): ").strip()


def send_room_to_all(states: list[SlotState], room: str, cfg) -> None:
    """Send /room to every slot (staggered). Call before collecting player list."""
    stagger = float(getattr(cfg, "ROOM_STAGGER_SEC", 0.15))
    for s in states:
        if s.proxy is None:
            continue
        ok = _run_coro_on_slot(s, s.proxy.send_room_command(room))
        logger.info("[slot %s] /room -> %s", s.label, "sent" if ok else "FAILED")
        time.sleep(stagger)


def send_ban_to_all(states: list[SlotState], target_user: str, cfg) -> None:
    """Send /ban to every slot (staggered). Call after player is chosen."""
    dmin = float(getattr(cfg, "BAN_DELAY_MIN_SEC", 1.0))
    dmax = float(getattr(cfg, "BAN_DELAY_MAX_SEC", 2.0))
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


def run_ban_round(states: list[SlotState], room: str, target_user: str, cfg) -> None:
    send_room_to_all(states, room, cfg)
    time.sleep(0.5)
    send_ban_to_all(states, target_user, cfg)


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
    if bool(getattr(cfg, "PACKET_AUTO_LOGIN", False)):
        cfg_flash_auto = False
        logger.info(
            "PACKET_AUTO_LOGIN=True — FLASH_AUTO_LOGIN_UI disabled (login is sent as LoginPacket by the proxy)",
        )

    def _flash_login_trigger(c) -> str:
        t = str(getattr(c, "FLASH_LOGIN_TRIGGER", "after_launch") or "after_launch").strip().lower()
        if t in ("main_tcp", "main", "tcp"):
            return "main_tcp"
        return "after_launch"

    flash_login_trigger = _flash_login_trigger(cfg)
    flash_login_main_tcp_hook = cfg_flash_auto and flash_login_trigger == "main_tcp"

    start_all_slots(
        states,
        this_exe=this_exe,
        allow_kill=allow_kill,
        cfg=cfg,
        flash_auto_login_ui=cfg_flash_auto,
        flash_login_main_tcp_hook=flash_login_main_tcp_hook,
    )

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
        stagger_after = float(getattr(cfg, "FLASH_STAGGER_AFTER_LOGIN_SEC", 0.5))
        for idx, (flash_row, st) in enumerate(zip(flash_accounts, states), start=1):
            st.login_success_event.clear()
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
            poll_dismiss = float(
                getattr(cfg, "FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC", 0.0) or 0.0
            )
            if (
                proc is not None
                and st.flash_pid is not None
                and poll_dismiss > 0
                and sys.platform == "win32"
            ):

                def _flash_error_dismiss_poll(
                    _st: SlotState = st,
                    _pd: float = poll_dismiss,
                ) -> None:
                    while True:
                        time.sleep(_pd)
                        if _st.login_success_event.is_set():
                            break
                        pid = _st.flash_pid
                        if pid is None or pid <= 0:
                            continue
                        if not flash_launch.flash_pid_is_alive(pid):
                            break
                        if not flash_launch.try_acquire_flash_ui(pid):
                            continue
                        try:
                            flash_launch.dismiss_flashplayer_actionscript_dialogs(
                                pid,
                                _st.label,
                                cfg,
                                pre_dismiss_sec=0.0,
                                quiet=True,
                            )
                        except Exception:
                            logger.debug(
                                "Slot %s: periodic Flash error dismiss failed",
                                _st.label,
                                exc_info=True,
                            )
                        finally:
                            flash_launch.release_flash_ui(pid)

                threading.Thread(
                    target=_flash_error_dismiss_poll,
                    daemon=True,
                    name=f"flash-err-dismiss-{st.label}",
                ).start()
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
            if not cfg_flash_auto:
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
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                chunk = min(poll_sec, left)
                if st.login_success_event.wait(timeout=chunk):
                    got_login = True
                    logger.info("Slot %s reported login success; proceeding.", st.label)
                    break
                if st.flash_main_tcp_seen:
                    extra_verbose = (
                        " With PROXY_VERBOSE_LOGIN_FLOW: expect a [MAIN→srv] LoginPacket after you submit; "
                        "if none appears, auto-login may be too early or submit missed — try "
                        "FLASH_LOGIN_AFTER_SUBMIT_EXTRA_SEC / FLASH_LOGIN_SECOND_SUBMIT_CLICK in .env."
                        if verbose
                        else ""
                    )
                    logger.info(
                        "Still waiting slot %s (~%.0fs left): no LoginSuccess yet. "
                        "MAIN TCP already connected — Flash reached 127.0.0.1:%s; next step is a "
                        "LoginPacket from the client. If none ever appears, login was not submitted.%s",
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
            if not got_login:
                logger.warning(
                    "Slot %s: no login success within %ss — finish manually or fix loader/proxy; continuing.",
                    st.label,
                    login_timeout,
                )
            time.sleep(max(0.0, stagger_after))
    _wait_for_game_clients(states, auto_flash_launched=auto_flash)

    while True:
        room = _pick_room(states)
        logger.info("Target room: %r", room)
        if not room:
            logger.info("Empty room; try again.")
            continue

        target = _show_and_pick_player(states, room)
        logger.info("Target user: %r", target)
        if not target:
            logger.info("Empty user; try again.")
            continue

        send_ban_to_all(states, target, cfg)

        _flush_log_handlers()
        again = input('Ban someone else? (y/n): ').strip().lower()
        logger.info("Ban someone else? answered: %r", again)
        if again not in ("y", "yes"):
            break

    logger.info("Exiting (proxy threads stop when you close this process).")


if __name__ == "__main__":
    main()
