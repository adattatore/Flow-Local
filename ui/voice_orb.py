"""
VoiceOrbOverlay (PyQt6)
- Stato IDLE: Piccolo bottone sottile minimale (54x18 px), discreto, zero ingombro.
- Stati ATTIVI (Listening, Command, Study, Whisper, Qwen): Voice Orb espanso (140x140 px)
  con singola onda polare Catmull-Rom -> Bézier (intensity=0.80, smoothing=0.50),
  bagliore radiale ed animazioni fluide di stato a 60 FPS.
- 100% trasparente, zero furto di focus, trascinabile e con click-to-talk garantito.
"""

import math
import time
import numpy as np
import sounddevice as sd
from typing import Callable, Optional

from PyQt6.QtWidgets import QApplication, QWidget, QMenu
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal, QObject
from PyQt6.QtGui import (
    QPainter, QColor, QPainterPath, QRadialGradient, QPen, QAction, QFont, QFontMetrics
)
from PyQt6.QtSvg import QSvgRenderer

from core.audio_analyzer import AudioAnalyzer

MIC_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"
     fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
  <line x1="12" x2="12" y1="19" y2="22"/>
</svg>
"""


def create_smooth_closed_path(points: list[QPointF]) -> QPainterPath:
    """Converte N punti polari in una curva chiusa continua C1 (Catmull-Rom -> Bézier)."""
    path = QPainterPath()
    n = len(points)
    if n < 3:
        return path

    path.moveTo(points[0])
    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        p3 = points[(i + 2) % n]

        c1 = QPointF(
            p1.x() + (p2.x() - p0.x()) / 6.0,
            p1.y() + (p2.y() - p0.y()) / 6.0
        )
        c2 = QPointF(
            p2.x() - (p3.x() - p1.x()) / 6.0,
            p2.y() - (p3.y() - p1.y()) / 6.0
        )
        path.cubicTo(c1, c2, p2)

    path.closeSubpath()
    return path


class OrbSignalBridge(QObject):
    change_state = pyqtSignal(str)
    feed_audio_signal = pyqtSignal(object)
    set_locked_signal = pyqtSignal(bool)
    set_badge_signal = pyqtSignal(str)
    show_toast_signal = pyqtSignal(str, float)


class VoiceOrbOverlay(QWidget):
    IDLE_W = 60
    IDLE_H = 22
    ACTIVE_W = 140
    ACTIVE_H = 140

    def __init__(self, on_click_toggle: Optional[Callable[[], None]] = None, on_open_hub: Optional[Callable[[], None]] = None, on_change_mode: Optional[Callable[[str], None]] = None):
        super().__init__()
        self.on_click_toggle = on_click_toggle
        self.on_open_hub = on_open_hub
        self.on_change_mode = on_change_mode

        self.analyzer = AudioAnalyzer(num_half_bands=12, intensity=0.80, smoothing=0.50)
        self.state = "IDLE"  # IDLE, LISTENING, COMMAND_MODE, STUDY_MODE, WHISPER, QWEN
        self.is_locked = False
        self.lock_start_time = 0.0

        # Badge Contesto Attivo ("{app_label} • {mode_label}")
        self.badge_text = ""
        self.badge_opacity = 0.0

        # Notifica Toast di Apprendimento ("+ Imparato: X → Y")
        self.toast_message = ""
        self.toast_end_time = 0.0
        self.toast_opacity = 0.0

        # Animazioni e interpolazioni (lerp)
        self.wave_envelope = 0.0     # 0.0 = nascosta, 1.0 = visibile
        self.spinner_opacity = 0.0   # 0.0 = invisibile, 1.0 = visibile
        self.spinner_angle = 0.0
        self.idle_breath = 0.0
        self.current_color = QColor("#64748B")

        # Ghost Mode (dopo 2 minuti di inattività)
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0

        # Ancoraggio e trascinamento
        self.anchor_x = 0
        self.anchor_y = 0
        self._drag_start = None
        self._drag_moved = False
        self._press_time = 0.0

        # Signal bridge per chiamate esterne thread-safe
        self.bridge = OrbSignalBridge()
        self.bridge.change_state.connect(self._set_state_slot)
        self.bridge.feed_audio_signal.connect(self._feed_audio_slot)
        self.bridge.set_locked_signal.connect(self._set_locked_slot)
        self.bridge.set_badge_signal.connect(self._set_badge_slot)
        self.bridge.show_toast_signal.connect(self._show_toast_slot)

        self._setup_window()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)  # ~60 FPS

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("FlowLocal (Tieni premuto Caps Lock o clicca per parlare)")

        avail = QApplication.primaryScreen().availableGeometry()
        self.anchor_x = avail.x() + avail.width() // 2
        self.anchor_y = avail.y() + avail.height() - 36

        self._apply_geometry_for_state("IDLE")

    def _apply_geometry_for_state(self, state: str):
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)

        if state == "IDLE" and not is_toast_active:
            w, h = self.IDLE_W, self.IDLE_H
        else:
            active_text = self.toast_message if is_toast_active else self.badge_text
            if active_text:
                f_app = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
                f_mode = QFont("Segoe UI", 9, QFont.Weight.Normal)
                fm_app = QFontMetrics(f_app)
                fm_mode = QFontMetrics(f_mode)
                if " • " in active_text:
                    p1, p2 = active_text.split(" • ", 1)
                    text_w = fm_app.horizontalAdvance(p1) + fm_app.horizontalAdvance(" • ") + fm_mode.horizontalAdvance(p2)
                else:
                    text_w = fm_app.horizontalAdvance(active_text)

                badge_w = text_w + 36
                gap = 12
                orb_w = 140
                w = int(orb_w + gap + badge_w + 14)
                h = self.ACTIVE_H
            else:
                w, h = self.ACTIVE_W, self.ACTIVE_H

        self.setGeometry(
            int(self.anchor_x - w / 2),
            int(self.anchor_y - h / 2),
            w,
            h
        )

    def set_badge_text(self, text: str):
        self.bridge.set_badge_signal.emit(text)

    def _set_badge_slot(self, text: str):
        self.badge_text = text
        if self.state != "IDLE" or (self.toast_message and time.time() < self.toast_end_time):
            self._apply_geometry_for_state(self.state)
        self.update()

    def show_toast(self, message: str, duration_s: float = 2.5):
        self.bridge.show_toast_signal.emit(message, duration_s)

    def _show_toast_slot(self, message: str, duration_s: float):
        self.toast_message = message
        self.toast_end_time = time.time() + duration_s
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0
        self._apply_geometry_for_state(self.state)
        self.update()

    def set_state(self, new_state: str):
        self.bridge.change_state.emit(new_state)

    def _set_state_slot(self, new_state: str):
        old_state = self.state
        self.state = new_state
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0

        # Ridimensiona il widget tra bottone sottile (54x18) e Voice Orb (140x140)
        if old_state == "IDLE" and new_state != "IDLE":
            self._apply_geometry_for_state(new_state)
        elif old_state != "IDLE" and new_state == "IDLE":
            self._apply_geometry_for_state("IDLE")

        self.update()

    def set_locked(self, locked: bool):
        self.bridge.set_locked_signal.emit(locked)

    def _set_locked_slot(self, locked: bool):
        self.is_locked = locked
        if locked:
            self.lock_start_time = time.time()
        self.update()

    def feed_audio(self, audio_chunk: np.ndarray):
        self.bridge.feed_audio_signal.emit(audio_chunk)

    def _feed_audio_slot(self, audio_chunk: np.ndarray):
        if self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE"):
            self.analyzer.process_chunk(audio_chunk)

    def show_listening(self):
        self.set_state("LISTENING")

    def show_whisper(self):
        self.set_state("WHISPER")

    def show_qwen(self):
        self.set_state("QWEN")

    def hide_orb(self):
        self.set_state("IDLE")

    def _lerp_color(self, c1: QColor, c2: QColor, t: float) -> QColor:
        return QColor(
            int(c1.red() + (c2.red() - c1.red()) * t),
            int(c1.green() + (c2.green() - c1.green()) * t),
            int(c1.blue() + (c2.blue() - c1.blue()) * t),
            int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
        )

    def _tick(self):
        self.idle_breath += 0.04
        self.spinner_angle = (self.spinner_angle + 6.5) % 360

        is_listening = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        is_processing = self.state in ("WHISPER", "QWEN")
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)

        target_wave = 1.0 if is_listening else 0.0
        target_spin = 1.0 if is_processing else 0.0
        target_badge = 1.0 if (self.state != "IDLE" and self.badge_text and not is_toast_active) else 0.0
        target_toast = 1.0 if is_toast_active else 0.0

        self.wave_envelope += (target_wave - self.wave_envelope) * 0.22
        self.spinner_opacity += (target_spin - self.spinner_opacity) * 0.22
        self.badge_opacity += (target_badge - self.badge_opacity) * 0.22
        self.toast_opacity += (target_toast - self.toast_opacity) * 0.22

        if not is_toast_active and self.toast_opacity < 0.03 and self.toast_message:
            self.toast_message = ""
            self._apply_geometry_for_state(self.state)

        # Colore in base allo stato attivo
        if is_toast_active:
            target_col = QColor("#10B981")   # Emerald Glow per apprendimento dizionario
        elif "Selezione" in self.badge_text:
            target_col = QColor("#F59E0B")   # Amber Gold per trasformazione selezione attiva
        elif self.state == "LISTENING":
            target_col = QColor("#38BDF8")   # Electric Cyan
        elif self.state == "COMMAND_MODE":
            target_col = QColor("#F59E0B")   # Amber Gold
        elif self.state == "STUDY_MODE":
            target_col = QColor("#A855F7")   # Amethyst Purple
        elif self.state == "WHISPER":
            target_col = QColor("#818CF8")   # Neon Indigo
        elif self.state == "QWEN":
            target_col = QColor("#10B981")   # Emerald Glow
        else:
            target_col = QColor("#64748B")   # Slate Grey (IDLE)

        self.current_color = self._lerp_color(self.current_color, target_col, 0.15)

        # Ghost Mode dopo inattività prolungata
        if self.state == "IDLE":
            idle_duration = time.time() - self.last_activity_time
            if idle_duration > 120.0:
                self.ghost_opacity += (0.25 - self.ghost_opacity) * 0.05
            else:
                self.ghost_opacity += (1.0 - self.ghost_opacity) * 0.1
        else:
            self.ghost_opacity = 1.0

        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._drag_moved = False
            self._press_time = time.time()
            self.last_activity_time = time.time()
            self.ghost_opacity = 1.0

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            delta = event.globalPosition().toPoint() - self._drag_start
            if delta.manhattanLength() > 5:
                self._drag_moved = True
                self.anchor_x += delta.x()
                self.anchor_y += delta.y()
                self._apply_geometry_for_state(self.state)
                self._drag_start = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._drag_moved:
                if self.on_click_toggle:
                    self.on_click_toggle()
            self._drag_moved = False
            self._drag_start = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.ghost_opacity < 0.99:
            painter.setOpacity(self.ghost_opacity)

        cx, cy = self.width() / 2.0, self.height() / 2.0
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)

        # =========================================================
        # 1. STATO IDLE PURO: BOTTONE SOTTILE MINIMALE (60x22 px)
        # =========================================================
        if self.state == "IDLE" and not is_toast_active:
            btn_w = float(self.width() - 2)
            btn_h = float(self.height() - 2)
            btn_rect = QRectF(1.0, 1.0, btn_w, btn_h)
            radius = btn_h / 2.0

            # Sfondo Obsidian Glass scuro
            painter.setBrush(QColor(18, 19, 26, 240))
            breath_alpha = int(70 + 45 * math.sin(self.idle_breath))
            painter.setPen(QPen(QColor(56, 189, 248, breath_alpha), 1.2))
            painter.drawRoundedRect(btn_rect, radius, radius)

            # Icona microfono vettoriale centrata (Lucide SVG)
            icon_color = "#38BDF8" if breath_alpha > 90 else "#94A3B8"
            svg_data = MIC_SVG.format(color=icon_color).encode("utf-8")
            svg_renderer = QSvgRenderer(svg_data)
            icon_size = 13.0
            svg_renderer.render(
                painter,
                QRectF(cx - icon_size / 2.0, cy - icon_size / 2.0, icon_size, icon_size)
            )
            return

        # =========================================================
        # 2. STATI ATTIVI O TOAST: VOICE ORB (140x140 px)
        # =========================================================
        if self.badge_text or is_toast_active:
            cx = 70.0
            cy = self.height() / 2.0
        else:
            cx, cy = self.width() / 2.0, self.height() / 2.0

        core_radius = 26.0  # Raggio cerchio centrale (diametro 52px)
        is_active = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        levels = self.analyzer.update(active=is_active)

        # Bagliore radiale + Onda polare singola Catmull-Rom
        if self.wave_envelope > 0.01:
            max_wave_height = 24.0 * self.wave_envelope
            avg_energy = float(np.mean(levels))

            glow_radius = core_radius + 14.0 + (avg_energy * 22.0)
            grad = QRadialGradient(cx, cy, glow_radius)
            c_center = QColor(self.current_color)
            c_center.setAlpha(int(115 * self.wave_envelope))
            c_edge = QColor(self.current_color)
            c_edge.setAlpha(0)
            grad.setColorAt(0.35, c_center)
            grad.setColorAt(1.0, c_edge)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawEllipse(QPointF(cx, cy), glow_radius, glow_radius)

            num_pts = len(levels)
            wave_points = []
            for i in range(num_pts):
                theta = (2.0 * math.pi * i) / num_pts - (math.pi / 2.0)
                r = core_radius + (2.0 * self.wave_envelope) + (levels[i] * max_wave_height)
                px = cx + r * math.cos(theta)
                py = cy + r * math.sin(theta)
                wave_points.append(QPointF(px, py))

            wave_path = create_smooth_closed_path(wave_points)
            wave_fill = QColor(self.current_color)
            wave_fill.setAlpha(int(220 * self.wave_envelope))
            painter.setBrush(wave_fill)
            painter.drawPath(wave_path)

        # Cerchio nero centrale (nucleo da 52px)
        painter.setBrush(QColor(10, 10, 14, 248))
        border_color = QColor(self.current_color)
        border_color.setAlpha(200)
        painter.setPen(QPen(border_color, 1.6))
        painter.drawEllipse(QPointF(cx, cy), core_radius, core_radius)

        # Icona SVG o Timer Hands-Free
        if self.is_locked and is_active:
            elapsed = int(time.time() - self.lock_start_time)
            mins = elapsed // 60
            secs = elapsed % 60
            time_str = f"{mins:02d}:{secs:02d}"

            painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            painter.setPen(QColor("#F8FAFC"))
            painter.drawText(QRectF(cx - 24, cy - 8, 48, 16), Qt.AlignmentFlag.AlignCenter, time_str)

            painter.setBrush(QColor("#EF4444"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(cx - 3, cy + 9, 6, 6), 1, 1)

        else:
            mic_opacity = 1.0 - self.spinner_opacity
            if mic_opacity > 0.05:
                painter.save()
                painter.setOpacity(mic_opacity)
                icon_hex = "#FFFFFF" if is_active else "#94A3B8"
                svg_data = MIC_SVG.format(color=icon_hex).encode("utf-8")
                renderer = QSvgRenderer(svg_data)
                icon_size = 20.0
                renderer.render(
                    painter,
                    QRectF(cx - icon_size / 2, cy - icon_size / 2, icon_size, icon_size)
                )
                painter.restore()

        # Spinner circolare interno (Whisper -> Qwen)
        if self.spinner_opacity > 0.05:
            painter.save()
            painter.setOpacity(self.spinner_opacity)

            spin_radius = core_radius - 6.5
            spin_rect = QRectF(
                cx - spin_radius, cy - spin_radius,
                spin_radius * 2, spin_radius * 2
            )
            track_pen = QPen(QColor(255, 255, 255, 25), 2.5)
            painter.setPen(track_pen)
            painter.drawEllipse(spin_rect)

            arc_pen = QPen(self.current_color, 2.8)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)
            start_angle = int(-self.spinner_angle * 16)
            span_angle = int(-110 * 16)
            painter.drawArc(spin_rect, start_angle, span_angle)
            painter.restore()

        # =========================================================
        # 3. NOTIFICA TOAST DI APPRENDIMENTO: "+ Imparato: {errata} → {corretta}"
        # =========================================================
        if is_toast_active and self.toast_opacity > 0.01:
            painter.save()
            painter.setOpacity(self.toast_opacity)

            gap = 12.0
            badge_h = 28.0
            badge_x = 140.0 + gap
            badge_y = cy - (badge_h / 2.0)
            badge_w = max(40.0, self.width() - badge_x - 12.0)
            badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)
            radius = badge_h / 2.0

            # 1. Sfondo pillola Obsidian Glass con riflesso smeraldo
            painter.setBrush(QColor(14, 18, 22, 235))
            border_col = QColor("#10B981")
            border_col.setAlpha(int(140 * self.toast_opacity))
            painter.setPen(QPen(border_col, 1.3))
            painter.drawRoundedRect(badge_rect, radius, radius)

            # 2. Dot indicatore smeraldo brillante
            dot_radius = 3.5
            dot_cx = badge_x + 13.0
            dot_cy = cy
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#10B981"))
            painter.drawEllipse(QPointF(dot_cx, dot_cy), dot_radius, dot_radius)

            # 3. Testo toast
            text_x = dot_cx + 9.0
            avail_text_w = badge_rect.right() - text_x - 10.0

            f_bold = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
            f_norm = QFont("Segoe UI", 9, QFont.Weight.Normal)
            fm_bold = QFontMetrics(f_bold)
            fm_norm = QFontMetrics(f_norm)

            if " → " in self.toast_message:
                left_part, right_part = self.toast_message.split(" → ", 1)
                arrow_str = " → "
                total_w = fm_bold.horizontalAdvance(left_part) + fm_norm.horizontalAdvance(arrow_str) + fm_bold.horizontalAdvance(right_part)
                start_x = text_x + max(0.0, (avail_text_w - total_w) / 2.0)
                base_y = badge_y + (badge_h + fm_bold.ascent() - fm_bold.descent()) / 2.0

                # Parte sinistra (es. "+ Imparato: errata")
                painter.setFont(f_bold)
                painter.setPen(QColor("#34D399"))  # Emerald soft
                painter.drawText(QPointF(start_x, base_y), left_part)
                start_x += fm_bold.horizontalAdvance(left_part)

                # Freccia
                painter.setFont(f_norm)
                painter.setPen(QColor("#64748B"))
                painter.drawText(QPointF(start_x, base_y), arrow_str)
                start_x += fm_norm.horizontalAdvance(arrow_str)

                # Parte destra (corretta)
                painter.setFont(f_bold)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(QPointF(start_x, base_y), right_part)
            else:
                painter.setFont(f_bold)
                painter.setPen(QColor("#34D399"))
                text_rect = QRectF(text_x, badge_y, avail_text_w, badge_h)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self.toast_message)

            painter.restore()

        # =========================================================
        # 4. BADGE ACCANTO AL VOICE ORB: "{app_label} • {mode_label}"
        # =========================================================
        elif self.badge_text and self.badge_opacity > 0.01:
            painter.save()
            painter.setOpacity(self.badge_opacity)

            gap = 12.0
            badge_h = 28.0
            badge_x = 140.0 + gap
            badge_y = cy - (badge_h / 2.0)
            badge_w = max(40.0, self.width() - badge_x - 12.0)
            badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)
            radius = badge_h / 2.0

            # 1. Pill Background (Obsidian Glass dark semi-transparent)
            painter.setBrush(QColor(15, 17, 24, 220))
            border_col = QColor(self.current_color)
            border_col.setAlpha(int(110 * self.badge_opacity))
            painter.setPen(QPen(border_col, 1.2))
            painter.drawRoundedRect(badge_rect, radius, radius)

            # 2. Glowing Dot Indicator
            dot_radius = 3.5
            dot_cx = badge_x + 13.0
            dot_cy = cy
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.current_color)
            painter.drawEllipse(QPointF(dot_cx, dot_cy), dot_radius, dot_radius)

            # 3. Text Formatting
            text_x = dot_cx + 9.0
            avail_text_w = badge_rect.right() - text_x - 10.0
            text_rect = QRectF(text_x, badge_y, avail_text_w, badge_h)

            if " • " in self.badge_text:
                app_part, mode_part = self.badge_text.split(" • ", 1)
                f_app = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
                f_mode = QFont("Segoe UI", 9, QFont.Weight.Normal)
                fm_app = QFontMetrics(f_app)
                fm_mode = QFontMetrics(f_mode)
                bullet_str = " • "

                total_content_w = fm_app.horizontalAdvance(app_part) + fm_app.horizontalAdvance(bullet_str) + fm_mode.horizontalAdvance(mode_part)
                start_x = text_x + max(0.0, (avail_text_w - total_content_w) / 2.0)
                base_y = badge_y + (badge_h + fm_app.ascent() - fm_app.descent()) / 2.0

                # App Label (Crisp White #F8FAFC)
                painter.setFont(f_app)
                painter.setPen(QColor("#F8FAFC"))
                painter.drawText(QPointF(start_x, base_y), app_part)
                start_x += fm_app.horizontalAdvance(app_part)

                # Bullet Dot (Current mode accent color)
                painter.setPen(self.current_color)
                painter.drawText(QPointF(start_x, base_y), bullet_str)
                start_x += fm_app.horizontalAdvance(bullet_str)

                # Mode Label (#94A3B8 soft slate)
                painter.setFont(f_mode)
                painter.setPen(QColor("#94A3B8"))
                painter.drawText(QPointF(start_x, base_y), mode_part)
            else:
                painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
                painter.setPen(QColor("#F8FAFC"))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self.badge_text)

            painter.restore()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #141419; color: #F8FAFC; border: 1px solid #2D2D3A; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; }"
            "QMenu::item:selected { background-color: #2563EB; }"
        )

        if self.on_open_hub:
            hub_act = QAction("🚀 Apri FlowLocal Hub", self)
            hub_act.triggered.connect(self.on_open_hub)
            menu.addAction(hub_act)
            menu.addSeparator()

        mode_menu = menu.addMenu("Modalità di Dettatura")
        for label, mod in [
            ("⚡ Standard (Push-to-Talk)", "LISTENING"),
            ("📚 Study & Feynman Mode", "STUDY_MODE"),
            ("🪄 Command Mode (Su testo)", "COMMAND_MODE"),
        ]:
            act = QAction(label, self)
            if self.on_change_mode:
                act.triggered.connect(lambda ch, m=mod: self.on_change_mode(m))
            mode_menu.addAction(act)

        menu.addSeparator()

        mic_menu = menu.addMenu("Seleziona Microfono")
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                act = QAction(dev["name"][:35], self)
                mic_menu.addAction(act)

        menu.exec(event.globalPos())
