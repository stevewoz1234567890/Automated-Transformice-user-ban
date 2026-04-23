# -*- mode: python ; coding: utf-8 -*-
# Build: python build_exe.py   →  ban_bot.exe in repo root
import importlib.util

from PyInstaller.utils.hooks import collect_all

# Fail at build time if deps are missing from *this* interpreter (avoids a broken exe).
for _mod, _hint in (
    ("caseus", "pip install git+https://github.com/friedkeenan/caseus.git"),
    ("pak", "pip install pak"),
):
    if importlib.util.find_spec(_mod) is None:
        raise SystemExit(
            f"ban_bot.spec: `{_mod}` is not importable in the Python running PyInstaller.\n"
            f"  {_hint}\n"
            "Use the same venv/interpreter for `pip install` and `python build_exe.py`."
        )

block_cipher = None

datas, binaries, hiddenimports = [], [], []
# ``pak`` is a separate PyPI package (caseus dependency) and is imported in ``bot/ban_proxy.py``;
# PyInstaller often omits it from the one-file bundle unless collected explicitly.
for pkg in ("caseus", "pak", "aiohttp", "colorama", "dotenv"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["ban_bot_entry.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ban_bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir="tmp",
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Avoid CopyIcons resource updates that often hit WinError 32 (AV / locked handles).
    icon="NONE",
)
