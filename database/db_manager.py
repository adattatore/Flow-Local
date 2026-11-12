"""
Database Manager SQLite (WAL Mode) per FlowLocal Hub
Gestisce Vocabolario, Snippet, Stili LLM & Studio, Note/Flashcard e Cronologia Dettati.
"""

import os
import re
import sqlite3
import threading
from typing import List, Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "flow_hub.db")


class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # 1. Tabella Vocabolario e Jargon
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vocabulary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correct_term TEXT NOT NULL,
                phonetic_aliases TEXT DEFAULT '',
                category TEXT DEFAULT 'Generale',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Tabella Snippet Vocali & Macro
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snippets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_phrase TEXT NOT NULL UNIQUE,
                expansion_text TEXT NOT NULL,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. Tabella Stili LLM (Qwen 2.5) & Studio
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_styles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                system_prompt TEXT NOT NULL,
                auto_target_apps TEXT DEFAULT '',
                is_active INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 4. Tabella Appunti di Studio, Scratchpad & Flashcard
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS study_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content_markdown TEXT NOT NULL,
                note_type TEXT DEFAULT 'Note', -- 'Note', 'Flashcard', 'Meeting', 'LaTeX'
                tags TEXT DEFAULT '',
                parent_note_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migrazione schema: parent_note_id per collegare le flashcard alla sbobina madre
        try:
            cursor.execute("ALTER TABLE study_notes ADD COLUMN parent_note_id INTEGER DEFAULT NULL;")
        except sqlite3.OperationalError:
            pass

        # Collegamento retroattivo flashcard esistenti alla sbobina sorgente tramite tags
        try:
            cursor.execute("""
                UPDATE study_notes
                SET parent_note_id = (
                    SELECT p.id FROM study_notes p
                    WHERE p.note_type != 'Flashcard'
                      AND (study_notes.tags LIKE '%' || p.title || '%' OR p.tags LIKE '%' || REPLACE(study_notes.tags, 'Esame, ', '') || '%')
                    ORDER BY p.id DESC LIMIT 1
                )
                WHERE note_type = 'Flashcard' AND parent_note_id IS NULL;
            """)
        except Exception:
            pass

        # 5. Tabella Cronologia Trascrizioni & Telemetria
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,
                final_text TEXT NOT NULL,
                target_app TEXT DEFAULT '',
                style_used TEXT DEFAULT 'Naturale',
                word_count INTEGER DEFAULT 0,
                whisper_ms INTEGER DEFAULT 0,
                qwen_ms INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 6. Tabella Relazioni e Grafo della Conoscenza (Note Links)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id, target_id),
                FOREIGN KEY (source_id) REFERENCES study_notes(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES study_notes(id) ON DELETE CASCADE
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_note_links_source ON note_links(source_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target_id);")

        # 7. Tabella Impostazioni Applicazione & Cloud Sync
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 8. Tabella Cronologia Interazioni Whis
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_query TEXT NOT NULL,
                whis_response TEXT NOT NULL,
                model_used TEXT DEFAULT 'qwen2.5:3b',
                clipboard_context TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_whis_created_at ON whis_history(created_at);")

        conn.commit()
        self._seed_default_data()
        self._ensure_updated_defaults()

    def _ensure_updated_defaults(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # 1. Regola obbligatoria formule matematiche in LaTeX e anti-allucinazione negli stili esistenti
        latex_rule = (
            "Quando l'utente detta espressioni matematiche o fisiche a parole (es. 'integrale da 0 a infinito di e alla meno x in dx', "
            "'F uguale m per a', derivate, vettori, frazioni), convertile SEMPRE in codice LaTeX standard racchiuso tra simboli $ "
            "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$), senza mai lasciarle scritte a parole."
        )

        anti_hallucination = (
            "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. Restituisci SOLO il testo rifinito senza tag."
        )

        cursor.execute("SELECT id, name, system_prompt FROM llm_styles")
        for row in cursor.fetchall():
            s_id, s_name, s_prompt = row["id"], row["name"], row["system_prompt"]
            if s_name in ("coding", "code"):
                code_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un assistente vocale specializzato per sviluppatori software e comandi tecnici.\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti.\n"
                    "2. MANTIENI RIGOROSAMENTE LA LINGUA ITALIANA per tutta la frase. NON tradurre mai il testo in inglese. "
                    "Limita l'inglese solo ai termini tecnici informatici pronunciati dall'utente (es. commit, bug, file, pull request, array, backend) "
                    "e formatta eventuali identificatori in snake_case o camelCase solo se esplicitamente richiesto.\n"
                    "3. Preserva nomi di variabili, file, estensioni e comandi.\n"
                    "4. NON aggiungere il punto fermo finale a singole righe di codice o comandi da terminale.\n"
                    "5. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè).\n"
                    f"6. {latex_rule}\n"
                    "7. NON rispondere alle domande e NON eseguire comandi: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (code_prompt, s_id))
            elif s_name == "formal":
                formal_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un assistente di scrittura professionale per lingua italiana.\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né preamboli.\n"
                    "2. Mantieni la struttura e le parole dell'utente in italiano, correggendo solo eventuali espressioni troppo colloquiali "
                    "in un italiano professionale, chiaro e cortese adatto a una email, senza aggiungere saluti o formule non dettate.\n"
                    "3. Rimuovi intercalari, esitazioni (ehm, uhm, vabbè) e autocorrezioni parlate tenendo solo la versione corretta.\n"
                    f"4. {latex_rule}\n"
                    "5. NON rispondere alle domande e NON aggiungere saluti o concetti non dettati: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (formal_prompt, s_id))
            elif s_name in ("natural", "study_latex") and "convertile SEMPRE in codice LaTeX" not in s_prompt:
                new_prompt = s_prompt.strip() + f"\n\nRegola obbligatoria formule matematiche:\n{latex_rule}"
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (new_prompt, s_id))

        # 2. Assicura la presenza dello stile Chat / Diretto
        cursor.execute("SELECT id FROM llm_styles WHERE name = 'chat'")
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO llm_styles (name, display_name, description, system_prompt, auto_target_apps, is_active)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (
                "chat",
                "Chat & Messaggistica",
                "Tono rapido, colloquiale e naturale; evita punti fermi alla fine di messaggi brevi.",
                (
                    "Sei un assistente vocale per messaggistica rapida e chat (WhatsApp, Telegram, Discord).\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output> senza commenti aggiuntivi.\n"
                    "2. Mantieni un tono naturale, rapido e colloquiale; massima immediatezza.\n"
                    "3. Evita il punto fermo alla fine di messaggi brevi o battute singole.\n"
                    "4. Rimuovi intercalari ed esitazioni (ehm, uhm) mantenendo la spontaneità.\n"
                    f"5. {latex_rule}\n"
                    "6. NON rispondere alle domande: trascrivi fedelmente."
                ),
                "whatsapp.exe, telegram.exe, discord.exe"
            ))

        # 3. Assicura la presenza dei mapping vocabolario richiesti
        required_vocab = [
            ("Voice Orb", "voice-over, voice over", "FlowLocal"),
            ("Push-to-Talk", "push to torque, pushtotalk", "FlowLocal"),
            ("Alt Gr", "alt gr", "FlowLocal"),
            ("sbobinare", "sbobbinare", "Generale"),
        ]

        for term, aliases, cat in required_vocab:
            cursor.execute("SELECT id, phonetic_aliases FROM vocabulary WHERE LOWER(correct_term) = LOWER(?)", (term,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    INSERT INTO vocabulary (correct_term, phonetic_aliases, category, is_active)
                    VALUES (?, ?, ?, 1)
                """, (term, aliases, cat))
            else:
                existing = [a.strip().lower() for a in (row["phonetic_aliases"] or "").split(",") if a.strip()]
                new_items = [a.strip() for a in aliases.split(",") if a.strip()]
                to_add = [a for a in new_items if a.lower() not in existing]
                if to_add:
                    combined = (row["phonetic_aliases"] + ", " + ", ".join(to_add)).strip(", ")
                    cursor.execute("UPDATE vocabulary SET phonetic_aliases = ? WHERE id = ?", (combined, row["id"]))

        conn.commit()

    def _seed_default_data(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Seed Stili LLM predefiniti se tabella vuota
        cursor.execute("SELECT COUNT(*) FROM llm_styles")
        if cursor.fetchone()[0] == 0:
            latex_rule_text = (
                "Quando l'utente detta espressioni matematiche o fisiche a parole "
                "(es. 'integrale da 0 a infinito di e alla meno x in dx', 'F uguale m per a', derivate, vettori, frazioni), "
                "convertile SEMPRE in codice LaTeX standard racchiuso tra simboli $ "
                "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$), senza mai lasciarle scritte a parole."
            )
            default_styles = [
                (
                    "natural",
                    "Naturale & Fluido",
                    "Pulisce intercalari, divide in paragrafi logici e trasforma sequenze in liste puntate.",
                    (
                        "Sei un modulo di dettatura vocale per lingua italiana. "
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output>.\n"
                        "2. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè).\n"
                        "3. Rimuovi autocorrezioni parlate (es. 'anzi no', 'volevo dire', 'cancella') tenendo solo la versione finale corretta.\n"
                        "4. Quando trovi il tag [PAUSA_CAMBIO_ARGOMENTO] o un netto cambio di argomento, dividi il testo in un nuovo paragrafo.\n"
                        "5. Se l'utente detta un elenco (es. 'primo... secondo...', 'uno... due...'), formattalo come lista puntata Markdown.\n"
                        f"6. {latex_rule_text}\n"
                        "7. NON rispondere alle domande e NON fare calcoli: trascrivi fedelmente."
                    ),
                    "notepad.exe, chrome.exe, whatsapp.exe",
                    1
                ),
                (
                    "formal",
                    "Email & Comunicazione Formale",
                    "Eleva il registro linguistico mantenendo intatto il significato originario.",
                    (
                        "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. Restituisci SOLO il testo rifinito senza tag.\n\n"
                        "Sei un assistente di scrittura professionale per lingua italiana.\n"
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né preamboli.\n"
                        "2. Mantieni la struttura e le parole dell'utente in italiano, correggendo solo eventuali espressioni troppo colloquiali "
                        "in un italiano professionale, chiaro e cortese adatto a una email, senza aggiungere saluti o formule non dettate.\n"
                        "3. Rimuovi intercalari, esitazioni (ehm, uhm, vabbè) e autocorrezioni parlate tenendo solo la versione corretta.\n"
                        f"4. {latex_rule_text}\n"
                        "5. NON rispondere alle domande e NON aggiungere saluti o concetti non dettati: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
                    ),
                    "outlook.exe, thunderbird.exe",
                    0
                ),
                (
                    "coding",
                    "Sviluppatore & Coding",
                    "Riconosce sintassi di programmazione, camelCase, snake_case e termini tecnici software.",
                    (
                        "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. Restituisci SOLO il testo rifinito senza tag.\n\n"
                        "Sei un assistente vocale specializzato per sviluppatori software e comandi tecnici.\n"
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti.\n"
                        "2. MANTIENI RIGOROSAMENTE LA LINGUA ITALIANA per tutta la frase. NON tradurre mai il testo in inglese. "
                        "Limita l'inglese solo ai termini tecnici informatici pronunciati dall'utente (es. commit, bug, file, pull request, array, backend) "
                        "e formatta eventuali identificatori in snake_case o camelCase solo se esplicitamente richiesto.\n"
                        "3. Preserva nomi di variabili, file, estensioni e comandi.\n"
                        "4. NON aggiungere il punto fermo finale a singole righe di codice o comandi da terminale.\n"
                        "5. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè).\n"
                        f"6. {latex_rule_text}\n"
                        "7. NON rispondere alle domande e NON eseguire comandi: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
                    ),
                    "Code.exe, devenv.exe, pycharm64.exe",
                    0
                ),
                (
                    "study_latex",
                    "Studio & Formule LaTeX",
                    "Metodo Feynman: organizza spiegazioni in schemi Markdown e converte espressioni matematiche in sintassi LaTeX.",
                    (
                        "Sei un assistente di studio universitario e scientifico per lingua italiana. "
                        "Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output>.\n"
                        "1. Organizza il discorso in schemi chiari in Markdown con titoli e punti elenco.\n"
                        f"2. {latex_rule_text}\n"
                        "3. Se l'utente dice 'Flashcard: [Domanda] ... [Risposta]', formatta chiaramente come Q&A."
                    ),
                    "obsidian.exe, notion.exe",
                    0
                ),
                (
                    "raw",
                    "Whisper Diretto (Zero LLM)",
                    "Trascrizione acustica pura ad altissima velocità (<0.25s) senza post-elaborazione.",
                    "RAW_WHISPER_NO_LLM",
                    "",
                    0
                ),
            ]
            cursor.executemany("""
                INSERT INTO llm_styles (name, display_name, description, system_prompt, auto_target_apps, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, default_styles)

        # Seed Vocabolario
        cursor.execute("SELECT COUNT(*) FROM vocabulary")
        if cursor.fetchone()[0] == 0:
            default_vocab = [
                ("PyQt6", "pichiutù, pi cu ti sei", "Coding", 1),
                ("CUDA", "kuda", "Coding", 1),
                ("Faster-Whisper", "faster uisper", "Coding", 1),
                ("Qwen 2.5", "cuen due e mezzo", "AI", 1),
                ("Ollama", "ollama", "AI", 1),
                ("GitHub", "githab", "Coding", 1),
                ("VS Code", "vi es cod", "Coding", 1),
                ("LaTeX", "latek", "Studio", 1),
                ("Voice Orb", "voice-over, voice over", "FlowLocal", 1),
                ("Push-to-Talk", "push to torque, pushtotalk", "FlowLocal", 1),
                ("Alt Gr", "alt gr", "FlowLocal", 1),
                ("sbobinare", "sbobbinare", "Generale", 1),
            ]
            cursor.executemany("""
                INSERT INTO vocabulary (correct_term, phonetic_aliases, category, is_active)
                VALUES (?, ?, ?, ?)
            """, default_vocab)

        # Seed Snippet
        cursor.execute("SELECT COUNT(*) FROM snippets")
        if cursor.fetchone()[0] == 0:
            default_snippets = [
                (
                    "firma",
                    "Cordiali saluti,\nAlessandro\nLead Developer & AI Architect",
                    0
                ),
                (
                    "coordinate bancarie",
                    "IBAN: IT00X0000000000000000000000\nIntestatario: Alessandro",
                    0
                ),
                (
                    "bug report",
                    "### Descrizione Bug\n**Comportamento atteso:**\n**Comportamento riscontrato:**\n**Passaggi per riprodurre:**\n1. ",
                    0
                ),
            ]
            cursor.executemany("""
                INSERT INTO snippets (trigger_phrase, expansion_text, use_count)
                VALUES (?, ?, ?)
            """, default_snippets)

        conn.commit()

    # --- VOCABOLARIO ---
    def get_all_vocabulary(self) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, correct_term, phonetic_aliases, category, is_active, created_at FROM vocabulary ORDER BY category, correct_term")
        return [dict(row) for row in cursor.fetchall()]

    def add_vocabulary(self, term: str, aliases: str = "", category: str = "Generale") -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO vocabulary (correct_term, phonetic_aliases, category, is_active)
            VALUES (?, ?, ?, 1)
        """, (term.strip(), aliases.strip(), category.strip()))
        conn.commit()
        return cursor.lastrowid

    def delete_vocabulary(self, item_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM vocabulary WHERE id = ?", (item_id,))
        conn.commit()

    def get_vocabulary_biasing_prompt(self) -> str:
        items = self.get_all_vocabulary()
        active_terms = [item["correct_term"] for item in items if item.get("is_active", 1)]
        return ", ".join(active_terms)

    def get_whisper_initial_prompt(self, limit: int = 40) -> str:
        """Estrae i termini distinti dal vocabolario per il bias acustico in Faster-Whisper."""
        items = self.get_all_vocabulary()
        seen = set()
        terms = []
        for item in items:
            if item.get("is_active", 1):
                term = (item.get("correct_term") or "").strip()
                if term and term.lower() not in seen:
                    seen.add(term.lower())
                    terms.append(term)
                    if len(terms) >= limit:
                        break
        if not terms:
            return ""
        return f"Glossario tecnico: {', '.join(terms)}."

    def learn_vocabulary_pair(self, alias: str, correct_term: str) -> bool:
        """Salva o aggiorna la coppia (alias_errato, parola_corretta) nella tabella vocabulary."""
        alias_clean = (alias or "").strip().lower()
        term_clean = (correct_term or "").strip()
        if not alias_clean or not term_clean or alias_clean == term_clean.lower():
            return False

        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT id, phonetic_aliases FROM vocabulary WHERE LOWER(correct_term) = LOWER(?)", (term_clean,))
        row = cursor.fetchone()
        if row:
            existing = [a.strip().lower() for a in (row["phonetic_aliases"] or "").split(",") if a.strip()]
            if alias_clean not in existing:
                existing.append(alias_clean)
                new_aliases = ", ".join(existing)
                cursor.execute("UPDATE vocabulary SET phonetic_aliases = ? WHERE id = ?", (new_aliases, row["id"]))
                conn.commit()
                return True
            return False
        else:
            cursor.execute("""
                INSERT INTO vocabulary (correct_term, phonetic_aliases, category, is_active)
                VALUES (?, ?, 'Auto-Appreso', 1)
            """, (term_clean, alias_clean))
            conn.commit()
            return True

    def apply_phonetic_replacements(self, text: str) -> str:
        """Sostituisce deterministiche alias fonetici prima di inviare a Whisper/LLM."""
        items = self.get_all_vocabulary()
        mappings = []
        for item in items:
            term = item["correct_term"]
            aliases = [a.strip() for a in item.get("phonetic_aliases", "").split(",") if a.strip()]
            for alias in aliases:
                mappings.append((alias, term))

        # Mapping predefiniti prioritari / fallback di sicurezza
        default_mappings = [
            ("voice-over", "Voice Orb"),
            ("voice over", "Voice Orb"),
            ("push to torque", "Push-to-Talk"),
            ("pushtotalk", "Push-to-Talk"),
            ("alt gr", "Alt Gr"),
            ("sbobbina", "sbobina"),
            ("sbobbine", "sbobine"),
            ("sbobbinare", "sbobinare"),
        ]
        for alias, term in default_mappings:
            if not any(m[0].lower() == alias.lower() for m in mappings):
                mappings.append((alias, term))

        # Ordina per lunghezza decrescente dell'alias per evitare sostituzioni parziali
        mappings.sort(key=lambda x: len(x[0]), reverse=True)

        result = text
        for alias, term in mappings:
            def _sub_case(m):
                matched = m.group(0)
                if matched.isupper():
                    return term.upper()
                elif matched[0].isupper():
                    return term[0].upper() + term[1:]
                return term

            pattern = re.compile(rf'\b{re.escape(alias)}\b', re.IGNORECASE)
            result = pattern.sub(_sub_case, result)
        return result

    # --- SNIPPET VOCALI ---
    def get_all_snippets(self) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, trigger_phrase, expansion_text, use_count, created_at FROM snippets ORDER BY trigger_phrase")
        return [dict(row) for row in cursor.fetchall()]

    def add_snippet(self, trigger_phrase: str, expansion_text: str) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO snippets (trigger_phrase, expansion_text, use_count)
            VALUES (?, ?, 0)
        """, (trigger_phrase.strip().lower(), expansion_text))
        conn.commit()
        return cursor.lastrowid

    def delete_snippet(self, item_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM snippets WHERE id = ?", (item_id,))
        conn.commit()

    def match_and_expand_snippet(self, spoken_text: str) -> Optional[str]:
        clean_text = spoken_text.strip().lower().rstrip(".,!?")
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, trigger_phrase, expansion_text, use_count FROM snippets")
        for row in cursor.fetchall():
            if clean_text == row["trigger_phrase"].lower():
                cursor.execute("UPDATE snippets SET use_count = use_count + 1 WHERE id = ?", (row["id"],))
                conn.commit()
                return row["expansion_text"]
        return None

    # --- STILI LLM ---
    def get_all_styles(self) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, display_name, description, system_prompt, auto_target_apps, is_active FROM llm_styles ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]

    def set_active_style(self, style_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE llm_styles SET is_active = 0")
        cursor.execute("UPDATE llm_styles SET is_active = 1 WHERE id = ?", (style_id,))
        conn.commit()

    def update_style_prompt(self, style_id: int, new_prompt: str, auto_apps: str = ""):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE llm_styles SET system_prompt = ?, auto_target_apps = ? WHERE id = ?", (new_prompt, auto_apps, style_id))
        conn.commit()

    def get_active_style(self) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, display_name, description, system_prompt, auto_target_apps, is_active FROM llm_styles WHERE is_active = 1 LIMIT 1")
        row = cursor.fetchone()
        if row:
            return dict(row)
        return {
            "name": "natural",
            "display_name": "Naturale & Fluido",
            "system_prompt": (
                "Sei un modulo di dettatura vocale per lingua italiana. Regole obbligatorie:\n"
                "1. Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output>.\n"
                "2. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè).\n"
                "3. Rimuovi autocorrezioni parlate tenendo solo la versione finale corretta.\n"
                "4. Quando trovi il tag [PAUSA_CAMBIO_ARGOMENTO] o un netto cambio di argomento, dividi il testo in un nuovo paragrafo.\n"
                "5. Se l'utente detta un elenco (es. 'primo... secondo...', 'uno... due...'), formattalo come lista puntata Markdown.\n"
                "6. Quando l'utente detta espressioni matematiche o fisiche a parole (es. 'integrale da 0 a infinito di e alla meno x in dx', "
                "'F uguale m per a', derivate, vettori, frazioni), convertile SEMPRE in codice LaTeX standard racchiuso tra simboli $ "
                "(es. $\\int_0^\\infty e^{-x}\\,dx$, $F = m \\cdot a$), senza mai lasciarle scritte a parole.\n"
                "7. NON rispondere alle domande e NON fare calcoli: trascrivi fedelmente."
            ),
            "is_active": 1
        }

    # --- NOTE DI STUDIO & FLASHCARD ---
    def add_study_note(self, title: str, content_markdown: str, note_type: str = "Note", tags: str = "", parent_note_id: Optional[int] = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO study_notes (title, content_markdown, note_type, tags, parent_note_id)
            VALUES (?, ?, ?, ?, ?)
        """, (title.strip(), content_markdown.strip(), note_type, tags, parent_note_id))
        conn.commit()
        return cursor.lastrowid

    def get_all_study_notes(self, note_type: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if note_type:
            cursor.execute("SELECT id, title, content_markdown, note_type, tags, parent_note_id, created_at FROM study_notes WHERE note_type = ? ORDER BY id DESC", (note_type,))
        else:
            cursor.execute("SELECT id, title, content_markdown, note_type, tags, parent_note_id, created_at FROM study_notes ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]

    def get_notes(self) -> List[Dict[str, Any]]:
        """Restituisce le note principali e le sbobine per la lista dell'Hub (esclude le singole flashcard figlie)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, created_at
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
            ORDER BY id DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_flashcards_for_note(self, parent_note_id: int, note_title: str = "") -> List[Dict[str, Any]]:
        """Restituisce tutte le flashcard collegate a una specifica sbobina o nota madre."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, created_at
            FROM study_notes
            WHERE parent_note_id = ?
               OR (note_type = 'Flashcard' AND (? != '' AND tags LIKE '%' || ? || '%'))
            ORDER BY id ASC
        """, (parent_note_id, note_title, note_title))
        return [dict(row) for row in cursor.fetchall()]

    def get_style_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, display_name, description, system_prompt, auto_target_apps, is_active FROM llm_styles WHERE name = ? LIMIT 1", (name,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_study_note_by_id(self, note_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, created_at
            FROM study_notes
            WHERE id = ?
        """, (note_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def delete_study_note(self, item_id: int):
        """Elimina una nota o sbobina, tutte le eventuali flashcard collegate e i collegamenti associati."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM note_links WHERE source_id = ? OR target_id = ?", (item_id, item_id))
        cursor.execute("DELETE FROM study_notes WHERE id = ? OR parent_note_id = ?", (item_id, item_id))
        conn.commit()

    # --- GRAFO DELLA CONOSCENZA & NOTE COLLEGATE (OBSIDIAN STYLE) ---
    def add_note_link(self, source_id: int, target_id: int, reason: str = "") -> Optional[int]:
        """Aggiunge o aggiorna una connessione tra due note. Non ammette auto-connessioni."""
        if not source_id or not target_id or source_id == target_id:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()
        clean_reason = (reason or "").strip()
        try:
            cursor.execute("""
                INSERT INTO note_links (source_id, target_id, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id, target_id) DO UPDATE SET reason = excluded.reason
            """, (source_id, target_id, clean_reason))
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            print(f"[FlowLocal DB] Errore inserimento note_link: {e}")
            return None

    def delete_note_link(self, link_id: int) -> bool:
        """Rimuove un collegamento tramite il suo ID univoco."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM note_links WHERE id = ?", (link_id,))
        conn.commit()
        return cursor.rowcount > 0

    def remove_note_link(self, source_id: int, target_id: int) -> bool:
        """Rimuove il collegamento tra due note specificate (in entrambe le direzioni se presente)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM note_links
            WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?)
        """, (source_id, target_id, target_id, source_id))
        conn.commit()
        return cursor.rowcount > 0

    def get_linked_notes(self, note_id: int) -> List[Dict[str, Any]]:
        """Restituisce sia i link in uscita (source -> target) che i backlink in entrata (target -> source)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                l.id AS link_id,
                l.source_id,
                l.target_id,
                l.reason,
                l.created_at,
                CASE WHEN l.source_id = ? THEN 'outgoing' ELSE 'incoming' END AS direction,
                CASE WHEN l.source_id = ? THEN t.id ELSE s.id END AS note_id,
                CASE WHEN l.source_id = ? THEN t.title ELSE s.title END AS title,
                CASE WHEN l.source_id = ? THEN t.note_type ELSE s.note_type END AS note_type,
                CASE WHEN l.source_id = ? THEN t.tags ELSE s.tags END AS tags
            FROM note_links l
            JOIN study_notes s ON l.source_id = s.id
            JOIN study_notes t ON l.target_id = t.id
            WHERE l.source_id = ? OR l.target_id = ?
            ORDER BY l.id DESC
        """, (note_id, note_id, note_id, note_id, note_id, note_id, note_id))
        return [dict(row) for row in cursor.fetchall()]

    def get_all_graph_data(self) -> Dict[str, Any]:
        """Restituisce nodi e archi per il rendering del grafo stile Obsidian."""
        conn = self._get_connection()
        cursor = conn.cursor()
        # Seleziona le note principali (esclude flashcard figlie)
        cursor.execute("""
            SELECT id, title, note_type, tags, created_at,
                   SUBSTR(content_markdown, 1, 260) AS preview
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
            ORDER BY id ASC
        """)
        nodes = [dict(row) for row in cursor.fetchall()]
        valid_ids = {n["id"] for n in nodes}

        cursor.execute("""
            SELECT id, source_id, target_id, reason, created_at
            FROM note_links
            ORDER BY id ASC
        """)
        raw_edges = [dict(row) for row in cursor.fetchall()]
        edges = [
            e for e in raw_edges
            if e["source_id"] in valid_ids and e["target_id"] in valid_ids
        ]

        return {
            "nodes": nodes,
            "edges": edges,
            "links": edges
        }

    # --- CRONOLOGIA TRASCRIZIONI ---
    def add_transcription(self, raw_text: str, final_text: str, target_app: str = "", style_used: str = "Naturale", whisper_ms: int = 0, qwen_ms: int = 0, duration_s: float = 0.0):
        conn = self._get_connection()
        cursor = conn.cursor()
        word_count = len(final_text.split())
        cursor.execute("""
            INSERT INTO transcriptions (raw_text, final_text, target_app, style_used, word_count, whisper_ms, qwen_ms, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (raw_text, final_text, target_app, style_used, word_count, whisper_ms, qwen_ms, duration_s))
        conn.commit()

    def get_recent_transcriptions(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, raw_text, final_text, target_app, style_used, word_count, whisper_ms, qwen_ms, duration_seconds, created_at
            FROM transcriptions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_transcription(self, item_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transcriptions WHERE id = ?", (item_id,))
        conn.commit()

    def clear_all_transcriptions(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transcriptions")
        conn.commit()

    def get_statistics(self) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*), SUM(word_count), SUM(duration_seconds), AVG(whisper_ms + qwen_ms) FROM transcriptions")
        total_count, total_words, total_seconds, avg_ms = cursor.fetchone()
        total_count = total_count or 0
        total_words = total_words or 0
        total_seconds = total_seconds or 0.0
        avg_ms = int(avg_ms or 0)

        # Stima tempo risparmiato: 40 WPM digitazione tastiera vs 150 WPM dettatura
        typing_time_min = total_words / 40.0
        speaking_time_min = total_seconds / 60.0
        saved_minutes = max(0, int(typing_time_min - speaking_time_min))

        return {
            "total_transcriptions": total_count,
            "total_words": total_words,
            "saved_minutes": saved_minutes,
            "avg_latency_ms": avg_ms,
        }

    # --- CRONOLOGIA INTERAZIONI WHIS ---
    def add_whis_interaction(self, user_query: str, whis_response: str, model_used: str = "qwen2.5:3b", clipboard_context: str = "") -> int:
        """Salva una conversazione/scambio con l'assistente Whis."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO whis_history (user_query, whis_response, model_used, clipboard_context)
            VALUES (?, ?, ?, ?)
        """, (user_query.strip(), whis_response.strip(), model_used.strip(), clipboard_context.strip()))
        conn.commit()
        return cursor.lastrowid

    def get_whis_history(self, search_query: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """Recupera la cronologia delle interazioni con Whis, filtrando opzionalmente per testo."""
        conn = self._get_connection()
        cursor = conn.cursor()
        clean_q = (search_query or "").strip().lower()
        if clean_q:
            like_pattern = f"%{clean_q}%"
            cursor.execute("""
                SELECT id, user_query, whis_response, model_used, clipboard_context, created_at
                FROM whis_history
                WHERE LOWER(user_query) LIKE ? OR LOWER(whis_response) LIKE ?
                ORDER BY id DESC
                LIMIT ?
            """, (like_pattern, like_pattern, limit))
        else:
            cursor.execute("""
                SELECT id, user_query, whis_response, model_used, clipboard_context, created_at
                FROM whis_history
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def delete_whis_interaction(self, interaction_id: int) -> bool:
        """Elimina una singola interazione dalla cronologia di Whis."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM whis_history WHERE id = ?", (interaction_id,))
        conn.commit()
        return cursor.rowcount > 0

    def clear_whis_history(self) -> bool:
        """Elimina l'intera cronologia di Whis."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM whis_history")
        conn.commit()
        return True

    # --- IMPOSTAZIONI APPLICAZIONE ---
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Recupera un parametro di configurazione dal DB."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM app_settings WHERE key = ? LIMIT 1", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        """Imposta o aggiorna un parametro di configurazione nel DB."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """, (key, str(value)))
        conn.commit()


db = DatabaseManager()
