# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for NFL-TCKR (console)
# Build:  pyinstaller NFL-TCKR-console.spec
# Output: dist\NFL-TCKR-console.exe  (single-file, console visible)
#
# Requirements: pyinstaller >= 6.0

import os
import sys

from PyInstaller.utils.hooks import collect_all as _pyi_collect_all

_spec_dir = os.path.dirname(os.path.abspath(SPEC))
_pyd_abi_tag = f"cp{sys.version_info.major}{sys.version_info.minor}-win_amd64.pyd"

_pyqt5_datas = []
_pyqt5_binaries = []
_pyqt5_hidden = []

_QT_EXCLUDE = (
    "Qt3D", "QtWebEngine", "QtWebEngineCore", "QtWebEngineWidgets",
    "QtMultimedia", "QtMultimediaWidgets",
    "QtQml", "QtQuick", "QtQuickWidgets",
    "QtSql", "QtTest", "QtBluetooth", "QtPositioning",
    "QtSensors", "QtSerialPort", "QtWebSockets",
    "QtXml", "QtXmlPatterns",
    "geometryloaders", "renderers", "sceneparsers",
    "sqldrivers", "webview", "geoservices",
    "port_v2",
)


def _qt_keep(name):
    return not any(x.lower() in name.lower() for x in _QT_EXCLUDE)


try:
    import PyQt5
    print(f"[SPEC] PyQt5 found at: {os.path.dirname(PyQt5.__file__)}")
    _raw_datas, _raw_bins, _raw_hidden = _pyi_collect_all("PyQt5")
    _pyqt5_datas = [(s, d) for s, d in _raw_datas if _qt_keep(s)]
    _pyqt5_binaries = [(s, d) for s, d in _raw_bins if _qt_keep(s)]
    _pyqt5_hidden = [h for h in _raw_hidden if _qt_keep(h) and h != "sip"]
    print(
        f"[SPEC] collect_all PyQt5: "
        f"{len(_pyqt5_binaries)} bins, {len(_pyqt5_datas)} datas, "
        f"{len(_pyqt5_hidden)} hidden"
    )
    import PyQt5.sip as _pyqt5_sip
    _sip_src = getattr(_pyqt5_sip, "__file__", None)
    if _sip_src and os.path.isfile(_sip_src):
        _sip_already = any(
            os.path.normcase(os.path.abspath(s)) == os.path.normcase(os.path.abspath(_sip_src))
            for s, _d in _pyqt5_binaries
        )
        if not _sip_already:
            _pyqt5_binaries.append((_sip_src, "PyQt5"))
            print(f"[SPEC] Explicitly bundling PyQt5.sip: {_sip_src}")
    else:
        print("[SPEC] WARNING: PyQt5.sip.__file__ not found")
except Exception as _e:
    print(f"[SPEC] collect_all PyQt5 ERROR: {_e}")

_datas = []
for _folder in ("fonts", "logos", "images"):
    _src = os.path.join(_spec_dir, _folder)
    if os.path.isdir(_src):
        _datas.append((_src, _folder))
        print(f"[SPEC] Bundling folder {_folder}/")
    else:
        print(f"[SPEC] WARNING: {_folder}/ not found")

try:
    import certifi
    _datas.append((certifi.where(), "certifi"))
except Exception as _e:
    print(f"[SPEC] certifi not bundled: {_e}")

a = Analysis(
    ["NFL-TCKR.py"],
    pathex=[_spec_dir],
    binaries=_pyqt5_binaries,
    datas=_datas + _pyqt5_datas,
    hiddenimports=[
        "PyQt5.sip",
        "requests",
        "requests.adapters",
        "requests.auth",
        "requests.exceptions",
        "urllib3",
        "urllib3.util.retry",
        "certifi",
        "charset_normalizer",
        "charset_normalizer.md",
        "idna",
        "unicodedata",
        "encodings",
        "encodings.idna",
        "encodings.utf_8",
        "encodings.ascii",
        "encodings.latin_1",
        "encodings.cp1252",
        "json",
        "datetime",
        "traceback",
        "ctypes",
        "ctypes.wintypes",
    ] + _pyqt5_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pandas",
        "matplotlib",
        "scipy",
        "numpy",
        "numba",
        "llvmlite",
        "tkinter",
        "_tkinter",
        "wx",
        "pytest",
        "IPython",
        "notebook",
        "Cython",
        "PyQt5.Qt3DAnimation",
        "PyQt5.Qt3DCore",
        "PyQt5.Qt3DExtras",
        "PyQt5.Qt3DInput",
        "PyQt5.Qt3DLogic",
        "PyQt5.Qt3DRender",
        "PyQt5.QtWebEngine",
        "PyQt5.QtWebEngineCore",
        "PyQt5.QtWebEngineWidgets",
        "PyQt5.QtMultimedia",
        "PyQt5.QtMultimediaWidgets",
        "PyQt5.QtQml",
        "PyQt5.QtQuick",
        "PyQt5.QtQuickWidgets",
        "PyQt5.QtSql",
        "PyQt5.QtTest",
        "PyQt5.QtBluetooth",
        "PyQt5.QtPositioning",
        "PyQt5.QtSensors",
        "PyQt5.QtSerialPort",
        "PyQt5.QtWebSockets",
        "PyQt5.QtXml",
        "PyQt5.QtXmlPatterns",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NFL-TCKR-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        "Qt5Core.dll", "Qt5Gui.dll", "Qt5Widgets.dll",
        "Qt5Network.dll", "Qt5PrintSupport.dll",
        f"QtWidgets.{_pyd_abi_tag}",
        f"QtCore.{_pyd_abi_tag}",
        f"QtGui.{_pyd_abi_tag}",
        f"QtNetwork.{_pyd_abi_tag}",
        f"QtPrintSupport.{_pyd_abi_tag}",
        f"sip.{_pyd_abi_tag}",
    ],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version-nfl-tckr.txt",
    uac_admin=False,
    uac_uiaccess=False,
)
