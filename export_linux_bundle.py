"""
export_linux_bundle.py
Genera il pacchetto portatile pulito per Linux Mint / Ubuntu:
  dist/FlowLocal_LinuxMint_Ubuntu.tar.gz

Contiene la cartella radice FlowLocal/ con tutto il necessario per l'installazione
1-clic tramite setup_ubuntu.sh, escludendo tassativamente database, token OAuth,
ambienti virtuali e artefatti di build.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / "dist"
OUTPUT_ARCHIVE = DIST_DIR / "FlowLocal_LinuxMint_Ubuntu.tar.gz"
ARCHIVE_ROOT = "FlowLocal"

# File singoli nella root del progetto da includere (obbligatori e opzionali)
REQUIRED_FILES = [
    "main.py",
    "pyproject.toml",
    "setup_ubuntu.sh",
]

OPTIONAL_FILES = [
    "uv.lock",
    "credentials.json",
    "app.ico",
    ".python-version",
    "README.md",
]

# Directory da includere ricorsivamente
INCLUDE_DIRS = [
    "core",
    "ui",
    "database",
    "assets",
    "mobile_pwa",
]

EXCLUDED_DIR_NAMES = {
    ".venv",
    "venv",
    "dist",
    "build",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "note_media",
}

EXCLUDED_EXTENSIONS = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".pyo",
}


def is_excluded(path: Path) -> bool:
    """Restituisce True se il file o la directory devono essere esclusi dal bundle."""
    name_low = path.name.lower()

    # Esclusione cartelle riservate
    for part in path.parts:
        if part in EXCLUDED_DIR_NAMES:
            return True

    # Esclusione file database, bytecode e token OAuth
    if path.suffix.lower() in EXCLUDED_EXTENSIONS:
        return True
    if name_low == "token.json" or (name_low.startswith("token_") and name_low.endswith(".json")):
        return True

    return False


def _add_file_to_tar(tar: tarfile.TarFile, src_path: Path, rel_path: Path) -> None:
    """Aggiunge un singolo file all'archivio tar.gz normalizzando permessi e LF per .sh."""
    arcname = f"{ARCHIVE_ROOT}/{rel_path.as_posix()}"

    if src_path.name == "setup_ubuntu.sh":
        raw_bytes = src_path.read_bytes().replace(b"\r\n", b"\n")
        info = tarfile.TarInfo(name=arcname)
        info.size = len(raw_bytes)
        info.mode = 0o755
        info.mtime = int(src_path.stat().st_mtime)
        tar.addfile(info, io.BytesIO(raw_bytes))
        return

    info = tar.gettarinfo(str(src_path), arcname=arcname)
    info.mode = 0o755 if src_path.suffix == ".sh" else 0o644
    with src_path.open("rb") as fh:
        tar.addfile(info, fh)


def build_linux_bundle() -> Path:
    """Costruisce dist/FlowLocal_LinuxMint_Ubuntu.tar.gz e restituisce il percorso creato."""
    for req in REQUIRED_FILES:
        req_path = PROJECT_ROOT / req
        if not req_path.is_file():
            raise FileNotFoundError(f"File obbligatorio mancante: {req_path}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if OUTPUT_ARCHIVE.exists():
        OUTPUT_ARCHIVE.unlink()

    included_count = 0
    excluded_count = 0

    with tarfile.open(OUTPUT_ARCHIVE, "w:gz") as tar:
        # 1. File nella root
        for fname in REQUIRED_FILES + OPTIONAL_FILES:
            fpath = PROJECT_ROOT / fname
            if not fpath.is_file():
                continue
            if is_excluded(Path(fname)):
                excluded_count += 1
                continue
            _add_file_to_tar(tar, fpath, Path(fname))
            included_count += 1

        # 2. Directory del progetto
        for dname in INCLUDE_DIRS:
            dir_path = PROJECT_ROOT / dname
            if not dir_path.is_dir():
                continue
            for root, dirs, files in os.walk(dir_path):
                # Potatura immediata delle directory escluse
                dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
                for fname in sorted(files):
                    fpath = Path(root) / fname
                    rel_path = fpath.relative_to(PROJECT_ROOT)
                    if is_excluded(rel_path):
                        excluded_count += 1
                        continue
                    _add_file_to_tar(tar, fpath, rel_path)
                    included_count += 1

    size_mb = OUTPUT_ARCHIVE.stat().st_size / (1024 * 1024)
    print(f"[export_linux_bundle] Archivio creato: {OUTPUT_ARCHIVE}")
    print(f"[export_linux_bundle] File inclusi: {included_count} | Esclusi: {excluded_count} | Dimensione: {size_mb:.2f} MB")
    return OUTPUT_ARCHIVE


if __name__ == "__main__":
    build_linux_bundle()
