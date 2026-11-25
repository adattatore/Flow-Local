"""Floating contextual Whis Chat widget (FlowNotes & FlowTime)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pyperclip
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.whis_agent import WhisAgent, WhisStreamWorker
from ui.icons import get_icon


def _parse_color_safe(raw: str, fallback: str) -> QColor:
    s = (raw or "").strip()
    if s.startswith("rgba(") and s.endswith(")"):
        parts = [p.strip() for p in s[5:-1].split(",")]
        if len(parts) >= 4:
            try:
                a_raw = float(parts[3])
                alpha = int(a_raw * 255) if a_raw <= 1.0 else int(a_raw)
                return QColor(
                    int(float(parts[0])),
                    int(float(parts[1])),
                    int(float(parts[2])),
                    max(0, min(255, alpha)),
                )
            except Exception:
                pass
    c = QColor(s)
    return c if c.isValid() else QColor(fallback)


class _WhisHeaderDot(QWidget):
    """Piccolo indicatore luminoso Whis (ciano -> viola)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._c1 = QColor("#38BDF8")
        self._c2 = QColor("#A855F7")

    def set_colors(self, c1: QColor, c2: QColor):
        self._c1 = QColor(c1)
        self._c2 = QColor(c2)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = self.width() / 2.0
        cy = self.height() / 2.0

        glow = QRadialGradient(cx, cy, 8.0)
        c1_g = QColor(self._c1)
        c1_g.setAlpha(115)
        glow.setColorAt(0.0, c1_g)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(0.0, 0.0, 16.0, 16.0))

        core = QLinearGradient(3.0, 3.0, 13.0, 13.0)
        core.setColorAt(0.0, self._c1)
        core.setColorAt(1.0, self._c2)
        p.setBrush(core)
        p.drawEllipse(QRectF(3.5, 3.5, 9.0, 9.0))
        p.end()


