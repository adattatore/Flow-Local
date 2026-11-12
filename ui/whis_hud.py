"""
ui/whis_hud.py — Finestrella HUD Minimale "Peek-from-Edge" per l'Assistente Whis
Caratteristiche:
- Ancorata al bordo destro dello schermo con animazione scorrevole (slide-in / slide-out)
- Zero focus-steal: WA_ShowWithoutActivating + WindowStaysOnTopHint + NoFocus su tutti i controlli
- Card scura con accenti ciano/viola (#38bdf8 / #a855f7) ed elegante avatar circolare Whis
- Streaming live dei token tramite QThread asincrono
- Pulsante rapido '📋 Copia' con feedback visivo istantaneo e pulsante '✕' per chiudere
- Auto-hide intelligente dopo 18s di inattività (con pausa automatica se il mouse è sopra)
"""

import time
import pyperclip
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTextEdit, QScrollArea, QGraphicsDropShadowEffect, QApplication
)
from PyQt6.QtCore import Qt, QPoint, QPropertyAnimation, QEasingCurve, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPen

from core.whis_agent import WhisAgent, WhisStreamWorker
from core.audio_feedback import audio_feedback, MODE_VOICE, MODE_SOFT_CHIME, MODE_MUTE


class WhisAvatar(QWidget):
    """Avatar circolare con gradiente celestiale ciano/viola e aureola di Whis."""

    def __init__(self, size: int = 32, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())

        # Cerchio esterno con sfumatura ciano (#38bdf8) -> viola (#a855f7)
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor("#38bdf8"))
        grad.setColorAt(1.0, QColor("#a855f7"))

        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, int(w - 2), int(h - 2))

        # Cerchio interno scuro per dare profondità
        p.setBrush(QColor("#0e1017"))
        p.drawEllipse(3, 3, int(w - 6), int(h - 6))

        # Aureola celestiale sottile tipica di Whis
        halo_pen = QPen(QColor("#38bdf8"))
        halo_pen.setWidthF(1.3)
        p.setPen(halo_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(6, 6, int(w - 12), int(h - 12))

        # Monogramma 'W' o glifo centrale
        p.setPen(QColor("#ffffff"))
        font = QFont("Segoe UI", int(self._size * 0.35), QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "W")

        p.end()


class WhisHUD(QWidget):
    """
    Finestra HUD flottante da bordo schermo per Whis.
    Zero-focus garantito su Windows e giochi/app a schermo intero.
    """

    # Segnale thread-safe per invocazione cross-thread da audio pipeline
    request_invoke = pyqtSignal(str, str)  # (user_query, explicit_selection)

    HUD_WIDTH = 430
    HUD_HEIGHT = 290
    MARGIN_RIGHT = 18
    MARGIN_BOTTOM = 38

    def __init__(self, agent: Optional[WhisAgent] = None, orb=None, parent=None):
        super().__init__(parent)
        self.agent = agent or WhisAgent()
        self.orb = orb
        self.worker: Optional[WhisStreamWorker] = None
        self._is_animating = False
        self._current_response_text = ""
        self._active_model_name = "qwen2.5:3b"
        self._last_metadata = {}
        self.wake_listener = None

        self._setup_window()
        self._setup_ui()
        self._setup_timers()

        audio_feedback.add_mode_listener(self._sync_audio_button)

        self.request_invoke.connect(self._on_request_invoke)

    def set_wake_listener(self, listener):
        self.wake_listener = listener

    def set_orb(self, orb):
        self.orb = orb

    def _setup_window(self):
        # Flags per overlay flottante non invasivo
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Dimensioni fisse
        self.resize(self.HUD_WIDTH, self.HUD_HEIGHT)

    def _get_target_geometry(self):
        screen = QApplication.primaryScreen().availableGeometry()
        target_x = screen.x() + screen.width() - self.HUD_WIDTH - self.MARGIN_RIGHT
        target_y = screen.y() + screen.height() - self.HUD_HEIGHT - self.MARGIN_BOTTOM
        hidden_x = screen.x() + screen.width() + 20
        return hidden_x, target_x, target_y

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(0)

        # Card principale
        self.card = QFrame()
        self.card.setObjectName("whis_card")
        self.card.setStyleSheet("""
            QFrame#whis_card {
                background-color: #0f1118;
                border: 1px solid #38bdf8;
                border-radius: 12px;
            }
        """)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)

        # 1. Header con Avatar, Titolo Whis, Badge e Pulsanti Azione
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        header_layout.setContentsMargins(0, 0, 0, 0)

        self.avatar = WhisAvatar(size=30)
        header_layout.addWidget(self.avatar)

        self.title_label = QLabel("Whis")
        self.title_label.setStyleSheet("color: #F8FAFC; font-size: 14px; font-weight: 700;")
        header_layout.addWidget(self.title_label)

        self.model_badge = QLabel("⚡ 3B")
        self.model_badge.setStyleSheet("""
            background-color: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 10px;
            font-weight: 600;
        """)
        header_layout.addWidget(self.model_badge)

        self.clip_badge = QLabel("📎 Appunti")
        self.clip_badge.setStyleSheet("""
            background-color: rgba(168, 85, 247, 0.15);
            color: #c084fc;
            border: 1px solid rgba(168, 85, 247, 0.35);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 10px;
            font-weight: 600;
        """)
        self.clip_badge.hide()
        header_layout.addWidget(self.clip_badge)

        self.screen_badge = QLabel("🖥️ Schermo")
        self.screen_badge.setStyleSheet("""
            background-color: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.35);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 10px;
            font-weight: 600;
        """)
        self.screen_badge.hide()
        header_layout.addWidget(self.screen_badge)

        self.source_badge = QLabel("📚 Fonte")
        self.source_badge.setStyleSheet("""
            background-color: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.35);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 10px;
            font-weight: 600;
        """)
        self.source_badge.hide()
        header_layout.addWidget(self.source_badge)

        self.calendar_badge = QLabel("📅 Google Calendar")
        self.calendar_badge.setStyleSheet("""
            background-color: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 10px;
            font-weight: 600;
        """)
        self.calendar_badge.hide()
        header_layout.addWidget(self.calendar_badge)

        header_layout.addStretch()

        # Pulsante rapido Audio (Voce / Muto / Ferma voce)
        self.audio_btn = QPushButton("🔊")
        self.audio_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audio_btn.clicked.connect(self._on_audio_btn_clicked)
        header_layout.addWidget(self.audio_btn)
        self._sync_audio_button(audio_feedback.get_mode())

        # Pulsante Copia rapido
        self.copy_btn = QPushButton("📋 Copia")
        self.copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #1a1d28;
                color: #94a3b8;
                border: 1px solid #2d3345;
                border-radius: 6px;
                padding: 3px 9px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #262a3a;
                color: #f8fafc;
                border-color: #38bdf8;
            }
            QPushButton:pressed {
                background-color: #0f1118;
            }
        """)
        self.copy_btn.clicked.connect(self._on_copy_clicked)
        header_layout.addWidget(self.copy_btn)

        # Pulsante Chiudi
        self.close_btn = QPushButton("✕")
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #64748b;
                border: none;
                border-radius: 4px;
                font-size: 13px;
                font-weight: bold;
                padding: 2px 6px;
            }
            QPushButton:hover {
                background-color: rgba(244, 63, 94, 0.18);
                color: #f43f5e;
            }
        """)
        self.close_btn.clicked.connect(self.slide_out)
        header_layout.addWidget(self.close_btn)

        card_layout.addLayout(header_layout)

        # 2. Richiesta utente (sintetica)
        self.query_label = QLabel()
        self.query_label.setWordWrap(True)
        self.query_label.setStyleSheet("color: #94a3b8; font-size: 11px; font-style: italic;")
        self.query_label.hide()
        card_layout.addWidget(self.query_label)

        # 3. Separatore orizzontale
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        divider.setStyleSheet("background-color: #1e2433; border: none; min-height: 1px; max-height: 1px;")
        card_layout.addWidget(divider)

        # 4. Area testo scrollabile per la risposta in streaming
        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.text_area.setFrameShape(QFrame.Shape.NoFrame)
        self.text_area.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #f1f5f9;
                font-family: 'Segoe UI', system-ui, sans-serif;
                font-size: 12px;
                line-height: 1.45;
                selection-background-color: #38bdf8;
                selection-color: #0f1118;
                border: none;
            }
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 5px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #282f42;
                min-height: 18px;
                border-radius: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #38bdf8;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        card_layout.addWidget(self.text_area, 1)

        # 5. Footer di stato
        self.status_label = QLabel("✨ Whis sta pensando...")
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 10px; font-weight: 500;")
        card_layout.addWidget(self.status_label)

        main_layout.addWidget(self.card)

    def _setup_timers(self):
        # Timer di inattività 18 secondi per auto-hide
        self.auto_hide_timer = QTimer(self)
        self.auto_hide_timer.setSingleShot(True)
        self.auto_hide_timer.setInterval(18000)
        self.auto_hide_timer.timeout.connect(self.slide_out)

    def enterEvent(self, event):
        """Se il mouse entra nell'HUD, arresta temporaneamente l'auto-hide."""
        self.auto_hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Se il mouse lascia l'HUD e la risposta è finita, riavvia l'auto-hide."""
        if not (self.worker and self.worker.isRunning()):
            self.auto_hide_timer.start(18000)
        super().leaveEvent(event)

    def _on_request_invoke(self, query: str, selection: str):
        """Slot Qt eseguito tassativamente sul thread GUI principale."""
        self.start_query(query, selection_text=selection)

    def start_query(self, user_query: str, selection_text: Optional[str] = None):
        """Inizia l'elaborazione e lo streaming visivo della risposta di Whis."""
        self.auto_hide_timer.stop()
        audio_feedback.stop()

        # Ripristina stile bordo standard se era in stato di ascolto
        self.card.setStyleSheet("""
            QFrame#whis_card {
                background-color: #0f1118;
                border: 1px solid #38bdf8;
                border-radius: 12px;
            }
        """)

        # Interrompi eventuale worker precedente
        if self.worker and self.worker.isRunning():
            try:
                self.worker.terminate()
                self.worker.wait(100)
            except Exception:
                pass

        # Pulisci stato
        self._current_response_text = ""
        self.copy_btn.setText("📋 Copia")

        # Rileva modello target in anteprima per il badge
        predicted_model = self.agent.select_model(user_query)
        self._active_model_name = predicted_model
        if "7b" in predicted_model.lower():
            self.model_badge.setText("🧠 7B Deep")
            self.model_badge.setStyleSheet("""
                background-color: rgba(168, 85, 247, 0.18);
                color: #c084fc;
                border: 1px solid rgba(168, 85, 247, 0.4);
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 10px;
                font-weight: 600;
            """)
        else:
            self.model_badge.setText("⚡ 3B Fast")
            self.model_badge.setStyleSheet("""
                background-color: rgba(56, 189, 248, 0.15);
                color: #38bdf8;
                border: 1px solid rgba(56, 189, 248, 0.35);
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 10px;
                font-weight: 600;
            """)

        # Reset badge contestuali
        self.clip_badge.hide()
        self.screen_badge.hide()
        self.source_badge.hide()
        self.calendar_badge.hide()

        # Rileva deissi preliminare se presente
        has_deictic = bool(selection_text or self.agent.detect_and_attach_clipboard(user_query)[1])
        if has_deictic:
            self.clip_badge.show()

        # Mostra anteprima domanda utente
        clean_q = user_query.strip()
        if len(clean_q) > 65:
            clean_q = clean_q[:62] + "..."
        self.query_label.setText(f'💬 "{clean_q}"')
        self.query_label.show()

        # Mostra stato "in corso"
        self.text_area.setPlainText("")
        self.status_label.setText("✨ Whis sta pensando...")
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 10px; font-weight: 500;")
        self.status_label.show()

        # Se l'Orb è presente, impostiamo il suo stato su THINKING senza aprire il popup laterale
        if self.orb:
            self.orb.set_whis_state("THINKING")
        else:
            self.slide_in()

        # Avvia lo streaming su QThread dedicato
        self.worker = WhisStreamWorker(self.agent, user_query, explicit_selection=selection_text)
        self.worker.chunk_received.connect(self._on_chunk)
        self.worker.metadata_detected.connect(self._on_metadata_detected)
        self.worker.finished_response.connect(self._on_finished)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.start()

    def _on_metadata_detected(self, meta: dict):
        """Attiva i badge contestuali corrispondenti ai Superpoteri di Whis."""
        self._last_metadata = meta or {}
        if meta.get("clipboard"):
            self.clip_badge.show()
        if meta.get("screen_ocr"):
            self.screen_badge.show()
        rag_src = meta.get("rag_source")
        if rag_src:
            clean_src = rag_src.replace("Slide:", "").replace("Nota:", "").replace("Appunto:", "").strip()
            if len(clean_src) > 16:
                clean_src = clean_src[:14] + "…"
            self.source_badge.setText(f"📚 {clean_src}")
            self.source_badge.show()
        if meta.get("note_action"):
            self.status_label.setText("📝 Nota registrata con successo")
            self.status_label.setStyleSheet("color: #10B981; font-size: 10px; font-weight: 600;")
        if meta.get("calendar"):
            self.calendar_badge.show()
            self.status_label.setText("📅 Google Calendar")
            self.status_label.setStyleSheet("color: #38BDF8; font-size: 10px; font-weight: 600;")

    def _on_chunk(self, chunk: str):
        """Ricezione token in tempo reale."""
        if not self._current_response_text:
            self.status_label.setText("✨ Whis sta rispondendo...")
        self._current_response_text += chunk
        self.text_area.setPlainText(self._current_response_text)

        # Scorrimento fluido verso il basso
        sb = self.text_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_finished(self, full_text: str, model_used: str):
        """Completamento della risposta."""
        self._current_response_text = full_text

        # Render Markdown se possibile
        try:
            self.text_area.setMarkdown(full_text)
        except Exception:
            self.text_area.setPlainText(full_text)

        sb = self.text_area.verticalScrollBar()
        sb.setValue(sb.maximum())

        # Aggiorna badge modello effettivo
        if "7b" in model_used.lower():
            self.model_badge.setText("🧠 7B Deep")
        else:
            self.model_badge.setText("⚡ 3B Fast")

        self.status_label.setText("Risposta completata")
        self.status_label.setStyleSheet("color: #64748b; font-size: 10px; font-weight: 400;")

        # Avvia auto-hide garantendo tempo adeguato per la lettura vocale se attiva
        auto_hide_ms = 18000
        if audio_feedback.get_mode() == MODE_VOICE and full_text:
            auto_hide_ms = max(18000, len(full_text) * 75 + 4000)
        self.auto_hide_timer.start(auto_hide_ms)

        # Riproduce vocalmente la risposta in modalità 'voce'
        audio_feedback.speak_response(full_text)

        # Mostra la pillola di risposta dinamica sopra il Voice Orb
        if self.orb:
            self.orb.show_whis_response(full_text, self._last_metadata)

        # Se Whis è in attesa di risposta interattiva (es. titolo appuntamento),
        # resta attivamente in ascolto senza che l'utente debba ripetere 'Whis' o toccare tasti
        if self._last_metadata and self._last_metadata.get("waiting_for_title"):
            self.auto_hide_timer.stop()

            def _arm_interactive_listening():
                import time
                # Attendi che la sintesi vocale di Whis abbia terminato di pronunciare la domanda
                while audio_feedback.is_speaking():
                    time.sleep(0.08)
                time.sleep(0.25)
                if hasattr(self, "wake_listener") and self.wake_listener:
                    self.wake_listener.open_command_listening(
                        prompt_status="🎙️ Ti ascolto... dimmi il titolo!",
                        timeout=14.0
                    )

            import threading
            threading.Thread(target=_arm_interactive_listening, daemon=True).start()

    def _on_error(self, err_msg: str):
        """Gestione errori durante la richiesta a Ollama."""
        self.text_area.setPlainText(f"⚠️ Impossibile completare la risposta:\n{err_msg}")
        self.status_label.setText("Errore di connessione")
        self.status_label.setStyleSheet("color: #f43f5e; font-size: 10px;")
        self.auto_hide_timer.start(12000)

        if self.orb:
            self.orb.show_whis_response(f"⚠️ Impossibile completare la risposta:\n{err_msg}", {"error": True})

    def _on_audio_btn_clicked(self):
        """Pulsante rapido: ferma la voce se sta parlando, altrimenti alterna tra Voce e Muto."""
        if audio_feedback.is_speaking():
            audio_feedback.stop()
            return
        cur_mode = audio_feedback.get_mode()
        if cur_mode == MODE_MUTE:
            audio_feedback.set_mode(MODE_VOICE)
        else:
            audio_feedback.set_mode(MODE_MUTE)

    def _sync_audio_button(self, mode: str):
        """Aggiorna l'icona, il colore e il tooltip del pulsante audio rapido."""
        if not hasattr(self, "audio_btn") or not self.audio_btn:
            return
        if mode == MODE_MUTE:
            self.audio_btn.setText("🔇")
            self.audio_btn.setToolTip("Audio Muto (clicca per attivare Voce TTS)")
            self.audio_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1a1d28;
                    color: #94a3b8;
                    border: 1px solid #2d3345;
                    border-radius: 6px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #262a3a;
                    color: #f8fafc;
                    border-color: #f43f5e;
                }
                QPushButton:pressed {
                    background-color: #0f1118;
                }
            """)
        elif mode == MODE_SOFT_CHIME:
            self.audio_btn.setText("🔔")
            self.audio_btn.setToolTip("Suono Morbido (clicca per passare a Muto)")
            self.audio_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1a1d28;
                    color: #fbbf24;
                    border: 1px solid #2d3345;
                    border-radius: 6px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #262a3a;
                    color: #f8fafc;
                    border-color: #fbbf24;
                }
                QPushButton:pressed {
                    background-color: #0f1118;
                }
            """)
        else:
            self.audio_btn.setText("🔊")
            self.audio_btn.setToolTip("Voce (TTS) attiva (clicca per silenziare o fermare)")
            self.audio_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1a1d28;
                    color: #38bdf8;
                    border: 1px solid #2d3345;
                    border-radius: 6px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #262a3a;
                    color: #f8fafc;
                    border-color: #38bdf8;
                }
                QPushButton:pressed {
                    background-color: #0f1118;
                }
            """)

    def _on_copy_clicked(self):
        """Copia la risposta negli appunti con feedback visivo temporaneo."""
        text = self._current_response_text or self.text_area.toPlainText()
        if text:
            try:
                pyperclip.copy(text)
                self.copy_btn.setText("✓ Copiato!")
                self.copy_btn.setStyleSheet("""
                    QPushButton {
                        background-color: rgba(34, 197, 94, 0.2);
                        color: #4ade80;
                        border: 1px solid rgba(34, 197, 94, 0.4);
                        border-radius: 6px;
                        padding: 3px 9px;
                        font-size: 11px;
                        font-weight: 600;
                    }
                """)
                QTimer.singleShot(2200, self._reset_copy_btn)
            except Exception:
                pass

    def _reset_copy_btn(self):
        self.copy_btn.setText("📋 Copia")
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #1a1d28;
                color: #94a3b8;
                border: 1px solid #2d3345;
                border-radius: 6px;
                padding: 3px 9px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #262a3a;
                color: #f8fafc;
                border-color: #38bdf8;
            }
        """)

    def show_followup_listening(self, prompt_text: str = "🎙️ Ti ascolto... dimmi il titolo!"):
        """Attiva l'ascolto per la risposta dell'utente preservando la domanda di Whis a schermo."""
        self.auto_hide_timer.stop()
        self.card.setStyleSheet("""
            QFrame#whis_card {
                background-color: #0f1118;
                border: 2px solid #38bdf8;
                border-radius: 12px;
            }
        """)
        self.status_label.setText(prompt_text)
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: 700;")
        self.status_label.show()
        if self.orb:
            self.orb.set_whis_state("LISTENING")
        else:
            self.slide_in()

    def show_wake_listening(self):
        """Apre l'HUD in modalità Fase 2 Alexa: 'In ascolto... Dimmi pure!'."""
        self.auto_hide_timer.stop()
        audio_feedback.stop()

        if self.worker and self.worker.isRunning():
            try:
                self.worker.terminate()
                self.worker.wait(100)
            except Exception:
                pass

        self._current_response_text = ""
        self.copy_btn.setText("📋 Copia")

        # Bordo ciano più marcato (2px) in modalità ascolto
        self.card.setStyleSheet("""
            QFrame#whis_card {
                background-color: #0f1118;
                border: 2px solid #38bdf8;
                border-radius: 12px;
            }
        """)

        self.query_label.hide()
        self.clip_badge.hide()
        self.screen_badge.hide()
        self.source_badge.hide()
        self.calendar_badge.hide()
        self.model_badge.setText("⚡ Whis")

        self.text_area.setPlainText("")
        self.status_label.setText("🎙️ In ascolto... Dimmi pure!")
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: 700;")
        self.status_label.show()

        if self.orb:
            self.orb.set_whis_state("LISTENING")
        else:
            self.slide_in()

    def set_listening_status(self, text: str):
        """Aggiorna lo stato acustico/vocale in tempo reale nell'HUD."""
        if not (self.worker and self.worker.isRunning()):
            self.status_label.setText(text)

    def dismiss_wake(self):
        """Chiude l'HUD se è trascorso il timeout senza comandi ricevuti."""
        audio_feedback.stop()
        if self.orb:
            self.orb.dismiss_whis()
        if not (self.worker and self.worker.isRunning()):
            self.slide_out()

    def show_latest(self):
        """Mostra di nuovo l'HUD laterale con l'ultima risposta generata (o dal DB)."""
        if not self._current_response_text:
            try:
                from database import db
                history = db.get_whis_history(limit=1)
                if history:
                    item = history[0]
                    self._current_response_text = item.get("whis_response", "")
                    q = item.get("user_query", "")
                    self.query_label.setText(f'💬 "{q[:60]}..."' if len(q) > 60 else f'💬 "{q}"')
                    self.query_label.show()
                    try:
                        self.text_area.setMarkdown(self._current_response_text)
                    except Exception:
                        self.text_area.setPlainText(self._current_response_text)
                    model_str = item.get("model_used", "3b")
                    self.model_badge.setText("🧠 7B Deep" if "7b" in model_str.lower() else "⚡ 3B Fast")
                    self.status_label.setText("Ultima risposta caricata")
            except Exception:
                pass

        if self._current_response_text:
            self.slide_in()
            self.auto_hide_timer.start(18000)

    def slide_in(self):
        """Fa scorrere la finestra fuori dal bordo destro dello schermo."""
        hidden_x, target_x, target_y = self._get_target_geometry()

        if not self.isVisible():
            self.move(hidden_x, target_y)
            self.show()

        self.anim = QPropertyAnimation(self, b"pos")
        self.anim.setDuration(320)
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(QPoint(target_x, target_y))
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.start()

    def slide_out(self):
        """Fa rientrare la finestra nel bordo destro e la nasconde."""
        self.auto_hide_timer.stop()
        audio_feedback.stop()
        hidden_x, _, target_y = self._get_target_geometry()

        self.anim_out = QPropertyAnimation(self, b"pos")
        self.anim_out.setDuration(260)
        self.anim_out.setStartValue(self.pos())
        self.anim_out.setEndValue(QPoint(hidden_x, target_y))
        self.anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self.anim_out.finished.connect(self._on_slide_out_finished)
        self.anim_out.start()

    def _on_slide_out_finished(self):
        self.hide()

    def closeEvent(self, event):
        audio_feedback.remove_mode_listener(self._sync_audio_button)
        audio_feedback.stop()
        super().closeEvent(event)

