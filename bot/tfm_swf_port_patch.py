"""
Patch tfm-proxy-loader ZWS SWF so the game connects to ``host:port`` instead of hardcoded ``localhost:11801``.

Upstream ignores ``loaderInfo.parameters``; the connection string lives in the LZMA-compressed body as
plain ASCII. We decompress, replace fixed-width strings, and recompress with the same LZMA settings.
"""

from __future__ import annotations

import logging
import struct
import lzma
from pathlib import Path

logger = logging.getLogger(__name__)

_ORIG_MAIN = b"localhost:11801"
_ORIG_POLICY = b"xmlsocket://localhost:10801"


def _lzma_filters_from_zws(p: bytes) -> tuple[list[dict], int]:
    """Parse LZMA sub-header starting at byte index 12 (after 8-byte SWF header + 4-byte prefix)."""
    props_byte = p[12]
    dict_size = struct.unpack_from("<I", p, 13)[0]
    lc = props_byte % 9
    rem = props_byte // 9
    pb = rem // 5
    lp = rem % 5
    filters: list[dict] = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}]
    return filters, 17  # compressed stream starts at offset 17


def decompress_zws_body(zws: bytes) -> bytes:
    filters, comp_off = _lzma_filters_from_zws(zws)
    return lzma.decompress(zws[comp_off:], format=lzma.FORMAT_RAW, filters=filters)


def recompress_zws_body(zws_original: bytes, new_body: bytes) -> bytes:
    filters, comp_off = _lzma_filters_from_zws(zws_original)
    new_comp = lzma.compress(new_body, format=lzma.FORMAT_RAW, filters=filters)
    head = zws_original[:comp_off]
    out = bytearray()
    out.extend(head)
    out.extend(new_comp)
    # ZWS/CWS: FileLength (bytes 4–8) is total UNCOMPRESSED size (8-byte header + decompressed
    # payload), not the on-disk compressed size. Wrong value breaks Flash parsing / runtime.
    struct.pack_into("<I", out, 4, 8 + len(new_body))
    return bytes(out)


def _main_connect_bytes(host: str, port: int) -> bytes:
    if port < 0 or port > 99999:
        raise ValueError(f"proxy_port out of range: {port}")
    h = host.strip()
    if len(h) != 9:
        raise ValueError(
            f"Patched host must be exactly 9 characters (like 127.0.0.1), got {len(h)!r}: {h!r}"
        )
    s = f"{h}:{port:>5}"
    if len(s) != 15:
        raise ValueError(f"internal: connect string length {len(s)} != 15: {s!r}")
    return s.encode("ascii")


def _policy_url_bytes(host: str) -> bytes:
    h = host.strip()
    if len(h) != 9:
        raise ValueError(f"policy host must be 9 chars: {h!r}")
    s = f"xmlsocket://{h}:10801"
    if len(s) != len(_ORIG_POLICY):
        raise ValueError(f"internal: policy string length mismatch: {s!r}")
    return s.encode("ascii")


def patch_loader_body(body: bytes, *, port: int, connect_host: str = "127.0.0.1") -> bytes:
    main = _main_connect_bytes(connect_host, port)
    pol = _policy_url_bytes(connect_host)
    if body.count(_ORIG_MAIN) != 1:
        raise ValueError(
            f"Expected exactly one {_ORIG_MAIN!r} in SWF body, found {body.count(_ORIG_MAIN)}"
        )
    if body.count(_ORIG_POLICY) != 1:
        raise ValueError(
            f"Expected exactly one {_ORIG_POLICY!r} in SWF body, found {body.count(_ORIG_POLICY)}"
        )
    body = body.replace(_ORIG_MAIN, main, 1)
    body = body.replace(_ORIG_POLICY, pol, 1)
    return body


def nine_char_connect_host(connect_host: str) -> str:
    """SWF string slot is 15 bytes ``host:ppppp`` with ``host`` exactly 9 ASCII chars."""
    h = connect_host.strip()
    if len(h) == 9 and h.isascii():
        return h
    return "127.0.0.1"


def build_patched_loader_swf(
    source_zws: Path,
    *,
    port: int,
    connect_host: str = "127.0.0.1",
    cache_dir: Path,
) -> Path:
    """
    Return path to a patched SWF (cached). ``connect_host`` must be 9 ASCII chars; longer IPs
    (e.g. ``10.47.103.2``) cannot fit and ``127.0.0.1`` is used instead.
    """
    h9 = nine_char_connect_host(connect_host)
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_host = h9.replace(":", "_").replace("/", "_")
    # Bump name so caches built with the old (wrong FileLength) patcher are ignored.
    out = cache_dir / f"TFMProxyLoader_patched_{safe_host}_{port}_zwsflen.swf"
    if out.is_file() and out.stat().st_size > 0:
        logger.info("Using cached patched loader port %s -> %s", port, out)
        return out

    raw = source_zws.read_bytes()
    if raw[:3] != b"ZWS":
        raise ValueError(f"Expected ZWS SWF, got signature {raw[:3]!r}")

    body = decompress_zws_body(raw)
    body = patch_loader_body(body, port=port, connect_host=h9)
    patched = recompress_zws_body(raw, body)

    # Round-trip check
    try:
        check = decompress_zws_body(patched)
        if _main_connect_bytes(h9, port) not in check:
            raise ValueError("round-trip verification failed (main string missing)")
    except Exception as e:
        raise ValueError(f"Patched SWF verification failed: {e}") from e

    out.write_bytes(patched)
    logger.info("Wrote patched loader for port %s host %s -> %s", port, h9, out)
    return out
