"""
Whis AI Agent — Assistente Virtuale Ingegneristico per FlowLocal
Caratteristiche:
- Modello rapido + Vision (default): qwen3.5:4b (latenza minima e multimodalità nativa)
- Modello alta precisione + Vision: qwen3.5:9b (attivato per analisi profonda o codice complesso)
- Modello leggero + Vision: qwen3.5:2b (per profili leggeri)
- Modalità configurabile: 'auto' (4B/9B dinamico), 'qwen3.5:4b', 'qwen3.5:9b', 'qwen3.5:2b'
- Personalità: calmo, brillante, arguto, sintetico, 2-4 frasi dritte al punto (salvo codice)
- Deissi / Clipboard context: lettura automatica appunti se il comando cita 'questo errore/codice/testo...'
- Memoria di sessione: ultimi 5 scambi per follow-up coerenti
- Persistenza automatica su SQLite (`whis_history`)
- Bus di stato reattivo con segnali PyQt6 per HUD e Hub Window
"""

import time
import re
import json
import base64
import ctypes
import datetime
import sys
import urllib.request
import urllib.error
from io import BytesIO
import pyperclip
from typing import Callable, Optional, List, Dict, Tuple, Any
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core.gemini_client import (
    DEFAULT_GEMINI_TIMEOUT,
    get_ai_routing_config,
    set_active_runtime_engine,
    should_use_gemini,
    stream_generate_content as gemini_stream_generate,
)

# Import per Screen OCR e Visione Schermo
try:
    import mss
except ImportError:
    mss = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine_whis: Optional[RapidOCR] = None

    def _get_ocr():
        global _ocr_engine_whis
        if _ocr_engine_whis is None:
            _ocr_engine_whis = RapidOCR()
        return _ocr_engine_whis
except ImportError:
    _get_ocr = lambda: None

OLLAMA_API_URL = "http://localhost:11434/api/chat"


def strip_think_blocks(text: str) -> str:
    """Rimuove eventuali blocchi di ragionamento <think>...</think> dall'output LLM."""
    if not text:
        return ""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

DEEP_ANALYSIS_KEYWORDS = re.compile(
    r'\b('
    r'analizza|analisi|approfondisci|approfondita|approfondito|dettagliata|dettagliato|'
    r'architettura|refactor|refactoring|algoritmo|classe|funzione|'
    r'implementa|debug|debugger|spiega nel dettaglio|ottimizza|ottimizzazione|'
    r'progetta|progettazione|benchmark|concorrenza|multithread|complessit[aà]|'
    r'scrivi un codice|scrivi uno script|sviluppa'
    r')\b',
    re.IGNORECASE
)

CLIPBOARD_TRIGGER_KEYWORDS = re.compile(
    r'\b('
    r'questo codice|quest\'errore|questo errore|questo testo|questo snippet|'
    r'quello che ho copiato|cosa ho copiato|nella clipboard|negli appunti|'
    r'dagli appunti|appunti di windows|correggi questo|spiegami questo|'
    r'traduci questo|analizza questo|guarda questo|su questo|'
    r'questo script|questi dati|questa funzione|questa classe|'
    r'questa eccezione|questo traceback|questo log|questa riga'
    r')\b',
    re.IGNORECASE
)
DEICTIC_KEYWORDS = CLIPBOARD_TRIGGER_KEYWORDS

# Trigger per Screen OCR: regex ampliata per intercettare qualsiasi domanda su schermo, slide, monitor, finestra, visione
SCREEN_TRIGGER_KEYWORDS = re.compile(
    r"\b(schermo|monitor|slide|pagina|finestr[ae]|ved[ieo]|vedendo|guarda\w*|legg[ie]\w*|mostr\w*|davanti)\b",
    re.IGNORECASE
)

# Trigger per Azioni DB avanzate sul Quaderno di Studio (Crea, Unisci, Elimina, Modifica)
NOTE_MERGE_KEYWORDS = re.compile(
    r'\b(?:unisci|fondi|aggrega)\b.*?\b(?:la\s+)?nota\b\s+(?:di|su|chiamata|intitolata)?\s*(.*?)\s+(?:a|con|ed?)\s+(?:quella|la\s+nota)\s+(?:di|su|chiamata|intitolata)?\s*(.*)$',
    re.IGNORECASE
)

NOTE_DELETE_KEYWORDS = re.compile(
    r'\b(?:elimina|cancella|rimuovi)\b.*?\b(?:la\s+)?nota\b\s+(?:di|su|chiamata|intitolata)?\s*(.*)$',
    re.IGNORECASE
)

NOTE_APPEND_KEYWORDS = re.compile(
    r'\b(?:aggiungi|accoda|modifica)\b.*?\b(?:alla|nella|la)\s+nota\b\s+(?:di|su|chiamata)?\s*(.*?)(?:\s*:\s*|\s+il\s+testo\s+|\s+con\s+)(.*)$',
    re.IGNORECASE
)

# Trigger per Cancellazione Appuntamenti Google Calendar
CALENDAR_DELETE_KEYWORDS = re.compile(
    r'\b('
    r'(?:cancell(?:a|are|ami|armi)?|annull(?:a|are|ami|armi)?|elimin(?:a|are|ami|armi)?|rimuov(?:i|ere|imi|ermi)?|disdi(?:ci|re|cimi)?|togl(?:i|iere|imi|ermi)?)'
    r'\s+(?:l[\'’]|il|un|questo|quell[\'’]|quello|mio|il\s+mio\s+)?(?:appuntamento|evento|impegno|incontro)\b|'
    r'(?:cancell(?:a|are|ami|armi)?|annull(?:a|are|ami|armi)?|elimin(?:a|are|ami|armi)?|rimuov(?:i|ere|imi|ermi)?|disdi(?:ci|re|cimi)?|togl(?:i|iere|imi|ermi)?)'
    r'\s+(?:dal|sul|nel|in|dall[\'’]|dalla|dall)\s+(?:mio\s+)?(?:calendario|agenda)\b|'
    r'(?:disdici|disdire|disdicimi)\b|'
    r'non\s+ho\s+pi[ùu]\s+(?:l[\'’]|il|un|questo|alcun\s+)?(?:appuntamento|impegno|evento)\b'
    r')',
    re.IGNORECASE
)

# Trigger per Micro-Briefing del giorno
CALENDAR_BRIEFING_KEYWORDS = re.compile(
    r'\b('
    r'aggiornami|'
    r'buongiorno\s+whis|'
    r'whis\s*,?\s*buongiorno|'
    r'briefing|'
    r'resoconto\s+(?:del\s+giorno|della\s+giornata)|'
    r'come\s+[eè]\s+la\s+mia\s+giornata|'
    r'cosa\s+mi\s+aspetta\s+oggi'
    r')\b',
    re.IGNORECASE
)

# Trigger per Azioni Google Calendar (Creazione, Cancellazione e Lettura appuntamenti)
CALENDAR_CREATE_KEYWORDS = re.compile(
    r'\b('
    r'(?:prend(?:i|ere|imi)|segn(?:a|are|ami)|fiss(?:a|are|ami)|aggiung(?:i|ere|imi)|cre(?:a|are|ami)|mett(?:i|ere|imi)|inserisc(?:i|ere|imi)|registr(?:a|are|ami))'
    r'\s+(?:un\s+)?(?:appuntamento|evento|impegno|incontro)\b|'
    r'(?:mett(?:i|ere|imi)|segn(?:a|are|ami)|aggiung(?:i|ere|imi)|inserisc(?:i|ere|imi)|registr(?:a|are|ami))'
    r'\s+(?:sul|al|nel|in|su|all[\'’]|nel\s+mio|sul\s+mio)\s+(?:calendario|agenda)\b|'
    r'ho\s+un\s+appuntamento\s+giorno'
    r')',
    re.IGNORECASE
)

