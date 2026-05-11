"""
CLI entry point: ``python -m bot.probe_clients_cli``

Probes every known way to access Transformice and prints a structured
report.  Useful as a standalone diagnostic before configuring the bot.

Also importable by ``ban_cli`` for startup diagnostics when
``BOT_PROBE_GAME_CLIENTS_AT_STARTUP=true``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_probe(repo_root: Path | None = None, *, json_output: bool = False) -> int:
    """Run the full probe and print results.  Returns 0 on success."""
    root = repo_root or _repo_root()

    from .env_setup import prepare_runtime_environment
    prepare_runtime_environment()

    from .client_probe import run_full_probe, log_probe_report
    from .game_client_registry import discover_available_methods, log_discovered_methods
    from .steam_client import describe_steam_setup
    from .ruffle_client import describe_ruffle_setup
    from .standalone_client import describe_standalone_setup

    # Phase 1: local discovery (no network)
    logger.info("Phase 1: Discovering locally available client methods...")
    methods = discover_available_methods(root)
    log_discovered_methods(methods)

    # Phase 2: extended diagnostics
    logger.info("")
    logger.info("Phase 2: Extended diagnostics...")
    steam_info = describe_steam_setup()
    ruffle_info = describe_ruffle_setup()
    standalone_info = describe_standalone_setup(root)

    for label, info in [("Steam", steam_info), ("Ruffle", ruffle_info), ("Standalone", standalone_info)]:
        logger.info("  %s: %s", label, json.dumps(info, indent=None, default=str))

    # Phase 3: network probes (SWF URLs, TCP, download checks)
    logger.info("")
    logger.info("Phase 3: Network probes (SWF endpoints, TCP, downloads)...")
    reports = run_full_probe(root)
    log_probe_report(reports)

    if json_output:
        out = {
            "methods": [
                {
                    "kind": m.kind.value,
                    "label": m.label,
                    "available": m.available,
                    "extra": m.extra,
                }
                for m in methods
            ],
            "steam": steam_info,
            "ruffle": ruffle_info,
            "standalone": standalone_info,
            "swf_probes": [
                {
                    "url": sp.endpoint.url,
                    "label": sp.endpoint.label,
                    "reachable": sp.reachable,
                    "http_status": sp.http_status,
                    "content_type": sp.content_type,
                    "is_swf": sp.is_swf,
                    "elapsed_ms": sp.elapsed_ms,
                }
                for rpt in reports
                for sp in rpt.swf_probes
            ],
            "tcp_probes": [
                {
                    "host": tp.host,
                    "port": tp.port,
                    "reachable": tp.reachable,
                    "elapsed_ms": tp.elapsed_ms,
                }
                for rpt in reports
                for tp in rpt.tcp_probes
            ],
        }
        print(json.dumps(out, indent=2, default=str))

    # Summary recommendation
    logger.info("")
    logger.info("=== RECOMMENDATION ===")

    usable = [m for m in methods if m.available is True]
    if not usable:
        logger.warning(
            "No locally available client methods detected.  Options:\n"
            "  1. Place flashplayer_32_sa_debug.exe + TFMProxyLoader.swf in the repo root\n"
            "  2. Install Transformice from Steam (free, app 659960)\n"
            "  3. Download Transformice.exe from %s\n"
            "  4. Install Ruffle desktop from https://ruffle.rs/downloads",
            "http://www.transformice.com/Transformice.exe",
        )
    else:
        logger.info("Available methods:")
        for m in usable:
            logger.info("  - %s (%s)", m.label, m.kind.value)
        logger.info("")
        logger.info(
            "Set BOT_GAME_CLIENT_MODE in .env to one of: "
            "flash_projector, standalone_exe, steam, ruffle"
        )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe all known Transformice game-client access methods."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    sys.exit(run_probe(json_output=args.json))


if __name__ == "__main__":
    main()
