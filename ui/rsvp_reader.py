"""Motore RSVP (Rapid Serial Visual Presentation) e Overlay Modale per FlowNotes.

Caratteristiche:
- Parser Markdown + LaTeX:
  - Pulizia sintassi Markdown (#, **, *, __, -, >, `, link, callout, todo) preservando i confini di blocco/paragrafo.
  - Blocchi formula display ($$...$$) e matrici estratti come token speciali {type: "formula", content: rawLatex}
    che mettono in pausa automatica la lettura e renderizzano la formula al centro del reader.
  - LaTeX inline ($...$) preservato come singolo token compatto senza spezzarlo sugli spazi interni.
- Calcolo ORP (Optimal Recognition Point) in base alla lunghezza della parola (esclusa punteggiatura):
  - len 1 -> 0, 2..5 -> 1, 6..9 -> 2, 10..13 -> 3, >13 -> 4.
- Pacing adattivo (moltiplicatori su baseDelay = 60000 / wpm):
  - > 8 caratteri: 1.2x
  - Virgola, :, ;: 1.5x
  - Punto, ?, !, o fine blocco/paragrafo: 2.1x
- Timer compensato sul drift con time.perf_counter() per precisione anche a 600-1000 WPM.
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


# =====================================================================
# COSTANTI & REGEX PARSER
# =====================================================================
RSVP_DEFAULT_WPM = 350
RSVP_MIN_WPM = 150
RSVP_MAX_WPM = 1000
RSVP_WPM_STEP = 25

_DISPLAY_LATEX_RE = re.compile(r"\$\$([\s\S]+?)\$\$")
_INLINE_LATEX_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n([\s\S]*?)```")
_AI_NOTE_RE = re.compile(r":::ai-note[^\n]*\n([\s\S]*?):::")
_TRAILING_PUNCT_RE = re.compile(r"[\.,;:!\?\)\]\}»\"'”’…]+$")
_LEADING_PUNCT_RE = re.compile(r"^[\(\[\{«\"'“‘¡¿]+")


@dataclass
class RsvpToken:
    """Singolo token pronto per la riproduzione RSVP."""
    type: str  # "word" | "formula"
    text: str  # parola visibile o sorgente LaTeX grezzo
    prefix: str = ""
    orp_char: str = ""
    suffix: str = ""
    orp_index: int = 0
    delay_mult: float = 1.0
    block_idx: int = 0
    is_paragraph_end: bool = False
    is_sentence_end: bool = False


def compute_orp_index(core_len: int) -> int:
    """Calcola l'indice ORP in base alla lunghezza della parola (senza punteggiatura)."""
    if core_len <= 1:
        return 0
    if core_len <= 5:
        return 1
    if core_len <= 9:
        return 2
    if core_len <= 13:
        return 3
    return 4


