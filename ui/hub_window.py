"""
FlowLocalHub (PyQt6)
Centro di controllo desktop (1080x700) con estetica Obsidian Dark (Linear/Raycast).
TopBar con telemetria hardware in tempo reale (RTX 5060 via NVML), Sidebar fissa e QStackedWidget a 6 schede:
1. Panoramica
2. Vocabolario & Jargon
3. Snippet Vocali
4. Stili LLM & Studio
5. Studio, Note & Flashcard
6. Impostazioni Audio & Hotkeys
"""

import os
import sounddevice as sd
import pyperclip
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QTextEdit, QComboBox, QFrame, QRadioButton, QButtonGroup, QScrollArea,
    QProgressBar, QMessageBox, QAbstractItemView, QSplitter
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont

from database import db

# Telemetria GPU tramite NVML (senza warning)
GPU_NAME = "NVIDIA GeForce RTX 5060"
nvml_handle = None
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pynvml
        pynvml.nvmlInit()
        nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        GPU_NAME = pynvml.nvmlDeviceGetName(nvml_handle)
except Exception:
    nvml_handle = None


OBSIDIAN_QSS = """
QMainWindow, QWidget {
    background-color: #090A0F;
    color: #F8FAFC;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 13px;
}

/* TOP BAR */
QFrame#topbar {
    background-color: #0E1017;
    border-bottom: 1px solid #1F2230;
    min-height: 48px;
    max-height: 48px;
    padding: 0 16px;
}

/* SIDEBAR */
QFrame#sidebar {
    background-color: #12131A;
    border-right: 1px solid #1F2230;
    min-width: 220px;
    max-width: 220px;
}

QPushButton.nav-btn {
    text-align: left;
    padding: 10px 14px;
    background-color: transparent;
    color: #94A3B8;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
}

QPushButton.nav-btn:hover {
    background-color: #1A1B24;
    color: #F8FAFC;
}

QPushButton.nav-btn:checked {
    background-color: #1E2030;
    color: #38BDF8;
    border-left: 3px solid #38BDF8;
    font-weight: 600;
}

/* CARDS */
QFrame.card {
    background-color: #12131A;
    border: 1px solid #1F2230;
    border-radius: 8px;
    padding: 16px;
}

QFrame.card-highlight {
    background-color: #12131A;
    border: 1px solid #6366F1;
    border-radius: 8px;
    padding: 16px;
}

/* TITOLI & TESTO */
QLabel.page-title {
    font-size: 20px;
    font-weight: 700;
    color: #F8FAFC;
}

QLabel.page-desc {
    font-size: 13px;
    color: #94A3B8;
}

QLabel.stat-val {
    font-size: 26px;
    font-weight: 700;
    color: #F8FAFC;
}

QLabel.stat-lbl {
    font-size: 11px;
    color: #94A3B8;
    font-weight: 600;
    letter-spacing: 0.5px;
}

/* INPUTS */
QLineEdit, QTextEdit, QComboBox {
    background-color: #161822;
    border: 1px solid #232738;
    border-radius: 6px;
    padding: 8px 12px;
    color: #F8FAFC;
    selection-background-color: #6366F1;
}

QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border: 1px solid #38BDF8;
    background-color: #181A26;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

/* PULSANTI */
QPushButton.btn-primary {
    background-color: #6366F1;
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 600;
}

QPushButton.btn-primary:hover {
    background-color: #4F46E5;
}

QPushButton.btn-secondary {
    background-color: #1F2230;
    color: #F8FAFC;
    border: 1px solid #2D3142;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 500;
}

QPushButton.btn-secondary:hover {
    background-color: #272B3C;
}

QPushButton.btn-danger {
    background-color: #7F1D1D;
    color: #FCA5A5;
    border: 1px solid #991B1B;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 500;
}

QPushButton.btn-danger:hover {
    background-color: #991B1B;
    color: #FFFFFF;
}

/* TABELLE */
QTableWidget {
    background-color: #12131A;
    border: 1px solid #1F2230;
    border-radius: 6px;
    gridline-color: #1A1B24;
    color: #F8FAFC;
    selection-background-color: #1E2030;
    selection-color: #38BDF8;
}

QHeaderView::section {
    background-color: #161822;
    color: #94A3B8;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #1F2230;
    font-weight: 600;
    font-size: 12px;
}

QScrollBar:vertical {
    background: #090A0F;
    width: 8px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: #1F2230;
    border-radius: 4px;
    min-height: 20px;
}

QScrollBar::handle:vertical:hover {
    background: #38BDF8;
}

QProgressBar {
    background-color: #161822;
    border: 1px solid #1F2230;
    border-radius: 4px;
    text-align: center;
    color: #F8FAFC;
    height: 12px;
}

QProgressBar::chunk {
    background-color: #10B981;
    border-radius: 3px;
}
"""


