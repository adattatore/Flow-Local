"""
FlowLocal v3.2 - Master Entry Point (PyQt6)
Avvia:
1. VoiceOrbOverlay (140x140 trasparente, cerchio nero centrale, onde Catmull-Rom/Bézier, stati IDLE/LISTENING/COMMAND/STUDY/WHISPER/QWEN)
2. FlowLocalHub (Finestra Principale 1080x700 Obsidian Dark con telemetria GPU RTX 5060)
3. QSystemTrayIcon (apri/chiudi Hub, toggle pausa, uscita pulita)
4. Dual Key Engine (Hardware GetAsyncKeyState + Pynput listener)
"""

import os
import sys
import ctypes

# Compatibilità PyAV >= 13/14 con Faster-Whisper:
# Le versioni recenti di PyAV hanno rimosso il parametro metadata_errors da av.open()
try:
    import av
    if hasattr(av, "open"):
        _orig_av_open = av.open
        def _safe_av_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return _orig_av_open(*args, **kwargs)
        av.open = _safe_av_open
    if hasattr(av, "container") and hasattr(av.container, "core") and hasattr(av.container.core, "open"):
        av.container.core.open = _safe_av_open
except Exception:
    pass

try:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)
except Exception:
    pass

import time
import queue
import signal
import threading
import subprocess
import urllib.request
import ctypes.wintypes
from typing import Optional

import core.ai_pipeline as aip

def _safe_sigint_handler(signum, frame):
    """Ignora i segnali SIGINT generati dalla simulazione interna di Ctrl+C per cattura selezione."""
    now = time.time()
    last_cap = getattr(aip, "LAST_SELECTION_CAPTURE_TIME", 0.0)
    if (now - last_cap) < 0.600:
        return
    print("\n[FlowLocal] Ricevuto arresto da terminale.")
    try:
        app_inst = QApplication.instance()
        if app_inst:
            app_inst.quit()
    except Exception:
        pass
    os._exit(0)

try:
    signal.signal(signal.SIGINT, _safe_sigint_handler)
except Exception:
    pass

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import Qt, QTimer, QAbstractNativeEventFilter
import keyboard

from database import db
from ui.voice_orb import VoiceOrbOverlay
from ui.whis_hud import WhisHUD
from core.ai_pipeline import FlowLocalEngine
from core.wake_listener import WhisWakeListener

VK_RSHIFT = 0xA1  # Shift Destro (Right Shift, tasto sotto Invio)
VK_F8 = 0x77      # Tasto F8 (fallback secondario)

WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8

keyboard_lock = threading.Lock()
last_release_time = 0.0
active_wake_listener: Optional[WhisWakeListener] = None
key_action_queue: queue.SimpleQueue = queue.SimpleQueue()
poller_reset_event = threading.Event()


def safe_handle_press(engine: FlowLocalEngine):
    if active_wake_listener and not active_wake_listener.is_paused:
        active_wake_listener.set_paused(True)
    with keyboard_lock:
        engine.handle_key_press()


def safe_handle_release(engine: FlowLocalEngine):
    global last_release_time
    with keyboard_lock:
        now = time.time()
        if (now - last_release_time) < 0.350:  # Debounce 350ms su rilascio Shift Destro / F8
            return
        last_release_time = now
        engine.handle_key_release()


def start_key_action_worker(engine: FlowLocalEngine):
    """Worker thread dedicato per disaccoppiare WH_KEYBOARD_LL da operazioni bloccanti."""
    def _worker():
        while True:
            action = key_action_queue.get()
            try:
                if action == "press":
                    safe_handle_press(engine)
                elif action == "release":
                    safe_handle_release(engine)
            except Exception as e:
                print(f"[FlowLocal] Errore key_action_worker: {e}")

    threading.Thread(target=_worker, daemon=True).start()