class _WhisMessageBubble(QFrame):
    """Singola bolla conversazionale nella chat flottante Whis."""

    insertRequested = pyqtSignal(str)
    replaceRequested = pyqtSignal(str)

    def __init__(
        self,
        role: str,
        text: str = "",
        allow_insert: bool = False,
        allow_replace: bool = False,
        is_dark: bool = True,
        accent_hex: str = "#38BDF8",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.role = role
        self._text = text or ""
        self._allow_insert = bool(allow_insert)
        self._allow_replace = bool(allow_replace)
        self._is_dark = is_dark
        self._accent_hex = accent_hex or "#38BDF8"
        self._finalized = role == "user"
        self._model_name = ""

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("WhisBubbleCard")
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(10, 8, 10, 8)
        card_lay.setSpacing(6)

        self.body_lbl = QLabel(self._text, self.card)
        self.body_lbl.setWordWrap(True)
        self.body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card_lay.addWidget(self.body_lbl)

        self.actions_bar = QWidget(self.card)
        act_lay = QHBoxLayout(self.actions_bar)
        act_lay.setContentsMargins(0, 2, 0, 0)
        act_lay.setSpacing(6)

        self.model_lbl = QLabel("", self.actions_bar)
        act_lay.addWidget(self.model_lbl)
        act_lay.addStretch(1)

        self.insert_btn = QPushButton("Inserisci", self.actions_bar)
        self.insert_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.insert_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.insert_btn.setToolTip("Inserisci risposta nella nota al cursore")
        self.insert_btn.clicked.connect(lambda: self.insertRequested.emit(self._text))
        act_lay.addWidget(self.insert_btn)

        self.replace_btn = QPushButton("Sostituisci", self.actions_bar)
        self.replace_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.replace_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.replace_btn.setToolTip("Sostituisci il testo selezionato nella nota")
        self.replace_btn.clicked.connect(lambda: self.replaceRequested.emit(self._text))
        act_lay.addWidget(self.replace_btn)

        self.copy_btn = QPushButton("Copia", self.actions_bar)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_btn.setToolTip("Copia risposta negli appunti")
        self.copy_btn.clicked.connect(self._on_copy_clicked)
        act_lay.addWidget(self.copy_btn)

        self.actions_bar.setVisible(False)
        card_lay.addWidget(self.actions_bar)

        if role == "user":
            row.addStretch(1)
            self.card.setMaximumWidth(285)
            row.addWidget(self.card)
        else:
            row.addWidget(self.card, 1)

        outer.addLayout(row)
        self.apply_theme(is_dark, accent_hex)

    def append_chunk(self, delta: str):
        if not delta:
            return
        self._text += delta
        self.body_lbl.setText(self._text)

    def set_error(self, err_text: str):
        self._finalized = True
        self._text = err_text or "Errore durante la richiesta."
        self.body_lbl.setText(self._text)
        self.model_lbl.setText("Errore")
        self.insert_btn.setVisible(False)
        self.replace_btn.setVisible(False)
        self.copy_btn.setVisible(bool(self._text.strip()))
        self.actions_bar.setVisible(True)

    def finalize(self, full_text: str, model_name: str = ""):
        self._finalized = True
        if full_text:
            self._text = full_text
        self.body_lbl.setText(self._text if self._text else "...")
        self._model_name = (model_name or "").strip()
        if self.role == "assistant":
            short_model = self._model_name.replace("models/", "")
            self.model_lbl.setText(short_model)
            has_text = bool(self._text.strip())
            self.insert_btn.setVisible(self._allow_insert and has_text)
            self.replace_btn.setVisible(self._allow_replace and has_text)
            self.copy_btn.setVisible(has_text)
            self.actions_bar.setVisible(has_text or bool(short_model))

    def _on_copy_clicked(self):
        if not self._text:
            return
        try:
            pyperclip.copy(self._text)
        except Exception:
            pass
        self.copy_btn.setText("✓ Copiato")
        QTimer.singleShot(1400, self._restore_copy_label)

    def _restore_copy_label(self):
        try:
            self.copy_btn.setText("Copia")
        except RuntimeError:
            pass

    def apply_theme(self, is_dark: bool, accent_hex: str):
        self._is_dark = is_dark
        self._accent_hex = accent_hex or "#38BDF8"
        acc = QColor(self._accent_hex)
        if not acc.isValid():
            acc = QColor("#38BDF8")

        if self.role == "user":
            if is_dark:
                bg = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.20)"
                bd = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.42)"
                fg = "#F8FAFC"
            else:
                bg = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.14)"
                bd = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.36)"
                fg = "#0F172A"
        else:
            if is_dark:
                bg = "rgba(255, 255, 255, 0.05)"
                bd = "rgba(148, 163, 184, 0.16)"
                fg = "#E2E8F0"
            else:
                bg = "rgba(15, 23, 42, 0.04)"
                bd = "rgba(15, 23, 42, 0.11)"
                fg = "#1E293B"

        self.card.setStyleSheet(
            f"QFrame#WhisBubbleCard {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 12px; }}"
        )
        self.body_lbl.setStyleSheet(
            f"color: {fg}; font-size: 12.5px; background: transparent; border: none;"
        )

        meta_fg = "#64748B" if is_dark else "#64748B"
        self.model_lbl.setStyleSheet(
            f"color: {meta_fg}; font-size: 10px; font-weight: 600; background: transparent; border: none;"
        )

        if is_dark:
            btn_bg = "rgba(255, 255, 255, 0.06)"
            btn_fg = "#CBD5E1"
            btn_bd = "rgba(148, 163, 184, 0.20)"
            btn_hv = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.24)"
        else:
            btn_bg = "rgba(255, 255, 255, 0.85)"
            btn_fg = "#334155"
            btn_bd = "rgba(15, 23, 42, 0.14)"
            btn_hv = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.15)"

        act_qss = (
            f"QPushButton {{ background-color: {btn_bg}; color: {btn_fg}; "
            f"border: 1px solid {btn_bd}; border-radius: 6px; padding: 2px 8px; "
            f"font-size: 10.5px; font-weight: 600; min-height: 22px; }}"
            f"QPushButton:hover {{ background-color: {btn_hv}; border-color: {self._accent_hex}; }}"
        )
        for b in (self.insert_btn, self.replace_btn, self.copy_btn):
            b.setStyleSheet(act_qss)


