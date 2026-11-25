"""Barra multifunzione stile Microsoft Word Fluent (Office 365) per FlowNotes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPoint, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QIntValidator, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.icons import get_icon
from ui.scientific_editor import HIGHLIGHT_COLORS, TEXT_COLORS


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    c = QColor((hex_color or "").strip())
    if not c.isValid():
        c = QColor("#6366F1")
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.3f})"


def _make_swatch_icon(hex_color: Optional[str], size: int = 12, is_dark: bool = True) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if hex_color:
        p.setBrush(QColor(hex_color))
        p.setPen(QColor(255, 255, 255, 110) if is_dark else QColor(15, 23, 42, 95))
        p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    else:
        p.setPen(QColor("#94A3B8" if is_dark else "#64748B"))
        p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
        p.drawLine(2, size - 3, size - 3, 2)
    p.end()
    return QIcon(pm)


def _make_bar_letter_icon(letter: str, bar_color: str, text_color: str, size: int = 16) -> QIcon:
    """Disegna icona stile Word (es. 'A' con barretta rossa sotto o 'ab' con barretta gialla)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("Segoe UI", 9, QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor(text_color))
    p.drawText(0, 0, size, size - 3, Qt.AlignmentFlag.AlignCenter, letter)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(bar_color))
    p.drawRoundedRect(2, size - 4, size - 4, 3, 1.0, 1.0)
    p.end()
    return QIcon(pm)


class _RibbonTabButton(QPushButton):
    """Pulsante tab superiore con supporto doppio clic per comprimere/espandere il Ribbon."""

    doubleClicked = pyqtSignal()

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class _RibbonGroupWidget(QWidget):
    """Gruppo nominato della card Ribbon (area controlli sopra + etichetta centrata in basso)."""

    def __init__(self, caption: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 3, 6, 2)
        root.setSpacing(2)

        self.body_widget = QWidget(self)
        self.body_layout = QHBoxLayout(self.body_widget)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(3)
        self.body_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.body_widget, 1)

        self.caption_lbl = QLabel(caption, self)
        self.caption_lbl.setObjectName("RibbonGroupCaption")
        self.caption_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_lbl.setFixedHeight(13)
        root.addWidget(self.caption_lbl, 0)


