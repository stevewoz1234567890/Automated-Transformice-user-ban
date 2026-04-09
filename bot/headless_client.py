"""
Option B: one ``caseus.Client`` per slot connecting to the local BanBotProxy (no Flash).

Requires ``Secrets`` (game keys) from ``HEADLESS_SECRETS_JSON`` or ``HEADLESS_SECRETS_DUMPER`` in
``bot/config.py``. Point ``Secrets`` at ``127.0.0.1:<main proxy_port>`` per slot — done here via
``Secrets.copy(server_address=..., server_ports=(...))``.

Keep ``PACKET_AUTO_LOGIN = False`` so the proxy does not inject a second ``LoginPacket``.
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
    """``caseus.Client`` with the same ``LoginPacket.loader_url`` as Flash and login success signaling."""

    def __init__(
        self,
        *,
        loader_url: str,
        login_success_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._loader_url = loader_url
        self._login_success_event = login_success_event

    async def login(self) -> None:
        if self.secrets.auth_key is not None:
            ciphered_auth_token = self.auth_token ^ self.secrets.auth_key
        else:
            ciphered_auth_token = None

        await self.main.write_packet(
            serverbound.LoginPacket,
            username=self.username,
            password_hash=self.password_hash,
            loader_url=self._loader_url,
            start_room=self.start_room,
            ciphered_auth_token=ciphered_auth_token,
            unk_short_6=18,
        )

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
    loader_url: str,
    login_success_event: threading.Event,
) -> None:
    pw_hash = shakikoo(password.strip())
    client = HeadlessProxyClient(
        secrets=secrets,
        username=username,
        password_hash=pw_hash,
        start_room=start_room,
        loader_url=loader_url,
        login_success_event=login_success_event,
        connect_to_satellite=True,
    )
    await client.start()


def _slot_thread_main(
    *,
    state: Any,
    row: dict[str, object],
    cfg: object,
    base_secrets: Secrets,
    stagger_sec: float,
) -> None:
    if stagger_sec > 0:
        time.sleep(stagger_sec)
    label = state.label
    main_port = state.port
    connect_host = state.proxy_bind_host if state.proxy_bind_host else "127.0.0.1"
    secrets = base_secrets.copy(
        server_address=connect_host,
        server_ports=(main_port,),
    )

    # Same file:///…swf?… as Flash / PACKET_AUTO_LOGIN (set on SlotState in ban_cli).
    loader_url = (getattr(state, "packet_loader_url", None) or "").strip() or Client.LOADER_URL

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
                loader_url=loader_url,
                login_success_event=state.login_success_event,
            )
        )
    except AccountError as e:
        logger.error("Slot %s: AccountError code=%s", label, e.error_code)
    except OSError as e:
        logger.error("Slot %s: connection error: %s", label, e)
    except Exception:
        logger.exception("Slot %s: headless client failed", label)


def start_headless_client_threads(
    states: list[Any],
    raw_accounts: list[dict[str, object]],
    cfg: object,
) -> None:
    """Spawn one daemon thread per slot; each runs ``asyncio.run(HeadlessProxyClient.start())``."""
    base = load_secrets_base(cfg)
    stagger = float(getattr(cfg, "HEADLESS_LOGIN_STAGGER_SEC", 0.5) or 0.0)
    stagger = max(0.0, stagger)

    for i, (state, row) in enumerate(zip(states, raw_accounts)):
        delay = stagger * i
        t = threading.Thread(
            target=_slot_thread_main,
            kwargs={
                "state": state,
                "row": row,
                "cfg": cfg,
                "base_secrets": base,
                "stagger_sec": delay,
            },
            name=f"headless-{state.label}",
            daemon=True,
        )
        t.start()
