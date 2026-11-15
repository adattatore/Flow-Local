"""
ui/notes_tree.py
Albero gerarchico stile Samsung Notes per la sidebar di FlowNotes:
- Cartelle e sottocartelle annidabili con indentazione 19px e linee guida verticali
- Icone cartella vettoriali colorate + badge numerico ricorsivo a destra
- Foglie distinte per tipo (📄 Nota, 🎙️ Sbobina con audio, ✅ Studiata)
- Azioni rapide su hover/tasto destro (+ Sottocartella, Colore, Rinomina, Elimina)
- Drag & Drop interno persistente su SQLite
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QModelIndex, QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)


FOLDER_PALETTE: List[tuple[str, str]] = [
    ("Indigo", "#6366F1"),
    ("Cielo", "#0EA5E9"),
    ("Smeraldo", "#10B981"),
    ("Ambra", "#F59E0B"),
    ("Rosa", "#EC4899"),
    ("Viola", "#8B5CF6"),
    ("Teal", "#14B8A6"),
    ("Corallo", "#F43F5E"),
    ("Lime", "#84CC16"),
    ("Ardesia", "#64748B"),
]

ROLE_NODE_DATA = Qt.ItemDataRole.UserRole
ROLE_COUNT = Qt.ItemDataRole.UserRole + 1
ROLE_FOLDER_COLOR = Qt.ItemDataRole.UserRole + 2
ROLE_IS_FOLDER = Qt.ItemDataRole.UserRole + 3
ROLE_LOCKED_FOLDER = Qt.ItemDataRole.UserRole + 4


def make_vector_folder_icon(color_hex: str, size: int = 18) -> QIcon:
    """Disegna un'icona cartella vettoriale nitida con QPainterPath."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    base = QColor(color_hex or "#6366F1")
    tab_col = base.darker(118)

    # Linguetta superiore
    tab = QPainterPath()
    tab.moveTo(1.5, 4.5)
    tab.lineTo(6.8, 4.5)
    tab.lineTo(8.3, 6.3)
    tab.lineTo(1.5, 6.3)
    tab.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(tab_col))
    p.drawPath(tab)

    # Corpo cartella
    body = QPainterPath()
    body.addRoundedRect(QRectF(1.5, 5.5, size - 3.0, size - 7.8), 2.5, 2.5)
    p.setBrush(QBrush(base))
    p.drawPath(body)
    p.end()
    return QIcon(pm)


class SamsungNotesItemDelegate(QStyledItemDelegate):
    """Delegate per disegnare badge contatori a destra e pulsante azioni su hover."""

    def __init__(self, tree: "SamsungNotesTreeWidget"):
        super().__init__(tree)
        self._tree = tree

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        is_folder = bool(index.data(ROLE_IS_FOLDER))
        count_val = index.data(ROLE_COUNT)
        is_hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        is_locked = bool(index.data(ROLE_LOCKED_FOLDER))

        # Riserva spazio fisso a destra: evita che il titolo venga coperto o si sposti su hover
        right_reserve = 0
        if is_folder and not is_locked:
            right_reserve = 56
        elif count_val is not None:
            right_reserve = 32

        # Disegna background/selezione/icona/testo standard con spazio riservato
        saved_rect = opt.rect
        if right_reserve > 0:
            opt.rect = saved_rect.adjusted(0, 0, -right_reserve, 0)
            # Disegna prima l'highlight su tutta la riga
            style = opt.widget.style() if opt.widget else None
            if style is not None:
                bg_opt = QStyleOptionViewItem(option)
                bg_opt.text = ""
                bg_opt.icon = QIcon()
                style.drawControl(QStyle.ControlElement.CE_ItemViewItem, bg_opt, painter, opt.widget)
                opt.backgroundBrush = QBrush(Qt.BrushStyle.NoBrush)
                opt.state &= ~QStyle.StateFlag.State_Selected
                opt.state &= ~QStyle.StateFlag.State_MouseOver

        painter.save()
        if right_reserve > 0:
            painter.setClipRect(opt.rect)
        super().paint(painter, opt, index)
        painter.restore()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self._tree._palette
        row_rect = saved_rect
        cur_right = row_rect.right() - 8

        # Numero note discreto a destra (solo testo, nessuno sfondo)
        if count_val is not None:
            txt = str(count_val)
            bf = QFont("Segoe UI", 8)
            bf.setWeight(QFont.Weight.DemiBold)
            painter.setFont(bf)
            w = max(20, painter.fontMetrics().horizontalAdvance(txt) + 4)
            count_rect = QRectF(cur_right - w, row_rect.top(), w, row_rect.height())
            is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
            num_col = QColor(pal.get("title", "#F8FAFC") if is_selected else pal.get("subtitle", "#94A3B8"))
            painter.setPen(QPen(num_col))
            painter.drawText(count_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
            cur_right -= int(w) + 6

        # Pulsante rapido ⋯ su hover per cartelle modificabili, nello slot fisso a sinistra del numero
        if is_folder and not is_locked and is_hovered:
            btn_rect = QRectF(cur_right - 20, row_rect.top() + (row_rect.height() - 18) / 2.0, 20.0, 18.0)
            self._tree._hovered_action_rect = (btn_rect, index)
            btn_bg = QColor(pal.get("border", "#3F3F46"))
            btn_bg.setAlpha(130)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(btn_bg))
            painter.drawRoundedRect(btn_rect, 4.0, 4.0)
            painter.setPen(QPen(QColor(pal.get("title", "#F8FAFC"))))
            painter.drawText(btn_rect, Qt.AlignmentFlag.AlignCenter, "⋯")

        painter.restore()


