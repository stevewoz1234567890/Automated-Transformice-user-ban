"""
Build ``ban_bot.exe`` in the repository root (PyInstaller one-file console app).

Writes the exe to a fresh ``dist/_staging_<uuid>/`` folder, then copies to ``ban_bot.exe`` in the repo
root and to ``dist/ban_bot.exe`` when possible (avoids WinError 32 when ``dist/ban_bot.exe`` is locked).

Uses ``tmp/`` for process TMP/TEMP during the build so stray ``*.tmp`` files stay out of the repo root.
The frozen exe extracts to ``tmp/`` at runtime (see ``runtime_tmpdir`` in ``ban_bot.spec``).

Requires: pip install -r requirements.txt (and install caseus the way you use at runtime).
The PyInstaller build must run with the same interpreter where ``import pak`` and ``import caseus`` succeed,
or modules will be missing inside ban_bot.exe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    spec = root / "ban_bot.spec"
    tmp_dir = root / "tmp"
    dist_dir = root / "dist"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    # PyInstaller updates ban_bot.exe in-place; another process (AV, IDE, old run) may lock
    # dist\\ban_bot.exe. Build to a fresh staging folder, then copy.
    staging = dist_dir / f"_staging_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    tmp_abs = str(tmp_dir.resolve())
    env["TMP"] = tmp_abs
    env["TEMP"] = tmp_abs
    env["TMPDIR"] = tmp_abs
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(staging.resolve()),
            "--workpath",
            str((root / "build" / "pyinstaller").resolve()),
            str(spec.resolve()),
        ],
        check=True,
        cwd=tmp_abs,
        env=env,
    )
    for p in root.glob("*.tmp"):
        try:
            p.unlink()
        except OSError:
            pass
    built = staging / "ban_bot.exe"
    out = root / "ban_bot.exe"
    out_dist = dist_dir / "ban_bot.exe"
    try:
        shutil.copy2(built, out)
        print(f"Built: {out}")
    except OSError as e:
        print(f"Built: {built} (could not copy to {out}: {e})")
    try:
        shutil.copy2(built, out_dist)
        if out.exists():
            print(f"Also: {out_dist}")
    except OSError as e:
        print(f"Note: could not copy to {out_dist}: {e}")
    try:
        shutil.rmtree(staging)
    except OSError:
        pass


if __name__ == "__main__":
    main()
