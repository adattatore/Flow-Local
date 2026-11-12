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
import urllib.request
import urllib.error
import pyperclip
from typing import Callable, Optional, List, Dict, Tuple
from PyQt6.QtCore import QObject, QThread, pyqtSignal

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

DEICTIC_KEYWORDS = re.compile(
    r'\b('
    r'questo errore|quest\'errore|questo codice|questo testo|'
    r'spiegami questo|correggi questo|analizza questo|guarda questo|'
    r'su questo|questo script|questi dati|questa funzione|questa classe|'
    r'questa eccezione|questo traceback|questo log|questa riga'
    r')\b',
    re.IGNORECASE
)

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

    def __init__(self, default_model: str = "qwen2.5:3b", deep_model: str = "qwen2.5:7b", parent=None):
        super().__init__(parent)
        self.default_model = default_model
        self.deep_model = deep_model
        self.model_mode = "auto"  # "auto", "3b", "7b"
        self.history: List[Dict[str, str]] = []
        self.current_status = "In attesa"
        self.last_query = ""

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

    def detect_and_attach_clipboard(self, prompt: str, explicit_selection: Optional[str] = None) -> Tuple[str, bool, str]:
        """
        Intercetta riferimenti deittici ('questo codice', 'questo errore') o selezioni attive
        e include automaticamente il contenuto degli appunti nel prompt inviato al modello.
        Restituisce: (prompt_con_contesto, clipboard_allegata_bool, raw_context_text).
        """
        attached = False
        context_text = ""

        if explicit_selection and explicit_selection.strip():
            context_text = explicit_selection.strip()[:8000]
            attached = True
        elif DEICTIC_KEYWORDS.search(prompt):
            try:
                clip = pyperclip.paste()
                if clip and clip.strip():
                    context_text = clip.strip()[:8000]
                    attached = True
            except Exception:
                pass

        if attached and context_text:
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
        on_done: Optional[Callable[[str, str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        explicit_selection: Optional[str] = None
    ) -> str:
        """
        Invia la richiesta in streaming a Ollama gestendo il fallback del modello,
        lo stato in tempo reale e il salvataggio automatico nel database locale.
        """
        self.set_status("Analisi richiesta...", user_prompt)
        selected_model = self.select_model(user_prompt)

        # Deissi / Lettura Clipboard
        prompt_with_context, clip_attached, clip_context = self.detect_and_attach_clipboard(user_prompt, explicit_selection)
        if clip_attached:
            self.set_status("Lettura Clipboard...", user_prompt)

        self.set_status(f"Elaborazione con {selected_model}...", user_prompt)

        messages = [{"role": "system", "content": WHIS_SYSTEM_PROMPT}]
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
            on_done(complete_text, actual_model)

        return complete_text


class WhisStreamWorker(QThread):
    """Worker QThread per lo streaming asincrono non bloccante della risposta di Whis."""

    chunk_received = pyqtSignal(str)
    finished_response = pyqtSignal(str, str)  # (full_text, model_name)
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
                on_done=lambda full_text, model: self.finished_response.emit(full_text, model),
                on_error=lambda err: self.error_occurred.emit(err),
                explicit_selection=self.explicit_selection
            )
        except Exception as e:
            self.error_occurred.emit(str(e))
