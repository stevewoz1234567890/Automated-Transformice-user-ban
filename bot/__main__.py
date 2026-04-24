"""Entry: load ``.env``, optional pip installs, then run the CLI."""

from __future__ import annotations

import subprocess
import sys

from .env_setup import (
    env_truthy,
    prepare_runtime_environment,
    require_source_runtime_imports,
)

prepare_runtime_environment()


def _maybe_pip_install_caseus_git() -> None:
    if getattr(sys, "frozen", False):
        return
    if not env_truthy("BOT_PIP_INSTALL_CASEUS_GIT_UPGRADE"):
        return
    spec = (
        sys.environ.get(
            "BOT_CASEUS_GIT_PIP_SPEC",
            "caseus @ git+https://github.com/friedkeenan/caseus.git",
        )
        or "caseus @ git+https://github.com/friedkeenan/caseus.git"
    ).strip()
    subprocess.run([sys.executable, "-m", "pip", "install", "-U", spec], check=False)


def _maybe_pip_install_tfm_secrets_cli() -> None:
    """If ``.env`` sets a pip spec, install it so ``tfm-secrets`` may appear in the venv Scripts."""
    if getattr(sys, "frozen", False):
        return
    if not env_truthy("BOT_PIP_INSTALL_TFM_SECRETS_CLI"):
        return
    spec = str(sys.environ.get("BOT_TFM_SECRETS_PIP_INSTALL_SPEC", "") or "").strip()
    if not spec:
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", spec],
        check=False,
    )


_maybe_pip_install_tfm_secrets_cli()
_maybe_pip_install_caseus_git()

require_source_runtime_imports()

from .ban_cli import main

if __name__ == "__main__":
    main()
