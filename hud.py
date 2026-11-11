"""
Modulo di compatibilità retroattiva per hud.py
Re-indirizza a ui.voice_orb.
"""
from ui.voice_orb import VoiceOrbOverlay, AudioAnalyzer

__all__ = ["VoiceOrbOverlay", "AudioAnalyzer"]
