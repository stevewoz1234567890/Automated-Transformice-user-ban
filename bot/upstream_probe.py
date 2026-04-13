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
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamProbeOutcome:
    """Result of ``run_upstream_tcp_probe`` (may include a final long-timeout round)."""

    results: list[tuple[int, str, float | None]]
    max_timeout_sec: float
    final_long_round_ran: bool


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


def run_upstream_tcp_probe(host: str, ports: tuple[int, ...], cfg: object) -> UpstreamProbeOutcome:
    """Log parallel TCP probes. Safe to call from sync code (uses asyncio.run).

    If every port fails, repeats up to ``UPSTREAM_PROBE_RETRIES`` extra times (see env), pausing
    ``UPSTREAM_PROBE_RETRY_PAUSE_SEC`` between rounds — helps transient WiFi / VPN / routing glitches.

    If still no success and the configured timeout is below the final floor, runs one more round with a
    longer per-port timeout (slow Wi‑Fi often fails at 6s but succeeds at 20s).
    """
    timeout = float(getattr(cfg, "UPSTREAM_PROBE_TIMEOUT_SEC", 10.0) or 10.0)
    timeout = max(1.0, min(timeout, 120.0))
    max_used = timeout
    if timeout < 10.0:
        logger.warning(
            "[probe] BOT_UPSTREAM_PROBE_TIMEOUT_SEC=%.1fs is aggressive; slow Wi‑Fi/VPN often needs 10–15s "
            "and will look like a dead host at 6s.",
            timeout,
        )
    extra_rounds = int(getattr(cfg, "UPSTREAM_PROBE_RETRIES", 0) or 0)
    extra_rounds = max(0, min(extra_rounds, 10))
    pause_sec = float(getattr(cfg, "UPSTREAM_PROBE_RETRY_PAUSE_SEC", 3.0) or 3.0)
    pause_sec = max(0.0, min(pause_sec, 60.0))
    max_attempts = 1 + extra_rounds
    last: list[tuple[int, str, float | None]] = []
    for attempt in range(max_attempts):
        if attempt > 0:
            logger.info(
                "[probe] round %s/%s after %.1fs pause (previous round: 0/%s ports ok)",
                attempt + 1,
                max_attempts,
                pause_sec,
                len(ports),
            )
            time.sleep(pause_sec)
        last = asyncio.run(
            log_upstream_tcp_probe_async(host, ports, timeout_sec=timeout),
        )
        if any(status == "ok" for _port, status, _dt in last):
            return UpstreamProbeOutcome(last, max_used, False)

    ran_final_long = False
    if not any(status == "ok" for _port, status, _dt in last) and bool(
        getattr(cfg, "UPSTREAM_PROBE_FINAL_LONG_TIMEOUT", True)
    ):
        final_cap = float(getattr(cfg, "UPSTREAM_PROBE_FINAL_TIMEOUT_CAP_SEC", 35.0) or 35.0)
        final_cap = max(15.0, min(final_cap, 120.0))
        final_floor = float(getattr(cfg, "UPSTREAM_PROBE_FINAL_TIMEOUT_FLOOR_SEC", 20.0) or 20.0)
        final_floor = max(10.0, min(final_floor, final_cap))
        if timeout < final_floor:
            final_t = min(final_cap, max(final_floor, timeout * 3.0))
            logger.info(
                "[probe] all rounds failed at %.1fs — trying once more at %.1fs (slow path; set "
                "BOT_UPSTREAM_PROBE_TIMEOUT_SEC higher to skip this)",
                timeout,
                final_t,
            )
            if pause_sec > 0:
                time.sleep(min(pause_sec, 2.0))
            last = asyncio.run(
                log_upstream_tcp_probe_async(host, ports, timeout_sec=final_t),
            )
            max_used = max(timeout, final_t)
            ran_final_long = True
    return UpstreamProbeOutcome(last, max_used, ran_final_long)


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

    outcome = run_upstream_tcp_probe(args.host, tuple(args.ports), _Cfg())
    if not any(status == "ok" for _port, status, _dt in outcome.results):
        sys.exit(1)


if __name__ == "__main__":
    main()
