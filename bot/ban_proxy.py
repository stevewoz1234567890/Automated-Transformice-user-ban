"""
Local Transformice proxy for a single game client: inject /room and /ban via CommandPacket.

Same architecture as stevewoz1234567890/transformice-bot (caseus.Proxy + tfm-proxy-loader).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import threading
import time
import weakref
from pathlib import Path

import pak
from caseus import Proxy
from caseus.packets import Packet, ServerboundPacket, clientbound, serverbound
from caseus.util.crypto import shakikoo

from .upstream_socket_bind import upstream_local_bind_tuple
from .trace_log import trace_step
from .issue1_forensic import dismiss_tail_issue1_cues

logger = logging.getLogger(__name__)

_print_lock = threading.Lock()

# What the CLI is doing (room list, join, /ban, …). Logged on MAIN teardown so mass
# ``clean-eof`` lines can be correlated with operator phase without guessing.
_OPERATOR_PHASE_LOCK = threading.Lock()
_OPERATOR_PHASE: str = "startup"


def set_operator_phase(name: str) -> None:
    """Set a short phase label (e.g. ``room_list``, ``ban``) for MAIN-close correlation."""
    global _OPERATOR_PHASE
    with _OPERATOR_PHASE_LOCK:
        _OPERATOR_PHASE = (name or "startup").strip() or "startup"


def get_operator_phase() -> str:
    with _OPERATOR_PHASE_LOCK:
        return _OPERATOR_PHASE


def _packet_diag_label(packet: object) -> str:
    """
    Type name for ring buffers, with GenericPacket code tuple when available
    (easier to correlate with TFM / caseus enum dumps than a bare 'Generic' name).
    """
    tn = type(packet).__name__
    try:
        if isinstance(packet, pak.GenericPacket):
            c = getattr(packet, "code", None)
            if c is not None:
                return f"{tn}{c!r}"
    except Exception:
        return tn
    return tn


def _diag_label_looks_change_sat(name: str) -> bool:
    """Ring-buffer names may be ``...ChangeSatellite...`` or generic (26, 41) sat redirect codes."""
    if "ChangeSatellite" in name or "ChangeSat" in name:
        return True
    return "(26, 41)" in name or "(26,41)" in name


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

    loader_patch_dir = (bot_root / "tmp" / "loader_patch").resolve()
    try:
        loader_patch_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if loader_patch_dir not in seen:
        seen.add(loader_patch_dir)
        trusted.append(loader_patch_dir)

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

    logger.info(
        "Flash trust cfg verified (%d path(s)): %s",
        len(lines),
        cfg_path,
    )
    for i, entry in enumerate(lines, 1):
        logger.debug("  TFMProxyLoader.cfg [%s/%s] %s", i, len(lines), entry)
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


_proxy_heartbeat_log_every_n: int | None = None


def _proxy_heartbeat_log_every() -> int:
    """
    How often to log auto-pong / main-keepalive at INFO (every Nth event per slot).
    With many slots, logging every 10 fills the console during ``input()`` prompts
    and looks like an infinite loop. Set ``BOT_PROXY_HEARTBEAT_LOG_EVERY=100`` (default)
    or higher to quiet; set lower for debugging.
    """
    global _proxy_heartbeat_log_every_n
    if _proxy_heartbeat_log_every_n is not None:
        return _proxy_heartbeat_log_every_n
    raw = (os.environ.get("BOT_PROXY_HEARTBEAT_LOG_EVERY") or "").strip()
    if not raw:
        n = 100
    else:
        try:
            n = max(1, min(10_000, int(raw, 0)))
        except ValueError:
            n = 100
    _proxy_heartbeat_log_every_n = n
    return n


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _issue1_handshake_probe_enabled() -> bool:
    """One INFO line per MAIN TCP (anchor ``ISSUE1_HANDSHAKE_PROBE``). Off if env is falsey-off."""
    v = (os.environ.get("BOT_ISSUE1_HANDSHAKE_PROBE") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def _main_packet_ring_limit() -> int:
    """Ring buffer of last MAIN packet labels per direction (raise when hunting clean-eof)."""
    try:
        n = int((os.environ.get("BOT_PROXY_MAIN_PACKET_RING") or "8").strip())
    except ValueError:
        n = 8
    return max(4, min(64, n))


def _root_cause_exc_fields(exc: BaseException | None) -> str:
    if exc is None:
        return "exc=(none)"
    parts: list[str] = [f"type={type(exc).__name__}", f"msg={exc!s}"]
    for attr in ("errno", "winerror"):
        if hasattr(exc, attr):
            try:
                parts.append(f"{attr}={getattr(exc, attr)!r}")
            except Exception:
                pass
    return ";".join(parts)


def _root_cause_flash_tcp_snapshot(client_writer: object | None) -> str:
    """Best-effort Flash→proxy StreamWriter / transport state at MAIN teardown (not upstream)."""
    chunks: list[str] = []
    try:
        if client_writer is None:
            return "Flash_tcp=writer_none"
        ic = getattr(client_writer, "is_closing", None)
        chunks.append(f"writer_closing={ic() if callable(ic) else ic}")
        tr = getattr(client_writer, "transport", None)
        if tr is None:
            chunks.append("transport=None")
            return "Flash_tcp=" + ";".join(chunks)
        chunks.append(f"tr_is_closing={tr.is_closing()}")
        try:
            chunks.append(f"peer={tr.get_extra_info('peername')!r}")
        except Exception:
            chunks.append("peer=?")
        try:
            chunks.append(f"sockname={tr.get_extra_info('sockname')!r}")
        except Exception:
            chunks.append("sockname=?")
    except Exception as e:
        chunks.append(f"snapshot_err={e!r}")
    return "Flash_tcp=" + ";".join(chunks)


def _main_ring_timeline(buf: list[tuple[float, str]], *, now_mono: float, tag: str) -> str:
    if not buf:
        return f"{tag}=(empty)"
    return f"{tag}=" + ";".join(
        f"{now_mono - ts:.3f}sAgo:{name}" for ts, name in buf
    )


# One semaphore per running event loop: caps simultaneous TCP handshakes to the game host
# across all BanBotProxy slots (Windows WinError 121 under many concurrent connects).
_upstream_connect_sem_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _parse_upstream_max_concurrent_connects() -> int:
    raw = (os.environ.get("BOT_UPSTREAM_MAX_CONCURRENT_CONNECTS") or "").strip()
    try:
        n = int(raw) if raw else 2
    except ValueError:
        n = 2
    return max(1, min(n, 64))


def _upstream_open_connection_timeout_sec() -> float:
    raw = (os.environ.get("BOT_UPSTREAM_OPEN_CONNECTION_TIMEOUT_SEC") or "").strip()
    try:
        t = float(raw) if raw else 12.0
    except ValueError:
        t = 12.0
    return max(3.0, min(t, 120.0))


def _open_streams_round_retries() -> int:
    """Extra full port-list sweeps after every port fails once (aligns with preflight retries)."""
    raw = (os.environ.get("BOT_UPSTREAM_OPEN_STREAMS_ROUND_RETRIES") or "").strip()
    if raw:
        try:
            return max(0, min(int(raw), 10))
        except ValueError:
            pass
    raw2 = (os.environ.get("BOT_UPSTREAM_PROBE_RETRIES") or "").strip()
    try:
        n = int(raw2) if raw2 else 2
    except ValueError:
        n = 2
    return max(0, min(n, 10))


def _open_streams_round_pause_sec() -> float:
    raw = (os.environ.get("BOT_UPSTREAM_OPEN_STREAMS_ROUND_PAUSE_SEC") or "").strip()
    if raw:
        try:
            return max(0.0, min(float(raw), 60.0))
        except ValueError:
            pass
    raw2 = (os.environ.get("BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC") or "").strip()
    try:
        p = float(raw2) if raw2 else 3.0
    except ValueError:
        p = 3.0
    return max(0.0, min(p, 60.0))


def _upstream_connect_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _upstream_connect_sem_by_loop.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_parse_upstream_max_concurrent_connects())
        _upstream_connect_sem_by_loop[loop] = sem
    return sem


class BanBotProxy(Proxy):
    """One proxy port ↔ one game instance; sends slash-commands as CommandPacket (no leading /)."""

    def __init__(
        self,
        *,
        slot_label: str = "",
        login_success_event: threading.Event | None = None,
        login_aborted_event: threading.Event | None = None,
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
        account_bind_ip: str = "",
        **kwargs,
    ):
        # Flash file:// SWF + Socket: use IPv4 literal so the client never targets the public
        # satellite IP from ChangeSatelliteServerPacket (sandbox Error #2048) and avoids
        # localhost → ::1 vs proxy listening on IPv4-only edge cases.
        kwargs.setdefault("expected_address", "127.0.0.1")
        super().__init__(**kwargs)
        # caseus.Proxy rewrites HandshakePacket.loader_stage_size to CORRECTED_LOADER_SIZE (vanilla TFMLoader
        # magic). Override when live server/tooling expects a different value (BOT_PROXY_* below).
        _lss_ov = (
            os.environ.get("BOT_PROXY_HANDSHAKE_LOADER_STAGE_SIZE")
            or os.environ.get("BOT_PROXY_CORRECTED_LOADER_STAGE_SIZE")
            or ""
        ).strip()
        if _lss_ov:
            try:
                self.CORRECTED_LOADER_SIZE = int(_lss_ov, 0)
            except ValueError:
                logger.warning(
                    "Slot %s: invalid BOT_PROXY_HANDSHAKE_LOADER_STAGE_SIZE=%r (expected int or 0x hex) — "
                    "using caseus default %s",
                    (slot_label or "").strip() or "?",
                    _lss_ov,
                    getattr(self, "CORRECTED_LOADER_SIZE", Proxy.CORRECTED_LOADER_SIZE),
                )
        self.slot_label = slot_label
        self._login_success_event = login_success_event
        self._login_aborted_event = login_aborted_event
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
        # Proxifier reference only (logged when upstream TCP fails); not used for bind().
        self._account_bind_ip = (account_bind_ip or "").strip()
        # Track when startup() opened the listeners and when MAIN TCP / login
        # success first arrived. Used to annotate the "logged in as …" line with
        # the wall-clock timing so slow slots are easy to spot.
        self._startup_mono: float | None = None
        self._main_tcp_mono: float | None = None
        self._login_success_mono: float | None = None
        # Counters for the proxy-side pong reply that keeps the upstream TCP
        # alive when Flash is minimised (see ``_auto_pong_server_ping``). We log
        # the first pong at INFO and a running count every ``BOT_PROXY_HEARTBEAT_LOG_EVERY``
        # replies so the log stays readable but still shows liveness health.
        self._auto_pong_sent_main = 0
        self._auto_pong_sent_satellite = 0
        self._keepalive_sent_main = 0
        # Log the first ChangeSatelliteServer redirect at INFO with full detail;
        # subsequent ones (every JoinRoomPacket triggers a fresh redirect) drop
        # to DEBUG to avoid 14× noise on every room change.
        self._sat_redirect_logged = False
        # Last MAIN session close info, populated by ``new_main_connection``'s
        # finally block. Surfaced in the post-login slot-status table so a
        # PARTL row carries its specific cause (e.g. ``clean-eof@9.6s pong=0/1``)
        # without forcing the operator to grep the WARNING stream above.
        self._main_last_close_reason: str | None = None
        self._main_last_close_alive_sec: float | None = None
        self._main_last_close_since_login_sec: float | None = None
        # Ring buffer of the last packets seen on MAIN in each direction. Used
        # by the session-end diagnostic to reveal which side produced the final
        # packet before EOF (server kicked vs Flash walked away).
        self._main_recent_from_server: list[tuple[float, str]] = []
        self._main_recent_from_client: list[tuple[float, str]] = []
        self._MAIN_RECENT_LIMIT = _main_packet_ring_limit()
        # Per MAIN-TCP session (cleared in ``new_main_connection``): join and sat
        # migration, used in MAIN close diagnostics. ChangeSat count/mono are
        # *post-login* only — the first ChangeSatelliteServerPacket on MAIN is the
        # normal login redirect; counting it made every long session look like
        # "ChangeSat#1@60s_ago" and hid real join/migration events.
        self._main_session_login_change_sat_seen: bool = False
        self._main_session_change_sat_count: int = 0
        self._main_session_last_change_sat_mono: float | None = None
        self._main_session_join_mono: float | None = None
        self._main_session_join_name: str = ""
        # Monotonic per Flash↔proxy MAIN TCP accept (each reconnect increments). Grep ``main_tcp#7``.
        self._main_tcp_generation: int = 0
        # Per MAIN session (reset on each new_main_connection): counts for root-cause hints.
        self._main_sess_ping_srv: int = 0
        self._main_sess_pong_cli: int = 0
        self._main_sess_anticheat_cli: int = 0
        self._issue1_last_handshake_gv: str | None = None
        self._issue1_last_handshake_lss: object | None = None
        self._issue1_warned_hs_gv_mismatch_tcp_gen: int = -999_999
        self._issue1_logged_handshake_probe_tcp_gen: int = -999_999
        # Last close summary for slot-status / grep (set in new_main_connection finally).
        self._main_last_close_diag: str | None = None
        # Cumulative (optional): last join seen on this proxy process.
        self._main_last_join_room_mono: float | None = None
        self._main_last_join_room_name: str = ""
        self.register_packet_listener(self._track_main_packet, Packet)
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
        if "open_streams" not in BanBotProxy.__dict__:
            raise RuntimeError(
                "BanBotProxy must define open_streams — caseus.Proxy would connect upstream without bot binds/throttle. "
                "Update bot/ban_proxy.py from the repo."
            )

    async def open_streams(self, address, ports):
        """Connect upstream like ``caseus.Proxy.open_streams``, with global throttle + timeouts.

        Many simultaneous ``asyncio.open_connection`` calls from 10+ Flash slots often trigger
        Windows WinError 121 (semaphore timeout). ``BOT_UPSTREAM_MAX_CONCURRENT_CONNECTS`` limits
        how many handshakes run at once process-wide. Failed sweeps repeat using the same retry
        knobs as net preflight (``BOT_UPSTREAM_PROBE_RETRIES`` / ``BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC``).
        """
        port_list = list(ports)
        timeout_sec = _upstream_open_connection_timeout_sec()
        rounds = _open_streams_round_retries()
        pause_sec = _open_streams_round_pause_sec()
        failures: list[tuple[int, str, str]] = []
        sem = _upstream_connect_semaphore()
        local_bind = upstream_local_bind_tuple(account_bind_ip=self._account_bind_ip)
        trace_step(
            logger,
            "upstream_open_streams",
            "begin host=%r ports=%s rounds=%s timeout=%.1fs local_bind=%s max_conc=%s",
            address,
            port_list,
            rounds + 1,
            timeout_sec,
            local_bind,
            _parse_upstream_max_concurrent_connects(),
            slot=self.slot_label,
        )

        async def _try_port(p: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
            async with sem:
                try:
                    oc_kw: dict[str, object] = {}
                    if local_bind is not None:
                        oc_kw["local_addr"] = local_bind
                    return await asyncio.wait_for(
                        asyncio.open_connection(address, p, **oc_kw),
                        timeout=timeout_sec,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    msg = str(e).strip().replace("\n", " ")
                    if len(msg) > 180:
                        msg = msg[:177] + "..."
                    failures.append((p, type(e).__name__, msg))
                    return None

        for round_i in range(rounds + 1):
            for port in random.sample(port_list, len(port_list)):
                conn = await _try_port(port)
                if conn is not None:
                    trace_step(
                        logger,
                        "upstream_open_streams",
                        "TCP handshake OK port=%s round=%s",
                        port,
                        round_i,
                        slot=self.slot_label,
                    )
                    if round_i > 0:
                        logger.info(
                            "Slot %s: upstream TCP OK host=%r port=%s after %s extra sweep round(s)",
                            self.slot_label,
                            address,
                            port,
                            round_i,
                        )
                    return conn
            if round_i < rounds:
                trace_step(
                    logger,
                    "upstream_open_streams",
                    "sweep round %s/%s exhausted host=%r sleeping %.1fs",
                    round_i + 1,
                    rounds + 1,
                    address,
                    pause_sec,
                    slot=self.slot_label,
                )
                logger.warning(
                    "Slot %s: upstream TCP all ports failed sweep round %s/%s host=%r timeout=%.1fs "
                    "max_concurrent=%s — sleeping %.1fs then retrying",
                    self.slot_label,
                    round_i + 1,
                    rounds + 1,
                    address,
                    timeout_sec,
                    _parse_upstream_max_concurrent_connects(),
                    pause_sec,
                )
                if pause_sec > 0:
                    await asyncio.sleep(pause_sec)

        logger.warning(
            "Slot %s: upstream TCP failed every attempt host=%r ports=%s per_try=%s "
            "bind_ip(ref)=%r env_main_server_address=%r python_exe=%r — "
            "typical causes: tether/Wi‑Fi handoff, VPN drop, firewall, or Proxifier/split-routing not "
            "applied to this Python process (see README). Startup preflight does not guarantee "
            "mid-session reachability. With per-row bind_ip and Proxifier, set "
            "BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET=true or BOT_NET_PREFLIGHT_TRY_ACCOUNT_BIND_IPS=true.",
            self.slot_label,
            address,
            port_list,
            failures,
            self._account_bind_ip or "(none)",
            getattr(self, "main_server_address", None),
            sys.executable,
        )
        raise ValueError(f"Unable to connect to address '{address}' on ports {port_list}")

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
        """Reply to the server's ``PingPacket`` so the connection survives whatever Flash does.

        Three modes, controlled by env ``BOT_AUTO_PONG`` (default: ``both``):

        * ``both`` (default, also accepts ``1`` / ``true`` / ``on``): proxy fires
          ``serverbound.PongPacket`` immediately *and* forwards the ping to Flash so the
          real client also pongs. This is what the original implementation did and is the
          safest setting because it survives both ``BOT_PACKET_AUTO_LOGIN=true`` (Flash
          never enters the post-login state and therefore never pongs on its own — observed
          as ``pongs_main=0`` for every slot followed by ``clean-eof``) *and* Flash being
          partially throttled.
        * ``flash`` / ``forward`` / ``0``: do **not** proxy-pong; only forward the ping to
          Flash. Useful when Flash is fully un-throttled *and* logged in via the UI flow,
          but breaks when ``BOT_PACKET_AUTO_LOGIN`` is on (Flash never pongs).
        * ``swallow`` / ``proxy_only``: proxy ponges and swallows the ping so Flash never
          sees it. Equivalent to the legacy ``56dfddd`` behaviour. Discouraged: server may
          detect "reply present but wrong fingerprint" and still close MAIN with
          ``clean-eof`` ~10 s after its last ping.

        ``serverbound.KeepAlivePacket`` is unrelated — the server does not treat it as a
        pong, so we still need a real ``PongPacket`` to pacify the server.
        """
        raw = os.environ.get("BOT_AUTO_PONG", "").strip().lower()
        if raw in ("", "both", "1", "true", "yes", "on"):
            mode = "both"
        elif raw in ("0", "flash", "forward", "off", "no", "false"):
            mode = "flash"
        elif raw in ("swallow", "proxy", "proxy_only"):
            mode = "swallow"
        else:
            mode = "both"

        if mode == "flash":
            return self.FORWARD_PACKET

        payload = getattr(packet, "payload", 0) or 0
        is_sat = bool(getattr(source, "is_satellite", False))
        conn = "SAT" if is_sat else "MAIN"
        try:
            await source.write_packet(serverbound.PongPacket, payload=payload)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
            logger.debug(
                "Slot %s: auto-pong %s send failed (%s: %s); upstream already closing",
                self.slot_label, conn, type(e).__name__, e,
            )
            return self.DO_NOTHING

        if is_sat:
            self._auto_pong_sent_satellite += 1
            count = self._auto_pong_sent_satellite
        else:
            self._auto_pong_sent_main += 1
            count = self._auto_pong_sent_main

        if count == 1:
            # The verbose mode-explanation is logged once at CLI startup
            # ("BOT_AUTO_PONG=both — ..."); per-slot first-pong only needs the
            # facts (slot, MAIN/SAT, payload) so the operator can confirm the
            # connection is live without 14 copies of the rationale.
            logger.info(
                "Slot %s: first auto-pong %s payload=%s",
                self.slot_label, conn, payload,
            )
        elif count % _proxy_heartbeat_log_every() == 0:
            logger.info(
                "Slot %s: %s auto-pong count=%d (payload=%s) — upstream still alive",
                self.slot_label, conn, count, payload,
            )
        else:
            logger.debug(
                "Slot %s: %s auto-pong #%d payload=%s",
                self.slot_label, conn, count, payload,
            )

        if mode == "both":
            # Let the ping continue to Flash so its own pong (if any) also reaches server.
            return self.FORWARD_PACKET
        return self.DO_NOTHING

    @pak.packet_listener(clientbound.ChangeSatelliteServerPacket)
    async def _proxy_satellite_server(self, source, packet):
        """Send Flash only 127.0.0.1 + local satellite port (never the public game host)."""
        if packet.should_ignore:
            return self.FORWARD_PACKET

        if not getattr(source, "is_satellite", False):
            if not self._main_session_login_change_sat_seen:
                self._main_session_login_change_sat_seen = True
            else:
                self._main_session_change_sat_count += 1
                self._main_session_last_change_sat_mono = time.monotonic()

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
        # Only log the first SAT redirect at INFO with full detail (the public
        # host + ports are useful exactly once for diagnosis). Subsequent
        # redirects (which happen on every JoinRoomPacket) are DEBUG so the
        # ban round doesn't pollute the console.
        srv_addr = getattr(packet, "address", None)
        if not getattr(self, "_sat_redirect_logged", False):
            logger.info(
                "Slot %s: SAT redirect → Flash uses 127.0.0.1:%s (server sent %r ports %s)",
                self.slot_label, self.host_satellite_port, srv_addr,
                getattr(packet, "ports", None),
            )
            self._sat_redirect_logged = True
        else:
            logger.debug(
                "Slot %s: SAT redirect → 127.0.0.1:%s (server %r)",
                self.slot_label, self.host_satellite_port, srv_addr,
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

    async def _track_main_packet(self, source, packet):
        """Remember the last few packet types on each MAIN direction for diagnostics."""
        if getattr(source, "is_satellite", False):
            return
        label = _packet_diag_label(packet)
        now = time.monotonic()
        # Servers send clientbound packets; caseus delivers them via the
        # ServerConnection source. ClientConnections deliver serverbound.
        if isinstance(packet, ServerboundPacket):
            buf = self._main_recent_from_client
            tn = type(packet).__name__
            if tn == "PongPacket":
                self._main_sess_pong_cli += 1
            elif "Anticheat" in tn:
                self._main_sess_anticheat_cli += 1
        else:
            buf = self._main_recent_from_server
            if type(packet).__name__ == "PingPacket":
                self._main_sess_ping_srv += 1
        buf.append((now, label))
        if len(buf) > self._MAIN_RECENT_LIMIT:
            del buf[: len(buf) - self._MAIN_RECENT_LIMIT]

    async def _log_all_main_packet(self, source, packet):
        if not self._log_all_main_packets:
            return
        if getattr(source, "is_satellite", False):
            return
        tn = type(packet).__name__
        if tn in ("KeepAlivePacket", "IPSPingPacket", "PingPacket", "PongPacket"):
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
        sat = getattr(source, "is_satellite", False)
        if not sat and self._main_handshake_mono is None:
            self._main_handshake_mono = time.monotonic()
        if not sat:
            gv = getattr(packet, "game_version", None)
            self._issue1_last_handshake_gv = None if gv is None else str(gv)
            self._issue1_last_handshake_lss = getattr(packet, "loader_stage_size", None)
            env_gvs = (os.environ.get("TFM_SECRETS_GAME_VERSION") or "").strip()
            tcpg_hs = getattr(self, "_main_tcp_generation", -1)
            ok_match_hs: bool | None = None
            if env_gvs and gv is not None:
                try:
                    fv = int(str(gv).strip(), 0)
                    ok_match_hs = fv == int(env_gvs, 0)
                except ValueError:
                    ok_match_hs = str(gv).strip() == env_gvs
                if not ok_match_hs and tcpg_hs != self._issue1_warned_hs_gv_mismatch_tcp_gen:
                    logger.warning(
                        "ISSUE1_HANDSHAKE_GV_MISMATCH slot=%s main_tcp#=%s TFM_SECRETS_GAME_VERSION=%r "
                        "HandshakePacket.game_version=%r loader_stage_size=%s "
                        "| confirm secrets dump + loader SWF from the same Transformice release (expect "
                        "PARTL/generic AS errors if handshake crypto disagrees).",
                        self.slot_label,
                        tcpg_hs,
                        env_gvs,
                        gv,
                        getattr(packet, "loader_stage_size", None),
                    )
                    self._issue1_warned_hs_gv_mismatch_tcp_gen = tcpg_hs
            if (
                _issue1_handshake_probe_enabled()
                and tcpg_hs != getattr(self, "_issue1_logged_handshake_probe_tcp_gen", -999_999)
            ):
                self._issue1_logged_handshake_probe_tcp_gen = tcpg_hs
                if ok_match_hs is None:
                    match_s = "n/a"
                else:
                    match_s = "yes" if ok_match_hs else "no"
                logger.info(
                    "ISSUE1_HANDSHAKE_PROBE slot=%s main_tcp#=%s HandshakePacket.game_version=%r "
                    "TFM_SECRETS_GAME_VERSION=%r match=%s loader_stage_size=%s",
                    self.slot_label,
                    tcpg_hs,
                    gv,
                    env_gvs or None,
                    match_s,
                    getattr(packet, "loader_stage_size", None),
                )
            try:
                from .issue1_forensic import maybe_warn_handshake_mismatch

                maybe_warn_handshake_mismatch(
                    self,
                    flash_game_version=self._issue1_last_handshake_gv,
                    loader_stage_size=self._issue1_last_handshake_lss,
                )
            except Exception:
                logger.debug("ISSUE1_HANDSHAKE hook failed", exc_info=True)
        if not self._verbose_login_flow:
            return
        conn = "SAT" if sat else "MAIN"
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
        self._startup_mono = time.monotonic()
        bind = self.host_address
        bind_s = bind if bind is not None else "*"

        def _describe(srv) -> str:
            """Return a compact family summary for every socket a listener owns."""
            socks = getattr(srv, "sockets", None) or []
            fams: list[str] = []
            for s in socks:
                try:
                    sa = s.getsockname()
                except OSError:
                    continue
                host = sa[0] if sa else "?"
                if host in ("0.0.0.0", "::"):
                    fams.append("IPv6" if ":" in host else "IPv4")
                else:
                    fams.append(str(host))
            return "+".join(fams) or "(no sockets)"

        # The CLI already logs ``Slot N: listen_host=... main=X satellite=Y`` for
        # every slot before this proxy runs; this line is the bind-family detail
        # (IPv4/IPv6) which is rarely interesting in production. DEBUG by default;
        # promote with ``BOT_PROXY_LOG_BIND_DETAIL=1`` if you need it.
        _bind_lvl = (
            logger.info
            if os.environ.get("BOT_PROXY_LOG_BIND_DETAIL", "").strip().lower()
                in ("1", "true", "yes", "on")
            else logger.debug
        )
        _bind_lvl(
            "Slot %s listening on bind=%s main=%s (%s) satellite=%s (%s)",
            self.slot_label,
            bind_s,
            self.host_main_port,
            _describe(self.main_srv),
            self.host_satellite_port,
            _describe(self.satellite_srv),
        )

    async def reset_packet_auto_login_for_reconnect(self) -> None:
        """
        Clear per-MAIN-session PACKET_AUTO_LOGIN state before relaunching Flash (PARTL retry).

        After a successful auto-login, ``_packet_login_sent`` stays True. A new Flash
        process reconnects to the same proxy port; without resetting, the post-sysinfo
        job sees the flag and skips :class:`serverbound.LoginPacket` — the operator
        then gets MAIN TCP + ``flash_main_tcp_seen`` but no LoginSuccess until timeout.
        """
        t = self._packet_login_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._packet_login_task = None
        self._packet_login_sent = False
        self._handshake_auth_token = None
        self._main_handshake_mono = None
        # Allow FLASH main_tcp UI hook to run again on the next first MAIN (non-packet path).
        self._first_main_hook_done = False

    def _tcp_close_side_guess(self) -> str:
        """
        Heuristic label for *which side likely initiated TCP teardown* (not proof).
        Pairs with ``last_io=`` on the same diagnostic line.
        """
        li = self._last_main_io_hint()
        if "server_newest" in li:
            return "server_last_packet_newest_infer_server_closed_or_idle_kick"
        if "client_newest" in li:
            return "client_last_packet_newest_infer_flash_closed_or_AS_crash"
        if "server_only" in li:
            return "only_server_samples_unusual"
        if "client_only" in li:
            return "only_client_samples_unusual"
        if "tie" in li:
            return "timestamps_tie_use_srv_cli_buffers"
        if "unknown" in li:
            return "no_packet_samples"
        return "ambiguous"

    def _last_main_io_hint(self) -> str:
        """Which direction saw the newest MAIN packet before close (heuristic for who went quiet last)."""
        srv = self._main_recent_from_server
        cli = self._main_recent_from_client
        if not srv and not cli:
            return "last_io=unknown(no_packets)"
        ts_s = max((t for t, _ in srv), default=None)
        ts_c = max((t for t, _ in cli), default=None)
        if ts_s is None:
            return "last_io=client_only"
        if ts_c is None:
            return "last_io=server_only"
        if ts_s > ts_c:
            return "last_io=server_newest(%.2fs_after_client)" % (ts_s - ts_c)
        if ts_c > ts_s:
            return "last_io=client_newest(%.2fs_after_server)" % (ts_c - ts_s)
        return "last_io=tie"

    def _format_main_close_diagnostic(
        self,
        *,
        close_reason: str,
        alive_sec: float,
        since_login: float | None,
        now_mono: float,
    ) -> str:
        """
        Grep-friendly single line to narrow *why* MAIN dropped (heuristic, not proof).

        Uses per-session ring buffers and join / ChangeSatellite counters; buffers are
        cleared on each new MAIN TCP in ``new_main_connection``.  ``clean-eof`` appends
        a short ``note=`` line: login batch vs room-phase tuning (``PRE_BAN`` is for room
        join, not sequential Flash opens)."""
        parts: list[str] = []
        parts.append("main_tcp#=%d" % getattr(self, "_main_tcp_generation", 0))
        parts.append("phase=%s" % get_operator_phase())
        parts.append(self._last_main_io_hint())
        srv = [n for _, n in self._main_recent_from_server]
        cli = [n for _, n in self._main_recent_from_client]

        jm = self._main_session_join_mono
        jn = (self._main_session_join_name or "").strip()
        if jm is not None and jn:
            parts.append("join_%.1fs_ago->%r" % (now_mono - jm, jn[:40]))

        csm = self._main_session_last_change_sat_mono
        ncs = self._main_session_change_sat_count
        if csm is not None and ncs > 0:
            parts.append("ChangeSat#%d@%.1fs_ago" % (ncs, now_mono - csm))

        cli_j = " ".join(cli[-4:])
        any_change_sat_in_srv4 = any(_diag_label_looks_change_sat(x) for x in srv[-4:])
        if any_change_sat_in_srv4 and jm is not None and (now_mono - jm) < 45.0:
            parts.append("pattern=ChangeSat_%.1fs_after_JoinRoom" % (now_mono - jm))
        if "JoinRoom" in cli_j and any_change_sat_in_srv4:
            parts.append("pattern=JoinRoomChain_then_ChangeSat")

        if close_reason != "clean-eof":
            parts.append("close=%s" % close_reason)
        if (
            self._auto_pong_sent_main == 0
            and since_login is not None
            and since_login > 3.0
            and any("PingPacket" in x for x in srv[-2:])
        ):
            parts.append("suspect=no_proxy_pong_on_MAIN(check_BOT_AUTO_PONG)")

        if alive_sec < 12.0 and since_login is not None and since_login < 20.0:
            parts.append("short_post_login_life=%.1fs" % alive_sec)

        if close_reason == "clean-eof":
            parts.append(
                "note=clean_eof:login_often=FLASH_STAGGER+AS;room_often=PRE_BAN+leader_not_for_login_batch"
            )

        rc_on = (
            os.environ.get("BOT_PROXY_MAIN_RC_HINT", "1").strip().lower()
            not in ("0", "false", "no", "off", "")
        )
        if rc_on:
            parts.append("tcp_side_guess=%s" % self._tcp_close_side_guess())
            parts.append(
                "sess_counts=srv_ping:%d cli_pong:%d cli_anticheat_like:%d "
                "proxy_pong_SENT_main:%d keepalive_SENT_main:%d"
                % (
                    getattr(self, "_main_sess_ping_srv", 0),
                    getattr(self, "_main_sess_pong_cli", 0),
                    getattr(self, "_main_sess_anticheat_cli", 0),
                    getattr(self, "_auto_pong_sent_main", 0),
                    getattr(self, "_keepalive_sent_main", 0),
                ),
            )
            if (
                getattr(self, "_packet_auto_login", False)
                and since_login is not None
                and since_login >= 5.0
                and getattr(self, "_main_sess_ping_srv", 0) >= 2
                and getattr(self, "_auto_pong_sent_main", 0) == 0
            ):
                parts.append(
                    "rc_CRITICAL=packets_auto_login_but_zero_proxy_PONG_check_BOT_AUTO_PONG_MAIN"
                )
            if getattr(self, "_main_sess_anticheat_cli", 0) > 120 and alive_sec < 180:
                parts.append("rc_note=heavy_anticheat_traffic_seen_in_other_logs_to_match_AC_kicks")

        try:
            from .flash_launch import (
                issue1_as_main_correlation_window_sec,
                last_as_dismiss_detail_for_slot,
                last_as_dismiss_monotonic_for_slot,
            )

            win_i1 = issue1_as_main_correlation_window_sec()
            adm = last_as_dismiss_monotonic_for_slot(self.slot_label)
            if win_i1 > 0 and adm is not None:
                dt_disp = now_mono - adm
                if dt_disp <= win_i1:
                    det = (last_as_dismiss_detail_for_slot(self.slot_label) or "").strip()
                    if det:
                        tail = det if len(det) <= 260 else det[:257] + "…"
                        parts.append("issue1_near_as_dismiss=%.2fs|%s" % (dt_disp, tail))
                    else:
                        parts.append("issue1_near_as_dismiss=%.2fs|no_tail" % dt_disp)
                    cue_ln = dismiss_tail_issue1_cues(det)
                    if cue_ln:
                        parts.append(cue_ln)
                        if close_reason == "clean-eof":
                            if "WRONG_VERSION_HINT_IN_AS_BODY" in cue_ln:
                                parts.append(
                                    "ISSUE1_PRIMARY_SUSPECT=STALE_GAME_VERSION_OR_LOADER_MISMATCH"
                                )
                            elif "BM_CLICK_CONTINUE_RANK" in cue_ln:
                                parts.append(
                                    "ISSUE1_PRIMARY_SUSPECT=CONTINUE_BMCLICK_AS_SESSION_TEARDOWN"
                                )
        except Exception:
            pass

        try:
            from .tfm_loader_alignment import (
                get_last_alignment_summary,
                issue1_alignment_log_fragment,
            )

            al_frag = issue1_alignment_log_fragment(get_last_alignment_summary())
            if al_frag:
                parts.append(al_frag)
        except Exception:
            pass

        joined = "|".join(parts)
        if (
            close_reason == "clean-eof"
            and "ISSUE1_PRIMARY_SUSPECT=" not in joined
            and "pattern=ChangeSat_" in joined
            and "_after_JoinRoom" in joined
        ):
            parts.append("ISSUE1_PRIMARY_SUSPECT=SAT_REDIRECT_NEAR_MAIN_EOF")
        return " | ".join(parts)

    async def new_main_connection(self, client_reader, client_writer):
        peer = None
        try:
            if client_writer.transport is not None:
                peer = client_writer.transport.get_extra_info("peername")
        except Exception:
            pass
        if self._main_tcp_mono is None:
            self._main_tcp_mono = time.monotonic()
        since_startup = (
            time.monotonic() - self._startup_mono if self._startup_mono is not None else None
        )
        logger.info(
            "Slot %s: MAIN TCP accept from %r (proxy port %s, +%.2fs after listen) "
            "main_tcp#=%d — game/loader reached this slot",
            self.slot_label,
            peer,
            self.host_main_port,
            since_startup if since_startup is not None else 0.0,
            getattr(self, "_main_tcp_generation", 0),
        )
        trace_step(
            logger,
            "main_tcp",
            "accepted peer=%r main_tcp#(current)=%s MAIN_listen_port=%s",
            peer,
            getattr(self, "_main_tcp_generation", 0),
            self.host_main_port,
            slot=self.slot_label,
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
        #
        # Also annotate how long the MAIN session survived and which side tore it down —
        # vital for diagnosing the "login ok but upstream closed" failure mode. The two
        # likely culprits (Flash watchdog vs TFM idle-kick) leave different fingerprints:
        #   • Flash-side close  -> logged at/near the time Flash's socket timer fires;
        #     client reader EOFs first, no server-side exception.
        #   • Server-side close -> ConnectionResetError/EOF from the upstream socket first;
        #     caseus propagates via destination.close(), so the client task also ends.
        tcp_accept_mono = time.monotonic()
        ab = self._login_aborted_event
        if ab is not None:
            ab.clear()
        # Isolate ring buffers and join / ChangeSatellite counters to this MAIN TCP only
        # (avoids mis-attributing the previous Flash session's packets in diagnostics).
        self._main_recent_from_server.clear()
        self._main_recent_from_client.clear()
        self._main_session_login_change_sat_seen = False
        self._main_session_change_sat_count = 0
        self._main_session_last_change_sat_mono = None
        self._main_session_join_mono = None
        self._main_session_join_name = ""
        self._main_tcp_generation += 1
        self._main_sess_ping_srv = 0
        self._main_sess_pong_cli = 0
        self._main_sess_anticheat_cli = 0
        self._issue1_last_handshake_gv = None
        self._issue1_last_handshake_lss = None
        close_reason = "clean-eof"
        close_exc: BaseException | None = None
        trace_step(
            logger,
            "main_tcp",
            "await super().new_main_connection (caseus listen); tcp_generation=%s",
            getattr(self, "_main_tcp_generation", 0),
            slot=self.slot_label,
        )
        try:
            await super().new_main_connection(client_reader, client_writer)
        except ValueError as e:
            trace_step(
                logger,
                "main_tcp",
                "ValueError (upstream open_streams exhaustion): %s",
                e,
                slot=self.slot_label,
            )
            # Upstream connect exhaustion (see ``open_streams``) — already logged per-port.
            close_reason = "upstream-tcp-all-ports-failed"
            close_exc = e
            hint_os = ""
            if sys.platform == "win32":
                hint_os = (
                    " On Windows, confirm Wi‑Fi/power savings off for the active adapter and that "
                    "Proxifier targets this python.exe."
                )
            logger.warning(
                "Slot %s: MAIN ended with %s (%s). "
                "See prior WARNING lines for per-port errors; grep upstream-tcp-all-ports-failed.%s",
                self.slot_label,
                close_reason,
                e,
                hint_os,
            )
            if (
                os.environ.get("BOT_PROXY_UPSTREAM_FAIL_TRACE", "").strip().lower()
                in ("1", "true", "yes", "on", "debug")
            ):
                logger.exception(
                    "Slot %s: BOT_PROXY_UPSTREAM_FAIL_TRACE enabled — full traceback",
                    self.slot_label,
                )
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
            close_reason = f"{type(e).__name__}"
            close_exc = e
            logger.debug(
                "Slot %s: main connection OS-level error (%s: %s)",
                self.slot_label, type(e).__name__, e,
            )
            if sys.platform == "win32" and getattr(e, "winerror", None) == 121:
                logger.warning(
                    "Slot %s: WinError 121 (semaphore timeout) — often Wi‑Fi/USB tether overload or TCP "
                    "stack contention; try fewer simultaneous Flash slots, higher stagger, or a stable uplink.",
                    self.slot_label,
                )
        except Exception as e:  # noqa: BLE001
            close_reason = f"unhandled:{type(e).__name__}"
            close_exc = e
            trace_step(
                logger,
                "main_tcp",
                "unhandled exception type=%s msg=%s",
                type(e).__name__,
                e,
                slot=self.slot_label,
            )
            logger.exception(
                "Slot %s: main connection listener raised unhandled exception",
                self.slot_label,
            )
        finally:
            alive_sec = time.monotonic() - tcp_accept_mono
            since_login = (
                time.monotonic() - self._login_success_mono
                if self._login_success_mono is not None
                else None
            )
            # Surface the last-close summary on the proxy itself so the
            # post-login slot-status table can include it on PARTL rows.
            self._main_last_close_reason = close_reason
            self._main_last_close_alive_sec = alive_sec
            self._main_last_close_since_login_sec = since_login
            # A post-login close that happens within the first few minutes of login is
            # the failure mode we are hunting — log it at WARNING so it is easy to spot.
            lvl = logger.warning if (since_login is not None and since_login < 600) else logger.info
            def _fmt_recent(buf: list[tuple[float, str]]) -> str:
                if not buf:
                    return "(none)"
                now_local = time.monotonic()
                return ",".join(f"-{now_local - ts:.1f}s:{name}" for ts, name in buf)

            lvl(
                "Slot %s: MAIN session ended alive=%.1fs login+%ss reason=%s main_tcp#=%d "
                "(pongs_main=%d pongs_sat=%d ka=%d main_clients_now=%d sat_clients_now=%d)%s "
                "operator_phase=%s srv→last=[%s] cli→last=[%s]",
                self.slot_label,
                alive_sec,
                f"{since_login:.1f}" if since_login is not None else "n/a",
                close_reason,
                getattr(self, "_main_tcp_generation", 0),
                self._auto_pong_sent_main,
                self._auto_pong_sent_satellite,
                self._keepalive_sent_main,
                len(self.main_clients or []),
                len(getattr(self, "satellite_clients", None) or []),
                f" exc={close_exc!r}" if close_exc is not None else "",
                get_operator_phase(),
                _fmt_recent(self._main_recent_from_server),
                _fmt_recent(self._main_recent_from_client),
            )
            now_close = time.monotonic()
            diag = self._format_main_close_diagnostic(
                close_reason=close_reason,
                alive_sec=alive_sec,
                since_login=since_login,
                now_mono=now_close,
            )
            self._main_last_close_diag = diag
            ev = self._login_success_event
            ab_ev = self._login_aborted_event
            if ev is not None and ab_ev is not None and not ev.is_set():
                ab_ev.set()
            _diag_off = os.environ.get("BOT_PROXY_MAIN_CLOSE_DIAG", "").strip().lower() in (
                "0",
                "false",
                "no",
                "off",
            )
            if not _diag_off:
                lvl(
                    "Slot %s: MAIN close diagnostic: %s",
                    self.slot_label,
                    diag,
                )
            need_rc = _env_truthy("BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE")
            need_i1 = _env_truthy("BOT_ISSUE1_FORENSIC")
            if need_rc or need_i1:
                from .flash_launch import (
                    last_as_dismiss_detail_for_slot,
                    last_as_dismiss_monotonic_for_slot,
                )

                now_p = time.monotonic()
                adm = last_as_dismiss_monotonic_for_slot(self.slot_label)
                det = last_as_dismiss_detail_for_slot(self.slot_label)
                det_trim = det.strip() if det else ""
                det_s = (det_trim[:400] + ("…" if len(det_trim) > 400 else "")) if det_trim else ""
                if adm is None:
                    as_part = "as_dismiss_never_this_slot"
                elif det_s:
                    as_part = f"sec_since_as_dismiss={now_p - adm:.4f} dismiss_tail={det_s}"
                else:
                    as_part = f"sec_since_as_dismiss={now_p - adm:.4f}"
                cues_root = dismiss_tail_issue1_cues(det_trim)
                if cues_root:
                    as_part = f"{as_part} | {cues_root}"
                    if (
                        close_reason == "clean-eof"
                        and "WRONG_VERSION_HINT_IN_AS_BODY" in cues_root
                    ):
                        as_part += " | ISSUE1_PRIMARY_SUSPECT=STALE_GAME_VERSION_OR_LOADER_MISMATCH"
                    elif (
                        close_reason == "clean-eof"
                        and "BM_CLICK_CONTINUE_RANK" in cues_root
                    ):
                        as_part += " | ISSUE1_PRIMARY_SUSPECT=CONTINUE_BMCLICK_AS_SESSION_TEARDOWN"
                try:
                    from .tfm_loader_alignment import (
                        get_last_alignment_summary,
                        issue1_alignment_log_fragment,
                    )

                    al_rc = issue1_alignment_log_fragment(get_last_alignment_summary())
                    if al_rc:
                        as_part = f"{as_part} | {al_rc}"
                except Exception:
                    pass
                flash_snap = _root_cause_flash_tcp_snapshot(client_writer)
                if need_rc:
                    logger.warning(
                        "ROOT_CAUSE_MAIN_CLOSE slot=%s main_tcp#=%s phase=%s reason=%s login_age=%s %s | "
                        "%s | %s | %s",
                        self.slot_label,
                        getattr(self, "_main_tcp_generation", 0),
                        get_operator_phase(),
                        close_reason,
                        f"{since_login:.3f}s" if since_login is not None else "n/a",
                        _root_cause_exc_fields(close_exc),
                        flash_snap,
                        _main_ring_timeline(
                            self._main_recent_from_server, now_mono=now_p, tag="srv_ring"
                        ),
                        _main_ring_timeline(
                            self._main_recent_from_client, now_mono=now_p, tag="cli_ring"
                        ),
                        as_part,
                    )
                if need_i1:
                    try:
                        from .issue1_forensic import log_main_teardown_banner

                        log_main_teardown_banner(
                            self,
                            close_reason=close_reason,
                            alive_sec=alive_sec,
                            since_login=since_login,
                            close_exc=close_exc,
                            diag=diag,
                            flash_tcp_snapshot=flash_snap,
                            as_since_dismiss=as_part,
                            packet_login_sent=bool(getattr(self, "_packet_login_sent", False)),
                        )
                    except Exception:
                        logger.debug("ISSUE1_MAIN_CLOSE banner failed", exc_info=True)
            if os.environ.get("BOT_PROXY_MAIN_CLOSE_VERBOSE", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                logger.debug(
                    "Slot %s: MAIN close verbose srv_order=%s cli_order=%s",
                    self.slot_label,
                    [n for _, n in self._main_recent_from_server],
                    [n for _, n in self._main_recent_from_client],
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
            logger.warning(
                "Slot %s: join_room — no upstream write path (MAIN or satellite)",
                self.slot_label,
            )
            return False
        name = room_name.strip()
        jm = time.monotonic()
        self._main_last_join_room_mono = jm
        self._main_last_join_room_name = name
        self._main_session_join_mono = jm
        self._main_session_join_name = name
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
        if os.environ.get("BOT_ROOM_LIST_TRACE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            logger.info(
                "Slot %s: RoomListPacket sent (game_mode=%s) operator_phase=%s",
                self.slot_label,
                game_mode_int,
                get_operator_phase(),
            )
        return True

    async def send_ban_command(self, nickname: str) -> bool:
        """Send /ban nickname#tag."""
        main_conn = self._main_write_conn()
        n_main = len(self.main_clients or [])
        n_sat = len(getattr(self, "satellite_clients", None) or [])
        if main_conn is None:
            hint = getattr(self, "_main_last_close_diag", None) or getattr(
                self, "_main_last_close_reason", None
            )
            extra = f" last_main_close={hint!r}" if hint else ""
            logger.error(
                "Slot %s: cannot send /ban — no upstream connection "
                "(main_clients=%d satellite_clients=%d)%s",
                self.slot_label, n_main, n_sat, extra,
            )
            return False
        if self.main_clients:
            conn_type = "main"
        elif getattr(self, "satellite_clients", None):
            conn_type = f"satellite[-1] (main_clients=0, {n_sat} sat)"
        else:
            conn_type = "unknown"
        target = normalize_nickname_tag(nickname)
        cmd = f"ban {target}"
        t0 = time.monotonic()
        try:
            await main_conn.write_packet_instance(serverbound.CommandPacket(command=cmd))
            dt = (time.monotonic() - t0) * 1000.0
            logger.info(
                "Slot %s: /ban %s sent via %s in %.1fms (as nick=%r)",
                self.slot_label, target, conn_type, dt, self._own_username or "?",
            )
            return True
        except Exception as e:
            logger.exception(
                "Slot %s: /ban %s failed via %s: %s",
                self.slot_label, target, conn_type, e,
            )
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
        # First keepalive: the loop used to sleep the full interval before the first
        # KeepAlivePacket. Idle MAIN can be closed by the game server in that window.
        _first_delay = min(4.0, max(0.4, float(interval) * 0.25))
        try:
            while True:
                await asyncio.sleep(_first_delay if sent == 0 else interval)
                conn = None
                if self.main_clients:
                    conn = self.main_clients[0].destination
                if conn is None:
                    logger.warning(
                        "Slot %s: main-keepalive loop exiting — MAIN upstream gone "
                        "(sent=%d pongs_main=%d pongs_sat=%d)",
                        self.slot_label, sent,
                        self._auto_pong_sent_main, self._auto_pong_sent_satellite,
                    )
                    return
                try:
                    await conn.write_packet(serverbound.KeepAlivePacket)
                    sent += 1
                    self._keepalive_sent_main = sent
                    # First keepalive is interesting; then only every N to avoid noise.
                    if sent == 1:
                        # Already logged "main-keepalive loop started (interval=..)" at
                        # INFO before the first await. Demoting this confirmation to DEBUG
                        # keeps the log to one line per slot's keepalive lifecycle.
                        logger.debug(
                            "Slot %s: first main-keepalive sent (interval=%.1fs)",
                            self.slot_label, interval,
                        )
                    elif sent % _proxy_heartbeat_log_every() == 0:
                        logger.info(
                            "Slot %s: main-keepalive count=%d (pongs_main=%d pongs_sat=%d) — MAIN alive",
                            self.slot_label, sent,
                            self._auto_pong_sent_main, self._auto_pong_sent_satellite,
                        )
                    else:
                        logger.debug(
                            "Slot %s: main-keepalive #%d", self.slot_label, sent,
                        )
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                    logger.warning(
                        "Slot %s: main-keepalive send failed (%s: %s); stopping loop "
                        "(sent=%d pongs_main=%d)",
                        self.slot_label, type(e).__name__, e, sent,
                        self._auto_pong_sent_main,
                    )
                    return
                except Exception:
                    logger.exception(
                        "Slot %s: main-keepalive unexpected error; stopping loop (sent=%d)",
                        self.slot_label, sent,
                    )
                    return
        except asyncio.CancelledError:
            logger.debug(
                "Slot %s: main-keepalive loop cancelled (sent=%d)",
                self.slot_label, sent,
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
        self._login_success_mono = time.monotonic()
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
        # Timing breakdown (if we have it): how long Flash took to reach us and
        # how long the handshake/login round-trip took once connected.
        timing = ""
        if self._main_tcp_mono is not None and self._startup_mono is not None:
            tcp_after = self._main_tcp_mono - self._startup_mono
            login_after = self._login_success_mono - self._main_tcp_mono
            timing = f" (tcp +{tcp_after:.1f}s, login +{login_after:.1f}s)"
        msg = f"OK  [slot {self.slot_label}] logged in as {user}{timing}"
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
