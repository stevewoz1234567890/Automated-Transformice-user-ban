# Automated Transformice user ban

This repository (**Automated-Transformice-user-ban**) is aimed at a **command-line** workflow for coordinating **room** moves and **staggered `/ban`** across many Transformice clients, as described in [`Initial-Idea.txt`](Initial-Idea.txt).

The implementation is the Python package **`bot/`**: one **local TCP proxy per game client** (via **caseus**). The proxy injects **`LoginPacket`** after **`SystemInformationPacket`** using credentials from **`BOT_ACCOUNTS_JSON`** in a repo-root **`.env`**. By default the bot does not start Flash Player, but **UI mode** (`--ui`) can open Flash windows automatically (see below).

**First run:** if **`.env`** is missing, it is created from **`.env.example`**, then any missing **`BOT_*`** keys are appended with defaults (see `bot/env_setup.py`).

**Automatic login (recommended):** run **`python -m bot --headless`** or set **`BOT_HEADLESS_AUTO_LOGIN=true`** in **`.env`**. The bot starts a built-in **caseus** TCP client per slot (see `bot/headless_client.py`, `HandshakePacket` → `SystemInformationPacket` → proxy-injected `LoginPacket`). **`TFM_SECRETS_*`** can be filled automatically when incomplete: try **`tfm-secrets`** (optional **`BOT_TFM_SECRETS_PIP_INSTALL_SPEC`** for a one-time `pip install` if the exe is missing), then **TFMSecretsLeaker.swf** via the **Flash debug projector** (`flashplayer_32_sa_debug.exe` in the repo root, or **`FLASHPLAYER_DEBUG`** — the leaker SWF is downloaded to **`tmp/`**). Results are **written into `.env`** by default (`BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV`). You can still set **`BOT_HEADLESS_SECRETS_INLINE_JSON`** or **`BOT_HEADLESS_SECRETS_DUMPER`**. **`BOT_UPSTREAM_SERVER_ADDRESS`** and **`BOT_UPSTREAM_SERVER_PORTS`** can override host/ports from secrets (comma-separated ports, e.g. `11801,12801,13801,14801`).

Before starting proxies, headless mode runs a **parallel TCP-only probe** ( **`BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS`**; timeout **`BOT_UPSTREAM_PROBE_TIMEOUT_SEC`**). If **no** port connects, **`BOT_UPSTREAM_ABORT_ON_PROBE_ALL_FAILED`** (default **true**) **exits immediately** so you do not burn through headless slots on a dead path. To test manually: `python -m bot.upstream_probe <host> 11801 12801 13801 14801`. Windows **WinError 121** is a **connect timeout** (firewall/VPN/path), not a tfm-secrets problem.

**Upstream port order:** by default **`BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS`** is false — the proxy tries configured ports in list order (put **11801** first if that is your main listener). Set **`BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS=true`** to restore random order like stock caseus. **`BOT_HEADLESS_LOGIN_STAGGER_SEC`** (default **6** s between slots) reduces burst connect attempts that can trigger 121 on later accounts. **`BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES`** (default **3**) stops the headless loop after N failed slots in a row. **`BOT_HEADLESS_SECRETS_ALWAYS_REFRESH`** (default **true**) re-runs tfm-secrets / the Flash leaker before using cached **`TFM_SECRETS_*`**. **`BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS`** (default **true**) writes **`BOT_UPSTREAM_*`** from the dump, sets **`BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY=true`**, and clears **`BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH`**. After **WinError 121**, the inter-slot pause grows by **`BOT_HEADLESS_STAGGER_WIN121_EXTRA_SEC`** up to **`BOT_HEADLESS_STAGGER_MAX_SEC`**.

**UI mode (Flash windows):** run **`python -m bot --ui`** or set **`BOT_UI_AUTO_LAUNCH_FLASH=true`** in **`.env`**. The bot starts the proxies and then launches one **Flash standalone projector** window per slot. Each window opens **`TFMProxyLoader.swf`** pointed at its slot's proxy port; the proxy injects `LoginPacket` automatically so you see the full game UI without entering credentials manually. Flash executable resolution order: **`BOT_UI_FLASH_PLAYER_PATH`** → **`FLASHPLAYER`** / **`FLASH_STANDALONE`** env vars → **`flashplayer_32_sa.exe`** → **`flashplayer_32_sa_debug.exe`** in the repo root. Use **`--no-ui`** to suppress window launches even if the env var is set. **`BOT_UI_FLASH_LAUNCH_STAGGER_SEC`** (default **1.0 s**) spaces out window launches to avoid all slots hammering the proxy simultaneously.

