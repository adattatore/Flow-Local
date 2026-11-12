"""
FlowLocalHub (PyQt6)
Centro di controllo desktop minimal-dark (960x640).

Layout:
- Sidebar fissa a sinistra (210 px): titolo, stato motore, navigazione, promemoria scorciatoia.
- QStackedWidget a 4 pagine:
  1. Cronologia     — ricerca fissa + lista scorrevole delle trascrizioni
  2. Note           — ricerca fissa + esportazione Markdown + lista note
  3. Dizionario     — form fisso (alias -> parola corretta) + lista regole
  4. Stili & Prompt — selettore modalità + editor System Prompt

Regole UI (AGENTS.md §7): solo QSS valido Qt6, form/ricerche fuori dalla QScrollArea
con altezza minima esplicita, liste scorrevoli anti-compressione.
"""

from datetime import datetime
from typing import Callable, Dict, List, Optional, Any
import os
import re
import threading
import pyperclip

# Compatibilità PyAV >= 13/14 con Faster-Whisper
try:
    import av
    if hasattr(av, "open"):
        _orig_av_open = av.open
        def _safe_av_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return _orig_av_open(*args, **kwargs)
        av.open = _safe_av_open
    if hasattr(av, "container") and hasattr(av.container, "core") and hasattr(av.container.core, "open"):
        av.container.core.open = _safe_av_open
except Exception:
    pass

import math
import random

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QPointF, QRectF, QLineF
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath, QRadialGradient,
    QLinearGradient, QCursor, QPolygonF
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QComboBox, QTreeWidget, QTreeWidgetItem, QSplitter
)

from database import db
from core.exporter import export_to_md, export_to_docx, export_to_pdf
from core.cloud_sync import cloud_sync
from core.whis_agent import WhisAgent, WhisStreamWorker
from core.audio_feedback import (
    audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE,
    VOICE_PRESETS, DEFAULT_VOICE_KEY, TEST_PHRASE
)

# Rilevamento GPU tramite NVML (opzionale, senza warning)
GPU_AVAILABLE = False
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pynvml
        pynvml.nvmlInit()
        pynvml.nvmlDeviceGetHandleByIndex(0)
        GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False


# =====================================================================
# STYLESHEET GLOBALE (solo proprietà QSS supportate da Qt6)
# =====================================================================
HUB_QSS = """
QWidget {
    color: #E2E8F0;
    font-family: 'Segoe UI';
    font-size: 13px;
}

QWidget#HubRoot, QStackedWidget#Pages, QWidget#Page {
    background-color: #161920;
}

/* ---------------- SIDEBAR ---------------- */
QFrame#Sidebar {
    background-color: #111318;
    border-right: 1px solid #232730;
}
QLabel#AppTitle {
    font-size: 18px;
    font-weight: bold;
    color: #F8FAFC;
    background-color: transparent;
}
QLabel#EngineStatus {
    background-color: #0F2A22;
    border: 1px solid #14532D;
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#NavSectionHeader {
    color: #64748B;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    padding: 8px 6px 2px 6px;
    background-color: transparent;
}
QPushButton#NavButton {
    min-height: 34px;
    max-height: 34px;
    text-align: left;
    padding: 0 12px;
    border: none;
    border-radius: 6px;
    background-color: transparent;
    color: #94A3B8;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#NavButton:hover {
    background-color: #1A1F29;
    color: #E2E8F0;
}
QPushButton#NavButton:checked {
    background-color: #1E293B;
    color: #38BDF8;
}
QFrame#ShortcutBox {
    background-color: #161A22;
    border: 1px solid #232730;
    border-radius: 10px;
}
QLabel#ShortcutKey {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 2px 8px;
    color: #38BDF8;
    font-weight: bold;
}
QLabel#ShortcutHint {
    color: #94A3B8;
    font-size: 12px;
    background-color: transparent;
}

/* ---------------- HEADER PAGINE ---------------- */
QLabel#PageTitle {
    font-size: 20px;
    font-weight: bold;
    color: #F8FAFC;
}
QLabel#PageSubtitle {
    font-size: 12px;
    color: #94A3B8;
}

/* ---------------- INPUT ---------------- */
QLineEdit {
    min-height: 36px;
    background-color: #1E222B;
    border: 1px solid #2F3542;
    border-radius: 8px;
    padding: 0 12px;
    color: #F1F5F9;
    font-size: 13px;
    selection-background-color: #0284C7;
}
QLineEdit:focus {
    border: 1px solid #38BDF8;
    background-color: #232833;
}
QTextEdit {
    min-height: 120px;
    background-color: #1E222B;
    border: 1px solid #2F3542;
    border-radius: 8px;
    padding: 10px;
    color: #F1F5F9;
    font-size: 13px;
    selection-background-color: #0284C7;
}
QTextEdit:focus {
    border: 1px solid #38BDF8;
}

/* ---------------- PULSANTI ---------------- */
QPushButton {
    min-height: 34px;
    border-radius: 7px;
    padding: 0 14px;
    font-weight: 600;
    background-color: #262B36;
    color: #E2E8F0;
    border: 1px solid #2F3542;
}
QPushButton:hover {
    background-color: #2F3542;
}
QPushButton:pressed {
    background-color: #1E222B;
}
QPushButton[variant="primary"] {
    background-color: #0284C7;
    border: 1px solid #0284C7;
    color: #FFFFFF;
}
QPushButton[variant="primary"]:hover {
    background-color: #0369A1;
    border: 1px solid #0369A1;
}
QPushButton[variant="success"] {
    background-color: #059669;
    border: 1px solid #10B981;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton[variant="success"]:hover {
    background-color: #10B981;
    border: 1px solid #34D399;
}
QPushButton[variant="success"]:disabled {
    background-color: #1E293B;
    border: 1px solid #334155;
    color: #64748B;
}
QPushButton[variant="danger"] {
    background-color: transparent;
    border: 1px solid #3F2A2E;
    color: #F87171;
}
QPushButton[variant="danger"]:hover {
    background-color: #2A1A1D;
}
QPushButton#ModeTab {
    background-color: #1C2029;
    border: 1px solid #2A303C;
    color: #94A3B8;
}
QPushButton#ModeTab:hover {
    color: #E2E8F0;
}
QPushButton#ModeTab:checked {
    background-color: #1E293B;
    border: 1px solid #38BDF8;
    color: #38BDF8;
}
QPushButton#FilterPill {
    background-color: #1A1E26;
    border: 1px solid #282E3B;
    border-radius: 14px;
    color: #94A3B8;
    font-size: 12px;
    font-weight: 600;
    padding: 0 14px;
    min-height: 28px;
    max-height: 28px;
}
QPushButton#FilterPill:hover {
    background-color: #222733;
    color: #E2E8F0;
}
QPushButton#FilterPill:checked {
    background-color: #1E293B;
    border: 1px solid #38BDF8;
    color: #38BDF8;
}

/* ---------------- LISTE SCORREVOLI ---------------- */
QScrollArea {
    background-color: transparent;
    border: none;
}
QWidget#ScrollContent {
    background-color: transparent;
}
QScrollBar:vertical {
    background-color: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background-color: #2F3542;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #3B4252;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background-color: transparent;
}

/* ---------------- CARD ---------------- */
QFrame#Card {
    background-color: #1C2029;
    border: 1px solid #2A303C;
    border-radius: 10px;
}
QFrame#Card QLabel {
    background-color: transparent;
}
QLabel#CardMeta {
    color: #64748B;
    font-size: 11px;
}
QLabel#CardTitle {
    color: #F1F5F9;
    font-weight: 600;
}
QLabel#CardBody {
    color: #CBD5E1;
    font-size: 13px;
}
QLabel#Badge {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 1px 8px;
    color: #7DD3FC;
    font-size: 11px;
    font-weight: 600;
}
QLabel#BadgeMuted {
    background-color: #20242D;
    border: 1px solid #2F3542;
    border-radius: 8px;
    padding: 1px 8px;
    color: #94A3B8;
    font-size: 11px;
}
QLabel#BadgeQuickNote {
    background-color: #2D2210;
    border: 1px solid #B45309;
    border-radius: 8px;
    padding: 1px 8px;
    color: #FBBF24;
    font-size: 11px;
    font-weight: 600;
}
QLabel#EmptyState {
    color: #64748B;
    font-size: 13px;
    padding: 30px;
}

QMessageBox {
    background-color: #161920;
}
"""

# Modalità di dettatura (etichetta UI -> nome stile nel DB llm_styles)
MODE_TABS = [
    ("Naturale", "natural"),
    ("Formale", "formal"),
    ("Code / Tech", "coding"),
    ("Studio / LaTeX", "study_latex"),
    ("Chat", "chat"),
]


