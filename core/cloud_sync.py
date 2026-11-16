"""
Cloud Sync Manager per FlowLocal (Coda Cloud Controllata & Anti-Lag Gaming).
Gestisce la sincronizzazione locale con Google Drive Desktop (FlowLocal_Sync),
le code inbox/outbox, l'elaborazione manuale sequenziale e la generazione
dell'indice completo library.json per la PWA Mobile.
"""

import json
import os
import re
import shutil
import string
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable

from core.paths import get_user_data_path
from database.db_manager import db


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
                    "tags": clean_tags_str,
                    "summary": summary_text,
                    "pdf_filename": expected_pdf if has_pdf else None,
                    "docx_filename": expected_docx if has_docx else None,
                    "created_at": created_at,
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

        library_payload = {
            "library_version": "1.0",
            "generated_at": datetime.now().isoformat(),
            "sync_folder": self._sync_base_dir,
            "profiles": sorted(list(profiles_set)),
            "subjects": sorted(list(subjects_set)),
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
        return library_payload

    def export_user_state(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """Esporta materie, esami e ore di studio in FlowLocal_Sync/user_state.json per la PWA."""
        from datetime import timezone
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

