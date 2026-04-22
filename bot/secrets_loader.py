"""
Load Transformice server crypto secrets for the proxy upstream connection.

Sources (in priority order when ``BOT_SECRETS_ALWAYS_REFRESH`` is true):
1. ``tfm-secrets`` CLI on PATH / venv Scripts.
2. ``TFMSecretsLeaker.swf`` via Flash debug projector (``flashplayer_32_sa_debug.exe``).
3. ``TFM_SECRETS_*`` keys already in ``.env``.
4. ``BOT_SECRETS_INLINE_JSON`` / ``BOT_SECRETS_INLINE`` dict on ``cfg``.

On success, secrets are persisted back to ``.env`` so the next run can skip the leaker.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from caseus import Secrets

from .env_setup import load_dotenv_file, repo_root, update_or_append_dotenv
from .tfm_secrets_acquire import try_load_secrets_via_leaker

logger = logging.getLogger(__name__)


def _resolved_dotenv_path(cfg: object) -> Path:
    rel = getattr(cfg, "SECRETS_DOTENV_PATH", None) or getattr(cfg, "HEADLESS_SECRETS_DOTENV_PATH", ".env")
    p = Path(str(rel).strip()).expanduser()
    if not p.is_absolute():
        p = (repo_root() / p).resolve()
    return p


def _secrets_from_config_inline(cfg: object) -> Secrets | None:
    """Build ``Secrets`` from inline dict config if present."""
    for attr in ("SECRETS_INLINE", "HEADLESS_SECRETS_INLINE"):
        raw = getattr(cfg, attr, None)
        if isinstance(raw, dict) and raw:
            kwargs = {k: raw[k] for k in Secrets._FIELDS if k in raw}
            if not kwargs:
                logger.warning("Inline secrets config has no recognized Secrets keys; ignored.")
                return None
            logger.info("Using inline secrets from config (fields: %s).", tuple(sorted(kwargs.keys())))
            return Secrets(**kwargs)
    return None


def _try_secrets_from_dumper_argv(argv: list[str], cfg: object) -> Secrets | None:
    """Run a secrets dumper subprocess; parse JSON from stdout into ``Secrets``."""
    timeout = float(getattr(cfg, "SECRETS_DUMPER_TIMEOUT_SEC", None) or getattr(cfg, "HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC", 120.0) or 120.0)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(1.0, timeout),
        )
    except FileNotFoundError:
        logger.warning("Secrets dumper not found: argv=%r", argv)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Secrets dumper timed out after %ss: argv=%r", timeout, argv)
        return None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        out = proc.stdout.decode("utf-8", errors="replace")
        logger.warning(
            "Secrets dumper failed (exit %s): stderr=%r stdout_preview=%r",
            proc.returncode,
            err[:2000],
            out[:500],
        )
        return None
    try:
        data: dict[str, Any] = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("Secrets dumper stdout is not JSON: %s", e)
        return None
    kwargs = {k: data[k] for k in Secrets._FIELDS if k in data}
    if not kwargs:
        logger.warning("Dumper JSON had no recognized Secrets fields.")
        return None
    try:
        return Secrets(**kwargs)
    except (TypeError, ValueError) as e:
        logger.warning("Could not build Secrets from dumper JSON: %s", e)
        return None


def _try_argv_for_string_dumper(name: str) -> list[str] | None:
    p = Path(name)
    if p.is_file():
        return [str(p.resolve())]
    found = shutil.which(name)
    if found:
        return [found]
    bindir = Path(sys.executable).resolve().parent
    if sys.platform == "win32":
        for suffix in (".exe", ".cmd", ".bat"):
            cand = bindir / f"{name}{suffix}"
            if cand.is_file():
                return [str(cand.resolve())]
    else:
        cand = bindir / name
        if cand.is_file():
            return [str(cand.resolve())]
    return None


def _dumper_argv_from_config(dumper: Any) -> list[str] | None:
    if isinstance(dumper, (list, tuple)):
        parts = [str(x) for x in dumper if str(x).strip()]
        if not parts:
            return None
        if len(parts) == 1:
            return _dumper_argv_from_config(parts[0])
        return parts
    s = str(dumper).strip()
    if not s:
        return None
    return _try_argv_for_string_dumper(s)


def _effective_dumper_spec(cfg: object) -> Any:
    """Explicit ``SECRETS_DUMPER`` setting, else ``tfm-secrets`` when auto-dump is enabled."""
    for attr in ("SECRETS_DUMPER", "HEADLESS_SECRETS_DUMPER"):
        d = getattr(cfg, attr, None)
        if isinstance(d, (list, tuple)):
            if any(str(x).strip() for x in d):
                return d
        elif d is not None and str(d).strip():
            return d
    auto_dump = getattr(cfg, "SECRETS_AUTO_DUMPER", None)
    if auto_dump is None:
        auto_dump = getattr(cfg, "HEADLESS_SECRETS_AUTO_DUMPER", True)
    if bool(auto_dump):
        return "tfm-secrets"
    return None


def _secrets_to_env_updates(secrets: Secrets, prefix: str) -> dict[str, str]:
    p = str(prefix).strip()
    if not p.endswith("_"):
        p = f"{p}_"
    cvt = secrets.client_verification_template
    if cvt is None:
        cvt_s = ""
    elif isinstance(cvt, (bytes, bytearray)):
        cvt_s = bytes(cvt).hex()
    else:
        cvt_s = str(cvt)
    ports = secrets.server_ports
    pks = secrets.packet_key_sources
    return {
        f"{p}SERVER_ADDRESS": str(secrets.server_address),
        f"{p}SERVER_PORTS": ",".join(str(int(x)) for x in ports),
        f"{p}GAME_VERSION": str(int(secrets.game_version)),
        f"{p}CONNECTION_TOKEN": str(secrets.connection_token),
        f"{p}AUTH_KEY": str(int(secrets.auth_key)),
        f"{p}PACKET_KEY_SOURCES": ",".join(str(int(x)) for x in pks),
        f"{p}CLIENT_VERIFICATION_TEMPLATE": cvt_s,
    }


def _persist_secrets_to_dotenv(sec: Secrets, cfg: object, dot: Path) -> None:
    pfx = str(
        getattr(cfg, "SECRETS_ENV_PREFIX", None)
        or getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_")
        or "TFM_SECRETS_"
    )
    persist = getattr(cfg, "SECRETS_PERSIST_DUMP_TO_DOTENV", None)
    if persist is None:
        persist = getattr(cfg, "HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV", True)
    if bool(persist):
        updates = _secrets_to_env_updates(sec, pfx)
        try:
            update_or_append_dotenv(dot, updates)
            for k, v in updates.items():
                os.environ[k] = v
            logger.info("Wrote %s TFM secrets fields to %s.", len(updates), dot)
        except OSError as e:
            logger.warning("Could not persist secrets to %s: %s", dot, e)
    else:
        logger.info("Not writing secrets to .env (BOT_SECRETS_PERSIST_DUMP_TO_DOTENV is false).")


def _secrets_from_env(cfg: object) -> Secrets | None:
    """Build ``Secrets`` from ``TFM_SECRETS_*`` environment variables."""
    prefix = str(
        getattr(cfg, "SECRETS_ENV_PREFIX", None)
        or getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_")
        or "TFM_SECRETS_"
    ).strip()
    if not prefix.endswith("_"):
        prefix = f"{prefix}_"

    def ge(suffix: str) -> str | None:
        v = os.environ.get(f"{prefix}{suffix}")
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    addr = ge("SERVER_ADDRESS")
    ports_s = ge("SERVER_PORTS")
    gv = ge("GAME_VERSION")
    tok = ge("CONNECTION_TOKEN")
    ak = ge("AUTH_KEY")
    pks = ge("PACKET_KEY_SOURCES")
    cvt = ge("CLIENT_VERIFICATION_TEMPLATE")

    if not any((addr, ports_s, gv, tok, ak, pks, cvt)):
        return None

    kwargs: dict[str, Any] = {}
    if addr:
        kwargs["server_address"] = addr
    if ports_s:
        kwargs["server_ports"] = tuple(
            int(x.strip()) for x in ports_s.replace(" ", "").split(",") if x.strip()
        )
    if gv:
        kwargs["game_version"] = int(gv, 0)
    if tok:
        kwargs["connection_token"] = tok
    if ak:
        kwargs["auth_key"] = int(ak, 0)
    if pks:
        kwargs["packet_key_sources"] = tuple(
            int(x.strip()) for x in pks.replace(" ", "").split(",") if x.strip()
        )
    if cvt:
        kwargs["client_verification_template"] = cvt

    missing = [f for f in Secrets._FIELDS if f not in kwargs]
    if missing:
        logger.debug(
            "Incomplete %s* in env: missing %s (filled: %s); may run secrets dumper next.",
            prefix,
            missing,
            tuple(sorted(kwargs.keys())) or "(none)",
        )
        return None
    try:
        return Secrets(**kwargs)
    except (TypeError, ValueError) as e:
        logger.error("Invalid values in environment for Secrets: %s", e)
        return None


def _try_acquire_secrets_from_dumpers(cfg: object, dot: Path, *, reason: str) -> Secrets | None:
    """Run ``tfm-secrets`` and/or Flash leaker; persist to ``.env`` on success."""
    dumper_spec = _effective_dumper_spec(cfg)
    if dumper_spec is not None:
        argv = _dumper_argv_from_config(dumper_spec)
        spec = str(getattr(cfg, "TFM_SECRETS_PIP_INSTALL_SPEC", "") or "").strip()
        if (
            argv is None
            and spec
            and not getattr(sys, "frozen", False)
            and bool(getattr(cfg, "SECRETS_AUTO_PIP_BEFORE_DUMPER", None) or getattr(cfg, "HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER", True))
        ):
            logger.info("tfm-secrets not found; running pip install -U %s.", spec)
            subprocess.run([sys.executable, "-m", "pip", "install", "-U", spec], check=False)
            argv = _dumper_argv_from_config(dumper_spec)
        if argv:
            if reason == "always_refresh":
                logger.info("Running secrets dumper argv=%r (always-refresh).", argv)
            else:
                logger.info("TFM secrets incomplete; running dumper argv=%r.", argv)
            sec = _try_secrets_from_dumper_argv(argv, cfg)
            if sec is not None:
                _persist_secrets_to_dotenv(sec, cfg, dot)
                return sec
            logger.warning("Secrets dumper ran but did not return usable secrets.")
        else:
            bindir = Path(sys.executable).resolve().parent
            logger.info(
                "tfm-secrets executable not found (checked PATH and %s). "
                "Install the CLI, set BOT_TFM_SECRETS_PIP_INSTALL_SPEC + pip retry, or use Flash leaker below.",
                bindir,
            )

    auto_leaker = getattr(cfg, "SECRETS_AUTO_LEAKER_SWF", None)
    if auto_leaker is None:
        auto_leaker = getattr(cfg, "HEADLESS_SECRETS_AUTO_LEAKER_SWF", True)
    if bool(auto_leaker):
        sec = try_load_secrets_via_leaker(repo_root())
        if sec is not None:
            logger.info("Secrets from TFMSecretsLeaker.swf.")
            _persist_secrets_to_dotenv(sec, cfg, dot)
            return sec
    return None


def load_secrets(cfg: object) -> Secrets:
    """
    Load TFM server crypto secrets.

    Tries live dumper/leaker first (if ``BOT_SECRETS_ALWAYS_REFRESH`` is true), then
    ``TFM_SECRETS_*`` from ``.env``, then inline config, then dumper as last resort.
    Raises ``SystemExit(1)`` if no secrets can be found.
    """
    dot = _resolved_dotenv_path(cfg)
    load_dotenv_file(dot)

    always_refresh = getattr(cfg, "SECRETS_ALWAYS_REFRESH", None)
    if always_refresh is None:
        always_refresh = getattr(cfg, "HEADLESS_SECRETS_ALWAYS_REFRESH", True)

    tried_live = False
    if bool(always_refresh):
        logger.info("Loading TFM secrets (always-refresh: trying live leaker/dumper first).")
        sec = _try_acquire_secrets_from_dumpers(cfg, dot, reason="always_refresh")
        tried_live = True
        if sec is not None:
            return sec
        logger.warning("Live secrets refresh failed; falling back to cached TFM_SECRETS_* in .env.")

    sec = _secrets_from_env(cfg)
    if sec is not None:
        logger.info("Secrets loaded from environment / .env.")
        return sec

    sec = _secrets_from_config_inline(cfg)
    if sec is not None:
        return sec

    if not tried_live:
        sec = _try_acquire_secrets_from_dumpers(cfg, dot, reason="env_incomplete")
        if sec is not None:
            return sec

    pfx = str(
        getattr(cfg, "SECRETS_ENV_PREFIX", None)
        or getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_")
        or "TFM_SECRETS_"
    ).strip()
    if not pfx.endswith("_"):
        pfx = f"{pfx}_"
    nonempty = sorted(k for k in os.environ if k.startswith(pfx) and str(os.environ.get(k, "")).strip())
    logger.error(
        "Expected secrets file: %s (exists=%s). Non-empty %s* keys: %s",
        dot,
        dot.is_file(),
        pfx,
        nonempty or "(none)",
    )
    msg = (
        "Cannot load TFM secrets. Options: "
        "(1) Place flashplayer_32_sa_debug.exe in the repo root so TFMSecretsLeaker.swf can run; "
        "(2) install tfm-secrets CLI on PATH / set BOT_TFM_SECRETS_PIP_INSTALL_SPEC; "
        "(3) fill TFM_SECRETS_* in .env manually; "
        "(4) set BOT_SECRETS_INLINE_JSON in .env."
    )
    logger.error(msg)
    raise SystemExit(1)


# Kept for backward compat; new code should call load_secrets().
load_secrets_base = load_secrets


def sync_upstream_cfg_from_secrets(cfg: object, secrets: Secrets) -> None:
    """
    Align ``BOT_UPSTREAM_*`` with the secrets dump (host + ports).

    Forces dump-only upstream and disables address mismatch to prevent zero-byte
    handshake closes from connecting to the wrong IP with mismatched crypto.
    """
    if not bool(getattr(cfg, "UPSTREAM_AUTO_SYNC_FROM_SECRETS", True)):
        return
    addr = str(getattr(secrets, "server_address", "") or "").strip()
    ports_raw = getattr(secrets, "server_ports", None)
    if not addr or not ports_raw:
        logger.warning("sync_upstream_cfg_from_secrets: secrets missing server_address/server_ports; skip.")
        return
    ports = tuple(int(x) for x in ports_raw)
    ports_s = ",".join(str(p) for p in ports)
    updates = {
        "BOT_UPSTREAM_SERVER_ADDRESS": addr,
        "BOT_UPSTREAM_SERVER_PORTS": ports_s,
        "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY": "true",
        "BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH": "false",
    }
    dot = _resolved_dotenv_path(cfg)
    try:
        update_or_append_dotenv(dot, updates)
    except OSError as e:
        logger.warning("Could not persist upstream sync to %s: %s", dot, e)
        return
    for k, v in updates.items():
        os.environ[k] = v
    cfg.UPSTREAM_SERVER_ADDRESS = addr
    cfg.UPSTREAM_SERVER_PORTS = ports
    cfg.UPSTREAM_FROM_SECRETS_DUMP_ONLY = True
    cfg.UPSTREAM_ALLOW_ADDRESS_MISMATCH = False
    logger.info(
        "Synced BOT_UPSTREAM_* from secrets dump (host=%s ports=%s).",
        addr,
        ports,
    )


def _upstream_ports_try_main_first(cfg: object, ports: tuple[int, ...]) -> tuple[int, ...]:
    """Put the main game TCP port (default 11801) first to avoid satellite zero-byte closes."""
    if not ports:
        return ports
    v = getattr(cfg, "UPSTREAM_MAIN_GAME_PORT_TRY_FIRST", 11801)
    if v is False or v is None:
        return ports
    m = int(v)
    if m not in ports:
        return ports
    lst = list(ports)
    lst.remove(m)
    out = (m,) + tuple(lst)
    if out != ports:
        logger.info("Upstream TCP try order: main port %s first (was %s).", m, ports)
    return out


def resolve_upstream(cfg: object, secrets: Secrets) -> tuple[str, tuple[int, ...]]:
    """
    Pick upstream host/ports from secrets dump and/or ``BOT_UPSTREAM_*`` config.

    Raises ``SystemExit(1)`` if host/ports cannot be determined.
    """
    dump_a = getattr(secrets, "server_address", None)
    dump_p = getattr(secrets, "server_ports", None)

    if bool(getattr(cfg, "UPSTREAM_FROM_SECRETS_DUMP_ONLY", False)):
        if not dump_a or not dump_p:
            logger.error("UPSTREAM_FROM_SECRETS_DUMP_ONLY is True but secrets lack server_address/server_ports.")
            raise SystemExit(1)
        return str(dump_a).strip(), _upstream_ports_try_main_first(
            cfg, tuple(int(x) for x in dump_p)
        )

    ua = getattr(cfg, "UPSTREAM_SERVER_ADDRESS", None)
    up = getattr(cfg, "UPSTREAM_SERVER_PORTS", None)

    ua_s = str(ua).strip() if ua is not None else ""
    if ua_s and up is not None and len(tuple(up)) > 0:
        chosen_a = ua_s
        chosen_p = tuple(int(x) for x in up)
        match_dump_order = bool(getattr(cfg, "UPSTREAM_PORTS_MATCH_DUMP_ORDER", True))
        effective_p = chosen_p
        if dump_a and dump_p:
            dump_p_i = tuple(int(x) for x in dump_p)
            same_a = chosen_a == str(dump_a).strip()
            strict = bool(getattr(cfg, "UPSTREAM_STRICT_MATCH_SECRETS_DUMP", False))
            if strict:
                same_p = chosen_p == dump_p_i
            else:
                same_p = len(chosen_p) == len(dump_p_i) and sorted(chosen_p) == sorted(dump_p_i)
            if not same_a:
                if not bool(getattr(cfg, "UPSTREAM_ALLOW_ADDRESS_MISMATCH", False)):
                    logger.error(
                        "BOT_UPSTREAM_SERVER_ADDRESS %r does not match dump host %r. "
                        "Set BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY=true or BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH=true.",
                        chosen_a,
                        str(dump_a).strip(),
                    )
                    raise SystemExit(1)
                logger.warning("UPSTREAM_ALLOW_ADDRESS_MISMATCH=true: using %r (dump host is %r).", chosen_a, str(dump_a).strip())
            if not same_p:
                msg = f"BOT_UPSTREAM_SERVER_PORTS {chosen_p} differs from dump {dump_p_i}. Align ports or clear the override."
                if strict:
                    logger.error(msg)
                    raise SystemExit(msg)
                logger.warning(msg)
            elif same_p and match_dump_order:
                effective_p = dump_p_i
        return chosen_a, _upstream_ports_try_main_first(cfg, effective_p)

    if not dump_a or not dump_p:
        logger.error(
            "Cannot resolve upstream: set BOT_UPSTREAM_SERVER_ADDRESS + BOT_UPSTREAM_SERVER_PORTS in .env, "
            "or allow the secrets leaker to auto-populate them."
        )
        raise SystemExit(1)
    return str(dump_a).strip(), _upstream_ports_try_main_first(cfg, tuple(int(x) for x in dump_p))


# Kept for backward compat.
resolve_headless_upstream = resolve_upstream
