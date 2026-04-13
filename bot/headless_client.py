"""
One ``caseus.Client`` per slot connecting to the local BanBotProxy (no Flash).

Secrets (no ``tfm-secrets.json`` in this flow):

1. Complete ``TFM_SECRETS_*`` in repo-root ``.env`` after ``load_dotenv_file``.
2. Or ``BOT_HEADLESS_SECRETS_INLINE_JSON`` on ``cfg``.
3. Or ``tfm-secrets`` on stdout when ``BOT_HEADLESS_SECRETS_AUTO_DUMPER`` is true (default).
4. Or TFMSecretsLeaker.swf via Flash debug projector when ``BOT_HEADLESS_SECRETS_AUTO_LEAKER_SWF``
   is true (default): place ``flashplayer_32_sa_debug.exe`` in the repo root or set
   ``FLASHPLAYER_DEBUG``; the leaker SWF is downloaded to ``tmp/`` automatically.
   On success, values are written back to ``.env`` when ``BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV``
   is true (default).

With ``BOT_HEADLESS_SECRETS_ALWAYS_REFRESH`` (default true), tfm-secrets / leaker run **before** trusting
cached ``TFM_SECRETS_*``. ``sync_upstream_cfg_from_secrets`` then aligns ``BOT_UPSTREAM_*`` with the dump.

Each client uses ``Secrets.copy(server_address=..., server_ports=(...))`` to target ``127.0.0.1:<proxy_port>``.
The proxy injects ``LoginPacket`` after ``SystemInformationPacket``; this client must **not** send
its own ``LoginPacket`` (see ``HeadlessProxyClient.login`` no-op).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pak
from caseus import Secrets

from .env_setup import load_dotenv_file, repo_root, update_or_append_dotenv
from .tfm_secrets_acquire import try_load_secrets_via_leaker
from caseus.clients.client import AccountError, Client
from caseus.packets import clientbound, serverbound
from caseus.util.crypto import shakikoo

logger = logging.getLogger(__name__)


def _resolved_dotenv_path(cfg: object) -> Path:
    rel = getattr(cfg, "HEADLESS_SECRETS_DOTENV_PATH", ".env")
    p = Path(str(rel).strip()).expanduser()
    if not p.is_absolute():
        p = (repo_root() / p).resolve()
    return p


def _secrets_from_config_inline(cfg: object) -> Secrets | None:
    """Build ``Secrets`` from ``HEADLESS_SECRETS_INLINE`` if it is a non-empty dict with known fields."""
    raw = getattr(cfg, "HEADLESS_SECRETS_INLINE", None)
    if not isinstance(raw, dict) or not raw:
        return None
    kwargs = {k: raw[k] for k in Secrets._FIELDS if k in raw}
    if not kwargs:
        logger.warning(
            "HEADLESS_SECRETS_INLINE is set but contains no recognized Secrets keys %s; ignored.",
            Secrets._FIELDS,
        )
        return None
    logger.info(
        "Using HEADLESS_SECRETS_INLINE from .env (fields: %s).",
        tuple(sorted(kwargs.keys())),
    )
    return Secrets(**kwargs)


def _try_secrets_from_dumper_argv(argv: list[str], cfg: object) -> Secrets | None:
    """Run a secrets dumper; parse JSON from stdout into ``Secrets`` (``.env`` may be updated later)."""
    timeout = float(getattr(cfg, "HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC", 120.0) or 120.0)
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
    """Resolve a single dumper token to argv, including venv ``Scripts`` on Windows."""
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
    """Explicit ``HEADLESS_SECRETS_DUMPER``, else ``tfm-secrets`` when auto-dump is enabled."""
    d = getattr(cfg, "HEADLESS_SECRETS_DUMPER", None)
    if isinstance(d, (list, tuple)):
        if any(str(x).strip() for x in d):
            return d
    elif d is not None and str(d).strip():
        return d
    if bool(getattr(cfg, "HEADLESS_SECRETS_AUTO_DUMPER", True)):
        return "tfm-secrets"
    return None


def _secrets_to_tfm_env_updates(secrets: Secrets, prefix: str) -> dict[str, str]:
    """Map a ``Secrets`` instance to ``TFM_SECRETS_*``-style keys for ``.env``."""
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
    pfx = str(getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_")
    if bool(getattr(cfg, "HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV", True)):
        updates = _secrets_to_tfm_env_updates(sec, pfx)
        try:
            update_or_append_dotenv(dot, updates)
            for k, v in updates.items():
                os.environ[k] = v
            logger.info(
                "Wrote %s TFM secrets fields to %s (BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV).",
                len(updates),
                dot,
            )
        except OSError as e:
            logger.warning("Could not persist secrets to %s: %s", dot, e)
    else:
        logger.info(
            "Not writing secrets to .env (BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV is false)."
        )


def _secrets_from_env(cfg: object) -> Secrets | None:
    """Build ``Secrets`` from ``TFM_SECRETS_*`` (prefix configurable) in ``os.environ``."""
    prefix = str(getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_").strip()
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
            "Incomplete %s* / .env: missing %s (filled: %s); may run tfm-secrets dumper next.",
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
    """Run ``tfm-secrets`` and/or Flash leaker; persist to ``.env`` on success. ``reason`` selects log wording."""
    dumper_spec = _effective_dumper_spec(cfg)
    if dumper_spec is not None:
        argv = _dumper_argv_from_config(dumper_spec)
        spec = str(getattr(cfg, "TFM_SECRETS_PIP_INSTALL_SPEC", "") or "").strip()
        if (
            argv is None
            and spec
            and not getattr(sys, "frozen", False)
            and bool(getattr(cfg, "HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER", True))
        ):
            logger.info(
                "tfm-secrets not found; running pip install -U %s (BOT_TFM_SECRETS_PIP_INSTALL_SPEC).",
                spec,
            )
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", spec],
                check=False,
            )
            argv = _dumper_argv_from_config(dumper_spec)
        if argv:
            if reason == "always_refresh":
                logger.info(
                    "Running secrets dumper argv=%r (BOT_HEADLESS_SECRETS_ALWAYS_REFRESH).",
                    argv,
                )
            else:
                logger.info(
                    "TFM secrets incomplete in .env; running dumper argv=%r (set BOT_HEADLESS_SECRETS_AUTO_DUMPER=false to skip).",
                    argv,
                )
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

    if bool(getattr(cfg, "HEADLESS_SECRETS_AUTO_LEAKER_SWF", True)):
        sec = try_load_secrets_via_leaker(repo_root())
        if sec is not None:
            if reason == "always_refresh":
                logger.info("Secrets from TFMSecretsLeaker.swf (always-refresh path).")
            else:
                logger.info("Secrets from TFMSecretsLeaker.swf (Flash debug projector).")
            _persist_secrets_to_dotenv(sec, cfg, dot)
            return sec
    return None


def load_secrets_base(cfg: object) -> Secrets:
    """Load server crypto: optional live dumper/leaker first, then ``.env``, inline, then dumper fallback."""
    dot = _resolved_dotenv_path(cfg)
    load_dotenv_file(dot)

    tried_live = False
    if bool(getattr(cfg, "HEADLESS_SECRETS_ALWAYS_REFRESH", True)):
        logger.info(
            "BOT_HEADLESS_SECRETS_ALWAYS_REFRESH: attempting live tfm-secrets / Flash leaker before cached .env.",
        )
        sec = _try_acquire_secrets_from_dumpers(cfg, dot, reason="always_refresh")
        tried_live = True
        if sec is not None:
            return sec
        logger.warning(
            "Live secrets refresh failed or was skipped; using complete TFM_SECRETS_* from .env if available.",
        )

    sec = _secrets_from_env(cfg)
    if sec is not None:
        logger.info(
            "Secrets from environment / %s (prefix %r).",
            getattr(cfg, "HEADLESS_SECRETS_DOTENV_PATH", ".env"),
            str(getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_"),
        )
        return sec

    sec = _secrets_from_config_inline(cfg)
    if sec is not None:
        return sec

    if not tried_live:
        sec = _try_acquire_secrets_from_dumpers(cfg, dot, reason="env_incomplete")
        if sec is not None:
            return sec

    pfx = str(getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_").strip()
    if not pfx.endswith("_"):
        pfx = f"{pfx}_"
    nonempty = sorted(k for k in os.environ if k.startswith(pfx) and str(os.environ.get(k, "")).strip())
    logger.error(
        "Expected secrets file: %s (exists=%s). Non-empty %s* keys after load: %s",
        dot,
        dot.is_file(),
        pfx,
        nonempty or "(none)",
    )
    msg = (
        "Headless needs secrets: (1) Place flashplayer_32_sa_debug.exe in the repo root (or set FLASHPLAYER_DEBUG) "
        "so TFMSecretsLeaker.swf can run; (2) or install tfm-secrets on PATH / set BOT_TFM_SECRETS_PIP_INSTALL_SPEC; "
        "(3) or fill TFM_SECRETS_* in .env; (4) or set BOT_HEADLESS_SECRETS_INLINE_JSON. "
        "Disable Flash fallback with BOT_HEADLESS_SECRETS_AUTO_LEAKER_SWF=false if undesired."
    )
    logger.error(msg)
    raise SystemExit(1)


def sync_upstream_cfg_from_secrets(cfg: object, secrets: Secrets) -> None:
    """
    Align ``BOT_UPSTREAM_*`` with the secrets dump (host + ports), force dump-only upstream, and turn off
    address mismatch — avoids zero-byte handshake closes from TCP to the wrong IP while using another dump's crypto.
    """
    if not bool(getattr(cfg, "UPSTREAM_AUTO_SYNC_FROM_SECRETS", True)):
        return
    addr = str(getattr(secrets, "server_address", "") or "").strip()
    ports_raw = getattr(secrets, "server_ports", None)
    if not addr or not ports_raw:
        logger.warning(
            "BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS: dump missing server_address/server_ports; skip sync.",
        )
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
        "Synced BOT_UPSTREAM_* from secrets dump (host=%s ports=%s); "
        "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY=true, BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH=false.",
        addr,
        ports,
    )


def _upstream_ports_try_main_first(cfg: object, ports: tuple[int, ...]) -> tuple[int, ...]:
    """Put the main game TCP port first (default 11801). Connecting to satellite (e.g. 12801) first often gets zero-byte closes for a main HandshakePacket."""
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
        logger.info(
            "Upstream TCP try order: main port %s first (was %s).",
            m,
            ports,
        )
    return out


def resolve_headless_upstream(cfg: object, base_secrets: Secrets) -> tuple[str, tuple[int, ...]]:
    """Pick upstream host/ports; warn or exit if config overrides disagree with this run's dump."""
    dump_a = getattr(base_secrets, "server_address", None)
    dump_p = getattr(base_secrets, "server_ports", None)

    if bool(getattr(cfg, "UPSTREAM_FROM_SECRETS_DUMP_ONLY", False)):
        if not dump_a or not dump_p:
            logger.error(
                "UPSTREAM_FROM_SECRETS_DUMP_ONLY is True but Secrets lack server_address / server_ports."
            )
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
                same_p = len(chosen_p) == len(dump_p_i) and sorted(chosen_p) == sorted(
                    dump_p_i
                )
            if not same_a:
                if not bool(getattr(cfg, "UPSTREAM_ALLOW_ADDRESS_MISMATCH", False)):
                    logger.error(
                        "BOT_UPSTREAM_SERVER_ADDRESS %r does not match TFM_SECRETS_SERVER_ADDRESS / "
                        "dump host %r. The handshake uses crypto tied to the dump; TCP to another IP "
                        "often connects then closes with zero bytes (no HandshakeResponse). "
                        "Clear BOT_UPSTREAM_SERVER_ADDRESS and BOT_UPSTREAM_SERVER_PORTS, or set "
                        "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY=true, or set "
                        "BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH=true to force (may still fail).",
                        chosen_a,
                        str(dump_a).strip(),
                    )
                    raise SystemExit(1)
                logger.warning(
                    "BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH=true: using upstream %r (dump host is %r).",
                    chosen_a,
                    str(dump_a).strip(),
                )
            if not same_p:
                msg = (
                    f"BOT_UPSTREAM_SERVER_PORTS {chosen_p} differs from dump {dump_p_i}. "
                    "Wrong port set can accept TCP then drop the handshake — align ports or clear the override."
                )
                if strict:
                    logger.error(msg)
                    raise SystemExit(msg)
                logger.warning(msg)
            elif same_p and match_dump_order:
                effective_p = dump_p_i
                if chosen_p != dump_p_i:
                    logger.info(
                        "Upstream ports use secrets dump order %s (config listed %s).",
                        dump_p_i,
                        chosen_p,
                    )
        return chosen_a, _upstream_ports_try_main_first(cfg, effective_p)

    if not dump_a or not dump_p:
        logger.error(
            "Headless needs server_address and server_ports from the secrets dump, or set "
            "BOT_UPSTREAM_SERVER_ADDRESS and BOT_UPSTREAM_SERVER_PORTS in .env."
        )
        raise SystemExit(1)
    return str(dump_a).strip(), _upstream_ports_try_main_first(
        cfg, tuple(int(x) for x in dump_p)
    )


