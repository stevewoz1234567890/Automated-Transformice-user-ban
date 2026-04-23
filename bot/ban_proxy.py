"""
Local Transformice proxy for a single game client: inject /room and /ban via CommandPacket.

Same architecture as stevewoz1234567890/transformice-bot (caseus.Proxy + tfm-proxy-loader).
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def ensure_flash_trust_config() -> tuple[Path, list[str]] | None:
    """
    Windows Flash Player trust for TFMProxyLoader (same idea as transformice-bot).

    Returns ``(cfg_path, trusted_path_strings)`` after a successful write and read-back check,
    else ``None``.
    """
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        logger.warning("APPDATA not set; skipping Flash trust cfg")
        return None
    trust_dir = Path(appdata) / "Macromedia" / "Flash Player" / "#Security" / "FlashPlayerTrust"
    cfg_path = trust_dir / "TFMProxyLoader.cfg"

    trusted: list[Path] = []
    seen: set[Path] = set()
    env_game = (os.environ.get("TRANSFORMICE_GAME_DIR") or os.environ.get("TFM_GAME_DIR") or "").strip()
    if env_game:
        gp = Path(env_game).expanduser().resolve()
        if gp.is_dir() and gp not in seen:
            seen.add(gp)
            trusted.append(gp)

    bot_root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    if bot_root not in seen:
        seen.add(bot_root)
        trusted.append(bot_root)

    lines: list[str] = []
    if cfg_path.is_file():
        try:
            for raw in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = raw.strip()
                if s and s not in lines:
                    lines.append(s)
        except OSError:
            pass
    for p in trusted:
        ps = str(p)
        if ps not in lines:
            lines.append(ps)

    text = "\r\n".join(lines) + "\r\n"
    try:
        trust_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(text, encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write Flash trust cfg: %s", e)
        return None

    try:
        reread = cfg_path.read_text(encoding="utf-8", errors="strict")
    except OSError as e:
        logger.warning("Wrote %s but could not re-read for verification: %s", cfg_path, e)
        return cfg_path, lines

    def _norm(s: str) -> str:
        return "\n".join(x.strip() for x in s.replace("\r\n", "\n").splitlines() if x.strip())

    if _norm(reread) != _norm(text):
        logger.error(
            "TFMProxyLoader.cfg content mismatch after write (check permissions). Path: %s",
            cfg_path,
        )
        return cfg_path, lines

    logger.info("Flash trust cfg written and verified: %s", cfg_path)
    for i, entry in enumerate(lines, 1):
        logger.info("  TFMProxyLoader.cfg [%s/%s] %s", i, len(lines), entry)
    return cfg_path, lines


def start_shared_flash_socket_policy_thread(
    *,
    port: int,
    bind_host: str = "127.0.0.1",
) -> threading.Thread:
    """
    One Flash socket-policy server for all slots.

    TFMProxyLoader always calls ``Security.loadPolicyFile("xmlsocket://localhost:10801")`` (port 10801
    in upstream). Per-slot policy ports on other TCP ports never match, so Flash blocks the game
    socket unless this shared listener exists.
    """

    async def _serve() -> None:
        async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                peer = None
                try:
                    if writer.transport is not None:
                        peer = writer.transport.get_extra_info("peername")
                except Exception:
                    pass
                logger.info("Shared Flash policy: TCP from %r → sending socket policy (port %s)", peer, port)
                writer.write(Proxy.SOCKET_POLICY_RESPONSE)
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_client, bind_host, port)
        logger.info("Shared Flash socket-policy listening on %s:%s", bind_host, port)
        await srv.serve_forever()

    def _run() -> None:
        try:
            asyncio.run(_serve())
        except OSError as e:
            logger.error("Shared Flash socket-policy server failed: %s", e)

    t = threading.Thread(
        target=_run,
        name="tfm-flash-socket-policy",
        daemon=True,
    )
    t.start()
    return t


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
        on_first_main_connection: object | None = None,
        on_main_tcp_accepted: object | None = None,
        verbose_login_flow: bool = False,
        log_all_main_packets: bool = False,
        packet_auto_login: bool = False,
        packet_login_username: str = "",
        packet_login_password: str = "",
        packet_login_loader_url: str = "",
        packet_login_delay_sec: float = 0.35,
        packet_login_start_room: str = "",
        main_keepalive_interval_sec: float = 15.0,
        **kwargs,
    ):
        # Flash file:// SWF + Socket: use IPv4 literal so the client never targets the public
        # satellite IP from ChangeSatelliteServerPacket (sandbox Error #2048) and avoids
        # localhost → ::1 vs proxy listening on IPv4-only edge cases.
        kwargs.setdefault("expected_address", "127.0.0.1")
        super().__init__(**kwargs)
        self.slot_label = slot_label
        self._login_success_event = login_success_event
        self._on_first_main_connection = on_first_main_connection
        self._on_main_tcp_accepted = on_main_tcp_accepted
        self._first_main_hook_done = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_username: str | None = None
        self._verbose_login_flow = verbose_login_flow
        self._log_all_main_packets = log_all_main_packets
        self._main_handshake_mono: float | None = None
        self._packet_auto_login = bool(packet_auto_login)
        self._packet_login_username = (packet_login_username or "").strip()
        self._packet_login_password = packet_login_password or ""
        self._packet_login_loader_url = (packet_login_loader_url or "").strip()
        self._packet_login_delay_sec = float(packet_login_delay_sec)
        self._packet_login_start_room = packet_login_start_room or ""
        self._handshake_auth_token: int | None = None
        self._packet_login_sent = False
        self._packet_login_task: asyncio.Task | None = None
        self._main_keepalive_interval_sec = float(main_keepalive_interval_sec)
        self._main_keepalive_task: asyncio.Task | None = None
        self._main_keepalive_started = False
        # Room list collected from RoomListPacket responses; key = room name, value = player count.
        self.known_rooms: dict[str, int] = {}
        self._room_list_ready = threading.Event()
        self.register_packet_listener(self._on_room_list_cb, clientbound.RoomListPacket)
        # Player list collected after joining a room; key = username, value = session_id.
        self.known_players: dict[str, int] = {}
        self._player_list_version: int = 0
        self.register_packet_listener(self._on_set_player_list, clientbound.SetPlayerListPacket)
        self.register_packet_listener(self._on_update_player_list, clientbound.UpdatePlayerListPacket)
        self.register_packet_listener(self._vl_account_error_cb, clientbound.AccountErrorPacket)
        self.register_packet_listener(self._vl_handshake_sb, serverbound.HandshakePacket)
        if self._packet_auto_login:
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

    @pak.packet_listener(clientbound.PingPacket)
    async def _auto_pong_server_ping(self, source, packet):
        """Reply to the server's ``PingPacket`` ourselves so the connection survives Flash throttling.

        The TFM server periodically sends a ``clientbound.PingPacket`` (id 28/6) on both
        the main and satellite connections and expects a matching ``serverbound.PongPacket``
        back within a few seconds. When the Flash projector window is minimized, Flash
        throttles its Timer / ENTER_FRAME events (down to ~1 Hz or less), so its pong reply
        is delayed past the server's timeout and the server closes the TCP. That propagates
        through caseus and empties ``self.main_clients`` / ``self.satellite_clients``, which
        is why ``/ban`` later fails with "no main connection for /ban".

        ``serverbound.KeepAlivePacket`` (id 26/26) is a different, *unsolicited* client heartbeat
        and the server does not treat it as a pong. Only sending a real ``PongPacket`` with the
        echoed payload keeps the connection alive. We fire the proxy-side pong immediately and
        still forward the ping to Flash (default ``FORWARD_PACKET`` behaviour) so Flash's own
        latency/UI bookkeeping is undisturbed. If Flash also pongs later, the server simply sees
        a duplicate payload byte and ignores it — but we no longer depend on Flash's throttled
        event loop for connection liveness.
        """
        payload = getattr(packet, "payload", 0) or 0
        try:
            await source.write_packet(serverbound.PongPacket, payload=payload)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
            logger.debug(
                "Slot %s: auto-pong send failed (%s: %s); upstream already closing",
                self.slot_label,
                type(e).__name__,
                e,
            )

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
        if not self._packet_auto_login:
            return
        dest = getattr(source, "destination", None)
        if dest is not None and getattr(dest, "is_satellite", False):
            return
        self._handshake_auth_token = packet.auth_token

    async def _schedule_packet_login_after_sysinfo(self, source, packet):
        if not self._packet_auto_login:
            return
        if getattr(source, "is_satellite", False):
            return
        if not (self._packet_login_username and self._packet_login_password.strip()):
            logger.warning(
                "Slot %s: PACKET_AUTO_LOGIN enabled but username/password missing for this slot",
                self.slot_label,
            )
            return
        if not self._packet_login_loader_url:
            logger.warning(
                "Slot %s: PACKET_AUTO_LOGIN needs loader URL (packet_loader_url empty)",
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
                logger.exception("Slot %s: PACKET_AUTO_LOGIN failed", self.slot_label)

        self._packet_login_task = asyncio.create_task(_job())

    async def _send_packet_login_upstream(self) -> None:
        if self._packet_login_sent or not self.main_clients:
            return
        server_conn = self.main_clients[0].destination
        at = self._handshake_auth_token
        if at is None:
            logger.warning(
                "Slot %s: PACKET_AUTO_LOGIN skipped (no auth_token from HandshakeResponse yet)",
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
        logger.info("Slot %s: PACKET_AUTO_LOGIN — LoginPacket sent upstream (no Flash UI needed)", self.slot_label)

    async def _gate_duplicate_login_packet(self, source, packet):
        if not self._packet_auto_login:
            return
        if getattr(source, "is_satellite", False):
            return
        if self._packet_login_sent:
            logger.info(
                "Slot %s: dropping client LoginPacket (PACKET_AUTO_LOGIN already satisfied)",
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
        # Flash trust is applied once in ban_cli.start_all_slots (not here — avoids 12 threads
        # racing on the same TFMProxyLoader.cfg and WinError 32/5).
        #
        # Retry port binding: if the bot is restarted quickly the OS may not have
        # released the previous TCP ports yet (TIME_WAIT / CLOSE_WAIT).  We retry
        # a few times with a short delay before giving up.
        _bind_retries = 8
        _bind_retry_delay = 2.0
        for attempt in range(_bind_retries):
            try:
                self.main_srv = await self.open_main_server()
                self.satellite_srv = await self.open_satellite_server()
                break
            except OSError as e:
                if e.errno != 10048 or attempt == _bind_retries - 1:
                    raise
                logger.warning(
                    "Slot %s: port in use (attempt %d/%d), retrying in %.0fs…",
                    self.slot_label,
                    attempt + 1,
                    _bind_retries,
                    _bind_retry_delay,
                )
                await asyncio.sleep(_bind_retry_delay)
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
        if self._on_main_tcp_accepted is not None:
            try:
                self._on_main_tcp_accepted()
            except Exception:
                logger.exception(
                    "Slot %s: on_main_tcp_accepted callback failed",
                    self.slot_label,
                )
        # caseus.Proxy.new_main_connection awaits listen() until this TCP session ends; the hook must run
        # *before* that or FLASH auto-login would only fire on disconnect.
        if (
            self._on_first_main_connection is not None
            and not self._first_main_hook_done
        ):
            self._first_main_hook_done = True
            hook = self._on_first_main_connection
            loop = asyncio.get_running_loop()

            def _run_hook() -> None:
                try:
                    hook()
                except Exception:
                    logger.exception(
                        "Slot %s: on_first_main_connection hook failed",
                        self.slot_label,
                    )

            logger.info(
                "Slot %s: scheduling FLASH auto-login (first MAIN TCP, before proxy listen)",
                self.slot_label,
            )
            loop.run_in_executor(None, _run_hook)

        # Mirror ``new_satellite_connection``: swallow abrupt Flash-side disconnects so the
        # log isn't flooded with "Task exception was never retrieved" tracebacks when we
        # force-close a stuck Flash window (BOT_FLASH_CLOSE_ON_LOGIN_FAIL). On Windows the
        # reset surfaces as WinError 64 ("El nombre de red especificado ya no está
        # disponible") wrapped in ConnectionResetError.
        try:
            await super().new_main_connection(client_reader, client_writer)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
            logger.debug(
                "Slot %s: main connection closed (%s: %s)",
                self.slot_label,
                type(e).__name__,
                e,
            )

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
        try:
            await super().new_satellite_connection(client_reader, client_writer)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            logger.debug(
                "Slot %s: satellite connection closed (%s: %s)",
                self.slot_label,
                type(e).__name__,
                e,
            )

    async def on_start(self):
        self._loop = asyncio.get_running_loop()
        tasks = [self.main_srv.serve_forever(), self.satellite_srv.serve_forever()]
        if self.socket_policy_srv is not None:
            tasks.append(self.socket_policy_srv.serve_forever())
        await asyncio.gather(*tasks)

    def _main_write_conn(self):
        """Return the best upstream connection for sending game commands.

        Prefers the main connection; falls back to the most recent satellite
        connection if the main TCP was closed.  Using sat[-1] (newest) rather
        than sat[0] (oldest) ensures we use the room satellite that was
        established after JoinRoomPacket, not the stale login satellite.
        """
        if self.main_clients:
            return self.main_clients[0].destination
        sat = getattr(self, "satellite_clients", None)
        if sat:
            return sat[-1].destination
        return None

    async def _on_room_list_cb(self, source, packet):
        """Collect rooms from every RoomListPacket the server sends."""
        if getattr(source, "is_satellite", False):
            return
        for r in (getattr(packet, "rooms", None) or []):
            name = (getattr(r, "name", "") or "").strip()
            if not name:
                continue
            try:
                num = int(getattr(r, "num_players", 0) or 0)
            except (TypeError, ValueError):
                num = 0
            self.known_rooms[name] = num
        self._room_list_ready.set()

    async def _on_set_player_list(self, source, packet):
        """Full player list sent by the server when we join a room (arrives via satellite)."""
        self.known_players.clear()
        for p in (getattr(packet, "players", None) or []):
            username = (getattr(p, "username", "") or "").strip()
            if username:
                self.known_players[username] = getattr(p, "session_id", 0) or 0
        self._player_list_version += 1

    async def _on_update_player_list(self, source, packet):
        """Single player joining the room after us (arrives via satellite)."""
        p = getattr(packet, "player", None)
        if p is None:
            return
        username = (getattr(p, "username", "") or "").strip()
        if username:
            self.known_players[username] = getattr(p, "session_id", 0) or 0

    async def join_room(self, room_name: str, community: str = "") -> bool:
        """
        Send ``JoinRoomPacket`` directly to the upstream main server as if the
        Flash client requested the room change.  This is what the game client
        sends when the player selects a room from the list (as opposed to the
        ``/room`` slash-command).  The server responds with ``JoinedRoomPacket``
        + ``SetPlayerListPacket`` (and other setup) which our listeners capture.
        """
        main_conn = self._main_write_conn()
        if main_conn is None:
            logger.warning("Slot %s: join_room — no main connection", self.slot_label)
            return False
        name = room_name.strip()
        await main_conn.write_packet_instance(
            serverbound.JoinRoomPacket(
                community=community,
                name=name,
                password="",
                auto=False,
                customization=None,
            )
        )
        logger.info("Slot %s: sent JoinRoomPacket %r", self.slot_label, name)
        return True

    async def request_room_list(self, game_mode_int: int = 1) -> bool:
        """
        Ask the server for the room list for *game_mode_int*.
        The response arrives as a ``clientbound.RoomListPacket`` and is captured
        by ``_on_room_list_cb``.  Common values: 1=Transformice, 2=Bootcamp,
        3=Vanilla, 4=Survivor, 5=Racing, 9=Module.
        """
        from caseus import enums as _enums
        main_conn = self._main_write_conn()
        if main_conn is None:
            return False
        try:
            gm = _enums.GameMode(game_mode_int)
        except (ValueError, KeyError):
            gm = _enums.GameMode.NONE
        await main_conn.write_packet_instance(
            serverbound.RoomListPacket(game_mode=gm)
        )
        return True

    async def send_ban_command(self, nickname: str) -> bool:
        """Send /ban nickname#tag."""
        main_conn = self._main_write_conn()
        if main_conn is None:
            logger.error("Slot %s: no main connection for /ban", self.slot_label)
            return False
        conn_type = (
            "main" if self.main_clients else
            "satellite[-1]" if getattr(self, "satellite_clients", None) else "unknown"
        )
        target = normalize_nickname_tag(nickname)
        cmd = f"ban {target}"
        try:
            await main_conn.write_packet_instance(serverbound.CommandPacket(command=cmd))
            logger.info("Slot %s: sent CommandPacket %r via %s", self.slot_label, cmd, conn_type)
            return True
        except Exception as e:
            logger.exception("Slot %s: /ban failed via %s: %s", self.slot_label, conn_type, e)
            return False

    async def _main_keepalive_loop(self) -> None:
        """Periodically send serverbound KeepAlivePacket on the main connection.

        Flash throttles Timer events when its window is minimized or in the
        background, so its own KeepAlivePackets stop firing and the TFM server
        eventually closes the idle main TCP (slot then shows as [PARTL] /
        "login ok but upstream closed").  This task runs in the proxy's own
        asyncio loop, so it is unaffected by Flash throttling, and keeps the
        main connection alive until we're ready to issue /ban commands.

        The loop exits when the main connection goes away, the task is
        cancelled, or write_packet raises a connection error.
        """
        interval = self._main_keepalive_interval_sec
        if interval <= 0:
            return
        logger.info(
            "Slot %s: main-keepalive loop started (interval=%.1fs)",
            self.slot_label,
            interval,
        )
        sent = 0
        try:
            while True:
                await asyncio.sleep(interval)
                conn = None
                if self.main_clients:
                    conn = self.main_clients[0].destination
                if conn is None:
                    logger.info(
                        "Slot %s: main-keepalive loop exiting (no main upstream; sent=%d)",
                        self.slot_label,
                        sent,
                    )
                    return
                try:
                    await conn.write_packet(serverbound.KeepAlivePacket)
                    sent += 1
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                    logger.info(
                        "Slot %s: main-keepalive send failed (%s: %s); stopping loop (sent=%d)",
                        self.slot_label,
                        type(e).__name__,
                        e,
                        sent,
                    )
                    return
                except Exception:
                    logger.exception(
                        "Slot %s: main-keepalive unexpected error; stopping loop",
                        self.slot_label,
                    )
                    return
        except asyncio.CancelledError:
            logger.debug(
                "Slot %s: main-keepalive loop cancelled (sent=%d)",
                self.slot_label,
                sent,
            )
            raise

    def _start_main_keepalive(self) -> None:
        if self._main_keepalive_started:
            return
        if self._main_keepalive_interval_sec <= 0:
            logger.debug("Slot %s: main-keepalive disabled (interval<=0)", self.slot_label)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._main_keepalive_started = True
        self._main_keepalive_task = loop.create_task(self._main_keepalive_loop())

    @pak.packet_listener(clientbound.LoginSuccessPacket)
    async def _on_login_success(self, source, packet):
        source.session_id = packet.session_id
        self._own_username = getattr(packet, "username", None) or ""
        if self._own_username:
            self._own_username = self._own_username.strip()
        user = self._own_username or "?"
        self._start_main_keepalive()
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
