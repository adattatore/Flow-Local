"""
Cloud Sync Manager per FlowLocal (Coda Cloud Controllata & Anti-Lag Gaming).
Gestisce la sincronizzazione locale con Google Drive Desktop (FlowLocal_Sync),
le code inbox/outbox, l'elaborazione manuale sequenziale e la generazione
dell'indice completo library.json per la PWA Mobile.
"""

import base64
import json
import os
import re
import shutil
import string
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Callable, Tuple

from core.paths import get_user_data_path
from database.db_manager import db

_SYNC_OBF_SECRET = "FlowLocalSyncKey2026"


def obfuscate_sync_secret(raw: str, secret: str = _SYNC_OBF_SECRET) -> str:
    """Offusca una chiave API con XOR + Base64 per il ponte FlowLocal_Sync."""
    if not raw:
        return ""
    raw_bytes = raw.encode("utf-8")
    sec_bytes = secret.encode("utf-8")
    xored = bytes(b ^ sec_bytes[i % len(sec_bytes)] for i, b in enumerate(raw_bytes))
    return base64.b64encode(xored).decode("ascii")


def deobfuscate_sync_secret(b64: str, secret: str = _SYNC_OBF_SECRET) -> str:
    """Decodifica una chiave offuscata con XOR + Base64."""
    if not b64:
        return ""
    raw_bytes = base64.b64decode(b64.encode("ascii"))
    sec_bytes = secret.encode("utf-8")
    plain = bytes(b ^ sec_bytes[i % len(sec_bytes)] for i, b in enumerate(raw_bytes))
    return plain.decode("utf-8")


SUPPORTED_AUDIO_EXTENSIONS = {
    ".m4a", ".mp3", ".wav", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".aac", ".flac"
}


def sanitize_filename(name: Optional[str], max_len: int = 80) -> str:
    """Rende una stringa sicura come nome file su Windows/macOS/Linux."""
    clean = re.sub(r'[\\/*?:"<>|]', "", str(name or "")).strip()
    clean = clean.replace(" ", "_")
    clean = "".join(c for c in clean if c.isalnum() or c in ("_", "-", "."))
    return clean[:max_len] if clean else "file"


