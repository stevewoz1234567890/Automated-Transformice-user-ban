"""
Default process environment for the ban bot.

The repo root ``.env`` may contain only ``BOT_ACCOUNTS_JSON``. On startup,
``env_setup.prepare_runtime_environment`` loads that file, optionally merges
``tfm-secrets.json``, then applies :data:`DEFAULT_PROCESS_ENV` with
:data:`setdefault <os.environ.setdefault>` — **``.env`` and JSON override these
coded defaults**, except accounts are reconciled specially (see ``apply_process_env_defaults``).
Edit defaults here only for repo-wide baseline behavior.
"""
from __future__ import annotations

import os
from typing import Final

# Used when .env is missing a non-empty BOT_ACCOUNTS_JSON (e.g. dev bootstrap only).
DEFAULT_ACCOUNTS_JSON: Final = (
    '[{"label":"1","proxy_port":38291,"bind_ip":"127.0.0.1","username":"","password":""}]'
)

# All values are strings. This dict intentionally does not set BOT_ACCOUNTS_JSON
# (restored in apply). Edit TFM and BOT_* here for your machine.
DEFAULT_PROCESS_ENV: dict[str, str] = {
    # Game crypto (headless; can be refreshed at runtime by tfm-secrets / dumper)
    "TFM_SECRETS_SERVER_ADDRESS": "51.38.60.113",
    "TFM_SECRETS_SERVER_PORTS": "12801,13801,14801,11801",
    "TFM_SECRETS_GAME_VERSION": "920",
    "TFM_SECRETS_CONNECTION_TOKEN": "XieNbK",
    "TFM_SECRETS_AUTH_KEY": "3415003",
    "TFM_SECRETS_PACKET_KEY_SOURCES": "2,50,57,19,60,20,56,8,7,45,98,74,90,106,97,75,100,76,80,96",
    "TFM_SECRETS_CLIENT_VERIFICATION_TEMPLATE": "aabbccdd00032d623700022d2daabbccdd00072d60602d2d2c2cfd4300072d2d2f2f2f762f0007000003340000238d00001f48170722202400032b2b2b000000",
    # When true (default), missing TFM_SECRETS_* are filled from repo-root ``tfm-secrets.json`` (after .env).
    "BOT_MERGE_TFM_SECRETS_JSON": "true",
    # Empty = ``<repo>/tfm-secrets.json``; otherwise path (absolute or relative to repo root).
    "BOT_TFM_SECRETS_JSON_PATH": "",
    "BOT_STRICT_LOADER_VERSION_CHECK": "false",
    "BOT_WARN_UNVERIFIED_LOADER_VERSION": "true",
    "BOT_PURGE_LEGACY_LOADER_PATCH_CACHE": "true",
    "BOT_BAN_DELAY_MIN_SEC": "1.0",
    "BOT_BAN_DELAY_MAX_SEC": "2.0",
    "BOT_BAN_BURST_MODE": "true",
    "BOT_BAN_PRESEND_STABILIZE_SEC": "0.35",
    "BOT_ROOM_STAGGER_SEC": "0.15",
    "BOT_ROOM_LIST_TIMEOUT_SEC": "10.0",
    "BOT_ROOM_LIST_MAX_SLOT_ATTEMPTS": "3",
    "BOT_PLAYER_LIST_COLLECT_TIMEOUT_SEC": "25.0",
    "BOT_UPSTREAM_WAIT_SEC": "20.0",
    "BOT_PLAYER_LIST_JOIN_LEADER_ONLY": "true",
    "BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC": "1.5",
    "BOT_BAN_PRE_ROUND_DISMISS_FLASH": "true",
    "BOT_ALL_SLOTS_LOGIN_TIMEOUT_SEC": "7200",
    "BOT_PROXY_VERBOSE_LOGIN_FLOW": "false",
    "BOT_PROXY_LOG_ALL_MAIN_PACKETS": "false",
    # Timeline-style INFO steps ``[trace #N | phase | slot]`` + full bot DEBUG to log.txt (see bot/trace_log.py).
    "BOT_DEBUG_TRACE": "false",
    # Mirror DEBUG to stderr as well as log.txt (noisy).
    "BOT_DEBUG_TRACE_CONSOLE": "false",
    # Empty = INFO for root; DEBUG upgrades bot.* detail to log.txt when combined with trace/debug_trace logic.
    "BOT_LOG_LEVEL": "",
    # Per-slot asyncio.run threads: log slow callbacks / enable asyncio debug logger when true.
    "BOT_ASYNCIO_DEBUG": "false",
    "BOT_ASYNCIO_SLOW_CALLBACK_SEC": "0.05",
    "BOT_SESSION_REPORT": "1",
    "BOT_PROXY_LOGIN_DIAGNOSTICS": "true",
    "BOT_PACKET_LOGIN_DELAY_SEC": "0.35",
    "BOT_PACKET_AUTO_LOGIN": "true",
    "BOT_MAIN_KEEPALIVE_INTERVAL_SEC": "15.0",
    "BOT_PACKET_LOGIN_START_ROOM": "",
    "BOT_PROXY_UPSTREAM_CONNECT_DIAG": "true",
    "BOT_UPSTREAM_CONNECT_SHUFFLE_PORTS": "false",
    "BOT_PROXY_BIND_HOST": "",
    "BOT_PROXY_LISTEN_USE_ACCOUNT_BIND_IP": "false",
    "BOT_SHARED_FLASH_SOCKET_POLICY_PORT": "10801",
    "BOT_FLASH_SOCKET_POLICY_BIND_HOST": "",
    "BOT_AUTO_PONG": "both",
    "BOT_SPARE_BIND_IPS": "",
    "BOT_HEADLESS_AUTO_LOGIN": "false",
    "BOT_HEADLESS_SECRETS_DOTENV_PATH": ".env",
    "BOT_HEADLESS_SECRETS_SEED_DOTENV_FROM_EXAMPLE": "true",
    "BOT_HEADLESS_SECRETS_ENV_PREFIX": "TFM_SECRETS_",
    "BOT_HEADLESS_SECRETS_INLINE_JSON": "",
    "BOT_HEADLESS_SECRETS_DUMPER": "",
    "BOT_HEADLESS_SECRETS_AUTO_DUMPER": "true",
    "BOT_HEADLESS_SECRETS_AUTO_LEAKER_SWF": "true",
    "BOT_HEADLESS_SECRETS_AUTO_PIP_BEFORE_DUMPER": "true",
    "BOT_HEADLESS_SECRETS_PERSIST_DUMP_TO_DOTENV": "true",
    "BOT_HEADLESS_SECRETS_DUMPER_TIMEOUT_SEC": "120",
    "BOT_HEADLESS_SECRETS_ALWAYS_REFRESH": "true",
    "BOT_UPSTREAM_AUTO_SYNC_FROM_SECRETS": "true",
    "BOT_UPSTREAM_FROM_SECRETS_DUMP_ONLY": "true",
    "BOT_UPSTREAM_PORTS_MATCH_DUMP_ORDER": "true",
    "BOT_UPSTREAM_STRICT_MATCH_SECRETS_DUMP": "false",
    "BOT_UPSTREAM_ALLOW_ADDRESS_MISMATCH": "false",
    "BOT_UPSTREAM_SERVER_ADDRESS": "51.38.60.113",
    "BOT_UPSTREAM_SERVER_PORTS": "12801,13801,14801,11801",
    "BOT_UPSTREAM_MAIN_GAME_PORT_TRY_FIRST": "11801",
    "BOT_UPSTREAM_TCP_PROBE_BEFORE_HEADLESS": "false",
    "BOT_UPSTREAM_ABORT_ON_PROBE_ALL_FAILED": "false",
    # Startup: DNS + local IPv4 hint + HTTP_PROXY note + multi-port TCP (see bot.net_preflight). Set false for legacy single-port-only check.
    "BOT_NET_PREFLIGHT_EXTENDED": "true",
    # Known-good PC parity (see README): optional one-shot defaults via BOT_KNOWN_GOOD_PARITY_MODE.
    "BOT_KNOWN_GOOD_PARITY_MODE": "false",
    # Use only first N rows of BOT_ACCOUNTS_JSON (unique proxy_port each); 0 = use full list.
    "BOT_BASELINE_MAX_SLOTS": "0",
    # Fail startup unless every configured game port accepts TCP (stricter than default any-port-OK).
    "BOT_NET_PREFLIGHT_REQUIRE_ALL_PORTS": "false",
    # Log [parity] checklist at startup (also implied when BOT_BASELINE_MAX_SLOTS > 0).
    "BOT_PARITY_STARTUP_REMINDERS": "false",
    "BOT_UPSTREAM_PROBE_TIMEOUT_SEC": "6",
    "BOT_UPSTREAM_PROBE_RETRIES": "2",
    "BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC": "3",
    "BOT_UPSTREAM_PROBE_FINAL_LONG_TIMEOUT": "true",
    "BOT_UPSTREAM_PROBE_FINAL_TIMEOUT_CAP_SEC": "35",
    "BOT_UPSTREAM_PROBE_FINAL_TIMEOUT_FLOOR_SEC": "20",
    "BOT_UPSTREAM_MAX_CONCURRENT_CONNECTS": "2",
    "BOT_UPSTREAM_OPEN_CONNECTION_TIMEOUT_SEC": "12",
    # Bind outbound TCP to a local IPv4 (multi-WAN / Proxifier per-source routing).
    "BOT_UPSTREAM_LOCAL_BIND_IPV4": "",
    "BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET": "false",
    # After default-route preflight fails, retry probe binding each distinct row bind_ip (reads BOT_ACCOUNTS_JSON).
    "BOT_NET_PREFLIGHT_TRY_ACCOUNT_BIND_IPS": "false",
    "BOT_HEADLESS_CONNECT_TO_SATELLITE": "true",
    "BOT_HEADLESS_EXIT_AFTER_LOGIN_SUCCESS": "true",
    "BOT_HEADLESS_LOGIN_STAGGER_SEC": "2.5",
    "BOT_HEADLESS_PARALLEL_LOGIN": "true",
    "BOT_HEADLESS_PARALLEL_START_STAGGER_SEC": "0.4",
    "BOT_HEADLESS_PARALLEL_RETRY_FAILED_SLOTS": "true",
    "BOT_HEADLESS_PARALLEL_RETRY_AFTER_SEC": "90",
    "BOT_HEADLESS_PARALLEL_RETRY_STAGGER_SEC": "5",
    "BOT_HEADLESS_SEQUENTIAL_EARLY_EXIT_ON_WIN121": "true",
    "BOT_HEADLESS_STOP_AFTER_CONSECUTIVE_LOGIN_FAILURES": "3",
    "BOT_HEADLESS_STAGGER_WIN121_EXTRA_SEC": "4",
    "BOT_HEADLESS_STAGGER_MAX_SEC": "15",
    "BOT_PIP_INSTALL_CASEUS_GIT_UPGRADE": "false",
    "BOT_CASEUS_GIT_PIP_SPEC": "caseus @ git+https://github.com/friedkeenan/caseus.git",
    "BOT_PIP_INSTALL_TFM_SECRETS_CLI": "false",
    "BOT_TFM_SECRETS_PIP_INSTALL_SPEC": "",
    # Typical in-game quorum for a room ban (distinct reports); warn when live sends drop below — docs/BAN_QUORUM_TRANSFORMICE.md
    "BOT_BAN_QUORUM_REPORTS": "11",
    "BOT_RETRY_FAILED_SLOTS": "true",
    "BOT_RETRY_MAX_ATTEMPTS": "3",
    # Shorter waits = snappier PARTL recovery (was 3.0 / 2.0; raise slightly if retries stampede OK slots).
    "BOT_RETRY_DELAY_SEC": "1.25",
    "BOT_RETRY_LOGIN_TIMEOUT_SEC": "120.0",
    "BOT_RETRY_BETWEEN_SLOT_SEC": "2.0",
    # 0 = skip post-login Adobe AS sweep entirely (fixes logs where MAIN dies with phase=as_sweep).
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_PASSES": "3",
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_DELAY_SEC": "0.4",
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_LEAD_SEC": "1.0",
    # Sweep-only (issue 1): never BM_CLICK sole Continuar by default — that matches a manual click
    # and can drop MAIN (operator_phase=as_sweep). Use WM_CLOSE on the Adobe popup instead.
    "BOT_POST_LOGIN_AS_SWEEP_CONTINUE_IF_SOLE_OPTION": "false",
    "BOT_POST_LOGIN_AS_SWEEP_USE_WMCLOSE": "true",
    # Sweep: Adobe AS popups — Esc first (keyboard), WM_CLOSE fallback if modal stays open—
    # BM_CLICK ranked buttons still dropped MAIN in multi-locale logs (phase=as_sweep).
    "BOT_POST_LOGIN_AS_SWEEP_ADOBE_ESCAPE_WMCLOSE_ONLY": "true",
    # Optional spacing (seconds) between slots within each sweep pass — spreads WM_CLOSE in time.
    "BOT_POST_LOGIN_AS_SWEEP_INTER_SLOT_PAUSE_SEC": "0",
    "BOT_UI_AUTO_LAUNCH_FLASH": "true",
    "BOT_UI_FLASH_PLAYER_PATH": "",
    # Default pairs with auto floor in ban_cli._effective_flash_stagger_sec (8+ slots). Higher = fewer
    # overlapping MAIN handshakes / AS crashes on large farms; lower only on fast single-IP setups.
    "BOT_UI_FLASH_LAUNCH_STAGGER_SEC": "3.5",
    "BOT_UI_SEQUENTIAL_LOGIN": "false",
    "BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC": "120",
    "BOT_FLASH_AUTO_LOGIN_UI": "false",
    # Before Flash session: tfm-secrets / leaker (if BOT_HEADLESS_SECRETS_ALWAYS_REFRESH) + optional TFMProxyLoader.swf fetch.
    "BOT_FLASH_STARTUP_REFRESH_TFM_ASSETS": "true",
    "BOT_PERSIST_REFRESHED_SECRETS_JSON": "true",
    "BOT_REFRESH_SECRETS_IN_FROZEN_BUILD": "",
    "BOT_FLASH_AUTO_FETCH_PROXY_LOADER": "true",
    # After TFMSecretsLeaker/tfm-secrets updates .env: re-download loader unless disabled below.
    "BOT_FLASH_FETCH_LOADER_AFTER_SECRETS_REFRESH": "true",
    # When true: only re-fetch loader when TFM_SECRETS_GAME_VERSION string changes (fewer downloads; can leave stale SWF).
    # Default false: GAME_VERSION often stays identical while dumped secrets still mismatch disk loader ⇒ AS/PARTL.
    "BOT_FLASH_FETCH_LOADER_AFTER_SECRETS_IF_VERSION_CHANGED": "false",
    "BOT_FLASH_REFRESH_PROXY_LOADER_EACH_RUN": "false",
    "BOT_FLASH_FETCH_PROXY_LOADER_IF_MISSING": "true",
    # Exact https URL for proxy loader SWF (overrides BOT_TFM_PROXY_LOADER_GITHUB_REPO). Use when releases lag
    # live Transformice; pairs with startup alignment WARN + MAIN ``ISSUE1_LOADER_PREFLIGHT=`` lines.
    "BOT_TFM_PROXY_LOADER_DOWNLOAD_URL": "",
    "BOT_TFM_PROXY_LOADER_GITHUB_REPO": "friedkeenan/tfm-proxy-loader",
    "BOT_TFM_PROXY_LOADER_ASSET_NAME": "TFMProxyLoader.swf",
    # Rewrite plaintext game-server IPv4 in the patched loader LZMA body to space-padded 127.0.0.1
    # (stops Flash #2048 sandbox: file:// patched SWF touching bare TFM_UPSTREAM_IP:port literals).
    "BOT_LOADER_NEUTRALIZE_UPSTREAM_IP_LITERALS": "true",
    # After replacing TFMProxyLoader.swf: delete tmp/loader_patch/*.swf so Flash repatches from new source bytes.
    "BOT_PURGE_ALL_LOADER_PATCH_CACHE_ON_LOADER_INSTALL": "true",
    "FLASH_MINIMIZE_AFTER_OPEN": "true",
    "BOT_FLASH_LOADER_EARLY_RETRY_INTERVAL_SEC": "2.0",
    "BOT_FLASH_LOADER_EARLY_RETRY_COUNT": "5",
    "BOT_FLASH_CLOSE_ON_LOGIN_FAIL": "true",
    "BOT_FLASH_CLOSE_GRACE_SEC": "2.0",
    "BOT_FLASH_EMBEDDED_IV_LOGIN_RELOAD": "true",
    "BOT_FLASH_EMBEDDED_IV_AFTER_HANDSHAKE_SEC": "38",
    "BOT_FLASH_EMBEDDED_IV_AFTER_MAIN_TCP_SEC": "72",
    "BOT_FLASH_EMBEDDED_IV_RELOAD_PAUSE_SEC": "1.25",
    "BOT_FLASH_EMBEDDED_IV_LOGIN_MAX_RELOAD_PER_SLOT": "20",
    "FLASH_ERROR_DISMISS_ALLOW_CONTINUE": "false",
    "FLASH_ERROR_DISMISS_USE_WMCLOSE": "false",
    "FLASH_ERROR_DISMISS_CONTINUE_IF_SOLE_OPTION": "true",
    "FLASH_FLASHPLAYER_ERROR_DISMISS_POLL_SEC": "3.0",
    "BOT_FLASH_FOCUS_PUMP": "1",
    "BOT_FLASH_FOCUS_PUMP_MS": "600",
    "BOT_FLASH_TILE_W": "80",
    "BOT_FLASH_TILE_H": "60",
    "BOT_FLASH_DIAG_KEEP_ONSCREEN": "false",
    "BOT_VALIDATE_ACCOUNTS_PROXY_PORT": "",
    "BOT_PROXY_HEARTBEAT_LOG_EVERY": "",
    "BOT_PROXY_LOG_BIND_DETAIL": "false",
    "BOT_PROXY_MAIN_CLOSE_DIAG": "true",
    "BOT_PROXY_MAIN_CLOSE_VERBOSE": "false",
    # Extra tcp_side_guess + sess_counts on MAIN close (set false to shorten lines).
    "BOT_PROXY_MAIN_RC_HINT": "true",
    # Optional: override caseus Proxy HandshakePacket loader_stage_size sent upstream (default 0x1FBD = 8125).
    # Set only if caseus/Tfm server mismatch is proven; wrong value breaks handshake.
    "BOT_PROXY_HANDSHAKE_LOADER_STAGE_SIZE": "",
    # One-shot "issue 1" hunt: correlates Handshake game_version vs TFM_SECRETS_GAME_VERSION, MAIN teardown
    # vs upstream socket + loader sha, AS first fingerprint — forces ROOT_CAUSE_MAIN_CLOSE + verbose login.
    "BOT_ISSUE1_FORENSIC": "false",
    # Always-on (unless 0): seconds — MAIN close diagnostic gains issue1_near_as_dismiss= when Flash dismiss was recent.
    "BOT_ISSUE1_AS_MAIN_CORR_WINDOW_SEC": "12",
    # Per-dismiss ISSUE1_AS_DISMISS_ACTION WARNING lines (spammy); forensic mode defaults this on when unset.
    "BOT_ISSUE1_AS_DISMISS_LOG": "false",
    # Sweep markers ISSUE1_SWEEP_BEGIN / ISSUE1_SWEEP_END (set false to hide).
    "BOT_ISSUE1_SWEEP_BOUNDARY_LOG": "true",
    # Per MAIN TCP: INFO ISSUE1_HANDSHAKE_PROBE compares HandshakePacket.game_version vs TFM_SECRETS_GAME_VERSION (set false to quiet).
    "BOT_ISSUE1_HANDSHAKE_PROBE": "true",
    # Deep clean-eof hunt: WARNING ROOT_CAUSE_MAIN_CLOSE on every MAIN end (Flash TCP, rings, AS-dismiss delta).
    "BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE": "false",
    # Last N packet labels per direction on MAIN (raise during ROOT_CAUSE runs; default 8).
    "BOT_PROXY_MAIN_PACKET_RING": "8",
    # Click Permitir / Allow on Adobe Flash Player *privacy* dialogs (Local Storage), not Esc.
    "FLASH_LOCAL_STORAGE_PERMISSION_DISMISS": "true",
    # Prefer SetForegroundWindow + keybd_event Esc before PostMessage Esc (Unset=true).
    "FLASH_ERROR_DISMISS_ESC_USE_FOREGROUND": "true",
    # After Esc bursts: wait before checking if Adobe error dialog HWND vanished (milliseconds).
    "FLASH_ERROR_ESC_POST_POLL_MS": "180",
    # Longer AS dialog text in logs (max 8000).
    # Default raised so nested-dialog #2048 / #2044 lines appear in WARN lines without extra env.
    "FLASH_ERROR_DISMISS_BODY_LOG_CHARS": "1600",
    # First-seen fingerprint preview length (default 1400; max 12000).
    "FLASH_ERROR_FIRST_FP_PREVIEW_CHARS": "1400",
    # Log a second WARNING line with remaining body after preview (same first fingerprint only).
    "FLASH_ERROR_LOG_FULL_BODY_FIRST_FP": "false",
}


def apply_process_env_defaults() -> None:
    """
    Fill **missing** ``os.environ`` keys from :data:`DEFAULT_PROCESS_ENV` via
    :meth:`setdefault <os.environ.setdefault>` so values from ``.env``,
    optional ``tfm-secrets.json`` merge, or the parent shell survive.

    Then restore ``BOT_ACCOUNTS_JSON`` from whatever was present before this
    loop (typically from ``.env``); if absent, assign :data:`DEFAULT_ACCOUNTS_JSON`.
    """
    saved = (os.environ.get("BOT_ACCOUNTS_JSON") or "").strip()
    for k, v in DEFAULT_PROCESS_ENV.items():
        if k != "BOT_ACCOUNTS_JSON":
            os.environ.setdefault(k, v)
    if saved:
        os.environ["BOT_ACCOUNTS_JSON"] = saved
    else:
        os.environ["BOT_ACCOUNTS_JSON"] = DEFAULT_ACCOUNTS_JSON
