"""
Command Palette Globale (Ctrl+K) per FlowLocal Hub.
Design futuristico ispirato al Voice Orb ("Tre Fuochi": Cyan -> Indigo -> Violet),
con superficie opaca ad alta leggibilità, ricerca unificata in tempo reale su
FlowNotes (note, cartelle, sbobine), FlowTime (eventi), FlowType (dettature) e Azioni Rapide.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QEvent, QPointF, QRectF, QSize, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.db_manager import db

try:
    from core.dictation_tracker import dictation_tracker
except Exception:
    dictation_tracker = None


def _parse_opaque_color(color_str: str, fallback: str = "#111827") -> QColor:
    """Converte una stringa colore (#hex o rgba(...)) in QColor completamente opaco."""
    s = (color_str or "").strip()
    if s.startswith("rgba(") and s.endswith(")"):
        parts = [p.strip() for p in s[5:-1].split(",")]
        if len(parts) >= 3:
            try:
                return QColor(int(float(parts[0])), int(float(parts[1])), int(float(parts[2])), 255)
            except Exception:
                pass
    c = QColor(s)
    if not c.isValid():
        c = QColor(fallback)
    c.setAlpha(255)
    return c


def _parse_color_rgba(color_str: str, fallback: str = "#6366F1") -> QColor:
    """Converte #hex o rgba(...) preservando l'eventuale canale alpha."""
    s = (color_str or "").strip()
    if s.startswith("rgba(") and s.endswith(")"):
        parts = [p.strip() for p in s[5:-1].split(",")]
        if len(parts) >= 4:
            try:
                a_raw = float(parts[3])
                a = int(a_raw * 255) if a_raw <= 1.0 else int(a_raw)
                return QColor(int(float(parts[0])), int(float(parts[1])), int(float(parts[2])), max(0, min(255, a)))
            except Exception:
                pass
    c = QColor(s)
    if not c.isValid():
        c = QColor(fallback)
    return c


def _blend_over(fg_str: str, bg: QColor, force_alpha: Optional[float] = None) -> QColor:
    """Miscela un colore (anche rgba trasparente) sopra una superficie opaca `bg`."""
    fg = _parse_color_rgba(fg_str)
    a = force_alpha if force_alpha is not None else (fg.alpha() / 255.0)
    a = max(0.0, min(1.0, a))
    r = int(fg.red() * a + bg.red() * (1.0 - a))
    g = int(fg.green() * a + bg.green() * (1.0 - a))
    b = int(fg.blue() * a + bg.blue() * (1.0 - a))
    return QColor(r, g, b, 255)


def _contrast_for(bg: QColor, prefer_light: str = "#F8FAFC", prefer_dark: str = "#0F172A") -> str:
    lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return prefer_dark if lum > 145 else prefer_light


def _is_light_color(c: QColor) -> bool:
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return lum > 145


class _MiniOrbIcon(QWidget):
    """Mini Voice Orb animato stile 'Tre Fuochi' nella barra di ricerca."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(45)

    def _tick(self):
        self._phase = (self._phase + 0.09) % (2.0 * math.pi)
        self.update()

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        pulse = 1.0 + 0.08 * math.sin(self._phase)

        # Alone esterno morbido
        halo = QRadialGradient(cx, cy, 13.5 * pulse)
        halo.setColorAt(0.0, QColor(99, 102, 241, 140))
        halo.setColorAt(0.55, QColor(56, 189, 248, 70))
        halo.setColorAt(1.0, QColor(168, 85, 247, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), 13.5 * pulse, 13.5 * pulse)

        # Tre fuochi (Cyan, Indigo, Violet/Magenta)
        flames = [
            (QColor(56, 189, 248, 220), 0.0),
            (QColor(99, 102, 241, 220), 2.09),
            (QColor(236, 72, 153, 210), 4.18),
        ]
        for col, offset in flames:
            ang = self._phase + offset
            fx = cx + 2.2 * math.cos(ang)
            fy = cy + 2.2 * math.sin(ang)
            g = QRadialGradient(fx, fy, 7.2)
            g.setColorAt(0.0, col)
            g.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(fx, fy), 7.2, 7.2)

        # Nucleo luminoso centrale
        core = QRadialGradient(cx, cy, 4.5)
        core.setColorAt(0.0, QColor(255, 255, 255, 245))
        core.setColorAt(1.0, QColor(129, 140, 248, 0))
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)


class _FuturisticPaletteFrame(QFrame):
    """Contenitore opaco con bordo gradiente 'Tre Fuochi' e bagliore superiore."""

    def __init__(self, is_light: bool, surface_color: QColor, parent=None):
        super().__init__(parent)
        self._is_light = is_light
        self._surface = surface_color

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        radius = 16.0

        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)

        # Superficie principale completamente opaca (zero trasparenza/sovrapposizione)
        p.fillPath(path, QBrush(self._surface))

        # Sottile bagliore radiale in alto a sinistra (effetto orb)
        p.save()
        p.setClipPath(path)
        orb_glow = QRadialGradient(r.left() + 48, r.top() + 26, 220)
        alpha_g = 30 if self._is_light else 45
        orb_glow.setColorAt(0.0, QColor(56, 189, 248, alpha_g))
        orb_glow.setColorAt(0.5, QColor(99, 102, 241, alpha_g // 2))
        orb_glow.setColorAt(1.0, QColor(168, 85, 247, 0))
        p.fillRect(r, QBrush(orb_glow))

        # Barra superiore luminosa 2px (Tre Fuochi)
        top_grad = QLinearGradient(r.left(), r.top(), r.right(), r.top())
        top_grad.setColorAt(0.0, QColor("#38BDF8"))
        top_grad.setColorAt(0.5, QColor("#6366F1"))
        top_grad.setColorAt(1.0, QColor("#EC4899"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(top_grad))
        p.drawRect(QRectF(r.left(), r.top(), r.width(), 2.5))
        p.restore()

        # Bordo gradiente sottile e nitido
        border_grad = QLinearGradient(r.topLeft(), r.bottomRight())
        if self._is_light:
            border_grad.setColorAt(0.0, QColor(56, 189, 248, 170))
            border_grad.setColorAt(0.5, QColor(99, 102, 241, 150))
            border_grad.setColorAt(1.0, QColor(168, 85, 247, 170))
        else:
            border_grad.setColorAt(0.0, QColor(56, 189, 248, 200))
            border_grad.setColorAt(0.5, QColor(99, 102, 241, 180))
            border_grad.setColorAt(1.0, QColor(236, 72, 153, 190))
        p.setPen(QPen(QBrush(border_grad), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


@dataclass
class PaletteEntry:
    kind: str  # "note", "folder", "event", "dictation", "action", "theme", "nav"
    title: str
    subtitle: str = ""
    badge: str = ""
    icon_text: str = "⚡"
    payload: Dict[str, Any] = field(default_factory=dict)


class _PaletteItemWidget(QWidget):
    """Riga ad alta leggibilità (nessun testo tagliato) con elisione pulita e badge pill."""

    def __init__(self, entry: PaletteEntry, title_col: str, sub_col: str, badge_bg: str, badge_fg: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(11)

        icon_lbl = QLabel(entry.icon_text)
        icon_lbl.setFixedSize(28, 28)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            f"font-size: 15px; background-color: {badge_bg}; border-radius: 8px; border: none;"
        )
        lay.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)

        title_font = QFont("Segoe UI", 10)
        title_font.setWeight(QFont.Weight.DemiBold)
        fm_title = QFontMetrics(title_font)
        elided_title = fm_title.elidedText(entry.title, Qt.TextElideMode.ElideRight, 430)

        title_lbl = QLabel(elided_title)
        title_lbl.setFont(title_font)
        title_lbl.setMinimumHeight(20)
        title_lbl.setStyleSheet(
            f"color: {title_col}; background: transparent; border: none;"
        )
        text_col.addWidget(title_lbl)

        if entry.subtitle:
            sub_font = QFont("Segoe UI", 8)
            fm_sub = QFontMetrics(sub_font)
            elided_sub = fm_sub.elidedText(entry.subtitle, Qt.TextElideMode.ElideRight, 430)

            sub_lbl = QLabel(elided_sub)
            sub_lbl.setFont(sub_font)
            sub_lbl.setMinimumHeight(16)
            sub_lbl.setStyleSheet(
                f"color: {sub_col}; background: transparent; border: none;"
            )
            text_col.addWidget(sub_lbl)

        lay.addLayout(text_col, 1)

        if entry.badge:
            badge_lbl = QLabel(entry.badge)
            badge_lbl.setMinimumHeight(22)
            badge_lbl.setStyleSheet(
                f"background-color: {badge_bg}; color: {badge_fg}; "
                f"border-radius: 7px; padding: 2px 9px; font-size: 10px; font-weight: 700;"
            )
            lay.addWidget(badge_lbl, 0, Qt.AlignmentFlag.AlignVCenter)


class GlobalCommandPalette(QDialog):
    """Overlay modale frameless stile Voice Orb / Tre Fuochi per ricerca e comandi rapidi (Ctrl+K)."""

    def __init__(
        self,
        parent: QWidget,
        palette: Dict[str, str],
        themes_map: Dict[str, Dict[str, str]],
        callbacks: Dict[str, Callable[..., Any]],
        reading_mode_active: bool = False,
    ):
        super().__init__(parent)
        self._palette = palette
        self._themes_map = themes_map or {}
        self._callbacks = callbacks or {}
        self._reading_mode_active = reading_mode_active
        self._entries: List[PaletteEntry] = []

        # Calcola colori solidi e ad alto contrasto dal tema attivo (_resolve_palette)
        # Nota: _resolve_palette usa dialog_bg / combo_popup_bg / root_bg / input_bg (non 'bg')
        raw_surface = (
            palette.get("dialog_bg")
            or palette.get("combo_popup_bg")
            or palette.get("root_bg")
            or palette.get("bg")
            or "#0B0F1A"
        )
        # Se root_bg è un gradiente QLinearGradient CSS, estrai il primo colore #hex
        if "gradient" in str(raw_surface).lower():
            m = re.search(r"#[0-9a-fA-F]{6}", str(raw_surface))
            raw_surface = m.group(0) if m else "#0B0F1A"

        self._surface = _parse_opaque_color(raw_surface, "#0B0F1A")
        self._is_light = _is_light_color(self._surface)

        self._title_col = _contrast_for(self._surface)
        if palette.get("title"):
            t_cand = _parse_opaque_color(palette["title"], self._title_col)
            # Usa il colore del tema solo se ha buon contrasto con _surface
            if _is_light_color(t_cand) != self._is_light:
                self._title_col = t_cand.name()
        self._text_col = self._title_col
        self._sub_col = palette.get("subtitle") or ("#64748B" if self._is_light else "#94A3B8")
        if _is_light_color(_parse_opaque_color(self._sub_col)) == self._is_light:
            self._sub_col = "#475569" if self._is_light else "#94A3B8"

        self._accent_col = _parse_opaque_color(palette.get("accent") or "#6366F1", "#6366F1").name()
        self._input_bg = _blend_over(
            palette.get("input_bg") or ("#FFFFFF" if self._is_light else "#111827"),
            self._surface,
        ).name()
        self._input_border = _blend_over(
            palette.get("input_border") or ("#CBD5E1" if self._is_light else "#334155"),
            self._surface,
            force_alpha=0.55 if "rgba" in str(palette.get("input_border", "")) else None,
        ).name()

        # Selezione e hover sempre traslucidi e leggibili sopra _surface (mai solidi sul testo!)
        sel_c = _blend_over(self._accent_col, self._surface, force_alpha=0.20)
        hov_c = _blend_over(self._accent_col, self._surface, force_alpha=0.10)
        self._sel_bg = sel_c.name()
        self._hover_bg = hov_c.name()

        # Badge pill: sfondo morbido al 16% di accent sopra _surface, testo ad alto contrasto
        badge_bg_c = _blend_over(self._accent_col, self._surface, force_alpha=0.16)
        self._badge_bg = badge_bg_c.name()
        if self._is_light:
            # In tema chiaro scurisci leggermente l'accento per il testo del badge
            acc_dark = QColor(self._accent_col).darker(145)
            self._badge_fg = acc_dark.name()
        else:
            acc_light = QColor(self._accent_col).lighter(135)
            self._badge_fg = acc_light.name()

        self._sep_col = _blend_over(
            palette.get("divider") or ("#CBD5E1" if self._is_light else "#334155"),
            self._surface,
            force_alpha=0.45 if "rgba" in str(palette.get("divider", "")) else None,
        ).name()

        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(680)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 22)

        self._container = _FuturisticPaletteFrame(self._is_light, self._surface)
        self._container.setObjectName("CommandPaletteBox")
        shadow = QGraphicsDropShadowEffect(self._container)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(15, 23, 42, 90 if self._is_light else 185))
        self._container.setGraphicsEffect(shadow)

        box_lay = QVBoxLayout(self._container)
        box_lay.setContentsMargins(14, 14, 14, 12)
        box_lay.setSpacing(10)

        # Barra di ricerca con Mini Voice Orb animato
        search_frame = QFrame()
        search_frame.setStyleSheet(
            f"background-color: {self._input_bg}; "
            f"border: 1px solid {self._input_border}; "
            f"border-radius: 11px;"
        )
        search_row = QHBoxLayout(search_frame)
        search_row.setContentsMargins(10, 4, 10, 4)
        search_row.setSpacing(10)

        self._orb_icon = _MiniOrbIcon(search_frame)
        search_row.addWidget(self._orb_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self.search_input = QLineEdit()
        self.search_input.setMinimumHeight(40)
        self.search_input.setPlaceholderText(
            "Cerca note, cartelle, eventi FlowTime, dettature o '>' per comandi..."
        )
        self.search_input.textChanged.connect(self._on_query_changed)
        self.search_input.installEventFilter(self)
        search_row.addWidget(self.search_input, 1)

        k_badge = QLabel("Ctrl+K")
        k_badge.setStyleSheet(
            f"background-color: {self._badge_bg}; color: {self._badge_fg}; "
            f"border-radius: 6px; padding: 3px 8px; font-size: 10px; font-weight: 700; border: none;"
        )
        search_row.addWidget(k_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        box_lay.addWidget(search_frame)

        # Lista risultati spaziosa
        self.results_list = QListWidget()
        self.results_list.setObjectName("CommandPaletteList")
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results_list.setSpacing(3)
        self.results_list.setMinimumHeight(140)
        self.results_list.setMaximumHeight(400)
        self.results_list.itemActivated.connect(self._activate_current_item)
        self.results_list.itemClicked.connect(self._activate_current_item)
        box_lay.addWidget(self.results_list)

        # Footer comandi
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {self._sep_col}; border: none;")
        box_lay.addWidget(sep)

        hint_lbl = QLabel("↑↓ Naviga   •   Invio Apri   •   Esc Chiudi   •   '>' Comandi rapidi")
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        hint_lbl.setStyleSheet(
            f"color: {self._sub_col}; font-size: 11px; padding: 2px 4px; background: transparent; border: none;"
        )
        box_lay.addWidget(hint_lbl)

        outer.addWidget(self._container)
        self._apply_styles()
        self._position_top_center()
        self._populate_results("")

    def _apply_styles(self):
        self.search_input.setStyleSheet(
            f"QLineEdit {{ "
            f"background-color: transparent; "
            f"color: {self._title_col}; "
            f"border: none; "
            f"font-size: 14px; "
            f"font-weight: 500; "
            f"padding: 4px 2px; "
            f"}}"
        )
        self.results_list.setStyleSheet(
            f"QListWidget#CommandPaletteList {{ "
            f"background-color: transparent; "
            f"border: none; "
            f"outline: none; "
            f"}} "
            f"QListWidget#CommandPaletteList::item {{ "
            f"border-radius: 10px; "
            f"border-left: 3px solid transparent; "
            f"}} "
            f"QListWidget#CommandPaletteList::item:selected {{ "
            f"background-color: {self._sel_bg}; "
            f"border-left: 3px solid {self._accent_col}; "
            f"}} "
            f"QListWidget#CommandPaletteList::item:hover {{ "
            f"background-color: {self._hover_bg}; "
            f"}}"
        )

    def _position_top_center(self):
        parent = self.parentWidget()
        if parent is not None:
            geo = parent.geometry()
            x = geo.x() + max(0, (geo.width() - self.width()) // 2)
            y = geo.y() + 64
            self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        self._position_top_center()
        self.search_input.setFocus(Qt.FocusReason.PopupFocusReason)

    def eventFilter(self, obj, event):
        if obj is self.search_input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                row = self.results_list.currentRow()
                if self.results_list.count() > 0:
                    self.results_list.setCurrentRow(min(row + 1, self.results_list.count() - 1))
                return True
            if key == Qt.Key.Key_Up:
                row = self.results_list.currentRow()
                if self.results_list.count() > 0:
                    self.results_list.setCurrentRow(max(row - 1, 0))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._activate_current_item()
                return True
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)

    def _on_query_changed(self, text: str):
        self._populate_results(text)

    def _build_action_entries(self, query_lower: str) -> List[PaletteEntry]:
        actions: List[PaletteEntry] = [
            PaletteEntry(
                kind="action",
                title="Nuova Nota",
                subtitle="Crea un nuovo appunto vuoto in FlowNotes",
                badge="Azione",
                icon_text="📝",
                payload={"action": "new_note"},
            ),
            PaletteEntry(
                kind="action",
                title="Nuova Sbobina",
                subtitle="Avvia registrazione o importazione lezione audio/video",
                badge="Azione",
                icon_text="🎙️",
                payload={"action": "new_sbobina"},
            ),
            PaletteEntry(
                kind="action",
                title=(
                    "Disattiva Modalità Lettura"
                    if self._reading_mode_active
                    else "Attiva Modalità Lettura"
                ),
                subtitle="Espandi o ripristina l'editor a schermo intero",
                badge="Vista",
                icon_text="📖",
                payload={"action": "toggle_reading"},
            ),
            PaletteEntry(
                kind="action",
                title="Novità e Suggerimenti",
                subtitle="Changelog, bug fix recenti e funzioni nascoste di FlowLocal",
                badge="Guida",
                icon_text="💡",
                payload={"action": "open_whats_new"},
            ),
            PaletteEntry(
                kind="action",
                title="Diagnostica di Sistema & Log",
                subtitle="Stato Whisper, Ollama/Gemini, Google Sync ed export log",
                badge="Sistema",
                icon_text="🩺",
                payload={"action": "open_diagnostics"},
            ),
            PaletteEntry(
                kind="nav",
                title="Vai a FlowType",
                subtitle="Dettatura vocale, cronologia e statistiche",
                badge="Scheda",
                icon_text="🎤",
                payload={"page_idx": 0},
            ),
            PaletteEntry(
                kind="nav",
                title="Vai a FlowNotes",
                subtitle="Quaderno universitario, cartelle e flashcard",
                badge="Scheda",
                icon_text="📚",
                payload={"page_idx": 1},
            ),
            PaletteEntry(
                kind="nav",
                title="Vai a FlowTime",
                subtitle="Calendario, scadenze ed esami",
                badge="Scheda",
                icon_text="📅",
                payload={"page_idx": 7},
            ),
            PaletteEntry(
                kind="nav",
                title="Vai a Whis",
                subtitle="Assistente AI contestuale locale",
                badge="Scheda",
                icon_text="✨",
                payload={"page_idx": 4},
            ),
        ]

        for theme_key, t_dict in self._themes_map.items():
            label = t_dict.get("name", theme_key.replace("_", " ").title())
            actions.append(
                PaletteEntry(
                    kind="theme",
                    title=f"Tema: {label}",
                    subtitle=f"Applica il tema visivo {label}",
                    badge="Tema",
                    icon_text="🎨",
                    payload={"theme_key": theme_key},
                )
            )

        if not query_lower:
            return actions
        return [
            a
            for a in actions
            if query_lower in a.title.lower() or query_lower in a.subtitle.lower() or query_lower in a.badge.lower()
        ]

    @staticmethod
    def _extract_snippet(content: str, query: str) -> tuple[str, int]:
        if not content:
            return "", 0
        q_low = query.lower()
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if q_low in line.lower():
                clean = re.sub(r"\s+", " ", line).strip()
                if len(clean) > 85:
                    clean = clean[:82] + "..."
                return clean, idx
        first = re.sub(r"\s+", " ", content).strip()
        return (first[:82] + "...") if len(first) > 85 else first, 0

    @staticmethod
    def _clean_title(title: str) -> str:
        t = (title or "").replace("Nota: ", "").strip()
        t = re.sub(r"^[#*_`>\-\s]+", "", t).strip()
        return t or "Appunto senza titolo"

    def _populate_results(self, raw_query: str):
        query = (raw_query or "").strip()
        entries: List[PaletteEntry] = []

        if query.startswith(">"):
            cmd_q = query[1:].strip().lower()
            entries.extend(self._build_action_entries(cmd_q))
        elif not query:
            # Stato iniziale: ultime note modificate + azioni rapide
            try:
                recent_notes = db.get_notes_by_hierarchy()[:8]
                for row in recent_notes:
                    nid = row.get("id")
                    subj = row.get("subject") or "Generale"
                    topic = row.get("topic") or ""
                    title = self._clean_title(row.get("title") or "")
                    ntype = row.get("note_type") or ""
                    audio_path = row.get("audio_path")
                    is_studied = bool(row.get("is_studied")) or ("Studiata" in (row.get("tags") or ""))
                    icon = "🎙️" if (ntype == "sbobina" or audio_path) else ("✅" if is_studied else "📄")
                    loc = f"{subj} › {topic}" if topic and topic != subj else subj
                    entries.append(
                        PaletteEntry(
                            kind="note",
                            title=title,
                            subtitle=loc,
                            badge="FlowNotes",
                            icon_text=icon,
                            payload={"note_id": nid, "match_line": 0},
                        )
                    )
            except Exception as e:
                print(f"[CommandPalette] Errore lettura note recenti: {e}")
            entries.extend(self._build_action_entries("")[:7])
        else:
            q_low = query.lower()

            # 1. FlowNotes: Note & Sbobine tramite SQLite FTS5 (BM25 ranking, fallback LIKE automatico)
            try:
                notes = db.search_notes_fts(query, limit=25) if hasattr(db, "search_notes_fts") else db.get_notes_by_hierarchy(search_query=query)
                if not notes:
                    notes = db.get_notes_by_hierarchy(search_query=query)
                title_matches: List[PaletteEntry] = []
                body_matches: List[PaletteEntry] = []
                for row in notes:
                    nid = row.get("id")
                    subj = row.get("subject") or "Generale"
                    topic = row.get("topic") or ""
                    raw_title = row.get("title") or ""
                    title = self._clean_title(raw_title)
                    content = row.get("content_markdown") or ""
                    ntype = row.get("note_type") or ""
                    audio_path = row.get("audio_path")
                    is_studied = bool(row.get("is_studied")) or ("Studiata" in (row.get("tags") or ""))
                    snippet, match_line = self._extract_snippet(content, query)
                    loc = f"{subj} › {topic}" if topic and topic != subj else subj
                    sub_txt = f"{loc} — {snippet}" if snippet else loc
                    icon = "🎙️" if (ntype == "sbobina" or audio_path) else ("✅" if is_studied else "📄")
                    pe = PaletteEntry(
                        kind="note",
                        title=title,
                        subtitle=sub_txt,
                        badge="Sbobina" if icon == "🎙️" else "Nota",
                        icon_text=icon,
                        payload={"note_id": nid, "match_line": match_line, "query": query},
                    )
                    if q_low in title.lower() or q_low in raw_title.lower():
                        title_matches.append(pe)
                    else:
                        body_matches.append(pe)
                entries.extend((title_matches + body_matches)[:18])
            except Exception as e:
                print(f"[CommandPalette] Errore ricerca note: {e}")

            # 2. FlowNotes: Cartelle
            try:
                folders = db.get_folders_tree()
                for f in folders:
                    if q_low in (f.get("name") or "").lower():
                        entries.append(
                            PaletteEntry(
                                kind="folder",
                                title=f.get("name", "Cartella"),
                                subtitle="Cartella FlowNotes",
                                badge="Cartella",
                                icon_text="📁",
                                payload={"folder_id": f.get("id")},
                            )
                        )
            except Exception as e:
                print(f"[CommandPalette] Errore ricerca cartelle: {e}")

            # 3. FlowTime: Eventi Calendario
            try:
                events = db.search_calendar_events(query)
                for ev in events[:10]:
                    ev_date = ev.get("event_date", "")
                    ev_time = ev.get("event_time", "")
                    ev_cat = ev.get("category", "Evento")
                    when = f"{ev_date} {ev_time}".strip()
                    entries.append(
                        PaletteEntry(
                            kind="event",
                            title=ev.get("title", "Evento"),
                            subtitle=f"{when} • {ev.get('description', '')}".strip(" •"),
                            badge=ev_cat,
                            icon_text="📅",
                            payload={"event_date": ev_date, "event_id": ev.get("id")},
                        )
                    )
            except Exception as e:
                print(f"[CommandPalette] Errore ricerca eventi: {e}")

            # 4. FlowType: Cronologia dettature (tracker in RAM + DB)
            seen_texts = set()
            if dictation_tracker is not None:
                try:
                    for item in dictation_tracker.get_history():
                        txt = (item.get("text") or "").strip()
                        if txt and q_low in txt.lower() and txt not in seen_texts:
                            seen_texts.add(txt)
                            preview = re.sub(r"\s+", " ", txt)
                            if len(preview) > 76:
                                preview = preview[:73] + "..."
                            entries.append(
                                PaletteEntry(
                                    kind="dictation",
                                    title=preview,
                                    subtitle=f"Dettatura recente ({item.get('timestamp', '')}) — Invio per copiare/incollare",
                                    badge="FlowType",
                                    icon_text="🎙️",
                                    payload={"text": txt},
                                )
                            )
                except Exception:
                    pass

            try:
                rows = db.get_recent_transcriptions(limit=100)
                for r in rows:
                    txt = (r.get("final_text") or r.get("raw_text") or "").strip()
                    ts = str(r.get("created_at") or "")[:16]
                    if txt and q_low in txt.lower() and txt not in seen_texts:
                        seen_texts.add(txt)
                        preview = re.sub(r"\s+", " ", txt)
                        if len(preview) > 76:
                            preview = preview[:73] + "..."
                        entries.append(
                            PaletteEntry(
                                kind="dictation",
                                title=preview,
                                subtitle=f"Cronologia FlowType ({ts}) — Invio per copiare/incollare",
                                badge="FlowType",
                                icon_text="💬",
                                payload={"text": txt},
                            )
                        )
                        if len(seen_texts) >= 12:
                            break
            except Exception as e:
                print(f"[CommandPalette] Errore ricerca trascrizioni: {e}")

            # 5. Azioni & Temi corrispondenti
            entries.extend(self._build_action_entries(q_low))

        self._entries = entries[:45]
        self.results_list.clear()

        if not self._entries:
            empty_item = QListWidgetItem("  Nessun risultato trovato.")
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            empty_item.setSizeHint(QSize(0, 42))
            self.results_list.addItem(empty_item)
            return

        for entry in self._entries:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 58 if entry.subtitle else 44))
            self.results_list.addItem(item)
            self.results_list.setItemWidget(
                item,
                _PaletteItemWidget(
                    entry,
                    self._title_col,
                    self._sub_col,
                    self._badge_bg,
                    self._badge_fg,
                    self.results_list,
                ),
            )

        self.results_list.setCurrentRow(0)

    def _activate_current_item(self, *_args):
        row = self.results_list.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        self.accept()

        if entry.kind == "note":
            cb = self._callbacks.get("open_note")
            if cb:
                cb(
                    entry.payload.get("note_id"),
                    entry.payload.get("match_line", 0),
                    entry.payload.get("query", ""),
                )
        elif entry.kind == "folder":
            cb = self._callbacks.get("open_folder")
            if cb:
                cb(entry.payload.get("folder_id"))
        elif entry.kind == "event":
            cb = self._callbacks.get("open_event")
            if cb:
                cb(entry.payload.get("event_date"))
        elif entry.kind == "dictation":
            txt = entry.payload.get("text", "")
            cb = self._callbacks.get("use_dictation")
            if cb:
                cb(txt)
            else:
                cb_inst = QApplication.clipboard()
                if cb_inst is not None:
                    cb_inst.setText(txt)
        elif entry.kind == "action":
            act = entry.payload.get("action")
            cb = self._callbacks.get(act)
            if cb:
                cb()
        elif entry.kind == "theme":
            cb = self._callbacks.get("set_theme")
            if cb:
                cb(entry.payload.get("theme_key"))
        elif entry.kind == "nav":
            cb = self._callbacks.get("navigate")
            if cb:
                cb(entry.payload.get("page_idx", 0))
