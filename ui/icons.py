"""Helper centrale per icone vettoriali monocromatiche (qtawesome Phosphor) e integrazione titlebar Windows 11."""

from __future__ import annotations

import ctypes
import sys
from typing import Dict, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

try:
    import qtawesome as qta
except Exception:  # pragma: no cover
    qta = None


# Mappa semantica -> nome icona Phosphor (ph.*) verificato in phosphor-1.3.0
ICON_MAP: Dict[str, str] = {
    # Navigazione & Tab Principali
    "mic": "ph.microphone-light",
    "notes": "ph.notebook-light",
    "calendar": "ph.calendar-blank-light",
    "sparkles": "ph.sparkle-light",
    "book": "ph.book-open-light",
    "sliders": "ph.sliders-horizontal-light",
    "settings": "ph.gear-light",
    "palette": "ph.palette-light",
    "cpu": "ph.cpu-light",
    "lightning": "ph.lightning-light",
    "cloud": "ph.cloud-light",
    "keyboard": "ph.keyboard-light",
    "cards": "ph.cards-light",
    "graph": "ph.share-network-light",
    "focus": "ph.target-light",
    "tree": "ph.tree-structure-light",

    # Azioni & Controlli
    "plus": "ph.plus",
    "folder_plus": "ph.folder-plus-light",
    "note_pencil": "ph.note-pencil-light",
    "trash": "ph.trash-light",
    "edit": "ph.pencil-simple-light",
    "search": "ph.magnifying-glass-light",
    "link": "ph.link-light",
    "copy": "ph.copy-light",
    "check": "ph.check",
    "check_square": "ph.check-square-light",
    "play": "ph.play-fill",
    "pause": "ph.pause-fill",
    "volume": "ph.speaker-high-light",
    "eye": "ph.eye-light",
    "eye_off": "ph.eye-slash-light",
    "lightning": "ph.lightning-fill",
    "refresh": "ph.arrows-clockwise-light",
    "folder": "ph.folder-light",
    "file": "ph.file-text-light",
    "pin": "ph.push-pin-light",
    "star": "ph.star-light",
    "star_fill": "ph.star-fill",
    "download": "ph.download-simple-light",
    "upload": "ph.upload-simple-light",
    "caret_right": "ph.caret-right",
    "caret_down": "ph.caret-down",
    "close": "ph.x",
    "clock": "ph.clock-light",
    "sun": "ph.sun-light",
    "moon": "ph.moon-light",
    "command": "ph.command-light",
    "zoom_in": "ph.magnifying-glass-plus-light",
    "zoom_out": "ph.magnifying-glass-minus-light",
    "expand": "ph.corners-out-light",
    "more": "ph.dots-three-bold",
    "arrow_right": "ph.arrow-right-light",
    "save": "ph.floppy-disk-light",
    "graduation": "ph.graduation-cap-light",
    "magic": "ph.magic-wand-light",
    "font": "ph.text-aa-light",
    "code": "ph.code-light",
    "quotes": "ph.quotes-light",
    "image": "ph.image-light",
    "camera": "ph.camera-light",
    "lightbulb": "ph.lightbulb-light",
    "paperclip": "ph.paperclip-light",
    "robot": "ph.robot-light",
    "desktop": "ph.desktop-light",
    "warning": "ph.warning-light",
    "info": "ph.info-light",
    "dot": "ph.circle-fill",
    "circle": "ph.circle",
    "lock": "ph.lock-simple-light",
    "lock_open": "ph.lock-simple-open-light",
    "split_h": "ph.columns-light",
    "board": "ph.kanban-light",
    "users": "ph.users-three-light",
    "share": "ph.export-light",
    "table": "ph.table-light",
    "list_bullets": "ph.list-bullets-light",
    "list_numbers": "ph.list-numbers-light",
    "indent": "ph.arrow-bend-down-right-light",
    "outdent": "ph.arrow-left-light",
    "eraser": "ph.eraser-light",
    "caret_up": "ph.caret-up",
    "math_ops": "ph.math-operations-light",
    "highlighter": "ph.highlighter-circle-light",
    "italic": "ph.text-italic-light",
    "underline": "ph.text-underline-light",
    "strikethrough": "ph.text-strikethrough-light",
    "text_t": "ph.text-t-light",
    "matrix": "ph.brackets-square-light",
}

_qta_ready: Optional[bool] = None


def _ensure_qta() -> bool:
    """Inizializza qtawesome in modo sicuro (gestendo eventuali restrizioni su Windows Fonts)."""
    global _qta_ready
    if _qta_ready is not None:
        return _qta_ready
    if qta is None:
        _qta_ready = False
        return False
    try:
        qta._instance()
        _qta_ready = True
    except PermissionError:
        import os
        prev = os.environ.pop("LOCALAPPDATA", None)
        try:
            qta._resource["iconic"] = None
            qta._instance()
            _qta_ready = True
        except Exception:
            _qta_ready = False
        finally:
            if prev is not None:
                os.environ["LOCALAPPDATA"] = prev
    except Exception:
        _qta_ready = False
    return _qta_ready


