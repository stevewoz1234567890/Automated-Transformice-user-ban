# Automated Transformice user ban

Multi-slot Transformice ban bot. Supports two modes:

- **Headless mode** (`--headless-keepalive`, recommended) — each account connects **directly** to the game server via a Python TCP client ([caseus](https://github.com/friedkeenan/caseus)). No Flash Player, no local proxy, no UI automation.
- **Flash mode** (legacy) — each account runs behind a local TCP proxy; Flash Player windows are auto-launched and login is injected via packet interception.

---

## What you need

- **Python 3.10+**
- **`TFM_SECRETS_*`** values in `.env` — game crypto used for login (see [`.env` reference](#env-reference) below)
- **One unique outbound IP per account** if the game server rate-limits by IP — assign them in Proxifier or equivalent; the `bind_ip` field in `BOT_ACCOUNTS_JSON` is for your reference only

**Flash mode only** (not needed for headless):
- **Flash Player standalone debug projector** (`flashplayer_32_sa_debug.exe`) in the repo root (or set `BOT_UI_FLASH_PLAYER_PATH` in `.env`)
- **`TFMProxyLoader.swf`** in the repo root — the patched loader that points Flash at the local proxy

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

### Headless mode (recommended)

```powershell
.\venv\Scripts\python.exe -m bot --headless-keepalive --skip-net-check
```

Each account connects directly to the Transformice server — no Flash Player windows, no proxy needed. After all slots log in, you get an interactive prompt to pick a room and send `/ban`.

### Flash mode (legacy)

```powershell
.\venv\Scripts\python.exe -m bot
```

Or use the frozen build: `ban_bot.exe` (`.env` must be in the same folder).

> **Note:** Flash mode requires `flashplayer_32_sa_debug.exe` and `TFMProxyLoader.swf`. If the Flash client crashes after login (`clean-eof` ~5s), the SWF loader is likely out of date — use headless mode instead.

### What happens automatically

#### Headless mode (`--headless-keepalive`)

1. **Secrets refreshed** — `TFMSecretsLeaker.swf` or `tfm-secrets` CLI fetches fresh game crypto.
2. **Direct TCP connections** — each slot opens a `caseus.Client` connection straight to the game server.
3. **Auto-login** — credentials from `BOT_ACCOUNTS_JSON` are sent directly. The log prints `DIRECT LOGIN SUCCESS as Username#XXXX`.
4. **All slots logged-in detection** — the bot waits up to 5 minutes for all slots.
5. **You pick a room** by typing the name (e.g. `*racing1`). All slots join via `JoinRoomPacket`.
6. **You pick a target** by typing the nickname. `/ban` is sent from every slot.
7. **"Ban someone else? (y/n)"** — answer `y` to pick another room; `n` exits.

#### Flash mode (legacy)

1. **Proxy listeners start** — one main port + satellite port per slot.
2. **Flash Player windows open** one at a time (controlled by `BOT_UI_AUTO_LAUNCH_FLASH`).
3. **Auto-login via packet injection** — after each slot's Flash connects to the proxy, the proxy injects `LoginPacket` with credentials from `BOT_ACCOUNTS_JSON` (requires `BOT_PACKET_AUTO_LOGIN=true`). The log prints `OK  [slot N] logged in as Username#XXXX`.
4. **All slots logged-in detection** — the bot waits automatically; no "Press Enter" prompt.
5. **Room list fetched** — the bot queries the game server for available rooms and prints a numbered menu.
6. **You pick a room** by number or type a name directly (e.g. `*racing1`).
7. **Player list fetched** — the bot joins the chosen room with all slots via `JoinRoomPacket` and prints every player currently there, numbered.
8. **You pick a target** by number or type the nickname directly (e.g. `Zizao#0000`).
9. **`/ban` sent** from every slot with a random 1–2 s gap between accounts (tunable in `.env`). In-game room bans normally need **[11 distinct `/ban` reports](docs/BAN_QUORUM_TRANSFORMICE.md)** (configurable via `BOT_BAN_QUORUM_REPORTS`). The CLI warns if too few slots succeed in one round.
10. **"Ban someone else? (y/n)"** — answer `y` to start a new room selection; `n` exits.

### Example console output (headless)

```
08:11:49 [INFO] Slot 1: direct headless started → 51.38.60.113:(11801, ...)
08:11:50 [INFO] Slot 1: DIRECT LOGIN SUCCESS as Thuglifex#3946
08:11:52 [INFO] Slot 2: DIRECT LOGIN SUCCESS as Doni#8783
...
08:12:23 [INFO] Headless-keepalive login complete: 14/14 slots logged in.
08:12:23 [INFO] All 14 slot(s) logged in — proceeding.

==================================================
  HEADLESS MODE — 14 slots logged in
==================================================

Enter room name (or 'q' to quit): *racing1
Enter player name to /ban (or 'skip'): Zizao#0000
08:12:45 [INFO] /ban Zizao#0000 complete: 14 ok, 0 fail in 3.2s
Ban someone else? (y/n):
```

### Example console output (Flash, legacy)

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
| `--headless-keepalive` | **Direct headless mode** — connect to the game server with Python TCP clients, no Flash or proxy needed |
| `--skip-net-check` | Skip the DNS/multi-port/HTTP preflight probe (useful with `--headless-keepalive`) |
| `--probe-clients` | Probe all known game-client access methods (SWF URLs, Steam, TCP, etc.) and exit with a report |
| `--client-mode MODE` | Override `BOT_GAME_CLIENT_MODE` (`flash_projector`, `standalone_exe`, `steam`, `ruffle`) |
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

Put the full block in **`.env`** (it overrides the placeholders in `bot/bot_env_defaults.py`), **or** save the JSON from `scripts/export_tfm_secrets_json.py` / `tfm-secrets` CLI as repo-root **`tfm-secrets.json`** — on startup, any **missing** `TFM_SECRETS_*` variables are filled from that file before code defaults apply (`BOT_MERGE_TFM_SECRETS_JSON`, default on). You still need a **`TFMProxyLoader.swf`** that matches the live game (same build as the secrets dump).

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
| `BOT_PROXY_UPSTREAM_FAIL_TRACE` | *(unset)* | If `true`, log a Python traceback when upstream TCP fails all game ports (after per-port WARNINGs) |
| `BOT_DEBUG_TRACE` | `false` | Session-wide ordered **`[trace #N \| phase \| slot]`** INFO steps (like single-stepping); full **`bot.*`** DEBUG written to **`log.txt`** (stderr stays INFO unless `BOT_DEBUG_TRACE_CONSOLE`). |
| `BOT_DEBUG_TRACE_CONSOLE` | `false` | With **`BOT_DEBUG_TRACE`**, also mirror DEBUG on stderr (very noisy). |
| `BOT_LOG_LEVEL` | *(empty)* | Set to **`DEBUG`** for **`bot.*`** file detail without trace markers. |
| `BOT_ASYNCIO_DEBUG` | `false` | Each slot `asyncio.run`: **`loop.set_debug(True)`** + asyncio logger DEBUG; pairs with trace for scheduler issues. |
| `BOT_ASYNCIO_SLOW_CALLBACK_SEC` | `0.05` | **`slow_callback_duration`** when **`BOT_ASYNCIO_DEBUG`** is on. |
| `BOT_UPSTREAM_MAX_CONCURRENT_CONNECTS` | `2` | Process-wide cap on simultaneous TCP handshakes to the game server across all slots (raises Windows WinError 121 when too high with many Flash clients / Proxifier). |
| `BOT_UPSTREAM_OPEN_CONNECTION_TIMEOUT_SEC` | `12` | Per-attempt timeout inside each upstream connect (wrapped with `asyncio.wait_for`). |
| `BOT_UPSTREAM_OPEN_STREAMS_ROUND_RETRIES` | *(unset)* | Extra full port sweeps when every port fails once; defaults to `BOT_UPSTREAM_PROBE_RETRIES`. |
| `BOT_UPSTREAM_OPEN_STREAMS_ROUND_PAUSE_SEC` | *(unset)* | Seconds between sweeps; defaults to `BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC`. |
| `BOT_UPSTREAM_LOCAL_BIND_IPV4` | *(empty)* | Force **all** upstream probes and (when set) proxy connects to bind outbound TCP from this IPv4 (must exist on a local NIC). |
| `BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET` | `false` | When `true`, each slot’s `BanBotProxy.open_streams` uses `local_addr=(row bind_ip, 0)` so traffic egresses like Proxifier expects (multi-WAN). |
| `BOT_NET_PREFLIGHT_TRY_ACCOUNT_BIND_IPS` | `false` | If default-route preflight fails, retry the multi-port probe once **per distinct** `bind_ip` in `BOT_ACCOUNTS_JSON`. |
| `BOT_PROXY_BIND_HOST` | *(all)* | IP the proxy listens on (leave blank for all interfaces) |
| `BOT_SHARED_FLASH_SOCKET_POLICY_PORT` | `10801` | Port serving Flash socket policy for all slots |

### Flash UI launch

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_UI_AUTO_LAUNCH_FLASH` | `true` | Auto-open `flashplayer_32_sa_debug.exe` per slot |
| `BOT_UI_FLASH_PLAYER_PATH` | *(repo root)* | Path to Flash Player exe if not in repo root |
| `BOT_UI_FLASH_LAUNCH_STAGGER_SEC` | `2.5` | Pause between opening successive Flash windows (auto floor may apply for many slots) |
| `BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC` | `900` | Per-slot login wait timeout (seconds) |

### Headless mode

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_GAME_CLIENT_MODE` | `flash_projector` | Active client mode (`flash_projector`, `standalone_exe`, `steam`, `ruffle`) |
| `BOT_PROBE_GAME_CLIENTS_AT_STARTUP` | `false` | Run client availability probe on startup |
| `HEADLESS_LOGIN_STAGGER_SEC` | `2.5` | Delay between starting successive direct headless slots |

### Ban timing

| Key | Default | Description |
|-----|---------|-------------|
| `BOT_BAN_DELAY_MIN_SEC` | `1.0` | Minimum random gap between `/ban` sends |
| `BOT_BAN_DELAY_MAX_SEC` | `2.0` | Maximum random gap between `/ban` sends |
| `BOT_ROOM_STAGGER_SEC` | `0.15` | Stagger between `JoinRoomPacket` sends across slots |
| `BOT_BAN_QUORUM_REPORTS` | `11` | Typical in-room distinct reports needed for a ban — [docs](docs/BAN_QUORUM_TRANSFORMICE.md); used for warnings only |

---

## Proving parity on another PC (known-good baseline)

Use this when the bot is stable on **your** machine but flaky on a friend’s (mass PARTL, ActionScript dialogs, preflight timeouts). The goal is to prove **same files + stable network + low slot count** before scaling Proxifier or slot count.

### One-shot automation (recommended)

In `.env` set **`BOT_KNOWN_GOOD_PARITY_MODE=true`** (see `bot/bot_env_defaults.py`). On startup the bot applies **`setdefault`** only (your explicit `.env` values win):

- **`BOT_BASELINE_MAX_SLOTS=3`** — use only the first three `BOT_ACCOUNTS_JSON` rows  
- **`BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true`** — exit unless **every** configured game port accepts TCP (not just one)  
- **`BOT_PARITY_STARTUP_REMINDERS=true`** — log a **`[parity]`** checklist  
- **`BOT_UPSTREAM_PROBE_RETRIES`** / **`BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC`** — preflight TCP probes repeat after failures (defaults in `bot/bot_env_defaults.py`; helps tether / Wi‑Fi blips)

You can set those keys manually instead of using `BOT_KNOWN_GOOD_PARITY_MODE`.

| Key | Purpose |
|-----|---------|
| `BOT_KNOWN_GOOD_PARITY_MODE` | Enables the three defaults above via `setdefault` |
| `BOT_BASELINE_MAX_SLOTS` | `0` = use full `BOT_ACCOUNTS_JSON`; `2`–`3` = baseline slice (unique `proxy_port` per row still required) |
| `BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS` | Require **`[probe] SUMMARY: all N ports accepted TCP`** (fatal if any port fails) |
| `BOT_PARITY_STARTUP_REMINDERS` | Log **`[parity]`** checklist at startup; **also implied** when `BOT_BASELINE_MAX_SLOTS` > 0 |
| `BOT_UPSTREAM_PROBE_RETRIES` | Extra preflight attempts after a failed TCP probe (`0`–`10`; default `2`) |
| `BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC` | Seconds to wait between those attempts (default `3`) |

### Manual checklist (same logic)

1. **Copy known-good crypto and loader** from the working PC into the same paths on the other PC:
   - The full **`TFM_SECRETS_*`** block in `.env` (and keep `TFM_SECRETS_SERVER_ADDRESS` / `TFM_SECRETS_SERVER_PORTS` consistent with that dump).
   - Repo-root **`TFMProxyLoader.swf`** (and match **`TFM_PROXY_SWF`** if you set it explicitly).
2. **One stable uplink—no tether hopping** — Prefer reliable Wi‑Fi or Ethernet; avoid switching **phone tether ↔ Wi‑Fi** during a run. In `log.txt`, confirm **`[probe] SUMMARY: all … ports accepted TCP`** under **`[preflight]`** (use **`BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS=true`** to enforce).
3. **Baseline with 2–3 slots** — Either trim **`BOT_ACCOUNTS_JSON`** by hand or set **`BOT_BASELINE_MAX_SLOTS=3`**. Each row needs a **unique `proxy_port`**. Run until **`All N slot(s) logged in — proceeding`** without repeated PARTL retries.
4. **Scale up, then tighten Proxifier** — Add more accounts gradually (set **`BOT_BASELINE_MAX_SLOTS=0`**). **`bind_ip` in JSON is reference only** unless an external tool routes traffic: route the process that opens **upstream TCP to the game**—typically **`python.exe`**, **`venv\Scripts\python.exe`**, or **`ban_bot.exe`**—not Flash alone. Preflight logs the exact **`exe=`** path. If preflight passes only on tether but fails on Wi‑Fi while each row has **`bind_ip`**, enable **`BOT_NET_PREFLIGHT_TRY_ACCOUNT_BIND_IPS=true`** and **`BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET=true`** so TCP uses those source addresses (README proxy table).

If the 2–3-slot baseline fails even after (1)–(2), the problem is almost always **network reachability** or **artifact mismatch**, not “how many Flash windows.”

---

## Build `ban_bot.exe` (optional)

```powershell
.\venv\Scripts\python.exe build_exe.py
```

Writes `ban_bot.exe` to the repo root. Run it from that folder so `.env` is next to the exe. Session logs append to `log.txt` in the same folder.

---

## Project layout

| Path | Role |
|------|------|
| `bot/` | Main Python package (`python -m bot`) |
| `bot/ban_cli.py` | CLI entry point — proxy setup, headless/Flash launch, room/player menus, ban loop |
| `bot/ban_proxy.py` | `BanBotProxy` — packet interception, auto-login, room list, player list (Flash mode) |
| `bot/direct_headless.py` | `BanBotDirectClient` + `DirectHeadlessSlot` — direct TCP to game server (headless mode) |
| `bot/headless_client.py` | `HeadlessProxyClient` — headless login through the local proxy |
| `bot/env_config.py` | Loads `.env` → config namespace |
| `bot/flash_launch.py` | Flash Player auto-launch and UI automation (Flash mode) |
| `bot/game_client_registry.py` | Enumerate known game-client access methods (SWF, Steam, Ruffle, etc.) |
| `bot/client_probe.py` | Probe SWF endpoints, TCP servers, and standalone EXE downloads |
| `bot/client_mode.py` | Resolve and validate the active game client mode |
| `bot/probe_clients_cli.py` | CLI diagnostic tool — `--probe-clients` report |
| `bot/steam_client.py` | Detect and launch the Steam Transformice client |
| `bot/ruffle_client.py` | Detect and launch Ruffle (Rust Flash emulator) |
| `bot/standalone_client.py` | Detect, download, and launch the standalone `Transformice.exe` |
| `docs/BAN_QUORUM_TRANSFORMICE.md` | How the ~11-report quorum relates to multi-slot `/ban` |
| `.env` | Your accounts + secrets + settings (gitignored) |
| `flashplayer_32_sa_debug.exe` | Flash standalone debug projector (Flash mode only) |
| `TFMProxyLoader.swf` | Patched loader SWF that connects Flash to the local proxy (Flash mode only) |
| `ban_bot.spec`, `build_exe.py` | Build config for `ban_bot.exe` |
| `requirements.txt` | Python dependencies |
| `log.txt` | Session log (append each run; repo root next to `.env`) |
| `logs/` | Markdown session reports |

---

## Legal / ToS

Use only in line with **Transformice's terms of service** and applicable law. This repository is for automation you are explicitly permitted to perform.
