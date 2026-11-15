"""
export_linux_deb.py
Genera il pacchetto installabile Debian/Ubuntu/Linux Mint (.deb) in puro Python:
  dist/flowlocal_0.1.0_amd64.deb

Non richiede dpkg-deb sul sistema host (funziona anche da Windows).
Su Linux Mint / Ubuntu si installa con:
  sudo apt install ./flowlocal_0.1.0_amd64.deb
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

from export_linux_bundle import (
    EXCLUDED_DIR_NAMES,
    INCLUDE_DIRS,
    OPTIONAL_FILES,
    PROJECT_ROOT,
    REQUIRED_FILES,
    is_excluded,
)

PACKAGE_NAME = "flowlocal"
PACKAGE_VERSION = "0.1.0"
PACKAGE_ARCH = "amd64"
DIST_DIR = PROJECT_ROOT / "dist"
OUTPUT_DEB = DIST_DIR / f"{PACKAGE_NAME}_{PACKAGE_VERSION}_{PACKAGE_ARCH}.deb"


def _add_bytes_to_tar(
    tar: tarfile.TarFile,
    arcname: str,
    data: bytes,
    mode: int = 0o644,
    mtime: int | None = None,
) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime if mtime is not None else int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _add_dir_to_tar(tar: tarfile.TarFile, arcname: str, mode: int = 0o755, mtime: int | None = None) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime if mtime is not None else int(time.time())
    tar.addfile(info)


def _ar_member(name: str, data: bytes, mtime: int, mode: str = "100644") -> bytes:
    """Costruisce un singolo membro dell'archivio UNIX `ar` (formato Debian .deb)."""
    header = (
        f"{name:<16}"
        f"{mtime:<12}"
        f"{'0':<6}"
        f"{'0':<6}"
        f"{mode:<8}"
        f"{len(data):<10}"
        "`\n"
    ).encode("ascii")
    if len(header) != 60:
        raise ValueError(f"Header ar non valido ({len(header)} byte) per {name}")
    padding = b"\n" if (len(data) % 2 == 1) else b""
    return header + data + padding


