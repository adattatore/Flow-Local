"""
StartupWelcomeOverlay (PyQt6)
Usa il VERO VoiceOrbOverlay dell'app:
1. All'avvio mette l'Orb reale in stato "QWEN" (i 3 fuochi reali che girano in basso
   al centro, esattamente come dopo aver finito di ascoltare) per ~1 secondo.
2. Chiama orb.launch_to_target(tx, ty) verso il centro dello schermo: sono i veri
   3 fuochi dell'Orb a spararsi verso il centro.
3. All'impatto al centro si apre la card bianca con "Benvenuto" (primo avvio
   sulla macchina) o "Bentornato" (avvii successivi), come se fosse stato dettato.
"""

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QSettings
from PyQt6.QtGui import (
    QPainter,
    QColor,
    QPainterPath,
    QLinearGradient,
    QPen,
    QFont,
    QFontMetrics,
    QCursor,
    QGuiApplication,
)
from PyQt6.QtWidgets import QApplication, QWidget


def resolve_welcome_greeting(mark_launched: bool = True) -> tuple[str, bool]:
    """Determina 'Benvenuto' al primo avvio sulla macchina o 'Bentornato' agli avvii successivi."""
    settings = QSettings("FlowLocal", "App")
    raw_val = settings.value("has_launched_before", False)
    has_launched = (
        raw_val
        if isinstance(raw_val, bool)
        else str(raw_val).strip().lower() in ("1", "true", "yes")
    )
    is_first_launch = not has_launched

    if mark_launched:
        settings.setValue("has_launched_before", True)
        try:
            count = int(settings.value("launch_count", 0) or 0)
        except Exception:
            count = 0
        settings.setValue("launch_count", count + 1)
        settings.sync()

    greeting = "Benvenuto" if is_first_launch else "Bentornato"
    return greeting, is_first_launch


class StartupWelcomeOverlay(QWidget):
    """Card bianca centrata che riceve lo sparo del vero VoiceOrbOverlay e mostra Benvenuto/Bentornato."""

    WIDGET_W = 460
    WIDGET_H = 200
    CARD_W = 390
    CARD_H = 128

    REVEAL_DURATION = 0.40
    HOLD_DURATION = 1.15
    FADE_DURATION = 0.42

    def __init__(self, greeting: Optional[str] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        if greeting is None:
            greeting, _ = resolve_welcome_greeting(mark_launched=True)
        self._greeting = greeting

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.setFixedSize(self.WIDGET_W, self.WIDGET_H)
        self._center_on_active_screen()

        self._start_time = 0.0
        self._elapsed = 0.0
        self._fade_start = self.REVEAL_DURATION + self.HOLD_DURATION
        self._total_duration = self._fade_start + self.FADE_DURATION

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _center_on_active_screen(self, screen=None):
        if screen is None:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.WIDGET_W) // 2
        y = geo.y() + (geo.height() - self.WIDGET_H) // 2
        self.move(x, y)

    def start(self, screen=None):
        """Mostra la card bianca al centro dello schermo."""
        self._center_on_active_screen(screen=screen)
        self._start_time = time.perf_counter()
        self._elapsed = 0.0
        self.show()
        self.raise_()
        if not self._timer.isActive():
            self._timer.start(16)  # ~60 FPS

    def _tick(self):
        self._elapsed = time.perf_counter() - self._start_time
        if self._elapsed >= self._total_duration:
            self._timer.stop()
            self.close()
            return
        self.update()

    @staticmethod
    def _ease_out_cubic(x: float) -> float:
        x = max(0.0, min(1.0, x))
        return 1.0 - ((1.0 - x) ** 3)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        t = self._elapsed
        cx = self.WIDGET_W / 2.0
        cy = self.WIDGET_H / 2.0

        fade_out = 1.0
        if t > self._fade_start:
            fade_out = max(0.0, 1.0 - (t - self._fade_start) / self.FADE_DURATION)
        if fade_out <= 0.01:
            return
        painter.setOpacity(fade_out)

        reveal_p = min(1.0, t / self.REVEAL_DURATION)
        eased_p = self._ease_out_cubic(reveal_p)

        card_w = 56.0 + (self.CARD_W - 56.0) * eased_p
        card_h = 56.0 + (self.CARD_H - 56.0) * eased_p
        card_x = cx - card_w / 2.0
        card_y = cy - card_h / 2.0
        radius = 28.0 - 4.0 * eased_p
        card_rect = QRectF(card_x, card_y, card_w, card_h)

        # Ombra morbida multi-livello sotto la card bianca
        for s_idx in range(4, 0, -1):
            pad = s_idx * 3.5
            s_alpha = int(14 * (5 - s_idx) * eased_p)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(15, 23, 42, s_alpha))
            painter.drawRoundedRect(
                card_rect.adjusted(-pad, -pad + 2.0, pad, pad + 3.0),
                radius + pad * 0.4,
                radius + pad * 0.4,
            )

        # Card bianca pura (#FFFFFF) con bordo sfumato dei 3 fuochi
        painter.setBrush(QColor("#FFFFFF"))
        rim_grad = QLinearGradient(card_rect.left(), card_rect.top(), card_rect.right(), card_rect.bottom())
        rim_grad.setColorAt(0.0, QColor(56, 189, 248, int(140 * eased_p)))
        rim_grad.setColorAt(0.5, QColor(168, 85, 247, int(140 * eased_p)))
        rim_grad.setColorAt(1.0, QColor(16, 185, 129, int(140 * eased_p)))
        painter.setPen(QPen(rim_grad, 1.6))
        painter.drawRoundedRect(card_rect, radius, radius)

        # Testo "Benvenuto" / "Bentornato" rivelato come se fosse appena stato dettato
        text_p = max(0.0, min(1.0, (reveal_p - 0.14) / 0.78))
        if text_p > 0.01:
            painter.save()
            clip_path = QPainterPath()
            clip_path.addRoundedRect(card_rect.adjusted(2, 2, -2, -2), radius, radius)
            painter.setClipPath(clip_path)

            font = QFont("Segoe UI", 28, QFont.Weight.Bold)
            painter.setFont(font)
            fm = QFontMetrics(font)

            total_chars = len(self._greeting)
            visible_chars = max(1, int(math.ceil(total_chars * text_p)))
            shown_text = self._greeting[:visible_chars]

            full_w = fm.horizontalAdvance(self._greeting)
            shown_w = fm.horizontalAdvance(shown_text)
            text_x = cx - full_w / 2.0
            text_y = cy - 6.0 + fm.ascent() / 2.0 - fm.descent() / 2.0

            painter.setPen(QColor(15, 23, 42, int(255 * min(1.0, text_p * 2.2))))
            painter.drawText(QPointF(text_x, text_y), shown_text)

            # Cursore di dettatura luminoso alla fine della parola
            if t < 1.15:
                blink = 1.0 if text_p < 1.0 else (0.5 + 0.5 * math.sin(t * 14.0))
                cur_alpha = int(220 * blink * max(0.0, 1.0 - max(0.0, t - 0.85) / 0.30))
                if cur_alpha > 8:
                    cur_x = text_x + shown_w + 3.0
                    cur_top = cy - 22.0
                    cur_h = 30.0
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(56, 189, 248, cur_alpha))
                    painter.drawRoundedRect(QRectF(cur_x, cur_top, 2.5, cur_h), 1.2, 1.2)

            # Sottotitolo discreto sotto al saluto
            sub_p = max(0.0, min(1.0, (t - 0.28) / 0.25))
            if sub_p > 0.01:
                sub_font = QFont("Segoe UI", 9, QFont.Weight.Medium)
                painter.setFont(sub_font)
                painter.setPen(QColor(100, 116, 139, int(220 * sub_p)))
                painter.drawText(
                    QRectF(card_x + 12, cy + 18.0, card_w - 24, 24),
                    Qt.AlignmentFlag.AlignCenter,
                    "FlowLocal pronto · Shift Destro per dettare",
                )

            painter.restore()


