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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

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

        conn.commit()
        self._seed_default_data()

    def _seed_default_data(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Seed Stili LLM predefiniti se tabella vuota
        cursor.execute("SELECT COUNT(*) FROM llm_styles")
        if cursor.fetchone()[0] == 0:
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
                        "6. NON rispondere alle domande e NON fare calcoli: trascrivi fedelmente."
                    ),
                    "notepad.exe, chrome.exe, whatsapp.exe",
                    1
                ),
                (
                    "formal",
                    "Email & Comunicazione Formale",
                    "Eleva il registro linguistico mantenendo intatto il significato originario.",
                    (
                        "Sei un assistente di scrittura professionale per lingua italiana. "
                        "Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output>.\n"
                        "Trasforma il testo pronunciato in una comunicazione formale, elegante e grammaticalmente impeccabile. "
                        "Mantieni il significato originale ma eleva il registro stilistico."
                    ),
                    "outlook.exe, thunderbird.exe",
                    0
                ),
                (
                    "coding",
                    "Sviluppatore & Coding",
                    "Riconosce sintassi di programmazione, camelCase, snake_case e termini tecnici software.",
                    (
                        "Sei un assistente vocale per sviluppatori software. "
                        "Restituisci ESCLUSIVAMENTE il testo elaborato all'interno dei tag <output></output>.\n"
                        "Quando l'utente detta codice o nomi di variabili, formattali correttamente (camelCase, snake_case, SQL, HTML). "
                        "Mantieni intatti i termini tecnici inglesi (commit, push, pull request, endpoint, bug, refactor, decorator)."
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
                        "2. Se l'utente detta espressioni matematiche o formule (es. 'integrale da zero a infinito', 'f di x uguale...'), convertile in sintassi LaTeX racchiusa tra $...$ o $$...$$.\n"
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

    def apply_phonetic_replacements(self, text: str) -> str:
        """Sostituisce deterministiche alias fonetici prima di inviare a Whisper/LLM."""
        items = self.get_all_vocabulary()
        result = text
        for item in items:
            term = item["correct_term"]
            aliases = [a.strip() for a in item.get("phonetic_aliases", "").split(",") if a.strip()]
            for alias in aliases:
                # Sostituzione case-insensitive
                pattern = re.compile(rf'\b{re.escape(alias)}\b', re.IGNORECASE)
                result = pattern.sub(term, result)
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
            "system_prompt": "Trascrivi con punteggiatura corretta e rimuovi intercalari.",
            "is_active": 1
        }

    # --- NOTE DI STUDIO & FLASHCARD ---
    def add_study_note(self, title: str, content_markdown: str, note_type: str = "Note", tags: str = "") -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO study_notes (title, content_markdown, note_type, tags)
            VALUES (?, ?, ?, ?)
        """, (title.strip(), content_markdown.strip(), note_type, tags))
        conn.commit()
        return cursor.lastrowid

    def get_all_study_notes(self, note_type: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if note_type:
            cursor.execute("SELECT id, title, content_markdown, note_type, tags, created_at FROM study_notes WHERE note_type = ? ORDER BY id DESC", (note_type,))
        else:
            cursor.execute("SELECT id, title, content_markdown, note_type, tags, created_at FROM study_notes ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]

    def delete_study_note(self, item_id: int):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM study_notes WHERE id = ?", (item_id,))
        conn.commit()

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


db = DatabaseManager()
