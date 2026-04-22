"""
CMD entry: multi-slot local proxies + Flash Player UI windows, then /room on all clients and staggered /ban.

On startup the bot:
1. Starts one local proxy per account slot (each listens on a unique TCP port).
2. Loads TFM crypto secrets via TFMSecretsLeaker.swf / tfm-secrets CLI / .env so the proxy
   can connect to the upstream game server when Flash connects.
3. Starts a shared Flash socket-policy server (port 10801 by default).
4. Launches one Flash standalone projector window per slot, each loading a patched
   ``TFMProxyLoader.swf`` that connects to its local proxy port.
5. Waits for all slots to log in (LoginSuccessPacket), then prompts for room + target user.

Flash Player path: place ``flashplayer_32_sa_debug.exe`` (or ``flashplayer_32_sa.exe``) beside
the bot, or set ``BOT_UI_FLASH_PLAYER_PATH`` / ``FLASHPLAYER`` environment variable.

You can also open Flash Player manually: File > Open > select the patched
``TFMProxyLoader_{slot}.swf`` from the ``tmp/`` directory, choose Transformice, and log in.
Pass ``--no-ui`` to skip auto-launching Flash windows entirely (manual connect only).
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
from caseus import Secrets

from .ban_proxy import BanBotProxy
from . import flash_launch
from .env_setup import env_truthy, load_bot_config, repo_root
from .secrets_loader import load_secrets, sync_upstream_cfg_from_secrets, resolve_upstream
from .portutil import ensure_port_free_or_kill_same_bot, tcp_port_is_free

logger = logging.getLogger(__name__)

# caseus.Proxy defaults use main 11801, satellite 12801, policy 10801 (+1000 / -1000).
# Those defaults are shared by every slot unless overridden — only one process can bind.
# Use larger offsets so derived ports stay unique for typical proxy_port ranges.
SATELLITE_PORT_OFFSET = 10_000
_PORT_SCAN_SPAN = 50_000
_MIN_AUX_PORT = 1024


def _pick_free_port(preferred: int, used: set[int], *, role: str, label: str) -> int:
    """Use ``preferred`` if unused and free; otherwise scan forward."""
    start = max(_MIN_AUX_PORT, preferred)
    for p in range(start, start + _PORT_SCAN_SPAN):
        if p in used:
            continue
        if tcp_port_is_free(p):
            if p != preferred:
                logger.info(
                    "Slot %s: %s port %s was busy or reserved; using %s",
                    label, role, preferred, p,
                )
            return p
    msg = (
        f"Slot {label}: no free {role} TCP port in [{start}, {start + _PORT_SCAN_SPAN}). "
        "Close other programs or change proxy_port values in config."
    )
    logger.error(msg)
    raise SystemExit(msg)


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
    flash_username: str = ""
    flash_password: str = ""
    # file:///…swf URL for LoginPacket.loader_url (same as patched loader).
    packet_loader_url: str = ""


def _assign_listen_ports(
    states: list[SlotState],
    *,
    shared_flash_policy_port: int | None,
) -> None:
    """Assign unique satellite ports; policy port is embedded in loader URL only."""
    used: set[int] = {s.port for s in states}
    if shared_flash_policy_port is not None:
        used.add(shared_flash_policy_port)

    for s in states:
        preferred_sat = s.port + SATELLITE_PORT_OFFSET
        s.satellite_port = _pick_free_port(preferred_sat, used, role="satellite", label=s.label)
        used.add(s.satellite_port)
        # Policy port is always the shared one; no per-slot policy servers.
        s.policy_port = None

    flat = [s.port for s in states] + [s.satellite_port for s in states]
    if len(flat) != len(set(flat)):
        dup = [p for p, n in Counter(flat).items() if n > 1]
        msg = f"Listen port collision after assignment. Duplicates: {dup!r}"
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

    log_path = repo_root() / "log.txt"
    file_h = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    logging.info("Logging to %s", log_path)


def _run_slot_async(
    state: SlotState,
    cfg: object,
    *,
    main_server_address: str | None = None,
    main_server_ports: tuple[int, ...] | None = None,
    packet_login_auth_key_fallback: int | None = None,
    packet_login_packet_key_sources_fallback: list | tuple | None = None,
    bootstrap_secrets: Secrets | None = None,
) -> None:
    async def _run():
        try:
            proxy = BanBotProxy(
                host_address=state.proxy_bind_host,
                host_main_port=state.port,
                host_satellite_port=state.satellite_port,
                host_socket_policy_port=None,
                slot_label=state.label,
                login_success_event=state.login_success_event,
                verbose_login_flow=bool(getattr(cfg, "PROXY_VERBOSE_LOGIN_FLOW", False)),
                log_all_main_packets=bool(getattr(cfg, "PROXY_LOG_ALL_MAIN_PACKETS", False)),
                login_diagnostics=bool(getattr(cfg, "PROXY_LOGIN_DIAGNOSTICS", True)),
                packet_login_username=state.flash_username,
                packet_login_password=state.flash_password,
                packet_login_loader_url=state.packet_loader_url,
                packet_login_delay_sec=float(getattr(cfg, "PACKET_LOGIN_DELAY_SEC", 0.35) or 0.35),
                packet_login_start_room=str(getattr(cfg, "PACKET_LOGIN_START_ROOM", "") or ""),
                main_server_address=main_server_address,
                main_server_ports=main_server_ports,
                upstream_connect_diag=bool(getattr(cfg, "PROXY_UPSTREAM_CONNECT_DIAG", True)),
                upstream_connect_shuffle_ports=bool(getattr(cfg, "UPSTREAM_CONNECT_SHUFFLE_PORTS", False)),
                packet_login_auth_key_fallback=packet_login_auth_key_fallback,
                packet_login_packet_key_sources_fallback=packet_login_packet_key_sources_fallback,
                bootstrap_secrets=bootstrap_secrets,
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
    main_server_address: str | None = None,
    main_server_ports: tuple[int, ...] | None = None,
    packet_login_auth_key_fallback: int | None = None,
    packet_login_packet_key_sources_fallback: list | tuple | None = None,
    bootstrap_secrets: Secrets | None = None,
) -> None:
    for s in states:
        for role, p in (("main", s.port), ("satellite", s.satellite_port)):
            if not ensure_port_free_or_kill_same_bot(p, this_exe=this_exe, allow_kill=allow_kill):
                msg = f"{role} port {p} (slot {s.label}) is in use. Free it or change proxy_port in config."
                logger.error(msg)
                raise SystemExit(msg)

    for s in states:
        t = threading.Thread(
            target=_run_slot_async,
            args=(s, cfg),
            kwargs={
                "main_server_address": main_server_address,
                "main_server_ports": main_server_ports,
                "packet_login_auth_key_fallback": packet_login_auth_key_fallback,
                "packet_login_packet_key_sources_fallback": packet_login_packet_key_sources_fallback,
                "bootstrap_secrets": bootstrap_secrets,
            },
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
        n, timeout_sec,
    )
    deadline = time.monotonic() + timeout_sec
    poll_sec = 2.0
    while time.monotonic() < deadline:
        pending = [s for s in states if s.proxy is not None and not s.login_success_event.is_set()]
        if not pending:
            logger.info("All %s slot(s) logged in.", n)
            return
        time.sleep(poll_sec)
    labels = [s.label for s in states if s.proxy is not None and not s.login_success_event.is_set()]
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
        logger.info("[slot %s] /ban %r -> %s", s.label, target_user, "sent" if ok else "FAILED")
        if i < len(active) - 1:
            time.sleep(random.uniform(dmin, dmax))

    logger.info("All accounts finished sending /ban commands for this round.")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transformice multi-slot /room + /ban bot (Flash UI + local proxy).")
    p.add_argument(
        "--no-kill-stale",
        action="store_true",
        help="Do not kill processes already listening on configured proxy ports.",
    )
    p.add_argument(
        "--no-ui",
        action="store_true",
        help=(
            "Do not auto-launch Flash Player windows. Start proxies only; "
            "open flashplayer_32_sa_debug.exe manually, File > Open, select the patched SWF from tmp/."
        ),
    )
    p.add_argument(
        "--ui-sequential",
        action="store_true",
        help=(
            "Open Flash windows one at a time: launch slot 1, wait for it to log in, "
            "then launch slot 2, and so on. Also enabled by BOT_UI_SEQUENTIAL_LOGIN=true. "
            "Timeout per slot: BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC (default 120 s)."
        ),
    )
    return p.parse_args(argv)


def _launch_ui_flash_players(states: list[SlotState], cfg: object, args: argparse.Namespace) -> None:
    """
    Resolve the Flash standalone projector and launch one window per slot.

    If no projector is found the function logs an error and returns — the user
    can open Flash Player manually instead.
    """
    root = repo_root()
    flash_exe = flash_launch.resolve_flash_player(root)
    if flash_exe is None:
        logger.error(
            "UI mode: no Flash standalone projector found. Place flashplayer_32_sa.exe or "
            "flashplayer_32_sa_debug.exe beside the bot, or set BOT_UI_FLASH_PLAYER_PATH / FLASHPLAYER. "
            "Open Flash manually: File > Open > select the patched SWF from tmp/."
        )
        return

    logger.info("UI mode: Flash projector → %s", flash_exe)

    # Trust all SWF directories so Flash does not show a security dialog.
    if sys.platform == "win32":
        swf_dirs = []
        for s in states:
            if not s.packet_loader_url:
                continue
            uri = s.packet_loader_url.split("?")[0]
            local = uri[len("file:///"):] if uri.startswith("file:///") else uri[len("file://"):]
            swf_dirs.append(Path(local).parent)
        swf_dirs = list({str(d): d for d in swf_dirs}.values())
    else:
        swf_dirs = list({
            Path(s.packet_loader_url.split("?")[0].replace("file:///", "/").replace("file://", "/")).parent
            for s in states if s.packet_loader_url
        })

    flash_launch.ensure_flash_trust(swf_dirs)

    stagger = max(0.0, float(getattr(cfg, "UI_FLASH_LAUNCH_STAGGER_SEC", 1.0) or 1.0))
    sequential = bool(getattr(cfg, "UI_SEQUENTIAL_LOGIN", False)) or bool(getattr(args, "ui_sequential", False))
    per_slot_timeout = float(getattr(cfg, "UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC", 120.0) or 120.0)

    n_total = sum(1 for s in states if s.packet_loader_url)
    launched = 0

    for i, s in enumerate(states):
        if not s.packet_loader_url:
            logger.warning(
                "UI-mode: slot %s has no loader URL (TFMProxyLoader.swf missing or TFM_PROXY_SWF not set). "
                "Skipping Flash launch for this slot.",
                s.label,
            )
            continue

        swf_local = flash_launch.swf_local_path_from_url(s.packet_loader_url)
        if swf_local is not None:
            if not swf_local.is_file():
                logger.warning(
                    "UI-mode: slot %s SWF not found on disk (%s). Attempting to regenerate ...",
                    s.label, swf_local,
                )
                row_dict = {
                    "proxy_port": s.port,
                    "_flash_satellite_port": s.satellite_port,
                    "_flash_policy_port": s.policy_port,
                    "_flash_connect_host": s.proxy_bind_host if s.proxy_bind_host else "127.0.0.1",
                }
                new_url = flash_launch.loader_document_url_for_row(row_dict, root) or ""
                if new_url:
                    s.packet_loader_url = new_url
                    logger.info("UI-mode: slot %s SWF regenerated → %s", s.label, new_url)
                else:
                    logger.error(
                        "UI-mode: slot %s could not regenerate SWF. "
                        "Check that TFMProxyLoader.swf exists in the repo root.",
                        s.label,
                    )
                    continue

        proc = flash_launch.launch_flash_player_for_slot(s.packet_loader_url, flash_exe, label=s.label)
        if proc is None:
            continue
        launched += 1

        if sequential and i < len(states) - 1:
            logger.info(
                "UI sequential: waiting for slot %s to log in before opening next window (timeout %.0fs) ...",
                s.label, per_slot_timeout,
            )
            logged_in = s.login_success_event.wait(timeout=per_slot_timeout)
            if logged_in:
                logger.info("UI sequential: slot %s logged in — opening next window.", s.label)
            else:
                logger.warning(
                    "UI sequential: slot %s did not log in within %.0fs — opening next window anyway.",
                    s.label, per_slot_timeout,
                )
        elif stagger > 0 and i < len(states) - 1:
            time.sleep(stagger)

    logger.info(
        "UI mode: launched %s/%s Flash window(s). Each window connects to its proxy and logs in automatically.",
        launched, n_total,
    )


def _cfg_shared_flash_policy_port(cfg) -> int | None:
    """Policy port embedded in ``LoginPacket.loader_url`` when shared mode is enabled (default 10801)."""
    raw = getattr(cfg, "SHARED_FLASH_SOCKET_POLICY_PORT", 10801)
    if raw is None or raw is False:
        return None
    p = int(raw)
    return p if p > 0 else None


_FLASH_POLICY_XML = (
    b'<cross-domain-policy>'
    b'<allow-access-from domain="*" to-ports="*" secure="false" />'
    b'</cross-domain-policy>\x00'
)


def _start_shared_flash_policy_server(port: int | None, cfg: object) -> None:
    """Start a shared Flash socket-policy server in a background thread.

    Flash Player requests a cross-domain policy on ``xmlsocket://127.0.0.1:<port>``.
    Without a server responding with the allow-all XML, Flash blocks all socket
    connections even for SWFs in a trusted directory.
    """
    if port is None:
        return

    bind_host = getattr(cfg, "PROXY_BIND_HOST", None) or "0.0.0.0"

    async def _policy_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(_FLASH_POLICY_XML)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve() -> None:
        srv = await asyncio.start_server(_policy_handler, bind_host, port)
        logger.info("Shared Flash policy server listening on %s:%s", bind_host, port)
        async with srv:
            await srv.serve_forever()

    def _thread_main() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        except Exception:
            logger.exception("Shared Flash policy server crashed on port %s", port)
        finally:
            loop.close()

    t = threading.Thread(target=_thread_main, name=f"flash-policy-{port}", daemon=True)
    t.start()
    logger.info("Shared Flash policy server thread started (port %s).", port)


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
    cfg = load_bot_config()

    this_exe = Path(sys.executable).resolve()
    allow_kill = not args.no_kill_stale

    proxy_bind = getattr(cfg, "PROXY_BIND_HOST", None)
    if isinstance(proxy_bind, str):
        proxy_bind = proxy_bind.strip() or None

    raw_accounts = cfg.ACCOUNTS
    for i, row in enumerate(raw_accounts):
        label = str(row.get("label", i + 1))
        u = str(row.get("username", "") or "").strip()
        pw = str(row.get("password", "") or "")
        if not u or not pw.strip():
            logger.error(
                "BOT_ACCOUNTS_JSON slot %s: username and password must be non-empty (required for proxy LoginPacket).",
                label,
            )
            raise SystemExit(1)

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
                msg = f"Duplicate bind_ip {ip!r} — each account needs a unique IP (Proxifier mapping)."
                logger.error(msg)
                raise SystemExit(msg)
            seen_ips.add(ip)
        label = str(row.get("label", i + 1))
        slot_listen: str | None = (ip if ip else proxy_bind) if use_account_bind_ip else proxy_bind
        states.append(SlotState(label=label, port=port, proxy_bind_host=slot_listen))

    if not use_account_bind_ip and any(str(row.get("bind_ip", "")).strip() for row in raw_accounts):
        logger.info(
            "PROXY_LISTEN_USE_ACCOUNT_BIND_IP is False: per-row bind_ip is ignored for listening "
            "(use it in Proxifier only). Proxies use distinct ports; clients connect to 127.0.0.1."
        )

    shared_flash_policy_port = _cfg_shared_flash_policy_port(cfg)

    if shared_flash_policy_port is not None:
        for s in states:
            if s.port == shared_flash_policy_port:
                msg = (
                    f"Slot {s.label}: proxy_port {s.port} equals SHARED_FLASH_SOCKET_POLICY_PORT "
                    f"({shared_flash_policy_port}); use a different main port or set SHARED_FLASH_SOCKET_POLICY_PORT=None."
                )
                logger.error(msg)
                raise SystemExit(msg)

    _assign_listen_ports(states, shared_flash_policy_port=shared_flash_policy_port)

    for s in states:
        logger.info(
            "Slot %s: listen_host=%r main=%s satellite=%s loader_url_policy_port=%s",
            s.label, s.proxy_bind_host, s.port, s.satellite_port,
            shared_flash_policy_port if shared_flash_policy_port is not None else "(per-slot disabled)",
        )

    for s, row in zip(states, raw_accounts):
        s.flash_username = str(row.get("username", "") or "")
        s.flash_password = str(row.get("password", "") or "")

    for row, st in zip(raw_accounts, states):
        row_dict = dict(row)
        row_dict["_flash_satellite_port"] = st.satellite_port
        row_dict["_flash_policy_port"] = shared_flash_policy_port if shared_flash_policy_port is not None else st.policy_port
        row_dict["_flash_connect_host"] = st.proxy_bind_host if st.proxy_bind_host else "127.0.0.1"
        st.packet_loader_url = flash_launch.loader_document_url_for_row(row_dict, repo_root()) or ""

    # Load TFM secrets and upstream configuration so the proxy can connect to the game server
    # when Flash Player sends its HandshakePacket.
    logger.info("Loading TFM secrets for proxy upstream configuration ...")
    upstream_addr: str | None = None
    upstream_ports: tuple[int, ...] | None = None
    base_secrets: Secrets | None = None
    try:
        base_secrets = load_secrets(cfg)
        sync_upstream_cfg_from_secrets(cfg, base_secrets)
        upstream_addr, upstream_ports = resolve_upstream(cfg, base_secrets)
        logger.info("Proxy upstream configured → %s ports %s", upstream_addr, upstream_ports)
    except SystemExit:
        logger.warning(
            "Failed to load TFM secrets. Flash windows may not be able to connect to the game server. "
            "Place flashplayer_32_sa_debug.exe in the repo root, or fill TFM_SECRETS_* in .env."
        )
        base_secrets = None

    auth_key_fallback: int | None = None
    packet_key_sources_fallback: list | tuple | None = None
    if base_secrets is not None:
        auth_key_fallback = getattr(base_secrets, "auth_key", None)
        packet_key_sources_fallback = getattr(base_secrets, "packet_key_sources", None)

    start_all_slots(
        states,
        this_exe=this_exe,
        allow_kill=allow_kill,
        cfg=cfg,
        main_server_address=upstream_addr,
        main_server_ports=upstream_ports,
        packet_login_auth_key_fallback=auth_key_fallback,
        packet_login_packet_key_sources_fallback=packet_key_sources_fallback,
        bootstrap_secrets=base_secrets,
    )

    # Start shared Flash socket-policy server (Flash needs this before it allows socket connections).
    _start_shared_flash_policy_server(shared_flash_policy_port, cfg)

    # Launch Flash Player windows unless the user passed --no-ui.
    if not args.no_ui:
        _launch_ui_flash_players(states, cfg, args)
    else:
        logger.info(
            "--no-ui: skipping Flash auto-launch. "
            "Open flashplayer_32_sa_debug.exe manually, File > Open, and select the patched SWF from tmp/."
        )

    _wait_for_all_slots_logged_in(states, cfg)

    while True:
        room = input("Target room (text after /room, e.g. *Racing1): ").strip().lstrip("\ufeff")
        logger.info("Target room entered: %r", room)
        if not room:
            logger.info("Empty room; try again.")
            continue
        target = input("Target user (nickname#tag, e.g. adrian#8912): ").strip().lstrip("\ufeff")
        logger.info("Target user entered: %r", target)
        if not target:
            logger.info("Empty user; try again.")
            continue

        run_ban_round(states, room, target, cfg)

        again = input("Ban someone else? (y/n): ").strip().lstrip("\ufeff").lower()
        logger.info("Ban someone else? answered: %r", again)
        if again not in ("y", "yes"):
            break

    logger.info("Exiting (proxy threads stop when you close this process).")


if __name__ == "__main__":
    main()
