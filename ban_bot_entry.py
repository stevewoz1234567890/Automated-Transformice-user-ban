"""
PyInstaller entry script.

``bot/__main__.py`` uses a relative import; PyInstaller runs the entry file as a bare
script, so imports must be absolute (``python -m bot`` still uses ``bot/__main__.py``).
"""

from bot.ban_cli import main

if __name__ == "__main__":
    main()
