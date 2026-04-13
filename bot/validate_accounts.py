"""
Validate each row in ``BOT_ACCOUNTS_JSON`` by running the same headless TCP login path as the main bot.

Uses **one account per subprocess** so the proxy thread exits cleanly between checks.

Examples::

    python -m bot.validate_accounts --all
    python -m bot.validate_accounts --index 0
    python -m bot.validate_accounts --all --start 0 --limit 5

Optional ``.env`` / environment:

- ``BOT_VALIDATE_ACCOUNTS_PROXY_PORT`` — if set, every check binds this main port instead of the row's
  ``proxy_port`` (handy when your ``.env`` lists many ports but you only want one free listener).

When using ``--all``, workers after the first set ``BOT_HEADLESS_SECRETS_ALWAYS_REFRESH=false`` and
``BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS=false`` unless overridden, to save time.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from colorama import init as colorama_init
from . import flash_launch
from .ban_cli import (
    SlotState,
    _assign_listen_ports,
    _cfg_shared_flash_policy_port,
    start_all_slots,
)
from .env_setup import load_bot_config, repo_root
from .headless_client import (
    load_secrets_base,
    resolve_headless_upstream,
    start_headless_client_threads,
    sync_upstream_cfg_from_secrets,
)
from .upstream_probe import run_upstream_tcp_probe

logger = logging.getLogger(__name__)


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


def _validate_one_index(
    *,
    index: int,
    cfg: object,
    this_exe: Path,
    allow_kill: bool,
    run_probe: bool,
) -> bool:
    """Return True if headless login reaches LoginSuccess for ``cfg.ACCOUNTS[index]``."""
    raw_accounts = cfg.ACCOUNTS
    if index < 0 or index >= len(raw_accounts):
        logger.error("Account index %s out of range (0..%s).", index, len(raw_accounts) - 1)
        return False

    row = raw_accounts[index]
    label = str(row.get("label", index + 1))
    username = str(row.get("username", "") or "").strip()
    password = str(row.get("password", "") or "")
    if not username or not password.strip():
        logger.error("Slot %s: missing username or password in BOT_ACCOUNTS_JSON.", label)
        return False

    override_port = os.environ.get("BOT_VALIDATE_ACCOUNTS_PROXY_PORT", "").strip()
    port = int(override_port) if override_port else int(row["proxy_port"])

    proxy_bind = getattr(cfg, "PROXY_BIND_HOST", None)
    if isinstance(proxy_bind, str):
        proxy_bind = proxy_bind.strip() or None
    use_account_bind_ip = getattr(cfg, "PROXY_LISTEN_USE_ACCOUNT_BIND_IP", False)
    ip = str(row.get("bind_ip", "")).strip()
    if use_account_bind_ip:
        slot_listen: str | None = ip if ip else proxy_bind
    else:
        slot_listen = proxy_bind

    state = SlotState(label=label, port=port, proxy_bind_host=slot_listen)
    shared_flash_policy_port = _cfg_shared_flash_policy_port(cfg)
    if shared_flash_policy_port is not None and state.port == shared_flash_policy_port:
        logger.error(
            "proxy_port %s equals SHARED_FLASH_SOCKET_POLICY_PORT %s; pick another port or disable shared policy.",
            state.port,
            shared_flash_policy_port,
        )
        return False

    _assign_listen_ports([state], shared_flash_policy_port=shared_flash_policy_port)
    state.flash_username = username
    state.flash_password = password
    row_dict = dict(row)
    row_dict["_flash_satellite_port"] = state.satellite_port
    row_dict["_flash_policy_port"] = (
        shared_flash_policy_port if shared_flash_policy_port is not None else state.policy_port
    )
    row_dict["_flash_connect_host"] = state.proxy_bind_host if state.proxy_bind_host else "127.0.0.1"
    state.packet_loader_url = flash_launch.loader_document_url_for_row(row_dict, repo_root()) or ""

    base_secrets = load_secrets_base(cfg)
    sync_upstream_cfg_from_secrets(cfg, base_secrets)
    upstream_addr, upstream_ports = resolve_headless_upstream(cfg, base_secrets)
    if not upstream_addr or not upstream_ports:
        logger.error("Could not resolve upstream host/ports for headless.")
        return False

    if run_probe and bool(getattr(cfg, "UPSTREAM_TCP_PROBE_BEFORE_HEADLESS", True)):
        probe_outcome = run_upstream_tcp_probe(upstream_addr, upstream_ports, cfg)
        probe_results = probe_outcome.results
        if bool(getattr(cfg, "UPSTREAM_ABORT_ON_PROBE_ALL_FAILED", True)):
            n_ok = sum(1 for _p, st, _ in probe_results if st == "ok")
            if len(probe_results) > 0 and n_ok == 0:
                logger.error(
                    "Upstream probe 0/%s — cannot validate logins until TCP reaches %r.",
                    len(probe_results),
                    upstream_addr,
                )
                if probe_outcome.final_long_round_ran and probe_outcome.max_timeout_sec >= 15.0:
                    logger.error(
                        "Long probe round (up to %.0fs per port) also failed — check VPN/firewall/network.",
                        probe_outcome.max_timeout_sec,
                    )
                return False

    auth_key_fallback: int | None = None
    packet_key_sources_fallback: list | tuple | None = None
    if base_secrets is not None:
        auth_key_fallback = getattr(base_secrets, "auth_key", None)
        packet_key_sources_fallback = getattr(base_secrets, "packet_key_sources", None)

    start_all_slots(
        [state],
        this_exe=this_exe,
        allow_kill=allow_kill,
        cfg=cfg,
        main_server_address=upstream_addr,
        main_server_ports=upstream_ports,
        packet_login_auth_key_fallback=auth_key_fallback,
        packet_login_packet_key_sources_fallback=packet_key_sources_fallback,
        bootstrap_secrets=base_secrets,
    )

    start_headless_client_threads([state], [row_dict], cfg, base_secrets=base_secrets)
    ok = state.login_success_event.is_set()
    if ok:
        logger.info("VALIDATE OK index=%s label=%s username=%s", index, label, username)
    else:
        logger.error("VALIDATE FAIL index=%s label=%s username=%s (no LoginSuccess)", index, label, username)
    return ok


def _run_worker(argv: list[str]) -> None:
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
    p = argparse.ArgumentParser(description="Validate one BOT_ACCOUNTS_JSON row via headless login.")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--no-kill-stale", action="store_true")
    p.add_argument("--no-probe", action="store_true", help="Skip upstream TCP probe (faster, less safe).")
    args = p.parse_args(argv)
    cfg = load_bot_config()
    this_exe = Path(sys.executable).resolve()
    ok = _validate_one_index(
        index=args.index,
        cfg=cfg,
        this_exe=this_exe,
        allow_kill=not args.no_kill_stale,
        run_probe=not args.no_probe,
    )
    raise SystemExit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
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
    p = argparse.ArgumentParser(
        description="Validate Transformice credentials from BOT_ACCOUNTS_JSON (headless login per account).",
    )
    p.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--all", action="store_true", help="Run one subprocess per account (sequential).")
    p.add_argument("--index", type=int, help="Validate a single 0-based account index in this process.")
    p.add_argument("--start", type=int, default=0, help="First index for --all (default 0).")
    p.add_argument("--limit", type=int, default=None, help="Max number of accounts to check from --start.")
    p.add_argument("--no-kill-stale", action="store_true", help="Do not kill stale listeners on bot ports.")
    p.add_argument("--no-probe", action="store_true", help="Pass through to workers: skip TCP probe.")
    args = p.parse_args(argv)

    if args.worker:
        _run_worker([x for x in argv if x != "--worker"])
        return

    if args.index is not None and args.all:
        logger.error("Use either --index or --all, not both.")
        raise SystemExit(2)

    if args.index is not None:
        cfg = load_bot_config()
        this_exe = Path(sys.executable).resolve()
        ok = _validate_one_index(
            index=args.index,
            cfg=cfg,
            this_exe=this_exe,
            allow_kill=not args.no_kill_stale,
            run_probe=not args.no_probe,
        )
        raise SystemExit(0 if ok else 1)

    if not args.all:
        p.print_help()
        print(
            "\nExamples:\n"
            "  python -m bot.validate_accounts --all\n"
            "  python -m bot.validate_accounts --index 0\n",
            file=sys.stderr,
        )
        raise SystemExit(2)

    cfg = load_bot_config()
    n = len(cfg.ACCOUNTS)
    end = n if args.limit is None else min(n, args.start + max(0, args.limit))
    if args.start < 0 or args.start >= n:
        logger.error("--start out of range.")
        raise SystemExit(2)
    if end <= args.start:
        logger.error("No accounts in range.")
        raise SystemExit(2)

    extra = []
    if args.no_kill_stale:
        extra.append("--no-kill-stale")
    if args.no_probe:
        extra.append("--no-probe")

    failed: list[int] = []
    for i in range(args.start, end):
        cmd = [sys.executable, "-m", "bot.validate_accounts", "--worker", "--index", str(i), *extra]
        env = os.environ.copy()
        if i > args.start:
            env.setdefault("BOT_HEADLESS_SECRETS_ALWAYS_REFRESH", "false")
            env.setdefault("BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS", "false")
        logger.info("--- validate subprocess index=%s/%s ---", i, end - 1)
        code = subprocess.call(cmd, env=env)
        if code != 0:
            failed.append(i)

    if failed:
        logger.error(
            "VALIDATE SUMMARY: failed indices %s (%s/%s OK)",
            failed,
            end - args.start - len(failed),
            end - args.start,
        )
        raise SystemExit(1)
    logger.info("VALIDATE SUMMARY: all %s account(s) OK.", end - args.start)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