# Trigger ultra-flessibile per Domande e Lettura Google Calendar (giornata, orari, pomeriggio, cosa devo fare...)
WEEKDAY_REGEX = r'(?:luned[ìi]|marted[ìi]|mercoled[ìi]|gioved[ìi]|venerd[ìi]|sabato|domenica)'
TEMPORAL_REGEX = (
    r'(?:oggi|domani|dopodomani|stasera|stamattina|stamane|stanotte|'
    r'(?:questo\s+)?pomeriggio|(?:questa\s+)?settimana|(?:il\s+prossimo\s+)?mese|'
    r'dopo|dopo\s+pranzo|in\s+programma|in\s+agenda|in\s+calendario|sul\s+calendario|nel\s+calendario|'
    + WEEKDAY_REGEX + r'(?:\s+\d{1,2})?|(?:il\s+|del\s+|giorno\s+|per\s+il\s+)?\d{1,2})'
)
CALENDAR_READ_KEYWORDS = re.compile(
    r'\b('
    r'che\s+(?:[eè]\s+)?impegn[io](?:\s+[oòh])?|'
    r'l[\'’]?impegn[io]\b|'
    r'(?:ho|hai|abbiamo|ci\s+sono)\s+(?:qualche\s+|un\s+|degli\s+|dei\s+)?(?:impegn[io]|appuntament[io]|event[io])\b|'
    r'(?:che|quali|cosa|mostra|mostrami|dimmi|leggi|controlla|vedi|guarda|ho|ci\s+sono)?\s*'
    r'(?:(?:il|lo|la|i|gli|le|un|sul|nel|in|dal|del|al|il\s+mio|i\s+miei|gli\s+altri|prossim[ioa])\s+)*'
    r'(?:impegn[io]|appuntament[io]|event[io]|agenda|calendario|programma)'
    r'(?:\s+(?:ho|abbiamo|ci\s+sono|avrei|avessi|per|di|il|del|nel|sul))*\s*'
    + TEMPORAL_REGEX + r'|'
    r'(?:prossim[ioa]\s+(?:impegn[io]|appuntament[io]|event[io]))|'
    r'cosa\s+ho\s+(?:in|sul|nel)\s+(?:calendario|agenda|programma)|'
    r'(?:che\s+)?cosa\s+(?:ho|abbiamo|c[\'’][eè]|faccio|devo\s+fare|ho\s+da\s+fare)\s+(?:per\s+)?' + TEMPORAL_REGEX + r'|'
    r'(?:ho|abbiamo|c[\'’][eè])\s+qualcosa\s+(?:da\s+fare\s+)?(?:per\s+)?' + TEMPORAL_REGEX + r'|'
    r'(?:' + WEEKDAY_REGEX + r'(?:\s+\d{1,2})?|oggi|domani|dopodomani)\s+(?:che\s+)?(?:ho|abbiamo|c[\'’][eè]|ci\s+sono|impegn[io]|appuntament[io]|sono\s+(?:libero|impegnato|occupato)|ho\s+qualcosa)|'
    r'quali\s+(?:sono\s+(?:i\s+miei\s+|gli\s+)?)?(?:appuntamenti|impegni)|'
    r'sono\s+(?:libero|impegnato|occupato)(?:\s+(?:per\s+)?' + TEMPORAL_REGEX + r')?'
    r')\b',
    re.IGNORECASE
)


