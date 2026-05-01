"""TCP preflight and extended startup network diagnostics.

Fails fast when there is no route to the game (or the internet, when no host is configured).
Optional extended block logs DNS, local IPv4 hints, HTTP proxy env vars (vs raw TCP used by the
game proxy), and multi-port upstream reachability — useful when the bot works on some PCs /
networks (Wi‑Fi, mobile hotspot, corporate) but not others.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from typing import Final

logger = logging.getLogger(__name__)

# When no game address is in env, this checks basic outbound TCP.
_DEFAULT_FALLBACK: Final[tuple[str, int, str]] = (
    "1.1.1.1",
    443,
    "Cloudflare 1.1.1.1:443 (basic connectivity)",
)


def _env_extended() -> bool:
    return (os.environ.get("BOT_NET_PREFLIGHT_EXTENDED") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def _env_require_all_game_ports() -> bool:
    """When true, multi-port preflight fails unless every configured port accepts TCP (parity baseline)."""
    return (os.environ.get("BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _probe_timeout_sec() -> float:
    raw = (os.environ.get("BOT_UPSTREAM_PROBE_TIMEOUT_SEC") or "").strip()
    try:
        t = float(raw) if raw else 6.0
    except ValueError:
        t = 6.0
    return max(1.0, min(t, 120.0))


def _all_tcp_ports(s: str) -> list[int]:
    ports: list[int] = []
    for part in (s or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = int(part, 0)
            if p > 0:
                ports.append(p)
        except ValueError:
            continue
    seen: set[int] = set()
    uniq: list[int] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _first_tcp_port(s: str) -> int | None:
    for p in _all_tcp_ports(s):
        return p
    return None


def _game_host_and_ports() -> tuple[str, list[int]] | None:
    host = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or "").strip() or (
        os.environ.get("BOT_UPSTREAM_SERVER_ADDRESS") or ""
    ).strip()
    raw_ports = (
        os.environ.get("TFM_SECRETS_SERVER_PORTS")
        or os.environ.get("BOT_UPSTREAM_SERVER_PORTS")
        or ""
    ).strip()
    ports = _all_tcp_ports(raw_ports)
    if not ports:
        ptry = (os.environ.get("BOT_UPSTREAM_MAIN_GAME_PORT_TRY_FIRST") or "").strip().lower()
        if ptry and ptry not in ("none", "false", "0"):
            try:
                p = int(ptry, 0)
                if p > 0:
                    ports = [p]
            except ValueError:
                ports = []
    if host and ports:
        return (host, ports)
    return None


def _game_host_and_port_minimal() -> tuple[str, int] | None:
    """Single-port view for legacy behavior when extended multi-port is disabled."""
    gp = _game_host_and_ports()
    if gp is None:
        return None
    host, ports = gp
    if not ports:
        return None
    return (host, ports[0])


def _tcp_check(host: str, port: int, timeout_sec: float, desc: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            pass
    except OSError as e:
        emsg = str(e)
        msg = (
            f"[preflight] FATAL: could not open TCP to {host}:{port} "
            f"({desc}). {emsg}. Check your connection, VPN, or DNS. "
            f"To skip this check: --skip-net-check"
        )
        logger.error("%s", msg)
        raise SystemExit(msg) from e
    logger.info("[preflight] OK — TCP to %s:%d (%s).", host, port, desc)


def _log_dns(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        logger.warning(
            "[preflight] DNS: getaddrinfo(%r) failed (%s: %s) — TCP may still work if the app uses a cached IP.",
            host,
            type(e).__name__,
            e,
        )
        return
    v4 = sorted({x[4][0] for x in infos if x[0] == socket.AF_INET})
    v6 = sorted({x[4][0] for x in infos if x[0] == socket.AF_INET6})
    if v4:
        logger.info("[preflight] DNS: %r → IPv4 %s", host, v4)
    if v6:
        logger.info("[preflight] DNS: %r → IPv6 %s (game proxy uses IPv4 sockets unless you force v6)", host, v6)
    if not v4 and not v6:
        logger.warning("[preflight] DNS: %r → no addresses from getaddrinfo", host)


def _log_local_ipv4_summary() -> None:
    """Best-effort non-loopback IPv4 names for this host (NAT / hotspot diagnosis)."""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "?"
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except OSError as e:
        logger.debug("[preflight] local IPv4 discovery via hostname failed: %s", e)
    if addrs:
        logger.info(
            "[preflight] This machine: hostname=%r local IPv4 (sample)=%s — outbound game TCP uses the route "
            "chosen by the OS (Wi‑Fi vs mobile tether vs VPN); per-account bind_ip is separate (see below).",
            hostname,
            sorted(addrs),
        )
    else:
        logger.info(
            "[preflight] This machine: hostname=%r — could not list non-loopback IPv4 via getaddrinfo "
            "(still OK on some networks).",
            hostname,
        )


def _log_http_proxy_env_notes() -> None:
    keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    found: list[str] = []
    for k in keys:
        v = os.environ.get(k)
        if v and str(v).strip():
            found.append(f"{k}=<set len={len(str(v).strip())}>")
    if found:
        logger.warning(
            "[preflight] Proxy-related env vars are set (%s). The Transformice proxy uses raw TCP "
            "sockets (caseus to game server), not HTTP CONNECT — these variables do not steer that traffic. "
            "Use Proxifier / per-interface routing / VPN when you need a specific outbound path per account.",
            "; ".join(found),
        )
    else:
        logger.info(
            "[preflight] No HTTP_PROXY/HTTPS_PROXY/ALL_PROXY in environment — raw upstream TCP is unconstrained "
            "by HTTP proxy settings (path is still OS routing / VPN / bind / third-party proxifiers).",
        )


def run_network_preflight(*, timeout_sec: float | None = None) -> None:
    """
    Verify TCP reachability before starting local listeners.

    * Extended (default): DNS + local IPv4 hint + HTTP-proxy env note + **all** configured game ports
      in parallel (same idea as ``bot.upstream_probe``).
    * ``BOT_NET_PREFLIGHT_EXTENDED=false``: legacy single TCP check to the first configured port only.
    """
    t = _probe_timeout_sec() if timeout_sec is None else max(1.0, min(float(timeout_sec), 120.0))
    ext = _env_extended()

    logger.info("[preflight] ========== network path (before bot proxies start) ==========")
    if _env_require_all_game_ports():
        logger.info(
            "[preflight] BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true — each configured game port must accept TCP "
            "(not only one). Matches known-good parity baseline; disable if your network blocks some ports.",
        )
    logger.info(
        "[preflight] platform=%s python=%s extended=%s timeout=%.1fs",
        sys.platform,
        sys.version.split()[0],
        ext,
        t,
    )
    frozen = bool(getattr(sys, "frozen", False))
    logger.info(
        "[preflight] Process opening upstream game TCP: exe=%r frozen=%s — "
        "route this executable in Proxifier/split-VPN when using per-account bind_ip.",
        sys.executable,
        frozen,
    )

    if ext:
        _log_local_ipv4_summary()
        _log_http_proxy_env_notes()

    game = _game_host_and_ports()
    if game is not None:
        host, ports = game
        if ext:
            _log_dns(host)
        if not ext or len(ports) == 1:
            hpm = _game_host_and_port_minimal()
            if hpm:
                h, p = hpm
                _tcp_check(h, p, t, "configured game / upstream (first port)")
            return

        from .upstream_probe import log_upstream_tcp_probe_async

        results = asyncio.run(
            log_upstream_tcp_probe_async(host, tuple(ports), timeout_sec=t),
        )
        if not any(status == "ok" for _port, status, _dt in results):
            bad = ", ".join(f"{port}:{status}" for port, status, _dt in sorted(results, key=lambda x: x[0]))
            msg = (
                f"[preflight] FATAL: could not open TCP to game host {host!r} on any of ports {ports} "
                f"(all attempts failed: {bad}). This is reachability (firewall / ISP / VPN / captive portal / "
                f"mobile CGNAT), not loader/crypto — fix network or use --skip-net-check to bypass (not recommended)."
            )
            logger.error("%s", msg)
            raise SystemExit(msg)
        if _env_require_all_game_ports():
            failed = [(port, status) for port, status, _dt in results if status != "ok"]
            if failed:
                bad = ", ".join(f"{port}:{status}" for port, status in failed)
                msg = (
                    f"[preflight] FATAL: BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true but some ports failed "
                    f"host={host!r} failures=[{bad}]. Use one stable uplink (no tether/Wi‑Fi hopping), fix firewall, "
                    f"or set BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=false if partial connectivity is acceptable."
                )
                logger.error("%s", msg)
                raise SystemExit(msg)
            logger.info(
                "[preflight] Strict all-ports check: %s/%s OK — baseline parity satisfied for configured ports.",
                len(results),
                len(results),
            )
        else:
            logger.info(
                "[preflight] Upstream: at least one game port OK — multi-port probe helps spot partial blocks "
                "(some networks allow 12801 but not 11801, etc.). Set BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true "
                "to require every port (recommended when proving parity vs a known-good PC).",
            )
        logger.info(
            "[preflight] Reachability: startup probe success does not guarantee mid-session TCP stability "
            "(tether handoff, Wi‑Fi sleep, VPN). Log keyword upstream-tcp-all-ports-failed indicates "
            "connect-after-startup failure; re-run `python -m bot.upstream_probe %s %s`.",
            host,
            " ".join(str(p) for p in ports),
        )
        return

    host, port, label = _DEFAULT_FALLBACK
    _tcp_check(host, port, t, label)
    logger.warning(
        "[preflight] TFM / upstream address not set — only %s was checked. "
        "Set TFM_SECRETS_SERVER_ADDRESS and TFM_SECRETS_SERVER_PORTS in bot/bot_env_defaults.py when diagnosing headless or explicit upstream.",
        label,
    )


def log_proxy_listen_vs_upstream(
    *,
    cfg: object,
    states: list[object],
    raw_accounts: list[dict[str, object]],
    shared_flash_policy_port: int | None,
) -> None:
    """
    After ``.env`` is loaded: explain how **local** listeners relate to **upstream** TCP and bind_ip.

    Helps when proxies work on one PC but fail on another: wrong expectation about HTTP_PROXY vs
    Proxifier, or PROXY_LISTEN_USE_ACCOUNT_BIND_IP on a machine without those IPs.
    """
    use_bind = bool(getattr(cfg, "PROXY_LISTEN_USE_ACCOUNT_BIND_IP", False))
    bind_host = getattr(cfg, "PROXY_BIND_HOST", None)
    if isinstance(bind_host, str):
        bind_host = bind_host.strip() or None

    flash_pol_host = str(
        getattr(cfg, "FLASH_SOCKET_POLICY_BIND_HOST", "") or "127.0.0.1",
    ).strip() or "127.0.0.1"

    n_slots = len(states)
    main_ports = [getattr(s, "port", 0) for s in states]
    mm = (min(main_ports), max(main_ports)) if main_ports else (0, 0)

    rows_with_bind = sum(1 for r in raw_accounts if str(r.get("bind_ip") or "").strip())

    logger.info("[preflight] ========== local proxy layout (after .env / slots) ==========")
    logger.info(
        "[preflight] Slots=%d main_port range=%s..%s | shared Flash policy port=%s (bind %s)",
        n_slots,
        mm[0],
        mm[1],
        shared_flash_policy_port,
        flash_pol_host,
    )
    logger.info(
        "[preflight] PROXY_LISTEN_USE_ACCOUNT_BIND_IP=%s PROXY_BIND_HOST=%r",
        use_bind,
        bind_host,
    )

    if use_bind:
        logger.info(
            "[preflight] Listen/bind: each proxy listens on the row's bind_ip (must exist on this NIC). "
            "Flash loader URLs must use that same address. This is separate from outbound source IP.",
        )
    else:
        logger.info(
            "[preflight] Listen/bind: proxies listen on PROXY_BIND_HOST or all interfaces; Flash usually connects to "
            "127.0.0.1:<main_port> (+ satellite + xmlsocket policy).",
        )

    if rows_with_bind:
        logger.info(
            "[preflight] Per-row bind_ip: %d/%d account(s) have bind_ip set — with PROXY_LISTEN_USE_ACCOUNT_BIND_IP=false "
            "(default) those values are not used for listen(); they are for your Proxifier/split-routing so each "
            "slot's outbound TCP to the game server uses the intended source IP. The bot process still opens upstream "
            "sockets from Python; external tools must steer that traffic per executable or per destination.",
            rows_with_bind,
            len(raw_accounts),
        )
    else:
        logger.info("[preflight] No bind_ip in accounts — all outbound game traffic uses the OS default route.")

    gh = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or "").strip() or (
        os.environ.get("BOT_UPSTREAM_SERVER_ADDRESS") or ""
    ).strip()
    if gh:
        logger.info(
            "[preflight] Upstream target from env: game host %r — each slot's proxy maintains raw TCP to that host "
            "(main + satellite paths), independent of HTTP_PROXY.",
            gh,
        )