**Manual connector:** omit `--headless` and `--ui`, keep **`BOT_HEADLESS_AUTO_LOGIN`** and **`BOT_UI_AUTO_LAUNCH_FLASH`** false; attach your own client to each slot's main port.

## What you need

- **Python 3.10+**
- **`TFMProxyLoader.swf`** in the repo root (or set **`TFM_PROXY_SWF`**) so the proxy can build a matching **`LoginPacket.loader_url`** (see `bot/flash_launch.py`). Also required for **UI mode** so Flash knows which proxy port to connect to.
- **Either** automatic headless mode (secrets JSON + optional flags above), **UI mode** (Flash standalone projector + `TFMProxyLoader.swf`), **or** one external connector per account that opens TCP to the **matching `proxy_port`** and completes the handshake through **`SystemInformationPacket`**.
- **Unique outbound IP per client** where required (e.g. **Proxifier** + proxies/VPN). The `bind_ip` field in **`BOT_ACCOUNTS_JSON`** is only a **reminder** of which IP you assigned; the bot does not configure Proxifier for you.

## Install

From the repository root (Windows example):

```powershell
cd D:\work\Automated-Transformice-user-ban
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

Create or edit **`.env`** in the repo root (see **`.env.example`**): set **`BOT_ACCOUNTS_JSON`** to a JSON array — one object per client with unique **`proxy_port`** (main port your client connects to), optional **`label`**, and a **distinct `bind_ip`** for your own tracking (must be unique when present). For each slot the bot binds **satellite** (prefers `proxy_port + 10000`). A **policy port number** for **`loader_url`** is either shared (default **10801** via **`BOT_SHARED_FLASH_SOCKET_POLICY_PORT`**) or per-slot (prefers `proxy_port − 10000`); the bot does **not** open Flash socket-policy listeners. If a preferred port is already in use, the next free port is chosen automatically and logged.

### Build `ban_bot.exe` (optional)

After **`pip install -r requirements.txt`** (see Install above), from the repo root:

```powershell
.\venv\Scripts\python.exe build_exe.py
```

**Important:** `caseus` is installed from GitHub via `requirements.txt`, and **`ban_bot.exe` must be built with the same Python** where `import caseus` and `import pak` work. If you use another interpreter to run `build_exe.py`, the frozen exe can start with `ModuleNotFoundError` for those packages. The spec aborts the build with a short message if they are missing.

That writes **`ban_bot.exe`** in the **repository root**. Run it from that folder so **`.env`** and **`.env.example`** sit beside the exe (same layout as the repo). Session logs go to **`log.txt`** in the same folder.

## How to use the bot

**Option A** — packet login from the proxy — is spelled out in root [`method.md`](method.md). The proxy always sends **`LoginPacket`** after **`SystemInformationPacket`** using each row's **`username`** / **`password`** from **`BOT_ACCOUNTS_JSON`**.

**Prerequisite:** something must open a **MAIN TCP** connection to each slot's proxy (typically **`127.0.0.1:<proxy_port>`**). Use **`--headless`** for built-in TCP clients, **`--ui`** to open Flash windows, or connect manually.

1. **Start the bot** from the repo root (choose one connector style):

   ```powershell
   # Headless (no visible game UI, needs TFM_SECRETS_*):
   .\venv\Scripts\python.exe -m bot --headless

   # UI mode (Flash windows per slot, needs TFMProxyLoader.swf + Flash projector):
   .\venv\Scripts\python.exe -m bot --ui

   # Manual (you connect each Flash/client yourself):
   .\venv\Scripts\python.exe -m bot
   ```

   Or use **`ban_bot.exe --ui`** / **`ban_bot.exe --headless`** (exe folder must include **`.env`** / **`.env.example`** and for UI mode the Flash projector and `TFMProxyLoader.swf`).

2. The process logs which **proxy ports** are active. In UI mode, one Flash window per slot opens automatically after the proxies bind. In headless mode, caseus TCP clients connect instead. In manual mode, connect each game client through **tfm-proxy-loader** (or equivalent) to the **main port** for that slot.

3. **Login:** the proxy injects credentials from **`BOT_ACCOUNTS_JSON`**. Logs should include **`LoginPacket sent upstream by proxy (packet login)`** and **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** arrives.

4. The bot **waits until every slot has logged in** (or until **`BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC`** in **`.env`**). It does not ask you to press Enter for that.

5. Enter the **target room** (the text you would type after `/room`, e.g. `*Racing1`).

6. Enter the **target player** as `nickname#tag` (e.g. `adrian#8912`).

