# -*- mode: python ; coding: utf-8 -*-
"""
FlowLocal PyInstaller Spec (--onedir, windowed, Windows standalone).
Raccoglie dipendenze native (PyQt6, Faster-Whisper, CTranslate2, ONNXRuntime,
RapidOCR, QtAwesome, SoundDevice, SciPy, NumPy, Matplotlib, Google API, ecc.)
escludendo tassativamente database, token personali, cache e ambienti virtuali.
"""

import importlib.util
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

SPEC_DIR = os.path.abspath(SPECPATH)

# Estensioni e cartelle da escludere tassativamente dal bundle
EXCLUDED_EXTENSIONS = {".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".log", ".pyc", ".pyo"}
EXCLUDED_FILENAMES = {"token.json", "token_calendar.json", "flow_hub.db", "flow_hub.db-shm", "flow_hub.db-wal"}
EXCLUDED_DIR_NAMES = {"__pycache__", ".venv", ".git", "build", "dist", "data", "note_media", "FlowLocal_Sync"}

datas = []
binaries = []
hiddenimports = []


def _is_excluded(path: str) -> bool:
    norm = os.path.normpath(path)
    parts = set(norm.split(os.sep))
    if parts & EXCLUDED_DIR_NAMES:
        return True
    base = os.path.basename(norm)
    if base.lower() in EXCLUDED_FILENAMES:
        return True
    _, ext = os.path.splitext(base)
    if ext.lower() in EXCLUDED_EXTENSIONS:
        return True
    if base.lower().startswith("token") and ext.lower() == ".json":
        return True
    return False


def collect_package_safe(pkg_name: str, full_collect: bool = True):
    """Raccoglie datas, binaries e hiddenimports di un pacchetto se presente nell'ambiente."""
    try:
        if importlib.util.find_spec(pkg_name) is None:
            return
    except Exception:
        return

    if full_collect:
        try:
            d, b, h = collect_all(pkg_name)
            datas.extend(d)
            binaries.extend(b)
            hiddenimports.extend(h)
            return
        except Exception:
            pass

    try:
        hiddenimports.extend(collect_submodules(pkg_name))
    except Exception:
        hiddenimports.append(pkg_name)


# Pacchetti core con DLL native, modelli ONNX, font o discovery cache
PACKAGES_TO_COLLECT = [
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "rapidocr_onnxruntime",
    "qtawesome",
    "sounddevice",
    "scipy",
    "numpy",
    "sympy",
    "matplotlib",
    "googleapiclient",
    "google_auth_oauthlib",
    "google_auth_httplib2",
    "pynput",
    "psutil",
    "fitz",
    "pymupdf",
    "av",
    "PIL",
    "docx",
    "pycaw",
    "comtypes",
    "pygame",
    "pyttsx3",
    "mss",
    "pyperclip",
    "pystray",
    "edge_tts",
    "tzdata",
    "imageio_ffmpeg",
]

for pkg in PACKAGES_TO_COLLECT:
    collect_package_safe(pkg, full_collect=True)

# Moduli PyQt6 e backend Windows/Audio aggiuntivi
hiddenimports.extend([
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtSvg",
    "PyQt6.QtMultimedia",
    "PyQt6.QtNetwork",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "win32api",
    "win32con",
    "win32gui",
    "sqlite3",
    "runpy",
])

# Moduli locali FlowLocal
for local_pkg in ("core", "ui", "database"):
    pkg_dir = os.path.join(SPEC_DIR, local_pkg)
    if os.path.isdir(pkg_dir):
        hiddenimports.extend(collect_submodules(local_pkg))

# Asset statici di sola lettura (icona + eventuali cartelle assets/ o themes/)
icon_file = os.path.join(SPEC_DIR, "app.ico")
if os.path.isfile(icon_file):
    datas.append((icon_file, "."))

# Credenziali OAuth Google (solo client secret app, MAI token personali)
creds_file = os.path.join(SPEC_DIR, "credentials.json")
if os.path.isfile(creds_file):
    datas.append((creds_file, "."))

for static_dir in ("assets", "themes"):
    src_dir = os.path.join(SPEC_DIR, static_dir)
    if os.path.isdir(src_dir):
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for fname in files:
                full_path = os.path.join(root, fname)
                if not _is_excluded(full_path):
                    rel_dir = os.path.relpath(root, SPEC_DIR)
                    datas.append((full_path, rel_dir))

# Filtro finale di sicurezza su datas per escludere file personali/DB eventualmente raccolti
filtered_datas = []
for src, dst in datas:
    if not _is_excluded(src):
        filtered_datas.append((src, dst))
datas = filtered_datas

a = Analysis(
    [os.path.join(SPEC_DIR, "main.py")],
    pathex=[SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(dict.fromkeys(hiddenimports)),
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
    [],
    exclude_binaries=True,
    name="FlowLocal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file if os.path.isfile(icon_file) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FlowLocal",
)
