"""Optional source binding for outbound TCP to the game server (multi-WAN / Proxifier).

When every account row carries a distinct ``bind_ip``, traffic must often leave via that address,
not the OS default route (e.g. Wi‑Fi). ``asyncio.open_connection(..., local_addr=(ip, 0))`` and
``socket.create_connection(..., source_address=(ip, 0))`` select the interface owning ``ip``.
"""

from __future__ import annotations

import json
import os
import re

_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$",
)


def parse_ipv4_literal(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if _IPV4_RE.match(s):
        return s
    return None


def account_bind_ip_for_socket_enabled() -> bool:
    """True when per-row ``bind_ip`` should set Python upstream ``local_addr`` / ``source_address``."""
    return (os.environ.get("BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def upstream_local_bind_tuple(
    *,
    account_bind_ip: str = "",
) -> tuple[str, int] | None:
    """Return ``(ipv4, 0)`` for ``local_addr`` / ``source_address``, or ``None``."""
    raw_global = (os.environ.get("BOT_UPSTREAM_LOCAL_BIND_IPV4") or "").strip()
    if raw_global.lower() not in ("", "none", "false", "0"):
        ip = parse_ipv4_literal(raw_global)
        return (ip, 0) if ip else None

    if not account_bind_ip_for_socket_enabled():
        return None
    ip = parse_ipv4_literal(account_bind_ip)
    return (ip, 0) if ip else None


def unique_account_bind_ipv4s_from_env() -> list[str]:
    """Distinct IPv4 ``bind_ip`` values from ``BOT_ACCOUNTS_JSON`` (Preflight runs before cfg load)."""
    raw = (os.environ.get("BOT_ACCOUNTS_JSON") or "").strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = parse_ipv4_literal(str(row.get("bind_ip") or ""))
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def env_preflight_try_account_bind_ips() -> bool:
    return (os.environ.get("BOT_NET_PREFLIGHT_TRY_ACCOUNT_BIND_IPS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