def _split_word_orp(raw_word: str) -> Tuple[str, str, str, int, int]:
    """Divide una parola in (prefix, orp_char, suffix, orp_idx_in_raw, clean_len)."""
    if not raw_word:
        return ("", "", "", 0, 0)

    # Inline LaTeX ($...$): trattato come token compatto unico
    if raw_word.startswith("$") and raw_word.endswith("$") and len(raw_word) >= 3:
        inner = raw_word[1:-1].strip()
        if not inner:
            return ("", raw_word, "", 0, 1)
        mid = max(0, len(inner) // 2)
        return (inner[:mid], inner[mid], inner[mid + 1 :], mid, len(inner))

    lead_m = _LEADING_PUNCT_RE.match(raw_word)
    lead_len = len(lead_m.group(0)) if lead_m else 0
    trail_m = _TRAILING_PUNCT_RE.search(raw_word[lead_len:])
    trail_len = len(trail_m.group(0)) if trail_m else 0

    core_end = len(raw_word) - trail_len
    core = raw_word[lead_len:core_end]
    if not core:
        core_len = len(raw_word)
        orp_idx = min(compute_orp_index(core_len), max(0, core_len - 1))
        return (
            raw_word[:orp_idx],
            raw_word[orp_idx] if raw_word else "",
            raw_word[orp_idx + 1 :],
            orp_idx,
            core_len,
        )

    core_len = len(core)
    orp_in_core = min(compute_orp_index(core_len), core_len - 1)
    orp_abs = lead_len + orp_in_core
    return (
        raw_word[:orp_abs],
        raw_word[orp_abs],
        raw_word[orp_abs + 1 :],
        orp_abs,
        core_len,
    )


def _clean_markdown_line(line: str) -> str:
    """Rimuove marcatori Markdown dalla riga mantenendo il testo pulito e il LaTeX intatto."""
    s = line.strip()
    if not s:
        return ""
    # Table separator lines (| --- | --- |)
    if re.fullmatch(r"\|?[\s:\-\|]+\|?", s) and "-" in s:
        return ""
    # Headings: # .. ######
    s = re.sub(r"^#{1,6}\s+", "", s)
    # Callout / Blockquote
    s = re.sub(r"^>\s*(\[![A-Z]+\]\s*)?", "", s)
    # Todo checkboxes
    s = re.sub(r"^[-*+]\s+\[[ xX]\]\s*", "", s)
    # Unordered list
    s = re.sub(r"^[-*+]\s+", "", s)
    # Ordered list (keep number or strip bullet? Strip "1. " marker)
    s = re.sub(r"^\d+\.\s+", "", s)
    # Links [label](url) -> label
    s = _MD_LINK_RE.sub(r"\1", s)
    # Bold / Italic / Underline / Strikethrough / Inline code
    s = s.replace("**", "").replace("__", "").replace("~~", "")
    # Single * or _ used for emphasis (avoid touching LaTeX inside $...$)
    parts = re.split(r"(\$[^$\n]+\$)", s)
    cleaned_parts: List[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            cleaned_parts.append(part)
        else:
            p = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", part)
            p = p.replace("`", "")
            # Table pipes -> spaces
            if "|" in p:
                p = p.replace("|", " ")
            cleaned_parts.append(p)
    s = "".join(cleaned_parts)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def tokenize_rsvp_markdown(
    markdown_text: str,
    start_block_idx: int = 0,
) -> List[RsvpToken]:
    """Converte testo Markdown + LaTeX in una lista piatta di RsvpToken.

    Args:
        markdown_text: Testo Markdown (intera nota, selezione o slice da blocco cursore).
        start_block_idx: Indice di blocco base nell'editor per mappare Esc -> blocco esatto.
    """
    if not markdown_text or not markdown_text.strip():
        return []

    text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Proteggi blocchi ai-note (estrai il corpo) e code fence
    text = _AI_NOTE_RE.sub(lambda m: m.group(1).strip(), text)
    text = _MD_CODE_FENCE_RE.sub(lambda m: m.group(1).strip(), text)

    # 2. Estrai i blocchi display LaTeX ($$...$$) sostituendoli con segnaposto univoci
    formulas: List[str] = []

    def _stash_formula(match: re.Match) -> str:
        raw_tex = match.group(1).strip()
        if not raw_tex:
            return ""
        idx = len(formulas)
        formulas.append(raw_tex)
        return f"\n\x00FORMULA_{idx}\x00\n"

    text = _DISPLAY_LATEX_RE.sub(_stash_formula, text)

    # 3. Processa riga per riga (preservando block_idx approssimato per riga)
    tokens: List[RsvpToken] = []
    lines = text.split("\n")

    for line_offset, raw_line in enumerate(lines):
        curr_block = start_block_idx + line_offset
        stripped = raw_line.strip()
        if not stripped:
            # Fine paragrafo: marca l'ultimo token parola con pausa extra 2.1x
            if tokens and tokens[-1].type == "word":
                tokens[-1].is_paragraph_end = True
                tokens[-1].delay_mult = max(tokens[-1].delay_mult, 2.1)
            continue

        # Segnaposto formula display
        fm = re.fullmatch(r"\x00FORMULA_(\d+)\x00", stripped)
        if fm:
            f_idx = int(fm.group(1))
            tokens.append(
                RsvpToken(
                    type="formula",
                    text=formulas[f_idx],
                    block_idx=curr_block,
                    delay_mult=1.0,
                )
            )
            continue

        clean_line = _clean_markdown_line(stripped)
        if not clean_line:
            continue

        # Proteggi LaTeX inline ($...$) sostituendo gli spazi interni con \x01
        inline_tex_list: List[str] = []

        def _stash_inline(m: re.Match) -> str:
            idx = len(inline_tex_list)
            inline_tex_list.append(m.group(0))
            return f"\x02ILATEX_{idx}\x02"

        protected = _INLINE_LATEX_RE.sub(_stash_inline, clean_line)
        raw_words = protected.split()

        line_word_tokens: List[RsvpToken] = []
        for w in raw_words:
            # Ripristina inline LaTeX se presente
            if "\x02ILATEX_" in w:
                for idx, tex_val in enumerate(inline_tex_list):
                    w = w.replace(f"\x02ILATEX_{idx}\x02", tex_val)

            # Controlla punteggiatura finale (fuori da $...$)
            check_str = w
            if check_str.startswith("$") and check_str.endswith("$"):
                has_end_punct = False
                has_mid_punct = False
            else:
                has_end_punct = bool(re.search(r"[\.\?!…]+[\"'\)\]\}»”’]*$", check_str))
                has_mid_punct = bool(re.search(r"[,;:]+[\"'\)\]\}»”’]*$", check_str))

            prefix, orp_char, suffix, orp_idx, clean_len = _split_word_orp(w)
            mult = 1.0
            if clean_len > 8:
                mult = 1.2
            if has_mid_punct:
                mult = max(mult, 1.5)
            if has_end_punct:
                mult = max(mult, 2.1)

            line_word_tokens.append(
                RsvpToken(
                    type="word",
                    text=w,
                    prefix=prefix,
                    orp_char=orp_char,
                    suffix=suffix,
                    orp_index=orp_idx,
                    delay_mult=mult,
                    block_idx=curr_block,
                    is_sentence_end=has_end_punct,
                )
            )

        if line_word_tokens:
            # Fine blocco/riga: applica pausa extra di fine blocco se è un blocco distinto
            line_word_tokens[-1].is_paragraph_end = True
            line_word_tokens[-1].delay_mult = max(line_word_tokens[-1].delay_mult, 2.1)
            tokens.extend(line_word_tokens)

    return tokens


def estimate_remaining_seconds(tokens: List[RsvpToken], current_idx: int, wpm: int) -> float:
    """Stima il tempo rimanente in secondi tenendo conto dei moltiplicatori adattivi."""
    if not tokens or current_idx >= len(tokens):
        return 0.0
    base_sec = 60.0 / max(1, wpm)
    total = 0.0
    for tok in tokens[current_idx:]:
        if tok.type == "formula":
            total += base_sec * 2.5
        else:
            total += base_sec * tok.delay_mult
    return total


def format_remaining_time(seconds: float) -> str:
    """Formatta i secondi rimanenti in modo leggibile (es. '42s' o '2m 15s')."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    mins, rem = divmod(s, 60)
    return f"{mins}m {rem:02d}s"


# =====================================================================
# RENDERER FORMULA LATEX PER RSVP (riusa il motore locale di FlowNotes)
# =====================================================================
def _render_formula_pixmap(raw_latex: str, ink_hex: str = "#F8FAFC") -> Optional[QPixmap]:
    """Renderizza una formula LaTeX (o matrice) in QPixmap trasparente ad alta risoluzione."""
    clean = (raw_latex or "").strip()
    if not clean:
        return None
    try:
        from ui.scientific_editor import (
            _mathtext_to_transparent_qimage,
            _render_matrix_png_b64,
        )

        if any(tag in clean for tag in ("\\begin{bmatrix}", "\\begin{pmatrix}", "\\begin{vmatrix}")):
            res = _render_matrix_png_b64(clean, dark_mode=True, reading_mode=False)
            if res is not None:
                b64, _, _ = res
                pix = QPixmap()
                if pix.loadFromData(base64.b64decode(b64), "PNG"):
                    return pix

        qimg = _mathtext_to_transparent_qimage(clean, dpi=220, ink_hex=ink_hex)
        if qimg is not None and not qimg.isNull():
            return QPixmap.fromImage(qimg)
    except Exception:
        pass
    return None


# =====================================================================
# HELPER COLORE GRADIENTE VERDE -> GIALLO -> ROSSO
# =====================================================================
def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(round(c1.red() + (c2.red() - c1.red()) * t)),
        int(round(c1.green() + (c2.green() - c1.green()) * t)),
        int(round(c1.blue() + (c2.blue() - c1.blue()) * t)),
    )


def _speed_color_for_ratio(ratio: float) -> QColor:
    """Restituisce un colore fluido da Verde (#10B981) -> Ambra (#F59E0B) -> Rosso (#EF4444)."""
    r = max(0.0, min(1.0, ratio))
    green = QColor("#10B981")
    amber = QColor("#F59E0B")
    red = QColor("#EF4444")
    if r <= 0.5:
        return _lerp_color(green, amber, r * 2.0)
    return _lerp_color(amber, red, (r - 0.5) * 2.0)


# =====================================================================
# BARRA VERTICALE VELOCITÀ STILE VOLUME SMARTPHONE (DESTRA)
# =====================================================================
class _VerticalSpeedVolumeBar(QWidget):
    """Barra verticale trascinabile stile volume telefono (150 - 1000 WPM, Verde -> Rosso)."""

    wpmChanged = pyqtSignal(int)

    def __init__(
        self,
        wpm: int = RSVP_DEFAULT_WPM,
        is_dark: bool = True,
        font_family: str = "Segoe UI",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._wpm = max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, wpm))
        self._is_dark = is_dark
        self._font_family = font_family or "Segoe UI"
        self._dragging = False
        self.setFixedSize(68, 300)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Trascina verso l'alto o usa la rotella per regolare i WPM (↑ / ↓)")

    def set_wpm(self, wpm: int):
        clamped = max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, int(wpm)))
        if clamped != self._wpm:
            self._wpm = clamped
            self.update()

    def _track_rect(self) -> Tuple[int, int, int, int]:
        # (x, y, w, h) della pillola verticale
        w = 46
        x = (self.width() - w) // 2
        y = 10
        h = self.height() - 56
        return (x, y, w, h)

    def _y_to_wpm(self, y: float) -> int:
        _, ty, _, th = self._track_rect()
        ratio = 1.0 - ((y - ty) / max(1, th))
        ratio = max(0.0, min(1.0, ratio))
        raw = RSVP_MIN_WPM + ratio * (RSVP_MAX_WPM - RSVP_MIN_WPM)
        snapped = int(round(raw / RSVP_WPM_STEP) * RSVP_WPM_STEP)
        return max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, snapped))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            new_wpm = self._y_to_wpm(event.position().y())
            if new_wpm != self._wpm:
                self._wpm = new_wpm
                self.update()
                self.wpmChanged.emit(new_wpm)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            new_wpm = self._y_to_wpm(event.position().y())
            if new_wpm != self._wpm:
                self._wpm = new_wpm
                self.update()
                self.wpmChanged.emit(new_wpm)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        step = RSVP_WPM_STEP if delta > 0 else -RSVP_WPM_STEP
        new_wpm = max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, self._wpm + step))
        if new_wpm != self._wpm:
            self._wpm = new_wpm
            self.update()
            self.wpmChanged.emit(new_wpm)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        tx, ty, tw, th = self._track_rect()
        radius = 18.0
        ratio = (self._wpm - RSVP_MIN_WPM) / max(1, RSVP_MAX_WPM - RSVP_MIN_WPM)
        fill_col = _speed_color_for_ratio(ratio)

        # Sfondo pillola stile volume iOS/Android
        track_bg = QColor(15, 20, 32, 220) if self._is_dark else QColor(241, 245, 249, 235)
        track_bd = QColor(255, 255, 255, 34) if self._is_dark else QColor(15, 23, 42, 30)

        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QLinearGradient, QPainterPath

        track_rect = QRectF(tx, ty, tw, th)
        clip_path = QPainterPath()
        clip_path.addRoundedRect(track_rect, radius, radius)

        p.setPen(QPen(track_bd, 1.2))
        p.setBrush(track_bg)
        p.drawPath(clip_path)

        # Riempimento dal basso verso l'alto con gradiente Verde -> Colore corrente
        fill_h = max(0.0, th * ratio)
        if fill_h > 1.0:
            p.save()
            p.setClipPath(clip_path)
            fill_top = ty + th - fill_h
            grad = QLinearGradient(tx, ty + th, tx, fill_top)
            grad.setColorAt(0.0, QColor("#10B981"))
            if ratio > 0.5:
                grad.setColorAt(0.5, QColor("#F59E0B"))
            grad.setColorAt(1.0, fill_col)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRect(QRectF(tx, fill_top, tw, fill_h + 2))
            p.restore()

        # Tacche discrete laterali sottili
        tick_col = QColor(255, 255, 255, 45) if self._is_dark else QColor(15, 23, 42, 40)
        p.setPen(QPen(tick_col, 1.0))
        for frac in (0.25, 0.5, 0.75):
            y_tk = int(round(ty + th * (1.0 - frac)))
            p.drawLine(tx + 8, y_tk, tx + tw - 8, y_tk)

        # Etichetta WPM sotto la pillola
        lbl_rect = QRectF(0, ty + th + 8, self.width(), 38)
        f_num = QFont(self._font_family, 13, QFont.Weight.Bold)
        p.setFont(f_num)
        p.setPen(fill_col)
        p.drawText(QRectF(0, ty + th + 6, self.width(), 20), Qt.AlignmentFlag.AlignCenter, f"{self._wpm}")

        f_sub = QFont(self._font_family, 8, QFont.Weight.DemiBold)
        p.setFont(f_sub)
        sub_col = QColor("#94A3B8") if self._is_dark else QColor("#475569")
        p.setPen(sub_col)
        p.drawText(QRectF(0, ty + th + 24, self.width(), 14), Qt.AlignmentFlag.AlignCenter, "WPM")


