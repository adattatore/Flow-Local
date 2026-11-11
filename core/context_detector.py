"""
Rilevamento Contesto Attivo per FlowLocal (Context-Aware Switching).
Usa ctypes.windll.user32 e ctypes.windll.kernel32 per determinare
la finestra attiva in primo piano e mappare la modalità di dettatura ottimale.
"""

import os
import re
import ctypes
from ctypes import wintypes
from typing import Tuple

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400

COMMON_APP_NAMES = {
    "chrome": "Chrome",
    "msedge": "Edge",
    "edge": "Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "notepad": "Notepad",
    "explorer": "Desktop",
    "spotify": "Spotify",
    "slack": "Slack",
}


def clean_app_name(title: str, exe_name: str) -> str:
    """Restituisce un nome pulito e leggibile dell'applicazione."""
    if exe_name:
        base = os.path.splitext(exe_name)[0].lower()
        if base in COMMON_APP_NAMES:
            return COMMON_APP_NAMES[base]
        clean_base = re.sub(r'[-_]+', ' ', base).strip()
        if clean_base:
            return clean_base.title()

    if title:
        parts = [p.strip() for p in title.split(" - ") if p.strip()]
        if parts:
            candidate = parts[-1]
            if len(candidate) <= 20:
                return candidate

    return "Desktop"


def get_foreground_window_info() -> Tuple[str, str]:
    """
    Legge il titolo della finestra in primo piano (GetForegroundWindow + GetWindowTextW)
    e il nome del file eseguibile del relativo processo.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""

    # Titolo finestra
    title = ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

    # Process ID ed eseguibile
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe_name = ""
    if pid.value:
        for flags in (PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_QUERY_INFORMATION):
            h_proc = kernel32.OpenProcess(flags, False, pid.value)
            if h_proc:
                try:
                    pbuf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(h_proc, 0, pbuf, ctypes.byref(size)):
                        exe_name = os.path.basename(pbuf.value)
                        break
                finally:
                    kernel32.CloseHandle(h_proc)

    return title, exe_name


def map_context(title: str, exe_name: str) -> Tuple[str, str, str]:
    """
    Mappa il titolo e l'eseguibile nei parametri (app_label, mode_key, mode_label).

    Regole di mapping:
    - Se titolo o processo contiene Antigravity, Code, Cursor, WindowsTerminal, cmd, powershell:
      -> ("Antigravity" o nome app, "code", "Code / Tech")
    - Se titolo contiene Gmail, Outlook, Mail, PEC, Thunderbird:
      -> ("Mail", "formal", "Formale")
    - Se titolo contiene Obsidian, Notion, Word, Overleaf, PDF:
      -> ("Appunti", "study_latex", "Studio / LaTeX")
    - Se titolo contiene WhatsApp, Telegram, Discord:
      -> ("Chat", "chat", "Chat / Diretto")
    - In tutti gli altri casi:
      -> (nome_app_pulito, "natural", "Naturale")
    """
    t = (title or "").lower()
    e = (exe_name or "").lower()
    combined = f"{t} {e}"

    # 1. Coding / Tech
    if "antigravity" in combined:
        return ("Antigravity", "code", "Code / Tech")
    elif "cursor" in combined:
        return ("Cursor", "code", "Code / Tech")
    elif "code" in combined:
        return ("VS Code", "code", "Code / Tech")
    elif "windowsterminal" in combined or "wt.exe" in e:
        return ("Terminal", "code", "Code / Tech")
    elif "powershell" in combined or "pwsh" in combined:
        return ("PowerShell", "code", "Code / Tech")
    elif re.search(r'\bcmd(\.exe)?\b', combined):
        return ("Cmd", "code", "Code / Tech")

    # 2. Formal / Mail
    for kw in ("gmail", "outlook", "mail", "pec", "thunderbird"):
        if kw in combined:
            return ("Mail", "formal", "Formale")

    # 3. Studio / LaTeX
    for kw in ("obsidian", "notion", "word", "overleaf", "pdf"):
        if kw in combined:
            return ("Appunti", "study_latex", "Studio / LaTeX")

    # 4. Chat / Diretto
    for kw in ("whatsapp", "telegram", "discord"):
        if kw in combined:
            return ("Chat", "chat", "Chat / Diretto")

    # 5. Default / Naturale
    app_label = clean_app_name(title, exe_name)
    return (app_label, "natural", "Naturale")


def detect_active_context() -> Tuple[str, str, str]:
    """
    Rileva la finestra attiva e restituisce (app_label, mode_key, mode_label).
    In caso di errore o assenza di finestra attiva, restituisce un fallback sicuro.
    """
    try:
        title, exe_name = get_foreground_window_info()
        return map_context(title, exe_name)
    except Exception:
        return ("Desktop", "natural", "Naturale")

