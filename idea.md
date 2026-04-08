# Idea: CMD-only login without Flash / TFMProxyLoader

This document captures the architectural notes for running the ban bot **without** Flash Player and **without** TFMProxyLoader, while keeping everything **CLI-driven** where possible.

## What “no interface” can mean

There are **two different things**:

1. **No Flash Player / no TFMProxyLoader in your workflow** (nothing to click, nothing to patch-loader).
2. **No game client at all** — only Python in a terminal.

This project’s proxy is built around **one real game TCP client per slot** connecting to `127.0.0.1:<proxy_port>`. The bot only sits in the middle and (optionally) injects packets.

---

## Option A — Packet login, no Flash started by the bot

You can avoid launching Flash and the loader from the bot:

1. In `bot/config.py` set **`PACKET_AUTO_LOGIN = True`** and put **`username` / `password`** on each account row (same as for UI automation).
2. Run from CMD:

   ```powershell
   cd D:\work\Automated-Transformice-user-ban
   .\venv\Scripts\python.exe -m bot --no-launch-flash
   ```

**What this does:**

- **`--no-launch-flash`** — does not start `flashplayer_32_sa_debug.exe` or `TFMProxyLoader.swf`.
- **`PACKET_AUTO_LOGIN`** — after the normal login handshake and **`SystemInformationPacket`**, the **proxy** sends **`LoginPacket`** (no typing in a UI). See `docs/TRANSFORMICE_LOGIN_ANALYSIS.md` (“Packet-based auto-login”).

**What it does not do:**

- Something must still **open MAIN TCP** to the proxy and send **Handshake** → … → **SystemInformation** (same as the Flash path). The proxy does not invent that by itself.

So this is **CMD-driven orchestration + no Flash/loader from the bot**, but **not** “only Python, no client,” unless you add another connector (Option B).

---

## Option B — Truly headless (no Flash, no loader), all logic in CMD

**caseus** includes a **`Client`** class that behaves like the standalone game client (handshake, system info, login, satellite, etc.). In principle you can:

1. Start the bot with **`--no-launch-flash`** (proxies only).
2. Run a **separate** Python process that uses **`caseus.Client`** with **`Secrets`** targeting **`127.0.0.1`** and your **`proxy_port`** (instead of the live server ports directly).
3. Set **`PACKET_AUTO_LOGIN = False`** for that flow, because **`Client` already sends `LoginPacket`** after verification — you do **not** want the proxy to send a second login (`PACKET_AUTO_LOGIN` is for when the real client is Flash and you skip the UI).

You still need **valid `Secrets`** (game version, connection token, keys, etc.). The caseus API supports things like **`Secrets.load_from_dumper("tfm-secrets")`** or loading from a leaker SWF — see `caseus/secrets.py` in your venv. This repo does **not** ship a finished “headless connector” script; it would be new code on top of `caseus.Client`.

---

## Practical summary

| Goal | Settings / commands |
|------|---------------------|
| Bot never starts Flash or TFMProxyLoader | `python -m bot --no-launch-flash` |
| No typing in Flash; login as `LoginPacket` from proxy | `PACKET_AUTO_LOGIN = True` + credentials in config |
| Something still must connect to each `proxy_port` | Either another client you control, or a future **Python `caseus.Client`** helper with **`PACKET_AUTO_LOGIN` off** |

---

## Bottom line

**All CLI prompts** (`/room`, `/ban`, etc.) already run in CMD. **Skipping Flash and TFMProxyLoader** is done with **`--no-launch-flash`**. **Skipping the login UI** is done with **`PACKET_AUTO_LOGIN`**, but you still need a **TCP client** that completes the handshake up to **`SystemInformationPacket`**. A **fully headless** path means wiring **`caseus.Client`** (or similar) to the local proxy and turning **`PACKET_AUTO_LOGIN` off** so login is not sent twice.

---

## References (in this repo)

- `docs/TRANSFORMICE_LOGIN_ANALYSIS.md` — login flow, `PACKET_AUTO_LOGIN`, Flash UI automation.
- `bot/ban_proxy.py` — `LoginPacket` injection, duplicate login gating.
- `bot/ban_cli.py` — `--no-launch-flash`, `PACKET_AUTO_LOGIN` interaction with `FLASH_AUTO_LOGIN_UI`.
- `README.md` — install, config, proxy ports.
