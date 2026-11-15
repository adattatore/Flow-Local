"""Finestra Impostazioni FlowLocal (2 colonne: Aspetto, Motore AI & Hardware, Audio, Google & Gruppi)."""

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
from core.audio_ducker import audio_ducker
from core.audio_feedback import audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE
from core.cloud_sync import cloud_sync
from ui.themes import FONT_PRESETS, GPU_AVAILABLE
from ui.theme_widgets import _make_button, _make_card, _make_label


_SYMBOLIC_FONT_EXACT = {
    "segoe fluent icons",
    "segoe mdl2 assets",
    "hololens mdl2 assets",
    "wingdings",
    "wingdings 2",
    "wingdings 3",
    "webdings",
    "symbol",
    "marlett",
    "mt extra",
    "ms outlook",
    "ms reference specialty",
    "segoe ui symbol",
    "segoe ui emoji",
}
_SYMBOLIC_FONT_SUBSTRINGS = (
    "wingdings",
    "webdings",
    "mdl2 assets",
    "fluent icons",
    "marlett",
)


def _is_symbolic_font(family: str) -> bool:
    """Restituisce True se il font è di soli simboli/icone e va escluso dal selettore testo."""
    low = (family or "").strip().casefold()
    if not low:
        return True
    if low in _SYMBOLIC_FONT_EXACT:
        return True
    if any(sub in low for sub in _SYMBOLIC_FONT_SUBSTRINGS):
        return True
    try:
        ws = QFontDatabase.writingSystems(family)
        if ws == [QFontDatabase.WritingSystem.Symbol]:
            return True
    except Exception:
        pass
    return False


def _detect_hardware_summary() -> Dict[str, Any]:
    """Rileva GPU/CPU disponibile e suggerisce il profilo hardware consigliato."""
    if GPU_AVAILABLE:
        try:
            import pynvml
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            raw_name = pynvml.nvmlDeviceGetName(handle)
            gpu_name = raw_name.decode("utf-8", errors="ignore") if isinstance(raw_name, bytes) else str(raw_name)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = round(mem.total / (1024 ** 3), 1)
            if vram_gb >= 12.0:
                rec_tier = "pro"
            elif vram_gb >= 6.0:
                rec_tier = "standard"
            else:
                rec_tier = "eco"
            return {
                "has_gpu": True,
                "badge_text": f"🟢 GPU Rilevata: {gpu_name} ({vram_gb} GB VRAM)",
                "detail_text": "Accelerazione CUDA attiva: Whisper float16 + LLM locale ad alta velocità.",
                "recommended_tier": rec_tier,
            }
        except Exception:
            return {
                "has_gpu": True,
                "badge_text": "🟢 GPU CUDA Attiva",
                "detail_text": "Accelerazione GPU rilevata per Whisper float16 e Ollama.",
                "recommended_tier": "standard",
            }
    cores = os.cpu_count() or 4
    return {
        "has_gpu": False,
        "badge_text": f"🟡 Modalità CPU ({cores} core · Whisper int8)",
        "detail_text": "Nessuna GPU NVIDIA rilevata: profilo leggero consigliato per evitare rallentamenti.",
        "recommended_tier": "eco",
    }


