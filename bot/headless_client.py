"""
One ``caseus.Client`` per slot connecting to the local BanBotProxy (no Flash).

Requires ``Secrets`` (game keys) from ``HEADLESS_SECRETS_JSON`` or ``HEADLESS_SECRETS_DUMPER`` in
``bot/config.py``. Each client uses ``Secrets.copy(server_address=..., server_ports=(...))`` to
target ``127.0.0.1:<proxy_port>`` while ``BanBotProxy`` is given the real ``server_address`` /
``server_ports`` from the same JSON so the proxy connects upstream immediately.

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
    """Load server crypto parameters (not per-slot); address/ports are overridden per slot."""
    json_path = getattr(cfg, "HEADLESS_SECRETS_JSON", None)
    dumper = getattr(cfg, "HEADLESS_SECRETS_DUMPER", None)

    if json_path:
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

    if dumper:
        cmd = str(dumper).strip()
        if not cmd:
            msg = "HEADLESS_SECRETS_DUMPER is empty"
            logger.error(msg)
            raise SystemExit(msg)
        return Secrets.load_from_dumper(cmd)

    msg = (
        "Headless mode requires HEADLESS_SECRETS_JSON (path to JSON from tfm-secrets) "
        "or HEADLESS_SECRETS_DUMPER (e.g. 'tfm-secrets') in bot/config.py"
    )
    logger.error(msg)
    raise SystemExit(msg)


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
                "Slot %s: [login] headless TCP session ended before LoginSuccess (disconnect or server closed)",
                label,
            )
            if bool(getattr(cfg, "PROXY_LOGIN_DIAGNOSTICS", True)):
                logger.info(
                    "Slot %s: [login][diag] headless client exited without LoginSuccess — check same slot’s "
                    "[login][diag] lines (upstream EOF, srv→proxy packet #, HandshakeResponse timing)",
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

    ``HEADLESS_LOGIN_STAGGER_SEC`` (default 0.5) is the pause **between** finishing one slot and
    starting the next (not used for overlapping parallel starts).
    """
    base = base_secrets if base_secrets is not None else load_secrets_base(cfg)
    gap = float(getattr(cfg, "HEADLESS_LOGIN_STAGGER_SEC", 0.5) or 0.0)
    gap = max(0.0, gap)

    pairs = list(zip(states, raw_accounts))
    for i, (state, row) in enumerate(pairs):
        if i > 0 and gap > 0:
            time.sleep(gap)
        _run_one_slot_headless(state=state, row=row, cfg=cfg, base_secrets=base)
