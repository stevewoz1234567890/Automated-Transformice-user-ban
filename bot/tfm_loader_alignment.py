"""
Client / asset alignment checks for TFMProxyLoader vs ``TFM_SECRETS_GAME_VERSION``.

The proxy and packet-login path use secrets from ``.env``; Flash loads the bundled loader SWF,
which may embed URLs or literals tied to an older game build. Mismatches surface as ActionScript
errors (not a single Python line in the proxy). This module logs a structured preflight block and
optional strict failure when embedded hints contradict the configured version.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import zlib
from pathlib import Path

from .flash_launch import resolve_flash_paths
from .tfm_swf_port_patch import decompress_zws_body

logger = logging.getLogger(__name__)

# Filled by :func:`log_client_asset_alignment` for session reports.
_last_alignment_md_lines: list[str] = []


def get_last_alignment_report_md() -> list[str]:
    return list(_last_alignment_md_lines)


def _bool_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _swf_scan_payload(raw: bytes) -> tuple[str, bytes] | None:
    if len(raw) < 8:
        return None
    sig = raw[:3]
    if sig == b"ZWS":
        try:
            return "ZWS", decompress_zws_body(raw)
        except Exception as e:
            logger.warning("Loader SWF: ZWS decompress failed (%s); scanning raw file for ASCII hints.", e)
            return "ZWS(decompress-failed)", raw
    if sig == b"FWS":
        return "FWS", raw[8:]
    if sig == b"CWS":
        try:
            return "CWS", zlib.decompress(raw[8:])
        except Exception as e:
            logger.warning("Loader SWF: CWS zlib decompress failed (%s); scanning raw bytes.", e)
            return "CWS(decompress-failed)", raw
    return "UNKNOWN", raw


def _extract_urlish_version_hints(body: bytes) -> list[int]:
    patterns = (
        rb"swf=r(\d{2,6})\b",
        rb"swf%3dr(\d{2,6})\b",
        rb"gameversion[=:](\d{2,6})\b",
        rb"game_version[=:](\d{2,6})\b",
    )
    found: set[int] = set()
    for pat in patterns:
        for m in re.finditer(pat, body, re.IGNORECASE):
            try:
                found.add(int(m.group(1)))
            except (ValueError, IndexError):
                pass
    return sorted(found)


def _config_game_version_int(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s, 0)
    except ValueError:
        return None


def _strict_mismatch(
    *,
    cfg_gv: int,
    urlish: list[int],
) -> bool:
    if not urlish:
        return False
    return cfg_gv not in urlish


def log_client_asset_alignment(repo_root: Path) -> None:
    """
    Log Flash exe + loader SWF paths, SWF format, and whether ``TFM_SECRETS_GAME_VERSION`` matches
    embedded loader hints. Populates :data:`_last_alignment_md_lines` for session reports.
    """
    global _last_alignment_md_lines
    flash, swf = resolve_flash_paths(repo_root)
    gv_raw = (os.environ.get("TFM_SECRETS_GAME_VERSION") or "").strip()
    cfg_gvi = _config_game_version_int(gv_raw)
    no_patch = _bool_env("TFM_NO_SWF_PATCH")
    strict = _bool_env("BOT_STRICT_LOADER_VERSION_CHECK")

    lines_md: list[str] = [
        "## Client / loader alignment (startup)\n\n",
        f"- **TFM_SECRETS_GAME_VERSION**: `{gv_raw or '—'}`\n",
        f"- **Flash projector**: `{'yes' if flash.is_file() else 'missing'}` `{flash}`\n",
        f"- **TFM_PROXY_SWF / loader**: `{'yes' if swf.is_file() else 'missing'}` `{swf}`\n",
        f"- **TFM_NO_SWF_PATCH**: `{no_patch}`\n",
        f"- **BOT_STRICT_LOADER_VERSION_CHECK**: `{strict}`\n",
    ]

    if not swf.is_file():
        logger.warning(
            "Client/asset alignment: loader SWF missing at %s — Flash cannot start; "
            "set TFM_PROXY_SWF or place TFMProxyLoader.swf in the repo root.",
            swf,
        )
        lines_md.append("- **scan**: loader file missing\n\n")
        _last_alignment_md_lines = lines_md
        return

    try:
        raw = swf.read_bytes()
    except OSError as e:
        logger.error("Client/asset alignment: cannot read %s (%s)", swf, e)
        lines_md.append(f"- **scan**: read error `{e}`\n\n")
        _last_alignment_md_lines = lines_md
        return

    scanned = _swf_scan_payload(raw)
    if scanned is None:
        body = raw
        tag = "?"
    else:
        tag, body = scanned

    gv_ascii = gv_raw.encode("ascii", errors="ignore") if gv_raw else b""
    literal_substrings = False
    if gv_ascii:
        literal_substrings = gv_ascii in body or (b"v" + gv_ascii) in body or (b"=" + gv_ascii) in body

    urlish = _extract_urlish_version_hints(body)
    lines_md.append(f"- **SWF on-disk signature**: `{raw[:3]!r}` **scan_tag**: `{tag}` **payload_bytes**: {len(body)}\n")
    lines_md.append(
        f"- **Literal CONFIG version bytes in payload**: "
        f"`{'yes' if literal_substrings else 'no'}` "
        f"(inconclusive if no; loader may take version only from server)\n"
    )
    if urlish:
        lines_md.append(f"- **URL-style embedded version hints** (swf=r… / gameversion…): `{urlish}`\n")
    else:
        lines_md.append("- **URL-style embedded version hints**: *(none found)*\n")

    logger.info(
        "Client/asset alignment: game_version=%r swf=%s format=%s payload=%s bytes literal_match=%s "
        "url_hints=%s flash_exe=%s",
        gv_raw or None,
        swf.name,
        tag,
        len(body),
        literal_substrings,
        urlish or None,
        flash.name if flash.is_file() else str(flash),
    )

    if cfg_gvi is not None and urlish and _strict_mismatch(cfg_gv=cfg_gvi, urlish=urlish):
        msg = (
            f"Client/asset alignment MISMATCH: TFM_SECRETS_GAME_VERSION={cfg_gvi} but loader embeds "
            f"{urlish} (swf=r / gameversion-style strings). Update the loader SWF and/or re-dump secrets "
            f"so Flash, LoginPacket, and MAIN agree — otherwise expect ActionScript / incorrect-version dialogs."
        )
        logger.error(msg)
        lines_md.append(f"- **MISMATCH**: configured `{cfg_gvi}` vs embedded `{urlish}`\n\n")
        _last_alignment_md_lines = lines_md
        if strict:
            logger.error("BOT_STRICT_LOADER_VERSION_CHECK=1 — exiting after loader/version mismatch.")
            sys.exit(1)
        return

    if cfg_gvi is not None and urlish and cfg_gvi in urlish:
        logger.info(
            "Client/asset alignment: loader embeds URL hints including configured game_version=%s — good signal.",
            cfg_gvi,
        )

    if cfg_gvi is not None and urlish and cfg_gvi in urlish and not literal_substrings:
        logger.debug(
            "Client/asset alignment: URL hints match config but literal version string not in payload (normal for some builds)."
        )

    lines_md.append("\n")
    _last_alignment_md_lines = lines_md