def resolve_icon_color(theme_key: str = "obsidian", role: str = "text") -> str:
    """Restituisce il colore esadecimale coerente con la palette del tema attivo."""
    from ui.themes import _THEME_PALETTES, _is_dark_theme

    pal = _THEME_PALETTES.get(theme_key) or _THEME_PALETTES["obsidian"]
    is_dark = _is_dark_theme(theme_key)

    if role == "muted":
        return pal.get("muted", "#94A3B8" if is_dark else "#64748B")
    if role == "subtitle":
        return pal.get("subtitle", "#94A3B8" if is_dark else "#475569")
    if role == "title":
        return pal.get("title", "#F8FAFC" if is_dark else "#0F172A")
    if role == "accent":
        return pal.get("shortcut_key_text") or pal.get("badge_text") or ("#38BDF8" if is_dark else "#0284C7")
    if role == "on_accent":
        return "#FFFFFF" if is_dark else "#FFFFFF"
    if role == "success":
        return "#34D399" if is_dark else "#16A34A"
    if role == "warning":
        return "#FBBF24" if is_dark else "#D97706"
    if role == "danger":
        return "#F87171" if is_dark else "#DC2626"
    return pal.get("text", "#E2E8F0" if is_dark else "#1E293B")


def get_icon(
    name: str,
    color: Optional[str] = None,
    theme_key: str = "obsidian",
    role: str = "text",
    size: int = 16,
    opacity: float = 1.0,
) -> QIcon:
    """Genera un QIcon vettoriale monocromatico da nome semantico o chiave Phosphor."""
    resolved_color = color or resolve_icon_color(theme_key, role)
    if opacity < 1.0:
        qc = QColor(resolved_color)
        qc.setAlphaF(max(0.0, min(1.0, opacity)))
        resolved_color = qc

    ph_name = ICON_MAP.get(name, name)
    if not ph_name.startswith("ph."):
        ph_name = f"ph.{ph_name}"

    if _ensure_qta():
        try:
            return qta.icon(ph_name, color=resolved_color)
        except Exception:
            # Fallback senza suffisso -light se la variante non esiste
            if ph_name.endswith("-light"):
                try:
                    return qta.icon(ph_name[:-6], color=resolved_color)
                except Exception:
                    pass
    return QIcon()


def get_pixmap(
    name: str,
    color: Optional[str] = None,
    theme_key: str = "obsidian",
    role: str = "text",
    size: int = 16,
    opacity: float = 1.0,
) -> QPixmap:
    """Restituisce un QPixmap nitido dell'icona vettoriale alla dimensione richiesta."""
    icon = get_icon(name, color=color, theme_key=theme_key, role=role, size=size, opacity=opacity)
    if icon.isNull():
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        return pm
    return icon.pixmap(QSize(size, size))


def _hex_to_colorref(hex_color: str) -> int:
    """Converte #RRGGBB in COLORREF Win32 (0x00BBGGRR)."""
    qc = QColor(hex_color)
    if not qc.isValid():
        return 0x000F0907
    return (qc.blue() << 16) | (qc.green() << 8) | qc.red()


def apply_win11_titlebar(widget: QWidget, theme_key: str, reading_mode: bool = False) -> None:
    """Sincronizza il colore della barra del titolo nativa Windows 11 con il tema QSS attivo."""
    if sys.platform != "win32" or widget is None:
        return
    try:
        from ui.themes import _THEME_PALETTES, _is_dark_theme

        hwnd = int(widget.winId())
        if not hwnd:
            return

        pal = _THEME_PALETTES.get(theme_key) or _THEME_PALETTES["obsidian"]
        is_dark = _is_dark_theme(theme_key)

        if reading_mode:
            bg_hex = "#1C1917" if is_dark else "#F5EFE6"
        else:
            bg_hex = pal.get("root_bg", "#07090F" if is_dark else "#F4F6FA")

        dwmapi = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36

        dark_val = ctypes.c_int(1 if is_dark else 0)
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark_val), ctypes.sizeof(dark_val)
        )

        caption_color = ctypes.c_int(_hex_to_colorref(bg_hex))
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(caption_color), ctypes.sizeof(caption_color)
        )

        title_hex = pal.get("title", "#F8FAFC" if is_dark else "#0F172A")
        text_color = ctypes.c_int(_hex_to_colorref(title_hex))
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_TEXT_COLOR, ctypes.byref(text_color), ctypes.sizeof(text_color)
        )
    except Exception:
        pass
