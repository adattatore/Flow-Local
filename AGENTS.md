# FlowLocal — Linee Guida e Architettura per Agenti AI

Questo documento definisce l'architettura tecnica, i vincoli di sistema e le **regole inderogabili** per qualsiasi agente AI o sviluppatore che operi sulla codebase di **FlowLocal**.

---

## 1. Panoramica dell'Architettura

FlowLocal è un sistema desktop locale ad altissime prestazioni per la dettatura vocale, trascrizione acustica, raffinamento semantico contestuale e studio universitario/scientifico.

```
[ Microfono (WASAPI) ]
         │ (Ring Buffer 350ms pre-roll, 16kHz resample)
         ▼
[ Faster-Whisper Large-v3-Turbo (CUDA 12, float16) ]
         │ (VAD filter: min_silence=500ms, pad=200ms, threshold=0.6)
         ▼ Trascrizione grezza
[ Normalizzatore Fonetico (SQLite Vocabulary) ]
         │
         ▼ <trascrizione> ... </trascrizione>
[ Ollama Qwen 2.5 (3B / float16) ] ◄── [ Context Detector (Win32 Foreground) ]
         │ (Guardrail anti-allucinazione sulla lunghezza)
         ▼ Testo raffinato + LaTeX $...$
[ WindowsInjector (Clipboard + Ctrl+V) ] ──► Finestra Utente (Zero Focus Steal)
```

### Componenti Chiave:
- **Runtime & Pacchetti**: Python 3.12 gestito tramite `uv`.
- **Interfaccia Grafica (PyQt6)**:
  - **VoiceOrbOverlay (`ui/voice_orb.py` / `hud.py`)**: HUD fluttuante minimale (IDLE: 60×22 px, Attivo: Voice Orb espanso con onde Catmull-Rom/Bézier a 60 FPS + badge pillola informativa).
  - **FlowLocalHub (`ui/hub_window.py`)**: Finestra di controllo e configurazione a 6 schede (Panoramica, Vocabolario, Snippet, Stili LLM, Note & Flashcard, Audio) con telemetria GPU in tempo reale (NVML).
  - **System Tray (`main.py`)**: Tray icon nativa Qt per mostrare/nascondere la dashboard e sospendere la dettatura.
- **Pipeline Acustica (Faster-Whisper)**:
  - Modello: `large-v3-turbo` in precisione `float16` su GPU NVIDIA CUDA 12 (con predisposizione per fallback su CPU `int8`).
  - Pre-buffer audio circolare di 350ms per non perdere l'attacco della voce.
  - VAD (Voice Activity Detection) e pause-detection (>1.3s) per segmentazione in paragrafi.
- **Raffinamento Contestuale (Ollama Qwen 2.5)**:
  - Context-Aware Switching (`core/context_detector.py`): rileva il titolo della finestra e il processo attivo con `ctypes.windll.user32` per applicare lo stile appropriato (`code`, `formal`, `study_latex`, `chat`, `natural`).
  - Iniezione termini di vocabolario e jargon tecnico via prompt biasing.
- **Persistenza Dati (SQLite WAL Mode)**:
  - Database: `flow_hub.db` gestito da `database/db_manager.py` con thread-local connection pool e WAL mode abilitato.

---

## 2. Regole Inderogabili per lo Sviluppo (Mandatory Rules)

Tutti gli agenti AI che modificano questo repository **DEVONO** rispettare rigorosamente le seguenti regole:

### 1. HUD: Click-Through e Zero Furto di Focus
- `VoiceOrbOverlay` deve **sempre** mantenere attive le window flags:
  ```python
  Qt.WindowType.FramelessWindowHint |
  Qt.WindowType.WindowStaysOnTopHint |
  Qt.WindowType.Tool |
  Qt.WindowType.WindowDoesNotAcceptFocus |
  Qt.WindowType.WindowTransparentForInput
  ```
- L'overlay non deve **mai** rubare il focus alla finestra in cui l'utente sta scrivendo.
- Quando l'overlay si espande per mostrare il badge di contesto, deve ridimensionarsi e centrarsi dinamicamente (`anchor_x`) senza tagliare il testo e senza disattivare `WindowTransparentForInput`.

### 2. Concorrenza e Debounce Hardware
- Mantenere la sincronizzazione thread-safe tramite `threading.Lock()` e `key_lock` durante la gestione dell'audio e dei tasti.
- Il debounce hardware di **350ms** sul rilascio del tasto `Alt Gr` (o `F8`) è essenziale per evitare falsi eventi di keyup/keydown dovuti al typematic repeat di Windows. Non rimuoverlo.

### 3. Preservazione Assoluta della Lingua Italiana in Modalità `code`
- Nella modalità `code` (utilizzata su Antigravity, VS Code, Cursor, Terminali, PowerShell), **NON tradurre mai il discorso dell'utente in inglese**.
- L'inglese deve essere limitato **esclusivamente** ai termini informatici pronunciati dall'utente (es. *commit*, *push*, *pull request*, *bug*, *array*, *backend*, *refactor*). L'intera frase deve rimanere in italiano corretto.
- La formattazione in `snake_case` o `camelCase` va applicata solo se richiesta o dettata.

### 4. Barriere Anti-Allucinazione (Whisper & Qwen)
- In Faster-Whisper `transcribe`:
  - `vad_filter=True`
  - `vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200)`
  - `condition_on_previous_text=False`
  - `no_speech_threshold=0.6`
- In `LLMRefiner.refine`:
  - Il testo pronunciato deve essere isolato nei tag: `<trascrizione>{raw_text}</trascrizione>`.
  - Il System Prompt deve includere la direttiva:
    > "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. Restituisci SOLO il testo rifinito senza tag."
  - **Guardrail sulla lunghezza**: se `len(refined_text) > max(len(raw_text) * 1.45, len(raw_text) + 40)`, la risposta di Qwen deve essere scartata ed è obbligatorio il fallback sul testo acustico grezzo pulito dal vocabolario fonetico.

### 5. Formule Matematiche e Fisiche in LaTeX Standard
- In tutte le modalità di dettatura deve rimanere attiva la regola di conversione automatica delle espressioni matematiche o fisiche dettate a voce (integrali, derivate, vettori `\vec{}`, frazioni, unità del Sistema Internazionale) in codice LaTeX racchiuso tra simboli `$`:
  > Es. "integrale da 0 a infinito di e alla meno x in dx" -> `$\int_0^\infty e^{-x}\,dx$`
  > Es. "F uguale m per a" -> `$F = m \cdot a$`
  > Es. "vettore v" -> `$\vec{v}$`

### 6. Pulizia del Repository Git
- **NON tracciare mai su Git**:
  - Database SQLite e relativi file di write-ahead log: `*.db`, `*.db-shm`, `*.db-wal`, `flow_hub.db*`.
  - Ambiente virtuale: `.venv/`.
  - Script scratch o test temporanei: `test_*.py`, `scratch_*.py`.