def _build_data_tar_gz(now: int) -> tuple[bytes, int, int]:
    """Costruisce data.tar.gz con /opt/flowlocal, /usr/bin/flowlocal, .desktop e icona."""
    buf = io.BytesIO()
    total_kb = 0
    file_count = 0

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in (
            "./opt",
            "./opt/flowlocal",
            "./usr",
            "./usr/bin",
            "./usr/share",
            "./usr/share/applications",
            "./usr/share/icons",
            "./usr/share/icons/hicolor",
            "./usr/share/icons/hicolor/512x512",
            "./usr/share/icons/hicolor/512x512/apps",
        ):
            _add_dir_to_tar(tar, d, mode=0o755, mtime=now)

        # 1. File nella root del progetto -> /opt/flowlocal/
        for fname in REQUIRED_FILES + OPTIONAL_FILES:
            fpath = PROJECT_ROOT / fname
            if not fpath.is_file() or is_excluded(Path(fname)):
                continue
            raw = fpath.read_bytes()
            if fname.endswith(".sh"):
                raw = raw.replace(b"\r\n", b"\n")
                mode = 0o755
            else:
                mode = 0o644
            _add_bytes_to_tar(tar, f"./opt/flowlocal/{fname}", raw, mode=mode, mtime=now)
            total_kb += max(1, (len(raw) + 1023) // 1024)
            file_count += 1

        # 2. Directory sorgenti -> /opt/flowlocal/<dir>/
        for dname in INCLUDE_DIRS:
            dir_path = PROJECT_ROOT / dname
            if not dir_path.is_dir():
                continue
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
                rel_dir = Path(root).relative_to(PROJECT_ROOT).as_posix()
                _add_dir_to_tar(tar, f"./opt/flowlocal/{rel_dir}", mode=0o755, mtime=now)
                for fname in sorted(files):
                    fpath = Path(root) / fname
                    rel_path = fpath.relative_to(PROJECT_ROOT)
                    if is_excluded(rel_path):
                        continue
                    raw = fpath.read_bytes()
                    mode = 0o755 if fpath.suffix == ".sh" else 0o644
                    _add_bytes_to_tar(tar, f"./opt/flowlocal/{rel_path.as_posix()}", raw, mode=mode, mtime=now)
                    total_kb += max(1, (len(raw) + 1023) // 1024)
                    file_count += 1

        # 3. Launcher CLI /usr/bin/flowlocal
        launcher_sh = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "export QT_QPA_PLATFORM=xcb\n"
            "if [ ! -x /opt/flowlocal/.venv/bin/python ]; then\n"
            "  echo 'Ambiente virtuale non trovato in /opt/flowlocal/.venv.'\n"
            "  echo 'Esegui: cd /opt/flowlocal && ./setup_ubuntu.sh'\n"
            "  exit 1\n"
            "fi\n"
            "exec /opt/flowlocal/.venv/bin/python /opt/flowlocal/main.py \"$@\"\n"
        ).encode("utf-8")
        _add_bytes_to_tar(tar, "./usr/bin/flowlocal", launcher_sh, mode=0o755, mtime=now)
        file_count += 1

        # 4. Icona applicazione /usr/share/icons/hicolor/512x512/apps/flowlocal.png
        icon_src = PROJECT_ROOT / "mobile_pwa" / "icon-512.png"
        if icon_src.is_file():
            icon_bytes = icon_src.read_bytes()
            _add_bytes_to_tar(
                tar,
                "./usr/share/icons/hicolor/512x512/apps/flowlocal.png",
                icon_bytes,
                mode=0o644,
                mtime=now,
            )
            total_kb += max(1, (len(icon_bytes) + 1023) // 1024)
            file_count += 1

        # 5. Voce Desktop /usr/share/applications/flowlocal.desktop
        desktop_entry = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=FlowLocal\n"
            "Comment=Dettatura vocale locale, trascrizione acustica e studio AI\n"
            "Exec=env QT_QPA_PLATFORM=xcb /opt/flowlocal/.venv/bin/python /opt/flowlocal/main.py\n"
            "Path=/opt/flowlocal\n"
            "Icon=flowlocal\n"
            "Terminal=false\n"
            "Categories=Office;Education;Utility;\n"
            "StartupNotify=true\n"
        ).encode("utf-8")
        _add_bytes_to_tar(tar, "./usr/share/applications/flowlocal.desktop", desktop_entry, mode=0o644, mtime=now)
        file_count += 1

    return buf.getvalue(), total_kb, file_count


def _build_control_tar_gz(now: int, installed_size_kb: int) -> bytes:
    """Costruisce control.tar.gz con control, postinst e prerm."""
    control_txt = (
        f"Package: {PACKAGE_NAME}\n"
        f"Version: {PACKAGE_VERSION}\n"
        "Section: utils\n"
        "Priority: optional\n"
        f"Architecture: {PACKAGE_ARCH}\n"
        f"Installed-Size: {installed_size_kb}\n"
        "Maintainer: FlowLocal <support@flowlocal.local>\n"
        "Depends: libportaudio2, libxcb-cursor0, libxcb-xinerama0, libxkbcommon-x11-0, ffmpeg, curl\n"
        "Description: FlowLocal - Dettatura vocale locale, trascrizione acustica e studio AI\n"
        " Assistente desktop locale per dettatura vocale con Faster-Whisper,\n"
        " raffinamento contestuale LLM/Gemini e workspace di studio.\n"
    ).encode("utf-8")

    postinst_sh = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "export PATH=\"/root/.local/bin:/usr/local/bin:$PATH\"\n"
        "if ! command -v uv >/dev/null 2>&1; then\n"
        "  echo '[FlowLocal] Installazione di uv...'\n"
        "  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh\n"
        "fi\n"
        "if command -v uv >/dev/null 2>&1; then\n"
        "  echo '[FlowLocal] Sincronizzazione ambiente Python isolato in /opt/flowlocal/.venv...'\n"
        "  cd /opt/flowlocal && uv sync\n"
        "else\n"
        "  echo '[FlowLocal] ATTENZIONE: uv non installato. Esegui manualmente: cd /opt/flowlocal && ./setup_ubuntu.sh'\n"
        "fi\n"
        "if command -v update-desktop-database >/dev/null 2>&1; then\n"
        "  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true\n"
        "fi\n"
        "exit 0\n"
    ).encode("utf-8")

    prerm_sh = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "if [ \"$1\" = \"remove\" ] || [ \"$1\" = \"purge\" ]; then\n"
        "  rm -rf /opt/flowlocal/.venv\n"
        "  find /opt/flowlocal -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true\n"
        "fi\n"
        "exit 0\n"
    ).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_bytes_to_tar(tar, "./control", control_txt, mode=0o644, mtime=now)
        _add_bytes_to_tar(tar, "./postinst", postinst_sh, mode=0o755, mtime=now)
        _add_bytes_to_tar(tar, "./prerm", prerm_sh, mode=0o755, mtime=now)
    return buf.getvalue()


def build_deb_package() -> Path:
    """Genera dist/flowlocal_0.1.0_amd64.deb e restituisce il percorso creato."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if OUTPUT_DEB.exists():
        OUTPUT_DEB.unlink()

    now = int(time.time())
    data_tgz, installed_kb, file_count = _build_data_tar_gz(now)
    control_tgz = _build_control_tar_gz(now, installed_kb)
    debian_binary = b"2.0\n"

    deb_bytes = (
        b"!<arch>\n"
        + _ar_member("debian-binary", debian_binary, now)
        + _ar_member("control.tar.gz", control_tgz, now)
        + _ar_member("data.tar.gz", data_tgz, now)
    )
    OUTPUT_DEB.write_bytes(deb_bytes)

    size_mb = len(deb_bytes) / (1024 * 1024)
    print(f"[export_linux_deb] Pacchetto .deb creato: {OUTPUT_DEB}")
    print(f"[export_linux_deb] File inclusi: {file_count} | Dimensione: {size_mb:.2f} MB")
    return OUTPUT_DEB


if __name__ == "__main__":
    build_deb_package()
