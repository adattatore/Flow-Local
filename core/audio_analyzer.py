"""
Audio Analyzer & FFT Processing per FlowLocal
Estrae 12 bande vocali in tempo reale (85Hz - 3400Hz), le specchia in 24 punti polari
con intensity=0.80 e smoothing=0.50 su CPU (zero carico sulla VRAM della RTX 5060).
"""

import math
import collections
import numpy as np


class AudioAnalyzer:
    def __init__(self, num_half_bands: int = 12, intensity: float = 0.80, smoothing: float = 0.50):
        self.num_half_bands = num_half_bands
        self.num_points = num_half_bands * 2
        self.target_levels = np.zeros(self.num_points)
        self.current_levels = np.zeros(self.num_points)

        self.intensity = intensity      # 0.80 da specifica
        self.smoothing = smoothing      # 0.50 da specifica (EMA)

        # Pre-buffer circolare da 350ms (7 chunk da 50ms)
        self.ring_buffer = collections.deque(maxlen=7)

    def process_chunk(self, audio_data: np.ndarray):
        """Elabora un blocco audio proveniente dallo stream WASAPI continuo."""
        try:
            if audio_data is None or len(audio_data) == 0:
                return

            if audio_data.ndim > 1:
                audio = audio_data[:, 0]
            else:
                audio = audio_data

            # Salva nel pre-buffer
            self.ring_buffer.append(audio_data)

            # Prendi gli ultimi 512 campioni per massima reattività temporale
            if len(audio) > 512:
                sample = audio[-512:]
            elif len(audio) >= 128:
                sample = audio
            else:
                return

            windowed = sample * np.hanning(len(sample))
            fft_mag = np.abs(np.fft.rfft(windowed))

            # Isola la banda vocale umana (~85 Hz - 3400 Hz)
            max_idx = min(len(fft_mag), 110)
            vocal_spectrum = fft_mag[3:max_idx]

            if len(vocal_spectrum) < self.num_half_bands:
                return

            chunks = np.array_split(vocal_spectrum, self.num_half_bands)
            half_vals = np.array([math.sqrt(np.mean(c)) for c in chunks])
            half_vals = np.clip(half_vals * 0.28 * self.intensity, 0.0, 1.0)

            # Specchia i 12 valori sui due lati del cerchio (24 punti totali)
            self.target_levels = np.concatenate([half_vals, half_vals[::-1]])
        except Exception:
            pass

    def update(self, active: bool) -> np.ndarray:
        """Calcola i livelli attuali tramite interpolazione esponenziale (EMA)."""
        target = self.target_levels if active else np.zeros(self.num_points)
        self.current_levels = (
            self.smoothing * target + (1.0 - self.smoothing) * self.current_levels
        )
        return self.current_levels

