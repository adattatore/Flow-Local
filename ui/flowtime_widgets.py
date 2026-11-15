"""Widget calendario, timeline, To-Do, Pomodoro Ring ed Esami per FlowTime & Focus."""

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

from database import db
from ui.themes import _format_timestamp
from ui.theme_widgets import ScrollList, _make_button, _make_card, _make_label


FLOWTIME_COLOR_PALETTE = [
    ("#6366F1", "Indigo"),
    ("#38BDF8", "Ciano"),
    ("#10B981", "Smeraldo"),
    ("#F59E0B", "Ambra"),
    ("#EC4899", "Rosa"),
    ("#EF4444", "Rosso"),
]


class LinkEventNoteDialog(QDialog):
    """HUD di ricerca e collegamento rapido tra note di FlowNotes e impegni di FlowTime."""

    open_note_requested = pyqtSignal(int)

    def __init__(
        self,
        event_id: Optional[int],
        event_title: str,
        preselected_ids: Optional[List[int]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.event_id = event_id
        self._selected_ids: List[int] = []
        if event_id is not None:
            self._selected_ids = [int(n["id"]) for n in db.get_notes_for_event(event_id)]
        elif preselected_ids:
            self._selected_ids = [int(x) for x in preselected_ids]
        self._selected_set = set(self._selected_ids)

        self.setWindowTitle("🔗 Collega Note a FlowTime")
        self.setMinimumSize(560, 460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(_make_label(f"🔗 {event_title or 'Impegno FlowTime'}", "CardTitle", wrap=True), 1)
        self.count_badge = _make_label("", "BadgeAccent")
        top_row.addWidget(self.count_badge)
        layout.addLayout(top_row)

        layout.addWidget(
            _make_label(
                "Cerca qualsiasi nota, sbobina o slide di FlowNotes e collegala all'impegno selezionato:",
                "PageSubtitle",
                wrap=True,
            )
        )

        # Barra di ricerca fuori dalla QScrollArea (regola Qt6 UI)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 Cerca per titolo, materia o contenuto...")
        self.search_edit.setMinimumHeight(38)
        self.search_edit.textChanged.connect(self._populate_list)
        layout.addWidget(self.search_edit)

        self.notes_list = ScrollList("Nessuna nota corrispondente trovata in FlowNotes.")
        self.notes_list.setMinimumHeight(290)
        layout.addWidget(self.notes_list, 1)

        self._all_notes = [
            n for n in db.get_all_study_notes()
            if (n.get("note_type") or "").lower() != "flashcard"
        ]
        self._populate_list("")

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.done_btn = _make_button("✓ Fatto", "primary")
        self.done_btn.clicked.connect(self.accept)
        btns.addWidget(self.done_btn)
        layout.addLayout(btns)

    @property
    def selected_note_ids(self) -> List[int]:
        return sorted(self._selected_set)

    def _update_count_badge(self):
        cnt = len(self._selected_set)
        self.count_badge.setText(f"{cnt} collegat{'a' if cnt == 1 else 'e'}")

    def _populate_list(self, query: str):
        self.notes_list.clear()
        self._update_count_badge()
        q = (query or "").strip().lower()
        matches = []
        for n in self._all_notes:
            title = (n.get("title") or "Senza titolo").strip()
            subj = (n.get("subject") or "Generale").strip()
            content = (n.get("content") or "")[:300].lower()
            if q and q not in title.lower() and q not in subj.lower() and q not in content:
                continue
            matches.append(n)

        if not matches:
            self.notes_list.show_empty("Nessuna nota trovata con questi criteri.")
            return

        # Mostra prima le note già collegate
        matches.sort(key=lambda item: (0 if int(item["id"]) in self._selected_set else 1))
        for note in matches:
            nid = int(note["id"])
            card, c_lay = _make_card()
            row = QHBoxLayout()
            row.setSpacing(10)

            info_col = QVBoxLayout()
            info_col.setSpacing(3)
            meta = QHBoxLayout()
            meta.setSpacing(6)
            meta.addWidget(_make_label(note.get("subject") or "Generale", "Badge"))
            type_lbl = (note.get("note_type") or "Appunto").capitalize()
            meta.addWidget(_make_label(f"{type_lbl} · {_format_timestamp(note.get('created_at'))}", "NoteMeta"))
            meta.addStretch(1)
            info_col.addLayout(meta)

            title_lbl = _make_label(note.get("title") or "Senza titolo", "CardTitle", wrap=True)
            info_col.addWidget(title_lbl)
            row.addLayout(info_col, 1)

            btn_col = QHBoxLayout()
            btn_col.setSpacing(6)
            is_linked = nid in self._selected_set
            toggle_btn = _make_button("✅ Collegata" if is_linked else "🔗 Collega", "primary" if is_linked else None)
            toggle_btn.clicked.connect(lambda _c=False, xid=nid: self._toggle_link(xid))
            btn_col.addWidget(toggle_btn)

            open_btn = _make_button("📖 Apri")
            open_btn.clicked.connect(lambda _c=False, xid=nid: self._on_open_note(xid))
            btn_col.addWidget(open_btn)
            row.addLayout(btn_col)

            c_lay.addLayout(row)
            self.notes_list.add_card(card)

    def _toggle_link(self, note_id: int):
        if note_id in self._selected_set:
            self._selected_set.remove(note_id)
            if self.event_id is not None:
                db.unlink_note_from_event(self.event_id, note_id)
        else:
            self._selected_set.add(note_id)
            if self.event_id is not None:
                db.link_note_to_event(self.event_id, note_id, link_type="manual")
        self._populate_list(self.search_edit.text())

    def _on_open_note(self, note_id: int):
        self.open_note_requested.emit(int(note_id))
        self.accept()


class TimelineQuickAddDialog(QDialog):
    """HUD rapido aperto cliccando uno spazio vuoto sulla timeline per aggiungere Evento o To-Do."""

    def __init__(self, date_str: str, start_time_str: str = "09:00", parent=None):
        super().__init__(parent)
        self.date_str = date_str
        self._item_type = "event"
        self._selected_color = FLOWTIME_COLOR_PALETTE[0][0]
        self._linked_note_ids: List[int] = []
        self.created_event: Optional[Dict[str, Any]] = None

        self.setWindowTitle("➕ Nuovo Impegno o Evento")
        self.setMinimumWidth(460)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(11)

        try:
            d_obj = dt_date.fromisoformat(date_str)
            day_pretty = d_obj.strftime("%d/%m/%Y")
        except Exception:
            day_pretty = date_str

        layout.addWidget(_make_label(f"📅 {day_pretty} · Aggiungi a FlowTime", "CardTitle"))

        # Switch Evento / To-Do
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self.event_mode_btn = QPushButton(f"📅 Evento ({start_time_str})")
        self.event_mode_btn.setObjectName("FTSegmentBtn")
        self.event_mode_btn.setCheckable(True)
        self.event_mode_btn.setChecked(True)
        self.event_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.todo_mode_btn = QPushButton("☑️ Impegno / To-Do (Senza orario)")
        self.todo_mode_btn.setObjectName("FTSegmentBtn")
        self.todo_mode_btn.setCheckable(True)
        self.todo_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._mode_group.addButton(self.event_mode_btn, 0)
        self._mode_group.addButton(self.todo_mode_btn, 1)
        self.event_mode_btn.toggled.connect(lambda checked: self._set_item_type("event" if checked else "todo"))
        mode_row.addWidget(self.event_mode_btn, 1)
        mode_row.addWidget(self.todo_mode_btn, 1)
        layout.addLayout(mode_row)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Titolo evento (es. Lezione Robotica)...")
        self.title_edit.setMinimumHeight(36)
        layout.addWidget(self.title_edit)

        self.time_row_widget = QWidget()
        self.time_row_widget.setStyleSheet("background: transparent;")
        tr = QHBoxLayout(self.time_row_widget)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(8)
        self.start_edit = QLineEdit(start_time_str or "09:00")
        self.start_edit.setFixedWidth(74)
        self.start_edit.setMinimumHeight(36)
        self.start_edit.setPlaceholderText("HH:MM")
        tr.addWidget(self.start_edit)

        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumHeight(36)
        for label, mins in [("30 min", 30), ("45 min", 45), ("1 ora", 60), ("1h 30m", 90), ("2 ore", 120), ("3 ore", 180)]:
            self.duration_combo.addItem(label, mins)
        self.duration_combo.setCurrentIndex(2)
        tr.addWidget(self.duration_combo)

        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("Luogo / Aula (opz.)")
        self.location_edit.setMinimumHeight(36)
        tr.addWidget(self.location_edit, 1)
        layout.addWidget(self.time_row_widget)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self.category_combo = QComboBox()
        self.category_combo.setMinimumHeight(36)
        for cat in ["Personale", "Università", "Lavoro", "Esame", "Studio"]:
            self.category_combo.addItem(cat)
        meta_row.addWidget(self.category_combo)

        self.subject_edit = QLineEdit()
        self.subject_edit.setPlaceholderText("Materia / Tag (opz.)")
        self.subject_edit.setMinimumHeight(36)
        meta_row.addWidget(self.subject_edit, 1)
        layout.addLayout(meta_row)

        # Colori + Ripetizione
        style_row = QHBoxLayout()
        style_row.setSpacing(8)
        style_row.addWidget(_make_label("Colore:", "PageSubtitle"))
        self._color_buttons: Dict[str, QPushButton] = {}
        for hex_col, name in FLOWTIME_COLOR_PALETTE:
            btn = QPushButton("")
            btn.setObjectName("FTColorDot")
            btn.setToolTip(name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, hc=hex_col: self._set_color(hc))
            self._color_buttons[hex_col] = btn
            style_row.addWidget(btn)
        style_row.addSpacing(8)

        self.recurrence_combo = QComboBox()
        self.recurrence_combo.setMinimumHeight(36)
        for label, val in [("Non si ripete", "none"), ("Ogni giorno", "daily"), ("Ogni settimana", "weekly"), ("Ogni mese", "monthly")]:
            self.recurrence_combo.addItem(label, val)
        style_row.addWidget(self.recurrence_combo, 1)
        layout.addLayout(style_row)
        self._refresh_color_buttons()

        # Collega nota
        note_row = QHBoxLayout()
        note_row.setSpacing(8)
        self.linked_badge = _make_label("Nessuna nota collegata", "Badge")
        note_row.addWidget(self.linked_badge, 1)
        self.link_notes_btn = _make_button("🔗 Collega Nota")
        self.link_notes_btn.clicked.connect(self._on_open_note_hud)
        note_row.addWidget(self.link_notes_btn)
        layout.addLayout(note_row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        save_btn = _make_button("💾 Salva in FlowTime", "primary")
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(save_btn)
        layout.addLayout(btns)
        self.title_edit.setFocus()

    def _set_item_type(self, mode: str):
        self._item_type = mode
        is_todo = mode == "todo"
        self.time_row_widget.setVisible(not is_todo)
        self.title_edit.setPlaceholderText(
            "Impegno senza orario (es. Innaffiare le piante)..."
            if is_todo
            else "Titolo evento (es. Lezione Robotica)..."
        )

    def _set_color(self, hex_col: str):
        self._selected_color = hex_col
        self._refresh_color_buttons()

    def _refresh_color_buttons(self):
        for hc, btn in self._color_buttons.items():
            border = "2px solid #FFFFFF" if hc == self._selected_color else "1px solid rgba(148,163,184,0.35)"
            btn.setStyleSheet(f"background-color: {hc}; border: {border}; border-radius: 10px;")

    def _on_open_note_hud(self):
        title = self.title_edit.text().strip() or "Nuovo impegno FlowTime"
        dlg = LinkEventNoteDialog(None, title, preselected_ids=self._linked_note_ids, parent=self)
        dlg.exec()
        self._linked_note_ids = list(dlg.selected_note_ids)
        cnt = len(self._linked_note_ids)
        self.linked_badge.setText("Nessuna nota collegata" if cnt == 0 else f"🔗 {cnt} not{'a' if cnt == 1 else 'e'} collegat{'a' if cnt == 1 else 'e'}")

    def _on_save(self):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Titolo mancante", "Inserisci un titolo per salvare in FlowTime.")
            self.title_edit.setFocus()
            return
        from core.calendar_sync import calendar_manager
        is_todo = self._item_type == "todo"
        created = calendar_manager.create_event(
            summary=title,
            date_str=self.date_str,
            start_time_str="00:00" if is_todo else (self.start_edit.text().strip() or "09:00"),
            duration_minutes=0 if is_todo else int(self.duration_combo.currentData() or 60),
            location="" if is_todo else self.location_edit.text().strip(),
            category=self.category_combo.currentText(),
            subject=self.subject_edit.text().strip(),
            color=self._selected_color,
            push_to_google=not is_todo,
            item_type=self._item_type,
            recurrence=str(self.recurrence_combo.currentData() or "none"),
        )
        for nid in self._linked_note_ids:
            db.link_note_to_event(created["id"], int(nid), link_type="manual")
        self.created_event = created
        self.accept()


class FlowTimeCalendarWidget(QCalendarWidget):
    """Calendario mensile con celle a sfondo neutro, giorno selezionato evidenziato e oggi cerchiato di rosso."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._activity_map: Dict[str, Dict[str, int]] = {}
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setGridVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setLocale(QLocale(QLocale.Language.Italian, QLocale.Country.Italy))

    def set_activity_data(
        self,
        activity_map: Dict[str, Dict[str, int]],
        is_dark: bool,
        reading_mode: bool = False,
    ):
        self._activity_map = dict(activity_map or {})
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self.updateCells()

    def paintCell(self, painter: QPainter, rect: QRect, date: QDate):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._is_dark:
            bg_neutral = QColor("#18130F") if self._reading_mode else QColor("#0D1322")
            text_main = QColor("#E6D7C3") if self._reading_mode else QColor("#E2E8F0")
            text_weekend = QColor("#A8957E") if self._reading_mode else QColor("#94A3B8")
            text_other_month = QColor("#574536") if self._reading_mode else QColor("#475569")
            sel_fill = QColor("#D97706") if self._reading_mode else QColor("#6366F1")
            sel_border = QColor("#FBBF24") if self._reading_mode else QColor("#818CF8")
        else:
            bg_neutral = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            text_main = QColor("#2A1F14") if self._reading_mode else QColor("#1E293B")
            text_weekend = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            text_other_month = QColor("#C4B5A0") if self._reading_mode else QColor("#CBD5E1")
            sel_fill = QColor("#B45309") if self._reading_mode else QColor("#4F46E5")
            sel_border = QColor("#D97706") if self._reading_mode else QColor("#6366F1")

        # 1. Sfondo neutro uniforme per ogni giorno
        painter.fillRect(rect, bg_neutral)

        is_selected = date == self.selectedDate()
        is_today = date == QDate.currentDate()
        in_month = date.year() == self.yearShown() and date.month() == self.monthShown()

        # 2. Giorno selezionato evidenziato chiaramente
        if is_selected:
            pill_rect = QRectF(rect).adjusted(4.0, 3.0, -4.0, -3.0)
            painter.setPen(QPen(sel_border, 1.4))
            painter.setBrush(QBrush(sel_fill))
            painter.drawRoundedRect(pill_rect, 8.0, 8.0)

        # 3. Numero del giorno (oggi in rosso quando non selezionato, senza cerchio)
        font = QFont(painter.font())
        font.setWeight(QFont.Weight.Bold if (is_selected or is_today) else QFont.Weight.Medium)
        painter.setFont(font)

        if is_selected:
            txt_col = QColor("#FFFFFF")
        elif not in_month:
            txt_col = text_other_month
        elif is_today:
            txt_col = QColor("#EF4444")
        elif date.dayOfWeek() in (6, 7):
            txt_col = text_weekend
        else:
            txt_col = text_main

        painter.setPen(txt_col)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(date.day()))

        # 5. Puntini discreti di attività in basso (senza colorare lo sfondo della cella)
        counts = self._activity_map.get(date.toString("yyyy-MM-dd")) or {}
        ev_cnt = counts.get("events", 0)
        note_cnt = counts.get("notes", 0)
        if ev_cnt > 0 or note_cnt > 0:
            dots = []
            if ev_cnt > 0:
                dots.append(QColor("#FFFFFF") if is_selected else QColor("#38BDF8"))
            if note_cnt > 0:
                dots.append(QColor("#A7F3D0") if is_selected else QColor("#10B981"))
            dot_y = float(rect.bottom()) - 3.5
            cx = float(rect.center().x())
            spacing = 5.5
            start_x = cx - ((len(dots) - 1) * spacing) / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            for i, dc in enumerate(dots):
                painter.setBrush(QBrush(dc))
                painter.drawEllipse(QPointF(start_x + i * spacing, dot_y), 1.7, 1.7)

        painter.restore()


class _DayTimelineCanvas(QWidget):
    """Canvas 00:00–23:59 a piena larghezza per la timeline giornaliera interattiva."""

    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)

    HOUR_HEIGHT = 54
    TOP_PAD = 12
    BOTTOM_PAD = 16
    AXIS_X = 52

    def __init__(self, parent=None):
        super().__init__(parent)
        self._date_str: str = dt_date.today().isoformat()
        self._events: List[Dict[str, Any]] = []
        self._notes_by_event: Dict[int, List[Dict[str, Any]]] = {}
        self._selected_event_id: Optional[int] = None
        self._placed_blocks: List[Dict[str, Any]] = []
        self._hit_rects: List[tuple] = []
        self._note_hit_rects: List[tuple] = []
        self._hover_time_min: Optional[int] = None
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setMouseTracking(True)
        self.setFixedHeight(24 * self.HOUR_HEIGHT + self.TOP_PAD + self.BOTTOM_PAD)

    @staticmethod
    def _parse_hhmm(time_str: str) -> Optional[int]:
        raw = (time_str or "").strip()
        if ":" not in raw:
            return None
        try:
            hh, mm = raw[:5].split(":")
            h_i = int(hh)
            m_i = int(mm)
            if h_i == 24 and m_i == 0:
                return 1440
            if 0 <= h_i <= 23 and 0 <= m_i <= 59:
                return h_i * 60 + m_i
        except Exception:
            return None
        return None

    def _y_to_snapped_hhmm(self, y: float) -> tuple:
        raw_mins = ((y - float(self.TOP_PAD)) / float(self.HOUR_HEIGHT)) * 60.0
        snapped = int(round(raw_mins / 30.0) * 30)
        snapped = max(0, min(1410, snapped))
        return snapped, f"{snapped // 60:02d}:{snapped % 60:02d}"

    def set_data(
        self,
        date_str: str,
        timed_events: List[Dict[str, Any]],
        is_dark: bool,
        reading_mode: bool = False,
        notes_by_event: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        selected_event_id: Optional[int] = None,
    ):
        self._date_str = date_str or dt_date.today().isoformat()
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self._notes_by_event = notes_by_event or {}
        self._selected_event_id = selected_event_id

        items = []
        for ev in timed_events:
            s_min = self._parse_hhmm(ev.get("start_time") or "")
            if s_min is None:
                s_min = 9 * 60
            e_min = self._parse_hhmm(ev.get("end_time") or "")
            if e_min is None or e_min <= s_min:
                e_min = min(1440, s_min + 60)
            vis_end = max(s_min + 34, e_min)
            items.append({"ev": ev, "start": s_min, "end": e_min, "vis_end": vis_end})

        items.sort(key=lambda x: (x["start"], x["vis_end"]))

        self._placed_blocks = []
        clusters: List[List[Dict[str, Any]]] = []
        cur_cluster: List[Dict[str, Any]] = []
        cluster_end = -1
        for it in items:
            if not cur_cluster or it["start"] < cluster_end:
                cur_cluster.append(it)
                cluster_end = max(cluster_end, it["vis_end"])
            else:
                clusters.append(cur_cluster)
                cur_cluster = [it]
                cluster_end = it["vis_end"]
        if cur_cluster:
            clusters.append(cur_cluster)

        for cl in clusters:
            lane_ends: List[int] = []
            for it in cl:
                placed_lane = None
                for li, lend in enumerate(lane_ends):
                    if it["start"] >= lend:
                        placed_lane = li
                        lane_ends[li] = it["vis_end"]
                        break
                if placed_lane is None:
                    placed_lane = len(lane_ends)
                    lane_ends.append(it["vis_end"])
                it["lane"] = placed_lane
            total_lanes = max(1, len(lane_ends))
            for it in cl:
                it["lanes"] = total_lanes
                self._placed_blocks.append(it)

        self._events = timed_events
        self.update()

    def current_time_y(self) -> float:
        now = datetime.now()
        now_min = now.hour * 60.0 + now.minute + now.second / 60.0
        return float(self.TOP_PAD) + (now_min / 60.0) * float(self.HOUR_HEIGHT)

    def first_focus_y(self) -> float:
        if self._date_str == dt_date.today().isoformat():
            return self.current_time_y()
        if self._placed_blocks:
            return float(self.TOP_PAD) + (self._placed_blocks[0]["start"] / 60.0) * float(self.HOUR_HEIGHT)
        return float(self.TOP_PAD) + 8.0 * float(self.HOUR_HEIGHT)

    def leaveEvent(self, event):
        if self._hover_time_min is not None:
            self._hover_time_min = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        if self._is_dark:
            bg_col = QColor("#18130F") if self._reading_mode else QColor("#0B0F1A")
            line_col = QColor(245, 158, 11, 30) if self._reading_mode else QColor(255, 255, 255, 22)
            half_col = QColor(245, 158, 11, 14) if self._reading_mode else QColor(255, 255, 255, 10)
            axis_txt = QColor("#A8957E") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#F5EBE0") if self._reading_mode else QColor("#F1F5F9")
            sub_col = QColor("#D6C5B0") if self._reading_mode else QColor("#94A3B8")
            pill_bg = QColor(99, 102, 241, 55)
            pill_txt = QColor("#C7D2FE")
        else:
            bg_col = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            line_col = QColor(180, 83, 9, 32) if self._reading_mode else QColor("#E2E8F0")
            half_col = QColor(180, 83, 9, 16) if self._reading_mode else QColor("#F1F5F9")
            axis_txt = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#1E293B")
            sub_col = QColor("#475569")
            pill_bg = QColor(99, 102, 241, 28)
            pill_txt = QColor("#4338CA")

        p.fillRect(self.rect(), bg_col)

        axis_font = QFont(p.font())
        axis_font.setPointSize(8)
        axis_font.setWeight(QFont.Weight.Medium)
        p.setFont(axis_font)

        left_x = float(self.AXIS_X)
        right_x = w - 8.0

        for hr in range(25):
            y = float(self.TOP_PAD) + hr * float(self.HOUR_HEIGHT)
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(left_x, y), QPointF(right_x, y))
            if hr < 24:
                p.setPen(axis_txt)
                lbl_rect = QRectF(2.0, y - 8.0, left_x - 10.0, 16.0)
                p.drawText(lbl_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{hr:02d}:00")
                hy = y + float(self.HOUR_HEIGHT) / 2.0
                p.setPen(QPen(half_col, 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(left_x, hy), QPointF(right_x, hy))

        p.setPen(QPen(line_col, 1.0))
        p.drawLine(QPointF(left_x, float(self.TOP_PAD)), QPointF(left_x, h - float(self.BOTTOM_PAD)))

        # Indicatore slot vuoto al passaggio del mouse
        if self._hover_time_min is not None:
            yh = float(self.TOP_PAD) + (self._hover_time_min / 60.0) * float(self.HOUR_HEIGHT)
            slot_h = float(self.HOUR_HEIGHT) / 2.0
            slot_rect = QRectF(left_x + 4.0, yh, max(40.0, right_x - left_x - 8.0), slot_h)
            p.setPen(QPen(QColor(99, 102, 241, 120), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(99, 102, 241, 18)))
            p.drawRoundedRect(slot_rect, 6.0, 6.0)
            p.setFont(axis_font)
            p.setPen(QColor("#818CF8"))
            p.drawText(
                slot_rect.adjusted(10.0, 0.0, -8.0, 0.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+ Aggiungi alle {self._hover_time_min // 60:02d}:{self._hover_time_min % 60:02d} (Evento o To-Do)",
            )

        # Blocchi evento
        self._hit_rects = []
        self._note_hit_rects = []
        avail_w = max(60.0, right_x - (left_x + 6.0))
        block_font_t = QFont(p.font())
        block_font_t.setPointSize(9)
        block_font_t.setWeight(QFont.Weight.DemiBold)
        block_font_m = QFont(p.font())
        block_font_m.setPointSize(8)
        note_font = QFont(p.font())
        note_font.setPointSize(8)
        note_font.setWeight(QFont.Weight.DemiBold)

        for item in self._placed_blocks:
            ev = item["ev"]
            ev_id = ev.get("id")
            y_top = float(self.TOP_PAD) + (item["start"] / 60.0) * float(self.HOUR_HEIGHT) + 1.0
            y_bot = float(self.TOP_PAD) + (item["vis_end"] / 60.0) * float(self.HOUR_HEIGHT) - 1.0
            bh = max(24.0, y_bot - y_top)

            lanes = item["lanes"]
            lane = item["lane"]
            gap = 5.0
            cw = (avail_w - gap * (lanes - 1)) / lanes
            bx = left_x + 6.0 + lane * (cw + gap)
            b_rect = QRectF(bx, y_top, cw, bh)
            self._hit_rects.append((b_rect, ev))

            accent = QColor(ev.get("color") or "#6366F1")
            if not accent.isValid():
                accent = QColor("#6366F1")
            is_selected = self._selected_event_id is not None and ev_id == self._selected_event_id

            fill_c = QColor(accent)
            fill_c.setAlpha(68 if is_selected else (48 if self._is_dark else 34))
            border_c = QColor(accent)
            border_c.setAlpha(240 if is_selected else (160 if self._is_dark else 140))

            p.setPen(QPen(border_c, 2.0 if is_selected else 1.0))
            p.setBrush(QBrush(fill_c))
            p.drawRoundedRect(b_rect, 8.0, 8.0)

            bar_rect = QRectF(bx, y_top + 1.5, 4.0, max(8.0, bh - 3.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(accent))
            p.drawRoundedRect(bar_rect, 2.0, 2.0)

            pad_in = 10.0
            inner_w = max(20.0, cw - pad_in - 8.0)
            summary = ev.get("summary") or "(Senza titolo)"
            time_txt = ev.get("start_time") or ""
            end_txt = ev.get("end_time") or ""
            time_span = f"{time_txt} - {end_txt}".strip(" -") if time_txt else ""

            meta_bits = [b for b in [time_span, ev.get("category") or "", ev.get("subject") or ""] if b]
            if ev.get("location"):
                meta_bits.append(f"📍 {ev['location']}")
            if (ev.get("recurrence") or "none") != "none":
                rec_map = {"daily": "🔁 Giorno", "weekly": "🔁 Sett.", "monthly": "🔁 Mese"}
                meta_bits.append(rec_map.get(ev.get("recurrence"), "🔁"))
            if ev.get("sync_status") == "synced":
                meta_bits.append("☁️ Google")

            linked_notes = self._notes_by_event.get(ev_id) or []

            if bh >= 42.0:
                p.setFont(block_font_m)
                p.setPen(sub_col)
                p.drawText(
                    QRectF(bx + pad_in, y_top + 4.0, inner_w, 15.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    QFontMetrics(block_font_m).elidedText(" · ".join(meta_bits), Qt.TextElideMode.ElideRight, int(inner_w)),
                )

                notes_row_h = 22.0 if (linked_notes and bh >= 66.0) else 0.0
                p.setFont(block_font_t)
                p.setPen(title_col)
                title_h = max(16.0, bh - 24.0 - notes_row_h)
                p.drawText(
                    QRectF(bx + pad_in, y_top + 20.0, inner_w, title_h),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                    summary,
                )

                if linked_notes and bh >= 66.0:
                    px = bx + pad_in
                    py = y_bot - 22.0
                    fm_n = QFontMetrics(note_font)
                    p.setFont(note_font)
                    for note in linked_notes[:3]:
                        n_title = (note.get("title") or "Nota").strip()
                        pill_label = f"📓 {fm_n.elidedText(n_title, Qt.TextElideMode.ElideRight, 130)}"
                        pw = min(inner_w - (px - (bx + pad_in)), float(fm_n.horizontalAdvance(pill_label) + 14))
                        if pw < 52.0:
                            break
                        n_rect = QRectF(px, py, pw, 18.0)
                        p.setPen(QPen(QColor(99, 102, 241, 130), 1.0))
                        p.setBrush(QBrush(pill_bg))
                        p.drawRoundedRect(n_rect, 6.0, 6.0)
                        p.setPen(pill_txt)
                        p.drawText(n_rect, Qt.AlignmentFlag.AlignCenter, pill_label)
                        self._note_hit_rects.append((n_rect, int(note["id"]), n_title))
                        px += pw + 6.0
            else:
                p.setFont(block_font_t)
                p.setPen(title_col)
                fm_t = QFontMetrics(block_font_t)
                suffix = f" (🔗{len(linked_notes)})" if linked_notes else ""
                elided = fm_t.elidedText(
                    f"{time_txt} · {summary}{suffix}".strip(" ·"), Qt.TextElideMode.ElideRight, int(inner_w)
                )
                p.drawText(
                    QRectF(bx + pad_in, y_top + 2.0, inner_w, bh - 4.0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    elided,
                )

        # Linea rossa ora corrente
        is_today = self._date_str == dt_date.today().isoformat()
        y_now = self.current_time_y()
        red_col = QColor("#EF4444") if is_today else QColor(239, 68, 68, 125)
        pen_style = Qt.PenStyle.SolidLine if is_today else Qt.PenStyle.DashLine
        p.setPen(QPen(red_col, 2.0 if is_today else 1.4, pen_style))
        p.drawLine(QPointF(left_x, y_now), QPointF(right_x, y_now))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(red_col))
        p.drawEllipse(QPointF(left_x, y_now), 4.0 if is_today else 3.0, 4.0 if is_today else 3.0)

        now_txt = datetime.now().strftime("%H:%M")
        pill_r = QRectF(3.0, y_now - 8.5, left_x - 8.0, 17.0)
        p.setBrush(QBrush(QColor("#EF4444") if is_today else QColor(239, 68, 68, 180)))
        p.drawRoundedRect(pill_r, 4.0, 4.0)
        pill_font = QFont(p.font())
        pill_font.setPointSize(7)
        pill_font.setWeight(QFont.Weight.Bold)
        p.setFont(pill_font)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, now_txt)

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())
        for nr, _nid, ntitle in reversed(self._note_hit_rects):
            if nr.contains(pos):
                if self._hover_time_min is not None:
                    self._hover_time_min = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(f"Apri nota '{ntitle}' in FlowNotes")
                return
        for r, ev in reversed(self._hit_rects):
            if r.contains(pos):
                if self._hover_time_min is not None:
                    self._hover_time_min = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                loc = f"\n📍 {ev['location']}" if ev.get("location") else ""
                self.setToolTip(f"{ev.get('time', '')} · {ev.get('summary', '')}{loc}\n(Clicca per modificare o collegare note)")
                return
        if float(self.TOP_PAD) <= pos.y() <= float(self.height() - self.BOTTOM_PAD) and pos.x() >= float(self.AXIS_X):
            snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Clicca per aggiungere un Evento o To-Do alle {hhmm}")
            if self._hover_time_min != snapped:
                self._hover_time_min = snapped
                self.update()
            return
        if self._hover_time_min is not None:
            self._hover_time_min = None
            self.update()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = QPointF(event.position())
            for nr, nid, _ntitle in reversed(self._note_hit_rects):
                if nr.contains(pos):
                    self.note_clicked.emit(nid)
                    return
            for r, ev in reversed(self._hit_rects):
                if r.contains(pos):
                    self.event_clicked.emit(ev)
                    return
            _snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.empty_slot_clicked.emit(self._date_str, hhmm)
            return
        super().mousePressEvent(event)


class _ClickableLabel(QLabel):
    """QLabel word-wrappable cliccabile per mostrare titoli To-Do lunghi su più righe senza tagli."""

    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


class FlowTimeTodoSidebar(QFrame):
    """Colonna laterale destra (~208px) dedicata a To-Do senza orario e Note, raggruppati per giornata."""

    date_selected = pyqtSignal(str)
    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    todo_toggled = pyqtSignal(int)
    add_todo_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFixedWidth(208)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._selected_date_str: str = dt_date.today().isoformat()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 10, 9, 8)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title_lbl = _make_label("☑️ Impegni & Note", "CardTitle")
        title_lbl.setStyleSheet("font-size: 13px; font-weight: 700; background: transparent;")
        hdr.addWidget(title_lbl, 1)

        add_btn = _make_button("+ To-Do", "primary")
        add_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
        add_btn.setToolTip("Aggiungi un impegno senza orario nella data selezionata")
        add_btn.clicked.connect(lambda: self.add_todo_requested.emit(self._selected_date_str))
        hdr.addWidget(add_btn)
        layout.addLayout(hdr)

        self.summary_badge = _make_label("", "CardMeta")
        layout.addWidget(self.summary_badge)

        self.scroll_list = ScrollList("Nessun impegno o nota nella settimana.")
        layout.addWidget(self.scroll_list, 1)

    def set_sidebar_data(
        self,
        selected_date_str: str,
        events_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_event: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ):
        self._selected_date_str = selected_date_str or dt_date.today().isoformat()
        self.scroll_list.clear()

        notes_by_ev_map = notes_by_event or {}
        all_dates = sorted(set(events_by_date.keys()) | set(notes_by_date.keys()))
        ordered_dates = [self._selected_date_str]
        for ds in all_dates:
            if ds == self._selected_date_str:
                continue
            evs = events_by_date.get(ds) or []
            nts = notes_by_date.get(ds) or []
            has_items = any(e.get("item_type") == "todo" or e.get("is_all_day") for e in evs) or bool(nts)
            if has_items:
                ordered_dates.append(ds)

        weekdays_up = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]
        months_up = ["GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET", "OTT", "NOV", "DIC"]
        today_str = dt_date.today().isoformat()

        pending_todos = 0
        total_notes = 0

        for ds in ordered_dates:
            evs = events_by_date.get(ds) or []
            todos = [e for e in evs if e.get("item_type") == "todo"]
            all_day = [e for e in evs if e.get("item_type") != "todo" and e.get("is_all_day")]
            d_notes = notes_by_date.get(ds) or []

            pending_todos += sum(1 for t in todos if not t.get("is_completed"))
            total_notes += len(d_notes)

            day_sec = QWidget()
            day_sec.setStyleSheet("background: transparent;")
            day_sec.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            dl = QVBoxLayout(day_sec)
            dl.setContentsMargins(0, 0, 0, 4)
            dl.setSpacing(4)

            try:
                d_obj = dt_date.fromisoformat(ds)
                label_txt = f"{weekdays_up[d_obj.weekday()]} {d_obj.day} {months_up[d_obj.month - 1]}"
            except Exception:
                label_txt = ds
            if ds == today_str:
                label_txt = f"OGGI · {label_txt}"

            hdr_frame = QFrame()
            hdr_frame.setObjectName("FTDayGroupHeaderSelected" if ds == self._selected_date_str else "FTDayGroupHeader")
            hdr_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            hl = QHBoxLayout(hdr_frame)
            hl.setContentsMargins(6, 3, 6, 3)
            hl.setSpacing(4)

            day_btn = QPushButton(label_txt)
            day_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            day_btn.setStyleSheet(
                "border: none; background: transparent; text-align: left; "
                "font-size: 10.5px; font-weight: 700; padding: 1px 2px;"
            )
            day_btn.clicked.connect(lambda _c=False, target_ds=ds: self.date_selected.emit(target_ds))
            hl.addWidget(day_btn, 1)

            cnt_items = len(todos) + len(all_day) + len(d_notes)
            if cnt_items > 0:
                hl.addWidget(_make_label(str(cnt_items), "BadgeMuted"))
            dl.addWidget(hdr_frame)

            for ev in todos:
                ev_id = int(ev.get("local_id") or ev.get("id"))
                is_done = bool(ev.get("is_completed"))
                pill = QFrame()
                pill.setObjectName("FTTodoPill")
                pill.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                pl = QVBoxLayout(pill)
                pl.setContentsMargins(7, 5, 7, 5)
                pl.setSpacing(3)

                row = QHBoxLayout()
                row.setSpacing(5)
                cb = QCheckBox("")
                cb.setChecked(is_done)
                cb.setToolTip("Segna impegno come completato / da fare")
                cb.clicked.connect(lambda _c=False, eid=ev_id: self.todo_toggled.emit(eid))
                row.addWidget(cb, 0, Qt.AlignmentFlag.AlignTop)

                accent = ev.get("color") or "#6366F1"
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {accent}; font-size: 10px; background: transparent; padding-top: 2px;")
                row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

                t_lbl = _ClickableLabel(ev.get("summary") or "Impegno")
                t_lbl.setWordWrap(True)
                t_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                t_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                fnt = t_lbl.font()
                fnt.setStrikeOut(is_done)
                t_lbl.setFont(fnt)
                t_lbl.setStyleSheet(
                    "font-size: 11.5px; font-weight: 600; background: transparent;"
                    + (" color: #64748B;" if is_done else "")
                )
                t_lbl.setToolTip("Clicca per modificare o collegare note")
                t_lbl.clicked.connect(lambda e=ev: self.event_clicked.emit(e))
                row.addWidget(t_lbl, 1)
                pl.addLayout(row)

                linked = notes_by_ev_map.get(ev_id) or ev.get("linked_notes") or []
                for note in linked[:2]:
                    nid = int(note["id"])
                    nb = QPushButton(f"📓 {(note.get('title') or 'Nota')[:19]}")
                    nb.setObjectName("FTNotePillBtn")
                    nb.setCursor(Qt.CursorShape.PointingHandCursor)
                    nb.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
                    nb.clicked.connect(lambda _c=False, target_id=nid: self.note_clicked.emit(target_id))
                    pl.addWidget(nb)

                dl.addWidget(pill)

            for ev in all_day:
                btn = QPushButton(f"📌 {(ev.get('summary') or 'Evento')[:22]}")
                btn.setObjectName("FTNotePillBtn")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(ev.get("summary") or "Evento")
                btn.clicked.connect(lambda _c=False, e=ev: self.event_clicked.emit(e))
                dl.addWidget(btn)

            for note in d_notes[:5]:
                nid = int(note["id"])
                nb = QPushButton(f"📓 {(note.get('title') or 'Nota')[:20]}")
                nb.setObjectName("FTNotePillBtn")
                nb.setCursor(Qt.CursorShape.PointingHandCursor)
                nb.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
                nb.clicked.connect(lambda _c=False, target_id=nid: self.note_clicked.emit(target_id))
                dl.addWidget(nb)

            if not todos and not all_day and not d_notes:
                empty_day = _make_label("Nessun To-Do o nota.", "NoteMeta", wrap=True)
                empty_day.setStyleSheet("padding: 2px 4px; background: transparent;")
                dl.addWidget(empty_day)

            self.scroll_list.add_card(day_sec)

        self.summary_badge.setText(f"{pending_todos} da fare · {total_notes} note nella settimana")


class FlowTimeDayTimeline(QFrame):
    """Timeline giornaliera verticale con intestazione giorno in stile Google Calendar."""

    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)
    todo_toggled = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._date_str: str = dt_date.today().isoformat()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(10)

        self.day_num_badge = QLabel(str(dt_date.today().day))
        self.day_num_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.day_num_badge.setFixedSize(34, 34)
        hdr.addWidget(self.day_num_badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.day_weekday_lbl = _make_label("", "CardMeta")
        self.day_weekday_lbl.setStyleSheet("font-size: 10px; font-weight: 700; letter-spacing: 0.8px; background: transparent;")
        title_col.addWidget(self.day_weekday_lbl)

        self.day_title_lbl = _make_label("Agenda & Timeline", "CardTitle")
        self.day_title_lbl.setStyleSheet("font-size: 15px; font-weight: 700; background: transparent;")
        title_col.addWidget(self.day_title_lbl)
        hdr.addLayout(title_col)

        self.day_stats_lbl = _make_label("", "BadgeAccent")
        hdr.addWidget(self.day_stats_lbl)
        hdr.addStretch(1)

        self.now_badge = QLabel(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.now_badge.setStyleSheet(
            "background-color: rgba(239, 68, 68, 0.14); color: #EF4444; "
            "border: 1px solid rgba(239, 68, 68, 0.40); border-radius: 7px; "
            "padding: 2px 8px; font-size: 11px; font-weight: 700;"
        )
        hdr.addWidget(self.now_badge)
        layout.addLayout(hdr)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.canvas = _DayTimelineCanvas(self)
        self.canvas.event_clicked.connect(self.event_clicked.emit)
        self.canvas.note_clicked.connect(self.note_clicked.emit)
        self.canvas.empty_slot_clicked.connect(self.empty_slot_clicked.emit)
        self.scroll_area.setWidget(self.canvas)
        layout.addWidget(self.scroll_area, 1)

        self._now_timer = QTimer(self)
        self._now_timer.timeout.connect(self._on_tick)
        self._now_timer.start(30000)

    def _on_tick(self):
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        if self.isVisible():
            self.canvas.update()

    def set_day_data(
        self,
        date_str: str,
        events: List[Dict[str, Any]],
        day_notes: List[Dict[str, Any]],
        notes_by_event: Dict[int, List[Dict[str, Any]]],
        is_dark: bool,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        self._date_str = date_str or dt_date.today().isoformat()
        try:
            d_obj = dt_date.fromisoformat(self._date_str)
        except Exception:
            d_obj = dt_date.today()

        weekdays_up = ["LUNEDÌ", "MARTEDÌ", "MERCOLEDÌ", "GIOVEDÌ", "VENERDÌ", "SABATO", "DOMENICA"]
        is_today = d_obj == dt_date.today()
        self.day_weekday_lbl.setText(weekdays_up[d_obj.weekday()])
        self.day_num_badge.setText(str(d_obj.day))
        if is_today:
            self.day_num_badge.setStyleSheet(
                "background-color: #6366F1; color: #FFFFFF; border: 2px solid #EF4444; "
                "border-radius: 17px; font-size: 15px; font-weight: 700;"
            )
        else:
            self.day_num_badge.setStyleSheet(
                "background-color: #6366F1; color: #FFFFFF; border: none; "
                "border-radius: 17px; font-size: 15px; font-weight: 700;"
            )

        timed = [e for e in events if e.get("item_type") != "todo" and not e.get("is_all_day")]
        self.canvas.set_data(
            date_str=self._date_str,
            timed_events=timed,
            is_dark=is_dark,
            reading_mode=reading_mode,
            notes_by_event=notes_by_event,
            selected_event_id=selected_event_id,
        )
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")

    def scroll_to_focus(self):
        y = int(self.canvas.first_focus_y())
        vp_h = max(160, self.scroll_area.viewport().height())
        sb = self.scroll_area.verticalScrollBar()
        target = max(sb.minimum(), min(sb.maximum(), y - vp_h // 3))
        sb.setValue(target)


class _WeekTimelineCanvas(QWidget):
    """Canvas a 7 colonne orarie (Lunedì–Domenica, 00:00–23:59) con linea ora live e click su slot vuoti."""

    event_clicked = pyqtSignal(dict)
    empty_slot_clicked = pyqtSignal(str, str)

    HOUR_HEIGHT = 48
    TOP_PAD = 8
    BOTTOM_PAD = 12
    AXIS_X = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self._monday: dt_date = dt_date.today() - timedelta(days=dt_date.today().weekday())
        self._selected_date_str: str = dt_date.today().isoformat()
        self._selected_event_id: Optional[int] = None
        self._placed_by_col: Dict[int, List[Dict[str, Any]]] = {}
        self._hit_rects: List[tuple] = []
        self._hover_slot: Optional[tuple] = None  # (col_idx, date_str, snapped_min)
        self._is_dark: bool = True
        self._reading_mode: bool = False
        self.setMouseTracking(True)
        self.setFixedHeight(24 * self.HOUR_HEIGHT + self.TOP_PAD + self.BOTTOM_PAD)

    def _y_to_snapped_hhmm(self, y: float) -> tuple:
        raw_mins = ((y - float(self.TOP_PAD)) / float(self.HOUR_HEIGHT)) * 60.0
        snapped = int(round(raw_mins / 30.0) * 30)
        snapped = max(0, min(1410, snapped))
        return snapped, f"{snapped // 60:02d}:{snapped % 60:02d}"

    def set_data(
        self,
        monday_date: dt_date,
        selected_date_str: str,
        timed_by_date: Dict[str, List[Dict[str, Any]]],
        is_dark: bool,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        self._monday = monday_date
        self._selected_date_str = selected_date_str
        self._is_dark = bool(is_dark)
        self._reading_mode = bool(reading_mode)
        self._selected_event_id = selected_event_id
        self._placed_by_col = {}

        for col_idx in range(7):
            d_str = (monday_date + timedelta(days=col_idx)).isoformat()
            evs = timed_by_date.get(d_str) or []
            items = []
            for ev in evs:
                s_min = _DayTimelineCanvas._parse_hhmm(ev.get("start_time") or "")
                if s_min is None:
                    s_min = 9 * 60
                e_min = _DayTimelineCanvas._parse_hhmm(ev.get("end_time") or "")
                if e_min is None or e_min <= s_min:
                    e_min = min(1440, s_min + 60)
                vis_end = max(s_min + 30, e_min)
                items.append({"ev": ev, "start": s_min, "end": e_min, "vis_end": vis_end})
            items.sort(key=lambda x: (x["start"], x["vis_end"]))

            placed: List[Dict[str, Any]] = []
            clusters: List[List[Dict[str, Any]]] = []
            cur: List[Dict[str, Any]] = []
            c_end = -1
            for it in items:
                if not cur or it["start"] < c_end:
                    cur.append(it)
                    c_end = max(c_end, it["vis_end"])
                else:
                    clusters.append(cur)
                    cur = [it]
                    c_end = it["vis_end"]
            if cur:
                clusters.append(cur)

            for cl in clusters:
                lane_ends: List[int] = []
                for it in cl:
                    p_lane = None
                    for li, lend in enumerate(lane_ends):
                        if it["start"] >= lend:
                            p_lane = li
                            lane_ends[li] = it["vis_end"]
                            break
                    if p_lane is None:
                        p_lane = len(lane_ends)
                        lane_ends.append(it["vis_end"])
                    it["lane"] = p_lane
                tot_lanes = max(1, len(lane_ends))
                for it in cl:
                    it["lanes"] = tot_lanes
                    placed.append(it)
            self._placed_by_col[col_idx] = placed

        self.update()

    def focus_y(self) -> float:
        today_d = dt_date.today()
        if self._monday <= today_d <= (self._monday + timedelta(days=6)):
            now = datetime.now()
            return float(self.TOP_PAD) + ((now.hour * 60.0 + now.minute) / 60.0) * float(self.HOUR_HEIGHT)
        return float(self.TOP_PAD) + 8.0 * float(self.HOUR_HEIGHT)

    def leaveEvent(self, event):
        if self._hover_slot is not None:
            self._hover_slot = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        if self._is_dark:
            bg_col = QColor("#18130F") if self._reading_mode else QColor("#0B0F1A")
            line_col = QColor(245, 158, 11, 26) if self._reading_mode else QColor(255, 255, 255, 20)
            half_col = QColor(245, 158, 11, 12) if self._reading_mode else QColor(255, 255, 255, 8)
            axis_txt = QColor("#A8957E") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#F5EBE0") if self._reading_mode else QColor("#F1F5F9")
            sub_col = QColor("#D6C5B0") if self._reading_mode else QColor("#94A3B8")
        else:
            bg_col = QColor("#FBF5E6") if self._reading_mode else QColor("#FFFFFF")
            line_col = QColor(180, 83, 9, 28) if self._reading_mode else QColor("#E2E8F0")
            half_col = QColor(180, 83, 9, 14) if self._reading_mode else QColor("#F1F5F9")
            axis_txt = QColor("#6E5B47") if self._reading_mode else QColor("#64748B")
            title_col = QColor("#1E293B")
            sub_col = QColor("#475569")

        p.fillRect(self.rect(), bg_col)

        left_x = float(self.AXIS_X)
        right_x = w - 2.0
        col_w = max(40.0, (right_x - left_x) / 7.0)
        today_str = dt_date.today().isoformat()
        today_col_idx: Optional[int] = None

        # Sfondo colonna Oggi / Selezionato + divisori verticali
        for col_idx in range(7):
            d_str = (self._monday + timedelta(days=col_idx)).isoformat()
            cx = left_x + col_idx * col_w
            if d_str == today_str:
                today_col_idx = col_idx
                p.fillRect(QRectF(cx, float(self.TOP_PAD), col_w, h - float(self.TOP_PAD + self.BOTTOM_PAD)), QColor(239, 68, 68, 14))
            elif d_str == self._selected_date_str:
                p.fillRect(QRectF(cx, float(self.TOP_PAD), col_w, h - float(self.TOP_PAD + self.BOTTOM_PAD)), QColor(99, 102, 241, 14))

        axis_font = QFont(p.font())
        axis_font.setPointSize(8)
        axis_font.setWeight(QFont.Weight.Medium)
        p.setFont(axis_font)

        for hr in range(25):
            y = float(self.TOP_PAD) + hr * float(self.HOUR_HEIGHT)
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(left_x, y), QPointF(right_x, y))
            if hr < 24:
                p.setPen(axis_txt)
                p.drawText(QRectF(2.0, y - 8.0, left_x - 8.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{hr:02d}:00")
                hy = y + float(self.HOUR_HEIGHT) / 2.0
                p.setPen(QPen(half_col, 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(left_x, hy), QPointF(right_x, hy))

        for col_idx in range(8):
            vx = left_x + col_idx * col_w
            p.setPen(QPen(line_col, 1.0))
            p.drawLine(QPointF(vx, float(self.TOP_PAD)), QPointF(vx, h - float(self.BOTTOM_PAD)))

        # Hover slot preview
        if self._hover_slot is not None:
            h_col, _h_ds, h_min = self._hover_slot
            hx = left_x + h_col * col_w + 2.0
            hy = float(self.TOP_PAD) + (h_min / 60.0) * float(self.HOUR_HEIGHT)
            hr_rect = QRectF(hx, hy, max(20.0, col_w - 4.0), float(self.HOUR_HEIGHT) / 2.0)
            p.setPen(QPen(QColor(99, 102, 241, 140), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(99, 102, 241, 24)))
            p.drawRoundedRect(hr_rect, 5.0, 5.0)
            p.setFont(axis_font)
            p.setPen(QColor("#818CF8"))
            p.drawText(hr_rect, Qt.AlignmentFlag.AlignCenter, f"+ {h_min // 60:02d}:{h_min % 60:02d}")

        # Blocchi evento per ciascuna colonna giorno
        self._hit_rects = []
        block_font_t = QFont(p.font())
        block_font_t.setPointSize(8)
        block_font_t.setWeight(QFont.Weight.DemiBold)
        block_font_m = QFont(p.font())
        block_font_m.setPointSize(7)

        for col_idx in range(7):
            cx = left_x + col_idx * col_w
            avail_w = max(24.0, col_w - 6.0)
            for item in self._placed_by_col.get(col_idx, []):
                ev = item["ev"]
                y_top = float(self.TOP_PAD) + (item["start"] / 60.0) * float(self.HOUR_HEIGHT) + 1.0
                y_bot = float(self.TOP_PAD) + (item["vis_end"] / 60.0) * float(self.HOUR_HEIGHT) - 1.0
                bh = max(20.0, y_bot - y_top)

                lanes = item["lanes"]
                lane = item["lane"]
                gap = 2.0
                cw = (avail_w - gap * (lanes - 1)) / lanes
                bx = cx + 3.0 + lane * (cw + gap)
                b_rect = QRectF(bx, y_top, cw, bh)
                self._hit_rects.append((b_rect, ev))

                accent = QColor(ev.get("color") or "#6366F1")
                if not accent.isValid():
                    accent = QColor("#6366F1")
                is_selected = self._selected_event_id is not None and ev.get("id") == self._selected_event_id

                fill_c = QColor(accent)
                fill_c.setAlpha(72 if is_selected else (50 if self._is_dark else 34))
                border_c = QColor(accent)
                border_c.setAlpha(240 if is_selected else (165 if self._is_dark else 140))

                p.setPen(QPen(border_c, 2.0 if is_selected else 1.0))
                p.setBrush(QBrush(fill_c))
                p.drawRoundedRect(b_rect, 6.0, 6.0)

                bar_rect = QRectF(bx, y_top + 1.5, 3.0, max(6.0, bh - 3.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(accent))
                p.drawRoundedRect(bar_rect, 1.5, 1.5)

                inner = QRectF(bx + 6.0, y_top + 2.0, max(10.0, cw - 9.0), bh - 4.0)
                summary = ev.get("summary") or "(Senza titolo)"
                time_txt = ev.get("start_time") or ""
                if bh >= 34.0 and time_txt:
                    p.setFont(block_font_m)
                    p.setPen(sub_col)
                    p.drawText(QRectF(inner.x(), inner.y(), inner.width(), 12.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, time_txt)
                    p.setFont(block_font_t)
                    p.setPen(title_col)
                    p.drawText(
                        QRectF(inner.x(), inner.y() + 12.0, inner.width(), max(12.0, inner.height() - 12.0)),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                        summary,
                    )
                else:
                    p.setFont(block_font_t)
                    p.setPen(title_col)
                    elided = QFontMetrics(block_font_t).elidedText(summary, Qt.TextElideMode.ElideRight, int(inner.width()))
                    p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        # Linea rossa ora live (evidenziata sulla colonna di Oggi se visibile)
        now = datetime.now()
        y_now = float(self.TOP_PAD) + ((now.hour * 60.0 + now.minute + now.second / 60.0) / 60.0) * float(self.HOUR_HEIGHT)
        p.setPen(QPen(QColor(239, 68, 68, 90 if today_col_idx is not None else 75), 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(left_x, y_now), QPointF(right_x, y_now))
        if today_col_idx is not None:
            tx = left_x + today_col_idx * col_w
            p.setPen(QPen(QColor("#EF4444"), 2.2, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(tx, y_now), QPointF(tx + col_w, y_now))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#EF4444")))
            p.drawEllipse(QPointF(tx, y_now), 4.0, 4.0)

        pill_r = QRectF(2.0, y_now - 8.5, left_x - 6.0, 17.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#EF4444")))
        p.drawRoundedRect(pill_r, 4.0, 4.0)
        pf = QFont(p.font())
        pf.setPointSize(7)
        pf.setWeight(QFont.Weight.Bold)
        p.setFont(pf)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, now.strftime("%H:%M"))

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())
        for r, ev in reversed(self._hit_rects):
            if r.contains(pos):
                if self._hover_slot is not None:
                    self._hover_slot = None
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                loc = f"\n📍 {ev['location']}" if ev.get("location") else ""
                self.setToolTip(f"{ev.get('time', '')} · {ev.get('summary', '')}{loc}\n(Clicca per modificare o collegare note)")
                return
        if pos.x() >= float(self.AXIS_X) and float(self.TOP_PAD) <= pos.y() <= float(self.height() - self.BOTTOM_PAD):
            col_w = max(40.0, (float(self.width()) - 2.0 - float(self.AXIS_X)) / 7.0)
            col_idx = max(0, min(6, int((pos.x() - float(self.AXIS_X)) / col_w)))
            d_obj = self._monday + timedelta(days=col_idx)
            snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            slot = (col_idx, d_obj.isoformat(), snapped)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Clicca per aggiungere in {d_obj.strftime('%d/%m')} alle {hhmm}")
            if self._hover_slot != slot:
                self._hover_slot = slot
                self.update()
            return
        if self._hover_slot is not None:
            self._hover_slot = None
            self.update()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = QPointF(event.position())
            for r, ev in reversed(self._hit_rects):
                if r.contains(pos):
                    self.event_clicked.emit(ev)
                    return
            col_w = max(40.0, (float(self.width()) - 2.0 - float(self.AXIS_X)) / 7.0)
            col_idx = max(0, min(6, int((max(float(self.AXIS_X), pos.x()) - float(self.AXIS_X)) / col_w)))
            d_str = (self._monday + timedelta(days=col_idx)).isoformat()
            _snapped, hhmm = self._y_to_snapped_hhmm(pos.y())
            self.empty_slot_clicked.emit(d_str, hhmm)
            return
        super().mousePressEvent(event)


class FlowTimeWeekView(QFrame):
    """Vista Settimanale a 7 colonne orarie (00:00–23:59) con striscia To-Do e oggi cerchiato in rosso."""

    date_selected = pyqtSignal(str)
    open_day_requested = pyqtSignal(str)
    event_clicked = pyqtSignal(dict)
    note_clicked = pyqtSignal(int)
    empty_slot_clicked = pyqtSignal(str, str)
    todo_toggled = pyqtSignal(int)
    prev_week_clicked = pyqtSignal()
    next_week_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        prev_btn = _make_button("◀")
        prev_btn.setStyleSheet("min-height: 24px; max-height: 24px; min-width: 26px; max-width: 26px; padding: 0px; font-size: 11px;")
        prev_btn.setToolTip("Settimana precedente")
        prev_btn.clicked.connect(self.prev_week_clicked.emit)
        nav.addWidget(prev_btn)

        next_btn = _make_button("▶")
        next_btn.setStyleSheet("min-height: 24px; max-height: 24px; min-width: 26px; max-width: 26px; padding: 0px; font-size: 11px;")
        next_btn.setToolTip("Settimana successiva")
        next_btn.clicked.connect(self.next_week_clicked.emit)
        nav.addWidget(next_btn)

        self.week_range_lbl = _make_label("Settimana", "CardTitle")
        self.week_range_lbl.setStyleSheet("font-size: 14px; font-weight: 700; background: transparent;")
        nav.addWidget(self.week_range_lbl)

        nav.addStretch(1)
        self.week_stats_lbl = _make_label("", "BadgeMuted")
        nav.addWidget(self.week_stats_lbl)

        self.now_badge = QLabel(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.now_badge.setStyleSheet(
            "background-color: rgba(239, 68, 68, 0.14); color: #EF4444; "
            "border: 1px solid rgba(239, 68, 68, 0.40); border-radius: 7px; "
            "padding: 2px 8px; font-size: 11px; font-weight: 700;"
        )
        nav.addWidget(self.now_badge)
        layout.addLayout(nav)

        # Barra intestazioni 7 giorni in stile Google Calendar (altezza bloccata 64 px)
        self.hdr_strip = QFrame()
        self.hdr_strip.setObjectName("FTWeekHeaderBar")
        self.hdr_strip.setFixedHeight(64)
        self.hdr_strip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hdr_cols_layout = QHBoxLayout(self.hdr_strip)
        self.hdr_cols_layout.setContentsMargins(_WeekTimelineCanvas.AXIS_X, 0, 9, 0)
        self.hdr_cols_layout.setSpacing(0)
        layout.addWidget(self.hdr_strip)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.week_canvas = _WeekTimelineCanvas(self)
        self.week_canvas.event_clicked.connect(self.event_clicked.emit)
        self.week_canvas.empty_slot_clicked.connect(self.empty_slot_clicked.emit)
        self.scroll_area.setWidget(self.week_canvas)
        layout.addWidget(self.scroll_area, 1)

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(30000)

    def _on_tick(self):
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        if self.isVisible():
            self.week_canvas.update()

    def scroll_to_focus(self):
        y = int(self.week_canvas.focus_y())
        vp_h = max(160, self.scroll_area.viewport().height())
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(max(sb.minimum(), min(sb.maximum(), y - vp_h // 3)))

    def set_week_data(
        self,
        selected_date_str: str,
        events_by_date: Dict[str, List[Dict[str, Any]]],
        notes_by_date: Dict[str, List[Dict[str, Any]]],
        is_dark: bool = True,
        reading_mode: bool = False,
        selected_event_id: Optional[int] = None,
    ):
        while self.hdr_cols_layout.count():
            item = self.hdr_cols_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        try:
            sel_d = dt_date.fromisoformat(selected_date_str)
        except Exception:
            sel_d = dt_date.today()

        monday = sel_d - timedelta(days=sel_d.weekday())
        sunday = monday + timedelta(days=6)
        months_full = [
            "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
            "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
        ]
        months_short = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]
        weekdays_up = ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"]

        if monday.month == sunday.month:
            range_txt = f"{months_full[monday.month - 1]} {monday.year} · {monday.day}–{sunday.day}"
        else:
            range_txt = f"{monday.day} {months_short[monday.month - 1]} – {sunday.day} {months_short[sunday.month - 1]} {sunday.year}"
        self.week_range_lbl.setText(range_txt)

        today_d = dt_date.today()
        total_events = 0
        total_notes = 0
        timed_by_date: Dict[str, List[Dict[str, Any]]] = {}

        for offset in range(7):
            d_cur = monday + timedelta(days=offset)
            d_str = d_cur.isoformat()
            is_today = d_cur == today_d
            is_selected = d_cur == sel_d

            day_events = events_by_date.get(d_str) or []
            day_notes = notes_by_date.get(d_str) or []
            total_events += len(day_events)
            total_notes += len(day_notes)

            timed_by_date[d_str] = [e for e in day_events if e.get("item_type") != "todo" and not e.get("is_all_day")]
            todos = [e for e in day_events if e.get("item_type") == "todo"]

            col_hdr = QFrame()
            col_hdr.setObjectName("FTWeekHeaderCell")
            if is_today:
                col_hdr.setStyleSheet(
                    "QFrame#FTWeekHeaderCell { background-color: rgba(239, 68, 68, 0.08); "
                    "border: none; border-top-left-radius: 6px; border-top-right-radius: 6px; }"
                )
            col_l = QVBoxLayout(col_hdr)
            col_l.setContentsMargins(2, 4, 2, 4)
            col_l.setSpacing(2)
            col_l.setAlignment(Qt.AlignmentFlag.AlignHCenter)

            # Riga 1: Sigla giorno in maiuscolo stile Google Calendar
            wd_lbl = QLabel(weekdays_up[offset])
            wd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wd_color = "#EF4444" if is_today else ("#818CF8" if (is_selected and is_dark) else ("#4F46E5" if is_selected else ("#94A3B8" if is_dark else "#64748B")))
            wd_lbl.setStyleSheet(
                f"color: {wd_color}; font-size: 10px; font-weight: 700; letter-spacing: 0.8px; background: transparent;"
            )
            col_l.addWidget(wd_lbl)

            # Riga 2: Numero del giorno (nessun cerchio rosso per oggi; solo pill piena quando selezionato)
            num_btn = QPushButton(str(d_cur.day))
            num_btn.setFixedSize(34, 34)
            num_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            num_btn.setToolTip("Clicca per selezionare · Riclicca per aprire la vista Giorno")
            if is_selected:
                num_btn.setStyleSheet(
                    "background-color: #6366F1; color: #FFFFFF; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 700; padding: 0px;"
                )
            elif is_today:
                num_btn.setStyleSheet(
                    "background-color: transparent; color: #EF4444; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 700; padding: 0px;"
                )
            else:
                txt_col = "#F1F5F9" if is_dark else "#1E293B"
                num_btn.setStyleSheet(
                    f"background-color: transparent; color: {txt_col}; border: none; "
                    "border-radius: 17px; font-size: 16px; font-weight: 600; padding: 0px;"
                )
            if is_selected:
                num_btn.clicked.connect(lambda _c=False, ds=d_str: self.open_day_requested.emit(ds))
            else:
                num_btn.clicked.connect(lambda _c=False, ds=d_str: self.date_selected.emit(ds))
            col_l.addWidget(num_btn, 0, Qt.AlignmentFlag.AlignHCenter)

            # Riga 3: Micro-indicatore fisso (12 px) che non espande mai l'intestazione
            parts = []
            if todos:
                parts.append(f"☑️{len(todos)}")
            if day_notes:
                parts.append(f"📓{len(day_notes)}")
            hint_lbl = QLabel(" ".join(parts))
            hint_lbl.setFixedHeight(12)
            hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint_lbl.setStyleSheet(
                f"color: {'#94A3B8' if is_dark else '#64748B'}; font-size: 9px; font-weight: 600; background: transparent;"
            )
            col_l.addWidget(hint_lbl)

            self.hdr_cols_layout.addWidget(col_hdr, 1)

        self.week_canvas.set_data(
            monday_date=monday,
            selected_date_str=sel_d.isoformat(),
            timed_by_date=timed_by_date,
            is_dark=is_dark,
            reading_mode=reading_mode,
            selected_event_id=selected_event_id,
        )
        self.now_badge.setText(f"🔴 {datetime.now().strftime('%H:%M')}")
        self.week_stats_lbl.setText(
            f"{total_events} {'impegno' if total_events == 1 else 'impegni'} · "
            f"{total_notes} {'nota' if total_notes == 1 else 'note'} nella settimana"
        )


class PomodoroRingWidget(QWidget):
    """Anello circolare QPainter per il Pomodoro Focus Timer adattato al tema attivo."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._progress: float = 1.0  # 0.0 -> 1.0
        self._time_text: str = "25:00"
        self._mode_text: str = "FOCUS"
        self._accent_color = QColor("#6366F1")
        self._track_color = QColor(148, 163, 184, 55)
        self._text_color = QColor("#F8FAFC")
        self._muted_color = QColor("#94A3B8")
        self.setFixedSize(196, 196)

    def set_theme_colors(self, accent: str, track: str, text: str, muted: str):
        self._accent_color = QColor(accent or "#6366F1")
        self._track_color = QColor(track) if track else QColor(148, 163, 184, 55)
        self._text_color = QColor(text or "#F8FAFC")
        self._muted_color = QColor(muted or "#94A3B8")
        self.update()

    def set_state(self, progress: float, time_text: str, mode_text: str):
        self._progress = max(0.0, min(1.0, float(progress)))
        self._time_text = time_text
        self._mode_text = mode_text
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(14, 14, self.width() - 28, self.height() - 28)
        track_pen = QPen(self._track_color, 10.0)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(track_pen)
        p.drawEllipse(rect)

        if self._progress > 0.001:
            arc_pen = QPen(self._accent_color, 10.0)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(arc_pen)
            span_angle = int(-self._progress * 360 * 16)
            p.drawArc(rect, 90 * 16, span_angle)

        p.setPen(QPen(self._text_color))
        tf = QFont(self.font())
        tf.setPointSize(26)
        tf.setWeight(QFont.Weight.Bold)
        p.setFont(tf)
        p.drawText(rect.adjusted(0, -10, 0, -10), Qt.AlignmentFlag.AlignCenter, self._time_text)

        p.setPen(QPen(self._muted_color))
        mf = QFont(self.font())
        mf.setPointSize(9)
        mf.setWeight(QFont.Weight.DemiBold)
        p.setFont(mf)
        p.drawText(rect.adjusted(0, 34, 0, 34), Qt.AlignmentFlag.AlignCenter, self._mode_text)


class ExamAddDialog(QDialog):
    """Dialogo compatto per aggiungere un appello d'esame collegato a una materia."""

    def __init__(self, folders: List[Dict[str, Any]], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("🎓 Aggiungi Esame (Exam Countdown)")
        self.setMinimumWidth(420)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(_make_label("Materia / Cartella FlowNotes", "CardMeta"))
        self.subject_combo = QComboBox()
        self.subject_combo.setMinimumHeight(36)
        self.subject_combo.addItem("Generale", None)
        for f in folders:
            if f.get("parent_id") is None and f.get("name", "").lower() != "generale":
                self.subject_combo.addItem(f"📁 {f['name']}", int(f["id"]))
        lay.addWidget(self.subject_combo)

        lay.addWidget(_make_label("Nome Esame", "CardMeta"))
        self.title_edit = QLineEdit()
        self.title_edit.setMinimumHeight(36)
        self.title_edit.setPlaceholderText("Es. Analisi Matematica I — Scritto")
        lay.addWidget(self.title_edit)

        row = QHBoxLayout()
        row.setSpacing(10)
        left_col = QVBoxLayout()
        left_col.addWidget(_make_label("Data Appello", "CardMeta"))
        self.date_edit = QDateEdit(QDate.currentDate().addDays(30))
        self.date_edit.setMinimumHeight(36)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        left_col.addWidget(self.date_edit)
        row.addLayout(left_col, 2)

        right_col = QVBoxLayout()
        right_col.addWidget(_make_label("CFU", "CardMeta"))
        self.cfu_spin = QSpinBox()
        self.cfu_spin.setMinimumHeight(36)
        self.cfu_spin.setRange(1, 30)
        self.cfu_spin.setValue(9)
        right_col.addWidget(self.cfu_spin)
        row.addLayout(right_col, 1)
        lay.addLayout(row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        save_btn = _make_button("💾 Salva Esame", "primary")
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(save_btn)
        lay.addLayout(btns)

    def _on_save(self):
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Nome mancante", "Inserisci il nome dell'esame.")
            return
        self.accept()

    def result_data(self) -> Dict[str, Any]:
        return {
            "subject_id": self.subject_combo.currentData(),
            "exam_title": self.title_edit.text().strip(),
            "exam_date": self.date_edit.date().toString("yyyy-MM-dd"),
            "cfu": int(self.cfu_spin.value()),
        }

