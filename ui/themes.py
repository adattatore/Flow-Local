"""Temi QSS multi-tema, preset tipografici e rilevamento GPU per FlowLocal Hub."""

from datetime import datetime, date as dt_date, timedelta
from typing import Callable, Dict, List, Optional, Any
import logging
import math
import os
import random
import re
import tempfile
import threading
import time

_themes_logger = logging.getLogger("FlowLocal.Themes")

REQUIRED_PALETTE_KEYS = (
    "text",
    "title",
    "subtitle",
    "muted",
    "root_bg",
    "sidebar_bg",
    "card_bg",
    "card_border",
    "input_bg",
    "input_border",
    "input_text",
    "btn_bg",
    "btn_border",
    "btn_text",
    "dialog_bg",
    "divider",
)

_PALETTE_ALIASES = {
    "bg": ("dialog_bg", "root_bg"),
    "background": ("root_bg", "dialog_bg"),
    "fg": ("text", "title"),
    "foreground": ("text", "title"),
    "border": ("card_border", "divider"),
    "surface": ("card_bg", "surface_soft"),
    "accent_text": ("badge_text", "accent"),
}


class SafePalette(dict):
    """Dizionario palette retrocompatibile che previene KeyError a runtime tramite alias e fallback."""

    def __init__(self, *args, theme_name: str = "obsidian", **kwargs):
        super().__init__(*args, **kwargs)
        self._theme_name = theme_name

    def __missing__(self, key: str) -> str:
        for alias in _PALETTE_ALIASES.get(key, ()):
            val = dict.get(self, alias)
            if val is not None:
                return val
        fallback = _THEME_PALETTES["obsidian"].get(key)
        if fallback is not None:
            _themes_logger.warning(
                "Chiave palette '%s' mancante nel tema '%s': uso fallback obsidian.",
                key,
                self._theme_name,
            )
            return fallback
        _themes_logger.warning(
            "Chiave palette sconosciuta '%s' nel tema '%s': uso fallback neutro.",
            key,
            self._theme_name,
        )
        return dict.get(self, "text", "#E2E8F0")


def validate_all_themes() -> List[str]:
    """Verifica che tutti i temi abbiano le chiavi richieste e generino QSS valido."""
    errors: List[str] = []
    for theme_key, raw in _THEME_PALETTES.items():
        for req in REQUIRED_PALETTE_KEYS:
            if req not in raw or not str(raw[req]).strip():
                errors.append(f"Tema '{theme_key}': chiave obbligatoria mancante '{req}'")
        for reading in (False, True):
            try:
                pal = _resolve_palette(theme_key, reading_mode=reading)
                if not isinstance(pal, SafePalette):
                    errors.append(f"Tema '{theme_key}' (reading={reading}): palette non SafePalette")
                qss = get_hub_qss(theme_key, reading_mode=reading)
                if not qss or "{" not in qss:
                    errors.append(f"Tema '{theme_key}' (reading={reading}): QSS vuoto o non valido")
            except Exception as exc:
                errors.append(f"Tema '{theme_key}' (reading={reading}): eccezione {exc!r}")
    return errors

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPoint, QPointF, QRect, QRectF, QLineF,
    QSettings, QByteArray, QSize, QDate, QLocale,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve,
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QPainterPath, QRadialGradient,
    QLinearGradient, QCursor, QPolygonF, QIcon, QPixmap, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCalendarWidget, QCheckBox, QDateEdit, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QComboBox, QFontComboBox, QSlider, QSpinBox, QTreeWidget, QTreeWidgetItem, QSplitter
)

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

CORE_SIGNATURE_THEMES = (
    ("Obsidian", "obsidian", "Scuro minimale ad alto contrasto (Consigliato)"),
    ("Chiaro", "light", "Chiarezza editoriale per ambienti luminosi"),
    ("Dark Orb", "dark_orb", "Scuro obsidian con riflessi aurora"),
    ("Orb", "orb", "Chiaro luminoso con accenti aurora"),
)

