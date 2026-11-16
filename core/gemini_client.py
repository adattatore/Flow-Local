"""
core/gemini_client.py
Client unificato per Google Gemini API (BYOK — Bring Your Own Key) e routing Ibrido.
Usa il client ufficiale `google-genai` se installato, oppure la REST API ufficiale
v1beta di Google Gemini via `urllib.request` senza dipendenze esterne aggiuntive.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_TIMEOUT = 8.0
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

_runtime_lock = threading.Lock()
_active_runtime_engine: str = ""
_runtime_listeners: List[Callable[[str], None]] = []


def set_active_runtime_engine(label: str):
    """Aggiorna lo stato del motore AI attivo in tempo reale e notifica i listener UI."""
    global _active_runtime_engine
    clean = (label or "").strip()
    if not clean:
        return
    with _runtime_lock:
        if _active_runtime_engine == clean:
            return
        _active_runtime_engine = clean
        listeners = list(_runtime_listeners)
    for cb in listeners:
        try:
            cb(clean)
        except Exception:
            pass


def get_active_runtime_engine(db_manager=None) -> str:
    """Restituisce l'etichetta discreta del motore AI attualmente in uso."""
    with _runtime_lock:
        if _active_runtime_engine:
            return _active_runtime_engine
    cfg = get_ai_routing_config(db_manager)
    prov = cfg["provider"]
    if prov == "adaptive":
        return "Adattivo • Gemini Online" if cfg["has_key"] else "Adattivo • Qwen 3.5 Offline"
    if prov == "gemini":
        return "Cloud • Gemini Online"
    if prov == "hybrid":
        return "Ibrido • Gemini + Qwen 3.5"
    return "Locale • Qwen 3.5"


def add_runtime_engine_listener(callback: Callable[[str], None]):
    with _runtime_lock:
        if callback not in _runtime_listeners:
            _runtime_listeners.append(callback)


def remove_runtime_engine_listener(callback: Callable[[str], None]):
    with _runtime_lock:
        if callback in _runtime_listeners:
            _runtime_listeners.remove(callback)


def is_ollama_available(base_url: str = "http://localhost:11434", timeout: float = 0.8) -> bool:
    """Verifica rapida e non bloccante se il daemon Ollama è attivo localmente."""
    url = f"{(base_url or 'http://localhost:11434').rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


class GeminiClientError(RuntimeError):
    """Errore di comunicazione o autenticazione con Google Gemini API."""


def normalize_provider(raw_provider: Optional[str]) -> str:
    val = (raw_provider or "local").strip().lower()
    if val in ("adaptive", "adattivo", "auto"):
        return "adaptive"
    if val in ("ollama", "local", ""):
        return "local"
    if val in ("gemini", "cloud"):
        return "gemini"
    if val == "hybrid":
        return "hybrid"
    return "local"


