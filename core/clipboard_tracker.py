"""
core/clipboard_tracker.py
Tracker thread-safe per l'ultima dettatura FlowType, anteprima Ghost-Text (prime 6 parole),
cronologia rapida (ultime 20 dettature) e ancoraggio focus Win32.
"""

from __future__ import annotations

from collections import deque
import ctypes
import sys
import threading
import time
from typing import Deque, Dict, List, Optional

import pyperclip
from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication


class _ClipboardSignals(QObject):
    """Bridge Qt per aggiornare QApplication.clipboard() in sicurezza dal main thread."""
    copyRequested = pyqtSignal(str)
    historyUpdated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.copyRequested.connect(self._on_copy_requested, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(str)
    def _on_copy_requested(self, text: str):
        try:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(text)
        except Exception:
            pass


class ClipboardDictationTracker:
    """
    Mantiene in memoria l'ultima dettatura FlowType, il timestamp, lo stato di consumo
    per il Ghost-Text (finestra 3 minuti) e la cronologia delle ultime 20 dettature.
    """

    MAX_HISTORY = 20
    DEFAULT_TTL_SECONDS = 180.0  # 3 minuti

    def __init__(self):
        self._lock = threading.Lock()
        self._last_text: str = ""
        self._last_timestamp: float = 0.0
        self._consumed: bool = True
        self._history: Deque[Dict] = deque(maxlen=self.MAX_HISTORY)
        self._saved_hwnd: Optional[int] = None
        self._signals: Optional[_ClipboardSignals] = None

    def _ensure_signals(self) -> Optional[_ClipboardSignals]:
        app = QApplication.instance()
        if self._signals is None and app is not None:
            sig = _ClipboardSignals()
            if sig.thread() != app.thread():
                sig.moveToThread(app.thread())
            self._signals = sig
        return self._signals

    @property
    def signals(self) -> Optional[_ClipboardSignals]:
        return self._ensure_signals()

    def copy_to_system_clipboard(self, text: str):
        """Copia immediatamente il testo negli appunti di Windows (Qt + pyperclip)."""
        if not text:
            return
        try:
            pyperclip.copy(text)
        except Exception:
            pass
        sig = self._ensure_signals()
        if sig is not None:
            sig.copyRequested.emit(text)

    def record_dictation(
        self,
        text: str,
        app_name: str = "",
        style: str = "",
        consumed: bool = False,
    ):
        clean = (text or "").strip()
        if not clean:
            return
        now = time.time()
        self.copy_to_system_clipboard(clean)
        with self._lock:
            self._last_text = clean
            self._last_timestamp = now
            self._consumed = bool(consumed)
            self._history.appendleft(
                {
                    "text": clean,
                    "timestamp": now,
                    "app": app_name or "",
                    "style": style or "",
                }
            )
        sig = self._ensure_signals()
        if sig is not None:
            sig.historyUpdated.emit()

    def get_active_preview(self, max_age_s: float = DEFAULT_TTL_SECONDS) -> Optional[Dict[str, str]]:
        """
        Restituisce l'anteprima (prime 6 parole + '[Tab per incollare]') se la dettatura
        è recente (< 3 minuti) e non ancora consumata.
        """
        with self._lock:
            if self._consumed or not self._last_text:
                return None
            if (time.time() - self._last_timestamp) > max_age_s:
                return None
            full_text = self._last_text

        words = full_text.split()
        if not words:
            return None
        first_six = " ".join(words[:6])
        ellipsis = "..." if len(words) > 6 else ""
        preview_words = f"{first_six}{ellipsis}"
        return {
            "full_text": full_text,
            "preview_words": preview_words,
            "ghost_text": f"{preview_words}  [Tab per incollare]",
        }

    def consume(self) -> str:
        """Restituisce il testo completo dell'ultima dettatura e consuma il Ghost-Text."""
        with self._lock:
            self._consumed = True
            return self._last_text

    def mark_consumed(self):
        with self._lock:
            self._consumed = True

    def get_history(self) -> List[Dict]:
        with self._lock:
            return list(self._history)

    def save_focus_target(self):
        """Salva l'HWND della finestra attiva all'inizio della dettatura."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            with self._lock:
                self._saved_hwnd = hwnd if hwnd > 0 else None
        except Exception:
            pass

    def get_saved_hwnd(self) -> Optional[int]:
        with self._lock:
            return self._saved_hwnd

    def restore_focus_target(self):
        """Tenta il ripristino del focus sulla finestra attiva all'inizio della dettatura."""
        if sys.platform != "win32":
            return
        with self._lock:
            hwnd = self._saved_hwnd
        if not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            cur_fg = int(user32.GetForegroundWindow() or 0)
            if cur_fg != hwnd and user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.03)
        except Exception:
            pass


dictation_tracker = ClipboardDictationTracker()
