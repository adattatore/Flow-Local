"""
Core AI Pipeline per FlowLocal
Faster-Whisper Large-v3-Turbo (CUDA) + Analisi Pause (>1.3s) + Ollama Qwen 2.5
Supporta:
- Prompt Biasing e Vocabolario tecnico tramite Qwen 2.5
- Pre-filtro fonetico deterministico e Macro Snippet
- Riformattazione strutturale e rimozione autocorrezioni
- Command Mode e Study Mode
- Trascrizione affidabile a zero ritardo (senza falsi keyup)
"""

import re
import time
import json
import ctypes
import difflib
import threading
import winsound
import urllib.request
import urllib.error
from typing import Tuple, Optional

import numpy as np
import sounddevice as sd
from scipy import signal
import pyperclip
from faster_whisper import WhisperModel

from database import db
from core.audio_analyzer import AudioAnalyzer
from core.context_detector import detect_active_context

RECORD_SAMPLE_RATE = 48000
WHISPER_SAMPLE_RATE = 16000
DECIMATE_FACTOR = 3  # 48000 / 3 = 16000
CHANNELS = 1

MODEL_SIZE = "large-v3-turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

DOUBLE_TAP_TIMEOUT = 0.35
LLM_MODEL = "qwen2.5:3b"
OLLAMA_API_URL = "http://localhost:11434/api/chat"

FILLER_WORDS_REGEX = re.compile(
    r'\b(ehm+|uhm+|mhh+|ah+|eh+|vabbè)\b[\.\,\…]*\s*',
    flags=re.IGNORECASE
)

VOICE_LEARN_REGEX = re.compile(
    r'^(?:impara|correggi|salva(?: nel dizionario)?)\s+["\']?(.+?)["\']?\s+(?:in|con|come|si scrive)\s+["\']?(.+?)["\']?[.!?]?$',
    flags=re.IGNORECASE
)


def find_best_input_device():
    try:
        s = sd.InputStream(device=None, samplerate=16000, channels=1)
        s.start()
        s.stop()
        s.close()
        return None, 16000
    except Exception:
        pass

    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d.get('max_input_channels', 0) > 0:
                sr = int(d.get('default_samplerate', 16000))
                try:
                    s = sd.InputStream(device=i, samplerate=sr, channels=1)
                    s.start()
                    s.stop()
                    s.close()
                    return i, sr
                except Exception:
                    continue
    except Exception:
        pass
    return None, 16000


def sound_start():
    try:
        winsound.Beep(1100, 60)
    except Exception:
        pass

def sound_locked():
    try:
        winsound.Beep(1200, 60)
        time.sleep(0.04)
        winsound.Beep(1600, 70)
    except Exception:
        pass

def sound_done():
    try:
        winsound.Beep(850, 80)
    except Exception:
        pass

def sound_toggle_pause(paused: bool):
    try:
        winsound.Beep(600 if paused else 1000, 100)
    except Exception:
        pass


