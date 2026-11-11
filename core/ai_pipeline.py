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
import threading
import winsound
import urllib.request
import urllib.error
from typing import Tuple

import numpy as np
import sounddevice as sd
from scipy import signal
import pyperclip
from faster_whisper import WhisperModel

from database import db
from core.audio_analyzer import AudioAnalyzer

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


class LLMRefiner:
    """Stadio 2: Rifinitura semantica, punteggiatura e vocabolario tecnico con Qwen 2.5."""

    @classmethod
    def refine(cls, raw_text: str, mode: str = "LISTENING") -> Tuple[str, int]:
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

        # 3. Verifica Stile Attivo
        active_style = db.get_active_style()
        if active_style.get("system_prompt") == "RAW_WHISPER_NO_LLM":
            return raw_text, 0

        system_prompt = active_style.get("system_prompt", "")

        # Regola obbligatoria formule matematiche in LaTeX
        latex_rule = (
            "Quando l'utente detta espressioni matematiche o fisiche a parole "
            "(es. 'integrale da 0 a infinito di e alla meno x in dx', 'F uguale m per a', derivate, vettori, frazioni), "
            "convertile SEMPRE in codice LaTeX standard racchiuso tra simboli $ "
            "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$), senza mai lasciarle scritte a parole."
        )
        if "convertile SEMPRE in codice LaTeX" not in system_prompt:
            system_prompt += f"\n\nRegola obbligatoria formule matematiche:\n{latex_rule}"

        # Aggiunta Vocabolario al prompt di Qwen
        vocab_terms = db.get_vocabulary_biasing_prompt()
        if vocab_terms:
            system_prompt += f"\nVocabolario e termini tecnici da rispettare: {vocab_terms}."

        user_content = f"Testo pronunciato da elaborare: {raw_text}"

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
                "num_predict": max(100, len(raw_text.split()) * 3)
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

                # Estrazione dal tag <output></output>
                match = re.search(r'<output>(.*?)</output>', content, flags=re.DOTALL)
                if match:
                    refined = match.group(1).strip()
                else:
                    refined = content.replace("<output>", "").replace("</output>", "").strip()

                if refined.startswith('"') and refined.endswith('"'):
                    refined = refined[1:-1].strip()

                # Rimuove eventuali backslash finali (\) o spazi superflui
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                if refined:
                    return refined, qwen_ms
        except Exception:
            pass

        qwen_ms = int((time.time() - t0) * 1000)
        clean = raw_text.replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
        while clean.endswith('\\'):
            clean = clean[:-1].rstrip()
        return clean, qwen_ms

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

            # Pre-buffer di 350ms
            self.audio_frames = list(self.analyzer.ring_buffer)
            self.is_recording = True
            self.is_locked = locked
            self.record_start_time = time.time()

            if self.orb:
                self.orb.set_state(self.current_mode)
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
            rec_mode = self.current_mode

        if self.orb:
            self.orb.set_state("WHISPER")
            self.orb.set_locked(False)

        if not captured:
            if self.orb:
                self.orb.set_state("IDLE")
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
            return

        threading.Thread(
            target=self._transcribe_and_inject,
            args=(audio_16k, duration_s, rec_mode),
            daemon=True
        ).start()

    def _transcribe_and_inject(self, audio_data: np.ndarray, duration_s: float, mode: str):
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
                return

            # 2. Faster-Whisper Inference con VAD a soglia sensibile
            segments, info = self.model.transcribe(
                audio_data,
                beam_size=5,
                language="it",
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=400,
                    speech_pad_ms=250,
                    threshold=0.20,
                ),
                initial_prompt=None,
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
                    vad_filter=False,
                    initial_prompt=None,
                )
                segments = list(segments)

            # Analisi delle pause temporali (> 1.3 secondi)
            text_parts = []
            prev_end = None

            for seg in segments:
                if seg.no_speech_prob > 0.85:
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
                return

            print(f"[FlowLocal] Trascrizione grezza Whisper ({whisper_ms}ms): {raw_text}")

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

            # Salvataggio nel database locale
            active_style = db.get_active_style().get("display_name", "Naturale")
            db.add_transcription(
                raw_text=raw_text,
                final_text=final_text,
                style_used=active_style,
                whisper_ms=whisper_ms,
                qwen_ms=qwen_ms,
                duration_s=duration_s
            )

            # Se in Study Mode, salva una copia negli Appunti di Studio
            if mode == "STUDY_MODE":
                title_preview = final_text.split("\n")[0][:40] or "Appunto Studio"
                db.add_study_note(title=title_preview, content_markdown=final_text, note_type="Note", tags="Studio")

        except Exception as e:
            print("Transcription error:", e)
        finally:
            if self.orb:
                self.orb.set_state("IDLE")