class SamsungNotesTreeWidget(QTreeWidget):
    """QTreeWidget stile Samsung Notes con guide gerarchiche, drag & drop e azioni cartella."""

    noteMovedToFolder = pyqtSignal(int, int)       # note_id, target_folder_id
    folderMovedToParent = pyqtSignal(int, object)  # folder_id, new_parent_id (int | None)
    createSubfolderRequested = pyqtSignal(int)     # parent_folder_id
    renameFolderRequested = pyqtSignal(int)        # folder_id
    changeFolderColorRequested = pyqtSignal(int, str)  # folder_id, color_hex
    deleteFolderRequested = pyqtSignal(int)        # folder_id
    toggleNoteStudiedRequested = pyqtSignal(int)   # note_id

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._palette: Dict[str, str] = {
            "border": "#2F2F37",
            "subtitle": "#94A3B8",
            "title": "#F8FAFC",
            "badge_bg": "#27272A",
            "badge_text": "#CBD5E1",
        }
        self._hovered_action_rect: Optional[tuple[QRectF, QModelIndex]] = None

        self.setObjectName("NKTree")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(19)
        self.setAnimated(True)
        self.setMouseTracking(True)
        self.setRootIsDecorated(True)
        self.header().setStretchLastSection(True)
        self.setItemDelegate(SamsungNotesItemDelegate(self))

        # Drag & Drop interno
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

        # Menu contestuale tasto destro
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_theme_palette(self, palette: Dict[str, str]):
        self._palette = dict(palette or {})
        self.viewport().update()

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex):
        """Disegna linee guida verticali di gerarchia e freccetta pulita ▸ / ▾."""
        if not index.isValid():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        guide_col = QColor(self._palette.get("border", "#3F3F46"))
        guide_col.setAlpha(95)
        painter.setPen(QPen(guide_col, 1.0))

        # Calcola profondità dell'item
        depth = 0
        p_idx = index.parent()
        while p_idx.isValid():
            depth += 1
            p_idx = p_idx.parent()

        ind = self.indentation()
        for d in range(depth):
            gx = rect.left() + d * ind + ind // 2
            painter.drawLine(gx, rect.top(), gx, rect.bottom())

        # Freccetta ▸ / ▾ per nodi con figli o cartelle
        item = self.itemFromIndex(index)
        is_folder = bool(index.data(ROLE_IS_FOLDER))
        has_children = item is not None and item.childCount() > 0
        if has_children or is_folder:
            arrow_x = rect.right() - ind + 3
            arrow_rect = QRectF(arrow_x, rect.top(), ind - 4, rect.height())
            arrow_col = QColor(self._palette.get("subtitle", "#94A3B8"))
            if not has_children:
                arrow_col.setAlpha(70)
            painter.setPen(QPen(arrow_col))
            af = QFont("Segoe UI", 8)
            painter.setFont(af)
            glyph = "▾" if self.isExpanded(index) else "▸"
            painter.drawText(arrow_rect, Qt.AlignmentFlag.AlignCenter, glyph)

        painter.restore()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._hovered_action_rect is not None:
                btn_rect, idx = self._hovered_action_rect
                if btn_rect.contains(event.position()):
                    item = self.itemFromIndex(idx)
                    if item is not None:
                        self._open_item_menu(item, self.viewport().mapToGlobal(event.pos()))
                        return
            item = self.itemAt(event.pos())
            if item is not None and bool(item.data(0, ROLE_IS_FOLDER)):
                idx = self.indexAt(event.pos())
                v_rect = self.visualRect(idx)
                # Se l'utente clicca sul nome/icona (dentro visualRect, non sulla freccetta dei rami),
                # alterna espansione/chiusura della cartella in aggiunta alla selezione
                super().mousePressEvent(event)
                if v_rect.contains(event.pos()):
                    item.setExpanded(not item.isExpanded())
                return
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        self._hovered_action_rect = None
        self.viewport().update()
        super().leaveEvent(event)

    def _on_context_menu(self, pos: QPoint):
        item = self.itemAt(pos)
        if item is not None:
            self._open_item_menu(item, self.viewport().mapToGlobal(pos))

    def _menu_stylesheet(self) -> str:
        pal = self._palette
        bg = pal.get("combo_popup_bg") or pal.get("dialog_bg") or pal.get("card_bg") or "#FFFFFF"
        text = pal.get("text") or "#1E293B"
        border = pal.get("card_border") or pal.get("divider") or pal.get("border") or "#CBD5E1"
        hover_bg = pal.get("nav_hover") or pal.get("badge_bg") or "#E2E8F0"
        hover_text = pal.get("title") or text
        return (
            f"QMenu {{ background-color: {bg}; color: {text}; border: 1px solid {border}; "
            f"border-radius: 8px; padding: 4px; }} "
            f"QMenu::item {{ padding: 6px 14px; border-radius: 5px; color: {text}; }} "
            f"QMenu::item:selected {{ background-color: {hover_bg}; color: {hover_text}; }} "
            f"QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}"
        )

    def _open_item_menu(self, item: QTreeWidgetItem, global_pos: QPoint):
        data = item.data(0, ROLE_NODE_DATA)
        if not data:
            return
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        if data[0] == "folder":
            folder_id = int(data[1])
            is_locked = bool(item.data(0, ROLE_LOCKED_FOLDER))

            act_sub = menu.addAction("➕ Sottocartella")
            act_sub.triggered.connect(lambda: self.createSubfolderRequested.emit(folder_id))

            color_menu = menu.addMenu("🎨 Colore Cartella")
            color_menu.setStyleSheet(self._menu_stylesheet())
            for c_name, c_hex in FOLDER_PALETTE:
                act_c = color_menu.addAction(make_vector_folder_icon(c_hex, 14), c_name)
                act_c.triggered.connect(
                    lambda _chk=False, fid=folder_id, col=c_hex: self.changeFolderColorRequested.emit(fid, col)
                )

            if not is_locked:
                menu.addSeparator()
                act_ren = menu.addAction("✎ Rinomina")
                act_ren.triggered.connect(lambda: self.renameFolderRequested.emit(folder_id))
                act_del = menu.addAction("🗑️ Elimina")
                act_del.triggered.connect(lambda: self.deleteFolderRequested.emit(folder_id))
            menu.exec(global_pos)
        elif data[0] == "note":
            note_id = int(data[1])
            act_st = menu.addAction("✅ Segna / Rimuovi Studiata")
            act_st.triggered.connect(lambda: self.toggleNoteStudiedRequested.emit(note_id))
            menu.exec(global_pos)

    def dropEvent(self, event):
        """Gestisce Drag & Drop di note e sottocartelle mantenendo integrità su SQLite."""
        dragged = self.currentItem()
        if dragged is None:
            event.ignore()
            return
        drag_data = dragged.data(0, ROLE_NODE_DATA)
        if not drag_data:
            event.ignore()
            return

        target_item = self.itemAt(event.position().toPoint())
        target_data = target_item.data(0, ROLE_NODE_DATA) if target_item is not None else None

        # Non permettere di spostare dentro 'Tutte le note' o 'Recenti'
        if drag_data[0] == "note":
            note_id = int(drag_data[1])
            target_folder_id: Optional[int] = None
            if target_data is not None:
                if target_data[0] == "folder":
                    target_folder_id = int(target_data[1])
                elif target_data[0] == "note" and target_item.parent() is not None:
                    p_data = target_item.parent().data(0, ROLE_NODE_DATA)
                    if p_data and p_data[0] == "folder":
                        target_folder_id = int(p_data[1])
            if target_folder_id is not None:
                event.accept()
                self.noteMovedToFolder.emit(note_id, target_folder_id)
                return
            event.ignore()
            return

        if drag_data[0] == "folder":
            folder_id = int(drag_data[1])
            if bool(dragged.data(0, ROLE_LOCKED_FOLDER)):
                event.ignore()
                return
            new_parent_id: Optional[int] = None
            if target_data is not None:
                if target_data[0] == "folder":
                    new_parent_id = int(target_data[1])
                elif target_data[0] == "note" and target_item.parent() is not None:
                    p_data = target_item.parent().data(0, ROLE_NODE_DATA)
                    if p_data and p_data[0] == "folder":
                        new_parent_id = int(p_data[1])
                else:
                    event.ignore()
                    return
            if new_parent_id == folder_id:
                event.ignore()
                return
            event.accept()
            self.folderMovedToParent.emit(folder_id, new_parent_id)
            return

        event.ignore()
