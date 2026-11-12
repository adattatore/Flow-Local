"""
core/audio_feedback.py
Manager centralizzato per il feedback acustico e vocale di Whis in FlowLocal.

Supporta 3 modalità salvate in SQLite (app_settings):
1. 'voce': Lettura neurale naturale italiana (edge-tts con fallback pyttsx3) + Soft Chime al risveglio.
2. 'suono_morbido': Micro-accordo sinusoidale generato in RAM (528Hz -> 660Hz) senza bip metallici.
3. 'muto': Silenzio totale (nessun suono al risveglio, nessuna lettura vocale).
"""

import io
import os
import re
import time
import wave
import asyncio
import tempfile
import threading
from typing import Optional, Callable, List

import numpy as np
import winsound
import pygame
import edge_tts
import pyttsx3

from database import db

# Costanti modalità
MODE_VOICE = "voce"
MODE_SOFT_CHIME = "suono_morbido"
MODE_MUTE = "muto"

VALID_MODES = [MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE]
SETTING_KEY = "whis_audio_feedback_mode"
DEFAULT_MODE = MODE_MUTE

# Profili vocali neurali edge-tts
VOICE_PRESETS = {
    "giuseppe": {
        "label": "Giuseppe (Giovane & Rapido)",
        "voice": "it-IT-GiuseppeNeural",
        "rate": "+12%",
        "pitch": "-1Hz",
    },
    "diego": {
        "label": "Diego (Stile JARVIS Profondo)",
        "voice": "it-IT-DiegoNeural",
        "rate": "+10%",
        "pitch": "-4Hz",
    },
    "isabella": {
        "label": "Isabella (Brillante & Chiara)",
        "voice": "it-IT-IsabellaNeural",
        "rate": "+10%",
        "pitch": "+0Hz",
    },
    "elsa": {
        "label": "Elsa (Calda & Morbida)",
        "voice": "it-IT-ElsaNeural",
        "rate": "+8%",
        "pitch": "+0Hz",
    },
}

SETTING_VOICE_KEY = "whis_voice_profile"
DEFAULT_VOICE_KEY = "giuseppe"
TEST_PHRASE = "Ciao Alessandro, sono Whis, pronto ad assisterti."


def _generate_soft_chime_wav(
    duration_s: float = 0.13,
    f_start: float = 528.0,
    f_end: float = 660.0,
    volume: float = 0.12,
    sample_rate: int = 44100
) -> bytes:
    """
    Genera in memoria RAM un micro-accordo sinusoidale morbido con inviluppo esponenziale
    fade-in/fade-out (528Hz -> 660Hz, 130ms, volume 0.12) per sostituire i vecchi bip metallici.
    """
    n_samples = int(sample_rate * duration_s)
    t = np.linspace(0, duration_s, n_samples, endpoint=False)

    # Glide armonico morbido tra 528Hz e 660Hz combinato con la fondamentale di supporto
    phase = 2 * np.pi * (f_start * t + 0.5 * (f_end - f_start) * (t**2 / duration_s))
    tone = 0.75 * np.sin(phase) + 0.25 * np.sin(2 * np.pi * f_end * t)

    # Inviluppo esponenziale: 15% fade-in morbido a campana, 85% decadimento naturale
    fade_in_len = max(1, int(n_samples * 0.15))
    fade_out_len = n_samples - fade_in_len
    env_in = np.sin(np.linspace(0, np.pi / 2, fade_in_len))
    env_out = np.exp(-3.5 * np.linspace(0, 1, fade_out_len))
    envelope = np.concatenate([env_in, env_out])

    audio = tone * envelope * volume
    audio_int16 = np.int16(np.clip(audio, -1.0, 1.0) * 32767)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


