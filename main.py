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

# Sopprime il banner "Hello from the pygame community" all'avvio
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

if len(sys.argv) >= 3 and sys.argv[1] == "--exec-python":
    import runpy
    import traceback
    _script_path = sys.argv[2]
    sys.argv = [_script_path] + sys.argv[3:]
    try:
        runpy.run_path(_script_path, run_name="__main__")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

from core.paths import get_resource_path
from core.logging_config import setup_logging

setup_logging()

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
from PyQt6.QtCore import Qt, QTimer, QAbstractNativeEventFilter, QByteArray
from PyQt6.QtSvg import QSvgRenderer
if sys.platform == "win32":
    import keyboard
else:
    keyboard = None

from database import db
from ui.voice_orb import VoiceOrbOverlay, render_orb_icon_pixmap
from ui.welcome_splash import show_startup_welcome
from ui.whis_hud import WhisHUD
from core.ai_pipeline import FlowLocalEngine
from core.wake_listener import WhisWakeListener

VK_RSHIFT = 0xA1  # Shift Destro (Right Shift, tasto sotto Invio)
VK_F8 = 0x77      # Tasto F8 (fallback secondario)
VK_RCONTROL = 0xA3  # Ctrl Destro (Push-to-Lock)
VK_RMENU = 0xA5     # Alt Gr (Push-to-Lock alternativo)

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
    with keyboard_lock:
        engine.handle_key_release()


def safe_handle_lock(engine: FlowLocalEngine):
    with keyboard_lock:
        engine.lock_current_recording()


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
                elif action == "lock":
                    safe_handle_lock(engine)
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
        if keyboard is None:
            return
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

    def _install_linux_pynput(self):
        """Avvia listener globale pynput su Linux (X11/XWayland) senza richiedere privilegi root."""
        from types import SimpleNamespace
        from pynput import keyboard as pynput_kb

        pressed_keys = set()

        def _map_key_name(key) -> str:
            if key == pynput_kb.Key.shift_r:
                return "right shift"
            if key == pynput_kb.Key.f8:
                return "f8"
            if key == pynput_kb.Key.ctrl_r:
                return "right ctrl"
            if key in (pynput_kb.Key.alt_r, getattr(pynput_kb, "Key.alt_gr", None)) or str(key) == "Key.alt_gr":
                return "alt gr"
            return ""

        def _on_press(key):
            name = _map_key_name(key)
            if not name or name in pressed_keys:
                return
            pressed_keys.add(name)
            self._callback(SimpleNamespace(name=name, event_type="down", scan_code=None, is_extended=False))

        def _on_release(key):
            name = _map_key_name(key)
            if not name:
                return
            pressed_keys.discard(name)
            self._callback(SimpleNamespace(name=name, event_type="up", scan_code=None, is_extended=False))

        listener = pynput_kb.Listener(on_press=_on_press, on_release=_on_release)
        listener.daemon = True
        listener.start()
        return listener

    def install(self):
        with self._lock:
            self._unhook_locked()
            self._clear_internal_state()
            try:
                if keyboard is not None:
                    self._handler = keyboard.hook(self._callback, suppress=True)
                else:
                    self._handler = self._install_linux_pynput()
            except Exception as e:
                print(f"[FlowLocal] Errore registrazione hook tastiera: {e}")

    def _unhook_locked(self):
        if self._handler is not None:
            try:
                if keyboard is not None:
                    keyboard.unhook(self._handler)
                elif hasattr(self._handler, "stop"):
                    self._handler.stop()
            except Exception:
                pass
            self._handler = None
        if keyboard is not None:
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
                if keyboard is not None:
                    self._handler = keyboard.hook(self._callback, suppress=True)
                else:
                    self._handler = self._install_linux_pynput()
            except Exception as e:
                print(f"[FlowLocal] Errore re-hook tastiera: {e}")


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
        if sys.platform != "win32" or eventType != b"windows_generic_MSG" or not message:
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
            popen_kwargs = {"close_fds": True}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = 0x08000000 | 0x00000200
            subprocess.Popen(["ollama", "serve"], **popen_kwargs)
        except Exception:
            pass


