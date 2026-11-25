"""
ui/orb_radial_menu.py
Unico vero Voice Orb al centro in alto (stesso disegno canonico di VoiceOrbOverlay)
+ Ventaglio di Plasma compatto a 3 spicchi da 60° (semicerchio inferiore 180°):
  - Note (FlowNotes)          -> Sinistra (120°–180°) · Ciano   #38BDF8 · page_idx = 1
  - Calendario (FlowTime)     -> Centro   (60°–120°)  · Viola   #A855F7 · page_idx = 7
  - Trascrizione (FlowType)   -> Destra   (0°–60°)    · Smeraldo #10B981 · page_idx = 0
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
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
from PyQt6.QtWidgets import QWidget

from core.audio_analyzer import AudioAnalyzer
from ui.icons import get_pixmap
from ui.voice_orb import create_smooth_closed_path, paint_voice_orb_body


RADIAL_WORLD_ITEMS: List[Dict[str, Any]] = [
    {
        "key": "notes",
        "label": "Note",
        "page_idx": 1,
        "color": "#38BDF8",
        "icon": "notes",
        "start_deg": 120.0,
        "end_deg": 180.0,
        "mid_deg": 146.0,
    },
    {
        "key": "calendar",
        "label": "Calendario",
        "page_idx": 7,
        "color": "#A855F7",
        "icon": "calendar",
        "start_deg": 60.0,
        "end_deg": 120.0,
        "mid_deg": 90.0,
    },
    {
        "key": "transcription",
        "label": "Trascrizione",
        "page_idx": 0,
        "color": "#10B981",
        "icon": "mic",
        "start_deg": 0.0,
        "end_deg": 60.0,
        "mid_deg": 34.0,
    },
]

WORLD_BY_KEY: Dict[str, Dict[str, Any]] = {it["key"]: it for it in RADIAL_WORLD_ITEMS}


class TopOrbAnchorWidget(QWidget):
    """Segnaposto invisibile nella Top Bar per centrare l'Orb e intercettare hover/click."""

    hoverEntered = pyqtSignal()
    hoverLeft = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, size: int = 44, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def enterEvent(self, event):
        self.hoverEntered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hoverLeft.emit()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class InAppVoiceOrbWidget(QWidget):
    """
    UNICO vero Voice Orb vivo al centro in alto di FlowLocalHub.
    Sempre visibile in stato IDLE (3 fuochi fluidi canonici senza bordi artificiali),
    si attiva direttamente sullo stesso widget durante la dettatura o l'elaborazione.
    """

    activeStateChanged = pyqtSignal(bool)
    CORE_RADIUS = 17.0

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        anchor_widget: Optional[QWidget] = None,
        is_dark: bool = True,
    ):
        super().__init__(parent)
        self._anchor_widget = anchor_widget
        self._is_dark = bool(is_dark)
        self._theme_key: str = "obsidian"
        self._palette: Dict[str, str] = {}

        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.analyzer = AudioAnalyzer(num_half_bands=12, intensity=0.80, smoothing=0.50)
        self.state: str = "IDLE"
        self.is_locked: bool = False
        self.lock_start_time: float = 0.0
        self.badge_text: str = ""
        self.badge_opacity: float = 0.0
        self._badge_show_until: float = 0.0
        self.toast_message: str = ""
        self.toast_end_time: float = 0.0
        self.toast_opacity: float = 0.0

        self._active_world: str = "notes"
        self._hovered: bool = False
        self._menu_open_factor: float = 0.0
        self._delegated_to_menu: bool = False
        self._radial_menu: Optional[Any] = None
        self.wave_envelope: float = 0.0
        self.spinner_opacity: float = 0.0
        self.spinner_angle: float = 0.0
        self._plasma_phase: float = 0.0
        self._orb_scale: float = 1.0
        self.current_color: QColor = QColor("#38BDF8")

        self._orb_cx: float = 150.0
        self._orb_cy: float = 28.0
        self._target_w: float = 300.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.sync_geometry()
        self.show()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_geometry()
        if not self._timer.isActive():
            self._timer.start(16)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def is_dictating(self) -> bool:
        return self.state != "IDLE"

    def set_anchor_widget(self, anchor_widget: Optional[QWidget]):
        self._anchor_widget = anchor_widget
        self.sync_geometry()

    def set_active_world(self, world_key: str):
        if world_key in WORLD_BY_KEY:
            self._active_world = world_key
            self.update()

    def set_hovered(self, hovered: bool):
        self._hovered = bool(hovered)
        self.update()

    def set_menu_open_factor(self, val: float):
        self._menu_open_factor = max(0.0, min(1.0, float(val)))
        self.update()

    def set_delegated_to_menu(self, active: bool):
        self._delegated_to_menu = bool(active)
        self.update()

    def set_radial_menu(self, menu: Optional[Any]):
        self._radial_menu = menu

    def plasma_phase(self) -> float:
        return self._plasma_phase

    def sync_plasma_phase(self, phase: float):
        self._plasma_phase = float(phase)

    def set_theme(self, theme_key: str = "obsidian", palette: Optional[Dict[str, str]] = None, is_dark: bool = True):
        self._theme_key = theme_key
        if palette is not None:
            self._palette = dict(palette)
        self._is_dark = bool(is_dark)
        self.update()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if self._anchor_widget is not None and self._anchor_widget.isVisible():
            center = self._anchor_widget.mapTo(parent, self._anchor_widget.rect().center())
        else:
            center = QPoint(max(150, parent.width() // 2), 28)
        w = min(max(300, int(round(self._target_w))), max(300, parent.width()))
        h = 116
        x = max(0, min(parent.width() - w, int(round(center.x() - w / 2.0))))
        self._orb_cx = float(center.x() - x)
        self._orb_cy = max(24.0, float(center.y()))
        self.setGeometry(x, 0, w, h)

    def global_orb_center(self) -> QPoint:
        eff_cy = self._orb_cy + 6.0 * max(self.wave_envelope, self.spinner_opacity)
        return self.mapToGlobal(QPoint(int(round(self._orb_cx)), int(round(eff_cy))))

    def _compute_target_width(self) -> float:
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)
        base_w = 300.0
        if is_toast_active:
            fm = QFontMetrics(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            return max(base_w, float(fm.horizontalAdvance(self.toast_message) + 56))
        if self.badge_text:
            fm = QFontMetrics(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            return max(base_w, float(fm.horizontalAdvance(self.badge_text) + 52))
        return base_w

    def set_state(self, new_state: str):
        was_dictating = self.is_dictating()
        self.state = new_state or "IDLE"
        is_now_dictating = self.is_dictating()
        if is_now_dictating:
            self._delegated_to_menu = False
            if self._radial_menu is not None and self._radial_menu.is_menu_open():
                self._radial_menu.close_immediate()
        self._target_w = self._compute_target_width()
        self.sync_geometry()
        if is_now_dictating:
            self.raise_()
        if was_dictating != is_now_dictating:
            self.activeStateChanged.emit(is_now_dictating)
        self.update()

    def feed_audio(self, audio_chunk: np.ndarray):
        if self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE"):
            self.analyzer.process_chunk(audio_chunk)

    def set_locked(self, locked: bool):
        self.is_locked = bool(locked)
        if self.is_locked:
            self.lock_start_time = time.time()
        self.update()

    def set_badge_text(self, text: str):
        self.badge_text = text or ""
        self._badge_show_until = (time.time() + 1.8) if self.badge_text else 0.0
        self._target_w = self._compute_target_width()
        self.sync_geometry()
        self.update()

    def show_toast(self, message: str, duration_s: float = 2.5):
        self.toast_message = message or ""
        self.toast_end_time = time.time() + float(duration_s)
        self._target_w = self._compute_target_width()
        self.sync_geometry()
        self.raise_()
        self.update()

    def set_whis_state(self, state: str):
        if state == "LISTENING":
            self.set_badge_text("✨ Whis • In ascolto...")
            self.current_color = QColor("#38BDF8")
            self.set_state("WHISPER")
        elif state == "THINKING":
            self.set_badge_text("✨ Whis • Elaborazione...")
            self.current_color = QColor("#38BDF8")
            self.set_state("QWEN")
        elif state == "IDLE":
            self.set_badge_text("")
            self.set_state("IDLE")

    @staticmethod
    def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
        return QColor(
            int(c1.red() + (c2.red() - c1.red()) * t),
            int(c1.green() + (c2.green() - c1.green()) * t),
            int(c1.blue() + (c2.blue() - c1.blue()) * t),
            int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
        )

    def _tick(self):
        if not self.isVisible():
            return
        self.spinner_angle = (self.spinner_angle + 6.5) % 360.0
        is_listening = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        is_processing = self.state in ("WHISPER", "QWEN")
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)

        target_wave = 1.0 if is_listening else 0.0
        target_spin = 1.0 if is_processing else 0.0
        badge_active = (
            self.state != "IDLE"
            and bool(self.badge_text)
            and not is_toast_active
            and time.time() < self._badge_show_until
        )
        target_badge = 1.0 if badge_active else 0.0
        target_toast = 1.0 if is_toast_active else 0.0
        target_scale = 1.08 if (self._hovered or self._menu_open_factor > 0.2) and self.state == "IDLE" else 1.0

        self.wave_envelope += (target_wave - self.wave_envelope) * 0.24
        self.spinner_opacity += (target_spin - self.spinner_opacity) * 0.24
        self.badge_opacity += (target_badge - self.badge_opacity) * 0.24
        self.toast_opacity += (target_toast - self.toast_opacity) * 0.24
        self._orb_scale += (target_scale - self._orb_scale) * 0.22

        cur_energy = (
            float(max(np.mean(self.analyzer.current_levels), np.mean(self.analyzer.target_levels)))
            if is_listening
            else 0.0
        )
        if is_listening:
            if cur_energy < 0.035:
                self._plasma_phase += 0.025
        elif is_processing:
            self._plasma_phase += 0.045
        else:
            self._plasma_phase += 0.024 + 0.025 * self._menu_open_factor + (0.012 if self._hovered else 0.0)

        if not is_toast_active and self.toast_opacity < 0.03 and self.toast_message:
            self.toast_message = ""
            self._target_w = self._compute_target_width()
            self.sync_geometry()

        if is_toast_active:
            target_col = QColor("#10B981")
        elif "Selezione" in self.badge_text:
            target_col = QColor("#F59E0B")
        elif self.state == "LISTENING":
            target_col = QColor("#38BDF8")
        elif self.state == "COMMAND_MODE":
            target_col = QColor("#F59E0B")
        elif self.state == "STUDY_MODE":
            target_col = QColor("#A855F7")
        elif self.state == "WHISPER":
            target_col = QColor("#818CF8")
        elif self.state == "QWEN":
            target_col = QColor("#10B981")
        else:
            # In stato IDLE l'Orb mostra i suoi 3 fuochi canonici (#38BDF8 / #A855F7 / #10B981)
            target_col = QColor("#38BDF8")

        self.current_color = self._lerp_color(self.current_color, target_col, 0.16)
        self.update()

    def paintEvent(self, event):
        if self._delegated_to_menu and self.state == "IDLE":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)
        is_listening = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        levels = self.analyzer.update(active=is_listening)
        avg_energy = float(np.mean(levels)) if len(levels) > 0 else 0.0

        # A riposo leggermente più compatto (14.5px), durante la dettatura raggiunge i 17.0px canonici
        idle_r = 14.5 + 1.0 * max(self._menu_open_factor, 0.5 if self._hovered else 0.0)
        active_mix = max(self.wave_envelope, self.spinner_opacity)
        core_radius = idle_r + (self.CORE_RADIUS - idle_r) * active_mix + (avg_energy * 2.0 if is_listening else 0.0)

        cx = self._orb_cx
        cy = self._orb_cy + 6.0 * active_mix

        paint_voice_orb_body(
            painter,
            cx,
            cy,
            core_radius=core_radius,
            levels=levels,
            plasma_phase=self._plasma_phase,
            wave_envelope=self.wave_envelope,
            spinner_opacity=self.spinner_opacity,
            spinner_angle=self.spinner_angle,
            current_color=self.current_color,
            state=self.state,
            is_active=is_listening,
            is_locked=self.is_locked,
            lock_start_time=self.lock_start_time,
            orb_scale=self._orb_scale,
            max_wave_height=16.5,
        )

        # Pillola di stato / toast centrata subito sotto l'Orb solo quando attiva
        pill_text = self.toast_message if is_toast_active else (self.badge_text if self.badge_opacity > 0.02 else "")
        pill_alpha = self.toast_opacity if is_toast_active else self.badge_opacity
        if pill_text and pill_alpha > 0.02:
            painter.save()
            painter.setOpacity(pill_alpha)
            f_pill = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            fm = QFontMetrics(f_pill)
            pill_w = min(float(self.width() - 16), float(fm.horizontalAdvance(pill_text) + 28))
            pill_h = 22.0
            pill_x = cx - pill_w / 2.0
            pill_y = cy + core_radius + 14.0
            pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)
            bg_c = QColor(14, 18, 26, 240) if self._is_dark else QColor(255, 255, 255, 244)
            bd_c = QColor("#10B981" if is_toast_active else self.current_color.name())
            bd_c.setAlpha(175)
            painter.setBrush(bg_c)
            painter.setPen(QPen(bd_c, 1.2))
            painter.drawRoundedRect(pill_rect, pill_h / 2.0, pill_h / 2.0)

            dot_c = QColor("#10B981" if is_toast_active else self.current_color.name())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot_c)
            painter.drawEllipse(QPointF(pill_x + 11.0, pill_y + pill_h / 2.0), 2.8, 2.8)

            painter.setFont(f_pill)
            painter.setPen(QColor("#F8FAFC" if self._is_dark else "#0F172A"))
            painter.drawText(
                QRectF(pill_x + 18.0, pill_y, pill_w - 24.0, pill_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                pill_text,
            )
            painter.restore()


# Alias per retrocompatibilità di import/test
InAppOrbActiveOverlay = InAppVoiceOrbWidget


class OrbRadialMenuOverlay(QWidget):
    """
    Menù radiale continuo: le stesse 3 curve fluide Catmull-Rom del Voice Orb
    si allungano organicamente verso i 3 mondi (zero aloni separati, zero cuciture):
      - Note (Sinistra 120°–180°)      -> Ciano    #38BDF8
      - Calendario (Centro 60°–120°)  -> Viola    #A855F7
      - Trascrizione (Destra 0°–60°)  -> Smeraldo #10B981
    """

    worldSelected = pyqtSignal(int)
    openProgressChanged = pyqtSignal(float)

    OVERLAY_W = 380
    OVERLAY_H = 196
    INNER_RADIUS = 20.0
    OUTER_RADIUS = 148.0
    LABEL_RADIUS = 84.0

    def __init__(
        self,
        parent: QWidget,
        anchor_widget: Optional[QWidget] = None,
        orb_widget: Optional[QWidget] = None,
        theme_key: str = "obsidian",
        is_dark: bool = True,
    ):
        super().__init__(parent)
        self._anchor_widget = anchor_widget or orb_widget
        self._top_orb_widget = orb_widget
        self._theme_key = theme_key
        self._is_dark = bool(is_dark)
        self._palette: Dict[str, str] = {}
        self._active_world: str = "notes"

        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._origin = QPointF(180.0, 28.0)
        self._is_open: bool = False
        self._open_progress: float = 0.0
        self._phase: float = 0.0
        self._hovered_idx: int = -1
        self._item_scales: List[float] = [1.0, 1.0, 1.0]
        self._item_glow: List[float] = [0.0, 0.0, 0.0]

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)

        self._open_delay_timer = QTimer(self)
        self._open_delay_timer.setSingleShot(True)
        self._open_delay_timer.setInterval(120)
        self._open_delay_timer.timeout.connect(self.open_immediate)

        self._close_grace_timer = QTimer(self)
        self._close_grace_timer.setSingleShot(True)
        self._close_grace_timer.setInterval(240)
        self._close_grace_timer.timeout.connect(self.close_animated)

        self.sync_geometry()
        self.hide()

    def set_top_orb_widget(self, orb_widget: Optional[QWidget]):
        self._top_orb_widget = orb_widget

    def set_active_world(self, world_key: str):
        if world_key in WORLD_BY_KEY:
            self._active_world = world_key
            self.update()

    def set_theme(self, theme_key: str, palette: Dict[str, str], is_dark: bool):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        self._is_dark = bool(is_dark)
        self.update()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if self._anchor_widget is not None and self._anchor_widget.isVisible():
            c = self._anchor_widget.mapTo(parent, self._anchor_widget.rect().center())
        else:
            c = QPoint(max(180, parent.width() // 2), 28)
        w = min(self.OVERLAY_W, max(300, parent.width()))
        h = min(self.OVERLAY_H, max(180, parent.height()))
        x = max(0, min(parent.width() - w, int(round(c.x() - w / 2.0))))
        self._origin = QPointF(float(c.x() - x), max(24.0, float(c.y())))
        self.setGeometry(x, 0, w, h)

    def is_menu_open(self) -> bool:
        return self._is_open

    def schedule_open(self):
        self._close_grace_timer.stop()
        if self._is_open:
            return
        self._open_delay_timer.start()

    def schedule_close(self):
        self._open_delay_timer.stop()
        if self._is_open or self._open_progress > 0.01:
            self._close_grace_timer.start()

    def open_immediate(self):
        self._open_delay_timer.stop()
        self._close_grace_timer.stop()
        self.sync_geometry()
        self._is_open = True
        if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "plasma_phase"):
            self._phase = self._top_orb_widget.plasma_phase()
            self._top_orb_widget.set_delegated_to_menu(True)
        self.show()
        self.raise_()
        if self._top_orb_widget is not None:
            self._top_orb_widget.raise_()
        if not self._anim_timer.isActive():
            self._anim_timer.start(16)
        self.update()

    def close_animated(self):
        self._open_delay_timer.stop()
        self._close_grace_timer.stop()
        self._is_open = False
        self._hovered_idx = -1
        if not self._anim_timer.isActive() and self._open_progress > 0.01:
            self._anim_timer.start(16)

    def close_immediate(self):
        self._open_delay_timer.stop()
        self._close_grace_timer.stop()
        self._is_open = False
        self._open_progress = 0.0
        self._hovered_idx = -1
        self._item_scales = [1.0, 1.0, 1.0]
        self._item_glow = [0.0, 0.0, 0.0]
        self._anim_timer.stop()
        if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "set_delegated_to_menu"):
            self._top_orb_widget.set_delegated_to_menu(False)
        self.openProgressChanged.emit(0.0)
        self.hide()

    def toggle_immediate(self):
        if self._is_open:
            self.close_animated()
        else:
            self.open_immediate()

    def _tick(self):
        self._phase += 0.036
        if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "sync_plasma_phase"):
            self._top_orb_widget.sync_plasma_phase(self._phase)
        target_p = 1.0 if self._is_open else 0.0
        self._open_progress += (target_p - self._open_progress) * 0.22
        if abs(target_p - self._open_progress) < 0.006:
            self._open_progress = target_p
        self.openProgressChanged.emit(self._open_progress)

        for idx, item in enumerate(RADIAL_WORLD_ITEMS):
            is_hov = (idx == self._hovered_idx)
            is_act = (item["key"] == self._active_world)
            tgt_scale = 1.14 if is_hov else (1.04 if is_act else 1.0)
            tgt_glow = 1.0 if is_hov else (0.40 if is_act else 0.14)
            self._item_scales[idx] += (tgt_scale - self._item_scales[idx]) * 0.24
            self._item_glow[idx] += (tgt_glow - self._item_glow[idx]) * 0.22

        if not self._is_open and self._open_progress <= 0.005:
            self._open_progress = 0.0
            self._anim_timer.stop()
            if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "set_delegated_to_menu"):
                self._top_orb_widget.set_delegated_to_menu(False)
            self.hide()
            return

        self.update()

    def _item_rect_and_center(self, idx: int) -> Tuple[QRectF, QPointF]:
        item = RADIAL_WORLD_ITEMS[idx]
        p = self._open_progress
        ease = 1.0 - (1.0 - p) ** 3

        ang_rad = math.radians(float(item["mid_deg"]))
        r = self.LABEL_RADIUS * ease
        drift_x = math.sin(self._phase * 1.15 + idx * 2.094) * 2.8 * ease
        drift_y = math.cos(self._phase * 0.90 + idx * 2.618) * 2.2 * ease

        cx = self._origin.x() + r * math.cos(ang_rad) + drift_x
        cy = self._origin.y() + r * math.sin(ang_rad) + drift_y
        rect = QRectF(cx - 46.0, cy - 24.0, 92.0, 48.0)
        return rect, QPointF(cx, cy)

    def _hit_test_item(self, pos: QPointF) -> int:
        if self._open_progress < 0.25:
            return -1
        dx = pos.x() - self._origin.x()
        dy = pos.y() - self._origin.y()
        dist = math.hypot(dx, dy)
        if dist < 20.0 or dist > self.OUTER_RADIUS + 12.0:
            return -1
        ang = math.degrees(math.atan2(dy, dx))
        if -15.0 <= ang < 0.0:
            ang = 0.0
        elif -180.0 <= ang <= -165.0:
            ang = 180.0
        if 120.0 <= ang <= 180.0:
            return 0  # Note
        if 60.0 <= ang < 120.0:
            return 1  # Calendario
        if 0.0 <= ang < 60.0:
            return 2  # Trascrizione
        return -1

    def _is_in_radial_zone(self, pos: QPointF) -> bool:
        dx = pos.x() - self._origin.x()
        dy = pos.y() - self._origin.y()
        dist = math.hypot(dx, dy)
        if dist <= 26.0:
            return True
        return (dy >= -16.0) and (dist <= self.OUTER_RADIUS + 14.0)

    def mouseMoveEvent(self, event):
        pos = event.position()
        hit = self._hit_test_item(pos)
        if hit != self._hovered_idx:
            self._hovered_idx = hit
            self.update()
        dist = math.hypot(pos.x() - self._origin.x(), pos.y() - self._origin.y())
        if hit >= 0 or dist <= 24.0:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        if self._is_open:
            if self._is_in_radial_zone(pos):
                self._close_grace_timer.stop()
            else:
                self._close_grace_timer.start()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_idx = -1
        if self._is_open:
            self._close_grace_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            hit = self._hit_test_item(pos)
            if hit >= 0:
                page_idx = int(RADIAL_WORLD_ITEMS[hit]["page_idx"])
                self.close_animated()
                self.worldSelected.emit(page_idx)
                event.accept()
                return
            self.close_animated()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_animated()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _build_flame_lobe_path(
        fx: float,
        fy: float,
        base_r: float,
        f_ang: float,
        target_rad: float,
        stretch_len: float,
        sigma: float,
        plasma_phase: float,
        flame_idx: int,
        wobble_amp: float,
    ) -> QPainterPath:
        """Curva continua Catmull-Rom: identica al fuoco dell'Orb a riposo e si allunga in petalo fluido."""
        num_pts = 48
        f_points: List[QPointF] = []
        for i in range(num_pts):
            theta = (2.0 * math.pi * i) / num_pts - (math.pi / 2.0)
            d_ang = math.atan2(math.sin(theta - target_rad), math.cos(theta - target_rad))
            lobe = math.exp(-0.5 * (abs(d_ang) / sigma) ** 2.3)
            wobble = (
                1.0
                + wobble_amp * math.sin(theta * 2.0 + f_ang)
                + 0.025 * lobe * math.sin(plasma_phase * 1.9 + theta * 3.0 + flame_idx * 2.1)
            )
            r_pt = (base_r * wobble) + (lobe * stretch_len)
            f_points.append(QPointF(fx + r_pt * math.cos(theta), fy + r_pt * math.sin(theta)))
        return create_smooth_closed_path(f_points)

    def paintEvent(self, event):
        p = self._open_progress
        if p <= 0.005:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        ox, oy = self._origin.x(), self._origin.y()
        ease = 1.0 - (1.0 - p) ** 3

        idle_core = 14.5
        wobble_amp = 0.045
        orbit_r = 2.4
        flame_specs = [
            (QColor("#38BDF8"), self._phase, 1.00, 195),
            (QColor("#A855F7"), self._phase * 1.35 + 2.1, 0.92, 185),
            (QColor("#10B981"), -self._phase * 0.9 + 4.2, 0.86, 175),
        ]

        # 1. Costruisci le 3 curve continue Catmull-Rom (estensione diretta delle fiamme dell'Orb)
        flame_geoms: List[Tuple[QPainterPath, QColor, float, float, float, float, float, int]] = []
        for idx, item in enumerate(RADIAL_WORLD_ITEMS):
            f_col, f_ang, r_scale, base_alpha = flame_specs[idx]
            glow = self._item_glow[idx]
            target_rad = math.radians(float(item["mid_deg"]))
            base_r = idle_core * 0.88 * r_scale
            stretch_len = ease * (112.0 + 16.0 * glow)
            sigma = 0.43 + 0.05 * glow

            fx = ox + orbit_r * math.cos(f_ang)
            fy = oy + orbit_r * math.sin(f_ang * 1.15)
            f_path = self._build_flame_lobe_path(
                fx,
                fy,
                base_r,
                f_ang,
                target_rad,
                stretch_len,
                sigma,
                self._phase,
                idx,
                wobble_amp,
            )
            flame_geoms.append((f_path, f_col, fx, fy, target_rad, base_r, stretch_len, base_alpha))

        # 2. Silhouette scura organica esattamente sotto il nucleo e i 3 petali (copre linee UI sotto)
        painter.setPen(Qt.PenStyle.NoPen)
        center_r = idle_core * 1.08
        center_bg = QRadialGradient(ox, oy, center_r)
        center_bg.setColorAt(0.0, QColor(8, 10, 16, 220))
        center_bg.setColorAt(0.70, QColor(8, 10, 16, 160))
        center_bg.setColorAt(1.0, QColor(8, 10, 16, 0))
        painter.setBrush(center_bg)
        painter.drawEllipse(QPointF(ox, oy), center_r, center_r)

        dark_peak = int(205 * p) if self._is_dark else int(185 * p)
        dark_mid = int(165 * p) if self._is_dark else int(145 * p)
        for f_path, _f_col, fx, fy, target_rad, base_r, stretch_len, _ba in flame_geoms:
            ux, uy = math.cos(target_rad), math.sin(target_rad)
            gx = fx + ux * stretch_len * 0.38
            gy = fy + uy * stretch_len * 0.38
            g_rad = max(12.0, base_r + stretch_len * 0.72)
            dg = QRadialGradient(gx, gy, g_rad)
            dg.setColorAt(0.0, QColor(8, 10, 16, dark_peak))
            dg.setColorAt(0.68, QColor(8, 10, 16, dark_mid))
            dg.setColorAt(1.0, QColor(8, 10, 16, 0))
            painter.setBrush(dg)
            painter.drawPath(f_path)

        # 3. Riempimento unico canonico in CompositionMode_Screen (nessuno stacco tra Orb e petali)
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        painter.setPen(Qt.PenStyle.NoPen)
        for idx, (f_path, f_col, fx, fy, target_rad, base_r, stretch_len, base_alpha) in enumerate(flame_geoms):
            glow = self._item_glow[idx]
            is_hov = (idx == self._hovered_idx)
            dim_other = 0.70 if (self._hovered_idx >= 0 and not is_hov) else 1.0
            eff_alpha = int(min(235.0, (base_alpha + 40.0 * glow) * dim_other))

            ux, uy = math.cos(target_rad), math.sin(target_rad)
            gx = fx + ux * stretch_len * 0.42
            gy = fy + uy * stretch_len * 0.42
            g_rad = max(12.0, base_r + stretch_len * 0.68)

            f_grad = QRadialGradient(gx, gy, g_rad)
            c_hot = QColor(f_col)
            c_hot.setAlpha(eff_alpha)
            c_mid = QColor(f_col)
            c_mid.setAlpha(int(eff_alpha * 0.72))
            c_edge = QColor(f_col)
            c_edge.setAlpha(int(eff_alpha * 0.24))
            c_tip = QColor(f_col)
            c_tip.setAlpha(0)
            f_grad.setColorAt(0.0, c_hot)
            f_grad.setColorAt(0.68, c_mid)
            f_grad.setColorAt(0.94, c_edge)
            f_grad.setColorAt(1.0, c_tip)

            painter.setBrush(f_grad)
            painter.drawPath(f_path)
        painter.restore()

        # 4. Icona + Testo nitido dentro il cuore di ciascuna fiamma
        item_geos = [self._item_rect_and_center(i) for i in range(len(RADIAL_WORLD_ITEMS))]
        for idx, item in enumerate(RADIAL_WORLD_ITEMS):
            _rect, center = item_geos[idx]
            ix, iy = center.x(), center.y()
            col = QColor(item["color"])
            is_hov = (idx == self._hovered_idx)
            is_act = (item["key"] == self._active_world)
            scale = (0.72 + 0.28 * ease) * self._item_scales[idx]

            painter.save()
            painter.setOpacity(min(1.0, p * 1.15))
            painter.translate(ix, iy)
            painter.scale(scale, scale)

            ic_color = "#FFFFFF" if is_hov else item["color"]
            ic_pm = get_pixmap(item["icon"], color=ic_color, size=18)
            if not ic_pm.isNull():
                painter.drawPixmap(QPointF(-9.0, -18.0), ic_pm)

            lbl_font = QFont("Segoe UI", 9, QFont.Weight.Bold if (is_hov or is_act) else QFont.Weight.DemiBold)
            lbl_font.setPointSizeF(9.5)
            painter.setFont(lbl_font)
            txt_rect = QRectF(-48.0, 2.0, 96.0, 20.0)

            painter.setPen(QColor(4, 7, 14, int(215 * p)))
            painter.drawText(txt_rect.translated(0.0, 1.1), Qt.AlignmentFlag.AlignCenter, item["label"])
            painter.setPen(QColor("#FFFFFF" if (is_hov or is_act) else "#F1F5F9"))
            painter.drawText(txt_rect, Qt.AlignmentFlag.AlignCenter, item["label"])

            if is_act:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(col)
                painter.drawEllipse(QPointF(0.0, 24.0), 2.5, 2.5)

            painter.restore()
