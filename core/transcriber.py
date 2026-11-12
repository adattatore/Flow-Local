"""
Modulo di utilità e re-export per il trascrittore acustico Faster-Whisper di FlowLocal.
Tutte le operazioni fanno riferimento a core.ai_pipeline.
"""

from core.ai_pipeline import (
    DEFAULT_WHISPER_INITIAL_PROMPT,
    normalize_phonetic_variants,
    FlowLocalEngine,
    WHIS_WAKE_REGEX,
    strip_vocal_fillers,
)

__all__ = [
    "DEFAULT_WHISPER_INITIAL_PROMPT",
    "normalize_phonetic_variants",
    "FlowLocalEngine",
    "WHIS_WAKE_REGEX",
    "strip_vocal_fillers",
]

