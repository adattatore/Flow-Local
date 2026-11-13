"""
WhisWakeListener — Monitoraggio Audio Hands-Free e Rilevamento Wake Word per FlowLocal
Esegue il monitoraggio continuo del microfono su CPU (0% GPU durante il silenzio)
e attiva la trascrizione Faster-Whisper solo al rilevamento di voce seguito da 0.9s di silenzio.
"""

import time
import re
import queue
import collections
import threading
from typing import Optional, Callable, Any

import numpy as np
import sounddevice as sd
from scipy import signal
from PyQt6.QtCore import QThread, pyqtSignal, QObject

# Pattern di riconoscimento wake-word case-insensitive
# Supporta: "Whis", "Ehi Whis", "Hey Whis", "Ok Whis", "Ciao Whis", "Allora Whis", "Senti Whis", "Wis", "Wiz", "Whizz", "Uis", "Quiz", "Luis", "Miss"
WAKE_WORD_PATTERN = re.compile(
    r"^[\s,.;:!?]*(?:ehi|hey|ok|ciao|allora|senti)?[\s,.;:!?]*(?:whis|wis|wiz|whizz|uis|quiz|luis|miss)\b[\s,.;:!?]*(.*)",
    re.IGNORECASE
)

DEFAULT_RMS_THRESHOLD = 0.009     # Soglia RMS calibrata per voce normale a distanza da scrivania
DEFAULT_SILENCE_DURATION = 0.9    # 0.9s di silenzio continuo per considerare terminata la frase
DEFAULT_MAX_DURATION = 10.0       # Durata massima di ascolto continuo (10s)
DEFAULT_MIN_SPEECH_DURATION = 0.35 # Durata minima per evitare falsi positivi su singoli rumori
PRE_BUFFER_CHUNKS = 6             # ~300ms di pre-buffer circolare per non tagliare l'attacco di "Whis"


def is_pure_wake_phrase(cmd: str) -> bool:
    """Restituisce True se la stringa contiene solo ripetizioni della wake word (es. 'ok Whis', 'Whis Whis')."""
    if not cmd or not cmd.strip():
        return True
    cleaned = re.sub(
        r'\b(?:ehi|hey|ok|ciao|allora|senti)?[\s,.;:!?]*(?:whis|wis|wiz|whizz|uis|quiz|luis|miss)\b',
        '',
        cmd,
        flags=re.IGNORECASE
    ).strip(' ,.;:!?')
    return len(cleaned) == 0


def _play_wake_feedback():
    """Feedback acustico non bloccante al rilevamento della wake word gestito da AudioFeedbackManager."""
    try:
        from core.audio_feedback import audio_feedback
        audio_feedback.play_wake_feedback()
    except Exception:
        pass


STATE_IDLE = 0
STATE_WAITING_FOR_COMMAND = 1


