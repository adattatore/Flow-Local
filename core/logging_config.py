"""Logging strutturato rotante e diagnostica sicura per FlowLocal."""

from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional

from core.paths import get_user_data_path

APP_VERSION = "3.3.0"
_LOGGING_INITIALIZED = False


def get_log_file_path() -> Path:
    """Restituisce il percorso del file di log rotante nella directory dati utente."""
    return get_user_data_path("logs/flowlocal.log")


def setup_logging(level: int = logging.INFO) -> Path:
    """Configura il logger root con file rotante (2 MB x 3 backup) senza duplicare handler."""
    global _LOGGING_INITIALIZED
    log_path = get_log_file_path()
    if _LOGGING_INITIALIZED:
        return log_path

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        pass

    # Evita rumore eccessivo da librerie di terze parti
    for noisy in ("urllib3", "googleapiclient.discovery_cache", "PIL", "matplotlib", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGING_INITIALIZED = True
    logging.getLogger("FlowLocal").info(
        "FlowLocal v%s avviato su %s (%s, Python %s)",
        APP_VERSION,
        platform.system(),
        platform.release(),
        platform.python_version(),
    )
    return log_path


def collect_diagnostics_snapshot(extra: Optional[Dict[str, str]] = None) -> str:
    """Genera un report diagnostico testuale privo di credenziali o token."""
    lines = [
        f"FlowLocal Diagnostics Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 64,
        f"Versione app : {APP_VERSION}",
        f"OS           : {platform.system()} {platform.release()} ({platform.version()})",
        f"Python       : {sys.version.splitlines()[0]}",
        f"Frozen       : {bool(getattr(sys, 'frozen', False))}",
        f"Log path     : {get_log_file_path()}",
    ]
    if extra:
        lines.append("-" * 64)
        for k, v in extra.items():
            lines.append(f"{k:<16}: {v}")
    lines.append("=" * 64)

    log_path = get_log_file_path()
    if log_path.exists():
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]
            lines.append("\n--- Ultimi 250 eventi di log ---")
            lines.extend(tail)
        except Exception as e:
            lines.append(f"\n[Impossibile leggere il log: {e}]")
    else:
        lines.append("\n[Nessun file di log ancora generato]")

    return "\n".join(lines) + "\n"


def export_diagnostics_file(dest_path: Path, extra: Optional[Dict[str, str]] = None) -> Path:
    """Salva il report diagnostico completo in un file di testo scelto dall'utente."""
    content = collect_diagnostics_snapshot(extra=extra)
    dest_path.write_text(content, encoding="utf-8")
    return dest_path
