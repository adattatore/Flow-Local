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
import signal
import threading
import subprocess
import urllib.request
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
from PyQt6.QtCore import Qt
import keyboard

from database import db
from ui.voice_orb import VoiceOrbOverlay
from ui.hub_window import FlowLocalHub
from ui.whis_hud import WhisHUD
from core.ai_pipeline import FlowLocalEngine
from core.wake_listener import WhisWakeListener

VK_RSHIFT = 0xA1  # Shift Destro (Right Shift, tasto sotto Invio)
VK_F8 = 0x77      # Tasto F8 (fallback secondario)

keyboard_lock = threading.Lock()
last_release_time = 0.0
active_wake_listener: Optional[WhisWakeListener] = None


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
            time.sleep(1.5)
        except Exception:
            pass

ensure_ollama_running()


def run_keyboard_poller(engine: FlowLocalEngine):
    """Poller hardware nativo Win32 (Livello 1) per Shift Destro e F8."""
    user32 = ctypes.windll.user32
    was_down = False

    while True:
        try:
            rshift_down = bool(user32.GetAsyncKeyState(VK_RSHIFT) & 0x8000)
            f8_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            is_down = rshift_down or f8_down

            if is_down and not was_down:
                was_down = True
                safe_handle_press(engine)
            elif not is_down and was_down:
                was_down = False
                safe_handle_release(engine)
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

    # 1. VoiceOrbOverlay flottante
    orb = VoiceOrbOverlay()

    # 2. FlowLocalHub (Finestra Principale 1080x700)
    hub = FlowLocalHub()

    # 2b. WhisHUD (HUD Assistente Peek-from-Edge da bordo destro) integrato con VoiceOrb
    whis_hud = WhisHUD(orb=orb)
    hub.set_whis_agent(whis_hud.agent)
    hub.whis_hud = whis_hud

    def toggle_hub():
        if hub.isVisible():
            hub.hide()
        else:
            hub.show()
            hub.raise_()
            hub.activateWindow()

    orb.on_open_hub = toggle_hub

    # 3. Core Engine: Faster-Whisper + Qwen 2.5 + Audio WASAPI
    engine = FlowLocalEngine(orb=orb, hub=hub, whis_hud=whis_hud)
    hub.engine = engine  # Condivide l'istanza con il modello Whisper già in VRAM
    hub.whis_hud = whis_hud
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
    hub.set_wake_listener(wake_listener)
    whis_hud.set_wake_listener(wake_listener)

    # Avvio di default: Attivo (ON) subito all'apertura
    if getattr(hub, "is_handsfree_enabled", False):
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
        if wake_listener and getattr(hub, "is_handsfree_enabled", False):
            def _delayed_resume():
                time.sleep(0.400)
                if not engine.is_recording and getattr(hub, "is_handsfree_enabled", False):
                    wake_listener.set_paused(False)
            threading.Thread(target=_delayed_resume, daemon=True).start()
        return res

    engine.start_recording = _wrapped_start_rec
    engine.stop_recording_and_process = _wrapped_stop_rec

    def on_device_changed(new_dev_idx):
        engine.change_device(new_dev_idx)
        if wake_listener:
            wake_listener.device_index = new_dev_idx
    hub.on_device_changed = on_device_changed

    # Modalità Fantasma: l'orb resta nascosto in IDLE e compare solo durante la dettatura
    orb.hide()
    hub.show()

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
    handsfree_act.setChecked(hub.is_handsfree_enabled)

    def on_toggle_handsfree(checked: bool):
        if hasattr(hub, "handsfree_switch"):
            hub.handsfree_switch.setChecked(checked)

    handsfree_act.toggled.connect(on_toggle_handsfree)

    def on_hub_handsfree_changed(active: bool):
        handsfree_act.blockSignals(True)
        handsfree_act.setChecked(active)
        handsfree_act.blockSignals(False)

    hub.on_handsfree_changed = on_hub_handsfree_changed
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
            hub.hide()
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

    # 5. Dual Key Engine con Hook Globale soppressivo su Shift Destro
    poller_thread = threading.Thread(target=run_keyboard_poller, args=(engine,), daemon=True)
    poller_thread.start()

    def on_key_event(event):
        """Intercetta ed elabora Shift Destro (Right Shift) e F8."""
        try:
            name = (event.name or "").lower()
            scan = getattr(event, "scan_code", None)
            is_rshift = (scan == 54 or name in ("right shift", "maiusc (destra)", "maiusc destra", "rshift"))

            if is_rshift:
                if event.event_type == "down":
                    safe_handle_press(engine)
                elif event.event_type == "up":
                    safe_handle_release(engine)
                # Sopprime l'evento a livello di sistema operativo per non inserire modificatori mentre si parla
                return False

            elif name == "f8":
                if event.event_type == "down":
                    safe_handle_press(engine)
                elif event.event_type == "up":
                    safe_handle_release(engine)
                return False
        except Exception:
            pass
        return True

    # Registra l'hook globale con soppressione attiva (suppress=True)
    hook_handler = keyboard.hook(on_key_event, suppress=True)

    def cleanup_keyboard():
        try:
            keyboard.unhook(hook_handler)
        except Exception:
            pass

    app.aboutToQuit.connect(cleanup_keyboard)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
