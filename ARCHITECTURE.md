# FlowLocal — Master Blueprint & Documento di Progetto

> **Concetto:** Assistente vocale AI 100% locale in stile Wispr Flow + Second Brain per lo studio e il lavoro.  
> **Stack Hardware:** NVIDIA GeForce RTX 5060 (8GB VRAM) + CPU Multicore per FFT e UI.  
> **Stack Software:** Python + PyQt6 Nativo (QSS) + `sounddevice` + `numpy` + Faster-Whisper (CUDA) + Qwen 2.5 via Ollama (CUDA) + SQLite (WAL).

---

## 1. Il "Voice Orb" (Il Cerchio Nero Flottante)

Invece della classica barra orizzontale, il widget sempre in primo piano è un **nucleo circolare nero compatto** che vive sul desktop senza rubare mai il focus alla finestra in cui stai scrivendo (`WindowDoesNotAcceptFocus`).

### 1.1 Logica Matematica e Parametri Scelti
* **Zero immagini statiche per le onde:** Tutto è calcolato matematicamente a 60/144 FPS sul thread grafico CPU (zero impatto sulla VRAM della RTX 5060).
* **Analisi Frequenze (FFT):** Il segnale del microfono viene trasformato con `numpy.fft.rfft()`, isolando la banda vocale ($85\text{ Hz} - 3400\text{ Hz}$) e dividendola in 12 bande specchiate sui due lati del cerchio (24 punti polari totali per avere una simmetria armonica).
* **Parametri fisici ideali (tarati su misura):**
  * **Intensità / Guadagno vocale:** `0.80`
  * **Fluidità / Smoothing esponenziale ($\alpha$):** `0.50` — secondo la legge:
    $$\text{livello\_attuale} = \alpha \cdot \text{target} + (1 - \alpha) \cdot \text{livello\_precedente}$$
* **Geometria dell'onda (Catmull-Rom $\rightarrow$ Bézier):**
  * **Niente doppio anello bianco esterno:** Solo il cerchio nero centrale (`raggio = 26px`, diametro `52px` dentro una finestra trasparente `140x140px`) circondato da **una singola onda polare morbida** e da un leggero bagliore radiale (`QRadialGradient`) sottostante.
  * I 24 punti polari vengono uniti tramite una curva spline chiusa continua ($C^1$) calcolata al volo con `QPainterPath.cubicTo`.

### 1.2 Macchina a Stati Visiva del Cerchio Nero
| Stato | Colore Accento | Comportamento Visivo del Cerchio |
| :--- | :--- | :--- |
| **`IDLE`** *(Inattivo)* | Grigio Ardesia (`#64748B`) | Cerchio nero discreto, bordo sottile che "respira" lentamente. Dopo 2 minuti di inattività scende al 25% di opacità (*Ghost Mode*). |
| **`LISTENING`** *(Push-to-Talk)* | Ciano Elettrico (`#38BDF8`) | Il bordo si illumina, compare il bagliore e l'onda polare danza attorno al cerchio nero seguendo le frequenze della voce. |
| **`COMMAND_MODE`** *(Testo selezionato)* | Ambra / Oro (`#F59E0B`) | Si attiva se premi il Push-to-Talk mentre hai del testo evidenziato sullo schermo (modifica o riassume il testo selezionato). |
| **`STUDY_MODE`** *(Modalità Studio)* | Viola Ametista (`#A855F7`) | Onda viola per ripetizione orale, generazione appunti strutturati, formule $\LaTeX$ e Flashcard. |
| **`WHISPER`** *(Stadio 1 CUDA)* | Indaco (`#818CF8`) | Le onde esterne vengono "risucchiate" nel cerchio nero; l'icona del microfono sfuma e compare uno **spinner circolare interno** indaco. |
| **`QWEN`** *(Stadio 2 CUDA)* | Verde Smeraldo (`#10B981`) | Lo spinner circolare interno diventa verde mentre Qwen 2.5 riformatta il testo. Lampo finale rapido quando incolla. |

### 1.3 Micro-interazioni sul Cerchio
* **Click Destro:** Menu rapido per cambiare microfono al volo e cambiare modalità.
* **Trascinamento (Left Click + Drag):** Sposta il cerchio ovunque sullo schermo.
* **Hands-Free Lock (Doppio Tap su `Alt Gr`):** Blocca il microfono in registrazione continua (utile per studio o dettati lunghi), mostrando un piccolo quadrato di Stop e un **timer (`02:14`)** dentro il cerchio nero.

---

## 2. Pipeline Intelligente a Doppio Stadio (Whisper + Qwen 2.5)

1. **Stadio 1 — Faster-Whisper con `initial_prompt` Dinamico:**
   Passiamo a `model.transcribe(..., initial_prompt=...)` la lista delle parole tecniche salvate nel tuo vocabolario SQLite, così i pesi neurali di Whisper riconoscono subito acronimi e nomi propri.
2. **Stadio 1.5 — Analisi delle Pause (VAD) e Pre-filtro Fonetico:**
   * Sostituzione deterministica immediata degli alias fonetici noti e degli **Snippet Vocali** (macro di testo).
   * Calcolo della distanza temporale tra i segmenti di Whisper: se c'è una pausa $> 1.3\text{ secondi}$ (`seg.start - last_end > 1.3`), il codice Python inserisce il tag `[PAUSA_CAMBIO_ARGOMENTO]`.
3. **Stadio 2 — Riformattazione Strutturale con Qwen 2.5:**
   Qwen 2.5 riceve il testo grezzo + i tag di pausa e applica 3 regole d'oro:
   * **Divisione in Sezioni:** Quando trova `[PAUSA_CAMBIO_ARGOMENTO]` o sente un cambio di tema, crea un nuovo paragrafo.
   * **Rilevamento Liste:** Se sente elenchi sequenziali (*"primo... secondo..."*, *"uno... due..."*), li trasforma in elenchi puntati o numerati.
   * **Pulizia Autocorrezioni:** Se ti correggi mentre parli (*"anzi no, cancella l'ultimo punto"*), elimina l'errore e tiene solo la frase corretta.
   * **Format Tag:** Restituisce il contenuto finale dentro `<output>...</output>`.

---

## 3. Funzionalità "Assistente & Studio"

1. **Study Mode — Metodo Feynman:** Schede Markdown con definizioni e punti chiave.
2. **Formule Matematiche $\LaTeX$:** Converte espressioni parlate in $\LaTeX$ ($...$).
3. **Generatore Vocale di Flashcard:** Trigger *"Flashcard: [Domanda] ... [Risposta]"* salvate nella tabella `study_notes`.
4. **Command Mode su Testo Selezionato:** Riscrive o riassume brani evidenziati prima del Push-to-Talk.

---

## 4. FlowLocal Hub (Design System Obsidian Dark)
Finestra desktop 1080x700:
- TopBar: Brand + Telemetria live RTX 5060 (NVML: Temperatura °C e VRAM usata/totale).
- Sidebar a 6 sezioni:
  1. `⚡ Panoramica`
  2. `📖 Vocabolario & Jargon`
  3. `🪄 Snippet Vocali`
  4. `🧠 Stili LLM & Studio`
  5. `📚 Studio, Note & Flashcard`
  6. `⚙️ Impostazioni Audio & Hotkeys`
