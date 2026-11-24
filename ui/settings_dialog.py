"""Finestra Impostazioni FlowLocal (2 colonne: Aspetto, Motore AI & Hardware, Audio, Google & Gruppi)."""

from typing import Any, Dict, List, Optional, Tuple
import os
import threading

from PyQt6.QtCore import (
    Qt, QTimer, QSize, QPropertyAnimation, QEasingCurve, QThread, QUrl, pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from database import db
from core.audio_ducker import audio_ducker
from core.audio_feedback import audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE
from core.cloud_sync import cloud_sync
from core.flow_groups import flow_groups
from core.hardware_profiler import (
    HardwareTier,
    build_profile,
    detect_hardware,
    ensure_first_run_hardware_tier,
)
from core.gemini_client import (
    add_runtime_engine_listener,
    get_active_runtime_engine,
    normalize_provider,
    remove_runtime_engine_listener,
    set_active_runtime_engine,
    verify_api_key,
)
from core.paths import get_resource_path
from ui.themes import FONT_PRESETS, GPU_AVAILABLE
from ui.theme_widgets import ThemePreviewButton, _make_button, _make_divider, _make_label
from ui.icons import apply_win11_titlebar, get_icon, get_pixmap


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
    """Rileva GPU/CPU disponibile tramite hardware_profiler e suggerisce il profilo hardware consigliato."""
    hw = detect_hardware()
    rec_profile = build_profile(hw.recommended_tier, hw)
    return {
        "has_gpu": hw.has_cuda,
        "status_role": "success" if hw.has_cuda else "warning",
        "badge_text": rec_profile.badge_text,
        "detail_text": rec_profile.detail_text,
        "recommended_tier": hw.recommended_tier.value,
    }


class _GeminiVerifyWorker(QThread):
    """Worker QThread per verificare in background la validità della chiave API Gemini."""

    verifyFinished = pyqtSignal(bool, str)

    def __init__(self, api_key: str, model: str, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._model = model

    def run(self):
        ok, msg = verify_api_key(self._api_key, model=self._model, timeout=6.0)
        self.verifyFinished.emit(ok, msg)


GEMINI_AISTUDIO_URL = "https://aistudio.google.com/apikey"


class _ImageZoomDialog(QDialog):
    """Finestra modale leggera per visualizzare uno screenshot della guida ingrandito."""

    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scaled = pixmap.scaled(
            820, 480, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        lbl.setPixmap(scaled)
        layout.addWidget(lbl, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = _make_button("Chiudi", "ghost")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class _ClickableScreenshot(QLabel):
    """Anteprima screenshot cliccabile per aprire la vista ingrandita."""

    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)
        self._full_pixmap = pixmap
        self._zoom_title = title
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Clicca per ingrandire lo screenshot")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        thumb = pixmap.scaledToWidth(
            min(520, pixmap.width()), Qt.TransformationMode.SmoothTransformation
        )
        self.setPixmap(thumb)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            dlg = _ImageZoomDialog(self._full_pixmap, self._zoom_title, parent=self)
            dlg.exec()
        super().mousePressEvent(event)


class GeminiKeyGuideDialog(QDialog):
    """Guida visiva passo-passo (4 step) per ottenere gratuitamente una chiave Google AI Studio."""

    def __init__(self, parent=None, theme_key: str = "obsidian", reading_mode: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Come ottenere la chiave gratuita Google Gemini (4 Step)")
        self.setMinimumSize(620, 520)
        self.resize(680, 620)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        title_lbl = _make_label("Ottieni la tua chiave gratuita in 30 secondi", "PageTitle")
        root.addWidget(title_lbl)

        note_card = QFrame()
        note_card.setObjectName("Card")
        note_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        nc_layout = QVBoxLayout(note_card)
        nc_layout.setContentsMargins(14, 10, 14, 10)
        note_lbl = _make_label(
            "La chiave gratuita non richiede carta di credito ed è più che sufficiente per l'uso universitario quotidiano.",
            "CardBody",
            wrap=True,
        )
        nc_layout.addWidget(note_lbl)
        root.addWidget(note_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("ScrollContent")
        c_layout = QVBoxLayout(content)
        c_layout.setContentsMargins(0, 4, 8, 4)
        c_layout.setSpacing(10)

        steps = [
            (
                1,
                "Accedi a Google AI Studio",
                "Clicca su Ottieni chiave gratuita e accedi con il tuo normale account Google (Gmail).",
            ),
            (
                2,
                "Crea una nuova API Key",
                "Nella pagina di Google AI Studio, clicca sul pulsante Create API key.",
            ),
            (
                3,
                "Seleziona un nuovo progetto",
                "Se richiesto, seleziona Create API key in new project e attendi qualche secondo.",
            ),
            (
                4,
                "Copia, incolla e verifica",
                "Copia il codice che inizia con AIza..., incollalo nel campo delle Impostazioni di FlowLocal e premi Verifica.",
            ),
        ]

        for step_num, step_title, step_desc in steps:
            card = QFrame()
            card.setObjectName("Card")
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(6)

            hdr = _make_label(f"Step {step_num} · {step_title}", "CardTitle", wrap=True)
            desc = _make_label(step_desc, "CardBody", wrap=True)
            cl.addWidget(hdr)
            cl.addWidget(desc)

            img_path = get_resource_path(os.path.join("assets", "guides", "gemini_key", f"step{step_num}.png"))
            if img_path.is_file():
                pm = QPixmap(str(img_path))
                if not pm.isNull():
                    preview = _ClickableScreenshot(pm, f"Step {step_num} · {step_title}", parent=self)
                    cl.addWidget(preview)

            c_layout.addWidget(card)

        c_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        open_btn = _make_button("Ottieni chiave gratuita", "primary")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GEMINI_AISTUDIO_URL)))
        footer.addWidget(open_btn)
        footer.addStretch(1)
        close_btn = _make_button("Chiudi", "ghost")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

        apply_win11_titlebar(self, theme_key, reading_mode)


class HubSettingsDialog(QDialog):
    """Finestra Impostazioni di FlowLocal Hub a 2 colonne (Sidebar Categorie + Pannello Contenuto)."""

    _runtimeEngineSignal = pyqtSignal(str)

    def __init__(self, hub: "FlowLocalHub", initial_tab: int = 0):
        super().__init__(hub)
        self.hub = hub
        self.setWindowTitle("Impostazioni FlowLocal")
        self.setMinimumSize(820, 560)
        self.resize(880, 620)
        self.setModal(True)

        self._nav_buttons: List[Tuple[QPushButton, str]] = []
        self._adv_anim: Optional[QPropertyAnimation] = None
        self._gemini_verify_worker: Optional[_GeminiVerifyWorker] = None

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---- COLONNA SINISTRA: Sidebar Categorie (180px) ----
        sidebar = QFrame()
        sidebar.setObjectName("SettingsSidebar")
        sidebar.setFixedWidth(180)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(10, 18, 10, 14)
        sb_layout.setSpacing(4)

        sb_title = _make_label("Impostazioni", "CardTitle")
        sb_title.setContentsMargins(8, 0, 8, 8)
        sb_layout.addWidget(sb_title)

        self._nav_grp = QButtonGroup(self)
        self._nav_grp.setExclusive(True)
        categories = [
            ("Aspetto & Testo", "palette", 0),
            ("Motore AI & Hardware", "lightning", 1),
            ("Audio & Scorciatoie", "mic", 2),
            ("Google & Gruppi", "cloud", 3),
        ]
        for label, icon_name, idx in categories:
            btn = QPushButton(f"  {label}")
            btn.setObjectName("SettingsNavBtn")
            btn.setCheckable(True)
            btn.setChecked(idx == 0)
            btn.setIconSize(QSize(15, 15))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, i=idx: self._switch_category(i))
            self._nav_grp.addButton(btn, idx)
            self._nav_buttons.append((btn, icon_name))
            sb_layout.addWidget(btn)

        sb_layout.addStretch(1)
        ver_lbl = _make_label("FlowLocal Hub", "PageSubtitle")
        ver_lbl.setContentsMargins(8, 0, 8, 0)
        sb_layout.addWidget(ver_lbl)
        root_layout.addWidget(sidebar)

        # ---- COLONNA DESTRA: QStackedWidget + Barra Chiusura ----
        right_panel = QWidget()
        right_panel.setObjectName("Page")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(24, 20, 24, 16)
        right_layout.setSpacing(14)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._wrap_scroll(self._build_appearance_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_ai_hardware_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_audio_shortcuts_page()))
        self.stack.addWidget(self._wrap_scroll(self._build_google_groups_page()))
        right_layout.addWidget(self.stack, 1)

        right_layout.addWidget(_make_divider())

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = _make_button("Chiudi", "ghost")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        right_layout.addLayout(close_row)

        root_layout.addWidget(right_panel, 1)

        self._refresh_icons()
        apply_win11_titlebar(self, self.hub._current_theme, self.hub._reading_mode)

        self._runtimeEngineSignal.connect(self._on_runtime_engine_updated, Qt.ConnectionType.QueuedConnection)
        self._runtime_cb = lambda label: self._runtimeEngineSignal.emit(label)
        add_runtime_engine_listener(self._runtime_cb)
        self.finished.connect(lambda _r=0: remove_runtime_engine_listener(self._runtime_cb))

        if 0 <= initial_tab < self.stack.count():
            nav_btn = self._nav_grp.button(initial_tab)
            if nav_btn is not None:
                nav_btn.setChecked(True)
            self._switch_category(initial_tab)
            self._refresh_icons()

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

    def _refresh_icons(self):
        """Aggiorna tutte le icone vettoriali e la titlebar in base al tema attivo."""
        tkey = getattr(self.hub, "_current_theme", "obsidian")
        self.setWindowIcon(get_icon("settings", theme_key=tkey))

        for btn, icon_name in self._nav_buttons:
            role = "accent" if btn.isChecked() else "subtitle"
            btn.setIcon(get_icon(icon_name, theme_key=tkey, role=role, size=15))

        if hasattr(self, "hw_dot_lbl"):
            self.hw_dot_lbl.setPixmap(
                get_pixmap("dot", theme_key=tkey, role=self._hw_status_role, size=10)
            )
        if hasattr(self, "gemini_eye_btn"):
            eye_icon = "eye_off" if self.gemini_eye_btn.isChecked() else "eye"
            self.gemini_eye_btn.setIcon(get_icon(eye_icon, theme_key=tkey, role="subtitle", size=15))
        if hasattr(self, "adv_toggle_btn"):
            caret = "caret_down" if self.adv_toggle_btn.isChecked() else "caret_right"
            self.adv_toggle_btn.setIcon(get_icon(caret, theme_key=tkey, role="subtitle", size=13))
        if hasattr(self, "gcal_sync_btn"):
            self.gcal_sync_btn.setIcon(get_icon("refresh", theme_key=tkey, role="on_accent", size=14))
        if hasattr(self, "gcal_connect_btn"):
            self.gcal_connect_btn.setIcon(get_icon("calendar", theme_key=tkey, role="text", size=14))
        if hasattr(self, "drive_sync_btn"):
            self.drive_sync_btn.setIcon(get_icon("refresh", theme_key=tkey, role="on_accent", size=14))
        if hasattr(self, "drive_folder_btn"):
            self.drive_folder_btn.setIcon(get_icon("folder", theme_key=tkey, role="text", size=14))

        apply_win11_titlebar(self, tkey, getattr(self.hub, "_reading_mode", False))

    # -----------------------------------------------------------------
    # SEZIONE 0: ASPETTO & TESTO
    # -----------------------------------------------------------------
    def _build_appearance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(20)

        layout.addWidget(_make_label("Aspetto & Testo", "PageTitle"))

        # 1. Sezione Temi: Core Signature (Consigliati) vs Temi Creativi (Sperimentali)
        from ui.themes import CORE_SIGNATURE_THEMES, CREATIVE_THEMES, CORE_SIGNATURE_THEME_KEYS

        theme_sec = QVBoxLayout()
        theme_sec.setSpacing(10)
        theme_sec.addWidget(_make_label("Temi Signature (Consigliati)", "CardTitle"))
        theme_sec.addWidget(
            _make_label(
                "Temi principali ottimizzati per leggibilità, contrasto e coerenza su tutte le viste.",
                "PageSubtitle",
                wrap=True,
            )
        )

        self._theme_grp = QButtonGroup(self)
        self._theme_grp.setExclusive(True)
        btn_id = 0

        core_grid = QGridLayout()
        core_grid.setSpacing(8)
        for idx, (t_label, t_key, t_desc) in enumerate(CORE_SIGNATURE_THEMES):
            btn = ThemePreviewButton(t_label, t_key)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setToolTip(t_desc)
            btn.setMinimumWidth(210)
            btn.setMinimumHeight(36)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, btn_id)
            btn_id += 1
            core_grid.addWidget(btn, idx // 2, idx % 2)
        theme_sec.addLayout(core_grid)

        # Sezione Temi Creativi / Sperimentali (aperta di default se l'utente usa già un tema creativo)
        using_creative = self.hub._current_theme not in CORE_SIGNATURE_THEME_KEYS
        creative_toggle = QPushButton(
            "▼  Temi Creativi & Sperimentali (8)" if using_creative else "▶  Temi Creativi & Sperimentali (8)"
        )
        creative_toggle.setObjectName("FilterPill")
        creative_toggle.setCheckable(True)
        creative_toggle.setChecked(using_creative)
        creative_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        creative_toggle.setMinimumHeight(30)
        theme_sec.addWidget(creative_toggle)

        creative_host = QWidget()
        creative_host.setVisible(using_creative)
        creative_grid = QGridLayout(creative_host)
        creative_grid.setContentsMargins(0, 4, 0, 0)
        creative_grid.setSpacing(8)
        for idx, (t_label, t_key, t_desc) in enumerate(CREATIVE_THEMES):
            btn = ThemePreviewButton(t_label, t_key)
            btn.setChecked(self.hub._current_theme == t_key)
            btn.setToolTip(f"{t_desc} (Tema creativo)")
            btn.setMinimumWidth(185)
            btn.setMinimumHeight(34)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _c=False, k=t_key: self._on_theme_pill_clicked(k))
            self._theme_grp.addButton(btn, btn_id)
            btn_id += 1
            creative_grid.addWidget(btn, idx // 3, idx % 3)

        def _toggle_creative(checked: bool):
            creative_host.setVisible(checked)
            creative_toggle.setText(
                "▼  Temi Creativi & Sperimentali (8)" if checked else "▶  Temi Creativi & Sperimentali (8)"
            )

        creative_toggle.toggled.connect(_toggle_creative)
        theme_sec.addWidget(creative_host)

        # Pulsante rapido per riaprire la Configurazione Guidata (Onboarding)
        if hasattr(self.hub, "_open_onboarding_wizard"):
            ob_row = QHBoxLayout()
            ob_row.setContentsMargins(0, 4, 0, 0)
            ob_btn = QPushButton(" Avvia Configurazione Guidata (Onboarding)")
            ob_btn.setObjectName("FilterPill")
            ob_btn.setIcon(get_icon("sparkles", theme_key=self.hub._current_theme, role="accent", size=14))
            ob_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ob_btn.setMinimumHeight(32)
            ob_btn.clicked.connect(lambda: (self.accept(), self.hub._open_onboarding_wizard()))
            ob_row.addWidget(ob_btn)
            ob_row.addStretch(1)
            theme_sec.addLayout(ob_row)

        layout.addLayout(theme_sec)

        layout.addWidget(_make_divider())

        # 2. Tipografia & Scala Dimensione Testo
        font_sec = QVBoxLayout()
        font_sec.setSpacing(10)
        font_sec.addWidget(_make_label("Tipografia & Dimensione Testo", "CardTitle"))
        self.font_subtitle_lbl = _make_label(
            "Personalizza il font della UI e dell'Editor.",
            "PageSubtitle",
            wrap=True,
        )
        font_sec.addWidget(self.font_subtitle_lbl)

        self._font_preset_grp = QButtonGroup(self)
        self._font_preset_grp.setExclusive(True)
        self._font_preset_btns: Dict[str, QPushButton] = {}
        preset_items = [
            ("auto", "Automatico", FONT_PRESETS["auto"]["subtitle"]),
            ("modern_sans", "Modern Sans", FONT_PRESETS["modern_sans"]["subtitle"]),
            ("geometric", "Geometric", FONT_PRESETS["geometric"]["subtitle"]),
            ("academic_serif", "Academic Serif", FONT_PRESETS["academic_serif"]["subtitle"]),
            ("dev_mono", "Developer Mono", FONT_PRESETS["dev_mono"]["subtitle"]),
            ("custom", "Font di Sistema", FONT_PRESETS["custom"]["subtitle"]),
        ]
        preset_grid = QGridLayout()
        preset_grid.setSpacing(8)
        for idx, (p_key, p_lbl, p_tip) in enumerate(preset_items):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setToolTip(p_tip)
            btn.setChecked(self.hub._font_preset == p_key)
            btn.setMinimumHeight(30)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_font_preset_selected(k))
            self._font_preset_grp.addButton(btn, idx)
            self._font_preset_btns[p_key] = btn
            preset_grid.addWidget(btn, idx // 3, idx % 3)
        font_sec.addLayout(preset_grid)

        # Selettore Font Windows filtrato (esclusi font simbolici/icone)
        sys_font_row = QHBoxLayout()
        sys_font_row.setSpacing(10)
        sys_lbl = QLabel("Font Windows")
        sys_lbl.setObjectName("CardBody")
        sys_lbl.setMinimumWidth(115)
        sys_font_row.addWidget(sys_lbl)

        self.system_font_combo = QComboBox()
        self.system_font_combo.setMinimumHeight(34)
        self.system_font_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._populate_system_fonts()
        self.system_font_combo.currentIndexChanged.connect(self._on_system_font_changed)
        sys_font_row.addWidget(self.system_font_combo, 1)
        font_sec.addLayout(sys_font_row)

        # Controllo Scala Dimensione compatto: etichetta + 4 pulsanti rapidi + 1 spinbox compatto
        scale_row = QHBoxLayout()
        scale_row.setSpacing(6)
        scale_lbl = QLabel("Scala Dimensione")
        scale_lbl.setObjectName("CardBody")
        scale_lbl.setMinimumWidth(115)
        scale_row.addWidget(scale_lbl)

        self._scale_grp = QButtonGroup(self)
        self._scale_grp.setExclusive(True)
        self._scale_btns: Dict[int, QPushButton] = {}
        for idx, pct in enumerate([90, 100, 110, 120]):
            s_btn = QPushButton(f"{pct}%")
            s_btn.setObjectName("FilterPill")
            s_btn.setCheckable(True)
            s_btn.setMinimumWidth(56)
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
        font_sec.addLayout(scale_row)

        layout.addLayout(font_sec)

        layout.addWidget(_make_divider())

        # 3. Preferenze FlowNotes (Nota all'avvio & Titolo AI automatico)
        fn_sec = QVBoxLayout()
        fn_sec.setSpacing(10)
        fn_sec.addWidget(_make_label("FlowNotes & Editor", "CardTitle"))

        startup_row = QHBoxLayout()
        startup_row.setSpacing(10)
        startup_lbl = _make_label("All'avvio apri", "CardBody")
        startup_lbl.setMinimumWidth(115)
        startup_row.addWidget(startup_lbl)

        self.startup_note_combo = QComboBox()
        self.startup_note_combo.setMinimumHeight(34)
        self.startup_note_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.startup_note_combo.addItem("Ultima nota visualizzata (Consigliato)", "last_viewed")
        self.startup_note_combo.addItem("Ripristina tutte le schede aperte", "restore_tabs")
        self.startup_note_combo.addItem("Nota più recente", "most_recent")
        self.startup_note_combo.addItem("Nuova nota vuota", "empty")
        saved_startup = (db.get_setting("startup_note_mode", "last_viewed") or "last_viewed").strip()
        s_idx = self.startup_note_combo.findData(saved_startup)
        if s_idx >= 0:
            self.startup_note_combo.setCurrentIndex(s_idx)
        self.startup_note_combo.currentIndexChanged.connect(
            lambda: db.set_setting("startup_note_mode", self.startup_note_combo.currentData() or "last_viewed")
        )
        startup_row.addWidget(self.startup_note_combo, 1)
        fn_sec.addLayout(startup_row)

        self.auto_title_chk = QCheckBox("Genera automaticamente il titolo delle nuove note dal contesto (AI)")
        self.auto_title_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_title_chk.setChecked((db.get_setting("auto_generate_note_title", "1") or "1").strip() == "1")
        self.auto_title_chk.toggled.connect(
            lambda chk: db.set_setting("auto_generate_note_title", "1" if chk else "0")
        )
        fn_sec.addWidget(self.auto_title_chk)

        layout.addLayout(fn_sec)
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
    # SEZIONE 1: MOTORE AI & HARDWARE
    # -----------------------------------------------------------------
    def _build_ai_hardware_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(20)

        layout.addWidget(_make_label("Motore AI & Hardware", "PageTitle"))

        # 1. Riepilogo Hardware
        hw_info = _detect_hardware_summary()
        self._hw_status_role = hw_info["status_role"]
        hw_sec = QVBoxLayout()
        hw_sec.setSpacing(10)

        hw_top = QHBoxLayout()
        hw_top.setSpacing(8)
        hw_top.addWidget(_make_label("Riepilogo Hardware", "CardTitle"))
        hw_top.addStretch(1)

        saved_tier = ensure_first_run_hardware_tier(db) or "auto"
        tier_labels = {"auto": "Automatico", "pro": "Pro", "standard": "Standard", "eco": "Eco"}
        rec_label = tier_labels.get(hw_info["recommended_tier"], "Standard")
        self.hw_tier_badge = _make_label(
            f"Profilo: {tier_labels.get(saved_tier, 'Automatico')}"
            + (f" · {rec_label}" if saved_tier == "auto" else ""),
            "BadgeAccent",
        )
        hw_top.addWidget(self.hw_tier_badge)
        hw_sec.addLayout(hw_top)

        hw_status_row = QHBoxLayout()
        hw_status_row.setSpacing(8)
        self.hw_dot_lbl = QLabel()
        self.hw_dot_lbl.setFixedSize(12, 12)
        hw_status_row.addWidget(self.hw_dot_lbl)
        self.hw_badge_lbl = _make_label(hw_info["badge_text"], "CardBody")
        hw_status_row.addWidget(self.hw_badge_lbl, 1)
        hw_sec.addLayout(hw_status_row)
        self.hw_detail_lbl = _make_label(hw_info["detail_text"], "PageSubtitle", wrap=True)
        hw_sec.addWidget(self.hw_detail_lbl)

        tier_row = QHBoxLayout()
        tier_row.setSpacing(6)
        tier_row.addWidget(_make_label("Profilo Attivo", "CardBody"))
        self._tier_grp = QButtonGroup(self)
        self._tier_grp.setExclusive(True)
        for idx, (t_key, t_lbl) in enumerate([
            ("auto", "Automatico"),
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
        hw_sec.addLayout(tier_row)
        layout.addLayout(hw_sec)

        layout.addWidget(_make_divider())

        # 2. Configurazione Motore AI
        ai_sec = QVBoxLayout()
        ai_sec.setSpacing(12)
        ai_hdr = QHBoxLayout()
        ai_hdr.setSpacing(8)
        ai_hdr.addWidget(_make_label("Configurazione Motore AI", "CardTitle"))
        ai_hdr.addStretch(1)
        self.ai_runtime_lbl = _make_label(get_active_runtime_engine(), "PageSubtitle")
        self.ai_runtime_lbl.setObjectName("Badge")
        ai_hdr.addWidget(self.ai_runtime_lbl)
        ai_sec.addLayout(ai_hdr)

        saved_key = (db.get_setting("gemini_api_key", "") or "").strip()
        raw_prov = db.get_setting("ai_provider", "")
        saved_provider = "adaptive" if (not raw_prov and saved_key) else normalize_provider(raw_prov or "local")
        self._provider_grp = QButtonGroup(self)
        self._provider_grp.setExclusive(True)
        self._provider_buttons: Dict[str, QPushButton] = {}
        prov_row = QHBoxLayout()
        prov_row.setSpacing(8)
        for idx, (p_key, p_lbl, p_tip) in enumerate([
            ("adaptive", "Adattivo", "Gemini Online quando disponibile, Qwen 3.5 / Deterministico Offline (timeout 3s)."),
            ("local", "Locale", "Usa esclusivamente Ollama Qwen 3.5 / elaborazione locale."),
            ("gemini", "Cloud Gemini", "Usa Google Gemini API per tutte le attività."),
            ("hybrid", "Ibrido", "Dettatura locale + Vision/Whis/Studio su Gemini."),
        ]):
            btn = QPushButton(p_lbl)
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setChecked(saved_provider == p_key)
            btn.setToolTip(p_tip)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=p_key: self._on_ai_provider_changed(k))
            self._provider_grp.addButton(btn, idx)
            self._provider_buttons[p_key] = btn
            prov_row.addWidget(btn)
        ai_sec.addLayout(prov_row)

        mod_row = QHBoxLayout()
        mod_row.setSpacing(10)
        mod_lbl = _make_label("Modello Locale", "CardBody")
        mod_lbl.setMinimumWidth(115)
        mod_row.addWidget(mod_lbl)
        self.local_model_combo = QComboBox()
        self.local_model_combo.setMinimumHeight(34)
        self.local_model_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.local_model_combo.addItem("qwen3.5:4b · Consigliato", "qwen3.5:4b")
        self.local_model_combo.addItem("qwen3.5:9b · Alta Precisione", "qwen3.5:9b")
        self.local_model_combo.addItem("qwen3.5:2b · Leggero", "qwen3.5:2b")
        saved_model = db.get_setting("whis_model_mode", "qwen3.5:4b") or "qwen3.5:4b"
        m_idx = self.local_model_combo.findData(saved_model)
        if m_idx >= 0:
            self.local_model_combo.setCurrentIndex(m_idx)
        self.local_model_combo.currentIndexChanged.connect(self._on_local_model_changed)
        mod_row.addWidget(self.local_model_combo, 1)
        ai_sec.addLayout(mod_row)

        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        key_lbl = _make_label("Chiave Gemini", "CardBody")
        key_lbl.setMinimumWidth(115)
        key_row.addWidget(key_lbl)
        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setMinimumHeight(36)
        self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key_edit.setPlaceholderText("AIza... (opzionale)")
        self.gemini_key_edit.setText(saved_key)
        self.gemini_key_edit.editingFinished.connect(self._on_gemini_key_saved)
        key_row.addWidget(self.gemini_key_edit, 1)

        self.gemini_eye_btn = _make_button("", "ghost")
        self.gemini_eye_btn.setCheckable(True)
        self.gemini_eye_btn.setFixedWidth(36)
        self.gemini_eye_btn.setToolTip("Mostra o nascondi chiave API")
        self.gemini_eye_btn.toggled.connect(self._on_toggle_gemini_eye)
        key_row.addWidget(self.gemini_eye_btn)

        self.gemini_verify_btn = _make_button("Verifica", "primary")
        self.gemini_verify_btn.clicked.connect(self._on_verify_gemini_key)
        key_row.addWidget(self.gemini_verify_btn)
        ai_sec.addLayout(key_row)

        key_help_row = QHBoxLayout()
        key_help_row.setSpacing(8)
        key_help_spacer = QWidget()
        key_help_spacer.setFixedWidth(115)
        key_help_row.addWidget(key_help_spacer)

        self.gemini_get_key_btn = _make_button("Ottieni chiave gratuita", "ghost")
        self.gemini_get_key_btn.setToolTip("Apre Google AI Studio nel browser per generare una chiave API gratuita")
        self.gemini_get_key_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GEMINI_AISTUDIO_URL)))
        key_help_row.addWidget(self.gemini_get_key_btn)

        self.gemini_guide_btn = _make_button("Come fare? (Guida 4 Step)", "ghost")
        self.gemini_guide_btn.setToolTip("Apri la guida visiva passo-passo per ottenere la chiave gratuita")
        self.gemini_guide_btn.clicked.connect(self._on_open_gemini_guide)
        key_help_row.addWidget(self.gemini_guide_btn)
        key_help_row.addStretch(1)
        ai_sec.addLayout(key_help_row)

        self.gemini_status_row = QWidget()
        gs_layout = QHBoxLayout(self.gemini_status_row)
        gs_layout.setContentsMargins(0, 2, 0, 0)
        gs_layout.setSpacing(6)
        self.gemini_status_icon = QLabel()
        self.gemini_status_icon.setFixedSize(16, 16)
        self.gemini_status_icon.setVisible(False)
        gs_layout.addWidget(self.gemini_status_icon)
        self.gemini_status_lbl = _make_label("", "PageSubtitle", wrap=True)
        gs_layout.addWidget(self.gemini_status_lbl, 1)
        self.gemini_status_row.setVisible(False)
        ai_sec.addWidget(self.gemini_status_row)

        # Sezione collassabile animata: Impostazioni Avanzate
        self.adv_toggle_btn = QPushButton(" Impostazioni Avanzate")
        self.adv_toggle_btn.setObjectName("CollapseToggle")
        self.adv_toggle_btn.setCheckable(True)
        self.adv_toggle_btn.setChecked(False)
        self.adv_toggle_btn.setIconSize(QSize(13, 13))
        self.adv_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_toggle_btn.toggled.connect(self._on_toggle_advanced_ai)
        ai_sec.addWidget(self.adv_toggle_btn)

        self.adv_container = QWidget()
        self.adv_container.setMaximumHeight(0)
        self.adv_container.setVisible(False)
        adv_layout = QVBoxLayout(self.adv_container)
        adv_layout.setContentsMargins(12, 6, 0, 4)
        adv_layout.setSpacing(10)

        ep_row = QHBoxLayout()
        ep_row.setSpacing(8)
        ep_lbl = _make_label("Endpoint Ollama", "PageSubtitle")
        ep_lbl.setMinimumWidth(115)
        ep_row.addWidget(ep_lbl)
        self.ollama_url_edit = QLineEdit()
        self.ollama_url_edit.setText(db.get_setting("ollama_base_url", "http://localhost:11434") or "http://localhost:11434")
        self.ollama_url_edit.editingFinished.connect(
            lambda: db.set_setting("ollama_base_url", self.ollama_url_edit.text().strip() or "http://localhost:11434")
        )
        ep_row.addWidget(self.ollama_url_edit, 1)
        adv_layout.addLayout(ep_row)

        gm_row = QHBoxLayout()
        gm_row.setSpacing(8)
        gm_lbl = _make_label("Modello Gemini", "PageSubtitle")
        gm_lbl.setMinimumWidth(115)
        gm_row.addWidget(gm_lbl)
        self.gemini_model_combo = QComboBox()
        self.gemini_model_combo.setMinimumHeight(32)
        self.gemini_model_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gemini_model_combo.addItem("gemini-3.5-flash-lite · Ultra-rapido (Consigliato)", "gemini-3.5-flash-lite")
        self.gemini_model_combo.addItem("gemini-3.5-flash · Bilanciato", "gemini-3.5-flash")
        self.gemini_model_combo.addItem("gemini-2.5-pro · Ragionamento", "gemini-2.5-pro")
        saved_gm = db.get_setting("gemini_model", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"
        gm_idx = self.gemini_model_combo.findData(saved_gm)
        if gm_idx >= 0:
            self.gemini_model_combo.setCurrentIndex(gm_idx)
        self.gemini_model_combo.currentIndexChanged.connect(
            lambda: db.set_setting("gemini_model", self.gemini_model_combo.currentData() or "gemini-3.5-flash-lite")
        )
        gm_row.addWidget(self.gemini_model_combo, 1)
        adv_layout.addLayout(gm_row)

        self.hybrid_fallback_chk = QCheckBox("Fallback su Ollama locale quando Cloud non è raggiungibile")
        self.hybrid_fallback_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hybrid_fallback_chk.setChecked((db.get_setting("ai_hybrid_fallback", "1") or "1") == "1")
        self.hybrid_fallback_chk.toggled.connect(
            lambda chk: db.set_setting("ai_hybrid_fallback", "1" if chk else "0")
        )
        adv_layout.addWidget(self.hybrid_fallback_chk)

        ai_sec.addWidget(self.adv_container)
        layout.addLayout(ai_sec)
        layout.addStretch(1)
        return page

    # -----------------------------------------------------------------
    # SEZIONE 2: AUDIO & SCORCIATOIE
    # -----------------------------------------------------------------
    def _build_audio_shortcuts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(20)

        layout.addWidget(_make_label("Audio & Scorciatoie", "PageTitle"))

        # 1. Audio & Feedback
        audio_sec = QVBoxLayout()
        audio_sec.setSpacing(10)
        audio_sec.addWidget(_make_label("Audio & Dettatura", "CardTitle"))

        self.auto_duck_checkbox = QCheckBox("Abbassa automaticamente i volumi di sistema durante la dettatura")
        self.auto_duck_checkbox.setChecked(audio_ducker.is_enabled())
        self.auto_duck_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_duck_checkbox.toggled.connect(self._on_auto_duck_toggled)
        audio_sec.addWidget(self.auto_duck_checkbox)

        self.always_on_mic_chk = QCheckBox("Mantieni il microfono in pre-buffer anche quando Whis è OFF")
        self.always_on_mic_chk.setToolTip(
            "Se disattivato (consigliato), quando spegni Whis il microfono di sistema si spegne completamente "
            "e viene aperto on-demand solo quando premi Shift Destro / F8."
        )
        self.always_on_mic_chk.setChecked((db.get_setting("always_on_prebuffer", "0") or "0").strip() == "1")
        self.always_on_mic_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.always_on_mic_chk.toggled.connect(self._on_always_on_mic_toggled)
        audio_sec.addWidget(self.always_on_mic_chk)

        fb_row = QHBoxLayout()
        fb_row.setSpacing(10)
        fb_lbl = _make_label("Feedback Whis", "CardBody")
        fb_lbl.setMinimumWidth(115)
        fb_row.addWidget(fb_lbl)
        self.settings_audio_combo = QComboBox()
        self.settings_audio_combo.setMinimumHeight(32)
        self.settings_audio_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_audio_combo.addItem("Muto", MODE_MUTE)
        self.settings_audio_combo.addItem("Suono Morbido", MODE_SOFT_CHIME)
        self.settings_audio_combo.addItem("Voce TTS", MODE_VOICE)
        cur_fb_idx = self.settings_audio_combo.findData(audio_feedback.get_mode())
        if cur_fb_idx >= 0:
            self.settings_audio_combo.setCurrentIndex(cur_fb_idx)
        self.settings_audio_combo.currentIndexChanged.connect(self._on_settings_audio_mode_changed)
        fb_row.addWidget(self.settings_audio_combo, 1)
        audio_sec.addLayout(fb_row)
        layout.addLayout(audio_sec)

        layout.addWidget(_make_divider())

        # 2. Scorciatoie Tastiera & Comandi Vocali
        sc_sec = QVBoxLayout()
        sc_sec.setSpacing(10)
        sc_sec.addWidget(_make_label("Scorciatoie & Comandi Vocali", "CardTitle"))

        shortcuts = [
            ("Shift Destro", "Push-to-Talk principale"),
            ("2× Shift Destro", "Attiva / disattiva Mani Libere"),
            ("F8", "Scorciatoia secondaria Push-to-Talk"),
            ("«Whis...»", "Avvia una richiesta all'assistente vocale"),
        ]
        for key_txt, desc_txt in shortcuts:
            row = QHBoxLayout()
            row.setSpacing(12)
            key_lbl = _make_label(key_txt, "ShortcutKey")
            key_lbl.setMinimumWidth(124)
            key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(key_lbl)
            row.addWidget(_make_label(desc_txt, "CardBody", wrap=True), 1)
            sc_sec.addLayout(row)

        layout.addLayout(sc_sec)
        layout.addStretch(1)
        return page

    # -----------------------------------------------------------------
    # SEZIONE 3: GOOGLE & GRUPPI
    # -----------------------------------------------------------------
    def _build_google_groups_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(20)

        layout.addWidget(_make_label("Google & Gruppi", "PageTitle"))

        # 1. Account Google & Calendar FlowTime
        gcal_sec = QVBoxLayout()
        gcal_sec.setSpacing(10)
        gc_top = QHBoxLayout()
        gc_top.setSpacing(8)
        gc_top.addWidget(_make_label("Google Calendar", "CardTitle"))
        gc_top.addStretch(1)
        self.gcal_account_badge = _make_label("Verifica...", "Badge")
        gc_top.addWidget(self.gcal_account_badge)
        gcal_sec.addLayout(gc_top)

        self.gcal_status_lbl = _make_label("", "PageSubtitle", wrap=True)
        gcal_sec.addWidget(self.gcal_status_lbl)

        gc_btns = QHBoxLayout()
        gc_btns.setSpacing(8)
        self.gcal_sync_btn = _make_button("Sincronizza", "primary")
        self.gcal_sync_btn.clicked.connect(self._on_sync_gcal)
        gc_btns.addWidget(self.gcal_sync_btn)

        self.gcal_connect_btn = _make_button("Cambia Account", "ghost")
        self.gcal_connect_btn.clicked.connect(self._on_connect_gcal)
        gc_btns.addWidget(self.gcal_connect_btn)
        gc_btns.addStretch(1)
        gcal_sec.addLayout(gc_btns)
        layout.addLayout(gcal_sec)

        layout.addWidget(_make_divider())

        # 2. Google Drive & Gruppi
        drive_sec = QVBoxLayout()
        drive_sec.setSpacing(10)
        dc_top = QHBoxLayout()
        dc_top.setSpacing(8)
        dc_top.addWidget(_make_label("Google Drive & FlowGroups", "CardTitle"))
        dc_top.addStretch(1)
        self.drive_status_badge = _make_label("Pronto", "BadgeAccent")
        dc_top.addWidget(self.drive_status_badge)
        drive_sec.addLayout(dc_top)

        self.drive_status_lbl = _make_label("", "PageSubtitle", wrap=True)
        drive_sec.addWidget(self.drive_status_lbl)

        dc_btns = QHBoxLayout()
        dc_btns.setSpacing(8)
        self.drive_sync_btn = _make_button("Sincronizza", "primary")
        self.drive_sync_btn.clicked.connect(self._on_sync_drive_now)
        dc_btns.addWidget(self.drive_sync_btn)

        self.drive_folder_btn = _make_button("Cambia Cartella", "ghost")
        self.drive_folder_btn.clicked.connect(self._on_change_drive_folder)
        dc_btns.addWidget(self.drive_folder_btn)

        self.drive_oauth_btn = _make_button("Connetti Drive API (drive.file)", "ghost")
        self.drive_oauth_btn.clicked.connect(self._on_toggle_drive_oauth)
        dc_btns.addWidget(self.drive_oauth_btn)
        dc_btns.addStretch(1)
        drive_sec.addLayout(dc_btns)
        layout.addLayout(drive_sec)

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

    def _on_always_on_mic_toggled(self, checked: bool):
        db.set_setting("always_on_prebuffer", "1" if checked else "0")
        eng = getattr(self.hub, "engine", None)
        if eng is not None and hasattr(eng, "set_always_on_mic"):
            hf_on = bool(getattr(self.hub, "is_handsfree_enabled", False))
            eng.set_always_on_mic(bool(checked or hf_on))

    def _on_settings_audio_mode_changed(self):
        mode = self.settings_audio_combo.currentData()
        if mode and mode != audio_feedback.get_mode():
            audio_feedback.set_mode(mode)

    def _on_theme_pill_clicked(self, theme_key: str):
        self.hub._set_theme(theme_key)
        self.setStyleSheet(self.hub.styleSheet())
        self._refresh_icons()

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
        tier_labels = {"auto": "Automatico", "pro": "Pro", "standard": "Standard", "eco": "Eco"}
        suffix = f" · {rec_label}" if tier_key == "auto" else ""
        self.hw_tier_badge.setText(f"Profilo: {tier_labels.get(tier_key, 'Automatico')}{suffix}")
        hw = detect_hardware()
        resolved = hw.recommended_tier if tier_key == "auto" else HardwareTier(tier_key)
        prof = build_profile(resolved, hw)
        self.hw_badge_lbl.setText(prof.badge_text)
        self.hw_detail_lbl.setText(prof.detail_text)

    def _on_ai_provider_changed(self, provider_key: str):
        db.set_setting("ai_provider", provider_key)
        has_key = bool(self.gemini_key_edit.text().strip())
        if provider_key == "adaptive":
            set_active_runtime_engine("Adattivo • Gemini Online" if has_key else "Adattivo • Qwen 3.5 Offline")
        elif provider_key == "gemini":
            set_active_runtime_engine("Cloud • Gemini Online")
        elif provider_key == "hybrid":
            set_active_runtime_engine("Ibrido • Gemini + Qwen 3.5")
        else:
            set_active_runtime_engine("Locale • Qwen 3.5")
        if hasattr(self, "ai_runtime_lbl"):
            self.ai_runtime_lbl.setText(get_active_runtime_engine())

    def _on_runtime_engine_updated(self, label: str):
        if hasattr(self, "ai_runtime_lbl"):
            self.ai_runtime_lbl.setText(label)

    def _on_open_gemini_guide(self):
        dlg = GeminiKeyGuideDialog(
            parent=self,
            theme_key=getattr(self.hub, "_current_theme", "obsidian"),
            reading_mode=getattr(self.hub, "_reading_mode", False),
        )
        dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

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
        tkey = getattr(self.hub, "_current_theme", "obsidian")
        self.gemini_eye_btn.setIcon(
            get_icon("eye_off" if checked else "eye", theme_key=tkey, role="subtitle", size=15)
        )

    def _on_gemini_key_saved(self):
        db.set_setting("gemini_api_key", self.gemini_key_edit.text().strip())
        cloud_sync.push_account_config_to_drive(async_bg=True)

    def _on_verify_gemini_key(self):
        key = self.gemini_key_edit.text().strip()
        db.set_setting("gemini_api_key", key)
        cloud_sync.push_account_config_to_drive(async_bg=True)
        self.gemini_status_row.setVisible(True)
        self.gemini_status_icon.setVisible(False)
        if not key:
            tkey = getattr(self.hub, "_current_theme", "obsidian")
            self.gemini_status_icon.setPixmap(get_pixmap("close", theme_key=tkey, role="danger", size=14))
            self.gemini_status_icon.setVisible(True)
            self.gemini_status_lbl.setText("Inserisci una chiave API Gemini prima di verificare.")
            return

        if self._gemini_verify_worker is not None and self._gemini_verify_worker.isRunning():
            return

        self.gemini_verify_btn.setEnabled(False)
        self.gemini_status_lbl.setText("Verifica in corso...")
        model = (self.gemini_model_combo.currentData() or "gemini-3.5-flash-lite") if hasattr(self, "gemini_model_combo") else "gemini-3.5-flash-lite"

        worker = _GeminiVerifyWorker(key, str(model), parent=self)
        worker.verifyFinished.connect(self._on_verify_gemini_done, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        self._gemini_verify_worker = worker
        worker.start()

    def _on_verify_gemini_done(self, ok: bool, msg: str):
        self.gemini_verify_btn.setEnabled(True)
        self.gemini_status_row.setVisible(True)
        tkey = getattr(self.hub, "_current_theme", "obsidian")
        icon_name = "check" if ok else "close"
        role = "success" if ok else "danger"
        self.gemini_status_icon.setPixmap(get_pixmap(icon_name, theme_key=tkey, role=role, size=14))
        self.gemini_status_icon.setVisible(True)
        self.gemini_status_lbl.setText(msg)
        self._gemini_verify_worker = None
        if ok:
            db.set_setting("ai_provider", "adaptive")
            if hasattr(self, "_provider_buttons") and "adaptive" in self._provider_buttons:
                self._provider_buttons["adaptive"].setChecked(True)
            set_active_runtime_engine("Adattivo • Gemini Online")
            if hasattr(self, "ai_runtime_lbl"):
                self.ai_runtime_lbl.setText(get_active_runtime_engine())
            if hasattr(self.hub, "_refresh_ai_onboarding_and_badge"):
                self.hub._refresh_ai_onboarding_and_badge()

    def _on_toggle_advanced_ai(self, checked: bool):
        tkey = getattr(self.hub, "_current_theme", "obsidian")
        self.adv_toggle_btn.setIcon(
            get_icon("caret_down" if checked else "caret_right", theme_key=tkey, role="subtitle", size=13)
        )
        if self._adv_anim is not None:
            self._adv_anim.stop()

        target_h = self.adv_container.sizeHint().height() + 12 if checked else 0
        if checked:
            self.adv_container.setVisible(True)

        anim = QPropertyAnimation(self.adv_container, b"maximumHeight", self)
        anim.setDuration(140)
        anim.setStartValue(self.adv_container.maximumHeight())
        anim.setEndValue(target_h)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if not checked:
            anim.finished.connect(lambda: self.adv_container.setVisible(False))
        else:
            anim.finished.connect(lambda: self.adv_container.setMaximumHeight(16777215))
        self._adv_anim = anim
        anim.start()

    def _refresh_gcal_status(self):
        from core.calendar_sync import calendar_manager
        if calendar_manager.is_auth_expired():
            self.gcal_account_badge.setText("Sessione scaduta")
            self.gcal_status_lbl.setText(
                "⚠️ Token Google scaduto o revocato (invalid_grant). Clicca 'Riconnetti Google' per rinnovare."
            )
            self.gcal_connect_btn.setText("Riconnetti Google")
            self.gcal_sync_btn.setEnabled(False)
        elif calendar_manager.has_token_file():
            self.gcal_account_badge.setText("Connesso")
            self.gcal_status_lbl.setText("Connesso • Calendario FlowTime sincronizzato.")
            self.gcal_connect_btn.setText("Cambia Account")
            self.gcal_sync_btn.setEnabled(True)
        elif calendar_manager.has_credentials_file():
            self.gcal_account_badge.setText("Da autorizzare")
            self.gcal_status_lbl.setText("Autorizza l'account Google per attivare la sincronizzazione.")
            self.gcal_connect_btn.setText("Connetti Google")
            self.gcal_sync_btn.setEnabled(False)
        else:
            self.gcal_account_badge.setText("Solo Locale")
            self.gcal_status_lbl.setText("Modalità offline attiva. Aggiungi credentials.json per collegare Google.")
            self.gcal_connect_btn.setText("Configura Google")
            self.gcal_sync_btn.setEnabled(False)

    def _on_connect_gcal(self):
        self.hub._on_connect_calendar_clicked()
        QTimer.singleShot(800, self._refresh_gcal_status)

    def _on_sync_gcal(self):
        self.hub._on_ft_sync_google_clicked()
        self.gcal_status_lbl.setText("Sincronizzazione in corso...")

    def _refresh_drive_status(self):
        sync_dir = cloud_sync.get_sync_base_dir()
        pending = cloud_sync.get_pending_count()
        outbox_lib = os.path.join(cloud_sync.get_outbox_study_dir(), "library.json")
        lib_ok = os.path.exists(outbox_lib)
        oauth_ok = flow_groups.has_token()
        groups_cnt = len(db.get_flow_groups())
        self.drive_status_badge.setText(
            f"{pending} in coda" if pending > 0 else ("Drive API + Locale" if oauth_ok and lib_ok else ("Drive API Attiva" if oauth_ok else ("Sincronizzato" if lib_ok else "Pronto")))
        )
        self.drive_status_lbl.setText(
            f"Cartella locale: {sync_dir} • Gruppi collegati: {groups_cnt}"
        )
        if hasattr(self, "drive_oauth_btn"):
            self.drive_oauth_btn.setText(
                "Disconnetti Drive API" if oauth_ok else "Connetti Drive API (drive.file)"
            )

    def _on_toggle_drive_oauth(self):
        if flow_groups.has_token():
            flow_groups.disconnect_drive()
            self._refresh_drive_status()
            if hasattr(self.hub, "_refresh_account_badge"):
                self.hub._refresh_account_badge()
            return
        try:
            self.drive_oauth_btn.setEnabled(False)
            self.drive_oauth_btn.setText("Connessione...")
            flow_groups.get_drive_service(interactive=True)
            if hasattr(self.hub, "_startup_cloud_sync"):
                self.hub._startup_cloud_sync()
        except Exception as exc:
            self.drive_status_lbl.setText(f"Errore OAuth Drive: {exc}")
        finally:
            self.drive_oauth_btn.setEnabled(True)
            self._refresh_drive_status()
            if hasattr(self.hub, "_refresh_account_badge"):
                self.hub._refresh_account_badge()

    def _on_sync_drive_now(self):
        try:
            res = cloud_sync.sync_with_drive(interactive=False)
            if res.get("gemini_key_imported"):
                saved_key = (db.get_setting("gemini_api_key", "") or "").strip()
                if saved_key and hasattr(self, "gemini_key_edit"):
                    self.gemini_key_edit.setText(saved_key)
            self._refresh_drive_status()
            if hasattr(self.hub, "_refresh_account_badge"):
                self.hub._refresh_account_badge()
            self.drive_status_lbl.setText(
                f"{self.drive_status_lbl.text()} • Indice aggiornato."
            )
        except Exception as exc:
            self.drive_status_lbl.setText(f"Errore sincronizzazione: {exc}")

    def _on_change_drive_folder(self):
        current_dir = cloud_sync.get_sync_base_dir()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Seleziona cartella Google Drive",
            current_dir,
        )
        if chosen:
            cloud_sync.set_sync_base_dir(chosen)
            self._refresh_drive_status()