def normalize_calendar_prompt(prompt: str) -> str:
    """Normalizza errori fonetici tipici di Whisper sui comandi italiani per il calendario."""
    norm = prompt
    # "impegnò" / "impegnio" -> "impegni ho" / "impegno" in contesto di domanda/agenda
    norm = re.sub(
        r'^\s*impegnò\b',
        'impegni ho',
        norm,
        flags=re.IGNORECASE,
    )
    norm = re.sub(
        r'\b(che|quali|se\s+ho|ho|miei|gli)\s+impegnò\b',
        r'\1 impegni ho',
        norm,
        flags=re.IGNORECASE,
    )
    norm = re.sub(
        r'\bimpegnò\s+(?=(?:oggi|domani|dopodomani|settimana|mese|stasera|stamattina|pomeriggio|per\b|il\b|' + WEEKDAY_REGEX + r'|\d{1,2}\b))',
        'impegni ho ',
        norm,
        flags=re.IGNORECASE,
    )
    norm = re.sub(r'\bimpegnio\b', 'impegno', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bche\s+(?:[eè]\s+)?impegn[io]\s+[oòh]\b', 'che impegni ho', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bche\s+impegno\b', 'che impegni', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bl[\'’]impegno\b', 'gli impegni', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bcos[\'’]ho\b', 'cosa ho', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bche\s+ho\b', 'cosa ho', norm, flags=re.IGNORECASE)
    norm = re.sub(r'\bche\s+c[\'’][eè]\b', "cosa c'è", norm, flags=re.IGNORECASE)
    return norm


def generate_date_context_table() -> str:
    """Genera una stringa riassuntiva con data e ora corrente e i prossimi 14 giorni con giorno della settimana in italiano."""
    now = datetime.datetime.now()
    days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    entries = [f"Oggi: {days_it[now.weekday()]} {now.strftime('%Y-%m-%d')} (ore {now.strftime('%H:%M')})"]
    for i in range(1, 15):
        d = now + datetime.timedelta(days=i)
        dow = days_it[d.weekday()]
        label = "Domani" if i == 1 else ("Dopodomani" if i == 2 else dow)
        entries.append(f"{label}: {dow} {d.strftime('%Y-%m-%d')}")
    return " | ".join(entries)


# Regex di cattura ampia per l'intento CREATE_NOTE:
# "Prendi un appunto...", "Segna un appunto...", "Prendi nota...", "Segna negli appunti...",
# "Crea una nota...", "Aggiungi un appunto...", "Salva una nota...", "Scrivi una nota...",
# "Annota che...", "Appunta che...", "Ricordami di..."
NOTE_CREATE_KEYWORDS = re.compile(
    r'\b(?:(?:prendi|segna|crea|aggiungi|scrivi|salva|annota|ricordami)\s+(?:un\s+|una\s+|negli\s+|nelle\s+)?(?:appunt[oi]|not[ae])|(?:annota|appunta)\s+che)\b(?:\s*:\s*|\s+)?(.*)',
    re.IGNORECASE | re.DOTALL
)
NOTE_ACTION_KEYWORDS = NOTE_CREATE_KEYWORDS


def extract_clean_note_content(raw_text: str) -> str:
    """
    Estrae e ripulisce il testo effettivo di un appunto da frasi parlate.
    Es. "sull'acqua che è trasparente" -> "L'acqua è trasparente"
    Es. "che domani c'è lezione" -> "Domani c'è lezione"
    """
    text = (raw_text or "").strip()
    if not text:
        return ""

    # Rimuove punteggiatura introduttiva
    text = re.sub(r'^[:,\-\s]+', '', text)

    # Gestione preposizioni articolate all'inizio
    articled_prepositions = [
        (r"^sull['’]\s*", "l'"),
        (r"^sullo\s+", "lo "),
        (r"^sulla\s+", "la "),
        (r"^sul\s+", "il "),
        (r"^sui\s+", "i "),
        (r"^sugli\s+", "gli "),
        (r"^sulle\s+", "le "),
        (r"^dell['’]\s*", "l'"),
        (r"^dello\s+", "lo "),
        (r"^della\s+", "la "),
        (r"^del\s+", "il "),
        (r"^dei\s+", "i "),
        (r"^degli\s+", "gli "),
        (r"^delle\s+", "le "),
        (r"^(?:su|di|a|che|in|per)\s+", ""),
    ]
    for pattern, repl in articled_prepositions:
        if re.search(pattern, text, flags=re.IGNORECASE):
            text = re.sub(pattern, repl, text, count=1, flags=re.IGNORECASE)
            break

    # Se dopo la preposizione compare 'che è' / 'che ha' / 'che sono' / 'che era':
    # es: "l'acqua che è trasparente" -> "l'acqua è trasparente"
    text = re.sub(r'^(l[\'’]\w+|[iIlL][oaie]\s+\w+)\s+che\s+(è|e|ha|sono|hanno|era|erano|fu|sarà)\b', r'\1 \2', text, flags=re.IGNORECASE)

    text = text.strip()
    if text:
        if text.lower().startswith("l'") and len(text) > 2:
            text = "L'" + text[2:]
        elif text.lower().startswith("l’") and len(text) > 2:
            text = "L’" + text[2:]
        else:
            text = text[0].upper() + text[1:]
    return text


def get_active_window_title() -> str:
    """Recupera il titolo della finestra attiva in primo piano."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.strip()
                    if title:
                        return title
        except Exception as e:
            print(f"[Whis Active Window Warning] {e}")
    try:
        from core.context_detector import get_foreground_window_info
        title, exe = get_foreground_window_info()
        if title:
            return f"{title} ({exe})" if exe else title
        if exe:
            return exe
    except Exception:
        pass
    return "Finestra Attiva Sconosciuta"


def capture_screen_text() -> str:
    """Cattura lo schermo intero e ne estrae il testo visibile tramite RapidOCR."""
    ocr = _get_ocr()
    if ocr is None:
        print("[Whis Screen OCR] Modulo OCR locale non disponibile (RapidOCR non inizializzato).")
        return ""

    # Metodo 1: mss.MSS() ad altissime prestazioni
    if mss is not None and np is not None:
        try:
            with mss.MSS() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(mon)
                arr = np.array(shot)[:, :, :3]
                res, _ = ocr(arr)
                if res:
                    lines = [line[1].strip() for line in res if len(line) > 1 and line[1] and line[1].strip()]
                    return "\n".join(lines)
        except Exception as e:
            print(f"[Whis Screen OCR mss warning] {e}")

    # Metodo 2: Fallback Pillow ImageGrab
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        arr = np.array(img)
        res, _ = ocr(arr)
        if res:
            lines = [line[1].strip() for line in res if len(line) > 1 and line[1] and line[1].strip()]
            return "\n".join(lines)
    except Exception as e:
        print(f"[Whis Screen OCR ImageGrab fallback warning] {e}")

    return ""


def capture_screen_image_base64(max_dim: int = 1280) -> str:
    """Cattura lo schermo intero come JPEG compresso in base64 per la Vision nativa di Qwen 3.5."""
    try:
        from PIL import Image
    except ImportError:
        return ""

    pil_img = None
    if mss is not None:
        try:
            with mss.MSS() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(mon)
                pil_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            print(f"[Whis Vision mss warning] {e}")

    if pil_img is None:
        try:
            from PIL import ImageGrab
            pil_img = ImageGrab.grab().convert("RGB")
        except Exception as e:
            print(f"[Whis Vision ImageGrab warning] {e}")
            return ""

    try:
        w, h = pil_img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            pil_img = pil_img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        buf = BytesIO()
        pil_img.save(buf, format="JPEG", quality=82, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print(f"[Whis Vision encode warning] {e}")
        return ""


WHIS_SYSTEM_PROMPT = (
    "Sei Whis, l'assistente virtuale integrato di FlowLocal. "
    "Sei brillante, calmo, arguto, sintetico ed estremamente competente. "
    "La tua personalità è ispirata alla serena autorevolezza e sagacia di Whis, "
    "ma sei sobrio e rigorosamente focalizzato su ingegneria del software, produttività, studio scientifico e sistemi.\n\n"
    "REGOLE TASSATIVE:\n"
    "1. Massima brevità ed efficacia: rispondi in massimo 2-4 frasi dritte al punto.\n"
    "2. Fai eccezione alla brevità SOLO se l'utente richiede esplicitamente codice, script, debug o spiegazioni approfondite: "
    "in tal caso fornisci codice pulito, idiomatico, commentato ed essenziale, senza preamboli ovvi.\n"
    "3. Se pronunci o tratti formule matematiche o fisiche, racchiudile SEMPRE in codice LaTeX standard tra simboli $...$.\n"
    "4. Tono: impeccabile, sereno, arguto ed elegante. Mai prolisso né servile.\n"
    "5. Rispondi sempre in italiano, a meno che l'utente non si rivolga a te in altra lingua.\n"
    "6. INTEGRAZIONE NATIVA CON GOOGLE CALENDAR E SISTEMA: FlowLocal è pienamente integrato con Google Calendar via API. "
    "Non dire MAI che non puoi interagire con calendari esterni o che l'utente deve farlo da solo: FlowLocal gestisce già creazione, cancellazione e consultazione dell'agenda."
)


class WhisAgent(QObject):
    """Modulo di intelligenza conversazionale per Whis in FlowLocal con segnali Qt."""

    status_changed = pyqtSignal(str, str)     # (status_text, current_query)
    interaction_saved = pyqtSignal(dict)      # (saved_interaction_dict)
    note_created = pyqtSignal(int, str)       # (note_id, title)

    def __init__(self, default_model: str = "qwen3.5:4b", deep_model: str = "qwen3.5:9b", light_model: str = "qwen3.5:2b", parent=None):
        super().__init__(parent)
        self.default_model = default_model
        self.deep_model = deep_model
        self.light_model = light_model
        self.model_mode = "auto"  # "auto", "qwen3.5:4b", "qwen3.5:9b", "qwen3.5:2b"
        self.history: List[Dict[str, str]] = []
        self.current_status = "In attesa"
        self.last_query = ""
        self.current_profile = "Alessandro"
        self.current_subject = "Generale"
        self.current_topic = "Generale"
        self.pending_calendar_create: Optional[Dict[str, Any]] = None
        self._last_screen_b64: str = ""
        try:
            from database.db_manager import db
            saved_mode = db.get_setting("whis_model_mode", None)
            if saved_mode:
                self.set_model_mode(saved_mode)
                if "qwen2.5" in saved_mode.lower():
                    db.set_setting("whis_model_mode", self.model_mode)
        except Exception:
            pass

    def set_study_context(self, profile: str = "Alessandro", subject: str = "Generale", topic: str = "Generale"):
        """Aggiorna il contesto accademico corrente per Whis (Materia, Argomento, Profilo)."""
        if profile and profile.strip().lower() not in ("tutti i profili", "tutti", "all", ""):
            self.current_profile = profile.strip()
        self.current_subject = (subject or "Generale").strip()
        self.current_topic = (topic or "Generale").strip()

    def set_model_mode(self, mode: str):
        """Imposta la modalità di selezione del modello: 'auto', 'qwen3.5:4b', 'qwen3.5:9b', 'qwen3.5:2b'."""
        mode_clean = (mode or "auto").lower().strip()
        if "qwen2.5" in mode_clean:
            # Migrazione silenziosa da qualsiasi vecchio tag qwen2.5
            self.model_mode = "qwen3.5:4b"
        elif mode_clean in ("3b", "4b", "rapido", "qwen3.5:4b"):
            self.model_mode = "qwen3.5:4b"
        elif mode_clean in ("7b", "9b", "profondo", "qwen3.5:9b"):
            self.model_mode = "qwen3.5:9b"
        elif mode_clean in ("2b", "leggero", "qwen3.5:2b"):
            self.model_mode = "qwen3.5:2b"
        else:
            self.model_mode = "auto"

    def get_model_mode(self) -> str:
        return self.model_mode

    def select_model(self, prompt: str) -> str:
        """Determina dinamicamente se impiegare il modello 4B rapido, 9B alta precisione o 2B leggero."""
        if self.model_mode == "qwen3.5:4b":
            return self.default_model
        if self.model_mode == "qwen3.5:9b":
            return self.deep_model
        if self.model_mode == "qwen3.5:2b":
            return self.light_model
        if DEEP_ANALYSIS_KEYWORDS.search(prompt):
            return self.deep_model
        return self.default_model

    def set_status(self, status: str, query: str = ""):
        """Aggiorna lo stato visivo di Whis e notifica tutti i listener Qt."""
        self.current_status = status
        if query:
            self.last_query = query
        self.status_changed.emit(status, self.last_query)

    def detect_and_attach_screen(self, prompt: str) -> Tuple[str, bool, str]:
        """
        Superpotere Vista Schermo (Vision Nativa Qwen 3.5 + RapidOCR):
        Se la richiesta cita schermo, monitor, slide, pagina, finestra, vedi, ecc.,
        cattura lo screenshot per la Vision nativa di Qwen 3.5 + testo RapidOCR e titolo finestra attiva.
        """
        self._last_screen_b64 = ""
        if SCREEN_TRIGGER_KEYWORDS.search(prompt):
            self.set_status("Cattura schermo (Vision + OCR)...", prompt)
            window_title = get_active_window_title()
            self._last_screen_b64 = capture_screen_image_base64()
            screen_txt = capture_screen_text()
            clean_ocr = (screen_txt or "").strip()[:6000]
            if clean_ocr:
                enhanced = (
                    f"[FINESTRA ATTIVA IN PRIMO PIANO]:\n{window_title}\n\n"
                    f"[TESTO SULLO SCHERMO (Screen OCR)]:\n"
                    f"```\n{clean_ocr}\n```\n\n"
                    f"[Richiesta dell'utente]:\n{prompt}"
                )
            else:
                enhanced = (
                    f"[FINESTRA ATTIVA IN PRIMO PIANO]:\n{window_title}\n\n"
                    f"[TESTO SULLO SCHERMO (Screen OCR)]:\n"
                    f"*(Nessun testo OCR estratto - Analizza direttamente l'immagine allegata dello schermo - Titolo finestra: {window_title})*\n\n"
                    f"[Richiesta dell'utente]:\n{prompt}"
                )
            return enhanced, True, clean_ocr
        return prompt, False, ""

    def is_calendar_command(self, prompt: str) -> bool:
        """Restituisce True se la richiesta dell'utente è un comando per Google Calendar."""
        norm_prompt = normalize_calendar_prompt(prompt)
        return bool(
            CALENDAR_CREATE_KEYWORDS.search(norm_prompt)
            or CALENDAR_DELETE_KEYWORDS.search(norm_prompt)
            or CALENDAR_BRIEFING_KEYWORDS.search(norm_prompt)
            or CALENDAR_READ_KEYWORDS.search(norm_prompt)
        )

    def is_note_command(self, prompt: str) -> bool:
        """Restituisce True se la richiesta dell'utente è un comando di creazione o gestione appunti."""
        # Non confondere mai comandi del calendario con gli appunti di studio
        if self.is_calendar_command(prompt):
            return False
        return bool(
            NOTE_CREATE_KEYWORDS.search(prompt)
            or NOTE_MERGE_KEYWORDS.search(prompt)
            or NOTE_DELETE_KEYWORDS.search(prompt)
            or NOTE_APPEND_KEYWORDS.search(prompt)
        )

    def detect_and_attach_rag(self, prompt: str) -> Tuple[str, Optional[str], Optional[dict]]:
        """
        Superpotere RAG sugli Appunti: Cerca nella knowledge base locale di studio note/slide pertinenti.
        """
        # NON chiamare il RAG e NON iniettare vecchie note quando l'intento dell'utente è creare/gestire una nota o il calendario!
        if self.is_note_command(prompt) or self.is_calendar_command(prompt):
            return prompt, None, None

        try:
            from database.db_manager import db
            # Prima tenta con filtro su materia corrente se non generale
            notes = []
            if self.current_subject and self.current_subject != "Generale":
                notes = db.search_knowledge_base(
                    query=prompt,
                    subject=self.current_subject,
                    topic=self.current_topic if self.current_topic != "Generale" else None,
                    limit=2
                )
            # Se nessun risultato, cerca globalmente
            if not notes:
                notes = db.search_knowledge_base(query=prompt, limit=2)

            if notes:
                top_note = notes[0]
                source_title = top_note.get("title") or "Appunto di studio"
                content_snip = (top_note.get("content_markdown") or "")[:2200].strip()
                enhanced = (
                    f"[CONTESTO DAGLI APPUNTI DI STUDIO (Fonte: {source_title})]:\n"
                    f"```markdown\n{content_snip}\n```\n\n"
                    f"[Domanda dell'utente]:\n{prompt}"
                )
                return enhanced, source_title, top_note
        except Exception as e:
            print(f"[WhisAgent RAG Search Warning] {e}")
        return prompt, None, None

    def execute_note_action(self, prompt: str, clip_context: str = "") -> Optional[str]:
        """
        Superpotere Azioni DB: Esegue comandi diretti di gestione appunti sul database SQLite:
        - Creazione: 'segna negli appunti...', 'crea una nota...', 'appunta...'
        - Unione: 'unisci la nota X a quella Y'
        - Eliminazione: 'elimina la nota X'
        - Modifica/Accodamento: 'aggiungi alla nota X il testo Y'
        """
        try:
            from database.db_manager import db

            # 1. Unione di due note
            match_merge = NOTE_MERGE_KEYWORDS.search(prompt)
            if match_merge:
                kw1 = match_merge.group(1).strip()
                kw2 = match_merge.group(2).strip()
                clean_kw1 = re.sub(r'^(?:di|su|chiamata|intitolata)\s+', '', kw1, flags=re.IGNORECASE).strip('"\',. ')
                clean_kw2 = re.sub(r'^(?:di|su|chiamata|intitolata)\s+', '', kw2, flags=re.IGNORECASE).strip('"\',. ')

                res1 = db.find_notes_by_keyword(clean_kw1) if clean_kw1 else []
                res2 = db.find_notes_by_keyword(clean_kw2) if clean_kw2 else []

                if res1 and res2:
                    n1 = res1[0]
                    n2 = res2[0]
                    if n1["id"] == n2["id"]:
                        if len(res2) > 1:
                            n2 = res2[1]
                        elif len(res1) > 1:
                            n1 = res1[1]

                    if n1["id"] != n2["id"]:
                        merged = db.merge_notes(n1["id"], n2["id"])
                        self.note_created.emit(n1["id"], n1["title"])
                        self.set_status("Note unite nel Quaderno", prompt)
                        return (
                            f"✅ Ho unito le due note nel Quaderno:\n\n"
                            f"**Nota finale:** `{n1['title']}`\n\n"
                            f"> {n1['title']} + {n2['title']}\n\n"
                            f"*(Aggiornato Notekeeper #{n1['id']})*"
                        )

                return f"Non sono riuscito a trovare due note distinte corrispondenti a '{clean_kw1}' e '{clean_kw2}' per l'unione."

            # 2. Eliminazione nota
            match_del = NOTE_DELETE_KEYWORDS.search(prompt)
            if match_del:
                kw = match_del.group(1).strip()
                clean_kw = re.sub(r'^(?:di|su|chiamata|intitolata)\s+', '', kw, flags=re.IGNORECASE).strip('"\',. ')
                res = db.find_notes_by_keyword(clean_kw) if clean_kw else []
                if res:
                    target = res[0]
                    db.delete_study_note(target["id"])
                    self.note_created.emit(target["id"], "deleted")
                    self.set_status("Nota eliminata", prompt)
                    return (
                        f"🗑️ Ho eliminato la nota dal Quaderno:\n\n"
                        f"**Titolo:** `{target['title']}`\n\n"
                        f"*(ID rimosso: #{target['id']})*"
                    )
                return f"Non ho trovato alcuna nota corrispondente a '{clean_kw}' da eliminare."

            # 3. Aggiunta/Modifica nota
            match_append = NOTE_APPEND_KEYWORDS.search(prompt)
            if match_append:
                kw_note = match_append.group(1).strip()
                extra_text = match_append.group(2).strip()
                clean_kw = re.sub(r'^(?:di|su|chiamata|intitolata)\s+', '', kw_note, flags=re.IGNORECASE).strip('"\',. ')
                res = db.find_notes_by_keyword(clean_kw) if clean_kw else []
                if res:
                    target = res[0]
                    new_content = f"{target['content_markdown'].strip()}\n\n{extra_text}"
                    db.update_study_note(
                        note_id=target["id"],
                        title=target["title"],
                        content_markdown=new_content,
                        profile=target.get("profile", "Alessandro"),
                        subject=target.get("subject", "Generale"),
                        topic=target.get("topic", "Generale"),
                        note_type=target.get("note_type", "appunto"),
                    )
                    self.note_created.emit(target["id"], target["title"])
                    self.set_status("Nota aggiornata", prompt)
                    return (
                        f"📝 Ho aggiornato la nota `{target['title']}` aggiungendo:\n\n"
                        f"> {extra_text}\n\n"
                        f"*(Aggiornato Notekeeper #{target['id']})*"
                    )
                return f"Non ho trovato la nota '{clean_kw}' a cui aggiungere il testo."

            # 4. Creazione nuova nota / appunto
            match_create = NOTE_CREATE_KEYWORDS.search(prompt)
            if not match_create:
                return None

            raw_body = (match_create.group(1) or "").strip()
            if not raw_body and clip_context:
                raw_body = clip_context.strip()
            elif not raw_body:
                raw_body = prompt.strip()

            body = extract_clean_note_content(raw_body)
            first_line = body.splitlines()[0][:45].strip()
            title = f"Nota: {first_line}" if first_line else "Nota da Whis"
            note_id = db.add_study_note(
                title=title,
                content_markdown=body,
                note_type="appunto",
                tags="whis,voce,appunto",
                profile=self.current_profile,
                subject=self.current_subject,
                topic=self.current_topic
            )
            self.note_created.emit(note_id, title)
            self.set_status("Appunto salvato", prompt)
            return (
                f"✅ Appunto salvato in [{self.current_subject} -> {self.current_topic}]:\n"
                f"«{body}»"
            )
        except Exception as e:
            return f"Si è verificato un errore durante la gestione della nota: {e}"

    def _extract_calendar_event(self, prompt: str, date_table: str) -> dict:
        """Estrae i parametri dell'evento via Qwen 2.5 (3B) con schema JSON rigoroso e fallback deterministico."""
        system_content = (
            "Sei un assistente per la gestione di Google Calendar. "
            "Il tuo compito è estrarre le informazioni sull'evento dal testo dell'utente e restituire ESCLUSIVAMENTE un oggetto JSON valido.\n\n"
            f"TABELLA DATE E ORE ATTUALI:\n{date_table}\n\n"
            "SCHEMA JSON:\n"
            "{\n"
            '  "title": "<Titolo o argomento dell\'appuntamento se specificato dall\'utente, altrimenti stringa vuota \"\">",\n'
            '  "date": "YYYY-MM-DD",\n'
            '  "start_time": "HH:MM",\n'
            '  "duration_minutes": 60\n'
            "}\n\n"
            "REGOLE RIGOROSE:\n"
            "1. Usa la TABELLA DATE E ORE per calcolare 'date'. 'domani' corrisponde esattamente alla data di Domani indicata.\n"
            "2. Se l'utente usa forme come 'giorno 15 alle 10', intende il giorno 15 del mese corrente (o del mese successivo se il 15 di questo mese è già trascorso).\n"
            "3. Se l'orario non è specificato, usa '09:00'. Se è specificato 'alle 13', usa '13:00'. Se è per tutto il giorno, usa 'all_day'.\n"
            "4. Se la durata non è specificata, usa 60 minuti.\n"
            "5. TITOLO: Se l'utente ha detto solo la data e l'ora SENZA specificare il motivo o argomento (es. 'segna un appuntamento domani alle 13', 'metti un evento per venerdì', 'fissa un impegno'), imposta TASSATIVAMENTE 'title': \"\". "
            "NON inventare MAI argomenti o materie non dette dall'utente. Se invece l'utente specifica il motivo (es. 'appuntamento dal dentista', 'riunione team'), inserisci solo quello nel 'title' (es. 'Dentista', 'Riunione team').\n"
            "6. Rispondi SOLO con il JSON senza apici markdown, senza spiegazioni."
        )
        try:
            payload = {
                "model": self.default_model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "format": "json",
                "options": {"temperature": 0.0}
            }
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = strip_think_blocks(data.get("message", {}).get("content", ""))
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "date" in parsed and "title" in parsed:
                    return parsed
        except Exception as e:
            print(f"[WhisAgent Calendar Extraction Warning] {e}")

        # Fallback euristico deterministico se Ollama non risponde
        now = datetime.datetime.now()
        target_date = now.strftime("%Y-%m-%d")
        prompt_lower = prompt.lower()
        if "domani" in prompt_lower:
            target_date = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        elif "dopodomani" in prompt_lower:
            target_date = (now + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        elif match_giorno := re.search(r'\bgiorno\s+(\d{1,2})\b', prompt_lower):
            day_num = int(match_giorno.group(1))
            try:
                cand = datetime.date(now.year, now.month, day_num)
                if cand < now.date():
                    next_month = now.month + 1 if now.month < 12 else 1
                    next_year = now.year if now.month < 12 else now.year + 1
                    cand = datetime.date(next_year, next_month, day_num)
                target_date = cand.strftime("%Y-%m-%d")
            except Exception:
                pass

        start_time = "09:00"
        if match_time := re.search(r'\balle\s+(\d{1,2})(?::(\d{2}))?\b', prompt_lower):
            h = int(match_time.group(1))
            m = int(match_time.group(2)) if match_time.group(2) else 0
            start_time = f"{h:02d}:{m:02d}"

        clean_title = CALENDAR_CREATE_KEYWORDS.sub('', prompt).strip()
        clean_title = re.sub(r'\b(domani|oggi|dopodomani|alle\s+\d{1,2}(?::\d{2})?|giorno\s+\d{1,2}|luned[ìi]|marted[ìi]|mercoled[ìi]|gioved[ìi]|venerd[ìi]|sabato|domenica)\b', '', clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r'^(?:per|di|a|il|la|lo|un|uno|una)\s+', '', clean_title, flags=re.IGNORECASE).strip(' :,-.!?')
        title = clean_title.capitalize() if clean_title else ""

        return {
            "title": title,
            "date": target_date,
            "start_time": start_time,
            "duration_minutes": 60
        }

    def execute_calendar_action(self, prompt: str) -> Tuple[str, dict]:
        """
        Superpotere Google Calendar: Crea o legge appuntamenti su Google Calendar.
        Restituisce una tupla (testo_risposta, metadata_dict).
        """
        meta = {"calendar": True}
        try:
            from core.calendar_sync import calendar_manager

            # Pre-normalizzazione fonetica e acustica per comandi calendario
            norm_prompt = normalize_calendar_prompt(prompt)

            # 1. INTENTO CALENDAR_CREATE
            if CALENDAR_CREATE_KEYWORDS.search(norm_prompt):
                date_table = generate_date_context_table()
                event_data = self._extract_calendar_event(norm_prompt, date_table)

                title = (event_data.get("title") or "").strip()
                # Se il titolo restituito non è presente nel prompt dell'utente (allucinazione esempio prompt), lo azzeriamo
                if title:
                    title_tokens = [t.lower() for t in re.findall(r'\b[a-zA-Z0-9àèéìòù]+\b', title)]
                    prompt_tokens = set(re.findall(r'\b[a-zA-Z0-9àèéìòù]+\b', norm_prompt.lower()))
                    if title_tokens and not any(t in prompt_tokens for t in title_tokens):
                        title = ""

                if title.lower() in ("appuntamento", "evento", "impegno", "nuovo appuntamento", "promemoria", ""):
                    title = ""

                date_str = (event_data.get("date") or datetime.date.today().isoformat()).strip()
                start_time_str = event_data.get("start_time", "09:00")
                duration = int(event_data.get("duration_minutes", 60) or 60)

                # Formattazione leggibile di data e orario per l'utente
                days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
                try:
                    parts = [int(p) for p in date_str.split("-")]
                    d_obj = datetime.date(parts[0], parts[1], parts[2])
                    dow = days_it[d_obj.weekday()]
                    date_display = f"{dow} {d_obj.day:02d}/{d_obj.month:02d}"
                except Exception:
                    date_display = date_str

                if not start_time_str or start_time_str.lower() in ("all_day", "allday"):
                    time_display = "per tutto il giorno"
                else:
                    time_display = f"alle {start_time_str}"

                target_phrase = f"{date_display} {time_display}"

                # MODALITÀ ASSISTENTE INTERATTIVO: Se il titolo non è stato pronunciato, chiedi e mantieni in sospeso
                if not title:
                    self.pending_calendar_create = {
                        "date": date_str,
                        "start_time": start_time_str,
                        "duration_minutes": duration,
                        "date_display": target_phrase
                    }
                    resp = f"📅 Ho impostato l'appuntamento per {target_phrase}. Di cosa si tratta? (Dimmi il titolo o l'attività)"
                    self.set_status("In attesa del titolo...", prompt)
                    meta["waiting_for_title"] = True
                    return resp, meta

                try:
                    created = calendar_manager.create_event(
                        summary=title,
                        date_str=date_str,
                        start_time_str=start_time_str,
                        duration_minutes=duration
                    )
                except Exception as e:
                    return f"⚠️ Errore durante la creazione dell'evento in FlowTime:\n{e}", meta

                sync_note = " (sincronizzazione Google attiva)" if calendar_manager.has_token_file() else ""
                resp = (
                    f"📅 Appuntamento salvato in FlowTime{sync_note}:\n"
                    f"• Titolo: **{title}**\n"
                    f"• Data e ora: {target_phrase}"
                )
                self.set_status("Evento salvato in FlowTime", prompt)
                return resp, meta

            # 2. INTENTO CALENDAR_DELETE: Cancellazione appuntamenti a voce
            if CALENDAR_DELETE_KEYWORDS.search(prompt):
                now = datetime.datetime.now()
                prompt_lower = prompt.lower()
                target_date = None
                date_label = "in agenda"

                weekday_map = {
                    "lunedì": 0, "lunedi": 0,
                    "martedì": 1, "martedi": 1,
                    "mercoledì": 2, "mercoledi": 2,
                    "giovedì": 3, "giovedi": 3,
                    "venerdì": 4, "venerdi": 4,
                    "sabato": 5,
                    "domenica": 6,
                }
                found_weekday = None
                for wname, wday_idx in weekday_map.items():
                    if re.search(rf'\b{wname}\b', prompt_lower):
                        found_weekday = (wname, wday_idx)
                        break

                if re.search(r'\bdopodomani\b', prompt_lower):
                    target_date = (now.date() + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
                    date_label = "per dopodomani"
                elif re.search(r'\bdomani\b', prompt_lower):
                    target_date = (now.date() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    date_label = "per domani"
                elif re.search(r'\boggi\b', prompt_lower):
                    target_date = now.strftime("%Y-%m-%d")
                    date_label = "per oggi"
                elif found_weekday:
                    wname, wday_idx = found_weekday
                    diff = (wday_idx - now.weekday()) % 7
                    if diff == 0:
                        diff = 7
                    target_date = (now.date() + datetime.timedelta(days=diff)).strftime("%Y-%m-%d")
                    date_label = f"per {wname.capitalize()}"
                elif match_giorno := re.search(r'\bgiorno\s+(\d{1,2})\b', prompt_lower):
                    day_num = int(match_giorno.group(1))
                    try:
                        cand = datetime.date(now.year, now.month, day_num)
                        if cand < now.date():
                            next_month = now.month + 1 if now.month < 12 else 1
                            next_year = now.year if now.month < 12 else now.year + 1
                            cand = datetime.date(next_year, next_month, day_num)
                        target_date = cand.strftime("%Y-%m-%d")
                        date_label = f"per il giorno {day_num}"
                    except Exception:
                        pass

                # Estrai parola chiave di ricerca ripulendo verbi, date e preposizioni accessorie
                clean_target = CALENDAR_DELETE_KEYWORDS.sub('', prompt).strip()
                clean_target = re.sub(
                    r'\b(per\s+domani|per\s+oggi|di\s+domani|di\s+oggi|per\s+dopodomani|di\s+dopodomani|'
                    r'domani|oggi|dopodomani|'
                    r'luned[ìi]|marted[ìi]|mercoled[ìi]|gioved[ìi]|venerd[ìi]|sabato|domenica|'
                    r'giorno\s+\d{1,2}|alle\s+\d{1,2}(?::\d{2})?)\b',
                    '', clean_target, flags=re.IGNORECASE
                ).strip(' :,-.!?')
                clean_target = re.sub(
                    r'\b(?:puoi|potresti|per\s+favore|gentilmente|subito|di|del|della|dello|dei|degli|delle|per|con|da|dal|dallo|dalla|dai|dagli|dalle|alle|a)\b',
                    '', clean_target, flags=re.IGNORECASE
                ).strip(' :,-.!?')
                clean_target = re.sub(r'^(?:l[\'’]|il|la|lo|i|gli|le|un|uno|una)\s*', '', clean_target, flags=re.IGNORECASE).strip(' :,-.!?')

                try:
                    matched = calendar_manager.find_events_by_query(query=clean_target, target_date=target_date, days=14)
                except Exception as e:
                    return f"⚠️ Errore durante la ricerca dell'evento da cancellare:\n{e}", meta

                if matched:
                    if len(matched) > 1 and not clean_target:
                        lines = [f"📅 Per {date_label} ho trovato {len(matched)} appuntamenti:"]
                        for ev in matched:
                            lines.append(f"• {ev['summary']} ({ev['time']})")
                        lines.append("Per evitare errori, specifica quale desideri cancellare (es. \"cancella l'appuntamento con...\").")
                        return "\n".join(lines), meta

                    target_ev = matched[0]
                    try:
                        calendar_manager.delete_event(target_ev["id"], calendar_id=target_ev.get("calendar_id", "primary"))
                        resp = (
                            f"🗑️ Ho cancellato l'appuntamento da FlowTime:\n"
                            f"• {target_ev['summary']} ({target_ev['date']}, {target_ev['time']})"
                        )
                        self.set_status("Evento eliminato da FlowTime", prompt)
                        return resp, meta
                    except Exception as e:
                        return f"⚠️ Errore durante l'eliminazione dell'evento: {e}", meta
                else:
                    if clean_target:
                        return f"📅 Non ho trovato nessun appuntamento corrispondente a '{clean_target}' {date_label} da cancellare.", meta
                    else:
                        return f"📅 Non risulta alcun appuntamento in programma {date_label} da cancellare.", meta

            # 3. INTENTO CALENDAR_READ / CALENDAR_BRIEFING: Risposta naturale contestuale con Qwen (lettura SQLite <1ms)
            if CALENDAR_READ_KEYWORDS.search(norm_prompt) or CALENDAR_BRIEFING_KEYWORDS.search(norm_prompt):
                now = datetime.datetime.now()
                days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
                today_display = f"{days_it[now.weekday()]} {now.strftime('%d/%m/%Y')}"
                prompt_lower = prompt.lower()
                target_date = None
                target_date_obj = None
                days = 1
                period_label = "di oggi"

                # Attenzione: verificare 'dopodomani' prima di 'domani', e giorni della settimana
                weekday_map = {
                    "lunedì": 0, "lunedi": 0,
                    "martedì": 1, "martedi": 1,
                    "mercoledì": 2, "mercoledi": 2,
                    "giovedì": 3, "giovedi": 3,
                    "venerdì": 4, "venerdi": 4,
                    "sabato": 5,
                    "domenica": 6,
                }
                found_weekday = None
                for wname, wday_idx in weekday_map.items():
                    if re.search(rf'\b{wname}\b', prompt_lower):
                        found_weekday = (wname, wday_idx)
                        break

                if re.search(r'\bdopodomani\b', prompt_lower):
                    target_date_obj = now.date() + datetime.timedelta(days=2)
                    target_date = target_date_obj.strftime("%Y-%m-%d")
                    period_label = "dopodomani"
                elif re.search(r'\bdomani\b', prompt_lower):
                    target_date_obj = now.date() + datetime.timedelta(days=1)
                    target_date = target_date_obj.strftime("%Y-%m-%d")
                    period_label = "domani"
                elif found_weekday and not re.search(r'\boggi\b', prompt_lower):
                    wname, wday_idx = found_weekday
                    if match_num := re.search(rf'\b{wname}\s+(\d{{1,2}})\b', prompt_lower):
                        day_num = int(match_num.group(1))
                        try:
                            cand = datetime.date(now.year, now.month, day_num)
                            if cand < now.date():
                                cand = datetime.date(now.year if now.month < 12 else now.year + 1, now.month + 1 if now.month < 12 else 1, day_num)
                            target_date_obj = cand
                        except Exception:
                            target_date_obj = now.date()
                    else:
                        diff = (wday_idx - now.weekday()) % 7
                        if diff == 0:
                            diff = 7
                        target_date_obj = now.date() + datetime.timedelta(days=diff)
                    target_date = target_date_obj.strftime("%Y-%m-%d")
                    period_label = f"{wname.capitalize()} {target_date_obj.day:02d}"
                elif re.search(r'\bsettimana\b', prompt_lower):
                    days = 7
                    target_date = None
                    target_date_obj = None
                    period_label = "questa settimana"
                elif re.search(r'\bmese\b', prompt_lower):
                    days = 30
                    target_date = None
                    target_date_obj = None
                    period_label = "il prossimo mese"
                elif match_giorno := re.search(r'\b(?:giorno\s+|il\s+|del\s+)?(\d{1,2})\b', prompt_lower):
                    day_num = int(match_giorno.group(1))
                    try:
                        cand = datetime.date(now.year, now.month, day_num)
                        if cand < now.date():
                            next_month = now.month + 1 if now.month < 12 else 1
                            next_year = now.year if now.month < 12 else now.year + 1
                            cand = datetime.date(next_year, next_month, day_num)
                        target_date_obj = cand
                        target_date = cand.strftime("%Y-%m-%d")
                        period_label = f"il giorno {day_num}"
                    except Exception:
                        target_date_obj = now.date()
                        target_date = target_date_obj.strftime("%Y-%m-%d")
                        period_label = "oggi"
                else:
                    # Default: oggi / briefing / pomeriggio / stasera
                    target_date_obj = now.date()
                    target_date = target_date_obj.strftime("%Y-%m-%d")
                    period_label = "oggi"

                try:
                    events = calendar_manager.get_upcoming_events(days=days, target_date=target_date)
                except Exception as e:
                    return f"⚠️ Errore durante la lettura degli impegni da FlowTime:\n{e}", meta

                temporal_notes: List[Dict[str, Any]] = []
                if target_date:
                    try:
                        from database.db_manager import db
                        temporal_notes = db.get_notes_for_date(target_date)
                    except Exception:
                        temporal_notes = []

                if target_date_obj:
                    target_dow = days_it[target_date_obj.weekday()]
                    target_display = f"{target_dow} {target_date_obj.strftime('%d/%m/%Y')}"
                else:
                    target_display = f"i prossimi {days} giorni"

                tmrw_obj = now.date() + datetime.timedelta(days=1)
                tmrw_display = f"{days_it[tmrw_obj.weekday()]} {tmrw_obj.strftime('%d/%m/%Y')}"

                notes_ctx_block = ""
                if temporal_notes:
                    note_lines = [
                        f"- {n['title']} ({n.get('subject') or 'Generale'}"
                        + (f", collegata a: {n['linked_event_title']}" if n.get("linked_event_title") else "")
                        + ")"
                        for n in temporal_notes[:6]
                    ]
                    notes_ctx_block = (
                        "\nNOTE DI STUDIO COLLEGATE / CREATE IN QUESTA DATA:\n"
                        + "\n".join(note_lines)
                        + "\n(Citali brevemente solo se rilevanti per lo studio o la giornata.)\n"
                    )

                # Sintesi conversazionale naturale potenziata con Qwen 2.5
                if events:
                    events_text = "\n".join([ev["formatted"] for ev in events])
                    system_briefing = (
                        "Sei Whis, l'assistente virtuale personale di FlowLocal.\n"
                        f"OGGI è: {today_display} ore {now.strftime('%H:%M')}.\n"
                        f"DOMANI è: {tmrw_display}.\n\n"
                        f"IMPEGNI IN AGENDA TROVATI per il periodo richiesto ({period_label} - {target_display}):\n"
                        f"{events_text}\n"
                        f"{notes_ctx_block}\n"
                        "REGOLE TASSATIVE:\n"
                        "1. L'utente HA EFFETTIVAMENTE gli impegni sopra indicati: NON dire MAI che è libero o che non ha impegni!\n"
                        "2. Rispondi in perfetto italiano, chiaro, naturale ed elegante in 1-2 frasi elencando gli impegni con relativi orari e luoghi/aule.\n"
                        "   Esempio: 'Lunedì 16 hai Fondamenti di Elettronica dalle 12:00 alle 14:00 in Aula F130 Ed.8 e a seguire Sistemi Robotici dalle 15:00 alle 18:00 in Aula V010 Ed.10.'\n"
                        "3. Rivolgiti sempre all'utente in seconda persona ('hai...', 'sei impegnato dalle...'). Non inventare parole o lingue straniere."
                    )
                else:
                    events_text = "Nessun evento in programma."
                    system_briefing = (
                        "Sei Whis, l'assistente virtuale personale di FlowLocal.\n"
                        f"OGGI è: {today_display} ore {now.strftime('%H:%M')}.\n"
                        f"DOMANI è: {tmrw_display}.\n\n"
                        f"Non risultano impegni segnati in FlowTime per {period_label} ({target_display}).\n"
                        f"{notes_ctx_block}\n"
                        "Conferma con gentilezza e brevità in perfetto italiano che per quel periodo non ci sono impegni in programma ed è completamente libero."
                    )

                try:
                    payload = {
                        "model": self.default_model,
                        "messages": [
                            {"role": "system", "content": system_briefing},
                            {"role": "user", "content": prompt}
                        ],
                        "stream": False,
                        "think": False,
                        "keep_alive": "30m",
                        "options": {"temperature": 0.0}
                    }
                    req = urllib.request.Request(
                        OLLAMA_API_URL,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=10.0) as resp_qwen:
                        data = json.loads(resp_qwen.read().decode("utf-8"))
                        answer = strip_think_blocks(data.get("message", {}).get("content", ""))

                        # Guardrail anti-allucinazione:
                        # Se abbiamo trovato eventi reali ma Qwen dice che non ce ne sono,
                        # scarta l'allucinazione e formatta la lista deterministica.
                        negative_markers = [
                            "non ho trovato", "non hai impegni", "non ci sono impegni", "nessun impegno",
                            "nessun appuntamento", "sei libero", "completamente libero", "tutto il giorno libero",
                            "non risultano", "non trovo", "non hai nulla", "nessun evento"
                        ]
                        has_negative_hallucination = events and any(m in answer.lower() for m in negative_markers)
                        if has_negative_hallucination:
                            lines = [f"📅 Per {period_label} ({target_display}) hai {len(events)} {'impegno' if len(events)==1 else 'impegni'} in agenda:"]
                            for ev in events:
                                lines.append(f"• {ev['time']}: {ev['summary']}")
                            answer = "\n".join(lines)

                        if answer:
                            self.set_status("Risposta agenda inviata", prompt)
                            return answer, meta
                except Exception as e_qwen:
                    print(f"[WhisAgent Calendar Qwen Synthesis Warning] {e_qwen}")

                # Fallback deterministico a lista puntata se Qwen non è raggiungibile
                if not events:
                    resp = f"📅 Non hai impegni in programma per {period_label} ({target_display}). Sei completamente libero!"
                else:
                    lines = [f"📅 Appuntamenti in FlowTime ({period_label}):"]
                    for ev in events:
                        lines.append(ev["formatted"])
                    resp = "\n".join(lines)

                if temporal_notes:
                    resp += "\n\n📓 Note collegate/del giorno:\n" + "\n".join(
                        f"• {n['title']} ({n.get('subject') or 'Generale'})" for n in temporal_notes[:4]
                    )

                self.set_status("Appuntamenti letti da FlowTime", prompt)
                return resp, meta

        except Exception as e:
            return f"⚠️ Errore imprevisto nella gestione di FlowTime: {e}", meta

        return "Nessuna azione calendario eseguita.", meta

    def detect_and_attach_clipboard(self, prompt: str, explicit_selection: Optional[str] = None) -> Tuple[str, bool, str]:
        """
        Allega la Clipboard (📋) SOLO ED ESCLUSIVAMENTE quando l'utente si riferisce ad essa
        usando espressioni dedicate ('questo codice', 'questo testo', 'quello che ho copiato', 'nella clipboard', ecc.).
        Per tutte le altre domande (note del DB, schermo OCR, query generali), la clipboard viene IGNORATA.
        """
        if not CLIPBOARD_TRIGGER_KEYWORDS.search(prompt):
            return prompt, False, ""

        context_text = ""
        if explicit_selection and explicit_selection.strip():
            context_text = explicit_selection.strip()[:8000]
        else:
            try:
                clip = pyperclip.paste()
                if clip and clip.strip():
                    context_text = clip.strip()[:8000]
            except Exception:
                pass

        if context_text:
            enhanced_prompt = (
                f"[Contesto dagli appunti / testo selezionato]:\n"
                f"```\n{context_text}\n```\n\n"
                f"[Richiesta vocale dell'utente]:\n{prompt}"
            )
            return enhanced_prompt, True, context_text

        return prompt, False, ""

    def clear_history(self):
        """Reimposta la memoria della sessione."""
        self.history.clear()

    def ask_stream(
        self,
        user_prompt: str,
        on_chunk: Callable[[str], None],
        on_done: Optional[Callable[..., None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        explicit_selection: Optional[str] = None,
        on_metadata: Optional[Callable[[dict], None]] = None
    ) -> str:
        """
        Invia la richiesta in streaming a Ollama gestendo superpoteri (Screen OCR, Azioni DB, RAG),
        il fallback del modello, lo stato in tempo reale e il salvataggio automatico.
        """
        self.set_status("Analisi richiesta...", user_prompt)
        metadata = {
            "screen_ocr": False,
            "clipboard": False,
            "rag_source": None,
            "note_action": False,
            "calendar": False
        }

        # 0. Intercettazione Follow-up interattivo FlowTime (titolo appuntamento in attesa)
        if self.pending_calendar_create:
            if re.search(r'\b(annulla|cancella|lascia\s+stare|niente|lascia\s+perdere|stop|non\s+importa)\b', user_prompt, re.IGNORECASE):
                self.pending_calendar_create = None
                resp = "Operazione annullata. Non ho salvato l'appuntamento."
                metadata["calendar"] = True
                if on_metadata:
                    on_metadata(metadata)
                on_chunk(resp)
                if on_done:
                    on_done(resp, "FlowTime", metadata)
                return resp

            pending = self.pending_calendar_create
            self.pending_calendar_create = None

            clean_title = user_prompt.strip(' .!?:,-"\'')
            clean_title = re.sub(r'^(?:chiamalo|intitolalo|scrivi|metti|[eè]|si\s+tratta\s+di|riguarda|fai|è\s+per|si\s+chiama)\s+', '', clean_title, flags=re.IGNORECASE).strip()
            final_title = clean_title.capitalize() if clean_title else "Nuovo Appuntamento"

            try:
                from core.calendar_sync import calendar_manager
                calendar_manager.create_event(
                    summary=final_title,
                    date_str=pending["date"],
                    start_time_str=pending["start_time"],
                    duration_minutes=pending["duration_minutes"]
                )
                date_display = pending.get("date_display", pending["date"])
                sync_note = " (sincronizzazione Google attiva)" if calendar_manager.has_token_file() else ""
                resp = (
                    f"✨ Perfetto! Ho salvato l'appuntamento in FlowTime{sync_note}:\n"
                    f"• Titolo: **{final_title}**\n"
                    f"• Data e ora: {date_display}"
                )
                self.set_status("Evento salvato in FlowTime", user_prompt)
            except Exception as e:
                resp = f"⚠️ Errore durante la creazione dell'evento in FlowTime:\n{e}"

            metadata["calendar"] = True
            if on_metadata:
                on_metadata(metadata)
            on_chunk(resp)
            if on_done:
                on_done(resp, "FlowTime", metadata)
            return resp

        # 1. Deissi / Lettura Clipboard preliminare
        prompt_with_context, clip_attached, clip_context = self.detect_and_attach_clipboard(user_prompt, explicit_selection)
        if clip_attached:
            metadata["clipboard"] = True
            self.set_status("Lettura Clipboard...", user_prompt)

        # 1b. SUPERPOTERE FLOWTIME / CALENDARIO (Creazione e Lettura appuntamenti <1ms)
        if self.is_calendar_command(user_prompt):
            self.set_status("FlowTime...", user_prompt)
            cal_resp, cal_meta = self.execute_calendar_action(user_prompt)
            metadata.update(cal_meta)
            if on_metadata:
                on_metadata(metadata)
            on_chunk(cal_resp)
            if on_done:
                try:
                    on_done(cal_resp, "FlowTime", metadata)
                except TypeError:
                    on_done(cal_resp, "FlowTime")
            return cal_resp

        # 2. SUPERPOTERE AZIONI DB: 'segna negli appunti...' o 'crea una nota...'
        note_action_resp = self.execute_note_action(user_prompt, clip_context=clip_context)
        if note_action_resp:
            metadata["note_action"] = True
            if on_metadata:
                on_metadata(metadata)
            on_chunk(note_action_resp)
            if on_done:
                try:
                    on_done(note_action_resp, "Azioni DB", metadata)
                except TypeError:
                    on_done(note_action_resp, "Azioni DB")
            return note_action_resp

        # 3. SUPERPOTERE VISTA SCHERMO (Screen OCR): 'guarda lo schermo', 'leggi questa slide'
        prompt_with_screen, screen_attached, _ = self.detect_and_attach_screen(user_prompt)
        if screen_attached:
            metadata["screen_ocr"] = True
            prompt_with_context = prompt_with_screen

        # 4. SUPERPOTERE RAG SUGLI APPUNTI: cerca note/slide per domande di studio
        prompt_with_rag, rag_source, _ = self.detect_and_attach_rag(user_prompt)
        if rag_source:
            metadata["rag_source"] = rag_source
            prompt_with_context = prompt_with_rag

        # Notifica immediata metadata per l'HUD (badge schermo, fonte, appunti)
        if on_metadata:
            on_metadata(metadata)

        selected_model = self.select_model(user_prompt)

        system_content = WHIS_SYSTEM_PROMPT
        if metadata.get("screen_ocr"):
            system_content = (
                "ATTENZIONE: HAI ACCESSO VISIVO NATIVO (MULTIMODALE VISION + OCR LOCALE) ALLO SCHERMO DELL'UTENTE. "
                "È ASSOLUTAMENTE VIETATO dire 'Non posso vedere il tuo schermo' o 'Sono un modello testuale'. "
                "Qui sotto trovi l'immagine dello schermo, il titolo della finestra attiva e il testo estratto dall'OCR: "
                "usali direttamente per rispondere con precisione a cosa sta guardando l'utente!\n\n"
                + WHIS_SYSTEM_PROMPT
            )

        messages = [{"role": "system", "content": system_content}]
        # Inietta gli ultimi scambi della sessione (fino a 10 messaggi = 5 scambi)
        messages.extend(self.history[-10:])
        user_msg: Dict[str, Any] = {"role": "user", "content": prompt_with_context}
        screen_b64 = getattr(self, "_last_screen_b64", "") if screen_attached else ""
        if screen_b64:
            user_msg["images"] = [screen_b64]
            metadata["vision_image"] = True
        messages.append(user_msg)

        routing_cfg = get_ai_routing_config()
        provider = routing_cfg.get("provider", "local")
        if screen_b64:
            task_type = "whis_screen"
        elif DEEP_ANALYSIS_KEYWORDS.search(user_prompt or ""):
            task_type = "whis_deep"
        else:
            task_type = "whis_chat"
        use_gemini = should_use_gemini(task_type)

        full_response: List[str] = []
        actual_model = selected_model
        gemini_succeeded = False

        if use_gemini:
            gem_model = routing_cfg.get("gemini_model") or "gemini-2.5-flash"
            actual_model = gem_model
            self.set_status(f"Elaborazione con {gem_model}...", user_prompt)
            try:
                img_bytes = base64.b64decode(screen_b64) if screen_b64 else None
                gem_messages = list(self.history[-10:]) + [{"role": "user", "content": prompt_with_context}]
                for delta in gemini_stream_generate(
                    gem_messages,
                    system_instruction=system_content,
                    image_bytes=img_bytes,
                    image_mime="image/png",
                    temperature=0.45,
                    max_output_tokens=2048,
                    model=gem_model,
                    timeout=DEFAULT_GEMINI_TIMEOUT,
                ):
                    full_response.append(delta)
                    on_chunk(delta)
                if full_response:
                    gemini_succeeded = True
                    set_active_runtime_engine("Adattivo • Gemini Online" if provider == "adaptive" else "Cloud • Gemini Online")
            except Exception as gem_err:
                print(f"[WhisAgent] Gemini non raggiungibile ({gem_err}). Fallback su motore locale.")
                if not routing_cfg.get("hybrid_fallback", True):
                    err_text = f"Errore di comunicazione con Gemini ({gem_model}): {gem_err}"
                    self.set_status("Errore", user_prompt)
                    if on_error:
                        on_error(err_text)
                    return ""
                full_response.clear()
                actual_model = selected_model

        if not gemini_succeeded:
            set_active_runtime_engine("Adattivo • Qwen 3.5 Offline" if provider == "adaptive" else "Locale • Qwen 3.5")
            self.set_status(f"Elaborazione con {selected_model}...", user_prompt)
            ollama_url = routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL
            payload = {
                "model": selected_model,
                "messages": messages,
                "stream": True,
                "think": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": 0.45,
                    "top_p": 0.9,
                }
            }

            try:
                req = urllib.request.Request(
                    ollama_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    for line in resp:
                        if not line:
                            continue
                        try:
                            chunk_obj = json.loads(line.decode("utf-8"))
                            delta = chunk_obj.get("message", {}).get("content", "")
                            if delta:
                                full_response.append(delta)
                                on_chunk(delta)
                            if chunk_obj.get("done", False):
                                break
                        except Exception:
                            pass
            except Exception as e:
                # Se il modello profondo fallisce o non risponde, fallback immediato su qwen3.5:4b
                if selected_model != self.default_model and self.model_mode != "qwen3.5:9b":
                    try:
                        payload["model"] = self.default_model
                        actual_model = self.default_model
                        self.set_status(f"Fallback su {self.default_model}...", user_prompt)
                        req = urllib.request.Request(
                            ollama_url,
                            data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        with urllib.request.urlopen(req, timeout=20.0) as resp:
                            for line in resp:
                                if not line:
                                    continue
                                try:
                                    chunk_obj = json.loads(line.decode("utf-8"))
                                    delta = chunk_obj.get("message", {}).get("content", "")
                                    if delta:
                                        full_response.append(delta)
                                        on_chunk(delta)
                                    if chunk_obj.get("done", False):
                                        break
                                except Exception:
                                    pass
                    except Exception as fallback_err:
                        err_text = f"Errore di comunicazione con Ollama ({self.default_model}): {fallback_err}"
                        self.set_status("Errore", user_prompt)
                        if on_error:
                            on_error(err_text)
                        return ""
                else:
                    err_text = f"Errore di comunicazione con Ollama ({selected_model}): {e}"
                    self.set_status("Errore", user_prompt)
                    if on_error:
                        on_error(err_text)
                    return ""

        complete_text = "".join(full_response).strip()

        # Aggiorna la memoria della sessione
        self.history.append({"role": "user", "content": prompt_with_context})
        self.history.append({"role": "assistant", "content": complete_text})
        if len(self.history) > 10:
            self.history = self.history[-10:]

        self.set_status("Risposta completata", user_prompt)

        # Salvataggio automatico persistente su SQLite
        try:
            from database import db
            row_id = db.add_whis_interaction(
                user_query=user_prompt,
                whis_response=complete_text,
                model_used=actual_model,
                clipboard_context=clip_context or ""
            )
            saved_item = {
                "id": row_id,
                "user_query": user_prompt,
                "whis_response": complete_text,
                "model_used": actual_model,
                "clipboard_context": clip_context or "",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.interaction_saved.emit(saved_item)
        except Exception as db_err:
            print(f"[WhisAgent] Avviso salvataggio database: {db_err}")

        if on_done:
            try:
                on_done(complete_text, actual_model, metadata)
            except TypeError:
                on_done(complete_text, actual_model)

        return complete_text


class WhisStreamWorker(QThread):
    """Worker QThread per lo streaming asincrono non bloccante della risposta di Whis."""

    chunk_received = pyqtSignal(str)
    finished_response = pyqtSignal(str, str)  # (full_text, model_name)
    metadata_detected = pyqtSignal(dict)      # (metadata: {"screen_ocr": bool, "rag_source": str, "clipboard": bool})
    error_occurred = pyqtSignal(str)

    def __init__(self, agent: WhisAgent, user_prompt: str, explicit_selection: Optional[str] = None):
        super().__init__()
        self.agent = agent
        self.user_prompt = user_prompt
        self.explicit_selection = explicit_selection

    def run(self):
        try:
            self.agent.ask_stream(
                user_prompt=self.user_prompt,
                on_chunk=lambda chunk: self.chunk_received.emit(chunk),
                on_done=lambda full_text, model, *args: self.finished_response.emit(full_text, model),
                on_error=lambda err: self.error_occurred.emit(err),
                explicit_selection=self.explicit_selection,
                on_metadata=lambda meta: self.metadata_detected.emit(meta)
            )
        except Exception as e:
            self.error_occurred.emit(str(e))
