"""Entry: load ``.env``, optional pip install for caseus, then run the CLI."""

from __future__ import annotations

import subprocess
import sys

from .env_setup import env_truthy, prepare_runtime_environment

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


_maybe_pip_install_caseus_git()

from .ban_cli import main  # noqa: E402

if __name__ == "__main__":
    main()
