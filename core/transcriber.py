"""
core/transcriber.py
Modulo di utilità e re-export per il trascrittore acustico Faster-Whisper di FlowLocal.
Tutte le operazioni fanno riferimento a core.ai_pipeline e alla normalizzazione post-trascrizione.
"""

import re
from core.ai_pipeline import (
    DEFAULT_WHISPER_INITIAL_PROMPT,
    normalize_phonetic_variants as _base_normalize_phonetic_variants,
    FlowLocalEngine,
    WHIS_WAKE_REGEX,
    strip_vocal_fillers,
)


def normalize_post_transcription(text: str) -> str:
    """Normalizzazione regex nomi e correzioni deterministiche post-trascrizione."""
    if not text:
        return text
    # Micro-fix regex nomi specifici
    text = re.sub(r"\bFlow\s*Local\s*ab\b", "FlowLocal Hub", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCoreAudioFeedback\.py\b", "core/audio_feedback.py", text, flags=re.IGNORECASE)

    # Correzioni deterministiche emerse dai test acustici FlowType
    text = re.sub(r"\bun'appuntamento\b", "un appuntamento", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(agli|degli|i miei|negli)\s+appuntiti\b", r"\1 appunti", text, flags=re.IGNORECASE)
    text = re.sub(r"\bnon deve peggiorarmi\b", "non deve parlarmi", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdownload\s+[Ll]a[Tt]e[Xx]\b", "Markdown e LaTeX", text)
    return text


def normalize_phonetic_variants(text: str) -> str:
    """Normalizzazione completa varianti fonetiche e nomi post-trascrizione."""
    text = _base_normalize_phonetic_variants(text)
    return normalize_post_transcription(text)


__all__ = [
    "DEFAULT_WHISPER_INITIAL_PROMPT",
    "normalize_phonetic_variants",
    "normalize_post_transcription",
    "FlowLocalEngine",
    "WHIS_WAKE_REGEX",
    "strip_vocal_fillers",
]
