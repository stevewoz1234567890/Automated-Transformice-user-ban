"""
TCP-only checks to the game host (no Transformice packets).

Use this to separate **network / firewall / WinError 121** from **handshake / secrets** issues.
Runs once before headless login when ``PROXY_LOGIN_DIAGNOSTICS`` and
``UPSTREAM_TCP_PROBE_BEFORE_HEADLESS`` are true.

Manual run::

    python -m bot.upstream_probe 51.38.60.113 11801 12801 13801 14801
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Iterable

logger = logging.getLogger(__name__)


async def _probe_one_port(
    host: str,
    port: int,
    *,
    timeout_sec: float,
) -> tuple[int, str, float | None]:
    t0 = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_sec,
        )
        dt = time.monotonic() - t0
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            pass
        return port, "ok", dt
    except TimeoutError:
        return port, f"timeout (>={timeout_sec:.1f}s)", None
    except OSError as e:
        return port, f"os:{type(e).__name__}:{e!s}", None
    except Exception as e:
        return port, f"{type(e).__name__}:{e!s}", None


async def probe_upstream_tcp_parallel(
    host: str,
    ports: Iterable[int],
    *,
    timeout_sec: float,
) -> list[tuple[int, str, float | None]]:
    ports_list = [int(p) for p in ports]
    tasks = [_probe_one_port(host, p, timeout_sec=timeout_sec) for p in ports_list]
    return list(await asyncio.gather(*tasks))


async def log_upstream_tcp_probe_async(
    host: str,
    ports: tuple[int, ...],
    *,
    timeout_sec: float,
) -> list[tuple[int, str, float | None]]:
    logger.info(
        "[probe] Parallel TCP connect test host=%r ports=%s timeout=%.1fs per port (no game protocol)",
        host,
        ports,
        timeout_sec,
    )
    t0 = time.monotonic()
    results = await probe_upstream_tcp_parallel(host, ports, timeout_sec=timeout_sec)
    elapsed = time.monotonic() - t0
    ok = [r for r in results if r[1] == "ok"]
    bad = [r for r in results if r[1] != "ok"]
    for port, status, dt in sorted(results, key=lambda x: x[0]):
        if status == "ok":
            logger.info("[probe] %s:%s -> %s in %.3fs", host, port, status, dt or 0.0)
        else:
            logger.warning("[probe] %s:%s -> FAILED %s", host, port, status)
    if not ok:
        logger.warning(
            "[probe] SUMMARY: 0/%s ports connected in %.2fs - reachability failure "
            "(firewall, VPN, ISP, or remote block). Not a tfm-secrets/handshake issue.",
            len(results),
            elapsed,
        )
    elif bad:
        logger.warning(
            "[probe] SUMMARY: %s/%s ports OK in %.2fs - some ports fail; proxy may still work on an OK port.",
            len(ok),
            len(results),
            elapsed,
        )
    else:
        logger.info(
            "[probe] SUMMARY: all %s ports accepted TCP in %.2fs - if login still fails, suspect handshake/token/server policy.",
            len(ok),
            elapsed,
        )
    return results


def run_upstream_tcp_probe(host: str, ports: tuple[int, ...], cfg: object) -> list[tuple[int, str, float | None]]:
    """Log parallel TCP probes. Safe to call from sync code (uses asyncio.run)."""
    timeout = float(getattr(cfg, "UPSTREAM_PROBE_TIMEOUT_SEC", 6.0) or 6.0)
    timeout = max(1.0, min(timeout, 120.0))
    return asyncio.run(
        log_upstream_tcp_probe_async(host, ports, timeout_sec=timeout),
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(
        description="TCP connect probe to game ports (diagnose WinError 121 / firewall).",
    )
    p.add_argument("host", help="e.g. 51.38.60.113")
    p.add_argument("ports", type=int, nargs="+", help="e.g. 11801 12801 13801 14801")
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=6.0,
        help="Seconds per port (default 6)",
    )
    args = p.parse_args(argv)

    class _Cfg:
        UPSTREAM_PROBE_TIMEOUT_SEC = args.timeout

    results = run_upstream_tcp_probe(args.host, tuple(args.ports), _Cfg())
    if not any(status == "ok" for _port, status, _dt in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