class WindowsInjector:
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002

    @classmethod
    def paste_text(cls, text: str):
        if not text:
            return

        # Rimuove eventuali backslash finali (\) o spazi superflui prima di incollare
        clean_text = text.rstrip()
        while clean_text.endswith('\\'):
            clean_text = clean_text[:-1].rstrip()

        if not clean_text:
            return

        old_clipboard = None
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            pass

        pyperclip.copy(clean_text)
        time.sleep(0.04)

        user32 = ctypes.windll.user32
        user32.keybd_event(cls.VK_CONTROL, 0, 0, 0)
        user32.keybd_event(cls.VK_V, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(cls.VK_V, 0, cls.KEYEVENTF_KEYUP, 0)
        user32.keybd_event(cls.VK_CONTROL, 0, cls.KEYEVENTF_KEYUP, 0)

        def restore():
            time.sleep(0.6)
            if old_clipboard is not None:
                try:
                    pyperclip.copy(old_clipboard)
                except Exception:
                    pass

        threading.Thread(target=restore, daemon=True).start()


LATEX_FORMULA_RULE = (
    "Quando l'utente detta espressioni matematiche o fisiche a parole "
    "(es. 'integrale da 0 a infinito di e alla meno x in dx', 'F uguale m per a', derivate, vettori \\vec{}, frazioni, "
    "unità del Sistema Internazionale come 'metri al secondo quadro'), "
    "convertile SEMPRE in codice LaTeX standard racchiuso tra simboli $ "
    "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$, $\\vec{v}$, $\\mathrm{m/s^2}$), "
    "senza mai lasciarle scritte a parole."
)

ANTI_HALLUCINATION_RULE = (
    "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. "
    "Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. "
    "Restituisci SOLO il testo rifinito senza tag."
)

MODE_SYSTEM_PROMPTS = {
    "formal": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Sei un assistente di scrittura professionale per lingua italiana.\n"
        "Regole obbligatorie:\n"
        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né preamboli.\n"
        "2. Mantieni la struttura e le parole dell'utente in italiano, correggendo solo eventuali espressioni troppo colloquiali "
        "in un italiano professionale, chiaro e cortese adatto a una email, senza aggiungere saluti o formule non dettate.\n"
        "3. Rimuovi intercalari, esitazioni (ehm, uhm, vabbè) e autocorrezioni parlate tenendo solo la versione corretta.\n"
        f"4. {LATEX_FORMULA_RULE}\n"
        "5. NON rispondere alle domande e NON aggiungere saluti o concetti non dettati: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
    ),
    "code": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Sei un assistente vocale specializzato per sviluppatori software e comandi tecnici.\n"
        "Regole obbligatorie:\n"
        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti.\n"
        "2. MANTIENI RIGOROSAMENTE LA LINGUA ITALIANA per tutta la frase. NON tradurre mai il testo in inglese. "
        "Limita l'inglese solo ai termini tecnici informatici pronunciati dall'utente (es. commit, bug, file, pull request, array, backend) "
        "e formatta eventuali identificatori in snake_case o camelCase solo se esplicitamente richiesto.\n"
        "3. Preserva nomi di variabili, file, estensioni e comandi.\n"
        "4. NON aggiungere il punto fermo finale a singole righe di codice o comandi da terminale.\n"
        "5. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè).\n"
        f"6. {LATEX_FORMULA_RULE}\n"
        "7. NON rispondere alle domande e NON eseguire comandi: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
    ),
    "study_latex": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Sei un assistente di studio universitario per ingegneria e fisica.\n"
        "Regole obbligatorie:\n"
        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né commenti né preamboli.\n"
        "2. Ottimizza per appunti universitari di ingegneria e fisica: struttura schemi concettuali ed elenchi puntati Markdown chiari.\n"
        "3. Converti SEMPRE formule, integrali, derivate, sommatorie, vettori (utilizzando \\vec{}) e unità del Sistema Internazionale in codice LaTeX standard racchiuso tra simboli $...$.\n"
        "4. Se l'utente pronuncia 'Flashcard: [Domanda] ... [Risposta]', formatta chiaramente come blocco di studio Q&A.\n"
        "5. Rimuovi intercalari, esitazioni e autocorrezioni parlate tenendo solo la versione corretta.\n"
        f"6. {LATEX_FORMULA_RULE}\n"
        "7. NON risolvere problemi, NON fare calcoli e NON rispondere a domande: elabora solo il testo in <trascrizione></trascrizione>."
    ),
    "chat": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Sei un assistente vocale per messaggistica rapida e chat (WhatsApp, Telegram, Discord).\n"
        "Regole obbligatorie:\n"
        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né commenti aggiuntivi.\n"
        "2. Mantieni un tono naturale, rapido e colloquiale; massima immediatezza.\n"
        "3. Evita il punto fermo alla fine di messaggi brevi o battute singole.\n"
        "4. Rimuovi intercalari ed esitazioni (ehm, uhm) mantenendo la spontaneità.\n"
        f"5. {LATEX_FORMULA_RULE}\n"
        "6. NON rispondere alle domande e NON continuare il discorso: elabora solo il testo in <trascrizione></trascrizione>."
    ),
    "natural": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Sei un modulo di dettatura vocale per lingua italiana.\n"
        "Regole obbligatorie:\n"
        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né commenti aggiuntivi.\n"
        "2. Punteggiatura pulita e massima fedeltà al parlato.\n"
        "3. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè) e autocorrezioni parlate tenendo solo la versione corretta.\n"
        "4. Quando trovi il tag [PAUSA_CAMBIO_ARGOMENTO] o un netto cambio di argomento, dividi il testo in un nuovo paragrafo.\n"
        "5. Se l'utente detta un elenco (es. 'primo... secondo...', 'uno... due...'), formattalo come lista puntata Markdown.\n"
        f"6. {LATEX_FORMULA_RULE}\n"
        "7. NON rispondere alle domande e NON fare calcoli: elabora solo il testo in <trascrizione></trascrizione>."
    )
}