class FloatingWhisChat(QWidget):
    """
    Assistente Whis flottante compatto e trascinabile all'interno di FlowLocal Hub.
    Consapevole del contesto corrente (nota/selezione in FlowNotes, impegni/esami in FlowTime).
    """

    insertIntoNoteRequested = pyqtSignal(str)
    replaceSelectionRequested = pyqtSignal(str)
    calendarDataChanged = pyqtSignal()
    noteDataChanged = pyqtSignal()
    closed = pyqtSignal()

    DEFAULT_WIDTH = 390
    DEFAULT_HEIGHT = 450

    def __init__(
        self,
        agent: Optional[WhisAgent] = None,
        theme_key: str = "obsidian",
        palette: Optional[Dict[str, str]] = None,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.agent = agent or WhisAgent()
        self._theme_key = theme_key
        self._palette: Dict[str, str] = dict(palette or {})
        self._is_dark = is_dark
        self._ui_context: Dict[str, Any] = {"workspace": "general"}
        self._active_worker: Optional[WhisStreamWorker] = None
        self._retired_workers: List[WhisStreamWorker] = []
        self._bubbles: List[_WhisMessageBubble] = []
        self._chip_buttons: List[QPushButton] = []
        self._user_dragged = False
        self._drag_origin: Optional[QPoint] = None

        self.setObjectName("FloatingWhisChat")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(330, 340)
        self.resize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)

        self._build_ui()
        self.set_theme(self._theme_key, self._palette, is_dark=self._is_dark)
        self.set_context(self._ui_context)

    def _accent_color_hex(self) -> str:
        ws = str(self._ui_context.get("workspace") or "general").lower()
        if ws == "calendar":
            return "#A855F7"
        if ws == "transcription":
            return "#10B981"
        return self._palette.get("shortcut_key_text") or self._palette.get("accent") or "#38BDF8"

    def _build_ui(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(18, 16, 18, 18)
        root_lay.setSpacing(8)

        # 1. Header minimale trascinabile (34px)
        self.header_bar = QFrame(self)
        self.header_bar.setObjectName("FloatingWhisHeader")
        self.header_bar.setFixedHeight(34)
        self.header_bar.setCursor(Qt.CursorShape.OpenHandCursor)
        self.header_bar.installEventFilter(self)

        hdr_lay = QHBoxLayout(self.header_bar)
        hdr_lay.setContentsMargins(4, 0, 2, 0)
        hdr_lay.setSpacing(7)

        self.header_dot = _WhisHeaderDot(self.header_bar)
        hdr_lay.addWidget(self.header_dot)

        self.title_lbl = QLabel("Whis", self.header_bar)
        hdr_lay.addWidget(self.title_lbl)

        self.context_chip = QLabel("Pronto", self.header_bar)
        self.context_chip.setMaximumWidth(210)
        hdr_lay.addWidget(self.context_chip)

        hdr_lay.addStretch(1)

        self.clear_btn = QPushButton("", self.header_bar)
        self.clear_btn.setFixedSize(24, 24)
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("Nuova conversazione")
        self.clear_btn.clicked.connect(self.clear_conversation)
        hdr_lay.addWidget(self.clear_btn)

        self.close_btn = QPushButton("", self.header_bar)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Chiudi (Esc)")
        self.close_btn.clicked.connect(self._on_close_clicked)
        hdr_lay.addWidget(self.close_btn)

        root_lay.addWidget(self.header_bar)

        # 2. Banner opzionale per testo selezionato
        self.selection_banner = QFrame(self)
        self.selection_banner.setObjectName("WhisSelectionBanner")
        sel_lay = QHBoxLayout(self.selection_banner)
        sel_lay.setContentsMargins(9, 5, 6, 5)
        sel_lay.setSpacing(6)

        self.selection_lbl = QLabel("", self.selection_banner)
        self.selection_lbl.setWordWrap(True)
        self.selection_lbl.setMaximumHeight(36)
        sel_lay.addWidget(self.selection_lbl, 1)

        self.clear_selection_btn = QPushButton("×", self.selection_banner)
        self.clear_selection_btn.setFixedSize(20, 20)
        self.clear_selection_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_selection_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_selection_btn.setToolTip("Rimuovi selezione attiva")
        self.clear_selection_btn.clicked.connect(self._clear_selected_text)
        sel_lay.addWidget(self.clear_selection_btn)

        self.selection_banner.hide()
        root_lay.addWidget(self.selection_banner)

        # 3. Area messaggi scorrevole (SOPRA alla barra di input, viewport opaca per evitare bleed-through)
        self.messages_scroll = QScrollArea(self)
        self.messages_scroll.setObjectName("FloatingWhisScroll")
        self.messages_scroll.setWidgetResizable(True)
        self.messages_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.messages_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.messages_scroll.viewport().setAutoFillBackground(True)

        self.messages_host = QWidget(self.messages_scroll)
        self.messages_host.setObjectName("FloatingWhisMessagesHost")
        self.messages_host.setAutoFillBackground(True)
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(2, 4, 4, 4)
        self.messages_layout.setSpacing(6)

        self.empty_state_card = QFrame(self.messages_host)
        self.empty_state_card.setObjectName("WhisEmptyCard")
        self.empty_state_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        es_lay = QVBoxLayout(self.empty_state_card)
        es_lay.setContentsMargins(10, 10, 10, 10)
        es_lay.setSpacing(4)
        self.empty_state_lbl = QLabel("", self.empty_state_card)
        self.empty_state_lbl.setWordWrap(True)
        es_lay.addWidget(self.empty_state_lbl)

        self.messages_layout.addWidget(self.empty_state_card)
        self.messages_layout.addStretch(1)
        self.messages_scroll.setWidget(self.messages_host)
        root_lay.addWidget(self.messages_scroll, 1)

        # 4. Chip contestuali rapidi (fuori dalla QScrollArea)
        self.chips_container = QWidget(self)
        self.chips_layout = QHBoxLayout(self.chips_container)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(5)
        root_lay.addWidget(self.chips_container)

        # 5. Barra di input SOTTO al testo (fuori dalla QScrollArea, >= 36px)
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)

        self.input_edit = QLineEdit(self)
        self.input_edit.setMinimumHeight(36)
        self.input_edit.setPlaceholderText("Chiedi a Whis su ciò che stai guardando...")
        self.input_edit.returnPressed.connect(self.send_query)
        input_row.addWidget(self.input_edit, 1)

        self.send_btn = QPushButton("", self)
        self.send_btn.setMinimumHeight(36)
        self.send_btn.setFixedWidth(38)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setToolTip("Invia a Whis")
        self.send_btn.clicked.connect(lambda: self.send_query())
        input_row.addWidget(self.send_btn)

        root_lay.addLayout(input_row)

    def eventFilter(self, watched, event):
        if watched is self.header_bar:
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                # Non avviare il drag se l'utente clicca sui pulsanti di header
                child = self.header_bar.childAt(event.position().toPoint())
                if isinstance(child, QPushButton):
                    return False
                self._drag_origin = event.globalPosition().toPoint() - self.pos()
                self.header_bar.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if etype == QEvent.Type.MouseMove and self._drag_origin is not None:
                new_pos = event.globalPosition().toPoint() - self._drag_origin
                self._user_dragged = True
                self.move(self._clamp_pos_to_parent(new_pos))
                return True
            if etype == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
                self._drag_origin = None
                self.header_bar.setCursor(Qt.CursorShape.OpenHandCursor)
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_close_clicked()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(8.0, 6.0, -8.0, -10.0)

        # Ombra esterna morbida dipinta manualmente (senza QGraphicsDropShadowEffect)
        shadow_layers = (
            (5.0, 3.0, 26 if self._is_dark else 10),
            (3.5, 2.0, 42 if self._is_dark else 16),
            (2.0, 1.0, 62 if self._is_dark else 24),
            (1.0, 0.5, 85 if self._is_dark else 32),
        )
        p.setPen(Qt.PenStyle.NoPen)
        for spread, dy, alpha in shadow_layers:
            sr = r.adjusted(-spread, -spread + dy, spread, spread + dy)
            sp = QPainterPath()
            sp.addRoundedRect(sr, 16.0 + spread * 0.5, 16.0 + spread * 0.5)
            p.fillPath(sp, QColor(0, 0, 0, alpha))

        path = QPainterPath()
        path.addRoundedRect(r, 16.0, 16.0)

        acc = QColor(self._accent_color_hex())
        if not acc.isValid():
            acc = QColor("#38BDF8")

        if self._is_dark:
            bg = QColor("#0D111C")
            sheen = QColor(acc.red(), acc.green(), acc.blue(), 18)
            border_c = QColor(acc.red(), acc.green(), acc.blue(), 115)
        else:
            bg = QColor("#FAFCFF")
            sheen = QColor(acc.red(), acc.green(), acc.blue(), 14)
            border_c = QColor(acc.red(), acc.green(), acc.blue(), 95)

        p.fillPath(path, bg)

        top_grad = QLinearGradient(r.left(), r.top(), r.right(), r.top() + 68.0)
        top_grad.setColorAt(0.0, sheen)
        top_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillPath(path, top_grad)

        pen = QPen(border_c)
        pen.setWidthF(1.1)
        p.setPen(pen)
        p.drawPath(path)
        p.end()

    def set_agent(self, agent: WhisAgent):
        if agent is not None:
            self.agent = agent

    def set_context(self, ui_context: Optional[Dict[str, Any]]):
        self._ui_context = dict(ui_context) if isinstance(ui_context, dict) else {"workspace": "general"}
        ws = str(self._ui_context.get("workspace") or "general").strip().lower()
        sel = str(self._ui_context.get("selected_text") or "").strip()

        if ws == "notes":
            title = str(self._ui_context.get("note_title") or "Nota").strip()
            short_title = (title[:18] + "…") if len(title) > 19 else title
            chip_text = f"Note · {short_title}"
            if sel:
                chip_text += " · Selezione"
            elif self._ui_context.get("has_audio_sync"):
                chip_text += " · Sbobina"
            empty_msg = (
                f"Sto osservando **{title}** e la tua selezione corrente."
                if sel
                else f"Sto osservando la nota **{title}** (inclusa eventuale sbobina audio)."
            )
            placeholder = (
                "Chiedi su questa selezione (spiega, riscrivi, correggi)..."
                if sel
                else "Chiedi a Whis su questa nota..."
            )
        elif ws == "calendar":
            date_lbl = str(
                self._ui_context.get("selected_date_label")
                or self._ui_context.get("selected_date")
                or "Oggi"
            ).strip()
            ev_count = len(self._ui_context.get("day_events") or [])
            short_d = (date_lbl[:16] + "…") if len(date_lbl) > 17 else date_lbl
            chip_text = f"Calendario · {short_d} · {ev_count} event{'o' if ev_count == 1 else 'i'}"
            empty_msg = (
                f"Vedo i tuoi impegni di **{date_lbl}**, la vista settimanale e i prossimi esami in FlowTime."
            )
            placeholder = "Chiedi su impegni, buchi liberi o scadenze..."
        elif ws == "transcription":
            mode = str(self._ui_context.get("active_mode") or "natural").strip()
            chip_text = f"Trascrizione · {mode}"
            empty_msg = "Vedo le trascrizioni recenti di FlowType. Chiedimi di rifinire o riassumere."
            placeholder = "Chiedi a Whis sulla dettatura corrente..."
        else:
            chip_text = "FlowLocal"
            empty_msg = "Ciao! Chiedimi qualsiasi cosa sul tuo lavoro corrente."
            placeholder = "Chiedi a Whis..."

        self.context_chip.setText(chip_text)
        self.empty_state_lbl.setText(empty_msg.replace("**", ""))
        self.input_edit.setPlaceholderText(placeholder)

        if sel:
            single_line = " ".join(sel.split())
            preview = (single_line[:88] + "…") if len(single_line) > 90 else single_line
            self.selection_lbl.setText(f"Selezione: «{preview}»")
            self.selection_banner.show()
        else:
            self.selection_banner.hide()

        self._rebuild_quick_chips()
        self.apply_context_accent()

    def _clear_selected_text(self):
        if isinstance(self._ui_context, dict):
            self._ui_context.pop("selected_text", None)
        self.set_context(self._ui_context)

    def _rebuild_quick_chips(self):
        for btn in self._chip_buttons:
            self.chips_layout.removeWidget(btn)
            btn.deleteLater()
        self._chip_buttons.clear()
        while self.chips_layout.count():
            item = self.chips_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        ws = str(self._ui_context.get("workspace") or "general").strip().lower()
        has_sel = bool(str(self._ui_context.get("selected_text") or "").strip())

        if ws == "notes" and has_sel:
            specs = [
                ("Spiega", "Spiegami in modo chiaro il testo selezionato."),
                ("Migliora", "Migliora forma e chiarezza del testo selezionato mantenendone il senso."),
                ("Riassumi", "Riassumi in punti essenziali il testo selezionato."),
                ("Correggi", "Correggi grammatica, punteggiatura ed eventuali errori nel testo selezionato."),
            ]
        elif ws == "notes":
            specs = [
                ("Riassumi nota", "Riassumi i concetti principali della nota aperta."),
                ("Punti chiave", "Estrai i punti chiave della nota in un elenco sintetico."),
                ("Spiega formule", "Spiega le formule e i passaggi presenti nella nota."),
            ]
        elif ws == "calendar":
            specs = [
                ("Pianifica oggi", "Aiutami a organizzare gli impegni e lo studio per la data selezionata."),
                ("Prossime scadenze", "Quali sono i prossimi esami e scadenze imminenti?"),
                ("Trova spazio libero", "Dove ho fasce orarie libere per studiare questa settimana?"),
            ]
        else:
            specs = [
                ("Riassumi", "Riassumi il contesto corrente in modo sintetico."),
                ("Migliora testo", "Rendi più chiaro ed elegante il testo corrente."),
            ]

        for label, prompt_text in specs:
            b = QPushButton(label, self.chips_container)
            b.setFixedHeight(26)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _chk=False, q=prompt_text: self.send_query(q))
            self.chips_layout.addWidget(b)
            self._chip_buttons.append(b)
        self.chips_layout.addStretch(1)
        self._style_chips()

    def apply_context_accent(self):
        self.set_theme(self._theme_key, self._palette, is_dark=self._is_dark)

    def set_theme(self, theme_key: str, palette: Optional[Dict[str, str]] = None, is_dark: bool = True):
        self._theme_key = theme_key or "obsidian"
        if palette is not None:
            self._palette = dict(palette)
        self._is_dark = bool(is_dark)

        acc_hex = self._accent_color_hex()
        acc = QColor(acc_hex)
        if not acc.isValid():
            acc = QColor("#38BDF8")
        c2 = QColor("#A855F7")
        self.header_dot.set_colors(acc, c2)

        surface_hex = "#0D111C" if self._is_dark else "#FAFCFF"
        sb_handle = "rgba(148, 163, 184, 0.30)" if self._is_dark else "rgba(100, 116, 139, 0.30)"
        sb_hover = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.55)"
        self.messages_scroll.setStyleSheet("")
        self.messages_scroll.viewport().setStyleSheet("")
        self.messages_host.setStyleSheet("")
        self.messages_scroll.verticalScrollBar().setStyleSheet(
            f"QScrollBar:vertical {{ background-color: {surface_hex}; width: 6px; margin: 2px 0; border: none; }}"
            f"QScrollBar::handle:vertical {{ background-color: {sb_handle}; border-radius: 3px; min-height: 24px; }}"
            f"QScrollBar::handle:vertical:hover {{ background-color: {sb_hover}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: {surface_hex}; }}"
        )
        surface_col = QColor(surface_hex)
        for target_w in (self.messages_scroll, self.messages_scroll.viewport(), self.messages_host):
            pal = target_w.palette()
            pal.setColor(QPalette.ColorRole.Window, surface_col)
            pal.setColor(QPalette.ColorRole.Base, surface_col)
            target_w.setPalette(pal)
            target_w.setAutoFillBackground(True)

        fg_title = "#F8FAFC" if self._is_dark else "#0F172A"
        fg_muted = "#94A3B8" if self._is_dark else "#475569"
        self.title_lbl.setStyleSheet(f"color: {fg_title}; font-size: 12.5px; font-weight: 700; background: transparent;")

        chip_bg = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, {0.16 if self._is_dark else 0.12})"
        chip_bd = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, {0.36 if self._is_dark else 0.28})"
        self.context_chip.setStyleSheet(
            f"background-color: {chip_bg}; color: {acc_hex}; border: 1px solid {chip_bd}; "
            f"border-radius: 9px; padding: 1px 8px; font-size: 10.5px; font-weight: 600;"
        )

        icon_btn_bg = "rgba(255,255,255,0.04)" if self._is_dark else "rgba(15,23,42,0.04)"
        icon_btn_hv = "rgba(255,255,255,0.12)" if self._is_dark else "rgba(15,23,42,0.10)"
        hdr_btn_qss = (
            f"QPushButton {{ background-color: {icon_btn_bg}; border: none; border-radius: 6px; padding: 0; }}"
            f"QPushButton:hover {{ background-color: {icon_btn_hv}; }}"
        )
        self.clear_btn.setStyleSheet(hdr_btn_qss)
        self.close_btn.setStyleSheet(hdr_btn_qss)
        self.clear_btn.setIcon(get_icon("eraser", theme_key=self._theme_key, role="subtitle", size=13))
        self.clear_btn.setIconSize(QSize(13, 13))
        self.close_btn.setIcon(get_icon("close", theme_key=self._theme_key, role="subtitle", size=13))
        self.close_btn.setIconSize(QSize(13, 13))

        sel_bg = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, {0.10 if self._is_dark else 0.08})"
        sel_bd = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, {0.26 if self._is_dark else 0.22})"
        self.selection_banner.setStyleSheet(
            f"QFrame#WhisSelectionBanner {{ background-color: {sel_bg}; border: 1px solid {sel_bd}; border-radius: 9px; }}"
        )
        self.selection_lbl.setStyleSheet(
            f"color: {'#CBD5E1' if self._is_dark else '#334155'}; font-size: 11px; background: transparent; border: none;"
        )
        self.clear_selection_btn.setStyleSheet(
            f"QPushButton {{ color: {fg_muted}; background: transparent; border: none; font-size: 13px; font-weight: 700; }}"
            f"QPushButton:hover {{ color: {fg_title}; }}"
        )

        es_bg = "rgba(255, 255, 255, 0.04)" if self._is_dark else "rgba(15, 23, 42, 0.035)"
        es_bd = "rgba(148, 163, 184, 0.14)" if self._is_dark else "rgba(15, 23, 42, 0.09)"
        self.empty_state_card.setStyleSheet(
            f"QFrame#WhisEmptyCard {{ background-color: {es_bg}; border: 1px solid {es_bd}; border-radius: 12px; }}"
        )
        self.empty_state_lbl.setStyleSheet(
            f"color: {fg_muted}; font-size: 12px; background: transparent; border: none;"
        )

        in_bg = "rgba(255, 255, 255, 0.06)" if self._is_dark else "#FFFFFF"
        in_bd = "rgba(148, 163, 184, 0.24)" if self._is_dark else "rgba(15, 23, 42, 0.16)"
        in_fg = "#F8FAFC" if self._is_dark else "#0F172A"
        self.input_edit.setStyleSheet(
            f"QLineEdit {{ background-color: {in_bg}; color: {in_fg}; border: 1px solid {in_bd}; "
            f"border-radius: 10px; padding: 6px 10px; font-size: 12.5px; min-height: 36px; }}"
            f"QLineEdit:focus {{ border-color: {acc_hex}; }}"
        )
        self.send_btn.setStyleSheet(
            f"QPushButton {{ background-color: {acc_hex}; border: none; border-radius: 10px; min-height: 36px; }}"
            f"QPushButton:hover {{ background-color: {acc.lighter(112).name()}; }}"
            f"QPushButton:disabled {{ background-color: rgba(148, 163, 184, 0.28); }}"
        )
        self.send_btn.setIcon(get_icon("sparkles", theme_key=self._theme_key, role="on_accent", size=15))
        self.send_btn.setIconSize(QSize(15, 15))

        self._style_chips()
        for bubble in self._bubbles:
            bubble.apply_theme(self._is_dark, acc_hex)
        self.update()

    def _style_chips(self):
        acc_hex = self._accent_color_hex()
        acc = QColor(acc_hex)
        if not acc.isValid():
            acc = QColor("#38BDF8")
        if self._is_dark:
            bg = "rgba(255, 255, 255, 0.05)"
            fg = "#CBD5E1"
            bd = "rgba(148, 163, 184, 0.20)"
            hv = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.20)"
        else:
            bg = "rgba(15, 23, 42, 0.04)"
            fg = "#334155"
            bd = "rgba(15, 23, 42, 0.12)"
            hv = f"rgba({acc.red()}, {acc.green()}, {acc.blue()}, 0.14)"
        qss = (
            f"QPushButton {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 13px; padding: 0px 10px; font-size: 10.5px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {hv}; border-color: {acc_hex}; }}"
        )
        for b in self._chip_buttons:
            b.setStyleSheet(qss)

    def _clamp_pos_to_parent(self, pos: QPoint) -> QPoint:
        parent = self.parentWidget()
        if parent is None:
            return pos
        margin = 14
        top_margin = 64
        max_x = max(margin, parent.width() - self.width() - margin)
        max_y = max(top_margin, parent.height() - self.height() - margin)
        x = max(margin, min(pos.x(), max_x))
        y = max(top_margin, min(pos.y(), max_y))
        return QPoint(x, y)

    def sync_parent_bounds(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if not self._user_dragged:
            margin = 18
            top_margin = 66
            target_x = max(margin, parent.width() - self.width() - margin)
            target_y = max(top_margin, parent.height() - self.height() - margin)
            self.move(self._clamp_pos_to_parent(QPoint(target_x, target_y)))
        else:
            self.move(self._clamp_pos_to_parent(self.pos()))

    def open_floating(
        self,
        ui_context: Optional[Dict[str, Any]] = None,
        prefill: str = "",
        auto_send: bool = False,
    ):
        if ui_context is not None:
            self.set_context(ui_context)
        self.sync_parent_bounds()
        self.show()
        self.raise_()
        if prefill:
            self.input_edit.setText(prefill)
        self.input_edit.setFocus()
        if auto_send and (prefill or self.input_edit.text().strip()):
            self.send_query()

    def toggle_floating(self, ui_context: Optional[Dict[str, Any]] = None):
        if self.isVisible():
            self._on_close_clicked()
        else:
            self.open_floating(ui_context=ui_context)

    def _on_close_clicked(self):
        self.hide()
        self.closed.emit()

    def clear_conversation(self):
        if self.agent is not None and hasattr(self.agent, "clear_history"):
            self.agent.clear_history()
        for b in self._bubbles:
            self.messages_layout.removeWidget(b)
            b.deleteLater()
        self._bubbles.clear()
        self.empty_state_card.show()

    def _append_bubble(
        self,
        role: str,
        text: str = "",
        allow_insert: bool = False,
        allow_replace: bool = False,
    ) -> _WhisMessageBubble:
        self.empty_state_card.hide()
        bubble = _WhisMessageBubble(
            role=role,
            text=text,
            allow_insert=allow_insert,
            allow_replace=allow_replace,
            is_dark=self._is_dark,
            accent_hex=self._accent_color_hex(),
            parent=self.messages_host,
        )
        bubble.insertRequested.connect(self.insertIntoNoteRequested.emit)
        bubble.replaceRequested.connect(self.replaceSelectionRequested.emit)

        insert_idx = max(0, self.messages_layout.count() - 1)
        self.messages_layout.insertWidget(insert_idx, bubble)
        self._bubbles.append(bubble)
        QTimer.singleShot(0, self._scroll_to_bottom)
        return bubble

    def _scroll_to_bottom(self):
        try:
            sb = self.messages_scroll.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())
        except RuntimeError:
            pass

    def _retire_worker(self, worker: WhisStreamWorker):
        if self._active_worker is worker:
            self._active_worker = None
            self.send_btn.setEnabled(True)
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)
        worker.deleteLater()

    def send_query(self, query_text: Optional[str] = None):
        query = (query_text if query_text is not None else self.input_edit.text()).strip()
        if not query or self._active_worker is not None:
            return
        if query_text is None:
            self.input_edit.clear()

        ctx_snapshot = dict(self._ui_context or {"workspace": "general"})
        ws = str(ctx_snapshot.get("workspace") or "general").lower()
        sel_text = str(ctx_snapshot.get("selected_text") or "").strip()

        self._append_bubble("user", query)
        assistant_bubble = self._append_bubble(
            "assistant",
            "",
            allow_insert=(ws == "notes"),
            allow_replace=(ws == "notes" and bool(sel_text)),
        )

        self.send_btn.setEnabled(False)
        worker = WhisStreamWorker(
            self.agent,
            query,
            explicit_selection=sel_text or None,
            ui_context=ctx_snapshot,
        )
        self._active_worker = worker
        self._retired_workers.append(worker)

        def on_chunk(delta: str, b=assistant_bubble):
            if b in self._bubbles:
                b.append_chunk(delta)
                self._scroll_to_bottom()

        def on_meta(meta: dict):
            if isinstance(meta, dict):
                if meta.get("calendar"):
                    self.calendarDataChanged.emit()
                if meta.get("note_action"):
                    self.noteDataChanged.emit()

        def on_done(full_text: str, model_name: str, b=assistant_bubble):
            if b in self._bubbles:
                b.finalize(full_text, model_name)
                self._scroll_to_bottom()
            if self._active_worker is worker:
                self._active_worker = None
            self.send_btn.setEnabled(True)

        def on_error(err: str, b=assistant_bubble):
            if b in self._bubbles:
                b.set_error(err)
                self._scroll_to_bottom()
            if self._active_worker is worker:
                self._active_worker = None
            self.send_btn.setEnabled(True)

        worker.chunk_received.connect(on_chunk)
        worker.metadata_detected.connect(on_meta)
        worker.finished_response.connect(on_done)
        worker.error_occurred.connect(on_error)
        worker.finished.connect(lambda w=worker: self._retire_worker(w))
        worker.start()
