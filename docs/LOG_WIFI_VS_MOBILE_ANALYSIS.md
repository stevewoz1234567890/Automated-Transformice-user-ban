# Wi‑Fi vs mobile hotspot logs (14 slots)

Comparison of two runs with the same upstream preflight (`51.38.60.113:12801` OK). Both show heavy PARTL churn under sequential Flash launch, overlapping CPU/MAIN load, and recurring Adobe Flash **Error de ActionScript** dialogs.

## What matched

- **Totals**: Roughly half the slots reach OK after the first login pass (Wi‑Fi: 6/14 OK vs mobile: 4/14 OK in the summaries you captured); remainder PARTL — same failure class, not a different root cause label.
- **Diagnostics**: Repeated `reason=clean-eof` on MAIN with `note=clean_eof:login_often=FLASH_STAGGER+AS;room_often=PRE_BAN+leader_not_for_login_batch`.
- **Network path to Transformice**: Not the bottleneck in either log; resets were clean TCP closes (`clean-eof` / `ConnectionResetError`), consistent with loader overload, handshake drop, or server-side close — not DNS failure.

## Mobile‑only signals

| Observation | Interpretation |
|-------------|----------------|
| **`Slot 13: satellite port 49900 was busy or reserved; using 49976`** | Another process (or ephemeral Windows use) occupied the preferred satellite port range; the bot already picks the next free port and logs it. Harmless if main + satellite rows stay consistent with Flash (verified in‐log via SAT redirect lines). |
| **Early MAIN delay on slot 1** (`no MAIN TCP … re-clicking …`) | Hotspot + higher latency or CPU contention: loader reached the proxy later; early re‑click logic already engaged. |
| **`ConnectionResetError('Connection lost')` on slot 2** | Typical of mobile CGNAT / radio idle or middlebox killing long‑idle TCP sooner than ethernet — same PARTL symptom with a clearer transport error string. |
| **Stuck PARTL retry on slot 10** (`Retry: slot 10 still waiting for login … MAIN_TCP=yes` then `MAIN session ended … login=n/a`) | MAIN connected, `LoginPacket` sent, then server closed before `LoginSuccessPacket`. The wait loop had no way to stop until the long retry timeout — **fixed in code** by signaling `login_aborted_event` when MAIN ends without login success. |

## Wi‑Fi‑only / stronger on Wi‑Fi

- **More `ConnectionResetError` with Spanish WinError 64** during Flash cleanup (Ctrl‑C): local stack message when killing Flash, not the game server.
- **Post‑login sweep** closed many AS error dialogs on Wi‑Fi run — same SWF/loader issue, more visible when more clients reached a late stage.

## Operational recommendations (unchanged from log footers)

1. Raise **`BOT_UI_FLASH_LAUNCH_STAGGER_SEC`** (floor already applies for 8+ slots) if mass PARTL persists.
2. Fix recurring **ActionScript** at source (loader/SWF / game version), not only auto‑dismiss.
3. On **mobile**, expect more transport resets; fewer simultaneous slots or longer stagger usually helps.

## Code fix (this repo)

- **`login_aborted_event`** on each slot: set when a MAIN session ends while `LoginSuccess` has not fired, so initial login and PARTL **retry** waits exit quickly instead of misleading `MAIN_TCP=yes` spin until `BOT_RETRY_LOGIN_TIMEOUT_SEC`.
