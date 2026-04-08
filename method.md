# Method: Option A — Packet login, no Flash started by the bot (optional)

**Git branch:** `cmd-option-a` (Git disallows a branch `cmd/option-a` while a branch named `cmd` exists — use `cmd-option-a` instead.)

This branch documents **Option A**: run with **`--no-launch-flash`** so the bot never starts Flash Player or `TFMProxyLoader.swf`. The proxy **always** sends **`LoginPacket`** after **`SystemInformationPacket`** using credentials from `bot/config.py`; there is no Flash login UI automation.

## Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (see root `README.md`).
- `bot/config.py` with **`ACCOUNTS`** rows: each slot needs **`proxy_port`**, **`username`**, **`password`**, and optional **`label`** / **`bind_ip`**.
- **Something** must still connect to each slot’s MAIN TCP (`127.0.0.1:<proxy_port>`) and complete the handshake through **`SystemInformationPacket`** (e.g. your own client, or Flash/loader started **manually** outside this bot). The bot does not create that connection by itself.

## Configuration

In **`bot/config.py`**:

1. **`PACKET_LOGIN_DELAY_SEC`** / **`PACKET_LOGIN_START_ROOM`** — optional tuning for the injected login.
2. **`ALL_SLOTS_LOGIN_TIMEOUT_SEC`** — max time to wait for **every** slot to show **`LoginSuccess`** before the ban prompt (no “press Enter when ready”).

Optional: **`PROXY_VERBOSE_LOGIN_FLOW`**, etc.

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

- Logs should show **`LoginPacket sent upstream by proxy (packet login)`** when the proxy injects login (see `bot/ban_proxy.py`).
- **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** is seen.
- If nothing connects to the proxy port, you will not get past the wait for login; ensure your external connector reaches **`127.0.0.1:<proxy_port>`** for each slot.

## See also

- Root **`README.md`** — how to run with `--no-launch-flash` and Option A expectations.
- **`docs/TRANSFORMICE_LOGIN_ANALYSIS.md`** — packet sequence and proxy login behavior.
