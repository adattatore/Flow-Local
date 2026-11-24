"""Sezione 'Novità e suggerimenti' (Changelog, Bug Fix e Funzioni Nascoste) per FlowLocal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.logging_config import APP_VERSION
from database import db
from ui.icons import get_icon, apply_win11_titlebar
from ui.themes import _is_dark_theme, _resolve_palette

CURRENT_WHATS_NEW_VERSION = APP_VERSION
_SETTING_LAST_SEEN = "whats_new_last_seen_version"
_SETTING_AUTO_SHOW = "whats_new_auto_show_on_update"


@dataclass(frozen=True)
class WhatsNewItem:
    category: str      # "feature" | "fix" | "tip"
    version: str       # es. "3.3.0" o "Pro Tip"
    title: str
    description: str
    shortcut: str = ""
    badge: str = ""


WHATS_NEW_ITEMS: List[WhatsNewItem] = [
    # --- NOVITÀ v3.3.0 ---
    WhatsNewItem(
        category="feature",
        version="3.3.0",
        title="Sezione Novità e Suggerimenti integrata",
        description=(
            "Un unico spazio sempre accessibile dalla barra in alto (o tramite Ctrl+K) per seguire "
            "le nuove funzioni, i bug fix risolti e scoprire le scorciatoie nascoste di FlowLocal."
        ),
        shortcut="Ctrl+K → Novità",
        badge="Nuovo",
    ),
    WhatsNewItem(
        category="feature",
        version="3.3.0",
        title="Notifiche Toast non bloccanti & Pannello Diagnostico",
        description=(
            "Gli avvisi importanti (come la scadenza della sessione Google Calendar o l'esportazione dei file) "
            "compaiono ora come toast discreti con azione rapida. Clicca sulla pillola di stato accanto al logo "
            "per verificare Whisper, Ollama/Gemini, Google Sync ed esportare il report diagnostico."
        ),
        badge="Nuovo",
    ),
    WhatsNewItem(
        category="feature",
        version="3.3.0",
        title="Logging rotante automatico & avvio più pulito",
        description=(
            "Rimossi i banner superflui all'avvio e introdotto un file di log rotante nella cartella dati utente, "
            "pronto da allegare in caso di diagnosi senza esporre mai chiavi API o token."
        ),
    ),
    # --- BUG FIX RECENTI ---
    WhatsNewItem(
        category="fix",
        version="3.3.0",
        title="Risolto 'Errore salvataggio' durante la modifica delle note",
        description=(
            "Corretta la desincronizzazione dei trigger FTS5 (COALESCE su colonne NULL) che causava "
            "errori SQLite al salvataggio automatico, aggiunto auto-healing automatico dell'indice FTS5, "
            "busy_timeout SQLite e protezione contro sovrapposizione dei thread di salvataggio."
        ),
        badge="Fix",
    ),
    WhatsNewItem(
        category="fix",
        version="3.3.0",
        title="Risolto testo barrato indesiderato nei To-Do dopo Invio",
        description=(
            "Premendo Invio su un To-Do (o digitando subito dopo aver creato/despuntato una voce) il cursore "
            "ora azzera sempre blockCharFormat e currentCharFormat: nessun testo tagliato per errore."
        ),
        badge="Fix",
    ),
    WhatsNewItem(
        category="fix",
        version="3.3.0",
        title="Risolto crash all'apertura di 'Tutti i To-Do'",
        description=(
            "Corretto il KeyError ('bg') che impediva l'apertura del riepilogo globale dei To-Do in FlowNotes "
            "e introdotta una palette tipizzata con fallback automatici (SafePalette) su tutti i 12 temi."
        ),
        badge="Fix",
    ),
    WhatsNewItem(
        category="fix",
        version="3.3.0",
        title="Gestione graziosa della sessione Google Calendar scaduta",
        description=(
            "Quando il token OAuth viene revocato (invalid_grant), l'app non fallisce silenziosamente in console "
            "ma propone un toast discreto con accesso diretto alla riconnessione."
        ),
        badge="Fix",
    ),
    WhatsNewItem(
        category="fix",
        version="3.2.x",
        title="Barriera anti-tag HTML spuri & fedeltà lessicale",
        description=(
            "Rafforzato il filtro post-LLM per eliminare eventuali tag <p>/<br> indesiderati e impedire "
            "troncamenti o espansioni non pronunciate durante la dettatura live."
        ),
    ),
    # --- FUNZIONI NASCOSTE & SUGGERIMENTI PRO ---
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="Modalità Mani Libere (Hands-Free) con doppio tocco",
        description=(
            "Premi due volte rapidamente Shift Destro per bloccare il microfono aperto senza tenere premuto il tasto. "
            "In alternativa, mentre stai già dettando con Shift Destro, premi Ctrl Destro o AltGr per fare Push-to-Lock."
        ),
        shortcut="2× Shift Dx / Ctrl Dx",
        badge="Nascosta",
    ),
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="Comandi vocali inline durante la dettatura",
        description=(
            "Se sbagli una frase mentre detti, non serve cancellare a mano: pronuncia "
            "'Anzi, cancella tutto' oppure 'Anzi no, ...' e FlowLocal ripulisce automaticamente il testo precedente."
        ),
        badge="Trucco",
    ),
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="Dettatura automatica di formule LaTeX",
        description=(
            "Detta naturalmente espressioni come 'integrale da 0 a infinito di e alla meno x in dx' oppure "
            "'F uguale m per a' o 'vettore v': vengono convertite automaticamente in LaTeX standard ($...$)."
        ),
        badge="Studio",
    ),
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="Command Palette globale per navigare senza mouse",
        description=(
            "Premi Ctrl+K da qualsiasi vista dell'Hub per cercare note, cambiare tema al volo, "
            "aprire la diagnostica o saltare istantaneamente a FlowTime, Flashcard e Whis."
        ),
        shortcut="Ctrl+K",
        badge="Velocità",
    ),
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="WikiLink [[Nota]], Knowledge Graph e RSVP Reader",
        description=(
            "Collega gli appunti scrivendo [[Titolo Nota]] nell'editor scientifico per costruire il tuo grafo "
            "di conoscenza. Nelle note lunghe puoi attivare l'RSVP Reader per leggere fino a 400+ parole/min."
        ),
        badge="Studio",
    ),
    WhatsNewItem(
        category="tip",
        version="Pro Tip",
        title="Modalità Lettura Adattiva non distruttiva",
        description=(
            "Il pulsante 'Lettura' nella barra in alto adatta il tema attivo con toni caldi riposanti "
            "e attenua del 50% il contrasto dei bordi secondari, mantenendo intatti colori d'accento e layout."
        ),
        badge="Comfort",
    ),
]

_CATEGORY_META = {
    "feature": ("Novità", "#38BDF8", "sparkles"),
    "fix": ("Bug Fix", "#10B981", "check"),
    "tip": ("Suggerimento", "#A855F7", "lightbulb"),
}


def has_unread_whats_new() -> bool:
    """Restituisce True se l'utente non ha ancora visto le novità della versione corrente."""
    try:
        seen = (db.get_setting(_SETTING_LAST_SEEN, "") or "").strip()
        return seen != CURRENT_WHATS_NEW_VERSION
    except Exception:
        return False


