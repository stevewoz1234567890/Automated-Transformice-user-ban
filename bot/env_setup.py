"""Repo-root ``.env`` bootstrap and bot settings loaded into a ``SimpleNamespace`` (``cfg``)."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

_NEW_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _normalize_accounts_json_text(s: str) -> str:
    s = s.strip().strip("\ufeff")
    return (
        s.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _strip_js_trailing_commas(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",\s*]", "]", s)
        s = re.sub(r",\s*}", "}", s)
    return s


def _parse_accounts_list(raw: str) -> list:
    """Parse ``BOT_ACCOUNTS_JSON`` with JSON, trailing-comma cleanup, or Python ``literal_eval``."""
    s = _normalize_accounts_json_text(raw)
    if not s:
        raise ValueError("empty string")
    loose = _strip_js_trailing_commas(s)
    errors: list[str] = []
    for label, candidate in (
        ("json", s),
        ("json+trailing_commas", loose),
    ):
        try:
            data = json.loads(candidate)
            if isinstance(data, list) and data:
                if candidate != s:
                    logger.info("BOT_ACCOUNTS_JSON: accepted as %s (normalized).", label)
                return data
        except json.JSONDecodeError as e:
            errors.append(f"{label}: {e}")
    for label, candidate in (("literal_eval", s), ("literal_eval+trailing_commas", loose)):
        try:
            data = ast.literal_eval(candidate)
            if isinstance(data, list) and data:
                logger.info("BOT_ACCOUNTS_JSON: parsed via %s (Python-style list).", label)
                return data
        except (ValueError, SyntaxError) as e:
            errors.append(f"{label}: {e}")
    bracket = s.find("[")
    if bracket > 0:
        try:
            return _parse_accounts_list(s[bracket:])
        except ValueError:
            pass
    raise ValueError("; ".join(errors) if errors else "no parse strategy matched")


def _try_parse_accounts_list(raw: str) -> bool:
    try:
        _parse_accounts_list(raw)
        return True
    except ValueError:
        return False


def _raw_bot_accounts_json_from_dotenv(dot: Path) -> str | None:
    """
    Read ``BOT_ACCOUNTS_JSON`` from ``.env`` with multiline / bracket continuation.
    The line-based ``load_dotenv_file`` only keeps the first line of an unquoted value.
    """
    if not dot.is_file():
        return None
    try:
        text = dot.read_text(encoding="utf-8")
    except OSError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val0 = line.partition("=")
        if key.strip().lower() != "bot_accounts_json":
            continue
        val0 = val0.strip()
        if not val0:
            return None
        if val0.startswith('"'):
            try:
                decoded = json.loads(val0)
                if isinstance(decoded, str):
                    val0 = decoded
                elif isinstance(decoded, list):
                    return json.dumps(decoded, separators=(",", ":"))
            except json.JSONDecodeError:
                pass
        elif len(val0) >= 2 and val0[0] == val0[-1] == "'":
            val0 = val0[1:-1].replace("\\'", "'")
        if _try_parse_accounts_list(val0):
            return val0
        parts: list[str] = [val0]
        for j in range(i + 1, len(lines)):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith("#"):
                continue
            if _NEW_ENV_ASSIGN.match(nxt):
                break
            parts.append(nxt)
            merged = "".join(parts)
            if _try_parse_accounts_list(merged):
                logger.info(
                    "BOT_ACCOUNTS_JSON: merged %s lines from .env (multiline value).",
                    len(parts),
                )
                return merged
        merged = "".join(parts)
        return merged
    return None


# Non-secret bot knobs: merged into ``.env`` when the key is missing or has an empty value.
# Game secrets use ``TFM_SECRETS_*`` from ``.env.example`` (filled by the user).
_ENV_DEFAULTS: dict[str, str] = {
    "BOT_ACCOUNTS_JSON": (
        '[{"label":"1","proxy_port":38291,"bind_ip":"127.0.0.1","username":"","password":""}]'
    ),
    "BOT_BAN_DELAY_MIN_SEC": "1.0",
    "BOT_BAN_DELAY_MAX_SEC": "2.0",
    "BOT_ROOM_STAGGER_SEC": "0.15",
    "BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC": "7200",
    "BOT_PROXY_VERBOSE_LOGIN_FLOW": "false",
    "BOT_PROXY_LOG_ALL_MAIN_PACKETS": "false",
    "BOT_PROXY_LOGIN_DIAGNOSTICS": "true",
    "BOT_PACKET_LOGIN_DELAY_SEC": "0.35",
    "BOT_PACKET_LOGIN_START_ROOM": "",
    "BOT_PROXY_UPSTREAM_CONNECT_DIAG": "true",
    "BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS": "false",
    "BOT_PROXY_BIND_HOST": "",
    "BOT_PROXY_LISTEN_USE_ACCOUNT_BIND_IP": "false",
    "BOT_SHARED_FLASH_SOCKET_POLICY_PORT": "10801",
    "BOT_HEADLESS_AUTO_LOGIN": "false",
    "BOT_HEADLESS_SECRETS_DOTENV_PATH": ".env",
    "BOT_HEADLESS_SECRETS_SEED_DOTENV_FROM_EXAMPLE": "true",
    "BOT_HEADLESS_SECRETS_ENV_PREFIX": "TFM_SECRETS_",
    "BOT_HEADLESS_SECRETS_INLINE_JSON": "",
    "BOT_HEADLESS_SECRETS_DUMPER": "",
    "BOT_HEADLESS_SECRETS_AUTO_DUMPER": "true",
    "BOT_HEADLESS_SECRETS_AUTO_LEAKER_SWF": "true",
    "BOT_HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER": "true",
    "BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV": "true",
    "BOT_HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC": "120",
    "BOT_HEADLESS_SECRETS_ALWAYS_REFRESH": "true",
    "BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS": "true",
    "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY": "false",
    "BOT_UPSTREAM_PORTS_MATCH_DUMP_ORDER": "true",
    "BOT_UPSTREAM_STRICT_MATCH_SECRETS_DUMP": "false",
    "BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH": "false",
    "BOT_UPSTREAM_SERVER_ADDRESS": "",
    "BOT_UPSTREAM_SERVER_PORTS": "",
    "BOT_UPSTREAM_MAIN_GAME_PORT_TRY_FIRST": "11801",
    "BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS": "true",
    "BOT_UPSTREAM_ABORT_ON_PROBE_ALL_FAILED": "true",
    "BOT_UPSTREAM_PROBE_TIMEOUT_SEC": "6",
    "BOT_HEADLESS_CONNECT_TO_SATELLITE": "true",
    "BOT_HEADLESS_PARALLEL_LOGIN": "true",
    "BOT_HEADLESS_PARALLEL_START_STAGGER_SEC": "0.4",
    "BOT_HEADLESS_EXIT_AFTER_LOGIN_SUCCESS": "true",
    "BOT_HEADLESS_LOGIN_STAGGER_SEC": "6",
    "BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES": "3",
    "BOT_HEADLESS_STAGGER_WIN121_EXTRA_SEC": "4",
    "BOT_HEADLESS_STAGGER_MAX_SEC": "15",
    "BOT_PIP_INSTALL_CASEUS_GIT_UPGRADE": "false",
    "BOT_CASEUS_GIT_PIP_SPEC": "caseus @ git+https://github.com/friedkeenan/caseus.git",
    "BOT_PIP_INSTALL_TFM_SECRETS_CLI": "false",
    "BOT_TFM_SECRETS_PIP_INSTALL_SPEC": "",
}


def repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_dotenv_file(path: Path) -> None:
    """Minimal KEY=VAL loader (no python-dotenv). Does not override keys already in ``os.environ``."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read .env file %s", path)
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = rest.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


