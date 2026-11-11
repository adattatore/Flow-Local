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

try:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)
except Exception:
    pass

import time
import threading
import subprocess
import urllib.request

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PyQt6.QtCore import Qt
import keyboard

from database import db
from ui.voice_orb import VoiceOrbOverlay
from ui.hub_window import FlowLocalHub
from core.ai_pipeline import FlowLocalEngine

VK_CAPITAL = 0x14  # Caps Lock (Bloc Maiusc)
VK_SHIFT = 0x10    # Shift
VK_F8 = 0x77       # Tasto F8

keyboard_lock = threading.Lock()
last_release_time = 0.0


def is_shift_down() -> bool:
    """Verifica se il tasto Shift è attualmente premuto."""
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
    except Exception:
        return False


def safe_handle_press(engine: FlowLocalEngine):
    with keyboard_lock:
        engine.handle_key_press()


def safe_handle_release(engine: FlowLocalEngine):
    global last_release_time
    with keyboard_lock:
        now = time.time()
        if (now - last_release_time) < 0.350:  # Debounce 350ms su rilascio Caps Lock / F8
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
    """Poller hardware nativo Win32 (Livello 1) per Caps Lock e F8."""
    user32 = ctypes.windll.user32
    was_down = False

    while True:
        try:
            # Se Shift è premuto con Caps Lock, lasciamo che l'utente usi la scorciatoia per attivare il Bloc Maiusc
            shift_pressed = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
            caps_down = bool(user32.GetAsyncKeyState(VK_CAPITAL) & 0x8000) and not shift_pressed
            f8_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            is_down = caps_down or f8_down

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

    def toggle_hub():
        if hub.isVisible():
            hub.hide()
        else:
            hub.show()
            hub.raise_()
            hub.activateWindow()

    orb.on_open_hub = toggle_hub

    # 3. Core Engine: Faster-Whisper + Qwen 2.5 + Audio WASAPI
    engine = FlowLocalEngine(orb=orb, hub=hub)
    orb.on_click_toggle = engine.toggle_recording_click
    orb.on_change_mode = engine.set_mode
    hub.on_device_changed = engine.change_device

    # Mostra entrambi i componenti all'avvio
    orb.show()
    hub.show()

    # 4. System Tray Icon nativa Qt
    tray = QSystemTrayIcon()
    tray.setIcon(create_tray_icon(active=True))
    tray.setToolTip("FlowLocal — AI Voice & Study Workspace (Caps Lock per parlare)")

    menu = QMenu()
    menu.setStyleSheet(
        "QMenu { background-color: #141419; color: #F8FAFC; border: 1px solid #2D2D3A; padding: 4px; border-radius: 8px; }"
        "QMenu::item { padding: 6px 14px; border-radius: 4px; }"
        "QMenu::item:selected { background-color: #2563EB; }"
    )

    open_hub_act = QAction("🚀 Mostra / Nascondi Hub", menu)
    open_hub_act.triggered.connect(toggle_hub)
    menu.addAction(open_hub_act)

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

    # 5. Dual Key Engine con Hook Globale soppressivo su Caps Lock
    poller_thread = threading.Thread(target=run_keyboard_poller, args=(engine,), daemon=True)
    poller_thread.start()

    def on_key_event(event):
        """Intercetta ed elabora Caps Lock e F8 sopprimendo il cambio maiuscole nativo."""
        try:
            name = (event.name or "").lower()
            if name == "caps lock":
                # Se l'utente preme Shift + Caps Lock, lascia passare l'evento per consentire il normale toggle del Bloc Maiusc
                if is_shift_down():
                    return True

                if event.event_type == "down":
                    safe_handle_press(engine)
                elif event.event_type == "up":
                    safe_handle_release(engine)
                # Sopprime l'evento a livello di sistema operativo per evitare il toggle maiuscole
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
