"""
core/audio_ducker.py
Smart Audio-Ducking su Spotify e App Multimediali tramite pycaw / WASAPI.

Caratteristiche:
- Esecuzione asincrona a latenza zero (0ms di blocco sul thread di dettatura) in un
  worker thread dedicato inizializzato con comtypes.CoInitialize().
- Rileva le sessioni audio attive di app multimediali (Spotify.exe, browser, player musicali/video),
  escludendo sempre il processo di FlowLocal stesso.
- Fade-out fluido in 120ms fino al 15% del volume originale all'inizio della registrazione vocale
  (Push-to-Talk FlowType o ascolto attivo di Whis).
- Fade-in morbido in 200ms al termine della registrazione vocale per ripristinare il volume originale.
- Gestione completamente fail-safe di qualsiasi eccezione COM/WASAPI.
- Stato attivo/disattivo persistito in SQLite (app_settings: 'auto_ducking_enabled', attivo di default).
"""

import os
import time
import queue
import atexit
import threading
from typing import Dict, Any, Optional, List, Callable

from database import db

SETTING_AUTO_DUCKING_KEY = "auto_ducking_enabled"
DEFAULT_AUTO_DUCKING_ENABLED = True

DUCK_RATIO = 0.15          # Riduce al 15% del volume originale
FADE_OUT_DURATION_S = 0.120  # 120ms fade-out
FADE_OUT_STEPS = 6
FADE_IN_DURATION_S = 0.200   # 200ms fade-in
FADE_IN_STEPS = 8

# Processi multimediali e browser target per l'Auto-Ducking
MEDIA_PROCESS_NAMES = {
    "spotify.exe",
    "musicbee.exe",
    "foobar2000.exe",
    "aimp.exe",
    "itunes.exe",
    "applemusic.exe",
    "amplibraryagent.exe",
    "tidal.exe",
    "deezer.exe",
    "qobuz.exe",
    "winamp.exe",
    "dopamine.exe",
    "vlc.exe",
    "mpv.exe",
    "mpc-hc64.exe",
    "mpc-hc.exe",
    "mpc-be64.exe",
    "mpc-be.exe",
    "potplayermini64.exe",
    "potplayer.exe",
    "wmplayer.exe",
    "microsoft.media.player.exe",
    "zune.exe",
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
    "waterfox.exe",
    "zen.exe",
}

MEDIA_NAME_KEYWORDS = ("spotify", "music", "vlc", "mpv", "player", "foobar", "tidal", "deezer")


class _SavedSessionEntry:
    """Rappresenta una sessione WASAPI salvata prima del ducking."""

    def __init__(self, key: str, volume_interface: Any, original_volume: float, process_name: str):
        self.key = key
        self.volume_interface = volume_interface
        self.original_volume = float(original_volume)
        self.current_volume = float(original_volume)
        self.process_name = process_name


