# Transformice login flow (this repository)

This document describes how **this project** interacts with Transformice login. It does **not** include the proprietary game client source (`Transformice.exe` / SWF internals).

## Scope

| Component | Role |
|-----------|------|
| **`Transformice.exe` / SWF** | Official client; login UI and password handling live inside the binary. |
| **`caseus` (Python)** | Parses and forwards the game’s TCP protocol (`HandshakePacket`, `LoginPacket`, `LoginSuccessPacket`, etc.). |
| **`bot/ban_proxy.py`** | Local proxy (`BanBotProxy`): logs login-phase traffic, rewrites `ChangeSatelliteServerPacket` to keep Flash on `127.0.0.1`, injects **`LoginPacket`** after **`SystemInformationPacket`**, signals success on `LoginSuccessPacket`. |
| **`bot/flash_launch.py`** | Launches Flash + `TFMProxyLoader.swf`, clicks Transformice in the loader (Windows). |
| **`bot/ban_cli.py`** | Multi-slot orchestration: proxies, optional sequential Flash launch, waits for **`LoginSuccess`** on every slot before **`/room`**. |

## Network sequence (proxy view)

1. Client opens **MAIN TCP** to the slot’s proxy port.
2. **Client → server:** `HandshakePacket` (version, loader info, etc.).
3. **Server → client:** `HandshakeResponsePacket` (includes `auth_token` used for the proxy-injected login).
4. **Client → server:** `SystemInformationPacket` — after this, the proxy sends **`LoginPacket`** (username, SHA-Kikoo password hash, loader URL, `ciphered_auth_token` from `auth_token` XOR `secrets.auth_key`). Passwords are never logged as raw bytes in this bot.
5. **Success:** `LoginSuccessPacket` — session id, username, etc. The proxy sets `login_success_event` so the CLI can proceed.

If the real client also sends a **`LoginPacket`**, it is dropped after the proxy has already sent one.

Failure paths commonly seen in logs: `AccountErrorPacket`, `CaptchaPacket`, `ChangeSatelliteServerPacket` (satellite redirect; the proxy forces local ports to avoid Flash sandbox errors on public hosts).

## References

- `bot/ban_proxy.py` — packet listeners, `LoginSuccessPacket`, satellite rewrite, proxy packet login.
- `bot/flash_launch.py` — `launch_one_flash_loader`, loader URL / SWF patch.
- `bot/ban_cli.py` — slot state, `ALL_SLOTS_LOGIN_TIMEOUT_SEC` wait for all slots to log in.
