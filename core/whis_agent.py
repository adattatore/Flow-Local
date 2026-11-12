"""
Whis AI Agent — Assistente Virtuale Ingegneristico per FlowLocal
Caratteristiche:
- Modello rapido: qwen2.5:3b (default per latenza minima)
- Modello profondo: qwen2.5:7b (attivato per analisi profonda o codice complesso)
- Modalità configurabile: 'auto' (3B/7B dinamico), '3b' (solo 3B), '7b' (solo 7B)
- Personalità: calmo, brillante, arguto, sintetico, 2-4 frasi dritte al punto (salvo codice)
- Deissi / Clipboard context: lettura automatica appunti se il comando cita 'questo errore/codice/testo...'
- Memoria di sessione: ultimi 5 scambi per follow-up coerenti
- Persistenza automatica su SQLite (`whis_history`)
- Bus di stato reattivo con segnali PyQt6 per HUD e Hub Window
"""

import time
import re
import json
import ctypes
import urllib.request
import urllib.error
import pyperclip
from typing import Callable, Optional, List, Dict, Tuple, Any
from PyQt6.QtCore import QObject, QThread, pyqtSignal

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
    """Recupera il titolo della finestra attiva in primo piano su Windows."""
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
    "5. Rispondi sempre in italiano, a meno che l'utente non si rivolga a te in altra lingua."
)


class WhisAgent(QObject):
    """Modulo di intelligenza conversazionale per Whis in FlowLocal con segnali Qt."""

    status_changed = pyqtSignal(str, str)     # (status_text, current_query)
    interaction_saved = pyqtSignal(dict)      # (saved_interaction_dict)
    note_created = pyqtSignal(int, str)       # (note_id, title)

    def __init__(self, default_model: str = "qwen2.5:3b", deep_model: str = "qwen2.5:7b", parent=None):
        super().__init__(parent)
        self.default_model = default_model
        self.deep_model = deep_model
        self.model_mode = "auto"  # "auto", "3b", "7b"
        self.history: List[Dict[str, str]] = []
        self.current_status = "In attesa"
        self.last_query = ""
        self.current_profile = "Alessandro"
        self.current_subject = "Generale"
        self.current_topic = "Generale"

    def set_study_context(self, profile: str = "Alessandro", subject: str = "Generale", topic: str = "Generale"):
        """Aggiorna il contesto accademico corrente per Whis (Materia, Argomento, Profilo)."""
        if profile and profile.strip().lower() not in ("tutti i profili", "tutti", "all", ""):
            self.current_profile = profile.strip()
        self.current_subject = (subject or "Generale").strip()
        self.current_topic = (topic or "Generale").strip()

    def set_model_mode(self, mode: str):
        """Imposta la modalità di selezione del modello: 'auto', '3b', '7b'."""
        mode_clean = (mode or "auto").lower().strip()
        if mode_clean in ("3b", "rapido", "qwen2.5:3b"):
            self.model_mode = "3b"
        elif mode_clean in ("7b", "profondo", "qwen2.5:7b"):
            self.model_mode = "7b"
        else:
            self.model_mode = "auto"

    def get_model_mode(self) -> str:
        return self.model_mode

    def select_model(self, prompt: str) -> str:
        """Determina dinamicamente se impiegare il modello 3B rapido o 7B per compiti complessi."""
        if self.model_mode == "3b":
            return self.default_model
        if self.model_mode == "7b":
            return self.deep_model
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
        Superpotere Vista Schermo: Se la richiesta cita schermo, monitor, slide, pagina, finestra, vedi, ecc.,
        cattura il monitor con mss + RapidOCR e include il titolo della finestra attiva in primo piano.
        """
        if SCREEN_TRIGGER_KEYWORDS.search(prompt):
            self.set_status("Cattura schermo (OCR)...", prompt)
            window_title = get_active_window_title()
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
                    f"*(Nessun testo rilevato su schermo o applicazione grafica - Titolo finestra: {window_title})*\n\n"
                    f"[Richiesta dell'utente]:\n{prompt}"
                )
            return enhanced, True, clean_ocr
        return prompt, False, ""

    def is_note_command(self, prompt: str) -> bool:
        """Restituisce True se la richiesta dell'utente è un comando di creazione o gestione appunti."""
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
        # NON chiamare il RAG e NON iniettare vecchie note quando l'intento dell'utente è creare/gestire una nota!
        if self.is_note_command(prompt):
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
        except Exception as e:
            return f"Si è verificato un errore durante la gestione della nota: {e}"

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
            "note_action": False
        }

        # 1. Deissi / Lettura Clipboard preliminare
        prompt_with_context, clip_attached, clip_context = self.detect_and_attach_clipboard(user_prompt, explicit_selection)
        if clip_attached:
            metadata["clipboard"] = True
            self.set_status("Lettura Clipboard...", user_prompt)

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
        self.set_status(f"Elaborazione con {selected_model}...", user_prompt)

        system_content = WHIS_SYSTEM_PROMPT
        if metadata.get("screen_ocr"):
            system_content = (
                "ATTENZIONE: HAI ACCESSO VISIVO ALLO SCHERMO DELL'UTENTE TRAMITE IL MODULO OCR LOCALE. "
                "È ASSOLUTAMENTE VIETATO dire 'Non posso vedere il tuo schermo' o 'Sono un modello testuale'. "
                "Qui sotto trovi il titolo della finestra attiva e tutto il testo appena letto dallo schermo dell'utente: "
                "usalo direttamente per rispondere a cosa sta guardando!\n\n"
                + WHIS_SYSTEM_PROMPT
            )

        messages = [{"role": "system", "content": system_content}]
        # Inietta gli ultimi scambi della sessione (fino a 10 messaggi = 5 scambi)
        messages.extend(self.history[-10:])
        messages.append({"role": "user", "content": prompt_with_context})

        payload = {
            "model": selected_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.45,
                "top_p": 0.9,
            }
        }

        full_response: List[str] = []
        actual_model = selected_model

        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
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
            # Se il modello profondo fallisce o non risponde, fallback immediato su 3B
            if selected_model != self.default_model and self.model_mode != "7b":
                try:
                    payload["model"] = self.default_model
                    actual_model = self.default_model
                    self.set_status(f"Fallback su {self.default_model}...", user_prompt)
                    req = urllib.request.Request(
                        OLLAMA_API_URL,
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
