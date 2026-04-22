# Automated Transformice user ban

This repository (**Automated-Transformice-user-ban**) provides a **command-line** workflow for coordinating **room** moves and **staggered `/ban`** across many Transformice clients, as described in [`Initial-Idea.txt`](Initial-Idea.txt).

The implementation is the Python package **`bot/`**: one **local TCP proxy per game client** (via **caseus**). The proxy injects **`LoginPacket`** after **`SystemInformationPacket`** using credentials from **`BOT_ACCOUNTS_JSON`** in a repo-root **`.env`**.

## Quick start (UI mode)

```powershell
# From the repo root:
.\venv\Scripts\python.exe -m bot
```

The bot will:
1. Load TFM crypto secrets via **TFMSecretsLeaker.swf** (using `flashplayer_32_sa_debug.exe`) or from `TFM_SECRETS_*` in `.env`.
2. Start one local TCP proxy per account slot.
3. Start a shared Flash socket-policy server on port **10801**.
4. Launch one **Flash standalone projector** window per slot, each loading a patched **`TFMProxyLoader.swf`** pointed at its proxy port.
5. In each Flash window, **click "Transformice"** to start the game connection through the proxy.
6. The proxy injects login credentials automatically — no typing needed in Flash.
7. After all slots log in, the CLI prompts for target room and player.

To skip auto-launching Flash windows (open Flash manually instead): `python -m bot --no-ui`

**Manual Flash connect:** open `flashplayer_32_sa_debug.exe` → **File > Open** → select `TFMProxyLoader.swf` from the repo root → click **Transformice** → the game connects through the proxy.

## What you need

- **Python 3.10+**
- **`TFMProxyLoader.swf`** in the repo root (or set **`TFM_PROXY_SWF`**).
- **`flashplayer_32_sa_debug.exe`** (or `flashplayer_32_sa.exe`) in the repo root — or set **`BOT_UI_FLASH_PLAYER_PATH`** / **`FLASHPLAYER`**. Used both for launching game windows and for running **TFMSecretsLeaker.swf** to obtain TFM crypto secrets.
- **`TFM_SECRETS_*`** keys in **`.env`** (auto-populated on first run via the leaker SWF). Alternatively set `BOT_UPSTREAM_SERVER_ADDRESS` + `BOT_UPSTREAM_SERVER_PORTS` manually.
- **Unique outbound IP per client** where required (e.g. **Proxifier** + proxies/VPN). The `bind_ip` field in **`BOT_ACCOUNTS_JSON`** is a reminder only.

## Install

From the repository root (Windows example):

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

Create or edit **`.env`** in the repo root (see **`.env.example`**): set **`BOT_ACCOUNTS_JSON`** to a JSON array — one object per client with unique **`proxy_port`**, optional **`label`**, credentials (`username` / `password`), and optional **`bind_ip`** for Proxifier tracking.

### Build `ban_bot.exe` (optional)

```powershell
.\venv\Scripts\python.exe build_exe.py
```

Writes **`ban_bot.exe`** in the repository root. Run it from that folder so `.env` and `TFMProxyLoader.swf` sit beside the exe.

## How to use the bot

1. **Start the bot:**

   ```powershell
   .\venv\Scripts\python.exe -m bot
   ```

2. The bot starts proxies and launches Flash windows. **Click "Transformice"** in each Flash window to connect it through the proxy.

3. The proxy injects credentials automatically. Logs show **`[login] LoginPacket sent upstream`** and **`OK  [slot …] logged in as …`** per slot.

4. The bot **waits until every slot has logged in** (timeout: **`BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC`**).

5. Enter the **target room** (e.g. `*Racing1`).

6. Enter the **target player** as `nickname#tag` (e.g. `adrian#8912`).

7. The bot sends **`/room`** on each slot, then staggered **`/ban`** commands.

8. After the round, answer **`Ban someone else? (y/n)`**.

### What you should see

