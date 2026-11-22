"""
Core AI Pipeline per FlowLocal
Faster-Whisper Large-v3-Turbo (CUDA) + Analisi Pause (>1.3s) + Ollama Qwen 3.5 (Multimodale)
Supporta:
- Prompt Biasing e Vocabolario tecnico tramite Qwen 3.5
- Pre-filtro fonetico deterministico e Macro Snippet
- Riformattazione strutturale e rimozione autocorrezioni
- Command Mode e Study Mode
- Trascrizione affidabile a zero ritardo (senza falsi keyup)
"""

import os
import re
import sys
import math
import time
import json
import wave
import queue
import ctypes
import difflib
import tempfile
import threading
import subprocess
import urllib.error
import urllib.request
from typing import Tuple, Optional, Callable, List, Dict, Union, Any

# Compatibilità pyav >= 13/14 con faster-whisper:
# Le versioni recenti di PyAV hanno rimosso il parametro metadata_errors da av.open()
try:
    import av
    if hasattr(av, "open"):
        _orig_av_open = av.open
        def _safe_av_open(*args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return _orig_av_open(*args, **kwargs)
        av.open = _safe_av_open
    if hasattr(av, "container") and hasattr(av.container, "core") and hasattr(av.container.core, "open"):
        av.container.core.open = _safe_av_open
except Exception:
    pass

import numpy as np
import sounddevice as sd
from scipy import signal
import pyperclip
from faster_whisper import WhisperModel

from database import db
from core.audio_analyzer import AudioAnalyzer
from core.context_detector import detect_active_context
from core.audio_ducker import audio_ducker, AudioDucker
from core.clipboard_tracker import dictation_tracker
from core.hardware_profiler import get_active_profile
from core.gemini_client import (
    DEFAULT_GEMINI_TIMEOUT,
    DICTATION_GEMINI_TIMEOUT,
    generate_content as gemini_generate_content,
    get_ai_routing_config,
    mark_gemini_unavailable,
    set_active_runtime_engine,
    should_use_gemini,
)

RECORD_SAMPLE_RATE = 48000
WHISPER_SAMPLE_RATE = 16000
DECIMATE_FACTOR = 3  # 48000 / 3 = 16000
CHANNELS = 1

MODEL_SIZE = "large-v3-turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

DOUBLE_TAP_TIMEOUT = 0.35
LLM_MODEL = "qwen3.5:4b"
LLM_HIGH_PRECISION_MODEL = "qwen3.5:9b"
OLLAMA_API_URL = "http://localhost:11434/api/chat"


def strip_think_blocks(text: str) -> str:
    """Rimuove eventuali blocchi di ragionamento <think>...</think> dall'output LLM."""
    if not text:
        return ""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

FILLER_WORDS_REGEX = re.compile(
    r'\b(ehm+|uhm+|mhh+|ah+|eh+|vabbè)\b[\.\,\…]*\s*',
    flags=re.IGNORECASE
)

VOICE_LEARN_REGEX = re.compile(
    r'^(?:impara|correggi|salva(?: nel dizionario)?)\s+["\']?(.+?)["\']?\s+(?:in|con|come|si scrive)\s+["\']?(.+?)["\']?[.!?]?$',
    flags=re.IGNORECASE
)

QUICK_NOTE_TRIGGER_REGEX = re.compile(
    r'^(?:nuovo\s+appunto|prendi\s+nota(?:\s+che|\s+di)?|salva\s+nota(?:\s+che)?|ricordami\s+di|appunto)\b[\s,.:;—–-]*',
    flags=re.IGNORECASE
)

WHIS_WAKE_REGEX = re.compile(
    r'^(?:whis|wis|uis|quiz|vis|whiss|huiss?|ouis|whois)\b[\s,.:;—–-]*',
    flags=re.IGNORECASE
)

DEFAULT_WHISPER_INITIAL_PROMPT = (
    "FlowLocal, FlowNotes, FlowType, FlowStudy, Whis, PyQt6, Ollama, Qwen, LaTeX, Markdown."
)

WHISPER_HALLUCINATION_PHRASES = (
    "sottotitoli creati dalla comunità",
    "sottotitoli a cura di",
    "grazie per la visione",
    "grazie per aver guardato",
    "iscriviti al canale",
    "amara.org",
    "al prossimo video",
    "grazie per l'ascolto",
)


def is_whisper_hallucination(text: str) -> bool:
    """Rileva frasi fantasma tipiche di Whisper su silenzi o rumore ambientale."""
    if not text:
        return True
    norm = re.sub(r"[\s.,!?;:…\"'()\-–—]+", " ", text.lower()).strip()
    if not norm:
        return True
    for phrase in WHISPER_HALLUCINATION_PHRASES:
        if norm == phrase or (len(norm) <= len(phrase) + 10 and phrase in norm):
            return True
    return False

LAST_SELECTION_CAPTURE_TIME = 0.0


def find_best_input_device():
    try:
        s = sd.InputStream(device=None, samplerate=16000, channels=1)
        s.start()
        s.stop()
        s.close()
        return None, 16000
    except Exception:
        pass

    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d.get('max_input_channels', 0) > 0:
                sr = int(d.get('default_samplerate', 16000))
                try:
                    s = sd.InputStream(device=i, samplerate=sr, channels=1)
                    s.start()
                    s.stop()
                    s.close()
                    return i, sr
                except Exception:
                    continue
    except Exception:
        pass
    return None, 16000


# Rimozione completa dei bip da Caps Lock / dettatura (100% silenzioso)
def sound_start():
    pass

def sound_locked():
    pass

def sound_done():
    pass

def sound_toggle_pause(paused: bool):
    pass


def _send_ctrl_shortcut(key_char: str, hold_s: float = 0.015) -> None:
    """Invia Ctrl+<tasto> su Windows (user32.keybd_event) o Linux (pynput)."""
    if sys.platform == "win32":
        vk = 0x56 if key_char.lower() == "v" else 0x43
        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)
        try:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(hold_s)
            user32.keybd_event(vk, 0, 0x0002, 0)
        finally:
            user32.keybd_event(0x11, 0, 0x0002, 0)
        return

    from pynput.keyboard import Controller, Key
    kb = Controller()
    with kb.pressed(Key.ctrl):
        kb.press(key_char.lower())
        time.sleep(hold_s)
        kb.release(key_char.lower())


