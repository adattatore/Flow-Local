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
    LLMRefiner,
    StreamingChunkSession,
    normalize_audio_robust,
    compute_dynamic_beam_size,
    join_streaming_chunks,
    WHIS_WAKE_REGEX,
    strip_vocal_fillers,
)
from core.audio_ducker import AudioDucker, audio_ducker


_CARDINAL_WORDS = {
    "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
}
_ORDINAL_WORDS = {
    "primo": 1, "secondo": 2, "terzo": 3, "quarto": 4, "quinto": 5,
    "sesto": 6, "settimo": 7, "ottavo": 8, "nono": 9, "decimo": 10,
}

# Matcha marcatori di elenco parlati:
# - "Uno, ..." / "Uno… ..." / "Due: ..."
# - "Primo punto, ..." / "Secondo punto ..."
# - "Punto uno, ..." / "Punto 2: ..."
# - "Primo, ..." / "Secondo, ..."
_SPOKEN_MARKER_RE = re.compile(
    r"(?:(?<=^)|(?<=[\.\!\?\:\;\,\…\n]\s)|(?<=[\.\!\?\:\;\,\…\n])|(?<=\s))"
    r"(?:"
    r"(?P<p_num>punto\s+(?:uno|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|\d{1,2}))"
    r"|(?P<ord_pt>(?:primo|secondo|terzo|quarto|quinto|sesto|settimo|ottavo|nono|decimo)\s+punto)"
    r"|(?P<card>(?:uno|due|tre|quattro|cinque|sei|sette|otto|nove|dieci))(?=\s*(?:[,:\;\.…]|\.\.\.))"
    r"|(?P<ord>(?:primo|secondo|terzo|quarto|quinto|sesto|settimo|ottavo|nono|decimo))(?=\s*(?:[,:\;…]|\.\.\.))"
    r")"
    r"(?:\s*(?:\.\.\.|[,:\;\.\-–—…]))*\s*",
    re.IGNORECASE,
)


def _marker_rank(match: re.Match) -> tuple[str, int]:
    if match.group("p_num"):
        tail = match.group("p_num").split()[-1].lower()
        return ("punto", int(tail) if tail.isdigit() else _CARDINAL_WORDS.get(tail, 0))
    if match.group("ord_pt"):
        head = match.group("ord_pt").split()[0].lower()
        return ("ord_pt", _ORDINAL_WORDS.get(head, 0))
    if match.group("card"):
        return ("card", _CARDINAL_WORDS.get(match.group("card").lower(), 0))
    if match.group("ord"):
        return ("ord", _ORDINAL_WORDS.get(match.group("ord").lower(), 0))
    return ("", 0)


def format_spoken_lists(text: str) -> str:
    """
    Riconosce elenchi parlati strutturati (es. 'Uno, ... Due, ... Tre, ...',
    'Primo punto... Secondo punto...', 'Punto uno... Punto due...') e li formatta
    in elenco Markdown pulito con trattino '- ' e iniziale maiuscola.
    """
    if not text or len(text) < 8:
        return text

    matches = list(_SPOKEN_MARKER_RE.finditer(text))
    if len(matches) < 2:
        return text

    # Verifica che ci sia una sequenza coerente (almeno 2 marcatori crescenti dello stesso tipo, partendo da 1 o 2)
    best_seq: list[re.Match] = []
    current_seq: list[re.Match] = []
    cur_kind = ""
    cur_rank = 0

    for m in matches:
        kind, rank = _marker_rank(m)
        if rank <= 0:
            continue
        if not current_seq:
            if rank == 1:
                current_seq = [m]
                cur_kind = kind
                cur_rank = rank
        elif kind == cur_kind and rank == cur_rank + 1:
            current_seq.append(m)
            cur_rank = rank
        else:
            if len(current_seq) >= 2 and len(current_seq) > len(best_seq):
                best_seq = current_seq
            if rank == 1:
                current_seq = [m]
                cur_kind = kind
                cur_rank = rank
            else:
                current_seq = []
                cur_kind = ""
                cur_rank = 0

    if len(current_seq) >= 2 and len(current_seq) > len(best_seq):
        best_seq = current_seq

    if len(best_seq) < 2:
        return text

    intro = text[: best_seq[0].start()].strip()
    # Rimuove eventuale punteggiatura isolata residua alla fine dell'intro (es. ","), mantenendo ":"
    if intro.endswith(","):
        intro = intro[:-1].rstrip()

    lines: list[str] = []
    if intro:
        lines.append(intro)

    for idx, m in enumerate(best_seq):
        start_body = m.end()
        end_body = best_seq[idx + 1].start() if idx + 1 < len(best_seq) else len(text)
        body = text[start_body:end_body].strip()
        # Rimuove eventuale punteggiatura iniziale residua
        body = re.sub(r"^[,:\;\-–—\s]+", "", body).strip()
        if not body:
            continue
        body = body[0].upper() + body[1:]
        lines.append(f"- {body}")

    return "\n".join(lines) if len(lines) >= 2 else text


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
    "format_spoken_lists",
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
