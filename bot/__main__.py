"""Entry: optionally upgrade caseus from git before importing the rest of the package (see config)."""

from __future__ import annotations

import subprocess
import sys


def _maybe_pip_install_caseus_git() -> None:
    try:
        from . import config as cfg
    except ImportError:
        return
    if not getattr(cfg, "PIP_INSTALL_CASEUS_GIT_UPGRADE", False):
        return
    if getattr(sys, "frozen", False):
        return
    spec = getattr(
        cfg,
        "CASEUS_GIT_PIP_SPEC",
        "caseus @ git+https://github.com/friedkeenan/caseus.git",
    )
    cmd = [sys.executable, "-m", "pip", "install", "-U", str(spec).strip()]
    subprocess.run(cmd, check=False)


_maybe_pip_install_caseus_git()

from .ban_cli import main

if __name__ == "__main__":
    main()
