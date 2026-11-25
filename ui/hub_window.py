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

from datetime import datetime, date as dt_date, timedelta
from typing import Callable, Dict, List, Optional, Any
import ctypes
from ctypes import wintypes
import os
import re
import sys
import tempfile
import threading
import time
import pyperclip
try:
    import psutil
except ImportError:
    psutil = None

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
from core.exporter import export_to_md, export_to_docx, export_to_pdf, export_to_tex
from core.audio_sync import archive_audio_for_note, save_note_audio_sync, load_note_audio_sync
from ui.scientific_editor import DictationGhostLineEdit, ScientificNoteEditor, strip_markdown_syntax
from ui.rsvp_reader import RsvpReaderModal, tokenize_rsvp_markdown
from ui.audio_sync_bar import AudioSyncBar
from ui.notes_tree import (
    SamsungNotesTreeWidget,
    ROLE_NODE_DATA,
    ROLE_COUNT,
    ROLE_FOLDER_COLOR,
    ROLE_IS_FOLDER,
    ROLE_LOCKED_FOLDER,
    make_vector_folder_icon,
)
from ui.command_palette import GlobalCommandPalette
from ui.toast_manager import ToastManager, DiagnosticsDialog
from ui.whats_new import WhatsNewDialog, has_unread_whats_new, should_auto_show_whats_new
from ui.onboarding_wizard import OnboardingWizardDialog, should_show_onboarding
from core.logging_config import APP_VERSION
from core.cloud_sync import cloud_sync
from core.whis_agent import WhisAgent, WhisStreamWorker
from core.audio_feedback import (
    audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE,
    VOICE_PRESETS, DEFAULT_VOICE_KEY, TEST_PHRASE
)
from core.audio_ducker import audio_ducker


import logging
logger = logging.getLogger("FlowLocalHub")

from ui.themes import (
    GPU_AVAILABLE,
    _DARK_THEME_KEYS,
    _is_dark_theme,
    _THEME_PALETTES,
    FONT_PRESETS,
    _resolve_font_family,
    _resolve_palette,
    get_hub_qss,
    HUB_QSS,
    MODE_TABS,
    _format_timestamp,
)
from ui.theme_widgets import (
    MorphologicalSlidingPill,
    ThemedPushButton,
    _make_button,
    _make_divider,
    _make_empty_state,
    _make_label,
    ScrollList,
    _make_card,
    OrbBrandBadge,
    ThemeRevealOverlay,
    AuroraDivider,
    SlidingStackedWidget,
    AnimatedSegmentedBox,
)
from ui.icons import get_icon, get_pixmap, apply_win11_titlebar
from ui.settings_dialog import HubSettingsDialog, GeminiKeyGuideDialog, GEMINI_AISTUDIO_URL
from core.gemini_client import (
    add_runtime_engine_listener,
    get_active_runtime_engine,
    is_ollama_available,
)
from core.flow_groups import flow_groups
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from ui.flownotes_workspace import (
    EditorPane,
    JoinOrCreateGroupDialog,
    TabContext,
    WorkspaceSwitcherButton,
)
from ui.flowtime_widgets import (
    FLOWTIME_COLOR_PALETTE,
    LinkEventNoteDialog,
    TimelineQuickAddDialog,
    FlowTimeCalendarWidget,
    FlowTimeTodoSidebar,
    FlowTimeDayTimeline,
    FlowTimeWeekView,
    PomodoroRingWidget,
    ExamAddDialog,
)
from ui.study_widgets import (
    AudioTranscribeThread,
    ExtractFlashcardsThread,
    _AutoSaveNoteThread,
    _SummarizeNoteThread,
    DocumentImportThread,
    CloudQueueProcessThread,
    NewSubjectTopicDialog,
    ConnectNoteDialog,
    AutoLinkThread,
    KnowledgeGraphWidget,
    QuickRuleDialog,
    SpotifyMiniBar,
    TimeMachineDialog,
)


