"""
Validate each row in ``BOT_ACCOUNTS_JSON`` for required fields and correct format.

This performs a **static** check only — it does not connect to the game server.
Use it to catch missing usernames, passwords, or duplicate proxy ports before
running the main bot.

Examples::

    python -m bot.validate_accounts
    python -m bot.validate_accounts --index 0
"""

from __future__ import annotations

import argparse
import logging
import sys

from colorama import init as colorama_init

from .env_setup import load_bot_config, repo_root

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


def validate_accounts(cfg: object, *, index: int | None = None) -> bool:
    """
    Check BOT_ACCOUNTS_JSON for missing/invalid fields.

    Returns ``True`` if all checked accounts are valid.
    """
    raw = cfg.ACCOUNTS
    if not raw:
        logger.error("BOT_ACCOUNTS_JSON is empty — no accounts configured.")
        return False

    if index is not None:
        if index < 0 or index >= len(raw):
            logger.error("Index %s out of range (0..%s).", index, len(raw) - 1)
            return False
        rows = [(index, raw[index])]
    else:
        rows = list(enumerate(raw))

    seen_ports: set[int] = set()
    seen_ips: set[str] = set()
    all_ok = True

    for i, row in rows:
        label = str(row.get("label", i + 1))
        username = str(row.get("username", "") or "").strip()
        password = str(row.get("password", "") or "")
        proxy_port_raw = row.get("proxy_port")
        bind_ip = str(row.get("bind_ip", "") or "").strip()

        ok = True
        if not username:
            logger.error("Slot %s (index %s): 'username' is empty.", label, i)
            ok = False
        if not password.strip():
            logger.error("Slot %s (index %s): 'password' is empty.", label, i)
            ok = False
        if proxy_port_raw is None:
            logger.error("Slot %s (index %s): 'proxy_port' is missing.", label, i)
            ok = False
        else:
            try:
                port = int(proxy_port_raw)
            except (TypeError, ValueError):
                logger.error("Slot %s (index %s): 'proxy_port' is not an integer: %r.", label, i, proxy_port_raw)
                ok = False
                port = None
            if port is not None:
                if port < 1 or port > 65535:
                    logger.error("Slot %s (index %s): proxy_port %s out of valid range 1-65535.", label, i, port)
                    ok = False
                elif port in seen_ports:
                    logger.error("Slot %s (index %s): duplicate proxy_port %s.", label, i, port)
                    ok = False
                else:
                    seen_ports.add(port)

        if bind_ip:
            if bind_ip in seen_ips:
                logger.error("Slot %s (index %s): duplicate bind_ip %r.", label, i, bind_ip)
                ok = False
            else:
                seen_ips.add(bind_ip)

        if ok:
            logger.info("OK  index=%s label=%s username=%s proxy_port=%s", i, label, username, proxy_port_raw)
        else:
            all_ok = False

    if all_ok:
        logger.info("All %s account(s) passed validation.", len(rows))
    else:
        logger.error("One or more accounts failed validation (see errors above).")
    return all_ok


def main(argv: list[str] | None = None) -> None:
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
        description="Validate BOT_ACCOUNTS_JSON entries for required fields and format.",
    )
    p.add_argument("--index", type=int, default=None, help="Check only this 0-based account index.")
    args = p.parse_args(argv)

    cfg = load_bot_config()
    ok = validate_accounts(cfg, index=args.index)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