CREATIVE_THEMES = (
    ("Ghibli Spring", "ghibli_spring", "Floreale pastello rilassante"),
    ("Pixel Chill", "pixel_chill", "Arcade 16-bit lo-fi"),
    ("Crimson Katana", "katana", "Dark fantasy affilato metallico"),
    ("Tactical HUD", "tactical_hud", "Visore tattico notturno"),
    ("Industrial Mecha", "industrial_mecha", "Hangar robotico heavy-duty"),
    ("Amber CRT", "amber_crt", "Terminale fosforo ambra anni '80"),
    ("Starfield Alabaster", "starfield_alabaster", "NASA-punk ceramica e ottone"),
    ("Cyber Synthwave", "cyber_synthwave", "Outrun neon ciano e magenta"),
)

CORE_SIGNATURE_THEME_KEYS = {k for _, k, _ in CORE_SIGNATURE_THEMES}


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

    base.setdefault("bg", base.get("dialog_bg") or base.get("root_bg") or "#0B0E16")
    return SafePalette(base, theme_name=theme)


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
QFrame#SettingsSidebar {{
    background-color: {p['sidebar_bg']};
    {p['sidebar_border_qss']}
}}
QPushButton#SettingsNavBtn {{
    min-height: 38px;
    max-height: 38px;
    text-align: left;
    padding: 0 10px;
    border: 1px solid transparent;
    border-radius: {p['nav_btn_radius']};
    background-color: transparent;
    color: {p['subtitle']};
    font-size: 11.5px;
    font-weight: 600;
}}
QPushButton#SettingsNavBtn:hover {{
    background-color: {p['nav_hover']};
    color: {p['nav_hover_text']};
}}
QPushButton#SettingsNavBtn:pressed {{
    {p['pill_pressed_qss']}
}}
QPushButton#SettingsNavBtn:checked {{
    background-color: {p['nav_checked_bg']};
    border: 1px solid {p['nav_checked_border']};
    color: {p['nav_checked_text']};
}}
QPushButton#CollapseToggle {{
    background-color: transparent;
    border: none;
    text-align: left;
    padding: 4px 2px;
    min-height: 26px;
    color: {p['subtitle']};
    font-size: 12px;
    font-weight: 700;
}}
QPushButton#CollapseToggle:hover {{
    color: {p['title']};
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
QSpinBox {{
    min-height: 30px;
    background-color: {p['input_bg']};
    {p['input_border_qss']}
    border-radius: {p['input_radius']};
    padding: 0 8px;
    color: {p['input_text']};
    font-size: 12px;
    font-weight: 600;
    selection-background-color: {p['selection_bg']};
}}
QSpinBox:focus {{
    {p['input_focus_border_qss']}
    background-color: {p['input_focus_bg']};
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
QPushButton[variant="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['subtitle']};
    font-weight: 500;
}}
QPushButton[variant="ghost"]:hover {{
    background-color: {p['nav_hover']};
    border: 1px solid {p['card_border']};
    color: {p['text']};
}}
QPushButton[variant="ghost"]:pressed {{
    background-color: {p['btn_pressed_bg']};
}}
QFrame#HairlineDivider {{
    background-color: {p['sidebar_border']};
    border: none;
    min-height: 1px;
    max-height: 1px;
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
    min-height: 32px;
    max-height: 32px;
    border-radius: {p['btn_radius']};
    padding: 0 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#NKActionBtn:pressed {{
    padding-top: 2px;
    padding-left: 13px;
    padding-right: 11px;
}}
QPushButton#NKToolbarIconBtn {{
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    border-radius: {p['btn_radius']};
    padding: 0;
}}
QPushButton#NKToolbarPill {{
    min-height: 32px;
    max-height: 32px;
    border-radius: {p['btn_radius']};
    padding: 0 12px;
    font-size: 12px;
    font-weight: 600;
    {p['pill_border_qss']}
    background-color: {p['nk_pill_bg']};
    color: {p['btn_text']};
}}
QPushButton#NKToolbarPill:hover {{
    {p['pill_hover_qss']}
}}
QPushButton#NKToolbarPill:pressed {{
    {p['pill_pressed_qss']}
    padding: 2px 12px 0 12px;
}}
QPushButton#NKToolbarPill:checked {{
    {p['pill_checked_qss']}
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