def get_ai_routing_config(db_manager=None) -> Dict[str, Any]:
    """Legge da SQLite la configurazione attiva del motore AI e i parametri BYOK."""
    if db_manager is None:
        from database import db as db_manager

    raw_prov = db_manager.get_setting("ai_provider", "")
    api_key = (db_manager.get_setting("gemini_api_key", "") or "").strip()
    if not raw_prov and api_key:
        provider = "adaptive"
    else:
        provider = normalize_provider(raw_prov or "local")
    gemini_model = (db_manager.get_setting("gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL).strip()
    hybrid_fallback = True if provider == "adaptive" else (db_manager.get_setting("ai_hybrid_fallback", "1") or "1").strip() == "1"
    ollama_base = (db_manager.get_setting("ollama_base_url", "http://localhost:11434") or "http://localhost:11434").rstrip("/")

    return {
        "provider": provider,
        "api_key": api_key,
        "has_key": bool(api_key),
        "gemini_model": gemini_model,
        "hybrid_fallback": hybrid_fallback,
        "ollama_base_url": ollama_base,
        "ollama_chat_url": f"{ollama_base}/api/chat",
    }


def should_use_gemini(task_type: str, db_manager=None) -> bool:
    """
    Determina se il task corrente deve essere instradato a Google Gemini Cloud BYOK.
    task_type supportati:
      - 'dictation': post-processing rapido dettatura (in 'adaptive'/'gemini' -> Gemini con fallback locale)
      - 'whis_chat': chat rapida Whis (in 'adaptive'/'gemini' -> Gemini con fallback locale)
      - 'whis_screen': analisi schermo/immagini Whis (in 'adaptive'/'hybrid'/'gemini' -> Gemini)
      - 'whis_deep': analisi profonda/codice complesso Whis (in 'adaptive'/'hybrid'/'gemini' -> Gemini)
      - 'vision_ocr': Vision OCR /ocr / Ctrl+Shift+V (in 'adaptive'/'hybrid'/'gemini' -> Gemini)
      - 'lecture_heavy': sbobine lunghe, sommari, flashcard, correlazioni (in 'adaptive'/'hybrid'/'gemini' -> Gemini)
    """
    cfg = get_ai_routing_config(db_manager)
    if not cfg["has_key"]:
        return False

    provider = cfg["provider"]
    if provider in ("adaptive", "gemini"):
        return True
    if provider == "hybrid":
        return task_type in ("vision_ocr", "whis_screen", "whis_deep", "lecture_heavy")
    return False


def verify_api_key(api_key: str, model: str = DEFAULT_GEMINI_MODEL, timeout: float = 6.0) -> Tuple[bool, str]:
    """Verifica rapida della chiave API Gemini. Restituisce (ok, messaggio)."""
    key = (api_key or "").strip()
    if not key:
        return False, "Chiave non valida / Offline (chiave vuota)"

    # Tentativo con google-genai se installato
    try:
        from google import genai  # type: ignore

        client = genai.Client(api_key=key)
        client.models.get(model=model)
        return True, "Chiave valida"
    except ImportError:
        pass
    except Exception as exc:
        err_str = str(exc)
        if "400" in err_str or "401" in err_str or "403" in err_str or "API_KEY_INVALID" in err_str:
            return False, "Chiave non valida"
        return False, f"Chiave non valida / Offline ({err_str[:60]})"

    # REST API diretta verso Google AI Studio
    url = f"{GEMINI_API_BASE}/models/{model}?key={key}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True, "Chiave valida"
            return False, f"Chiave non valida (HTTP {resp.status})"
    except urllib.error.HTTPError as he:
        if he.code in (400, 401, 403):
            return False, f"Chiave non valida (HTTP {he.code})"
        return False, f"Chiave non valida / Offline (HTTP {he.code})"
    except Exception:
        return False, "Chiave non valida / Offline"


def _build_rest_contents(
    messages: List[Dict[str, Any]],
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/png",
) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        role = "model" if msg.get("role") == "assistant" else "user"
        parts: List[Dict[str, Any]] = []
        text = str(msg.get("content") or "")
        if text:
            parts.append({"text": text})
        # Allega eventuale immagine sull'ultimo messaggio utente
        if image_bytes and idx == len(messages) - 1 and role == "user":
            parts.append({
                "inline_data": {
                    "mime_type": image_mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            })
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def generate_content(
    prompt_text: str,
    *,
    system_instruction: str = "",
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/png",
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
    stop_sequences: Optional[List[str]] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_GEMINI_TIMEOUT,
) -> str:
    """Esegue una chiamata sincrona a Gemini (SDK google-genai o REST API v1beta)."""
    cfg = get_ai_routing_config()
    resolved_key = (api_key or cfg["api_key"]).strip()
    resolved_model = (model or cfg["gemini_model"] or DEFAULT_GEMINI_MODEL).strip()
    if not resolved_key:
        raise GeminiClientError("API Key Gemini mancante.")

    # 1. Tentativo tramite google-genai SDK se presente
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        client = genai.Client(api_key=resolved_key)
        parts: List[Any] = [prompt_text]
        if image_bytes:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))
        config_kwargs: Dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if stop_sequences:
            config_kwargs["stop_sequences"] = stop_sequences

        response = client.models.generate_content(
            model=resolved_model,
            contents=parts,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        out_text = (getattr(response, "text", None) or "").strip()
        if out_text:
            return out_text
        raise GeminiClientError("Risposta vuota da Gemini SDK.")
    except ImportError:
        pass
    except GeminiClientError:
        raise
    except Exception as exc:
        raise GeminiClientError(f"Errore Gemini SDK: {exc}") from exc

    # 2. REST API diretta v1beta
    url = f"{GEMINI_API_BASE}/models/{resolved_model}:generateContent?key={resolved_key}"
    gen_cfg: Dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if stop_sequences:
        gen_cfg["stopSequences"] = stop_sequences

    payload: Dict[str, Any] = {
        "contents": _build_rest_contents(
            [{"role": "user", "content": prompt_text}],
            image_bytes=image_bytes,
            image_mime=image_mime,
        ),
        "generationConfig": gen_cfg,
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise GeminiClientError(f"Errore REST Gemini: {exc}") from exc

    candidates = raw.get("candidates") or []
    if not candidates:
        raise GeminiClientError("Nessun candidato restituito da Gemini.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_chunks = [str(p.get("text") or "") for p in parts if p.get("text")]
    out = "".join(text_chunks).strip()
    if not out:
        raise GeminiClientError("Risposta testuale vuota da Gemini.")
    return out


def stream_generate_content(
    messages: List[Dict[str, Any]],
    *,
    system_instruction: str = "",
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/png",
    temperature: float = 0.45,
    max_output_tokens: int = 2048,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_GEMINI_TIMEOUT,
) -> Iterator[str]:
    """Esegue una chiamata in streaming a Gemini restituendo i delta testuali."""
    cfg = get_ai_routing_config()
    resolved_key = (api_key or cfg["api_key"]).strip()
    resolved_model = (model or cfg["gemini_model"] or DEFAULT_GEMINI_MODEL).strip()
    if not resolved_key:
        raise GeminiClientError("API Key Gemini mancante.")

    # 1. Tentativo tramite google-genai SDK se presente
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        client = genai.Client(api_key=resolved_key)
        contents: List[Any] = []
        for idx, msg in enumerate(messages):
            role = "model" if msg.get("role") == "assistant" else "user"
            msg_parts: List[Any] = [str(msg.get("content") or "")]
            if image_bytes and idx == len(messages) - 1 and role == "user":
                msg_parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))
            contents.append(types.Content(role=role, parts=msg_parts))

        config_kwargs: Dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        for chunk in client.models.generate_content_stream(
            model=resolved_model,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        ):
            delta = getattr(chunk, "text", None) or ""
            if delta:
                yield delta
        return
    except ImportError:
        pass
    except Exception as exc:
        raise GeminiClientError(f"Errore stream Gemini SDK: {exc}") from exc

    # 2. Streaming SSE via REST API v1beta
    url = f"{GEMINI_API_BASE}/models/{resolved_model}:streamGenerateContent?alt=sse&key={resolved_key}"
    payload: Dict[str, Any] = {
        "contents": _build_rest_contents(messages, image_bytes=image_bytes, image_mime=image_mime),
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                json_str = line[5:].strip()
                if not json_str or json_str == "[DONE]":
                    continue
                try:
                    obj = json.loads(json_str)
                    candidates = obj.get("candidates") or []
                    if not candidates:
                        continue
                    parts = (candidates[0].get("content") or {}).get("parts") or []
                    for p in parts:
                        delta = p.get("text") or ""
                        if delta:
                            yield delta
                except Exception:
                    continue
    except Exception as exc:
        raise GeminiClientError(f"Errore stream REST Gemini: {exc}") from exc
