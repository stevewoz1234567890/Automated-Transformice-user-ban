# Method: Option A — Packet auto-login, no Flash started by the bot

**Git branch:** `cmd-option-a` (the name `cmd/option-a` is impossible while branch `cmd` exists; see root `idea.md`.)

This branch documents **Option A**: use **`PACKET_AUTO_LOGIN`** in `bot/config.py` and **`--no-launch-flash`** so the bot never starts Flash Player or `TFMProxyLoader.swf`. The proxy sends **`LoginPacket`** after **`SystemInformationPacket`**; you do not automate the Flash login UI.

## Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (see root `README.md`).
- `bot/config.py` with **`ACCOUNTS`** rows: each slot needs **`proxy_port`**, **`username`**, **`password`**, and optional **`label`** / **`bind_ip`**.
- **Something** must still connect to each slot’s MAIN TCP (`127.0.0.1:<proxy_port>`) and complete the handshake through **`SystemInformationPacket`** (e.g. your own client, or Flash/loader started **manually** outside this bot). The bot does not create that connection by itself.

## Configuration

In **`bot/config.py`**:

1. Set **`PACKET_AUTO_LOGIN = True`**.
2. Ensure each account row has **`username`** and **`password`** (used only for proxy-injected login; not for Flash UI when packet login is on).
3. With **`PACKET_AUTO_LOGIN = True`**, **`FLASH_AUTO_LOGIN_UI`** is effectively disabled for the run (see `bot/ban_cli.py`).

Optional: tune **`PROXY_VERBOSE_LOGIN_FLOW`**, **`PACKET_LOGIN_DELAY`** (if present in your config), etc., per your `ban_proxy` / config conventions.

## Command line

From the repository root:

```powershell
.\venv\Scripts\python.exe -m bot --no-launch-flash
```

Or with the frozen exe (from repo root, next to `bot\config.py`):

```powershell
.\ban_bot.exe --no-launch-flash
```

Flags:

- **`--no-launch-flash`** — do not auto-start `flashplayer_32_sa_debug.exe` + `TFMProxyLoader.swf`.
- **`--no-kill-stale`** — optional; do not kill processes already on your proxy ports.

## What to expect

- Logs should show **`PACKET_AUTO_LOGIN — LoginPacket sent upstream (no Flash UI needed)`** when the proxy injects login (see `bot/ban_proxy.py`).
- **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** is seen.
- If nothing connects to the proxy port, you will not get past the wait for clients; ensure your external connector reaches **`127.0.0.1:<proxy_port>`** for each slot.

## See also

- Root **`idea.md`** — full comparison of Option A vs Option B.
- **`docs/TRANSFORMICE_LOGIN_ANALYSIS.md`** — packet sequence and `PACKET_AUTO_LOGIN` behavior.