_active_welcome_overlay: Optional[StartupWelcomeOverlay] = None


def show_startup_welcome(orb=None, greeting: Optional[str] = None):
    """Fa girare il vero VoiceOrbOverlay in stato QWEN per ~1s, poi spara i 3 fuochi al centro e mostra il saluto."""
    if greeting is None:
        greeting, _ = resolve_welcome_greeting(mark_launched=True)

    screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if orb is None:
        _open_welcome_card(greeting=greeting, screen=screen)
        return

    # Se l'utente sta già dettando, non interferire con l'Orb
    if getattr(orb, "state", "IDLE") != "IDLE":
        return

    # 1. Usa il VERO VoiceOrbOverlay in basso: stato QWEN (i 3 fuochi che girano come dopo l'ascolto)
    orb.set_badge_text("")
    orb.set_state("QWEN")

    def _shoot_to_center():
        if getattr(orb, "state", "IDLE") not in ("QWEN", "WHISPER"):
            return
        target_screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen() or screen
        if target_screen:
            geo = target_screen.availableGeometry()
            dpr = max(1.0, float(target_screen.devicePixelRatio()))
            tx = int((geo.x() + geo.width() / 2.0) * dpr)
            ty = int((geo.y() + geo.height() / 2.0) * dpr)
        else:
            tx, ty = 960, 540

        # 2. Il VERO Orb spara i suoi 3 fuochi verso il centro dello schermo
        orb.launch_to_target(tx, ty)

        # 3. Quando i fuochi impattano al centro (~230ms), appare la card bianca col saluto
        QTimer.singleShot(230, lambda: _open_welcome_card(greeting=greeting, screen=target_screen))

    QTimer.singleShot(1000, _shoot_to_center)


def _open_welcome_card(greeting: str, screen=None) -> StartupWelcomeOverlay:
    global _active_welcome_overlay
    overlay = StartupWelcomeOverlay(greeting=greeting)
    _active_welcome_overlay = overlay
    overlay.destroyed.connect(lambda: _clear_welcome_ref(overlay))
    overlay.start(screen=screen)
    return overlay


def _clear_welcome_ref(overlay: StartupWelcomeOverlay):
    global _active_welcome_overlay
    if _active_welcome_overlay is overlay:
        _active_welcome_overlay = None
