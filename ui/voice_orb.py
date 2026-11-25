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
import pyperclip
import numpy as np
import sounddevice as sd
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication, QWidget, QMenu, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)
from PyQt6.QtCore import (
    Qt, QTimer, QPointF, QRectF, pyqtSignal, QObject, QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QPainter, QColor, QPainterPath, QRadialGradient, QLinearGradient, QConicalGradient,
    QPen, QAction, QFont, QFontMetrics, QCursor, QGuiApplication, QPixmap
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


def render_orb_icon_pixmap(size: int = 64, active: bool = True) -> QPixmap:
    """Renderizza l'icona statica dell'Orb con i 3 fuochi fluidi (usata per tray, finestra e app.ico)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    cx = size / 2.0
    cy = size / 2.0
    outer_r = size * 0.45

    # Base scura Obsidian per contrasto su sfondi chiari e scuri
    base_grad = QRadialGradient(cx, cy, outer_r)
    base_grad.setColorAt(0.0, QColor(12, 16, 26, 250))
    base_grad.setColorAt(0.82, QColor(8, 10, 16, 245))
    base_grad.setColorAt(1.0, QColor(8, 10, 16, 225))
    p.setBrush(base_grad)
    rim_col = QColor(56, 189, 248, 95) if active else QColor(148, 163, 184, 75)
    p.setPen(QPen(rim_col, max(1.0, size * 0.032)))
    p.drawEllipse(QPointF(cx, cy), outer_r, outer_r)

    # I 3 fuochi fluidi (ciano, viola, smeraldo) oppure toni grigi se in pausa
    if active:
        flame_specs = [
            (QColor("#38BDF8"), 0.45, 0.92, 220),
            (QColor("#A855F7"), 2.55, 0.85, 205),
            (QColor("#10B981"), 4.65, 0.80, 195),
        ]
    else:
        flame_specs = [
            (QColor("#94A3B8"), 0.45, 0.88, 145),
            (QColor("#64748B"), 2.55, 0.80, 130),
            (QColor("#475569"), 4.65, 0.74, 120),
        ]

    num_pts = 24
    core_r = outer_r * 0.74
    orbit_r = outer_r * 0.14

    p.save()
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
    p.setPen(Qt.PenStyle.NoPen)

    for f_col, f_ang, r_scale, f_alpha in flame_specs:
        fx = cx + orbit_r * math.cos(f_ang)
        fy = cy + orbit_r * math.sin(f_ang * 1.15)

        f_points = []
        for i in range(num_pts):
            theta = (2.0 * math.pi * i) / num_pts - (math.pi / 2.0)
            # Profilo organico a 3 lobi per richiamare i fuochi vivi dell'orb
            wobble = 1.0 + 0.14 * math.sin(theta * 2.0 + f_ang) + 0.07 * math.cos(theta * 3.0 - f_ang)
            r_pt = core_r * r_scale * wobble
            f_points.append(QPointF(fx + r_pt * math.cos(theta), fy + r_pt * math.sin(theta)))

        f_path = create_smooth_closed_path(f_points)
        f_grad = QRadialGradient(fx, fy, core_r * r_scale * 1.25)
        c_hot = QColor(f_col)
        c_hot.setAlpha(f_alpha)
        c_mid = QColor(f_col)
        c_mid.setAlpha(int(f_alpha * 0.68))
        c_tip = QColor(f_col)
        c_tip.setAlpha(0)
        f_grad.setColorAt(0.0, c_hot)
        f_grad.setColorAt(0.65, c_mid)
        f_grad.setColorAt(1.0, c_tip)

        p.setBrush(f_grad)
        p.drawPath(f_path)

    # Nucleo luminoso centrale
    center_grad = QRadialGradient(cx, cy, core_r * 0.48)
    center_col = QColor(240, 249, 255, 160 if active else 75)
    center_tip = QColor(240, 249, 255, 0)
    center_grad.setColorAt(0.0, center_col)
    center_grad.setColorAt(1.0, center_tip)
    p.setBrush(center_grad)
    p.drawEllipse(QPointF(cx, cy), core_r * 0.48, core_r * 0.48)
    p.restore()

    # Piccolo badge pausa in basso a destra quando la dettatura è sospesa
    if not active:
        badge_r = size * 0.20
        bx = size - badge_r - size * 0.04
        by = size - badge_r - size * 0.04
        p.setPen(QPen(QColor("#94A3B8"), max(1.0, size * 0.03)))
        p.setBrush(QColor(14, 17, 24, 245))
        p.drawEllipse(QPointF(bx, by), badge_r, badge_r)

        bar_w = max(1.2, badge_r * 0.28)
        bar_h = max(3.0, badge_r * 0.85)
        gap = max(1.0, badge_r * 0.24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#F8FAFC"))
        p.drawRoundedRect(QRectF(bx - gap - bar_w * 0.5, by - bar_h * 0.5, bar_w, bar_h), 0.8, 0.8)
        p.drawRoundedRect(QRectF(bx + gap - bar_w * 0.5, by - bar_h * 0.5, bar_w, bar_h), 0.8, 0.8)

    p.end()
    return pixmap


def paint_voice_orb_body(
    painter: QPainter,
    cx: float,
    cy: float,
    *,
    core_radius: float,
    levels: np.ndarray,
    plasma_phase: float,
    wave_envelope: float,
    spinner_opacity: float,
    spinner_angle: float,
    current_color: QColor,
    state: str,
    is_active: bool,
    is_locked: bool = False,
    lock_start_time: float = 0.0,
    orb_scale: float = 1.0,
    max_wave_height: float = 20.0,
    flame_pull_vectors: Optional[List[Tuple[float, float]]] = None,
):
    """Rendering canonico del Voice Orb (3 fuochi fluidi Catmull-Rom, onde vocali e comete)."""
    avg_energy = float(np.mean(levels)) if len(levels) > 0 else 0.0
    num_pts = len(levels) if len(levels) > 0 else 24

    painter.save()
    if orb_scale < 0.999 or orb_scale > 1.001:
        painter.translate(cx, cy)
        painter.scale(orb_scale, orb_scale)
        painter.translate(-cx, -cy)

    c_p1 = QColor(current_color)
    c_p2 = QColor("#A855F7") if state != "STUDY_MODE" else QColor("#38BDF8")
    c_p3 = QColor("#10B981") if state != "QWEN" else QColor("#38BDF8")

    # Piccolo fondo scuro morbido al centro per leggibilità/contrasto su sfondi chiari
    base_r = core_radius * 1.05
    base_grad = QRadialGradient(cx, cy, base_r)
    base_grad.setColorAt(0.0, QColor(8, 10, 16, 215))
    base_grad.setColorAt(0.70, QColor(8, 10, 16, 150))
    base_grad.setColorAt(1.0, QColor(8, 10, 16, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(base_grad)
    painter.drawEllipse(QPointF(cx, cy), base_r, base_r)

    # I 3 fuochi fluidi indipendenti (si dissolvono dolcemente nelle 3 comete durante il caricamento)
    flame_vis = max(0.0, 1.0 - spinner_opacity)
    if flame_vis > 0.02:
        eff_wave_h = max_wave_height * wave_envelope
        speech_mix = min(1.0, avg_energy * 8.0) if is_active else 0.0
        wobble_amp = 0.045 * (1.0 - speech_mix)
        flame_layers = [
            (c_p1, plasma_phase, 0, 1.00, int(195 * flame_vis)),
            (c_p2, plasma_phase * 1.35 + 2.1, 8, 0.92, int(185 * flame_vis)),
            (c_p3, -plasma_phase * 0.9 + 4.2, 16, 0.86, int(175 * flame_vis)),
        ]

        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        for f_idx, (f_col, f_ang, band_offset, r_scale, f_alpha) in enumerate(flame_layers):
            orbit_r = 2.4 * (1.0 - 0.5 * speech_mix)
            pull_x, pull_y = (0.0, 0.0)
            if flame_pull_vectors and f_idx < len(flame_pull_vectors):
                pull_x, pull_y = flame_pull_vectors[f_idx]
            fx = cx + orbit_r * math.cos(f_ang) + pull_x
            fy = cy + orbit_r * math.sin(f_ang * 1.15) + pull_y

            f_points = []
            for i in range(num_pts):
                theta = (2.0 * math.pi * i) / num_pts - (math.pi / 2.0)
                raw_lvl = float(levels[(i + band_offset) % num_pts]) if len(levels) > 0 else 0.0
                lvl = min(1.0, (raw_lvl ** 0.80) * 1.28)
                wobble = 1.0 + wobble_amp * math.sin(theta * 2.0 + f_ang)
                r_pt = (core_radius * 0.88 * r_scale * wobble) + (lvl * eff_wave_h * r_scale)
                f_points.append(QPointF(fx + r_pt * math.cos(theta), fy + r_pt * math.sin(theta)))

            f_path = create_smooth_closed_path(f_points)
            pull_len = math.hypot(pull_x, pull_y)
            f_grad = QRadialGradient(fx, fy, core_radius * r_scale + eff_wave_h + 2.0 + pull_len * 0.35)
            c_hot = QColor(f_col)
            c_hot.setAlpha(f_alpha)
            c_mid = QColor(f_col)
            c_mid.setAlpha(int(f_alpha * 0.72))
            c_tip = QColor(f_col)
            c_tip.setAlpha(0)
            f_grad.setColorAt(0.0, c_hot)
            f_grad.setColorAt(0.68, c_mid)
            f_grad.setColorAt(1.0, c_tip)

            painter.setBrush(f_grad)
            painter.drawPath(f_path)
        painter.restore()

    # Timer Hands-Free (se bloccato)
    if is_locked and is_active:
        elapsed = int(time.time() - lock_start_time)
        time_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        painter.setPen(QColor("#F8FAFC"))
        painter.drawText(QRectF(cx - 18, cy - 7, 36, 14), Qt.AlignmentFlag.AlignCenter, time_str)

    # Caricamento Whisper -> Qwen: i 3 fuochi diventano 3 fiammelle/comete che si rincorrono
    if spinner_opacity > 0.02:
        painter.save()
        painter.setOpacity(spinner_opacity)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        painter.setPen(Qt.PenStyle.NoPen)

        ring_r = core_radius * 0.78
        speed_mult = 1.25 if state == "QWEN" else 1.0
        base_rad = math.radians(spinner_angle * speed_mult)
        comet_colors = (c_p1, c_p2, c_p3)
        trail_steps = 9

        for idx, com_col in enumerate(comet_colors):
            chase_offset = 0.34 * math.sin(base_rad * 1.35 + idx * 2.1)
            head_ang = base_rad + idx * (2.0 * math.pi / 3.0) + chase_offset

            for s in range(trail_steps, -1, -1):
                t_frac = 1.0 - (s / trail_steps)
                ang = head_ang - (s * 0.11)
                px = cx + ring_r * math.cos(ang)
                py = cy + ring_r * math.sin(ang)
                rad = 1.2 + 2.8 * (t_frac ** 1.35)
                alpha = int(215 * (t_frac ** 1.6))

                g = QRadialGradient(px, py, rad * 1.55)
                if s == 0:
                    g.setColorAt(0.0, QColor(255, 255, 255, 235))
                    g.setColorAt(0.45, QColor(com_col.red(), com_col.green(), com_col.blue(), 220))
                    g.setColorAt(1.0, QColor(com_col.red(), com_col.green(), com_col.blue(), 0))
                else:
                    g.setColorAt(0.0, QColor(com_col.red(), com_col.green(), com_col.blue(), alpha))
                    g.setColorAt(1.0, QColor(com_col.red(), com_col.green(), com_col.blue(), 0))
                painter.setBrush(g)
                painter.drawEllipse(QPointF(px, py), rad * 1.55, rad * 1.55)

        painter.restore()

    painter.restore()


class WhisOrbBubble(QWidget):
    """
    Pillola dinamica auto-espandibile per le risposte di Whis posizionata sopra il Voice Orb.
    - Zero furto di focus (WindowDoesNotAcceptFocus | Tool | WindowStaysOnTopHint | FramelessWindowHint).
    - Design Obsidian Glass (#0E1118, bordo ciano celestiale #38BDF8 a 60 FPS).
    - Si espande morbidamente in altezza/larghezza a seconda del testo.
    - Click-to-copy, hover-to-pause e tasto ✕ rapido.
    """

    def __init__(self, orb_overlay: "VoiceOrbOverlay"):
        super().__init__()
        self.orb = orb_overlay
        self._full_text = ""

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._build_ui()

        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self.fade_out)

        self.anim_opacity = None

    def _build_ui(self):
        self.card = QFrame(self)
        self.card.setObjectName("WhisOrbCard")
        self.card.setStyleSheet("""
            QFrame#WhisOrbCard {
                background-color: rgba(14, 17, 24, 0.96);
                border: 1.5px solid rgba(56, 189, 248, 0.60);
                border-radius: 12px;
            }
        """)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(12, 9, 12, 10)
        card_layout.setSpacing(5)

        # Header row
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        self.title_label = QLabel("✨ Whis")
        self.title_label.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: bold;")
        header.addWidget(self.title_label)

        self.context_badge = QLabel("📅 Calendar")
        self.context_badge.setStyleSheet("""
            background-color: rgba(56, 189, 248, 0.18);
            color: #7DD3FC;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 4px;
            padding: 1px 5px;
            font-size: 9px;
            font-weight: 600;
        """)
        self.context_badge.hide()
        header.addWidget(self.context_badge)

        self.copy_feedback = QLabel("")
        self.copy_feedback.setStyleSheet("color: #10B981; font-size: 10px; font-weight: 600;")
        header.addWidget(self.copy_feedback)

        header.addStretch()

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748B;
                border: none;
                font-size: 11px;
                font-weight: bold;
                border-radius: 9px;
            }
            QPushButton:hover {
                background-color: rgba(244, 63, 94, 0.2);
                color: #F43F5E;
            }
        """)
        self.close_btn.clicked.connect(self.fade_out)
        header.addWidget(self.close_btn)

        card_layout.addLayout(header)

        # Body label
        self.body_label = QLabel()
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.body_label.setStyleSheet("""
            color: #F1F5F9;
            font-family: 'Segoe UI', system-ui, sans-serif;
            font-size: 11px;
            line-height: 1.4;
        """)
        card_layout.addWidget(self.body_label)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.card)

    def show_response(self, text: str, metadata: Optional[dict] = None, duration_s: float = 8.5):
        clean_text = (text or "").strip()
        if not clean_text:
            return
        self._full_text = clean_text
        self.body_label.setText(clean_text)
        self.copy_feedback.setText("")

        meta = metadata or {}
        if meta.get("calendar"):
            self.context_badge.setText("📅 Google Calendar")
            self.context_badge.show()
            self.card.setStyleSheet("""
                QFrame#WhisOrbCard {
                    background-color: rgba(14, 17, 24, 0.96);
                    border: 1.5px solid rgba(56, 189, 248, 0.70);
                    border-radius: 12px;
                }
            """)
        elif meta.get("note_action"):
            self.context_badge.setText("📝 Appunti")
            self.context_badge.show()
            self.card.setStyleSheet("""
                QFrame#WhisOrbCard {
                    background-color: rgba(14, 17, 24, 0.96);
                    border: 1.5px solid rgba(16, 185, 129, 0.70);
                    border-radius: 12px;
                }
            """)
        else:
            self.context_badge.hide()
            self.card.setStyleSheet("""
                QFrame#WhisOrbCard {
                    background-color: rgba(14, 17, 24, 0.96);
                    border: 1.5px solid rgba(56, 189, 248, 0.55);
                    border-radius: 12px;
                }
            """)

        font = self.body_label.font()
        fm = QFontMetrics(font)

        lines = clean_text.splitlines()
        if len(clean_text) < 45 and len(lines) <= 1:
            target_w = max(240, fm.horizontalAdvance(clean_text) + 40)
        elif len(clean_text) < 90 and len(lines) <= 2:
            target_w = 340
        else:
            target_w = 420

        avail_w = target_w - 24
        rect = fm.boundingRect(0, 0, avail_w, 2000, int(Qt.TextFlag.TextWordWrap), clean_text)
        text_h = rect.height()
        target_h = max(58, text_h + 46)
        target_h = min(280, target_h)

        pos_x = int(self.orb.anchor_x - target_w // 2)
        orb_top_y = int(self.orb.anchor_y - (self.orb.ACTIVE_H // 2))
        pos_y = int(orb_top_y - target_h - 10)

        screen = QApplication.primaryScreen().availableGeometry()
        pos_x = max(screen.x() + 10, min(pos_x, screen.x() + screen.width() - target_w - 10))
        pos_y = max(screen.y() + 10, min(pos_y, screen.y() + screen.height() - target_h - 10))

        self.setGeometry(pos_x, pos_y, target_w, target_h)

        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()

        self.anim_opacity = QPropertyAnimation(self, b"windowOpacity")
        self.anim_opacity.setDuration(160)
        self.anim_opacity.setStartValue(0.0)
        self.anim_opacity.setEndValue(1.0)
        self.anim_opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim_opacity.start()

        auto_hide_ms = int(max(duration_s, len(clean_text) * 0.05 + 4.0) * 1000)
        self.dismiss_timer.start(auto_hide_ms)

    def fade_out(self):
        self.dismiss_timer.stop()
        if not self.isVisible():
            return
        self.anim_opacity = QPropertyAnimation(self, b"windowOpacity")
        self.anim_opacity.setDuration(200)
        self.anim_opacity.setStartValue(self.windowOpacity())
        self.anim_opacity.setEndValue(0.0)
        self.anim_opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        self.anim_opacity.finished.connect(self.hide)
        self.anim_opacity.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._full_text:
                pyperclip.copy(self._full_text)
                self.copy_feedback.setText("Copiato! ✓")
                QTimer.singleShot(1800, lambda: self.copy_feedback.setText("") if hasattr(self, "copy_feedback") else None)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self.dismiss_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.dismiss_timer.start(3500)
        super().leaveEvent(event)


class OrbSignalBridge(QObject):
    change_state = pyqtSignal(str)
    feed_audio_signal = pyqtSignal(object)
    set_locked_signal = pyqtSignal(bool)
    set_badge_signal = pyqtSignal(str)
    show_toast_signal = pyqtSignal(str, float)
    show_whis_response_signal = pyqtSignal(str, dict, float)
    set_whis_state_signal = pyqtSignal(str)
    dismiss_whis_signal = pyqtSignal()
    startup_splash_signal = pyqtSignal()
    launch_target_signal = pyqtSignal(int, int)


class VoiceOrbOverlay(QWidget):
    IDLE_W = 60
    IDLE_H = 22
    ACTIVE_W = 104
    ACTIVE_H = 104
    ORB_CX = 52.0
    CORE_RADIUS = 17.0
    BADGE_X = 76.0

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
        self._badge_show_until = 0.0

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
        self._orb_scale = 0.0
        self._shimmer_phase = 0.0
        self._plasma_phase = 0.0
        self._startup_active = False
        self._startup_t = 0.0
        self._startup_y_offset = 0.0
        self._launch_active = False
        self._launch_t = 0.0
        self._launch_start = QPointF(0.0, 0.0)
        self._launch_end = QPointF(0.0, 0.0)
        self._target_w = float(self.ACTIVE_W)
        self._anim_w = float(self.ACTIVE_W)
        self._svg_cache: dict[str, QSvgRenderer] = {}

        # Ghost Mode (dopo 2 minuti di inattività)
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0

        # Ancoraggio e trascinamento
        self.anchor_x = 0
        self.anchor_y = 0
        self._user_dragged = False
        self._drag_start = None
        self._drag_moved = False
        self._press_time = 0.0

        # Signal bridge per chiamate esterne thread-safe
        self._in_app_orb_provider: Optional[Callable[[], Optional[Any]]] = None
        self._routed_to_in_app: bool = False
        self._active_in_app_orb: Optional[Any] = None

        self.bridge = OrbSignalBridge()
        self.bridge.change_state.connect(self._set_state_slot)
        self.bridge.feed_audio_signal.connect(self._feed_audio_slot)
        self.bridge.set_locked_signal.connect(self._set_locked_slot)
        self.bridge.set_badge_signal.connect(self._set_badge_slot)
        self.bridge.show_toast_signal.connect(self._show_toast_slot)
        self.bridge.show_whis_response_signal.connect(self._show_whis_response_slot)
        self.bridge.set_whis_state_signal.connect(self._set_whis_state_slot)
        self.bridge.dismiss_whis_signal.connect(self._dismiss_whis_slot)
        self.bridge.startup_splash_signal.connect(self._play_startup_splash_slot)
        self.bridge.launch_target_signal.connect(self._launch_to_target_slot)

        self._setup_window()
        self.whis_bubble = WhisOrbBubble(self)

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
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setToolTip("FlowLocal (Tieni premuto Shift Destro o clicca per parlare)")

        self._update_anchor_for_active_screen(force=True)

        self._apply_geometry_for_state("IDLE", immediate=True)
        self.hide()

    def _update_anchor_for_active_screen(self, force: bool = False):
        """Posiziona l'ancora sullo schermo attivo (dove si trova il cursore) se non trascinato manualmente."""
        if self._user_dragged and not force:
            return
        screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        self.anchor_x = avail.x() + avail.width() // 2
        self.anchor_y = avail.y() + avail.height() - 48

    def _get_svg_renderer(self, color_hex: str) -> QSvgRenderer:
        """Restituisce un QSvgRenderer dalla cache per evitare allocazioni SVG a 60 FPS."""
        renderer = self._svg_cache.get(color_hex)
        if renderer is None:
            svg_data = MIC_SVG.format(color=color_hex).encode("utf-8")
            renderer = QSvgRenderer(svg_data)
            self._svg_cache[color_hex] = renderer
        return renderer

    def _compute_target_width(self, state: str) -> tuple[int, int]:
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)

        if state == "IDLE" and not is_toast_active:
            return self.IDLE_W, self.IDLE_H

        if is_toast_active:
            f_app = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            fm_app = QFontMetrics(f_app)
            text_w = fm_app.horizontalAdvance(self.toast_message)
            badge_w = text_w + 26
            return max(self.ACTIVE_W, int(self.BADGE_X + badge_w + 10)), self.ACTIVE_H

        if self.badge_text:
            f_app = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            f_mode = QFont("Segoe UI", 8, QFont.Weight.Normal)
            fm_app = QFontMetrics(f_app)
            fm_mode = QFontMetrics(f_mode)
            if " • " in self.badge_text:
                p1, p2 = self.badge_text.split(" • ", 1)
                text_w = fm_app.horizontalAdvance(p1) + fm_app.horizontalAdvance(" • ") + fm_mode.horizontalAdvance(p2)
            else:
                text_w = fm_app.horizontalAdvance(self.badge_text)
            return max(self.ACTIVE_W, int(text_w + 24)), self.ACTIVE_H

        return self.ACTIVE_W, self.ACTIVE_H

    def _apply_geometry_for_state(self, state: str, immediate: bool = False):
        target_w, h = self._compute_target_width(state)
        self._target_w = float(target_w)
        if immediate or (state == "IDLE" and not self._startup_active and not (self.toast_message and time.time() < self.toast_end_time)):
            self._anim_w = float(target_w)
            w = target_w
        else:
            w = int(round(self._anim_w))

        y_off = int(round(self._startup_y_offset)) if self._startup_active else 0
        self.setGeometry(
            int(self.anchor_x - w / 2),
            int(self.anchor_y + y_off - h / 2),
            w,
            h
        )

    def set_in_app_orb_provider(self, provider: Optional[Callable[[], Optional[Any]]]):
        """Collega un provider che restituisce l'Orb in-app quando FlowLocal è la finestra attiva."""
        self._in_app_orb_provider = provider

    def _resolve_in_app_orb(self, prefer_sticky: bool = True) -> Optional[Any]:
        if prefer_sticky and self._routed_to_in_app and self._active_in_app_orb is not None:
            try:
                p = self._active_in_app_orb.parentWidget()
                if p is not None and p.isVisible():
                    return self._active_in_app_orb
            except Exception:
                pass
        if self._in_app_orb_provider is None:
            return None
        try:
            return self._in_app_orb_provider()
        except Exception:
            return None

    def set_badge_text(self, text: str):
        self.bridge.set_badge_signal.emit(text)

    def _set_badge_slot(self, text: str):
        self.badge_text = text
        self._badge_show_until = (time.time() + 1.6) if text else 0.0
        in_app = self._resolve_in_app_orb(prefer_sticky=True)
        if self._routed_to_in_app and in_app is not None:
            in_app.set_badge_text(text)
            return
        if in_app is not None and self.state == "IDLE":
            in_app.set_badge_text(text)
            return
        if self.state != "IDLE" or (self.toast_message and time.time() < self.toast_end_time):
            self._apply_geometry_for_state(self.state)
        self.update()

    def show_toast(self, message: str, duration_s: float = 2.5):
        self.bridge.show_toast_signal.emit(message, duration_s)

    def _show_toast_slot(self, message: str, duration_s: float):
        in_app = self._resolve_in_app_orb(prefer_sticky=True)
        if in_app is not None:
            self.last_activity_time = time.time()
            self.hide()
            in_app.show_toast(message, duration_s)
            return
        if self.state == "IDLE":
            self._update_anchor_for_active_screen()
        self.toast_message = message
        self.toast_end_time = time.time() + duration_s
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0
        self._apply_geometry_for_state(self.state)
        self.show()
        self.update()

    def play_startup_splash(self):
        """Avvia l'animazione di apertura: l'Orb salta da sotto lo schermo con uno schizzo fluido e sprofonda."""
        self.bridge.startup_splash_signal.emit()

    def _play_startup_splash_slot(self):
        if self.state != "IDLE":
            return
        self._update_anchor_for_active_screen()
        self._startup_active = True
        self._startup_t = 0.0
        self._startup_y_offset = 76.0
        self._orb_scale = 0.85
        self.wave_envelope = 0.3
        self.current_color = QColor("#38BDF8")
        self._anim_w = float(self.ACTIVE_W)
        self._target_w = float(self.ACTIVE_W)
        self.setGeometry(
            int(self.anchor_x - self.ACTIVE_W / 2),
            int(self.anchor_y + self._startup_y_offset - self.ACTIVE_H / 2),
            self.ACTIVE_W,
            self.ACTIVE_H,
        )
        self.show()
        self.update()

    def launch_to_target(self, tx: int, ty: int):
        """Fa volare i 3 fuochi verso il punto (tx, ty) dello schermo come se sparassero il testo."""
        self.bridge.launch_target_signal.emit(int(tx), int(ty))

    def _launch_to_target_slot(self, tx: int, ty: int):
        # Converte coordinate fisiche Win32 in coordinate logiche Qt (DIP)
        dpr = max(1.0, float(self.devicePixelRatioF()))
        screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen:
            dpr = max(1.0, float(screen.devicePixelRatio()))

        in_app = self._resolve_in_app_orb(prefer_sticky=True)
        if in_app is not None:
            c = in_app.global_orb_center()
            sx, sy = float(c.x()), float(c.y())
            in_app.set_state("IDLE")
            self._routed_to_in_app = False
            self._active_in_app_orb = None
        else:
            sx = float(self.anchor_x)
            sy = float(self.anchor_y - (self.ACTIVE_H / 2.0) + 44.0)
        ex = float(tx) / dpr
        ey = float(ty) / dpr

        margin = 56
        min_x = int(min(sx, ex) - margin)
        min_y = int(min(sy, ey) - margin)
        max_x = int(max(sx, ex) + margin)
        max_y = int(max(sy, ey) + margin)
        w = max(80, max_x - min_x)
        h = max(80, max_y - min_y)

        self.state = "IDLE"
        self.badge_text = ""
        self.badge_opacity = 0.0
        self.spinner_opacity = 0.0
        self.wave_envelope = 0.0
        self._launch_active = True
        self._launch_t = 0.0
        self._launch_start = QPointF(sx - min_x, sy - min_y)
        self._launch_end = QPointF(ex - min_x, ey - min_y)

        self.setGeometry(min_x, min_y, w, h)
        self.show()
        self.update()

    def set_state(self, new_state: str):
        self.bridge.change_state.emit(new_state)

    def _set_state_slot(self, new_state: str):
        was_idle = (self.state == "IDLE")
        if new_state != "IDLE":
            if self._startup_active:
                self._startup_active = False
                self._startup_y_offset = 0.0
            if self._launch_active:
                self._launch_active = False
                self._launch_t = 0.0
        self.state = new_state
        self.last_activity_time = time.time()
        self.ghost_opacity = 1.0

        if new_state != "IDLE":
            in_app = self._resolve_in_app_orb(prefer_sticky=not was_idle)
            if in_app is not None:
                self._routed_to_in_app = True
                self._active_in_app_orb = in_app
                self.hide()
                if self.badge_text:
                    in_app.set_badge_text(self.badge_text)
                in_app.set_state(new_state)
                return
        elif self._routed_to_in_app:
            in_app = self._active_in_app_orb
            self._routed_to_in_app = False
            self._active_in_app_orb = None
            self.hide()
            if in_app is not None:
                in_app.set_state("IDLE")
            return

        if new_state == "IDLE":
            is_toast = bool(self.toast_message and time.time() < self.toast_end_time)
            if not is_toast:
                self.hide()
            else:
                self._apply_geometry_for_state("IDLE")
                self.show()
        else:
            if was_idle:
                self._update_anchor_for_active_screen()
                self._orb_scale = 0.78
            self._apply_geometry_for_state(new_state)
            self.show()

        self.update()

    def set_locked(self, locked: bool):
        self.bridge.set_locked_signal.emit(locked)

    def _set_locked_slot(self, locked: bool):
        self.is_locked = locked
        if locked:
            self.lock_start_time = time.time()
        if self._routed_to_in_app and self._active_in_app_orb is not None:
            self._active_in_app_orb.set_locked(locked)
            return
        self.update()

    def feed_audio(self, audio_chunk: np.ndarray):
        self.bridge.feed_audio_signal.emit(audio_chunk)

    def _feed_audio_slot(self, audio_chunk: np.ndarray):
        if self._routed_to_in_app and self._active_in_app_orb is not None:
            self._active_in_app_orb.feed_audio(audio_chunk)
            return
        if self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE"):
            self.analyzer.process_chunk(audio_chunk)

    def show_listening(self):
        if hasattr(self, "whis_bubble") and self.whis_bubble:
            self.whis_bubble.fade_out()
        self.set_state("LISTENING")

    def show_whisper(self):
        self.set_state("WHISPER")

    def show_qwen(self):
        self.set_state("QWEN")

    def hide_orb(self):
        self.set_state("IDLE")

    def show_whis_response(self, text: str, metadata: Optional[dict] = None, duration_s: float = 8.5):
        """Mostra la pillola di risposta auto-espandibile di Whis sopra il Voice Orb."""
        self.bridge.show_whis_response_signal.emit(text, metadata or {}, duration_s)

    def _show_whis_response_slot(self, text: str, metadata: dict, duration_s: float):
        if hasattr(self, "whis_bubble"):
            self.whis_bubble.show_response(text, metadata, duration_s)
        self.set_badge_text("")
        self.set_state("IDLE")

    def set_whis_state(self, state: str):
        """Imposta lo stato di Whis nel Voice Orb ('LISTENING', 'THINKING', 'IDLE')."""
        self.bridge.set_whis_state_signal.emit(state)

    def _set_whis_state_slot(self, state: str):
        if state == "LISTENING":
            if hasattr(self, "whis_bubble"):
                self.whis_bubble.fade_out()
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

    def dismiss_whis(self):
        """Chiude immediatamente la pillola di risposta di Whis."""
        self.bridge.dismiss_whis_signal.emit()

    def _dismiss_whis_slot(self):
        if hasattr(self, "whis_bubble"):
            self.whis_bubble.fade_out()
        self.set_badge_text("")
        if self.state in ("WHISPER", "QWEN"):
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
        self._shimmer_phase = (self._shimmer_phase + 0.032) % 1.0

        if self._routed_to_in_app:
            if self.isVisible():
                self.hide()
            return

        # Avanzamento animazione di volo verso il campo di input
        if self._launch_active:
            self._launch_t += 0.052  # ~320ms a 60 FPS
            if self._launch_t >= 1.0:
                self._launch_active = False
                self._launch_t = 0.0
                if self.state == "IDLE":
                    self.hide()
                return
            self.update()
            return

        # Avanzamento animazione di avvio (salto da sotto lo schermo + schizzo fluido + rientro)
        if self._startup_active:
            self._startup_t += 0.014  # ~1.15s a 60 FPS
            t = self._startup_t
            if t >= 1.0:
                self._startup_active = False
                self._startup_t = 0.0
                self._startup_y_offset = 0.0
                self.wave_envelope = 0.0
                if self.state == "IDLE":
                    self.hide()
                return
            # Curva verticale: parte a +76px (sotto), salta fino a -24px, sprofonda a +76px
            jump_curve = math.sin(math.pi * t)
            self._startup_y_offset = 76.0 - 100.0 * (jump_curve ** 0.78)
            self.wave_envelope = math.sin(math.pi * min(1.0, t * 1.15))
            self._orb_scale = 0.85 + 0.15 * jump_curve
            self._plasma_phase += 0.12
            self.setGeometry(
                int(self.anchor_x - self.ACTIVE_W / 2),
                int(self.anchor_y + self._startup_y_offset - self.ACTIVE_H / 2),
                self.ACTIVE_W,
                self.ACTIVE_H,
            )
            self.update()
            return

        is_listening = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        is_processing = self.state in ("WHISPER", "QWEN")
        is_toast_active = bool(self.toast_message and time.time() < self.toast_end_time)
        is_visible_active = (self.state != "IDLE") or is_toast_active

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
        target_scale = 1.0 if is_visible_active else 0.78

        self.wave_envelope += (target_wave - self.wave_envelope) * 0.22
        self.spinner_opacity += (target_spin - self.spinner_opacity) * 0.22
        self.badge_opacity += (target_badge - self.badge_opacity) * 0.22
        self.toast_opacity += (target_toast - self.toast_opacity) * 0.22
        self._orb_scale += (target_scale - self._orb_scale) * 0.24

        # Movimento fluido del plasma: lento in attesa, bloccato quando parli per enfatizzare le frequenze vocali
        cur_energy = float(max(np.mean(self.analyzer.current_levels), np.mean(self.analyzer.target_levels))) if is_listening else 0.0
        if is_listening:
            if cur_energy < 0.035:
                self._plasma_phase += 0.025
        else:
            self._plasma_phase += 0.045

        if not is_toast_active and self.toast_opacity < 0.03 and self.toast_message:
            self.toast_message = ""
            self._apply_geometry_for_state(self.state)

        # Interpolazione fluida della larghezza della pillola (niente scatti laterali)
        if is_visible_active and abs(self._target_w - self._anim_w) >= 0.5:
            self._anim_w += (self._target_w - self._anim_w) * 0.25
            if abs(self._target_w - self._anim_w) < 0.5:
                self._anim_w = self._target_w
            w = int(round(self._anim_w))
            h = self.ACTIVE_H
            self.setGeometry(
                int(self.anchor_x - w / 2),
                int(self.anchor_y - h / 2),
                w,
                h,
            )

        # Modalità Fantasma: in stato IDLE senza toast attivi, l'overlay resta completamente nascosto
        if self.state == "IDLE" and not is_toast_active and self.isVisible():
            self.hide()

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
                self._user_dragged = True
                self.anchor_x += delta.x()
                self.anchor_y += delta.y()
                self._apply_geometry_for_state(self.state, immediate=True)
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
        # 0. VOLO DEI 3 FUOCHI VERSO IL CAMPO DI INPUT ("spara il testo")
        # =========================================================
        if self._launch_active:
            c_p1 = QColor(self.current_color)
            c_p2 = QColor("#A855F7")
            c_p3 = QColor("#10B981")

            sx, sy = self._launch_start.x(), self._launch_start.y()
            ex, ey = self._launch_end.x(), self._launch_end.y()
            # Punto di controllo Bézier leggermente curvo verso l'alto/lato
            ctrl_x = (sx + ex) * 0.5 + (ey - sy) * 0.14
            ctrl_y = min(sy, ey) - 35.0

            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            painter.setPen(Qt.PenStyle.NoPen)

            for idx, f_col in enumerate((c_p1, c_p2, c_p3)):
                t_i = max(0.0, min(1.0, self._launch_t * 1.18 - idx * 0.07))
                if t_i <= 0.01:
                    continue

                # Scia di 6 punti lungo la curva dietro la testa
                for s in range(6, -1, -1):
                    ts = max(0.0, t_i - s * 0.032)
                    u = 1.0 - ts
                    px = (u * u) * sx + 2 * u * ts * ctrl_x + (ts * ts) * ex
                    py = (u * u) * sy + 2 * u * ts * ctrl_y + (ts * ts) * ey

                    # Prospettiva: entra nello schermo restringendosi verso la fine
                    depth_scale = max(0.22, 1.0 - 0.78 * (ts ** 1.5))
                    tail_frac = 1.0 - (s / 6.0)
                    rad = (2.2 + 5.0 * tail_frac) * depth_scale
                    alpha = int(225 * (tail_frac ** 1.4) * (1.0 - 0.55 * max(0.0, ts - 0.75) / 0.25))

                    g = QRadialGradient(px, py, rad * 1.7)
                    if s == 0:
                        g.setColorAt(0.0, QColor(255, 255, 255, max(0, min(255, alpha))))
                        g.setColorAt(0.45, QColor(f_col.red(), f_col.green(), f_col.blue(), max(0, min(255, alpha))))
                        g.setColorAt(1.0, QColor(f_col.red(), f_col.green(), f_col.blue(), 0))
                    else:
                        g.setColorAt(0.0, QColor(f_col.red(), f_col.green(), f_col.blue(), max(0, min(255, alpha))))
                        g.setColorAt(1.0, QColor(f_col.red(), f_col.green(), f_col.blue(), 0))
                    painter.setBrush(g)
                    painter.drawEllipse(QPointF(px, py), rad * 1.7, rad * 1.7)

            # Micro-flash d'impatto quando i fuochi entrano nel campo di input
            if self._launch_t > 0.72:
                imp = (self._launch_t - 0.72) / 0.28
                imp_r = 6.0 + 18.0 * imp
                imp_a = int(190 * (1.0 - imp))
                if imp_a > 4:
                    ig = QRadialGradient(ex, ey, imp_r)
                    ig.setColorAt(0.0, QColor(255, 255, 255, imp_a))
                    ig.setColorAt(0.45, QColor(c_p1.red(), c_p1.green(), c_p1.blue(), int(imp_a * 0.75)))
                    ig.setColorAt(1.0, QColor(c_p1.red(), c_p1.green(), c_p1.blue(), 0))
                    painter.setBrush(ig)
                    painter.drawEllipse(QPointF(ex, ey), imp_r, imp_r)

            painter.restore()
            return

        # =========================================================
        # 1. STATO IDLE PURO: BOTTONE SOTTILE MINIMALE (60x22 px)
        # =========================================================
        if self.state == "IDLE" and not is_toast_active and not self._startup_active:
            btn_w = float(self.width() - 2)
            btn_h = float(self.height() - 2)
            btn_rect = QRectF(1.0, 1.0, btn_w, btn_h)
            radius = btn_h / 2.0

            # Sfondo Obsidian Glass scuro
            painter.setBrush(QColor(18, 19, 26, 240))
            breath_alpha = int(70 + 45 * math.sin(self.idle_breath))
            painter.setPen(QPen(QColor(56, 189, 248, breath_alpha), 1.2))
            painter.drawRoundedRect(btn_rect, radius, radius)

            # Icona microfono vettoriale centrata (Lucide SVG in cache)
            icon_color = "#38BDF8" if breath_alpha > 90 else "#94A3B8"
            svg_renderer = self._get_svg_renderer(icon_color)
            icon_size = 13.0
            svg_renderer.render(
                painter,
                QRectF(cx - icon_size / 2.0, cy - icon_size / 2.0, icon_size, icon_size)
            )
            return

        # =========================================================
        # 2. STATI ATTIVI, TOAST O STARTUP SPLASH: VOICE ORB
        # =========================================================
        if is_toast_active:
            cx = self.ORB_CX
            cy = self.height() / 2.0
        else:
            cx = self.width() / 2.0
            cy = 44.0

        is_active = self.state in ("LISTENING", "COMMAND_MODE", "STUDY_MODE")
        levels = self.analyzer.update(active=is_active)
        if self._startup_active:
            avg_energy = 0.45 * math.sin(math.pi * self._startup_t)
            levels = np.array([
                0.25 + 0.25 * math.sin(self._plasma_phase * 1.4 + i * 0.55)
                for i in range(len(levels))
            ], dtype=np.float32)
        else:
            avg_energy = float(np.mean(levels)) if len(levels) > 0 else 0.0

        core_radius = self.CORE_RADIUS + (avg_energy * 2.0 if (is_active or self._startup_active) else 0.0)
        orb_scale = max(0.75, min(1.0, self._orb_scale if self._orb_scale > 0.01 else 1.0))

        # 1. Schizzo fluido di gocce colorate intorno all'Orb durante l'animazione di apertura
        if self._startup_active and self.wave_envelope > 0.01:
            painter.save()
            if orb_scale < 0.999:
                painter.translate(cx, cy)
                painter.scale(orb_scale, orb_scale)
                painter.translate(-cx, -cy)
            c_p1 = QColor(self.current_color)
            c_p2 = QColor("#A855F7") if self.state != "STUDY_MODE" else QColor("#38BDF8")
            c_p3 = QColor("#10B981") if self.state != "QWEN" else QColor("#38BDF8")
            splash_p = min(1.0, self._startup_t * 1.25)
            drop_alpha = int(210 * math.sin(math.pi * splash_p))
            if drop_alpha > 5:
                drop_colors = [c_p1, c_p2, c_p3, QColor("#38BDF8")]
                for d_idx in range(8):
                    ang = (2.0 * math.pi * d_idx / 8.0) + 0.35
                    dist = core_radius + 6.0 + (18.0 * (splash_p ** 0.7)) + (4.0 if d_idx % 2 == 0 else 0.0)
                    d_rad = max(1.4, (3.4 - 1.8 * splash_p) * (1.0 if d_idx % 2 == 0 else 0.75))
                    dx = cx + dist * math.cos(ang)
                    dy = cy + dist * math.sin(ang)
                    dc = QColor(drop_colors[d_idx % len(drop_colors)])
                    dc.setAlpha(drop_alpha)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(dc)
                    painter.drawEllipse(QPointF(dx, dy), d_rad, d_rad)
            painter.restore()

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
            is_active=is_active or self._startup_active,
            is_locked=self.is_locked,
            lock_start_time=self.lock_start_time,
            orb_scale=orb_scale,
            max_wave_height=20.0,
        )

        # =========================================================
        # 3. NOTIFICA TOAST DI APPRENDIMENTO (esce dall'Orb)
        # =========================================================
        if is_toast_active and self.toast_opacity > 0.01:
            painter.save()
            painter.setOpacity(self.toast_opacity)
            painter.setClipRect(QRectF(cx, 0.0, max(0.0, self.width() - cx), float(self.height())))

            badge_h = 22.0
            target_badge_x = self.BADGE_X
            badge_w = max(36.0, self.width() - target_badge_x - 10.0)
            slide_offset = (1.0 - self.toast_opacity) * (badge_w * 0.55 + (target_badge_x - cx))
            badge_x = target_badge_x - slide_offset
            badge_y = cy - (badge_h / 2.0)
            badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)
            radius = badge_h / 2.0

            # 1. Sfondo pillola Obsidian Glass con riflesso smeraldo
            painter.setBrush(QColor(14, 18, 22, 235))
            border_col = QColor("#10B981")
            border_col.setAlpha(int(140 * self.toast_opacity))
            painter.setPen(QPen(border_col, 1.2))
            painter.drawRoundedRect(badge_rect, radius, radius)

            # 2. Dot indicatore smeraldo brillante
            dot_radius = 2.6
            dot_cx = badge_x + 11.0
            dot_cy = cy
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#10B981"))
            painter.drawEllipse(QPointF(dot_cx, dot_cy), dot_radius, dot_radius)

            # 3. Testo toast
            text_x = dot_cx + 7.5
            avail_text_w = badge_rect.right() - text_x - 8.0

            f_bold = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            f_norm = QFont("Segoe UI", 8, QFont.Weight.Normal)
            fm_bold = QFontMetrics(f_bold)
            fm_norm = QFontMetrics(f_norm)

            if " → " in self.toast_message:
                left_part, right_part = self.toast_message.split(" → ", 1)
                arrow_str = " → "
                total_w = fm_bold.horizontalAdvance(left_part) + fm_norm.horizontalAdvance(arrow_str) + fm_bold.horizontalAdvance(right_part)
                start_x = text_x + max(0.0, (avail_text_w - total_w) / 2.0)
                base_y = badge_y + (badge_h + fm_bold.ascent() - fm_bold.descent()) / 2.0

                painter.setFont(f_bold)
                painter.setPen(QColor("#34D399"))
                painter.drawText(QPointF(start_x, base_y), left_part)
                start_x += fm_bold.horizontalAdvance(left_part)

                painter.setFont(f_norm)
                painter.setPen(QColor("#64748B"))
                painter.drawText(QPointF(start_x, base_y), arrow_str)
                start_x += fm_norm.horizontalAdvance(arrow_str)

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
        # 4. MICRO-ETICHETTA FLUTTUANTE SOTTO L'ORB (senza box né bordo, svanisce dopo 1.6s)
        # =========================================================
        elif self.badge_text and self.badge_opacity > 0.01:
            painter.save()
            painter.setOpacity(self.badge_opacity)

            # Leggero movimento verso l'alto durante la comparsa (vicino all'Orb, sopra la taskbar)
            float_y = (1.0 - self.badge_opacity) * 3.0
            label_y = cy + self.CORE_RADIUS + 10.0 + float_y
            shadow_col = QColor(0, 0, 0, int(190 * self.badge_opacity))

            if " • " in self.badge_text:
                app_part, mode_part = self.badge_text.split(" • ", 1)
                f_app = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
                f_mode = QFont("Segoe UI", 8, QFont.Weight.Normal)
                fm_app = QFontMetrics(f_app)
                fm_mode = QFontMetrics(f_mode)
                bullet_str = " · "

                total_w = fm_app.horizontalAdvance(app_part) + fm_app.horizontalAdvance(bullet_str) + fm_mode.horizontalAdvance(mode_part)
                start_x = cx - (total_w / 2.0)
                base_y = label_y + fm_app.ascent()

                # App Label + ombra morbida
                painter.setFont(f_app)
                painter.setPen(shadow_col)
                painter.drawText(QPointF(start_x + 1.0, base_y + 1.0), app_part)
                painter.setPen(QColor("#F8FAFC"))
                painter.drawText(QPointF(start_x, base_y), app_part)
                start_x += fm_app.horizontalAdvance(app_part)

                # Separatore con colore plasma
                painter.setPen(shadow_col)
                painter.drawText(QPointF(start_x + 1.0, base_y + 1.0), bullet_str)
                painter.setPen(self.current_color)
                painter.drawText(QPointF(start_x, base_y), bullet_str)
                start_x += fm_app.horizontalAdvance(bullet_str)

                # Mode Label
                painter.setFont(f_mode)
                painter.setPen(shadow_col)
                painter.drawText(QPointF(start_x + 1.0, base_y + 1.0), mode_part)
                painter.setPen(QColor("#CBD5E1"))
                painter.drawText(QPointF(start_x, base_y), mode_part)
            else:
                f_single = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
                painter.setFont(f_single)
                text_rect = QRectF(0.0, label_y, float(self.width()), 18.0)
                painter.setPen(shadow_col)
                painter.drawText(text_rect.translated(1.0, 1.0), Qt.AlignmentFlag.AlignCenter, self.badge_text)
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
