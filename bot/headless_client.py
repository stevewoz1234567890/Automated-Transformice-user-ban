"""
One ``caseus.Client`` per slot connecting to the local BanBotProxy (no Flash).

Requires ``Secrets`` from ``HEADLESS_SECRETS_DUMPER`` (e.g. ``tfm-secrets``) each run, or optionally
``HEADLESS_SECRETS_JSON`` for a static file. Each client uses ``Secrets.copy(server_address=..., server_ports=(...))``
to target ``127.0.0.1:<proxy_port>`` while ``BanBotProxy`` is given the real upstream from config
or the dump so the proxy connects immediately.

The proxy injects ``LoginPacket`` after ``SystemInformationPacket``; this client must **not** send
its own ``LoginPacket`` (see ``HeadlessProxyClient.login`` no-op).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pak
from caseus import Secrets
from caseus.clients.client import AccountError, Client
from caseus.packets import clientbound, serverbound
from caseus.util.crypto import shakikoo

logger = logging.getLogger(__name__)


def load_secrets_base(cfg: object) -> Secrets:
    """Load server crypto parameters (not per-slot). Prefer a live dumper each run over a JSON file."""
    json_path = getattr(cfg, "HEADLESS_SECRETS_JSON", None)
    dumper = getattr(cfg, "HEADLESS_SECRETS_DUMPER", None)

    if dumper and str(dumper).strip():
        return Secrets.load_from_dumper(str(dumper).strip())

    if json_path and str(json_path).strip():
        p = Path(str(json_path).strip()).expanduser()
        if not p.is_file():
            root = (
                Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent
            )
            alt = (root / p).resolve()
            if alt.is_file():
                p = alt
        if not p.is_file():
            msg = f"HEADLESS_SECRETS_JSON not found: {p}"
            logger.error(msg)
            raise SystemExit(msg)
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return Secrets(**data)

    msg = (
        "Headless mode requires HEADLESS_SECRETS_DUMPER (e.g. 'tfm-secrets' on PATH) in bot/config.py "
        "to refresh secrets each run, or set HEADLESS_SECRETS_JSON to a tfm-secrets JSON file."
    )
    logger.error(msg)
    raise SystemExit(msg)


def resolve_headless_upstream(cfg: object, base_secrets: Secrets) -> tuple[str, tuple[int, ...]]:
    """Pick upstream host/ports; warn or exit if config overrides disagree with this run's dump."""
    ua = getattr(cfg, "UPSTREAM_SERVER_ADDRESS", None)
    up = getattr(cfg, "UPSTREAM_SERVER_PORTS", None)
    dump_a = getattr(base_secrets, "server_address", None)
    dump_p = getattr(base_secrets, "server_ports", None)

    ua_s = str(ua).strip() if ua is not None else ""
    if ua_s and up is not None and len(tuple(up)) > 0:
        chosen_a = ua_s
        chosen_p = tuple(int(x) for x in up)
        if dump_a and dump_p:
            dump_p_i = tuple(int(x) for x in dump_p)
            same_a = chosen_a == str(dump_a).strip()
            same_p = chosen_p == dump_p_i
            if not (same_a and same_p):
                msg = (
                    f"UPSTREAM_SERVER_* ({chosen_a!r}, {chosen_p}) differs from this run's secrets "
                    f"dump ({dump_a!r}, {dump_p_i}). A wrong shard often accepts TCP then closes with "
                    "no handshake reply — align or clear UPSTREAM_SERVER_* to use the dump."
                )
                if bool(getattr(cfg, "UPSTREAM_STRICT_MATCH_SECRETS_DUMP", False)):
                    logger.error(msg)
                    raise SystemExit(msg)
                logger.warning(msg)
        return chosen_a, chosen_p

    if not dump_a or not dump_p:
        logger.error(
            "Headless needs server_address and server_ports from the secrets dump, or set "
            "UPSTREAM_SERVER_ADDRESS and UPSTREAM_SERVER_PORTS in config."
        )
        raise SystemExit(1)
    return str(dump_a).strip(), tuple(int(x) for x in dump_p)


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
        logger.error("Slot %s: headless login needs username and password in config", label)
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
