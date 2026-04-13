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


def _maybe_pip_install_tfm_secrets_cli() -> None:
    """If config sets a pip spec, install it so ``tfm-secrets`` may appear in the venv Scripts (before headless load)."""
    try:
        from . import config as cfg
    except ImportError:
        return
    if getattr(sys, "frozen", False):
        return
    if not getattr(cfg, "PIP_INSTALL_TFM_SECRETS_CLI", False):
        return
    spec = str(getattr(cfg, "TFM_SECRETS_PIP_INSTALL_SPEC", "") or "").strip()
    if not spec:
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", spec],
        check=False,
    )


_maybe_pip_install_tfm_secrets_cli()
_maybe_pip_install_caseus_git()

from .ban_cli import main

if __name__ == "__main__":
    main()
