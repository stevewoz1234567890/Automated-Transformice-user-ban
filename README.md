# Automated Transformice user ban (CLI)

CMD tool that runs **one local Transformice proxy per game client**, then injects `/room` and `/ban` using the same **caseus** + **tfm-proxy-loader** approach as [transformice-bot](https://github.com/stevewoz1234567890/transformice-bot) (invite bot). The GitHub **web** URL for that repo may 404 if it is private; `git clone` still works when you have access.

## What you need

- Python **3.10+**
- **Transformice** standalone client, **TFMProxyLoader.swf**, **Transformice.swf** (see the transformice-bot README)
- **One game process per account**, each tfm-proxy-loader aimed at the **matching TCP port** from your config
- **Unique outbound IP per client** (e.g. **Proxifier** + proxies/VPN) — `bind_ip` in config is only a reminder of which IP you assigned

## Setup

```powershell
cd D:\work\Automated-Transformice-user-ban
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy bot\config.example.py bot\config.py
# Edit bot\config.py: proxy_port per slot, labels, bind_ip notes
```

## Run

From the repo root:

```powershell
.\venv\Scripts\python.exe -m bot
```

Or double-click `run_ban_bot.bat`.

1. Start every Transformice instance and log in (each through its proxy port).
2. When prompted, press **Enter** (or wait until all clients are detected).
3. Enter **target room** (text after `/room`, e.g. `*Racing1`) and **target user** (`nickname#tag`).
4. The bot sends `/room` on every connected slot, then `/ban` on each with a **1–2 s** random gap (configurable in `bot/config.py`).
5. After each round it asks whether to ban someone else.

You should see **`OK [slot …] logged in as …`** when a client finishes logging in through the proxy. Chat lines that mention “ban” are echoed when the server sends them.

## Flags

- **`--no-kill-stale`** — do not try to kill processes already listening on your proxy ports (pass on the command line after `-m bot`).

## Spec mapping (Initial-Idea.txt)

| Requirement | Implementation |
|-------------|----------------|
| `config.py` with ~11 accounts + unique IP | `bot/config.py`: one row per client; `bind_ip` documents Proxifier mapping; ports must be unique |
| Prompt room + user | CLI `input()` |
| `/room` then `/ban` with 1–2 s jitter | `CommandPacket` + `BAN_DELAY_*` |
| Per-action confirmation | Prints for each `/room` and `/ban` send |
| “OK” on login | `LoginSuccessPacket` handler |
| Loop “ban someone else?” | After each round |
| Game chat ban hint | `RoomMessagePacket` / `GeneralMessagePacket` listeners |

## Legal / ToS

Use only in line with **Transformice’s terms** and applicable law. This repository is for legitimate automation you are allowed to perform.

## Reference clone

A shallow clone of `transformice-bot` used while developing can be ignored: `_ref_transformice-bot/` (see `.gitignore`).
