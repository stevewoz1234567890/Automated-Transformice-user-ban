#!/usr/bin/env python3
r"""
Write repo-root ``tfm-secrets.json`` (optional). For headless mode, copy values into repo-root ``.env`` as ``TFM_SECRETS_*`` or use ``HEADLESS_SECRETS_DUMPER``.

1. If ``tfm-secrets`` is on PATH, its stdout is written (must be JSON).
2. Otherwise uses ``caseus.Secrets.load_from_leaker_swf`` with ``TFMSecretsLeaker.swf``
   (downloaded to ``tmp/``) and the Flash debug projector:

   - ``flashplayer_32_sa_debug.exe`` in the repository root, or
   - ``FLASHPLAYER_DEBUG`` / ``FLASH_DEBUG_STANDALONE`` pointing at the projector exe.

Run from repo root: venv python scripts/export_tfm_secrets_json.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = OUT_DEFAULT = ROOT / "tfm-secrets.json"
LEAKER_URL = (
    "https://github.com/friedkeenan/tfm-secrets-leaker/releases/download/v1.5.32/"
    "TFMSecretsLeaker.swf"
)
LEAKER_CACHED = ROOT / "tmp" / "TFMSecretsLeaker.swf"


def _secrets_to_dict(s) -> dict:
    from caseus import Secrets

    d: dict = {}
    for f in Secrets._FIELDS:
        v = getattr(s, f)
        if f == "client_verification_template" and isinstance(v, bytes):
            d[f] = v.hex()
        elif f == "server_ports" and v is not None:
            d[f] = list(v)
        elif f == "packet_key_sources" and v is not None:
            d[f] = list(v)
        else:
            d[f] = v
    return d


def _try_tfm_secrets_cmd() -> str | None:
    try:
        p = subprocess.run(
            ["tfm-secrets"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except FileNotFoundError:
        return None
    if p.returncode != 0:
        sys.stderr.write(
            f"tfm-secrets failed (exit {p.returncode}): {p.stderr.decode('utf-8', errors='replace')!r}\n"
        )
        return None
    return p.stdout.decode("utf-8", errors="replace")


def _resolve_flash_debugger() -> Path | None:
    env = (os.environ.get("FLASHPLAYER_DEBUG") or os.environ.get("FLASH_DEBUG_STANDALONE") or "").strip()
    if env:
        pe = Path(env).expanduser()
        if pe.is_file():
            return pe
    cand = ROOT / "flashplayer_32_sa_debug.exe"
    if cand.is_file():
        return cand
    return None


def _ensure_leaker_swf() -> Path:
    LEAKER_CACHED.parent.mkdir(parents=True, exist_ok=True)
    if not LEAKER_CACHED.is_file():
        print(f"Downloading TFMSecretsLeaker.swf to {LEAKER_CACHED}")
        urllib.request.urlretrieve(LEAKER_URL, LEAKER_CACHED)
    return LEAKER_CACHED


def main(argv: list[str]) -> int:
    global OUT
    if len(argv) > 1:
        OUT = Path(argv[1]).expanduser().resolve()
    else:
        OUT = OUT_DEFAULT

    text = _try_tfm_secrets_cmd()
    if text is not None:
        json.loads(text)  # validate
        OUT.write_text(text, encoding="utf-8")
        print(f"Wrote {OUT} (tfm-secrets on PATH)")
        return 0

    flash = _resolve_flash_debugger()
    if flash is None:
        print(
            "Neither `tfm-secrets` on PATH nor flashplayer_32_sa_debug.exe in repo root "
            "(set FLASHPLAYER_DEBUG to the projector path).",
            file=sys.stderr,
        )
        return 1

    leaker = _ensure_leaker_swf()
    from caseus import Secrets

    print(f"Running leaker SWF via {flash} …")
    secrets = Secrets.load_from_leaker_swf(leaker, debug_standalone=str(flash))
    OUT.write_text(json.dumps(_secrets_to_dict(secrets), indent=2), encoding="utf-8")
    print(f"Wrote {OUT} (caseus.load_from_leaker_swf)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
