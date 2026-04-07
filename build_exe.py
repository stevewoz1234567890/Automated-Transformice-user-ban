"""
Build ``ban_bot.exe`` in the repository root (PyInstaller one-file console app).

Uses ``tmp/`` for process TMP/TEMP during the build so stray ``*.tmp`` files stay out of the repo root.
The frozen exe extracts to ``tmp/`` at runtime (see ``runtime_tmpdir`` in ``ban_bot.spec``).

Requires: pip install -r requirements-build.txt
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    spec = root / "ban_bot.spec"
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
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
            "--distpath",
            str(root.resolve()),
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
    print(f"Built: {root / 'ban_bot.exe'}")


if __name__ == "__main__":
    main()