class FlowLocalHub(QMainWindow):
    def __init__(self, on_device_changed: Optional[callable] = None):
        super().__init__()
        self.on_device_changed = on_device_changed
        self.setWindowTitle("FlowLocal Hub — AI Voice & Study Workspace")
        self.resize(1080, 700)
        self.setMinimumSize(920, 600)
        self.setStyleSheet(OBSIDIAN_QSS)

        self._build_ui()
        self._refresh_all_views()

        # Timer telemetria e statistiche live
        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self._update_hardware_telemetry)
        self.telemetry_timer.start(2000)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---------------------------------------------------------------------
        # TOP BAR: Brand & Telemetria Live GPU
        # ---------------------------------------------------------------------
        topbar = QFrame()
        topbar.setObjectName("topbar")
        tbl = QHBoxLayout(topbar)
        tbl.setContentsMargins(16, 0, 16, 0)
        tbl.setSpacing(12)

        logo_lbl = QLabel("🎙️ FLOW LOCAL")
        logo_lbl.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        logo_lbl.setStyleSheet("color: #F8FAFC; letter-spacing: 1.5px;")

        hub_badge = QLabel("STUDIO & WORKSPACE")
        hub_badge.setStyleSheet("background-color: #1E2030; color: #38BDF8; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: bold;")

        tbl.addWidget(logo_lbl)
        tbl.addWidget(hub_badge)
        tbl.addStretch()

        # Badge Hardware GPU & Modelli
        self.badge_gpu = QLabel("🟢 RTX 5060: Inizializzazione...")
        self.badge_gpu.setStyleSheet("background-color: #161822; color: #10B981; border: 1px solid #1F2230; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-family: monospace;")

        self.badge_models = QLabel("⚡ Whisper Large-v3-Turbo + Qwen 2.5")
        self.badge_models.setStyleSheet("background-color: #161822; color: #818CF8; border: 1px solid #1F2230; padding: 4px 10px; border-radius: 6px; font-size: 11px;")

        tbl.addWidget(self.badge_gpu)
        tbl.addWidget(self.badge_models)
        main_layout.addWidget(topbar)

        # ---------------------------------------------------------------------
        # BODY: Sidebar + QStackedWidget
        # ---------------------------------------------------------------------
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # 1. Sidebar di navigazione
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(14, 18, 14, 18)
        sb_layout.setSpacing(6)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        self.btn_home = QPushButton("⚡  Panoramica")
        self.btn_vocab = QPushButton("📖  Vocabolario & Jargon")
        self.btn_snippets = QPushButton("🪄  Snippet Vocali")
        self.btn_styles = QPushButton("🧠  Stili LLM & Studio")
        self.btn_study = QPushButton("📚  Studio, Note & Flashcard")
        self.btn_audio = QPushButton("⚙️  Audio & Hotkeys")

        nav_items = [
            (self.btn_home, 0),
            (self.btn_vocab, 1),
            (self.btn_snippets, 2),
            (self.btn_styles, 3),
            (self.btn_study, 4),
            (self.btn_audio, 5),
        ]

        for btn, idx in nav_items:
            btn.setCheckable(True)
            btn.setProperty("class", "nav-btn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav_group.addButton(btn, idx)
            sb_layout.addWidget(btn)

        self.nav_group.idClicked.connect(self._on_nav_clicked)
        self.btn_home.setChecked(True)

        sb_layout.addStretch()

        # Status Footer
        hw_box = QFrame()
        hw_box.setStyleSheet("background-color: #161822; border: 1px solid #1F2230; border-radius: 6px; padding: 10px;")
        hw_layout = QVBoxLayout(hw_box)
        hw_layout.setContentsMargins(6, 6, 6, 6)
        hw_layout.setSpacing(3)
        hw_lbl1 = QLabel("100% Locale & Privato")
        hw_lbl1.setStyleSheet("color: #10B981; font-weight: 600; font-size: 11px;")
        hw_lbl2 = QLabel("Zero dati inviati al Cloud")
        hw_lbl2.setStyleSheet("color: #94A3B8; font-size: 10px;")
        hw_layout.addWidget(hw_lbl1)
        hw_layout.addWidget(hw_lbl2)
        sb_layout.addWidget(hw_box)

        body_layout.addWidget(sidebar)

        # 2. Area Schede QStackedWidget
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background-color: #090A0F; padding: 22px;")

        self.view_home = self._create_home_view()
        self.view_vocab = self._create_vocab_view()
        self.view_snippets = self._create_snippets_view()
        self.view_styles = self._create_styles_view()
        self.view_study = self._create_study_view()
        self.view_audio = self._create_audio_view()

        self.stack.addWidget(self.view_home)      # 0
        self.stack.addWidget(self.view_vocab)     # 1
        self.stack.addWidget(self.view_snippets)  # 2
        self.stack.addWidget(self.view_styles)    # 3
        self.stack.addWidget(self.view_study)     # 4
        self.stack.addWidget(self.view_audio)     # 5

        body_layout.addWidget(self.stack)
        main_layout.addWidget(body)

    def _update_hardware_telemetry(self):
        if nvml_handle:
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(nvml_handle)
                temp = pynvml.nvmlDeviceGetTemperature(nvml_handle, pynvml.NVML_TEMPERATURE_GPU)
                used_gb = mem.used / (1024**3)
                tot_gb = mem.total / (1024**3)
                self.badge_gpu.setText(f"🟢 RTX 5060: {temp}°C | VRAM {used_gb:.1f}/{tot_gb:.1f} GB")
            except Exception:
                pass

    def _on_nav_clicked(self, idx: int):
        self.stack.setCurrentIndex(idx)
        if idx == 0:
            self._refresh_home_stats()
        elif idx == 1:
            self._refresh_vocab_table()
        elif idx == 2:
            self._refresh_snippets_table()
        elif idx == 3:
            self._refresh_styles_view()
        elif idx == 4:
            self._refresh_study_view()

    # =========================================================================
    # VISTA 1: PANORAMICA (HOME)
    # =========================================================================
    def _create_home_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(16)

        # Hero Banner
        hero = QFrame()
        hero.setProperty("class", "card")
        hl = QVBoxLayout(hero)
        t = QLabel("Bentornato su FlowLocal")
        t.setProperty("class", "page-title")
        sub = QLabel("Il tuo assistente vocale e secondo cervello 100% offline. Premi 'Alt Gr' per parlare ovunque.")
        sub.setProperty("class", "page-desc")
        hl.addWidget(t)
        hl.addWidget(sub)
        vbox.addWidget(hero)

        # 3 KPI Cards Principali
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)

        self.kpi_words = self._make_stat_card("PAROLE / TEMPO RISPARMIATO", "0 parole", "#38BDF8")
        self.kpi_latency = self._make_stat_card("LATENZA GPU COMBINATA", "0 ms", "#10B981")
        self.kpi_jargon = self._make_stat_card("TERMINI JARGON ATTIVI", "8 attivi", "#6366F1")

        stats_layout.addWidget(self.kpi_words)
        stats_layout.addWidget(self.kpi_latency)
        stats_layout.addWidget(self.kpi_jargon)
        vbox.addLayout(stats_layout)

        # Feed Ultime Dettature
        feed_box = QFrame()
        feed_box.setProperty("class", "card")
        fl = QVBoxLayout(feed_box)
        ft = QLabel("Ultime Trascrizioni Effettuate")
        ft.setStyleSheet("font-weight: 700; font-size: 14px; color: #F8FAFC;")
        fl.addWidget(ft)

        self.home_feed_table = QTableWidget()
        self.home_feed_table.setColumnCount(4)
        self.home_feed_table.setHorizontalHeaderLabels(["Ora", "Trascrizione Rifinita", "Latenza", "Azioni"])
        self.home_feed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.home_feed_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.home_feed_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        fl.addWidget(self.home_feed_table)

        vbox.addWidget(feed_box)
        return widget

    def _make_stat_card(self, label: str, value: str, accent_color: str) -> QFrame:
        card = QFrame()
        card.setProperty("class", "card")
        cl = QVBoxLayout(card)
        cl.setSpacing(4)
        lbl = QLabel(label)
        lbl.setProperty("class", "stat-lbl")
        val = QLabel(value)
        val.setProperty("class", "stat-val")
        val.setStyleSheet(f"color: {accent_color};")
        cl.addWidget(lbl)
        cl.addWidget(val)
        card.val_label = val
        return card

    def _refresh_home_stats(self):
        stats = db.get_statistics()
        vocab_count = len(db.get_all_vocabulary())
        self.kpi_words.val_label.setText(f"{stats['total_words']:,} ({stats['saved_minutes']} min)")
        self.kpi_latency.val_label.setText(f"{stats['avg_latency_ms']} ms")
        self.kpi_jargon.val_label.setText(f"{vocab_count} attivi")

        recents = db.get_recent_transcriptions(limit=6)
        self.home_feed_table.setRowCount(len(recents))
        for row, item in enumerate(recents):
            time_str = item["created_at"].split()[1][:5] if " " in item["created_at"] else item["created_at"]
            self.home_feed_table.setItem(row, 0, QTableWidgetItem(time_str))
            self.home_feed_table.setItem(row, 1, QTableWidgetItem(item["final_text"]))
            total_lat = item.get("whisper_ms", 0) + item.get("qwen_ms", 0)
            self.home_feed_table.setItem(row, 2, QTableWidgetItem(f"{total_lat} ms"))

            copy_btn = QPushButton("Copia")
            copy_btn.setProperty("class", "btn-secondary")
            copy_btn.clicked.connect(lambda ch, t=item["final_text"]: pyperclip.copy(t))
            self.home_feed_table.setCellWidget(row, 3, copy_btn)

    # =========================================================================
    # VISTA 2: VOCABOLARIO & JARGON
    # =========================================================================
    def _create_vocab_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(14)

        t = QLabel("Vocabolario & Termini Tecnici")
        t.setProperty("class", "page-title")
        d = QLabel("Inserisci termini specifici o alias fonetici per il Prompt Biasing di Whisper e la correzione esatta di Qwen 2.5.")
        d.setProperty("class", "page-desc")
        vbox.addWidget(t)
        vbox.addWidget(d)

        # Form inserimento
        form_box = QFrame()
        form_box.setProperty("class", "card")
        fl = QHBoxLayout(form_box)
        fl.setSpacing(10)

        self.vocab_term_input = QLineEdit()
        self.vocab_term_input.setPlaceholderText("Termine corretto (es. PyQt6, CUDA)")

        self.vocab_aliases_input = QLineEdit()
        self.vocab_aliases_input.setPlaceholderText("Pronuncia fonetica / Alias (es. pichiutù)")

        self.vocab_cat_combo = QComboBox()
        self.vocab_cat_combo.addItems(["Coding", "AI", "Studio", "Aziendale", "Generale"])

        add_btn = QPushButton("+ Aggiungi")
        add_btn.setProperty("class", "btn-primary")
        add_btn.clicked.connect(self._add_vocab_item)

        fl.addWidget(self.vocab_term_input, 2)
        fl.addWidget(self.vocab_aliases_input, 2)
        fl.addWidget(self.vocab_cat_combo, 1)
        fl.addWidget(add_btn, 1)
        vbox.addWidget(form_box)

        # Tabella
        self.vocab_table = QTableWidget()
        self.vocab_table.setColumnCount(4)
        self.vocab_table.setHorizontalHeaderLabels(["Termine Corretto", "Alias Fonetici", "Categoria", "Azioni"])
        self.vocab_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.vocab_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.vocab_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        vbox.addWidget(self.vocab_table)

        return widget

    def _refresh_vocab_table(self):
        items = db.get_all_vocabulary()
        self.vocab_table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.vocab_table.setItem(row, 0, QTableWidgetItem(item["correct_term"]))
            self.vocab_table.setItem(row, 1, QTableWidgetItem(item.get("phonetic_aliases", "")))
            self.vocab_table.setItem(row, 2, QTableWidgetItem(item.get("category", "Generale")))

            del_btn = QPushButton("Elimina")
            del_btn.setProperty("class", "btn-danger")
            del_btn.clicked.connect(lambda ch, i=item["id"]: self._delete_vocab_item(i))
            self.vocab_table.setCellWidget(row, 3, del_btn)

    def _add_vocab_item(self):
        term = self.vocab_term_input.text().strip()
        aliases = self.vocab_aliases_input.text().strip()
        cat = self.vocab_cat_combo.currentText()
        if term:
            db.add_vocabulary(term, aliases, cat)
            self.vocab_term_input.clear()
            self.vocab_aliases_input.clear()
            self._refresh_vocab_table()

    def _delete_vocab_item(self, item_id: int):
        db.delete_vocabulary(item_id)
        self._refresh_vocab_table()

    # =========================================================================
    # VISTA 3: SNIPPET VOCALI
    # =========================================================================
    def _create_snippets_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(14)

        t = QLabel("Snippet Vocali & Macro di Testo")
        t.setProperty("class", "page-title")
        d = QLabel("Pronuncia una parola trigger (es. 'firma', 'bug report') per incollare all'istante interi blocchi di testo predefiniti.")
        d.setProperty("class", "page-desc")
        vbox.addWidget(t)
        vbox.addWidget(d)

        form_box = QFrame()
        form_box.setProperty("class", "card")
        fl = QVBoxLayout(form_box)

        self.snippet_trigger_input = QLineEdit()
        self.snippet_trigger_input.setPlaceholderText("Parola trigger vocale (es. 'firma', 'coordinate bancarie')")
        fl.addWidget(self.snippet_trigger_input)

        self.snippet_text_input = QTextEdit()
        self.snippet_text_input.setPlaceholderText("Testo espanso che verrà incollato automaticamente...")
        self.snippet_text_input.setMaximumHeight(80)
        fl.addWidget(self.snippet_text_input)

        add_btn = QPushButton("+ Salva Snippet")
        add_btn.setProperty("class", "btn-primary")
        add_btn.clicked.connect(self._add_snippet_item)
        fl.addWidget(add_btn, alignment=Qt.AlignmentFlag.AlignRight)

        vbox.addWidget(form_box)

        self.snippets_table = QTableWidget()
        self.snippets_table.setColumnCount(4)
        self.snippets_table.setHorizontalHeaderLabels(["Trigger Vocale", "Testo Espanso", "Utilizzi", "Azioni"])
        self.snippets_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.snippets_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.snippets_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        vbox.addWidget(self.snippets_table)

        return widget

    def _refresh_snippets_table(self):
        items = db.get_all_snippets()
        self.snippets_table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.snippets_table.setItem(row, 0, QTableWidgetItem(item["trigger_phrase"]))
            self.snippets_table.setItem(row, 1, QTableWidgetItem(item["expansion_text"]))
            self.snippets_table.setItem(row, 2, QTableWidgetItem(str(item.get("use_count", 0))))

            del_btn = QPushButton("Elimina")
            del_btn.setProperty("class", "btn-danger")
            del_btn.clicked.connect(lambda ch, i=item["id"]: self._delete_snippet_item(i))
            self.snippets_table.setCellWidget(row, 3, del_btn)

    def _add_snippet_item(self):
        trig = self.snippet_trigger_input.text().strip()
        txt = self.snippet_text_input.toPlainText().strip()
        if trig and txt:
            db.add_snippet(trig, txt)
            self.snippet_trigger_input.clear()
            self.snippet_text_input.clear()
            self._refresh_snippets_table()

    def _delete_snippet_item(self, item_id: int):
        db.delete_snippet(item_id)
        self._refresh_snippets_table()

    # =========================================================================
    # VISTA 4: STILI LLM & STUDIO (QWEN 2.5)
    # =========================================================================
    def _create_styles_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(14)

        t = QLabel("Stili di Rifinitura LLM & Studio (Qwen 2.5)")
        t.setProperty("class", "page-title")
        d = QLabel("Seleziona il profilo di scrittura desiderato o abbinalo automaticamente alle applicazioni target.")
        d.setProperty("class", "page-desc")
        vbox.addWidget(t)
        vbox.addWidget(d)

        self.styles_group = QButtonGroup(self)
        self.styles_radio_layout = QVBoxLayout()
        vbox.addLayout(self.styles_radio_layout)

        # Editor System Prompt
        editor_box = QFrame()
        editor_box.setProperty("class", "card")
        el = QVBoxLayout(editor_box)
        el_title = QLabel("System Prompt Attivo & App Associate")
        el_title.setStyleSheet("font-weight: 700; color: #F8FAFC;")

        self.style_apps_input = QLineEdit()
        self.style_apps_input.setPlaceholderText("Applicazioni associate (es. Code.exe, notepad.exe)")

        self.style_prompt_edit = QTextEdit()
        self.style_prompt_edit.setMinimumHeight(130)

        save_btn = QPushButton("Salva Modifiche Profilo")
        save_btn.setProperty("class", "btn-primary")
        save_btn.clicked.connect(self._save_active_style)

        el.addWidget(el_title)
        el.addWidget(QLabel("App Target (separati da virgola):"))
        el.addWidget(self.style_apps_input)
        el.addWidget(QLabel("Prompt di Istruzione:"))
        el.addWidget(self.style_prompt_edit)
        el.addWidget(save_btn, alignment=Qt.AlignmentFlag.AlignRight)
        vbox.addWidget(editor_box)

        vbox.addStretch()
        return widget

    def _refresh_styles_view(self):
        while self.styles_radio_layout.count():
            item = self.styles_radio_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        styles = db.get_all_styles()
        active_style = None

        for s in styles:
            rb = QRadioButton(f"{s['display_name']} — {s.get('description', '')}")
            rb.setStyleSheet("color: #F8FAFC; font-weight: 500; padding: 4px;")
            self.styles_group.addButton(rb, s["id"])
            if s["is_active"]:
                rb.setChecked(True)
                active_style = s
            self.styles_radio_layout.addWidget(rb)

        self.styles_group.idClicked.connect(self._on_style_selected)

        if active_style:
            self.style_prompt_edit.setText(active_style["system_prompt"])
            self.style_apps_input.setText(active_style.get("auto_target_apps", ""))

    def _on_style_selected(self, style_id: int):
        db.set_active_style(style_id)
        styles = db.get_all_styles()
        for s in styles:
            if s["id"] == style_id:
                self.style_prompt_edit.setText(s["system_prompt"])
                self.style_apps_input.setText(s.get("auto_target_apps", ""))
                break

    def _save_active_style(self):
        active_id = self.styles_group.checkedId()
        if active_id > 0:
            prompt = self.style_prompt_edit.toPlainText().strip()
            apps = self.style_apps_input.text().strip()
            db.update_style_prompt(active_id, prompt, apps)
            QMessageBox.information(self, "Salvataggio", "Profilo LLM aggiornato con successo!")

    # =========================================================================
    # VISTA 5: STUDIO, NOTE & FLASHCARD
    # =========================================================================
    def _create_study_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(14)

        t = QLabel("Studio, Note & Flashcard Vocali")
        t.setProperty("class", "page-title")
        d = QLabel("Archivio degli appunti presi con il Metodo Feynman e Flashcard generate dicendo 'Flashcard: [Domanda] ... [Risposta]'.")
        d.setProperty("class", "page-desc")
        vbox.addWidget(t)
        vbox.addWidget(d)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Colonna Sinistra: Elenco Note
        left_box = QFrame()
        left_box.setProperty("class", "card")
        ll = QVBoxLayout(left_box)
        ll_title = QLabel("I Tuoi Appunti & Flashcard")
        ll_title.setStyleSheet("font-weight: 700; color: #F8FAFC;")
        ll.addWidget(ll_title)

        self.notes_table = QTableWidget()
        self.notes_table.setColumnCount(3)
        self.notes_table.setHorizontalHeaderLabels(["Titolo", "Tipo", "Data"])
        self.notes_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.notes_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.notes_table.clicked.connect(self._on_note_clicked)
        ll.addWidget(self.notes_table)
        splitter.addWidget(left_box)

        # Colonna Destra: Visualizzatore Markdown
        right_box = QFrame()
        right_box.setProperty("class", "card")
        rl = QVBoxLayout(right_box)
        rl_title = QLabel("Dettaglio Scheda")
        rl_title.setStyleSheet("font-weight: 700; color: #F8FAFC;")

        self.note_viewer = QTextEdit()
        self.note_viewer.setReadOnly(True)

        copy_note_btn = QPushButton("Copia negli Appunti")
        copy_note_btn.setProperty("class", "btn-secondary")
        copy_note_btn.clicked.connect(lambda: pyperclip.copy(self.note_viewer.toPlainText()))

        rl.addWidget(rl_title)
        rl.addWidget(self.note_viewer)
        rl.addWidget(copy_note_btn, alignment=Qt.AlignmentFlag.AlignRight)
        splitter.addWidget(right_box)

        splitter.setSizes([450, 450])
        vbox.addWidget(splitter)
        return widget

    def _refresh_study_view(self):
        notes = db.get_all_study_notes()
        self.notes_table.setRowCount(len(notes))
        for row, n in enumerate(notes):
            self.notes_table.setItem(row, 0, QTableWidgetItem(n["title"]))
            self.notes_table.setItem(row, 1, QTableWidgetItem(n.get("note_type", "Note")))
            date_str = n["created_at"].split()[0] if " " in n["created_at"] else n["created_at"]
            self.notes_table.setItem(row, 2, QTableWidgetItem(date_str))

    def _on_note_clicked(self, index):
        row = index.row()
        notes = db.get_all_study_notes()
        if row < len(notes):
            n = notes[row]
            self.note_viewer.setText(f"# {n['title']}\n\n*Tipo:* {n['note_type']} | *Data:* {n['created_at']}\n\n---\n\n{n['content_markdown']}")

    # =========================================================================
    # VISTA 6: IMPOSTAZIONI AUDIO & HOTKEYS
    # =========================================================================
    def _create_audio_view(self) -> QWidget:
        widget = QWidget()
        vbox = QVBoxLayout(widget)
        vbox.setSpacing(16)

        t = QLabel("Impostazioni Audio & Hotkeys")
        t.setProperty("class", "page-title")
        d = QLabel("Configurazione del microfono hardware WASAPI e assegnazione tasti.")
        d.setProperty("class", "page-desc")
        vbox.addWidget(t)
        vbox.addWidget(d)

        card = QFrame()
        card.setProperty("class", "card")
        cl = QVBoxLayout(card)
        cl.setSpacing(14)

        cl.addWidget(QLabel("Dispositivo Microfono:"))
        self.device_combo = QComboBox()
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    self.device_combo.addItem(f"{dev['name']} [ID: {idx}]", idx)
        except Exception:
            self.device_combo.addItem("Microfono predefinito", None)
        self.device_combo.currentIndexChanged.connect(self._on_device_selection_changed)
        cl.addWidget(self.device_combo)

        cl.addWidget(QLabel("Livello Audio di Ingresso (VU-Meter):"))
        self.audio_meter = QProgressBar()
        self.audio_meter.setRange(0, 100)
        self.audio_meter.setValue(0)
        cl.addWidget(self.audio_meter)

        vbox.addWidget(card)

        hotkey_box = QFrame()
        hotkey_box.setProperty("class", "card")
        hl = QVBoxLayout(hotkey_box)
        hl.setSpacing(8)
        hl.addWidget(QLabel("Tasto Primario Push-to-Talk: Alt Gr (VK_RMENU)"))
        hl.addWidget(QLabel("Hands-Free Lock: Doppio tocco rapido su Alt Gr (avvia timer di dettatura continua)"))
        hl.addWidget(QLabel("Command Mode: Evidenzia del testo prima di premere Alt Gr per riassumerlo/modificarlo"))
        vbox.addWidget(hotkey_box)

        vbox.addStretch()
        return widget

    def update_live_volume(self, rms_normalized: float):
        val = int(min(100, max(0, rms_normalized * 100)))
        self.audio_meter.setValue(val)

    def _on_device_selection_changed(self, index: int):
        dev_idx = self.device_combo.currentData()
        if self.on_device_changed and dev_idx is not None:
            self.on_device_changed(dev_idx)

    def _refresh_all_views(self):
        self._refresh_home_stats()
        self._refresh_vocab_table()
        self._refresh_snippets_table()
        self._refresh_styles_view()
        self._refresh_study_view()