class AudioFeedbackManager:
    """Singleton/Manager per feedback acustico e sintesi vocale di Whis."""

    _instance: Optional["AudioFeedbackManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._chime_bytes = _generate_soft_chime_wav()
        self._stop_event = threading.Event()
        self._current_worker: Optional[threading.Thread] = None
        self._playback_lock = threading.Lock()
        self._listeners: List[Callable[[str], None]] = []
        self._voice_listeners: List[Callable[[str], None]] = []

        # Inizializza mixer pygame per audio senza finestre
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception:
            pass

        # Migrazione / enforce default muto per evitare TTS innaturale
        try:
            if db.get_setting("whis_audio_silent_default_migrated", "0") != "1":
                db.set_setting(SETTING_KEY, MODE_MUTE)
                db.set_setting("whis_audio_silent_default_migrated", "1")
        except Exception:
            pass

    def get_mode(self) -> str:
        """Restituisce la modalità corrente salvata in SQLite ('voce', 'suono_morbido', 'muto')."""
        try:
            saved = db.get_setting(SETTING_KEY, DEFAULT_MODE)
            if saved in VALID_MODES:
                return saved
        except Exception:
            pass
        return DEFAULT_MODE

    def set_mode(self, mode: str):
        """Imposta e persiste la modalità audio di Whis, notificando i listener registrati."""
        if mode not in VALID_MODES:
            mode = DEFAULT_MODE
        try:
            db.set_setting(SETTING_KEY, mode)
        except Exception:
            pass

        # Se passa a muto o altro mentre sta parlando, interrompi subito la voce
        if mode != MODE_VOICE:
            self.stop()

        self._notify_listeners(mode)

    def add_mode_listener(self, cb: Callable[[str], None]):
        """Registra una funzione di callback da eseguire al cambio modalità."""
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_mode_listener(self, cb: Callable[[str], None]):
        """Rimuove un listener precedentemente registrato."""
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _notify_listeners(self, mode: str):
        for cb in list(self._listeners):
            try:
                cb(mode)
            except Exception:
                pass

    def get_voice_profile(self) -> str:
        """Restituisce la chiave del profilo vocale corrente salvata in SQLite ('giuseppe', 'diego', ecc.)."""
        try:
            saved = db.get_setting(SETTING_VOICE_KEY, DEFAULT_VOICE_KEY)
            if saved in VOICE_PRESETS:
                return saved
        except Exception:
            pass
        return DEFAULT_VOICE_KEY

    def set_voice_profile(self, profile_key: str):
        """Imposta e persiste il profilo vocale di Whis, notificando i listener registrati."""
        if profile_key not in VOICE_PRESETS:
            profile_key = DEFAULT_VOICE_KEY
        try:
            db.set_setting(SETTING_VOICE_KEY, profile_key)
        except Exception:
            pass
        self._notify_voice_listeners(profile_key)

    def get_voice_config(self, profile_key: Optional[str] = None) -> dict:
        """Restituisce la configurazione (voice, rate, pitch) per il profilo specificato o attivo."""
        key = profile_key or self.get_voice_profile()
        return VOICE_PRESETS.get(key, VOICE_PRESETS[DEFAULT_VOICE_KEY])

    def add_voice_listener(self, cb: Callable[[str], None]):
        """Registra un listener per gli aggiornamenti del profilo vocale."""
        if cb not in self._voice_listeners:
            self._voice_listeners.append(cb)

    def remove_voice_listener(self, cb: Callable[[str], None]):
        """Rimuove un listener precedentemente registrato."""
        if cb in self._voice_listeners:
            self._voice_listeners.remove(cb)

    def _notify_voice_listeners(self, profile_key: str):
        for cb in list(self._voice_listeners):
            try:
                cb(profile_key)
            except Exception:
                pass

    def preview_voice(self, profile_key: Optional[str] = None):
        """
        Fa pronunciare la frase di prova ("Ciao Alessandro, sono Whis, pronto ad assisterti.")
        con il profilo vocale specificato o attivo, salvando la selezione.
        """
        if profile_key and profile_key in VOICE_PRESETS:
            self.set_voice_profile(profile_key)

        self.stop()
        worker = threading.Thread(
            target=self._tts_worker,
            args=(TEST_PHRASE,),
            daemon=True,
            name="WhisTTSPreviewWorker"
        )
        self._current_worker = worker
        worker.start()

    def play_wake_feedback(self):
        """
        Feedback acustico al risveglio della wake word di Whis:
        - 'muto': nessun suono
        - 'suono_morbido' o 'voce': morbido micro-accordo sinusoidale asincrono in RAM
        """
        mode = self.get_mode()
        if mode == MODE_MUTE:
            return

        try:
            winsound.PlaySound(self._chime_bytes, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            pass

    @staticmethod
    def clean_text_for_speech(text: str) -> str:
        """
        Pulisce il testo prima della lettura vocale:
        - Rimuove codice Markdown ```...``` e `...`
        - Rimuove tag [FONTE: ...] o [Fonte: ...]
        - Rimuove link markdown [testo](url) mantenendo solo testo
        - Rimuove intestazioni markdown (#, ##)
        - Rimuove elenchi puntati (*, -, +)
        - Rimuove asterischi e tilde di formattazione
        - Rimuove delimitatori LaTeX ($...$)
        - Rimuove emoji unicode
        """
        if not text:
            return ""

        # 1. Rimuovi blocchi di codice completi ```...```
        cleaned = re.sub(r"```[\s\S]*?```", "", text)

        # 2. Rimuovi tag di citazione/fonte
        cleaned = re.sub(r"\[FONTE:.*?\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\[Fonte:.*?\]", "", cleaned, flags=re.IGNORECASE)

        # 3. Rimuovi link markdown [testo](link) -> testo
        cleaned = re.sub(r"\[(.*?)\]\([^)]*\)", r"\1", cleaned)

        # 4. Rimuovi codice inline `codice`
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)

        # 5. Rimuovi intestazioni markdown (#, ##, ...) a inizio riga
        cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned, flags=re.MULTILINE)

        # 6. Rimuovi simboli punto elenco -, *, + a inizio riga
        cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)

        # 7. Rimuovi asterischi e tilde
        cleaned = re.sub(r"[*~_]", "", cleaned)

        # 8. Sostituisci formule LaTeX $...$ con il contenuto
        cleaned = re.sub(r"\$([^$]+)\$", r"\1", cleaned)

        # 9. Rimuovi emoji
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF"
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "\U0001F900-\U0001F9FF"
            "\U0001FA70-\U0001FAFF"
            "]+",
            flags=re.UNICODE
        )
        cleaned = emoji_pattern.sub("", cleaned)

        # 10. Normalizza spazi
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return cleaned.strip()

    def is_speaking(self) -> bool:
        """Restituisce True se la riproduzione vocale è attualmente attiva."""
        try:
            if pygame.mixer.get_init():
                return bool(pygame.mixer.music.get_busy())
        except Exception:
            pass
        return bool(self._current_worker and self._current_worker.is_alive())

    def stop(self):
        """Interrompe immediatamente qualsiasi audio o sintesi vocale in corso."""
        self._stop_event.set()
        try:
            if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
        except Exception:
            pass

    def speak_response(self, text: str):
        """
        Avvia la sintesi vocale in un thread asincrono non bloccante.
        Se la modalità non è 'voce', ignora la richiesta.
        """
        if self.get_mode() != MODE_VOICE:
            return

        clean_text = self.clean_text_for_speech(text)
        if not clean_text:
            return

        # Interrompi eventuale parlato precedente
        self.stop()

        worker = threading.Thread(
            target=self._tts_worker,
            args=(clean_text,),
            daemon=True,
            name="WhisTTSWorker"
        )
        self._current_worker = worker
        worker.start()

    def _tts_worker(self, text: str):
        """Worker in background che tenta prima edge-tts e poi fallback su pyttsx3 offline."""
        with self._playback_lock:
            self._stop_event.clear()

            # 1. Tentativo con Edge-TTS (voce neurale online di alta qualità)
            success = self._try_edge_tts(text)
            if success or self._stop_event.is_set():
                return

            # 2. Fallback offline con pyttsx3 (SAPI5 su Windows)
            print("[AudioFeedback] Fallback automatico su sintesi vocale offline pyttsx3...")
            self._try_pyttsx3(text)

    def _try_edge_tts(self, text: str) -> bool:
        """Esegue sintesi vocale neurale con edge-tts utilizzando il profilo attivo."""
        try:
            cfg = self.get_voice_config()
            voice_id = cfg.get("voice", "it-IT-GiuseppeNeural")
            rate = cfg.get("rate", "+12%")
            pitch = cfg.get("pitch", "-1Hz")

            async def _stream():
                comm = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
                chunks = []
                async for chunk in comm.stream():
                    if self._stop_event.is_set():
                        return None
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
                return b"".join(chunks)

            # Esegui in un nuovo event loop per il thread corrente
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                audio_bytes = loop.run_until_complete(_stream())
            finally:
                loop.close()

            if self._stop_event.is_set() or not audio_bytes:
                return False

            self._play_audio_stream(audio_bytes)
            return True
        except Exception as e:
            print(f"[AudioFeedback] edge-tts non disponibile ({e})")
            return False

    def _try_pyttsx3(self, text: str) -> bool:
        """Esegue sintesi vocale offline nativa con pyttsx3 e voce italiana."""
        tmp_wav = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp_wav = tf.name

            engine = pyttsx3.init()
            # Cerca voce italiana installata nel sistema Windows
            for v in engine.getProperty("voices"):
                if "it" in v.id.lower() or "elsa" in v.name.lower():
                    engine.setProperty("voice", v.id)
                    break
            engine.setProperty("rate", 175)
            engine.save_to_file(text, tmp_wav)
            engine.runAndWait()
            del engine

            if self._stop_event.is_set():
                return False

            if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
                with open(tmp_wav, "rb") as f:
                    data = f.read()
                self._play_audio_stream(data)
                return True
            return False
        except Exception as e:
            print(f"[AudioFeedback] Errore sintesi vocale pyttsx3: {e}")
            return False
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass

    def _play_audio_stream(self, audio_bytes: bytes):
        """Riproduce i byte audio con pygame.mixer.music consentendo lo stop immediato."""
        if not pygame.mixer.get_init():
            try:
                pygame.mixer.init()
            except Exception:
                return

        try:
            bio = io.BytesIO(audio_bytes)
            pygame.mixer.music.load(bio)
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy():
                if self._stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.04)
        except Exception as e:
            print(f"[AudioFeedback] Errore riproduzione audio: {e}")


# Istanza singleton globale
audio_feedback = AudioFeedbackManager()
