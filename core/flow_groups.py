"""
core/flow_groups.py
Engine FlowGroups per la collaborazione su appunti e sbobine universitarie.

Supporta 3 modalità con struttura identica (group_manifest.json, .lock UTC TTL 30m, .history/ 15 versioni):
  1. Creazione in-app su Google Drive API v3 con scope non-sensibile `drive.file` (+ link condivisibile opzionale)
  2. Unione a cartelle condivise (ID / Link Drive o selezionate via Google Picker nella PWA)
  3. Fallback su Cartella Locale / Google Drive Desktop (PC)

Anti-duplicati Drive API: ogni scrittura verifica prima l'esistenza tramite query
`name = '...' and '<parent_id>' in parents and trashed = false` ed esegue `files().update` invece di `files().create`.
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
    GOOGLE_DRIVE_AVAILABLE = True
except ImportError:
    GOOGLE_DRIVE_AVAILABLE = False

from core.cloud_sync import sanitize_filename
from core.paths import get_resource_path, get_user_data_path, is_frozen
from database.db_manager import db

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
LOCK_TTL_SECONDS = 1800  # 30 minuti
HISTORY_MAX_VERSIONS = 15

_USER_CREDS_PATH = str(get_user_data_path("credentials.json"))
_BUNDLED_CREDS_PATH = str(get_resource_path("credentials.json"))
DEFAULT_CREDENTIALS_PATH = _USER_CREDS_PATH if os.path.exists(_USER_CREDS_PATH) else _BUNDLED_CREDS_PATH
DEFAULT_DRIVE_TOKEN_PATH = str(get_user_data_path("token_drive.json"))

_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_iso(ts_str: str) -> Optional[datetime]:
    s = (ts_str or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def extract_drive_folder_id(folder_id_or_url: str) -> str:
    """Estrae l'ID cartella da un URL Google Drive oppure restituisce l'ID grezzo."""
    raw = (folder_id_or_url or "").strip()
    if not raw:
        return ""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    m2 = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", raw)
    if m2:
        return m2.group(1)
    return raw


def _escape_drive_q(val: str) -> str:
    return (val or "").replace("\\", "\\\\").replace("'", "\\'")


class FlowGroupsManager:
    """Gestore unificato FlowGroups (Google Drive API `drive.file` + Cartella Locale)."""

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
    ):
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self.token_path = token_path or DEFAULT_DRIVE_TOKEN_PATH
        self._service = None

    def _resolve_paths(self):
        if not os.path.exists(self.credentials_path):
            candidates = [
                str(get_user_data_path("credentials.json")),
                str(get_resource_path("credentials.json")),
            ]
            if not is_frozen():
                candidates.append(os.path.join(os.getcwd(), "credentials.json"))
            for cand in candidates:
                if os.path.exists(cand):
                    self.credentials_path = cand
                    break

    def has_token(self) -> bool:
        return os.path.exists(self.token_path)

    def has_credentials_file(self) -> bool:
        self._resolve_paths()
        return os.path.exists(self.credentials_path)

    def get_drive_service(self, interactive: bool = False):
        """Restituisce il client Google Drive API v3 autenticato con scope `drive.file`."""
        if not GOOGLE_DRIVE_AVAILABLE:
            raise RuntimeError("Librerie Google API non installate.")
        self._resolve_paths()

        creds = None
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, DRIVE_SCOPES)
            except Exception:
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(self.token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if not interactive:
                return None
            if not os.path.exists(self.credentials_path):
                raise FileNotFoundError(
                    "File credentials.json non trovato per l'autenticazione Google Drive."
                )
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def disconnect_drive(self):
        self._service = None
        if os.path.exists(self.token_path):
            try:
                os.remove(self.token_path)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # DRIVE API HELPERS (ANTI-DUPLICATI)
    # ------------------------------------------------------------------
    def _find_drive_item(
        self,
        service,
        name: str,
        parent_id: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        parts = [f"name = '{_escape_drive_q(name)}'", "trashed = false"]
        if parent_id:
            parts.append(f"'{_escape_drive_q(parent_id)}' in parents")
        if mime_type:
            parts.append(f"mimeType = '{_escape_drive_q(mime_type)}'")
        q = " and ".join(parts)
        resp = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="files(id, name, mimeType, modifiedTime)",
                pageSize=5,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files") or []
        return files[0] if files else None

    def _ensure_drive_folder(self, service, name: str, parent_id: Optional[str] = None) -> str:
        existing = self._find_drive_item(service, name, parent_id=parent_id, mime_type=_DRIVE_FOLDER_MIME)
        if existing:
            return existing["id"]
        meta: Dict[str, Any] = {"name": name, "mimeType": _DRIVE_FOLDER_MIME}
        if parent_id:
            meta["parents"] = [parent_id]
        created = (
            service.files()
            .create(body=meta, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def _upsert_drive_text(
        self,
        service,
        name: str,
        parent_id: str,
        text_content: str,
        mime_type: str = "text/markdown",
    ) -> str:
        data = (text_content or "").encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        existing = self._find_drive_item(service, name, parent_id=parent_id)
        if existing:
            updated = (
                service.files()
                .update(
                    fileId=existing["id"],
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return updated["id"]
        meta = {"name": name, "parents": [parent_id]}
        created = (
            service.files()
            .create(body=meta, media_body=media, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def _download_drive_text(self, service, file_id: str) -> str:
        req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue().decode("utf-8", errors="replace")

    def _delete_drive_item(self, service, file_id: str):
        try:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CREAZIONE & UNIONE GRUPPI
    # ------------------------------------------------------------------
    def create_group(
        self,
        group_name: str,
        subjects: Optional[List[str]] = None,
        author: str = "Studente",
        local_fallback_path: str = "",
        share_link: bool = True,
    ) -> Dict[str, Any]:
        clean_name = (group_name or "FlowGroup").strip()
        subj_list = [s.strip() for s in (subjects or ["Generale"]) if s.strip()] or ["Generale"]
        manifest = {
            "schema_version": 1,
            "group_name": clean_name,
            "created_by": author,
            "subjects": subj_list,
            "members": [author],
            "updated_at_utc": _utc_iso(),
        }

        drive_folder_id = ""
        web_link = ""
        lpath = (local_fallback_path or "").strip()

        if lpath:
            os.makedirs(lpath, exist_ok=True)
            os.makedirs(os.path.join(lpath, ".history"), exist_ok=True)
            for subj in subj_list:
                os.makedirs(os.path.join(lpath, sanitize_filename(subj)), exist_ok=True)
            manifest_path = os.path.join(lpath, "group_manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        else:
            service = self.get_drive_service(interactive=True)
            if not service:
                raise RuntimeError("Google Drive non autenticato.")
            drive_folder_id = self._ensure_drive_folder(service, f"FlowGroup - {clean_name}")
            self._ensure_drive_folder(service, ".history", parent_id=drive_folder_id)
            for subj in subj_list:
                self._ensure_drive_folder(service, sanitize_filename(subj), parent_id=drive_folder_id)
            self._upsert_drive_text(
                service,
                "group_manifest.json",
                drive_folder_id,
                json.dumps(manifest, ensure_ascii=False, indent=2),
                mime_type="application/json",
            )
            if share_link:
                try:
                    service.permissions().create(
                        fileId=drive_folder_id,
                        body={"type": "anyone", "role": "writer"},
                        fields="id",
                        supportsAllDrives=True,
                    ).execute()
                except Exception:
                    pass
            web_link = f"https://drive.google.com/drive/folders/{drive_folder_id}"

        gid = db.add_flow_group(
            group_name=clean_name,
            drive_folder_id=drive_folder_id,
            local_fallback_path=lpath,
            role="owner",
        )
        return {
            "id": gid,
            "group_name": clean_name,
            "drive_folder_id": drive_folder_id,
            "local_fallback_path": lpath,
            "share_link": web_link,
        }

    def join_group(
        self,
        folder_id_or_url: str = "",
        group_name: str = "",
        local_fallback_path: str = "",
    ) -> Dict[str, Any]:
        lpath = (local_fallback_path or "").strip()
        fid = extract_drive_folder_id(folder_id_or_url)
        resolved_name = (group_name or "").strip()

        if lpath:
            if not os.path.isdir(lpath):
                raise FileNotFoundError(f"Cartella locale non trovata: {lpath}")
            manifest_path = os.path.join(lpath, "group_manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    resolved_name = resolved_name or m.get("group_name") or os.path.basename(lpath)
                except Exception:
                    pass
            resolved_name = resolved_name or os.path.basename(lpath) or "Gruppo Locale"
        elif fid:
            service = self.get_drive_service(interactive=True)
            if service:
                try:
                    meta = (
                        service.files()
                        .get(fileId=fid, fields="id,name", supportsAllDrives=True)
                        .execute()
                    )
                    if not resolved_name:
                        resolved_name = meta.get("name", "").replace("FlowGroup - ", "").strip()
                    m_item = self._find_drive_item(service, "group_manifest.json", parent_id=fid)
                    if m_item:
                        m_data = json.loads(self._download_drive_text(service, m_item["id"]))
                        resolved_name = m_data.get("group_name") or resolved_name
                except Exception:
                    pass
            resolved_name = resolved_name or f"Gruppo {fid[:8]}"
        else:
            raise ValueError("Inserisci un link/ID cartella Google Drive o una cartella locale.")

        gid = db.add_flow_group(
            group_name=resolved_name,
            drive_folder_id=fid,
            local_fallback_path=lpath,
            role="member",
        )
        return {"id": gid, "group_name": resolved_name, "drive_folder_id": fid, "local_fallback_path": lpath}

    # ------------------------------------------------------------------
    # LOCK DISTRIBUITO UTC (TTL 30 MIN)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_lock_active(lock_data: Dict[str, Any], current_author: str) -> bool:
        if not lock_data:
            return False
        lock_author = (lock_data.get("author") or "").strip()
        if lock_author and lock_author == (current_author or "").strip():
            return False
        dt = _parse_utc_iso(lock_data.get("locked_at_utc", ""))
        if not dt:
            return False
        age = (_utc_now() - dt).total_seconds()
        return 0 <= age < LOCK_TTL_SECONDS

    def acquire_lock(
        self,
        group_rec: Dict[str, Any],
        subject: str,
        filename: str,
        author: str,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        subj_dir = sanitize_filename(subject or "Generale")
        safe_fn = filename if filename.endswith(".md") else f"{filename}.md"
        lock_name = f"{safe_fn}.lock"
        payload = {
            "author": author or "Studente",
            "locked_at_utc": _utc_iso(),
            "ttl_seconds": LOCK_TTL_SECONDS,
            "file": f"{subj_dir}/{safe_fn}",
        }

        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath:
            target_dir = os.path.join(lpath, subj_dir)
            os.makedirs(target_dir, exist_ok=True)
            lock_path = os.path.join(target_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    with open(lock_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if self._is_lock_active(existing, author):
                        return (False, f"File bloccato da {existing.get('author')} (UTC {existing.get('locked_at_utc')}).", existing)
                except Exception:
                    pass
            with open(lock_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return (True, "Lock acquisito", payload)

        fid = (group_rec.get("drive_folder_id") or "").strip()
        service = self.get_drive_service(interactive=False)
        if not service or not fid:
            return (False, "Google Drive non connesso.", None)

        subj_id = self._ensure_drive_folder(service, subj_dir, parent_id=fid)
        existing_item = self._find_drive_item(service, lock_name, parent_id=subj_id)
        if existing_item:
            try:
                existing = json.loads(self._download_drive_text(service, existing_item["id"]))
                if self._is_lock_active(existing, author):
                    return (False, f"File bloccato da {existing.get('author')} (UTC {existing.get('locked_at_utc')}).", existing)
            except Exception:
                pass
        self._upsert_drive_text(
            service,
            lock_name,
            subj_id,
            json.dumps(payload, ensure_ascii=False, indent=2),
            mime_type="application/json",
        )
        return (True, "Lock acquisito", payload)

    def release_lock(self, group_rec: Dict[str, Any], subject: str, filename: str):
        subj_dir = sanitize_filename(subject or "Generale")
        safe_fn = filename if filename.endswith(".md") else f"{filename}.md"
        lock_name = f"{safe_fn}.lock"

        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath:
            lock_path = os.path.join(lpath, subj_dir, lock_name)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
            return

        fid = (group_rec.get("drive_folder_id") or "").strip()
        service = self.get_drive_service(interactive=False)
        if not service or not fid:
            return
        subj_item = self._find_drive_item(service, subj_dir, parent_id=fid, mime_type=_DRIVE_FOLDER_MIME)
        if not subj_item:
            return
        lock_item = self._find_drive_item(service, lock_name, parent_id=subj_item["id"])
        if lock_item:
            self._delete_drive_item(service, lock_item["id"])

    # ------------------------------------------------------------------
    # PUBBLICAZIONE, STORICO (.history/ 15 VERSIONI) & DOWNLOAD
    # ------------------------------------------------------------------
    def _prune_local_history(self, history_dir: str, stem: str):
        if not os.path.isdir(history_dir):
            return
        prefix = f"{stem}_"
        matches = sorted(
            [f for f in os.listdir(history_dir) if f.startswith(prefix) and f.endswith(".md")],
            reverse=True,
        )
        for old_f in matches[HISTORY_MAX_VERSIONS:]:
            try:
                os.remove(os.path.join(history_dir, old_f))
            except Exception:
                pass

    def _prune_drive_history(self, service, history_folder_id: str, stem: str):
        q = (
            f"name contains '{_escape_drive_q(stem)}_' and '{_escape_drive_q(history_folder_id)}' in parents "
            "and trashed = false"
        )
        resp = (
            service.files()
            .list(
                q=q,
                orderBy="name desc",
                fields="files(id,name)",
                pageSize=50,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = [f for f in (resp.get("files") or []) if f["name"].startswith(f"{stem}_") and f["name"].endswith(".md")]
        for old_f in files[HISTORY_MAX_VERSIONS:]:
            self._delete_drive_item(service, old_f["id"])

    def publish_note(
        self,
        group_rec: Dict[str, Any],
        subject: str,
        title: str,
        markdown_content: str,
        author: str = "Studente",
    ) -> Dict[str, Any]:
        subj_dir = sanitize_filename(subject or "Generale")
        stem = sanitize_filename(title or "Appunto")
        md_name = f"{stem}.md"

        ok, msg, lock_info = self.acquire_lock(group_rec, subj_dir, md_name, author)
        if not ok:
            raise RuntimeError(msg)

        ts_tag = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        lpath = (group_rec.get("local_fallback_path") or "").strip()

        try:
            if lpath:
                target_dir = os.path.join(lpath, subj_dir)
                hist_dir = os.path.join(lpath, ".history")
                os.makedirs(target_dir, exist_ok=True)
                os.makedirs(hist_dir, exist_ok=True)
                target_file = os.path.join(target_dir, md_name)
                if os.path.exists(target_file):
                    with open(target_file, "r", encoding="utf-8") as f:
                        prev_txt = f.read()
                    if prev_txt != markdown_content:
                        hist_file = os.path.join(hist_dir, f"{stem}_{ts_tag}.md")
                        with open(hist_file, "w", encoding="utf-8") as hf:
                            hf.write(prev_txt)
                        self._prune_local_history(hist_dir, stem)
                with open(target_file, "w", encoding="utf-8") as f:
                    f.write(markdown_content)

                manifest_path = os.path.join(lpath, "group_manifest.json")
                manifest = {"schema_version": 1, "group_name": group_rec.get("group_name"), "subjects": [subj_dir]}
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as mf:
                            manifest = json.load(mf)
                    except Exception:
                        pass
                subjs = set(manifest.get("subjects") or [])
                subjs.add(subj_dir)
                manifest["subjects"] = sorted(subjs)
                manifest["updated_at_utc"] = _utc_iso()
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(manifest, mf, ensure_ascii=False, indent=2)
            else:
                fid = (group_rec.get("drive_folder_id") or "").strip()
                service = self.get_drive_service(interactive=True)
                if not service or not fid:
                    raise RuntimeError("Google Drive non connesso.")
                subj_id = self._ensure_drive_folder(service, subj_dir, parent_id=fid)
                hist_id = self._ensure_drive_folder(service, ".history", parent_id=fid)

                existing_md = self._find_drive_item(service, md_name, parent_id=subj_id)
                if existing_md:
                    prev_txt = self._download_drive_text(service, existing_md["id"])
                    if prev_txt != markdown_content:
                        self._upsert_drive_text(service, f"{stem}_{ts_tag}.md", hist_id, prev_txt)
                        self._prune_drive_history(service, hist_id, stem)

                self._upsert_drive_text(service, md_name, subj_id, markdown_content)

                m_item = self._find_drive_item(service, "group_manifest.json", parent_id=fid)
                manifest = {"schema_version": 1, "group_name": group_rec.get("group_name"), "subjects": [subj_dir]}
                if m_item:
                    try:
                        manifest = json.loads(self._download_drive_text(service, m_item["id"]))
                    except Exception:
                        pass
                subjs = set(manifest.get("subjects") or [])
                subjs.add(subj_dir)
                manifest["subjects"] = sorted(subjs)
                manifest["updated_at_utc"] = _utc_iso()
                self._upsert_drive_text(
                    service,
                    "group_manifest.json",
                    fid,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    mime_type="application/json",
                )
        finally:
            self.release_lock(group_rec, subj_dir, md_name)

        if group_rec.get("id"):
            db.update_flow_group_sync(int(group_rec["id"]))
        return {"subject": subj_dir, "filename": md_name, "updated_at_utc": _utc_iso()}

    def list_group_notes(self, group_rec: Dict[str, Any], current_author: str = "") -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath:
            if not os.path.isdir(lpath):
                return []
            for entry in sorted(os.listdir(lpath)):
                if entry.startswith("."):
                    continue
                full_subj = os.path.join(lpath, entry)
                if not os.path.isdir(full_subj):
                    continue
                files = os.listdir(full_subj)
                locks = {}
                for fn in files:
                    if fn.endswith(".md.lock"):
                        try:
                            with open(os.path.join(full_subj, fn), "r", encoding="utf-8") as lf:
                                ld = json.load(lf)
                            if self._is_lock_active(ld, current_author):
                                locks[fn[:-5]] = ld
                        except Exception:
                            pass
                for fn in sorted(files):
                    if fn.endswith(".md"):
                        results.append({
                            "subject": entry,
                            "filename": fn,
                            "title": os.path.splitext(fn)[0].replace("_", " "),
                            "local_path": os.path.join(full_subj, fn),
                            "file_id": "",
                            "lock_info": locks.get(fn),
                        })
            return results

        fid = (group_rec.get("drive_folder_id") or "").strip()
        service = self.get_drive_service(interactive=False)
        if not service or not fid:
            return []
        folders_resp = (
            service.files()
            .list(
                q=f"'{_escape_drive_q(fid)}' in parents and mimeType = '{_DRIVE_FOLDER_MIME}' and trashed = false",
                fields="files(id,name)",
                pageSize=50,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for folder in folders_resp.get("files") or []:
            fname = folder["name"]
            if fname.startswith("."):
                continue
            files_resp = (
                service.files()
                .list(
                    q=f"'{_escape_drive_q(folder['id'])}' in parents and trashed = false",
                    fields="files(id,name,modifiedTime)",
                    pageSize=100,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            f_list = files_resp.get("files") or []
            locks: Dict[str, Any] = {}
            for item in f_list:
                if item["name"].endswith(".md.lock"):
                    try:
                        ld = json.loads(self._download_drive_text(service, item["id"]))
                        if self._is_lock_active(ld, current_author):
                            locks[item["name"][:-5]] = ld
                    except Exception:
                        pass
            for item in f_list:
                if item["name"].endswith(".md"):
                    results.append({
                        "subject": fname,
                        "filename": item["name"],
                        "title": os.path.splitext(item["name"])[0].replace("_", " "),
                        "local_path": "",
                        "file_id": item["id"],
                        "lock_info": locks.get(item["name"]),
                    })
        return results

    def download_note(self, group_rec: Dict[str, Any], note_item: Dict[str, Any]) -> Tuple[str, str]:
        title = note_item.get("title") or "Nota Gruppo"
        local_p = note_item.get("local_path") or ""
        if local_p and os.path.exists(local_p):
            with open(local_p, "r", encoding="utf-8") as f:
                return (title, f.read())
        file_id = note_item.get("file_id") or ""
        service = self.get_drive_service(interactive=True)
        if not service or not file_id:
            raise RuntimeError("Impossibile scaricare la nota dal gruppo.")
        return (title, self._download_drive_text(service, file_id))


flow_groups = FlowGroupsManager()