class LLMRefiner:
    """Stadio 2: Rifinitura semantica, punteggiatura e vocabolario tecnico con Qwen 2.5."""

    @classmethod
    def refine(cls, raw_text: str, mode: str = "natural") -> Tuple[str, int]:
        t0 = time.time()
        if not raw_text or not raw_text.strip():
            return raw_text, 0

        # 1. Snippet Vocali deterministici
        expanded = db.match_and_expand_snippet(raw_text)
        if expanded:
            return expanded, 0

        # 2. Controllo Flashcard automatica
        if raw_text.strip().lower().startswith("flashcard:"):
            cls._save_flashcard(raw_text)

        # 3. Verifica Stile Attivo (Zero LLM)
        active_style = db.get_active_style()
        if active_style.get("system_prompt") == "RAW_WHISPER_NO_LLM":
            return raw_text, 0

        # 4. Selezione dinamica del System Prompt in base alla modalità
        mode_clean = (mode or "natural").lower()
        if mode_clean in ("code", "coding", "code / tech"):
            mode_key = "code"
        elif mode_clean in ("study_latex", "study", "study_mode", "studio / latex", "studio"):
            mode_key = "study_latex"
        elif mode_clean in ("formal", "formale"):
            mode_key = "formal"
        elif mode_clean in ("chat", "chat / diretto"):
            mode_key = "chat"
        else:
            mode_key = "natural"

        system_prompt = MODE_SYSTEM_PROMPTS.get(mode_key, MODE_SYSTEM_PROMPTS["natural"])

        # Assicura la presenza della regola anti-allucinazione nel prompt
        if "SEI UN FILTRO DI PULIZIA TESTUALE" not in system_prompt:
            system_prompt = f"{ANTI_HALLUCINATION_RULE}\n\n{system_prompt}"

        # Aggiunta Vocabolario al prompt di Qwen
        vocab_terms = db.get_vocabulary_biasing_prompt()
        if vocab_terms:
            system_prompt += f"\nVocabolario e termini tecnici da rispettare: {vocab_terms}."

        user_content = f"Testo pronunciato da elaborare:\n<trascrizione>\n{raw_text}\n</trascrizione>"

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "repeat_penalty": 1.15,
                "temperature": 0.1,
                "num_predict": max(100, len(raw_text.split()) * 3),
                "stop": ["</trascrizione>"]
            }
        }

        qwen_ms = 0
        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("message", {}).get("content", "").strip()
                qwen_ms = int((time.time() - t0) * 1000)

                # Rimozione preliminare di qualsiasi tag residuo simile a <trascrizione> (inclusi refusi come </trascrizizione>)
                refined = re.sub(r"</?trascri\w*\s*>", "", content, flags=re.IGNORECASE).strip()

                # Estrazione e rimozione di eventuali altri tag XML residui (<output>, ecc.)
                for tag in ("output", "text", "refined"):
                    match = re.search(rf'<{tag}>(.*?)</{tag}>', refined, flags=re.DOTALL | re.IGNORECASE)
                    if match:
                        refined = match.group(1)
                    else:
                        refined = re.sub(rf'</?{tag}>', '', refined, flags=re.IGNORECASE)

                refined = re.sub(r"</?trascri\w*\s*>", "", refined, flags=re.IGNORECASE).strip()

                refined = refined.strip()
                if refined.startswith('"') and refined.endswith('"'):
                    refined = refined[1:-1].strip()

                # Rimuove eventuali backslash finali (\) o spazi superflui
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                # Guardrail sulla lunghezza anti-allucinazione:
                # Se Qwen genera spiegazioni, risponde alla domanda o allucina:
                # scarta la risposta se supera max(len(raw_text) * 1.45, len(raw_text) + 40)
                max_allowed_len = max(len(raw_text) * 1.45, len(raw_text) + 40)
                if len(refined) > max_allowed_len:
                    print(f"[FlowLocal] Allucinazione LLM scartata ({len(refined)} car > {max_allowed_len:.0f} max). Fallback su testo acustico.")
                    clean = db.apply_phonetic_replacements(raw_text).replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
                    while clean.endswith('\\'):
                        clean = clean[:-1].rstrip()
                    return clean, qwen_ms

                if refined:
                    return refined, qwen_ms
        except Exception:
            pass

        qwen_ms = int((time.time() - t0) * 1000)
        clean = db.apply_phonetic_replacements(raw_text).replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
        while clean.endswith('\\'):
            clean = clean[:-1].rstrip()
        return clean, qwen_ms

    @classmethod
    def transform_selection(cls, selected_text: str, voice_command: str) -> str:
        """Trasforma il testo selezionato applicando l'istruzione vocale dell'utente via Qwen 2.5."""
        if not selected_text or not selected_text.strip():
            return selected_text
        if not voice_command or not voice_command.strip():
            return selected_text

        system_prompt = (
            "SEI UN EDITOR DI TESTI PROFESSIONALE PER LINGUA ITALIANA. "
            "Applica l'istruzione dell'utente al testo selezionato mantenendo rigorosamente tutti i fatti, "
            "i tempi verbali (passato, presente, futuro) e il significato originale. "
            "Usa un italiano formale idiomatico, corretto nei congiuntivi e senza mai inserire parole inglesi o calchi dall'inglese. "
            "Restituisci ESCLUSIVAMENTE il testo trasformato.\n\n"
            "Esempio:\n"
            "<comando>Riscrivilo in stile formale</comando>\n"
            "<testo_selezionato>ciao prof le mando il file domani appena finisco di controllare i conti spero vada bene</testo_selezionato>\n"
            "Output:\n"
            "Gentile Professore, Le invierò il file domani non appena avrò terminato il controllo dei conti. Spero che vada tutto bene."
        )

        user_content = (
            f"<comando>{voice_command.strip()}</comando>\n"
            f"<testo_selezionato>{selected_text.strip()}</testo_selezionato>"
        )

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "repeat_penalty": 1.15,
                "temperature": 0.15,
                "top_p": 0.85,
                "num_predict": max(300, len(selected_text.split()) * 4),
                "stop": ["</testo_selezionato>", "</comando>", "</trascrizione>"]
            }
        }

        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("message", {}).get("content", "").strip()

                # Pulizia tag residui (inclusi refusi o chiusure XML)
                refined = re.sub(r"</?(?:testo_selezionato|comando|trascri\w*|output|result)>\s*", "", content, flags=re.IGNORECASE).strip()
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                if refined:
                    return refined
        except Exception as e:
            print(f"[FlowLocal] Errore trasformazione selezione LLM: {e}")

        return selected_text

    @classmethod
    def _save_flashcard(cls, text: str):
        try:
            content = text[10:].strip()
            parts = content.split("?", 1)
            if len(parts) == 2:
                q = parts[0].strip() + "?"
                a = parts[1].strip()
            else:
                q = content
                a = ""
            db.add_study_note(title=f"Flashcard: {q[:30]}...", content_markdown=f"**D:** {q}\n\n**R:** {a}", note_type="Flashcard", tags="Vocale")
        except Exception:
            pass


