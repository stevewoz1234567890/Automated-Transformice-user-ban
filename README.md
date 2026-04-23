# Automated Transformice user ban

Multi-slot Transformice ban bot. Each account runs behind a **local TCP proxy** (via [caseus](https://github.com/friedkeenan/caseus)). The bot auto-launches Flash Player, auto-logs every slot in via packet injection, then guides you through picking a room and a player before sending `/ban` from all accounts simultaneously.

---

## What you need

- **Python 3.10+**
- **Flash Player standalone debug projector** (`flashplayer_32_sa_debug.exe`) in the repo root (or set `BOT_UI_FLASH_PLAYER_PATH` in `.env`)
- **`TFMProxyLoader.swf`** in the repo root — the patched loader that points Flash at the local proxy
- **`TFM_SECRETS_*`** values in `.env` — game crypto used for packet-based auto-login (see [`.env` reference](#env-reference) below)
- **One unique outbound IP per account** if the game server rate-limits by IP — assign them in Proxifier or equivalent; the `bind_ip` field in `BOT_ACCOUNTS_JSON` is for your reference only

---

## Install

```powershell
cd path\to\Automated-Transformice-user-ban
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

Copy `.env.example` to `.env` (or create `.env` from scratch) and fill in your accounts and secrets — see [`.env` reference](#env-reference).

---

## Run

```powershell
.\venv\Scripts\python.exe -m bot
```

Or use the frozen build: `ban_bot.exe` (`.env` must be in the same folder).

### What happens automatically

1. **Proxy listeners start** — one main port + satellite port per slot.
2. **Flash Player windows open** one at a time (controlled by `BOT_UI_AUTO_LAUNCH_FLASH`).
3. **Auto-login via packet injection** — after each slot's Flash connects to the proxy, the proxy injects `LoginPacket` with credentials from `BOT_ACCOUNTS_JSON` (requires `BOT_PACKET_AUTO_LOGIN=true`). The log prints `OK  [slot N] logged in as Username#XXXX`.
4. **All slots logged-in detection** — the bot waits automatically; no "Press Enter" prompt.
5. **Room list fetched** — the bot queries the game server for available rooms and prints a numbered menu.
6. **You pick a room** by number or type a name directly (e.g. `*racing1`).
7. **Player list fetched** — the bot joins the chosen room with all slots via `JoinRoomPacket` and prints every player currently there, numbered.
8. **You pick a target** by number or type the nickname directly (e.g. `Zizao#0000`).
9. **`/ban` sent** from every slot with a random 1–2 s gap between accounts (tunable in `.env`).
10. **"Ban someone else? (y/n)"** — answer `y` to start a new room selection; `n` exits.

### What you see in the console

```
04:25:26 [INFO] OK  [slot 1] logged in as Husarz#3007
...
04:25:27 [INFO] All 6 slot(s) logged in — proceeding.
04:25:27 [INFO] Fetching room list from game server...

Available rooms (12 total):
    1. *1                              [21 players]
    2. *bootcamp1                      [13 players]
    3. *racing 1                       [19 players]
    ...

Enter room number or room name (e.g. *Racing1): 3
04:25:30 [INFO] Collecting player list for '*racing 1'...

Players in '*racing 1' (25 total):
    1. Aaron#6719
    2. Bartbakerxxl#0000
    3. Candydandy#6598
    ...

Enter player number or nickname (e.g. Zizao#0000): 1
04:25:31 [INFO] [slot 1] /ban "Aaron#6719" -> sent
...
Ban someone else? (y/n):
```

---

## Command-line flags

| Flag | Effect |
|------|--------|
| `--no-kill-stale` | Do not kill processes already occupying configured proxy ports |
| `--launch-flash-no-click` | Open Flash windows but skip the automated Transformice button click (click manually) |
| `--no-flash-auto-login` | Disable the auto-dismiss + auto-login UI (login manually in each window) |

---

## `.env` reference

### Accounts

```ini
BOT_ACCOUNTS_JSON = [
    {
        "label":      "1",
        "proxy_port": 38291,
        "bind_ip":    "10.47.103.2",
        "username":   "account@example.com",
        "password":   "hunter2"
    },
    ...
]
```

| Key | Required | Notes |
|-----|----------|-------|
| `proxy_port` | ✔ | Unique TCP port the proxy listens on (Flash connects here) |
| `username` | ✔ | Email or `Nickname#tag` |
| `password` | ✔ | Plain-text password (used to compute the login hash) |
| `label` | — | Human-readable slot name shown in logs |
| `bind_ip` | — | Reference only — which outbound IP you assigned in Proxifier |

### Crypto secrets (packet auto-login)

Fill these from a secrets dump (e.g. `tfm-secrets` tool or `TFMSecretsLeaker.swf`):

```ini
TFM_SECRETS_SERVER_ADDRESS=51.38.60.113
TFM_SECRETS_SERVER_PORTS=12801,13801,14801,11801
TFM_SECRETS_GAME_VERSION=920
TFM_SECRETS_CONNECTION_TOKEN=XieNbK
TFM_SECRETS_AUTH_KEY=3415003
TFM_SECRETS_PACKET_KEY_SOURCES=2,50,57,...
TFM_SECRETS_CLIENT_VERIFICATION_TEMPLATE=aabbccdd...
```

### Proxy / login behavior

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_PACKET_AUTO_LOGIN` | `false` | Inject `LoginPacket` automatically after `SystemInformationPacket` |
| `BOT_PACKET_LOGIN_DELAY_SEC` | `0.35` | Delay before sending the login packet |
| `BOT_PACKET_LOGIN_START_ROOM` | *(empty)* | Optional start room sent in `LoginPacket` |
| `BOT_PROXY_VERBOSE_LOGIN_FLOW` | `true` | Log extra handshake / login packet details |
| `BOT_PROXY_LOG_ALL_MAIN_PACKETS` | `false` | Log every main-connection packet (debug) |
| `BOT_PROXY_BIND_HOST` | *(all)* | IP the proxy listens on (leave blank for all interfaces) |
| `BOT_SHARED_FLASH_SOCKET_POLICY_PORT` | `10801` | Port serving Flash socket policy for all slots |

### Flash UI launch

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_UI_AUTO_LAUNCH_FLASH` | `true` | Auto-open `flashplayer_32_sa_debug.exe` per slot |
| `BOT_UI_FLASH_PLAYER_PATH` | *(repo root)* | Path to Flash Player exe if not in repo root |
| `BOT_UI_FLASH_LAUNCH_STAGGER_SEC` | `0.5` | Pause between opening successive Flash windows |
| `BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC` | `900` | Per-slot login wait timeout (seconds) |

### Ban timing

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_BAN_DELAY_MIN_SEC` | `1.0` | Minimum random gap between `/ban` sends |
| `BOT_BAN_DELAY_MAX_SEC` | `2.0` | Maximum random gap between `/ban` sends |
| `BOT_ROOM_STAGGER_SEC` | `0.15` | Stagger between `JoinRoomPacket` sends across slots |

---

## Build `ban_bot.exe` (optional)

```powershell
.\venv\Scripts\python.exe build_exe.py
```

Writes `ban_bot.exe` to the repo root. Run it from that folder so `.env` is next to the exe. Logs go to `log.txt` in the same folder.

---

## Project layout

| Path | Role |
|------|------|
| `bot/` | Main Python package (`python -m bot`) |
| `bot/ban_cli.py` | CLI entry point — proxy setup, Flash launch, room/player menus, ban loop |
| `bot/ban_proxy.py` | `BanBotProxy` — packet interception, auto-login, room list, player list |
| `bot/env_config.py` | Loads `.env` → config namespace |
| `bot/flash_launch.py` | Flash Player auto-launch and UI automation |
| `.env` | Your accounts + secrets + settings (gitignored) |
| `flashplayer_32_sa_debug.exe` | Flash standalone debug projector |
| `TFMProxyLoader.swf` | Patched loader SWF that connects Flash to the local proxy |
| `ban_bot.spec`, `build_exe.py` | Build config for `ban_bot.exe` |
| `requirements.txt` | Python dependencies |
| `log.txt` | Session log (appended on each run) |

---

## Legal / ToS

Use only in line with **Transformice's terms of service** and applicable law. This repository is for automation you are explicitly permitted to perform.
