"""
ui/orb_radial_menu.py
Unico vero Voice Orb al centro in alto (stesso disegno canonico di VoiceOrbOverlay)
+ Floating Glass Dock compatto ancorato subito sotto l'Orb per passare tra i 3 mondi:
  - Note (FlowNotes)          -> Sinistra · Ciano    #38BDF8 · page_idx = 1
  - Calendario (FlowTime)     -> Centro   · Viola    #A855F7 · page_idx = 7
  - Trascrizione (FlowType)   -> Destra   · Smeraldo #10B981 · page_idx = 0
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from core.audio_analyzer import AudioAnalyzer
from ui.icons import get_pixmap
from ui.voice_orb import paint_voice_orb_body


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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent; border: none;")

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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
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
        self._orb_cy: float = 26.0
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
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.update()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if self._anchor_widget is not None and self._anchor_widget.isVisible():
            center = self._anchor_widget.mapTo(parent, self._anchor_widget.rect().center())
        else:
            center = QPoint(max(150, parent.width() // 2), 26)
        w = min(max(300, int(round(self._target_w))), max(300, parent.width()))
        h = 112
        x = max(0, min(parent.width() - w, int(round(center.x() - w / 2.0))))
        self._orb_cx = float(center.x() - x)
        self._orb_cy = max(22.0, float(center.y()))
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
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
            target_col = QColor("#38BDF8")

        self.current_color = self._lerp_color(self.current_color, target_col, 0.16)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)
        is_listening = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        levels = self.analyzer.update(active=is_listening)
        avg_energy = float(np.mean(levels)) if len(levels) > 0 else 0.0

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
    Floating Glass Dock compatto ancorato subito sotto il Voice Orb:
      - Note (FlowNotes)        -> Sinistra · Ciano    #38BDF8
      - Calendario (FlowTime)   -> Centro   · Viola    #A855F7
      - Trascrizione (FlowType) -> Destra   · Smeraldo #10B981
    Progettato senza WA_TranslucentBackground su widget figlio così rimane sempre cliccabile
    anche dopo molteplici cambi di tema su Qt6 Windows.
    """

    worldSelected = pyqtSignal(int)
    openProgressChanged = pyqtSignal(float)

    OVERLAY_W = 368
    OVERLAY_H = 114
    DOCK_W = 340.0
    DOCK_H = 50.0
    DOCK_Y = 52.0
    DOCK_RADIUS = 16.0
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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._origin = QPointF(184.0, 26.0)
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
        self._open_delay_timer.setInterval(110)
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
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        self.setEnabled(True)
        self.setMouseTracking(True)
        self.update()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if self._anchor_widget is not None and self._anchor_widget.isVisible():
            c = self._anchor_widget.mapTo(parent, self._anchor_widget.rect().center())
        else:
            c = QPoint(max(184, parent.width() // 2), 26)
        w = min(self.OVERLAY_W, max(320, parent.width()))
        h = min(self.OVERLAY_H, max(114, parent.height()))
        x = max(0, min(parent.width() - w, int(round(c.x() - w / 2.0))))
        self._origin = QPointF(float(c.x() - x), max(22.0, float(c.y())))
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
        gpos = QCursor.pos()
        if self.isVisible() and self.rect().contains(self.mapFromGlobal(gpos)):
            return
        if self._anchor_widget is not None and self._anchor_widget.isVisible():
            if self._anchor_widget.rect().contains(self._anchor_widget.mapFromGlobal(gpos)):
                return
        if self._is_open or self._open_progress > 0.01:
            self._close_grace_timer.start()

    def open_immediate(self):
        self._open_delay_timer.stop()
        self._close_grace_timer.stop()
        self.sync_geometry()
        self._is_open = True
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setEnabled(True)
        self.setMouseTracking(True)
        if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "plasma_phase"):
            self._phase = self._top_orb_widget.plasma_phase()
        self.show()
        self.raise_()
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
        self._open_progress += (target_p - self._open_progress) * 0.26
        if abs(target_p - self._open_progress) < 0.006:
            self._open_progress = target_p
        self.openProgressChanged.emit(self._open_progress)

        for idx, item in enumerate(RADIAL_WORLD_ITEMS):
            is_hov = (idx == self._hovered_idx)
            is_act = (item["key"] == self._active_world)
            tgt_scale = 1.04 if is_hov else (1.01 if is_act else 1.0)
            tgt_glow = 1.0 if is_hov else (0.50 if is_act else 0.10)
            self._item_scales[idx] += (tgt_scale - self._item_scales[idx]) * 0.26
            self._item_glow[idx] += (tgt_glow - self._item_glow[idx]) * 0.24

        if not self._is_open and self._open_progress <= 0.005:
            self._open_progress = 0.0
            self._anim_timer.stop()
            if self._top_orb_widget is not None and hasattr(self._top_orb_widget, "set_delegated_to_menu"):
                self._top_orb_widget.set_delegated_to_menu(False)
            self.hide()
            return

        self.update()

    def _dock_rect(self) -> QRectF:
        p = self._open_progress
        ease = 1.0 - (1.0 - p) ** 3
        w = min(self.DOCK_W, max(280.0, float(self.width()) - 28.0))
        x = (float(self.width()) - w) / 2.0
        y = self.DOCK_Y - 10.0 * (1.0 - ease)
        return QRectF(x, y, w, self.DOCK_H)

    def _item_rect_and_center(self, idx: int) -> Tuple[QRectF, QPointF]:
        dock = self._dock_rect().adjusted(6.0, 5.0, -6.0, -5.0)
        gap = 4.0
        seg_w = (dock.width() - gap * 2.0) / 3.0
        x = dock.left() + idx * (seg_w + gap)
        rect = QRectF(x, dock.top(), seg_w, dock.height())
        return rect, rect.center()

    def _hit_test_item(self, pos: QPointF) -> int:
        if self._open_progress < 0.18:
            return -1
        for idx in range(len(RADIAL_WORLD_ITEMS)):
            rect, _center = self._item_rect_and_center(idx)
            if rect.adjusted(-2.0, -3.0, 2.0, 3.0).contains(pos):
                return idx
        return -1

    def _is_in_radial_zone(self, pos: QPointF) -> bool:
        dock = self._dock_rect().adjusted(-12.0, -10.0, 12.0, 12.0)
        if dock.contains(pos):
            return True
        # Ponte invisibile tra l'Orb nella TopBar e il Dock sottostante
        return abs(pos.x() - self._origin.x()) <= 36.0 and -4.0 <= pos.y() <= self.DOCK_Y + 8.0

    def enterEvent(self, event):
        self._close_grace_timer.stop()
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position()
        hit = self._hit_test_item(pos)
        if hit != self._hovered_idx:
            self._hovered_idx = hit
            self.update()
        on_orb = abs(pos.x() - self._origin.x()) <= 24.0 and 0.0 <= pos.y() <= 46.0
        if hit >= 0 or on_orb:
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
    def _parse_hex_color(val: Optional[str], fallback: str) -> QColor:
        if val and str(val).strip().startswith("#"):
            c = QColor(str(val).strip())
            if c.isValid():
                return c
        return QColor(fallback)

    def paintEvent(self, event):
        p = self._open_progress
        if p <= 0.005:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        ease = 1.0 - (1.0 - p) ** 3
        dock = self._dock_rect()

        painter.save()
        painter.setOpacity(min(1.0, p * 1.15))

        # 1. Ombra morbida multi-livello (niente QGraphicsDropShadowEffect)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, y_off, alpha in ((8.0, 5.0, 18), (5.0, 3.0, 30), (2.5, 1.5, 44)):
            sh_rect = dock.adjusted(-spread, -spread + y_off, spread, spread + y_off)
            painter.setBrush(QColor(0, 0, 0, int(alpha * ease) if self._is_dark else int(alpha * 0.55 * ease)))
            painter.drawRoundedRect(sh_rect, self.DOCK_RADIUS + spread * 0.5, self.DOCK_RADIUS + spread * 0.5)

        # 2. Superficie glass ad alta opacità allineata al tema
        dock_path = QPainterPath()
        dock_path.addRoundedRect(dock, self.DOCK_RADIUS, self.DOCK_RADIUS)

        bg_col = self._parse_hex_color(
            self._palette.get("dialog_bg") or self._palette.get("ribbon_bg"),
            "#101521" if self._is_dark else "#FFFFFF",
        )
        bg_col.setAlpha(244 if self._is_dark else 248)
        painter.fillPath(dock_path, bg_col)

        # 3. Micro-bagliore aurora sul bordo superiore centrato sul mondo attivo/hovered
        glow_idx = self._hovered_idx
        if glow_idx < 0:
            for idx, item in enumerate(RADIAL_WORLD_ITEMS):
                if item["key"] == self._active_world:
                    glow_idx = idx
                    break
        if glow_idx < 0:
            glow_idx = 0

        glow_item = RADIAL_WORLD_ITEMS[glow_idx]
        glow_col = QColor(glow_item["color"])
        _, glow_center = self._item_rect_and_center(glow_idx)

        painter.save()
        painter.setClipPath(dock_path)
        top_aura = QRadialGradient(glow_center.x(), dock.top() + 2.0, dock.width() * 0.42)
        c_aura_in = QColor(glow_col)
        c_aura_in.setAlpha(48 if self._is_dark else 32)
        c_aura_mid = QColor(glow_col)
        c_aura_mid.setAlpha(14 if self._is_dark else 10)
        c_aura_out = QColor(glow_col)
        c_aura_out.setAlpha(0)
        top_aura.setColorAt(0.0, c_aura_in)
        top_aura.setColorAt(0.55, c_aura_mid)
        top_aura.setColorAt(1.0, c_aura_out)
        painter.setBrush(top_aura)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(dock)
        painter.restore()

        # 4. Bordo hairline 1px
        border_col = QColor(255, 255, 255, 26) if self._is_dark else QColor(15, 23, 42, 24)
        painter.setPen(QPen(border_col, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(dock_path)

        # 5. I 3 segmenti orizzontali
        fg_idle = self._parse_hex_color(self._palette.get("subtitle"), "#94A3B8" if self._is_dark else "#475569")
        fg_active = self._parse_hex_color(self._palette.get("title"), "#F8FAFC" if self._is_dark else "#0F172A")

        for idx, item in enumerate(RADIAL_WORLD_ITEMS):
            seg_rect, seg_center = self._item_rect_and_center(idx)
            col = QColor(item["color"])
            is_hov = (idx == self._hovered_idx)
            is_act = (item["key"] == self._active_world)
            glow = self._item_glow[idx]

            # Pillola di evidenziazione su hover / attivo
            if is_hov or is_act or glow > 0.12:
                pill_fill = QColor(col)
                fill_alpha = int((38 if is_hov else 24) * glow) if self._is_dark else int((34 if is_hov else 20) * glow)
                pill_fill.setAlpha(max(0, min(60, fill_alpha)))
                pill_bdr = QColor(col)
                bdr_alpha = int((125 if is_hov else 80) * glow)
                pill_bdr.setAlpha(max(0, min(150, bdr_alpha)))
                painter.setBrush(pill_fill)
                painter.setPen(QPen(pill_bdr, 1.0))
                painter.drawRoundedRect(seg_rect, 11.0, 11.0)

            ic_color = item["color"] if (is_hov or is_act) else fg_idle.name()
            ic_pm = get_pixmap(item["icon"], color=ic_color, size=14)
            lbl_font = QFont("Segoe UI", 9, QFont.Weight.DemiBold if (is_hov or is_act) else QFont.Weight.Medium)
            lbl_font.setPointSizeF(9.0)
            fm = QFontMetrics(lbl_font)
            txt_w = fm.horizontalAdvance(item["label"])
            ic_w = 14.0 if not ic_pm.isNull() else 0.0
            spacing = 5.0 if ic_w > 0 else 0.0
            total_w = ic_w + spacing + txt_w
            start_x = seg_center.x() - total_w / 2.0

            painter.save()
            if not ic_pm.isNull():
                painter.drawPixmap(QPointF(start_x, seg_center.y() - 7.0), ic_pm)

            painter.setFont(lbl_font)
            painter.setPen(fg_active if (is_hov or is_act) else fg_idle)
            txt_rect = QRectF(start_x + ic_w + spacing, seg_rect.top(), txt_w + 4.0, seg_rect.height() - (3.0 if is_act else 0.0))
            painter.drawText(txt_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, item["label"])

            if is_act:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(col)
                bar_w = 14.0
                painter.drawRoundedRect(
                    QRectF(seg_center.x() - bar_w / 2.0, seg_rect.bottom() - 4.0, bar_w, 2.2),
                    1.1,
                    1.1,
                )
            painter.restore()

        painter.restore()
