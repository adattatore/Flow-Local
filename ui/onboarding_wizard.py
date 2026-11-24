"""Wizard di Onboarding guidato in 3 step (60 secondi) per FlowLocal."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.logging_config import APP_VERSION
from database import db
from ui.icons import apply_win11_titlebar, get_icon
from ui.themes import CORE_SIGNATURE_THEMES, GPU_AVAILABLE, _resolve_palette

_SETTING_ONBOARDING_DONE = "onboarding_completed"
_SETTING_USER_PROFILE = "user_profile_mode"

USER_PROFILES = (
    (
        "student",
        "Studente Universitario",
        "Perfetto per sbobinature, formule LaTeX automatiche, Flashcard e FlowTime esami.",
        "graduation",
        "dark_orb",
    ),
    (
        "developer",
        "Sviluppatore & Tech",
        "Dettatura in italiano con termini tecnici preservati, snippet e integrazione IDE/Terminale.",
        "code",
        "obsidian",
    ),
    (
        "general",
        "Produttività Generale",
        "Note rapide, email pulite, To-Do aggregati e assistente vocale Whis sempre pronto.",
        "sparkles",
        "light",
    ),
)


def should_show_onboarding() -> bool:
    """Mostra l'onboarding solo se non ancora completato e l'utente non ha già dati esistenti."""
    try:
        if (db.get_setting(_SETTING_ONBOARDING_DONE, "") or "").strip() == "1":
            return False
        has_notes = len(db.get_notes_by_hierarchy()) > 0
        has_trans = len(db.get_recent_transcriptions(limit=1)) > 0
        if has_notes or has_trans:
            db.set_setting(_SETTING_ONBOARDING_DONE, "1")
            return False
        return True
    except Exception:
        return False


def mark_onboarding_completed() -> None:
    try:
        db.set_setting(_SETTING_ONBOARDING_DONE, "1")
    except Exception:
        pass


class OnboardingWizardDialog(QDialog):
    """Configurazione guidata in 3 step: Profilo & Tema, Voce & Scorciatoie, Stato AI."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        theme_key: str = "obsidian",
        reading_mode: bool = False,
        on_theme_selected: Optional[Callable[[str], None]] = None,
        on_open_settings: Optional[Callable[[int], None]] = None,
        ollama_detected: bool = False,
    ):
        super().__init__(parent)
        self._theme_key = theme_key
        self._reading_mode = reading_mode
        self._on_theme_selected = on_theme_selected
        self._on_open_settings = on_open_settings
        self._ollama_detected = ollama_detected
        self._selected_profile = (db.get_setting(_SETTING_USER_PROFILE, "student") or "student").strip()

        self._pal = _resolve_palette(theme_key, reading_mode=reading_mode)
        self.setWindowTitle(f"Benvenuto in FlowLocal v{APP_VERSION}")
        self.setMinimumSize(640, 500)
        self.resize(680, 540)
        self.setStyleSheet(
            f"QDialog {{ background-color: {self._pal['dialog_bg']}; color: {self._pal['text']}; }}"
        )
        apply_win11_titlebar(self, theme_key, reading_mode=reading_mode)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # Header + Stepper 1-2-3
        hdr = QHBoxLayout()
        hdr_text = QVBoxLayout()
        hdr_text.setSpacing(2)
        t_lbl = QLabel("Configura FlowLocal in 60 secondi")
        t_lbl.setStyleSheet(f"color: {self._pal['title']}; font-size: 18px; font-weight: 700;")
        s_lbl = QLabel("Personalizza stile, scorciatoie vocali e motore AI locale.")
        s_lbl.setWordWrap(True)
        s_lbl.setStyleSheet(f"color: {self._pal['subtitle']}; font-size: 12px;")
        hdr_text.addWidget(t_lbl)
        hdr_text.addWidget(s_lbl)
        hdr.addLayout(hdr_text, 1)
        root.addLayout(hdr)

        # Pillole step
        step_row = QHBoxLayout()
        step_row.setSpacing(8)
        self._step_pills: list[QLabel] = []
        for idx, label in enumerate(("1. Profilo & Stile", "2. Voce & Comandi", "3. Motori AI")):
            pill = QLabel(label)
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pill.setMinimumHeight(28)
            self._step_pills.append(pill)
            step_row.addWidget(pill, 1)
        root.addLayout(step_row)

        # Stack 3 pagine
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_step_profile_and_theme())
        self._stack.addWidget(self._build_step_voice_and_shortcuts())
        self._stack.addWidget(self._build_step_ai_check())
        root.addWidget(self._stack, 1)

        # Footer navigazione
        footer = QHBoxLayout()
        footer.setSpacing(8)
        skip_btn = QPushButton("Salta")
        skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skip_btn.setMinimumHeight(34)
        skip_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {self._pal['muted']}; border: none; padding: 0 10px; font-size: 12px; }}"
            f"QPushButton:hover {{ color: {self._pal['title']}; }}"
        )
        skip_btn.clicked.connect(self._finish)
        footer.addWidget(skip_btn)

        footer.addStretch(1)

        self._back_btn = QPushButton("Indietro")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setMinimumHeight(34)
        self._back_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._pal['btn_bg']}; color: {self._pal['btn_text']};"
            f" border: 1px solid {self._pal['btn_border']}; border-radius: 8px; padding: 0 14px; font-size: 12px; }}"
            f"QPushButton:hover {{ background-color: {self._pal['btn_hover_bg']}; }}"
        )
        self._back_btn.clicked.connect(self._go_back)
        footer.addWidget(self._back_btn)

        self._next_btn = QPushButton("Avanti →")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setMinimumHeight(34)
        self._next_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._pal['accent']}; color: #FFFFFF;"
            f" border: none; border-radius: 8px; padding: 0 18px; font-size: 12px; font-weight: 700; }}"
            f"QPushButton:hover {{ background-color: #4F46E5; }}"
        )
        self._next_btn.clicked.connect(self._go_next)
        footer.addWidget(self._next_btn)

        root.addLayout(footer)
        self._sync_step_ui()

    def _card_qss(self, accent: Optional[str] = None) -> str:
        pal = self._pal
        left = f"border-left: 3px solid {accent};" if accent else ""
        return (
            f"QFrame {{ background-color: {pal['card_bg']}; border: 1px solid {pal['card_border']};"
            f" {left} border-radius: 10px; }}"
        )

    def _build_step_profile_and_theme(self) -> QWidget:
        pal = self._pal
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(12)

        p_lbl = QLabel("Come userai principalmente FlowLocal?")
        p_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700;")
        lay.addWidget(p_lbl)

        self._profile_group = QButtonGroup(self)
        self._profile_group.setExclusive(True)
        for key, title, desc, icon_name, rec_theme in USER_PROFILES:
            btn = QPushButton(f"  {title}\n  {desc}")
            btn.setCheckable(True)
            btn.setChecked(key == self._selected_profile)
            btn.setIcon(get_icon(icon_name, theme_key=self._theme_key, role="accent", size=18))
            btn.setIconSize(QSize(18, 18))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(54)
            btn.setStyleSheet(
                f"QPushButton {{ text-align: left; background-color: {pal['card_bg']}; color: {pal['text']};"
                f" border: 1px solid {pal['card_border']}; border-radius: 10px; padding: 8px 14px; font-size: 12px; }}"
                f"QPushButton:hover {{ border-color: {pal['focus_border']}; }}"
                f"QPushButton:checked {{ background-color: {pal['badge_bg']}; border: 1px solid {pal['badge_border']}; color: {pal['title']}; font-weight: 600; }}"
            )
            btn.clicked.connect(lambda _c=False, k=key, rt=rec_theme: self._select_profile(k, rt))
            self._profile_group.addButton(btn)
            lay.addWidget(btn)

        t_lbl = QLabel("Scegli uno dei 4 Temi Signature")
        t_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700; margin-top: 4px;")
        lay.addWidget(t_lbl)

        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        grid = QGridLayout()
        grid.setSpacing(8)
        for idx, (label, key, desc) in enumerate(CORE_SIGNATURE_THEMES):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(key == self._theme_key)
            b.setToolTip(desc)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setStyleSheet(
                f"QPushButton {{ background-color: {pal['pill_bg']}; color: {pal['text']};"
                f" border: 1px solid {pal['pill_border']}; border-radius: 8px; padding: 0 12px; font-size: 12px; font-weight: 600; }}"
                f"QPushButton:hover {{ border-color: {pal['focus_border']}; }}"
                f"QPushButton:checked {{ background-color: {pal['badge_bg']}; color: {pal['badge_text']}; border: 1px solid {pal['badge_border']}; }}"
            )
            b.clicked.connect(lambda _c=False, k=key: self._apply_theme_choice(k))
            self._theme_group.addButton(b)
            grid.addWidget(b, idx // 2, idx % 2)
        lay.addLayout(grid)
        lay.addStretch(1)
        return page

    def _build_step_voice_and_shortcuts(self) -> QWidget:
        pal = self._pal
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)

        intro = QLabel("Le 4 scorciatoie essenziali per andare subito al doppio della velocità:")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700;")
        lay.addWidget(intro)

        tips = (
            ("Shift Destro (tieni premuto)", "Push-to-Talk ovunque su Windows", "Detta in qualsiasi app senza perdere il focus. F8 funziona come tasto di riserva.", "#38BDF8"),
            ("2× Shift Destro  oppure  Ctrl Destro", "Modalità Mani Libere (Hands-Free)", "Tocca due volte Shift Destro (o premi Ctrl Destro mentre detti) per bloccare il microfono aperto.", "#10B981"),
            ("Ctrl + K", "Command Palette Globale", "Cerca note, cambia tema al volo, apri Novità & Suggerimenti o Diagnostica senza toccare il mouse.", "#A855F7"),
            ("Dettatura LaTeX & Correzioni inline", "Formule e ripensamenti naturali", "Di' 'integrale da 0 a infinito...' per LaTeX automatico, oppure 'Anzi, cancella tutto' per correggerti a voce.", "#F59E0B"),
        )
        for shortcut, title, desc, accent in tips:
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(self._card_qss(accent))
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 9, 12, 9)
            cl.setSpacing(3)
            top = QHBoxLayout()
            tl = QLabel(title)
            tl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700; border: none; background: transparent;")
            sc = QLabel(shortcut)
            sc.setStyleSheet(
                f"color: {accent}; background-color: {pal['pill_bg']}; border: 1px solid {pal['pill_border']};"
                f" border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 700;"
            )
            top.addWidget(tl, 1)
            top.addWidget(sc)
            cl.addLayout(top)
            dl = QLabel(desc)
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px; border: none; background: transparent;")
            cl.addWidget(dl)
            lay.addWidget(card)

        lay.addStretch(1)
        return page

    def _build_step_ai_check(self) -> QWidget:
        pal = self._pal
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(10)

        gemini_ok = bool((db.get_setting("gemini_api_key", "") or "").strip())
        checks = (
            (
                "Motore Acustico (Faster-Whisper)",
                "GPU NVIDIA CUDA rilevata — trascrizione ultra-rapida in float16." if GPU_AVAILABLE else "Modalità CPU attiva (nessuna GPU CUDA rilevata).",
                "GPU" if GPU_AVAILABLE else "CPU",
                "#10B981" if GPU_AVAILABLE else "#38BDF8",
            ),
            (
                "Motore LLM Locale (Ollama)",
                "Ollama è attivo e pronto per il raffinamento offline al 100%." if self._ollama_detected else "Ollama non rilevato in questo momento: puoi avviarlo oppure usare Gemini Cloud.",
                "Online" if self._ollama_detected else "Offline",
                "#10B981" if self._ollama_detected else "#F59E0B",
            ),
            (
                "Gemini API Cloud (Opzionale / Fallback)",
                "Chiave API Gemini configurata." if gemini_ok else "Non configurata. Puoi aggiungere una chiave gratuita da Impostazioni → Motore AI.",
                "Pronta" if gemini_ok else "Opzionale",
                "#10B981" if gemini_ok else "#94A3B8",
            ),
        )

        intro = QLabel("Riepilogo rapido del tuo sistema:")
        intro.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700;")
        lay.addWidget(intro)

        for title, desc, badge, accent in checks:
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(self._card_qss(accent))
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 10, 12, 10)
            col = QVBoxLayout()
            col.setSpacing(2)
            tl = QLabel(title)
            tl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700; border: none; background: transparent;")
            dl = QLabel(desc)
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px; border: none; background: transparent;")
            col.addWidget(tl)
            col.addWidget(dl)
            row.addLayout(col, 1)
            bl = QLabel(badge)
            bl.setStyleSheet(
                f"color: {accent}; background-color: {pal['pill_bg']}; border: 1px solid {accent};"
                f" border-radius: 7px; padding: 3px 9px; font-size: 11px; font-weight: 700;"
            )
            row.addWidget(bl, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(card)

        if self._on_open_settings is not None:
            btn_row = QHBoxLayout()
            cfg_btn = QPushButton("Apri Impostazioni Motore AI / Google")
            cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            cfg_btn.setMinimumHeight(34)
            cfg_btn.setStyleSheet(
                f"QPushButton {{ background-color: {pal['btn_bg']}; color: {pal['btn_text']};"
                f" border: 1px solid {pal['btn_border']}; border-radius: 8px; padding: 0 14px; font-size: 12px; }}"
                f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
            )
            cfg_btn.clicked.connect(lambda: (self._finish(), self._on_open_settings(2)))
            btn_row.addWidget(cfg_btn)
            btn_row.addStretch(1)
            lay.addLayout(btn_row)

        lay.addStretch(1)
        return page

    def _select_profile(self, profile_key: str, recommended_theme: str):
        self._selected_profile = profile_key
        try:
            db.set_setting(_SETTING_USER_PROFILE, profile_key)
        except Exception:
            pass
        if recommended_theme:
            self._apply_theme_choice(recommended_theme)

    def _apply_theme_choice(self, theme_key: str):
        self._theme_key = theme_key
        if self._on_theme_selected:
            self._on_theme_selected(theme_key)

    def _sync_step_ui(self):
        idx = self._stack.currentIndex()
        pal = self._pal
        for i, pill in enumerate(self._step_pills):
            if i == idx:
                pill.setStyleSheet(
                    f"background-color: {pal['badge_bg']}; color: {pal['badge_text']};"
                    f" border: 1px solid {pal['badge_border']}; border-radius: 8px; font-size: 11px; font-weight: 700;"
                )
            elif i < idx:
                pill.setStyleSheet(
                    f"background-color: {pal['pill_bg']}; color: #10B981;"
                    f" border: 1px solid {pal['pill_border']}; border-radius: 8px; font-size: 11px; font-weight: 600;"
                )
            else:
                pill.setStyleSheet(
                    f"background-color: {pal['pill_bg']}; color: {pal['muted']};"
                    f" border: 1px solid {pal['pill_border']}; border-radius: 8px; font-size: 11px;"
                )
        self._back_btn.setVisible(idx > 0)
        self._next_btn.setText("Inizia a usare FlowLocal ✓" if idx == self._stack.count() - 1 else "Avanti →")

    def _go_back(self):
        if self._stack.currentIndex() > 0:
            self._stack.setCurrentIndex(self._stack.currentIndex() - 1)
            self._sync_step_ui()

    def _go_next(self):
        if self._stack.currentIndex() < self._stack.count() - 1:
            self._stack.setCurrentIndex(self._stack.currentIndex() + 1)
            self._sync_step_ui()
        else:
            self._finish()

    def _finish(self):
        mark_onboarding_completed()
        self.accept()
