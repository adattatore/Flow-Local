"""
transcriber.py — Modulo di compatibilità top-level che riesporta `core.transcriber`.
"""

from core.transcriber import (
    DEFAULT_WHISPER_INITIAL_PROMPT,
    normalize_phonetic_variants,
    normalize_post_transcription,
    FlowLocalEngine,
    LLMRefiner,
    StreamingChunkSession,
    normalize_audio_robust,
    compute_dynamic_beam_size,
    join_streaming_chunks,
    AudioDucker,
    audio_ducker,
    WHIS_WAKE_REGEX,
    strip_vocal_fillers,
)

__all__ = [
    "DEFAULT_WHISPER_INITIAL_PROMPT",
    "normalize_phonetic_variants",
    "normalize_post_transcription",
    "FlowLocalEngine",
    "LLMRefiner",
    "StreamingChunkSession",
    "normalize_audio_robust",
    "compute_dynamic_beam_size",
    "join_streaming_chunks",
    "AudioDucker",
    "audio_ducker",
    "WHIS_WAKE_REGEX",
    "strip_vocal_fillers",
]