class KeyboardHookManager:
    """Gestisce l'hook globale keyboard in modo thread-safe e resiliente a standby/lock."""

    def __init__(self, callback):
        self._callback = callback
        self._lock = threading.Lock()
        self._handler = None

    @staticmethod
    def _clear_internal_state():
        """Pulisce eventuali stati tasti/modificatori rimasti incastrati in `keyboard`."""
        try:
            lock = getattr(keyboard, "_pressed_events_lock", None)
            if lock is not None:
                with lock:
                    for attr in ("_pressed_events", "_physically_pressed_keys", "_logically_pressed_keys"):
                        obj = getattr(keyboard, attr, None)
                        if hasattr(obj, "clear"):
                            obj.clear()
            else:
                for attr in ("_pressed_events", "_physically_pressed_keys", "_logically_pressed_keys"):
                    obj = getattr(keyboard, attr, None)
                    if hasattr(obj, "clear"):
                        obj.clear()
        except Exception:
            pass

    def install(self):
        with self._lock:
            self._unhook_locked()
            self._clear_internal_state()
            try:
                self._handler = keyboard.hook(self._callback, suppress=True)
            except Exception as e:
                print(f"[FlowLocal] Errore registrazione keyboard.hook: {e}")

    def _unhook_locked(self):
        if self._handler is not None:
            try:
                keyboard.unhook(self._handler)
            except Exception:
                pass
            self._handler = None
        try:
            keyboard.unhook_all()
        except Exception:
            pass

    def unhook(self):
        with self._lock:
            self._unhook_locked()
            self._clear_internal_state()

    def rehook(self):
        with self._lock:
            self._unhook_locked()
            self._clear_internal_state()
            try:
                self._handler = keyboard.hook(self._callback, suppress=True)
            except Exception as e:
                print(f"[FlowLocal] Errore re-hook keyboard: {e}")


class PowerEventFilter(QAbstractNativeEventFilter):
    """Intercetta standby/risveglio e blocco/sblocco sessione Windows per ripristinare hook e audio."""

    def __init__(self, hook_manager: KeyboardHookManager, engine: FlowLocalEngine):
        super().__init__()
        self._hook_manager = hook_manager
        self._engine = engine
        self._resume_pending = False

    def _schedule_resume_recovery(self):
        if self._resume_pending:
            return
        self._resume_pending = True

        def _do_recover():
            self._resume_pending = False
            poller_reset_event.set()
            self._engine.reset_key_state()
            self._hook_manager.rehook()
            threading.Thread(target=self._engine.recover_audio_stream, daemon=True).start()
            print("[FlowLocal] Hook tastiera e audio ripristinati dopo risveglio/sblocco.")

        QTimer.singleShot(350, _do_recover)

    def nativeEventFilter(self, eventType, message):
        if eventType != b"windows_generic_MSG" or not message:
            return False, 0
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_POWERBROADCAST:
                if msg.wParam == PBT_APMSUSPEND:
                    poller_reset_event.set()
                    self._engine.reset_key_state()
                    self._hook_manager.unhook()
                    print("[FlowLocal] Standby rilevato: hook tastiera rilasciato.")
                elif msg.wParam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self._schedule_resume_recovery()
            elif msg.message == WM_WTSSESSION_CHANGE:
                if msg.wParam == WTS_SESSION_LOCK:
                    poller_reset_event.set()
                    self._engine.reset_key_state()
                    self._hook_manager.unhook()
                elif msg.wParam == WTS_SESSION_UNLOCK:
                    self._schedule_resume_recovery()
        except Exception:
            pass
        return False, 0


def ensure_ollama_running():
    try:
        urllib.request.urlopen("http://localhost:11434/api/version", timeout=0.8)
    except Exception:
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                creationflags=0x08000000 | 0x00000200,
                close_fds=True
            )
        except Exception:
            pass


def run_keyboard_poller(engine: FlowLocalEngine, hook_manager: Optional[KeyboardHookManager] = None):
    """Poller hardware nativo Win32 (Livello 1) per Shift Destro e F8."""
    user32 = ctypes.windll.user32
    was_down = False
    last_wall = time.time()
    last_gap_recover = 0.0

    while True:
        try:
            now_wall = time.time()
            if poller_reset_event.is_set():
                poller_reset_event.clear()
                was_down = False

            # Fallback di sicurezza per Modern Standby (salto temporale > 3s)
            if (now_wall - last_wall) > 3.0 and (now_wall - last_gap_recover) > 5.0:
                last_gap_recover = now_wall
                was_down = False
                engine.reset_key_state()
                if hook_manager is not None:
                    threading.Thread(target=hook_manager.rehook, daemon=True).start()
                threading.Thread(target=engine.recover_audio_stream, daemon=True).start()
            last_wall = now_wall

            rshift_down = bool(user32.GetAsyncKeyState(VK_RSHIFT) & 0x8000)
            f8_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            is_down = rshift_down or f8_down

            if is_down and not was_down:
                was_down = True
                key_action_queue.put("press")
            elif not is_down and was_down:
                was_down = False
                key_action_queue.put("release")
        except Exception:
            pass

        time.sleep(0.015)


