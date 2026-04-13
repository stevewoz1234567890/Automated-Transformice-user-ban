# Automated Transformice user ban

This repository (**Automated-Transformice-user-ban**) is aimed at a **command-line** workflow for coordinating **room** moves and **staggered `/ban`** across many Transformice clients, as described in [`Initial-Idea.txt`](Initial-Idea.txt).

The implementation is the Python package **`bot/`**: one **local TCP proxy per game client** (via **caseus**). The proxy injects **`LoginPacket`** after **`SystemInformationPacket`** using credentials from **`BOT_ACCOUNTS_JSON`** in a repo-root **`.env`**. The bot does **not** start Flash Player.

**First run:** if **`.env`** is missing, it is created from **`.env.example`**, then any missing **`BOT_*`** keys are appended with defaults (see `bot/env_setup.py`).

**Automatic login (recommended):** run **`python -m bot --headless`** or set **`BOT_HEADLESS_AUTO_LOGIN=true`** in **`.env`**. The bot starts a built-in **caseus** TCP client per slot (see `bot/headless_client.py`, `HandshakePacket` → `SystemInformationPacket` → proxy-injected `LoginPacket`). You need **`TFM_SECRETS_*` in `.env`**, or **`BOT_HEADLESS_SECRETS_INLINE_JSON`**, or **`BOT_HEADLESS_SECRETS_DUMPER`** (CLI that prints tfm-secrets JSON on stdout — nothing is read from `tfm-secrets.json` by default). **`BOT_UPSTREAM_SERVER_ADDRESS`** and **`BOT_UPSTREAM_SERVER_PORTS`** can override host/ports from secrets (comma-separated ports, e.g. `11801,12801,13801,14801`).

Before headless login, the bot runs a **parallel TCP-only probe** to those ports (toggle with **`BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS`**; timeout **`BOT_UPSTREAM_PROBE_TIMEOUT_SEC`**). To test manually: `python -m bot.upstream_probe <host> 11801 12801 13801 14801`. Windows **WinError 121** is a **connect timeout** (firewall/VPN/path), not a tfm-secrets problem.

**Upstream port order:** by default **`BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS`** is false — the proxy tries configured ports in list order (put **11801** first if that is your main listener). Set **`BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS=true`** to restore random order like stock caseus. **`BOT_HEADLESS_LOGIN_STAGGER_SEC`** (default **2.5** s between slots) reduces burst connect attempts that can trigger 121 on later accounts.

**Manual connector:** omit `--headless` and keep **`BOT_HEADLESS_AUTO_LOGIN`** false; attach your own client to each slot’s main port.

## What you need

- **Python 3.10+**
- **`TFMProxyLoader.swf`** in the repo root (or set **`TFM_PROXY_SWF`**) so the proxy can build a matching **`LoginPacket.loader_url`** (see `bot/flash_launch.py`). Game assets are optional for the bot itself.
- **Either** automatic headless mode (secrets JSON + optional flags above) **or** one external connector per account that opens TCP to the **matching `proxy_port`** and completes the handshake through **`SystemInformationPacket`**.
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

**Option A** — packet login from the proxy — is spelled out in root [`method.md`](method.md). The proxy always sends **`LoginPacket`** after **`SystemInformationPacket`** using each row’s **`username`** / **`password`** from **`BOT_ACCOUNTS_JSON`**. The bot never starts Flash Player.

**Prerequisite:** something must open a **MAIN TCP** connection to each slot’s proxy (typically **`127.0.0.1:<proxy_port>`**). Use **`--headless`** to do that inside the bot, or connect manually.

1. **Start the bot** from the repo root (add **`--headless`** for automatic TCP login if secrets are configured):

   ```powershell
   .\venv\Scripts\python.exe -m bot --headless
   ```

   Or: **`ban_bot.exe --headless`** (exe folder must include **`.env`** / **`.env.example`** as usual). Without **`--headless`**, connect each external client to the **main port** for that slot.

