# Method: Option B — Headless `caseus.Client` to the local proxy

This branch documents **Option B**: run the bot **without** launching Flash (`--no-launch-flash`), and use a **separate** Python process built on **`caseus.Client`** to connect to **`127.0.0.1:<proxy_port>`** with valid **`Secrets`**. The client performs the full login sequence (including **`LoginPacket`**); the proxy must **not** also inject login.

## Why not `PACKET_AUTO_LOGIN` here?

**`caseus.Client`** sends **`LoginPacket`** after **`ClientVerificationPacket`** when **`username`** is set. If **`PACKET_AUTO_LOGIN`** were **True**, the proxy could send **`LoginPacket`** earlier and you would risk **duplicate** logins (the proxy drops duplicates only in the `PACKET_AUTO_LOGIN` path — see `bot/ban_proxy.py`).

For Option B, set **`PACKET_AUTO_LOGIN = False`** in **`bot/config.py`**.

## Prerequisites

- Same Python env as the bot: **`pip install -r requirements.txt`** (includes **caseus** from GitHub).
- **`Secrets`** with real server parameters: **`game_version`**, **`connection_token`**, **`auth_key`**, **`packet_key_sources`**, **`client_verification_template`**, etc. Typical sources:
  - **`Secrets.load_from_dumper("tfm-secrets")`** if you have the **`tfm-secrets`** tool, or
  - **`Secrets.load_from_leaker_swf(...)`** (see **caseus** `secrets.py`), or
  - manual construction from a trusted dump.
- Point **`server_address`** to **`127.0.0.1`** (or your **`PROXY_BIND_HOST`**) and **`server_ports`** to **`(<main_proxy_port>,)`** — the same **main** port as the slot in **`bot/config.py`**.

## Bot side (proxies only)

```powershell
.\venv\Scripts\python.exe -m bot --no-launch-flash
```

Configure **`ACCOUNTS`** with **`proxy_port`** per slot; **`username` / `password`** are used by your **`Client`** script, not by Flash. Keep **`PACKET_AUTO_LOGIN = False`**.

## Client side (conceptual)

You run **one async `caseus.Client` per slot** (or stagger them), roughly:

1. Build **`Secrets`** → **`Client(secrets=..., username=..., password_hash=shakikoo(password), start_room=...)`**.
2. **`await client.start()`** (or equivalent **`startup` → `on_start`**) so the client connects to the **local proxy**, which forwards to the real server.

Exact wiring depends on your **caseus** version; refer to **`venv\Lib\site-packages\caseus\clients\client.py`** for **`startup`**, **`handshake`**, **`login`**, and satellite handling.

This repository **does not** yet ship a ready-made **`headless_connect.py`**; implementing it is application code on top of **caseus**.

## Satellite and policy ports

The live game uses **MAIN** + **satellite** (+ Flash policy for SWF). The proxy in this repo listens on **main**, **satellite**, and optionally a **shared Flash socket-policy** port. A Python **`Client`** must follow **`ChangeSatelliteServerPacket`** like the real client; **caseus** **`Client`** already implements **`_on_change_satellite_server`**. Ensure your **`BanBotProxy`** satellite rewrite (see **`ban_proxy.py`**) stays consistent with local ports.

## See also

- Root **`idea.md`** — Option A vs B and limitations.
- **`docs/TRANSFORMICE_LOGIN_ANALYSIS.md`** — handshake / login packet order.
- **caseus** **`Secrets`**, **`Client`** — `venv\Lib\site-packages\caseus\`.
