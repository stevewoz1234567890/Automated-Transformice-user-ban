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

from .bot_env_defaults import apply_process_env_defaults

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


def require_source_runtime_imports() -> None:
    """
    Ensure critical packages are importable. If any are missing, attempt automatic
    ``pip install -r requirements.txt``. Skipped for PyInstaller builds.
    """
    if getattr(sys, "frozen", False):
        return
    need = ("pak", "caseus", "colorama")
    missing: list[str] = []
    for name in need:
        try:
            __import__(name)
        except ModuleNotFoundError:
            missing.append(name)
    if not missing:
        return
    root = repo_root()
    req = root / "requirements.txt"
    if req.is_file():
        import subprocess as _sp
        print(
            f"[auto-setup] Missing package(s): {', '.join(missing)}\n"
            f"[auto-setup] Running: {sys.executable} -m pip install -r {req}\n",
            file=sys.stderr,
        )
        _sp.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            check=False,
        )
        still_missing: list[str] = []
        for name in need:
            try:
                __import__(name)
            except ModuleNotFoundError:
                still_missing.append(name)
        if not still_missing:
            print("[auto-setup] All packages installed successfully.\n", file=sys.stderr)
            return
        missing = still_missing
    print(
        "Missing Python package(s): "
        + ", ".join(missing)
        + ".\n"
        "From the repository root, run:\n"
        f"  {sys.executable} -m pip install -r requirements.txt\n"
        f"Repository: {root}\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


def merge_repo_tfm_secrets_json(repo_root: Path, *, prefix: str = "TFM_SECRETS_") -> None:
    """Fill missing ``TFM_SECRETS_*`` from ``tfm-secrets.json`` (same shape as export_tfm_secrets_json)."""

    def _truthy_merge(key: str, default: bool = True) -> bool:
        v = os.environ.get(key)
        if v is None or not str(v).strip():
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    if not _truthy_merge("BOT_MERGE_TFM_SECRETS_JSON", True):
        return
    raw_path = (os.environ.get("BOT_TFM_SECRETS_JSON_PATH") or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (repo_root / path).resolve()
    else:
        path = repo_root / "tfm-secrets.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[secrets] Cannot read merge file %s (%s)", path, e)
        return
    if not isinstance(data, dict):
        logger.warning("[secrets] %s: expected JSON object, got %s", path, type(data).__name__)
        return
    pfx = str(prefix).strip()
    if not pfx.endswith("_"):
        pfx = f"{pfx}_"

    updates: dict[str, str] = {}
    sv = data.get("server_address")
    if isinstance(sv, str) and sv.strip():
        updates[f"{pfx}SERVER_ADDRESS"] = sv.strip()
    ports = data.get("server_ports")
    if isinstance(ports, (list, tuple)) and ports:
        try:
            updates[f"{pfx}SERVER_PORTS"] = ",".join(str(int(x)) for x in ports)
        except (TypeError, ValueError):
            pass
    gv = data.get("game_version")
    if gv is not None:
        try:
            updates[f"{pfx}GAME_VERSION"] = str(int(gv))
        except (TypeError, ValueError):
            pass
    tok = data.get("connection_token")
    if isinstance(tok, str) and tok.strip():
        updates[f"{pfx}CONNECTION_TOKEN"] = tok.strip()
    ak = data.get("auth_key")
    if ak is not None:
        try:
            updates[f"{pfx}AUTH_KEY"] = str(int(ak))
        except (TypeError, ValueError):
            pass
    pks = data.get("packet_key_sources")
    if isinstance(pks, (list, tuple)) and pks:
        try:
            updates[f"{pfx}PACKET_KEY_SOURCES"] = ",".join(str(int(x)) for x in pks)
        except (TypeError, ValueError):
            pass
    cvt = data.get("client_verification_template")
    if isinstance(cvt, (bytes, bytearray)):
        updates[f"{pfx}CLIENT_VERIFICATION_TEMPLATE"] = bytes(cvt).hex()
    elif isinstance(cvt, str) and cvt.strip():
        updates[f"{pfx}CLIENT_VERIFICATION_TEMPLATE"] = cvt.strip()

    if not updates:
        logger.warning("[secrets] %s: no recognizable fields merged", path)
        return

    merged = 0
    applied: list[str] = []
    for k, val in updates.items():
        prior = os.environ.get(k)
        if prior is None or not str(prior).strip():
            os.environ[k] = val
            merged += 1
            suffix = k.removeprefix(pfx)
            applied.append(suffix)
            continue
    if merged > 0:
        logger.info(
            "[secrets] Merged %s field(s) from %s into empty TFM env keys (%s); .env overrides are kept.",
            merged,
            path,
            ",".join(applied[:12]) + ("…" if len(applied) > 12 else ""),
        )


def sync_upstream_env_with_tfm_secrets() -> None:
    """Match ``BOT_UPSTREAM_SERVER_*`` to ``TFM_SECRETS_*`` when auto-sync is on (Flash + headless)."""
    v = os.environ.get("BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS", "true")
    if str(v).strip().lower() in ("0", "false", "no", "off"):
        return
    addr = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or "").strip()
    ports_s = (os.environ.get("TFM_SECRETS_SERVER_PORTS") or "").strip()
    if not addr or not ports_s:
        return
    os.environ["BOT_UPSTREAM_SERVER_ADDRESS"] = addr
    os.environ["BOT_UPSTREAM_SERVER_PORTS"] = ports_s
    os.environ["BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY"] = "true"
    os.environ["BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH"] = "false"
    logger.info(
        "[secrets] BOT_UPSTREAM_* synced from TFM_SECRETS_* (address=%s ports=%s)",
        addr,
        ports_s,
    )


def prepare_runtime_environment() -> None:
    """Bootstrap ``.env``, optional JSON secrets merge, then apply code-side defaults to ``os.environ``."""
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
    if not dot.is_file():
        logger.warning(
            ".env file not found at %s — bot will use coded defaults. "
            "Copy .env.example to .env and configure BOT_ACCOUNTS_JSON.",
            dot,
        )
    load_dotenv_file(dot)
    merge_repo_tfm_secrets_json(root, prefix=os.environ.get("BOT_HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_")
    apply_process_env_defaults()
    sync_upstream_env_with_tfm_secrets()
    _warn_if_accounts_unconfigured()


def _warn_if_accounts_unconfigured() -> None:
    """Log a warning if BOT_ACCOUNTS_JSON has no real credentials (first-run hint)."""
    raw = (os.environ.get("BOT_ACCOUNTS_JSON") or "").strip()
    if not raw or raw == "[]":
        logger.warning(
            "BOT_ACCOUNTS_JSON is empty — edit .env and add your Transformice account credentials."
        )
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = _parse_accounts_list(raw)
        except ValueError:
            return
    if not isinstance(data, list) or not data:
        return
    empty = sum(
        1 for r in data
        if not str(r.get("username", "")).strip() or not str(r.get("password", "")).strip()
    )
    if empty == len(data):
        logger.warning(
            "All %d account(s) in BOT_ACCOUNTS_JSON have empty username or password. "
            "Edit .env and fill in your Transformice credentials before running.",
            len(data),
        )


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
