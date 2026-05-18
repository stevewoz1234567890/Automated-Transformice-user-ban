# Automated Transformice user ban

## 1. Purpose of this bot

Automate coordinated **in-room bans** on [Transformice](https://www.transformice.com/) using **many accounts at once**.

The bot runs one **local TCP proxy per account** (built on [caseus](https://github.com/friedkeenan/caseus)). Each proxy sits between a game client and the real game servers. After every slot is logged in, you pick a **room** and a **player**; the bot sends **`/room`** then **`/ban`** from all live slots, with small delays so reports land like distinct players (the game often needs on the order of **~11** separate `/ban` reports for a room ban).

Configuration lives in repo-root **`.env`**: `BOT_ACCOUNTS_JSON` (credentials + per-slot `proxy_port`), `TFM_SECRETS_*` (crypto for login packets), and many `BOT_*` toggles in `bot/bot_env_defaults.py`.

**Branches target two different client styles:**

| Branch family | How clients connect |
|---------------|---------------------|
| **`main`** (current default) | **Headless** — built-in caseus TCP clients per slot (`python -m bot --headless`). No Flash Player launched by the bot. |
| **`separate`**, **`ui`**, stashed **`build/cli-transformice`** | **Flash + proxy** — `flashplayer_32_sa_debug.exe` + `TFMProxyLoader.swf` per slot; proxy injects `LoginPacket` after `SystemInformationPacket` (`BOT_PACKET_AUTO_LOGIN`). |

---

## 2. What I have tried

### Core stack (all branches)

- **caseus + pak** for protocol, local `BanBotProxy` per slot, secrets from **TFMSecretsLeaker.swf** and/or **`tfm-secrets`** CLI into `.env` / `tfm-secrets.json`.
- **Multi-account JSON** in `.env` (up to **14** rows tested), auto-assigned `proxy_port` ranges, optional per-row `bind_ip` (reference for multi-WAN / SOCKS; not required if you use one home IP).
- **Upstream TCP preflight** to `TFM_SECRETS_SERVER_ADDRESS` before proxies start (`python -m bot.upstream_probe`).
- **`python -m bot.validate_accounts`** — headless login check per row.
- Interactive flow: wait for all logins → room name → player nickname → staggered `/ban` → “ban someone else?”.

### `main` branch

- **Headless-only** workflow: `--headless`, built-in clients, no Flash auto-launch.
- Auto **tfm-secrets / leaker** refresh, persist to `.env`, sync `BOT_UPSTREAM_*` from dump.
- Upstream probe **abort** when all ports fail; **WinError 121** stagger between slots; stop after consecutive login failures.
- Commit note: **“success with only one account”** on headless path.
- Reverted a larger change that moved more behavior into `.env` auto room-list / auto-detect login (commit reverted on `main`).

### `separate` / `ui` (large Flash + proxy line — ~100+ commits ahead of `main`)

- **Flash Player** auto-launch per slot, sequential login (`BOT_UI_SEQUENTIAL_LOGIN`), stagger tuned for 8–14 slots.
- **Packet auto-login** from proxy; **BOT_AUTO_PONG=both** so server pings survive while Flash is slow or not fully in-game.
- **TFMProxyLoader.swf** download/refresh from GitHub; **ZWS patching** (ports, neutralize hard-coded IPs) to reduce Flash **#2048**; local HTTP server for game SWF (`x_forteresse/Transformice.swf` cache).
- **info.php mirror** + `hosts` hint for `51.158.113.197`.
- **Win32 UI automation**: loader click (keyboard / BM_CLICK), ActionScript “Dismiss all”, login-skip on “already connected”, **MAIN_DROP_LOGIN_RELOAD** when MAIN closes before `LoginSuccess`.
- **ISSUE1 forensic** logging (handshake `game_version` vs `TFM_SECRETS_GAME_VERSION`, MAIN close diagnostics).
- **Ban audit** JSONL, room/player menus from server, leader-only `JoinRoom`, burst `/ban`, PARTL retry, session markdown reports under `logs/`.
- **ProxyBridge** slot maps + optional SOCKS JSON generation (eval ProxiFyre/FlowProxy later removed from tree).
- **Docker** parity experiments (later removed).

### `multi-login`

- **Parallel headless** login (`BOT_HEADLESS_PARALLEL_LOGIN`) vs sequential.
- **Per-account spawned consoles** on Windows for isolated runs / single-slot index mode.
- Non-blocking upstream connect gate, throttling concurrent TCP handshakes (mitigate WinError 121).

### `UI-Allow`

- **Flash-only** bot: headless path removed; `--ui` / sequential UI login focus.

### `feature/alternative-game-clients`

- **Per-account SOCKS5** in JSON for different exit IPs on `/ban`.
- Fixes: **`/ban` via MAIN not satellite**, room-join race, satellite retry/fallback, trustable ban vote logging.

### `build/cli-transformice` (branch + stash — not on `main`)

- **PyInstaller** `ban_bot.exe` build (`build_exe.py`, `ban_bot.spec`); exclude `caseus.sniffers` for PyInstaller 6 on Python 3.10.
- Extra modules: `upstream_socks`, `proxybridge_manager`, `loader_http_preflight`, `tfm_game_swf_cache`, `login_skip`, etc.
- Reference workflow from [xaaxxaxaxaxaxa/cli-transformice](https://github.com/xaaxxaxaxaxaxa/cli-transformice) (same caseus/pak stack).
- Live tests on this line (Flash path, 1 slot, `BOT_BASELINE_MAX_SLOTS=1`):
  - Refreshed **TFMProxyLoader.swf** from GitHub and re-ran **TFMSecretsLeaker** (new `connection_token` / `auth_key` in `.env`).
  - **Main server** `51.38.60.113` — all game ports OK (~0.2 s).
  - **Satellite** `188.165.225.27` — **all ports timeout** (ping works, TCP does not).
  - Repeated **MAIN_DROP_LOGIN_RELOAD**; no `LoginSuccess` in `log.txt`.
  - Handshake warning: `TFM_SECRETS_GAME_VERSION=924` vs `HandshakePacket.game_version=66`.
- **No Proxifier** on the test machine — outbound is plain OS routing only.

### Operational scripts tried

- `scripts/fix_network_windows.ps1` (admin: hosts + firewall allow for `venv\Scripts\python.exe`) — satellite probe still failed without network change.
- `scripts/check_outbound_ip.py`, `scripts/generate_proxybridge_port_rules.py`.

---

## 3. Open problems

1. **Satellite TCP unreachable from this PC**  
   After MAIN handshake, the game directs traffic to satellite hosts (e.g. **`188.165.225.27:13801`**). `python -m bot.upstream_probe 188.165.225.27 13801 12801` **times out** on every port, while **`51.38.60.113` succeeds**. Login cannot complete until satellite TCP works (try **another network**, **phone hotspot**, or a **system VPN** — not per-app Proxifier). Re-probe before each bot run.

2. **Secrets / loader / handshake mismatch**  
   Logs show **`ISSUE1_HANDSHAKE_GV_MISMATCH`**: env `TFM_SECRETS_GAME_VERSION=924` vs Flash `HandshakePacket.game_version=66`, then **clean-eof** ~1 s after proxy `LoginPacket`. Loader SWF cannot be proven to embed version 924 from bytes alone. Need a **known-good `TFMProxyLoader.swf` + secrets dump from the same live Transformice build** (or a loader URL that matches current game).

3. **`main` vs Flash branches diverged**  
   **`main`** is headless-focused and **missing** most Flash automation, loader patching, and recent proxy fixes that live on **`separate`** / **`ui`**. Stashed WIP on **`build/cli-transformice`** is not merged. Pick one line of development or merge before expecting one README/workflow to match the code.

4. **14-slot Flash login not stable in practice**  
   On the Flash branch, slot 1 alone did not reach **`LoginSuccess`** in recent runs (satellite + handshake issues). Mass **PARTL**, ActionScript dialogs, and reload loops were mitigated in code but not eliminated in production tests.

5. **Multi-IP `/ban` quorum not validated here**  
   `feature/alternative-game-clients` adds **SOCKS5 per slot** for distinct exit IPs; not wired or tested on the current machine (no SOCKS URLs in `.env`).

6. **Legal / ToS**  
   Use only where Transformice’s terms and applicable law allow this automation.
