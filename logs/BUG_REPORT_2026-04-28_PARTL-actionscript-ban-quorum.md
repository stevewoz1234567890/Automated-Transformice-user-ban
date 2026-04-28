# Bug report: PARTL mass failures, ActionScript dialogs, degraded /ban quorum

**Date:** 2026-04-28  
**Context:** Sessions logged under `...\Automated-Transformice-user-ban\log.txt` (paths referencing `C:\Users\carls\Pictures\farm\...`), 14 slots, game version **920**, `TFMProxyLoader.swf`, `PACKET_AUTO_LOGIN=True`, `BOT_AUTO_PONG=both`.

## Summary

With **14 concurrent Flash clients**, most slots lose the MAIN upstream shortly after login (**PARTL**). Repeated **Adobe Flash Player — Error de ActionScript** dialogs are auto-dismissed hundreds of times per session. Ban rounds then run with only **2 live MAIN connections**, so **12 slots skip** `/ban`, far below typical in-room quorum needs (~11 distinct sends per `BOT_BAN_QUORUM_REPORTS`; see `docs/BAN_QUORUM_TRANSFORMICE.md`).

## Symptoms

1. **PARTL:** After login phase (and after PARTL retry rounds), **9–10 of 14** slots show **FAIL [PARTL]** — `conn=m=0`, upstream closed **`clean-eof`**, sometimes **`ConnectionResetError`**.
2. **MAIN lifetime:** Many **`MAIN session ended`** lines with **`reason=clean-eof`** during `startup`, `partl_retry`, `room_list`, `player_list`, `idle`, **`as_sweep`** — MAIN drops while server still sends pings/shop/tribulle traffic.
3. **ActionScript:** Continuous **`Error de ActionScript:`** dialogs (**`&Continuar`**); session counters on the order of **40–52** closures per run; login diagnostics report **`AS_dialogs_closed≈49`**, **`incorrect_version_dialogs=0`**, **`literal_version_in_swf=False`**, **`cfg_game_version='920'`**.
4. **Stagger:** Effective stagger reached **~3.9s** then **5.0s** (`BOT_UI_FLASH_LAUNCH_STAGGER_SEC` plus auto floor for 8+ / 14+ slots); PARTL persisted.
5. **Ban round:** **`Ban round: 12 slot(s) with no upstream (PARTL) — skipped; sending from 2 live slot(s)`**; quorum warning when only **2** `/ban` sends succeed.
6. **Misc:** Slot 14 satellite port **`50250` busy** → alternate **`50312`**; retry relaunch **`no fresh bind_ip`** reuses same bind IP.

## Likely causes (as inferred from logs + bot diagnostics)

| Area | Notes |
|------|--------|
| Loader / SWF runtime | Recurring ActionScript errors point to client/loader mismatch or unstable patched SWF behavior under load—not dismissed-dialog logic alone. |
| Concurrency | **14× Flash + MAIN/sat** overlap; stagger helps but does not eliminate PARTL when AS errors and CPU contention persist. |
| Retry churn | Sequential **WM_CLOSE** relaunches correlate with **additional** MAIN drops on slots that were briefly OK (cascade). |
| Quorum | Failure mode is **too few live MAIN sockets at ban time**, not necessarily packet formatting of `/ban`. |

## Recommended mitigations

1. Align **`TFM_PROXY_SWF`** / secrets / **`TFM_SECRETS_GAME_VERSION`** with live game build; investigate ActionScript root cause until dialog spam stops.
2. Reduce simultaneous load: **fewer slots** and/or raise **`BOT_UI_FLASH_LAUNCH_STAGGER_SEC`** above the automatic minimum until PARTL rate drops.
3. Treat **`BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC`** and leader-only join as mitigations for **room** phase only; they do not fix early PARTL from login/Flash overlap (as logged).
4. For ban effectiveness expectations, keep **`docs/BAN_QUORUM_TRANSFORMICE.md`** in sync with observed live sender counts.

## Related files

- `docs/BAN_QUORUM_TRANSFORMICE.md` — quorum expectations  
- `docs/LOG_WIFI_VS_MOBILE_ANALYSIS.md` — environmental notes if network varies  

---

*This report documents observed behavior from log analysis on 2026-04-28; not a root-cause proof for server-side kicks.*
