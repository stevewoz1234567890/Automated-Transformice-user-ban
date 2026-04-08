# Transformice login flow (this repository)

This document describes how **this project** interacts with Transformice login. It does **not** include the proprietary game client source (`Transformice.exe` / SWF internals).

## Scope

| Component | Role |
|-----------|------|
| **`Transformice.exe` / SWF** | Official client; login UI and password handling live inside the binary. |
| **`caseus` (Python)** | Parses and forwards the game’s TCP protocol (`HandshakePacket`, `LoginPacket`, `LoginSuccessPacket`, etc.). |
| **`bot/ban_proxy.py`** | Local proxy (`BanBotProxy`): logs login-phase traffic, rewrites `ChangeSatelliteServerPacket` to keep Flash on `127.0.0.1`, signals success on `LoginSuccessPacket`. |
| **`bot/flash_launch.py`** | Launches Flash + `TFMProxyLoader.swf`, optional UI automation (`run_flash_login_ui`) via Windows input. |
| **`bot/ban_cli.py`** | Multi-slot orchestration: proxies, sequential Flash launch, waits for `LoginSuccess` per slot. |

## Network sequence (proxy view)

1. Client opens **MAIN TCP** to the slot’s proxy port.
2. **Client → server:** `HandshakePacket` (version, loader info, etc.).
3. **Server → client:** `HandshakeResponsePacket` (includes `auth_token` in verbose logs).
4. **Client → server:** `LoginPacket` — username, `login_method`, `start_room`, **`password_hash`** (never logged as raw bytes in this bot; length-only metadata).
5. **Success:** `LoginSuccessPacket` — session id, username, etc. The proxy sets `login_success_event` so the CLI can open the next client.

Failure paths commonly seen in logs: `AccountErrorPacket`, `CaptchaPacket`, `ChangeSatelliteServerPacket` (satellite redirect; the proxy forces local ports to avoid Flash sandbox errors on public hosts).

## Flash UI automation (`FLASH_AUTO_LOGIN_UI`)

When enabled, the bot dismisses common dialogs and pastes or types credentials into the Flash window. Triggers:

- **`main_tcp`** (default): runs after the **first MAIN TCP accept** for the slot (game reached the proxy).
- **`after_launch`**: fixed delay from Flash process start (see `FLASH_LOGIN_AFTER_LAUNCH_SEC`).

Relevant settings live in `bot/config.py` (`FLASH_LOGIN_*`, `PROXY_VERBOSE_LOGIN_FLOW`, etc.). Per-slot credentials come from `ACCOUNTS` rows (`username` / `password`) for clipboard/UI automation only.

## Race condition fixed in `main_tcp` mode

The proxy runs in a **background thread** while `launch_one_flash_loader` runs on the **main thread** (wait for HWND, delay, click “Transformice”). MAIN TCP can therefore be accepted **before** the loader finishes. Without coordination, auto-login could dismiss or type **before** the login screen exists.

The bot now waits on **`flash_loader_ready_event`** until `launch_one_flash_loader` returns for that slot, then runs the `main_tcp` hook logic. If Flash is not auto-launched (`--no-launch-flash`), all slots get the event set after proxies start so manual launch does not deadlock the hook.

Optional timeout: **`FLASH_LOGIN_WAIT_LOADER_READY_SEC`** in `bot/config.py` (default 180; clamped 5–600 in code).

## Periodic error dismiss vs auto-login

If **`FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC`** is greater than zero, a background thread runs the same dismiss clicks as auto-login. That used to **overlap** `run_flash_login_ui` and steal focus, so **`LoginPacket` never appeared**. The bot now holds a **per-Flash-PID lock** during `run_flash_login_ui` so the poll **skips** while auto-login runs, and the poll **stops** after `LoginSuccess`. Prefer **`FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC = 0`** when using `FLASH_AUTO_LOGIN_UI` unless you still see sandbox errors after login.

**Dismiss vs main window:** The standalone Flash title bar is also “Adobe Flash Player”, so the old logic treated the **main game** as an error dialog and clicked dismiss fractions on the **login screen**, breaking login. Dismiss taps now apply only to **small** top-level windows (see **`FLASH_DISMISS_MAX_DIALOG_CLIENT_AREA`** in `bot/config.py`).

## Packet-based auto-login (`PACKET_AUTO_LOGIN`)

Yes: the proxy can send **`LoginPacket`** on the main TCP connection after **`SystemInformationPacket`**, using the same **SHAKikoo** password hash as the real client (`caseus.util.crypto.shakikoo`) and **`ciphered_auth_token`** from **`HandshakeResponsePacket.auth_token`** XOR **`secrets.auth_key`**.

Enable **`PACKET_AUTO_LOGIN = True`** in `bot/config.py`. That **turns off** `FLASH_AUTO_LOGIN_UI` for the run. The bot fills **`LoginPacket.loader_url`** from the same patched loader URL as Flash (`loader_document_url_for_row`). If the client later sends a duplicate **`LoginPacket`**, it is dropped so the server does not see two logins.

This avoids fragile mouse/clipboard automation but still depends on the live game (captcha, account errors, bans). Use only where you are allowed to automate.

## References

- `bot/ban_proxy.py` — packet listeners, `LoginSuccessPacket`, satellite rewrite.
- `bot/flash_launch.py` — `run_flash_login_ui`, HWND helpers, clipboard paste.
- `bot/ban_cli.py` — slot state, `FLASH_LOGIN_TRIGGER`, loader-ready synchronization.
