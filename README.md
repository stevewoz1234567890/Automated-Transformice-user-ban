# Automated Transformice user ban

This repository (**Automated-Transformice-user-ban**) is aimed at a **command-line** workflow for coordinating **room** moves and **staggered `/ban`** across many Transformice clients, as described in [`Initial-Idea.txt`](Initial-Idea.txt).

The implementation is the Python package **`bot/`**: one **local TCP proxy per game client** (via **caseus**), with the game attached through **tfm-proxy-loader** — the same general pattern as the invite-style **transformice-bot** stack (local proxy + patched loader).

## What you need

- **Python 3.10+**
- **Transformice** standalone client and **TFMProxyLoader** / loader SWF (this repo includes `Transformice.exe`, `Transformice.swf`, `TFMProxyLoader.swf`, and related assets in the project root — use them or your own matching setup).
- **One game process per account**, each **tfm-proxy-loader** aimed at the **matching `proxy_port`** from `bot/config.py`.
- **Unique outbound IP per client** where required (e.g. **Proxifier** + proxies/VPN). The `bind_ip` field in config is only a **reminder** of which IP you assigned; the bot does not configure Proxifier for you.

## Install

From the repository root (Windows example):

```powershell
cd D:\work\Automated-Transformice-user-ban
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
```

Create or edit **`bot/config.py`** (gitignored if you keep secrets there): one row per client — unique **`proxy_port`** (this is the **main** port for tfm-proxy-loader), optional **`label`**, and a **distinct `bind_ip`** string for your own tracking (must be unique when present). For each slot the bot also binds **satellite** (prefers `proxy_port + 10000`) and **Flash socket-policy** (prefers `proxy_port − 10000`) ports. If a preferred port is already in use, the next free port is chosen automatically and logged.

### Build `ban_bot.exe` (optional)

After **`pip install -r requirements.txt`** (see Install above), from the repo root:

```powershell
.\venv\Scripts\python.exe build_exe.py
```

**Important:** `caseus` is installed from GitHub via `requirements.txt`, and **`ban_bot.exe` must be built with the same Python** where `import caseus` and `import pak` work. If you use another interpreter to run `build_exe.py`, the frozen exe can start with `ModuleNotFoundError` for those packages. The spec aborts the build with a short message if they are missing.

That writes **`ban_bot.exe`** in the **repository root**. Run it from that folder so **`bot/config.py`** is found at `bot\config.py` next to the exe. Session logs go to **`log.txt`** in the same folder.

## How to use the bot

**Option A** — packet login from the proxy (no Flash UI automation) — is spelled out in root [`method.md`](method.md). The proxy always sends **`LoginPacket`** after **`SystemInformationPacket`** using each row’s **`username`** / **`password`** in `bot/config.py`. Run with **`--no-launch-flash`** if you do not want the bot to start Flash Player + `TFMProxyLoader.swf`.

**Prerequisite:** something must still open a **MAIN TCP** connection to each slot’s proxy (typically **`127.0.0.1:<proxy_port>`** from `bot/config.py`) and complete the handshake through **`SystemInformationPacket`** — for example Flash/loader started manually or by the bot. The bot does not create that connection by itself.

1. **Start the bot** from the repo root (often with **`--no-launch-flash`**):

   ```powershell
   .\venv\Scripts\python.exe -m bot --no-launch-flash
   ```

   Or: **`ban_bot.exe --no-launch-flash`** (exe must sit next to `bot\config.py` as usual). Omit **`--no-launch-flash`** to let the bot open Flash + loader **one slot at a time** on Windows (see `bot/flash_launch.py`).

2. The process prints which **proxy ports** are active. Connect each game client through **tfm-proxy-loader** (or equivalent) to the **main port** for that slot.

3. **Login:** the proxy injects credentials from `bot/config.py`. Logs should include **`LoginPacket sent upstream by proxy (packet login)`** and **`OK [slot …] logged in as …`** when **`LoginSuccessPacket`** arrives.

4. The bot **waits until every slot has logged in** (or until **`ALL_SLOTS_LOGIN_TIMEOUT_SEC`** in `bot/config.py`). It does not ask you to press Enter for that.

5. Enter the **target room** (the text you would type after `/room`, e.g. `*Racing1`).

6. Enter the **target player** as `nickname#tag` (e.g. `adrian#8912`).

7. The bot sends **`/room`** on each connected slot (with a small stagger), then **`/ban`** on each with a **random 1–2 s** gap between accounts (defaults configurable in `bot/config.py` via `BAN_DELAY_MIN_SEC` / `BAN_DELAY_MAX_SEC`).

8. After the round, it asks **`Ban someone else? (y/n)`**. Answer **`y`** to enter another room and target; **`n`** exits.

### What you should see

- **`OK [slot …] logged in as …`** when a client finishes logging in through the proxy (per-slot confirmation).
- Lines confirming **`/room`** and **`/ban`** sends per slot.
- Optional echoes when the server pushes chat or messages that look ban-related (see proxy handlers in `bot/ban_proxy.py`).

### Command-line flags

- **`--no-launch-flash`** — do not auto-start `flashplayer_32_sa_debug.exe` + `TFMProxyLoader.swf`.
- **`--no-kill-stale`** — do not try to kill processes already listening on your configured proxy ports (e.g. `python -m bot --no-kill-stale` or `ban_bot.exe --no-kill-stale`).

### Environment / Flash trust

If the game lives in a folder different from this repo, set **`TRANSFORMICE_GAME_DIR`** (or **`TFM_GAME_DIR`**) to the directory that contains **`Transformice.exe`**, then restart the bot and the game so Flash trust / loader paths stay consistent (see `ensure_flash_trust_config` in `bot/ban_proxy.py`).

## Spec mapping (`Initial-Idea.txt`)

| Requirement | Implementation |
|-------------|----------------|
| `config.py` with ~11 accounts + unique IP | `bot/config.py`: one row per client; unique `proxy_port`; unique `bind_ip` when set (Proxifier / VPN discipline) |
| Prompt for room + user | CLI `input()` in `bot/ban_cli.py` |
| Wait until all accounts logged in | `login_success_event` per slot; `ALL_SLOTS_LOGIN_TIMEOUT_SEC` |
| `/room` then `/ban` with 1–2 s jitter | `CommandPacket` in proxy; `ROOM_STAGGER_SEC`, `BAN_DELAY_*` in config |
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
| `bot/config.py` | Per-client `proxy_port` / `bind_ip` / accounts (local; gitignored by default) |
| `ban_bot.spec`, `build_exe.py`, `requirements.txt` | Build **`ban_bot.exe`** in the repo root |
| `ban_bot.exe` | Frozen Windows app (build output; gitignored) |
| `Initial-Idea.txt` | Original feature / difficulty notes |
| `method.md` | Option A: proxy packet login, `--no-launch-flash`, external MAIN TCP |
| `Transformice*.swf`, `Transformice.exe`, `TFMProxyLoader.swf` | Client / loader assets (as committed) |
