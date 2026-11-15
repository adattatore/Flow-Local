"""Widget morfologici multi-tema, bottoni, card e transizioni animate per FlowLocal Hub."""

from datetime import datetime, date as dt_date, timedelta
from typing import Callable, Dict, List, Optional, Any
import math
import os
import random
import re
import tempfile
import threading
import time

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

from ui.themes import _is_dark_theme, _resolve_palette, _MORPHOLOGICAL_THEMES




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