def _defined_env_keys(text: str) -> set[str]:
    """Keys that already appear in an assignment (even ``KEY=``), so we should not append a duplicate."""
    keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key:
            keys.add(key)
    return keys


def merge_defaults_into_dotenv(path: Path, defaults: dict[str, str]) -> None:
    """Append ``KEY=value`` for defaults whose key is not present in the file at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = _defined_env_keys(text)
    extra: list[str] = []
    for k, v in defaults.items():
        if k not in have:
            extra.append(f"{k}={v}")
    if not extra:
        return
    sep = "" if not text or text.endswith("\n") else "\n"
    path.write_text(text + sep + "\n".join(extra) + "\n", encoding="utf-8")


def format_dotenv_value(val: str) -> str:
    """Serialize a value for a ``KEY=value`` line (quote if needed)."""
    if not val:
        return ""
    if any(c in val for c in "\n\r\"") or (val.startswith(" ") or val.endswith(" ")):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if "#" in val:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


def update_or_append_dotenv(path: Path, updates: dict[str, str]) -> None:
    """Replace existing assignments or append keys at the end of a ``.env`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keys_done: set[str] = set()
    out_lines: list[str] = []
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in updates:
                    out_lines.append(f"{k}={format_dotenv_value(updates[k])}")
                    keys_done.add(k)
                    continue
            out_lines.append(raw)
    for k, v in updates.items():
        if k not in keys_done:
            out_lines.append(f"{k}={format_dotenv_value(v)}")
    path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")


def _bootstrap_dotenv_path() -> Path:
    root = repo_root()
    rel = os.environ.get("BOT_HEADLESS_SECRETS_DOTENV_PATH", "").strip()
    if not rel:
        rel = ".env"
    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    return p


def prepare_runtime_environment() -> None:
    """Copy ``.env.example`` → ``.env`` when missing, merge ``BOT_*`` defaults, load into the process env."""
    root = repo_root()
    seed = os.environ.get("BOT_HEADLESS_SECRETS_SEED_DOTENV_FROM_EXAMPLE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    dot = _bootstrap_dotenv_path()
    ex = root / ".env.example"
    if seed and not dot.is_file() and ex.is_file():
        try:
            shutil.copy(ex, dot)
            logger.info("Created %s from .env.example", dot)
        except OSError as e:
            logger.warning("Could not copy .env.example to %s: %s", dot, e)
    merge_defaults_into_dotenv(dot, _ENV_DEFAULTS)
    load_dotenv_file(dot)


def _truthy(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int_ge_zero(key: str, default: int) -> int:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    return max(0, int(v, 0))


def _float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is None or not str(v).strip():
        return default
    return float(v)


def _opt_str(key: str) -> str | None:
    s = str(os.environ.get(key, "") or "").strip()
    return s if s else None


def _shared_flash_policy_port() -> int | None:
    raw = os.environ.get("BOT_SHARED_FLASH_SOCKET_POLICY_PORT", "10801")
    if raw is None:
        return 10801
    s = str(raw).strip()
    if not s or s.lower() in ("none", "false", "0"):
        return None
    p = int(s)
    return p if p > 0 else None


def _upstream_ports() -> tuple[int, ...] | None:
    v = os.environ.get("BOT_UPSTREAM_SERVER_PORTS", "").strip()
    if not v:
        return None
    return tuple(int(x.strip()) for x in v.split(",") if x.strip())


def _upstream_main_port_try_first() -> int | bool:
    v = os.environ.get("BOT_UPSTREAM_MAIN_GAME_PORT_TRY_FIRST", "11801").strip()
    if not v or v.lower() in ("none", "false", "0"):
        return False
    return int(v)


def _headless_secrets_inline() -> dict | None:
    raw = os.environ.get("BOT_HEADLESS_SECRETS_INLINE_JSON", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("BOT_HEADLESS_SECRETS_INLINE_JSON is not valid JSON: %s", e)
        raise SystemExit(1) from e
    if not isinstance(data, dict) or not data:
        return None
    return data


def _headless_secrets_dumper() -> str | list | tuple | None:
    raw = os.environ.get("BOT_HEADLESS_SECRETS_DUMPER", "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("BOT_HEADLESS_SECRETS_DUMPER JSON list is invalid: %s", e)
            raise SystemExit(1) from e
        if isinstance(data, list):
            return data
        return None
    return raw


def load_bot_config() -> SimpleNamespace:
    """Load ``cfg`` with the same attribute names as the former ``bot/config.py`` module."""
    prepare_runtime_environment()

    dot = _bootstrap_dotenv_path()
    accounts_raw = _raw_bot_accounts_json_from_dotenv(dot)
    if accounts_raw is None or not str(accounts_raw).strip():
        accounts_raw = os.environ.get("BOT_ACCOUNTS_JSON", "").strip()
    if not accounts_raw:
        logger.error("BOT_ACCOUNTS_JSON is empty in .env (see .env.example).")
        raise SystemExit(1)
    try:
        accounts = _parse_accounts_list(accounts_raw)
    except ValueError as e:
        logger.error(
            "BOT_ACCOUNTS_JSON could not be parsed (use strict JSON array or Python list syntax): %s",
            e,
        )
        raise SystemExit(1) from e
    if not isinstance(accounts, list) or not accounts:
        logger.error("BOT_ACCOUNTS_JSON must be a non-empty JSON array of account objects.")
        raise SystemExit(1)

    proxy_bind = _opt_str("BOT_PROXY_BIND_HOST")

    return SimpleNamespace(
        ACCOUNTS=accounts,
        BAN_DELAY_MIN_SEC=_float("BOT_BAN_DELAY_MIN_SEC", 1.0),
        BAN_DELAY_MAX_SEC=_float("BOT_BAN_DELAY_MAX_SEC", 2.0),
        ROOM_STAGGER_SEC=_float("BOT_ROOM_STAGGER_SEC", 0.15),
        ALL_SLOTS_LOGIN_TIMEOUT_SEC=_float("BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC", 7200.0),
        PROXY_VERBOSE_LOGIN_FLOW=_truthy("BOT_PROXY_VERBOSE_LOGIN_FLOW", False),
        PROXY_LOG_ALL_MAIN_PACKETS=_truthy("BOT_PROXY_LOG_ALL_MAIN_PACKETS", False),
        PROXY_LOGIN_DIAGNOSTICS=_truthy("BOT_PROXY_LOGIN_DIAGNOSTICS", True),
        PACKET_LOGIN_DELAY_SEC=_float("BOT_PACKET_LOGIN_DELAY_SEC", 0.35),
        PACKET_LOGIN_START_ROOM=str(os.environ.get("BOT_PACKET_LOGIN_START_ROOM", "") or ""),
        PROXY_UPSTREAM_CONNECT_DIAG=_truthy("BOT_PROXY_UPSTREAM_CONNECT_DIAG", True),
        UPSTREAM_CONNECT_SHUFFLE_PORTS=_truthy("BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS", False),
        PROXY_BIND_HOST=proxy_bind,
        PROXY_LISTEN_USE_ACCOUNT_BIND_IP=_truthy("BOT_PROXY_LISTEN_USE_ACCOUNT_BIND_IP", False),
        SHARED_FLASH_SOCKET_POLICY_PORT=_shared_flash_policy_port(),
        HEADLESS_AUTO_LOGIN=_truthy("BOT_HEADLESS_AUTO_LOGIN", False),
        HEADLESS_SECRETS_DOTENV_PATH=str(os.environ.get("BOT_HEADLESS_SECRETS_DOTENV_PATH", ".env") or ".env"),
        HEADLESS_SECRETS_SEED_DOTENV_FROM_EXAMPLE=_truthy(
            "BOT_HEADLESS_SECRETS_SEED_DOTENV_FROM_EXAMPLE", True
        ),
        HEADLESS_SECRETS_ENV_PREFIX=str(
            os.environ.get("BOT_HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_"
        ),
        HEADLESS_SECRETS_INLINE=_headless_secrets_inline(),
        HEADLESS_SECRETS_DUMPER=_headless_secrets_dumper(),
        HEADLESS_SECRETS_AUTO_DUMPER=_truthy("BOT_HEADLESS_SECRETS_AUTO_DUMPER", True),
        HEADLESS_SECRETS_AUTO_LEAKER_SWF=_truthy("BOT_HEADLESS_SECRETS_AUTO_LEAKER_SWF", True),
        HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER=_truthy(
            "BOT_HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER", True
        ),
        HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV=_truthy(
            "BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV", True
        ),
        HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC=_float("BOT_HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC", 120.0),
        HEADLESS_SECRETS_ALWAYS_REFRESH=_truthy("BOT_HEADLESS_SECRETS_ALWAYS_REFRESH", True),
        UPSTREAM_AUTO_SYNC_FROM_SECRETS=_truthy("BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS", True),
        UPSTREAM_FROM_SECRETS_DUMP_ONLY=_truthy("BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY", False),
        UPSTREAM_PORTS_MATCH_DUMP_ORDER=_truthy("BOT_UPSTREAM_PORTS_MATCH_DUMP_ORDER", True),
        UPSTREAM_STRICT_MATCH_SECRETS_DUMP=_truthy("BOT_UPSTREAM_STRICT_MATCH_SECRETS_DUMP", False),
        UPSTREAM_ALLOW_ADDRESS_MISMATCH=_truthy("BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH", False),
        UPSTREAM_SERVER_ADDRESS=_opt_str("BOT_UPSTREAM_SERVER_ADDRESS"),
        UPSTREAM_SERVER_PORTS=_upstream_ports(),
        UPSTREAM_MAIN_GAME_PORT_TRY_FIRST=_upstream_main_port_try_first(),
        UPSTREAM_TCP_PROBE_BEFORE_HEADLESS=_truthy("BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS", True),
        UPSTREAM_ABORT_ON_PROBE_ALL_FAILED=_truthy("BOT_UPSTREAM_ABORT_ON_PROBE_ALL_FAILED", True),
        UPSTREAM_PROBE_TIMEOUT_SEC=_float("BOT_UPSTREAM_PROBE_TIMEOUT_SEC", 6.0),
        HEADLESS_CONNECT_TO_SATELLITE=_truthy("BOT_HEADLESS_CONNECT_TO_SATELLITE", True),
        HEADLESS_PARALLEL_LOGIN=_truthy("BOT_HEADLESS_PARALLEL_LOGIN", True),
        HEADLESS_PARALLEL_START_STAGGER_SEC=_float(
            "BOT_HEADLESS_PARALLEL_START_STAGGER_SEC", 0.4
        ),
        HEADLESS_EXIT_AFTER_LOGIN_SUCCESS=_truthy("BOT_HEADLESS_EXIT_AFTER_LOGIN_SUCCESS", True),
        HEADLESS_LOGIN_STAGGER_SEC=_float("BOT_HEADLESS_LOGIN_STAGGER_SEC", 6.0),
        HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES=_int_ge_zero(
            "BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES", 3
        ),
        HEADLESS_STAGGER_WIN121_EXTRA_SEC=_float("BOT_HEADLESS_STAGGER_WIN121_EXTRA_SEC", 4.0),
        HEADLESS_STAGGER_MAX_SEC=_float("BOT_HEADLESS_STAGGER_MAX_SEC", 15.0),
        PIP_INSTALL_CASEUS_GIT_UPGRADE=_truthy("BOT_PIP_INSTALL_CASEUS_GIT_UPGRADE", False),
        CASEUS_GIT_PIP_SPEC=str(
            os.environ.get(
                "BOT_CASEUS_GIT_PIP_SPEC",
                "caseus @ git+https://github.com/friedkeenan/caseus.git",
            )
            or "caseus @ git+https://github.com/friedkeenan/caseus.git"
        ),
        PIP_INSTALL_TFM_SECRETS_CLI=_truthy("BOT_PIP_INSTALL_TFM_SECRETS_CLI", False),
        TFM_SECRETS_PIP_INSTALL_SPEC=str(os.environ.get("BOT_TFM_SECRETS_PIP_INSTALL_SPEC", "") or ""),
    )


def env_truthy(key: str) -> bool:
    """Read a boolean from ``os.environ`` (after ``prepare_runtime_environment``)."""
    return _truthy(key, False)
