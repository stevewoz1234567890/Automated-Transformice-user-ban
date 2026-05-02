"""
Auto-refresh TFM crypto + tfm-proxy-loader before the Flash UI ban session.

Runs once near ``ban_cli`` startup (before network preflight) so probes and proxies see current
secrets, and Flash loads a loader SWF that matches upstream tooling releases.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOADER_REPO = "friedkeenan/tfm-proxy-loader"
_GITHUB_RELEASE_API_TIMEOUT_SEC = 30.0


def _truthy(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _secrets_json_dict(sec: object) -> dict:
    from caseus import Secrets

    out: dict = {}
    for f in Secrets._FIELDS:
        v = getattr(sec, f, None)
        if v is None:
            continue
        if f == "client_verification_template" and isinstance(v, bytes):
            out[f] = v.hex()
        elif f == "server_ports":
            out[f] = list(v)
        elif f == "packet_key_sources":
            out[f] = list(v)
        else:
            out[f] = v
    return out


def persist_secrets_to_repo_json(sec: object, repo: Path) -> None:
    """Write repo-root ``tfm-secrets.json`` for merge-on-next-start and tooling parity."""
    if not _truthy("BOT_PERSIST_REFRESHED_SECRETS_JSON", True):
        return
    path = repo / "tfm-secrets.json"
    try:
        path.write_text(
            json.dumps(_secrets_json_dict(sec), indent=2),
            encoding="utf-8",
        )
        logger.info("[refresh] Wrote refreshed secrets to %s", path)
    except OSError as e:
        logger.warning("[refresh] Could not write %s (%s)", path, e)


def github_latest_asset_download_url(*, github_repo: str, asset_filename: str) -> str | None:
    owner, _, name = github_repo.partition("/")
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        return None
    api = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
    req = urllib.request.Request(
        api,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Automated-Transformice-user-ban/start",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_GITHUB_RELEASE_API_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as e:
        logger.warning("[refresh] GitHub release API failed for %s (%s)", github_repo, e)
        return None
    assets = data.get("assets") or []
    target = asset_filename.strip()
    for a in assets:
        if str(a.get("name") or "").strip() == target:
            u = str(a.get("browser_download_url") or "").strip()
            if u.startswith("http"):
                return u
    # Source-only release: fallback to predictable latest/download if present on next fetch
    fallback = (
        f"https://github.com/{owner}/{name}/releases/latest/download/"
        + urllib.parse.quote(target)
    )
    logger.debug("[refresh] No matching asset '%s'; will try fallback URL.", target)
    return fallback


def fetch_proxy_loader_swf(repo: Path) -> bool:
    """Download ``TFMProxyLoader.swf`` into repo root or ``TFM_PROXY_SWF``."""
    if not _truthy("BOT_FLASH_AUTO_FETCH_PROXY_LOADER", True):
        logger.info("[refresh] Loader download disabled (BOT_FLASH_AUTO_FETCH_PROXY_LOADER=false)")
        return False
    explicit = (os.environ.get("TFM_PROXY_SWF") or "").strip()
    dest = Path(explicit).expanduser().resolve() if explicit else (repo / "TFMProxyLoader.swf")
    refresh_each = _truthy("BOT_FLASH_REFRESH_PROXY_LOADER_EACH_RUN", False)
    fetch_if_missing = _truthy("BOT_FLASH_FETCH_PROXY_LOADER_IF_MISSING", True)
    need_download = (not dest.is_file() and fetch_if_missing) or (dest.is_file() and refresh_each)
    if not need_download:
        if dest.is_file():
            logger.info(
                "[refresh] Loader already present (%s); set BOT_FLASH_REFRESH_PROXY_LOADER_EACH_RUN=true "
                "to replace with latest upstream release.",
                dest,
            )
        else:
            logger.info(
                "[refresh] Loader missing but BOT_FLASH_FETCH_PROXY_LOADER_IF_MISSING=false — not downloading.",
            )
        return bool(dest.is_file())

    fixed_url = (os.environ.get("BOT_TFM_PROXY_LOADER_DOWNLOAD_URL") or "").strip()
    url = fixed_url if fixed_url else None
    if not url:
        repo_spec = (
            os.environ.get("BOT_TFM_PROXY_LOADER_GITHUB_REPO") or _DEFAULT_LOADER_REPO
        ).strip()
        fname = (
            os.environ.get("BOT_TFM_PROXY_LOADER_ASSET_NAME") or "TFMProxyLoader.swf"
        ).strip()
        url = github_latest_asset_download_url(github_repo=repo_spec, asset_filename=fname)
    if not url:
        logger.error(
            "[refresh] Cannot resolve loader download URL "
            "(set BOT_TFM_PROXY_LOADER_DOWNLOAD_URL to a full https URL)",
        )
        return False

    tmp = dest.with_suffix(dest.suffix + ".download")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("[refresh] Loader temp dir %s (%s)", tmp.parent, e)
        return False
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Automated-Transformice-user-ban/fetch-proxy-loader",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=90.0) as resp:
            body = resp.read()
    except OSError as e:
        logger.error("[refresh] Loader download failed from %s (%s)", url, e)
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        return False
    if len(body) < 1000 or body[:3] not in (b"FWS", b"CWS", b"ZWS"):
        preview = body[:80]
        logger.error(
            "[refresh] Response from %s is not SWF-shaped (starts %r); refusing to overwrite %s",
            url,
            preview,
            dest,
        )
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    try:
        tmp.write_bytes(body)
        shutil.move(str(tmp), dest)
        logger.info("[refresh] Installed loader SWF (%s bytes) → %s", len(body), dest)
        try:
            if getattr(sys, "frozen", False):
                os.environ.pop("TFM_PURGE_LOADER_PATCH_CACHE_THIS_RUN", None)
        except Exception:
            pass
    except OSError as e:
        logger.error("[refresh] Could not write loader to %s (%s)", dest, e)
        return False
    return True


def refresh_tfm_secrets_via_dumpers(repo: Path) -> bool:
    """Run tfm-secrets / Flash leaker like headless; update ``os.environ`` and ``.env``."""
    from .env_setup import load_bot_config
    from .headless_client import (
        _resolved_dotenv_path,
        _try_acquire_secrets_from_dumpers,
        sync_upstream_cfg_from_secrets,
    )

    cfg = load_bot_config()
    if not getattr(cfg, "HEADLESS_SECRETS_ALWAYS_REFRESH", False):
        logger.info("[refresh] Skipping live secrets dumper (BOT_HEADLESS_SECRETS_ALWAYS_REFRESH=false)")
        return False
    if getattr(sys, "frozen", False) and (
        os.environ.get("BOT_REFRESH_SECRETS_IN_FROZEN_BUILD") or ""
    ).strip().lower() not in ("1", "true", "yes", "on"):
        logger.info("[refresh] Frozen build: skipping dumper/leaker unless BOT_REFRESH_SECRETS_IN_FROZEN_BUILD=true")
        return False

    dot = _resolved_dotenv_path(cfg)
    sec = _try_acquire_secrets_from_dumpers(cfg, dot, reason="always_refresh")
    if sec is None:
        logger.warning("[refresh] Live secrets refresh failed — using existing TFM_SECRETS_* / tfm-secrets.json")
        return False

    persist_secrets_to_repo_json(sec, repo)
    try:
        sync_upstream_cfg_from_secrets(cfg, sec)
        for attr, val in (
            ("UPSTREAM_SERVER_ADDRESS", str(sec.server_address).strip()),
            (
                "UPSTREAM_SERVER_PORTS",
                tuple(int(x) for x in sec.server_ports),
            ),
            ("UPSTREAM_FROM_SECRETS_DUMP_ONLY", True),
            ("UPSTREAM_ALLOW_ADDRESS_MISMATCH", False),
        ):
            setattr(cfg, attr, val)
        return True
    except Exception:
        logger.exception("[refresh] After dump, sync_upstream_cfg_from_secrets failed")
        return True


def run_flash_startup_refresh(repo: Path) -> None:
    """Entry: secrets first (defines game host), then optional loader SWF fetch."""
    if not _truthy("BOT_FLASH_STARTUP_REFRESH_TFM_ASSETS", True):
        logger.info("[refresh] Disabled via BOT_FLASH_STARTUP_REFRESH_TFM_ASSETS=false")
        return

    refreshed = refresh_tfm_secrets_via_dumpers(repo)
    if refreshed:
        from .env_setup import sync_upstream_env_with_tfm_secrets

        sync_upstream_env_with_tfm_secrets()

    fetch_proxy_loader_swf(repo)
