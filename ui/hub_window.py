"""
FlowLocalHub (PyQt6)
Centro di controllo desktop minimal-dark (960x640).

Layout:
- Sidebar fissa a sinistra (210 px): titolo, stato motore, navigazione, promemoria scorciatoia.
- QStackedWidget a 4 pagine:
  1. Cronologia     — ricerca fissa + lista scorrevole delle trascrizioni
  2. Note           — ricerca fissa + esportazione Markdown + lista note
  3. Dizionario     — form fisso (alias -> parola corretta) + lista regole
  4. Stili & Prompt — selettore modalità + editor System Prompt

Regole UI (AGENTS.md §7): solo QSS valido Qt6, form/ricerche fuori dalla QScrollArea
con altezza minima esplicita, liste scorrevoli anti-compressione.
"""

from datetime import datetime, date as dt_date, timedelta
from typing import Callable, Dict, List, Optional, Any
import ctypes
from ctypes import wintypes
import os
import re
import tempfile
import threading
import pyperclip
try:
    import psutil
except ImportError:
    psutil = None

# Compatibilità PyAV >= 13/14 con Faster-Whisper
try:
    import av
    if hasattr(av, "open"):
        _orig_av_open = av.open
        def _safe_av_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return _orig_av_open(*args, **kwargs)
        av.open = _safe_av_open
    if hasattr(av, "container") and hasattr(av.container, "core") and hasattr(av.container.core, "open"):
        av.container.core.open = _safe_av_open
except Exception:
    pass

import math
import random

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPoint, QPointF, QRect, QRectF, QLineF,
    QSettings, QByteArray, QSize, QDate, QLocale,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve,
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QFontMetrics, QPainterPath, QRadialGradient,
    QLinearGradient, QCursor, QPolygonF, QIcon, QPixmap, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCalendarWidget, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QComboBox, QFontComboBox, QSlider, QSpinBox, QTreeWidget, QTreeWidgetItem, QSplitter
)

from database import db
from core.exporter import export_to_md, export_to_docx, export_to_pdf, export_to_tex
from core.audio_sync import archive_audio_for_note, save_note_audio_sync, load_note_audio_sync
from ui.scientific_editor import DictationGhostLineEdit, ScientificNoteEditor, strip_markdown_syntax
from ui.audio_sync_bar import AudioSyncBar
from ui.notes_tree import (
    SamsungNotesTreeWidget,
    ROLE_NODE_DATA,
    ROLE_COUNT,
    ROLE_FOLDER_COLOR,
    ROLE_IS_FOLDER,
    ROLE_LOCKED_FOLDER,
    make_vector_folder_icon,
)
from ui.command_palette import GlobalCommandPalette
from core.cloud_sync import cloud_sync
from core.whis_agent import WhisAgent, WhisStreamWorker
from core.audio_feedback import (
    audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE,
    VOICE_PRESETS, DEFAULT_VOICE_KEY, TEST_PHRASE
)
from core.audio_ducker import audio_ducker

# Rilevamento GPU tramite NVML (opzionale, senza warning)
GPU_AVAILABLE = False
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pynvml
        pynvml.nvmlInit()
        pynvml.nvmlDeviceGetHandleByIndex(0)
        GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False


# =====================================================================
# STYLESHEET GLOBALE MULTI-TEMA (solo proprietà QSS supportate da Qt6)
# Temi: "obsidian" (scuro), "light" (chiaro minimale), "orb" (chiaro aurora 3 fuochi),
#       "dark_orb" (scuro obsidian + riflessi aurora 3 fuochi),
#       "ghibli_spring" (floreale anime pastello), "pixel_chill" (arcade 16-bit lo-fi),
#       "katana" (dark fantasy affilato metallico),
#       "tactical_hud" (visore tattico notturno), "industrial_mecha" (hangar robotico heavy-duty),
#       "amber_crt" (terminale fosforo ambra anni '80), "starfield_alabaster" (NASA-punk ceramica/ottone),
#       "cyber_synthwave" (outrun neon ciano/magenta)
# Modalità Lettura: overlay di toni caldi riposanti (seppia / ambra espresso)
# =====================================================================
_DARK_THEME_KEYS = {
    "obsidian",
    "dark_orb",
    "pixel_chill",
    "katana",
    "tactical_hud",
    "industrial_mecha",
    "amber_crt",
    "cyber_synthwave",
}


def _is_dark_theme(theme_key: str) -> bool:
    return theme_key in _DARK_THEME_KEYS


_THEME_PALETTES = {
    "obsidian": {
        "text": "#E2E8F0",
        "title": "#F8FAFC",
        "subtitle": "#94A3B8",
        "muted": "#64748B",
        "sec_header": "#475569",
        "root_bg": "#07090F",
        "sidebar_bg": "#05070B",
        "sidebar_border": "rgba(255, 255, 255, 0.06)",
        "nav_hover": "rgba(255, 255, 255, 0.04)",
        "nav_hover_text": "#F1F5F9",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.16), stop:0.6 rgba(168, 85, 247, 0.12), stop:1 rgba(16, 185, 129, 0.06))"
        ),
        "nav_checked_border": "rgba(56, 189, 248, 0.28)",
        "nav_checked_text": "#F8FAFC",
        "surface_soft": "rgba(255, 255, 255, 0.025)",
        "card_bg": "rgba(255, 255, 255, 0.028)",
        "card_hover_bg": "rgba(255, 255, 255, 0.042)",
        "card_border": "rgba(255, 255, 255, 0.06)",
        "card_hover_border": "rgba(56, 189, 248, 0.22)",
        "card_body": "#CBD5E1",
        "input_bg": "#0D111A",
        "input_focus_bg": "#111724",
        "input_border": "#1C2536",
        "input_text": "#F1F5F9",
        "btn_bg": "#131926",
        "btn_hover_bg": "#192233",
        "btn_pressed_bg": "#0F141E",
        "btn_border": "#1F293D",
        "btn_hover_border": "#2B3954",
        "btn_text": "#E2E8F0",
        "pill_bg": "#0F141F",
        "pill_border": "#1C2536",
        "pill_hover_bg": "#161E2E",
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.18), stop:1 rgba(168, 85, 247, 0.16))"
        ),
        "pill_checked_text": "#7DD3FC",
        "badge_bg": "rgba(56, 189, 248, 0.12)",
        "badge_border": "rgba(56, 189, 248, 0.30)",
        "badge_text": "#7DD3FC",
        "badge_muted_bg": "rgba(168, 85, 247, 0.10)",
        "badge_muted_border": "rgba(168, 85, 247, 0.25)",
        "badge_muted_text": "#C4B5FD",
        "engine_bg": "rgba(16, 185, 129, 0.10)",
        "engine_border": "rgba(16, 185, 129, 0.28)",
        "engine_text": "#34D399",
        "shortcut_key_bg": "rgba(56, 189, 248, 0.10)",
        "shortcut_key_border": "rgba(56, 189, 248, 0.28)",
        "shortcut_key_text": "#38BDF8",
        "scroll_handle": "#1E283B",
        "dialog_bg": "#0B0E16",
        "combo_popup_bg": "#0B0E16",
        "divider": "#27272A",
        "editor_bg": "#0D111A",
        "nk_container_bg": "#0B0F17",
        "nk_sidebar_bg": "rgba(255, 255, 255, 0.015)",
        "nk_card_active_bg": "rgba(99, 102, 241, 0.16)",
        "nk_pill_bg": "rgba(255, 255, 255, 0.06)",
        "nk_pill_hover_bg": "rgba(255, 255, 255, 0.11)",
        "chevron_color": "#94A3B8",
    },
    "light": {
        "text": "#1E293B",
        "title": "#0F172A",
        "subtitle": "#475569",
        "muted": "#64748B",
        "sec_header": "#94A3B8",
        "root_bg": "#F4F6FA",
        "sidebar_bg": "#FFFFFF",
        "sidebar_border": "#E2E8F0",
        "nav_hover": "#F1F5F9",
        "nav_hover_text": "#0F172A",
        "nav_checked_bg": "#E0F2FE",
        "nav_checked_border": "#BAE6FD",
        "nav_checked_text": "#0369A1",
        "surface_soft": "#F8FAFC",
        "card_bg": "#FFFFFF",
        "card_hover_bg": "#FFFFFF",
        "card_border": "#E2E8F0",
        "card_hover_border": "#94A3B8",
        "card_body": "#334155",
        "input_bg": "#FFFFFF",
        "input_focus_bg": "#FFFFFF",
        "input_border": "#CBD5E1",
        "input_text": "#0F172A",
        "btn_bg": "#FFFFFF",
        "btn_hover_bg": "#F1F5F9",
        "btn_pressed_bg": "#E2E8F0",
        "btn_border": "#CBD5E1",
        "btn_hover_border": "#94A3B8",
        "btn_text": "#1E293B",
        "pill_bg": "#FFFFFF",
        "pill_border": "#CBD5E1",
        "pill_hover_bg": "#F1F5F9",
        "pill_checked_bg": "#E0F2FE",
        "pill_checked_text": "#0369A1",
        "badge_bg": "#E0F2FE",
        "badge_border": "#BAE6FD",
        "badge_text": "#0369A1",
        "badge_muted_bg": "#F3E8FF",
        "badge_muted_border": "#E9D5FF",
        "badge_muted_text": "#7E22CE",
        "engine_bg": "#DCFCE7",
        "engine_border": "#86EFAC",
        "engine_text": "#15803D",
        "shortcut_key_bg": "#E0F2FE",
        "shortcut_key_border": "#7DD3FC",
        "shortcut_key_text": "#0284C7",
        "scroll_handle": "#CBD5E1",
        "dialog_bg": "#FFFFFF",
        "combo_popup_bg": "#FFFFFF",
        "divider": "#E4E7EC",
        "editor_bg": "#FFFFFF",
        "nk_container_bg": "#FFFFFF",
        "nk_sidebar_bg": "#F8FAFC",
        "nk_card_active_bg": "#EEF2FF",
        "nk_pill_bg": "#F1F5F9",
        "nk_pill_hover_bg": "#E2E8F0",
        "chevron_color": "#64748B",
    },
    "orb": {
        "text": "#0F172A",
        "title": "#0B1324",
        "subtitle": "#475569",
        "muted": "#64748B",
        "sec_header": "#64748B",
        "root_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #ECFEFF, stop:0.48 #F5F3FF, stop:1 #ECFDF5)"
        ),
        "sidebar_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #F0FDFF, stop:0.55 #F6F3FF, stop:1 #F0FDF9)"
        ),
        "sidebar_border": "rgba(56, 189, 248, 0.22)",
        "nav_hover": "rgba(56, 189, 248, 0.10)",
        "nav_hover_text": "#0F172A",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.22), stop:0.55 rgba(168, 85, 247, 0.16), stop:1 rgba(16, 185, 129, 0.14))"
        ),
        "nav_checked_border": "rgba(56, 189, 248, 0.45)",
        "nav_checked_text": "#0C4A6E",
        "surface_soft": "rgba(255, 255, 255, 0.65)",
        "card_bg": "rgba(255, 255, 255, 0.78)",
        "card_hover_bg": "rgba(255, 255, 255, 0.94)",
        "card_border": "rgba(56, 189, 248, 0.22)",
        "card_hover_border": "rgba(168, 85, 247, 0.40)",
        "card_body": "#1E293B",
        "input_bg": "rgba(255, 255, 255, 0.88)",
        "input_focus_bg": "#FFFFFF",
        "input_border": "rgba(56, 189, 248, 0.30)",
        "input_text": "#0F172A",
        "btn_bg": "rgba(255, 255, 255, 0.82)",
        "btn_hover_bg": "#FFFFFF",
        "btn_pressed_bg": "#E0F2FE",
        "btn_border": "rgba(148, 163, 184, 0.35)",
        "btn_hover_border": "rgba(56, 189, 248, 0.55)",
        "btn_text": "#0F172A",
        "pill_bg": "rgba(255, 255, 255, 0.75)",
        "pill_border": "rgba(56, 189, 248, 0.25)",
        "pill_hover_bg": "#FFFFFF",
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.24), stop:1 rgba(168, 85, 247, 0.20))"
        ),
        "pill_checked_text": "#0369A1",
        "badge_bg": "rgba(56, 189, 248, 0.16)",
        "badge_border": "rgba(56, 189, 248, 0.38)",
        "badge_text": "#0369A1",
        "badge_muted_bg": "rgba(168, 85, 247, 0.14)",
        "badge_muted_border": "rgba(168, 85, 247, 0.32)",
        "badge_muted_text": "#7E22CE",
        "engine_bg": "rgba(16, 185, 129, 0.16)",
        "engine_border": "rgba(16, 185, 129, 0.40)",
        "engine_text": "#047857",
        "shortcut_key_bg": "rgba(56, 189, 248, 0.16)",
        "shortcut_key_border": "rgba(56, 189, 248, 0.42)",
        "shortcut_key_text": "#0284C7",
        "scroll_handle": "rgba(56, 189, 248, 0.35)",
        "dialog_bg": "#F5FAFF",
        "combo_popup_bg": "#FFFFFF",
        "divider": "#E4E7EC",
        "editor_bg": "#FFFFFF",
        "nk_container_bg": "rgba(255, 255, 255, 0.88)",
        "nk_sidebar_bg": "rgba(248, 250, 252, 0.72)",
        "nk_card_active_bg": "#F3E8FF",
        "nk_pill_bg": "#F1F5F9",
        "nk_pill_hover_bg": "#E2E8F0",
        "chevron_color": "#64748B",
    },
    "dark_orb": {
        "text": "#F1F5F9",
        "title": "#F8FAFC",
        "subtitle": "#BAE6FD",
        "muted": "#7DD3FC",
        "sec_header": "#38BDF8",
        "root_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #0C2D48, stop:0.48 #241242, stop:1 #0A3328)"
        ),
        "sidebar_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #0E3250, stop:0.50 #28144A, stop:1 #0B382C)"
        ),
        "sidebar_border": "rgba(56, 189, 248, 0.45)",
        "nav_hover": "rgba(56, 189, 248, 0.16)",
        "nav_hover_text": "#FFFFFF",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.44), stop:0.52 rgba(168, 85, 247, 0.40), stop:1 rgba(16, 185, 129, 0.34))"
        ),
        "nav_checked_border": "rgba(56, 189, 248, 0.65)",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.14), stop:0.50 rgba(168, 85, 247, 0.14), stop:1 rgba(16, 185, 129, 0.12))"
        ),
        "card_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(14, 52, 82, 0.65), stop:0.52 rgba(42, 20, 74, 0.60), stop:1 rgba(12, 54, 42, 0.60))"
        ),
        "card_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(20, 70, 108, 0.82), stop:0.52 rgba(58, 28, 102, 0.78), stop:1 rgba(16, 72, 56, 0.78))"
        ),
        "card_border": "rgba(56, 189, 248, 0.38)",
        "card_hover_border": "rgba(168, 85, 247, 0.70)",
        "card_body": "#E2E8F0",
        "input_bg": "rgba(10, 34, 56, 0.82)",
        "input_focus_bg": "rgba(16, 48, 78, 0.95)",
        "input_border": "rgba(56, 189, 248, 0.42)",
        "input_text": "#F8FAFC",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(14, 56, 88, 0.78), stop:0.52 rgba(46, 22, 82, 0.74), stop:1 rgba(12, 58, 44, 0.74))"
        ),
        "btn_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(24, 82, 126, 0.92), stop:0.52 rgba(68, 32, 118, 0.88), stop:1 rgba(18, 82, 64, 0.88))"
        ),
        "btn_pressed_bg": "#0D2338",
        "btn_border": "rgba(56, 189, 248, 0.42)",
        "btn_hover_border": "rgba(168, 85, 247, 0.75)",
        "btn_text": "#F1F5F9",
        "pill_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(14, 52, 82, 0.72), stop:0.55 rgba(44, 20, 78, 0.68), stop:1 rgba(12, 54, 42, 0.68))"
        ),
        "pill_border": "rgba(56, 189, 248, 0.40)",
        "pill_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(24, 76, 118, 0.88), stop:0.55 rgba(64, 30, 112, 0.85), stop:1 rgba(18, 76, 58, 0.85))"
        ),
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.45), stop:0.52 rgba(168, 85, 247, 0.42), stop:1 rgba(16, 185, 129, 0.36))"
        ),
        "pill_checked_text": "#F0F9FF",
        "badge_bg": "rgba(56, 189, 248, 0.24)",
        "badge_border": "rgba(56, 189, 248, 0.55)",
        "badge_text": "#BAE6FD",
        "badge_muted_bg": "rgba(168, 85, 247, 0.24)",
        "badge_muted_border": "rgba(168, 85, 247, 0.50)",
        "badge_muted_text": "#E9D5FF",
        "engine_bg": "rgba(16, 185, 129, 0.22)",
        "engine_border": "rgba(16, 185, 129, 0.52)",
        "engine_text": "#6EE7B7",
        "shortcut_key_bg": "rgba(56, 189, 248, 0.22)",
        "shortcut_key_border": "rgba(56, 189, 248, 0.52)",
        "shortcut_key_text": "#7DD3FC",
        "scroll_handle": "rgba(56, 189, 248, 0.45)",
        "dialog_bg": "#0D2036",
        "combo_popup_bg": "#0D2036",
        "divider": "rgba(56, 189, 248, 0.30)",
        "editor_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #091E30, stop:0.50 #180D2E, stop:1 #07221B)"
        ),
        "nk_container_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #0A253B, stop:0.50 #1E1038, stop:1 #082B22)"
        ),
        "nk_sidebar_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(14, 58, 90, 0.75), stop:0.52 rgba(48, 22, 84, 0.75), stop:1 rgba(12, 60, 46, 0.75))"
        ),
        "nk_card_active_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.36), stop:0.55 rgba(168, 85, 247, 0.34), stop:1 rgba(16, 185, 129, 0.28))"
        ),
        "nk_pill_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.22), stop:0.55 rgba(168, 85, 247, 0.22), stop:1 rgba(16, 185, 129, 0.18))"
        ),
        "nk_pill_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 rgba(56, 189, 248, 0.36), stop:0.55 rgba(168, 85, 247, 0.34), stop:1 rgba(16, 185, 129, 0.28))"
        ),
        "chevron_color": "#7DD3FC",
    },
    "ghibli_spring": {
        "text": "#2D312E",
        "title": "#222623",
        "subtitle": "#525B54",
        "muted": "#6E7870",
        "sec_header": "#5B9BD5",
        "root_bg": "#FCF9F2",
        "sidebar_bg": "#EDF3E8",
        "sidebar_border": "#D3E2CA",
        "nav_hover": "rgba(242, 132, 158, 0.16)",
        "nav_hover_text": "#2D312E",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #F69DB2, stop:1 #F2849E)"
        ),
        "nav_checked_border": "#D96B85",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": "#EBF3F9",
        "card_bg": "#EDF3E8",
        "card_hover_bg": "#F4F8F0",
        "card_border": "#CFE0C5",
        "card_hover_border": "#F2849E",
        "card_body": "#2D312E",
        "input_bg": "#FFFFFF",
        "input_focus_bg": "#FFFDF9",
        "input_border": "#C5D9BC",
        "input_text": "#2D312E",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #F69DB2, stop:1 #F2849E)"
        ),
        "btn_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #FFADC0, stop:1 #F69DB2)"
        ),
        "btn_pressed_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #F2849E, stop:1 #D96B85)"
        ),
        "btn_border": "#F7B2C4",
        "btn_hover_border": "#F7B2C4",
        "btn_text": "#FFFFFF",
        "pill_bg": "#EBF3F9",
        "pill_border": "#BFD8EC",
        "pill_hover_bg": "#DCECF8",
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #F69DB2, stop:1 #F2849E)"
        ),
        "pill_checked_text": "#FFFFFF",
        "badge_bg": "#EBF3F9",
        "badge_border": "#5B9BD5",
        "badge_text": "#2B6CB0",
        "badge_muted_bg": "#FEF6DF",
        "badge_muted_border": "#F4C44E",
        "badge_muted_text": "#9C6B10",
        "engine_bg": "#E2F0DA",
        "engine_border": "#8CC078",
        "engine_text": "#2E6B22",
        "shortcut_key_bg": "#FEF6DF",
        "shortcut_key_border": "#F4C44E",
        "shortcut_key_text": "#9C6B10",
        "scroll_handle": "#F2849E",
        "scroll_handle_hover": "#D96B85",
        "dialog_bg": "#FCF9F2",
        "combo_popup_bg": "#FCF9F2",
        "divider": "#D3E2CA",
        "editor_bg": "#FCF9F2",
        "nk_container_bg": "#FCF9F2",
        "nk_sidebar_bg": "#EDF3E8",
        "nk_card_active_bg": "#FADCE4",
        "nk_pill_bg": "#EBF3F9",
        "nk_pill_hover_bg": "#DCECF8",
        "chevron_color": "#5B9BD5",
        "focus_border": "#F2849E",
        "selection_bg": "#F2849E",
        "accent": "#F2849E",
        "primary_grad": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #F69DB2, stop:1 #F2849E)"
        ),
        "primary_hover_grad": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #FFADC0, stop:1 #F69DB2)"
        ),
        "card_radius": "14px",
        "input_radius": "14px",
        "combo_radius": "14px",
        "btn_radius": "18px",
        "pill_radius": "18px",
        "top_box_radius": "18px",
        "top_btn_radius": "15px",
        "sub_btn_radius": "14px",
        "nav_btn_radius": "16px",
        "nk_radius": "14px",
        "badge_radius": "12px",
        "small_radius": "10px",
        "scroll_radius": "3px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #CFE0C5; border-bottom: 2px solid #B8D0AA;",
        "card_hover_border_qss": "border: 1px solid #F2849E; border-bottom: 2px solid #D96B85;",
        "input_border_qss": "border: 1px solid #C5D9BC; border-bottom: 2px solid #AFC9A4;",
        "input_focus_border_qss": "border: 1px solid #F2849E; border-bottom: 2px solid #D96B85;",
        "topbar_border_qss": "border-bottom: 2px solid #D3E2CA;",
        "sidebar_border_qss": "border-right: 2px solid #D3E2CA;",
        "top_box_border_qss": "border: 1px solid #BFD8EC; border-bottom: 2px solid #A4C7E4;",
        "btn_padding": "4px 16px",
        "btn_border_qss": "border: 1px solid #F7B2C4; border-bottom: 3px solid #D96B85;",
        "btn_hover_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFADC0, stop:1 #F69DB2);\n"
            "    border: 1px solid #F7B2C4;\n"
            "    border-bottom: 3px solid #D96B85;\n"
            "    color: #FFFFFF;"
        ),
        "btn_pressed_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F2849E, stop:1 #D96B85);\n"
            "    border: 1px solid #D96B85;\n"
            "    border-bottom: 1px solid #D96B85;\n"
            "    padding-top: 6px;\n"
            "    padding-bottom: 2px;"
        ),
        "primary_border_qss": "border: 1px solid #F7B2C4; border-bottom: 3px solid #D96B85;",
        "primary_hover_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFADC0, stop:1 #F69DB2);\n"
            "    border: 1px solid #F7B2C4;\n"
            "    border-bottom: 3px solid #D96B85;\n"
            "    color: #FFFFFF;"
        ),
        "primary_pressed_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F2849E, stop:1 #D96B85);\n"
            "    border: 1px solid #D96B85;\n"
            "    border-bottom: 1px solid #D96B85;\n"
            "    padding-top: 6px;\n"
            "    padding-bottom: 2px;"
        ),
        "top_btn_hover_qss": (
            "color: #2D312E;\n"
            "    background-color: rgba(242, 132, 158, 0.16);\n"
            "    border: 1px solid #F7B2C4;"
        ),
        "top_btn_pressed_qss": (
            "background-color: rgba(242, 132, 158, 0.28);\n"
            "    padding-top: 7px;\n"
            "    padding-bottom: 3px;"
        ),
        "top_btn_checked_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F69DB2, stop:1 #F2849E);\n"
            "    border: 1px solid #F7B2C4;\n"
            "    border-bottom: 2px solid #D96B85;\n"
            "    color: #FFFFFF;"
        ),
        "pill_border_qss": "border: 1px solid #BFD8EC; border-bottom: 2px solid #A4C7E4;",
        "pill_hover_qss": (
            "background-color: #DCECF8;\n"
            "    border: 1px solid #5B9BD5;\n"
            "    border-bottom: 2px solid #4A86C0;\n"
            "    color: #222623;"
        ),
        "pill_pressed_qss": (
            "border-bottom: 1px solid #D96B85;\n"
            "    padding-top: 2px;"
        ),
        "pill_checked_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #F69DB2, stop:1 #F2849E);\n"
            "    border: 1px solid #F7B2C4;\n"
            "    border-bottom: 2px solid #D96B85;\n"
            "    color: #FFFFFF;"
        ),
    },
    "pixel_chill": {
        "text": "#F0EAD6",
        "title": "#F0EAD6",
        "subtitle": "#DFC89A",
        "muted": "#A9B4B8",
        "sec_header": "#8CA87C",
        "root_bg": "#4A5559",
        "sidebar_bg": "#374044",
        "sidebar_border": "#1E2426",
        "nav_hover": "#4A5559",
        "nav_hover_text": "#F0EAD6",
        "nav_checked_bg": "#D94E34",
        "nav_checked_border": "#1E2426",
        "nav_checked_text": "#F0EAD6",
        "surface_soft": "#3E484C",
        "card_bg": "#374044",
        "card_hover_bg": "#3F494E",
        "card_border": "#1E2426",
        "card_hover_border": "#8CA87C",
        "card_body": "#F0EAD6",
        "input_bg": "#2B3236",
        "input_focus_bg": "#23292C",
        "input_border": "#1E2426",
        "input_text": "#F0EAD6",
        "btn_bg": "#D94E34",
        "btn_hover_bg": "#E55A40",
        "btn_pressed_bg": "#C04129",
        "btn_border": "#1E2426",
        "btn_hover_border": "#1E2426",
        "btn_text": "#F0EAD6",
        "pill_bg": "#374044",
        "pill_border": "#1E2426",
        "pill_hover_bg": "#4A5559",
        "pill_checked_bg": "#D94E34",
        "pill_checked_text": "#F0EAD6",
        "badge_bg": "#2B3236",
        "badge_border": "#8CA87C",
        "badge_text": "#8CA87C",
        "badge_muted_bg": "#2B3236",
        "badge_muted_border": "#DFC89A",
        "badge_muted_text": "#DFC89A",
        "engine_bg": "#2B3629",
        "engine_border": "#8CA87C",
        "engine_text": "#A5C494",
        "shortcut_key_bg": "#2B3236",
        "shortcut_key_border": "#DFC89A",
        "shortcut_key_text": "#DFC89A",
        "scroll_handle": "#8CA87C",
        "scroll_handle_hover": "#DFC89A",
        "dialog_bg": "#374044",
        "combo_popup_bg": "#2B3236",
        "divider": "#1E2426",
        "editor_bg": "#2E363A",
        "nk_container_bg": "#374044",
        "nk_sidebar_bg": "#2F373B",
        "nk_card_active_bg": "#4A5559",
        "nk_pill_bg": "#2B3236",
        "nk_pill_hover_bg": "#4A5559",
        "chevron_color": "#DFC89A",
        "focus_border": "#8CA87C",
        "selection_bg": "#D94E34",
        "accent": "#D94E34",
        "primary_grad": "#D94E34",
        "primary_hover_grad": "#E55A40",
        "font_family": "'Consolas', 'JetBrains Mono', 'Courier New', monospace",
        "card_radius": "0px",
        "input_radius": "0px",
        "combo_radius": "0px",
        "btn_radius": "0px",
        "pill_radius": "0px",
        "top_box_radius": "0px",
        "top_btn_radius": "0px",
        "sub_btn_radius": "0px",
        "nav_btn_radius": "0px",
        "nk_radius": "0px",
        "badge_radius": "0px",
        "small_radius": "0px",
        "scroll_radius": "0px",
        "scroll_width": "8px",
        "card_border_qss": (
            "border-top: 2px solid #627075;\n"
            "    border-left: 2px solid #627075;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;"
        ),
        "card_hover_border_qss": (
            "border-top: 2px solid #8CA87C;\n"
            "    border-left: 2px solid #8CA87C;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;"
        ),
        "input_border_qss": (
            "border-top: 2px solid #1E2426;\n"
            "    border-left: 2px solid #1E2426;\n"
            "    border-right: 2px solid #627075;\n"
            "    border-bottom: 2px solid #627075;"
        ),
        "input_focus_border_qss": (
            "border-top: 2px solid #8CA87C;\n"
            "    border-left: 2px solid #8CA87C;\n"
            "    border-right: 2px solid #DFC89A;\n"
            "    border-bottom: 2px solid #DFC89A;"
        ),
        "topbar_border_qss": "border-bottom: 3px solid #1E2426;",
        "sidebar_border_qss": "border-right: 3px solid #1E2426;",
        "top_box_border_qss": (
            "border-top: 2px solid #627075;\n"
            "    border-left: 2px solid #627075;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;"
        ),
        "btn_padding": "6px 14px",
        "btn_border_qss": (
            "border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 4px solid #1E2426;\n"
            "    border-bottom: 4px solid #1E2426;"
        ),
        "btn_hover_qss": (
            "background-color: #E55A40;\n"
            "    border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 4px solid #1E2426;\n"
            "    border-bottom: 4px solid #1E2426;"
        ),
        "btn_pressed_qss": (
            "background-color: #C04129;\n"
            "    border-right: 1px solid #1E2426;\n"
            "    border-bottom: 1px solid #1E2426;\n"
            "    border-top: 2px solid #1E2426;\n"
            "    border-left: 2px solid #1E2426;\n"
            "    padding-top: 9px;\n"
            "    padding-left: 17px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 11px;"
        ),
        "primary_border_qss": (
            "border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 4px solid #1E2426;\n"
            "    border-bottom: 4px solid #1E2426;"
        ),
        "primary_hover_qss": (
            "background-color: #E55A40;\n"
            "    border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 4px solid #1E2426;\n"
            "    border-bottom: 4px solid #1E2426;"
        ),
        "primary_pressed_qss": (
            "background-color: #C04129;\n"
            "    border-right: 1px solid #1E2426;\n"
            "    border-bottom: 1px solid #1E2426;\n"
            "    border-top: 2px solid #1E2426;\n"
            "    border-left: 2px solid #1E2426;\n"
            "    padding-top: 9px;\n"
            "    padding-left: 17px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 11px;"
        ),
        "top_btn_hover_qss": (
            "color: #F0EAD6;\n"
            "    background-color: #4A5559;\n"
            "    border-top: 2px solid #627075;\n"
            "    border-left: 2px solid #627075;\n"
            "    border-right: 2px solid #1E2426;\n"
            "    border-bottom: 2px solid #1E2426;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #C04129;\n"
            "    border: 2px solid #1E2426;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 18px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 14px;"
        ),
        "top_btn_checked_qss": (
            "background-color: #D94E34;\n"
            "    border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;\n"
            "    color: #F0EAD6;"
        ),
        "pill_border_qss": (
            "border-top: 2px solid #627075;\n"
            "    border-left: 2px solid #627075;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;"
        ),
        "pill_hover_qss": (
            "background-color: #4A5559;\n"
            "    border-top: 2px solid #8CA87C;\n"
            "    border-left: 2px solid #8CA87C;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;\n"
            "    color: #F0EAD6;"
        ),
        "pill_pressed_qss": (
            "border: 1px solid #1E2426;\n"
            "    padding-top: 2px;\n"
            "    padding-left: 16px;\n"
            "    padding-right: 12px;"
        ),
        "pill_checked_qss": (
            "background-color: #D94E34;\n"
            "    border-top: 2px solid #F07B65;\n"
            "    border-left: 2px solid #F07B65;\n"
            "    border-right: 3px solid #1E2426;\n"
            "    border-bottom: 3px solid #1E2426;\n"
            "    color: #F0EAD6;"
        ),
    },
    "katana": {
        "text": "#E5DEC9",
        "title": "#F2ECE1",
        "subtitle": "#C59B5F",
        "muted": "#8C7F70",
        "sec_header": "#C59B5F",
        "root_bg": "#141111",
        "sidebar_bg": "#1F1A1A",
        "sidebar_border": "#3D312A",
        "nav_hover": "#2A1A1A",
        "nav_hover_text": "#FFFFFF",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #C92A2A, stop:0.55 #8F1D1D, stop:1 #2A1212)"
        ),
        "nav_checked_border": "#C59B5F",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": "#1A1515",
        "card_bg": "#1F1A1A",
        "card_hover_bg": "#272020",
        "card_border": "#3D312A",
        "card_hover_border": "#C59B5F",
        "card_body": "#E5DEC9",
        "input_bg": "#181313",
        "input_focus_bg": "#211919",
        "input_border": "#3D312A",
        "input_text": "#E5DEC9",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #2A2222, stop:0.5 #1A1414, stop:1 #241A1A)"
        ),
        "btn_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212)"
        ),
        "btn_pressed_bg": "#3D0E0E",
        "btn_border": "#7A603B",
        "btn_hover_border": "#E6B875",
        "btn_text": "#E5DEC9",
        "pill_bg": "#1F1A1A",
        "pill_border": "#3D312A",
        "pill_hover_bg": "#2D1818",
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #C92A2A, stop:0.55 #8F1D1D, stop:1 #2A1212)"
        ),
        "pill_checked_text": "#FFFFFF",
        "badge_bg": "rgba(201, 42, 42, 0.18)",
        "badge_border": "#C92A2A",
        "badge_text": "#FF8787",
        "badge_muted_bg": "rgba(197, 155, 95, 0.14)",
        "badge_muted_border": "#C59B5F",
        "badge_muted_text": "#E6B875",
        "engine_bg": "rgba(197, 155, 95, 0.15)",
        "engine_border": "#C59B5F",
        "engine_text": "#E6B875",
        "shortcut_key_bg": "rgba(201, 42, 42, 0.18)",
        "shortcut_key_border": "#C59B5F",
        "shortcut_key_text": "#E5DEC9",
        "scroll_handle": "#7A603B",
        "scroll_handle_hover": "#C92A2A",
        "dialog_bg": "#181414",
        "combo_popup_bg": "#181414",
        "divider": "#3D312A",
        "editor_bg": "#161212",
        "nk_container_bg": "#181414",
        "nk_sidebar_bg": "#1F1A1A",
        "nk_card_active_bg": "rgba(201, 42, 42, 0.24)",
        "nk_pill_bg": "#261F1F",
        "nk_pill_hover_bg": "#382626",
        "chevron_color": "#C59B5F",
        "focus_border": "#C92A2A",
        "selection_bg": "#C92A2A",
        "accent": "#C92A2A",
        "primary_grad": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #2A2222, stop:0.5 #1A1414, stop:1 #241A1A)"
        ),
        "primary_hover_grad": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212)"
        ),
        "card_radius": "2px",
        "input_radius": "2px",
        "combo_radius": "2px",
        "btn_radius": "2px",
        "pill_radius": "2px",
        "top_box_radius": "2px",
        "top_btn_radius": "2px",
        "sub_btn_radius": "2px",
        "nav_btn_radius": "2px",
        "nk_radius": "2px",
        "badge_radius": "2px",
        "small_radius": "2px",
        "scroll_radius": "1px",
        "scroll_width": "5px",
        "card_border_qss": "border: 1px solid #3D312A; border-top: 1px solid #6E583B;",
        "card_hover_border_qss": "border: 1px solid #7A603B; border-top: 1px solid #C59B5F; border-left: 2px solid #C92A2A;",
        "input_border_qss": "border: 1px solid #3D312A; border-left: 2px solid #7A603B;",
        "input_focus_border_qss": "border: 1px solid #C59B5F; border-left: 3px solid #C92A2A;",
        "topbar_border_qss": "border-bottom: 1px solid #6E583B;",
        "sidebar_border_qss": "border-right: 1px solid #6E583B;",
        "top_box_border_qss": "border: 1px solid #3D312A; border-top: 1px solid #6E583B;",
        "btn_padding": "5px 13px",
        "btn_border_qss": "border: 1px solid #7A603B; border-left: 3px solid #C59B5F;",
        "btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212);\n"
            "    border: 1px solid #E6B875;\n"
            "    border-left: 3px solid #FF4D4D;\n"
            "    color: #FFFFFF;"
        ),
        "btn_pressed_qss": (
            "background-color: #3D0E0E;\n"
            "    border-color: #C92A2A;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 15px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 11px;"
        ),
        "primary_border_qss": "border: 1px solid #7A603B; border-left: 3px solid #C59B5F;",
        "primary_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212);\n"
            "    border: 1px solid #E6B875;\n"
            "    border-left: 3px solid #FF4D4D;\n"
            "    color: #FFFFFF;"
        ),
        "primary_pressed_qss": (
            "background-color: #3D0E0E;\n"
            "    border-color: #C92A2A;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 15px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 11px;"
        ),
        "top_btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212);\n"
            "    border: 1px solid #E6B875;\n"
            "    border-left: 3px solid #FF4D4D;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #3D0E0E;\n"
            "    border-color: #C92A2A;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 18px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 14px;"
        ),
        "top_btn_checked_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #3A1818, stop:0.5 #241010, stop:1 #301414);\n"
            "    border: 1px solid #7A603B;\n"
            "    border-left: 3px solid #C59B5F;\n"
            "    color: #FFFFFF;"
        ),
        "pill_border_qss": "border: 1px solid #7A603B; border-left: 2px solid #C59B5F;",
        "pill_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #D93838, stop:0.55 #8F1D1D, stop:1 #2A1212);\n"
            "    border: 1px solid #E6B875;\n"
            "    border-left: 2px solid #FF4D4D;\n"
            "    color: #FFFFFF;"
        ),
        "pill_pressed_qss": (
            "background-color: #3D0E0E;\n"
            "    border-color: #C92A2A;\n"
            "    padding-top: 2px;\n"
            "    padding-left: 15px;\n"
            "    padding-right: 13px;"
        ),
        "pill_checked_qss": (
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #591515, stop:0.45 #8F1D1D, stop:0.5 #C92A2A, stop:0.55 #8F1D1D, stop:1 #2A1212);\n"
            "    border: 1px solid #C59B5F;\n"
            "    border-left: 3px solid #FF4D4D;\n"
            "    color: #FFFFFF;"
        ),
    },
    "tactical_hud": {
        "text": "#D4F5E2",
        "title": "#D4F5E2",
        "subtitle": "#6C9980",
        "muted": "#507862",
        "sec_header": "#00FF66",
        "root_bg": "#0B0E11",
        "sidebar_bg": "#13181D",
        "sidebar_border": "#1E3A2B",
        "nav_hover": "#152E20",
        "nav_hover_text": "#FFFFFF",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20)"
        ),
        "nav_checked_border": "#00FF66",
        "nav_checked_text": "#00FF66",
        "surface_soft": "#0F1419",
        "card_bg": "#13181D",
        "card_hover_bg": "#172026",
        "card_border": "#1E3A2B",
        "card_hover_border": "#00FF66",
        "card_body": "#D4F5E2",
        "input_bg": "#0D1317",
        "input_focus_bg": "#111C16",
        "input_border": "#1E3A2B",
        "input_text": "#D4F5E2",
        "btn_bg": "#111C16",
        "btn_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20)"
        ),
        "btn_pressed_bg": "#00FF66",
        "btn_border": "#1E3A2B",
        "btn_hover_border": "#33FF85",
        "btn_text": "#00FF66",
        "pill_bg": "#111C16",
        "pill_border": "#1E3A2B",
        "pill_hover_bg": "#152E20",
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20)"
        ),
        "pill_checked_text": "#00FF66",
        "badge_bg": "rgba(0, 255, 102, 0.12)",
        "badge_border": "#00FF66",
        "badge_text": "#00FF66",
        "badge_muted_bg": "rgba(255, 176, 0, 0.12)",
        "badge_muted_border": "#FFB000",
        "badge_muted_text": "#FFB000",
        "engine_bg": "rgba(0, 255, 102, 0.14)",
        "engine_border": "#00FF66",
        "engine_text": "#00FF66",
        "shortcut_key_bg": "rgba(255, 176, 0, 0.14)",
        "shortcut_key_border": "#FFB000",
        "shortcut_key_text": "#FFB000",
        "scroll_handle": "#1E3A2B",
        "scroll_handle_hover": "#00FF66",
        "dialog_bg": "#0B0E11",
        "combo_popup_bg": "#0B0E11",
        "divider": "#1E3A2B",
        "editor_bg": "#0D1115",
        "nk_container_bg": "#0F1419",
        "nk_sidebar_bg": "#13181D",
        "nk_card_active_bg": "rgba(0, 255, 102, 0.18)",
        "nk_pill_bg": "#111C16",
        "nk_pill_hover_bg": "#152E20",
        "chevron_color": "#00FF66",
        "focus_border": "#00FF66",
        "selection_bg": "#1F4730",
        "accent": "#00FF66",
        "danger_color": "#FF2A2A",
        "primary_grad": "#111C16",
        "primary_hover_grad": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20)"
        ),
        "font_family": "'JetBrains Mono', 'Consolas', 'Courier New', monospace",
        "card_radius": "0px",
        "input_radius": "0px",
        "combo_radius": "0px",
        "btn_radius": "0px",
        "pill_radius": "0px",
        "top_box_radius": "0px",
        "top_btn_radius": "0px",
        "sub_btn_radius": "0px",
        "nav_btn_radius": "0px",
        "nk_radius": "0px",
        "badge_radius": "0px",
        "small_radius": "0px",
        "scroll_radius": "0px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #1E3A2B; border-left: 3px solid #00FF66;",
        "card_hover_border_qss": "border: 1px solid #00FF66; border-left: 3px solid #33FF85;",
        "input_border_qss": "border-top: 1px solid #1E3A2B; border-bottom: 1px solid #1E3A2B; border-left: 3px solid #00FF66; border-right: 3px solid #00FF66;",
        "input_focus_border_qss": "border-top: 1px solid #33FF85; border-bottom: 1px solid #33FF85; border-left: 4px solid #33FF85; border-right: 4px solid #33FF85;",
        "topbar_border_qss": "border-bottom: 2px solid #00FF66;",
        "sidebar_border_qss": "border-right: 2px solid #1E3A2B;",
        "top_box_border_qss": "border-top: 1px solid #1E3A2B; border-bottom: 1px solid #1E3A2B; border-left: 3px solid #00FF66; border-right: 3px solid #00FF66;",
        "btn_padding": "6px 14px",
        "btn_border_qss": "border-top: 1px solid #1E3A2B; border-bottom: 1px solid #1E3A2B; border-left: 4px solid #00FF66; border-right: 4px solid #00FF66;",
        "btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #FFFFFF;\n"
            "    border-color: #33FF85;"
        ),
        "btn_pressed_qss": (
            "background-color: #00FF66;\n"
            "    color: #0B0E11;\n"
            "    border-color: #00FF66;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 15px;\n"
            "    padding-bottom: 5px;\n"
            "    padding-right: 13px;"
        ),
        "primary_text": "#00FF66",
        "primary_border_qss": "border-top: 1px solid #00FF66; border-bottom: 1px solid #00FF66; border-left: 4px solid #00FF66; border-right: 4px solid #00FF66;",
        "primary_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #FFFFFF;\n"
            "    border-color: #33FF85;"
        ),
        "primary_pressed_qss": (
            "background-color: #00FF66;\n"
            "    color: #0B0E11;\n"
            "    border-color: #00FF66;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 15px;\n"
            "    padding-bottom: 5px;\n"
            "    padding-right: 13px;"
        ),
        "top_btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #FFFFFF;\n"
            "    border-top: 1px solid #33FF85;\n"
            "    border-bottom: 1px solid #33FF85;\n"
            "    border-left: 3px solid #33FF85;\n"
            "    border-right: 3px solid #33FF85;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #00FF66;\n"
            "    color: #0B0E11;\n"
            "    padding-top: 7px;\n"
            "    padding-left: 17px;\n"
            "    padding-bottom: 3px;\n"
            "    padding-right: 15px;"
        ),
        "top_btn_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #00FF66;\n"
            "    border-top: 1px solid #1E3A2B;\n"
            "    border-bottom: 1px solid #1E3A2B;\n"
            "    border-left: 4px solid #00FF66;\n"
            "    border-right: 4px solid #00FF66;"
        ),
        "pill_border_qss": "border-top: 1px solid #1E3A2B; border-bottom: 1px solid #1E3A2B; border-left: 3px solid #00FF66; border-right: 3px solid #00FF66;",
        "pill_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #FFFFFF;\n"
            "    border-color: #33FF85;"
        ),
        "pill_pressed_qss": (
            "background-color: #00FF66;\n"
            "    color: #0B0E11;\n"
            "    padding-top: 2px;\n"
            "    padding-left: 15px;\n"
            "    padding-right: 13px;"
        ),
        "pill_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #152E20, stop:0.5 #1F4730, stop:1 #152E20);\n"
            "    color: #00FF66;\n"
            "    border-top: 1px solid #33FF85;\n"
            "    border-bottom: 1px solid #33FF85;\n"
            "    border-left: 4px solid #00FF66;\n"
            "    border-right: 4px solid #00FF66;"
        ),
    },
    "industrial_mecha": {
        "text": "#ECEEEF",
        "title": "#ECEEEF",
        "subtitle": "#9AA0A6",
        "muted": "#7A8086",
        "sec_header": "#F5B700",
        "root_bg": "#232528",
        "sidebar_bg": "#1A1C1E",
        "sidebar_border": "#383B40",
        "nav_hover": "#2F3338",
        "nav_hover_text": "#FFFFFF",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #32363B, stop:1 #26292D)"
        ),
        "nav_checked_border": "#FF5500",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": "#1E2023",
        "card_bg": "#1A1C1E",
        "card_hover_bg": "#222528",
        "card_border": "#383B40",
        "card_hover_border": "#FF5500",
        "card_body": "#ECEEEF",
        "input_bg": "#16181A",
        "input_focus_bg": "#1D2024",
        "input_border": "#383B40",
        "input_text": "#ECEEEF",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #32363B, stop:1 #26292D)"
        ),
        "btn_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #3E434A, stop:1 #2F3338)"
        ),
        "btn_pressed_bg": "#202225",
        "btn_border": "#4A4F57",
        "btn_hover_border": "#F5B700",
        "btn_text": "#ECEEEF",
        "pill_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #32363B, stop:1 #26292D)"
        ),
        "pill_border": "#383B40",
        "pill_hover_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #3E434A, stop:1 #2F3338)"
        ),
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #3E434A, stop:1 #26292D)"
        ),
        "pill_checked_text": "#FFFFFF",
        "badge_bg": "rgba(255, 85, 0, 0.16)",
        "badge_border": "#FF5500",
        "badge_text": "#FF7733",
        "badge_muted_bg": "rgba(245, 183, 0, 0.14)",
        "badge_muted_border": "#F5B700",
        "badge_muted_text": "#F5B700",
        "engine_bg": "rgba(245, 183, 0, 0.15)",
        "engine_border": "#F5B700",
        "engine_text": "#F5B700",
        "shortcut_key_bg": "rgba(255, 85, 0, 0.16)",
        "shortcut_key_border": "#FF5500",
        "shortcut_key_text": "#FF7733",
        "scroll_handle": "#4A4F57",
        "scroll_handle_hover": "#FF5500",
        "dialog_bg": "#1A1C1E",
        "combo_popup_bg": "#1A1C1E",
        "divider": "#383B40",
        "editor_bg": "#1D1F22",
        "nk_container_bg": "#1A1C1E",
        "nk_sidebar_bg": "#1A1C1E",
        "nk_card_active_bg": "rgba(255, 85, 0, 0.22)",
        "nk_pill_bg": "#26292D",
        "nk_pill_hover_bg": "#32363B",
        "chevron_color": "#F5B700",
        "focus_border": "#FF5500",
        "selection_bg": "#FF5500",
        "accent": "#FF5500",
        "primary_grad": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #32363B, stop:1 #26292D)"
        ),
        "primary_hover_grad": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #3E434A, stop:1 #2F3338)"
        ),
        "card_radius": "3px",
        "input_radius": "3px",
        "combo_radius": "3px",
        "btn_radius": "3px",
        "pill_radius": "3px",
        "top_box_radius": "3px",
        "top_btn_radius": "3px",
        "sub_btn_radius": "3px",
        "nav_btn_radius": "3px",
        "nk_radius": "3px",
        "badge_radius": "3px",
        "small_radius": "3px",
        "scroll_radius": "2px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #383B40; border-bottom: 3px solid #141517;",
        "card_hover_border_qss": "border: 1px solid #FF5500; border-bottom: 3px solid #141517; border-left: 4px solid #F5B700;",
        "input_border_qss": "border: 1px solid #383B40; border-left: 3px solid #FF5500;",
        "input_focus_border_qss": "border: 1px solid #F5B700; border-left: 4px solid #F5B700;",
        "topbar_border_qss": "border-bottom: 2px solid #383B40;",
        "sidebar_border_qss": "border-right: 2px solid #383B40;",
        "top_box_border_qss": "border: 1px solid #383B40; border-bottom: 3px solid #141517;",
        "btn_padding": "6px 14px",
        "btn_border_qss": (
            "border-top: 1px solid #4A4F57;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 4px solid #141517;\n"
            "    border-left: 5px solid #FF5500;"
        ),
        "btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E434A, stop:1 #2F3338);\n"
            "    border-top: 1px solid #5A606A;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 4px solid #141517;\n"
            "    border-left: 5px solid #F5B700;\n"
            "    color: #FFFFFF;"
        ),
        "btn_pressed_qss": (
            "background-color: #202225;\n"
            "    border-bottom: 1px solid #141517;\n"
            "    border-top: 2px solid #141517;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-left: 5px solid #FF5500;\n"
            "    padding-top: 9px;\n"
            "    padding-bottom: 3px;"
        ),
        "primary_text": "#ECEEEF",
        "primary_border_qss": (
            "border-top: 1px solid #4A4F57;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 4px solid #141517;\n"
            "    border-left: 5px solid #FF5500;"
        ),
        "primary_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E434A, stop:1 #2F3338);\n"
            "    border-top: 1px solid #5A606A;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 4px solid #141517;\n"
            "    border-left: 5px solid #F5B700;\n"
            "    color: #FFFFFF;"
        ),
        "primary_pressed_qss": (
            "background-color: #202225;\n"
            "    border-bottom: 1px solid #141517;\n"
            "    border-top: 2px solid #141517;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-left: 5px solid #FF5500;\n"
            "    padding-top: 9px;\n"
            "    padding-bottom: 3px;"
        ),
        "top_btn_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E434A, stop:1 #2F3338);\n"
            "    border-top: 1px solid #4A4F57;\n"
            "    border-right: 1px solid #141517;\n"
            "    border-bottom: 2px solid #141517;\n"
            "    border-left: 4px solid #F5B700;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #202225;\n"
            "    border-bottom: 1px solid #141517;\n"
            "    border-top: 2px solid #141517;\n"
            "    border-left: 4px solid #FF5500;\n"
            "    padding-top: 7px;\n"
            "    padding-bottom: 3px;"
        ),
        "top_btn_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #32363B, stop:1 #26292D);\n"
            "    border-top: 1px solid #4A4F57;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 3px solid #141517;\n"
            "    border-left: 5px solid #FF5500;\n"
            "    color: #FFFFFF;"
        ),
        "pill_border_qss": (
            "border-top: 1px solid #4A4F57;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 3px solid #141517;\n"
            "    border-left: 4px solid #FF5500;"
        ),
        "pill_hover_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E434A, stop:1 #2F3338);\n"
            "    border-top: 1px solid #5A606A;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 3px solid #141517;\n"
            "    border-left: 4px solid #F5B700;\n"
            "    color: #FFFFFF;"
        ),
        "pill_pressed_qss": (
            "background-color: #202225;\n"
            "    border-bottom: 1px solid #141517;\n"
            "    border-top: 2px solid #141517;\n"
            "    border-left: 4px solid #FF5500;\n"
            "    padding-top: 3px;"
        ),
        "pill_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E434A, stop:1 #26292D);\n"
            "    border-top: 1px solid #5A606A;\n"
            "    border-right: 2px solid #141517;\n"
            "    border-bottom: 3px solid #141517;\n"
            "    border-left: 5px solid #F5B700;\n"
            "    color: #FFFFFF;"
        ),
    },
    "amber_crt": {
        "text": "#E6CFA8",
        "title": "#E6CFA8",
        "subtitle": "#B89C72",
        "muted": "#7D6340",
        "sec_header": "#FFB642",
        "root_bg": "#120D07",
        "sidebar_bg": "#1A1209",
        "sidebar_border": "#4A3516",
        "nav_hover": "#FFB642",
        "nav_hover_text": "#120D07",
        "nav_checked_bg": "#FFB642",
        "nav_checked_border": "#FFB642",
        "nav_checked_text": "#120D07",
        "surface_soft": "#160F08",
        "card_bg": "#1A1209",
        "card_hover_bg": "#24190C",
        "card_border": "#4A3516",
        "card_hover_border": "#FFB642",
        "card_body": "#E6CFA8",
        "input_bg": "#140E07",
        "input_focus_bg": "#1E150A",
        "input_border": "#4A3516",
        "input_text": "#E6CFA8",
        "btn_bg": "#1A1209",
        "btn_hover_bg": "#FFB642",
        "btn_pressed_bg": "#CC8E2E",
        "btn_border": "#4A3516",
        "btn_hover_border": "#FFB642",
        "btn_text": "#E6CFA8",
        "pill_bg": "#1A1209",
        "pill_border": "#4A3516",
        "pill_hover_bg": "#FFB642",
        "pill_checked_bg": "#FFB642",
        "pill_checked_text": "#120D07",
        "badge_bg": "rgba(255, 182, 66, 0.14)",
        "badge_border": "#FFB642",
        "badge_text": "#FFB642",
        "badge_muted_bg": "rgba(74, 53, 22, 0.35)",
        "badge_muted_border": "#4A3516",
        "badge_muted_text": "#B89C72",
        "engine_bg": "rgba(255, 182, 66, 0.16)",
        "engine_border": "#FFB642",
        "engine_text": "#FFB642",
        "shortcut_key_bg": "rgba(255, 182, 66, 0.14)",
        "shortcut_key_border": "#FFB642",
        "shortcut_key_text": "#FFB642",
        "scroll_handle": "#4A3516",
        "scroll_handle_hover": "#FFB642",
        "dialog_bg": "#120D07",
        "combo_popup_bg": "#120D07",
        "divider": "#4A3516",
        "editor_bg": "#140E07",
        "nk_container_bg": "#160F08",
        "nk_sidebar_bg": "#1A1209",
        "nk_card_active_bg": "rgba(255, 182, 66, 0.20)",
        "nk_pill_bg": "#1A1209",
        "nk_pill_hover_bg": "#2B1E0F",
        "chevron_color": "#E6CFA8",
        "focus_border": "#FFB642",
        "selection_bg": "#7A541E",
        "accent": "#FFB642",
        "primary_grad": "#1F150A",
        "primary_hover_grad": "#FFB642",
        "font_family": "'Consolas', 'JetBrains Mono', 'Courier New', monospace",
        "card_radius": "2px",
        "input_radius": "2px",
        "combo_radius": "2px",
        "btn_radius": "2px",
        "pill_radius": "2px",
        "top_box_radius": "2px",
        "top_btn_radius": "2px",
        "sub_btn_radius": "2px",
        "nav_btn_radius": "2px",
        "nk_radius": "2px",
        "badge_radius": "2px",
        "small_radius": "2px",
        "scroll_radius": "1px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #4A3516;",
        "card_hover_border_qss": "border: 1px solid #FFB642; border-bottom: 2px solid #FFB642;",
        "input_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #4A3516;",
        "input_focus_border_qss": "border: 1px solid #FFB642; border-bottom: 2px solid #FFB642;",
        "topbar_border_qss": "border-bottom: 1px solid #4A3516;",
        "sidebar_border_qss": "border-right: 1px solid #4A3516;",
        "top_box_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #4A3516;",
        "btn_padding": "6px 14px",
        "btn_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #4A3516;",
        "btn_hover_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;"
        ),
        "btn_pressed_qss": (
            "background-color: #CC8E2E;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;\n"
            "    padding-top: 8px;\n"
            "    padding-left: 16px;\n"
            "    padding-bottom: 4px;\n"
            "    padding-right: 12px;"
        ),
        "primary_text": "#FFB642",
        "primary_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #FFB642;",
        "primary_hover_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;"
        ),
        "primary_pressed_qss": (
            "background-color: #CC8E2E;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;\n"
            "    padding-top: 8px;\n"
            "    padding-left: 16px;\n"
            "    padding-bottom: 4px;\n"
            "    padding-right: 12px;"
        ),
        "top_btn_hover_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #CC8E2E;\n"
            "    color: #120D07;\n"
            "    padding-top: 8px;\n"
            "    padding-left: 18px;\n"
            "    padding-bottom: 2px;\n"
            "    padding-right: 14px;"
        ),
        "top_btn_checked_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;\n"
            "    border-bottom: 2px solid #FFB642;"
        ),
        "pill_border_qss": "border: 1px solid #4A3516; border-bottom: 2px solid #4A3516;",
        "pill_hover_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;"
        ),
        "pill_pressed_qss": (
            "background-color: #CC8E2E;\n"
            "    color: #120D07;\n"
            "    padding-top: 2px;\n"
            "    padding-left: 16px;\n"
            "    padding-right: 12px;"
        ),
        "pill_checked_qss": (
            "background-color: #FFB642;\n"
            "    color: #120D07;\n"
            "    border: 1px solid #FFB642;\n"
            "    border-bottom: 2px solid #FFB642;"
        ),
    },
    "starfield_alabaster": {
        "text": "#14213D",
        "title": "#14213D",
        "subtitle": "#5C677D",
        "muted": "#6E788E",
        "sec_header": "#1B365D",
        "root_bg": "#F4F3EF",
        "sidebar_bg": "#E6E4DD",
        "sidebar_border": "#C59B27",
        "nav_hover": "#D8D5CA",
        "nav_hover_text": "#14213D",
        "nav_checked_bg": "#1B365D",
        "nav_checked_border": "#C59B27",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": "#ECEAE3",
        "card_bg": "#E6E4DD",
        "card_hover_bg": "#EFECE6",
        "card_border": "#C59B27",
        "card_hover_border": "#1B365D",
        "card_body": "#14213D",
        "input_bg": "#FFFFFF",
        "input_focus_bg": "#FFFFFF",
        "input_border": "#C59B27",
        "input_text": "#14213D",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #FFFFFF, stop:1 #EAE8E1)"
        ),
        "btn_hover_bg": "#25477A",
        "btn_pressed_bg": "#1B365D",
        "btn_border": "#C59B27",
        "btn_hover_border": "#E5B83B",
        "btn_text": "#1B365D",
        "pill_bg": (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 #FFFFFF, stop:1 #EAE8E1)"
        ),
        "pill_border": "#C59B27",
        "pill_hover_bg": "#25477A",
        "pill_checked_bg": "#1B365D",
        "pill_checked_text": "#FFFFFF",
        "badge_bg": "rgba(27, 54, 93, 0.12)",
        "badge_border": "#1B365D",
        "badge_text": "#1B365D",
        "badge_muted_bg": "rgba(197, 155, 39, 0.16)",
        "badge_muted_border": "#C59B27",
        "badge_muted_text": "#7A5E10",
        "engine_bg": "rgba(197, 155, 39, 0.18)",
        "engine_border": "#C59B27",
        "engine_text": "#1B365D",
        "shortcut_key_bg": "rgba(200, 62, 43, 0.12)",
        "shortcut_key_border": "#C83E2B",
        "shortcut_key_text": "#C83E2B",
        "scroll_handle": "#C59B27",
        "scroll_handle_hover": "#1B365D",
        "dialog_bg": "#F4F3EF",
        "combo_popup_bg": "#FFFFFF",
        "divider": "#CFCBC0",
        "editor_bg": "#FAF9F6",
        "nk_container_bg": "#F4F3EF",
        "nk_sidebar_bg": "#E6E4DD",
        "nk_card_active_bg": "rgba(27, 54, 93, 0.14)",
        "nk_pill_bg": "#FFFFFF",
        "nk_pill_hover_bg": "#EAE8E1",
        "chevron_color": "#1B365D",
        "focus_border": "#1B365D",
        "selection_bg": "#1B365D",
        "accent": "#1B365D",
        "danger_color": "#C83E2B",
        "primary_grad": "#1B365D",
        "primary_hover_grad": "#25477A",
        "card_radius": "5px",
        "input_radius": "5px",
        "combo_radius": "5px",
        "btn_radius": "5px",
        "pill_radius": "5px",
        "top_box_radius": "5px",
        "top_btn_radius": "5px",
        "sub_btn_radius": "5px",
        "nav_btn_radius": "5px",
        "nk_radius": "5px",
        "badge_radius": "5px",
        "small_radius": "5px",
        "scroll_radius": "2px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #C59B27; border-bottom: 2px solid #9E7B1C;",
        "card_hover_border_qss": "border: 1px solid #1B365D; border-bottom: 2px solid #C59B27;",
        "input_border_qss": "border: 1px solid #C59B27; border-bottom: 2px solid #9E7B1C;",
        "input_focus_border_qss": "border: 1px solid #1B365D; border-bottom: 2px solid #C59B27;",
        "topbar_border_qss": "border-bottom: 2px solid #C59B27;",
        "sidebar_border_qss": "border-right: 1px solid #C59B27;",
        "top_box_border_qss": "border: 1px solid #C59B27; border-bottom: 2px solid #9E7B1C;",
        "btn_padding": "6px 14px",
        "btn_border_qss": "border: 1px solid #C59B27; border-bottom: 2px solid #9E7B1C;",
        "btn_hover_qss": (
            "background-color: #25477A;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 2px solid #E5B83B;\n"
            "    color: #FFFFFF;"
        ),
        "btn_pressed_qss": (
            "background-color: #1B365D;\n"
            "    color: #FFFFFF;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom-width: 1px;\n"
            "    border-bottom-color: #C59B27;\n"
            "    padding-top: 8px;\n"
            "    padding-bottom: 4px;"
        ),
        "primary_text": "#FFFFFF",
        "primary_border_qss": "border: 1px solid #14213D; border-bottom: 3px solid #C59B27;",
        "primary_hover_qss": (
            "background-color: #25477A;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 3px solid #E5B83B;\n"
            "    color: #FFFFFF;"
        ),
        "primary_pressed_qss": (
            "background-color: #14213D;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 1px solid #C59B27;\n"
            "    padding-top: 8px;\n"
            "    padding-bottom: 4px;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_hover_qss": (
            "background-color: #25477A;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 2px solid #E5B83B;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_pressed_qss": (
            "background-color: #14213D;\n"
            "    color: #FFFFFF;\n"
            "    border-bottom-width: 1px;\n"
            "    padding-top: 8px;\n"
            "    padding-bottom: 2px;"
        ),
        "top_btn_checked_qss": (
            "background-color: #1B365D;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 3px solid #C59B27;\n"
            "    color: #FFFFFF;"
        ),
        "pill_border_qss": "border: 1px solid #C59B27; border-bottom: 2px solid #9E7B1C;",
        "pill_hover_qss": (
            "background-color: #25477A;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 2px solid #E5B83B;\n"
            "    color: #FFFFFF;"
        ),
        "pill_pressed_qss": (
            "border-bottom-width: 1px;\n"
            "    padding-top: 3px;"
        ),
        "pill_checked_qss": (
            "background-color: #1B365D;\n"
            "    border: 1px solid #14213D;\n"
            "    border-bottom: 3px solid #C59B27;\n"
            "    color: #FFFFFF;"
        ),
    },
    "cyber_synthwave": {
        "text": "#F0F6FF",
        "title": "#F0F6FF",
        "subtitle": "#B892FF",
        "muted": "#8A63D2",
        "sec_header": "#00F0FF",
        "root_bg": "#0D0814",
        "sidebar_bg": "#160E24",
        "sidebar_border": "#FF007F",
        "nav_hover": "#2A0E3D",
        "nav_hover_text": "#FFFFFF",
        "nav_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #FF007F, stop:1 #7928CA)"
        ),
        "nav_checked_border": "#00F0FF",
        "nav_checked_text": "#FFFFFF",
        "surface_soft": "#120B1E",
        "card_bg": "#160E24",
        "card_hover_bg": "#1F1333",
        "card_border": "#4A1C6D",
        "card_hover_border": "#00F0FF",
        "card_body": "#F0F6FF",
        "input_bg": "#11091C",
        "input_focus_bg": "#1B0E2E",
        "input_border": "#FF007F",
        "input_text": "#F0F6FF",
        "btn_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #2A0E3D, stop:1 #0F2537)"
        ),
        "btn_hover_bg": (
            "qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, "
            "stop:0 #FF3399, stop:1 #1F0A33)"
        ),
        "btn_pressed_bg": "#1F0A33",
        "btn_border": "#FF007F",
        "btn_hover_border": "#00F0FF",
        "btn_text": "#F0F6FF",
        "pill_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #2A0E3D, stop:1 #0F2537)"
        ),
        "pill_border": "#FF007F",
        "pill_hover_bg": (
            "qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, "
            "stop:0 #FF3399, stop:1 #1F0A33)"
        ),
        "pill_checked_bg": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #FF007F, stop:1 #7928CA)"
        ),
        "pill_checked_text": "#FFFFFF",
        "badge_bg": "rgba(255, 0, 127, 0.16)",
        "badge_border": "#FF007F",
        "badge_text": "#FF66B2",
        "badge_muted_bg": "rgba(0, 240, 255, 0.14)",
        "badge_muted_border": "#00F0FF",
        "badge_muted_text": "#00F0FF",
        "engine_bg": "rgba(0, 240, 255, 0.16)",
        "engine_border": "#00F0FF",
        "engine_text": "#00F0FF",
        "shortcut_key_bg": "rgba(255, 0, 127, 0.18)",
        "shortcut_key_border": "#00F0FF",
        "shortcut_key_text": "#00F0FF",
        "scroll_handle": "#FF007F",
        "scroll_handle_hover": "#00F0FF",
        "dialog_bg": "#120A1E",
        "combo_popup_bg": "#120A1E",
        "divider": "#4A1C6D",
        "editor_bg": "#11091C",
        "nk_container_bg": "#120B1E",
        "nk_sidebar_bg": "#160E24",
        "nk_card_active_bg": "rgba(255, 0, 127, 0.24)",
        "nk_pill_bg": "#211235",
        "nk_pill_hover_bg": "#321A52",
        "chevron_color": "#00F0FF",
        "focus_border": "#00F0FF",
        "selection_bg": "#FF007F",
        "accent": "#FF007F",
        "primary_grad": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #FF007F, stop:1 #7928CA)"
        ),
        "primary_hover_grad": (
            "qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, "
            "stop:0 #FF3399, stop:1 #1F0A33)"
        ),
        "card_radius": "8px",
        "input_radius": "8px",
        "combo_radius": "8px",
        "btn_radius": "8px",
        "pill_radius": "8px",
        "top_box_radius": "8px",
        "top_btn_radius": "8px",
        "sub_btn_radius": "8px",
        "nav_btn_radius": "8px",
        "nk_radius": "8px",
        "badge_radius": "8px",
        "small_radius": "8px",
        "scroll_radius": "4px",
        "scroll_width": "6px",
        "card_border_qss": "border: 1px solid #4A1C6D; border-bottom: 2px solid #00F0FF;",
        "card_hover_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "input_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "input_focus_border_qss": "border: 1px solid #00F0FF; border-bottom: 2px solid #FF007F;",
        "topbar_border_qss": "border-bottom: 2px solid #00F0FF;",
        "sidebar_border_qss": "border-right: 1px solid #FF007F;",
        "top_box_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "btn_padding": "6px 14px",
        "btn_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "btn_hover_qss": (
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, stop:0 #FF3399, stop:1 #1F0A33);\n"
            "    border: 1px solid #00F0FF;\n"
            "    border-bottom: 2px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
        "btn_pressed_qss": (
            "background-color: #1F0A33;\n"
            "    border-color: #FF007F;\n"
            "    padding-top: 8px;\n"
            "    padding-bottom: 4px;"
        ),
        "primary_text": "#FFFFFF",
        "primary_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "primary_hover_qss": (
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, stop:0 #FF3399, stop:1 #1F0A33);\n"
            "    border: 1px solid #00F0FF;\n"
            "    border-bottom: 2px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
        "primary_pressed_qss": (
            "background-color: #1F0A33;\n"
            "    border-color: #FF007F;\n"
            "    padding-top: 8px;\n"
            "    padding-bottom: 4px;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_hover_qss": (
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, stop:0 #FF3399, stop:1 #1F0A33);\n"
            "    border: 1px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
        "top_btn_pressed_qss": (
            "padding-top: 8px;\n"
            "    padding-bottom: 2px;\n"
            "    border-color: #FF007F;"
        ),
        "top_btn_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF007F, stop:1 #7928CA);\n"
            "    border: 1px solid #FF007F;\n"
            "    border-bottom: 2px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
        "pill_border_qss": "border: 1px solid #FF007F; border-bottom: 2px solid #00F0FF;",
        "pill_hover_qss": (
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.8, fx:0.5, fy:0.5, stop:0 #FF3399, stop:1 #1F0A33);\n"
            "    border: 1px solid #00F0FF;\n"
            "    border-bottom: 2px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
        "pill_pressed_qss": (
            "padding-top: 3px;\n"
            "    border-color: #FF007F;"
        ),
        "pill_checked_qss": (
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF007F, stop:1 #7928CA);\n"
            "    border: 1px solid #FF007F;\n"
            "    border-bottom: 2px solid #00F0FF;\n"
            "    color: #FFFFFF;"
        ),
    },
}

# Varianti Adattive per la Modalità Lettura (Non distruttiva: conserva l'identità e le forme del tema attivo,
# ma rilassa lo sfondo/testo dell'Editor e attenua del 50% i bordi secondari nella sidebar/card)
_THEME_READING_VARIANTS: Dict[str, Dict[str, str]] = {
    "obsidian": {
        "editor_bg": "#171411",
        "nk_container_bg": "#15120F",
        "nk_sidebar_bg": "#110E0C",
        "text": "#E6D9C6",
        "card_body": "#DFD1BC",
    },
    "light": {
        "editor_bg": "#FBF5E6",
        "nk_container_bg": "#F8F0DE",
        "nk_sidebar_bg": "#F1E7D2",
        "root_bg": "#F6EFE0",
        "text": "#3A2D20",
        "card_body": "#463728",
    },
    "orb": {
        "editor_bg": "#FAF5E8",
        "nk_container_bg": "#F6EFE0",
        "nk_sidebar_bg": "#EFE6D4",
        "text": "#352B20",
        "card_body": "#423629",
    },
    "dark_orb": {
        "editor_bg": "#121C24",
        "nk_container_bg": "#101820",
        "nk_sidebar_bg": "#0D151C",
        "text": "#DCE6EC",
        "card_body": "#D2DEE6",
    },
    "ghibli_spring": {
        "editor_bg": "#FAF3E3",
        "nk_container_bg": "#F7EFE0",
        "nk_sidebar_bg": "#EBEFE0",
        "root_bg": "#F8F2E4",
        "text": "#36332C",
        "card_body": "#3E3A32",
    },
    "pixel_chill": {
        "editor_bg": "#2B302E",
        "nk_container_bg": "#323836",
        "nk_sidebar_bg": "#2C3230",
        "text": "#E6DFCE",
        "card_body": "#DFD7C4",
    },
    "katana": {
        "editor_bg": "#191513",
        "nk_container_bg": "#1C1715",
        "nk_sidebar_bg": "#171311",
        "text": "#E2D8C5",
        "card_body": "#DBD0BC",
    },
    "tactical_hud": {
        "editor_bg": "#0E1412",
        "nk_container_bg": "#111815",
        "nk_sidebar_bg": "#0F1513",
        "text": "#C2E2CF",
        "card_body": "#B8D9C6",
    },
    "industrial_mecha": {
        "editor_bg": "#1E1E1D",
        "nk_container_bg": "#212120",
        "nk_sidebar_bg": "#1A1B1A",
        "text": "#DFDCD4",
        "card_body": "#D6D2C9",
    },
    "amber_crt": {
        "editor_bg": "#14100A",
        "nk_container_bg": "#17120B",
        "nk_sidebar_bg": "#130E08",
        "text": "#DFCAA6",
        "card_body": "#D8C29D",
    },
    "starfield_alabaster": {
        "editor_bg": "#F9F3E5",
        "nk_container_bg": "#F4EDE0",
        "nk_sidebar_bg": "#E9E2D4",
        "text": "#252B3B",
        "card_body": "#303646",
    },
    "cyber_synthwave": {
        "editor_bg": "#140E1D",
        "nk_container_bg": "#171022",
        "nk_sidebar_bg": "#130C1C",
        "text": "#E2DCF0",
        "card_body": "#D8D1E8",
    },
}


def _dim_hex_colors_in_qss(qss_str: str, alpha: float = 0.50) -> str:
    """Attenua del 50% il contrasto dei colori esadecimali nei bordi secondari per la Modalità Lettura."""
    if not qss_str:
        return qss_str

    def _repl(m):
        hex_code = m.group(1)
        if len(hex_code) == 6:
            r = int(hex_code[0:2], 16)
            g = int(hex_code[2:4], 16)
            b = int(hex_code[4:6], 16)
            return f"rgba({r}, {g}, {b}, {alpha:.2f})"
        return m.group(0)

    return re.sub(r"#([0-9A-Fa-f]{6})\b", _repl, qss_str)


# Preset Tipografici Curati (Override Utente + Auto Predefinito del Tema)
FONT_PRESETS: Dict[str, Dict[str, str]] = {
    "auto": {
        "label": "Auto (Tema)",
        "subtitle": "Usa il font nativo del tema (monospace per Pixel/HUD/CRT, sans-serif per gli altri)",
        "family": "",
    },
    "modern_sans": {
        "label": "Modern Sans",
        "subtitle": "Inter / Segoe UI / System Sans (Default UI)",
        "family": "'Inter', 'Segoe UI', sans-serif",
    },
    "geometric": {
        "label": "Geometric & Clean",
        "subtitle": "Plus Jakarta Sans / DM Sans / Aptos",
        "family": "'Plus Jakarta Sans', 'DM Sans', 'Aptos', 'Segoe UI', sans-serif",
    },
    "academic_serif": {
        "label": "Academic / Serif",
        "subtitle": "Georgia / Merriweather / Cambria (Ideale per Studio & Lettura)",
        "family": "'Georgia', 'Merriweather', 'Cambria', serif",
    },
    "dev_mono": {
        "label": "Developer / Mono",
        "subtitle": "JetBrains Mono / Cascadia Code / Consolas (Tech & Arcade)",
        "family": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
    },
    "custom": {
        "label": "Font di Sistema",
        "subtitle": "Qualsiasi font installato su Windows",
        "family": "",
    },
}


def _resolve_font_family(theme: str, font_preset: str = "auto", custom_font: str = "") -> str:
    """Risolve la font-family CSS per QSS e Editor rispettando Auto (Tema), Preset o Font di Sistema."""
    theme_default = _THEME_PALETTES.get(theme, _THEME_PALETTES["obsidian"]).get("font_family", "'Segoe UI'")
    if not font_preset or font_preset == "auto":
        return theme_default
    if font_preset == "custom":
        clean = (custom_font or "").strip().replace("'", "").replace('"', "")
        if clean:
            return f"'{clean}', 'Segoe UI', sans-serif"
        return theme_default
    preset_info = FONT_PRESETS.get(font_preset)
    if preset_info and preset_info.get("family"):
        return preset_info["family"]
    return theme_default


def _resolve_palette(
    theme: str = "obsidian",
    reading_mode: bool = False,
    font_preset: str = "auto",
    custom_font: str = "",
) -> dict:
    base = dict(_THEME_PALETTES.get(theme, _THEME_PALETTES["obsidian"]))
    base.setdefault("focus_border", "#38BDF8")
    base.setdefault("selection_bg", "#0284C7")
    base.setdefault("accent", "#6366F1")
    base.setdefault("primary_text", "#FFFFFF")
    base.setdefault("danger_color", "#E11D48")
    base.setdefault(
        "primary_grad",
        "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284C7, stop:0.55 #6366F1, stop:1 #9333EA)",
    )
    base.setdefault(
        "primary_hover_grad",
        "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0EA5E9, stop:0.55 #818CF8, stop:1 #A855F7)",
    )
    base["font_family"] = _resolve_font_family(theme, font_preset, custom_font)
    base.setdefault("card_radius", "14px")
    base.setdefault("input_radius", "10px")
    base.setdefault("combo_radius", "8px")
    base.setdefault("btn_radius", "9px")
    base.setdefault("pill_radius", "15px")
    base.setdefault("top_box_radius", "10px")
    base.setdefault("top_btn_radius", "7px")
    base.setdefault("sub_btn_radius", "6px")
    base.setdefault("nav_btn_radius", "10px")
    base.setdefault("nk_radius", "12px")
    base.setdefault("badge_radius", "9px")
    base.setdefault("small_radius", "6px")
    base.setdefault("scroll_radius", "2px")
    base.setdefault("scroll_width", "5px")
    base.setdefault("scroll_handle_hover", "#94A3B8")
    base.setdefault("card_border_qss", f"border: 1px solid {base['card_border']};")
    base.setdefault("card_hover_border_qss", f"border: 1px solid {base['card_hover_border']};")
    base.setdefault("input_border_qss", f"border: 1px solid {base['input_border']};")
    base.setdefault("input_focus_border_qss", f"border: 1px solid {base['focus_border']};")
    base.setdefault("topbar_border_qss", f"border-bottom: 1px solid {base['sidebar_border']};")
    base.setdefault("sidebar_border_qss", f"border-right: 1px solid {base['sidebar_border']};")
    base.setdefault("top_box_border_qss", f"border: 1px solid {base['divider']};")
    base.setdefault("btn_padding", "0 14px")
    base.setdefault("btn_border_qss", f"border: 1px solid {base['btn_border']};")
    base.setdefault(
        "btn_hover_qss",
        f"background-color: {base['btn_hover_bg']};\n    border: 1px solid {base['btn_hover_border']};",
    )
    base.setdefault("btn_pressed_qss", f"background-color: {base['btn_pressed_bg']};")
    base.setdefault("primary_border_qss", "border: 1px solid rgba(125, 211, 252, 0.35);")
    base.setdefault(
        "primary_hover_qss",
        f"background-color: {base['primary_hover_grad']};\n    border: 1px solid rgba(125, 211, 252, 0.65);",
    )
    base.setdefault("primary_pressed_qss", f"background-color: {base['btn_pressed_bg']};")
    base.setdefault(
        "top_btn_hover_qss",
        f"color: {base['title']};\n    background-color: {base['nav_hover']};",
    )
    base.setdefault("top_btn_pressed_qss", f"background-color: {base['nav_hover']};")
    base.setdefault(
        "top_btn_checked_qss",
        f"background-color: {base['nav_checked_bg']};\n    color: {base['nav_checked_text']};",
    )
    base.setdefault("pill_border_qss", f"border: 1px solid {base['pill_border']};")
    base.setdefault(
        "pill_hover_qss",
        f"background-color: {base['pill_hover_bg']};\n    color: {base['title']};\n    border: 1px solid {base['btn_hover_border']};",
    )
    base.setdefault("pill_pressed_qss", f"background-color: {base['btn_pressed_bg']};")
    base.setdefault(
        "pill_checked_qss",
        f"background-color: {base['pill_checked_bg']};\n    border: 1px solid {base['nav_checked_border']};\n    color: {base['pill_checked_text']};",
    )

    if reading_mode:
        # Modalità Lettura Adattiva (Non distruttiva): mantiene colori d'accento e forme del tema attivo,
        # rilassa sfondo/testo dell'area di lettura e attenua del 50% i bordi secondari nella sidebar/card.
        variant = _THEME_READING_VARIANTS.get(theme)
        if variant:
            base.update(variant)
        for border_key in (
            "sidebar_border",
            "divider",
            "card_border",
            "input_border",
            "sidebar_border_qss",
            "topbar_border_qss",
            "card_border_qss",
            "input_border_qss",
            "top_box_border_qss",
        ):
            if border_key in base:
                base[border_key] = _dim_hex_colors_in_qss(str(base[border_key]), alpha=0.50)

    return base


def _get_chevron_icon_path(color: str) -> str:
    """Genera e restituisce il path di una chevron SVG pulita per QComboBox::down-arrow."""
    safe = color.lstrip("#")
    path = os.path.join(tempfile.gettempdir(), f"flowlocal_chevron_{safe}.svg")
    if not os.path.exists(path):
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" '
            f'fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
            f'<polyline points="6 9 12 15 18 9"></polyline></svg>'
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        except Exception:
            pass
    return path.replace("\\", "/")


def _make_svg_icon(svg_inner: str, color: str = "#64748B", size: int = 15) -> QIcon:
    """Renderizza un'icona SVG monocromatica nitida in QIcon."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f'{svg_inner}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return QIcon(pm)


def get_hub_qss(
    theme: str = "obsidian",
    reading_mode: bool = False,
    font_preset: str = "auto",
    custom_font: str = "",
    font_scale: int = 100,
) -> str:
    p = _resolve_palette(theme, reading_mode, font_preset=font_preset, custom_font=custom_font)
    chevron_url = _get_chevron_icon_path(p["chevron_color"])
    qss = f"""
QWidget {{
    color: {p['text']};
    font-family: {p['font_family']};
    font-size: 13px;
}}

QMainWindow, QWidget#HubRoot, QStackedWidget#Pages, QWidget#Page {{
    background-color: {p['root_bg']};
}}

/* ---------------- TOP BAR & SEGMENTED NAV ---------------- */
QFrame#TopNavBar {{
    background-color: {p['sidebar_bg']};
    {p['topbar_border_qss']}
}}
QFrame#TopSegmentBox {{
    background-color: {p['surface_soft']};
    {p['top_box_border_qss']}
    border-radius: {p['top_box_radius']};
}}
QFrame#TopSlidingPill {{
    {p['top_btn_checked_qss']}
    border-radius: {p['top_btn_radius']};
}}
QFrame#SubSlidingPill {{
    {p['pill_checked_qss']}
    border-radius: {p['sub_btn_radius']};
}}
QPushButton#TopSegmentBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {p['top_btn_radius']};
    padding: 5px 16px;
    color: {p['subtitle']};
    font-size: 13px;
    font-weight: 600;
    min-height: 28px;
}}
QPushButton#TopSegmentBtn:hover {{
    {p['top_btn_hover_qss']}
}}
QPushButton#TopSegmentBtn:pressed {{
    {p['top_btn_pressed_qss']}
}}
QPushButton#TopSegmentBtn:checked {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['nav_checked_text']};
}}
QFrame#SubNavBar {{
    background-color: transparent;
    border-bottom: 1px solid {p['divider']};
}}
QPushButton#SubNavBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {p['sub_btn_radius']};
    padding: 4px 12px;
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 500;
    min-height: 26px;
}}
QPushButton#SubNavBtn:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#SubNavBtn:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#SubNavBtn:checked {{
    {p['pill_checked_qss']}
    font-weight: 600;
}}
QPushButton#SubNavBtn[has_sliding_pill="true"]:checked {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['pill_checked_text']};
    font-weight: 600;
}}

/* ---------------- SIDEBAR ---------------- */
QFrame#Sidebar {{
    background-color: {p['sidebar_bg']};
    {p['sidebar_border_qss']}
}}
QLabel#AppTitle {{
    font-size: 18px;
    font-weight: 700;
    color: {p['title']};
    background-color: transparent;
}}
QLabel#EngineStatus {{
    background-color: {p['engine_bg']};
    border: 1px solid {p['engine_border']};
    border-radius: {p['badge_radius']};
    padding: 4px 10px;
    color: {p['engine_text']};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#NavSectionHeader {{
    color: {p['sec_header']};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.2px;
    padding: 12px 10px 4px 10px;
    background-color: transparent;
}}
QPushButton#NavButton {{
    min-height: 36px;
    max-height: 36px;
    text-align: left;
    padding: 0 12px;
    border: 1px solid transparent;
    border-radius: {p['nav_btn_radius']};
    background-color: transparent;
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#NavButton:hover {{
    background-color: {p['nav_hover']};
    color: {p['nav_hover_text']};
}}
QPushButton#NavButton:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#NavButton:checked {{
    background-color: {p['nav_checked_bg']};
    border: 1px solid {p['nav_checked_border']};
    color: {p['nav_checked_text']};
}}
QFrame#ShortcutBox {{
    background-color: {p['surface_soft']};
    {p['card_border_qss']}
    border-radius: {p['card_radius']};
}}
QLabel#ShortcutKey {{
    background-color: {p['shortcut_key_bg']};
    border: 1px solid {p['shortcut_key_border']};
    border-radius: {p['small_radius']};
    padding: 2px 8px;
    color: {p['shortcut_key_text']};
    font-weight: 700;
}}
QLabel#ShortcutHint {{
    color: {p['subtitle']};
    font-size: 11.5px;
    background-color: transparent;
}}

/* ---------------- HEADER PAGINE ---------------- */
QLabel#PageTitle {{
    font-size: 21px;
    font-weight: 700;
    color: {p['title']};
    background-color: transparent;
}}
QLabel#PageSubtitle {{
    font-size: 12px;
    color: {p['subtitle']};
    background-color: transparent;
}}

/* ---------------- INPUT & COMBO ---------------- */
QLineEdit {{
    min-height: 36px;
    background-color: {p['input_bg']};
    {p['input_border_qss']}
    border-radius: {p['input_radius']};
    padding: 0 12px;
    color: {p['input_text']};
    font-size: 13px;
    selection-background-color: {p['selection_bg']};
}}
QLineEdit:focus {{
    {p['input_focus_border_qss']}
    background-color: {p['input_focus_bg']};
}}
QTextEdit {{
    min-height: 120px;
    background-color: {p['input_bg']};
    {p['input_border_qss']}
    border-radius: {p['input_radius']};
    padding: 10px 12px;
    color: {p['input_text']};
    font-size: 13px;
    selection-background-color: {p['selection_bg']};
}}
QTextEdit:focus {{
    {p['input_focus_border_qss']}
    background-color: {p['input_focus_bg']};
}}
QComboBox {{
    min-height: 28px;
    background-color: {p['input_bg']};
    {p['input_border_qss']}
    border-radius: {p['combo_radius']};
    padding: 0 26px 0 10px;
    color: {p['input_text']};
    font-size: 12px;
    font-weight: 500;
}}
QComboBox:focus {{
    {p['input_focus_border_qss']}
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: url({chevron_url});
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background-color: {p['combo_popup_bg']};
    color: {p['input_text']};
    selection-background-color: {p['selection_bg']};
    selection-color: #FFFFFF;
    {p['input_border_qss']}
}}

/* ---------------- PULSANTI (Tattili & Multi-Tema) ---------------- */
QPushButton {{
    min-height: 34px;
    border-radius: {p['btn_radius']};
    padding: {p['btn_padding']};
    font-weight: 600;
    background-color: {p['btn_bg']};
    color: {p['btn_text']};
    {p['btn_border_qss']}
}}
QPushButton:hover {{
    {p['btn_hover_qss']}
}}
QPushButton:pressed {{
    {p['btn_pressed_qss']}
}}
QPushButton[variant="primary"] {{
    background-color: {p['primary_grad']};
    {p['primary_border_qss']}
    color: {p['primary_text']};
}}
QPushButton[variant="primary"]:hover {{
    {p['primary_hover_qss']}
}}
QPushButton[variant="primary"]:pressed {{
    {p['primary_pressed_qss']}
}}
QPushButton[variant="success"] {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #059669, stop:1 #10B981
    );
    border: 1px solid rgba(52, 211, 153, 0.45);
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[variant="success"]:hover {{
    background-color: #10B981;
    border: 1px solid #34D399;
}}
QPushButton[variant="success"]:disabled {{
    background-color: {p['btn_bg']};
    {p['btn_border_qss']}
    color: {p['muted']};
}}
QPushButton[variant="danger"] {{
    background-color: transparent;
    border: 1px solid rgba(244, 63, 94, 0.32);
    color: {p['danger_color']};
}}
QPushButton[variant="danger"]:hover {{
    background-color: rgba(244, 63, 94, 0.12);
    border: 1px solid rgba(244, 63, 94, 0.55);
}}
QPushButton#ModeTab {{
    background-color: {p['pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['btn_radius']};
    color: {p['subtitle']};
    padding: 0 14px;
}}
QPushButton#ModeTab:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#ModeTab:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#ModeTab:checked {{
    {p['pill_checked_qss']}
}}
QPushButton#FilterPill {{
    background-color: {p['pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['pill_radius']};
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 600;
    padding: 0 14px;
    min-height: 28px;
    max-height: 28px;
}}
QPushButton#FilterPill:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#FilterPill:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#FilterPill:checked {{
    {p['pill_checked_qss']}
}}

/* ---------------- LISTE SCORREVOLI & QLISTWIDGET ---------------- */
QScrollArea {{
    background-color: transparent;
    border: none;
}}
QWidget#ScrollContent {{
    background-color: transparent;
}}
QListWidget {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['card_radius']};
    color: {p['text']};
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-radius: {p['small_radius']};
    margin: 2px 0;
}}
QListWidget::item:hover {{
    background-color: {p['nav_hover']};
}}
QListWidget::item:selected {{
    background-color: {p['nk_card_active_bg']};
    color: {p['title']};
}}
QScrollBar:vertical {{
    background-color: transparent;
    width: {p['scroll_width']};
    margin: 2px 1px;
}}
QScrollBar::handle:vertical {{
    background-color: {p['scroll_handle']};
    border-radius: {p['scroll_radius']};
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {p['scroll_handle_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background-color: transparent;
}}
QScrollBar:horizontal {{
    background-color: transparent;
    height: {p['scroll_width']};
    margin: 1px 2px;
}}
QScrollBar::handle:horizontal {{
    background-color: {p['scroll_handle']};
    border-radius: {p['scroll_radius']};
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {p['scroll_handle_hover']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background-color: transparent;
}}

/* ---------------- CARD ---------------- */
QFrame#Card {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['card_radius']};
}}
QFrame#Card:hover {{
    background-color: {p['card_hover_bg']};
    {p['card_hover_border_qss']}
}}
QFrame#Card QLabel {{
    background-color: transparent;
}}
QLabel#CardMeta {{
    color: {p['muted']};
    font-size: 11px;
    background-color: transparent;
}}
QLabel#CardTitle {{
    color: {p['title']};
    font-weight: 600;
    background-color: transparent;
}}
QLabel#CardBody {{
    color: {p['card_body']};
    font-size: 13px;
    background-color: transparent;
}}
QLabel#Badge {{
    background-color: {p['badge_bg']};
    border: 1px solid {p['badge_border']};
    border-radius: {p['badge_radius']};
    padding: 2px 9px;
    color: {p['badge_text']};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#BadgeMuted {{
    background-color: {p['badge_muted_bg']};
    border: 1px solid {p['badge_muted_border']};
    border-radius: {p['badge_radius']};
    padding: 2px 9px;
    color: {p['badge_muted_text']};
    font-size: 11px;
}}
QLabel#BadgeQuickNote {{
    background-color: {p['engine_bg']};
    border: 1px solid {p['engine_border']};
    border-radius: {p['badge_radius']};
    padding: 2px 9px;
    color: {p['engine_text']};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#EmptyState {{
    color: {p['muted']};
    font-size: 13px;
    padding: 30px;
}}

QDialog, QMessageBox {{
    background-color: {p['dialog_bg']};
}}

/* ---------------- GRAFO & QUADERNO ---------------- */
QFrame#KnowledgeGraphFrame {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['card_radius']};
}}
QFrame#KnowledgeGraphToolbar {{
    background-color: {p['surface_soft']};
    border-bottom: 1px solid {p['card_border']};
    border-top-left-radius: {p['card_radius']};
    border-top-right-radius: {p['card_radius']};
}}
QGraphicsView#KnowledgeGraphView {{
    background-color: transparent;
    border: none;
}}
QSplitter::handle {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 transparent, stop:0.42 transparent,
        stop:0.45 {p['divider']}, stop:0.55 {p['divider']},
        stop:0.58 transparent, stop:1 transparent
    );
    width: 6px;
}}
QSplitter::handle:hover {{
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 transparent, stop:0.32 transparent,
        stop:0.35 {p['accent']}, stop:0.65 {p['accent']},
        stop:0.68 transparent, stop:1 transparent
    );
}}
QPushButton#NKCollapseBtn {{
    min-height: 26px;
    max-height: 26px;
    min-width: 26px;
    max-width: 26px;
    padding: 0px;
    {p['pill_border_qss']}
    border-radius: {p['small_radius']};
    background-color: {p['nk_pill_bg']};
    color: {p['subtitle']};
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#NKCollapseBtn:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#NKCollapseBtn:pressed {{
    {p['pill_pressed_qss']}
    padding: 0px;
    padding-top: 2px;
}}
QPushButton#NKRestorePill {{
    min-height: 26px;
    max-height: 26px;
    padding: 0 10px;
    {p['pill_border_qss']}
    border-radius: {p['pill_radius']};
    background-color: {p['nk_pill_bg']};
    color: {p['subtitle']};
    font-size: 11.5px;
    font-weight: 600;
}}
QPushButton#NKRestorePill:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#NKRestorePill:pressed {{
    {p['pill_pressed_qss']}
    padding: 2px 10px 0 10px;
}}
QPushButton#NKRestorePill:checked {{
    {p['pill_checked_qss']}
}}
QFrame#NotekeeperContainer {{
    background-color: {p['nk_container_bg']};
    {p['card_border_qss']}
    border-radius: {p['nk_radius']};
}}
QWidget#NKCol1 {{
    background-color: {p['nk_sidebar_bg']};
    {p['sidebar_border_qss']}
    border-top-left-radius: {p['nk_radius']};
    border-bottom-left-radius: {p['nk_radius']};
}}
QWidget#NKCol2 {{
    background-color: transparent;
}}
QWidget#NKCol3 {{
    background-color: {p['editor_bg']};
    border-top-right-radius: {p['nk_radius']};
    border-bottom-right-radius: {p['nk_radius']};
}}
QTreeWidget {{
    background-color: transparent;
    border: none;
    color: {p['text']};
    font-size: 12px;
    padding: 2px 0;
    outline: none;
}}
QTreeWidget::item {{
    padding: 5px 6px;
    border-radius: {p['small_radius']};
    margin: 1px 0;
}}
QTreeWidget::item:hover {{
    background-color: {p['nav_hover']};
}}
QTreeWidget::item:selected {{
    background-color: {p['nk_card_active_bg']};
    color: {p['title']};
}}
QLabel#CloudBadge {{
    background-color: {p['surface_soft']};
    color: {p['muted']};
    {p['pill_border_qss']}
    border-radius: {p['small_radius']};
    padding: 0 12px;
    min-height: 32px;
    max-height: 32px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton#NKHeaderBtn {{
    min-height: 32px;
    max-height: 32px;
    border-radius: {p['btn_radius']};
    padding: 0 12px;
    font-size: 12px;
}}
QLabel#CloudBadge[pending="true"] {{
    background-color: {p['badge_bg']};
    color: {p['badge_text']};
    border: 1px solid {p['badge_border']};
    font-weight: 700;
}}
QFrame#NKNoteCard {{
    background-color: transparent;
    border: 1px solid transparent;
    border-bottom: 1px solid {p['divider']};
    border-radius: {p['small_radius']};
    padding: 4px 6px;
}}
QFrame#NKNoteCard:hover {{
    background-color: {p['nav_hover']};
}}
QFrame#NKNoteCard[selected="true"] {{
    background-color: {p['nk_card_active_bg']};
    border: 1px solid transparent;
    border-left: 3px solid {p['accent']};
}}
QLabel#NKNoteTitle {{
    color: {p['title']};
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
QLabel#NKNotePreview {{
    color: {p['subtitle']};
    font-size: 11.5px;
    background: transparent;
}}
QLabel#NKNoteMeta {{
    color: {p['muted']};
    font-size: 11px;
    background: transparent;
}}
QLineEdit#NKEditorTitle {{
    background-color: transparent;
    border: none;
    padding: 2px 0px;
    color: {p['title']};
    font-size: 30px;
    font-weight: 700;
    min-height: 42px;
}}
QLineEdit#NKEditorTitle:focus {{
    background-color: transparent;
    border: none;
}}
QLineEdit#NKEditorPill {{
    background-color: {p['nk_pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['small_radius']};
    padding: 0 8px;
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 500;
    min-height: 26px;
    max-height: 26px;
}}
QLineEdit#NKEditorPill:hover, QLineEdit#NKEditorPill:focus {{
    background-color: {p['nk_pill_hover_bg']};
    {p['input_focus_border_qss']}
    color: {p['title']};
}}
QComboBox#NKEditorPillCombo {{
    background-color: {p['nk_pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['small_radius']};
    padding: 0 22px 0 8px;
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 500;
    min-height: 26px;
    max-height: 26px;
}}
QComboBox#NKEditorPillCombo:hover, QComboBox#NKEditorPillCombo:focus {{
    background-color: {p['nk_pill_hover_bg']};
    {p['input_focus_border_qss']}
    color: {p['title']};
}}
QFrame#NKEditorDivider {{
    background-color: {p['divider']};
    border: none;
    max-height: 1px;
    margin-bottom: 8px;
}}
QTextEdit#NKEditorBody {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 4px 0px;
    color: {p['text']};
    font-size: 13.5px;
}}
QTextEdit#NKEditorBody:focus {{
    background-color: transparent;
    border: none;
}}
QPushButton#NKActionBtn {{
    min-height: 30px;
    max-height: 30px;
    border-radius: {p['btn_radius']};
    padding: 0 11px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton#NKActionBtn:pressed {{
    padding-top: 2px;
    padding-left: 12px;
    padding-right: 10px;
}}
QPushButton#NKCol1Btn {{
    min-height: 34px;
    max-height: 34px;
    border-radius: {p['btn_radius']};
    padding: 0 10px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#NKCol1Btn:pressed {{
    padding-top: 2px;
    padding-left: 11px;
    padding-right: 9px;
}}

/* ---------------- SPOTIFY MINI BAR & SETTINGS GEAR ---------------- */
QFrame#SpotifyMiniBar {{
    background-color: {p['surface_soft']};
    {p['top_box_border_qss']}
    border-radius: {p['pill_radius']};
}}
QPushButton#SpotifyTrackBtn {{
    background-color: transparent;
    border: none;
    border-radius: {p['small_radius']};
    padding: 0 8px;
    color: {p['text']};
    font-size: 11.5px;
    font-weight: 600;
    min-height: 24px;
    max-height: 24px;
    text-align: left;
}}
QPushButton#SpotifyTrackBtn:hover {{
    background-color: {p['nav_hover']};
    color: {p['title']};
}}
QPushButton#SpotifyTrackBtn:pressed {{
    padding: 2px 8px 0 8px;
}}
QPushButton#SpotifyCtrlBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {p['small_radius']};
    padding: 0px;
    color: {p['subtitle']};
    font-size: 11px;
    font-weight: 600;
    min-height: 24px;
    max-height: 24px;
    min-width: 24px;
    max-width: 24px;
}}
QPushButton#SpotifyCtrlBtn:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#SpotifyCtrlBtn:pressed {{
    {p['pill_pressed_qss']}
    padding: 0px;
    padding-top: 2px;
}}

/* ---------------- AUDIO SYNC BAR (FLOWNOTES SBOBINE) ---------------- */
QFrame#AudioSyncBar {{
    background-color: {p['surface_soft']};
    {p['top_box_border_qss']}
    border-radius: {p['btn_radius']};
    margin-top: 8px;
}}
QPushButton#AudioSyncPlayBtn {{
    background-color: {p.get('accent') or p.get('nav_checked_border') or '#6366F1'};
    border: none;
    border-radius: 15px;
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 700;
    padding: 0px;
}}
QPushButton#AudioSyncPlayBtn:hover {{
    background-color: {p['nav_checked_border']};
}}
QPushButton#AudioSyncJumpBtn, QPushButton#AudioSyncSpeedBtn {{
    background-color: {p['pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['small_radius']};
    color: {p['text']};
    font-size: 11px;
    font-weight: 600;
    padding: 0 8px;
    min-height: 26px;
    max-height: 26px;
}}
QPushButton#AudioSyncJumpBtn:hover, QPushButton#AudioSyncSpeedBtn:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#AudioSyncJumpBtn:pressed, QPushButton#AudioSyncSpeedBtn:pressed {{
    {p['pill_pressed_qss']}
}}
QLabel#AudioSyncTimeLabel {{
    background-color: transparent;
    border: none;
    color: {p['subtitle']};
    font-size: 11px;
    font-weight: 600;
}}
QSlider#AudioSyncSlider {{
    background-color: transparent;
}}
QSlider#AudioSyncSlider::groove:horizontal {{
    border: none;
    height: 4px;
    background-color: {p['divider']};
    border-radius: 2px;
}}
QSlider#AudioSyncSlider::sub-page:horizontal {{
    background-color: {p.get('accent') or p.get('nav_checked_border') or '#6366F1'};
    border-radius: 2px;
}}
QSlider#AudioSyncSlider::handle:horizontal {{
    background-color: {p.get('accent') or p.get('nav_checked_border') or '#6366F1'};
    border: 2px solid {p['surface_soft']};
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 7px;
}}
QPushButton#SettingsGearBtn {{
    background-color: {p['pill_bg']};
    {p['pill_border_qss']}
    border-radius: {p['pill_radius']};
    color: {p['subtitle']};
    font-size: 14px;
    padding: 0px;
    min-height: 28px;
    max-height: 28px;
    min-width: 30px;
    max-width: 30px;
}}
QPushButton#SettingsGearBtn:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#SettingsGearBtn:pressed {{
    padding: 0px;
    padding-top: 2px;
}}

/* ---------------- FLOWTIME CALENDARIO & COLLEGAMENTI ---------------- */
QCalendarWidget {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['nk_radius']};
}}
QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background-color: {p['surface_soft']};
    border-bottom: 1px solid {p['divider']};
    border-top-left-radius: {p['nk_radius']};
    border-top-right-radius: {p['nk_radius']};
    padding: 4px;
}}
QCalendarWidget QToolButton {{
    color: {p['title']};
    background-color: transparent;
    border: none;
    border-radius: {p['small_radius']};
    padding: 4px 8px;
    font-size: 12px;
    font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{
    background-color: {p['nav_hover']};
}}
QCalendarWidget QMenu {{
    background-color: {p['combo_popup_bg']};
    color: {p['text']};
    {p['input_border_qss']}
}}
QCalendarWidget QSpinBox {{
    background-color: {p['input_bg']};
    color: {p['input_text']};
    {p['input_border_qss']}
    border-radius: {p['small_radius']};
    padding: 2px 4px;
}}
QCalendarWidget QAbstractItemView:enabled {{
    background-color: {p['editor_bg']};
    color: {p['text']};
    selection-background-color: {p['selection_bg']};
    selection-color: #FFFFFF;
    outline: none;
    border-bottom-left-radius: {p['nk_radius']};
    border-bottom-right-radius: {p['nk_radius']};
}}
QCalendarWidget QAbstractItemView:disabled {{
    color: {p['muted']};
}}
QFrame#FTEventCard {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['nk_radius']};
}}
QFrame#FTEventCard:hover {{
    background-color: {p['card_hover_bg']};
    {p['card_hover_border_qss']}
}}
QLabel#FTTimePill {{
    background-color: {p['badge_bg']};
    border: 1px solid {p['badge_border']};
    border-radius: {p['small_radius']};
    padding: 2px 8px;
    color: {p['badge_text']};
    font-size: 11px;
    font-weight: 700;
}}
QPushButton#FTNotePillBtn {{
    background-color: {p['engine_bg']};
    border: 1px solid {p['engine_border']};
    border-radius: {p['small_radius']};
    padding: 2px 9px;
    color: {p['engine_text']};
    font-size: 11px;
    font-weight: 600;
    min-height: 24px;
    max-height: 24px;
}}
QPushButton#FTNotePillBtn:hover {{
    border: 1px solid {p['accent']};
    color: {p['title']};
}}
QFrame#FTTimelineCard {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['nk_radius']};
}}
QFrame#FTWeekDayCol {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['nk_radius']};
}}
QFrame#FTWeekDayColToday {{
    background-color: {p['card_bg']};
    border: 1px solid rgba(239, 68, 68, 0.55);
    border-radius: {p['nk_radius']};
}}
QFrame#FTWeekDayColSelected {{
    background-color: {p['card_hover_bg']};
    border: 1.5px solid {p['accent']};
    border-radius: {p['nk_radius']};
}}
QFrame#FTWeekEventCard {{
    background-color: {p['surface_soft']};
    {p['card_border_qss']}
    border-radius: {p['small_radius']};
}}
QFrame#FTWeekEventCard:hover {{
    background-color: {p['card_hover_bg']};
    border: 1px solid {p['accent']};
}}
QPushButton#FTWeekDayHeaderBtn {{
    background-color: transparent;
    border: none;
    border-radius: {p['small_radius']};
    padding: 4px 6px;
    color: {p['title']};
    font-size: 12px;
    font-weight: 700;
    text-align: left;
}}
QPushButton#FTWeekDayHeaderBtn:hover {{
    background-color: {p['nav_hover']};
}}
QFrame#FTTodoStrip {{
    background-color: {p['surface_soft']};
    {p['card_border_qss']}
    border-radius: {p['input_radius']};
}}
QFrame#FTTodoPill {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['small_radius']};
}}
QFrame#FTTodoPill:hover {{
    border: 1px solid {p['accent']};
    background-color: {p['card_hover_bg']};
}}
QFrame#FTWeekHeaderBar {{
    background-color: transparent;
    border-bottom: 1px solid {p['divider']};
}}
QFrame#FTWeekHeaderCell {{
    background-color: transparent;
    border-left: 1px solid {p['divider']};
}}
QFrame#FTDayGroupHeader {{
    background-color: {p['surface_soft']};
    {p['card_border_qss']}
    border-radius: {p['small_radius']};
}}
QFrame#FTDayGroupHeaderSelected {{
    background-color: {p['nav_checked_bg']};
    border: 1px solid {p['nav_checked_border']};
    border-radius: {p['small_radius']};
}}
QFrame#FTSearchResultCard {{
    background-color: {p['card_bg']};
    {p['card_border_qss']}
    border-radius: {p['input_radius']};
}}
QFrame#FTSearchResultCard:hover {{
    background-color: {p['card_hover_bg']};
    {p['card_hover_border_qss']}
}}
QLabel#FTWeekAxisHeader {{
    color: {p['muted']};
    font-size: 10.5px;
    font-weight: 700;
    background: transparent;
}}
QPushButton#FTColorDot {{
    border-radius: 10px;
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    padding: 0px;
}}
QPushButton#FTColorDot:pressed {{
    padding: 0px;
}}
QPushButton#FTSegmentBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {p['small_radius']};
    padding: 4px 10px;
    color: {p['subtitle']};
    font-size: 11.5px;
    font-weight: 600;
    min-height: 26px;
}}
QPushButton#FTSegmentBtn:hover {{
    background-color: {p['nav_hover']};
    color: {p['title']};
}}
QPushButton#FTSegmentBtn:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#FTSegmentBtn:checked {{
    background-color: {p['nav_checked_bg']};
    border: 1px solid {p['nav_checked_border']};
    color: {p['nav_checked_text']};
    font-weight: 700;
}}
QCheckBox {{
    color: {p['text']};
    font-size: 12px;
    spacing: 6px;
}}
"""
    scale_pct = max(80, min(140, int(font_scale or 100)))
    if scale_pct != 100:
        factor = scale_pct / 100.0

        def _scale_font_match(m):
            val = float(m.group(1))
            scaled = round(val * factor, 1)
            if scaled.is_integer():
                return f"font-size: {int(scaled)}px;"
            return f"font-size: {scaled}px;"

        qss = re.sub(r"font-size:\s*(\d+(?:\.\d+)?)px;", _scale_font_match, qss)
    return qss


HUB_QSS = get_hub_qss("obsidian")

# Modalità di dettatura (etichetta UI -> nome stile nel DB llm_styles)
MODE_TABS = [
    ("Naturale", "natural"),
    ("Formale", "formal"),
    ("Code / Tech", "coding"),
    ("Studio / LaTeX", "study_latex"),
    ("Chat", "chat"),
]


def _format_timestamp(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y · %H:%M")
    except ValueError:
        return str(raw)


_MORPHOLOGICAL_THEMES = {
    "pixel_chill",
    "ghibli_spring",
    "katana",
    "tactical_hud",
    "industrial_mecha",
}


def _build_pixel_notched_path(rect: QRectF, step: float = 3.0) -> QPainterPath:
    """Costruisce una sagoma Pixel Art 8-bit con angoli scavati a gradini (2 step per angolo)."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    s = max(2.0, min(step, min(w, h) * 0.14))
    s2 = s * 2.0
    path = QPainterPath()
    # Partenza in alto a sinistra dopo i 2 gradini
    path.moveTo(x + s2, y)
    path.lineTo(x + w - s2, y)
    # Angolo alto-destra a gradini
    path.lineTo(x + w - s2, y + s)
    path.lineTo(x + w - s, y + s)
    path.lineTo(x + w - s, y + s2)
    path.lineTo(x + w, y + s2)
    # Lato destro -> angolo basso-destra a gradini
    path.lineTo(x + w, y + h - s2)
    path.lineTo(x + w - s, y + h - s2)
    path.lineTo(x + w - s, y + h - s)
    path.lineTo(x + w - s2, y + h - s)
    path.lineTo(x + w - s2, y + h)
    # Lato inferiore -> angolo basso-sinistra a gradini
    path.lineTo(x + s2, y + h)
    path.lineTo(x + s2, y + h - s)
    path.lineTo(x + s, y + h - s)
    path.lineTo(x + s, y + h - s2)
    path.lineTo(x, y + h - s2)
    # Lato sinistro -> angolo alto-sinistra a gradini
    path.lineTo(x, y + s2)
    path.lineTo(x + s, y + s2)
    path.lineTo(x + s, y + s)
    path.lineTo(x + s2, y + s)
    path.closeSubpath()
    return path


def _build_chamfered_45_path(rect: QRectF, cut: float = 7.5) -> QPainterPath:
    """Costruisce una sagoma poligonale con angoli opposti (alto-destra e basso-sinistra) tagliati a 45°."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    c = max(4.0, min(cut, min(w, h) * 0.34))
    r_small = 2.0
    path = QPainterPath()
    # Angolo alto-sinistra leggermente solido -> taglio 45° alto-destra
    path.moveTo(x + r_small, y)
    path.lineTo(x + w - c, y)
    path.lineTo(x + w, y + c)
    # Angolo basso-destra -> taglio 45° basso-sinistra
    path.lineTo(x + w, y + h - r_small)
    path.lineTo(x + w - r_small, y + h)
    path.lineTo(x + c, y + h)
    path.lineTo(x, y + h - c)
    path.lineTo(x, y + r_small)
    path.closeSubpath()
    return path


def _draw_sakura_flower(painter: QPainter, cx: float, cy: float, radius: float, petal_color: QColor, center_color: QColor):
    """Disegna un delicato fiorellino di sakura a 5 petali vettoriali con intaglio apicale."""
    painter.save()
    painter.translate(cx, cy)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(petal_color)
    for i in range(5):
        painter.save()
        painter.rotate(i * 72.0 - 18.0)
        petal = QPainterPath()
        petal.moveTo(0.0, 0.0)
        petal.cubicTo(-radius * 0.55, -radius * 0.45, -radius * 0.48, -radius * 0.95, -radius * 0.14, -radius * 1.0)
        petal.lineTo(0.0, -radius * 0.80)  # piccolo intaglio tipico del petalo di ciliegio
        petal.lineTo(radius * 0.14, -radius * 1.0)
        petal.cubicTo(radius * 0.48, -radius * 0.95, radius * 0.55, -radius * 0.45, 0.0, 0.0)
        painter.drawPath(petal)
        painter.restore()
    painter.setBrush(center_color)
    painter.drawEllipse(QPointF(0.0, 0.0), max(1.2, radius * 0.24), max(1.2, radius * 0.24))
    painter.restore()


def _draw_sakura_petal(painter: QPainter, cx: float, cy: float, size: float, angle_deg: float, color: QColor):
    """Disegna un singolo petalo di sakura fluttuante."""
    painter.save()
    painter.translate(cx, cy)
    painter.rotate(angle_deg)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    p = QPainterPath()
    p.moveTo(0.0, size * 0.6)
    p.cubicTo(-size * 0.55, size * 0.1, -size * 0.45, -size * 0.55, -size * 0.12, -size * 0.65)
    p.lineTo(0.0, -size * 0.45)
    p.lineTo(size * 0.12, -size * 0.65)
    p.cubicTo(size * 0.45, -size * 0.55, size * 0.55, size * 0.1, 0.0, size * 0.6)
    painter.drawPath(p)
    painter.restore()


def _paint_morphological_surface(
    painter: QPainter,
    rect: QRectF,
    theme_key: str,
    *,
    is_primary: bool = False,
    is_danger: bool = False,
    is_success: bool = False,
    is_checked: bool = False,
    is_hovered: bool = False,
    is_down: bool = False,
    is_pill: bool = False,
    reading_mode: bool = False,
) -> QColor:
    """
    Disegna la sagoma morfologica reale del bottone/pillola per i temi speciali
    e restituisce il QColor consigliato per il testo/icona in primo piano.
    """
    w, h = rect.width(), rect.height()
    if w <= 4 or h <= 4:
        return QColor("#FFFFFF")

    active_like = is_primary or is_checked or is_pill

    # =========================================================================
    # 1. PIXEL CHILL: Sagoma Pixel Art 8-bit a gradini + bordo interno + ombra
    # =========================================================================
    if theme_key == "pixel_chill":
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        step = 2.5 if h < 30 else 3.0
        # Ombra a scalini in basso a destra (solo se non premuto)
        if not is_down:
            shadow_rect = QRectF(rect.x() + 2.0, rect.y() + 2.0, w - 2.0, h - 2.0)
            shadow_path = _build_pixel_notched_path(shadow_rect, step)
            painter.fillPath(shadow_path, QColor("#181D1F"))

        body_rect = QRectF(
            rect.x() + (1.0 if is_down else 0.0),
            rect.y() + (1.0 if is_down else 0.0),
            w - 2.0,
            h - 2.0,
        )
        outer_path = _build_pixel_notched_path(body_rect, step)

        if is_danger:
            bg_col = QColor("#8F2D2D") if not is_hovered else QColor("#A83838")
            inner_bdr = QColor("#F07B65")
            txt_col = QColor("#FFFDF9")
        elif is_success:
            bg_col = QColor("#3E6B48") if not is_hovered else QColor("#4B7F57")
            inner_bdr = QColor("#A3D9A5")
            txt_col = QColor("#FFFDF9")
        elif active_like:
            bg_col = QColor("#E06D53") if not is_hovered else QColor("#F07B65")
            inner_bdr = QColor("#FFE3D8")
            txt_col = QColor("#1E2426") if is_pill else QColor("#FFFDF9")
            if is_pill:
                bg_col = QColor("#F07B65")
                inner_bdr = QColor("#1E2426")
        elif is_hovered:
            bg_col = QColor("#4A565B")
            inner_bdr = QColor("#F07B65")
            txt_col = QColor("#FFFDF9")
        else:
            bg_col = QColor("#394246") if not reading_mode else QColor("#333B3E")
            inner_bdr = QColor("#627075") if not reading_mode else QColor("#505C60")
            txt_col = QColor("#EAE4DC")

        # Bordo esterno nero-ardesia 8-bit
        painter.fillPath(outer_path, QColor("#1E2426"))

        # Corpo interno scavato di 2px
        inner_rect = body_rect.adjusted(2.0, 2.0, -2.0, -2.0)
        inner_path = _build_pixel_notched_path(inner_rect, max(2.0, step - 0.5))
        painter.fillPath(inner_path, inner_bdr)

        # Area centrale riempimento pixel
        core_rect = body_rect.adjusted(4.0, 4.0, -4.0, -4.0)
        if core_rect.width() > 4 and core_rect.height() > 4:
            core_path = _build_pixel_notched_path(core_rect, max(1.5, step - 1.0))
            painter.fillPath(core_path, bg_col)

        # Rivetti/pixel d'angolo 8-bit a contrasto
        stud_col = QColor("#FFFDF9") if active_like else QColor("#8CA87C")
        stud_col.setAlpha(180 if active_like or is_hovered else 110)
        painter.fillRect(QRectF(body_rect.left() + step * 2 + 2, body_rect.top() + 3, 2, 2), stud_col)
        painter.fillRect(QRectF(body_rect.right() - step * 2 - 4, body_rect.top() + 3, 2, 2), stud_col)
        painter.fillRect(QRectF(body_rect.left() + step * 2 + 2, body_rect.bottom() - 5, 2, 2), stud_col)
        painter.fillRect(QRectF(body_rect.right() - step * 2 - 4, body_rect.bottom() - 5, 2, 2), stud_col)

        painter.restore()
        return txt_col

    # =========================================================================
    # 2. GHIBLI SPRING: Cartellino illustrato a doppia linea + Sakura Floreali
    # =========================================================================
    if theme_key == "ghibli_spring":
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        body_rect = rect.adjusted(1.0, 1.0, -1.0, -1.0)
        if is_down:
            body_rect.translate(0.0, 1.0)
        radius = min(11.0, body_rect.height() * 0.38)

        grad = QLinearGradient(body_rect.topLeft(), body_rect.bottomRight())
        if is_danger:
            grad.setColorAt(0.0, QColor("#FDE8E8"))
            grad.setColorAt(1.0, QColor("#F9D2D2"))
            outer_bdr = QColor("#E07A7A")
            inner_bdr = QColor(255, 255, 255, 190)
            sakura_col = QColor(224, 122, 122, 110)
            txt_col = QColor("#9E2A2B")
        elif active_like:
            grad.setColorAt(0.0, QColor("#F69DB2"))
            grad.setColorAt(1.0, QColor("#EE7895"))
            outer_bdr = QColor("#D96582")
            inner_bdr = QColor(255, 250, 245, 185)
            sakura_col = QColor(255, 245, 248, 155)
            txt_col = QColor("#FFFFFF")
        elif is_hovered:
            grad.setColorAt(0.0, QColor("#FCE4EC"))
            grad.setColorAt(1.0, QColor("#F8BBD0"))
            outer_bdr = QColor("#F2849E")
            inner_bdr = QColor(255, 255, 255, 200)
            sakura_col = QColor(242, 132, 158, 135)
            txt_col = QColor("#2C3A2F")
        else:
            if reading_mode:
                grad.setColorAt(0.0, QColor("#F7F1E3"))
                grad.setColorAt(1.0, QColor("#EFE6D4"))
                outer_bdr = QColor("#C5D6C0")
            else:
                grad.setColorAt(0.0, QColor("#F2F7F9"))
                grad.setColorAt(1.0, QColor("#E4EFF5"))
                outer_bdr = QColor("#A8C8E0")
            inner_bdr = QColor(255, 255, 255, 175)
            sakura_col = QColor(242, 132, 158, 95)
            txt_col = QColor("#2C3A2F")

        outer_path = QPainterPath()
        outer_path.addRoundedRect(body_rect, radius, radius)
        painter.fillPath(outer_path, grad)

        # Clip ai confini del bottone per disegnare i fiori di ciliegio negli angoli
        painter.save()
        painter.setClipPath(outer_path)
        stamen_col = QColor("#F4C44E") if not active_like else QColor("#FFE89E")
        if w >= 54:
            _draw_sakura_flower(
                painter,
                body_rect.left() + 8.5,
                body_rect.top() + 7.5,
                5.2 if h >= 28 else 4.2,
                sakura_col,
                stamen_col,
            )
            _draw_sakura_petal(
                painter,
                body_rect.left() + 17.5,
                body_rect.top() + 5.5,
                3.0,
                38.0,
                sakura_col,
            )
            _draw_sakura_flower(
                painter,
                body_rect.right() - 8.5,
                body_rect.bottom() - 7.0,
                4.6 if h >= 28 else 3.8,
                sakura_col,
                stamen_col,
            )
            _draw_sakura_petal(
                painter,
                body_rect.right() - 17.0,
                body_rect.top() + 7.5,
                3.2,
                -32.0,
                sakura_col,
            )
        else:
            # Bottone icona compatto: singolo petalo delicato nell'angolo
            _draw_sakura_petal(
                painter,
                body_rect.right() - 5.5,
                body_rect.top() + 5.5,
                3.0,
                -28.0,
                sakura_col,
            )
        painter.restore()

        # Cornice esterna morbida
        painter.setPen(QPen(outer_bdr, 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outer_path)

        # Cornice interna a doppia linea stile cartellino illustrato Ghibli
        if w >= 36 and h >= 22:
            inset = 3.0
            inner_rect = body_rect.adjusted(inset, inset, -inset, -inset)
            inner_radius = max(4.0, radius - inset)
            painter.setPen(QPen(inner_bdr, 1.0))
            painter.drawRoundedRect(inner_rect, inner_radius, inner_radius)

        painter.restore()
        return txt_col

    # =========================================================================
    # 3. KATANA / TACTICAL HUD / INDUSTRIAL MECHA: Taglio Poligonale a 45°
    # =========================================================================
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    body_rect = rect.adjusted(1.0, 1.0, -1.0, -1.0)
    if is_down:
        body_rect.translate(0.5, 1.0)
    cut = min(8.5, max(5.0, body_rect.height() * 0.30))
    poly_path = _build_chamfered_45_path(body_rect, cut)

    if theme_key == "katana":
        grad = QLinearGradient(body_rect.topLeft(), body_rect.bottomRight())
        if active_like or is_danger:
            grad.setColorAt(0.0, QColor("#591515"))
            grad.setColorAt(0.52, QColor("#B82323"))
            grad.setColorAt(1.0, QColor("#2A1212"))
            bdr_col = QColor("#FF4D4D") if is_hovered else QColor("#C59B5F")
            slash_col = QColor(255, 220, 180, 95)
            txt_col = QColor("#FFFDF9")
        elif is_hovered:
            grad.setColorAt(0.0, QColor("#3D1A1A"))
            grad.setColorAt(1.0, QColor("#221212"))
            bdr_col = QColor("#E03131")
            slash_col = QColor(224, 49, 49, 90)
            txt_col = QColor("#FFFDF9")
        else:
            grad.setColorAt(0.0, QColor("#2A2222") if not reading_mode else QColor("#241D1D"))
            grad.setColorAt(1.0, QColor("#1A1414") if not reading_mode else QColor("#161111"))
            bdr_col = QColor("#523428") if not reading_mode else QColor("#3D2820")
            slash_col = QColor(197, 155, 95, 55)
            txt_col = QColor("#F4EFEA")

        painter.fillPath(poly_path, grad)

        # Riflesso diagonale lama (Hamon) + filetto oro/cremisi sugli smussi a 45°
        painter.save()
        painter.setClipPath(poly_path)
        slash = QPainterPath()
        slash.moveTo(body_rect.left() + 4.0, body_rect.bottom())
        slash.lineTo(body_rect.left() + 14.0, body_rect.top())
        slash.lineTo(body_rect.left() + 18.5, body_rect.top())
        slash.lineTo(body_rect.left() + 8.5, body_rect.bottom())
        slash.closeSubpath()
        painter.fillPath(slash, slash_col)
        painter.restore()

        painter.setPen(QPen(bdr_col, 1.35 if active_like else 1.1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(poly_path)

        # Accento dorato/cremisi vivo lungo i due tagli obliqui a 45°
        chamfer_pen = QPen(QColor("#C59B5F") if active_like else QColor("#A63A3A"), 1.8)
        painter.setPen(chamfer_pen)
        painter.drawLine(
            QPointF(body_rect.right() - cut, body_rect.top()),
            QPointF(body_rect.right(), body_rect.top() + cut),
        )
        painter.drawLine(
            QPointF(body_rect.left() + cut, body_rect.bottom()),
            QPointF(body_rect.left(), body_rect.bottom() - cut),
        )
        painter.restore()
        return txt_col

    if theme_key == "tactical_hud":
        grad = QLinearGradient(body_rect.topLeft(), body_rect.bottomLeft())
        if is_danger:
            grad.setColorAt(0.0, QColor("#3A1216"))
            grad.setColorAt(1.0, QColor("#240B0E"))
            bdr_col = QColor("#FF334B")
            bracket_col = QColor("#FF6678")
            txt_col = QColor("#FFE5E8")
        elif active_like:
            grad.setColorAt(0.0, QColor("#183826"))
            grad.setColorAt(1.0, QColor("#0F2418"))
            bdr_col = QColor("#00FF66")
            bracket_col = QColor("#73FFAA")
            txt_col = QColor("#E6FFF0")
        elif is_hovered:
            grad.setColorAt(0.0, QColor("#152E20"))
            grad.setColorAt(1.0, QColor("#0E1F16"))
            bdr_col = QColor("#00E65C")
            bracket_col = QColor("#00FF66")
            txt_col = QColor("#E6FFF0")
        else:
            grad.setColorAt(0.0, QColor("#111C16") if not reading_mode else QColor("#0E1712"))
            grad.setColorAt(1.0, QColor("#0B130F") if not reading_mode else QColor("#09100C"))
            bdr_col = QColor("#234233") if not reading_mode else QColor("#1A3025")
            bracket_col = QColor("#00CC52")
            txt_col = QColor("#B8E6C8")

        painter.fillPath(poly_path, grad)
        painter.setPen(QPen(bdr_col, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(poly_path)

        # Tacche telemetriche HUD sugli angoli smussati e staffa luminosa
        painter.setPen(QPen(bracket_col, 1.8 if active_like else 1.4))
        # Angolo alto-sinistra: staffa a L
        painter.drawLine(QPointF(body_rect.left(), body_rect.top() + 7.0), QPointF(body_rect.left(), body_rect.top() + 1.0))
        painter.drawLine(QPointF(body_rect.left() + 1.0, body_rect.top()), QPointF(body_rect.left() + 8.0, body_rect.top()))
        # Angolo alto-destra: rinforzo diagonale 45°
        painter.drawLine(
            QPointF(body_rect.right() - cut, body_rect.top()),
            QPointF(body_rect.right(), body_rect.top() + cut),
        )
        # Angolo basso-sinistra: rinforzo diagonale 45°
        painter.drawLine(
            QPointF(body_rect.left() + cut, body_rect.bottom()),
            QPointF(body_rect.left(), body_rect.bottom() - cut),
        )
        painter.restore()
        return txt_col

    # industrial_mecha
    grad = QLinearGradient(body_rect.topLeft(), body_rect.bottomLeft())
    if is_danger:
        grad.setColorAt(0.0, QColor("#9E2424"))
        grad.setColorAt(1.0, QColor("#6E1717"))
        bdr_col = QColor("#FF4D4D")
        stripe_col = QColor("#F5B700")
        txt_col = QColor("#FFFFFF")
    elif active_like:
        grad.setColorAt(0.0, QColor("#FF5500"))
        grad.setColorAt(1.0, QColor("#CC4400"))
        bdr_col = QColor("#F5B700")
        stripe_col = QColor("#1A1C1E")
        txt_col = QColor("#111315") if is_pill else QColor("#FFFFFF")
    elif is_hovered:
        grad.setColorAt(0.0, QColor("#3D4248"))
        grad.setColorAt(1.0, QColor("#2D3136"))
        bdr_col = QColor("#FF5500")
        stripe_col = QColor("#FF5500")
        txt_col = QColor("#F3F4F6")
    else:
        grad.setColorAt(0.0, QColor("#32363B") if not reading_mode else QColor("#2B2F33"))
        grad.setColorAt(1.0, QColor("#26292D") if not reading_mode else QColor("#212428"))
        bdr_col = QColor("#494E54") if not reading_mode else QColor("#383C42")
        stripe_col = QColor("#F5B700")
        txt_col = QColor("#E5E7EB")

    painter.fillPath(poly_path, grad)

    # Banda corazzata laterale sinistra + rivetti meccanici
    painter.save()
    painter.setClipPath(poly_path)
    painter.fillRect(QRectF(body_rect.left(), body_rect.top(), 4.0, body_rect.height()), stripe_col)
    rivet_col = QColor("#111315") if active_like else QColor("#8C939E")
    painter.fillRect(QRectF(body_rect.right() - 5.5, body_rect.bottom() - 5.5, 2.2, 2.2), rivet_col)
    painter.fillRect(QRectF(body_rect.left() + 6.5, body_rect.top() + 3.5, 2.2, 2.2), rivet_col)
    painter.restore()

    painter.setPen(QPen(bdr_col, 1.3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(poly_path)

    painter.restore()
    return txt_col


class MorphologicalSlidingPill(QFrame):
    """Pillola attiva scorrevole che assume la sagoma morfologica reale del tema corrente."""

    def __init__(self, pill_object_name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName(pill_object_name)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        theme_key = ThemedPushButton.current_theme
        if theme_key not in _MORPHOLOGICAL_THEMES:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        _paint_morphological_surface(
            painter,
            QRectF(0.0, 0.0, float(self.width()), float(self.height())),
            theme_key,
            is_primary=True,
            is_checked=True,
            is_pill=True,
            reading_mode=ThemedPushButton.reading_mode,
        )
        painter.end()


class ThemedPushButton(QPushButton):
    """
    Bottone con sagoma morfologica reale (8-bit pixel steps, Ghibli sakura + doppia cornice,
    o lama/corazza tagliata a 45°) per i temi speciali, con fallback nativo QSS per i temi classici.
    """

    current_theme: str = "obsidian"
    reading_mode: bool = False

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def paintEvent(self, event):
        theme_key = self.current_theme
        if theme_key not in _MORPHOLOGICAL_THEMES:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))

        variant = str(self.property("variant") or "")
        obj_name = self.objectName()
        has_sliding_pill = bool(self.property("has_sliding_pill"))
        is_checked = self.isChecked()
        is_hovered = self.underMouse() and self.isEnabled()
        is_down = self.isDown() and self.isEnabled()

        is_primary = variant in ("primary", "accent") or obj_name in ("PrimaryBtn",)
        is_danger = variant == "danger"
        is_success = variant == "success"

        p = _resolve_palette(theme_key, self.reading_mode)

        if has_sliding_pill:
            # I bottoni dentro AnimatedSegmentedBox hanno già MorphologicalSlidingPill sotto quando checked
            if is_checked:
                txt_col = QColor(p.get("nav_checked_text", p.get("title", "#FFFFFF")))
                if theme_key == "pixel_chill":
                    txt_col = QColor("#1E2426")
                elif theme_key == "industrial_mecha":
                    txt_col = QColor("#111315")
            elif is_hovered:
                txt_col = _paint_morphological_surface(
                    painter,
                    rect,
                    theme_key,
                    is_hovered=True,
                    is_down=is_down,
                    reading_mode=self.reading_mode,
                )
            else:
                txt_col = QColor(p.get("subtitle", "#94A3B8"))
        else:
            txt_col = _paint_morphological_surface(
                painter,
                rect,
                theme_key,
                is_primary=is_primary,
                is_danger=is_danger,
                is_success=is_success,
                is_checked=is_checked,
                is_hovered=is_hovered,
                is_down=is_down,
                reading_mode=self.reading_mode,
            )

        if not self.isEnabled():
            txt_col.setAlpha(115)

        painter.setFont(self.font())
        painter.setPen(txt_col)
        text_rect = rect.adjusted(6.0, 0.0, -6.0, 0.0)
        if is_down and not has_sliding_pill:
            text_rect.translate(0.5, 1.0)

        icon = self.icon()
        text = self.text()
        if not icon.isNull():
            ic_sz = self.iconSize()
            iw, ih = ic_sz.width() or 16, ic_sz.height() or 16
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text) if text else 0
            gap = 6 if text else 0
            total_w = iw + gap + tw
            start_x = rect.center().x() - total_w / 2.0
            ic_y = rect.center().y() - ih / 2.0 + (1.0 if is_down else 0.0)
            pm = icon.pixmap(iw, ih)
            painter.drawPixmap(int(round(start_x)), int(round(ic_y)), pm)
            if text:
                t_rect = QRectF(start_x + iw + gap, text_rect.y(), tw + 8.0, text_rect.height())
                painter.drawText(t_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.end()


def _make_button(text: str, variant: Optional[str] = None) -> QPushButton:
    btn = ThemedPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if variant:
        btn.setProperty("variant", variant)
    return btn


def _make_label(text: str, object_name: str, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(object_name)
    if object_name.startswith("Badge"):
        lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    if wrap:
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return lbl


class ScrollList(QScrollArea):
    """QScrollArea anti-compressione: card a larghezza piena, altezza naturale."""

    def __init__(self, empty_text: str = "Nessun elemento."):
        super().__init__()
        self.empty_text = empty_text
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.content = QWidget()
        self.content.setObjectName("ScrollContent")
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.vbox = QVBoxLayout(self.content)
        self.vbox.setContentsMargins(2, 8, 6, 8)
        self.vbox.setSpacing(8)
        self.vbox.addStretch(1)
        self.setWidget(self.content)

    def clear(self):
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def add_card(self, widget: QWidget):
        self.vbox.insertWidget(self.vbox.count() - 1, widget)
        widget.show()

    def show_empty(self, text: Optional[str] = None):
        lbl = _make_label(text or self.empty_text, "EmptyState", wrap=True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.add_card(lbl)


def _make_card() -> (QFrame, QVBoxLayout):
    card = QFrame()
    card.setObjectName("Card")
    card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)
    return card, layout


class AudioTranscribeThread(QThread):
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(str, str, str, object)  # final_markdown, file_name, source_path, sync_data
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, file_path: str, subject_context: str = "", engine=None):
        super().__init__()
        self.file_path = file_path
        self.subject_context = (subject_context or "").strip()
        self.engine = engine
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            target_engine = self.engine
            if target_engine is None:
                from core.ai_pipeline import FlowLocalEngine
                target_engine = FlowLocalEngine()

            def on_progress(msg: str, percent: int = -1):
                self.progress.emit(msg, percent)

            def is_cancelled() -> bool:
                return self.cancel_requested

            markdown, sync_data = target_engine.transcribe_audio_file(
                self.file_path,
                subject_context=self.subject_context,
                progress_callback=on_progress,
                cancel_check=is_cancelled,
                return_sync_data=True
            )
            if self.cancel_requested:
                self.cancelled.emit()
                return

            file_name = os.path.basename(self.file_path)
            self.finished.emit(markdown, file_name, self.file_path, sync_data)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))


class ExtractFlashcardsThread(QThread):
    finished = pyqtSignal(int, str)  # count, note_title
    error = pyqtSignal(str)

    def __init__(self, note_id: Optional[int], note_text: str, note_title: str):
        super().__init__()
        self.note_id = note_id
        self.note_text = (note_text or "").strip()
        self.note_title = (note_title or "Nota").strip()

    def run(self):
        try:
            from core.ai_pipeline import LLMRefiner
            cards = LLMRefiner.extract_flashcards_from_note(self.note_text)
            if not cards:
                raise ValueError("Nessuna flashcard generata dal contenuto della nota.")
            count = 0
            for item in cards:
                q = (item.get("question") or "").strip()
                a = (item.get("answer") or "").strip()
                if q and a:
                    db.add_study_note(
                        title=f"Flashcard: {q[:60]}",
                        content_markdown=f"**Domanda:**\n{q}\n\n**Risposta:**\n{a}",
                        note_type="Flashcard",
                        tags=f"Esame, {self.note_title[:30]}",
                        parent_note_id=self.note_id
                    )
                    count += 1
            self.finished.emit(count, self.note_title)
        except Exception as e:
            self.error.emit(str(e))


class _AutoSaveNoteThread(QThread):
    """Salva silenziosamente in SQLite in background senza bloccare la UI."""
    saved = pyqtSignal(int, str, str, str, str, str)  # note_id, title, content, subj, top, ntype
    error = pyqtSignal(str)

    def __init__(self, note_id: Optional[int], title: str, content: str, prof: str, subj: str, top: str, ntype: str):
        super().__init__()
        self.note_id = note_id
        self.title = title
        self.content = content
        self.prof = prof
        self.subj = subj
        self.top = top
        self.ntype = ntype

    def run(self):
        try:
            if self.note_id:
                db.update_study_note(self.note_id, self.title, self.content, self.prof, self.subj, self.top, self.ntype)
                saved_id = self.note_id
            else:
                saved_id = db.add_study_note(self.title, self.content, self.ntype, "", None, self.prof, self.subj, self.top)
            self.saved.emit(saved_id, self.title, self.content, self.subj, self.top, self.ntype)
        except Exception as e:
            self.error.emit(str(e))


class _SummarizeNoteThread(QThread):
    """Genera un riassunto AI della nota corrente tramite Ollama Qwen 2.5 in background."""
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, note_text: str):
        super().__init__()
        self.note_text = (note_text or "").strip()

    def run(self):
        try:
            import requests
            from core.ai_pipeline import OLLAMA_API_URL, LLMRefiner
            model = LLMRefiner._detect_best_model() if hasattr(LLMRefiner, "_detect_best_model") else "qwen2.5:3b"
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Sei un tutor universitario. Riassumi in modo chiaro, conciso e strutturato "
                            "i concetti chiave del seguente appunto in italiano (massimo 5 punti elenco)."
                        ),
                    },
                    {"role": "user", "content": self.note_text[:6000]},
                ],
                "stream": False,
            }
            resp = requests.post(OLLAMA_API_URL, json=payload, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            summary = (data.get("message", {}).get("content") or "").strip()
            if not summary:
                raise ValueError("Risposta vuota dal modello Qwen.")
            self.finished.emit(summary)
        except Exception as e:
            self.error.emit(str(e))


class DocumentImportThread(QThread):
    """Thread di elaborazione asincrona per l'importazione di Slide, PDF e Immagini (con OCR)."""
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(dict)       # result_dict
    error = pyqtSignal(str)

    def __init__(self, file_path: str, profile: str = "Alessandro", subject: str = "Generale", topic: str = "Generale"):
        super().__init__()
        self.file_path = file_path
        self.profile = profile
        self.subject = subject
        self.topic = topic

    def run(self):
        try:
            from core.doc_importer import import_document

            def _on_progress(current, total, msg):
                pct = int((current / max(1, total)) * 100)
                self.progress.emit(msg, pct)

            result = import_document(
                file_path=self.file_path,
                profile=self.profile,
                subject=self.subject,
                topic=self.topic,
                progress_callback=_on_progress
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class CloudQueueProcessThread(QThread):
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(int, list)  # count, results
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, engine=None):
        super().__init__()
        self.engine = engine
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            from core.cloud_sync import cloud_sync
            target_engine = self.engine
            if target_engine is None:
                from core.ai_pipeline import FlowLocalEngine
                target_engine = FlowLocalEngine()

            pending = cloud_sync.get_pending_queue()
            if not pending:
                self.finished.emit(0, [])
                return

            total = len(pending)
            results = []

            for idx, item in enumerate(pending, 1):
                if self.cancel_requested:
                    self.cancelled.emit()
                    return

                item_filename = (item.get("filename") or "file").strip()

                def on_progress(msg: str, pct: int = -1):
                    base_pct = int(((idx - 1) / total) * 100)
                    file_pct = pct if pct >= 0 else 0
                    scaled_pct = int(base_pct + (file_pct / total))
                    self.progress.emit(f"File {idx}/{total} ({item_filename}): {msg}", scaled_pct)

                def is_cancelled() -> bool:
                    return self.cancel_requested

                res = cloud_sync.process_queue_item(
                    item=item,
                    engine=target_engine,
                    progress_callback=on_progress,
                    cancel_check=is_cancelled
                )
                results.append(res)

            # Rigenerazione indice library.json per la PWA Mobile
            self.progress.emit("Rigenerazione indice library.json per PWA Mobile...", 99)
            cloud_sync.generate_library_json()
            self.progress.emit("Elaborazione coda completata!", 100)
            self.finished.emit(len(results), results)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))


class NewSubjectTopicDialog(QDialog):
    """Dialogo compatto per creare una nuova Materia e Argomento nel Notekeeper."""

    def __init__(self, parent=None, default_profile="Alessandro"):
        super().__init__(parent)
        self.setWindowTitle("Nuova Materia / Argomento")
        self.setFixedWidth(440)
        self.setStyleSheet("""
            QDialog { background-color: #161920; color: #F8FAFC; }
            QLabel { color: #E2E8F0; font-size: 12px; font-weight: 500; }
            QLineEdit, QComboBox {
                background-color: #1E2330;
                color: #FFFFFF;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                min-height: 32px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_lbl = QLabel("📚 Crea Materia & Argomento")
        title_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #38BDF8;")
        layout.addWidget(title_lbl)

        layout.addWidget(QLabel("Profilo Studente:"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["Alessandro", "Giada"])
        if default_profile in ("Alessandro", "Giada"):
            self.profile_combo.setCurrentText(default_profile)
        layout.addWidget(self.profile_combo)

        layout.addWidget(QLabel("Materia (es. Meccanica dei Robot, Fisica 1):"))
        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Nome Materia...")
        layout.addWidget(self.subject_input)

        layout.addWidget(QLabel("Argomento (es. Cinematica Diretta, Urti):"))
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("Nome Argomento...")
        layout.addWidget(self.topic_input)

        layout.addWidget(QLabel("Titolo Primo Appunto (opzionale):"))
        self.note_title_input = QLineEdit()
        self.note_title_input.setPlaceholderText("Introduzione...")
        layout.addWidget(self.note_title_input)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = _make_button("Crea", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self):
        s = self.subject_input.text().strip()
        t = self.topic_input.text().strip() or "Generale"
        if not s:
            QMessageBox.warning(self, "Attenzione", "Inserisci almeno il nome della materia.")
            return
        p = self.profile_combo.currentText().strip()
        ntitle = self.note_title_input.text().strip() or f"Appunto iniziale - {t}"
        db.add_study_note(
            title=ntitle,
            content_markdown=f"# {ntitle}\n\nAppunti per la materia **{s}**, argomento **{t}**.",
            note_type="appunto",
            profile=p,
            subject=s,
            topic=t,
            tags=f"Notekeeper, {s}, {t}, Profilo: {p}"
        )
        self.accept()


# =====================================================================
# SISTEMA NOTE COLLEGATE & GRAFO DELLA CONOSCENZA (OBSIDIAN STYLE)
# =====================================================================

class ConnectNoteDialog(QDialog):
    """Dialogo compatto per creare manualmente un collegamento tra due note."""

    def __init__(self, parent, source_id: int, source_title: str):
        super().__init__(parent)
        self.source_id = source_id
        self.source_title = source_title
        self.setWindowTitle("🔗 Collega Nota (Stile Obsidian)")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog {
                background-color: #11141C;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
            QLabel {
                color: #CBD5E1;
                font-family: 'Segoe UI';
                font-size: 12px;
            }
            QLineEdit {
                background-color: #161D2B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
                min-height: 36px;
            }
            QLineEdit:focus {
                border-color: #0284C7;
            }
            QComboBox {
                background-color: #161D2B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
                min-height: 36px;
            }
            QComboBox:focus {
                border-color: #0284C7;
            }
            QComboBox QAbstractItemView {
                background-color: #161D2B;
                color: #F8FAFC;
                selection-background-color: #0284C7;
                selection-color: #FFFFFF;
                border: 1px solid #334155;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel("🔗 Nuovo Collegamento tra Note")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #F8FAFC;")
        layout.addWidget(header)

        src_lbl = QLabel(f"Nota di origine: <b style='color: #38BDF8;'>{source_title}</b>")
        src_lbl.setWordWrap(True)
        layout.addWidget(src_lbl)

        target_lbl = QLabel("Collega verso la nota:")
        target_lbl.setStyleSheet("font-weight: 600; color: #E2E8F0;")
        layout.addWidget(target_lbl)

        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(36)

        all_notes = db.get_notes()
        self.valid_targets = [n for n in all_notes if n.get("id") != self.source_id]
        if not self.valid_targets:
            self.target_combo.addItem("Nessun'altra nota disponibile", None)
            self.target_combo.setEnabled(False)
        else:
            for n in self.valid_targets:
                nid = n.get("id")
                ntitle = n.get("title") or "Senza Titolo"
                ntype = n.get("note_type") or "Nota"
                icon = "🎙️" if ntype == "Sbobina" else "📝"
                self.target_combo.addItem(f"{icon} [ID {nid}] {ntitle} ({ntype})", nid)
        layout.addWidget(self.target_combo)

        reason_lbl = QLabel("Motivo del collegamento (opzionale):")
        reason_lbl.setStyleSheet("font-weight: 600; color: #E2E8F0;")
        layout.addWidget(reason_lbl)

        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Es. Stesso istituto giuridico, approfondimento concettuale...")
        self.reason_edit.setMinimumHeight(36)
        layout.addWidget(self.reason_edit)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addStretch(1)

        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        self.save_btn = _make_button("🔗 Crea Collegamento", "primary")
        self.save_btn.clicked.connect(self._create_link)
        self.save_btn.setEnabled(bool(self.valid_targets))
        btn_box.addWidget(self.save_btn)

        layout.addLayout(btn_box)

    def _create_link(self):
        target_id = self.target_combo.currentData()
        if not target_id:
            QMessageBox.warning(self, "Nota non valida", "Seleziona una nota valida di destinazione.")
            return
        reason = self.reason_edit.text().strip()
        lid = db.add_note_link(self.source_id, target_id, reason)
        if lid:
            self.accept()
        else:
            QMessageBox.critical(self, "Errore", "Impossibile creare il collegamento.")


class AutoLinkThread(QThread):
    """Thread di background per eseguire l'auto-linking semantico con Ollama Qwen."""
    finished = pyqtSignal(int, int)  # note_id, count_links_created

    def __init__(self, note_id: int, title: str, content: str):
        super().__init__()
        self.note_id = note_id
        self.title = title
        self.content = content

    def run(self):
        try:
            from core.ai_pipeline import LLMRefiner
            all_notes = db.get_notes()
            candidates = [n for n in all_notes if n.get("id") != self.note_id]
            if not candidates:
                self.finished.emit(self.note_id, 0)
                return

            related = LLMRefiner.find_related_notes(self.title, self.content, candidates)
            created_count = 0
            for r in related:
                target_id = r.get("id")
                reason = r.get("reason", "")
                if target_id and db.add_note_link(self.note_id, target_id, reason):
                    created_count += 1
            self.finished.emit(self.note_id, created_count)
        except Exception as e:
            print(f"[FlowLocal] Errore esecuzione AutoLinkThread: {e}")
            self.finished.emit(self.note_id, 0)


class GraphEdgeItem(QGraphicsItem):
    """Arco orientato del Grafo della Conoscenza con tooltip motivazione."""

    def __init__(self, source_node: "GraphNodeItem", target_node: "GraphNodeItem", reason: str = "", link_id: int = 0):
        super().__init__()
        self.source = source_node
        self.target = target_node
        self.reason = reason
        self.link_id = link_id
        self.line = QLineF()
        self.setZValue(1)
        self.setAcceptHoverEvents(True)
        self.is_hovered = False

        reason_str = f"\nMotivo: {self.reason}" if self.reason else "\nMotivo: Correlazione concettuale"
        self.setToolTip(f"🔗 {self.source.title} ──► {self.target.title}{reason_str}")
        self.adjust()

    def boundingRect(self) -> QRectF:
        if self.line.isNull():
            return QRectF()
        extra = 12.0
        p1 = self.line.p1()
        p2 = self.line.p2()
        return QRectF(
            min(p1.x(), p2.x()) - extra,
            min(p1.y(), p2.y()) - extra,
            abs(p1.x() - p2.x()) + extra * 2,
            abs(p1.y() - p2.y()) + extra * 2
        )

    def adjust(self):
        if not self.source or not self.target:
            return
        line = QLineF(self.source.pos(), self.target.pos())
        length = line.length()
        if length <= 0.001:
            return

        self.prepareGeometryChange()
        r_src = self.source.radius
        r_dst = self.target.radius
        dx = line.dx() / length
        dy = line.dy() / length
        p1 = QPointF(self.source.pos().x() + dx * r_src, self.source.pos().y() + dy * r_src)
        p2 = QPointF(self.target.pos().x() - dx * r_dst, self.target.pos().y() - dy * r_dst)
        self.line = QLineF(p1, p2)
        self.update()

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.setZValue(5)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.setZValue(1)
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        if self.line.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self.is_hovered:
            pen = QPen(QColor("#38BDF8"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            color_brush = QColor("#38BDF8")
        else:
            pen = QPen(QColor(56, 189, 248, 140), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            color_brush = QColor(56, 189, 248, 180)

        painter.setPen(pen)
        painter.drawLine(self.line)

        # Freccia orientata verso il target
        angle = math.atan2(-self.line.dy(), self.line.dx())
        arrow_size = 9.0 if self.is_hovered else 7.5
        p2 = self.line.p2()
        arrow_p1 = p2 - QPointF(math.sin(angle + math.pi / 3.0) * arrow_size,
                                math.cos(angle + math.pi / 3.0) * arrow_size)
        arrow_p2 = p2 - QPointF(math.sin(angle + math.pi - math.pi / 3.0) * arrow_size,
                                math.cos(angle + math.pi - math.pi / 3.0) * arrow_size)

        arrow_head = QPolygonF([p2, arrow_p1, arrow_p2])
        painter.setBrush(QBrush(color_brush))
        painter.drawPolygon(arrow_head)


class GraphNodeItem(QGraphicsItem):
    """Nodo interattivo del Grafo (Sbobina o Appunto Rapido) colorato per tipo e trascinabile."""

    def __init__(self, node_data: Dict[str, Any], on_node_click: Optional[Callable[[int], None]] = None):
        super().__init__()
        self.node_id = node_data.get("id")
        self.title = node_data.get("title") or "Senza Titolo"
        self.note_type = node_data.get("note_type") or "Note"
        self.tags = node_data.get("tags") or ""
        self.preview = node_data.get("preview") or ""
        self.on_node_click = on_node_click

        # Dimensioni differenziate per tipo di nodo (compatte per evitare sovrapposizioni)
        self.is_sbobina = (self.note_type.lower() == "sbobina" or "sbobina" in self.title.lower())
        self.radius = 15.0 if self.is_sbobina else 11.5
        self.edges: List[GraphEdgeItem] = []

        # Variabili fisica Force-Directed
        self.vx = 0.0
        self.vy = 0.0
        self.mass = 1.3 if self.is_sbobina else 1.0
        self.is_dragged = False
        self.is_hovered = False

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

        # Etichetta pulita e compatta
        clean_title = self.title.replace("Sbobina:", "").strip()
        self.label_text = clean_title if len(clean_title) <= 16 else clean_title[:14] + "…"

        # Normalizzazione retrocompatibile tag per visualizzazione ("Profilo: Lei" -> "Profilo: Giada")
        raw_tags = self.tags or ""
        norm_tags = raw_tags.replace("Profilo: Lei", "Profilo: Giada")
        norm_tokens = [(t or "").strip() for t in norm_tags.split(",") if (t or "").strip()]
        norm_tokens = ["Giada" if t in ("Lei", "Profilo: Lei") else t for t in norm_tokens]
        display_tags = ", ".join(norm_tokens)

        tipo_display = "🎙️ Sbobina" if self.is_sbobina else "⚡ Appunto Rapido"
        tip = f"[{tipo_display}] {self.title}\nTag: {display_tags or 'Nessuno'}"
        if self.preview:
            tip += f"\n\nAnteprima:\n{self.preview[:160]}..."
        tip += "\n\n(Clicca per aprire ed evidenziare la nota nella lista)"
        self.setToolTip(tip)

    def add_edge(self, edge: GraphEdgeItem):
        self.edges.append(edge)

    def boundingRect(self) -> QRectF:
        r = self.radius + 6.0
        return QRectF(-r - 35, -r, (r + 35) * 2, r * 2 + 22)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(-self.radius, -self.radius, self.radius * 2, self.radius * 2)
        return path

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.adjust()
            scene = self.scene()
            if scene and hasattr(scene, "wake_simulation"):
                scene.wake_simulation()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self.is_dragged = True
        self.setZValue(25)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.is_dragged = False
        self.setZValue(10)
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self.on_node_click:
                self.on_node_click(self.node_id)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.setZValue(20)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.setZValue(10)
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # Tavolozza per tipo di nodo
        if self.is_sbobina:
            base_col = QColor("#7C3AED")      # Viola scuro
            border_col = QColor("#C4B5FD")    # Viola brillante
            glow_col = QColor(139, 92, 246, 75)
            icon_char = "🎙️"
        elif "latex" in self.note_type.lower():
            base_col = QColor("#D97706")      # Ambra
            border_col = QColor("#FDE68A")
            glow_col = QColor(245, 158, 11, 75)
            icon_char = "📐"
        else:
            base_col = QColor("#0284C7")      # Cyan FlowLocal
            border_col = QColor("#7DD3FC")
            glow_col = QColor(2, 132, 199, 75)
            icon_char = "⚡"

        # Alone luminoso esterno su hover o selezione
        if self.is_hovered or self.isSelected():
            glow_pen = QPen(QColor("#FFFFFF"), 2.0)
            painter.setPen(glow_pen)
            painter.setBrush(QBrush(glow_col))
            painter.drawEllipse(QPointF(0, 0), self.radius + 6.0, self.radius + 6.0)

        # Cerchio del nodo con gradiente radiale
        grad = QRadialGradient(QPointF(-self.radius * 0.25, -self.radius * 0.25), self.radius * 1.3)
        grad.setColorAt(0.0, border_col)
        grad.setColorAt(1.0, base_col)

        b_width = 3.0 if (self.is_hovered or self.isSelected()) else 2.0
        painter.setPen(QPen(border_col, b_width))
        painter.setBrush(QBrush(grad))
        painter.drawEllipse(QPointF(0, 0), self.radius, self.radius)

        # Icona centrale
        font_icon = QFont("Segoe UI Emoji", 8 if self.radius < 14 else 10)
        painter.setFont(font_icon)
        painter.setPen(QPen(QColor("#FFFFFF")))
        painter.drawText(QRectF(-self.radius, -self.radius, self.radius * 2, self.radius * 2), Qt.AlignmentFlag.AlignCenter, icon_char)

        # Pillola titolo sotto il nodo (font compatto 8pt) per evitare sovrapposizioni
        font_label = QFont("Segoe UI", 8)
        font_label.setBold(True)
        painter.setFont(font_label)

        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(self.label_text) + 8
        text_h = metrics.height() + 2
        text_y = self.radius + 3

        pill_rect = QRectF(-text_w / 2, text_y, text_w, text_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(15, 20, 31, 220)))
        painter.drawRoundedRect(pill_rect, 3, 3)

        text_color = QColor("#38BDF8") if self.is_hovered else QColor("#F8FAFC")
        painter.setPen(QPen(text_color))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self.label_text)


class KnowledgeGraphScene(QGraphicsScene):
    """Scena con simulazione fisica Force-Directed (Coulomb + Hooke + Center Gravity)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes: List[GraphNodeItem] = []
        self.edges: List[GraphEdgeItem] = []

        self.sim_timer = QTimer(self)
        self.sim_timer.setInterval(28)  # ~35 FPS
        self.sim_timer.timeout.connect(self._step_simulation)
        self.iterations = 0

    def wake_simulation(self):
        self.iterations = 0
        if not self.sim_timer.isActive():
            self.sim_timer.start()

    def _step_simulation(self):
        if not self.nodes:
            self.sim_timer.stop()
            return

        self.iterations += 1
        total_movement = 0.0

        k_rep = 14000.0    # Repulsione elettrostatica aumentata (distanza nodi maggiore)
        k_spring = 0.025   # Molla elastica morbida
        l_0 = 260.0        # Lunghezza a riposo della molla aumentata (260 px)
        k_gravity = 0.0025 # Attrazione verso il baricentro ridotta per distanziare i nodi
        damping = 0.85     # Smorzamento della velocità

        n_count = len(self.nodes)

        # 1. Repulsione Coulomb tra ogni coppia di nodi
        for i in range(n_count):
            n1 = self.nodes[i]
            p1 = n1.pos()
            for j in range(i + 1, n_count):
                n2 = self.nodes[j]
                p2 = n2.pos()
                dx = p1.x() - p2.x()
                dy = p1.y() - p2.y()
                dist = math.hypot(dx, dy)
                if dist < 1.0:
                    dx = random.uniform(-2, 2)
                    dy = random.uniform(-2, 2)
                    dist = math.hypot(dx, dy) or 1.0

                if dist < 900.0:
                    force = k_rep / (dist * dist)
                    fx = (dx / dist) * force
                    fy = (dy / dist) * force

                    n1.vx += fx / n1.mass
                    n1.vy += fy / n1.mass
                    n2.vx -= fx / n2.mass
                    n2.vy -= fy / n2.mass

        # 2. Attrazione Hooke lungo gli archi
        for edge in self.edges:
            n1 = edge.source
            n2 = edge.target
            p1 = n1.pos()
            p2 = n2.pos()
            dx = p2.x() - p1.x()
            dy = p2.y() - p1.y()
            dist = math.hypot(dx, dy)
            if dist < 1.0:
                continue
            delta = dist - l_0
            force = k_spring * delta
            fx = (dx / dist) * force
            fy = (dy / dist) * force

            n1.vx += fx / n1.mass
            n1.vy += fy / n1.mass
            n2.vx -= fx / n2.mass
            n2.vy -= fy / n2.mass

        # 3. Gravità centrale e aggiornamento posizioni
        for node in self.nodes:
            pos = node.pos()
            node.vx -= pos.x() * k_gravity
            node.vy -= pos.y() * k_gravity

            node.vx *= damping
            node.vy *= damping

            speed = math.hypot(node.vx, node.vy)
            if speed > 16.0:
                node.vx = (node.vx / speed) * 16.0
                node.vy = (node.vy / speed) * 16.0

            if not node.is_dragged:
                node.setPos(pos.x() + node.vx, pos.y() + node.vy)
                total_movement += speed

            for edge in node.edges:
                edge.adjust()

        # Fermata quando la convergenza è raggiunta o dopo 350 iterazioni
        if (total_movement < 0.4 and self.iterations > 25) or self.iterations > 350:
            self.sim_timer.stop()


class KnowledgeGraphWidget(QFrame):
    """Widget interattivo PyQt6 contenente QGraphicsView + QGraphicsScene per il Grafo della Conoscenza."""

    def __init__(self, on_node_click: Optional[Callable[[int], None]] = None, parent=None):
        super().__init__(parent)
        self.on_node_click = on_node_click
        self.setObjectName("KnowledgeGraphFrame")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Barra controlli overlay in cima al grafo
        self.toolbar = QFrame()
        self.toolbar.setObjectName("KnowledgeGraphToolbar")
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(8)

        self.stats_label = QLabel("Caricamento nodi...")
        self.stats_label.setStyleSheet("color: #94A3B8; font-size: 11.5px; font-weight: 500;")
        tb_layout.addWidget(self.stats_label)

        tb_layout.addStretch(1)

        zoom_in_btn = _make_button("🔍 +")
        zoom_in_btn.setToolTip("Ingrandisci zoom")
        zoom_in_btn.clicked.connect(self._zoom_in)
        tb_layout.addWidget(zoom_in_btn)

        zoom_out_btn = _make_button("🔍 -")
        zoom_out_btn.setToolTip("Riduci zoom")
        zoom_out_btn.clicked.connect(self._zoom_out)
        tb_layout.addWidget(zoom_out_btn)

        fit_btn = _make_button("🎯 Centra")
        fit_btn.setToolTip("Centra tutti i nodi nella vista")
        fit_btn.clicked.connect(self._fit_view)
        tb_layout.addWidget(fit_btn)

        reload_btn = _make_button("🔄 Ricarica")
        reload_btn.setToolTip("Ricarica i nodi e riavvia la simulazione fisica")
        reload_btn.clicked.connect(self.load_data)
        tb_layout.addWidget(reload_btn)

        layout.addWidget(self.toolbar)

        # Scena e Vista QGraphics
        self.scene = KnowledgeGraphScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setObjectName("KnowledgeGraphView")

        layout.addWidget(self.view, 1)

    def _zoom_in(self):
        self.view.scale(1.2, 1.2)

    def _zoom_out(self):
        self.view.scale(0.83, 0.83)

    def _fit_view(self):
        rect = self.scene.itemsBoundingRect()
        if not rect.isNull() and rect.width() > 10:
            self.view.fitInView(rect.adjusted(-90, -90, 90, 90), Qt.AspectRatioMode.KeepAspectRatio)

    def load_data(self):
        """Carica nodi e archi dal database SQLite e avvia la disposizione force-directed."""
        self.scene.clear()
        self.scene.nodes.clear()
        self.scene.edges.clear()

        graph_data = db.get_all_graph_data()
        nodes_raw = graph_data.get("nodes", [])
        edges_raw = graph_data.get("edges", [])

        if not nodes_raw:
            self.stats_label.setText("Nessuna nota salvata nel Grafo.")
            empty_item = self.scene.addText("Nessuna nota presente.\nSalva o sbobina una lezione per visualizzare il Grafo della Conoscenza.")
            empty_item.setDefaultTextColor(QColor("#64748B"))
            font = QFont("Segoe UI", 12)
            font.setBold(True)
            empty_item.setFont(font)
            empty_item.setPos(-160, -20)
            return

        sbobine_count = sum(1 for n in nodes_raw if (n.get("note_type", "").lower() == "sbobina" or "sbobina" in (n.get("title") or "").lower()))
        rapidi_count = len(nodes_raw) - sbobine_count
        self.stats_label.setText(
            f"🟣 {sbobine_count} Sbobine   |   🔵 {rapidi_count} Appunti Rapidi   |   🔗 {len(edges_raw)} Collegamenti"
        )

        nodes_map: Dict[int, GraphNodeItem] = {}

        # Disposizione iniziale circolare con ampia spaziatura e jitter casuale
        count = len(nodes_raw)
        radius_init = max(180.0, count * 65.0)
        for i, n_data in enumerate(nodes_raw):
            node_item = GraphNodeItem(n_data, on_node_click=self.on_node_click)
            theta = (2.0 * math.pi * i) / count
            jitter = random.uniform(-15, 15)
            x = math.cos(theta) * (radius_init + jitter)
            y = math.sin(theta) * (radius_init + jitter)
            node_item.setPos(x, y)
            self.scene.addItem(node_item)
            self.scene.nodes.append(node_item)
            nodes_map[n_data["id"]] = node_item

        # Creazione archi orientati
        for e_data in edges_raw:
            src_id = e_data.get("source_id")
            tgt_id = e_data.get("target_id")
            if src_id in nodes_map and tgt_id in nodes_map:
                src_item = nodes_map[src_id]
                tgt_item = nodes_map[tgt_id]
                edge_item = GraphEdgeItem(src_item, tgt_item, reason=e_data.get("reason", ""), link_id=e_data.get("id", 0))
                self.scene.addItem(edge_item)
                self.scene.edges.append(edge_item)
                src_item.add_edge(edge_item)
                tgt_item.add_edge(edge_item)

        # Avvio simulazione fisica
        self.scene.wake_simulation()
        QTimer.singleShot(250, self._fit_view)


from ui.voice_orb import create_smooth_closed_path


class OrbBrandBadge(QWidget):
    """Mini emblema animato identico all'Orb in ascolto (3 fuochi fluidi senza onde vocali)."""

    def __init__(self, size: int = 32, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._plasma_phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start(16)  # ~60 FPS come VoiceOrbOverlay

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def _tick(self):
        if not self.isVisible():
            return
        self._plasma_phase += 0.025
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        cx, cy = w / 2.0, h / 2.0
        core_radius = min(w, h) * 0.38

        # Piccolo fondo scuro morbido al centro (come VoiceOrbOverlay)
        base_r = core_radius * 1.12
        base_grad = QRadialGradient(cx, cy, base_r)
        base_grad.setColorAt(0.0, QColor(8, 10, 16, 220))
        base_grad.setColorAt(0.70, QColor(8, 10, 16, 155))
        base_grad.setColorAt(1.0, QColor(8, 10, 16, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base_grad)
        p.drawEllipse(QPointF(cx, cy), base_r, base_r)

        # I 3 fuochi fluidi indipendenti in ascolto (senza onde vocali)
        num_pts = 16
        wobble_amp = 0.065
        orbit_r = 1.6
        flame_layers = [
            (QColor("#38BDF8"), self._plasma_phase, 1.00, 195),
            (QColor("#A855F7"), self._plasma_phase * 1.35 + 2.1, 0.92, 185),
            (QColor("#10B981"), -self._plasma_phase * 0.9 + 4.2, 0.86, 175),
        ]

        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        for f_col, f_ang, r_scale, f_alpha in flame_layers:
            fx = cx + orbit_r * math.cos(f_ang)
            fy = cy + orbit_r * math.sin(f_ang * 1.15)

            f_points = []
            for i in range(num_pts):
                theta = (2.0 * math.pi * i) / num_pts - (math.pi / 2.0)
                wobble = 1.0 + wobble_amp * math.sin(theta * 2.0 + f_ang)
                r_pt = core_radius * 0.88 * r_scale * wobble
                f_points.append(QPointF(fx + r_pt * math.cos(theta), fy + r_pt * math.sin(theta)))

            f_path = create_smooth_closed_path(f_points)
            f_grad = QRadialGradient(fx, fy, core_radius * r_scale + 2.0)
            c_hot = QColor(f_col)
            c_hot.setAlpha(f_alpha)
            c_mid = QColor(f_col)
            c_mid.setAlpha(int(f_alpha * 0.72))
            c_tip = QColor(f_col)
            c_tip.setAlpha(0)
            f_grad.setColorAt(0.0, c_hot)
            f_grad.setColorAt(0.68, c_mid)
            f_grad.setColorAt(1.0, c_tip)

            p.setBrush(f_grad)
            p.drawPath(f_path)
        p.restore()


class ThemeRevealOverlay(QWidget):
    """Animazione fluida a 60 FPS: un Orb luminoso si espande dal centro rivelando il nuovo tema."""

    def __init__(self, parent: QWidget, old_pixmap):
        super().__init__(parent)
        self._old_pixmap = old_pixmap
        self._progress = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(parent.rect())
        self.show()
        self.raise_()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(16)

    def _step(self):
        self._progress += 0.045  # ~360ms a 60 FPS
        if self._progress >= 1.0:
            self._timer.stop()
            self.hide()
            self.deleteLater()
            return
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        cx, cy = w / 2.0, h / 2.0
        max_r = math.hypot(cx, cy) + 24.0

        # Ease-out cubico
        t = 1.0 - ((1.0 - min(1.0, self._progress)) ** 3)
        r = max(1.0, max_r * t)

        # Disegna il vecchio tema solo FUORI dal cerchio che si espande
        outer_path = QPainterPath()
        outer_path.addRect(QRectF(0.0, 0.0, w, h))
        hole_path = QPainterPath()
        hole_path.addEllipse(QPointF(cx, cy), r, r)
        clip_path = outer_path.subtracted(hole_path)

        p.save()
        p.setClipPath(clip_path)
        p.drawPixmap(0, 0, self._old_pixmap)
        p.restore()

        # Anello plasma dei 3 fuochi sul bordo dell'espansione
        ring_alpha = int(200 * (1.0 - self._progress * 0.75))
        if ring_alpha > 4:
            p.save()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            ring_grad = QRadialGradient(cx, cy, r + 18.0)
            inner_stop = max(0.0, (r - 18.0) / (r + 18.0))
            mid_stop = max(inner_stop, min(1.0, r / (r + 18.0)))
            ring_grad.setColorAt(0.0, QColor(56, 189, 248, 0))
            ring_grad.setColorAt(inner_stop, QColor(56, 189, 248, 0))
            ring_grad.setColorAt(mid_stop, QColor(168, 85, 247, ring_alpha))
            ring_grad.setColorAt(1.0, QColor(16, 185, 129, 0))
            p.setPen(QPen(QColor(56, 189, 248, ring_alpha), 2.5))
            p.setBrush(ring_grad)
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.restore()


class AuroraDivider(QWidget):
    """Sottile linea luminosa sfumata con i 3 colori dei fuochi."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(2)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, float(self.width()), 0)
        grad.setColorAt(0.0, QColor(56, 189, 248, 0))
        grad.setColorAt(0.25, QColor(56, 189, 248, 160))
        grad.setColorAt(0.55, QColor(168, 85, 247, 170))
        grad.setColorAt(0.80, QColor(16, 185, 129, 140))
        grad.setColorAt(1.0, QColor(16, 185, 129, 0))
        p.fillRect(self.rect(), grad)


class QuickRuleDialog(QDialog):
    """Dialog rapido per trasformare una trascrizione in regola Dizionario o Snippet."""

    def __init__(self, initial_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Correggi / Salva Regola")
        self.setMinimumWidth(520)
        self.setStyleSheet(parent.styleSheet() if parent and parent.styleSheet() else HUB_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(_make_label("Crea regola rapida dalla trascrizione", "PageTitle"))
        layout.addWidget(_make_label(
            "Salva una correzione fonetica nel Dizionario oppure trasforma la frase in uno Snippet vocale.",
            "PageSubtitle",
            wrap=True,
        ))

        tab_row = QHBoxLayout()
        tab_row.setSpacing(8)
        self.btn_dict = QPushButton("📖 Dizionario (Alias → Parola)")
        self.btn_dict.setObjectName("FilterPill")
        self.btn_dict.setCheckable(True)
        self.btn_dict.setChecked(True)
        self.btn_snip = QPushButton("⚡ Snippet (Trigger → Testo)")
        self.btn_snip.setObjectName("FilterPill")
        self.btn_snip.setCheckable(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.btn_dict, 0)
        self._mode_group.addButton(self.btn_snip, 1)

        tab_row.addWidget(self.btn_dict)
        tab_row.addWidget(self.btn_snip)
        tab_row.addStretch(1)
        layout.addLayout(tab_row)

        self.stack = QStackedWidget()

        # Pagina 0: Dizionario
        dict_w = QWidget()
        dl = QVBoxLayout(dict_w)
        dl.setContentsMargins(0, 4, 0, 0)
        dl.setSpacing(8)
        dl.addWidget(_make_label("Parola errata / alias pronunciato", "CardMeta"))
        self.alias_edit = QLineEdit(initial_text.strip())
        self.alias_edit.setPlaceholderText("Es. antigraffiti")
        self.alias_edit.setMinimumHeight(36)
        dl.addWidget(self.alias_edit)
        dl.addWidget(_make_label("Parola corretta da inserire", "CardMeta"))
        self.term_edit = QLineEdit()
        self.term_edit.setPlaceholderText("Es. Antigravity")
        self.term_edit.setMinimumHeight(36)
        dl.addWidget(self.term_edit)
        dl.addStretch(1)
        self.stack.addWidget(dict_w)

        # Pagina 1: Snippet
        snip_w = QWidget()
        sl = QVBoxLayout(snip_w)
        sl.setContentsMargins(0, 4, 0, 0)
        sl.setSpacing(8)
        sl.addWidget(_make_label("Frase trigger vocale (breve)", "CardMeta"))
        self.trigger_edit = QLineEdit()
        self.trigger_edit.setPlaceholderText("Es. firma cliente")
        self.trigger_edit.setMinimumHeight(36)
        sl.addWidget(self.trigger_edit)
        sl.addWidget(_make_label("Testo espanso completo", "CardMeta"))
        self.expansion_edit = QTextEdit()
        self.expansion_edit.setMinimumHeight(110)
        self.expansion_edit.setPlainText(initial_text.strip())
        sl.addWidget(self.expansion_edit, 1)
        self.stack.addWidget(snip_w)

        self._mode_group.idClicked.connect(self.stack.setCurrentIndex)
        layout.addWidget(self.stack, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        save_btn = _make_button("Salva regola", "primary")
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(cancel_btn)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

    def _on_save(self):
        if self.stack.currentIndex() == 0:
            alias = self.alias_edit.text().strip()
            term = self.term_edit.text().strip()
            if not term:
                self.term_edit.setFocus()
                return
            if alias:
                db.learn_vocabulary_pair(alias, term)
            else:
                existing = {v["correct_term"].lower() for v in db.get_all_vocabulary()}
                if term.lower() not in existing:
                    db.add_vocabulary(term, "", "Manuale")
        else:
            trigger = self.trigger_edit.text().strip()
            expansion = self.expansion_edit.toPlainText().strip()
            if not trigger:
                self.trigger_edit.setFocus()
                return
            if not expansion:
                self.expansion_edit.setFocus()
                return
            db.add_snippet(trigger, expansion)
        self.accept()


class SpotifyMiniBar(QFrame):
    """Mini-player Spotify compatto per la barra superiore di FlowLocal (icone vettoriali nitide)."""

    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_PLAY_PAUSE = 0xB3
    APPCOMMAND_MEDIA_NEXTTRACK = 11
    APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
    APPCOMMAND_MEDIA_PLAY_PAUSE = 14
    WM_APPCOMMAND = 0x0319
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SpotifyMiniBar")
        self.setFixedHeight(30)
        self.setFixedWidth(242)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._spotify_hwnd: Optional[int] = None
        self._spotify_running = False
        self._is_playing = False
        self._is_dark_theme = True
        self._last_raw_title = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)

        self.track_btn = QPushButton("🎵 Spotify")
        self.track_btn.setObjectName("SpotifyTrackBtn")
        self.track_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.track_btn.setFixedWidth(152)
        self.track_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.track_btn.setToolTip("Clicca per aprire o portare in primo piano Spotify")
        self.track_btn.clicked.connect(self._on_track_clicked)
        layout.addWidget(self.track_btn)

        self.prev_btn = QPushButton("")
        self.prev_btn.setObjectName("SpotifyCtrlBtn")
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.setIconSize(QSize(13, 13))
        self.prev_btn.setToolTip("Brano precedente")
        self.prev_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_PREV_TRACK, self.APPCOMMAND_MEDIA_PREVIOUSTRACK)
        )
        layout.addWidget(self.prev_btn)

        self.play_btn = QPushButton("")
        self.play_btn.setObjectName("SpotifyCtrlBtn")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setIconSize(QSize(13, 13))
        self.play_btn.setToolTip("Play / Pausa")
        self.play_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_PLAY_PAUSE, self.APPCOMMAND_MEDIA_PLAY_PAUSE)
        )
        layout.addWidget(self.play_btn)

        self.next_btn = QPushButton("")
        self.next_btn.setObjectName("SpotifyCtrlBtn")
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.setIconSize(QSize(13, 13))
        self.next_btn.setToolTip("Brano successivo")
        self.next_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_NEXT_TRACK, self.APPCOMMAND_MEDIA_NEXTTRACK)
        )
        layout.addWidget(self.next_btn)

        self._update_control_icons()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_spotify)
        self._poll_timer.start(1800)
        QTimer.singleShot(300, self._poll_spotify)

    @staticmethod
    def _make_media_icon(kind: str, color: str, size: int = 13) -> QIcon:
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        col = QColor(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))

        s = float(size)
        if kind == "pause":
            bw = max(2.2, s * 0.22)
            gap = max(2.2, s * 0.20)
            total_w = bw * 2.0 + gap
            x0 = (s - total_w) / 2.0
            top = s * 0.16
            bh = s * 0.68
            p.drawRoundedRect(QRectF(x0, top, bw, bh), 1.0, 1.0)
            p.drawRoundedRect(QRectF(x0 + bw + gap, top, bw, bh), 1.0, 1.0)
        elif kind == "play":
            poly = QPolygonF([
                QPointF(s * 0.25, s * 0.14),
                QPointF(s * 0.86, s * 0.50),
                QPointF(s * 0.25, s * 0.86),
            ])
            p.drawPolygon(poly)
        elif kind == "prev":
            p.drawRoundedRect(QRectF(s * 0.15, s * 0.18, s * 0.14, s * 0.64), 0.8, 0.8)
            poly = QPolygonF([
                QPointF(s * 0.84, s * 0.17),
                QPointF(s * 0.34, s * 0.50),
                QPointF(s * 0.84, s * 0.83),
            ])
            p.drawPolygon(poly)
        elif kind == "next":
            poly = QPolygonF([
                QPointF(s * 0.16, s * 0.17),
                QPointF(s * 0.66, s * 0.50),
                QPointF(s * 0.16, s * 0.83),
            ])
            p.drawPolygon(poly)
            p.drawRoundedRect(QRectF(s * 0.71, s * 0.18, s * 0.14, s * 0.64), 0.8, 0.8)

        p.end()
        return QIcon(pm)

    def set_theme_mode(self, is_dark: bool):
        self._is_dark_theme = bool(is_dark)
        self._update_control_icons()

    def _update_control_icons(self):
        fg = "#E2E8F0" if self._is_dark_theme else "#334155"
        play_col = ("#10B981" if self._is_dark_theme else "#059669") if self._is_playing else fg
        win = self.window()
        if win is not None and hasattr(win, "_active_palette"):
            try:
                pal = win._active_palette()
                fg = pal.get("text", fg)
                if self._is_playing:
                    play_col = pal.get("accent", play_col)
                else:
                    play_col = fg
            except Exception:
                pass
        self.prev_btn.setIcon(self._make_media_icon("prev", fg))
        self.next_btn.setIcon(self._make_media_icon("next", fg))
        if self._is_playing:
            self.play_btn.setIcon(self._make_media_icon("pause", play_col))
            self.play_btn.setToolTip("Metti in pausa Spotify")
        else:
            self.play_btn.setIcon(self._make_media_icon("play", play_col))
            self.play_btn.setToolTip("Avvia o riprendi Spotify")

    @staticmethod
    def _find_spotify_window() -> tuple:
        """Restituisce (is_running, hwnd, title) per Spotify Desktop su Windows."""
        spotify_pids = set()
        if psutil is not None:
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    pname = (proc.info.get("name") or "").lower()
                    if pname == "spotify.exe":
                        spotify_pids.add(proc.info["pid"])
            except Exception:
                pass

        if not spotify_pids:
            return False, None, ""

        found_windows = []
        try:
            user32 = ctypes.windll.user32
            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in spotify_pids:
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = (buf.value or "").strip()
                if title and title not in ("Default IME", "MSCTFIME UI", "AngleHiddenWindow"):
                    found_windows.append((hwnd, title))
                return True

            user32.EnumWindows(enum_proc(_cb), 0)
        except Exception:
            pass

        if not found_windows:
            return True, None, "Spotify"

        # Preferisci la finestra che mostra "Artista - Brano"
        for hwnd, title in found_windows:
            if " - " in title or " – " in title:
                return True, hwnd, title
        return True, found_windows[0][0], found_windows[0][1]

    def _poll_spotify(self):
        win = self.window()
        if win is not None and not win.isVisible():
            return
        running, hwnd, raw_title = self._find_spotify_window()
        self._spotify_running = running
        self._spotify_hwnd = hwnd
        self._last_raw_title = raw_title

        is_playing_now = False
        if not running:
            raw_display = "🎵 Apri Spotify"
            tooltip = "Spotify non è aperto — clicca per avviarlo"
        elif raw_title.lower() in ("spotify", "spotify free", "spotify premium", ""):
            raw_display = "🎵 Spotify · Pausa"
            tooltip = "Spotify in pausa — premi Play per riprendere"
        else:
            is_playing_now = True
            clean = raw_title.replace(" - ", " — ")
            tooltip = f"In riproduzione su Spotify:\n{clean}"
            raw_display = f"🎵 {clean}"

        display = self.track_btn.fontMetrics().elidedText(
            raw_display, Qt.TextElideMode.ElideRight, 134
        )

        if is_playing_now != self._is_playing:
            self._is_playing = is_playing_now
            self._update_control_icons()

        if display != self.track_btn.text() or tooltip != self.track_btn.toolTip():
            self.track_btn.setText(display)
            self.track_btn.setToolTip(tooltip)

    def _verify_or_fallback_appcommand(self, prev_title: str, app_command: int):
        running, hwnd, raw_title = self._find_spotify_window()
        if running and hwnd and raw_title == prev_title:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, self.WM_APPCOMMAND, hwnd, app_command << 16)
            except Exception as e:
                print(f"[SpotifyMiniBar] Fallback WM_APPCOMMAND fallito: {e}")
        QTimer.singleShot(260, self._poll_spotify)

    def _send_media_key(self, vk_code: int, app_command: int):
        prev_title = self._last_raw_title
        if vk_code == self.VK_MEDIA_PLAY_PAUSE and self._spotify_running:
            self._is_playing = not self._is_playing
            self._update_control_icons()
        try:
            user32 = ctypes.windll.user32
            scan_code = user32.MapVirtualKeyW(vk_code, 0)
            user32.keybd_event(vk_code, scan_code, self.KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(vk_code, scan_code, self.KEYEVENTF_EXTENDEDKEY | self.KEYEVENTF_KEYUP, 0)
        except Exception as e:
            print(f"[SpotifyMiniBar] Errore comando media: {e}")
        QTimer.singleShot(320, lambda pt=prev_title, cmd=app_command: self._verify_or_fallback_appcommand(pt, cmd))

    def _on_track_clicked(self):
        if self._spotify_running and self._spotify_hwnd:
            try:
                user32 = ctypes.windll.user32
                if user32.IsIconic(self._spotify_hwnd):
                    user32.ShowWindow(self._spotify_hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(self._spotify_hwnd)
                return
            except Exception:
                pass
        try:
            os.startfile("spotify:")
            QTimer.singleShot(1500, self._poll_spotify)
        except Exception as e:
            print(f"[SpotifyMiniBar] Impossibile avviare Spotify: {e}")


class SlidingStackedWidget(QStackedWidget):
    """
    QStackedWidget con transizioni spaziali direzionali fluide (Stile iOS / One UI):
    - Parallasse asimmetrica (pagina entrante 100% sopra con raise_(), pagina uscente 35% in direzione opposta).
    - Durata 280ms con curva QEasingCurve.Type.OutCubic.
    - Gestione Interrupt istantanea sui click rapidi (zero blocco input, zero glitch).
    - Supporta mappa di ranghi spaziali (`spatial_ranks`) per rispettare l'ordine visivo dei bottoni nella Top Bar.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        duration_ms: int = 280,
        spatial_ranks: Optional[Dict[int, int]] = None,
    ):
        super().__init__(parent)
        self._duration_ms: int = duration_ms
        self._spatial_ranks: Dict[int, int] = dict(spatial_ranks or {})
        self._is_animating: bool = False
        self._anim_group: Optional[QParallelAnimationGroup] = None
        self._prev_index: Optional[int] = None
        self._target_index: Optional[int] = None

    def set_spatial_ranks(self, ranks: Dict[int, int]):
        self._spatial_ranks = dict(ranks or {})

    def _rank_of(self, index: int) -> int:
        return self._spatial_ranks.get(index, index)

    def currentIndex(self) -> int:
        if self._is_animating and self._target_index is not None:
            return self._target_index
        return super().currentIndex()

    def currentWidget(self) -> Optional[QWidget]:
        idx = self.currentIndex()
        return self.widget(idx) if 0 <= idx < self.count() else super().currentWidget()

    def _abort_active_animation(self):
        """Interrompe istantaneamente l'animazione in corso e consolida il vecchio target_index come corrente."""
        if not self._is_animating:
            return
        if self._anim_group is not None:
            try:
                self._anim_group.finished.disconnect(self._on_animation_finished)
            except Exception:
                pass
            self._anim_group.stop()
            self._anim_group.deleteLater()
            self._anim_group = None

        prev_w = self.widget(self._prev_index) if self._prev_index is not None else None
        targ_w = self.widget(self._target_index) if self._target_index is not None else None

        if self._target_index is not None and 0 <= self._target_index < self.count():
            super().setCurrentIndex(self._target_index)

        if prev_w is not None and prev_w is not targ_w:
            prev_w.hide()
            prev_w.move(0, 0)
        if targ_w is not None:
            targ_w.move(0, 0)
            targ_w.resize(self.size())
            targ_w.show()

        self._is_animating = False
        self._prev_index = None
        self._target_index = None

    def setCurrentIndex(self, new_index: int):
        if new_index < 0 or new_index >= self.count():
            return

        # Interrupt Handling: se l'utente clicca un'altra tab durante l'animazione,
        # ferma subito il gruppo corrente, consolida il target precedente e avvia la nuova transizione.
        if self._is_animating:
            if new_index == self._target_index:
                return
            self._abort_active_animation()

        cur_index = super().currentIndex()
        if new_index == cur_index:
            return

        # Se il widget non è visibile o ha larghezza nulla (es. avvio iniziale), switch istantaneo
        w = self.width()
        h = self.height()
        if not self.isVisible() or w <= 20 or h <= 20:
            super().setCurrentIndex(new_index)
            return

        cur_widget = self.widget(cur_index)
        next_widget = self.widget(new_index)
        if cur_widget is None or next_widget is None:
            super().setCurrentIndex(new_index)
            return

        cur_rank = self._rank_of(cur_index)
        new_rank = self._rank_of(new_index)
        moving_right = new_rank > cur_rank

        # Fissa le dimensioni di entrambi i widget a self.size() e porta in primo piano la pagina entrante
        cur_widget.resize(self.size())
        next_widget.resize(self.size())

        # Parallasse asimmetrica (100% entrante sopra, 35% uscente nella direzione opposta sotto)
        parallax_offset = max(1, int(w * 0.35))
        if moving_right:
            in_start = QPoint(w, 0)
            out_end = QPoint(-parallax_offset, 0)
        else:
            in_start = QPoint(-w, 0)
            out_end = QPoint(parallax_offset, 0)

        cur_widget.move(0, 0)
        next_widget.move(in_start)
        next_widget.show()
        next_widget.raise_()

        self._is_animating = True
        self._prev_index = cur_index
        self._target_index = new_index

        anim_in = QPropertyAnimation(next_widget, b"pos", self)
        anim_in.setDuration(self._duration_ms)
        anim_in.setStartValue(in_start)
        anim_in.setEndValue(QPoint(0, 0))
        anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_out = QPropertyAnimation(cur_widget, b"pos", self)
        anim_out.setDuration(self._duration_ms)
        anim_out.setStartValue(QPoint(0, 0))
        anim_out.setEndValue(out_end)
        anim_out.setEasingCurve(QEasingCurve.Type.OutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(anim_in)
        group.addAnimation(anim_out)
        group.finished.connect(self._on_animation_finished)
        self._anim_group = group

        # Emette subito currentChanged verso i listener (Top Bar / Sub Bar) per sincronizzare la pillola
        self.currentChanged.emit(new_index)
        group.start()

    def _on_animation_finished(self):
        prev_idx = self._prev_index
        targ_idx = self._target_index
        self._is_animating = False
        self._prev_index = None
        self._target_index = None

        if self._anim_group is not None:
            self._anim_group.deleteLater()
            self._anim_group = None

        prev_w = self.widget(prev_idx) if prev_idx is not None else None
        targ_w = self.widget(targ_idx) if targ_idx is not None else None

        if targ_idx is not None and 0 <= targ_idx < self.count():
            self.blockSignals(True)
            super().setCurrentIndex(targ_idx)
            self.blockSignals(False)

        if prev_w is not None and prev_w is not targ_w:
            prev_w.hide()
            prev_w.move(0, 0)
        if targ_w is not None:
            targ_w.move(0, 0)
            targ_w.resize(self.size())
            targ_w.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._is_animating:
            for idx in (self._prev_index, self._target_index):
                w = self.widget(idx) if idx is not None else None
                if w is not None:
                    w.resize(self.size())


class AnimatedSegmentedBox(QFrame):
    """
    Contenitore Segmented Control con pillola attiva scorrevole animata (280ms OutCubic).
    Sincronizzato con lo scorrimento spaziale di SlidingStackedWidget nella Top Bar e nelle Sub Bar.
    """

    def __init__(
        self,
        box_object_name: str = "TopSegmentBox",
        pill_object_name: str = "TopSlidingPill",
        duration_ms: int = 280,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName(box_object_name)
        self._duration_ms = duration_ms
        self._buttons: List[QPushButton] = []
        self._active_btn: Optional[QPushButton] = None

        self.pill = MorphologicalSlidingPill(pill_object_name, self)
        self.pill.lower()
        self.pill.hide()

        self._anim = QPropertyAnimation(self.pill, b"geometry", self)
        self._anim.setDuration(self._duration_ms)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def register_button(self, btn: QPushButton):
        btn.setProperty("has_sliding_pill", True)
        self._buttons.append(btn)
        btn.raise_()
        btn.toggled.connect(lambda checked, b=btn: self._on_btn_toggled(b, checked))

    def _on_btn_toggled(self, btn: QPushButton, checked: bool):
        if checked:
            self.slide_to_button(btn, animate=self.isVisible())

    def slide_to_button(self, btn: QPushButton, animate: bool = True):
        if btn is None:
            return
        prev_btn = self._active_btn
        self._active_btn = btn
        target_rect = btn.geometry()
        if target_rect.width() <= 4 or target_rect.height() <= 4:
            QTimer.singleShot(0, self.sync_pill_geometry)
            return

        self.pill.show()
        self.pill.lower()
        for b in self._buttons:
            b.raise_()

        if not animate or not self.isVisible() or prev_btn is None or self.pill.width() <= 4:
            if self._anim.state() == QPropertyAnimation.State.Running:
                self._anim.stop()
            self.pill.setGeometry(target_rect)
            return

        current_rect = self.pill.geometry()
        if current_rect == target_rect:
            return

        if self._anim.state() == QPropertyAnimation.State.Running:
            current_rect = self.pill.geometry()
            self._anim.stop()

        self._anim.setStartValue(current_rect)
        self._anim.setEndValue(target_rect)
        self._anim.start()

    def sync_pill_geometry(self):
        if self._active_btn is None:
            for b in self._buttons:
                if b.isChecked():
                    self._active_btn = b
                    break
        if self._active_btn is not None:
            rect = self._active_btn.geometry()
            if rect.width() > 4 and rect.height() > 4:
                if self._anim.state() != QPropertyAnimation.State.Running:
                    self.pill.setGeometry(rect)
                self.pill.show()
                self.pill.lower()
                for b in self._buttons:
                    b.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.sync_pill_geometry)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_pill_geometry()


class HubSettingsDialog(QDialog):
    """Finestra Impostazioni di FlowLocal Hub (Temi, Tipografia & Zoom, Sync Google Calendar, Sistema)."""

    def __init__(self, hub: "FlowLocalHub"):
        super().__init__(hub)
        self.hub = hub
        self.setWindowTitle("⚙️ Impostazioni FlowLocal")
        self.setMinimumWidth(560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title_lbl = _make_label("Impostazioni FlowLocal", "PageTitle")
        layout.addWidget(title_lbl)

        # 1. Sezione Aspetto & Temi
        theme_card = QFrame()
        theme_card.setObjectName("Card")
        tc_layout = QVBoxLayout(theme_card)
        tc_layout.setContentsMargins(14, 10, 14, 10)
        tc_layout.setSpacing(8)
        tc_layout.addWidget(_make_label("🎨 Tema Interfaccia", "CardTitle"))
        tc_layout.addWidget(_make_label("Scegli lo stile visivo della dashboard (Modalità Lettura resta accessibile in alto).", "PageSubtitle", wrap=True))

        self._theme_grp = QButtonGroup(self)
        self._theme_grp.setExclusive(True)

        theme_row1 = QHBoxLayout()
        theme_row1.setSpacing(6)
        for idx, (t_label, t_key) in enumerate([
            ("Obsidian", "obsidian"),
            ("Chiaro", "light"),
            ("Orb", "orb"),
            ("Dark Orb", "dark_orb"),
        ]):
            btn = QPushButton(t_label)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, idx)
            theme_row1.addWidget(btn)
        tc_layout.addLayout(theme_row1)

        theme_row2 = QHBoxLayout()
        theme_row2.setSpacing(6)
        for idx, (t_label, t_key) in enumerate([
            ("🌸 Ghibli Spring", "ghibli_spring"),
            ("👾 Pixel Chill", "pixel_chill"),
            ("⚔️ Crimson Katana", "katana"),
            ("🎯 Tactical HUD", "tactical_hud"),
        ], start=4):
            btn = QPushButton(t_label)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, idx)
            theme_row2.addWidget(btn)
        tc_layout.addLayout(theme_row2)

        theme_row3 = QHBoxLayout()
        theme_row3.setSpacing(6)
        for idx, (t_label, t_key) in enumerate([
            ("🤖 Industrial Mecha", "industrial_mecha"),
            ("🖥️ Amber CRT", "amber_crt"),
            ("🚀 Starfield Alabaster", "starfield_alabaster"),
            ("🌆 Cyber Synthwave", "cyber_synthwave"),
        ], start=8):
            btn = QPushButton(t_label)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, idx)
            theme_row3.addWidget(btn)
        tc_layout.addLayout(theme_row3)
        layout.addWidget(theme_card)

        # 1b. Sezione Tipografia & Scala Dimensione Font
        font_card = QFrame()
        font_card.setObjectName("Card")
        fc_layout = QVBoxLayout(font_card)
        fc_layout.setContentsMargins(14, 10, 14, 10)
        fc_layout.setSpacing(8)
        fc_layout.addWidget(_make_label("🔤 Tipografia & Dimensione Testo", "CardTitle"))
        self.font_subtitle_lbl = _make_label(
            "Personalizza il font della UI e dell'Editor (i blocchi codice mantengono sempre il font monospace).",
            "PageSubtitle",
            wrap=True,
        )
        fc_layout.addWidget(self.font_subtitle_lbl)

        self._font_preset_grp = QButtonGroup(self)
        self._font_preset_grp.setExclusive(True)
        self._font_preset_btns: Dict[str, QPushButton] = {}

        preset_row1 = QHBoxLayout()
        preset_row1.setSpacing(6)
        for idx, (p_key, p_lbl, p_tip) in enumerate([
            ("auto", "✨ Auto (Predefinito Tema)", FONT_PRESETS["auto"]["subtitle"]),
            ("modern_sans", "Modern Sans", FONT_PRESETS["modern_sans"]["subtitle"]),
            ("geometric", "Geometric & Clean", FONT_PRESETS["geometric"]["subtitle"]),
        ]):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setToolTip(p_tip)
            btn.setChecked(self.hub._font_preset == p_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_font_preset_selected(k))
            self._font_preset_grp.addButton(btn, idx)
            self._font_preset_btns[p_key] = btn
            preset_row1.addWidget(btn)
        fc_layout.addLayout(preset_row1)

        preset_row2 = QHBoxLayout()
        preset_row2.setSpacing(6)
        for idx, (p_key, p_lbl, p_tip) in enumerate([
            ("academic_serif", "📖 Academic / Serif", FONT_PRESETS["academic_serif"]["subtitle"]),
            ("dev_mono", "💻 Developer / Monospace", FONT_PRESETS["dev_mono"]["subtitle"]),
            ("custom", "🪟 Font di Sistema", FONT_PRESETS["custom"]["subtitle"]),
        ], start=3):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setToolTip(p_tip)
            btn.setChecked(self.hub._font_preset == p_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_font_preset_selected(k))
            self._font_preset_grp.addButton(btn, idx)
            self._font_preset_btns[p_key] = btn
            preset_row2.addWidget(btn)
        fc_layout.addLayout(preset_row2)

        # Riga QFontComboBox filtrato per qualsiasi font scalabile installato su Windows + Scala Dimensione Font
        sys_font_row = QHBoxLayout()
        sys_font_row.setSpacing(8)
        sys_lbl = QLabel("Font Windows:")
        sys_lbl.setObjectName("CardBody")
        sys_font_row.addWidget(sys_lbl)

        self.system_font_combo = QFontComboBox()
        self.system_font_combo.setMinimumHeight(34)
        self.system_font_combo.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
        if self.hub._custom_font_family:
            self.system_font_combo.blockSignals(True)
            self.system_font_combo.setCurrentFont(QFont(self.hub._custom_font_family))
            self.system_font_combo.blockSignals(False)
        self.system_font_combo.currentFontChanged.connect(self._on_system_font_changed)
        sys_font_row.addWidget(self.system_font_combo, 1)
        fc_layout.addLayout(sys_font_row)

        # Riga Zoom / Scala Dimensione Font (90%, 100%, 110%, 120% + Slider/SpinBox)
        scale_row = QHBoxLayout()
        scale_row.setSpacing(6)
        scale_lbl = QLabel("Scala Dimensione:")
        scale_lbl.setObjectName("CardBody")
        scale_row.addWidget(scale_lbl)

        self._scale_grp = QButtonGroup(self)
        self._scale_grp.setExclusive(True)
        self._scale_btns: Dict[int, QPushButton] = {}
        for idx, pct in enumerate([90, 100, 110, 120]):
            s_btn = QPushButton(f"{pct}%")
            s_btn.setObjectName("FilterPill")
            s_btn.setCheckable(True)
            s_btn.setChecked(self.hub._font_scale == pct)
            s_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            s_btn.clicked.connect(lambda _c=False, p=pct: self._on_font_scale_changed(p))
            self._scale_grp.addButton(s_btn, idx)
            self._scale_btns[pct] = s_btn
            scale_row.addWidget(s_btn)

        self.font_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.font_scale_slider.setRange(90, 120)
        self.font_scale_slider.setSingleStep(5)
        self.font_scale_slider.setPageStep(10)
        self.font_scale_slider.setValue(self.hub._font_scale)
        self.font_scale_slider.setFixedWidth(90)
        self.font_scale_slider.valueChanged.connect(self._on_font_scale_changed)
        scale_row.addWidget(self.font_scale_slider)

        self.font_scale_spin = QSpinBox()
        self.font_scale_spin.setRange(90, 120)
        self.font_scale_spin.setSingleStep(5)
        self.font_scale_spin.setSuffix("%")
        self.font_scale_spin.setMinimumHeight(32)
        self.font_scale_spin.setValue(self.hub._font_scale)
        self.font_scale_spin.valueChanged.connect(self._on_font_scale_changed)
        scale_row.addWidget(self.font_scale_spin)
        fc_layout.addLayout(scale_row)
        layout.addWidget(font_card)

        # 2. Sezione Google Calendar Sync
        gcal_card = QFrame()
        gcal_card.setObjectName("Card")
        gc_layout = QVBoxLayout(gcal_card)
        gc_layout.setContentsMargins(14, 10, 14, 10)
        gc_layout.setSpacing(8)
        gc_layout.addWidget(_make_label("🗓️ FlowTime & Google Calendar", "CardTitle"))

        self.gcal_status_lbl = QLabel("")
        self.gcal_status_lbl.setWordWrap(True)
        self.gcal_status_lbl.setObjectName("CardBody")
        gc_layout.addWidget(self.gcal_status_lbl)

        gc_btns = QHBoxLayout()
        gc_btns.setSpacing(8)
        self.gcal_connect_btn = _make_button("📅 Connetti Google Calendar", "primary")
        self.gcal_connect_btn.clicked.connect(self._on_connect_gcal)
        gc_btns.addWidget(self.gcal_connect_btn)

        self.gcal_sync_btn = _make_button("🔄 Sincronizza Ora")
        self.gcal_sync_btn.clicked.connect(self._on_sync_gcal)
        gc_btns.addWidget(self.gcal_sync_btn)
        gc_btns.addStretch(1)
        gc_layout.addLayout(gc_btns)
        layout.addWidget(gcal_card)
        self._refresh_gcal_status()

        # 3. Sezione Motore & Scorciatoie
        sys_card = QFrame()
        sys_card.setObjectName("Card")
        sc_layout = QVBoxLayout(sys_card)
        sc_layout.setContentsMargins(14, 10, 14, 10)
        sc_layout.setSpacing(6)
        sc_layout.addWidget(_make_label("⚡ Motore AI, Audio & Scorciatoie", "CardTitle"))
        hw_str = "● GPU CUDA attiva (Whisper float16 + Ollama)" if GPU_AVAILABLE else "● CPU Fast (Whisper int8)"
        hw_lbl = QLabel(hw_str)
        hw_lbl.setStyleSheet(f"color: {'#10B981' if GPU_AVAILABLE else '#F59E0B'}; font-size: 12px; font-weight: 600; background: transparent;")
        sc_layout.addWidget(hw_lbl)

        self.auto_duck_checkbox = QCheckBox("Abbassa automaticamente la musica durante la dettatura (Auto-Ducking)")
        self.auto_duck_checkbox.setChecked(audio_ducker.is_enabled())
        self.auto_duck_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_duck_checkbox.setStyleSheet(
            "QCheckBox { font-size: 12px; font-weight: 600; padding: 4px 0px; background: transparent; }"
        )
        self.auto_duck_checkbox.toggled.connect(self._on_auto_duck_toggled)
        sc_layout.addWidget(self.auto_duck_checkbox)

        hint_lbl = QLabel("• Push-to-Talk: Shift Destro (doppio tocco = Mani Libere)\n• Scorciatoia secondaria: F8\n• Assistente Whis: pronuncia \"Whis...\" durante la dettatura")
        hint_lbl.setWordWrap(True)
        hint_lbl.setObjectName("PageSubtitle")
        sc_layout.addWidget(hint_lbl)
        layout.addWidget(sys_card)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = _make_button("Chiudi", "primary")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _on_auto_duck_toggled(self, checked: bool):
        audio_ducker.set_enabled(bool(checked))
        if hasattr(self.hub, "styles_auto_duck_chk") and self.hub.styles_auto_duck_chk is not None:
            self.hub.styles_auto_duck_chk.blockSignals(True)
            self.hub.styles_auto_duck_chk.setChecked(bool(checked))
            self.hub.styles_auto_duck_chk.blockSignals(False)

    def _on_theme_pill_clicked(self, theme_key: str):
        self.hub._set_theme(theme_key)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_font_preset_selected(self, preset_key: str):
        if preset_key == "custom":
            fam = self.system_font_combo.currentFont().family()
            self.hub._set_custom_font_family(fam)
        else:
            self.hub._set_font_preset(preset_key)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_system_font_changed(self, qfont: QFont):
        fam = qfont.family()
        if "custom" in self._font_preset_btns:
            self._font_preset_btns["custom"].setChecked(True)
        self.hub._set_custom_font_family(fam)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_font_scale_changed(self, val: int):
        pct = max(90, min(120, int(val)))
        self.font_scale_slider.blockSignals(True)
        self.font_scale_slider.setValue(pct)
        self.font_scale_slider.blockSignals(False)

        self.font_scale_spin.blockSignals(True)
        self.font_scale_spin.setValue(pct)
        self.font_scale_spin.blockSignals(False)

        if pct in self._scale_btns:
            self._scale_btns[pct].setChecked(True)
        else:
            checked_btn = self._scale_grp.checkedButton()
            if checked_btn is not None:
                self._scale_grp.setExclusive(False)
                checked_btn.setChecked(False)
                self._scale_grp.setExclusive(True)

        self.hub._set_font_scale(pct)
        self.setStyleSheet(self.hub.styleSheet())

    def _refresh_gcal_status(self):
        from core.calendar_sync import calendar_manager
        if calendar_manager.has_token_file():
            self.gcal_status_lbl.setText("🟢 Google Calendar collegato: la sincronizzazione bidirezionale con FlowTime avviene in background, mentre Whis legge istantaneamente da SQLite.")
            self.gcal_connect_btn.setText("📅 Riconnetti Google")
            self.gcal_sync_btn.setEnabled(True)
        elif calendar_manager.has_credentials_file():
            self.gcal_status_lbl.setText("🟡 File credentials.json rilevato: clicca su 'Connetti Google Calendar' per autorizzare l'accesso.")
            self.gcal_sync_btn.setEnabled(False)
        else:
            self.gcal_status_lbl.setText("⚪ Modalità 100% Locale attiva: FlowTime e Whis funzionano istantaneamente offline. Aggiungi credentials.json per abilitare anche Google Calendar.")
            self.gcal_sync_btn.setEnabled(False)

    def _on_connect_gcal(self):
        self.hub._on_connect_calendar_clicked()
        QTimer.singleShot(800, self._refresh_gcal_status)

    def _on_sync_gcal(self):
        self.hub._on_ft_sync_google_clicked()
        self.gcal_status_lbl.setText("⏳ Sincronizzazione con Google Calendar avviata in background...")


FLOWTIME_COLOR_PALETTE = [
    ("#6366F1", "Indigo"),
    ("#38BDF8", "Ciano"),
    ("#10B981", "Smeraldo"),
    ("#F59E0B", "Ambra"),
    ("#EC4899", "Rosa"),
    ("#EF4444", "Rosso"),
]


class LinkEventNoteDialog(QDialog):
    """HUD di ricerca e collegamento rapido tra note di FlowNotes e impegni di FlowTime."""

    open_note_requested = pyqtSignal(int)

    def __init__(
        self,
        event_id: Optional[int],
        event_title: str,
        preselected_ids: Optional[List[int]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.event_id = event_id
        self._selected_ids: List[int] = []
        if event_id is not None:
            self._selected_ids = [int(n["id"]) for n in db.get_notes_for_event(event_id)]
        elif preselected_ids:
            self._selected_ids = [int(x) for x in preselected_ids]
        self._selected_set = set(self._selected_ids)

        self.setWindowTitle("🔗 Collega Note a FlowTime")
        self.setMinimumSize(560, 460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(_make_label(f"🔗 {event_title or 'Impegno FlowTime'}", "CardTitle", wrap=True), 1)
        self.count_badge = _make_label("", "BadgeAccent")
        top_row.addWidget(self.count_badge)
        layout.addLayout(top_row)

        layout.addWidget(
            _make_label(
                "Cerca qualsiasi nota, sbobina o slide di FlowNotes e collegala all'impegno selezionato:",
                "PageSubtitle",
                wrap=True,
            )
        )

        # Barra di ricerca fuori dalla QScrollArea (regola Qt6 UI)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 Cerca per titolo, materia o contenuto...")
        self.search_edit.setMinimumHeight(38)
        self.search_edit.textChanged.connect(self._populate_list)
        layout.addWidget(self.search_edit)

        self.notes_list = ScrollList("Nessuna nota corrispondente trovata in FlowNotes.")
        self.notes_list.setMinimumHeight(290)
        layout.addWidget(self.notes_list, 1)

        self._all_notes = [
            n for n in db.get_all_study_notes()
            if (n.get("note_type") or "").lower() != "flashcard"
        ]
        self._populate_list("")

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.done_btn = _make_button("✓ Fatto", "primary")
        self.done_btn.clicked.connect(self.accept)
        btns.addWidget(self.done_btn)
        layout.addLayout(btns)

    @property
    def selected_note_ids(self) -> List[int]:
        return sorted(self._selected_set)

    def _update_count_badge(self):
        cnt = len(self._selected_set)
        self.count_badge.setText(f"{cnt} collegat{'a' if cnt == 1 else 'e'}")

    def _populate_list(self, query: str):
        self.notes_list.clear()
        self._update_count_badge()
        q = (query or "").strip().lower()
        matches = []
        for n in self._all_notes:
            title = (n.get("title") or "Senza titolo").strip()
            subj = (n.get("subject") or "Generale").strip()
            content = (n.get("content") or "")[:300].lower()
            if q and q not in title.lower() and q not in subj.lower() and q not in content:
                continue
            matches.append(n)

        if not matches:
            self.notes_list.show_empty("Nessuna nota trovata con questi criteri.")
            return

        # Mostra prima le note già collegate
        matches.sort(key=lambda item: (0 if int(item["id"]) in self._selected_set else 1))
        for note in matches:
            nid = int(note["id"])
            card, c_lay = _make_card()
            row = QHBoxLayout()
            row.setSpacing(10)

            info_col = QVBoxLayout()
            info_col.setSpacing(3)
            meta = QHBoxLayout()
            meta.setSpacing(6)
            meta.addWidget(_make_label(note.get("subject") or "Generale", "Badge"))
            type_lbl = (note.get("note_type") or "Appunto").capitalize()
            meta.addWidget(_make_label(f"{type_lbl} · {_format_timestamp(note.get('created_at'))}", "NoteMeta"))
            meta.addStretch(1)
            info_col.addLayout(meta)

            title_lbl = _make_label(note.get("title") or "Senza titolo", "CardTitle", wrap=True)
            info_col.addWidget(title_lbl)
            row.addLayout(info_col, 1)

            btn_col = QHBoxLayout()
            btn_col.setSpacing(6)
            is_linked = nid in self._selected_set
            toggle_btn = _make_button("✅ Collegata" if is_linked else "🔗 Collega", "primary" if is_linked else None)
            toggle_btn.clicked.connect(lambda _c=False, xid=nid: self._toggle_link(xid))
            btn_col.addWidget(toggle_btn)

            open_btn = _make_button("📖 Apri")
            open_btn.clicked.connect(lambda _c=False, xid=nid: self._on_open_note(xid))
            btn_col.addWidget(open_btn)
            row.addLayout(btn_col)

            c_lay.addLayout(row)
            self.notes_list.add_card(card)

    def _toggle_link(self, note_id: int):
        if note_id in self._selected_set:
            self._selected_set.remove(note_id)
            if self.event_id is not None:
                db.unlink_note_from_event(self.event_id, note_id)
        else:
            self._selected_set.add(note_id)
            if self.event_id is not None:
                db.link_note_to_event(self.event_id, note_id, link_type="manual")
        self._populate_list(self.search_edit.text())

    def _on_open_note(self, note_id: int):
        self.open_note_requested.emit(int(note_id))
        self.accept()


class TimelineQuickAddDialog(QDialog):
    """HUD rapido aperto cliccando uno spazio vuoto sulla timeline per aggiungere Evento o To-Do."""

    def __init__(self, date_str: str, start_time_str: str = "09:00", parent=None):
        super().__init__(parent)
        self.date_str = date_str
        self._item_type = "event"
        self._selected_color = FLOWTIME_COLOR_PALETTE[0][0]
        self._linked_note_ids: List[int] = []
        self.created_event: Optional[Dict[str, Any]] = None

        self.setWindowTitle("➕ Nuovo Impegno o Evento")
        self.setMinimumWidth(460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(11)

        try:
            d_obj = dt_date.fromisoformat(date_str)
            day_pretty = d_obj.strftime("%d/%m/%Y")
        except Exception:
            day_pretty = date_str

        layout.addWidget(_make_label(f"📅 {day_pretty} · Aggiungi a FlowTime", "CardTitle"))

        # Switch Evento / To-Do
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self.event_mode_btn = QPushButton(f"📅 Evento ({start_time_str})")
        self.event_mode_btn.setObjectName("FTSegmentBtn")
        self.event_mode_btn.setCheckable(True)
        self.event_mode_btn.setChecked(True)
        self.event_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.todo_mode_btn = QPushButton("☑️ Impegno / To-Do (Senza orario)")
        self.todo_mode_btn.setObjectName("FTSegmentBtn")
        self.todo_mode_btn.setCheckable(True)
        self.todo_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._mode_group.addButton(self.event_mode_btn, 0)
        self._mode_group.addButton(self.todo_mode_btn, 1)
        self.event_mode_btn.toggled.connect(lambda checked: self._set_item_type("event" if checked else "todo"))
        mode_row.addWidget(self.event_mode_btn, 1)
        mode_row.addWidget(self.todo_mode_btn, 1)
        layout.addLayout(mode_row)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Titolo evento (es. Lezione Robotica)...")
        self.title_edit.setMinimumHeight(36)
        layout.addWidget(self.title_edit)

        self.time_row_widget = QWidget()
        self.time_row_widget.setStyleSheet("background: transparent;")
        tr = QHBoxLayout(self.time_row_widget)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(8)
        self.start_edit = QLineEdit(start_time_str or "09:00")
        self.start_edit.setFixedWidth(74)
        self.start_edit.setMinimumHeight(36)
        self.start_edit.setPlaceholderText("HH:MM")
        tr.addWidget(self.start_edit)

        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumHeight(36)
        for label, mins in [("30 min", 30), ("45 min", 45), ("1 ora", 60), ("1h 30m", 90), ("2 ore", 120), ("3 ore", 180)]:
            self.duration_combo.addItem(label, mins)
        self.duration_combo.setCurrentIndex(2)
        tr.addWidget(self.duration_combo)

        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("Luogo / Aula (opz.)")
        self.location_edit.setMinimumHeight(36)
        tr.addWidget(self.location_edit, 1)
        layout.addWidget(self.time_row_widget)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self.category_combo = QComboBox()
        self.category_combo.setMinimumHeight(36)
        for cat in ["Personale", "Università", "Lavoro", "Esame", "Studio"]:
            self.category_combo.addItem(cat)
        meta_row.addWidget(self.category_combo)

        self.subject_edit = QLineEdit()
        self.subject_edit.setPlaceholderText("Materia / Tag (opz.)")
        self.subject_edit.setMinimumHeight(36)
        meta_row.addWidget(self.subject_edit, 1)
        layout.addLayout(meta_row)

        # Colori + Ripetizione
        style_row = QHBoxLayout()
        style_row.setSpacing(8)
        style_row.addWidget(_make_label("Colore:", "PageSubtitle"))
        self._color_buttons: Dict[str, QPushButton] = {}
        for hex_col, name in FLOWTIME_COLOR_PALETTE:
            btn = QPushButton("")
            btn.setObjectName("FTColorDot")
            btn.setToolTip(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, hc=hex_col: self._set_color(hc))
            self._color_buttons[hex_col] = btn
            style_row.addWidget(btn)
        style_row.addSpacing(8)

        self.recurrence_combo = QComboBox()
        self.recurrence_combo.setMinimumHeight(36)
        for label, val in [("Non si ripete", "none"), ("Ogni giorno", "daily"), ("Ogni settimana", "weekly"), ("Ogni mese", "monthly")]:
            self.recurrence_combo.addItem(label, val)
        style_row.addWidget(self.recurrence_combo, 1)
        layout.addLayout(style_row)
        self._refresh_color_buttons()

        # Collega nota
        note_row = QHBoxLayout()
        note_row.setSpacing(8)
        self.linked_badge = _make_label("Nessuna nota collegata", "Badge")
        note_row.addWidget(self.linked_badge, 1)
        self.link_notes_btn = _make_button("🔗 Collega Nota")
        self.link_notes_btn.clicked.connect(self._on_open_note_hud)
        note_row.addWidget(self.link_notes_btn)
        layout.addLayout(note_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        save_btn = _make_button("💾 Salva in FlowTime", "primary")
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(save_btn)
        layout.addLayout(btns)
        self.title_edit.setFocus()

    def _set_item_type(self, mode: str):
        self._item_type = mode
        is_todo = mode == "todo"
        self.time_row_widget.setVisible(not is_todo)
        self.title_edit.setPlaceholderText(
            "Impegno senza orario (es. Innaffiare le piante)..."
            if is_todo
            else "Titolo evento (es. Lezione Robotica)..."
        )

    def _set_color(self, hex_col: str):
        self._selected_color = hex_col
        self._refresh_color_buttons()

    def _refresh_color_buttons(self):
        for hc, btn in self._color_buttons.items():
            border = "2px solid #FFFFFF" if hc == self._selected_color else "1px solid rgba(148,163,184,0.35)"
            btn.setStyleSheet(f"background-color: {hc}; border: {border}; border-radius: 10px;")

    def _on_open_note_hud(self):
        title = self.title_edit.text().strip() or "Nuovo impegno FlowTime"
        dlg = LinkEventNoteDialog(None, title, preselected_ids=self._linked_note_ids, parent=self)
        dlg.exec()
        self._linked_note_ids = list(dlg.selected_note_ids)
        cnt = len(self._linked_note_ids)
        self.linked_badge.setText("Nessuna nota collegata" if cnt == 0 else f"🔗 {cnt} not{'a' if cnt == 1 else 'e'} collegat{'a' if cnt == 1 else 'e'}")

    def _on_save(self):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Titolo mancante", "Inserisci un titolo per salvare in FlowTime.")
            self.title_edit.setFocus()
            return
        from core.calendar_sync import calendar_manager
        is_todo = self._item_type == "todo"
        created = calendar_manager.create_event(
            summary=title,
            date_str=self.date_str,
            start_time_str="00:00" if is_todo else (self.start_edit.text().strip() or "09:00"),
            duration_minutes=0 if is_todo else int(self.duration_combo.currentData() or 60),
            location="" if is_todo else self.location_edit.text().strip(),
            category=self.category_combo.currentText(),
            subject=self.subject_edit.text().strip(),
            color=self._selected_color,
            push_to_google=not is_todo,
            item_type=self._item_type,
            recurrence=str(self.recurrence_combo.currentData() or "none"),
        )
        for nid in self._linked_note_ids:
            db.link_note_to_event(created["id"], int(nid), link_type="manual")
        self.created_event = created
        self.accept()


class FlowTimeCalendarWidget(QCalendarWidget):
    """Calendario mensile con celle a sfondo neutro, giorno selezionato evidenziato e oggi cerchiato di rosso."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._activity_map: Dict[str, Dict[str, int]] = {}
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setGridVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setLocale(QLocale(QLocale.Language.Italian, QLocale.Country.Italy))

    def set_activity_data(
        self,
        activity_map: Dict[str, Dict[str, int]],
        is_dark: bool,
        reading_mode: bool = False,
    ):
        self._activity_map = dict(activity_map or {})
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self.updateCells()

    def paintCell(self, painter: QPainter, rect: QRect, date: QDate):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._is_dark:
            bg_neutral = QColor("#18130F") if self._reading_mode else QColor("#0D1322")
            text_main = QColor("#E6D7C3") if self._reading_mode else QColor("#E2E8F0")
            text_weekend = QColor("#A8957E") if self._reading_mode else QColor("#94A3B8")
            text_other_month = QColor("#574536") if self._reading_mode else QColor("#475569")
            sel_fill = QColor("#D97706") if self._reading_mode else QColor("#6366F1")
            sel_border = QColor("#FBBF24") if self._reading_mode else QColor("#818CF8")
        else:
            bg_neutral = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            text_main = QColor("#2A1F14") if self._reading_mode else QColor("#1E293B")
            text_weekend = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            text_other_month = QColor("#C4B5A0") if self._reading_mode else QColor("#CBD5E1")
            sel_fill = QColor("#B45309") if self._reading_mode else QColor("#4F46E5")
            sel_border = QColor("#D97706") if self._reading_mode else QColor("#6366F1")

        # 1. Sfondo neutro uniforme per ogni giorno
        painter.fillRect(rect, bg_neutral)

        is_selected = date == self.selectedDate()
        is_today = date == QDate.currentDate()
        in_month = date.year() == self.yearShown() and date.month() == self.monthShown()

        # 2. Giorno selezionato evidenziato chiaramente
        if is_selected:
            pill_rect = QRectF(rect).adjusted(4.0, 3.0, -4.0, -3.0)
            painter.setPen(QPen(sel_border, 1.4))
            painter.setBrush(QBrush(sel_fill))
            painter.drawRoundedRect(pill_rect, 8.0, 8.0)

        # 3. Numero del giorno (oggi in rosso quando non selezionato, senza cerchio)
        font = QFont(painter.font())
        font.setWeight(QFont.Weight.Bold if (is_selected or is_today) else QFont.Weight.Medium)
        painter.setFont(font)

        if is_selected:
            txt_col = QColor("#FFFFFF")
        elif not in_month:
            txt_col = text_other_month
        elif is_today:
            txt_col = QColor("#EF4444")
        elif date.dayOfWeek() in (6, 7):
            txt_col = text_weekend
        else:
            txt_col = text_main

        painter.setPen(txt_col)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(date.day()))

        # 5. Puntini discreti di attività in basso (senza colorare lo sfondo della cella)
        counts = self._activity_map.get(date.toString("yyyy-MM-dd")) or {}
        ev_cnt = counts.get("events", 0)
        note_cnt = counts.get("notes", 0)
        if ev_cnt > 0 or note_cnt > 0:
            dots = []
            if ev_cnt > 0:
                dots.append(QColor("#FFFFFF") if is_selected else QColor("#38BDF8"))
            if note_cnt > 0:
                dots.append(QColor("#A7F3D0") if is_selected else QColor("#10B981"))
            dot_y = float(rect.bottom()) - 3.5
            cx = float(rect.center().x())
            spacing = 5.5
            start_x = cx - ((len(dots) - 1) * spacing) / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            for i, dc in enumerate(dots):
                painter.setBrush(QBrush(dc))
                painter.drawEllipse(QPointF(start_x + i * spacing, dot_y), 1.7, 1.7)

        painter.restore()


class _DayTimelineCanvas(QWidget):
    """Canvas 00:00–23:59 a piena larghezza per la timeline giornaliera interattiva."""

    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)

    HOUR_HEIGHT = 54
    TOP_PAD = 12
    BOTTOM_PAD = 16
    AXIS_X = 52

    def __init__(self, parent=None):
        super().__init__(parent)
        self._date_str: str = dt_date.today().isoformat()
        self._events: List[Dict[str, Any]] = []
        self._notes_by_event: Dict[int, List[Dict[str, Any]]] = {}
        self._selected_event_id: Optional[int] = None
        self._placed_blocks: List[Dict[str, Any]] = []
        self._hit_rects: List[tuple] = []
        self._note_hit_rects: List[tuple] = []
        self._hover_time_min: Optional[int] = None
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setMouseTracking(True)
        self.setFixedHeight(24 * self.HOUR_HEIGHT + self.TOP_PAD + self.BOTTOM_PAD)

    @staticmethod
    def _parse_hhmm(time_str: str) -> Optional[int]:
        raw = (time_str or "").strip()
        if ":" not in raw:
            return None
        try:
            hh, mm = raw[:5].split(":")
            h_i = int(hh)
            m_i = int(mm)
            if h_i == 24 and m_i == 0:
                return 1440
            if 0 <= h_i <= 23 and 0 <= m_i <= 59:
                return h_i * 60 + m_i
        except Exception:
            return None
        return None

    def _y_to_snapped_hhmm(self, y: float) -> tuple:
        raw_mins = ((y - float(self.TOP_PAD)) / float(self.HOUR_HEIGHT)) * 60.0
        snapped = int(round(raw_mins / 30.0) * 30)
        snapped = max(0, min(1410, snapped))
        return snapped, f"{snapped // 60:02d}:{snapped % 60:02d}"

    def set_data(
        self,
        date_str: str,
        timed_events: List[Dict[str, Any]],
        is_dark: bool,
        reading_mode: bool = False,
        notes_by_event: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        selected_event_id: Optional[int] = None,
    ):
        self._date_str = date_str or dt_date.today().isoformat()
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self._notes_by_event = notes_by_event or {}
        self._selected_event_id = selected_event_id

        items = []
        for ev in timed_events:
            s_min = self._parse_hhmm(ev.get("start_time") or "")
            if s_min is None:
                s_min = 9 * 60
            e_min = self._parse_hhmm(ev.get("end_time") or "")
            if e_min is None or e_min <= s_min:
                e_min = min(1440, s_min + 60)
            vis_end = max(s_min + 34, e_min)
            items.append({"ev": ev, "start": s_min, "end": e_min, "vis_end": vis_end})

        items.sort(key=lambda x: (x["start"], x["vis_end"]))

        self._placed_blocks = []
        clusters: List[List[Dict[str, Any]]] = []
        cur_cluster: List[Dict[str, Any]] = []
        cluster_end = -1
        for it in items:
            if not cur_cluster or it["start"] < cluster_end:
                cur_cluster.append(it)
                cluster_end = max(cluster_end, it["vis_end"])
            else:
                clusters.append(cur_cluster)
                cur_cluster = [it]
                cluster_end = it["vis_end"]
        if cur_cluster:
            clusters.append(cur_cluster)

        for cl in clusters:
            lane_ends: List[int] = []
            for it in cl:
                placed_lane = None
                for li, lend in enumerate(lane_ends):
                    if it["start"] >= lend:
                        placed_lane = li
                        lane_ends[li] = it["vis_end"]
                        break
                if placed_lane is None:
                    placed_lane = len(lane_ends)
                    lane_ends.append(it["vis_end"])
                it["lane"] = placed_lane
            total_lanes = max(1, len(lane_ends))
            for it in cl:
                it["lanes"] = total_lanes
                self._placed_blocks.append(it)

        self._events = timed_events
        self.update()

    def current_time_y(self) -> float:
        now = datetime.now()
        now_min = now.hour * 60.0 + now.minute + now.second / 60.0
        return float(self.TOP_PAD) + (now_min / 60.0) * float(self.HOUR_HEIGHT)

    def first_focus_y(self) -> float:
        if self._date_str == dt_date.today().isoformat():
            return self.current_time_y()
        if self._placed_blocks:
            return float(self.TOP_PAD) + (self._placed_blocks[0]["start"] / 60.0) * float(self.HOUR_HEIGHT)
        return float(self.TOP_PAD) + 8.0 * float(self.HOUR_HEIGHT)

    def leaveEvent(self, event):
        if self._hover_time_min is not None:
            self._hover_time_min = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        if self._is_dark:
            bg_col = QColor("#18130F") if self._reading_mode else QColor("#0B0F1A")
            line_col = QColor(245, 158, 11, 30) if self._reading_mode else QColor(255, 255, 255, 22)
            half_col = QColor(245, 158, 11, 14) if self._reading_mode else QColor(255, 255, 255, 10)
            axis_txt = QColor("#A8957E") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#F5EBE0") if self._reading_mode else QColor("#F1F5F9")
            sub_col = QColor("#D6C5B0") if self._reading_mode else QColor("#94A3B8")
            pill_bg = QColor(99, 102, 241, 55)
            pill_txt = QColor("#C7D2FE")
        else:
            bg_col = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            line_col = QColor(180, 83, 9, 32) if self._reading_mode else QColor("#E2E8F0")
            half_col = QColor(180, 83, 9, 16) if self._reading_mode else QColor("#F1F5F9")
            axis_txt = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#1E293B")
            sub_col = QColor("#475569")
            pill_bg = QColor(99, 102, 241, 28)
            pill_txt = QColor("#4338CA")

        p.fillRect(self.rect(), bg_col)

        axis_font = QFont(p.font())
        axis_font.setPointSize(8)
        axis_font.setWeight(QFont.Weight.Medium)
        p.setFont(axis_font)

        left_x = float(self.AXIS_X)
        right_x = w - 8.0

        for hr in range(25):
            y = float(self.TOP_PAD) + hr * float(self.HOUR_HEIGHT)
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(left_x, y), QPointF(right_x, y))
            if hr < 24:
                p.setPen(axis_txt)
                lbl_rect = QRectF(2.0, y - 8.0, left_x - 10.0, 16.0)
                p.drawText(lbl_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{hr:02d}:00")
                hy = y + float(self.HOUR_HEIGHT) / 2.0
                p.setPen(QPen(half_col, 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(left_x, hy), QPointF(right_x, hy))

        p.setPen(QPen(line_col, 1.0))
        p.drawLine(QPointF(left_x, float(self.TOP_PAD)), QPointF(left_x, h - float(self.BOTTOM_PAD)))

        # Indicatore slot vuoto al passaggio del mouse
        if self._hover_time_min is not None:
            yh = float(self.TOP_PAD) + (self._hover_time_min / 60.0) * float(self.HOUR_HEIGHT)
            slot_h = float(self.HOUR_HEIGHT) / 2.0
            slot_rect = QRectF(left_x + 4.0, yh, max(40.0, right_x - left_x - 8.0), slot_h)
            p.setPen(QPen(QColor(99, 102, 241, 120), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(99, 102, 241, 18)))
            p.drawRoundedRect(slot_rect, 6.0, 6.0)
            p.setFont(axis_font)
            p.setPen(QColor("#818CF8"))
            p.drawText(
                slot_rect.adjusted(10.0, 0.0, -8.0, 0.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+ Aggiungi alle {self._hover_time_min // 60:02d}:{self._hover_time_min % 60:02d} (Evento o To-Do)",
            )

        # Blocchi evento
        self._hit_rects = []
        self._note_hit_rects = []
        avail_w = max(60.0, right_x - (left_x + 6.0))
        block_font_t = QFont(p.font())
        block_font_t.setPointSize(9)
        block_font_t.setWeight(QFont.Weight.DemiBold)
        block_font_m = QFont(p.font())
        block_font_m.setPointSize(8)
        note_font = QFont(p.font())
        note_font.setPointSize(8)
        note_font.setWeight(QFont.Weight.DemiBold)

        for item in self._placed_blocks:
            ev = item["ev"]
            ev_id = ev.get("id")
            y_top = float(self.TOP_PAD) + (item["start"] / 60.0) * float(self.HOUR_HEIGHT) + 1.0
            y_bot = float(self.TOP_PAD) + (item["vis_end"] / 60.0) * float(self.HOUR_HEIGHT) - 1.0
            bh = max(24.0, y_bot - y_top)

            lanes = item["lanes"]
            lane = item["lane"]
            gap = 5.0
            cw = (avail_w - gap * (lanes - 1)) / lanes
            bx = left_x + 6.0 + lane * (cw + gap)
            b_rect = QRectF(bx, y_top, cw, bh)
            self._hit_rects.append((b_rect, ev))

            accent = QColor(ev.get("color") or "#6366F1")
            if not accent.isValid():
                accent = QColor("#6366F1")
            is_selected = self._selected_event_id is not None and ev_id == self._selected_event_id

            fill_c = QColor(accent)
            fill_c.setAlpha(68 if is_selected else (48 if self._is_dark else 34))
            border_c = QColor(accent)
            border_c.setAlpha(240 if is_selected else (160 if self._is_dark else 140))

            p.setPen(QPen(border_c, 2.0 if is_selected else 1.0))
            p.setBrush(QBrush(fill_c))
            p.drawRoundedRect(b_rect, 8.0, 8.0)

            bar_rect = QRectF(bx, y_top + 1.5, 4.0, max(8.0, bh - 3.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(accent))
            p.drawRoundedRect(bar_rect, 2.0, 2.0)

            pad_in = 10.0
            inner_w = max(20.0, cw - pad_in - 8.0)
            summary = ev.get("summary") or "(Senza titolo)"
            time_txt = ev.get("start_time") or ""
            end_txt = ev.get("end_time") or ""
            time_span = f"{time_txt} - {end_txt}".strip(" -") if time_txt else ""

            meta_bits = [b for b in [time_span, ev.get("category") or "", ev.get("subject") or ""] if b]
            if ev.get("location"):
                meta_bits.append(f"📍 {ev['location']}")
            if (ev.get("recurrence") or "none") != "none":
                rec_map = {"daily": "🔁 Giorno", "weekly": "🔁 Sett.", "monthly": "🔁 Mese"}
                meta_bits.append(rec_map.get(ev.get("recurrence"), "🔁"))
            if ev.get("sync_status") == "synced":
                meta_bits.append("☁️ Google")

            linked_notes = self._notes_by_event.get(ev_id) or []

            if bh >= 42.0:
                p.setFont(block_font_m)
                p.setPen(sub_col)
                p.drawText(
                    QRectF(bx + pad_in, y_top + 4.0, inner_w, 15.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    QFontMetrics(block_font_m).elidedText(" · ".join(meta_bits), Qt.TextElideMode.ElideRight, int(inner_w)),
                )

                notes_row_h = 22.0 if (linked_notes and bh >= 66.0) else 0.0
                p.setFont(block_font_t)
                p.setPen(title_col)
                title_h = max(16.0, bh - 24.0 - notes_row_h)
                p.drawText(
                    QRectF(bx + pad_in, y_top + 20.0, inner_w, title_h),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                    summary,
                )

                if linked_notes and bh >= 66.0:
                    px = bx + pad_in
                    py = y_bot - 22.0
                    fm_n = QFontMetrics(note_font)
                    p.setFont(note_font)
                    for note in linked_notes[:3]:
                        n_title = (note.get("title") or "Nota").strip()
                        pill_label = f"📓 {fm_n.elidedText(n_title, Qt.TextElideMode.ElideRight, 130)}"
                        pw = min(inner_w - (px - (bx + pad_in)), float(fm_n.horizontalAdvance(pill_label) + 14))
                        if pw < 52.0:
                            break
                        n_rect = QRectF(px, py, pw, 18.0)
                        p.setPen(QPen(QColor(99, 102, 241, 130), 1.0))
                        p.setBrush(QBrush(pill_bg))
                        p.drawRoundedRect(n_rect, 6.0, 6.0)
                        p.setPen(pill_txt)
                        p.drawText(n_rect, Qt.AlignmentFlag.AlignCenter, pill_label)
                        self._note_hit_rects.append((n_rect, int(note["id"]), n_title))
                        px += pw + 6.0
            else:
                p.setFont(block_font_t)
                p.setPen(title_col)
                fm_t = QFontMetrics(block_font_t)
                suffix = f" (🔗{len(linked_notes)})" if linked_notes else ""
                elided = fm_t.elidedText(
                    f"{time_txt} · {summary}{suffix}".strip(" ·"), Qt.TextElideMode.ElideRight, int(inner_w)
                )
                p.drawText(
                    QRectF(bx + pad_in, y_top + 2.0, inner_w, bh - 4.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    elided,
                )

        # Linea rossa ora corrente
        is_today = self._date_str == dt_date.today().isoformat()
        y_now = self.current_time_y()
        red_col = QColor("#EF4444") if is_today else QColor(239, 68, 68, 125)
        pen_style = Qt.PenStyle.SolidLine if is_today else Qt.PenStyle.DashLine
        p.setPen(QPen(red_col, 2.0 if is_today else 1.4, pen_style))
        p.drawLine(QPointF(left_x, y_now), QPointF(right_x, y_now))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(red_col))
        p.drawEllipse(QPointF(left_x, y_now), 4.0 if is_today else 3.0, 4.0 if is_today else 3.0)

        now_txt = datetime.now().strftime("%H:%M")
        pill_r = QRectF(3.0, y_now - 8.5, left_x - 8.0, 17.0)
        p.setBrush(QBrush(QColor("#EF4444") if is_today else QColor(239, 68, 68, 180)))
        p.drawRoundedRect(pill_r, 4.0, 4.0)
        pill_font = QFont(p.font())
        pill_font.setPointSize(7)
        pill_font.setWeight(QFont.Weight.Bold)
        p.setFont(pill_font)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, now_txt)

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())
        for nr, _nid, ntitle in reversed(self._note_hit_rects):
            if nr.contains(pos):
                if self._hover_time_min is not None:
                    self._hover_time_min = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(f"Apri nota '{ntitle}' in FlowNotes")
                return
        for r, ev in reversed(self._hit_rects):
            if r.contains(pos):
                if self._hover_time_min is not None:
                    self._hover_time_min = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                loc = f"\n📍 {ev['location']}" if ev.get("location") else ""
                self.setToolTip(f"{ev.get('time', '')} · {ev.get('summary', '')}{loc}\n(Clicca per modificare o collegare note)")
                return
        if float(self.TOP_PAD) <= pos.y() <= float(self.height() - self.BOTTOM_PAD) and pos.x() >= float(self.AXIS_X):
            snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Clicca per aggiungere un Evento o To-Do alle {hhmm}")
            if self._hover_time_min != snapped:
                self._hover_time_min = snapped
                self.update()
            return
        if self._hover_time_min is not None:
            self._hover_time_min = None
            self.update()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = QPointF(event.position())
            for nr, nid, _ntitle in reversed(self._note_hit_rects):
                if nr.contains(pos):
                    self.note_clicked.emit(nid)
                    return
            for r, ev in reversed(self._hit_rects):
                if r.contains(pos):
                    self.event_clicked.emit(ev)
                    return
            _snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.empty_slot_clicked.emit(self._date_str, hhmm)
            return
        super().mousePressEvent(event)


class _ClickableLabel(QLabel):
    """QLabel word-wrappable cliccabile per mostrare titoli To-Do lunghi su più righe senza tagli."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


class FlowTimeTodoSidebar(QFrame):
    """Colonna laterale destra (~208px) dedicata a To-Do senza orario e Note, raggruppati per giornata."""

    date_selected = pyqtSignal(str)
    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    todo_toggled = pyqtSignal(int)
    add_todo_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFixedWidth(208)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._selected_date_str: str = dt_date.today().isoformat()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 10, 9, 8)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title_lbl = _make_label("☑️ Impegni & Note", "CardTitle")
        title_lbl.setStyleSheet("font-size: 13px; font-weight: 700; background: transparent;")
        hdr.addWidget(title_lbl, 1)

        add_btn = _make_button("+ To-Do", "primary")
        add_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
        add_btn.setToolTip("Aggiungi un impegno senza orario nella data selezionata")
        add_btn.clicked.connect(lambda: self.add_todo_requested.emit(self._selected_date_str))
        hdr.addWidget(add_btn)
        layout.addLayout(hdr)

        self.summary_badge = _make_label("", "CardMeta")
        layout.addWidget(self.summary_badge)

        self.scroll_list = ScrollList("Nessun impegno o nota nella settimana.")
        layout.addWidget(self.scroll_list, 1)

    def set_sidebar_data(
        self,
        selected_date_str: str,
        events_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_event: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ):
        self._selected_date_str = selected_date_str or dt_date.today().isoformat()
        self.scroll_list.clear()

        notes_by_ev_map = notes_by_event or {}
        all_dates = sorted(set(events_by_date.keys()) | set(notes_by_date.keys()))
        ordered_dates = [self._selected_date_str]
        for ds in all_dates:
            if ds == self._selected_date_str:
                continue
            evs = events_by_date.get(ds) or []
            nts = notes_by_date.get(ds) or []
            has_items = any(e.get("item_type") == "todo" or e.get("is_all_day") for e in evs) or bool(nts)
            if has_items:
                ordered_dates.append(ds)

        weekdays_up = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]
        months_up = ["GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET", "OTT", "NOV", "DIC"]
        today_str = dt_date.today().isoformat()

        pending_todos = 0
        total_notes = 0

        for ds in ordered_dates:
            evs = events_by_date.get(ds) or []
            todos = [e for e in evs if e.get("item_type") == "todo"]
            all_day = [e for e in evs if e.get("item_type") != "todo" and e.get("is_all_day")]
            d_notes = notes_by_date.get(ds) or []

            pending_todos += sum(1 for t in todos if not t.get("is_completed"))
            total_notes += len(d_notes)

            day_sec = QWidget()
            day_sec.setStyleSheet("background: transparent;")
            day_sec.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            dl = QVBoxLayout(day_sec)
            dl.setContentsMargins(0, 0, 0, 4)
            dl.setSpacing(4)

            try:
                d_obj = dt_date.fromisoformat(ds)
                label_txt = f"{weekdays_up[d_obj.weekday()]} {d_obj.day} {months_up[d_obj.month - 1]}"
            except Exception:
                label_txt = ds
            if ds == today_str:
                label_txt = f"OGGI · {label_txt}"

            hdr_frame = QFrame()
            hdr_frame.setObjectName("FTDayGroupHeaderSelected" if ds == self._selected_date_str else "FTDayGroupHeader")
            hdr_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            hl = QHBoxLayout(hdr_frame)
            hl.setContentsMargins(6, 3, 6, 3)
            hl.setSpacing(4)

            day_btn = QPushButton(label_txt)
            day_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            day_btn.setStyleSheet(
                "border: none; background: transparent; text-align: left; "
                "font-size: 10.5px; font-weight: 700; padding: 1px 2px;"
            )
            day_btn.clicked.connect(lambda _c=False, target_ds=ds: self.date_selected.emit(target_ds))
            hl.addWidget(day_btn, 1)

            cnt_items = len(todos) + len(all_day) + len(d_notes)
            if cnt_items > 0:
                hl.addWidget(_make_label(str(cnt_items), "BadgeMuted"))
            dl.addWidget(hdr_frame)

            for ev in todos:
                ev_id = int(ev.get("local_id") or ev.get("id"))
                is_done = bool(ev.get("is_completed"))
                pill = QFrame()
                pill.setObjectName("FTTodoPill")
                pill.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                pl = QVBoxLayout(pill)
                pl.setContentsMargins(7, 5, 7, 5)
                pl.setSpacing(3)

                row = QHBoxLayout()
                row.setSpacing(5)
                cb = QCheckBox("")
                cb.setChecked(is_done)
                cb.setToolTip("Segna impegno come completato / da fare")
                cb.clicked.connect(lambda _c=False, eid=ev_id: self.todo_toggled.emit(eid))
                row.addWidget(cb, 0, Qt.AlignmentFlag.AlignTop)

                accent = ev.get("color") or "#6366F1"
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {accent}; font-size: 10px; background: transparent; padding-top: 2px;")
                row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

                t_lbl = _ClickableLabel(ev.get("summary") or "Impegno")
                t_lbl.setWordWrap(True)
                t_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                t_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                fnt = t_lbl.font()
                fnt.setStrikeOut(is_done)
                t_lbl.setFont(fnt)
                t_lbl.setStyleSheet(
                    "font-size: 11.5px; font-weight: 600; background: transparent;"
                    + (" color: #64748B;" if is_done else "")
                )
                t_lbl.setToolTip("Clicca per modificare o collegare note")
                t_lbl.clicked.connect(lambda e=ev: self.event_clicked.emit(e))
                row.addWidget(t_lbl, 1)
                pl.addLayout(row)

                linked = notes_by_ev_map.get(ev_id) or ev.get("linked_notes") or []
                for note in linked[:2]:
                    nid = int(note["id"])
                    nb = QPushButton(f"📓 {(note.get('title') or 'Nota')[:19]}")
                    nb.setObjectName("FTNotePillBtn")
                    nb.setCursor(Qt.CursorShape.PointingHandCursor)
                    nb.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
                    nb.clicked.connect(lambda _c=False, target_id=nid: self.note_clicked.emit(target_id))
                    pl.addWidget(nb)

                dl.addWidget(pill)

            for ev in all_day:
                btn = QPushButton(f"📌 {(ev.get('summary') or 'Evento')[:22]}")
                btn.setObjectName("FTNotePillBtn")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(ev.get("summary") or "Evento")
                btn.clicked.connect(lambda _c=False, e=ev: self.event_clicked.emit(e))
                dl.addWidget(btn)

            for note in d_notes[:5]:
                nid = int(note["id"])
                nb = QPushButton(f"📓 {(note.get('title') or 'Nota')[:20]}")
                nb.setObjectName("FTNotePillBtn")
                nb.setCursor(Qt.CursorShape.PointingHandCursor)
                nb.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
                nb.clicked.connect(lambda _c=False, target_id=nid: self.note_clicked.emit(target_id))
                dl.addWidget(nb)

            if not todos and not all_day and not d_notes:
                empty_day = _make_label("Nessun To-Do o nota.", "NoteMeta", wrap=True)
                empty_day.setStyleSheet("padding: 2px 4px; background: transparent;")
                dl.addWidget(empty_day)

            self.scroll_list.add_card(day_sec)

        self.summary_badge.setText(f"{pending_todos} da fare · {total_notes} note nella settimana")


class FlowTimeDayTimeline(QFrame):
    """Timeline giornaliera verticale con intestazione giorno in stile Google Calendar."""

    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)
    todo_toggled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._date_str: str = dt_date.today().isoformat()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(10)

        self.day_num_badge = QLabel(str(dt_date.today().day))
        self.day_num_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.day_num_badge.setFixedSize(34, 34)
        hdr.addWidget(self.day_num_badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.day_weekday_lbl = _make_label("", "CardMeta")
        self.day_weekday_lbl.setStyleSheet("font-size: 10px; font-weight: 700; letter-spacing: 0.8px; background: transparent;")
        title_col.addWidget(self.day_weekday_lbl)

        self.day_title_lbl = _make_label("Agenda & Timeline", "CardTitle")
        self.day_title_lbl.setStyleSheet("font-size: 15px; font-weight: 700; background: transparent;")
        title_col.addWidget(self.day_title_lbl)
        hdr.addLayout(title_col)

        self.day_stats_lbl = _make_label("", "BadgeAccent")
        hdr.addWidget(self.day_stats_lbl)
        hdr.addStretch(1)

        self.now_badge = QLabel(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.now_badge.setStyleSheet(
            "background-color: rgba(239, 68, 68, 0.14); color: #EF4444; "
            "border: 1px solid rgba(239, 68, 68, 0.40); border-radius: 7px; "
            "padding: 2px 8px; font-size: 11px; font-weight: 700;"
        )
        hdr.addWidget(self.now_badge)
        layout.addLayout(hdr)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.canvas = _DayTimelineCanvas(self)
        self.canvas.event_clicked.connect(self.event_clicked.emit)
        self.canvas.note_clicked.connect(self.note_clicked.emit)
        self.canvas.empty_slot_clicked.connect(self.empty_slot_clicked.emit)
        self.scroll_area.setWidget(self.canvas)
        layout.addWidget(self.scroll_area, 1)

        self._now_timer = QTimer(self)
        self._now_timer.timeout.connect(self._on_tick)
        self._now_timer.start(30000)

    def _on_tick(self):
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        if self.isVisible():
            self.canvas.update()

    def set_day_data(
        self,
        date_str: str,
        events: List[Dict[str, Any]],
        day_notes: List[Dict[str, Any]],
        notes_by_event: Dict[int, List[Dict[str, Any]]],
        is_dark: bool,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        self._date_str = date_str or dt_date.today().isoformat()
        try:
            d_obj = dt_date.fromisoformat(self._date_str)
        except Exception:
            d_obj = dt_date.today()

        weekdays_up = ["LUNEDÌ", "MARTEDÌ", "MERCOLEDÌ", "GIOVEDÌ", "VENERDÌ", "SABATO", "DOMENICA"]
        is_today = d_obj == dt_date.today()
        self.day_weekday_lbl.setText(weekdays_up[d_obj.weekday()])
        self.day_num_badge.setText(str(d_obj.day))
        if is_today:
            self.day_num_badge.setStyleSheet(
                "background-color: #6366F1; color: #FFFFFF; border: 2px solid #EF4444; "
                "border-radius: 17px; font-size: 15px; font-weight: 700;"
            )
        else:
            self.day_num_badge.setStyleSheet(
                "background-color: #6366F1; color: #FFFFFF; border: none; "
                "border-radius: 17px; font-size: 15px; font-weight: 700;"
            )

        timed = [e for e in events if e.get("item_type") != "todo" and not e.get("is_all_day")]
        self.canvas.set_data(
            date_str=self._date_str,
            timed_events=timed,
            is_dark=is_dark,
            reading_mode=reading_mode,
            notes_by_event=notes_by_event,
            selected_event_id=selected_event_id,
        )
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")

    def scroll_to_focus(self):
        y = int(self.canvas.first_focus_y())
        vp_h = max(160, self.scroll_area.viewport().height())
        sb = self.scroll_area.verticalScrollBar()
        target = max(sb.minimum(), min(sb.maximum(), y - vp_h // 3))
        sb.setValue(target)


class _WeekTimelineCanvas(QWidget):
    """Canvas a 7 colonne orarie (Lunedì–Domenica, 00:00–23:59) con linea ora live e click su slot vuoti."""

    event_clicked = pyqtSignal(dict)
    empty_slot_clicked = pyqtSignal(str, str)

    HOUR_HEIGHT = 48
    TOP_PAD = 8
    BOTTOM_PAD = 12
    AXIS_X = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self._monday: dt_date = dt_date.today() - timedelta(days=dt_date.today().weekday())
        self._selected_date_str: str = dt_date.today().isoformat()
        self._selected_event_id: Optional[int] = None
        self._placed_by_col: Dict[int, List[Dict[str, Any]]] = {}
        self._hit_rects: List[tuple] = []
        self._hover_slot: Optional[tuple] = None  # (col_idx, date_str, snapped_min)
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setMouseTracking(True)
        self.setFixedHeight(24 * self.HOUR_HEIGHT + self.TOP_PAD + self.BOTTOM_PAD)

    def _y_to_snapped_hhmm(self, y: float) -> tuple:
        raw_mins = ((y - float(self.TOP_PAD)) / float(self.HOUR_HEIGHT)) * 60.0
        snapped = int(round(raw_mins / 30.0) * 30)
        snapped = max(0, min(1410, snapped))
        return snapped, f"{snapped // 60:02d}:{snapped % 60:02d}"

    def set_data(
        self,
        monday_date: dt_date,
        selected_date_str: str,
        timed_by_date: Dict[str, List[Dict[str, Any]]],
        is_dark: bool,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        self._monday = monday_date
        self._selected_date_str = selected_date_str
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self._selected_event_id = selected_event_id
        self._placed_by_col = {}

        for col_idx in range(7):
            d_str = (monday_date + timedelta(days=col_idx)).isoformat()
            evs = timed_by_date.get(d_str) or []
            items = []
            for ev in evs:
                s_min = _DayTimelineCanvas._parse_hhmm(ev.get("start_time") or "")
                if s_min is None:
                    s_min = 9 * 60
                e_min = _DayTimelineCanvas._parse_hhmm(ev.get("end_time") or "")
                if e_min is None or e_min <= s_min:
                    e_min = min(1440, s_min + 60)
                vis_end = max(s_min + 30, e_min)
                items.append({"ev": ev, "start": s_min, "end": e_min, "vis_end": vis_end})
            items.sort(key=lambda x: (x["start"], x["vis_end"]))

            placed: List[Dict[str, Any]] = []
            clusters: List[List[Dict[str, Any]]] = []
            cur: List[Dict[str, Any]] = []
            c_end = -1
            for it in items:
                if not cur or it["start"] < c_end:
                    cur.append(it)
                    c_end = max(c_end, it["vis_end"])
                else:
                    clusters.append(cur)
                    cur = [it]
                    c_end = it["vis_end"]
            if cur:
                clusters.append(cur)

            for cl in clusters:
                lane_ends: List[int] = []
                for it in cl:
                    p_lane = None
                    for li, lend in enumerate(lane_ends):
                        if it["start"] >= lend:
                            p_lane = li
                            lane_ends[li] = it["vis_end"]
                            break
                    if p_lane is None:
                        p_lane = len(lane_ends)
                        lane_ends.append(it["vis_end"])
                    it["lane"] = p_lane
                tot_lanes = max(1, len(lane_ends))
                for it in cl:
                    it["lanes"] = tot_lanes
                    placed.append(it)
            self._placed_by_col[col_idx] = placed

        self.update()

    def focus_y(self) -> float:
        today_d = dt_date.today()
        if self._monday <= today_d <= (self._monday + timedelta(days=6)):
            now = datetime.now()
            return float(self.TOP_PAD) + ((now.hour * 60.0 + now.minute) / 60.0) * float(self.HOUR_HEIGHT)
        return float(self.TOP_PAD) + 8.0 * float(self.HOUR_HEIGHT)

    def leaveEvent(self, event):
        if self._hover_slot is not None:
            self._hover_slot = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        if self._is_dark:
            bg_col = QColor("#18130F") if self._reading_mode else QColor("#0B0F1A")
            line_col = QColor(245, 158, 11, 26) if self._reading_mode else QColor(255, 255, 255, 20)
            half_col = QColor(245, 158, 11, 12) if self._reading_mode else QColor(255, 255, 255, 8)
            axis_txt = QColor("#A8957E") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#F5EBE0") if self._reading_mode else QColor("#F1F5F9")
            sub_col = QColor("#D6C5B0") if self._reading_mode else QColor("#94A3B8")
        else:
            bg_col = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            line_col = QColor(180, 83, 9, 28) if self._reading_mode else QColor("#E2E8F0")
            half_col = QColor(180, 83, 9, 14) if self._reading_mode else QColor("#F1F5F9")
            axis_txt = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#1E293B")
            sub_col = QColor("#475569")

        p.fillRect(self.rect(), bg_col)

        left_x = float(self.AXIS_X)
        right_x = w - 2.0
        col_w = max(40.0, (right_x - left_x) / 7.0)
        today_str = dt_date.today().isoformat()
        today_col_idx: Optional[int] = None

        # Sfondo colonna Oggi / Selezionato + divisori verticali
        for col_idx in range(7):
            d_str = (self._monday + timedelta(days=col_idx)).isoformat()
            cx = left_x + col_idx * col_w
            if d_str == today_str:
                today_col_idx = col_idx
                p.fillRect(QRectF(cx, float(self.TOP_PAD), col_w, h - float(self.TOP_PAD + self.BOTTOM_PAD)), QColor(239, 68, 68, 14))
            elif d_str == self._selected_date_str:
                p.fillRect(QRectF(cx, float(self.TOP_PAD), col_w, h - float(self.TOP_PAD + self.BOTTOM_PAD)), QColor(99, 102, 241, 14))

        axis_font = QFont(p.font())
        axis_font.setPointSize(8)
        axis_font.setWeight(QFont.Weight.Medium)
        p.setFont(axis_font)

        for hr in range(25):
            y = float(self.TOP_PAD) + hr * float(self.HOUR_HEIGHT)
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(left_x, y), QPointF(right_x, y))
            if hr < 24:
                p.setPen(axis_txt)
                p.drawText(QRectF(2.0, y - 8.0, left_x - 8.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{hr:02d}:00")
                hy = y + float(self.HOUR_HEIGHT) / 2.0
                p.setPen(QPen(half_col, 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(left_x, hy), QPointF(right_x, hy))

        for col_idx in range(8):
            vx = left_x + col_idx * col_w
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(vx, float(self.TOP_PAD)), QPointF(vx, h - float(self.BOTTOM_PAD)))

        # Hover slot preview
        if self._hover_slot is not None:
            h_col, _h_ds, h_min = self._hover_slot
            hx = left_x + h_col * col_w + 2.0
            hy = float(self.TOP_PAD) + (h_min / 60.0) * float(self.HOUR_HEIGHT)
            hr_rect = QRectF(hx, hy, max(20.0, col_w - 4.0), float(self.HOUR_HEIGHT) / 2.0)
            p.setPen(QPen(QColor(99, 102, 241, 140), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(99, 102, 241, 24)))
            p.drawRoundedRect(hr_rect, 5.0, 5.0)
            p.setFont(axis_font)
            p.setPen(QColor("#818CF8"))
            p.drawText(hr_rect, Qt.AlignmentFlag.AlignCenter, f"+ {h_min // 60:02d}:{h_min % 60:02d}")

        # Blocchi evento per ciascuna colonna giorno
        self._hit_rects = []
        block_font_t = QFont(p.font())
        block_font_t.setPointSize(8)
        block_font_t.setWeight(QFont.Weight.DemiBold)
        block_font_m = QFont(p.font())
        block_font_m.setPointSize(7)

        for col_idx in range(7):
            cx = left_x + col_idx * col_w
            avail_w = max(24.0, col_w - 6.0)
            for item in self._placed_by_col.get(col_idx, []):
                ev = item["ev"]
                y_top = float(self.TOP_PAD) + (item["start"] / 60.0) * float(self.HOUR_HEIGHT) + 1.0
                y_bot = float(self.TOP_PAD) + (item["vis_end"] / 60.0) * float(self.HOUR_HEIGHT) - 1.0
                bh = max(20.0, y_bot - y_top)

                lanes = item["lanes"]
                lane = item["lane"]
                gap = 2.0
                cw = (avail_w - gap * (lanes - 1)) / lanes
                bx = cx + 3.0 + lane * (cw + gap)
                b_rect = QRectF(bx, y_top, cw, bh)
                self._hit_rects.append((b_rect, ev))

                accent = QColor(ev.get("color") or "#6366F1")
                if not accent.isValid():
                    accent = QColor("#6366F1")
                is_selected = self._selected_event_id is not None and ev.get("id") == self._selected_event_id

                fill_c = QColor(accent)
                fill_c.setAlpha(72 if is_selected else (50 if self._is_dark else 34))
                border_c = QColor(accent)
                border_c.setAlpha(240 if is_selected else (165 if self._is_dark else 140))

                p.setPen(QPen(border_c, 2.0 if is_selected else 1.0))
                p.setBrush(QBrush(fill_c))
                p.drawRoundedRect(b_rect, 6.0, 6.0)

                bar_rect = QRectF(bx, y_top + 1.5, 3.0, max(6.0, bh - 3.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(accent))
                p.drawRoundedRect(bar_rect, 1.5, 1.5)

                inner = QRectF(bx + 6.0, y_top + 2.0, max(10.0, cw - 9.0), bh - 4.0)
                summary = ev.get("summary") or "(Senza titolo)"
                time_txt = ev.get("start_time") or ""
                if bh >= 34.0 and time_txt:
                    p.setFont(block_font_m)
                    p.setPen(sub_col)
                    p.drawText(QRectF(inner.x(), inner.y(), inner.width(), 12.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, time_txt)
                    p.setFont(block_font_t)
                    p.setPen(title_col)
                    p.drawText(
                        QRectF(inner.x(), inner.y() + 12.0, inner.width(), max(12.0, inner.height() - 12.0)),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                        summary,
                    )
                else:
                    p.setFont(block_font_t)
                    p.setPen(title_col)
                    elided = QFontMetrics(block_font_t).elidedText(summary, Qt.TextElideMode.ElideRight, int(inner.width()))
                    p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        # Linea rossa ora live (evidenziata sulla colonna di Oggi se visibile)
        now = datetime.now()
        y_now = float(self.TOP_PAD) + ((now.hour * 60.0 + now.minute + now.second / 60.0) / 60.0) * float(self.HOUR_HEIGHT)
        p.setPen(QPen(QColor(239, 68, 68, 90 if today_col_idx is not None else 75), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(left_x, y_now), QPointF(right_x, y_now))
        if today_col_idx is not None:
            tx = left_x + today_col_idx * col_w
            p.setPen(QPen(QColor("#EF4444"), 2.2, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(tx, y_now), QPointF(tx + col_w, y_now))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#EF4444")))
            p.drawEllipse(QPointF(tx, y_now), 4.0, 4.0)

        pill_r = QRectF(2.0, y_now - 8.5, left_x - 6.0, 17.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#EF4444")))
        p.drawRoundedRect(pill_r, 4.0, 4.0)
        pf = QFont(p.font())
        pf.setPointSize(7)
        pf.setWeight(QFont.Weight.Bold)
        p.setFont(pf)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, now.strftime("%H:%M"))

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())
        for r, ev in reversed(self._hit_rects):
            if r.contains(pos):
                if self._hover_slot is not None:
                    self._hover_slot = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                loc = f"\n📍 {ev['location']}" if ev.get("location") else ""
                self.setToolTip(f"{ev.get('time', '')} · {ev.get('summary', '')}{loc}\n(Clicca per modificare o collegare note)")
                return
        if pos.x() >= float(self.AXIS_X) and float(self.TOP_PAD) <= pos.y() <= float(self.height() - self.BOTTOM_PAD):
            col_w = max(40.0, (float(self.width()) - 2.0 - float(self.AXIS_X)) / 7.0)
            col_idx = max(0, min(6, int((pos.x() - float(self.AXIS_X)) / col_w)))
            d_obj = self._monday + timedelta(days=col_idx)
            snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            slot = (col_idx, d_obj.isoformat(), snapped)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Clicca per aggiungere in {d_obj.strftime('%d/%m')} alle {hhmm}")
            if self._hover_slot != slot:
                self._hover_slot = slot
                self.update()
            return
        if self._hover_slot is not None:
            self._hover_slot = None
            self.update()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = QPointF(event.position())
            for r, ev in reversed(self._hit_rects):
                if r.contains(pos):
                    self.event_clicked.emit(ev)
                    return
            col_w = max(40.0, (float(self.width()) - 2.0 - float(self.AXIS_X)) / 7.0)
            col_idx = max(0, min(6, int((max(float(self.AXIS_X), pos.x()) - float(self.AXIS_X)) / col_w)))
            d_str = (self._monday + timedelta(days=col_idx)).isoformat()
            _snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.empty_slot_clicked.emit(d_str, hhmm)
            return
        super().mousePressEvent(event)


class FlowTimeWeekView(QFrame):
    """Vista Settimanale a 7 colonne orarie (00:00–23:59) con striscia To-Do e oggi cerchiato in rosso."""

    date_selected = pyqtSignal(str)
    open_day_requested = pyqtSignal(str)
    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)
    todo_toggled = pyqtSignal(int)
    prev_week_clicked = pyqtSignal()
    next_week_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        prev_btn = _make_button("◀")
        prev_btn.setStyleSheet("min-height: 24px; max-height: 24px; min-width: 26px; max-width: 26px; padding: 0px; font-size: 11px;")
        prev_btn.setToolTip("Settimana precedente")
        prev_btn.clicked.connect(self.prev_week_clicked.emit)
        nav.addWidget(prev_btn)

        next_btn = _make_button("▶")
        next_btn.setStyleSheet("min-height: 24px; max-height: 24px; min-width: 26px; max-width: 26px; padding: 0px; font-size: 11px;")
        next_btn.setToolTip("Settimana successiva")
        next_btn.clicked.connect(self.next_week_clicked.emit)
        nav.addWidget(next_btn)

        self.week_range_lbl = _make_label("Settimana", "CardTitle")
        self.week_range_lbl.setStyleSheet("font-size: 14px; font-weight: 700; background: transparent;")
        nav.addWidget(self.week_range_lbl)

        nav.addStretch(1)
        self.week_stats_lbl = _make_label("", "BadgeMuted")
        nav.addWidget(self.week_stats_lbl)

        self.now_badge = QLabel(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.now_badge.setStyleSheet(
            "background-color: rgba(239, 68, 68, 0.14); color: #EF4444; "
            "border: 1px solid rgba(239, 68, 68, 0.40); border-radius: 7px; "
            "padding: 2px 8px; font-size: 11px; font-weight: 700;"
        )
        nav.addWidget(self.now_badge)
        layout.addLayout(nav)

        # Barra intestazioni 7 giorni in stile Google Calendar (altezza bloccata 64 px)
        self.hdr_strip = QFrame()
        self.hdr_strip.setObjectName("FTWeekHeaderBar")
        self.hdr_strip.setFixedHeight(64)
        self.hdr_strip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hdr_cols_layout = QHBoxLayout(self.hdr_strip)
        self.hdr_cols_layout.setContentsMargins(_WeekTimelineCanvas.AXIS_X, 0, 9, 0)
        self.hdr_cols_layout.setSpacing(0)
        layout.addWidget(self.hdr_strip)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.week_canvas = _WeekTimelineCanvas(self)
        self.week_canvas.event_clicked.connect(self.event_clicked.emit)
        self.week_canvas.empty_slot_clicked.connect(self.empty_slot_clicked.emit)
        self.scroll_area.setWidget(self.week_canvas)
        layout.addWidget(self.scroll_area, 1)

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(30000)

    def _on_tick(self):
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        if self.isVisible():
            self.week_canvas.update()

    def scroll_to_focus(self):
        y = int(self.week_canvas.focus_y())
        vp_h = max(160, self.scroll_area.viewport().height())
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(max(sb.minimum(), min(sb.maximum(), y - vp_h // 3)))

    def set_week_data(
        self,
        selected_date_str: str,
        events_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_date: Dict[str, List[Dict[str, Any]]],
        is_dark: bool = True,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        while self.hdr_cols_layout.count():
            item = self.hdr_cols_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        try:
            sel_d = dt_date.fromisoformat(selected_date_str)
        except Exception:
            sel_d = dt_date.today()

        monday = sel_d - timedelta(days=sel_d.weekday())
        sunday = monday + timedelta(days=6)
        months_full = [
            "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
            "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
        ]
        months_short = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]
        weekdays_up = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]

        if monday.month == sunday.month:
            range_txt = f"{months_full[monday.month - 1]} {monday.year} · {monday.day}–{sunday.day}"
        else:
            range_txt = f"{monday.day} {months_short[monday.month - 1]} – {sunday.day} {months_short[sunday.month - 1]} {sunday.year}"
        self.week_range_lbl.setText(range_txt)

        today_d = dt_date.today()
        total_events = 0
        total_notes = 0
        timed_by_date: Dict[str, List[Dict[str, Any]]] = {}

        for offset in range(7):
            d_cur = monday + timedelta(days=offset)
            d_str = d_cur.isoformat()
            is_today = d_cur == today_d
            is_selected = d_cur == sel_d

            day_events = events_by_date.get(d_str) or []
            day_notes = notes_by_date.get(d_str) or []
            total_events += len(day_events)
            total_notes += len(day_notes)

            timed_by_date[d_str] = [e for e in day_events if e.get("item_type") != "todo" and not e.get("is_all_day")]
            todos = [e for e in day_events if e.get("item_type") == "todo"]

            col_hdr = QFrame()
            col_hdr.setObjectName("FTWeekHeaderCell")
            if is_today:
                col_hdr.setStyleSheet(
                    "QFrame#FTWeekHeaderCell { background-color: rgba(239, 68, 68, 0.08); "
                    "border: none; border-top-left-radius: 6px; border-top-right-radius: 6px; }"
                )
            col_l = QVBoxLayout(col_hdr)
            col_l.setContentsMargins(2, 4, 2, 4)
            col_l.setSpacing(2)
            col_l.setAlignment(Qt.AlignmentFlag.AlignHCenter)

            # Riga 1: Sigla giorno in maiuscolo stile Google Calendar
            wd_lbl = QLabel(weekdays_up[offset])
            wd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wd_color = "#EF4444" if is_today else ("#818CF8" if (is_selected and is_dark) else ("#4F46E5" if is_selected else ("#94A3B8" if is_dark else "#64748B")))
            wd_lbl.setStyleSheet(
                f"color: {wd_color}; font-size: 10px; font-weight: 700; letter-spacing: 0.8px; background: transparent;"
            )
            col_l.addWidget(wd_lbl)

            # Riga 2: Numero del giorno (nessun cerchio rosso per oggi; solo pill piena quando selezionato)
            num_btn = QPushButton(str(d_cur.day))
            num_btn.setFixedSize(34, 34)
            num_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            num_btn.setToolTip("Clicca per selezionare · Riclicca per aprire la vista Giorno")
            if is_selected:
                num_btn.setStyleSheet(
                    "background-color: #6366F1; color: #FFFFFF; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 700; padding: 0px;"
                )
            elif is_today:
                num_btn.setStyleSheet(
                    "background-color: transparent; color: #EF4444; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 700; padding: 0px;"
                )
            else:
                txt_col = "#F1F5F9" if is_dark else "#1E293B"
                num_btn.setStyleSheet(
                    f"background-color: transparent; color: {txt_col}; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 600; padding: 0px;"
                )
            if is_selected:
                num_btn.clicked.connect(lambda _c=False, ds=d_str: self.open_day_requested.emit(ds))
            else:
                num_btn.clicked.connect(lambda _c=False, ds=d_str: self.date_selected.emit(ds))
            col_l.addWidget(num_btn, 0, Qt.AlignmentFlag.AlignHCenter)

            # Riga 3: Micro-indicatore fisso (12 px) che non espande mai l'intestazione
            parts = []
            if todos:
                parts.append(f"☑️{len(todos)}")
            if day_notes:
                parts.append(f"📓{len(day_notes)}")
            hint_lbl = QLabel(" ".join(parts))
            hint_lbl.setFixedHeight(12)
            hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint_lbl.setStyleSheet(
                f"color: {'#94A3B8' if is_dark else '#64748B'}; font-size: 9px; font-weight: 600; background: transparent;"
            )
            col_l.addWidget(hint_lbl)

            self.hdr_cols_layout.addWidget(col_hdr, 1)

        self.week_canvas.set_data(
            monday_date=monday,
            selected_date_str=sel_d.isoformat(),
            timed_by_date=timed_by_date,
            is_dark=is_dark,
            reading_mode=reading_mode,
            selected_event_id=selected_event_id,
        )
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.week_stats_lbl.setText(
            f"{total_events} {'impegno' if total_events == 1 else 'impegni'} · "
            f"{total_notes} {'nota' if total_notes == 1 else 'note'} nella settimana"
        )


class FlowLocalHub(QMainWindow):
    note_added = pyqtSignal()
    flowtime_sync_done = pyqtSignal(bool, str)

    def __init__(self, on_device_changed: Optional[Callable] = None, engine=None):
        super().__init__()
        # API pubblica mantenuta per compatibilità con main.py / pipeline audio
        self.on_device_changed = on_device_changed
        self.engine = engine
        self.live_volume = 0.0
        self._transcribe_thread = None
        self._import_thread = None
        self._autolink_thread = None
        self._note_cards: Dict[int, QFrame] = {}
        self._ft_editing_event_id: Optional[int] = None

        self.setWindowTitle("FlowLocal Hub")
        self.resize(1120, 680)
        self.setMinimumSize(920, 580)
        settings = QSettings("FlowLocal", "Hub")
        saved_theme = str(settings.value("theme", "obsidian") or "obsidian")
        self._current_theme = saved_theme if saved_theme in _THEME_PALETTES else "obsidian"
        raw_rm = settings.value("reading_mode", False)
        self._reading_mode = str(raw_rm).lower() in ("true", "1", "yes") if not isinstance(raw_rm, bool) else raw_rm
        saved_fp = str(settings.value("font_preset", "auto") or "auto")
        self._font_preset = saved_fp if saved_fp in FONT_PRESETS else "auto"
        self._custom_font_family = str(settings.value("custom_font_family", "") or "")
        try:
            self._font_scale = max(90, min(120, int(settings.value("font_scale", 100) or 100)))
        except Exception:
            self._font_scale = 100
        ThemedPushButton.current_theme = self._current_theme
        ThemedPushButton.reading_mode = self._reading_mode
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )

        self.note_added.connect(self._refresh_notekeeper)
        self.flowtime_sync_done.connect(self._on_ft_sync_finished)
        try:
            from core.calendar_sync import calendar_manager
            calendar_manager.add_sync_listener(lambda ok, msg: self.flowtime_sync_done.emit(ok, msg))
        except Exception:
            pass

        self._current_mode_name = MODE_TABS[0][1]
        self._last_transcription_marker = None
        self._last_whis_marker = None

        # Assistente Whis condiviso e monitoraggio
        self.whis_agent = WhisAgent()
        self.whis_hud = None
        self._active_whis_worker = None
        self.wake_listener = None
        self.is_handsfree_enabled = True
        self.on_handsfree_changed: Optional[Callable[[bool], None]] = None
        self.whis_agent.status_changed.connect(self._on_whis_status_changed)
        self.whis_agent.interaction_saved.connect(self._on_whis_interaction_saved)

        # Inizializzazione Gestore Coda Cloud Controllata (Anti-Lag Gaming)
        self.cloud_sync = cloud_sync
        self._cloud_queue_thread = None

        self._nk_current_filter_folder_id: Optional[int] = None
        self._build_ui()
        self._sync_editor_typography()
        self._refresh_current_page()

        # Scorciatoia globale Command Palette (Ctrl+K)
        self._cmd_palette_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._cmd_palette_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._cmd_palette_shortcut.activated.connect(self._open_command_palette)

        # Sync iniziale e periodico Google Calendar in background (se connesso)
        QTimer.singleShot(1500, self._startup_google_calendar_sync)
        self.gcal_sync_timer = QTimer(self)
        self.gcal_sync_timer.timeout.connect(self._periodic_google_calendar_sync)
        self.gcal_sync_timer.start(120000)

        # Aggiornamento leggero della Cronologia: ricostruisce solo se arrivano nuovi dettati
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_new_transcriptions)
        self.poll_timer.start(2500)

        # Timer leggero Coda Cloud (ogni 10s, sola lettura disco, 0 CPU / 0 GPU)
        self.cloud_poll_timer = QTimer(self)
        self.cloud_poll_timer.timeout.connect(self._poll_cloud_queue)
        self.cloud_poll_timer.start(10000)
        self._poll_cloud_queue()

    # -----------------------------------------------------------------
    # API pubblica
    # -----------------------------------------------------------------
    def update_live_volume(self, rms_normalized: float):
        """Chiamato dal thread audio: memorizza solo il valore (nessuna operazione UI cross-thread)."""
        self.live_volume = float(rms_normalized)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_windows_titlebar_theme(self._current_theme)
        self._refresh_current_page()

    def _active_is_dark(self) -> bool:
        return _is_dark_theme(getattr(self, "_current_theme", "obsidian"))

    def _active_palette(self) -> dict:
        return _resolve_palette(
            getattr(self, "_current_theme", "obsidian"),
            getattr(self, "_reading_mode", False),
            font_preset=getattr(self, "_font_preset", "auto"),
            custom_font=getattr(self, "_custom_font_family", ""),
        )

    def _apply_windows_titlebar_theme(self, theme_key: str, reading_mode: Optional[bool] = None):
        """Sincronizza la barra del titolo nativa di Windows 10/11 con il tema attivo."""
        if reading_mode is None:
            reading_mode = getattr(self, "_reading_mode", False)
        try:
            import ctypes
            hwnd = int(self.winId())
            if not hwnd:
                return
            dwmapi = ctypes.windll.dwmapi
            is_dark = 1 if _is_dark_theme(theme_key) else 0
            dark_val = ctypes.c_int(is_dark)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
            if reading_mode:
                cap_hex = 0x000D1115 if is_dark else 0x00D0E5EF  # #15110D / #EFE5D0 (BGR)
                text_hex = 0x00E0EBF5 if is_dark else 0x00141F2A
            else:
                caption_colors = {
                    "obsidian": 0x000B0705,             # #05070B (BGR)
                    "light": 0x00FFFFFF,                # #FFFFFF (BGR)
                    "orb": 0x00FFFDF0,                  # #F0FDFF (BGR)
                    "dark_orb": 0x0050320E,             # #0E3250 (BGR)
                    "ghibli_spring": 0x00E8F3ED,        # #EDF3E8 (BGR)
                    "pixel_chill": 0x00444037,          # #374044 (BGR)
                    "katana": 0x001A1A1F,               # #1F1A1A (BGR)
                    "tactical_hud": 0x001D1813,         # #13181D (BGR)
                    "industrial_mecha": 0x001E1C1A,     # #1A1C1E (BGR)
                    "amber_crt": 0x0009121A,            # #1A1209 (BGR)
                    "starfield_alabaster": 0x00DDE4E6,  # #E6E4DD (BGR)
                    "cyber_synthwave": 0x00240E16,      # #160E24 (BGR)
                }
                cap_hex = caption_colors.get(theme_key, 0x000B0705)
                text_hex = 0x00FCFAF8 if is_dark else 0x002A170F
            cap_val = ctypes.c_int(cap_hex)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap_val), ctypes.sizeof(cap_val))
            text_val = ctypes.c_int(text_hex)
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_val), ctypes.sizeof(text_val))
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Struttura principale (TOP BAR 4 MONDI: FlowType | FlowNotes | FlowTime | Whis)
    # -----------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("HubRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.nav_buttons_by_page: Dict[int, QPushButton] = {}
        main_layout.addWidget(self._build_top_bar())
        main_layout.addWidget(self._build_flowtype_subbar())

        # Ordine spaziale direzionale (Top Bar: [0] FlowType -> [1] FlowNotes -> [2] FlowTime -> [3] Whis,
        # più sub-viste interne Dettatura/Dizionario/Stili e Quaderno/Flashcard/Grafo)
        self.pages = SlidingStackedWidget(
            duration_ms=280,
            spatial_ranks={
                0: 0,   # FlowType: Dettatura
                5: 1,   # FlowType: Dizionario & Snippet
                6: 2,   # FlowType: Stili & Prompt
                1: 10,  # FlowNotes: Quaderno
                2: 11,  # FlowNotes: Flashcard
                3: 12,  # FlowNotes: Grafo
                7: 20,  # FlowTime
                4: 30,  # Whis
            },
        )
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self._build_flowtype_page())             # Index 0: 🎙️ FlowType
        self.pages.addWidget(self._build_flowstudy_notekeeper_page()) # Index 1: 📓 FlowNotes - Quaderno
        self.pages.addWidget(self._build_flowstudy_flashcards_page()) # Index 2: 📓 FlowNotes - Flashcard
        self.pages.addWidget(self._build_flowstudy_graph_page())      # Index 3: 📓 FlowNotes - Grafo
        self.pages.addWidget(self._build_whis_page())                 # Index 4: ✨ Whis
        self.pages.addWidget(self._build_dictionary_page())           # Index 5: 📖 Dizionario & Snippet
        self.pages.addWidget(self._build_styles_page())               # Index 6: ⚙️ Stili & Prompt
        self.pages.addWidget(self._build_flowtime_page())             # Index 7: 🗓️ FlowTime
        self.pages.currentChanged.connect(self._sync_top_nav_state)
        main_layout.addWidget(self.pages, 1)

        self._on_nav_clicked(0)

    def _build_top_bar(self) -> QFrame:
        top_bar = QFrame()
        top_bar.setObjectName("TopNavBar")
        top_bar.setFixedHeight(52)

        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(10)

        # A sinistra: solo Logo + FlowLocal (contenitore rigido per stabilità del menù centrale)
        left_host = QWidget()
        left_host.setStyleSheet("background: transparent;")
        left_box = QHBoxLayout(left_host)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(8)
        left_box.addWidget(OrbBrandBadge(24))
        title = QLabel("FlowLocal")
        title.setObjectName("AppTitle")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        left_box.addWidget(title)
        layout.addWidget(left_host)

        layout.addStretch(1)

        # Al centro: Segmented Control a 4 pillole con pillola attiva scorrevole (FlowType | FlowNotes | FlowTime | Whis)
        seg_box = AnimatedSegmentedBox(
            box_object_name="TopSegmentBox",
            pill_object_name="TopSlidingPill",
            duration_ms=280,
        )
        self.top_segment_box = seg_box
        seg_box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        seg_layout = QHBoxLayout(seg_box)
        seg_layout.setContentsMargins(4, 3, 4, 3)
        seg_layout.setSpacing(4)

        self.top_world_group = QButtonGroup(self)
        self.top_world_group.setExclusive(True)

        self.top_flowtype_btn = ThemedPushButton("🎙️ FlowType")
        self.top_flownotes_btn = ThemedPushButton("📓 FlowNotes")
        self.top_flowtime_btn = ThemedPushButton("🗓️ FlowTime")
        self.top_whis_btn = ThemedPushButton("✨ Whis")

        for idx, (btn, target_page) in enumerate([
            (self.top_flowtype_btn, 0),
            (self.top_flownotes_btn, 1),
            (self.top_flowtime_btn, 7),
            (self.top_whis_btn, 4),
        ]):
            btn.setObjectName("TopSegmentBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, p=target_page: self._on_nav_clicked(p))
            self.top_world_group.addButton(btn, idx)
            seg_layout.addWidget(btn)
            seg_box.register_button(btn)

        layout.addWidget(seg_box)
        layout.addStretch(1)

        # A destra: Barra Spotify + Modalità Lettura + Rotellina Impostazioni (larghezza fissa anti-shift)
        right_host = QWidget()
        right_host.setFixedWidth(368)
        right_host.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        right_host.setStyleSheet("background: transparent;")
        right_row = QHBoxLayout(right_host)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(6)

        self.spotify_bar = SpotifyMiniBar(self)
        self.spotify_bar.set_theme_mode(self._active_is_dark())
        right_row.addWidget(self.spotify_bar)

        self.reading_mode_btn = ThemedPushButton("📖 Lettura")
        self.reading_mode_btn.setObjectName("FilterPill")
        self.reading_mode_btn.setCheckable(True)
        self.reading_mode_btn.setChecked(self._reading_mode)
        self.reading_mode_btn.setFixedWidth(84)
        self.reading_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reading_mode_btn.setToolTip("Modalità Lettura: toni caldi riposanti per la vista")
        self.reading_mode_btn.setStyleSheet("padding: 0 8px; font-size: 11px; min-height: 26px;")
        self.reading_mode_btn.toggled.connect(self._toggle_reading_mode)
        right_row.addWidget(self.reading_mode_btn)

        self.settings_btn = ThemedPushButton("⚙️")
        self.settings_btn.setObjectName("SettingsGearBtn")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Impostazioni FlowLocal (Temi, Google Calendar, Sistema)")
        self.settings_btn.clicked.connect(self._on_open_settings_clicked)
        right_row.addWidget(self.settings_btn)

        layout.addWidget(right_host)

        return top_bar

    def _build_flowtype_subbar(self) -> QFrame:
        subbar = AnimatedSegmentedBox(
            box_object_name="SubNavBar",
            pill_object_name="SubSlidingPill",
            duration_ms=260,
        )
        subbar.setFixedHeight(38)
        self.flowtype_subbar = subbar

        layout = QHBoxLayout(subbar)
        layout.setContentsMargins(24, 4, 24, 4)
        layout.setSpacing(6)

        self.flowtype_sub_group = QButtonGroup(self)
        self.flowtype_sub_group.setExclusive(True)
        for idx, (lbl, page_idx) in enumerate([
            ("Dettatura · Statistiche", 0),
            ("Dizionario · Snippet", 5),
            ("Stili · Impostazioni", 6),
        ]):
            btn = ThemedPushButton(lbl)
            btn.setObjectName("SubNavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, p=page_idx: self._on_nav_clicked(p))
            self.flowtype_sub_group.addButton(btn, idx)
            self.nav_buttons_by_page[page_idx] = btn
            layout.addWidget(btn)
            subbar.register_button(btn)

        layout.addStretch(1)
        return subbar

    def _sync_top_nav_state(self, page_idx: int):
        if hasattr(self, "flowtype_subbar"):
            self.flowtype_subbar.setVisible(page_idx in (0, 5, 6))

        if page_idx in (0, 5, 6) and hasattr(self, "top_flowtype_btn"):
            self.top_flowtype_btn.setChecked(True)
        elif page_idx in (1, 2, 3) and hasattr(self, "top_flownotes_btn"):
            self.top_flownotes_btn.setChecked(True)
        elif page_idx == 7 and hasattr(self, "top_flowtime_btn"):
            self.top_flowtime_btn.setChecked(True)
        elif page_idx == 4 and hasattr(self, "top_whis_btn"):
            self.top_whis_btn.setChecked(True)

        if page_idx in self.nav_buttons_by_page:
            self.nav_buttons_by_page[page_idx].setChecked(True)

        if hasattr(self, "fn_sidebar_view_btns") and page_idx in self.fn_sidebar_view_btns:
            self.fn_sidebar_view_btns[page_idx].setChecked(True)
        if hasattr(self, "fn_fc_view_btns") and page_idx in self.fn_fc_view_btns:
            self.fn_fc_view_btns[page_idx].setChecked(True)
        if hasattr(self, "fn_graph_view_btns") and page_idx in self.fn_graph_view_btns:
            self.fn_graph_view_btns[page_idx].setChecked(True)

    def _set_theme(self, theme_key: str):
        if theme_key not in _THEME_PALETTES or theme_key == self._current_theme:
            return
        self._current_theme = theme_key
        QSettings("FlowLocal", "Hub").setValue("theme", theme_key)
        self._apply_theme_change()

    def _toggle_reading_mode(self, checked: bool):
        if bool(checked) == self._reading_mode:
            return
        self._reading_mode = bool(checked)
        QSettings("FlowLocal", "Hub").setValue("reading_mode", self._reading_mode)
        self._apply_theme_change()

    def _set_font_preset(self, preset_key: str):
        if preset_key not in FONT_PRESETS or preset_key == self._font_preset:
            return
        self._font_preset = preset_key
        QSettings("FlowLocal", "Hub").setValue("font_preset", preset_key)
        self._apply_typography_change()

    def _set_custom_font_family(self, family: str):
        clean_fam = (family or "").strip()
        if not clean_fam:
            return
        if self._font_preset == "custom" and self._custom_font_family == clean_fam:
            return
        self._font_preset = "custom"
        self._custom_font_family = clean_fam
        settings = QSettings("FlowLocal", "Hub")
        settings.setValue("font_preset", "custom")
        settings.setValue("custom_font_family", clean_fam)
        self._apply_typography_change()

    def _set_font_scale(self, scale_pct: int):
        clamped = max(90, min(120, int(scale_pct or 100)))
        if clamped == self._font_scale:
            return
        self._font_scale = clamped
        QSettings("FlowLocal", "Hub").setValue("font_scale", clamped)
        self._apply_typography_change()

    def _sync_editor_typography(self):
        if hasattr(self, "nk_editor_body") and hasattr(self.nk_editor_body, "set_typography"):
            resolved_font = _resolve_font_family(
                self._current_theme,
                self._font_preset,
                self._custom_font_family,
            )
            self.nk_editor_body.set_typography(resolved_font, self._font_scale)

    def _apply_typography_change(self):
        self.setUpdatesEnabled(False)
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )
        self._sync_editor_typography()
        if hasattr(self, "top_segment_box"):
            QTimer.singleShot(0, self.top_segment_box.sync_pill_geometry)
        if hasattr(self, "flowtype_subbar") and hasattr(self.flowtype_subbar, "sync_pill_geometry"):
            QTimer.singleShot(0, self.flowtype_subbar.sync_pill_geometry)
        self.setUpdatesEnabled(True)

    def _apply_theme_change(self):
        old_pixmap = self.grab() if self.isVisible() else None
        ThemedPushButton.current_theme = self._current_theme
        ThemedPushButton.reading_mode = self._reading_mode
        is_dark = self._active_is_dark()
        if hasattr(self, "nk_col3_layout"):
            if self._reading_mode:
                self.nk_col3_layout.setContentsMargins(84, 22, 84, 20)
            else:
                self.nk_col3_layout.setContentsMargins(48, 18, 48, 16)
        if hasattr(self, "nk_editor_body"):
            if hasattr(self.nk_editor_body, "set_theme_modes"):
                self.nk_editor_body.set_theme_modes(is_dark, self._reading_mode)
            elif hasattr(self.nk_editor_body, "set_dark_mode"):
                self.nk_editor_body.set_dark_mode(is_dark)
            if hasattr(self.nk_editor_body, "set_sync_accent_color"):
                p_sync = self._active_palette()
                self.nk_editor_body.set_sync_accent_color(
                    p_sync.get("accent") or p_sync.get("nav_checked_border") or "#6366F1"
                )

        overlay = ThemeRevealOverlay(self, old_pixmap) if old_pixmap is not None else None
        self.setUpdatesEnabled(False)
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )
        self._sync_editor_typography()
        self._apply_windows_titlebar_theme(self._current_theme, self._reading_mode)
        if hasattr(self, "nk_tree") and hasattr(self.nk_tree, "set_theme_palette"):
            self.nk_tree.set_theme_palette(self._active_palette())
        if hasattr(self, "spotify_bar"):
            self.spotify_bar.set_theme_mode(is_dark)
        if hasattr(self, "flowtype_style_title"):
            p = self._active_palette()
            st_col = p["title"] if is_dark or self._reading_mode else p["card_body"]
            self.flowtype_style_title.setStyleSheet(
                f"font-size: 12.5px; font-weight: 600; color: {st_col}; background: transparent;"
            )
        if hasattr(self, "whis_list"):
            self._refresh_whis_page()
        if hasattr(self, "nk_tree"):
            self._refresh_notekeeper()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()
        if hasattr(self, "top_segment_box"):
            QTimer.singleShot(0, self.top_segment_box.sync_pill_geometry)
        if hasattr(self, "flowtype_subbar") and hasattr(self.flowtype_subbar, "sync_pill_geometry"):
            QTimer.singleShot(0, self.flowtype_subbar.sync_pill_geometry)
        self.setUpdatesEnabled(True)
        self.update()
        if overlay is not None:
            overlay.raise_()

    def _on_nav_clicked(self, page_idx: int):
        self.pages.setCurrentIndex(page_idx)
        self._sync_top_nav_state(page_idx)
        self._refresh_current_page()

    def _refresh_current_page(self):
        idx = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if idx == 0:
            self._refresh_history()
        elif idx == 1:
            self._refresh_notekeeper()
        elif idx == 2:
            self._refresh_flashcards_page()
        elif idx == 3:
            if hasattr(self, "graph_widget"):
                self.graph_widget.load_data()
        elif idx == 4:
            self._refresh_whis_page()
        elif idx == 5:
            self._refresh_dictionary()
        elif idx == 6:
            self._load_mode_prompt(self._current_mode_name)
        elif idx == 7:
            self._refresh_flowtime_page()
            self._periodic_google_calendar_sync()

    @staticmethod
    def _make_page(title: str, subtitle: str) -> (QWidget, QVBoxLayout):
        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(_make_label(title, "PageTitle"))
        header.addWidget(_make_label(subtitle, "PageSubtitle", wrap=True))
        layout.addLayout(header)
        return page, layout

    # -----------------------------------------------------------------
    # 1. PILASTRO: FLOWTYPE (SCRITTURA VOCALE & DETTATURA LIVE)
    # -----------------------------------------------------------------
    def _build_flowtype_page(self) -> QWidget:
        page, layout = self._make_page("🎙️ FlowType", "Scrittura vocale in tempo reale, telemetria e gestione degli stili di dettatura.")

        # --- 4 MINI-CARD STATISTICHE FLOWTYPE (IN ALTO) ---
        stats_box = QFrame()
        stats_box.setObjectName("FlowTypeStatsBox")
        stats_box.setStyleSheet("QFrame#FlowTypeStatsBox { background-color: transparent; border: none; }")
        st_layout = QHBoxLayout(stats_box)
        st_layout.setContentsMargins(0, 0, 0, 4)
        st_layout.setSpacing(10)

        def _make_stat_card(icon_title: str, default_val: str, subtitle: str, color_val: str = "#38BDF8"):
            card = QFrame()
            card.setObjectName("Card")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(14, 12, 14, 12)
            c_layout.setSpacing(3)

            lbl_title = QLabel(icon_title)
            lbl_title.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600; background: transparent;")
            c_layout.addWidget(lbl_title)

            lbl_val = QLabel(default_val)
            lbl_val.setStyleSheet(f"color: {color_val}; font-size: 20px; font-weight: 700; background: transparent;")
            c_layout.addWidget(lbl_val)

            lbl_sub = QLabel(subtitle)
            lbl_sub.setStyleSheet("color: #64748B; font-size: 10px; background: transparent;")
            c_layout.addWidget(lbl_sub)
            return card, lbl_val

        card_words, self.stat_words_label = _make_stat_card("Parole", "0", "dettate con FlowLocal", "#38BDF8")
        card_wpm, self.stat_wpm_label = _make_stat_card("Velocità", "150 WPM", "parole al minuto", "#10B981")
        card_time, self.stat_time_label = _make_stat_card("Risparmio", "0 min", "rispetto a digitazione", "#A855F7")
        card_streak, self.stat_streak_label = _make_stat_card("Streak", "0 Giorni", "continuità", "#7DD3FC")

        st_layout.addWidget(card_words)
        st_layout.addWidget(card_wpm)
        st_layout.addWidget(card_time)
        st_layout.addWidget(card_streak)
        layout.addWidget(stats_box)

        # --- SELETTORE RAPIDO STILE DI DETTATURA ---
        style_box = QFrame()
        style_box.setObjectName("Card")
        s_layout = QVBoxLayout(style_box)
        s_layout.setContentsMargins(14, 12, 14, 12)
        s_layout.setSpacing(10)

        s_header = QHBoxLayout()
        s_title = QLabel("Stile di Dettatura")
        p_active = self._active_palette()
        st_col = p_active["title"] if self._active_is_dark() or getattr(self, "_reading_mode", False) else p_active["card_body"]
        s_title.setStyleSheet(f"font-size: 12.5px; font-weight: 600; color: {st_col}; background: transparent;")
        self.flowtype_style_title = s_title
        s_header.addWidget(s_title)

        self.flowtype_active_style_label = QLabel("Naturale")
        self.flowtype_active_style_label.setObjectName("Badge")
        s_header.addWidget(self.flowtype_active_style_label)
        s_header.addStretch(1)
        s_layout.addLayout(s_header)

        pills_layout = QHBoxLayout()
        pills_layout.setSpacing(8)

        self.flowtype_style_group = QButtonGroup(self)
        self.flowtype_style_group.setExclusive(True)

        styles_data = [
            ("Naturale", "natural", "Riformulazione sintattica conservativa e rimozione esitazioni."),
            ("Formale", "formal", "Registro alto per documenti ufficiali, email e relazioni."),
            ("Code / Tech", "code", "Preserva l'italiano e i termini tecnici informatici dettati."),
            ("Studio / LaTeX", "study_latex", "Espressioni matematiche e formule convertite in $...$ LaTeX."),
            ("Chat", "chat", "Discorso informale, punteggiatura snella e ritmo naturale.")
        ]

        self._flowtype_style_buttons = {}
        for s_idx, (s_label, s_code, s_desc) in enumerate(styles_data):
            btn = QPushButton(s_label)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(s_desc)
            btn.clicked.connect(lambda _c=False, code=s_code, lbl=s_label: self._on_flowtype_style_selected(code, lbl))
            self.flowtype_style_group.addButton(btn, s_idx)
            self._flowtype_style_buttons[s_code] = btn
            pills_layout.addWidget(btn)
            if s_idx == 0:
                btn.setChecked(True)

        pills_layout.addStretch(1)
        s_layout.addLayout(pills_layout)
        layout.addWidget(style_box)

        # --- STATO HOTKEY SHIFT DESTRO ---
        hotkey_box = QFrame()
        hotkey_box.setObjectName("Card")
        h_layout = QHBoxLayout(hotkey_box)
        h_layout.setContentsMargins(14, 10, 14, 10)
        h_layout.setSpacing(12)

        key_badge = QLabel("Shift Destro")
        key_badge.setObjectName("ShortcutKey")
        h_layout.addWidget(key_badge)

        h_info = QLabel("<b>Push-to-Talk principale:</b> tieni premuto Shift Destro (sotto Invio) per dettare live, rilascia per incollare con zero focus steal.<br>"
                        "<b>Push-to-Lock (Hands-Free):</b> premi <b>Ctrl Destro</b> (o <b>Alt Gr</b>) mentre tieni Shift Destro, oppure doppio tocco. <b>Shift Sinistro:</b> resta 100% libero.")
        h_info.setWordWrap(True)
        h_info.setStyleSheet("color: #94A3B8; font-size: 12px; background: transparent;")
        h_layout.addWidget(h_info, 1)

        layout.addWidget(hotkey_box)

        # --- TOOLBAR RICERCA E SVUOTA CRONOLOGIA ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("🔍 Cerca nelle trascrizioni recenti…")
        self.history_search.setMinimumHeight(36)
        self.history_search.textChanged.connect(self._refresh_history)
        toolbar.addWidget(self.history_search, 1)

        clear_btn = _make_button("🗑️ Svuota cronologia", "danger")
        clear_btn.clicked.connect(self._clear_history)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        # --- LISTA TRASCRIZIONI RECENTI ---
        self.history_list = ScrollList("Nessuna trascrizione ancora. Tieni premuto Shift Destro per iniziare a dettare.")
        layout.addWidget(self.history_list, 1)
        return page

    def _build_history_page(self) -> QWidget:
        """Retrocompatibilità: rimanda a _build_flowtype_page."""
        return self._build_flowtype_page()

    def _on_flowtype_style_selected(self, style_code: str, style_label: str):
        self.flowtype_active_style_label.setText(style_label)
        if self.engine:
            self.engine.current_mode = style_code

    def _refresh_history(self):
        # Aggiornamento statistiche FlowType
        try:
            stats = db.get_statistics()
            total_words = stats.get("total_words", 0)
            saved_min = stats.get("saved_minutes", 0)
            if hasattr(self, "stat_words_label"):
                self.stat_words_label.setText(f"{total_words:,}".replace(",", "."))
            if hasattr(self, "stat_time_label"):
                if saved_min >= 60:
                    self.stat_time_label.setText(f"{saved_min // 60}h {saved_min % 60}m")
                else:
                    self.stat_time_label.setText(f"{saved_min} min")
            if hasattr(self, "stat_wpm_label"):
                wpm = stats.get("speaking_wpm", 150)
                self.stat_wpm_label.setText(f"{wpm} WPM")
            if hasattr(self, "stat_streak_label"):
                streak_days = stats.get("streak_days", 0)
                streak_txt = f"{streak_days} Giorno" if streak_days == 1 else f"{streak_days} Giorni"
                self.stat_streak_label.setText(streak_txt)
        except Exception:
            pass

        query = self.history_search.text().strip().lower() if hasattr(self, "history_search") else ""
        items = db.get_recent_transcriptions(limit=200 if query else 20)
        self._last_transcription_marker = items[0]["id"] if items else None

        if query:
            items = [
                t for t in items
                if query in (t.get("final_text") or "").lower()
                or query in (t.get("target_app") or "").lower()
                or query in (t.get("style_used") or "").lower()
            ]

        self.history_list.clear()
        if not items:
            self.history_list.show_empty("Nessun risultato." if query else None)
            return

        for t in items:
            self.history_list.add_card(self._make_history_card(t))

    def _make_history_card(self, t: Dict) -> QFrame:
        card, layout = _make_card()
        card.setMinimumHeight(96)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(t.get("created_at")), "CardMeta"))
        if t.get("style_used"):
            meta_row.addWidget(_make_label(t["style_used"], "Badge"))
        if t.get("target_app"):
            meta_row.addWidget(_make_label(t["target_app"], "BadgeMuted"))
        meta_row.addStretch(1)

        text = t.get("final_text") or ""
        fix_btn = _make_button("✏️ Correggi")
        fix_btn.clicked.connect(lambda _c=False, s=text: self._open_quick_rule_dialog(s))
        meta_row.addWidget(fix_btn)

        copy_btn = _make_button("📋 Copia")
        copy_btn.clicked.connect(lambda _c=False, s=text: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)
        layout.addLayout(meta_row)

        layout.addWidget(_make_label(text, "CardBody", wrap=True))
        return card

    def _open_quick_rule_dialog(self, text: str):
        dlg = QuickRuleDialog(text, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_dictionary()

    def _clear_history(self):
        reply = QMessageBox.question(
            self, "Svuota cronologia",
            "Eliminare definitivamente tutte le trascrizioni salvate?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.clear_all_transcriptions()
            self._refresh_history()

    def _poll_new_transcriptions(self):
        if not self.isVisible():
            return
        curr_page = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if curr_page == 0:
            latest = db.get_recent_transcriptions(limit=1)
            marker = latest[0]["id"] if latest else None
            if marker != self._last_transcription_marker:
                self._refresh_history()
        elif curr_page == 1:
            latest_whis = db.get_whis_history(limit=1)
            whis_marker = latest_whis[0]["id"] if latest_whis else None
            if whis_marker != getattr(self, "_last_whis_marker", None):
                self._refresh_whis_page()

    # -----------------------------------------------------------------
    # 1b. WHIS
    # -----------------------------------------------------------------
    def set_whis_agent(self, agent: WhisAgent):
        """Assegna l'agente Whis condiviso e collega i listener di stato ed eventi."""
        if hasattr(self, "whis_agent") and self.whis_agent:
            try:
                self.whis_agent.status_changed.disconnect(self._on_whis_status_changed)
                self.whis_agent.interaction_saved.disconnect(self._on_whis_interaction_saved)
            except Exception:
                pass
        self.whis_agent = agent
        if self.whis_agent:
            try:
                self.whis_agent.status_changed.connect(self._on_whis_status_changed)
                self.whis_agent.interaction_saved.connect(self._on_whis_interaction_saved)
                self.whis_agent.note_created.connect(lambda _id, _title: self._refresh_notekeeper())
            except Exception:
                pass
            if hasattr(self, "whis_model_combo"):
                idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
                if idx >= 0:
                    self.whis_model_combo.setCurrentIndex(idx)

    def _build_whis_page(self) -> QWidget:
        page, layout = self._make_page("✨ Whis", "Assistente virtuale ingegneristico, cronologia interazioni e monitoraggio live.")

        # 1. Header & Live Status Panel (Obsidian Glass)
        status_frame = QFrame()
        status_frame.setObjectName("Card")
        sf_layout = QVBoxLayout(status_frame)
        sf_layout.setContentsMargins(14, 12, 14, 12)
        sf_layout.setSpacing(10)

        # Riga 1: Status live + Switch Mani Libere + Mostra HUD
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self.whis_status_label = QLabel("● Pronto — Mani Libere Attive (\"Whis...\")")
        self.whis_status_label.setStyleSheet("color: #34D399; font-size: 12px; font-weight: 600; background: transparent;")
        row1.addWidget(self.whis_status_label, 1)

        # Switch Mani Libere (Attivo ON di default)
        self.handsfree_switch = QPushButton("Mani Libere: ON")
        self.handsfree_switch.setObjectName("FilterPill")
        self.handsfree_switch.setCheckable(True)
        self.handsfree_switch.setChecked(self.is_handsfree_enabled)
        self.handsfree_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handsfree_switch.toggled.connect(self._on_handsfree_toggled)
        row1.addWidget(self.handsfree_switch)

        # Pulsante Mostra ultimo HUD
        show_hud_btn = _make_button("Mostra HUD")
        show_hud_btn.clicked.connect(self._show_last_whis_hud)
        row1.addWidget(show_hud_btn)

        sf_layout.addLayout(row1)

        # Riga 2: Selettori compatti (Modello, Audio, Voce, Prova) + Google Calendar
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        lbl_mod = QLabel("Modello:")
        lbl_mod.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_mod)

        self.whis_model_combo = QComboBox()
        self.whis_model_combo.setMinimumHeight(30)
        self.whis_model_combo.addItem("Auto (3B/7B)", "auto")
        self.whis_model_combo.addItem("Rapido (qwen2.5:3b)", "3b")
        self.whis_model_combo.addItem("Profondo (qwen2.5:7b)", "7b")
        if hasattr(self, "whis_agent") and self.whis_agent:
            cur_idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
            if cur_idx >= 0:
                self.whis_model_combo.setCurrentIndex(cur_idx)
        self.whis_model_combo.currentIndexChanged.connect(self._on_whis_model_changed)
        row2.addWidget(self.whis_model_combo)

        lbl_audio = QLabel("Audio:")
        lbl_audio.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_audio)

        self.whis_audio_combo = QComboBox()
        self.whis_audio_combo.setMinimumHeight(30)
        self.whis_audio_combo.addItem("Muto (Default)", MODE_MUTE)
        self.whis_audio_combo.addItem("Suono Morbido", MODE_SOFT_CHIME)
        self.whis_audio_combo.addItem("Voce (TTS)", MODE_VOICE)

        cur_audio_mode = audio_feedback.get_mode()
        cur_audio_idx = self.whis_audio_combo.findData(cur_audio_mode)
        if cur_audio_idx >= 0:
            self.whis_audio_combo.setCurrentIndex(cur_audio_idx)

        self.whis_audio_combo.currentIndexChanged.connect(self._on_whis_audio_mode_changed)
        audio_feedback.add_mode_listener(self._sync_whis_audio_combo)
        row2.addWidget(self.whis_audio_combo)

        lbl_voice = QLabel("Voce:")
        lbl_voice.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_voice)

        self.whis_voice_combo = QComboBox()
        self.whis_voice_combo.setMinimumHeight(30)
        for k, v in VOICE_PRESETS.items():
            self.whis_voice_combo.addItem(v["label"], k)

        cur_voice = audio_feedback.get_voice_profile()
        cur_v_idx = self.whis_voice_combo.findData(cur_voice)
        if cur_v_idx >= 0:
            self.whis_voice_combo.setCurrentIndex(cur_v_idx)

        self.whis_voice_combo.currentIndexChanged.connect(self._on_whis_voice_changed)
        audio_feedback.add_voice_listener(self._sync_whis_voice_combo)
        row2.addWidget(self.whis_voice_combo)

        self.whis_voice_test_btn = _make_button("Prova")
        self.whis_voice_test_btn.setToolTip("Ascolta un'anteprima vocale con il profilo selezionato")
        self.whis_voice_test_btn.clicked.connect(self._on_whis_voice_test_clicked)
        row2.addWidget(self.whis_voice_test_btn)

        row2.addStretch(1)

        self.whis_cal_btn = _make_button("Google Calendar")
        self.whis_cal_btn.clicked.connect(self._on_connect_calendar_clicked)
        row2.addWidget(self.whis_cal_btn)

        self.whis_cal_status = QLabel()
        self.whis_cal_status.setStyleSheet("font-size: 11px; font-weight: 500; background: transparent;")
        row2.addWidget(self.whis_cal_status)
        self._update_calendar_status_ui()
        sf_layout.addLayout(row2)

        self._update_handsfree_ui(self.is_handsfree_enabled)

        layout.addWidget(status_frame)

        # 2. Barra di Ricerca Cronologia
        search_row = QHBoxLayout()
        search_row.setSpacing(10)

        self.whis_search = QLineEdit()
        self.whis_search.setPlaceholderText("🔍 Cerca nelle conversazioni con Whis…")
        self.whis_search.setMinimumHeight(36)
        self.whis_search.textChanged.connect(self._refresh_whis_page)
        search_row.addWidget(self.whis_search, 1)

        clear_btn = _make_button("🗑️ Svuota cronologia", "danger")
        clear_btn.clicked.connect(self._clear_whis_history)
        search_row.addWidget(clear_btn)

        layout.addLayout(search_row)

        # 3. Feed Cronologia Conversazioni
        self.whis_list = ScrollList("Nessuna conversazione con Whis ancora. Usa Shift Destro dicendo 'Whis...' oppure scrivi qui sotto.")
        layout.addWidget(self.whis_list, 1)

        # 4. Input Testuale Rapido in Basso
        input_bar = QHBoxLayout()
        input_bar.setSpacing(10)

        self.whis_input = DictationGhostLineEdit(dark_mode=self._active_is_dark())
        self.whis_input.setPlaceholderText("Chiedi qualcosa a Whis o incolla del codice…")
        self.whis_input.setMinimumHeight(40)
        self.whis_input.returnPressed.connect(self._send_whis_text_query)
        input_bar.addWidget(self.whis_input, 1)

        self.whis_send_btn = _make_button("Invia ✨", "primary")
        self.whis_send_btn.setMinimumHeight(40)
        self.whis_send_btn.clicked.connect(self._send_whis_text_query)
        input_bar.addWidget(self.whis_send_btn)

        layout.addLayout(input_bar)

        return page

    def _on_whis_model_changed(self):
        mode = self.whis_model_combo.currentData()
        if hasattr(self, "whis_agent") and self.whis_agent:
            self.whis_agent.set_model_mode(mode)

    def _on_whis_audio_mode_changed(self):
        if not hasattr(self, "whis_audio_combo") or not self.whis_audio_combo:
            return
        mode = self.whis_audio_combo.currentData()
        if mode and mode != audio_feedback.get_mode():
            audio_feedback.set_mode(mode)

    def _sync_whis_audio_combo(self, mode: str):
        if not hasattr(self, "whis_audio_combo") or not self.whis_audio_combo:
            return
        idx = self.whis_audio_combo.findData(mode)
        if idx >= 0 and self.whis_audio_combo.currentIndex() != idx:
            self.whis_audio_combo.blockSignals(True)
            self.whis_audio_combo.setCurrentIndex(idx)
            self.whis_audio_combo.blockSignals(False)

    def _on_whis_voice_changed(self):
        if not hasattr(self, "whis_voice_combo") or not self.whis_voice_combo:
            return
        key = self.whis_voice_combo.currentData()
        if key and key != audio_feedback.get_voice_profile():
            audio_feedback.set_voice_profile(key)

    def _sync_whis_voice_combo(self, key: str):
        if not hasattr(self, "whis_voice_combo") or not self.whis_voice_combo:
            return
        idx = self.whis_voice_combo.findData(key)
        if idx >= 0 and self.whis_voice_combo.currentIndex() != idx:
            self.whis_voice_combo.blockSignals(True)
            self.whis_voice_combo.setCurrentIndex(idx)
            self.whis_voice_combo.blockSignals(False)

    def _on_whis_voice_test_clicked(self):
        """Fa pronunciare a Whis la frase di test con la voce selezionata."""
        key = self.whis_voice_combo.currentData() if hasattr(self, "whis_voice_combo") else None
        audio_feedback.preview_voice(key)

    def _update_calendar_status_ui(self):
        """Aggiorna lo stato visivo di FlowTime / Google Calendar nell'Hub (zero chiamate di rete)."""
        if not hasattr(self, "whis_cal_status") or not self.whis_cal_status:
            return
        from core.calendar_sync import calendar_manager
        if calendar_manager.has_token_file():
            self.whis_cal_status.setText("🟢 FlowTime + Google Calendar sincronizzati")
            self.whis_cal_status.setStyleSheet("color: #10B981; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Google Calendar: Connesso")
        elif calendar_manager.has_credentials_file():
            self.whis_cal_status.setText("🟡 FlowTime locale attivo · credentials.json pronto")
            self.whis_cal_status.setStyleSheet("color: #F59E0B; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Connetti Google Calendar")
        else:
            self.whis_cal_status.setText("🟢 FlowTime Locale Attivo (Google Sync opzionale)")
            self.whis_cal_status.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Connetti Google Calendar")

    def _on_connect_calendar_clicked(self):
        """Gestore clic pulsante autorizzazione Google Calendar."""
        from core.calendar_sync import calendar_manager
        if not calendar_manager.has_credentials_file() and not calendar_manager.has_token_file():
            QMessageBox.information(
                self,
                "Google Calendar — Configurazione Richiesta",
                "Per connettere Google Calendar a Whis:\n\n"
                "1. Vai su Google Cloud Console e crea o scarica le credenziali OAuth (tipo 'Applicazione Desktop').\n"
                "2. Salva il file come 'credentials.json' nella cartella principale del progetto:\n"
                f"{os.getcwd()}\n\n"
                "3. Clicca nuovamente su 'Connetti Google Calendar' per completare l'accesso nel browser."
            )
            return

        self.whis_cal_status.setText("⏳ Connessione in corso nel browser...")
        self.whis_cal_status.setStyleSheet("color: #38BDF8; font-size: 11px;")
        QApplication.processEvents()

        def _auth_thread():
            ok, msg = calendar_manager.authenticate(interactive=True)
            QTimer.singleShot(0, lambda: self._on_calendar_auth_finished(ok, msg))

        threading.Thread(target=_auth_thread, daemon=True, name="GoogleCalendarAuthThread").start()

    def _on_calendar_auth_finished(self, ok: bool, msg: str):
        self._update_calendar_status_ui()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()
        if ok:
            QMessageBox.information(self, "Google Calendar", "✅ Google Calendar connesso e sincronizzato con FlowTime!\nWhis continuerà a rispondere istantaneamente dal database locale.")
        else:
            QMessageBox.warning(self, "Google Calendar", f"⚠️ Impossibile completare l'autenticazione:\n{msg}")

    def set_wake_listener(self, listener):
        """Collega il listener hands-free e sincronizza lo stato dello switch UI."""
        if hasattr(self, "wake_listener") and self.wake_listener and hasattr(self.wake_listener, "status_message"):
            try:
                self.wake_listener.status_message.disconnect(self._on_wake_status_message)
            except Exception:
                pass
        self.wake_listener = listener
        if hasattr(listener, "status_message"):
            try:
                listener.status_message.connect(self._on_wake_status_message)
            except Exception:
                pass
        if hasattr(self, "handsfree_switch"):
            is_active = self.is_handsfree_enabled
            self.handsfree_switch.blockSignals(True)
            self.handsfree_switch.setChecked(is_active)
            self.handsfree_switch.blockSignals(False)
            self._update_handsfree_ui(is_active)

    def _on_wake_status_message(self, msg: str):
        if hasattr(self, "whis_status_label") and self.is_handsfree_enabled:
            self.whis_status_label.setText(msg)
            if "rilevato parlato" in msg.lower():
                self.whis_status_label.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: 600;")
            else:
                self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")

    def _on_handsfree_toggled(self, checked: bool):
        """Gestisce il click sullo switch Attivazione Mani Libere."""
        self.is_handsfree_enabled = checked
        if self.wake_listener:
            if checked:
                if not self.wake_listener.isRunning():
                    self.wake_listener.start()
                self.wake_listener.set_paused(False)
            else:
                self.wake_listener.set_paused(True)

        self._update_handsfree_ui(checked)
        if callable(getattr(self, "on_handsfree_changed", None)):
            try:
                self.on_handsfree_changed(checked)
            except Exception:
                pass

    def _update_handsfree_ui(self, active: bool):
        """Aggiorna lo stato visivo dello switch e dei badge informativi."""
        if not hasattr(self, "handsfree_switch"):
            return

        if active:
            self.handsfree_switch.setText("Mani Libere: ON")
            self.handsfree_switch.setStyleSheet("")
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("● Pronto — Mani Libere Attive (\"Whis...\")")
                self.whis_status_label.setStyleSheet("color: #34D399; font-size: 12px; font-weight: 600; background: transparent;")
        else:
            self.handsfree_switch.setText("Mani Libere: OFF")
            self.handsfree_switch.setStyleSheet("")
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("● Pronto (Shift Destro)")
                self.whis_status_label.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 600; background: transparent;")

    def _show_last_whis_hud(self):
        if hasattr(self, "whis_hud") and self.whis_hud:
            self.whis_hud.show_latest()

    def _on_whis_status_changed(self, status: str, query: str = ""):
        if not hasattr(self, "whis_status_label"):
            return
        if status in ("In attesa", "Risposta completata", "Pronto"):
            if getattr(self, "is_handsfree_enabled", False):
                self.whis_status_label.setText("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
            else:
                self.whis_status_label.setText("🟢 Pronto (In ascolto su Shift Destro + \"Whis...\")")
            self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")
        else:
            clean_q = f': "{query[:40]}..."' if query else ""
            self.whis_status_label.setText(f"🟣 {status}{clean_q}")
            self.whis_status_label.setStyleSheet("color: #C084FC; font-size: 12px; font-weight: 600;")

    def _on_whis_interaction_saved(self, item: Dict):
        self._refresh_whis_page()

    def _refresh_whis_page(self):
        query = self.whis_search.text().strip().lower() if hasattr(self, "whis_search") else ""
        items = db.get_whis_history(search_query=query, limit=100)
        self._last_whis_marker = items[0]["id"] if items else None

        self.whis_list.clear()
        if not items:
            self.whis_list.show_empty("Nessun risultato trovato." if query else None)
            return

        for item in items:
            self.whis_list.add_card(self._make_whis_card(item))

    def _make_whis_card(self, item: Dict) -> QFrame:
        card, layout = _make_card()

        # Row 1: Header con timestamp, modello, badge clipboard e azioni
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(item.get("created_at")), "CardMeta"))

        model_str = item.get("model_used") or "3b"
        if "7b" in model_str.lower():
            badge_model = _make_label("🧠 7B Deep", "Badge")
            badge_model.setStyleSheet("background-color: rgba(168, 85, 247, 0.18); color: #C084FC; border: 1px solid rgba(168, 85, 247, 0.4); border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 600;")
            meta_row.addWidget(badge_model)
        else:
            badge_model = _make_label("⚡ 3B Fast", "Badge")
            badge_model.setStyleSheet("background-color: rgba(56, 189, 248, 0.15); color: #38BDF8; border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 600;")
            meta_row.addWidget(badge_model)

        if item.get("clipboard_context"):
            clip_badge = _make_label("📎 Appunti allegati", "BadgeMuted")
            clip_badge.setStyleSheet("background-color: #1E222D; border: 1px solid #2E364A; color: #C084FC; border-radius: 4px; padding: 1px 6px; font-size: 10px;")
            clip_badge.setToolTip(str(item["clipboard_context"])[:300])
            meta_row.addWidget(clip_badge)

        meta_row.addStretch(1)

        whis_resp = item.get("whis_response") or ""
        user_q = item.get("user_query") or ""

        # Pulsante Copia
        copy_btn = _make_button("📋 Copia")
        def on_copy(_c=False, btn=copy_btn, text=whis_resp):
            self._copy_to_clipboard(text)
            btn.setText("✓ Copiato!")
            QTimer.singleShot(2000, lambda: btn.setText("📋 Copia"))
        copy_btn.clicked.connect(on_copy)
        meta_row.addWidget(copy_btn)

        # Pulsante Salva nelle Note
        save_note_btn = _make_button("📝 Salva nelle Note")
        def on_save_note(_c=False, btn=save_note_btn, q=user_q, ans=whis_resp):
            title = f"Whis: {q[:38]}" if q else "Risposta Whis"
            content = f"**Domanda:**\n{q}\n\n**Risposta di Whis:**\n{ans}"
            db.add_study_note(title=title, content_markdown=content, note_type="Note", tags="Whis, Studio")
            btn.setText("✓ Salvata!")
            btn.setEnabled(False)
            self.note_added.emit()
            QTimer.singleShot(2500, lambda: (btn.setText("📝 Salva nelle Note"), btn.setEnabled(True)))
        save_note_btn.clicked.connect(on_save_note)
        meta_row.addWidget(save_note_btn)

        # Pulsante Elimina singola card
        del_btn = _make_button("🗑️", "danger")
        del_btn.setToolTip("Elimina questa conversazione")
        del_btn.clicked.connect(lambda _c=False, item_id=item["id"]: self._delete_whis_interaction(item_id))
        meta_row.addWidget(del_btn)

        layout.addLayout(meta_row)

        p_act = self._active_palette()
        q_bg = p_act["badge_bg"]
        q_color = p_act["badge_text"]
        resp_color = p_act["card_body"]
        q_radius = p_act["input_radius"]

        # Row 2: Domanda Utente
        q_box = QFrame()
        q_box.setStyleSheet(f"background-color: {q_bg}; border: none; border-radius: {q_radius}; padding: 6px 10px;")
        q_layout = QVBoxLayout(q_box)
        q_layout.setContentsMargins(6, 4, 6, 4)
        q_lbl = _make_label(f"💬 {user_q}", "CardTitle", wrap=True)
        q_lbl.setStyleSheet(f"color: {q_color}; font-weight: 600; font-size: 13px; background: transparent;")
        q_layout.addWidget(q_lbl)
        layout.addWidget(q_box)

        # Row 3: Risposta Whis
        resp_lbl = _make_label(whis_resp, "CardBody", wrap=True)
        resp_lbl.setStyleSheet(f"color: {resp_color}; font-size: 13px; padding-top: 4px; background: transparent;")
        layout.addWidget(resp_lbl)

        return card

    def _delete_whis_interaction(self, item_id: int):
        db.delete_whis_interaction(item_id)
        self._refresh_whis_page()

    def _clear_whis_history(self):
        reply = QMessageBox.question(
            self, "Svuota cronologia Whis",
            "Eliminare definitivamente tutte le conversazioni salvate con Whis?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.clear_whis_history()
            self._refresh_whis_page()

    def _send_whis_text_query(self):
        query = self.whis_input.text().strip()
        if not query:
            return
        audio_feedback.stop()
        self.whis_input.clear()
        self.whis_send_btn.setEnabled(False)

        if not hasattr(self, "whis_agent") or not self.whis_agent:
            self.whis_agent = WhisAgent()

        self._on_whis_status_changed(f"Elaborazione con {self.whis_agent.select_model(query)}...", query)

        self._active_whis_worker = WhisStreamWorker(self.whis_agent, query)
        def on_finished(full_text, model):
            self.whis_send_btn.setEnabled(True)
            self._on_whis_status_changed("Pronto")
            self._refresh_whis_page()
            audio_feedback.speak_response(full_text)
        def on_error(err):
            self.whis_send_btn.setEnabled(True)
            self._on_whis_status_changed("Errore")
            QMessageBox.warning(self, "Whis", f"Errore durante la richiesta a Whis:\n{err}")

        self._active_whis_worker.finished_response.connect(on_finished)
        self._active_whis_worker.error_occurred.connect(on_error)
        self._active_whis_worker.start()

    # -----------------------------------------------------------------
    # 2. PILASTRO: FLOWSTUDY (NOTEKEEPER A 3 COLONNE & SBOBINE)
    # -----------------------------------------------------------------
    def _build_flownotes_view_bar(self, store_attr: str) -> QWidget:
        """Barra compatta [ 📄 Quaderno ] [ 🃏 Flashcard ] [ 🕸️ Grafo ] per le viste di FlowNotes."""
        bar = AnimatedSegmentedBox(
            box_object_name="FlowNotesViewBar",
            pill_object_name="SubSlidingPill",
            duration_ms=260,
        )
        bar.setStyleSheet("QFrame#FlowNotesViewBar { background: transparent; border: none; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        btn_map: Dict[int, QPushButton] = {}
        grp = QButtonGroup(bar)
        grp.setExclusive(True)
        for idx, (lbl, pid) in enumerate([
            ("Quaderno", 1),
            ("Flashcard", 2),
            ("Grafo", 3),
        ]):
            b = ThemedPushButton(lbl)
            b.setObjectName("SubNavBtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("padding: 5px 8px; font-size: 12px;")
            b.clicked.connect(lambda _c=False, p=pid: self._on_nav_clicked(p))
            grp.addButton(b, idx)
            btn_map[pid] = b
            layout.addWidget(b)
            bar.register_button(b)
        setattr(self, store_attr, btn_map)
        return bar

    def _build_flowstudy_notekeeper_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        main_vbox = QVBoxLayout(page)
        main_vbox.setContentsMargins(10, 8, 10, 10)
        main_vbox.setSpacing(6)

        # Barra di avanzamento compatta sbobina file
        self.sbobina_progress_box = QFrame()
        self.sbobina_progress_box.setStyleSheet(
            "QFrame { background-color: #131722; border: 1px solid #1E293B; border-radius: 8px; }"
        )
        p_layout = QHBoxLayout(self.sbobina_progress_box)
        p_layout.setContentsMargins(10, 6, 10, 6)
        p_layout.setSpacing(10)

        self.sbobina_status_label = QLabel("Pronto")
        self.sbobina_status_label.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 500;")
        p_layout.addWidget(self.sbobina_status_label, 1)

        self.sbobina_progress_bar = QProgressBar()
        self.sbobina_progress_bar.setRange(0, 100)
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_progress_bar.setFixedHeight(14)
        self.sbobina_progress_bar.setTextVisible(True)
        self.sbobina_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1E293B; border-radius: 7px; text-align: center; color: #FFFFFF; font-size: 10px; font-weight: bold; min-width: 120px; }"
            "QProgressBar::chunk { background-color: #0284C7; border-radius: 7px; }"
        )
        p_layout.addWidget(self.sbobina_progress_bar, 2)

        self.sbobina_cancel_btn = _make_button("✕ Annulla", "danger")
        self.sbobina_cancel_btn.setMinimumHeight(28)
        self.sbobina_cancel_btn.clicked.connect(self._on_cancel_sbobina_clicked)
        p_layout.addWidget(self.sbobina_cancel_btn)

        self.sbobina_progress_box.hide()
        main_vbox.addWidget(self.sbobina_progress_box)

        # ---------------- LAYOUT FLOWNOTES A 2 COLONNE STILE NOTION ----------------
        nk_container = QFrame()
        nk_container.setObjectName("NotekeeperContainer")
        nk_container_layout = QVBoxLayout(nk_container)
        nk_container_layout.setContentsMargins(0, 0, 0, 0)
        nk_container_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        self.nk_splitter = splitter

        # ===== COLONNA 1: SIDEBAR UNICA FLOWNOTES (280px, COLLASSABILE) =====
        col1 = QWidget()
        col1.setObjectName("NKCol1")
        col1.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col1.setMinimumWidth(280)
        col1.setMaximumWidth(380)
        self.nk_col1 = col1
        col1_layout = QVBoxLayout(col1)
        col1_layout.setContentsMargins(10, 10, 10, 10)
        col1_layout.setSpacing(7)

        # Switch Viste Studio in cima alla Sidebar + pulsante collasso
        col1_top_row = QHBoxLayout()
        col1_top_row.setContentsMargins(0, 0, 0, 0)
        col1_top_row.setSpacing(4)
        col1_top_row.addWidget(self._build_flownotes_view_bar("fn_sidebar_view_btns"), 1)

        self.nk_hide_col1_btn = _make_button("«")
        self.nk_hide_col1_btn.setObjectName("NKCollapseBtn")
        self.nk_hide_col1_btn.setToolTip("Nascondi Sidebar FlowNotes")
        self.nk_hide_col1_btn.clicked.connect(lambda: self._hide_nk_col(0))
        col1_top_row.addWidget(self.nk_hide_col1_btn)
        col1_layout.addLayout(col1_top_row)

        # Barra di ricerca + Profilo compatto
        self.nk_search_input = QLineEdit()
        self.nk_search_input.setPlaceholderText("🔍 Cerca note, materie...")
        self.nk_search_input.setMinimumHeight(32)
        self.nk_search_input.textChanged.connect(self._on_nk_search_changed)
        col1_layout.addWidget(self.nk_search_input)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.nk_new_note_btn = _make_button("+ Nuova Nota", "primary")
        self.nk_new_note_btn.setFixedHeight(32)
        self.nk_new_note_btn.clicked.connect(self._on_nk_new_note_clicked)
        actions_row.addWidget(self.nk_new_note_btn, 1)
        col1_layout.addLayout(actions_row)

        # Filtro Profilo compatto + Toggle Vista Card
        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.nk_profile_combo = QComboBox()
        self.nk_profile_combo.addItems(["Tutti i profili", "Alessandro", "Giada"])
        self.nk_profile_combo.setMinimumHeight(28)
        self.nk_profile_combo.currentIndexChanged.connect(self._on_nk_profile_changed)
        profile_row.addWidget(self.nk_profile_combo, 1)

        self.nk_card_view_btn = _make_button("☷")
        self.nk_card_view_btn.setObjectName("NKCollapseBtn")
        self.nk_card_view_btn.setCheckable(True)
        self.nk_card_view_btn.setToolTip("Mostra/Nascondi colonna Vista Card")
        self.nk_card_view_btn.setFixedSize(32, 28)
        self.nk_card_view_btn.clicked.connect(self._toggle_nk_card_view)
        profile_row.addWidget(self.nk_card_view_btn)
        col1_layout.addLayout(profile_row)

        # Albero Cartelle & Sottocartelle Stile Samsung Notes (FlowNotes)
        self.nk_tree = SamsungNotesTreeWidget()
        self.nk_tree.setObjectName("NKTree")
        self.nk_tree.set_theme_palette(self._active_palette())
        self.nk_tree.currentItemChanged.connect(self._on_nk_tree_item_changed)
        self.nk_tree.noteMovedToFolder.connect(self._on_nk_note_moved)
        self.nk_tree.folderMovedToParent.connect(self._on_nk_folder_moved)
        self.nk_tree.createSubfolderRequested.connect(self._on_nk_create_subfolder)
        self.nk_tree.renameFolderRequested.connect(self._on_nk_rename_folder)
        self.nk_tree.changeFolderColorRequested.connect(self._on_nk_change_folder_color)
        self.nk_tree.deleteFolderRequested.connect(self._on_nk_delete_folder)
        self.nk_tree.toggleNoteStudiedRequested.connect(self._on_nk_toggle_studied)
        col1_layout.addWidget(self.nk_tree, 1)

        # Footer compatto della Sidebar
        col1_btn_layout = QVBoxLayout()
        col1_btn_layout.setSpacing(5)
        self.nk_new_sub_btn = _make_button("+ Materia")
        self.nk_new_sub_btn.setObjectName("NKCol1Btn")
        self.nk_new_sub_btn.setFixedHeight(30)
        self.nk_new_sub_btn.clicked.connect(self._on_nk_new_subject_clicked)
        col1_btn_layout.addWidget(self.nk_new_sub_btn)

        footer_row2 = QHBoxLayout()
        footer_row2.setSpacing(5)
        self.import_slide_btn = _make_button("📥 Importa")
        self.import_slide_btn.setObjectName("NKCol1Btn")
        self.import_slide_btn.setToolTip("Importa PDF / Slide / Immagini")
        self.import_slide_btn.setFixedHeight(30)
        self.import_slide_btn.clicked.connect(self._on_import_slide_clicked)
        footer_row2.addWidget(self.import_slide_btn, 1)

        self.sbobina_btn = _make_button("🎙️ Sbobina")
        self.sbobina_btn.setObjectName("NKCol1Btn")
        self.sbobina_btn.setToolTip("Sbobina Audio / Lezione")
        self.sbobina_btn.setFixedHeight(30)
        self.sbobina_btn.clicked.connect(self._on_sbobina_clicked)
        footer_row2.addWidget(self.sbobina_btn, 1)
        col1_btn_layout.addLayout(footer_row2)
        col1_layout.addLayout(col1_btn_layout)

        splitter.addWidget(col1)

        # ===== COLONNA 2 OPZIONALE: VISTA CARD (NASCOSTA DI DEFAULT PER DARE 80% ALL'EDITOR) =====
        col2 = QWidget()
        col2.setObjectName("NKCol2")
        col2.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col2.setMinimumWidth(240)
        self.nk_col2 = col2
        col2_layout = QVBoxLayout(col2)
        col2_layout.setContentsMargins(8, 10, 8, 10)
        col2_layout.setSpacing(6)

        self.nk_show_col1_in_col2_btn = _make_button("📁 Sidebar ▸")
        self.nk_show_col1_in_col2_btn.setObjectName("NKRestorePill")
        self.nk_show_col1_in_col2_btn.setVisible(False)
        self.nk_show_col1_in_col2_btn.clicked.connect(lambda: self._show_nk_col(0))
        col2_layout.addWidget(self.nk_show_col1_in_col2_btn, 0, Qt.AlignmentFlag.AlignLeft)

        col2_hdr = QHBoxLayout()
        col2_hdr.addWidget(_make_label("VISTA CARD", "NavSectionHeader"))
        col2_hdr.addStretch(1)
        self.nk_hide_col2_btn = _make_button("«")
        self.nk_hide_col2_btn.setObjectName("NKCollapseBtn")
        self.nk_hide_col2_btn.setToolTip("Chiudi Vista Card")
        self.nk_hide_col2_btn.clicked.connect(lambda: self._toggle_nk_card_view(False))
        col2_hdr.addWidget(self.nk_hide_col2_btn)
        col2_layout.addLayout(col2_hdr)

        self.nk_notes_list = ScrollList("Nessuna nota per l'argomento selezionato.")
        self.nk_notes_list.vbox.setContentsMargins(2, 6, 6, 6)
        self.nk_notes_list.vbox.setSpacing(6)
        col2_layout.addWidget(self.nk_notes_list, 1)

        col2.setVisible(False)
        splitter.addWidget(col2)

        # ===== AREA PRINCIPALE DESTRA: FOGLIO EDITOR NOTION (80% DELLO SCHERMO) =====
        col3 = QWidget()
        col3.setObjectName("NKCol3")
        col3.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col3.setMinimumWidth(420)
        self.nk_col3 = col3
        col3_layout = QVBoxLayout(col3)
        self.nk_col3_layout = col3_layout
        if getattr(self, "_reading_mode", False):
            col3_layout.setContentsMargins(84, 22, 84, 20)
        else:
            col3_layout.setContentsMargins(48, 18, 48, 16)
        col3_layout.setSpacing(8)

        # Toolbar superiore unica: ripristino sidebar, auto-save, Vista Studio + azioni secondarie
        editor_top_bar = QHBoxLayout()
        editor_top_bar.setContentsMargins(0, 0, 0, 0)
        editor_top_bar.setSpacing(6)

        self.nk_show_col1_btn = _make_button("📁 Sidebar ▸")
        self.nk_show_col1_btn.setObjectName("NKRestorePill")
        self.nk_show_col1_btn.setVisible(False)
        self.nk_show_col1_btn.clicked.connect(lambda: self._show_nk_col(0))
        editor_top_bar.addWidget(self.nk_show_col1_btn)

        self.nk_show_col2_btn = _make_button("☷ Card ▸")
        self.nk_show_col2_btn.setObjectName("NKRestorePill")
        self.nk_show_col2_btn.setVisible(False)
        self.nk_show_col2_btn.clicked.connect(lambda: self._toggle_nk_card_view(True))
        editor_top_bar.addWidget(self.nk_show_col2_btn)

        self.nk_autosave_badge = QLabel("✓ Salvato automaticamente")
        self.nk_autosave_badge.setObjectName("NKAutoSaveBadge")
        self.nk_autosave_badge.setStyleSheet(
            "background: transparent; border: none; color: #787774; font-size: 12px; font-weight: 400;"
        )
        editor_top_bar.addWidget(self.nk_autosave_badge)

        editor_top_bar.addStretch(1)

        self.cloud_badge = QLabel("☁️ (0)")
        self.cloud_badge.setObjectName("CloudBadge")
        self.cloud_badge.setProperty("pending", False)
        self.cloud_badge.setFixedHeight(28)
        self.cloud_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cloud_badge.setToolTip("Coda Cloud in sospeso")
        editor_top_bar.addWidget(self.cloud_badge)

        self.cloud_process_btn = _make_button("Elabora", "success")
        self.cloud_process_btn.setObjectName("NKActionBtn")
        self.cloud_process_btn.setEnabled(False)
        self.cloud_process_btn.setVisible(False)
        self.cloud_process_btn.clicked.connect(self._on_process_cloud_queue_clicked)
        editor_top_bar.addWidget(self.cloud_process_btn)

        self.cloud_sync_btn = _make_button("☁️ Cloud")
        self.cloud_sync_btn.setObjectName("NKActionBtn")
        self.cloud_sync_btn.setToolTip("Rigenera outbox_study/library.json con note e flashcard del PC")
        self.cloud_sync_btn.clicked.connect(self._on_sync_cloud_clicked)
        editor_top_bar.addWidget(self.cloud_sync_btn)

        self.export_all_btn = _make_button("Esporta ▾")
        self.export_all_btn.setObjectName("NKActionBtn")
        self.export_all_btn.clicked.connect(self._show_export_all_menu)
        editor_top_bar.addWidget(self.export_all_btn)

        self.nk_fc_btn = _make_button("🃏 Flashcard")
        self.nk_fc_btn.setObjectName("NKActionBtn")
        self.nk_fc_btn.setToolTip("Genera flashcard d'esame da questa nota")
        self.nk_fc_btn.clicked.connect(self._on_nk_extract_flashcards_clicked)
        editor_top_bar.addWidget(self.nk_fc_btn)

        self.nk_copy_btn = _make_button("📋")
        self.nk_copy_btn.setObjectName("NKActionBtn")
        self.nk_copy_btn.setToolTip("Copia Markdown negli appunti")
        self.nk_copy_btn.clicked.connect(lambda: self._copy_to_clipboard(self.nk_editor_body.get_markdown_text()))
        editor_top_bar.addWidget(self.nk_copy_btn)

        self.nk_del_btn = _make_button("🗑️", "danger")
        self.nk_del_btn.setObjectName("NKActionBtn")
        self.nk_del_btn.setToolTip("Elimina nota corrente")
        self.nk_del_btn.clicked.connect(self._on_nk_delete_clicked)
        editor_top_bar.addWidget(self.nk_del_btn)

        self.nk_ask_whis_btn = _make_button("✨ Whis", "accent")
        self.nk_ask_whis_btn.setObjectName("NKActionBtn")
        self.nk_ask_whis_btn.setToolTip("Chiedi a Whis di spiegare questo appunto")
        self.nk_ask_whis_btn.clicked.connect(self._on_nk_ask_whis_clicked)
        editor_top_bar.addWidget(self.nk_ask_whis_btn)

        self.nk_studio_btn = _make_button("✦ Vista Studio")
        self.nk_studio_btn.setObjectName("NKRestorePill")
        self.nk_studio_btn.setCheckable(True)
        self.nk_studio_btn.setToolTip("Alterna tra Modifica WYSIWYG e Vista Studio impaginata con formule LaTeX (Ctrl+E)")
        self.nk_studio_btn.clicked.connect(self._on_nk_studio_btn_clicked)
        editor_top_bar.addWidget(self.nk_studio_btn)

        self.nk_studio_shortcut = QShortcut(QKeySequence("Ctrl+E"), col3)
        self.nk_studio_shortcut.activated.connect(self._on_nk_studio_shortcut)

        self.nk_focus_mode_btn = _make_button("⛶")
        self.nk_focus_mode_btn.setObjectName("NKRestorePill")
        self.nk_focus_mode_btn.setToolTip("Modalità Focus (nascondi/ripristina sidebar)")
        self.nk_focus_mode_btn.clicked.connect(self._toggle_nk_focus_mode)
        editor_top_bar.addWidget(self.nk_focus_mode_btn)

        col3_layout.addLayout(editor_top_bar)

        # Titolo edit senza scatola
        self.nk_editor_title = DictationGhostLineEdit(dark_mode=self._active_is_dark())
        self.nk_editor_title.setObjectName("NKEditorTitle")
        self.nk_editor_title.setPlaceholderText("Titolo della nota...")
        self.nk_editor_title.setMinimumHeight(36)
        col3_layout.addWidget(self.nk_editor_title)

        # Barra Metadati Compatta a Pillole (height: 26px)
        meta_grid = QHBoxLayout()
        meta_grid.setSpacing(6)

        self.nk_editor_subject = QLineEdit()
        self.nk_editor_subject.setObjectName("NKEditorPill")
        self.nk_editor_subject.setPlaceholderText("Materia")
        self.nk_editor_subject.setFixedHeight(26)
        self.nk_editor_subject.setMaximumWidth(150)
        meta_grid.addWidget(self.nk_editor_subject)

        sep1 = QLabel("›")
        sep1.setStyleSheet("color: #94A3B8; font-size: 13px; background: transparent;")
        meta_grid.addWidget(sep1)

        self.nk_editor_topic = QLineEdit()
        self.nk_editor_topic.setObjectName("NKEditorPill")
        self.nk_editor_topic.setPlaceholderText("Argomento")
        self.nk_editor_topic.setFixedHeight(26)
        self.nk_editor_topic.setMaximumWidth(150)
        meta_grid.addWidget(self.nk_editor_topic)

        sep2 = QLabel("·")
        sep2.setStyleSheet("color: #94A3B8; font-size: 14px; font-weight: bold; background: transparent;")
        meta_grid.addWidget(sep2)

        self.nk_editor_type = QComboBox()
        self.nk_editor_type.setObjectName("NKEditorPillCombo")
        self.nk_editor_type.addItems(["appunto", "sbobina", "slide"])
        self.nk_editor_type.setFixedHeight(26)
        meta_grid.addWidget(self.nk_editor_type)

        sep3 = QLabel("·")
        sep3.setStyleSheet("color: #94A3B8; font-size: 14px; font-weight: bold; background: transparent;")
        meta_grid.addWidget(sep3)

        self.nk_editor_profile = QComboBox()
        self.nk_editor_profile.setObjectName("NKEditorPillCombo")
        self.nk_editor_profile.addItems(["Alessandro", "Giada"])
        self.nk_editor_profile.setFixedHeight(26)
        meta_grid.addWidget(self.nk_editor_profile)

        meta_grid.addStretch(1)
        col3_layout.addLayout(meta_grid)

        # Sottile linea divisoria orizzontale
        editor_divider = QFrame()
        editor_divider.setObjectName("NKEditorDivider")
        editor_divider.setFixedHeight(1)
        col3_layout.addWidget(editor_divider)

        # Markdown + LaTeX Scientific Editor WYSIWYG stile Notion
        is_dark = self._active_is_dark()
        is_reading = getattr(self, "_reading_mode", False)
        self.nk_editor_body = ScientificNoteEditor(dark_mode=is_dark, reading_mode=is_reading)
        self.nk_editor_body.setObjectName("NKEditorBody")
        self.nk_editor_body.setPlaceholderText(
            "Scrivi o detta qui in Markdown + LaTeX ($...$), incolla immagini (Ctrl+V) o digita '/' per i comandi..."
        )
        self.nk_editor_body.setMinimumHeight(240)
        p_init = self._active_palette()
        self.nk_editor_body.set_sync_accent_color(
            p_init.get("accent") or p_init.get("nav_checked_border") or "#6366F1"
        )
        col3_layout.addWidget(self.nk_editor_body, 1)

        # Smart Audio Bar contestuale per le sbobine (nascosta di default)
        self.nk_audio_bar = AudioSyncBar(self)
        self.nk_audio_bar.hide()
        col3_layout.addWidget(self.nk_audio_bar)

        self.nk_audio_bar.positionChangedMs.connect(self.nk_editor_body.highlight_audio_position)
        self.nk_audio_bar.playbackStateChanged.connect(
            lambda playing: None if playing else self.nk_editor_body.clear_audio_highlight()
        )
        self.nk_editor_body.seekRequested.connect(
            lambda ms: self.nk_audio_bar.seek_ms(ms, auto_play=True)
        )
        self.nk_editor_body.togglePlayRequested.connect(self.nk_audio_bar.toggle_play_pause)

        # Auto-save silenzioso con debounce 800ms
        self._nk_loading_note = False
        self._nk_autosave_timer = QTimer(self)
        self._nk_autosave_timer.setSingleShot(True)
        self._nk_autosave_timer.setInterval(800)
        self._nk_autosave_timer.timeout.connect(self._perform_nk_autosave)

        self.nk_editor_title.textChanged.connect(self._on_nk_editor_modified)
        self.nk_editor_subject.textChanged.connect(self._on_nk_editor_modified)
        self.nk_editor_topic.textChanged.connect(self._on_nk_editor_modified)
        self.nk_editor_type.currentIndexChanged.connect(self._on_nk_editor_modified)
        self.nk_editor_profile.currentIndexChanged.connect(self._on_nk_editor_modified)
        self.nk_editor_body.markdownModified.connect(self._on_nk_editor_modified)
        self.nk_editor_body.summarizeRequested.connect(self._on_nk_summarize_clicked)
        self.nk_editor_body.flashcardsRequested.connect(self._on_nk_extract_flashcards_clicked)
        self.nk_editor_body.studioViewChanged.connect(self._on_nk_studio_view_changed)

        splitter.addWidget(col3)

        # Impedisci il collasso dell'Editor (col 2), consenti il collasso controllato di col 0 e 1
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, True)
        splitter.setCollapsible(2, False)
        for h_idx in (1, 2):
            handle = splitter.handle(h_idx)
            if handle is not None:
                handle.setCursor(Qt.CursorShape.SplitHCursor)
        splitter.splitterMoved.connect(lambda _pos, _idx: self._sync_nk_panel_toggles())

        # Layout a 2 colonne di default: Sidebar Unica 280px + Editor Gigante ~80%
        splitter.setSizes([280, 0, 920])
        nk_container_layout.addWidget(splitter, 1)
        main_vbox.addWidget(nk_container, 1)

        self._nk_selected_note_id = None
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None

        return page

    def _build_notes_page(self) -> QWidget:
        """Retrocompatibilità: rimanda a _build_flowstudy_notekeeper_page."""
        return self._build_flowstudy_notekeeper_page()

    def _on_nk_search_changed(self):
        self._refresh_notekeeper()

    def _toggle_nk_card_view(self, force_state: Optional[bool] = None):
        if not hasattr(self, "nk_col2"):
            return
        show = (not self.nk_col2.isVisible()) if force_state is None else bool(force_state)
        self.nk_col2.setVisible(show)
        if hasattr(self, "nk_card_view_btn"):
            self.nk_card_view_btn.setChecked(show)
        if show:
            total = max(sum(self.nk_splitter.sizes()), 1020)
            c1_w = 280 if self.nk_col1.isVisible() else 0
            self.nk_splitter.setSizes([c1_w, 280, max(420, total - c1_w - 280)])
        self._sync_nk_panel_toggles()

    def _is_nk_col_collapsed(self, col_idx: int) -> bool:
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        if not col.isVisible():
            return True
        sizes = self.nk_splitter.sizes()
        return col_idx < len(sizes) and sizes[col_idx] == 0

    def _sync_nk_panel_toggles(self):
        if not hasattr(self, "nk_splitter"):
            return
        col1_closed = self._is_nk_col_collapsed(0)
        col2_closed = self._is_nk_col_collapsed(1)
        self.nk_show_col1_in_col2_btn.setVisible(col1_closed and not col2_closed)
        self.nk_show_col1_btn.setVisible(col1_closed and col2_closed)
        self.nk_show_col2_btn.setVisible(False)
        if col1_closed and col2_closed:
            self.nk_focus_mode_btn.setToolTip("Ripristina Sidebar FlowNotes")
        else:
            self.nk_focus_mode_btn.setToolTip("Modalità Focus (nascondi sidebar)")

    def _hide_nk_col(self, col_idx: int):
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        col.hide()
        self._sync_nk_panel_toggles()

    def _show_nk_col(self, col_idx: int):
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        target_w = 280 if col_idx == 0 else 280
        col.show()
        sizes = self.nk_splitter.sizes()
        if len(sizes) == 3:
            total = sum(sizes)
            if sizes[col_idx] < 100:
                sizes[col_idx] = target_w
                other_idx = 1 if col_idx == 0 else 0
                other_w = sizes[other_idx] if self.nk_col1.isVisible() and self.nk_col2.isVisible() else 0
                sizes[2] = max(420, total - target_w - other_w)
                self.nk_splitter.setSizes(sizes)
        self._sync_nk_panel_toggles()

    def _toggle_nk_focus_mode(self):
        if self._is_nk_col_collapsed(0):
            self.nk_col1.show()
            total = max(sum(self.nk_splitter.sizes()), 1020)
            c2_w = 280 if self.nk_col2.isVisible() else 0
            self.nk_splitter.setSizes([280, c2_w, max(420, total - 280 - c2_w)])
        else:
            self.nk_col1.hide()
            self.nk_col2.hide()
        self._sync_nk_panel_toggles()

    def _make_note_leaf_item(self, note: Dict, p: Dict[str, str], max_len: int = 32) -> QTreeWidgetItem:
        clean_t = strip_markdown_syntax((note.get("title") or "Senza titolo").replace("Nota: ", "").strip()) or "Senza titolo"
        is_sbobina = (note.get("note_type") == "sbobina") or bool(note.get("audio_path"))
        tags_str = (note.get("tags") or "")
        is_studied = bool(note.get("is_studied")) or ("Studiata" in tags_str)
        icon_prefix = "🎙️" if is_sbobina else ("✅" if is_studied else "📄")
        label = f"{icon_prefix} {clean_t[:max_len]}"
        leaf = QTreeWidgetItem([label])
        leaf.setData(0, Qt.ItemDataRole.UserRole, ("note", note["id"]))
        leaf.setData(0, ROLE_NODE_DATA, ("note", note["id"]))
        leaf.setData(0, ROLE_IS_FOLDER, False)
        leaf.setToolTip(0, clean_t)
        leaf.setForeground(0, QBrush(QColor(p["text"])))
        leaf.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        return leaf

    def _refresh_notekeeper(self):
        """Ricarica l'albero gerarchico stile Samsung Notes (Tutte, Recenti, Cartelle/Sottocartelle -> Note)."""
        if not hasattr(self, "nk_tree"):
            return
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text
        sq = self.nk_search_input.text().strip() if hasattr(self, "nk_search_input") else ""

        all_notes = db.get_notes_by_hierarchy(
            profile=prof_filter,
            subject=None,
            topic=None,
            search_query=sq,
        )
        folders = db.get_folders_tree(profile=prof_filter)

        p = self._active_palette()
        if hasattr(self.nk_tree, "set_theme_palette"):
            self.nk_tree.set_theme_palette(p)

        self.nk_tree.blockSignals(True)
        self.nk_tree.clear()

        # 1. Voce fissa/collassabile in cima: Tutte le note
        all_item = QTreeWidgetItem(["Tutte le note"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("all",))
        all_item.setData(0, ROLE_NODE_DATA, ("all",))
        all_item.setData(0, ROLE_IS_FOLDER, True)
        all_item.setData(0, ROLE_LOCKED_FOLDER, True)
        all_item.setData(0, ROLE_COUNT, len(all_notes))
        all_item.setIcon(0, make_vector_folder_icon(p.get("accent", "#6366F1"), 16))
        f_top = QFont("Segoe UI", 9)
        f_top.setWeight(QFont.Weight.DemiBold)
        all_item.setFont(0, f_top)
        all_item.setForeground(0, QBrush(QColor(p["title"])))
        all_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.nk_tree.addTopLevelItem(all_item)

        # 2. Sezione rapida: Recenti (ultimi appunti modificati)
        recent_slice = all_notes[:8]
        recent_item = QTreeWidgetItem(["Recenti"])
        recent_item.setData(0, Qt.ItemDataRole.UserRole, ("recent",))
        recent_item.setData(0, ROLE_NODE_DATA, ("recent",))
        recent_item.setData(0, ROLE_IS_FOLDER, True)
        recent_item.setData(0, ROLE_LOCKED_FOLDER, True)
        recent_item.setData(0, ROLE_COUNT, len(recent_slice))
        recent_item.setIcon(0, make_vector_folder_icon(p.get("subtitle", "#9CA3AF"), 16))
        recent_item.setFont(0, f_top)
        recent_item.setForeground(0, QBrush(QColor(p["badge_text"])))
        recent_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        for n in recent_slice:
            recent_item.addChild(self._make_note_leaf_item(n, p, max_len=32))
        self.nk_tree.addTopLevelItem(recent_slice and recent_item or recent_item)
        recent_item.setExpanded(True)

        # 3. Albero gerarchico delle cartelle & sottocartelle
        notes_by_folder: Dict[Optional[int], List[Dict]] = {}
        for n in all_notes:
            fid = n.get("folder_id")
            notes_by_folder.setdefault(fid, []).append(n)

        children_by_parent: Dict[Optional[int], List[Dict]] = {}
        for f in folders:
            children_by_parent.setdefault(f.get("parent_id"), []).append(f)

        def _build_folder_node(f_dict: Dict) -> tuple[QTreeWidgetItem, int]:
            fid = f_dict["id"]
            color_hex = f_dict.get("color") or "#6366F1"
            f_item = QTreeWidgetItem([f_dict["name"]])
            f_item.setData(0, Qt.ItemDataRole.UserRole, ("folder", fid))
            f_item.setData(0, ROLE_NODE_DATA, ("folder", fid))
            f_item.setData(0, ROLE_IS_FOLDER, True)
            f_item.setData(0, ROLE_LOCKED_FOLDER, False)
            f_item.setData(0, ROLE_FOLDER_COLOR, color_hex)
            f_item.setIcon(0, make_vector_folder_icon(color_hex, 16))
            f_font = QFont("Segoe UI", 9)
            f_font.setWeight(QFont.Weight.DemiBold)
            f_item.setFont(0, f_font)
            f_item.setForeground(0, QBrush(QColor(p["title"])))
            f_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )

            direct_notes = notes_by_folder.get(fid, [])
            total_count = len(direct_notes)

            for sub_f in children_by_parent.get(fid, []):
                sub_item, sub_cnt = _build_folder_node(sub_f)
                total_count += sub_cnt
                if not sq or sub_cnt > 0:
                    f_item.addChild(sub_item)
                    sub_item.setExpanded(True)

            for n in direct_notes:
                f_item.addChild(self._make_note_leaf_item(n, p, max_len=30))

            f_item.setData(0, ROLE_COUNT, total_count)
            return f_item, total_count

        for root_f in children_by_parent.get(None, []):
            r_item, r_cnt = _build_folder_node(root_f)
            if not sq or r_cnt > 0:
                self.nk_tree.addTopLevelItem(r_item)
                r_item.setExpanded(True)

        # Eventuali note orfane senza folder_id
        orphan_notes = notes_by_folder.get(None, [])
        if orphan_notes:
            for n in orphan_notes:
                self.nk_tree.addTopLevelItem(self._make_note_leaf_item(n, p, max_len=32))

        self.nk_tree.blockSignals(False)
        self._refresh_nk_notes_list()

    def _sync_whis_study_context(self):
        """Sincronizza profilo, materia e argomento correnti con l'agente Whis."""
        if hasattr(self, "whis_agent") and self.whis_agent and hasattr(self.whis_agent, "set_study_context"):
            prof = self.nk_profile_combo.currentText() if hasattr(self, "nk_profile_combo") else "Alessandro"
            self.whis_agent.set_study_context(
                profile="Alessandro" if prof == "Tutti i profili" else prof,
                subject=self._nk_current_filter_subject or "Generale",
                topic=self._nk_current_filter_topic or "Generale"
            )

    def _on_nk_profile_changed(self):
        self._refresh_notekeeper()
        self._sync_whis_study_context()

    def _on_nk_tree_item_changed(self, current, _previous):
        if not current:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data[0] == "note":
            note_id = data[1]
            self._load_note_into_nk_editor(note_id)
            return
        if data[0] in ("all", "recent"):
            self._nk_current_filter_folder_id = None
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif data[0] == "folder":
            self._nk_current_filter_folder_id = data[1]
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif data[0] == "filter":
            self._nk_current_filter_folder_id = None
            _, subj, topic = data
            if subj == "all":
                self._nk_current_filter_subject = None
                self._nk_current_filter_topic = None
            elif topic == "all":
                self._nk_current_filter_subject = subj
                self._nk_current_filter_topic = None
            else:
                self._nk_current_filter_subject = subj
                self._nk_current_filter_topic = topic
        self._refresh_nk_notes_list()
        self._sync_whis_study_context()

    def _refresh_nk_notes_list(self):
        if not hasattr(self, "nk_notes_list"):
            return
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text
        sq = self.nk_search_input.text().strip() if hasattr(self, "nk_search_input") else ""

        notes = db.get_notes_by_hierarchy(
            profile=prof_filter,
            subject=self._nk_current_filter_subject,
            topic=self._nk_current_filter_topic,
            search_query=sq,
            folder_id=getattr(self, "_nk_current_filter_folder_id", None),
        )

        self.nk_notes_list.clear()
        if not notes:
            self.nk_notes_list.show_empty("Nessuna nota per la cartella selezionata.")
            return

        for n in notes:
            card = self._make_nk_note_card(n)
            self.nk_notes_list.add_card(card)

        # Se nessuna nota è selezionata, carica la prima nell'editor
        if getattr(self, "_nk_selected_note_id", None) is None and notes:
            self._load_note_into_nk_editor(notes[0]["id"])

    # -----------------------------------------------------------------
    # Gestione Cartelle Gerarchiche (Samsung Notes Style)
    # -----------------------------------------------------------------
    def _on_nk_create_subfolder(self, parent_id: int):
        name, ok = QInputDialog.getText(self, "Nuova Sottocartella", "Nome sottocartella:")
        if not ok or not name.strip():
            return
        prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        if prof == "Tutti i profili":
            prof = "Alessandro"
        db.create_folder(name=name.strip(), parent_id=parent_id, profile=prof)
        self._refresh_notekeeper()

    def _on_nk_rename_folder(self, folder_id: int):
        folders = {f["id"]: f for f in db.get_folders_tree()}
        cur_name = folders.get(folder_id, {}).get("name", "")
        new_name, ok = QInputDialog.getText(self, "Rinomina Cartella", "Nuovo nome:", text=cur_name)
        if not ok or not new_name.strip():
            return
        db.rename_folder(folder_id, new_name.strip())
        self._refresh_notekeeper()

    def _on_nk_change_folder_color(self, folder_id: int, color_hex: str):
        db.set_folder_color(folder_id, color_hex)
        self._refresh_notekeeper()

    def _on_nk_delete_folder(self, folder_id: int):
        notes_in_folder = db.get_notes_by_hierarchy(folder_id=folder_id)
        if notes_in_folder:
            box = QMessageBox(self)
            box.setWindowTitle("Elimina Cartella")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(f"La cartella contiene {len(notes_in_folder)} note (incluse eventuali sottocartelle).")
            box.setInformativeText("Vuoi spostare le note nella cartella Generale prima di eliminare?")
            btn_move = box.addButton("Sposta in Generale", QMessageBox.ButtonRole.AcceptRole)
            btn_del_all = box.addButton("Elimina tutto", QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = box.addButton("Annulla", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_cancel or clicked is None:
                return
            move_to_gen = (clicked is btn_move)
        else:
            res = QMessageBox.question(
                self,
                "Elimina Cartella",
                "Eliminare questa cartella vuota?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                return
            move_to_gen = True

        if getattr(self, "_nk_current_filter_folder_id", None) == folder_id:
            self._nk_current_filter_folder_id = None
        db.delete_folder(folder_id, move_notes_to_generale=move_to_gen)
        self._refresh_notekeeper()

    def _on_nk_note_moved(self, note_id: int, target_folder_id: int):
        if db.move_note_to_folder(note_id, target_folder_id):
            self._refresh_notekeeper()

    def _on_nk_folder_moved(self, folder_id: int, target_parent_id: object):
        parent_val = int(target_parent_id) if target_parent_id is not None else None
        if db.move_folder(folder_id, parent_val):
            self._refresh_notekeeper()

    def _on_nk_toggle_studied(self, note_id: int):
        db.toggle_note_studied(note_id)
        self._refresh_notekeeper()

    # -----------------------------------------------------------------
    # Command Palette Globale (Ctrl+K)
    # -----------------------------------------------------------------
    def _open_command_palette(self):
        callbacks = {
            "open_note": self._cp_open_note,
            "open_folder": self._cp_open_folder,
            "open_event": self._cp_open_event,
            "use_dictation": self._cp_use_dictation,
            "new_note": self._cp_new_note,
            "new_sbobina": self._cp_new_sbobina,
            "toggle_reading": self._cp_toggle_reading,
            "set_theme": self._set_theme,
            "navigate": self._on_nav_clicked,
        }
        dlg = GlobalCommandPalette(
            parent=self,
            palette=self._active_palette(),
            themes_map=_THEME_PALETTES,
            callbacks=callbacks,
            reading_mode_active=bool(getattr(self, "_reading_mode", False)),
        )
        dlg.exec()

    def _cp_open_note(self, note_id: Optional[int], match_line: int = 0, query: str = ""):
        if not note_id:
            return
        self._on_nav_clicked(1)
        self._load_note_into_nk_editor(note_id)
        if hasattr(self, "nk_editor_body") and match_line > 0:
            doc = self.nk_editor_body.document()
            block = doc.findBlockByNumber(match_line)
            if block.isValid():
                cur = QTextCursor(block)
                self.nk_editor_body.setTextCursor(cur)
                self.nk_editor_body.ensureCursorVisible()
            self.nk_editor_body.setFocus()

    def _cp_open_folder(self, folder_id: Optional[int]):
        self._on_nav_clicked(1)
        if folder_id is not None:
            self._nk_current_filter_folder_id = folder_id
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
            self._refresh_notekeeper()

    def _cp_open_event(self, event_date: str):
        self._on_nav_clicked(7)
        if event_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(event_date[:10], "yyyy-MM-dd")
            if qd.isValid():
                self.ft_calendar.setSelectedDate(qd)
                if hasattr(self, "_refresh_flowtime_page"):
                    self._refresh_flowtime_page()

    def _cp_use_dictation(self, text: str):
        if not text:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        if (
            hasattr(self, "pages")
            and self.pages.currentIndex() == 1
            and hasattr(self, "nk_editor_body")
        ):
            self.nk_editor_body.insertPlainText(text)
            self.nk_editor_body.setFocus()

    def _cp_new_note(self):
        self._on_nav_clicked(1)
        if hasattr(self, "_on_nk_new_note_clicked"):
            self._on_nk_new_note_clicked()

    def _cp_new_sbobina(self):
        self._on_nav_clicked(1)
        if hasattr(self, "_on_sbobina_clicked"):
            self._on_sbobina_clicked()

    def _cp_toggle_reading(self):
        new_state = not bool(getattr(self, "_reading_mode", False))
        if hasattr(self, "reading_mode_btn"):
            self.reading_mode_btn.setChecked(new_state)
        self._toggle_reading_mode(new_state)

    def _make_nk_note_card(self, note: Dict) -> QFrame:
        card = QFrame()
        card.setObjectName("NKNoteCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        note_id = note.get("id")
        is_selected = (note_id == getattr(self, "_nk_selected_note_id", None))
        card.setProperty("selected", is_selected)
        card.setProperty("note_id", note_id)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # 1. Rimuovi il prefisso automatico "Nota: " e tag Markdown dal titolo visualizzato
        raw_title = (note.get("title") or "Senza titolo").strip()
        if raw_title.lower().startswith("nota: "):
            raw_title = raw_title[6:].strip() or "Senza titolo"
        raw_title = strip_markdown_syntax(raw_title) or "Senza titolo"

        t_lbl = QLabel(raw_title)
        t_lbl.setObjectName("NKNoteTitle")
        t_lbl.setWordWrap(True)
        t_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.addWidget(t_lbl)

        # 2. Non ripetere il corpo se è uguale al titolo o più corto di 5 caratteri
        #    (ignora puntini di sospensione '...', punteggiatura finale e case)
        content = (note.get("content_markdown") or "").strip()
        norm_title = re.sub(r'[.…\s]+$', '', raw_title).strip().lower()
        norm_body = re.sub(r'[.…\s]+$', '', content).strip().lower()
        cleaned_body = content
        if norm_title and norm_body.startswith(norm_title):
            if len(content) <= len(raw_title) + 15:
                cleaned_body = ""
            else:
                cleaned_body = content[len(norm_title):].lstrip(" \t\r\n:.-—–…")
        if cleaned_body and len(cleaned_body) >= 5 and norm_body != norm_title:
            preview = (cleaned_body[:95].replace('\n', ' ') + "...") if len(cleaned_body) > 95 else cleaned_body.replace('\n', ' ')
            p_lbl = QLabel(preview)
            p_lbl.setObjectName("NKNotePreview")
            p_lbl.setWordWrap(True)
            p_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout.addWidget(p_lbl)

        # 3. Metadati minimali su una sola riga discreta (dot colorato + tipo + eventuale profilo + ora)
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.setSpacing(5)

        nt = (note.get("note_type") or "appunto").lower()
        if nt == "sbobina":
            dot_color, type_name = "#10B981", "Sbobina"
        elif nt == "slide":
            dot_color, type_name = "#0EA5E9", "Slide"
        else:
            dot_color, type_name = "#6366F1", "Appunto"

        dot_lbl = QLabel("●")
        dot_lbl.setStyleSheet(f"color: {dot_color}; font-size: 9px; background: transparent;")
        meta_row.addWidget(dot_lbl)

        show_all_profiles = (
            hasattr(self, "nk_profile_combo")
            and self.nk_profile_combo.currentText().strip() == "Tutti i profili"
        )
        type_text = f"{type_name} · {note.get('profile') or 'Alessandro'}" if show_all_profiles else type_name
        type_lbl = QLabel(type_text)
        type_lbl.setObjectName("NKNoteMeta")
        meta_row.addWidget(type_lbl)
        meta_row.addStretch(1)

        dt = _format_timestamp(note.get("created_at"))
        dt_lbl = QLabel(dt)
        dt_lbl.setObjectName("NKNoteMeta")
        meta_row.addWidget(dt_lbl)
        layout.addLayout(meta_row)

        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.mousePressEvent = lambda _e, nid=note_id: self._load_note_into_nk_editor(nid)
        return card

    def _load_note_into_nk_editor(self, note_id: int):
        note = db.get_study_note_by_id(note_id)
        if not note:
            return
        self._nk_selected_note_id = note_id
        # Aggiorna stato attivo su tutte le card nella lista centrale
        if hasattr(self, "nk_notes_list"):
            for i in range(self.nk_notes_list.vbox.count()):
                w = self.nk_notes_list.vbox.itemAt(i).widget()
                if w is not None and w.objectName() == "NKNoteCard":
                    is_sel = (w.property("note_id") == note_id)
                    if bool(w.property("selected")) != is_sel:
                        w.setProperty("selected", is_sel)
                        w.style().unpolish(w)
                        w.style().polish(w)
        if hasattr(self, "nk_editor_title"):
            self._nk_loading_note = True
            try:
                if hasattr(self, "_nk_autosave_timer"):
                    self._nk_autosave_timer.stop()
                title_txt = (note.get("title") or "").strip()
                if title_txt.lower().startswith("nota: "):
                    title_txt = title_txt[6:].strip()
                self.nk_editor_title.setText(title_txt)
                self.nk_editor_profile.setCurrentText(note.get("profile") or "Alessandro")
                self.nk_editor_subject.setText(note.get("subject") or "Generale")
                self.nk_editor_topic.setText(note.get("topic") or "Generale")
                ntype = (note.get("note_type") or "appunto").lower()
                idx = self.nk_editor_type.findText(ntype, Qt.MatchFlag.MatchFixedString)
                if idx >= 0:
                    self.nk_editor_type.setCurrentIndex(idx)
                self.nk_editor_body.set_markdown_text(note.get("content_markdown") or "")
                sync_data = load_note_audio_sync(note_id)
                if sync_data and sync_data.get("audio_path"):
                    self.nk_editor_body.set_audio_sync_data(sync_data.get("segments") or [])
                    if hasattr(self, "nk_audio_bar"):
                        self.nk_audio_bar.load_audio(sync_data["audio_path"])
                else:
                    self.nk_editor_body.set_audio_sync_data(None)
                    if hasattr(self, "nk_audio_bar"):
                        self.nk_audio_bar.unload()
                self._set_nk_autosave_badge(saved=True)
            finally:
                self._nk_loading_note = False

    def _set_nk_autosave_badge(self, saved: bool, custom_text: Optional[str] = None):
        if not hasattr(self, "nk_autosave_badge"):
            return
        if saved:
            self.nk_autosave_badge.setText(custom_text or "✓ Salvato automaticamente")
            self.nk_autosave_badge.setStyleSheet(
                "background: transparent; border: none; color: #787774; font-size: 12px; font-weight: 400;"
            )
        else:
            self.nk_autosave_badge.setText(custom_text or "Modifica in corso...")
            self.nk_autosave_badge.setStyleSheet(
                "background: transparent; border: none; color: #787774; font-size: 12px; font-weight: 400;"
            )

    def _on_nk_editor_modified(self, *_args):
        if getattr(self, "_nk_loading_note", False):
            return
        if hasattr(self, "nk_editor_body") and self.nk_editor_body.is_studio_view:
            return
        self._set_nk_autosave_badge(saved=False)
        if hasattr(self, "_nk_autosave_timer"):
            self._nk_autosave_timer.start(800)

    def _perform_nk_autosave(self):
        if getattr(self, "_nk_loading_note", False):
            return
        raw_title = self.nk_editor_title.text().strip()
        content = self.nk_editor_body.get_markdown_text().strip()
        note_id = getattr(self, "_nk_selected_note_id", None)

        if note_id is None and not raw_title and not content:
            self._set_nk_autosave_badge(saved=True)
            return

        title = raw_title or "Appunto senza titolo"
        prof = self.nk_editor_profile.currentText().strip() or "Alessandro"
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        ntype = self.nk_editor_type.currentText().strip() or "appunto"

        self._autosave_thread = _AutoSaveNoteThread(note_id, title, content, prof, subj, top, ntype)
        self._autosave_thread.saved.connect(self._on_nk_autosave_finished)
        self._autosave_thread.error.connect(lambda err: self._set_nk_autosave_badge(saved=False, custom_text="⚠ Errore salvataggio"))
        self._autosave_thread.start()

    def _on_nk_autosave_finished(self, saved_id: int, title: str, content: str, subj: str, top: str, ntype: str):
        was_new = (getattr(self, "_nk_selected_note_id", None) is None)
        self._nk_selected_note_id = saved_id
        self._set_nk_autosave_badge(saved=True)

        # Aggiorna la card nella lista centrale senza mai toccare il cursore/scroll dell'editor
        updated_in_place = False
        if not was_new and hasattr(self, "nk_notes_list"):
            for i in range(self.nk_notes_list.vbox.count()):
                w = self.nk_notes_list.vbox.itemAt(i).widget()
                if w is not None and w.objectName() == "NKNoteCard" and w.property("note_id") == saved_id:
                    for lbl in w.findChildren(QLabel):
                        if lbl.objectName() == "NKNoteTitle":
                            lbl.setText(title)
                            updated_in_place = True
                            break
                    break
        if was_new or not updated_in_place:
            self._refresh_notekeeper()

    def _on_nk_studio_btn_clicked(self, checked: bool):
        if hasattr(self, "nk_editor_body"):
            self.nk_editor_body.set_studio_view(checked)

    def _on_nk_studio_shortcut(self):
        if hasattr(self, "nk_editor_body"):
            self.nk_editor_body.toggle_studio_view()

    def _on_nk_studio_view_changed(self, is_studio: bool):
        if hasattr(self, "nk_studio_btn"):
            self.nk_studio_btn.blockSignals(True)
            self.nk_studio_btn.setChecked(is_studio)
            self.nk_studio_btn.setText("✏️ Modifica" if is_studio else "✦ Vista Studio")
            self.nk_studio_btn.blockSignals(False)

    def _on_nk_summarize_clicked(self):
        text = self.nk_editor_body.get_markdown_text().strip()
        if not text:
            QMessageBox.information(self, "Riassumi AI", "Scrivi prima del testo nella nota per generare il riassunto.")
            return
        self._set_nk_autosave_badge(saved=False, custom_text="✨ Riassunto AI in corso...")
        self._summarize_thread = _SummarizeNoteThread(text)
        self._summarize_thread.finished.connect(self._on_nk_summarize_finished)
        self._summarize_thread.error.connect(self._on_nk_summarize_error)
        self._summarize_thread.start()

    def _on_nk_summarize_finished(self, summary: str):
        quoted_lines = "\n".join(f"> {line}" if line.strip() else ">" for line in summary.splitlines())
        box = f"\n\n> 📌 **Riassunto AI**\n>\n{quoted_lines}\n"
        cur_md = self.nk_editor_body.get_markdown_text().rstrip()
        self.nk_editor_body.set_markdown_text(cur_md + box)
        self._on_nk_editor_modified()

    def _on_nk_summarize_error(self, err: str):
        self._set_nk_autosave_badge(saved=True)
        QMessageBox.warning(self, "Errore Riassunto AI", f"Impossibile generare il riassunto con Qwen 2.5:\n{err}")

    def _on_nk_new_note_clicked(self):
        self._nk_loading_note = True
        try:
            if hasattr(self, "_nk_autosave_timer"):
                self._nk_autosave_timer.stop()
            self._nk_selected_note_id = None
            self.nk_editor_title.setText("")
            self.nk_editor_subject.setText(self._nk_current_filter_subject or "Generale")
            self.nk_editor_topic.setText(self._nk_current_filter_topic or "Generale")
            if hasattr(self, "nk_editor_body") and self.nk_editor_body.is_studio_view:
                self.nk_editor_body.set_studio_view(False)
            self.nk_editor_body.set_markdown_text("")
            self.nk_editor_body.set_audio_sync_data(None)
            if hasattr(self, "nk_audio_bar"):
                self.nk_audio_bar.unload()
            self._set_nk_autosave_badge(saved=True)
            self.nk_editor_title.setFocus()
        finally:
            self._nk_loading_note = False

    def _on_nk_delete_clicked(self):
        if not getattr(self, "_nk_selected_note_id", None):
            return
        reply = QMessageBox.question(
            self, "Elimina Nota",
            "Vuoi davvero eliminare questa nota e tutte le eventuali flashcard e relazioni associate?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._nk_loading_note = True
            try:
                if hasattr(self, "_nk_autosave_timer"):
                    self._nk_autosave_timer.stop()
                db.delete_study_note(self._nk_selected_note_id)
                self._nk_selected_note_id = None
                self.nk_editor_title.setText("")
                self.nk_editor_body.set_markdown_text("")
                self.nk_editor_body.set_audio_sync_data(None)
                if hasattr(self, "nk_audio_bar"):
                    self.nk_audio_bar.unload()
                self._set_nk_autosave_badge(saved=True)
            finally:
                self._nk_loading_note = False
            self._refresh_notekeeper()

    def _on_nk_ask_whis_clicked(self):
        title = self.nk_editor_title.text().strip()
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        content = self.nk_editor_body.get_markdown_text().strip()

        if content:
            try:
                pyperclip.copy(content)
            except Exception:
                pass

        self.pages.setCurrentIndex(4)
        if 4 in self.nav_buttons_by_page:
            self.nav_buttons_by_page[4].setChecked(True)
        self._refresh_current_page()

        if hasattr(self, "whis_input"):
            self.whis_input.setText(f"Spiegami questo appunto su {subj} - {top}: {title}")
            self.whis_input.setFocus()

    def _on_nk_new_subject_clicked(self):
        prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        dlg = NewSubjectTopicDialog(self, default_profile=prof)
        if dlg.exec():
            db.sync_folders_from_notes()
            self._refresh_notekeeper()

    def _on_nk_extract_flashcards_clicked(self):
        if not getattr(self, "_nk_selected_note_id", None):
            QMessageBox.warning(self, "Attenzione", "Seleziona o salva prima una nota nell'editor.")
            return
        note = db.get_study_note_by_id(self._nk_selected_note_id)
        if not note:
            return
        self._extract_flashcards_clicked(note, self.nk_fc_btn)

    # -----------------------------------------------------------------
    # FLASHCARD ESAME (PILASTRO FLOWSTUDY - SCHEDA DEDICATA)
    # -----------------------------------------------------------------
    def _build_flowstudy_flashcards_page(self) -> QWidget:
        page, layout = self._make_page("🃏 Flashcard Esame", "Ripasso attivo e interrogazione sui concetti chiave estratti dalle tue sbobine.")
        layout.insertWidget(0, self._build_flownotes_view_bar("fn_fc_view_btns"), 0, Qt.AlignmentFlag.AlignLeft)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.fc_search = QLineEdit()
        self.fc_search.setPlaceholderText("🔍 Cerca nelle flashcard d'esame...")
        self.fc_search.setMinimumHeight(36)
        self.fc_search.textChanged.connect(self._refresh_flashcards_page)
        toolbar.addWidget(self.fc_search, 1)

        self.fc_profile_filter = QComboBox()
        self.fc_profile_filter.addItems(["Tutti i profili", "Alessandro", "Giada"])
        self.fc_profile_filter.setMinimumHeight(36)
        self.fc_profile_filter.currentIndexChanged.connect(self._refresh_flashcards_page)
        toolbar.addWidget(self.fc_profile_filter)

        layout.addLayout(toolbar)

        self.fc_list = ScrollList("Nessuna flashcard d'esame presente. Creala da un appunto cliccando '🧠 Flashcard'.")
        layout.addWidget(self.fc_list, 1)
        return page

    def _refresh_flashcards_page(self):
        if not hasattr(self, "fc_list"):
            return
        query = self.fc_search.text().strip().lower() if hasattr(self, "fc_search") else ""
        prof = self.fc_profile_filter.currentText().strip() if hasattr(self, "fc_profile_filter") else "Tutti i profili"

        all_fc = db.get_all_study_notes(note_type="Flashcard")
        if prof != "Tutti i profili":
            all_fc = [f for f in all_fc if (f.get("profile") or "Alessandro") == prof or (prof in (f.get("tags") or ""))]

        if query:
            all_fc = [
                f for f in all_fc
                if query in (f.get("title") or "").lower()
                or query in (f.get("content_markdown") or "").lower()
                or query in (f.get("tags") or "").lower()
            ]

        self.fc_list.clear()
        if not all_fc:
            self.fc_list.show_empty("Nessuna flashcard trovata con i filtri attuali.")
            return

        for fc in all_fc:
            self.fc_list.add_card(self._make_flashcard_study_card(fc))

    def _make_flashcard_study_card(self, fc: Dict) -> QFrame:
        card, layout = _make_card()
        fc_id = fc.get("id")
        title = fc.get("title") or "Domanda d'esame"
        q_text = title.replace("Flashcard:", "").strip()
        content = fc.get("content_markdown") or ""

        ans_text = content
        if "**Domanda:**" in content and "**Risposta:**" in content:
            parts = content.split("**Risposta:**", 1)
            q_part = parts[0].replace("**Domanda:**", "").strip()
            if q_part:
                q_text = q_part
            if len(parts) > 1:
                ans_text = parts[1].strip()

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label("🃏 Flashcard", "Badge"))
        subj = fc.get("subject") or "Generale"
        top = fc.get("topic") or "Generale"
        if subj != "Generale":
            meta_row.addWidget(_make_label(f"📚 {subj}", "BadgeMuted"))
        if top != "Generale":
            meta_row.addWidget(_make_label(f"🏷️ {top}", "BadgeMuted"))
        meta_row.addStretch(1)

        del_btn = _make_button("🗑️", "danger")
        del_btn.setToolTip("Elimina flashcard")
        del_btn.clicked.connect(lambda _c=False, fid=fc_id: self._delete_flashcard_from_page(fid))
        meta_row.addWidget(del_btn)
        layout.addLayout(meta_row)

        q_label = QLabel(f"<b>Domanda:</b> {q_text}")
        q_label.setWordWrap(True)
        q_label.setStyleSheet("color: #38BDF8; font-size: 13.5px; font-weight: 600; margin-top: 4px;")
        layout.addWidget(q_label)

        ans_frame = QFrame()
        ans_frame.setStyleSheet("background-color: #131722; border: 1px dashed #334155; border-radius: 6px; padding: 8px;")
        ans_layout = QVBoxLayout(ans_frame)
        ans_label = QLabel(ans_text)
        ans_label.setWordWrap(True)
        ans_label.setStyleSheet("color: #E2E8F0; font-size: 12.5px; line-height: 1.4;")
        ans_layout.addWidget(ans_label)
        ans_frame.hide()
        layout.addWidget(ans_frame)

        toggle_btn = _make_button("👁️ Mostra Risposta")
        def _toggle(f=ans_frame, b=toggle_btn):
            if f.isVisible():
                f.hide()
                b.setText("👁️ Mostra Risposta")
            else:
                f.show()
                b.setText("🙈 Nascondi Risposta")
        toggle_btn.clicked.connect(_toggle)
        layout.addWidget(toggle_btn)

        return card

    def _delete_flashcard_from_page(self, fid: int):
        db.delete_study_note(fid)
        self._refresh_flashcards_page()

    # -----------------------------------------------------------------
    # GRAFO CONCETTUALE (PILASTRO FLOWSTUDY - OBSIDIAN STYLE)
    # -----------------------------------------------------------------
    def _build_flowstudy_graph_page(self) -> QWidget:
        page, layout = self._make_page("🕸️ Grafo Concettuale", "Visualizzazione delle relazioni tra materie, argomenti, formule e sbobine (Obsidian Style).")
        layout.insertWidget(0, self._build_flownotes_view_bar("fn_graph_view_btns"), 0, Qt.AlignmentFlag.AlignLeft)

        self.graph_widget = KnowledgeGraphWidget(on_node_click=self._on_graph_node_clicked)
        layout.addWidget(self.graph_widget, 1)
        return page

    def _on_graph_node_clicked(self, note_id: int):
        """Quando si clicca un nodo nel Grafo, passa al Notekeeper e carica la nota nell'editor."""
        self.pages.setCurrentIndex(1)
        if 1 in self.nav_buttons_by_page:
            self.nav_buttons_by_page[1].setChecked(True)
        self._refresh_notekeeper()
        self._load_note_into_nk_editor(note_id)

    def _filtered_notes(self) -> List[Dict]:
        return db.get_notes()

    def _refresh_notes(self):
        self._refresh_notekeeper()

    def _make_note_card(self, n: Dict) -> QFrame:
        card, layout = _make_card()
        note_id = n.get("id")
        note_type = (n.get("note_type") or "").strip()
        is_quick_note = (note_type == "Appunto Rapido" or "rapido" in note_type.lower())

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(n.get("created_at")), "CardMeta"))

        if is_quick_note:
            meta_row.addWidget(_make_label("⚡ Appunto", "BadgeQuickNote"))
        elif note_type:
            badge_text = "🎙️ Sbobina" if note_type.lower() == "sbobina" else note_type
            meta_row.addWidget(_make_label(badge_text, "Badge"))

        if n.get("tags"):
            raw_tags = str(n["tags"])
            # Normalizzazione retrocompatibile tag per visualizzazione ("Profilo: Lei" -> "Profilo: Giada")
            norm_tags = raw_tags.replace("Profilo: Lei", "Profilo: Giada")
            norm_tokens = [(t or "").strip() for t in norm_tags.split(",") if (t or "").strip()]
            norm_tokens = ["Giada" if t in ("Lei", "Profilo: Lei") else t for t in norm_tokens]
            display_tags = ", ".join(norm_tokens)
            meta_row.addWidget(_make_label(display_tags, "BadgeMuted"))
        meta_row.addStretch(1)

        content = n.get("content_markdown") or ""

        copy_btn = _make_button("Copia")
        copy_btn.clicked.connect(lambda _c=False, s=content: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)

        # Il pulsante Flashcard compare solo su Sbobine o Note madri (mai su Appunti Rapidi o Flashcard)
        if not is_quick_note and note_type != "Flashcard":
            flashcard_btn = _make_button("🧠 Flashcard")
            flashcard_btn.clicked.connect(lambda _c=False, note_dict=n, btn=flashcard_btn: self._extract_flashcards_clicked(note_dict, btn))
            meta_row.addWidget(flashcard_btn)

        export_single_btn = _make_button("Esporta ▾")
        export_single_btn.clicked.connect(lambda _c=False, note_dict=n, btn=export_single_btn: self._show_export_single_menu(note_dict, btn))
        meta_row.addWidget(export_single_btn)

        # Gestione anteprima compatta ed espansione su richiesta (solo per Sbobine o Note lunghe, NON per Appunti Rapidi)
        if not is_quick_note:
            is_long = len(content) > 280
            preview_text = (content[:280].rstrip() + "...") if is_long else content
            body_label = _make_label(preview_text, "CardBody", wrap=True)

            if is_long:
                expand_btn = _make_button("Espandi ▼")
                is_expanded = [False]

                def toggle_expand(b=expand_btn, lbl=body_label, full=content, prev=preview_text, state=is_expanded):
                    state[0] = not state[0]
                    if state[0]:
                        lbl.setText(full)
                        b.setText("Comprimi ▲")
                    else:
                        lbl.setText(prev)
                        b.setText("Espandi ▼")

                expand_btn.clicked.connect(toggle_expand)
                meta_row.addWidget(expand_btn)
        else:
            # Per Appunti Rapidi: mostra direttamente il testo completo in formato compatto
            body_label = _make_label(content, "CardBody", wrap=True)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, nid=note_id: self._delete_note(nid))
        meta_row.addWidget(del_btn)
        layout.addLayout(meta_row)

        if n.get("title") and n.get("title") != "Appunto Rapido" and n.get("title") != (content or "").strip():
            layout.addWidget(_make_label(n["title"], "CardTitle", wrap=True))
        layout.addWidget(body_label)

        # Raggruppamento gerarchico flashcard collegate sotto la sbobina madre (solo per Sbobine / Note di studio, escluse Appunti Rapidi)
        if not is_quick_note:
            flashcards = db.get_flashcards_for_note(note_id, n.get("title", ""))
            if flashcards:
                fc_count = len(flashcards)
                fc_toggle_btn = QPushButton(f"🗂️ Flashcard collegate ({fc_count}) ▾")
                fc_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                fc_toggle_btn.setStyleSheet(
                    "QPushButton { background-color: #161D2B; color: #38BDF8; border: 1px solid #1E293B; border-radius: 6px; padding: 6px 12px; font-weight: 600; font-size: 11.5px; text-align: left; }"
                    "QPushButton:hover { background-color: #1E293B; color: #7DD3FC; border-color: #38BDF8; }"
                )
                layout.addWidget(fc_toggle_btn)

                fc_container = QWidget()
                fc_layout = QVBoxLayout(fc_container)
                fc_layout.setContentsMargins(4, 4, 4, 4)
                fc_layout.setSpacing(6)
                fc_container.hide()

                def make_toggle_fc_container(container=fc_container, btn=fc_toggle_btn, cnt=fc_count):
                    def _toggle():
                        if container.isVisible():
                            container.hide()
                            btn.setText(f"🗂️ Flashcard collegate ({cnt}) ▾")
                        else:
                            container.show()
                            btn.setText(f"🗂️ Flashcard collegate ({cnt}) ▴")
                    return _toggle

                fc_toggle_btn.clicked.connect(make_toggle_fc_container())

                for fc in flashcards:
                    fc_content = fc.get("content_markdown") or ""
                    q_text = ""
                    a_text = ""
                    m = re.search(r'\*\*Domanda:\*\*\s*(.*?)\s*\*\*Risposta:\*\*\s*(.*)', fc_content, re.DOTALL | re.IGNORECASE)
                    if not m:
                        m = re.search(r'\*\*D:\*\*\s*(.*?)\s*\*\*R:\*\*\s*(.*)', fc_content, re.DOTALL | re.IGNORECASE)
                    if m:
                        q_text = m.group(1).strip()
                        a_text = m.group(2).strip()
                    else:
                        q_text = fc.get("title", "").replace("Flashcard:", "").strip() or "Domanda d'esame"
                        a_text = fc_content.strip()

                    fc_card = QFrame()
                    fc_card.setStyleSheet(
                        "QFrame { background-color: #0F141F; border: 1px solid #1E293B; border-radius: 6px; padding: 6px 10px; }"
                    )
                    fc_card_layout = QVBoxLayout(fc_card)
                    fc_card_layout.setContentsMargins(8, 6, 8, 6)
                    fc_card_layout.setSpacing(4)

                    q_row = QHBoxLayout()
                    q_label = QLabel(f"❓ {q_text}")
                    q_label.setWordWrap(True)
                    q_label.setStyleSheet("color: #F8FAFC; font-size: 12px; font-weight: bold;")
                    q_row.addWidget(q_label, 1)

                    ans_btn = _make_button("Mostra Risposta")
                    ans_btn.setMinimumHeight(24)
                    ans_btn.setStyleSheet(
                        "QPushButton { background-color: #1E293B; color: #38BDF8; border: 1px solid #334155; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 500; }"
                        "QPushButton:hover { background-color: #283548; color: #7DD3FC; }"
                    )

                    ans_label = QLabel(a_text)
                    ans_label.setWordWrap(True)
                    ans_label.setStyleSheet(
                        "background-color: #162032; color: #CBD5E1; border-left: 3px solid #10B981; padding: 6px 10px; border-radius: 4px; font-size: 11.5px; margin-top: 4px;"
                    )
                    ans_label.hide()

                    def make_ans_toggle(lbl=ans_label, b=ans_btn):
                        def _toggle():
                            if lbl.isVisible():
                                lbl.hide()
                                b.setText("Mostra Risposta")
                            else:
                                lbl.show()
                                b.setText("Nascondi Risposta")
                        return _toggle

                    ans_btn.clicked.connect(make_ans_toggle())
                    q_row.addWidget(ans_btn)

                    del_fc_btn = QPushButton("✕")
                    del_fc_btn.setFixedSize(22, 22)
                    del_fc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    del_fc_btn.setToolTip("Elimina questa flashcard")
                    del_fc_btn.setStyleSheet(
                        "QPushButton { background-color: transparent; color: #EF4444; border: 1px solid #7F1D1D; border-radius: 4px; font-size: 10px; font-weight: bold; }"
                        "QPushButton:hover { background-color: #7F1D1D; color: #FFFFFF; }"
                    )
                    del_fc_btn.clicked.connect(lambda _c=False, fid=fc["id"]: self._delete_flashcard(fid))
                    q_row.addWidget(del_fc_btn)

                    fc_card_layout.addLayout(q_row)
                    fc_card_layout.addWidget(ans_label)
                    fc_layout.addWidget(fc_card)

                layout.addWidget(fc_container)

        # Barra compatta Collegamenti & Backlink (stile Obsidian)
        links_box = QFrame()
        links_box.setObjectName("NoteLinksBox")
        links_box.setStyleSheet("""
            QFrame#NoteLinksBox {
                background-color: #0E131F;
                border: 1px solid #1E293B;
                border-radius: 6px;
                padding: 4px 8px;
                margin-top: 4px;
            }
        """)
        links_row = QHBoxLayout(links_box)
        links_row.setContentsMargins(6, 4, 6, 4)
        links_row.setSpacing(6)

        lbl_icon = QLabel("🔗 Collegamenti:")
        lbl_icon.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: bold;")
        links_row.addWidget(lbl_icon)

        note_links = db.get_linked_notes(note_id)
        if note_links:
            for lk in note_links:
                is_out = (lk.get("direction") == "outgoing")
                dir_arrow = "↗" if is_out else "↙"
                type_icon = "🎙️" if (lk.get("note_type") or "").lower() == "sbobina" else "⚡"
                raw_t = (lk.get("title") or "Nota").replace("Sbobina:", "").strip()
                trunc_t = (raw_t[:20] + "…") if len(raw_t) > 20 else raw_t

                pill_btn = QPushButton(f"{dir_arrow} {type_icon} {trunc_t}")
                pill_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                dir_label = "Collegamento in uscita" if is_out else "Backlink in entrata"
                pill_btn.setToolTip(
                    f"🔗 [{dir_label}]\n"
                    f"Titolo: {lk.get('title')}\n"
                    f"Tipo: {lk.get('note_type', 'Nota')}\n"
                    f"Motivo: {lk.get('reason') or 'Correlazione concettuale'}\n\n"
                    f"(Clicca per scorrere ed evidenziare la nota)"
                )
                pill_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #162032;
                        color: #7DD3FC;
                        border: 1px solid #0369A1;
                        border-radius: 11px;
                        padding: 2px 9px;
                        font-size: 11px;
                        font-weight: 500;
                    }
                    QPushButton:hover {
                        background-color: #0284C7;
                        color: #FFFFFF;
                        border-color: #38BDF8;
                    }
                """)
                target_nid = lk.get("note_id")
                pill_btn.clicked.connect(lambda _c=False, tid=target_nid: self._scroll_to_and_highlight_note(tid))

                # Menu contestuale tasto destro per eliminare il collegamento
                pill_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                def make_pill_menu(btn=pill_btn, lid=lk["link_id"], note_t=lk.get("title")):
                    def _show_menu(pos):
                        menu = QMenu(self)
                        menu.setStyleSheet("""
                            QMenu {
                                background-color: #1A1F29;
                                color: #F8FAFC;
                                border: 1px solid #2D3545;
                                padding: 4px;
                                border-radius: 6px;
                            }
                            QMenu::item:selected {
                                background-color: #EF4444;
                            }
                        """)
                        del_act = menu.addAction(f"Elimina collegamento con '{note_t[:22]}...'")
                        del_act.triggered.connect(lambda: self._delete_note_link(lid))
                        menu.exec(btn.mapToGlobal(pos))
                    return _show_menu
                pill_btn.customContextMenuRequested.connect(make_pill_menu())
                links_row.addWidget(pill_btn)
        else:
            no_link_lbl = QLabel("Nessuno")
            no_link_lbl.setStyleSheet("color: #64748B; font-size: 11px; font-style: italic;")
            links_row.addWidget(no_link_lbl)

        # Pulsante + Collega manuale
        add_link_btn = QPushButton("+ Collega")
        add_link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_link_btn.setToolTip("Aggiungi un collegamento manuale verso un'altra nota o sbobina")
        add_link_btn.setStyleSheet("""
            QPushButton {
                background-color: #1E293B;
                color: #94A3B8;
                border: 1px dashed #334155;
                border-radius: 11px;
                padding: 2px 9px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #283548;
                color: #38BDF8;
                border-color: #38BDF8;
            }
        """)
        add_link_btn.clicked.connect(lambda _c=False, nid=note_id, t=n.get("title", ""): self._show_connect_note_dialog(nid, t))
        links_row.addWidget(add_link_btn)

        # Pulsante 🪄 Auto-Link semantico con Qwen
        autolink_btn = QPushButton("🪄 Auto-Link")
        autolink_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        autolink_btn.setToolTip("Cerca e collega automaticamente fino a 3 note correlate con Ollama Qwen")
        autolink_btn.setStyleSheet("""
            QPushButton {
                background-color: #1E293B;
                color: #C084FC;
                border: 1px dashed #6B21A8;
                border-radius: 11px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #3B0764;
                color: #E9D5FF;
                border-color: #C084FC;
            }
        """)
        autolink_btn.clicked.connect(lambda _c=False, nid=note_id, t=n.get("title", ""), c=content: self._auto_link_single_note(nid, t, c, autolink_btn))
        links_row.addWidget(autolink_btn)

        links_row.addStretch(1)
        layout.addWidget(links_box)

        self._note_cards[note_id] = card
        return card

    def _delete_flashcard(self, flashcard_id: int):
        db.delete_study_note(flashcard_id)
        self._refresh_notes()

    def _delete_note(self, note_id: int):
        db.delete_study_note(note_id)
        self._refresh_notes()
        if hasattr(self, "graph_widget"):
            self.graph_widget.load_data()

    def _delete_note_link(self, link_id: int):
        db.delete_note_link(link_id)
        self._refresh_notes()
        if hasattr(self, "graph_widget"):
            self.graph_widget.load_data()

    def _show_connect_note_dialog(self, source_id: int, source_title: str):
        dlg = ConnectNoteDialog(self, source_id, source_title)
        if dlg.exec():
            self._refresh_notes()
            if hasattr(self, "graph_widget"):
                self.graph_widget.load_data()

    def _start_autolinking(self, note_id: int, title: str, content: str, btn: Optional[QPushButton] = None):
        """Avvia l'auto-linking asincrono in background per non bloccare la UI."""
        self._autolink_thread = AutoLinkThread(note_id, title, content)
        def _on_done(nid, count):
            if btn is not None:
                btn.setEnabled(True)
                btn.setText("🪄 Auto-Link")
            if count > 0:
                self._refresh_notes()
                if hasattr(self, "graph_widget"):
                    self.graph_widget.load_data()
        self._autolink_thread.finished.connect(_on_done)
        self._autolink_thread.start()

    def _auto_link_single_note(self, note_id: int, title: str, content: str, btn: QPushButton):
        btn.setEnabled(False)
        btn.setText("⏳ Analisi...")
        self._start_autolinking(note_id, title, content, btn=btn)

    def _set_notes_view_mode(self, mode: int):
        """0 = Vista Quaderno Notekeeper (Pagina 1), 1 = Vista Grafo Concettuale (Pagina 3)."""
        target_page = 3 if mode == 1 else 1
        self.pages.setCurrentIndex(target_page)
        if target_page in getattr(self, "nav_buttons_by_page", {}):
            self.nav_buttons_by_page[target_page].setChecked(True)
        if mode == 1 and hasattr(self, "graph_widget"):
            self.graph_widget.load_data()
        elif mode == 0:
            self._refresh_notekeeper()

    def _on_graph_node_clicked(self, note_id: int):
        """Quando si clicca un nodo nel Grafo, passa al Notekeeper e carica la nota nell'editor."""
        self.pages.setCurrentIndex(1)
        if 1 in getattr(self, "nav_buttons_by_page", {}):
            self.nav_buttons_by_page[1].setChecked(True)
        self._refresh_notekeeper()
        self._load_note_into_nk_editor(note_id)

    def _scroll_to_and_highlight_note(self, target_note_id: int):
        """Passa al Notekeeper e carica la nota nell'editor."""
        self._on_graph_node_clicked(target_note_id)

    def _on_sbobina_clicked(self):
        if self._transcribe_thread and self._transcribe_thread.isRunning():
            QMessageBox.information(self, "Sbobinatura in corso", "Una sbobinatura è già in fase di elaborazione.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleziona file audio o video per sbobinatura",
            "",
            "File Audio e Video (*.mp3 *.m4a *.wav *.ogg *.mp4 *.mov *.mkv *.webm);;File Audio (*.mp3 *.m4a *.wav *.ogg);;File Video (*.mp4 *.mov *.mkv *.webm);;Tutti i file (*.*)"
        )
        if not file_path:
            return

        # Prompt opzionale per contesto e glossario della materia
        subject_context, ok = QInputDialog.getText(
            self,
            "Contesto / Materia della lezione",
            "Inserisci la materia o parole chiave (es. Diritto Sanitario, tirocinio, Legge Gelli-Bianco, colpa generica, imperizia, dolo):"
        )
        if not ok:
            subject_context = ""

        self.sbobina_btn.setEnabled(False)
        self.sbobina_btn.setText("⏳ In corso...")

        # Mostra barra di avanzamento e resetta stato
        self.sbobina_status_label.setText("Inizializzazione job...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()

        self._transcribe_thread = AudioTranscribeThread(
            file_path=file_path,
            subject_context=subject_context or "",
            engine=self.engine
        )
        self._transcribe_thread.progress.connect(self._on_sbobina_progress)
        self._transcribe_thread.finished.connect(self._on_sbobina_finished)
        self._transcribe_thread.error.connect(self._on_sbobina_error)
        self._transcribe_thread.cancelled.connect(self._on_sbobina_cancelled)
        self._transcribe_thread.start()

    def _on_sbobina_progress(self, msg: str, percent: int = -1):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)
            self.sbobina_btn.setText(f"⏳ {percent}%")
        else:
            self.sbobina_btn.setText("⏳ In corso...")

    def _on_cancel_sbobina_clicked(self):
        if self._transcribe_thread and self._transcribe_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento sbobinatura in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            self._transcribe_thread.cancel()
        if self._import_thread and self._import_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento importazione slide in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            try:
                self._import_thread.terminate()
            except Exception:
                pass
            self.sbobina_progress_box.hide()
            if hasattr(self, "import_slide_btn"):
                self.import_slide_btn.setEnabled(True)
        if self._cloud_queue_thread and self._cloud_queue_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento coda cloud in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            self._cloud_queue_thread.cancel()

    # -----------------------------------------------------------------
    # GESTORE CODA CLOUD CONTROLLATA (ANTI-LAG GAMING)
    # -----------------------------------------------------------------
    def _poll_cloud_queue(self):
        """Timer 10s: aggiorna il badge della coda cloud (sola lettura disco, zero CPU/GPU)."""
        try:
            count = self.cloud_sync.get_pending_count()
            if hasattr(self, "cloud_badge"):
                is_queue_running = bool(self._cloud_queue_thread and self._cloud_queue_thread.isRunning())
                if count > 0:
                    self.cloud_badge.setText(f"☁️ Coda Cloud ({count} in attesa)")
                    self.cloud_badge.setProperty("pending", True)
                    self.cloud_badge.style().unpolish(self.cloud_badge)
                    self.cloud_badge.style().polish(self.cloud_badge)
                    is_busy = is_queue_running or bool(self._transcribe_thread and self._transcribe_thread.isRunning())
                    self.cloud_process_btn.setVisible(True)
                    self.cloud_process_btn.setEnabled(not is_busy)
                else:
                    self.cloud_badge.setText("☁️ Coda Cloud (0)")
                    self.cloud_badge.setProperty("pending", False)
                    self.cloud_badge.style().unpolish(self.cloud_badge)
                    self.cloud_badge.style().polish(self.cloud_badge)
                    if not is_queue_running:
                        self.cloud_process_btn.setEnabled(False)
                        self.cloud_process_btn.setVisible(False)
        except Exception:
            pass

    def _on_process_cloud_queue_clicked(self):
        """Avvia l'elaborazione manuale sequenziale dei file in inbox_audio/."""
        if (self._cloud_queue_thread and self._cloud_queue_thread.isRunning()) or (
            self._transcribe_thread and self._transcribe_thread.isRunning()
        ):
            QMessageBox.information(
                self, "Elaborazione in corso", "Un'altra operazione acustica o di coda è già attiva."
            )
            return

        pending = self.cloud_sync.get_pending_queue()
        if not pending:
            QMessageBox.information(
                self,
                "Coda vuota",
                f"Nessun file audio trovato in attesa nella cartella:\n{self.cloud_sync.get_inbox_audio_dir()}",
            )
            return

        total = len(pending)
        reply = QMessageBox.question(
            self,
            "Avvia Elaborazione Coda Cloud",
            f"Trovati {total} file audio in attesa.\n\n"
            f"Verranno sbobinati sequenzialmente con Qwen 2.5 7B, salvati nel DB, "
            f"esportati in PDF/DOCX e archiviati in processed/.\n\n"
            f"Vuoi procedere ora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.cloud_process_btn.setEnabled(False)
        self.cloud_process_btn.setText("⏳ In elaborazione...")
        self.sbobina_btn.setEnabled(False)

        self.sbobina_status_label.setText(f"Avvio Coda Cloud: 0/{total} completati...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()

        self._cloud_queue_thread = CloudQueueProcessThread(engine=self.engine)
        self._cloud_queue_thread.progress.connect(self._on_cloud_queue_progress)
        self._cloud_queue_thread.finished.connect(self._on_cloud_queue_finished)
        self._cloud_queue_thread.error.connect(self._on_cloud_queue_error)
        self._cloud_queue_thread.cancelled.connect(self._on_cloud_queue_cancelled)
        self._cloud_queue_thread.start()

    def _on_cloud_queue_progress(self, msg: str, percent: int = -1):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)

    def _on_cloud_queue_finished(self, count: int, results: list):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        self._refresh_notes()

        msg = (
            f"Elaborazione completata per {count} lezioni audio dalla Coda Cloud!\n\n"
            f"• Sbobine e Flashcard d'esame registrate nel DB\n"
            f"• File PDF e DOCX esportati in 'outbox_study/'\n"
            f"• Indice 'library.json' aggiornato per la PWA Mobile\n"
            f"• File audio spostati in 'inbox_audio/processed/'"
        )
        QMessageBox.information(self, "Coda Cloud Completata", msg)

    def _on_cloud_queue_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        QMessageBox.critical(
            self, "Errore Coda Cloud", f"Si è verificato un errore durante l'elaborazione:\n{err_msg}"
        )

    def _on_cloud_queue_cancelled(self):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        self._refresh_notes()
        QMessageBox.information(self, "Coda Annullata", "L'elaborazione della coda cloud è stata interrotta.")

    def _on_sync_cloud_clicked(self):
        """Rigenera manualmente outbox_study/library.json."""
        try:
            data = self.cloud_sync.generate_library_json()
            out_path = os.path.join(self.cloud_sync.get_outbox_study_dir(), "library.json")
            total_notes = data["stats"]["total_notes"]
            total_fc = data["stats"]["total_flashcards"]
            QMessageBox.information(
                self,
                "Cloud Sincronizzato",
                f"Indice libreria per PWA Mobile aggiornato con successo!\n\n"
                f"• Percorso: {out_path}\n"
                f"• Note & Sbobine: {total_notes}\n"
                f"• Flashcard: {total_fc}\n\n"
                f"Google Drive Desktop sincronizzerà il file sul cloud.",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Errore Aggiornamento Cloud", f"Impossibile aggiornare library.json:\n{e}"
            )

    def _on_cloud_folder_clicked(self):
        """Permette di visualizzare o cambiare la cartella sincronizzata da Google Drive."""
        current_dir = self.cloud_sync.get_sync_base_dir()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Seleziona cartella sincronizzata Google Drive (FlowLocal_Sync)",
            current_dir,
        )
        if chosen and os.path.exists(chosen):
            self.cloud_sync.set_sync_base_dir(chosen)
            self._poll_cloud_queue()
            QMessageBox.information(
                self,
                "Cartella Cloud Impostata",
                f"Cartella principale impostata su:\n{chosen}\n\n"
                f"Sottocartelle verificate:\n"
                f"• inbox_audio/\n"
                f"• inbox_audio/processed/\n"
                f"• outbox_study/",
            )

    def _on_sbobina_cancelled(self):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina File Audio")
        QMessageBox.information(
            self,
            "Sbobinatura Annullata",
            "L'operazione di sbobinatura è stata interrotta con successo."
        )

    def _on_sbobina_finished(self, markdown: str, file_name: str, source_path: str = "", sync_data=None):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina")

        # Salva la sbobina nella tabella study_notes del database
        title = f"Sbobina: {file_name}"
        new_id = db.add_study_note(
            title=title,
            content_markdown=markdown,
            note_type="Sbobina",
            tags=f"Audio, {file_name}"
        )

        # Archivia audio in data/audio_notes/ e salva la mappa timestamp parola-per-parola
        if source_path and os.path.exists(source_path):
            try:
                archived_audio = archive_audio_for_note(new_id, source_path)
                if archived_audio:
                    db.update_study_note_audio_path(new_id, archived_audio)
                    segments = (sync_data or {}).get("segments", []) if isinstance(sync_data, dict) else []
                    save_note_audio_sync(new_id, archived_audio, segments)
            except Exception as sync_err:
                print(f"[FlowLocal Sbobina] Avviso salvataggio audio sync: {sync_err}")

        self._refresh_notes()
        if hasattr(self, "nk_tree"):
            self._refresh_notekeeper()
            self._load_note_into_nk_editor(new_id)
        self._start_autolinking(new_id, title, markdown)

        QMessageBox.information(
            self,
            "Sbobinatura completata",
            f"La lezione di '{file_name}' è stata sbobinata e salvata nelle tue Note con successo!"
        )

    def _on_sbobina_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina File Audio")
        QMessageBox.critical(
            self,
            "Errore Sbobinatura",
            f"Si è verificato un errore durante la sbobinatura dell'audio:\n{err_msg}"
        )

    def _on_import_slide_clicked(self):
        """Apre un selettore file e avvia l'importazione di PDF o immagini con OCR nel Notekeeper."""
        if self._import_thread and self._import_thread.isRunning():
            QMessageBox.information(self, "Importazione in corso", "Un'importazione di slide o documenti è già in fase di elaborazione.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleziona Slide o Documento PDF",
            "",
            "Documenti e Immagini (*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.txt *.md);;File PDF (*.pdf);;Immagini (*.png *.jpg *.jpeg *.webp *.bmp);;Tutti i file (*.*)"
        )
        if not file_path:
            return

        # Rileva profilo attivo dal selettore
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        profile = "Alessandro" if prof_text == "Tutti i profili" else prof_text

        # Rileva materia e argomento correnti dall'albero Notekeeper
        subject = self._nk_current_filter_subject or "Generale"
        topic = self._nk_current_filter_topic or "Generale"

        # Attiva barra di avanzamento
        file_name = os.path.basename(file_path)
        self.sbobina_status_label.setText(f"Inizializzazione importazione: {file_name}...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(False)

        self._import_thread = DocumentImportThread(
            file_path=file_path,
            profile=profile,
            subject=subject,
            topic=topic
        )
        self._import_thread.progress.connect(self._on_import_slide_progress)
        self._import_thread.finished.connect(self._on_import_slide_finished)
        self._import_thread.error.connect(self._on_import_slide_error)
        self._import_thread.start()

    def _on_import_slide_progress(self, msg: str, percent: int):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)

    def _on_import_slide_finished(self, result: dict):
        self.sbobina_progress_box.hide()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(True)

        self._refresh_notekeeper()
        note_id = result.get("id")
        if note_id:
            self._load_note_into_nk_editor(note_id)

        pages = result.get("pages", 1)
        ocr_pages = result.get("ocr_pages", 0)
        ocr_info = f" (di cui {ocr_pages} con OCR)" if ocr_pages > 0 else ""
        QMessageBox.information(
            self,
            "Slide Importate con Successo",
            f"Documento «{result.get('title', 'Slide')}» importato nel Quaderno!\n\n"
            f"• Pagine elaborate: {pages}{ocr_info}\n"
            f"• Materia: {result.get('subject')}\n"
            f"• Argomento: {result.get('topic')}\n"
            f"• Profilo: {result.get('profile')}"
        )

    def _on_import_slide_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(True)
        QMessageBox.critical(
            self,
            "Errore Importazione Slide",
            f"Si è verificato un errore durante l'importazione del documento:\n\n{err_msg}"
        )

    def _extract_flashcards_clicked(self, note_dict: Dict, btn: QPushButton):
        content = note_dict.get("content_markdown") or ""
        title = note_dict.get("title") or "Sbobina"
        note_id = note_dict.get("id")
        if not content.strip():
            QMessageBox.warning(self, "Nota vuota", "La nota non contiene testo sufficiente per estrarre flashcard.")
            return

        btn.setEnabled(False)
        btn.setText("⏳ Estrazione...")

        self._flashcard_thread = ExtractFlashcardsThread(note_id, content, title)
        self._flashcard_thread.finished.connect(lambda count, t, b=btn: self._on_flashcards_finished(count, t, b))
        self._flashcard_thread.error.connect(lambda err, b=btn: self._on_flashcards_error(err, b))
        self._flashcard_thread.start()

    def _on_flashcards_finished(self, count: int, note_title: str, btn: QPushButton):
        btn.setEnabled(True)
        btn.setText("🧠 Flashcard")
        self._refresh_notes()
        QMessageBox.information(
            self,
            "Flashcard Generate",
            f"Sono state create ed estratte con successo {count} flashcard d'esame nel tuo database per '{note_title}'!"
        )

    def _on_flashcards_error(self, err_msg: str, btn: QPushButton):
        btn.setEnabled(True)
        btn.setText("🧠 Flashcard")
        QMessageBox.warning(
            self,
            "Errore Flashcard",
            f"Impossibile generare le flashcard:\n{err_msg}"
        )

    def _show_export_all_menu(self):
        notes = self._filtered_notes()
        if not notes:
            QMessageBox.information(self, "Esporta note", "Non ci sono note o sbobine da esportare.")
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1A1F29; color: #F8FAFC; border: 1px solid #2D3545; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; font-size: 13px; }"
            "QMenu::item:selected { background-color: #0284C7; }"
        )

        md_act = menu.addAction("📄 Markdown (.md)")
        tex_act = menu.addAction("📄 Scarica documento LaTeX (.tex)")
        docx_act = menu.addAction("📘 Documento Word (.docx)")
        pdf_act = menu.addAction("📕 Documento PDF (.pdf)")

        chosen = menu.exec(self.export_all_btn.mapToGlobal(self.export_all_btn.rect().bottomLeft()))
        if not chosen:
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        if chosen == md_act:
            default_name = f"FlowLocal_Note_{timestamp}.md"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Markdown (*.md)")
            if path:
                try:
                    export_to_md(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Markdown:\n{e}")

        elif chosen == tex_act:
            # Esporta la nota corrente aperta nell'editor (o tutte le filtrate se nessuna è aperta)
            if hasattr(self, "nk_editor_title") and hasattr(self, "nk_editor_body") and (
                getattr(self, "_nk_selected_note_id", None) or self.nk_editor_body.get_markdown_text().strip()
            ):
                cur_title = self.nk_editor_title.text().strip() or "Nota_FlowLocal"
                safe_t = re.sub(r'[\/:*?"<>|]', '_', cur_title)[:40] or "Nota"
                target_data = {
                    "title": cur_title,
                    "profile": self.nk_editor_profile.currentText().strip() or "Alessandro",
                    "subject": self.nk_editor_subject.text().strip() or "Generale",
                    "topic": self.nk_editor_topic.text().strip() or "Generale",
                    "note_type": self.nk_editor_type.currentText().strip() or "appunto",
                    "content_markdown": self.nk_editor_body.get_markdown_text(),
                }
                default_name = f"{safe_t}_{timestamp}.tex"
            else:
                target_data = notes
                default_name = f"FlowLocal_Note_{timestamp}.tex"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta documento LaTeX", default_name, "LaTeX (*.tex)")
            if path:
                try:
                    export_to_tex(target_data, path)
                    QMessageBox.information(self, "Esportazione completata", f"Documento LaTeX esportato in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in LaTeX:\n{e}")

        elif chosen == docx_act:
            default_name = f"FlowLocal_Note_{timestamp}.docx"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Documento Word (*.docx)")
            if path:
                try:
                    export_to_docx(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Word:\n{e}")

        elif chosen == pdf_act:
            default_name = f"FlowLocal_Note_{timestamp}.pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Documento PDF (*.pdf)")
            if path:
                try:
                    export_to_pdf(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in PDF:\n{e}")

    def _show_export_single_menu(self, note_dict: Dict, btn_widget: QWidget):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1A1F29; color: #F8FAFC; border: 1px solid #2D3545; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; font-size: 13px; }"
            "QMenu::item:selected { background-color: #0284C7; }"
        )

        md_act = menu.addAction("📄 Markdown (.md)")
        tex_act = menu.addAction("📄 Scarica documento LaTeX (.tex)")
        docx_act = menu.addAction("📘 Documento Word (.docx)")
        pdf_act = menu.addAction("📕 Documento PDF (.pdf)")

        chosen = menu.exec(btn_widget.mapToGlobal(btn_widget.rect().bottomLeft()))
        if not chosen:
            return

        raw_title = (note_dict.get("title") or "Nota").replace("Sbobina: ", "").strip()
        safe_title = re.sub(r'[\/:*?"<>|]', '_', raw_title)[:40] or "Sbobina"
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')

        if chosen == md_act:
            default_name = f"{safe_title}_{timestamp}.md"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Markdown (*.md)")
            if path:
                try:
                    export_to_md(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Markdown:\n{e}")

        elif chosen == tex_act:
            default_name = f"{safe_title}_{timestamp}.tex"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta documento LaTeX", default_name, "LaTeX (*.tex)")
            if path:
                try:
                    export_to_tex(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Documento LaTeX esportato in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in LaTeX:\n{e}")

        elif chosen == docx_act:
            default_name = f"{safe_title}_{timestamp}.docx"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Documento Word (*.docx)")
            if path:
                try:
                    export_to_docx(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in Word:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Word:\n{e}")

        elif chosen == pdf_act:
            default_name = f"{safe_title}_{timestamp}.pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Documento PDF (*.pdf)")
            if path:
                try:
                    export_to_pdf(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in PDF:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in PDF:\n{e}")

    # -----------------------------------------------------------------
    # 3. DIZIONARIO
    # -----------------------------------------------------------------
    def _build_dictionary_page(self) -> QWidget:
        page, layout = self._make_page(
            "Dizionario & Snippet",
            "Correzioni fonetiche per guidare Whisper e scorciatoie vocali per espandere frasi a latenza zero.",
        )

        # Toggle pill Dizionario / Snippet
        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)
        self.dict_tab_btn = QPushButton("📖 Dizionario")
        self.dict_tab_btn.setObjectName("FilterPill")
        self.dict_tab_btn.setCheckable(True)
        self.dict_tab_btn.setChecked(True)
        self.snippet_tab_btn = QPushButton("⚡ Snippet Vocali")
        self.snippet_tab_btn.setObjectName("FilterPill")
        self.snippet_tab_btn.setCheckable(True)

        self.dict_subtab_group = QButtonGroup(self)
        self.dict_subtab_group.setExclusive(True)
        self.dict_subtab_group.addButton(self.dict_tab_btn, 0)
        self.dict_subtab_group.addButton(self.snippet_tab_btn, 1)

        pill_row.addWidget(self.dict_tab_btn)
        pill_row.addWidget(self.snippet_tab_btn)
        pill_row.addStretch(1)
        layout.addLayout(pill_row)

        self.dict_stack = SlidingStackedWidget(duration_ms=260)

        # --- Sub-tab 0: Dizionario ---
        dict_panel = QWidget()
        dict_layout = QVBoxLayout(dict_panel)
        dict_layout.setContentsMargins(0, 4, 0, 0)
        dict_layout.setSpacing(10)

        form = QHBoxLayout()
        form.setSpacing(10)
        self.dict_alias_input = QLineEdit()
        self.dict_alias_input.setPlaceholderText("Parola errata / alias (es. antigraffiti)")
        self.dict_alias_input.setMinimumHeight(36)
        form.addWidget(self.dict_alias_input, 1)

        arrow = QLabel("→")
        arrow.setStyleSheet("color: #64748B; font-size: 16px;")
        form.addWidget(arrow)

        self.dict_term_input = QLineEdit()
        self.dict_term_input.setPlaceholderText("Parola corretta (es. Antigravity)")
        self.dict_term_input.setMinimumHeight(36)
        self.dict_term_input.returnPressed.connect(self._add_dictionary_rule)
        form.addWidget(self.dict_term_input, 1)

        add_btn = _make_button("Aggiungi", "primary")
        add_btn.clicked.connect(self._add_dictionary_rule)
        form.addWidget(add_btn)
        dict_layout.addLayout(form)

        self.dict_list = ScrollList("Il dizionario è vuoto.")
        dict_layout.addWidget(self.dict_list, 1)
        self.dict_stack.addWidget(dict_panel)

        # --- Sub-tab 1: Snippet Vocali ---
        snip_panel = QWidget()
        snip_layout = QVBoxLayout(snip_panel)
        snip_layout.setContentsMargins(0, 4, 0, 0)
        snip_layout.setSpacing(10)

        snip_top = QHBoxLayout()
        snip_top.setSpacing(10)
        self.snippet_trigger_input = QLineEdit()
        self.snippet_trigger_input.setPlaceholderText("Frase trigger vocale (es. firma cliente)")
        self.snippet_trigger_input.setMinimumHeight(36)
        snip_top.addWidget(self.snippet_trigger_input, 1)

        add_snip_btn = _make_button("Aggiungi Snippet", "primary")
        add_snip_btn.clicked.connect(self._add_snippet_rule)
        snip_top.addWidget(add_snip_btn)
        snip_layout.addLayout(snip_top)

        self.snippet_expansion_input = QTextEdit()
        self.snippet_expansion_input.setPlaceholderText("Testo completo da incollare quando pronunci il trigger...")
        self.snippet_expansion_input.setMinimumHeight(110)
        self.snippet_expansion_input.setMaximumHeight(130)
        snip_layout.addWidget(self.snippet_expansion_input)

        self.snippet_list = ScrollList("Nessuno snippet configurato.")
        snip_layout.addWidget(self.snippet_list, 1)
        self.dict_stack.addWidget(snip_panel)

        self.dict_subtab_group.idClicked.connect(self.dict_stack.setCurrentIndex)
        layout.addWidget(self.dict_stack, 1)
        return page

    def _refresh_dictionary(self):
        items = db.get_all_vocabulary()
        self.dict_list.clear()
        if not items:
            self.dict_list.show_empty()
        else:
            for v in items:
                self.dict_list.add_card(self._make_dictionary_card(v))

        if hasattr(self, "snippet_list"):
            snippets = db.get_all_snippets()
            self.snippet_list.clear()
            if not snippets:
                self.snippet_list.show_empty()
            else:
                for s in snippets:
                    self.snippet_list.add_card(self._make_snippet_card(s))

    def _make_snippet_card(self, s: Dict) -> QFrame:
        card, layout = _make_card()
        row = QHBoxLayout()
        row.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.addWidget(_make_label(f"⚡ \"{s['trigger_phrase']}\"", "CardTitle", wrap=True))
        text_col.addWidget(_make_label(s.get("expansion_text") or "", "CardBody", wrap=True))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        uses = s.get("use_count", 0)
        meta_row.addWidget(_make_label(f"Usato {uses} volte", "BadgeMuted"))
        meta_row.addStretch(1)
        text_col.addLayout(meta_row)
        row.addLayout(text_col, 1)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, sid=s["id"]: self._delete_snippet_rule(sid))
        row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(row)
        return card

    def _add_snippet_rule(self):
        trigger = self.snippet_trigger_input.text().strip()
        expansion = self.snippet_expansion_input.toPlainText().strip()
        if not trigger:
            self.snippet_trigger_input.setFocus()
            return
        if not expansion:
            self.snippet_expansion_input.setFocus()
            return
        db.add_snippet(trigger, expansion)
        self.snippet_trigger_input.clear()
        self.snippet_expansion_input.clear()
        self.snippet_trigger_input.setFocus()
        self._refresh_dictionary()

    def _delete_snippet_rule(self, snippet_id: int):
        db.delete_snippet(snippet_id)
        self._refresh_dictionary()

    def _make_dictionary_card(self, v: Dict) -> QFrame:
        card, layout = _make_card()
        row = QHBoxLayout()
        row.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        aliases = (v.get("phonetic_aliases") or "").strip()
        rule = f"{aliases}  →  {v['correct_term']}" if aliases else v["correct_term"]
        text_col.addWidget(_make_label(rule, "CardTitle", wrap=True))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(v.get("category") or "Generale", "BadgeMuted"))
        if not aliases:
            meta_row.addWidget(_make_label("solo glossario", "CardMeta"))
        meta_row.addStretch(1)
        text_col.addLayout(meta_row)
        row.addLayout(text_col, 1)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, vid=v["id"]: self._delete_dictionary_rule(vid))
        row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(row)
        return card

    def _add_dictionary_rule(self):
        alias = self.dict_alias_input.text().strip()
        term = self.dict_term_input.text().strip()
        if not term:
            self.dict_term_input.setFocus()
            return

        if alias:
            db.learn_vocabulary_pair(alias, term)
        else:
            existing = {v["correct_term"].lower() for v in db.get_all_vocabulary()}
            if term.lower() not in existing:
                db.add_vocabulary(term, "", "Manuale")

        self.dict_alias_input.clear()
        self.dict_term_input.clear()
        self.dict_alias_input.setFocus()
        self._refresh_dictionary()

    def _delete_dictionary_rule(self, vocab_id: int):
        db.delete_vocabulary(vocab_id)
        self._refresh_dictionary()

    # -----------------------------------------------------------------
    # 4. STILI & PROMPT
    # -----------------------------------------------------------------
    def _build_styles_page(self) -> QWidget:
        page, layout = self._make_page(
            "Stili & Impostazioni",
            "Seleziona una modalità, modifica il System Prompt e configura l'audio in dettatura.",
        )

        # Card impostazione rapida Auto-Ducking
        duck_card = QFrame()
        duck_card.setObjectName("Card")
        duck_layout = QHBoxLayout(duck_card)
        duck_layout.setContentsMargins(14, 10, 14, 10)
        duck_layout.setSpacing(10)

        self.styles_auto_duck_chk = QCheckBox(
            "🎵 Abbassa automaticamente la musica durante la dettatura (Auto-Ducking)"
        )
        self.styles_auto_duck_chk.setChecked(audio_ducker.is_enabled())
        self.styles_auto_duck_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.styles_auto_duck_chk.setStyleSheet(
            "QCheckBox { font-size: 13px; font-weight: 600; padding: 2px 0px; background: transparent; }"
        )
        self.styles_auto_duck_chk.toggled.connect(lambda checked: audio_ducker.set_enabled(bool(checked)))
        duck_layout.addWidget(self.styles_auto_duck_chk)
        duck_layout.addStretch(1)

        duck_badge = QLabel("Spotify / Browser • 15% fade")
        duck_badge.setObjectName("Badge")
        duck_layout.addWidget(duck_badge)
        layout.addWidget(duck_card)

        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for idx, (label, name) in enumerate(MODE_TABS):
            btn = QPushButton(label)
            btn.setObjectName("ModeTab")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, n=name: self._load_mode_prompt(n))
            self.mode_group.addButton(btn, idx)
            tabs_row.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)
        tabs_row.addStretch(1)
        layout.addLayout(tabs_row)

        self.prompt_editor = QTextEdit()
        self.prompt_editor.setAcceptRichText(False)
        self.prompt_editor.setMinimumHeight(110)
        self.prompt_editor.setPlaceholderText("System Prompt della modalità selezionata…")
        self.prompt_editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.prompt_editor, 1)

        footer = QHBoxLayout()
        self.prompt_status = _make_label("", "PageSubtitle")
        footer.addWidget(self.prompt_status, 1)
        save_btn = _make_button("Salva Prompt", "primary")
        save_btn.clicked.connect(self._save_mode_prompt)
        footer.addWidget(save_btn)
        layout.addLayout(footer)
        return page

    def _load_mode_prompt(self, mode_name: str):
        self._current_mode_name = mode_name
        style = db.get_style_by_name(mode_name)
        if style:
            self.prompt_editor.setPlainText(style.get("system_prompt") or "")
            self.prompt_status.setText(style.get("display_name") or "")
        else:
            self.prompt_editor.setPlainText("")
            self.prompt_status.setText("Nessuno stile salvato per questa modalità: verrà creato al salvataggio.")

    def _save_mode_prompt(self):
        prompt = self.prompt_editor.toPlainText().strip()
        if not prompt:
            self.prompt_status.setText("Il prompt non può essere vuoto.")
            return

        style = db.get_style_by_name(self._current_mode_name)
        if style:
            db.update_style_prompt(style["id"], prompt, style.get("auto_target_apps") or "")
        else:
            label = next((l for l, n in MODE_TABS if n == self._current_mode_name), self._current_mode_name)
            conn = db._get_connection()
            conn.execute(
                "INSERT INTO llm_styles (name, display_name, description, system_prompt, auto_target_apps, is_active) "
                "VALUES (?, ?, '', ?, '', 0)",
                (self._current_mode_name, label, prompt),
            )
            conn.commit()

        self.prompt_status.setText(f"Salvato · {datetime.now().strftime('%H:%M:%S')}")

    # -----------------------------------------------------------------
    # IMPOSTAZIONI & FLOWTIME (CALENDARIO LOCALE + GOOGLE SYNC + NOTE)
    # -----------------------------------------------------------------
    def _startup_google_calendar_sync(self):
        try:
            from core.calendar_sync import calendar_manager
            if calendar_manager.has_token_file():
                calendar_manager.sync_in_background(force=True)
        except Exception:
            pass

    def _periodic_google_calendar_sync(self):
        try:
            from core.calendar_sync import calendar_manager
            if calendar_manager.has_token_file():
                calendar_manager.sync_in_background(force=False)
        except Exception:
            pass

    def _on_open_settings_clicked(self):
        dlg = HubSettingsDialog(self)
        dlg.exec()

    def _open_note_from_flowtime(self, note_id: int):
        """Apre direttamente in FlowNotes la nota cliccata da FlowTime."""
        if not note_id:
            return
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None
        if hasattr(self, "nk_profile_combo"):
            self.nk_profile_combo.blockSignals(True)
            self.nk_profile_combo.setCurrentText("Tutti i profili")
            self.nk_profile_combo.blockSignals(False)
        if hasattr(self, "nk_search_input"):
            self.nk_search_input.blockSignals(True)
            self.nk_search_input.clear()
            self.nk_search_input.blockSignals(False)
        self._nk_selected_note_id = note_id
        self._on_nav_clicked(1)
        self._load_note_into_nk_editor(note_id)

    def _build_flowtime_page(self) -> QWidget:
        self._ft_form_item_type: str = "event"
        self._ft_form_color: str = FLOWTIME_COLOR_PALETTE[0][0]
        self._ft_form_linked_note_ids: List[int] = []
        self._ft_color_buttons: Dict[str, QPushButton] = {}

        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(10)

        # Barra superiore compatta su riga unica: Titolo + Stretch + Ricerca + Pill Giorno/Settimana + Oggi (fuori da QScrollArea)
        top_ctrl = QHBoxLayout()
        top_ctrl.setContentsMargins(0, 0, 0, 0)
        top_ctrl.setSpacing(8)

        self.ft_page_title_lbl = _make_label("🗓️ FlowTime", "PageTitle")
        top_ctrl.addWidget(self.ft_page_title_lbl)
        top_ctrl.addStretch(1)

        self.ft_search_input = QLineEdit()
        self.ft_search_input.setPlaceholderText("🔍 Cerca eventi, To-Do o aule…")
        self.ft_search_input.setFixedWidth(260)
        self.ft_search_input.setMinimumHeight(36)
        self.ft_search_input.textChanged.connect(self._on_ft_search_changed)
        top_ctrl.addWidget(self.ft_search_input)

        # Selettore Vista Giorno / Vista Settimana
        self.ft_view_group = QButtonGroup(self)
        self.ft_view_group.setExclusive(True)
        self.ft_view_day_btn = QPushButton("📅 Giorno")
        self.ft_view_day_btn.setObjectName("FilterPill")
        self.ft_view_day_btn.setCheckable(True)
        self.ft_view_day_btn.setChecked(True)
        self.ft_view_day_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_view_week_btn = QPushButton("🗓️ Settimana")
        self.ft_view_week_btn.setObjectName("FilterPill")
        self.ft_view_week_btn.setCheckable(True)
        self.ft_view_week_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_view_group.addButton(self.ft_view_day_btn, 0)
        self.ft_view_group.addButton(self.ft_view_week_btn, 1)
        self.ft_view_group.idClicked.connect(self._on_ft_view_mode_changed)
        top_ctrl.addWidget(self.ft_view_day_btn)
        top_ctrl.addWidget(self.ft_view_week_btn)

        ft_today_btn = _make_button("📅 Oggi")
        ft_today_btn.clicked.connect(self._on_ft_today_clicked)
        top_ctrl.addWidget(ft_today_btn)
        layout.addLayout(top_ctrl)

        # Corpo principale: Calendario/Card Modifica a sinistra + Stack (Giorno | Settimana | Ricerca) + Sidebar To-Do a destra
        body = QHBoxLayout()
        body.setSpacing(12)

        # --- COLONNA SINISTRA: Calendario mensile custom + Card Creazione/Modifica ---
        left_col = QWidget()
        left_col.setFixedWidth(308)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.ft_calendar = FlowTimeCalendarWidget()
        self.ft_calendar.setMinimumHeight(206)
        self.ft_calendar.setMaximumHeight(224)
        self.ft_calendar.selectionChanged.connect(self._on_ft_date_selected)
        self.ft_calendar.currentPageChanged.connect(self._on_ft_month_changed)
        left_layout.addWidget(self.ft_calendar)

        # Form rapido creazione/modifica evento o To-Do (sempre fuori QScrollArea)
        form_card = QFrame()
        form_card.setObjectName("Card")
        fc_layout = QVBoxLayout(form_card)
        fc_layout.setContentsMargins(10, 9, 10, 9)
        fc_layout.setSpacing(6)

        hdr_form = QHBoxLayout()
        hdr_form.setSpacing(6)
        self.ft_form_title_lbl = _make_label("➕ Nuovo Impegno", "CardTitle")
        hdr_form.addWidget(self.ft_form_title_lbl, 1)
        self.ft_form_sync_lbl = _make_label("", "CardMeta")
        hdr_form.addWidget(self.ft_form_sync_lbl)
        fc_layout.addLayout(hdr_form)

        # Switch Evento / To-Do senza orario
        type_row = QHBoxLayout()
        type_row.setSpacing(6)
        self.ft_type_group = QButtonGroup(self)
        self.ft_type_group.setExclusive(True)
        self.ft_type_event_btn = QPushButton("📅 Evento")
        self.ft_type_event_btn.setObjectName("FTSegmentBtn")
        self.ft_type_event_btn.setCheckable(True)
        self.ft_type_event_btn.setChecked(True)
        self.ft_type_event_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_type_todo_btn = QPushButton("☑️ To-Do")
        self.ft_type_todo_btn.setObjectName("FTSegmentBtn")
        self.ft_type_todo_btn.setCheckable(True)
        self.ft_type_todo_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_type_group.addButton(self.ft_type_event_btn, 0)
        self.ft_type_group.addButton(self.ft_type_todo_btn, 1)
        self.ft_type_event_btn.toggled.connect(
            lambda checked: self._set_ft_form_item_type("event" if checked else "todo")
        )
        type_row.addWidget(self.ft_type_event_btn, 1)
        type_row.addWidget(self.ft_type_todo_btn, 1)
        fc_layout.addLayout(type_row)

        self.ft_title_input = QLineEdit()
        self.ft_title_input.setPlaceholderText("Titolo evento / lezione / esame…")
        self.ft_title_input.setMinimumHeight(36)
        self.ft_title_input.returnPressed.connect(self._on_ft_save_event)
        fc_layout.addWidget(self.ft_title_input)

        # Blocco orario + luogo (nascosto in modalità To-Do)
        self.ft_time_row_widget = QWidget()
        self.ft_time_row_widget.setStyleSheet("background: transparent;")
        tw_layout = QVBoxLayout(self.ft_time_row_widget)
        tw_layout.setContentsMargins(0, 0, 0, 0)
        tw_layout.setSpacing(6)

        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        self.ft_start_input = QLineEdit("09:00")
        self.ft_start_input.setPlaceholderText("HH:MM")
        self.ft_start_input.setMinimumHeight(36)
        self.ft_start_input.setMaximumWidth(68)
        time_row.addWidget(self.ft_start_input)

        self.ft_duration_combo = QComboBox()
        self.ft_duration_combo.setMinimumHeight(36)
        for lbl, mins in [("30 min", 30), ("1 ora", 60), ("1h 30m", 90), ("2 ore", 120), ("3 ore", 180)]:
            self.ft_duration_combo.addItem(lbl, mins)
        self.ft_duration_combo.setCurrentIndex(1)
        time_row.addWidget(self.ft_duration_combo, 1)

        self.ft_allday_chk = QCheckBox("Giorno")
        self.ft_allday_chk.setToolTip("Evento che dura tutto il giorno")
        self.ft_allday_chk.toggled.connect(self._on_ft_allday_toggled)
        time_row.addWidget(self.ft_allday_chk)
        tw_layout.addLayout(time_row)

        self.ft_location_input = QLineEdit()
        self.ft_location_input.setPlaceholderText("Luogo / Aula (opzionale)")
        self.ft_location_input.setMinimumHeight(36)
        tw_layout.addWidget(self.ft_location_input)
        fc_layout.addWidget(self.ft_time_row_widget)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self.ft_category_combo = QComboBox()
        self.ft_category_combo.setMinimumHeight(36)
        for cat in ("Personale", "Università", "Esame", "Studio", "Lavoro"):
            self.ft_category_combo.addItem(cat)
        meta_row.addWidget(self.ft_category_combo)

        self.ft_subject_input = QLineEdit()
        self.ft_subject_input.setPlaceholderText("Materia (opz.)")
        self.ft_subject_input.setMinimumHeight(36)
        meta_row.addWidget(self.ft_subject_input, 1)
        fc_layout.addLayout(meta_row)

        # Colori + Ripetizione
        style_row = QHBoxLayout()
        style_row.setSpacing(5)
        for hex_col, cname in FLOWTIME_COLOR_PALETTE:
            cbtn = QPushButton("")
            cbtn.setObjectName("FTColorDot")
            cbtn.setToolTip(f"Colore: {cname}")
            cbtn.setCursor(Qt.CursorShape.PointingHandCursor)
            cbtn.clicked.connect(lambda _c=False, hc=hex_col: self._set_ft_form_color(hc))
            self._ft_color_buttons[hex_col] = cbtn
            style_row.addWidget(cbtn)

        self.ft_recurrence_combo = QComboBox()
        self.ft_recurrence_combo.setMinimumHeight(30)
        for lbl, val in [("Non si ripete", "none"), ("Ogni giorno", "daily"), ("Ogni sett.", "weekly"), ("Ogni mese", "monthly")]:
            self.ft_recurrence_combo.addItem(lbl, val)
        style_row.addWidget(self.ft_recurrence_combo, 1)
        fc_layout.addLayout(style_row)
        self._refresh_ft_form_color_buttons()

        # Note collegate all'impegno selezionato + bottone HUD ricerca note
        self.ft_form_notes_box = QWidget()
        self.ft_form_notes_box.setStyleSheet("background: transparent;")
        self.ft_form_notes_layout = QVBoxLayout(self.ft_form_notes_box)
        self.ft_form_notes_layout.setContentsMargins(0, 0, 0, 0)
        self.ft_form_notes_layout.setSpacing(3)
        self.ft_form_notes_box.setVisible(False)
        fc_layout.addWidget(self.ft_form_notes_box)

        self.ft_link_note_btn = _make_button("🔗 Collega Nota...")
        self.ft_link_note_btn.setStyleSheet("min-height: 28px; max-height: 28px; padding: 0 10px; font-size: 11.5px;")
        self.ft_link_note_btn.setToolTip("Apri l'HUD di ricerca note per collegare appunti o sbobine a questo impegno")
        self.ft_link_note_btn.clicked.connect(self._on_ft_form_link_note_clicked)
        fc_layout.addWidget(self.ft_link_note_btn)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.ft_save_btn = _make_button("💾 Salva in FlowTime", "primary")
        self.ft_save_btn.clicked.connect(self._on_ft_save_event)
        btn_row.addWidget(self.ft_save_btn, 1)

        self.ft_delete_btn = _make_button("🗑️", "danger")
        self.ft_delete_btn.setToolTip("Elimina impegno selezionato")
        self.ft_delete_btn.clicked.connect(self._on_ft_delete_active_event)
        self.ft_delete_btn.setVisible(False)
        btn_row.addWidget(self.ft_delete_btn)

        self.ft_cancel_edit_btn = _make_button("Annulla")
        self.ft_cancel_edit_btn.clicked.connect(self._reset_ft_form)
        self.ft_cancel_edit_btn.setVisible(False)
        btn_row.addWidget(self.ft_cancel_edit_btn)
        fc_layout.addLayout(btn_row)

        left_layout.addWidget(form_card)
        left_layout.addStretch(1)
        body.addWidget(left_col)

        # --- STACK DESTRO A PIENA LARGHEZZA: 0 = Giorno Timeline | 1 = Settimana Timeline | 2 = Ricerca ---
        self.ft_main_stack = SlidingStackedWidget(duration_ms=260)

        self.ft_day_timeline = FlowTimeDayTimeline(self)
        self.ft_day_timeline.event_clicked.connect(self._on_ft_edit_event)
        self.ft_day_timeline.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_day_timeline.empty_slot_clicked.connect(self._on_ft_empty_slot_clicked)
        self.ft_day_timeline.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_main_stack.addWidget(self.ft_day_timeline)  # Indice 0: Vista Giorno

        self.ft_week_view = FlowTimeWeekView(self)
        self.ft_week_view.date_selected.connect(self._on_ft_week_date_selected)
        self.ft_week_view.open_day_requested.connect(self._on_ft_open_day_from_week)
        self.ft_week_view.event_clicked.connect(self._on_ft_edit_event)
        self.ft_week_view.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_week_view.empty_slot_clicked.connect(self._on_ft_empty_slot_clicked)
        self.ft_week_view.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_week_view.prev_week_clicked.connect(lambda: self._shift_ft_selected_days(-7))
        self.ft_week_view.next_week_clicked.connect(lambda: self._shift_ft_selected_days(7))
        self.ft_main_stack.addWidget(self.ft_week_view)  # Indice 1: Vista Settimana

        # Pagina 2: Risultati ricerca FlowTime
        search_page = QFrame()
        search_page.setObjectName("Card")
        sp_layout = QVBoxLayout(search_page)
        sp_layout.setContentsMargins(12, 10, 12, 10)
        sp_layout.setSpacing(8)
        sp_hdr = QHBoxLayout()
        self.ft_search_header_lbl = _make_label("🔍 Risultati Ricerca", "CardTitle")
        sp_hdr.addWidget(self.ft_search_header_lbl, 1)
        clear_search_btn = _make_button("✕ Chiudi ricerca")
        clear_search_btn.clicked.connect(lambda: self.ft_search_input.clear())
        sp_hdr.addWidget(clear_search_btn)
        sp_layout.addLayout(sp_hdr)
        self.ft_search_results_list = ScrollList("Nessun evento o impegno trovato.")
        sp_layout.addWidget(self.ft_search_results_list, 1)
        self.ft_main_stack.addWidget(search_page)  # Indice 2: Ricerca

        body.addWidget(self.ft_main_stack, 1)

        # --- COLONNA DESTRA: Pannello Impegni (To-Do) & Note raggruppati per giorno ---
        self.ft_todo_sidebar = FlowTimeTodoSidebar(self)
        self.ft_todo_sidebar.date_selected.connect(self._on_ft_week_date_selected)
        self.ft_todo_sidebar.event_clicked.connect(self._on_ft_edit_event)
        self.ft_todo_sidebar.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_todo_sidebar.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_todo_sidebar.add_todo_requested.connect(self._on_ft_quick_add_todo)
        body.addWidget(self.ft_todo_sidebar)

        layout.addLayout(body, 1)
        return page

    def _selected_ft_date_str(self) -> str:
        if hasattr(self, "ft_calendar"):
            return self.ft_calendar.selectedDate().toString("yyyy-MM-dd")
        return dt_date.today().isoformat()

    def _set_ft_form_item_type(self, mode: str):
        self._ft_form_item_type = "todo" if mode == "todo" else "event"
        is_todo = self._ft_form_item_type == "todo"
        if hasattr(self, "ft_time_row_widget"):
            self.ft_time_row_widget.setVisible(not is_todo)
        if hasattr(self, "ft_title_input"):
            self.ft_title_input.setPlaceholderText(
                "Impegno To-Do (es. Innaffiare le piante)…"
                if is_todo
                else "Titolo evento / lezione / esame…"
            )

    def _set_ft_form_color(self, hex_col: str):
        self._ft_form_color = hex_col or FLOWTIME_COLOR_PALETTE[0][0]
        self._refresh_ft_form_color_buttons()

    def _refresh_ft_form_color_buttons(self):
        for hc, btn in getattr(self, "_ft_color_buttons", {}).items():
            border = "2px solid #FFFFFF" if hc == self._ft_form_color else "1px solid rgba(148,163,184,0.35)"
            btn.setStyleSheet(f"background-color: {hc}; border: {border}; border-radius: 10px;")

    def _refresh_ft_form_notes_ui(self):
        if not hasattr(self, "ft_form_notes_layout"):
            return
        while self.ft_form_notes_layout.count():
            item = self.ft_form_notes_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif item.layout() is not None:
                sub = item.layout()
                while sub.count():
                    si = sub.takeAt(0)
                    if si.widget():
                        si.widget().hide()
                        si.widget().deleteLater()

        note_map = {int(n["id"]): n for n in db.get_all_study_notes()}
        valid_ids = [nid for nid in self._ft_form_linked_note_ids if nid in note_map]
        self._ft_form_linked_note_ids = valid_ids

        if not valid_ids:
            self.ft_form_notes_box.setVisible(False)
            self.ft_link_note_btn.setText("🔗 Collega Nota...")
            return

        self.ft_form_notes_box.setVisible(True)
        self.ft_link_note_btn.setText(f"🔗 Gestisci Note ({len(valid_ids)})...")
        for nid in valid_ids[:4]:
            note = note_map[nid]
            row = QHBoxLayout()
            row.setSpacing(4)
            open_btn = QPushButton(f"📓 {(note.get('title') or 'Nota')[:26]}")
            open_btn.setObjectName("FTNotePillBtn")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
            open_btn.clicked.connect(lambda _c=False, target_id=nid: self._open_note_from_flowtime(target_id))
            row.addWidget(open_btn, 1)

            rm_btn = QPushButton("×")
            rm_btn.setStyleSheet("min-height: 22px; max-height: 22px; min-width: 22px; max-width: 22px; padding: 0px; font-size: 12px;")
            rm_btn.setToolTip("Scollega questa nota")
            rm_btn.clicked.connect(lambda _c=False, target_id=nid: self._on_ft_form_unlink_note(target_id))
            row.addWidget(rm_btn)
            self.ft_form_notes_layout.addLayout(row)

    def _on_ft_form_unlink_note(self, note_id: int):
        self._ft_form_linked_note_ids = [x for x in self._ft_form_linked_note_ids if x != note_id]
        if self._ft_editing_event_id is not None:
            db.unlink_note_from_event(self._ft_editing_event_id, note_id)
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_form_link_note_clicked(self):
        ev_title = self.ft_title_input.text().strip() or (
            "Modifica Impegno" if self._ft_editing_event_id else "Nuovo Impegno"
        )
        dlg = LinkEventNoteDialog(
            self._ft_editing_event_id,
            ev_title,
            preselected_ids=self._ft_form_linked_note_ids,
            parent=self,
        )
        dlg.open_note_requested.connect(self._open_note_from_flowtime)
        dlg.exec()
        self._ft_form_linked_note_ids = list(dlg.selected_note_ids)
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_allday_toggled(self, checked: bool):
        if hasattr(self, "ft_start_input"):
            self.ft_start_input.setEnabled(not checked)
        if hasattr(self, "ft_duration_combo"):
            self.ft_duration_combo.setEnabled(not checked)

    def _on_ft_view_mode_changed(self, mode_idx: int):
        if hasattr(self, "ft_search_input") and self.ft_search_input.text().strip():
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(1 if mode_idx == 1 else 0)
            if mode_idx == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(50, self.ft_day_timeline.scroll_to_focus)
            elif mode_idx == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(50, self.ft_week_view.scroll_to_focus)
        self._refresh_flowtime_lists()

    def _on_ft_search_changed(self):
        q = self.ft_search_input.text().strip() if hasattr(self, "ft_search_input") else ""
        if hasattr(self, "ft_main_stack"):
            if q:
                self.ft_main_stack.setCurrentIndex(2)
            else:
                self.ft_main_stack.setCurrentIndex(1 if self.ft_view_week_btn.isChecked() else 0)
        self._refresh_flowtime_lists()

    def _on_ft_week_date_selected(self, date_str: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar"):
            self.ft_calendar.setSelectedDate(qd)

    def _on_ft_open_day_from_week(self, date_str: str):
        self._on_ft_week_date_selected(date_str)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        self._on_ft_view_mode_changed(0)

    def _shift_ft_selected_days(self, delta_days: int):
        if hasattr(self, "ft_calendar"):
            new_qdate = self.ft_calendar.selectedDate().addDays(delta_days)
            self.ft_calendar.setSelectedDate(new_qdate)

    def _on_ft_today_clicked(self):
        if hasattr(self, "ft_search_input") and self.ft_search_input.text().strip():
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
            self.ft_main_stack.setCurrentIndex(1 if self.ft_view_week_btn.isChecked() else 0)
        if hasattr(self, "ft_calendar"):
            self.ft_calendar.setSelectedDate(QDate.currentDate())
            self._refresh_flowtime_page()

    def _on_ft_date_selected(self):
        self._refresh_flowtime_lists()
        if hasattr(self, "ft_main_stack"):
            if self.ft_main_stack.currentIndex() == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(40, self.ft_day_timeline.scroll_to_focus)
            elif self.ft_main_stack.currentIndex() == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(40, self.ft_week_view.scroll_to_focus)

    def _on_ft_month_changed(self, year: int, month: int):
        self._update_ft_calendar_highlights(year, month)

    def _on_ft_empty_slot_clicked(self, date_str: str, hhmm: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar") and qd != self.ft_calendar.selectedDate():
            self.ft_calendar.setSelectedDate(qd)
        dlg = TimelineQuickAddDialog(date_str, hhmm, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_flowtime_page()

    def _on_ft_quick_add_todo(self, date_str: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar") and qd != self.ft_calendar.selectedDate():
            self.ft_calendar.setSelectedDate(qd)
        dlg = TimelineQuickAddDialog(date_str, "09:00", parent=self)
        dlg.todo_mode_btn.setChecked(True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_flowtime_page()

    def _on_ft_todo_toggled(self, event_id: int):
        from core.calendar_sync import calendar_manager
        calendar_manager.toggle_todo(event_id)
        self._refresh_flowtime_page()

    def _refresh_flowtime_page(self):
        if not hasattr(self, "ft_calendar"):
            return
        sel_date = self.ft_calendar.selectedDate()
        self._update_ft_calendar_highlights(sel_date.year(), sel_date.month())
        self._refresh_flowtime_lists()
        if hasattr(self, "ft_main_stack"):
            if self.ft_main_stack.currentIndex() == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(70, self.ft_day_timeline.scroll_to_focus)
            elif self.ft_main_stack.currentIndex() == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(70, self.ft_week_view.scroll_to_focus)

    def _update_ft_calendar_highlights(self, year: int, month: int):
        if not hasattr(self, "ft_calendar"):
            return
        activity = db.get_dates_with_activity(year, month)
        self.ft_calendar.set_activity_data(activity, self._active_is_dark(), self._reading_mode)

    def _refresh_flowtime_lists(self):
        if not hasattr(self, "ft_day_timeline"):
            return
        from core.calendar_sync import calendar_manager, ITALIAN_WEEKDAYS

        date_str = self._selected_ft_date_str()
        search_q = self.ft_search_input.text().strip() if hasattr(self, "ft_search_input") else ""
        day_events = calendar_manager.get_upcoming_events(days=1, target_date=date_str)

        try:
            d_obj = dt_date.fromisoformat(date_str)
            months_full = [
                "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
            ]
            day_title = f"{ITALIAN_WEEKDAYS[d_obj.weekday()]} {d_obj.day} {months_full[d_obj.month - 1]} {d_obj.year}"
        except Exception:
            d_obj = dt_date.today()
            day_title = date_str

        day_notes = db.get_notes_for_date(date_str)
        notes_by_event: Dict[int, List[Dict[str, Any]]] = {}
        for ev in day_events:
            eid = int(ev.get("local_id") or ev.get("id"))
            notes_by_event[eid] = ev.get("linked_notes") or db.get_notes_for_event(eid)

        todo_cnt = sum(1 for e in day_events if e.get("item_type") == "todo")
        ev_cnt = len(day_events) - todo_cnt
        self.ft_day_timeline.day_title_lbl.setText(day_title)
        self.ft_day_timeline.day_stats_lbl.setText(
            f"{ev_cnt} event{'o' if ev_cnt == 1 else 'i'} · {todo_cnt} To-Do · {len(day_notes)} not{'a' if len(day_notes) == 1 else 'e'}"
        )
        self.ft_day_timeline.set_day_data(
            date_str=date_str,
            events=day_events,
            day_notes=day_notes,
            notes_by_event=notes_by_event,
            is_dark=self._active_is_dark(),
            reading_mode=self._reading_mode,
            selected_event_id=self._ft_editing_event_id,
        )

        # Aggiorna Vista Settimanale a 7 colonne orarie (Lun–Dom) e Sidebar destra Impegni (To-Do) & Note
        monday = d_obj - timedelta(days=d_obj.weekday())
        sunday = monday + timedelta(days=6)
        week_rows = db.get_calendar_events(start_date=monday.isoformat(), end_date=sunday.isoformat())
        week_events_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for r in week_rows:
            ev_fmt = calendar_manager._format_db_event(r)
            week_events_by_date.setdefault(ev_fmt["event_date"], []).append(ev_fmt)
        week_events_by_date.setdefault(date_str, day_events)
        week_notes_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for offset in range(7):
            ds = (monday + timedelta(days=offset)).isoformat()
            week_notes_by_date[ds] = db.get_notes_for_date(ds)
        week_notes_by_date.setdefault(date_str, day_notes)
        week_notes_by_event: Dict[int, List[Dict[str, Any]]] = dict(notes_by_event)
        for ev_list in week_events_by_date.values():
            for ev in ev_list:
                if ev.get("item_type") == "todo" or ev.get("is_all_day"):
                    eid = int(ev.get("local_id") or ev.get("id"))
                    if eid not in week_notes_by_event:
                        week_notes_by_event[eid] = ev.get("linked_notes") or db.get_notes_for_event(eid)

        if hasattr(self, "ft_week_view"):
            self.ft_week_view.set_week_data(
                date_str,
                week_events_by_date,
                week_notes_by_date,
                is_dark=self._active_is_dark(),
                reading_mode=self._reading_mode,
                selected_event_id=self._ft_editing_event_id,
            )

        if hasattr(self, "ft_todo_sidebar"):
            self.ft_todo_sidebar.set_sidebar_data(
                date_str,
                week_events_by_date,
                week_notes_by_date,
                week_notes_by_event,
            )

        # Popola pagina ricerca se attiva
        if search_q and hasattr(self, "ft_search_results_list"):
            raw_rows = db.search_calendar_events(query=search_q, limit=60)
            results = [calendar_manager._format_db_event(r) for r in raw_rows]
            self.ft_search_header_lbl.setText(f"🔍 Risultati ricerca: \"{search_q}\" ({len(results)})")
            self.ft_search_results_list.clear()
            if not results:
                self.ft_search_results_list.show_empty(f"Nessun evento o impegno trovato per '{search_q}'.")
            else:
                for ev in results:
                    self.ft_search_results_list.add_card(self._make_ft_search_card(ev))

    def _make_ft_search_card(self, ev: Dict[str, Any]) -> QFrame:
        card = QFrame()
        card.setObjectName("FTSearchResultCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)

        ev_id = int(ev.get("local_id") or ev.get("id"))
        is_todo = ev.get("item_type") == "todo"
        top = QHBoxLayout()
        top.setSpacing(6)
        time_label = f"{ev['date']} · ☑️ To-Do" if is_todo else f"{ev['date']} · {ev['time']}"
        top.addWidget(_make_label(time_label, "FTTimePill"))
        top.addWidget(_make_label(ev.get("category") or "Personale", "BadgeMuted"))
        if ev.get("subject"):
            top.addWidget(_make_label(ev["subject"], "Badge"))
        top.addStretch(1)

        if is_todo:
            done_btn = _make_button("✅ Fatto" if ev.get("is_completed") else "☐ Completa")
            done_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
            done_btn.clicked.connect(lambda _c=False, eid=ev_id: self._on_ft_todo_toggled(eid))
            top.addWidget(done_btn)

        open_btn = _make_button("✏️ Modifica & Vai", "primary")
        open_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 10px; font-size: 11px;")
        open_btn.clicked.connect(lambda _c=False, e=ev: self._on_ft_open_search_result(e))
        top.addWidget(open_btn)
        layout.addLayout(top)

        t_lbl = _make_label(ev.get("summary") or "(Senza titolo)", "CardTitle", wrap=True)
        layout.addWidget(t_lbl)
        if ev.get("location"):
            layout.addWidget(_make_label(f"📍 {ev['location']}", "CardMeta", wrap=True))
        return card

    def _on_ft_open_search_result(self, ev: Dict[str, Any]):
        if hasattr(self, "ft_search_input"):
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(0)
        self._on_ft_edit_event(ev)

    def _reset_ft_form(self):
        self._ft_editing_event_id = None
        self._ft_form_linked_note_ids = []
        self.ft_form_title_lbl.setText("➕ Nuovo Impegno")
        self.ft_form_sync_lbl.setText("")
        self.ft_save_btn.setText("💾 Salva in FlowTime")
        self.ft_delete_btn.setVisible(False)
        self.ft_cancel_edit_btn.setVisible(False)
        self.ft_type_event_btn.setChecked(True)
        self._set_ft_form_item_type("event")
        self.ft_title_input.clear()
        self.ft_start_input.setText("09:00")
        self.ft_duration_combo.setCurrentIndex(1)
        self.ft_allday_chk.setChecked(False)
        self.ft_category_combo.setCurrentIndex(0)
        self.ft_subject_input.clear()
        self.ft_location_input.clear()
        self.ft_recurrence_combo.setCurrentIndex(0)
        self._set_ft_form_color(FLOWTIME_COLOR_PALETTE[0][0])
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_edit_event(self, ev: Dict[str, Any]):
        ev_date = (ev.get("event_date") or "").strip()
        if ev_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(ev_date, "yyyy-MM-dd")
            if qd.isValid() and qd != self.ft_calendar.selectedDate():
                self.ft_calendar.setSelectedDate(qd)
        ev_id = int(ev.get("local_id") or ev.get("id"))
        self._ft_editing_event_id = ev_id
        is_todo = ev.get("item_type") == "todo"
        self.ft_form_title_lbl.setText("✏️ Modifica To-Do" if is_todo else "✏️ Modifica Evento")
        sync_st = ev.get("sync_status") or "local_only"
        self.ft_form_sync_lbl.setText("☁️ Google" if sync_st == "synced" else "💻 Locale")
        self.ft_save_btn.setText("💾 Aggiorna")
        self.ft_delete_btn.setVisible(True)
        self.ft_cancel_edit_btn.setVisible(True)

        if is_todo:
            done_btn = _make_button("✅ Fatto" if ev.get("is_completed") else "☐ Completa")
            done_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
            done_btn.clicked.connect(lambda _c=False, eid=ev_id: self._on_ft_todo_toggled(eid))
            top.addWidget(done_btn)

        open_btn = _make_button("✏️ Modifica & Vai", "primary")
        open_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 10px; font-size: 11px;")
        open_btn.clicked.connect(lambda _c=False, e=ev: self._on_ft_open_search_result(e))
        top.addWidget(open_btn)
        layout.addLayout(top)

        t_lbl = _make_label(ev.get("summary") or "(Senza titolo)", "CardTitle", wrap=True)
        layout.addWidget(t_lbl)
        if ev.get("location"):
            layout.addWidget(_make_label(f"📍 {ev['location']}", "CardMeta", wrap=True))
        return card

    def _on_ft_open_search_result(self, ev: Dict[str, Any]):
        if hasattr(self, "ft_search_input"):
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(0)
        self._on_ft_edit_event(ev)

    def _reset_ft_form(self):
        self._ft_editing_event_id = None
        self._ft_form_linked_note_ids = []
        self.ft_form_title_lbl.setText("➕ Nuovo Impegno")
        self.ft_form_sync_lbl.setText("")
        self.ft_save_btn.setText("💾 Salva in FlowTime")
        self.ft_delete_btn.setVisible(False)
        self.ft_cancel_edit_btn.setVisible(False)
        self.ft_type_event_btn.setChecked(True)
        self._set_ft_form_item_type("event")
        self.ft_title_input.clear()
        self.ft_start_input.setText("09:00")
        self.ft_duration_combo.setCurrentIndex(1)
        self.ft_allday_chk.setChecked(False)
        self.ft_category_combo.setCurrentIndex(0)
        self.ft_subject_input.clear()
        self.ft_location_input.clear()
        self.ft_recurrence_combo.setCurrentIndex(0)
        self._set_ft_form_color(FLOWTIME_COLOR_PALETTE[0][0])
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_edit_event(self, ev: Dict[str, Any]):
        ev_date = (ev.get("event_date") or "").strip()
        if ev_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(ev_date, "yyyy-MM-dd")
            if qd.isValid() and qd != self.ft_calendar.selectedDate():
                self.ft_calendar.setSelectedDate(qd)
        ev_id = int(ev.get("local_id") or ev.get("id"))
        self._ft_editing_event_id = ev_id
        is_todo = ev.get("item_type") == "todo"
        self.ft_form_title_lbl.setText("✏️ Modifica To-Do" if is_todo else "✏️ Modifica Evento")
        sync_st = ev.get("sync_status") or "local_only"
        self.ft_form_sync_lbl.setText("☁️ Google" if sync_st == "synced" else "💻 Locale")
        self.ft_save_btn.setText("💾 Aggiorna")
        self.ft_delete_btn.setVisible(True)
        self.ft_cancel_edit_btn.setVisible(True)

        if is_todo:
            self.ft_type_todo_btn.setChecked(True)
            self._set_ft_form_item_type("todo")
        else:
            self.ft_type_event_btn.setChecked(True)
            self._set_ft_form_item_type("event")

        self.ft_title_input.setText(ev.get("summary") or "")
        is_all = bool(ev.get("is_all_day"))
        self.ft_allday_chk.setChecked(is_all)
        self.ft_start_input.setText(ev.get("start_time") or "09:00")
        st_s = (ev.get("start_time") or "").strip()
        en_s = (ev.get("end_time") or "").strip()
        if not is_all and not is_todo and ":" in st_s and ":" in en_s:
            try:
                sh, sm = [int(x) for x in st_s[:5].split(":")]
                eh, em = [int(x) for x in en_s[:5].split(":")]
                diff = (eh * 60 + em) - (sh * 60 + sm)
                if diff > 0:
                    idx_d = self.ft_duration_combo.findData(diff)
                    if idx_d >= 0:
                        self.ft_duration_combo.setCurrentIndex(idx_d)
            except Exception:
                pass
        cat_idx = self.ft_category_combo.findText(ev.get("category") or "Personale")
        if cat_idx >= 0:
            self.ft_category_combo.setCurrentIndex(cat_idx)
        self.ft_subject_input.setText(ev.get("subject") or "")
        self.ft_location_input.setText(ev.get("location") or "")

        rec_val = ev.get("recurrence") or "none"
        rec_idx = self.ft_recurrence_combo.findData(rec_val)
        self.ft_recurrence_combo.setCurrentIndex(rec_idx if rec_idx >= 0 else 0)

        col_val = ev.get("color") or FLOWTIME_COLOR_PALETTE[0][0]
        self._set_ft_form_color(col_val)

        self._ft_form_linked_note_ids = [int(n["id"]) for n in db.get_notes_for_event(ev_id)]
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()
        self.ft_title_input.setFocus()

    def _on_ft_save_event(self):
        title = self.ft_title_input.text().strip()
        if not title:
            self.ft_title_input.setFocus()
            return
        from core.calendar_sync import calendar_manager

        date_str = self._selected_ft_date_str()
        item_type = self._ft_form_item_type
        is_todo = item_type == "todo"
        is_all = True if is_todo else self.ft_allday_chk.isChecked()
        start_str = "00:00" if is_todo else ("all_day" if is_all else (self.ft_start_input.text().strip() or "09:00"))
        duration = 0 if is_todo else int(self.ft_duration_combo.currentData() or 60)
        cat = self.ft_category_combo.currentText()
        subj = self.ft_subject_input.text().strip()
        loc = "" if is_todo else self.ft_location_input.text().strip()
        rec = str(self.ft_recurrence_combo.currentData() or "none")
        color = self._ft_form_color

        if self._ft_editing_event_id is not None:
            end_str = ""
            if not is_all and not is_todo:
                try:
                    _, _, start_str, end_str = calendar_manager._parse_date_and_times(date_str, start_str, duration)
                except Exception:
                    end_str = ""
            calendar_manager.update_event(
                self._ft_editing_event_id,
                summary=title,
                date_str=date_str,
                start_time_str=start_str,
                end_time_str=end_str,
                is_all_day=is_all,
                location=loc,
                category=cat,
                subject=subj,
                color=color,
                item_type=item_type,
                recurrence=rec,
            )
            for nid in self._ft_form_linked_note_ids:
                db.link_note_to_event(self._ft_editing_event_id, int(nid), link_type="manual")
        else:
            created = calendar_manager.create_event(
                summary=title,
                date_str=date_str,
                start_time_str=start_str,
                duration_minutes=duration,
                location=loc,
                category=cat,
                subject=subj,
                color=color,
                push_to_google=not is_todo,
                item_type=item_type,
                recurrence=rec,
            )
            if created and created.get("local_id"):
                cid = int(created["local_id"])
                for nid in self._ft_form_linked_note_ids:
                    db.link_note_to_event(cid, int(nid), link_type="manual")

        self._reset_ft_form()
        self._refresh_flowtime_page()

    def _on_ft_delete_active_event(self):
        if self._ft_editing_event_id is None:
            return
        self._on_ft_delete_event(self._ft_editing_event_id)

    def _on_ft_delete_event(self, event_id: int):
        from core.calendar_sync import calendar_manager
        calendar_manager.delete_event(event_id)
        if self._ft_editing_event_id == event_id:
            self._reset_ft_form()
        else:
            self._refresh_flowtime_page()

    def _on_ft_link_note_clicked(self, event_id: int, event_title: str):
        dlg = LinkEventNoteDialog(event_id, event_title, parent=self)
        dlg.open_note_requested.connect(self._open_note_from_flowtime)
        dlg.exec()
        self._refresh_flowtime_lists()

    def _on_ft_sync_google_clicked(self):
        from core.calendar_sync import calendar_manager
        if not calendar_manager.has_token_file():
            self._on_connect_calendar_clicked()
            return
        calendar_manager.sync_in_background(force=True)

    def _on_ft_sync_finished(self, ok: bool, msg: str):
        self._update_calendar_status_ui()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()

    # -----------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------
    @staticmethod
    def _copy_to_clipboard(text: str):
        QApplication.clipboard().setText(text or "")
