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
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
from matplotlib.mathtext import math_to_image

from PyQt6.QtCore import (
    Qt, QBuffer, QEvent, QIODevice, QMimeData, QObject, QPoint, QPointF, QRectF,
    QRegularExpression, QUrl, pyqtSignal
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
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
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.math_eval import evaluate_inline_equals, parse_latex_matrix
from core.clipboard_tracker import dictation_tracker


BASE_DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

PLACEHOLDER_TEXTS: Dict[str, str] = {
    "h1": "Titolo 1",
    "h2": "Titolo 2",
    "h3": "Titolo 3",
    "todo": "To-do",
    "quote": "Citazione...",
    "callout": "Nota in evidenza...",
}


class BlockUserData(QTextBlockUserData):
    """Metadati persistenti per blocco (placeholder ghost-text senza sporcare QTextDocument)."""

    def __init__(self, placeholder_kind: str = ""):
        super().__init__()
        self.placeholder_kind: str = placeholder_kind


def strip_markdown_syntax(text: str) -> str:
    """Rimuove tag Markdown (#, ##, **, $$, ```, :::ai-note) per titoli e anteprime pulite nella sidebar."""
    if not text:
        return ""
    s = re.sub(r":::ai-note[^\n]*\n?", "", text)
    s = s.replace(":::", "")
    s = re.sub(r"```[a-zA-Z]*", "", s)
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


def _mathtext_to_transparent_qimage(expr: str, dpi: int, ink_hex: str) -> Optional[QImage]:
    """
    Renderizza un'espressione LaTeX con matplotlib.mathtext su sfondo 100% trasparente
    nel colore `ink_hex`, eliminando il rettangolo bianco opaco di default.
    """
    import numpy as np

    buf = io.BytesIO()
    math_to_image(f"${expr}$", buf, dpi=dpi, format="png", color="#000000")
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
            escaped = html.escape(tok)
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
        elif re.match(r"^[-*]\s+\[([ xX])\]\s+", stripped):
            _close_lists()
            m_todo = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", stripped)
            checked = (m_todo.group(1).lower() == "x")
            txt_html = _render_inline_markdown_html(m_todo.group(2), dark_mode, reading_mode, font_scale_pct)
            glyph = "☑" if checked else "☐"
            style = f"text-decoration: line-through; color: {fg_muted};" if checked else f"color: {fg_text};"
            blocks.append(f'<div style="margin: 3px 0; {style}">{glyph} {txt_html}</div>')
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
        h1_fg = "#F8FAFC" if self._dark_mode else "#0F172A"
        h2_fg = "#E2E8F0" if self._dark_mode else "#1E293B"
        h3_fg = "#CBD5E1" if self._dark_mode else "#334155"
        bold_fg = "#FFFFFF" if self._dark_mode else "#0F172A"
        code_fg = "#CBD5E1" if self._dark_mode else "#334155"
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


SLASH_COMMANDS: List[Dict[str, str]] = [
    {"cmd": "/tabella", "label": "▦  /tabella", "desc": "Tabella interattiva Notion (celle navigabili con Tab)"},
    {"cmd": "/quote", "label": "▎  /quote", "desc": "Blocco citazione con barra verticale"},
    {"cmd": "/callout", "label": "💡 /callout", "desc": "Nota in evidenza stile Notion"},
    {"cmd": "/todo", "label": "☐  /todo", "desc": "To-Do list interattiva cliccabile"},
    {"cmd": "/code", "label": "⌘  /code", "desc": "Blocco codice a larghezza piena"},
    {"cmd": "/bibtex", "label": "❝  /bibtex", "desc": "Blocco citazione BibTeX accademico"},
    {"cmd": "/file", "label": "📎 /file", "desc": "Upload or embed a file (PDF, Immagine, Doc)"},
    {"cmd": "/immagine", "label": "🖼️ /immagine", "desc": "Incorpora immagine da file"},
    {"cmd": "/formula", "label": "∑  /formula", "desc": "Blocco equazione LaTeX ($$ ... $$)"},
    {"cmd": "/matrice", "label": "⊞  /matrice", "desc": "Matrice interattiva modificabile (bmatrix)"},
    {"cmd": "/h1", "label": "H1 /h1", "desc": "Titolo grande di sezione"},
    {"cmd": "/h2", "label": "H2 /h2", "desc": "Sottotitolo di sezione"},
    {"cmd": "/h3", "label": "H3 /h3", "desc": "Titolo di terzo livello"},
    {"cmd": "/riassumi", "label": "✦  /riassumi", "desc": "Riassumi nota con Qwen 2.5 AI"},
    {"cmd": "/flashcard", "label": "🃏 /flashcard", "desc": "Genera flashcard d'esame"},
]


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
        self.apply_theme(dark_mode)
        self.itemClicked.connect(self._on_item_clicked)
        self.filter_commands("")

    def apply_theme(self, dark_mode: bool):
        self._dark_mode = dark_mode
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
        # Alias /tabs -> /tabella, /matrix -> /matrice
        if q == "/tabs":
            q = "/tabella"
        elif q == "/matrix":
            q = "/matrice"
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

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAcceptRichText(False)
        self.document().setDocumentMargin(12 if reading_mode else 4)
        self.highlighter = MarkdownLatexHighlighter(self.document(), dark_mode=dark_mode)

        self.slash_popup = SlashCommandPopup(self, dark_mode=dark_mode)
        self.slash_popup.commandChosen.connect(self._apply_slash_command)
        self.slash_popup.hide()

        self.textChanged.connect(self._on_internal_text_changed)
        self.cursorPositionChanged.connect(self._on_cursor_pos_changed)
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
        parts: List[str] = []
        fit = block.begin()
        while not fit.atEnd():
            frag = fit.fragment()
            if frag.isValid():
                cf = frag.charFormat()
                if cf.hasProperty(PROP_LATEX_SOURCE):
                    tex_src = str(cf.property(PROP_LATEX_SOURCE) or "").strip()
                    if tex_src:
                        parts.append(f"$$\n{tex_src}\n$$")
                else:
                    t = frag.text().replace("\ufffc", "")
                    if t:
                        parts.append(t)
            fit += 1
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
        if kind == "todo":
            if txt.startswith("☑ "):
                return f"- [x] {txt[2:]}"
            if txt.startswith("☐ "):
                return f"- [ ] {txt[2:]}"
            return f"- [ ] {txt}"
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
            else:
                cursor.setBlockFormat(_default_block_format())
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
                if stripped.endswith("$$") and len(stripped) > 4:
                    expr = stripped[2:-2].strip()
                    i += 1
                else:
                    math_lines = [stripped[2:]]
                    i += 1
                    while i < len(lines):
                        cur_l = lines[i].strip()
                        if cur_l.endswith("$$"):
                            math_lines.append(cur_l[:-2])
                            i += 1
                            break
                        math_lines.append(lines[i])
                        i += 1
                    expr = "\n".join(math_lines).strip()
                if expr and re.search(r"\\begin\{[bp]matrix\}", expr):
                    _ensure_new_block()
                    self._insert_latex_block_at_cursor(cursor, expr)
                else:
                    _ensure_new_block()
                    cursor.insertText(f"$$\n{expr}\n$$")
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

            # 6. Blocco To-Do (- [ ] o - [x])
            todo_m = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", line)
            if todo_m:
                _ensure_new_block()
                checked = (todo_m.group(1).lower() == "x")
                self._apply_todo_block_at_cursor(cursor, todo_m.group(2), checked=checked)
                i += 1
                continue

            # 7. Testo normale + eventuale anteprima immagine inline
            _ensure_new_block()
            cursor.insertText(line)
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

    def _apply_heading_block_at_cursor(self, cursor: QTextCursor, level: int = 1, text: str = ""):
        bf = QTextBlockFormat()
        bf.setTopMargin(12 if level == 1 else (9 if level == 2 else 6))
        bf.setBottomMargin(5 if level == 1 else 3)
        kind = f"h{level}"
        bf.setProperty(PROP_BLOCK_KIND, kind)
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData(kind))

        scale = max(0.8, min(1.5, self._font_scale_pct / 100.0))
        size_map = {1: 15.5, 2: 13.5, 3: 12.0, 4: 11.5}
        pt_size = size_map.get(level, 11.5)
        cf = QTextCharFormat()
        cf.setFontWeight(QFont.Weight.Bold if level <= 2 else QFont.Weight.DemiBold)
        cf.setFontPointSize(round(pt_size * scale, 1))
        cf.setForeground(QColor("#F8FAFC" if self._dark_mode else "#0F172A"))
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
                qimg = _mathtext_to_transparent_qimage(clean, dpi=175, ink_hex=ink_hex)
                if qimg is not None and not qimg.isNull():
                    w_px = max(20, int(qimg.width() * 0.62))
                    h_px = max(20, int(qimg.height() * 0.62))
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

    def _apply_quote_block_at_cursor(self, cursor: QTextCursor, text: str = ""):
        bf = QTextBlockFormat()
        bf.setLeftMargin(18)
        bf.setTopMargin(6)
        bf.setBottomMargin(6)
        bf.setProperty(PROP_BLOCK_KIND, "quote")
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("quote"))

        cf = QTextCharFormat()
        cf.setForeground(QColor("#CBD5E1" if self._dark_mode else "#334155"))
        cursor.setCharFormat(cf)
        if text:
            cursor.insertText(text)

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
            cursor.insertText(text, cf)

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

    def _apply_todo_block_at_cursor(self, cursor: QTextCursor, text: str = "", checked: bool = False):
        bf = QTextBlockFormat()
        bf.setTopMargin(3)
        bf.setBottomMargin(3)
        bf.setProperty(PROP_BLOCK_KIND, "todo")
        cursor.setBlockFormat(bf)
        cursor.block().setUserData(BlockUserData("todo"))

        glyph = "☑ " if checked else "☐ "
        chk_fmt = QTextCharFormat()
        chk_fmt.setForeground(QColor("#6366F1" if checked else ("#94A3B8" if self._dark_mode else "#64748B")))
        cursor.insertText(glyph, chk_fmt)

        txt_fmt = QTextCharFormat()
        if checked:
            txt_fmt.setFontStrikeOut(True)
            txt_fmt.setForeground(QColor("#64748B" if self._dark_mode else "#9CA3AF"))
        else:
            txt_fmt.setFontStrikeOut(False)
            txt_fmt.setForeground(QColor("#E2E8F0" if self._dark_mode else "#1E293B"))
        cursor.setCharFormat(txt_fmt)
        if text:
            cursor.insertText(text, txt_fmt)

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

        # 2. Disegna barra verticale Quote (3px), Callout, File, AI Note
        block = self.document().begin()
        while block.isValid():
            kind = block.blockFormat().property(PROP_BLOCK_KIND)
            if kind == "quote":
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

        # 5. Pulsanti discreti ✕ per eliminare blocchi speciali (/code, /callout, /file, ai_note)
        for rect, action_kind, target in pending_delete_badges:
            idx = len(self._block_action_rects)
            self._block_action_rects.append((rect, action_kind, target))
            self._draw_delete_badge(fg_painter, rect, self._hovered_action_idx == idx)

        fg_painter.end()

    # -----------------------------------------------------------------
    # ELIMINAZIONE BLOCCHI UNDO-SAFE & MATRICE IN-PLACE
    # -----------------------------------------------------------------
    def _delete_frame_undoable(self, frame: QTextFrame):
        if frame is None:
            return
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
        self.viewport().update()

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

        if not self._is_studio_view and event.button() == Qt.MouseButton.LeftButton:
            pt_f = QPointF(event.pos())
            for rect, action_kind, target in self._block_action_rects:
                if rect.contains(pt_f):
                    if action_kind == "delete_frame":
                        self._delete_frame_undoable(target)
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

            if kind == "todo" and cur.positionInBlock() <= 2:
                self._toggle_todo_block(block)
                return

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

    def _edit_latex_at_cursor(self, cur: QTextCursor, old_tex: str):
        if re.search(r"\\begin\{[bpv]matrix\}", old_tex):
            pos = min(cur.selectionStart(), cur.selectionEnd())
            self._open_inline_matrix_editor(pos, old_tex)
            return
        val, accepted = QInputDialog.getMultiLineText(
            self,
            "Modifica LaTeX",
            "Codice LaTeX:",
            old_tex,
        )
        if accepted and val.strip():
            c = QTextCursor(cur)
            c.beginEditBlock()
            c.removeSelectedText()
            self._insert_latex_block_at_cursor(c, val.strip())
            c.endEditBlock()
            self._on_internal_text_changed()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if not self._is_studio_view and event.button() == Qt.MouseButton.LeftButton:
            mat_hit = self._find_matrix_at_point(event.pos())
            if mat_hit is not None:
                self._open_inline_matrix_editor(mat_hit[0], mat_hit[1])
                return
            cur = self.cursorForPosition(event.pos())
            cf = cur.charFormat()
            if not cf.hasProperty(PROP_LATEX_SOURCE):
                probe = QTextCursor(cur)
                if probe.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1):
                    cf = probe.charFormat()
                    cur = probe
            if cf.hasProperty(PROP_LATEX_SOURCE):
                old_tex = str(cf.property(PROP_LATEX_SOURCE) or "")
                self._edit_latex_at_cursor(cur, old_tex)
                return
        super().mouseDoubleClickEvent(event)

    def _toggle_todo_block(self, block):
        txt = block.text()
        if txt.startswith("☐ "):
            checked = True
            new_glyph = "☑ "
        elif txt.startswith("☑ "):
            checked = False
            new_glyph = "☐ "
        else:
            return

        self._internal_update = True
        try:
            c = QTextCursor(block)
            c.beginEditBlock()
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 2)
            chk_fmt = QTextCharFormat()
            chk_fmt.setForeground(QColor("#6366F1" if checked else ("#94A3B8" if self._dark_mode else "#64748B")))
            c.insertText(new_glyph, chk_fmt)

            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            txt_fmt = QTextCharFormat()
            if checked:
                txt_fmt.setFontStrikeOut(True)
                txt_fmt.setForeground(QColor("#64748B" if self._dark_mode else "#9CA3AF"))
            else:
                txt_fmt.setFontStrikeOut(False)
                txt_fmt.setForeground(QColor("#E2E8F0" if self._dark_mode else "#1E293B"))
            c.mergeCharFormat(txt_fmt)
            c.endEditBlock()
        finally:
            self._internal_update = False

        self._on_internal_text_changed()

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

    def _apply_slash_command(self, cmd: str):
        cursor = self.textCursor()
        if self._slash_start_pos is not None and cursor.position() >= self._slash_start_pos:
            cursor.setPosition(self._slash_start_pos, QTextCursor.MoveMode.MoveAnchor)
            cursor.setPosition(self.textCursor().position(), QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        self._close_slash_popup()

        block_commands = {
            "/formula", "/matrice", "/matrix", "/tabella", "/quote", "/callout",
            "/todo", "/h1", "/h2", "/h3", "/code", "/bibtex", "/file",
        }
        if cmd in block_commands and cursor.block().text().strip() != "":
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.setCharFormat(QTextCharFormat())
            cursor.block().setUserData(None)

        if cmd == "/tabella":
            picker = GridSizePickerPopup("Tabella", dark_mode=self._dark_mode, parent=self)
            rect = self.cursorRect()
            picker.move(self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4)))
            if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_size():
                r, c = picker.selected_size()
                tbl = self._insert_table_at_cursor(cursor, rows=r, cols=c)
                self.setTextCursor(tbl.cellAt(0, 0).firstCursorPosition())
                self._on_internal_text_changed()
        elif cmd == "/quote":
            self._apply_quote_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/callout":
            self._apply_callout_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/todo":
            self._apply_todo_block_at_cursor(cursor, "", checked=False)
            self.setTextCursor(cursor)
        elif cmd == "/code":
            self._insert_code_frame_at_cursor(cursor, "", lang="python")
        elif cmd == "/bibtex":
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
        elif cmd == "/file":
            self._apply_file_block_at_cursor(cursor, "")
            self.setTextCursor(cursor)
        elif cmd == "/immagine":
            self._open_file_picker_and_embed(target_block=None)
        elif cmd == "/formula":
            cursor.insertText("$$\n  \n$$")
            cursor.movePosition(QTextCursor.MoveOperation.Up)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
            self.setTextCursor(cursor)
        elif cmd in ("/matrice", "/matrix"):
            picker = GridSizePickerPopup("Matrice", dark_mode=self._dark_mode, parent=self)
            rect = self.cursorRect()
            picker.move(self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4)))
            if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_size():
                r, c = picker.selected_size()
                rows_tex = [
                    " & ".join("1" if (r == c and ri == ci) else "0" for ci in range(c))
                    for ri in range(r)
                ]
                init_tex = "\\begin{bmatrix}\n" + " \\\\\n".join(rows_tex) + "\n\\end{bmatrix}"
                mat_pos = cursor.position()
                self._insert_latex_block_at_cursor(cursor, init_tex)
                self.setTextCursor(cursor)
                self._on_internal_text_changed()
                self._open_inline_matrix_editor(mat_pos, init_tex)
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

    # -----------------------------------------------------------------
    # TASTIERA: TAB CALC/TABELLA, BACKSPACE BLOCCHI, AUTO-TODO/QUOTE, SLASH, =
    # -----------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        if self._is_studio_view:
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()

        # 0. Scorciatoia Play/Pausa Audio-Sync (Ctrl+Spazio) nelle sbobine
        if self._audio_sync_active and key == Qt.Key.Key_Space and (mods & Qt.KeyboardModifier.ControlModifier):
            self.togglePlayRequested.emit()
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

        # 3b. Backspace all'inizio di un blocco speciale vuoto -> elimina il blocco / torna a paragrafo
        block = cursor.block()
        b_kind = block.blockFormat().property(PROP_BLOCK_KIND)
        if key == Qt.Key.Key_Backspace and not cursor.hasSelection() and b_kind:
            if b_kind == "todo" and cursor.positionInBlock() <= 2 and block.text() in ("☐ ", "☑ ", "☐", "☑", ""):
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                block.setUserData(None)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
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

        # 4. Invio in blocco To-Do, Quote, Callout, Heading
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (mods & Qt.KeyboardModifier.ShiftModifier):
            if b_kind == "todo":
                b_txt = block.text().strip()
                if b_txt in ("☐", "☑", ""):
                    # Esci dalla lista To-Do
                    cursor.beginEditBlock()
                    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                    cursor.setBlockFormat(QTextBlockFormat())
                    cursor.setCharFormat(QTextCharFormat())
                    block.setUserData(None)
                    cursor.endEditBlock()
                    self.setTextCursor(cursor)
                    return
                else:
                    cursor.insertBlock()
                    self._apply_todo_block_at_cursor(cursor, "", checked=False)
                    self.setTextCursor(cursor)
                    return
            elif b_kind in ("quote", "callout") and block.text().strip() == "":
                cursor.beginEditBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                block.setUserData(None)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                return
            elif b_kind in ("h1", "h2", "h3", "h4"):
                cursor.insertBlock()
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.setCharFormat(QTextCharFormat())
                cursor.block().setUserData(None)
                self.setTextCursor(cursor)
                return

        # 5. Auto-conversione "> " -> Quote, "[] " -> To-Do, "# "/"## "/"### " -> H1/H2/H3
        if key == Qt.Key.Key_Space and not b_kind:
            before_txt = block.text()[: cursor.positionInBlock()]
            if before_txt in ("#", "##", "###"):
                lvl = len(before_txt)
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, len(before_txt))
                cursor.removeSelectedText()
                self._apply_heading_block_at_cursor(cursor, level=lvl, text="")
                self.setTextCursor(cursor)
                return
            if before_txt == ">":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1)
                cursor.removeSelectedText()
                self._apply_quote_block_at_cursor(cursor, "")
                self.setTextCursor(cursor)
                return
            if before_txt == "[]":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 2)
                cursor.removeSelectedText()
                self._apply_todo_block_at_cursor(cursor, "", checked=False)
                self.setTextCursor(cursor)
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

        super().keyPressEvent(event)

        if self.slash_popup.isVisible():
            self._update_slash_filter()
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