def create_tray_icon(active=True) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    bg = QColor("#6366F1") if active else QColor("#71717A")
    p.setBrush(bg)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 28, 28)

    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(12, 7, 8, 12, 4, 4)
    pen = p.pen()
    pen.setColor(QColor(255, 255, 255))
    pen.setWidth(2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(9, 10, 14, 11, 0, -180 * 16)
    p.drawLine(16, 21, 16, 25)
    p.drawLine(12, 25, 20, 25)
    p.end()
    return QIcon(pixmap)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Avvio non bloccante di Ollama in background
    threading.Thread(target=ensure_ollama_running, daemon=True).start()

    # 1. VoiceOrbOverlay flottante
    orb = VoiceOrbOverlay()

    # 2. WhisHUD (HUD Assistente Peek-from-Edge da bordo destro) integrato con VoiceOrb
    whis_hud = WhisHUD(orb=orb)

    # 3. Core Engine: Faster-Whisper (caricamento async) + Qwen 2.5 + Audio WASAPI
    engine = FlowLocalEngine(orb=orb, hub=None, whis_hud=whis_hud)
    orb.on_click_toggle = engine.toggle_recording_click
    orb.on_change_mode = engine.set_mode

    # 3b. Hands-free Wake Word Listener ("Whis...")
    global active_wake_listener
    wake_listener = WhisWakeListener(
        whisper_instance=engine,
        sample_rate=engine.sample_rate,
        device_index=engine.dev_index,
    )
    active_wake_listener = wake_listener

    def on_wake_word(command: str):
        clean_cmd = (command or "").strip()
        if not clean_cmd:
            clean_cmd = "Ciao Whis, come puoi aiutarmi?"
        print(f"[FlowLocal] Wake word rilevata! Inoltro comando a Whis: '{clean_cmd}'")
        whis_hud.request_invoke.emit(clean_cmd, "")

    wake_listener.wake_word_detected.connect(on_wake_word)
    wake_listener.wake_activated.connect(whis_hud.show_wake_listening)
    wake_listener.followup_activated.connect(whis_hud.show_followup_listening)
    wake_listener.command_timeout.connect(whis_hud.dismiss_wake)
    wake_listener.status_message.connect(whis_hud.set_listening_status)
    whis_hud.set_wake_listener(wake_listener)

    handsfree_state = {"enabled": True}
    hub_ref = {"hub": None}

    def on_device_changed(new_dev_idx):
        engine.change_device(new_dev_idx)
        if wake_listener:
            wake_listener.device_index = new_dev_idx

    # Avvio di default: Attivo (ON) subito all'apertura
    if handsfree_state["enabled"]:
        wake_listener.start()
        wake_listener.set_paused(False)

    # Gestione automatica della pausa: quando l'utente usa Shift Destro / F8,
    # il listener hands-free si mette istantaneamente in pausa per evitare conflitti audio
    _orig_start_rec = engine.start_recording
    _orig_stop_rec = engine.stop_recording_and_process

    def _wrapped_start_rec(*args, **kwargs):
        if wake_listener and not wake_listener.is_paused:
            wake_listener.set_paused(True)
        return _orig_start_rec(*args, **kwargs)

    def _wrapped_stop_rec(*args, **kwargs):
        res = _orig_stop_rec(*args, **kwargs)
        if wake_listener and handsfree_state["enabled"]:
            def _delayed_resume():
                time.sleep(0.400)
                if not engine.is_recording and handsfree_state["enabled"]:
                    wake_listener.set_paused(False)
            threading.Thread(target=_delayed_resume, daemon=True).start()
        return res

    engine.start_recording = _wrapped_start_rec
    engine.stop_recording_and_process = _wrapped_stop_rec

    def get_or_create_hub():
        if hub_ref["hub"] is None:
            from ui.hub_window import FlowLocalHub
            hub = FlowLocalHub()
            hub.set_whis_agent(whis_hud.agent)
            hub.whis_hud = whis_hud
            hub.engine = engine
            engine.hub = hub
            hub.set_wake_listener(wake_listener)
            hub.on_device_changed = on_device_changed
            hub.on_handsfree_changed = on_hub_handsfree_changed
            hub.is_handsfree_enabled = handsfree_state["enabled"]
            if hasattr(hub, "handsfree_switch"):
                hub.handsfree_switch.blockSignals(True)
                hub.handsfree_switch.setChecked(handsfree_state["enabled"])
                hub.handsfree_switch.blockSignals(False)
            hub_ref["hub"] = hub
        return hub_ref["hub"]

    def toggle_hub():
        hub = get_or_create_hub()
        if hub.isVisible():
            hub.hide()
        else:
            hub.show()
            hub.raise_()
            hub.activateWindow()

    orb.on_open_hub = toggle_hub

    # Modalità Fantasma: l'orb resta nascosto in IDLE e compare solo durante la dettatura
    orb.hide()
    # L'Hub non si apre automaticamente all'avvio: resta accessibile dall'icona nella system tray.

    # 4. System Tray Icon nativa Qt
    tray = QSystemTrayIcon()
    tray.setIcon(create_tray_icon(active=True))
    tray.setToolTip("FlowLocal — AI Voice & Study Workspace (Shift Destro per parlare)")

    menu = QMenu()
    menu.setStyleSheet(
        "QMenu { background-color: #141419; color: #F8FAFC; border: 1px solid #2D2D3A; padding: 4px; border-radius: 8px; }"
        "QMenu::item { padding: 6px 14px; border-radius: 4px; }"
        "QMenu::item:selected { background-color: #2563EB; }"
    )

    open_hub_act = QAction("🚀 Mostra / Nascondi Hub", menu)
    open_hub_act.triggered.connect(toggle_hub)
    menu.addAction(open_hub_act)

    whis_act = QAction("✨ Chiedi a Whis", menu)
    whis_act.triggered.connect(lambda: whis_hud.start_query("Ciao Whis, dimmi come puoi aiutarmi."))
    menu.addAction(whis_act)

    handsfree_act = QAction("🎙️ Attiva Mani Libere (\"Di' Whis...\")", menu)
    handsfree_act.setCheckable(True)
    handsfree_act.setChecked(handsfree_state["enabled"])

    def on_toggle_handsfree(checked: bool):
        handsfree_state["enabled"] = checked
        hub = hub_ref["hub"]
        if hub is not None and hasattr(hub, "handsfree_switch"):
            hub.handsfree_switch.setChecked(checked)
        elif wake_listener:
            if checked:
                if not wake_listener.isRunning():
                    wake_listener.start()
                wake_listener.set_paused(False)
            else:
                wake_listener.set_paused(True)

    handsfree_act.toggled.connect(on_toggle_handsfree)

    def on_hub_handsfree_changed(active: bool):
        handsfree_state["enabled"] = active
        handsfree_act.blockSignals(True)
        handsfree_act.setChecked(active)
        handsfree_act.blockSignals(False)

    menu.addAction(handsfree_act)

    menu.addSeparator()

    pause_act = QAction("⏸️ Sospendi Dettatura", menu)

    def on_toggle_pause():
        is_paused = engine.toggle_pause()
        tray.setIcon(create_tray_icon(active=not is_paused))
        pause_act.setText("▶️ Riprendi Dettatura" if is_paused else "⏸️ Sospendi Dettatura")

    pause_act.triggered.connect(on_toggle_pause)
    menu.addAction(pause_act)

    menu.addSeparator()

    def on_exit():
        try:
            tray.hide()
            orb.hide()
            if hub_ref["hub"] is not None:
                hub_ref["hub"].hide()
            whis_hud.hide()
            if wake_listener:
                wake_listener.stop()
            if engine.stream:
                engine.stream.stop()
                engine.stream.close()
        except Exception:
            pass
        app.quit()
        os._exit(0)

    exit_act = QAction("❌ Esci da FlowLocal", menu)
    exit_act.triggered.connect(on_exit)
    menu.addAction(exit_act)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: toggle_hub() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    QTimer.singleShot(250, orb.play_startup_splash)

    # 5. Dual Key Engine con Hook Globale soppressivo non-bloccante e recovery da standby
    start_key_action_worker(engine)

    def on_key_event(event):
        """Intercetta Shift Destro (Right Shift) e F8 senza bloccare il thread WH_KEYBOARD_LL."""
        try:
            name = (event.name or "").lower()
            scan = getattr(event, "scan_code", None)
            is_rshift = (scan == 54 or name in ("right shift", "maiusc (destra)", "maiusc destra", "rshift"))

            if is_rshift or name == "f8":
                if event.event_type == "down":
                    key_action_queue.put("press")
                elif event.event_type == "up":
                    key_action_queue.put("release")
                # Sopprime l'evento a livello di sistema operativo per non inserire modificatori mentre si parla
                return False
        except Exception:
            pass
        return True

    hook_manager = KeyboardHookManager(on_key_event)
    hook_manager.install()

    power_filter = PowerEventFilter(hook_manager, engine)
    app.installNativeEventFilter(power_filter)

    wts_registered_hwnd = None
    try:
        orb_hwnd = int(orb.winId())
        if ctypes.windll.wtsapi32.WTSRegisterSessionNotification(orb_hwnd, 0):
            wts_registered_hwnd = orb_hwnd
    except Exception:
        pass

    poller_thread = threading.Thread(target=run_keyboard_poller, args=(engine, hook_manager), daemon=True)
    poller_thread.start()

    def cleanup_keyboard():
        hook_manager.unhook()
        if wts_registered_hwnd is not None:
            try:
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(wts_registered_hwnd)
            except Exception:
                pass

    app.aboutToQuit.connect(cleanup_keyboard)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
