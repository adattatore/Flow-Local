"""
ui/flownotes_workspace.py
Componenti modulari per FlowNotes Multi-Tab, Workspace Switcher (Notion-style) e Split-View (VS Code-style):
- WorkspaceSwitcherButton: selettore dropdown in cima alla sidebar (Quaderno Locale vs FlowGroups)
- JoinOrCreateGroupDialog: mini-dialogo compatto per creare o unirsi a un gruppo Drive/Locale
- Win11TabBar: barra delle schede stile Windows 11 File Explorer con tasto +, chiusura × e middle-click
- BreadcrumbBar: percorso contestuale + azioni dirette (Acquisisci Lock, Pubblica, Copia nel Quaderno)
- GroupBoardWidget: Bacheca & Stato Sbobine per i gruppi FlowGroups
- EditorPane: pannello editor autonomo (Left / Right) con gestione focus, tab stack e autosave
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDrag, QFont, QIcon, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.flow_groups import flow_groups
from database.db_manager import db
from ui.audio_sync_bar import AudioSyncBar
from ui.icons import get_icon, resolve_icon_color
from ui.scientific_editor import DictationGhostLineEdit, ScientificNoteEditor


@dataclass
class TabContext:
    """Stato completo di una singola scheda aperta in FlowNotes."""

    tab_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    workspace_type: str = "local"  # 'local' | 'group' | 'board'
    group_id: Optional[int] = None
    group_rec: Optional[Dict[str, Any]] = None
    note_id: Optional[int] = None
    subject: str = "Generale"
    topic: str = "Generale"
    note_type: str = "appunto"
    profile: str = "Alessandro"
    title: str = "Nuova nota"
    rel_path: str = ""
    group_note_item: Optional[Dict[str, Any]] = None
    content_markdown: str = ""
    is_dirty: bool = False
    is_locked_by_me: bool = False
    lock_info: Optional[Dict[str, Any]] = None
    is_studio_view: bool = False
    is_new_empty: bool = False
    is_favorite: bool = False
    is_trashed: bool = False
    auto_title_done: bool = False
    created_by_name: str = ""
    created_by_email: str = ""
    created_by_avatar: str = ""
    last_edited_by_name: str = ""
    last_edited_by_email: str = ""
    last_edited_at_utc: str = ""

    def display_title(self, max_len: int = 24) -> str:
        base = (self.title or "Nuova nota").strip()
        if len(base) > max_len:
            base = base[: max_len - 1].rstrip() + "…"
        if self.is_trashed:
            return f"[CESTINO] {base}"
        dirty_mark = " *" if self.is_dirty else ""
        if self.workspace_type in ("group", "board") and self.group_rec:
            gname = (self.group_rec.get("group_name") or "GRP").strip()
            badge = gname[:8].upper() if len(gname) > 8 else gname.upper()
            return f"[{badge}] {base}{dirty_mark}"
        return f"{base}{dirty_mark}"

    def identity_key(self) -> tuple:
        if self.workspace_type == "local" and self.note_id is not None:
            return ("local", int(self.note_id))
        if self.workspace_type == "group" and self.group_id is not None and self.rel_path:
            return ("group", int(self.group_id), self.rel_path)
        if self.workspace_type == "board" and self.group_id is not None:
            return ("board", int(self.group_id))
        return ("tab", self.tab_id)


class JoinOrCreateGroupDialog(QDialog):
    """Mini-dialogo compatto per creare o unirsi a un FlowGroup (Drive o Locale)."""

    def __init__(
        self,
        author_name: str = "Studente",
        default_subject: str = "Generale",
        palette: Optional[Dict[str, str]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.author_name = author_name or "Studente"
        self.default_subject = default_subject or "Generale"
        self._palette = palette or {}
        self.result_group: Optional[Dict[str, Any]] = None

        self.setWindowTitle("FlowGroups — Crea o Unisciti a un Gruppo")
        self.resize(520, 340)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        hdr = QLabel("Collaborazione FlowGroups")
        hdr.setObjectName("CardTitle")
        root.addWidget(hdr)

        sub = QLabel(
            "Collega una cartella condivisa su Google Drive o una cartella locale sincronizzata "
            "(es. Google Drive Desktop / OneDrive)."
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        # Tab 1: Unisciti con Link/ID Drive
        t_drive = QWidget()
        l_drive = QVBoxLayout(t_drive)
        l_drive.setContentsMargins(8, 12, 8, 8)
        l_drive.setSpacing(8)
        l_drive.addWidget(QLabel("Link cartella Google Drive o Folder ID:"))
        self.drive_url_input = QLineEdit()
        self.drive_url_input.setMinimumHeight(36)
        self.drive_url_input.setPlaceholderText("https://drive.google.com/drive/folders/... oppure ID")
        l_drive.addWidget(self.drive_url_input)
        self.drive_name_input = QLineEdit()
        self.drive_name_input.setMinimumHeight(36)
        self.drive_name_input.setPlaceholderText("Nome gruppo personalizzato (opzionale)")
        l_drive.addWidget(self.drive_name_input)
        l_drive.addStretch(1)
        btn_join_drive = QPushButton(" Unisciti al Gruppo Drive")
        btn_join_drive.setObjectName("PrimaryBtn")
        btn_join_drive.setMinimumHeight(36)
        btn_join_drive.setIcon(get_icon("cloud", size=15))
        btn_join_drive.clicked.connect(self._on_join_drive)
        l_drive.addWidget(btn_join_drive)
        tabs.addTab(t_drive, get_icon("cloud", size=14), "Link / ID Drive")

        # Tab 2: Cartella Locale
        t_local = QWidget()
        l_local = QVBoxLayout(t_local)
        l_local.setContentsMargins(8, 12, 8, 8)
        l_local.setSpacing(8)
        l_local.addWidget(QLabel("Seleziona una cartella condivisa sul PC:"))
        row_loc = QHBoxLayout()
        row_loc.setSpacing(6)
        self.local_path_input = QLineEdit()
        self.local_path_input.setMinimumHeight(36)
        self.local_path_input.setPlaceholderText("Percorso cartella gruppo...")
        row_loc.addWidget(self.local_path_input, 1)
        btn_browse = QPushButton("Sfoglia...")
        btn_browse.setMinimumHeight(36)
        btn_browse.clicked.connect(self._on_browse_local)
        row_loc.addWidget(btn_browse)
        l_local.addLayout(row_loc)
        l_local.addStretch(1)
        btn_join_local = QPushButton(" Collega Cartella Locale")
        btn_join_local.setObjectName("PrimaryBtn")
        btn_join_local.setMinimumHeight(36)
        btn_join_local.setIcon(get_icon("folder", size=15))
        btn_join_local.clicked.connect(self._on_join_local)
        l_local.addWidget(btn_join_local)
        tabs.addTab(t_local, get_icon("folder", size=14), "Cartella Locale")

        # Tab 3: Crea Nuovo Gruppo
        t_new = QWidget()
        l_new = QVBoxLayout(t_new)
        l_new.setContentsMargins(8, 12, 8, 8)
        l_new.setSpacing(8)
        l_new.addWidget(QLabel("Nome del nuovo gruppo:"))
        self.new_name_input = QLineEdit()
        self.new_name_input.setMinimumHeight(36)
        self.new_name_input.setPlaceholderText("Es. Corso Robotica 2026")
        l_new.addWidget(self.new_name_input)
        l_new.addWidget(QLabel("Destinazione:"))
        self.new_mode_combo = QComboBox()
        self.new_mode_combo.setMinimumHeight(36)
        self.new_mode_combo.addItems(["Google Drive API (con link condivisibile)", "Cartella Locale / Drive Desktop"])
        l_new.addWidget(self.new_mode_combo)
        l_new.addStretch(1)
        btn_create = QPushButton(" Crea Nuovo Gruppo")
        btn_create.setObjectName("PrimaryBtn")
        btn_create.setMinimumHeight(36)
        btn_create.setIcon(get_icon("plus", size=15))
        btn_create.clicked.connect(self._on_create_group)
        l_new.addWidget(btn_create)
        tabs.addTab(t_new, get_icon("plus", size=14), "Crea Gruppo")

        root.addWidget(tabs, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        btn_cancel = QPushButton("Annulla")
        btn_cancel.setMinimumHeight(32)
        btn_cancel.clicked.connect(self.reject)
        foot.addWidget(btn_cancel)
        root.addLayout(foot)

    def _on_browse_local(self):
        chosen = QFileDialog.getExistingDirectory(self, "Seleziona cartella FlowGroup")
        if chosen:
            self.local_path_input.setText(chosen)

    def _on_join_drive(self):
        url_or_id = self.drive_url_input.text().strip()
        if not url_or_id:
            QMessageBox.information(self, "FlowGroups", "Inserisci un link o ID cartella Google Drive.")
            return
        try:
            self.result_group = flow_groups.join_group(
                folder_id_or_url=url_or_id,
                group_name=self.drive_name_input.text().strip(),
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Errore Unione Drive", str(exc))

    def _on_join_local(self):
        lpath = self.local_path_input.text().strip()
        if not lpath:
            QMessageBox.information(self, "FlowGroups", "Seleziona una cartella locale.")
            return
        try:
            self.result_group = flow_groups.join_group(local_fallback_path=lpath)
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Errore Cartella Locale", str(exc))

    def _on_create_group(self):
        name = self.new_name_input.text().strip()
        if not name:
            QMessageBox.information(self, "FlowGroups", "Inserisci il nome del gruppo.")
            return
        use_local = (self.new_mode_combo.currentIndex() == 1)
        local_dir = ""
        if use_local:
            local_dir = QFileDialog.getExistingDirectory(self, "Seleziona cartella per il nuovo FlowGroup")
            if not local_dir:
                return
        try:
            self.result_group = flow_groups.create_group(
                group_name=name,
                subjects=[self.default_subject, "Generale"],
                author=self.author_name,
                local_fallback_path=local_dir,
                share_link=True,
            )
            if self.result_group.get("share_link"):
                from PyQt6.QtWidgets import QApplication
                cb = QApplication.clipboard()
                if cb is not None:
                    cb.setText(self.result_group["share_link"])
                QMessageBox.information(
                    self,
                    "Gruppo Creato",
                    f"Gruppo '{name}' creato.\nLink condivisibile copiato negli appunti:\n{self.result_group['share_link']}",
                )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Errore Creazione Gruppo", str(exc))


class WorkspaceSwitcherButton(QPushButton):
    """Selettore Workspace stile Notion in cima alla sidebar di FlowNotes (zero emoji, icone qtawesome)."""

    workspaceSelected = pyqtSignal(object)  # None (Locale) oppure group_id (int)
    joinOrCreateRequested = pyqtSignal()
    removeGroupRequested = pyqtSignal(int)

    def __init__(self, theme_key: str = "obsidian", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme_key = theme_key
        self._palette: Dict[str, str] = {}
        self._active_group_id: Optional[int] = None
        self._groups: List[Dict[str, Any]] = []

        self.setObjectName("NKWorkspaceSwitcher")
        self.setMinimumHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._open_menu)
        self._refresh_button_label()

    def set_theme(self, theme_key: str, palette: Dict[str, str]):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        self._refresh_button_label()
        self._apply_style()

    def _apply_style(self):
        p = self._palette
        bg = p.get("card_bg") or p.get("input_bg") or "rgba(148, 163, 184, 0.08)"
        hover = p.get("nav_hover") or p.get("btn_hover_bg") or "rgba(148, 163, 184, 0.16)"
        border = p.get("card_border") or p.get("border") or "#334155"
        fg = p.get("title") or p.get("text") or "#F8FAFC"
        self.setStyleSheet(
            f"QPushButton#NKWorkspaceSwitcher {{ "
            f"background-color: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 8px; padding: 6px 10px; text-align: left; font-weight: 600; font-size: 12.5px; }}"
            f"QPushButton#NKWorkspaceSwitcher:hover {{ background-color: {hover}; }}"
        )

    def set_state(self, groups: List[Dict[str, Any]], active_group_id: Optional[int]):
        self._groups = list(groups or [])
        self._active_group_id = active_group_id
        self._refresh_button_label()

    def _refresh_button_label(self):
        if self._active_group_id is None:
            self.setIcon(get_icon("notes", theme_key=self._theme_key, role="accent", size=15))
            self.setText("  Il mio Quaderno (Locale)  ▾")
            self.setToolTip("Workspace attivo: Il mio Quaderno (Database SQLite locale)")
            return
        g = next((x for x in self._groups if int(x["id"]) == int(self._active_group_id)), None)
        if not g:
            self._active_group_id = None
            self._refresh_button_label()
            return
        is_local = bool((g.get("local_fallback_path") or "").strip())
        icon_name = "folder" if is_local else "cloud"
        prefix = "[Locale]" if is_local else "[Drive]"
        self.setIcon(get_icon(icon_name, theme_key=self._theme_key, role="accent", size=15))
        name = (g.get("group_name") or "Gruppo").strip()
        if len(name) > 19:
            name = name[:18] + "…"
        self.setText(f"  {prefix} {name}  ▾")
        self.setToolTip(f"Workspace Gruppo attivo: {prefix} {g.get('group_name')}")

    def _menu_qss(self) -> str:
        p = self._palette
        bg = p.get("combo_popup_bg") or p.get("dialog_bg") or p.get("card_bg") or "#1E293B"
        fg = p.get("text") or "#F8FAFC"
        bd = p.get("card_border") or p.get("border") or "#334155"
        hv = p.get("nav_hover") or p.get("badge_bg") or "#334155"
        return (
            f"QMenu {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 8px; padding: 5px; min-width: 250px; }}"
            f"QMenu::item {{ padding: 7px 12px; border-radius: 6px; font-size: 12.5px; }}"
            f"QMenu::item:selected {{ background-color: {hv}; }}"
            f"QMenu::separator {{ height: 1px; background-color: {bd}; margin: 4px 8px; }}"
        )

    def _open_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_qss())

        act_local = menu.addAction(
            get_icon("notes", theme_key=self._theme_key, role="accent" if self._active_group_id is None else "text", size=15),
            "Il mio Quaderno (Locale)",
        )
        act_local.setCheckable(True)
        act_local.setChecked(self._active_group_id is None)
        act_local.triggered.connect(lambda: self.workspaceSelected.emit(None))

        if self._groups:
            menu.addSeparator()
            for g in self._groups:
                gid = int(g["id"])
                is_local = bool((g.get("local_fallback_path") or "").strip())
                prefix = "[Locale]" if is_local else "[Drive]"
                icon_name = "folder" if is_local else "cloud"
                is_cur = (self._active_group_id is not None and int(self._active_group_id) == gid)
                act = menu.addAction(
                    get_icon(icon_name, theme_key=self._theme_key, role="accent" if is_cur else "text", size=15),
                    f"{prefix} {g.get('group_name')}",
                )
                act.setCheckable(True)
                act.setChecked(is_cur)
                act.triggered.connect(lambda _chk=False, target_id=gid: self.workspaceSelected.emit(target_id))

        menu.addSeparator()
        act_add = menu.addAction(
            get_icon("plus", theme_key=self._theme_key, role="accent", size=15),
            "Crea o Unisciti a un Gruppo...",
        )
        act_add.triggered.connect(self.joinOrCreateRequested.emit)

        if self._active_group_id is not None:
            act_rem = menu.addAction(
                get_icon("trash", theme_key=self._theme_key, role="danger", size=14),
                "Scollega gruppo corrente...",
            )
            cur_gid = int(self._active_group_id)
            act_rem.triggered.connect(lambda: self.removeGroupRequested.emit(cur_gid))

        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))


class Win11TabBar(QWidget):
    """Barra delle schede stile Windows 11 File Explorer con pulsante + e middle-click per chiudere."""

    tabSelected = pyqtSignal(int)
    tabCloseRequested = pyqtSignal(int)
    tabMoved = pyqtSignal(int, int)
    newTabRequested = pyqtSignal()
    splitTabRequested = pyqtSignal(int)
    duplicateTabRequested = pyqtSignal(int)
    closeOtherTabsRequested = pyqtSignal(int)
    closeAllTabsRequested = pyqtSignal()
    nodeDroppedFromTree = pyqtSignal(object)

    def __init__(self, theme_key: str = "obsidian", is_dark: bool = True, pane_id: str = "left", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme_key = theme_key
        self._is_dark = is_dark
        self._pane_id = pane_id
        self._palette: Dict[str, str] = {}
        self._drag_start_pos: Optional[QPoint] = None
        self._drag_tab_idx: int = -1
        self.setObjectName("NKWin11TabStrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 0)
        lay.setSpacing(4)

        self.tab_bar = QTabBar(self)
        self.tab_bar.setObjectName("NKWin11TabBar")
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(True)
        self.tab_bar.setUsesScrollButtons(True)
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setElideMode(Qt.TextElideMode.ElideRight)
        self.tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_bar.currentChanged.connect(self.tabSelected.emit)
        self.tab_bar.tabMoved.connect(self.tabMoved.emit)
        self.tab_bar.customContextMenuRequested.connect(self._on_context_menu)
        self.tab_bar.installEventFilter(self)
        lay.addWidget(self.tab_bar, 1)

        self.new_tab_btn = QPushButton(self)
        self.new_tab_btn.setObjectName("NKNewTabBtn")
        self.new_tab_btn.setFixedSize(28, 28)
        self.new_tab_btn.setIcon(get_icon("plus", theme_key=theme_key, role="subtitle", size=14))
        self.new_tab_btn.setToolTip("Nuova scheda (Ctrl+N)")
        self.new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_tab_btn.clicked.connect(self.newTabRequested.emit)
        lay.addWidget(self.new_tab_btn)

    def set_theme(self, theme_key: str, palette: Dict[str, str], is_dark: Optional[bool] = None):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        if is_dark is not None:
            self._is_dark = bool(is_dark)
        self.new_tab_btn.setIcon(get_icon("plus", theme_key=theme_key, role="subtitle", size=14))
        p = self._palette
        strip_bg = (
            p.get("sidebar_bg")
            or p.get("nav_bg")
            or ("#0D1117" if self._is_dark else "#ECEFF5")
        )
        sel_bg = p.get("editor_bg") or p.get("card_bg") or ("#161B24" if self._is_dark else "#FFFFFF")
        bd = p.get("card_border") or p.get("border") or ("#2A3342" if self._is_dark else "#D8DEE9")
        fg = p.get("subtitle") or ("#94A3B8" if self._is_dark else "#64748B")
        sel_fg = p.get("title") or ("#F8FAFC" if self._is_dark else "#0F172A")
        tab_hv = "rgba(255, 255, 255, 0.06)" if self._is_dark else "rgba(15, 23, 42, 0.05)"
        btn_hv = "rgba(255, 255, 255, 0.10)" if self._is_dark else "rgba(15, 23, 42, 0.07)"
        self.setStyleSheet(
            f"QWidget#NKWin11TabStrip {{ background-color: {strip_bg}; "
            f"border-top-left-radius: 10px; border-top-right-radius: 10px; }}"
        )
        self.tab_bar.setStyleSheet(
            f"QTabBar#NKWin11TabBar {{ background: transparent; border: none; }}"
            f"QTabBar#NKWin11TabBar::tab {{ "
            f"background-color: transparent; color: {fg}; border: 1px solid transparent; "
            f"border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; "
            f"padding: 7px 8px 7px 12px; margin-right: 2px; min-width: 110px; max-width: 220px; font-size: 12px; }}"
            f"QTabBar#NKWin11TabBar::tab:hover {{ background-color: {tab_hv}; color: {sel_fg}; }}"
            f"QTabBar#NKWin11TabBar::tab:selected {{ "
            f"background-color: {sel_bg}; color: {sel_fg}; border: 1px solid {bd}; "
            f"border-bottom: none; font-weight: 600; }}"
        )
        self.new_tab_btn.setStyleSheet(
            f"QPushButton#NKNewTabBtn {{ background: transparent; border: none; border-radius: 6px; "
            f"min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; padding: 0px; margin-bottom: 3px; }}"
            f"QPushButton#NKNewTabBtn:hover {{ background-color: {btn_hv}; }}"
        )

    def sync_tabs(self, tabs: List[TabContext], active_idx: int):
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count() > 0:
            self.tab_bar.removeTab(0)

        for i, tab in enumerate(tabs):
            if tab.workspace_type == "board":
                ic = get_icon("board", theme_key=self._theme_key, role="accent", size=13)
            elif tab.workspace_type == "group":
                is_loc = bool(tab.group_rec and (tab.group_rec.get("local_fallback_path") or "").strip())
                ic = get_icon("folder" if is_loc else "cloud", theme_key=self._theme_key, role="accent", size=13)
            else:
                ic = get_icon("file", theme_key=self._theme_key, role="subtitle", size=13)

            idx = self.tab_bar.addTab(ic, tab.display_title())
            self.tab_bar.setTabToolTip(idx, f"{tab.title} ({'Gruppo' if tab.workspace_type != 'local' else 'Locale'})")

            close_btn = QPushButton()
            close_btn.setObjectName("NKTabCloseBtn")
            close_btn.setFixedSize(18, 18)
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.setIcon(get_icon("close", theme_key=self._theme_key, role="subtitle", size=10))
            close_btn.setToolTip("Chiudi scheda (Ctrl+W)")
            close_hv = "rgba(255, 255, 255, 0.11)" if self._is_dark else "rgba(15, 23, 42, 0.08)"
            close_pr = "rgba(255, 255, 255, 0.16)" if self._is_dark else "rgba(15, 23, 42, 0.14)"
            close_btn.setStyleSheet(
                "QPushButton#NKTabCloseBtn { background: transparent; border: none; border-radius: 4px; "
                "min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px; padding: 0px; margin: 0px; }"
                f"QPushButton#NKTabCloseBtn:hover {{ background-color: {close_hv}; }}"
                f"QPushButton#NKTabCloseBtn:pressed {{ background-color: {close_pr}; }}"
            )
            close_btn.clicked.connect(lambda _chk=False, btn=close_btn: self._on_close_btn_clicked(btn))
            self.tab_bar.setTabButton(idx, QTabBar.ButtonPosition.RightSide, close_btn)

        if 0 <= active_idx < self.tab_bar.count():
            self.tab_bar.setCurrentIndex(active_idx)
        self.tab_bar.blockSignals(False)

    def _on_close_btn_clicked(self, btn: QPushButton):
        for idx in range(self.tab_bar.count()):
            if self.tab_bar.tabButton(idx, QTabBar.ButtonPosition.RightSide) is btn:
                self.tabCloseRequested.emit(idx)
                return

    def update_tab_title(self, idx: int, tab: TabContext):
        if 0 <= idx < self.tab_bar.count():
            self.tab_bar.setTabText(idx, tab.display_title())

    def eventFilter(self, watched, event):
        if watched is self.tab_bar:
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.MiddleButton:
                    idx = self.tab_bar.tabAt(event.pos())
                    if idx >= 0:
                        self.tabCloseRequested.emit(idx)
                        return True
                elif event.button() == Qt.MouseButton.LeftButton:
                    self._drag_start_pos = event.pos()
                    self._drag_tab_idx = self.tab_bar.tabAt(event.pos())
            elif etype == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                if (
                    (event.buttons() & Qt.MouseButton.LeftButton)
                    and self._drag_start_pos is not None
                    and self._drag_tab_idx >= 0
                ):
                    pos = event.pos()
                    # Se l'utente trascina fuori dalla barra delle schede (stile Chrome), avvia il drag tra pannelli
                    if pos.y() < -14 or pos.y() > self.tab_bar.height() + 14:
                        idx = self._drag_tab_idx
                        self._drag_start_pos = None
                        self._drag_tab_idx = -1
                        if 0 <= idx < self.tab_bar.count():
                            drag = QDrag(self)
                            mime = QMimeData()
                            payload = json.dumps({"source_pane_id": self._pane_id, "tab_idx": idx}).encode("utf-8")
                            mime.setData("application/x-flownotes-tab", payload)
                            drag.setMimeData(mime)
                            t_rect = self.tab_bar.tabRect(idx)
                            if t_rect.isValid():
                                pix = self.tab_bar.grab(t_rect)
                                drag.setPixmap(pix)
                                drag.setHotSpot(QPoint(min(t_rect.width() // 2, 60), t_rect.height() // 2))
                            drag.exec(Qt.DropAction.MoveAction)
                            return True
            elif etype == QEvent.Type.MouseButtonRelease:
                self._drag_start_pos = None
                self._drag_tab_idx = -1
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            try:
                raw = bytes(event.mimeData().data("application/x-flownotes-node")).decode("utf-8")
                data = json.loads(raw)
                if data:
                    self.nodeDroppedFromTree.emit(tuple(data))
                    event.acceptProposedAction()
                    return
            except Exception:
                pass
        super().dropEvent(event)

    def _on_context_menu(self, pos: QPoint):
        idx = self.tab_bar.tabAt(pos)
        if idx < 0:
            return
        menu = QMenu(self)
        p = self._palette
        bg = p.get("combo_popup_bg") or p.get("card_bg") or "#1E293B"
        fg = p.get("text") or "#F8FAFC"
        bd = p.get("card_border") or "#334155"
        hv = p.get("nav_hover") or "#334155"
        menu.setStyleSheet(
            f"QMenu {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 12px; border-radius: 5px; }}"
            f"QMenu::item:selected {{ background-color: {hv}; }}"
        )
        act_split = menu.addAction(get_icon("split_h", theme_key=self._theme_key, size=14), "Sposta nel pannello affiancato (Ctrl+\\)")
        act_split.triggered.connect(lambda: self.splitTabRequested.emit(idx))
        act_dup = menu.addAction(get_icon("copy", theme_key=self._theme_key, size=14), "Duplica scheda")
        act_dup.triggered.connect(lambda: self.duplicateTabRequested.emit(idx))
        menu.addSeparator()
        act_close = menu.addAction(get_icon("close", theme_key=self._theme_key, size=14), "Chiudi scheda (Ctrl+W)")
        act_close.triggered.connect(lambda: self.tabCloseRequested.emit(idx))
        act_others = menu.addAction("Chiudi altre schede")
        act_others.triggered.connect(lambda: self.closeOtherTabsRequested.emit(idx))
        act_all = menu.addAction("Chiudi tutte")
        act_all.triggered.connect(self.closeAllTabsRequested.emit)
        menu.exec(self.tab_bar.mapToGlobal(pos))


class BreadcrumbBar(QWidget):
    """Barra unificata: Breadcrumb cliccabile a sinistra + azioni Gruppo/Editor a destra."""

    acquireLockRequested = pyqtSignal()
    publishGroupRequested = pyqtSignal()
    copyToLocalRequested = pyqtSignal()
    publishLocalToGroupRequested = pyqtSignal()
    rootClicked = pyqtSignal()
    subjectClicked = pyqtSignal(str)

    def __init__(self, theme_key: str = "obsidian", is_dark: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme_key = theme_key
        self._is_dark = is_dark
        self._palette: Dict[str, str] = {}
        self._current_subject: str = "Generale"
        self.setObjectName("NKBreadcrumbBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 10, 6)
        lay.setSpacing(4)

        self.root_btn = QPushButton("Il mio Quaderno")
        self.root_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.root_btn.clicked.connect(self.rootClicked.emit)
        lay.addWidget(self.root_btn)

        self.sep_1 = QLabel("›")
        lay.addWidget(self.sep_1)

        self.subj_btn = QPushButton("Generale")
        self.subj_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.subj_btn.clicked.connect(lambda: self.subjectClicked.emit(self._current_subject))
        lay.addWidget(self.subj_btn)

        self.sep_2 = QLabel("›")
        lay.addWidget(self.sep_2)

        self.path_lbl = QLabel("")
        self.path_lbl.setObjectName("NKBreadcrumbPath")
        self.path_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.path_lbl, 1)

        # Stato Lock + Azioni Gruppo
        self.lock_pill = QLabel("")
        self.lock_pill.setObjectName("NKLockPill")
        self.lock_pill.setVisible(False)
        lay.addWidget(self.lock_pill)

        self.btn_acquire_lock = QPushButton(" Acquisisci Lock")
        self.btn_acquire_lock.setObjectName("NKToolbarPill")
        self.btn_acquire_lock.setFixedHeight(28)
        self.btn_acquire_lock.setIcon(get_icon("lock_open", theme_key=theme_key, role="text", size=13))
        self.btn_acquire_lock.clicked.connect(self.acquireLockRequested.emit)
        self.btn_acquire_lock.setVisible(False)
        lay.addWidget(self.btn_acquire_lock)

        self.btn_publish_group = QPushButton(" Salva nel Gruppo")
        self.btn_publish_group.setObjectName("NKActionBtn")
        self.btn_publish_group.setFixedHeight(28)
        self.btn_publish_group.setIcon(get_icon("upload", theme_key=theme_key, role="accent", size=13))
        self.btn_publish_group.clicked.connect(self.publishGroupRequested.emit)
        self.btn_publish_group.setVisible(False)
        lay.addWidget(self.btn_publish_group)

        self.btn_copy_local = QPushButton(" Copia nel Quaderno")
        self.btn_copy_local.setObjectName("NKToolbarPill")
        self.btn_copy_local.setFixedHeight(28)
        self.btn_copy_local.setIcon(get_icon("copy", theme_key=theme_key, role="text", size=13))
        self.btn_copy_local.clicked.connect(self.copyToLocalRequested.emit)
        self.btn_copy_local.setVisible(False)
        lay.addWidget(self.btn_copy_local)

        # Azione rapida per nota Locale
        self.btn_publish_local = QPushButton(" Pubblica in un Gruppo...")
        self.btn_publish_local.setObjectName("NKToolbarPill")
        self.btn_publish_local.setFixedHeight(28)
        self.btn_publish_local.setIcon(get_icon("cloud", theme_key=theme_key, role="text", size=13))
        self.btn_publish_local.clicked.connect(self.publishLocalToGroupRequested.emit)
        self.btn_publish_local.setVisible(False)
        lay.addWidget(self.btn_publish_local)

        # Slot per la strip di azioni condivisa dell'editor (Vista Studio, RSVP, Whis icon, ..., Time Machine, Split, Focus)
        self.toolbar_slot = QHBoxLayout()
        self.toolbar_slot.setContentsMargins(0, 0, 0, 0)
        self.toolbar_slot.setSpacing(4)
        lay.addLayout(self.toolbar_slot)

    def set_theme(self, theme_key: str, palette: Dict[str, str], is_dark: Optional[bool] = None):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        if is_dark is not None:
            self._is_dark = bool(is_dark)
        surface_bg = self._palette.get("editor_bg") or self._palette.get("card_bg") or ("#161B24" if self._is_dark else "#FFFFFF")
        soft_bd = self._palette.get("card_border") or self._palette.get("border") or ("#2A3342" if self._is_dark else "#E2E8F0")
        self.setStyleSheet(
            f"QWidget#NKBreadcrumbBar {{ background-color: {surface_bg}; border-bottom: 1px solid {soft_bd}; }}"
        )
        sub_col = self._palette.get("subtitle") or "#94A3B8"
        title_col = self._palette.get("title") or "#F8FAFC"
        btn_qss = (
            f"QPushButton {{ background: transparent; border: none; color: {sub_col}; "
            f"font-size: 11.5px; padding: 2px 4px; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {title_col}; background-color: rgba(148,163,184,0.12); }}"
        )
        self.root_btn.setStyleSheet(btn_qss)
        self.subj_btn.setStyleSheet(btn_qss)
        self.sep_1.setStyleSheet(f"color: {sub_col}; font-size: 11.5px; background: transparent;")
        self.sep_2.setStyleSheet(f"color: {sub_col}; font-size: 11.5px; background: transparent;")
        self.path_lbl.setStyleSheet(f"color: {title_col}; font-size: 11.5px; font-weight: 600; background: transparent;")
        self.btn_acquire_lock.setIcon(get_icon("lock_open", theme_key=theme_key, role="text", size=13))
        self.btn_publish_group.setIcon(get_icon("upload", theme_key=theme_key, role="accent", size=13))
        self.btn_copy_local.setIcon(get_icon("copy", theme_key=theme_key, role="text", size=13))
        self.btn_publish_local.setIcon(get_icon("cloud", theme_key=theme_key, role="text", size=13))

    def update_from_tab(self, tab: Optional[TabContext]):
        if tab is None:
            self.root_btn.setText("Il mio Quaderno")
            self.sep_1.setVisible(False)
            self.subj_btn.setVisible(False)
            self.sep_2.setVisible(False)
            self.path_lbl.setText("")
            self.lock_pill.setVisible(False)
            self.btn_acquire_lock.setVisible(False)
            self.btn_publish_group.setVisible(False)
            self.btn_copy_local.setVisible(False)
            self.btn_publish_local.setVisible(False)
            return

        if tab.workspace_type == "board":
            gname = (tab.group_rec or {}).get("group_name") or "Gruppo"
            self.root_btn.setText(gname)
            self.sep_1.setVisible(True)
            self.subj_btn.setVisible(False)
            self.sep_2.setVisible(False)
            self.path_lbl.setText("Bacheca & Stato Sbobine")
            self.lock_pill.setVisible(False)
            self.btn_acquire_lock.setVisible(False)
            self.btn_publish_group.setVisible(False)
            self.btn_copy_local.setVisible(False)
            self.btn_publish_local.setVisible(False)
            return

        if tab.workspace_type == "group":
            gname = (tab.group_rec or {}).get("group_name") or "Gruppo"
            subj = tab.subject or "Generale"
            title = tab.title or "Nota"
            self._current_subject = subj
            self.root_btn.setText(gname)
            self.sep_1.setVisible(True)
            self.subj_btn.setText(subj)
            self.subj_btn.setVisible(True)
            self.sep_2.setVisible(True)
            self.path_lbl.setText(title if len(title) <= 28 else title[:27] + "…")

            self.btn_publish_local.setVisible(False)
            self.btn_copy_local.setVisible(True)
            self.lock_pill.setVisible(True)

            locked_other = bool((tab.group_note_item or {}).get("locked_by_other"))
            lock_auth = (tab.group_note_item or {}).get("lock_author") or (tab.lock_info or {}).get("author") or ""
            if tab.is_locked_by_me:
                col = resolve_icon_color(self._theme_key, "success")
                self.lock_pill.setText("Modifica attiva (Lock tuo)")
                self.lock_pill.setStyleSheet(
                    f"color: {col}; font-size: 11px; font-weight: 600; background: transparent; padding: 2px 6px;"
                )
                self.btn_acquire_lock.setVisible(False)
                self.btn_publish_group.setVisible(True)
                self.btn_publish_group.setEnabled(True)
            elif locked_other:
                col = resolve_icon_color(self._theme_key, "warning")
                self.lock_pill.setText(f"Bloccato da {lock_auth}")
                self.lock_pill.setStyleSheet(
                    f"color: {col}; font-size: 11px; font-weight: 600; background: transparent; padding: 2px 6px;"
                )
                self.btn_acquire_lock.setVisible(True)
                self.btn_acquire_lock.setEnabled(False)
                self.btn_acquire_lock.setToolTip(f"In modifica da {lock_auth}")
                self.btn_publish_group.setVisible(False)
            else:
                col = resolve_icon_color(self._theme_key, "muted")
                self.lock_pill.setText("Sola lettura")
                self.lock_pill.setStyleSheet(
                    f"color: {col}; font-size: 11px; background: transparent; padding: 2px 6px;"
                )
                self.btn_acquire_lock.setVisible(True)
                self.btn_acquire_lock.setEnabled(True)
                self.btn_acquire_lock.setToolTip("Acquisisci il lock distribuito (.lock) per modificare questa nota")
                self.btn_publish_group.setVisible(False)
            return

        # Local note
        subj = tab.subject or "Generale"
        title = tab.title or "Nuova nota"
        self._current_subject = subj
        self.root_btn.setText("Cestino" if tab.is_trashed else "Il mio Quaderno")
        self.sep_1.setVisible(True)
        self.subj_btn.setText(subj)
        self.subj_btn.setVisible(True)
        self.sep_2.setVisible(True)
        self.path_lbl.setText(title if len(title) <= 28 else title[:27] + "…")
        self.lock_pill.setVisible(False)
        self.btn_acquire_lock.setVisible(False)
        self.btn_publish_group.setVisible(False)
        self.btn_copy_local.setVisible(False)
        self.btn_publish_local.setVisible(not tab.is_trashed)


class GroupBoardWidget(QWidget):
    """Bacheca & Stato Sbobine del gruppo: riepilogo lezioni, autori e file .lock attivi."""

    openGroupNoteRequested = pyqtSignal(dict)
    refreshRequested = pyqtSignal()

    def __init__(self, theme_key: str = "obsidian", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._theme_key = theme_key
        self._palette: Dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(10)

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(8)
        self.title_lbl = QLabel("Bacheca & Stato Sbobine")
        self.title_lbl.setObjectName("CardTitle")
        hdr_row.addWidget(self.title_lbl, 1)

        self.refresh_btn = QPushButton(" Aggiorna")
        self.refresh_btn.setObjectName("NKToolbarPill")
        self.refresh_btn.setFixedHeight(28)
        self.refresh_btn.setIcon(get_icon("refresh", theme_key=theme_key, role="text", size=14))
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        hdr_row.addWidget(self.refresh_btn)
        root.addLayout(hdr_row)

        self.meta_lbl = QLabel("")
        self.meta_lbl.setObjectName("Muted")
        self.meta_lbl.setWordWrap(True)
        root.addWidget(self.meta_lbl)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_inner = QWidget()
        self.scroll_vbox = QVBoxLayout(self.scroll_inner)
        self.scroll_vbox.setContentsMargins(0, 0, 4, 8)
        self.scroll_vbox.setSpacing(8)
        self.scroll.setWidget(self.scroll_inner)
        root.addWidget(self.scroll, 1)

    def set_theme(self, theme_key: str, palette: Dict[str, str]):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        self.refresh_btn.setIcon(get_icon("refresh", theme_key=theme_key, role="text", size=14))

    def populate(self, board_data: Dict[str, Any]):
        while self.scroll_vbox.count():
            it = self.scroll_vbox.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        gname = board_data.get("group_name") or "Gruppo"
        mode_lbl = "Cartella Locale" if board_data.get("mode") == "local" else "Google Drive"
        src = board_data.get("source") or ""
        notes = board_data.get("notes") or []
        locks = board_data.get("active_locks") or []
        members = board_data.get("members") or []

        self.title_lbl.setText(f"{gname} — Bacheca & Stato Sbobine")
        members_txt = ", ".join(members) if members else "Membri collaborativi"
        self.meta_lbl.setText(
            f"Sorgente: {mode_lbl} ({src})  ·  Note: {len(notes)}  ·  Lock attivi: {len(locks)}  ·  Autori: {members_txt}"
        )

        p = self._palette
        card_bg = p.get("card_bg") or "rgba(148,163,184,0.08)"
        card_bd = p.get("card_border") or p.get("border") or "#334155"

        if locks:
            sec_lock = QLabel("File attualmente in modifica (.lock attivi)")
            sec_lock.setObjectName("NavSectionHeader")
            self.scroll_vbox.addWidget(sec_lock)
            for lk in locks:
                card = QFrame()
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                card.setStyleSheet(
                    f"QFrame {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
                )
                cl = QHBoxLayout(card)
                cl.setContentsMargins(10, 8, 10, 8)
                cl.setSpacing(8)
                ic = QLabel()
                ic.setPixmap(get_icon("lock", theme_key=self._theme_key, role="warning", size=16).pixmap(QSize(16, 16)))
                cl.addWidget(ic)
                info = QLabel(f"[{lk.get('subject')}] {lk.get('title')} — Bloccato da {lk.get('lock_author') or 'Studente'}")
                info.setWordWrap(True)
                info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                cl.addWidget(info, 1)
                self.scroll_vbox.addWidget(card)

        sec_notes = QLabel("Lezioni & Sbobine del Gruppo")
        sec_notes.setObjectName("NavSectionHeader")
        self.scroll_vbox.addWidget(sec_notes)

        if not notes:
            empty = QLabel("Nessuna nota ancora pubblicata in questo gruppo. Seleziona una nota locale e clicca 'Pubblica in un Gruppo...'.")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.scroll_vbox.addWidget(empty)
        else:
            for item in notes:
                card = QFrame()
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                card.setStyleSheet(
                    f"QFrame {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
                )
                cl = QHBoxLayout(card)
                cl.setContentsMargins(10, 8, 10, 8)
                cl.setSpacing(8)

                is_locked = bool(item.get("lock_info"))
                ic_name = "lock" if is_locked else "file"
                ic_role = "warning" if item.get("locked_by_other") else ("success" if item.get("locked_by_me") else "subtitle")
                ic_lbl = QLabel()
                ic_lbl.setPixmap(get_icon(ic_name, theme_key=self._theme_key, role=ic_role, size=16).pixmap(QSize(16, 16)))
                cl.addWidget(ic_lbl)

                status_txt = ""
                if item.get("locked_by_other"):
                    status_txt = f" · Bloccato da {item.get('lock_author')}"
                elif item.get("locked_by_me"):
                    status_txt = " · In modifica (tu)"

                pub_name = (item.get("created_by_name") or item.get("last_edited_by_name") or "").strip()
                edit_name = (item.get("last_edited_by_name") or "").strip()
                raw_ts = (item.get("last_edited_at_utc") or item.get("modified_at") or "").strip()
                mod_txt = raw_ts[:16].replace("T", " ") if raw_ts else ""
                date_short = ""
                if len(raw_ts) >= 10:
                    try:
                        from datetime import datetime as _dt
                        _d = _dt.strptime(raw_ts[:10], "%Y-%m-%d")
                        _months = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]
                        date_short = f"{_d.day} {_months[_d.month - 1]}"
                    except Exception:
                        date_short = raw_ts[:10]

                if pub_name:
                    initials = "".join(w[0].upper() for w in pub_name.split()[:2]) or "U"
                    av_badge = QLabel(initials)
                    av_badge.setFixedSize(26, 26)
                    av_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    accent_col = p.get("accent") or "#6366F1"
                    av_badge.setStyleSheet(
                        f"background-color: {accent_col}; color: #FFFFFF; border-radius: 13px; "
                        "font-size: 10px; font-weight: 700; border: none;"
                    )
                    cl.addWidget(av_badge)

                author_line = f"Pubblicata da {pub_name}" if pub_name else ""
                if date_short and author_line:
                    author_line += f" • {date_short}"
                elif mod_txt and not author_line:
                    author_line = mod_txt
                if edit_name and pub_name and edit_name != pub_name:
                    author_line += f" · Modificata da {edit_name}"

                lbl = QLabel(f"[{item.get('subject')}] {item.get('title')}{status_txt}\n{author_line}")
                lbl.setWordWrap(True)
                lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                cl.addWidget(lbl, 1)

                open_btn = QPushButton("Apri")
                open_btn.setObjectName("NKToolbarPill")
                open_btn.setFixedHeight(26)
                open_btn.clicked.connect(lambda _chk=False, it=item: self.openGroupNoteRequested.emit(it))
                cl.addWidget(open_btn)
                self.scroll_vbox.addWidget(card)

        self.scroll_vbox.addStretch(1)


class _AutoTitleWorker(QThread):
    """Worker asincrono non bloccante per generare il titolo della nota dal contenuto."""

    titleReady = pyqtSignal(str, str)  # (tab_id, generated_title)

    def __init__(self, tab_id: str, content_md: str, folder_name: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._tab_id = tab_id
        self._content_md = content_md
        self._folder_name = folder_name

    def run(self):
        try:
            from core.ai_pipeline import LLMRefiner
            title = LLMRefiner.generate_note_title(self._content_md, folder_name=self._folder_name)
            if title:
                self.titleReady.emit(self._tab_id, title)
        except Exception:
            pass


class EditorPane(QWidget):
    """Pannello Editor autonomo (Left / Right) con TabBar Win11, Breadcrumb, Editor e Bacheca."""

    activated = pyqtSignal(object)          # self
    activeTabChanged = pyqtSignal(object)   # TabContext | None
    lastTabClosed = pyqtSignal(object)      # self
    localNoteSaved = pyqtSignal(int, str, bool)  # note_id, title, was_new
    studioViewChanged = pyqtSignal(bool)
    rsvpRequested = pyqtSignal(int, bool)
    summarizeRequested = pyqtSignal()
    flashcardsRequested = pyqtSignal()
    sendSelectionToSplitRequested = pyqtSignal(str)
    acquireLockRequested = pyqtSignal(object)
    publishGroupRequested = pyqtSignal(object)
    copyToLocalRequested = pyqtSignal(object)
    publishLocalToGroupRequested = pyqtSignal(object)
    openGroupNoteFromBoardRequested = pyqtSignal(object, dict)
    refreshBoardRequested = pyqtSignal(object)
    splitTabRequested = pyqtSignal(object, int)
    tabDroppedFromPane = pyqtSignal(str, int, object, object)  # source_pane_id, tab_idx, target_pane, drop_pos
    nodeDroppedFromTree = pyqtSignal(object, object)  # node_data, target_pane
    wikilinkOpenRequested = pyqtSignal(str)  # titolo nota da [[wikilink]]
    noteIdOpenRequested = pyqtSignal(int)    # apertura nota da pannello Backlinks
    restoreTrashedNoteRequested = pyqtSignal(int)
    permanentDeleteTrashedNoteRequested = pyqtSignal(int)

    def __init__(
        self,
        pane_id: str,
        theme_key: str = "obsidian",
        is_dark: bool = True,
        reading_mode: bool = False,
        palette: Optional[Dict[str, str]] = None,
        autosave_thread_cls: Any = None,
        audio_sync_loader: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.pane_id = pane_id
        self._theme_key = theme_key
        self._is_dark = is_dark
        self._reading_mode = reading_mode
        self._palette = dict(palette or {})
        self._autosave_thread_cls = autosave_thread_cls
        self._audio_sync_loader = audio_sync_loader

        self.tabs: List[TabContext] = []
        self.current_tab_index: int = -1
        self._loading_note: bool = False
        self._dirty_for_snapshot: bool = False
        self._last_snapshot_ts: float = 0.0
        self._is_active_pane: bool = (pane_id == "left")
        self._autosave_thread = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Indicatore superiore sottile (colore accento quando il pannello ha il focus)
        self.active_indicator = QFrame()
        self.active_indicator.setFixedHeight(2)
        root.addWidget(self.active_indicator)

        self.setAcceptDrops(True)

        # Barra delle schede stile Windows 11 Explorer
        self.tab_bar_widget = Win11TabBar(theme_key=theme_key, is_dark=is_dark, pane_id=pane_id, parent=self)
        self.tab_bar_widget.tabSelected.connect(self.switch_to_tab)
        self.tab_bar_widget.tabCloseRequested.connect(self.close_tab)
        self.tab_bar_widget.tabMoved.connect(self._on_tab_moved)
        self.tab_bar_widget.newTabRequested.connect(self.open_new_empty_tab)
        self.tab_bar_widget.splitTabRequested.connect(lambda idx: self.splitTabRequested.emit(self, idx))
        self.tab_bar_widget.duplicateTabRequested.connect(self.duplicate_tab)
        self.tab_bar_widget.closeOtherTabsRequested.connect(self.close_other_tabs)
        self.tab_bar_widget.closeAllTabsRequested.connect(self.close_all_tabs)
        self.tab_bar_widget.nodeDroppedFromTree.connect(lambda data: self.nodeDroppedFromTree.emit(data, self))
        root.addWidget(self.tab_bar_widget)

        # Breadcrumb & Azioni Contestuali
        self.breadcrumb = BreadcrumbBar(theme_key=theme_key, is_dark=is_dark, parent=self)
        self.breadcrumb.acquireLockRequested.connect(lambda: self.acquireLockRequested.emit(self))
        self.breadcrumb.publishGroupRequested.connect(lambda: self.publishGroupRequested.emit(self))
        self.breadcrumb.copyToLocalRequested.connect(lambda: self.copyToLocalRequested.emit(self))
        self.breadcrumb.publishLocalToGroupRequested.connect(lambda: self.publishLocalToGroupRequested.emit(self))
        root.addWidget(self.breadcrumb)

        # Stack: Editor (0) vs Bacheca Gruppo (1)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Pagina 0: Editor
        self.editor_page = QWidget()
        self.editor_page.setObjectName("NKEditorSurface")
        self.editor_page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ed_lay = QVBoxLayout(self.editor_page)
        ed_lay.setContentsMargins(12, 10, 12, 6)
        ed_lay.setSpacing(6)

        self.trash_banner = QFrame()
        self.trash_banner.setObjectName("NKTrashBanner")
        self.trash_banner.setStyleSheet(
            "QFrame#NKTrashBanner { background-color: rgba(239, 68, 68, 0.12); "
            "border: 1px solid rgba(239, 68, 68, 0.35); border-radius: 8px; }"
        )
        tb_lay = QHBoxLayout(self.trash_banner)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(8)
        self._trash_banner_lbl = QLabel("Questa nota è nel Cestino (sola lettura)")
        self._trash_banner_lbl.setWordWrap(True)
        self._trash_banner_lbl.setStyleSheet(
            "background: transparent; border: none; color: #FCA5A5; font-size: 12px; font-weight: 600;"
            if is_dark else
            "background: transparent; border: none; color: #B91C1C; font-size: 12px; font-weight: 600;"
        )
        tb_lay.addWidget(self._trash_banner_lbl, 1)
        self._trash_restore_btn = QPushButton("Ripristina")
        self._trash_restore_btn.setObjectName("NKToolbarPill")
        self._trash_restore_btn.setFixedHeight(26)
        self._trash_restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._trash_restore_btn.clicked.connect(self._on_trash_restore_clicked)
        tb_lay.addWidget(self._trash_restore_btn)
        self._trash_perm_del_btn = QPushButton("Elimina definitivamente")
        self._trash_perm_del_btn.setObjectName("NKToolbarPill")
        self._trash_perm_del_btn.setFixedHeight(26)
        self._trash_perm_del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._trash_perm_del_btn.clicked.connect(self._on_trash_perm_delete_clicked)
        tb_lay.addWidget(self._trash_perm_del_btn)
        self.trash_banner.hide()
        ed_lay.addWidget(self.trash_banner)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)

        self.editor_title = DictationGhostLineEdit(dark_mode=is_dark)
        self.editor_title.setObjectName("NKEditorTitle")
        self.editor_title.setPlaceholderText("Titolo della nota...")
        self.editor_title.setMinimumHeight(36)
        title_row.addWidget(self.editor_title, 1)

        self.btn_auto_title = QPushButton("")
        self.btn_auto_title.setObjectName("NKToolbarIconBtn")
        self.btn_auto_title.setFixedSize(28, 28)
        self.btn_auto_title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_title.setIcon(get_icon("magic", theme_key=theme_key, role="accent", size=14))
        self.btn_auto_title.setToolTip("Genera titolo automatico in base al contenuto della nota")
        self.btn_auto_title.clicked.connect(lambda: self.trigger_auto_title(manual=True))
        title_row.addWidget(self.btn_auto_title)
        ed_lay.addLayout(title_row)

        self.editor_author_lbl = QLabel("")
        self.editor_author_lbl.setObjectName("NKEditorAuthorMeta")
        self.editor_author_lbl.setStyleSheet(
            "background: transparent; border: none; color: #94A3B8; font-size: 11.5px; padding: 0 2px;"
        )
        self.editor_author_lbl.setWordWrap(True)
        self.editor_author_lbl.setVisible(False)
        ed_lay.addWidget(self.editor_author_lbl)

        meta_grid = QHBoxLayout()
        meta_grid.setSpacing(6)

        self.editor_subject = QLineEdit()
        self.editor_subject.setObjectName("NKEditorPill")
        self.editor_subject.setPlaceholderText("Cartella")
        self.editor_subject.setFixedHeight(26)
        self.editor_subject.setMaximumWidth(150)
        meta_grid.addWidget(self.editor_subject)

        self._meta_sep1 = QLabel("›")
        self._meta_sep1.setStyleSheet("color: #94A3B8; font-size: 13px; background: transparent;")
        meta_grid.addWidget(self._meta_sep1)

        self.editor_topic = QLineEdit()
        self.editor_topic.setObjectName("NKEditorPill")
        self.editor_topic.setPlaceholderText("Argomento")
        self.editor_topic.setFixedHeight(26)
        self.editor_topic.setMaximumWidth(150)
        meta_grid.addWidget(self.editor_topic)

        sep2 = QLabel("·")
        sep2.setStyleSheet("color: #94A3B8; font-size: 14px; font-weight: bold; background: transparent;")
        meta_grid.addWidget(sep2)

        self.editor_type = QComboBox()
        self.editor_type.setObjectName("NKEditorPillCombo")
        self.editor_type.addItems(["appunto", "sbobina", "slide"])
        self.editor_type.setFixedHeight(26)
        meta_grid.addWidget(self.editor_type)

        sep3 = QLabel("·")
        sep3.setStyleSheet("color: #94A3B8; font-size: 14px; font-weight: bold; background: transparent;")
        meta_grid.addWidget(sep3)

        self.editor_profile = QComboBox()
        self.editor_profile.setObjectName("NKEditorPillCombo")
        self.editor_profile.addItems(["Alessandro", "Giada"])
        self.editor_profile.setFixedHeight(26)
        meta_grid.addWidget(self.editor_profile)

        meta_grid.addStretch(1)
        self.autosave_badge = QLabel("✓ Salvato automaticamente")
        self.autosave_badge.setObjectName("NKAutoSaveBadge")
        self.autosave_badge.setStyleSheet(
            "background: transparent; border: none; color: #787774; font-size: 11.5px; font-weight: 400;"
        )
        meta_grid.addWidget(self.autosave_badge)
        ed_lay.addLayout(meta_grid)

        divider = QFrame()
        divider.setObjectName("NKEditorDivider")
        divider.setFixedHeight(1)
        ed_lay.addWidget(divider)

        self.editor_body = ScientificNoteEditor(dark_mode=is_dark, reading_mode=reading_mode)
        self.editor_body.setObjectName("NKEditorBody")
        self.editor_body.setPlaceholderText(
            "Scrivi o detta qui in Markdown + LaTeX ($...$), incolla immagini (Ctrl+V) o digita '/' per i comandi..."
        )
        self.editor_body.setMinimumHeight(220)
        accent = self._palette.get("accent") or self._palette.get("nav_checked_border") or "#6366F1"
        self.editor_body.set_sync_accent_color(accent)
        ed_lay.addWidget(self.editor_body, 1)

        self.audio_bar = AudioSyncBar(self)
        self.audio_bar.hide()
        ed_lay.addWidget(self.audio_bar)

        # Pannello Backlinks compatto (Note collegate / citazioni [[Wikilink]])
        self.backlinks_bar = QFrame()
        self.backlinks_bar.setObjectName("NKBacklinksBar")
        self.backlinks_bar.setStyleSheet(
            "QFrame#NKBacklinksBar { background-color: rgba(148, 163, 184, 0.06); "
            "border-top: 1px solid rgba(148, 163, 184, 0.16); border-radius: 6px; }"
        )
        bl_lay = QHBoxLayout(self.backlinks_bar)
        bl_lay.setContentsMargins(10, 5, 10, 5)
        bl_lay.setSpacing(6)
        self._backlinks_lbl = QLabel("🔗 Note collegate:")
        self._backlinks_lbl.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600; background: transparent;")
        bl_lay.addWidget(self._backlinks_lbl)
        self._backlinks_pills_lay = QHBoxLayout()
        self._backlinks_pills_lay.setContentsMargins(0, 0, 0, 0)
        self._backlinks_pills_lay.setSpacing(5)
        bl_lay.addLayout(self._backlinks_pills_lay, 1)
        self.backlinks_bar.hide()
        ed_lay.addWidget(self.backlinks_bar)

        self.audio_bar.positionChangedMs.connect(self.editor_body.highlight_audio_position)
        self.audio_bar.playbackStateChanged.connect(
            lambda playing: None if playing else self.editor_body.clear_audio_highlight()
        )
        self.editor_body.seekRequested.connect(lambda ms: self.audio_bar.seek_ms(ms, auto_play=True))
        self.editor_body.togglePlayRequested.connect(self.audio_bar.toggle_play_pause)

        self.stack.addWidget(self.editor_page)

        # Pagina 1: Bacheca Gruppo
        self.board_widget = GroupBoardWidget(theme_key=theme_key, parent=self)
        self.board_widget.openGroupNoteRequested.connect(
            lambda item: self.openGroupNoteFromBoardRequested.emit(self, item)
        )
        self.board_widget.refreshRequested.connect(lambda: self.refreshBoardRequested.emit(self))
        self.stack.addWidget(self.board_widget)

        # Autosave timer + Auto-Title timer (generazione automatica titolo contestuale)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(800)
        self._autosave_timer.timeout.connect(self._perform_autosave)

        self._auto_title_timer = QTimer(self)
        self._auto_title_timer.setSingleShot(True)
        self._auto_title_timer.setInterval(2400)
        self._auto_title_timer.timeout.connect(lambda: self.trigger_auto_title(manual=False))
        self._auto_title_workers: List[_AutoTitleWorker] = []

        self.editor_title.textChanged.connect(self._on_editor_modified)
        self.editor_subject.textChanged.connect(self._on_editor_modified)
        self.editor_topic.textChanged.connect(self._on_editor_modified)
        self.editor_type.currentIndexChanged.connect(self._on_editor_modified)
        self.editor_profile.currentIndexChanged.connect(self._on_editor_modified)
        self.editor_body.markdownModified.connect(self._on_editor_modified)
        self.editor_body.summarizeRequested.connect(self.summarizeRequested.emit)
        self.editor_body.flashcardsRequested.connect(self.flashcardsRequested.emit)
        self.editor_body.studioViewChanged.connect(self.studioViewChanged.emit)
        self.editor_body.rsvpRequested.connect(self.rsvpRequested.emit)
        self.editor_body.sendSelectionToSplitRequested.connect(self.sendSelectionToSplitRequested.emit)
        self.editor_body.wikilinkClicked.connect(self.wikilinkOpenRequested.emit)

        for w in (self, self.editor_title, self.editor_body, self.editor_subject, self.editor_topic, self.tab_bar_widget):
            w.installEventFilter(self)

        self.set_theme(theme_key, is_dark, reading_mode, self._palette)
        self.set_active_pane(self._is_active_pane)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.activated.emit(self)
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            try:
                raw = bytes(event.mimeData().data("application/x-flownotes-node")).decode("utf-8")
                data = json.loads(raw)
                if data:
                    self.nodeDroppedFromTree.emit(tuple(data))
                    event.acceptProposedAction()
                    return
            except Exception:
                pass
        super().dropEvent(event)

    def set_active_pane(self, active: bool):
        self._is_active_pane = bool(active)
        tab = self.active_tab()
        is_group = bool(tab and tab.workspace_type in ("group", "board"))
        if is_group:
            accent = resolve_icon_color(self._theme_key, "success")
        else:
            accent = self._palette.get("accent") or self._palette.get("nav_checked_border") or "#6366F1"
        if active:
            self.active_indicator.setStyleSheet(f"background-color: {accent}; border: none; border-radius: 1px;")
        else:
            self.active_indicator.setStyleSheet("background-color: transparent; border: none;")

    def _sync_topic_chip_visibility(self):
        subj = self.editor_subject.text().strip().lower()
        top = self.editor_topic.text().strip().lower()
        show_topic = bool(top and top != subj)
        self._meta_sep1.setVisible(show_topic)
        self.editor_topic.setVisible(show_topic)

    def set_theme(self, theme_key: str, is_dark: bool, reading_mode: bool, palette: Dict[str, str]):
        self._theme_key = theme_key
        self._is_dark = is_dark
        self._reading_mode = reading_mode
        self._palette = dict(palette or {})
        surface_bg = self._palette.get("editor_bg") or self._palette.get("card_bg") or ("#161B24" if is_dark else "#FFFFFF")
        if hasattr(self, "editor_page"):
            self.editor_page.setStyleSheet(f"QWidget#NKEditorSurface {{ background-color: {surface_bg}; }}")
        self.tab_bar_widget.set_theme(theme_key, self._palette, is_dark=is_dark)
        self.breadcrumb.set_theme(theme_key, self._palette, is_dark=is_dark)
        self.board_widget.set_theme(theme_key, self._palette)
        if hasattr(self.editor_body, "set_theme_modes"):
            self.editor_body.set_theme_modes(is_dark, reading_mode)
        accent = self._palette.get("accent") or self._palette.get("nav_checked_border") or "#6366F1"
        if hasattr(self.editor_body, "set_sync_accent_color"):
            self.editor_body.set_sync_accent_color(accent)
        if hasattr(self.editor_title, "set_dark_mode"):
            self.editor_title.set_dark_mode(is_dark)
        if hasattr(self, "btn_auto_title"):
            self.btn_auto_title.setIcon(get_icon("magic", theme_key=theme_key, role="accent", size=14))
        if hasattr(self, "_trash_banner_lbl"):
            col_tr = "#FCA5A5" if is_dark else "#B91C1C"
            self._trash_banner_lbl.setStyleSheet(
                f"background: transparent; border: none; color: {col_tr}; font-size: 12px; font-weight: 600;"
            )
        self.set_active_pane(self._is_active_pane)

    def active_tab(self) -> Optional[TabContext]:
        if 0 <= self.current_tab_index < len(self.tabs):
            return self.tabs[self.current_tab_index]
        return None

    def _capture_current_tab_state(self):
        tab = self.active_tab()
        if tab is None or tab.workspace_type == "board":
            return
        tab.title = self.editor_title.text().strip() or tab.title or "Nuova nota"
        tab.subject = self.editor_subject.text().strip() or "Generale"
        tab.topic = self.editor_topic.text().strip() or "Generale"
        tab.note_type = self.editor_type.currentText().strip() or "appunto"
        tab.profile = self.editor_profile.currentText().strip() or "Alessandro"
        tab.content_markdown = self.editor_body.get_markdown_text()
        tab.is_studio_view = bool(self.editor_body.is_studio_view)

    def _on_trash_restore_clicked(self):
        tab = self.active_tab()
        if tab and tab.note_id is not None:
            self.restoreTrashedNoteRequested.emit(int(tab.note_id))

    def _on_trash_perm_delete_clicked(self):
        tab = self.active_tab()
        if tab and tab.note_id is not None:
            self.permanentDeleteTrashedNoteRequested.emit(int(tab.note_id))

    def close_tabs_for_note_id(self, note_id: int):
        """Chiude tutte le schede locali associate a note_id senza autosave o conferme."""
        target_id = int(note_id)
        matching = [
            i for i, t in enumerate(self.tabs)
            if t.workspace_type == "local" and t.note_id is not None and int(t.note_id) == target_id
        ]
        if not matching:
            return
        self._autosave_timer.stop()
        for idx in reversed(matching):
            self.tabs.pop(idx)
            if self.current_tab_index == idx:
                self.current_tab_index = -1
            elif self.current_tab_index > idx:
                self.current_tab_index -= 1
        if not self.tabs:
            self.current_tab_index = -1
            self.tab_bar_widget.sync_tabs([], -1)
            self.breadcrumb.update_from_tab(None)
            self.lastTabClosed.emit(self)
            self.activeTabChanged.emit(None)
            return
        if self.current_tab_index < 0 or self.current_tab_index >= len(self.tabs):
            self.current_tab_index = max(0, min(matching[0], len(self.tabs) - 1))
        self.tab_bar_widget.sync_tabs(self.tabs, self.current_tab_index)
        self._render_tab(self.tabs[self.current_tab_index])
        self.activeTabChanged.emit(self.tabs[self.current_tab_index])

    def open_tab(self, ctx: TabContext, in_new_tab: bool = False):
        # 1. Se la stessa nota/bacheca è già aperta in una scheda, passa a quella
        key = ctx.identity_key()
        for i, existing in enumerate(self.tabs):
            if existing.identity_key() == key:
                # Aggiorna eventuali metadati/contenuti se è una nota di gruppo ricaricata
                if ctx.workspace_type == "group" and not existing.is_dirty:
                    existing.content_markdown = ctx.content_markdown
                    existing.group_note_item = ctx.group_note_item
                    existing.lock_info = ctx.lock_info
                    existing.is_locked_by_me = ctx.is_locked_by_me
                elif ctx.workspace_type == "local":
                    existing.is_trashed = ctx.is_trashed
                    existing.is_favorite = ctx.is_favorite
                self.switch_to_tab(i)
                self.tab_bar_widget.update_tab_title(i, existing)
                return

        cur = self.active_tab()
        if not in_new_tab and cur is not None and (not cur.is_dirty or cur.is_new_empty):
            # Sostituisci la scheda corrente se non modificata
            self.tabs[self.current_tab_index] = ctx
            self.tab_bar_widget.sync_tabs(self.tabs, self.current_tab_index)
            self._render_tab(ctx)
        else:
            self.tabs.append(ctx)
            new_idx = len(self.tabs) - 1
            self.current_tab_index = new_idx
            self.tab_bar_widget.sync_tabs(self.tabs, new_idx)
            self._render_tab(ctx)

        self.activeTabChanged.emit(self.active_tab())

    def open_new_empty_tab(self, subject: str = "Generale", topic: str = "Generale", profile: str = "Alessandro"):
        ctx = TabContext(
            workspace_type="local",
            note_id=None,
            subject=subject or "Generale",
            topic=topic or "Generale",
            profile=profile or "Alessandro",
            title="Nuova nota",
            content_markdown="",
            is_new_empty=True,
        )
        self.open_tab(ctx, in_new_tab=True)
        self.editor_title.setText("")
        self.editor_title.setFocus()

    def switch_to_tab(self, idx: int):
        if idx < 0 or idx >= len(self.tabs):
            return
        if self.current_tab_index != idx:
            self._capture_current_tab_state()
            self.current_tab_index = idx
        tab = self.tabs[idx]
        self._render_tab(tab)
        self.tab_bar_widget.tab_bar.blockSignals(True)
        self.tab_bar_widget.tab_bar.setCurrentIndex(idx)
        self.tab_bar_widget.tab_bar.blockSignals(False)
        self.activeTabChanged.emit(tab)

    def cycle_tab(self, step: int = 1):
        if len(self.tabs) <= 1:
            return
        nxt = (self.current_tab_index + step) % len(self.tabs)
        self.switch_to_tab(nxt)

    def _on_tab_moved(self, from_idx: int, to_idx: int):
        if not (0 <= from_idx < len(self.tabs) and 0 <= to_idx < len(self.tabs)) or from_idx == to_idx:
            return
        tab = self.tabs.pop(from_idx)
        self.tabs.insert(to_idx, tab)
        self.current_tab_index = self.tab_bar_widget.tab_bar.currentIndex()

    def take_tab(self, idx: int) -> Optional[TabContext]:
        """Rimuove e restituisce una scheda senza chiedere conferma (usata per spostamento drag & drop)."""
        if idx < 0 or idx >= len(self.tabs):
            return None
        if idx == self.current_tab_index:
            self._capture_current_tab_state()
        tab = self.tabs.pop(idx)
        if not self.tabs:
            self.current_tab_index = -1
            self.tab_bar_widget.sync_tabs(self.tabs, -1)
            self.lastTabClosed.emit(self)
        else:
            if self.current_tab_index >= len(self.tabs):
                self.current_tab_index = len(self.tabs) - 1
            elif self.current_tab_index > idx:
                self.current_tab_index -= 1
            self.tab_bar_widget.sync_tabs(self.tabs, self.current_tab_index)
            self._render_tab(self.tabs[self.current_tab_index])
            self.activeTabChanged.emit(self.tabs[self.current_tab_index])
        return tab

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-tab") or event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-tab") or event.mimeData().hasFormat("application/x-flownotes-node"):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat("application/x-flownotes-tab"):
            try:
                raw = bytes(event.mimeData().data("application/x-flownotes-tab")).decode("utf-8")
                info = json.loads(raw)
                src_pane_id = str(info.get("source_pane_id") or "")
                tab_idx = int(info.get("tab_idx", -1))
                if src_pane_id and tab_idx >= 0:
                    self.tabDroppedFromPane.emit(src_pane_id, tab_idx, self, event.position().toPoint())
                    event.acceptProposedAction()
                    return
            except Exception:
                pass
        if event.mimeData().hasFormat("application/x-flownotes-node"):
            try:
                raw = bytes(event.mimeData().data("application/x-flownotes-node")).decode("utf-8")
                data = json.loads(raw)
                if data:
                    self.nodeDroppedFromTree.emit(tuple(data), self)
                    event.acceptProposedAction()
                    return
            except Exception:
                pass
        super().dropEvent(event)

    def close_tab(self, idx: int):
        if idx < 0 or idx >= len(self.tabs):
            return
        if idx == self.current_tab_index:
            self._capture_current_tab_state()
        tab = self.tabs[idx]

        if tab.is_dirty and tab.workspace_type == "group":
            ans = QMessageBox.question(
                self,
                "Modifiche non pubblicate",
                f"La nota di gruppo '{tab.title}' ha modifiche non pubblicate. Chiudere comunque?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        elif tab.is_dirty and tab.workspace_type == "local":
            self._perform_autosave()

        self.tabs.pop(idx)
        if not self.tabs:
            self.current_tab_index = -1
            self.tab_bar_widget.sync_tabs([], -1)
            self.breadcrumb.update_from_tab(None)
            self.lastTabClosed.emit(self)
            self.activeTabChanged.emit(None)
            return

        new_idx = min(idx, len(self.tabs) - 1)
        self.current_tab_index = new_idx
        self.tab_bar_widget.sync_tabs(self.tabs, new_idx)
        self._render_tab(self.tabs[new_idx])
        self.activeTabChanged.emit(self.tabs[new_idx])

    def close_other_tabs(self, keep_idx: int):
        if keep_idx < 0 or keep_idx >= len(self.tabs):
            return
        keep_tab = self.tabs[keep_idx]
        self.tabs = [keep_tab]
        self.current_tab_index = 0
        self.tab_bar_widget.sync_tabs(self.tabs, 0)
        self._render_tab(keep_tab)
        self.activeTabChanged.emit(keep_tab)

    def close_all_tabs(self):
        for i in range(len(self.tabs) - 1, -1, -1):
            self.close_tab(i)

    def duplicate_tab(self, idx: int):
        if idx < 0 or idx >= len(self.tabs):
            return
        if idx == self.current_tab_index:
            self._capture_current_tab_state()
        src = self.tabs[idx]
        dup = TabContext(
            workspace_type=src.workspace_type,
            group_id=src.group_id,
            group_rec=src.group_rec,
            note_id=src.note_id,
            subject=src.subject,
            topic=src.topic,
            note_type=src.note_type,
            profile=src.profile,
            title=f"{src.title} (Copia)" if src.workspace_type == "local" else src.title,
            rel_path=src.rel_path,
            group_note_item=src.group_note_item,
            content_markdown=src.content_markdown,
            is_dirty=False,
            is_locked_by_me=src.is_locked_by_me,
            lock_info=src.lock_info,
            is_studio_view=src.is_studio_view,
        )
        self.tabs.append(dup)
        new_idx = len(self.tabs) - 1
        self.current_tab_index = new_idx
        self.tab_bar_widget.sync_tabs(self.tabs, new_idx)
        self._render_tab(dup)
        self.activeTabChanged.emit(dup)

    def _render_tab(self, tab: TabContext):
        self.breadcrumb.update_from_tab(tab)
        self.set_active_pane(self._is_active_pane)
        if tab.workspace_type == "board":
            self.stack.setCurrentIndex(1)
            return

        self.stack.setCurrentIndex(0)
        self._loading_note = True
        try:
            self._autosave_timer.stop()
            if tab.is_new_empty and tab.title == "Nuova nota":
                self.editor_title.setText("")
            else:
                self.editor_title.setText(tab.title or "")
            self.editor_subject.setText(tab.subject or "Generale")
            self.editor_topic.setText(tab.topic or "Generale")
            self._sync_topic_chip_visibility()
            idx_t = self.editor_type.findText((tab.note_type or "appunto").lower(), Qt.MatchFlag.MatchFixedString)
            if idx_t >= 0:
                self.editor_type.setCurrentIndex(idx_t)
            self.editor_profile.setCurrentText(tab.profile or "Alessandro")

            if self.editor_body.is_studio_view != tab.is_studio_view:
                self.editor_body.set_studio_view(tab.is_studio_view)

            self.editor_body.set_markdown_text(tab.content_markdown or "")

            # Provenienza autore sotto il titolo
            src_meta = tab.group_note_item if isinstance(tab.group_note_item, dict) else {}
            cb_name = (src_meta.get("created_by_name") or tab.created_by_name or "").strip()
            le_name = (src_meta.get("last_edited_by_name") or tab.last_edited_by_name or "").strip()
            if cb_name or le_name:
                parts = []
                if cb_name:
                    parts.append(f"Creata da {cb_name}")
                if le_name:
                    parts.append(f"Ultima modifica di {le_name}")
                self.editor_author_lbl.setText("  •  ".join(parts))
                self.editor_author_lbl.setVisible(True)
            else:
                self.editor_author_lbl.setVisible(False)

            # Read-only enforcement per note di gruppo senza lock o note nel Cestino
            self.trash_banner.setVisible(bool(tab.is_trashed))
            is_ro = (tab.workspace_type == "group" and not tab.is_locked_by_me) or bool(tab.is_trashed)
            self.editor_body.setReadOnly(is_ro)
            self.editor_title.setReadOnly(is_ro)
            self.editor_subject.setReadOnly(tab.workspace_type == "group" or bool(tab.is_trashed))
            self.editor_topic.setReadOnly(tab.workspace_type == "group" or bool(tab.is_trashed))
            self.editor_type.setEnabled(not tab.is_trashed)
            self.editor_profile.setEnabled(not tab.is_trashed)
            self.btn_auto_title.setEnabled(not tab.is_trashed)

            if tab.workspace_type == "local" and tab.note_id and self._audio_sync_loader:
                sync_data = self._audio_sync_loader(tab.note_id)
                if sync_data and sync_data.get("audio_path"):
                    self.editor_body.set_audio_sync_data(sync_data.get("segments") or [])
                    self.audio_bar.load_audio(sync_data["audio_path"])
                else:
                    self.editor_body.set_audio_sync_data(None)
                    self.audio_bar.unload()
            else:
                self.editor_body.set_audio_sync_data(None)
                self.audio_bar.unload()

            if tab.workspace_type == "group":
                badge_txt = "Modifica con Lock attivo" if tab.is_locked_by_me else "Sola lettura (Acquisisci Lock per modificare)"
                self.set_autosave_badge(True, badge_txt)
                self.refresh_backlinks(None, "")
            elif tab.is_trashed:
                self.set_autosave_badge(True, "Nel Cestino (Sola lettura)")
                self.refresh_backlinks(tab.note_id, tab.title)
            else:
                self.set_autosave_badge(not tab.is_dirty)
                self.refresh_backlinks(tab.note_id, tab.title)
        finally:
            self._loading_note = False

    def refresh_backlinks(self, note_id: Optional[int], title: str = ""):
        """Aggiorna la barra compatta dei Backlinks / Note collegate sotto l'editor."""
        if not hasattr(self, "backlinks_bar"):
            return
        while self._backlinks_pills_lay.count():
            it = self._backlinks_pills_lay.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        if not note_id:
            self.backlinks_bar.hide()
            return
        try:
            links = db.get_note_backlinks(int(note_id), title or "")
        except Exception:
            links = []
        if not links:
            self.backlinks_bar.hide()
            return
        self._backlinks_lbl.setText(f"🔗 Collegate ({len(links)}):")
        for lk in links[:6]:
            lid = int(lk["id"])
            ltitle = (lk.get("title") or "Nota").strip()
            short_t = (ltitle[:24] + "…") if len(ltitle) > 25 else ltitle
            btn = QPushButton(short_t)
            btn.setObjectName("FilterPill")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.setToolTip(f"{ltitle} — {lk.get('reason') or 'Nota collegata'}")
            btn.clicked.connect(lambda _c=False, target_id=lid: self.noteIdOpenRequested.emit(target_id))
            self._backlinks_pills_lay.addWidget(btn)
        self._backlinks_pills_lay.addStretch(1)
        self.backlinks_bar.show()

    def set_autosave_badge(self, saved: bool, custom_text: Optional[str] = None):
        if saved:
            self.autosave_badge.setText(custom_text or "✓ Salvato automaticamente")
        else:
            self.autosave_badge.setText(custom_text or "Modifica in corso...")

    def _on_editor_modified(self, *_args):
        if self._loading_note or self.editor_body.is_studio_view:
            return
        tab = self.active_tab()
        if tab is None or tab.workspace_type == "board" or tab.is_trashed:
            return
        tab.is_dirty = True
        tab.is_new_empty = False
        tab.title = self.editor_title.text().strip() or tab.title or "Appunto senza titolo"
        tab.content_markdown = self.editor_body.get_markdown_text()
        self.tab_bar_widget.update_tab_title(self.current_tab_index, tab)
        self.breadcrumb.update_from_tab(tab)

        if tab.workspace_type == "local":
            self._dirty_for_snapshot = True
            self.set_autosave_badge(False)
            self._autosave_timer.start(800)

            # Pianifica generazione automatica del titolo se assente/default e contenuto sufficiente
            auto_enabled = (db.get_setting("auto_generate_note_title", "1") or "1").strip() == "1"
            cur_title = self.editor_title.text().strip()
            is_default_title = (not cur_title) or (cur_title.lower() in ("nuova nota", "appunto senza titolo", "senza titolo"))
            if auto_enabled and is_default_title and not tab.auto_title_done and len(tab.content_markdown.strip()) >= 45:
                self._auto_title_timer.start(2400)
        else:
            self.set_autosave_badge(False, "Modifiche locali (premi Pubblica per salvare nel Gruppo)")

    def trigger_auto_title(self, manual: bool = False):
        """Avvia la generazione asincrona del titolo in base al contenuto della nota."""
        tab = self.active_tab()
        if tab is None or tab.workspace_type != "local":
            return
        if self.editor_title.isReadOnly():
            return
        content = self.editor_body.get_markdown_text().strip()
        if len(content) < (12 if manual else 45):
            if manual:
                self.set_autosave_badge(True, "Scrivi prima qualche riga per generare il titolo")
            return
        cur_title = self.editor_title.text().strip()
        is_default_title = (not cur_title) or (cur_title.lower() in ("nuova nota", "appunto senza titolo", "senza titolo"))
        if not manual and not is_default_title:
            return

        folder_name = self.editor_subject.text().strip() or tab.subject or ""
        if manual:
            self.set_autosave_badge(False, "✦ Generazione titolo in corso...")

        worker = _AutoTitleWorker(tab.tab_id, content, folder_name=folder_name, parent=self)
        self._auto_title_workers.append(worker)
        worker.titleReady.connect(
            lambda tid, new_title, is_man=manual: self._on_auto_title_ready(tid, new_title, is_man)
        )
        worker.finished.connect(lambda w=worker: self._cleanup_title_worker(w))
        worker.start()

    def _cleanup_title_worker(self, worker: _AutoTitleWorker):
        if worker in self._auto_title_workers:
            self._auto_title_workers.remove(worker)
        worker.deleteLater()

    def _on_auto_title_ready(self, tab_id: str, generated_title: str, manual: bool):
        clean_t = (generated_title or "").strip()
        if not clean_t:
            return
        target_tab = next((t for t in self.tabs if t.tab_id == tab_id), None)
        if target_tab is None or target_tab.workspace_type != "local":
            return
        is_active = (self.active_tab() is target_tab)
        cur_title = self.editor_title.text().strip() if is_active else (target_tab.title or "").strip()
        is_default_title = (not cur_title) or (cur_title.lower() in ("nuova nota", "appunto senza titolo", "senza titolo"))
        if not manual and not is_default_title:
            return

        target_tab.title = clean_t
        target_tab.auto_title_done = True
        if is_active:
            had_body_focus = self.editor_body.hasFocus()
            self._loading_note = True
            try:
                self.editor_title.setText(clean_t)
            finally:
                self._loading_note = False
            if had_body_focus:
                self.editor_body.setFocus()
            self.tab_bar_widget.update_tab_title(self.current_tab_index, target_tab)
            self.breadcrumb.update_from_tab(target_tab)
            self.set_autosave_badge(False, "✦ Titolo generato")
            self._autosave_timer.start(350)
        elif target_tab.note_id:
            try:
                db.update_study_note(
                    target_tab.note_id,
                    clean_t,
                    target_tab.content_markdown,
                    target_tab.profile,
                    target_tab.subject,
                    target_tab.topic,
                    target_tab.note_type,
                )
            except Exception:
                pass
            idx = self.tabs.index(target_tab)
            self.tab_bar_widget.update_tab_title(idx, target_tab)

    def _perform_autosave(self):
        if self._loading_note:
            return
        tab = self.active_tab()
        if tab is None or tab.workspace_type != "local" or tab.is_trashed:
            return
        raw_title = self.editor_title.text().strip()
        content = self.editor_body.get_markdown_text().strip()
        if tab.note_id is None and not raw_title and not content:
            tab.is_dirty = False
            self.set_autosave_badge(True)
            return

        title = raw_title or "Appunto senza titolo"
        prof = self.editor_profile.currentText().strip() or "Alessandro"
        subj = self.editor_subject.text().strip() or "Generale"
        top = self.editor_topic.text().strip() or "Generale"
        ntype = self.editor_type.currentText().strip() or "appunto"

        if self._autosave_thread_cls is None:
            return
        # Non distruggere mai un thread di salvataggio ancora in esecuzione: ripianifica brevemente
        if self._autosave_thread is not None and self._autosave_thread.isRunning():
            self._autosave_timer.start(250)
            return
        was_new = (tab.note_id is None)
        worker = self._autosave_thread_cls(tab.note_id, title, content, prof, subj, top, ntype)
        self._autosave_thread = worker
        worker.saved.connect(
            lambda sid, t, c, s, tp, nt: self._on_autosave_finished(tab, was_new, sid, t, c)
        )
        worker.error.connect(self._on_autosave_error)
        worker.start()

    def _on_autosave_error(self, err_msg: str):
        import logging
        logging.getLogger("FlowLocal.AutoSave").error("Errore salvataggio nota: %s", err_msg)
        self.set_autosave_badge(False, "Errore salvataggio")

    def _on_autosave_finished(self, tab: TabContext, was_new: bool, saved_id: int, title: str, content: str):
        import time
        tab.note_id = saved_id
        tab.title = title
        tab.is_dirty = False
        tab.is_new_empty = False
        if self.active_tab() is tab:
            self.set_autosave_badge(True)
            self.tab_bar_widget.update_tab_title(self.current_tab_index, tab)
            self.breadcrumb.update_from_tab(tab)
        now_ts = time.time()
        if self._dirty_for_snapshot and (now_ts - self._last_snapshot_ts >= 600.0):
            try:
                db.add_note_snapshot(saved_id, title, content, "Salvataggio automatico")
                self._dirty_for_snapshot = False
                self._last_snapshot_ts = now_ts
            except Exception:
                pass
        self.localNoteSaved.emit(saved_id, title, was_new)
