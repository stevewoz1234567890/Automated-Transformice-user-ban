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
from pathlib import Path

import pak
from caseus import Proxy
from caseus.packets import clientbound, serverbound

logger = logging.getLogger(__name__)

_print_lock = threading.Lock()


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def project_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_flash_trust_config() -> None:
    """Windows Flash Player trust for TFMProxyLoader (same idea as transformice-bot)."""
    if sys.platform != "win32":
        return
    appdata = os.environ.get("APPDATA")
    if not appdata:
        logger.warning("APPDATA not set; skipping Flash trust cfg")
        return
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

    bot_root = project_root_dir().resolve()
    if bot_root not in seen:
        seen.add(bot_root)
        trusted.append(bot_root)

    text = "\r\n".join(str(p) for p in trusted) + "\r\n"
    try:
        trust_dir.mkdir(parents=True, exist_ok=True)
        if cfg_path.is_file():
            cfg_path.unlink()
        cfg_path.write_text(text, encoding="utf-8")
        logger.info("Flash trust cfg written: %s", cfg_path)
    except OSError as e:
        logger.warning("Could not write Flash trust cfg: %s", e)


def normalize_nickname_tag(nickname: str) -> str:
    n = nickname.strip()
    if "#" not in n:
        return f"{n}#0000"
    return n


class BanBotProxy(Proxy):
    """One proxy port ↔ one game instance; sends slash-commands as CommandPacket (no leading /)."""

    def __init__(self, *, slot_label: str = "", **kwargs):
        super().__init__(**kwargs)
        self.slot_label = slot_label
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_username: str | None = None

    async def startup(self):
        ensure_flash_trust_config()
        self.main_srv = await self.open_main_server()
        self.satellite_srv = await self.open_satellite_server()
        if self.host_socket_policy_port is not None:
            self.socket_policy_srv = await self.open_socket_policy_server()
        logger.info(
            "Slot %s listening main=%s satellite=%s",
            self.slot_label,
            self.host_main_port,
            self.host_satellite_port,
        )

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
        _safe_print(f"OK  [slot {self.slot_label}] logged in as {user}")

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