class FlowNotesRibbonWidget(QWidget):
    """
    Ribbon Word Fluent per FlowNotes:
    - Tab Bar superiore: Edita | Inserisci | Gruppo | Studio | Whis (+ chevron collasso)
    - Card arrotondata flottante con gruppi nominati e controlli grandi/compatti.
    """

    # Tab Edita
    fontScaleDeltaRequested = pyqtSignal(int)
    fontSizeRequested = pyqtSignal(int)
    fontSizeDeltaRequested = pyqtSignal(int)
    clearFormatRequested = pyqtSignal()
    boldRequested = pyqtSignal()
    italicRequested = pyqtSignal()
    underlineRequested = pyqtSignal()
    strikeRequested = pyqtSignal()
    subscriptRequested = pyqtSignal()
    superscriptRequested = pyqtSignal()
    highlightRequested = pyqtSignal(str)
    textColorRequested = pyqtSignal(str)
    bulletListRequested = pyqtSignal()
    numberedListRequested = pyqtSignal()
    todoListRequested = pyqtSignal()
    indentDeltaRequested = pyqtSignal(int)
    quoteRequested = pyqtSignal()
    calloutRequested = pyqtSignal()
    linkNoteRequested = pyqtSignal()
    headingLevelRequested = pyqtSignal(int)

    # Tab Inserisci
    insertTableRequested = pyqtSignal()
    insertMatrixRequested = pyqtSignal()
    insertFormulaRequested = pyqtSignal()
    insertImageRequested = pyqtSignal()
    insertFileRequested = pyqtSignal()
    insertOcrRequested = pyqtSignal()
    insertCodeRequested = pyqtSignal()
    insertBibtexRequested = pyqtSignal()

    # Tab Gruppo
    acquireLockRequested = pyqtSignal()
    publishGroupRequested = pyqtSignal()
    publishLocalToSpecificGroupRequested = pyqtSignal(int)
    joinOrCreateGroupRequested = pyqtSignal()
    copyToLocalRequested = pyqtSignal()
    duplicateNoteRequested = pyqtSignal()
    noteStatusChangeRequested = pyqtSignal(str)
    historyRequested = pyqtSignal()
    exportFormatRequested = pyqtSignal(str)
    deleteNoteRequested = pyqtSignal()

    # Tab Studio
    studioViewToggled = pyqtSignal(bool)
    rsvpRequested = pyqtSignal()
    flashcardsRequested = pyqtSignal()
    timestampMarkerRequested = pyqtSignal()
    splitViewRequested = pyqtSignal()
    sendToSplitRequested = pyqtSignal()
    focusModeRequested = pyqtSignal()
    proseWidthToggled = pyqtSignal(bool)
    favoriteToggled = pyqtSignal()

    # Tab Whis
    askWhisRequested = pyqtSignal()
    summarizeRequested = pyqtSignal()
    aiCorrectSelectionRequested = pyqtSignal()
    sbobinaRequested = pyqtSignal()
    importSlideRequested = pyqtSignal()

    TAB_NAMES: Tuple[str, ...] = ("Edita", "Inserisci", "Gruppo", "Studio", "Whis")

    def __init__(
        self,
        theme_key: str = "obsidian",
        palette: Optional[Dict[str, str]] = None,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._theme_key = theme_key
        self._palette: Dict[str, str] = dict(palette or {})
        self._is_dark = bool(is_dark)
        self._collapsed = True
        self._full_width_mode = True
        self._active_group_color: Optional[str] = None
        self._groups: List[Dict[str, Any]] = []
        self._current_tab_ctx: Any = None
        self._last_hl_hex = "#FDE047"
        self._last_col_hex = "#EF4444"
        self._icon_bindings: List[Tuple[QToolButton, str, str, int]] = []
        self._dividers: List[QFrame] = []

        self.setObjectName("FlowNotesRibbon")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 2, 10, 2)
        root.setSpacing(4)

        # ---- 1. TAB STRIP SUPERIORE ----
        tab_bar = QWidget(self)
        tab_bar.setObjectName("RibbonTabBar")
        tab_bar.setFixedHeight(28)
        tab_lay = QHBoxLayout(tab_bar)
        tab_lay.setContentsMargins(2, 0, 2, 0)
        tab_lay.setSpacing(2)

        self.tab_buttons: List[_RibbonTabButton] = []
        self._tab_btn_group = QButtonGroup(self)
        self._tab_btn_group.setExclusive(True)
        for idx, name in enumerate(self.TAB_NAMES):
            btn = _RibbonTabButton(name, tab_bar)
            btn.setObjectName("RibbonTabBtn")
            btn.setFixedHeight(26)
            btn.setChecked(idx == 0)
            btn.clicked.connect(lambda _c=False, i=idx: self._on_tab_button_clicked(i))
            btn.doubleClicked.connect(lambda i=idx: self._on_tab_double_clicked(i))
            self._tab_btn_group.addButton(btn, idx)
            self.tab_buttons.append(btn)
            tab_lay.addWidget(btn)

        tab_lay.addStretch(1)

        self.btn_width_quick = QToolButton(tab_bar)
        self.btn_width_quick.setObjectName("RibbonQuickToggleBtn")
        self.btn_width_quick.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_width_quick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_width_quick.setCheckable(True)
        self.btn_width_quick.setFixedSize(26, 24)
        self.btn_width_quick.setToolTip("Centra colonna di lettura (760–840px) oppure allinea il testo a sinistra")
        self.btn_width_quick.clicked.connect(self._on_prose_width_toggled)
        tab_lay.addWidget(self.btn_width_quick)

        self.btn_collapse = QToolButton(tab_bar)
        self.btn_collapse.setObjectName("RibbonCollapseBtn")
        self.btn_collapse.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_collapse.setFixedSize(26, 24)
        self.btn_collapse.setToolTip("Comprimi o espandi la barra multifunzione (clic sul tab attivo)")
        self.btn_collapse.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        tab_lay.addWidget(self.btn_collapse)
        root.addWidget(tab_bar)

        # ---- 2. FLOATING ROUNDED RIBBON CARD ----
        self.ribbon_card = QFrame(self)
        self.ribbon_card.setObjectName("FlowNotesRibbonCard")
        self.ribbon_card.setFixedHeight(92)
        card_lay = QHBoxLayout(self.ribbon_card)
        card_lay.setContentsMargins(6, 2, 6, 2)
        card_lay.setSpacing(0)

        self.stack = QStackedWidget(self.ribbon_card)
        card_lay.addWidget(self.stack, 1)
        root.addWidget(self.ribbon_card)

        self._build_edita_tab()
        self._build_inserisci_tab()
        self._build_gruppo_tab()
        self._build_studio_tab()
        self._build_whis_tab()

        self.stack.setCurrentIndex(0)
        self.ribbon_card.setVisible(False)
        self.set_theme(self._theme_key, self._palette, self._is_dark)

    # -----------------------------------------------------------------
    # WIDGET FACTORY HELPERS
    # -----------------------------------------------------------------
    def _new_page(self) -> Tuple[QWidget, QHBoxLayout]:
        w = QWidget(self.stack)
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        w.setAutoFillBackground(False)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(2)
        self.stack.addWidget(w)
        return w, lay

    def _add_group_divider(self, parent_lay: QHBoxLayout):
        div = QFrame(self.ribbon_card)
        div.setObjectName("RibbonGroupDivider")
        div.setFixedWidth(1)
        div.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        parent_lay.addWidget(div)
        self._dividers.append(div)

    def _make_large_btn(
        self,
        text: str,
        icon_name: str,
        tip: str,
        *,
        role: str = "text",
        checkable: bool = False,
        popup_arrow: bool = False,
        object_name: str = "RibbonLargeBtn",
    ) -> QToolButton:
        btn = QToolButton(self.ribbon_card)
        btn.setObjectName(object_name)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setText(f"{text} ▾" if popup_arrow else text)
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        btn.setIconSize(QSize(18, 18))
        btn.setMinimumWidth(52)
        btn.setFixedHeight(66)
        self._icon_bindings.append((btn, icon_name, role, 18))
        return btn

    def _make_small_btn(
        self,
        text: str,
        tip: str,
        *,
        icon_name: Optional[str] = None,
        role: str = "text",
        checkable: bool = False,
        min_w: int = 26,
        font_bold: bool = False,
        font_italic: bool = False,
        font_underline: bool = False,
        font_strike: bool = False,
    ) -> QToolButton:
        btn = QToolButton(self.ribbon_card)
        btn.setObjectName("RibbonSmallBtn")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setCheckable(checkable)
        btn.setToolTip(tip)
        btn.setMinimumWidth(min_w)
        btn.setFixedHeight(26)
        if icon_name:
            btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text else Qt.ToolButtonStyle.ToolButtonIconOnly
            )
            btn.setIconSize(QSize(14, 14))
            self._icon_bindings.append((btn, icon_name, role, 14))
        else:
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        if text:
            btn.setText(text)
        f = QFont("Segoe UI", 9)
        f.setBold(font_bold)
        f.setItalic(font_italic)
        f.setUnderline(font_underline)
        f.setStrikeOut(font_strike)
        btn.setFont(f)
        return btn

    def _make_style_tile(self, title: str, level: int, tip: str) -> QToolButton:
        tile = QToolButton(self.ribbon_card)
        tile.setObjectName("RibbonStyleTile")
        tile.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tile.setCursor(Qt.CursorShape.PointingHandCursor)
        tile.setCheckable(True)
        tile.setText(title)
        tile.setToolTip(tip)
        tile.setFixedSize(64, 54)
        f = QFont("Segoe UI", 9 if level == 0 else (11 if level == 1 else (10 if level == 2 else 9)))
        f.setBold(level in (1, 2))
        f.setWeight(QFont.Weight.DemiBold if level == 3 else (QFont.Weight.Bold if level in (1, 2) else QFont.Weight.Normal))
        tile.setFont(f)
        tile.clicked.connect(lambda _c=False, lvl=level: self.headingLevelRequested.emit(lvl))
        return tile

    # -----------------------------------------------------------------
    # COSTRUZIONE 5 TAB WORD FLUENT
    # -----------------------------------------------------------------
    def _build_edita_tab(self):
        _, page_lay = self._new_page()

        # 1. Gruppo Carattere (2 righe compatte stile Word Home)
        grp_font = _RibbonGroupWidget("Carattere", self.ribbon_card)
        font_rows = QWidget(grp_font)
        fl = QVBoxLayout(font_rows)
        fl.setContentsMargins(0, 2, 0, 2)
        fl.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(3)
        self.combo_font_size = QComboBox(self.ribbon_card)
        self.combo_font_size.setObjectName("RibbonFontSizeCombo")
        self.combo_font_size.setEditable(True)
        self.combo_font_size.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_font_size.setFixedSize(56, 26)
        self.combo_font_size.setToolTip("Dimensione carattere del testo (1–100 pt)")
        self.combo_font_size.addItems(
            ["8", "9", "10", "11", "12", "14", "16", "18", "20", "22", "24", "28", "36", "48", "72", "96"]
        )
        if self.combo_font_size.lineEdit() is not None:
            self.combo_font_size.lineEdit().setValidator(QIntValidator(1, 100, self.combo_font_size))
            self.combo_font_size.lineEdit().returnPressed.connect(self._emit_font_size_from_combo)
        self.combo_font_size.setCurrentText("11")
        self.combo_font_size.textActivated.connect(lambda _t: self._emit_font_size_from_combo())

        self.btn_font_up = self._make_small_btn("A+", "Aumenta dimensione carattere testo selezionato", icon_name="zoom_in", min_w=40)
        self.btn_font_up.clicked.connect(lambda: self.fontSizeDeltaRequested.emit(1))
        self.btn_font_down = self._make_small_btn("A-", "Riduci dimensione carattere testo selezionato", icon_name="zoom_out", min_w=40)
        self.btn_font_down.clicked.connect(lambda: self.fontSizeDeltaRequested.emit(-1))
        self.btn_clear_fmt = self._make_small_btn("Pulisci", "Cancella formattazione inline dalla selezione", icon_name="eraser", min_w=60)
        self.btn_clear_fmt.clicked.connect(self.clearFormatRequested.emit)
        row1.addWidget(self.combo_font_size)
        row1.addWidget(self.btn_font_up)
        row1.addWidget(self.btn_font_down)
        row1.addWidget(self.btn_clear_fmt)
        row1.addStretch(1)
        fl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(2)
        self.btn_bold = self._make_small_btn("G", "Grassetto (Ctrl+B)", checkable=True, font_bold=True)
        self.btn_bold.clicked.connect(self.boldRequested.emit)
        self.btn_italic = self._make_small_btn("C", "Corsivo (Ctrl+I)", checkable=True, font_italic=True)
        self.btn_italic.clicked.connect(self.italicRequested.emit)
        self.btn_underline = self._make_small_btn("S", "Sottolineato (Ctrl+U)", checkable=True, font_underline=True)
        self.btn_underline.clicked.connect(self.underlineRequested.emit)
        self.btn_strike = self._make_small_btn("ab", "Barrato", checkable=True, font_strike=True, min_w=28)
        self.btn_strike.clicked.connect(self.strikeRequested.emit)
        self.btn_sub = self._make_small_btn("x₂", "Pedice", checkable=True, min_w=28)
        self.btn_sub.clicked.connect(self.subscriptRequested.emit)
        self.btn_sup = self._make_small_btn("x²", "Apice", checkable=True, min_w=28)
        self.btn_sup.clicked.connect(self.superscriptRequested.emit)

        self.btn_highlight = self._make_small_btn("▾", "Evidenziatore testo", min_w=42)
        self.btn_highlight.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_highlight.clicked.connect(self._open_highlight_menu)

        self.btn_text_color = self._make_small_btn("▾", "Colore testo", min_w=42)
        self.btn_text_color.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_text_color.clicked.connect(self._open_text_color_menu)

        for b in (
            self.btn_bold, self.btn_italic, self.btn_underline,
            self.btn_strike, self.btn_sub, self.btn_sup,
            self.btn_highlight, self.btn_text_color,
        ):
            row2.addWidget(b)
        fl.addLayout(row2)
        grp_font.body_layout.addWidget(font_rows)
        page_lay.addWidget(grp_font)
        self._add_group_divider(page_lay)

        # 2. Gruppo Paragrafo (2 righe compatte)
        grp_para = _RibbonGroupWidget("Paragrafo", self.ribbon_card)
        para_rows = QWidget(grp_para)
        pl = QVBoxLayout(para_rows)
        pl.setContentsMargins(0, 2, 0, 2)
        pl.setSpacing(4)

        p_row1 = QHBoxLayout()
        p_row1.setContentsMargins(0, 0, 0, 0)
        p_row1.setSpacing(2)
        self.btn_bullets = self._make_small_btn("", "Elenco puntato (/elenco oppure '- ')", icon_name="list_bullets", min_w=28)
        self.btn_bullets.clicked.connect(self.bulletListRequested.emit)
        self.btn_numbers = self._make_small_btn("", "Elenco numerato (1. ...)", icon_name="list_numbers", min_w=28)
        self.btn_numbers.clicked.connect(self.numberedListRequested.emit)
        self.btn_todo = self._make_small_btn("", "Lista To-Do interattiva (/todo)", icon_name="check_square", min_w=28)
        self.btn_todo.clicked.connect(self.todoListRequested.emit)
        self.btn_outdent = self._make_small_btn("", "Riduci rientro (Shift+Tab)", icon_name="outdent", min_w=28)
        self.btn_outdent.clicked.connect(lambda: self.indentDeltaRequested.emit(-1))
        self.btn_indent = self._make_small_btn("", "Aumenta rientro (Tab)", icon_name="indent", min_w=28)
        self.btn_indent.clicked.connect(lambda: self.indentDeltaRequested.emit(1))
        for b in (self.btn_bullets, self.btn_numbers, self.btn_todo, self.btn_outdent, self.btn_indent):
            p_row1.addWidget(b)
        pl.addLayout(p_row1)

        p_row2 = QHBoxLayout()
        p_row2.setContentsMargins(0, 0, 0, 0)
        p_row2.setSpacing(2)
        self.btn_quote = self._make_small_btn("Cit.", "Blocco citazione (/quote)", icon_name="quotes", min_w=54)
        self.btn_quote.clicked.connect(self.quoteRequested.emit)
        self.btn_callout = self._make_small_btn("Nota", "Blocco Callout in evidenza (/callout)", icon_name="info", min_w=56)
        self.btn_callout.clicked.connect(self.calloutRequested.emit)
        self.btn_link_note = self._make_small_btn("", "Collega nota ([[Titolo]])", icon_name="link", min_w=28)
        self.btn_link_note.clicked.connect(self.linkNoteRequested.emit)
        for b in (self.btn_quote, self.btn_callout, self.btn_link_note):
            p_row2.addWidget(b)
        p_row2.addStretch(1)
        pl.addLayout(p_row2)
        grp_para.body_layout.addWidget(para_rows)
        page_lay.addWidget(grp_para)
        self._add_group_divider(page_lay)

        # 3. Gruppo Stili (Galleria tile cliccabili stile Word)
        grp_styles = _RibbonGroupWidget("Stili", self.ribbon_card)
        self.style_gallery = QFrame(grp_styles)
        self.style_gallery.setObjectName("RibbonStyleGallery")
        sg_lay = QHBoxLayout(self.style_gallery)
        sg_lay.setContentsMargins(4, 3, 4, 3)
        sg_lay.setSpacing(4)

        self._style_btn_group = QButtonGroup(self)
        self._style_btn_group.setExclusive(True)
        self.style_tiles: Dict[int, QToolButton] = {}
        for lvl, lbl, tip in (
            (0, "Normale", "Paragrafo di testo normale"),
            (1, "Titolo 1", "Titolo grande di sezione (/h1)"),
            (2, "Titolo 2", "Sottotitolo di sezione (/h2)"),
            (3, "Titolo 3", "Titolo di terzo livello (/h3)"),
        ):
            t = self._make_style_tile(lbl, lvl, tip)
            self._style_btn_group.addButton(t, lvl)
            self.style_tiles[lvl] = t
            sg_lay.addWidget(t)
        self.style_tiles[0].setChecked(True)
        grp_styles.body_layout.addWidget(self.style_gallery)
        page_lay.addWidget(grp_styles)
        self._add_group_divider(page_lay)

        # 4. Gruppo Rapidi (Tabella / Matrice / Formula)
        grp_quick = _RibbonGroupWidget("Rapidi", self.ribbon_card)
        b_tbl = self._make_large_btn("Tabella", "table", "Inserisci tabella interattiva (/tabella)")
        b_tbl.clicked.connect(self.insertTableRequested.emit)
        b_mat = self._make_large_btn("Matrice", "matrix", "Inserisci matrice LaTeX (/matrice)")
        b_mat.clicked.connect(self.insertMatrixRequested.emit)
        b_frm = self._make_large_btn("Formula", "math_ops", "Inserisci equazione LaTeX $$...$$ (/formula)")
        b_frm.clicked.connect(self.insertFormulaRequested.emit)
        for b in (b_tbl, b_mat, b_frm):
            grp_quick.body_layout.addWidget(b)
        page_lay.addWidget(grp_quick)
        page_lay.addStretch(1)

    def _build_inserisci_tab(self):
        _, page_lay = self._new_page()

        # Strutture
        grp_struct = _RibbonGroupWidget("Strutture", self.ribbon_card)
        b_tbl = self._make_large_btn("Tabella", "table", "Tabella interattiva con navigazione Tab")
        b_tbl.clicked.connect(self.insertTableRequested.emit)
        b_mat = self._make_large_btn("Matrice", "matrix", "Matrice LaTeX interattiva")
        b_mat.clicked.connect(self.insertMatrixRequested.emit)
        b_frm = self._make_large_btn("Formula", "math_ops", "Blocco equazione $$...$$")
        b_frm.clicked.connect(self.insertFormulaRequested.emit)
        for b in (b_tbl, b_mat, b_frm):
            grp_struct.body_layout.addWidget(b)
        page_lay.addWidget(grp_struct)
        self._add_group_divider(page_lay)

        # Media & File
        grp_media = _RibbonGroupWidget("Media & File", self.ribbon_card)
        b_img = self._make_large_btn("Immagine", "image", "Incorpora immagine nella nota (/immagine)")
        b_img.clicked.connect(self.insertImageRequested.emit)
        b_file = self._make_large_btn("Allega", "paperclip", "Allega file PDF, Doc o Immagine (/file)")
        b_file.clicked.connect(self.insertFileRequested.emit)
        b_ocr = self._make_large_btn("OCR", "camera", "Estrai LaTeX o tabella da immagine appunti (Ctrl+Shift+V)")
        b_ocr.clicked.connect(self.insertOcrRequested.emit)
        for b in (b_img, b_file, b_ocr):
            grp_media.body_layout.addWidget(b)
        page_lay.addWidget(grp_media)
        self._add_group_divider(page_lay)

        # Blocchi
        grp_blocks = _RibbonGroupWidget("Blocchi", self.ribbon_card)
        b_code = self._make_large_btn("Codice", "code", "Inserisci blocco codice Python (/code)")
        b_code.clicked.connect(self.insertCodeRequested.emit)
        b_bib = self._make_large_btn("BibTeX", "book", "Inserisci citazione accademica BibTeX (/bibtex)")
        b_bib.clicked.connect(self.insertBibtexRequested.emit)
        b_qt = self._make_large_btn("Citazione", "quotes", "Inserisci blocco citazione (/quote)")
        b_qt.clicked.connect(self.quoteRequested.emit)
        b_co = self._make_large_btn("Callout", "info", "Inserisci nota in evidenza (/callout)")
        b_co.clicked.connect(self.calloutRequested.emit)
        b_lk = self._make_large_btn("Collega", "link", "Inserisci link interno [[Nota]]")
        b_lk.clicked.connect(self.linkNoteRequested.emit)
        for b in (b_code, b_bib, b_qt, b_co, b_lk):
            grp_blocks.body_layout.addWidget(b)
        page_lay.addWidget(grp_blocks)
        page_lay.addStretch(1)

    def _build_gruppo_tab(self):
        _, page_lay = self._new_page()

        # Collaborazione (incluso bottone mutante Modifica <-> Salva)
        grp_collab = _RibbonGroupWidget("Collaborazione", self.ribbon_card)
        self.btn_lock_action = self._make_large_btn(
            "Modifica",
            "lock_open",
            "Acquisisci il lock di modifica o salva/pubblica la nota nel gruppo",
            object_name="RibbonMutateBtn",
        )
        self.btn_lock_action.setMinimumWidth(68)
        self.btn_lock_action.clicked.connect(self._on_lock_action_clicked)

        self.btn_publish_group = self._make_large_btn(
            "Pubblica",
            "cloud",
            "Pubblica questa nota locale in un FlowGroup",
            popup_arrow=True,
        )
        self.btn_publish_group.setMinimumWidth(70)
        self.btn_publish_group.clicked.connect(self._open_publish_menu)

        self.btn_copy_or_dup = self._make_large_btn(
            "Duplica",
            "copy",
            "Copia nel Quaderno locale o duplica la nota",
        )
        self.btn_copy_or_dup.setMinimumWidth(66)
        self.btn_copy_or_dup.clicked.connect(self._on_copy_or_dup_clicked)

        for b in (self.btn_lock_action, self.btn_publish_group, self.btn_copy_or_dup):
            grp_collab.body_layout.addWidget(b)
        page_lay.addWidget(grp_collab)
        self._add_group_divider(page_lay)

        # Stato & Versioni
        grp_state = _RibbonGroupWidget("Stato & Versioni", self.ribbon_card)
        self.btn_status = self._make_large_btn(
            "Privata",
            "dot",
            "Cambia stato (Privata, Pubblicata, In manutenzione)",
            popup_arrow=True,
        )
        self.btn_status.setMinimumWidth(78)
        self.btn_status.clicked.connect(self._open_status_menu)

        self.btn_history = self._make_large_btn(
            "Cronologia",
            "clock",
            "Cronologia versioni (Time Machine)",
        )
        self.btn_history.setMinimumWidth(68)
        self.btn_history.clicked.connect(self.historyRequested.emit)

        self.btn_export = self._make_large_btn(
            "Esporta",
            "download",
            "Esporta in Markdown, LaTeX, Word o PDF",
            popup_arrow=True,
        )
        self.btn_export.setMinimumWidth(66)
        self.btn_export.clicked.connect(self._open_export_menu)

        self.btn_delete = self._make_large_btn(
            "Elimina",
            "trash",
            "Elimina la nota corrente",
            role="danger",
        )
        self.btn_delete.clicked.connect(self.deleteNoteRequested.emit)

        for b in (self.btn_status, self.btn_history, self.btn_export, self.btn_delete):
            grp_state.body_layout.addWidget(b)
        page_lay.addWidget(grp_state)
        page_lay.addStretch(1)

    def _build_studio_tab(self):
        _, page_lay = self._new_page()

        grp_learn = _RibbonGroupWidget("Apprendimento", self.ribbon_card)
        self.btn_studio_view = self._make_large_btn(
            "Vista Studio",
            "book",
            "Alterna editor e Vista Studio impaginata (Ctrl+E)",
            checkable=True,
        )
        self.btn_studio_view.setMinimumWidth(74)
        self.btn_studio_view.clicked.connect(lambda chk: self.studioViewToggled.emit(bool(chk)))

        self.btn_rsvp = self._make_large_btn("RSVP", "lightning", "Lettura veloce RSVP (Alt+R)")
        self.btn_rsvp.clicked.connect(self.rsvpRequested.emit)

        self.btn_flashcards = self._make_large_btn("Flashcard", "cards", "Estrai flashcard d'esame da questa nota")
        self.btn_flashcards.setMinimumWidth(62)
        self.btn_flashcards.clicked.connect(self.flashcardsRequested.emit)

        for b in (self.btn_studio_view, self.btn_rsvp, self.btn_flashcards):
            grp_learn.body_layout.addWidget(b)
        page_lay.addWidget(grp_learn)
        self._add_group_divider(page_lay)

        grp_audio = _RibbonGroupWidget("Audio & Sbobina", self.ribbon_card)
        self.btn_sbobina = self._make_large_btn("Sbobina", "mic", "Sbobina un file audio o una lezione")
        self.btn_sbobina.clicked.connect(self.sbobinaRequested.emit)

        self.btn_marker = self._make_large_btn("Marcatore", "clock", "Inserisci marcatore audio [HH:MM:SS]")
        self.btn_marker.setMinimumWidth(66)
        self.btn_marker.clicked.connect(self.timestampMarkerRequested.emit)

        for b in (self.btn_sbobina, self.btn_marker):
            grp_audio.body_layout.addWidget(b)
        page_lay.addWidget(grp_audio)
        self._add_group_divider(page_lay)

        grp_ws = _RibbonGroupWidget("Workspace", self.ribbon_card)
        self.btn_split_view = self._make_large_btn(
            "Split-View",
            "split_h",
            "Affianca due note a destra (Ctrl+\\)",
            checkable=True,
        )
        self.btn_split_view.setMinimumWidth(64)
        self.btn_split_view.clicked.connect(self.splitViewRequested.emit)

        self.btn_send_split = self._make_large_btn(
            "Invia Split",
            "arrow_right",
            "Invia il testo selezionato al pannello affiancato",
        )
        self.btn_send_split.setMinimumWidth(66)
        self.btn_send_split.clicked.connect(self.sendToSplitRequested.emit)

        self.btn_focus = self._make_large_btn(
            "Focus",
            "expand",
            "Nascondi sidebar per scrittura senza distrazioni",
            checkable=True,
        )
        self.btn_focus.clicked.connect(self.focusModeRequested.emit)

        self.btn_full_width = self._make_large_btn(
            "Centrato",
            "split_h",
            "Centra colonna di lettura (760–840px) oppure allinea il testo a sinistra",
            checkable=True,
        )
        self.btn_full_width.setMinimumWidth(62)
        self.btn_full_width.clicked.connect(self._on_prose_width_toggled)

        self.btn_favorite = self._make_large_btn(
            "Preferito",
            "star",
            "Aggiungi o rimuovi questa nota dai Preferiti",
            checkable=True,
        )
        self.btn_favorite.clicked.connect(self.favoriteToggled.emit)

        for b in (self.btn_split_view, self.btn_send_split, self.btn_focus, self.btn_full_width, self.btn_favorite):
            grp_ws.body_layout.addWidget(b)
        page_lay.addWidget(grp_ws)
        page_lay.addStretch(1)

    def _build_whis_tab(self):
        _, page_lay = self._new_page()

        grp_whis = _RibbonGroupWidget("Assistente Nota", self.ribbon_card)
        b_ask = self._make_large_btn(
            "Chiedi Whis",
            "sparkles",
            "Apri Whis sul contesto di questa nota",
            role="accent",
        )
        b_ask.setMinimumWidth(74)
        b_ask.clicked.connect(self.askWhisRequested.emit)

        b_sum = self._make_large_btn("Riassumi AI", "magic", "Genera un blocco di sintesi AI della nota")
        b_sum.setMinimumWidth(72)
        b_sum.clicked.connect(self.summarizeRequested.emit)

        b_fix = self._make_large_btn("Correggi AI", "edit", "Correggi ortografia e chiarezza della selezione")
        b_fix.setMinimumWidth(70)
        b_fix.clicked.connect(self.aiCorrectSelectionRequested.emit)

        b_fc = self._make_large_btn("Flashcard AI", "cards", "Genera flashcard di ripasso con AI")
        b_fc.setMinimumWidth(74)
        b_fc.clicked.connect(self.flashcardsRequested.emit)

        for b in (b_ask, b_sum, b_fix, b_fc):
            grp_whis.body_layout.addWidget(b)
        page_lay.addWidget(grp_whis)
        self._add_group_divider(page_lay)

        grp_docs = _RibbonGroupWidget("Documenti", self.ribbon_card)
        b_imp = self._make_large_btn("Importa", "download", "Importa PDF, Slide o Immagini")
        b_imp.clicked.connect(self.importSlideRequested.emit)
        grp_docs.body_layout.addWidget(b_imp)
        page_lay.addWidget(grp_docs)
        page_lay.addStretch(1)

    # -----------------------------------------------------------------
    # MENU CONTESTUALI & AZIONI DINAMICHE
    # -----------------------------------------------------------------
    def _menu_stylesheet(self) -> str:
        p = self._palette
        bg = p.get("combo_popup_bg") or p.get("dialog_bg") or ("#161B26" if self._is_dark else "#FFFFFF")
        fg = p.get("text") or ("#F8FAFC" if self._is_dark else "#0F172A")
        bd = p.get("card_border") or ("#2D3748" if self._is_dark else "#CBD5E1")
        hv = p.get("btn_hover_bg") or ("#253042" if self._is_dark else "#E2E8F0")
        return (
            f"QMenu {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; border-radius: 8px; padding: 5px; }}"
            f"QMenu::item {{ padding: 6px 12px; border-radius: 5px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {hv}; }}"
            f"QMenu::separator {{ height: 1px; background-color: {bd}; margin: 4px 8px; }}"
        )

    def _open_highlight_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        for name, hex_c in HIGHLIGHT_COLORS:
            act = menu.addAction(_make_swatch_icon(hex_c, 12, self._is_dark), name)
            act.triggered.connect(lambda _c=False, h=hex_c: self._pick_highlight(h))
        menu.addSeparator()
        rem = menu.addAction(_make_swatch_icon(None, 12, self._is_dark), "Rimuovi evidenziazione")
        rem.triggered.connect(lambda: self.highlightRequested.emit(""))
        menu.exec(self.btn_highlight.mapToGlobal(QPoint(0, self.btn_highlight.height() + 4)))

    def _pick_highlight(self, hex_c: str):
        self._last_hl_hex = hex_c
        self._refresh_color_split_icons()
        self.highlightRequested.emit(hex_c)

    def _open_text_color_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        for name, hex_c in TEXT_COLORS:
            act = menu.addAction(_make_swatch_icon(hex_c, 12, self._is_dark), name)
            act.triggered.connect(lambda _c=False, h=hex_c: self._pick_text_color(h))
        menu.addSeparator()
        rem = menu.addAction(_make_swatch_icon(None, 12, self._is_dark), "Colore predefinito")
        rem.triggered.connect(lambda: self.textColorRequested.emit(""))
        menu.exec(self.btn_text_color.mapToGlobal(QPoint(0, self.btn_text_color.height() + 4)))

    def _pick_text_color(self, hex_c: str):
        self._last_col_hex = hex_c
        self._refresh_color_split_icons()
        self.textColorRequested.emit(hex_c)

    def _on_lock_action_clicked(self):
        tab = self._current_tab_ctx
        if tab is None or getattr(tab, "workspace_type", None) != "group":
            return
        if getattr(tab, "is_locked_by_me", False):
            self.publishGroupRequested.emit()
        else:
            self.acquireLockRequested.emit()

    def _on_copy_or_dup_clicked(self):
        tab = self._current_tab_ctx
        if tab is not None and getattr(tab, "workspace_type", None) == "group":
            self.copyToLocalRequested.emit()
        else:
            self.duplicateNoteRequested.emit()

    def _open_publish_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        if self._groups:
            for g in self._groups:
                gid = int(g["id"])
                gcol = str(g.get("accent_color") or g.get("color") or "#38BDF8")
                act = menu.addAction(_make_swatch_icon(gcol, 11, self._is_dark), str(g.get("group_name") or "Gruppo"))
                act.triggered.connect(lambda _c=False, target_id=gid: self.publishLocalToSpecificGroupRequested.emit(target_id))
            menu.addSeparator()
        act_new = menu.addAction(get_icon("plus", theme_key=self._theme_key, role="accent", size=14), "Crea o Unisciti a un Gruppo...")
        act_new.triggered.connect(self.joinOrCreateGroupRequested.emit)
        menu.exec(self.btn_publish_group.mapToGlobal(QPoint(0, self.btn_publish_group.height() + 4)))

    def _open_status_menu(self):
        tab = self._current_tab_ctx
        if tab is None or getattr(tab, "workspace_type", "") == "board" or getattr(tab, "is_trashed", False):
            return
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        if tab.workspace_type == "local":
            act_priv = menu.addAction(_make_swatch_icon("#94A3B8", 10, self._is_dark), "Privata (Quaderno locale)")
            act_priv.setCheckable(True)
            act_priv.setChecked(True)
            act_priv.setEnabled(False)
            if self._groups:
                sub = menu.addMenu(get_icon("cloud", theme_key=self._theme_key, role="accent", size=14), "Pubblica in un Gruppo")
                sub.setStyleSheet(self._menu_stylesheet())
                for g in self._groups:
                    gid = int(g["id"])
                    gcol = str(g.get("accent_color") or g.get("color") or "#38BDF8")
                    ag = sub.addAction(_make_swatch_icon(gcol, 10, self._is_dark), str(g.get("group_name") or "Gruppo"))
                    ag.triggered.connect(lambda _c=False, tid=gid: self.publishLocalToSpecificGroupRequested.emit(tid))
            menu.addSeparator()
            act_del = menu.addAction(get_icon("trash", theme_key=self._theme_key, role="danger", size=14), "Elimina nota")
            act_del.triggered.connect(self.deleteNoteRequested.emit)
        elif tab.workspace_type == "group":
            is_maint = (getattr(tab, "note_status", "published") == "maintenance")
            can_maint = bool(getattr(tab, "can_set_maintenance", False))
            can_del = bool(getattr(tab, "can_delete_in_group", False))

            act_pub = menu.addAction(_make_swatch_icon("#10B981", 10, self._is_dark), "Pubblicata nel gruppo")
            act_pub.setCheckable(True)
            act_pub.setChecked(not is_maint)
            act_pub.setEnabled(is_maint and can_maint)
            act_pub.triggered.connect(lambda: self.noteStatusChangeRequested.emit("published"))

            act_m = menu.addAction(_make_swatch_icon("#F59E0B", 10, self._is_dark), "In manutenzione (sola lettura)")
            act_m.setCheckable(True)
            act_m.setChecked(is_maint)
            act_m.setEnabled((not is_maint) and can_maint)
            act_m.triggered.connect(lambda: self.noteStatusChangeRequested.emit("maintenance"))

            menu.addSeparator()
            act_priv = menu.addAction(get_icon("lock", theme_key=self._theme_key, role="text", size=14), "Rendi Privata (sposta nel Quaderno)")
            act_priv.setEnabled(can_del)
            act_priv.triggered.connect(lambda: self.noteStatusChangeRequested.emit("make_private"))

            menu.addSeparator()
            act_del = menu.addAction(get_icon("trash", theme_key=self._theme_key, role="danger", size=14), "Elimina dal gruppo")
            act_del.setEnabled(can_del)
            act_del.triggered.connect(self.deleteNoteRequested.emit)
        menu.exec(self.btn_status.mapToGlobal(QPoint(0, self.btn_status.height() + 4)))

    def _open_export_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        for label, fmt in (
            ("Markdown (.md)", "md"),
            ("Documento LaTeX (.tex)", "tex"),
            ("Documento Word (.docx)", "docx"),
            ("Documento PDF (.pdf)", "pdf"),
        ):
            act = menu.addAction(label)
            act.triggered.connect(lambda _c=False, f=fmt: self.exportFormatRequested.emit(f))
        menu.exec(self.btn_export.mapToGlobal(QPoint(0, self.btn_export.height() + 4)))

    # -----------------------------------------------------------------
    # STATO PUBBLICO, TAB SWITCHING, COLLASSO E TEMA
    # -----------------------------------------------------------------
    def _on_tab_button_clicked(self, index: int):
        idx = max(0, min(len(self.TAB_NAMES) - 1, int(index)))
        if not self._collapsed and self.active_tab_index() == idx:
            self.set_collapsed(True)
            return
        self.set_active_tab(idx)

    def _on_tab_double_clicked(self, index: int):
        if self.active_tab_index() == index:
            self.set_collapsed(not self._collapsed)
        else:
            self.set_active_tab(index)
            if self._collapsed:
                self.set_collapsed(False)

    def _on_prose_width_toggled(self, centered: bool):
        full_w = not bool(centered)
        self.set_full_width_checked(full_w)
        self.proseWidthToggled.emit(full_w)

    def set_full_width_checked(self, full_width: bool):
        self._full_width_mode = bool(full_width)
        centered = not self._full_width_mode
        for b in (getattr(self, "btn_full_width", None), getattr(self, "btn_width_quick", None)):
            if b is not None:
                b.blockSignals(True)
                b.setChecked(centered)
                b.blockSignals(False)
        self._refresh_width_quick_icon()

    def active_tab_index(self) -> int:
        return self.stack.currentIndex()

    def set_active_tab(self, index: int):
        idx = max(0, min(len(self.TAB_NAMES) - 1, int(index)))
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.tab_buttons):
            btn.setChecked(i == idx)
        if self._collapsed:
            self.set_collapsed(False)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool):
        self._collapsed = bool(collapsed)
        self.ribbon_card.setVisible(not self._collapsed)
        self._refresh_collapse_icon()

    def set_groups(self, groups: List[Dict[str, Any]]):
        self._groups = list(groups or [])

    def update_heading_indicator(self, block_kind: str):
        lvl_map = {"h1": 1, "h2": 2, "h3": 3, "h4": 3}
        lvl = lvl_map.get(block_kind or "", 0)
        tile = self.style_tiles.get(lvl)
        if tile is not None:
            tile.setChecked(True)

    def _emit_font_size_from_combo(self):
        raw = (self.combo_font_size.currentText() or "").strip()
        try:
            val = int(raw)
        except ValueError:
            return
        clamped = max(1, min(100, val))
        if str(clamped) != raw:
            self.combo_font_size.blockSignals(True)
            self.combo_font_size.setEditText(str(clamped))
            self.combo_font_size.blockSignals(False)
        self.fontSizeRequested.emit(clamped)

    def update_inline_format_indicators(self, fmt_state: Dict[str, Any]):
        st = fmt_state or {}
        script = str(st.get("script") or "").strip().lower()
        pairs = (
            (self.btn_bold, bool(st.get("bold"))),
            (self.btn_italic, bool(st.get("italic"))),
            (self.btn_underline, bool(st.get("underline"))),
            (self.btn_strike, bool(st.get("strike"))),
            (self.btn_sub, script == "sub"),
            (self.btn_sup, script == "sup"),
        )
        for btn, checked in pairs:
            btn.blockSignals(True)
            btn.setChecked(checked)
            btn.blockSignals(False)
        fsz = st.get("font_size")
        if fsz is not None and hasattr(self, "combo_font_size"):
            le = self.combo_font_size.lineEdit()
            if not self.combo_font_size.hasFocus() and (le is None or not le.hasFocus()):
                self.combo_font_size.blockSignals(True)
                self.combo_font_size.setEditText(str(max(1, min(100, int(fsz)))))
                self.combo_font_size.blockSignals(False)

    def update_from_context(
        self,
        tab: Any,
        *,
        is_studio: bool = False,
        is_split: bool = False,
        is_focus: bool = False,
        is_favorite: bool = False,
        block_kind: str = "",
    ):
        self._current_tab_ctx = tab
        self.update_heading_indicator(block_kind)

        # Studio / Workspace toggles
        self.btn_studio_view.blockSignals(True)
        self.btn_studio_view.setChecked(bool(is_studio))
        self.btn_studio_view.setText("Modifica" if is_studio else "Vista Studio")
        self.btn_studio_view.blockSignals(False)

        self.btn_split_view.blockSignals(True)
        self.btn_split_view.setChecked(bool(is_split))
        self.btn_split_view.blockSignals(False)
        self.btn_send_split.setEnabled(bool(is_split))

        self.btn_focus.blockSignals(True)
        self.btn_focus.setChecked(bool(is_focus))
        self.btn_focus.blockSignals(False)

        is_local_saved = bool(tab and getattr(tab, "workspace_type", None) == "local" and getattr(tab, "note_id", None) is not None)
        self.btn_favorite.setEnabled(is_local_saved)
        self.btn_favorite.blockSignals(True)
        self.btn_favorite.setChecked(bool(is_favorite) if is_local_saved else False)
        self.btn_favorite.setIcon(
            get_icon(
                "star_fill" if (is_local_saved and is_favorite) else "star",
                theme_key=self._theme_key,
                role="warning" if (is_local_saved and is_favorite) else "text",
                size=18,
            )
        )
        self.btn_favorite.blockSignals(False)

        # Gruppo tab: tinta dinamica e mutazione Modifica <-> Salva
        ws_type = getattr(tab, "workspace_type", None) if tab is not None else None
        group_rec = getattr(tab, "group_rec", None) if tab is not None else None
        new_group_col: Optional[str] = None
        if ws_type in ("group", "board") and isinstance(group_rec, dict):
            new_group_col = str(group_rec.get("accent_color") or group_rec.get("color") or "#38BDF8")

        if new_group_col != self._active_group_color:
            self._active_group_color = new_group_col
            self._apply_tab_bar_qss()

        if ws_type == "group":
            is_maint = (getattr(tab, "note_status", "published") == "maintenance")
            is_mine = bool(getattr(tab, "is_locked_by_me", False))
            g_item = getattr(tab, "group_note_item", None) or {}
            locked_other = bool(g_item.get("locked_by_other"))
            lock_auth = g_item.get("lock_author") or ""

            self.btn_publish_group.setEnabled(False)
            self.btn_copy_or_dup.setText("Copia Locale")
            self.btn_copy_or_dup.setEnabled(True)
            self.btn_history.setEnabled(False)
            self.btn_status.setEnabled(True)
            self.btn_status.setText("Manutenz. ▾" if is_maint else "Pubblicata ▾")
            self.btn_status.setIcon(_make_swatch_icon("#F59E0B" if is_maint else "#10B981", 10, self._is_dark))
            self.btn_delete.setEnabled(bool(getattr(tab, "can_delete_in_group", False)))

            if is_maint:
                self._set_lock_button_visual("Manutenzione", "lock", enabled=False, save_mode=False)
                self.btn_lock_action.setToolTip("Nota in manutenzione: rimuovi la manutenzione per modificare")
            elif is_mine:
                self._set_lock_button_visual("Salva", "upload", enabled=True, save_mode=True)
                self.btn_lock_action.setToolTip("Salva e pubblica le modifiche nel gruppo (rilascia il lock)")
            elif locked_other:
                self._set_lock_button_visual("In uso", "lock", enabled=False, save_mode=False)
                self.btn_lock_action.setToolTip(f"Attualmente in modifica da {lock_auth}")
            else:
                self._set_lock_button_visual("Modifica", "lock_open", enabled=True, save_mode=False)
                self.btn_lock_action.setToolTip("Acquisisci il lock di modifica esclusiva nel gruppo")
        elif ws_type == "board":
            self._set_lock_button_visual("Modifica", "lock_open", enabled=False, save_mode=False)
            self.btn_publish_group.setEnabled(False)
            self.btn_copy_or_dup.setText("Duplica")
            self.btn_copy_or_dup.setEnabled(False)
            self.btn_status.setText("Bacheca")
            self.btn_status.setEnabled(False)
            self.btn_history.setEnabled(False)
            self.btn_delete.setEnabled(False)
        else:
            is_trashed = bool(getattr(tab, "is_trashed", False)) if tab is not None else False
            self._set_lock_button_visual("Modifica", "lock_open", enabled=False, save_mode=False)
            self.btn_lock_action.setToolTip("Disponibile quando apri una nota di un FlowGroup")
            self.btn_publish_group.setEnabled(tab is not None and not is_trashed)
            self.btn_copy_or_dup.setText("Duplica")
            self.btn_copy_or_dup.setEnabled(tab is not None and not is_trashed)
            self.btn_status.setText("Privata ▾")
            self.btn_status.setIcon(_make_swatch_icon("#94A3B8", 10, self._is_dark))
            self.btn_status.setEnabled(tab is not None and not is_trashed)
            self.btn_history.setEnabled(is_local_saved and not is_trashed)
            self.btn_delete.setEnabled(is_local_saved and not is_trashed)

    def _set_lock_button_visual(self, label: str, icon_name: str, *, enabled: bool, save_mode: bool):
        self.btn_lock_action.setText(label)
        self.btn_lock_action.setEnabled(enabled)
        self.btn_lock_action.setProperty("saveMode", bool(save_mode))
        self.btn_lock_action.setIcon(
            get_icon(icon_name, theme_key=self._theme_key, role="success" if save_mode else "text", size=18)
        )
        self._apply_lock_button_qss(save_mode=save_mode)

    def _refresh_collapse_icon(self):
        ic_name = "caret_down" if self._collapsed else "caret_up"
        self.btn_collapse.setIcon(get_icon(ic_name, theme_key=self._theme_key, role="subtitle", size=12))

    def _refresh_width_quick_icon(self):
        if hasattr(self, "btn_width_quick"):
            role = "accent" if (not self._full_width_mode) else "subtitle"
            self.btn_width_quick.setIcon(get_icon("expand", theme_key=self._theme_key, role=role, size=12))

    def _refresh_color_split_icons(self):
        fg = self._palette.get("text") or ("#E2E8F0" if self._is_dark else "#0F172A")
        self.btn_highlight.setIcon(_make_bar_letter_icon("ab", self._last_hl_hex, fg, size=16))
        self.btn_text_color.setIcon(_make_bar_letter_icon("A", self._last_col_hex, fg, size=16))

    def _apply_lock_button_qss(self, save_mode: bool):
        p = self._palette
        fg = p.get("text") or ("#E2E8F0" if self._is_dark else "#0F172A")
        hv = p.get("btn_hover_bg") or ("rgba(255, 255, 255, 0.08)" if self._is_dark else "#EEF2F7")
        muted = p.get("muted") or "#64748B"
        if save_mode:
            qss = (
                "QToolButton#RibbonMutateBtn {"
                " background-color: rgba(16, 185, 129, 0.16);"
                " border: 1px solid rgba(16, 185, 129, 0.55);"
                " border-radius: 6px; color: #10B981;"
                " font-size: 10.5px; font-weight: 700; padding: 4px 6px;"
                "}"
                "QToolButton#RibbonMutateBtn:hover {"
                " background-color: rgba(16, 185, 129, 0.26);"
                "}"
            )
        else:
            qss = (
                f"QToolButton#RibbonMutateBtn {{ background: transparent; border: 1px solid transparent; border-radius: 6px; color: {fg}; font-size: 10.5px; font-weight: 500; padding: 4px 6px; }}"
                f"QToolButton#RibbonMutateBtn:hover {{ background-color: {hv}; }}"
                f"QToolButton#RibbonMutateBtn:disabled {{ color: {muted}; }}"
            )
        self.btn_lock_action.setStyleSheet(qss)

    def _apply_tab_bar_qss(self):
        p = self._palette
        sub = p.get("subtitle") or ("#94A3B8" if self._is_dark else "#64748B")
        title = p.get("title") or ("#F8FAFC" if self._is_dark else "#0F172A")
        hv = p.get("btn_hover_bg") or ("rgba(255, 255, 255, 0.06)" if self._is_dark else "rgba(15, 23, 42, 0.05)")
        accent = "#38BDF8" if self._is_dark else "#2563EB"
        nav_bd = p.get("nav_checked_border") or ""
        if nav_bd.startswith("#"):
            accent = nav_bd

        for idx, btn in enumerate(self.tab_buttons):
            tab_accent = self._active_group_color if (idx == 2 and self._active_group_color) else accent
            if idx == 2 and self._active_group_color:
                btn.setIcon(_make_swatch_icon(self._active_group_color, 8, self._is_dark))
                btn.setIconSize(QSize(8, 8))
            else:
                btn.setIcon(QIcon())
            btn.setStyleSheet(
                f"QPushButton#RibbonTabBtn {{ background: transparent; border: none; border-bottom: 2.5px solid transparent; "
                f"color: {sub}; font-size: 11.5px; font-weight: 500; padding: 2px 11px; margin: 0px; }}"
                f"QPushButton#RibbonTabBtn:hover {{ color: {title}; background-color: {hv}; border-top-left-radius: 5px; border-top-right-radius: 5px; }}"
                f"QPushButton#RibbonTabBtn:checked {{ color: {title}; font-weight: 700; border-bottom: 2.5px solid {tab_accent}; }}"
            )

    def set_theme(self, theme_key: str, palette: Dict[str, str], is_dark: bool):
        self._theme_key = theme_key
        self._palette = dict(palette or {})
        self._is_dark = bool(is_dark)

        p = self._palette
        card_bg = p.get("ribbon_bg") or p.get("dialog_bg") or ("#121722" if self._is_dark else "#F8FAFC")
        card_bd = p.get("card_border") or ("rgba(255, 255, 255, 0.08)" if self._is_dark else "#CBD5E1")
        fg = p.get("text") or ("#E2E8F0" if self._is_dark else "#0F172A")
        title_fg = p.get("title") or ("#F8FAFC" if self._is_dark else "#0F172A")
        muted = p.get("muted") or ("#64748B" if self._is_dark else "#94A3B8")
        hv = p.get("btn_hover_bg") or ("rgba(255, 255, 255, 0.08)" if self._is_dark else "#E2E8F0")
        chk_bg = p.get("nav_checked_bg") or ("rgba(56, 189, 248, 0.16)" if self._is_dark else "#DBEAFE")
        chk_bd = p.get("nav_checked_border") or ("rgba(56, 189, 248, 0.48)" if self._is_dark else "#60A5FA")
        tile_bg = p.get("editor_bg") or ("#0B0E14" if self._is_dark else "#FFFFFF")
        div_col = p.get("divider") or ("rgba(148, 163, 184, 0.18)" if self._is_dark else "#CBD5E1")

        self.ribbon_card.setStyleSheet(
            f"QFrame#FlowNotesRibbonCard {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
            f"QLabel#RibbonGroupCaption {{ color: {muted}; font-size: 9.5px; font-weight: 500; background: transparent; }}"
            f"QToolButton#RibbonLargeBtn {{ background: transparent; border: 1px solid transparent; border-radius: 6px; color: {fg}; font-size: 10px; font-weight: 500; padding: 4px 5px; }}"
            f"QToolButton#RibbonLargeBtn:hover {{ background-color: {hv}; }}"
            f"QToolButton#RibbonLargeBtn:checked {{ background-color: {chk_bg}; border: 1px solid {chk_bd}; color: {title_fg}; font-weight: 600; }}"
            f"QToolButton#RibbonLargeBtn:disabled {{ color: {muted}; }}"
            f"QToolButton#RibbonSmallBtn {{ background: transparent; border: 1px solid transparent; border-radius: 5px; color: {fg}; font-size: 10.5px; padding: 2px 5px; }}"
            f"QToolButton#RibbonSmallBtn:hover {{ background-color: {hv}; }}"
            f"QToolButton#RibbonSmallBtn:checked {{ background-color: {chk_bg}; border: 1px solid {chk_bd}; color: {title_fg}; }}"
            f"QToolButton#RibbonSmallBtn:disabled {{ color: {muted}; }}"
            f"QFrame#RibbonStyleGallery {{ background-color: {tile_bg}; border: 1px solid {card_bd}; border-radius: 7px; }}"
            f"QToolButton#RibbonStyleTile {{ background-color: transparent; border: 1px solid {card_bd}; border-radius: 5px; color: {fg}; padding: 2px 4px; }}"
            f"QToolButton#RibbonStyleTile:hover {{ background-color: {hv}; }}"
            f"QToolButton#RibbonStyleTile:checked {{ background-color: {chk_bg}; border: 1.5px solid {chk_bd}; color: {title_fg}; }}"
            f"QComboBox#RibbonFontSizeCombo {{ background-color: {tile_bg}; color: {fg}; border: 1px solid {card_bd}; border-radius: 5px; padding: 1px 6px; font-size: 10.5px; font-weight: 600; min-height: 24px; }}"
            f"QComboBox#RibbonFontSizeCombo:hover {{ border: 1px solid {chk_bd}; }}"
            f"QComboBox#RibbonFontSizeCombo::drop-down {{ border: none; width: 16px; }}"
            f"QComboBox#RibbonFontSizeCombo QAbstractItemView {{ background-color: {tile_bg}; color: {fg}; border: 1px solid {card_bd}; selection-background-color: {hv}; }}"
        )

        self.btn_collapse.setStyleSheet(
            f"QToolButton#RibbonCollapseBtn {{ background: transparent; border: none; border-radius: 5px; color: {muted}; }}"
            f"QToolButton#RibbonCollapseBtn:hover {{ background-color: {hv}; color: {title_fg}; }}"
        )
        self.btn_width_quick.setStyleSheet(
            f"QToolButton#RibbonQuickToggleBtn {{ background: transparent; border: none; border-radius: 5px; color: {muted}; }}"
            f"QToolButton#RibbonQuickToggleBtn:hover {{ background-color: {hv}; color: {title_fg}; }}"
            f"QToolButton#RibbonQuickToggleBtn:checked {{ background-color: {chk_bg}; border: 1px solid {chk_bd}; }}"
        )

        for div in self._dividers:
            div.setStyleSheet(f"QFrame#RibbonGroupDivider {{ background-color: {div_col}; border: none; margin: 6px 2px; }}")

        for btn, icon_name, role, size in self._icon_bindings:
            if btn is self.btn_lock_action or btn is self.btn_favorite or btn is self.btn_status:
                continue
            btn.setIcon(get_icon(icon_name, theme_key=self._theme_key, role=role, size=size))

        self._apply_tab_bar_qss()
        self._refresh_collapse_icon()
        self._refresh_width_quick_icon()
        self._refresh_color_split_icons()

        save_mode = bool(self.btn_lock_action.property("saveMode"))
        self._apply_lock_button_qss(save_mode=save_mode)
        if self._current_tab_ctx is not None:
            self.update_from_context(
                self._current_tab_ctx,
                is_studio=self.btn_studio_view.isChecked(),
                is_split=self.btn_split_view.isChecked(),
                is_focus=self.btn_focus.isChecked(),
                is_favorite=self.btn_favorite.isChecked(),
            )
        else:
            self.btn_lock_action.setIcon(get_icon("lock_open", theme_key=self._theme_key, role="text", size=18))
            self.btn_favorite.setIcon(get_icon("star", theme_key=self._theme_key, role="text", size=18))
            self.btn_status.setIcon(_make_swatch_icon("#94A3B8", 10, self._is_dark))
