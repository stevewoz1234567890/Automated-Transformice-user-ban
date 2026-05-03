"""
Patch tfm-proxy-loader ZWS SWF so the game connects to ``host:port`` instead of hardcoded ``localhost:11801``.

Upstream ignores ``loaderInfo.parameters``; the connection string lives in the LZMA-compressed body as
plain ASCII. We decompress, replace fixed-width strings, and recompress with the same LZMA settings.

Some loader builds also embed ``TFM_SECRETS_SERVER_ADDRESS`` (plain IPv4) elsewhere in the bytecode.
When Flash runs the patched SWF from ``file://``, those literals can trigger **Error #2048** (security
sandbox: local SWF cannot ``load`` / open socket to bare game IP). Replacing those host bytes with a
**same-length** ``127.0.0.1`` + ASCII space padding keeps structs stable while forcing traffic through
the local proxy ports we already patch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import lzma
import os
import re
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

# (resolved_path, mtime, size) -> sha256 hex digest prefix; avoids re-hashing the same file per slot.
_source_swf_digest_cache: dict[tuple[str, float, int], str] = {}

# Purge legacy patch caches at most once per resolved cache_dir per process (14 slots → 1 scan).
_legacy_loader_patch_purged: set[str] = set()

_ORIG_MAIN = b"localhost:11801"
_ORIG_POLICY = b"xmlsocket://localhost:10801"

# Patched-loader cache basename shape (see :func:`build_patched_loader_swf`):
# ``TFMProxyLoader_patched_<host>_<port>_<8 hex up_slug>_<16 hex source>_zwsflen2.swf``.
# Older intermediate names used ``…_<port>_<16hex>_zwsflen2.swf`` only; purge drops those so Flash cannot
# keep a patched loader missing upstream literal neutralization (Flash #2048).
_PATCH_CACHE_UPSTREAM_AWARE_SUFFIX = re.compile(
    r"_\d+_[0-9a-f]{8}_[0-9a-f]{16}_zwsflen2\.swf$",
    re.IGNORECASE,
)


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


def _server_address_from_tfm_secrets_json(repo_root: Path) -> str:
    """Read ``server_address`` from repo-root ``tfm-secrets.json`` (same shape as leaker/export)."""
    path = repo_root / "tfm-secrets.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    sv = data.get("server_address")
    if not isinstance(sv, str):
        return ""
    h = sv.strip()
    if h and h.lower() not in ("127.0.0.1", "localhost"):
        return h
    return ""


def resolve_upstream_ipv4_literal_for_patch(*, repo_root_hint: Path | None = None) -> str:
    """Host from secrets/upstream sync (plaintext IPv4 literals only — used for loader neutralization)."""
    for key in ("TFM_SECRETS_SERVER_ADDRESS", "BOT_UPSTREAM_SERVER_ADDRESS"):
        raw = (os.environ.get(key) or "").strip()
        if raw and raw.lower() not in ("127.0.0.1", "localhost"):
            return raw
    # Some entrypoints merge ``tfm-secrets.json`` after first env read; still neutralize from JSON on disk.
    if repo_root_hint is not None:
        from_json = _server_address_from_tfm_secrets_json(repo_root_hint)
        if from_json:
            logger.debug(
                "Loader patch: using upstream IPv4 %r from tfm-secrets.json (env had no TFM/BOT address yet)",
                from_json,
            )
            return from_json
    return ""


def _upstream_neutralization_enabled() -> bool:
    v = (os.environ.get("BOT_LOADER_NEUTRALIZE_UPSTREAM_IP_LITERALS") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _is_plausible_ascii_ipv4_dotted_quad(s: str) -> bool:
    if not s or len(s) < 7 or len(s) > 15:
        return False
    if any(ord(ch) > 127 for ch in s):
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or len(p) > 3:
            return False
        try:
            n = int(p, 10)
        except ValueError:
            return False
        if n > 255:
            return False
    return True


def nine_char_connect_host(connect_host: str) -> str:
    """SWF string slot is 15 bytes ``host:ppppp`` with ``host`` exactly 9 ASCII chars."""
    h = connect_host.strip()
    if len(h) == 9 and h.isascii():
        return h
    return "127.0.0.1"


def _padded_ipv4_placeholder(target_len: int) -> bytes:
    """Replace longer upstream IPv4 literals with ``127.0.0.1`` + trailing ASCII spaces."""
    logical = nine_char_connect_host("127.0.0.1").encode("ascii")
    if target_len < len(logical):
        raise ValueError(f"padded_ipv4_placeholder: slot {target_len} < {len(logical)}")
    return logical + b" " * (target_len - len(logical))


def neutralize_upstream_ip_literals(body: bytes, *, upstream_host: str) -> tuple[bytes, int]:
    """
    Replace plaintext ``upstream_host`` (IPv4 quad) wherever it sits before ``:PORT`` / NUL-delimited
    string so Flash stops opening ``file:// → bare game IP`` paths that violate the local sandbox (#2048).
    """
    h = upstream_host.strip()
    if not h or h in ("127.0.0.1", "::1"):
        return body, 0
    if not _is_plausible_ascii_ipv4_dotted_quad(h):
        return body, 0
    needle = h.encode("ascii")
    rp = _padded_ipv4_placeholder(len(needle))
    mv = bytearray(body)
    n_repl = 0
    i = 0
    digits = frozenset(ord(x) for x in "0123456789")

    while True:
        idx = mv.find(needle, i)
        if idx < 0:
            break
        tail = idx + len(needle)
        if idx > 0 and mv[idx - 1] in digits:
            i = idx + 1
            continue
        if tail >= len(mv):
            i = idx + 1
            continue
        nxt = mv[tail]
        ok = False
        if nxt == ord(":"):
            pi = tail + 1
            port_digits = 0
            while pi < len(mv) and 48 <= mv[pi] <= 57:
                port_digits += 1
                pi += 1
            if 1 <= port_digits <= 5 and tail + 1 + port_digits <= len(mv):
                try:
                    pval = int(mv[tail + 1 : tail + 1 + port_digits].decode("ascii"))
                except ValueError:
                    pval = -1
                if 1 <= pval <= 65535:
                    ok = True
        elif nxt == 0:
            ok = True
        elif nxt in (ord("/"), ord("?"), ord("#"), ord("\\")):
            ok = True
        if ok:
            mv[idx:tail] = rp
            n_repl += 1
            i = tail + 1
        else:
            i = idx + 1
    return bytes(mv), n_repl


def patch_loader_body(
    body: bytes,
    *,
    port: int,
    connect_host: str = "127.0.0.1",
    upstream_server_address: str | None = None,
) -> bytes:
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
    ua = (upstream_server_address or "").strip()
    if ua and _upstream_neutralization_enabled():
        body, n_lit = neutralize_upstream_ip_literals(body, upstream_host=ua)
        if n_lit > 0:
            logger.info(
                "SWF bytecode: neutralized %d plaintext %r literal(s) → padded 127.0.0.1 "
                "(Fixes Flash #2048 sandbox: file:// patched loader touching bare upstream IP)",
                n_lit,
                ua,
            )
    return body


def _upstream_cache_slug(*, upstream_for_neutralization: str) -> str:
    """Stable short token so patched loaders rebuild when upstream IP/neutralization changes."""
    stem = upstream_for_neutralization.strip().lower()
    suffix = "|1" if _upstream_neutralization_enabled() else "|0"
    if not stem or stem == "127.0.0.1":
        return hashlib.sha256(("(none)|" + suffix).encode()).hexdigest()[:8]
    return hashlib.sha256((stem + suffix).encode()).hexdigest()[:8]


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
    Remove patched-loader cache files that are not the **current** filename shape.

    Keeps only ``…_<port>_<8hex upslug>_<16hex src>_zwsflen2.swf`` (upstream-neutralization-aware).
    Drops: host+port-only names, zlib-era ``_zwsflen`` artifacts, and the intermediate
    ``…_<port>_<16hex src>_zwsflen2.swf`` shape (same source digest suffix as today but missing
    ``up_slug`` — Flash would reuse a loader built without plaintext-IP stripping).
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
        if _PATCH_CACHE_UPSTREAM_AWARE_SUFFIX.search(name):
            continue
        try:
            entry.unlink()
            removed += 1
            logger.info(
                "Removed legacy patched-loader cache (superseded filename; rebuilt with current patcher): %s",
                name,
            )
        except OSError as e:
            logger.warning("Could not remove legacy loader cache %s (%s)", entry, e)
    if removed:
        logger.info(
            "Loader patch cache cleanup: removed %d legacy file(s) under %s — "
            "current caches use ``_<port>_<8hex upstream slug>_<16hex source>_zwsflen2.swf``.",
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
    try:
        _legacy_loader_patch_purged.discard(str(cache_dir.resolve()))
    except OSError:
        _legacy_loader_patch_purged.discard(str(cache_dir))
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
    # Drop legacy ``…_<port>_<16hex>_zwsflen2.swf`` names before cache lookup so mixed upgraded trees
    # never reuse loaders built without upstream literal neutralization (Flash #2048 / PARTL).
    raw_purge = (os.environ.get("BOT_PURGE_LEGACY_LOADER_PATCH_CACHE") or "true").strip().lower()
    if raw_purge not in ("0", "false", "no", "off"):
        try:
            key = str(cache_dir.resolve())
        except OSError:
            key = str(cache_dir)
        if key not in _legacy_loader_patch_purged:
            purge_legacy_loader_patch_cache(cache_dir)
            _legacy_loader_patch_purged.add(key)

    safe_host = h9.replace(":", "_").replace("/", "_")
    src_tag = _source_swf_cache_tag(source_zws)
    repo_root_hint: Path | None = None
    if cache_dir.name == "loader_patch" and cache_dir.parent.name == "tmp":
        repo_root_hint = cache_dir.parent.parent.resolve()
    up_lit = resolve_upstream_ipv4_literal_for_patch(repo_root_hint=repo_root_hint)
    up_slug = _upstream_cache_slug(upstream_for_neutralization=up_lit if up_lit else "127.0.0.1")
    # Bump suffix so caches built with older patchers are ignored. Previous suffix "_zwsflen"
    # left the ZWS CompressedLength field stale, which broke SWFs whose re-compressed body was
    # shorter than the original compressed stream (Flash would silently show a blank window).
    # ``src_tag`` ties the cache entry to the **current** loader bytes (see ``_source_swf_cache_tag``).
    # ``up_slug`` forces rebuild when TFM upstream IP literals / neutralization flag change.
    out = cache_dir / f"TFMProxyLoader_patched_{safe_host}_{port}_{up_slug}_{src_tag}_zwsflen2.swf"
    if out.is_file() and out.stat().st_size > 0:
        logger.debug("Using cached patched loader port %s -> %s", port, out)
        return out

    raw = source_zws.read_bytes()
    if raw[:3] != b"ZWS":
        raise ValueError(f"Expected ZWS SWF, got signature {raw[:3]!r}")

    body = decompress_zws_body(raw)
    body = patch_loader_body(
        body,
        port=port,
        connect_host=h9,
        upstream_server_address=up_lit,
    )
    patched = recompress_zws_body(raw, body)

    # Round-trip check
    try:
        check = decompress_zws_body(patched)
        if _main_connect_bytes(h9, port) not in check:
            raise ValueError("round-trip verification failed (main string missing)")
        if (
            up_lit
            and _upstream_neutralization_enabled()
            and _is_plausible_ascii_ipv4_dotted_quad(up_lit)
            and up_lit.encode("ascii") in check
        ):
            logger.warning(
                "Patched loader LZMA body still contains plaintext upstream %r — "
                "neutralize may have missed fragmented literals; Flash #2048 risk if file:// hits bare IP.",
                up_lit,
            )
    except Exception as e:
        raise ValueError(f"Patched SWF verification failed: {e}") from e

    out.write_bytes(patched)
    logger.info("Wrote patched loader for port %s host %s -> %s", port, h9, out)
    return out
