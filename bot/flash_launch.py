"""
Build ``file:///…TFMProxyLoader.swf?…`` URLs for ``LoginPacket.loader_url`` (packet login).

The bot does not start Flash Player or the loader SWF; an external client must open the MAIN TCP
connection to each proxy port. Port numbers here match what a patched loader would use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import tfm_swf_port_patch


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_loader_swf(root: Path | None = None) -> Path:
    root = root or _repo_root()
    env_swf = (os.environ.get("TFM_PROXY_SWF") or "").strip()
    return Path(env_swf) if env_swf else root / "TFMProxyLoader.swf"


def _loader_document_url(
    swf: Path,
    *,
    main_port: int,
    satellite_port: int,
    policy_port: int | None,
    connect_host: str,
) -> str:
    """Query string becomes ``loaderInfo.parameters`` (forks can read host / port / satellite / policy)."""
    uri = swf.resolve().as_uri()
    h = connect_host.strip() or "127.0.0.1"
    q = [
        f"host={h}",
        f"proxyHost={h}",
        f"port={int(main_port)}",
        f"satellite={int(satellite_port)}",
    ]
    if policy_port is not None:
        pp = int(policy_port)
        q.append(f"policy_port={pp}")
        q.append(f"policyPort={pp}")
    return uri + "?" + "&".join(q)


AccountRow = dict[str, object]


def loader_document_url_for_row(row: AccountRow, root: Path | None = None) -> str | None:
    """
    ``file:///...swf?host=...`` document URL for ``LoginPacket.loader_url``.
    Returns ``None`` only if the SWF path is missing.
    """
    root = root or _repo_root()
    swf = _resolve_loader_swf(root)
    if not swf.is_file():
        return None

    port = int(row["proxy_port"])
    sat_raw = row.get("_flash_satellite_port")
    satellite = int(sat_raw) if sat_raw is not None else port + 10_000
    pol_raw = row.get("_flash_policy_port")
    policy = int(pol_raw) if pol_raw is not None else None
    ch_raw = row.get("_flash_connect_host")
    connect_host = str(ch_raw).strip() if ch_raw is not None else "127.0.0.1"
    patch_host = tfm_swf_port_patch.nine_char_connect_host(connect_host)

    swf_arg = swf
    no_patch = (os.environ.get("TFM_NO_SWF_PATCH") or "").strip().lower() in ("1", "true", "yes")
    if not no_patch:
        try:
            head = swf.read_bytes()[:3]
            if head == b"ZWS":
                cache_dir = root / "tmp" / "loader_patch"
                swf_arg = tfm_swf_port_patch.build_patched_loader_swf(
                    swf,
                    port=port,
                    connect_host=connect_host,
                    cache_dir=cache_dir,
                )
        except Exception:
            swf_arg = swf

    return _loader_document_url(
        swf_arg,
        main_port=port,
        satellite_port=satellite,
        policy_port=policy,
        connect_host=patch_host,
    )
