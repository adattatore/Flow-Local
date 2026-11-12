"""
Core Audio Pipeline per FlowLocal
Gestisce Faster-Whisper Large-v3-Turbo su CUDA, Ollama Qwen 2.5, Audio WASAPI continuo,
iniezione del testo in Windows e integrazione con il Database SQLite.
"""

import os
import re
import site
import time
import json
import ctypes
import threading
import collections
import winsound
import urllib.request
import urllib.error
from typing import Optional

# Caricamento DLL NVIDIA per CTranslate2 / CUDA
for sp in site.getsitepackages():
    for sub in [('nvidia', 'cublas', 'bin'), ('nvidia', 'cudnn', 'bin')]:
        p = os.path.join(sp, *sub)
        if os.path.exists(p):
            try:
                os.add_dll_directory(p)
            except Exception:
                pass

import numpy as np
import sounddevice as sd
from scipy import signal
import pyperclip
from faster_whisper import WhisperModel

from database import db
from ui.voice_orb import VoiceOrbOverlay
from ui.hub_window import FlowLocalHub

RECORD_SAMPLE_RATE = 48000
WHISPER_SAMPLE_RATE = 16000
DECIMATE_FACTOR = 3  # 48000 / 3 = 16000
CHANNELS = 1

MODEL_SIZE = "large-v3-turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

PRE_BUFFER_CHUNKS = 7  # ~350ms pre-buffer per non perdere l'inizio
DOUBLE_TAP_TIMEOUT = 0.35

LLM_MODEL = "qwen2.5:3b"
OLLAMA_API_URL = "http://localhost:11434/api/chat"

DEFAULT_WHISPER_INITIAL_PROMPT = (
    "FlowLocal, Whis, JARVIS, Ollama, Llama, Qwen, Faster-Whisper, "
    "PyQt6, HUD, overlay, pop-up, GitHub, Antigravity, Emporio 87."
)

FILLER_WORDS_REGEX = re.compile(
    r'\b(ehm+|uhm+|mhh+|ah+|eh+|vabbè)\b[\.\,\…]*\s*',
    flags=re.IGNORECASE
)


def find_best_input_device():
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            host = sd.query_hostapis(d['hostapi'])['name']
            if 'WASAPI' in host and d['max_input_channels'] > 0 and 'Microphone' in d['name']:
                return i, 48000
    except Exception:
        pass
    return None, 16000


# Feedback Sonori
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
        if not text.strip():
            return

        old_clipboard = None
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            pass

        pyperclip.copy(text)
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
    def refine(cls, raw_text: str) -> str:
        if not raw_text or not raw_text.strip():
            return raw_text

        # 1. Verifica se combacia con uno Snippet Vocale registrato
        snippet_text = db.check_snippet_match(raw_text)
        if snippet_text:
            return snippet_text

        # 2. Verifica lo stile attivo
        active_style = db.get_active_style()
        if active_style.get("prompt") == "RAW_WHISPER_NO_LLM":
            return raw_text

        pre_cleaned = FILLER_WORDS_REGEX.sub('', raw_text).strip()
        pre_cleaned = re.sub(r'([.,;:!?])([A-Za-z0-9])', r'\1 \2', pre_cleaned)

        words = pre_cleaned.split()
        if len(words) <= 1:
            return pre_cleaned

        # Costruzione del system prompt con integrazione del vocabolario tecnico
        system_prompt = active_style.get("prompt", "")
        vocab_terms = db.get_vocabulary_prompt_string()
        if vocab_terms:
            system_prompt += f"\nVocabolario tecnico e nomi da rispettare rigorosamente: {vocab_terms}."

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Testo pronunciato da trascrivere: {pre_cleaned}"}
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.0,
                "num_predict": max(60, len(words) * 2)
            }
        }

        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                refined = data.get("message", {}).get("content", "").strip()
                if refined.startswith('"') and refined.endswith('"'):
                    refined = refined[1:-1].strip()
                if refined:
                    return refined
        except Exception:
            pass

        return pre_cleaned