class FlowLocalEngine:
    def __init__(self, orb=None, hub=None):
        self.orb = orb
        self.hub = hub
        self.analyzer = AudioAnalyzer(num_half_bands=12, intensity=0.80, smoothing=0.50)

        self.model = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            download_root=None,
        )

        def warm_llm():
            try:
                LLMRefiner.refine("Test audio iniziale")
            except Exception:
                pass
        threading.Thread(target=warm_llm, daemon=True).start()

        self.audio_frames = []
        self.is_recording = False
        self.is_locked = False
        self.is_paused = False
        self.current_mode = "LISTENING"
        self.current_context = ("Desktop", "natural", "Naturale")
        self.active_mode_key = "natural"
        self.active_selection_text: Optional[str] = None
        self.previous_clipboard: Optional[str] = None

        self.last_press_time = 0.0
        self.last_release_time = 0.0
        self.record_start_time = 0.0
        self.lock = threading.Lock()
        self.key_lock = threading.Lock()
        self.is_key_down = False
        self.live_rms = 0.0

        self.dev_index, self.sample_rate = find_best_input_device()
        self.use_decimation = (self.sample_rate != WHISPER_SAMPLE_RATE)
        self.stream = None
        self._start_stream()

    def _start_stream(self):
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass

        block_size = int(self.sample_rate * 0.05)
        self.stream = sd.InputStream(
            device=self.dev_index,
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="float32",
            blocksize=block_size,
            callback=self._audio_callback,
        )
        self.stream.start()

    def change_device(self, new_device_idx):
        self.dev_index = new_device_idx
        if new_device_idx is not None:
            try:
                info = sd.query_devices(new_device_idx)
                self.sample_rate = int(info.get('default_samplerate', 16000))
            except Exception:
                self.sample_rate = 16000
        else:
            self.sample_rate = 16000
        self.use_decimation = (self.sample_rate != WHISPER_SAMPLE_RATE)
        self._start_stream()

    def set_mode(self, mode: str):
        self.current_mode = mode
        if self.is_recording and self.orb:
            self.orb.set_state(mode)

    def handle_key_press(self):
        with self.key_lock:
            if self.is_key_down:
                return  # Ignora typematic repeat mentre il tasto è tenuto premuto
            self.is_key_down = True

            now = time.time()
            time_since_last = now - self.last_press_time
            self.last_press_time = now

            if self.is_recording:
                if self.is_locked:
                    self.stop_recording_and_process()
            else:
                if time_since_last < DOUBLE_TAP_TIMEOUT:
                    self.start_recording(locked=True)
                else:
                    self.start_recording(locked=False)

    def handle_key_release(self):
        with self.key_lock:
            now = time.time()
            if (now - self.last_release_time) < 0.350:  # Debounce 350ms
                return
            self.last_release_time = now

            if not self.is_key_down:
                return
            self.is_key_down = False

            if self.is_recording and not self.is_locked:
                self.stop_recording_and_process()

    def toggle_recording_click(self):
        if self.is_recording:
            self.stop_recording_and_process()
        else:
            self.start_recording(locked=True)

    def toggle_pause(self) -> bool:
        self.is_paused = not self.is_paused
        sound_toggle_pause(self.is_paused)
        return self.is_paused

    def _audio_callback(self, indata, frames, time_info, status):
        chunk = indata.copy()
        rms = float(np.sqrt(np.mean(chunk**2)))
        self.live_rms = rms

        self.analyzer.process_chunk(chunk)

        if self.orb:
            self.orb.feed_audio(chunk)

        if self.hub and self.hub.isVisible():
            self.hub.update_live_volume(min(1.0, rms * 10.0))

        with self.lock:
            if self.is_recording:
                self.audio_frames.append(chunk)

    def start_recording(self, locked=False):
        if self.is_paused:
            return

        with self.lock:
            if self.is_recording:
                return

            # 0. Rilevamento selezione attiva da trasformare (Text-to-Action)
            previous_clip = None
            try:
                previous_clip = pyperclip.paste()
            except Exception:
                pass
            self.previous_clipboard = previous_clip

            selected_text = None
            try:
                pyperclip.copy("")
                user32 = ctypes.windll.user32
                VK_CONTROL = 0x11
                VK_C = 0x43
                KEYEVENTF_KEYUP = 0x0002
                user32.keybd_event(VK_CONTROL, 0, 0, 0)
                user32.keybd_event(VK_C, 0, 0, 0)
                time.sleep(0.100)  # ~100ms di attesa per la cattura selezione su Windows
                user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
                user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.020)

                current_clip = (pyperclip.paste() or "").strip()
                if current_clip:
                    selected_text = current_clip
            except Exception:
                selected_text = None

            if selected_text:
                self.active_selection_text = selected_text
                # Non ripristiniamo subito la clipboard precedente: selected_text rimane attivo per la sessione
                sel_len = len(selected_text)
                badge_str = f"✨ Selezione ({sel_len} car.)"
            else:
                self.active_selection_text = None
                badge_str = None
                # Ripristina subito la clipboard precedente se non c'è selezione
                if previous_clip is not None:
                    try:
                        pyperclip.copy(previous_clip)
                    except Exception:
                        pass

            # Rilevamento immediato del contesto attivo
            app_label, mode_key, mode_label = detect_active_context()
            self.current_context = (app_label, mode_key, mode_label)
            self.active_mode_key = mode_key

            # Pre-buffer di 350ms
            self.audio_frames = list(self.analyzer.ring_buffer)
            self.is_recording = True
            self.is_locked = locked
            self.record_start_time = time.time()

            if self.orb:
                # set_badge_text emette set_badge_signal al main thread Qt in modo thread-safe
                if badge_str:
                    self.orb.set_badge_text(badge_str)
                else:
                    self.orb.set_badge_text(f"{app_label} • {mode_label}")
                orb_state = "STUDY_MODE" if mode_key == "study_latex" else self.current_mode
                self.orb.set_state(orb_state)
                self.orb.set_locked(locked)

            if locked:
                threading.Thread(target=sound_locked, daemon=True).start()
            else:
                threading.Thread(target=sound_start, daemon=True).start()

    def stop_recording_and_process(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            self.is_locked = False
            captured = list(self.audio_frames)
            self.audio_frames.clear()
            rec_mode = self.active_mode_key
            rec_context = self.current_context
            selection_text = self.active_selection_text
            prev_clipboard = self.previous_clipboard
            self.active_selection_text = None
            self.previous_clipboard = None

        if self.orb:
            self.orb.set_state("WHISPER")
            self.orb.set_locked(False)

        if not captured:
            if self.orb:
                self.orb.set_state("IDLE")
            # Ripristina previous_clipboard se presente
            if prev_clipboard is not None:
                try:
                    pyperclip.copy(prev_clipboard)
                except Exception:
                    pass
            return

        raw_audio = np.concatenate(captured, axis=0).flatten()

        if self.use_decimation and self.sample_rate != WHISPER_SAMPLE_RATE:
            num_target_samples = int(len(raw_audio) * WHISPER_SAMPLE_RATE / self.sample_rate)
            audio_16k = signal.resample(raw_audio, num_target_samples).astype(np.float32)
        else:
            audio_16k = raw_audio

        duration_s = len(audio_16k) / WHISPER_SAMPLE_RATE

        if duration_s < 0.20:
            if self.orb:
                self.orb.set_state("IDLE")
            if prev_clipboard is not None:
                try:
                    pyperclip.copy(prev_clipboard)
                except Exception:
                    pass
            return

        threading.Thread(
            target=self._transcribe_and_inject,
            args=(audio_16k, duration_s, rec_mode, rec_context, selection_text, prev_clipboard),
            daemon=True
        ).start()

    def _transcribe_and_inject(self, audio_data: np.ndarray, duration_s: float, mode: str, context: tuple = ("Desktop", "natural", "Naturale"), selection_text: Optional[str] = None, previous_clipboard: Optional[str] = None):
        t0 = time.time()
        whisper_ms = 0
        try:
            # 1. Normalizzazione audio (elimina problemi di microfoni a basso guadagno)
            max_val = float(np.max(np.abs(audio_data)))
            if max_val > 0.0005:
                audio_data = (audio_data / max_val * 0.85).astype(np.float32)
            else:
                print("[FlowLocal] Livello audio impercettibile / microfono muto.")
                if self.orb:
                    self.orb.set_state("IDLE")
                if previous_clipboard is not None:
                    try:
                        pyperclip.copy(previous_clipboard)
                    except Exception:
                        pass
                return

            # 2. Faster-Whisper Inference con VAD, barriere anti-allucinazione e bias acustico dinamico
            whisper_prompt = db.get_whisper_initial_prompt(limit=40) or None

            segments, info = self.model.transcribe(
                audio_data,
                beam_size=5,
                language="it",
                temperature=0.0,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
                initial_prompt=whisper_prompt,
            )

            segments = list(segments)
            # Fallback immediato senza VAD nel caso la voce sia stata tagliata
            if not segments:
                segments, _ = self.model.transcribe(
                    audio_data,
                    beam_size=5,
                    language="it",
                    temperature=0.0,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.6,
                    vad_filter=False,
                    initial_prompt=whisper_prompt,
                )
                segments = list(segments)

            # Analisi delle pause temporali (> 1.3 secondi)
            text_parts = []
            prev_end = None

            for seg in segments:
                if seg.no_speech_prob > 0.6:
                    continue
                seg_text = seg.text.strip()
                if not seg_text:
                    continue

                if prev_end is not None and (seg.start - prev_end) > 1.3:
                    text_parts.append("[PAUSA_CAMBIO_ARGOMENTO]")

                text_parts.append(seg_text)
                prev_end = seg.end

            raw_text = " ".join(text_parts).strip()
            whisper_ms = int((time.time() - t0) * 1000)

            if not raw_text:
                print("[FlowLocal] Nessun testo vocale rilevato.")
                if self.orb:
                    self.orb.set_state("IDLE")
                if previous_clipboard is not None:
                    try:
                        pyperclip.copy(previous_clipboard)
                    except Exception:
                        pass
                return

            print(f"[FlowLocal] Trascrizione grezza Whisper ({whisper_ms}ms): {raw_text}")

            # =========================================================
            # CASO A: TRASFORMAZIONE SELEZIONE ATTIVA (Text-to-Action)
            # =========================================================
            if selection_text and selection_text.strip():
                if self.orb:
                    self.orb.set_state("QWEN")

                t_qwen = time.time()
                transformed_text = LLMRefiner.transform_selection(
                    selected_text=selection_text,
                    voice_command=raw_text
                )
                qwen_ms = int((time.time() - t_qwen) * 1000)

                # Sovrascrive direttamente il testo evidenziato nella finestra attiva
                pyperclip.copy(transformed_text)
                time.sleep(0.04)

                user32 = ctypes.windll.user32
                VK_CONTROL = 0x11
                VK_V = 0x56
                KEYEVENTF_KEYUP = 0x0002
                user32.keybd_event(VK_CONTROL, 0, 0, 0)
                user32.keybd_event(VK_V, 0, 0, 0)
                time.sleep(0.03)
                user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
                user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

                sound_done()

                # Ripristina previous_clipboard dopo ~150ms
                def restore_clip():
                    time.sleep(0.18)
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass

                threading.Thread(target=restore_clip, daemon=True).start()

                # Salvataggio nel database locale con mode="Selezione"
                app_label, _, _ = context
                db.add_transcription(
                    raw_text=f"[Comando: {raw_text}] Su selezione ({len(selection_text)} car.)",
                    final_text=transformed_text,
                    target_app=app_label,
                    style_used="Selezione",
                    whisper_ms=whisper_ms,
                    qwen_ms=qwen_ms,
                    duration_s=duration_s
                )
                return

            # =========================================================
            # CASO B: DETTATURA NORMALE ESISTENTE
            # =========================================================

            # 3. Intercettazione apprendimento vocale istantaneo ("Correggi X in Y")
            learn_match = VOICE_LEARN_REGEX.match(raw_text.strip())
            if learn_match:
                errata = learn_match.group(1).strip().strip('"\'.,')
                corretta = learn_match.group(2).strip().strip('"\'.,')
                if errata and corretta and errata.lower() != corretta.lower():
                    print(f"[FlowLocal] Apprendimento vocale istantaneo: '{errata}' -> '{corretta}'")
                    db.learn_vocabulary_pair(errata, corretta)
                    if self.orb:
                        self.orb.show_toast(f"+ Imparato: {errata} → {corretta}", duration_s=2.5)
                    sound_done()
                    return

            # Sostituzione deterministica alias fonetici
            raw_text = db.apply_phonetic_replacements(raw_text)

            # Stadio 2: Rifinitura Qwen 2.5
            if self.orb:
                self.orb.set_state("QWEN")

            final_text, qwen_ms = LLMRefiner.refine(raw_text, mode=mode)
            final_text = final_text.rstrip()
            while final_text.endswith('\\'):
                final_text = final_text[:-1].rstrip()

            # Iniezione testo e feedback sonoro
            WindowsInjector.paste_text(final_text)
            sound_done()

            # Watcher passivo su clipboard per correzioni manuali post-incolla (12 secondi)
            self._start_clipboard_learning_watcher(final_text)

            # Salvataggio nel database locale
            app_label, mode_key, mode_label = context
            db.add_transcription(
                raw_text=raw_text,
                final_text=final_text,
                target_app=app_label,
                style_used=mode_label,
                whisper_ms=whisper_ms,
                qwen_ms=qwen_ms,
                duration_s=duration_s
            )

            # Se in Study Mode o study_latex, salva una copia negli Appunti di Studio
            if mode in ("STUDY_MODE", "study_latex"):
                title_preview = final_text.split("\n")[0][:40] or "Appunto Studio"
                db.add_study_note(title=title_preview, content_markdown=final_text, note_type="Note", tags="Studio")

        except Exception as e:
            print("Transcription error:", e)
        finally:
            if self.orb:
                self.orb.set_state("IDLE")

    @staticmethod
    def _find_correction_pair(injected_text: str, clip_text: str) -> Optional[Tuple[str, str]]:
        clip = (clip_text or "").strip()
        injected = (injected_text or "").strip()
        if not clip or not injected or clip == injected:
            return None

        orig_words = injected.split()
        clip_words = clip.split()

        # Caso 1: L'utente ha selezionato e copiato/tagliato la frase o frammento modificato (>2 parole)
        if len(clip_words) > 2:
            matcher = difflib.SequenceMatcher(None, orig_words, clip_words)
            opcodes = matcher.get_opcodes()
            replace_ops = [op for op in opcodes if op[0] == 'replace']
            if len(replace_ops) == 1:
                tag, i1, i2, j1, j2 = replace_ops[0]
                if (1 <= (i2 - i1) <= 2) and (1 <= (j2 - j1) <= 2):
                    errata = ' '.join(orig_words[i1:i2])
                    corretta = ' '.join(clip_words[j1:j2])
                    errata_clean = re.sub(r'^[^\w]+|[^\w]+$', '', errata)
                    corretta_clean = re.sub(r'^[^\w]+|[^\w]+$', '', corretta)
                    if errata_clean and corretta_clean and errata_clean.lower() != corretta_clean.lower():
                        sim = difflib.SequenceMatcher(None, errata_clean.lower(), corretta_clean.lower()).ratio()
                        if sim > 0.45 and len(errata_clean) <= 30 and len(corretta_clean) <= 30:
                            return errata_clean, corretta_clean

        # Caso 2: L'utente ha copiato solo la singola parola/espressione corretta (1 o 2 parole)
        if 1 <= len(clip_words) <= 2 and len(clip) <= 30:
            corretta_clean = re.sub(r'^[^\w]+|[^\w]+$', '', clip)
            if not corretta_clean:
                return None
            best_match = None
            best_sim = 0.45
            for n in (1, 2):
                for i in range(len(orig_words) - n + 1):
                    cand = ' '.join(orig_words[i:i+n])
                    cand_clean = re.sub(r'^[^\w]+|[^\w]+$', '', cand)
                    if not cand_clean or cand_clean.lower() == corretta_clean.lower():
                        continue
                    sim = difflib.SequenceMatcher(None, cand_clean.lower(), corretta_clean.lower()).ratio()
                    if sim > best_sim and len(cand_clean) <= 30:
                        best_sim = sim
                        best_match = (cand_clean, corretta_clean)
            if best_match:
                return best_match

        return None

    def _start_clipboard_learning_watcher(self, injected_text: str):
        if not injected_text or not injected_text.strip():
            return
        threading.Thread(
            target=self._clipboard_learning_worker,
            args=(injected_text.strip(),),
            daemon=True
        ).start()

    def _clipboard_learning_worker(self, injected_text: str):
        # Attesa per il completamento dell'iniezione Ctrl+V e del ripristino clipboard iniziale
        time.sleep(0.7)
        try:
            initial_clip = pyperclip.paste()
        except Exception:
            initial_clip = ""

        start_time = time.time()
        last_clip = initial_clip

        while (time.time() - start_time) < 12.0:
            time.sleep(0.35)
            try:
                curr_clip = pyperclip.paste()
            except Exception:
                continue

            if not curr_clip or curr_clip == last_clip or curr_clip == injected_text:
                continue

            last_clip = curr_clip
            pair = self._find_correction_pair(injected_text, curr_clip)
            if pair:
                errata, corretta = pair
                print(f"[FlowLocal] Apprendimento passivo da correzione clipboard: '{errata}' -> '{corretta}'")
                db.learn_vocabulary_pair(errata, corretta)
                if self.orb:
                    self.orb.show_toast(f"+ Imparato: {errata} → {corretta}", duration_s=2.5)
                break
