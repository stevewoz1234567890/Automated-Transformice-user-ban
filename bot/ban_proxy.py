"""
Local Transformice proxy for a single game client: inject /room and /ban via CommandPacket.

Same architecture as stevewoz1234567890/transformice-bot (caseus.Proxy + tfm-proxy-loader).
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import threading
import time
from pathlib import Path

import pak
from caseus import Proxy, Secrets
from caseus.packets import Packet, ServerboundPacket, clientbound, serverbound
from caseus.packets.packet import ClientboundPacket
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
        upstream_connect_diag: bool = True,
        packet_login_auth_key_fallback: int | None = None,
        packet_login_packet_key_sources_fallback: list | tuple | None = None,
        bootstrap_secrets: Secrets | None = None,
        login_diagnostics: bool = True,
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
        self._sysinfo_received: bool = False
        self._login_watchdog_task: asyncio.Task | None = None
        self._upstream_connect_diag = upstream_connect_diag
        self._upstream_tcp_established: bool = False
        self._upstream_open_streams_entered: bool = False
        self._upstream_endpoint: tuple[str, int] | None = None
        self._packet_login_auth_key_fallback = packet_login_auth_key_fallback
        self._packet_login_packet_key_sources_fallback = packet_login_packet_key_sources_fallback
        self._bootstrap_secrets = bootstrap_secrets
        self._login_diag = login_diagnostics
        self._login_diag_upstream_cb_seq = 0
        self.register_packet_listener(self._vl_account_error_cb, clientbound.AccountErrorPacket)
        self.register_packet_listener(
            self._log_client_verification_challenge,
            clientbound.ClientVerificationPacket,
        )
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
        if self._login_diag:
            self.register_packet_listener(
                self._login_diag_clientbound_from_upstream,
                ClientboundPacket,
            )
            self.register_packet_listener(
                self._login_diag_after_handshake_sb,
                serverbound.HandshakePacket,
                after=True,
            )
            self.register_packet_listener(
                self._login_diag_set_language_sb,
                serverbound.SetLanguagePacket,
                after=True,
            )

    @staticmethod
    def _listen_stream_label(source_conn) -> str:
        """Identify which TCP leg failed for listen-loop diagnostics."""
        if getattr(source_conn, "is_satellite", False):
            return "satellite"
        name = type(source_conn).__name__
        if name == "ServerConnection":
            return "upstream(game→proxy)"
        if name == "ClientConnection":
            return "local(client→proxy)"
        return name

    async def _listen_impl(self, source_conn):
        """Same as ``caseus.Proxy._listen_impl`` but log parse/read errors to ``log.txt``."""
        label = self._listen_stream_label(source_conn)
        while self.is_serving() and not source_conn.is_closing():
            clean_eof = False
            try:
                async for packet in source_conn.continuously_read_packets():
                    packet.make_immutable()

                    await self._listen_to_packet(source_conn, packet)

                clean_eof = True
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.exception(
                    "Slot %s: [login] MAIN listen stopped on %s — %s: %s "
                    "(parse/decrypt vs secrets, wrong game_version, or truncated TCP; "
                    "see [login][diag] lines if PROXY_LOGIN_DIAGNOSTICS is on)",
                    self.slot_label,
                    label,
                    type(e).__name__,
                    e,
                )
                raise
            finally:
                await self.end_listener_tasks()
            if clean_eof and self._login_diag:
                self._log_listen_stream_ended_clean_eof(label)

    def _log_listen_stream_ended_clean_eof(self, label: str) -> None:
        ev = self._login_success_event
        success = ev is not None and ev.is_set()
        waiting = self._during_login_wait()
        extra = ""
        if label == "upstream(game→proxy)" and waiting and not success:
            extra = (
                " If Handshake was sent but no HandshakeResponse was logged, the server likely "
                "closed without answering or bytes could not be parsed as packets."
            )
        log_fn = logger.warning if waiting and not success else logger.info
        log_fn(
            "Slot %s: [login][diag] %s read loop ended (EOF / peer closed TCP). "
            "during_login_wait=%s login_success=%s handshake_auth_token=%s "
            "sysinfo_received=%s login_packet_sent=%s upstream_tcp_ok=%s "
            "upstream_clientbound_logged=%s%s",
            self.slot_label,
            label,
            waiting,
            success,
            self._handshake_auth_token is not None,
            self._sysinfo_received,
            self._packet_login_sent,
            self._upstream_tcp_established,
            self._login_diag_upstream_cb_seq,
            extra,
        )

    async def _login_diag_clientbound_from_upstream(self, source, packet):
        if not self._during_login_wait():
            return
        if getattr(source, "is_satellite", False):
            return
        if type(source).__name__ != "ServerConnection":
            return
        tn = type(packet).__name__
        if tn in ("KeepAlivePacket", "IPSPingPacket", "PingPacket"):
            return
        if isinstance(packet, clientbound.LoginSuccessPacket):
            return
        self._login_diag_upstream_cb_seq += 1
        pid = getattr(packet, "id", None)
        body = ""
        if isinstance(packet, pak.GenericPacket):
            body = f" generic_code={getattr(packet, 'code', None)!r}"
        logger.info(
            "Slot %s: [login][diag] srv→proxy #%s %s id=%s%s",
            self.slot_label,
            self._login_diag_upstream_cb_seq,
            tn,
            pid,
            body,
        )

    async def _login_diag_after_handshake_sb(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        srv = source.destination
        if type(srv).__name__ != "ServerConnection":
            return
        sec = getattr(srv, "secrets", None)
        logger.info(
            "Slot %s: [login][diag] Handshake forwarded to upstream; server_conn ctx "
            "game_version=%r has_packet_key_sources=%s has_verification_template=%s has_auth_key=%s",
            self.slot_label,
            getattr(sec, "game_version", None),
            getattr(sec, "packet_key_sources", None) is not None,
            getattr(sec, "client_verification_template", None) is not None,
            getattr(sec, "auth_key", None) is not None,
        )

    async def _login_diag_set_language_sb(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        if type(source).__name__ != "ClientConnection":
            return
        logger.info(
            "Slot %s: [login][diag] client sent SetLanguagePacket toward server (after HandshakeResponse)",
            self.slot_label,
        )

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
        logger.debug(
            "Slot %s: ChangeSatelliteServer rewrite → %r ports=%s (was %r %s)",
            self.slot_label,
            addr,
            proxied_ports,
            getattr(packet, "address", None),
            getattr(packet, "ports", None),
        )
        await source.destination.write_packet_instance(proxied)
        return self.DO_NOTHING

    def _reset_main_session_state(self) -> None:
        """New MAIN TCP client: clear login pipeline state."""
        self._handshake_auth_token = None
        self._packet_login_sent = False
        self._sysinfo_received = False
        self._main_handshake_mono = None
        self._upstream_tcp_established = False
        self._upstream_open_streams_entered = False
        self._upstream_endpoint = None
        if self._packet_login_task is not None and not self._packet_login_task.done():
            self._packet_login_task.cancel()
        self._packet_login_task = None
        self._login_diag_upstream_cb_seq = 0

    async def _login_stall_watchdog(self) -> None:
        """Warn when the login pipeline stalls (helps diagnose silent upstream failures)."""
        try:
            await asyncio.sleep(20.0)
            if self._login_success_event is not None and self._login_success_event.is_set():
                return
            if self._handshake_auth_token is None:
                detail = ""
                if not self._upstream_tcp_established:
                    if self._upstream_open_streams_entered:
                        detail = (
                            " (upstream TCP never completed — see earlier [login] upstream TCP attempt lines; "
                            "possible slow connect, RST, or firewall on python.exe)"
                        )
                    else:
                        detail = (
                            " (still before/during open_streams to game host — connect may be blocked or hanging)"
                        )
                else:
                    detail = (
                        f" (upstream TCP OK to {self._upstream_endpoint!r} but no HandshakeResponse — "
                        "wrong game_version/secrets, server drop, or packet parse issue)"
                    )
                    if self._login_diag:
                        detail += (
                            f" [diag: upstream clientbound packets decoded before timeout="
                            f"{self._login_diag_upstream_cb_seq}]"
                        )
                logger.warning(
                    "Slot %s: [login] no HandshakeResponse from upstream after 20s%s — "
                    "check UPSTREAM host/ports, game version vs secrets, firewall, or server load",
                    self.slot_label,
                    detail,
                )
                return
            await asyncio.sleep(25.0)
            if self._login_success_event is not None and self._login_success_event.is_set():
                return
            if not self._sysinfo_received:
                logger.warning(
                    "Slot %s: [login] HandshakeResponse OK but no SystemInformationPacket from client "
                    "after 25s — headless client or loader may be stuck",
                    self.slot_label,
                )
            elif not self._packet_login_sent:
                logger.warning(
                    "Slot %s: [login] SystemInformation seen but LoginPacket was not injected "
                    "(missing username/password/loader URL on this slot?)",
                    self.slot_label,
                )
        except asyncio.CancelledError:
            pass

    async def _log_client_verification_challenge(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        logger.warning(
            "Slot %s: [login] server sent ClientVerificationPacket (anti-bot) — "
            "if login fails, the headless path may not satisfy this challenge; try a full client",
            self.slot_label,
        )

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
        logger.info(
            "Slot %s: [login] HandshakeResponse received auth_token=%s (num_online=%s)",
            self.slot_label,
            packet.auth_token,
            getattr(packet, "num_online_players", None),
        )
        if self._login_diag and self._main_handshake_mono is not None:
            logger.info(
                "Slot %s: [login][diag] HandshakeResponse %.3fs after first MAIN HandshakePacket",
                self.slot_label,
                time.monotonic() - self._main_handshake_mono,
            )

    async def _schedule_packet_login_after_sysinfo(self, source, packet):
        if getattr(source, "is_satellite", False):
            return
        self._sysinfo_received = True
        delay = max(0.0, self._packet_login_delay_sec)
        if not (self._packet_login_username and self._packet_login_password.strip()):
            logger.warning(
                "Slot %s: [login] SystemInformation received but username/password missing — cannot inject LoginPacket",
                self.slot_label,
            )
            return
        if not self._packet_login_loader_url:
            logger.warning(
                "Slot %s: [login] SystemInformation received but packet_loader_url empty — cannot inject LoginPacket",
                self.slot_label,
            )
            return
        logger.info(
            "Slot %s: [login] SystemInformation received — injecting LoginPacket in %.2fs",
            self.slot_label,
            delay,
        )

        async def _job() -> None:
            try:
                await asyncio.sleep(delay)
                if self._packet_login_sent:
                    return
                await self._send_packet_login_upstream()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Slot %s: [login] packet login task failed", self.slot_label)

        self._packet_login_task = asyncio.create_task(_job())

    async def _send_packet_login_upstream(self) -> None:
        if self._packet_login_sent or not self.main_clients:
            return
        server_conn = self.main_clients[0].destination
        at = self._handshake_auth_token
        if at is None:
            logger.warning(
                "Slot %s: [login] cannot inject LoginPacket — no auth_token yet (HandshakeResponse missing?)",
                self.slot_label,
            )
            return
        secrets = server_conn.secrets
        if (
            getattr(secrets, "packet_key_sources", None) is None
            and self._packet_login_packet_key_sources_fallback is not None
        ):
            server_conn.secrets = secrets.copy(
                packet_key_sources=self._packet_login_packet_key_sources_fallback
            )
            secrets = server_conn.secrets
        ak = getattr(secrets, "auth_key", None)
        if ak is None and self._packet_login_auth_key_fallback is not None:
            ak = self._packet_login_auth_key_fallback
        if ak is not None:
            ciphered = int(at) ^ int(ak)
        else:
            ciphered = None
        logger.info(
            "Slot %s: [login] sending LoginPacket username=%r auth_key_present=%s ciphered_token=%s "
            "(auth_key from server_conn=%s fallback=%s packet_key_sources merged=%s)",
            self.slot_label,
            self._packet_login_username,
            ak is not None,
            ciphered is not None,
            getattr(server_conn.secrets, "auth_key", None) is not None,
            self._packet_login_auth_key_fallback is not None,
            self._packet_login_packet_key_sources_fallback is not None,
        )
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
        logger.info(
            "Slot %s: [login] LoginPacket sent upstream (waiting for LoginSuccess or AccountError)",
            self.slot_label,
        )

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
        logger.warning(
            "Slot %s: [login] AccountErrorPacket error_code=%s suggested_username=%r unk=%r — %s",
            self.slot_label,
            ec,
            getattr(packet, "suggested_username", None),
            _trunc(getattr(packet, "unk_string_3", ""), 80),
            hint,
        )

    async def _vl_captcha_cb(self, source, packet):
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

    async def open_streams(self, address, ports):
        """
        Same behavior as ``caseus.Proxy.open_streams`` (random port order), but log each
        ``asyncio.open_connection`` attempt. The base implementation swallows exceptions,
        which hides refused / timeout / firewall errors.
        """
        if not self._upstream_connect_diag:
            return await super().open_streams(address, ports)

        ports_seq = list(ports)
        order = random.sample(ports_seq, len(ports_seq))
        self._upstream_open_streams_entered = True
        logger.info(
            "Slot %s: [login] upstream TCP: connecting to host=%r port_try_order=%s (pool=%s)",
            self.slot_label,
            address,
            order,
            tuple(ports_seq),
        )
        last_exc: BaseException | None = None
        for port in order:
            try:
                t0 = time.monotonic()
                server_reader, server_writer = await asyncio.open_connection(address, port)
                dt = time.monotonic() - t0
                peer = None
                try:
                    if server_writer.transport is not None:
                        peer = server_writer.transport.get_extra_info("peername")
                except Exception:
                    pass
                self._upstream_tcp_established = True
                self._upstream_endpoint = (str(address), int(port))
                logger.info(
                    "Slot %s: [login] upstream TCP connected %s:%s in %.3fs remote_peer=%r",
                    self.slot_label,
                    address,
                    port,
                    dt,
                    peer,
                )
                return server_reader, server_writer
            except Exception as e:
                last_exc = e
                extra = ""
                if isinstance(e, OSError):
                    for name in ("errno", "winerror"):
                        v = getattr(e, name, None)
                        if v is not None:
                            extra += f" {name}={v}"
                logger.warning(
                    "Slot %s: [login] upstream TCP attempt failed %s:%s — %s: %s%s",
                    self.slot_label,
                    address,
                    port,
                    type(e).__name__,
                    e,
                    extra,
                )
        logger.error(
            "Slot %s: [login] upstream TCP all ports failed — last_error=%s: %s",
            self.slot_label,
            type(last_exc).__name__ if last_exc else "None",
            last_exc,
        )
        raise ValueError(f"Unable to connect to address '{address}' on ports {ports}")

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
                    logger.debug("Slot %s %s bound %s", self.slot_label, name, s.getsockname())
                except OSError:
                    pass

    async def new_main_connection(self, client_reader, client_writer):
        self._reset_main_session_state()
        peer = None
        try:
            if client_writer.transport is not None:
                peer = client_writer.transport.get_extra_info("peername")
        except Exception:
            pass
        logger.info(
            "Slot %s: [login] MAIN client connected from %r (listening port %s)",
            self.slot_label,
            peer,
            self.host_main_port,
        )
        if getattr(self, "main_server_address", None) is not None:
            logger.info(
                "Slot %s: [login] proxy upstream target host=%r ports=%r",
                self.slot_label,
                self.main_server_address,
                self.main_server_ports,
            )
        if self._login_watchdog_task is not None and not self._login_watchdog_task.done():
            self._login_watchdog_task.cancel()
        self._login_watchdog_task = asyncio.create_task(self._login_stall_watchdog())
        try:
            client = self.ClientConnection(self, reader=client_reader, writer=client_writer)
            if self._bootstrap_secrets is not None:
                client.secrets = self._bootstrap_secrets.copy()

            if self.main_server_address is not None and self.main_server_ports is not None:
                try:
                    server_reader, server_writer = await self.open_streams(
                        self.main_server_address, self.main_server_ports
                    )
                except ValueError:
                    client.close()
                    await client.wait_closed()
                    raise

                server = self.ServerConnection(
                    self, destination=client, reader=server_reader, writer=server_writer
                )
                client.destination = server
                client.main.server = server
                if self._bootstrap_secrets is not None:
                    server.secrets = client.secrets

            if self._login_diag and self._bootstrap_secrets is not None:
                bs = self._bootstrap_secrets
                tok = getattr(bs, "connection_token", None) or ""
                logger.info(
                    "Slot %s: [login][diag] proxy legs seeded from bootstrap: game_version=%r "
                    "connection_token_len=%s has_packet_key_sources=%s has_verification_template=%s",
                    self.slot_label,
                    getattr(bs, "game_version", None),
                    len(tok) if isinstance(tok, str) else 0,
                    getattr(bs, "packet_key_sources", None) is not None,
                    getattr(bs, "client_verification_template", None) is not None,
                )

            async with client:
                await self.listen(client)
        except ValueError as e:
            err_s = str(e)
            if "Unable to connect" in err_s:
                logger.error(
                    "Slot %s: [login] upstream TCP failed: %s — refresh tfm-secrets for the "
                    "current game build; check firewall/VPN; optionally set "
                    "UPSTREAM_SERVER_ADDRESS and UPSTREAM_SERVER_PORTS in bot/config.py (try main port 11801).",
                    self.slot_label,
                    err_s,
                )
                return
            raise
        finally:
            if self._login_watchdog_task is not None and not self._login_watchdog_task.done():
                self._login_watchdog_task.cancel()
                self._login_watchdog_task = None

    async def new_satellite_connection(self, client_reader, client_writer):
        peer = None
        try:
            if client_writer.transport is not None:
                peer = client_writer.transport.get_extra_info("peername")
        except Exception:
            pass
        logger.debug(
            "Slot %s: SATELLITE TCP from %r (port %s)",
            self.slot_label,
            peer,
            self.host_satellite_port,
        )
        await super().new_satellite_connection(client_reader, client_writer)

    async def on_start(self):
        self._loop = asyncio.get_running_loop()
        loop = self._loop
        prior = loop.get_exception_handler()

        def _asyncio_exc_handler(loop_ref, context):
            exc = context.get("exception")
            if exc is not None:
                logger.error(
                    "Slot %s: asyncio: %s",
                    self.slot_label,
                    context.get("message", ""),
                    exc_info=exc,
                )
            elif context.get("message"):
                logger.warning(
                    "Slot %s: asyncio: %s",
                    self.slot_label,
                    context.get("message"),
                )
            if prior is not None:
                prior(loop_ref, context)
            else:
                loop_ref.default_exception_handler(context)

        loop.set_exception_handler(_asyncio_exc_handler)
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
            logger.debug(
                "Slot %s: LoginSuccessPacket details global_id=%s community=%s registered=%s session_id=%s",
                self.slot_label,
                getattr(packet, "global_id", None),
                getattr(packet, "community", None),
                getattr(packet, "registered", None),
                getattr(packet, "session_id", None),
            )
        if self._login_watchdog_task is not None and not self._login_watchdog_task.done():
            self._login_watchdog_task.cancel()
        msg = f"OK  [slot {self.slot_label}] logged in as {user}"
        logger.info(msg)
        _safe_print(msg)
        if self._login_diag:
            logger.info(
                "Slot %s: [login][diag] LoginSuccess — pipeline complete (session_id=%r)",
                self.slot_label,
                getattr(packet, "session_id", None),
            )
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