class FlowLocalEngine:
    def __init__(self, orb: Optional[VoiceOrbOverlay] = None, hub: Optional[FlowLocalHub] = None):
        self.orb = orb
        self.hub = hub
        self.model = WhisperModel(
            MODEL_SIZE,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            download_root=None,
        )

        def warm_llm():
            try:
                LLMRefiner.refine("Test audio")
            except Exception:
                pass
        threading.Thread(target=warm_llm, daemon=True).start()

        self.audio_frames = []
        self.is_recording = False
        self.is_locked = False
        self.is_paused = False
        self.last_press_time = 0.0
        self.lock = threading.Lock()
        self.live_rms = 0.0

        self.ring_buffer = collections.deque(maxlen=PRE_BUFFER_CHUNKS)

        self.dev_index, self.sample_rate = find_best_input_device()
        self.use_decimation = (self.sample_rate == 48000)
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

    def change_device(self, new_device_idx: int):
        self.dev_index = new_device_idx
        self._start_stream()

    def handle_key_press(self):
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

        if self.orb:
            self.orb.feed_audio(chunk)

        if self.hub and self.hub.isVisible():
            self.hub.update_live_volume(min(1.0, rms * 10.0))

        with self.lock:
            if self.is_recording:
                self.audio_frames.append(chunk)
            else:
                self.ring_buffer.append(chunk)

    def start_recording(self, locked=False):
        if self.is_paused:
            return

        with self.lock:
            if self.is_recording:
                return
            self.audio_frames = list(self.ring_buffer)
            self.is_recording = True
            self.is_locked = locked

            if self.orb:
                self.orb.show_listening()

            if locked:
                threading.Thread(target=sound_locked, daemon=True).start()
            else:
                threading.Thread(target=sound_start, daemon=True).start()

    def stop_recording_and_process(self):
        time.sleep(0.12)

        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            self.is_locked = False
            captured = list(self.audio_frames)
            self.audio_frames = []

        if self.orb:
            self.orb.show_whisper()

        if not captured:
            if self.orb:
                self.orb.hide()
            return

        raw_audio = np.concatenate(captured, axis=0).flatten()

        if self.use_decimation:
            audio_16k = signal.decimate(raw_audio, DECIMATE_FACTOR).astype(np.float32)
        else:
            audio_16k = raw_audio

        duration_s = len(audio_16k) / WHISPER_SAMPLE_RATE

        if duration_s < 0.25:
            if self.orb:
                self.orb.hide()
            return

        threading.Thread(target=self._transcribe_and_inject, args=(audio_16k, duration_s), daemon=True).start()

    def _transcribe_and_inject(self, audio_data: np.ndarray, duration_s: float):
        t0 = time.time()
        try:
            # Biasing Whisper da database vocabolario
            vocab_prompt = db.get_vocabulary_prompt_string()
            if vocab_prompt:
                initial_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} {vocab_prompt}"
            else:
                initial_prompt = DEFAULT_WHISPER_INITIAL_PROMPT

            segments, info = self.model.transcribe(
                audio_data,
                beam_size=5,
                language="it",
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=350,
                    speech_pad_ms=200,
                ),
                initial_prompt=initial_prompt,
            )

            text_parts = []
            for seg in segments:
                text = seg.text.strip()
                if seg.no_speech_prob > 0.75:
                    continue
                if text:
                    text_parts.append(text)

            raw_text = " ".join(text_parts).strip()

            if not raw_text:
                if self.orb:
                    self.orb.hide()
                return

            if self.orb:
                self.orb.show_qwen()

            final_text = LLMRefiner.refine(raw_text)

            inference_ms = int((time.time() - t0) * 1000)

            # Iniezione testo e feedback
            WindowsInjector.paste_text(final_text)
            sound_done()

            # Salvataggio nel database locale SQLite (WAL)
            db.add_transcription(
                raw_text=raw_text,
                refined_text=final_text,
                duration_s=duration_s,
                inference_ms=inference_ms,
            )

        except Exception:
            pass
        finally:
            if self.orb:
                self.orb.hide()

