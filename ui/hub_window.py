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
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from database import db

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
QPushButton#NavButton {
    min-height: 40px;
    max-height: 40px;
    text-align: left;
    padding: 0 14px;
    border: none;
    border-radius: 8px;
    background-color: transparent;
    color: #94A3B8;
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


class FlowLocalHub(QMainWindow):
    def __init__(self, on_device_changed: Optional[Callable] = None):
        super().__init__()
        # API pubblica mantenuta per compatibilità con main.py / pipeline audio
        self.on_device_changed = on_device_changed
        self.live_volume = 0.0

        self.setWindowTitle("FlowLocal Hub")
        self.resize(960, 640)
        self.setMinimumSize(820, 540)
        self.setStyleSheet(HUB_QSS)

        self._current_mode_name = MODE_TABS[0][1]
        self._last_transcription_marker = None

        self._build_ui()
        self._refresh_current_page()

        # Aggiornamento leggero della Cronologia: ricostruisce solo se arrivano nuovi dettati
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_new_transcriptions)
        self.poll_timer.start(2500)

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
    # Struttura principale
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
        self.pages.addWidget(self._build_history_page())
        self.pages.addWidget(self._build_notes_page())
        self.pages.addWidget(self._build_dictionary_page())
        self.pages.addWidget(self._build_styles_page())
        main_layout.addWidget(self.pages, 1)

        self.nav_buttons[0].setChecked(True)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(6)

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

        layout.addSpacing(22)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: List[QPushButton] = []
        for idx, label in enumerate(["Cronologia", "Note", "Dizionario", "Stili && Prompt"]):
            btn = QPushButton(label)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _checked, i=idx: self._on_nav_clicked(i))
            self.nav_group.addButton(btn, idx)
            self.nav_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch(1)

        shortcut_box = QFrame()
        shortcut_box.setObjectName("ShortcutBox")
        sb_layout = QVBoxLayout(shortcut_box)
        sb_layout.setContentsMargins(12, 10, 12, 10)
        sb_layout.setSpacing(6)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key = QLabel("Caps Lock")
        key.setObjectName("ShortcutKey")
        key_row.addWidget(key)
        key_row.addStretch(1)
        sb_layout.addLayout(key_row)

        hint = QLabel("Tieni premuto per dettare.\nDoppio tocco: mani libere.\nShift+Caps: Bloc Maiusc.")
        hint.setObjectName("ShortcutHint")
        hint.setWordWrap(True)
        sb_layout.addWidget(hint)

        layout.addWidget(shortcut_box)
        return sidebar

    def _on_nav_clicked(self, idx: int):
        self.pages.setCurrentIndex(idx)
        self._refresh_current_page()

    def _refresh_current_page(self):
        idx = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if idx == 0:
            self._refresh_history()
        elif idx == 1:
            self._refresh_notes()
        elif idx == 2:
            self._refresh_dictionary()
        elif idx == 3:
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
    # 1. CRONOLOGIA
    # -----------------------------------------------------------------
    def _build_history_page(self) -> QWidget:
        page, layout = self._make_page("Cronologia", "Le tue ultime dettature, con modalità e app di destinazione.")

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Cerca nelle trascrizioni…")
        self.history_search.setMinimumHeight(36)
        self.history_search.textChanged.connect(self._refresh_history)
        toolbar.addWidget(self.history_search, 1)

        clear_btn = _make_button("Svuota", "danger")
        clear_btn.clicked.connect(self._clear_history)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        self.history_list = ScrollList("Nessuna trascrizione ancora. Tieni premuto Alt Gr per iniziare.")
        layout.addWidget(self.history_list, 1)
        return page

    def _refresh_history(self):
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
        copy_btn = _make_button("Copia")
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
        if not self.isVisible() or self.pages.currentIndex() != 0:
            return
        latest = db.get_recent_transcriptions(limit=1)
        marker = latest[0]["id"] if latest else None
        if marker != self._last_transcription_marker:
            self._refresh_history()

    # -----------------------------------------------------------------
    # 2. NOTE
    # -----------------------------------------------------------------
    def _build_notes_page(self) -> QWidget:
        page, layout = self._make_page("Note", "Appunti di studio e flashcard salvati dalla dettatura.")

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.notes_search = QLineEdit()
        self.notes_search.setPlaceholderText("Cerca nelle note…")
        self.notes_search.setMinimumHeight(36)
        self.notes_search.textChanged.connect(self._refresh_notes)
        toolbar.addWidget(self.notes_search, 1)

        export_btn = _make_button("Esporta (.md)")
        export_btn.clicked.connect(self._export_notes)
        toolbar.addWidget(export_btn)
        layout.addLayout(toolbar)

        self.notes_list = ScrollList("Nessuna nota salvata. Detta in un'app di studio (Obsidian, Notion, Overleaf…) per crearne.")
        layout.addWidget(self.notes_list, 1)
        return page

    def _filtered_notes(self) -> List[Dict]:
        query = self.notes_search.text().strip().lower()
        notes = db.get_notes()
        if query:
            notes = [
                n for n in notes
                if query in (n.get("title") or "").lower()
                or query in (n.get("content_markdown") or "").lower()
                or query in (n.get("tags") or "").lower()
            ]
        return notes

    def _refresh_notes(self):
        if not hasattr(self, "notes_list"):
            return
        notes = self._filtered_notes()
        self.notes_list.clear()
        if not notes:
            self.notes_list.show_empty("Nessun risultato." if self.notes_search.text().strip() else None)
            return
        for n in notes:
            self.notes_list.add_card(self._make_note_card(n))

    def _make_note_card(self, n: Dict) -> QFrame:
        card, layout = _make_card()

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(n.get("created_at")), "CardMeta"))
        if n.get("note_type"):
            meta_row.addWidget(_make_label(n["note_type"], "Badge"))
        if n.get("tags"):
            meta_row.addWidget(_make_label(n["tags"], "BadgeMuted"))
        meta_row.addStretch(1)

        content = n.get("content_markdown") or ""
        copy_btn = _make_button("Copia")
        copy_btn.clicked.connect(lambda _c=False, s=content: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, nid=n["id"]: self._delete_note(nid))
        meta_row.addWidget(del_btn)
        layout.addLayout(meta_row)

        if n.get("title"):
            layout.addWidget(_make_label(n["title"], "CardTitle", wrap=True))
        layout.addWidget(_make_label(content, "CardBody", wrap=True))
        return card

    def _delete_note(self, note_id: int):
        db.delete_study_note(note_id)
        self._refresh_notes()

    def _export_notes(self):
        notes = self._filtered_notes()
        if not notes:
            QMessageBox.information(self, "Esporta note", "Non ci sono note da esportare.")
            return

        default_name = f"FlowLocal_Note_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        path, _ = QFileDialog.getSaveFileName(self, "Esporta note", default_name, "Markdown (*.md)")
        if not path:
            return

        parts = ["# FlowLocal — Note\n"]
        for n in notes:
            parts.append(f"## {n.get('title') or 'Nota'}")
            meta = " · ".join(x for x in (_format_timestamp(n.get("created_at")), n.get("note_type"), n.get("tags")) if x)
            if meta:
                parts.append(f"*{meta}*\n")
            parts.append((n.get("content_markdown") or "").strip() + "\n")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))
        except OSError as e:
            QMessageBox.warning(self, "Esporta note", f"Impossibile salvare il file:\n{e}")

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
