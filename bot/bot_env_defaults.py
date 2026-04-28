"""
Default process environment for the ban bot.

The repo root ``.env`` may contain only ``BOT_ACCOUNTS_JSON``. On startup,
``env_setup.prepare_runtime_environment`` loads that file, then this module
fills ``os.environ`` from :data:`DEFAULT_PROCESS_ENV` (and restores accounts).
Edit this module to change non-account behavior.
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
    "BOT_STRICT_LOADER_VERSION_CHECK": "false",
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
    "BOT_UPSTREAM_PROBE_TIMEOUT_SEC": "6",
    "BOT_UPSTREAM_PROBE_RETRIES": "2",
    "BOT_UPSTREAM_PROBE_RETRY_PAUSE_SEC": "3",
    "BOT_UPSTREAM_PROBE_FINAL_LONG_TIMEOUT": "true",
    "BOT_UPSTREAM_PROBE_FINAL_TIMEOUT_CAP_SEC": "35",
    "BOT_UPSTREAM_PROBE_FINAL_TIMEOUT_FLOOR_SEC": "20",
    "BOT_UPSTREAM_MAX_CONCURRENT_CONNECTS": "2",
    "BOT_UPSTREAM_OPEN_CONNECTION_TIMEOUT_SEC": "12",
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
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_PASSES": "3",
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_DELAY_SEC": "0.4",
    "BOT_POST_LOGIN_ACTIONSCRIPT_SWEEP_LEAD_SEC": "1.0",
    "BOT_UI_AUTO_LAUNCH_FLASH": "true",
    "BOT_UI_FLASH_PLAYER_PATH": "",
    # Slightly higher default spreads Flash/CPU load (fewer PARTL → less time in retry). Override down if you accept more failures.
    "BOT_UI_FLASH_LAUNCH_STAGGER_SEC": "2.5",
    "BOT_UI_SEQUENTIAL_LOGIN": "false",
    "BOT_UI_SEQUENTIAL_LOGIN_TIMEOUT_SEC": "120",
    "BOT_FLASH_AUTO_LOGIN_UI": "false",
    "FLASH_MINIMIZE_AFTER_OPEN": "true",
    "BOT_FLASH_LOADER_EARLY_RETRY_INTERVAL_SEC": "2.0",
    "BOT_FLASH_LOADER_EARLY_RETRY_COUNT": "5",
    "BOT_FLASH_CLOSE_ON_LOGIN_FAIL": "true",
    "BOT_FLASH_CLOSE_GRACE_SEC": "2.0",
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
}


def apply_process_env_defaults() -> None:
    """
    Set ``os.environ`` from :data:`DEFAULT_PROCESS_ENV`, then restore
    ``BOT_ACCOUNTS_JSON`` from whatever was read from the user ``.env`` (if non-empty);
    otherwise use :data:`DEFAULT_ACCOUNTS_JSON`.
    """
    saved = (os.environ.get("BOT_ACCOUNTS_JSON") or "").strip()
    for k, v in DEFAULT_PROCESS_ENV.items():
        os.environ[k] = v
    if saved:
        os.environ["BOT_ACCOUNTS_JSON"] = saved
    else:
        os.environ["BOT_ACCOUNTS_JSON"] = DEFAULT_ACCOUNTS_JSON
