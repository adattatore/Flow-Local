"""
Build & Release Pipeline per FlowLocal (Windows Standalone + Inno Setup).
Uso:
    uv run python build_release.py
    python build_release.py [--skip-smoke] [--no-clean]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SPEC_PATH = PROJECT_ROOT / "FlowLocal.spec"
ISS_PATH = PROJECT_ROOT / "installer.iss"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
APP_DIST_DIR = DIST_DIR / "FlowLocal"
APP_EXE_PATH = APP_DIST_DIR / "FlowLocal.exe"
SETUP_EXE_PATH = DIST_DIR / "FlowLocal_Setup.exe"


def _step(title: str):
    print(f"\n{'=' * 70}\n[FlowLocal Build] {title}\n{'=' * 70}", flush=True)


def ensure_pyinstaller():
    _step("1/5 Verifica PyInstaller nell'ambiente attivo")
    try:
        import PyInstaller  # noqa: F401
        print(f"PyInstaller già presente: {PyInstaller.__version__}")
        return
    except ImportError:
        print("PyInstaller non trovato. Installazione in corso...")

    uv_bin = shutil.which("uv")
    if uv_bin:
        cmd = [uv_bin, "pip", "install", "pyinstaller"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "pyinstaller"]

    print(f"Esecuzione: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)

    import PyInstaller as _pi  # noqa: F401
    print(f"PyInstaller installato con successo: {_pi.__version__}")


def clean_previous_outputs():
    _step("2/5 Pulizia artefatti precedenti")
    for target in (BUILD_DIR / "FlowLocal", APP_DIST_DIR):
        if target.exists():
            print(f"Rimozione {target}...")
            shutil.rmtree(target, ignore_errors=True)
    if SETUP_EXE_PATH.exists():
        SETUP_EXE_PATH.unlink(missing_ok=True)


def run_pyinstaller():
    _step("3/5 Build PyInstaller (--onedir) con FlowLocal.spec")
    if not SPEC_PATH.is_file():
        raise FileNotFoundError(f"Spec file non trovato: {SPEC_PATH}")

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_PATH)]
    print(f"Esecuzione: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)

    if not APP_EXE_PATH.is_file():
        raise RuntimeError(f"Build completata ma eseguibile mancante: {APP_EXE_PATH}")

    # Verifica assenza file personali nel bundle
    leaked = []
    for root, _, files in os.walk(APP_DIST_DIR):
        for fname in files:
            low = fname.lower()
            if low.endswith((".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".log")) or (
                low.startswith("token") and low.endswith(".json")
            ):
                leaked.append(os.path.join(root, fname))
    if leaked:
        raise RuntimeError(f"Trovati file sensibili/DB nel bundle: {leaked}")

    print(f"Bundle generato con successo: {APP_EXE_PATH}")


def smoke_test_executable():
    _step("4/5 Verifica import, percorsi frozen e avvio di FlowLocal.exe")
    smoke_code = """
import os
import sys

assert getattr(sys, "frozen", False), "sys.frozen deve essere True"
assert hasattr(sys, "_MEIPASS"), "sys._MEIPASS mancante"

from core.paths import is_frozen, get_resource_path, get_user_data_dir
assert is_frozen() is True
assert get_resource_path(os.path.join("assets", "guides", "gemini_key", "README.txt")).is_file(), "assets/guides/gemini_key/README.txt mancante in _MEIPASS"

from core.gemini_client import DEFAULT_GEMINI_TIMEOUT, normalize_provider
assert DEFAULT_GEMINI_TIMEOUT == 3.0, f"Timeout Gemini errato: {DEFAULT_GEMINI_TIMEOUT}"
assert normalize_provider("adaptive") == "adaptive"

local_app = os.path.normcase(os.path.abspath(os.environ.get("LOCALAPPDATA", "")))
user_dir = os.path.normcase(os.path.abspath(str(get_user_data_dir())))
assert local_app and user_dir.startswith(local_app), f"User data dir non in LOCALAPPDATA: {user_dir}"

import database.db_manager as dbm
assert os.path.normcase(dbm.DB_PATH).startswith(user_dir), f"DB_PATH errato: {dbm.DB_PATH}"

import core.calendar_sync as gcal
assert os.path.normcase(gcal.DEFAULT_TOKEN_PATH).startswith(user_dir), f"TOKEN_PATH errato: {gcal.DEFAULT_TOKEN_PATH}"