class HubSettingsDialog(QDialog):
    """Finestra Impostazioni di FlowLocal Hub a 2 colonne (Sidebar Categorie + Pannello Contenuto)."""

    def __init__(self, hub: "FlowLocalHub"):
        super().__init__(hub)
        self.hub = hub
        self.setWindowTitle("⚙️ Impostazioni FlowLocal")
        self.setMinimumSize(780, 540)
        self.resize(840, 610)
        self.setModal(True)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---- COLONNA SINISTRA: Sidebar Categorie (180px) ----
        sidebar = QFrame()
        sidebar.setObjectName("SettingsSidebar")
        sidebar.setFixedWidth(180)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(10, 16, 10, 14)
        sb_layout.setSpacing(6)

        sb_title = _make_label("Impostazioni", "CardTitle")
        sb_title.setContentsMargins(6, 0, 6, 6)
        sb_layout.addWidget(sb_title)

        self._nav_grp = QButtonGroup(self)
        self._nav_grp.setExclusive(True)
        categories = [
            ("🎨 Aspetto & Testo", 0),
            ("⚡ Motore AI & Hardware", 1),
            ("🎙️ Audio & Scorciatoie", 2),
            ("☁️ Google & Gruppi", 3),
        ]
        for label, idx in categories:
            btn = QPushButton(label)
            btn.setObjectName("SettingsNavBtn")
            btn.setCheckable(True)
            btn.setChecked(idx == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, i=idx: self._switch_category(i))
            self._nav_grp.addButton(btn, idx)
            sb_layout.addWidget(btn)

        sb_layout.addStretch(1)
        ver_lbl = _make_label("FlowLocal Hub", "PageSubtitle")
        ver_lbl.setContentsMargins(6, 0, 6, 0)
        sb_layout.addWidget(ver_lbl)
        root_layout.addWidget(sidebar)

        # ---- COLONNA DESTRA: QStackedWidget + Barra Chiusura ----
        right_panel = QWidget()
        right_panel.setObjectName("Page")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(18, 16, 18, 14)
        right_layout.setSpacing(12)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._wrap_scroll(self._build_appearance_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_ai_hardware_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_audio_shortcuts_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_google_groups_page()))
        right_layout.addWidget(self.stack, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = _make_button("Chiudi", "primary")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        right_layout.addLayout(close_row)

        root_layout.addWidget(right_panel, 1)

    def _wrap_scroll(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner.setObjectName("ScrollContent")
        scroll.setWidget(inner)
        return scroll

    def _switch_category(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 3:
            self._refresh_gcal_status()
            self._refresh_drive_status()

    # -----------------------------------------------------------------
    # SEZIONE 0: ASPETTO & TESTO
    # -----------------------------------------------------------------
    def _build_appearance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 6, 6)
        layout.setSpacing(12)

        layout.addWidget(_make_label("🎨 Aspetto & Testo", "PageTitle"))

        # 1. Griglia Temi (3 colonne x 4 righe, zero troncamenti)
        theme_card, tc_layout = _make_card()
        tc_layout.addWidget(_make_label("Tema Interfaccia", "CardTitle"))
        tc_layout.addWidget(
            _make_label(
                "Scegli lo stile visivo della dashboard (Modalità Lettura resta accessibile in alto).",
                "PageSubtitle",
                wrap=True,
            )
        )

        self._theme_grp = QButtonGroup(self)
        self._theme_grp.setExclusive(True)
        themes = [
            ("Obsidian", "obsidian"),
            ("Chiaro", "light"),
            ("Orb", "orb"),
            ("Dark Orb", "dark_orb"),
            ("🌸 Ghibli Spring", "ghibli_spring"),
            ("👾 Pixel Chill", "pixel_chill"),
            ("⚔️ Crimson Katana", "katana"),
            ("🎯 Tactical HUD", "tactical_hud"),
            ("🤖 Industrial Mecha", "industrial_mecha"),
            ("🖥️ Amber CRT", "amber_crt"),
            ("🚀 Starfield Alabaster", "starfield_alabaster"),
            ("🌆 Cyber Synthwave", "cyber_synthwave"),
        ]
        theme_grid = QGridLayout()
        theme_grid.setSpacing(8)
        for idx, (t_label, t_key) in enumerate(themes):
            btn = QPushButton(t_label)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setMinimumWidth(170)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, idx)
            theme_grid.addWidget(btn, idx // 3, idx % 3)
        tc_layout.addLayout(theme_grid)
        layout.addWidget(theme_card)

        # 2. Tipografia & Scala Dimensione Testo
        font_card, fc_layout = _make_card()
        fc_layout.addWidget(_make_label("🔤 Tipografia & Dimensione Testo", "CardTitle"))
        self.font_subtitle_lbl = _make_label(
            "Personalizza il font della UI e dell'Editor (i blocchi codice mantengono sempre il font monospace).",
            "PageSubtitle",
            wrap=True,
        )
        fc_layout.addWidget(self.font_subtitle_lbl)

        self._font_preset_grp = QButtonGroup(self)
        self._font_preset_grp.setExclusive(True)
        self._font_preset_btns: Dict[str, QPushButton] = {}
        preset_items = [
            ("auto", "✨ Auto (Tema)", FONT_PRESETS["auto"]["subtitle"]),
            ("modern_sans", "Modern Sans", FONT_PRESETS["modern_sans"]["subtitle"]),
            ("geometric", "Geometric & Clean", FONT_PRESETS["geometric"]["subtitle"]),
            ("academic_serif", "📖 Academic / Serif", FONT_PRESETS["academic_serif"]["subtitle"]),
            ("dev_mono", "💻 Developer / Mono", FONT_PRESETS["dev_mono"]["subtitle"]),
            ("custom", "🪟 Font di Sistema", FONT_PRESETS["custom"]["subtitle"]),
        ]
        preset_grid = QGridLayout()
        preset_grid.setSpacing(6)
        for idx, (p_key, p_lbl, p_tip) in enumerate(preset_items):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setToolTip(p_tip)
            btn.setChecked(self.hub._font_preset == p_key)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_font_preset_selected(k))
            self._font_preset_grp.addButton(btn, idx)
            self._font_preset_btns[p_key] = btn
            preset_grid.addWidget(btn, idx // 3, idx % 3)
        fc_layout.addLayout(preset_grid)

        # Selettore Font Windows filtrato (esclusi font simbolici/icone)
        sys_font_row = QHBoxLayout()
        sys_font_row.setSpacing(8)
        sys_lbl = QLabel("Font Windows:")
        sys_lbl.setObjectName("CardBody")
        sys_font_row.addWidget(sys_lbl)

        self.system_font_combo = QComboBox()
        self.system_font_combo.setMinimumHeight(34)
        self._populate_system_fonts()
        self.system_font_combo.currentIndexChanged.connect(self._on_system_font_changed)
        sys_font_row.addWidget(self.system_font_combo, 1)
        fc_layout.addLayout(sys_font_row)

        # Controllo Scala Dimensione compatto: 4 pulsanti rapidi + 1 spinbox compatto
        scale_row = QHBoxLayout()
        scale_row.setSpacing(6)
        scale_lbl = QLabel("Scala Dimensione:")
        scale_lbl.setObjectName("CardBody")
        scale_row.addWidget(scale_lbl)

        self._scale_grp = QButtonGroup(self)
        self._scale_grp.setExclusive(True)
        self._scale_btns: Dict[int, QPushButton] = {}
        for idx, pct in enumerate([90, 100, 110, 120]):
            s_btn = QPushButton(f"{pct}%")
            s_btn.setObjectName("FilterPill")
            s_btn.setCheckable(True)
            s_btn.setChecked(self.hub._font_scale == pct)
            s_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            s_btn.clicked.connect(lambda _c=False, p=pct: self._on_font_scale_changed(p))
            self._scale_grp.addButton(s_btn, idx)
            self._scale_btns[pct] = s_btn
            scale_row.addWidget(s_btn)

        scale_row.addStretch(1)
        self.font_scale_spin = QSpinBox()
        self.font_scale_spin.setRange(90, 120)
        self.font_scale_spin.setSingleStep(5)
        self.font_scale_spin.setSuffix("%")
        self.font_scale_spin.setFixedWidth(74)
        self.font_scale_spin.setMinimumHeight(30)
        self.font_scale_spin.setValue(self.hub._font_scale)
        self.font_scale_spin.valueChanged.connect(self._on_font_scale_changed)
        scale_row.addWidget(self.font_scale_spin)
        fc_layout.addLayout(scale_row)

        layout.addWidget(font_card)
        layout.addStretch(1)
        return page

    def _populate_system_fonts(self):
        self.system_font_combo.blockSignals(True)
        self.system_font_combo.clear()
        families = sorted(QFontDatabase.families(), key=str.casefold)
        for fam in families:
            if not QFontDatabase.isSmoothlyScalable(fam):
                continue
            if _is_symbolic_font(fam):
                continue
            self.system_font_combo.addItem(fam, fam)
            idx = self.system_font_combo.count() - 1
            self.system_font_combo.setItemData(idx, QFont(fam), Qt.ItemDataRole.FontRole)

        target = (self.hub._custom_font_family or "").strip()
        if target:
            found = self.system_font_combo.findData(target)
            if found < 0:
                for i in range(self.system_font_combo.count()):
                    if self.system_font_combo.itemText(i).casefold() == target.casefold():
                        found = i
                        break
            if found >= 0:
                self.system_font_combo.setCurrentIndex(found)
        self.system_font_combo.blockSignals(False)

    # -----------------------------------------------------------------
    # SEZIONE 1: MOTORE AI & HARDWARE (BLOCCO 5)
    # -----------------------------------------------------------------
    def _build_ai_hardware_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 6, 6)
        layout.setSpacing(12)

        layout.addWidget(_make_label("⚡ Motore AI & Hardware", "PageTitle"))

        # 1. Card Riepilogo Hardware
        hw_info = _detect_hardware_summary()
        hw_card, hw_layout = _make_card()
        hw_top = QHBoxLayout()
        hw_top.setSpacing(8)
        hw_top.addWidget(_make_label("Riepilogo Hardware", "CardTitle"))
        hw_top.addStretch(1)

        saved_tier = db.get_setting("hardware_tier", "auto") or "auto"
        tier_labels = {"auto": "Auto", "pro": "Pro", "standard": "Standard", "eco": "Eco"}
        rec_label = tier_labels.get(hw_info["recommended_tier"], "Standard")
        self.hw_tier_badge = _make_label(
            f"Profilo: {tier_labels.get(saved_tier, 'Auto')}"
            + (f" ({rec_label})" if saved_tier == "auto" else ""),
            "BadgeAccent",
        )
        hw_top.addWidget(self.hw_tier_badge)
        hw_layout.addLayout(hw_top)

        hw_badge = _make_label(hw_info["badge_text"], "Badge")
        hw_layout.addWidget(hw_badge)
        hw_layout.addWidget(_make_label(hw_info["detail_text"], "PageSubtitle", wrap=True))

        tier_row = QHBoxLayout()
        tier_row.setSpacing(6)
        tier_row.addWidget(_make_label("Profilo Attivo:", "CardBody"))
        self._tier_grp = QButtonGroup(self)
        self._tier_grp.setExclusive(True)
        for idx, (t_key, t_lbl) in enumerate([
            ("auto", "Auto"),
            ("pro", "Pro"),
            ("standard", "Standard"),
            ("eco", "Eco"),
        ]):
            btn = QPushButton(t_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(saved_tier == t_key)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=t_key, r=rec_label: self._on_hardware_tier_changed(k, r))
            self._tier_grp.addButton(btn, idx)
            tier_row.addWidget(btn)
        tier_row.addStretch(1)
        hw_layout.addLayout(tier_row)
        layout.addWidget(hw_card)

        # 2. Card Provider AI, Modello Locale & Gemini BYOK
        ai_card, ai_layout = _make_card()
        ai_layout.addWidget(_make_label("Configurazione Motore AI", "CardTitle"))

        # Provider AI
        prov_lbl = _make_label("Provider AI:", "CardBody")
        ai_layout.addWidget(prov_lbl)
        saved_provider = db.get_setting("ai_provider", "local") or "local"
        self._provider_grp = QButtonGroup(self)
        self._provider_grp.setExclusive(True)
        prov_row = QHBoxLayout()
        prov_row.setSpacing(6)
        for idx, (p_key, p_lbl) in enumerate([
            ("local", "Locale (Ollama)"),
            ("gemini", "Cloud (Google Gemini BYOK)"),
            ("hybrid", "Ibrido"),
        ]):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(saved_provider == p_key)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_ai_provider_changed(k))
            self._provider_grp.addButton(btn, idx)
            prov_row.addWidget(btn)
        ai_layout.addLayout(prov_row)

        # Modello Locale
        mod_row = QHBoxLayout()
        mod_row.setSpacing(8)
        mod_row.addWidget(_make_label("Modello Locale:", "CardBody"))
        self.local_model_combo = QComboBox()
        self.local_model_combo.setMinimumHeight(34)
        self.local_model_combo.addItem("qwen3.5:4b (Consigliato)", "qwen3.5:4b")
        self.local_model_combo.addItem("qwen3.5:9b (Alta Precisione)", "qwen3.5:9b")
        self.local_model_combo.addItem("qwen3.5:2b (Leggero)", "qwen3.5:2b")
        saved_model = db.get_setting("whis_model_mode", "qwen3.5:4b") or "qwen3.5:4b"
        m_idx = self.local_model_combo.findData(saved_model)
        if m_idx >= 0:
            self.local_model_combo.setCurrentIndex(m_idx)
        self.local_model_combo.currentIndexChanged.connect(self._on_local_model_changed)
        mod_row.addWidget(self.local_model_combo, 1)
        ai_layout.addLayout(mod_row)

        # Chiave API Gemini (Opzionale)
        ai_layout.addWidget(_make_label("Chiave API Gemini (Opzionale):", "CardBody"))
        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key_edit.setPlaceholderText("AIza... (chiave personale Google AI Studio)")
        self.gemini_key_edit.setText(db.get_setting("gemini_api_key", "") or "")
        self.gemini_key_edit.editingFinished.connect(self._on_gemini_key_saved)
        key_row.addWidget(self.gemini_key_edit, 1)

        self.gemini_eye_btn = _make_button("👁️")
        self.gemini_eye_btn.setCheckable(True)
        self.gemini_eye_btn.setToolTip("Mostra / nascondi chiave API")
        self.gemini_eye_btn.toggled.connect(self._on_toggle_gemini_eye)
        key_row.addWidget(self.gemini_eye_btn)

        self.gemini_verify_btn = _make_button("Verifica", "primary")
        self.gemini_verify_btn.clicked.connect(self._on_verify_gemini_key)
        key_row.addWidget(self.gemini_verify_btn)
        ai_layout.addLayout(key_row)

        self.gemini_status_lbl = _make_label("", "PageSubtitle", wrap=True)
        self.gemini_status_lbl.setVisible(False)
        ai_layout.addWidget(self.gemini_status_lbl)

        # Sezione collassabile: Impostazioni Avanzate
        self.adv_toggle_btn = QPushButton("▸ Impostazioni Avanzate")
        self.adv_toggle_btn.setObjectName("CollapseToggle")
        self.adv_toggle_btn.setCheckable(True)
        self.adv_toggle_btn.setChecked(False)
        self.adv_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_toggle_btn.toggled.connect(self._on_toggle_advanced_ai)
        ai_layout.addWidget(self.adv_toggle_btn)

        self.adv_container = QWidget()
        self.adv_container.setVisible(False)
        adv_layout = QVBoxLayout(self.adv_container)
        adv_layout.setContentsMargins(8, 4, 0, 2)
        adv_layout.setSpacing(8)

        ep_row = QHBoxLayout()
        ep_row.setSpacing(8)
        ep_row.addWidget(_make_label("Endpoint Ollama:", "PageSubtitle"))
        self.ollama_url_edit = QLineEdit()
        self.ollama_url_edit.setText(db.get_setting("ollama_base_url", "http://localhost:11434") or "http://localhost:11434")
        self.ollama_url_edit.editingFinished.connect(
            lambda: db.set_setting("ollama_base_url", self.ollama_url_edit.text().strip() or "http://localhost:11434")
        )
        ep_row.addWidget(self.ollama_url_edit, 1)
        adv_layout.addLayout(ep_row)

        gm_row = QHBoxLayout()
        gm_row.setSpacing(8)
        gm_row.addWidget(_make_label("Modello Gemini:", "PageSubtitle"))
        self.gemini_model_combo = QComboBox()
        self.gemini_model_combo.setMinimumHeight(32)
        self.gemini_model_combo.addItem("gemini-2.5-flash (Consigliato)", "gemini-2.5-flash")
        self.gemini_model_combo.addItem("gemini-2.5-pro (Ragionamento Avanzato)", "gemini-2.5-pro")
        saved_gm = db.get_setting("gemini_model", "gemini-2.5-flash") or "gemini-2.5-flash"
        gm_idx = self.gemini_model_combo.findData(saved_gm)
        if gm_idx >= 0:
            self.gemini_model_combo.setCurrentIndex(gm_idx)
        self.gemini_model_combo.currentIndexChanged.connect(
            lambda: db.set_setting("gemini_model", self.gemini_model_combo.currentData() or "gemini-2.5-flash")
        )
        gm_row.addWidget(self.gemini_model_combo, 1)
        adv_layout.addLayout(gm_row)

        self.hybrid_fallback_chk = QCheckBox("Fallback automatico su Ollama locale se Gemini Cloud è offline")
        self.hybrid_fallback_chk.setChecked((db.get_setting("ai_hybrid_fallback", "1") or "1") == "1")
        self.hybrid_fallback_chk.toggled.connect(
            lambda chk: db.set_setting("ai_hybrid_fallback", "1" if chk else "0")
        )
        adv_layout.addWidget(self.hybrid_fallback_chk)

        ai_layout.addWidget(self.adv_container)
        layout.addWidget(ai_card)
        layout.addStretch(1)
        return page

    # -----------------------------------------------------------------
    # SEZIONE 2: AUDIO & SCORCIATOIE
    # -----------------------------------------------------------------
    def _build_audio_shortcuts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 6, 6)
        layout.setSpacing(12)

        layout.addWidget(_make_label("🎙️ Audio & Scorciatoie", "PageTitle"))

        # 1. Audio & Feedback
        audio_card, ac_layout = _make_card()
        ac_layout.addWidget(_make_label("Audio & Dettatura", "CardTitle"))

        self.auto_duck_checkbox = QCheckBox("Abbassa automaticamente la musica durante la dettatura (Auto-Ducking)")
        self.auto_duck_checkbox.setChecked(audio_ducker.is_enabled())
        self.auto_duck_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_duck_checkbox.toggled.connect(self._on_auto_duck_toggled)
        ac_layout.addWidget(self.auto_duck_checkbox)

        fb_row = QHBoxLayout()
        fb_row.setSpacing(8)
        fb_row.addWidget(_make_label("Feedback Audio Whis:", "CardBody"))
        self.settings_audio_combo = QComboBox()
        self.settings_audio_combo.setMinimumHeight(32)
        self.settings_audio_combo.addItem("Muto (Default)", MODE_MUTE)
        self.settings_audio_combo.addItem("Suono Morbido", MODE_SOFT_CHIME)
        self.settings_audio_combo.addItem("Voce (TTS)", MODE_VOICE)
        cur_fb_idx = self.settings_audio_combo.findData(audio_feedback.get_mode())
        if cur_fb_idx >= 0:
            self.settings_audio_combo.setCurrentIndex(cur_fb_idx)
        self.settings_audio_combo.currentIndexChanged.connect(self._on_settings_audio_mode_changed)
        fb_row.addWidget(self.settings_audio_combo, 1)
        ac_layout.addLayout(fb_row)
        layout.addWidget(audio_card)

        # 2. Scorciatoie Tastiera & Comandi Vocali
        sc_card, sc_layout = _make_card()
        sc_layout.addWidget(_make_label("Scorciatoie & Comandi Vocali", "CardTitle"))

        shortcuts = [
            ("Shift Destro", "Push-to-Talk principale (tieni premuto per dettare)"),
            ("2× Shift Destro", "Attiva / disattiva modalità Mani Libere (Hands-Free)"),
            ("F8", "Scorciatoia secondaria Push-to-Talk"),
            ("\"Whis...\"", "Pronuncia all'inizio della dettatura per interrogare l'assistente"),
        ]
        for key_txt, desc_txt in shortcuts:
            row = QHBoxLayout()
            row.setSpacing(10)
            key_lbl = _make_label(key_txt, "ShortcutKey")
            key_lbl.setMinimumWidth(120)
            key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(key_lbl)
            row.addWidget(_make_label(desc_txt, "CardBody", wrap=True), 1)
            sc_layout.addLayout(row)

        layout.addWidget(sc_card)
        layout.addStretch(1)
        return page

    # -----------------------------------------------------------------
    # SEZIONE 3: GOOGLE & GRUPPI (BLOCCO 6)
    # -----------------------------------------------------------------
    def _build_google_groups_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 6, 6)
        layout.setSpacing(12)

        layout.addWidget(_make_label("☁️ Google & Gruppi", "PageTitle"))

        # 1. Stato Connessione Google & Calendar FlowTime
        gcal_card, gc_layout = _make_card()
        gc_top = QHBoxLayout()
        gc_top.setSpacing(8)
        gc_top.addWidget(_make_label("🗓️ Account Google & Calendar FlowTime", "CardTitle"))
        gc_top.addStretch(1)
        self.gcal_account_badge = _make_label("Verifica...", "Badge")
        gc_top.addWidget(self.gcal_account_badge)
        gc_layout.addLayout(gc_top)

        self.gcal_status_lbl = _make_label("", "CardBody", wrap=True)
        gc_layout.addWidget(self.gcal_status_lbl)

        gc_btns = QHBoxLayout()
        gc_btns.setSpacing(8)
        self.gcal_sync_btn = _make_button("🔄 Sincronizza Ora", "primary")
        self.gcal_sync_btn.clicked.connect(self._on_sync_gcal)
        gc_btns.addWidget(self.gcal_sync_btn)

        self.gcal_connect_btn = _make_button("📅 Riconnetti / Cambia Account")
        self.gcal_connect_btn.clicked.connect(self._on_connect_gcal)
        gc_btns.addWidget(self.gcal_connect_btn)
        gc_btns.addStretch(1)
        gc_layout.addLayout(gc_btns)
        layout.addWidget(gcal_card)

        # 2. Stato Google Drive (FlowGroups / PWA)
        drive_card, dc_layout = _make_card()
        dc_top = QHBoxLayout()
        dc_top.setSpacing(8)
        dc_top.addWidget(_make_label("📁 Google Drive & Gruppi (FlowGroups / PWA)", "CardTitle"))
        dc_top.addStretch(1)
        self.drive_status_badge = _make_label("Attivo", "BadgeAccent")
        dc_top.addWidget(self.drive_status_badge)
        dc_layout.addLayout(dc_top)

        self.drive_status_lbl = _make_label("", "CardBody", wrap=True)
        dc_layout.addWidget(self.drive_status_lbl)

        dc_btns = QHBoxLayout()
        dc_btns.setSpacing(8)
        self.drive_sync_btn = _make_button("🔄 Sincronizza Ora", "primary")
        self.drive_sync_btn.clicked.connect(self._on_sync_drive_now)
        dc_btns.addWidget(self.drive_sync_btn)

        self.drive_folder_btn = _make_button("📁 Cambia Cartella Drive")
        self.drive_folder_btn.clicked.connect(self._on_change_drive_folder)
        dc_btns.addWidget(self.drive_folder_btn)
        dc_btns.addStretch(1)
        dc_layout.addLayout(dc_btns)
        layout.addWidget(drive_card)

        layout.addStretch(1)
        self._refresh_gcal_status()
        self._refresh_drive_status()
        return page

    # -----------------------------------------------------------------
    # HANDLERS
    # -----------------------------------------------------------------
    def _on_auto_duck_toggled(self, checked: bool):
        audio_ducker.set_enabled(bool(checked))
        if hasattr(self.hub, "styles_auto_duck_chk") and self.hub.styles_auto_duck_chk is not None:
            self.hub.styles_auto_duck_chk.blockSignals(True)
            self.hub.styles_auto_duck_chk.setChecked(bool(checked))
            self.hub.styles_auto_duck_chk.blockSignals(False)

    def _on_settings_audio_mode_changed(self):
        mode = self.settings_audio_combo.currentData()
        if mode and mode != audio_feedback.get_mode():
            audio_feedback.set_mode(mode)

    def _on_theme_pill_clicked(self, theme_key: str):
        self.hub._set_theme(theme_key)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_font_preset_selected(self, preset_key: str):
        if preset_key == "custom":
            fam = self.system_font_combo.currentData() or self.system_font_combo.currentText()
            self.hub._set_custom_font_family(fam)
        else:
            self.hub._set_font_preset(preset_key)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_system_font_changed(self):
        fam = self.system_font_combo.currentData() or self.system_font_combo.currentText()
        if not fam:
            return
        if "custom" in self._font_preset_btns:
            self._font_preset_btns["custom"].setChecked(True)
        self.hub._set_custom_font_family(fam)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_font_scale_changed(self, val: int):
        pct = max(90, min(120, int(val)))
        self.font_scale_spin.blockSignals(True)
        self.font_scale_spin.setValue(pct)
        self.font_scale_spin.blockSignals(False)

        if pct in self._scale_btns:
            self._scale_btns[pct].setChecked(True)
        else:
            checked_btn = self._scale_grp.checkedButton()
            if checked_btn is not None:
                self._scale_grp.setExclusive(False)
                checked_btn.setChecked(False)
                self._scale_grp.setExclusive(True)

        self.hub._set_font_scale(pct)
        self.setStyleSheet(self.hub.styleSheet())

    def _on_hardware_tier_changed(self, tier_key: str, rec_label: str):
        db.set_setting("hardware_tier", tier_key)
        tier_labels = {"auto": "Auto", "pro": "Pro", "standard": "Standard", "eco": "Eco"}
        suffix = f" ({rec_label})" if tier_key == "auto" else ""
        self.hw_tier_badge.setText(f"Profilo: {tier_labels.get(tier_key, 'Auto')}{suffix}")

    def _on_ai_provider_changed(self, provider_key: str):
        db.set_setting("ai_provider", provider_key)

    def _on_local_model_changed(self):
        mode = self.local_model_combo.currentData()
        if not mode:
            return
        db.set_setting("whis_model_mode", str(mode))
        if hasattr(self.hub, "whis_agent") and self.hub.whis_agent:
            self.hub.whis_agent.set_model_mode(mode)
        if hasattr(self.hub, "whis_model_combo") and self.hub.whis_model_combo is not None:
            idx = self.hub.whis_model_combo.findData(mode)
            if idx >= 0:
                self.hub.whis_model_combo.blockSignals(True)
                self.hub.whis_model_combo.setCurrentIndex(idx)
                self.hub.whis_model_combo.blockSignals(False)

    def _on_toggle_gemini_eye(self, checked: bool):
        self.gemini_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_gemini_key_saved(self):
        db.set_setting("gemini_api_key", self.gemini_key_edit.text().strip())

    def _on_verify_gemini_key(self):
        key = self.gemini_key_edit.text().strip()
        db.set_setting("gemini_api_key", key)
        self.gemini_status_lbl.setVisible(True)
        if not key:
            self.gemini_status_lbl.setText("⚠️ Inserisci una chiave API Gemini prima di verificare.")
            return

        self.gemini_verify_btn.setEnabled(False)
        self.gemini_status_lbl.setText("⏳ Verifica chiave Gemini in corso...")

        def _check():
            import urllib.request
            import urllib.error
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    ok = resp.status == 200
                msg = "🟢 Chiave API Gemini valida e verificata!" if ok else f"⚠️ Risposta inattesa ({resp.status})."
            except urllib.error.HTTPError as he:
                msg = f"🔴 Chiave non valida o non autorizzata (HTTP {he.code})."
            except Exception as exc:
                msg = f"🟡 Impossibile raggiungere Google AI Studio (rete offline?): {exc}"
            QTimer.singleShot(0, lambda: self._on_verify_gemini_done(msg))

        threading.Thread(target=_check, daemon=True, name="VerifyGeminiKeyThread").start()

    def _on_verify_gemini_done(self, msg: str):
        self.gemini_verify_btn.setEnabled(True)
        self.gemini_status_lbl.setVisible(True)
        self.gemini_status_lbl.setText(msg)

    def _on_toggle_advanced_ai(self, checked: bool):
        self.adv_toggle_btn.setText("▾ Impostazioni Avanzate" if checked else "▸ Impostazioni Avanzate")
        self.adv_container.setVisible(bool(checked))

    def _refresh_gcal_status(self):
        from core.calendar_sync import calendar_manager
        if calendar_manager.has_token_file():
            self.gcal_account_badge.setText("🟢 Connesso")
            self.gcal_status_lbl.setText(
                "Account Google collegato: la sincronizzazione bidirezionale con FlowTime avviene in background, "
                "mentre Whis legge istantaneamente da SQLite."
            )
            self.gcal_connect_btn.setText("📅 Riconnetti / Cambia Account")
            self.gcal_sync_btn.setEnabled(True)
        elif calendar_manager.has_credentials_file():
            self.gcal_account_badge.setText("🟡 Da Autorizzare")
            self.gcal_status_lbl.setText(
                "File credentials.json rilevato: clicca su 'Riconnetti / Cambia Account' per autorizzare l'accesso Google."
            )
            self.gcal_connect_btn.setText("📅 Connetti Google")
            self.gcal_sync_btn.setEnabled(False)
        else:
            self.gcal_account_badge.setText("⚪ Solo Locale")
            self.gcal_status_lbl.setText(
                "Modalità 100% Locale attiva: FlowTime e Whis funzionano istantaneamente offline. "
                "Aggiungi credentials.json per abilitare Google Calendar."
            )
            self.gcal_connect_btn.setText("📅 Configura Google")
            self.gcal_sync_btn.setEnabled(False)

    def _on_connect_gcal(self):
        self.hub._on_connect_calendar_clicked()
        QTimer.singleShot(800, self._refresh_gcal_status)

    def _on_sync_gcal(self):
        self.hub._on_ft_sync_google_clicked()
        self.gcal_status_lbl.setText("⏳ Sincronizzazione con Google Calendar avviata in background...")

    def _refresh_drive_status(self):
        sync_dir = cloud_sync.get_sync_base_dir()
        pending = cloud_sync.get_pending_count()
        outbox_lib = os.path.join(cloud_sync.get_outbox_study_dir(), "library.json")
        lib_ok = os.path.exists(outbox_lib)
        self.drive_status_badge.setText(
            f"🟢 {pending} in coda" if pending > 0 else ("🟢 Sincronizzato" if lib_ok else "🟡 Pronto")
        )
        self.drive_status_lbl.setText(
            f"Cartella Drive: {sync_dir}\n"
            f"Coda audio in attesa: {pending} · Indice PWA (library.json): "
            f"{'Presente' if lib_ok else 'Da generare'}"
        )

    def _on_sync_drive_now(self):
        try:
            cloud_sync.generate_library_json()
            self._refresh_drive_status()
            self.drive_status_lbl.setText(
                self.drive_status_lbl.text() + "\n✅ Indice library.json aggiornato per Google Drive / PWA."
            )
        except Exception as exc:
            self.drive_status_lbl.setText(f"⚠️ Errore sincronizzazione Drive: {exc}")

    def _on_change_drive_folder(self):
        current_dir = cloud_sync.get_sync_base_dir()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Seleziona cartella sincronizzata Google Drive (FlowLocal_Sync)",
            current_dir,
        )
        if chosen:
            cloud_sync.set_sync_base_dir(chosen)
            self._refresh_drive_status()

