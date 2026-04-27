"""Lightweight TCP preflight so the bot fails fast when there is no route to the game or the internet."""
from __future__ import annotations

import logging
import os
import socket
from typing import Final

logger = logging.getLogger(__name__)

# When no game address is in env, this checks basic outbound TCP.
_DEFAULT_FALLBACK: Final[tuple[str, int, str]] = (
    "1.1.1.1",
    443,
    "Cloudflare 1.1.1.1:443 (basic connectivity)",
)


def _first_tcp_port(s: str) -> int | None:
    for part in (s or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            return int(part, 0)
        except ValueError:
            continue
    return None


def _game_host_and_port() -> tuple[str, int] | None:
    host = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or "").strip() or (os.environ.get("BOT_UPSTREAM_SERVER_ADDRESS") or "").strip()
    raw_ports = (os.environ.get("TFM_SECRETS_SERVER_PORTS") or os.environ.get("BOT_UPSTREAM_SERVER_PORTS") or "").strip()
    port = _first_tcp_port(raw_ports)
    if port is None:
        ptry = (os.environ.get("BOT_UPSTREAM_MAIN_GAME_PORT_TRY_FIRST") or "").strip().lower()
        if ptry and ptry not in ("none", "false", "0"):
            try:
                port = int(ptry, 0)
            except ValueError:
                port = None
    if host and port is not None and port > 0:
        return (host, port)
    return None


def _tcp_check(host: str, port: int, timeout_sec: float, desc: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            pass
    except OSError as e:
        emsg = str(e)
        msg = (
            f"Network preflight failed: could not open TCP to {host}:{port} "
            f"({desc}). {emsg}. Check your connection, VPN, or DNS. "
            f"To skip this check: --skip-net-check"
        )
        logger.error("%s", msg)
        raise SystemExit(msg) from e
    logger.info("Network preflight: OK — TCP to %s:%d (%s).", host, port, desc)


def run_network_preflight(*, timeout_sec: float = 5.0) -> None:
    """
    If ``TFM`` / ``BOT_UPSTREAM`` name a host and a port, verify TCP to the **game** host first.
    Otherwise verify generic outbound TCP (Cloudflare) so a dead default route is caught early.
    """
    game = _game_host_and_port()
    if game is not None:
        host, port = game
        _tcp_check(host, port, timeout_sec, "configured game / upstream")
        return
    host, port, label = _DEFAULT_FALLBACK
    _tcp_check(host, port, timeout_sec, label)
    logger.warning(
        "Network preflight: TFM / upstream address not set — only %s was checked. "
        "Set TFM_SECRETS_SERVER_ADDRESS and ports in bot/bot_env_defaults.py when using headless or explicit upstream.",
        label,
    )