class CloudSyncManager:
    """
    Gestore della cartella sincronizzata da Google Drive Desktop.
    Struttura directory:
      <sync_base_dir>/
        ├── inbox_audio/           (File audio in attesa di sbobinatura)
        │   └── processed/         (Archivio storico audio elaborati)
        └── outbox_study/          (Sbobine, PDF, DOCX e library.json per PWA)
    """

    def __init__(self, sync_base_dir: Optional[str] = None):
        self._sync_base_dir = sync_base_dir or self._discover_or_load_sync_dir()
        self.ensure_sync_folders()

    def _discover_or_load_sync_dir(self) -> str:
        """Cerca la cartella salvata nel DB o rileva automaticamente Google Drive Desktop."""
        saved = db.get_setting("cloud_sync_folder")
        if saved and os.path.exists(saved):
            return saved

        # Su Linux i dati utente scrivibili risiedono in ~/.local/share/FlowLocal
        if sys.platform.startswith("linux"):
            linux_sync_dir = str(get_user_data_path("FlowLocal_Sync"))
            os.makedirs(linux_sync_dir, exist_ok=True)
            db.set_setting("cloud_sync_folder", linux_sync_dir)
            return linux_sync_dir

        # Percorsi tipici di Google Drive Desktop su Windows
        home = os.path.expanduser("~")
        candidate_paths = [
            r"G:\Il mio Drive\FlowLocal_Sync",
            r"G:\My Drive\FlowLocal_Sync",
            r"G:\FlowLocal_Sync",
            os.path.join(home, "Google Drive", "Il mio Drive", "FlowLocal_Sync"),
            os.path.join(home, "Google Drive", "FlowLocal_Sync"),
            os.path.join(home, "FlowLocal_Sync"),
        ]

        for cand in candidate_paths:
            # Se la cartella madre esiste, candidiamo questo percorso
            parent = os.path.dirname(cand)
            if os.path.exists(parent):
                try:
                    os.makedirs(cand, exist_ok=True)
                    db.set_setting("cloud_sync_folder", cand)
                    return cand
                except Exception:
                    continue

        # Fallback sicuro nella home utente
        default_path = os.path.join(home, "FlowLocal_Sync")
        os.makedirs(default_path, exist_ok=True)
        db.set_setting("cloud_sync_folder", default_path)
        return default_path

    def get_sync_base_dir(self) -> str:
        """Restituisce il percorso assoluto della cartella principale sincronizzata."""
        return self._sync_base_dir

    def set_sync_base_dir(self, new_dir: str):
        """Aggiorna il percorso principale, lo persiste nel DB e crea le sottocartelle."""
        if not new_dir or not new_dir.strip():
            return
        abs_path = os.path.abspath(new_dir.strip())
        self._sync_base_dir = abs_path
        db.set_setting("cloud_sync_folder", abs_path)
        self.ensure_sync_folders()

    def ensure_sync_folders(self):
        """Assicura l'esistenza di inbox_audio, processed e outbox_study."""
        try:
            os.makedirs(self.get_inbox_audio_dir(), exist_ok=True)
            os.makedirs(self.get_processed_audio_dir(), exist_ok=True)
            os.makedirs(self.get_outbox_study_dir(), exist_ok=True)
        except Exception as e:
            print(f"[CloudSync] Avviso creazione cartelle: {e}")

    def get_inbox_audio_dir(self) -> str:
        return os.path.join(self._sync_base_dir, "inbox_audio")

    def get_processed_audio_dir(self) -> str:
        return os.path.join(self._sync_base_dir, "inbox_audio", "processed")

    def get_outbox_study_dir(self) -> str:
        return os.path.join(self._sync_base_dir, "outbox_study")

    # -------------------------------------------------------------------------
    # SCANSIONE CODA CLOUD (Leggerissima, 0 CPU / 0 GPU, sola lettura)
    # -------------------------------------------------------------------------
    def get_pending_queue(self) -> List[Dict[str, Any]]:
        """
        Scansiona inbox_audio/ alla ricerca di file audio pendenti e relativi .meta.json.
        Operazione di sola lettura I/O senza elaborazione grafica né acustica.
        """
        inbox = self.get_inbox_audio_dir()
        if not os.path.exists(inbox):
            return []

        pending_items: List[Dict[str, Any]] = []
        try:
            with os.scandir(inbox) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if name.startswith(".") or name.endswith(".meta.json"):
                        continue

                    _, ext = os.path.splitext(name)
                    if ext.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                        continue

                    audio_path = entry.path
                    size_bytes = 0
                    try:
                        size_bytes = entry.stat().st_size
                    except Exception:
                        pass

                    # Ricerca metadati .meta.json associati
                    meta_path = None
                    possible_metas = [
                        f"{audio_path}.meta.json",
                        f"{os.path.splitext(audio_path)[0]}.meta.json",
                    ]
                    for p in possible_metas:
                        if os.path.exists(p):
                            meta_path = p
                            break

                    default_title = os.path.splitext(name)[0]
                    profile = "Alessandro"
                    subject = ""
                    title = default_title
                    created_at = None

                    if meta_path:
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta_data = json.load(f)
                            if isinstance(meta_data, dict):
                                meta_profile = (meta_data.get("profile") or "").strip()
                                if meta_profile:
                                    # Retrocompatibilità automatica: 'Lei' -> 'Giada'
                                    profile = "Giada" if meta_profile.lower() in ("lei", "giada") else meta_profile
                                meta_subject = (meta_data.get("subject") or "").strip()
                                subject = meta_subject
                                meta_title = (meta_data.get("title") or "").strip()
                                if meta_title:
                                    title = meta_title
                                created_at = meta_data.get("created_at")
                        except Exception:
                            pass

                    pending_items.append({
                        "filename": name,
                        "audio_path": audio_path,
                        "meta_path": meta_path,
                        "size_bytes": size_bytes,
                        "profile": profile,
                        "subject": subject,
                        "title": title,
                        "created_at": created_at,
                    })
        except Exception as e:
            print(f"[CloudSync] Errore lettura coda inbox: {e}")

        # Ordina per data/nome per elaborazione FIFO coerente
        pending_items.sort(key=lambda x: x["filename"].lower())
        return pending_items

    def get_pending_count(self) -> int:
        """Restituisce il conteggio immediato dei file audio in attesa."""
        return len(self.get_pending_queue())

    # -------------------------------------------------------------------------
    # GENERAZIONE INDICE LIBRERIA (outbox_study/library.json)
    # -------------------------------------------------------------------------
    def generate_library_json(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Esporta lo snapshot completo del database in formato JSON per la PWA Mobile:
        sbobine, appunti rapidi, flashcard, sommari esecutivi e grafi relazionali.
        """
        self.ensure_sync_folders()
        try:
            self.import_mobile_changes()
        except Exception as e:
            print(f"[CloudSync] Avviso import modifiche mobile: {e}")

        all_notes = db.get_all_study_notes()
        all_graph = db.get_all_graph_data()

        outbox_dir = self.get_outbox_study_dir()

        # Raccoglie i profili e le materie uniche
        profiles_set = {"Alessandro", "Giada"}
        subjects_set = set()

        notes_list = []
        flashcards_list = []

        # Mappa per associare le flashcard al titolo della nota genitore
        note_titles_by_id = {n["id"]: n["title"] for n in all_notes}

        for n in all_notes:
            note_id = n["id"]
            note_type = (n.get("note_type") or "Note").strip()
            title = (n.get("title") or "Senza titolo").strip()
            content = (n.get("content_markdown") or "")
            tags_str = str(n.get("tags") or "")
            created_at = str(n.get("created_at") or "")
            parent_id = n.get("parent_note_id")

            # Profilo, Materia e Argomento dal database con fallback su tag
            profile = (n.get("profile") or "Alessandro").strip()
            if "Giada" in tags_str or "Profilo: Giada" in tags_str or "Lei" in tags_str or "Profilo: Lei" in tags_str:
                profile = "Giada"
            elif "Alessandro" in tags_str or "Profilo: Alessandro" in tags_str:
                profile = "Alessandro"

            subject = (n.get("subject") or "").strip()
            topic = (n.get("topic") or "Generale").strip()
            if not subject or subject == "Generale":
                # Cerca tag di materia tipo "Materia: XXX" oppure "Fisica", "Diritto", ecc.
                tag_tokens = [(t or "").strip() for t in tags_str.split(",") if (t or "").strip()]
                for tok in tag_tokens:
                    if tok.lower().startswith("materia:"):
                        subject = (tok.split(":", 1)[1] or "").strip()
                        break
                    elif tok.lower() not in ("audio", "sbobina", "note", "flashcard", "esame", "alessandro", "giada", "lei"):
                        subject = tok
                        break

            if not subject:
                subject = "Generale"

            profiles_set.add(profile)
            if subject and subject != "Generale":
                subjects_set.add(subject)

            # Normalizzazione automatica retrocompatibile dei tag ("Profilo: Lei" -> "Profilo: Giada", "Lei" -> "Giada")
            norm_tags = tags_str.replace("Profilo: Lei", "Profilo: Giada")
            norm_tokens = [(t or "").strip() for t in norm_tags.split(",") if (t or "").strip()]
            norm_tokens = ["Giada" if t in ("Lei", "Profilo: Lei") else t for t in norm_tokens]
            clean_tags_str = ", ".join(norm_tokens)

            if note_type == "Flashcard":
                # Parsing domanda / risposta dal markdown se strutturato
                q_text = (title or "").replace("Flashcard:", "").strip()
                a_text = content
                if "**Domanda:**" in content and "**Risposta:**" in content:
                    parts = content.split("**Risposta:**", 1)
                    q_part = (parts[0] or "").replace("**Domanda:**", "").strip()
                    a_part = (parts[1] or "").strip() if len(parts) > 1 else ""
                    if q_part:
                        q_text = q_part
                    if a_part:
                        a_text = a_part

                flashcards_list.append({
                    "id": note_id,
                    "parent_note_id": parent_id,
                    "parent_title": note_titles_by_id.get(parent_id, ""),
                    "question": q_text,
                    "answer": a_text,
                    "profile": profile,
                    "subject": subject,
                    "topic": topic,
                    "note_type": "Flashcard",
                    "tags": clean_tags_str,
                    "created_at": created_at,
                })
            else:
                # Estrazione Sommario Esecutivo se presente nella nota
                summary_text = ""
                if "# Sommario Esecutivo" in content:
                    try:
                        sum_part = content.split("# Sommario Esecutivo", 1)[1]
                        if "## Trascrizione" in sum_part:
                            summary_text = (sum_part.split("## Trascrizione", 1)[0] or "").strip()
                        elif "---" in sum_part:
                            summary_text = (sum_part.split("---", 1)[0] or "").strip()
                        else:
                            summary_text = (sum_part[:600] or "").strip()
                    except Exception:
                        summary_text = ""

                # Controllo eventuale file PDF / DOCX presente in outbox
                safe_t = sanitize_filename(title)
                expected_pdf = f"{safe_t}_{note_id}.pdf"
                expected_docx = f"{safe_t}_{note_id}.docx"
                has_pdf = os.path.exists(os.path.join(outbox_dir, expected_pdf))
                has_docx = os.path.exists(os.path.join(outbox_dir, expected_docx))

                notes_list.append({
                    "id": note_id,
                    "title": title,
                    "content_markdown": content,
                    "note_type": note_type,
                    "profile": profile,
                    "subject": subject,
                    "topic": topic,
                    "folder_id": n.get("folder_id"),
                    "tags": clean_tags_str,
                    "summary": summary_text,
                    "pdf_filename": expected_pdf if has_pdf else None,
                    "docx_filename": expected_docx if has_docx else None,
                    "created_at": created_at,
                    "updated_at": str(n.get("updated_at") or created_at),
                    "trashed": bool(n.get("trashed")),
                    "created_by_name": str(n.get("created_by_name") or ""),
                    "created_by_email": str(n.get("created_by_email") or ""),
                    "created_by_avatar": str(n.get("created_by_avatar") or ""),
                    "last_edited_by_name": str(n.get("last_edited_by_name") or ""),
                    "last_edited_by_email": str(n.get("last_edited_by_email") or ""),
                    "last_edited_at_utc": str(n.get("last_edited_at_utc") or n.get("updated_at") or created_at),
                })

        # Relazioni e link del grafo
        links_list = []
        raw_links = all_graph.get("links") or all_graph.get("edges") or []
        for l in raw_links:
            s_id = l.get("source_id")
            t_id = l.get("target_id")
            links_list.append({
                "source_id": s_id,
                "target_id": t_id,
                "source_title": note_titles_by_id.get(s_id, f"Nota #{s_id}"),
                "target_title": note_titles_by_id.get(t_id, f"Nota #{t_id}"),
                "reason": l.get("reason") or "",
            })

        try:
            folders_tree = db.get_folders_tree()
        except Exception:
            folders_tree = []

        library_payload = {
            "library_version": "1.1",
            "generated_at": datetime.now().isoformat(),
            "sync_folder": self._sync_base_dir,
            "profiles": sorted(list(profiles_set)),
            "subjects": sorted(list(subjects_set)),
            "folders": folders_tree,
            "stats": {
                "total_notes": len(notes_list),
                "total_flashcards": len(flashcards_list),
                "total_links": len(links_list),
            },
            "notes": notes_list,
            "flashcards": flashcards_list,
            "links": links_list,
        }

        target_file = output_path or os.path.join(outbox_dir, "library.json")
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(library_payload, f, ensure_ascii=False, indent=2)
            print(f"[CloudSync] library.json generato con successo: {target_file}")
        except Exception as e:
            print(f"[CloudSync] Errore scrittura library.json: {e}")

        self.export_user_state()
        self.export_flowtime_state()
        self.export_account_config()
        return library_payload

    @staticmethod
    def _discover_google_client_id() -> str:
        """Recupera il client_id OAuth da settings, token o credentials.json."""
        saved = (db.get_setting("google_client_id", "") or "").strip()
        if saved:
            return saved
        for fname in ("token_drive.json", "token_calendar.json", "credentials.json"):
            for p in (str(get_user_data_path(fname)), os.path.join(os.getcwd(), fname)):
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        cid = (
                            data.get("client_id")
                            or (data.get("installed") or {}).get("client_id")
                            or (data.get("web") or {}).get("client_id")
                            or ""
                        ).strip()
                        if cid:
                            return cid
                    except Exception:
                        pass
        return ""

    def export_account_config(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Esporta la configurazione condivisa (gemini_api_key offuscata in XOR+Base64, google_client_id e preferenze tema)
        in FlowLocal_Sync/account_config.json per la sincronizzazione automatica tra dispositivi.
        """
        api_key = (db.get_setting("gemini_api_key", "") or "").strip()
        theme = (db.get_setting("theme", "obsidian") or "obsidian").strip()
        reading_mode = (db.get_setting("reading_mode", "0") or "0").strip()
        client_id = self._discover_google_client_id()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload: Dict[str, Any] = {
            "schema_version": 2,
            "updated_at_utc": now_utc,
            "obfuscation": "xor-b64-v1",
            "gemini_api_key_obf": obfuscate_sync_secret(api_key) if api_key else "",
            "google_client_id": client_id,
            "theme": theme,
            "reading_mode": reading_mode == "1",
        }
        target = output_path or os.path.join(self._sync_base_dir, "account_config.json")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not api_key and os.path.exists(target):
                try:
                    with open(target, "r", encoding="utf-8") as ef:
                        prev = json.load(ef)
                    if isinstance(prev, dict) and prev.get("gemini_api_key_obf"):
                        payload["gemini_api_key_obf"] = prev["gemini_api_key_obf"]
                except Exception:
                    pass
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            db.set_setting("account_config_updated_at", now_utc)
        except Exception as e:
            print(f"[CloudSync] Errore scrittura account_config.json: {e}")
        return payload

    def export_user_config(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """Alias retrocompatibile verso export_account_config."""
        return self.export_account_config(output_path=output_path)

    def import_account_config(self, cfg: Dict[str, Any]) -> Dict[str, bool]:
        """Importa chiave Gemini e tema da account_config.json se presenti e più recenti o mancanti localmente."""
        changed = {"gemini_key": False, "theme": False}
        if not isinstance(cfg, dict):
            return changed

        remote_ts = str(cfg.get("updated_at_utc") or "")
        local_ts = str(db.get_setting("account_config_updated_at", "") or "")
        remote_is_newer = bool(remote_ts and (not local_ts or remote_ts > local_ts))

        obf = str(cfg.get("gemini_api_key_obf") or "").strip()
        if obf:
            try:
                decoded = deobfuscate_sync_secret(obf).strip()
            except Exception:
                decoded = ""
            local_key = (db.get_setting("gemini_api_key", "") or "").strip()
            if decoded and (not local_key or (remote_is_newer and decoded != local_key)):
                db.set_setting("gemini_api_key", decoded)
                if not (db.get_setting("ai_provider", "") or "").strip():
                    db.set_setting("ai_provider", "adaptive")
                changed["gemini_key"] = True

        remote_theme = str(cfg.get("theme") or "").strip()
        if remote_theme and remote_is_newer:
            local_theme = (db.get_setting("theme", "obsidian") or "obsidian").strip()
            if remote_theme != local_theme:
                db.set_setting("theme", remote_theme)
                changed["theme"] = True
        if "reading_mode" in cfg and remote_is_newer:
            db.set_setting("reading_mode", "1" if cfg.get("reading_mode") else "0")

        if remote_ts and (changed["gemini_key"] or changed["theme"]):
            db.set_setting("account_config_updated_at", remote_ts)
        return changed

    def export_flowtime_state(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """Esporta eventi calendario, esami, sessioni studio e cartelle in FlowLocal_Sync/flowtime_state.json."""
        try:
            folders = db.get_folders_tree()
        except Exception:
            folders = []
        try:
            exams = db.get_exam_countdowns()
        except Exception:
            exams = []
        try:
            weekly_study = db.get_weekly_study_summary()
        except Exception:
            weekly_study = []
        try:
            start_d = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            end_d = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
            events = db.get_calendar_events(start_d, end_d)
        except Exception:
            events = []

        payload = {
            "schema_version": 1,
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "folders": folders,
            "exams": exams,
            "weekly_study": weekly_study,
            "events": events,
        }
        target = output_path or os.path.join(self._sync_base_dir, "flowtime_state.json")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[CloudSync] Errore scrittura flowtime_state.json: {e}")
        return payload

    @staticmethod
    def _parse_utc_ts(ts: Any) -> float:
        s = str(ts or "").strip()
        if not s:
            return 0.0
        try:
            if s.endswith("Z"):
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return 0.0

    def merge_library_payload(self, lib: Dict[str, Any]) -> int:
        """Merge bidirezionale di library.json in SQLite basato su updated_at UTC (incluse note cestinate e campi autore)."""
        if not isinstance(lib, dict):
            return 0
        imported = 0
        existing_notes = db.get_all_study_notes()
        existing_by_id = {str(n["id"]): n for n in existing_notes}
        existing_pwa_by_tag: Dict[str, Dict[str, Any]] = {}
        for n in existing_notes:
            m = re.search(r"pwa_id:(pwa_[^\s,]+)", str(n.get("tags") or ""))
            if m:
                existing_pwa_by_tag[m.group(1)] = n

        for n in (lib.get("notes") or []):
            nid = str(n.get("id") or "")
            remote_ts = self._parse_utc_ts(n.get("updated_at") or n.get("last_edited_at_utc") or n.get("created_at"))
            if nid.startswith("pwa_"):
                if nid in existing_pwa_by_tag:
                    db_note = existing_pwa_by_tag[nid]
                    local_ts = self._parse_utc_ts(db_note.get("updated_at") or db_note.get("last_edited_at_utc") or db_note.get("created_at"))
                    if remote_ts > local_ts:
                        db.update_study_note(
                            note_id=int(db_note["id"]),
                            title=n.get("title") or db_note.get("title") or "Appunto Mobile",
                            content_markdown=n.get("content_markdown") or "",
                            profile=n.get("profile") or db_note.get("profile") or "Alessandro",
                            subject=n.get("subject") or db_note.get("subject") or "Generale",
                            topic=n.get("topic") or db_note.get("topic") or "Generale",
                            note_type=n.get("note_type") or db_note.get("note_type") or "Note",
                            last_edited_by_name=n.get("last_edited_by_name") or "",
                            last_edited_by_email=n.get("last_edited_by_email") or "",
                            last_edited_at_utc=n.get("last_edited_at_utc") or n.get("updated_at") or "",
                            updated_at=n.get("updated_at") or "",
                            trashed=1 if n.get("trashed") else 0,
                        )
                        imported += 1
                    continue
                tags = str(n.get("tags") or "").strip()
                pwa_tag = f"pwa_id:{nid}"
                full_tags = f"{tags}, {pwa_tag}" if tags else pwa_tag
                db.add_study_note(
                    title=n.get("title") or "Appunto Mobile",
                    content_markdown=n.get("content_markdown") or "",
                    note_type=n.get("note_type") or "Note",
                    tags=full_tags,
                    profile=n.get("profile") or "Alessandro",
                    subject=n.get("subject") or "Generale",
                    topic=n.get("topic") or "Generale",
                    created_by_name=n.get("created_by_name") or "",
                    created_by_email=n.get("created_by_email") or "",
                    created_by_avatar=n.get("created_by_avatar") or "",
                    last_edited_by_name=n.get("last_edited_by_name") or "",
                    last_edited_by_email=n.get("last_edited_by_email") or "",
                    last_edited_at_utc=n.get("last_edited_at_utc") or n.get("updated_at") or "",
                    updated_at=n.get("updated_at") or "",
                    trashed=1 if n.get("trashed") else 0,
                )
                imported += 1
            elif nid in existing_by_id:
                db_note = existing_by_id[nid]
                local_ts = self._parse_utc_ts(db_note.get("updated_at") or db_note.get("last_edited_at_utc") or db_note.get("created_at"))
                if (remote_ts > local_ts) or (
                    n.get("source") == "pwa_edited"
                    and (n.get("content_markdown") or "") != (db_note.get("content_markdown") or "")
                ):
                    db.update_study_note(
                        note_id=int(nid),
                        title=n.get("title") or db_note.get("title") or "Senza titolo",
                        content_markdown=n.get("content_markdown") or "",
                        profile=n.get("profile") or db_note.get("profile") or "Alessandro",
                        subject=n.get("subject") or db_note.get("subject") or "Generale",
                        topic=n.get("topic") or db_note.get("topic") or "Generale",
                        note_type=n.get("note_type") or db_note.get("note_type") or "Note",
                        last_edited_by_name=n.get("last_edited_by_name") or "",
                        last_edited_by_email=n.get("last_edited_by_email") or "",
                        last_edited_at_utc=n.get("last_edited_at_utc") or n.get("updated_at") or "",
                        updated_at=n.get("updated_at") or "",
                        trashed=1 if n.get("trashed") else 0,
                    )
                    imported += 1
        return imported

    def merge_flowtime_payload(self, ft: Dict[str, Any]) -> Tuple[int, int]:
        """Merge di esami ed eventi calendario da flowtime_state.json in SQLite."""
        if not isinstance(ft, dict):
            return (0, 0)
        ev_imp = 0
        ex_imp = 0
        existing_events = db.get_calendar_events("2000-01-01", "2100-01-01")
        known_ev_keys = {(e.get("title"), e.get("event_date"), e.get("start_time")) for e in existing_events}
        for ev in (ft.get("events") or []):
            ev_id = str(ev.get("id") or "")
            key = (ev.get("title"), ev.get("event_date"), ev.get("start_time") or "")
            if ev_id.startswith("pwa_") and key not in known_ev_keys and ev.get("event_date"):
                db.add_calendar_event(
                    title=ev.get("title") or "Evento Mobile",
                    event_date=ev.get("event_date"),
                    start_time=ev.get("start_time") or "",
                    end_time=ev.get("end_time") or "",
                    is_all_day=bool(ev.get("is_all_day")),
                    description=ev.get("description") or "Creato su FlowLocal Mobile",
                    category=ev.get("category") or "Studio",
                    subject=ev.get("subject") or "",
                    color=ev.get("color") or "#6366F1",
                    source="pwa",
                )
                known_ev_keys.add(key)
                ev_imp += 1

        existing_exams = db.get_exam_countdowns()
        known_ex_keys = {(ex.get("exam_title"), ex.get("exam_date")) for ex in existing_exams}
        for ex in (ft.get("exams") or []):
            ex_id = str(ex.get("id") or "")
            ex_key = (ex.get("exam_title"), ex.get("exam_date"))
            if ex_id.startswith("ex_") and ex_key not in known_ex_keys and ex.get("exam_date"):
                db.add_exam_countdown(
                    subject_id=None,
                    exam_title=ex.get("exam_title") or "Esame",
                    exam_date=ex.get("exam_date"),
                    cfu=int(ex.get("cfu") or 6),
                )
                known_ex_keys.add(ex_key)
                ex_imp += 1
        return (ev_imp, ex_imp)

    def import_mobile_changes(self) -> Dict[str, int]:
        """
        Importa in SQLite eventuali note, cartelle, esami ed eventi creati o modificati
        dalla PWA Mobile su FlowLocal_Sync (library.json, flowtime_state.json e account_config.json).
        """
        stats = {"notes_imported": 0, "events_imported": 0, "exams_imported": 0}
        acc_cfg_path = os.path.join(self._sync_base_dir, "account_config.json")
        if os.path.exists(acc_cfg_path):
            try:
                with open(acc_cfg_path, "r", encoding="utf-8") as f:
                    self.import_account_config(json.load(f))
            except Exception as e:
                print(f"[CloudSync] Avviso lettura account_config.json: {e}")

        lib_path = os.path.join(self.get_outbox_study_dir(), "library.json")
        if os.path.exists(lib_path):
            try:
                with open(lib_path, "r", encoding="utf-8") as f:
                    lib = json.load(f)
                stats["notes_imported"] += self.merge_library_payload(lib)
            except Exception as e:
                print(f"[CloudSync] Avviso lettura library.json per import: {e}")

        ft_path = os.path.join(self._sync_base_dir, "flowtime_state.json")
        if os.path.exists(ft_path):
            try:
                with open(ft_path, "r", encoding="utf-8") as f:
                    ft = json.load(f)
                ev_imp, ex_imp = self.merge_flowtime_payload(ft)
                stats["events_imported"] += ev_imp
                stats["exams_imported"] += ex_imp
            except Exception as e:
                print(f"[CloudSync] Avviso lettura flowtime_state.json per import: {e}")

        return stats

    def sync_with_drive(self, interactive: bool = False) -> Dict[str, Any]:
        """
        Sincronizza i 3 file unificati (account_config.json, library.json, flowtime_state.json)
        nella cartella FlowLocal_Sync su Google Drive (se autenticato) e localmente.
        """
        from core.flow_groups import flow_groups

        result: Dict[str, Any] = {
            "drive_synced": False,
            "gemini_key_imported": False,
            "theme_imported": False,
            "notes_imported": 0,
            "events_imported": 0,
            "exams_imported": 0,
        }

        service = flow_groups.get_drive_service(interactive=interactive)
        if service:
            root_id = flow_groups._ensure_drive_folder(service, "FlowLocal_Sync")
            outbox_id = flow_groups._ensure_drive_folder(service, "outbox_study", parent_id=root_id)

            # 1. account_config.json (Gemini API Key + Tema)
            cfg_item = (
                flow_groups._find_drive_item(service, "account_config.json", parent_id=root_id)
                or flow_groups._find_drive_item(service, "user_config.json", parent_id=root_id)
            )
            if cfg_item:
                try:
                    remote_cfg = json.loads(flow_groups._download_drive_text(service, cfg_item["id"]))
                    chg = self.import_account_config(remote_cfg)
                    result["gemini_key_imported"] = chg.get("gemini_key", False)
                    result["theme_imported"] = chg.get("theme", False)
                except Exception as e:
                    print(f"[CloudSync] Avviso download account_config.json: {e}")

            # 2. library.json (Note, Cartelle, Cestino - bidirezionale su updated_at UTC)
            lib_item = (
                flow_groups._find_drive_item(service, "library.json", parent_id=root_id)
                or flow_groups._find_drive_item(service, "library.json", parent_id=outbox_id)
            )
            if lib_item:
                try:
                    remote_lib = json.loads(flow_groups._download_drive_text(service, lib_item["id"]))
                    result["notes_imported"] += self.merge_library_payload(remote_lib)
                except Exception as e:
                    print(f"[CloudSync] Avviso download library.json: {e}")

            # 3. flowtime_state.json (Esami, Eventi, Sessioni Pomodoro)
            ft_item = flow_groups._find_drive_item(service, "flowtime_state.json", parent_id=root_id)
            if ft_item:
                try:
                    remote_ft = json.loads(flow_groups._download_drive_text(service, ft_item["id"]))
                    ev_i, ex_i = self.merge_flowtime_payload(remote_ft)
                    result["events_imported"] += ev_i
                    result["exams_imported"] += ex_i
                except Exception as e:
                    print(f"[CloudSync] Avviso download flowtime_state.json: {e}")

            # Rigenera snapshot locali aggiornati dopo il merge e carica i 3 file su Drive
            lib_payload = self.generate_library_json()
            ft_payload = self.export_flowtime_state()
            cfg_payload = self.export_account_config()

            flow_groups._upsert_drive_text(
                service, "account_config.json", root_id,
                json.dumps(cfg_payload, ensure_ascii=False, indent=2), mime_type="application/json",
            )
            flow_groups._upsert_drive_text(
                service, "library.json", root_id,
                json.dumps(lib_payload, ensure_ascii=False, indent=2), mime_type="application/json",
            )
            flow_groups._upsert_drive_text(
                service, "flowtime_state.json", root_id,
                json.dumps(ft_payload, ensure_ascii=False, indent=2), mime_type="application/json",
            )
            result["drive_synced"] = True
        else:
            self.generate_library_json()
            self.export_flowtime_state()
            self.export_account_config()

        return result

    def push_account_config_to_drive(self, async_bg: bool = True) -> None:
        """Salva subito account_config.json localmente e su Google Drive (in background)."""
        cfg_payload = self.export_account_config()

        def _worker():
            try:
                from core.flow_groups import flow_groups
                service = flow_groups.get_drive_service(interactive=False)
                if not service:
                    return
                root_id = flow_groups._ensure_drive_folder(service, "FlowLocal_Sync")
                flow_groups._upsert_drive_text(
                    service, "account_config.json", root_id,
                    json.dumps(cfg_payload, ensure_ascii=False, indent=2), mime_type="application/json",
                )
            except Exception as e:
                print(f"[CloudSync] Avviso push account_config.json su Drive: {e}")

        if async_bg:
            import threading
            threading.Thread(target=_worker, daemon=True).start()
        else:
            _worker()

    def export_user_state(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """Esporta materie, esami e ore di studio in FlowLocal_Sync/user_state.json per la PWA."""
        try:
            folders = db.get_folders_tree()
        except Exception:
            folders = []
        try:
            exams = db.get_exam_countdowns()
        except Exception:
            exams = []
        try:
            weekly_study = db.get_weekly_study_summary()
        except Exception:
            weekly_study = []

        payload = {
            "schema_version": 1,
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "folders": folders,
            "exams": exams,
            "weekly_study": weekly_study,
        }
        target = output_path or os.path.join(self._sync_base_dir, "user_state.json")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[CloudSync] Errore scrittura user_state.json: {e}")
        return payload

    # -------------------------------------------------------------------------
    # ELABORAZIONE SEQUENZIALE CODA (Triggerata solo manualmente)
    # -------------------------------------------------------------------------
    def process_queue_item(
        self,
        item: Dict[str, Any],
        engine,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Processa un singolo file audio dalla coda:
        1. Pipeline acustica + Qwen 2.5 7B con VRAM swap
        2. Salvataggio nel DB con tag profilo e materia
        3. Estrazione flashcard d'esame
        4. Esportazione PDF e DOCX in outbox_study/
        5. Spostamento file audio e metadati in inbox_audio/processed/
        """
        from core.ai_pipeline import LLMRefiner
        from core.exporter import export_to_pdf, export_to_docx

        audio_path = item["audio_path"]
        meta_path = item.get("meta_path")
        filename = (item.get("filename") or "file").strip()
        default_title = os.path.splitext(filename)[0]

        raw_profile = (item.get("profile") or "").strip() or "Alessandro"
        profile = "Giada" if raw_profile.lower() in ("lei", "giada") else raw_profile
        subject = (item.get("subject") or "").strip()
        raw_title = (item.get("title") or "").strip()
        custom_title = raw_title if raw_title else default_title

        def report(msg: str, pct: int = -1):
            if progress_callback:
                progress_callback(msg, pct)

        if cancel_check and cancel_check():
            raise InterruptedError("Operazione annullata.")

        # 1. Trascrizione ed elaborazione della lezione
        subj_desc = f" ({subject})" if subject else ""
        report(f"Sbobinatura di '{filename}'{subj_desc}...", 10)
        markdown, sync_data = engine.transcribe_audio_file(
            file_path=audio_path,
            subject_context=subject,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            return_sync_data=True,
        )

        if cancel_check and cancel_check():
            raise InterruptedError("Operazione annullata.")

        # 2. Salvataggio nel Database
        report("Salvataggio sbobina nel database...", 92)
        note_title = f"Sbobina: {custom_title}"

        tag_parts = ["Sbobina"]
        if subject:
            tag_parts.append(f"Materia: {subject}")
        tag_parts.extend([f"Profilo: {profile}", "Audio"])
        tags_str = ", ".join(tag_parts)

        new_note_id = db.add_study_note(
            title=note_title,
            content_markdown=markdown,
            note_type="Sbobina",
            tags=tags_str,
        )

        try:
            from core.audio_sync import archive_audio_for_note, save_note_audio_sync
            archived_audio = archive_audio_for_note(new_note_id, audio_path)
            if archived_audio:
                db.update_study_note_audio_path(new_note_id, archived_audio)
                save_note_audio_sync(new_note_id, archived_audio, (sync_data or {}).get("segments", []))
        except Exception as sync_err:
            print(f"[FlowLocal CloudSync] Avviso salvataggio audio sync: {sync_err}")

        # 3. Estrazione Flashcard d'esame
        report("Estrazione flashcard d'esame con Qwen...", 94)
        flashcard_count = 0
        fc_tag_parts = ["Flashcard"]
        if subject:
            fc_tag_parts.append(f"Materia: {subject}")
        fc_tag_parts.append(f"Profilo: {profile}")
        fc_tags_str = ", ".join(fc_tag_parts)

        try:
            cards = LLMRefiner.extract_flashcards_from_note(markdown)
            for c in (cards or []):
                q = (c.get("question") or "").strip()
                a = (c.get("answer") or "").strip()
                if q and a:
                    db.add_study_note(
                        title=f"Flashcard: {q[:60]}",
                        content_markdown=f"**Domanda:**\n{q}\n\n**Risposta:**\n{a}",
                        note_type="Flashcard",
                        tags=fc_tags_str,
                        parent_note_id=new_note_id,
                    )
                    flashcard_count += 1
        except Exception as e:
            print(f"[CloudSync] Avviso estrazione flashcard per {filename}: {e}")

        # 4. Esportazione PDF e DOCX in outbox_study/
        report("Esportazione documenti PDF e DOCX in outbox...", 96)
        outbox_dir = self.get_outbox_study_dir()
        safe_t = sanitize_filename(custom_title)
        pdf_path = os.path.join(outbox_dir, f"{safe_t}_{new_note_id}.pdf")
        docx_path = os.path.join(outbox_dir, f"{safe_t}_{new_note_id}.docx")

        note_dict = {
            "id": new_note_id,
            "title": note_title,
            "content_markdown": markdown,
            "note_type": "Sbobina",
            "tags": tags_str,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            export_to_pdf(note_dict, pdf_path)
        except Exception as e:
            print(f"[CloudSync] Errore esportazione PDF: {e}")

        try:
            export_to_docx(note_dict, docx_path)
        except Exception as e:
            print(f"[CloudSync] Errore esportazione DOCX: {e}")

        # 5. Archiviazione sicura audio e .meta.json in inbox_audio/processed/
        report("Archiviazione audio elaborato in processed/...", 98)
        processed_dir = self.get_processed_audio_dir()
        dest_audio = os.path.join(processed_dir, filename)
        if os.path.exists(dest_audio):
            base, ext = os.path.splitext(filename)
            t_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest_audio = os.path.join(processed_dir, f"{base}_{t_stamp}{ext}")

        try:
            shutil.move(audio_path, dest_audio)
        except Exception as e:
            print(f"[CloudSync] Errore spostamento audio: {e}")

        if meta_path and os.path.exists(meta_path):
            dest_meta = os.path.join(processed_dir, os.path.basename(meta_path))
            if os.path.exists(dest_meta):
                m_base, m_ext = os.path.splitext(os.path.basename(meta_path))
                t_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest_meta = os.path.join(processed_dir, f"{m_base}_{t_stamp}{m_ext}")
            try:
                shutil.move(meta_path, dest_meta)
            except Exception as e:
                print(f"[CloudSync] Errore spostamento meta: {e}")

        report("Completato con successo!", 100)
        return {
            "note_id": new_note_id,
            "title": note_title,
            "flashcard_count": flashcard_count,
            "pdf_path": pdf_path,
            "docx_path": docx_path,
        }


cloud_sync = CloudSyncManager()