# =====================================================================
# WIDGET GUIDA FOCALE ORP (QPainter Puro — ZERO JITTER)
# =====================================================================
class _OrpFocusStage(QWidget):
    """Stage centrale RSVP con rendering QPainter bloccato sull'asse centrale (zero micro-movimenti)."""

    ORP_FOCAL_DARK = "#FF6B6B"   # Rosso corallo ad alto contrasto su sfondo scuro
    ORP_FOCAL_LIGHT = "#E11D48"  # Cremisi nitido su sfondo chiaro

    def __init__(
        self,
        is_dark: bool = True,
        text_hex: str = "#F8FAFC",
        muted_hex: str = "#94A3B8",
        font_family: str = "Segoe UI",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._is_dark = is_dark
        self._text_hex = text_hex or ("#F8FAFC" if is_dark else "#0F172A")
        self._muted_hex = muted_hex or ("#94A3B8" if is_dark else "#64748B")
        self._orp_hex = self.ORP_FOCAL_DARK if is_dark else self.ORP_FOCAL_LIGHT
        self._font_family = font_family or "Segoe UI"
        self._current_token: Optional[RsvpToken] = None
        self._is_formula = False
        self.setMinimumHeight(210)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(24, 16, 24, 16)
        root_lay.setSpacing(0)

        # Pannello Formula Display ($$...$$)
        self._formula_container = QWidget(self)
        f_lay = QVBoxLayout(self._formula_container)
        f_lay.setContentsMargins(16, 10, 16, 10)
        f_lay.setSpacing(10)
        f_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._formula_img = QLabel()
        self._formula_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._formula_img.setWordWrap(True)

        self._formula_hint = QLabel("Formula in pausa — premi Spazio per riprendere")
        self._formula_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._formula_hint.setStyleSheet(
            f"color: {self._muted_hex}; font-size: 12px; font-weight: 500; background: transparent;"
        )

        f_lay.addWidget(self._formula_img, 0, Qt.AlignmentFlag.AlignCenter)
        f_lay.addWidget(self._formula_hint, 0, Qt.AlignmentFlag.AlignCenter)
        self._formula_container.hide()

        root_lay.addWidget(self._formula_container, 1, Qt.AlignmentFlag.AlignVCenter)

    def show_word(self, token: RsvpToken):
        self._is_formula = False
        self._current_token = token
        self._formula_container.hide()
        self.update()

    def show_formula(self, raw_latex: str):
        self._is_formula = True
        self._formula_container.show()
        ink = "#F8FAFC" if self._is_dark else "#0F172A"
        pix = _render_formula_pixmap(raw_latex, ink_hex=ink)
        if pix is not None and not pix.isNull():
            max_w = max(360, self.width() - 80)
            max_h = 240
            if pix.width() > max_w or pix.height() > max_h:
                pix = pix.scaled(
                    max_w,
                    max_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._formula_img.setPixmap(pix)
            self._formula_img.setStyleSheet("background: transparent;")
        else:
            self._formula_img.setPixmap(QPixmap())
            self._formula_img.setText(raw_latex)
            self._formula_img.setStyleSheet(
                f"color: {self._text_hex}; font-family: Consolas, monospace; font-size: 20px; "
                "background: rgba(255,255,255,0.05); border-radius: 8px; padding: 14px;"
            )
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._is_formula or self._current_token is None:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w = self.width()
        h = self.height()
        cx = w / 2.0
        cy = h / 2.0

        tok = self._current_token
        prefix = tok.prefix
        orp_ch = tok.orp_char
        suffix = tok.suffix

        # Font grande e leggibile (50pt); scala solo se una parola lunghissima supera i margini
        pt_size = 50
        font = QFont(self._font_family, pt_size, QFont.Weight.DemiBold)
        font.setStyleHint(QFont.StyleHint.SansSerif)
        fm = QFontMetrics(font)

        max_side = (w / 2.0) - 32.0
        orp_w = fm.horizontalAdvance(orp_ch)
        pre_w = fm.horizontalAdvance(prefix)
        suf_w = fm.horizontalAdvance(suffix)
        while pt_size > 24 and (pre_w + orp_w / 2.0 > max_side or suf_w + orp_w / 2.0 > max_side):
            pt_size -= 3
            font.setPointSize(pt_size)
            fm = QFontMetrics(font)
            orp_w = fm.horizontalAdvance(orp_ch)
            pre_w = fm.horizontalAdvance(prefix)
            suf_w = fm.horizontalAdvance(suffix)

        # Linee guida orizzontali + due tacche verticali esattamente a x = cx (zero jitter!)
        guide_half_h = max(46, fm.height() // 2 + 18)
        top_y = int(round(cy - guide_half_h))
        bot_y = int(round(cy + guide_half_h))

        line_col = QColor(255, 255, 255, 28) if self._is_dark else QColor(15, 23, 42, 26)
        p.setPen(QPen(line_col, 1.0))
        margin_x = 40
        p.drawLine(margin_x, top_y + 12, w - margin_x, top_y + 12)
        p.drawLine(margin_x, bot_y - 12, w - margin_x, bot_y - 12)

        guide_col = QColor(self._orp_hex)
        guide_col.setAlpha(220)
        p.setPen(QPen(guide_col, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        icx = int(round(cx))
        p.drawLine(icx, top_y, icx, top_y + 12)
        p.drawLine(icx, bot_y - 12, icx, bot_y)

        # Posizioni matematicamente bloccate sull'asse cx
        baseline_y = int(round(cy + (fm.ascent() - fm.descent()) / 2.0))
        orp_x = cx - (orp_w / 2.0)
        pre_x = orp_x - pre_w
        suf_x = orp_x + orp_w

        text_col = QColor(self._text_hex)
        p.setFont(font)
        p.setPen(text_col)
        if prefix:
            p.drawText(int(round(pre_x)), baseline_y, prefix)
        if suffix:
            p.drawText(int(round(suf_x)), baseline_y, suffix)

        # Lettera focale ORP in grassetto e colore ad alto contrasto
        orp_font = QFont(font)
        orp_font.setWeight(QFont.Weight.Bold)
        p.setFont(orp_font)
        p.setPen(QColor(self._orp_hex))
        if orp_ch:
            p.drawText(int(round(orp_x)), baseline_y, orp_ch)


# =====================================================================
# OVERLAY MODALE RSVP READER
# =====================================================================
class RsvpReaderModal(QWidget):
    """Overlay modale distractive-free per lettura veloce RSVP integrata in FlowNotes."""

    closedAtBlock = pyqtSignal(int)

    def __init__(
        self,
        tokens: List[RsvpToken],
        parent: Optional[QWidget] = None,
        accent_hex: str = "#818CF8",
        font_family: str = "Segoe UI",
        on_close_block: Optional[Callable[[int], None]] = None,
        palette: Optional[Dict[str, str]] = None,
        is_dark: bool = True,
    ):
        super().__init__(parent)
        self._tokens = tokens or []
        self._idx = 0
        self._playing = False
        self._is_dark = is_dark
        self._palette = palette or {}
        self._accent_hex = accent_hex or "#818CF8"
        self._font_family = font_family or "Segoe UI"
        self._on_close_block = on_close_block
        self._is_closing = False

        # Carica WPM da QSettings (equivalente nativo persistente di localStorage)
        self._settings = QSettings("FlowLocal", "FlowNotes")
        saved_wpm = int(self._settings.value("rsvp/wpm", RSVP_DEFAULT_WPM))
        self._wpm = max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, saved_wpm))

        # Timer ad alta precisione compensato sul drift (time.perf_counter)
        self._next_deadline: float = 0.0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(12)
        self._tick_timer.timeout.connect(self._on_tick)

        # Auto-hide controlli durante il play
        self._controls_hidden = False
        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.setInterval(1800)
        self._autohide_timer.timeout.connect(self._hide_controls_for_play)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        if parent is not None:
            self.setGeometry(parent.rect())

        # Sfondo sfocato nativo Qt (snapshot del parent + blur offscreen)
        self._bg_blur_lbl = QLabel(self)
        self._bg_blur_lbl.setScaledContents(True)
        self._capture_parent_blur()

        self._build_ui()

        if parent is not None:
            parent.installEventFilter(self)

        # Animazione fluida di apertura (fade-in)
        self._opacity_fx = QGraphicsOpacityEffect(self)
        self._opacity_fx.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_fx)

        self._fade_anim = QPropertyAnimation(self._opacity_fx, b"opacity", self)
        self._fade_anim.setDuration(180)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._render_current_token()

    def _capture_parent_blur(self):
        p = self.parentWidget()
        if p is None or p.width() <= 0 or p.height() <= 0:
            self._bg_blur_lbl.hide()
            return
        try:
            snap = p.grab()
            w, h = snap.width(), snap.height()
            small = snap.scaled(
                max(16, w // 14),
                max(16, h // 14),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            blurred = small.scaled(
                w,
                h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._bg_blur_lbl.setGeometry(self.rect())
            self._bg_blur_lbl.setPixmap(blurred)
            self._bg_blur_lbl.lower()
        except Exception:
            self._bg_blur_lbl.hide()

    def _build_ui(self):
        pal = self._palette
        is_dark = self._is_dark

        text_hex = pal.get("title") or ("#F8FAFC" if is_dark else "#0F172A")
        muted_hex = pal.get("subtitle") or ("#94A3B8" if is_dark else "#475569")
        scrim_bg = "rgba(7, 9, 15, 0.80)" if is_dark else "rgba(241, 245, 249, 0.82)"
        card_bg = pal.get("input_bg") or ("#0D111A" if is_dark else "#FFFFFF")
        card_bd = pal.get("card_border") or ("rgba(255, 255, 255, 0.12)" if is_dark else "rgba(15, 23, 42, 0.12)")
        btn_bg = pal.get("btn_bg") or ("#131926" if is_dark else "#F1F5F9")
        btn_hv = pal.get("btn_hover_bg") or ("#192233" if is_dark else "#E2E8F0")
        btn_bd = pal.get("btn_border") or ("#1F293D" if is_dark else "#CBD5E1")
        btn_fg = pal.get("btn_text") or text_hex
        badge_bg = pal.get("pill_bg") or ("rgba(255,255,255,0.07)" if is_dark else "rgba(15,23,42,0.06)")
        badge_bd = pal.get("pill_border") or card_bd

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scrim = QFrame(self)
        self._scrim.setObjectName("RsvpScrim")
        self._scrim.setStyleSheet(f"QFrame#RsvpScrim {{ background-color: {scrim_bg}; border: none; }}")
        self._scrim.setMouseTracking(True)
        root.addWidget(self._scrim)

        scrim_lay = QVBoxLayout(self._scrim)
        scrim_lay.setContentsMargins(36, 24, 36, 24)
        scrim_lay.setSpacing(16)

        # Header minimale (auto-hide durante play)
        self._header_bar = QWidget(self._scrim)
        h_lay = QHBoxLayout(self._header_bar)
        h_lay.setContentsMargins(0, 0, 0, 0)

        self._badge_lbl = QLabel("⚡  RSVP Reader")
        self._badge_lbl.setStyleSheet(
            f"color: {self._accent_hex}; font-size: 13px; font-weight: 700; "
            f"background: {badge_bg}; border: 1px solid {badge_bd}; "
            "border-radius: 12px; padding: 5px 14px;"
        )
        h_lay.addWidget(self._badge_lbl)
        h_lay.addStretch(1)

        self._close_btn = QPushButton("Esc  ✕")
        self._close_btn.setObjectName("RsvpSecondaryBtn")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            f"QPushButton#RsvpSecondaryBtn {{ color: {btn_fg}; background: {btn_bg}; "
            f"border: 1px solid {btn_bd}; border-radius: 10px; "
            "padding: 6px 14px; font-size: 12px; font-weight: 600; }"
            f"QPushButton#RsvpSecondaryBtn:hover {{ background: {btn_hv}; }}"
        )
        self._close_btn.clicked.connect(self.close_reader)
        h_lay.addWidget(self._close_btn)
        scrim_lay.addWidget(self._header_bar)

        scrim_lay.addStretch(1)

        # Corpo centrale: [spacer sx per simmetria] [Card + Controlli al centro] [Barra volume velocità a dx]
        body_row = QHBoxLayout()
        body_row.setSpacing(24)

        left_spacer = QWidget(self._scrim)
        left_spacer.setFixedWidth(68)
        body_row.addWidget(left_spacer)
        body_row.addStretch(1)

        center_col = QVBoxLayout()
        center_col.setSpacing(18)
        center_col.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Card centrale focale
        self._center_card = QFrame(self._scrim)
        self._center_card.setObjectName("RsvpCenterCard")
        self._center_card.setMinimumWidth(620)
        self._center_card.setMaximumWidth(900)
        self._center_card.setStyleSheet(
            f"QFrame#RsvpCenterCard {{ background-color: {card_bg}; "
            f"border: 1px solid {card_bd}; border-radius: 20px; }}"
        )
        c_lay = QVBoxLayout(self._center_card)
        c_lay.setContentsMargins(24, 20, 24, 20)
        c_lay.setSpacing(8)

        self._stage = _OrpFocusStage(
            is_dark=is_dark,
            text_hex=text_hex,
            muted_hex=muted_hex,
            font_family=self._font_family,
            parent=self._center_card,
        )
        c_lay.addWidget(self._stage)
        center_col.addWidget(self._center_card, 0, Qt.AlignmentFlag.AlignCenter)

        # Selettori di parola, pausa e frase al centro dello schermo (sotto la card)
        self._controls_bar = QFrame(self._scrim)
        self._controls_bar.setObjectName("RsvpControlsBar")
        self._controls_bar.setMinimumWidth(460)
        self._controls_bar.setMaximumWidth(640)
        self._controls_bar.setStyleSheet(
            f"QFrame#RsvpControlsBar {{ background-color: {card_bg}; "
            f"border: 1px solid {card_bd}; border-radius: 16px; }}"
        )
        ctrl_v = QVBoxLayout(self._controls_bar)
        ctrl_v.setContentsMargins(18, 12, 18, 12)
        ctrl_v.setSpacing(10)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)
        ctrl_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_css = (
            f"QPushButton#RsvpSecondaryBtn {{ color: {btn_fg}; background: {btn_bg}; "
            f"border: 1px solid {btn_bd}; border-radius: 10px; "
            "padding: 8px 14px; font-size: 12px; font-weight: 600; }"
            f"QPushButton#RsvpSecondaryBtn:hover {{ background: {btn_hv}; }}"
        )

        self._prev_sent_btn = QPushButton("⏮  Frase")
        self._prev_sent_btn.setObjectName("RsvpSecondaryBtn")
        self._prev_sent_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_sent_btn.setStyleSheet(btn_css)
        self._prev_sent_btn.setToolTip("Torna alla frase precedente (Shift+←)")
        self._prev_sent_btn.clicked.connect(self.step_sentence_back)

        self._prev_btn = QPushButton("◀  Parola")
        self._prev_btn.setObjectName("RsvpSecondaryBtn")
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.setStyleSheet(btn_css)
        self._prev_btn.setToolTip("Parola precedente (←)")
        self._prev_btn.clicked.connect(lambda: self.step_word(-1))

        self._play_btn = QPushButton("▶  Play")
        self._play_btn.setObjectName("RsvpPrimaryBtn")
        self._play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_btn.setMinimumWidth(120)
        self._play_btn.setStyleSheet(
            f"QPushButton#RsvpPrimaryBtn {{ color: #FFFFFF; background: {self._accent_hex}; "
            "border: none; border-radius: 10px; padding: 9px 22px; font-size: 13px; font-weight: 700; }"
            f"QPushButton#RsvpPrimaryBtn:hover {{ background: {self._accent_hex}; }}"
        )
        self._play_btn.clicked.connect(self.toggle_play)

        self._next_btn = QPushButton("Parola  ▶")
        self._next_btn.setObjectName("RsvpSecondaryBtn")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setStyleSheet(btn_css)
        self._next_btn.setToolTip("Parola successiva (→)")
        self._next_btn.clicked.connect(lambda: self.step_word(1))

        ctrl_row.addWidget(self._prev_sent_btn)
        ctrl_row.addWidget(self._prev_btn)
        ctrl_row.addWidget(self._play_btn)
        ctrl_row.addWidget(self._next_btn)
        ctrl_v.addLayout(ctrl_row)

        # Riga progresso + tempo stimato rimanente
        prog_row = QHBoxLayout()
        prog_row.setSpacing(12)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(5)
        bar_bg = "rgba(255,255,255,0.10)" if is_dark else "rgba(15,23,42,0.10)"
        self._progress_bar.setStyleSheet(
            f"QProgressBar {{ background: {bar_bg}; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {self._accent_hex}; border-radius: 2px; }}"
        )

        self._stats_lbl = QLabel("0 / 0 · 0s")
        self._stats_lbl.setStyleSheet(
            f"color: {muted_hex}; font-size: 11px; font-weight: 600; background: transparent;"
        )

        prog_row.addWidget(self._progress_bar, 1)
        prog_row.addWidget(self._stats_lbl)
        ctrl_v.addLayout(prog_row)

        center_col.addWidget(self._controls_bar, 0, Qt.AlignmentFlag.AlignCenter)
        body_row.addLayout(center_col)
        body_row.addStretch(1)

        # Barra verticale stile volume smartphone sulla destra
        self._speed_bar = _VerticalSpeedVolumeBar(
            wpm=self._wpm,
            is_dark=is_dark,
            font_family=self._font_family,
            parent=self._scrim,
        )
        self._speed_bar.wpmChanged.connect(lambda v: self.set_wpm(v, update_bar=False))
        body_row.addWidget(self._speed_bar, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        scrim_lay.addLayout(body_row)
        scrim_lay.addStretch(1)

        # Preserva lo spazio esatto nel layout quando i controlli si nascondono in auto-hide
        # così la card centrale NON si sposta mai di 1 pixel
        for w in (self._header_bar, self._controls_bar, self._speed_bar):
            sp = w.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            w.setSizePolicy(sp)
            w.setMouseTracking(True)
            w.installEventFilter(self)
        self._scrim.installEventFilter(self)

    # -----------------------------------------------------------------
    # LIFECYCLE & ANIMAZIONI
    # -----------------------------------------------------------------
    def open_animated(self):
        self.show()
        self.raise_()
        self.setFocus()
        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect()
        except Exception:
            pass
        if self.graphicsEffect() is None:
            self._opacity_fx = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._opacity_fx)
            self._fade_anim.setTargetObject(self._opacity_fx)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.finished.connect(lambda: self.setGraphicsEffect(None))
        self._fade_anim.start()

    def close_reader(self):
        if self._is_closing:
            return
        self._is_closing = True
        self.pause()
        self._autohide_timer.stop()

        block_idx = 0
        if self._tokens and 0 <= self._idx < len(self._tokens):
            block_idx = self._tokens[self._idx].block_idx
        elif self._tokens:
            block_idx = self._tokens[-1].block_idx

        self._fade_anim.stop()
        try:
            self._fade_anim.finished.disconnect()
        except Exception:
            pass
        if self.graphicsEffect() is None:
            self._opacity_fx = QGraphicsOpacityEffect(self)
            self._opacity_fx.setOpacity(1.0)
            self.setGraphicsEffect(self._opacity_fx)
            self._fade_anim.setTargetObject(self._opacity_fx)
        self._fade_anim.setStartValue(self._opacity_fx.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setDuration(140)

        def _finish():
            self.closedAtBlock.emit(block_idx)
            if self._on_close_block is not None:
                try:
                    self._on_close_block(block_idx)
                except Exception:
                    pass
            self.close()

        self._fade_anim.finished.connect(_finish)
        self._fade_anim.start()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            self._capture_parent_blur()
        elif hasattr(self, "_speed_bar") and obj in (
            self._scrim,
            self._header_bar,
            self._controls_bar,
            self._speed_bar,
        ):
            if event.type() in (
                QEvent.Type.MouseMove,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.Wheel,
                QEvent.Type.Enter,
            ):
                self._reveal_controls()
                if self._playing:
                    self._autohide_timer.start()
        return super().eventFilter(obj, event)

    def _is_interacting_with_ui(self) -> bool:
        if self._speed_bar._dragging:
            return True
        if (
            self._controls_bar.underMouse()
            or self._speed_bar.underMouse()
            or self._header_bar.underMouse()
        ):
            return True
        return False

    def mouseMoveEvent(self, event):
        self._reveal_controls()
        if self._playing:
            self._autohide_timer.start()
        super().mouseMoveEvent(event)

    # -----------------------------------------------------------------
    # CONTROLLO RIPRODUZIONE & TIMER COMPENSATO SUL DRIFT
    # -----------------------------------------------------------------
    def _current_token_delay_sec(self) -> float:
        if not self._tokens or self._idx >= len(self._tokens):
            return 60.0 / self._wpm
        tok = self._tokens[self._idx]
        return (60.0 / self._wpm) * tok.delay_mult

    def play(self):
        if not self._tokens:
            return
        if self._idx >= len(self._tokens) - 1 and not self._playing:
            # Se era arrivato alla fine, riparte dall'inizio
            if self._idx >= len(self._tokens):
                self._idx = 0

        # Se siamo fermi su una formula, premendo play si avanza alla parola successiva
        if self._tokens[self._idx].type == "formula":
            if self._idx + 1 < len(self._tokens):
                self._idx += 1
                self._render_current_token()
            else:
                self.pause()
                return

        self._playing = True
        self._play_btn.setText("⏸  Pausa")
        self._next_deadline = time.perf_counter() + self._current_token_delay_sec()
        self._tick_timer.start()
        self._autohide_timer.start()

    def pause(self):
        self._playing = False
        self._tick_timer.stop()
        self._autohide_timer.stop()
        self._play_btn.setText("▶  Play")
        self._reveal_controls()

    def toggle_play(self):
        if self._playing:
            self.pause()
        else:
            self.play()

    def _on_tick(self):
        if not self._playing or not self._tokens:
            return
        now = time.perf_counter()
        if now < self._next_deadline:
            return

        # Avanza al token successivo compensando il drift accumulato
        while now >= self._next_deadline:
            if self._idx + 1 >= len(self._tokens):
                self.pause()
                self._render_current_token()
                return
            self._idx += 1
            tok = self._tokens[self._idx]
            if tok.type == "formula":
                # Pausa automatica sui blocchi formula display
                self.pause()
                self._render_current_token()
                return
            self._next_deadline += self._current_token_delay_sec()

        self._render_current_token()

    def step_word(self, delta: int):
        if not self._tokens:
            return
        was_playing = self._playing
        self.pause()
        self._idx = max(0, min(len(self._tokens) - 1, self._idx + delta))
        self._render_current_token()
        if was_playing and self._tokens[self._idx].type != "formula":
            self.play()

    def step_sentence_back(self):
        if not self._tokens:
            return
        self.pause()
        i = max(0, self._idx - 1)
        while i > 0 and not (self._tokens[i - 1].is_sentence_end or self._tokens[i - 1].is_paragraph_end):
            i -= 1
        self._idx = i
        self._render_current_token()

    def set_wpm(self, wpm: int, update_bar: bool = True):
        clamped = max(RSVP_MIN_WPM, min(RSVP_MAX_WPM, int(wpm)))
        self._wpm = clamped
        self._settings.setValue("rsvp/wpm", self._wpm)
        if update_bar and hasattr(self, "_speed_bar"):
            self._speed_bar.set_wpm(self._wpm)
        self._update_stats()
        if self._playing:
            self._reveal_controls()
            self._autohide_timer.start()

    def _hide_controls_for_play(self):
        if not self._playing:
            return
        if self._is_interacting_with_ui():
            self._autohide_timer.start()
            return
        self._controls_hidden = True
        self._header_bar.hide()
        self._controls_bar.hide()
        self._speed_bar.hide()

    def _reveal_controls(self):
        if self._controls_hidden:
            self._controls_hidden = False
            self._header_bar.show()
            self._controls_bar.show()
            self._speed_bar.show()

    def _render_current_token(self):
        total = len(self._tokens)
        if total == 0:
            self._stage.show_word(RsvpToken(type="word", text="Vuoto", prefix="Vu", orp_char="o", suffix="to", orp_index=2))
            self._update_stats()
            return
        tok = self._tokens[self._idx]
        if tok.type == "formula":
            self._stage.show_formula(tok.text)
        else:
            self._stage.show_word(tok)
        self._update_stats()

    def _update_stats(self):
        total = len(self._tokens)
        curr = min(total, self._idx + 1) if total > 0 else 0
        self._progress_bar.setMaximum(max(1, total))
        self._progress_bar.setValue(curr)
        rem_sec = estimate_remaining_seconds(self._tokens, self._idx, self._wpm)
        self._stats_lbl.setText(f"{curr} / {total} · ~{format_remaining_time(rem_sec)}")

    # -----------------------------------------------------------------
    # KEYBOARD CONTROLS
    # -----------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key.Key_Escape:
            self.close_reader()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.toggle_play()
            event.accept()
            return
        if key == Qt.Key.Key_Left:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.step_sentence_back()
            else:
                self.step_word(-1)
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            self.step_word(1)
            event.accept()
            return
        if key == Qt.Key.Key_Up:
            self.set_wpm(self._wpm + RSVP_WPM_STEP)
            event.accept()
            return
        if key == Qt.Key.Key_Down:
            self.set_wpm(self._wpm - RSVP_WPM_STEP)
            event.accept()
            return

        super().keyPressEvent(event)
