# Method: Option A — Packet login (no Flash Player from the bot)

**Git branch:** `cmd-option-a` (Git disallows a branch `cmd/option-a` while a branch named `cmd` exists — use `cmd-option-a` instead.)

The bot **never** starts Flash Player or `TFMProxyLoader.swf`. The proxy **always** sends **`LoginPacket`** after **`SystemInformationPacket`** using credentials from `bot/config.py`. The **`LoginPacket.loader_url`** string is still built from **`TFMProxyLoader.swf`** on disk (same metadata a real loader would use); that is not the same as launching Flash.

## Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (see root `README.md`).
- `bot/config.py` with **`ACCOUNTS`** rows: each slot needs **`proxy_port`**, **`username`**, **`password`**, and optional **`label`** / **`bind_ip`**.
- **Either** enable **headless TCP** (`python -m bot --headless` or `HEADLESS_AUTO_LOGIN = True`) with **`HEADLESS_SECRETS_JSON`** (and upstream `server_address` / `server_ports` in that JSON), **or** connect each slot yourself to MAIN TCP (`127.0.0.1:<proxy_port>`) through **`SystemInformationPacket`** (external client, Ruffle, etc.).

## Configuration

In **`bot/config.py`**:

1. **`PACKET_LOGIN_DELAY_SEC`** / **`PACKET_LOGIN_START_ROOM`** — optional tuning for the injected login.
2. **`ALL_SLOTS_LOGIN_TIMEOUT_SEC`** — max time to wait for **every** slot to show **`LoginSuccess`** before the ban prompt (no “press Enter when ready”).

Optional: **`PROXY_VERBOSE_LOGIN_FLOW`**, **`SHARED_FLASH_SOCKET_POLICY_PORT`** (number embedded in **`loader_url`** only), etc.

## Command line

From the repository root:

```powershell
.\venv\Scripts\python.exe -m bot --headless
```

Or with the frozen exe (from repo root, next to `bot\config.py`):

```powershell
.\ban_bot.exe --headless
```

Flags:

- **`--headless`** — start built-in caseus clients (needs secrets JSON + upstream server in JSON or `UPSTREAM_SERVER_*` in config).
- **`--no-headless`** — never start built-in clients (use an external connector).
- **`--no-kill-stale`** — optional; do not kill processes already on your proxy ports.

## What to expect

- Logs should show **`LoginPacket sent upstream by proxy (packet login)`** when the proxy injects login (see `bot/ban_proxy.py`).
- **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** is seen.
- If nothing connects to the proxy port, you will not get past the wait for login; ensure your external connector reaches **`127.0.0.1:<proxy_port>`** for each slot.

## See also

- Root **`README.md`** — install and Option A expectations.
- **`docs/TRANSFORMICE_LOGIN_ANALYSIS.md`** — packet sequence and proxy login behavior.
