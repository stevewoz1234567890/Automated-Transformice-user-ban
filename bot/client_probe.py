"""
Probe each known Transformice access method for reachability.

Checks:
  - HTTP HEAD/GET for official SWF URLs (alive? correct Content-Type?)
  - Local file existence for Flash projector, standalone EXE
  - Steam installation directory
  - Ruffle binary on PATH
  - TCP connectivity to known game-server ports (reuses net_preflight logic)
  - Standalone EXE download availability
"""

from __future__ import annotations

import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .game_client_registry import (
    KNOWN_SWF_ENDPOINTS,
    STANDALONE_EXE_URL,
    ClientKind,
    ClientMethod,
    SwfEndpoint,
    discover_available_methods,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SEC = 15.0
_TCP_TIMEOUT_SEC = 8.0


@dataclass
class SwfProbeResult:
    endpoint: SwfEndpoint
    reachable: bool
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    is_swf: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class TcpProbeResult:
    host: str
    port: int
    reachable: bool
    elapsed_ms: float = 0.0
    error: str | None = None


@dataclass
class ClientProbeReport:
    method: ClientMethod
    swf_probes: list[SwfProbeResult]
    tcp_probes: list[TcpProbeResult]
    notes: list[str]
    standalone_download_ok: bool | None = None


def probe_swf_endpoint(ep: SwfEndpoint) -> SwfProbeResult:
    """HTTP HEAD then GET to check whether a remote SWF URL is alive and serves Flash content."""
    t0 = time.monotonic()
    req = urllib.request.Request(
        ep.url,
        method="HEAD",
        headers={"User-Agent": "TFM-ClientProbe/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            status = resp.status
            ct = resp.headers.get("Content-Type", "")
            cl_raw = resp.headers.get("Content-Length")
            cl = int(cl_raw) if cl_raw and cl_raw.strip().isdigit() else None
    except urllib.error.HTTPError as e:
        return SwfProbeResult(
            endpoint=ep,
            reachable=False,
            http_status=e.code,
            error=str(e),
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        return SwfProbeResult(
            endpoint=ep,
            reachable=False,
            error=str(e),
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )

    is_swf = any(
        tok in (ct or "").lower()
        for tok in ("application/x-shockwave-flash", "swf", "octet-stream")
    )
    return SwfProbeResult(
        endpoint=ep,
        reachable=(200 <= status < 400),
        http_status=status,
        content_type=ct,
        content_length=cl,
        is_swf=is_swf,
        elapsed_ms=(time.monotonic() - t0) * 1000,
    )


def probe_tcp(host: str, port: int) -> TcpProbeResult:
    """Raw TCP connect to a game-server endpoint."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT_SEC) as s:
            s.shutdown(socket.SHUT_RDWR)
        return TcpProbeResult(
            host=host, port=port, reachable=True,
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )
    except OSError as e:
        return TcpProbeResult(
            host=host, port=port, reachable=False,
            error=str(e),
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )


def probe_standalone_download() -> bool:
    """HEAD request to check whether the official Transformice.exe download URL is alive."""
    req = urllib.request.Request(
        STANDALONE_EXE_URL,
        method="HEAD",
        headers={"User-Agent": "TFM-ClientProbe/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


def _game_server_targets() -> list[tuple[str, int]]:
    """Collect upstream host/ports from env for TCP probing."""
    host = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or
            os.environ.get("BOT_UPSTREAM_SERVER_ADDRESS") or "").strip()
    ports_s = (os.environ.get("TFM_SECRETS_SERVER_PORTS") or
               os.environ.get("BOT_UPSTREAM_SERVER_PORTS") or "").strip()
    if not host:
        host = "51.38.60.113"
    if not ports_s:
        ports_s = "12801,13801,14801,11801"
    ports = []
    for tok in ports_s.replace(" ", "").split(","):
        if tok.strip().isdigit():
            ports.append(int(tok.strip()))
    return [(host, p) for p in ports]


def run_full_probe(repo_root: Path) -> list[ClientProbeReport]:
    """Probe every known access method and return structured results."""
    methods = discover_available_methods(repo_root)
    reports: list[ClientProbeReport] = []

    for m in methods:
        swf_probes: list[SwfProbeResult] = []
        tcp_probes: list[TcpProbeResult] = []
        standalone_dl: bool | None = None
        notes: list[str] = []

        # SWF endpoint probes
        if m.swf_endpoints:
            for ep in m.swf_endpoints:
                r = probe_swf_endpoint(ep)
                swf_probes.append(r)
                if r.reachable:
                    notes.append(f"SWF reachable: {ep.label} ({r.http_status}, {r.content_type})")
                else:
                    notes.append(f"SWF unreachable: {ep.label} ({r.error})")

        # Standalone EXE download probe
        if m.kind == ClientKind.STANDALONE_EXE:
            standalone_dl = probe_standalone_download()
            if standalone_dl:
                notes.append("Standalone EXE download URL is alive")
            else:
                notes.append("Standalone EXE download URL is NOT reachable")

        # TCP probe for game server (only once for first method, skip duplicates)
        if m.kind in (ClientKind.FLASH_PROJECTOR, ClientKind.STEAM, ClientKind.STANDALONE_EXE):
            if not any(r.tcp_probes for r in reports):
                targets = _game_server_targets()
                for host, port in targets:
                    r = probe_tcp(host, port)
                    tcp_probes.append(r)
                    if r.reachable:
                        notes.append(f"TCP OK: {host}:{port} ({r.elapsed_ms:.0f}ms)")
                    else:
                        notes.append(f"TCP FAIL: {host}:{port} ({r.error})")

        # Steam-specific notes
        if m.kind == ClientKind.STEAM:
            steam_dir = m.extra.get("steam_dir")
            if steam_dir:
                sd = Path(steam_dir)
                swf_in_steam = list(sd.glob("*.swf"))
                exe_in_steam = list(sd.glob("Transformice*"))
                notes.append(f"Steam dir SWFs: {[s.name for s in swf_in_steam]}")
                notes.append(f"Steam dir EXEs: {[e.name for e in exe_in_steam]}")
            else:
                notes.append("Steam not installed or Transformice not found in library")

        # Ruffle notes
        if m.kind == ClientKind.RUFFLE:
            rbin = m.extra.get("ruffle_binary")
            if rbin:
                notes.append(f"Ruffle binary found: {rbin}")
                notes.append("Ruffle needs a websockify bridge for TCP socket support")
            else:
                notes.append("Ruffle not found on PATH or common locations")

        reports.append(
            ClientProbeReport(
                method=m,
                swf_probes=swf_probes,
                tcp_probes=tcp_probes,
                standalone_download_ok=standalone_dl,
                notes=notes,
            )
        )

    return reports


def log_probe_report(reports: Sequence[ClientProbeReport]) -> None:
    """Pretty-print probe results."""
    logger.info("=" * 60)
    logger.info("TRANSFORMICE CLIENT ACCESS — PROBE RESULTS")
    logger.info("=" * 60)

    for rpt in reports:
        m = rpt.method
        avail = {True: "AVAILABLE", False: "NOT AVAILABLE", None: "UNKNOWN"}[m.available]
        logger.info("")
        logger.info("--- %s [%s] ---", m.label, avail)
        logger.info("    Kind: %s", m.kind.value)

        for sp in rpt.swf_probes:
            status = "OK" if sp.reachable else "FAIL"
            logger.info(
                "    SWF %s: %s (HTTP %s, type=%s, size=%s, swf=%s, %dms)",
                sp.endpoint.label, status, sp.http_status, sp.content_type,
                sp.content_length, sp.is_swf, sp.elapsed_ms,
            )

        for tp in rpt.tcp_probes:
            status = "OK" if tp.reachable else "FAIL"
            logger.info("    TCP %s:%d: %s (%dms)", tp.host, tp.port, status, tp.elapsed_ms)

        if rpt.standalone_download_ok is not None:
            logger.info("    Standalone DL: %s", "OK" if rpt.standalone_download_ok else "FAIL")

        for note in rpt.notes:
            logger.info("    > %s", note)

    logger.info("")
    logger.info("=" * 60)

    # Summary
    reachable_swfs = []
    for rpt in reports:
        for sp in rpt.swf_probes:
            if sp.reachable and sp.is_swf:
                reachable_swfs.append(sp.endpoint)
    if reachable_swfs:
        logger.info("Reachable SWF endpoints (%d):", len(reachable_swfs))
        for ep in reachable_swfs:
            logger.info("  - %s: %s", ep.label, ep.url)
    else:
        logger.warning("No reachable SWF endpoints found — official URLs may be down or moved to Steam-only.")

    any_tcp_ok = any(tp.reachable for rpt in reports for tp in rpt.tcp_probes)
    if any_tcp_ok:
        logger.info("Game server TCP: at least one port is reachable.")
    else:
        logger.warning("Game server TCP: NO ports reachable — check firewall/VPN/ISP.")

    usable = [rpt for rpt in reports if rpt.method.available is True]
    logger.info("Locally usable methods: %d", len(usable))
    for rpt in usable:
        logger.info("  - %s", rpt.method.label)
