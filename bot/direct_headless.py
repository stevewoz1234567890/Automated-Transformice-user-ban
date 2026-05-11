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
      - Graceful satellite connection handling with retry
      - Room list and player list collection
      - Room join confirmation via JoinedRoomPacket
    """

    SATELLITE_CONNECT_TIMEOUT = 8.0
    SATELLITE_CONNECT_RETRIES = 2
    SATELLITE_RETRY_DELAY = 1.5

    def __init__(
        self,
        *,
        login_success_event: threading.Event | None = None,
        label: str = "?",
        **kwargs: Any,
    ) -> None:
        kwargs["connect_to_satellite"] = False
        super().__init__(**kwargs)
        self.register_packet_listener(
            self._on_change_satellite_server,
            clientbound.ChangeSatelliteServerPacket,
        )
        self.register_packet_listener(
            self._on_room_list,
            clientbound.RoomListPacket,
        )
        self.register_packet_listener(
            self._on_set_player_list,
            clientbound.SetPlayerListPacket,
        )
        self.register_packet_listener(
            self._on_update_player_list,
            clientbound.UpdatePlayerListPacket,
        )
        self.register_packet_listener(
            self._on_joined_room,
            clientbound.JoinedRoomPacket,
        )
        self._login_success_event = login_success_event
        self._login_failed_event: threading.Event | None = None
        self._label = label
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logged_in = False
        self._login_failed = False
        self._login_error: str | None = None
        self._own_username: str | None = None
        self._satellite_ready = asyncio.Event()
        self._satellite_failed = False
        self._joined_room_event = asyncio.Event()
        self._joined_room_name: str | None = None
        self.current_room: str | None = None
        self.known_rooms: dict[str, int] = {}
        self._room_list_ready = asyncio.Event()
        self.known_players: dict[str, int] = {}
        self._player_list_ready = asyncio.Event()
        self._packets_sent: int = 0
        self._packets_recv: int = 0

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
        self._login_failed = True
        self._login_error = f"AccountError error_code={ec}"
        logger.error("Slot %s: AccountError error_code=%s", self._label, ec)
        if self._login_failed_event is not None:
            self._login_failed_event.set()

    async def _on_joined_room(self, server, packet):
        """Server confirms we entered a room."""
        raw = getattr(packet, "raw_name", "") or ""
        official = getattr(packet, "official", False)
        self.current_room = raw
        self._joined_room_name = raw
        self._joined_room_event.set()
        logger.info(
            "Slot %s: SERVER CONFIRMED room entry → %r (official=%s)",
            self._label, raw, official,
        )

    async def _on_room_list(self, server, packet):
        """Collect rooms from every ``RoomListPacket`` the server sends."""
        for r in (getattr(packet, "rooms", None) or []):
            name = (getattr(r, "name", "") or "").strip()
            if not name:
                continue
            try:
                num = int(getattr(r, "num_players", 0) or 0)
            except (TypeError, ValueError):
                num = 0
            self.known_rooms[name] = num
        logger.info("Slot %s: room list received (%d rooms)", self._label, len(self.known_rooms))
        self._room_list_ready.set()

    async def _on_set_player_list(self, server, packet):
        """Full player list sent when we join a room."""
        self.known_players.clear()
        for p in (getattr(packet, "players", None) or []):
            username = (getattr(p, "username", "") or "").strip()
            if username:
                self.known_players[username] = getattr(p, "session_id", 0) or 0
        own_in_list = self._own_username in self.known_players if self._own_username else False
        logger.info(
            "Slot %s: player list set (%d players) room=%r self_present=%s",
            self._label, len(self.known_players), self.current_room, own_in_list,
        )
        self._player_list_ready.set()

    async def _on_update_player_list(self, server, packet):
        """Single player joining the room after us."""
        p = getattr(packet, "player", None)
        if p is None:
            return
        username = (getattr(p, "username", "") or "").strip()
        if username:
            self.known_players[username] = getattr(p, "session_id", 0) or 0
            logger.debug(
                "Slot %s: player joined room: %s (room=%r)",
                self._label, username, self.current_room,
            )

    async def request_room_list(self, game_mode_int: int = 1) -> bool:
        """Ask the server for the room list. Response arrives via ``_on_room_list``."""
        from caseus import enums as _enums
        try:
            gm = _enums.GameMode(game_mode_int)
        except (ValueError, KeyError):
            gm = _enums.GameMode.NONE
        try:
            conn = self.main
            await conn.write_packet_instance(
                serverbound.RoomListPacket(game_mode=gm)
            )
            logger.info("Slot %s: RoomListPacket sent (game_mode=%s)", self._label, game_mode_int)
            return True
        except Exception as exc:
            logger.error("Slot %s: request_room_list failed: %s", self._label, exc)
            return False

    async def reset_room_list(self) -> bool:
        self.known_rooms.clear()
        self._room_list_ready.clear()
        return True

    async def wait_room_list(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._room_list_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_player_list(self, timeout: float = 12.0) -> bool:
        try:
            await asyncio.wait_for(self._player_list_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def open_streams(self, address, ports):
        """Override to add timeout and retry logic for connections."""
        last_exc = None
        for attempt in range(1, self.SATELLITE_CONNECT_RETRIES + 1):
            for port in ports:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(address, port),
                        timeout=self.SATELLITE_CONNECT_TIMEOUT,
                    )
                    logger.info(
                        "Slot %s: connected to %s:%d (attempt %d)",
                        self._label, address, port, attempt,
                    )
                    return reader, writer
                except (OSError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    logger.debug(
                        "Slot %s: %s:%d attempt %d failed: %s",
                        self._label, address, port, attempt, exc,
                    )
            if attempt < self.SATELLITE_CONNECT_RETRIES:
                await asyncio.sleep(self.SATELLITE_RETRY_DELAY)
        raise ValueError(
            f"Unable to connect to address '{address}' on ports {list(ports)}: {last_exc}"
        )

    async def _on_change_satellite_server(self, server, packet):
        """Override caseus default to add graceful error handling and readiness signaling."""
        if packet.should_ignore:
            return

        addr = getattr(packet, "address", None)
        ports = getattr(packet, "ports", None)
        logger.info(
            "Slot %s: satellite redirect → %s:%s (current_room=%r)",
            self._label, addr, ports, self.current_room,
        )
        self._satellite_ready.clear()
        self._satellite_failed = False

        if self.satellite is not self.main:
            logger.debug("Slot %s: closing old satellite connection", self._label)
            self.satellite.close()
            await self.satellite.wait_closed()

        try:
            reader, writer = await self.open_streams(packet.address, packet.ports)
        except (ValueError, OSError) as exc:
            logger.error(
                "Slot %s: satellite connection FAILED (%s:%s): %s  "
                "— /ban will use main connection as fallback. "
                "Ensure Proxifier routes python.exe to this IP.",
                self._label, addr, ports, exc,
            )
            self._satellite_failed = True
            self._satellite_ready.set()
            return

        self.satellite = self.Connection(self, reader=reader, writer=writer)
        await self.satellite.write_packet(
            serverbound.SatelliteDelayedIdentificationPacket,
            timestamp=packet.timestamp,
            global_id=packet.global_id,
            auth_id=packet.auth_id,
        )
        logger.info(
            "Slot %s: satellite ready (%s) — room=%r satellite≠main=%s",
            self._label, addr, self.current_room, self.satellite is not self.main,
        )
        self._satellite_ready.set()

    async def send_command(self, cmd: str) -> bool:
        """Send a ``/command`` to the server (used for /ban, /room, etc.)."""
        try:
            use_satellite = self.satellite is not self.main
            conn = self.satellite if use_satellite else self.main
            logger.info(
                "Slot %s: send_command(%r) via %s (room=%r, satellite_failed=%s)",
                self._label, cmd, "SATELLITE" if use_satellite else "MAIN",
                self.current_room, self._satellite_failed,
            )
            await conn.write_packet(serverbound.CommandPacket, command=cmd)
            self._packets_sent += 1
            return True
        except Exception as exc:
            logger.error("Slot %s: send_command(%r) FAILED: %s", self._label, cmd, exc)
            return False

    async def join_room_async(self, room_name: str, *, community: str = "") -> bool:
        """Send JoinRoomPacket via the MAIN connection (room changes go through main)."""
        target = room_name.strip()
        already_here = self.current_room is not None and self.current_room == target
        if already_here:
            logger.info(
                "Slot %s: already in room %r — skipping JoinRoomPacket (keeping player list)",
                self._label, target,
            )
            self._joined_room_event.set()
            return True

        self._player_list_ready.clear()
        self._joined_room_event.clear()
        self._joined_room_name = None
        self.known_players.clear()
        try:
            conn = self.main
            main_alive = conn is not None and not getattr(conn, '_closing', False)
            logger.info(
                "Slot %s: sending JoinRoomPacket for %r via MAIN (main_alive=%s, "
                "current_room=%r, satellite_is_main=%s)",
                self._label, target, main_alive,
                self.current_room, self.satellite is self.main,
            )
            await conn.write_packet_instance(
                serverbound.JoinRoomPacket(
                    community=community,
                    name=target,
                    password="",
                    auto=False,
                    customization=None,
                )
            )
            self._packets_sent += 1
            logger.info("Slot %s: JoinRoomPacket sent for %r (total_sent=%d)", self._label, target, self._packets_sent)
            return True
        except Exception as exc:
            logger.error("Slot %s: join_room(%r) FAILED: %s", self._label, target, exc)
            return False

    async def wait_joined_room(self, timeout: float = 15.0) -> bool:
        """Wait for the server to confirm room entry via JoinedRoomPacket."""
        try:
            await asyncio.wait_for(self._joined_room_event.wait(), timeout=timeout)
            logger.info(
                "Slot %s: room join CONFIRMED by server → %r",
                self._label, self._joined_room_name,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Slot %s: JoinedRoomPacket NOT received within %.1fs "
                "(server did not confirm room entry — join may have failed)",
                self._label, timeout,
            )
            return False

    async def wait_satellite_ready(self, timeout: float = 15.0) -> bool:
        """Wait for the satellite connection to be established after a room join."""
        try:
            await asyncio.wait_for(self._satellite_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return self._satellite_failed is False and self.satellite is not self.main

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
        self._login_failed_event = threading.Event()

        self._client = BanBotDirectClient(
            secrets=secrets,
            username=username,
            password_hash=shakikoo(password.strip()),
            start_room=start_room,
            login_success_event=self._login_success_event,
            label=label,
        )
        self._client._login_failed_event = self._login_failed_event
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def logged_in(self) -> bool:
        return self._login_success_event.is_set()

    @property
    def login_failed(self) -> bool:
        """True if the slot permanently failed (AccountError or connection died before login)."""
        if self._login_failed_event.is_set():
            return True
        if self._thread is not None and not self._thread.is_alive() and not self.logged_in:
            return True
        return False

    @property
    def login_error(self) -> str | None:
        return self._client._login_error

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
            self._client._login_failed = True
            self._client._login_error = f"crashed: {exc}"
            logger.error("Slot %s: direct headless client crashed: %s", self.label, exc, exc_info=True)
        finally:
            loop.close()
            if not self.logged_in:
                self._login_failed_event.set()
            logger.warning("Slot %s: direct headless connection ended", self.label)

    def send_ban(self, target: str, *, timeout: float = 5.0) -> bool:
        cmd = f"ban {target}"
        return self._run_async(self._client.send_command(cmd), timeout=timeout)

    def join_room(self, room_name: str, *, timeout: float = 5.0) -> bool:
        return self._run_async(self._client.join_room_async(room_name), timeout=timeout)

    def wait_satellite(self, *, timeout: float = 10.0) -> bool:
        return self._run_async(self._client.wait_satellite_ready(timeout=timeout), timeout=timeout + 2)

    def wait_joined_room(self, *, timeout: float = 15.0) -> bool:
        """Block until the server confirms room entry (JoinedRoomPacket)."""
        return self._run_async(self._client.wait_joined_room(timeout=timeout), timeout=timeout + 2)

    @property
    def current_room(self) -> str | None:
        return self._client.current_room

    def fetch_room_list(self, game_modes: list[int] | None = None, *, timeout: float = 10.0) -> dict[str, int]:
        """Request room list(s) from the server and return ``{name: player_count}``."""
        if game_modes is None:
            game_modes = [1, 2, 8, 9, 18]
        self._run_async(self._client.reset_room_list(), timeout=3.0)
        for gm in game_modes:
            self._run_async(self._client.request_room_list(gm), timeout=5.0)
            time.sleep(0.3)
        self._run_async(self._client.wait_room_list(timeout=timeout), timeout=timeout + 2)
        time.sleep(1.5)
        return dict(self._client.known_rooms)

    def wait_player_list(self, *, timeout: float = 12.0) -> bool:
        return self._run_async(self._client.wait_player_list(timeout=timeout), timeout=timeout + 2)

    def get_known_players(self) -> dict[str, int]:
        return dict(self._client.known_players)

    def _run_async(self, coro, *, timeout: float = 5.0):
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
