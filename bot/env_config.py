"""
Load ban-bot settings from ``.env`` in the repository root (or next to ``ban_bot.exe`` when frozen).

``BOT_ACCOUNTS_JSON`` may span multiple lines. Non-breaking spaces (e.g. from some editors) are
normalized so ``json`` can parse the array.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ENV_BOOL_TRUE = {"1", "true", "yes", "on"}
_ENV_BOOL_FALSE = {"0", "false", "no", "off"}


def _bool_from_env(val: str | None) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in _ENV_BOOL_TRUE:
        return True
    if s in _ENV_BOOL_FALSE:
        return False
    return None


def _float_from_env(val: str | None) -> float | None:
    if val is None or not str(val).strip():
        return None
    try:
        return float(str(val).strip())
    except ValueError:
        return None


def _nb_space_normalize(s: str) -> str:
    return s.replace("\u00a0", " ").replace("\ufeff", "")


def _json_loose(s: str) -> str:
    """Allow trailing commas (invalid in strict JSON) often found in hand-edited .env arrays."""
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r",\s*]", "]", s)
    return s


def _array_text_only_after_equals(rest: str) -> str:
    """
    The tail after ``BOT_ACCOUNTS_JSON =`` often includes ``# ---`` and more ``KEY=`` lines. Keep only
    the leading ``[...]`` (stop before the first section comment or bare ``KEY=`` after the closing ``]``).
    """
    t = rest.lstrip()
    if not t.startswith("["):
        return rest
    m2 = re.search(
        r"\](?:\r?\n[ \t]*){1,3}#\s*---",
        t,
    )
    if m2:
        return t[: m2.start() + 1]
    m3 = re.search(r"\](?:\r?\n[ \t]*){1,3}[A-Z][A-Z0-9_]*\s*=", t)
    if m3:
        return t[: m3.start() + 1]
    return t


def _split_env_and_accounts(
    raw: str,
) -> tuple[str, list[dict[str, object]]]:
    """
    ``python-dotenv`` cannot parse a multiline ``BOT_ACCOUNTS_JSON = [ ... ]`` block.
    We extract the array with ``json.JSONDecoder`` and return the rest of the file for ``load_dotenv``.
    """
    nraw = _nb_space_normalize(raw)
    m = re.search(r"(?ms)^\s*BOT_ACCOUNTS_JSON\s*=\s*", nraw)
    if not m:
        return nraw, []
    tail = nraw[m.end() :]
    lstrip_n = len(tail) - len(tail.lstrip())
    rest = tail.lstrip()
    if not rest.startswith("["):
        return nraw, []
    arr = _array_text_only_after_equals(rest)
    dec = json.JSONDecoder()
    try:
        data, j = dec.raw_decode(_json_loose(arr), 0)
    except json.JSONDecodeError as e:
        logger.error("BOT_ACCOUNTS_JSON is not valid JSON: %s", e)
        raise SystemExit(1) from e
    if not isinstance(data, list):
        logger.error("BOT_ACCOUNTS_JSON must be a JSON array.")
        raise SystemExit(1)
    loose = _json_loose(arr)
    if j != len(loose):
        logger.error(
            "BOT_ACCOUNTS_JSON: trailing garbage after the JSON array (char %s); check brackets.",
            j,
        )
        raise SystemExit(1)
    if rest[: len(arr)] != arr:
        logger.error("Internal error: could not remove BOT_ACCOUNTS_JSON block (prefix mismatch).")
        raise SystemExit(1)
    end = m.end() + lstrip_n + len(arr)
    cleaned = nraw[: m.start()] + nraw[end:].lstrip()
    return cleaned, [x for x in data if isinstance(x, dict)]


def load_config_from_env(repo_root: Path) -> object:
    """
    Return a namespace with ``ACCOUNTS`` and the same attribute names the old ``config.py`` used
    (``BAN_DELAY_MIN_SEC``, ``SHARED_FLASH_SOCKET_POLICY_PORT``, etc.).
    """
    path = (repo_root / ".env").resolve()
    if not path.is_file():
        logger.error(
            "Missing %s. Add a .env in the project root (see comments for BOT_ACCOUNTS_JSON and BOT_*).",
            path,
        )
        raise SystemExit(1)

    raw = path.read_text(encoding="utf-8", errors="replace")
    cleaned, accounts = _split_env_and_accounts(raw)
    load_dotenv(stream=io.StringIO(cleaned), override=False)
    if not accounts:
        s = (os.environ.get("BOT_ACCOUNTS_JSON") or "").strip()
        if s:
            dec = json.JSONDecoder()
            try:
                d, _ = dec.raw_decode(_json_loose(_nb_space_normalize(s)), 0)
            except json.JSONDecodeError as e:
                logger.error("BOT_ACCOUNTS_JSON is not valid JSON: %s", e)
                raise SystemExit(1) from e
            if isinstance(d, list):
                accounts = [x for x in d if isinstance(x, dict)]
    if not accounts:
        logger.error("No accounts: set BOT_ACCOUNTS_JSON in .env to a non-empty JSON array.")
        raise SystemExit(1)

    ns: SimpleNamespace = SimpleNamespace(ACCOUNTS=accounts)

    def f(attr: str, key: str, default: float) -> None:
        v = _float_from_env(os.environ.get(key))
        setattr(ns, attr, v if v is not None else default)

    def b(attr: str, key: str, default: bool) -> None:
        raw = os.environ.get(key)
        parsed = _bool_from_env(raw) if raw is not None and str(raw).strip() != "" else None
        setattr(ns, attr, parsed if parsed is not None else default)

    F = f
    B = b

    F("BAN_DELAY_MIN_SEC", "BOT_BAN_DELAY_MIN_SEC", 1.0)
    F("BAN_DELAY_MAX_SEC", "BOT_BAN_DELAY_MAX_SEC", 2.0)
    F("ROOM_STAGGER_SEC", "BOT_ROOM_STAGGER_SEC", 0.15)
    F("FLASH_SLOT_LOGIN_TIMEOUT_SEC", "BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC", 900.0)
    B("PROXY_VERBOSE_LOGIN_FLOW", "BOT_PROXY_VERBOSE_LOGIN_FLOW", True)
    B("PROXY_LOG_ALL_MAIN_PACKETS", "BOT_PROXY_LOG_ALL_MAIN_PACKETS", False)
    B("PACKET_AUTO_LOGIN", "BOT_PACKET_AUTO_LOGIN", False)
    F("PACKET_LOGIN_DELAY_SEC", "BOT_PACKET_LOGIN_DELAY_SEC", 0.35)
    # Proxy-driven KeepAlivePacket so idle main connections don't get dropped by TFM
    # while their Flash client is minimized (Flash throttles background timers, so its
    # own keepalives stop, and the server eventually kills the connection → PARTL status).
    # 0 or negative disables the feature.
    F("MAIN_KEEPALIVE_INTERVAL_SEC", "BOT_MAIN_KEEPALIVE_INTERVAL_SEC", 15.0)
    F("ROOM_LIST_TIMEOUT_SEC", "BOT_ROOM_LIST_TIMEOUT_SEC", 10.0)
    _rmax = (os.environ.get("BOT_ROOM_LIST_MAX_SLOT_ATTEMPTS") or "").strip()
    try:
        ns.ROOM_LIST_MAX_SLOT_ATTEMPTS = int(_rmax) if _rmax else 3
    except ValueError:
        ns.ROOM_LIST_MAX_SLOT_ATTEMPTS = 3
    ns.ROOM_LIST_MAX_SLOT_ATTEMPTS = max(1, min(32, int(ns.ROOM_LIST_MAX_SLOT_ATTEMPTS)))
    F("PLAYER_LIST_COLLECT_TIMEOUT_SEC", "BOT_PLAYER_LIST_COLLECT_TIMEOUT_SEC", 25.0)
    B("BAN_PRE_ROUND_DISMISS_FLASH", "BOT_BAN_PRE_ROUND_DISMISS_FLASH", True)
    room = (os.environ.get("BOT_PACKET_LOGIN_START_ROOM") or "").strip()
    ns.PACKET_LOGIN_START_ROOM = room

    bind = (os.environ.get("BOT_PROXY_BIND_HOST") or "").strip()
    ns.PROXY_BIND_HOST = bind if bind else None
    B("PROXY_LISTEN_USE_ACCOUNT_BIND_IP", "BOT_PROXY_LISTEN_USE_ACCOUNT_BIND_IP", False)
    pbind = (os.environ.get("BOT_FLASH_SOCKET_POLICY_BIND_HOST") or "").strip()
    if pbind:
        ns.FLASH_SOCKET_POLICY_BIND_HOST = pbind

    # If the key is absent, ban_cli uses default 10801; if present (e.g. none/empty), per-slot policy.
    if "BOT_SHARED_FLASH_SOCKET_POLICY_PORT" in os.environ:
        v = (os.environ.get("BOT_SHARED_FLASH_SOCKET_POLICY_PORT") or "").strip()
        if not v or v.lower() in ("none", "null", "false", "0"):
            ns.SHARED_FLASH_SOCKET_POLICY_PORT = None
        else:
            try:
                ns.SHARED_FLASH_SOCKET_POLICY_PORT = int(v, 0)
            except ValueError:
                logger.error("BOT_SHARED_FLASH_SOCKET_POLICY_PORT must be an integer, or none/empty for per-slot policy.")
                raise SystemExit(1)

    B("FLASH_AUTO_LOGIN_UI", "BOT_FLASH_AUTO_LOGIN_UI", False)

    # --- UI / Flash launch ---
    # BOT_UI_AUTO_LAUNCH_FLASH: whether the bot should auto-open flashplayer per slot.
    # Defaults to True (historical behavior) but .env sets it False to require manual launch.
    B("UI_AUTO_LAUNCH_FLASH", "BOT_UI_AUTO_LAUNCH_FLASH", True)

    # FLASH_MINIMIZE_AFTER_OPEN: minimize each Flash window after login succeeds.
    # Reads directly from FLASH_MINIMIZE_AFTER_OPEN in .env (not BOT_* prefixed).
    _min_raw = (os.environ.get("FLASH_MINIMIZE_AFTER_OPEN") or "").strip().lower()
    ns.FLASH_MINIMIZE_AFTER_OPEN = _min_raw in ("1", "true", "yes", "on")

    # BOT_UI_FLASH_PLAYER_PATH: optional path to flashplayer exe; fed into FLASH_PLAYER_EXE
    # which resolve_flash_paths() reads.  Must be applied before flash_launch_files_present().
    fp = (os.environ.get("BOT_UI_FLASH_PLAYER_PATH") or "").strip()
    if fp:
        os.environ.setdefault("FLASH_PLAYER_EXE", fp)
        ns.FLASH_PLAYER_EXE = fp

    # BOT_UI_FLASH_LAUNCH_STAGGER_SEC: delay between opening successive Flash windows.
    F("FLASH_STAGGER_AFTER_LOGIN_SEC", "BOT_UI_FLASH_LAUNCH_STAGGER_SEC", 0.5)

    # BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC: per-slot login wait when doing sequential launch.
    # Overrides BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC when explicitly set.
    seq_to = _float_from_env(os.environ.get("BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC"))
    if seq_to is not None:
        ns.FLASH_SLOT_LOGIN_TIMEOUT_SEC = seq_to

    # Early Transformice-button re-click loop after Flash launch. The initial click happens
    # FLASH_LOADER_POST_OPEN_DELAY_SEC after Flash starts; later slots often render the loader
    # more slowly (CPU saturated by earlier Flash instances) and miss that single click, so we
    # re-click every EARLY_RETRY_INTERVAL_SEC for EARLY_RETRY_COUNT attempts until MAIN TCP
    # accept is observed. This is what actually makes slots without bind_ip connect reliably.
    F("FLASH_LOADER_EARLY_RETRY_INTERVAL_SEC", "BOT_FLASH_LOADER_EARLY_RETRY_INTERVAL_SEC", 3.0)
    early_cnt = _float_from_env(os.environ.get("BOT_FLASH_LOADER_EARLY_RETRY_COUNT"))
    ns.FLASH_LOADER_EARLY_RETRY_COUNT = int(early_cnt) if early_cnt is not None else 5

    # Auto-close failed Flash windows: once a slot's login timeout expires without a
    # LoginSuccessPacket, post WM_CLOSE to its Flash window(s) (and TerminateProcess if
    # the grace period elapses). Keeps the desktop clean when many slots fail to reach
    # the login screen so the user doesn't have to hunt and close stale tabs manually.
    close_fail = _bool_from_env(os.environ.get("BOT_FLASH_CLOSE_ON_LOGIN_FAIL"))
    ns.FLASH_CLOSE_ON_LOGIN_FAIL = True if close_fail is None else close_fail
    F("FLASH_CLOSE_GRACE_SEC", "BOT_FLASH_CLOSE_GRACE_SEC", 2.0)

    return ns