def _format_timestamp(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y · %H:%M")
    except ValueError:
        return str(raw)


def _make_button(text: str, variant: Optional[str] = None) -> QPushButton:
    btn = QPushButton(text)
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

        self.content = QWidget()
        self.content.setObjectName("ScrollContent")
        self.vbox = QVBoxLayout(self.content)
        self.vbox.setContentsMargins(0, 0, 6, 0)
        self.vbox.setSpacing(10)
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


class AudioTranscribeThread(QThread):
    progress = pyqtSignal(str, int)  # status_msg, percent
    finished = pyqtSignal(str, str)  # final_markdown, file_name
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

            markdown = target_engine.transcribe_audio_file(
                self.file_path,
                subject_context=self.subject_context,
                progress_callback=on_progress,
                cancel_check=is_cancelled
            )
            if self.cancel_requested:
                self.cancelled.emit()
                return

            file_name = os.path.basename(self.file_path)
            self.finished.emit(markdown, file_name)
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


class QuickNoteDialog(QDialog):
    """Dialogo compatto per creare al volo un Appunto Rapido dall'Hub."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚡ Nuovo Appunto Rapido")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog {
                background-color: #161920;
            }
            QLabel {
                color: #E2E8F0;
                font-family: 'Segoe UI';
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header = QLabel("⚡ Nuovo Appunto Rapido")
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #F8FAFC;")
        layout.addWidget(header)

        sub = QLabel("Annota al volo un pensiero o promemoria. Verrà archiviato nella categoria Appunti Rapidi.")
        sub.setStyleSheet("font-size: 12px; color: #94A3B8;")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        title_lbl = QLabel("Titolo (opzionale):")
        title_lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #CBD5E1;")
        layout.addWidget(title_lbl)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Es. Idea progetto, Promemoria esame...")
        self.title_edit.setMinimumHeight(36)
        layout.addWidget(self.title_edit)

        content_lbl = QLabel("Contenuto dell'appunto:")
        content_lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #CBD5E1;")
        layout.addWidget(content_lbl)

        self.content_edit = QTextEdit()
        self.content_edit.setPlaceholderText("Scrivi o incolla qui il tuo appunto rapido...")
        self.content_edit.setMinimumHeight(120)
        layout.addWidget(self.content_edit)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addStretch(1)

        cancel_btn = _make_button("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_box.addWidget(cancel_btn)

        save_btn = _make_button("⚡ Salva Appunto", "primary")
        save_btn.clicked.connect(self._save_note)
        btn_box.addWidget(save_btn)

        layout.addLayout(btn_box)
        self.content_edit.setFocus()

    def _save_note(self):
        text = self.content_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Testo vuoto", "Inserisci il contenuto dell'appunto prima di salvare.")
            return

        title = self.title_edit.text().strip()
        if not title:
            title = text.split("\n")[0][:45].strip() or "Appunto Rapido"
            if len(title) > 42:
                title = title[:42] + "..."

        new_id = db.add_study_note(
            title=title,
            content_markdown=text,
            note_type="Appunto Rapido",
            tags="Hub, Rapido"
        )
        self.created_note_id = new_id
        self.created_note_title = title
        self.created_note_text = text
        self.accept()


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
        self.setStyleSheet("""
            QFrame#KnowledgeGraphFrame {
                background-color: #0A0D14;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Barra controlli overlay in cima al grafo
        self.toolbar = QFrame()
        self.toolbar.setStyleSheet("""
            QFrame {
                background-color: #0F141F;
                border-bottom: 1px solid #1E293B;
                padding: 4px 8px;
            }
        """)
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
        self.view.setStyleSheet("background-color: #0A0D14;")

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


class FlowLocalHub(QMainWindow):
    note_added = pyqtSignal()

    def __init__(self, on_device_changed: Optional[Callable] = None, engine=None):
        super().__init__()
        # API pubblica mantenuta per compatibilità con main.py / pipeline audio
        self.on_device_changed = on_device_changed
        self.engine = engine
        self.live_volume = 0.0
        self._transcribe_thread = None
        self._import_thread = None
        self._autolink_thread = None
        self._note_cards: Dict[int, QFrame] = {}

        self.setWindowTitle("FlowLocal Hub")
        self.resize(1120, 680)
        self.setMinimumSize(920, 580)
        self.setStyleSheet(HUB_QSS)

        self.note_added.connect(self._refresh_notekeeper)

        self._current_mode_name = MODE_TABS[0][1]
        self._last_transcription_marker = None
        self._last_whis_marker = None

        # Assistente Whis condiviso e monitoraggio
        self.whis_agent = WhisAgent()
        self.whis_hud = None
        self._active_whis_worker = None
        self.wake_listener = None
        self.is_handsfree_enabled = True
        self.on_handsfree_changed: Optional[Callable[[bool], None]] = None
        self.whis_agent.status_changed.connect(self._on_whis_status_changed)
        self.whis_agent.interaction_saved.connect(self._on_whis_interaction_saved)

        # Inizializzazione Gestore Coda Cloud Controllata (Anti-Lag Gaming)
        self.cloud_sync = cloud_sync
        self._cloud_queue_thread = None

        self._build_ui()
        self._refresh_current_page()

        # Aggiornamento leggero della Cronologia: ricostruisce solo se arrivano nuovi dettati
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_new_transcriptions)
        self.poll_timer.start(2500)

        # Timer leggero Coda Cloud (ogni 10s, sola lettura disco, 0 CPU / 0 GPU)
        self.cloud_poll_timer = QTimer(self)
        self.cloud_poll_timer.timeout.connect(self._poll_cloud_queue)
        self.cloud_poll_timer.start(10000)
        self._poll_cloud_queue()

    # -----------------------------------------------------------------
    # API pubblica
    # -----------------------------------------------------------------
    def update_live_volume(self, rms_normalized: float):
        """Chiamato dal thread audio: memorizza solo il valore (nessuna operazione UI cross-thread)."""
        self.live_volume = float(rms_normalized)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_current_page()

    # -----------------------------------------------------------------
    # Struttura principale (I 3 PILASTRI: FlowType, FlowStudy, Whis)
    # -----------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("HubRoot")
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        main_layout.addWidget(self._build_sidebar())

        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self._build_flowtype_page())             # Index 0: 🎙️ FlowType
        self.pages.addWidget(self._build_flowstudy_notekeeper_page()) # Index 1: 📚 FlowStudy - Quaderno 3 Colonne
        self.pages.addWidget(self._build_flowstudy_flashcards_page()) # Index 2: 📚 FlowStudy - Flashcard Esame
        self.pages.addWidget(self._build_flowstudy_graph_page())      # Index 3: 📚 FlowStudy - Grafo Concettuale
        self.pages.addWidget(self._build_whis_page())                 # Index 4: ✨ Whis
        self.pages.addWidget(self._build_dictionary_page())           # Index 5: 📖 Dizionario & Snippet
        self.pages.addWidget(self._build_styles_page())               # Index 6: ⚙️ Stili & Prompt
        main_layout.addWidget(self.pages, 1)

        if 0 in self.nav_buttons_by_page:
            self.nav_buttons_by_page[0].setChecked(True)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 14)
        layout.setSpacing(4)

        title = QLabel("FlowLocal")
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        if GPU_AVAILABLE:
            status = QLabel("● GPU / CUDA Attivo")
            status.setStyleSheet("color: #10B981;")
        else:
            status = QLabel("● CPU (GPU non rilevata)")
            status.setStyleSheet("color: #F59E0B; background-color: #2A2210; border: 1px solid #4A3A12;")
        status.setObjectName("EngineStatus")
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 4, 0, 0)
        status_row.addWidget(status)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: List[QPushButton] = []
        self.nav_buttons_by_page: Dict[int, QPushButton] = {}

        nav_structure = [
            ("🎙️ FLOWTYPE", [
                ("🎙️ Dettatura Live", 0),
                ("📖 Dizionario && Snippet", 5),
                ("⚙️ Stili && Prompt", 6),
            ]),
            ("📚 FLOWSTUDY", [
                ("🗂️ Quaderno && Sbobine", 1),
                ("🃏 Flashcard Esame", 2),
                ("🕸️ Grafo Concettuale", 3),
            ]),
            ("✨ WHIS", [
                ("✨ Assistente Whis", 4),
            ]),
        ]

        btn_seq = 0
        for section_title, buttons in nav_structure:
            sec_lbl = QLabel(section_title)
            sec_lbl.setObjectName("NavSectionHeader")
            layout.addWidget(sec_lbl)
            for btn_label, page_idx in buttons:
                btn = QPushButton(btn_label)
                btn.setObjectName("NavButton")
                btn.setCheckable(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                btn.clicked.connect(lambda _checked, p=page_idx: self._on_nav_clicked(p))
                self.nav_group.addButton(btn, btn_seq)
                self.nav_buttons.append(btn)
                self.nav_buttons_by_page[page_idx] = btn
                layout.addWidget(btn)
                btn_seq += 1

        layout.addStretch(1)

        shortcut_box = QFrame()
        shortcut_box.setObjectName("ShortcutBox")
        sb_layout = QVBoxLayout(shortcut_box)
        sb_layout.setContentsMargins(10, 8, 10, 8)
        sb_layout.setSpacing(4)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key = QLabel("Shift Destro")
        key.setObjectName("ShortcutKey")
        key_row.addWidget(key)
        key_row.addStretch(1)
        sb_layout.addLayout(key_row)

        hint = QLabel("Dettatura rapida: premi o tieni premuto Shift Destro.\nDoppio tocco: mani libere.\nF8: tasto secondario.")
        hint.setObjectName("ShortcutHint")
        hint.setWordWrap(True)
        sb_layout.addWidget(hint)

        layout.addWidget(shortcut_box)
        return sidebar

    def _on_nav_clicked(self, page_idx: int):
        self.pages.setCurrentIndex(page_idx)
        if page_idx in self.nav_buttons_by_page:
            self.nav_buttons_by_page[page_idx].setChecked(True)
        self._refresh_current_page()

    def _refresh_current_page(self):
        idx = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if idx == 0:
            self._refresh_history()
        elif idx == 1:
            self._refresh_notekeeper()
        elif idx == 2:
            self._refresh_flashcards_page()
        elif idx == 3:
            if hasattr(self, "graph_widget"):
                self.graph_widget.load_data()
        elif idx == 4:
            self._refresh_whis_page()
        elif idx == 5:
            self._refresh_dictionary()
        elif idx == 6:
            self._load_mode_prompt(self._current_mode_name)

    @staticmethod
    def _make_page(title: str, subtitle: str) -> (QWidget, QVBoxLayout):
        page = QWidget()
        page.setObjectName("Page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(_make_label(title, "PageTitle"))
        header.addWidget(_make_label(subtitle, "PageSubtitle", wrap=True))
        layout.addLayout(header)
        return page, layout

    # -----------------------------------------------------------------
    # 1. PILASTRO: FLOWTYPE (SCRITTURA VOCALE & DETTATURA LIVE)
    # -----------------------------------------------------------------
    def _build_flowtype_page(self) -> QWidget:
        page, layout = self._make_page("🎙️ FlowType", "Scrittura vocale in tempo reale, telemetria e gestione degli stili di dettatura.")

        # --- 4 MINI-CARD STATISTICHE FLOWTYPE (IN ALTO) ---
        stats_box = QFrame()
        stats_box.setObjectName("FlowTypeStatsBox")
        stats_box.setStyleSheet("QFrame#FlowTypeStatsBox { background-color: transparent; border: none; }")
        st_layout = QHBoxLayout(stats_box)
        st_layout.setContentsMargins(0, 0, 0, 4)
        st_layout.setSpacing(10)

        def _make_stat_card(icon_title: str, default_val: str, subtitle: str, color_val: str = "#38BDF8"):
            card = QFrame()
            card.setStyleSheet("""
                QFrame {
                    background-color: #12151E;
                    border: 1px solid #1E293B;
                    border-radius: 8px;
                }
            """)
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 10, 12, 10)
            c_layout.setSpacing(3)

            lbl_title = QLabel(icon_title)
            lbl_title.setStyleSheet("color: #94A3B8; font-size: 11.5px; font-weight: 600;")
            c_layout.addWidget(lbl_title)

            lbl_val = QLabel(default_val)
            lbl_val.setStyleSheet(f"color: {color_val}; font-size: 18px; font-weight: bold;")
            c_layout.addWidget(lbl_val)

            lbl_sub = QLabel(subtitle)
            lbl_sub.setStyleSheet("color: #64748B; font-size: 10px;")
            c_layout.addWidget(lbl_sub)
            return card, lbl_val

        card_words, self.stat_words_label = _make_stat_card("📝 Parole Totali", "0", "dettate con FlowLocal", "#38BDF8")
        card_wpm, self.stat_wpm_label = _make_stat_card("⚡ Velocità WPM", "150 WPM", "parole al minuto vocali", "#10B981")
        card_time, self.stat_time_label = _make_stat_card("⏱️ Tempo Risparmiato", "0 min", "rispetto a digitazione", "#F59E0B")
        card_streak, self.stat_streak_label = _make_stat_card("🔥 Streak", "0 Giorni", "continuità di dettatura", "#EC4899")

        st_layout.addWidget(card_words)
        st_layout.addWidget(card_wpm)
        st_layout.addWidget(card_time)
        st_layout.addWidget(card_streak)
        layout.addWidget(stats_box)

        # --- SELETTORE RAPIDO STILE DI DETTATURA ---
        style_box = QFrame()
        style_box.setObjectName("FlowTypeStyleBox")
        style_box.setStyleSheet("""
            QFrame#FlowTypeStyleBox {
                background-color: #11141C;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
        """)
        s_layout = QVBoxLayout(style_box)
        s_layout.setContentsMargins(12, 10, 12, 10)
        s_layout.setSpacing(8)

        s_header = QHBoxLayout()
        s_title = QLabel("Stile di Dettatura Rapido:")
        s_title.setStyleSheet("font-size: 12.5px; font-weight: 700; color: #38BDF8;")
        s_header.addWidget(s_title)

        self.flowtype_active_style_label = QLabel("Naturale")
        self.flowtype_active_style_label.setStyleSheet("""
            background-color: #0369A1;
            color: #FFFFFF;
            border-radius: 4px;
            padding: 2px 10px;
            font-size: 11px;
            font-weight: bold;
        """)
        s_header.addWidget(self.flowtype_active_style_label)
        s_header.addStretch(1)
        s_layout.addLayout(s_header)

        pills_layout = QHBoxLayout()
        pills_layout.setSpacing(8)

        self.flowtype_style_group = QButtonGroup(self)
        self.flowtype_style_group.setExclusive(True)

        styles_data = [
            ("Naturale", "natural", "Riformulazione sintattica conservativa e rimozione esitazioni."),
            ("Formale", "formal", "Registro alto per documenti ufficiali, email e relazioni."),
            ("Code / Tech", "code", "Preserva l'italiano e i termini tecnici informatici dettati."),
            ("Studio / LaTeX", "study_latex", "Espressioni matematiche e formule convertite in $...$ LaTeX."),
            ("Chat", "chat", "Discorso informale, punteggiatura snella e ritmo naturale.")
        ]

        self._flowtype_style_buttons = {}
        for s_idx, (s_label, s_code, s_desc) in enumerate(styles_data):
            btn = QPushButton(s_label)
            btn.setObjectName("StylePill")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(s_desc)
            btn.setStyleSheet("""
                QPushButton#StylePill {
                    background-color: #1E2330;
                    color: #94A3B8;
                    border: 1px solid #334155;
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-size: 12px;
                    font-weight: 600;
                    min-height: 28px;
                }
                QPushButton#StylePill:hover {
                    background-color: #273042;
                    color: #F8FAFC;
                }
                QPushButton#StylePill:checked {
                    background-color: #0284C7;
                    color: #FFFFFF;
                    border-color: #38BDF8;
                }
            """)
            btn.clicked.connect(lambda _c=False, code=s_code, lbl=s_label: self._on_flowtype_style_selected(code, lbl))
            self.flowtype_style_group.addButton(btn, s_idx)
            self._flowtype_style_buttons[s_code] = btn
            pills_layout.addWidget(btn)
            if s_idx == 0:
                btn.setChecked(True)

        pills_layout.addStretch(1)
        s_layout.addLayout(pills_layout)
        layout.addWidget(style_box)

        # --- STATO HOTKEY SHIFT DESTRO ---
        hotkey_box = QFrame()
        hotkey_box.setStyleSheet("""
            QFrame {
                background-color: #11141C;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
        """)
        h_layout = QHBoxLayout(hotkey_box)
        h_layout.setContentsMargins(12, 10, 12, 10)
        h_layout.setSpacing(12)

        key_badge = QLabel("Shift Destro")
        key_badge.setStyleSheet("""
            background-color: #1E293B;
            border: 1px solid #38BDF8;
            border-radius: 6px;
            padding: 4px 10px;
            color: #38BDF8;
            font-size: 12px;
            font-weight: bold;
        """)
        h_layout.addWidget(key_badge)

        h_info = QLabel("<b>Push-to-Talk principale:</b> tieni premuto Shift Destro (sotto Invio) per dettare live, rilascia per incollare con zero focus steal.<br>"
                        "<b>Doppio tocco:</b> modalità mani libere. <b>Shift Sinistro:</b> resta 100% libero per le maiuscole ordinarie.")
        h_info.setWordWrap(True)
        h_info.setStyleSheet("color: #94A3B8; font-size: 12px;")
        h_layout.addWidget(h_info, 1)

        layout.addWidget(hotkey_box)

        # --- TOOLBAR RICERCA E SVUOTA CRONOLOGIA ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("🔍 Cerca nelle trascrizioni recenti…")
        self.history_search.setMinimumHeight(36)
        self.history_search.textChanged.connect(self._refresh_history)
        toolbar.addWidget(self.history_search, 1)

        clear_btn = _make_button("🗑️ Svuota cronologia", "danger")
        clear_btn.clicked.connect(self._clear_history)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        # --- LISTA TRASCRIZIONI RECENTI ---
        self.history_list = ScrollList("Nessuna trascrizione ancora. Tieni premuto Shift Destro per iniziare a dettare.")
        layout.addWidget(self.history_list, 1)
        return page

    def _build_history_page(self) -> QWidget:
        """Retrocompatibilità: rimanda a _build_flowtype_page."""
        return self._build_flowtype_page()

    def _on_flowtype_style_selected(self, style_code: str, style_label: str):
        self.flowtype_active_style_label.setText(style_label)
        if self.engine:
            self.engine.current_mode = style_code

    def _refresh_history(self):
        # Aggiornamento statistiche FlowType
        try:
            stats = db.get_statistics()
            total_words = stats.get("total_words", 0)
            saved_min = stats.get("saved_minutes", 0)
            if hasattr(self, "stat_words_label"):
                self.stat_words_label.setText(f"{total_words:,}".replace(",", "."))
            if hasattr(self, "stat_time_label"):
                if saved_min >= 60:
                    self.stat_time_label.setText(f"{saved_min // 60}h {saved_min % 60}m")
                else:
                    self.stat_time_label.setText(f"{saved_min} min")
            if hasattr(self, "stat_wpm_label"):
                wpm = stats.get("speaking_wpm", 150)
                self.stat_wpm_label.setText(f"{wpm} WPM")
            if hasattr(self, "stat_streak_label"):
                streak_days = stats.get("streak_days", 0)
                streak_txt = f"{streak_days} Giorno" if streak_days == 1 else f"{streak_days} Giorni"
                self.stat_streak_label.setText(streak_txt)
        except Exception:
            pass

        query = self.history_search.text().strip().lower() if hasattr(self, "history_search") else ""
        items = db.get_recent_transcriptions(limit=200)
        self._last_transcription_marker = items[0]["id"] if items else None

        if query:
            items = [
                t for t in items
                if query in (t.get("final_text") or "").lower()
                or query in (t.get("target_app") or "").lower()
                or query in (t.get("style_used") or "").lower()
            ]

        self.history_list.clear()
        if not items:
            self.history_list.show_empty("Nessun risultato." if query else None)
            return

        for t in items:
            self.history_list.add_card(self._make_history_card(t))

    def _make_history_card(self, t: Dict) -> QFrame:
        card, layout = _make_card()

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(t.get("created_at")), "CardMeta"))
        if t.get("style_used"):
            meta_row.addWidget(_make_label(t["style_used"], "Badge"))
        if t.get("target_app"):
            meta_row.addWidget(_make_label(t["target_app"], "BadgeMuted"))
        meta_row.addStretch(1)

        text = t.get("final_text") or ""
        copy_btn = _make_button("📋 Copia")
        copy_btn.clicked.connect(lambda _c=False, s=text: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)
        layout.addLayout(meta_row)

        layout.addWidget(_make_label(text, "CardBody", wrap=True))
        return card

    def _clear_history(self):
        reply = QMessageBox.question(
            self, "Svuota cronologia",
            "Eliminare definitivamente tutte le trascrizioni salvate?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.clear_all_transcriptions()
            self._refresh_history()

    def _poll_new_transcriptions(self):
        if not self.isVisible():
            return
        curr_page = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if curr_page == 0:
            latest = db.get_recent_transcriptions(limit=1)
            marker = latest[0]["id"] if latest else None
            if marker != self._last_transcription_marker:
                self._refresh_history()
        elif curr_page == 1:
            latest_whis = db.get_whis_history(limit=1)
            whis_marker = latest_whis[0]["id"] if latest_whis else None
            if whis_marker != getattr(self, "_last_whis_marker", None):
                self._refresh_whis_page()

    # -----------------------------------------------------------------
    # 1b. WHIS
    # -----------------------------------------------------------------
    def set_whis_agent(self, agent: WhisAgent):
        """Assegna l'agente Whis condiviso e collega i listener di stato ed eventi."""
        if hasattr(self, "whis_agent") and self.whis_agent:
            try:
                self.whis_agent.status_changed.disconnect(self._on_whis_status_changed)
                self.whis_agent.interaction_saved.disconnect(self._on_whis_interaction_saved)
            except Exception:
                pass
        self.whis_agent = agent
        if self.whis_agent:
            try:
                self.whis_agent.status_changed.connect(self._on_whis_status_changed)
                self.whis_agent.interaction_saved.connect(self._on_whis_interaction_saved)
                self.whis_agent.note_created.connect(lambda _id, _title: self._refresh_notekeeper())
            except Exception:
                pass
            if hasattr(self, "whis_model_combo"):
                idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
                if idx >= 0:
                    self.whis_model_combo.setCurrentIndex(idx)

    def _build_whis_page(self) -> QWidget:
        page, layout = self._make_page("✨ Whis", "Assistente virtuale ingegneristico, cronologia interazioni e monitoraggio live.")

        # 1. Header & Live Status Panel
        status_frame = QFrame()
        status_frame.setObjectName("WhisStatusFrame")
        status_frame.setStyleSheet("""
            QFrame#WhisStatusFrame {
                background-color: #12141D;
                border: 1px solid #232938;
                border-radius: 10px;
            }
        """)
        sf_layout = QVBoxLayout(status_frame)
        sf_layout.setContentsMargins(14, 12, 14, 12)
        sf_layout.setSpacing(10)

        # Riga 1 (Barra superiore): Status live + Switch Mani Libere + Selettore modalità + Mostra HUD
        row1 = QHBoxLayout()
        row1.setSpacing(12)

        self.whis_status_label = QLabel("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
        self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")
        row1.addWidget(self.whis_status_label, 1)

        # Switch Mani Libere nella barra superiore (Attivo ON di default)
        self.handsfree_switch = QPushButton("🎙️ Mani Libere (\"Di' Whis...\"): ON")
        self.handsfree_switch.setObjectName("HandsfreeSwitch")
        self.handsfree_switch.setCheckable(True)
        self.handsfree_switch.setChecked(self.is_handsfree_enabled)
        self.handsfree_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handsfree_switch.setMinimumHeight(32)
        self.handsfree_switch.toggled.connect(self._on_handsfree_toggled)
        row1.addWidget(self.handsfree_switch)

        # Selettore modalità modello
        lbl_mod = QLabel("Modello:")
        lbl_mod.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 500;")
        row1.addWidget(lbl_mod)

        self.whis_model_combo = QComboBox()
        self.whis_model_combo.setMinimumHeight(32)
        self.whis_model_combo.setStyleSheet("""
            QComboBox {
                min-height: 30px;
                background-color: #1A1E29;
                border: 1px solid #2E3547;
                border-radius: 6px;
                padding: 0 10px;
                color: #F1F5F9;
                font-size: 11px;
                font-weight: 500;
            }
            QComboBox:focus { border: 1px solid #38BDF8; }
            QComboBox QAbstractItemView {
                background-color: #12141D;
                color: #F1F5F9;
                selection-background-color: #0284C7;
                border: 1px solid #2E3547;
            }
        """)
        self.whis_model_combo.addItem("Auto (3B/7B)", "auto")
        self.whis_model_combo.addItem("Rapido (qwen2.5:3b)", "3b")
        self.whis_model_combo.addItem("Profondo (qwen2.5:7b)", "7b")
        if hasattr(self, "whis_agent") and self.whis_agent:
            cur_idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
            if cur_idx >= 0:
                self.whis_model_combo.setCurrentIndex(cur_idx)
        self.whis_model_combo.currentIndexChanged.connect(self._on_whis_model_changed)
        row1.addWidget(self.whis_model_combo)

        # Selettore modalità audio feedback Whis (Voce TTS / Suono Morbido / Muto)
        lbl_audio = QLabel("Audio:")
        lbl_audio.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 500;")
        row1.addWidget(lbl_audio)

        self.whis_audio_combo = QComboBox()
        self.whis_audio_combo.setMinimumHeight(32)
        self.whis_audio_combo.setStyleSheet("""
            QComboBox {
                min-height: 30px;
                background-color: #1A1E29;
                border: 1px solid #2E3547;
                border-radius: 6px;
                padding: 0 10px;
                color: #F1F5F9;
                font-size: 11px;
                font-weight: 500;
            }
            QComboBox:focus { border: 1px solid #38BDF8; }
            QComboBox QAbstractItemView {
                background-color: #12141D;
                color: #F1F5F9;
                selection-background-color: #0284C7;
                border: 1px solid #2E3547;
            }
        """)
        self.whis_audio_combo.addItem("🔇 Muto (Default)", MODE_MUTE)
        self.whis_audio_combo.addItem("🔔 Suono Morbido", MODE_SOFT_CHIME)
        self.whis_audio_combo.addItem("🗣️ Voce (TTS)", MODE_VOICE)

        cur_audio_mode = audio_feedback.get_mode()
        cur_audio_idx = self.whis_audio_combo.findData(cur_audio_mode)
        if cur_audio_idx >= 0:
            self.whis_audio_combo.setCurrentIndex(cur_audio_idx)

        self.whis_audio_combo.currentIndexChanged.connect(self._on_whis_audio_mode_changed)
        audio_feedback.add_mode_listener(self._sync_whis_audio_combo)
        row1.addWidget(self.whis_audio_combo)

        # Selettore profilo vocale Whis
        lbl_voice = QLabel("Voce:")
        lbl_voice.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 500;")
        row1.addWidget(lbl_voice)

        self.whis_voice_combo = QComboBox()
        self.whis_voice_combo.setMinimumHeight(32)
        self.whis_voice_combo.setStyleSheet("""
            QComboBox {
                min-height: 30px;
                background-color: #1A1E29;
                border: 1px solid #2E3547;
                border-radius: 6px;
                padding: 0 10px;
                color: #F1F5F9;
                font-size: 11px;
                font-weight: 500;
            }
            QComboBox:focus { border: 1px solid #38BDF8; }
            QComboBox QAbstractItemView {
                background-color: #12141D;
                color: #F1F5F9;
                selection-background-color: #0284C7;
                border: 1px solid #2E3547;
            }
        """)
        for k, v in VOICE_PRESETS.items():
            self.whis_voice_combo.addItem(v["label"], k)

        cur_voice = audio_feedback.get_voice_profile()
        cur_v_idx = self.whis_voice_combo.findData(cur_voice)
        if cur_v_idx >= 0:
            self.whis_voice_combo.setCurrentIndex(cur_v_idx)

        self.whis_voice_combo.currentIndexChanged.connect(self._on_whis_voice_changed)
        audio_feedback.add_voice_listener(self._sync_whis_voice_combo)
        row1.addWidget(self.whis_voice_combo)

        # Pulsante Prova Voce
        self.whis_voice_test_btn = _make_button("▶️ Prova")
        self.whis_voice_test_btn.setStyleSheet("""
            QPushButton {
                min-height: 30px;
                padding: 0 11px;
                background-color: #1A1E29;
                border: 1px solid #10B981;
                border-radius: 6px;
                color: #10B981;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(16, 185, 129, 0.15);
                color: #FFFFFF;
                border-color: #34D399;
            }
            QPushButton:pressed {
                background-color: #0F172A;
            }
        """)
        self.whis_voice_test_btn.setToolTip("Ascolta un'anteprima vocale con il profilo selezionato")
        self.whis_voice_test_btn.clicked.connect(self._on_whis_voice_test_clicked)
        row1.addWidget(self.whis_voice_test_btn)

        # Pulsante Mostra ultimo HUD
        show_hud_btn = _make_button("🪟 Mostra ultimo HUD")
        show_hud_btn.setStyleSheet("""
            QPushButton {
                min-height: 30px;
                padding: 0 12px;
                background-color: #1A1E29;
                border: 1px solid #38BDF8;
                border-radius: 6px;
                color: #38BDF8;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(56, 189, 248, 0.15);
                color: #FFFFFF;
            }
        """)
        show_hud_btn.clicked.connect(self._show_last_whis_hud)
        row1.addWidget(show_hud_btn)

        sf_layout.addLayout(row1)

        # Riga 2: Integrazione Google Calendar e Sincronizzazione Agenda
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        self.whis_cal_btn = QPushButton("📅 Connetti Google Calendar")
        self.whis_cal_btn.setMinimumHeight(30)
        self.whis_cal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.whis_cal_btn.setStyleSheet("""
            QPushButton {
                min-height: 28px;
                padding: 0 12px;
                background-color: #1A1E29;
                border: 1px solid #38BDF8;
                border-radius: 6px;
                color: #38BDF8;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(56, 189, 248, 0.15);
                color: #FFFFFF;
                border-color: #7DD3FC;
            }
            QPushButton:pressed {
                background-color: #0F172A;
            }
        """)
        self.whis_cal_btn.clicked.connect(self._on_connect_calendar_clicked)
        row2.addWidget(self.whis_cal_btn)

        self.whis_cal_status = QLabel()
        self.whis_cal_status.setStyleSheet("font-size: 11px; font-weight: 500;")
        row2.addWidget(self.whis_cal_status)
        self._update_calendar_status_ui()

        row2.addStretch()
        sf_layout.addLayout(row2)

        self._update_handsfree_ui(self.is_handsfree_enabled)

        layout.addWidget(status_frame)

        # 2. Barra di Ricerca Cronologia
        search_row = QHBoxLayout()
        search_row.setSpacing(10)

        self.whis_search = QLineEdit()
        self.whis_search.setPlaceholderText("🔍 Cerca nelle conversazioni con Whis…")
        self.whis_search.setMinimumHeight(36)
        self.whis_search.textChanged.connect(self._refresh_whis_page)
        search_row.addWidget(self.whis_search, 1)

        clear_btn = _make_button("🗑️ Svuota cronologia", "danger")
        clear_btn.clicked.connect(self._clear_whis_history)
        search_row.addWidget(clear_btn)

        layout.addLayout(search_row)

        # 3. Feed Cronologia Conversazioni
        self.whis_list = ScrollList("Nessuna conversazione con Whis ancora. Usa Shift Destro dicendo 'Whis...' oppure scrivi qui sotto.")
        layout.addWidget(self.whis_list, 1)

        # 4. Input Testuale Rapido in Basso
        input_bar = QHBoxLayout()
        input_bar.setSpacing(10)

        self.whis_input = QLineEdit()
        self.whis_input.setPlaceholderText("Chiedi qualcosa a Whis o incolla del codice…")
        self.whis_input.setMinimumHeight(40)
        self.whis_input.returnPressed.connect(self._send_whis_text_query)
        input_bar.addWidget(self.whis_input, 1)

        self.whis_send_btn = _make_button("Invia ✨", "primary")
        self.whis_send_btn.setMinimumHeight(40)
        self.whis_send_btn.clicked.connect(self._send_whis_text_query)
        input_bar.addWidget(self.whis_send_btn)

        layout.addLayout(input_bar)

        return page

    def _on_whis_model_changed(self):
        mode = self.whis_model_combo.currentData()
        if hasattr(self, "whis_agent") and self.whis_agent:
            self.whis_agent.set_model_mode(mode)

    def _on_whis_audio_mode_changed(self):
        if not hasattr(self, "whis_audio_combo") or not self.whis_audio_combo:
            return
        mode = self.whis_audio_combo.currentData()
        if mode and mode != audio_feedback.get_mode():
            audio_feedback.set_mode(mode)

    def _sync_whis_audio_combo(self, mode: str):
        if not hasattr(self, "whis_audio_combo") or not self.whis_audio_combo:
            return
        idx = self.whis_audio_combo.findData(mode)
        if idx >= 0 and self.whis_audio_combo.currentIndex() != idx:
            self.whis_audio_combo.blockSignals(True)
            self.whis_audio_combo.setCurrentIndex(idx)
            self.whis_audio_combo.blockSignals(False)

    def _on_whis_voice_changed(self):
        if not hasattr(self, "whis_voice_combo") or not self.whis_voice_combo:
            return
        key = self.whis_voice_combo.currentData()
        if key and key != audio_feedback.get_voice_profile():
            audio_feedback.set_voice_profile(key)

    def _sync_whis_voice_combo(self, key: str):
        if not hasattr(self, "whis_voice_combo") or not self.whis_voice_combo:
            return
        idx = self.whis_voice_combo.findData(key)
        if idx >= 0 and self.whis_voice_combo.currentIndex() != idx:
            self.whis_voice_combo.blockSignals(True)
            self.whis_voice_combo.setCurrentIndex(idx)
            self.whis_voice_combo.blockSignals(False)

    def _on_whis_voice_test_clicked(self):
        """Fa pronunciare a Whis la frase di test con la voce selezionata."""
        key = self.whis_voice_combo.currentData() if hasattr(self, "whis_voice_combo") else None
        audio_feedback.preview_voice(key)

    def _update_calendar_status_ui(self):
        """Aggiorna lo stato visivo della connessione a Google Calendar nell'Hub."""
        if not hasattr(self, "whis_cal_status") or not self.whis_cal_status:
            return
        from core.calendar_sync import calendar_manager
        if calendar_manager.is_connected():
            self.whis_cal_status.setText("🟢 Connesso (Pronto a creare e leggere eventi)")
            self.whis_cal_status.setStyleSheet("color: #10B981; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Google Calendar: Connesso")
        elif calendar_manager.has_credentials_file():
            self.whis_cal_status.setText("🟡 credentials.json presente — Clicca per autorizzare")
            self.whis_cal_status.setStyleSheet("color: #F59E0B; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Connetti Google Calendar")
        else:
            self.whis_cal_status.setText("⚪ credentials.json mancante nella cartella principale")
            self.whis_cal_status.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 400;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Connetti Google Calendar")

    def _on_connect_calendar_clicked(self):
        """Gestore clic pulsante autorizzazione Google Calendar."""
        from core.calendar_sync import calendar_manager
        if not calendar_manager.has_credentials_file() and not calendar_manager.has_token_file():
            QMessageBox.information(
                self,
                "Google Calendar — Configurazione Richiesta",
                "Per connettere Google Calendar a Whis:\n\n"
                "1. Vai su Google Cloud Console e crea o scarica le credenziali OAuth (tipo 'Applicazione Desktop').\n"
                "2. Salva il file come 'credentials.json' nella cartella principale del progetto:\n"
                f"{os.getcwd()}\n\n"
                "3. Clicca nuovamente su 'Connetti Google Calendar' per completare l'accesso nel browser."
            )
            return

        self.whis_cal_status.setText("⏳ Connessione in corso nel browser...")
        self.whis_cal_status.setStyleSheet("color: #38BDF8; font-size: 11px;")
        QApplication.processEvents()

        def _auth_thread():
            ok, msg = calendar_manager.authenticate(interactive=True)
            QTimer.singleShot(0, lambda: self._on_calendar_auth_finished(ok, msg))

        threading.Thread(target=_auth_thread, daemon=True, name="GoogleCalendarAuthThread").start()

    def _on_calendar_auth_finished(self, ok: bool, msg: str):
        self._update_calendar_status_ui()
        if ok:
            QMessageBox.information(self, "Google Calendar", "✅ Google Calendar connesso con successo!\nOra puoi chiedere a Whis di creare o leggere i tuoi appuntamenti.")
        else:
            QMessageBox.warning(self, "Google Calendar", f"⚠️ Impossibile completare l'autenticazione:\n{msg}")

    def set_wake_listener(self, listener):
        """Collega il listener hands-free e sincronizza lo stato dello switch UI."""
        if hasattr(self, "wake_listener") and self.wake_listener and hasattr(self.wake_listener, "status_message"):
            try:
                self.wake_listener.status_message.disconnect(self._on_wake_status_message)
            except Exception:
                pass
        self.wake_listener = listener
        if hasattr(listener, "status_message"):
            try:
                listener.status_message.connect(self._on_wake_status_message)
            except Exception:
                pass
        if hasattr(self, "handsfree_switch"):
            is_active = self.is_handsfree_enabled
            self.handsfree_switch.blockSignals(True)
            self.handsfree_switch.setChecked(is_active)
            self.handsfree_switch.blockSignals(False)
            self._update_handsfree_ui(is_active)

    def _on_wake_status_message(self, msg: str):
        if hasattr(self, "whis_status_label") and self.is_handsfree_enabled:
            self.whis_status_label.setText(msg)
            if "rilevato parlato" in msg.lower():
                self.whis_status_label.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: 600;")
            else:
                self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")

    def _on_handsfree_toggled(self, checked: bool):
        """Gestisce il click sullo switch Attivazione Mani Libere."""
        self.is_handsfree_enabled = checked
        if self.wake_listener:
            if checked:
                if not self.wake_listener.isRunning():
                    self.wake_listener.start()
                self.wake_listener.set_paused(False)
            else:
                self.wake_listener.set_paused(True)

        self._update_handsfree_ui(checked)
        if callable(getattr(self, "on_handsfree_changed", None)):
            try:
                self.on_handsfree_changed(checked)
            except Exception:
                pass

    def _update_handsfree_ui(self, active: bool):
        """Aggiorna lo stato visivo dello switch e dei badge informativi."""
        if not hasattr(self, "handsfree_switch"):
            return

        if active:
            self.handsfree_switch.setText("🎙️ Mani Libere (\"Di' Whis...\"): ON")
            self.handsfree_switch.setStyleSheet("""
                QPushButton {
                    background-color: #064E3B;
                    border: 1px solid #10B981;
                    border-radius: 6px;
                    padding: 0 14px;
                    color: #6EE7B7;
                    font-size: 11.5px;
                    font-weight: 700;
                    min-height: 32px;
                }
                QPushButton:hover {
                    background-color: #047857;
                    color: #FFFFFF;
                }
            """)
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
                self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")
        else:
            self.handsfree_switch.setText("🎙️ Mani Libere (\"Di' Whis...\"): OFF")
            self.handsfree_switch.setStyleSheet("""
                QPushButton {
                    background-color: #1A1E29;
                    border: 1px solid #2E3547;
                    border-radius: 6px;
                    padding: 0 14px;
                    color: #94A3B8;
                    font-size: 11.5px;
                    font-weight: 600;
                    min-height: 32px;
                }
                QPushButton:hover {
                    background-color: #242A3A;
                    color: #E2E8F0;
                    border: 1px solid #38BDF8;
                }
            """)
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("🟢 Pronto (In ascolto su Shift Destro + \"Whis...\")")
                self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")

    def _show_last_whis_hud(self):
        if hasattr(self, "whis_hud") and self.whis_hud:
            self.whis_hud.show_latest()

    def _on_whis_status_changed(self, status: str, query: str = ""):
        if not hasattr(self, "whis_status_label"):
            return
        if status in ("In attesa", "Risposta completata", "Pronto"):
            if getattr(self, "is_handsfree_enabled", False):
                self.whis_status_label.setText("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
            else:
                self.whis_status_label.setText("🟢 Pronto (In ascolto su Shift Destro + \"Whis...\")")
            self.whis_status_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 600;")
        else:
            clean_q = f': "{query[:40]}..."' if query else ""
            self.whis_status_label.setText(f"🟣 {status}{clean_q}")
            self.whis_status_label.setStyleSheet("color: #C084FC; font-size: 12px; font-weight: 600;")

    def _on_whis_interaction_saved(self, item: Dict):
        self._refresh_whis_page()

    def _refresh_whis_page(self):
        query = self.whis_search.text().strip().lower() if hasattr(self, "whis_search") else ""
        items = db.get_whis_history(search_query=query, limit=100)
        self._last_whis_marker = items[0]["id"] if items else None

        self.whis_list.clear()
        if not items:
            self.whis_list.show_empty("Nessun risultato trovato." if query else None)
            return

        for item in items:
            self.whis_list.add_card(self._make_whis_card(item))

    def _make_whis_card(self, item: Dict) -> QFrame:
        card, layout = _make_card()
        card.setStyleSheet("""
            QFrame#Card {
                background-color: #141722;
                border: 1px solid #232938;
                border-left: 3px solid #38BDF8;
                border-radius: 8px;
            }
        """)

        # Row 1: Header con timestamp, modello, badge clipboard e azioni
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(item.get("created_at")), "CardMeta"))

        model_str = item.get("model_used") or "3b"
        if "7b" in model_str.lower():
            badge_model = _make_label("🧠 7B Deep", "Badge")
            badge_model.setStyleSheet("background-color: rgba(168, 85, 247, 0.18); color: #C084FC; border: 1px solid rgba(168, 85, 247, 0.4); border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 600;")
            meta_row.addWidget(badge_model)
        else:
            badge_model = _make_label("⚡ 3B Fast", "Badge")
            badge_model.setStyleSheet("background-color: rgba(56, 189, 248, 0.15); color: #38BDF8; border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 600;")
            meta_row.addWidget(badge_model)

        if item.get("clipboard_context"):
            clip_badge = _make_label("📎 Appunti allegati", "BadgeMuted")
            clip_badge.setStyleSheet("background-color: #1E222D; border: 1px solid #2E364A; color: #C084FC; border-radius: 4px; padding: 1px 6px; font-size: 10px;")
            clip_badge.setToolTip(str(item["clipboard_context"])[:300])
            meta_row.addWidget(clip_badge)

        meta_row.addStretch(1)

        whis_resp = item.get("whis_response") or ""
        user_q = item.get("user_query") or ""

        # Pulsante Copia
        copy_btn = _make_button("📋 Copia")
        def on_copy(_c=False, btn=copy_btn, text=whis_resp):
            self._copy_to_clipboard(text)
            btn.setText("✓ Copiato!")
            QTimer.singleShot(2000, lambda: btn.setText("📋 Copia"))
        copy_btn.clicked.connect(on_copy)
        meta_row.addWidget(copy_btn)

        # Pulsante Salva nelle Note
        save_note_btn = _make_button("📝 Salva nelle Note")
        def on_save_note(_c=False, btn=save_note_btn, q=user_q, ans=whis_resp):
            title = f"Whis: {q[:38]}" if q else "Risposta Whis"
            content = f"**Domanda:**\n{q}\n\n**Risposta di Whis:**\n{ans}"
            db.add_study_note(title=title, content_markdown=content, note_type="Note", tags="Whis, Studio")
            btn.setText("✓ Salvata!")
            btn.setEnabled(False)
            self.note_added.emit()
            QTimer.singleShot(2500, lambda: (btn.setText("📝 Salva nelle Note"), btn.setEnabled(True)))
        save_note_btn.clicked.connect(on_save_note)
        meta_row.addWidget(save_note_btn)

        # Pulsante Elimina singola card
        del_btn = _make_button("🗑️", "danger")
        del_btn.setToolTip("Elimina questa conversazione")
        del_btn.clicked.connect(lambda _c=False, item_id=item["id"]: self._delete_whis_interaction(item_id))
        meta_row.addWidget(del_btn)

        layout.addLayout(meta_row)

        # Row 2: Domanda Utente
        q_box = QFrame()
        q_box.setStyleSheet("background-color: #1A1F2C; border: 1px solid #2C354A; border-radius: 6px; padding: 6px 10px;")
        q_layout = QVBoxLayout(q_box)
        q_layout.setContentsMargins(6, 4, 6, 4)
        q_lbl = _make_label(f"💬 {user_q}", "CardTitle", wrap=True)
        q_lbl.setStyleSheet("color: #7DD3FC; font-weight: 600; font-size: 13px;")
        q_layout.addWidget(q_lbl)
        layout.addWidget(q_box)

        # Row 3: Risposta Whis
        resp_lbl = _make_label(whis_resp, "CardBody", wrap=True)
        resp_lbl.setStyleSheet("color: #F1F5F9; font-size: 13px; line-height: 1.45; padding-top: 4px;")
        layout.addWidget(resp_lbl)

        return card

    def _delete_whis_interaction(self, item_id: int):
        db.delete_whis_interaction(item_id)
        self._refresh_whis_page()

    def _clear_whis_history(self):
        reply = QMessageBox.question(
            self, "Svuota cronologia Whis",
            "Eliminare definitivamente tutte le conversazioni salvate con Whis?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.clear_whis_history()
            self._refresh_whis_page()

    def _send_whis_text_query(self):
        query = self.whis_input.text().strip()
        if not query:
            return
        audio_feedback.stop()
        self.whis_input.clear()
        self.whis_send_btn.setEnabled(False)

        if not hasattr(self, "whis_agent") or not self.whis_agent:
            self.whis_agent = WhisAgent()

        self._on_whis_status_changed(f"Elaborazione con {self.whis_agent.select_model(query)}...", query)

        self._active_whis_worker = WhisStreamWorker(self.whis_agent, query)
        def on_finished(full_text, model):
            self.whis_send_btn.setEnabled(True)
            self._on_whis_status_changed("Pronto")
            self._refresh_whis_page()
            audio_feedback.speak_response(full_text)
        def on_error(err):
            self.whis_send_btn.setEnabled(True)
            self._on_whis_status_changed("Errore")
            QMessageBox.warning(self, "Whis", f"Errore durante la richiesta a Whis:\n{err}")

        self._active_whis_worker.finished_response.connect(on_finished)
        self._active_whis_worker.error_occurred.connect(on_error)
        self._active_whis_worker.start()

    # -----------------------------------------------------------------
    # 2. PILASTRO: FLOWSTUDY (NOTEKEEPER A 3 COLONNE & SBOBINE)
    # -----------------------------------------------------------------
    def _build_flowstudy_notekeeper_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        main_vbox = QVBoxLayout(page)
        main_vbox.setContentsMargins(18, 16, 18, 16)
        main_vbox.setSpacing(10)

        # Header Notekeeper
        header = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        header_text.addWidget(_make_label("🗂️ Quaderno & Sbobine", "PageTitle"))
        header_text.addWidget(_make_label("Notekeeper gerarchico: Profilo ➔ Materia ➔ Argomento con editor e flashcard.", "PageSubtitle", wrap=True))
        header.addLayout(header_text)
        header.addStretch(1)

        # Cloud sync badge & button
        self.cloud_badge = QLabel("☁️ Coda Cloud")
        self.cloud_badge.setObjectName("CloudBadge")
        self.cloud_badge.setStyleSheet("""
            QLabel {
                background-color: #131722;
                color: #64748B;
                border: 1px solid #1E293B;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 11px;
                font-weight: 500;
            }
        """)
        header.addWidget(self.cloud_badge)

        self.cloud_process_btn = _make_button("▶️ Elabora Coda", "success")
        self.cloud_process_btn.setEnabled(False)
        self.cloud_process_btn.clicked.connect(self._on_process_cloud_queue_clicked)
        header.addWidget(self.cloud_process_btn)

        self.cloud_sync_btn = _make_button("🔄 Aggiorna Cloud")
        self.cloud_sync_btn.setToolTip("Rigenera outbox_study/library.json con note e flashcard del PC")
        self.cloud_sync_btn.clicked.connect(self._on_sync_cloud_clicked)
        header.addWidget(self.cloud_sync_btn)

        self.export_all_btn = _make_button("Esporta ▾")
        self.export_all_btn.clicked.connect(self._show_export_all_menu)
        header.addWidget(self.export_all_btn)

        main_vbox.addLayout(header)

        # Barra di avanzamento compatta sbobina file
        self.sbobina_progress_box = QFrame()
        self.sbobina_progress_box.setStyleSheet(
            "QFrame { background-color: #131722; border: 1px solid #1E293B; border-radius: 8px; }"
        )
        p_layout = QHBoxLayout(self.sbobina_progress_box)
        p_layout.setContentsMargins(10, 6, 10, 6)
        p_layout.setSpacing(10)

        self.sbobina_status_label = QLabel("Pronto")
        self.sbobina_status_label.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 500;")
        p_layout.addWidget(self.sbobina_status_label, 1)

        self.sbobina_progress_bar = QProgressBar()
        self.sbobina_progress_bar.setRange(0, 100)
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_progress_bar.setFixedHeight(14)
        self.sbobina_progress_bar.setTextVisible(True)
        self.sbobina_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1E293B; border-radius: 7px; text-align: center; color: #FFFFFF; font-size: 10px; font-weight: bold; min-width: 120px; }"
            "QProgressBar::chunk { background-color: #0284C7; border-radius: 7px; }"
        )
        p_layout.addWidget(self.sbobina_progress_bar, 2)

        self.sbobina_cancel_btn = _make_button("✕ Annulla", "danger")
        self.sbobina_cancel_btn.setMinimumHeight(28)
        self.sbobina_cancel_btn.clicked.connect(self._on_cancel_sbobina_clicked)
        p_layout.addWidget(self.sbobina_cancel_btn)

        self.sbobina_progress_box.hide()
        main_vbox.addWidget(self.sbobina_progress_box)

        # ---------------- LAYOUT NOTEKEEPER A 3 COLONNE ----------------
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #1E2330;
                width: 3px;
            }
        """)

        # ===== COLONNA 1: FILTRO PROFILO & ALBERO MATERIE -> ARGOMENTI =====
        col1 = QFrame()
        col1.setStyleSheet("QFrame { background-color: #11141C; border: 1px solid #1E293B; border-radius: 8px; }")
        col1_layout = QVBoxLayout(col1)
        col1_layout.setContentsMargins(10, 10, 10, 10)
        col1_layout.setSpacing(8)

        col1_header = QLabel("📚 Materie & Argomenti")
        col1_header.setStyleSheet("font-weight: bold; font-size: 13px; color: #38BDF8; border: none;")
        col1_layout.addWidget(col1_header)

        # Filtro Profilo
        self.nk_profile_combo = QComboBox()
        self.nk_profile_combo.addItems(["Tutti i profili", "Alessandro", "Giada"])
        self.nk_profile_combo.setMinimumHeight(32)
        self.nk_profile_combo.currentIndexChanged.connect(self._on_nk_profile_changed)
        col1_layout.addWidget(self.nk_profile_combo)

        # Albero espandibile QTreeWidget
        self.nk_tree = QTreeWidget()
        self.nk_tree.setHeaderHidden(True)
        self.nk_tree.setAnimated(True)
        self.nk_tree.setStyleSheet("""
            QTreeWidget {
                background-color: #161A24;
                border: 1px solid #1E2330;
                border-radius: 6px;
                color: #E2E8F0;
                font-size: 12px;
                padding: 4px;
            }
            QTreeWidget::item {
                padding: 5px 6px;
                border-radius: 4px;
            }
            QTreeWidget::item:hover {
                background-color: #1E293B;
            }
            QTreeWidget::item:selected {
                background-color: #0284C7;
                color: #FFFFFF;
            }
        """)
        self.nk_tree.currentItemChanged.connect(self._on_nk_tree_item_changed)
        col1_layout.addWidget(self.nk_tree, 1)

        # Azioni colonna 1
        col1_btn_layout = QVBoxLayout()
        col1_btn_layout.setSpacing(6)
        self.nk_new_sub_btn = _make_button("+ Nuova Materia / Argomento", "primary")
        self.nk_new_sub_btn.setMinimumHeight(32)
        self.nk_new_sub_btn.clicked.connect(self._on_nk_new_subject_clicked)
        col1_btn_layout.addWidget(self.nk_new_sub_btn)

        self.import_slide_btn = _make_button("📄 Importa Slide / PDF")
        self.import_slide_btn.setMinimumHeight(32)
        self.import_slide_btn.clicked.connect(self._on_import_slide_clicked)
        col1_btn_layout.addWidget(self.import_slide_btn)

        self.sbobina_btn = _make_button("🎙️ Sbobina File Audio")
        self.sbobina_btn.setMinimumHeight(32)
        self.sbobina_btn.clicked.connect(self._on_sbobina_clicked)
        col1_btn_layout.addWidget(self.sbobina_btn)
        col1_layout.addLayout(col1_btn_layout)

        splitter.addWidget(col1)

        # ===== COLONNA 2: LISTA DELLE NOTE FILTRATE =====
        col2 = QFrame()
        col2.setStyleSheet("QFrame { background-color: #11141C; border: 1px solid #1E293B; border-radius: 8px; }")
        col2_layout = QVBoxLayout(col2)
        col2_layout.setContentsMargins(10, 10, 10, 10)
        col2_layout.setSpacing(8)

        # Ricerca
        self.nk_search_input = QLineEdit()
        self.nk_search_input.setPlaceholderText("🔍 Cerca nelle note...")
        self.nk_search_input.setMinimumHeight(34)
        self.nk_search_input.textChanged.connect(self._refresh_nk_notes_list)
        col2_layout.addWidget(self.nk_search_input)

        col2_actions = QHBoxLayout()
        col2_actions.setSpacing(6)
        self.nk_new_note_btn = _make_button("✍️ Nuovo Appunto")
        self.nk_new_note_btn.setMinimumHeight(30)
        self.nk_new_note_btn.clicked.connect(self._on_nk_new_note_clicked)
        col2_actions.addWidget(self.nk_new_note_btn)

        self.quick_note_btn = _make_button("⚡ Rapido")
        self.quick_note_btn.setMinimumHeight(30)
        self.quick_note_btn.clicked.connect(self._open_quick_note_dialog)
        col2_actions.addWidget(self.quick_note_btn)
        col2_layout.addLayout(col2_actions)

        # Note list scroll
        self.nk_notes_list = ScrollList("Nessuna nota per l'argomento selezionato.")
        col2_layout.addWidget(self.nk_notes_list, 1)

        splitter.addWidget(col2)

        # ===== COLONNA 3: EDITOR / VIEWER COMPLETO =====
        col3 = QFrame()
        col3.setStyleSheet("QFrame { background-color: #11141C; border: 1px solid #1E293B; border-radius: 8px; }")
        col3_layout = QVBoxLayout(col3)
        col3_layout.setContentsMargins(14, 12, 14, 12)
        col3_layout.setSpacing(8)

        # Titolo edit
        self.nk_editor_title = QLineEdit()
        self.nk_editor_title.setPlaceholderText("Titolo della nota...")
        self.nk_editor_title.setMinimumHeight(36)
        self.nk_editor_title.setStyleSheet("""
            QLineEdit {
                background-color: #181D28;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #FFFFFF;
                font-size: 14px;
                font-weight: bold;
                padding: 4px 10px;
            }
        """)
        col3_layout.addWidget(self.nk_editor_title)

        # Metadata bar: Profilo, Materia, Argomento, Tipo
        meta_grid = QHBoxLayout()
        meta_grid.setSpacing(6)

        self.nk_editor_profile = QComboBox()
        self.nk_editor_profile.addItems(["Alessandro", "Giada"])
        self.nk_editor_profile.setMinimumHeight(32)
        meta_grid.addWidget(self.nk_editor_profile)

        self.nk_editor_subject = QLineEdit()
        self.nk_editor_subject.setPlaceholderText("Materia...")
        self.nk_editor_subject.setMinimumHeight(32)
        meta_grid.addWidget(self.nk_editor_subject)

        self.nk_editor_topic = QLineEdit()
        self.nk_editor_topic.setPlaceholderText("Argomento...")
        self.nk_editor_topic.setMinimumHeight(32)
        meta_grid.addWidget(self.nk_editor_topic)

        self.nk_editor_type = QComboBox()
        self.nk_editor_type.addItems(["appunto", "sbobina", "slide"])
        self.nk_editor_type.setMinimumHeight(32)
        meta_grid.addWidget(self.nk_editor_type)

        col3_layout.addLayout(meta_grid)

        # Markdown Editor Body
        self.nk_editor_body = QTextEdit()
        self.nk_editor_body.setPlaceholderText("Scrivi o incolla qui i tuoi appunti in formato Markdown...")
        self.nk_editor_body.setMinimumHeight(240)
        self.nk_editor_body.setStyleSheet("""
            QTextEdit {
                background-color: #161A24;
                border: 1px solid #1E293B;
                border-radius: 6px;
                color: #F8FAFC;
                font-size: 13px;
                line-height: 1.5;
                padding: 10px;
            }
        """)
        col3_layout.addWidget(self.nk_editor_body, 1)

        # Bottom Actions Toolbar
        editor_actions = QHBoxLayout()
        editor_actions.setSpacing(8)

        self.nk_save_btn = _make_button("💾 Salva Modifiche", "primary")
        self.nk_save_btn.clicked.connect(self._on_nk_save_clicked)
        editor_actions.addWidget(self.nk_save_btn)

        self.nk_fc_btn = _make_button("🧠 Flashcard")
        self.nk_fc_btn.clicked.connect(self._on_nk_extract_flashcards_clicked)
        editor_actions.addWidget(self.nk_fc_btn)

        self.nk_copy_btn = _make_button("📋 Copia")
        self.nk_copy_btn.clicked.connect(lambda: self._copy_to_clipboard(self.nk_editor_body.toPlainText()))
        editor_actions.addWidget(self.nk_copy_btn)

        self.nk_del_btn = _make_button("🗑️ Elimina", "danger")
        self.nk_del_btn.clicked.connect(self._on_nk_delete_clicked)
        editor_actions.addWidget(self.nk_del_btn)

        editor_actions.addStretch(1)

        self.nk_ask_whis_btn = _make_button("✨ Chiedi a Whis", "accent")
        self.nk_ask_whis_btn.clicked.connect(self._on_nk_ask_whis_clicked)
        editor_actions.addWidget(self.nk_ask_whis_btn)

        col3_layout.addLayout(editor_actions)

        splitter.addWidget(col3)

        # Imposta proporzioni splitter iniziali (22%, 33%, 45%)
        splitter.setSizes([230, 320, 470])
        main_vbox.addWidget(splitter, 1)

        self._nk_selected_note_id = None
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None

        return page

    def _build_notes_page(self) -> QWidget:
        """Retrocompatibilità: rimanda a _build_flowstudy_notekeeper_page."""
        return self._build_flowstudy_notekeeper_page()

    def _refresh_notekeeper(self):
        """Ricarica l'albero gerarchico Materie -> Argomenti e la lista note."""
        if not hasattr(self, "nk_tree"):
            return
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text

        tree = db.get_subjects_tree(prof_filter)
        self.nk_tree.blockSignals(True)
        self.nk_tree.clear()

        # Elemento radice Tutte le Materie
        root_item = QTreeWidgetItem(["🌐 Tutte le Materie"])
        root_item.setData(0, Qt.ItemDataRole.UserRole, ("all", "all"))
        root_item.setForeground(0, QBrush(QColor("#38BDF8")))
        font = QFont("Segoe UI", 10)
        font.setBold(True)
        root_item.setFont(0, font)
        self.nk_tree.addTopLevelItem(root_item)

        for subj, topic_list in tree.items():
            s_item = QTreeWidgetItem([f"📚 {subj}"])
            s_item.setData(0, Qt.ItemDataRole.UserRole, (subj, "all"))
            s_font = QFont("Segoe UI", 10)
            s_font.setBold(True)
            s_item.setFont(0, s_font)
            s_item.setForeground(0, QBrush(QColor("#E2E8F0")))

            for t_info in topic_list:
                t_name = t_info["topic"]
                t_cnt = t_info["count"]
                c_item = QTreeWidgetItem([f"🏷️ {t_name} ({t_cnt})"])
                c_item.setData(0, Qt.ItemDataRole.UserRole, (subj, t_name))
                c_item.setForeground(0, QBrush(QColor("#94A3B8")))
                s_item.addChild(c_item)

            self.nk_tree.addTopLevelItem(s_item)
            s_item.setExpanded(True)

        self.nk_tree.setCurrentItem(root_item)
        self.nk_tree.blockSignals(False)

        self._refresh_nk_notes_list()

    def _sync_whis_study_context(self):
        """Sincronizza profilo, materia e argomento correnti con l'agente Whis."""
        if hasattr(self, "whis_agent") and self.whis_agent and hasattr(self.whis_agent, "set_study_context"):
            prof = self.nk_profile_combo.currentText() if hasattr(self, "nk_profile_combo") else "Alessandro"
            self.whis_agent.set_study_context(
                profile="Alessandro" if prof == "Tutti i profili" else prof,
                subject=self._nk_current_filter_subject or "Generale",
                topic=self._nk_current_filter_topic or "Generale"
            )

    def _on_nk_profile_changed(self):
        self._refresh_notekeeper()
        self._sync_whis_study_context()

    def _on_nk_tree_item_changed(self, current, _previous):
        if not current:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        subj, topic = data
        if subj == "all":
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif topic == "all":
            self._nk_current_filter_subject = subj
            self._nk_current_filter_topic = None
        else:
            self._nk_current_filter_subject = subj
            self._nk_current_filter_topic = topic
        self._refresh_nk_notes_list()
        self._sync_whis_study_context()

    def _refresh_nk_notes_list(self):
        if not hasattr(self, "nk_notes_list"):
            return
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text
        sq = self.nk_search_input.text().strip() if hasattr(self, "nk_search_input") else ""

        notes = db.get_notes_by_hierarchy(
            profile=prof_filter,
            subject=self._nk_current_filter_subject,
            topic=self._nk_current_filter_topic,
            search_query=sq
        )

        self.nk_notes_list.clear()
        if not notes:
            self.nk_notes_list.show_empty("Nessuna nota per l'argomento selezionato.")
            return

        for n in notes:
            card = self._make_nk_note_card(n)
            self.nk_notes_list.add_card(card)

        # Se nessuna nota è selezionata, carica la prima nell'editor
        if getattr(self, "_nk_selected_note_id", None) is None and notes:
            self._load_note_into_nk_editor(notes[0]["id"])

    def _make_nk_note_card(self, note: Dict) -> QFrame:
        card = QFrame()
        card.setObjectName("NKNoteCard")
        note_id = note.get("id")
        is_selected = (note_id == getattr(self, "_nk_selected_note_id", None))
        bg = "#1E293B" if is_selected else "#161A24"
        border = "#0284C7" if is_selected else "#1E293B"
        card.setStyleSheet(f"""
            QFrame#NKNoteCard {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px;
            }}
            QFrame#NKNoteCard:hover {{
                border-color: #38BDF8;
                background-color: #1E2330;
            }}
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(4)

        nt = (note.get("note_type") or "appunto").lower()
        badge_lbl = "🎙️ Sbobina" if nt == "sbobina" else ("📄 Slide" if nt == "slide" else "✍️ Appunto")
        top_row.addWidget(_make_label(badge_lbl, "Badge"))

        prof = note.get("profile") or "Alessandro"
        top_row.addWidget(_make_label(prof, "BadgeMuted"))
        top_row.addStretch(1)

        dt = _format_timestamp(note.get("created_at"))
        top_row.addWidget(_make_label(dt, "CardMeta"))
        layout.addLayout(top_row)

        t_lbl = QLabel(note.get("title") or "Senza titolo")
        t_lbl.setWordWrap(True)
        t_lbl.setStyleSheet("color: #FFFFFF; font-size: 13px; font-weight: bold;")
        layout.addWidget(t_lbl)

        content = (note.get("content_markdown") or "").strip()
        preview = (content[:95].replace('\n', ' ') + "...") if len(content) > 95 else content
        if preview:
            p_lbl = QLabel(preview)
            p_lbl.setWordWrap(True)
            p_lbl.setStyleSheet("color: #94A3B8; font-size: 11.5px;")
            layout.addWidget(p_lbl)

        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.mousePressEvent = lambda _e, nid=note_id: self._load_note_into_nk_editor(nid)
        return card

    def _load_note_into_nk_editor(self, note_id: int):
        note = db.get_study_note_by_id(note_id)
        if not note:
            return
        self._nk_selected_note_id = note_id
        if hasattr(self, "nk_editor_title"):
            self.nk_editor_title.setText(note.get("title") or "")
            self.nk_editor_profile.setCurrentText(note.get("profile") or "Alessandro")
            self.nk_editor_subject.setText(note.get("subject") or "Generale")
            self.nk_editor_topic.setText(note.get("topic") or "Generale")
            ntype = (note.get("note_type") or "appunto").lower()
            idx = self.nk_editor_type.findText(ntype, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self.nk_editor_type.setCurrentIndex(idx)
            self.nk_editor_body.setPlainText(note.get("content_markdown") or "")

    def _on_nk_save_clicked(self):
        title = self.nk_editor_title.text().strip() or "Appunto senza titolo"
        prof = self.nk_editor_profile.currentText().strip() or "Alessandro"
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        ntype = self.nk_editor_type.currentText().strip() or "appunto"
        content = self.nk_editor_body.toPlainText().strip()

        if getattr(self, "_nk_selected_note_id", None):
            db.update_study_note(self._nk_selected_note_id, title, content, prof, subj, top, ntype)
        else:
            new_id = db.add_study_note(title, content, ntype, "", None, prof, subj, top)
            self._nk_selected_note_id = new_id

        orig_text = self.nk_save_btn.text()
        self.nk_save_btn.setText("✓ Salvato!")
        QTimer.singleShot(1400, lambda: self.nk_save_btn.setText(orig_text))

        self._refresh_notekeeper()

    def _on_nk_new_note_clicked(self):
        self._nk_selected_note_id = None
        self.nk_editor_title.setText("")
        self.nk_editor_subject.setText(self._nk_current_filter_subject or "Generale")
        self.nk_editor_topic.setText(self._nk_current_filter_topic or "Generale")
        self.nk_editor_body.setPlainText("")
        self.nk_editor_title.setFocus()

    def _on_nk_delete_clicked(self):
        if not getattr(self, "_nk_selected_note_id", None):
            return
        reply = QMessageBox.question(
            self, "Elimina Nota",
            "Vuoi davvero eliminare questa nota e tutte le eventuali flashcard e relazioni associate?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            db.delete_study_note(self._nk_selected_note_id)
            self._nk_selected_note_id = None
            self.nk_editor_title.setText("")
            self.nk_editor_body.setPlainText("")
            self._refresh_notekeeper()

    def _on_nk_ask_whis_clicked(self):
        title = self.nk_editor_title.text().strip()
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        content = self.nk_editor_body.toPlainText().strip()

        if content:
            try:
                pyperclip.copy(content)
            except Exception:
                pass

        self.pages.setCurrentIndex(4)
        if 4 in self.nav_buttons_by_page:
            self.nav_buttons_by_page[4].setChecked(True)
        self._refresh_current_page()

        if hasattr(self, "whis_input"):
            self.whis_input.setText(f"Spiegami questo appunto su {subj} - {top}: {title}")
            self.whis_input.setFocus()

    def _on_nk_new_subject_clicked(self):
        prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        dlg = NewSubjectTopicDialog(self, default_profile=prof)
        if dlg.exec():
            self._refresh_notekeeper()

    def _on_nk_extract_flashcards_clicked(self):
        if not getattr(self, "_nk_selected_note_id", None):
            QMessageBox.warning(self, "Attenzione", "Seleziona o salva prima una nota nell'editor.")
            return
        note = db.get_study_note_by_id(self._nk_selected_note_id)
        if not note:
            return
        self._extract_flashcards_clicked(note, self.nk_fc_btn)

    # -----------------------------------------------------------------
    # FLASHCARD ESAME (PILASTRO FLOWSTUDY - SCHEDA DEDICATA)
    # -----------------------------------------------------------------
    def _build_flowstudy_flashcards_page(self) -> QWidget:
        page, layout = self._make_page("🃏 Flashcard Esame", "Ripasso attivo e interrogazione sui concetti chiave estratti dalle tue sbobine.")

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.fc_search = QLineEdit()
        self.fc_search.setPlaceholderText("🔍 Cerca nelle flashcard d'esame...")
        self.fc_search.setMinimumHeight(36)
        self.fc_search.textChanged.connect(self._refresh_flashcards_page)
        toolbar.addWidget(self.fc_search, 1)

        self.fc_profile_filter = QComboBox()
        self.fc_profile_filter.addItems(["Tutti i profili", "Alessandro", "Giada"])
        self.fc_profile_filter.setMinimumHeight(36)
        self.fc_profile_filter.currentIndexChanged.connect(self._refresh_flashcards_page)
        toolbar.addWidget(self.fc_profile_filter)

        layout.addLayout(toolbar)

        self.fc_list = ScrollList("Nessuna flashcard d'esame presente. Creala da un appunto cliccando '🧠 Flashcard'.")
        layout.addWidget(self.fc_list, 1)
        return page

    def _refresh_flashcards_page(self):
        if not hasattr(self, "fc_list"):
            return
        query = self.fc_search.text().strip().lower() if hasattr(self, "fc_search") else ""
        prof = self.fc_profile_filter.currentText().strip() if hasattr(self, "fc_profile_filter") else "Tutti i profili"

        all_fc = db.get_all_study_notes(note_type="Flashcard")
        if prof != "Tutti i profili":
            all_fc = [f for f in all_fc if (f.get("profile") or "Alessandro") == prof or (prof in (f.get("tags") or ""))]

        if query:
            all_fc = [
                f for f in all_fc
                if query in (f.get("title") or "").lower()
                or query in (f.get("content_markdown") or "").lower()
                or query in (f.get("tags") or "").lower()
            ]

        self.fc_list.clear()
        if not all_fc:
            self.fc_list.show_empty("Nessuna flashcard trovata con i filtri attuali.")
            return

        for fc in all_fc:
            self.fc_list.add_card(self._make_flashcard_study_card(fc))

    def _make_flashcard_study_card(self, fc: Dict) -> QFrame:
        card, layout = _make_card()
        fc_id = fc.get("id")
        title = fc.get("title") or "Domanda d'esame"
        q_text = title.replace("Flashcard:", "").strip()
        content = fc.get("content_markdown") or ""

        ans_text = content
        if "**Domanda:**" in content and "**Risposta:**" in content:
            parts = content.split("**Risposta:**", 1)
            q_part = parts[0].replace("**Domanda:**", "").strip()
            if q_part:
                q_text = q_part
            if len(parts) > 1:
                ans_text = parts[1].strip()

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label("🃏 Flashcard", "Badge"))
        subj = fc.get("subject") or "Generale"
        top = fc.get("topic") or "Generale"
        if subj != "Generale":
            meta_row.addWidget(_make_label(f"📚 {subj}", "BadgeMuted"))
        if top != "Generale":
            meta_row.addWidget(_make_label(f"🏷️ {top}", "BadgeMuted"))
        meta_row.addStretch(1)

        del_btn = _make_button("🗑️", "danger")
        del_btn.setToolTip("Elimina flashcard")
        del_btn.clicked.connect(lambda _c=False, fid=fc_id: self._delete_flashcard_from_page(fid))
        meta_row.addWidget(del_btn)
        layout.addLayout(meta_row)

        q_label = QLabel(f"<b>Domanda:</b> {q_text}")
        q_label.setWordWrap(True)
        q_label.setStyleSheet("color: #38BDF8; font-size: 13.5px; font-weight: 600; margin-top: 4px;")
        layout.addWidget(q_label)

        ans_frame = QFrame()
        ans_frame.setStyleSheet("background-color: #131722; border: 1px dashed #334155; border-radius: 6px; padding: 8px;")
        ans_layout = QVBoxLayout(ans_frame)
        ans_label = QLabel(ans_text)
        ans_label.setWordWrap(True)
        ans_label.setStyleSheet("color: #E2E8F0; font-size: 12.5px; line-height: 1.4;")
        ans_layout.addWidget(ans_label)
        ans_frame.hide()
        layout.addWidget(ans_frame)

        toggle_btn = _make_button("👁️ Mostra Risposta")
        def _toggle(f=ans_frame, b=toggle_btn):
            if f.isVisible():
                f.hide()
                b.setText("👁️ Mostra Risposta")
            else:
                f.show()
                b.setText("🙈 Nascondi Risposta")
        toggle_btn.clicked.connect(_toggle)
        layout.addWidget(toggle_btn)

        return card

    def _delete_flashcard_from_page(self, fid: int):
        db.delete_study_note(fid)
        self._refresh_flashcards_page()

    # -----------------------------------------------------------------
    # GRAFO CONCETTUALE (PILASTRO FLOWSTUDY - OBSIDIAN STYLE)
    # -----------------------------------------------------------------
    def _build_flowstudy_graph_page(self) -> QWidget:
        page, layout = self._make_page("🕸️ Grafo Concettuale", "Visualizzazione delle relazioni tra materie, argomenti, formule e sbobine (Obsidian Style).")

        self.graph_widget = KnowledgeGraphWidget(on_node_click=self._on_graph_node_clicked)
        layout.addWidget(self.graph_widget, 1)
        return page

    def _on_graph_node_clicked(self, note_id: int):
        """Quando si clicca un nodo nel Grafo, passa al Notekeeper e carica la nota nell'editor."""
        self.pages.setCurrentIndex(1)
        if 1 in self.nav_buttons_by_page:
            self.nav_buttons_by_page[1].setChecked(True)
        self._refresh_notekeeper()
        self._load_note_into_nk_editor(note_id)

    def _open_quick_note_dialog(self):
        dialog = QuickNoteDialog(self)
        if dialog.exec():
            self._refresh_notekeeper()
            created_id = getattr(dialog, "created_note_id", None)
            if created_id:
                self._load_note_into_nk_editor(created_id)
                self._start_autolinking(created_id, dialog.created_note_title, dialog.created_note_text)

    def _filtered_notes(self) -> List[Dict]:
        return db.get_notes()

    def _refresh_notes(self):
        self._refresh_notekeeper()

    def _make_note_card(self, n: Dict) -> QFrame:
        card, layout = _make_card()
        note_id = n.get("id")
        note_type = (n.get("note_type") or "").strip()
        is_quick_note = (note_type == "Appunto Rapido" or "rapido" in note_type.lower())

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(n.get("created_at")), "CardMeta"))

        if is_quick_note:
            meta_row.addWidget(_make_label("⚡ Appunto", "BadgeQuickNote"))
        elif note_type:
            badge_text = "🎙️ Sbobina" if note_type.lower() == "sbobina" else note_type
            meta_row.addWidget(_make_label(badge_text, "Badge"))

        if n.get("tags"):
            raw_tags = str(n["tags"])
            # Normalizzazione retrocompatibile tag per visualizzazione ("Profilo: Lei" -> "Profilo: Giada")
            norm_tags = raw_tags.replace("Profilo: Lei", "Profilo: Giada")
            norm_tokens = [(t or "").strip() for t in norm_tags.split(",") if (t or "").strip()]
            norm_tokens = ["Giada" if t in ("Lei", "Profilo: Lei") else t for t in norm_tokens]
            display_tags = ", ".join(norm_tokens)
            meta_row.addWidget(_make_label(display_tags, "BadgeMuted"))
        meta_row.addStretch(1)

        content = n.get("content_markdown") or ""

        copy_btn = _make_button("Copia")
        copy_btn.clicked.connect(lambda _c=False, s=content: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)

        # Il pulsante Flashcard compare solo su Sbobine o Note madri (mai su Appunti Rapidi o Flashcard)
        if not is_quick_note and note_type != "Flashcard":
            flashcard_btn = _make_button("🧠 Flashcard")
            flashcard_btn.clicked.connect(lambda _c=False, note_dict=n, btn=flashcard_btn: self._extract_flashcards_clicked(note_dict, btn))
            meta_row.addWidget(flashcard_btn)

        export_single_btn = _make_button("Esporta ▾")
        export_single_btn.clicked.connect(lambda _c=False, note_dict=n, btn=export_single_btn: self._show_export_single_menu(note_dict, btn))
        meta_row.addWidget(export_single_btn)

        # Gestione anteprima compatta ed espansione su richiesta (solo per Sbobine o Note lunghe, NON per Appunti Rapidi)
        if not is_quick_note:
            is_long = len(content) > 280
            preview_text = (content[:280].rstrip() + "...") if is_long else content
            body_label = _make_label(preview_text, "CardBody", wrap=True)

            if is_long:
                expand_btn = _make_button("Espandi ▼")
                is_expanded = [False]

                def toggle_expand(b=expand_btn, lbl=body_label, full=content, prev=preview_text, state=is_expanded):
                    state[0] = not state[0]
                    if state[0]:
                        lbl.setText(full)
                        b.setText("Comprimi ▲")
                    else:
                        lbl.setText(prev)
                        b.setText("Espandi ▼")

                expand_btn.clicked.connect(toggle_expand)
                meta_row.addWidget(expand_btn)
        else:
            # Per Appunti Rapidi: mostra direttamente il testo completo in formato compatto
            body_label = _make_label(content, "CardBody", wrap=True)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, nid=note_id: self._delete_note(nid))
        meta_row.addWidget(del_btn)
        layout.addLayout(meta_row)

        if n.get("title") and n.get("title") != "Appunto Rapido" and n.get("title") != (content or "").strip():
            layout.addWidget(_make_label(n["title"], "CardTitle", wrap=True))
        layout.addWidget(body_label)

        # Raggruppamento gerarchico flashcard collegate sotto la sbobina madre (solo per Sbobine / Note di studio, escluse Appunti Rapidi)
        if not is_quick_note:
            flashcards = db.get_flashcards_for_note(note_id, n.get("title", ""))
            if flashcards:
                fc_count = len(flashcards)
                fc_toggle_btn = QPushButton(f"🗂️ Flashcard collegate ({fc_count}) ▾")
                fc_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                fc_toggle_btn.setStyleSheet(
                    "QPushButton { background-color: #161D2B; color: #38BDF8; border: 1px solid #1E293B; border-radius: 6px; padding: 6px 12px; font-weight: 600; font-size: 11.5px; text-align: left; }"
                    "QPushButton:hover { background-color: #1E293B; color: #7DD3FC; border-color: #38BDF8; }"
                )
                layout.addWidget(fc_toggle_btn)

                fc_container = QWidget()
                fc_layout = QVBoxLayout(fc_container)
                fc_layout.setContentsMargins(4, 4, 4, 4)
                fc_layout.setSpacing(6)
                fc_container.hide()

                def make_toggle_fc_container(container=fc_container, btn=fc_toggle_btn, cnt=fc_count):
                    def _toggle():
                        if container.isVisible():
                            container.hide()
                            btn.setText(f"🗂️ Flashcard collegate ({cnt}) ▾")
                        else:
                            container.show()
                            btn.setText(f"🗂️ Flashcard collegate ({cnt}) ▴")
                    return _toggle

                fc_toggle_btn.clicked.connect(make_toggle_fc_container())

                for fc in flashcards:
                    fc_content = fc.get("content_markdown") or ""
                    q_text = ""
                    a_text = ""
                    m = re.search(r'\*\*Domanda:\*\*\s*(.*?)\s*\*\*Risposta:\*\*\s*(.*)', fc_content, re.DOTALL | re.IGNORECASE)
                    if not m:
                        m = re.search(r'\*\*D:\*\*\s*(.*?)\s*\*\*R:\*\*\s*(.*)', fc_content, re.DOTALL | re.IGNORECASE)
                    if m:
                        q_text = m.group(1).strip()
                        a_text = m.group(2).strip()
                    else:
                        q_text = fc.get("title", "").replace("Flashcard:", "").strip() or "Domanda d'esame"
                        a_text = fc_content.strip()

                    fc_card = QFrame()
                    fc_card.setStyleSheet(
                        "QFrame { background-color: #0F141F; border: 1px solid #1E293B; border-radius: 6px; padding: 6px 10px; }"
                    )
                    fc_card_layout = QVBoxLayout(fc_card)
                    fc_card_layout.setContentsMargins(8, 6, 8, 6)
                    fc_card_layout.setSpacing(4)

                    q_row = QHBoxLayout()
                    q_label = QLabel(f"❓ {q_text}")
                    q_label.setWordWrap(True)
                    q_label.setStyleSheet("color: #F8FAFC; font-size: 12px; font-weight: bold;")
                    q_row.addWidget(q_label, 1)

                    ans_btn = _make_button("Mostra Risposta")
                    ans_btn.setMinimumHeight(24)
                    ans_btn.setStyleSheet(
                        "QPushButton { background-color: #1E293B; color: #38BDF8; border: 1px solid #334155; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 500; }"
                        "QPushButton:hover { background-color: #283548; color: #7DD3FC; }"
                    )

                    ans_label = QLabel(a_text)
                    ans_label.setWordWrap(True)
                    ans_label.setStyleSheet(
                        "background-color: #162032; color: #CBD5E1; border-left: 3px solid #10B981; padding: 6px 10px; border-radius: 4px; font-size: 11.5px; margin-top: 4px;"
                    )
                    ans_label.hide()

                    def make_ans_toggle(lbl=ans_label, b=ans_btn):
                        def _toggle():
                            if lbl.isVisible():
                                lbl.hide()
                                b.setText("Mostra Risposta")
                            else:
                                lbl.show()
                                b.setText("Nascondi Risposta")
                        return _toggle

                    ans_btn.clicked.connect(make_ans_toggle())
                    q_row.addWidget(ans_btn)

                    del_fc_btn = QPushButton("✕")
                    del_fc_btn.setFixedSize(22, 22)
                    del_fc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    del_fc_btn.setToolTip("Elimina questa flashcard")
                    del_fc_btn.setStyleSheet(
                        "QPushButton { background-color: transparent; color: #EF4444; border: 1px solid #7F1D1D; border-radius: 4px; font-size: 10px; font-weight: bold; }"
                        "QPushButton:hover { background-color: #7F1D1D; color: #FFFFFF; }"
                    )
                    del_fc_btn.clicked.connect(lambda _c=False, fid=fc["id"]: self._delete_flashcard(fid))
                    q_row.addWidget(del_fc_btn)

                    fc_card_layout.addLayout(q_row)
                    fc_card_layout.addWidget(ans_label)
                    fc_layout.addWidget(fc_card)

                layout.addWidget(fc_container)

        # Barra compatta Collegamenti & Backlink (stile Obsidian)
        links_box = QFrame()
        links_box.setObjectName("NoteLinksBox")
        links_box.setStyleSheet("""
            QFrame#NoteLinksBox {
                background-color: #0E131F;
                border: 1px solid #1E293B;
                border-radius: 6px;
                padding: 4px 8px;
                margin-top: 4px;
            }
        """)
        links_row = QHBoxLayout(links_box)
        links_row.setContentsMargins(6, 4, 6, 4)
        links_row.setSpacing(6)

        lbl_icon = QLabel("🔗 Collegamenti:")
        lbl_icon.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: bold;")
        links_row.addWidget(lbl_icon)

        note_links = db.get_linked_notes(note_id)
        if note_links:
            for lk in note_links:
                is_out = (lk.get("direction") == "outgoing")
                dir_arrow = "↗" if is_out else "↙"
                type_icon = "🎙️" if (lk.get("note_type") or "").lower() == "sbobina" else "⚡"
                raw_t = (lk.get("title") or "Nota").replace("Sbobina:", "").strip()
                trunc_t = (raw_t[:20] + "…") if len(raw_t) > 20 else raw_t

                pill_btn = QPushButton(f"{dir_arrow} {type_icon} {trunc_t}")
                pill_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                dir_label = "Collegamento in uscita" if is_out else "Backlink in entrata"
                pill_btn.setToolTip(
                    f"🔗 [{dir_label}]\n"
                    f"Titolo: {lk.get('title')}\n"
                    f"Tipo: {lk.get('note_type', 'Nota')}\n"
                    f"Motivo: {lk.get('reason') or 'Correlazione concettuale'}\n\n"
                    f"(Clicca per scorrere ed evidenziare la nota)"
                )
                pill_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #162032;
                        color: #7DD3FC;
                        border: 1px solid #0369A1;
                        border-radius: 11px;
                        padding: 2px 9px;
                        font-size: 11px;
                        font-weight: 500;
                    }
                    QPushButton:hover {
                        background-color: #0284C7;
                        color: #FFFFFF;
                        border-color: #38BDF8;
                    }
                """)
                target_nid = lk.get("note_id")
                pill_btn.clicked.connect(lambda _c=False, tid=target_nid: self._scroll_to_and_highlight_note(tid))

                # Menu contestuale tasto destro per eliminare il collegamento
                pill_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                def make_pill_menu(btn=pill_btn, lid=lk["link_id"], note_t=lk.get("title")):
                    def _show_menu(pos):
                        menu = QMenu(self)
                        menu.setStyleSheet("""
                            QMenu {
                                background-color: #1A1F29;
                                color: #F8FAFC;
                                border: 1px solid #2D3545;
                                padding: 4px;
                                border-radius: 6px;
                            }
                            QMenu::item:selected {
                                background-color: #EF4444;
                            }
                        """)
                        del_act = menu.addAction(f"Elimina collegamento con '{note_t[:22]}...'")
                        del_act.triggered.connect(lambda: self._delete_note_link(lid))
                        menu.exec(btn.mapToGlobal(pos))
                    return _show_menu
                pill_btn.customContextMenuRequested.connect(make_pill_menu())
                links_row.addWidget(pill_btn)
        else:
            no_link_lbl = QLabel("Nessuno")
            no_link_lbl.setStyleSheet("color: #64748B; font-size: 11px; font-style: italic;")
            links_row.addWidget(no_link_lbl)

        # Pulsante + Collega manuale
        add_link_btn = QPushButton("+ Collega")
        add_link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_link_btn.setToolTip("Aggiungi un collegamento manuale verso un'altra nota o sbobina")
        add_link_btn.setStyleSheet("""
            QPushButton {
                background-color: #1E293B;
                color: #94A3B8;
                border: 1px dashed #334155;
                border-radius: 11px;
                padding: 2px 9px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #283548;
                color: #38BDF8;
                border-color: #38BDF8;
            }
        """)
        add_link_btn.clicked.connect(lambda _c=False, nid=note_id, t=n.get("title", ""): self._show_connect_note_dialog(nid, t))
        links_row.addWidget(add_link_btn)

        # Pulsante 🪄 Auto-Link semantico con Qwen
        autolink_btn = QPushButton("🪄 Auto-Link")
        autolink_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        autolink_btn.setToolTip("Cerca e collega automaticamente fino a 3 note correlate con Ollama Qwen")
        autolink_btn.setStyleSheet("""
            QPushButton {
                background-color: #1E293B;
                color: #C084FC;
                border: 1px dashed #6B21A8;
                border-radius: 11px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #3B0764;
                color: #E9D5FF;
                border-color: #C084FC;
            }
        """)
        autolink_btn.clicked.connect(lambda _c=False, nid=note_id, t=n.get("title", ""), c=content: self._auto_link_single_note(nid, t, c, autolink_btn))
        links_row.addWidget(autolink_btn)

        links_row.addStretch(1)
        layout.addWidget(links_box)

        self._note_cards[note_id] = card
        return card

    def _delete_flashcard(self, flashcard_id: int):
        db.delete_study_note(flashcard_id)
        self._refresh_notes()

    def _delete_note(self, note_id: int):
        db.delete_study_note(note_id)
        self._refresh_notes()
        if hasattr(self, "graph_widget"):
            self.graph_widget.load_data()

    def _delete_note_link(self, link_id: int):
        db.delete_note_link(link_id)
        self._refresh_notes()
        if hasattr(self, "graph_widget"):
            self.graph_widget.load_data()

    def _show_connect_note_dialog(self, source_id: int, source_title: str):
        dlg = ConnectNoteDialog(self, source_id, source_title)
        if dlg.exec():
            self._refresh_notes()
            if hasattr(self, "graph_widget"):
                self.graph_widget.load_data()

    def _start_autolinking(self, note_id: int, title: str, content: str, btn: Optional[QPushButton] = None):
        """Avvia l'auto-linking asincrono in background per non bloccare la UI."""
        self._autolink_thread = AutoLinkThread(note_id, title, content)
        def _on_done(nid, count):
            if btn is not None:
                btn.setEnabled(True)
                btn.setText("🪄 Auto-Link")
            if count > 0:
                self._refresh_notes()
                if hasattr(self, "graph_widget"):
                    self.graph_widget.load_data()
        self._autolink_thread.finished.connect(_on_done)
        self._autolink_thread.start()

    def _auto_link_single_note(self, note_id: int, title: str, content: str, btn: QPushButton):
        btn.setEnabled(False)
        btn.setText("⏳ Analisi...")
        self._start_autolinking(note_id, title, content, btn=btn)

    def _set_notes_view_mode(self, mode: int):
        """0 = Vista Quaderno Notekeeper (Pagina 1), 1 = Vista Grafo Concettuale (Pagina 3)."""
        target_page = 3 if mode == 1 else 1
        self.pages.setCurrentIndex(target_page)
        if target_page in getattr(self, "nav_buttons_by_page", {}):
            self.nav_buttons_by_page[target_page].setChecked(True)
        if mode == 1 and hasattr(self, "graph_widget"):
            self.graph_widget.load_data()
        elif mode == 0:
            self._refresh_notekeeper()

    def _on_graph_node_clicked(self, note_id: int):
        """Quando si clicca un nodo nel Grafo, passa al Notekeeper e carica la nota nell'editor."""
        self.pages.setCurrentIndex(1)
        if 1 in getattr(self, "nav_buttons_by_page", {}):
            self.nav_buttons_by_page[1].setChecked(True)
        self._refresh_notekeeper()
        self._load_note_into_nk_editor(note_id)

    def _scroll_to_and_highlight_note(self, target_note_id: int):
        """Passa al Notekeeper e carica la nota nell'editor."""
        self._on_graph_node_clicked(target_note_id)

    def _on_sbobina_clicked(self):
        if self._transcribe_thread and self._transcribe_thread.isRunning():
            QMessageBox.information(self, "Sbobinatura in corso", "Una sbobinatura è già in fase di elaborazione.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleziona file audio o video per sbobinatura",
            "",
            "File Audio e Video (*.mp3 *.m4a *.wav *.ogg *.mp4 *.mov *.mkv *.webm);;File Audio (*.mp3 *.m4a *.wav *.ogg);;File Video (*.mp4 *.mov *.mkv *.webm);;Tutti i file (*.*)"
        )
        if not file_path:
            return

        # Prompt opzionale per contesto e glossario della materia
        subject_context, ok = QInputDialog.getText(
            self,
            "Contesto / Materia della lezione",
            "Inserisci la materia o parole chiave (es. Diritto Sanitario, tirocinio, Legge Gelli-Bianco, colpa generica, imperizia, dolo):"
        )
        if not ok:
            subject_context = ""

        self.sbobina_btn.setEnabled(False)
        self.sbobina_btn.setText("⏳ In corso...")

        # Mostra barra di avanzamento e resetta stato
        self.sbobina_status_label.setText("Inizializzazione job...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()

        self._transcribe_thread = AudioTranscribeThread(
            file_path=file_path,
            subject_context=subject_context or "",
            engine=self.engine
        )
        self._transcribe_thread.progress.connect(self._on_sbobina_progress)
        self._transcribe_thread.finished.connect(self._on_sbobina_finished)
        self._transcribe_thread.error.connect(self._on_sbobina_error)
        self._transcribe_thread.cancelled.connect(self._on_sbobina_cancelled)
        self._transcribe_thread.start()

    def _on_sbobina_progress(self, msg: str, percent: int = -1):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)
            self.sbobina_btn.setText(f"⏳ {percent}%")
        else:
            self.sbobina_btn.setText("⏳ In corso...")

    def _on_cancel_sbobina_clicked(self):
        if self._transcribe_thread and self._transcribe_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento sbobinatura in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            self._transcribe_thread.cancel()
        if self._import_thread and self._import_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento importazione slide in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            try:
                self._import_thread.terminate()
            except Exception:
                pass
            self.sbobina_progress_box.hide()
            if hasattr(self, "import_slide_btn"):
                self.import_slide_btn.setEnabled(True)
        if self._cloud_queue_thread and self._cloud_queue_thread.isRunning():
            self.sbobina_status_label.setText("Annullamento coda cloud in corso...")
            self.sbobina_cancel_btn.setEnabled(False)
            self._cloud_queue_thread.cancel()

    # -----------------------------------------------------------------
    # GESTORE CODA CLOUD CONTROLLATA (ANTI-LAG GAMING)
    # -----------------------------------------------------------------
    def _poll_cloud_queue(self):
        """Timer 10s: aggiorna il badge della coda cloud (sola lettura disco, zero CPU/GPU)."""
        try:
            count = self.cloud_sync.get_pending_count()
            if hasattr(self, "cloud_badge"):
                if count > 0:
                    self.cloud_badge.setText(f"☁️ Coda Cloud ({count} in attesa)")
                    self.cloud_badge.setStyleSheet("""
                        QLabel {
                            background-color: #1E3A8A;
                            color: #60A5FA;
                            border: 1px solid #3B82F6;
                            border-radius: 6px;
                            padding: 6px 14px;
                            font-size: 12px;
                            font-weight: 700;
                        }
                    """)
                    is_busy = (
                        (self._cloud_queue_thread and self._cloud_queue_thread.isRunning())
                        or (self._transcribe_thread and self._transcribe_thread.isRunning())
                    )
                    self.cloud_process_btn.setEnabled(not is_busy)
                else:
                    self.cloud_badge.setText("☁️ Coda Cloud (0 in attesa)")
                    self.cloud_badge.setStyleSheet("""
                        QLabel {
                            background-color: #131722;
                            color: #64748B;
                            border: 1px solid #1E293B;
                            border-radius: 6px;
                            padding: 6px 14px;
                            font-size: 12px;
                            font-weight: 500;
                        }
                    """)
                    is_queue_running = self._cloud_queue_thread and self._cloud_queue_thread.isRunning()
                    if not is_queue_running:
                        self.cloud_process_btn.setEnabled(False)
        except Exception:
            pass

    def _on_process_cloud_queue_clicked(self):
        """Avvia l'elaborazione manuale sequenziale dei file in inbox_audio/."""
        if (self._cloud_queue_thread and self._cloud_queue_thread.isRunning()) or (
            self._transcribe_thread and self._transcribe_thread.isRunning()
        ):
            QMessageBox.information(
                self, "Elaborazione in corso", "Un'altra operazione acustica o di coda è già attiva."
            )
            return

        pending = self.cloud_sync.get_pending_queue()
        if not pending:
            QMessageBox.information(
                self,
                "Coda vuota",
                f"Nessun file audio trovato in attesa nella cartella:\n{self.cloud_sync.get_inbox_audio_dir()}",
            )
            return

        total = len(pending)
        reply = QMessageBox.question(
            self,
            "Avvia Elaborazione Coda Cloud",
            f"Trovati {total} file audio in attesa.\n\n"
            f"Verranno sbobinati sequenzialmente con Qwen 2.5 7B, salvati nel DB, "
            f"esportati in PDF/DOCX e archiviati in processed/.\n\n"
            f"Vuoi procedere ora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.cloud_process_btn.setEnabled(False)
        self.cloud_process_btn.setText("⏳ In elaborazione...")
        self.sbobina_btn.setEnabled(False)

        self.sbobina_status_label.setText(f"Avvio Coda Cloud: 0/{total} completati...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()

        self._cloud_queue_thread = CloudQueueProcessThread(engine=self.engine)
        self._cloud_queue_thread.progress.connect(self._on_cloud_queue_progress)
        self._cloud_queue_thread.finished.connect(self._on_cloud_queue_finished)
        self._cloud_queue_thread.error.connect(self._on_cloud_queue_error)
        self._cloud_queue_thread.cancelled.connect(self._on_cloud_queue_cancelled)
        self._cloud_queue_thread.start()

    def _on_cloud_queue_progress(self, msg: str, percent: int = -1):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)

    def _on_cloud_queue_finished(self, count: int, results: list):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        self._refresh_notes()

        msg = (
            f"Elaborazione completata per {count} lezioni audio dalla Coda Cloud!\n\n"
            f"• Sbobine e Flashcard d'esame registrate nel DB\n"
            f"• File PDF e DOCX esportati in 'outbox_study/'\n"
            f"• Indice 'library.json' aggiornato per la PWA Mobile\n"
            f"• File audio spostati in 'inbox_audio/processed/'"
        )
        QMessageBox.information(self, "Coda Cloud Completata", msg)

    def _on_cloud_queue_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        QMessageBox.critical(
            self, "Errore Coda Cloud", f"Si è verificato un errore durante l'elaborazione:\n{err_msg}"
        )

    def _on_cloud_queue_cancelled(self):
        self.sbobina_progress_box.hide()
        self.cloud_process_btn.setEnabled(True)
        self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        self._refresh_notes()
        QMessageBox.information(self, "Coda Annullata", "L'elaborazione della coda cloud è stata interrotta.")

    def _on_sync_cloud_clicked(self):
        """Rigenera manualmente outbox_study/library.json."""
        try:
            data = self.cloud_sync.generate_library_json()
            out_path = os.path.join(self.cloud_sync.get_outbox_study_dir(), "library.json")
            total_notes = data["stats"]["total_notes"]
            total_fc = data["stats"]["total_flashcards"]
            QMessageBox.information(
                self,
                "Cloud Sincronizzato",
                f"Indice libreria per PWA Mobile aggiornato con successo!\n\n"
                f"• Percorso: {out_path}\n"
                f"• Note & Sbobine: {total_notes}\n"
                f"• Flashcard: {total_fc}\n\n"
                f"Google Drive Desktop sincronizzerà il file sul cloud.",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Errore Aggiornamento Cloud", f"Impossibile aggiornare library.json:\n{e}"
            )

    def _on_cloud_folder_clicked(self):
        """Permette di visualizzare o cambiare la cartella sincronizzata da Google Drive."""
        current_dir = self.cloud_sync.get_sync_base_dir()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Seleziona cartella sincronizzata Google Drive (FlowLocal_Sync)",
            current_dir,
        )
        if chosen and os.path.exists(chosen):
            self.cloud_sync.set_sync_base_dir(chosen)
            self._poll_cloud_queue()
            QMessageBox.information(
                self,
                "Cartella Cloud Impostata",
                f"Cartella principale impostata su:\n{chosen}\n\n"
                f"Sottocartelle verificate:\n"
                f"• inbox_audio/\n"
                f"• inbox_audio/processed/\n"
                f"• outbox_study/",
            )

    def _on_sbobina_cancelled(self):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina File Audio")
        QMessageBox.information(
            self,
            "Sbobinatura Annullata",
            "L'operazione di sbobinatura è stata interrotta con successo."
        )

    def _on_sbobina_finished(self, markdown: str, file_name: str):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina File Audio")

        # Salva la sbobina nella tabella study_notes del database
        title = f"Sbobina: {file_name}"
        new_id = db.add_study_note(
            title=title,
            content_markdown=markdown,
            note_type="Sbobina",
            tags=f"Audio, {file_name}"
        )
        self._refresh_notes()
        self._start_autolinking(new_id, title, markdown)

        QMessageBox.information(
            self,
            "Sbobinatura completata",
            f"La lezione di '{file_name}' è stata sbobinata e salvata nelle tue Note con successo!"
        )

    def _on_sbobina_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina File Audio")
        QMessageBox.critical(
            self,
            "Errore Sbobinatura",
            f"Si è verificato un errore durante la sbobinatura dell'audio:\n{err_msg}"
        )

    def _on_import_slide_clicked(self):
        """Apre un selettore file e avvia l'importazione di PDF o immagini con OCR nel Notekeeper."""
        if self._import_thread and self._import_thread.isRunning():
            QMessageBox.information(self, "Importazione in corso", "Un'importazione di slide o documenti è già in fase di elaborazione.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleziona Slide o Documento PDF",
            "",
            "Documenti e Immagini (*.pdf *.png *.jpg *.jpeg *.webp *.bmp *.txt *.md);;File PDF (*.pdf);;Immagini (*.png *.jpg *.jpeg *.webp *.bmp);;Tutti i file (*.*)"
        )
        if not file_path:
            return

        # Rileva profilo attivo dal selettore
        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        profile = "Alessandro" if prof_text == "Tutti i profili" else prof_text

        # Rileva materia e argomento correnti dall'albero Notekeeper
        subject = self._nk_current_filter_subject or "Generale"
        topic = self._nk_current_filter_topic or "Generale"

        # Attiva barra di avanzamento
        file_name = os.path.basename(file_path)
        self.sbobina_status_label.setText(f"Inizializzazione importazione: {file_name}...")
        self.sbobina_progress_bar.setValue(0)
        self.sbobina_cancel_btn.setEnabled(True)
        self.sbobina_progress_box.show()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(False)

        self._import_thread = DocumentImportThread(
            file_path=file_path,
            profile=profile,
            subject=subject,
            topic=topic
        )
        self._import_thread.progress.connect(self._on_import_slide_progress)
        self._import_thread.finished.connect(self._on_import_slide_finished)
        self._import_thread.error.connect(self._on_import_slide_error)
        self._import_thread.start()

    def _on_import_slide_progress(self, msg: str, percent: int):
        self.sbobina_status_label.setText(msg)
        if percent >= 0:
            self.sbobina_progress_bar.setValue(percent)

    def _on_import_slide_finished(self, result: dict):
        self.sbobina_progress_box.hide()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(True)

        self._refresh_notekeeper()
        note_id = result.get("id")
        if note_id:
            self._load_note_into_nk_editor(note_id)

        pages = result.get("pages", 1)
        ocr_pages = result.get("ocr_pages", 0)
        ocr_info = f" (di cui {ocr_pages} con OCR)" if ocr_pages > 0 else ""
        QMessageBox.information(
            self,
            "Slide Importate con Successo",
            f"Documento «{result.get('title', 'Slide')}» importato nel Quaderno!\n\n"
            f"• Pagine elaborate: {pages}{ocr_info}\n"
            f"• Materia: {result.get('subject')}\n"
            f"• Argomento: {result.get('topic')}\n"
            f"• Profilo: {result.get('profile')}"
        )

    def _on_import_slide_error(self, err_msg: str):
        self.sbobina_progress_box.hide()
        if hasattr(self, "import_slide_btn"):
            self.import_slide_btn.setEnabled(True)
        QMessageBox.critical(
            self,
            "Errore Importazione Slide",
            f"Si è verificato un errore durante l'importazione del documento:\n\n{err_msg}"
        )

    def _extract_flashcards_clicked(self, note_dict: Dict, btn: QPushButton):
        content = note_dict.get("content_markdown") or ""
        title = note_dict.get("title") or "Sbobina"
        note_id = note_dict.get("id")
        if not content.strip():
            QMessageBox.warning(self, "Nota vuota", "La nota non contiene testo sufficiente per estrarre flashcard.")
            return

        btn.setEnabled(False)
        btn.setText("⏳ Estrazione...")

        self._flashcard_thread = ExtractFlashcardsThread(note_id, content, title)
        self._flashcard_thread.finished.connect(lambda count, t, b=btn: self._on_flashcards_finished(count, t, b))
        self._flashcard_thread.error.connect(lambda err, b=btn: self._on_flashcards_error(err, b))
        self._flashcard_thread.start()

    def _on_flashcards_finished(self, count: int, note_title: str, btn: QPushButton):
        btn.setEnabled(True)
        btn.setText("🧠 Flashcard")
        self._refresh_notes()
        QMessageBox.information(
            self,
            "Flashcard Generate",
            f"Sono state create ed estratte con successo {count} flashcard d'esame nel tuo database per '{note_title}'!"
        )

    def _on_flashcards_error(self, err_msg: str, btn: QPushButton):
        btn.setEnabled(True)
        btn.setText("🧠 Flashcard")
        QMessageBox.warning(
            self,
            "Errore Flashcard",
            f"Impossibile generare le flashcard:\n{err_msg}"
        )

    def _show_export_all_menu(self):
        notes = self._filtered_notes()
        if not notes:
            QMessageBox.information(self, "Esporta note", "Non ci sono note o sbobine da esportare.")
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1A1F29; color: #F8FAFC; border: 1px solid #2D3545; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; font-size: 13px; }"
            "QMenu::item:selected { background-color: #0284C7; }"
        )

        md_act = menu.addAction("📄 Markdown (.md)")
        docx_act = menu.addAction("📘 Documento Word (.docx)")
        pdf_act = menu.addAction("📕 Documento PDF (.pdf)")

        chosen = menu.exec(self.export_all_btn.mapToGlobal(self.export_all_btn.rect().bottomLeft()))
        if not chosen:
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        if chosen == md_act:
            default_name = f"FlowLocal_Note_{timestamp}.md"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Markdown (*.md)")
            if path:
                try:
                    export_to_md(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Markdown:\n{e}")

        elif chosen == docx_act:
            default_name = f"FlowLocal_Note_{timestamp}.docx"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Documento Word (*.docx)")
            if path:
                try:
                    export_to_docx(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Word:\n{e}")

        elif chosen == pdf_act:
            default_name = f"FlowLocal_Note_{timestamp}.pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Documento PDF (*.pdf)")
            if path:
                try:
                    export_to_pdf(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in PDF:\n{e}")

    def _show_export_single_menu(self, note_dict: Dict, btn_widget: QWidget):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1A1F29; color: #F8FAFC; border: 1px solid #2D3545; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; font-size: 13px; }"
            "QMenu::item:selected { background-color: #0284C7; }"
        )

        md_act = menu.addAction("📄 Markdown (.md)")
        docx_act = menu.addAction("📘 Documento Word (.docx)")
        pdf_act = menu.addAction("📕 Documento PDF (.pdf)")

        chosen = menu.exec(btn_widget.mapToGlobal(btn_widget.rect().bottomLeft()))
        if not chosen:
            return

        raw_title = (note_dict.get("title") or "Nota").replace("Sbobina: ", "").strip()
        safe_title = re.sub(r'[\/:*?"<>|]', '_', raw_title)[:40] or "Sbobina"
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')

        if chosen == md_act:
            default_name = f"{safe_title}_{timestamp}.md"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Markdown (*.md)")
            if path:
                try:
                    export_to_md(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Markdown:\n{e}")

        elif chosen == docx_act:
            default_name = f"{safe_title}_{timestamp}.docx"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Documento Word (*.docx)")
            if path:
                try:
                    export_to_docx(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in Word:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Word:\n{e}")

        elif chosen == pdf_act:
            default_name = f"{safe_title}_{timestamp}.pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta nota", default_name, "Documento PDF (*.pdf)")
            if path:
                try:
                    export_to_pdf(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Nota esportata in PDF:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in PDF:\n{e}")

    # -----------------------------------------------------------------
    # 3. DIZIONARIO
    # -----------------------------------------------------------------
    def _build_dictionary_page(self) -> QWidget:
        page, layout = self._make_page(
            "Dizionario",
            "Correzioni applicate a ogni dettatura e glossario usato per guidare Whisper.",
        )

        form = QHBoxLayout()
        form.setSpacing(10)
        self.dict_alias_input = QLineEdit()
        self.dict_alias_input.setPlaceholderText("Parola errata / alias (es. antigraffiti)")
        self.dict_alias_input.setMinimumHeight(36)
        form.addWidget(self.dict_alias_input, 1)

        arrow = QLabel("→")
        arrow.setStyleSheet("color: #64748B; font-size: 16px;")
        form.addWidget(arrow)

        self.dict_term_input = QLineEdit()
        self.dict_term_input.setPlaceholderText("Parola corretta (es. Antigravity)")
        self.dict_term_input.setMinimumHeight(36)
        self.dict_term_input.returnPressed.connect(self._add_dictionary_rule)
        form.addWidget(self.dict_term_input, 1)

        add_btn = _make_button("Aggiungi", "primary")
        add_btn.clicked.connect(self._add_dictionary_rule)
        form.addWidget(add_btn)
        layout.addLayout(form)

        self.dict_list = ScrollList("Il dizionario è vuoto.")
        layout.addWidget(self.dict_list, 1)
        return page

    def _refresh_dictionary(self):
        items = db.get_all_vocabulary()
        self.dict_list.clear()
        if not items:
            self.dict_list.show_empty()
            return
        for v in items:
            self.dict_list.add_card(self._make_dictionary_card(v))

    def _make_dictionary_card(self, v: Dict) -> QFrame:
        card, layout = _make_card()
        row = QHBoxLayout()
        row.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        aliases = (v.get("phonetic_aliases") or "").strip()
        rule = f"{aliases}  →  {v['correct_term']}" if aliases else v["correct_term"]
        text_col.addWidget(_make_label(rule, "CardTitle", wrap=True))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(v.get("category") or "Generale", "BadgeMuted"))
        if not aliases:
            meta_row.addWidget(_make_label("solo glossario", "CardMeta"))
        meta_row.addStretch(1)
        text_col.addLayout(meta_row)
        row.addLayout(text_col, 1)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, vid=v["id"]: self._delete_dictionary_rule(vid))
        row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(row)
        return card

    def _add_dictionary_rule(self):
        alias = self.dict_alias_input.text().strip()
        term = self.dict_term_input.text().strip()
        if not term:
            self.dict_term_input.setFocus()
            return

        if alias:
            db.learn_vocabulary_pair(alias, term)
        else:
            existing = {v["correct_term"].lower() for v in db.get_all_vocabulary()}
            if term.lower() not in existing:
                db.add_vocabulary(term, "", "Manuale")

        self.dict_alias_input.clear()
        self.dict_term_input.clear()
        self.dict_alias_input.setFocus()
        self._refresh_dictionary()

    def _delete_dictionary_rule(self, vocab_id: int):
        db.delete_vocabulary(vocab_id)
        self._refresh_dictionary()

    # -----------------------------------------------------------------
    # 4. STILI & PROMPT
    # -----------------------------------------------------------------
    def _build_styles_page(self) -> QWidget:
        page, layout = self._make_page(
            "Stili & Prompt",
            "Seleziona una modalità e modifica il relativo System Prompt.",
        )

        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(8)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for idx, (label, name) in enumerate(MODE_TABS):
            btn = QPushButton(label)
            btn.setObjectName("ModeTab")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, n=name: self._load_mode_prompt(n))
            self.mode_group.addButton(btn, idx)
            tabs_row.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)
        tabs_row.addStretch(1)
        layout.addLayout(tabs_row)

        self.prompt_editor = QTextEdit()
        self.prompt_editor.setAcceptRichText(False)
        self.prompt_editor.setMinimumHeight(110)
        self.prompt_editor.setPlaceholderText("System Prompt della modalità selezionata…")
        self.prompt_editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.prompt_editor, 1)

        footer = QHBoxLayout()
        self.prompt_status = _make_label("", "PageSubtitle")
        footer.addWidget(self.prompt_status, 1)
        save_btn = _make_button("Salva Prompt", "primary")
        save_btn.clicked.connect(self._save_mode_prompt)
        footer.addWidget(save_btn)
        layout.addLayout(footer)
        return page

    def _load_mode_prompt(self, mode_name: str):
        self._current_mode_name = mode_name
        style = db.get_style_by_name(mode_name)
        if style:
            self.prompt_editor.setPlainText(style.get("system_prompt") or "")
            self.prompt_status.setText(style.get("display_name") or "")
        else:
            self.prompt_editor.setPlainText("")
            self.prompt_status.setText("Nessuno stile salvato per questa modalità: verrà creato al salvataggio.")

    def _save_mode_prompt(self):
        prompt = self.prompt_editor.toPlainText().strip()
        if not prompt:
            self.prompt_status.setText("Il prompt non può essere vuoto.")
            return

        style = db.get_style_by_name(self._current_mode_name)
        if style:
            db.update_style_prompt(style["id"], prompt, style.get("auto_target_apps") or "")
        else:
            label = next((l for l, n in MODE_TABS if n == self._current_mode_name), self._current_mode_name)
            conn = db._get_connection()
            conn.execute(
                "INSERT INTO llm_styles (name, display_name, description, system_prompt, auto_target_apps, is_active) "
                "VALUES (?, ?, '', ?, '', 0)",
                (self._current_mode_name, label, prompt),
            )
            conn.commit()

        self.prompt_status.setText(f"Salvato · {datetime.now().strftime('%H:%M:%S')}")

    # -----------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------
    @staticmethod
    def _copy_to_clipboard(text: str):
        QApplication.clipboard().setText(text or "")