def run_keyboard_poller(engine: FlowLocalEngine, hook_manager: Optional[KeyboardHookManager] = None):
    """Poller hardware nativo Win32 (Livello 1) per Shift Destro, F8 e Ctrl Destro/AltGr (Push-to-Lock)."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    was_down = False
    was_lock_down = False
    last_wall = time.time()
    last_gap_recover = 0.0

    while True:
        try:
            now_wall = time.time()
            if poller_reset_event.is_set():
                poller_reset_event.clear()
                was_down = False
                was_lock_down = False

            # Fallback di sicurezza per Modern Standby (salto temporale > 3s)
            if (now_wall - last_wall) > 3.0 and (now_wall - last_gap_recover) > 5.0:
                last_gap_recover = now_wall
                was_down = False
                was_lock_down = False
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

            lock_down = bool(user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000) or bool(user32.GetAsyncKeyState(VK_RMENU) & 0x8000)
            if lock_down and not was_lock_down:
                was_lock_down = True
                if is_down and engine.is_recording and not engine.is_locked:
                    key_action_queue.put("lock")
            elif not lock_down and was_lock_down:
                was_lock_down = False
        except Exception:
            pass

        time.sleep(0.015)


def create_tray_icon(active=True) -> QIcon:
    """Crea l'icona della system tray allineata all'Orb con i 3 fuochi."""
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64):
        icon.addPixmap(render_orb_icon_pixmap(sz, active=active))
    return icon


def create_app_icon() -> QIcon:
    """Restituisce l'icona principale dell'app (app.ico o fallback Orb 3 fuochi)."""
    ico_path = str(get_resource_path("app.ico"))
    if os.path.exists(ico_path):
        ico = QIcon(ico_path)
        if not ico.isNull():
            return ico
    icon = QIcon()
    for sz in (16, 32, 48, 64, 128, 256):
        icon.addPixmap(render_orb_icon_pixmap(sz, active=True))
    return icon


def create_svg_icon(svg_inner: str, color: str, size: int = 16) -> QIcon:
    """Renderizza un'icona SVG vettoriale nitida per il menù contestuale."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{svg_inner}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(create_app_icon())

    # Avvio non bloccante di Ollama in background
    threading.Thread(target=ensure_ollama_running, daemon=True).start()

    # 1. VoiceOrbOverlay flottante
    orb = VoiceOrbOverlay()

    # 2. WhisHUD (HUD Assistente Peek-from-Edge da bordo destro) integrato con VoiceOrb
    whis_hud = WhisHUD(orb=orb)

    # 3. Core Engine: Faster-Whisper (caricamento async) + Qwen 3.5 + Audio WASAPI
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

    saved_hf = (db.get_setting("whis_handsfree_enabled", "1") or "1").strip().lower()
    handsfree_state = {"enabled": saved_hf in ("1", "true", "on", "yes")}
    hub_ref = {"hub": None}

    def _sync_engine_mic_policy():
        """Spegne realmente il microfono di sistema quando Whis è OFF (e pre-buffer always-on non richiesto)."""
        always_prebuf = (db.get_setting("always_on_prebuffer", "0") or "0").strip().lower() in ("1", "true", "on", "yes")
        keep_open = bool(handsfree_state["enabled"] or always_prebuf)
        if hasattr(engine, "set_always_on_mic"):
            engine.set_always_on_mic(keep_open)

    def on_device_changed(new_dev_idx):
        engine.change_device(new_dev_idx)
        if wake_listener:
            wake_listener.device_index = new_dev_idx
            if handsfree_state["enabled"] and wake_listener.isRunning():
                wake_listener.set_paused(True)
                wake_listener.set_paused(False)

    # Applica politica microfono all'avvio in base allo stato persistito di Whis
    _sync_engine_mic_policy()
    if handsfree_state["enabled"]:
        wake_listener.set_paused(False)
        wake_listener.start()
    else:
        wake_listener.set_paused(True)

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
    menu.setWindowFlags(
        menu.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setStyleSheet(
        "QMenu {"
        "  background-color: #0E1118;"
        "  color: #F1F5F9;"
        "  border: 1px solid #262C3D;"
        "  border-radius: 10px;"
        "  padding: 6px;"
        "  font-family: 'Segoe UI';"
        "  font-size: 13px;"
        "  font-weight: 500;"
        "}"
        "QMenu::item {"
        "  padding: 8px 16px 8px 10px;"
        "  border-radius: 6px;"
        "  margin: 1px 0px;"
        "  background-color: transparent;"
        "}"
        "QMenu::item:selected {"
        "  background-color: rgba(56, 189, 248, 0.16);"
        "  color: #FFFFFF;"
        "}"
        "QMenu::separator {"
        "  height: 1px;"
        "  background-color: #1E2433;"
        "  margin: 5px 8px;"
        "}"
        "QMenu::indicator {"
        "  width: 14px;"
        "  height: 14px;"
        "  margin-left: 6px;"
        "  border-radius: 4px;"
        "}"
        "QMenu::indicator:unchecked {"
        "  border: 1px solid #334155;"
        "  background-color: #161B26;"
        "}"
        "QMenu::indicator:checked {"
        "  border: 1px solid #10B981;"
        "  background-color: #10B981;"
        "}"
    )

    hub_icon = create_svg_icon(
        '<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        "#38BDF8",
    )
    open_hub_act = QAction(hub_icon, "Mostra / Nascondi Hub", menu)
    open_hub_act.triggered.connect(toggle_hub)
    menu.addAction(open_hub_act)

    whis_icon = create_svg_icon(
        '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/>'
        '<path d="M19 3v3M20.5 4.5h-3"/>',
        "#A855F7",
    )
    whis_act = QAction(whis_icon, "Chiedi a Whis", menu)
    whis_act.triggered.connect(lambda: whis_hud.start_query("Ciao Whis, dimmi come puoi aiutarmi."))
    menu.addAction(whis_act)

    mic_svg = (
        '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/>'
        '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/>'
        '<line x1="12" y1="19" x2="12" y2="22"/>'
    )
    mic_icon_on = create_svg_icon(mic_svg, "#10B981")
    mic_icon_off = create_svg_icon(mic_svg, "#94A3B8")

    handsfree_act = QAction(
        mic_icon_on if handsfree_state["enabled"] else mic_icon_off,
        "Attiva Mani Libere (\"Di' Whis...\")",
        menu,
    )
    handsfree_act.setCheckable(True)
    handsfree_act.setChecked(handsfree_state["enabled"])

    def on_toggle_handsfree(checked: bool):
        handsfree_state["enabled"] = bool(checked)
        db.set_setting("whis_handsfree_enabled", "1" if checked else "0")
        handsfree_act.setIcon(mic_icon_on if checked else mic_icon_off)
        _sync_engine_mic_policy()
        hub = hub_ref["hub"]
        if hub is not None and hasattr(hub, "handsfree_switch"):
            hub.handsfree_switch.setChecked(checked)
        elif wake_listener:
            if checked:
                if not wake_listener.isRunning():
                    wake_listener.set_paused(False)
                    wake_listener.start()
                else:
                    wake_listener.set_paused(False)
            else:
                wake_listener.set_paused(True)

    handsfree_act.toggled.connect(on_toggle_handsfree)

    def on_hub_handsfree_changed(active: bool):
        handsfree_state["enabled"] = bool(active)
        db.set_setting("whis_handsfree_enabled", "1" if active else "0")
        _sync_engine_mic_policy()
        handsfree_act.blockSignals(True)
        handsfree_act.setChecked(active)
        handsfree_act.setIcon(mic_icon_on if active else mic_icon_off)
        handsfree_act.blockSignals(False)

    menu.addAction(handsfree_act)

    menu.addSeparator()

    pause_icon = create_svg_icon(
        '<rect x="6" y="4" width="4" height="16" rx="1"/>'
        '<rect x="14" y="4" width="4" height="16" rx="1"/>',
        "#F59E0B",
    )
    resume_icon = create_svg_icon(
        '<polygon points="6 4 20 12 6 20 6 4"/>',
        "#10B981",
    )
    pause_act = QAction(pause_icon, "Sospendi Dettatura", menu)

    def on_toggle_pause():
        is_paused = engine.toggle_pause()
        tray.setIcon(create_tray_icon(active=not is_paused))
        pause_act.setIcon(resume_icon if is_paused else pause_icon)
        pause_act.setText("Riprendi Dettatura" if is_paused else "Sospendi Dettatura")

    pause_act.triggered.connect(on_toggle_pause)
    menu.addAction(pause_act)

    menu.addSeparator()

    def on_exit():
        try:
            tray.hide()
            orb.hide()
            if hub_ref["hub"] is not None:
                try:
                    if hasattr(hub_ref["hub"], "save_workspace_session_state"):
                        hub_ref["hub"].save_workspace_session_state()
                except Exception:
                    pass
                hub_ref["hub"].hide()
            whis_hud.hide()
            if wake_listener:
                wake_listener.stop()
            if hasattr(engine, "_stop_stream"):
                engine._stop_stream()
            elif engine.stream:
                engine.stream.stop()
                engine.stream.close()
        except Exception:
            pass
        try:
            cleanup_keyboard()
        except Exception:
            pass
        try:
            db.checkpoint_and_close()
        except Exception:
            pass
        app.quit()
        QTimer.singleShot(450, lambda: os._exit(0))

    exit_icon = create_svg_icon(
        '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>'
        '<line x1="12" y1="2" x2="12" y2="12"/>',
        "#F43F5E",
    )
    exit_act = QAction(exit_icon, "Esci da FlowLocal", menu)
    exit_act.triggered.connect(on_exit)
    menu.addAction(exit_act)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: toggle_hub() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    QTimer.singleShot(150, lambda: show_startup_welcome(orb=orb))

    # 5. Dual Key Engine con Hook Globale soppressivo non-bloccante e recovery da standby
    start_key_action_worker(engine)

    def on_key_event(event):
        """Intercetta Shift Destro (Right Shift), F8 e Ctrl Destro/AltGr (Push-to-Lock) senza bloccare WH_KEYBOARD_LL."""
        try:
            name = (event.name or "").lower()
            scan = getattr(event, "scan_code", None)
            is_ext = bool(getattr(event, "is_extended", False))
            is_rshift = (scan == 54 or name in ("right shift", "maiusc (destra)", "maiusc destra", "rshift"))

            if is_rshift or name == "f8":
                if event.event_type == "down":
                    key_action_queue.put("press")
                elif event.event_type == "up":
                    key_action_queue.put("release")
                # Sopprime l'evento a livello di sistema operativo per non inserire modificatori mentre si parla
                return False

            is_lock_key = (
                name in ("right ctrl", "ctrl destra", "ctrl (destra)", "rctrl", "alt gr", "right alt", "alt destra", "ralt")
                or (scan == 29 and is_ext)
            )
            if is_lock_key and event.event_type == "down":
                if engine.is_recording and not engine.is_locked and engine.is_key_down:
                    key_action_queue.put("lock")
                    return False
        except Exception:
            pass
        return True

    hook_manager = KeyboardHookManager(on_key_event)
    hook_manager.install()

    wts_registered_hwnd = None
    if sys.platform == "win32":
        power_filter = PowerEventFilter(hook_manager, engine)
        app.installNativeEventFilter(power_filter)

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
        if wts_registered_hwnd is not None and sys.platform == "win32":
            try:
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(wts_registered_hwnd)
            except Exception:
                pass

    app.aboutToQuit.connect(cleanup_keyboard)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
