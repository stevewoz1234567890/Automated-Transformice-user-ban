"""
CMD entry: multi-slot local proxies, /room on all clients, then staggered /ban.

Prerequisite: one Transformice + tfm-proxy-loader per configured ``proxy_port``; unique IP per
process via Proxifier (``bind_ip`` in config is documentation only).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from colorama import init as colorama_init

from .ban_proxy import BanBotProxy, ensure_flash_trust_config
from .portutil import ensure_port_free_or_kill_same_bot

logger = logging.getLogger(__name__)


def _load_accounts_module():
    try:
        import bot.config as cfg  # type: ignore
    except ImportError as e:
        print(
            "Missing bot.config. Copy bot/config.example.py to bot/config.py and edit ACCOUNTS.",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    accounts = getattr(cfg, "ACCOUNTS", None)
    if not accounts:
        print("config.ACCOUNTS is empty.", file=sys.stderr)
        raise SystemExit(1)
    return cfg


@dataclass
class SlotState:
    label: str
    port: int
    proxy: BanBotProxy | None = None
    loop: asyncio.AbstractEventLoop | None = None
    thread: threading.Thread | None = None
    error: str | None = None


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root.addHandler(h)


def _run_slot_async(state: SlotState) -> None:
    async def _run():
        try:
            proxy = BanBotProxy(host_main_port=state.port, slot_label=state.label)
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
        if not ensure_port_free_or_kill_same_bot(s.port, this_exe=this_exe, allow_kill=allow_kill):
            raise SystemExit(
                f"Port {s.port} (slot {s.label}) is in use. Free it or change proxy_port in config."
            )

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
    print(f"Start {len(states)} game client(s); point each tfm-proxy-loader at its port from config.")
    print("Press Enter when all are logged in (or wait for auto-detect)…")
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
            print("All configured slots have a connected client.")
            return
        time.sleep(0.4)
    print("Timeout waiting for connections — continuing anyway (some /room or /ban may fail).")


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
        print(f"[slot {s.label}] /room → {'sent' if ok else 'FAILED'}")
        time.sleep(stagger)

    time.sleep(0.5)

    active = [s for s in states if s.proxy is not None]
    for i, s in enumerate(active):
        ok = _run_coro_on_slot(s, s.proxy.send_ban_command(target_user))
        print(f"[slot {s.label}] /ban {target_user!r} → {'sent' if ok else 'FAILED'}")
        if i < len(active) - 1:
            time.sleep(random.uniform(dmin, dmax))

    print("All accounts finished sending /ban commands for this round.")


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
            raise SystemExit(f"Duplicate proxy_port: {port}")
        seen_ports.add(port)
        ip = str(row.get("bind_ip", "")).strip()
        if ip:
            if ip in seen_ips:
                raise SystemExit(
                    f"Duplicate bind_ip {ip!r} — each account needs a unique IP (Proxifier mapping)."
                )
            seen_ips.add(ip)
        label = str(row.get("label", i + 1))
        states.append(SlotState(label=label, port=port))

    this_exe = Path(sys.executable).resolve()
    allow_kill = not args.no_kill_stale

    print("Ban bot — ports:", ", ".join(str(s.port) for s in states))
    start_all_slots(states, this_exe=this_exe, allow_kill=allow_kill)
    _wait_for_game_clients(states)

    while True:
        room = input("Target room (text after /room, e.g. *Racing1): ").strip()
        if not room:
            print("Empty room; try again.")
            continue
        target = input("Target user (nickname#tag, e.g. adrian#8912): ").strip()
        if not target:
            print("Empty user; try again.")
            continue

        run_ban_round(states, room, target, cfg)

        again = input('Ban someone else? (y/n): ').strip().lower()
        if again not in ("y", "yes"):
            break

    print("Exiting (proxy threads stop when you close this process).")


if __name__ == "__main__":
    main()
