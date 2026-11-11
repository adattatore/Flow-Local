# FlowLocal 🎙️⚡

Dettatura vocale AI ultra-rapida basata su **Whisper Large-v3-Turbo**, 100% in locale e privata, ottimizzata per la tua GPU **NVIDIA GeForce RTX 5060**.

---

### Caratteristiche Principali

- **Latenza sub-secondo (~300ms):** inferenza immediata su GPU NVIDIA con precisione FP16 e Silero VAD (Voice Activity Detection).
- **Push-to-Talk Ibrido:**
  - **Tieni premuto `Alt Destro`:** registra mentre parli, rilascia per scrivere all'istante dove si trova il cursore.
  - **Doppio click su `Alt Destro`:** attiva la modalità **Hands-Free (Lock)**. Parla quanto vuoi; premi di nuovo una volta per finalizzare e incollare.
- **Iniezione trasparente:** Incolla il testo tramite `Ctrl+V` simulato preservando automaticamente il contenuto precedente dei tuoi appunti.
- **Segnali acustici discreti:** Bip di conferma per inizio, blocco continuo e completamento.

---

### Come Avviare

Puoi avviarlo in due modi:
1. **Con doppio click:** apri il file `run.bat`
2. **Da terminale:**
   ```bash
   cd C:\Users\aless\.gemini\antigravity\scratch\flow-local
   uv run python main.py
   ```

---

### Personalizzazione Tasto

Nel file `main.py`, modifica la riga:
```python
HOTKEY = keyboard.Key.alt_r
```
Opzioni comuni:
- `keyboard.Key.f8` (Tasto F8)
- `keyboard.Key.f9` (Tasto F9)
- `keyboard.Key.ctrl_r` (Ctrl Destro)
- `keyboard.Key.caps_lock` (Blocco Maiuscole)
