"""
Direct headless Transformice client that connects straight to the game server
(bypassing the local proxy).  Used by ``--headless-keepalive`` to avoid the
Flash crash issue — no Flash Player needed at all.

Each ``DirectHeadlessSlot`` holds a running caseus.Client in a background thread.
The ban_cli can call ``send_ban(target)`` or ``join_room(room)`` on the slot
from the main thread.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pak
import caseus
from caseus.secrets import Secrets
from caseus.util.crypto import shakikoo
from caseus.packets import serverbound, clientbound

logger = logging.getLogger(__name__)


class BanBotDirectClient(caseus.Client):
    """
    caseus.Client with:
      - No proxy needed (connects directly to the game server)
      - Stays connected after login (keepalive)
      - Exposes ``send_command(cmd)`` for /ban, /room, etc.
    """

    def __init__(
        self,
        *,
        login_success_event: threading.Event | None = None,
        label: str = "?",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._login_success_event = login_success_event
        self._label = label
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logged_in = False
        self._own_username: str | None = None

    @pak.packet_listener(clientbound.LoginSuccessPacket)
    async def _on_login_success_direct(self, server, packet):
        self._logged_in = True
        self._own_username = getattr(packet, "username", None) or self.username
        logger.info(
            "Slot %s: DIRECT LOGIN SUCCESS as %s",
            self._label,
            self._own_username,
        )
        if self._login_success_event is not None:
            self._login_success_event.set()

    @pak.packet_listener(clientbound.AccountErrorPacket)
    async def _on_account_error(self, server, packet):
        ec = getattr(packet, "error_code", None)
        logger.error("Slot %s: AccountError error_code=%s", self._label, ec)

    async def send_command(self, cmd: str) -> bool:
        """Send a ``/command`` to the server (used for /ban, /room, etc.)."""
        try:
            conn = self.satellite if self.satellite is not self.main else self.main
            await conn.write_packet(serverbound.CommandPacket, command=cmd)
            return True
        except Exception as exc:
            logger.error("Slot %s: send_command(%r) failed: %s", self._label, cmd, exc)
            return False

    async def join_room_async(self, room_name: str, *, community: int = 0) -> bool:
        try:
            conn = self.satellite if self.satellite is not self.main else self.main
            await conn.write_packet_instance(
                serverbound.JoinRoomPacket(
                    community=caseus.enums.Community(community),
                    name=room_name.strip(),
                    password="",
                    auto=False,
                    customization=None,
                )
            )
            logger.info("Slot %s: JoinRoomPacket sent for %r", self._label, room_name)
            return True
        except Exception as exc:
            logger.error("Slot %s: join_room(%r) failed: %s", self._label, room_name, exc)
            return False

    async def run_forever(self) -> None:
        """Connect, login, and stay connected — handling pings automatically."""
        self._loop = asyncio.get_running_loop()
        await self.start()


class DirectHeadlessSlot:
    """
    Wraps a ``BanBotDirectClient`` in a background thread.
    Provides synchronous ``send_ban(target)`` / ``join_room(room)`` callables.
    """

    def __init__(
        self,
        *,
        secrets: Secrets,
        username: str,
        password: str,
        label: str,
        start_room: str = "",
        login_success_event: threading.Event | None = None,
    ) -> None:
        self.label = label
        self.username = username
        self._login_success_event = login_success_event or threading.Event()

        self._client = BanBotDirectClient(
            secrets=secrets,
            username=username,
            password_hash=shakikoo(password.strip()),
            start_room=start_room,
            login_success_event=self._login_success_event,
            label=label,
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def logged_in(self) -> bool:
        return self._login_success_event.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_in_thread,
            name=f"direct-headless-{self.label}",
            daemon=True,
        )
        self._thread.start()

    def _run_in_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._client.run_forever())
        except Exception as exc:
            logger.error("Slot %s: direct headless client crashed: %s", self.label, exc, exc_info=True)
        finally:
            loop.close()
            logger.warning("Slot %s: direct headless connection ended", self.label)

    def send_ban(self, target: str, *, timeout: float = 5.0) -> bool:
        cmd = f"ban {target}"
        return self._run_async(self._client.send_command(cmd), timeout=timeout)

    def join_room(self, room_name: str, *, timeout: float = 5.0) -> bool:
        return self._run_async(self._client.join_room_async(room_name), timeout=timeout)

    def _run_async(self, coro, *, timeout: float = 5.0) -> bool:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error("Slot %s: event loop not ready", self.label)
            return False
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:
            logger.error("Slot %s: async call failed: %s", self.label, exc)
            return False


def start_direct_headless_slots(
    raw_accounts: list[dict[str, Any]],
    secrets: Secrets,
    *,
    stagger_sec: float = 2.0,
    start_room: str = "",
) -> list[DirectHeadlessSlot]:
    """
    Start one ``DirectHeadlessSlot`` per account, connecting directly to the
    game server without any local proxy.
    """
    slots: list[DirectHeadlessSlot] = []
    for i, row in enumerate(raw_accounts):
        label = str(row.get("label", i + 1))
        username = str(row.get("username", "")).strip()
        password = str(row.get("password", ""))
        if not username or not password.strip():
            logger.error("Slot %s: needs username and password in BOT_ACCOUNTS_JSON", label)
            continue

        login_event = threading.Event()
        slot = DirectHeadlessSlot(
            secrets=secrets,
            username=username,
            password=password,
            label=label,
            start_room=start_room,
            login_success_event=login_event,
        )
        slot.start()
        logger.info("Slot %s: direct headless started → %s:%s",
                     label, secrets.server_address, secrets.server_ports)
        slots.append(slot)

        if stagger_sec > 0 and i < len(raw_accounts) - 1:
            time.sleep(stagger_sec)

    return slots
