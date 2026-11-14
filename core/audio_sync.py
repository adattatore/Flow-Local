"""
Gestione archiviazione audio delle sbobine, salvataggio/caricamento della mappa
timestamp (.sync.json) e allineamento progressivo parola-per-parola tra
trascrizione Whisper e testo renderizzato nel QTextDocument di FlowNotes.
"""

from __future__ import annotations

import bisect
import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_NOTES_DIR = os.path.join(_PROJECT_ROOT, "data", "audio_notes")

_TOKEN_RE = re.compile(r"\w+(?:['’]\w+)?", re.UNICODE)


def ensure_audio_notes_dir() -> str:
    """Crea (se mancante) e restituisce la directory dedicata alle tracce audio delle note."""
    os.makedirs(AUDIO_NOTES_DIR, exist_ok=True)
    return AUDIO_NOTES_DIR


def get_sync_json_path(note_id: int) -> str:
    """Restituisce il percorso del file .sync.json associato all'ID della nota."""
    ensure_audio_notes_dir()
    return os.path.join(AUDIO_NOTES_DIR, f"note_{int(note_id)}.sync.json")


def _to_stored_audio_path(abs_path: str) -> str:
    """Converte un percorso dentro il progetto in percorso relativo portabile."""
    try:
        rel = os.path.relpath(abs_path, _PROJECT_ROOT)
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    except Exception:
        pass
    return abs_path


def resolve_audio_path(stored_path: str) -> str:
    """Risolve un percorso salvato (relativo o assoluto) in percorso assoluto locale."""
    if not stored_path:
        return ""
    if os.path.isabs(stored_path):
        return stored_path
    return os.path.normpath(os.path.join(_PROJECT_ROOT, stored_path))


def archive_audio_for_note(note_id: int, source_audio_path: str) -> str:
    """
    Copia il file audio/video sorgente in data/audio_notes/note_<id><ext>
    e restituisce il percorso normalizzato da salvare nei metadati.
    """
    if not source_audio_path or not os.path.exists(source_audio_path):
        return ""
    ensure_audio_notes_dir()
    _, ext = os.path.splitext(source_audio_path)
    ext = ext.lower() if ext else ".mp3"
    dest_abs = os.path.join(AUDIO_NOTES_DIR, f"note_{int(note_id)}{ext}")
    src_norm = os.path.normcase(os.path.abspath(source_audio_path))
    dst_norm = os.path.normcase(os.path.abspath(dest_abs))
    if src_norm != dst_norm:
        shutil.copy2(source_audio_path, dest_abs)
    return _to_stored_audio_path(dest_abs)


