"""
Database Manager SQLite (WAL Mode) per FlowLocal Hub
Gestisce Vocabolario, Snippet, Stili LLM & Studio, Note/Flashcard e Cronologia Dettati.
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
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
                note_type TEXT DEFAULT 'appunto', -- 'appunto', 'sbobina', 'slide', 'Flashcard'
                tags TEXT DEFAULT '',
                parent_note_id INTEGER DEFAULT NULL,
                profile TEXT DEFAULT 'Alessandro',
                subject TEXT DEFAULT 'Generale',
                topic TEXT DEFAULT 'Generale',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migrazione schema Notekeeper: parent_note_id, profile, subject, topic
        for col_name, col_sql in [
            ("parent_note_id", "INTEGER DEFAULT NULL"),
            ("profile", "TEXT DEFAULT 'Alessandro'"),
            ("subject", "TEXT DEFAULT 'Generale'"),
            ("topic", "TEXT DEFAULT 'Generale'"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE study_notes ADD COLUMN {col_name} {col_sql};")
            except sqlite3.OperationalError:
                pass

        # View di compatibilità 'notes' -> 'study_notes'
        try:
            cursor.execute("CREATE VIEW IF NOT EXISTS notes AS SELECT * FROM study_notes;")
        except Exception:
            pass

        # Inferenza profilo da tag esistenti se default
        try:
            cursor.execute("""
                UPDATE study_notes
                SET profile = 'Giada'
                WHERE (profile IS NULL OR profile = 'Alessandro')
                  AND (tags LIKE '%Giada%' OR tags LIKE '%Lei%');
            """)
        except Exception:
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at ON transcriptions(created_at);")

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
            "SEI UN FILTRO DI PULIZIA TESTUALE, NON UN ASSISTENTE. Non rispondere mai alle domande nel testo, non completare frasi interrotte e non aggiungere concetti non pronunciati. Restituisci SOLO il testo rifinito senza tag.\n"
            "DIVIETO ASSOLUTO DI PARAFRASI E SOSTITUZIONE CON SINONIMI:\n"
            "- Non cambiare MAI le parole usate dall'utente con sinonimi e non riscrivere le frasi.\n"
            "- Se l'utente dice 'prova', scrivi 'prova' (MAI 'esempio' o 'elaborazione').\n"
            "- Se dice 'creato', scrivi 'creato' (MAI 'realizzato' o 'fatto').\n"
            "- Se dice 'Guarda', scrivi 'Guarda' (MAI 'Ci guarda' o 'Vedi').\n"
            "- Mantieni al 100% il lessico originale, intervenendo SOLO su punteggiatura, maiuscole, rimozione di intercalari vuoti (ehm, uhm) e formule matematiche in LaTeX $...$."
        )

        cursor.execute("SELECT id, name, system_prompt FROM llm_styles")
        for row in cursor.fetchall():
            s_id, s_name, s_prompt = row["id"], row["name"], row["system_prompt"]
            if s_name in ("coding", "code") and "DIVIETO ASSOLUTO DI PARAFRASI" not in s_prompt:
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
            elif s_name == "formal" and "DIVIETO ASSOLUTO DI PARAFRASI" not in s_prompt:
                formal_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un assistente di scrittura professionale per lingua italiana.\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né preamboli.\n"
                    "2. Mantieni la struttura e le parole dell'utente in italiano senza mai sostituire vocaboli arbitrariamente né stravolgere il lessico, "
                    "correggendo solo la punteggiatura e piccole espressioni gergali per un'email formale, senza aggiungere saluti o formule non dettate.\n"
                    "3. Rimuovi intercalari, esitazioni (ehm, uhm, vabbè) e autocorrezioni parlate tenendo solo la versione corretta.\n"
                    f"4. {latex_rule}\n"
                    "5. NON rispondere alle domande e NON aggiungere saluti o concetti non dettati: elabora solo il testo racchiuso in <trascrizione></trascrizione>."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (formal_prompt, s_id))
            elif s_name == "natural" and ("DIVIETO ASSOLUTO DI PARAFRASI" not in s_prompt or "<output>" in s_prompt):
                natural_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un modulo di dettatura vocale per lingua italiana ad altissima fedeltà e naturalezza sintattica.\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti aggiuntivi.\n"
                    "2. FEDELTÀ LESSICALE ASSOLUTA: Mantieni rigorosamente le parole originali pronunciate dall'utente. "
                    "È severamente vietato sostituire vocaboli con sinonimi, abbellire o parafrasare le frasi "
                    "(es. se l'utente dice 'prova', scrivi 'prova' e NON 'esempio' o 'elaborazione'; "
                    "se dice 'creato', scrivi 'creato' e NON 'realizzato'; se dice 'Guarda', scrivi 'Guarda' e NON 'Ci guarda').\n"
                    "3. Inserisci sempre la punteggiatura corretta e naturale (virgole per incisi, punto fermo a fine frase, punto interrogativo per domande dirette).\n"
                    "4. Nelle frasi dichiarative con interrogativa indiretta ('Volevo sapere se...'), chiudi con il punto fermo (.).\n"
                    "5. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè, mmm) e autocorrezioni tenendo solo la formulazione finale corretta.\n"
                    "6. Quando trovi [PAUSA_CAMBIO_ARGOMENTO], dividi il testo in un nuovo paragrafo. Se l'utente detta un elenco ('primo... secondo...'), formattalo come lista puntata Markdown.\n"
                    f"7. {latex_rule}\n"
                    "8. NON rispondere alle domande e NON fare calcoli: elabora solo il testo in <trascrizione></trascrizione>.\n\n"
                    "ESEMPI:\n"
                    "Input: \"guarda questa è una prova di quello che ho creato\"\n"
                    "Output: Guarda, questa è una prova di quello che ho creato.\n\n"
                    "Input: \"allora praticamente ci serve l'equazione F uguale M per A e l'integrale da 0 a infinito di E alla meno X in DX\"\n"
                    "Output: Ci serve l'equazione $F = m \\cdot a$ e $\\int_0^\\infty e^{-x}\\,dx$."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (natural_prompt, s_id))
            elif s_name == "study_latex" and ("DIVIETO ASSOLUTO DI PARAFRASI" not in s_prompt or "<output>" in s_prompt):
                study_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un assistente di studio universitario per ingegneria e fisica.\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né commenti né preamboli.\n"
                    "2. Ottimizza per appunti universitari di ingegneria e fisica: preserva il lessico dell'utente strutturando schemi concettuali ed elenchi puntati Markdown chiari.\n"
                    "3. Converti SEMPRE formule, integrali, derivate, sommatorie, vettori (utilizzando \\vec{}) e unità del Sistema Internazionale in codice LaTeX standard racchiuso tra simboli $...$.\n"
                    "4. Se l'utente pronuncia 'Flashcard: [Domanda] ... [Risposta]', formatta chiaramente come blocco di studio Q&A.\n"
                    f"5. {latex_rule}\n"
                    "6. NON risolvere problemi, NON fare calcoli e NON rispondere a domande: elabora solo il testo in <trascrizione></trascrizione>."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (study_prompt, s_id))
            elif s_name == "chat" and ("DIVIETO ASSOLUTO DI PARAFRASI" not in s_prompt or "<output>" in s_prompt):
                chat_prompt = (
                    f"{anti_hallucination}\n\n"
                    "Sei un assistente vocale per messaggistica rapida e chat (WhatsApp, Telegram, Discord).\n"
                    "Regole obbligatorie:\n"
                    "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti aggiuntivi.\n"
                    "2. Mantieni le parole originali e un tono naturale, rapido e colloquiale come digitato a tastiera; massima immediatezza.\n"
                    "3. Evita il punto fermo alla fine di messaggi brevi o battute singole, ma chiudi sempre le domande con il punto interrogativo (?).\n"
                    "4. Rimuovi intercalari ed esitazioni pure (ehm, uhm).\n"
                    f"5. {latex_rule}\n"
                    "6. NON rispondere alle domande e NON continuare il discorso: elabora solo il testo in <trascrizione></trascrizione>."
                )
                cursor.execute("UPDATE llm_styles SET system_prompt = ? WHERE id = ?", (chat_prompt, s_id))

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

        # 4. Deduplica copie ridondanti della nota di test 'matrice di trasformazione omogenea'
        cursor.execute("""
            DELETE FROM study_notes
            WHERE (title LIKE '%matrice%omogenea%' OR content_markdown LIKE '%matrice%omogenea%')
              AND id NOT IN (
                  SELECT MIN(id) FROM study_notes
                  WHERE (title LIKE '%matrice%omogenea%' OR content_markdown LIKE '%matrice%omogenea%')
              )
        """)

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
                        f"{anti_hallucination}\n\n"
                        "Sei un modulo di dettatura vocale per lingua italiana ad altissima fedeltà e naturalezza sintattica.\n"
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né commenti aggiuntivi.\n"
                        "2. FEDELTÀ LESSICALE ASSOLUTA: Mantieni rigorosamente le parole originali pronunciate dall'utente. "
                        "È severamente vietato sostituire vocaboli con sinonimi, abbellire o parafrasare le frasi "
                        "(es. se l'utente dice 'prova', scrivi 'prova' e NON 'esempio' o 'elaborazione'; "
                        "se dice 'creato', scrivi 'creato' e NON 'realizzato'; se dice 'Guarda', scrivi 'Guarda' e NON 'Ci guarda').\n"
                        "3. Inserisci sempre la punteggiatura corretta e naturale (virgole per incisi, punto fermo a fine frase, punto interrogativo per domande dirette).\n"
                        "4. Nelle frasi dichiarative con interrogativa indiretta ('Volevo sapere se...'), chiudi con il punto fermo (.).\n"
                        "5. Rimuovi intercalari ed esitazioni (ehm, uhm, vabbè, mmm) e autocorrezioni tenendo solo la formulazione finale corretta.\n"
                        "6. Quando trovi [PAUSA_CAMBIO_ARGOMENTO], dividi il testo in un nuovo paragrafo. Se l'utente detta un elenco ('primo... secondo...'), formattalo come lista puntata Markdown.\n"
                        f"7. {latex_rule_text}\n"
                        "8. NON rispondere alle domande e NON fare calcoli: elabora solo il testo in <trascrizione></trascrizione>.\n\n"
                        "ESEMPI:\n"
                        "Input: \"guarda questa è una prova di quello che ho creato\"\n"
                        "Output: Guarda, questa è una prova di quello che ho creato.\n\n"
                        "Input: \"allora praticamente ci serve l'equazione F uguale M per A e l'integrale da 0 a infinito di E alla meno X in DX\"\n"
                        "Output: Ci serve l'equazione $F = m \\cdot a$ e $\\int_0^\\infty e^{-x}\\,dx$."
                    ),
                    "notepad.exe, chrome.exe, whatsapp.exe",
                    1
                ),
                (
                    "formal",
                    "Email & Comunicazione Formale",
                    "Eleva il registro linguistico mantenendo intatto il significato originario.",
                    (
                        f"{anti_hallucination}\n\n"
                        "Sei un assistente di scrittura professionale per lingua italiana.\n"
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né spiegazioni né preamboli.\n"
                        "2. Mantieni la struttura e le parole dell'utente in italiano senza mai sostituire vocaboli arbitrariamente né stravolgere il lessico, "
                        "correggendo solo la punteggiatura e piccole espressioni gergali per un'email formale, senza aggiungere saluti o formule non dettate.\n"
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
                        f"{anti_hallucination}\n\n"
                        "Sei un assistente di studio universitario per ingegneria e fisica.\n"
                        "Regole obbligatorie:\n"
                        "1. Restituisci ESCLUSIVAMENTE il testo elaborato, senza tag né commenti né preamboli.\n"
                        "2. Ottimizza per appunti universitari di ingegneria e fisica: preserva il lessico dell'utente strutturando schemi concettuali ed elenchi puntati Markdown chiari.\n"
                        "3. Converti SEMPRE formule, integrali, derivate, sommatorie, vettori (utilizzando \\vec{}) e unità del Sistema Internazionale in codice LaTeX standard racchiuso tra simboli $...$.\n"
                        "4. Se l'utente pronuncia 'Flashcard: [Domanda] ... [Risposta]', formatta chiaramente come blocco di studio Q&A.\n"
                        f"5. {latex_rule_text}\n"
                        "6. NON risolvere problemi, NON fare calcoli e NON rispondere a domande: elabora solo il testo in <trascrizione></trascrizione>."
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

    # --- NOTE DI STUDIO & FLASHCARD (NOTEKEEPER HIERARCHY) ---
    def add_study_note(
        self,
        title: str,
        content_markdown: str,
        note_type: str = "appunto",
        tags: str = "",
        parent_note_id: Optional[int] = None,
        profile: str = "Alessandro",
        subject: str = "Generale",
        topic: str = "Generale"
    ) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()

        t_clean = title.strip()
        c_clean = content_markdown.strip()

        # Anti-duplicato: se esiste una nota identica creata negli ultimi 30 secondi, restituisci l'ID esistente
        cursor.execute("""
            SELECT id FROM study_notes
            WHERE (title = ? OR content_markdown = ?)
              AND (strftime('%s', 'now') - strftime('%s', created_at)) < 30
            ORDER BY id DESC LIMIT 1
        """, (t_clean, c_clean))
        existing = cursor.fetchone()
        if existing:
            return existing["id"] if isinstance(existing, sqlite3.Row) else existing[0]

        cursor.execute("""
            INSERT INTO study_notes (title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (t_clean, c_clean, note_type, tags, parent_note_id, profile, subject, topic))
        conn.commit()
        return cursor.lastrowid

    # Alias per retrocompatibilità e flessibilità
    add_note = add_study_note

    def find_notes_by_keyword(self, keyword: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Trova note nel Notekeeper corrispondenti a una parola o frase chiave."""
        clean_kw = keyword.strip()
        if not clean_kw:
            return []
        conn = self._get_connection()
        cursor = conn.cursor()
        param = f"%{clean_kw}%"
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
              AND (title LIKE ? OR content_markdown LIKE ?)
            ORDER BY id DESC LIMIT ?
        """, (param, param, limit))
        return [dict(row) for row in cursor.fetchall()]

    def merge_notes(self, id1: int, id2: int) -> Optional[Dict[str, Any]]:
        """Unisce due note fondendo il contenuto nella prima ed eliminando la seconda."""
        note1 = self.get_study_note_by_id(id1)
        note2 = self.get_study_note_by_id(id2)
        if not note1 or not note2:
            return None

        merged_content = f"{note1['content_markdown'].strip()}\n\n---\n\n{note2['content_markdown'].strip()}"
        self.update_study_note(
            note_id=id1,
            title=note1["title"],
            content_markdown=merged_content,
            profile=note1.get("profile", "Alessandro"),
            subject=note1.get("subject", "Generale"),
            topic=note1.get("topic", "Generale"),
            note_type=note1.get("note_type", "appunto"),
        )
        self.delete_study_note(id2)
        note1["content_markdown"] = merged_content
        return note1

    def update_study_note(
        self,
        note_id: int,
        title: str,
        content_markdown: str,
        profile: str = "Alessandro",
        subject: str = "Generale",
        topic: str = "Generale",
        note_type: str = "appunto"
    ) -> bool:
        """Aggiorna metadati e contenuto di una nota nel Notekeeper."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE study_notes
            SET title = ?, content_markdown = ?, profile = ?, subject = ?, topic = ?, note_type = ?
            WHERE id = ?
        """, (title.strip(), content_markdown.strip(), profile.strip(), subject.strip(), topic.strip(), note_type.strip(), note_id))
        conn.commit()
        return cursor.rowcount > 0

    def get_all_study_notes(self, note_type: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if note_type:
            cursor.execute(
                "SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at "
                "FROM study_notes WHERE note_type = ? ORDER BY id DESC",
                (note_type,)
            )
        else:
            cursor.execute(
                "SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at "
                "FROM study_notes ORDER BY id DESC"
            )
        return [dict(row) for row in cursor.fetchall()]

    def get_notes(self) -> List[Dict[str, Any]]:
        """Restituisce le note principali e le sbobine per la lista dell'Hub (esclude le singole flashcard figlie)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
            ORDER BY id DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_subjects_tree(self, profile: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Restituisce l'albero gerarchico {subject: [{'topic': topic_name, 'count': num_notes}, ...]}."""
        conn = self._get_connection()
        cursor = conn.cursor()
        query = """
            SELECT COALESCE(NULLIF(TRIM(subject), ''), 'Generale') as subj,
                   COALESCE(NULLIF(TRIM(topic), ''), 'Generale') as top,
                   COUNT(*) as cnt
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
        """
        params = []
        if profile and profile.strip().lower() not in ("tutti", "all", ""):
            query += " AND profile = ?"
            params.append(profile.strip())
        query += " GROUP BY subj, top ORDER BY subj ASC, top ASC"
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        tree: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            s = row["subj"]
            t = row["top"]
            c = row["cnt"]
            if s not in tree:
                tree[s] = []
            tree[s].append({"topic": t, "count": c})
        return tree

    def get_notes_by_hierarchy(
        self,
        profile: Optional[str] = None,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
        search_query: str = ""
    ) -> List[Dict[str, Any]]:
        """Recupera le note filtrate per profilo, materia, argomento e testo di ricerca."""
        conn = self._get_connection()
        cursor = conn.cursor()
        query = """
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
            FROM study_notes
            WHERE (parent_note_id IS NULL AND note_type != 'Flashcard')
        """
        params = []
        if profile and profile.strip().lower() not in ("tutti", "all", ""):
            query += " AND profile = ?"
            params.append(profile.strip())
        if subject and subject.strip().lower() not in ("tutte", "tutti", "all", ""):
            query += " AND subject = ?"
            params.append(subject.strip())
        if topic and topic.strip().lower() not in ("tutti", "tutte", "all", ""):
            query += " AND topic = ?"
            params.append(topic.strip())
        if search_query and search_query.strip():
            sq = f"%{search_query.strip()}%"
            query += " AND (title LIKE ? OR content_markdown LIKE ? OR tags LIKE ?)"
            params.extend([sq, sq, sq])
        query += " ORDER BY id DESC"
        cursor.execute(query, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def search_knowledge_base(
        self,
        query: str,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
        limit: int = 4
    ) -> List[Dict[str, Any]]:
        """Cerca per parole chiave (filtrando rigorosamente le stopword italiane) dentro notes e flashcards."""
        if not query or not query.strip():
            return []

        stopwords = {
            "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
            "di", "a", "da", "in", "con", "su", "per", "tra", "fra",
            "e", "ed", "o", "od", "ma", "che", "chi", "cui", "qual", "quale", "quali",
            "come", "cosa", "dove", "quando", "perché", "questo", "questa",
            "questi", "queste", "quello", "quella", "quelli", "quelle",
            "mio", "tuo", "suo", "nostro", "vostro", "loro", "non", "più",
            "molto", "poco", "tutto", "tutti", "tutte", "anche", "ancora",
            "solo", "già", "mentre", "se", "si", "ci", "vi", "ne",
            "ha", "hanno", "ho", "hai", "è", "era", "sono", "stato", "stata",
            "stati", "state", "del", "dello", "della", "dei", "degli", "delle",
            "al", "allo", "alla", "ai", "agli", "alle", "dal", "dallo", "dalla",
            "dai", "dagli", "dalle", "nel", "nello", "nella", "nei", "negli",
            "nelle", "sul", "sullo", "sulla", "sui", "sugli", "sulle", "sull",
            # Termini generici di comando o metadati (vietati come keyword di ricerca RAG)
            "whis", "nota", "note", "appunto", "appunti", "slide", "sbobina", "sbobine",
            "prendi", "segna", "crea", "aggiungi", "dimmi", "salva", "annota", "scrivi",
            "ricordami", "trova", "cerca", "leggi", "mostra"
        }

        raw_words = re.findall(r'\b[A-Za-z0-9_À-ÿ]{3,}\b', query.lower())
        keywords = [w for w in raw_words if w not in stopwords]
        # SOGLIA MINIMA: se non ci sono keyword specifiche, restituisci lista vuota
        # (MAI restituire note fallback a caso se la domanda non contiene termini precisi!)
        if not keywords:
            return []

        conn = self._get_connection()
        cursor = conn.cursor()

        sql_base = """
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
            FROM study_notes
            WHERE 1=1
        """
        params = []
        if subject and subject.strip().lower() not in ("tutte", "tutti", "all", ""):
            sql_base += " AND subject = ?"
            params.append(subject.strip())
        if topic and topic.strip().lower() not in ("tutti", "tutte", "all", ""):
            sql_base += " AND topic = ?"
            params.append(topic.strip())

        kw_conditions = []
        for kw in keywords[:8]:
            kw_param = f"%{kw}%"
            kw_conditions.append("(title LIKE ? OR content_markdown LIKE ? OR tags LIKE ?)")
            params.extend([kw_param, kw_param, kw_param])

        if not kw_conditions:
            return []

        sql_base += f" AND ({' OR '.join(kw_conditions)})"
        sql_base += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql_base, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def get_flashcards_for_note(self, parent_note_id: int, note_title: str = "") -> List[Dict[str, Any]]:
        """Restituisce tutte le flashcard collegate a una specifica sbobina o nota madre."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
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
            SELECT id, title, content_markdown, note_type, tags, parent_note_id, profile, subject, topic, created_at
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

    def get_dictation_streak(self) -> int:
        """
        Calcola i giorni consecutivi (streak) di dettatura effettivi basati sui giorni di calendario (localtime).
        Se l'utente ha dettato oggi, la streak include oggi e conta a ritroso i giorni consecutivi precedenti.
        Se non ha ancora dettato oggi ma ha dettato ieri, la streak accumulata viene mantenuta attiva (in attesa della dettatura di oggi).
        Se l'ultimo giorno di dettatura è anteriore a ieri o non ci sono trascrizioni, la streak è 0.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT DATE(created_at, 'localtime') as day
            FROM transcriptions
            WHERE created_at IS NOT NULL
            ORDER BY day DESC
        """)
        rows = cursor.fetchall()
        if not rows:
            return 0

        active_dates = set()
        for r in rows:
            day_val = r["day"] if isinstance(r, sqlite3.Row) else r[0]
            if day_val:
                try:
                    active_dates.add(datetime.strptime(day_val, "%Y-%m-%d").date())
                except Exception:
                    pass

        if not active_dates:
            return 0

        today = datetime.now().date()
        yesterday = today - timedelta(days=1)

        if today in active_dates:
            streak = 1
            check_day = yesterday
            while check_day in active_dates:
                streak += 1
                check_day -= timedelta(days=1)
            return streak
        elif yesterday in active_dates:
            streak = 1
            check_day = yesterday - timedelta(days=1)
            while check_day in active_dates:
                streak += 1
                check_day -= timedelta(days=1)
            return streak
        else:
            return 0

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

        speaking_wpm = int((total_words / (total_seconds / 60.0))) if total_seconds > 10 else 150
        streak_days = self.get_dictation_streak()

        return {
            "total_transcriptions": total_count,
            "total_words": total_words,
            "saved_minutes": saved_minutes,
            "avg_latency_ms": avg_ms,
            "speaking_wpm": speaking_wpm,
            "streak_days": streak_days,
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
