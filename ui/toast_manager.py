"""Sistema di Toast non bloccanti e Pannello Diagnostico per FlowLocal Hub (PyQt6)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.logging_config import APP_VERSION, export_diagnostics_file, get_log_file_path
from ui.icons import get_icon, apply_win11_titlebar
from ui.themes import _is_dark_theme, _resolve_palette

_LEVEL_ACCENTS = {
    "info": ("#38BDF8", "info"),
    "success": ("#10B981", "check"),
    "warning": ("#F59E0B", "warning"),
    "error": ("#F43F5E", "warning"),
}


class ToastCard(QFrame):
    """Singola notifica toast non bloccante con azione opzionale e pausa auto-dismiss su hover."""

    def __init__(
        self,
        parent: QWidget,
        message: str,
        title: str = "",
        level: str = "info",
        duration_ms: int = 5200,
        action_label: str = "",
        on_action: Optional[Callable[[], None]] = None,
        on_closed: Optional[Callable[["ToastCard"], None]] = None,
        theme_key: str = "obsidian",
        reading_mode: bool = False,
    ):
        super().__init__(parent)
        self._on_closed = on_closed
        self._on_action = on_action
        self._duration_ms = max(0, int(duration_ms))
        self._closing = False

        pal = _resolve_palette(theme_key, reading_mode=reading_mode)
        accent, icon_name = _LEVEL_ACCENTS.get(level, _LEVEL_ACCENTS["info"])
        is_dark = _is_dark_theme(theme_key)
        bg = pal["dialog_bg"] if is_dark else "#FFFFFF"
        border = pal["card_border"]
        text_col = pal["text"]
        title_col = pal["title"]
        sub_col = pal["subtitle"]

        self.setObjectName("FlowToastCard")
        self.setFixedWidth(360)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.setStyleSheet(
            f"QFrame#FlowToastCard {{"
            f"  background-color: {bg};"
            f"  border: 1px solid {border};"
            f"  border-left: 3px solid {accent};"
            f"  border-radius: 10px;"
            f"}}"
        )

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setStyleSheet("background: transparent; border: none;")
        icon_lbl.setPixmap(get_icon(icon_name, theme_key=theme_key, color=accent, size=18).pixmap(QSize(18, 18)))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(icon_lbl)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(4)

        if title:
            t_lbl = QLabel(title)
            t_lbl.setWordWrap(True)
            t_lbl.setStyleSheet(
                f"color: {title_col}; font-size: 12px; font-weight: 700; background: transparent; border: none;"
            )
            body.addWidget(t_lbl)

        m_lbl = QLabel(message)
        m_lbl.setWordWrap(True)
        m_lbl.setStyleSheet(
            f"color: {text_col if not title else sub_col}; font-size: 12px; background: transparent; border: none;"
        )
        body.addWidget(m_lbl)

        if action_label and on_action is not None:
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 4, 0, 0)
            action_btn = QPushButton(action_label)
            action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            action_btn.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: {pal['btn_bg']};"
                f"  color: {accent};"
                f"  border: 1px solid {accent};"
                f"  border-radius: 6px;"
                f"  padding: 4px 10px;"
                f"  font-size: 11px;"
                f"  font-weight: 600;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background-color: {pal['btn_hover_bg']};"
                f"}}"
            )
            action_btn.clicked.connect(self._handle_action)
            btn_row.addWidget(action_btn)
            btn_row.addStretch(1)
            body.addLayout(btn_row)

        outer.addLayout(body, 1)

        close_btn = QPushButton("")
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setIcon(get_icon("close", theme_key=theme_key, role="muted", size=12))
        close_btn.setIconSize(QSize(12, 12))
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 5px; }"
            f"QPushButton:hover {{ background-color: {pal['nav_hover']}; }}"
        )
        close_btn.clicked.connect(self.dismiss)
        outer.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        if self._duration_ms > 0:
            self._timer.start(self._duration_ms)

        self.adjustSize()

    def _handle_action(self):
        cb = self._on_action
        self.dismiss()
        if cb:
            cb()

    def enterEvent(self, event):
        if self._timer.isActive():
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._closing and self._duration_ms > 0:
            self._timer.start(max(1800, self._duration_ms // 2))
        super().leaveEvent(event)

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self.hide()
        if self._on_closed:
            self._on_closed(self)
        self.deleteLater()


class ToastManager:
    """Gestisce lo stack di notifiche toast ancorato in basso a destra dentro la finestra Hub."""

    MAX_VISIBLE = 3
    MARGIN_RIGHT = 20
    MARGIN_BOTTOM = 20
    SPACING = 10

    def __init__(self, host: QWidget):
        self._host = host
        self._toasts: List[ToastCard] = []

    def show_toast(
        self,
        message: str,
        title: str = "",
        level: str = "info",
        duration_ms: int = 5200,
        action_label: str = "",
        on_action: Optional[Callable[[], None]] = None,
    ) -> ToastCard:
        theme_key = getattr(self._host, "_current_theme", "obsidian")
        reading_mode = getattr(self._host, "_reading_mode", False)

        while len(self._toasts) >= self.MAX_VISIBLE:
            oldest = self._toasts.pop(0)
            oldest.dismiss()

        card = ToastCard(
            parent=self._host,
            message=message,
            title=title,
            level=level,
            duration_ms=duration_ms,
            action_label=action_label,
            on_action=on_action,
            on_closed=self._on_toast_closed,
            theme_key=theme_key,
            reading_mode=reading_mode,
        )
        self._toasts.append(card)
        card.show()
        card.raise_()
        self.reposition()
        return card

    def _on_toast_closed(self, card: ToastCard):
        if card in self._toasts:
            self._toasts.remove(card)
        self.reposition()

    def reposition(self):
        if not self._host or not self._toasts:
            return
        host_w = self._host.width()
        host_h = self._host.height()
        y_cursor = host_h - self.MARGIN_BOTTOM
        for card in reversed(self._toasts):
            card.adjustSize()
            cw = card.width()
            ch = card.sizeHint().height()
            card.resize(cw, ch)
            x = max(12, host_w - cw - self.MARGIN_RIGHT)
            y = max(60, y_cursor - ch)
            card.move(x, y)
            card.raise_()
            y_cursor = y - self.SPACING


class DiagnosticsDialog(QDialog):
    """Pannello diagnostico compatto con stato motori, sincronizzazione e export log."""

    def __init__(
        self,
        parent: QWidget,
        status_items: List[Dict[str, str]],
        diagnostics_extra: Dict[str, str],
        on_open_google_settings: Optional[Callable[[], None]] = None,
        theme_key: str = "obsidian",
        reading_mode: bool = False,
    ):
        super().__init__(parent)
        self._theme_key = theme_key
        self._reading_mode = reading_mode
        self._diagnostics_extra = diagnostics_extra
        self._on_open_google_settings = on_open_google_settings

        pal = _resolve_palette(theme_key, reading_mode=reading_mode)
        self.setWindowTitle(f"Diagnostica di Sistema — FlowLocal v{APP_VERSION}")
        self.setMinimumSize(540, 440)
        self.resize(580, 480)
        self.setStyleSheet(
            f"QDialog {{ background-color: {pal['dialog_bg']}; color: {pal['text']}; }}"
        )
        apply_win11_titlebar(self, theme_key, reading_mode=reading_mode)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_lbl = QLabel("Stato Sistema & Diagnostica")
        title_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 16px; font-weight: 700;")
        sub_lbl = QLabel(f"FlowLocal v{APP_VERSION} · Controllo rapido motori AI, audio e sincronizzazione")
        sub_lbl.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px;")
        sub_lbl.setWordWrap(True)
        title_box.addWidget(title_lbl)
        title_box.addWidget(sub_lbl)
        hdr.addLayout(title_box, 1)
        root.addLayout(hdr)

        # Area scorrevole con le card di stato
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body_w = QWidget()
        body_w.setStyleSheet("background: transparent;")
        body_l = QVBoxLayout(body_w)
        body_l.setContentsMargins(0, 2, 4, 2)
        body_l.setSpacing(8)

        for item in status_items:
            level = item.get("level", "info")
            accent, _ = _LEVEL_ACCENTS.get(level, _LEVEL_ACCENTS["info"])
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(
                f"QFrame {{"
                f"  background-color: {pal['card_bg']};"
                f"  border: 1px solid {pal['card_border']};"
                f"  border-left: 3px solid {accent};"
                f"  border-radius: 10px;"
                f"}}"
            )
            cl = QHBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(10)

            info_col = QVBoxLayout()
            info_col.setSpacing(2)
            k_lbl = QLabel(item.get("label", ""))
            k_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 600; border: none; background: transparent;")
            v_lbl = QLabel(item.get("detail", ""))
            v_lbl.setWordWrap(True)
            v_lbl.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px; border: none; background: transparent;")
            info_col.addWidget(k_lbl)
            info_col.addWidget(v_lbl)
            cl.addLayout(info_col, 1)

            badge = QLabel(item.get("status", "OK"))
            badge.setStyleSheet(
                f"color: {accent}; background-color: {pal['pill_bg']}; border: 1px solid {accent};"
                f"border-radius: 8px; padding: 3px 9px; font-size: 11px; font-weight: 600;"
            )
            cl.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

            if item.get("action") == "reconnect_google" and self._on_open_google_settings:
                fix_btn = QPushButton("Riconnetti")
                fix_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                fix_btn.setStyleSheet(
                    f"QPushButton {{ background-color: {pal['btn_bg']}; color: {accent}; border: 1px solid {accent};"
                    f" border-radius: 7px; padding: 4px 10px; font-size: 11px; font-weight: 600; }}"
                    f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
                )
                fix_btn.clicked.connect(self._reconnect_google)
                cl.addWidget(fix_btn, 0, Qt.AlignmentFlag.AlignVCenter)

            body_l.addWidget(card)

        body_l.addStretch(1)
        scroll.setWidget(body_w)
        root.addWidget(scroll, 1)

        # Footer azioni
        footer = QHBoxLayout()
        footer.setSpacing(8)

        open_logs_btn = QPushButton(" Apri cartella log")
        open_logs_btn.setIcon(get_icon("folder", theme_key=theme_key, role="text", size=14))
        open_logs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_logs_btn.setMinimumHeight(34)
        open_logs_btn.setStyleSheet(
            f"QPushButton {{ background-color: {pal['btn_bg']}; color: {pal['btn_text']};"
            f" border: 1px solid {pal['btn_border']}; border-radius: 8px; padding: 0 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
        )
        open_logs_btn.clicked.connect(self._open_log_folder)
        footer.addWidget(open_logs_btn)

        export_btn = QPushButton(" Esporta report (.txt)")
        export_btn.setIcon(get_icon("download", theme_key=theme_key, role="text", size=14))
        export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        export_btn.setMinimumHeight(34)
        export_btn.setStyleSheet(
            f"QPushButton {{ background-color: {pal['btn_bg']}; color: {pal['btn_text']};"
            f" border: 1px solid {pal['btn_border']}; border-radius: 8px; padding: 0 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
        )
        export_btn.clicked.connect(self._export_report)
        footer.addWidget(export_btn)

        footer.addStretch(1)

        close_btn = QPushButton("Chiudi")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setMinimumHeight(34)
        close_btn.setStyleSheet(
            f"QPushButton {{ background-color: {pal['btn_bg']}; color: {pal['title']};"
            f" border: 1px solid {pal['btn_border']}; border-radius: 8px; padding: 0 16px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
        )
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)

        root.addLayout(footer)

    def _reconnect_google(self):
        self.accept()
        if self._on_open_google_settings:
            self._on_open_google_settings()

    def _open_log_folder(self):
        folder = get_log_file_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            pass

    def _export_report(self):
        default_name = str(Path.home() / f"FlowLocal_Diagnostics_v{APP_VERSION}.txt")
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Esporta Report Diagnostico",
            default_name,
            "File di testo (*.txt)",
        )
        if not dest:
            return
        try:
            export_diagnostics_file(Path(dest), extra=self._diagnostics_extra)
            parent = self.parent()
            if parent is not None and hasattr(parent, "show_toast"):
                parent.show_toast(
                    f"Report salvato in {Path(dest).name}",
                    title="Diagnostica esportata",
                    level="success",
                )
        except Exception as exc:
            parent = self.parent()
            if parent is not None and hasattr(parent, "show_toast"):
                parent.show_toast(str(exc), title="Errore esportazione log", level="error")