class FlowLocalHub(QMainWindow):
    note_added = pyqtSignal()
    flowtime_sync_done = pyqtSignal(bool, str)
    _aiRuntimeSignal = pyqtSignal(str)
    _ollamaProbeSignal = pyqtSignal(bool)
    _cloudSyncDoneSignal = pyqtSignal(dict)
    _avatarLoadedSignal = pyqtSignal(bytes)
    _calendarAuthDoneSignal = pyqtSignal(bool, str)

    def __init__(self, on_device_changed: Optional[Callable] = None, engine=None):
        super().__init__()
        self._ollama_detected: Optional[bool] = None
        self._account_avatar_pixmap: Optional[QPixmap] = None
        self._account_avatar_url_loaded: str = ""
        self._cloud_sync_running: bool = False
        self._startup_checks_done: bool = False
        self._toast_manager = ToastManager(self)
        self._aiRuntimeSignal.connect(self._on_ai_runtime_changed, Qt.ConnectionType.QueuedConnection)
        self._ollamaProbeSignal.connect(self._on_ollama_probe_finished, Qt.ConnectionType.QueuedConnection)
        self._cloudSyncDoneSignal.connect(self._on_cloud_sync_done, Qt.ConnectionType.QueuedConnection)
        self._avatarLoadedSignal.connect(self._on_avatar_loaded, Qt.ConnectionType.QueuedConnection)
        self._calendarAuthDoneSignal.connect(self._on_calendar_auth_finished, Qt.ConnectionType.QueuedConnection)
        add_runtime_engine_listener(lambda lbl: self._aiRuntimeSignal.emit(lbl))
        # API pubblica mantenuta per compatibilità con main.py / pipeline audio
        self.on_device_changed = on_device_changed
        self.engine = engine
        self.live_volume = 0.0
        self._transcribe_thread = None
        self._import_thread = None
        self._autolink_thread = None
        self._note_cards: Dict[int, QFrame] = {}
        self._ft_editing_event_id: Optional[int] = None

        # Blocco 4: Pomodoro Focus Timer & Time Machine state
        self._pomo_mode: str = "focus"  # "focus" | "break"
        self._pomo_focus_mins: int = 25
        self._pomo_break_mins: int = 5
        self._pomo_total_secs: int = 25 * 60
        self._pomo_remaining_secs: int = 25 * 60
        self._pomo_running: bool = False
        self._pomo_timer = QTimer(self)
        self._pomo_timer.setInterval(1000)
        self._pomo_timer.timeout.connect(self._on_pomo_tick)

        self._nk_last_snapshot_ts: float = 0.0
        self._nk_dirty_for_snapshot: bool = False

        self.setWindowTitle("FlowLocal Hub")
        self.resize(1120, 680)
        self.setMinimumSize(920, 580)
        settings = QSettings("FlowLocal", "Hub")
        saved_theme = str(settings.value("theme", "obsidian") or "obsidian")
        self._current_theme = saved_theme if saved_theme in _THEME_PALETTES else "obsidian"
        raw_rm = settings.value("reading_mode", False)
        self._reading_mode = str(raw_rm).lower() in ("true", "1", "yes") if not isinstance(raw_rm, bool) else raw_rm
        saved_fp = str(settings.value("font_preset", "auto") or "auto")
        self._font_preset = saved_fp if saved_fp in FONT_PRESETS else "auto"
        self._custom_font_family = str(settings.value("custom_font_family", "") or "")
        try:
            self._font_scale = max(90, min(120, int(settings.value("font_scale", 100) or 100)))
        except Exception:
            self._font_scale = 100
        ThemedPushButton.current_theme = self._current_theme
        ThemedPushButton.reading_mode = self._reading_mode
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )

        self.note_added.connect(self._refresh_notekeeper)
        self.flowtime_sync_done.connect(self._on_ft_sync_finished)
        try:
            from core.calendar_sync import calendar_manager
            calendar_manager.add_sync_listener(lambda ok, msg: self.flowtime_sync_done.emit(ok, msg))
        except Exception:
            pass

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

        self._nk_current_filter_folder_id: Optional[int] = None
        self._build_ui()
        self._sync_editor_typography()
        self._refresh_current_page()

        # Scorciatoia globale Command Palette (Ctrl+K)
        self._cmd_palette_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._cmd_palette_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._cmd_palette_shortcut.activated.connect(self._open_command_palette)

        # Sync iniziale e periodico Google Calendar & Cloud Account in background (se connesso)
        QTimer.singleShot(1500, self._startup_google_calendar_sync)
        QTimer.singleShot(2200, self._startup_cloud_sync)
        self.gcal_sync_timer = QTimer(self)
        self.gcal_sync_timer.timeout.connect(self._periodic_google_calendar_sync)
        self.gcal_sync_timer.start(120000)

        # Aggiornamento leggero della Cronologia: ricostruisce solo se arrivano nuovi dettati
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_new_transcriptions)
        self.poll_timer.start(2500)

        # Timer leggero Coda Cloud (ogni 10s, sola lettura disco, 0 CPU / 0 GPU)
        self.cloud_poll_timer = QTimer(self)
        self.cloud_poll_timer.timeout.connect(self._poll_cloud_queue)
        self.cloud_poll_timer.start(10000)
        self._poll_cloud_queue()

        # Probe asincrono non bloccante per banner onboarding su PC senza Ollama né chiave Gemini
        threading.Thread(
            target=self._probe_ollama_worker,
            daemon=True,
            name="FlowLocal-OllamaProbe",
        ).start()

    # -----------------------------------------------------------------
    # API pubblica
    # -----------------------------------------------------------------
    def update_live_volume(self, rms_normalized: float):
        """Chiamato dal thread audio: memorizza solo il valore (nessuna operazione UI cross-thread)."""
        self.live_volume = float(rms_normalized)

    def closeEvent(self, event):
        self._save_nk_snapshot_if_dirty("Chiusura finestra")
        if hasattr(self, "save_workspace_session_state"):
            try:
                self.save_workspace_session_state()
            except Exception:
                pass
        event.ignore()
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_windows_titlebar_theme(self._current_theme)
        self._refresh_current_page()
        self._refresh_system_status_pill()
        if hasattr(self, "_toast_manager"):
            self._toast_manager.reposition()
        if not self._startup_checks_done:
            self._startup_checks_done = True
            QTimer.singleShot(700, self._run_startup_health_checks)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_toast_manager"):
            self._toast_manager.reposition()

    def show_toast(
        self,
        message: str,
        title: str = "",
        level: str = "info",
        duration_ms: int = 5200,
        action_label: str = "",
        on_action: Optional[Callable[[], None]] = None,
    ):
        """Mostra una notifica toast non bloccante in basso a destra nell'Hub."""
        if hasattr(self, "_toast_manager") and self._toast_manager is not None:
            self._toast_manager.show_toast(
                message=message,
                title=title,
                level=level,
                duration_ms=duration_ms,
                action_label=action_label,
                on_action=on_action,
            )

    def _active_is_dark(self) -> bool:
        return _is_dark_theme(getattr(self, "_current_theme", "obsidian"))

    def _active_palette(self) -> dict:
        return _resolve_palette(
            getattr(self, "_current_theme", "obsidian"),
            getattr(self, "_reading_mode", False),
            font_preset=getattr(self, "_font_preset", "auto"),
            custom_font=getattr(self, "_custom_font_family", ""),
        )

    def _apply_windows_titlebar_theme(self, theme_key: str, reading_mode: Optional[bool] = None):
        """Sincronizza la barra del titolo nativa di Windows 10/11 con il tema attivo."""
        if sys.platform != "win32":
            return
        if reading_mode is None:
            reading_mode = getattr(self, "_reading_mode", False)
        try:
            import ctypes
            hwnd = int(self.winId())
            if not hwnd:
                return
            dwmapi = ctypes.windll.dwmapi
            is_dark = 1 if _is_dark_theme(theme_key) else 0
            dark_val = ctypes.c_int(is_dark)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
            if reading_mode:
                cap_hex = 0x000D1115 if is_dark else 0x00D0E5EF  # #15110D / #EFE5D0 (BGR)
                text_hex = 0x00E0EBF5 if is_dark else 0x00141F2A
            else:
                caption_colors = {
                    "obsidian": 0x000B0705,             # #05070B (BGR)
                    "light": 0x00FFFFFF,                # #FFFFFF (BGR)
                    "orb": 0x00FFFDF0,                  # #F0FDFF (BGR)
                    "dark_orb": 0x0050320E,             # #0E3250 (BGR)
                    "ghibli_spring": 0x00E8F3ED,        # #EDF3E8 (BGR)
                    "pixel_chill": 0x00444037,          # #374044 (BGR)
                    "katana": 0x001A1A1F,               # #1F1A1A (BGR)
                    "tactical_hud": 0x001D1813,         # #13181D (BGR)
                    "industrial_mecha": 0x001E1C1A,     # #1A1C1E (BGR)
                    "amber_crt": 0x0009121A,            # #1A1209 (BGR)
                    "starfield_alabaster": 0x00DDE4E6,  # #E6E4DD (BGR)
                    "cyber_synthwave": 0x00240E16,      # #160E24 (BGR)
                }
                cap_hex = caption_colors.get(theme_key, 0x000B0705)
                text_hex = 0x00FCFAF8 if is_dark else 0x002A170F
            cap_val = ctypes.c_int(cap_hex)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap_val), ctypes.sizeof(cap_val))
            text_val = ctypes.c_int(text_hex)
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_val), ctypes.sizeof(text_val))
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Struttura principale (TOP BAR 4 MONDI: FlowType | FlowNotes | FlowTime | Whis)
    # -----------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("HubRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.nav_buttons_by_page: Dict[int, QPushButton] = {}
        main_layout.addWidget(self._build_top_bar())
        main_layout.addWidget(self._build_flowtype_subbar())

        # Ordine spaziale direzionale (Top Bar: [0] FlowType -> [1] FlowNotes -> [2] FlowTime -> [3] Whis,
        # più sub-viste interne Dettatura/Dizionario/Stili e Quaderno/Flashcard/Grafo)
        self.pages = SlidingStackedWidget(
            duration_ms=280,
            spatial_ranks={
                0: 0,   # FlowType: Dettatura
                5: 1,   # FlowType: Dizionario & Snippet
                6: 2,   # FlowType: Stili & Prompt
                1: 10,  # FlowNotes: Quaderno
                2: 11,  # FlowNotes: Flashcard
                3: 12,  # FlowNotes: Grafo
                7: 20,  # FlowTime
                4: 30,  # Whis
            },
        )
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self._build_flowtype_page())             # Index 0: 🎙️ FlowType
        self.pages.addWidget(self._build_flowstudy_notekeeper_page()) # Index 1: 📓 FlowNotes - Quaderno
        self.pages.addWidget(self._build_flowstudy_flashcards_page()) # Index 2: 📓 FlowNotes - Flashcard
        self.pages.addWidget(self._build_flowstudy_graph_page())      # Index 3: 📓 FlowNotes - Grafo
        self.pages.addWidget(self._build_whis_page())                 # Index 4: ✨ Whis
        self.pages.addWidget(self._build_dictionary_page())           # Index 5: 📖 Dizionario & Snippet
        self.pages.addWidget(self._build_styles_page())               # Index 6: ⚙️ Stili & Prompt
        self.pages.addWidget(self._build_flowtime_page())             # Index 7: 🗓️ FlowTime
        self.pages.addWidget(self._build_flowstudy_focus_page())      # Index 8: 📓 FlowNotes - Focus & Esami
        self.pages.currentChanged.connect(self._sync_top_nav_state)
        main_layout.addWidget(self.pages, 1)

        self._on_nav_clicked(0)

    def _build_top_bar(self) -> QFrame:
        top_bar = QFrame()
        top_bar.setObjectName("TopNavBar")
        top_bar.setFixedHeight(52)

        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(10)

        # A sinistra: solo Logo + FlowLocal (contenitore rigido per stabilità del menù centrale)
        left_host = QWidget()
        left_host.setStyleSheet("background: transparent;")
        left_box = QHBoxLayout(left_host)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(8)
        left_box.addWidget(OrbBrandBadge(24))
        title = QLabel("FlowLocal")
        title.setObjectName("AppTitle")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        left_box.addWidget(title)

        self.system_status_pill = QPushButton("● Pronto")
        self.system_status_pill.setObjectName("FilterPill")
        self.system_status_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.system_status_pill.setToolTip("Stato motori AI e sincronizzazione — Clicca per aprire la diagnostica")
        self.system_status_pill.clicked.connect(self._open_diagnostics_dialog)
        left_box.addWidget(self.system_status_pill)
        self._refresh_system_status_pill()

        layout.addWidget(left_host)

        layout.addStretch(1)

        # Al centro: Segmented Control a 4 pillole con pillola attiva scorrevole (FlowType | FlowNotes | FlowTime | Whis)
        seg_box = AnimatedSegmentedBox(
            box_object_name="TopSegmentBox",
            pill_object_name="TopSlidingPill",
            duration_ms=280,
        )
        self.top_segment_box = seg_box
        seg_box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        seg_layout = QHBoxLayout(seg_box)
        seg_layout.setContentsMargins(4, 3, 4, 3)
        seg_layout.setSpacing(4)

        self.top_world_group = QButtonGroup(self)
        self.top_world_group.setExclusive(True)

        self.top_flowtype_btn = ThemedPushButton(" FlowType")
        self.top_flownotes_btn = ThemedPushButton(" FlowNotes")
        self.top_flowtime_btn = ThemedPushButton(" FlowTime")
        self.top_whis_btn = ThemedPushButton(" Whis")

        for idx, (btn, target_page, icon_name) in enumerate([
            (self.top_flowtype_btn, 0, "mic"),
            (self.top_flownotes_btn, 1, "notes"),
            (self.top_flowtime_btn, 7, "calendar"),
            (self.top_whis_btn, 4, "sparkles"),
        ]):
            btn.setObjectName("TopSegmentBtn")
            btn.setCheckable(True)
            btn.setIcon(get_icon(icon_name, theme_key=self._current_theme, role="text", size=14))
            btn.setIconSize(QSize(14, 14))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, p=target_page: self._on_nav_clicked(p))
            self.top_world_group.addButton(btn, idx)
            seg_layout.addWidget(btn)
            seg_box.register_button(btn)

        layout.addWidget(seg_box)
        layout.addStretch(1)

        # Mini-pillola Pomodoro attiva in background (visibile solo quando in corso / in pausa)
        self.pomo_top_pill = ThemedPushButton(" 25:00")
        self.pomo_top_pill.setObjectName("FilterPill")
        self.pomo_top_pill.setIcon(get_icon("clock", theme_key=self._current_theme, role="accent", size=13))
        self.pomo_top_pill.setIconSize(QSize(13, 13))
        self.pomo_top_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pomo_top_pill.setToolTip("Pomodoro attivo — Clicca per aprire Focus & Esami in FlowNotes")
        self.pomo_top_pill.setStyleSheet("padding: 0 10px; font-size: 11px; min-height: 26px; font-weight: 600;")
        self.pomo_top_pill.clicked.connect(self._jump_to_flowtime_focus)
        self.pomo_top_pill.hide()
        layout.addWidget(self.pomo_top_pill)

        # A destra: Barra Spotify + Modalità Lettura + Badge Profilo Account + Rotellina Impostazioni
        right_host = QWidget()
        right_host.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        right_host.setStyleSheet("background: transparent;")
        right_row = QHBoxLayout(right_host)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(6)

        self.spotify_bar = SpotifyMiniBar(self)
        self.spotify_bar.set_theme_mode(self._active_is_dark())
        right_row.addWidget(self.spotify_bar)

        self.reading_mode_btn = ThemedPushButton(" Lettura")
        self.reading_mode_btn.setObjectName("FilterPill")
        self.reading_mode_btn.setCheckable(True)
        self.reading_mode_btn.setChecked(self._reading_mode)
        self.reading_mode_btn.setIcon(get_icon("book", theme_key=self._current_theme, role="subtitle", size=13))
        self.reading_mode_btn.setIconSize(QSize(13, 13))
        self.reading_mode_btn.setFixedWidth(88)
        self.reading_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reading_mode_btn.setToolTip("Modalità Lettura: toni caldi per la vista")
        self.reading_mode_btn.setStyleSheet("padding: 0 8px; font-size: 11px; min-height: 26px;")
        self.reading_mode_btn.toggled.connect(self._toggle_reading_mode)
        right_row.addWidget(self.reading_mode_btn)

        self.account_badge_btn = QPushButton(" Accedi")
        self.account_badge_btn.setObjectName("FilterPill")
        self.account_badge_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_badge_btn.setIconSize(QSize(20, 20))
        self.account_badge_btn.setToolTip("Account Google & Stato Sincronizzazione")
        self.account_badge_btn.clicked.connect(self._on_account_badge_clicked)
        right_row.addWidget(self.account_badge_btn)
        self._refresh_account_badge()

        self.whats_new_btn = ThemedPushButton("")
        self.whats_new_btn.setObjectName("SettingsGearBtn")
        self.whats_new_btn.setIcon(get_icon("lightbulb", theme_key=self._current_theme, role="subtitle", size=15))
        self.whats_new_btn.setIconSize(QSize(15, 15))
        self.whats_new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.whats_new_btn.setToolTip("Novità, bug fix e suggerimenti nascosti")
        self.whats_new_btn.clicked.connect(self._open_whats_new_dialog)
        right_row.addWidget(self.whats_new_btn)
        self._refresh_whats_new_badge()

        self.settings_btn = ThemedPushButton("")
        self.settings_btn.setObjectName("SettingsGearBtn")
        self.settings_btn.setIcon(get_icon("settings", theme_key=self._current_theme, role="subtitle", size=15))
        self.settings_btn.setIconSize(QSize(15, 15))
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Impostazioni FlowLocal")
        self.settings_btn.clicked.connect(self._on_open_settings_clicked)
        right_row.addWidget(self.settings_btn)

        layout.addWidget(right_host)

        return top_bar

    def _make_circular_account_icon(self, avatar_px: Optional[QPixmap], initials: str, synced: bool) -> QIcon:
        from PyQt6.QtGui import QPainterPath
        size = 22
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if avatar_px and not avatar_px.isNull():
            path = QPainterPath()
            path.addEllipse(0, 0, size - 2, size - 2)
            p.setClipPath(path)
            scaled = avatar_px.scaled(size - 2, size - 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(0, 0, scaled)
            p.setClipping(False)
        else:
            p.setBrush(QColor("#6366F1" if synced else "#64748B"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(0, 0, size - 2, size - 2)
            p.setPen(QColor("#FFFFFF"))
            f = p.font()
            f.setPixelSize(10)
            f.setBold(True)
            p.setFont(f)
            p.drawText(0, 0, size - 2, size - 2, Qt.AlignmentFlag.AlignCenter, initials[:2].upper() or "G")

        # Indicatore verde/grigio di sync in basso a destra
        dot_col = QColor("#10B981") if synced else QColor("#94A3B8")
        p.setBrush(dot_col)
        p.setPen(QPen(QColor("#0F172A"), 1.5))
        p.drawEllipse(size - 9, size - 9, 8, 8)
        p.end()
        return QIcon(pm)

    def _refresh_account_badge(self):
        if not hasattr(self, "account_badge_btn"):
            return
        acc = db.get_google_account()
        connected = flow_groups.has_token()
        name = acc.get("display_name") or acc.get("email") or ("Google Sync" if connected else "Accedi Google")
        short_name = name.split()[0] if " " in name else name
        if len(short_name) > 14:
            short_name = short_name[:13] + "…"
        initials = "".join(w[0] for w in name.split()[:2]) or "G"
        avatar_url = acc.get("avatar_url") or ""
        if connected and avatar_url and avatar_url != self._account_avatar_url_loaded:
            self._account_avatar_url_loaded = avatar_url
            def _dl(url=avatar_url):
                try:
                    import urllib.request
                    with urllib.request.urlopen(url, timeout=6) as resp:
                        self._avatarLoadedSignal.emit(resp.read())
                except Exception:
                    pass
            threading.Thread(target=_dl, daemon=True).start()
        elif not connected:
            self._account_avatar_pixmap = None
            self._account_avatar_url_loaded = ""

        self.account_badge_btn.setText(f" {short_name}")
        self.account_badge_btn.setIcon(self._make_circular_account_icon(self._account_avatar_pixmap, initials, connected))
        self.account_badge_btn.setStyleSheet(
            "padding: 0 10px 0 6px; font-size: 11px; min-height: 26px; font-weight: 600;"
        )
        tip = f"{acc.get('display_name') or 'Account Google'}\n{acc.get('email') or ''}".strip() if connected else "Clicca per accedere con Google e sincronizzare"
        self.account_badge_btn.setToolTip(tip)

    def _on_avatar_loaded(self, raw_bytes: bytes):
        pm = QPixmap()
        if pm.loadFromData(raw_bytes):
            self._account_avatar_pixmap = pm
            self._refresh_account_badge()

    def _on_account_badge_clicked(self):
        menu = QMenu(self)
        acc = db.get_google_account()
        connected = flow_groups.has_token()
        gemini_ok = bool((db.get_setting("gemini_api_key", "") or "").strip())

        if connected:
            hdr = menu.addAction(f"{acc.get('display_name') or 'Account Google'} ({acc.get('email') or 'connesso'})")
        else:
            hdr = menu.addAction("Account Google non connesso")
        hdr.setEnabled(False)

        gem_act = menu.addAction(f"Chiave Gemini: {'Attiva (Sync)' if gemini_ok else 'Mancante'}")
        gem_act.setEnabled(False)
        menu.addSeparator()

        sync_act = menu.addAction(get_icon("refresh", theme_key=self._current_theme, size=14), "Sincronizza ora")
        sync_act.triggered.connect(lambda: self._run_cloud_sync(interactive=not connected))

        if connected:
            disc_act = menu.addAction(get_icon("close", theme_key=self._current_theme, role="danger", size=14), "Disconnetti")
            def _disconnect():
                flow_groups.disconnect_drive()
                self._refresh_account_badge()
                self._refresh_notekeeper()
            disc_act.triggered.connect(_disconnect)
        else:
            login_act = menu.addAction(get_icon("cloud", theme_key=self._current_theme, size=14), "Accedi con Google")
            login_act.triggered.connect(lambda: self._run_cloud_sync(interactive=True))

        menu.exec(self.account_badge_btn.mapToGlobal(QPoint(0, self.account_badge_btn.height())))

    def _startup_cloud_sync(self):
        if flow_groups.has_token():
            self._run_cloud_sync(interactive=False)

    def _run_cloud_sync(self, interactive: bool = False):
        if self._cloud_sync_running:
            return
        self._cloud_sync_running = True
        if interactive:
            try:
                flow_groups.get_drive_service(interactive=True)
                self._refresh_account_badge()
            except Exception as exc:
                self._cloud_sync_running = False
                QMessageBox.warning(self, "Accesso Google", str(exc))
                return

        def _worker():
            try:
                res = cloud_sync.sync_with_drive(interactive=False)
                self._cloudSyncDoneSignal.emit(res)
            except Exception as e:
                self._cloudSyncDoneSignal.emit({"error": str(e)})

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cloud_sync_done(self, res: dict):
        self._cloud_sync_running = False
        self._refresh_account_badge()
        if res.get("gemini_key_imported") and hasattr(self, "_refresh_ai_onboarding_and_badge"):
            self._refresh_ai_onboarding_and_badge()
        if res.get("theme_imported"):
            self._current_theme = db.get_setting("theme", "obsidian")
            self._reading_mode = db.get_setting("reading_mode", "0") == "1"
            QSettings("FlowLocal", "Hub").setValue("theme", self._current_theme)
            QSettings("FlowLocal", "Hub").setValue("reading_mode", self._reading_mode)
            self._apply_theme_change()
        if res.get("notes_imported", 0) > 0:
            self._refresh_notekeeper()
        if res.get("events_imported", 0) > 0 or res.get("exams_imported", 0) > 0:
            self._refresh_current_page()

    def _build_flowtype_subbar(self) -> QFrame:
        subbar = AnimatedSegmentedBox(
            box_object_name="SubNavBar",
            pill_object_name="SubSlidingPill",
            duration_ms=260,
        )
        subbar.setFixedHeight(38)
        self.flowtype_subbar = subbar

        layout = QHBoxLayout(subbar)
        layout.setContentsMargins(24, 4, 24, 4)
        layout.setSpacing(6)

        self.flowtype_sub_group = QButtonGroup(self)
        self.flowtype_sub_group.setExclusive(True)
        for idx, (lbl, page_idx) in enumerate([
            ("Dettatura · Statistiche", 0),
            ("Dizionario · Snippet", 5),
            ("Stili · Impostazioni", 6),
        ]):
            btn = ThemedPushButton(lbl)
            btn.setObjectName("SubNavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, p=page_idx: self._on_nav_clicked(p))
            self.flowtype_sub_group.addButton(btn, idx)
            self.nav_buttons_by_page[page_idx] = btn
            layout.addWidget(btn)
            subbar.register_button(btn)

        layout.addStretch(1)
        return subbar

    def _sync_top_nav_state(self, page_idx: int):
        if hasattr(self, "flowtype_subbar"):
            self.flowtype_subbar.setVisible(page_idx in (0, 5, 6))

        if page_idx in (0, 5, 6) and hasattr(self, "top_flowtype_btn"):
            self.top_flowtype_btn.setChecked(True)
        elif page_idx in (1, 2, 3, 8) and hasattr(self, "top_flownotes_btn"):
            self.top_flownotes_btn.setChecked(True)
        elif page_idx == 7 and hasattr(self, "top_flowtime_btn"):
            self.top_flowtime_btn.setChecked(True)
        elif page_idx == 4 and hasattr(self, "top_whis_btn"):
            self.top_whis_btn.setChecked(True)

        if page_idx in self.nav_buttons_by_page:
            self.nav_buttons_by_page[page_idx].setChecked(True)

        if hasattr(self, "fn_sidebar_view_btns") and page_idx in self.fn_sidebar_view_btns:
            self.fn_sidebar_view_btns[page_idx].setChecked(True)
        if hasattr(self, "fn_fc_view_btns") and page_idx in self.fn_fc_view_btns:
            self.fn_fc_view_btns[page_idx].setChecked(True)
        if hasattr(self, "fn_graph_view_btns") and page_idx in self.fn_graph_view_btns:
            self.fn_graph_view_btns[page_idx].setChecked(True)
        if hasattr(self, "fn_focus_view_btns") and page_idx in self.fn_focus_view_btns:
            self.fn_focus_view_btns[page_idx].setChecked(True)

    def _set_theme(self, theme_key: str):
        if theme_key not in _THEME_PALETTES or theme_key == self._current_theme:
            return
        self._current_theme = theme_key
        QSettings("FlowLocal", "Hub").setValue("theme", theme_key)
        db.set_setting("theme", theme_key)
        self._apply_theme_change()
        cloud_sync.push_account_config_to_drive(async_bg=True)

    def _toggle_reading_mode(self, checked: bool):
        if bool(checked) == self._reading_mode:
            return
        self._reading_mode = bool(checked)
        QSettings("FlowLocal", "Hub").setValue("reading_mode", self._reading_mode)
        db.set_setting("reading_mode", "1" if self._reading_mode else "0")
        self._apply_theme_change()
        cloud_sync.push_account_config_to_drive(async_bg=True)

    def _set_font_preset(self, preset_key: str):
        if preset_key not in FONT_PRESETS or preset_key == self._font_preset:
            return
        self._font_preset = preset_key
        QSettings("FlowLocal", "Hub").setValue("font_preset", preset_key)
        self._apply_typography_change()

    def _set_custom_font_family(self, family: str):
        clean_fam = (family or "").strip()
        if not clean_fam:
            return
        if self._font_preset == "custom" and self._custom_font_family == clean_fam:
            return
        self._font_preset = "custom"
        self._custom_font_family = clean_fam
        settings = QSettings("FlowLocal", "Hub")
        settings.setValue("font_preset", "custom")
        settings.setValue("custom_font_family", clean_fam)
        self._apply_typography_change()

    def _set_font_scale(self, scale_pct: int):
        clamped = max(90, min(120, int(scale_pct or 100)))
        if clamped == self._font_scale:
            return
        self._font_scale = clamped
        QSettings("FlowLocal", "Hub").setValue("font_scale", clamped)
        self._apply_typography_change()

    def _sync_editor_typography(self):
        resolved_font = _resolve_font_family(
            self._current_theme,
            self._font_preset,
            self._custom_font_family,
        )
        for pane_attr in ("nk_pane_left", "nk_pane_right"):
            pane = getattr(self, pane_attr, None)
            if pane is not None and hasattr(pane.editor_body, "set_typography"):
                pane.editor_body.set_typography(resolved_font, self._font_scale)

    def _apply_typography_change(self):
        self.setUpdatesEnabled(False)
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )
        self._sync_editor_typography()
        if hasattr(self, "top_segment_box"):
            QTimer.singleShot(0, self.top_segment_box.sync_pill_geometry)
        if hasattr(self, "flowtype_subbar") and hasattr(self.flowtype_subbar, "sync_pill_geometry"):
            QTimer.singleShot(0, self.flowtype_subbar.sync_pill_geometry)
        self.setUpdatesEnabled(True)

    def _apply_theme_change(self):
        old_pixmap = self.grab() if self.isVisible() else None
        ThemedPushButton.current_theme = self._current_theme
        ThemedPushButton.reading_mode = self._reading_mode
        is_dark = self._active_is_dark()
        if hasattr(self, "nk_col3_layout"):
            if self._reading_mode:
                self.nk_col3_layout.setContentsMargins(64, 18, 64, 16)
            else:
                self.nk_col3_layout.setContentsMargins(28, 12, 28, 12)
        p_sync = self._active_palette()
        for pane_attr in ("nk_pane_left", "nk_pane_right"):
            pane = getattr(self, pane_attr, None)
            if pane is not None:
                pane.set_theme(self._current_theme, is_dark, self._reading_mode, p_sync)
        if hasattr(self, "nk_workspace_switcher"):
            self.nk_workspace_switcher.set_theme(self._current_theme, p_sync)
        if hasattr(self, "nk_split_btn"):
            self.nk_split_btn.setIcon(get_icon("split_h", theme_key=self._current_theme, role="subtitle", size=15))
        if hasattr(self, "nk_send_to_split_btn"):
            self.nk_send_to_split_btn.setIcon(get_icon("arrow_right", theme_key=self._current_theme, role="subtitle", size=14))

        overlay = ThemeRevealOverlay(self, old_pixmap) if old_pixmap is not None else None
        self.setUpdatesEnabled(False)
        self.setStyleSheet(
            get_hub_qss(
                self._current_theme,
                self._reading_mode,
                font_preset=self._font_preset,
                custom_font=self._custom_font_family,
                font_scale=self._font_scale,
            )
        )
        self._sync_editor_typography()
        self._apply_windows_titlebar_theme(self._current_theme, self._reading_mode)
        self._refresh_hub_icons()
        if hasattr(self, "nk_tree") and hasattr(self.nk_tree, "set_theme_palette"):
            self.nk_tree.set_theme_palette(self._active_palette())
        if hasattr(self, "spotify_bar"):
            self.spotify_bar.set_theme_mode(is_dark)
        if hasattr(self, "flowtype_style_title"):
            p = self._active_palette()
            st_col = p["title"] if is_dark or self._reading_mode else p["card_body"]
            self.flowtype_style_title.setStyleSheet(
                f"font-size: 12.5px; font-weight: 600; color: {st_col}; background: transparent;"
            )
        if hasattr(self, "whis_list"):
            self._refresh_whis_page()
        if hasattr(self, "nk_tree"):
            self._refresh_notekeeper()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()
        if hasattr(self, "pomo_ring"):
            p = self._active_palette()
            self.pomo_ring.set_theme_colors(
                p.get("accent") or p.get("nav_checked_border") or "#6366F1",
                p.get("border") or "#334155",
                p.get("title") if self._active_is_dark() or self._reading_mode else p.get("card_body"),
                p.get("muted") or "#94A3B8",
            )
        if hasattr(self, "top_segment_box"):
            QTimer.singleShot(0, self.top_segment_box.sync_pill_geometry)
        if hasattr(self, "flowtype_subbar") and hasattr(self.flowtype_subbar, "sync_pill_geometry"):
            QTimer.singleShot(0, self.flowtype_subbar.sync_pill_geometry)
        self.setUpdatesEnabled(True)
        self.update()
        if overlay is not None:
            overlay.raise_()

    def _refresh_hub_icons(self):
        """Aggiorna le icone vettoriali della UI in base al tema QSS attivo."""
        tkey = getattr(self, "_current_theme", "obsidian")
        pairs = [
            ("top_flowtype_btn", "mic", "text", 14),
            ("top_flownotes_btn", "notes", "text", 14),
            ("top_flowtime_btn", "calendar", "text", 14),
            ("top_whis_btn", "sparkles", "text", 14),
            ("pomo_top_pill", "clock", "accent", 13),
            ("reading_mode_btn", "book", "subtitle", 13),
            ("whats_new_btn", "lightbulb", "subtitle", 15),
            ("settings_btn", "settings", "subtitle", 15),
            ("import_slide_btn", "download", "text", 14),
            ("sbobina_btn", "mic", "text", 14),
            ("nk_more_btn", "more", "subtitle", 15),
            ("nk_history_btn", "clock", "subtitle", 15),
            ("nk_focus_mode_btn", "expand", "subtitle", 15),
            ("nk_studio_btn", "book", "text", 14),
            ("nk_rsvp_btn", "lightning", "text", 14),
            ("nk_ask_whis_btn", "sparkles", "accent", 14),
            ("ft_today_btn", "calendar", "text", 14),
            ("ft_save_btn", "save", "on_accent", 14),
            ("ft_delete_btn", "trash", "danger", 14),
            ("ft_link_note_btn", "link", "text", 14),
            ("whis_send_btn", "sparkles", "on_accent", 14),
        ]
        for attr, icon_name, role, size in pairs:
            w = getattr(self, attr, None)
            if w is not None:
                w.setIcon(get_icon(icon_name, theme_key=tkey, role=role, size=size))
                w.setIconSize(QSize(size, size))
        self._refresh_system_status_pill()
        self._refresh_whats_new_badge()

    def _on_nav_clicked(self, page_idx: int):
        self.pages.setCurrentIndex(page_idx)
        self._sync_top_nav_state(page_idx)
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
        elif idx == 7:
            self._refresh_flowtime_page()
            self._periodic_google_calendar_sync()
        elif idx == 8:
            self._refresh_pomo_subjects()
            self._refresh_weekly_study_summary()
            self._refresh_exam_countdowns()

    @staticmethod
    def _make_page(title: str, subtitle: str) -> (QWidget, QVBoxLayout):
        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
        page, layout = self._make_page("FlowType", "Dettatura vocale in tempo reale e stili contestuali.")

        # --- 4 MINI-CARD STATISTICHE FLOWTYPE (IN ALTO) ---
        stats_box = QFrame()
        stats_box.setObjectName("FlowTypeStatsBox")
        stats_box.setStyleSheet("QFrame#FlowTypeStatsBox { background-color: transparent; border: none; }")
        st_layout = QHBoxLayout(stats_box)
        st_layout.setContentsMargins(0, 0, 0, 4)
        st_layout.setSpacing(10)

        def _make_stat_card(icon_title: str, default_val: str, subtitle: str, color_val: str = "#38BDF8"):
            card = QFrame()
            card.setObjectName("Card")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(14, 12, 14, 12)
            c_layout.setSpacing(3)

            lbl_title = QLabel(icon_title)
            lbl_title.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600; background: transparent;")
            c_layout.addWidget(lbl_title)

            lbl_val = QLabel(default_val)
            lbl_val.setStyleSheet(f"color: {color_val}; font-size: 20px; font-weight: 700; background: transparent;")
            c_layout.addWidget(lbl_val)

            lbl_sub = QLabel(subtitle)
            lbl_sub.setStyleSheet("color: #64748B; font-size: 10px; background: transparent;")
            c_layout.addWidget(lbl_sub)
            return card, lbl_val

        card_words, self.stat_words_label = _make_stat_card("Parole", "0", "dettate con FlowLocal", "#38BDF8")
        card_wpm, self.stat_wpm_label = _make_stat_card("Velocità", "150 WPM", "parole al minuto", "#10B981")
        card_time, self.stat_time_label = _make_stat_card("Risparmio", "0 min", "rispetto a digitazione", "#A855F7")
        card_streak, self.stat_streak_label = _make_stat_card("Streak", "0 Giorni", "continuità", "#7DD3FC")

        st_layout.addWidget(card_words)
        st_layout.addWidget(card_wpm)
        st_layout.addWidget(card_time)
        st_layout.addWidget(card_streak)
        layout.addWidget(stats_box)

        # --- BANNER DISCRETO ONBOARDING AI (solo se Ollama assente e nessuna chiave Gemini) ---
        self.ai_onboarding_card = QFrame()
        self.ai_onboarding_card.setObjectName("Card")
        self.ai_onboarding_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        onb_layout = QVBoxLayout(self.ai_onboarding_card)
        onb_layout.setContentsMargins(14, 12, 14, 12)
        onb_layout.setSpacing(8)

        onb_title = _make_label("Attiva l'AI avanzata senza appesantire il PC", "CardTitle", wrap=True)
        onb_text = _make_label(
            "Collega una chiave gratuita Google AI Studio in 30 secondi per sbloccare LaTeX, Whis e OCR da screenshot.",
            "CardBody",
            wrap=True,
        )
        onb_layout.addWidget(onb_title)
        onb_layout.addWidget(onb_text)

        onb_actions = QHBoxLayout()
        onb_actions.setSpacing(8)
        onb_get_btn = _make_button("Ottieni chiave gratuita", "primary")
        onb_get_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GEMINI_AISTUDIO_URL)))
        onb_actions.addWidget(onb_get_btn)

        onb_guide_btn = _make_button("Guida (4 step)", "ghost")
        onb_guide_btn.clicked.connect(self._on_open_gemini_guide_from_hub)
        onb_actions.addWidget(onb_guide_btn)

        onb_insert_btn = _make_button("Inserisci chiave", "ghost")
        onb_insert_btn.clicked.connect(lambda: self._on_open_settings_clicked(initial_tab=1))
        onb_actions.addWidget(onb_insert_btn)

        onb_actions.addStretch(1)
        onb_dismiss_btn = _make_button("Continua solo con trascrizione vocale", "ghost")
        onb_dismiss_btn.clicked.connect(self._on_dismiss_ai_onboarding)
        onb_actions.addWidget(onb_dismiss_btn)
        onb_layout.addLayout(onb_actions)

        self.ai_onboarding_card.setVisible(False)
        layout.addWidget(self.ai_onboarding_card)

        # --- SELETTORE RAPIDO STILE DI DETTATURA ---
        style_box = QFrame()
        style_box.setObjectName("Card")
        s_layout = QVBoxLayout(style_box)
        s_layout.setContentsMargins(14, 12, 14, 12)
        s_layout.setSpacing(10)

        s_header = QHBoxLayout()
        s_title = QLabel("Stile di Dettatura")
        p_active = self._active_palette()
        st_col = p_active["title"] if self._active_is_dark() or getattr(self, "_reading_mode", False) else p_active["card_body"]
        s_title.setStyleSheet(f"font-size: 12.5px; font-weight: 600; color: {st_col}; background: transparent;")
        self.flowtype_style_title = s_title
        s_header.addWidget(s_title)

        self.flowtype_active_style_label = QLabel("Naturale")
        self.flowtype_active_style_label.setObjectName("Badge")
        s_header.addWidget(self.flowtype_active_style_label)
        s_header.addStretch(1)

        self.ai_engine_pill = ThemedPushButton(get_active_runtime_engine())
        self.ai_engine_pill.setObjectName("FilterPill")
        self.ai_engine_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ai_engine_pill.setToolTip("Motore AI attivo — Clicca per aprire Motore AI & Hardware")
        self.ai_engine_pill.setStyleSheet("padding: 2px 10px; font-size: 11px; min-height: 24px;")
        self.ai_engine_pill.clicked.connect(lambda: self._on_open_settings_clicked(initial_tab=1))
        s_header.addWidget(self.ai_engine_pill)

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
            btn.setObjectName("FilterPill")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(s_desc)
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
        hotkey_box.setObjectName("Card")
        h_layout = QHBoxLayout(hotkey_box)
        h_layout.setContentsMargins(14, 10, 14, 10)
        h_layout.setSpacing(12)

        key_badge = QLabel("Shift Destro")
        key_badge.setObjectName("ShortcutKey")
        h_layout.addWidget(key_badge)

        h_info = QLabel("<b>Push-to-Talk principale:</b> tieni premuto Shift Destro (sotto Invio) per dettare live, rilascia per incollare con zero focus steal.<br>"
                        "<b>Push-to-Lock (Hands-Free):</b> premi <b>Ctrl Destro</b> (o <b>Alt Gr</b>) mentre tieni Shift Destro, oppure doppio tocco. <b>Shift Sinistro:</b> resta 100% libero.")
        h_info.setWordWrap(True)
        h_info.setStyleSheet("color: #94A3B8; font-size: 12px; background: transparent;")
        h_layout.addWidget(h_info, 1)

        layout.addWidget(hotkey_box)

        # --- TOOLBAR RICERCA E SVUOTA CRONOLOGIA ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Cerca nelle trascrizioni...")
        self.history_search.setMinimumHeight(36)
        self.history_search.textChanged.connect(self._refresh_history)
        toolbar.addWidget(self.history_search, 1)

        clear_btn = _make_button("Svuota cronologia", "danger")
        clear_btn.setIcon(get_icon("trash", theme_key=self._current_theme, role="danger", size=14))
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
        items = db.get_recent_transcriptions(limit=200 if query else 20)
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
        card.setMinimumHeight(96)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta_row.addWidget(_make_label(_format_timestamp(t.get("created_at")), "CardMeta"))
        if t.get("style_used"):
            meta_row.addWidget(_make_label(t["style_used"], "Badge"))
        if t.get("target_app"):
            meta_row.addWidget(_make_label(t["target_app"], "BadgeMuted"))
        meta_row.addStretch(1)

        text = t.get("final_text") or ""
        body_lbl = _make_label(text, "CardBody", wrap=True)
        body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        fix_btn = _make_button("✏️ Correggi")
        fix_btn.setToolTip("Micro-correzione 1-clic nel Dizionario (puoi anche evidenziare una parola prima di cliccare)")
        fix_btn.clicked.connect(
            lambda _c=False, s=text, lbl=body_lbl: self._open_quick_rule_dialog(s, lbl.selectedText())
        )
        meta_row.addWidget(fix_btn)

        copy_btn = _make_button("📋 Copia")
        copy_btn.clicked.connect(lambda _c=False, s=text: self._copy_to_clipboard(s))
        meta_row.addWidget(copy_btn)
        layout.addLayout(meta_row)

        layout.addWidget(body_lbl)
        return card

    def _open_quick_rule_dialog(self, text: str, selected_fragment: str = ""):
        dlg = QuickRuleDialog(text, parent=self, selected_fragment=selected_fragment)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_dictionary()

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
                try:
                    saved_mode = db.get_setting("whis_model_mode", None)
                    if saved_mode:
                        self.whis_agent.set_model_mode(saved_mode)
                except Exception:
                    pass
                idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
                if idx >= 0:
                    self.whis_model_combo.setCurrentIndex(idx)

    def _build_whis_page(self) -> QWidget:
        page, layout = self._make_page("Whis", "Assistente locale e cronologia interazioni.")

        # 1. Header & Live Status Panel (Obsidian Glass)
        status_frame = QFrame()
        status_frame.setObjectName("Card")
        sf_layout = QVBoxLayout(status_frame)
        sf_layout.setContentsMargins(14, 12, 14, 12)
        sf_layout.setSpacing(10)

        # Riga 1: Status live + Switch Mani Libere + Mostra HUD
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self.whis_status_label = QLabel("● Pronto — Mani Libere Attive (\"Whis...\")")
        self.whis_status_label.setStyleSheet("color: #34D399; font-size: 12px; font-weight: 600; background: transparent;")
        row1.addWidget(self.whis_status_label, 1)

        # Switch Mani Libere (Attivo ON di default)
        self.handsfree_switch = QPushButton("Mani Libere: ON")
        self.handsfree_switch.setObjectName("FilterPill")
        self.handsfree_switch.setCheckable(True)
        self.handsfree_switch.setChecked(self.is_handsfree_enabled)
        self.handsfree_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handsfree_switch.toggled.connect(self._on_handsfree_toggled)
        row1.addWidget(self.handsfree_switch)

        # Pulsante Mostra ultimo HUD
        show_hud_btn = _make_button("Mostra HUD")
        show_hud_btn.clicked.connect(self._show_last_whis_hud)
        row1.addWidget(show_hud_btn)

        sf_layout.addLayout(row1)

        # Riga 2: Selettori compatti (Modello, Audio, Voce, Prova) + Google Calendar
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        lbl_mod = QLabel("Modello:")
        lbl_mod.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_mod)

        self.whis_model_combo = QComboBox()
        self.whis_model_combo.setMinimumHeight(30)
        self.whis_model_combo.addItem("Auto (4B/9B)", "auto")
        self.whis_model_combo.addItem("qwen3.5:4b (Consigliato - Veloce + Vision)", "qwen3.5:4b")
        self.whis_model_combo.addItem("qwen3.5:9b (Alta Precisione + Vision)", "qwen3.5:9b")
        self.whis_model_combo.addItem("qwen3.5:2b (Leggero + Vision)", "qwen3.5:2b")
        if hasattr(self, "whis_agent") and self.whis_agent:
            cur_idx = self.whis_model_combo.findData(self.whis_agent.get_model_mode())
            if cur_idx >= 0:
                self.whis_model_combo.setCurrentIndex(cur_idx)
        self.whis_model_combo.currentIndexChanged.connect(self._on_whis_model_changed)
        row2.addWidget(self.whis_model_combo)

        lbl_audio = QLabel("Audio:")
        lbl_audio.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_audio)

        self.whis_audio_combo = QComboBox()
        self.whis_audio_combo.setMinimumHeight(30)
        self.whis_audio_combo.addItem("Muto (Default)", MODE_MUTE)
        self.whis_audio_combo.addItem("Suono Morbido", MODE_SOFT_CHIME)
        self.whis_audio_combo.addItem("Voce (TTS)", MODE_VOICE)

        cur_audio_mode = audio_feedback.get_mode()
        cur_audio_idx = self.whis_audio_combo.findData(cur_audio_mode)
        if cur_audio_idx >= 0:
            self.whis_audio_combo.setCurrentIndex(cur_audio_idx)

        self.whis_audio_combo.currentIndexChanged.connect(self._on_whis_audio_mode_changed)
        audio_feedback.add_mode_listener(self._sync_whis_audio_combo)
        row2.addWidget(self.whis_audio_combo)

        lbl_voice = QLabel("Voce:")
        lbl_voice.setStyleSheet("color: #64748B; font-size: 11px; font-weight: 600; background: transparent;")
        row2.addWidget(lbl_voice)

        self.whis_voice_combo = QComboBox()
        self.whis_voice_combo.setMinimumHeight(30)
        for k, v in VOICE_PRESETS.items():
            self.whis_voice_combo.addItem(v["label"], k)

        cur_voice = audio_feedback.get_voice_profile()
        cur_v_idx = self.whis_voice_combo.findData(cur_voice)
        if cur_v_idx >= 0:
            self.whis_voice_combo.setCurrentIndex(cur_v_idx)

        self.whis_voice_combo.currentIndexChanged.connect(self._on_whis_voice_changed)
        audio_feedback.add_voice_listener(self._sync_whis_voice_combo)
        row2.addWidget(self.whis_voice_combo)

        self.whis_voice_test_btn = _make_button("Prova")
        self.whis_voice_test_btn.setToolTip("Ascolta un'anteprima vocale con il profilo selezionato")
        self.whis_voice_test_btn.clicked.connect(self._on_whis_voice_test_clicked)
        row2.addWidget(self.whis_voice_test_btn)

        row2.addStretch(1)

        self.whis_cal_btn = _make_button("Google Calendar")
        self.whis_cal_btn.clicked.connect(self._on_connect_calendar_clicked)
        row2.addWidget(self.whis_cal_btn)

        self.whis_cal_status = QLabel()
        self.whis_cal_status.setStyleSheet("font-size: 11px; font-weight: 500; background: transparent;")
        row2.addWidget(self.whis_cal_status)
        self._update_calendar_status_ui()
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

        self.whis_input = DictationGhostLineEdit(dark_mode=self._active_is_dark())
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
        try:
            if mode:
                db.set_setting("whis_model_mode", str(mode))
        except Exception:
            pass

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
        """Aggiorna lo stato visivo di FlowTime / Google Calendar nell'Hub (zero chiamate di rete)."""
        if not hasattr(self, "whis_cal_status") or not self.whis_cal_status:
            return
        from core.calendar_sync import calendar_manager
        if calendar_manager.has_token_file():
            self.whis_cal_status.setText("🟢 FlowTime + Google Calendar sincronizzati")
            self.whis_cal_status.setStyleSheet("color: #10B981; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Google Calendar: Connesso")
        elif calendar_manager.has_credentials_file():
            self.whis_cal_status.setText("🟡 FlowTime locale attivo · credentials.json pronto")
            self.whis_cal_status.setStyleSheet("color: #F59E0B; font-size: 11px; font-weight: 500;")
            if hasattr(self, "whis_cal_btn") and self.whis_cal_btn:
                self.whis_cal_btn.setText("📅 Connetti Google Calendar")
        else:
            self.whis_cal_status.setText("🟢 FlowTime Locale Attivo (Google Sync opzionale)")
            self.whis_cal_status.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: 500;")
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
            self._calendarAuthDoneSignal.emit(bool(ok), str(msg or ""))

        threading.Thread(target=_auth_thread, daemon=True, name="GoogleCalendarAuthThread").start()

    def _on_calendar_auth_finished(self, ok: bool, msg: str):
        self._update_calendar_status_ui()
        self._refresh_system_status_pill()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()
        if hasattr(self, "pomo_ring"):
            p = self._active_palette()
            self.pomo_ring.set_theme_colors(
                p.get("accent") or p.get("nav_checked_border") or "#6366F1",
                p.get("border") or "#334155",
                p.get("title") if self._active_is_dark() or self._reading_mode else p.get("card_body"),
                p.get("muted") or "#94A3B8",
            )
        if ok:
            self.show_toast(
                "Google Calendar connesso e sincronizzato con FlowTime.",
                title="Google Calendar",
                level="success",
            )
        else:
            self.show_toast(
                f"Impossibile completare l'autenticazione: {msg}",
                title="Google Calendar",
                level="warning",
                duration_ms=8000,
            )

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
        self.is_handsfree_enabled = bool(checked)
        db.set_setting("whis_handsfree_enabled", "1" if checked else "0")
        if self.wake_listener:
            if checked:
                if not self.wake_listener.isRunning():
                    self.wake_listener.set_paused(False)
                    self.wake_listener.start()
                else:
                    self.wake_listener.set_paused(False)
            else:
                self.wake_listener.set_paused(True)

        eng = getattr(self, "engine", None)
        if eng is not None and hasattr(eng, "set_always_on_mic"):
            always_prebuf = (db.get_setting("always_on_prebuffer", "0") or "0").strip().lower() in ("1", "true", "on", "yes")
            eng.set_always_on_mic(bool(checked or always_prebuf))

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
            self.handsfree_switch.setText("Mani Libere: ON")
            self.handsfree_switch.setStyleSheet("")
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("● Pronto — Mani Libere Attive (\"Whis...\")")
                self.whis_status_label.setStyleSheet("color: #34D399; font-size: 12px; font-weight: 600; background: transparent;")
        else:
            self.handsfree_switch.setText("Mani Libere: OFF")
            self.handsfree_switch.setStyleSheet("")
            if hasattr(self, "whis_status_label"):
                self.whis_status_label.setText("● Pronto (Shift Destro)")
                self.whis_status_label.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 600; background: transparent;")

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

        p_act = self._active_palette()
        q_bg = p_act["badge_bg"]
        q_color = p_act["badge_text"]
        resp_color = p_act["card_body"]
        q_radius = p_act["input_radius"]

        # Row 2: Domanda Utente
        q_box = QFrame()
        q_box.setStyleSheet(f"background-color: {q_bg}; border: none; border-radius: {q_radius}; padding: 6px 10px;")
        q_layout = QVBoxLayout(q_box)
        q_layout.setContentsMargins(6, 4, 6, 4)
        q_lbl = _make_label(f"💬 {user_q}", "CardTitle", wrap=True)
        q_lbl.setStyleSheet(f"color: {q_color}; font-weight: 600; font-size: 13px; background: transparent;")
        q_layout.addWidget(q_lbl)
        layout.addWidget(q_box)

        # Row 3: Risposta Whis
        resp_lbl = _make_label(whis_resp, "CardBody", wrap=True)
        resp_lbl.setStyleSheet(f"color: {resp_color}; font-size: 13px; padding-top: 4px; background: transparent;")
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
    def _build_flownotes_view_bar(self, store_attr: str) -> QWidget:
        """Barra compatta [ Quaderno ] [ Flashcard ] [ Grafo ] [ Focus ] per le viste di FlowNotes."""
        bar = AnimatedSegmentedBox(
            box_object_name="FlowNotesViewBar",
            pill_object_name="SubSlidingPill",
            duration_ms=260,
        )
        bar.setStyleSheet("QFrame#FlowNotesViewBar { background: transparent; border: none; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        btn_map: Dict[int, QPushButton] = {}
        grp = QButtonGroup(bar)
        grp.setExclusive(True)
        for idx, (lbl, pid) in enumerate([
            ("Quaderno", 1),
            ("Flashcard", 2),
            ("Grafo", 3),
            ("Focus", 8),
        ]):
            b = ThemedPushButton(lbl)
            b.setObjectName("SubNavBtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("padding: 5px 6px; font-size: 11.5px;")
            b.clicked.connect(lambda _c=False, p=pid: self._on_nav_clicked(p))
            grp.addButton(b, idx)
            btn_map[pid] = b
            layout.addWidget(b)
            bar.register_button(b)
        setattr(self, store_attr, btn_map)
        return bar

    def _build_flowstudy_notekeeper_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        main_vbox = QVBoxLayout(page)
        main_vbox.setContentsMargins(10, 8, 10, 10)
        main_vbox.setSpacing(6)

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

        self.sbobina_cancel_btn = _make_button("Annulla", "danger")
        self.sbobina_cancel_btn.setIcon(get_icon("close", theme_key=self._current_theme, role="danger", size=13))
        self.sbobina_cancel_btn.setMinimumHeight(28)
        self.sbobina_cancel_btn.clicked.connect(self._on_cancel_sbobina_clicked)
        p_layout.addWidget(self.sbobina_cancel_btn)

        self.sbobina_progress_box.hide()
        main_vbox.addWidget(self.sbobina_progress_box)

        # ---------------- LAYOUT FLOWNOTES A 2 COLONNE STILE NOTION ----------------
        nk_container = QFrame()
        nk_container.setObjectName("NotekeeperContainer")
        nk_container_layout = QVBoxLayout(nk_container)
        nk_container_layout.setContentsMargins(0, 0, 0, 0)
        nk_container_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        self.nk_splitter = splitter

        # ===== COLONNA 1: SIDEBAR UNICA FLOWNOTES (280px, COLLASSABILE) =====
        col1 = QWidget()
        col1.setObjectName("NKCol1")
        col1.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col1.setMinimumWidth(280)
        col1.setMaximumWidth(380)
        self.nk_col1 = col1
        col1_layout = QVBoxLayout(col1)
        col1_layout.setContentsMargins(10, 10, 10, 10)
        col1_layout.setSpacing(7)

        # Switch Viste Studio in cima alla Sidebar + pulsante collasso
        col1_top_row = QHBoxLayout()
        col1_top_row.setContentsMargins(0, 0, 0, 0)
        col1_top_row.setSpacing(4)
        col1_top_row.addWidget(self._build_flownotes_view_bar("fn_sidebar_view_btns"), 1)

        self.nk_hide_col1_btn = _make_button("«")
        self.nk_hide_col1_btn.setObjectName("NKCollapseBtn")
        self.nk_hide_col1_btn.setToolTip("Nascondi Sidebar FlowNotes")
        self.nk_hide_col1_btn.clicked.connect(lambda: self._hide_nk_col(0))
        col1_top_row.addWidget(self.nk_hide_col1_btn)
        col1_layout.addLayout(col1_top_row)

        # Workspace Switcher stile Notion (Quaderno Locale vs FlowGroups)
        self._nk_active_workspace_group_id: Optional[int] = None
        self.nk_workspace_switcher = WorkspaceSwitcherButton(theme_key=self._current_theme, parent=col1)
        self.nk_workspace_switcher.set_theme(self._current_theme, self._active_palette())
        self.nk_workspace_switcher.workspaceSelected.connect(self._on_nk_workspace_selected)
        self.nk_workspace_switcher.joinOrCreateRequested.connect(self._on_nk_join_or_create_group)
        self.nk_workspace_switcher.removeGroupRequested.connect(self._on_nk_remove_group)
        col1_layout.addWidget(self.nk_workspace_switcher)

        # Barra di ricerca + Profilo compatto
        self.nk_search_input = QLineEdit()
        self.nk_search_input.setPlaceholderText("Cerca note, materie...")
        self.nk_search_input.setMinimumHeight(32)
        self.nk_search_input.textChanged.connect(self._on_nk_search_changed)
        col1_layout.addWidget(self.nk_search_input)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.nk_new_note_btn = _make_button("+ Nuova Nota", "primary")
        self.nk_new_note_btn.setFixedHeight(32)
        self.nk_new_note_btn.clicked.connect(self._on_nk_new_note_clicked)
        actions_row.addWidget(self.nk_new_note_btn, 1)
        col1_layout.addLayout(actions_row)

        # Filtro Profilo compatto + Toggle Vista Card
        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.nk_profile_combo = QComboBox()
        self.nk_profile_combo.addItems(["Tutti i profili", "Alessandro", "Giada"])
        self.nk_profile_combo.setMinimumHeight(28)
        self.nk_profile_combo.currentIndexChanged.connect(self._on_nk_profile_changed)
        profile_row.addWidget(self.nk_profile_combo, 1)

        self.nk_card_view_btn = _make_button("☷")
        self.nk_card_view_btn.setObjectName("NKCollapseBtn")
        self.nk_card_view_btn.setCheckable(True)
        self.nk_card_view_btn.setToolTip("Mostra/Nascondi colonna Vista Card")
        self.nk_card_view_btn.setFixedSize(32, 28)
        self.nk_card_view_btn.clicked.connect(self._toggle_nk_card_view)
        profile_row.addWidget(self.nk_card_view_btn)
        col1_layout.addLayout(profile_row)

        # Albero Cartelle & Sottocartelle Stile Samsung Notes (FlowNotes)
        self.nk_tree = SamsungNotesTreeWidget()
        self.nk_tree.setObjectName("NKTree")
        self.nk_tree.set_theme_palette(self._active_palette())
        self.nk_tree.currentItemChanged.connect(self._on_nk_tree_item_changed)
        self.nk_tree.noteMovedToFolder.connect(self._on_nk_note_moved)
        self.nk_tree.folderMovedToParent.connect(self._on_nk_folder_moved)
        self.nk_tree.createSubfolderRequested.connect(self._on_nk_create_subfolder)
        self.nk_tree.renameFolderRequested.connect(self._on_nk_rename_folder)
        self.nk_tree.changeFolderColorRequested.connect(self._on_nk_change_folder_color)
        self.nk_tree.deleteFolderRequested.connect(self._on_nk_delete_folder)
        self.nk_tree.toggleNoteStudiedRequested.connect(self._on_nk_toggle_studied)
        self.nk_tree.toggleNoteFavoriteRequested.connect(self._on_nk_toggle_favorite)
        self.nk_tree.trashNoteRequested.connect(self._on_nk_trash_note)
        self.nk_tree.restoreNoteRequested.connect(self._on_nk_restore_note)
        self.nk_tree.permanentDeleteNoteRequested.connect(self._on_nk_permanent_delete_note)
        self.nk_tree.emptyTrashRequested.connect(self._on_nk_empty_trash)
        self.nk_tree.nodeLeftClicked.connect(lambda data: self._open_nk_tree_node(data, in_new_tab=False))
        self.nk_tree.nodeMiddleClicked.connect(lambda data: self._open_nk_tree_node(data, in_new_tab=True))
        self.nk_tree.nodeDoubleClicked.connect(lambda data: self._open_nk_tree_node(data, in_new_tab=True))
        self.nk_tree.openInNewTabRequested.connect(lambda data: self._open_nk_tree_node(data, in_new_tab=True))
        self.nk_tree.openInSplitRequested.connect(lambda data: self._open_nk_tree_node(data, in_split=True))
        col1_layout.addWidget(self.nk_tree, 1)

        # Footer compatto della Sidebar
        col1_btn_layout = QVBoxLayout()
        col1_btn_layout.setSpacing(5)
        self.nk_new_sub_btn = _make_button("+ Cartella")
        self.nk_new_sub_btn.setObjectName("NKCol1Btn")
        self.nk_new_sub_btn.setFixedHeight(30)
        self.nk_new_sub_btn.setToolTip("Crea una nuova cartella in FlowNotes")
        self.nk_new_sub_btn.clicked.connect(self._on_nk_new_subject_clicked)
        col1_btn_layout.addWidget(self.nk_new_sub_btn)

        footer_row2 = QHBoxLayout()
        footer_row2.setSpacing(5)
        self.import_slide_btn = _make_button(" Importa", "ghost")
        self.import_slide_btn.setObjectName("NKCol1Btn")
        self.import_slide_btn.setIcon(get_icon("download", theme_key=self._current_theme, role="text", size=14))
        self.import_slide_btn.setToolTip("Importa PDF, Slide o Immagini")
        self.import_slide_btn.setFixedHeight(30)
        self.import_slide_btn.clicked.connect(self._on_import_slide_clicked)
        footer_row2.addWidget(self.import_slide_btn, 1)

        self.sbobina_btn = _make_button(" Sbobina", "ghost")
        self.sbobina_btn.setObjectName("NKCol1Btn")
        self.sbobina_btn.setIcon(get_icon("mic", theme_key=self._current_theme, role="text", size=14))
        self.sbobina_btn.setToolTip("Sbobina Audio o Lezione")
        self.sbobina_btn.setFixedHeight(30)
        self.sbobina_btn.clicked.connect(self._on_sbobina_clicked)
        footer_row2.addWidget(self.sbobina_btn, 1)
        col1_btn_layout.addLayout(footer_row2)
        col1_layout.addLayout(col1_btn_layout)

        splitter.addWidget(col1)

        # ===== COLONNA 2 OPZIONALE: VISTA CARD (NASCOSTA DI DEFAULT PER DARE 80% ALL'EDITOR) =====
        col2 = QWidget()
        col2.setObjectName("NKCol2")
        col2.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col2.setMinimumWidth(240)
        self.nk_col2 = col2
        col2_layout = QVBoxLayout(col2)
        col2_layout.setContentsMargins(8, 10, 8, 10)
        col2_layout.setSpacing(6)

        self.nk_show_col1_in_col2_btn = _make_button("📁 Sidebar ▸")
        self.nk_show_col1_in_col2_btn.setObjectName("NKRestorePill")
        self.nk_show_col1_in_col2_btn.setVisible(False)
        self.nk_show_col1_in_col2_btn.clicked.connect(lambda: self._show_nk_col(0))
        col2_layout.addWidget(self.nk_show_col1_in_col2_btn, 0, Qt.AlignmentFlag.AlignLeft)

        col2_hdr = QHBoxLayout()
        col2_hdr.addWidget(_make_label("VISTA CARD", "NavSectionHeader"))
        col2_hdr.addStretch(1)
        self.nk_hide_col2_btn = _make_button("«")
        self.nk_hide_col2_btn.setObjectName("NKCollapseBtn")
        self.nk_hide_col2_btn.setToolTip("Chiudi Vista Card")
        self.nk_hide_col2_btn.clicked.connect(lambda: self._toggle_nk_card_view(False))
        col2_hdr.addWidget(self.nk_hide_col2_btn)
        col2_layout.addLayout(col2_hdr)

        self.nk_notes_list = ScrollList("Nessuna nota per l'argomento selezionato.")
        self.nk_notes_list.vbox.setContentsMargins(2, 6, 6, 6)
        self.nk_notes_list.vbox.setSpacing(6)
        col2_layout.addWidget(self.nk_notes_list, 1)

        col2.setVisible(False)
        splitter.addWidget(col2)

        # ===== AREA PRINCIPALE DESTRA: FOGLIO EDITOR NOTION (80% DELLO SCHERMO) =====
        col3 = QWidget()
        col3.setObjectName("NKCol3")
        col3.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col3.setMinimumWidth(420)
        self.nk_col3 = col3
        col3_layout = QVBoxLayout(col3)
        self.nk_col3_layout = col3_layout
        if getattr(self, "_reading_mode", False):
            col3_layout.setContentsMargins(64, 18, 64, 16)
        else:
            col3_layout.setContentsMargins(28, 12, 28, 12)
        col3_layout.setSpacing(6)

        # Pillole di ripristino sidebar (visibili solo quando la sidebar è nascosta)
        restore_bar = QHBoxLayout()
        restore_bar.setContentsMargins(0, 0, 0, 0)
        restore_bar.setSpacing(6)

        self.nk_show_col1_btn = _make_button(" Sidebar ▸")
        self.nk_show_col1_btn.setObjectName("NKRestorePill")
        self.nk_show_col1_btn.setIcon(get_icon("folder", theme_key=self._current_theme, role="subtitle", size=13))
        self.nk_show_col1_btn.setVisible(False)
        self.nk_show_col1_btn.clicked.connect(lambda: self._show_nk_col(0))
        restore_bar.addWidget(self.nk_show_col1_btn)

        self.nk_show_col2_btn = _make_button(" Card ▸")
        self.nk_show_col2_btn.setObjectName("NKRestorePill")
        self.nk_show_col2_btn.setVisible(False)
        self.nk_show_col2_btn.clicked.connect(lambda: self._toggle_nk_card_view(True))
        restore_bar.addWidget(self.nk_show_col2_btn)
        restore_bar.addStretch(1)
        col3_layout.addLayout(restore_bar)

        # Strip unificata di azioni rapide (dockata nella riga Breadcrumb sotto la Tab Bar)
        self.nk_actions_strip = QWidget(col3)
        strip_lay = QHBoxLayout(self.nk_actions_strip)
        strip_lay.setContentsMargins(0, 0, 0, 0)
        strip_lay.setSpacing(4)

        self.nk_studio_btn = _make_button(" Vista Studio")
        self.nk_studio_btn.setObjectName("NKToolbarPill")
        self.nk_studio_btn.setFixedHeight(28)
        self.nk_studio_btn.setIcon(get_icon("book", theme_key=self._current_theme, role="text", size=13))
        self.nk_studio_btn.setCheckable(True)
        self.nk_studio_btn.setToolTip("Alterna Modifica e Vista Studio (Ctrl+E)")
        self.nk_studio_btn.clicked.connect(self._on_nk_studio_btn_clicked)
        strip_lay.addWidget(self.nk_studio_btn)

        self.nk_studio_shortcut = QShortcut(QKeySequence("Ctrl+E"), col3)
        self.nk_studio_shortcut.activated.connect(self._on_nk_studio_shortcut)

        self.nk_rsvp_btn = _make_button(" RSVP")
        self.nk_rsvp_btn.setObjectName("NKToolbarPill")
        self.nk_rsvp_btn.setFixedHeight(28)
        self.nk_rsvp_btn.setIcon(get_icon("lightning", theme_key=self._current_theme, role="text", size=13))
        self.nk_rsvp_btn.setToolTip("Lettura veloce RSVP (Alt+R)")
        self.nk_rsvp_btn.clicked.connect(self._on_nk_rsvp_btn_clicked)
        strip_lay.addWidget(self.nk_rsvp_btn)

        self.nk_favorite_btn = _make_button("", "ghost")
        self.nk_favorite_btn.setObjectName("NKToolbarIconBtn")
        self.nk_favorite_btn.setFixedSize(28, 28)
        self.nk_favorite_btn.setCheckable(True)
        self.nk_favorite_btn.setIcon(get_icon("star", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_favorite_btn.setToolTip("Aggiungi o rimuovi questa nota dai Preferiti")
        self.nk_favorite_btn.clicked.connect(self._on_nk_toggle_favorite_active)
        strip_lay.addWidget(self.nk_favorite_btn)

        self.nk_ask_whis_btn = _make_button("", "ghost")
        self.nk_ask_whis_btn.setObjectName("NKToolbarIconBtn")
        self.nk_ask_whis_btn.setFixedSize(28, 28)
        self.nk_ask_whis_btn.setIcon(get_icon("sparkles", theme_key=self._current_theme, role="accent", size=14))
        self.nk_ask_whis_btn.setToolTip("Analizza questa nota con Whis")
        self.nk_ask_whis_btn.clicked.connect(self._on_nk_ask_whis_clicked)
        strip_lay.addWidget(self.nk_ask_whis_btn)

        toolbar_divider = QFrame()
        toolbar_divider.setFixedSize(1, 16)
        toolbar_divider.setStyleSheet("background-color: rgba(148, 163, 184, 0.28); border: none;")
        strip_lay.addWidget(toolbar_divider, 0, Qt.AlignmentFlag.AlignVCenter)

        self.nk_more_btn = _make_button("", "ghost")
        self.nk_more_btn.setObjectName("NKToolbarIconBtn")
        self.nk_more_btn.setFixedSize(28, 28)
        self.nk_more_btn.setIcon(get_icon("more", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_more_btn.setToolTip("Altre azioni (Marcatore audio, Esporta, Duplica, Elimina)")
        self.nk_more_btn.clicked.connect(self._show_nk_more_menu)
        strip_lay.addWidget(self.nk_more_btn)

        self.nk_history_btn = _make_button("", "ghost")
        self.nk_history_btn.setObjectName("NKToolbarIconBtn")
        self.nk_history_btn.setFixedSize(28, 28)
        self.nk_history_btn.setIcon(get_icon("clock", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_history_btn.setToolTip("Cronologia versioni (Time Machine)")
        self.nk_history_btn.clicked.connect(self._on_nk_history_clicked)
        strip_lay.addWidget(self.nk_history_btn)

        self.nk_split_btn = _make_button("", "ghost")
        self.nk_split_btn.setObjectName("NKToolbarIconBtn")
        self.nk_split_btn.setFixedSize(28, 28)
        self.nk_split_btn.setCheckable(True)
        self.nk_split_btn.setIcon(get_icon("split_h", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_split_btn.setToolTip("Affianca a destra (Split View — Ctrl+\\)")
        self.nk_split_btn.clicked.connect(lambda: self._on_nk_toggle_split_view())
        strip_lay.addWidget(self.nk_split_btn)

        self.nk_send_to_split_btn = _make_button("", "ghost")
        self.nk_send_to_split_btn.setObjectName("NKToolbarIconBtn")
        self.nk_send_to_split_btn.setFixedSize(28, 28)
        self.nk_send_to_split_btn.setIcon(get_icon("arrow_right", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_send_to_split_btn.setToolTip("Invia selezione al pannello affiancato")
        self.nk_send_to_split_btn.clicked.connect(self._on_nk_send_selection_to_other_pane)
        self.nk_send_to_split_btn.setVisible(False)
        strip_lay.addWidget(self.nk_send_to_split_btn)

        self.nk_focus_mode_btn = _make_button("", "ghost")
        self.nk_focus_mode_btn.setObjectName("NKToolbarIconBtn")
        self.nk_focus_mode_btn.setFixedSize(28, 28)
        self.nk_focus_mode_btn.setIcon(get_icon("expand", theme_key=self._current_theme, role="subtitle", size=14))
        self.nk_focus_mode_btn.setToolTip("Schermo intero / Focus")
        self.nk_focus_mode_btn.clicked.connect(self._toggle_nk_focus_mode)
        strip_lay.addWidget(self.nk_focus_mode_btn)

        # Dual-Pane Split-View (EditorPane Left & Right in QSplitter Orizzontale)
        is_dark = self._active_is_dark()
        is_reading = getattr(self, "_reading_mode", False)
        p_init = self._active_palette()

        self.nk_panes_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.nk_panes_splitter.setChildrenCollapsible(False)
        self.nk_panes_splitter.setHandleWidth(6)

        self.nk_pane_left = EditorPane(
            pane_id="left",
            theme_key=self._current_theme,
            is_dark=is_dark,
            reading_mode=is_reading,
            palette=p_init,
            autosave_thread_cls=_AutoSaveNoteThread,
            audio_sync_loader=load_note_audio_sync,
            parent=col3,
        )
        self.nk_pane_right = EditorPane(
            pane_id="right",
            theme_key=self._current_theme,
            is_dark=is_dark,
            reading_mode=is_reading,
            palette=p_init,
            autosave_thread_cls=_AutoSaveNoteThread,
            audio_sync_loader=load_note_audio_sync,
            parent=col3,
        )
        self.nk_pane_right.hide()

        self._nk_active_pane = self.nk_pane_left
        self.nk_pane_left.breadcrumb.toolbar_slot.addWidget(self.nk_actions_strip)
        for pane in (self.nk_pane_left, self.nk_pane_right):
            pane.activated.connect(self._on_nk_pane_activated)
            pane.activeTabChanged.connect(self._on_nk_active_tab_changed)
            pane.lastTabClosed.connect(self._on_nk_pane_last_tab_closed)
            pane.localNoteSaved.connect(self._on_nk_pane_local_saved)
            pane.studioViewChanged.connect(self._on_nk_studio_view_changed)
            pane.rsvpRequested.connect(self._on_nk_rsvp_requested)
            pane.summarizeRequested.connect(self._on_nk_summarize_clicked)
            pane.flashcardsRequested.connect(self._on_nk_extract_flashcards_clicked)
            pane.sendSelectionToSplitRequested.connect(self._on_nk_send_text_to_other_pane)
            pane.acquireLockRequested.connect(self._on_nk_acquire_group_lock)
            pane.publishGroupRequested.connect(self._on_nk_publish_group_note)
            pane.copyToLocalRequested.connect(self._on_nk_copy_group_to_local)
            pane.publishLocalToGroupRequested.connect(self._on_nk_publish_local_to_group)
            pane.openGroupNoteFromBoardRequested.connect(self._on_nk_open_group_note_from_board)
            pane.refreshBoardRequested.connect(self._on_nk_refresh_group_board)
            pane.splitTabRequested.connect(self._on_nk_split_specific_tab)
            pane.tabDroppedFromPane.connect(self._on_nk_tab_dropped_from_pane)
            pane.nodeDroppedFromTree.connect(self._on_nk_node_dropped_from_tree)
            pane.wikilinkOpenRequested.connect(self._open_note_by_wikilink_title)
            pane.noteIdOpenRequested.connect(self._load_note_into_nk_editor)
            pane.restoreTrashedNoteRequested.connect(self._on_nk_restore_note)
            pane.permanentDeleteTrashedNoteRequested.connect(self._on_nk_permanent_delete_note)
            pane.breadcrumb.rootClicked.connect(self._on_nk_breadcrumb_root_clicked)
            pane.breadcrumb.subjectClicked.connect(self._on_nk_breadcrumb_subject_clicked)

        self.nk_panes_splitter.addWidget(self.nk_pane_left)
        self.nk_panes_splitter.addWidget(self.nk_pane_right)
        col3_layout.addWidget(self.nk_panes_splitter, 1)

        # Scorciatoie da tastiera FlowNotes (attive quando la pagina è visibile)
        self.nk_shortcut_new_tab = QShortcut(QKeySequence("Ctrl+N"), page)
        self.nk_shortcut_new_tab.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.nk_shortcut_new_tab.activated.connect(self._on_nk_shortcut_new_tab)

        self.nk_shortcut_close_tab = QShortcut(QKeySequence("Ctrl+W"), page)
        self.nk_shortcut_close_tab.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.nk_shortcut_close_tab.activated.connect(self._on_nk_shortcut_close_tab)

        self.nk_shortcut_next_tab = QShortcut(QKeySequence("Ctrl+Tab"), page)
        self.nk_shortcut_next_tab.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.nk_shortcut_next_tab.activated.connect(lambda: self._active_nk_pane().cycle_tab(1))

        self.nk_shortcut_prev_tab = QShortcut(QKeySequence("Ctrl+Shift+Tab"), page)
        self.nk_shortcut_prev_tab.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.nk_shortcut_prev_tab.activated.connect(lambda: self._active_nk_pane().cycle_tab(-1))

        self.nk_shortcut_split = QShortcut(QKeySequence("Ctrl+\\"), page)
        self.nk_shortcut_split.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.nk_shortcut_split.activated.connect(self._on_nk_toggle_split_view)

        splitter.addWidget(col3)

        # Impedisci il collasso dell'Editor (col 2), consenti il collasso controllato di col 0 e 1
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, True)
        splitter.setCollapsible(2, False)
        for h_idx in (1, 2):
            handle = splitter.handle(h_idx)
            if handle is not None:
                handle.setCursor(Qt.CursorShape.SplitHCursor)
        splitter.splitterMoved.connect(lambda _pos, _idx: self._sync_nk_panel_toggles())

        # Layout a 2 colonne di default: Sidebar Unica 280px + Editor Gigante ~80%
        splitter.setSizes([280, 0, 920])
        nk_container_layout.addWidget(splitter, 1)
        main_vbox.addWidget(nk_container, 1)

        self._nk_selected_note_id = None
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None
        self._nk_filter_favorites_only = False
        self._nk_initial_note_loaded = False
        self._restoring_nk_session = False

        return page

    def _build_notes_page(self) -> QWidget:
        """Retrocompatibilità: rimanda a _build_flowstudy_notekeeper_page."""
        return self._build_flowstudy_notekeeper_page()

    # -----------------------------------------------------------------
    # FlowNotes Multi-Workspace, Multi-Tab & Split-View Helpers
    # -----------------------------------------------------------------
    def _active_nk_pane(self) -> EditorPane:
        pane = getattr(self, "_nk_active_pane", None)
        if pane is not None:
            return pane
        return self.nk_pane_left

    def _other_nk_pane(self, pane: Optional[EditorPane] = None) -> EditorPane:
        cur = pane or self._active_nk_pane()
        return self.nk_pane_right if cur is self.nk_pane_left else self.nk_pane_left

    @property
    def nk_editor_title(self):
        return self._active_nk_pane().editor_title

    @property
    def nk_editor_subject(self):
        return self._active_nk_pane().editor_subject

    @property
    def nk_editor_topic(self):
        return self._active_nk_pane().editor_topic

    @property
    def nk_editor_type(self):
        return self._active_nk_pane().editor_type

    @property
    def nk_editor_profile(self):
        return self._active_nk_pane().editor_profile

    @property
    def nk_editor_body(self):
        return self._active_nk_pane().editor_body

    @property
    def nk_audio_bar(self):
        return self._active_nk_pane().audio_bar

    @property
    def nk_autosave_badge(self):
        return self._active_nk_pane().autosave_badge

    def _nk_author_name(self) -> str:
        acc_name = db.get_google_account().get("display_name")
        if acc_name:
            return acc_name
        if hasattr(self, "nk_profile_combo"):
            p = self.nk_profile_combo.currentText().strip()
            if p and p != "Tutti i profili":
                return p
        return "Alessandro"

    def _on_nk_pane_activated(self, pane: EditorPane):
        if getattr(self, "_nk_active_pane", None) is pane:
            return
        self._nk_active_pane = pane
        self.nk_pane_left.set_active_pane(pane is self.nk_pane_left)
        self.nk_pane_right.set_active_pane(pane is self.nk_pane_right)
        if hasattr(self, "nk_actions_strip") and hasattr(pane, "breadcrumb"):
            if self.nk_actions_strip.parentWidget() is not pane.breadcrumb:
                pane.breadcrumb.toolbar_slot.addWidget(self.nk_actions_strip)
                self.nk_actions_strip.show()
        self._sync_toolbar_from_active_pane()

    def _on_nk_breadcrumb_root_clicked(self):
        active_gid = getattr(self, "_nk_active_workspace_group_id", None)
        if active_gid is not None:
            groups = {int(g["id"]): g for g in db.get_flow_groups()}
            g = groups.get(int(active_gid))
            if g is not None:
                self._active_nk_pane().open_group_board(g)
                return
        self._nk_current_filter_folder_id = None
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None
        self._refresh_nk_notes_list()

    def _on_nk_breadcrumb_subject_clicked(self, subject: str):
        subj = (subject or "").strip()
        if not subj:
            return
        if hasattr(self, "nk_search_edit"):
            self.nk_search_edit.setText("")
        if hasattr(self, "nk_tree"):
            for i in range(self.nk_tree.topLevelItemCount()):
                item = self.nk_tree.topLevelItem(i)
                if item and subj.lower() in item.text(0).lower():
                    item.setExpanded(True)
                    self.nk_tree.setCurrentItem(item)
                    self._on_nk_tree_item_clicked(item, 0)
                    break

    def _on_nk_active_tab_changed(self, tab: Optional[TabContext]):
        if tab is not None and tab.workspace_type == "local":
            self._nk_selected_note_id = tab.note_id
            if tab.note_id is not None and not getattr(self, "_restoring_nk_session", False):
                try:
                    db.set_setting("last_viewed_note_id", str(int(tab.note_id)))
                except Exception:
                    pass
        if not getattr(self, "_restoring_nk_session", False):
            self.save_workspace_session_state()
        self._sync_toolbar_from_active_pane()

    def save_workspace_session_state(self):
        """Salva le schede locali aperte (left/right pane) e lo stato Split View per ripristino all'avvio."""
        if not hasattr(self, "nk_pane_left"):
            return
        try:
            left_ids = [
                int(t.note_id)
                for t in self.nk_pane_left.tabs
                if t.workspace_type == "local" and t.note_id is not None
            ]
            right_ids = [
                int(t.note_id)
                for t in self.nk_pane_right.tabs
                if t.workspace_type == "local" and t.note_id is not None
            ] if hasattr(self, "nk_pane_right") else []
            payload = {
                "left_ids": left_ids[:15],
                "left_active": self.nk_pane_left.current_tab_index,
                "right_ids": right_ids[:15],
                "right_active": self.nk_pane_right.current_tab_index if hasattr(self, "nk_pane_right") else -1,
                "split_open": bool(hasattr(self, "nk_pane_right") and not self.nk_pane_right.isHidden() and right_ids),
            }
            db.set_setting("flownotes_open_session", json.dumps(payload))
        except Exception:
            pass

    def _sync_toolbar_from_active_pane(self):
        pane = self._active_nk_pane()
        tab = pane.active_tab()
        if hasattr(self, "nk_favorite_btn"):
            is_local_saved = bool(tab and tab.workspace_type == "local" and tab.note_id is not None)
            is_fav = bool(tab and tab.is_favorite) if is_local_saved else False
            self.nk_favorite_btn.setEnabled(is_local_saved)
            self.nk_favorite_btn.blockSignals(True)
            self.nk_favorite_btn.setChecked(is_fav)
            self.nk_favorite_btn.setIcon(
                get_icon(
                    "star_fill" if is_fav else "star",
                    theme_key=self._current_theme,
                    role="warning" if is_fav else "subtitle",
                    size=14,
                )
            )
            self.nk_favorite_btn.setToolTip(
                "Rimuovi dai Preferiti" if is_fav else "Aggiungi questa nota ai Preferiti"
            )
            self.nk_favorite_btn.blockSignals(False)
        if hasattr(self, "nk_studio_btn"):
            is_st = bool(tab and tab.is_studio_view) or bool(pane.editor_body.is_studio_view)
            self.nk_studio_btn.blockSignals(True)
            self.nk_studio_btn.setChecked(is_st)
            self.nk_studio_btn.setText(" Modifica" if is_st else " Vista Studio")
            self.nk_studio_btn.blockSignals(False)
        if hasattr(self, "nk_split_btn") and hasattr(self, "nk_pane_right"):
            is_split = not self.nk_pane_right.isHidden()
            self.nk_split_btn.blockSignals(True)
            self.nk_split_btn.setChecked(is_split)
            self.nk_split_btn.blockSignals(False)
            if hasattr(self, "nk_send_to_split_btn"):
                self.nk_send_to_split_btn.setVisible(is_split)

    def _on_nk_pane_last_tab_closed(self, pane: EditorPane):
        if pane is self.nk_pane_right:
            self.nk_pane_right.hide()
            self._on_nk_pane_activated(self.nk_pane_left)
            self._sync_toolbar_from_active_pane()
        else:
            pane.open_new_empty_tab(
                subject=self._nk_current_filter_subject or "Generale",
                topic=self._nk_current_filter_topic or "Generale",
                profile=self._nk_author_name(),
            )

    def _on_nk_pane_local_saved(self, saved_id: int, title: str, was_new: bool):
        self._nk_selected_note_id = saved_id
        try:
            db.set_setting("last_viewed_note_id", str(int(saved_id)))
        except Exception:
            pass
        self.save_workspace_session_state()
        self._sync_toolbar_from_active_pane()
        if self._nk_active_workspace_group_id is None:
            if was_new:
                self._refresh_notekeeper()
            else:
                # Aggiorna il titolo nel tree e nelle card senza ricostruire l'intera UI
                clean_t = strip_markdown_syntax((title or "Senza titolo").strip()) or "Senza titolo"
                if hasattr(self, "nk_tree"):
                    from PyQt6.QtWidgets import QTreeWidgetItemIterator
                    it = QTreeWidgetItemIterator(self.nk_tree)
                    while it.value():
                        item = it.value()
                        ndata = item.data(0, ROLE_NODE_DATA)
                        if ndata and ndata[0] == "note" and int(ndata[1]) == int(saved_id):
                            old_lbl = item.text(0)
                            prefix = ""
                            for tok in ("★ ", "🎙️ ", "✅ ", "📄 "):
                                if old_lbl.startswith(tok):
                                    prefix += tok
                                    old_lbl = old_lbl[len(tok):]
                            if not prefix:
                                prefix = "📄 "
                            item.setText(0, f"{prefix}{clean_t[:32]}")
                            item.setToolTip(0, clean_t)
                        it += 1
                if hasattr(self, "nk_notes_list"):
                    for i in range(self.nk_notes_list.vbox.count()):
                        w = self.nk_notes_list.vbox.itemAt(i).widget()
                        if w is not None and w.objectName() == "NKNoteCard" and w.property("note_id") == saved_id:
                            for lbl in w.findChildren(QLabel):
                                if lbl.objectName() == "NKNoteTitle":
                                    lbl.setText(clean_t)
                                    break
                            break

    def _on_nk_workspace_selected(self, group_id: Optional[int]):
        self._nk_active_workspace_group_id = int(group_id) if group_id is not None else None
        self._refresh_notekeeper()
        if self._nk_active_workspace_group_id is not None:
            self._open_group_board_in_pane(self._active_nk_pane(), self._nk_active_workspace_group_id)

    def _on_nk_join_or_create_group(self):
        dlg = JoinOrCreateGroupDialog(
            author_name=self._nk_author_name(),
            default_subject=self._nk_current_filter_subject or "Generale",
            palette=self._active_palette(),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_group:
            gid = int(dlg.result_group["id"])
            self._nk_active_workspace_group_id = gid
            self._refresh_notekeeper()
            self._open_group_board_in_pane(self._active_nk_pane(), gid)

    def _on_nk_remove_group(self, group_id: int):
        ans = QMessageBox.question(
            self,
            "Scollega Gruppo",
            "Scollegare questo gruppo da FlowNotes? (I file su Drive / cartella locale non verranno eliminati.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        db.delete_flow_group(int(group_id))
        if self._nk_active_workspace_group_id == int(group_id):
            self._nk_active_workspace_group_id = None
        self._refresh_notekeeper()

    def _open_group_board_in_pane(self, pane: EditorPane, group_id: int, in_new_tab: bool = False):
        groups = {int(g["id"]): g for g in db.get_flow_groups()}
        g = groups.get(int(group_id))
        if not g:
            return
        try:
            board_data = flow_groups.get_group_board_data(g, current_author=self._nk_author_name())
        except Exception as exc:
            QMessageBox.warning(self, "FlowGroups", f"Impossibile caricare la bacheca del gruppo:\n{exc}")
            return
        pane.board_widget.populate(board_data)
        ctx = TabContext(
            workspace_type="board",
            group_id=int(group_id),
            group_rec=g,
            title=f"Bacheca · {g.get('group_name')}",
        )
        pane.open_tab(ctx, in_new_tab=in_new_tab)

    def _on_nk_refresh_group_board(self, pane: EditorPane):
        tab = pane.active_tab()
        if tab and tab.group_id:
            try:
                board_data = flow_groups.get_group_board_data(tab.group_rec or {}, current_author=self._nk_author_name())
                pane.board_widget.populate(board_data)
            except Exception as exc:
                QMessageBox.warning(self, "FlowGroups", str(exc))
            if self._nk_active_workspace_group_id == tab.group_id:
                self._refresh_notekeeper()

    def _on_nk_open_group_note_from_board(self, pane: EditorPane, item: dict):
        tab = pane.active_tab()
        g = (tab.group_rec if tab else None) or {}
        if not g and self._nk_active_workspace_group_id:
            groups = {int(x["id"]): x for x in db.get_flow_groups()}
            g = groups.get(int(self._nk_active_workspace_group_id), {})
        if g:
            self._open_group_note_in_pane(pane, g, item, in_new_tab=True)

    def _open_group_note_in_pane(
        self,
        pane: EditorPane,
        group_rec: Dict[str, Any],
        item: Dict[str, Any],
        in_new_tab: bool = False,
    ):
        try:
            title, md = flow_groups.download_note(group_rec, item)
        except Exception as exc:
            QMessageBox.warning(self, "FlowGroups", f"Impossibile aprire la nota del gruppo:\n{exc}")
            return
        ctx = TabContext(
            workspace_type="group",
            group_id=int(group_rec["id"]),
            group_rec=group_rec,
            subject=item.get("subject") or "Generale",
            title=title,
            rel_path=item.get("rel_path") or f"{item.get('subject')}/{item.get('filename')}",
            group_note_item=item,
            content_markdown=md,
            is_dirty=False,
            is_locked_by_me=bool(item.get("locked_by_me")),
            lock_info=item.get("lock_info"),
            created_by_name=item.get("created_by_name") or "",
            created_by_email=item.get("created_by_email") or "",
            created_by_avatar=item.get("created_by_avatar") or "",
            last_edited_by_name=item.get("last_edited_by_name") or "",
            last_edited_by_email=item.get("last_edited_by_email") or "",
            last_edited_at_utc=item.get("last_edited_at_utc") or "",
        )
        pane.open_tab(ctx, in_new_tab=in_new_tab)

    def _open_nk_tree_node(self, data: object, in_new_tab: bool = False, in_split: bool = False):
        if not data:
            return
        target_pane = self._active_nk_pane()
        if in_split:
            if self.nk_pane_right.isHidden():
                self.nk_pane_right.show()
                w = max(self.nk_panes_splitter.width(), 840)
                self.nk_panes_splitter.setSizes([w // 2, w // 2])
            target_pane = self.nk_pane_right
            in_new_tab = True

        kind = data[0]
        if kind == "todos":
            self._open_all_todos_dialog()
            return
        if kind == "note":
            self._load_note_into_nk_editor(int(data[1]), pane=target_pane, in_new_tab=in_new_tab)
        elif kind == "trashed_note":
            self._load_note_into_nk_editor(int(data[1]), pane=target_pane, in_new_tab=in_new_tab)
        elif kind == "group_board":
            self._open_group_board_in_pane(target_pane, int(data[1]), in_new_tab=in_new_tab)
        elif kind == "group_note":
            gid = int(data[1])
            item = data[2]
            groups = {int(x["id"]): x for x in db.get_flow_groups()}
            g = groups.get(gid)
            if g:
                self._open_group_note_in_pane(target_pane, g, item, in_new_tab=in_new_tab)
        if in_split:
            self._on_nk_pane_activated(self.nk_pane_right)
            self._sync_toolbar_from_active_pane()

    def _on_nk_toggle_split_view(self):
        if not hasattr(self, "nk_pane_right"):
            return
        if not self.nk_pane_right.isHidden():
            self.nk_pane_right.hide()
            self._on_nk_pane_activated(self.nk_pane_left)
            self._sync_toolbar_from_active_pane()
            return

        self.nk_pane_right.show()
        w = max(self.nk_panes_splitter.width(), 840)
        self.nk_panes_splitter.setSizes([w // 2, w // 2])
        if not self.nk_pane_right.tabs:
            cur_tab = self.nk_pane_left.active_tab()
            if cur_tab is not None and cur_tab.workspace_type == "local" and cur_tab.note_id:
                self._load_note_into_nk_editor(cur_tab.note_id, pane=self.nk_pane_right, in_new_tab=True)
            elif cur_tab is not None and cur_tab.workspace_type == "group" and cur_tab.group_rec and cur_tab.group_note_item:
                self._open_group_note_in_pane(self.nk_pane_right, cur_tab.group_rec, cur_tab.group_note_item, in_new_tab=True)
            else:
                self.nk_pane_right.open_new_empty_tab(
                    subject=self._nk_current_filter_subject or "Generale",
                    topic=self._nk_current_filter_topic or "Generale",
                    profile=self._nk_author_name(),
                )
        self._on_nk_pane_activated(self.nk_pane_right)
        self._sync_toolbar_from_active_pane()

    def _on_nk_split_specific_tab(self, source_pane: EditorPane, tab_idx: int):
        if tab_idx < 0 or tab_idx >= len(source_pane.tabs):
            return
        tab = source_pane.tabs[tab_idx]
        target_pane = self._other_nk_pane(source_pane)
        if self.nk_pane_right.isHidden():
            self.nk_pane_right.show()
            w = max(self.nk_panes_splitter.width(), 840)
            self.nk_panes_splitter.setSizes([w // 2, w // 2])
        if tab.workspace_type == "local" and tab.note_id:
            self._load_note_into_nk_editor(tab.note_id, pane=target_pane, in_new_tab=True)
        elif tab.workspace_type == "group" and tab.group_rec and tab.group_note_item:
            self._open_group_note_in_pane(target_pane, tab.group_rec, tab.group_note_item, in_new_tab=True)
        elif tab.workspace_type == "board" and tab.group_id:
            self._open_group_board_in_pane(target_pane, tab.group_id, in_new_tab=True)
        else:
            target_pane.open_new_empty_tab(profile=self._nk_author_name())
        self._on_nk_pane_activated(target_pane)
        self._sync_toolbar_from_active_pane()

    def _on_nk_tab_dropped_from_pane(self, source_pane_id: str, tab_idx: int, target_pane: EditorPane, drop_pos: QPoint):
        source_pane = self.nk_pane_left if source_pane_id == "left" else self.nk_pane_right
        if tab_idx < 0 or tab_idx >= len(source_pane.tabs):
            return
        # Se rilasciato sulla metà destra del pannello singolo, attiva Split View e sposta a destra
        if source_pane is target_pane and self.nk_pane_right.isHidden() and drop_pos.x() > max(200, int(target_pane.width() * 0.55)):
            self.nk_pane_right.show()
            w = max(self.nk_panes_splitter.width(), 840)
            self.nk_panes_splitter.setSizes([w // 2, w // 2])
            target_pane = self.nk_pane_right
        if source_pane is target_pane:
            return
        tab = source_pane.take_tab(tab_idx)
        if tab is None:
            return
        target_pane.open_tab(tab, in_new_tab=True)
        self._on_nk_pane_activated(target_pane)
        self._sync_toolbar_from_active_pane()

    def _on_nk_node_dropped_from_tree(self, data: object, target_pane: EditorPane):
        if not data:
            return
        self._on_nk_pane_activated(target_pane)
        self._open_nk_tree_node(data, in_new_tab=True)

    def _on_nk_send_selection_to_other_pane(self):
        pane = self._active_nk_pane()
        cur = pane.editor_body.textCursor()
        if cur.hasSelection() and cur.selectedText().strip():
            sel = (
                pane.editor_body._selection_to_markdown(cur)
                if hasattr(pane.editor_body, "_selection_to_markdown")
                else cur.selectedText().replace("\u2029", "\n")
            )
            self._on_nk_send_text_to_other_pane(sel)
        else:
            QMessageBox.information(self, "Split View", "Seleziona prima del testo o una formula da inviare al pannello affiancato.")

    def _on_nk_send_text_to_other_pane(self, text: str):
        clean = (text or "").strip()
        if not clean:
            return
        cur_pane = self._active_nk_pane()
        if self.nk_pane_right.isHidden():
            self._on_nk_toggle_split_view()
        target = self._other_nk_pane(cur_pane)
        if target.stack.currentIndex() != 0 or not target.tabs:
            target.open_new_empty_tab(profile=self._nk_author_name())
        tab = target.active_tab()
        if tab and tab.workspace_type == "group" and not tab.is_locked_by_me:
            QMessageBox.information(self, "Sola lettura", "Il pannello affiancato è in sola lettura. Acquisisci il lock o apri una nota del tuo Quaderno.")
            return
        cur_md = target.editor_body.get_markdown_text().rstrip()
        new_md = f"{cur_md}\n\n{clean}\n" if cur_md else f"{clean}\n"
        target.editor_body.set_markdown_text(new_md)
        target._on_editor_modified()
        target.set_autosave_badge(True, "✓ Selezione ricevuta dal pannello affiancato")

    def _on_nk_shortcut_new_tab(self):
        pane = self._active_nk_pane()
        pane.open_new_empty_tab(
            subject=self._nk_current_filter_subject or "Generale",
            topic=self._nk_current_filter_topic or "Generale",
            profile=self._nk_author_name(),
        )

    def _on_nk_shortcut_close_tab(self):
        pane = self._active_nk_pane()
        if pane.current_tab_index >= 0:
            pane.close_tab(pane.current_tab_index)

    def _on_nk_acquire_group_lock(self, pane: EditorPane):
        tab = pane.active_tab()
        if not tab or tab.workspace_type != "group" or not tab.group_rec:
            return
        fn = (tab.group_note_item or {}).get("filename") or f"{tab.title}.md"
        try:
            ok, msg, lock_data = flow_groups.acquire_lock(
                tab.group_rec,
                subject=tab.subject,
                filename=fn,
                author=self._nk_author_name(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Acquisizione Lock", str(exc))
            return
        if not ok:
            QMessageBox.warning(self, "Nota Bloccata", msg)
            return
        tab.is_locked_by_me = True
        tab.lock_info = lock_data
        if tab.group_note_item is not None:
            tab.group_note_item["locked_by_me"] = True
            tab.group_note_item["locked_by_other"] = False
            tab.group_note_item["lock_author"] = self._nk_author_name()
            tab.group_note_item["lock_info"] = lock_data
        pane._render_tab(tab)
        self._refresh_notekeeper()

    def _on_nk_publish_group_note(self, pane: EditorPane):
        tab = pane.active_tab()
        if not tab or tab.workspace_type != "group" or not tab.group_rec:
            return
        pane._capture_current_tab_state()
        try:
            res = flow_groups.publish_note(
                tab.group_rec,
                subject=tab.subject,
                title=tab.title,
                markdown_content=tab.content_markdown,
                author=self._nk_author_name(),
                release_lock=True,
            )
            tab.is_dirty = False
            tab.is_locked_by_me = False
            tab.lock_info = None
            for k in ("created_by_name", "created_by_email", "created_by_avatar", "last_edited_by_name", "last_edited_by_email", "last_edited_at_utc"):
                if res.get(k):
                    setattr(tab, k, res[k])
            if tab.group_note_item is not None:
                tab.group_note_item["locked_by_me"] = False
                tab.group_note_item["locked_by_other"] = False
                tab.group_note_item["lock_info"] = None
                for k in ("created_by_name", "created_by_email", "created_by_avatar", "last_edited_by_name", "last_edited_by_email", "last_edited_at_utc"):
                    if res.get(k):
                        tab.group_note_item[k] = res[k]
            pane.tab_bar_widget.update_tab_title(pane.current_tab_index, tab)
            pane._render_tab(tab)
            pane.set_autosave_badge(True, f"✓ Pubblicato in {res['subject']} (.history/ aggiornato)")
            self._refresh_notekeeper()
        except Exception as exc:
            QMessageBox.warning(self, "Errore Pubblicazione", str(exc))

    def _on_nk_copy_group_to_local(self, pane: EditorPane):
        tab = pane.active_tab()
        if not tab or tab.workspace_type != "group":
            return
        pane._capture_current_tab_state()
        try:
            nid = db.add_study_note(
                title=tab.title or "Nota Gruppo",
                content_markdown=tab.content_markdown or "",
                note_type="appunto",
                profile=self._nk_author_name(),
                subject=tab.subject or "Generale",
                topic="FlowGroup",
            )
            pane.set_autosave_badge(True, f"✓ Copiata nel Quaderno Locale (ID {nid})")
            if self._nk_active_workspace_group_id is None:
                self._refresh_notekeeper()
        except Exception as exc:
            QMessageBox.warning(self, "Copia nel Quaderno", str(exc))

    def _on_nk_publish_local_to_group(self, pane: EditorPane):
        tab = pane.active_tab()
        if not tab or tab.workspace_type != "local":
            return
        pane._capture_current_tab_state()
        groups = db.get_flow_groups()
        if not groups:
            ans = QMessageBox.question(
                self,
                "Nessun Gruppo Collegato",
                "Non hai ancora collegato nessun FlowGroup. Vuoi crearne o collegarne uno ora?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._on_nk_join_or_create_group()
            return
        menu = QMenu(self)
        for g in groups:
            is_loc = bool((g.get("local_fallback_path") or "").strip())
            prefix = "[Locale]" if is_loc else "[Drive]"
            act = menu.addAction(
                get_icon("folder" if is_loc else "cloud", theme_key=self._current_theme, size=14),
                f"{prefix} {g.get('group_name')}",
            )
            act.setData(g)
        chosen = menu.exec(pane.breadcrumb.btn_publish_local.mapToGlobal(QPoint(0, pane.breadcrumb.btn_publish_local.height())))
        if not chosen:
            return
        g = chosen.data()
        try:
            res = flow_groups.publish_note(
                g,
                subject=tab.subject or "Generale",
                title=tab.title or "Appunto",
                markdown_content=tab.content_markdown or "",
                author=self._nk_author_name(),
                release_lock=True,
            )
            pane.set_autosave_badge(True, f"✓ Pubblicata in {g.get('group_name')} ({res['subject']})")
            if self._nk_active_workspace_group_id == int(g["id"]):
                self._refresh_notekeeper()
        except Exception as exc:
            QMessageBox.warning(self, "Errore Pubblicazione", str(exc))

    def _on_nk_search_changed(self):
        self._refresh_notekeeper()

    def _toggle_nk_card_view(self, force_state: Optional[bool] = None):
        if not hasattr(self, "nk_col2"):
            return
        show = (not self.nk_col2.isVisible()) if force_state is None else bool(force_state)
        self.nk_col2.setVisible(show)
        if hasattr(self, "nk_card_view_btn"):
            self.nk_card_view_btn.setChecked(show)
        if show:
            total = max(sum(self.nk_splitter.sizes()), 1020)
            c1_w = 280 if self.nk_col1.isVisible() else 0
            self.nk_splitter.setSizes([c1_w, 280, max(420, total - c1_w - 280)])
        self._sync_nk_panel_toggles()

    def _is_nk_col_collapsed(self, col_idx: int) -> bool:
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        if not col.isVisible():
            return True
        sizes = self.nk_splitter.sizes()
        return col_idx < len(sizes) and sizes[col_idx] == 0

    def _sync_nk_panel_toggles(self):
        if not hasattr(self, "nk_splitter"):
            return
        col1_closed = self._is_nk_col_collapsed(0)
        col2_closed = self._is_nk_col_collapsed(1)
        self.nk_show_col1_in_col2_btn.setVisible(col1_closed and not col2_closed)
        self.nk_show_col1_btn.setVisible(col1_closed and col2_closed)
        self.nk_show_col2_btn.setVisible(False)
        if col1_closed and col2_closed:
            self.nk_focus_mode_btn.setToolTip("Ripristina Sidebar FlowNotes")
        else:
            self.nk_focus_mode_btn.setToolTip("Modalità Focus (nascondi sidebar)")

    def _hide_nk_col(self, col_idx: int):
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        col.hide()
        self._sync_nk_panel_toggles()

    def _show_nk_col(self, col_idx: int):
        col = self.nk_col1 if col_idx == 0 else self.nk_col2
        target_w = 280 if col_idx == 0 else 280
        col.show()
        sizes = self.nk_splitter.sizes()
        if len(sizes) == 3:
            total = sum(sizes)
            if sizes[col_idx] < 100:
                sizes[col_idx] = target_w
                other_idx = 1 if col_idx == 0 else 0
                other_w = sizes[other_idx] if self.nk_col1.isVisible() and self.nk_col2.isVisible() else 0
                sizes[2] = max(420, total - target_w - other_w)
                self.nk_splitter.setSizes(sizes)
        self._sync_nk_panel_toggles()

    def _toggle_nk_focus_mode(self):
        if self._is_nk_col_collapsed(0):
            self.nk_col1.show()
            total = max(sum(self.nk_splitter.sizes()), 1020)
            c2_w = 280 if self.nk_col2.isVisible() else 0
            self.nk_splitter.setSizes([280, c2_w, max(420, total - 280 - c2_w)])
        else:
            self.nk_col1.hide()
            self.nk_col2.hide()
        self._sync_nk_panel_toggles()

    def _make_note_leaf_item(self, note: Dict, p: Dict[str, str], max_len: int = 32) -> QTreeWidgetItem:
        clean_t = strip_markdown_syntax((note.get("title") or "Senza titolo").replace("Nota: ", "").strip()) or "Senza titolo"
        is_sbobina = (note.get("note_type") == "sbobina") or bool(note.get("audio_path"))
        tags_str = (note.get("tags") or "")
        is_studied = bool(note.get("is_studied")) or ("Studiata" in tags_str)
        is_favorite = bool(note.get("is_favorite"))
        icon_prefix = "🎙️" if is_sbobina else ("✅" if is_studied else "📄")
        fav_prefix = "★ " if is_favorite else ""
        label = f"{fav_prefix}{icon_prefix} {clean_t[:max_len]}"
        leaf = QTreeWidgetItem([label])
        leaf.setData(0, Qt.ItemDataRole.UserRole, ("note", note["id"]))
        leaf.setData(0, ROLE_NODE_DATA, ("note", note["id"]))
        leaf.setData(0, ROLE_IS_FOLDER, False)
        leaf.setToolTip(0, f"★ {clean_t}" if is_favorite else clean_t)
        leaf.setForeground(0, QBrush(QColor(p["text"])))
        leaf.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        return leaf

    def _refresh_notekeeper(self):
        """Ricarica l'albero gerarchico (Quaderno Locale oppure FlowGroup attivo)."""
        if not hasattr(self, "nk_tree"):
            return
        groups = db.get_flow_groups()
        active_gid = getattr(self, "_nk_active_workspace_group_id", None)
        if active_gid is not None and not any(int(g["id"]) == int(active_gid) for g in groups):
            active_gid = None
            self._nk_active_workspace_group_id = None
        if hasattr(self, "nk_workspace_switcher"):
            self.nk_workspace_switcher.set_state(groups, active_gid)

        p = self._active_palette()
        if hasattr(self.nk_tree, "set_theme_palette"):
            self.nk_tree.set_theme_palette(p)
        sq = self.nk_search_input.text().strip() if hasattr(self, "nk_search_input") else ""

        if active_gid is not None:
            g = next((x for x in groups if int(x["id"]) == int(active_gid)), None)
            if g is not None:
                self._refresh_group_tree(g, p, sq)
                return

        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text

        all_notes = db.get_notes_by_hierarchy(
            profile=prof_filter,
            subject=None,
            topic=None,
            search_query=sq,
        )
        folders = db.get_folders_tree(profile=prof_filter)

        self.nk_tree.blockSignals(True)
        self.nk_tree.clear()

        # 1. Voce fissa/collassabile in cima: Tutte le note
        all_item = QTreeWidgetItem(["Tutte le note"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("all",))
        all_item.setData(0, ROLE_NODE_DATA, ("all",))
        all_item.setData(0, ROLE_IS_FOLDER, True)
        all_item.setData(0, ROLE_LOCKED_FOLDER, True)
        all_item.setData(0, ROLE_COUNT, len(all_notes))
        all_item.setIcon(0, make_vector_folder_icon(p.get("accent", "#6366F1"), 16))
        f_top = QFont("Segoe UI", 9)
        f_top.setWeight(QFont.Weight.DemiBold)
        all_item.setFont(0, f_top)
        all_item.setForeground(0, QBrush(QColor(p["title"])))
        all_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.nk_tree.addTopLevelItem(all_item)

        # 1b. Sezione rapida: Preferiti
        fav_notes = [n for n in all_notes if bool(n.get("is_favorite"))]
        fav_item = QTreeWidgetItem(["Preferiti"])
        fav_item.setData(0, Qt.ItemDataRole.UserRole, ("favorites",))
        fav_item.setData(0, ROLE_NODE_DATA, ("favorites",))
        fav_item.setData(0, ROLE_IS_FOLDER, True)
        fav_item.setData(0, ROLE_LOCKED_FOLDER, True)
        fav_item.setData(0, ROLE_COUNT, len(fav_notes))
        fav_item.setIcon(0, get_icon("star_fill" if fav_notes else "star", theme_key=self._current_theme, role="warning", size=15))
        fav_item.setFont(0, f_top)
        fav_item.setForeground(0, QBrush(QColor(p["title"])))
        fav_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        for n in fav_notes:
            fav_item.addChild(self._make_note_leaf_item(n, p, max_len=30))
        self.nk_tree.addTopLevelItem(fav_item)
        if fav_notes:
            fav_item.setExpanded(True)

        # 1c. Sezione rapida: Tutti i To-Do aperti (aggregati da tutte le note)
        open_todos = db.get_all_note_todos(include_completed=False, profile=prof_filter)
        todos_item = QTreeWidgetItem(["To-Do aperti"])
        todos_item.setData(0, Qt.ItemDataRole.UserRole, ("todos",))
        todos_item.setData(0, ROLE_NODE_DATA, ("todos",))
        todos_item.setData(0, ROLE_IS_FOLDER, True)
        todos_item.setData(0, ROLE_LOCKED_FOLDER, True)
        todos_item.setData(0, ROLE_COUNT, len(open_todos))
        todos_item.setIcon(0, get_icon("check_square", theme_key=self._current_theme, role="success" if open_todos else "subtitle", size=15))
        todos_item.setFont(0, f_top)
        todos_item.setForeground(0, QBrush(QColor(p["title"])))
        todos_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.nk_tree.addTopLevelItem(todos_item)

        # 2. Sezione rapida: Recenti (ultimi appunti modificati)
        recent_slice = all_notes[:8]
        recent_item = QTreeWidgetItem(["Recenti"])
        recent_item.setData(0, Qt.ItemDataRole.UserRole, ("recent",))
        recent_item.setData(0, ROLE_NODE_DATA, ("recent",))
        recent_item.setData(0, ROLE_IS_FOLDER, True)
        recent_item.setData(0, ROLE_LOCKED_FOLDER, True)
        recent_item.setData(0, ROLE_COUNT, len(recent_slice))
        recent_item.setIcon(0, make_vector_folder_icon(p.get("subtitle", "#9CA3AF"), 16))
        recent_item.setFont(0, f_top)
        recent_item.setForeground(0, QBrush(QColor(p["badge_text"])))
        recent_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        for n in recent_slice:
            recent_item.addChild(self._make_note_leaf_item(n, p, max_len=32))
        self.nk_tree.addTopLevelItem(recent_slice and recent_item or recent_item)
        recent_item.setExpanded(True)

        # 3. Albero gerarchico delle cartelle & sottocartelle
        notes_by_folder: Dict[Optional[int], List[Dict]] = {}
        for n in all_notes:
            fid = n.get("folder_id")
            notes_by_folder.setdefault(fid, []).append(n)

        children_by_parent: Dict[Optional[int], List[Dict]] = {}
        for f in folders:
            children_by_parent.setdefault(f.get("parent_id"), []).append(f)

        def _build_folder_node(f_dict: Dict) -> tuple[QTreeWidgetItem, int]:
            fid = f_dict["id"]
            color_hex = f_dict.get("color") or "#6366F1"
            f_item = QTreeWidgetItem([f_dict["name"]])
            f_item.setData(0, Qt.ItemDataRole.UserRole, ("folder", fid))
            f_item.setData(0, ROLE_NODE_DATA, ("folder", fid))
            f_item.setData(0, ROLE_IS_FOLDER, True)
            f_item.setData(0, ROLE_LOCKED_FOLDER, False)
            f_item.setData(0, ROLE_FOLDER_COLOR, color_hex)
            f_item.setIcon(0, make_vector_folder_icon(color_hex, 16))
            f_font = QFont("Segoe UI", 9)
            f_font.setWeight(QFont.Weight.DemiBold)
            f_item.setFont(0, f_font)
            f_item.setForeground(0, QBrush(QColor(p["title"])))
            f_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )

            direct_notes = notes_by_folder.get(fid, [])
            total_count = len(direct_notes)

            for sub_f in children_by_parent.get(fid, []):
                sub_item, sub_cnt = _build_folder_node(sub_f)
                total_count += sub_cnt
                if not sq or sub_cnt > 0:
                    f_item.addChild(sub_item)
                    sub_item.setExpanded(True)

            for n in direct_notes:
                f_item.addChild(self._make_note_leaf_item(n, p, max_len=30))

            f_item.setData(0, ROLE_COUNT, total_count)
            return f_item, total_count

        for root_f in children_by_parent.get(None, []):
            r_item, r_cnt = _build_folder_node(root_f)
            if not sq or r_cnt > 0:
                self.nk_tree.addTopLevelItem(r_item)
                r_item.setExpanded(True)

        # Eventuali note orfane senza folder_id
        orphan_notes = notes_by_folder.get(None, [])
        if orphan_notes:
            for n in orphan_notes:
                self.nk_tree.addTopLevelItem(self._make_note_leaf_item(n, p, max_len=32))

        # 4. Cestino ispezionabile in fondo all'albero
        trashed_notes = db.get_trashed_study_notes(profile=prof_filter, search_query=sq)
        trash_item = QTreeWidgetItem(["Cestino"])
        trash_item.setData(0, Qt.ItemDataRole.UserRole, ("trash_root",))
        trash_item.setData(0, ROLE_NODE_DATA, ("trash_root",))
        trash_item.setData(0, ROLE_IS_FOLDER, True)
        trash_item.setData(0, ROLE_LOCKED_FOLDER, True)
        trash_item.setData(0, ROLE_COUNT, len(trashed_notes))
        trash_item.setIcon(
            0,
            get_icon(
                "trash",
                theme_key=self._current_theme,
                role="danger" if trashed_notes else "subtitle",
                size=15,
            ),
        )
        trash_item.setFont(0, f_top)
        trash_item.setForeground(0, QBrush(QColor(p["title"] if trashed_notes else p["subtitle"])))
        trash_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        for tn in trashed_notes:
            clean_t = strip_markdown_syntax((tn.get("title") or "Senza titolo").replace("Nota: ", "").strip()) or "Senza titolo"
            t_leaf = QTreeWidgetItem([f"🗑 {clean_t[:30]}"])
            t_leaf.setData(0, Qt.ItemDataRole.UserRole, ("trashed_note", tn["id"]))
            t_leaf.setData(0, ROLE_NODE_DATA, ("trashed_note", tn["id"]))
            t_leaf.setData(0, ROLE_IS_FOLDER, False)
            t_leaf.setToolTip(0, f"{clean_t} (nel Cestino)")
            t_leaf.setForeground(0, QBrush(QColor(p["subtitle"])))
            t_leaf.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            trash_item.addChild(t_leaf)
        self.nk_tree.addTopLevelItem(trash_item)
        if sq and trashed_notes:
            trash_item.setExpanded(True)

        self.nk_tree.blockSignals(False)
        self._refresh_nk_notes_list()

    def _refresh_group_tree(self, group_rec: Dict[str, Any], p: Dict[str, str], sq: str = ""):
        """Popola l'albero della sidebar con Bacheca & Stato Sbobine e le materie/note del FlowGroup."""
        from ui.notes_tree import ROLE_STATUS_ICON
        gid = int(group_rec["id"])
        self.nk_tree.blockSignals(True)
        self.nk_tree.clear()

        # 1. Voce fissa in cima: Bacheca & Stato Sbobine
        board_item = QTreeWidgetItem(["Bacheca & Stato Sbobine"])
        board_item.setData(0, Qt.ItemDataRole.UserRole, ("group_board", gid))
        board_item.setData(0, ROLE_NODE_DATA, ("group_board", gid))
        board_item.setData(0, ROLE_IS_FOLDER, True)
        board_item.setData(0, ROLE_LOCKED_FOLDER, True)
        board_item.setIcon(0, get_icon("board", theme_key=self._current_theme, role="accent", size=16))
        f_top = QFont("Segoe UI", 9)
        f_top.setWeight(QFont.Weight.DemiBold)
        board_item.setFont(0, f_top)
        board_item.setForeground(0, QBrush(QColor(p["title"])))
        board_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.nk_tree.addTopLevelItem(board_item)

        try:
            board_data = flow_groups.get_group_board_data(group_rec, current_author=self._nk_author_name())
        except Exception as exc:
            err_item = QTreeWidgetItem([f"Errore lettura gruppo: {exc}"])
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.nk_tree.addTopLevelItem(err_item)
            self.nk_tree.blockSignals(False)
            return

        subjects = board_data.get("subjects") or ["Generale"]
        notes = board_data.get("notes") or []
        by_subj: Dict[str, List[Dict[str, Any]]] = {s: [] for s in subjects}
        for n in notes:
            s = n.get("subject") or "Generale"
            if sq and sq.lower() not in (n.get("title") or "").lower() and sq.lower() not in s.lower():
                continue
            by_subj.setdefault(s, []).append(n)

        for subj in sorted(by_subj.keys()):
            s_notes = by_subj[subj]
            if sq and not s_notes:
                continue
            f_item = QTreeWidgetItem([subj])
            f_item.setData(0, Qt.ItemDataRole.UserRole, ("group_subject", gid, subj))
            f_item.setData(0, ROLE_NODE_DATA, ("group_subject", gid, subj))
            f_item.setData(0, ROLE_IS_FOLDER, True)
            f_item.setData(0, ROLE_LOCKED_FOLDER, True)
            f_item.setData(0, ROLE_COUNT, len(s_notes))
            f_item.setIcon(0, make_vector_folder_icon(p.get("accent", "#6366F1"), 16))
            f_item.setFont(0, f_top)
            f_item.setForeground(0, QBrush(QColor(p["title"])))
            f_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            for item in s_notes:
                leaf = QTreeWidgetItem([(item.get("title") or "Nota")[:32]])
                leaf.setData(0, Qt.ItemDataRole.UserRole, ("group_note", gid, item))
                leaf.setData(0, ROLE_NODE_DATA, ("group_note", gid, item))
                leaf.setData(0, ROLE_IS_FOLDER, False)
                leaf.setIcon(0, get_icon("file", theme_key=self._current_theme, role="subtitle", size=14))
                leaf.setForeground(0, QBrush(QColor(p["text"])))

                lock_d = item.get("lock_info")
                if lock_d:
                    auth = lock_d.get("author") or item.get("lock_author") or "Studente"
                    utc_ts = lock_d.get("locked_at_utc") or ""
                    if item.get("locked_by_other"):
                        leaf.setData(0, ROLE_STATUS_ICON, get_icon("lock", theme_key=self._current_theme, role="warning", size=14))
                        leaf.setToolTip(0, f"Bloccato da {auth} ({utc_ts})")
                    else:
                        leaf.setData(0, ROLE_STATUS_ICON, get_icon("edit", theme_key=self._current_theme, role="success", size=14))
                        leaf.setToolTip(0, f"In modifica da te ({auth} · {utc_ts})")
                else:
                    leaf.setToolTip(0, f"{item.get('title')} ({subj})")

                leaf.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                f_item.addChild(leaf)

            self.nk_tree.addTopLevelItem(f_item)
            f_item.setExpanded(True)

        self.nk_tree.blockSignals(False)

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
        if data[0] in ("note", "group_board", "group_note", "todos"):
            # L'apertura della nota/bacheca/vista To-Do avviene su click sinistro rilasciato (nodeLeftClicked),
            # mai su currentItemChanged, così tasto destro e drag & drop non la aprono per errore.
            return
        if data[0] in ("all", "recent"):
            self._nk_filter_favorites_only = False
            self._nk_current_filter_folder_id = None
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif data[0] == "favorites":
            self._nk_filter_favorites_only = True
            self._nk_current_filter_folder_id = None
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif data[0] == "folder":
            self._nk_filter_favorites_only = False
            self._nk_current_filter_folder_id = data[1]
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
        elif data[0] == "filter":
            self._nk_filter_favorites_only = False
            self._nk_current_filter_folder_id = None
            _, subj, topic = data
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
            search_query=sq,
            folder_id=getattr(self, "_nk_current_filter_folder_id", None),
        )
        if getattr(self, "_nk_filter_favorites_only", False):
            notes = [n for n in notes if bool(n.get("is_favorite"))]

        self.nk_notes_list.clear()
        if not notes:
            self.nk_notes_list.show_empty(
                "Nessuna nota nei Preferiti" if getattr(self, "_nk_filter_favorites_only", False) else "Cartella vuota",
                icon_name="star" if getattr(self, "_nk_filter_favorites_only", False) else "notes",
                subtitle="Clicca la stella su una nota per aggiungerla ai Preferiti." if getattr(self, "_nk_filter_favorites_only", False) else "Crea una nuova nota o sbobina una lezione.",
                shortcut_hint="Ctrl+N",
                theme_key=self._current_theme,
            )
            return

        for n in notes:
            card = self._make_nk_note_card(n)
            self.nk_notes_list.add_card(card)

        # Caricamento iniziale intelligente (configurabile da Impostazioni: ultima nota visualizzata / ripristina schede / più recente / vuota)
        if not getattr(self, "_nk_initial_note_loaded", False) and getattr(self, "_nk_selected_note_id", None) is None:
            self._nk_initial_note_loaded = True
            self._restore_initial_note_if_needed(notes)

    def _restore_initial_note_if_needed(self, notes: List[Dict[str, Any]]):
        """Apre all'avvio la nota/sessione configurata dall'utente in Impostazioni."""
        mode = (db.get_setting("startup_note_mode", "last_viewed") or "last_viewed").strip().lower()
        if mode == "empty":
            self._active_nk_pane().open_new_empty_tab(
                subject="Generale",
                topic="Generale",
                profile=self._nk_author_name(),
            )
            return

        if mode == "restore_tabs":
            raw_sess = db.get_setting("flownotes_open_session", "") or ""
            if raw_sess:
                try:
                    sess = json.loads(raw_sess)
                    left_ids = [int(i) for i in (sess.get("left_ids") or []) if db.get_study_note_by_id(int(i))]
                    right_ids = [int(i) for i in (sess.get("right_ids") or []) if db.get_study_note_by_id(int(i))]
                    if left_ids:
                        self._restoring_nk_session = True
                        try:
                            for idx_n, nid in enumerate(left_ids):
                                self._load_note_into_nk_editor(nid, pane=self.nk_pane_left, in_new_tab=(idx_n > 0))
                            l_act = int(sess.get("left_active", 0))
                            if 0 <= l_act < len(self.nk_pane_left.tabs):
                                self.nk_pane_left.switch_to_tab(l_act)
                            if sess.get("split_open") and right_ids:
                                self.nk_pane_right.show()
                                for idx_r, nid in enumerate(right_ids):
                                    self._load_note_into_nk_editor(nid, pane=self.nk_pane_right, in_new_tab=(idx_r > 0))
                                r_act = int(sess.get("right_active", 0))
                                if 0 <= r_act < len(self.nk_pane_right.tabs):
                                    self.nk_pane_right.switch_to_tab(r_act)
                        finally:
                            self._restoring_nk_session = False
                        self._sync_toolbar_from_active_pane()
                        return
                except Exception:
                    self._restoring_nk_session = False

        if mode in ("last_viewed", "restore_tabs"):
            raw_last = (db.get_setting("last_viewed_note_id", "") or "").strip()
            if raw_last.isdigit():
                last_note = db.get_study_note_by_id(int(raw_last))
                if last_note and not int(last_note.get("trashed") or 0):
                    self._load_note_into_nk_editor(int(raw_last))
                    return

        if notes:
            self._load_note_into_nk_editor(notes[0]["id"])

    # -----------------------------------------------------------------
    # Gestione Cartelle Gerarchiche (Samsung Notes Style)
    # -----------------------------------------------------------------
    def _on_nk_create_subfolder(self, parent_id: int):
        name, ok = QInputDialog.getText(self, "Nuova Sottocartella", "Nome sottocartella:")
        if not ok or not name.strip():
            return
        prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
        if prof == "Tutti i profili":
            prof = "Alessandro"
        db.create_folder(name=name.strip(), parent_id=parent_id, profile=prof)
        self._refresh_notekeeper()

    def _on_nk_rename_folder(self, folder_id: int):
        folders = {f["id"]: f for f in db.get_folders_tree()}
        cur_name = folders.get(folder_id, {}).get("name", "")
        new_name, ok = QInputDialog.getText(self, "Rinomina Cartella", "Nuovo nome:", text=cur_name)
        if not ok or not new_name.strip():
            return
        db.rename_folder(folder_id, new_name.strip())
        self._refresh_notekeeper()

    def _on_nk_change_folder_color(self, folder_id: int, color_hex: str):
        db.set_folder_color(folder_id, color_hex)
        self._refresh_notekeeper()

    def _on_nk_delete_folder(self, folder_id: int):
        notes_in_folder = db.get_notes_by_hierarchy(folder_id=folder_id)
        if notes_in_folder:
            box = QMessageBox(self)
            box.setWindowTitle("Elimina Cartella")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(f"La cartella contiene {len(notes_in_folder)} note (incluse eventuali sottocartelle).")
            box.setInformativeText("Vuoi spostare le note nella cartella Generale prima di eliminare?")
            btn_move = box.addButton("Sposta in Generale", QMessageBox.ButtonRole.AcceptRole)
            btn_del_all = box.addButton("Elimina tutto", QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = box.addButton("Annulla", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_cancel or clicked is None:
                return
            move_to_gen = (clicked is btn_move)
        else:
            res = QMessageBox.question(
                self,
                "Elimina Cartella",
                "Eliminare questa cartella vuota?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                return
            move_to_gen = True

        if getattr(self, "_nk_current_filter_folder_id", None) == folder_id:
            self._nk_current_filter_folder_id = None
        db.delete_folder(folder_id, move_notes_to_generale=move_to_gen)
        self._refresh_notekeeper()

    def _on_nk_note_moved(self, note_id: int, target_folder_id: int):
        if db.move_note_to_folder(note_id, target_folder_id):
            self._refresh_notekeeper()

    def _on_nk_folder_moved(self, folder_id: int, target_parent_id: object):
        parent_val = int(target_parent_id) if target_parent_id is not None else None
        if db.move_folder(folder_id, parent_val):
            self._refresh_notekeeper()

    def _on_nk_toggle_studied(self, note_id: int):
        db.toggle_note_studied(note_id)
        self._refresh_notekeeper()

    def _on_nk_toggle_favorite(self, note_id: int):
        new_state = db.toggle_note_favorite(int(note_id))
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is None:
                continue
            for t in pane.tabs:
                if t.workspace_type == "local" and t.note_id == int(note_id):
                    t.is_favorite = new_state
        self._sync_toolbar_from_active_pane()
        self._refresh_notekeeper()

    def _on_nk_toggle_favorite_active(self):
        pane = self._active_nk_pane()
        tab = pane.active_tab()
        if tab is None or tab.workspace_type != "local" or tab.note_id is None:
            return
        self._on_nk_toggle_favorite(int(tab.note_id))

    def _on_nk_trash_note(self, note_id: int):
        nid = int(note_id)
        note = db.get_study_note_by_id(nid)
        if not note:
            return
        db.trash_study_note(nid)
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is not None:
                pane.close_tabs_for_note_id(nid)
        if getattr(self, "_nk_selected_note_id", None) == nid:
            self._nk_selected_note_id = None
        self._refresh_notekeeper()
        self.save_workspace_session_state()
        title_lbl = (note.get("title") or "Nota").strip()
        self.show_toast(
            f"'{title_lbl}' spostata nel Cestino.",
            title="Cestino",
            level="info",
            action_label="Annulla",
            on_action=lambda target_id=nid: self._on_nk_restore_note(target_id),
        )

    def _on_nk_restore_note(self, note_id: int):
        nid = int(note_id)
        note = db.get_study_note_by_id(nid)
        if not note:
            return
        db.restore_study_note(nid)
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is None:
                continue
            for idx, tab in enumerate(pane.tabs):
                if tab.workspace_type == "local" and tab.note_id is not None and int(tab.note_id) == nid:
                    tab.is_trashed = False
                    pane.tab_bar_widget.update_tab_title(idx, tab)
                    if idx == pane.current_tab_index:
                        pane._render_tab(tab)
        self._refresh_notekeeper()
        self._sync_toolbar_from_active_pane()
        self.save_workspace_session_state()
        title_lbl = (note.get("title") or "Nota").strip()
        self.show_toast(f"'{title_lbl}' ripristinata dal Cestino.", title="Nota ripristinata", level="success")

    def _on_nk_permanent_delete_note(self, note_id: int):
        nid = int(note_id)
        note = db.get_study_note_by_id(nid)
        title_lbl = (note.get("title") or "questa nota").strip() if note else "questa nota"
        reply = QMessageBox.question(
            self,
            "Elimina definitivamente",
            f"Vuoi eliminare definitivamente '{title_lbl}' e tutte le eventuali flashcard e relazioni associate?\n"
            "Questa operazione non può essere annullata.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is not None:
                pane.close_tabs_for_note_id(nid)
        db.delete_study_note(nid)
        if getattr(self, "_nk_selected_note_id", None) == nid:
            self._nk_selected_note_id = None
        self._refresh_notekeeper()
        self.save_workspace_session_state()
        self.show_toast(f"'{title_lbl}' eliminata definitivamente.", title="Cestino", level="warning")

    def _on_nk_empty_trash(self):
        trashed = db.get_trashed_study_notes()
        if not trashed:
            return
        reply = QMessageBox.question(
            self,
            "Svuota Cestino",
            f"Eliminare definitivamente tutte le {len(trashed)} note presenti nel Cestino?\n"
            "Questa operazione non può essere annullata.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for tn in trashed:
            nid = int(tn["id"])
            for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
                if pane is not None:
                    pane.close_tabs_for_note_id(nid)
            if getattr(self, "_nk_selected_note_id", None) == nid:
                self._nk_selected_note_id = None
        removed = db.empty_trash()
        self._refresh_notekeeper()
        self.save_workspace_session_state()
        self.show_toast(f"Cestino svuotato ({removed} note eliminate).", title="Cestino", level="warning")

    def _open_note_by_wikilink_title(self, title: str):
        """Apre la nota locale corrispondente a un [[wikilink]] (o ne propone la creazione)."""
        clean = (title or "").strip()
        if not clean:
            return
        matches = db.search_note_titles_for_wikilink(clean, limit=10)
        chosen = None
        for m in matches:
            t_norm = (m.get("title") or "").strip().lower()
            if t_norm == clean.lower() or t_norm == f"nota: {clean.lower()}":
                chosen = m
                break
        if chosen is None and matches:
            chosen = matches[0]
        if chosen is not None:
            self._load_note_into_nk_editor(int(chosen["id"]))
            return
        ans = QMessageBox.question(
            self,
            "Crea nota collegata",
            f"Nessuna nota trovata per [[{clean}]].\nVuoi creare subito una nuova nota con questo titolo?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
            if prof == "Tutti i profili":
                prof = "Alessandro"
            nid = db.add_study_note(
                title=clean,
                content_markdown="",
                note_type="appunto",
                profile=prof,
                subject=self._nk_current_filter_subject or "Generale",
                topic=self._nk_current_filter_topic or "Generale",
                folder_id=getattr(self, "_nk_current_filter_folder_id", None),
            )
            self._refresh_notekeeper()
            self._load_note_into_nk_editor(int(nid))

    def _reload_open_tabs_for_note(self, note_id: int):
        """Aggiorna il contenuto nei pane aperti se una nota è stata modificata esternamente (es. toggle To-Do)."""
        note = db.get_study_note_by_id(int(note_id))
        if not note:
            return
        new_md = note.get("content_markdown") or ""
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is None:
                continue
            for idx, tab in enumerate(pane.tabs):
                if tab.workspace_type == "local" and tab.note_id == int(note_id):
                    tab.content_markdown = new_md
                    tab.is_dirty = False
                    if idx == pane.current_tab_index:
                        pane.editor_body.set_markdown_text(new_md)

    def _jump_to_note_line(self, note_id: int, line_index: int):
        """Apre la nota nell'editor attivo e posiziona il cursore sulla riga del To-Do."""
        self._load_note_into_nk_editor(int(note_id))
        pane = self._active_nk_pane()
        if pane is not None and hasattr(pane, "editor_body"):
            doc = pane.editor_body.document()
            blk = doc.findBlockByNumber(max(0, int(line_index)))
            if blk.isValid():
                cur = QTextCursor(blk)
                pane.editor_body.setTextCursor(cur)
                pane.editor_body.ensureCursorVisible()
                pane.editor_body.setFocus()

    def _open_all_todos_dialog(self):
        """Apre la vista aggregata di tutti i To-Do presenti nelle note locali."""
        pal = self._active_palette()
        dlg_bg = pal.get("dialog_bg") or pal.get("root_bg") or "#0B0E16"
        text_col = pal.get("text") or "#E2E8F0"
        title_col = pal.get("title") or text_col
        sub_col = pal.get("subtitle") or "#94A3B8"
        card_bg = pal.get("card_bg") or "rgba(255, 255, 255, 0.03)"
        card_border = pal.get("card_border") or "rgba(255, 255, 255, 0.08)"

        dlg = QDialog(self)
        dlg.setWindowTitle("Tutti i To-Do delle Note")
        dlg.resize(740, 560)
        dlg.setStyleSheet(
            f"QDialog {{ background-color: {dlg_bg}; color: {text_col}; }}"
            f"QLabel {{ color: {text_col}; }}"
        )

        root = QVBoxLayout(dlg)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        title_lbl = QLabel("✅ Tutti i To-Do delle Note")
        title_lbl.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {title_col};")
        hdr.addWidget(title_lbl)
        hdr.addStretch(1)
        count_lbl = QLabel("")
        count_lbl.setStyleSheet(f"font-size: 12px; color: {sub_col};")
        hdr.addWidget(count_lbl)
        root.addLayout(hdr)

        # Barra ricerca + filtro (SEMPRE fuori dalla QScrollArea — AGENTS.md Regola 7)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        search_edit = QLineEdit()
        search_edit.setPlaceholderText("🔍 Filtra attività, nota o cartella...")
        search_edit.setMinimumHeight(36)
        filter_row.addWidget(search_edit, 1)

        show_done_cb = QCheckBox("Mostra anche completati")
        show_done_cb.setChecked(False)
        filter_row.addWidget(show_done_cb)
        root.addLayout(filter_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        cards_lay = QVBoxLayout(container)
        cards_lay.setContentsMargins(0, 4, 4, 4)
        cards_lay.setSpacing(8)
        cards_lay.addStretch(1)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        prof_text = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Tutti i profili"
        prof_filter = None if prof_text == "Tutti i profili" else prof_text

        def _rebuild_list():
            while cards_lay.count() > 1:
                it = cards_lay.takeAt(0)
                if it.widget() is not None:
                    it.widget().deleteLater()

            include_done = show_done_cb.isChecked()
            q = search_edit.text().strip().lower()
            items = db.get_all_note_todos(include_completed=include_done, profile=prof_filter)
            if q:
                items = [
                    t for t in items
                    if q in (t.get("text") or "").lower()
                    or q in (t.get("note_title") or "").lower()
                    or q in (t.get("folder_name") or "").lower()
                    or q in (t.get("subject") or "").lower()
                ]
            open_cnt = sum(1 for t in items if not t.get("checked"))
            count_lbl.setText(f"{open_cnt} aperti · {len(items)} mostrati")

            if not items:
                empty_w = _make_empty_state(
                    "check_square",
                    "Nessun To-Do con i filtri attuali",
                    "Scrivi [] seguito da spazio in qualsiasi nota per creare una checklist interattiva.",
                    shortcut_hint="[] + Spazio",
                    theme_key=self._current_theme,
                )
                cards_lay.insertWidget(0, empty_w)
                return

            for td in items:
                card = QFrame()
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                card.setStyleSheet(
                    f"QFrame {{ background-color: {card_bg}; border: 1px solid {card_border}; "
                    f"border-radius: 10px; }}"
                )
                c_lay = QHBoxLayout(card)
                c_lay.setContentsMargins(12, 9, 12, 9)
                c_lay.setSpacing(10)

                cb = QCheckBox()
                cb.setChecked(bool(td.get("checked")))
                cb.setCursor(Qt.CursorShape.PointingHandCursor)

                def _on_toggle(state, nid=td["note_id"], lidx=td["line_index"]):
                    ok = db.toggle_note_todo_line(int(nid), int(lidx))
                    if ok:
                        self._reload_open_tabs_for_note(int(nid))
                        self._refresh_notekeeper()
                        _rebuild_list()

                cb.stateChanged.connect(_on_toggle)
                c_lay.addWidget(cb, 0, Qt.AlignmentFlag.AlignTop)

                txt_col = QVBoxLayout()
                txt_col.setContentsMargins(0, 0, 0, 0)
                txt_col.setSpacing(3)

                indent_prefix = "↳ " * min(int(td.get("indent") or 0), 3)
                task_lbl = QLabel(f"{indent_prefix}{td.get('text') or ''}")
                task_lbl.setWordWrap(True)
                if td.get("checked"):
                    task_lbl.setStyleSheet(
                        f"color: {sub_col}; font-size: 13px; text-decoration: line-through; border: none;"
                    )
                else:
                    task_lbl.setStyleSheet(
                        f"color: {text_col}; font-size: 13px; font-weight: 500; border: none;"
                    )
                txt_col.addWidget(task_lbl)

                meta_lbl = QLabel(f"📄 {td.get('note_title')}  ·  📁 {td.get('folder_name') or td.get('subject') or 'Generale'}")
                meta_lbl.setWordWrap(True)
                meta_lbl.setStyleSheet(f"color: {sub_col}; font-size: 11px; border: none;")
                txt_col.addWidget(meta_lbl)
                c_lay.addLayout(txt_col, 1)

                open_btn = _make_button("Apri →", "ghost")
                open_btn.setMinimumHeight(30)
                open_btn.clicked.connect(
                    lambda _c=False, nid=td["note_id"], lidx=td["line_index"]: (
                        dlg.accept(),
                        self._jump_to_note_line(int(nid), int(lidx)),
                    )
                )
                c_lay.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignVCenter)
                cards_lay.insertWidget(cards_lay.count() - 1, card)

        search_edit.textChanged.connect(lambda _t: _rebuild_list())
        show_done_cb.stateChanged.connect(lambda _s: _rebuild_list())
        _rebuild_list()

        foot = QHBoxLayout()
        foot.addStretch(1)
        close_btn = _make_button("Chiudi", "primary")
        close_btn.clicked.connect(dlg.accept)
        foot.addWidget(close_btn)
        root.addLayout(foot)

        dlg.exec()

    # -----------------------------------------------------------------
    # Command Palette Globale (Ctrl+K)
    # -----------------------------------------------------------------
    def _open_command_palette(self):
        callbacks = {
            "open_note": self._cp_open_note,
            "open_folder": self._cp_open_folder,
            "open_event": self._cp_open_event,
            "use_dictation": self._cp_use_dictation,
            "new_note": self._cp_new_note,
            "new_sbobina": self._cp_new_sbobina,
            "toggle_reading": self._cp_toggle_reading,
            "open_whats_new": self._open_whats_new_dialog,
            "open_diagnostics": self._open_diagnostics_dialog,
            "open_onboarding": self._open_onboarding_wizard,
            "set_theme": self._set_theme,
            "navigate": self._on_nav_clicked,
        }
        dlg = GlobalCommandPalette(
            parent=self,
            palette=self._active_palette(),
            themes_map=_THEME_PALETTES,
            callbacks=callbacks,
            reading_mode_active=bool(getattr(self, "_reading_mode", False)),
        )
        dlg.exec()

    def _cp_open_note(self, note_id: Optional[int], match_line: int = 0, query: str = ""):
        if not note_id:
            return
        self._on_nav_clicked(1)
        self._load_note_into_nk_editor(note_id)
        if hasattr(self, "nk_editor_body") and match_line > 0:
            doc = self.nk_editor_body.document()
            block = doc.findBlockByNumber(match_line)
            if block.isValid():
                cur = QTextCursor(block)
                self.nk_editor_body.setTextCursor(cur)
                self.nk_editor_body.ensureCursorVisible()
            self.nk_editor_body.setFocus()

    def _cp_open_folder(self, folder_id: Optional[int]):
        self._on_nav_clicked(1)
        if folder_id is not None:
            self._nk_current_filter_folder_id = folder_id
            self._nk_current_filter_subject = None
            self._nk_current_filter_topic = None
            self._refresh_notekeeper()

    def _cp_open_event(self, event_date: str):
        self._on_nav_clicked(7)
        if event_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(event_date[:10], "yyyy-MM-dd")
            if qd.isValid():
                self.ft_calendar.setSelectedDate(qd)
                if hasattr(self, "_refresh_flowtime_page"):
                    self._refresh_flowtime_page()

    def _cp_use_dictation(self, text: str):
        if not text:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        if (
            hasattr(self, "pages")
            and self.pages.currentIndex() == 1
            and hasattr(self, "nk_editor_body")
        ):
            self.nk_editor_body.insertPlainText(text)
            self.nk_editor_body.setFocus()

    def _cp_new_note(self):
        self._on_nav_clicked(1)
        if hasattr(self, "_on_nk_new_note_clicked"):
            self._on_nk_new_note_clicked()

    def _cp_new_sbobina(self):
        self._on_nav_clicked(1)
        if hasattr(self, "_on_sbobina_clicked"):
            self._on_sbobina_clicked()

    def _cp_toggle_reading(self):
        new_state = not bool(getattr(self, "_reading_mode", False))
        if hasattr(self, "reading_mode_btn"):
            self.reading_mode_btn.setChecked(new_state)
        self._toggle_reading_mode(new_state)

    def _make_nk_note_card(self, note: Dict) -> QFrame:
        card = QFrame()
        card.setObjectName("NKNoteCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        note_id = note.get("id")
        is_selected = (note_id == getattr(self, "_nk_selected_note_id", None))
        card.setProperty("selected", is_selected)
        card.setProperty("note_id", note_id)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # 1. Rimuovi il prefisso automatico "Nota: " e tag Markdown dal titolo visualizzato
        raw_title = (note.get("title") or "Senza titolo").strip()
        if raw_title.lower().startswith("nota: "):
            raw_title = raw_title[6:].strip() or "Senza titolo"
        raw_title = strip_markdown_syntax(raw_title) or "Senza titolo"
        if bool(note.get("is_favorite")):
            raw_title = f"★ {raw_title}"

        t_lbl = QLabel(raw_title)
        t_lbl.setObjectName("NKNoteTitle")
        t_lbl.setWordWrap(True)
        t_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.addWidget(t_lbl)

        # 2. Non ripetere il corpo se è uguale al titolo o più corto di 5 caratteri
        #    (ignora puntini di sospensione '...', punteggiatura finale e case)
        content = (note.get("content_markdown") or "").strip()
        norm_title = re.sub(r'[.…\s]+$', '', raw_title).strip().lower()
        norm_body = re.sub(r'[.…\s]+$', '', content).strip().lower()
        cleaned_body = content
        if norm_title and norm_body.startswith(norm_title):
            if len(content) <= len(raw_title) + 15:
                cleaned_body = ""
            else:
                cleaned_body = content[len(norm_title):].lstrip(" \t\r\n:.-—–…")
        if cleaned_body and len(cleaned_body) >= 5 and norm_body != norm_title:
            preview = (cleaned_body[:95].replace('\n', ' ') + "...") if len(cleaned_body) > 95 else cleaned_body.replace('\n', ' ')
            p_lbl = QLabel(preview)
            p_lbl.setObjectName("NKNotePreview")
            p_lbl.setWordWrap(True)
            p_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            layout.addWidget(p_lbl)

        # 3. Metadati minimali su una sola riga discreta (dot colorato + tipo + eventuale profilo + ora)
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.setSpacing(5)

        nt = (note.get("note_type") or "appunto").lower()
        if nt == "sbobina":
            dot_color, type_name = "#10B981", "Sbobina"
        elif nt == "slide":
            dot_color, type_name = "#0EA5E9", "Slide"
        else:
            dot_color, type_name = "#6366F1", "Appunto"

        dot_lbl = QLabel("●")
        dot_lbl.setStyleSheet(f"color: {dot_color}; font-size: 9px; background: transparent;")
        meta_row.addWidget(dot_lbl)

        show_all_profiles = (
            hasattr(self, "nk_profile_combo")
            and self.nk_profile_combo.currentText().strip() == "Tutti i profili"
        )
        type_text = f"{type_name} · {note.get('profile') or 'Alessandro'}" if show_all_profiles else type_name
        type_lbl = QLabel(type_text)
        type_lbl.setObjectName("NKNoteMeta")
        meta_row.addWidget(type_lbl)
        meta_row.addStretch(1)

        dt = _format_timestamp(note.get("created_at"))
        dt_lbl = QLabel(dt)
        dt_lbl.setObjectName("NKNoteMeta")
        meta_row.addWidget(dt_lbl)
        layout.addLayout(meta_row)

        card.setCursor(Qt.CursorShape.PointingHandCursor)

        def _on_card_press(ev, nid=note_id):
            if ev.button() == Qt.MouseButton.MiddleButton:
                self._load_note_into_nk_editor(nid, in_new_tab=True)
                return
            if ev.button() == Qt.MouseButton.RightButton:
                menu = QMenu(card)
                act_tab = menu.addAction(get_icon("plus", theme_key=self._current_theme, size=14), "Apri in nuova scheda")
                act_split = menu.addAction(get_icon("split_h", theme_key=self._current_theme, size=14), "Affianca a destra (Split View)")
                chosen = menu.exec(card.mapToGlobal(ev.pos()))
                if chosen == act_tab:
                    self._load_note_into_nk_editor(nid, in_new_tab=True)
                elif chosen == act_split:
                    self._open_nk_tree_node(("note", nid), in_split=True)
                return
            self._load_note_into_nk_editor(nid)

        card.mousePressEvent = _on_card_press
        return card

    def _load_note_into_nk_editor(
        self,
        note_id: int,
        pane: Optional[EditorPane] = None,
        in_new_tab: bool = False,
    ):
        note = db.get_study_note_by_id(note_id)
        if not note:
            return
        target_pane = pane or self._active_nk_pane()
        self._nk_selected_note_id = note_id
        # Aggiorna stato attivo su tutte le card nella lista centrale
        if hasattr(self, "nk_notes_list"):
            for i in range(self.nk_notes_list.vbox.count()):
                w = self.nk_notes_list.vbox.itemAt(i).widget()
                if w is not None and w.objectName() == "NKNoteCard":
                    is_sel = (w.property("note_id") == note_id)
                    if bool(w.property("selected")) != is_sel:
                        w.setProperty("selected", is_sel)
                        w.style().unpolish(w)
                        w.style().polish(w)

        title_txt = (note.get("title") or "").strip()
        if title_txt.lower().startswith("nota: "):
            title_txt = title_txt[6:].strip()
        ctx = TabContext(
            workspace_type="local",
            note_id=int(note_id),
            subject=note.get("subject") or "Generale",
            topic=note.get("topic") or "Generale",
            note_type=(note.get("note_type") or "appunto").lower(),
            profile=note.get("profile") or "Alessandro",
            title=title_txt or "Appunto senza titolo",
            content_markdown=note.get("content_markdown") or "",
            is_dirty=False,
            is_favorite=bool(note.get("is_favorite")),
            is_trashed=bool(note.get("trashed")),
            auto_title_done=bool(title_txt and title_txt.lower() not in ("nuova nota", "appunto senza titolo", "senza titolo")),
            created_by_name=note.get("created_by_name") or "",
            created_by_email=note.get("created_by_email") or "",
            created_by_avatar=note.get("created_by_avatar") or "",
            last_edited_by_name=note.get("last_edited_by_name") or "",
            last_edited_by_email=note.get("last_edited_by_email") or "",
            last_edited_at_utc=note.get("last_edited_at_utc") or "",
        )
        target_pane.open_tab(ctx, in_new_tab=in_new_tab)
        if not getattr(self, "_restoring_nk_session", False):
            try:
                db.set_setting("last_viewed_note_id", str(int(note_id)))
            except Exception:
                pass
            self.save_workspace_session_state()
        self._sync_toolbar_from_active_pane()

    def _set_nk_autosave_badge(self, saved: bool, custom_text: Optional[str] = None):
        if hasattr(self, "nk_pane_left"):
            self._active_nk_pane().set_autosave_badge(saved, custom_text)

    def _on_nk_editor_modified(self, *_args):
        if hasattr(self, "nk_pane_left"):
            self._active_nk_pane()._on_editor_modified(*_args)

    def _perform_nk_autosave(self):
        if hasattr(self, "nk_pane_left"):
            self._active_nk_pane()._perform_autosave()

    def _on_nk_autosave_finished(self, saved_id: int, title: str, content: str, subj: str, top: str, ntype: str):
        was_new = (getattr(self, "_nk_selected_note_id", None) is None)
        self._nk_selected_note_id = saved_id
        self._set_nk_autosave_badge(saved=True)
        now_ts = time.time()
        if getattr(self, "_nk_dirty_for_snapshot", False) and (now_ts - getattr(self, "_nk_last_snapshot_ts", 0.0) >= 600.0):
            try:
                db.add_note_snapshot(saved_id, title, content, "Salvataggio automatico")
                self._nk_dirty_for_snapshot = False
                self._nk_last_snapshot_ts = now_ts
            except Exception as exc:
                logger.warning(f"Time Machine auto-snapshot fallito: {exc}")

        # Aggiorna la card nella lista centrale senza mai toccare il cursore/scroll dell'editor
        updated_in_place = False
        if not was_new and hasattr(self, "nk_notes_list"):
            for i in range(self.nk_notes_list.vbox.count()):
                w = self.nk_notes_list.vbox.itemAt(i).widget()
                if w is not None and w.objectName() == "NKNoteCard" and w.property("note_id") == saved_id:
                    for lbl in w.findChildren(QLabel):
                        if lbl.objectName() == "NKNoteTitle":
                            lbl.setText(title)
                            updated_in_place = True
                            break
                    break
        if was_new or not updated_in_place:
            self._refresh_notekeeper()
        for pane in (getattr(self, "nk_pane_left", None), getattr(self, "nk_pane_right", None)):
            if pane is not None and hasattr(pane, "refresh_backlinks"):
                tab = pane.active_tab()
                if tab is not None and tab.workspace_type == "local" and tab.note_id is not None:
                    pane.refresh_backlinks(tab.note_id, tab.title)

    def _on_nk_studio_btn_clicked(self, checked: bool):
        if hasattr(self, "nk_editor_body"):
            self.nk_editor_body.set_studio_view(checked)

    def _on_nk_studio_shortcut(self):
        if hasattr(self, "nk_editor_body"):
            self.nk_editor_body.toggle_studio_view()

    def _on_nk_studio_view_changed(self, is_studio: bool):
        if hasattr(self, "nk_studio_btn"):
            self.nk_studio_btn.blockSignals(True)
            self.nk_studio_btn.setChecked(is_studio)
            self.nk_studio_btn.setText(" Modifica" if is_studio else " Vista Studio")
            self.nk_studio_btn.blockSignals(False)

    def _on_nk_rsvp_btn_clicked(self):
        if not hasattr(self, "nk_editor_body"):
            return
        cur = self.nk_editor_body.textCursor()
        has_sel = cur.hasSelection() and bool(cur.selectedText().strip())
        self._on_nk_rsvp_requested(start_block_idx=0, selection_only=has_sel)

    def _show_nk_more_menu(self):
        pal = self._active_palette()
        is_dark = self._active_is_dark()
        bg = pal.get("input_bg") or ("#1A1F29" if is_dark else "#FFFFFF")
        fg = pal.get("text") or ("#F8FAFC" if is_dark else "#0F172A")
        bd = pal.get("card_border") or ("#2D3545" if is_dark else "#CBD5E1")
        hv = pal.get("btn_hover_bg") or ("#253042" if is_dark else "#F1F5F9")

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; padding: 6px; border-radius: 10px; }}"
            f"QMenu::item {{ padding: 7px 14px; border-radius: 6px; font-size: 12.5px; }}"
            f"QMenu::item:selected {{ background-color: {hv}; }}"
            f"QMenu::separator {{ height: 1px; background: {bd}; margin: 4px 8px; }}"
        )

        # Sottomenu Esporta (opzioni di export esistenti)
        export_sub = menu.addMenu(get_icon("download", theme_key=self._current_theme, role="subtitle", size=14), "Esporta")
        export_sub.setStyleSheet(menu.styleSheet())
        act_md = export_sub.addAction("📄 Markdown (.md)")
        act_tex = export_sub.addAction("📄 Documento LaTeX (.tex)")
        act_docx = export_sub.addAction("📘 Documento Word (.docx)")
        act_pdf = export_sub.addAction("📕 Documento PDF (.pdf)")

        act_marker = menu.addAction(get_icon("clock", theme_key=self._current_theme, role="subtitle", size=14), "Inserisci marcatore audio [HH:MM:SS]")
        act_dup = menu.addAction(get_icon("copy", theme_key=self._current_theme, role="subtitle", size=14), "Duplica nota")
        menu.addSeparator()

        act_del = menu.addAction(get_icon("trash", theme_key=self._current_theme, role="danger", size=14), "Elimina nota")
        # Colore rosso danger per l'azione Elimina nel menu
        del_font = act_del.font()
        del_font.setWeight(QFont.Weight.DemiBold)
        act_del.setFont(del_font)

        chosen = menu.exec(self.nk_more_btn.mapToGlobal(self.nk_more_btn.rect().bottomLeft()))
        if not chosen:
            return
        if chosen in (act_md, act_tex, act_docx, act_pdf):
            fmt_map = {act_md: "md", act_tex: "tex", act_docx: "docx", act_pdf: "pdf"}
            self._export_notes_by_format(fmt_map[chosen])
        elif chosen == act_marker:
            self._on_nk_insert_timestamp_marker()
        elif chosen == act_dup:
            self._on_nk_duplicate_clicked()
        elif chosen == act_del:
            self._on_nk_delete_clicked()

    def _on_nk_duplicate_clicked(self):
        if not hasattr(self, "nk_editor_body"):
            return
        raw_title = self.nk_editor_title.text().strip() or "Appunto senza titolo"
        content = self.nk_editor_body.get_markdown_text()
        prof = self.nk_editor_profile.currentText().strip() or "Alessandro"
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        ntype = self.nk_editor_type.currentText().strip() or "appunto"
        new_title = f"{raw_title} (Copia)"
        try:
            new_id = db.add_study_note(new_title, content, ntype, "", None, prof, subj, top)
            self._nk_selected_note_id = new_id
            self._refresh_study_notes_list(select_note_id=new_id)
            self._set_nk_autosave_badge(saved=True, custom_text="✓ Nota duplicata")
        except Exception as exc:
            QMessageBox.warning(self, "Duplica nota", f"Impossibile duplicare la nota:\n{exc}")

    def _on_nk_rsvp_requested(self, start_block_idx: int = 0, selection_only: bool = False):
        if not hasattr(self, "nk_editor_body"):
            return
        md_slice, base_block = self.nk_editor_body.get_rsvp_markdown_slice(
            start_block_idx=start_block_idx,
            selection_only=selection_only,
        )
        tokens = tokenize_rsvp_markdown(md_slice, start_block_idx=base_block)
        if not tokens:
            self._set_nk_autosave_badge(saved=True, custom_text="⚡ RSVP: nessun testo da leggere")
            return
        pal = self._active_palette()
        accent_hex = pal.get("accent") or pal.get("nav_checked_border") or "#818CF8"
        font_family = getattr(self, "_font_family", "Segoe UI")
        if "," in font_family:
            font_family = font_family.split(",")[0].strip().strip("'").strip('"')

        modal = RsvpReaderModal(
            tokens=tokens,
            parent=self,
            accent_hex=accent_hex,
            font_family=font_family,
            on_close_block=self.nk_editor_body.focus_block,
            palette=pal,
            is_dark=self._active_is_dark(),
        )
        self._rsvp_modal = modal
        modal.open_animated()

    def _on_nk_summarize_clicked(self):
        text = self.nk_editor_body.get_markdown_text().strip()
        if not text:
            QMessageBox.information(self, "Riassumi AI", "Scrivi prima del testo nella nota per generare il riassunto.")
            return
        self._save_nk_snapshot_now("Prima di raffinamento AI")
        self._set_nk_autosave_badge(saved=False, custom_text="✨ Riassunto AI in corso...")
        self._summarize_thread = _SummarizeNoteThread(text)
        self._summarize_thread.finished.connect(self._on_nk_summarize_finished)
        self._summarize_thread.error.connect(self._on_nk_summarize_error)
        self._summarize_thread.start()

    def _on_nk_summarize_finished(self, summary: str):
        quoted_lines = "\n".join(f"> {line}" if line.strip() else ">" for line in summary.splitlines())
        box = f"\n\n> 📌 **Riassunto AI**\n>\n{quoted_lines}\n"
        cur_md = self.nk_editor_body.get_markdown_text().rstrip()
        self.nk_editor_body.set_markdown_text(cur_md + box)
        self._on_nk_editor_modified()

    def _on_nk_summarize_error(self, err: str):
        self._set_nk_autosave_badge(saved=True)
        QMessageBox.warning(self, "Errore Riassunto AI", f"Impossibile generare il riassunto con Qwen 3.5:\n{err}")

    def _on_nk_new_note_clicked(self):
        self._active_nk_pane().open_new_empty_tab(
            subject=self._nk_current_filter_subject or "Generale",
            topic=self._nk_current_filter_topic or "Generale",
            profile=self._nk_author_name(),
        )

    def _save_nk_snapshot_now(self, reason: str = "Salvataggio manuale"):
        note_id = getattr(self, "_nk_selected_note_id", None)
        if not note_id or not hasattr(self, "nk_editor_body"):
            return
        title = (self.nk_editor_title.text().strip() if hasattr(self, "nk_editor_title") else "") or "Appunto senza titolo"
        content = self.nk_editor_body.get_markdown_text()
        try:
            db.add_note_snapshot(note_id, title, content, reason)
            self._nk_dirty_for_snapshot = False
            self._nk_last_snapshot_ts = time.time()
        except Exception as exc:
            logger.warning(f"Time Machine snapshot ({reason}) fallito: {exc}")

    def _save_nk_snapshot_if_dirty(self, reason: str = "Chiusura"):
        if getattr(self, "_nk_dirty_for_snapshot", False):
            self._save_nk_snapshot_now(reason)

    def _on_nk_history_clicked(self):
        pane = self._active_nk_pane()
        tab = pane.active_tab()
        note_id = tab.note_id if (tab and tab.workspace_type == "local") else None
        if not note_id:
            QMessageBox.information(
                self,
                "Time Machine",
                "Seleziona o salva prima una nota locale per visualizzarne la cronologia."
            )
            return
        note_title = pane.editor_title.text().strip() or "Nota"
        dlg = TimeMachineDialog(note_id, note_title, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_snapshot:
            restored_title = dlg.selected_snapshot.get("title")
            restored_content = dlg.selected_snapshot.get("content")
            if restored_content is not None:
                self._save_nk_snapshot_now("Prima di ripristino")
                pane._loading_note = True
                try:
                    pane._autosave_timer.stop()
                    if restored_title is not None:
                        pane.editor_title.setText(restored_title)
                    if pane.editor_body.is_studio_view:
                        pane.editor_body.set_studio_view(False)
                    pane.editor_body.set_markdown_text(restored_content)
                    pane._dirty_for_snapshot = False
                    pane._last_snapshot_ts = time.time()
                finally:
                    pane._loading_note = False
                pane._perform_autosave()
                pane.set_autosave_badge(True, custom_text="🕒 Versione ripristinata")

    def _show_nk_groups_menu(self):
        menu = QMenu(self)
        act_marker = menu.addAction(get_icon("clock", theme_key=self._current_theme, size=14), "Inserisci marcatore audio [HH:MM:SS]")
        act_marker.triggered.connect(self._on_nk_insert_timestamp_marker)
        menu.addSeparator()
        act_join = menu.addAction(get_icon("plus", theme_key=self._current_theme, size=14), "Crea o Unisciti a un Gruppo...")
        act_join.triggered.connect(self._on_nk_join_or_create_group)
        anchor_btn = getattr(self, "nk_groups_btn", None) or getattr(self, "nk_more_btn", None)
        if anchor_btn is not None:
            menu.exec(anchor_btn.mapToGlobal(QPoint(0, anchor_btn.height())))

    def _on_nk_groups_clicked(self):
        self._on_nk_join_or_create_group()

    def _on_nk_insert_timestamp_marker(self):
        pane = self._active_nk_pane()
        pos_ms = 0
        if hasattr(pane.audio_bar, "player"):
            try:
                pos_ms = int(pane.audio_bar.player.position())
            except Exception:
                pos_ms = 0
        total_secs = max(0, pos_ms // 1000)
        hh = total_secs // 3600
        mm = (total_secs % 3600) // 60
        ss = total_secs % 60
        marker = f"[{hh:02d}:{mm:02d}:{ss:02d}] "
        pane.editor_body.insertPlainText(marker)
        pane.editor_body.setFocus()

    def _on_nk_delete_clicked(self):
        pane = self._active_nk_pane()
        tab = pane.active_tab()
        if not tab or tab.workspace_type != "local" or not tab.note_id:
            return
        reply = QMessageBox.question(
            self, "Elimina Nota",
            "Vuoi davvero eliminare questa nota e tutte le eventuali flashcard e relazioni associate?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            pane._autosave_timer.stop()
            db.delete_study_note(tab.note_id)
            if getattr(self, "_nk_selected_note_id", None) == tab.note_id:
                self._nk_selected_note_id = None
            tab.is_dirty = False
            pane.close_tab(pane.current_tab_index)
            self._refresh_notekeeper()

    def _on_nk_ask_whis_clicked(self):
        title = self.nk_editor_title.text().strip()
        subj = self.nk_editor_subject.text().strip() or "Generale"
        top = self.nk_editor_topic.text().strip() or "Generale"
        content = self.nk_editor_body.get_markdown_text().strip()

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
        active_gid = getattr(self, "_nk_active_workspace_group_id", None)
        if active_gid is not None:
            groups = {int(g["id"]): g for g in db.get_flow_groups()}
            g = groups.get(int(active_gid))
            if g is not None:
                name, ok = QInputDialog.getText(self, "Nuova Cartella Gruppo", "Nome cartella:")
                if ok and name.strip():
                    try:
                        flow_groups.create_group_subject(g, name.strip())
                        self._refresh_notekeeper()
                    except Exception as exc:
                        QMessageBox.warning(self, "FlowGroups", str(exc))
                return

        from ui.notes_tree import FOLDER_PALETTE, make_vector_folder_icon
        dlg = QDialog(self)
        dlg.setWindowTitle("Nuova Cartella")
        dlg.setFixedWidth(400)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        lay.addWidget(_make_label("Crea una nuova cartella", "CardTitle"))
        lay.addWidget(QLabel("Nome cartella:"))
        name_edit = QLineEdit()
        name_edit.setMinimumHeight(36)
        name_edit.setPlaceholderText("Es. Fisica 1, Progetti, Appunti vari...")
        lay.addWidget(name_edit)

        lay.addWidget(QLabel("Colore:"))
        color_combo = QComboBox()
        color_combo.setMinimumHeight(34)
        for c_name, c_hex in FOLDER_PALETTE:
            color_combo.addItem(make_vector_folder_icon(c_hex, 14), c_name, c_hex)
        lay.addWidget(color_combo)

        lay.addWidget(QLabel("Cartella padre (opzionale):"))
        parent_combo = QComboBox()
        parent_combo.setMinimumHeight(34)
        parent_combo.addItem("Nessuna (cartella principale)", None)
        cur_parent = getattr(self, "_nk_current_filter_folder_id", None)
        for f in db.get_folders_tree():
            parent_combo.addItem(f["name"], int(f["id"]))
            if cur_parent is not None and int(f["id"]) == int(cur_parent):
                parent_combo.setCurrentIndex(parent_combo.count() - 1)
        lay.addWidget(parent_combo)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_b = _make_button("Annulla", "ghost")
        cancel_b.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_b)
        ok_b = _make_button("Crea Cartella", "primary")
        ok_b.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_b)
        lay.addLayout(btn_row)

        name_edit.setFocus()
        if dlg.exec() == QDialog.DialogCode.Accepted and name_edit.text().strip():
            prof = self.nk_profile_combo.currentText().strip() if hasattr(self, "nk_profile_combo") else "Alessandro"
            if prof == "Tutti i profili":
                prof = "Alessandro"
            db.create_folder(
                name=name_edit.text().strip(),
                parent_id=parent_combo.currentData(),
                color=color_combo.currentData() or "#6366F1",
                profile=prof,
            )
            self._refresh_notekeeper()

    def _on_nk_extract_flashcards_clicked(self):
        if not getattr(self, "_nk_selected_note_id", None):
            QMessageBox.warning(self, "Attenzione", "Seleziona o salva prima una nota nell'editor.")
            return
        note = db.get_study_note_by_id(self._nk_selected_note_id)
        if not note:
            return
        self._extract_flashcards_clicked(note, getattr(self, "nk_fc_btn", getattr(self, "nk_more_btn", None)))

    # -----------------------------------------------------------------
    # FLASHCARD ESAME (PILASTRO FLOWSTUDY - SCHEDA DEDICATA)
    # -----------------------------------------------------------------
    def _build_flowstudy_flashcards_page(self) -> QWidget:
        page, layout = self._make_page("🃏 Flashcard Esame", "Ripasso attivo e interrogazione sui concetti chiave estratti dalle tue sbobine.")
        layout.insertWidget(0, self._build_flownotes_view_bar("fn_fc_view_btns"), 0, Qt.AlignmentFlag.AlignLeft)

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

        self.fc_due_only_btn = _make_button("📅 Da ripassare oggi (0)", "ghost")
        self.fc_due_only_btn.setCheckable(True)
        self.fc_due_only_btn.setMinimumHeight(36)
        self.fc_due_only_btn.setToolTip("Mostra solo le flashcard in scadenza oggi (algoritmo SM-2)")
        self.fc_due_only_btn.toggled.connect(self._refresh_flashcards_page)
        toolbar.addWidget(self.fc_due_only_btn)

        layout.addLayout(toolbar)

        self.fc_list = ScrollList("Nessuna flashcard d'esame presente. Creala da un appunto cliccando '🧠 Flashcard'.")
        layout.addWidget(self.fc_list, 1)
        return page

    def _refresh_flashcards_page(self):
        if not hasattr(self, "fc_list"):
            return
        from datetime import date as _date
        today_str = _date.today().isoformat()
        query = self.fc_search.text().strip().lower() if hasattr(self, "fc_search") else ""
        prof = self.fc_profile_filter.currentText().strip() if hasattr(self, "fc_profile_filter") else "Tutti i profili"

        all_fc = db.get_all_study_notes(note_type="Flashcard")
        if prof != "Tutti i profili":
            all_fc = [f for f in all_fc if (f.get("profile") or "Alessandro") == prof or (prof in (f.get("tags") or ""))]

        due_cnt = sum(
            1 for f in all_fc
            if not (f.get("sr_next_review") or "").strip() or (f.get("sr_next_review") or "")[:10] <= today_str
        )
        if hasattr(self, "fc_due_only_btn"):
            self.fc_due_only_btn.setText(f"📅 Da ripassare oggi ({due_cnt})")

        if hasattr(self, "fc_due_only_btn") and self.fc_due_only_btn.isChecked():
            all_fc = [
                f for f in all_fc
                if not (f.get("sr_next_review") or "").strip() or (f.get("sr_next_review") or "")[:10] <= today_str
            ]

        if query:
            all_fc = [
                f for f in all_fc
                if query in (f.get("title") or "").lower()
                or query in (f.get("content_markdown") or "").lower()
                or query in (f.get("tags") or "").lower()
            ]

        # Ordina: prima quelle da ripassare oggi, poi per data di ripasso più vicina
        all_fc.sort(
            key=lambda f: (
                0 if (not (f.get("sr_next_review") or "").strip() or (f.get("sr_next_review") or "")[:10] <= today_str) else 1,
                (f.get("sr_next_review") or "0000-00-00")[:10],
            )
        )

        self.fc_list.clear()
        if not all_fc:
            empty_msg = (
                "🎉 Tutte le flashcard di oggi sono state ripassate!"
                if (hasattr(self, "fc_due_only_btn") and self.fc_due_only_btn.isChecked())
                else "Nessuna flashcard trovata con i filtri attuali."
            )
            self.fc_list.show_empty(empty_msg)
            return

        for fc in all_fc:
            self.fc_list.add_card(self._make_flashcard_study_card(fc))

    def _on_flashcard_sm2_rated(self, fid: int, quality: int):
        db.review_flashcard_sm2(int(fid), int(quality))
        self._refresh_flashcards_page()

    def _make_flashcard_study_card(self, fc: Dict) -> QFrame:
        from datetime import date as _date
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

        # Badge stato Spaced Repetition (SM-2)
        next_rev = (fc.get("sr_next_review") or "").strip()[:10]
        reps = int(fc.get("sr_repetitions") or 0)
        today_str = _date.today().isoformat()
        if not next_rev and reps == 0:
            sr_badge = _make_label("🆕 Nuova", "BadgeMuted")
        elif not next_rev or next_rev <= today_str:
            sr_badge = _make_label("🔥 Da ripassare", "Badge")
        else:
            try:
                delta_days = (_date.fromisoformat(next_rev) - _date.today()).days
                sr_badge = _make_label(f"✅ Tra {delta_days}g", "BadgeMuted")
            except Exception:
                sr_badge = _make_label(f"✅ {next_rev}", "BadgeMuted")
        meta_row.addWidget(sr_badge)
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
        ans_layout.setSpacing(8)
        ans_label = QLabel(ans_text)
        ans_label.setWordWrap(True)
        ans_label.setStyleSheet("color: #E2E8F0; font-size: 12.5px; line-height: 1.4;")
        ans_layout.addWidget(ans_label)

        # Valutazione SM-2 (Anki-style)
        rate_hint = QLabel("Quanto bene la ricordavi? (aggiorna la pianificazione SM-2)")
        rate_hint.setWordWrap(True)
        rate_hint.setStyleSheet("color: #94A3B8; font-size: 11px; border: none; padding-top: 4px;")
        ans_layout.addWidget(rate_hint)

        rate_row = QHBoxLayout()
        rate_row.setSpacing(6)
        for lbl_txt, q_val, tip in (
            ("🔁 Ripeti (1g)", 1, "Non ricordata: ripeti domani"),
            ("🟠 Difficile", 3, "Ricordata con fatica"),
            ("🟢 Bene", 4, "Ricordata correttamente"),
            ("⚡ Facile", 5, "Ricordata subito: intervallo lungo"),
        ):
            r_btn = _make_button(lbl_txt, "ghost")
            r_btn.setToolTip(tip)
            r_btn.setMinimumHeight(30)
            r_btn.clicked.connect(lambda _c=False, fid=fc_id, q=q_val: self._on_flashcard_sm2_rated(fid, q))
            rate_row.addWidget(r_btn)
        ans_layout.addLayout(rate_row)

        ans_frame.hide()
        layout.addWidget(ans_frame)

        toggle_btn = _make_button("👁️ Mostra Risposta")
        def _toggle(_checked=False, f=ans_frame, b=toggle_btn):
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
        layout.insertWidget(0, self._build_flownotes_view_bar("fn_graph_view_btns"), 0, Qt.AlignmentFlag.AlignLeft)

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
                is_queue_running = bool(self._cloud_queue_thread and self._cloud_queue_thread.isRunning())
                if count > 0:
                    self.cloud_badge.setText(f"☁️ Coda Cloud ({count} in attesa)")
                    self.cloud_badge.setProperty("pending", True)
                    self.cloud_badge.style().unpolish(self.cloud_badge)
                    self.cloud_badge.style().polish(self.cloud_badge)
                    is_busy = is_queue_running or bool(self._transcribe_thread and self._transcribe_thread.isRunning())
                    self.cloud_process_btn.setVisible(True)
                    self.cloud_process_btn.setEnabled(not is_busy)
                else:
                    self.cloud_badge.setText("☁️ Coda Cloud (0)")
                    self.cloud_badge.setProperty("pending", False)
                    self.cloud_badge.style().unpolish(self.cloud_badge)
                    self.cloud_badge.style().polish(self.cloud_badge)
                    if not is_queue_running:
                        self.cloud_process_btn.setEnabled(False)
                        self.cloud_process_btn.setVisible(False)
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
            f"Verranno sbobinati sequenzialmente con Qwen 3.5, salvati nel DB, "
            f"esportati in PDF/DOCX e archiviati in processed/.\n\n"
            f"Vuoi procedere ora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if hasattr(self, "cloud_process_btn"):
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
        if hasattr(self, "cloud_process_btn"):
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
        if hasattr(self, "cloud_process_btn"):
            self.cloud_process_btn.setEnabled(True)
            self.cloud_process_btn.setText("▶️ Elabora Coda")
        self.sbobina_btn.setEnabled(True)
        self._poll_cloud_queue()
        QMessageBox.critical(
            self, "Errore Coda Cloud", f"Si è verificato un errore durante l'elaborazione:\n{err_msg}"
        )

    def _on_cloud_queue_cancelled(self):
        self.sbobina_progress_box.hide()
        if hasattr(self, "cloud_process_btn"):
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

    def _on_sbobina_finished(self, markdown: str, file_name: str, source_path: str = "", sync_data=None):
        self.sbobina_progress_box.hide()
        self.sbobina_btn.setEnabled(True)
        self.sbobina_btn.setText("🎙️ Sbobina")

        # Salva la sbobina nella tabella study_notes del database
        title = f"Sbobina: {file_name}"
        new_id = db.add_study_note(
            title=title,
            content_markdown=markdown,
            note_type="Sbobina",
            tags=f"Audio, {file_name}"
        )

        # Archivia audio in data/audio_notes/ e salva la mappa timestamp parola-per-parola
        if source_path and os.path.exists(source_path):
            try:
                archived_audio = archive_audio_for_note(new_id, source_path)
                if archived_audio:
                    db.update_study_note_audio_path(new_id, archived_audio)
                    segments = (sync_data or {}).get("segments", []) if isinstance(sync_data, dict) else []
                    save_note_audio_sync(new_id, archived_audio, segments)
            except Exception as sync_err:
                print(f"[FlowLocal Sbobina] Avviso salvataggio audio sync: {sync_err}")

        self._refresh_notes()
        if hasattr(self, "nk_tree"):
            self._refresh_notekeeper()
            self._load_note_into_nk_editor(new_id)
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
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #1A1F29; color: #F8FAFC; border: 1px solid #2D3545; padding: 4px; border-radius: 8px; }"
            "QMenu::item { padding: 6px 14px; border-radius: 4px; font-size: 13px; }"
            "QMenu::item:selected { background-color: #0284C7; }"
        )

        md_act = menu.addAction("📄 Markdown (.md)")
        tex_act = menu.addAction("📄 Scarica documento LaTeX (.tex)")
        docx_act = menu.addAction("📘 Documento Word (.docx)")
        pdf_act = menu.addAction("📕 Documento PDF (.pdf)")

        anchor = getattr(self, "nk_more_btn", None)
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft()) if anchor is not None else QCursor.pos()
        chosen = menu.exec(pos)
        if not chosen:
            return
        fmt_map = {md_act: "md", tex_act: "tex", docx_act: "docx", pdf_act: "pdf"}
        self._export_notes_by_format(fmt_map[chosen])

    def _export_notes_by_format(self, fmt: str):
        notes = self._filtered_notes()
        if not notes:
            QMessageBox.information(self, "Esporta note", "Non ci sono note o sbobine da esportare.")
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        if fmt == "md":
            default_name = f"FlowLocal_Note_{timestamp}.md"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Markdown (*.md)")
            if path:
                try:
                    export_to_md(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Markdown:\n{e}")

        elif fmt == "tex":
            # Esporta la nota corrente aperta nell'editor (o tutte le filtrate se nessuna è aperta)
            if hasattr(self, "nk_editor_title") and hasattr(self, "nk_editor_body") and (
                getattr(self, "_nk_selected_note_id", None) or self.nk_editor_body.get_markdown_text().strip()
            ):
                cur_title = self.nk_editor_title.text().strip() or "Nota_FlowLocal"
                safe_t = re.sub(r'[\/:*?"<>|]', '_', cur_title)[:40] or "Nota"
                target_data = {
                    "title": cur_title,
                    "profile": self.nk_editor_profile.currentText().strip() or "Alessandro",
                    "subject": self.nk_editor_subject.text().strip() or "Generale",
                    "topic": self.nk_editor_topic.text().strip() or "Generale",
                    "note_type": self.nk_editor_type.currentText().strip() or "appunto",
                    "content_markdown": self.nk_editor_body.get_markdown_text(),
                }
                default_name = f"{safe_t}_{timestamp}.tex"
            else:
                target_data = notes
                default_name = f"FlowLocal_Note_{timestamp}.tex"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta documento LaTeX", default_name, "LaTeX (*.tex)")
            if path:
                try:
                    export_to_tex(target_data, path)
                    QMessageBox.information(self, "Esportazione completata", f"Documento LaTeX esportato in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in LaTeX:\n{e}")

        elif fmt == "docx":
            default_name = f"FlowLocal_Note_{timestamp}.docx"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta tutte le note", default_name, "Documento Word (*.docx)")
            if path:
                try:
                    export_to_docx(notes, path)
                    QMessageBox.information(self, "Esportazione completata", f"Tutte le note sono state esportate in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in Word:\n{e}")

        elif fmt == "pdf":
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
        tex_act = menu.addAction("📄 Scarica documento LaTeX (.tex)")
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

        elif chosen == tex_act:
            default_name = f"{safe_title}_{timestamp}.tex"
            path, _ = QFileDialog.getSaveFileName(self, "Esporta documento LaTeX", default_name, "LaTeX (*.tex)")
            if path:
                try:
                    export_to_tex(note_dict, path)
                    QMessageBox.information(self, "Esportazione completata", f"Documento LaTeX esportato in:\n{path}")
                except Exception as e:
                    QMessageBox.warning(self, "Errore esportazione", f"Impossibile esportare in LaTeX:\n{e}")

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
            "Dizionario & Snippet",
            "Correzioni fonetiche per guidare Whisper e scorciatoie vocali per espandere frasi a latenza zero.",
        )

        # Toggle pill Dizionario / Snippet
        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)
        self.dict_tab_btn = QPushButton("📖 Dizionario")
        self.dict_tab_btn.setObjectName("FilterPill")
        self.dict_tab_btn.setCheckable(True)
        self.dict_tab_btn.setChecked(True)
        self.snippet_tab_btn = QPushButton("⚡ Snippet Vocali")
        self.snippet_tab_btn.setObjectName("FilterPill")
        self.snippet_tab_btn.setCheckable(True)

        self.dict_subtab_group = QButtonGroup(self)
        self.dict_subtab_group.setExclusive(True)
        self.dict_subtab_group.addButton(self.dict_tab_btn, 0)
        self.dict_subtab_group.addButton(self.snippet_tab_btn, 1)

        pill_row.addWidget(self.dict_tab_btn)
        pill_row.addWidget(self.snippet_tab_btn)
        pill_row.addStretch(1)
        layout.addLayout(pill_row)

        self.dict_stack = SlidingStackedWidget(duration_ms=260)

        # --- Sub-tab 0: Dizionario ---
        dict_panel = QWidget()
        dict_layout = QVBoxLayout(dict_panel)
        dict_layout.setContentsMargins(0, 4, 0, 0)
        dict_layout.setSpacing(10)

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
        dict_layout.addLayout(form)

        self.dict_list = ScrollList("Il dizionario è vuoto.")
        dict_layout.addWidget(self.dict_list, 1)
        self.dict_stack.addWidget(dict_panel)

        # --- Sub-tab 1: Snippet Vocali ---
        snip_panel = QWidget()
        snip_layout = QVBoxLayout(snip_panel)
        snip_layout.setContentsMargins(0, 4, 0, 0)
        snip_layout.setSpacing(10)

        snip_top = QHBoxLayout()
        snip_top.setSpacing(10)
        self.snippet_trigger_input = QLineEdit()
        self.snippet_trigger_input.setPlaceholderText("Frase trigger vocale (es. firma cliente)")
        self.snippet_trigger_input.setMinimumHeight(36)
        snip_top.addWidget(self.snippet_trigger_input, 1)

        add_snip_btn = _make_button("Aggiungi Snippet", "primary")
        add_snip_btn.clicked.connect(self._add_snippet_rule)
        snip_top.addWidget(add_snip_btn)
        snip_layout.addLayout(snip_top)

        self.snippet_expansion_input = QTextEdit()
        self.snippet_expansion_input.setPlaceholderText("Testo completo da incollare quando pronunci il trigger...")
        self.snippet_expansion_input.setMinimumHeight(110)
        self.snippet_expansion_input.setMaximumHeight(130)
        snip_layout.addWidget(self.snippet_expansion_input)

        self.snippet_list = ScrollList("Nessuno snippet configurato.")
        snip_layout.addWidget(self.snippet_list, 1)
        self.dict_stack.addWidget(snip_panel)

        self.dict_subtab_group.idClicked.connect(self.dict_stack.setCurrentIndex)
        layout.addWidget(self.dict_stack, 1)
        return page

    def _refresh_dictionary(self):
        items = db.get_all_vocabulary()
        self.dict_list.clear()
        if not items:
            self.dict_list.show_empty()
        else:
            for v in items:
                self.dict_list.add_card(self._make_dictionary_card(v))

        if hasattr(self, "snippet_list"):
            snippets = db.get_all_snippets()
            self.snippet_list.clear()
            if not snippets:
                self.snippet_list.show_empty()
            else:
                for s in snippets:
                    self.snippet_list.add_card(self._make_snippet_card(s))

    def _make_snippet_card(self, s: Dict) -> QFrame:
        card, layout = _make_card()
        row = QHBoxLayout()
        row.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        text_col.addWidget(_make_label(f"⚡ \"{s['trigger_phrase']}\"", "CardTitle", wrap=True))
        text_col.addWidget(_make_label(s.get("expansion_text") or "", "CardBody", wrap=True))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        uses = s.get("use_count", 0)
        meta_row.addWidget(_make_label(f"Usato {uses} volte", "BadgeMuted"))
        meta_row.addStretch(1)
        text_col.addLayout(meta_row)
        row.addLayout(text_col, 1)

        del_btn = _make_button("Elimina", "danger")
        del_btn.clicked.connect(lambda _c=False, sid=s["id"]: self._delete_snippet_rule(sid))
        row.addWidget(del_btn, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(row)
        return card

    def _add_snippet_rule(self):
        trigger = self.snippet_trigger_input.text().strip()
        expansion = self.snippet_expansion_input.toPlainText().strip()
        if not trigger:
            self.snippet_trigger_input.setFocus()
            return
        if not expansion:
            self.snippet_expansion_input.setFocus()
            return
        db.add_snippet(trigger, expansion)
        self.snippet_trigger_input.clear()
        self.snippet_expansion_input.clear()
        self.snippet_trigger_input.setFocus()
        self._refresh_dictionary()

    def _delete_snippet_rule(self, snippet_id: int):
        db.delete_snippet(snippet_id)
        self._refresh_dictionary()

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
            "Stili & Impostazioni",
            "Seleziona una modalità, modifica il System Prompt e configura l'audio in dettatura.",
        )

        # Card impostazione rapida Auto-Ducking
        duck_card = QFrame()
        duck_card.setObjectName("Card")
        duck_layout = QHBoxLayout(duck_card)
        duck_layout.setContentsMargins(14, 10, 14, 10)
        duck_layout.setSpacing(10)

        self.styles_auto_duck_chk = QCheckBox(
            "🎵 Abbassa automaticamente la musica durante la dettatura (Auto-Ducking)"
        )
        self.styles_auto_duck_chk.setChecked(audio_ducker.is_enabled())
        self.styles_auto_duck_chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.styles_auto_duck_chk.setStyleSheet(
            "QCheckBox { font-size: 13px; font-weight: 600; padding: 2px 0px; background: transparent; }"
        )
        self.styles_auto_duck_chk.toggled.connect(lambda checked: audio_ducker.set_enabled(bool(checked)))
        duck_layout.addWidget(self.styles_auto_duck_chk)
        duck_layout.addStretch(1)

        duck_badge = QLabel("Spotify / Browser • 15% fade")
        duck_badge.setObjectName("Badge")
        duck_layout.addWidget(duck_badge)
        layout.addWidget(duck_card)

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
    # IMPOSTAZIONI & FLOWTIME (CALENDARIO LOCALE + GOOGLE SYNC + NOTE)
    # -----------------------------------------------------------------
    def _startup_google_calendar_sync(self):
        try:
            from core.calendar_sync import calendar_manager
            if calendar_manager.has_token_file():
                calendar_manager.sync_in_background(force=True)
        except Exception:
            pass

    def _periodic_google_calendar_sync(self):
        try:
            from core.calendar_sync import calendar_manager
            if calendar_manager.has_token_file():
                calendar_manager.sync_in_background(force=False)
        except Exception:
            pass

    def _probe_ollama_worker(self):
        ollama_url = db.get_setting("ollama_base_url", "http://localhost:11434") or "http://localhost:11434"
        ok = is_ollama_available(ollama_url, timeout=0.8)
        self._ollamaProbeSignal.emit(ok)

    def _on_ollama_probe_finished(self, ollama_ok: bool):
        self._ollama_detected = ollama_ok
        self._refresh_ai_onboarding_and_badge()

    def _on_ai_runtime_changed(self, label: str):
        if hasattr(self, "ai_engine_pill") and self.ai_engine_pill is not None:
            self.ai_engine_pill.setText(label)

    def _refresh_ai_onboarding_and_badge(self):
        if hasattr(self, "ai_engine_pill") and self.ai_engine_pill is not None:
            self.ai_engine_pill.setText(get_active_runtime_engine())
        if hasattr(self, "ai_onboarding_card") and self.ai_onboarding_card is not None:
            has_key = bool((db.get_setting("gemini_api_key", "") or "").strip())
            dismissed = (db.get_setting("dismissed_gemini_onboarding", "0") or "0") == "1"
            show_banner = (self._ollama_detected is False) and (not has_key) and (not dismissed)
            self.ai_onboarding_card.setVisible(show_banner)

    def _on_open_gemini_guide_from_hub(self):
        dlg = GeminiKeyGuideDialog(
            parent=self,
            theme_key=self._current_theme,
            reading_mode=self._reading_mode,
        )
        dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

    def _on_dismiss_ai_onboarding(self):
        db.set_setting("dismissed_gemini_onboarding", "1")
        if hasattr(self, "ai_onboarding_card") and self.ai_onboarding_card is not None:
            self.ai_onboarding_card.setVisible(False)

    def _on_open_settings_clicked(self, initial_tab: int = 0):
        dlg = HubSettingsDialog(self, initial_tab=int(initial_tab) if isinstance(initial_tab, int) else 0)
        dlg.exec()
        self._refresh_ai_onboarding_and_badge()
        self._refresh_system_status_pill()

    def _refresh_whats_new_badge(self):
        if not hasattr(self, "whats_new_btn"):
            return
        unread = has_unread_whats_new()
        if unread:
            self.whats_new_btn.setToolTip(f"Novità v{APP_VERSION} disponibili! Clicca per leggere novità, fix e tips")
            self.whats_new_btn.setIcon(get_icon("lightbulb", theme_key=self._current_theme, color="#38BDF8", size=15))
            self.whats_new_btn.setStyleSheet(
                "QPushButton { border: 1px solid rgba(56, 189, 248, 0.55); border-radius: 8px; }"
            )
        else:
            self.whats_new_btn.setToolTip("Novità, bug fix e suggerimenti nascosti")
            self.whats_new_btn.setIcon(get_icon("lightbulb", theme_key=self._current_theme, role="subtitle", size=15))
            self.whats_new_btn.setStyleSheet("")

    def _open_whats_new_dialog(self, initial_filter: str = "all"):
        dlg = WhatsNewDialog(
            parent=self,
            theme_key=self._current_theme,
            reading_mode=self._reading_mode,
            initial_filter=initial_filter if isinstance(initial_filter, str) else "all",
        )
        dlg.exec()
        self._refresh_whats_new_badge()

    def _open_onboarding_wizard(self):
        dlg = OnboardingWizardDialog(
            parent=self,
            theme_key=self._current_theme,
            reading_mode=self._reading_mode,
            on_theme_selected=self._set_theme,
            on_open_settings=lambda tab_idx: self._on_open_settings_clicked(initial_tab=tab_idx),
            ollama_detected=bool(self._ollama_detected),
        )
        dlg.exec()
        self._refresh_system_status_pill()

    def _collect_system_health(self):
        """Raccoglie stato live dei sottosistemi per Status Pill e DiagnosticsDialog."""
        items = []
        extra = {"Tema attivo": self._current_theme, "Modalità lettura": str(self._reading_mode)}
        has_warning = False

        # 1. Motore Acustico / GPU
        gpu_txt = "CUDA GPU attiva" if GPU_AVAILABLE else "Modalità CPU (fallback)"
        whisper_ok = self.engine is not None
        items.append({
            "label": "Motore Acustico (Faster-Whisper)",
            "detail": f"{gpu_txt} · Push-to-Talk: Shift Destro / F8",
            "status": "GPU" if GPU_AVAILABLE else "CPU",
            "level": "success" if whisper_ok else "warning",
        })
        extra["Hardware"] = gpu_txt

        # 2. Motore LLM (Ollama / Gemini)
        gemini_key = bool((db.get_setting("gemini_api_key", "") or "").strip())
        ollama_ok = bool(self._ollama_detected) if self._ollama_detected is not None else False
        runtime_lbl = get_active_runtime_engine() or ("Ollama locale" if ollama_ok else ("Gemini Cloud" if gemini_key else "Non configurato"))
        llm_ok = ollama_ok or gemini_key
        if not llm_ok:
            has_warning = True
        items.append({
            "label": "Motore AI (Raffinamento & Studio)",
            "detail": f"Attivo: {runtime_lbl} · Ollama: {'Online' if ollama_ok else 'Offline'} · Gemini API: {'Configurata' if gemini_key else 'Assente'}",
            "status": "Pronto" if llm_ok else "Da configurare",
            "level": "success" if llm_ok else "warning",
        })
        extra["Motore AI"] = runtime_lbl

        # 3. Google Calendar & Cloud Sync
        cal_expired = False
        cal_connected = False
        try:
            from core.calendar_sync import calendar_manager
            cal_expired = calendar_manager.is_auth_expired()
            cal_connected = calendar_manager.has_token_file()
        except Exception:
            pass

        if cal_expired:
            has_warning = True
            items.append({
                "label": "Google Calendar & Sync",
                "detail": "Sessione OAuth scaduta o revocata (invalid_grant). Riconnetti l'account per riprendere la sincronizzazione.",
                "status": "Scaduto",
                "level": "warning",
                "action": "reconnect_google",
            })
            extra["Google Calendar"] = "Token scaduto (invalid_grant)"
        elif cal_connected:
            items.append({
                "label": "Google Calendar & Sync",
                "detail": "Account Google connesso e sincronizzazione abilitata.",
                "status": "Connesso",
                "level": "success",
            })
            extra["Google Calendar"] = "Connesso"
        else:
            items.append({
                "label": "Google Calendar & Sync",
                "detail": "Nessun account Google connesso (opzionale). Puoi collegarlo in Impostazioni → Google & Gruppi.",
                "status": "Non collegato",
                "level": "info",
            })
            extra["Google Calendar"] = "Non collegato"

        # 4. Database SQLite WAL
        try:
            note_count = len(db.get_notes_by_hierarchy())
            items.append({
                "label": "Database Locale (SQLite WAL)",
                "detail": f"Connessione attiva · {note_count} note archiviate in FlowNotes.",
                "status": "OK",
                "level": "success",
            })
            extra["Note archiviate"] = str(note_count)
        except Exception as exc:
            has_warning = True
            items.append({
                "label": "Database Locale (SQLite WAL)",
                "detail": f"Errore lettura database: {exc}",
                "status": "Errore",
                "level": "error",
            })

        return items, extra, has_warning

    def _refresh_system_status_pill(self):
        if not hasattr(self, "system_status_pill"):
            return
        _, _, has_warning = self._collect_system_health()
        pal = self._active_palette()
        if has_warning:
            dot_col = "#F59E0B"
            label = "● Avviso"
            tip = "Uno o più servizi richiedono attenzione — Clicca per aprire la diagnostica"
        else:
            dot_col = "#10B981"
            label = "● GPU" if GPU_AVAILABLE else "● Pronto"
            tip = "Tutti i sistemi operativi — Clicca per aprire la diagnostica e i log"
        self.system_status_pill.setText(label)
        self.system_status_pill.setToolTip(tip)
        self.system_status_pill.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {pal['pill_bg']};"
            f"  color: {dot_col};"
            f"  border: 1px solid {pal['pill_border']};"
            f"  border-radius: 12px;"
            f"  padding: 0 9px;"
            f"  font-size: 11px;"
            f"  font-weight: 700;"
            f"  min-height: 24px;"
            f"}}"
            f"QPushButton:hover {{ background-color: {pal['pill_hover_bg']}; border-color: {dot_col}; }}"
        )

    def _open_diagnostics_dialog(self):
        items, extra, _ = self._collect_system_health()
        dlg = DiagnosticsDialog(
            parent=self,
            status_items=items,
            diagnostics_extra=extra,
            on_open_google_settings=lambda: self._on_open_settings_clicked(initial_tab=4),
            theme_key=self._current_theme,
            reading_mode=self._reading_mode,
        )
        dlg.exec()
        self._refresh_system_status_pill()

    def _run_startup_health_checks(self):
        """Esegue controlli discreti all'apertura dell'Hub e mostra toast azionabili se necessario."""
        self._refresh_system_status_pill()
        self._refresh_whats_new_badge()

        # 1. Verifica scadenza token Google Calendar (evita errori silenti solo in console)
        try:
            from core.calendar_sync import calendar_manager
            if calendar_manager.is_auth_expired():
                self.show_toast(
                    "La sessione di Google Calendar è scaduta o revocata. Riconnetti l'account per sincronizzare gli eventi.",
                    title="Sessione Google scaduta",
                    level="warning",
                    duration_ms=9000,
                    action_label="Riconnetti",
                    on_action=lambda: self._on_open_settings_clicked(initial_tab=4),
                )
        except Exception:
            pass

        # 2. Onboarding al primo avvio assoluto oppure notifica discreta novità versione
        if should_show_onboarding():
            QTimer.singleShot(250, self._open_onboarding_wizard)
        elif should_auto_show_whats_new():
            self.show_toast(
                "Scopri le ultime novità, i bug fix risolti e le funzioni nascoste di questa versione.",
                title=f"Novità in FlowLocal v{APP_VERSION}",
                level="info",
                duration_ms=8000,
                action_label="Apri Novità",
                on_action=self._open_whats_new_dialog,
            )

    def _open_note_from_flowtime(self, note_id: int):
        """Apre direttamente in FlowNotes la nota cliccata da FlowTime."""
        if not note_id:
            return
        self._nk_current_filter_subject = None
        self._nk_current_filter_topic = None
        if hasattr(self, "nk_profile_combo"):
            self.nk_profile_combo.blockSignals(True)
            self.nk_profile_combo.setCurrentText("Tutti i profili")
            self.nk_profile_combo.blockSignals(False)
        if hasattr(self, "nk_search_input"):
            self.nk_search_input.blockSignals(True)
            self.nk_search_input.clear()
            self.nk_search_input.blockSignals(False)
        self._nk_selected_note_id = note_id
        self._on_nav_clicked(1)
        self._load_note_into_nk_editor(note_id)

    def _build_flowtime_page(self) -> QWidget:
        self._ft_form_item_type: str = "event"
        self._ft_form_color: str = FLOWTIME_COLOR_PALETTE[0][0]
        self._ft_form_linked_note_ids: List[int] = []
        self._ft_color_buttons: Dict[str, QPushButton] = {}

        page = QWidget()
        page.setObjectName("Page")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(10)

        # Barra superiore compatta su riga unica: Titolo + Stretch + Ricerca + Pill Giorno/Settimana + Oggi (fuori da QScrollArea)
        top_ctrl = QHBoxLayout()
        top_ctrl.setContentsMargins(0, 0, 0, 0)
        top_ctrl.setSpacing(8)

        self.ft_page_title_lbl = _make_label("FlowTime", "PageTitle")
        top_ctrl.addWidget(self.ft_page_title_lbl)
        top_ctrl.addStretch(1)

        self.ft_search_input = QLineEdit()
        self.ft_search_input.setPlaceholderText("Cerca eventi, To-Do o aule...")
        self.ft_search_input.setFixedWidth(260)
        self.ft_search_input.setMinimumHeight(36)
        self.ft_search_input.textChanged.connect(self._on_ft_search_changed)
        top_ctrl.addWidget(self.ft_search_input)

        # Selettore Vista Giorno / Vista Settimana
        self.ft_view_group = QButtonGroup(self)
        self.ft_view_group.setExclusive(True)
        self.ft_view_day_btn = QPushButton("Giorno")
        self.ft_view_day_btn.setObjectName("FilterPill")
        self.ft_view_day_btn.setCheckable(True)
        self.ft_view_day_btn.setChecked(True)
        self.ft_view_day_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_view_week_btn = QPushButton("Settimana")
        self.ft_view_week_btn.setObjectName("FilterPill")
        self.ft_view_week_btn.setCheckable(True)
        self.ft_view_week_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_view_group.addButton(self.ft_view_day_btn, 0)
        self.ft_view_group.addButton(self.ft_view_week_btn, 1)
        self.ft_view_group.idClicked.connect(self._on_ft_view_mode_changed)
        top_ctrl.addWidget(self.ft_view_day_btn)
        top_ctrl.addWidget(self.ft_view_week_btn)

        self.ft_today_btn = _make_button(" Oggi", "ghost")
        self.ft_today_btn.setIcon(get_icon("calendar", theme_key=self._current_theme, role="text", size=14))
        self.ft_today_btn.clicked.connect(self._on_ft_today_clicked)
        top_ctrl.addWidget(self.ft_today_btn)
        layout.addLayout(top_ctrl)

        # Corpo principale: Calendario/Card Modifica a sinistra + Stack (Giorno | Settimana | Ricerca) + Sidebar To-Do a destra
        body = QHBoxLayout()
        body.setSpacing(12)

        # --- COLONNA SINISTRA: Calendario mensile custom + Card Creazione/Modifica ---
        left_col = QWidget()
        left_col.setFixedWidth(308)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.ft_calendar = FlowTimeCalendarWidget()
        self.ft_calendar.setMinimumHeight(206)
        self.ft_calendar.setMaximumHeight(224)
        self.ft_calendar.selectionChanged.connect(self._on_ft_date_selected)
        self.ft_calendar.currentPageChanged.connect(self._on_ft_month_changed)
        left_layout.addWidget(self.ft_calendar)

        # Form rapido creazione/modifica evento o To-Do (sempre fuori QScrollArea)
        form_card = QFrame()
        form_card.setObjectName("Card")
        fc_layout = QVBoxLayout(form_card)
        fc_layout.setContentsMargins(10, 9, 10, 9)
        fc_layout.setSpacing(6)

        hdr_form = QHBoxLayout()
        hdr_form.setSpacing(6)
        self.ft_form_title_lbl = _make_label("➕ Nuovo Impegno", "CardTitle")
        hdr_form.addWidget(self.ft_form_title_lbl, 1)
        self.ft_form_sync_lbl = _make_label("", "CardMeta")
        hdr_form.addWidget(self.ft_form_sync_lbl)
        fc_layout.addLayout(hdr_form)

        # Switch Evento / To-Do senza orario
        type_row = QHBoxLayout()
        type_row.setSpacing(6)
        self.ft_type_group = QButtonGroup(self)
        self.ft_type_group.setExclusive(True)
        self.ft_type_event_btn = QPushButton("📅 Evento")
        self.ft_type_event_btn.setObjectName("FTSegmentBtn")
        self.ft_type_event_btn.setCheckable(True)
        self.ft_type_event_btn.setChecked(True)
        self.ft_type_event_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_type_todo_btn = QPushButton("☑️ To-Do")
        self.ft_type_todo_btn.setObjectName("FTSegmentBtn")
        self.ft_type_todo_btn.setCheckable(True)
        self.ft_type_todo_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.ft_type_group.addButton(self.ft_type_event_btn, 0)
        self.ft_type_group.addButton(self.ft_type_todo_btn, 1)
        self.ft_type_event_btn.toggled.connect(
            lambda checked: self._set_ft_form_item_type("event" if checked else "todo")
        )
        type_row.addWidget(self.ft_type_event_btn, 1)
        type_row.addWidget(self.ft_type_todo_btn, 1)
        fc_layout.addLayout(type_row)

        self.ft_title_input = QLineEdit()
        self.ft_title_input.setPlaceholderText("Titolo evento / lezione / esame…")
        self.ft_title_input.setMinimumHeight(36)
        self.ft_title_input.returnPressed.connect(self._on_ft_save_event)
        fc_layout.addWidget(self.ft_title_input)

        # Blocco orario + luogo (nascosto in modalità To-Do)
        self.ft_time_row_widget = QWidget()
        self.ft_time_row_widget.setStyleSheet("background: transparent;")
        tw_layout = QVBoxLayout(self.ft_time_row_widget)
        tw_layout.setContentsMargins(0, 0, 0, 0)
        tw_layout.setSpacing(6)

        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        self.ft_start_input = QLineEdit("09:00")
        self.ft_start_input.setPlaceholderText("HH:MM")
        self.ft_start_input.setMinimumHeight(36)
        self.ft_start_input.setMaximumWidth(68)
        time_row.addWidget(self.ft_start_input)

        self.ft_duration_combo = QComboBox()
        self.ft_duration_combo.setMinimumHeight(36)
        for lbl, mins in [("30 min", 30), ("1 ora", 60), ("1h 30m", 90), ("2 ore", 120), ("3 ore", 180)]:
            self.ft_duration_combo.addItem(lbl, mins)
        self.ft_duration_combo.setCurrentIndex(1)
        time_row.addWidget(self.ft_duration_combo, 1)

        self.ft_allday_chk = QCheckBox("Giorno")
        self.ft_allday_chk.setToolTip("Evento che dura tutto il giorno")
        self.ft_allday_chk.toggled.connect(self._on_ft_allday_toggled)
        time_row.addWidget(self.ft_allday_chk)
        tw_layout.addLayout(time_row)

        self.ft_location_input = QLineEdit()
        self.ft_location_input.setPlaceholderText("Luogo / Aula (opzionale)")
        self.ft_location_input.setMinimumHeight(36)
        tw_layout.addWidget(self.ft_location_input)
        fc_layout.addWidget(self.ft_time_row_widget)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self.ft_category_combo = QComboBox()
        self.ft_category_combo.setMinimumHeight(36)
        for cat in ("Personale", "Università", "Esame", "Studio", "Lavoro"):
            self.ft_category_combo.addItem(cat)
        meta_row.addWidget(self.ft_category_combo)

        self.ft_subject_input = QLineEdit()
        self.ft_subject_input.setPlaceholderText("Materia (opz.)")
        self.ft_subject_input.setMinimumHeight(36)
        meta_row.addWidget(self.ft_subject_input, 1)
        fc_layout.addLayout(meta_row)

        # Colori + Ripetizione
        style_row = QHBoxLayout()
        style_row.setSpacing(5)
        for hex_col, cname in FLOWTIME_COLOR_PALETTE:
            cbtn = QPushButton("")
            cbtn.setObjectName("FTColorDot")
            cbtn.setToolTip(f"Colore: {cname}")
            cbtn.setCursor(Qt.CursorShape.PointingHandCursor)
            cbtn.clicked.connect(lambda _c=False, hc=hex_col: self._set_ft_form_color(hc))
            self._ft_color_buttons[hex_col] = cbtn
            style_row.addWidget(cbtn)

        self.ft_recurrence_combo = QComboBox()
        self.ft_recurrence_combo.setMinimumHeight(30)
        for lbl, val in [("Non si ripete", "none"), ("Ogni giorno", "daily"), ("Ogni sett.", "weekly"), ("Ogni mese", "monthly")]:
            self.ft_recurrence_combo.addItem(lbl, val)
        style_row.addWidget(self.ft_recurrence_combo, 1)
        fc_layout.addLayout(style_row)
        self._refresh_ft_form_color_buttons()

        # Note collegate all'impegno selezionato + bottone HUD ricerca note
        self.ft_form_notes_box = QWidget()
        self.ft_form_notes_box.setStyleSheet("background: transparent;")
        self.ft_form_notes_layout = QVBoxLayout(self.ft_form_notes_box)
        self.ft_form_notes_layout.setContentsMargins(0, 0, 0, 0)
        self.ft_form_notes_layout.setSpacing(3)
        self.ft_form_notes_box.setVisible(False)
        fc_layout.addWidget(self.ft_form_notes_box)

        self.ft_link_note_btn = _make_button(" Collega Nota...", "ghost")
        self.ft_link_note_btn.setIcon(get_icon("link", theme_key=self._current_theme, role="text", size=14))
        self.ft_link_note_btn.setStyleSheet("min-height: 28px; max-height: 28px; padding: 0 10px; font-size: 11.5px;")
        self.ft_link_note_btn.setToolTip("Collega appunti o sbobine a questo impegno")
        self.ft_link_note_btn.clicked.connect(self._on_ft_form_link_note_clicked)
        fc_layout.addWidget(self.ft_link_note_btn)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.ft_save_btn = _make_button(" Salva", "primary")
        self.ft_save_btn.setIcon(get_icon("save", theme_key=self._current_theme, role="on_accent", size=14))
        self.ft_save_btn.clicked.connect(self._on_ft_save_event)
        btn_row.addWidget(self.ft_save_btn, 1)

        self.ft_delete_btn = _make_button("", "danger")
        self.ft_delete_btn.setIcon(get_icon("trash", theme_key=self._current_theme, role="danger", size=14))
        self.ft_delete_btn.setToolTip("Elimina impegno")
        self.ft_delete_btn.clicked.connect(self._on_ft_delete_active_event)
        self.ft_delete_btn.setVisible(False)
        btn_row.addWidget(self.ft_delete_btn)

        self.ft_cancel_edit_btn = _make_button("Annulla", "ghost")
        self.ft_cancel_edit_btn.clicked.connect(self._reset_ft_form)
        self.ft_cancel_edit_btn.setVisible(False)
        btn_row.addWidget(self.ft_cancel_edit_btn)
        fc_layout.addLayout(btn_row)

        left_layout.addWidget(form_card)
        left_layout.addStretch(1)
        body.addWidget(left_col)

        # --- STACK DESTRO A PIENA LARGHEZZA: 0 = Giorno Timeline | 1 = Settimana Timeline | 2 = Ricerca ---
        self.ft_main_stack = SlidingStackedWidget(duration_ms=260)

        self.ft_day_timeline = FlowTimeDayTimeline(self)
        self.ft_day_timeline.event_clicked.connect(self._on_ft_edit_event)
        self.ft_day_timeline.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_day_timeline.empty_slot_clicked.connect(self._on_ft_empty_slot_clicked)
        self.ft_day_timeline.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_main_stack.addWidget(self.ft_day_timeline)  # Indice 0: Vista Giorno

        self.ft_week_view = FlowTimeWeekView(self)
        self.ft_week_view.date_selected.connect(self._on_ft_week_date_selected)
        self.ft_week_view.open_day_requested.connect(self._on_ft_open_day_from_week)
        self.ft_week_view.event_clicked.connect(self._on_ft_edit_event)
        self.ft_week_view.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_week_view.empty_slot_clicked.connect(self._on_ft_empty_slot_clicked)
        self.ft_week_view.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_week_view.prev_week_clicked.connect(lambda: self._shift_ft_selected_days(-7))
        self.ft_week_view.next_week_clicked.connect(lambda: self._shift_ft_selected_days(7))
        self.ft_main_stack.addWidget(self.ft_week_view)  # Indice 1: Vista Settimana

        # Pagina 2: Risultati ricerca FlowTime
        search_page = QFrame()
        search_page.setObjectName("Card")
        sp_layout = QVBoxLayout(search_page)
        sp_layout.setContentsMargins(12, 10, 12, 10)
        sp_layout.setSpacing(8)
        sp_hdr = QHBoxLayout()
        self.ft_search_header_lbl = _make_label("🔍 Risultati Ricerca", "CardTitle")
        sp_hdr.addWidget(self.ft_search_header_lbl, 1)
        clear_search_btn = _make_button("✕ Chiudi ricerca")
        clear_search_btn.clicked.connect(lambda: self.ft_search_input.clear())
        sp_hdr.addWidget(clear_search_btn)
        sp_layout.addLayout(sp_hdr)
        self.ft_search_results_list = ScrollList("Nessun evento o impegno trovato.")
        sp_layout.addWidget(self.ft_search_results_list, 1)
        self.ft_main_stack.addWidget(search_page)  # Indice 2: Ricerca

        body.addWidget(self.ft_main_stack, 1)

        # --- COLONNA DESTRA: Pannello Impegni (To-Do) & Note raggruppati per giorno ---
        self.ft_todo_sidebar = FlowTimeTodoSidebar(self)
        self.ft_todo_sidebar.date_selected.connect(self._on_ft_week_date_selected)
        self.ft_todo_sidebar.event_clicked.connect(self._on_ft_edit_event)
        self.ft_todo_sidebar.note_clicked.connect(self._open_note_from_flowtime)
        self.ft_todo_sidebar.todo_toggled.connect(self._on_ft_todo_toggled)
        self.ft_todo_sidebar.add_todo_requested.connect(self._on_ft_quick_add_todo)
        body.addWidget(self.ft_todo_sidebar)

        layout.addLayout(body, 1)
        return page

    def _selected_ft_date_str(self) -> str:
        if hasattr(self, "ft_calendar"):
            return self.ft_calendar.selectedDate().toString("yyyy-MM-dd")
        return dt_date.today().isoformat()

    def _set_ft_form_item_type(self, mode: str):
        self._ft_form_item_type = "todo" if mode == "todo" else "event"
        is_todo = self._ft_form_item_type == "todo"
        if hasattr(self, "ft_time_row_widget"):
            self.ft_time_row_widget.setVisible(not is_todo)
        if hasattr(self, "ft_title_input"):
            self.ft_title_input.setPlaceholderText(
                "Impegno To-Do (es. Innaffiare le piante)…"
                if is_todo
                else "Titolo evento / lezione / esame…"
            )

    def _set_ft_form_color(self, hex_col: str):
        self._ft_form_color = hex_col or FLOWTIME_COLOR_PALETTE[0][0]
        self._refresh_ft_form_color_buttons()

    def _refresh_ft_form_color_buttons(self):
        for hc, btn in getattr(self, "_ft_color_buttons", {}).items():
            border = "2px solid #FFFFFF" if hc == self._ft_form_color else "1px solid rgba(148,163,184,0.35)"
            btn.setStyleSheet(f"background-color: {hc}; border: {border}; border-radius: 10px;")

    def _refresh_ft_form_notes_ui(self):
        if not hasattr(self, "ft_form_notes_layout"):
            return
        while self.ft_form_notes_layout.count():
            item = self.ft_form_notes_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif item.layout() is not None:
                sub = item.layout()
                while sub.count():
                    si = sub.takeAt(0)
                    if si.widget():
                        si.widget().hide()
                        si.widget().deleteLater()

        note_map = {int(n["id"]): n for n in db.get_all_study_notes()}
        valid_ids = [nid for nid in self._ft_form_linked_note_ids if nid in note_map]
        self._ft_form_linked_note_ids = valid_ids

        if not valid_ids:
            self.ft_form_notes_box.setVisible(False)
            self.ft_link_note_btn.setText("🔗 Collega Nota...")
            return

        self.ft_form_notes_box.setVisible(True)
        self.ft_link_note_btn.setText(f"🔗 Gestisci Note ({len(valid_ids)})...")
        for nid in valid_ids[:4]:
            note = note_map[nid]
            row = QHBoxLayout()
            row.setSpacing(4)
            open_btn = QPushButton(f"📓 {(note.get('title') or 'Nota')[:26]}")
            open_btn.setObjectName("FTNotePillBtn")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.setToolTip(f"Apri '{note.get('title') or 'Nota'}' in FlowNotes")
            open_btn.clicked.connect(lambda _c=False, target_id=nid: self._open_note_from_flowtime(target_id))
            row.addWidget(open_btn, 1)

            rm_btn = QPushButton("×")
            rm_btn.setStyleSheet("min-height: 22px; max-height: 22px; min-width: 22px; max-width: 22px; padding: 0px; font-size: 12px;")
            rm_btn.setToolTip("Scollega questa nota")
            rm_btn.clicked.connect(lambda _c=False, target_id=nid: self._on_ft_form_unlink_note(target_id))
            row.addWidget(rm_btn)
            self.ft_form_notes_layout.addLayout(row)

    def _on_ft_form_unlink_note(self, note_id: int):
        self._ft_form_linked_note_ids = [x for x in self._ft_form_linked_note_ids if x != note_id]
        if self._ft_editing_event_id is not None:
            db.unlink_note_from_event(self._ft_editing_event_id, note_id)
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_form_link_note_clicked(self):
        ev_title = self.ft_title_input.text().strip() or (
            "Modifica Impegno" if self._ft_editing_event_id else "Nuovo Impegno"
        )
        dlg = LinkEventNoteDialog(
            self._ft_editing_event_id,
            ev_title,
            preselected_ids=self._ft_form_linked_note_ids,
            parent=self,
        )
        dlg.open_note_requested.connect(self._open_note_from_flowtime)
        dlg.exec()
        self._ft_form_linked_note_ids = list(dlg.selected_note_ids)
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_allday_toggled(self, checked: bool):
        if hasattr(self, "ft_start_input"):
            self.ft_start_input.setEnabled(not checked)
        if hasattr(self, "ft_duration_combo"):
            self.ft_duration_combo.setEnabled(not checked)

    def _on_ft_view_mode_changed(self, mode_idx: int):
        if hasattr(self, "ft_search_input") and self.ft_search_input.text().strip():
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(1 if mode_idx == 1 else 0)
            if mode_idx == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(50, self.ft_day_timeline.scroll_to_focus)
            elif mode_idx == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(50, self.ft_week_view.scroll_to_focus)
        self._refresh_flowtime_lists()

    def _on_ft_search_changed(self):
        q = self.ft_search_input.text().strip() if hasattr(self, "ft_search_input") else ""
        if hasattr(self, "ft_main_stack"):
            if q:
                self.ft_main_stack.setCurrentIndex(2)
            else:
                self.ft_main_stack.setCurrentIndex(1 if self.ft_view_week_btn.isChecked() else 0)
        self._refresh_flowtime_lists()

    def _on_ft_week_date_selected(self, date_str: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar"):
            self.ft_calendar.setSelectedDate(qd)

    def _on_ft_open_day_from_week(self, date_str: str):
        self._on_ft_week_date_selected(date_str)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        self._on_ft_view_mode_changed(0)

    def _shift_ft_selected_days(self, delta_days: int):
        if hasattr(self, "ft_calendar"):
            new_qdate = self.ft_calendar.selectedDate().addDays(delta_days)
            self.ft_calendar.setSelectedDate(new_qdate)

    def _on_ft_today_clicked(self):
        if hasattr(self, "ft_search_input") and self.ft_search_input.text().strip():
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
            self.ft_main_stack.setCurrentIndex(1 if self.ft_view_week_btn.isChecked() else 0)
        if hasattr(self, "ft_calendar"):
            self.ft_calendar.setSelectedDate(QDate.currentDate())
            self._refresh_flowtime_page()

    def _on_ft_date_selected(self):
        self._refresh_flowtime_lists()
        if hasattr(self, "ft_main_stack"):
            if self.ft_main_stack.currentIndex() == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(40, self.ft_day_timeline.scroll_to_focus)
            elif self.ft_main_stack.currentIndex() == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(40, self.ft_week_view.scroll_to_focus)

    def _on_ft_month_changed(self, year: int, month: int):
        self._update_ft_calendar_highlights(year, month)

    def _on_ft_empty_slot_clicked(self, date_str: str, hhmm: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar") and qd != self.ft_calendar.selectedDate():
            self.ft_calendar.setSelectedDate(qd)
        dlg = TimelineQuickAddDialog(date_str, hhmm, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_flowtime_page()

    def _on_ft_quick_add_todo(self, date_str: str):
        qd = QDate.fromString(date_str, "yyyy-MM-dd")
        if qd.isValid() and hasattr(self, "ft_calendar") and qd != self.ft_calendar.selectedDate():
            self.ft_calendar.setSelectedDate(qd)
        dlg = TimelineQuickAddDialog(date_str, "09:00", parent=self)
        dlg.todo_mode_btn.setChecked(True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_flowtime_page()

    def _on_ft_todo_toggled(self, event_id: int):
        from core.calendar_sync import calendar_manager
        calendar_manager.toggle_todo(event_id)
        self._refresh_flowtime_page()

    def _refresh_flowtime_page(self):
        if not hasattr(self, "ft_calendar"):
            return
        sel_date = self.ft_calendar.selectedDate()
        self._update_ft_calendar_highlights(sel_date.year(), sel_date.month())
        self._refresh_flowtime_lists()
        if hasattr(self, "ft_main_stack"):
            if self.ft_main_stack.currentIndex() == 0 and hasattr(self, "ft_day_timeline"):
                QTimer.singleShot(70, self.ft_day_timeline.scroll_to_focus)
            elif self.ft_main_stack.currentIndex() == 1 and hasattr(self, "ft_week_view"):
                QTimer.singleShot(70, self.ft_week_view.scroll_to_focus)

    def _update_ft_calendar_highlights(self, year: int, month: int):
        if not hasattr(self, "ft_calendar"):
            return
        activity = db.get_dates_with_activity(year, month)
        self.ft_calendar.set_activity_data(activity, self._active_is_dark(), self._reading_mode)

    def _refresh_flowtime_lists(self):
        if not hasattr(self, "ft_day_timeline"):
            return
        from core.calendar_sync import calendar_manager, ITALIAN_WEEKDAYS

        date_str = self._selected_ft_date_str()
        search_q = self.ft_search_input.text().strip() if hasattr(self, "ft_search_input") else ""
        day_events = calendar_manager.get_upcoming_events(days=1, target_date=date_str)

        try:
            d_obj = dt_date.fromisoformat(date_str)
            months_full = [
                "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
            ]
            day_title = f"{ITALIAN_WEEKDAYS[d_obj.weekday()]} {d_obj.day} {months_full[d_obj.month - 1]} {d_obj.year}"
        except Exception:
            d_obj = dt_date.today()
            day_title = date_str

        day_notes = db.get_notes_for_date(date_str)
        notes_by_event: Dict[int, List[Dict[str, Any]]] = {}
        for ev in day_events:
            eid = int(ev.get("local_id") or ev.get("id"))
            notes_by_event[eid] = ev.get("linked_notes") or db.get_notes_for_event(eid)

        todo_cnt = sum(1 for e in day_events if e.get("item_type") == "todo")
        ev_cnt = len(day_events) - todo_cnt
        self.ft_day_timeline.day_title_lbl.setText(day_title)
        self.ft_day_timeline.day_stats_lbl.setText(
            f"{ev_cnt} event{'o' if ev_cnt == 1 else 'i'} · {todo_cnt} To-Do · {len(day_notes)} not{'a' if len(day_notes) == 1 else 'e'}"
        )
        self.ft_day_timeline.set_day_data(
            date_str=date_str,
            events=day_events,
            day_notes=day_notes,
            notes_by_event=notes_by_event,
            is_dark=self._active_is_dark(),
            reading_mode=self._reading_mode,
            selected_event_id=self._ft_editing_event_id,
        )

        # Aggiorna Vista Settimanale a 7 colonne orarie (Lun–Dom) e Sidebar destra Impegni (To-Do) & Note
        monday = d_obj - timedelta(days=d_obj.weekday())
        sunday = monday + timedelta(days=6)
        week_rows = db.get_calendar_events(start_date=monday.isoformat(), end_date=sunday.isoformat())
        week_events_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for r in week_rows:
            ev_fmt = calendar_manager._format_db_event(r)
            week_events_by_date.setdefault(ev_fmt["event_date"], []).append(ev_fmt)
        week_events_by_date.setdefault(date_str, day_events)
        week_notes_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for offset in range(7):
            ds = (monday + timedelta(days=offset)).isoformat()
            week_notes_by_date[ds] = db.get_notes_for_date(ds)
        week_notes_by_date.setdefault(date_str, day_notes)
        week_notes_by_event: Dict[int, List[Dict[str, Any]]] = dict(notes_by_event)
        for ev_list in week_events_by_date.values():
            for ev in ev_list:
                if ev.get("item_type") == "todo" or ev.get("is_all_day"):
                    eid = int(ev.get("local_id") or ev.get("id"))
                    if eid not in week_notes_by_event:
                        week_notes_by_event[eid] = ev.get("linked_notes") or db.get_notes_for_event(eid)

        if hasattr(self, "ft_week_view"):
            self.ft_week_view.set_week_data(
                date_str,
                week_events_by_date,
                week_notes_by_date,
                is_dark=self._active_is_dark(),
                reading_mode=self._reading_mode,
                selected_event_id=self._ft_editing_event_id,
            )

        if hasattr(self, "ft_todo_sidebar"):
            self.ft_todo_sidebar.set_sidebar_data(
                date_str,
                week_events_by_date,
                week_notes_by_date,
                week_notes_by_event,
            )

        # Popola pagina ricerca se attiva
        if search_q and hasattr(self, "ft_search_results_list"):
            raw_rows = db.search_calendar_events(query=search_q, limit=60)
            results = [calendar_manager._format_db_event(r) for r in raw_rows]
            self.ft_search_header_lbl.setText(f"🔍 Risultati ricerca: \"{search_q}\" ({len(results)})")
            self.ft_search_results_list.clear()
            if not results:
                self.ft_search_results_list.show_empty(f"Nessun evento o impegno trovato per '{search_q}'.")
            else:
                for ev in results:
                    self.ft_search_results_list.add_card(self._make_ft_search_card(ev))

    def _make_ft_search_card(self, ev: Dict[str, Any]) -> QFrame:
        card = QFrame()
        card.setObjectName("FTSearchResultCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)

        ev_id = int(ev.get("local_id") or ev.get("id"))
        is_todo = ev.get("item_type") == "todo"
        top = QHBoxLayout()
        top.setSpacing(6)
        time_label = f"{ev['date']} · ☑️ To-Do" if is_todo else f"{ev['date']} · {ev['time']}"
        top.addWidget(_make_label(time_label, "FTTimePill"))
        top.addWidget(_make_label(ev.get("category") or "Personale", "BadgeMuted"))
        if ev.get("subject"):
            top.addWidget(_make_label(ev["subject"], "Badge"))
        top.addStretch(1)

        if is_todo:
            done_btn = _make_button("✅ Fatto" if ev.get("is_completed") else "☐ Completa")
            done_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
            done_btn.clicked.connect(lambda _c=False, eid=ev_id: self._on_ft_todo_toggled(eid))
            top.addWidget(done_btn)

        open_btn = _make_button("✏️ Modifica & Vai", "primary")
        open_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 10px; font-size: 11px;")
        open_btn.clicked.connect(lambda _c=False, e=ev: self._on_ft_open_search_result(e))
        top.addWidget(open_btn)
        layout.addLayout(top)

        t_lbl = _make_label(ev.get("summary") or "(Senza titolo)", "CardTitle", wrap=True)
        layout.addWidget(t_lbl)
        if ev.get("location"):
            layout.addWidget(_make_label(f"📍 {ev['location']}", "CardMeta", wrap=True))
        return card

    def _on_ft_open_search_result(self, ev: Dict[str, Any]):
        if hasattr(self, "ft_search_input"):
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(0)
        self._on_ft_edit_event(ev)

    def _reset_ft_form(self):
        self._ft_editing_event_id = None
        self._ft_form_linked_note_ids = []
        self.ft_form_title_lbl.setText("➕ Nuovo Impegno")
        self.ft_form_sync_lbl.setText("")
        self.ft_save_btn.setText("💾 Salva in FlowTime")
        self.ft_delete_btn.setVisible(False)
        self.ft_cancel_edit_btn.setVisible(False)
        self.ft_type_event_btn.setChecked(True)
        self._set_ft_form_item_type("event")
        self.ft_title_input.clear()
        self.ft_start_input.setText("09:00")
        self.ft_duration_combo.setCurrentIndex(1)
        self.ft_allday_chk.setChecked(False)
        self.ft_category_combo.setCurrentIndex(0)
        self.ft_subject_input.clear()
        self.ft_location_input.clear()
        self.ft_recurrence_combo.setCurrentIndex(0)
        self._set_ft_form_color(FLOWTIME_COLOR_PALETTE[0][0])
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_edit_event(self, ev: Dict[str, Any]):
        ev_date = (ev.get("event_date") or "").strip()
        if ev_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(ev_date, "yyyy-MM-dd")
            if qd.isValid() and qd != self.ft_calendar.selectedDate():
                self.ft_calendar.setSelectedDate(qd)
        ev_id = int(ev.get("local_id") or ev.get("id"))
        self._ft_editing_event_id = ev_id
        is_todo = ev.get("item_type") == "todo"
        self.ft_form_title_lbl.setText("✏️ Modifica To-Do" if is_todo else "✏️ Modifica Evento")
        sync_st = ev.get("sync_status") or "local_only"
        self.ft_form_sync_lbl.setText("☁️ Google" if sync_st == "synced" else "💻 Locale")
        self.ft_save_btn.setText("💾 Aggiorna")
        self.ft_delete_btn.setVisible(True)
        self.ft_cancel_edit_btn.setVisible(True)

        if is_todo:
            done_btn = _make_button("✅ Fatto" if ev.get("is_completed") else "☐ Completa")
            done_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 8px; font-size: 11px;")
            done_btn.clicked.connect(lambda _c=False, eid=ev_id: self._on_ft_todo_toggled(eid))
            top.addWidget(done_btn)

        open_btn = _make_button("✏️ Modifica & Vai", "primary")
        open_btn.setStyleSheet("min-height: 24px; max-height: 24px; padding: 0 10px; font-size: 11px;")
        open_btn.clicked.connect(lambda _c=False, e=ev: self._on_ft_open_search_result(e))
        top.addWidget(open_btn)
        layout.addLayout(top)

        t_lbl = _make_label(ev.get("summary") or "(Senza titolo)", "CardTitle", wrap=True)
        layout.addWidget(t_lbl)
        if ev.get("location"):
            layout.addWidget(_make_label(f"📍 {ev['location']}", "CardMeta", wrap=True))
        return card

    def _on_ft_open_search_result(self, ev: Dict[str, Any]):
        if hasattr(self, "ft_search_input"):
            self.ft_search_input.blockSignals(True)
            self.ft_search_input.clear()
            self.ft_search_input.blockSignals(False)
        if hasattr(self, "ft_view_day_btn"):
            self.ft_view_day_btn.setChecked(True)
        if hasattr(self, "ft_main_stack"):
            self.ft_main_stack.setCurrentIndex(0)
        self._on_ft_edit_event(ev)

    def _reset_ft_form(self):
        self._ft_editing_event_id = None
        self._ft_form_linked_note_ids = []
        self.ft_form_title_lbl.setText("➕ Nuovo Impegno")
        self.ft_form_sync_lbl.setText("")
        self.ft_save_btn.setText("💾 Salva in FlowTime")
        self.ft_delete_btn.setVisible(False)
        self.ft_cancel_edit_btn.setVisible(False)
        self.ft_type_event_btn.setChecked(True)
        self._set_ft_form_item_type("event")
        self.ft_title_input.clear()
        self.ft_start_input.setText("09:00")
        self.ft_duration_combo.setCurrentIndex(1)
        self.ft_allday_chk.setChecked(False)
        self.ft_category_combo.setCurrentIndex(0)
        self.ft_subject_input.clear()
        self.ft_location_input.clear()
        self.ft_recurrence_combo.setCurrentIndex(0)
        self._set_ft_form_color(FLOWTIME_COLOR_PALETTE[0][0])
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()

    def _on_ft_edit_event(self, ev: Dict[str, Any]):
        ev_date = (ev.get("event_date") or "").strip()
        if ev_date and hasattr(self, "ft_calendar"):
            qd = QDate.fromString(ev_date, "yyyy-MM-dd")
            if qd.isValid() and qd != self.ft_calendar.selectedDate():
                self.ft_calendar.setSelectedDate(qd)
        ev_id = int(ev.get("local_id") or ev.get("id"))
        self._ft_editing_event_id = ev_id
        is_todo = ev.get("item_type") == "todo"
        self.ft_form_title_lbl.setText("✏️ Modifica To-Do" if is_todo else "✏️ Modifica Evento")
        sync_st = ev.get("sync_status") or "local_only"
        self.ft_form_sync_lbl.setText("☁️ Google" if sync_st == "synced" else "💻 Locale")
        self.ft_save_btn.setText("💾 Aggiorna")
        self.ft_delete_btn.setVisible(True)
        self.ft_cancel_edit_btn.setVisible(True)

        if is_todo:
            self.ft_type_todo_btn.setChecked(True)
            self._set_ft_form_item_type("todo")
        else:
            self.ft_type_event_btn.setChecked(True)
            self._set_ft_form_item_type("event")

        self.ft_title_input.setText(ev.get("summary") or "")
        is_all = bool(ev.get("is_all_day"))
        self.ft_allday_chk.setChecked(is_all)
        self.ft_start_input.setText(ev.get("start_time") or "09:00")
        st_s = (ev.get("start_time") or "").strip()
        en_s = (ev.get("end_time") or "").strip()
        if not is_all and not is_todo and ":" in st_s and ":" in en_s:
            try:
                sh, sm = [int(x) for x in st_s[:5].split(":")]
                eh, em = [int(x) for x in en_s[:5].split(":")]
                diff = (eh * 60 + em) - (sh * 60 + sm)
                if diff > 0:
                    idx_d = self.ft_duration_combo.findData(diff)
                    if idx_d >= 0:
                        self.ft_duration_combo.setCurrentIndex(idx_d)
            except Exception:
                pass
        cat_idx = self.ft_category_combo.findText(ev.get("category") or "Personale")
        if cat_idx >= 0:
            self.ft_category_combo.setCurrentIndex(cat_idx)
        self.ft_subject_input.setText(ev.get("subject") or "")
        self.ft_location_input.setText(ev.get("location") or "")

        rec_val = ev.get("recurrence") or "none"
        rec_idx = self.ft_recurrence_combo.findData(rec_val)
        self.ft_recurrence_combo.setCurrentIndex(rec_idx if rec_idx >= 0 else 0)

        col_val = ev.get("color") or FLOWTIME_COLOR_PALETTE[0][0]
        self._set_ft_form_color(col_val)

        self._ft_form_linked_note_ids = [int(n["id"]) for n in db.get_notes_for_event(ev_id)]
        self._refresh_ft_form_notes_ui()
        self._refresh_flowtime_lists()
        self.ft_title_input.setFocus()

    def _on_ft_save_event(self):
        title = self.ft_title_input.text().strip()
        if not title:
            self.ft_title_input.setFocus()
            return
        from core.calendar_sync import calendar_manager

        date_str = self._selected_ft_date_str()
        item_type = self._ft_form_item_type
        is_todo = item_type == "todo"
        is_all = True if is_todo else self.ft_allday_chk.isChecked()
        start_str = "00:00" if is_todo else ("all_day" if is_all else (self.ft_start_input.text().strip() or "09:00"))
        duration = 0 if is_todo else int(self.ft_duration_combo.currentData() or 60)
        cat = self.ft_category_combo.currentText()
        subj = self.ft_subject_input.text().strip()
        loc = "" if is_todo else self.ft_location_input.text().strip()
        rec = str(self.ft_recurrence_combo.currentData() or "none")
        color = self._ft_form_color

        if self._ft_editing_event_id is not None:
            end_str = ""
            if not is_all and not is_todo:
                try:
                    _, _, start_str, end_str = calendar_manager._parse_date_and_times(date_str, start_str, duration)
                except Exception:
                    end_str = ""
            calendar_manager.update_event(
                self._ft_editing_event_id,
                summary=title,
                date_str=date_str,
                start_time_str=start_str,
                end_time_str=end_str,
                is_all_day=is_all,
                location=loc,
                category=cat,
                subject=subj,
                color=color,
                item_type=item_type,
                recurrence=rec,
            )
            for nid in self._ft_form_linked_note_ids:
                db.link_note_to_event(self._ft_editing_event_id, int(nid), link_type="manual")
        else:
            created = calendar_manager.create_event(
                summary=title,
                date_str=date_str,
                start_time_str=start_str,
                duration_minutes=duration,
                location=loc,
                category=cat,
                subject=subj,
                color=color,
                push_to_google=not is_todo,
                item_type=item_type,
                recurrence=rec,
            )
            if created and created.get("local_id"):
                cid = int(created["local_id"])
                for nid in self._ft_form_linked_note_ids:
                    db.link_note_to_event(cid, int(nid), link_type="manual")

        self._reset_ft_form()
        self._refresh_flowtime_page()

    def _on_ft_delete_active_event(self):
        if self._ft_editing_event_id is None:
            return
        self._on_ft_delete_event(self._ft_editing_event_id)

    def _on_ft_delete_event(self, event_id: int):
        from core.calendar_sync import calendar_manager
        calendar_manager.delete_event(event_id)
        if self._ft_editing_event_id == event_id:
            self._reset_ft_form()
        else:
            self._refresh_flowtime_page()

    def _on_ft_link_note_clicked(self, event_id: int, event_title: str):
        dlg = LinkEventNoteDialog(event_id, event_title, parent=self)
        dlg.open_note_requested.connect(self._open_note_from_flowtime)
        dlg.exec()
        self._refresh_flowtime_lists()

    def _on_ft_sync_google_clicked(self):
        from core.calendar_sync import calendar_manager
        if not calendar_manager.has_token_file():
            self._on_connect_calendar_clicked()
            return
        calendar_manager.sync_in_background(force=True)

    def _on_ft_sync_finished(self, ok: bool, msg: str):
        self._update_calendar_status_ui()
        if hasattr(self, "ft_calendar"):
            self._refresh_flowtime_page()
        if hasattr(self, "pomo_ring"):
            p = self._active_palette()
            self.pomo_ring.set_theme_colors(
                p.get("accent") or p.get("nav_checked_border") or "#6366F1",
                p.get("border") or "#334155",
                p.get("title") if self._active_is_dark() or self._reading_mode else p.get("card_body"),
                p.get("muted") or "#94A3B8",
            )

    # -----------------------------------------------------------------
    # FLOWNOTES: POMODORO FOCUS PER MATERIA & EXAM COUNTDOWN (BLOCCO 4)
    # -----------------------------------------------------------------
    def _build_flowstudy_focus_page(self) -> QWidget:
        page, outer_layout = self._make_page(
            "⏱️ Focus & Esami",
            "Timer Pomodoro per materia e conto alla rovescia collegato alle tue note di FlowNotes.",
        )
        outer_layout.insertWidget(0, self._build_flownotes_view_bar("fn_focus_view_btns"), 0, Qt.AlignmentFlag.AlignLeft)

        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        outer_layout.addLayout(layout, 1)

        # Colonna sinistra: Pomodoro Focus
        pomo_card = QFrame()
        pomo_card.setObjectName("Card")
        pomo_layout = QVBoxLayout(pomo_card)
        pomo_layout.setContentsMargins(14, 12, 14, 12)
        pomo_layout.setSpacing(10)

        pomo_hdr = QHBoxLayout()
        pomo_hdr.addWidget(_make_label("⏱️ Pomodoro Focus", "CardTitle"))
        pomo_hdr.addStretch(1)
        self.pomo_mode_switch_btn = _make_button("☕ Pausa")
        self.pomo_mode_switch_btn.setToolTip("Passa manualmente tra modalità Focus e Pausa")
        self.pomo_mode_switch_btn.clicked.connect(self._toggle_pomo_mode)
        pomo_hdr.addWidget(self.pomo_mode_switch_btn)
        pomo_layout.addLayout(pomo_hdr)

        subj_row = QHBoxLayout()
        subj_row.setSpacing(8)
        subj_lbl = QLabel("Materia:")
        subj_lbl.setStyleSheet("font-weight: 600; background: transparent;")
        subj_row.addWidget(subj_lbl)
        self.pomo_subject_combo = QComboBox()
        self.pomo_subject_combo.setMinimumHeight(36)
        subj_row.addWidget(self.pomo_subject_combo, 1)
        pomo_layout.addLayout(subj_row)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        btn_25 = _make_button("25m / 5m")
        btn_25.clicked.connect(lambda: self._set_pomo_preset(25, 5))
        btn_50 = _make_button("50m / 10m")
        btn_50.clicked.connect(lambda: self._set_pomo_preset(50, 10))
        preset_row.addWidget(btn_25)
        preset_row.addWidget(btn_50)

        self.pomo_custom_focus_spin = QSpinBox()
        self.pomo_custom_focus_spin.setRange(1, 180)
        self.pomo_custom_focus_spin.setValue(self._pomo_focus_mins)
        self.pomo_custom_focus_spin.setSuffix("m")
        self.pomo_custom_focus_spin.setMinimumHeight(32)
        self.pomo_custom_focus_spin.setToolTip("Minuti di Focus personalizzati")

        self.pomo_custom_break_spin = QSpinBox()
        self.pomo_custom_break_spin.setRange(1, 60)
        self.pomo_custom_break_spin.setValue(self._pomo_break_mins)
        self.pomo_custom_break_spin.setSuffix("m")
        self.pomo_custom_break_spin.setMinimumHeight(32)
        self.pomo_custom_break_spin.setToolTip("Minuti di Pausa personalizzati")

        btn_custom = _make_button("Custom")
        btn_custom.clicked.connect(
            lambda: self._set_pomo_preset(
                self.pomo_custom_focus_spin.value(),
                self.pomo_custom_break_spin.value(),
            )
        )
        preset_row.addWidget(self.pomo_custom_focus_spin)
        preset_row.addWidget(self.pomo_custom_break_spin)
        preset_row.addWidget(btn_custom)
        pomo_layout.addLayout(preset_row)

        ring_wrap = QHBoxLayout()
        ring_wrap.addStretch(1)
        self.pomo_ring = PomodoroRingWidget(pomo_card)
        p = self._active_palette()
        self.pomo_ring.set_theme_colors(
            p.get("accent") or p.get("nav_checked_border") or "#6366F1",
            p.get("border") or "#334155",
            p.get("title") if self._active_is_dark() or self._reading_mode else p.get("card_body"),
            p.get("muted") or "#94A3B8",
        )
        ring_wrap.addWidget(self.pomo_ring)
        ring_wrap.addStretch(1)
        pomo_layout.addLayout(ring_wrap)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        ctrl_row.addStretch(1)
        self.pomo_start_btn = _make_button("▶ Avvia", "primary")
        self.pomo_start_btn.setMinimumWidth(120)
        self.pomo_start_btn.clicked.connect(self._toggle_pomo_running)
        ctrl_row.addWidget(self.pomo_start_btn)

        self.pomo_reset_btn = _make_button("↺ Reset")
        self.pomo_reset_btn.clicked.connect(self._reset_pomo)
        ctrl_row.addWidget(self.pomo_reset_btn)
        ctrl_row.addStretch(1)
        pomo_layout.addLayout(ctrl_row)

        weekly_lbl = _make_label("📊 Ore studiate questa settimana", "Muted")
        weekly_lbl.setStyleSheet("font-weight: 700; margin-top: 6px; background: transparent;")
        pomo_layout.addWidget(weekly_lbl)

        self.pomo_weekly_scroll = QScrollArea()
        self.pomo_weekly_scroll.setWidgetResizable(True)
        self.pomo_weekly_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pomo_weekly_scroll.setMinimumHeight(110)
        self.pomo_weekly_container = QWidget()
        self.pomo_weekly_container.setObjectName("TransparentContainer")
        self.pomo_weekly_container.setStyleSheet("QWidget#TransparentContainer { background: transparent; }")
        self.pomo_weekly_vbox = QVBoxLayout(self.pomo_weekly_container)
        self.pomo_weekly_vbox.setContentsMargins(0, 0, 0, 0)
        self.pomo_weekly_vbox.setSpacing(6)
        self.pomo_weekly_vbox.addStretch(1)
        self.pomo_weekly_scroll.setWidget(self.pomo_weekly_container)
        pomo_layout.addWidget(self.pomo_weekly_scroll, 1)

        layout.addWidget(pomo_card, 1)

        # Colonna destra: Prossimi Esami
        exams_card = QFrame()
        exams_card.setObjectName("Card")
        exams_layout = QVBoxLayout(exams_card)
        exams_layout.setContentsMargins(14, 12, 14, 12)
        exams_layout.setSpacing(10)

        exams_hdr = QHBoxLayout()
        exams_hdr.addWidget(_make_label("🎓 Prossimi Esami", "CardTitle"))
        exams_hdr.addStretch(1)
        self.ft_add_exam_btn = _make_button("+ Aggiungi Esame", "primary")
        self.ft_add_exam_btn.clicked.connect(self._on_add_exam_clicked)
        exams_hdr.addWidget(self.ft_add_exam_btn)
        exams_layout.addLayout(exams_hdr)

        self.ft_exams_scroll = QScrollArea()
        self.ft_exams_scroll.setWidgetResizable(True)
        self.ft_exams_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.ft_exams_container = QWidget()
        self.ft_exams_container.setObjectName("TransparentContainer")
        self.ft_exams_container.setStyleSheet("QWidget#TransparentContainer { background: transparent; }")
        self.ft_exams_vbox = QVBoxLayout(self.ft_exams_container)
        self.ft_exams_vbox.setContentsMargins(0, 0, 0, 0)
        self.ft_exams_vbox.setSpacing(8)
        self.ft_exams_vbox.addStretch(1)
        self.ft_exams_scroll.setWidget(self.ft_exams_container)
        exams_layout.addWidget(self.ft_exams_scroll, 1)

        layout.addWidget(exams_card, 1)

        self._refresh_pomo_subjects()
        self._update_pomo_ui()
        self._refresh_weekly_study_summary()
        self._refresh_exam_countdowns()
        return page

    def _refresh_pomo_subjects(self):
        if not hasattr(self, "pomo_subject_combo"):
            return
        prev_data = self.pomo_subject_combo.currentData()
        self.pomo_subject_combo.blockSignals(True)
        self.pomo_subject_combo.clear()
        self.pomo_subject_combo.addItem("Studio Generale", None)
        try:
            folders = db.get_folders_tree()
            for f in folders:
                if f.get("parent_id") is None:
                    self.pomo_subject_combo.addItem(f"📁 {f.get('name', 'Materia')}", int(f["id"]))
            for f in folders:
                if f.get("parent_id") is not None:
                    self.pomo_subject_combo.addItem(f"   └ {f.get('name', 'Modulo')}", int(f["id"]))
        except Exception as exc:
            logger.warning(f"Errore caricamento materie Pomodoro: {exc}")
        if prev_data is not None:
            idx = self.pomo_subject_combo.findData(prev_data)
            if idx >= 0:
                self.pomo_subject_combo.setCurrentIndex(idx)
        self.pomo_subject_combo.blockSignals(False)

    def _set_pomo_preset(self, focus_mins: int, break_mins: int):
        self._pomo_timer.stop()
        self._pomo_running = False
        self._pomo_focus_mins = max(1, int(focus_mins))
        self._pomo_break_mins = max(1, int(break_mins))
        if hasattr(self, "pomo_custom_focus_spin"):
            self.pomo_custom_focus_spin.setValue(self._pomo_focus_mins)
        if hasattr(self, "pomo_custom_break_spin"):
            self.pomo_custom_break_spin.setValue(self._pomo_break_mins)
        self._pomo_mode = "focus"
        self._pomo_total_secs = self._pomo_focus_mins * 60
        self._pomo_remaining_secs = self._pomo_total_secs
        self._update_pomo_ui()

    def _toggle_pomo_mode(self):
        self._pomo_timer.stop()
        self._pomo_running = False
        self._pomo_mode = "break" if self._pomo_mode == "focus" else "focus"
        mins = self._pomo_focus_mins if self._pomo_mode == "focus" else self._pomo_break_mins
        self._pomo_total_secs = max(60, mins * 60)
        self._pomo_remaining_secs = self._pomo_total_secs
        self._update_pomo_ui()

    def _toggle_pomo_running(self):
        if self._pomo_running:
            self._pomo_timer.stop()
            self._pomo_running = False
        else:
            if self._pomo_remaining_secs <= 0:
                mins = self._pomo_focus_mins if self._pomo_mode == "focus" else self._pomo_break_mins
                self._pomo_total_secs = max(60, mins * 60)
                self._pomo_remaining_secs = self._pomo_total_secs
            self._pomo_running = True
            self._pomo_timer.start()
        self._update_pomo_ui()

    def _reset_pomo(self):
        self._pomo_timer.stop()
        self._pomo_running = False
        mins = self._pomo_focus_mins if self._pomo_mode == "focus" else self._pomo_break_mins
        self._pomo_total_secs = max(60, mins * 60)
        self._pomo_remaining_secs = self._pomo_total_secs
        self._update_pomo_ui()

    def _on_pomo_tick(self):
        if self._pomo_remaining_secs > 1:
            self._pomo_remaining_secs -= 1
            self._update_pomo_ui()
            return

        self._pomo_timer.stop()
        self._pomo_running = False
        self._pomo_remaining_secs = 0
        try:
            from core.audio_feedback import audio_feedback
            audio_feedback.play_soft_chime()
        except Exception:
            try:
                QApplication.beep()
            except Exception:
                pass

        if self._pomo_mode == "focus":
            subj_id = self.pomo_subject_combo.currentData() if hasattr(self, "pomo_subject_combo") else None
            note_id = getattr(self, "_nk_selected_note_id", None)
            try:
                db.add_study_session(subj_id, note_id, self._pomo_focus_mins, mode="focus")
            except Exception as exc:
                logger.warning(f"Errore salvataggio sessione Pomodoro: {exc}")
            self._refresh_weekly_study_summary()
            self._refresh_exam_countdowns()
            self._pomo_mode = "break"
            self._pomo_total_secs = max(60, self._pomo_break_mins * 60)
            self._pomo_remaining_secs = self._pomo_total_secs
        else:
            self._pomo_mode = "focus"
            self._pomo_total_secs = max(60, self._pomo_focus_mins * 60)
            self._pomo_remaining_secs = self._pomo_total_secs
        self._update_pomo_ui()

    def _update_pomo_ui(self):
        mins = max(0, self._pomo_remaining_secs) // 60
        secs = max(0, self._pomo_remaining_secs) % 60
        time_str = f"{mins:02d}:{secs:02d}"
        if hasattr(self, "pomo_ring"):
            progress = (self._pomo_remaining_secs / self._pomo_total_secs) if self._pomo_total_secs > 0 else 0.0
            mode_txt = "PAUSA" if self._pomo_mode == "break" else ("FOCUS" if self._pomo_running else "PRONTO")
            self.pomo_ring.set_state(progress, time_str, mode_txt)
        if hasattr(self, "pomo_start_btn"):
            self.pomo_start_btn.setText("⏸ Pausa" if self._pomo_running else "▶ Avvia")
        if hasattr(self, "pomo_mode_switch_btn"):
            self.pomo_mode_switch_btn.setText("🎯 Focus" if self._pomo_mode == "break" else "☕ Pausa")
        if hasattr(self, "pomo_top_pill"):
            icon = "☕" if self._pomo_mode == "break" else "⏱️"
            self.pomo_top_pill.setText(f"{icon} {time_str}")
            show_pill = self._pomo_running or (0 < self._pomo_remaining_secs < self._pomo_total_secs)
            self.pomo_top_pill.setVisible(show_pill)

    def _jump_to_flowtime_focus(self):
        self._on_nav_clicked(8)

    @staticmethod
    def _clear_vbox(vbox: QVBoxLayout):
        while vbox.count():
            item = vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def _refresh_weekly_study_summary(self):
        if not hasattr(self, "pomo_weekly_vbox"):
            return
        self._clear_vbox(self.pomo_weekly_vbox)
        try:
            rows = db.get_weekly_study_summary()
        except Exception as exc:
            logger.warning(f"Errore riepilogo settimanale studio: {exc}")
            rows = []

        if not rows:
            empty = _make_empty_state(
                "focus",
                "Nessuna sessione Focus negli ultimi 7 giorni",
                "Avvia un ciclo Pomodoro per monitorare automaticamente le tue ore di studio per materia.",
                theme_key=self._current_theme,
            )
            self.pomo_weekly_vbox.addWidget(empty)
            self.pomo_weekly_vbox.addStretch(1)
            return

        for r in rows:
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(
                "QFrame { background-color: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.18); border-radius: 8px; }"
            )
            row_l = QHBoxLayout(card)
            row_l.setContentsMargins(10, 6, 10, 6)
            row_l.setSpacing(8)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {r.get('subject_color') or '#6366F1'}; border: none; background: transparent;")
            row_l.addWidget(dot)
            name_lbl = QLabel(str(r.get("subject_name") or "Studio Generale"))
            name_lbl.setWordWrap(True)
            name_lbl.setStyleSheet("font-weight: 600; border: none; background: transparent;")
            row_l.addWidget(name_lbl, 1)
            mins = int(r.get("total_minutes") or 0)
            hrs_txt = f"{mins / 60.0:.1f}h ({mins}m)" if mins >= 60 else f"{mins} min"
            val_lbl = QLabel(f"{hrs_txt} · {int(r.get('sessions_count') or 0)} sess.")
            val_lbl.setStyleSheet("font-size: 11.5px; font-weight: 600; border: none; background: transparent;")
            row_l.addWidget(val_lbl)
            self.pomo_weekly_vbox.addWidget(card)
        self.pomo_weekly_vbox.addStretch(1)

    def _on_add_exam_clicked(self):
        try:
            folders = db.get_folders_tree()
        except Exception:
            folders = []
        dlg = ExamAddDialog(folders, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.result_data()
            if not data["exam_title"]:
                QMessageBox.information(self, "Nuovo Esame", "Inserisci il nome dell'esame.")
                return
            try:
                db.add_exam_countdown(
                    subject_id=data["subject_id"],
                    exam_title=data["exam_title"],
                    exam_date=data["exam_date"],
                    cfu=data["cfu"],
                )
            except Exception as exc:
                QMessageBox.warning(self, "Errore", f"Impossibile salvare l'esame:\n{exc}")
                return
            self._refresh_exam_countdowns()

    def _on_delete_exam_clicked(self, exam_id: int):
        try:
            db.delete_exam_countdown(exam_id)
        except Exception as exc:
            logger.warning(f"Errore eliminazione esame {exam_id}: {exc}")
        self._refresh_exam_countdowns()

    def _refresh_exam_countdowns(self):
        if not hasattr(self, "ft_exams_vbox"):
            return
        self._clear_vbox(self.ft_exams_vbox)
        try:
            exams = db.get_exam_countdowns()
        except Exception as exc:
            logger.warning(f"Errore caricamento esami: {exc}")
            exams = []

        if not exams:
            empty = _make_empty_state(
                "graduation",
                "Nessun esame in programma",
                "Aggiungi un appello per collegarlo alle tue note.",
                shortcut_hint="+ Aggiungi Esame",
                theme_key=self._current_theme,
            )
            self.ft_exams_vbox.addWidget(empty)
            self.ft_exams_vbox.addStretch(1)
            return

        today = dt_date.today()
        for ex in exams:
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(
                "QFrame { background-color: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.2); border-radius: 10px; }"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(6)

            top_r = QHBoxLayout()
            top_r.setSpacing(8)
            title_lbl = QLabel(str(ex.get("exam_title") or "Esame"))
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet("font-size: 13.5px; font-weight: 700; border: none; background: transparent;")
            top_r.addWidget(title_lbl, 1)

            try:
                ex_d = dt_date.fromisoformat(str(ex.get("exam_date") or ""))
                delta = (ex_d - today).days
            except Exception:
                delta = 0

            if delta > 0:
                badge_txt = f"-{delta} gg"
                badge_col = "#EF4444" if delta <= 7 else ("#F59E0B" if delta <= 21 else "#6366F1")
            elif delta == 0:
                badge_txt = "Oggi!"
                badge_col = "#EF4444"
            else:
                badge_txt = "Passato"
                badge_col = "#64748B"

            badge = QLabel(badge_txt)
            badge.setStyleSheet(
                f"background-color: {badge_col}; color: #FFFFFF; border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 700; border: none;"
            )
            top_r.addWidget(badge)

            del_btn = QPushButton("✕")
            del_btn.setFixedSize(24, 24)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setToolTip("Rimuovi esame")
            del_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; color: #94A3B8; font-weight: 700; }QPushButton:hover { color: #EF4444; }"
            )
            del_btn.clicked.connect(lambda _c=False, eid=int(ex["id"]): self._on_delete_exam_clicked(eid))
            top_r.addWidget(del_btn)
            cl.addLayout(top_r)

            focus_mins = int(ex.get("focus_minutes") or 0)
            focus_hrs = f"{focus_mins / 60.0:.1f}h" if focus_mins >= 60 else f"{focus_mins}m"
            meta_txt = (
                f"📁 {ex.get('subject_name') or 'Generale'} · 📅 {ex.get('exam_date')} · "
                f"{int(ex.get('cfu') or 0)} CFU · ⏱️ {focus_hrs} focus"
            )
            meta_lbl = _make_label(meta_txt, "Muted")
            meta_lbl.setWordWrap(True)
            meta_lbl.setStyleSheet("font-size: 11.5px; border: none; background: transparent;")
            cl.addWidget(meta_lbl)

            total_n = int(ex.get("total_notes") or 0)
            studied_n = int(ex.get("studied_notes") or 0)
            pct = int(round((studied_n / total_n) * 100)) if total_n > 0 else 0

            pbar = QProgressBar()
            pbar.setRange(0, 100)
            pbar.setValue(pct)
            pbar.setTextVisible(False)
            pbar.setFixedHeight(8)
            cl.addWidget(pbar)

            prog_lbl = _make_label(f"Note studiate (✅): {studied_n} / {total_n} ({pct}%)", "Muted")
            prog_lbl.setWordWrap(True)
            prog_lbl.setStyleSheet("font-size: 11px; border: none; background: transparent;")
            cl.addWidget(prog_lbl)

            self.ft_exams_vbox.addWidget(card)
        self.ft_exams_vbox.addStretch(1)

    def _copy_to_clipboard(text: str):
        QApplication.clipboard().setText(text or "")