def mark_whats_new_seen() -> None:
    try:
        db.set_setting(_SETTING_LAST_SEEN, CURRENT_WHATS_NEW_VERSION)
    except Exception:
        pass


def should_auto_show_whats_new() -> bool:
    try:
        auto_val = (db.get_setting(_SETTING_AUTO_SHOW, "1") or "1").strip()
        return auto_val != "0" and has_unread_whats_new()
    except Exception:
        return False


class WhatsNewDialog(QDialog):
    """Dialogo moderno 'Novità e suggerimenti' con filtri per categoria e ricerca istantanea."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        theme_key: str = "obsidian",
        reading_mode: bool = False,
        initial_filter: str = "all",
    ):
        super().__init__(parent)
        self._theme_key = theme_key
        self._reading_mode = reading_mode
        self._active_filter = initial_filter

        pal = _resolve_palette(theme_key, reading_mode=reading_mode)
        self._pal = pal

        self.setWindowTitle(f"Novità e Suggerimenti — FlowLocal v{APP_VERSION}")
        self.setMinimumSize(640, 520)
        self.resize(700, 580)
        self.setStyleSheet(
            f"QDialog {{ background-color: {pal['dialog_bg']}; color: {pal['text']}; }}"
        )
        apply_win11_titlebar(self, theme_key, reading_mode=reading_mode)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        hdr_text = QVBoxLayout()
        hdr_text.setSpacing(2)
        title_lbl = QLabel("Novità e Suggerimenti")
        title_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 18px; font-weight: 700;")
        sub_lbl = QLabel(
            f"Versione {APP_VERSION} · Aggiornamenti, bug fix e funzioni nascoste per usare FlowLocal al massimo"
        )
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px;")
        hdr_text.addWidget(title_lbl)
        hdr_text.addWidget(sub_lbl)
        hdr.addLayout(hdr_text, 1)

        ver_badge = QLabel(f"v{APP_VERSION}")
        ver_badge.setStyleSheet(
            f"color: {pal['badge_text']}; background-color: {pal['badge_bg']};"
            f"border: 1px solid {pal['badge_border']}; border-radius: 9px;"
            f"padding: 4px 10px; font-size: 11px; font-weight: 700;"
        )
        hdr.addWidget(ver_badge, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(hdr)

        # Controlli fissi fuori dalla QScrollArea (AGENTS.md §7): filtri + ricerca
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for key, label in (
            ("all", "Tutto"),
            ("feature", "Novità"),
            ("fix", "Bug Fix"),
            ("tip", "Funzioni nascoste & Tips"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == initial_filter)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(30)
            btn.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: {pal['pill_bg']};"
                f"  color: {pal['subtitle']};"
                f"  border: 1px solid {pal['pill_border']};"
                f"  border-radius: 15px;"
                f"  padding: 0 12px;"
                f"  font-size: 12px;"
                f"  font-weight: 600;"
                f"}}"
                f"QPushButton:hover {{ background-color: {pal['pill_hover_bg']}; color: {pal['title']}; }}"
                f"QPushButton:checked {{"
                f"  background-color: {pal['badge_bg']};"
                f"  color: {pal['badge_text']};"
                f"  border: 1px solid {pal['badge_border']};"
                f"}}"
            )
            btn.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
            self._filter_group.addButton(btn)
            filter_row.addWidget(btn)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        self._search_input = QLineEdit()
        self._search_input.setMinimumHeight(36)
        self._search_input.setPlaceholderText("Cerca novità, bug fix o scorciatoie (es. LaTeX, Shift, Google)...")
        self._search_input.setStyleSheet(
            f"QLineEdit {{"
            f"  background-color: {pal['input_bg']};"
            f"  color: {pal['input_text']};"
            f"  border: 1px solid {pal['input_border']};"
            f"  border-radius: 9px;"
            f"  padding: 0 12px;"
            f"  font-size: 13px;"
            f"}}"
            f"QLineEdit:focus {{ border: 1px solid {pal['focus_border']}; }}"
        )
        self._search_input.textChanged.connect(self._render_items)
        root.addWidget(self._search_input)

        # Lista scorrevole anti-compressione
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 2, 6, 2)
        self._list_layout.setSpacing(9)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        # Footer
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self._auto_show_cb = QCheckBox("Mostra automaticamente dopo un nuovo aggiornamento")
        try:
            auto_enabled = (db.get_setting(_SETTING_AUTO_SHOW, "1") or "1").strip() != "0"
        except Exception:
            auto_enabled = True
        self._auto_show_cb.setChecked(auto_enabled)
        self._auto_show_cb.setStyleSheet(f"color: {pal['subtitle']}; font-size: 12px;")
        self._auto_show_cb.toggled.connect(self._on_auto_show_toggled)
        footer.addWidget(self._auto_show_cb)
        footer.addStretch(1)

        close_btn = QPushButton("Ho capito")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setMinimumHeight(34)
        close_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {pal['btn_bg']};"
            f"  color: {pal['title']};"
            f"  border: 1px solid {pal['btn_border']};"
            f"  border-radius: 8px;"
            f"  padding: 0 18px;"
            f"  font-size: 12px;"
            f"  font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{ background-color: {pal['btn_hover_bg']}; }}"
        )
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

        self._render_items()
        mark_whats_new_seen()

    def _on_auto_show_toggled(self, checked: bool):
        try:
            db.set_setting(_SETTING_AUTO_SHOW, "1" if checked else "0")
        except Exception:
            pass

    def _set_filter(self, key: str):
        self._active_filter = key
        self._render_items()

    def _render_items(self):
        while self._list_layout.count():
            child = self._list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        query = self._search_input.text().strip().lower()
        pal = self._pal
        matched = 0

        for item in WHATS_NEW_ITEMS:
            if self._active_filter != "all" and item.category != self._active_filter:
                continue
            if query:
                haystack = f"{item.title} {item.description} {item.shortcut} {item.version} {item.badge}".lower()
                if query not in haystack:
                    continue

            matched += 1
            cat_label, accent, icon_name = _CATEGORY_META.get(item.category, ("Info", "#38BDF8", "info"))

            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            card.setStyleSheet(
                f"QFrame {{"
                f"  background-color: {pal['card_bg']};"
                f"  border: 1px solid {pal['card_border']};"
                f"  border-left: 3px solid {accent};"
                f"  border-radius: 10px;"
                f"}}"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 11, 14, 11)
            cl.setSpacing(6)

            top_row = QHBoxLayout()
            top_row.setSpacing(8)

            ic_lbl = QLabel()
            ic_lbl.setStyleSheet("background: transparent; border: none;")
            ic_lbl.setPixmap(get_icon(icon_name, theme_key=self._theme_key, color=accent, size=15).pixmap(QSize(15, 15)))
            top_row.addWidget(ic_lbl)

            t_lbl = QLabel(item.title)
            t_lbl.setWordWrap(True)
            t_lbl.setStyleSheet(f"color: {pal['title']}; font-size: 13px; font-weight: 700; border: none; background: transparent;")
            top_row.addWidget(t_lbl, 1)

            pill_text = f"{cat_label} · {item.version}" if item.version else cat_label
            cat_pill = QLabel(pill_text)
            cat_pill.setStyleSheet(
                f"color: {accent}; background-color: {pal['pill_bg']};"
                f"border: 1px solid {pal['pill_border']}; border-radius: 8px;"
                f"padding: 2px 8px; font-size: 10px; font-weight: 600;"
            )
            top_row.addWidget(cat_pill)
            cl.addLayout(top_row)

            desc_lbl = QLabel(item.description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                f"color: {pal['subtitle']}; font-size: 12px; line-height: 1.4; border: none; background: transparent;"
            )
            cl.addWidget(desc_lbl)

            if item.shortcut:
                sc_row = QHBoxLayout()
                sc_row.setContentsMargins(0, 2, 0, 0)
                sc_lbl = QLabel(f"Scorciatoia: {item.shortcut}")
                sc_lbl.setStyleSheet(
                    f"color: {pal['shortcut_key_text']}; background-color: {pal['shortcut_key_bg']};"
                    f"border: 1px solid {pal['shortcut_key_border']}; border-radius: 6px;"
                    f"padding: 2px 8px; font-size: 11px; font-weight: 600;"
                )
                sc_row.addWidget(sc_lbl)
                sc_row.addStretch(1)
                cl.addLayout(sc_row)

            self._list_layout.addWidget(card)

        if matched == 0:
            empty = QLabel("Nessun elemento trovato per questo filtro o ricerca.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {pal['muted']}; font-size: 13px; padding: 32px;")
            self._list_layout.addWidget(empty)

        self._list_layout.addStretch(1)
