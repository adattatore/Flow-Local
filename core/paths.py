"""
Risoluzione centralizzata dei percorsi per FlowLocal (modalità sviluppo, Linux e PyInstaller frozen).

- Risorse statiche impacchettate (sola lettura): risolte via sys._MEIPASS quando
  getattr(sys, 'frozen', False) è True, altrimenti dalla root del progetto
  (es. app.ico, credentials.json, assets/, temi QSS).
- Dati utente scrivibili:
  - Su Linux (sys.platform.startswith("linux")): ~/.local/share/FlowLocal
    (es. flow_hub.db, token.json, token_calendar.json, FlowLocal_Sync, cache e file temporanei).
  - Su Windows frozen: %LOCALAPPDATA%\\FlowLocal.
  - Su Windows sviluppo: root del progetto per retrocompatibilità.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """Restituisce True se l'applicazione è in esecuzione come eseguibile PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def get_project_root() -> Path:
    """Restituisce la root del repository in sviluppo o la cartella dell'eseguibile in frozen."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_resource_dir() -> Path:
    """
    Restituisce la directory delle risorse statiche di sola lettura.
    In modalità frozen usa sys._MEIPASS (es. _internal).
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_resource_path(relative_path: str | Path) -> Path:
    """Risolve il percorso assoluto di una risorsa statica impacchettata."""
    return get_resource_dir() / relative_path


def get_user_data_dir() -> Path:
    """
    Restituisce la directory scrivibile per i dati utente.
    - Su Linux (sys.platform.startswith("linux")): ~/.local/share/FlowLocal
      (creandola automaticamente al primo avvio).
    - In modalità frozen (Windows): %LOCALAPPDATA%\\FlowLocal (creandola se assente).
    - In modalità sviluppo Windows: root del progetto per retrocompatibilità.
    """
    if sys.platform.startswith("linux"):
        data_dir = Path.home() / ".local" / "share" / "FlowLocal"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    if is_frozen():
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        data_dir = Path(local_app_data) / "FlowLocal"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    return Path(__file__).resolve().parent.parent


def get_user_data_path(relative_path: str | Path) -> Path:
    """
    Risolve un percorso scrivibile nella directory dati utente,
    assicurando l'esistenza delle cartelle padre.
    """
    target = get_user_data_dir() / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def get_cache_dir() -> Path:
    """Restituisce la directory scrivibile per la cache dell'applicazione."""
    cache_dir = get_user_data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_temp_dir() -> Path:
    """Restituisce la directory scrivibile per i file temporanei dell'applicazione."""
    temp_dir = get_user_data_dir() / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir
