"""
One ``caseus.Client`` per slot connecting to the local BanBotProxy (no Flash).

Secrets (no ``tfm-secrets.json`` in this flow):

1. Optional ``HEADLESS_SECRETS_DUMPER`` — run a subprocess that prints tfm-secrets JSON on stdout;
   parsed in memory only.
2. Variables from the process environment, after loading repo-root ``.env`` (``TFM_SECRETS_*``).
3. ``BOT_HEADLESS_SECRETS_INLINE_JSON`` (or ``HEADLESS_SECRETS_INLINE`` on ``cfg`` from ``.env``).

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

from .env_setup import load_dotenv_file, repo_root
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
    """Run a secrets dumper; parse JSON from stdout into ``Secrets``. No files read or written."""
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
        logger.warning(
            "Incomplete %s* / .env: missing %s (filled: %s). Fill every field from .env.example.",
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


def load_secrets_base(cfg: object) -> Secrets:
    """Load server crypto: optional dumper (stdout JSON), then ``.env`` / env vars, then inline dict."""
    load_dotenv_file(_resolved_dotenv_path(cfg))

    dumper = getattr(cfg, "HEADLESS_SECRETS_DUMPER", None)
    dumper_nonempty = False
    if isinstance(dumper, (list, tuple)):
        dumper_nonempty = any(str(x).strip() for x in dumper)
    elif dumper is not None:
        dumper_nonempty = bool(str(dumper).strip())

    if dumper_nonempty:
        argv = _dumper_argv_from_config(dumper)
        if argv:
            sec = _try_secrets_from_dumper_argv(argv, cfg)
            if sec is not None:
                logger.info(
                    "Secrets from HEADLESS_SECRETS_DUMPER (subprocess JSON on stdout; no secret files read)."
                )
                return sec
        bindir = Path(sys.executable).resolve().parent
        logger.warning(
            "HEADLESS_SECRETS_DUMPER is set but did not yield secrets (missing binary, failure, or timeout). "
            "Checked PATH and %s for .exe/.cmd. Falling back to .env / BOT_HEADLESS_SECRETS_INLINE_JSON.",
            bindir,
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

    dot = _resolved_dotenv_path(cfg)
    pfx = str(getattr(cfg, "HEADLESS_SECRETS_ENV_PREFIX", "TFM_SECRETS_") or "TFM_SECRETS_").strip()
    if not pfx.endswith("_"):
        pfx = f"{pfx}_"
    nonempty = sorted(k for k in os.environ if k.startswith(pfx) and str(os.environ.get(k, "")).strip())
    logger.error(
        "Expected secrets file: %s (exists=%s). Non-empty %s* keys after load: %s",
        dot,
        dot.is_file(),
        pfx,
        nonempty or "(none — fill .env or use BOT_HEADLESS_SECRETS_INLINE_JSON / BOT_HEADLESS_SECRETS_DUMPER)",
    )
    msg = (
        "Headless needs secrets: ensure .env exists in the repo root (created from .env.example on first run), "
        "set TFM_SECRETS_SERVER_ADDRESS, TFM_SECRETS_SERVER_PORTS, TFM_SECRETS_GAME_VERSION, "
        "TFM_SECRETS_CONNECTION_TOKEN, TFM_SECRETS_AUTH_KEY, TFM_SECRETS_PACKET_KEY_SOURCES, "
        "TFM_SECRETS_CLIENT_VERIFICATION_TEMPLATE, or set BOT_HEADLESS_SECRETS_INLINE_JSON / "
        "BOT_HEADLESS_SECRETS_DUMPER. Pull latest for .env.example if missing."
    )
    logger.error(msg)
    raise SystemExit(msg)


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
            if not (same_a and same_p):
                msg = (
                    f"UPSTREAM_SERVER_* ({chosen_a!r}, {chosen_p}) differs from this run's secrets "
                    f"dump ({dump_a!r}, {dump_p_i}). A wrong shard often accepts TCP then closes with "
                    "no handshake reply — align or clear UPSTREAM_SERVER_* or set "
                    "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY = true."
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
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._login_success_event = login_success_event
        self._sysinfo_after_verification_pending = False

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


async def _run_one_client(
    *,
    secrets: Secrets,
    username: str,
    password: str,
    start_room: str,
    login_success_event: threading.Event,
    connect_to_satellite: bool,
) -> None:
    pw_hash = shakikoo(password.strip())
    client = HeadlessProxyClient(
        secrets=secrets,
        username=username,
        password_hash=pw_hash,
        start_room=start_room,
        login_success_event=login_success_event,
        connect_to_satellite=connect_to_satellite,
    )
    await client.start()


def _run_one_slot_headless(
    *,
    state: Any,
    row: dict[str, object],
    cfg: object,
    base_secrets: Secrets,
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
    try:
        asyncio.run(
            _run_one_client(
                secrets=secrets,
                username=username,
                password=password,
                start_room=start_room,
                login_success_event=state.login_success_event,
                connect_to_satellite=bool(getattr(cfg, "HEADLESS_CONNECT_TO_SATELLITE", True)),
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


def start_headless_client_threads(
    states: list[Any],
    raw_accounts: list[dict[str, object]],
    cfg: object,
    *,
    base_secrets: Secrets | None = None,
) -> None:
    """
    Run headless TCP login **one slot at a time** (main thread): each ``HeadlessProxyClient`` runs
    to completion before the next starts. This avoids hammering the game server with parallel
    handshakes from one host.

    ``HEADLESS_LOGIN_STAGGER_SEC`` (default 2.5) is the pause **between** finishing one slot and
    starting the next (not used for overlapping parallel starts). Increase if you see WinError 121
    on later slots (rate limiting / connect timeouts).
    """
    base = base_secrets if base_secrets is not None else load_secrets_base(cfg)
    gap = float(getattr(cfg, "HEADLESS_LOGIN_STAGGER_SEC", 2.5) or 0.0)
    gap = max(0.0, gap)

    pairs = list(zip(states, raw_accounts))
    for i, (state, row) in enumerate(pairs):
        if i > 0 and gap > 0:
            time.sleep(gap)
        _run_one_slot_headless(state=state, row=row, cfg=cfg, base_secrets=base)
