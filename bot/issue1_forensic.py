"""
Dense correlation logs for diagnosing client / secrets / loader drift ("issue 1").

Enable with::

    BOT_ISSUE1_FORENSIC=true

Grep defaults (always on unless disabled)::

    ISSUE1_SWEEP_BEGIN  ISSUE1_SWEEP_END
    issue1_near_as_dismiss=   dismiss_tail=
    ISSUE1_DISMISS_CUE=       ISSUE1_AS_DISMISS_ACTION
    ISSUE1_HANDSHAKE_GV_MISMATCH                   HandshakePacket.game_version ≠ TFM_SECRETS_GAME_VERSION
    ISSUE1_LOADER_PREFLIGHT                        weak SWF version markers at startup scan (CONTEXT on AS dupes too)

Set ``BOT_ISSUE1_SWEEP_BOUNDARY_LOG=0`` to hide sweep boundary markers.
Tune ``BOT_ISSUE1_AS_MAIN_CORR_WINDOW_SEC`` (seconds) for MAIN-vs-dismiss proximity on diagnostics.
Also see ``BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE`` for extra ROOT_CAUSE lines.
``BOT_ISSUE1_AS_DISMISS_LOG=true`` logs one WARNING per Flash dismiss (anchor ``ISSUE1_AS_DISMISS_ACTION``);
when ``BOT_ISSUE1_FORENSIC`` is enabled this defaults on unless you set the var to ``false`` explicitly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RE_ISSUE1_IV1 = re.compile(r"(?:^|\s)iv=1(?:\s|$)")


def active() -> bool:
    return (os.environ.get("BOT_ISSUE1_FORENSIC") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def apply_env_overrides() -> bool:
    """
    Aggressive diagnostics (overrides weaker env defaults for this process).

    Call from ``ban_cli.main`` after logging is configured so the banner appears.
    """
    if not active():
        return False
    os.environ["BOT_PROXY_ROOT_CAUSE_MAIN_CLOSE"] = "true"
    os.environ["BOT_PROXY_VERBOSE_LOGIN_FLOW"] = "true"

    try:
        cur_ring = int((os.environ.get("BOT_PROXY_MAIN_PACKET_RING") or "8").strip())
    except ValueError:
        cur_ring = 8
    target_ring = max(24, min(64, cur_ring))
    os.environ["BOT_PROXY_MAIN_PACKET_RING"] = str(target_ring)

    try:
        fp_prev = int((os.environ.get("FLASH_ERROR_FIRST_FP_PREVIEW_CHARS") or "1400").strip())
    except ValueError:
        fp_prev = 1400
    os.environ["FLASH_ERROR_FIRST_FP_PREVIEW_CHARS"] = str(max(4000, min(12000, fp_prev)))

    os.environ["FLASH_ERROR_LOG_FULL_BODY_FIRST_FP"] = "true"

    _dm = os.environ.get("BOT_ISSUE1_AS_DISMISS_LOG")
    if _dm is None or str(_dm).strip() == "":
        os.environ["BOT_ISSUE1_AS_DISMISS_LOG"] = "true"

    try:
        body_prev = int((os.environ.get("FLASH_ERROR_DISMISS_BODY_LOG_CHARS") or "720").strip())
    except ValueError:
        body_prev = 720
    os.environ["FLASH_ERROR_DISMISS_BODY_LOG_CHARS"] = str(max(2000, min(8000, body_prev)))

    logger.warning(
        "ISSUE1_FORENSIC enabled — tightened env: ROOT_CAUSE_MAIN_CLOSE VERBOSE_LOGIN "
        "MAIN_PACKET_RING=%s long AS previews; BOT_ISSUE1_AS_DISMISS_LOG defaulted on "
        "(ISSUE1_AS_DISMISS_ACTION per dismiss; set BOT_ISSUE1_AS_DISMISS_LOG=false to quiet). "
        "Optional BOT_PROXY_LOG_ALL_MAIN_PACKETS=true.",
        target_ring,
    )
    return True


def dismiss_tail_issue1_cues(detail: str) -> str | None:
    """
    Compact tokens for MAIN / ROOT_CAUSE lines (grep ``ISSUE1_DISMISS_CUE=``).

    Built from the same string stored as ``last_as_dismiss_detail_for_slot`` /
    ``issue1_near_as_dismiss=...|tail`` (see ``_dismiss_action_corr`` in flash_launch).
    """
    if not (detail or "").strip():
        return None
    tail = detail.strip()
    tok: list[str] = []
    if _RE_ISSUE1_IV1.search(tail):
        tok.append("WRONG_VERSION_HINT_IN_AS_BODY")
    if "ctx=login_phase_poll" in tail:
        tok.append("DISMISS_CTX_LOGIN_POLL")
    elif "ctx=post_login_sweep" in tail:
        tok.append("DISMISS_CTX_POST_LOGIN_SWEEP")
    elif "ctx=pre_ban_round" in tail:
        tok.append("DISMISS_CTX_PRE_BAN")
    if "meth=BM_CLICK" in tail:
        tok.append("AUTO_BM_CLICK")
        if "rank3" in tail or "rank4" in tail:
            tok.append("BM_CLICK_CONTINUE_RANK")
    elif "meth=Escape+WM_CLOSE_adobe_esc" in tail:
        tok.append("AUTO_ADOBE_ESC_WM_CLOSE")
    elif "meth=Escape+WM_CLOSE_nomatch_buttons" in tail:
        tok.append("AUTO_ESC_WM_CLOSE_NOMATCH_FALLBACK")
    if tok:
        return "ISSUE1_DISMISS_CUE=" + ",".join(tok)
    return None


def loader_source_fingerprint() -> str:
    """Stable line for correlating repo-root loader bytes with HANDSHAKE vs secrets."""
    try:
        from .flash_launch import resolve_flash_paths

        root = Path(__file__).resolve().parent.parent
        _flash_exe, swf = resolve_flash_paths(root)
        if not swf.is_file():
            return f"proxy_swf=missing:{swf}"
        raw = swf.read_bytes()
        h = hashlib.sha256(raw).hexdigest()[:16]
        return f"proxy_swf_path={swf.name} bytes={len(raw)} sha256[0:16]={h}"
    except OSError as e:
        return f"proxy_swf_read_err={e!r}"
    except Exception as e:
        return f"proxy_swf_digest_err={e!r}"


def log_as_error_slot_banner(slot_label: str, pid: int | None, fingerprint: str) -> None:
    if not active():
        return
    gv = (os.environ.get("TFM_SECRETS_GAME_VERSION") or "").strip()
    addr = (os.environ.get("TFM_SECRETS_SERVER_ADDRESS") or "").strip()
    lf = loader_source_fingerprint()
    logger.warning(
        "ISSUE1_AS_FIRST_FP slot=%s pid=%s fingerprint=%s env_TFM_SECRETS_GAME_VERSION=%r "
        "TFM_SECRETS_SERVER_ADDRESS=%r | %s",
        slot_label,
        pid,
        fingerprint,
        gv,
        addr,
        lf,
    )


def maybe_warn_handshake_mismatch(
    proxy: Any,
    *,
    flash_game_version: str | None,
    loader_stage_size: object | None,
) -> None:
    """Log once worth of HANDSHAKE tunnel facts while forensic is on."""
    if not active():
        return
    gv_env = (os.environ.get("TFM_SECRETS_GAME_VERSION") or "").strip()
    fgv = (flash_game_version or "").strip()
    if fgv:
        match = str(bool(gv_env) and fgv == gv_env)
    else:
        match = "n/a_missing_handshake_gv"
    logger.warning(
        "ISSUE1_HANDSHAKE slot=%s HandshakePacket.game_version=%r env_TFM_SECRETS_GAME_VERSION=%r "
        "match=%s loader_stage_size=%s account_bind_ip(ref)=%r",
        getattr(proxy, "slot_label", "?"),
        flash_game_version,
        gv_env,
        match,
        loader_stage_size,
        getattr(proxy, "_account_bind_ip", "") or "",
    )


def _upstream_transport_bits(proxy: Any) -> str:
    mcs = getattr(proxy, "main_clients", None) or []
    if not mcs:
        return "upstream_main_clients=0"
    dest = getattr(mcs[0], "destination", None)
    if dest is None:
        return "upstream_dest=None"
    for cand in ("_writer", "writer"):
        w = getattr(dest, cand, None)
        if w is None:
            continue
        tr = getattr(w, "transport", None)
        if tr is None:
            return f"upstream_no_transport({cand})"
        try:
            peer = tr.get_extra_info("peername")
            local = tr.get_extra_info("sockname")
            return (
                f"upstream_peer={peer!r} local_bind={local!r} upstream_sock_closing={tr.is_closing()}"
            )
        except OSError as e:
            return f"upstream_sockinfo_err={e!r}"
    return "upstream_writer_not_found"


def _trunc_plain(s: str, n: int) -> str:
    t = s.replace("\r", " ").replace("\n", " ")
    return t if len(t) <= n else t[: max(0, n - 3)] + "..."


def log_main_teardown_banner(
    proxy: Any,
    *,
    close_reason: str,
    alive_sec: float,
    since_login: float | None,
    close_exc: BaseException | None,
    diag: str,
    flash_tcp_snapshot: str,
    as_since_dismiss: str,
    packet_login_sent: bool,
) -> None:
    """One greedy WARNING line tying Flash TCP, handshake GV, upstream socket, loader file, PARTL diag."""
    if not active():
        return
    gv_env = (os.environ.get("TFM_SECRETS_GAME_VERSION") or "").strip()
    gv_handshake = getattr(proxy, "_issue1_last_handshake_gv", None) or ""
    gv_match = "n/a" if not gv_handshake else str(gv_env == gv_handshake)

    pkt_url = getattr(proxy, "_packet_login_loader_url", "") or ""
    url_note = _trunc_plain(f"loader_url[{len(pkt_url)}]={pkt_url}", 220)

    maddr = getattr(proxy, "main_server_address", None)
    upstream = _upstream_transport_bits(proxy)

    diag_short = _trunc_plain(diag, 520)

    exc_s = "(none)"
    if close_exc is not None:
        exc_s = f"{type(close_exc).__name__}:{close_exc}"
        win = getattr(close_exc, "winerror", None)
        if win is not None:
            exc_s += f" winerror={win}"

    logger.warning(
        "ISSUE1_MAIN_CLOSE slot=%s tcp_gen=%s reason=%s alive=%.3fs login_age=%s PACKET_LOGIN_SENT=%s | "
        "env_gv=%r handshake_gv=%r gv_match_handshake_vs_env=%s lss=%r bind_ip(ref)=%r main_server_address=%s | "
        "flash_tcp:%s | upstream:%s | as:%s | exc=%s | %s | loader:%s | diag:%s",
        getattr(proxy, "slot_label", "?"),
        getattr(proxy, "_main_tcp_generation", 0),
        close_reason,
        alive_sec,
        f"{since_login:.3f}s" if since_login is not None else "n/a",
        packet_login_sent,
        gv_env,
        gv_handshake,
        gv_match,
        getattr(proxy, "_issue1_last_handshake_lss", None),
        getattr(proxy, "_account_bind_ip", "") or "",
        maddr,
        flash_tcp_snapshot,
        upstream,
        as_since_dismiss,
        exc_s,
        url_note,
        loader_source_fingerprint(),
        diag_short,
    )