class HeadlessProxyClient(Client):
    """``caseus.Client`` that completes Handshake + SystemInformation; proxy sends ``LoginPacket``."""

    def __init__(
        self,
        *,
        login_success_event: threading.Event | None = None,
        exit_after_login_success: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._login_success_event = login_success_event
        self._exit_after_login_success = exit_after_login_success
        self._sysinfo_after_verification_pending = False

    async def _close_bootstrap_connections(self) -> None:
        """End ``listen()`` so ``start()`` returns; needed for multi-slot headless stagger."""
        await asyncio.sleep(0)
        try:
            if self.satellite is not self.main and not self.satellite.is_closing():
                self.satellite.close()
                await self.satellite.wait_closed()
        except (OSError, asyncio.CancelledError) as e:
            logger.debug("Headless satellite close: %s", e)
        try:
            if not self.main.is_closing():
                self.main.close()
                await self.main.wait_closed()
        except (OSError, asyncio.CancelledError) as e:
            logger.debug("Headless main close: %s", e)

    async def login(self) -> None:
        """Do not send ``LoginPacket`` — ``BanBotProxy`` injects it after ``SystemInformationPacket``."""
        return

    async def _emit_system_information_and_steam(self) -> None:
        await self.main.write_packet(
            serverbound.SystemInformationPacket,
            language=self.system_language,
            os=self.OS,
            flash_version=self.FLASH_VERSION,
        )
        if self.steam_id is not None:
            await self.main.write_packet(
                serverbound.SteamInfoPacket,
                user_id=self.steam_id,
            )

    @pak.packet_listener(clientbound.HandshakeResponsePacket)
    async def _on_handshake_response(self, server, packet):
        self.auth_token = packet.auth_token
        await self.set_desired_language(fallback=packet.language)
        if self.secrets.client_verification_template is not None:
            self._sysinfo_after_verification_pending = True
            return
        await self._emit_system_information_and_steam()

    @pak.packet_listener(clientbound.ClientVerificationPacket)
    async def _on_client_verification(self, server, packet):
        if self.secrets.client_verification_template is not None:
            await self.main.write_packet(
                serverbound.ClientVerificationPacket,
                ciphered_data=self.secrets.client_verification_data(
                    packet.verification_token,
                    ctx=self.main.ctx,
                ),
            )
        if self._sysinfo_after_verification_pending:
            self._sysinfo_after_verification_pending = False
            await self._emit_system_information_and_steam()
        if self.username is not None:
            await self.login()

    @pak.packet_listener(clientbound.LoginSuccessPacket)
    async def _on_login_success(self, server, packet):
        await super()._on_login_success(server, packet)
        if self._login_success_event is not None:
            self._login_success_event.set()
        if self._exit_after_login_success:
            logger.info(
                "Headless: LoginSuccess — closing client TCP so the next slot can run "
                "(BOT_HEADLESS_EXIT_AFTER_LOGIN_SUCCESS).",
            )
            asyncio.create_task(self._close_bootstrap_connections())


async def _run_one_client(
    *,
    secrets: Secrets,
    username: str,
    password: str,
    start_room: str,
    login_success_event: threading.Event,
    connect_to_satellite: bool,
    exit_after_login_success: bool,
) -> None:
    pw_hash = shakikoo(password.strip())
    client = HeadlessProxyClient(
        secrets=secrets,
        username=username,
        password_hash=pw_hash,
        start_room=start_room,
        login_success_event=login_success_event,
        connect_to_satellite=connect_to_satellite,
        exit_after_login_success=exit_after_login_success,
    )
    await client.start()


def _run_one_slot_headless(
    *,
    state: Any,
    row: dict[str, object],
    cfg: object,
    base_secrets: Secrets,
    exit_after_login_success: bool | None = None,
) -> None:
    label = state.label
    main_port = state.port
    connect_host = state.proxy_bind_host if state.proxy_bind_host else "127.0.0.1"
    secrets = base_secrets.copy(
        server_address=connect_host,
        server_ports=(main_port,),
    )

    username = str(row.get("username", "") or "").strip()
    password = str(row.get("password", "") or "")
    if not username or not password.strip():
        logger.error("Slot %s: headless login needs username and password in BOT_ACCOUNTS_JSON", label)
        return

    start_room = str(getattr(cfg, "PACKET_LOGIN_START_ROOM", "") or "")

    logger.info(
        "Slot %s: starting headless caseus.Client → %s:%s",
        label,
        connect_host,
        main_port,
    )
    exit_after = exit_after_login_success
    if exit_after is None:
        exit_after = bool(getattr(cfg, "HEADLESS_EXIT_AFTER_LOGIN_SUCCESS", True))
    try:
        asyncio.run(
            _run_one_client(
                secrets=secrets,
                username=username,
                password=password,
                start_room=start_room,
                login_success_event=state.login_success_event,
                connect_to_satellite=bool(getattr(cfg, "HEADLESS_CONNECT_TO_SATELLITE", True)),
                exit_after_login_success=exit_after,
            )
        )
        if not state.login_success_event.is_set():
            logger.warning(
                "Slot %s: [login] headless ended before LoginSuccess (upstream [login][diag] above)",
                label,
            )
        else:
            logger.debug("Slot %s: headless session ended (connection closed)", label)
    except AccountError as e:
        logger.error("Slot %s: [login] AccountError from server error_code=%s", label, e.error_code)
    except OSError as e:
        logger.error("Slot %s: [login] cannot reach proxy (check port): %s", label, e)
    except Exception:
        logger.exception("Slot %s: headless client failed", label)


def _count_win121_slots(states: list[Any]) -> int:
    n = 0
    for st in states:
        ev = getattr(st, "headless_seen_upstream_win121", None)
        if ev is not None and ev.is_set():
            n += 1
    return n


def start_headless_client_threads(
    states: list[Any],
    raw_accounts: list[dict[str, object]],
    cfg: object,
    *,
    base_secrets: Secrets | None = None,
) -> bool:
    """
    Run headless TCP login for every slot.

    **Sequential (** ``BOT_HEADLESS_PARALLEL_LOGIN=false``): one ``HeadlessProxyClient`` at a time on the
    main thread. ``BOT_HEADLESS_LOGIN_STAGGER_SEC`` is the pause between slots. Multi-slot ban mode will
    exit unless you use Flash clients, because ``HEADLESS_EXIT_AFTER_LOGIN_SUCCESS`` closes TCP after each login.

    **Parallel (default,** ``BOT_HEADLESS_PARALLEL_LOGIN=true``): one daemon thread per slot, each runs its own
    ``asyncio`` loop so every account can stay connected at once. Sessions keep the TCP session open
    (``exit_after_login_success`` is forced off for this path). Optional
    ``BOT_HEADLESS_PARALLEL_START_STAGGER_SEC`` offsets each thread's start by ``index * stagger`` to
    reduce simultaneous connects (WinError 121 / server throttling). After
    ``BOT_HEADLESS_PARALLEL_RETRY_AFTER_SEC``, failed slots (no ``LoginSuccess``, headless thread
    finished) can be retried once if ``BOT_HEADLESS_PARALLEL_RETRY_FAILED_SLOTS`` is true.

    Logs showing a second upstream host after login (e.g. shard IP) are normal game behavior.

    This function blocks until every slot reaches
    ``LoginSuccess`` or ``BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC`` elapses.

    ``HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES`` applies only to sequential mode.

    Returns ``False`` if sequential mode stopped early (consecutive failures) or parallel mode timed
    out before all slots logged in; ``True`` otherwise.
    """
    base = base_secrets if base_secrets is not None else load_secrets_base(cfg)
    pairs = list(zip(states, raw_accounts))

    if bool(getattr(cfg, "HEADLESS_PARALLEL_LOGIN", True)):
        if int(getattr(cfg, "HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES", 0) or 0) > 0:
            logger.info(
                "BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES is ignored when "
                "BOT_HEADLESS_PARALLEL_LOGIN is true.",
            )
        start_stagger = float(getattr(cfg, "HEADLESS_PARALLEL_START_STAGGER_SEC", 0.0) or 0.0)
        start_stagger = max(0.0, start_stagger)
        n = len(pairs)
        logger.info(
            "Headless parallel login: starting %s caseus.Client thread(s); "
            "sessions stay open after LoginSuccess.",
            n,
        )

        def _run_slot(idx: int, st: Any, rw: dict[str, object]) -> None:
            if start_stagger > 0 and idx > 0:
                time.sleep(start_stagger * idx)
            _run_one_slot_headless(
                state=st,
                row=rw,
                cfg=cfg,
                base_secrets=base,
                exit_after_login_success=False,
            )

        for i, (state, row) in enumerate(pairs):
            t = threading.Thread(
                target=_run_slot,
                args=(i, state, row),
                name=f"tfm-headless-{state.label}",
                daemon=True,
            )
            state.headless_thread = t
            t.start()

        timeout_sec = float(getattr(cfg, "ALL_SLOTS_LOGIN_TIMEOUT_SEC", 7200.0) or 7200.0)
        timeout_sec = max(60.0, timeout_sec)
        deadline = time.monotonic() + timeout_sec
        poll_sec = 0.25
        parallel_t0 = time.monotonic()
        retry_wave_done = False
        retry_after = float(getattr(cfg, "HEADLESS_PARALLEL_RETRY_AFTER_SEC", 90.0) or 90.0)
        retry_after = max(15.0, retry_after)
        retry_stagger = float(getattr(cfg, "HEADLESS_PARALLEL_RETRY_STAGGER_SEC", 5.0) or 0.0)
        retry_stagger = max(0.0, retry_stagger)

        def _retry_one(st: Any, rw: dict[str, object]) -> None:
            _run_one_slot_headless(
                state=st,
                row=rw,
                cfg=cfg,
                base_secrets=base,
                exit_after_login_success=False,
            )

        while time.monotonic() < deadline:
            if all(st.login_success_event.is_set() for st, _ in pairs):
                logger.info("Headless parallel login: all %s slot(s) reached LoginSuccess.", n)
                return True
            if (
                not retry_wave_done
                and bool(getattr(cfg, "HEADLESS_PARALLEL_RETRY_FAILED_SLOTS", True))
                and (time.monotonic() - parallel_t0) >= retry_after
            ):
                retry_wave_done = True
                to_retry: list[tuple[Any, dict[str, object]]] = []
                for st, rw in pairs:
                    if st.login_success_event.is_set():
                        continue
                    ht = getattr(st, "headless_thread", None)
                    if ht is not None and ht.is_alive():
                        continue
                    to_retry.append((st, rw))
                if to_retry:
                    retry_states = [st for st, _ in to_retry]
                    w121_n = _count_win121_slots(retry_states)
                    if w121_n >= len(retry_states) and len(retry_states) >= 2:
                        logger.info(
                            "Headless parallel: retry wave for %s slot(s) (labels: %s) — "
                            "each failed slot reported WinError 121 (TCP connect timed out). That is almost "
                            "always firewall/VPN/ISP or Windows choking on many parallel outbound connects, "
                            "not game 'load limits'. Try: allow python.exe in Windows Firewall, another "
                            "network/VPN, BOT_HEADLESS_PARALLEL_LOGIN=false for sequential login, or a much "
                            "larger BOT_HEADLESS_PARALLEL_START_STAGGER_SEC.",
                            len(to_retry),
                            ", ".join(st.label for st, _ in to_retry),
                        )
                    elif w121_n * 2 >= len(retry_states):
                        logger.info(
                            "Headless parallel: retry wave for %s slot(s) (labels: %s) — "
                            "most failures saw WinError 121 (connect timeout). Check firewall/VPN and consider "
                            "sequential headless (BOT_HEADLESS_PARALLEL_LOGIN=false) or higher stagger.",
                            len(to_retry),
                            ", ".join(st.label for st, _ in to_retry),
                        )
                    else:
                        logger.info(
                            "Headless parallel: retry wave for %s slot(s) (labels: %s) — "
                            "first wave did not reach LoginSuccess (mixed causes); try "
                            "BOT_HEADLESS_PARALLEL_START_STAGGER_SEC if only some slots fail.",
                            len(to_retry),
                            ", ".join(st.label for st, _ in to_retry),
                        )
                    for j, (st, rw) in enumerate(to_retry):
                        if retry_stagger > 0 and j > 0:
                            time.sleep(retry_stagger)
                        st.login_success_event.clear()
                        t2 = threading.Thread(
                            target=_retry_one,
                            args=(st, rw),
                            name=f"tfm-headless-retry-{st.label}",
                            daemon=True,
                        )
                        st.headless_thread = t2
                        t2.start()
            time.sleep(poll_sec)
        pending_states = [st for st, _ in pairs if not st.login_success_event.is_set()]
        pending = [st.label for st in pending_states]
        logger.error(
            "Headless parallel login timed out after %.0fs — missing LoginSuccess for slot(s): %s",
            timeout_sec,
            ", ".join(pending) if pending else "(unknown)",
        )
        if pending_states:
            w121_n = _count_win121_slots(pending_states)
            if w121_n >= len(pending_states):
                logger.error(
                    "All failing slots reported WinError 121 — TCP to the game host is not completing from "
                    "this PC (firewall, VPN, routing, or overload from parallel connects). tfm-secrets will not "
                    "fix this; use `python -m bot.upstream_probe <host> <ports>` from the same machine.",
                )
        return False

    base_gap = float(getattr(cfg, "HEADLESS_LOGIN_STAGGER_SEC", 6.0) or 0.0)
    base_gap = max(0.0, base_gap)
    adaptive_gap = base_gap
    win121_extra = float(getattr(cfg, "HEADLESS_STAGGER_WIN121_EXTRA_SEC", 4.0) or 0.0)
    gap_cap = float(getattr(cfg, "HEADLESS_STAGGER_MAX_SEC", 15.0) or 15.0)
    max_consec = int(getattr(cfg, "HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES", 0) or 0)

    consec_fail = 0
    for i, (state, row) in enumerate(pairs):
        if max_consec > 0 and consec_fail >= max_consec:
            logger.warning(
                "Stopping headless logins: %s consecutive slot(s) without LoginSuccess "
                "(BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES=%s). "
                "If the startup TCP probe showed 0 ports OK or you see WinError 121, fix firewall/VPN/path "
                "to the game host first (secrets alone will not fix that). Otherwise refresh TFM_SECRETS_* "
                "or increase BOT_HEADLESS_LOGIN_STAGGER_SEC.",
                max_consec,
                max_consec,
            )
            return False
        if i > 0 and adaptive_gap > 0:
            time.sleep(adaptive_gap)
        w121_ev = getattr(state, "headless_seen_upstream_win121", None)
        if w121_ev is not None:
            w121_ev.clear()
        _run_one_slot_headless(state=state, row=row, cfg=cfg, base_secrets=base)
        if w121_ev is not None and w121_ev.is_set() and win121_extra > 0:
            before = adaptive_gap
            adaptive_gap = min(gap_cap, adaptive_gap + win121_extra)
            if adaptive_gap > before:
                logger.info(
                    "WinError 121 on slot %s: increasing inter-slot pause %.2fs → %.2fs "
                    "(cap %.2fs; BOT_HEADLESS_STAGGER_WIN121_EXTRA_SEC / BOT_HEADLESS_STAGGER_MAX_SEC).",
                    state.label,
                    before,
                    adaptive_gap,
                    gap_cap,
                )
        if state.login_success_event.is_set():
            consec_fail = 0
        else:
            consec_fail += 1
    return True
