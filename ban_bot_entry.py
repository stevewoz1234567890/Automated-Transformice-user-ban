"""
PyInstaller entry script.

``bot/__main__.py`` uses a relative import; PyInstaller runs the entry file as a bare
script, so imports must be absolute (``python -m bot`` still uses ``bot/__main__.py``).
"""

from bot.env_setup import prepare_runtime_environment, require_source_runtime_imports

prepare_runtime_environment()
require_source_runtime_imports()

from bot.ban_cli import main

if __name__ == "__main__":
    main()