# Verifica import critici nel bundle
modules = [
    "PyQt6.QtWidgets",
    "PyQt6.QtSvg",
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "rapidocr_onnxruntime",
    "qtawesome",
    "sounddevice",
    "scipy",
    "numpy",
    "matplotlib",
    "googleapiclient.discovery",
    "google_auth_oauthlib.flow",
    "psutil",
    "pynput",
    "core.ai_pipeline",
    "ui.hub_window",
    "ui.voice_orb",
    "ui.scientific_editor",
]
for mod in modules:
    __import__(mod)

print("SMOKE_IMPORTS_OK")
"""
    smoke_home = BUILD_DIR / "smoke_home"
    smoke_localappdata = smoke_home / "AppData" / "Local"
    smoke_localappdata.mkdir(parents=True, exist_ok=True)
    smoke_env = os.environ.copy()
    smoke_env["LOCALAPPDATA"] = str(smoke_localappdata)
    smoke_env["USERPROFILE"] = str(smoke_home)
    smoke_env["HOME"] = str(smoke_home)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", dir=str(smoke_home), delete=False, encoding="utf-8"
    ) as tf:
        tf.write(smoke_code)
        tmp_script = tf.name

    try:
        res = subprocess.run(
            [str(APP_EXE_PATH), "--exec-python", tmp_script],
            cwd=str(APP_DIST_DIR),
            env=smoke_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0 or "SMOKE_IMPORTS_OK" not in res.stdout:
            raise RuntimeError(
                f"Smoke test import fallito (rc={res.returncode}):\n"
                f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
            )
        print("Tutti gli import critici e i percorsi frozen (%LOCALAPPDATA%\\FlowLocal / _MEIPASS) verificati OK.")
    finally:
        try:
            os.remove(tmp_script)
        except OSError:
            pass

    # Test di avvio reale del processo GUI per 4 secondi
    proc = subprocess.Popen(
        [str(APP_EXE_PATH)],
        cwd=str(APP_DIST_DIR),
        env=smoke_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(4.0)
    ret = proc.poll()
    if ret is not None:
        out, err = proc.communicate(timeout=5)
        shutil.rmtree(smoke_home, ignore_errors=True)
        raise RuntimeError(f"FlowLocal.exe è terminato prematuramente (exit={ret}):\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(smoke_home, ignore_errors=True)
    print("Avvio reale di FlowLocal.exe verificato OK (nessun crash all'avvio).")


def find_iscc() -> Path | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    which_iscc = shutil.which("ISCC") or shutil.which("ISCC.exe")
    return Path(which_iscc) if which_iscc else None


def build_installer():
    _step("5/5 Compilazione Installer Windows (Inno Setup 6)")
    iscc = find_iscc()
    if iscc is None:
        print(
            "Inno Setup 6 (ISCC.exe) non è installato su questo PC.\n"
            "Per generare dist\\FlowLocal_Setup.exe in un clic:\n"
            "  1. Installa Inno Setup 6 (es. `winget install JRSoftware.InnoSetup` oppure da https://jrsoftware.org/isdl.php)\n"
            "  2. Esegui nel terminale:\n"
            f'       ISCC.exe "{ISS_PATH}"\n'
            "     oppure apri installer.iss con Inno Setup Compiler e premi Ctrl+F9 (Compile)."
        )
        return

    print(f"Trovato ISCC.exe: {iscc}")
    subprocess.run([str(iscc), str(ISS_PATH)], cwd=str(PROJECT_ROOT), check=True)
    if SETUP_EXE_PATH.is_file():
        size_mb = SETUP_EXE_PATH.stat().st_size / (1024 * 1024)
        print(f"Installer generato con successo: {SETUP_EXE_PATH} ({size_mb:.1f} MB)")
    else:
        raise RuntimeError(f"ISCC terminato ma {SETUP_EXE_PATH} non trovato.")


def main():
    parser = argparse.ArgumentParser(description="Build release FlowLocal per Windows")
    parser.add_argument("--no-clean", action="store_true", help="Non pulire build/dist prima della build")
    parser.add_argument("--skip-smoke", action="store_true", help="Salta la verifica di avvio dell'eseguibile")
    args = parser.parse_args()

    ensure_pyinstaller()
    if not args.no_clean:
        clean_previous_outputs()
    run_pyinstaller()
    if not args.skip_smoke:
        smoke_test_executable()
    build_installer()
    print("\n[FlowLocal Build] Pipeline completata!")


if __name__ == "__main__":
    main()
