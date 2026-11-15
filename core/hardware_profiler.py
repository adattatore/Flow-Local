"""
core/hardware_profiler.py
Modulo leggero e non bloccante (< 300ms all'avvio) per la profilazione hardware automatica
e la classificazione nei 3 Tier adattivi di FlowLocal: PRO, STANDARD ed ECO.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class HardwareTier(str, Enum):
    PRO = "pro"
    STANDARD = "standard"
    ECO = "eco"


@dataclass(frozen=True)
class HardwareInfo:
    has_cuda: bool
    cuda_device_count: int
    gpu_name: str
    vram_gb: float
    ram_gb: float
    cpu_cores: int
    cpu_arch: str
    has_avx2: bool
    recommended_tier: HardwareTier
    detection_ms: float
    cpu_model: str = ""


@dataclass(frozen=True)
class HardwareProfile:
    tier: HardwareTier
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_cpu_threads: int
    llm_default_model: str
    llm_high_precision_model: str
    suggest_gemini_byok: bool
    badge_text: str
    detail_text: str


_CACHED_HARDWARE_INFO: Optional[HardwareInfo] = None


def _read_linux_cpuinfo() -> str:
    """Legge /proc/cpuinfo su Linux in modo sicuro (< 1ms)."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            return f.read(65536)
    except Exception:
        return ""


def _detect_cpu_model() -> str:
    """Rileva il modello della CPU (es. 'AMD Ryzen 7 ...' su Linux via /proc/cpuinfo)."""
    try:
        if sys.platform.startswith("linux"):
            cpuinfo = _read_linux_cpuinfo()
            m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, flags=re.MULTILINE)
            if m:
                return m.group(1).strip()
        proc = platform.processor() or ""
        return proc.strip()
    except Exception:
        return ""


def _check_avx2_support() -> bool:
    """Verifica rapida (<1ms) del supporto alle istruzioni AVX2 su CPU x86_64."""
    try:
        if platform.system() == "Windows":
            # PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        if sys.platform.startswith("linux"):
            cpuinfo = _read_linux_cpuinfo()
            m = re.search(r"^flags\s*:\s*(.+)$", cpuinfo, flags=re.MULTILINE)
            if m:
                return "avx2" in m.group(1).split()
    except Exception:
        pass
    return False


def _query_nvidia_gpu() -> tuple[str, float]:
    """Rileva nome GPU e VRAM totale (GB) tramite pynvml o nvidia-smi fallback."""
    # 1. Tentativo primario con pynvml (veloce, in-process)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        gpu_name = (
            raw_name.decode("utf-8", errors="ignore")
            if isinstance(raw_name, bytes)
            else str(raw_name)
        ).strip()
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_gb = round(mem.total / (1024 ** 3), 1)
        return gpu_name, vram_gb
    except Exception:
        pass

    # 2. Fallback leggero via nvidia-smi (solo se pynvml non disponibile)
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            creationflags=creationflags,
        )
        if res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 2:
                gpu_name = parts[0]
                vram_mb = float(parts[1])
                return gpu_name, round(vram_mb / 1024.0, 1)
    except Exception:
        pass

    return "", 0.0


def detect_hardware(force_refresh: bool = False) -> HardwareInfo:
    """
    Ispeziona GPU CUDA, VRAM, RAM totale e CPU in < 300ms.
    Il risultato viene memorizzato in cache per chiamate successive a costo zero.
    """
    global _CACHED_HARDWARE_INFO
    if _CACHED_HARDWARE_INFO is not None and not force_refresh:
        return _CACHED_HARDWARE_INFO

    t0 = time.perf_counter()

    # 1. CUDA device count tramite ctranslate2
    cuda_count = 0
    try:
        import ctranslate2

        cuda_count = int(ctranslate2.get_cuda_device_count() or 0)
    except Exception:
        cuda_count = 0

    # 2. RAM totale e CPU tramite psutil / os
    ram_gb = 8.0
    try:
        import psutil

        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        ram_gb = 8.0

    cpu_cores = os.cpu_count() or 4
    cpu_arch = platform.machine() or "x86_64"
    cpu_model = _detect_cpu_model()
    has_avx2 = _check_avx2_support()

    # 3. Dettagli GPU NVIDIA (solo se CUDA rilevato o per conferma)
    gpu_name = ""
    vram_gb = 0.0
    if cuda_count > 0:
        gpu_name, vram_gb = _query_nvidia_gpu()
        if not gpu_name:
            gpu_name = "GPU NVIDIA CUDA"

    has_cuda = cuda_count > 0

    # 4. Classificazione automatica nei 3 Tier
    if has_cuda and vram_gb >= 6.0:
        rec_tier = HardwareTier.PRO
    elif (has_cuda and vram_gb >= 3.5) or (not has_cuda and ram_gb >= 16.0):
        rec_tier = HardwareTier.STANDARD
    else:
        rec_tier = HardwareTier.ECO

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    info = HardwareInfo(
        has_cuda=has_cuda,
        cuda_device_count=cuda_count,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        cpu_arch=cpu_arch,
        has_avx2=has_avx2,
        recommended_tier=rec_tier,
        detection_ms=elapsed_ms,
        cpu_model=cpu_model,
    )
    _CACHED_HARDWARE_INFO = info
    return info


