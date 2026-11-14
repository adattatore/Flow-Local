"""
Barra player audio contestuale compatta per le sbobine di FlowNotes.
Gestisce riproduzione via PyQt6.QtMultimedia (QMediaPlayer + QAudioOutput),
salto rapido -10s / +10s, slider fluido mm:ss / mm:ss e velocità ciclica
1.0x -> 1.25x -> 1.5x -> 2.0x.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)


def _format_ms(ms: int) -> str:
    total_sec = max(0, int(ms) // 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class AudioSyncBar(QFrame):
    """Smart Audio Bar contestuale per le sbobine con sincronizzazione parola-per-parola."""

    positionChangedMs = pyqtSignal(int)
    playbackStateChanged = pyqtSignal(bool)

    SPEED_STEPS = (1.0, 1.25, 1.5, 2.0)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("AudioSyncBar")
        self.setFixedHeight(44)

        self._audio_path: str = ""
        self._duration_ms: int = 0
        self._is_scrubbing: bool = False
        self._speed_idx: int = 0

        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)

        self._build_ui()
        self._connect_player_signals()
        self.hide()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        self.play_btn = QPushButton("▶", self)
        self.play_btn.setObjectName("AudioSyncPlayBtn")
        self.play_btn.setFixedSize(30, 30)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setToolTip("Play / Pausa (Ctrl+Spazio nell'editor)")
        self.play_btn.clicked.connect(self.toggle_play_pause)
        layout.addWidget(self.play_btn)

        self.back_10_btn = QPushButton("-10s", self)
        self.back_10_btn.setObjectName("AudioSyncJumpBtn")
        self.back_10_btn.setFixedHeight(26)
        self.back_10_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_10_btn.setToolTip("Indietro di 10 secondi")
        self.back_10_btn.clicked.connect(lambda: self.jump_seconds(-10))
        layout.addWidget(self.back_10_btn)

        self.fwd_10_btn = QPushButton("+10s", self)
        self.fwd_10_btn.setObjectName("AudioSyncJumpBtn")
        self.fwd_10_btn.setFixedHeight(26)
        self.fwd_10_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fwd_10_btn.setToolTip("Avanti di 10 secondi")
        self.fwd_10_btn.clicked.connect(lambda: self.jump_seconds(10))
        layout.addWidget(self.fwd_10_btn)

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setObjectName("AudioSyncSlider")
        self.slider.setRange(0, 0)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        layout.addWidget(self.slider, 1)

        self.time_label = QLabel("00:00 / 00:00", self)
        self.time_label.setObjectName("AudioSyncTimeLabel")
        self.time_label.setMinimumWidth(92)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.time_label)

        self.speed_btn = QPushButton("1.0x", self)
        self.speed_btn.setObjectName("AudioSyncSpeedBtn")
        self.speed_btn.setFixedHeight(26)
        self.speed_btn.setMinimumWidth(46)
        self.speed_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed_btn.setToolTip("Cambia velocità di riproduzione (1.0x → 1.25x → 1.5x → 2.0x)")
        self.speed_btn.clicked.connect(self.cycle_speed)
        layout.addWidget(self.speed_btn)

    def _connect_player_signals(self):
        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.durationChanged.connect(self._on_player_duration_changed)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)

    def load_audio(self, audio_path: str):
        """Carica una traccia audio locale e mostra la barra."""
        if not audio_path or not os.path.exists(audio_path):
            self.unload()
            return
        norm_new = os.path.normcase(os.path.abspath(audio_path))
        norm_cur = os.path.normcase(os.path.abspath(self._audio_path)) if self._audio_path else ""
        if norm_new != norm_cur:
            self._player.stop()
            self._audio_path = audio_path
            self._duration_ms = 0
            self.slider.setRange(0, 0)
            self.slider.setValue(0)
            self.time_label.setText("00:00 / 00:00")
            self._player.setSource(QUrl.fromLocalFile(audio_path))
            self._player.setPlaybackRate(self.SPEED_STEPS[self._speed_idx])
        self.play_btn.setText(
            "⏸" if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else "▶"
        )
        self.show()

    def unload(self):
        """Ferma la riproduzione, scollega la sorgente e nasconde la barra."""
        self._player.stop()
        self._player.setSource(QUrl())
        self._audio_path = ""
        self._duration_ms = 0
        self.slider.setRange(0, 0)
        self.slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.play_btn.setText("▶")
        self.hide()

    def is_active(self) -> bool:
        return bool(self._audio_path) and self.isVisible()

    def toggle_play_pause(self):
        if not self._audio_path:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def seek_ms(self, pos_ms: int, auto_play: bool = False):
        if not self._audio_path:
            return
        target = max(0, int(pos_ms))
        if self._duration_ms > 0:
            target = min(target, self._duration_ms)
        self._player.setPosition(target)
        if not self._is_scrubbing:
            self.slider.setValue(target)
        self._update_time_label(target, self._duration_ms)
        self.positionChangedMs.emit(target)
        if auto_play and self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._player.play()

    def jump_seconds(self, delta_s: int):
        if not self._audio_path:
            return
        self.seek_ms(self._player.position() + int(delta_s * 1000))

    def cycle_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(self.SPEED_STEPS)
        rate = self.SPEED_STEPS[self._speed_idx]
        self._player.setPlaybackRate(rate)
        label = f"{rate:.2f}".rstrip("0")
        if label.endswith("."):
            label += "0"
        self.speed_btn.setText(f"{label}x")

    def _on_slider_pressed(self):
        self._is_scrubbing = True

    def _on_slider_moved(self, value: int):
        self._update_time_label(value, self._duration_ms)

    def _on_slider_released(self):
        self._is_scrubbing = False
        self.seek_ms(self.slider.value())

    def _on_player_duration_changed(self, duration_ms: int):
        self._duration_ms = max(0, int(duration_ms))
        self.slider.setRange(0, self._duration_ms)
        self._update_time_label(self._player.position(), self._duration_ms)

    def _on_player_position_changed(self, pos_ms: int):
        pos_ms = max(0, int(pos_ms))
        if not self._is_scrubbing:
            self.slider.setValue(pos_ms)
            self._update_time_label(pos_ms, self._duration_ms)
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.positionChangedMs.emit(pos_ms)

    def _on_player_state_changed(self, state):
        is_playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        self.play_btn.setText("⏸" if is_playing else "▶")
        self.playbackStateChanged.emit(is_playing)

    def _update_time_label(self, pos_ms: int, dur_ms: int):
        self.time_label.setText(f"{_format_ms(pos_ms)} / {_format_ms(dur_ms)}")