class WhisWakeListener(QThread):
    """
    QThread per il rilevamento hands-free della wake-word 'Whis' con macchina a stati a 2 Fasi (stile Alexa).
    
    Caratteristiche:
    - Monitoraggio audio su CPU calcolando solo l'RMS (0% uso GPU in assenza di parlato).
    - Modalità Alexa a 2 Fasi:
      * Caso A: Se l'utente dice solo 'Whis' (o 'Ehi Whis') e si ferma, emette wake_activated(),
        apre WhisHUD in stato 'In ascolto' ed entra in WAITING_FOR_COMMAND per 7 secondi.
      * Caso B: Se l'utente dice subito la frase completa ('Whis, cosa sto guardando?'),
        apre subito l'HUD ed emette wake_word_detected(command).
    - Timeout a 7 secondi: se dopo 'Whis' l'utente non dice nulla, chiude l'HUD senza allucinare.
    - Metodo thread-safe `set_paused(bool)` per disattivare istantaneamente l'ascolto su Caps Lock.
    """

    wake_activated = pyqtSignal()            # Ehi Whis senza comando immediato (apre HUD in ascolto)
    followup_activated = pyqtSignal(str)     # Emesso per follow-up interattivo (mantiene la domanda a schermo)
    wake_word_detected = pyqtSignal(str)     # Comando estratto da inviare a WhisAgent
    command_timeout = pyqtSignal()           # 7s trascorsi senza parlato in Fase 2 (chiude HUD)
    speech_started = pyqtSignal()            # Voce rilevata sopra soglia
    speech_ended = pyqtSignal()              # Silenzio rilevato, inizio analisi
    state_changed = pyqtSignal(bool)         # True se in ascolto attivo, False se in pausa
    status_message = pyqtSignal(str)        # Messaggio di stato per l'Hub

    def __init__(
        self,
        whisper_instance: Any = None,
        rms_threshold: float = DEFAULT_RMS_THRESHOLD,
        silence_duration: float = DEFAULT_SILENCE_DURATION,
        max_duration: float = DEFAULT_MAX_DURATION,
        sample_rate: int = 16000,
        device_index: Optional[int] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.whisper_instance = whisper_instance
        self.rms_threshold = float(rms_threshold)
        self.noise_floor = 0.003
        self.silence_duration = float(silence_duration)
        self.max_duration = float(max_duration)
        self.sample_rate = int(sample_rate)
        self.device_index = device_index

        self.listener_state = STATE_IDLE
        self.waiting_command_start_time = 0.0
        self.waiting_command_timeout = 7.0

        self._is_paused: bool = False
        self._running: bool = False
        self._lock = threading.Lock()
        self._audio_queue: queue.Queue = queue.Queue(maxsize=300)
        self._stream: Optional[sd.InputStream] = None

    @property
    def is_paused(self) -> bool:
        """Restituisce lo stato di pausa del listener."""
        with self._lock:
            return self._is_paused

    @is_paused.setter
    def is_paused(self, value: bool):
        self.set_paused(value)

    def set_paused(self, paused: bool):
        """
        Pone istantaneamente il listener in pausa o lo riattiva.
        Rilascia lo stream audio durante la pausa per zero conflitti con Caps Lock.
        """
        with self._lock:
            self._is_paused = bool(paused)
            if self._is_paused:
                # Svuota immediatamente la coda audio residua
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
                try:
                    if self._stream and self._stream.active:
                        self._stream.stop()
                except Exception:
                    pass
            else:
                try:
                    if self._stream and not self._stream.active:
                        self._stream.start()
                except Exception:
                    pass
        self.state_changed.emit(not paused)

    def set_whisper_instance(self, whisper_instance: Any):
        """Imposta o aggiorna l'istanza/funzione Faster-Whisper."""
        with self._lock:
            self.whisper_instance = whisper_instance

    def set_rms_threshold(self, threshold: float):
        """Regola dinamicamente la soglia di energia RMS."""
        with self._lock:
            self.rms_threshold = max(0.001, float(threshold))

    def open_command_listening(self, prompt_status: str = "🎙️ In ascolto... dimmi il titolo!", timeout: float = 12.0):
        """Apre direttamente la modalità di ascolto comandi senza richiedere la wake-word (Follow-up interattivo)."""
        with self._lock:
            self._is_paused = False
            self.listener_state = STATE_WAITING_FOR_COMMAND
            self.waiting_command_start_time = time.time()
            self.waiting_command_timeout = timeout
        if not self.isRunning():
            self.start()
        self.followup_activated.emit(prompt_status)
        self.status_message.emit(prompt_status)

    def stop(self):
        """Interrompe il thread in modo pulito e chiude il dispositivo audio."""
        self._running = False
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        except Exception:
            pass
        self.wait(1200)

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any):
        """Callback audio SoundDevice ad altissima efficienza (CPU-only)."""
        if self._is_paused or not self._running:
            return

        try:
            chunk = indata.copy()
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            pass

    def run(self):
        """Ciclo di vita principale del thread di monitoraggio."""
        self._running = True
        block_size = int(self.sample_rate * 0.05)  # Frame da 50ms

        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block_size,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            # Fallback a samplerate 48000 se 16000 non e supportato nativamente
            try:
                self.sample_rate = 48000
                block_size = int(self.sample_rate * 0.05)
                self._stream = sd.InputStream(
                    device=self.device_index,
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=block_size,
                    callback=self._audio_callback,
                )
                self._stream.start()
            except Exception as e2:
                print(f"[WhisWakeListener] Impossibile avviare InputStream audio: {e2}")
                self._running = False
                return

        ring_buffer = collections.deque(maxlen=PRE_BUFFER_CHUNKS)
        speech_chunks = []
        is_speaking = False
        speech_start_time = 0.0
        last_speech_time = 0.0

        while self._running:
            # Controllo timeout Fase 2 (Stile Alexa: attesa comando dopo 'Whis')
            current_time = time.time()
            if self.listener_state == STATE_WAITING_FOR_COMMAND:
                timeout_limit = getattr(self, "waiting_command_timeout", 7.0) or 7.0
                if not is_speaking and (current_time - self.waiting_command_start_time > timeout_limit):
                    self.listener_state = STATE_IDLE
                    self.command_timeout.emit()
                    self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")

            try:
                chunk = self._audio_queue.get(timeout=0.08)
            except queue.Empty:
                continue

            # Se e in pausa, azzera qualsiasi stato di accumulo
            if self._is_paused:
                if is_speaking:
                    is_speaking = False
                    speech_chunks.clear()
                    ring_buffer.clear()
                if self.listener_state == STATE_WAITING_FOR_COMMAND:
                    self.listener_state = STATE_IDLE
                    self.command_timeout.emit()
                continue

            # Calcolo energia RMS puramente su CPU (0% GPU)
            rms = float(np.sqrt(np.mean(chunk**2)))
            current_time = time.time()

            if rms >= self.rms_threshold:
                # Parlato rilevato
                if not is_speaking:
                    is_speaking = True
                    speech_start_time = current_time
                    speech_chunks = list(ring_buffer)
                    self.speech_started.emit()
                    if self.listener_state == STATE_WAITING_FOR_COMMAND:
                        self.status_message.emit("🎙️ Ti sto ascoltando...")
                    else:
                        self.status_message.emit("🎙️ Mani Libere: rilevato parlato, verifica wake-word...")

                speech_chunks.append(chunk)
                last_speech_time = current_time
            else:
                # Silenzio: adatta gradualmente la soglia al rumore della stanza (voce normale a distanza)
                self.noise_floor = 0.98 * self.noise_floor + 0.02 * rms
                self.rms_threshold = max(0.007, min(0.020, self.noise_floor * 2.2))

                if not is_speaking:
                    ring_buffer.append(chunk)
                else:
                    speech_chunks.append(chunk)
                    silence_elapsed = current_time - last_speech_time
                    total_elapsed = current_time - speech_start_time

                    # Condizione di fine frase: 0.9s di silenzio o durata massima raggiunta
                    if silence_elapsed >= self.silence_duration or total_elapsed >= self.max_duration:
                        is_speaking = False
                        self.speech_ended.emit()

                        captured = list(speech_chunks)
                        speech_chunks.clear()
                        ring_buffer.clear()

                        if captured and not self._is_paused:
                            self._process_utterance(captured)

        # Cleanup alla chiusura
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        except Exception:
            pass

    def _process_utterance(self, chunks: list):
        """Converte l'audio accumulato e gestisce la logica a 2 Fasi (stile Alexa)."""
        try:
            raw_audio = np.concatenate(chunks, axis=0).flatten()

            # Ricampionamento a 16000Hz se l'audio e a 48000Hz
            if self.sample_rate == 48000:
                audio_16k = signal.decimate(raw_audio, 3).astype(np.float32)
            else:
                audio_16k = raw_audio.astype(np.float32)

            duration_s = len(audio_16k) / 16000.0
            if duration_s < DEFAULT_MIN_SPEECH_DURATION:
                # Troppo breve per contenere una wake word o comando valido
                return

            if self._is_paused:
                return

            # Esecuzione trascrizione via Faster-Whisper
            text = self._transcribe_audio(audio_16k)
            if not text or self._is_paused:
                if self.listener_state == STATE_WAITING_FOR_COMMAND:
                    self.listener_state = STATE_IDLE
                    self.command_timeout.emit()
                    self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
                return

            if self.listener_state == STATE_WAITING_FOR_COMMAND:
                # FASE 2: L'utente sta rispondendo all'ascolto aperto di Whis
                self.listener_state = STATE_IDLE
                # Se l'utente ha ripetuto per caso "Whis ..." togliamo il prefisso
                match = WAKE_WORD_PATTERN.match(text)
                raw_command = match.group(1).strip() if match else text.strip()
                command = "" if is_pure_wake_phrase(raw_command) else raw_command

                if len(command) >= 2:
                    self.wake_word_detected.emit(command)
                    self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
                else:
                    self.command_timeout.emit()
                    self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
                return

            # FASE 1 (STATE_IDLE): Verifica corrispondenza con la wake-word
            match = WAKE_WORD_PATTERN.match(text)
            if match:
                raw_cmd = match.group(1).strip()
                # Se il comando contiene solo ripetizioni della wake word (es. loop Whisper), scartale per entrare in ascolto
                command = "" if is_pure_wake_phrase(raw_cmd) else raw_cmd

                # Feedback acustico discreto non bloccante
                threading.Thread(target=_play_wake_feedback, daemon=True).start()

                # CASO A: L'utente dice solo "Whis" (o "Ehi Whis") e si ferma (< 2 parole o < 4 caratteri o vuoto)
                if not command or len(command.split()) < 2 or len(command) < 4:
                    self.listener_state = STATE_WAITING_FOR_COMMAND
                    self.waiting_command_start_time = time.time()
                    self.wake_activated.emit()
                    self.status_message.emit("🎙️ In ascolto... Dimmi pure!")
                else:
                    # CASO B: L'utente dice già la frase intera ("Whis, cosa sto vedendo sullo schermo?")
                    self.wake_word_detected.emit(command)
                    self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")
            else:
                # Se non inizia con "Whis", scarta silenziosamente l'audio e ripristina status
                self.status_message.emit("🟢 Pronto — Mani Libere Attive (Pronuncia \"Whis...\")")

        except Exception as e:
            print(f"[WhisWakeListener] Errore elaborazione segmento audio: {e}")

    def _transcribe_audio(self, audio_16k: np.ndarray) -> str:
        """Invia l'audio all'istanza o funzione Faster-Whisper registrata."""
        with self._lock:
            target = self.whisper_instance

        if target is None:
            return ""

        try:
            # 1. Se e una funzione callable diretta
            if callable(target):
                res = target(audio_16k)
                if isinstance(res, str):
                    raw = res.strip()
                elif hasattr(res, "__iter__"):
                    raw = " ".join([getattr(s, "text", str(s)).strip() for s in res]).strip()
                else:
                    raw = ""
                return re.sub(r'\bsullo scarbo\b', 'sullo schermo', raw, flags=re.IGNORECASE).strip()

            # 2. Se e un oggetto con metodo transcribe (WhisperModel)
            model_to_call = None
            if hasattr(target, "transcribe") and callable(target.transcribe):
                model_to_call = target
            elif hasattr(target, "model") and hasattr(target.model, "transcribe") and callable(target.model.transcribe):
                model_to_call = target.model

            if model_to_call:
                whisper_prompt = "Whis."
                try:
                    from core.ai_pipeline import DEFAULT_WHISPER_INITIAL_PROMPT
                    from database.db_manager import db
                    db_prompt = db.get_whisper_initial_prompt(limit=40)
                    whisper_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} {db_prompt}".strip() if db_prompt else DEFAULT_WHISPER_INITIAL_PROMPT
                except Exception:
                    pass

                transcribe_kwargs = dict(
                    beam_size=5,
                    language="it",
                    temperature=0.0,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.6,
                    initial_prompt=whisper_prompt,
                )
                segments, info = model_to_call.transcribe(
                    audio_16k,
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=500,
                        speech_pad_ms=200,
                    ),
                    **transcribe_kwargs,
                )
                segments = list(segments)
                if not segments:
                    segments, _ = model_to_call.transcribe(
                        audio_16k,
                        vad_filter=False,
                        **transcribe_kwargs,
                    )
                    segments = list(segments)

                parts = []
                for seg in segments:
                    if getattr(seg, "no_speech_prob", 0.0) < 0.60:
                        t = seg.text.strip()
                        if t:
                            parts.append(t)
                raw_text = " ".join(parts).strip()
                # Deduplica loop di ripetizione (es. "ok Whis, ok Whis, ok Whis" -> "ok Whis")
                raw_text = re.sub(r'(?i)\b((?:(?:ehi|hey|ok|ciao|allora|senti)\s+)?whis)\b(?:[\s,.;:!?]+\1\b)+', r'\1', raw_text).strip()
                cleaned = re.sub(r'\bsullo scarbo\b', 'sullo schermo', raw_text, flags=re.IGNORECASE).strip()
                try:
                    from core.ai_pipeline import normalize_phonetic_variants
                    cleaned = normalize_phonetic_variants(cleaned)
                except Exception:
                    pass
                return cleaned.strip()

        except Exception as e:
            print(f"[WhisWakeListener] Errore durante la trascrizione Faster-Whisper: {e}")

        return ""

