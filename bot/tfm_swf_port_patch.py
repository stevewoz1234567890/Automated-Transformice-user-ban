"""
Patch tfm-proxy-loader ZWS SWF so the game connects to ``host:port`` instead of hardcoded ``localhost:11801``.

Upstream ignores ``loaderInfo.parameters``; the connection string lives in the LZMA-compressed body as
plain ASCII. We decompress, replace fixed-width strings, and recompress with the same LZMA settings.
"""

from __future__ import annotations

import hashlib
import logging
import lzma
import os
import re
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# (resolved_path, mtime, size) -> sha256 hex digest prefix; avoids re-hashing the same file per slot.
_source_swf_digest_cache: dict[tuple[str, float, int], str] = {}

_ORIG_MAIN = b"localhost:11801"
_ORIG_POLICY = b"xmlsocket://localhost:10801"

# Patched-loader cache files now end with ``_<16 hex of source SWF>_zwsflen2.swf``.
_PATCH_CACHE_NEW_STYLE_SUFFIX = re.compile(r"_[0-9a-f]{16}_zwsflen2\.swf$", re.IGNORECASE)


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
    # ZWS-specific CompressedLength field at bytes 8–12 tells Flash how many bytes of LZMA data
    # follow the 5-byte LZMA-properties header. If we leave the original value in place and the
    # re-compressed stream is SHORTER, Flash reads past EOF and silently refuses to start the
    # SWF (blank window, no error). Even a few bytes short is fatal. Keep this in sync with the
    # actual compressed payload size.
    struct.pack_into("<I", out, 8, len(new_comp))
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


def _source_swf_cache_tag(source_zws: Path) -> str:
    """
    Short stable fingerprint of ``source_zws`` bytes for patch cache filenames.

    Previously the cache key was only ``host`` + ``port``, so replacing ``TFMProxyLoader.swf`` on
    disk (new game build / aligned loader) while keeping the same ports **reused an old patched
    file** — Flash then ran stale bytecode against current ``TFM_SECRETS_*`` / server behaviour,
    which surfaces as repeated ActionScript errors and PARTL ``clean-eof`` on MAIN.
    """
    rp = str(source_zws.resolve())
    try:
        st = source_zws.stat()
    except OSError:
        return "missing"
    key = (rp, float(st.st_mtime), int(st.st_size))
    hit = _source_swf_digest_cache.get(key)
    if hit is not None:
        return hit
    digest = hashlib.sha256()
    with source_zws.open("rb") as bf:
        for chunk in iter(lambda: bf.read(1024 * 1024), b""):
            digest.update(chunk)
    tag = digest.hexdigest()[:16]
    _source_swf_digest_cache[key] = tag
    return tag


def purge_legacy_loader_patch_cache(cache_dir: Path) -> int:
    """
    Remove patched-loader files from before source-hash cache names (``…_<port>_zwsflen2.swf``).

    Current entries include a 16-hex digest before ``_zwsflen2.swf``. Older builds keyed only by host
    + port could leave stale patched SWFs on disk across loader upgrades.
    """
    removed = 0
    if not cache_dir.is_dir():
        return 0
    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith("TFMProxyLoader_patched_"):
            continue
        if not name.endswith("_zwsflen2.swf"):
            continue
        if _PATCH_CACHE_NEW_STYLE_SUFFIX.search(name):
            continue
        try:
            entry.unlink()
            removed += 1
            logger.info(
                "Removed legacy patched-loader cache (no source-hash segment; superseded format): %s",
                name,
            )
        except OSError as e:
            logger.warning("Could not remove legacy loader cache %s (%s)", entry, e)
    if removed:
        logger.info(
            "Loader patch cache cleanup: removed %d legacy file(s) under %s — "
            "current caches include a 16-hex source SWF fingerprint in the filename.",
            removed,
            cache_dir,
        )
    return removed


def maybe_purge_legacy_loader_patch_cache(repo_root: Path) -> int:
    """If ``BOT_PURGE_LEGACY_LOADER_PATCH_CACHE`` is enabled (default), run :func:`purge_legacy_loader_patch_cache`."""
    raw = (os.environ.get("BOT_PURGE_LEGACY_LOADER_PATCH_CACHE") or "true").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return 0
    cache_dir = repo_root / "tmp" / "loader_patch"
    return purge_legacy_loader_patch_cache(cache_dir)


def invalidate_source_swf_digest_cache() -> None:
    """Clear memo used by :func:`_source_swf_cache_tag` (e.g. after replacing ``TFMProxyLoader.swf`` on disk)."""
    _source_swf_digest_cache.clear()


def purge_all_patched_loader_swfs(repo_root: Path) -> int:
    """Remove every ``*.swf`` under ``tmp/loader_patch`` so patched loaders are rebuilt from the current source."""
    cache_dir = repo_root / "tmp" / "loader_patch"
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".swf":
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError as e:
            logger.warning("Could not remove patched loader cache %s (%s)", entry, e)
    if removed:
        logger.info(
            "Loader patch cache: removed %d patched SWF(s) under %s (rebuilt on next Flash launch).",
            removed,
            cache_dir,
        )
        invalidate_source_swf_digest_cache()
    return removed


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
    src_tag = _source_swf_cache_tag(source_zws)
    # Bump suffix so caches built with older patchers are ignored. Previous suffix "_zwsflen"
    # left the ZWS CompressedLength field stale, which broke SWFs whose re-compressed body was
    # shorter than the original compressed stream (Flash would silently show a blank window).
    # ``src_tag`` ties the cache entry to the **current** loader bytes (see ``_source_swf_cache_tag``).
    out = cache_dir / f"TFMProxyLoader_patched_{safe_host}_{port}_{src_tag}_zwsflen2.swf"
    if out.is_file() and out.stat().st_size > 0:
        logger.debug("Using cached patched loader port %s -> %s", port, out)
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