- **`Loading TFM secrets …`** → leaker SWF runs, then **`Proxy upstream configured → <ip> ports …`**
- **`Slot X: listen_host=… main=… satellite=…`** for each proxy
- **`Shared Flash policy server listening on 0.0.0.0:10801`**
- **`UI-mode: Flash Player (slot X) started (pid …)`** for each Flash window
- After clicking Transformice in each window: **`[login] MAIN client connected`** → **`[login] upstream TCP connected`** → **`[login] LoginPacket sent upstream`** → **`OK  [slot X] logged in as …`**
- **`All X slot(s) logged in.`** then the room/ban prompt

### Command-line flags

- **`--no-ui`** — do not auto-launch Flash windows; open Flash manually instead.
- **`--ui-sequential`** — open Flash windows one at a time, waiting for each slot to log in before opening the next. Also enabled by **`BOT_UI_SEQUENTIAL_LOGIN=true`**. Timeout: **`BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC`** (default 120 s).
- **`--no-kill-stale`** — do not try to kill processes already listening on configured proxy ports.

### TFM secrets

On startup the bot tries to obtain live TFM crypto secrets in this order:
1. **TFMSecretsLeaker.swf** via the Flash debug projector (`flashplayer_32_sa_debug.exe` in the repo root).
2. **`TFM_SECRETS_*`** keys already in `.env` (written by a previous successful run).
3. **`tfm-secrets`** CLI on PATH (if installed).

Secrets are written back to `.env` on success so subsequent runs start instantly. Set **`BOT_SECRETS_ALWAYS_REFRESH=false`** to skip the live leaker and always use cached `.env` values.

**Upstream port order:** by default the bot tries port **11801** first. Set **`BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS=true`** to randomise. **`BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS`** (default true) overwrites `BOT_UPSTREAM_SERVER_ADDRESS/PORTS` from the live dump.

### Upstream connectivity

To diagnose TCP reachability to the game servers:

```powershell
.\venv\Scripts\python.exe -m bot.upstream_probe <host> 11801 12801 13801 14801
```

**WinError 121** is a connect timeout (firewall/VPN/routing) — not a secrets problem.

## Spec mapping (`Initial-Idea.txt`)

| Requirement | Implementation |
|-------------|----------------|
| ~11 accounts + unique IP | **`.env`**: `BOT_ACCOUNTS_JSON` array; unique `proxy_port`; unique `bind_ip` (Proxifier) |
| Prompt for room + user | CLI `input()` in `bot/ban_cli.py` |
| Wait until all accounts logged in | `login_success_event` per slot; `BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC` |
| `/room` then `/ban` with 1–2 s jitter | `CommandPacket` via proxy; `BOT_ROOM_STAGGER_SEC`, `BOT_BAN_DELAY_*` |
| Per-action confirmation | Logged for each `/room` and `/ban` |
| "OK" on login | `LoginSuccessPacket` handler in `BanBotProxy` |
| Loop "ban someone else?" | Main loop in `ban_cli.main` |
| Open Flash player (UI mode) | Auto-launch via `bot/flash_launch.py`; `--no-ui` to skip |

## Legal / ToS

Use only in line with **Transformice's terms** and applicable law.

## Layout

| Path | Role |
|------|------|
| `bot/` | Ban CLI + local proxy (`python -m bot`) |
| `bot/ban_cli.py` | Main entry: start proxies, launch Flash, wait for logins, send ban commands |
| `bot/ban_proxy.py` | Per-slot proxy: HandshakePacket forwarding, LoginPacket injection, ban commands |
| `bot/secrets_loader.py` | TFM crypto secrets loading (leaker SWF / tfm-secrets CLI / .env) |
| `bot/flash_launch.py` | Flash standalone player launch + SWF patching + trust file |
| `bot/tfm_swf_port_patch.py` | Binary patch `TFMProxyLoader.swf` with proxy host:port |
| `.env` | Per-client `BOT_ACCOUNTS_JSON`, `TFM_SECRETS_*`, and bot knobs |
| `.env.example` | Template copied to `.env` on first run |
| `TFMProxyLoader.swf` | Multi-game loader SWF; patched per slot to connect to the local proxy |
| `flashplayer_32_sa_debug.exe` | Flash standalone projector for UI mode and TFMSecretsLeaker.swf |
