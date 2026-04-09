"""
Local Transformice proxy for a single game client: inject /room and /ban via CommandPacket.

Same architecture as stevewoz1234567890/transformice-bot (caseus.Proxy + tfm-proxy-loader).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

import pak
from caseus import Proxy
from caseus.packets import Packet, ServerboundPacket, clientbound, serverbound
from caseus.util.crypto import shakikoo

logger = logging.getLogger(__name__)

_print_lock = threading.Lock()


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def project_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def normalize_nickname_tag(nickname: str) -> str:
    n = nickname.strip()
    if "#" not in n:
        return f"{n}#0000"
    return n


def _trunc(text: object | None, max_len: int = 140) -> str:
    if text is None:
        return ""
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _password_field_meta(raw: object | None) -> str:
    """Length-only description; never log password or hash bytes."""
    if raw is None:
        return "None"
    if raw == "":
        return "empty"
    try:
        ln = len(raw)
    except TypeError:
        return "<?>"
    return f"len={ln} <redacted>"


# Observed in the wild + caseus test server; not official Atelier801 API docs.
_ACCOUNT_ERROR_HINTS: dict[int, str] = {
    2: "login rejected (wrong password, unknown email, or account not usable as sent)",
}


def _account_error_hint(code: int | None) -> str:
    if code is None:
        return ""
    return _ACCOUNT_ERROR_HINTS.get(
        int(code),
        "no local mapping — compare with in-game message / community",
    )


class BanBotProxy(Proxy):
    """One proxy port ↔ one game instance; sends slash-commands as CommandPacket (no leading /)."""

    def __init__(
        self,
        *,
        slot_label: str = "",
        login_success_event: threading.Event | None = None,
        verbose_login_flow: bool = False,
        log_all_main_packets: bool = False,
        packet_login_username: str = "",
        packet_login_password: str = "",
        packet_login_loader_url: str = "",
        packet_login_delay_sec: float = 0.35,
        packet_login_start_room: str = "",
        **kwargs,
    ):
        # Flash file:// SWF + Socket: use IPv4 literal so the client never targets the public
        # satellite IP from ChangeSatelliteServerPacket (sandbox Error #2048) and avoids
        # localhost → ::1 vs proxy listening on IPv4-only edge cases.
        kwargs.setdefault("expected_address", "127.0.0.1")
        super().__init__(**kwargs)
        self.slot_label = slot_label
        self._login_success_event = login_success_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_username: str | None = None
        self._verbose_login_flow = verbose_login_flow
        self._log_all_main_packets = log_all_main_packets
        self._main_handshake_mono: float | None = None
        self._packet_login_username = (packet_login_username or "").strip()
        self._packet_login_password = packet_login_password or ""
        self._packet_login_loader_url = (packet_login_loader_url or "").strip()
        self._packet_login_delay_sec = float(packet_login_delay_sec)
        self._packet_login_start_room = packet_login_start_room or ""
        self._handshake_auth_token: int | None = None
        self._packet_login_sent = False
        self._packet_login_task: asyncio.Task | None = None
        self.register_packet_listener(self._vl_account_error_cb, clientbound.AccountErrorPacket)
        self.register_packet_listener(self._vl_handshake_sb, serverbound.HandshakePacket)
        self.register_packet_listener(
            self._capture_handshake_auth_token,
            clientbound.HandshakeResponsePacket,
        )
        self.register_packet_listener(
            self._schedule_packet_login_after_sysinfo,
            serverbound.SystemInformationPacket,
        )
        self.register_packet_listener(
            self._gate_duplicate_login_packet,
            serverbound.LoginPacket,
        )
        self.register_packet_listener(self._vl_login_sb, serverbound.LoginPacket)
        if verbose_login_flow:
            self._register_verbose_login_flow_listeners()
        if log_all_main_packets:
            self.register_packet_listener(self._log_all_main_packet, Packet)

    def _during_login_wait(self) -> bool:
        ev = self._login_success_event
        if ev is None:
            return True
        return not ev.is_set()

    def _satellite_client_address(self) -> str:
        a = (getattr(self, "expected_address", None) or "127.0.0.1").strip()
        if a.lower() == "localhost":
            return "127.0.0.1"
        return a

    @pak.packet_listener(clientbound.ChangeSatelliteServerPacket)
    async def _proxy_satellite_server(self, source, packet):
        """Send Flash only 127.0.0.1 + local satellite port (never the public game host)."""
        if packet.should_ignore:
            return self.FORWARD_PACKET

        self._satellite_packets.append((packet, source.destination))

        addr = self._satellite_client_address()
        # Server sends several ports (e.g. 11801-12801-13801-14801). Some clients iterate them.
        # Replicate the local proxy port once per server port so Flash never tries the real host:12801.
        orig_ports = list(getattr(packet, "ports", None) or [])
        if orig_ports:
            proxied_ports = [self.host_satellite_port] * len(orig_ports)
        else:
            proxied_ports = [self.host_satellite_port]
        proxied = packet.copy(
            address=addr,
            ports=proxied_ports,
        )
        logger.info(
            "Slot %s: ChangeSatelliteServer → Flash client: address=%r ports=%s "
            "(server sent %r ports %s; all local — avoids Flash #2048 on public host)",
            self.slot_label,
            addr,
            proxied_ports,
            getattr(packet, "address", None),
            getattr(packet, "ports", None),
        )
        await source.destination.write_packet_instance(proxied)
        return self.DO_NOTHING

    def _register_verbose_login_flow_listeners(self) -> None:
        self.register_packet_listener(self._vl_sysinfo_sb, serverbound.SystemInformationPacket)
        self.register_packet_listener(self._vl_captcha_req_sb, serverbound.CaptchaRequestPacket)
        self.register_packet_listener(self._vl_handshake_cb, clientbound.HandshakeResponsePacket)
        self.register_packet_listener(self._vl_captcha_cb, clientbound.CaptchaPacket)
        self.register_packet_listener(self._vl_change_sat_cb, clientbound.ChangeSatelliteServerPacket)
        self.register_packet_listener(self._vl_server_msg_cb, clientbound.ServerMessagePacket)
        self.register_packet_listener(self._vl_translated_cb, clientbound.TranslatedGeneralMessagePacket)

    async def _capture_handshake_auth_token(self, source, packet):
        dest = getattr(source, "destination", None)
        if dest is not None and getattr(dest, "is_satellite", False):
            return
        self._handshake_auth_token = packet.auth_token

    async def _schedule_packet_login_after_sysinfo(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        if not (self._packet_login_username and self._packet_login_password.strip()):
            logger.warning(
                "Slot %s: username/password missing for this slot (packet login)",
                self.slot_label,
            )
            return
        if not self._packet_login_loader_url:
            logger.warning(
                "Slot %s: packet login needs loader URL (packet_loader_url empty)",
                self.slot_label,
            )
            return

        async def _job() -> None:
            try:
                await asyncio.sleep(max(0.0, self._packet_login_delay_sec))
                if self._packet_login_sent:
                    return
                await self._send_packet_login_upstream()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Slot %s: packet login failed", self.slot_label)

        self._packet_login_task = asyncio.create_task(_job())

    async def _send_packet_login_upstream(self) -> None:
        if self._packet_login_sent or not self.main_clients:
            return
        server_conn = self.main_clients[0].destination
        at = self._handshake_auth_token
        if at is None:
            logger.warning(
                "Slot %s: packet login skipped (no auth_token from HandshakeResponse yet)",
                self.slot_label,
            )
            return
        secrets = server_conn.secrets
        ak = getattr(secrets, "auth_key", None)
        if ak is not None:
            ciphered = int(at) ^ int(ak)
        else:
            ciphered = None
        pw_hash = shakikoo(self._packet_login_password.strip())
        await server_conn.write_packet(
            serverbound.LoginPacket,
            username=self._packet_login_username,
            password_hash=pw_hash,
            loader_url=self._packet_login_loader_url,
            start_room=self._packet_login_start_room,
            ciphered_auth_token=ciphered,
            unk_short_6=18,
        )
        self._packet_login_sent = True
        logger.info("Slot %s: LoginPacket sent upstream by proxy (packet login)", self.slot_label)

    async def _gate_duplicate_login_packet(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        if self._packet_login_sent:
            logger.info(
                "Slot %s: dropping client LoginPacket (proxy packet login already satisfied)",
                self.slot_label,
            )
            return self.DO_NOTHING
        self._packet_login_sent = True

    async def _log_all_main_packet(self, source, packet):
        if not self._log_all_main_packets:
            return
        if getattr(source, "is_satellite", False):
            return
        tn = type(packet).__name__
        if tn in ("KeepAlivePacket", "IPSPingPacket", "PingPacket"):
            return
        direction = "→srv" if isinstance(packet, ServerboundPacket) else "srv→"
        body = str(packet)
        if isinstance(packet, pak.GenericPacket):
            body = f"<generic code={getattr(packet, 'code', None)!r} len={len(body)}>"
        logger.info(
            "Slot %s [MAIN all] %s %s %s",
            self.slot_label,
            direction,
            tn,
            _trunc(body, 320),
        )

    async def _vl_handshake_sb(self, source, packet):
        if not getattr(source, "is_satellite", False) and self._main_handshake_mono is None:
            self._main_handshake_mono = time.monotonic()
        if not self._verbose_login_flow:
            return
        conn = "SAT" if getattr(source, "is_satellite", False) else "MAIN"
        logger.info(
            "Slot %s [%s→srv] HandshakePacket game_version=%s loader_stage_size=%s player_type=%s "
            "browser_info=%r referrer=%s",
            self.slot_label,
            conn,
            getattr(packet, "game_version", None),
            getattr(packet, "loader_stage_size", None),
            _trunc(getattr(packet, "player_type", ""), 40),
            _trunc(getattr(packet, "browser_info", ""), 80),
            getattr(packet, "referrer", None),
        )

    async def _vl_login_sb(self, source, packet):
        if not getattr(source, "is_satellite", False) and self._main_handshake_mono is not None:
            dt = time.monotonic() - self._main_handshake_mono
            logger.info(
                "Slot %s: LoginPacket %.2fs after first MAIN HandshakePacket (auto-login vs manual timing)",
                self.slot_label,
                dt,
            )
        if not self._verbose_login_flow:
            return
        conn = "SAT" if getattr(source, "is_satellite", False) else "MAIN"
        logger.info(
            "Slot %s [%s→srv] LoginPacket username=%r login_method=%s start_room=%r "
            "password_hash=%s loader_url=%r",
            self.slot_label,
            conn,
            getattr(packet, "username", None),
            getattr(packet, "login_method", None),
            getattr(packet, "start_room", None),
            _password_field_meta(getattr(packet, "password_hash", None)),
            _trunc(getattr(packet, "loader_url", ""), 100),
        )

    async def _vl_sysinfo_sb(self, source, packet):
        if not self._verbose_login_flow:
            return
        logger.info(
            "Slot %s [→srv] SystemInformationPacket os=%r flash_version=%r language=%r",
            self.slot_label,
            getattr(packet, "os", None),
            getattr(packet, "flash_version", None),
            getattr(packet, "language", None),
        )

    async def _vl_captcha_req_sb(self, source, packet):
        if not self._verbose_login_flow:
            return
        logger.info("Slot %s [→srv] CaptchaRequestPacket (client asks for captcha challenge)", self.slot_label)

    async def _vl_handshake_cb(self, source, packet):
        if not self._verbose_login_flow:
            return
        logger.info(
            "Slot %s [srv→] HandshakeResponsePacket num_online=%s language=%r country=%r auth_token=%s",
            self.slot_label,
            getattr(packet, "num_online_players", None),
            getattr(packet, "language", None),
            getattr(packet, "country", None),
            getattr(packet, "auth_token", None),
        )

    async def _vl_account_error_cb(self, source, packet):
        ec = getattr(packet, "error_code", None)
        hint = _account_error_hint(ec)
        if self._verbose_login_flow:
            logger.warning(
                "Slot %s [srv→] AccountErrorPacket error_code=%s suggested_username=%r unk_string_3=%r — %s",
                self.slot_label,
                ec,
                getattr(packet, "suggested_username", None),
                _trunc(getattr(packet, "unk_string_3", ""), 80),
                hint,
            )
        else:
            logger.warning(
                "Slot %s [srv→] AccountErrorPacket error_code=%s — %s",
                self.slot_label,
                ec,
                hint,
            )

    async def _vl_captcha_cb(self, source, packet):
        if not self._verbose_login_flow:
            return
        info = getattr(packet, "info", None)
        w = h = typ = None
        if info is not None:
            w = getattr(info, "width", None)
            h = getattr(info, "height", None)
            typ = getattr(info, "type", None)
        logger.warning(
            "Slot %s [srv→] CaptchaPacket type=%s size=%sx%s (image not logged)",
            self.slot_label,
            typ,
            w,
            h,
        )

    async def _vl_change_sat_cb(self, source, packet):
        if not self._verbose_login_flow:
            return
        logger.info(
            "Slot %s [srv→] ChangeSatelliteServerPacket (raw from upstream; Flash gets %r:%s) "
            "address=%r ports=%s should_ignore=%s auth_id=%s",
            self.slot_label,
            self._satellite_client_address(),
            self.host_satellite_port,
            getattr(packet, "address", None),
            getattr(packet, "ports", None),
            getattr(packet, "should_ignore", None),
            getattr(packet, "auth_id", None),
        )

    async def _vl_server_msg_cb(self, source, packet):
        if not self._verbose_login_flow or not self._during_login_wait():
            return
        if getattr(source, "is_satellite", False):
            return
        logger.info(
            "Slot %s [srv→] ServerMessagePacket channel=%s template=%r args=%s",
            self.slot_label,
            getattr(packet, "general_channel", None),
            _trunc(getattr(packet, "template", ""), 200),
            getattr(packet, "template_args", None),
        )

    async def _vl_translated_cb(self, source, packet):
        if not self._verbose_login_flow or not self._during_login_wait():
            return
        if getattr(source, "is_satellite", False):
            return
        logger.info(
            "Slot %s [srv→] TranslatedGeneralMessagePacket lang=%r template=%r args=%s",
            self.slot_label,
            getattr(packet, "language", None),
            _trunc(getattr(packet, "template", ""), 200),
            getattr(packet, "template_args", None),
        )

    @pak.packet_listener(clientbound.ChangeMainServerPacket)
    async def _on_change_main_server(self, source, packet):
        if self._verbose_login_flow:
            logger.warning(
                "Slot %s: ChangeMainServerPacket address=%r (proxy cannot follow main-server redirect)",
                self.slot_label,
                getattr(packet, "address", None),
            )
        raise NotImplementedError(f"We do not properly handle changing the main server: {packet}")

    async def startup(self):
        self.main_srv = await self.open_main_server()
        self.satellite_srv = await self.open_satellite_server()
        if self.host_socket_policy_port is not None:
            self.socket_policy_srv = await self.open_socket_policy_server()
        bind = self.host_address
        bind_s = bind if bind is not None else "(all interfaces)"
        logger.info(
            "Slot %s listening host=%s main=%s satellite=%s",
            self.slot_label,
            bind_s,
            self.host_main_port,
            self.host_satellite_port,
        )
        for name, srv in (("main", self.main_srv), ("satellite", self.satellite_srv)):
            socks = getattr(srv, "sockets", None) or []
            for s in socks:
                try:
                    logger.info("Slot %s %s server bound to %s", self.slot_label, name, s.getsockname())
                except OSError:
                    pass

    async def new_main_connection(self, client_reader, client_writer):
        peer = None
        try:
            if client_writer.transport is not None:
                peer = client_writer.transport.get_extra_info("peername")
        except Exception:
            pass
        logger.info(
            "Slot %s: MAIN TCP accept from %r (proxy main port %s) — game/loader reached this slot",
            self.slot_label,
            peer,
            self.host_main_port,
        )
        await super().new_main_connection(client_reader, client_writer)

    async def new_satellite_connection(self, client_reader, client_writer):
        peer = None
        try:
            if client_writer.transport is not None:
                peer = client_writer.transport.get_extra_info("peername")
        except Exception:
            pass
        logger.info(
            "Slot %s: SATELLITE TCP accept from %r (satellite port %s)",
            self.slot_label,
            peer,
            self.host_satellite_port,
        )
        await super().new_satellite_connection(client_reader, client_writer)

    async def on_start(self):
        self._loop = asyncio.get_running_loop()
        tasks = [self.main_srv.serve_forever(), self.satellite_srv.serve_forever()]
        if self.socket_policy_srv is not None:
            tasks.append(self.socket_policy_srv.serve_forever())
        await asyncio.gather(*tasks)

    def _main_write_conn(self):
        if not self.main_clients:
            return None
        client = self.main_clients[0]
        return client.destination

    async def send_room_command(self, room_user_input: str) -> bool:
        """
        Send /room <name>. ``room_user_input`` is what you would type after /room, e.g. ``*Racing1``.
        """
        main_conn = self._main_write_conn()
        if main_conn is None:
            logger.error("Slot %s: no main connection for /room", self.slot_label)
            return False
        raw = room_user_input.strip()
        cmd = raw if raw.lower().startswith("room ") else f"room {raw}"
        try:
            await main_conn.write_packet_instance(serverbound.CommandPacket(command=cmd))
            logger.info("Slot %s: sent CommandPacket %r", self.slot_label, cmd)
            return True
        except Exception as e:
            logger.exception("Slot %s: /room failed: %s", self.slot_label, e)
            return False

    async def send_ban_command(self, nickname: str) -> bool:
        """Send /ban nickname#tag."""
        main_conn = self._main_write_conn()
        if main_conn is None:
            logger.error("Slot %s: no main connection for /ban", self.slot_label)
            return False
        target = normalize_nickname_tag(nickname)
        cmd = f"ban {target}"
        try:
            await main_conn.write_packet_instance(serverbound.CommandPacket(command=cmd))
            logger.info("Slot %s: sent CommandPacket %r", self.slot_label, cmd)
            return True
        except Exception as e:
            logger.exception("Slot %s: /ban failed: %s", self.slot_label, e)
            return False

    @pak.packet_listener(clientbound.LoginSuccessPacket)
    async def _on_login_success(self, source, packet):
        source.session_id = packet.session_id
        self._own_username = getattr(packet, "username", None) or ""
        if self._own_username:
            self._own_username = self._own_username.strip()
        user = self._own_username or "?"
        if self._verbose_login_flow:
            logger.info(
                "Slot %s: LoginSuccessPacket global_id=%s community=%s registered=%s session_id=%s "
                "played_time=%s staff_roles=%s modo_all_staff_ch=%s",
                self.slot_label,
                getattr(packet, "global_id", None),
                getattr(packet, "community", None),
                getattr(packet, "registered", None),
                getattr(packet, "session_id", None),
                getattr(packet, "played_time", None),
                getattr(packet, "staff_roles", None),
                getattr(packet, "modo_can_speak_in_all_staff_channels", None),
            )
        msg = f"OK  [slot {self.slot_label}] logged in as {user}"
        logger.info(msg)
        _safe_print(msg)
        if self._login_success_event is not None:
            self._login_success_event.set()

    @pak.packet_listener(clientbound.RoomMessagePacket)
    async def _on_room_chat(self, source, packet):
        msg = (getattr(packet, "message", "") or "").lower()
        if "ban" in msg:
            u = getattr(packet, "username", "") or ""
            _safe_print(f"[chat {self.slot_label}] {u}: {packet.message}")

    @pak.packet_listener(clientbound.GeneralMessagePacket)
    async def _on_general_chat(self, source, packet):
        msg = getattr(packet, "message", "") or ""
        if "ban" in msg.lower():
            _safe_print(f"[game {self.slot_label}] {msg}")