def build_profile(tier: HardwareTier, hw: HardwareInfo) -> HardwareProfile:
    """Costruisce la configurazione Whisper + LLM/Vision ottimale per il Tier indicato."""
    cpu_threads = max(2, min(8, (hw.cpu_cores or 4) // 2))
    tier_upper = tier.value.upper()

    if tier == HardwareTier.PRO:
        if hw.has_cuda:
            w_model, w_dev, w_comp = "large-v3-turbo", "cuda", "float16"
            badge = (
                f"{hw.gpu_name} ({hw.vram_gb:.1f} GB VRAM) • Profilo {tier_upper}"
                if hw.vram_gb > 0
                else f"{hw.gpu_name} • Profilo {tier_upper}"
            )
            detail = (
                f"Whisper {w_model} (CUDA float16) • LLM locale qwen3.5:4b / 9b • "
                f"RAM {hw.ram_gb:.0f} GB ({hw.cpu_cores} core)."
            )
        else:
            # Override manuale su Pro senza GPU: usa small int8 su CPU per evitare OOM
            w_model, w_dev, w_comp = "small", "cpu", "int8"
            badge = f"Modalità CPU ({hw.cpu_cores} core, {hw.ram_gb:.0f} GB RAM) • Profilo {tier_upper}"
            detail = (
                "Nessuna GPU CUDA rilevata: Whisper small (CPU int8). "
                "Consigliato Gemini Cloud BYOK per prestazioni ottimali."
            )
        return HardwareProfile(
            tier=tier,
            whisper_model=w_model,
            whisper_device=w_dev,
            whisper_compute_type=w_comp,
            whisper_cpu_threads=cpu_threads,
            llm_default_model="qwen3.5:4b",
            llm_high_precision_model="qwen3.5:9b",
            suggest_gemini_byok=not hw.has_cuda,
            badge_text=badge,
            detail_text=detail,
        )

    if tier == HardwareTier.STANDARD:
        if hw.has_cuda:
            w_model, w_dev, w_comp = "small", "cuda", "int8_float16"
            llm_def, llm_deep = "qwen3.5:4b", "qwen3.5:4b"
            badge = (
                f"{hw.gpu_name} ({hw.vram_gb:.1f} GB VRAM) • Profilo {tier_upper}"
                if hw.vram_gb > 0
                else f"{hw.gpu_name} • Profilo {tier_upper}"
            )
            detail = (
                f"Whisper {w_model} (CUDA int8_float16) • LLM locale {llm_def} • "
                f"RAM {hw.ram_gb:.0f} GB."
            )
            suggest_byok = False
        else:
            w_model, w_dev, w_comp = "small", "cpu", "int8"
            llm_def, llm_deep = "qwen3.5:2b", "qwen3.5:4b"
            badge = f"CPU {hw.cpu_cores} core ({hw.ram_gb:.0f} GB RAM) • Profilo {tier_upper}"
            detail = (
                f"Whisper {w_model} (CPU int8) • LLM locale {llm_def}. "
                "Suggerimento: attiva Gemini Cloud BYOK per Vision OCR e riassunti istantanei."
            )
            suggest_byok = True

        return HardwareProfile(
            tier=tier,
            whisper_model=w_model,
            whisper_device=w_dev,
            whisper_compute_type=w_comp,
            whisper_cpu_threads=cpu_threads,
            llm_default_model=llm_def,
            llm_high_precision_model=llm_deep,
            suggest_gemini_byok=suggest_byok,
            badge_text=badge,
            detail_text=detail,
        )

    # HardwareTier.ECO
    w_model, w_dev, w_comp = "base", "cpu", "int8"
    if hw.has_cuda:
        badge = f"{hw.gpu_name} ({hw.vram_gb:.1f} GB VRAM) • Profilo ECO"
    elif hw.cpu_model:
        badge = f"{hw.cpu_model} ({hw.cpu_cores} core, {hw.ram_gb:.0f} GB RAM) • Profilo ECO"
    else:
        badge = f"CPU {hw.cpu_cores} core ({hw.ram_gb:.0f} GB RAM) • Profilo ECO"
    detail = (
        "Whisper base (CPU int8, < 600 MB RAM) • LLM leggero qwen3.5:2b. "
        "Ideale in modalità Cloud Gemini BYOK o Ibrido su portatili senza GPU."
    )
    return HardwareProfile(
        tier=tier,
        whisper_model=w_model,
        whisper_device=w_dev,
        whisper_compute_type=w_comp,
        whisper_cpu_threads=cpu_threads,
        llm_default_model="qwen3.5:2b",
        llm_high_precision_model="qwen3.5:2b",
        suggest_gemini_byok=not hw.has_cuda,
        badge_text=badge,
        detail_text=detail,
    )


def ensure_first_run_hardware_tier(db_manager=None) -> str:
    """
    Al primo avvio, se app_settings non contiene 'hardware_tier',
    rileva l'hardware e salva automaticamente il profilo consigliato in SQLite.
    """
    if db_manager is None:
        from database import db as db_manager

    saved = db_manager.get_setting("hardware_tier", None)
    if saved is None or not str(saved).strip():
        hw = detect_hardware()
        rec = hw.recommended_tier.value
        db_manager.set_setting("hardware_tier", rec)
        return rec
    return str(saved).strip().lower()


def get_active_profile(db_manager=None) -> HardwareProfile:
    """Restituisce il HardwareProfile attivo in base al setting salvato (o Automatico)."""
    if db_manager is None:
        from database import db as db_manager

    hw = detect_hardware()
    saved = ensure_first_run_hardware_tier(db_manager)

    if saved == "auto":
        tier = hw.recommended_tier
    elif saved in (HardwareTier.PRO.value, HardwareTier.STANDARD.value, HardwareTier.ECO.value):
        tier = HardwareTier(saved)
    else:
        tier = hw.recommended_tier

    return build_profile(tier, hw)
