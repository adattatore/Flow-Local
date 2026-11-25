"""
Editor Scientifico WYSIWYG a Blocchi Reali in stile Notion per FlowLocal (Quaderno & Sbobine).
Include:
- ScientificNoteEditor(QTextEdit) WYSIWYG con supporto temi Dark (Obsidian) e Light
- Vere tabelle interattive QTextTable (navigabili con Tab, auto-riga su ultima cella)
- Blocchi Quote con barra verticale sinistra continua (3px) disegnata in paintEvent
- Blocchi To-Do List interattivi (☐ / ☑) cliccabili con strikeout e auto-continuazione su Invio
- Blocchi Code / BibTeX in QTextFrame a larghezza piena con card arrotondata (8px)
- Blocco Upload or embed a file (/file, /immagine) + incolla immagini Ctrl+V / Drag & Drop
- Motore offline matplotlib.mathtext + Renderer Matrici nativo QPainter ad alta risoluzione
- SlashCommandPopup (/) navigabile da tastiera posizionato sotto il cursore
"""

import base64
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
from matplotlib.mathtext import math_to_image

from PyQt6.QtCore import (
    Qt, QBuffer, QEvent, QIODevice, QMimeData, QObject, QPoint, QPointF, QRectF,
    QRegularExpression, QThread, QTimer, QUrl, pyqtSignal
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QIcon,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextFrame,
    QTextFrameFormat,
    QTextImageFormat,
    QTextLength,
    QTextTable,
    QTextTableFormat,
    QAction,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.math_eval import evaluate_inline_equals, parse_latex_matrix
from core.clipboard_tracker import dictation_tracker

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
VISION_OCR_MODEL = "qwen3.5:4b"


def _strip_think_blocks_safe(text: str) -> str:
    """Rimuove eventuali blocchi <think>...</think> dall'output LLM."""
    if not text:
        return ""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


from core.paths import get_user_data_dir, is_frozen

BASE_DATA_DIR = str(get_user_data_dir())
NOTE_MEDIA_DIR = os.path.join(BASE_DATA_DIR, "note_media")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_INLINE_IMAGE_WIDTH = 520

# Custom QTextFormat properties per blocchi WYSIWYG Notion
PROP_BLOCK_KIND = QTextFormat.Property.UserProperty + 1   # "quote" | "todo" | "file" | "h1" | "h2" | "h3" | "h4" | "ai_note" | "callout"
PROP_FILE_PATH = QTextFormat.Property.UserProperty + 2    # path locale per blocco "file"
PROP_FRAME_KIND = QTextFormat.Property.UserProperty + 3   # "python" | "bibtex" | "code"
PROP_LATEX_SOURCE = QTextFormat.Property.UserProperty + 5 # sorgente LaTeX su QTextImageFormat renderizzato
PROP_AI_NOTE_TITLE = QTextFormat.Property.UserProperty + 6
PROP_AI_NOTE_BODY = QTextFormat.Property.UserProperty + 7
PROP_AI_NOTE_EXPANDED = QTextFormat.Property.UserProperty + 8
PROP_OCR_PLACEHOLDER_ID = QTextFormat.Property.UserProperty + 9
PROP_CODE_ID = QTextFormat.Property.UserProperty + 10
PROP_TODO_CHECKED = QTextFormat.Property.UserProperty + 11  # bool: stato spunta blocco To-Do
PROP_TODO_INDENT = QTextFormat.Property.UserProperty + 12   # int: livello indentazione To-Do (0..4)
PROP_BULLET_INDENT = QTextFormat.Property.UserProperty + 13  # int: livello indentazione elenco (0..4)
PROP_INLINE_HIGHLIGHT = QTextFormat.Property.UserProperty + 14  # str: hex colore evidenziazione
PROP_INLINE_COLOR = QTextFormat.Property.UserProperty + 15      # str: hex colore testo
PROP_INLINE_BOLD = QTextFormat.Property.UserProperty + 16       # bool: grassetto WYSIWYG
PROP_INLINE_ITALIC = QTextFormat.Property.UserProperty + 17     # bool: corsivo WYSIWYG
PROP_INLINE_UNDERLINE = QTextFormat.Property.UserProperty + 18  # bool: sottolineato WYSIWYG
PROP_INLINE_STRIKE = QTextFormat.Property.UserProperty + 19     # bool: barrato WYSIWYG
PROP_INLINE_SCRIPT = QTextFormat.Property.UserProperty + 20     # str: "" | "sub" | "sup"
PROP_INLINE_FONT_SIZE = QTextFormat.Property.UserProperty + 21  # int: 1..100 pt (0 = default)

WORD_FONT_SIZES: Tuple[int, ...] = (8, 9, 10, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 36, 48, 72, 96)

HIGHLIGHT_COLORS: List[Tuple[str, str]] = [
    ("Giallo", "#FDE047"),
    ("Verde", "#4ADE80"),
    ("Ciano", "#38BDF8"),
    ("Rosa", "#F472B6"),
    ("Arancio", "#FB923C"),
]
TEXT_COLORS: List[Tuple[str, str]] = [
    ("Rosso", "#EF4444"),
    ("Blu", "#3B82F6"),
    ("Verde", "#10B981"),
    ("Ambra", "#F59E0B"),
    ("Viola", "#A855F7"),
    ("Ciano", "#06B6D4"),
]


def _highlight_brush_for_hex(hex_color: str, dark_mode: bool) -> QBrush:
    """Restituisce un QBrush traslucido leggibile per l'evidenziatore inline."""
    c = QColor((hex_color or "").strip())
    if not c.isValid():
        return QBrush(Qt.BrushStyle.NoBrush)
    c.setAlpha(78 if dark_mode else 135)
    return QBrush(c)


PLACEHOLDER_TEXTS: Dict[str, str] = {
    "h1": "Titolo 1",
    "h2": "Titolo 2",
    "h3": "Titolo 3",
    "todo": "To-do",
    "bullet": "Voce elenco...",
    "quote": "Citazione...",
    "callout": "Nota in evidenza...",
}


class BlockUserData(QTextBlockUserData):
    """Metadati persistenti per blocco (placeholder ghost-text senza sporcare QTextDocument)."""

    def __init__(self, placeholder_kind: str = ""):
        super().__init__()
        self.placeholder_kind: str = placeholder_kind


def strip_markdown_syntax(text: str) -> str:
    """Rimuove tag Markdown (#, ##, **, ==, ~~, <u>, <sub>, <sup>, <mark>, <span>, $$, ```, :::ai-note) per titoli e anteprime pulite nella sidebar."""
    if not text:
        return ""
    s = re.sub(r":::ai-note[^\n]*\n?", "", text)
    s = s.replace(":::", "")
    s = re.sub(r"```[a-zA-Z]*", "", s)
    s = re.sub(r"</?mark(?:\s+data-color=\"#[0-9A-Fa-f]{6}\")?>", "", s)
    s = re.sub(r"<span\s+style=\"color:\s*#[0-9A-Fa-f]{6}\s*;?\">|</span>", "", s)
    s = re.sub(r"<span\s+style=\"font-size:\s*\d{1,3}pt\s*;?\">", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(?:u|sub|sup)>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"~~([^~\n]+?)~~", r"\1", s)
    s = re.sub(r"==([^=\n]+?)==", r"\1", s)
    s = s.replace("$$", "").replace("**", "")
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^>\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^[-*]\s+\[[ xX]\]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Cache (formula, is_display, dark_mode) -> (base64_png, width_px, height_px)
_LATEX_PNG_CACHE: Dict[Tuple[str, bool, bool], Tuple[str, int, int]] = {}


def ensure_note_media_dir() -> str:
    os.makedirs(NOTE_MEDIA_DIR, exist_ok=True)
    return NOTE_MEDIA_DIR


def _qimage_to_png_b64(img: QImage) -> str:
    ba = QBuffer()
    ba.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(ba, "PNG")
    return base64.b64encode(bytes(ba.data())).decode("ascii")


def _normalize_latex_for_mathtext(expr: str) -> str:
    """Normalizza costrutti LaTeX comuni per renderli 100% compatibili con matplotlib.mathtext."""
    s = (expr or "").strip()
    if s.startswith("$$") and s.endswith("$$") and len(s) > 4:
        s = s[2:-2].strip()
    elif s.startswith("$") and s.endswith("$") and len(s) > 2:
        s = s[1:-1].strip()
    if not s:
        return s
    # Apici/pedici Unicode -> sintassi LaTeX esplicita
    sup_map = str.maketrans({
        "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4",
        "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
        "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
        "₅": "_5", "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9",
    })
    s = s.translate(sup_map)
    s = re.sub(r"\\(displaystyle|textstyle|scriptstyle|limits|nolimits)(?![a-zA-Z])", "", s)
    s = s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = re.sub(r"\\text\s*\{", r"\\mathrm{", s)
    s = re.sub(r"\\textbf\s*\{", r"\\mathbf{", s)
    # Rimuovi spazi attorno a ^, _, { e } (tipici dell'output tokenizzato di Qwen Vision)
    s = re.sub(r"\s*\^\s*", "^", s)
    s = re.sub(r"\s*_\s*", "_", s)
    s = re.sub(r"\{\s+", "{", s)
    s = re.sub(r"\s+\}", "}", s)
    s = re.sub(r"(\\[a-zA-Z]+)\s+\{", r"\1{", s)
    s = re.sub(r"\}\s+\{", "}{", s)
    return s.strip()


def _mathtext_to_transparent_qimage(expr: str, dpi: int, ink_hex: str) -> Optional[QImage]:
    """
    Renderizza un'espressione LaTeX con matplotlib.mathtext su sfondo 100% trasparente
    nel colore `ink_hex`, eliminando il rettangolo bianco opaco di default.
    """
    import numpy as np

    norm_expr = _normalize_latex_for_mathtext(expr)
    buf = io.BytesIO()
    math_to_image(f"${norm_expr}$", buf, dpi=dpi, format="png", color="#000000")
    src = QImage.fromData(buf.getvalue(), "PNG")
    if src.isNull():
        return None
    src = src.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = src.width(), src.height()
    ptr = src.bits()
    ptr.setsize(src.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, src.bytesPerLine() // 4, 4))[:, :w, :].copy()

    # Nero su bianco: luminanza 255 -> trasparente (alpha 0), luminanza 0 -> opaco (alpha 255)
    gray = arr[:, :, 0].astype(np.float32)
    alpha = np.clip(255.0 - gray, 0.0, 255.0).astype(np.uint8)

    ink = QColor(ink_hex)
    arr[:, :, 0] = ink.red()
    arr[:, :, 1] = ink.green()
    arr[:, :, 2] = ink.blue()
    arr[:, :, 3] = alpha

    out = QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return out.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _render_cell_qimage(cell_tex: str, dpi: int = 160, ink_hex: str = "#0F172A") -> QImage:
    """Renderizza una singola cella di matrice tramite matplotlib.mathtext (o fallback QPainter)."""
    clean = (cell_tex or "").strip() or "0"
    try:
        qimg = _mathtext_to_transparent_qimage(clean, dpi=dpi, ink_hex=ink_hex)
        if qimg is not None and not qimg.isNull():
            return qimg
    except Exception:
        pass

    font = QFont("Cambria Math", 14)
    font.setItalic(True)
    probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(probe)
    p.setFont(font)
    rect = p.boundingRect(0, 0, 400, 100, Qt.AlignmentFlag.AlignCenter, clean)
    p.end()
    w = max(24, rect.width() + 14)
    h = max(26, rect.height() + 8)
    out = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p2 = QPainter(out)
    p2.setRenderHint(QPainter.RenderHint.Antialiasing)
    p2.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p2.setFont(font)
    p2.setPen(QColor(ink_hex))
    p2.drawText(out.rect(), Qt.AlignmentFlag.AlignCenter, clean)
    p2.end()
    return out


def _editor_ink_hex(dark_mode: bool = False, reading_mode: bool = False) -> str:
    if reading_mode:
        return "#EFE2CE" if dark_mode else "#2A1F14"
    return "#F1F5F9" if dark_mode else "#0F172A"


def _render_matrix_png_b64(expr: str, dark_mode: bool = False, reading_mode: bool = False) -> Optional[Tuple[str, int, int]]:
    """
    Renderer nativo ad alta risoluzione con QPainter per matrici \\begin{bmatrix} / \\begin{pmatrix} / \\begin{vmatrix}.
    Renderizza ogni cella tramite matplotlib.mathtext e disegna parentesi vettoriali spesse.
    """
    m = re.search(r"\\begin\{([bpv])matrix\}(.*?)\\end\{\1matrix\}", expr, re.DOTALL)
    if not m:
        return None

    ink_hex = _editor_ink_hex(dark_mode, reading_mode)
    kind = m.group(1)
    body = m.group(2).strip()
    prefix = expr[: m.start()].strip()
    suffix = expr[m.end() :].strip()

    raw_rows = [r.strip() for r in body.split(r"\\") if r.strip()]
    if not raw_rows:
        return None

    grid_tex = [[c.strip() for c in r.split("&")] for r in raw_rows]
    ncols = max(len(r) for r in grid_tex)
    for r in grid_tex:
        while len(r) < ncols:
            r.append("")

    cell_imgs: List[List[QImage]] = [
        [_render_cell_qimage(c, dpi=165, ink_hex=ink_hex) for c in row] for row in grid_tex
    ]

    col_widths = [
        max(cell_imgs[r][c].width() for r in range(len(cell_imgs)))
        for c in range(ncols)
    ]
    row_heights = [
        max(cell_imgs[r][c].height() for c in range(ncols))
        for r in range(len(cell_imgs))
    ]

    cell_pad_x = 18
    cell_pad_y = 12
    bracket_w = 14
    inner_w = sum(col_widths) + cell_pad_x * max(0, ncols - 1) + 16
    inner_h = sum(row_heights) + cell_pad_y * max(0, len(row_heights) - 1) + 14

    prefix_img = _render_cell_qimage(prefix, dpi=175, ink_hex=ink_hex) if prefix else None
    suffix_img = _render_cell_qimage(suffix, dpi=175, ink_hex=ink_hex) if suffix else None

    prefix_w = (prefix_img.width() + 14) if prefix_img else 0
    suffix_w = (suffix_img.width() + 14) if suffix_img else 0
    total_w = prefix_w + bracket_w + inner_w + bracket_w + suffix_w + 16
    total_h = max(inner_h + 12, (prefix_img.height() + 12) if prefix_img else 0)

    canvas = QImage(total_w, total_h, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    ink = QColor(ink_hex)
    mat_top = (total_h - inner_h) / 2.0
    mat_bot = mat_top + inner_h
    mat_left = 8.0 + prefix_w
    mat_right = mat_left + bracket_w + inner_w + bracket_w

    if prefix_img is not None:
        py = int((total_h - prefix_img.height()) / 2)
        painter.drawImage(8, py, prefix_img)

    pen = QPen(ink, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    arm = 9.0
    if kind == "b":
        lp = QPainterPath()
        lp.moveTo(mat_left + arm, mat_top)
        lp.lineTo(mat_left, mat_top)
        lp.lineTo(mat_left, mat_bot)
        lp.lineTo(mat_left + arm, mat_bot)
        painter.drawPath(lp)

        rp = QPainterPath()
        rp.moveTo(mat_right - arm, mat_top)
        rp.lineTo(mat_right, mat_top)
        rp.lineTo(mat_right, mat_bot)
        rp.lineTo(mat_right - arm, mat_bot)
        painter.drawPath(rp)
    elif kind == "v":
        painter.drawLine(QPointF(mat_left + 4, mat_top), QPointF(mat_left + 4, mat_bot))
        painter.drawLine(QPointF(mat_right - 4, mat_top), QPointF(mat_right - 4, mat_bot))
    else:
        lp = QPainterPath()
        lp.moveTo(mat_left + arm, mat_top)
        lp.quadTo(mat_left - 4, (mat_top + mat_bot) / 2.0, mat_left + arm, mat_bot)
        painter.drawPath(lp)

        rp = QPainterPath()
        rp.moveTo(mat_right - arm, mat_top)
        rp.quadTo(mat_right + 4, (mat_top + mat_bot) / 2.0, mat_right - arm, mat_bot)
        painter.drawPath(rp)

    cur_y = mat_top + 7.0
    for r_idx, row in enumerate(cell_imgs):
        rh = row_heights[r_idx]
        cur_x = mat_left + bracket_w + 8.0
        for c_idx, c_img in enumerate(row):
            cw = col_widths[c_idx]
            dx = int(cur_x + (cw - c_img.width()) / 2.0)
            dy = int(cur_y + (rh - c_img.height()) / 2.0)
            painter.drawImage(dx, dy, c_img)
            cur_x += cw + cell_pad_x
        cur_y += rh + cell_pad_y

    if suffix_img is not None:
        sy = int((total_h - suffix_img.height()) / 2)
        painter.drawImage(int(mat_right + 10), sy, suffix_img)

    painter.end()

    scale_factor = 0.62
    disp_w = max(40, int(total_w * scale_factor))
    disp_h = max(30, int(total_h * scale_factor))
    return _qimage_to_png_b64(canvas), disp_w, disp_h


def render_latex_to_html(expr: str, is_display: bool = False, dark_mode: bool = False, reading_mode: bool = False) -> str:
    """
    Renderizza un'espressione LaTeX su sfondo trasparente tramite matplotlib.mathtext
    e renderer QPainter per matrici bmatrix/pmatrix.
    """
    clean = (expr or "").strip()
    if not clean:
        return ""

    ink_hex = _editor_ink_hex(dark_mode, reading_mode)
    cache_key = (clean, is_display, dark_mode, reading_mode)
    if cache_key in _LATEX_PNG_CACHE:
        b64, w_px, h_px = _LATEX_PNG_CACHE[cache_key]
        return _format_math_img_tag(b64, w_px, h_px, is_display, clean)

    if is_display and re.search(r"\\begin\{[bp]matrix\}", clean):
        mat_res = _render_matrix_png_b64(clean, dark_mode=dark_mode, reading_mode=reading_mode)
        if mat_res is not None:
            b64, w_px, h_px = mat_res
            _LATEX_PNG_CACHE[cache_key] = (b64, w_px, h_px)
            return _format_math_img_tag(b64, w_px, h_px, True, clean)

    try:
        dpi = 175 if is_display else 145
        qimg = _mathtext_to_transparent_qimage(clean, dpi=dpi, ink_hex=ink_hex)
        if qimg is not None and not qimg.isNull():
            scale_factor = 0.62 if is_display else 0.56
            w_px = max(12, int(qimg.width() * scale_factor))
            h_px = max(12, int(qimg.height() * scale_factor))
            b64 = _qimage_to_png_b64(qimg)
            _LATEX_PNG_CACHE[cache_key] = (b64, w_px, h_px)
            return _format_math_img_tag(b64, w_px, h_px, is_display, clean)
    except Exception:
        pass

    escaped = html.escape(clean)
    if dark_mode:
        if is_display:
            return (
                f'<div style="text-align: center; margin: 12px 0; padding: 10px 14px; '
                f'background-color: #202020; border: 1px solid #2F2F2F; border-radius: 8px; '
                f'font-family: \'Cambria Math\', \'Times New Roman\', serif; font-style: italic; '
                f'font-size: 13pt; color: #C4B5FD;">{escaped}</div>'
            )
        return (
            f'<span style="font-family: \'Cambria Math\', \'Times New Roman\', serif; '
            f'font-style: italic; color: #A5B4FC;">{escaped}</span>'
        )
    if is_display:
        return (
            f'<div style="text-align: center; margin: 12px 0; padding: 10px 14px; '
            f'background-color: #F7F6F3; border: 1px solid #E5E7EB; border-radius: 8px; '
            f'font-family: \'Cambria Math\', \'Times New Roman\', serif; font-style: italic; '
            f'font-size: 13pt; color: #312E81;">{escaped}</div>'
        )
    return (
        f'<span style="font-family: \'Cambria Math\', \'Times New Roman\', serif; '
        f'font-style: italic; color: #4338CA;">{escaped}</span>'
    )


def _format_math_img_tag(b64: str, w_px: int, h_px: int, is_display: bool, alt: str) -> str:
    safe_alt = html.escape(alt)
    img = (
        f'<img src="data:image/png;base64,{b64}" width="{w_px}" height="{h_px}" '
        f'alt="{safe_alt}" style="vertical-align: middle;" />'
    )
    if is_display:
        return f'<div style="text-align: center; margin: 12px 0;">{img}</div>'
    return img


_INLINE_MARKUP_RE = re.compile(
    r'<mark(?:\s+data-color="(#[0-9A-Fa-f]{6})")?>(.*?)</mark>'
    r"|==([^=\n]+?)=="
    r'|<span\s+style="color:\s*(#[0-9A-Fa-f]{6})\s*;?">(.*?)</span>'
    r"|<(u|sub|sup)>(.*?)</\6>"
    r'|<span\s+style="font-size:\s*(\d{1,3})pt\s*;?">(.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)


def _hex_rgba_css(hex_col: str, alpha: float = 0.35) -> str:
    c = QColor((hex_col or "").strip())
    if not c.isValid():
        return f"rgba(253, 224, 71, {alpha})"
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _render_styled_inline_fragment(raw_str: str) -> str:
    """Converte in sicurezza <mark>, ==...==, <span style="color:#...">, <span style="font-size:...">, <u>, <sub>, <sup>."""
    parts: List[str] = []
    pos = 0
    for m in _INLINE_MARKUP_RE.finditer(raw_str):
        if m.start() > pos:
            parts.append(html.escape(raw_str[pos:m.start()]))
        if m.group(2) is not None:
            hl_hex = (m.group(1) or "#FDE047").upper()
            inner_html = _render_styled_inline_fragment(m.group(2))
            rgba = _hex_rgba_css(hl_hex, 0.35)
            parts.append(
                f'<mark style="background-color: {rgba}; color: inherit; padding: 0 2px; border-radius: 3px;">'
                f"{inner_html}</mark>"
            )
        elif m.group(3) is not None:
            inner_html = _render_styled_inline_fragment(m.group(3))
            rgba = _hex_rgba_css("#FDE047", 0.35)
            parts.append(
                f'<mark style="background-color: {rgba}; color: inherit; padding: 0 2px; border-radius: 3px;">'
                f"{inner_html}</mark>"
            )
        elif m.group(4) is not None and m.group(5) is not None:
            col_hex = m.group(4).upper()
            inner_html = _render_styled_inline_fragment(m.group(5))
            parts.append(f'<span style="color: {col_hex};">{inner_html}</span>')
        elif m.group(6) is not None and m.group(7) is not None:
            tag = m.group(6).lower()
            inner_html = _render_styled_inline_fragment(m.group(7))
            parts.append(f"<{tag}>{inner_html}</{tag}>")
        elif m.group(8) is not None and m.group(9) is not None:
            sz = max(1, min(100, int(m.group(8))))
            inner_html = _render_styled_inline_fragment(m.group(9))
            parts.append(f'<span style="font-size: {sz}pt;">{inner_html}</span>')
        pos = m.end()
    if pos < len(raw_str):
        parts.append(html.escape(raw_str[pos:]))
    return "".join(parts)


def _render_inline_markdown_html(
    text: str,
    dark_mode: bool = False,
    reading_mode: bool = False,
    font_scale_pct: int = 100,
) -> str:
    tokens = re.split(r"(\$\$.*?\$\$|\$[^$\n]+?\$|`[^`\n]+?`)", text)
    out: List[str] = []
    scale = max(0.8, min(1.5, float(font_scale_pct or 100) / 100.0))
    code_pt = round(10.0 * scale, 1)
    if reading_mode:
        code_bg = "#241D17" if dark_mode else "#F3E8D2"
        code_fg = "#E8D9C5" if dark_mode else "#2A1F14"
        code_bd = "#3A2F26" if dark_mode else "#E2D2B5"
        bold_fg = "#F8EFE2" if dark_mode else "#2A1F14"
        em_fg = "#D6C4AD" if dark_mode else "#4E3D2C"
    else:
        code_bg = "#202020" if dark_mode else "#F7F6F3"
        code_fg = "#E2E8F0" if dark_mode else "#0F172A"
        code_bd = "#2F2F2F" if dark_mode else "#E5E7EB"
        bold_fg = "#FFFFFF" if dark_mode else "#0F172A"
        em_fg = "#CBD5E1" if dark_mode else "#334155"

    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("$$") and tok.endswith("$$") and len(tok) > 4:
            out.append(render_latex_to_html(tok[2:-2], is_display=True, dark_mode=dark_mode, reading_mode=reading_mode))
        elif tok.startswith("$") and tok.endswith("$") and len(tok) > 2:
            out.append(render_latex_to_html(tok[1:-1], is_display=False, dark_mode=dark_mode, reading_mode=reading_mode))
        elif tok.startswith("`") and tok.endswith("`") and len(tok) >= 2:
            out.append(
                f'<code style="background-color: {code_bg}; color: {code_fg}; '
                f'border: 1px solid {code_bd}; padding: 1px 5px; border-radius: 4px; '
                f'font-family: \'JetBrains Mono\', \'Cascadia Code\', Consolas, monospace; '
                f'font-size: {code_pt:g}pt;">{html.escape(tok[1:-1])}</code>'
            )
        else:
            escaped = _render_styled_inline_fragment(tok)
            escaped = re.sub(
                r"\*\*\*(.+?)\*\*\*",
                rf'<strong style="color: {bold_fg}; font-weight: 700;"><em style="color: {em_fg}; font-style: italic;">\1</em></strong>',
                escaped,
            )
            escaped = re.sub(
                r"\*\*(.+?)\*\*",
                rf'<strong style="color: {bold_fg}; font-weight: 700;">\1</strong>',
                escaped,
            )
            escaped = re.sub(
                r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
                rf'<em style="color: {em_fg}; font-style: italic;">\1</em>',
                escaped,
            )
            escaped = re.sub(
                r"~~([^~\n]+?)~~",
                r"<s>\1</s>",
                escaped,
            )
            out.append(escaped)
    return "".join(out)


def _resolve_image_uri_and_size(raw_path: str, base_dir: str) -> Tuple[str, Optional[int]]:
    clean_p = raw_path.strip()
    if os.path.isabs(clean_p):
        abs_p = clean_p
    else:
        abs_p = os.path.abspath(os.path.join(base_dir, clean_p))
    width_attr = None
    if os.path.exists(abs_p):
        qimg = QImage(abs_p)
        if not qimg.isNull() and qimg.width() > 0:
            width_attr = min(qimg.width(), MAX_INLINE_IMAGE_WIDTH)
    uri = QUrl.fromLocalFile(abs_p).toString()
    return uri, width_attr


def markdown_to_studio_html(
    md_text: str,
    base_dir: str = BASE_DATA_DIR,
    dark_mode: bool = False,
    reading_mode: bool = False,
    font_family_css: str = "'Segoe UI', sans-serif",
    font_scale_pct: int = 100,
    bg_page_override: str = "",
    fg_text_override: str = "",
) -> str:
    """Converte una nota Markdown + LaTeX in HTML impaginato stile Notion per la Vista Studio."""
    scale = max(0.8, min(1.5, font_scale_pct / 100.0))
    body_pt = round(11.0 * scale, 1)
    h1_pt = round(18.0 * scale, 1)
    h2_pt = round(14.5 * scale, 1)
    h3_pt = round(12.5 * scale, 1)
    code_pt = round(10.0 * scale, 1)
    if reading_mode:
        bg_page = bg_page_override or ("#18130F" if dark_mode else "#FBF5E6")
        fg_text = fg_text_override or ("#E8D9C5" if dark_mode else "#3E3022")
        fg_h1 = "#F8EFE2" if dark_mode else "#2A1F14"
        fg_h2 = "#EFE2CE" if dark_mode else "#35281B"
        fg_h3 = "#D6C4AD" if dark_mode else "#4E3D2C"
        fg_muted = "#A3917B" if dark_mode else "#7D6B57"
        card_bg = "#241D17" if dark_mode else "#F3E8D2"
        card_bd = "#3A2F26" if dark_mode else "#E2D2B5"
        tbl_hdr_bg = "#201914" if dark_mode else "#EFE2C9"
        quote_bar = "#D97706" if dark_mode else "#B45309"
    else:
        bg_page = bg_page_override or ("#0D111A" if dark_mode else "#FFFFFF")
        fg_text = fg_text_override or ("#E2E8F0" if dark_mode else "#1E293B")
        fg_h1 = "#FFFFFF" if dark_mode else "#0F172A"
        fg_h2 = "#F1F5F9" if dark_mode else "#1E293B"
        fg_h3 = "#CBD5E1" if dark_mode else "#334155"
        fg_muted = "#94A3B8" if dark_mode else "#64748B"
        card_bg = "#202020" if dark_mode else "#F7F6F3"
        card_bd = "#2F2F2F" if dark_mode else "#E5E7EB"
        tbl_hdr_bg = "#161B26" if dark_mode else "#F8FAFC"
        quote_bar = "#FFFFFF" if dark_mode else "#111827"

    lines = (md_text or "").replace("\ufffc", "").splitlines()
    blocks: List[str] = []
    i = 0
    in_ul = False
    in_ol = False

    def _close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            blocks.append("</ul>")
            in_ul = False
        if in_ol:
            blocks.append("</ol>")
            in_ol = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Blocco codice / BibTeX
        if stripped.startswith("```"):
            _close_lists()
            lang = stripped[3:].strip().lower()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_html = html.escape("\n".join(code_lines))
            lang_badge = (
                f'<div style="color: {fg_muted}; font-size: 8.5pt; font-weight: 600; '
                f'margin-bottom: 6px; text-transform: uppercase;">{html.escape(lang)}</div>'
                if lang
                else ""
            )
            blocks.append(
                f'<div style="background-color: {card_bg}; border: 1px solid {card_bd}; '
                f'padding: 14px 16px; margin: 12px 0; border-radius: 8px; '
                f'font-family: \'JetBrains Mono\', \'Cascadia Code\', Consolas, monospace; font-size: {code_pt}pt; color: {fg_text}; '
                f'white-space: pre-wrap;">{lang_badge}{code_html}</div>'
            )
            i += 1
            continue

        # Blocco matematico $$ ... $$
        if stripped.startswith("$$"):
            _close_lists()
            if stripped.endswith("$$") and len(stripped) > 4:
                expr = stripped[2:-2].strip()
                blocks.append(render_latex_to_html(expr, is_display=True, dark_mode=dark_mode, reading_mode=reading_mode))
                i += 1
                continue
            math_lines = [stripped[2:]]
            i += 1
            while i < len(lines):
                cur = lines[i].strip()
                if cur.endswith("$$"):
                    math_lines.append(cur[:-2])
                    break
                math_lines.append(lines[i])
                i += 1
            expr = "\n".join(math_lines).strip()
            blocks.append(render_latex_to_html(expr, is_display=True, dark_mode=dark_mode, reading_mode=reading_mode))
            i += 1
            continue

        # Immagini ![alt](path)
        img_m = re.match(r"^!\[(.*?)\]\((.+?)\)\s*$", stripped)
        if img_m:
            _close_lists()
            alt = html.escape(img_m.group(1) or "Screenshot")
            uri, w_attr = _resolve_image_uri_and_size(img_m.group(2), base_dir)
            w_str = f' width="{w_attr}"' if w_attr else ' width="480"'
            blocks.append(
                f'<div style="margin: 12px 0; text-align: left;">'
                f'<img src="{uri}"{w_str} alt="{alt}" />'
                f'<div style="color: {fg_muted}; font-size: 9pt; margin-top: 4px;">{alt}</div>'
                f'</div>'
            )
            i += 1
            continue

        # Tabelle Markdown
        if stripped.startswith("|") and "|" in stripped[1:]:
            _close_lists()
            rows: List[List[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                raw_row = lines[i].strip().strip("|")
                cells = [c.strip() for c in raw_row.split("|")]
                if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                t_html = [
                    f'<table style="border-collapse: collapse; margin: 12px 0; width: 100%; '
                    f'border: 1px solid {card_bd};">'
                ]
                for r_idx, row in enumerate(rows):
                    t_html.append("<tr>")
                    for cell in row:
                        cell_html = _render_inline_markdown_html(cell, dark_mode=dark_mode, reading_mode=reading_mode, font_scale_pct=font_scale_pct)
                        if r_idx == 0:
                            t_html.append(
                                f'<th style="background-color: {tbl_hdr_bg}; color: {fg_h1}; '
                                f'border: 1px solid {card_bd}; padding: 8px 12px; '
                                f'text-align: left; font-weight: 600;">{cell_html}</th>'
                            )
                        else:
                            t_html.append(
                                f'<td style="background-color: {bg_page}; color: {fg_text}; '
                                f'border: 1px solid {card_bd}; padding: 8px 12px;">{cell_html}</td>'
                            )
                    t_html.append("</tr>")
                t_html.append("</table>")
                blocks.append("".join(t_html))
            continue

        # Titoli
        if stripped.startswith("### "):
            _close_lists()
            blocks.append(
                f'<h3 style="color: {fg_h3}; font-size: {h3_pt}pt; font-weight: 600; '
                f'margin: 14px 0 4px 0;">{_render_inline_markdown_html(stripped[4:], dark_mode, reading_mode, font_scale_pct)}</h3>'
            )
        elif stripped.startswith("## "):
            _close_lists()
            blocks.append(
                f'<h2 style="color: {fg_h2}; font-size: {h2_pt}pt; font-weight: 700; '
                f'margin: 16px 0 6px 0; border-bottom: 1px solid {card_bd}; '
                f'padding-bottom: 4px;">{_render_inline_markdown_html(stripped[3:], dark_mode, reading_mode, font_scale_pct)}</h2>'
            )
        elif stripped.startswith("# "):
            _close_lists()
            blocks.append(
                f'<h1 style="color: {fg_h1}; font-size: {h1_pt}pt; font-weight: 700; '
                f'margin: 16px 0 8px 0;">{_render_inline_markdown_html(stripped[2:], dark_mode, reading_mode, font_scale_pct)}</h1>'
            )
        elif stripped.startswith("> "):
            _close_lists()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(_render_inline_markdown_html(lines[i].strip()[2:], dark_mode, reading_mode, font_scale_pct))
                i += 1
            blocks.append(
                f'<div style="border-left: 3px solid {quote_bar}; '
                f'padding: 4px 14px; margin: 10px 0; color: {fg_text};">'
                f'{"<br/>".join(quote_lines)}</div>'
            )
            continue
        elif re.match(r"^\s*[-*]\s+\[([ xX])\]\s*", line):
            _close_lists()
            m_todo = re.match(r"^(\s*)[-*]\s+\[([ xX])\]\s*(.*)$", line)
            indent_lvl = min(4, len(m_todo.group(1).replace("\t", "  ")) // 2)
            checked = (m_todo.group(2).lower() == "x")
            txt_html = _render_inline_markdown_html(m_todo.group(3), dark_mode, reading_mode, font_scale_pct)
            glyph = "☑" if checked else "☐"
            pad_left = 26 + indent_lvl * 22
            style = f"text-decoration: line-through; color: {fg_muted};" if checked else f"color: {fg_text};"
            blocks.append(
                f'<div style="margin: 4px 0; padding-left: {pad_left}px; text-indent: -20px; {style}">'
                f'<span style="margin-right: 6px;">{glyph}</span>{txt_html}</div>'
            )
        elif re.match(r"^[-*]\s+", stripped):
            if in_ol:
                blocks.append("</ol>")
                in_ol = False
            if not in_ul:
                blocks.append(f'<ul style="margin: 6px 0 6px 20px; color: {fg_text};">')
                in_ul = True
            item_txt = re.sub(r"^[-*]\s+", "", stripped)
            blocks.append(f'<li style="margin: 3px 0;">{_render_inline_markdown_html(item_txt, dark_mode, reading_mode, font_scale_pct)}</li>')
        elif re.match(r"^\d+\.\s+", stripped):
            if in_ul:
                blocks.append("</ul>")
                in_ul = False
            if not in_ol:
                blocks.append(f'<ol style="margin: 6px 0 6px 20px; color: {fg_text};">')
                in_ol = True
            item_txt = re.sub(r"^\d+\.\s+", "", stripped)
            blocks.append(f'<li style="margin: 3px 0;">{_render_inline_markdown_html(item_txt, dark_mode, reading_mode, font_scale_pct)}</li>')
        elif not stripped:
            _close_lists()
            blocks.append('<div style="height: 6px;"></div>')
        else:
            _close_lists()
            p_lh = "1.78" if reading_mode else "1.6"
            blocks.append(
                f'<p style="margin: 5px 0; line-height: {p_lh}; color: {fg_text};">'
                f'{_render_inline_markdown_html(line, dark_mode, reading_mode, font_scale_pct)}</p>'
            )

        i += 1

    _close_lists()
    body = "\n".join(blocks)
    body_lh = "1.75" if reading_mode else "1.55"
    body_pad = "10px 18px" if reading_mode else "0px"
    return (
        f'<html><body style="background-color: {bg_page}; color: {fg_text}; '
        f'font-family: {font_family_css}; font-size: {body_pt}pt; line-height: {body_lh}; padding: {body_pad};">'
        f"{body}</body></html>"
    )


class MarkdownLatexHighlighter(QSyntaxHighlighter):
    """
    Evidenziatore sintattico live per Markdown e LaTeX.
    NESSUNO sfondo colorato dietro ai caratteri.
    """

    def __init__(self, document: QTextDocument, dark_mode: bool = True):
        super().__init__(document)
        self._enabled = True
        self._dark_mode = dark_mode
        self._font_scale: float = 1.0
        self._rules: List[Tuple[QRegularExpression, QTextCharFormat]] = []
        self._rebuild_rules()

    def set_dark_mode(self, dark_mode: bool):
        if self._dark_mode == dark_mode:
            return
        self._dark_mode = dark_mode
        self._rebuild_rules()
        self.rehighlight()

    def set_font_scale(self, scale_pct: int):
        new_scale = max(0.8, min(1.5, scale_pct / 100.0))
        if abs(self._font_scale - new_scale) < 0.01:
            return
        self._font_scale = new_scale
        self._rebuild_rules()
        self.rehighlight()

    def _rebuild_rules(self):
        self._rules.clear()
        h1_fg = "#FFFFFF" if self._dark_mode else "#0F172A"
        h2_fg = "#F8FAFC" if self._dark_mode else "#1E293B"
        h3_fg = "#E2E8F0" if self._dark_mode else "#334155"
        bold_fg = "#FFFFFF" if self._dark_mode else "#0F172A"
        code_fg = "#E2E8F0" if self._dark_mode else "#334155"
        latex_fg = "#818CF8" if self._dark_mode else "#4F46E5"
        img_fg = "#34D399" if self._dark_mode else "#059669"

        h1_fmt = QTextCharFormat()
        h1_fmt.setForeground(QColor(h1_fg))
        h1_fmt.setFontWeight(QFont.Weight.Bold)
        h1_fmt.setFontPointSize(round(16 * self._font_scale, 1))
        self._rules.append((QRegularExpression(r"^#\s+.*$"), h1_fmt))

        h2_fmt = QTextCharFormat()
        h2_fmt.setForeground(QColor(h2_fg))
        h2_fmt.setFontWeight(QFont.Weight.DemiBold)
        h2_fmt.setFontPointSize(round(13 * self._font_scale, 1))
        self._rules.append((QRegularExpression(r"^##\s+.*$"), h2_fmt))

        h3_fmt = QTextCharFormat()
        h3_fmt.setForeground(QColor(h3_fg))
        h3_fmt.setFontWeight(QFont.Weight.DemiBold)
        h3_fmt.setFontPointSize(round(11.5 * self._font_scale, 1))
        self._rules.append((QRegularExpression(r"^###\s+.*$"), h3_fmt))

        bold_fmt = QTextCharFormat()
        bold_fmt.setForeground(QColor(bold_fg))
        bold_fmt.setFontWeight(QFont.Weight.Bold)
        self._rules.append((QRegularExpression(r"\*\*[^*\n]+\*\*"), bold_fmt))

        self._code_fmt = QTextCharFormat()
        self._code_fmt.setForeground(QColor(code_fg))
        self._code_fmt.setFontFamily("Consolas")
        self._code_fmt.setFontPointSize(round(10 * self._font_scale, 1))
        self._rules.append((QRegularExpression(r"`[^`\n]+`"), self._code_fmt))

        img_fmt = QTextCharFormat()
        img_fmt.setForeground(QColor(img_fg))
        self._rules.append((QRegularExpression(r"!\[[^\]]*\]\([^)]+\)"), img_fmt))

        self._latex_fmt = QTextCharFormat()
        self._latex_fmt.setForeground(QColor(latex_fg))
        self._latex_fmt.setFontFamily("Consolas")
        self._rules.append((QRegularExpression(r"\$\$[^$]+\$\$"), self._latex_fmt))
        self._rules.append((QRegularExpression(r"(?<!\$)\$[^$\n]+\$(?!\$)"), self._latex_fmt))

        wiki_fmt = QTextCharFormat()
        wiki_fmt.setForeground(QColor("#38BDF8" if self._dark_mode else "#0284C7"))
        wiki_fmt.setFontUnderline(True)
        self._rules.append((QRegularExpression(r"\[\[[^\]\n]+\]\]"), wiki_fmt))

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.rehighlight()

    def highlightBlock(self, text: str):
        if not self._enabled:
            return
        # Non sovrascrivere i blocchi speciali WYSIWYG (To-Do, File, Code Frame)
        bf = self.currentBlock().blockFormat()
        if bf.property(PROP_BLOCK_KIND) in ("todo", "file"):
            return
        cur_frame = QTextCursor(self.currentBlock()).currentFrame()
        if cur_frame is not None and cur_frame.frameFormat().hasProperty(PROP_FRAME_KIND):
            return

        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)

        self.setCurrentBlockState(0)
        prev_state = self.previousBlockState()
        stripped = text.strip()
        if prev_state == 1:
            self.setFormat(0, len(text), self._latex_fmt)
            if not stripped.endswith("$$"):
                self.setCurrentBlockState(1)
        elif stripped.startswith("$$") and not (stripped.endswith("$$") and len(stripped) > 4):
            self.setFormat(0, len(text), self._latex_fmt)
            self.setCurrentBlockState(1)

        # Preserva evidenziatore, colore e stili inline WYSIWYG sopra eventuali match di sintassi
        blk = self.currentBlock()
        b_pos = blk.position()
        fit = blk.begin()
        while not fit.atEnd():
            frag = fit.fragment()
            if frag.isValid():
                cf = frag.charFormat()
                hl_hex = str(cf.property(PROP_INLINE_HIGHLIGHT) or "").strip()
                col_hex = str(cf.property(PROP_INLINE_COLOR) or "").strip()
                is_b = bool(cf.property(PROP_INLINE_BOLD))
                is_i = bool(cf.property(PROP_INLINE_ITALIC))
                is_u = bool(cf.property(PROP_INLINE_UNDERLINE))
                is_s = bool(cf.property(PROP_INLINE_STRIKE))
                script = str(cf.property(PROP_INLINE_SCRIPT) or "").strip().lower()
                fsz = int(cf.property(PROP_INLINE_FONT_SIZE) or 0)
                if hl_hex or col_hex or is_b or is_i or is_u or is_s or script or (1 <= fsz <= 100):
                    f_start = max(0, frag.position() - b_pos)
                    f_len = min(len(text) - f_start, frag.length())
                    for pos in range(f_start, f_start + f_len):
                        merged = self.format(pos)
                        if hl_hex:
                            merged.setBackground(_highlight_brush_for_hex(hl_hex, self._dark_mode))
                        if col_hex:
                            merged.setForeground(QColor(col_hex))
                        if is_b:
                            merged.setFontWeight(QFont.Weight.Bold)
                        if is_i:
                            merged.setFontItalic(True)
                        if is_u:
                            merged.setFontUnderline(True)
                        if is_s:
                            merged.setFontStrikeOut(True)
                        if script == "sub":
                            merged.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSubScript)
                        elif script == "sup":
                            merged.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
                        if 1 <= fsz <= 100:
                            merged.setFontPointSize(float(fsz))
                        self.setFormat(pos, 1, merged)
            fit += 1


SLASH_COMMANDS: List[Dict[str, str]] = [
    {"cmd": "/tabella", "label": "▦  /tabella", "desc": "Tabella interattiva (celle navigabili con Tab)"},
    {"cmd": "/quote", "label": "▎  /quote", "desc": "Blocco citazione con barra verticale"},
    {"cmd": "/callout", "label": "◈  /callout", "desc": "Nota in evidenza"},
    {"cmd": "/todo", "label": "☐  /todo", "desc": "To-Do list interattiva cliccabile"},
    {"cmd": "/elenco", "label": "•  /elenco", "desc": "Elenco puntato (oppure digita '- ' a inizio riga)"},
    {"cmd": "/code", "label": "⌘  /code", "desc": "Blocco codice a larghezza piena"},
    {"cmd": "/bibtex", "label": "❝  /bibtex", "desc": "Blocco citazione BibTeX accademico"},
    {"cmd": "/file", "label": "⎘  /file", "desc": "Allega un file (PDF, Immagine, Doc)"},
    {"cmd": "/immagine", "label": "▣  /immagine", "desc": "Incorpora immagine da file"},
    {"cmd": "/formula", "label": "∑  /formula", "desc": "Blocco equazione LaTeX ($$ ... $$)"},
    {"cmd": "/matrice", "label": "⊞  /matrice", "desc": "Matrice interattiva (bmatrix)"},
    {"cmd": "/ocr", "label": "⌖  /ocr", "desc": "Estrai LaTeX/Tabella da immagine (Ctrl+Shift+V)"},
    {"cmd": "/h1", "label": "H1 /h1", "desc": "Titolo grande di sezione"},
    {"cmd": "/h2", "label": "H2 /h2", "desc": "Sottotitolo di sezione"},
    {"cmd": "/h3", "label": "H3 /h3", "desc": "Titolo di terzo livello"},
    {"cmd": "/riassumi", "label": "✦  /riassumi", "desc": "Riassumi nota con AI locale"},
    {"cmd": "/flashcard", "label": "❖  /flashcard", "desc": "Genera flashcard d'esame"},
    {"cmd": "/rsvp", "label": "⚡  /rsvp", "desc": "Lettura veloce RSVP (alias: /speedread, Alt+R)"},
]


class WikilinkPopup(QListWidget):
    """Popup di autocompletamento live per [[wikilink]] verso altre note di FlowNotes."""

    noteChosen = pyqtSignal(str)  # titolo scelto

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(290)
        self.setMaximumHeight(220)
        self.itemClicked.connect(self._on_item_clicked)
        self.apply_theme(dark_mode)

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
        if dark_mode:
            bg, fg, bd, sel_bg, sel_fg = "#161B26", "#F1F5F9", "#2D3748", "#312E81", "#FFFFFF"
        else:
            bg, fg, bd, sel_bg, sel_fg = "#FFFFFF", "#0F172A", "#CBD5E1", "#E0E7FF", "#1E1B4B"
        self.setStyleSheet(
            f"WikilinkPopup {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 8px; padding: 4px; font-size: 12px; }}"
            f"WikilinkPopup::item {{ padding: 6px 10px; border-radius: 5px; }}"
            f"WikilinkPopup::item:selected {{ background-color: {sel_bg}; color: {sel_fg}; }}"
        )

    def filter_notes(self, prefix: str = "") -> int:
        from database.db_manager import db
        self.clear()
        try:
            rows = db.search_note_titles_for_wikilink(prefix, limit=12)
        except Exception:
            rows = []
        for r in rows:
            raw_t = strip_markdown_syntax((r.get("title") or "").replace("Nota: ", "").strip())
            if not raw_t:
                continue
            subj = (r.get("subject") or "Generale").strip()
            label = f"🔗 {raw_t}   ·   {subj}" if subj and subj != "Generale" else f"🔗 {raw_t}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, raw_t)
            self.addItem(item)
        if self.count() > 0:
            self.setCurrentRow(0)
            row_h = max(28, self.sizeHintForRow(0))
            self.setFixedHeight(min(220, row_h * self.count() + 12))
        return self.count()

    def current_title(self) -> str:
        it = self.currentItem()
        return str(it.data(Qt.ItemDataRole.UserRole) or "") if it is not None else ""

    def _on_item_clicked(self, item: QListWidgetItem):
        title = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if title:
            self.noteChosen.emit(title)


class SlashPreviewPopup(QFrame):
    """Pannello laterale di anteprima live per il comando selezionato nel menu Slash (/)."""

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self._current_cmd: str = ""
        self._latex_pix_cache: Dict[Tuple[str, bool], QPixmap] = {}

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(238)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)

        self._hdr_lbl = QLabel("ANTEPRIMA")
        lay.addWidget(self._hdr_lbl)

        self._title_lbl = QLabel("")
        self._title_lbl.setWordWrap(True)
        lay.addWidget(self._title_lbl)

        self._preview_box = QFrame()
        self._preview_box.setObjectName("SlashPreviewBox")
        box_lay = QVBoxLayout(self._preview_box)
        box_lay.setContentsMargins(10, 8, 10, 8)
        box_lay.setSpacing(4)

        self._rich_lbl = QLabel()
        self._rich_lbl.setWordWrap(True)
        self._rich_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._rich_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        box_lay.addWidget(self._rich_lbl)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.hide()
        box_lay.addWidget(self._img_lbl)

        lay.addWidget(self._preview_box)

        self._hint_lbl = QLabel("")
        self._hint_lbl.setWordWrap(True)
        lay.addWidget(self._hint_lbl)

        self.apply_theme(dark_mode)

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
        if dark_mode:
            bg, bd, box_bg, box_bd = "#191919", "#2F2F2F", "#12141C", "#262B38"
            fg, muted, hdr = "#F8FAFC", "#94A3B8", "#64748B"
        else:
            bg, bd, box_bg, box_bd = "#FFFFFF", "#E2E8F0", "#F8FAFC", "#CBD5E1"
            fg, muted, hdr = "#0F172A", "#475569", "#64748B"

        self.setStyleSheet(
            f"SlashPreviewPopup {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 8px; }}"
            f"QFrame#SlashPreviewBox {{ background-color: {box_bg}; border: 1px solid {box_bd}; border-radius: 6px; }}"
        )
        self._hdr_lbl.setStyleSheet(f"color: {hdr}; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        self._title_lbl.setStyleSheet(f"color: {fg}; font-size: 12px; font-weight: 600;")
        self._rich_lbl.setStyleSheet(f"color: {fg}; font-size: 11px;")
        self._hint_lbl.setStyleSheet(f"color: {muted}; font-size: 10px;")

        if self._current_cmd:
            self.set_command(self._current_cmd)

    def _get_cached_latex_pixmap(self, kind: str) -> Optional[QPixmap]:
        key = (kind, self._dark_mode)
        if key in self._latex_pix_cache:
            return self._latex_pix_cache[key]
        ink = "#E2E8F0" if self._dark_mode else "#0F172A"
        pix: Optional[QPixmap] = None
        try:
            if kind == "/matrice":
                res = _render_matrix_png_b64(
                    "\\begin{bmatrix} 1 & 0 \\\\ 0 & 1 \\end{bmatrix}",
                    dark_mode=self._dark_mode,
                    reading_mode=False,
                )
                if res is not None:
                    qimg = QImage.fromData(base64.b64decode(res[0]), "PNG")
                    if not qimg.isNull():
                        pix = QPixmap.fromImage(qimg)
            elif kind == "/formula":
                qimg = _mathtext_to_transparent_qimage(r"\int_0^1 x^2\,dx = \frac{1}{3}", dpi=155, ink_hex=ink)
                if qimg is not None and not qimg.isNull():
                    pix = QPixmap.fromImage(qimg)
        except Exception:
            pix = None
        if pix is not None and not pix.isNull():
            if pix.width() > 188:
                pix = pix.scaledToWidth(188, Qt.TransformationMode.SmoothTransformation)
            self._latex_pix_cache[key] = pix
        return pix

    def set_command(self, cmd: str):
        self._current_cmd = cmd or ""
        fg = "#E2E8F0" if self._dark_mode else "#1E293B"
        muted = "#94A3B8" if self._dark_mode else "#64748B"
        bd = "#334155" if self._dark_mode else "#CBD5E1"
        acc = "#818CF8" if self._dark_mode else "#4F46E5"

        self._img_lbl.hide()
        self._rich_lbl.show()

        if cmd in ("/formula", "/matrice"):
            pix = self._get_cached_latex_pixmap(cmd)
            if pix is not None:
                self._img_lbl.setPixmap(pix)
                self._img_lbl.show()
                self._rich_lbl.hide()

        previews: Dict[str, Tuple[str, str, str]] = {
            "/tabella": (
                "Tabella Interattiva",
                f"<table border='1' cellspacing='0' cellpadding='4' style='border-color:{bd}; width:100%; font-size:10px; color:{fg};'>"
                f"<tr><th>Col 1</th><th>Col 2</th><th>Col 3</th></tr>"
                f"<tr><td>Dato A</td><td>12.4</td><td>OK</td></tr>"
                f"<tr><td>Dato B</td><td>8.1</td><td>…</td></tr></table>",
                "Naviga tra le celle con Tab / Shift+Tab.",
            ),
            "/quote": (
                "Citazione",
                f"<div style='border-left: 3px solid {acc}; padding-left: 8px; color:{muted}; font-style: italic;'>"
                f"“La matematica è il linguaggio della natura.”</div>",
                "Premi Invio su riga vuota per uscire.",
            ),
            "/callout": (
                "Nota in Evidenza",
                f"<div style='background-color:rgba(245,158,11,0.14); border-left:3px solid #F59E0B; padding:6px 8px; color:{fg};'>"
                f"💡 <b>Definizione chiave</b><br><span style='color:{muted};'>Concetto da ricordare per l'esame.</span></div>",
                "Blocco evidenziato stile Notion.",
            ),
            "/todo": (
                "Lista To-Do",
                f"<div style='color:{fg}; line-height:1.5;'>"
                f"☑ Ripassare derivate<br>☐ Risolvere esercizi Cap. 3</div>",
                "Clicca su ☐/☑ per spuntare.",
            ),
            "/elenco": (
                "Elenco Puntato",
                f"<div style='color:{fg}; line-height:1.5;'>"
                f"• Primo punto<br>• Secondo punto</div>",
                "Digita '- ' a inizio riga · Tab per indentare.",
            ),
            "/code": (
                "Blocco Python Eseguibile",
                f"<div style='font-family:Consolas,monospace; font-size:10px; color:{fg};'>"
                f"<span style='color:#10B981; font-weight:bold;'>▶ Esegui</span><br>"
                f"x = np.linspace(0, 6)<br>plt.plot(x, np.sin(x))</div>",
                "Esegui con ▶ Esegui o Ctrl+Invio.",
            ),
            "/bibtex": (
                "Citazione BibTeX",
                f"<div style='font-family:Consolas,monospace; font-size:10px; color:{muted};'>"
                f"@article{{rossi2026,<br>&nbsp;&nbsp;author = {{Rossi, M.}},<br>&nbsp;&nbsp;year = {{2026}}}}</div>",
                "Card formattata per bibliografia.",
            ),
            "/file": (
                "Allega File",
                f"<div style='color:{fg};'>📄 <b>Dispense_Lez04.pdf</b><br>"
                f"<span style='color:{muted}; font-size:10px;'>Clicca per aprire</span></div>",
                "Incorpora PDF, immagini o documenti.",
            ),
            "/immagine": (
                "Immagine Inline",
                f"<div style='text-align:center; color:{muted}; padding:6px 0;'>"
                f"🖼️ <b>Immagine da file</b><br><span style='font-size:10px;'>PNG, JPG, WEBP…</span></div>",
                "Seleziona un file immagine dal PC.",
            ),
            "/formula": (
                "Formula LaTeX",
                f"<div style='text-align:center; color:{fg}; font-family:serif; font-size:13px;'>∫₀¹ x² dx = 1/3</div>",
                "Doppio clic sulla formula per modificarla.",
            ),
            "/matrice": (
                "Matrice Interattiva",
                f"<div style='text-align:center; color:{fg}; font-family:Consolas,monospace;'>[ 1  0 ; 0  1 ]</div>",
                "Modificabile con griglia interattiva.",
            ),
            "/ocr": (
                "Vision OCR Istantaneo",
                f"<div style='color:{fg};'>📷 <b>Screenshot → LaTeX / Tabella</b><br>"
                f"<span style='color:{muted}; font-size:10px;'>Converte formule, matrici e tabelle dagli appunti.</span></div>",
                "Scorciatoia rapida: Ctrl+Shift+V.",
            ),
            "/h1": (
                "Titolo Grande (H1)",
                f"<div style='font-size:16px; font-weight:700; color:{fg};'>Titolo di Sezione</div>",
                "Intestazione principale.",
            ),
            "/h2": (
                "Sottotitolo (H2)",
                f"<div style='font-size:14px; font-weight:700; color:{fg};'>Sottotitolo</div>",
                "Intestazione di secondo livello.",
            ),
            "/h3": (
                "Titolo 3 (H3)",
                f"<div style='font-size:12px; font-weight:600; color:{fg};'>Paragrafo evidenza</div>",
                "Intestazione di terzo livello.",
            ),
            "/riassumi": (
                "Riassunto AI (Qwen 3.5)",
                f"<div style='color:{fg};'>✦ <b>Sintesi automatica</b><br>"
                f"<span style='color:{muted}; font-size:10px;'>Genera punti chiave e formule principali della nota.</span></div>",
                "Elaborazione locale tramite Qwen 3.5.",
            ),
            "/flashcard": (
                "Flashcard d'Esame",
                f"<div style='color:{fg};'>🃏 <b>Domanda → Risposta</b><br>"
                f"<span style='color:{muted}; font-size:10px;'>Crea carte di ripasso dalla nota corrente.</span></div>",
                "Salvate nella sezione Flashcard.",
            ),
            "/rsvp": (
                "Lettura Veloce RSVP",
                f"<div style='text-align:center; color:{fg}; font-size:14px; font-weight:700;'>"
                f"le<span style='color:{acc};'>t</span>tura</div>"
                f"<div style='text-align:center; color:{muted}; font-size:10px; margin-top:4px;'>"
                f"350 WPM · ORP focale · Pausa su formule $$</div>",
                "Scorciatoia: Alt+R (o seleziona testo).",
            ),
        }

        title, html_body, hint = previews.get(
            cmd,
            (cmd or "Comando", f"<div style='color:{muted};'>Anteprima non disponibile.</div>", ""),
        )
        self._title_lbl.setText(title)
        if self._rich_lbl.isVisible():
            self._rich_lbl.setText(html_body)
        self._hint_lbl.setText(hint)
        self.adjustSize()


class SlashCommandPopup(QListWidget):
    """Menu fluttuante stile Notion per i comandi rapidi (/)."""

    commandChosen = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(340)
        self.setMaximumHeight(280)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self._last_mouse_pos: Optional[QPoint] = None
        self._preview_popup = SlashPreviewPopup(parent, dark_mode=dark_mode)
        self._preview_popup.hide()
        self.apply_theme(dark_mode)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentRowChanged.connect(self._on_current_row_changed)
        self.filter_commands("")

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
        if hasattr(self, "_preview_popup") and self._preview_popup is not None:
            self._preview_popup.apply_theme(dark_mode)
        if dark_mode:
            self.setStyleSheet(
                "QListWidget {"
                "  background-color: #191919;"
                "  border: 1px solid #2F2F2F;"
                "  border-radius: 8px;"
                "  padding: 4px;"
                "  color: #E2E8F0;"
                "  font-size: 12px;"
                "}"
                "QListWidget::item {"
                "  padding: 6px 10px;"
                "  border-radius: 5px;"
                "  color: #CBD5E1;"
                "}"
                "QListWidget::item:selected {"
                "  background-color: #2A2A2A;"
                "  color: #FFFFFF;"
                "}"
                "QListWidget::item:hover {"
                "  background-color: #232323;"
                "}"
            )
        else:
            self.setStyleSheet(
                "QListWidget {"
                "  background-color: #FFFFFF;"
                "  border: 1px solid #E5E7EB;"
                "  border-radius: 8px;"
                "  padding: 4px;"
                "  color: #1E293B;"
                "  font-size: 12px;"
                "}"
                "QListWidget::item {"
                "  padding: 6px 10px;"
                "  border-radius: 5px;"
                "  color: #334155;"
                "}"
                "QListWidget::item:selected {"
                "  background-color: #EEF2FF;"
                "  color: #4F46E5;"
                "}"
                "QListWidget::item:hover {"
                "  background-color: #F8FAFC;"
                "}"
            )

    def filter_commands(self, prefix: str) -> int:
        self.clear()
        q = (prefix or "").lower()
        # Alias /tabs -> /tabella, /matrix -> /matrice, /speedread -> /rsvp
        if q == "/tabs":
            q = "/tabella"
        elif q == "/matrix":
            q = "/matrice"
        elif q.startswith("/sp") and "/speedread".startswith(q):
            q = "/rsvp"
        for entry in SLASH_COMMANDS:
            if not q or entry["cmd"].startswith(q) or q.lstrip("/") in entry["desc"].lower():
                item = QListWidgetItem(f"{entry['label']}  —  {entry['desc']}")
                item.setData(Qt.ItemDataRole.UserRole, entry["cmd"])
                self.addItem(item)
        if self.count() > 0:
            self.setCurrentRow(0)
            row_h = max(30, self.sizeHintForRow(0))
            self.setFixedHeight(min(280, self.count() * row_h + 12))
        return self.count()

    def _on_item_clicked(self, item: QListWidgetItem):
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if cmd:
            self.commandChosen.emit(cmd)

    def current_command(self) -> Optional[str]:
        item = self.currentItem()
        if item is not None:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def _update_preview_for_cmd(self, cmd: Optional[str]):
        if not cmd or not self.isVisible():
            self._preview_popup.hide()
            return
        self._preview_popup.set_command(cmd)
        self._position_preview_popup()
        self._preview_popup.show()

    def _position_preview_popup(self):
        p_pos = self.pos()
        pw = self._preview_popup.width()
        ph = self._preview_popup.height()
        px = p_pos.x() + self.width() + 6
        py = p_pos.y()
        screen = QApplication.screenAt(p_pos) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if px + pw > avail.right():
                px = max(avail.left(), p_pos.x() - pw - 6)
            if py + ph > avail.bottom():
                py = max(avail.top(), avail.bottom() - ph - 6)
        self._preview_popup.move(px, py)

    def _on_current_row_changed(self, row: int):
        item = self.item(row) if row >= 0 else None
        cmd = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._update_preview_for_cmd(cmd)

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.pos()
        if self._last_mouse_pos is None or pos != self._last_mouse_pos:
            self._last_mouse_pos = pos
            item = self.itemAt(pos)
            if item is not None and item is not self.currentItem():
                self.setCurrentItem(item)
        super().mouseMoveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        cmd = self.current_command()
        if cmd:
            self._preview_popup.set_command(cmd)
            self._position_preview_popup()
            self._preview_popup.show()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.isVisible() and self._preview_popup.isVisible():
            self._position_preview_popup()

    def hideEvent(self, event):
        self._preview_popup.hide()
        super().hideEvent(event)


class _GridPickerCanvas(QWidget):
    """Canvas interattivo 6x6 di celle 22x22px con hover live in stile Word/Excel."""

    hoveredChanged = pyqtSignal(int, int)
    cellClicked = pyqtSignal(int, int)

    MAX_ROWS = 6
    MAX_COLS = 6
    CELL_SIZE = 22
    CELL_GAP = 4

    def __init__(self, dark_mode: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self._hover_r = 3
        self._hover_c = 3
        self.setMouseTracking(True)
        w = self.MAX_COLS * self.CELL_SIZE + (self.MAX_COLS - 1) * self.CELL_GAP + 4
        h = self.MAX_ROWS * self.CELL_SIZE + (self.MAX_ROWS - 1) * self.CELL_GAP + 4
        self.setFixedSize(w, h)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _cell_at(self, pos: QPoint) -> Tuple[int, int]:
        step = self.CELL_SIZE + self.CELL_GAP
        c = max(1, min(self.MAX_COLS, int((pos.x() - 2) // step) + 1))
        r = max(1, min(self.MAX_ROWS, int((pos.y() - 2) // step) + 1))
        return r, c

    def mouseMoveEvent(self, event: QMouseEvent):
        r, c = self._cell_at(event.pos())
        if (r, c) != (self._hover_r, self._hover_c):
            self._hover_r, self._hover_c = r, c
            self.hoveredChanged.emit(r, c)
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            r, c = self._cell_at(event.pos())
            self._hover_r, self._hover_c = r, c
            self.cellClicked.emit(r, c)
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        step = self.CELL_SIZE + self.CELL_GAP
        idle_fill = QColor("#262626" if self._dark_mode else "#F1F5F9")
        idle_border = QColor("#3F3F46" if self._dark_mode else "#CBD5E1")
        active_fill = QColor("#6366F1")
        active_border = QColor("#818CF8")

        for r in range(1, self.MAX_ROWS + 1):
            for c in range(1, self.MAX_COLS + 1):
                x = 2 + (c - 1) * step
                y = 2 + (r - 1) * step
                active = (r <= self._hover_r and c <= self._hover_c)
                painter.setBrush(active_fill if active else idle_fill)
                painter.setPen(QPen(active_border if active else idle_border, 1.2))
                painter.drawRoundedRect(QRectF(x, y, self.CELL_SIZE, self.CELL_SIZE), 4.0, 4.0)


class GridSizePickerPopup(QDialog):
    """Popup selettore dimensione griglia 6x6 stile Microsoft Word / Excel."""

    def __init__(self, title_prefix: str = "Tabella", dark_mode: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._title_prefix = title_prefix
        self._dark_mode = dark_mode
        self._selected: Optional[Tuple[int, int]] = None

        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        bg = "#191919" if dark_mode else "#FFFFFF"
        border = "#2F2F2F" if dark_mode else "#E2E8F0"
        fg = "#F1F5F9" if dark_mode else "#0F172A"
        self.setStyleSheet(
            f"QDialog {{ background-color: {bg}; border: 1px solid {border}; border-radius: 10px; }}"
            f"QLabel {{ color: {fg}; font-size: 12px; font-weight: 600; background: transparent; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        self._header_lbl = QLabel(f"{self._title_prefix} 3 × 3")
        self._header_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._header_lbl)

        self.canvas = _GridPickerCanvas(dark_mode=dark_mode, parent=self)
        self.canvas.hoveredChanged.connect(self._on_hovered)
        self.canvas.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignCenter)

    def _on_hovered(self, r: int, c: int):
        self._header_lbl.setText(f"{self._title_prefix} {r} × {c}")

    def _on_cell_clicked(self, r: int, c: int):
        self._selected = (r, c)
        self.accept()

    def selected_size(self) -> Optional[Tuple[int, int]]:
        return self._selected


class MatrixEditorDialog(QDialog):
    """Dialog visuale cella-per-cella per creare e modificare matrici LaTeX (bmatrix/pmatrix/vmatrix)."""

    _ENV_CHOICES = [
        ("[ ] Quadre (bmatrix)", "bmatrix", "[", "]"),
        ("( ) Tonde (pmatrix)", "pmatrix", "(", ")"),
        ("| | Determinante (vmatrix)", "vmatrix", "|", "|"),
    ]

    def __init__(
        self,
        rows: int = 3,
        cols: int = 3,
        initial_tex: str = "",
        dark_mode: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self._rows = max(1, min(8, rows))
        self._cols = max(1, min(8, cols))
        self._env = "bmatrix"
        self._prefix = ""
        self._cells: List[List[QLineEdit]] = []
        initial_values: List[List[str]] = []

        if initial_tex.strip():
            m = re.search(r"\\begin\{([bpv])matrix\}(.*?)\\end\{\1matrix\}", initial_tex, re.DOTALL)
            if m:
                self._env = f"{m.group(1)}matrix"
                self._prefix = initial_tex[: m.start()].strip()
                raw_rows = [r.strip() for r in m.group(2).split(r"\\") if r.strip()]
                if raw_rows:
                    initial_values = [[c.strip() for c in r.split("&")] for r in raw_rows]
                    self._rows = max(1, min(8, len(initial_values)))
                    self._cols = max(1, min(8, max(len(r) for r in initial_values)))

        self.setWindowTitle("Editor Matrice LaTeX")
        self.setModal(True)
        self._apply_dialog_style()
        self._build_ui(initial_values)

    def _apply_dialog_style(self):
        if self._dark_mode:
            self.setStyleSheet(
                "QDialog { background-color: #18181B; color: #F1F5F9; }"
                "QLabel { color: #CBD5E1; font-size: 12px; background: transparent; }"
                "QLineEdit { background-color: #27272A; color: #F8FAFC; border: 1px solid #3F3F46;"
                "  border-radius: 6px; padding: 4px 6px; font-size: 13px; }"
                "QLineEdit:focus { border: 1px solid #6366F1; background-color: #1E1B4B; }"
                "QComboBox { background-color: #27272A; color: #F8FAFC; border: 1px solid #3F3F46;"
                "  border-radius: 6px; padding: 4px 8px; min-height: 28px; }"
                "QPushButton { background-color: #27272A; color: #E2E8F0; border: 1px solid #3F3F46;"
                "  border-radius: 6px; padding: 5px 10px; font-size: 12px; }"
                "QPushButton:hover { background-color: #3F3F46; }"
                "QPushButton#MatrixConfirmBtn { background-color: #6366F1; color: #FFFFFF; border: none; font-weight: 600; }"
                "QPushButton#MatrixConfirmBtn:hover { background-color: #4F46E5; }"
            )
        else:
            self.setStyleSheet(
                "QDialog { background-color: #FFFFFF; color: #0F172A; }"
                "QLabel { color: #334155; font-size: 12px; background: transparent; }"
                "QLineEdit { background-color: #F8FAFC; color: #0F172A; border: 1px solid #CBD5E1;"
                "  border-radius: 6px; padding: 4px 6px; font-size: 13px; }"
                "QLineEdit:focus { border: 1px solid #6366F1; background-color: #FFFFFF; }"
                "QComboBox { background-color: #FFFFFF; color: #0F172A; border: 1px solid #CBD5E1;"
                "  border-radius: 6px; padding: 4px 8px; min-height: 28px; }"
                "QPushButton { background-color: #F8FAFC; color: #1E293B; border: 1px solid #CBD5E1;"
                "  border-radius: 6px; padding: 5px 10px; font-size: 12px; }"
                "QPushButton:hover { background-color: #E2E8F0; }"
                "QPushButton#MatrixConfirmBtn { background-color: #6366F1; color: #FFFFFF; border: none; font-weight: 600; }"
                "QPushButton#MatrixConfirmBtn:hover { background-color: #4F46E5; }"
            )

    def _build_ui(self, initial_values: List[List[str]]):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # Barra superiore: Prefisso + Tipo parentesi
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(QLabel("Prefisso:"))
        self.prefix_edit = QLineEdit(self._prefix)
        self.prefix_edit.setPlaceholderText("es. T = , A = , J(q) =")
        self.prefix_edit.setMinimumWidth(120)
        self.prefix_edit.setMinimumHeight(30)
        top_row.addWidget(self.prefix_edit, 1)

        top_row.addWidget(QLabel("Parentesi:"))
        self.env_combo = QComboBox()
        for label, env_name, _l, _r in self._ENV_CHOICES:
            self.env_combo.addItem(label, env_name)
        env_idx = self.env_combo.findData(self._env)
        if env_idx >= 0:
            self.env_combo.setCurrentIndex(env_idx)
        self.env_combo.currentIndexChanged.connect(self._sync_brackets)
        top_row.addWidget(self.env_combo)
        root.addLayout(top_row)

        # Barra dimensionamento rapido
        dim_row = QHBoxLayout()
        dim_row.setSpacing(6)
        self.dim_label = QLabel(f"Dimensione: {self._rows} × {self._cols}")
        dim_row.addWidget(self.dim_label)
        dim_row.addStretch(1)

        for lbl, dr, dc in [
            ("+ Riga", 1, 0),
            ("- Riga", -1, 0),
            ("+ Colonna", 0, 1),
            ("- Colonna", 0, -1),
        ]:
            btn = QPushButton(lbl)
            btn.clicked.connect(lambda _c=False, r_d=dr, c_d=dc: self._resize_grid(r_d, c_d))
            dim_row.addWidget(btn)
        root.addLayout(dim_row)

        # Griglia celle tra parentesi
        mat_row = QHBoxLayout()
        mat_row.setSpacing(6)
        bracket_color = "#CBD5E1" if self._dark_mode else "#334155"
        self.left_bracket_lbl = QLabel("[")
        self.left_bracket_lbl.setStyleSheet(f"font-size: 42px; font-weight: 300; color: {bracket_color};")
        self.right_bracket_lbl = QLabel("]")
        self.right_bracket_lbl.setStyleSheet(f"font-size: 42px; font-weight: 300; color: {bracket_color};")

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(2, 2, 2, 2)
        self.grid_layout.setSpacing(6)

        mat_row.addStretch(1)
        mat_row.addWidget(self.left_bracket_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        mat_row.addWidget(self.grid_widget)
        mat_row.addWidget(self.right_bracket_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        mat_row.addStretch(1)
        root.addLayout(mat_row)

        # Bottoni conferma
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Annulla")
        cancel_btn.clicked.connect(self.reject)
        confirm_btn = QPushButton("Inserisci / Aggiorna Matrice")
        confirm_btn.setObjectName("MatrixConfirmBtn")
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(confirm_btn)
        root.addLayout(btn_row)

        self._rebuild_cells(initial_values)
        self._sync_brackets()

    def _sync_brackets(self):
        env_name = self.env_combo.currentData() or "bmatrix"
        self._env = env_name
        l_ch, r_ch = "[", "]"
        for _lbl, name, l_sym, r_sym in self._ENV_CHOICES:
            if name == env_name:
                l_ch, r_ch = l_sym, r_sym
                break
        self.left_bracket_lbl.setText(l_ch)
        self.right_bracket_lbl.setText(r_ch)

    def _snapshot_values(self) -> List[List[str]]:
        return [[cell.text() for cell in row] for row in self._cells]

    def _resize_grid(self, dr: int, dc: int):
        new_r = max(1, min(8, self._rows + dr))
        new_c = max(1, min(8, self._cols + dc))
        if (new_r, new_c) == (self._rows, self._cols):
            return
        snap = self._snapshot_values()
        self._rows, self._cols = new_r, new_c
        self._rebuild_cells(snap)
        self.adjustSize()

    def _rebuild_cells(self, values: List[List[str]]):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cells = []
        self.dim_label.setText(f"Dimensione: {self._rows} × {self._cols}")

        for r in range(self._rows):
            row_edits: List[QLineEdit] = []
            for c in range(self._cols):
                default_val = "1" if r == c else "0"
                val = values[r][c] if (r < len(values) and c < len(values[r])) else default_val
                le = QLineEdit(val)
                le.setAlignment(Qt.AlignmentFlag.AlignCenter)
                le.setFixedSize(68, 30)
                self.grid_layout.addWidget(le, r, c)
                row_edits.append(le)
            self._cells.append(row_edits)

        # Tab order riga per riga
        flat = [cell for row in self._cells for cell in row]
        for idx in range(len(flat) - 1):
            QWidget.setTabOrder(flat[idx], flat[idx + 1])

    def set_cell_value(self, r: int, c: int, val: str):
        if 0 <= r < len(self._cells) and 0 <= c < len(self._cells[r]):
            self._cells[r][c].setText(val)

    def to_latex(self) -> str:
        env = self.env_combo.currentData() or self._env or "bmatrix"
        rows_tex = [
            " & ".join((cell.text().strip() or "0") for cell in row)
            for row in self._cells
        ]
        body = " \\\\\n".join(rows_tex)
        mat = f"\\begin{{{env}}}\n{body}\n\\end{{{env}}}"
        prefix = self.prefix_edit.text().strip()
        return f"{prefix} {mat}" if prefix else mat


class InlineMatrixEditor(QFrame):
    """
    Editor in-place (Dual-State Interactive Matrix) sovrapposto alla matrice nella viewport.
    Supporta navigazione Tab/Frecce, +Riga/-Riga/+Colonna/-Colonna, tipo parentesi,
    Elimina (✕) e commit automatico su focus-out o Ctrl+Invio.
    """

    commitRequested = pyqtSignal(str)
    deleteRequested = pyqtSignal()

    _ENV_CHOICES = [
        ("[ ]", "bmatrix", "[", "]"),
        ("( )", "pmatrix", "(", ")"),
        ("| |", "vmatrix", "|", "|"),
    ]

    def __init__(
        self,
        parent_viewport: QWidget,
        doc_pos: int,
        initial_tex: str = "",
        dark_mode: bool = True,
    ):
        super().__init__(parent_viewport)
        self.doc_pos = doc_pos
        self._dark_mode = dark_mode
        self._rows = 3
        self._cols = 3
        self._env = "bmatrix"
        self._prefix = ""
        self._cells: List[List[QLineEdit]] = []
        self._closing = False

        initial_values: List[List[str]] = []
        if initial_tex.strip():
            m = re.search(r"\\begin\{([bpv])matrix\}(.*?)\\end\{\1matrix\}", initial_tex, re.DOTALL)
            if m:
                self._env = f"{m.group(1)}matrix"
                self._prefix = initial_tex[: m.start()].strip()
                raw_rows = [r.strip() for r in m.group(2).split(r"\\") if r.strip()]
                if raw_rows:
                    initial_values = [[c.strip() for c in r.split("&")] for r in raw_rows]
                    self._rows = max(1, min(8, len(initial_values)))
                    self._cols = max(1, min(8, max(len(r) for r in initial_values)))

        self.setObjectName("InlineMatrixEditor")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_style()
        self._build_ui(initial_values)

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_app_focus_changed)

    def _apply_style(self):
        if self._dark_mode:
            self.setStyleSheet(
                "QFrame#InlineMatrixEditor {"
                "  background-color: #18181B;"
                "  border: 1.5px solid #6366F1;"
                "  border-radius: 10px;"
                "}"
                "QLabel { color: #CBD5E1; font-size: 11px; background: transparent; }"
                "QLineEdit {"
                "  background-color: #27272A; color: #F8FAFC; border: 1px solid #3F3F46;"
                "  border-radius: 5px; padding: 2px 4px; font-size: 12px; min-height: 24px;"
                "}"
                "QLineEdit:focus { border: 1px solid #818CF8; background-color: #1E1B4B; }"
                "QComboBox {"
                "  background-color: #27272A; color: #F8FAFC; border: 1px solid #3F3F46;"
                "  border-radius: 5px; padding: 2px 6px; font-size: 11px; min-height: 24px;"
                "}"
                "QPushButton {"
                "  background-color: #27272A; color: #E2E8F0; border: 1px solid #3F3F46;"
                "  border-radius: 5px; padding: 3px 7px; font-size: 11px; min-height: 22px;"
                "}"
                "QPushButton:hover { background-color: #3F3F46; }"
                "QPushButton#InlineMatDelBtn { color: #F87171; border-color: #4C1D24; }"
                "QPushButton#InlineMatDelBtn:hover { background-color: #7F1D1D; color: #FFFFFF; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#InlineMatrixEditor {"
                "  background-color: #FFFFFF;"
                "  border: 1.5px solid #6366F1;"
                "  border-radius: 10px;"
                "}"
                "QLabel { color: #334155; font-size: 11px; background: transparent; }"
                "QLineEdit {"
                "  background-color: #F8FAFC; color: #0F172A; border: 1px solid #CBD5E1;"
                "  border-radius: 5px; padding: 2px 4px; font-size: 12px; min-height: 24px;"
                "}"
                "QLineEdit:focus { border: 1px solid #6366F1; background-color: #FFFFFF; }"
                "QComboBox {"
                "  background-color: #F8FAFC; color: #0F172A; border: 1px solid #CBD5E1;"
                "  border-radius: 5px; padding: 2px 6px; font-size: 11px; min-height: 24px;"
                "}"
                "QPushButton {"
                "  background-color: #F1F5F9; color: #1E293B; border: 1px solid #CBD5E1;"
                "  border-radius: 5px; padding: 3px 7px; font-size: 11px; min-height: 22px;"
                "}"
                "QPushButton:hover { background-color: #E2E8F0; }"
                "QPushButton#InlineMatDelBtn { color: #DC2626; border-color: #FECACA; }"
                "QPushButton#InlineMatDelBtn:hover { background-color: #FEE2E2; }"
            )

    def _build_ui(self, initial_values: List[List[str]]):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(4)

        self.prefix_edit = QLineEdit(self._prefix)
        self.prefix_edit.setPlaceholderText("A =")
        self.prefix_edit.setFixedWidth(56)
        self.prefix_edit.setMinimumHeight(24)
        self.prefix_edit.installEventFilter(self)
        bar.addWidget(self.prefix_edit)

        for lbl, dr, dc in [
            ("+ Riga", 1, 0),
            ("- Riga", -1, 0),
            ("+ Colonna", 0, 1),
            ("- Colonna", 0, -1),
        ]:
            btn = QPushButton(lbl)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _c=False, r_d=dr, c_d=dc: self._resize_grid(r_d, c_d))
            bar.addWidget(btn)

        self.env_combo = QComboBox()
        for label, env_name, _l, _r in self._ENV_CHOICES:
            self.env_combo.addItem(label, env_name)
        idx = self.env_combo.findData(self._env)
        if idx >= 0:
            self.env_combo.setCurrentIndex(idx)
        self.env_combo.currentIndexChanged.connect(self._sync_brackets)
        bar.addWidget(self.env_combo)

        del_btn = QPushButton("✕")
        del_btn.setObjectName("InlineMatDelBtn")
        del_btn.setToolTip("Elimina matrice")
        del_btn.setFixedWidth(26)
        del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        del_btn.clicked.connect(self._on_delete)
        bar.addWidget(del_btn)
        root.addLayout(bar)

        mat_row = QHBoxLayout()
        mat_row.setSpacing(5)
        b_col = "#94A3B8" if self._dark_mode else "#475569"
        self.left_bracket_lbl = QLabel("[")
        self.left_bracket_lbl.setStyleSheet(f"font-size: 32px; font-weight: 300; color: {b_col};")
        self.right_bracket_lbl = QLabel("]")
        self.right_bracket_lbl.setStyleSheet(f"font-size: 32px; font-weight: 300; color: {b_col};")

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(2, 2, 2, 2)
        self.grid_layout.setSpacing(4)

        mat_row.addStretch(1)
        mat_row.addWidget(self.left_bracket_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        mat_row.addWidget(self.grid_widget)
        mat_row.addWidget(self.right_bracket_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        mat_row.addStretch(1)
        root.addLayout(mat_row)

        hint = QLabel("Tab / Frecce: naviga  •  Ctrl+Invio o clic fuori: salva")
        hint.setStyleSheet("font-size: 10px; opacity: 0.7;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)

        self._rebuild_grid(initial_values)
        self._sync_brackets()
        self.adjustSize()

    def _sync_brackets(self):
        env = self.env_combo.currentData() or "bmatrix"
        for _lbl, name, l_ch, r_ch in self._ENV_CHOICES:
            if name == env:
                self.left_bracket_lbl.setText(l_ch)
                self.right_bracket_lbl.setText(r_ch)
                break

    def _snapshot_values(self) -> List[List[str]]:
        return [[c.text() for c in row] for row in self._cells]

    def _resize_grid(self, dr: int, dc: int):
        new_r = max(1, min(8, self._rows + dr))
        new_c = max(1, min(8, self._cols + dc))
        if new_r == self._rows and new_c == self._cols:
            return
        vals = self._snapshot_values()
        self._rows, self._cols = new_r, new_c
        self._rebuild_grid(vals)
        self.adjustSize()
        if self._cells and self._cells[0]:
            self._cells[0][0].setFocus()

    def _rebuild_grid(self, values: List[List[str]]):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cells.clear()

        for r in range(self._rows):
            row_edits: List[QLineEdit] = []
            for c in range(self._cols):
                val = ""
                if r < len(values) and c < len(values[r]):
                    val = values[r][c]
                elif not values:
                    val = "1" if (self._rows == self._cols and r == c) else "0"
                le = QLineEdit(val)
                le.setAlignment(Qt.AlignmentFlag.AlignCenter)
                le.setFixedWidth(48)
                le.setMinimumHeight(26)
                le.setProperty("mat_pos", (r, c))
                le.installEventFilter(self)
                self.grid_layout.addWidget(le, r, c)
                row_edits.append(le)
            self._cells.append(row_edits)

    def focus_first_cell(self):
        if self._cells and self._cells[0]:
            self._cells[0][0].setFocus()
            self._cells[0][0].selectAll()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (mods & Qt.KeyboardModifier.ControlModifier):
                self.commit()
                return True
            if key == Qt.Key.Key_Escape:
                self.commit()
                return True

            pos = obj.property("mat_pos") if isinstance(obj, QLineEdit) else None
            if pos is not None:
                r, c = pos
                if key == Qt.Key.Key_Tab:
                    nc = (c + 1) % self._cols
                    nr = (r + (1 if nc == 0 else 0)) % self._rows
                    self._focus_cell(nr, nc)
                    return True
                if key == Qt.Key.Key_Backtab:
                    nc = (c - 1) if c > 0 else (self._cols - 1)
                    nr = r if c > 0 else ((r - 1) % self._rows)
                    self._focus_cell(nr, nc)
                    return True
                if key == Qt.Key.Key_Up and r > 0:
                    self._focus_cell(r - 1, c)
                    return True
                if key == Qt.Key.Key_Down and r < self._rows - 1:
                    self._focus_cell(r + 1, c)
                    return True
                if key == Qt.Key.Key_Left and isinstance(obj, QLineEdit) and obj.cursorPosition() == 0 and c > 0:
                    self._focus_cell(r, c - 1)
                    return True
                if (
                    key == Qt.Key.Key_Right
                    and isinstance(obj, QLineEdit)
                    and obj.cursorPosition() == len(obj.text())
                    and c < self._cols - 1
                ):
                    self._focus_cell(r, c + 1)
                    return True
        return super().eventFilter(obj, event)

    def _focus_cell(self, r: int, c: int):
        if 0 <= r < len(self._cells) and 0 <= c < len(self._cells[r]):
            cell = self._cells[r][c]
            cell.setFocus()
            cell.selectAll()

    def _on_app_focus_changed(self, _old: Optional[QWidget], new: Optional[QWidget]):
        if self._closing or new is None:
            return
        if new is self or self.isAncestorOf(new):
            return
        # Permetti interazione col popup del QComboBox
        if self.env_combo.view() is not None and (new is self.env_combo.view() or self.env_combo.view().isAncestorOf(new)):
            return
        self.commit()

    def _on_delete(self):
        if self._closing:
            return
        self._closing = True
        self._disconnect_focus()
        self.deleteRequested.emit()

    def commit(self):
        if self._closing:
            return
        self._closing = True
        self._disconnect_focus()
        self.commitRequested.emit(self.to_latex())

    def _disconnect_focus(self):
        app = QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._on_app_focus_changed)
            except TypeError:
                pass

    def to_latex(self) -> str:
        env = self.env_combo.currentData() or self._env or "bmatrix"
        rows_tex = [
            " & ".join((cell.text().strip() or "0") for cell in row)
            for row in self._cells
        ]
        body = " \\\\\n".join(rows_tex)
        mat = f"\\begin{{{env}}}\n{body}\n\\end{{{env}}}"
        prefix = self.prefix_edit.text().strip()
        return f"{prefix} {mat}" if prefix else mat


class DictationGhostLineEdit(QLineEdit):
    """QLineEdit con anteprima Ghost-Text delle prime 6 parole dell'ultima dettatura FlowType (Tab per incollare)."""

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self._ghost_dismissed = False
        sig = dictation_tracker.signals
        if sig is not None:
            sig.historyUpdated.connect(self._on_dictation_updated, Qt.ConnectionType.QueuedConnection)

    def _on_dictation_updated(self):
        self._ghost_dismissed = False
        self.update()

    def focusInEvent(self, event):
        self._ghost_dismissed = False
        super().focusInEvent(event)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.text() or not self.hasFocus() or self._ghost_dismissed:
            return
        preview = dictation_tracker.get_active_preview()
        if preview is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ghost_col = QColor(203, 213, 225, 130) if self._dark_mode else QColor(71, 85, 105, 130)
        painter.setPen(QPen(ghost_col))
        f = QFont(self.font())
        f.setItalic(True)
        painter.setFont(f)
        rect = self.contentsRect().adjusted(10, 0, -10, 0)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, preview["ghost_text"])
        painter.end()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        if not self.text() and not self._ghost_dismissed:
            preview = dictation_tracker.get_active_preview()
            if preview is not None:
                if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ShiftModifier):
                    full_text = dictation_tracker.consume()
                    if full_text:
                        self.setText(full_text)
                        self.setCursorPosition(len(full_text))
                    return
                elif key not in (
                    Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta
                ):
                    self._ghost_dismissed = True
        super().keyPressEvent(event)


_RAPID_OCR_INSTANCE = None


def _get_rapid_ocr():
    """Inizializza RapidOCR in modo lazy per il fallback istantaneo offline."""
    global _RAPID_OCR_INSTANCE
    if _RAPID_OCR_INSTANCE is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_OCR_INSTANCE = RapidOCR()
    return _RAPID_OCR_INSTANCE


class _VisionOcrWorker(QThread):
    """Worker asincrono per Vision OCR: Gemini Cloud (se attivo/ibrido) o Ollama locale, fallback RapidOCR."""

    ocrCompleted = pyqtSignal(str, str, bool, str)  # placeholder_id, result_text, used_fallback, error_msg

    def __init__(self, placeholder_id: str, png_bytes: bytes, parent=None):
        super().__init__(parent)
        self.placeholder_id = placeholder_id
        self.png_bytes = png_bytes

    def run(self):
        from core.gemini_client import (
            DEFAULT_GEMINI_TIMEOUT,
            generate_content as gemini_generate_content,
            get_ai_routing_config,
            set_active_runtime_engine,
            should_use_gemini,
        )
        from core.hardware_profiler import get_active_profile

        prompt = (
            "Trascrivi fedelmente il contenuto di questa immagine restituendo SOLO il codice finale, "
            "senza introduzioni, commenti o spiegazioni:\n"
            "- Se è una formula, equazione o sistema di equazioni: restituisci codice LaTeX pulito racchiuso tra $$ e $$. "
            "Fai massima attenzione a esponenti/apici con ^ (es. E = mc^2, x^{n+1}), pedici con _ (es. x_1), "
            "frazioni con \\frac{a}{b}, radici con \\sqrt{x} e lettere greche.\n"
            "- Se è una singola matrice numerica o simbolica: restituisci esclusivamente \\begin{bmatrix} ... \\end{bmatrix}.\n"
            "- Se è una tabella di dati: restituisci una tabella Markdown formattata (| col | ... |).\n"
            "- Altrimenti restituisci il testo estratto pulito."
        )

        routing_cfg = get_ai_routing_config()
        provider = routing_cfg.get("provider", "local")
        use_gemini = should_use_gemini("vision_ocr")

        # 1. Primario Gemini Cloud BYOK se attivo in modalità 'adaptive', 'gemini' o 'hybrid'
        if use_gemini:
            try:
                content = gemini_generate_content(
                    prompt,
                    image_bytes=self.png_bytes,
                    image_mime="image/png",
                    temperature=0.1,
                    max_output_tokens=2048,
                    timeout=DEFAULT_GEMINI_TIMEOUT,
                )
                cleaned = _strip_think_blocks_safe(content)
                if cleaned:
                    set_active_runtime_engine("Adattivo • Gemini Online" if provider == "adaptive" else "Cloud • Gemini Online")
                    self.ocrCompleted.emit(self.placeholder_id, cleaned, False, "")
                    return
            except Exception as gem_exc:
                print(f"[VisionOCR] Gemini non raggiungibile ({gem_exc}). Fallback su motore locale.")

        # 2. Ollama Vision locale (primario in modalità 'local', o fallback se 'hybrid_fallback' / 'adaptive' attivo)
        if not use_gemini or routing_cfg.get("hybrid_fallback", True):
            local_vision_model = get_active_profile().llm_default_model or VISION_OCR_MODEL
            b64_img = base64.b64encode(self.png_bytes).decode("ascii")
            payload = {
                "model": local_vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [b64_img],
                    }
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30s",
                "options": {
                    "temperature": 0.1,
                },
            }
            try:
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_CHAT_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=28.0) as resp:
                    raw_json = json.loads(resp.read().decode("utf-8", errors="replace"))
                content = raw_json.get("message", {}).get("content", "") or ""
                cleaned = _strip_think_blocks_safe(content)
                if cleaned:
                    set_active_runtime_engine("Adattivo • Qwen 3.5 Offline" if provider == "adaptive" else "Locale • Qwen 3.5")
                    self.ocrCompleted.emit(self.placeholder_id, cleaned, False, "")
                    return
            except Exception:
                pass

        # Fallback istantaneo con rapidocr-onnxruntime
        try:
            import numpy as np
            from PIL import Image

            pil_img = Image.open(io.BytesIO(self.png_bytes)).convert("RGB")
            img_arr = np.array(pil_img)
            ocr_engine = _get_rapid_ocr()
            res, _ = ocr_engine(img_arr)
            lines = []
            if res:
                for item in res:
                    if len(item) >= 2 and item[1]:
                        lines.append(str(item[1]).strip())
            text_out = "\n".join(lines).strip()
            set_active_runtime_engine("Adattivo • RapidOCR Offline" if provider == "adaptive" else "Locale • RapidOCR")
            if text_out:
                self.ocrCompleted.emit(self.placeholder_id, text_out, True, "")
            else:
                self.ocrCompleted.emit(self.placeholder_id, "", True, "Nessun testo o formula rilevata nell'immagine.")
        except Exception as exc:
            self.ocrCompleted.emit(self.placeholder_id, "", True, f"Errore OCR fallback: {exc}")


_PYTHON_PRELUDE = (
    "import sys, os, math, tempfile, uuid\n"
    "try:\n"
    "    import numpy as np\n"
    "except Exception:\n"
    "    pass\n"
    "try:\n"
    "    import sympy as sp\n"
    "except Exception:\n"
    "    pass\n"
    "try:\n"
    "    import matplotlib\n"
    "    matplotlib.use('Agg')\n"
    "    import matplotlib.pyplot as plt\n"
    "except Exception:\n"
    "    plt = None\n"
)
_PYTHON_PRELUDE_LINES = _PYTHON_PRELUDE.count("\n")

_PYTHON_POSTLUDE = (
    "\ntry:\n"
    "    if 'plt' in globals() and plt is not None and plt.get_fignums():\n"
    "        _fl_dir = os.path.join(tempfile.gettempdir(), 'flowlocal_plots')\n"
    "        os.makedirs(_fl_dir, exist_ok=True)\n"
    "        for _fl_num in list(plt.get_fignums()):\n"
    "            _fl_fig = plt.figure(_fl_num)\n"
    "            _fl_out = os.path.join(_fl_dir, f'plot_{uuid.uuid4().hex[:10]}.png')\n"
    "            _fl_fig.savefig(_fl_out, dpi=150, bbox_inches='tight')\n"
    "            print(f'__FLOWLOCAL_PLOT__:{_fl_out}')\n"
    "        plt.close('all')\n"
    "except Exception as _fl_err:\n"
    "    print(f'[Plot Error] {_fl_err}', file=sys.stderr)\n"
)


class _PythonExecWorker(QThread):
    """Esegue un blocco Python in subprocess isolato con timeout 10s e cattura grafici matplotlib."""

    execCompleted = pyqtSignal(str, str, str, list, float, bool)  # code_id, stdout, stderr, plot_paths, elapsed, ok

    def __init__(self, code_id: str, code_text: str, parent=None):
        super().__init__(parent)
        self.code_id = code_id
        self.code_text = code_text

    def _clean_traceback(self, err_text: str, script_path: str) -> str:
        if not err_text:
            return ""
        cleaned = err_text.replace(script_path, "<blocco-python>")

        def _shift_line(m):
            ln = int(m.group(1))
            adj = max(1, ln - _PYTHON_PRELUDE_LINES)
            return f"line {adj}"

        cleaned = re.sub(r"\bline (\d+)\b", _shift_line, cleaned)
        return cleaned.strip()

    def run(self):
        t0 = time.perf_counter()
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", prefix="flowlocal_exec_", delete=False, encoding="utf-8"
            ) as tf:
                tmp_path = tf.name
                tf.write(_PYTHON_PRELUDE)
                tf.write(self.code_text)
                if not self.code_text.endswith("\n"):
                    tf.write("\n")
                tf.write(_PYTHON_POSTLUDE)

            kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": 10.0,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            cmd = [sys.executable, "--exec-python", tmp_path] if is_frozen() else [sys.executable, "-u", tmp_path]
            proc = subprocess.run(cmd, **kwargs)
            elapsed = time.perf_counter() - t0
            raw_out = proc.stdout or ""
            raw_err = self._clean_traceback(proc.stderr or "", tmp_path)

            plot_paths: List[str] = []
            out_lines: List[str] = []
            for ln in raw_out.splitlines():
                if ln.startswith("__FLOWLOCAL_PLOT__:"):
                    p = ln.split(":", 1)[1].strip()
                    if p and os.path.exists(p):
                        plot_paths.append(p)
                else:
                    out_lines.append(ln)

            ok = (proc.returncode == 0)
            self.execCompleted.emit(
                self.code_id,
                "\n".join(out_lines).strip(),
                raw_err,
                plot_paths,
                elapsed,
                ok,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.perf_counter() - t0
            self.execCompleted.emit(
                self.code_id,
                "",
                "TimeoutError: esecuzione interrotta (limite massimo 10 secondi).",
                [],
                elapsed,
                False,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            self.execCompleted.emit(self.code_id, "", f"Errore esecuzione: {exc}", [], elapsed, False)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


class CodeOutputPanel(QFrame):
    """Pannello inline compatto sotto al blocco codice Python per stdout, traceback e grafici."""

    closeRequested = pyqtSignal(str)
    insertPlotRequested = pyqtSignal(str, str)  # code_id, plot_path

    def __init__(self, code_id: str, parent=None, dark_mode: bool = True, reading_mode: bool = False):
        super().__init__(parent)
        self.code_id = code_id
        self._dark_mode = dark_mode
        self._reading_mode = reading_mode
        self._copy_text = ""
        self._plot_paths: List[str] = []

        self.setObjectName("CodeOutputPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(10, 6, 10, 8)
        root_lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)

        self._title_lbl = QLabel("Output")
        hdr.addWidget(self._title_lbl)
        hdr.addStretch(1)

        self._insert_plot_btn = QPushButton("📥 Inserisci grafico nella nota")
        self._insert_plot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._insert_plot_btn.clicked.connect(self._on_insert_plot)
        self._insert_plot_btn.hide()
        hdr.addWidget(self._insert_plot_btn)

        self._copy_btn = QPushButton("📋 Copia")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._on_copy)
        hdr.addWidget(self._copy_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(22, 20)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(lambda: self.closeRequested.emit(self.code_id))
        hdr.addWidget(self._close_btn)
        root_lay.addLayout(hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMaximumHeight(220)

        self._body_widget = QWidget()
        self._body_lay = QVBoxLayout(self._body_widget)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(6)

        self._out_lbl = QLabel()
        self._out_lbl.setWordWrap(True)
        self._out_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._out_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._body_lay.addWidget(self._out_lbl)

        self._err_lbl = QLabel()
        self._err_lbl.setWordWrap(True)
        self._err_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._err_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._body_lay.addWidget(self._err_lbl)

        self._plots_container = QWidget()
        self._plots_lay = QVBoxLayout(self._plots_container)
        self._plots_lay.setContentsMargins(0, 0, 0, 0)
        self._plots_lay.setSpacing(6)
        self._body_lay.addWidget(self._plots_container)

        self._scroll.setWidget(self._body_widget)
        root_lay.addWidget(self._scroll)

        self.apply_theme(dark_mode, reading_mode)

    def apply_theme(self, dark_mode: bool, reading_mode: bool = False):
        self._dark_mode = dark_mode
        self._reading_mode = reading_mode
        if reading_mode:
            bg = "#1E1712" if dark_mode else "#EFE2C8"
            bd = "#3A2F26" if dark_mode else "#DCC9A5"
            fg = "#E2E8F0" if dark_mode else "#292524"
            btn_bg = "#2C231C" if dark_mode else "#E5D3B3"
        else:
            bg = "#161820" if dark_mode else "#F1F5F9"
            bd = "#2D3343" if dark_mode else "#CBD5E1"
            fg = "#E2E8F0" if dark_mode else "#1E293B"
            btn_bg = "#232836" if dark_mode else "#E2E8F0"
        err_fg = "#FCA5A5" if dark_mode else "#B91C1C"
        muted = "#94A3B8" if dark_mode else "#475569"

        self.setStyleSheet(
            f"QFrame#CodeOutputPanel {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 8px; }}"
            f"QScrollArea {{ background-color: transparent; }}"
            f"QLabel {{ color: {fg}; font-family: Consolas, monospace; font-size: 11px; }}"
            f"QPushButton {{ background-color: {btn_bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 4px; padding: 2px 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ background-color: #4F46E5; color: #FFFFFF; }}"
        )
        self._title_lbl.setStyleSheet(f"color: {muted}; font-family: 'Segoe UI', sans-serif; font-weight: 600; font-size: 11px;")
        self._err_lbl.setStyleSheet(f"color: {err_fg}; font-family: Consolas, monospace; font-size: 11px;")

    def set_result(self, stdout: str, stderr: str, plot_paths: List[str], elapsed: float, ok: bool):
        self._plot_paths = [p for p in (plot_paths or []) if os.path.exists(p)]
        status_txt = f"✓ {elapsed:.2f}s" if ok else f"✕ Errore ({elapsed:.2f}s)"
        self._title_lbl.setText(f"Output  •  {status_txt}")

        if stdout:
            self._out_lbl.setText(stdout)
            self._out_lbl.show()
        elif not stderr and not self._plot_paths:
            self._out_lbl.setText("(Nessun output)")
            self._out_lbl.show()
        else:
            self._out_lbl.hide()

        if stderr:
            self._err_lbl.setText(stderr)
            self._err_lbl.show()
        else:
            self._err_lbl.hide()

        while self._plots_lay.count():
            item = self._plots_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for p_path in self._plot_paths:
            pix = QPixmap(p_path)
            if not pix.isNull():
                lbl = QLabel()
                lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
                scaled = pix.scaledToWidth(min(480, pix.width()), Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(scaled)
                self._plots_lay.addWidget(lbl)

        self._plots_container.setVisible(bool(self._plot_paths))
        self._insert_plot_btn.setVisible(bool(self._plot_paths))
        self._copy_text = "\n".join(x for x in (stdout, stderr) if x).strip()

        self._body_widget.adjustSize()
        content_h = self._body_widget.sizeHint().height() + 44
        self.setFixedHeight(max(68, min(280, content_h)))

    def _on_copy(self):
        if self._copy_text:
            QApplication.clipboard().setText(self._copy_text)
            self._copy_btn.setText("✓ Copiato")
            QTimer.singleShot(1400, lambda: self._copy_btn.setText("📋 Copia"))

    def _on_insert_plot(self):
        if self._plot_paths:
            self.insertPlotRequested.emit(self.code_id, self._plot_paths[0])


class LatexSourceEditDialog(QDialog):
    """Dialog tematizzato per modificare il codice LaTeX di una formula inline."""

    def __init__(self, initial_tex: str, dark_mode: bool = True, reading_mode: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Modifica Formula LaTeX")
        self.setMinimumWidth(440)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        if reading_mode:
            bg = "#1E1712" if dark_mode else "#FBF5E9"
            card_bg = "#2A2018" if dark_mode else "#F3E8D2"
            bd = "#44372D" if dark_mode else "#DCC9A5"
            fg = "#F8FAFC" if dark_mode else "#1C1917"
            muted = "#CBD5E1" if dark_mode else "#57534E"
            btn_bg = "#33281F" if dark_mode else "#E5D3B3"
        else:
            bg = "#141824" if dark_mode else "#FFFFFF"
            card_bg = "#1E2433" if dark_mode else "#F8FAFC"
            bd = "#334155" if dark_mode else "#CBD5E1"
            fg = "#F8FAFC" if dark_mode else "#0F172A"
            muted = "#94A3B8" if dark_mode else "#475569"
            btn_bg = "#1E293B" if dark_mode else "#E2E8F0"

        self.setStyleSheet(
            f"QDialog {{ background-color: {bg}; color: {fg}; }}"
            f"QLabel {{ color: {muted}; font-size: 12px; }}"
            f"QTextEdit {{ background-color: {card_bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 6px; padding: 8px; font-family: Consolas, monospace; font-size: 13px; }}"
            f"QTextEdit:focus {{ border: 1px solid #6366F1; }}"
            f"QPushButton {{ background-color: {btn_bg}; color: {fg}; border: 1px solid {bd}; "
            f"border-radius: 6px; padding: 6px 14px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: #475569; color: #FFFFFF; }}"
            f"QPushButton#OkBtn {{ background-color: #4F46E5; color: #FFFFFF; border: 1px solid #4338CA; }}"
            f"QPushButton#OkBtn:hover {{ background-color: #4338CA; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        lbl = QLabel("Codice LaTeX (es. E = mc^2, \\frac{a}{b}):")
        lay.addWidget(lbl)

        self._edit = QTextEdit()
        self._edit.setAcceptRichText(False)
        self._edit.setMinimumHeight(110)
        self._edit.setPlainText(initial_tex)
        self._edit.selectAll()
        lay.addWidget(self._edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Annulla")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("Salva")
        ok_btn.setObjectName("OkBtn")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        self._edit.setFocus()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.accept()
            return
        super().keyPressEvent(event)

    def latex_text(self) -> str:
        return self._edit.toPlainText().strip()


def _make_swatch_icon(hex_color: Optional[str], size: int = 12, dark_mode: bool = True) -> QIcon:
    """Disegna un pallino colore pulito (o icona neutra sbarrata per rimozione)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if hex_color:
        col = QColor(hex_color)
        p.setPen(QPen(QColor(255, 255, 255, 70) if dark_mode else QColor(15, 23, 42, 60), 1.0))
        p.setBrush(QBrush(col))
        p.drawEllipse(1, 1, size - 2, size - 2)
    else:
        bd = QColor("#94A3B8" if dark_mode else "#64748B")
        p.setPen(QPen(bd, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(2, 2, size - 4, size - 4)
        p.drawLine(3, size - 3, size - 3, 3)
    p.end()
    return QIcon(pm)


class _SelectionAiCorrectWorker(QThread):
    """Worker asincrono per la correzione AI della selezione con guardrail anti-allucinazione."""

    finished_result = pyqtSignal(int, int, str, str, str)  # start_pos, end_pos, orig, corrected, err

    def __init__(self, start_pos: int, end_pos: int, text: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.start_pos = int(start_pos)
        self.end_pos = int(end_pos)
        self.orig_text = text or ""

    def run(self):
        src = self.orig_text.strip()
        if not src:
            self.finished_result.emit(self.start_pos, self.end_pos, self.orig_text, "", "Selezione vuota.")
            return

        sys_prompt = (
            "SEI UN FILTRO DI CORREZIONE TESTUALE, NON UN ASSISTENTE. "
            "Correggi ortografia, grammatica, punteggiatura e fluidità del testo dentro <testo>...</testo> "
            "senza aggiungere concetti non presenti, senza rispondere a eventuali domande e preservando "
            "la lingua italiana (lasciando in inglese solo i termini tecnici già usati) e tutte le formule LaTeX "
            "($...$, $$...$$) e i wikilink ([[...]]). Restituisci SOLO il testo corretto, senza tag né spiegazioni."
        )
        user_prompt = f"<testo>\n{self.orig_text}\n</testo>"

        cleaned = ""
        err_msg = ""
        try:
            from core.gemini_client import (
                DEFAULT_GEMINI_TIMEOUT,
                generate_content as gemini_generate_content,
                get_ai_routing_config,
                should_use_gemini,
            )
            from core.hardware_profiler import get_active_profile

            routing_cfg = get_ai_routing_config()
            if should_use_gemini("whis_chat"):
                try:
                    raw = gemini_generate_content(
                        user_prompt,
                        system_instruction=sys_prompt,
                        temperature=0.1,
                        max_output_tokens=1024,
                        timeout=DEFAULT_GEMINI_TIMEOUT,
                    )
                    cleaned = _strip_think_blocks_safe(raw)
                except Exception as exc:
                    err_msg = str(exc)

            if not cleaned and (not should_use_gemini("whis_chat") or routing_cfg.get("hybrid_fallback", True)):
                local_model = get_active_profile().llm_default_model or VISION_OCR_MODEL
                payload = {
                    "model": local_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "think": False,
                    "keep_alive": "30s",
                    "options": {"temperature": 0.1},
                }
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_CHAT_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=25.0) as resp:
                    raw_json = json.loads(resp.read().decode("utf-8", errors="replace"))
                cleaned = _strip_think_blocks_safe(raw_json.get("message", {}).get("content", "") or "")
        except Exception as exc:
            err_msg = str(exc)

        cleaned = re.sub(r"^<testo>\s*|\s*</testo>$", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            self.finished_result.emit(
                self.start_pos,
                self.end_pos,
                self.orig_text,
                "",
                err_msg or "Nessun risultato dall'AI locale/cloud.",
            )
            return

        max_allowed = max(int(len(self.orig_text) * 1.45), len(self.orig_text) + 40)
        if len(cleaned) > max_allowed:
            self.finished_result.emit(
                self.start_pos,
                self.end_pos,
                self.orig_text,
                "",
                "Risposta AI scartata dal guardrail anti-allucinazione.",
            )
            return

        self.finished_result.emit(self.start_pos, self.end_pos, self.orig_text, cleaned, "")


class _LinkNotePickerPopup(QFrame):
    """Popup compatto con ricerca per collegare il testo selezionato a un'altra nota ([[Titolo]])."""

    noteChosen = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self.setObjectName("LinkNotePickerPopup")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setFixedWidth(310)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self._search_edit = QLineEdit(self)
        self._search_edit.setMinimumHeight(34)
        self._search_edit.setPlaceholderText("Cerca nota da collegare...")
        self._search_edit.textChanged.connect(self._on_query_changed)
        self._search_edit.returnPressed.connect(self._accept_current)
        lay.addWidget(self._search_edit)

        self._list = QListWidget(self)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setMinimumHeight(140)
        self._list.setMaximumHeight(220)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)
        lay.addWidget(self._list)

        self._search_edit.installEventFilter(self)
        self._list.installEventFilter(self)
        self.apply_theme(dark_mode)

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
        if dark_mode:
            bg, fg, bd, in_bg, sel_bg, sel_fg = "#161B26", "#F1F5F9", "#2D3748", "#0F141E", "#312E81", "#FFFFFF"
        else:
            bg, fg, bd, in_bg, sel_bg, sel_fg = "#FFFFFF", "#0F172A", "#CBD5E1", "#F8FAFC", "#E0E7FF", "#1E1B4B"
        self.setStyleSheet(
            f"QFrame#LinkNotePickerPopup {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 10px; }}"
            f"QLineEdit {{ background-color: {in_bg}; color: {fg}; border: 1px solid {bd}; border-radius: 6px; padding: 4px 8px; font-size: 12px; }}"
            f"QLineEdit:focus {{ border-color: #6366F1; }}"
            f"QListWidget {{ background-color: transparent; color: {fg}; border: none; font-size: 12px; }}"
            f"QListWidget::item {{ padding: 6px 8px; border-radius: 5px; }}"
            f"QListWidget::item:selected {{ background-color: {sel_bg}; color: {sel_fg}; }}"
        )

    def open_for_query(self, initial_query: str = ""):
        clean = (initial_query or "").strip()
        if len(clean) > 48 or "\n" in clean:
            clean = ""
        self._search_edit.blockSignals(True)
        self._search_edit.setText(clean)
        self._search_edit.blockSignals(False)
        count = self._populate_notes(clean)
        if count == 0 and clean:
            self._search_edit.blockSignals(True)
            self._search_edit.setText("")
            self._search_edit.blockSignals(False)
            self._populate_notes("")
        self.adjustSize()
        self.show()
        self.raise_()
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def _populate_notes(self, query: str) -> int:
        from database.db_manager import db
        self._list.clear()
        try:
            rows = db.search_note_titles_for_wikilink(query, limit=14)
        except Exception:
            rows = []
        for r in rows:
            raw_t = strip_markdown_syntax((r.get("title") or "").replace("Nota: ", "").strip())
            if not raw_t:
                continue
            subj = (r.get("subject") or "Generale").strip()
            label = f"🔗 {raw_t}   ·   {subj}" if subj and subj != "Generale" else f"🔗 {raw_t}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, raw_t)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)
        return self._list.count()

    def _on_query_changed(self, text: str):
        self._populate_notes(text)

    def _accept_current(self):
        it = self._list.currentItem()
        if it is not None:
            title = str(it.data(Qt.ItemDataRole.UserRole) or "")
            if title:
                self.hide()
                self.noteChosen.emit(title)

    def _on_item_clicked(self, item: QListWidgetItem):
        title = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if title:
            self.hide()
            self.noteChosen.emit(title)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            k = event.key()
            if k == Qt.Key.Key_Escape:
                self.hide()
                return True
            if watched is self._search_edit and k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                cur = self._list.currentRow()
                nxt = min(self._list.count() - 1, cur + 1) if k == Qt.Key.Key_Down else max(0, cur - 1)
                if self._list.count() > 0:
                    self._list.setCurrentRow(nxt)
                return True
        return super().eventFilter(watched, event)


class _SelectionFloatingToolbar(QFrame):
    """Toolbar fluttuante contestuale che appare sopra il testo selezionato."""

    boldClicked = pyqtSignal()
    highlightClicked = pyqtSignal(str)
    colorClicked = pyqtSignal(str)
    aiCorrectClicked = pyqtSignal()
    askWhisClicked = pyqtSignal()
    linkNoteClicked = pyqtSignal()
    rsvpClicked = pyqtSignal()
    captureSelectionRequested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True):
        super().__init__(parent)
        self._dark_mode = dark_mode
        self.setObjectName("SelectionFloatingToolbar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)

        def _make_tool_btn(text: str, tip: str, bold_font: bool = False) -> QPushButton:
            b = QPushButton(text, self)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            if bold_font:
                b.setProperty("boldBtn", True)
            return b

        self._bold_btn = _make_tool_btn("B", "Grassetto (Ctrl+B)", bold_font=True)
        self._bold_btn.clicked.connect(self.boldClicked.emit)
        lay.addWidget(self._bold_btn)

        self._highlight_btn = _make_tool_btn("🖍 ▾", "Evidenzia testo")
        self._highlight_btn.clicked.connect(self._open_highlight_menu)
        lay.addWidget(self._highlight_btn)

        self._color_btn = _make_tool_btn("A ▾", "Colore testo")
        self._color_btn.clicked.connect(self._open_color_menu)
        lay.addWidget(self._color_btn)

        self._sep1 = QFrame(self)
        self._sep1.setFixedSize(1, 18)
        lay.addWidget(self._sep1)

        self._ai_btn = _make_tool_btn("✦ Correggi", "Correggi ortografia e chiarezza con AI")
        self._ai_btn.clicked.connect(self.aiCorrectClicked.emit)
        lay.addWidget(self._ai_btn)

        self._whis_btn = _make_tool_btn("✦ Whis", "Chiedi a Whis sul testo selezionato")
        self._whis_btn.clicked.connect(self.askWhisClicked.emit)
        lay.addWidget(self._whis_btn)

        self._link_btn = _make_tool_btn("🔗 Collega", "Collega ad un'altra nota ([[...]])")
        self._link_btn.clicked.connect(self.linkNoteClicked.emit)
        lay.addWidget(self._link_btn)

        self._sep2 = QFrame(self)
        self._sep2.setFixedSize(1, 18)
        lay.addWidget(self._sep2)

        self._rsvp_btn = _make_tool_btn("⚡ RSVP", "Lettura veloce RSVP sulla selezione (Alt+R)")
        self._rsvp_btn.clicked.connect(self.rsvpClicked.emit)
        lay.addWidget(self._rsvp_btn)

        self._all_btns = [
            self._bold_btn,
            self._highlight_btn,
            self._color_btn,
            self._ai_btn,
            self._whis_btn,
            self._link_btn,
            self._rsvp_btn,
        ]
        self.apply_theme(dark_mode)
        self.adjustSize()

    def _menu_stylesheet(self) -> str:
        if self._dark_mode:
            bg, fg, bd, hv = "#161B26", "#F1F5F9", "#2D3748", "#312E81"
        else:
            bg, fg, bd, hv = "#FFFFFF", "#0F172A", "#CBD5E1", "#E0E7FF"
        return (
            f"QMenu {{ background-color: {bg}; color: {fg}; border: 1px solid {bd}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 5px 12px; border-radius: 5px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {hv}; }}"
            f"QMenu::separator {{ height: 1px; background-color: {bd}; margin: 4px 6px; }}"
        )

    def _open_highlight_menu(self):
        self.captureSelectionRequested.emit()
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        for name, hex_c in HIGHLIGHT_COLORS:
            act = menu.addAction(_make_swatch_icon(hex_c, 12, self._dark_mode), name)
            act.triggered.connect(lambda _chk=False, h=hex_c: self.highlightClicked.emit(h))
        menu.addSeparator()
        rem_act = menu.addAction(_make_swatch_icon(None, 12, self._dark_mode), "Rimuovi evidenziazione")
        rem_act.triggered.connect(lambda: self.highlightClicked.emit(""))
        menu.exec(self._highlight_btn.mapToGlobal(QPoint(0, self._highlight_btn.height() + 4)))

    def _open_color_menu(self):
        self.captureSelectionRequested.emit()
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        for name, hex_c in TEXT_COLORS:
            act = menu.addAction(_make_swatch_icon(hex_c, 12, self._dark_mode), name)
            act.triggered.connect(lambda _chk=False, h=hex_c: self.colorClicked.emit(h))
        menu.addSeparator()
        rem_act = menu.addAction(_make_swatch_icon(None, 12, self._dark_mode), "Colore predefinito")
        rem_act.triggered.connect(lambda: self.colorClicked.emit(""))
        menu.exec(self._color_btn.mapToGlobal(QPoint(0, self._color_btn.height() + 4)))

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
        if dark_mode:
            bg = "rgba(18, 24, 38, 0.96)"
            bd = "rgba(129, 140, 248, 0.35)"
            fg = "#E2E8F0"
            hv = "rgba(129, 140, 248, 0.25)"
            sep = "rgba(148, 163, 184, 0.22)"
        else:
            bg = "rgba(255, 255, 255, 0.96)"
            bd = "rgba(79, 70, 229, 0.30)"
            fg = "#0F172A"
            hv = "rgba(79, 70, 229, 0.14)"
            sep = "rgba(100, 116, 139, 0.22)"
        self.setStyleSheet(
            f"QFrame#SelectionFloatingToolbar {{ background-color: {bg}; border: 1px solid {bd}; border-radius: 9px; }}"
        )
        self._sep1.setStyleSheet(f"background-color: {sep}; border: none;")
        self._sep2.setStyleSheet(f"background-color: {sep}; border: none;")
        btn_qss = (
            f"QPushButton {{ color: {fg}; background: transparent; border: none; border-radius: 6px; "
            f"padding: 4px 8px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {hv}; }}"
        )
        for btn in self._all_btns:
            if btn.property("boldBtn"):
                btn.setStyleSheet(
                    f"QPushButton {{ color: {fg}; background: transparent; border: none; border-radius: 6px; "
                    f"padding: 4px 9px; font-size: 11.5px; font-weight: 800; }}"
                    f"QPushButton:hover {{ background-color: {hv}; }}"
                )
            else:
                btn.setStyleSheet(btn_qss)
        self.adjustSize()


class ScientificNoteEditor(QTextEdit):
    """
    Editor WYSIWYG a Blocchi Reali in stile Notion:
    - Tabelle interattive QTextTable (Tab tra celle, auto-riga su ultima cella)
    - Blocchi Quote con barra verticale sinistra continua (3px) in paintEvent
    - Blocchi To-Do List interattivi (☐ / ☑) cliccabili con strikeout
    - Blocchi Code / BibTeX in QTextFrame a larghezza piena con card arrotondata
    - Blocco Upload or embed a file (/file, /immagine) + Ctrl+V / Drag & Drop
    - Modalità Vista Studio per rendering LaTeX/Matrici ad alta risoluzione
    """

    summarizeRequested = pyqtSignal()
    flashcardsRequested = pyqtSignal()
    studioViewChanged = pyqtSignal(bool)
    markdownModified = pyqtSignal()
    seekRequested = pyqtSignal(int)
    togglePlayRequested = pyqtSignal()
    rsvpRequested = pyqtSignal(int, bool)  # (start_block_idx, selection_only)
    sendSelectionToSplitRequested = pyqtSignal(str)
    askWhisSelectionRequested = pyqtSignal(str)
    wikilinkClicked = pyqtSignal(str)  # titolo nota cliccato con Ctrl+Click su [[Titolo]]

    def __init__(self, parent: Optional[QWidget] = None, dark_mode: bool = True, reading_mode: bool = False):
        super().__init__(parent)
        self._dark_mode: bool = dark_mode
        self._reading_mode: bool = reading_mode
        self._font_family_css: str = "'Segoe UI', sans-serif"
        self._font_scale_pct: int = 100
        self._raw_markdown: str = ""
        self._is_studio_view: bool = False
        self._internal_update: bool = False
        self._slash_start_pos: Optional[int] = None
        self._wikilink_start_pos: Optional[int] = None

        # Interactive blocks, inline matrix editor & inline calc ghost-text state
        self._block_action_rects: List[Tuple[QRectF, str, Any]] = []
        self._hovered_action_idx: int = -1
        self._hovered_matrix_pos: Optional[int] = None
        self._inline_matrix_editor: Optional[InlineMatrixEditor] = None
        self._inline_calc_suggestion: Optional[Dict[str, Any]] = None

        # Audio-Text Sync state
        self._audio_sync_active: bool = False
        self._sync_segments: List[Dict] = []
        self._sync_spans: List = []
        self._sync_dirty: bool = False
        self._sync_accent_color: QColor = QColor("#6366F1")
        self._active_highlight_span = None
        self._auto_scroll_paused: bool = False

        # Blocco 3B: Vision OCR, AI Selection Correct & Inline Python Execution state
        self._active_ocr_workers: List[_VisionOcrWorker] = []
        self._active_exec_workers: List[_PythonExecWorker] = []
        self._bg_workers: List[QThread] = []
        self._saved_selection_range: Optional[Tuple[int, int]] = None
        self._pending_link_range: Optional[Tuple[int, int]] = None
        self._pending_link_text: str = ""
        self._code_exec_states: Dict[str, Dict[str, Any]] = {}
        self._code_output_panels: Dict[str, CodeOutputPanel] = {}
        self._toast_label: Optional[QLabel] = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_discrete_toast)

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAcceptRichText(False)
        self.document().setDocumentMargin(12 if reading_mode else 4)
        self.highlighter = MarkdownLatexHighlighter(self.document(), dark_mode=dark_mode)

        self.slash_popup = SlashCommandPopup(self, dark_mode=dark_mode)
        self.slash_popup.commandChosen.connect(self._apply_slash_command)
        self.slash_popup.hide()

        self.wikilink_popup = WikilinkPopup(self, dark_mode=dark_mode)
        self.wikilink_popup.noteChosen.connect(self._apply_wikilink)
        self.wikilink_popup.hide()

        self._link_picker_popup = _LinkNotePickerPopup(self, dark_mode=dark_mode)
        self._link_picker_popup.noteChosen.connect(self._on_link_picker_note_chosen)
        self._link_picker_popup.hide()

        self._selection_toolbar = _SelectionFloatingToolbar(self.viewport(), dark_mode=dark_mode)
        self._selection_toolbar.captureSelectionRequested.connect(self._capture_current_selection_range)
        self._selection_toolbar.boldClicked.connect(self.toggle_bold_selection)
        self._selection_toolbar.highlightClicked.connect(self._on_selection_highlight_clicked)
        self._selection_toolbar.colorClicked.connect(self._on_selection_color_clicked)
        self._selection_toolbar.aiCorrectClicked.connect(self._on_selection_ai_correct_clicked)
        self._selection_toolbar.askWhisClicked.connect(self._on_selection_whis_clicked)
        self._selection_toolbar.linkNoteClicked.connect(self._on_selection_link_note_clicked)
        self._selection_toolbar.rsvpClicked.connect(self._on_selection_rsvp_clicked)
        self._selection_toolbar.hide()

        self.textChanged.connect(self._on_internal_text_changed)
        self.cursorPositionChanged.connect(self._on_cursor_pos_changed)
        self.selectionChanged.connect(self._on_selection_changed_for_toolbar)
        sig = dictation_tracker.signals
        if sig is not None:
            sig.historyUpdated.connect(self.viewport().update, Qt.ConnectionType.QueuedConnection)

    @property
    def is_studio_view(self) -> bool:
        return self._is_studio_view

    def set_typography(self, font_family_css: str, font_scale_pct: int = 100):
        font_family_css = font_family_css or "'Segoe UI', sans-serif"
        font_scale_pct = max(80, min(150, int(font_scale_pct or 100)))
        if self._font_family_css == font_family_css and self._font_scale_pct == font_scale_pct:
            return
        self._font_family_css = font_family_css
        self._font_scale_pct = font_scale_pct
        self.highlighter.set_font_scale(font_scale_pct)
        # Apply primary font family and scaled base size to QTextEdit document
        primary_family = font_family_css.split(",")[0].strip().strip("'").strip('"')
        f = self.font()
        if primary_family:
            f.setFamily(primary_family)
        f.setPointSizeF(round(11.0 * (font_scale_pct / 100.0), 1))
        self.setFont(f)
        self.document().setDefaultFont(f)
        cur_md = self.get_markdown_text()
        self.set_markdown_text(cur_md)
        self.viewport().update()

    def set_theme_modes(self, dark_mode: bool, reading_mode: bool):
        if self._dark_mode == dark_mode and self._reading_mode == reading_mode:
            return
        self._dark_mode = dark_mode
        self._reading_mode = reading_mode
        self.document().setDocumentMargin(12 if reading_mode else 4)
        self.highlighter.set_dark_mode(dark_mode)
        self.slash_popup.apply_theme(dark_mode)
        self.wikilink_popup.apply_theme(dark_mode)
        self._link_picker_popup.apply_theme(dark_mode)
        self._selection_toolbar.apply_theme(dark_mode)
        for panel in self._code_output_panels.values():
            panel.apply_theme(dark_mode, reading_mode)
        # Aggiorna bordi tabelle e colori dei blocchi esistenti
        cur_md = self.get_markdown_text()
        self.set_markdown_text(cur_md)
        self.viewport().update()

    def set_dark_mode(self, dark_mode: bool):
        self.set_theme_modes(dark_mode, self._reading_mode)

    def set_reading_mode(self, reading_mode: bool):
        self.set_theme_modes(self._dark_mode, reading_mode)

    def _on_internal_text_changed(self):
        if self._internal_update or self._is_studio_view:
            return
        if self._audio_sync_active:
            self._sync_dirty = True
        self._raw_markdown = self._serialize_document_to_markdown()
        self.markdownModified.emit()

    # -----------------------------------------------------------------
    # SERIALIZZAZIONE WYSIWYG <-> MARKDOWN
    # -----------------------------------------------------------------
    def _serialize_document_to_markdown(self) -> str:
        """Serializza il QTextDocument (inclusi QTextTable, QTextFrame, Quote, To-Do, File) in Markdown."""
        root = self.document().rootFrame()
        lines: List[str] = []
        it = root.begin()
        while not it.atEnd():
            child_frame = it.currentFrame()
            if child_frame is not None:
                if isinstance(child_frame, QTextTable):
                    lines.extend(self._serialize_table_to_md(child_frame))
                elif child_frame.frameFormat().hasProperty(PROP_FRAME_KIND):
                    lang = child_frame.frameFormat().property(PROP_FRAME_KIND) or "python"
                    code_lines = []
                    fit = child_frame.begin()
                    while not fit.atEnd():
                        fb = fit.currentBlock()
                        if fb.isValid():
                            code_lines.append(fb.text())
                        fit += 1
                    lines.append(f"```{lang}")
                    lines.extend(code_lines)
                    lines.append("```")
            else:
                block = it.currentBlock()
                if block.isValid():
                    lines.append(self._serialize_block_to_md(block))
            it += 1

        # Rimuove eventuali segnaposto immagine \ufffc
        md = "\n".join(lines)
        md = re.sub(r"\n?\ufffc\n?", "\n", md).replace("\ufffc", "")
        return md

    def _serialize_table_to_md(self, table: QTextTable) -> List[str]:
        rows_md: List[str] = []
        nrows = table.rows()
        ncols = table.columns()
        for r in range(nrows):
            cells = []
            for c in range(ncols):
                cell = table.cellAt(r, c)
                txt = cell.firstCursorPosition().block().text().replace("|", "\\|").strip()
                cells.append(txt)
            rows_md.append("| " + " | ".join(cells) + " |")
            if r == 0:
                rows_md.append("| " + " | ".join(["---"] * ncols) + " |")
        return rows_md

    def _serialize_block_to_md(self, block) -> str:
        kind = block.blockFormat().property(PROP_BLOCK_KIND)
        # Controlla se il blocco contiene immagini LaTeX renderizzate (PROP_LATEX_SOURCE)
        # e serializza tutti gli stili inline WYSIWYG coalescendo frammenti contigui identici.
        parts: List[str] = []
        run_txt: List[str] = []
        run_key: Tuple[bool, bool, bool, bool, str, int, str, str] = (False, False, False, False, "", 0, "", "")

        def _flush_run():
            if not run_txt:
                return
            chunk = "".join(run_txt)
            run_txt.clear()
            r_b, r_i, r_u, r_s, r_sc, r_fsz, r_hl, r_col = run_key
            if r_u:
                chunk = f"<u>{chunk}</u>"
            if r_b and r_i:
                chunk = f"***{chunk}***"
            elif r_b:
                chunk = f"**{chunk}**"
            elif r_i:
                chunk = f"*{chunk}*"
            if r_s:
                chunk = f"~~{chunk}~~"
            if r_sc in ("sub", "sup"):
                chunk = f"<{r_sc}>{chunk}</{r_sc}>"
            if 1 <= r_fsz <= 100:
                chunk = f'<span style="font-size:{r_fsz}pt">{chunk}</span>'
            if r_col:
                chunk = f'<span style="color:{r_col}">{chunk}</span>'
            if r_hl:
                if r_hl.upper() == "#FDE047" and "==" not in chunk:
                    chunk = f"=={chunk}=="
                else:
                    chunk = f'<mark data-color="{r_hl}">{chunk}</mark>'
            parts.append(chunk)

        fit = block.begin()
        while not fit.atEnd():
            frag = fit.fragment()
            if frag.isValid():
                cf = frag.charFormat()
                if cf.hasProperty(PROP_LATEX_SOURCE):
                    _flush_run()
                    run_key = (False, False, False, False, "", 0, "", "")
                    tex_src = str(cf.property(PROP_LATEX_SOURCE) or "").strip()
                    if tex_src:
                        parts.append(f"$$\n{tex_src}\n$$")
                else:
                    t = frag.text().replace("\ufffc", "")
                    if t:
                        hl_hex = str(cf.property(PROP_INLINE_HIGHLIGHT) or "").strip()
                        col_hex = str(cf.property(PROP_INLINE_COLOR) or "").strip()
                        fsz_val = int(cf.property(PROP_INLINE_FONT_SIZE) or 0)
                        if not (1 <= fsz_val <= 100):
                            fsz_val = 0
                        f_key = (
                            bool(cf.property(PROP_INLINE_BOLD)),
                            bool(cf.property(PROP_INLINE_ITALIC)),
                            bool(cf.property(PROP_INLINE_UNDERLINE)),
                            bool(cf.property(PROP_INLINE_STRIKE)),
                            str(cf.property(PROP_INLINE_SCRIPT) or "").strip().lower(),
                            fsz_val,
                            hl_hex,
                            col_hex,
                        )
                        if run_txt and f_key != run_key:
                            _flush_run()
                        run_key = f_key
                        run_txt.append(t)
            fit += 1
        _flush_run()
        txt = "".join(parts)

        if kind == "h1":
            return f"# {txt}"
        if kind == "h2":
            return f"## {txt}"
        if kind == "h3":
            return f"### {txt}"
        if kind == "h4":
            return f"#### {txt}"
        if kind == "ai_note":
            ai_title = str(block.blockFormat().property(PROP_AI_NOTE_TITLE) or "Nota AI").strip()
            ai_body = str(block.blockFormat().property(PROP_AI_NOTE_BODY) or "").strip()
            return f":::ai-note {ai_title}\n{ai_body}\n:::"
        if kind == "callout":
            return f"> [!NOTE] {txt}" if txt else "> [!NOTE]"
        if kind == "quote":
            return f"> {txt}"
        if kind == "bullet":
            indent_lvl = max(0, min(4, int(block.blockFormat().property(PROP_BULLET_INDENT) or 0)))
            return f"{'  ' * indent_lvl}- {txt}"
        if kind == "todo":
            checked = bool(block.blockFormat().property(PROP_TODO_CHECKED))
            indent_lvl = int(block.blockFormat().property(PROP_TODO_INDENT) or 0)
            indent_lvl = max(0, min(4, indent_lvl))
            clean_txt = txt
            if clean_txt.startswith("☑ "):
                checked = True
                clean_txt = clean_txt[2:]
            elif clean_txt.startswith("☐ "):
                clean_txt = clean_txt[2:]
            prefix = "  " * indent_lvl
            mark = "x" if checked else " "
            return f"{prefix}- [{mark}] {clean_txt}"
        if kind == "file":
            fpath = block.blockFormat().property(PROP_FILE_PATH) or ""
            if fpath:
                fname = os.path.basename(fpath)
                return f"[📄 {fname}]({fpath})"
            return "> 📤 Upload or embed a file"
        return txt

    def get_markdown_text(self) -> str:
        if self._is_studio_view:
            return self._raw_markdown
        self._raw_markdown = self._serialize_document_to_markdown()
        return self._raw_markdown

    def set_markdown_text(self, text: str):
        """Popola il documento WYSIWYG (o Vista Studio) a partire dal testo Markdown."""
        self._raw_markdown = text or ""
        self._internal_update = True
        try:
            if self._is_studio_view:
                self.setHtml(
                    markdown_to_studio_html(
                        self._raw_markdown,
                        BASE_DATA_DIR,
                        dark_mode=self._dark_mode,
                        reading_mode=self._reading_mode,
                        font_family_css=self._font_family_css,
                        font_scale_pct=self._font_scale_pct,
                    )
                )
            else:
                self._populate_wysiwyg_document(self._raw_markdown)
        finally:
            self._internal_update = False

    def _populate_wysiwyg_document(self, md_text: str):
        """Ricostruisce blocchi reali Notion (QTextTable, QTextFrame, Quote, To-Do, File, Immagini) da Markdown."""
        self.clear()
        cursor = self.textCursor()
        lines = (md_text or "").splitlines()
        i = 0
        first_block = True

        def _default_block_format() -> QTextBlockFormat:
            bf = QTextBlockFormat()
            if self._reading_mode:
                bf.setLineHeight(135.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
                bf.setBottomMargin(4)
            return bf

        def _ensure_new_block():
            nonlocal first_block
            if not first_block:
                cursor.insertBlock()
            cursor.setBlockFormat(_default_block_format())
            cursor.setCharFormat(QTextCharFormat())
            first_block = False

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 1. Blocco Code / BibTeX -> QTextFrame card arrotondata
            if stripped.startswith("```"):
                lang = stripped[3:].strip().lower() or "python"
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                if not first_block:
                    cursor.insertBlock()
                    cursor.setBlockFormat(QTextBlockFormat())
                first_block = False
                self._insert_code_frame_at_cursor(cursor, "\n".join(code_lines), lang=lang, add_trailing_block=False)
                # Sposta il cursore dopo il frame per i blocchi successivi
                cur_f = cursor.currentFrame()
                if cur_f is not None and cur_f != self.document().rootFrame():
                    cursor = cur_f.lastCursorPosition()
                    cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
                i += 1
                continue

            # 1b. Blocco Nota AI collassabile (:::ai-note Titolo ... :::)
            if stripped.startswith(":::ai-note"):
                ai_title = stripped[len(":::ai-note"):].strip() or "Sintesi AI"
                ai_lines = []
                i += 1
                while i < len(lines) and lines[i].strip() != ":::":
                    ai_lines.append(lines[i])
                    i += 1
                if i < len(lines) and lines[i].strip() == ":::":
                    i += 1
                _ensure_new_block()
                self._apply_ai_note_block_at_cursor(
                    cursor,
                    title=ai_title,
                    body="\n".join(ai_lines).strip(),
                    expanded=False,
                )
                continue

            # 2. Tabella Markdown -> Vera QTextTable interattiva
            if stripped.startswith("|") and "|" in stripped[1:]:
                table_rows: List[List[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    raw_row = lines[i].strip().strip("|")
                    cells = [c.strip() for c in raw_row.split("|")]
                    if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                        table_rows.append(cells)
                    i += 1
                if table_rows:
                    if not first_block:
                        cursor.insertBlock()
                        cursor.setBlockFormat(QTextBlockFormat())
                    first_block = False
                    self._insert_table_at_cursor(cursor, data=table_rows)
                    tbl = cursor.currentTable()
                    if tbl is not None:
                        cursor = tbl.lastCursorPosition()
                        cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
                continue

            # 3. Blocco File link [📄 nome](path)
            file_m = re.match(r"^\[📄\s*(.+?)\]\((.+?)\)\s*$", stripped)
            if file_m:
                _ensure_new_block()
                self._apply_file_block_at_cursor(cursor, file_m.group(2).strip())
                i += 1
                continue

            # 4. Placeholder Upload or embed a file
            if stripped == "> 📤 Upload or embed a file":
                _ensure_new_block()
                self._apply_file_block_at_cursor(cursor, "")
                i += 1
                continue

            # 4b. Blocco Callout (> [!NOTE] ...)
            callout_m = re.match(r"^>\s*\[!(?:NOTE|CALLOUT|TIP|IMPORTANT)\]\s*(.*)$", stripped, re.IGNORECASE)
            if callout_m:
                _ensure_new_block()
                self._apply_callout_block_at_cursor(cursor, callout_m.group(1).strip())
                i += 1
                continue

            # 5. Blocco Quote (> ...)
            if line.startswith("> "):
                _ensure_new_block()
                self._apply_quote_block_at_cursor(cursor, line[2:])
                i += 1
                continue

            # 5b. Blocco Matematico $$ ... $$ con matrice o formula renderizzata inline
            if stripped.startswith("$$"):
                closed = False
                expr = ""
                next_i = i + 1
                if stripped.endswith("$$") and len(stripped) > 4:
                    expr = stripped[2:-2].strip()
                    closed = True
                else:
                    math_lines = [stripped[2:]]
                    j = i + 1
                    while j < len(lines):
                        cur_l = lines[j].strip()
                        if cur_l.endswith("$$"):
                            math_lines.append(cur_l[:-2])
                            j += 1
                            closed = True
                            break
                        math_lines.append(lines[j])
                        j += 1
                    if closed:
                        expr = "\n".join(math_lines).strip()
                        next_i = j
                if closed and expr:
                    _ensure_new_block()
                    self._insert_latex_block_at_cursor(cursor, expr)
                    i = next_i
                    continue
                if closed and not expr:
                    i = next_i
                    continue

            # 5c. Titoli WYSIWYG (#, ##, ###, #### senza simboli # visibili)
            h_match = re.match(r"^(#{1,4})\s+(.*)$", line)
            if h_match:
                h_level = len(h_match.group(1))
                h_text = h_match.group(2).strip()
                if h_text.startswith("**") and h_text.endswith("**") and len(h_text) > 4:
                    h_text = h_text[2:-2].strip()
                _ensure_new_block()
                self._apply_heading_block_at_cursor(cursor, level=h_level, text=h_text)
                i += 1
                continue

            # 6. Blocco To-Do (- [ ] o - [x], con supporto indentazione annidata)
            todo_m = re.match(r"^(\s*)[-*]\s+\[([ xX])\]\s*(.*)$", line)
            if todo_m:
                _ensure_new_block()
                indent_lvl = min(4, len(todo_m.group(1).replace("\t", "  ")) // 2)
                checked = (todo_m.group(2).lower() == "x")
                self._apply_todo_block_at_cursor(cursor, todo_m.group(3), checked=checked, indent_level=indent_lvl)
                i += 1
                continue

            # 6b. Blocco Elenco Puntato (- testo / * testo, escludendo divisori --- / ***)
            if not re.match(r"^(?:-{3,}|\*{3,}|_{3,})$", stripped):
                bullet_m = re.match(r"^(\s*)[-*•]\s+(.*)$", line)
                if bullet_m:
                    _ensure_new_block()
                    indent_lvl = min(4, len(bullet_m.group(1).replace("\t", "  ")) // 2)
                    self._apply_bullet_block_at_cursor(cursor, bullet_m.group(2), indent_level=indent_lvl)
                    i += 1
                    continue

            # 7. Testo normale + eventuale anteprima immagine inline
            _ensure_new_block()
            self._insert_inline_formatted_text(cursor, line)
            img_m = re.match(r"^!\[(.*?)\]\((.+?)\)\s*$", stripped)
            if img_m:
                rel_or_abs = img_m.group(2).strip()
                abs_p = (
                    rel_or_abs
                    if os.path.isabs(rel_or_abs)
                    else os.path.abspath(os.path.join(BASE_DATA_DIR, rel_or_abs))
                )
                if os.path.exists(abs_p):
                    qimg = QImage(abs_p)
                    if not qimg.isNull():
                        cursor.insertText("\n")
                        self._insert_scaled_qimage_at_cursor(cursor, qimg, abs_p)
            i += 1

        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)

    # -----------------------------------------------------------------
    # COSTRUTTORI BLOCCHI WYSIWYG NOTION
    # -----------------------------------------------------------------
    def _insert_table_at_cursor(
        self,
        cursor: QTextCursor,
        rows: int = 3,
        cols: int = 3,
        data: Optional[List[List[str]]] = None,
    ) -> QTextTable:
        if data:
            rows = max(1, len(data))
            cols = max(1, max(len(r) for r in data))

        border_hex = "#2F2F2F" if self._dark_mode else "#E5E7EB"
        t_fmt = QTextTableFormat()
        t_fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        t_fmt.setBorder(1.0)
        t_fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        t_fmt.setBorderBrush(QBrush(QColor(border_hex)))
        t_fmt.setCellPadding(10)
        t_fmt.setCellSpacing(0)
        t_fmt.setBorderCollapse(True)
        t_fmt.setTopMargin(10)
        t_fmt.setBottomMargin(10)

        table = cursor.insertTable(rows, cols, t_fmt)
        if data:
            for r_idx, row in enumerate(data):
                for c_idx, val in enumerate(row):
                    cell = table.cellAt(r_idx, c_idx)
                    c_cur = cell.firstCursorPosition()
                    if r_idx == 0:
                        hdr_fmt = QTextCharFormat()
                        hdr_fmt.setFontWeight(QFont.Weight.DemiBold)
                        c_cur.setCharFormat(hdr_fmt)
                    c_cur.insertText(val)
        return table

    def _heading_char_format(self, level: int = 1) -> QTextCharFormat:
        scale = max(0.8, min(1.5, self._font_scale_pct / 100.0))
        size_map = {1: 15.5, 2: 13.5, 3: 12.0, 4: 11.5}
        pt_size = size_map.get(level, 11.5)
        cf = QTextCharFormat()
        cf.setFontWeight(QFont.Weight.Bold if level <= 2 else QFont.Weight.DemiBold)
        cf.setFontPointSize(round(pt_size * scale, 1))
        cf.setForeground(QColor("#F8FAFC" if self._dark_mode else "#0F172A"))
        return cf

    def _apply_heading_block_at_cursor(self, cursor: QTextCursor, level: int = 1, text: str = ""):
        bf = QTextBlockFormat()
        bf.setTopMargin(12 if level == 1 else (9 if level == 2 else 6))
        bf.setBottomMargin(5 if level == 1 else 3)
        kind = f"h{level}"
        bf.setProperty(PROP_BLOCK_KIND, kind)
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData(kind))

        cf = self._heading_char_format(level)
        cursor.setBlockCharFormat(cf)
        cursor.setCharFormat(cf)
        if text:
            cursor.insertText(text, cf)

    def _insert_latex_block_at_cursor(self, cursor: QTextCursor, tex_expr: str):
        """Renderizza immediatamente una matrice/formula LaTeX come immagine inline modificabile con doppio clic."""
        clean = (tex_expr or "").strip()
        if not clean:
            return
        qimg = None
        if re.search(r"\\begin\{[bpv]matrix\}", clean):
            mat_res = _render_matrix_png_b64(clean, dark_mode=self._dark_mode, reading_mode=self._reading_mode)
            if mat_res is not None:
                b64, w_px, h_px = mat_res
                qimg = QImage.fromData(base64.b64decode(b64), "PNG")
        else:
            try:
                ink_hex = _editor_ink_hex(self._dark_mode, self._reading_mode)
                qimg = _mathtext_to_transparent_qimage(clean, dpi=190, ink_hex=ink_hex)
                if qimg is not None and not qimg.isNull():
                    w_px = max(20, int(qimg.width() * 0.68))
                    h_px = max(20, int(qimg.height() * 0.68))
            except Exception:
                qimg = None

        if qimg is None or qimg.isNull():
            cursor.insertText(f"$$\n{clean}\n$$")
            return

        res_name = f"latex://{abs(hash((clean, self._dark_mode, self._reading_mode)))}"
        self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(res_name), qimg)
        fmt = QTextImageFormat()
        fmt.setName(res_name)
        fmt.setWidth(w_px)
        fmt.setHeight(h_px)
        fmt.setProperty(PROP_LATEX_SOURCE, clean)
        fmt.setToolTip("Doppio clic per modificare la matrice / formula")
        cursor.insertImage(fmt)

    _INLINE_WYSIWYG_RE = re.compile(
        r'<mark(?:\s+data-color="(#[0-9A-Fa-f]{6})")?>(.*?)</mark>'
        r"|==([^=\n]+?)=="
        r'|<span\s+style="color:\s*(#[0-9A-Fa-f]{6})\s*;?">(.*?)</span>'
        r"|<(u|sub|sup)>(.*?)</\6>"
        r"|~~([^~\n]+?)~~"
        r"|\*\*\*([^*\n]+?)\*\*\*"
        r"|\*\*([^*\n]+?)\*\*"
        r"|(?<!\*)\*([^*\n]+?)\*(?!\*)"
        r'|<span\s+style="font-size:\s*(\d{1,3})pt\s*;?">(.*?)</span>',
        re.DOTALL | re.IGNORECASE,
    )
    _ATOMIC_INLINE_RE = re.compile(r"(\$\$.*?\$\$|\$[^$\n]+?\$|`[^`\n]+?`|\[\[[^\]\n]+\]\])")

    @staticmethod
    def _parse_inline_markup_runs(
        raw_text: str,
        cur_style: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Analizza Markdown/HTML inline in run WYSIWYG proteggendo LaTeX, codice inline e wikilink."""
        st: Dict[str, Any] = {
            "bold": False,
            "italic": False,
            "underline": False,
            "strike": False,
            "script": "",
            "font_size": 0,
            "hl": "",
            "col": "",
        }
        if cur_style:
            st.update(cur_style)

        runs: List[Tuple[str, Dict[str, Any]]] = []
        if not raw_text:
            return runs

        def _parse_segment(seg: str, seg_style: Dict[str, Any]):
            pos = 0
            for m in ScientificNoteEditor._INLINE_WYSIWYG_RE.finditer(seg):
                if m.start() > pos:
                    runs.append((seg[pos:m.start()], dict(seg_style)))
                child_st = dict(seg_style)
                inner = ""
                if m.group(2) is not None:
                    child_st["hl"] = (m.group(1) or "#FDE047").upper()
                    inner = m.group(2)
                elif m.group(3) is not None:
                    child_st["hl"] = "#FDE047"
                    inner = m.group(3)
                elif m.group(4) is not None and m.group(5) is not None:
                    child_st["col"] = m.group(4).upper()
                    inner = m.group(5)
                elif m.group(6) is not None and m.group(7) is not None:
                    tag = m.group(6).lower()
                    if tag == "u":
                        child_st["underline"] = True
                    else:
                        child_st["script"] = tag
                    inner = m.group(7)
                elif m.group(8) is not None:
                    child_st["strike"] = True
                    inner = m.group(8)
                elif m.group(9) is not None:
                    child_st["bold"] = True
                    child_st["italic"] = True
                    inner = m.group(9)
                elif m.group(10) is not None:
                    child_st["bold"] = True
                    inner = m.group(10)
                elif m.group(11) is not None:
                    child_st["italic"] = True
                    inner = m.group(11)
                elif m.group(12) is not None and m.group(13) is not None:
                    child_st["font_size"] = max(1, min(100, int(m.group(12))))
                    inner = m.group(13)
                if inner:
                    _parse_segment(inner, child_st)
                pos = m.end()
            if pos < len(seg):
                runs.append((seg[pos:], dict(seg_style)))

        # Proteggi token atomici ($...$, `...`, [[...]]) da formattazione * / ** / ~~
        tokens = ScientificNoteEditor._ATOMIC_INLINE_RE.split(raw_text)
        for tok in tokens:
            if not tok:
                continue
            if ScientificNoteEditor._ATOMIC_INLINE_RE.fullmatch(tok):
                runs.append((tok, dict(st)))
            else:
                _parse_segment(tok, st)
        return runs

    def _apply_inline_visuals_to_format(
        self,
        cf: QTextCharFormat,
        preserve_block_bold: bool = False,
    ):
        """Applica sul QTextCharFormat i veri attributi visivi Qt delle proprietà inline."""
        is_b = bool(cf.property(PROP_INLINE_BOLD))
        is_i = bool(cf.property(PROP_INLINE_ITALIC))
        is_u = bool(cf.property(PROP_INLINE_UNDERLINE))
        is_s = bool(cf.property(PROP_INLINE_STRIKE))
        script = str(cf.property(PROP_INLINE_SCRIPT) or "").strip().lower()
        fsz = int(cf.property(PROP_INLINE_FONT_SIZE) or 0)
        hl_hex = str(cf.property(PROP_INLINE_HIGHLIGHT) or "").strip()
        col_hex = str(cf.property(PROP_INLINE_COLOR) or "").strip()

        if is_b:
            cf.setFontWeight(QFont.Weight.Bold)
        elif not preserve_block_bold:
            cf.setFontWeight(QFont.Weight.Normal)

        cf.setFontItalic(is_i)
        cf.setFontUnderline(is_u)
        cf.setFontStrikeOut(is_s)

        if script == "sub":
            cf.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSubScript)
        elif script == "sup":
            cf.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
        else:
            cf.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignNormal)

        if 1 <= fsz <= 100:
            cf.setFontPointSize(float(fsz))
        elif not preserve_block_bold:
            cf.clearProperty(QTextFormat.Property.FontPointSize)

        if hl_hex:
            cf.setBackground(_highlight_brush_for_hex(hl_hex, self._dark_mode))
        if col_hex:
            cf.setForeground(QColor(col_hex))

    def _insert_inline_formatted_text(
        self,
        cursor: QTextCursor,
        text: str,
        base_fmt: Optional[QTextCharFormat] = None,
    ):
        base = QTextCharFormat(base_fmt) if base_fmt is not None else QTextCharFormat()
        if not text:
            cursor.setCharFormat(base)
            return
        preserve_bold = base.fontWeight() >= QFont.Weight.DemiBold
        for chunk, st in self._parse_inline_markup_runs(text):
            if not chunk:
                continue
            cf = QTextCharFormat(base)
            if st.get("hl"):
                cf.setProperty(PROP_INLINE_HIGHLIGHT, st["hl"])
            if st.get("col"):
                cf.setProperty(PROP_INLINE_COLOR, st["col"])
            if st.get("bold"):
                cf.setProperty(PROP_INLINE_BOLD, True)
            if st.get("italic"):
                cf.setProperty(PROP_INLINE_ITALIC, True)
            if st.get("underline"):
                cf.setProperty(PROP_INLINE_UNDERLINE, True)
            if st.get("strike"):
                cf.setProperty(PROP_INLINE_STRIKE, True)
            if st.get("script") in ("sub", "sup"):
                cf.setProperty(PROP_INLINE_SCRIPT, st["script"])
            fsz = int(st.get("font_size") or 0)
            if 1 <= fsz <= 100:
                cf.setProperty(PROP_INLINE_FONT_SIZE, fsz)
            self._apply_inline_visuals_to_format(cf, preserve_block_bold=preserve_bold)
            cursor.insertText(chunk, cf)
        cursor.setCharFormat(base)

    def _apply_quote_block_at_cursor(self, cursor: QTextCursor, text: str = ""):
        bf = QTextBlockFormat()
        bf.setLeftMargin(18)
        bf.setTopMargin(6)
        bf.setBottomMargin(6)
        bf.setProperty(PROP_BLOCK_KIND, "quote")
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("quote"))

        cf = QTextCharFormat()
        cf.setForeground(QColor("#E2E8F0" if self._dark_mode else "#334155"))
        cursor.setCharFormat(cf)
        if text:
            self._insert_inline_formatted_text(cursor, text, base_fmt=cf)

    def _apply_callout_block_at_cursor(self, cursor: QTextCursor, text: str = ""):
        bf = QTextBlockFormat()
        bf.setLeftMargin(34)
        bf.setRightMargin(28)
        bf.setTopMargin(8)
        bf.setBottomMargin(8)
        bf.setProperty(PROP_BLOCK_KIND, "callout")
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("callout"))

        cf = QTextCharFormat()
        cf.setForeground(QColor("#E2E8F0" if self._dark_mode else "#1E293B"))
        cursor.setCharFormat(cf)
        if text:
            self._insert_inline_formatted_text(cursor, text, base_fmt=cf)

    def _apply_ai_note_block_at_cursor(
        self,
        cursor: QTextCursor,
        title: str = "Sintesi AI",
        body: str = "",
        expanded: bool = False,
    ):
        bf = QTextBlockFormat()
        bf.setTopMargin(7)
        bf.setBottomMargin(7)
        bf.setLeftMargin(14)
        bf.setRightMargin(14)
        bf.setProperty(PROP_BLOCK_KIND, "ai_note")
        bf.setProperty(PROP_AI_NOTE_TITLE, title or "Sintesi AI")
        bf.setProperty(PROP_AI_NOTE_BODY, body or "")
        bf.setProperty(PROP_AI_NOTE_EXPANDED, bool(expanded))
        cursor.setBlockFormat(bf)

        scale = max(0.8, min(1.5, self._font_scale_pct / 100.0))
        hdr_cf = QTextCharFormat()
        hdr_cf.setFontWeight(QFont.Weight.DemiBold)
        hdr_cf.setFontPointSize(round(10.0 * scale, 1))
        hdr_cf.setForeground(QColor(self._sync_accent_color))

        clean_title = (title or "Sintesi AI").strip()
        if expanded and body:
            cursor.insertText(f"▾ 💡 Nota AI · {clean_title}\u2028", hdr_cf)
            body_cf = QTextCharFormat()
            body_cf.setFontPointSize(round(10.0 * scale, 1))
            body_cf.setForeground(QColor("#CBD5E1" if self._dark_mode else "#334155"))
            cursor.insertText(body.strip().replace("\n", "\u2028"), body_cf)
        else:
            cursor.insertText(f"▸ 💡 Nota AI · {clean_title}  (clicca per espandere)", hdr_cf)

    def _toggle_ai_note_block(self, block):
        bf = block.blockFormat()
        title = str(bf.property(PROP_AI_NOTE_TITLE) or "Sintesi AI")
        body = str(bf.property(PROP_AI_NOTE_BODY) or "")
        expanded = not bool(bf.property(PROP_AI_NOTE_EXPANDED))

        self._internal_update = True
        try:
            cur = QTextCursor(block)
            cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            self._apply_ai_note_block_at_cursor(cur, title=title, body=body, expanded=expanded)
        finally:
            self._internal_update = False
        self.viewport().update()

    def _todo_left_margin(self, indent_level: int = 0) -> float:
        lvl = max(0, min(4, int(indent_level or 0)))
        return 30.0 + lvl * 22.0

    def _apply_todo_block_at_cursor(
        self,
        cursor: QTextCursor,
        text: str = "",
        checked: bool = False,
        indent_level: int = 0,
    ):
        lvl = max(0, min(4, int(indent_level or 0)))
        bf = QTextBlockFormat()
        bf.setTopMargin(4)
        bf.setBottomMargin(4)
        bf.setLeftMargin(self._todo_left_margin(lvl))
        bf.setProperty(PROP_BLOCK_KIND, "todo")
        bf.setProperty(PROP_TODO_CHECKED, bool(checked))
        bf.setProperty(PROP_TODO_INDENT, lvl)
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("todo"))

        txt_fmt = QTextCharFormat()
        if checked:
            txt_fmt.setFontStrikeOut(True)
            txt_fmt.setForeground(QColor("#64748B" if self._dark_mode else "#9CA3AF"))
        else:
            txt_fmt.setFontStrikeOut(False)
            txt_fmt.clearForeground()
        cursor.setBlockCharFormat(txt_fmt)
        cursor.setCharFormat(txt_fmt)
        if text:
            if checked:
                cursor.insertText(text, txt_fmt)
            else:
                self._insert_inline_formatted_text(cursor, text, base_fmt=txt_fmt)
        elif not checked and cursor.block().text():
            # Se il blocco appena creato contiene testo splittato da un to-do barrato, ripulisce lo strikeout
            clean_c = QTextCursor(cursor.block())
            clean_c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            clean_c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            clean_c.setCharFormat(txt_fmt)

    def _bullet_left_margin(self, indent_level: int = 0) -> float:
        lvl = max(0, min(4, int(indent_level or 0)))
        return 28.0 + lvl * 22.0

    def _apply_bullet_block_at_cursor(
        self,
        cursor: QTextCursor,
        text: str = "",
        indent_level: int = 0,
    ):
        lvl = max(0, min(4, int(indent_level or 0)))
        bf = QTextBlockFormat()
        bf.setTopMargin(3)
        bf.setBottomMargin(3)
        bf.setLeftMargin(self._bullet_left_margin(lvl))
        bf.setProperty(PROP_BLOCK_KIND, "bullet")
        bf.setProperty(PROP_BULLET_INDENT, lvl)
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("bullet"))

        clean_cf = QTextCharFormat()
        clean_cf.setFontStrikeOut(False)
        clean_cf.clearForeground()
        cursor.setBlockCharFormat(clean_cf)
        cursor.setCharFormat(clean_cf)
        if text:
            self._insert_inline_formatted_text(cursor, text, base_fmt=clean_cf)

    def _set_bullet_indent(self, block, delta: int) -> bool:
        """Aumenta o riduce il livello di indentazione (0..4) di una voce elenco."""
        if not block.isValid() or block.blockFormat().property(PROP_BLOCK_KIND) != "bullet":
            return False
        bf = block.blockFormat()
        cur_lvl = int(bf.property(PROP_BULLET_INDENT) or 0)
        new_lvl = max(0, min(4, cur_lvl + int(delta)))
        if new_lvl == cur_lvl:
            return False
        bf.setProperty(PROP_BULLET_INDENT, new_lvl)
        bf.setLeftMargin(self._bullet_left_margin(new_lvl))
        self._internal_update = True
        try:
            c = QTextCursor(block)
            c.setBlockFormat(bf)
        finally:
            self._internal_update = False
        self.viewport().update()
        self._on_internal_text_changed()
        return True

    def toggle_bullet_list(self):
        """Attiva/disattiva l'elenco puntato sul blocco corrente o sui blocchi selezionati."""
        if self._is_studio_view:
            return
        cur = self.textCursor()
        doc = self.document()
        s_pos = min(cur.selectionStart(), cur.selectionEnd())
        e_pos = max(cur.selectionStart(), cur.selectionEnd())
        start_blk = doc.findBlock(s_pos)
        end_blk = doc.findBlock(max(s_pos, e_pos - 1 if e_pos > s_pos else e_pos))
        if not start_blk.isValid():
            return

        blocks = []
        b = start_blk
        while b.isValid():
            blocks.append(b)
            if b == end_blk:
                break
            b = b.next()

        all_bullets = all(blk.blockFormat().property(PROP_BLOCK_KIND) == "bullet" for blk in blocks)
        cur.beginEditBlock()
        for blk in blocks:
            bc = QTextCursor(blk)
            if all_bullets:
                bc.setBlockFormat(QTextBlockFormat())
                bc.setBlockCharFormat(QTextCharFormat())
                blk.setUserData(None)
            else:
                self._apply_bullet_block_at_cursor(bc, text="", indent_level=0)
        cur.endEditBlock()
        self.viewport().update()
        self._on_internal_text_changed()

    def _resolve_selection_range(self, sel_range: Optional[Tuple[int, int]] = None) -> Optional[Tuple[int, int]]:
        if sel_range is not None:
            s, e = int(sel_range[0]), int(sel_range[1])
            if e > s:
                return (s, e)
        cur = self.textCursor()
        if cur.hasSelection() and cur.selectedText().strip():
            return (min(cur.selectionStart(), cur.selectionEnd()), max(cur.selectionStart(), cur.selectionEnd()))
        if self._saved_selection_range is not None:
            s, e = self._saved_selection_range
            doc_max = max(0, self.document().characterCount() - 1)
            s = max(0, min(s, doc_max))
            e = max(0, min(e, doc_max))
            if e > s:
                return (s, e)
        return None

    def _toggle_inline_bool_property(
        self,
        prop_id: int,
        sel_range: Optional[Tuple[int, int]] = None,
    ):
        """Attiva/disattiva una proprietà inline WYSIWYG sulla selezione o sul cursore corrente."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            cf = QTextCharFormat(self.currentCharFormat())
            new_state = not bool(cf.property(prop_id))
            if new_state:
                cf.setProperty(prop_id, True)
            else:
                cf.clearProperty(prop_id)
            blk_kind = str(self.textCursor().block().blockFormat().property(PROP_BLOCK_KIND) or "")
            self._apply_inline_visuals_to_format(cf, preserve_block_bold=blk_kind in ("h1", "h2", "h3", "h4"))
            self.setCurrentCharFormat(cf)
            return

        s_pos, e_pos = rng
        all_on = self._range_has_uniform_bool_property(s_pos, e_pos, prop_id)
        self._set_inline_char_property_on_range(s_pos, e_pos, prop_id, not all_on)
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self._saved_selection_range = (s_pos, e_pos)
        self._rehighlight_pos_range(s_pos, e_pos)
        self._on_internal_text_changed()

    def _toggle_inline_script(
        self,
        script_kind: str,
        sel_range: Optional[Tuple[int, int]] = None,
    ):
        """Attiva/disattiva pedice ('sub') o apice ('sup') in modalità WYSIWYG mutualmente esclusiva."""
        if self._is_studio_view:
            return
        target = script_kind.strip().lower()
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            cf = QTextCharFormat(self.currentCharFormat())
            cur_val = str(cf.property(PROP_INLINE_SCRIPT) or "").strip().lower()
            if cur_val == target:
                cf.clearProperty(PROP_INLINE_SCRIPT)
            else:
                cf.setProperty(PROP_INLINE_SCRIPT, target)
            blk_kind = str(self.textCursor().block().blockFormat().property(PROP_BLOCK_KIND) or "")
            self._apply_inline_visuals_to_format(cf, preserve_block_bold=blk_kind in ("h1", "h2", "h3", "h4"))
            self.setCurrentCharFormat(cf)
            return

        s_pos, e_pos = rng
        already = self._range_has_uniform_inline_property(s_pos, e_pos, PROP_INLINE_SCRIPT, target)
        new_val = "" if already else target
        self._set_inline_char_property_on_range(s_pos, e_pos, PROP_INLINE_SCRIPT, new_val)
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self._saved_selection_range = (s_pos, e_pos)
        self._rehighlight_pos_range(s_pos, e_pos)
        self._on_internal_text_changed()

    def _rehighlight_pos_range(self, s_pos: int, e_pos: int):
        doc = self.document()
        blk = doc.findBlock(s_pos)
        while blk.isValid() and blk.position() <= e_pos:
            self.highlighter.rehighlightBlock(blk)
            blk = blk.next()

    def toggle_bold_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva il grassetto WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_bool_property(PROP_INLINE_BOLD, sel_range=sel_range)

    def toggle_italic_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva il corsivo WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_bool_property(PROP_INLINE_ITALIC, sel_range=sel_range)

    def toggle_underline_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva il sottolineato WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_bool_property(PROP_INLINE_UNDERLINE, sel_range=sel_range)

    def toggle_strikethrough_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva il barrato WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_bool_property(PROP_INLINE_STRIKE, sel_range=sel_range)

    def toggle_subscript_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva il pedice WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_script("sub", sel_range=sel_range)

    def toggle_superscript_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Attiva o disattiva l'apice WYSIWYG sulla selezione o al cursore."""
        self._toggle_inline_script("sup", sel_range=sel_range)

    def default_body_font_size(self) -> int:
        return max(1, min(100, int(round(11.0 * (self._font_scale_pct / 100.0)))))

    def apply_font_size_selection(self, size_pt: Optional[int], sel_range: Optional[Tuple[int, int]] = None):
        """Applica una dimensione carattere (1..100 pt) solo al testo selezionato o al cursore corrente."""
        if self._is_studio_view:
            return
        clean_sz = 0 if (size_pt is None or int(size_pt) <= 0) else max(1, min(100, int(size_pt)))
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            cf = QTextCharFormat(self.currentCharFormat())
            if clean_sz > 0:
                cf.setProperty(PROP_INLINE_FONT_SIZE, clean_sz)
            else:
                cf.clearProperty(PROP_INLINE_FONT_SIZE)
            blk_kind = str(self.textCursor().block().blockFormat().property(PROP_BLOCK_KIND) or "")
            self._apply_inline_visuals_to_format(cf, preserve_block_bold=blk_kind in ("h1", "h2", "h3", "h4"))
            self.setCurrentCharFormat(cf)
            return

        s_pos, e_pos = rng
        self._set_inline_char_property_on_range(s_pos, e_pos, PROP_INLINE_FONT_SIZE, clean_sz)
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self._saved_selection_range = (s_pos, e_pos)
        self._rehighlight_pos_range(s_pos, e_pos)
        self._on_internal_text_changed()

    def step_font_size_selection(self, direction: int, sel_range: Optional[Tuple[int, int]] = None):
        """Aumenta o riduce la dimensione carattere del testo selezionato lungo la scala Word (1..100)."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range(sel_range)
        cur_sz = self.default_body_font_size()
        if rng is not None:
            s_pos, e_pos = rng
            blk = self.document().findBlock(s_pos)
            if blk.isValid():
                fit = blk.begin()
                while not fit.atEnd():
                    frag = fit.fragment()
                    if frag.isValid():
                        fs = max(s_pos, frag.position())
                        fe = min(e_pos, frag.position() + frag.length())
                        if fe > fs and frag.text().strip():
                            f_sz = int(frag.charFormat().property(PROP_INLINE_FONT_SIZE) or 0)
                            if 1 <= f_sz <= 100:
                                cur_sz = f_sz
                            elif frag.charFormat().fontPointSize() > 0:
                                cur_sz = max(1, min(100, int(round(frag.charFormat().fontPointSize()))))
                            break
                    fit += 1
        else:
            cur_sz = int(self.current_inline_format_state().get("font_size") or cur_sz)

        if direction > 0:
            larger = [s for s in WORD_FONT_SIZES if s > cur_sz]
            next_sz = larger[0] if larger else min(100, cur_sz + 2)
        else:
            smaller = [s for s in WORD_FONT_SIZES if s < cur_sz]
            next_sz = smaller[-1] if smaller else max(1, cur_sz - 1)

        target_val = 0 if next_sz == self.default_body_font_size() else next_sz
        self.apply_font_size_selection(target_val, sel_range=rng)

    def clear_inline_formatting_selection(self, sel_range: Optional[Tuple[int, int]] = None):
        """Rimuove tutte le formattazioni inline WYSIWYG (grassetto, corsivo, dimensione, colori, pedice/apice)."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            cf = QTextCharFormat(self.currentCharFormat())
            for prop in (
                PROP_INLINE_HIGHLIGHT,
                PROP_INLINE_COLOR,
                PROP_INLINE_BOLD,
                PROP_INLINE_ITALIC,
                PROP_INLINE_UNDERLINE,
                PROP_INLINE_STRIKE,
                PROP_INLINE_SCRIPT,
                PROP_INLINE_FONT_SIZE,
            ):
                cf.clearProperty(prop)
            cf.clearBackground()
            cf.clearForeground()
            self._apply_inline_visuals_to_format(cf, preserve_block_bold=False)
            self.setCurrentCharFormat(cf)
            return

        s_pos, e_pos = rng
        for prop_id, empty_val in (
            (PROP_INLINE_HIGHLIGHT, ""),
            (PROP_INLINE_COLOR, ""),
            (PROP_INLINE_BOLD, False),
            (PROP_INLINE_ITALIC, False),
            (PROP_INLINE_UNDERLINE, False),
            (PROP_INLINE_STRIKE, False),
            (PROP_INLINE_SCRIPT, ""),
            (PROP_INLINE_FONT_SIZE, 0),
        ):
            self._set_inline_char_property_on_range(s_pos, e_pos, prop_id, empty_val)

        # Pulisce anche eventuali marcatori Markdown/HTML incollati letteralmente
        doc = self.document()
        c = QTextCursor(doc)
        c.setPosition(s_pos)
        c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        raw_sel = c.selectedText()
        cleaned = raw_sel.replace("**", "")
        cleaned = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", cleaned)
        cleaned = re.sub(r"~~([^~\n]+?)~~", r"\1", cleaned)
        cleaned = re.sub(r"</?(?:u|sub|sup)>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<span\s+style=\"font-size:\s*\d{1,3}pt\s*;?\">|</span>", "", cleaned, flags=re.IGNORECASE)
        if cleaned != raw_sel:
            c.beginEditBlock()
            c.removeSelectedText()
            c.insertText(cleaned, QTextCharFormat())
            c.endEditBlock()
            new_e = s_pos + len(cleaned)
        else:
            new_e = e_pos

        sel_cur = QTextCursor(doc)
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(new_e, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self._saved_selection_range = (s_pos, new_e)
        self._rehighlight_pos_range(s_pos, new_e)
        self._on_internal_text_changed()

    def current_inline_format_state(self) -> Dict[str, Any]:
        """Restituisce lo stato degli stili inline alla selezione o al cursore per i toggle del Ribbon."""
        def_sz = self.default_body_font_size()
        out: Dict[str, Any] = {
            "bold": False,
            "italic": False,
            "underline": False,
            "strike": False,
            "script": "",
            "font_size": def_sz,
        }
        if self._is_studio_view:
            return out
        cur = self.textCursor()
        if cur.hasSelection() and cur.selectedText().strip():
            s_pos = min(cur.selectionStart(), cur.selectionEnd())
            e_pos = max(cur.selectionStart(), cur.selectionEnd())
            out["bold"] = self._range_has_uniform_bool_property(s_pos, e_pos, PROP_INLINE_BOLD)
            out["italic"] = self._range_has_uniform_bool_property(s_pos, e_pos, PROP_INLINE_ITALIC)
            out["underline"] = self._range_has_uniform_bool_property(s_pos, e_pos, PROP_INLINE_UNDERLINE)
            out["strike"] = self._range_has_uniform_bool_property(s_pos, e_pos, PROP_INLINE_STRIKE)
            if self._range_has_uniform_inline_property(s_pos, e_pos, PROP_INLINE_SCRIPT, "sub"):
                out["script"] = "sub"
            elif self._range_has_uniform_inline_property(s_pos, e_pos, PROP_INLINE_SCRIPT, "sup"):
                out["script"] = "sup"
            blk = self.document().findBlock(s_pos)
            if blk.isValid():
                fit = blk.begin()
                while not fit.atEnd():
                    frag = fit.fragment()
                    if frag.isValid():
                        fs = max(s_pos, frag.position())
                        fe = min(e_pos, frag.position() + frag.length())
                        if fe > fs and frag.text().strip():
                            f_sz = int(frag.charFormat().property(PROP_INLINE_FONT_SIZE) or 0)
                            if 1 <= f_sz <= 100:
                                out["font_size"] = f_sz
                            elif frag.charFormat().fontPointSize() > 0:
                                out["font_size"] = max(1, min(100, int(round(frag.charFormat().fontPointSize()))))
                            break
                    fit += 1
            return out

        cf = cur.charFormat() if cur.positionInBlock() > 0 or not cur.block().text() else self.currentCharFormat()
        out["bold"] = bool(cf.property(PROP_INLINE_BOLD))
        out["italic"] = bool(cf.property(PROP_INLINE_ITALIC))
        out["underline"] = bool(cf.property(PROP_INLINE_UNDERLINE))
        out["strike"] = bool(cf.property(PROP_INLINE_STRIKE))
        out["script"] = str(cf.property(PROP_INLINE_SCRIPT) or "").strip().lower()
        f_sz = int(cf.property(PROP_INLINE_FONT_SIZE) or 0)
        if 1 <= f_sz <= 100:
            out["font_size"] = f_sz
        elif cf.fontPointSize() > 0:
            out["font_size"] = max(1, min(100, int(round(cf.fontPointSize()))))
        return out

    def current_block_kind(self) -> str:
        """Restituisce il tipo di blocco corrente ('h1', 'h2', 'h3', 'bullet', 'todo', 'quote', 'callout', '')."""
        if self._is_studio_view:
            return ""
        blk = self.textCursor().block()
        if not blk.isValid():
            return ""
        return str(blk.blockFormat().property(PROP_BLOCK_KIND) or "")

    def _selected_blocks(self) -> List[Any]:
        cur = self.textCursor()
        doc = self.document()
        s_pos = min(cur.selectionStart(), cur.selectionEnd())
        e_pos = max(cur.selectionStart(), cur.selectionEnd())
        start_blk = doc.findBlock(s_pos)
        end_blk = doc.findBlock(max(s_pos, e_pos - 1 if e_pos > s_pos else e_pos))
        blocks = []
        b = start_blk
        while b.isValid():
            blocks.append(b)
            if b == end_blk:
                break
            b = b.next()
        return blocks

    def set_block_heading(self, level: int):
        """Imposta il blocco corrente come Normale (level=0) oppure H1..H3 preservando il testo."""
        if self._is_studio_view:
            return
        blk = self.textCursor().block()
        if not blk.isValid():
            return
        cur = self.textCursor()
        cur_pos_in_blk = cur.positionInBlock()
        cur.beginEditBlock()
        bc = QTextCursor(blk)
        if level <= 0:
            bc.setBlockFormat(QTextBlockFormat())
            clean_cf = QTextCharFormat()
            clean_cf.clearForeground()
            bc.setBlockCharFormat(clean_cf)
            bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            bc.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            bc.setCharFormat(clean_cf)
            blk.setUserData(None)
        else:
            lvl = max(1, min(4, int(level)))
            self._apply_heading_block_at_cursor(bc, level=lvl, text="")
            h_cf = self._heading_char_format(lvl)
            bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            bc.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            bc.mergeCharFormat(h_cf)
        cur.endEditBlock()
        restore = QTextCursor(blk)
        restore.setPosition(min(blk.position() + cur_pos_in_blk, blk.position() + len(blk.text())))
        self.setTextCursor(restore)
        self.highlighter.rehighlightBlock(blk)
        self.viewport().update()
        self._on_internal_text_changed()

    def _toggle_special_block_kind(self, target_kind: str):
        if self._is_studio_view:
            return
        blocks = self._selected_blocks()
        if not blocks:
            return
        all_target = all(blk.blockFormat().property(PROP_BLOCK_KIND) == target_kind for blk in blocks)
        cur = self.textCursor()
        cur.beginEditBlock()
        for blk in blocks:
            bc = QTextCursor(blk)
            if all_target:
                bc.setBlockFormat(QTextBlockFormat())
                clean_cf = QTextCharFormat()
                clean_cf.clearForeground()
                bc.setBlockCharFormat(clean_cf)
                bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                bc.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                bc.setCharFormat(clean_cf)
                blk.setUserData(None)
            elif target_kind == "todo":
                self._apply_todo_block_at_cursor(bc, text="", checked=False, indent_level=0)
            elif target_kind == "quote":
                self._apply_quote_block_at_cursor(bc, text="")
            elif target_kind == "callout":
                self._apply_callout_block_at_cursor(bc, text="")
        cur.endEditBlock()
        self.viewport().update()
        self._on_internal_text_changed()

    def toggle_todo_list(self):
        """Attiva/disattiva il blocco To-Do sui blocchi selezionati o correnti preservando il testo."""
        self._toggle_special_block_kind("todo")

    def toggle_quote_block(self):
        """Attiva/disattiva il blocco Citazione sui blocchi selezionati o correnti."""
        self._toggle_special_block_kind("quote")

    def toggle_callout_block(self):
        """Attiva/disattiva il blocco Callout sui blocchi selezionati o correnti."""
        self._toggle_special_block_kind("callout")

    def toggle_numbered_list(self):
        """Attiva/rimuove il prefisso numerato (1. , 2. , ...) sui blocchi selezionati o correnti."""
        if self._is_studio_view:
            return
        blocks = self._selected_blocks()
        if not blocks:
            return
        all_numbered = all(bool(re.match(r"^\s*\d+\.\s+", blk.text())) for blk in blocks)
        cur = self.textCursor()
        cur.beginEditBlock()
        for idx, blk in enumerate(blocks, 1):
            bc = QTextCursor(blk)
            txt = blk.text()
            if all_numbered:
                m = re.match(r"^(\s*\d+\.\s+)", txt)
                if m:
                    bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                    bc.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, len(m.group(1)))
                    bc.removeSelectedText()
            else:
                if blk.blockFormat().property(PROP_BLOCK_KIND) in ("bullet", "todo"):
                    bc.setBlockFormat(QTextBlockFormat())
                    blk.setUserData(None)
                if not re.match(r"^\s*\d+\.\s+", txt):
                    bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                    bc.insertText(f"{idx}. ", QTextCharFormat())
        cur.endEditBlock()
        self.viewport().update()
        self._on_internal_text_changed()

    def indent_current_block(self, delta: int):
        """Aumenta o riduce il rientro del blocco corrente (+1 / -1)."""
        if self._is_studio_view:
            return
        blk = self.textCursor().block()
        if not blk.isValid():
            return
        kind = blk.blockFormat().property(PROP_BLOCK_KIND)
        if kind == "bullet":
            self._set_bullet_indent(blk, delta)
            return
        if kind == "todo":
            self._set_todo_indent(blk, delta)
            return
        bf = blk.blockFormat()
        cur_m = float(bf.leftMargin() or 0.0)
        new_m = max(0.0, min(88.0, cur_m + (22.0 if delta > 0 else -22.0)))
        if abs(new_m - cur_m) < 0.1:
            return
        bf.setLeftMargin(new_m)
        c = QTextCursor(blk)
        c.beginEditBlock()
        c.setBlockFormat(bf)
        c.endEditBlock()
        self.viewport().update()
        self._on_internal_text_changed()

    def _popup_anchor_point(self, anchor_pos: Optional[QPoint] = None) -> QPoint:
        if anchor_pos is not None:
            return anchor_pos
        rect = self.cursorRect()
        return self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4))

    def insert_table_interactive(self, anchor_pos: Optional[QPoint] = None):
        """Apre il selettore griglia e inserisce una tabella interattiva alla posizione del cursore."""
        if self._is_studio_view:
            return
        picker = GridSizePickerPopup("Tabella", dark_mode=self._dark_mode, parent=self)
        picker.move(self._popup_anchor_point(anchor_pos))
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_size():
            r, c = picker.selected_size()
            cursor = self.textCursor()
            if cursor.block().text().strip() != "":
                cursor.insertBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                cursor.block().setUserData(None)
            tbl = self._insert_table_at_cursor(cursor, rows=r, cols=c)
            self.setTextCursor(tbl.cellAt(0, 0).firstCursorPosition())
            self._on_internal_text_changed()

    def insert_matrix_interactive(self, anchor_pos: Optional[QPoint] = None):
        """Apre il selettore dimensioni e inserisce una matrice LaTeX modificabile inline."""
        if self._is_studio_view:
            return
        picker = GridSizePickerPopup("Matrice", dark_mode=self._dark_mode, parent=self)
        picker.move(self._popup_anchor_point(anchor_pos))
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_size():
            r, c = picker.selected_size()
            rows_tex = [
                " & ".join("1" if (r == c and ri == ci) else "0" for ci in range(c))
                for ri in range(r)
            ]
            init_tex = "\\begin{bmatrix}\n" + " \\\\\n".join(rows_tex) + "\n\\end{bmatrix}"
            cursor = self.textCursor()
            if cursor.block().text().strip() != "":
                cursor.insertBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                cursor.block().setUserData(None)
            mat_pos = cursor.position()
            self._insert_latex_block_at_cursor(cursor, init_tex)
            self.setTextCursor(cursor)
            self._on_internal_text_changed()
            self._open_inline_matrix_editor(mat_pos, init_tex)

    def insert_formula_block(self):
        """Inserisce un blocco formula $$ ... $$ alla posizione del cursore."""
        if self._is_studio_view:
            return
        cursor = self.textCursor()
        if cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)
        cursor.insertText("$$\n  \n$$")
        cursor.movePosition(QTextCursor.MoveOperation.Up)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
        self.setTextCursor(cursor)

    def insert_image_interactive(self):
        """Apre il selettore file e incorpora un'immagine inline."""
        if self._is_studio_view:
            return
        self._open_file_picker_and_embed(target_block=None)

    def insert_file_block(self):
        """Inserisce un blocco allegato file (/file) alla posizione del cursore."""
        if self._is_studio_view:
            return
        cursor = self.textCursor()
        if cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)
        self._apply_file_block_at_cursor(cursor, "")
        self.setTextCursor(cursor)

    def insert_code_block(self, lang: str = "python"):
        """Inserisce un blocco codice a larghezza piena."""
        if self._is_studio_view:
            return
        cursor = self.textCursor()
        if cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)
        self._insert_code_frame_at_cursor(cursor, "", lang=lang)

    def insert_bibtex_block(self):
        """Inserisce un blocco citazione accademica BibTeX."""
        if self._is_studio_view:
            return
        cursor = self.textCursor()
        if cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)
        self._insert_code_frame_at_cursor(
            cursor,
            "@article{autore2026,\n"
            "  author  = {Cognome, Nome},\n"
            "  title   = {Titolo dell'articolo},\n"
            "  journal = {Rivista Scientifica},\n"
            "  year    = {2026}\n"
            "}",
            lang="bibtex",
        )

    def open_link_note_picker(self, anchor_pos: Optional[QPoint] = None):
        """Apre il selettore note [[Titolo]] dalla selezione corrente o al cursore."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range()
        if rng is not None:
            s_pos, e_pos = rng
            c = QTextCursor(self.document())
            c.setPosition(s_pos)
            c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
            sel_text = c.selectedText().replace("\u2029", " ").strip()
        else:
            pos = self.textCursor().position()
            s_pos, e_pos = pos, pos
            sel_text = ""

        self._pending_link_range = (s_pos, e_pos)
        self._pending_link_text = sel_text
        self._selection_toolbar.hide()

        global_pt = self._popup_anchor_point(anchor_pos)
        screen = QApplication.screenAt(global_pt) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pt.x() + self._link_picker_popup.width() > avail.right():
                global_pt.setX(max(avail.left(), avail.right() - self._link_picker_popup.width() - 8))
            if global_pt.y() + 260 > avail.bottom():
                above_pt = self.viewport().mapToGlobal(QPoint(self.cursorRect().left(), self.cursorRect().top()))
                global_pt.setY(max(avail.top(), above_pt.y() - 260))
        self._link_picker_popup.move(global_pt)
        self._link_picker_popup.open_for_query(sel_text)

    def trigger_selection_ai_correct(self) -> bool:
        """Avvia la correzione AI sulla selezione corrente. Ritorna False se nessuna selezione attiva."""
        if self._is_studio_view:
            return False
        if self._resolve_selection_range() is None:
            return False
        self._on_selection_ai_correct_clicked()
        return True

    def _set_inline_char_property_on_range(self, s_pos: int, e_pos: int, prop_id: int, value: Any):
        """Imposta o rimuove una proprietà inline WYSIWYG preservando tutte le altre proprietà."""
        doc = self.document()
        blk = doc.findBlock(s_pos)
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        while blk.isValid() and blk.position() < e_pos:
            blk_kind = str(blk.blockFormat().property(PROP_BLOCK_KIND) or "")
            preserve_bold = blk_kind in ("h1", "h2", "h3", "h4")
            slices: List[Tuple[int, int, QTextCharFormat]] = []
            fit = blk.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid():
                    fs = max(s_pos, frag.position())
                    fe = min(e_pos, frag.position() + frag.length())
                    if fe > fs:
                        cf = QTextCharFormat(frag.charFormat())
                        if prop_id == PROP_INLINE_HIGHLIGHT:
                            clean_val = str(value or "").strip().upper()
                            if clean_val:
                                cf.setProperty(PROP_INLINE_HIGHLIGHT, clean_val)
                            else:
                                cf.clearProperty(PROP_INLINE_HIGHLIGHT)
                                cf.clearBackground()
                        elif prop_id == PROP_INLINE_COLOR:
                            clean_val = str(value or "").strip().upper()
                            if clean_val:
                                cf.setProperty(PROP_INLINE_COLOR, clean_val)
                            else:
                                cf.clearProperty(PROP_INLINE_COLOR)
                                cf.clearForeground()
                        elif prop_id in (PROP_INLINE_BOLD, PROP_INLINE_ITALIC, PROP_INLINE_UNDERLINE, PROP_INLINE_STRIKE):
                            if bool(value):
                                cf.setProperty(prop_id, True)
                            else:
                                cf.clearProperty(prop_id)
                        elif prop_id == PROP_INLINE_SCRIPT:
                            clean_sc = str(value or "").strip().lower()
                            if clean_sc in ("sub", "sup"):
                                cf.setProperty(PROP_INLINE_SCRIPT, clean_sc)
                            else:
                                cf.clearProperty(PROP_INLINE_SCRIPT)
                        elif prop_id == PROP_INLINE_FONT_SIZE:
                            fsz_val = int(value or 0)
                            if 1 <= fsz_val <= 100:
                                cf.setProperty(PROP_INLINE_FONT_SIZE, fsz_val)
                            else:
                                cf.clearProperty(PROP_INLINE_FONT_SIZE)
                        self._apply_inline_visuals_to_format(cf, preserve_block_bold=preserve_bold)
                        slices.append((fs, fe, cf))
                fit += 1
            for fs, fe, cf in slices:
                sc = QTextCursor(doc)
                sc.setPosition(fs)
                sc.setPosition(fe, QTextCursor.MoveMode.KeepAnchor)
                sc.setCharFormat(cf)
            blk = blk.next()
        cur.endEditBlock()

    def _range_has_uniform_bool_property(self, s_pos: int, e_pos: int, prop_id: int) -> bool:
        doc = self.document()
        blk = doc.findBlock(s_pos)
        found_any = False
        while blk.isValid() and blk.position() < e_pos:
            fit = blk.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid():
                    fs = max(s_pos, frag.position())
                    fe = min(e_pos, frag.position() + frag.length())
                    if fe > fs and frag.text().strip():
                        found_any = True
                        if not bool(frag.charFormat().property(prop_id)):
                            return False
                fit += 1
            blk = blk.next()
        return found_any

    def _range_has_uniform_inline_property(self, s_pos: int, e_pos: int, prop_id: int, expected_val: str) -> bool:
        doc = self.document()
        blk = doc.findBlock(s_pos)
        found_any = False
        target = expected_val.strip().upper()
        while blk.isValid() and blk.position() < e_pos:
            fit = blk.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid():
                    fs = max(s_pos, frag.position())
                    fe = min(e_pos, frag.position() + frag.length())
                    if fe > fs and frag.text().strip():
                        found_any = True
                        cur_val = str(frag.charFormat().property(prop_id) or "").strip().upper()
                        if cur_val != target:
                            return False
                fit += 1
            blk = blk.next()
        return found_any

    def apply_highlight_selection(self, hex_color: Optional[str], sel_range: Optional[Tuple[int, int]] = None):
        """Applica o rimuove l'evidenziatore inline sul testo selezionato."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            return
        s_pos, e_pos = rng
        clean_hex = (hex_color or "").strip().upper()
        if clean_hex and self._range_has_uniform_inline_property(s_pos, e_pos, PROP_INLINE_HIGHLIGHT, clean_hex):
            clean_hex = ""
        self._set_inline_char_property_on_range(s_pos, e_pos, PROP_INLINE_HIGHLIGHT, clean_hex)
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self.highlighter.rehighlightBlock(self.document().findBlock(s_pos))
        blk = self.document().findBlock(s_pos).next()
        while blk.isValid() and blk.position() < e_pos:
            self.highlighter.rehighlightBlock(blk)
            blk = blk.next()
        self._on_internal_text_changed()

    def apply_text_color_selection(self, hex_color: Optional[str], sel_range: Optional[Tuple[int, int]] = None):
        """Applica o rimuove il colore del testo inline sulla selezione."""
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range(sel_range)
        if rng is None:
            return
        s_pos, e_pos = rng
        clean_hex = (hex_color or "").strip().upper()
        if clean_hex and self._range_has_uniform_inline_property(s_pos, e_pos, PROP_INLINE_COLOR, clean_hex):
            clean_hex = ""
        self._set_inline_char_property_on_range(s_pos, e_pos, PROP_INLINE_COLOR, clean_hex)
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        blk = self.document().findBlock(s_pos)
        while blk.isValid() and blk.position() < e_pos:
            self.highlighter.rehighlightBlock(blk)
            blk = blk.next()
        self._on_internal_text_changed()

    def _insert_code_frame_at_cursor(
        self,
        cursor: QTextCursor,
        code_text: str = "",
        lang: str = "python",
        add_trailing_block: bool = True,
    ) -> QTextFrame:
        f_fmt = QTextFrameFormat()
        f_fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        f_fmt.setPadding(16)
        f_fmt.setTopMargin(12)
        f_fmt.setBottomMargin(12)
        f_fmt.setBorder(0)
        f_fmt.setProperty(PROP_FRAME_KIND, lang or "python")
        f_fmt.setProperty(PROP_CODE_ID, uuid.uuid4().hex[:12])

        frame = cursor.insertFrame(f_fmt)
        inner_cur = frame.firstCursorPosition()
        scale = max(0.8, min(1.5, self._font_scale_pct / 100.0))
        cf = QTextCharFormat()
        cf.setFontFamily("Consolas")
        cf.setFontPointSize(round(10 * scale, 1))
        cf.setForeground(QColor("#E2E8F0" if self._dark_mode else "#1E293B"))
        inner_cur.setCharFormat(cf)
        if code_text:
            inner_cur.insertText(code_text, cf)

        if add_trailing_block:
            # Assicura che il cursore resti all'interno del blocco codice per iniziare a scrivere
            self.setTextCursor(inner_cur)
        return frame

    def _apply_file_block_at_cursor(self, cursor: QTextCursor, file_path: str = ""):
        bf = QTextBlockFormat()
        bf.setTopMargin(10)
        bf.setBottomMargin(10)
        bf.setLeftMargin(14)
        bf.setRightMargin(14)
        bf.setProperty(PROP_BLOCK_KIND, "file")
        bf.setProperty(PROP_FILE_PATH, file_path)
        cursor.setBlockFormat(bf)

        cf = QTextCharFormat()
        cf.setForeground(QColor("#94A3B8" if self._dark_mode else "#475569"))
        if file_path:
            fname = os.path.basename(file_path)
            cursor.insertText(f"📄 {fname}  (Clicca per aprire)", cf)
        else:
            cursor.insertText("📤 Upload or embed a file", cf)

    def _insert_scaled_qimage_at_cursor(self, cursor: QTextCursor, qimg: QImage, image_path: str):
        w = qimg.width()
        h = qimg.height()
        if w > MAX_INLINE_IMAGE_WIDTH and w > 0:
            ratio = MAX_INLINE_IMAGE_WIDTH / float(w)
            w = MAX_INLINE_IMAGE_WIDTH
            h = max(1, int(h * ratio))
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource,
            QUrl.fromLocalFile(image_path),
            qimg,
        )
        fmt = QTextImageFormat()
        fmt.setName(QUrl.fromLocalFile(image_path).toString())
        fmt.setWidth(w)
        fmt.setHeight(h)
        cursor.insertImage(fmt)

    # -----------------------------------------------------------------
    # PAINT EVENT: CARD, GHOST-TEXT PLACEHOLDERS, INLINE CALC & AZIONI BLOCCO
    # -----------------------------------------------------------------
    def _matrix_rect_in_viewport(self, frag_pos: int, img_w: float, img_h: float) -> QRectF:
        c = QTextCursor(self.document())
        c.setPosition(frag_pos)
        r = self.cursorRect(c)
        w = max(40.0, float(img_w or 80.0))
        h = max(24.0, float(img_h or 40.0))
        top = r.bottom() - h
        return QRectF(float(r.left()), float(top), w, h)

    def _draw_delete_badge(self, painter: QPainter, rect: QRectF, hovered: bool):
        bg = QColor("#EF4444" if hovered else ("#2A2A32" if self._dark_mode else "#E2E8F0"))
        bg.setAlpha(210 if hovered else 115)
        fg = QColor("#FFFFFF" if hovered else ("#CBD5E1" if self._dark_mode else "#475569"))
        fg.setAlpha(255 if hovered else 150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(rect, 4.0, 4.0)
        painter.setPen(QPen(fg))
        f = QFont(self.font())
        f.setPointSizeF(9.0)
        f.setWeight(QFont.Weight.Bold)
        painter.setFont(f)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "✕")

    def paintEvent(self, event):
        self._block_action_rects = []
        pending_delete_badges: List[Tuple[QRectF, str, Any]] = []
        pending_run_badges: List[Tuple[QRectF, QRectF, QTextFrame, str]] = []
        visible_code_panel_rects: Dict[str, QRectF] = {}
        vp_rect = self.viewport().rect()

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        doc_layout = self.document().documentLayout()
        hx = -self.horizontalScrollBar().value()
        vy = -self.verticalScrollBar().value()

        if self._reading_mode:
            card_bg = QColor("#241D17" if self._dark_mode else "#F3E8D2")
            card_bd = QColor("#3A2F26" if self._dark_mode else "#E2D2B5")
            quote_bar_color = QColor("#D97706" if self._dark_mode else "#B45309")
        else:
            card_bg = QColor("#202020" if self._dark_mode else "#F7F6F3")
            card_bd = QColor("#2F2F2F" if self._dark_mode else "#E5E7EB")
            quote_bar_color = QColor("#FFFFFF" if self._dark_mode else "#111827")

        # 1. Disegna card arrotondate (8px) per tutti i QTextFrame di tipo code/bibtex
        root = self.document().rootFrame()
        for frame in root.childFrames():
            if frame.frameFormat().hasProperty(PROP_FRAME_KIND):
                f_kind = str(frame.frameFormat().property(PROP_FRAME_KIND) or "")
                f_rect = doc_layout.frameBoundingRect(frame).translated(hx, vy)
                draw_rect = QRectF(
                    2.0,
                    f_rect.top() + 4.0,
                    max(40.0, self.viewport().width() - 6.0),
                    max(28.0, f_rect.height() - 8.0),
                )
                painter.setPen(QPen(card_bd, 1.0))
                painter.setBrush(QBrush(card_bg))
                painter.drawRoundedRect(draw_rect, 8.0, 8.0)
                if draw_rect.bottom() >= 0 and draw_rect.top() <= vp_rect.height():
                    del_rect = QRectF(draw_rect.right() - 24.0, draw_rect.top() + 6.0, 18.0, 18.0)
                    pending_delete_badges.append((del_rect, "delete_frame", frame))
                    if f_kind in ("python", "py"):
                        code_id = str(frame.frameFormat().property(PROP_CODE_ID) or "")
                        run_rect = QRectF(del_rect.left() - 70.0, draw_rect.top() + 6.0, 64.0, 18.0)
                        status_rect = QRectF(run_rect.left() - 106.0, draw_rect.top() + 6.0, 100.0, 18.0)
                        pending_run_badges.append((run_rect, status_rect, frame, code_id))
                        if code_id and code_id in self._code_output_panels:
                            visible_code_panel_rects[code_id] = draw_rect

        # 2. Disegna barra verticale Quote (3px), Callout, File, AI Note, Checkbox To-Do, Pallini Elenco
        pending_todo_boxes: List[Tuple[QRectF, QRectF, bool, int]] = []
        pending_bullet_dots: List[Tuple[float, float, int]] = []
        block = self.document().begin()
        while block.isValid():
            kind = block.blockFormat().property(PROP_BLOCK_KIND)
            if kind == "bullet":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                if b_rect.bottom() >= -10 and b_rect.top() <= vp_rect.height() + 10:
                    bf = block.blockFormat()
                    indent_lvl = int(bf.property(PROP_BULLET_INDENT) or 0)
                    left_m = float(bf.leftMargin() or 28.0)
                    doc_m = float(self.document().documentMargin() or 0.0)
                    line_h = b_rect.height()
                    blk_lay = block.layout()
                    if blk_lay is not None and blk_lay.lineCount() > 0:
                        line_h = float(blk_lay.lineAt(0).height() or line_h)
                    cx = hx + doc_m + max(7.0, left_m - 13.0)
                    cy = b_rect.top() + line_h / 2.0
                    pending_bullet_dots.append((cx, cy, indent_lvl))
            elif kind == "todo":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                if b_rect.bottom() >= -10 and b_rect.top() <= vp_rect.height() + 10:
                    bf = block.blockFormat()
                    checked = bool(bf.property(PROP_TODO_CHECKED))
                    left_m = float(bf.leftMargin() or 30.0)
                    doc_m = float(self.document().documentMargin() or 0.0)
                    # Centra verticalmente la checkbox sulla prima riga del blocco (anche su to-do multi-riga)
                    line_h = b_rect.height()
                    blk_lay = block.layout()
                    if blk_lay is not None and blk_lay.lineCount() > 0:
                        line_h = float(blk_lay.lineAt(0).height() or line_h)
                    box_sz = 15.0
                    box_x = hx + doc_m + max(4.0, left_m - 23.0)
                    box_y = b_rect.top() + max(0.0, (line_h - box_sz) / 2.0)
                    box_rect = QRectF(box_x, box_y, box_sz, box_sz)
                    hit_rect = box_rect.adjusted(-4.0, -3.0, 3.0, 3.0)
                    pending_todo_boxes.append((box_rect, hit_rect, checked, block.blockNumber()))
            elif kind == "quote":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                bar_rect = QRectF(4.0, b_rect.top() + 2.0, 3.0, max(14.0, b_rect.height() - 4.0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(quote_bar_color))
                painter.drawRoundedRect(bar_rect, 1.5, 1.5)
            elif kind == "callout":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                draw_rect = QRectF(
                    2.0,
                    b_rect.top() - 4.0,
                    max(40.0, self.viewport().width() - 6.0),
                    max(32.0, b_rect.height() + 8.0),
                )
                co_bg = QColor("#F59E0B" if self._dark_mode else "#D97706")
                co_bg.setAlpha(26 if self._dark_mode else 20)
                co_bd = QColor("#F59E0B" if self._dark_mode else "#D97706")
                co_bd.setAlpha(90 if self._dark_mode else 75)
                painter.setPen(QPen(co_bd, 1.0))
                painter.setBrush(QBrush(co_bg))
                painter.drawRoundedRect(draw_rect, 8.0, 8.0)
                bar_rect = QRectF(4.0, draw_rect.top() + 4.0, 3.0, max(14.0, draw_rect.height() - 8.0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor("#F59E0B")))
                painter.drawRoundedRect(bar_rect, 1.5, 1.5)
                painter.setPen(QPen(QColor("#FBBF24" if self._dark_mode else "#B45309")))
                ic_font = QFont(self.font())
                ic_font.setPointSizeF(11.0)
                painter.setFont(ic_font)
                painter.drawText(QRectF(12.0, draw_rect.top(), 20.0, 28.0), Qt.AlignmentFlag.AlignCenter, "💡")
                if draw_rect.bottom() >= 0 and draw_rect.top() <= vp_rect.height():
                    del_rect = QRectF(draw_rect.right() - 24.0, draw_rect.top() + 6.0, 18.0, 18.0)
                    pending_delete_badges.append((del_rect, "delete_block", block.blockNumber()))
            elif kind == "file":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                draw_rect = QRectF(
                    2.0,
                    b_rect.top() - 4.0,
                    max(40.0, self.viewport().width() - 6.0),
                    max(34.0, b_rect.height() + 8.0),
                )
                painter.setPen(QPen(card_bd, 1.0))
                painter.setBrush(QBrush(card_bg))
                painter.drawRoundedRect(draw_rect, 8.0, 8.0)
                if draw_rect.bottom() >= 0 and draw_rect.top() <= vp_rect.height():
                    del_rect = QRectF(draw_rect.right() - 24.0, draw_rect.top() + 6.0, 18.0, 18.0)
                    pending_delete_badges.append((del_rect, "delete_block", block.blockNumber()))
            elif kind == "ai_note":
                b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
                draw_rect = QRectF(
                    2.0,
                    b_rect.top() - 3.0,
                    max(40.0, self.viewport().width() - 6.0),
                    max(28.0, b_rect.height() + 6.0),
                )
                ai_bg = QColor(self._sync_accent_color)
                ai_bg.setAlpha(24 if self._dark_mode else 18)
                ai_bd = QColor(self._sync_accent_color)
                ai_bd.setAlpha(95 if self._dark_mode else 75)
                painter.setPen(QPen(ai_bd, 1.0))
                painter.setBrush(QBrush(ai_bg))
                painter.drawRoundedRect(draw_rect, 8.0, 8.0)
                bar_rect = QRectF(4.0, draw_rect.top() + 4.0, 3.0, max(14.0, draw_rect.height() - 8.0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(self._sync_accent_color))
                painter.drawRoundedRect(bar_rect, 1.5, 1.5)
                if draw_rect.bottom() >= 0 and draw_rect.top() <= vp_rect.height():
                    del_rect = QRectF(draw_rect.right() - 24.0, draw_rect.top() + 6.0, 18.0, 18.0)
                    pending_delete_badges.append((del_rect, "delete_block", block.blockNumber()))
            block = block.next()

        painter.end()
        super().paintEvent(event)

        if self._is_studio_view:
            return

        fg_painter = QPainter(self.viewport())
        fg_painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        ghost_color = QColor(226, 232, 240, 110) if self._dark_mode else QColor(30, 41, 59, 110)

        # 3. Ghost-Text Placeholder per blocchi vuoti (/h1, /h2, /h3, /todo, /quote, /callout)
        #    e overlay hover su Matrici interattive
        block = self.document().begin()
        while block.isValid():
            b_rect = doc_layout.blockBoundingRect(block).translated(hx, vy)
            if b_rect.bottom() >= -20 and b_rect.top() <= vp_rect.height() + 20:
                ud = block.userData()
                p_kind = ud.placeholder_kind if isinstance(ud, BlockUserData) else ""
                if not p_kind:
                    bk = block.blockFormat().property(PROP_BLOCK_KIND)
                    if bk in PLACEHOLDER_TEXTS:
                        p_kind = bk

                if p_kind in PLACEHOLDER_TEXTS:
                    txt = block.text()
                    is_empty = (txt in ("☐ ", "☑ ", "☐", "☑", "")) if p_kind == "todo" else (txt == "")
                    if is_empty:
                        end_cur = QTextCursor(block)
                        end_cur.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                        crect = self.cursorRect(end_cur)
                        ph_font = QFont(end_cur.charFormat().font())
                        if ph_font.pointSizeF() <= 0:
                            ph_font = QFont(self.font())
                        fg_painter.setFont(ph_font)
                        fg_painter.setPen(QPen(ghost_color))
                        draw_x = float(crect.left() + 2)
                        avail_w = max(80.0, float(self.viewport().width()) - draw_x - 32.0)
                        fg_painter.drawText(
                            QRectF(draw_x, float(crect.top()), avail_w, float(max(18, crect.height()))),
                            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                            PLACEHOLDER_TEXTS[p_kind],
                        )

                # Cerca matrici nel blocco per hover pill ("✎ Modifica" + "✕")
                fit = block.begin()
                while not fit.atEnd():
                    frag = fit.fragment()
                    if frag.isValid():
                        cf = frag.charFormat()
                        if cf.hasProperty(PROP_LATEX_SOURCE):
                            tex = str(cf.property(PROP_LATEX_SOURCE) or "")
                            if re.search(r"\\begin\{[bpv]matrix\}", tex):
                                img_fmt = cf.toImageFormat()
                                m_rect = self._matrix_rect_in_viewport(
                                    frag.position(), img_fmt.width(), img_fmt.height()
                                )
                                is_mat_hovered = (self._hovered_matrix_pos == frag.position())
                                if is_mat_hovered:
                                    hl_pen = QPen(QColor("#6366F1"), 1.2, Qt.PenStyle.DashLine)
                                    fg_painter.setPen(hl_pen)
                                    fg_painter.setBrush(Qt.BrushStyle.NoBrush)
                                    fg_painter.drawRoundedRect(m_rect.adjusted(-3, -3, 3, 3), 6.0, 6.0)

                                    edit_rect = QRectF(m_rect.right() - 84.0, max(2.0, m_rect.top() - 10.0), 62.0, 18.0)
                                    del_rect = QRectF(m_rect.right() - 18.0, max(2.0, m_rect.top() - 10.0), 18.0, 18.0)

                                    edit_idx = len(self._block_action_rects)
                                    self._block_action_rects.append((edit_rect, "edit_matrix", frag.position()))
                                    e_hov = (self._hovered_action_idx == edit_idx)
                                    fg_painter.setPen(Qt.PenStyle.NoPen)
                                    fg_painter.setBrush(QBrush(QColor("#4F46E5" if e_hov else "#6366F1")))
                                    fg_painter.drawRoundedRect(edit_rect, 4.0, 4.0)
                                    fg_painter.setPen(QPen(QColor("#FFFFFF")))
                                    ef = QFont(self.font())
                                    ef.setPointSizeF(8.5)
                                    ef.setWeight(QFont.Weight.DemiBold)
                                    fg_painter.setFont(ef)
                                    fg_painter.drawText(edit_rect, Qt.AlignmentFlag.AlignCenter, "✎ Modifica")

                                    del_idx = len(self._block_action_rects)
                                    self._block_action_rects.append((del_rect, "delete_matrix", frag.position()))
                                    self._draw_delete_badge(fg_painter, del_rect, self._hovered_action_idx == del_idx)
                    fit += 1
            block = block.next()

        # 4. Ghost-Text per Auto-Calcolo Inline (=) oppure Anteprima Dettatura FlowType (prime 6 parole)
        if self._inline_calc_suggestion is not None:
            sug = self._inline_calc_suggestion
            if self.textCursor().position() == sug.get("pos"):
                crect = self.cursorRect()
                calc_color = QColor(148, 163, 184, 155) if self._dark_mode else QColor(100, 116, 139, 155)
                cf_font = QFont(self.font())
                if sug.get("kind") == "matrix":
                    cf_font.setPointSizeF(max(8.5, cf_font.pointSizeF() - 1.0))
                    cf_font.setItalic(True)
                fg_painter.setFont(cf_font)
                fg_painter.setPen(QPen(calc_color))
                gx = float(crect.left() + 2)
                gw = max(120.0, float(self.viewport().width()) - gx - 12.0)
                fg_painter.drawText(
                    QRectF(gx, float(crect.top()), gw, float(max(18, crect.height()))),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    sug.get("ghost_text", ""),
                )
        elif self.hasFocus():
            cur = self.textCursor()
            cur_blk = cur.block()
            if cur_blk.isValid() and cur_blk.text() == "" and not cur_blk.blockFormat().property(PROP_BLOCK_KIND):
                preview = dictation_tracker.get_active_preview()
                if preview is not None:
                    crect = self.cursorRect()
                    d_color = QColor(203, 213, 225, 115) if self._dark_mode else QColor(71, 85, 105, 115)
                    df = QFont(self.font())
                    df.setItalic(True)
                    fg_painter.setFont(df)
                    fg_painter.setPen(QPen(d_color))
                    gx = float(crect.left() + 2)
                    gw = max(120.0, float(self.viewport().width()) - gx - 16.0)
                    fg_painter.drawText(
                        QRectF(gx, float(crect.top()), gw, float(max(18, crect.height()))),
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                        preview["ghost_text"],
                    )

        # 4b-bis. Disegna pallini vettoriali nel margine sinistro dei blocchi Elenco Puntato
        bullet_col = QColor("#94A3B8" if self._dark_mode else "#475569")
        for cx, cy, indent_lvl in pending_bullet_dots:
            if indent_lvl <= 0:
                fg_painter.setPen(Qt.PenStyle.NoPen)
                fg_painter.setBrush(QBrush(bullet_col))
                fg_painter.drawEllipse(QPointF(cx, cy), 2.6, 2.6)
            elif indent_lvl == 1:
                fg_painter.setPen(QPen(bullet_col, 1.3))
                fg_painter.setBrush(Qt.BrushStyle.NoBrush)
                fg_painter.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
            else:
                fg_painter.setPen(Qt.PenStyle.NoPen)
                fg_painter.setBrush(QBrush(bullet_col))
                fg_painter.drawRoundedRect(QRectF(cx - 2.2, cy - 2.2, 4.4, 4.4), 1.0, 1.0)

        # 4b. Disegna checkbox vettoriali interattive nel margine sinistro dei blocchi To-Do
        for box_rect, hit_rect, checked, blk_num in pending_todo_boxes:
            todo_idx = len(self._block_action_rects)
            self._block_action_rects.append((hit_rect, "toggle_todo", blk_num))
            is_hov = (self._hovered_action_idx == todo_idx)
            if checked:
                fill_col = QColor(self._sync_accent_color)
                if is_hov:
                    fill_col = fill_col.lighter(115)
                fg_painter.setPen(QPen(fill_col, 1.4))
                fg_painter.setBrush(QBrush(fill_col))
                fg_painter.drawRoundedRect(box_rect, 3.5, 3.5)
                # Segno di spunta nitido vettoriale
                chk_pen = QPen(QColor("#FFFFFF"), 1.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                fg_painter.setPen(chk_pen)
                chk_path = QPainterPath()
                chk_path.moveTo(box_rect.left() + 3.4, box_rect.top() + box_rect.height() * 0.54)
                chk_path.lineTo(box_rect.left() + box_rect.width() * 0.43, box_rect.bottom() - 3.6)
                chk_path.lineTo(box_rect.right() - 3.0, box_rect.top() + 3.8)
                fg_painter.drawPath(chk_path)
            else:
                border_col = QColor(self._sync_accent_color) if is_hov else QColor("#94A3B8" if self._dark_mode else "#64748B")
                bg_col = QColor(self._sync_accent_color) if is_hov else QColor(0, 0, 0, 0)
                if is_hov:
                    bg_col.setAlpha(36)
                fg_painter.setPen(QPen(border_col, 1.5))
                fg_painter.setBrush(QBrush(bg_col))
                fg_painter.drawRoundedRect(box_rect, 3.5, 3.5)

        # 5. Pulsanti discreti ✕ e ▶ Esegui per blocchi speciali (/code, /callout, /file, ai_note)
        for run_rect, status_rect, frame, code_id in pending_run_badges:
            st = self._code_exec_states.get(code_id, {}) if code_id else {}
            st_kind = st.get("status", "ready")
            elapsed = st.get("elapsed", 0.0)
            if st_kind == "running":
                st_txt = "⏳ Esecuzione..."
                st_col = QColor("#FBBF24" if self._dark_mode else "#D97706")
            elif st_kind == "done":
                st_txt = f"✓ {elapsed:.2f}s"
                st_col = QColor("#34D399" if self._dark_mode else "#059669")
            elif st_kind == "error":
                st_txt = f"✕ {elapsed:.2f}s"
                st_col = QColor("#F87171" if self._dark_mode else "#DC2626")
            else:
                st_txt = "Pronto"
                st_col = QColor("#94A3B8" if self._dark_mode else "#64748B")

            sf = QFont(self.font())
            sf.setPointSizeF(8.0)
            fg_painter.setFont(sf)
            fg_painter.setPen(QPen(st_col))
            fg_painter.drawText(status_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, st_txt)

            run_idx = len(self._block_action_rects)
            self._block_action_rects.append((run_rect, "run_code", frame))
            r_hov = (self._hovered_action_idx == run_idx)
            r_bg = QColor("#059669" if r_hov else ("#1E293B" if self._dark_mode else "#E2E8F0"))
            r_bg.setAlpha(235 if r_hov else 190)
            r_fg = QColor("#FFFFFF" if r_hov else ("#E2E8F0" if self._dark_mode else "#1E293B"))
            fg_painter.setPen(Qt.PenStyle.NoPen)
            fg_painter.setBrush(QBrush(r_bg))
            fg_painter.drawRoundedRect(run_rect, 4.0, 4.0)
            fg_painter.setPen(QPen(r_fg))
            rf = QFont(self.font())
            rf.setPointSizeF(8.2)
            rf.setWeight(QFont.Weight.DemiBold)
            fg_painter.setFont(rf)
            fg_painter.drawText(run_rect, Qt.AlignmentFlag.AlignCenter, "▶ Esegui")

        for rect, action_kind, target in pending_delete_badges:
            idx = len(self._block_action_rects)
            self._block_action_rects.append((rect, action_kind, target))
            self._draw_delete_badge(fg_painter, rect, self._hovered_action_idx == idx)

        fg_painter.end()

        # 6. Riposiziona pannelli Output inline sotto i rispettivi blocchi codice Python
        for cid, panel in list(self._code_output_panels.items()):
            f_draw_rect = visible_code_panel_rects.get(cid)
            if f_draw_rect is None:
                panel.hide()
                continue
            pw = max(160, int(f_draw_rect.width()))
            px = int(f_draw_rect.left())
            py = int(f_draw_rect.bottom()) + 4
            if py > vp_rect.height() - 16 or py + panel.height() < 0:
                panel.hide()
                continue
            if panel.width() != pw:
                panel.setFixedWidth(pw)
            panel.move(px, py)
            if not panel.isVisible():
                panel.show()
                panel.raise_()

    # -----------------------------------------------------------------
    # ELIMINAZIONE BLOCCHI UNDO-SAFE & MATRICE IN-PLACE
    # -----------------------------------------------------------------
    def _delete_frame_undoable(self, frame: QTextFrame):
        if frame is None:
            return
        cid = str(frame.frameFormat().property(PROP_CODE_ID) or "")
        if cid:
            self._close_code_output_panel(cid)
        doc = self.document()
        c = QTextCursor(doc)
        c.beginEditBlock()
        start = max(0, frame.firstPosition() - 1)
        end = min(max(0, doc.characterCount() - 1), frame.lastPosition() + 1)
        c.setPosition(start)
        c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        c.setBlockFormat(QTextBlockFormat())
        c.setCharFormat(QTextCharFormat())
        c.block().setUserData(None)
        c.endEditBlock()
        self.setTextCursor(c)
        self._on_internal_text_changed()

    def _delete_block_undoable(self, block_number: int):
        doc = self.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return
        c = QTextCursor(block)
        c.beginEditBlock()
        if doc.blockCount() <= 1:
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            c.removeSelectedText()
            c.setBlockFormat(QTextBlockFormat())
            c.setCharFormat(QTextCharFormat())
            block.setUserData(None)
        else:
            if block.previous().isValid():
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.movePosition(QTextCursor.MoveOperation.PreviousBlock)
                c.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                c.setPosition(block.position() + max(0, len(block.text())), QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
            else:
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
        c.endEditBlock()
        self.setTextCursor(c)
        self._on_internal_text_changed()

    def _delete_matrix_at_pos(self, doc_pos: int):
        doc = self.document()
        if doc_pos < 0 or doc_pos >= doc.characterCount() - 1:
            return
        c = QTextCursor(doc)
        c.beginEditBlock()
        c.setPosition(doc_pos)
        c.setPosition(doc_pos + 1, QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        c.endEditBlock()
        self.setTextCursor(c)
        self._hovered_matrix_pos = None
        self._on_internal_text_changed()

    def _close_inline_matrix_editor(self):
        if self._inline_matrix_editor is not None:
            ed = self._inline_matrix_editor
            self._inline_matrix_editor = None
            ed.hide()
            ed.deleteLater()

    def _open_inline_matrix_editor(self, doc_pos: int, old_tex: str):
        self._close_inline_matrix_editor()
        ed = InlineMatrixEditor(
            self.viewport(),
            doc_pos=doc_pos,
            initial_tex=old_tex,
            dark_mode=self._dark_mode,
        )

        def _on_commit(new_tex: str):
            self._close_inline_matrix_editor()
            if new_tex and new_tex.strip() != old_tex.strip():
                doc = self.document()
                if 0 <= doc_pos < doc.characterCount() - 1:
                    c = QTextCursor(doc)
                    c.beginEditBlock()
                    c.setPosition(doc_pos)
                    c.setPosition(doc_pos + 1, QTextCursor.MoveMode.KeepAnchor)
                    c.removeSelectedText()
                    self._insert_latex_block_at_cursor(c, new_tex)
                    c.endEditBlock()
                    self.setTextCursor(c)
                    self._on_internal_text_changed()
            self.setFocus()

        def _on_del():
            self._close_inline_matrix_editor()
            self._delete_matrix_at_pos(doc_pos)
            self.setFocus()

        ed.commitRequested.connect(_on_commit)
        ed.deleteRequested.connect(_on_del)

        c = QTextCursor(self.document())
        c.setPosition(doc_pos)
        crect = self.cursorRect(c)
        px = max(6, min(crect.left(), max(6, self.viewport().width() - ed.width() - 8)))
        py = max(6, min(crect.top(), max(6, self.viewport().height() - ed.height() - 8)))
        ed.move(px, py)
        ed.show()
        ed.raise_()
        ed.focus_first_cell()
        self._inline_matrix_editor = ed

    def _collect_labeled_matrices(self) -> Dict[str, Any]:
        """Estrae tutte le matrici con etichetta (es. A = [Matrice]) presenti nel documento."""
        matrices: Dict[str, Any] = {}
        block = self.document().begin()
        while block.isValid():
            prev_text = ""
            fit = block.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid():
                    cf = frag.charFormat()
                    if cf.hasProperty(PROP_LATEX_SOURCE):
                        tex = str(cf.property(PROP_LATEX_SOURCE) or "")
                        parsed = parse_latex_matrix(tex)
                        if parsed is not None:
                            lbl, arr = parsed
                            if not lbl and prev_text:
                                lm = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$", prev_text)
                                if lm:
                                    lbl = lm.group(1)
                            if lbl:
                                matrices[lbl] = arr
                        prev_text = ""
                    else:
                        prev_text += frag.text().replace("\ufffc", "")
                fit += 1
            block = block.next()
        return matrices

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._selection_toolbar.hide()
        self.viewport().update()

    def _on_selection_changed_for_toolbar(self):
        if self._is_studio_view:
            self._selection_toolbar.hide()
            return
        cur = self.textCursor()
        if not cur.hasSelection() or not cur.selectedText().strip():
            self._selection_toolbar.hide()
            return
        self._saved_selection_range = (
            min(cur.selectionStart(), cur.selectionEnd()),
            max(cur.selectionStart(), cur.selectionEnd()),
        )
        # Posiziona la toolbar fluttuante sopra l'inizio (o fine) della selezione
        start_cur = QTextCursor(self.document())
        start_cur.setPosition(min(cur.selectionStart(), cur.selectionEnd()))
        rect = self.cursorRect(start_cur)
        self._selection_toolbar.adjustSize()
        tw = self._selection_toolbar.width()
        th = self._selection_toolbar.height()
        vp = self.viewport().rect()
        tx = max(8, min(rect.left(), vp.width() - tw - 8))
        ty = rect.top() - th - 6
        if ty < 6:
            ty = min(vp.height() - th - 6, rect.bottom() + 6)
        self._selection_toolbar.move(tx, ty)
        self._selection_toolbar.show()
        self._selection_toolbar.raise_()

    def _capture_current_selection_range(self):
        cur = self.textCursor()
        if cur.hasSelection() and cur.selectedText().strip():
            self._saved_selection_range = (
                min(cur.selectionStart(), cur.selectionEnd()),
                max(cur.selectionStart(), cur.selectionEnd()),
            )

    def _on_selection_highlight_clicked(self, hex_color: str):
        self.apply_highlight_selection(hex_color or None)
        self._selection_toolbar.hide()

    def _on_selection_color_clicked(self, hex_color: str):
        self.apply_text_color_selection(hex_color or None)
        self._selection_toolbar.hide()

    def _on_selection_ai_correct_clicked(self):
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range()
        if rng is None:
            return
        s_pos, e_pos = rng
        c = QTextCursor(self.document())
        c.setPosition(s_pos)
        c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        orig_text = c.selectedText().replace("\u2029", "\n")
        if not orig_text.strip():
            return

        self._selection_toolbar.hide()
        self._show_discrete_toast("✦ Correzione AI in corso...", duration_ms=8000)

        worker = _SelectionAiCorrectWorker(s_pos, e_pos, orig_text, parent=self)
        self._bg_workers.append(worker)
        worker.finished_result.connect(self._on_selection_ai_correct_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda w=worker: self._cleanup_worker(self._bg_workers, w))
        worker.start()

    def _on_selection_ai_correct_finished(
        self,
        start_pos: int,
        end_pos: int,
        original_text: str,
        corrected_text: str,
        error_msg: str,
    ):
        if error_msg or not corrected_text.strip():
            self._show_discrete_toast(error_msg or "Impossibile correggere il testo selezionato.")
            return

        doc = self.document()
        max_pos = max(0, doc.characterCount() - 1)
        if start_pos < 0 or end_pos > max_pos or start_pos >= end_pos:
            self._show_discrete_toast("Testo modificato durante la correzione: operazione annullata.")
            return

        chk = QTextCursor(doc)
        chk.setPosition(start_pos)
        chk.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        current_slice = chk.selectedText().replace("\u2029", "\n")
        if current_slice != original_text:
            self._show_discrete_toast("Testo modificato durante la correzione: operazione annullata.")
            return

        chk.beginEditBlock()
        chk.removeSelectedText()
        chk.insertText(corrected_text, QTextCharFormat())
        chk.endEditBlock()
        new_cur = QTextCursor(doc)
        new_cur.setPosition(start_pos)
        new_cur.setPosition(start_pos + len(corrected_text), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(new_cur)
        self._on_internal_text_changed()
        self._show_discrete_toast("✓ Testo corretto con AI (Ctrl+Z per annullare)")

    def _on_selection_whis_clicked(self):
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range()
        if rng is None:
            return
        s_pos, e_pos = rng
        c = QTextCursor(self.document())
        c.setPosition(s_pos)
        c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        sel_text = c.selectedText().replace("\u2029", "\n").strip()
        if not sel_text:
            return
        self._saved_selection_range = rng
        self._selection_toolbar.hide()
        self.askWhisSelectionRequested.emit(sel_text)

    def _insert_whis_lines_at_cursor(self, cur: QTextCursor, clean_text: str):
        lines = clean_text.split("\n")
        for idx, line_txt in enumerate(lines):
            if idx > 0:
                cur.insertBlock()
            if line_txt:
                self._insert_inline_formatted_text(cur, line_txt)

    def insert_text_from_whis(self, text: str) -> bool:
        """Inserisce testo generato da Whis al cursore in formato WYSIWYG (singolo step Undo)."""
        if self._is_studio_view or self.isReadOnly():
            return False
        clean_text = (text or "").replace("\r\n", "\n").strip()
        if not clean_text:
            return False

        cur = self.textCursor()
        if cur.hasSelection():
            cur.setPosition(max(cur.selectionStart(), cur.selectionEnd()))
        need_space = False
        if cur.positionInBlock() > 0:
            prev_ch = cur.block().text()[cur.positionInBlock() - 1]
            if prev_ch and not prev_ch.isspace() and clean_text[0] not in ".,;:!?)]}\n":
                need_space = True
        cur.beginEditBlock()
        if need_space:
            cur.insertText(" ", QTextCharFormat())
        self._insert_whis_lines_at_cursor(cur, clean_text)
        cur.endEditBlock()
        self.setTextCursor(cur)
        self._on_internal_text_changed()
        self._show_discrete_toast("✓ Inserito da Whis (Ctrl+Z per annullare)")
        return True

    def replace_selection_from_whis(self, text: str) -> bool:
        """Sostituisce la selezione corrente (o salvata) con il testo generato da Whis in WYSIWYG."""
        if self._is_studio_view or self.isReadOnly():
            return False
        clean_text = (text or "").replace("\r\n", "\n").strip()
        if not clean_text:
            return False

        rng = self._resolve_selection_range()
        if rng is None:
            return self.insert_text_from_whis(clean_text)

        s_pos, e_pos = rng
        doc = self.document()
        max_pos = max(0, doc.characterCount() - 1)
        s_pos = max(0, min(s_pos, max_pos))
        e_pos = max(0, min(e_pos, max_pos))
        if e_pos <= s_pos:
            return self.insert_text_from_whis(clean_text)

        cur = QTextCursor(doc)
        cur.beginEditBlock()
        cur.setPosition(s_pos)
        cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        self._insert_whis_lines_at_cursor(cur, clean_text)
        end_pos = cur.position()
        cur.endEditBlock()

        self._saved_selection_range = (s_pos, end_pos)
        sel_cur = QTextCursor(doc)
        sel_cur.setPosition(s_pos)
        sel_cur.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel_cur)
        self._on_internal_text_changed()
        self._show_discrete_toast("✓ Selezione sostituita da Whis (Ctrl+Z per annullare)")
        return True

    def _on_selection_link_note_clicked(self):
        if self._is_studio_view:
            return
        rng = self._resolve_selection_range()
        if rng is None:
            return
        s_pos, e_pos = rng
        c = QTextCursor(self.document())
        c.setPosition(s_pos)
        c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
        sel_text = c.selectedText().replace("\u2029", " ").strip()
        if not sel_text:
            return

        self._pending_link_range = (s_pos, e_pos)
        self._pending_link_text = sel_text
        self._selection_toolbar.hide()

        start_cur = QTextCursor(self.document())
        start_cur.setPosition(s_pos)
        rect = self.cursorRect(start_cur)
        global_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 6))
        screen = QApplication.screenAt(global_pt) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pt.x() + self._link_picker_popup.width() > avail.right():
                global_pt.setX(max(avail.left(), avail.right() - self._link_picker_popup.width() - 8))
            if global_pt.y() + 260 > avail.bottom():
                above_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.top()))
                global_pt.setY(max(avail.top(), above_pt.y() - 260))
        self._link_picker_popup.move(global_pt)
        self._link_picker_popup.open_for_query(sel_text)

    def _on_link_picker_note_chosen(self, chosen_title: str):
        clean_t = (chosen_title or "").strip()
        rng = self._pending_link_range
        sel_text = (self._pending_link_text or "").strip()
        self._pending_link_range = None
        self._pending_link_text = ""
        if not clean_t or rng is None:
            return

        s_pos, e_pos = rng
        doc = self.document()
        max_pos = max(0, doc.characterCount() - 1)
        if s_pos < 0 or e_pos > max_pos or s_pos > e_pos:
            return

        c = QTextCursor(doc)
        c.beginEditBlock()
        if s_pos == e_pos:
            c.setPosition(s_pos)
            c.insertText(f"[[{clean_t}]]", QTextCharFormat())
        else:
            s_low = sel_text.lower()
            t_low = clean_t.lower()
            replace_entire = (s_low == t_low) or (
                len(s_low) >= 3 and (s_low in t_low or t_low in s_low) and abs(len(s_low) - len(t_low)) <= 12
            )
            if replace_entire:
                c.setPosition(s_pos)
                c.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
                c.insertText(f"[[{clean_t}]]", QTextCharFormat())
            else:
                c.setPosition(e_pos)
                c.insertText(f" [[{clean_t}]]", QTextCharFormat())
        c.endEditBlock()
        self.setTextCursor(c)
        self.setFocus()
        self._on_internal_text_changed()

    def _on_selection_rsvp_clicked(self):
        cur = self.textCursor()
        if cur.hasSelection():
            start_cur = QTextCursor(self.document())
            start_cur.setPosition(min(cur.selectionStart(), cur.selectionEnd()))
            self._selection_toolbar.hide()
            self.rsvpRequested.emit(start_cur.blockNumber(), True)

    def get_rsvp_markdown_slice(self, start_block_idx: int = 0, selection_only: bool = False) -> Tuple[str, int]:
        """Estrae il Markdown (o snippet selezionato) per la modalità RSVP preservando il LaTeX."""
        if self._is_studio_view:
            return (self._raw_markdown or "", 0)

        cur = self.textCursor()
        if selection_only and cur.hasSelection():
            s_pos = min(cur.selectionStart(), cur.selectionEnd())
            e_pos = max(cur.selectionStart(), cur.selectionEnd())
            start_cur = QTextCursor(self.document())
            start_cur.setPosition(s_pos)
            base_block = start_cur.blockNumber()

            # Estrai i frammenti nella selezione convertendo eventuali PROP_LATEX_SOURCE in $$...$$
            parts: List[str] = []
            blk = start_cur.block()
            while blk.isValid() and blk.position() <= e_pos:
                fit = blk.begin()
                while not fit.atEnd():
                    frag = fit.fragment()
                    if frag.isValid():
                        f_start = frag.position()
                        f_end = f_start + frag.length()
                        if f_end > s_pos and f_start < e_pos:
                            cf = frag.charFormat()
                            if cf.hasProperty(PROP_LATEX_SOURCE):
                                tex_src = str(cf.property(PROP_LATEX_SOURCE) or "").strip()
                                if tex_src:
                                    parts.append(f"\n$$\n{tex_src}\n$$\n")
                            else:
                                txt = frag.text().replace("\ufffc", "")
                                cut_l = max(0, s_pos - f_start)
                                cut_r = min(len(txt), e_pos - f_start)
                                if cut_r > cut_l:
                                    parts.append(txt[cut_l:cut_r])
                    fit += 1
                if blk.position() + blk.length() <= e_pos:
                    parts.append("\n")
                blk = blk.next()
            return ("".join(parts).strip(), base_block)

        # Dal blocco start_block_idx fino alla fine della nota
        lines: List[str] = []
        doc = self.document()
        blk = doc.findBlockByNumber(max(0, start_block_idx))
        while blk.isValid():
            lines.append(self._serialize_block_to_md(blk))
            blk = blk.next()
        md = "\n".join(lines)
        md = re.sub(r"\n?\ufffc\n?", "\n", md).replace("\ufffc", "")
        return (md, max(0, start_block_idx))

    def focus_block(self, block_idx: int):
        """Riporta il focus nell'editor esattamente al blocco indicato."""
        if self._is_studio_view:
            self.setFocus()
            return
        doc = self.document()
        blk = doc.findBlockByNumber(max(0, min(block_idx, doc.blockCount() - 1)))
        if blk.isValid():
            cur = QTextCursor(blk)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
        self.setFocus()

    def _on_cursor_pos_changed(self):
        if self._inline_calc_suggestion is not None:
            if self.textCursor().position() != self._inline_calc_suggestion.get("pos"):
                self._inline_calc_suggestion = None
                self.viewport().update()
        elif dictation_tracker.get_active_preview() is not None:
            self.viewport().update()

    # -----------------------------------------------------------------
    # VISTA STUDIO TOGGLE
    # -----------------------------------------------------------------
    def set_studio_view(self, enabled: bool):
        if enabled == self._is_studio_view:
            return
        if not self._is_studio_view:
            self._raw_markdown = self._serialize_document_to_markdown()
        self._is_studio_view = enabled
        self.slash_popup.hide()

        self._internal_update = True
        try:
            if enabled:
                self.highlighter.set_enabled(False)
                self.setReadOnly(True)
                self.setHtml(
                    markdown_to_studio_html(
                        self._raw_markdown,
                        BASE_DATA_DIR,
                        dark_mode=self._dark_mode,
                        reading_mode=self._reading_mode,
                        font_family_css=self._font_family_css,
                        font_scale_pct=self._font_scale_pct,
                    )
                )
            else:
                self.setReadOnly(False)
                self.highlighter.set_enabled(True)
                self._populate_wysiwyg_document(self._raw_markdown)
        finally:
            self._internal_update = False

        self.studioViewChanged.emit(enabled)

    def toggle_studio_view(self):
        self.set_studio_view(not self._is_studio_view)

    # -----------------------------------------------------------------
    # INCOLLA IMMAGINI (Ctrl+V) E DRAG & DROP
    # -----------------------------------------------------------------
    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        if source.hasImage():
            return True
        if source.hasUrls():
            for url in source.urls():
                if url.isLocalFile():
                    ext = os.path.splitext(url.toLocalFile())[1].lower()
                    if ext in IMAGE_EXTENSIONS:
                        return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData):
        if self._is_studio_view:
            return

        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self._save_and_insert_image(img)
                return

        if source.hasUrls():
            handled = False
            for url in source.urls():
                if url.isLocalFile():
                    local_path = url.toLocalFile()
                    ext = os.path.splitext(local_path)[1].lower()
                    if ext in IMAGE_EXTENSIONS and os.path.exists(local_path):
                        qimg = QImage(local_path)
                        if not qimg.isNull():
                            self._save_and_insert_image(qimg)
                            handled = True
            if handled:
                return

        if source.hasText():
            plain = source.text()
            if plain:
                norm_plain = plain.replace("\r\n", "\n").replace("\r", "\n")
                cur = self.textCursor()
                blk_kind = cur.block().blockFormat().property(PROP_BLOCK_KIND)
                cur.beginEditBlock()
                if blk_kind == "todo" and bool(cur.block().blockFormat().property(PROP_TODO_CHECKED)):
                    cur.insertText(plain)
                elif cur.currentFrame() is not None and cur.currentFrame().frameFormat().hasProperty(PROP_FRAME_KIND):
                    cur.insertText(plain)
                elif blk_kind in ("h1", "h2", "h3", "h4"):
                    lvl = int(blk_kind[1])
                    h_cf = self._heading_char_format(lvl)
                    lines = norm_plain.split("\n")
                    first_line = re.sub(r"^#{1,4}\s+", "", lines[0])
                    cur.setBlockCharFormat(h_cf)
                    cur.insertText(first_line, h_cf)
                    for extra_line in lines[1:]:
                        cur.insertBlock()
                        cur.setBlockFormat(QTextBlockFormat())
                        cur.setBlockCharFormat(QTextCharFormat())
                        cur.setCharFormat(QTextCharFormat())
                        cur.block().setUserData(None)
                        hm = re.match(r"^(#{1,4})\s+(.*)$", extra_line)
                        if hm:
                            self._apply_heading_block_at_cursor(cur, level=len(hm.group(1)), text=hm.group(2))
                        else:
                            clean_cf = QTextCharFormat()
                            clean_cf.clearForeground()
                            cur.insertText(extra_line, clean_cf)
                    cur.endEditBlock()
                    self.setTextCursor(cur)
                    self.setCurrentCharFormat(h_cf if len(lines) == 1 else QTextCharFormat())
                    return
                else:
                    is_empty_start = cur.atBlockStart() and cur.block().text() == ""
                    if is_empty_start and ("\n" in norm_plain or re.match(r"^#{1,4}\s+", norm_plain)):
                        lines = norm_plain.split("\n")
                        for idx, ln in enumerate(lines):
                            if idx > 0:
                                cur.insertBlock()
                                cur.setBlockFormat(QTextBlockFormat())
                                cur.setBlockCharFormat(QTextCharFormat())
                                cur.setCharFormat(QTextCharFormat())
                                cur.block().setUserData(None)
                            hm = re.match(r"^(#{1,4})\s+(.*)$", ln)
                            if hm and cur.atBlockStart() and cur.block().text() == "":
                                self._apply_heading_block_at_cursor(cur, level=len(hm.group(1)), text=hm.group(2))
                            else:
                                clean_cf = QTextCharFormat()
                                clean_cf.clearForeground()
                                cur.insertText(ln, clean_cf)
                    else:
                        clean_cf = QTextCharFormat()
                        clean_cf.clearForeground()
                        cur.insertText(plain, clean_cf)
                cur.endEditBlock()
                self.setTextCursor(cur)
                return

        super().insertFromMimeData(source)

    def _save_and_insert_image(self, qimg: QImage):
        ensure_note_media_dir()
        ts = f"{time.strftime('%Y%m%d_%H%M%S')}_{int((time.time() % 1) * 1000):03d}"
        filename = f"img_{ts}.png"
        abs_path = os.path.join(NOTE_MEDIA_DIR, filename)
        qimg.save(abs_path, "PNG")

        rel_md = f"![Screenshot](note_media/{filename})"
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.positionInBlock() > 0:
            cursor.insertText("\n")
        cursor.insertText(rel_md + "\n")
        self._insert_scaled_qimage_at_cursor(cursor, qimg, abs_path)
        cursor.insertText("\n")
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    def _open_file_picker_and_embed(self, target_block=None):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Carica o incorpora un file",
            "",
            "Tutti i file (*.*);;Immagini (*.png *.jpg *.jpeg *.webp *.bmp);;Documenti PDF (*.pdf)",
        )
        if not file_path:
            return

        ext = os.path.splitext(file_path)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            qimg = QImage(file_path)
            if not qimg.isNull():
                if target_block is not None:
                    cur = QTextCursor(target_block)
                    cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                    cur.removeSelectedText()
                    cur.setBlockFormat(QTextBlockFormat())
                    self.setTextCursor(cur)
                self._save_and_insert_image(qimg)
                return

        if target_block is not None:
            cur = QTextCursor(target_block)
            cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            self._apply_file_block_at_cursor(cur, file_path)
        else:
            cur = self.textCursor()
            self._apply_file_block_at_cursor(cur, file_path)
            self.setTextCursor(cur)
        self._on_internal_text_changed()

    # -----------------------------------------------------------------
    # AUDIO-TEXT SYNC (KARAOKE HIGHLIGHTING & ALT+CLICK SEEK)
    # -----------------------------------------------------------------
    def set_sync_accent_color(self, hex_color: str):
        col = QColor(hex_color or "#6366F1")
        if col.isValid():
            self._sync_accent_color = col
            if self._active_highlight_span is not None:
                self._apply_extra_selection_span(self._active_highlight_span)

    def set_audio_sync_data(self, segments: Optional[List[Dict]]):
        self._auto_scroll_paused = False
        self._active_highlight_span = None
        if not segments:
            self._audio_sync_active = False
            self._sync_segments = []
            self._sync_spans = []
            self._sync_dirty = False
            self.setExtraSelections([])
            return
        self._audio_sync_active = True
        self._sync_segments = list(segments)
        self._rebuild_sync_spans()

    def _rebuild_sync_spans(self):
        if not self._audio_sync_active or not self._sync_segments:
            self._sync_spans = []
            self._sync_dirty = False
            return
        from core.audio_sync import AudioTextAligner, build_doc_tokens_from_qtextdocument
        doc_tokens = build_doc_tokens_from_qtextdocument(self.document())
        self._sync_spans = AudioTextAligner.align(self._sync_segments, doc_tokens)
        self._sync_dirty = False

    def clear_audio_highlight(self):
        self._active_highlight_span = None
        self.setExtraSelections([])

    def resume_auto_scroll(self):
        self._auto_scroll_paused = False

    def _apply_extra_selection_span(self, span):
        if span is None:
            self.setExtraSelections([])
            return
        cur = QTextCursor(self.document())
        doc_len = max(0, self.document().characterCount() - 1)
        s_pos = max(0, min(int(span.char_start), doc_len))
        e_pos = max(s_pos, min(int(span.char_end), doc_len))
        cur.setPosition(s_pos)
        cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)

        bg = QColor(self._sync_accent_color)
        bg.setAlpha(80 if self._dark_mode else 65)

        sel = QTextEdit.ExtraSelection()
        sel.cursor = cur
        fmt = QTextCharFormat()
        fmt.setBackground(QBrush(bg))
        sel.format = fmt
        self.setExtraSelections([sel])

    def highlight_audio_position(self, pos_ms: int):
        if not self._audio_sync_active or self._is_studio_view:
            return
        if self._sync_dirty:
            self._rebuild_sync_spans()
        from core.audio_sync import AudioTextAligner
        span = AudioTextAligner.find_span_at_ms(self._sync_spans, pos_ms)
        if span == self._active_highlight_span:
            return
        self._active_highlight_span = span
        self._apply_extra_selection_span(span)
        if span is not None and not self._auto_scroll_paused:
            self._scroll_to_char_discreetly(span.char_start)

    def _scroll_to_char_discreetly(self, char_pos: int):
        doc_len = max(0, self.document().characterCount() - 1)
        pos = max(0, min(int(char_pos), doc_len))
        cur = QTextCursor(self.document())
        cur.setPosition(pos)
        rect = self.cursorRect(cur)
        vp_rect = self.viewport().rect().adjusted(0, 16, 0, -16)
        vbar = self.verticalScrollBar()
        if vbar is None:
            return
        if rect.top() < vp_rect.top():
            delta = rect.top() - vp_rect.top() - 24
            vbar.setValue(vbar.value() + delta)
        elif rect.bottom() > vp_rect.bottom():
            delta = rect.bottom() - vp_rect.bottom() + 24
            vbar.setValue(vbar.value() + delta)

    def _find_ms_for_cursor(self, cur: QTextCursor) -> Optional[int]:
        if not self._audio_sync_active:
            return None
        if self._sync_dirty:
            self._rebuild_sync_spans()
        from core.audio_sync import AudioTextAligner
        return AudioTextAligner.find_ms_for_char(self._sync_spans, cur.position()) if hasattr(AudioTextAligner, "find_ms_for_char") else AudioTextAligner.find_ms_at_char(self._sync_spans, cur.position())

    def wheelEvent(self, event):
        if self._audio_sync_active:
            self._auto_scroll_paused = True
        super().wheelEvent(event)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        cur_sel = self.textCursor()
        if cur_sel.hasSelection() and cur_sel.selectedText().strip():
            sel_md = self._selection_to_markdown(cur_sel) if hasattr(self, "_selection_to_markdown") else cur_sel.selectedText().replace("\u2029", "\n")
            from ui.icons import get_icon
            send_act = menu.addAction(get_icon("arrow_right", size=14), "Invia selezione al pannello affiancato")
            send_act.triggered.connect(lambda _chk=False, txt=sel_md: self.sendSelectionToSplitRequested.emit(txt))
        if self._audio_sync_active and not self._is_studio_view:
            cur = self.cursorForPosition(event.pos())
            target_ms = self._find_ms_for_cursor(cur)
            if target_ms is not None:
                first_act = menu.actions()[0] if menu.actions() else None
                listen_act = menu.addAction("🔊 Ascolta da questo punto")
                if first_act is not None:
                    menu.removeAction(listen_act)
                    menu.insertAction(first_act, listen_act)
                    menu.insertSeparator(first_act)
                listen_act.triggered.connect(lambda _chk=False, ms=target_ms: self._emit_seek(ms))
        menu.exec(event.globalPos())

    def _emit_seek(self, target_ms: int):
        self._auto_scroll_paused = False
        self.seekRequested.emit(int(target_ms))

    # -----------------------------------------------------------------
    # MOUSE INTERACTION (HOVER, DELETE BLOCK, MATRIX IN-PLACE, TO-DO)
    # -----------------------------------------------------------------
    def _find_matrix_at_point(self, pt: QPoint) -> Optional[Tuple[int, str]]:
        """Restituisce (frag_position, latex_source) se pt cade sopra una matrice renderizzata."""
        pt_f = QPointF(pt)
        cur = self.cursorForPosition(pt)
        block = cur.block()
        if not block.isValid():
            return None
        fit = block.begin()
        while not fit.atEnd():
            frag = fit.fragment()
            if frag.isValid():
                cf = frag.charFormat()
                if cf.hasProperty(PROP_LATEX_SOURCE):
                    tex = str(cf.property(PROP_LATEX_SOURCE) or "")
                    if re.search(r"\\begin\{[bpv]matrix\}", tex):
                        img_fmt = cf.toImageFormat()
                        m_rect = self._matrix_rect_in_viewport(
                            frag.position(), img_fmt.width(), img_fmt.height()
                        )
                        if m_rect.adjusted(-6, -12, 6, 6).contains(pt_f):
                            return frag.position(), tex
            fit += 1
        return None

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._is_studio_view:
            pt_f = QPointF(event.pos())
            new_act_idx = -1
            for idx, (rect, _kind, _target) in enumerate(self._block_action_rects):
                if rect.contains(pt_f):
                    new_act_idx = idx
                    break

            mat_hit = self._find_matrix_at_point(event.pos())
            new_mat_pos = mat_hit[0] if mat_hit is not None else None

            if new_act_idx != self._hovered_action_idx or new_mat_pos != self._hovered_matrix_pos:
                self._hovered_action_idx = new_act_idx
                self._hovered_matrix_pos = new_mat_pos
                if new_act_idx >= 0 or new_mat_pos is not None:
                    self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                self.viewport().update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if (
            self._audio_sync_active
            and not self._is_studio_view
            and event.button() == Qt.MouseButton.LeftButton
            and (event.modifiers() & Qt.KeyboardModifier.AltModifier)
        ):
            cur = self.cursorForPosition(event.pos())
            target_ms = self._find_ms_for_cursor(cur)
            if target_ms is not None:
                self._emit_seek(target_ms)
                return

        if (
            not self._is_studio_view
            and event.button() == Qt.MouseButton.LeftButton
            and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            cur = self.cursorForPosition(event.pos())
            blk_txt = cur.block().text()
            pos_in_blk = cur.positionInBlock()
            for m in re.finditer(r"\[\[([^\]\n]+)\]\]", blk_txt):
                if m.start() <= pos_in_blk <= m.end():
                    target_title = m.group(1).strip()
                    if target_title:
                        self.wikilinkClicked.emit(target_title)
                        return

        if not self._is_studio_view and event.button() == Qt.MouseButton.LeftButton:
            pt_f = QPointF(event.pos())
            for rect, action_kind, target in self._block_action_rects:
                if rect.contains(pt_f):
                    if action_kind == "toggle_todo":
                        blk = self.document().findBlockByNumber(int(target))
                        if blk.isValid():
                            self._toggle_todo_block(blk)
                    elif action_kind == "delete_frame":
                        self._delete_frame_undoable(target)
                    elif action_kind == "run_code":
                        self._run_code_frame(target)
                    elif action_kind == "delete_block":
                        self._delete_block_undoable(target)
                    elif action_kind == "delete_matrix":
                        self._delete_matrix_at_pos(target)
                    elif action_kind == "edit_matrix":
                        c = QTextCursor(self.document())
                        c.setPosition(target)
                        c.setPosition(target + 1, QTextCursor.MoveMode.KeepAnchor)
                        tex = str(c.charFormat().property(PROP_LATEX_SOURCE) or "")
                        if tex:
                            self._open_inline_matrix_editor(target, tex)
                    return

            mat_hit = self._find_matrix_at_point(event.pos())
            if mat_hit is not None:
                frag_pos, tex = mat_hit
                self._open_inline_matrix_editor(frag_pos, tex)
                return

            cur = self.cursorForPosition(event.pos())
            block = cur.block()
            kind = block.blockFormat().property(PROP_BLOCK_KIND)

            if kind == "ai_note":
                self._toggle_ai_note_block(block)
                return

            if kind == "file":
                fpath = block.blockFormat().property(PROP_FILE_PATH) or ""
                if fpath and os.path.exists(fpath):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(fpath))
                else:
                    self._open_file_picker_and_embed(target_block=block)
                return

        super().mousePressEvent(event)

    def _find_latex_fragment_at_pos(self, pos: int) -> Optional[Tuple[int, str]]:
        """Trova il singolo carattere immagine LaTeX (PROP_LATEX_SOURCE) vicino a pos."""
        doc = self.document()
        for probe_pos in (pos, pos - 1, pos + 1):
            if probe_pos < 0 or probe_pos >= doc.characterCount():
                continue
            block = doc.findBlock(probe_pos)
            if not block.isValid():
                continue
            fit = block.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid() and frag.position() <= probe_pos < frag.position() + frag.length():
                    cf = frag.charFormat()
                    if cf.hasProperty(PROP_LATEX_SOURCE):
                        return (frag.position(), str(cf.property(PROP_LATEX_SOURCE) or ""))
                fit += 1
        return None

    def _edit_latex_at_cursor(self, cur: QTextCursor, old_tex: str, frag_pos: Optional[int] = None):
        if frag_pos is None:
            hit = self._find_latex_fragment_at_pos(min(cur.selectionStart(), cur.selectionEnd()))
            if hit is not None:
                frag_pos, old_tex = hit
        if re.search(r"\\begin\{[bpv]matrix\}", old_tex):
            pos = frag_pos if frag_pos is not None else min(cur.selectionStart(), cur.selectionEnd())
            self._open_inline_matrix_editor(pos, old_tex)
            return

        anchor_cur = None
        if frag_pos is not None and 0 <= frag_pos < self.document().characterCount() - 1:
            anchor_cur = QTextCursor(self.document())
            anchor_cur.setPosition(frag_pos)
            anchor_cur.setPosition(frag_pos + 1, QTextCursor.MoveMode.KeepAnchor)

        dlg = LatexSourceEditDialog(
            old_tex,
            dark_mode=self._dark_mode,
            reading_mode=self._reading_mode,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_tex = dlg.latex_text()
            if not new_tex:
                return
            # Rimuovi eventuali delimitatori $ / $$ esterni inseriti dall'utente
            if new_tex.startswith("$$") and new_tex.endswith("$$") and len(new_tex) > 4:
                new_tex = new_tex[2:-2].strip()
            elif new_tex.startswith("$") and new_tex.endswith("$") and len(new_tex) > 2:
                new_tex = new_tex[1:-1].strip()

            c = QTextCursor(self.document())
            c.beginEditBlock()
            if anchor_cur is not None and anchor_cur.hasSelection():
                c.setPosition(min(anchor_cur.anchor(), anchor_cur.position()))
                c.setPosition(max(anchor_cur.anchor(), anchor_cur.position()), QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
            else:
                re_hit = self._find_latex_fragment_at_pos(frag_pos if frag_pos is not None else cur.position())
                if re_hit is not None:
                    c.setPosition(re_hit[0])
                    c.setPosition(re_hit[0] + 1, QTextCursor.MoveMode.KeepAnchor)
                    c.removeSelectedText()
                else:
                    c = QTextCursor(cur)
                    if c.hasSelection():
                        c.removeSelectedText()
            c.setCharFormat(QTextCharFormat())
            self._insert_latex_block_at_cursor(c, new_tex)
            c.endEditBlock()
            self.setTextCursor(c)
            self._on_internal_text_changed()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if not self._is_studio_view and event.button() == Qt.MouseButton.LeftButton:
            mat_hit = self._find_matrix_at_point(event.pos())
            if mat_hit is not None:
                event.accept()
                self._open_inline_matrix_editor(mat_hit[0], mat_hit[1])
                return
            cur = self.cursorForPosition(event.pos())
            hit = self._find_latex_fragment_at_pos(cur.position())
            if hit is not None:
                event.accept()
                frag_pos, old_tex = hit
                self._edit_latex_at_cursor(cur, old_tex, frag_pos=frag_pos)
                return
        super().mouseDoubleClickEvent(event)

    def _toggle_todo_block(self, block):
        if not block.isValid() or block.blockFormat().property(PROP_BLOCK_KIND) != "todo":
            return
        bf = block.blockFormat()
        checked = not bool(bf.property(PROP_TODO_CHECKED))
        bf.setProperty(PROP_TODO_CHECKED, checked)

        self._internal_update = True
        try:
            c = QTextCursor(block)
            c.beginEditBlock()
            c.setBlockFormat(bf)

            # Ripulisce eventuale prefisso legacy "☐ " / "☑ "
            txt = block.text()
            if txt.startswith("☐ ") or txt.startswith("☑ "):
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 2)
                c.removeSelectedText()

            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            txt_fmt = QTextCharFormat()
            if checked:
                txt_fmt.setFontStrikeOut(True)
                txt_fmt.setForeground(QColor("#64748B" if self._dark_mode else "#9CA3AF"))
                c.setBlockCharFormat(txt_fmt)
                c.mergeCharFormat(txt_fmt)
            else:
                txt_fmt.setFontStrikeOut(False)
                txt_fmt.clearForeground()
                c.setBlockCharFormat(txt_fmt)
                c.setCharFormat(txt_fmt)
            c.endEditBlock()
        finally:
            self._internal_update = False

        # Sincronizza il currentCharFormat se il cursore attivo è su questo blocco
        if self.textCursor().block() == block:
            cur_fmt = QTextCharFormat()
            if checked:
                cur_fmt.setFontStrikeOut(True)
                cur_fmt.setForeground(QColor("#64748B" if self._dark_mode else "#9CA3AF"))
            else:
                cur_fmt.setFontStrikeOut(False)
                cur_fmt.clearForeground()
            self.setCurrentCharFormat(cur_fmt)

        self.viewport().update()
        self._on_internal_text_changed()

    def _set_todo_indent(self, block, delta: int) -> bool:
        """Aumenta o riduce il livello di indentazione (0..4) di un blocco To-Do."""
        if not block.isValid() or block.blockFormat().property(PROP_BLOCK_KIND) != "todo":
            return False
        bf = block.blockFormat()
        cur_lvl = int(bf.property(PROP_TODO_INDENT) or 0)
        new_lvl = max(0, min(4, cur_lvl + int(delta)))
        if new_lvl == cur_lvl:
            return False
        bf.setProperty(PROP_TODO_INDENT, new_lvl)
        bf.setLeftMargin(self._todo_left_margin(new_lvl))
        self._internal_update = True
        try:
            c = QTextCursor(block)
            c.setBlockFormat(bf)
        finally:
            self._internal_update = False
        self.viewport().update()
        self._on_internal_text_changed()
        return True

    # -----------------------------------------------------------------
    # SLASH COMMANDS (/)
    # -----------------------------------------------------------------
    def _position_slash_popup(self):
        rect = self.cursorRect()
        global_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4))
        screen = QApplication.screenAt(global_pt) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pt.x() + self.slash_popup.width() > avail.right():
                global_pt.setX(max(avail.left(), avail.right() - self.slash_popup.width() - 8))
            if global_pt.y() + self.slash_popup.height() > avail.bottom():
                above_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.top()))
                global_pt.setY(max(avail.top(), above_pt.y() - self.slash_popup.height() - 4))
        self.slash_popup.move(global_pt)

    def _update_slash_filter(self):
        if self._slash_start_pos is None:
            self.slash_popup.hide()
            return
        cur_pos = self.textCursor().position()
        if cur_pos <= self._slash_start_pos:
            self._close_slash_popup()
            return
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(self._slash_start_pos)
        sel_cur.setPosition(cur_pos, QTextCursor.MoveMode.KeepAnchor)
        prefix = sel_cur.selectedText()
        if "\u2029" in prefix or "\n" in prefix or " " in prefix or not prefix.startswith("/"):
            self._close_slash_popup()
            return
        matches = self.slash_popup.filter_commands(prefix)
        if matches == 0:
            self._close_slash_popup()
        else:
            self._position_slash_popup()

    def _close_slash_popup(self):
        self._slash_start_pos = None
        self.slash_popup.hide()

    # -----------------------------------------------------------------
    # WIKILINK AUTOCOMPLETE ([[Titolo Nota]])
    # -----------------------------------------------------------------
    def _position_wikilink_popup(self):
        rect = self.cursorRect()
        global_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4))
        screen = QApplication.screenAt(global_pt) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pt.x() + self.wikilink_popup.width() > avail.right():
                global_pt.setX(max(avail.left(), avail.right() - self.wikilink_popup.width() - 8))
            if global_pt.y() + self.wikilink_popup.height() > avail.bottom():
                above_pt = self.viewport().mapToGlobal(QPoint(rect.left(), rect.top()))
                global_pt.setY(max(avail.top(), above_pt.y() - self.wikilink_popup.height() - 4))
        self.wikilink_popup.move(global_pt)

    def _close_wikilink_popup(self):
        self._wikilink_start_pos = None
        self.wikilink_popup.hide()

    def _update_wikilink_filter(self):
        if self._wikilink_start_pos is None:
            self.wikilink_popup.hide()
            return
        cur_pos = self.textCursor().position()
        if cur_pos < self._wikilink_start_pos + 2:
            self._close_wikilink_popup()
            return
        sel_cur = QTextCursor(self.document())
        sel_cur.setPosition(self._wikilink_start_pos)
        sel_cur.setPosition(cur_pos, QTextCursor.MoveMode.KeepAnchor)
        raw = sel_cur.selectedText()
        if "\u2029" in raw or "\n" in raw or "]]" in raw or not raw.startswith("[["):
            self._close_wikilink_popup()
            return
        prefix = raw[2:]
        matches = self.wikilink_popup.filter_notes(prefix)
        if matches == 0:
            self.wikilink_popup.hide()
        else:
            self._position_wikilink_popup()
            if not self.wikilink_popup.isVisible():
                self.wikilink_popup.show()

    def _apply_wikilink(self, title: str):
        clean_t = (title or "").strip()
        if not clean_t:
            self._close_wikilink_popup()
            return
        cursor = self.textCursor()
        if self._wikilink_start_pos is not None and cursor.position() >= self._wikilink_start_pos:
            cursor.beginEditBlock()
            cursor.setPosition(self._wikilink_start_pos, QTextCursor.MoveMode.MoveAnchor)
            cursor.setPosition(self.textCursor().position(), QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(f"[[{clean_t}]]", QTextCharFormat())
            cursor.endEditBlock()
            self.setTextCursor(cursor)
        self._close_wikilink_popup()
        self._on_internal_text_changed()

    def _apply_slash_command(self, cmd: str):
        cursor = self.textCursor()
        if self._slash_start_pos is not None and cursor.position() >= self._slash_start_pos:
            cursor.setPosition(self._slash_start_pos, QTextCursor.MoveMode.MoveAnchor)
            cursor.setPosition(self.textCursor().position(), QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        self._close_slash_popup()

        block_commands = {
            "/formula", "/matrice", "/matrix", "/tabella", "/quote", "/callout",
            "/todo", "/elenco", "/h1", "/h2", "/h3", "/code", "/bibtex", "/file",
        }
        if cmd in block_commands and cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)

        if cmd == "/tabella":
            self.setTextCursor(cursor)
            self.insert_table_interactive()
        elif cmd == "/quote":
            self._apply_quote_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/callout":
            self._apply_callout_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/todo":
            self._apply_todo_block_at_cursor(cursor, "", checked=False)
            self.setTextCursor(cursor)
        elif cmd == "/elenco":
            self._apply_bullet_block_at_cursor(cursor, "", indent_level=0)
            self.setTextCursor(cursor)
        elif cmd == "/code":
            self._insert_code_frame_at_cursor(cursor, "", lang="python")
        elif cmd == "/bibtex":
            self.setTextCursor(cursor)
            self.insert_bibtex_block()
        elif cmd == "/file":
            self._apply_file_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/immagine":
            self.insert_image_interactive()
        elif cmd == "/formula":
            self.setTextCursor(cursor)
            self.insert_formula_block()
        elif cmd in ("/matrice", "/matrix"):
            self.setTextCursor(cursor)
            self.insert_matrix_interactive()
        elif cmd == "/ocr":
            self.setTextCursor(cursor)
            self.trigger_vision_ocr()
        elif cmd == "/h1":
            self._apply_heading_block_at_cursor(cursor, level=1, text="")
            self.setTextCursor(cursor)
        elif cmd == "/h2":
            self._apply_heading_block_at_cursor(cursor, level=2, text="")
            self.setTextCursor(cursor)
        elif cmd == "/h3":
            self._apply_heading_block_at_cursor(cursor, level=3, text="")
            self.setTextCursor(cursor)
        elif cmd == "/riassumi":
            self.setTextCursor(cursor)
            self.summarizeRequested.emit()
        elif cmd == "/flashcard":
            self.setTextCursor(cursor)
            self.flashcardsRequested.emit()
        elif cmd in ("/rsvp", "/speedread"):
            self.setTextCursor(cursor)
            self.rsvpRequested.emit(cursor.blockNumber(), False)

    # -----------------------------------------------------------------
    # TASTIERA: TAB CALC/TABELLA, BACKSPACE BLOCCHI, AUTO-TODO/QUOTE, SLASH, =
    # -----------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()

        # Scorciatoia RSVP (Alt+R): selezione se attiva, altrimenti dal blocco cursore a fine nota
        if key == Qt.Key.Key_R and (mods & Qt.KeyboardModifier.AltModifier) and not (mods & Qt.KeyboardModifier.ControlModifier):
            cur = self.textCursor()
            if cur.hasSelection() and cur.selectedText().strip():
                start_cur = QTextCursor(self.document())
                start_cur.setPosition(min(cur.selectionStart(), cur.selectionEnd()))
                self._selection_toolbar.hide()
                self.rsvpRequested.emit(start_cur.blockNumber(), True)
            else:
                self.rsvpRequested.emit(cur.blockNumber(), False)
            event.accept()
            return

        if self._is_studio_view:
            super().keyPressEvent(event)
            return

        # 0-pre. Scorciatoie inline rapide (Ctrl+B Grassetto, Ctrl+I Corsivo, Ctrl+U Sottolineato)
        if (
            (mods & Qt.KeyboardModifier.ControlModifier)
            and not (mods & Qt.KeyboardModifier.AltModifier)
            and not (mods & Qt.KeyboardModifier.ShiftModifier)
        ):
            if key == Qt.Key.Key_B:
                self.toggle_bold_selection()
                event.accept()
                return
            if key == Qt.Key.Key_I:
                self.toggle_italic_selection()
                event.accept()
                return
            if key == Qt.Key.Key_U:
                self.toggle_underline_selection()
                event.accept()
                return

        # 0. Scorciatoia Play/Pausa Audio-Sync (Ctrl+Spazio) nelle sbobine
        if self._audio_sync_active and key == Qt.Key.Key_Space and (mods & Qt.KeyboardModifier.ControlModifier):
            self.togglePlayRequested.emit()
            return

        # 0a. Vision OCR istantaneo da appunti (Ctrl+Shift+V) — lascia intatto Ctrl+V
        if (
            key == Qt.Key.Key_V
            and (mods & Qt.KeyboardModifier.ControlModifier)
            and (mods & Qt.KeyboardModifier.ShiftModifier)
            and not (mods & Qt.KeyboardModifier.AltModifier)
        ):
            self.trigger_vision_ocr()
            return

        # 0a-bis. Esecuzione inline blocco codice Python (Ctrl+Enter) o toggle To-Do (Ctrl+Enter / Ctrl+Shift+X)
        if (
            key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and (mods & Qt.KeyboardModifier.ControlModifier)
            and not (mods & Qt.KeyboardModifier.ShiftModifier)
        ):
            chk_frame = self.textCursor().currentFrame()
            if chk_frame is not None and chk_frame.frameFormat().property(PROP_FRAME_KIND) in ("python", "py"):
                self._run_code_frame(chk_frame)
                return
            cur_blk = self.textCursor().block()
            if cur_blk.isValid() and cur_blk.blockFormat().property(PROP_BLOCK_KIND) == "todo":
                self._toggle_todo_block(cur_blk)
                return

        if (
            key == Qt.Key.Key_X
            and (mods & Qt.KeyboardModifier.ControlModifier)
            and (mods & Qt.KeyboardModifier.ShiftModifier)
        ):
            cur_blk = self.textCursor().block()
            if cur_blk.isValid() and cur_blk.blockFormat().property(PROP_BLOCK_KIND) == "todo":
                self._toggle_todo_block(cur_blk)
                return

        # 0b. Accetta suggerimento Ghost-Text di Auto-Calcolo (=) con Tab
        if self._inline_calc_suggestion is not None:
            if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ShiftModifier):
                sug = self._inline_calc_suggestion
                self._inline_calc_suggestion = None
                c = self.textCursor()
                c.beginEditBlock()
                if sug.get("kind") == "matrix":
                    c.insertText(" ")
                    self._insert_latex_block_at_cursor(c, sug["latex"])
                else:
                    c.insertText(sug.get("insert_text", ""))
                c.endEditBlock()
                self.setTextCursor(c)
                self._on_internal_text_changed()
                self.viewport().update()
                return
            elif key not in (
                Qt.Key.Key_Shift,
                Qt.Key.Key_Control,
                Qt.Key.Key_Alt,
                Qt.Key.Key_Meta,
            ):
                self._inline_calc_suggestion = None
                self.viewport().update()

        # 0c. Accetta anteprima Ghost-Text della dettatura FlowType con Tab su riga vuota
        if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ShiftModifier) and not self.slash_popup.isVisible():
            c_chk = self.textCursor()
            if c_chk.currentTable() is None and c_chk.block().text() == "" and not c_chk.block().blockFormat().property(PROP_BLOCK_KIND):
                preview = dictation_tracker.get_active_preview()
                if preview is not None:
                    full_text = dictation_tracker.consume()
                    if full_text:
                        c_chk.beginEditBlock()
                        c_chk.insertText(full_text)
                        c_chk.endEditBlock()
                        self.setTextCursor(c_chk)
                        self._on_internal_text_changed()
                        self.viewport().update()
                        return

        # 1. Menu Slash attivo
        if self.slash_popup.isVisible():
            if key == Qt.Key.Key_Down:
                nxt = min(self.slash_popup.count() - 1, self.slash_popup.currentRow() + 1)
                self.slash_popup.setCurrentRow(nxt)
                return
            if key == Qt.Key.Key_Up:
                prv = max(0, self.slash_popup.currentRow() - 1)
                self.slash_popup.setCurrentRow(prv)
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                cmd = self.slash_popup.current_command()
                if cmd:
                    self._apply_slash_command(cmd)
                return
            if key == Qt.Key.Key_Escape:
                self._close_slash_popup()
                return

        # 1b. Menu Wikilink [[ attivo
        if self.wikilink_popup.isVisible():
            if key == Qt.Key.Key_Down:
                nxt = min(self.wikilink_popup.count() - 1, self.wikilink_popup.currentRow() + 1)
                self.wikilink_popup.setCurrentRow(nxt)
                return
            if key == Qt.Key.Key_Up:
                prv = max(0, self.wikilink_popup.currentRow() - 1)
                self.wikilink_popup.setCurrentRow(prv)
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                chosen_title = self.wikilink_popup.current_title()
                if chosen_title:
                    self._apply_wikilink(chosen_title)
                else:
                    self._close_wikilink_popup()
                return
            if key == Qt.Key.Key_Escape:
                self._close_wikilink_popup()
                return

        cursor = self.textCursor()

        # 2. Navigazione Tab / Shift+Tab nelle QTextTable
        table = cursor.currentTable()
        if table is not None and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            cell = table.cellAt(cursor)
            r, c = cell.row(), cell.column()
            is_back = (key == Qt.Key.Key_Backtab) or bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if is_back:
                if r > 0 or c > 0:
                    pc = (c - 1) if c > 0 else (table.columns() - 1)
                    pr = r if c > 0 else (r - 1)
                    self.setTextCursor(table.cellAt(pr, pc).firstCursorPosition())
                return
            else:
                if r == table.rows() - 1 and c == table.columns() - 1:
                    table.appendRows(1)
                    self.setTextCursor(table.cellAt(r + 1, 0).firstCursorPosition())
                else:
                    nc = (c + 1) % table.columns()
                    nr = r + (1 if nc == 0 else 0)
                    self.setTextCursor(table.cellAt(nr, nc).firstCursorPosition())
                return

        # 3. Uscita da QTextFrame (Code / BibTeX) o eliminazione su Backspace se vuoto
        cur_frame = cursor.currentFrame()
        if cur_frame is not None and cur_frame.frameFormat().hasProperty(PROP_FRAME_KIND):
            if key == Qt.Key.Key_Backspace and not cursor.hasSelection():
                if cursor.position() == cur_frame.firstPosition():
                    frame_txt = ""
                    fit = cur_frame.begin()
                    while not fit.atEnd():
                        fb = fit.currentBlock()
                        if fb.isValid():
                            frame_txt += fb.text()
                        fit += 1
                    if frame_txt.strip() == "":
                        self._delete_frame_undoable(cur_frame)
                        return

            is_exit_shortcut = (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and bool(mods & Qt.KeyboardModifier.ShiftModifier))
            at_last_block = (cursor.block() == cur_frame.lastCursorPosition().block())
            if is_exit_shortcut or (key == Qt.Key.Key_Down and at_last_block):
                exit_cur = cur_frame.lastCursorPosition()
                if not exit_cur.movePosition(QTextCursor.MoveOperation.NextBlock):
                    exit_cur.movePosition(QTextCursor.MoveOperation.End)
                if exit_cur.currentFrame() == cur_frame:
                    root_cur = self.document().rootFrame().lastCursorPosition()
                    root_cur.insertBlock()
                    root_cur.setBlockFormat(QTextBlockFormat())
                    root_cur.setCharFormat(QTextCharFormat())
                    root_cur.block().setUserData(None)
                    self.setTextCursor(root_cur)
                else:
                    self.setTextCursor(exit_cur)
                return

        # 3b. Indentazione Tab / Shift+Tab nei blocchi To-Do ed Elenco Puntato
        block = cursor.block()
        b_kind = block.blockFormat().property(PROP_BLOCK_KIND)
        if b_kind in ("todo", "bullet") and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and not self.slash_popup.isVisible():
            is_back = (key == Qt.Key.Key_Backtab) or bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if b_kind == "todo":
                self._set_todo_indent(block, -1 if is_back else 1)
            else:
                self._set_bullet_indent(block, -1 if is_back else 1)
            return

        # 3c. Backspace all'inizio di un blocco speciale -> de-indenta / torna a paragrafo
        if key == Qt.Key.Key_Backspace and not cursor.hasSelection() and b_kind:
            if b_kind == "bullet" and cursor.atBlockStart():
                cur_lvl = int(block.blockFormat().property(PROP_BULLET_INDENT) or 0)
                if cur_lvl > 0:
                    self._set_bullet_indent(block, -1)
                    return
                cursor.beginEditBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                block.setUserData(None)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                self.viewport().update()
                self._on_internal_text_changed()
                return
            if b_kind == "todo" and cursor.atBlockStart():
                cur_lvl = int(block.blockFormat().property(PROP_TODO_INDENT) or 0)
                if cur_lvl > 0:
                    self._set_todo_indent(block, -1)
                    return
                if block.text().strip() == "":
                    cursor.beginEditBlock()
                    cursor.setBlockFormat(QTextBlockFormat())
                    cursor.setCharFormat(QTextCharFormat())
                    block.setUserData(None)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self.viewport().update()
                    self._on_internal_text_changed()
                    return
            if cursor.atBlockStart() and block.text() == "":
                if b_kind in ("callout", "file", "ai_note") and self.document().blockCount() > 1 and block.previous().isValid():
                    self._delete_block_undoable(block.blockNumber())
                else:
                    cursor.beginEditBlock()
                    cursor.setBlockFormat(QTextBlockFormat())
                    cursor.setCharFormat(QTextCharFormat())
                    block.setUserData(None)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self._on_internal_text_changed()
                return

        # 4. Invio in blocco To-Do, Elenco, Quote, Callout, Heading, File, AI Note
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (mods & Qt.KeyboardModifier.ShiftModifier):
            if b_kind == "bullet":
                b_txt = block.text().strip()
                clean_fmt = QTextCharFormat()
                clean_fmt.setFontStrikeOut(False)
                clean_fmt.clearForeground()
                if b_txt == "":
                    cursor.beginEditBlock()
                    cursor.setBlockFormat(QTextBlockFormat())
                    cursor.setBlockCharFormat(clean_fmt)
                    cursor.setCharFormat(clean_fmt)
                    block.setUserData(None)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self.setCurrentCharFormat(clean_fmt)
                    self.viewport().update()
                    self._on_internal_text_changed()
                    return
                else:
                    cur_lvl = int(block.blockFormat().property(PROP_BULLET_INDENT) or 0)
                    cursor.beginEditBlock()
                    cursor.insertBlock()
                    self._apply_bullet_block_at_cursor(cursor, "", indent_level=cur_lvl)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self.setCurrentCharFormat(clean_fmt)
                    self.viewport().update()
                    return
            if b_kind == "todo":
                b_txt = block.text().strip()
                clean_fmt = QTextCharFormat()
                clean_fmt.setFontStrikeOut(False)
                clean_fmt.clearForeground()
                if b_txt == "":
                    # Esci dalla lista To-Do con formato testo pulito (nessun residuo strikeout)
                    cursor.beginEditBlock()
                    cursor.setBlockFormat(QTextBlockFormat())
                    cursor.setBlockCharFormat(clean_fmt)
                    cursor.setCharFormat(clean_fmt)
                    block.setUserData(None)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self.setCurrentCharFormat(clean_fmt)
                    self.viewport().update()
                    return
                else:
                    cur_lvl = int(block.blockFormat().property(PROP_TODO_INDENT) or 0)
                    cursor.beginEditBlock()
                    cursor.insertBlock()
                    self._apply_todo_block_at_cursor(cursor, "", checked=False, indent_level=cur_lvl)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    self.setCurrentCharFormat(clean_fmt)
                    self.viewport().update()
                    return
            elif b_kind in ("quote", "callout") and block.text().strip() == "":
                cursor.beginEditBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                block.setUserData(None)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                return
            elif b_kind in ("h1", "h2", "h3", "h4", "file", "ai_note"):
                cursor.insertBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                cursor.block().setUserData(None)
                self.setTextCursor(cursor)
                return

        # 5. Auto-conversione "> " -> Quote, "[] " -> To-Do, "# "/"## "/"### "/"#### " -> H1/H2/H3/H4
        if key == Qt.Key.Key_Space and not b_kind:
            before_txt = block.text()[: cursor.positionInBlock()]
            if before_txt in ("#", "##", "###", "####"):
                lvl = len(before_txt)
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, len(before_txt))
                cursor.removeSelectedText()
                self._apply_heading_block_at_cursor(cursor, level=lvl, text="")
                self.setTextCursor(cursor)
                self.setCurrentCharFormat(self._heading_char_format(lvl))
                return
            if before_txt == ">":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1)
                cursor.removeSelectedText()
                self._apply_quote_block_at_cursor(cursor, "")
                self.setTextCursor(cursor)
                return
            if before_txt in ("-", "*"):
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1)
                cursor.removeSelectedText()
                self._apply_bullet_block_at_cursor(cursor, "", indent_level=0)
                self.setTextCursor(cursor)
                self.viewport().update()
                self._on_internal_text_changed()
                return
            if before_txt == "[]":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 2)
                cursor.removeSelectedText()
                self._apply_todo_block_at_cursor(cursor, "", checked=False)
                self.setTextCursor(cursor)
                clean_fmt = QTextCharFormat()
                clean_fmt.setFontStrikeOut(False)
                clean_fmt.clearForeground()
                self.setCurrentCharFormat(clean_fmt)
                return

        # 6. Apertura Slash Command Popup su '/'
        if event.text() == "/":
            pos = cursor.position()
            pos_in_block = cursor.positionInBlock()
            block_text = cursor.block().text()
            at_boundary = (pos_in_block == 0) or (
                pos_in_block <= len(block_text) and block_text[pos_in_block - 1] in (" ", "\t")
            )
            super().keyPressEvent(event)
            if at_boundary:
                self._slash_start_pos = pos
                self.slash_popup.filter_commands("/")
                self._position_slash_popup()
                self.slash_popup.show()
            return

        # 6b. Apertura Wikilink Popup su '[['
        if event.text() == "[":
            pos_in_block = cursor.positionInBlock()
            block_text = cursor.block().text()
            has_prev_bracket = (pos_in_block >= 1 and block_text[pos_in_block - 1] == "[")
            start_pos = cursor.position() - 1
            super().keyPressEvent(event)
            if has_prev_bracket:
                self._wikilink_start_pos = start_pos
                if self.wikilink_popup.filter_notes("") > 0:
                    self._position_wikilink_popup()
                    self.wikilink_popup.show()
            return

        # Guardia anti-strikethrough residuo: evita che digitare su un To-Do non spuntato o testo normale
        # erediti il barrato/grigio da una riga To-Do completata precedente.
        if event.text() and event.text().isprintable():
            is_checked_todo = (b_kind == "todo") and bool(block.blockFormat().property(PROP_TODO_CHECKED))
            if not is_checked_todo and self.currentCharFormat().fontStrikeOut():
                clean_cf = QTextCharFormat(self.currentCharFormat())
                clean_cf.setFontStrikeOut(False)
                clean_cf.clearForeground()
                self.setCurrentCharFormat(clean_cf)

        super().keyPressEvent(event)

        if self.slash_popup.isVisible():
            self._update_slash_filter()
            return

        if self._wikilink_start_pos is not None:
            self._update_wikilink_filter()
            return

        # 7. Auto-Calcolo Inline alla digitazione di '='
        if event.text() == "=":
            c = self.textCursor()
            b = c.block()
            before_eq = b.text()[: max(0, c.positionInBlock() - 1)]
            matrices = self._collect_labeled_matrices()
            res = evaluate_inline_equals(before_eq, matrices)
            if res is not None:
                res["pos"] = c.position()
                self._inline_calc_suggestion = res
                self.viewport().update()

    # -----------------------------------------------------------------
    # BLOCCO 3B: VISION OCR ISTANTANEO (/ocr & Ctrl+Shift+V)
    # -----------------------------------------------------------------
    def _show_discrete_toast(self, message: str, duration_ms: int = 2600):
        if self._toast_label is None:
            lbl = QLabel(self.viewport())
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._toast_label = lbl
        lbl = self._toast_label
        bg = "#1E293B" if self._dark_mode else "#0F172A"
        bd = "#334155" if self._dark_mode else "#1E293B"
        lbl.setStyleSheet(
            f"QLabel {{ background-color: {bg}; color: #F8FAFC; border: 1px solid {bd}; "
            f"border-radius: 6px; padding: 6px 12px; font-size: 11px; }}"
        )
        lbl.setText(message)
        lbl.adjustSize()
        px = max(12, (self.viewport().width() - lbl.width()) // 2)
        py = max(12, self.viewport().height() - lbl.height() - 16)
        lbl.move(px, py)
        lbl.show()
        lbl.raise_()
        self._toast_timer.start(duration_ms)

    def _hide_discrete_toast(self):
        if self._toast_label is not None:
            self._toast_label.hide()

    def trigger_vision_ocr(self):
        if self._is_studio_view:
            return
        cb = QApplication.clipboard()
        qimg = cb.image() if cb is not None else QImage()
        if qimg is None or qimg.isNull():
            self._show_discrete_toast("Nessuno screenshot trovato negli appunti")
            return

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        qimg.save(buf, "PNG")
        png_bytes = bytes(buf.data())
        buf.close()
        if not png_bytes:
            self._show_discrete_toast("Impossibile leggere l'immagine dagli appunti")
            return

        placeholder_id = uuid.uuid4().hex[:12]
        cur = self.textCursor()
        cur.beginEditBlock()
        cf = QTextCharFormat()
        cf.setForeground(QColor("#94A3B8" if self._dark_mode else "#64748B"))
        cf.setFontItalic(True)
        cf.setProperty(PROP_OCR_PLACEHOLDER_ID, placeholder_id)
        cur.insertText("⏳ Estrazione formula / tabella in corso...", cf)
        cur.endEditBlock()
        self.setTextCursor(cur)

        worker = _VisionOcrWorker(placeholder_id, png_bytes, parent=self)
        self._active_ocr_workers.append(worker)
        worker.ocrCompleted.connect(self._on_vision_ocr_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda w=worker: self._cleanup_worker(self._active_ocr_workers, w))
        worker.start()

    def _cleanup_worker(self, pool: list, worker: QThread):
        if worker in pool:
            pool.remove(worker)
        worker.deleteLater()

    def _find_ocr_placeholder_range(self, placeholder_id: str) -> Optional[Tuple[int, int]]:
        doc = self.document()
        block = doc.begin()
        start_pos = None
        end_pos = None
        while block.isValid():
            fit = block.begin()
            while not fit.atEnd():
                frag = fit.fragment()
                if frag.isValid():
                    if frag.charFormat().property(PROP_OCR_PLACEHOLDER_ID) == placeholder_id:
                        if start_pos is None:
                            start_pos = frag.position()
                        end_pos = frag.position() + frag.length()
                fit += 1
            block = block.next()
        if start_pos is not None and end_pos is not None:
            return (start_pos, end_pos)
        return None

    @staticmethod
    def _parse_markdown_table(md_text: str) -> Optional[List[List[str]]]:
        lines = [ln.strip() for ln in md_text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        if not all("|" in ln for ln in lines[:2]):
            return None
        sep_re = re.compile(r"^\|?[\s:\-]+\|[\s:\-\|]*$")
        if not sep_re.match(lines[1]):
            return None
        rows: List[List[str]] = []
        for idx, ln in enumerate(lines):
            if idx == 1 and sep_re.match(ln):
                continue
            if not "|" in ln:
                return None
            parts = ln.strip("|").split("|")
            rows.append([p.strip() for p in parts])
        return rows if len(rows) >= 1 else None

    def _on_vision_ocr_finished(self, placeholder_id: str, result_text: str, used_fallback: bool, error_msg: str):
        rng = self._find_ocr_placeholder_range(placeholder_id)
        if rng is None:
            return
        start_pos, end_pos = rng
        cur = QTextCursor(self.document())
        cur.beginEditBlock()
        cur.setPosition(start_pos)
        cur.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        cur.setCharFormat(QTextCharFormat())

        if error_msg or not result_text.strip():
            cur.endEditBlock()
            self._show_discrete_toast(error_msg or "Nessun risultato estratto")
            return

        raw = result_text.strip()
        # Rimuovi eventuale fence markdown ```latex / ```markdown
        fence_m = re.match(r"^```[a-zA-Z]*\s*\n([\s\S]*?)\n```$", raw)
        if fence_m:
            raw = fence_m.group(1).strip()

        # Normalizza \[ ... \] -> $$ ... $$
        if raw.startswith("\\[") and raw.endswith("\\]"):
            raw = f"$${raw[2:-2].strip()}$$"

        # 1. Singola matrice -> Matrice Interattiva Blocco 3A
        inner_no_dollars = re.sub(r"^\$\$\s*|\s*\$\$$", "", raw).strip()
        if re.fullmatch(r"\\begin\{[bpv]matrix\}[\s\S]*?\\end\{[bpv]matrix\}", inner_no_dollars):
            self._insert_latex_block_at_cursor(cur, inner_no_dollars)
            cur.endEditBlock()
            self.setTextCursor(cur)
            self._on_internal_text_changed()
            return

        # 2. Tabella Markdown -> QTextTable interattiva
        tbl_rows = self._parse_markdown_table(raw)
        if tbl_rows is not None:
            tbl = self._insert_table_at_cursor(cur, data=tbl_rows)
            cur.endEditBlock()
            self.setTextCursor(tbl.cellAt(0, 0).firstCursorPosition())
            self._on_internal_text_changed()
            return

        # 3. Formula / equazioni LaTeX -> render inline
        if (raw.startswith("$$") and raw.endswith("$$")) or (raw.startswith("$") and raw.endswith("$") and raw.count("$") == 2):
            tex_inner = raw.strip("$").strip()
            if "\n" in tex_inner and "\\begin{" not in tex_inner:
                for line_tex in [ln.strip() for ln in tex_inner.splitlines() if ln.strip()]:
                    self._insert_latex_block_at_cursor(cur, line_tex)
                    cur.insertText("\n")
            else:
                self._insert_latex_block_at_cursor(cur, tex_inner)
            cur.endEditBlock()
            self.setTextCursor(cur)
            self._on_internal_text_changed()
            return

        # 4. Testo semplice (es. fallback RapidOCR)
        cur.insertText(raw)
        cur.endEditBlock()
        self.setTextCursor(cur)
        self._on_internal_text_changed()
        if used_fallback:
            self._show_discrete_toast("Estratto con OCR locale (RapidOCR)")

    # -----------------------------------------------------------------
    # BLOCCO 3B: ESECUZIONE INLINE PYTHON (▶ Esegui & Ctrl+Enter)
    # -----------------------------------------------------------------
    def _get_or_assign_frame_code_id(self, frame: QTextFrame) -> str:
        cid = str(frame.frameFormat().property(PROP_CODE_ID) or "")
        if not cid:
            cid = uuid.uuid4().hex[:12]
            fmt = frame.frameFormat()
            fmt.setProperty(PROP_CODE_ID, cid)
            frame.setFrameFormat(fmt)
        return cid

    @staticmethod
    def _extract_frame_text(frame: QTextFrame) -> str:
        lines: List[str] = []
        fit = frame.begin()
        while not fit.atEnd():
            fb = fit.currentBlock()
            if fb.isValid():
                lines.append(fb.text())
            fit += 1
        return "\n".join(lines)

    def _run_code_frame(self, frame: QTextFrame):
        if frame is None or self._is_studio_view:
            return
        code_id = self._get_or_assign_frame_code_id(frame)
        cur_state = self._code_exec_states.get(code_id, {})
        if cur_state.get("status") == "running":
            return

        code_text = self._extract_frame_text(frame)
        self._code_exec_states[code_id] = {"status": "running", "elapsed": 0.0}
        self.viewport().update()

        worker = _PythonExecWorker(code_id, code_text, parent=self)
        self._active_exec_workers.append(worker)
        worker.execCompleted.connect(self._on_python_exec_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda w=worker: self._cleanup_worker(self._active_exec_workers, w))
        worker.start()

    def _on_python_exec_finished(
        self, code_id: str, stdout: str, stderr: str, plot_paths: list, elapsed: float, ok: bool
    ):
        self._code_exec_states[code_id] = {
            "status": "done" if ok else "error",
            "elapsed": elapsed,
        }
        panel = self._code_output_panels.get(code_id)
        if panel is None:
            panel = CodeOutputPanel(
                code_id, parent=self.viewport(), dark_mode=self._dark_mode, reading_mode=self._reading_mode
            )
            panel.closeRequested.connect(self._close_code_output_panel)
            panel.insertPlotRequested.connect(self._insert_plot_image_after_frame)
            self._code_output_panels[code_id] = panel
        panel.set_result(stdout, stderr, plot_paths, elapsed, ok)
        panel.show()
        self.viewport().update()

    def _close_code_output_panel(self, code_id: str):
        panel = self._code_output_panels.pop(code_id, None)
        if panel is not None:
            panel.hide()
            panel.deleteLater()
        self.viewport().update()

    def _insert_plot_image_after_frame(self, code_id: str, plot_path: str):
        if not plot_path or not os.path.exists(plot_path):
            return
        qimg = QImage(plot_path)
        if qimg.isNull():
            return
        target_frame = None
        for frame in self.document().rootFrame().childFrames():
            if str(frame.frameFormat().property(PROP_CODE_ID) or "") == code_id:
                target_frame = frame
                break
        if target_frame is None:
            return

        exit_cur = target_frame.lastCursorPosition()
        if not exit_cur.movePosition(QTextCursor.MoveOperation.NextBlock) or exit_cur.currentFrame() == target_frame:
            exit_cur = self.document().rootFrame().lastCursorPosition()
            exit_cur.insertBlock()
            exit_cur.setBlockFormat(QTextBlockFormat())
            exit_cur.setCharFormat(QTextCharFormat())
        self.setTextCursor(exit_cur)
        self._save_and_insert_image(qimg)
        self._show_discrete_toast("Grafico inserito nella nota")