def save_note_audio_sync(
    note_id: int,
    audio_path: str,
    segments: List[Dict[str, Any]],
) -> str:
    """
    Salva la mappa JSON dei timestamp di segmenti e parole accanto all'ID della nota:
    {"audio_path": "...", "segments": [{"start": 0.0, "end": 4.2, "text": "...", "words": [{"w": "...", "s": 0.0, "e": 0.4}]}]}
    """
    sync_path = get_sync_json_path(note_id)
    payload = {
        "audio_path": _to_stored_audio_path(audio_path) if os.path.isabs(audio_path) else audio_path,
        "segments": segments or [],
    }
    with open(sync_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return sync_path


def load_note_audio_sync(note_id: int) -> Optional[Dict[str, Any]]:
    """
    Carica la mappa audio-testo per la nota indicata, risolvendo il percorso audio.
    Restituisce None se la nota non ha audio associato o se il file audio non esiste.
    """
    if note_id is None:
        return None
    sync_path = os.path.join(AUDIO_NOTES_DIR, f"note_{int(note_id)}.sync.json")
    if not os.path.exists(sync_path):
        return None
    try:
        with open(sync_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    raw_audio = str(data.get("audio_path") or "").strip()
    abs_audio = resolve_audio_path(raw_audio)
    if not abs_audio or not os.path.exists(abs_audio):
        return None

    data["audio_path"] = abs_audio
    if not isinstance(data.get("segments"), list):
        data["segments"] = []
    return data


def delete_note_audio_sync(note_id: int) -> None:
    """Rimuove il file .sync.json e l'audio archiviato associati alla nota (se presenti)."""
    if note_id is None:
        return
    sync_path = os.path.join(AUDIO_NOTES_DIR, f"note_{int(note_id)}.sync.json")
    audio_to_remove = ""
    if os.path.exists(sync_path):
        try:
            with open(sync_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("audio_path"):
                cand = resolve_audio_path(str(data["audio_path"]))
                if os.path.abspath(cand).startswith(os.path.abspath(AUDIO_NOTES_DIR)):
                    audio_to_remove = cand
        except Exception:
            pass
        try:
            os.remove(sync_path)
        except Exception:
            pass
    if audio_to_remove and os.path.exists(audio_to_remove):
        try:
            os.remove(audio_to_remove)
        except Exception:
            pass


@dataclass(frozen=True)
class DocToken:
    char_start: int
    char_end: int
    norm: str


@dataclass(frozen=True)
class SyncedSpan:
    char_start: int
    char_end: int
    start_ms: int
    end_ms: int


def _normalize_word(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^\w]", "", text.lower(), flags=re.UNICODE)
    return cleaned


def tokenize_document_blocks(blocks: List[Tuple[int, str]]) -> List[DocToken]:
    """
    Estrae i token parola con gli offset di carattere esatti nel QTextDocument
    a partire da una lista di tuple (block_position, block_text).
    """
    tokens: List[DocToken] = []
    for base_pos, block_text in blocks:
        if not block_text:
            continue
        for m in _TOKEN_RE.finditer(block_text):
            norm = _normalize_word(m.group(0))
            if not norm:
                continue
            tokens.append(
                DocToken(
                    char_start=base_pos + m.start(),
                    char_end=base_pos + m.end(),
                    norm=norm,
                )
            )
    return tokens


def build_doc_tokens_from_qtextdocument(doc: Any) -> List[DocToken]:
    """Costruisce la lista di DocToken direttamente da un QTextDocument PyQt6,
    ignorando eventuali blocchi 'ai_note' (riassunti/note AI espandibili) per
    preservare l'allineamento 1:1 del karaoke con il parlato."""
    from PyQt6.QtGui import QTextFormat
    prop_block_kind = QTextFormat.Property.UserProperty + 1
    blocks: List[Tuple[int, str]] = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().property(prop_block_kind) != "ai_note":
            blocks.append((block.position(), block.text()))
        block = block.next()
    return tokenize_document_blocks(blocks)


class AudioTextAligner:
    """
    Allinea progressivamente le parole timestampate da Faster-Whisper ai token
    del testo nell'editor QTextEdit, gestendo riformattazioni LLM, sommari iniziali
    e modifiche manuali dell'utente.
    """

    @staticmethod
    def _flatten_audio_words(segments: List[Dict[str, Any]]) -> List[Tuple[str, int, int]]:
        flat: List[Tuple[str, int, int]] = []
        for seg in segments or []:
            seg_start = float(seg.get("start", 0.0) or 0.0)
            seg_end = float(seg.get("end", seg_start) or seg_start)
            words = seg.get("words") or []
            if words:
                for w in words:
                    w_str = str(w.get("w") or "").strip()
                    s_sec = float(w.get("s", seg_start) or seg_start)
                    e_sec = float(w.get("e", s_sec) or s_sec)
                    # Un token Whisper può contenere punteggiatura o apostrofi; spezziamo in sub-token se necessario
                    sub_matches = list(_TOKEN_RE.finditer(w_str))
                    if not sub_matches:
                        continue
                    total_sub = len(sub_matches)
                    dur_ms = max(10, int(round((e_sec - s_sec) * 1000.0)))
                    base_ms = int(round(s_sec * 1000.0))
                    for idx, sm in enumerate(sub_matches):
                        norm = _normalize_word(sm.group(0))
                        if not norm:
                            continue
                        sub_s = base_ms + (idx * dur_ms) // total_sub
                        sub_e = base_ms + ((idx + 1) * dur_ms) // total_sub
                        flat.append((norm, sub_s, max(sub_s + 10, sub_e)))
            else:
                seg_text = str(seg.get("text") or "").strip()
                sub_matches = list(_TOKEN_RE.finditer(seg_text))
                if not sub_matches:
                    continue
                total_sub = len(sub_matches)
                base_ms = int(round(seg_start * 1000.0))
                dur_ms = max(20, int(round((seg_end - seg_start) * 1000.0)))
                for idx, sm in enumerate(sub_matches):
                    norm = _normalize_word(sm.group(0))
                    if not norm:
                        continue
                    sub_s = base_ms + (idx * dur_ms) // total_sub
                    sub_e = base_ms + ((idx + 1) * dur_ms) // total_sub
                    flat.append((norm, sub_s, max(sub_s + 10, sub_e)))
        return flat

    @classmethod
    def align(
        cls,
        segments: List[Dict[str, Any]],
        doc_tokens: List[DocToken],
    ) -> List[SyncedSpan]:
        """
        Esegue l'allineamento progressivo tra i segmenti/parole audio e i token del documento.
        Restituisce una lista ordinata di SyncedSpan per highlight e seek.
        """
        audio_words = cls._flatten_audio_words(segments)
        if not audio_words or not doc_tokens:
            return []

        a_norms = [aw[0] for aw in audio_words]
        d_norms = [dt.norm for dt in doc_tokens]

        matcher = difflib.SequenceMatcher(None, a_norms, d_norms, autojunk=False)
        opcodes = matcher.get_opcodes()

        # Prima passata: assegna timestamp esatti sugli "equal" e interpolati sui "replace"
        assigned: List[Optional[Tuple[int, int]]] = [None] * len(doc_tokens)

        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                for offset in range(i2 - i1):
                    _, s_ms, e_ms = audio_words[i1 + offset]
                    assigned[j1 + offset] = (s_ms, e_ms)
            elif tag == "replace":
                d_count = j2 - j1
                a_count = i2 - i1
                if d_count > 0 and a_count > 0:
                    span_s = audio_words[i1][1]
                    span_e = max(span_s + 20, audio_words[i2 - 1][2])
                    total_dur = span_e - span_s
                    for k in range(d_count):
                        s_ms = span_s + (k * total_dur) // d_count
                        e_ms = span_s + ((k + 1) * total_dur) // d_count
                        assigned[j1 + k] = (s_ms, max(s_ms + 10, e_ms))

        # Seconda passata: interpola eventuali gap interni (insert) tra ancore adiacenti,
        # lasciando non mappati eventuali blocchi iniziali puramente redazionali (es. Sommario Esecutivo)
        n = len(assigned)
        idx = 0
        while idx < n:
            if assigned[idx] is not None:
                idx += 1
                continue
            gap_start = idx
            while idx < n and assigned[idx] is None:
                idx += 1
            gap_end = idx  # escluso

            prev_anchor = assigned[gap_start - 1] if gap_start > 0 else None
            next_anchor = assigned[gap_end] if gap_end < n else None

            if prev_anchor is not None and next_anchor is not None:
                g_s = prev_anchor[1]
                g_e = max(g_s, next_anchor[0])
                gap_len = gap_end - gap_start
                if g_e > g_s and gap_len > 0:
                    dur = g_e - g_s
                    for k in range(gap_len):
                        s_ms = g_s + (k * dur) // gap_len
                        e_ms = g_s + ((k + 1) * dur) // gap_len
                        assigned[gap_start + k] = (s_ms, max(s_ms + 10, e_ms))
                else:
                    # Gap senza durata apprezzabile tra due parole contigue
                    for k in range(gap_len):
                        assigned[gap_start + k] = (g_s, g_s + 20)

        spans: List[SyncedSpan] = []
        for dt, ts in zip(doc_tokens, assigned):
            if ts is None:
                continue
            spans.append(
                SyncedSpan(
                    char_start=dt.char_start,
                    char_end=dt.char_end,
                    start_ms=ts[0],
                    end_ms=ts[1],
                )
            )
        return spans

    @staticmethod
    def find_span_at_ms(spans: List[SyncedSpan], pos_ms: int) -> Optional[SyncedSpan]:
        """Individua lo span di documento attivo al millisecondo corrente in O(log N)."""
        if not spans:
            return None
        starts = [sp.start_ms for sp in spans]
        idx = bisect.bisect_right(starts, pos_ms) - 1
        if idx < 0:
            # Prima della prima parola: evidenzia solo se siamo entro 300ms dall'inizio
            if spans[0].start_ms - pos_ms <= 300:
                return spans[0]
            return None
        cand = spans[idx]
        if cand.start_ms <= pos_ms <= cand.end_ms + 250:
            return cand
        # Se siamo in una pausa breve tra due parole, mantieni la parola precedente fino a 600ms
        if idx + 1 < len(spans) and pos_ms < spans[idx + 1].start_ms:
            if pos_ms - cand.end_ms <= 600:
                return cand
        return None

    @staticmethod
    def find_ms_at_char(spans: List[SyncedSpan], char_pos: int) -> Optional[int]:
        """Trova il timestamp (ms) corrispondente alla posizione del cursore nell'editor."""
        if not spans:
            return None
        char_starts = [sp.char_start for sp in spans]
        idx = bisect.bisect_right(char_starts, char_pos) - 1
        if idx < 0:
            return spans[0].start_ms
        cand = spans[idx]
        if cand.char_start <= char_pos <= cand.char_end:
            return cand.start_ms
        if idx + 1 < len(spans):
            nxt = spans[idx + 1]
            # Scegli lo span più vicino al clic
            if (nxt.char_start - char_pos) < (char_pos - cand.char_end):
                return nxt.start_ms
        return cand.start_ms