7. The bot sends **`/room`** on each connected slot (with a small stagger), then **`/ban`** on each with a **random 1–2 s** gap between accounts (defaults **`BOT_BAN_DELAY_MIN_SEC`** / **`BOT_BAN_DELAY_MAX_SEC`** in **`.env`**).

8. After the round, it asks **`Ban someone else? (y/n)`**. Answer **`y`** to enter another room and target; **`n`** exits.

### What you should see

- **`OK [slot …] logged in as …`** when a client finishes logging in through the proxy (per-slot confirmation).
- Lines confirming **`/room`** and **`/ban`** sends per slot.
- In UI mode: **`UI-mode: Flash Player (slot …) started (pid …)`** for each window launched.
- Optional echoes when the server pushes chat or messages that look ban-related (see proxy handlers in `bot/ban_proxy.py`).

### Command-line flags

- **`--ui`** — launch one Flash standalone player window per slot (UI mode; see above).
- **`--no-ui`** — do not launch Flash windows even if **`BOT_UI_AUTO_LAUNCH_FLASH=true`** in **`.env`**.
- **`--headless`** — start built-in caseus TCP clients (requires secrets in **`.env`**; see above).
- **`--no-headless`** — do not start built-in clients even if **`BOT_HEADLESS_AUTO_LOGIN`** is true in **`.env`**.
- **`--no-kill-stale`** — do not try to kill processes already listening on your configured proxy ports (e.g. `python -m bot --no-kill-stale` or `ban_bot.exe --no-kill-stale`).

### Loader SWF path

Point **`TFM_PROXY_SWF`** at `TFMProxyLoader.swf` if it is not in the repository root next to the exe. The proxy reads that file to build **`loader_url`** for packet login; in UI mode the Flash window also loads this SWF to connect to the proxy.

## Spec mapping (`Initial-Idea.txt`)

| Requirement | Implementation |
|-------------|----------------|
| `config.py` with ~11 accounts + unique IP | **`.env`**: `BOT_ACCOUNTS_JSON` array; unique `proxy_port`; unique `bind_ip` when set (Proxifier / VPN discipline) |
| Prompt for room + user | CLI `input()` in `bot/ban_cli.py` |
| Wait until all accounts logged in | `login_success_event` per slot; `BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC` |
| `/room` then `/ban` with 1–2 s jitter | `CommandPacket` in proxy; `BOT_ROOM_STAGGER_SEC`, `BOT_BAN_DELAY_*` in `.env` |
| Per-action confirmation | Prints for each `/room` and `/ban` |
| "OK" on login | `LoginSuccessPacket` handler in `BanBotProxy` |
| Loop "ban someone else?" | Main loop in `ban_cli.main` |
| Chat hint when someone is banned | Room / general message listeners in `ban_proxy.py` |
| Open Flash player (UI mode) | `--ui` / `BOT_UI_AUTO_LAUNCH_FLASH`; `bot/flash_launch.py` `launch_flash_players_for_slots` |

## Legal / ToS

Use only in line with **Transformice's terms** and applicable law. This repository is for automation you are explicitly allowed to perform.

## Layout

| Path | Role |
|------|------|
| `bot/` | Ban CLI + local proxy (`python -m bot`) |
| `bot/flash_launch.py` | Build loader URLs + UI-mode Flash player launch |
| `.env` | Per-client `BOT_ACCOUNTS_JSON`, `TFM_SECRETS_*`, and bot knobs (local; gitignored) |
| `.env.example` | Template copied to `.env` on first run |
| `ban_bot.spec`, `build_exe.py`, `requirements.txt` | Build **`ban_bot.exe`** in the repo root |
| `ban_bot.exe` | Frozen Windows app (build output; gitignored) |
| `Initial-Idea.txt` | Original feature / difficulty notes |
| `method.md` | Option A: proxy packet login, external MAIN TCP |
| `Transformice*.swf`, `Transformice.exe`, `TFMProxyLoader.swf` | Client / loader assets (as committed) |
| `flashplayer_32_sa.exe` / `flashplayer_32_sa_debug.exe` | Flash standalone projector for UI mode (place here or set `BOT_UI_FLASH_PLAYER_PATH`) |