class AudioDucker:
    """
    Gestore leggero e thread-safe per lo Smart Audio-Ducking su Windows (pycaw / WASAPI).
    Opera interamente in un thread COM separato (con comtypes.CoInitialize()) per non
    introdurre mai alcun ritardo nell'avvio/arresto della dettatura.
    """

    _instance: Optional["AudioDucker"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._own_pid = os.getpid()
        self._lock = threading.Lock()
        self._cmd_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._saved_sessions: Dict[str, _SavedSessionEntry] = {}
        self._is_ducked = False
        self._listeners: List[Callable[[bool], None]] = []

        # Carica impostazione persistita (attivo di default)
        self._enabled: bool = self._load_enabled_from_db()

        # Avvia il worker thread COM dedicato
        self._worker_thread = threading.Thread(
            target=self._com_worker_loop,
            daemon=True,
            name="FlowLocalAudioDuckerCOM",
        )
        self._worker_thread.start()

        atexit.register(self.restore_immediately)

    def _load_enabled_from_db(self) -> bool:
        try:
            val = db.get_setting(SETTING_AUTO_DUCKING_KEY, None)
            if val is None:
                db.set_setting(SETTING_AUTO_DUCKING_KEY, "1")
                return True
            return str(val).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return DEFAULT_AUTO_DUCKING_ENABLED

    def is_enabled(self) -> bool:
        """Restituisce True se l'Auto-Ducking è abilitato nelle impostazioni."""
        return bool(self._enabled)

    def set_enabled(self, enabled: bool):
        """Abilita o disabilita l'Auto-Ducking salvando la preferenza in SQLite."""
        new_state = bool(enabled)
        self._enabled = new_state
        try:
            db.set_setting(SETTING_AUTO_DUCKING_KEY, "1" if new_state else "0")
        except Exception:
            pass

        if not new_state:
            self.unduck()

        for cb in list(self._listeners):
            try:
                cb(new_state)
            except Exception:
                pass

    def add_listener(self, callback: Callable[[bool], None]):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[bool], None]):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def duck(self):
        """
        Richiede l'abbassamento fluido (fade-out 120ms al 15%) delle sessioni multimediali attive.
        Non bloccante (0ms sul chiamante).
        """
        if not self._enabled:
            return
        try:
            self._cmd_queue.put_nowait("duck")
        except Exception:
            pass

    def unduck(self):
        """
        Richiede il ripristino fluido (fade-in 200ms al 100% del volume originale).
        Non bloccante (0ms sul chiamante).
        """
        try:
            self._cmd_queue.put_nowait("unduck")
        except Exception:
            pass

    # Alias semantici per gli hook di registrazione
    def on_recording_start(self):
        self.duck()

    def on_recording_stop(self):
        self.unduck()

    def restore_immediately(self):
        """Ripristino d'emergenza sincrono in uscita dall'applicazione."""
        try:
            self._cmd_queue.put_nowait("restore_now")
        except Exception:
            pass

    @staticmethod
    def _is_target_media_process(proc_name: str, pid: int, own_pid: int) -> bool:
        if not proc_name or pid <= 0 or pid == own_pid:
            return False
        clean = proc_name.strip().lower()
        if clean in ("python.exe", "pythonw.exe", "flowlocal.exe"):
            return False
        if clean in MEDIA_PROCESS_NAMES:
            return True
        return any(kw in clean for kw in MEDIA_NAME_KEYWORDS)

    def _has_pending_opposite_command(self, current_cmd: str) -> bool:
        """Controlla se nella coda è arrivato un comando opposto per interrompere il fade."""
        return not self._cmd_queue.empty()

    def _collect_media_sessions(self) -> Dict[str, _SavedSessionEntry]:
        """Individua tutte le sessioni audio WASAPI attive di app multimediali tramite pycaw."""
        found: Dict[str, _SavedSessionEntry] = {}
        try:
            from pycaw.pycaw import AudioUtilities

            sessions = AudioUtilities.GetAllSessions()
            for idx, session in enumerate(sessions):
                try:
                    proc = session.Process
                    if proc is None:
                        continue
                    pid = int(proc.pid)
                    if pid == self._own_pid:
                        continue
                    proc_name = (proc.name() or "").strip()
                    if not self._is_target_media_process(proc_name, pid, self._own_pid):
                        continue

                    vol_iface = session.SimpleAudioVolume
                    if vol_iface is None:
                        continue

                    try:
                        if vol_iface.GetMute():
                            continue
                    except Exception:
                        pass

                    orig_vol = float(vol_iface.GetMasterVolume())
                    if orig_vol <= 0.01:
                        continue

                    # Per i browser (che possono avere molte tab inattive), verifica se la sessione
                    # è in stato Active (State == 1) quando disponibile, mentre per Spotify/player
                    # musicali accetta qualsiasi sessione non muta.
                    is_browser = proc_name.lower() in {
                        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                        "opera.exe", "vivaldi.exe", "arc.exe", "waterfox.exe", "zen.exe"
                    }
                    if is_browser:
                        try:
                            state = int(session.State)
                            if state != 1:  # AudioSessionStateActive == 1
                                continue
                        except Exception:
                            pass

                    inst_id = getattr(session, "InstanceIdentifier", None) or f"{pid}_{idx}"
                    key = f"{pid}:{inst_id}"
                    found[key] = _SavedSessionEntry(
                        key=key,
                        volume_interface=vol_iface,
                        original_volume=orig_vol,
                        process_name=proc_name,
                    )
                except Exception:
                    continue
        except Exception:
            pass
        return found

    def _perform_duck(self):
        """Esegue il fade-out fluido in 120ms al 15% del volume originale (nel thread COM)."""
        if not self._enabled:
            return

        try:
            # Se non siamo già in stato ducked, raccoglie e salva i volumi originali
            if not self._is_ducked or not self._saved_sessions:
                new_sessions = self._collect_media_sessions()
                for k, entry in new_sessions.items():
                    if k not in self._saved_sessions:
                        self._saved_sessions[k] = entry

            if not self._saved_sessions:
                self._is_ducked = True
                return

            self._is_ducked = True
            steps = max(1, FADE_OUT_STEPS)
            step_delay = FADE_OUT_DURATION_S / float(steps)

            # Punto di partenza per ogni sessione
            start_vols: Dict[str, float] = {}
            target_vols: Dict[str, float] = {}
            for k, entry in list(self._saved_sessions.items()):
                try:
                    curr = float(entry.volume_interface.GetMasterVolume())
                except Exception:
                    curr = entry.current_volume
                start_vols[k] = curr
                target_vols[k] = max(0.0, min(1.0, entry.original_volume * DUCK_RATIO))

            for step in range(1, steps + 1):
                if self._has_pending_opposite_command("duck"):
                    break
                alpha = step / float(steps)
                for k, entry in list(self._saved_sessions.items()):
                    try:
                        s_vol = start_vols.get(k, entry.original_volume)
                        t_vol = target_vols.get(k, entry.original_volume * DUCK_RATIO)
                        v = s_vol + (t_vol - s_vol) * alpha
                        v_clamped = max(0.0, min(1.0, float(v)))
                        entry.volume_interface.SetMasterVolume(v_clamped, None)
                        entry.current_volume = v_clamped
                    except Exception:
                        pass
                if step < steps:
                    time.sleep(step_delay)
        except Exception:
            pass

    def _perform_unduck(self, immediate: bool = False):
        """Esegue il fade-in morbido in 200ms per ripristinare il volume originale (nel thread COM)."""
        try:
            if not self._saved_sessions:
                self._is_ducked = False
                return

            if immediate:
                for entry in list(self._saved_sessions.values()):
                    try:
                        entry.volume_interface.SetMasterVolume(
                            max(0.0, min(1.0, float(entry.original_volume))), None
                        )
                    except Exception:
                        pass
                self._saved_sessions.clear()
                self._is_ducked = False
                return

            steps = max(1, FADE_IN_STEPS)
            step_delay = FADE_IN_DURATION_S / float(steps)

            start_vols: Dict[str, float] = {}
            for k, entry in list(self._saved_sessions.items()):
                try:
                    curr = float(entry.volume_interface.GetMasterVolume())
                except Exception:
                    curr = entry.current_volume
                start_vols[k] = curr

            interrupted = False
            for step in range(1, steps + 1):
                if self._has_pending_opposite_command("unduck"):
                    interrupted = True
                    break
                alpha = step / float(steps)
                for k, entry in list(self._saved_sessions.items()):
                    try:
                        s_vol = start_vols.get(k, entry.current_volume)
                        t_vol = entry.original_volume
                        v = s_vol + (t_vol - s_vol) * alpha
                        v_clamped = max(0.0, min(1.0, float(v)))
                        entry.volume_interface.SetMasterVolume(v_clamped, None)
                        entry.current_volume = v_clamped
                    except Exception:
                        pass
                if step < steps:
                    time.sleep(step_delay)

            if not interrupted:
                self._saved_sessions.clear()
                self._is_ducked = False
        except Exception:
            self._saved_sessions.clear()
            self._is_ducked = False

    def _com_worker_loop(self):
        """Loop dedicato con comtypes.CoInitialize() per operazioni WASAPI fail-safe."""
        com_initialized = False
        try:
            import comtypes
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        try:
            while True:
                cmd = self._cmd_queue.get()
                if cmd is None:
                    break

                # Coalesce eventuali comandi multipli accodati rapidamente:
                # prevale sempre l'ultimo stato richiesto, salvo 'restore_now'.
                while not self._cmd_queue.empty():
                    try:
                        nxt = self._cmd_queue.get_nowait()
                        if nxt is None:
                            cmd = None
                            break
                        if nxt == "restore_now":
                            cmd = "restore_now"
                            break
                        cmd = nxt
                    except queue.Empty:
                        break

                if cmd is None:
                    break
                elif cmd == "duck":
                    self._perform_duck()
                elif cmd == "unduck":
                    self._perform_unduck(immediate=False)
                elif cmd == "restore_now":
                    self._perform_unduck(immediate=True)
        except Exception:
            pass
        finally:
            if com_initialized:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass


# Istanza singleton globale pronta all'uso
audio_ducker = AudioDucker()
