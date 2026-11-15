"""Thread worker AI/Cloud, Grafo Conoscenza, SpotifyMiniBar e dialog di studio per FlowNotes."""

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

import ctypes
from ctypes import wintypes
try:
    import psutil
except ImportError:
    psutil = None
from database import db
from core.cloud_sync import cloud_sync
from ui.themes import _format_timestamp, HUB_QSS
from ui.theme_widgets import _make_button, _make_label



class AudioTranscribeThread(QThread):
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(str, str, str, object)  # final_markdown, file_name, source_path, sync_data
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, file_path: str, subject_context: str = "", engine=None):
        super().__init__()
        self.file_path = file_path
        self.subject_context = (subject_context or "").strip()
        self.engine = engine
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            target_engine = self.engine
            if target_engine is None:
                from core.ai_pipeline import FlowLocalEngine
                target_engine = FlowLocalEngine()

            def on_progress(msg: str, percent: int = -1):
                self.progress.emit(msg, percent)

            def is_cancelled() -> bool:
                return self.cancel_requested

            markdown, sync_data = target_engine.transcribe_audio_file(
                self.file_path,
                subject_context=self.subject_context,
                progress_callback=on_progress,
                cancel_check=is_cancelled,
                return_sync_data=True
            )
            if self.cancel_requested:
                self.cancelled.emit()
                return

            file_name = os.path.basename(self.file_path)
            self.finished.emit(markdown, file_name, self.file_path, sync_data)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))


class ExtractFlashcardsThread(QThread):
    finished = pyqtSignal(int, str)  # count, note_title
    error = pyqtSignal(str)

    def __init__(self, note_id: Optional[int], note_text: str, note_title: str):
        super().__init__()
        self.note_id = note_id
        self.note_text = (note_text or "").strip()
        self.note_title = (note_title or "Nota").strip()

    def run(self):
        try:
            from core.ai_pipeline import LLMRefiner
            cards = LLMRefiner.extract_flashcards_from_note(self.note_text)
            if not cards:
                raise ValueError("Nessuna flashcard generata dal contenuto della nota.")
            count = 0
            for item in cards:
                q = (item.get("question") or "").strip()
                a = (item.get("answer") or "").strip()
                if q and a:
                    db.add_study_note(
                        title=f"Flashcard: {q[:60]}",
                        content_markdown=f"**Domanda:**\n{q}\n\n**Risposta:**\n{a}",
                        note_type="Flashcard",
                        tags=f"Esame, {self.note_title[:30]}",
                        parent_note_id=self.note_id
                    )
                    count += 1
            self.finished.emit(count, self.note_title)
        except Exception as e:
            self.error.emit(str(e))


class _AutoSaveNoteThread(QThread):
    """Salva silenziosamente in SQLite in background senza bloccare la UI."""
    saved = pyqtSignal(int, str, str, str, str, str)  # note_id, title, content, subj, top, ntype
    error = pyqtSignal(str)

    def __init__(self, note_id: Optional[int], title: str, content: str, prof: str, subj: str, top: str, ntype: str):
        super().__init__()
        self.note_id = note_id
        self.title = title
        self.content = content
        self.prof = prof
        self.subj = subj
        self.top = top
        self.ntype = ntype

    def run(self):
        try:
            if self.note_id:
                db.update_study_note(self.note_id, self.title, self.content, self.prof, self.subj, self.top, self.ntype)
                saved_id = self.note_id
            else:
                saved_id = db.add_study_note(self.title, self.content, self.ntype, "", None, self.prof, self.subj, self.top)
            self.saved.emit(saved_id, self.title, self.content, self.subj, self.top, self.ntype)
        except Exception as e:
            self.error.emit(str(e))


class _SummarizeNoteThread(QThread):
    """Genera un riassunto AI della nota corrente tramite Ollama Qwen 3.5 in background."""
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, note_text: str):
        super().__init__()
        self.note_text = (note_text or "").strip()

    def run(self):
        try:
            import requests
            from core.ai_pipeline import OLLAMA_API_URL, LLMRefiner, strip_think_blocks
            model = LLMRefiner.detect_lecture_model() if hasattr(LLMRefiner, "detect_lecture_model") else "qwen3.5:4b"
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Sei un tutor universitario. Riassumi in modo chiaro, conciso e strutturato "
                            "i concetti chiave del seguente appunto in italiano (massimo 5 punti elenco)."
                        ),
                    },
                    {"role": "user", "content": self.note_text[:6000]},
                ],
                "stream": False,
                "think": False,
            }
            resp = requests.post(OLLAMA_API_URL, json=payload, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            summary = strip_think_blocks(data.get("message", {}).get("content") or "")
            if not summary:
                raise ValueError("Risposta vuota dal modello Qwen.")
            self.finished.emit(summary)
        except Exception as e:
            self.error.emit(str(e))


class DocumentImportThread(QThread):
    """Thread di elaborazione asincrona per l'importazione di Slide, PDF e Immagini (con OCR)."""
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(dict)       # result_dict
    error = pyqtSignal(str)

    def __init__(self, file_path: str, profile: str = "Alessandro", subject: str = "Generale", topic: str = "Generale"):
        super().__init__()
        self.file_path = file_path
        self.profile = profile
        self.subject = subject
        self.topic = topic

    def run(self):
        try:
            from core.doc_importer import import_document

            def _on_progress(current, total, msg):
                pct = int((current / max(1, total)) * 100)
                self.progress.emit(msg, pct)

            result = import_document(
                file_path=self.file_path,
                profile=self.profile,
                subject=self.subject,
                topic=self.topic,
                progress_callback=_on_progress
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class CloudQueueProcessThread(QThread):
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(int, list)  # count, results
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, engine=None):
        super().__init__()
        self.engine = engine
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            from core.cloud_sync import cloud_sync
            target_engine = self.engine
            if target_engine is None:
                from core.ai_pipeline import FlowLocalEngine
                target_engine = FlowLocalEngine()

            pending = cloud_sync.get_pending_queue()
            if not pending:
                self.finished.emit(0, [])
                return

            total = len(pending)
            results = []

            for idx, item in enumerate(pending, 1):
                if self.cancel_requested:
                    self.cancelled.emit()
                    return

                item_filename = (item.get("filename") or "file").strip()

                def on_progress(msg: str, pct: int = -1):
                    base_pct = int(((idx - 1) / total) * 100)
                    file_pct = pct if pct >= 0 else 0
                    scaled_pct = int(base_pct + (file_pct / total))
                    self.progress.emit(f"File {idx}/{total} ({item_filename}): {msg}", scaled_pct)

                def is_cancelled() -> bool:
                    return self.cancel_requested

                res = cloud_sync.process_queue_item(
                    item=item,
                    engine=target_engine,
                    progress_callback=on_progress,
                    cancel_check=is_cancelled
                )
                results.append(res)

            # Rigenerazione indice library.json per la PWA Mobile
            self.progress.emit("Rigenerazione indice library.json per PWA Mobile...", 99)
            cloud_sync.generate_library_json()
            self.progress.emit("Elaborazione coda completata!", 100)
            self.finished.emit(len(results), results)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))


