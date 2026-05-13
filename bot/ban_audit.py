"""Structured ban audit trail: append-only JSONL log for every /ban action.

Each line is a self-contained JSON object with wall-clock timestamp, target,
slot, account nickname, success/failure, latency, and round context.  The file
lives at ``logs/ban_audit.jsonl`` and is designed to be machine-parseable
(``jq``, ``pandas.read_json(lines=True)``, etc.) and human-grepable.

Usage from ``ban_proxy.py`` / ``ban_cli.py``::

    from .ban_audit import audit_ban_send, audit_ban_round

    audit_ban_send(...)   # one call per /ban packet
    audit_ban_round(...)  # one call per completed round
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_audit_logger: logging.Logger | None = None
_log_dir: Path | None = None


def _repo_root() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _ensure_audit_logger() -> logging.Logger:
    """Lazy-init a dedicated logger that writes JSONL to ``logs/ban_audit.jsonl``."""
    global _audit_logger, _log_dir
    if _audit_logger is not None:
        return _audit_logger
    with _lock:
        if _audit_logger is not None:
            return _audit_logger
        root = _repo_root()
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _log_dir = log_dir

        al = logging.getLogger("bot.ban_audit")
        al.setLevel(logging.INFO)
        al.propagate = False

        path = log_dir / "ban_audit.jsonl"
        fh = logging.FileHandler(path, encoding="utf-8", mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(message)s"))
        al.addHandler(fh)
        _audit_logger = al
    return _audit_logger


def _wall_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _wall_local_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def audit_ban_send(
    *,
    target_user: str,
    slot_label: str,
    account_nick: str,
    success: bool,
    conn_type: str,
    latency_ms: float,
    error: str = "",
    round_id: str = "",
    room: str = "",
) -> None:
    """Record one /ban packet send (or failure) as a JSONL line."""
    entry = {
        "event": "ban_send",
        "ts_utc": _wall_iso(),
        "ts_local": _wall_local_iso(),
        "target": target_user,
        "slot": slot_label,
        "account": account_nick,
        "success": success,
        "conn": conn_type,
        "latency_ms": round(latency_ms, 1),
        "round_id": round_id,
    }
    if room:
        entry["room"] = room
    if error:
        entry["error"] = error
    _emit(entry)


def audit_ban_round(
    *,
    round_id: str,
    target_user: str,
    ok_count: int,
    fail_count: int,
    skip_count: int,
    total_slots: int,
    live_at_send: int,
    elapsed_sec: float,
    burst_mode: bool,
    quorum: int,
    quorum_met: bool,
    room: str = "",
    slot_results: list[dict] | None = None,
) -> None:
    """Record the summary of a completed ban round."""
    entry = {
        "event": "ban_round",
        "ts_utc": _wall_iso(),
        "ts_local": _wall_local_iso(),
        "round_id": round_id,
        "target": target_user,
        "ok": ok_count,
        "fail": fail_count,
        "skip": skip_count,
        "total_slots": total_slots,
        "live_at_send": live_at_send,
        "elapsed_sec": round(elapsed_sec, 2),
        "burst": burst_mode,
        "quorum": quorum,
        "quorum_met": quorum_met,
    }
    if room:
        entry["room"] = room
    if slot_results:
        entry["slots"] = slot_results
    _emit(entry)


def audit_ban_skip(
    *,
    target_user: str,
    slot_label: str,
    reason: str,
    round_id: str = "",
) -> None:
    """Record a slot that was skipped (no upstream connection)."""
    entry = {
        "event": "ban_skip",
        "ts_utc": _wall_iso(),
        "ts_local": _wall_local_iso(),
        "target": target_user,
        "slot": slot_label,
        "reason": reason,
        "round_id": round_id,
    }
    _emit(entry)


def _emit(entry: dict) -> None:
    try:
        lg = _ensure_audit_logger()
        lg.info(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass


def audit_log_path() -> Path | None:
    """Return the path to the current audit log (``None`` if not yet initialised)."""
    if _log_dir is not None:
        return _log_dir / "ban_audit.jsonl"
    return _repo_root() / "logs" / "ban_audit.jsonl"
