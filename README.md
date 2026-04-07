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

That writes **`ban_bot.exe`** in the **repository root**. Run it from that folder so **`bot/config.py`** is found at `bot\config.py` next to the exe. Session logs go to **`log.txt`** in the same folder.

## How to use the bot

1. **Start the bot** from the repo root: either run **`ban_bot.exe`**, or (after install):

   ```powershell
   .\venv\Scripts\python.exe -m bot
   ```

2. The process prints which **proxy ports** are active. For **each** configured slot, start **Transformice** through **tfm-proxy-loader** and point that loader at the **port** listed for that slot in `bot/config.py` (same idea as multi-slot invite bots).

3. **Log in** in each game window as usual (the bot does not type passwords; login happens in the client).

4. When the CLI says so, press **Enter** when all clients are ready, **or** wait until the tool detects that **every** slot has a connected client.

5. Enter the **target room** (the text you would type after `/room`, e.g. `*Racing1`).

6. Enter the **target player** as `nickname#tag` (e.g. `adrian#8912`).

7. The bot sends **`/room`** on each connected slot (with a small stagger), then **`/ban`** on each with a **random 1–2 s** gap between accounts (defaults configurable in `bot/config.py` via `BAN_DELAY_MIN_SEC` / `BAN_DELAY_MAX_SEC`).

8. After the round, it asks **`Ban someone else? (y/n)`**. Answer **`y`** to enter another room and target; **`n`** exits.

### What you should see

- **`OK [slot …] logged in as …`** when a client finishes logging in through the proxy (per-slot confirmation).
- Lines confirming **`/room`** and **`/ban`** sends per slot.
- Optional echoes when the server pushes chat or messages that look ban-related (see proxy handlers in `bot/ban_proxy.py`).

### Command-line flags

- **`--no-kill-stale`** — do not try to kill processes already listening on your configured proxy ports (e.g. `python -m bot --no-kill-stale` or `ban_bot.exe --no-kill-stale`).

### Environment / Flash trust

If the game lives in a folder different from this repo, set **`TRANSFORMICE_GAME_DIR`** (or **`TFM_GAME_DIR`**) to the directory that contains **`Transformice.exe`**, then restart the bot and the game so Flash trust / loader paths stay consistent (see `ensure_flash_trust_config` in `bot/ban_proxy.py`).

## Spec mapping (`Initial-Idea.txt`)

| Requirement | Implementation |
|-------------|----------------|
| `config.py` with ~11 accounts + unique IP | `bot/config.py`: one row per client; unique `proxy_port`; unique `bind_ip` when set (Proxifier / VPN discipline) |
| Prompt for room + user | CLI `input()` in `bot/ban_cli.py` |
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
| `Transformice*.swf`, `Transformice.exe`, `TFMProxyLoader.swf` | Client / loader assets (as committed) |