class NewSubjectTopicDialog(QDialog):
    """Dialogo compatto per creare una nuova Materia e Argomento nel Notekeeper."""

    def __init__(self, parent=None, default_profile="Alessandro"):
        super().__init__(parent)
        self.setWindowTitle("Nuova Materia / Argomento")
        self.setFixedWidth(440)
        self.setStyleSheet("""
            QDialog { background-color: #161920; color: #F8FAFC; }
            QLabel { color: #E2E8F0; font-size: 12px; font-weight: 500; }
            QLineEdit, QComboBox {
                background-color: #1E2330;
                color: #FFFFFF;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                min-height: 32px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_lbl = QLabel("📚 Crea Materia & Argomento")
        title_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #38BDF8;")
        layout.addWidget(title_lbl)

        layout.addWidget(QLabel("Profilo Studente:"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["Alessandro", "Giada"])
        if default_profile in ("Alessandro", "Giada"):
            self.profile_combo.setCurrentText(default_profile)
        layout.addWidget(self.profile_combo)

        layout.addWidget(QLabel("Materia (es. Meccanica dei Robot, Fisica 1):"))
        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Nome Materia...")
        layout.addWidget(self.subject_input)

        layout.addWidget(QLabel("Argomento (es. Cinematica Diretta, Urti):"))
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("Nome Argomento...")
        layout.addWidget(self.topic_input)

        layout.addWidget(QLabel("Titolo Primo Appunto (opzionale):"))
        self.note_title_input = QLineEdit()
        self.note_title_input.setPlaceholderText("Introduzione...")
        layout.addWidget(self.note_title_input)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = _make_button("Crea", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self):
        s = self.subject_input.text().strip()
        t = self.topic_input.text().strip() or "Generale"
        if not s:
            QMessageBox.warning(self, "Attenzione", "Inserisci almeno il nome della materia.")
            return
        p = self.profile_combo.currentText().strip()
        ntitle = self.note_title_input.text().strip() or f"Appunto iniziale - {t}"
        db.add_study_note(
            title=ntitle,
            content_markdown=f"# {ntitle}\n\nAppunti per la materia **{s}**, argomento **{t}**.",
            note_type="appunto",
            profile=p,
            subject=s,
            topic=t,
            tags=f"Notekeeper, {s}, {t}, Profilo: {p}"
        )
        self.accept()


# =====================================================================
# SISTEMA NOTE COLLEGATE & GRAFO DELLA CONOSCENZA (OBSIDIAN STYLE)
# =====================================================================

class ConnectNoteDialog(QDialog):
    """Dialogo compatto per creare manualmente un collegamento tra due note."""

    def __init__(self, parent, source_id: int, source_title: str):
        super().__init__(parent)
        self.source_id = source_id
        self.source_title = source_title
        self.setWindowTitle("🔗 Collega Nota (Stile Obsidian)")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog {
                background-color: #11141C;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
            QLabel {
                color: #CBD5E1;
                font-family: 'Segoe UI';
                font-size: 12px;
            }
            QLineEdit {
                background-color: #161D2B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
                min-height: 36px;
            }
            QLineEdit:focus {
                border-color: #0284C7;
            }
            QComboBox {
                background-color: #161D2B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
                min-height: 36px;
            }
            QComboBox:focus {
                border-color: #0284C7;
            }
            QComboBox QAbstractItemView {
                background-color: #161D2B;
                color: #F8FAFC;
                selection-background-color: #0284C7;
                selection-color: #FFFFFF;
                border: 1px solid #334155;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel("🔗 Nuovo Collegamento tra Note")
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #F8FAFC;")
        layout.addWidget(header)

        src_lbl = QLabel(f"Nota di origine: <b style='color: #38BDF8;'>{source_title}</b>")
        src_lbl.setWordWrap(True)
        layout.addWidget(src_lbl)

        target_lbl = QLabel("Collega verso la nota:")
        target_lbl.setStyleSheet("font-weight: 600; color: #E2E8F0;")
        layout.addWidget(target_lbl)

        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(36)

        all_notes = db.get_notes()
        self.valid_targets = [n for n in all_notes if n.get("id") != self.source_id]
        if not self.valid_targets:
            self.target_combo.addItem("Nessun'altra nota disponibile", None)
            self.target_combo.setEnabled(False)
        else:
            for n in self.valid_targets:
                nid = n.get("id")
                ntitle = n.get("title") or "Senza Titolo"
                ntype = n.get("note_type") or "Nota"
                icon = "🎙️" if ntype == "Sbobina" else "📝"
                self.target_combo.addItem(f"{icon} [ID {nid}] {ntitle} ({ntype})", nid)
        layout.addWidget(self.target_combo)

        reason_lbl = QLabel("Motivo del collegamento (opzionale):")
        reason_lbl.setStyleSheet("font-weight: 600; color: #E2E8F0;")
        layout.addWidget(reason_lbl)

        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Es. Stesso istituto giuridico, approfondimento concettuale...")
        self.reason_edit.setMinimumHeight(36)
        layout.addWidget(self.reason_edit)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addStretch(1)

        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        self.save_btn = _make_button("🔗 Crea Collegamento", "primary")
        self.save_btn.clicked.connect(self._create_link)
        self.save_btn.setEnabled(bool(self.valid_targets))
        btn_box.addWidget(self.save_btn)

        layout.addLayout(btn_box)

    def _create_link(self):
        target_id = self.target_combo.currentData()
        if not target_id:
            QMessageBox.warning(self, "Nota non valida", "Seleziona una nota valida di destinazione.")
            return
        reason = self.reason_edit.text().strip()
        lid = db.add_note_link(self.source_id, target_id, reason)
        if lid:
            self.accept()
        else:
            QMessageBox.critical(self, "Errore", "Impossibile creare il collegamento.")


class AutoLinkThread(QThread):
    """Thread di background per eseguire l'auto-linking semantico con Ollama Qwen."""
    finished = pyqtSignal(int, int)  # note_id, count_links_created

    def __init__(self, note_id: int, title: str, content: str):
        super().__init__()
        self.note_id = note_id
        self.title = title
        self.content = content

    def run(self):
        try:
            from core.ai_pipeline import LLMRefiner
            all_notes = db.get_notes()
            candidates = [n for n in all_notes if n.get("id") != self.note_id]
            if not candidates:
                self.finished.emit(self.note_id, 0)
                return

            related = LLMRefiner.find_related_notes(self.title, self.content, candidates)
            created_count = 0
            for r in related:
                target_id = r.get("id")
                reason = r.get("reason", "")
                if target_id and db.add_note_link(self.note_id, target_id, reason):
                    created_count += 1
            self.finished.emit(self.note_id, created_count)
        except Exception as e:
            print(f"[FlowLocal] Errore esecuzione AutoLinkThread: {e}")
            self.finished.emit(self.note_id, 0)


class GraphEdgeItem(QGraphicsItem):
    """Arco orientato del Grafo della Conoscenza con tooltip motivazione."""

    def __init__(self, source_node: "GraphNodeItem", target_node: "GraphNodeItem", reason: str = "", link_id: int = 0):
        super().__init__()
        self.source = source_node
        self.target = target_node
        self.reason = reason
        self.link_id = link_id
        self.line = QLineF()
        self.setZValue(1)
        self.setAcceptHoverEvents(True)
        self.is_hovered = False

        reason_str = f"\nMotivo: {self.reason}" if self.reason else "\nMotivo: Correlazione concettuale"
        self.setToolTip(f"🔗 {self.source.title} ──► {self.target.title}{reason_str}")
        self.adjust()

    def boundingRect(self) -> QRectF:
        if self.line.isNull():
            return QRectF()
        extra = 12.0
        p1 = self.line.p1()
        p2 = self.line.p2()
        return QRectF(
            min(p1.x(), p2.x()) - extra,
            min(p1.y(), p2.y()) - extra,
            abs(p1.x() - p2.x()) + extra * 2,
            abs(p1.y() - p2.y()) + extra * 2
        )

    def adjust(self):
        if not self.source or not self.target:
            return
        line = QLineF(self.source.pos(), self.target.pos())
        length = line.length()
        if length <= 0.001:
            return

        self.prepareGeometryChange()
        r_src = self.source.radius
        r_dst = self.target.radius
        dx = line.dx() / length
        dy = line.dy() / length
        p1 = QPointF(self.source.pos().x() + dx * r_src, self.source.pos().y() + dy * r_src)
        p2 = QPointF(self.target.pos().x() - dx * r_dst, self.target.pos().y() - dy * r_dst)
        self.line = QLineF(p1, p2)
        self.update()

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.setZValue(5)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.setZValue(1)
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        if self.line.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self.is_hovered:
            pen = QPen(QColor("#38BDF8"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            color_brush = QColor("#38BDF8")
        else:
            pen = QPen(QColor(56, 189, 248, 140), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            color_brush = QColor(56, 189, 248, 180)

        painter.setPen(pen)
        painter.drawLine(self.line)

        # Freccia orientata verso il target
        angle = math.atan2(-self.line.dy(), self.line.dx())
        arrow_size = 9.0 if self.is_hovered else 7.5
        p2 = self.line.p2()
        arrow_p1 = p2 - QPointF(math.sin(angle + math.pi / 3.0) * arrow_size,
                                math.cos(angle + math.pi / 3.0) * arrow_size)
        arrow_p2 = p2 - QPointF(math.sin(angle + math.pi - math.pi / 3.0) * arrow_size,
                                math.cos(angle + math.pi - math.pi / 3.0) * arrow_size)

        arrow_head = QPolygonF([p2, arrow_p1, arrow_p2])
        painter.setBrush(QBrush(color_brush))
        painter.drawPolygon(arrow_head)


class GraphNodeItem(QGraphicsItem):
    """Nodo interattivo del Grafo (Sbobina o Appunto Rapido) colorato per tipo e trascinabile."""

    def __init__(self, node_data: Dict[str, Any], on_node_click: Optional[Callable[[int], None]] = None):
        super().__init__()
        self.node_id = node_data.get("id")
        self.title = node_data.get("title") or "Senza Titolo"
        self.note_type = node_data.get("note_type") or "Note"
        self.tags = node_data.get("tags") or ""
        self.preview = node_data.get("preview") or ""
        self.on_node_click = on_node_click

        # Dimensioni differenziate per tipo di nodo (compatte per evitare sovrapposizioni)
        self.is_sbobina = (self.note_type.lower() == "sbobina" or "sbobina" in self.title.lower())
        self.radius = 15.0 if self.is_sbobina else 11.5
        self.edges: List[GraphEdgeItem] = []

        # Variabili fisica Force-Directed
        self.vx = 0.0
        self.vy = 0.0
        self.mass = 1.3 if self.is_sbobina else 1.0
        self.is_dragged = False
        self.is_hovered = False

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

        # Etichetta pulita e compatta
        clean_title = self.title.replace("Sbobina:", "").strip()
        self.label_text = clean_title if len(clean_title) <= 16 else clean_title[:14] + "…"

        # Normalizzazione retrocompatibile tag per visualizzazione ("Profilo: Lei" -> "Profilo: Giada")
        raw_tags = self.tags or ""
        norm_tags = raw_tags.replace("Profilo: Lei", "Profilo: Giada")
        norm_tokens = [(t or "").strip() for t in norm_tags.split(",") if (t or "").strip()]
        norm_tokens = ["Giada" if t in ("Lei", "Profilo: Lei") else t for t in norm_tokens]
        display_tags = ", ".join(norm_tokens)

        tipo_display = "🎙️ Sbobina" if self.is_sbobina else "⚡ Appunto Rapido"
        tip = f"[{tipo_display}] {self.title}\nTag: {display_tags or 'Nessuno'}"
        if self.preview:
            tip += f"\n\nAnteprima:\n{self.preview[:160]}..."
        tip += "\n\n(Clicca per aprire ed evidenziare la nota nella lista)"
        self.setToolTip(tip)

    def add_edge(self, edge: GraphEdgeItem):
        self.edges.append(edge)

    def boundingRect(self) -> QRectF:
        r = self.radius + 6.0
        return QRectF(-r - 35, -r, (r + 35) * 2, r * 2 + 22)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(-self.radius, -self.radius, self.radius * 2, self.radius * 2)
        return path

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.adjust()
            scene = self.scene()
            if scene and hasattr(scene, "wake_simulation"):
                scene.wake_simulation()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self.is_dragged = True
        self.setZValue(25)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.is_dragged = False
        self.setZValue(10)
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self.on_node_click:
                self.on_node_click(self.node_id)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.setZValue(20)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.setZValue(10)
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # Tavolozza per tipo di nodo
        if self.is_sbobina:
            base_col = QColor("#7C3AED")      # Viola scuro
            border_col = QColor("#C4B5FD")    # Viola brillante
            glow_col = QColor(139, 92, 246, 75)
            icon_char = "🎙️"
        elif "latex" in self.note_type.lower():
            base_col = QColor("#D97706")      # Ambra
            border_col = QColor("#FDE68A")
            glow_col = QColor(245, 158, 11, 75)
            icon_char = "📐"
        else:
            base_col = QColor("#0284C7")      # Cyan FlowLocal
            border_col = QColor("#7DD3FC")
            glow_col = QColor(2, 132, 199, 75)
            icon_char = "⚡"

        # Alone luminoso esterno su hover o selezione
        if self.is_hovered or self.isSelected():
            glow_pen = QPen(QColor("#FFFFFF"), 2.0)
            painter.setPen(glow_pen)
            painter.setBrush(QBrush(glow_col))
            painter.drawEllipse(QPointF(0, 0), self.radius + 6.0, self.radius + 6.0)

        # Cerchio del nodo con gradiente radiale
        grad = QRadialGradient(QPointF(-self.radius * 0.25, -self.radius * 0.25), self.radius * 1.3)
        grad.setColorAt(0.0, border_col)
        grad.setColorAt(1.0, base_col)

        b_width = 3.0 if (self.is_hovered or self.isSelected()) else 2.0
        painter.setPen(QPen(border_col, b_width))
        painter.setBrush(QBrush(grad))
        painter.drawEllipse(QPointF(0, 0), self.radius, self.radius)

        # Icona centrale
        font_icon = QFont("Segoe UI Emoji", 8 if self.radius < 14 else 10)
        painter.setFont(font_icon)
        painter.setPen(QPen(QColor("#FFFFFF")))
        painter.drawText(QRectF(-self.radius, -self.radius, self.radius * 2, self.radius * 2), Qt.AlignmentFlag.AlignCenter, icon_char)

        # Pillola titolo sotto il nodo (font compatto 8pt) per evitare sovrapposizioni
        font_label = QFont("Segoe UI", 8)
        font_label.setBold(True)
        painter.setFont(font_label)

        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(self.label_text) + 8
        text_h = metrics.height() + 2
        text_y = self.radius + 3

        pill_rect = QRectF(-text_w / 2, text_y, text_w, text_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(15, 20, 31, 220)))
        painter.drawRoundedRect(pill_rect, 3, 3)

        text_color = QColor("#38BDF8") if self.is_hovered else QColor("#F8FAFC")
        painter.setPen(QPen(text_color))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self.label_text)


class KnowledgeGraphScene(QGraphicsScene):
    """Scena con simulazione fisica Force-Directed (Coulomb + Hooke + Center Gravity)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes: List[GraphNodeItem] = []
        self.edges: List[GraphEdgeItem] = []

        self.sim_timer = QTimer(self)
        self.sim_timer.setInterval(28)  # ~35 FPS
        self.sim_timer.timeout.connect(self._step_simulation)
        self.iterations = 0

    def wake_simulation(self):
        self.iterations = 0
        if not self.sim_timer.isActive():
            self.sim_timer.start()

    def _step_simulation(self):
        if not self.nodes:
            self.sim_timer.stop()
            return

        self.iterations += 1
        total_movement = 0.0

        k_rep = 14000.0    # Repulsione elettrostatica aumentata (distanza nodi maggiore)
        k_spring = 0.025   # Molla elastica morbida
        l_0 = 260.0        # Lunghezza a riposo della molla aumentata (260 px)
        k_gravity = 0.0025 # Attrazione verso il baricentro ridotta per distanziare i nodi
        damping = 0.85     # Smorzamento della velocità

        n_count = len(self.nodes)

        # 1. Repulsione Coulomb tra ogni coppia di nodi
        for i in range(n_count):
            n1 = self.nodes[i]
            p1 = n1.pos()
            for j in range(i + 1, n_count):
                n2 = self.nodes[j]
                p2 = n2.pos()
                dx = p1.x() - p2.x()
                dy = p1.y() - p2.y()
                dist = math.hypot(dx, dy)
                if dist < 1.0:
                    dx = random.uniform(-2, 2)
                    dy = random.uniform(-2, 2)
                    dist = math.hypot(dx, dy) or 1.0

                if dist < 900.0:
                    force = k_rep / (dist * dist)
                    fx = (dx / dist) * force
                    fy = (dy / dist) * force

                    n1.vx += fx / n1.mass
                    n1.vy += fy / n1.mass
                    n2.vx -= fx / n2.mass
                    n2.vy -= fy / n2.mass

        # 2. Attrazione Hooke lungo gli archi
        for edge in self.edges:
            n1 = edge.source
            n2 = edge.target
            p1 = n1.pos()
            p2 = n2.pos()
            dx = p2.x() - p1.x()
            dy = p2.y() - p1.y()
            dist = math.hypot(dx, dy)
            if dist < 1.0:
                continue
            delta = dist - l_0
            force = k_spring * delta
            fx = (dx / dist) * force
            fy = (dy / dist) * force

            n1.vx += fx / n1.mass
            n1.vy += fy / n1.mass
            n2.vx -= fx / n2.mass
            n2.vy -= fy / n2.mass

        # 3. Gravità centrale e aggiornamento posizioni
        for node in self.nodes:
            pos = node.pos()
            node.vx -= pos.x() * k_gravity
            node.vy -= pos.y() * k_gravity

            node.vx *= damping
            node.vy *= damping

            speed = math.hypot(node.vx, node.vy)
            if speed > 16.0:
                node.vx = (node.vx / speed) * 16.0
                node.vy = (node.vy / speed) * 16.0

            if not node.is_dragged:
                node.setPos(pos.x() + node.vx, pos.y() + node.vy)
                total_movement += speed

            for edge in node.edges:
                edge.adjust()

        # Fermata quando la convergenza è raggiunta o dopo 350 iterazioni
        if (total_movement < 0.4 and self.iterations > 25) or self.iterations > 350:
            self.sim_timer.stop()


class KnowledgeGraphWidget(QFrame):
    """Widget interattivo PyQt6 contenente QGraphicsView + QGraphicsScene per il Grafo della Conoscenza."""

    def __init__(self, on_node_click: Optional[Callable[[int], None]] = None, parent=None):
        super().__init__(parent)
        self.on_node_click = on_node_click
        self.setObjectName("KnowledgeGraphFrame")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Barra controlli overlay in cima al grafo
        self.toolbar = QFrame()
        self.toolbar.setObjectName("KnowledgeGraphToolbar")
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(8)

        self.stats_label = QLabel("Caricamento nodi...")
        self.stats_label.setStyleSheet("color: #94A3B8; font-size: 11.5px; font-weight: 500;")
        tb_layout.addWidget(self.stats_label)

        tb_layout.addStretch(1)

        zoom_in_btn = _make_button("🔍 +")
        zoom_in_btn.setToolTip("Ingrandisci zoom")
        zoom_in_btn.clicked.connect(self._zoom_in)
        tb_layout.addWidget(zoom_in_btn)

        zoom_out_btn = _make_button("🔍 -")
        zoom_out_btn.setToolTip("Riduci zoom")
        zoom_out_btn.clicked.connect(self._zoom_out)
        tb_layout.addWidget(zoom_out_btn)

        fit_btn = _make_button("🎯 Centra")
        fit_btn.setToolTip("Centra tutti i nodi nella vista")
        fit_btn.clicked.connect(self._fit_view)
        tb_layout.addWidget(fit_btn)

        reload_btn = _make_button("🔄 Ricarica")
        reload_btn.setToolTip("Ricarica i nodi e riavvia la simulazione fisica")
        reload_btn.clicked.connect(self.load_data)
        tb_layout.addWidget(reload_btn)

        layout.addWidget(self.toolbar)

        # Scena e Vista QGraphics
        self.scene = KnowledgeGraphScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setObjectName("KnowledgeGraphView")

        layout.addWidget(self.view, 1)

    def _zoom_in(self):
        self.view.scale(1.2, 1.2)

    def _zoom_out(self):
        self.view.scale(0.83, 0.83)

    def _fit_view(self):
        rect = self.scene.itemsBoundingRect()
        if not rect.isNull() and rect.width() > 10:
            self.view.fitInView(rect.adjusted(-90, -90, 90, 90), Qt.AspectRatioMode.KeepAspectRatio)

    def load_data(self):
        """Carica nodi e archi dal database SQLite e avvia la disposizione force-directed."""
        self.scene.clear()
        self.scene.nodes.clear()
        self.scene.edges.clear()

        graph_data = db.get_all_graph_data()
        nodes_raw = graph_data.get("nodes", [])
        edges_raw = graph_data.get("edges", [])

        if not nodes_raw:
            self.stats_label.setText("Nessuna nota salvata nel Grafo.")
            empty_item = self.scene.addText("Nessuna nota presente.\nSalva o sbobina una lezione per visualizzare il Grafo della Conoscenza.")
            empty_item.setDefaultTextColor(QColor("#64748B"))
            font = QFont("Segoe UI", 12)
            font.setBold(True)
            empty_item.setFont(font)
            empty_item.setPos(-160, -20)
            return

        sbobine_count = sum(1 for n in nodes_raw if (n.get("note_type", "").lower() == "sbobina" or "sbobina" in (n.get("title") or "").lower()))
        rapidi_count = len(nodes_raw) - sbobine_count
        self.stats_label.setText(
            f"🟣 {sbobine_count} Sbobine   |   🔵 {rapidi_count} Appunti Rapidi   |   🔗 {len(edges_raw)} Collegamenti"
        )

        nodes_map: Dict[int, GraphNodeItem] = {}

        # Disposizione iniziale circolare con ampia spaziatura e jitter casuale
        count = len(nodes_raw)
        radius_init = max(180.0, count * 65.0)
        for i, n_data in enumerate(nodes_raw):
            node_item = GraphNodeItem(n_data, on_node_click=self.on_node_click)
            theta = (2.0 * math.pi * i) / count
            jitter = random.uniform(-15, 15)
            x = math.cos(theta) * (radius_init + jitter)
            y = math.sin(theta) * (radius_init + jitter)
            node_item.setPos(x, y)
            self.scene.addItem(node_item)
            self.scene.nodes.append(node_item)
            nodes_map[n_data["id"]] = node_item

        # Creazione archi orientati
        for e_data in edges_raw:
            src_id = e_data.get("source_id")
            tgt_id = e_data.get("target_id")
            if src_id in nodes_map and tgt_id in nodes_map:
                src_item = nodes_map[src_id]
                tgt_item = nodes_map[tgt_id]
                edge_item = GraphEdgeItem(src_item, tgt_item, reason=e_data.get("reason", ""), link_id=e_data.get("id", 0))
                self.scene.addItem(edge_item)
                self.scene.edges.append(edge_item)
                src_item.add_edge(edge_item)
                tgt_item.add_edge(edge_item)

        # Avvio simulazione fisica
        self.scene.wake_simulation()
        QTimer.singleShot(250, self._fit_view)



class QuickRuleDialog(QDialog):
    """Dialog rapido per trasformare una trascrizione in regola Dizionario o Snippet."""

    def __init__(self, initial_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Correggi / Salva Regola")
        self.setMinimumWidth(520)
        self.setStyleSheet(parent.styleSheet() if parent and parent.styleSheet() else HUB_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(_make_label("Crea regola rapida dalla trascrizione", "PageTitle"))
        layout.addWidget(_make_label(
            "Salva una correzione fonetica nel Dizionario oppure trasforma la frase in uno Snippet vocale.",
            "PageSubtitle",
            wrap=True,
        ))

        tab_row = QHBoxLayout()
        tab_row.setSpacing(8)
        self.btn_dict = QPushButton("📖 Dizionario (Alias → Parola)")
        self.btn_dict.setObjectName("FilterPill")
        self.btn_dict.setCheckable(True)
        self.btn_dict.setChecked(True)
        self.btn_snip = QPushButton("⚡ Snippet (Trigger → Testo)")
        self.btn_snip.setObjectName("FilterPill")
        self.btn_snip.setCheckable(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.btn_dict, 0)
        self._mode_group.addButton(self.btn_snip, 1)

        tab_row.addWidget(self.btn_dict)
        tab_row.addWidget(self.btn_snip)
        tab_row.addStretch(1)
        layout.addLayout(tab_row)

        self.stack = QStackedWidget()

        # Pagina 0: Dizionario
        dict_w = QWidget()
        dl = QVBoxLayout(dict_w)
        dl.setContentsMargins(0, 4, 0, 0)
        dl.setSpacing(8)
        dl.addWidget(_make_label("Parola errata / alias pronunciato", "CardMeta"))
        self.alias_edit = QLineEdit(initial_text.strip())
        self.alias_edit.setPlaceholderText("Es. antigraffiti")
        self.alias_edit.setMinimumHeight(36)
        dl.addWidget(self.alias_edit)
        dl.addWidget(_make_label("Parola corretta da inserire", "CardMeta"))
        self.term_edit = QLineEdit()
        self.term_edit.setPlaceholderText("Es. Antigravity")
        self.term_edit.setMinimumHeight(36)
        dl.addWidget(self.term_edit)
        dl.addStretch(1)
        self.stack.addWidget(dict_w)

        # Pagina 1: Snippet
        snip_w = QWidget()
        sl = QVBoxLayout(snip_w)
        sl.setContentsMargins(0, 4, 0, 0)
        sl.setSpacing(8)
        sl.addWidget(_make_label("Frase trigger vocale (breve)", "CardMeta"))
        self.trigger_edit = QLineEdit()
        self.trigger_edit.setPlaceholderText("Es. firma cliente")
        self.trigger_edit.setMinimumHeight(36)
        sl.addWidget(self.trigger_edit)
        sl.addWidget(_make_label("Testo espanso completo", "CardMeta"))
        self.expansion_edit = QTextEdit()
        self.expansion_edit.setMinimumHeight(110)
        self.expansion_edit.setPlainText(initial_text.strip())
        sl.addWidget(self.expansion_edit, 1)
        self.stack.addWidget(snip_w)

        self._mode_group.idClicked.connect(self.stack.setCurrentIndex)
        layout.addWidget(self.stack, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        save_btn = _make_button("Salva regola", "primary")
        save_btn.clicked.connect(self._on_save)
        btns.addWidget(cancel_btn)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

    def _on_save(self):
        if self.stack.currentIndex() == 0:
            alias = self.alias_edit.text().strip()
            term = self.term_edit.text().strip()
            if not term:
                self.term_edit.setFocus()
                return
            if alias:
                db.learn_vocabulary_pair(alias, term)
            else:
                existing = {v["correct_term"].lower() for v in db.get_all_vocabulary()}
                if term.lower() not in existing:
                    db.add_vocabulary(term, "", "Manuale")
        else:
            trigger = self.trigger_edit.text().strip()
            expansion = self.expansion_edit.toPlainText().strip()
            if not trigger:
                self.trigger_edit.setFocus()
                return
            if not expansion:
                self.expansion_edit.setFocus()
                return
            db.add_snippet(trigger, expansion)
        self.accept()


class SpotifyMiniBar(QFrame):
    """Mini-player Spotify compatto per la barra superiore di FlowLocal (icone vettoriali nitide)."""

    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_PLAY_PAUSE = 0xB3
    APPCOMMAND_MEDIA_NEXTTRACK = 11
    APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
    APPCOMMAND_MEDIA_PLAY_PAUSE = 14
    WM_APPCOMMAND = 0x0319
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SpotifyMiniBar")
        self.setFixedHeight(30)
        self.setFixedWidth(242)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._spotify_hwnd: Optional[int] = None
        self._spotify_running = False
        self._is_playing = False
        self._is_dark_theme = True
        self._last_raw_title = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)

        self.track_btn = QPushButton("🎵 Spotify")
        self.track_btn.setObjectName("SpotifyTrackBtn")
        self.track_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.track_btn.setFixedWidth(152)
        self.track_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.track_btn.setToolTip("Clicca per aprire o portare in primo piano Spotify")
        self.track_btn.clicked.connect(self._on_track_clicked)
        layout.addWidget(self.track_btn)

        self.prev_btn = QPushButton("")
        self.prev_btn.setObjectName("SpotifyCtrlBtn")
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.setIconSize(QSize(13, 13))
        self.prev_btn.setToolTip("Brano precedente")
        self.prev_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_PREV_TRACK, self.APPCOMMAND_MEDIA_PREVIOUSTRACK)
        )
        layout.addWidget(self.prev_btn)

        self.play_btn = QPushButton("")
        self.play_btn.setObjectName("SpotifyCtrlBtn")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setIconSize(QSize(13, 13))
        self.play_btn.setToolTip("Play / Pausa")
        self.play_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_PLAY_PAUSE, self.APPCOMMAND_MEDIA_PLAY_PAUSE)
        )
        layout.addWidget(self.play_btn)

        self.next_btn = QPushButton("")
        self.next_btn.setObjectName("SpotifyCtrlBtn")
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.setIconSize(QSize(13, 13))
        self.next_btn.setToolTip("Brano successivo")
        self.next_btn.clicked.connect(
            lambda: self._send_media_key(self.VK_MEDIA_NEXT_TRACK, self.APPCOMMAND_MEDIA_NEXTTRACK)
        )
        layout.addWidget(self.next_btn)

        self._update_control_icons()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_spotify)
        self._poll_timer.start(1800)
        QTimer.singleShot(300, self._poll_spotify)

    @staticmethod
    def _make_media_icon(kind: str, color: str, size: int = 13) -> QIcon:
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        col = QColor(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))

        s = float(size)
        if kind == "pause":
            bw = max(2.2, s * 0.22)
            gap = max(2.2, s * 0.20)
            total_w = bw * 2.0 + gap
            x0 = (s - total_w) / 2.0
            top = s * 0.16
            bh = s * 0.68
            p.drawRoundedRect(QRectF(x0, top, bw, bh), 1.0, 1.0)
            p.drawRoundedRect(QRectF(x0 + bw + gap, top, bw, bh), 1.0, 1.0)
        elif kind == "play":
            poly = QPolygonF([
                QPointF(s * 0.25, s * 0.14),
                QPointF(s * 0.86, s * 0.50),
                QPointF(s * 0.25, s * 0.86),
            ])
            p.drawPolygon(poly)
        elif kind == "prev":
            p.drawRoundedRect(QRectF(s * 0.15, s * 0.18, s * 0.14, s * 0.64), 0.8, 0.8)
            poly = QPolygonF([
                QPointF(s * 0.84, s * 0.17),
                QPointF(s * 0.34, s * 0.50),
                QPointF(s * 0.84, s * 0.83),
            ])
            p.drawPolygon(poly)
        elif kind == "next":
            poly = QPolygonF([
                QPointF(s * 0.16, s * 0.17),
                QPointF(s * 0.66, s * 0.50),
                QPointF(s * 0.16, s * 0.83),
            ])
            p.drawPolygon(poly)
            p.drawRoundedRect(QRectF(s * 0.71, s * 0.18, s * 0.14, s * 0.64), 0.8, 0.8)

        p.end()
        return QIcon(pm)

    def set_theme_mode(self, is_dark: bool):
        self._is_dark_theme = bool(is_dark)
        self._update_control_icons()

    def _update_control_icons(self):
        fg = "#E2E8F0" if self._is_dark_theme else "#334155"
        play_col = ("#10B981" if self._is_dark_theme else "#059669") if self._is_playing else fg
        win = self.window()
        if win is not None and hasattr(win, "_active_palette"):
            try:
                pal = win._active_palette()
                fg = pal.get("text", fg)
                if self._is_playing:
                    play_col = pal.get("accent", play_col)
                else:
                    play_col = fg
            except Exception:
                pass
        self.prev_btn.setIcon(self._make_media_icon("prev", fg))
        self.next_btn.setIcon(self._make_media_icon("next", fg))
        if self._is_playing:
            self.play_btn.setIcon(self._make_media_icon("pause", play_col))
            self.play_btn.setToolTip("Metti in pausa Spotify")
        else:
            self.play_btn.setIcon(self._make_media_icon("play", play_col))
            self.play_btn.setToolTip("Avvia o riprendi Spotify")

    @staticmethod
    def _find_spotify_window() -> tuple:
        """Restituisce (is_running, hwnd, title) per Spotify Desktop su Windows."""
        spotify_pids = set()
        if psutil is not None:
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    pname = (proc.info.get("name") or "").lower()
                    if pname == "spotify.exe":
                        spotify_pids.add(proc.info["pid"])
            except Exception:
                pass

        if not spotify_pids:
            return False, None, ""

        found_windows = []
        try:
            user32 = ctypes.windll.user32
            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in spotify_pids:
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = (buf.value or "").strip()
                if title and title not in ("Default IME", "MSCTFIME UI", "AngleHiddenWindow"):
                    found_windows.append((hwnd, title))
                return True

            user32.EnumWindows(enum_proc(_cb), 0)
        except Exception:
            pass

        if not found_windows:
            return True, None, "Spotify"

        # Preferisci la finestra che mostra "Artista - Brano"
        for hwnd, title in found_windows:
            if " - " in title or " – " in title:
                return True, hwnd, title
        return True, found_windows[0][0], found_windows[0][1]

    def _poll_spotify(self):
        win = self.window()
        if win is not None and not win.isVisible():
            return
        running, hwnd, raw_title = self._find_spotify_window()
        self._spotify_running = running
        self._spotify_hwnd = hwnd
        self._last_raw_title = raw_title

        is_playing_now = False
        if not running:
            raw_display = "🎵 Apri Spotify"
            tooltip = "Spotify non è aperto — clicca per avviarlo"
        elif raw_title.lower() in ("spotify", "spotify free", "spotify premium", ""):
            raw_display = "🎵 Spotify · Pausa"
            tooltip = "Spotify in pausa — premi Play per riprendere"
        else:
            is_playing_now = True
            clean = raw_title.replace(" - ", " — ")
            tooltip = f"In riproduzione su Spotify:\n{clean}"
            raw_display = f"🎵 {clean}"

        display = self.track_btn.fontMetrics().elidedText(
            raw_display, Qt.TextElideMode.ElideRight, 134
        )

        if is_playing_now != self._is_playing:
            self._is_playing = is_playing_now
            self._update_control_icons()

        if display != self.track_btn.text() or tooltip != self.track_btn.toolTip():
            self.track_btn.setText(display)
            self.track_btn.setToolTip(tooltip)

    def _verify_or_fallback_appcommand(self, prev_title: str, app_command: int):
        running, hwnd, raw_title = self._find_spotify_window()
        if running and hwnd and raw_title == prev_title:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, self.WM_APPCOMMAND, hwnd, app_command << 16)
            except Exception as e:
                print(f"[SpotifyMiniBar] Fallback WM_APPCOMMAND fallito: {e}")
        QTimer.singleShot(260, self._poll_spotify)

    def _send_media_key(self, vk_code: int, app_command: int):
        prev_title = self._last_raw_title
        if vk_code == self.VK_MEDIA_PLAY_PAUSE and self._spotify_running:
            self._is_playing = not self._is_playing
            self._update_control_icons()
        try:
            user32 = ctypes.windll.user32
            scan_code = user32.MapVirtualKeyW(vk_code, 0)
            user32.keybd_event(vk_code, scan_code, self.KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(vk_code, scan_code, self.KEYEVENTF_EXTENDEDKEY | self.KEYEVENTF_KEYUP, 0)
        except Exception as e:
            print(f"[SpotifyMiniBar] Errore comando media: {e}")
        QTimer.singleShot(320, lambda pt=prev_title, cmd=app_command: self._verify_or_fallback_appcommand(pt, cmd))

    def _on_track_clicked(self):
        if self._spotify_running and self._spotify_hwnd:
            try:
                user32 = ctypes.windll.user32
                if user32.IsIconic(self._spotify_hwnd):
                    user32.ShowWindow(self._spotify_hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(self._spotify_hwnd)
                return
            except Exception:
                pass
        try:
            os.startfile("spotify:")
            QTimer.singleShot(1500, self._poll_spotify)
        except Exception as e:
            print(f"[SpotifyMiniBar] Impossibile avviare Spotify: {e}")



class TimeMachineDialog(QDialog):
    """Dialogo Time Machine per consultare e ripristinare le versioni storiche di una nota."""

    def __init__(self, note_id: int, note_title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.note_id = note_id
        self.selected_snapshot: Optional[Dict[str, Any]] = None
        self.setWindowTitle(f"🕒 Time Machine — {note_title or 'Nota'}")
        self.resize(820, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        hdr = QHBoxLayout()
        hdr.addWidget(_make_label("🕒 Cronologia Versioni (ultimi 20 snapshot)", "CardTitle"), 1)
        close_btn = _make_button("Chiudi")
        close_btn.clicked.connect(self.reject)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.version_list = QListWidget()
        self.version_list.setMinimumWidth(260)
        self.version_list.currentRowChanged.connect(self._on_version_selected)
        splitter.addWidget(self.version_list)

        right_w = QWidget()
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(8, 0, 0, 0)
        right_l.setSpacing(8)

        self.preview_title_lbl = _make_label("Seleziona una versione", "CardTitle", wrap=True)
        right_l.addWidget(self.preview_title_lbl)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        right_l.addWidget(self.preview_text, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.restore_btn = _make_button("⏪ Ripristina questa versione", "primary")
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self._on_restore_clicked)
        foot.addWidget(self.restore_btn)
        right_l.addLayout(foot)

        splitter.addWidget(right_w)
        splitter.setSizes([270, 520])
        lay.addWidget(splitter, 1)

        self._snapshots = db.get_note_history(note_id, limit=20)
        self._populate()

    @staticmethod
    def _format_ts(ts_str: str) -> str:
        try:
            dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
            today = dt_date.today()
            if dt.date() == today:
                return f"Oggi, {dt.strftime('%H:%M')}"
            if (today - dt.date()).days == 1:
                return f"Ieri, {dt.strftime('%H:%M')}"
            return dt.strftime("%d/%m %H:%M")
        except Exception:
            return ts_str

    def _populate(self):
        self.version_list.clear()
        if not self._snapshots:
            item = QListWidgetItem("Nessuno snapshot disponibile per questa nota.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.version_list.addItem(item)
            self.preview_text.setPlainText("Gli snapshot vengono creati automaticamente durante la scrittura o prima delle elaborazioni AI.")
            return
        for snap in self._snapshots:
            when = self._format_ts(snap.get("local_created_at") or "")
            reason = snap.get("trigger_reason") or "Salvataggio automatico"
            item = QListWidgetItem(f"{when}\n{reason}")
            item.setData(Qt.ItemDataRole.UserRole, snap)
            self.version_list.addItem(item)
        self.version_list.setCurrentRow(0)

    def _on_version_selected(self, row: int):
        item = self.version_list.item(row) if row >= 0 else None
        snap = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(snap, dict):
            self.restore_btn.setEnabled(False)
            return
        self.selected_snapshot = snap
        self.preview_title_lbl.setText(snap.get("title") or "Senza titolo")
        self.preview_text.setPlainText(snap.get("content") or "")
        self.restore_btn.setEnabled(True)

    def _on_restore_clicked(self):
        if self.selected_snapshot is not None:
            self.accept()