class WindowsInjector:
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002

    @classmethod
    def paste_text(cls, text: str):
        if not text:
            return

        # Rimuove eventuali backslash finali (\) o spazi superflui prima di incollare
        clean_text = text.rstrip()
        while clean_text.endswith('\\'):
            clean_text = clean_text[:-1].rstrip()

        if not clean_text:
            return

        pyperclip.copy(clean_text)
        time.sleep(0.025)
        _send_ctrl_shortcut("v", hold_s=0.015)

    @classmethod
    def get_target_point(cls) -> Optional[Tuple[int, int]]:
        """Restituisce le coordinate schermo (x, y) del caret di testo attivo o del campo/finestra target."""
        if sys.platform != "win32":
            return None
        try:
            from ctypes import wintypes

            class GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND),
                    ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND),
                    ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT),
                ]

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            tid = user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0

            gui = GUITHREADINFO()
            gui.cbSize = ctypes.sizeof(GUITHREADINFO)
            focus_hwnd = hwnd
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gui)):
                if gui.hwndFocus:
                    focus_hwnd = gui.hwndFocus
                if gui.hwndCaret and (gui.rcCaret.right > gui.rcCaret.left or gui.rcCaret.bottom > gui.rcCaret.top):
                    pt = wintypes.POINT(
                        (gui.rcCaret.left + gui.rcCaret.right) // 2,
                        (gui.rcCaret.top + gui.rcCaret.bottom) // 2,
                    )
                    if user32.ClientToScreen(gui.hwndCaret, ctypes.byref(pt)):
                        return int(pt.x), int(pt.y)

            # Fallback MSAA (OBJID_CARET = -8) per app Chromium/Electron/VS Code/Browser
            if focus_hwnd:
                try:
                    import comtypes
                    from comtypes import GUID
                    from comtypes.automation import VARIANT
                    comtypes.CoInitialize()
                    try:
                        # IID_IAccessible = {618736E0-3C3D-11CF-810C-00AA00389B71}
                        iid_iaccessible = GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")

                        class IAccessible(comtypes.IUnknown):
                            _iid_ = iid_iaccessible

                        # Chiama AccessibleObjectFromWindow via vtable/oleacc usando comtypes.gen/oleacc
                        oleacc = ctypes.windll.oleacc
                        p_acc = ctypes.c_void_p()
                        OBJID_CARET = ctypes.c_uint(0xFFFFFFF8)
                        hr = oleacc.AccessibleObjectFromWindow(
                            focus_hwnd,
                            OBJID_CARET,
                            ctypes.byref(iid_iaccessible),
                            ctypes.byref(p_acc),
                        )
                        if hr == 0 and p_acc.value:
                            # Chiamata diretta al metodo accLocation (vtable index 22 di IAccessible)
                            vtable = ctypes.cast(p_acc, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                            # accLocation(this, *pxLeft, *pyTop, *pcxWidth, *pcyHeight, varChild)
                            proto = ctypes.WINFUNCTYPE(
                                ctypes.HRESULT,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_long),
                                ctypes.POINTER(ctypes.c_long),
                                ctypes.POINTER(ctypes.c_long),
                                ctypes.POINTER(ctypes.c_long),
                                VARIANT,
                            )
                            acc_location = proto(vtable[22])
                            var_child = VARIANT(0)
                            x_left = ctypes.c_long(0)
                            y_top = ctypes.c_long(0)
                            w_box = ctypes.c_long(0)
                            h_box = ctypes.c_long(0)
                            hr_loc = acc_location(
                                p_acc,
                                ctypes.byref(x_left),
                                ctypes.byref(y_top),
                                ctypes.byref(w_box),
                                ctypes.byref(h_box),
                                var_child,
                            )
                            # Release COM pointer (vtable[2] = IUnknown::Release)
                            rel_proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
                            rel_proto(vtable[2])(p_acc)
                            if hr_loc == 0 and (w_box.value > 0 or h_box.value > 0) and (x_left.value != 0 or y_top.value != 0):
                                return int(x_left.value + w_box.value // 2), int(y_top.value + h_box.value // 2)
                    finally:
                        comtypes.CoUninitialize()
                except Exception:
                    pass

            if hwnd:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    cursor = wintypes.POINT()
                    if user32.GetCursorPos(ctypes.byref(cursor)):
                        if rect.left <= cursor.x <= rect.right and rect.top <= cursor.y <= rect.bottom:
                            return int(cursor.x), int(cursor.y)
                    return int((rect.left + rect.right) // 2), int(rect.top + (rect.bottom - rect.top) * 0.55)
        except Exception:
            pass
        return None


LATEX_FORMULA_RULE = (
    "Quando l'utente detta espressioni matematiche o fisiche a parole "
    "(es. 'integrale da 0 a infinito di e alla meno x in dx', 'F uguale m per a', derivate, vettori \\vec{}, frazioni, "
    "unità del Sistema Internazionale come 'metri al secondo quadro'), "
    "SOSTITUISCI direttamente quelle parole con il codice LaTeX standard racchiuso tra simboli $ "
    "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$, $\\vec{v}$, $\\mathrm{m/s^2}$). "
    "VIETATO ripetere sia la forma a parole sia il LaTeX (mai scrivere 'ovvero $...$' o 'cioè $...$')."
)

ANTI_HALLUCINATION_RULE = (
    "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. "
    "Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. "
    "Restituisci SOLO testo puro rifinito: MAI tag XML o HTML (<p>, </p>, <br>, <div>, <span>, <trascrizione>).\n"
    "FEDELTÀ INTEGRALE E DIVIETO DI PARAFRASI O RIASSUNTO:\n"
    "- Conserva TUTTE le frasi e TUTTE le parole di contenuto pronunciate dall'utente: è severamente vietato riassumere, tagliare frasi o sostituire vocaboli con sinonimi.\n"
    "- Limita gli interventi a punteggiatura, maiuscole, rimozione di esitazioni vuote (ehm, uhm) e sostituzione di formule matematiche con LaTeX $...$."
)


def strip_spurious_html_tags(text: str, raw_source: str = "") -> str:
    """Rimuove eventuali tag HTML spuri (<p>, <br>, <div>, <span>, ecc.) emessi dall'LLM
    quando non erano presenti nel testo parlato originale."""
    if not text:
        return text
    if raw_source and re.search(r"</?(?:p|br|div|span)\b", raw_source, flags=re.IGNORECASE):
        return text
    cleaned = re.sub(r"</p>\s*<p(?:\s[^>]*)?>", "\n\n", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:p|div|span|b|strong|i|em|ul|ol|li|html|body)(?:\s[^>]*)?>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

PUNCTUATION_RULE = (
    "PUNTEGGIATURA E PULIZIA NATURALE:\n"
    "- Inserisci punteggiatura fluida (virgole, punto fermo a fine frase, '?' solo per domande dirette; '.' per interrogative indirette come 'Volevo sapere se...').\n"
    "- Spezza con '.' periodi troppo lunghi uniti solo da virgole al cambio di proposizione.\n"
    "- Rimuovi esitazioni vocali (ehm, uhm, mmm, vabbè), doppi riempitivi ('diciamo, cioè' -> 'cioè') e falsi inizi tronchi tenendo solo la formulazione finale.\n"
    "- Correggi evidenti refusi morfologici da parlato veloce senza cambiare il lessico né parafrasare."
)


def apply_inline_voice_commands(text: str) -> str:
    """
    Applica comandi vocali inline di auto-correzione durante la dettatura.
    Esempio: 'Ok... Anzi, cancella tutto. Va bene, passiamo oltre' -> 'Va bene, passiamo oltre'.
    Richiede che il comando sia un enunciato isolato (delimitato da inizio/punteggiatura e fine/punteggiatura)
    per evitare falsi positivi in frasi normali come 'ho detto cancella tutto e non ha cancellato'.
    """
    if not text:
        return text

    reset_pattern = re.compile(
        r'(?:'
        r'(?:^|(?<=[.!?;\n])|(?<=\[PAUSA_CAMBIO_ARGOMENTO\]))\s*(?:(?:anzi|no|aspetta|scusa|ops)[,.:;\s]+)?'
        r'|'
        r'(?<=,)\s*(?:anzi|no|aspetta|scusa|ops)[,.:;\s]+'
        r')'
        r'(?:cancella\s+tutto|annulla\s+tutto|scarta\s+tutto|ricomincia(?:\s+da\s+capo)?)'
        r'(?=\s*(?:$|[.!?,;\n]|\[PAUSA_CAMBIO_ARGOMENTO\]))'
        r'[.!?,;\s]*',
        re.IGNORECASE,
    )

    matches = list(reset_pattern.finditer(text))
    if not matches:
        return text

    last_match = matches[-1]
    remaining = text[last_match.end():].strip()
    return remaining


def strip_pause_markers(text: str) -> str:
    """Converte eventuali tag interni [PAUSA_CAMBIO_ARGOMENTO] residui in interruzioni di paragrafo."""
    if not text:
        return text
    cleaned = re.sub(r'\s*\[PAUSA_CAMBIO_ARGOMENTO\]\s*', '\n\n', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def normalize_phonetic_variants(text: str) -> str:
    """Normalizzazione automatica post-Whisper / pre-LLM di varianti fonetiche comuni."""
    if not text:
        return text

    def _preserve_case(matched_word: str, replacement: str) -> str:
        if matched_word.isupper():
            return replacement.upper()
        if matched_word and matched_word[0].isupper():
            return replacement.capitalize()
        return replacement.lower()

    def _replace_sbobbina(match: re.Match) -> str:
        word = match.group(0)
        # Sostituisce la doppia 'bb' con 'b' (es. sbobbina -> sbobina, sbobbine -> sbobine, sbobbinare -> sbobinare)
        simplified = re.sub(r'bb', 'b', word, count=1, flags=re.IGNORECASE)
        return _preserve_case(word, simplified)

    # 1. Whis: varianti fonetiche
    text = re.sub(r'\b(WIS|Wis|wis|Wiz|wiz|Whizz|whizz|Uis|uis|uisss|uiss|huiss|WHOIS)\b', 'Whis', text)
    text = re.sub(r'\b[Ww]hois\b', 'Whis', text)

    # 2. FlowType, FlowStudy, FlowLocal, FlowNotes, FlowLocal Hub
    text = re.sub(r'\bFlow\s*[Ss]tud+y\b', 'FlowStudy', text, flags=re.IGNORECASE)
    text = re.sub(r'\bFlow\s*[Nn]otes?\b', 'FlowNotes', text, flags=re.IGNORECASE)
    text = re.sub(r'\bFlow[\s.,_-]*[Tt](?:ype|ap)\b', 'FlowType', text, flags=re.IGNORECASE)
    text = re.sub(r'\bFlow\s*Local\s*ab\b', 'FlowLocal Hub', text, flags=re.IGNORECASE)
    text = re.sub(r'\bflow\s*local\b', 'FlowLocal', text, flags=re.IGNORECASE)
    text = re.sub(r'\bRTX\s*5060\b', 'RTX 5060', text, flags=re.IGNORECASE)

    # 2b. Correzioni termini tecnici e grammaticali sicuri
    text = re.sub(r'\b[Gg]iacobian([aoe])\b', r'Jacobian\1', text)
    text = re.sub(r"\bun'appuntamento\b", "un appuntamento", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(agli|degli|i miei|negli)\s+appuntiti\b", r"\1 appunti", text, flags=re.IGNORECASE)

    # 2c. Rimozione intercalari specifici e correzione maiuscole
    text = re.sub(r',\s*diciamo,', ',', text, flags=re.IGNORECASE)
    text = re.sub(r'(^|[\.\!\?\n]\s*)Allora,\s*([a-zà-úA-Z])', lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r'(^|[\.\!\?\n]\s*)[Pp]raticamente,?\s*([a-zà-úA-Z])', lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r'(^|[\.\!\?\n]\s*)[Pp]raticamente,?\s*', r'\1', text)

    # 3. Allucinazione fonetica 'da scrittore' -> 'trascrittore' dopo parole chiave
    text = re.sub(r'\b(come|questo|un|il)\s+da\s+scrittore\b', r'\1 trascrittore', text, flags=re.IGNORECASE)

    # 4. 'mi sta casando' -> 'mi sta gasando'
    text = re.sub(r'\bmi\s+sta\s+casando\b', 'mi sta gasando', text, flags=re.IGNORECASE)

    # 5. Correzione automatica varianti fonetiche sbobbina/sbobbine e forme correlate
    text = re.sub(r'\b(sbobbin\w*)\b', _replace_sbobbina, text, flags=re.IGNORECASE)

    # 6. 'mentre che' -> 'mentre' (preserva maiuscola se a inizio frase)
    text = re.sub(r'\bMentre\s+che\b', 'Mentre', text)
    text = re.sub(r'\bmentre\s+che\b', 'mentre', text)

    # 7. 'in Idol' / 'modalità Idol' -> 'in idle' / 'modalità idle'
    text = re.sub(r'\bmodalit[aà]\s+idol\b', 'modalità idle', text, flags=re.IGNORECASE)
    text = re.sub(r'\bModalit[aà]\s+idol\b', 'Modalità idle', text)
    text = re.sub(r'\bin\s+idol\b', 'in idle', text, flags=re.IGNORECASE)
    text = re.sub(r'\bIn\s+idol\b', 'In idle', text)

    # 8. 'caps lock' -> 'Caps Lock'
    text = re.sub(r'\bcaps\s+lock\b', 'Caps Lock', text, flags=re.IGNORECASE)

    return text


def strip_vocal_fillers(text: str, capitalize_first: bool = True) -> str:
    """Rimuove esitazioni vocali pure e tic verbali vuoti (mmm, ehm, uhm) all'inizio o isolati."""
    if not text:
        return text
    # Rimuove esitazioni pure all'inizio del testo (es. 'mmm ', 'ehm, ', 'uhm... ')
    cleaned = re.sub(r'^(?:ehm|uhm|mmm|vabb[eè]|beh)\b[\s,.:;—–-]*', '', text, flags=re.IGNORECASE).strip()
    # Rimuove esitazioni isolate all'interno del testo
    cleaned = re.sub(r'\b(?:ehm|uhm|mmm)\b[\s,.:;—–-]*', '', cleaned, flags=re.IGNORECASE).strip()
    if capitalize_first and cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned or text


def normalize_audio_robust(audio_data: np.ndarray, max_gain: float = 12.0, noise_gate_p98: float = 0.006) -> Tuple[np.ndarray, float]:
    """
    Normalizzazione audio robusta basata sul 98° percentile con Noise Gate e guadagno limitato:
    1. Rimuove eventuale DC offset.
    2. Calcola p98 = np.percentile(np.abs(audio_data), 98).
    3. Se p98 < noise_gate_p98 (silenzio o solo rumore ambientale), NON amplifica il fruscio
       evitando che il VAD scambi il rumore per parlato generando allucinazioni Whisper.
    4. Altrimenti scala verso 0.80 con guadagno limitato (<= max_gain) + soft-clipping np.tanh.
    Restituisce (audio_normalizzato_float32, p98_pre_scaling).
    """
    if audio_data is None or len(audio_data) == 0:
        return np.zeros(0, dtype=np.float32), 0.0

    audio_data = audio_data.astype(np.float32, copy=False)
    audio_data = audio_data - float(np.mean(audio_data))
    p98 = float(np.percentile(np.abs(audio_data), 98))
    if p98 >= noise_gate_p98:
        gain = min(float(max_gain), 0.80 / p98)
        audio_data = np.tanh(audio_data * gain).astype(np.float32)
    else:
        audio_data = audio_data.astype(np.float32, copy=False)
    return audio_data, p98


def compute_dynamic_beam_size(duration_sec: float) -> int:
    """
    Calcola dinamicamente beam_size per Faster-Whisper in base alla durata del buffer audio:
    - duration_sec < 2.5        -> beam_size = 1 (comandi rapidissimi)
    - 2.5 <= duration_sec <= 8.0 -> beam_size = 3 (frasi medie)
    - duration_sec > 8.0        -> beam_size = 4 (dettati lunghi)
    """
    if duration_sec < 2.5:
        return 1
    if duration_sec <= 8.0:
        return 3
    return 4


def join_streaming_chunks(chunks: List[str]) -> str:
    """
    Unisce in ordine FIFO i micro-chunk trascritti/rifiniti in streaming,
    preservando la continuità sintattica, la punteggiatura e i cambi di paragrafo.
    """
    valid = [c.strip() for c in chunks if c and c.strip()]
    if not valid:
        return ""
    if len(valid) == 1:
        return valid[0]

    acc = valid[0]
    for nxt in valid[1:]:
        acc_r = acc.rstrip()
        nxt_l = nxt.lstrip()
        if not nxt_l:
            continue
        if acc.endswith("\n\n") or nxt_l.startswith("\n"):
            acc = f"{acc_r}\n\n{nxt_l.lstrip()}"
            continue

        last_ch = acc_r[-1] if acc_r else ""
        # Se il chunk precedente termina con virgola o punto e virgola e il successivo
        # inizia con una parola comune maiuscola non seguita da maiuscole (non acronimo),
        # armonizza l'iniziale in minuscolo per continuità sintattica.
        if last_ch in (",", ";") and len(nxt_l) >= 2 and nxt_l[0].isupper() and nxt_l[1].islower():
            first_word = re.split(r"\s+|[,.;:!?]", nxt_l, maxsplit=1)[0]
            known_proper = {
                "Whis", "FlowLocal", "FlowType", "FlowStudy", "FlowNotes", "FlowTime",
                "Antigravity", "PyQt6", "Ollama", "Qwen", "Whisper", "LaTeX", "Markdown",
                "Google", "Windows", "Spotify", "Git", "GitHub", "Python", "CUDA",
                "Triscina", "Selinunte", "Castelvetrano", "Palermo", "Giada", "Alessandro",
            }
            if first_word not in known_proper:
                nxt_l = nxt_l[0].lower() + nxt_l[1:]

        acc = f"{acc_r} {nxt_l}"
    return acc.strip()


MODE_SYSTEM_PROMPTS = {
    "formal": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Modalità: SCRITTURA FORMALE ITALIANA.\n"
        "1. Mantieni le parole dell'utente correggendo solo punteggiatura e piccole forme colloquiali per email/documenti formali, senza aggiungere saluti non dettati.\n"
        f"2. {PUNCTUATION_RULE}\n"
        f"3. {LATEX_FORMULA_RULE}"
    ),
    "code": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Modalità: SVILUPPATORE / TERMINALE.\n"
        "1. MANTIENI RIGOROSAMENTE LA LINGUA ITALIANA per tutta la frase. NON tradurre mai in inglese: usa l'inglese solo per i termini informatici pronunciati (commit, bug, push, pull request, array, backend, refactor).\n"
        "2. Preserva nomi di variabili, file, estensioni e comandi; applica snake_case/camelCase solo se dettato.\n"
        "3. NON aggiungere punto fermo finale su singole righe di codice o comandi.\n"
        "4. Rimuovi esitazioni vocali (ehm, uhm, mmm, vabbè).\n"
        f"5. {LATEX_FORMULA_RULE}"
    ),
    "study_latex": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Modalità: APPUNTI DI STUDIO & LATEX.\n"
        "1. Preserva il lessico dell'utente strutturando elenchi puntati Markdown quando dettati per punti.\n"
        "2. Se l'utente pronuncia 'Flashcard: [Domanda] ... [Risposta]', formatta chiaramente come Q&A.\n"
        f"3. {PUNCTUATION_RULE}\n"
        f"4. {LATEX_FORMULA_RULE}"
    ),
    "chat": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Modalità: CHAT / MESSAGGISTICA RAPIDA.\n"
        "1. Mantieni tono naturale e diretto; evita il punto fermo finale sui messaggi brevi di una sola frase, ma usa '?' per le domande dirette.\n"
        f"2. {PUNCTUATION_RULE}\n"
        f"3. {LATEX_FORMULA_RULE}"
    ),
    "natural": (
        f"{ANTI_HALLUCINATION_RULE}\n"
        "Modalità: DETTATURA NATURALE ITALIANA AD ALTA FEDELTÀ.\n"
        "1. Converti [PAUSA_CAMBIO_ARGOMENTO] in un nuovo paragrafo.\n"
        "2. Formatta elenchi parlati ('uno... due...', 'primo... secondo...') come lista puntata Markdown ('- ').\n"
        f"3. {PUNCTUATION_RULE}\n"
        f"4. {LATEX_FORMULA_RULE}\n"
        "Esempio: \"allora ci serve F uguale m per a\" -> \"Ci serve $F = m \\cdot a$.\""
    ),
}