2. The process logs which **proxy ports** are active. If not using headless, connect each game client through **tfm-proxy-loader** (or equivalent) to the **main port** for that slot.

3. **Login:** the proxy injects credentials from **`BOT_ACCOUNTS_JSON`**. Logs should include **`LoginPacket sent upstream by proxy (packet login)`** and **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** arrives.

4. The bot **waits until every slot has logged in** (or until **`BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC`** in **`.env`**). It does not ask you to press Enter for that.

5. Enter the **target room** (the text you would type after `/room`, e.g. `*Racing1`).

6. Enter the **target player** as `nickname#tag` (e.g. `adrian#8912`).

7. The bot sends **`/room`** on each connected slot (with a small stagger), then **`/ban`** on each with a **random 1–2 s** gap between accounts (defaults **`BOT_BAN_DELAY_MIN_SEC`** / **`BOT_BAN_DELAY_MAX_SEC`** in **`.env`**).

8. After the round, it asks **`Ban someone else? (y/n)`**. Answer **`y`** to enter another room and target; **`n`** exits.

### What you should see

- **`OK [slot …] logged in as …`** when a client finishes logging in through the proxy (per-slot confirmation).
- Lines confirming **`/room`** and **`/ban`** sends per slot.
- Optional echoes when the server pushes chat or messages that look ban-related (see proxy handlers in `bot/ban_proxy.py`).

### Command-line flags

- **`--headless`** — start built-in caseus TCP clients (requires secrets in **`.env`**; see above).
- **`--no-headless`** — do not start built-in clients even if **`BOT_HEADLESS_AUTO_LOGIN`** is true in **`.env`**.
- **`--no-kill-stale`** — do not try to kill processes already listening on your configured proxy ports (e.g. `python -m bot --no-kill-stale` or `ban_bot.exe --no-kill-stale`).

### Loader SWF path

Point **`TFM_PROXY_SWF`** at `TFMProxyLoader.swf` if it is not in the repository root next to the exe. The proxy reads that file to build **`loader_url`** for packet login; Flash Player is not started by the bot.

## Spec mapping (`Initial-Idea.txt`)

| Requirement | Implementation |
|-------------|----------------|
| `config.py` with ~11 accounts + unique IP | **`.env`**: `BOT_ACCOUNTS_JSON` array; unique `proxy_port`; unique `bind_ip` when set (Proxifier / VPN discipline) |
| Prompt for room + user | CLI `input()` in `bot/ban_cli.py` |
| Wait until all accounts logged in | `login_success_event` per slot; `BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC` |
| `/room` then `/ban` with 1–2 s jitter | `CommandPacket` in proxy; `BOT_ROOM_STAGGER_SEC`, `BOT_BAN_DELAY_*` in `.env` |
| Per-action confirmation | Prints for each `/room` and `/ban` |
| “OK” on login | `LoginSuccessPacket` handler in `BanBotProxy` |
| Loop “ban someone else?” | Main loop in `ban_cli.main` |
| Chat hint when someone is banned | Room / general message listeners in `ban_proxy.py` |

## Legal / ToS

Use only in line with **Transformice’s terms** and applicable law. This repository is for automation you are explicitly allowed to perform.

## Layout

| Path | Role |
|------|------|
| `bot/` | Ban CLI + local proxy (`python -m bot`) |
| `.env` | Per-client `BOT_ACCOUNTS_JSON`, `TFM_SECRETS_*`, and bot knobs (local; gitignored) |
| `.env.example` | Template copied to `.env` on first run |
| `ban_bot.spec`, `build_exe.py`, `requirements.txt` | Build **`ban_bot.exe`** in the repo root |
| `ban_bot.exe` | Frozen Windows app (build output; gitignored) |
| `Initial-Idea.txt` | Original feature / difficulty notes |
| `method.md` | Option A: proxy packet login, external MAIN TCP |
| `Transformice*.swf`, `Transformice.exe`, `TFMProxyLoader.swf` | Client / loader assets (as committed) |
