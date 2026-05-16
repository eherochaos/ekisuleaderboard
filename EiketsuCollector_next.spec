# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


def _tk_datas():
    python_root = Path(sys.base_prefix)
    items = [
        (python_root / 'tcl' / 'tcl8.6', '_tcl_data'),
        (python_root / 'tcl' / 'tk8.6', '_tk_data'),
        (python_root / 'Lib' / 'tkinter', 'tkinter'),
    ]
    return [(str(source), target) for source, target in items if source.exists()]


a = Analysis(
    ['src\\eiketsu_env\\client_gui.py'],
    pathex=['src'],
    binaries=[],
    datas=_tk_datas(),
    hiddenimports=['tkinter', '_tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='EiketsuCollector_next',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
