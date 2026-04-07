"""
CMD entry: multi-slot local proxies, /room on all clients, then staggered /ban.

Prerequisite: one Transformice + tfm-proxy-loader per configured ``proxy_port``; unique IP per
process via Proxifier (``bind_ip`` in config is documentation only).
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
from dataclasses import dataclass
from pathlib import Path

from colorama import init as colorama_init

from .ban_proxy import BanBotProxy, ensure_flash_trust_config
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
    policy_port: int = 0
    proxy: BanBotProxy | None = None
    loop: asyncio.AbstractEventLoop | None = None
    thread: threading.Thread | None = None
    error: str | None = None


def _assign_listen_ports(states: list[SlotState]) -> None:
    """Set satellite and Flash socket-policy ports; prefer main±offset, else next free port."""
    used: set[int] = {s.port for s in states}

    for s in states:
        preferred_sat = s.port + SATELLITE_PORT_OFFSET
        s.satellite_port = _pick_free_port(preferred_sat, used, role="satellite", label=s.label)
        used.add(s.satellite_port)

        preferred_pol = s.port - SOCKET_POLICY_PORT_OFFSET
        s.policy_port = _pick_free_port(preferred_pol, used, role="policy", label=s.label)
        used.add(s.policy_port)

    triples = [(s.port, s.satellite_port, s.policy_port) for s in states]
    flat = [p for t in triples for p in t]
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


def _run_slot_async(state: SlotState) -> None:
    async def _run():
        try:
            proxy = BanBotProxy(
                host_main_port=state.port,
                host_satellite_port=state.satellite_port,
                host_socket_policy_port=state.policy_port,
                slot_label=state.label,
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


def start_all_slots(states: list[SlotState], *, this_exe: Path, allow_kill: bool) -> None:
    for s in states:
        for role, p in (
            ("main", s.port),
            ("satellite", s.satellite_port),
            ("policy", s.policy_port),
        ):
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
            args=(s,),
            name=f"tfm-ban-{s.port}",
            daemon=True,
        )
        s.thread = t
        t.start()

    time.sleep(1.0)


def _wait_for_game_clients(states: list[SlotState], timeout_sec: float = 600.0) -> None:
    logger.info(
        "Start %s game client(s); point each tfm-proxy-loader at its port from config.",
        len(states),
    )
    logger.info("Press Enter when all are logged in (or wait for auto-detect)…")
    # Non-blocking wait: user can press Enter early
    deadline = time.monotonic() + timeout_sec
    entered = threading.Event()

    def _read_enter():
        try:
            input()
        finally:
            entered.set()

    threading.Thread(target=_read_enter, daemon=True).start()

    while time.monotonic() < deadline:
        if entered.is_set():
            return
        if all(s.proxy and s.proxy.main_clients for s in states):
            logger.info("All configured slots have a connected client.")
            return
        time.sleep(0.4)
    logger.info(
        "Timeout waiting for connections — continuing anyway (some /room or /ban may fail).",
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
        logger.info("[slot %s] /room → %s", s.label, "sent" if ok else "FAILED")
        time.sleep(stagger)

    time.sleep(0.5)

    active = [s for s in states if s.proxy is not None]
    for i, s in enumerate(active):
        ok = _run_coro_on_slot(s, s.proxy.send_ban_command(target_user))
        logger.info(
            "[slot %s] /ban %r → %s",
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    colorama_init()
    _configure_logging()
    args = _parse_args(argv)
    cfg = _load_accounts_module()

    raw_accounts = cfg.ACCOUNTS
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
                    f"Duplicate bind_ip {ip!r} — each account needs a unique IP (Proxifier mapping)."
                )
                logger.error(msg)
                raise SystemExit(msg)
            seen_ips.add(ip)
        label = str(row.get("label", i + 1))
        states.append(SlotState(label=label, port=port))

    _assign_listen_ports(states)

    this_exe = Path(sys.executable).resolve()
    allow_kill = not args.no_kill_stale

    logger.info(
        "Ban bot — main ports: %s",
        ", ".join(str(s.port) for s in states),
    )
    start_all_slots(states, this_exe=this_exe, allow_kill=allow_kill)
    _wait_for_game_clients(states)

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