def _unload_ollama_model(model_name: str):
    """Scarica immediatamente un modello dalla VRAM GPU impostando keep_alive a 0."""
    try:
        payload = {"model": model_name, "messages": [], "keep_alive": 0}
        req = urllib.request.Request(
            OLLAMA_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            pass
        print(f"[FlowLocal VRAM] Scaricato '{model_name}' dalla VRAM GPU.")
    except Exception as e:
        print(f"[FlowLocal VRAM] Avviso scaricamento {model_name}: {e}")


def _preload_ollama_model(model_name: str, keep_alive: str = "10m"):
    """Precarica un modello nella VRAM GPU per disponibilità immediata a zero latenza."""
    try:
        payload = {"model": model_name, "messages": [], "keep_alive": keep_alive}
        req = urllib.request.Request(
            OLLAMA_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            pass
        print(f"[FlowLocal VRAM] Precaricato '{model_name}' in VRAM GPU (keep_alive={keep_alive}).")
    except Exception as e:
        print(f"[FlowLocal VRAM] Avviso precaricamento {model_name}: {e}")


class LLMRefiner:
    """Stadio 2: Rifinitura semantica, punteggiatura e vocabolario tecnico con Qwen 3.5."""

    @classmethod
    def refine(cls, raw_text: str, mode: str = "natural", previous_context: str = "") -> Tuple[str, int]:
        t0 = time.time()
        if not raw_text or not raw_text.strip():
            return raw_text, 0

        prev_ctx = (previous_context or "").strip()[-60:].strip()
        # Se il contesto precedente non termina con punteggiatura di fine periodo, non forzare la maiuscola
        should_cap = True
        if prev_ctx and prev_ctx[-1:] not in (".", "!", "?", ":", "\n"):
            should_cap = False

        # Normalizzazione deterministica delle varianti fonetiche
        raw_text = normalize_phonetic_variants(raw_text)

        # 1. Snippet Vocali deterministici (solo se non siamo nel mezzo di un dettato multi-chunk)
        if not prev_ctx:
            expanded = db.match_and_expand_snippet(raw_text)
            if expanded:
                return expanded, 0

        # 2. Controllo Flashcard automatica
        if raw_text.strip().lower().startswith("flashcard:"):
            cls._save_flashcard(raw_text)

        # 3. Verifica Stile Attivo (Zero LLM)
        active_style = db.get_active_style()
        if active_style.get("system_prompt") == "RAW_WHISPER_NO_LLM":
            from core.transcriber import format_spoken_lists
            return format_spoken_lists(strip_pause_markers(raw_text)), 0

        # 4. Selezione dinamica del System Prompt in base alla modalità + integrazione stili personalizzati dal DB
        mode_clean = (mode or "natural").lower()
        if mode_clean in ("code", "coding", "code / tech"):
            mode_key = "code"
        elif mode_clean in ("study_latex", "study", "study_mode", "studio / latex", "studio"):
            mode_key = "study_latex"
        elif mode_clean in ("formal", "formale"):
            mode_key = "formal"
        elif mode_clean in ("chat", "chat / diretto"):
            mode_key = "chat"
        else:
            mode_key = "natural"

        system_prompt = MODE_SYSTEM_PROMPTS.get(mode_key, MODE_SYSTEM_PROMPTS["natural"])

        # Se l'utente ha selezionato nell'Hub uno stile personalizzato (non default di sistema),
        # integra il suo system_prompt mantenendo attive le barriere anti-allucinazione, LaTeX e italiano in code.
        db_style = None
        active_name = (active_style.get("name") or "").strip().lower()
        if active_name and active_name not in ("natural", "formal", "code", "coding", "study_latex", "chat", "raw_whisper"):
            db_style = active_style
        else:
            lookup_names = ("coding", "code") if mode_key == "code" else (mode_key,)
            for lname in lookup_names:
                cand = db.get_style_by_name(lname)
                if cand and (cand.get("system_prompt") or "").strip():
                    db_style = cand
                    break

        if db_style:
            custom_p = (db_style.get("system_prompt") or "").strip()
            # Usa lo stile personalizzato solo se l'utente l'ha effettivamente personalizzato (non vecchio prompt legacy con <output>)
            if custom_p and custom_p != "RAW_WHISPER_NO_LLM" and "<output>" not in custom_p and "DIVIETO ASSOLUTO DI PARAFRASI" not in custom_p:
                system_prompt = f"{ANTI_HALLUCINATION_RULE}\n\n{custom_p}"

        # Assicura le garanzie inderogabili anche su stili personalizzati
        if "SEI UN FILTRO DI PULIZIA TESTUALE" not in system_prompt:
            system_prompt = f"{ANTI_HALLUCINATION_RULE}\n\n{system_prompt}"
        if mode_key == "code" and "NON tradurre mai" not in system_prompt:
            system_prompt += "\nMANTIENI RIGOROSAMENTE LA LINGUA ITALIANA: non tradurre mai il testo in inglese."
        if "LaTeX" not in system_prompt:
            system_prompt += f"\n{LATEX_FORMULA_RULE}"

        # Aggiunta Vocabolario al prompt di Qwen (compatto, max 30 termini)
        vocab_terms = db.get_vocabulary_biasing_prompt()
        if vocab_terms:
            vocab_list = [t.strip() for t in vocab_terms.split(",") if t.strip()][:30]
            if vocab_list:
                system_prompt += f"\nTermini tecnici da rispettare: {', '.join(vocab_list)}."

        # Continuità sintattica e di punteggiatura tra chunk in streaming (ultimi 60 caratteri)
        if prev_ctx:
            system_prompt += (
                f"\nContesto precedente (NON ripetere): \"{prev_ctx}\". "
                "Mantieni continuità grammaticale e restituisci SOLO il testo in <trascrizione></trascrizione>."
            )

        clean_prompt_text = strip_vocal_fillers(raw_text, capitalize_first=should_cap)

        # Fast-path deterministico a 0ms per micro-frasi da 1-2 parole senza matematica o contesto streaming
        # (evita di attendere secondi di inferenza LLM per un semplice "Grazie." o "Ok")
        _math_hint_re = re.compile(
            r"\b(uguale|integrale|derivata|vettore|fratto|radice|sommatoria|pigreco|pi greco|alfa|beta|gamma|delta|theta|lambda|omega|quadro|cubo|elevato|alla)\b",
            re.IGNORECASE,
        )
        _prompt_words = clean_prompt_text.split()
        if (
            not prev_ctx
            and 1 <= len(_prompt_words) <= 2
            and "[PAUSA_CAMBIO_ARGOMENTO]" not in clean_prompt_text
            and not _math_hint_re.search(clean_prompt_text)
        ):
            fast_out = clean_prompt_text.strip()
            if mode_key in ("chat", "code") and fast_out.endswith(".") and not fast_out.endswith("..."):
                fast_out = fast_out[:-1].rstrip()
            return fast_out, 0

        user_content = f"Testo pronunciato da elaborare:\n<trascrizione>\n{clean_prompt_text}\n</trascrizione>"

        routing_cfg = get_ai_routing_config()
        provider = routing_cfg.get("provider", "local")
        hw_profile = get_active_profile()
        local_model = hw_profile.llm_default_model or LLM_MODEL
        ollama_url = routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL
        use_gemini = should_use_gemini("dictation")

        qwen_ms = 0
        req_timeout = max(5.0, min(10.0, 5.0 + len(raw_text) / 100.0))
        num_predict_tokens = max(128, int(len(raw_text.split()) * 4.5))
        raw_llm_content = ""
        used_gemini = False

        if use_gemini:
            try:
                raw_llm_content = gemini_generate_content(
                    user_content,
                    system_instruction=system_prompt,
                    temperature=0.0,
                    max_output_tokens=num_predict_tokens,
                    stop_sequences=["</trascrizione>"],
                    timeout=DICTATION_GEMINI_TIMEOUT,
                )
                if raw_llm_content:
                    used_gemini = True
                    set_active_runtime_engine("Adattivo • Gemini Online" if provider == "adaptive" else "Cloud • Gemini Online")
            except Exception as gem_exc:
                mark_gemini_unavailable()
                print(f"[FlowLocal] Gemini non raggiungibile in dettatura ({gem_exc}). Fallback su motore locale (cooldown 30s).")
                raw_llm_content = ""

        if not raw_llm_content and (not use_gemini or routing_cfg.get("hybrid_fallback", True)):
            payload = {
                "model": local_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": {
                    "num_ctx": 2048,
                    "repeat_penalty": 1.02,
                    "temperature": 0.0,
                    "num_predict": num_predict_tokens,
                    "stop": ["</trascrizione>"]
                }
            }
            try:
                req = urllib.request.Request(
                    ollama_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_llm_content = data.get("message", {}).get("content", "") or ""
                    if raw_llm_content:
                        set_active_runtime_engine("Adattivo • Qwen 3.5 Offline" if provider == "adaptive" else "Locale • Qwen 3.5")
            except Exception:
                raw_llm_content = ""
                if not used_gemini:
                    set_active_runtime_engine("Adattivo • Deterministico Offline" if provider == "adaptive" else "Locale • Deterministico")

        if raw_llm_content:
            try:
                content = strip_think_blocks(raw_llm_content)
                qwen_ms = int((time.time() - t0) * 1000)

                # Rimozione preliminare di qualsiasi tag residuo simile a <trascrizione> (inclusi refusi come </trascrizizione>)
                refined = re.sub(r"</?trascri\w*\s*>", "", content, flags=re.IGNORECASE).strip()

                # Estrazione e rimozione di eventuali altri tag XML residui (<output>, ecc.)
                for tag in ("output", "text", "refined"):
                    match = re.search(rf'<{tag}>(.*?)</{tag}>', refined, flags=re.DOTALL | re.IGNORECASE)
                    if match:
                        refined = match.group(1)
                    else:
                        refined = re.sub(rf'</?{tag}>', '', refined, flags=re.IGNORECASE)

                refined = re.sub(r"</?trascri\w*\s*>", "", refined, flags=re.IGNORECASE).strip()

                # Rimuove eventuali tag HTML spuri (<p>, <br>, <div>, ecc.) emessi da modelli locali in modalità code
                refined = strip_spurious_html_tags(refined, raw_source=raw_text)

                # Rimuove prefissi o preamboli discorsivi artificiali (es. 'Trascrizione: ...', '**TRASCRIZIONE:** ...')
                refined = re.sub(
                    r'^\s*(?:\*{1,2}|")?\s*(?:ecco(?:\s+qui)?\s+(?:il\s+)?testo\s+(?:elaborato|rifinito|trascritto)|trascrizione|output|testo\s+elaborato|testo\s+rifinito)\s*(?:\*{1,2})?\s*:\s*(?:<br\s*/?>|\*{1,2})*\s*',
                    '',
                    refined,
                    flags=re.IGNORECASE
                ).strip()
                refined = re.sub(r'^(?:<br\s*/?>)+', '', refined, flags=re.IGNORECASE).strip()

                refined = refined.strip()
                if refined.startswith('"') and refined.endswith('"'):
                    refined = refined[1:-1].strip()

                # Se l'LLM ha accidentalmente ripetuto il previous_context all'inizio, rimuovilo
                if prev_ctx and refined.lower().startswith(prev_ctx.lower()) and not clean_prompt_text.lower().startswith(prev_ctx.lower()):
                    refined = refined[len(prev_ctx):].lstrip()

                # Pulizia esitazioni vocali residue all'inizio + formattazione elenchi parlati espliciti
                refined = strip_vocal_fillers(refined, capitalize_first=should_cap)
                from core.transcriber import format_spoken_lists
                refined = format_spoken_lists(refined)

                # Rimuove eventuali backslash finali (\) o spazi superflui
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                # Guardrail sulla lunghezza (anti-allucinazione E anti-troncamento/riassunto):
                # 1. Troppo lungo (> max(len * 1.45, len + 40)): scarta allucinazione/risposta assistente.
                # 2. Troppo corto (< 50% del parlato originale senza formule LaTeX su frasi >= 30 car.):
                #    scarta troncamenti o risposte monoparola dell'LLM (es. intera frase ridotta a "Prova").
                max_allowed_len = max(len(raw_text) * 1.45, len(raw_text) + 40)
                min_allowed_len = (len(clean_prompt_text) * 0.48) if (len(clean_prompt_text) >= 30 and "$" not in refined) else 0
                if len(refined) > max_allowed_len or len(refined) < min_allowed_len:
                    reason = "troppo lungo" if len(refined) > max_allowed_len else "troncato/riassunto"
                    print(f"[FlowLocal] Output LLM scartato ({reason}: {len(refined)} car vs {len(raw_text)} raw). Fallback su testo acustico.")
                    clean = normalize_phonetic_variants(raw_text)
                    clean = db.apply_phonetic_replacements(clean).replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
                    clean = strip_vocal_fillers(clean, capitalize_first=should_cap)
                    clean = format_spoken_lists(clean)
                    while clean.endswith('\\'):
                        clean = clean[:-1].rstrip()
                    return clean, qwen_ms

                # Guardrail lessicale anti-parafrasi / allucinazione sinonimi / cancellazione parole:
                if not cls._check_lexical_fidelity(clean_prompt_text, refined):
                    print(f"[FlowLocal] Parafrasi o taglio eccessivo LLM scartato. Fallback su trascrizione acustica.")
                    clean = normalize_phonetic_variants(raw_text)
                    clean = db.apply_phonetic_replacements(clean).replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
                    clean = strip_vocal_fillers(clean, capitalize_first=should_cap)
                    clean = format_spoken_lists(clean)
                    while clean.endswith('\\'):
                        clean = clean[:-1].rstrip()
                    return clean, qwen_ms

                # Rimuove eventuali tag pausa residui non convertiti da Qwen
                refined = strip_pause_markers(refined)

                if refined:
                    return refined, qwen_ms
            except Exception:
                pass

        qwen_ms = int((time.time() - t0) * 1000)
        clean = normalize_phonetic_variants(raw_text)
        clean = db.apply_phonetic_replacements(clean).replace("[PAUSA_CAMBIO_ARGOMENTO]", "\n\n").rstrip()
        clean = strip_vocal_fillers(clean, capitalize_first=should_cap)
        if mode_key != "code":
            from core.transcriber import format_spoken_lists
            clean = format_spoken_lists(clean)
        while clean.endswith('\\'):
            clean = clean[:-1].rstrip()
        return clean, qwen_ms

    @classmethod
    def _check_lexical_fidelity(cls, raw: str, refined: str) -> bool:
        """
        Verifica bidirezionale di fedeltà lessicale:
        1. Precision: l'LLM non deve aver inventato/sostituito > 30% delle parole.
        2. Recall: l'LLM non deve aver tagliato/cancellato > 45% delle parole di contenuto originali
           (protegge da crolli tipo una frase intera ridotta a una singola parola "Prova").
        Esclude gli span LaTeX ($...$ / $$...$$) dal conteggio drift.
        """
        if not refined or not raw:
            return True

        has_latex = "$" in refined
        refined_no_math = re.sub(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$", " ", refined)

        # Estrae parole alfanumeriche di almeno 3 caratteri
        raw_content_words = [w for w in re.findall(r'[a-zA-Zà-úÀ-Ú0-9]+', raw.lower()) if len(w) >= 3]
        raw_words = set(re.findall(r'[a-zA-Zà-úÀ-Ú0-9]+', raw.lower()))
        refined_words = [w for w in re.findall(r'[a-zA-Zà-úÀ-Ú0-9]+', refined_no_math.lower()) if len(w) >= 3]

        # Controllo Recall (anti-troncamento) su frasi con almeno 5 parole di contenuto
        if len(raw_content_words) >= 5 and not has_latex:
            refined_set = set(refined_words)
            kept = sum(
                1 for ow in raw_content_words
                if any(ow == rw or (len(ow) >= 4 and len(rw) >= 4 and ow[:4] == rw[:4]) for rw in refined_set)
            )
            recall_ratio = kept / len(raw_content_words)
            if recall_ratio < 0.55:
                return False

        if len(refined_words) < 3:
            return len(raw_content_words) < 5 or has_latex

        # Controllo Precision (anti-parafrasi)
        hallucinated_words = 0
        for rw in refined_words:
            match = any(rw == ow or (len(rw) >= 4 and len(ow) >= 4 and rw[:4] == ow[:4]) for ow in raw_words)
            if not match:
                hallucinated_words += 1

        drift_ratio = hallucinated_words / len(refined_words)
        return drift_ratio <= 0.30

    @classmethod
    def generate_note_title(cls, content_markdown: str, folder_name: str = "") -> str:
        """
        Genera un titolo conciso (3-7 parole) in italiano basato sul contesto della nota.
        Usa il routing AI attivo (Gemini/Ollama) con timeout breve e fallback euristico locale.
        """
        clean_md = (content_markdown or "").strip()
        if not clean_md:
            return ""

        def _heuristic_title(md: str) -> str:
            for ln in md.splitlines():
                s = ln.strip()
                if not s or s.startswith("```") or s.startswith(":::") or s.startswith("!["):
                    continue
                s = re.sub(r"^#{1,6}\s+", "", s)
                s = re.sub(r"^[-*]\s+\[[ xX]\]\s+", "", s)
                s = re.sub(r"^[-*•>]\s+", "", s)
                s = re.sub(r"[*_`~]+", "", s)
                s = re.sub(r"\$\$?([^$]+)\$\$?", r"\1", s)
                s = re.sub(r"\s+", " ", s).strip(" .,:;—–-")
                if len(s) >= 3:
                    words = s.split()
                    candidate = " ".join(words[:7])
                    if len(candidate) > 54:
                        candidate = candidate[:51].rstrip() + "..."
                    return candidate[0].upper() + candidate[1:]
            return "Nuova nota"

        sample = clean_md[:1800]
        ctx_line = f"\nCartella: {folder_name.strip()}" if (folder_name or "").strip() and folder_name.strip().lower() != "generale" else ""
        system_prompt = (
            "Genera un titolo breve, chiaro e descrittivo in italiano (massimo 6 parole) per la nota fornita."
            f"{ctx_line}\n"
            "Restituisci SOLO il titolo su una singola riga: niente virgolette, niente prefissi ('Titolo:'), niente punto finale, niente emoji."
        )
        user_msg = f"<nota>\n{sample}\n</nota>"

        routing_cfg = get_ai_routing_config()
        use_gemini = should_use_gemini("dictation")
        raw_title = ""

        if use_gemini:
            try:
                raw_title = gemini_generate_content(
                    user_msg,
                    system_instruction=system_prompt,
                    temperature=0.2,
                    max_output_tokens=32,
                    timeout=3.5,
                )
            except Exception:
                mark_gemini_unavailable()
                raw_title = ""

        if not raw_title and (not use_gemini or routing_cfg.get("hybrid_fallback", True)):
            hw_profile = get_active_profile()
            local_model = hw_profile.llm_default_model or LLM_MODEL
            payload = {
                "model": local_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": 0.2,
                    "num_predict": 32,
                },
            }
            try:
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=4.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_title = data.get("message", {}).get("content", "") or ""
            except Exception:
                raw_title = ""

        if raw_title:
            cleaned = strip_think_blocks(raw_title)
            cleaned = cleaned.splitlines()[0].strip() if cleaned else ""
            cleaned = re.sub(r"^(?:titolo|title)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = cleaned.strip("\"'`*# ").rstrip(".")
            cleaned = re.sub(r"\s+", " ", cleaned)
            if 2 <= len(cleaned) <= 72:
                return cleaned[0].upper() + cleaned[1:]

        return _heuristic_title(clean_md)

    @classmethod
    def refine_text(cls, raw_text: str, mode: str = "natural", previous_context: str = "") -> Tuple[str, int]:
        """Alias architetturale per refine."""
        return cls.refine(raw_text, mode=mode, previous_context=previous_context)

    @classmethod
    def transform_selection(cls, selected_text: str, voice_command: str) -> str:
        """Trasforma il testo selezionato applicando l'istruzione vocale dell'utente via Qwen 3.5."""
        if not selected_text or not selected_text.strip():
            return selected_text
        if not voice_command or not voice_command.strip():
            return selected_text

        system_prompt = (
            "SEI UN EDITOR DI TESTI PROFESSIONALE PER LINGUA ITALIANA. "
            "Applica l'istruzione dell'utente al testo selezionato mantenendo rigorosamente tutti i fatti, "
            "i tempi verbali (passato, presente, futuro) e il significato originale. "
            "Usa un italiano formale idiomatico, corretto nei congiuntivi e senza mai inserire parole inglesi o calchi dall'inglese. "
            "Restituisci ESCLUSIVAMENTE il testo trasformato.\n\n"
            "Esempio:\n"
            "<comando>Riscrivilo in stile formale</comando>\n"
            "<testo_selezionato>ciao prof le mando il file domani appena finisco di controllare i conti spero vada bene</testo_selezionato>\n"
            "Output:\n"
            "Gentile Professore, Le invierò il file domani non appena avrò terminato il controllo dei conti. Spero che vada tutto bene."
        )

        user_content = (
            f"<comando>{voice_command.strip()}</comando>\n"
            f"<testo_selezionato>{selected_text.strip()}</testo_selezionato>"
        )

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": False,
            "think": False,
            "keep_alive": "30m",
            "options": {
                "repeat_penalty": 1.15,
                "temperature": 0.15,
                "top_p": 0.85,
                "num_predict": max(300, len(selected_text.split()) * 4),
                "stop": ["</testo_selezionato>", "</comando>", "</trascrizione>"]
            }
        }

        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = strip_think_blocks(data.get("message", {}).get("content", ""))

                # Pulizia tag residui (inclusi refusi o chiusure XML)
                refined = re.sub(r"</?(?:testo_selezionato|comando|trascri\w*|output|result)>\s*", "", content, flags=re.IGNORECASE).strip()
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                if refined:
                    return refined
        except Exception as e:
            print(f"[FlowLocal] Errore trasformazione selezione LLM: {e}")

        return selected_text

    @classmethod
    def detect_lecture_model(cls) -> str:
        """Rileva se qwen3.5:9b è presente su Ollama (tramite API /api/tags o CLI 'ollama list'), con fallback su qwen3.5:4b."""
        try:
            req_tags = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req_tags, timeout=1.2) as resp_tags:
                models_data = json.loads(resp_tags.read().decode())
                names = [m.get("name", "") for m in models_data.get("models", [])]
                if any(LLM_HIGH_PRECISION_MODEL in n for n in names):
                    return LLM_HIGH_PRECISION_MODEL
        except Exception:
            pass

        try:
            res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=2.0)
            if res.returncode == 0 and LLM_HIGH_PRECISION_MODEL in res.stdout:
                return LLM_HIGH_PRECISION_MODEL
        except Exception:
            pass

        return LLM_MODEL

    @classmethod
    def reformat_lecture_block(cls, chunk_text: str, subject_context: str = "") -> str:
        """Riformatta un blocco di sbobinatura universitaria in Markdown chiaro ed elegante con Qwen 3.5 (preferendo 9B),
        correggendo refusi fonetici e allucinazioni acustiche incompatibili con il contesto accademico."""
        clean_chunk = (chunk_text or "").strip()
        if not clean_chunk:
            return ""

        clean_ctx = (subject_context or "").strip()
        ctx_instruction = f"\nMateria / Parole chiave della lezione: {clean_ctx}." if clean_ctx else ""

        system_prompt = (
            "SEI UN SBOBINATORE UNIVERSITARIO FEDELE E PRECISO. "
            f"{ctx_instruction}\n\n"
            "Trasforma la trascrizione grezza della lezione in una sbobina scorrevole, ordinata e FEDELISSIMA alle parole esatte del docente, "
            "così che l'allineamento audio parola-per-parola resti preciso.\n"
            "REGOLE TASSATIVE:\n"
            "1. NON riassumere, NON parafrasare e NON tagliare frasi pronunciate: aggiungi solo punteggiatura corretta, divisione in paragrafi e correggi evidenti refusi fonetici coerenti con la materia.\n"
            "2. NON inventare MAI concetti, NON completare elenchi con frasi come 'Non specificate nel testo, ma potrebbero includere...' e NON aggiungere commenti meta come 'Questo esempio illustra...'.\n"
            "3. Usa SOLO titoli brevi di livello 2 (## Titolo breve [MM:SS], massimo 7 parole) nei veri cambi di argomento, seguiti SEMPRE da una riga vuota (\\n\\n) prima del testo parlato. NON usare mai ### o #### e NON mettere mai un intero paragrafo sulla riga di ##.\n"
            "4. Se ritieni utile aggiungere un breve riassunto, una sintesi o un chiarimento di supporto che NON è stato pronunciato testualmente, inseriscilo ESCLUSIVAMENTE in una nota espandibile dedicata nel formato:\n"
            ":::ai-note Titolo breve\nContenuto sintetico della nota.\n:::\n"
            "5. Non lasciare marcatori temporali [MM:SS] isolati nelle frasi ordinarie.\n"
            "6. Quando incontri `[AUDIO_NON_CHIARO da MM:SS a MM:SS]` o una frase incomprensibile, trasformalo esclusivamente in **`[Audio non chiaro / testo incompleto: da MM:SS a MM:SS]`** senza inventare nulla."
        )

        user_content = f"<trascrizione_lezione>\n{clean_chunk}\n</trascrizione_lezione>"

        routing_cfg = get_ai_routing_config()
        use_gemini = should_use_gemini("lecture_heavy")
        lecture_model = cls.detect_lecture_model()
        raw_content = ""

        if use_gemini:
            try:
                raw_content = gemini_generate_content(
                    user_content,
                    system_instruction=system_prompt,
                    temperature=0.15,
                    max_output_tokens=2048,
                    stop_sequences=["</trascrizione_lezione>", "</sbobina>", "</trascrizione>"],
                    timeout=50.0,
                )
            except Exception as gem_exc:
                print(f"[FlowLocal Sbobina] Gemini BYOK non raggiungibile ({gem_exc}). Fallback su Ollama locale.")
                raw_content = ""

        if not raw_content and (not use_gemini or routing_cfg.get("hybrid_fallback", True)):
            print(f"[FlowLocal Sbobina] Riformattazione blocco con modello: {lecture_model}")
            payload = {
                "model": lecture_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": {
                    "repeat_penalty": 1.15,
                    "temperature": 0.15,
                    "num_predict": 2048,
                    "stop": ["</trascrizione_lezione>", "</sbobina>", "</trascrizione>"]
                }
            }
            try:
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=50.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_content = data.get("message", {}).get("content", "") or ""
            except Exception as e:
                print(f"[FlowLocal] Errore riformattazione blocco sbobina LLM ({lecture_model}): {e}")
                raw_content = ""

        if raw_content:
            try:
                content = strip_think_blocks(raw_content)
                refined = re.sub(r"</?(?:trascrizione_lezione|sbobina|output|result)>\s*", "", content, flags=re.IGNORECASE).strip()
                refined = refined.rstrip()
                while refined.endswith('\\'):
                    refined = refined[:-1].rstrip()

                # Normalizzazione finale segnaposto audio intervallo
                refined = re.sub(
                    r'\[AUDIO_NON_CHIARO\s+da\s+(\d{2}:\d{2})\s+a\s+(\d{2}:\d{2})\]',
                    r'**[Audio non chiaro / testo incompleto: da \1 a \2]**',
                    refined,
                    flags=re.IGNORECASE
                )
                refined = re.sub(
                    r'\[AUDIO_NON_CHIARO\s+(\d{2}:\d{2})\]',
                    r'**[Audio non chiaro / testo incompleto: da \1 a \1]**',
                    refined,
                    flags=re.IGNORECASE
                )
                # Rimuove marcatori temporali [MM:SS] standalone lasciati nel testo ordinario
                refined = re.sub(r'(?<!Audio non chiaro / testo incompleto: da )(?<!a )\[\d{2}:\d{2}\]\s*', '', refined)

                # Pulizia regex finale: rimuove backtick residui attorno ai segnaposto temporali
                refined = re.sub(r'`+(\[Audio non chiaro[^\]]+\])`+', r'\1', refined)
                refined = re.sub(r'`+(\*{0,2}\[Audio non chiaro[^\]]+\]\*{0,2})`+', r'\1', refined)

                # Rimuove eventuali caratteri cirillici spuri allucinati da Whisper/LLM (es. ОффStaff -> Staff)
                refined = re.sub(r'[\u0400-\u04FF]+', '', refined)

                # Guardrail titoli: normalizza ###/#### e impedisce che interi paragrafi finiscano su righe # / ##
                fixed_lines: List[str] = []
                for ln in refined.splitlines():
                    hm = re.match(r'^(#{1,4})\s+(.*)$', ln)
                    if hm:
                        body_h = hm.group(2).strip()
                        if body_h.startswith("**") and body_h.endswith("**") and len(body_h) > 4:
                            body_h = body_h[2:-2].strip()
                        words_h = body_h.split()
                        if len(words_h) > 12:
                            # Se c'è un punto fermo nei primi 12 vocaboli, separa titolo e paragrafo; altrimenti demota a testo normale
                            sent_m = re.match(r'^([^.!?]{4,90}[.!?])\s+(.+)$', body_h)
                            if sent_m and len(sent_m.group(1).split()) <= 12:
                                fixed_lines.append(f"## {sent_m.group(1).strip()}")
                                fixed_lines.append("")
                                fixed_lines.append(sent_m.group(2).strip())
                            else:
                                fixed_lines.append(body_h)
                        else:
                            fixed_lines.append(f"## {body_h}")
                    else:
                        fixed_lines.append(ln)
                refined = "\n".join(fixed_lines).strip()

                if refined:
                    return refined
            except Exception as e:
                print(f"[FlowLocal] Errore post-processing sbobina: {e}")

        return clean_chunk

    @classmethod
    def generate_lecture_summary(cls, lecture_markdown: str, subject_context: str = "") -> str:
        """Genera un riquadro di Sommario Esecutivo con i punti chiave della lezione."""
        clean_lecture = (lecture_markdown or "").strip()
        if not clean_lecture:
            return ""

        lecture_model = cls.detect_lecture_model()
        clean_ctx = (subject_context or "").strip()
        ctx_info = f"\nMateria: {clean_ctx}" if clean_ctx else ""

        system_prompt = (
            "SEI UN ASSISTENTE UNIVERSITARIO ESPERTO. "
            f"{ctx_info}\n"
            "Genera un Sommario Esecutivo sintetico, chiaro e rigoroso dei punti chiave trattati nella lezione. "
            "Restituisci ESCLUSIVAMENTE un riquadro in stile citazione Markdown nel seguente formato esatto:\n\n"
            "> 📌 **Punti Chiave della Lezione:**\n"
            "> - [Punto chiave 1]\n"
            "> - [Punto chiave 2]\n"
            "> - [Punto chiave 3]\n"
            "> - [Punto chiave 4]\n\n"
            "Includi da 4 a 6 punti salienti. Nessun commento, premessa o testo extra prima o dopo il riquadro."
        )

        sample_text = clean_lecture
        if len(sample_text) > 8000:
            sample_text = sample_text[:8000] + "\n...(continua)"

        routing_cfg = get_ai_routing_config()
        use_gemini = should_use_gemini("lecture_heavy")
        user_msg_text = f"<testo_lezione>\n{sample_text}\n</testo_lezione>"
        raw_summary = ""

        if use_gemini:
            try:
                raw_summary = gemini_generate_content(
                    user_msg_text,
                    system_instruction=system_prompt,
                    temperature=0.2,
                    max_output_tokens=600,
                    timeout=30.0,
                )
            except Exception as gem_exc:
                print(f"[FlowLocal] Gemini BYOK non raggiungibile per sommario ({gem_exc}). Fallback su Ollama.")
                raw_summary = ""

        if not raw_summary and (not use_gemini or routing_cfg.get("hybrid_fallback", True)):
            payload = {
                "model": lecture_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg_text}
                ],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": 600
                }
            }
            try:
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_summary = data.get("message", {}).get("content") or ""
            except Exception as e:
                print(f"[FlowLocal] Errore generazione sommario esecutivo ({lecture_model}): {e}")
                raw_summary = ""

        if raw_summary:
            summary = strip_think_blocks(raw_summary)
            summary = re.sub(r"</?(?:testo_lezione|output|result)>\s*", "", summary, flags=re.IGNORECASE).strip()
            if summary:
                return summary

        return ""

    @classmethod
    def extract_flashcards_from_note(cls, note_text: str) -> List[Dict[str, str]]:
        """Estrae 5-8 domande e risposte d'esame in formato strutturato per flashcard da una nota di studio."""
        clean_note = (note_text or "").strip()
        if not clean_note:
            return []

        lecture_model = cls.detect_lecture_model()
        system_prompt = (
            "SEI UN DOCENTE UNIVERSITARIO ESPERTO. "
            "Estrai dal testo della lezione da 5 a 8 domande d'esame fondamentali con risposte sintetiche e precise per flashcard di ripasso attivo. "
            "Restituisci ESCLUSIVAMENTE un array JSON valido con questa struttura esatta:\n"
            "[\n"
            "  {\"question\": \"Domanda d'esame 1?\", \"answer\": \"Risposta sintetica 1.\"},\n"
            "  {\"question\": \"Domanda d'esame 2?\", \"answer\": \"Risposta sintetica 2.\"}\n"
            "]\n"
            "Non aggiungere assolutamente testo introduttivo, saluti, spiegazioni o blocchi di codice markdown prima o dopo il JSON."
        )

        sample_text = clean_note
        if len(sample_text) > 9000:
            sample_text = sample_text[:9000]

        routing_cfg = get_ai_routing_config()
        use_gemini = should_use_gemini("lecture_heavy")
        user_msg_text = f"<testo_lezione>\n{sample_text}\n</testo_lezione>"
        raw_cards_content = ""

        if use_gemini:
            try:
                raw_cards_content = gemini_generate_content(
                    user_msg_text,
                    system_instruction=system_prompt,
                    temperature=0.25,
                    max_output_tokens=1200,
                    timeout=45.0,
                )
            except Exception as gem_exc:
                print(f"[FlowLocal] Gemini BYOK non raggiungibile per flashcard ({gem_exc}). Fallback su Ollama.")
                raw_cards_content = ""

        if not raw_cards_content and (not use_gemini or routing_cfg.get("hybrid_fallback", True)):
            payload = {
                "model": lecture_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg_text}
                ],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.25,
                    "num_predict": 1200
                }
            }
            try:
                req = urllib.request.Request(
                    routing_cfg.get("ollama_chat_url") or OLLAMA_API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=45.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_cards_content = data.get("message", {}).get("content") or ""
            except Exception as e:
                print(f"[FlowLocal] Errore estrazione flashcard ({lecture_model}): {e}")
                raw_cards_content = ""

        if raw_cards_content:
            try:
                content = strip_think_blocks(raw_cards_content)
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    if isinstance(parsed, list):
                        cards = []
                        for item in parsed:
                            if isinstance(item, dict) and "question" in item and "answer" in item:
                                q_val = (str(item.get("question") or "")).strip()
                                a_val = (str(item.get("answer") or "")).strip()
                                if q_val and a_val:
                                    cards.append({"question": q_val, "answer": a_val})
                        if cards:
                            return cards
            except Exception as e:
                print(f"[FlowLocal] Errore parsing flashcard: {e}")

        return []

    @classmethod
    def _save_flashcard(cls, text: str):
        try:
            content = text[10:].strip()
            parts = content.split("?", 1)
            if len(parts) == 2:
                q = parts[0].strip() + "?"
                a = parts[1].strip()
            else:
                q = content
                a = ""
            db.add_study_note(title=f"Flashcard: {q[:30]}...", content_markdown=f"**D:** {q}\n\n**R:** {a}", note_type="Flashcard", tags="Vocale")
        except Exception:
            pass

    @classmethod
    def find_related_notes(
        cls,
        new_title: str,
        new_content: str,
        existing_notes_summary: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Individua fino a 3 note/sbobine correlate tramite:
        1. Riferimenti espliciti Wikilink [[Titolo]] presenti nel testo/titolo.
        2. Auto-linking semantico concettuale interrogando Ollama (qwen3.5:9b se presente, altrimenti modello standard).
        Restituisce una lista di dict: [{'id': int, 'title': str, 'reason': str}].
        """
        if not existing_notes_summary:
            return []

        results: List[Dict[str, Any]] = []
        linked_ids = set()

        notes_by_id = {n["id"]: n for n in existing_notes_summary if "id" in n}

        # 1. Parsing Wikilink espliciti [[Titolo]]
        full_text = f"{new_title or ''}\n{new_content or ''}"
        wikilinks = re.findall(r'\[\[(.*?)\]\]', full_text)
        for link_target in wikilinks:
            target_clean = link_target.strip().lower()
            if not target_clean:
                continue
            for n in existing_notes_summary:
                nid = n.get("id")
                ntitle = (n.get("title") or "").strip()
                if nid and nid not in linked_ids:
                    if target_clean == ntitle.lower() or target_clean in ntitle.lower() or ntitle.lower() in target_clean:
                        results.append({
                            "id": nid,
                            "title": ntitle,
                            "reason": f"Riferimento esplicito [[{link_target.strip()}]]"
                        })
                        linked_ids.add(nid)
                        break

        # Se abbiamo già raggiunto 3 correlazioni tramite wikilink espliciti, restituisci
        if len(results) >= 3:
            return results[:3]

        # 2. Auto-linking semantico concettuale con Ollama Qwen
        candidate_notes = [n for n in existing_notes_summary if n.get("id") not in linked_ids]
        if not candidate_notes:
            return results

        candidates_formatted = []
        for n in candidate_notes[:25]:
            nid = n.get("id")
            ntitle = (n.get("title") or "Senza Titolo").strip()
            ntags = (n.get("tags") or "").strip()
            preview = (n.get("preview") or n.get("content_markdown") or "").strip()
            preview = re.sub(r'[\r\n]+', ' ', preview)
            if len(preview) > 130:
                preview = preview[:130] + "..."
            candidates_formatted.append(f"- ID {nid}: \"{ntitle}\" [Tag: {ntags}] -> {preview}")

        candidates_block = "\n".join(candidates_formatted)
        content_sample = (new_content or "").strip()
        if len(content_sample) > 3000:
            content_sample = content_sample[:3000] + "\n...(continua)"

        lecture_model = getattr(cls, "model", None) or cls.detect_lecture_model()

        system_prompt = (
            "SEI UN ASSISTENTE PER LA GESTIONE DELLA CONOSCENZA E GRAFI ACCADEMICI (stile Obsidian / Zettelkasten).\n"
            "Analizza la NUOVA NOTA/SBOBINA e confrontala con l'ELENCO DELLE NOTE ESISTENTI per individuare da 0 a un massimo di 3 note correlate "
            "per argomento, medesima materia, istituto giuridico, algoritmi, teoremi o concetti chiave condivisi.\n\n"
            "REGOLE INDEROGABILI:\n"
            "1. Scegli SOLO note con una reale e fondata correlazione concettuale. Se nessuna nota esistente è pertinente, restituisci una lista vuota [].\n"
            "2. Per ogni nota correlata, fornisci il suo ID numerico e una 'reason' sintetica e chiara (max 12-15 parole, es. 'Stesso istituto giuridico: responsabilità in vigilando' oppure 'Approfondimento del medesimo algoritmo di ordinamento').\n"
            "3. Restituisci ESCLUSIVAMENTE un array JSON valido con questa struttura esatta:\n"
            "[\n"
            "  {\"id\": 1, \"reason\": \"Breve motivazione della correlazione\"}\n"
            "]\n"
            "NON includere spiegazioni, markdown o testo prima o dopo il JSON."
        )

        user_content = (
            f"<nuova_nota>\nTitolo: {new_title}\nContenuto:\n{content_sample}\n</nuova_nota>\n\n"
            f"<note_esistenti>\n{candidates_block}\n</note_esistenti>"
        )

        payload = {
            "model": lecture_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 400
            }
        }

        try:
            req = urllib.request.Request(
                OLLAMA_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = strip_think_blocks(data.get("message", {}).get("content", ""))

                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and "id" in item:
                                try:
                                    target_id = int(item["id"])
                                except (ValueError, TypeError):
                                    continue
                                if target_id in notes_by_id and target_id not in linked_ids:
                                    reason_text = str(item.get("reason") or "Correlazione semantica concettuale").strip()
                                    results.append({
                                        "id": target_id,
                                        "title": notes_by_id[target_id].get("title", ""),
                                        "reason": reason_text
                                    })
                                    linked_ids.add(target_id)
                                    if len(results) >= 3:
                                        break
        except Exception as e:
            print(f"[FlowLocal] Errore auto-linking note LLM ({lecture_model}): {e}")

        return results[:3]


class StreamingChunkSession:
    """
    Sessione FIFO asincrona per il Micro-Chunking in Streaming (Background Silence Segmentation)
    durante il Push-to-Talk o la registrazione attiva in FlowType.
    Elabora in background ciascun segmento audio sganciato (>= 4s con pausa >= 0.9s)
    tramite Faster-Whisper + Qwen 2.5 mentre l'utente sta ancora parlando, propagando
    gli ultimi 60 caratteri del chunk precedente rifinito (previous_context) sia a Whisper
    (initial_prompt) sia a Qwen per continuità sintattica e di punteggiatura.
    """

    def __init__(self, engine: "FlowLocalEngine", src_rate: int, mode: str):
        self.engine = engine
        self.src_rate = int(src_rate)
        self.mode = mode or "natural"
        self.fifo_queue: "queue.Queue[Optional[List[np.ndarray]]]" = queue.Queue()
        self.completed_chunks: List[Dict[str, Any]] = []
        self.previous_context: str = ""
        self.has_enqueued_chunks: bool = False
        self.total_audio_duration_s: float = 0.0
        self._cancelled: bool = False
        self._worker_started: bool = False
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._done_event = threading.Event()
        self._done_event.set()

    def enqueue_chunk(self, frames: List[np.ndarray]):
        if not frames:
            return
        with self._lock:
            self.has_enqueued_chunks = True
            total_samples = sum(len(f) for f in frames)
            if self.src_rate > 0:
                self.total_audio_duration_s += total_samples / float(self.src_rate)
            self._done_event.clear()
            self.fifo_queue.put(list(frames))
            if not self._worker_started:
                self._worker_started = True
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name="FlowLocalStreamingChunkWorker",
                )
                self._worker_thread.start()

    def finish_and_wait(self, timeout: float = 25.0):
        with self._lock:
            if not self._worker_started:
                return
            self.fifo_queue.put(None)
        self._done_event.wait(timeout=timeout)

    def finalize_and_collect(self, timeout: float = 25.0) -> List[Tuple[str, str, int, int]]:
        """
        Attende il completamento dei micro-chunk in coda FIFO e restituisce la lista
        ordinata di tuple (raw_text, refined_text, whisper_ms, qwen_ms).
        """
        self.finish_and_wait(timeout=timeout)
        return [
            (
                str(c.get("raw_text", "")),
                str(c.get("refined_text", "")),
                int(c.get("whisper_ms", 0)),
                int(c.get("qwen_ms", 0)),
            )
            for c in self.completed_chunks
        ]

    def cancel(self):
        """Annulla e svuota la sessione di micro-chunking."""
        with self._lock:
            self._cancelled = True
            while not self.fifo_queue.empty():
                try:
                    self.fifo_queue.get_nowait()
                except queue.Empty:
                    break
            if self._worker_started:
                self.fifo_queue.put(None)

    def _worker_loop(self):
        try:
            while True:
                frames = self.fifo_queue.get()
                if frames is None:
                    break
                try:
                    self._process_single_chunk(frames)
                except Exception as e:
                    print(f"[FlowLocal StreamChunk] Errore elaborazione micro-chunk: {e}")
        finally:
            self._done_event.set()

    def _process_single_chunk(self, frames: List[np.ndarray]):
        raw_chunk, whisper_ms, duration_s = self.engine._transcribe_single_audio_segment(
            frames,
            self.src_rate,
            previous_context=self.previous_context,
        )
        if not raw_chunk or not raw_chunk.strip():
            return

        # Gestione comandi vocali inline di auto-correzione ("Anzi, cancella tutto...")
        after_cmd = apply_inline_voice_commands(raw_chunk)
        if after_cmd != raw_chunk:
            # L'utente ha pronunciato un comando di reset: svuota i chunk precedenti
            self.completed_chunks.clear()
            self.previous_context = ""
            raw_chunk = after_cmd
            if not raw_chunk or not raw_chunk.strip():
                return

        raw_chunk = normalize_phonetic_variants(raw_chunk)
        raw_chunk = db.apply_phonetic_replacements(raw_chunk)

        refined_chunk, qwen_ms = LLMRefiner.refine(
            raw_chunk,
            mode=self.mode,
            previous_context=self.previous_context,
        )
        refined_chunk = strip_pause_markers(refined_chunk).rstrip()
        while refined_chunk.endswith('\\'):
            refined_chunk = refined_chunk[:-1].rstrip()

        if refined_chunk:
            self.previous_context = refined_chunk[-60:].strip()
            self.completed_chunks.append({
                "raw_text": raw_chunk,
                "refined_text": refined_chunk,
                "whisper_ms": whisper_ms,
                "qwen_ms": qwen_ms,
                "duration_s": duration_s,
            })
            print(
                f"[FlowLocal StreamChunk] Chunk #{len(self.completed_chunks)} pronto "
                f"(Whisper {whisper_ms}ms + Qwen {qwen_ms}ms): {refined_chunk}"
            )


class FlowLocalEngine:
    def __init__(self, orb=None, hub=None, whis_hud=None):
        self.orb = orb
        self.hub = hub
        self.whis_hud = whis_hud
        self.analyzer = AudioAnalyzer(num_half_bands=12, intensity=0.80, smoothing=0.50)

        self.model = None
        self._model_ready = threading.Event()
        self._model_error: Optional[str] = None

        def _load_whisper_and_warm():
            t_load = time.time()
            hw_prof = get_active_profile()
            w_model = hw_prof.whisper_model
            w_dev = hw_prof.whisper_device
            w_comp = hw_prof.whisper_compute_type
            try:
                kwargs = {
                    "device": w_dev,
                    "compute_type": w_comp,
                    "download_root": None,
                }
                if w_dev == "cpu" and hw_prof.whisper_cpu_threads > 0:
                    kwargs["cpu_threads"] = hw_prof.whisper_cpu_threads
                model = WhisperModel(w_model, **kwargs)
                self.model = model
                load_ms = int((time.time() - t_load) * 1000)
                print(f"[FlowLocal] Faster-Whisper '{w_model}' ({w_dev}/{w_comp}, Tier {hw_prof.tier.value.upper()}) pronto in {load_ms}ms.")
            except Exception as e:
                print(f"[FlowLocal] Errore caricamento WhisperModel ({w_model} su {w_dev}/{w_comp}): {e}")
                if w_dev != "cpu":
                    try:
                        print("[FlowLocal] Tentativo fallback automatico Whisper su CPU (small/int8)...")
                        model = WhisperModel(
                            "small",
                            device="cpu",
                            compute_type="int8",
                            cpu_threads=hw_prof.whisper_cpu_threads or 4,
                            download_root=None,
                        )
                        self.model = model
                        load_ms = int((time.time() - t_load) * 1000)
                        print(f"[FlowLocal] Faster-Whisper fallback 'small' (cpu/int8) pronto in {load_ms}ms.")
                    except Exception as cpu_err:
                        self._model_error = str(cpu_err)
                        print(f"[FlowLocal] Errore fallback CPU WhisperModel: {cpu_err}")
                else:
                    self._model_error = str(e)
            finally:
                self._model_ready.set()

            detected_lecture = LLMRefiner.detect_lecture_model()
            local_llm = hw_prof.llm_default_model or LLM_MODEL
            print(f"[FlowLocal] Dettatura live: {local_llm} | Sbobinatura d'aula: {detected_lecture} | Profilo: {hw_prof.tier.value.upper()}")

            # Attende brevemente che Ollama (avviato in background) risponda prima del warm-up
            for attempt in range(4):
                try:
                    urllib.request.urlopen("http://localhost:11434/api/version", timeout=0.5)
                    LLMRefiner.refine("Test audio iniziale")
                    break
                except Exception:
                    time.sleep(0.6)

        threading.Thread(target=_load_whisper_and_warm, daemon=True).start()

        self.audio_frames = []
        self.is_recording = False
        self.is_locked = False
        self.is_paused = False
        self.current_mode = "LISTENING"
        self.current_context = ("Desktop", "natural", "Naturale")
        self.active_mode_key = "natural"
        self.active_selection_text: Optional[str] = None
        self.previous_clipboard: Optional[str] = None

        # Stato Background Silence Segmentation (Micro-Chunking in Streaming)
        self._noise_floor: float = 0.003
        self._silence_rms_threshold: float = 0.008
        self._chunk_speech_duration_s: float = 0.0
        self._chunk_silence_duration_s: float = 0.0
        self._streaming_session: Optional[StreamingChunkSession] = None

        self.last_press_time = 0.0
        self.last_release_time = 0.0
        self.record_start_time = 0.0
        self.lock = threading.Lock()
        self.key_lock = threading.Lock()
        self._whisper_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self.is_key_down = False
        self.live_rms = 0.0
        self._always_on_mic = (
            (db.get_setting("whis_handsfree_enabled", "1") or "1").strip().lower() in ("1", "true", "on", "yes")
            or (db.get_setting("always_on_prebuffer", "0") or "0").strip().lower() in ("1", "true", "on", "yes")
        )

        self.dev_index, self.sample_rate = find_best_input_device()
        self.use_decimation = (self.sample_rate != WHISPER_SAMPLE_RATE)
        self.stream = None
        if self._always_on_mic:
            self._start_stream()

    def _start_stream(self):
        with self._stream_lock:
            try:
                if self.stream:
                    self.stream.stop()
                    self.stream.close()
                    self.stream = None
            except Exception:
                self.stream = None

            block_size = int(self.sample_rate * 0.05)
            self.stream = sd.InputStream(
                device=self.dev_index,
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="float32",
                blocksize=block_size,
                callback=self._audio_callback,
            )
            self.stream.start()

    def _stop_stream(self):
        """Chiude e rilascia completamente lo stream microfono di sistema."""
        with self._stream_lock:
            if self.stream is not None:
                try:
                    self.stream.stop()
                except Exception:
                    pass
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
        try:
            self.analyzer.ring_buffer.clear()
        except Exception:
            pass

    def set_always_on_mic(self, enabled: bool):
        """Controlla se il microfono resta aperto in IDLE (pre-buffer) o si attiva solo on-demand."""
        self._always_on_mic = bool(enabled)
        if self._always_on_mic and not self.is_paused:
            if self.stream is None or not getattr(self.stream, "active", False):
                try:
                    self._start_stream()
                except Exception as exc:
                    print(f"[FlowLocal] Avviso riapertura microfono always-on: {exc}")
        elif not self.is_recording:
            self._stop_stream()

    def change_device(self, new_device_idx):
        self.dev_index = new_device_idx
        if new_device_idx is not None:
            try:
                info = sd.query_devices(new_device_idx)
                self.sample_rate = int(info.get('default_samplerate', 16000))
            except Exception:
                self.sample_rate = 16000
        else:
            self.sample_rate = 16000
        self.use_decimation = (self.sample_rate != WHISPER_SAMPLE_RATE)
        if self._always_on_mic or self.is_recording:
            self._start_stream()
        else:
            self._stop_stream()

    def set_mode(self, mode: str):
        self.current_mode = mode
        if self.is_recording and self.orb:
            self.orb.set_state(mode)

    def handle_key_press(self):
        with self.key_lock:
            if self.is_key_down:
                return  # Ignora typematic repeat mentre il tasto è tenuto premuto
            self.is_key_down = True

            now = time.time()
            time_since_last = now - self.last_press_time
            self.last_press_time = now

            if self.is_recording:
                if self.is_locked:
                    self.stop_recording_and_process()
            else:
                if time_since_last < DOUBLE_TAP_TIMEOUT:
                    self.start_recording(locked=True)
                else:
                    self.start_recording(locked=False)

    def handle_key_release(self):
        with self.key_lock:
            now = time.time()
            if (now - self.last_release_time) < 0.350:  # Debounce 350ms
                return
            self.last_release_time = now

            if not self.is_key_down:
                return
            self.is_key_down = False

            if self.is_recording and not self.is_locked:
                # Tail-Padding Microfono (320ms) per catturare fino all'ultima consonante anche se si rilascia il tasto in anticipo
                def _delayed_stop():
                    time.sleep(0.320)
                    with self.key_lock:
                        if self.is_recording and not self.is_locked and not self.is_key_down:
                            self.stop_recording_and_process()
                threading.Thread(target=_delayed_stop, daemon=True).start()

    def lock_current_recording(self):
        """Transizione fluida Push-to-Lock (es. Shift Destro tenuto -> Ctrl Destro / Alt Gr)."""
        with self.key_lock:
            if self.is_recording and not self.is_locked and self.is_key_down:
                self.is_locked = True
                if self.orb:
                    self.orb.set_locked(True)
                    self.orb.set_badge_text("🔒 Bloccato — premi Shift Destro per inviare")
                threading.Thread(target=sound_locked, daemon=True).start()

    def reset_key_state(self):
        """Resetta lo stato del tasto Push-to-Talk dopo sospensione/risveglio di sistema."""
        with self.key_lock:
            self.is_key_down = False
            self.last_release_time = 0.0

    def recover_audio_stream(self):
        """Riavvia lo stream WASAPI solo se il microfono always-on o una registrazione sono attivi."""
        if not self._always_on_mic and not self.is_recording:
            return
        try:
            if self.stream is None or not self.stream.active:
                self._start_stream()
        except Exception:
            try:
                self._start_stream()
            except Exception:
                pass

    def toggle_recording_click(self):
        if self.is_recording:
            self.stop_recording_and_process()
        else:
            self.start_recording(locked=True)

    def toggle_pause(self) -> bool:
        self.is_paused = not self.is_paused
        if self.is_paused and not self.is_recording:
            self._stop_stream()
        elif not self.is_paused and self._always_on_mic:
            try:
                self._start_stream()
            except Exception:
                pass
        sound_toggle_pause(self.is_paused)
        return self.is_paused

    def _audio_callback(self, indata, frames, time_info, status):
        chunk = indata.copy()
        rms = float(np.sqrt(np.mean(chunk**2)))
        self.live_rms = rms

        self.analyzer.process_chunk(chunk)

        if self.orb:
            self.orb.feed_audio(chunk)

        try:
            if self.hub and self.hub.isVisible():
                self.hub.update_live_volume(min(1.0, rms * 10.0))
        except Exception:
            pass

        chunk_dur_s = len(chunk) / float(self.sample_rate or 48000)

        with self.lock:
            if not self.is_recording:
                # Calibrazione adattiva rumore di fondo in IDLE
                if rms < 0.025:
                    self._noise_floor = 0.96 * self._noise_floor + 0.04 * rms
                    self._silence_rms_threshold = max(0.0045, min(0.018, self._noise_floor * 2.4 + 0.0025))
            else:
                self.audio_frames.append(chunk)

                # Background Silence Segmentation (Micro-Chunking in Streaming)
                # Attivo durante la dettatura standard (non durante comandi brevi su selezione testo)
                if self._streaming_session is not None and self.active_selection_text is None:
                    if rms >= self._silence_rms_threshold:
                        self._chunk_speech_duration_s += chunk_dur_s
                        self._chunk_silence_duration_s = 0.0
                    else:
                        if self._chunk_speech_duration_s > 0.25:
                            self._chunk_silence_duration_s += chunk_dur_s

                    # Calcola durata totale accumulata nel buffer corrente
                    accumulated_samples = sum(len(c) for c in self.audio_frames)
                    accumulated_dur_s = accumulated_samples / float(self.sample_rate or 48000)

                    # Quando rilevi una pausa naturale >= 0.9s e il buffer corrente supera i 4.0s
                    if (
                        self._chunk_silence_duration_s >= 0.90
                        and accumulated_dur_s >= 4.0
                        and self._chunk_speech_duration_s >= 0.80
                    ):
                        # Sgancia il segmento accumulato mantenendo gli ultimi ~3 frame silenziosi come pre-roll
                        if len(self.audio_frames) > 4:
                            detached_frames = list(self.audio_frames[:-3])
                            self.audio_frames = list(self.audio_frames[-3:])
                        else:
                            detached_frames = list(self.audio_frames)
                            self.audio_frames.clear()

                        self._chunk_speech_duration_s = 0.0
                        self._chunk_silence_duration_s = 0.0
                        self._streaming_session.enqueue_chunk(detached_frames)

    def start_recording(self, locked=False):
        if self.is_paused:
            return

        with self.lock:
            if self.is_recording:
                return

        # Avvia lo stream microfono on-demand se era spento (Whis OFF) prima di fare qualsiasi altra cosa
        if self.stream is None or not getattr(self.stream, "active", False):
            try:
                self._start_stream()
            except Exception as exc:
                print(f"[FlowLocal] Impossibile aprire microfono on-demand: {exc}")
                return

        # Rilevamento contesto e cattura selezione attiva FUORI da self.lock (non blocca mai _audio_callback!)
        app_label, mode_key, mode_label = detect_active_context()
        dictation_tracker.save_focus_target()

        previous_clip = None
        try:
            previous_clip = pyperclip.paste()
        except Exception:
            pass

        selected_text = None
        from core.context_detector import get_foreground_window_info
        _, fg_exe = get_foreground_window_info()
        fg_exe_clean = (fg_exe or "").lower()

        TERMINAL_EXES = {
            "powershell.exe", "pwsh.exe", "cmd.exe", "windowsterminal.exe",
            "conhost.exe", "bash.exe", "wsl.exe", "alacritty.exe", "mintty.exe"
        }

        if fg_exe_clean not in TERMINAL_EXES and app_label.lower() not in ("powershell", "cmd", "terminal"):
            global LAST_SELECTION_CAPTURE_TIME
            LAST_SELECTION_CAPTURE_TIME = time.time()
            try:
                pyperclip.copy("")
                _send_ctrl_shortcut("c", hold_s=0.045)
                time.sleep(0.020)

                current_clip = (pyperclip.paste() or "").strip()
                if current_clip:
                    selected_text = current_clip
            except Exception:
                selected_text = None

        if not selected_text and previous_clip is not None:
            try:
                pyperclip.copy(previous_clip)
            except Exception:
                pass

        with self.lock:
            if self.is_recording:
                return

            self.current_context = (app_label, mode_key, mode_label)
            self.active_mode_key = mode_key
            self.previous_clipboard = previous_clip

            if selected_text:
                self.active_selection_text = selected_text
                sel_len = len(selected_text)
                badge_str = f"✨ Selezione ({sel_len} car.)"
            else:
                self.active_selection_text = None
                badge_str = None

            # Smart Audio-Ducking su Spotify / App Multimediali (fade-out 120ms al 15%)
            audio_ducker.duck()

            # Pre-buffer di 350ms + reset stato Micro-Chunking in streaming
            self.audio_frames = list(self.analyzer.ring_buffer)
            self._chunk_speech_duration_s = 0.0
            self._chunk_silence_duration_s = 0.0
            self._streaming_session = StreamingChunkSession(
                engine=self,
                src_rate=self.sample_rate,
                mode=self.active_mode_key,
            )
            self.is_recording = True
            self.is_locked = locked
            self.record_start_time = time.time()

            if self.orb:
                if badge_str:
                    self.orb.set_badge_text(badge_str)
                else:
                    self.orb.set_badge_text(f"{app_label} • {mode_label}")
                orb_state = "STUDY_MODE" if mode_key == "study_latex" else self.current_mode
                self.orb.set_state(orb_state)
                self.orb.set_locked(locked)

            if locked:
                threading.Thread(target=sound_locked, daemon=True).start()
            else:
                threading.Thread(target=sound_start, daemon=True).start()

    def stop_recording_and_process(self):
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            self.is_locked = False
            captured = list(self.audio_frames)
            self.audio_frames.clear()
            rec_mode = self.active_mode_key
            rec_context = self.current_context
            selection_text = self.active_selection_text
            prev_clipboard = self.previous_clipboard
            streaming_session = self._streaming_session
            self.active_selection_text = None
            self.previous_clipboard = None
            self._streaming_session = None
            self._chunk_speech_duration_s = 0.0
            self._chunk_silence_duration_s = 0.0

        # Chiudi subito lo stream microfono se siamo in modalità On-Demand (Whis OFF)
        if not self._always_on_mic:
            self._stop_stream()

        # Ripristina il volume originale delle app multimediali (fade-in 200ms)
        audio_ducker.unduck()

        has_prior_chunks = bool(streaming_session and streaming_session.has_enqueued_chunks)

        if self.orb:
            self.orb.set_state("WHISPER")
            self.orb.set_locked(False)

        if not captured and not has_prior_chunks:
            if streaming_session:
                streaming_session.cancel()
            if self.orb:
                self.orb.set_state("IDLE")
            # Ripristina previous_clipboard se presente
            if prev_clipboard is not None:
                try:
                    pyperclip.copy(prev_clipboard)
                except Exception:
                    pass
            return

        total_samples = sum(len(c) for c in captured)
        src_rate = self.sample_rate
        tail_duration_s = total_samples / src_rate if src_rate else 0.0
        total_duration_s = tail_duration_s + (streaming_session.total_audio_duration_s if streaming_session else 0.0)

        if tail_duration_s < 0.20 and not has_prior_chunks:
            if streaming_session:
                streaming_session.cancel()
            if self.orb:
                self.orb.set_state("IDLE")
            if prev_clipboard is not None:
                try:
                    pyperclip.copy(prev_clipboard)
                except Exception:
                    pass
            return

        threading.Thread(
            target=self._transcribe_and_inject,
            args=(captured, src_rate, total_duration_s, rec_mode, rec_context, selection_text, prev_clipboard, streaming_session),
            daemon=True
        ).start()

    @staticmethod
    def _resample_to_16k(raw_audio: np.ndarray, src_rate: int) -> np.ndarray:
        """Ricampionamento polifase veloce a 16kHz (eseguito nel worker thread)."""
        if src_rate == WHISPER_SAMPLE_RATE:
            return raw_audio.astype(np.float32, copy=False)
        if src_rate % WHISPER_SAMPLE_RATE == 0:
            down = src_rate // WHISPER_SAMPLE_RATE
            return signal.resample_poly(raw_audio, up=1, down=down).astype(np.float32)
        g = math.gcd(WHISPER_SAMPLE_RATE, src_rate)
        return signal.resample_poly(
            raw_audio,
            up=WHISPER_SAMPLE_RATE // g,
            down=src_rate // g,
        ).astype(np.float32)

    def _transcribe_single_audio_segment(
        self,
        captured_chunks: List[np.ndarray],
        src_rate: int,
        previous_context: str = "",
    ) -> Tuple[str, int, float]:
        """
        Esegue la pipeline acustica su un singolo segmento audio (o micro-chunk):
        1. Normalizzazione Robusta sul 98° percentile + Soft-Clipping iperbolico (np.tanh)
        2. Beam Search Dinamico (beam_size 1 / 3 / 4) in base a duration_sec
           con condition_on_previous_text=True, vad_filter=True (min_silence_duration_ms=400)
           e initial_prompt arricchito con gli ultimi 60 caratteri del chunk precedente.
        Restituisce (raw_text, whisper_ms, duration_sec).
        """
        if not captured_chunks:
            return "", 0, 0.0

        t0 = time.time()
        raw_audio = np.concatenate(captured_chunks, axis=0).flatten()
        if raw_audio.size == 0:
            return "", 0, 0.0

        audio_data = self._resample_to_16k(raw_audio, src_rate)
        duration_sec = len(audio_data) / float(WHISPER_SAMPLE_RATE)
        if duration_sec < 0.15:
            return "", 0, duration_sec

        # 1. Normalizzazione Audio Robusta con Noise Gate (evita di amplificare il fruscio di fondo)
        audio_data, p98 = normalize_audio_robust(audio_data)
        if p98 < 0.005:
            return "", 0, duration_sec

        # Zero-padding finale di 350ms: impedisce a Silero VAD e Whisper di tagliare l'ultima sillaba
        tail_silence = np.zeros(int(WHISPER_SAMPLE_RATE * 0.35), dtype=np.float32)
        audio_for_whisper = np.concatenate([audio_data, tail_silence])

        # Attende il caricamento asincrono del modello se l'utente ha parlato subito all'avvio
        if not self._model_ready.is_set():
            self._model_ready.wait(timeout=30.0)
        if self.model is None:
            print(f"[FlowLocal] Modello Whisper non disponibile: {self._model_error or 'timeout'}")
            return "", 0, duration_sec

        # 2. Costruzione initial_prompt leggero (evita sovraccarico e copia dal prompt nei silenzi)
        db_prompt = db.get_whisper_initial_prompt(limit=18)
        base_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} {db_prompt}".strip() if db_prompt else DEFAULT_WHISPER_INITIAL_PROMPT
        ctx_tail = (previous_context or "").strip()[-60:].strip()
        whisper_prompt = f"{base_prompt} {ctx_tail}".strip() if ctx_tail else base_prompt

        # 3. Beam Search Dinamico + barriere anti-allucinazione su Faster-Whisper (thread-safe su CUDA)
        beam_size = compute_dynamic_beam_size(duration_sec)
        common_kwargs = dict(
            beam_size=beam_size,
            language="it",
            temperature=0.0,
            condition_on_previous_text=False,
            compression_ratio_threshold=2.2,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            initial_prompt=whisper_prompt,
        )

        with self._whisper_lock:
            segments, _ = self.model.transcribe(
                audio_for_whisper,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
                **common_kwargs,
            )
            segments = list(segments)

            # Fallback senza VAD anche su code brevi (>= 0.18s) se l'energia vocale è reale,
            # evitando di mangiare l'ultima parola dopo una pausa di micro-chunking
            if not segments and duration_sec >= 0.18 and p98 >= 0.012:
                segments, _ = self.model.transcribe(
                    audio_for_whisper,
                    vad_filter=False,
                    **common_kwargs,
                )
                segments = list(segments)

        # Analisi pause (> 1.3s) + filtri qualità/anti-allucinazione/deduplica loop
        text_parts = []
        prev_end = None
        last_norm = ""
        repeat_count = 0

        for seg in segments:
            if getattr(seg, "no_speech_prob", 0.0) > 0.6:
                continue
            avg_lp = getattr(seg, "avg_logprob", 0.0)
            if avg_lp is not None and avg_lp < -1.15:
                continue
            seg_text = (seg.text or "").strip()
            if not seg_text or is_whisper_hallucination(seg_text):
                continue

            norm_seg = seg_text.lower().rstrip(".,!?")
            if norm_seg == last_norm:
                repeat_count += 1
                if repeat_count >= 2:
                    continue
            else:
                last_norm = norm_seg
                repeat_count = 1

            if prev_end is not None and (seg.start - prev_end) > 1.3:
                text_parts.append("[PAUSA_CAMBIO_ARGOMENTO]")

            text_parts.append(seg_text)
            prev_end = seg.end

        raw_text = " ".join(text_parts).strip()
        whisper_ms = int((time.time() - t0) * 1000)
        return raw_text, whisper_ms, duration_sec

    def _transcribe_and_inject(
        self,
        captured_chunks: List[np.ndarray],
        src_rate: int,
        duration_s: float,
        mode: str,
        context: tuple = ("Desktop", "natural", "Naturale"),
        selection_text: Optional[str] = None,
        previous_clipboard: Optional[str] = None,
        streaming_session: Optional[StreamingChunkSession] = None,
    ):
        whisper_ms = 0
        qwen_ms = 0
        launched_to_target = False
        try:
            has_streamed_chunks = bool(streaming_session and streaming_session.has_enqueued_chunks)

            # Se il micro-chunking in background ha già processato segmenti durante il Push-to-Talk:
            if has_streamed_chunks and not (selection_text and selection_text.strip()):
                prior_results = streaming_session.finalize_and_collect(timeout=25.0)
                prev_ctx = streaming_session.previous_context

                tail_raw, tail_w_ms, _ = self._transcribe_single_audio_segment(
                    captured_chunks,
                    src_rate,
                    previous_context=prev_ctx,
                )

                raw_parts = [r[0] for r in prior_results if r[0]]
                refined_parts = [r[1] for r in prior_results if r[1]]
                whisper_ms = sum(r[2] for r in prior_results) + tail_w_ms
                qwen_ms = sum(r[3] for r in prior_results)

                if tail_raw:
                    tail_clean = apply_inline_voice_commands(tail_raw)
                    if tail_clean:
                        tail_clean = normalize_phonetic_variants(tail_clean)
                        tail_clean = db.apply_phonetic_replacements(tail_clean)
                        raw_parts.append(tail_clean)

                        if self.orb:
                            self.orb.set_state("QWEN")
                        tail_refined, tail_q_ms = LLMRefiner.refine(
                            tail_clean,
                            mode=mode,
                            previous_context=prev_ctx,
                        )
                        qwen_ms += tail_q_ms
                        tail_refined = strip_pause_markers(tail_refined).rstrip()
                        while tail_refined.endswith('\\'):
                            tail_refined = tail_refined[:-1].rstrip()
                        if tail_refined:
                            refined_parts.append(tail_refined)

                raw_text = join_streaming_chunks(raw_parts)
                final_text = join_streaming_chunks(refined_parts)

                if not raw_text and not final_text:
                    print("[FlowLocal] Nessun testo vocale rilevato nei micro-chunk.")
                    if self.orb:
                        self.orb.set_state("IDLE")
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass
                    return

                print(
                    f"[FlowLocal] Micro-Chunking completato ({len(prior_results) + (1 if tail_raw else 0)} segmenti, "
                    f"Whisper tot: {whisper_ms}ms, Qwen tot: {qwen_ms}ms): {final_text}"
                )
            else:
                if streaming_session:
                    streaming_session.cancel()

                raw_text, whisper_ms, _ = self._transcribe_single_audio_segment(
                    captured_chunks,
                    src_rate,
                    previous_context="",
                )

                if not raw_text:
                    print("[FlowLocal] Nessun testo vocale rilevato o livello audio impercettibile.")
                    if self.orb:
                        self.orb.set_state("IDLE")
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass
                    return

                print(f"[FlowLocal] Trascrizione grezza Whisper ({whisper_ms}ms): {raw_text}")
                final_text = ""

            # =========================================================
            # CASO 0: ROUTER VOCALE ASSISTENTE WHIS (Zero-Focus)
            # =========================================================
            raw_text = normalize_phonetic_variants(raw_text)
            whis_match = WHIS_WAKE_REGEX.match(raw_text.strip())
            if whis_match:
                # Rimozione del prefisso wake-word
                query_text = WHIS_WAKE_REGEX.sub('', raw_text.strip()).strip()
                query_text = re.sub(r'^[\s,.:;—–-]+', '', query_text).strip()
                if query_text and query_text[0].islower():
                    query_text = query_text[0].upper() + query_text[1:]
                elif not query_text:
                    query_text = "Ciao Whis"

                print(f"[FlowLocal] Intercettata wake-word Whis: '{query_text}'")

                # Ripristina previous_clipboard se necessario
                if previous_clipboard is not None:
                    try:
                        pyperclip.copy(previous_clipboard)
                    except Exception:
                        pass

                # Reset stato VoiceOrb a IDLE
                if self.orb:
                    self.orb.set_state("IDLE")

                sound_done()

                # Notifica WhisHUD in modalità thread-safe sul thread principale
                if self.whis_hud and hasattr(self.whis_hud, "request_invoke"):
                    self.whis_hud.request_invoke.emit(query_text, selection_text or "")

                # Tracciamento nel database locale
                app_label, _, _ = context
                db.add_transcription(
                    raw_text=raw_text,
                    final_text=f"[Whis] {query_text}",
                    target_app=app_label,
                    style_used="Whis",
                    whisper_ms=whisper_ms,
                    qwen_ms=0,
                    duration_s=duration_s
                )
                return

            # =========================================================
            # CASO A: TRASFORMAZIONE SELEZIONE ATTIVA (Text-to-Action)
            # =========================================================
            if selection_text and selection_text.strip():
                if self.orb:
                    self.orb.set_state("QWEN")

                t_qwen = time.time()
                transformed_text = LLMRefiner.transform_selection(
                    selected_text=selection_text,
                    voice_command=raw_text
                )
                qwen_ms = int((time.time() - t_qwen) * 1000)

                # Sovrascrive direttamente il testo evidenziato nella finestra attiva
                pyperclip.copy(transformed_text)
                time.sleep(0.04)

                _send_ctrl_shortcut("v", hold_s=0.03)

                sound_done()

                # Ripristina previous_clipboard dopo ~150ms
                def restore_clip():
                    time.sleep(0.18)
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass

                threading.Thread(target=restore_clip, daemon=True).start()

                # Salvataggio nel database locale con mode="Selezione"
                app_label, _, _ = context
                db.add_transcription(
                    raw_text=f"[Comando: {raw_text}] Su selezione ({len(selection_text)} car.)",
                    final_text=transformed_text,
                    target_app=app_label,
                    style_used="Selezione",
                    whisper_ms=whisper_ms,
                    qwen_ms=qwen_ms,
                    duration_s=duration_s
                )
                return

            # =========================================================
            # CASO B: DETTATURA NORMALE ESISTENTE
            # =========================================================

            # 3. Intercettazione apprendimento vocale istantaneo ("Correggi X in Y")
            learn_match = VOICE_LEARN_REGEX.match(raw_text.strip())
            if learn_match:
                errata = learn_match.group(1).strip().strip('"\'.,')
                corretta = learn_match.group(2).strip().strip('"\'.,')
                if errata and corretta and errata.lower() != corretta.lower():
                    print(f"[FlowLocal] Apprendimento vocale istantaneo: '{errata}' -> '{corretta}'")
                    db.learn_vocabulary_pair(errata, corretta)
                    if self.orb:
                        self.orb.show_toast(f"+ Imparato: {errata} → {corretta}", duration_s=2.5)
                    sound_done()
                    return

            if not has_streamed_chunks:
                # Comandi vocali inline di auto-correzione ("Anzi, cancella tutto...")
                raw_text = apply_inline_voice_commands(raw_text)
                if not raw_text:
                    print("[FlowLocal] Dettatura annullata tramite comando vocale inline.")
                    if self.orb:
                        self.orb.set_state("IDLE")
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass
                    sound_done()
                    return

                # Normalizzazione automatica varianti fonetiche e sostituzione alias da DB
                raw_text = normalize_phonetic_variants(raw_text)
                raw_text = db.apply_phonetic_replacements(raw_text)

                # Stadio 2: Rifinitura Qwen 2.5
                if self.orb:
                    self.orb.set_state("QWEN")

                final_text, qwen_ms = LLMRefiner.refine(raw_text, mode=mode)
                final_text = strip_pause_markers(final_text).rstrip()
                while final_text.endswith('\\'):
                    final_text = final_text[:-1].rstrip()
            else:
                # Se nella coda l'utente ha pronunciato "cancella tutto" sull'intero dettato
                checked_inline = apply_inline_voice_commands(final_text)
                if not checked_inline:
                    print("[FlowLocal] Dettatura in streaming annullata tramite comando vocale inline.")
                    if self.orb:
                        self.orb.set_state("IDLE")
                    if previous_clipboard is not None:
                        try:
                            pyperclip.copy(previous_clipboard)
                        except Exception:
                            pass
                    sound_done()
                    return
                final_text = checked_inline.rstrip()
                while final_text.endswith('\\'):
                    final_text = final_text[:-1].rstrip()

            # 4. Trigger Vocale Zero-Focus per Appunti Rapidi ("Prendi nota", "Appunto", ecc.)
            is_quick_note = bool(
                QUICK_NOTE_TRIGGER_REGEX.match(raw_text.strip()) or
                QUICK_NOTE_TRIGGER_REGEX.match(final_text.strip())
            )
            if is_quick_note:
                # Rimuove il prefisso trigger dal testo raffinato
                clean_note = QUICK_NOTE_TRIGGER_REGEX.sub('', final_text).strip()
                if not clean_note:
                    clean_note = QUICK_NOTE_TRIGGER_REGEX.sub('', raw_text).strip() or final_text.strip()

                # Rimuove eventuale punteggiatura iniziale residua
                clean_note = re.sub(r'^[\s,.:;—–-]+', '', clean_note).strip()
                if clean_note:
                    clean_note = clean_note[0].upper() + clean_note[1:]

                title = LLMRefiner.generate_note_title(clean_note) or (clean_note.split("\n")[0][:45].strip() or "Appunto Rapido")
                if len(title) > 58:
                    title = title[:55].rstrip() + "..."

                print(f"[FlowLocal] Appunto rapido salvato (zero-focus): '{title}'")
                db.add_study_note(
                    title=title,
                    content_markdown=clean_note,
                    note_type="Appunto Rapido",
                    tags="Vocale, Rapido"
                )

                app_label, _, _ = context
                db.add_transcription(
                    raw_text=raw_text,
                    final_text=clean_note,
                    target_app=app_label,
                    style_used="Appunto Rapido",
                    whisper_ms=whisper_ms,
                    qwen_ms=qwen_ms,
                    duration_s=duration_s
                )

                # Notifica discreta su HUD (zero focus steal) e segnale sonoro senza incolla
                if self.orb:
                    self.orb.show_toast("⚡ Appunto rapido salvato nell'Hub", duration_s=2.5)
                sound_done()

                # Notifica thread-safe all'Hub per refresh immediato se aperto
                if self.hub and hasattr(self.hub, "note_added"):
                    self.hub.note_added.emit()

                return

            # Salvataggio immediato negli appunti + tracker cronologia e Ghost-Text
            app_label, mode_key, mode_label = context
            target_pt = WindowsInjector.get_target_point()
            if target_pt is None:
                dictation_tracker.restore_focus_target()
                target_pt = WindowsInjector.get_target_point()

            dictation_tracker.record_dictation(
                final_text,
                app_name=app_label,
                style=mode_label,
                consumed=False,
            )

            # Iniezione testo e feedback sonoro
            WindowsInjector.paste_text(final_text)
            sound_done()
            if self.orb and target_pt:
                self.orb.launch_to_target(target_pt[0], target_pt[1])
                launched_to_target = True

            # Watcher passivo su clipboard per correzioni manuali post-incolla (12 secondi)
            self._start_clipboard_learning_watcher(final_text)

            # Salvataggio nel database locale
            db.add_transcription(
                raw_text=raw_text,
                final_text=final_text,
                target_app=app_label,
                style_used=mode_label,
                whisper_ms=whisper_ms,
                qwen_ms=qwen_ms,
                duration_s=duration_s
            )

            # Se in Study Mode o study_latex, salva una copia negli Appunti di Studio
            if mode in ("STUDY_MODE", "study_latex"):
                title_preview = final_text.split("\n")[0][:40] or "Appunto Studio"
                db.add_study_note(title=title_preview, content_markdown=final_text, note_type="Note", tags="Studio")

        except Exception as e:
            print("Transcription error:", e)
        finally:
            if self.orb and not launched_to_target:
                self.orb.set_state("IDLE")

    @staticmethod
    def _find_correction_pair(injected_text: str, clip_text: str) -> Optional[Tuple[str, str]]:
        clip = (clip_text or "").strip()
        injected = (injected_text or "").strip()
        if not clip or not injected or clip == injected:
            return None

        orig_words = injected.split()
        clip_words = clip.split()

        # Caso 1: L'utente ha selezionato e copiato/tagliato la frase o frammento modificato (>2 parole)
        if len(clip_words) > 2:
            matcher = difflib.SequenceMatcher(None, orig_words, clip_words)
            opcodes = matcher.get_opcodes()
            replace_ops = [op for op in opcodes if op[0] == 'replace']
            if len(replace_ops) == 1:
                tag, i1, i2, j1, j2 = replace_ops[0]
                if (1 <= (i2 - i1) <= 2) and (1 <= (j2 - j1) <= 2):
                    errata = ' '.join(orig_words[i1:i2])
                    corretta = ' '.join(clip_words[j1:j2])
                    errata_clean = re.sub(r'^[^\w]+|[^\w]+$', '', errata)
                    corretta_clean = re.sub(r'^[^\w]+|[^\w]+$', '', corretta)
                    if errata_clean and corretta_clean and errata_clean.lower() != corretta_clean.lower():
                        sim = difflib.SequenceMatcher(None, errata_clean.lower(), corretta_clean.lower()).ratio()
                        if sim > 0.45 and len(errata_clean) <= 30 and len(corretta_clean) <= 30:
                            return errata_clean, corretta_clean

        # Caso 2: L'utente ha copiato solo la singola parola/espressione corretta (1 o 2 parole)
        if 1 <= len(clip_words) <= 2 and len(clip) <= 30:
            corretta_clean = re.sub(r'^[^\w]+|[^\w]+$', '', clip)
            if not corretta_clean:
                return None
            best_match = None
            best_sim = 0.45
            for n in (1, 2):
                for i in range(len(orig_words) - n + 1):
                    cand = ' '.join(orig_words[i:i+n])
                    cand_clean = re.sub(r'^[^\w]+|[^\w]+$', '', cand)
                    if not cand_clean or cand_clean.lower() == corretta_clean.lower():
                        continue
                    sim = difflib.SequenceMatcher(None, cand_clean.lower(), corretta_clean.lower()).ratio()
                    if sim > best_sim and len(cand_clean) <= 30:
                        best_sim = sim
                        best_match = (cand_clean, corretta_clean)
            if best_match:
                return best_match

        return None

    def _start_clipboard_learning_watcher(self, injected_text: str):
        if not injected_text or not injected_text.strip():
            return
        threading.Thread(
            target=self._clipboard_learning_worker,
            args=(injected_text.strip(),),
            daemon=True
        ).start()

    def _clipboard_learning_worker(self, injected_text: str):
        # Attesa per il completamento dell'iniezione Ctrl+V e del ripristino clipboard iniziale
        time.sleep(0.7)
        try:
            initial_clip = pyperclip.paste()
        except Exception:
            initial_clip = ""

        start_time = time.time()
        last_clip = initial_clip

        while (time.time() - start_time) < 12.0:
            time.sleep(0.35)
            try:
                curr_clip = pyperclip.paste()
            except Exception:
                continue

            if not curr_clip or curr_clip == last_clip or curr_clip == injected_text:
                continue

            last_clip = curr_clip
            pair = self._find_correction_pair(injected_text, curr_clip)
            if pair:
                errata, corretta = pair
                print(f"[FlowLocal] Apprendimento passivo da correzione clipboard: '{errata}' -> '{corretta}'")
                db.learn_vocabulary_pair(errata, corretta)
                if self.orb:
                    self.orb.show_toast(f"+ Imparato: {errata} → {corretta}", duration_s=2.5)
                break

    def transcribe_audio_file(
        self,
        file_path: str,
        subject_context: str = "",
        progress_callback: Optional[Callable[..., None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        return_sync_data: bool = False
    ):
        """Sbobina un file audio o video (.mp3, .m4a, .wav, .ogg, .mp4, .mov, .mkv, .webm)
        con pre-processing d'aula, de-noising FFT, Faster-Whisper, gestione VRAM e Qwen 2.5 7B."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File non trovato: {file_path}")

        temp_wav_path = None
        target_audio_path = file_path

        def report(msg: str, percent: int = -1):
            if progress_callback:
                try:
                    # Supporta callback a 1 o 2 argomenti (msg, percent)
                    progress_callback(msg, percent)
                except TypeError:
                    try:
                        progress_callback(msg)
                    except Exception:
                        pass
                except Exception:
                    pass
            pct_str = f" [{percent}%]" if percent >= 0 else ""
            print(f"[FlowLocal Sbobina]{pct_str} {msg}")

        def check_cancelled():
            if cancel_check and cancel_check():
                raise InterruptedError("Sbobinatura annullata dall'utente.")

        check_cancelled()

        # 1. Pre-elaborazione FFmpeg con De-noiser d'aula (afftdn + dynaudnorm)
        report("Fase 1/3: Pre-elaborazione audio e de-noising aula in corso...", 5)
        ffmpeg_exe = None
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

        if not ffmpeg_exe:
            try:
                res = subprocess.run(["ffmpeg", "-version"], capture_output=True)
                if res.returncode == 0:
                    ffmpeg_exe = "ffmpeg"
            except Exception:
                ffmpeg_exe = None

        if ffmpeg_exe:
            try:
                temp_fd, temp_wav_path = tempfile.mkstemp(suffix="_cleaned_lecture.wav")
                os.close(temp_fd)

                # Catena filtri: taglio rumble (80Hz), cutoff alte frequenze (7500Hz),
                # denoiser FFT d'aula (afftdn -25dB), normalizzatore dinamico (dynaudnorm)
                cmd = [
                    ffmpeg_exe, "-y", "-i", file_path,
                    "-af", "highpass=f=80,lowpass=f=7500,afftdn=nf=-25,dynaudnorm=f=150:g=15",
                    "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le",
                    temp_wav_path
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                check_cancelled()

                if proc.returncode == 0 and os.path.exists(temp_wav_path) and os.path.getsize(temp_wav_path) > 100:
                    target_audio_path = temp_wav_path
                    report("Filtri d'aula e de-noising applicati con successo.", 15)
                else:
                    report("FFmpeg ha restituito errore. Utilizzo file sorgente come fallback.", 15)
            except InterruptedError:
                raise
            except Exception as e:
                report(f"Avviso FFmpeg: {e}. Procedo con audio originale.", 15)
        else:
            report("FFmpeg non disponibile: procedo direttamente con il file sorgente.", 15)

        try:
            check_cancelled()

            # Carica l'audio direttamente come ndarray float32 se abbiamo generato il WAV a 16kHz
            audio_input = target_audio_path
            if target_audio_path == temp_wav_path:
                try:
                    with wave.open(temp_wav_path, "rb") as wf:
                        raw_bytes = wf.readframes(wf.getnframes())
                        audio_input = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                except Exception:
                    audio_input = target_audio_path

            # 2. Trascrizione Faster-Whisper (Profilo Aula: threshold 0.35, min_silence 800ms)
            report("Fase 2/3: Trascrizione acustica Faster-Whisper (Profilo Aula)...", 25)
            base_prompt = db.get_whisper_initial_prompt(limit=40) or ""
            clean_ctx = (subject_context or "").strip()
            if clean_ctx:
                if base_prompt:
                    whisper_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} Lezione universitaria di {clean_ctx}. {base_prompt}. Trascrizione accurata in italiano."
                else:
                    whisper_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} Lezione universitaria di {clean_ctx}. Glossario: {clean_ctx}. Trascrizione accurata in italiano."
            else:
                whisper_prompt = f"{DEFAULT_WHISPER_INITIAL_PROMPT} {base_prompt}".strip() or None

            check_cancelled()

            if not self._model_ready.is_set():
                self._model_ready.wait(timeout=30.0)
            if self.model is None:
                raise RuntimeError(f"Modello Whisper non pronto: {self._model_error or 'timeout'}")

            with self._whisper_lock:
                segments, info = self.model.transcribe(
                    audio_input,
                    beam_size=5,
                    language="it",
                    temperature=0.0,
                    condition_on_previous_text=False,
                    compression_ratio_threshold=2.2,
                    log_prob_threshold=-1.2,
                    no_speech_threshold=0.65,
                    word_timestamps=True,
                    vad_filter=True,
                    vad_parameters=dict(
                        threshold=0.35,
                        min_silence_duration_ms=800,
                    ),
                    initial_prompt=whisper_prompt,
                )
                segments = list(segments)

            raw_parts = []
            sync_segments: List[Dict[str, Any]] = []
            repeat_count = 0
            last_text = ""
            last_timestamp_sec = -999.0
            unclear_start: Optional[float] = None
            unclear_end: Optional[float] = None

            def flush_unclear():
                nonlocal unclear_start, unclear_end
                if unclear_start is not None and unclear_end is not None:
                    m1, s1 = int(unclear_start) // 60, int(unclear_start) % 60
                    m2, s2 = int(unclear_end) // 60, int(unclear_end) % 60
                    raw_parts.append(f"[AUDIO_NON_CHIARO da {m1:02d}:{s1:02d} a {m2:02d}:{s2:02d}]")
                    unclear_start = None
                    unclear_end = None

            for seg in segments:
                check_cancelled()
                start_sec = getattr(seg, "start", 0.0) or 0.0
                end_sec = getattr(seg, "end", start_sec) or start_sec
                avg_logprob = getattr(seg, "avg_logprob", 0.0)

                # Se la confidenza acustica è troppo bassa (voce sussurrata, rauca o rumori d'aula)
                if avg_logprob is not None and avg_logprob < -1.15:
                    if unclear_start is None:
                        unclear_start = start_sec
                    unclear_end = end_sec
                    continue

                flush_unclear()

                text_clean = seg.text.strip()
                if not text_clean:
                    continue

                # Filtro post-segmento: scarta o deduplica frasi consecutive identiche ripetute in loop (> 2 volte)
                norm_text = text_clean.lower().rstrip(".,!?")
                if norm_text == last_text:
                    repeat_count += 1
                    if repeat_count > 2:
                        continue
                else:
                    last_text = norm_text
                    repeat_count = 1

                # Inserisce un riferimento temporale [MM:SS] all'inizio o ogni ~30 secondi
                insert_time_prefix = ""
                if (start_sec - last_timestamp_sec) >= 30.0 or last_timestamp_sec < 0:
                    m = int(start_sec) // 60
                    s = int(start_sec) % 60
                    insert_time_prefix = f"[{m:02d}:{s:02d}] "
                    last_timestamp_sec = start_sec

                raw_parts.append(f"{insert_time_prefix}{text_clean}")

                seg_words = []
                for w_obj in (getattr(seg, "words", None) or []):
                    w_txt = (getattr(w_obj, "word", "") or "").strip()
                    if not w_txt:
                        continue
                    w_s = round(float(getattr(w_obj, "start", start_sec) or start_sec), 3)
                    w_e = round(float(getattr(w_obj, "end", w_s) or w_s), 3)
                    seg_words.append({"w": w_txt, "s": w_s, "e": max(w_s, w_e)})
                sync_segments.append({
                    "start": round(float(start_sec), 3),
                    "end": round(float(end_sec), 3),
                    "text": text_clean,
                    "words": seg_words,
                })

            flush_unclear()

            full_raw_text = " ".join(raw_parts).strip()
            if not full_raw_text:
                empty_md = "*(Nessun parlato rilevato nel file audio)*"
                return (empty_md, {"segments": []}) if return_sync_data else empty_md

            # Normalizzazione automatica varianti fonetiche e glossario utente
            full_raw_text = normalize_phonetic_variants(full_raw_text)
            full_raw_text = db.apply_phonetic_replacements(full_raw_text)

            check_cancelled()

            # 3. Suddivisione in chunk da circa 2200 caratteri (a fine frase) per rielaborazione fluida
            sentences = full_raw_text.replace("\r\n", "\n").split(". ")
            chunks = []
            curr_chunk = []
            curr_len = 0

            for s in sentences:
                s_strip = s.strip()
                if not s_strip:
                    continue
                add_len = len(s_strip) + 2
                if curr_len + add_len > 2200 and curr_chunk:
                    chunks.append(". ".join(curr_chunk) + ".")
                    curr_chunk = [s_strip]
                    curr_len = len(s_strip)
                else:
                    curr_chunk.append(s_strip)
                    curr_len += add_len

            if curr_chunk:
                chunks.append(". ".join(curr_chunk) + ("" if curr_chunk[-1].endswith(".") else "."))

            # 4. Gestione dinamica VRAM: scarica il 4B prima di avviare il 9B
            active_lecture_model = LLMRefiner.detect_lecture_model()
            if "9b" in active_lecture_model:
                report("Ottimizzazione VRAM: scaricamento modello 4B per liberare VRAM...", 42)
                _unload_ollama_model(LLM_MODEL)

            total_chunks = len(chunks)
            report(f"Fase 3/3: Riformattazione {active_lecture_model} (0/{total_chunks})...", 45)

            formatted_blocks = []
            for i, chunk in enumerate(chunks, 1):
                check_cancelled()
                pct = int(45 + (i / total_chunks) * 45)  # Da 45% a 90%
                report(f"Riformattazione blocco {i}/{total_chunks} ({active_lecture_model})...", pct)
                formatted = LLMRefiner.reformat_lecture_block(chunk, subject_context=subject_context)
                formatted_blocks.append((formatted or "").strip())

            check_cancelled()
            lecture_body = "\n\n".join(b for b in formatted_blocks if b).strip()

            # Pulizia regex finale: rimozione di eventuali backtick attorno ai segnaposto temporali
            lecture_body = re.sub(r'`+(\[Audio non chiaro[^\]]+\])`+', r'\1', lecture_body)
            lecture_body = re.sub(r'`+(\*{0,2}\[Audio non chiaro[^\]]+\]\*{0,2})`+', r'\1', lecture_body)

            # 5. Generazione automatica Sommario Esecutivo
            report("Generazione Sommario Esecutivo della lezione...", 94)
            check_cancelled()
            summary = LLMRefiner.generate_lecture_summary(lecture_body, subject_context=subject_context)
            clean_summary = (summary or "").strip()

            if clean_summary:
                summary_lines = [
                    re.sub(r'^>\s*', '', sl).strip()
                    for sl in clean_summary.splitlines()
                    if sl.strip()
                ]
                # Rimuovi eventuale intestazione ridondante "📌 Punti Chiave della Lezione:"
                if summary_lines and "punti chiave" in summary_lines[0].lower():
                    summary_lines = summary_lines[1:]
                summary_body = "\n".join(summary_lines).strip()
                if summary_body:
                    ai_note_block = f":::ai-note Sommario Esecutivo\n{summary_body}\n:::"
                    final_markdown = f"{ai_note_block}\n\n{lecture_body}".strip()
                else:
                    final_markdown = lecture_body
            else:
                final_markdown = lecture_body

            report("Sbobinatura completata con successo!", 100)
            if return_sync_data:
                return final_markdown, {"segments": sync_segments}
            return final_markdown

        finally:
            if temp_wav_path and os.path.exists(temp_wav_path):
                try:
                    os.remove(temp_wav_path)
                except Exception:
                    pass

            # Ripristino dinamico VRAM: scarica 9B e ricarica 4B per dettatura rapida push-to-talk
            try:
                active_model = LLMRefiner.detect_lecture_model()
                if "9b" in active_model:
                    _unload_ollama_model(LLM_HIGH_PRECISION_MODEL)
                _preload_ollama_model(LLM_MODEL, "10m")
            except Exception:
                pass
