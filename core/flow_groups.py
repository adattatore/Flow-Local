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
import urllib.request
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

GOOGLE_ACCOUNT_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]
DRIVE_SCOPES = GOOGLE_ACCOUNT_SCOPES
LOCK_TTL_SECONDS = 1800  # 30 minuti
HISTORY_MAX_VERSIONS = 15

DEFAULT_GROUP_COLOR = "#6366F1"
GROUP_COLOR_PALETTE = [
    "#6366F1",
    "#3B82F6",
    "#10B981",
    "#F59E0B",
    "#EF4444",
    "#8B5CF6",
    "#EC4899",
    "#14B8A6",
    "#F97316",
    "#06B6D4",
]
DEFAULT_GROUP_PERMISSIONS: Dict[str, bool] = {
    "users_can_delete_own": True,
    "users_can_delete_any": False,
    "users_can_set_maintenance": True,
    "users_can_manage_bots": False,
}

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_hex_color(color: Optional[str], default: str = DEFAULT_GROUP_COLOR) -> str:
    """Valida e normalizza un colore esadecimale #RRGGBB."""
    c = (color or "").strip()
    if _HEX_COLOR_RE.match(c):
        return c.upper()
    d = (default or DEFAULT_GROUP_COLOR).strip()
    return d.upper() if _HEX_COLOR_RE.match(d) else DEFAULT_GROUP_COLOR


def normalize_role(role: Optional[str]) -> str:
    """Normalizza i ruoli legacy ('owner'/'member') nei ruoli v3 ('admin'/'user')."""
    r = (role or "").strip().lower()
    if r in ("admin", "owner", "amministratore"):
        return "admin"
    return "user"


def normalize_group_manifest(
    raw: Optional[Dict[str, Any]],
    group_name: str = "Gruppo",
    fallback_color: str = DEFAULT_GROUP_COLOR,
) -> Dict[str, Any]:
    """Normalizza un group_manifest.json (v1/v2/v3) allo schema v3 preservando campi extra."""
    data: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    clean_name = (data.get("group_name") or group_name or "Gruppo").strip() or "Gruppo"
    clean_color = normalize_hex_color(data.get("color"), fallback_color)

    created_by = str(data.get("created_by") or "").strip()
    created_by_email = str(data.get("created_by_email") or "").strip()

    normalized_members: List[Dict[str, str]] = []
    seen_emails: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}

    def _add_or_merge_member(name: str, email: str, role: str, joined_at: str = ""):
        n_clean = (name or "").strip()
        e_clean = (email or "").strip()
        if not n_clean and not e_clean:
            return
        r_norm = normalize_role(role)
        e_key = e_clean.lower()
        n_key = n_clean.lower()

        idx: Optional[int] = None
        if e_key and e_key in seen_emails:
            idx = seen_emails[e_key]
        elif n_key and n_key in seen_names:
            idx = seen_names[n_key]

        if idx is not None:
            existing = normalized_members[idx]
            if not existing["name"] and n_clean:
                existing["name"] = n_clean
            if not existing["email"] and e_clean:
                existing["email"] = e_clean
            if r_norm == "admin":
                existing["role"] = "admin"
            if not existing["joined_at_utc"] and joined_at:
                existing["joined_at_utc"] = joined_at
            if existing["email"]:
                seen_emails[existing["email"].lower()] = idx
            if existing["name"]:
                seen_names[existing["name"].lower()] = idx
            return

        new_idx = len(normalized_members)
        entry = {
            "name": n_clean or e_clean,
            "email": e_clean,
            "role": r_norm,
            "joined_at_utc": (joined_at or "").strip(),
        }
        normalized_members.append(entry)
        if e_key:
            seen_emails[e_key] = new_idx
        if n_key:
            seen_names[n_key] = new_idx

    raw_members = data.get("members") or []
    if isinstance(raw_members, list):
        for item in raw_members:
            if isinstance(item, dict):
                _add_or_merge_member(
                    str(item.get("name") or ""),
                    str(item.get("email") or ""),
                    str(item.get("role") or "user"),
                    str(item.get("joined_at_utc") or ""),
                )
            elif isinstance(item, str) and item.strip():
                is_creator = bool(created_by and item.strip().lower() == created_by.lower())
                _add_or_merge_member(item.strip(), "", "admin" if is_creator else "user", "")

    if created_by or created_by_email:
        _add_or_merge_member(created_by or created_by_email, created_by_email, "admin", str(data.get("updated_at_utc") or ""))

    raw_perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    perms = dict(DEFAULT_GROUP_PERMISSIONS)
    for k, default_v in DEFAULT_GROUP_PERMISSIONS.items():
        if k in raw_perms:
            perms[k] = bool(raw_perms[k])
        else:
            perms[k] = bool(default_v)

    subjs = data.get("subjects")
    if not isinstance(subjs, list) or not subjs:
        subjs = ["Generale"]
    notes_meta = data.get("notes_meta") if isinstance(data.get("notes_meta"), dict) else {}

    data["schema_version"] = 3
    data["group_name"] = clean_name
    data["color"] = clean_color
    data["created_by"] = created_by
    data["created_by_email"] = created_by_email
    data["subjects"] = list(subjs)
    data["members"] = normalized_members
    data["permissions"] = perms
    data["notes_meta"] = notes_meta
    data["updated_at_utc"] = str(data.get("updated_at_utc") or "")
    return data

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


_AUTHOR_META_KEYS = (
    "created_by_name",
    "created_by_email",
    "created_by_avatar",
    "last_edited_by_name",
    "last_edited_by_email",
    "last_edited_at_utc",
)


def parse_md_frontmatter(raw_text: str) -> Tuple[Dict[str, str], str]:
    """Estrae eventuale frontmatter YAML (--- ... ---) da un file .md e restituisce (meta, body)."""
    text = raw_text or ""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return ({}, text)
    lines = text.splitlines()
    end_idx = -1
    for i in range(1, min(len(lines), 40)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return ({}, text)
    meta: Dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if key:
                meta[key] = val
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
    return (meta, body)


def build_md_with_frontmatter(body: str, meta: Dict[str, str]) -> str:
    """Compone un file .md con frontmatter YAML contenente i metadati autore/provenienza."""
    clean_meta = {k: str(meta.get(k) or "").replace("\n", " ").strip() for k in _AUTHOR_META_KEYS}
    for extra_k in ("archived_by_name", "archived_by_email", "archived_at_utc"):
        if meta.get(extra_k):
            clean_meta[extra_k] = str(meta[extra_k]).replace("\n", " ").strip()
    fm_lines = ["---"]
    for k, v in clean_meta.items():
        if v:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    return "\n".join(fm_lines) + (body or "").lstrip("\r\n")


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

    def fetch_and_save_userinfo(self, creds) -> Dict[str, str]:
        """Recupera sub, name, email e picture da Google UserInfo e li salva localmente."""
        if not creds or not getattr(creds, "token", None):
            return db.get_google_account()
        try:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {creds.token}"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            user_id = str(data.get("sub") or "").strip()
            display_name = str(data.get("name") or data.get("given_name") or "").strip()
            email = str(data.get("email") or "").strip()
            avatar_url = str(data.get("picture") or "").strip()
            if user_id or email or display_name:
                db.set_google_account(user_id, display_name, email, avatar_url)
            return db.get_google_account()
        except Exception as e:
            print(f"[GoogleAuth] Avviso recupero userinfo: {e}")
            return db.get_google_account()

    def get_drive_service(self, interactive: bool = False):
        """Restituisce il client Google Drive API v3 autenticato con scope unificato (openid, profile, email, drive.file)."""
        if not GOOGLE_DRIVE_AVAILABLE:
            raise RuntimeError("Librerie Google API non installate.")
        self._resolve_paths()

        creds = None
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path)
            except Exception:
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(self.token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except Exception:
                creds = None

        # Se mancano gli scope identità e l'utente ha chiesto login interattivo, riesegui il consenso
        if creds and creds.valid and interactive:
            existing_scopes = set(getattr(creds, "scopes", None) or [])
            if existing_scopes and not {"https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"}.issubset(existing_scopes):
                if not db.get_google_account().get("email"):
                    creds = None

        if not creds or not creds.valid:
            if not interactive:
                return None
            if not os.path.exists(self.credentials_path):
                raise FileNotFoundError(
                    "File credentials.json non trovato per l'autenticazione Google Drive."
                )
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, GOOGLE_ACCOUNT_SCOPES)
            creds = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            self.fetch_and_save_userinfo(creds)
        else:
            acc = db.get_google_account()
            if not acc.get("email") and getattr(creds, "token", None):
                self.fetch_and_save_userinfo(creds)

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def disconnect_drive(self):
        self._service = None
        if os.path.exists(self.token_path):
            try:
                os.remove(self.token_path)
            except Exception:
                pass
        db.clear_google_account()

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
    @staticmethod
    def _current_author_identity(fallback_author: str = "Studente") -> Dict[str, str]:
        acc = db.get_google_account()
        name = acc.get("display_name") or (fallback_author or "").strip() or "Studente"
        return {
            "name": name,
            "email": acc.get("email") or "",
            "avatar": acc.get("avatar_url") or "",
        }

    def create_group(
        self,
        group_name: str,
        subjects: Optional[List[str]] = None,
        author: str = "Studente",
        local_fallback_path: str = "",
        share_link: bool = True,
        color: str = DEFAULT_GROUP_COLOR,
    ) -> Dict[str, Any]:
        clean_name = (group_name or "FlowGroup").strip()
        clean_color = normalize_hex_color(color, DEFAULT_GROUP_COLOR)
        subj_list = [s.strip() for s in (subjects or ["Generale"]) if s.strip()] or ["Generale"]
        ident = self._current_author_identity(author)
        now_iso = _utc_iso()
        manifest = normalize_group_manifest(
            {
                "schema_version": 3,
                "group_name": clean_name,
                "color": clean_color,
                "created_by": ident["name"],
                "created_by_email": ident["email"],
                "subjects": subj_list,
                "members": [
                    {
                        "name": ident["name"],
                        "email": ident["email"],
                        "role": "admin",
                        "joined_at_utc": now_iso,
                    }
                ],
                "permissions": dict(DEFAULT_GROUP_PERMISSIONS),
                "notes_meta": {},
                "updated_at_utc": now_iso,
            },
            group_name=clean_name,
            fallback_color=clean_color,
        )

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
            role="admin",
            color=clean_color,
        )
        return {
            "id": gid,
            "group_name": clean_name,
            "drive_folder_id": drive_folder_id,
            "local_fallback_path": lpath,
            "share_link": web_link,
            "color": clean_color,
            "role": "admin",
        }

    def join_group(
        self,
        folder_id_or_url: str = "",
        group_name: str = "",
        local_fallback_path: str = "",
        author: str = "Studente",
    ) -> Dict[str, Any]:
        lpath = (local_fallback_path or "").strip()
        fid = extract_drive_folder_id(folder_id_or_url)
        resolved_name = (group_name or "").strip()
        resolved_color = DEFAULT_GROUP_COLOR
        resolved_role = "user"
        ident = self._current_author_identity(author)
        loaded_manifest: Optional[Dict[str, Any]] = None

        if lpath:
            if not os.path.isdir(lpath):
                raise FileNotFoundError(f"Cartella locale non trovata: {lpath}")
            manifest_path = os.path.join(lpath, "group_manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    loaded_manifest = normalize_group_manifest(m, group_name=resolved_name or os.path.basename(lpath))
                    resolved_name = resolved_name or loaded_manifest.get("group_name") or os.path.basename(lpath)
                    resolved_color = loaded_manifest["color"]
                    resolved_role = self.get_member_role(
                        loaded_manifest,
                        email=ident["email"],
                        name=ident["name"],
                        local_fallback_role="user",
                    )
                    self._ensure_member_in_manifest(loaded_manifest, ident, default_role=resolved_role)
                    loaded_manifest["updated_at_utc"] = _utc_iso()
                    with open(manifest_path, "w", encoding="utf-8") as wf:
                        json.dump(loaded_manifest, wf, ensure_ascii=False, indent=2)
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
                        loaded_manifest = normalize_group_manifest(m_data, group_name=resolved_name or "Gruppo")
                        resolved_name = resolved_name or loaded_manifest.get("group_name") or resolved_name
                        resolved_color = loaded_manifest["color"]
                        resolved_role = self.get_member_role(
                            loaded_manifest,
                            email=ident["email"],
                            name=ident["name"],
                            local_fallback_role="user",
                        )
                except Exception:
                    pass
            resolved_name = resolved_name or f"Gruppo {fid[:8]}"
        else:
            raise ValueError("Inserisci un link/ID cartella Google Drive o una cartella locale.")

        gid = db.add_flow_group(
            group_name=resolved_name,
            drive_folder_id=fid,
            local_fallback_path=lpath,
            role=resolved_role,
            color=resolved_color,
        )
        return {
            "id": gid,
            "group_name": resolved_name,
            "drive_folder_id": fid,
            "local_fallback_path": lpath,
            "color": resolved_color,
            "role": resolved_role,
        }

    # ------------------------------------------------------------------
    # LOCK DISTRIBUITO UTC (TTL 30 MIN)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_lock_valid(lock_data: Dict[str, Any]) -> bool:
        if not lock_data:
            return False
        dt = _parse_utc_iso(lock_data.get("locked_at_utc", ""))
        if not dt:
            return False
        age = (_utc_now() - dt).total_seconds()
        return 0 <= age < LOCK_TTL_SECONDS

    @classmethod
    def _is_lock_active(cls, lock_data: Dict[str, Any], current_author: str, current_email: str = "") -> bool:
        if not cls._is_lock_valid(lock_data):
            return False
        lock_email = (lock_data.get("author_email") or "").strip().lower()
        cur_email = (current_email or "").strip().lower()
        if lock_email and cur_email and lock_email == cur_email:
            return False
        lock_author = (lock_data.get("author_name") or lock_data.get("author") or "").strip()
        if lock_author and lock_author == (current_author or "").strip():
            return False
        return True

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
        ident = self._current_author_identity(author)
        payload = {
            "author": ident["name"],
            "author_name": ident["name"],
            "author_email": ident["email"],
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
                    if self._is_lock_active(existing, ident["name"], ident["email"]):
                        return (False, f"File bloccato da {existing.get('author_name') or existing.get('author')} (UTC {existing.get('locked_at_utc')}).", existing)
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
                if self._is_lock_active(existing, ident["name"], ident["email"]):
                    return (False, f"File bloccato da {existing.get('author_name') or existing.get('author')} (UTC {existing.get('locked_at_utc')}).", existing)
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
        release_lock: bool = True,
        created_by_meta: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        subj_dir = sanitize_filename(subject or "Generale")
        stem = sanitize_filename(title or "Appunto")
        md_name = f"{stem}.md"
        rel_key = f"{subj_dir}/{md_name}"
        ident = self._current_author_identity(author)
        now_iso = _utc_iso()

        ok, msg, lock_info = self.acquire_lock(group_rec, subj_dir, md_name, ident["name"])
        if not ok:
            raise RuntimeError(msg)

        ts_tag = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        lpath = (group_rec.get("local_fallback_path") or "").strip()

        # Rimuovi eventuale frontmatter duplicato dal corpo markdown
        _, clean_body = parse_md_frontmatter(markdown_content or "")
        final_meta: Dict[str, str] = {}

        try:
            if lpath:
                target_dir = os.path.join(lpath, subj_dir)
                hist_dir = os.path.join(lpath, ".history")
                os.makedirs(target_dir, exist_ok=True)
                os.makedirs(hist_dir, exist_ok=True)
                target_file = os.path.join(target_dir, md_name)

                manifest_path = os.path.join(lpath, "group_manifest.json")
                raw_manifest: Dict[str, Any] = {
                    "schema_version": 3,
                    "group_name": group_rec.get("group_name"),
                    "color": group_rec.get("color") or DEFAULT_GROUP_COLOR,
                    "subjects": [subj_dir],
                    "notes_meta": {},
                }
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as mf:
                            raw_manifest = json.load(mf)
                    except Exception:
                        pass
                manifest = normalize_group_manifest(
                    raw_manifest,
                    group_name=group_rec.get("group_name") or "Gruppo",
                    fallback_color=group_rec.get("color") or DEFAULT_GROUP_COLOR,
                )

                prev_meta: Dict[str, str] = {}
                if os.path.exists(target_file):
                    with open(target_file, "r", encoding="utf-8") as f:
                        prev_raw = f.read()
                    prev_meta, prev_body = parse_md_frontmatter(prev_raw)
                    if prev_body.strip() != clean_body.strip():
                        hist_meta = dict(prev_meta)
                        hist_meta["archived_by_name"] = ident["name"]
                        hist_meta["archived_by_email"] = ident["email"]
                        hist_meta["archived_at_utc"] = now_iso
                        hist_file = os.path.join(hist_dir, f"{stem}_{ts_tag}.md")
                        with open(hist_file, "w", encoding="utf-8") as hf:
                            hf.write(build_md_with_frontmatter(prev_body, hist_meta))
                        self._prune_local_history(hist_dir, stem)

                existing_manifest_meta = (manifest.get("notes_meta") or {}).get(rel_key) or {}
                cb = created_by_meta or {}
                final_meta = {
                    "created_by_name": prev_meta.get("created_by_name") or existing_manifest_meta.get("created_by_name") or cb.get("created_by_name") or ident["name"],
                    "created_by_email": prev_meta.get("created_by_email") or existing_manifest_meta.get("created_by_email") or cb.get("created_by_email") or ident["email"],
                    "created_by_avatar": prev_meta.get("created_by_avatar") or existing_manifest_meta.get("created_by_avatar") or cb.get("created_by_avatar") or ident["avatar"],
                    "last_edited_by_name": ident["name"],
                    "last_edited_by_email": ident["email"],
                    "last_edited_at_utc": now_iso,
                }
                with open(target_file, "w", encoding="utf-8") as f:
                    f.write(build_md_with_frontmatter(clean_body, final_meta))

                subjs = set(manifest.get("subjects") or [])
                subjs.add(subj_dir)
                manifest["subjects"] = sorted(subjs)
                self._ensure_member_in_manifest(
                    manifest,
                    ident,
                    default_role=normalize_role(group_rec.get("role") or "user"),
                )
                notes_meta = manifest.get("notes_meta") if isinstance(manifest.get("notes_meta"), dict) else {}
                notes_meta[rel_key] = final_meta
                manifest["notes_meta"] = notes_meta
                manifest["updated_at_utc"] = now_iso
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(manifest, mf, ensure_ascii=False, indent=2)
            else:
                fid = (group_rec.get("drive_folder_id") or "").strip()
                service = self.get_drive_service(interactive=True)
                if not service or not fid:
                    raise RuntimeError("Google Drive non connesso.")
                subj_id = self._ensure_drive_folder(service, subj_dir, parent_id=fid)
                hist_id = self._ensure_drive_folder(service, ".history", parent_id=fid)

                m_item = self._find_drive_item(service, "group_manifest.json", parent_id=fid)
                raw_manifest = {
                    "schema_version": 3,
                    "group_name": group_rec.get("group_name"),
                    "color": group_rec.get("color") or DEFAULT_GROUP_COLOR,
                    "subjects": [subj_dir],
                    "notes_meta": {},
                }
                if m_item:
                    try:
                        raw_manifest = json.loads(self._download_drive_text(service, m_item["id"]))
                    except Exception:
                        pass
                manifest = normalize_group_manifest(
                    raw_manifest,
                    group_name=group_rec.get("group_name") or "Gruppo",
                    fallback_color=group_rec.get("color") or DEFAULT_GROUP_COLOR,
                )

                prev_meta = {}
                existing_md = self._find_drive_item(service, md_name, parent_id=subj_id)
                if existing_md:
                    prev_raw = self._download_drive_text(service, existing_md["id"])
                    prev_meta, prev_body = parse_md_frontmatter(prev_raw)
                    if prev_body.strip() != clean_body.strip():
                        hist_meta = dict(prev_meta)
                        hist_meta["archived_by_name"] = ident["name"]
                        hist_meta["archived_by_email"] = ident["email"]
                        hist_meta["archived_at_utc"] = now_iso
                        self._upsert_drive_text(
                            service,
                            f"{stem}_{ts_tag}.md",
                            hist_id,
                            build_md_with_frontmatter(prev_body, hist_meta),
                        )
                        self._prune_drive_history(service, hist_id, stem)

                existing_manifest_meta = (manifest.get("notes_meta") or {}).get(rel_key) or {}
                cb = created_by_meta or {}
                final_meta = {
                    "created_by_name": prev_meta.get("created_by_name") or existing_manifest_meta.get("created_by_name") or cb.get("created_by_name") or ident["name"],
                    "created_by_email": prev_meta.get("created_by_email") or existing_manifest_meta.get("created_by_email") or cb.get("created_by_email") or ident["email"],
                    "created_by_avatar": prev_meta.get("created_by_avatar") or existing_manifest_meta.get("created_by_avatar") or cb.get("created_by_avatar") or ident["avatar"],
                    "last_edited_by_name": ident["name"],
                    "last_edited_by_email": ident["email"],
                    "last_edited_at_utc": now_iso,
                }
                self._upsert_drive_text(service, md_name, subj_id, build_md_with_frontmatter(clean_body, final_meta))

                subjs = set(manifest.get("subjects") or [])
                subjs.add(subj_dir)
                manifest["subjects"] = sorted(subjs)
                self._ensure_member_in_manifest(
                    manifest,
                    ident,
                    default_role=normalize_role(group_rec.get("role") or "user"),
                )
                notes_meta = manifest.get("notes_meta") if isinstance(manifest.get("notes_meta"), dict) else {}
                notes_meta[rel_key] = final_meta
                manifest["notes_meta"] = notes_meta
                manifest["updated_at_utc"] = now_iso
                self._upsert_drive_text(
                    service,
                    "group_manifest.json",
                    fid,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    mime_type="application/json",
                )
        finally:
            if release_lock:
                self.release_lock(group_rec, subj_dir, md_name)

        if group_rec.get("id"):
            db.update_flow_group_sync(int(group_rec["id"]))
        return {"subject": subj_dir, "filename": md_name, "updated_at_utc": now_iso, **final_meta}

    @staticmethod
    def _ensure_member_in_manifest(
        manifest: Dict[str, Any],
        ident: Dict[str, str],
        default_role: str = "user",
    ) -> Dict[str, Any]:
        """Aggiunge o aggiorna un membro nel manifest senza declassare un admin esistente."""
        name = (ident.get("name") or "").strip()
        email = (ident.get("email") or "").strip()
        if not name and not email:
            return manifest
        members = manifest.get("members")
        if not isinstance(members, list):
            members = []
            manifest["members"] = members

        e_low = email.lower()
        n_low = name.lower()
        for m in members:
            if not isinstance(m, dict):
                continue
            m_email = (m.get("email") or "").strip().lower()
            m_name = (m.get("name") or "").strip().lower()
            if (e_low and m_email and e_low == m_email) or (n_low and m_name and n_low == m_name):
                if not m.get("name") and name:
                    m["name"] = name
                if not m.get("email") and email:
                    m["email"] = email
                if normalize_role(m.get("role")) != "admin" and normalize_role(default_role) == "admin":
                    m["role"] = "admin"
                return manifest

        members.append({
            "name": name or email,
            "email": email,
            "role": normalize_role(default_role),
            "joined_at_utc": _utc_iso(),
        })
        return manifest

    def _save_group_manifest(
        self,
        group_rec: Dict[str, Any],
        manifest: Dict[str, Any],
        interactive: bool = False,
    ) -> None:
        lpath = (group_rec.get("local_fallback_path") or "").strip()
        payload = json.dumps(manifest, ensure_ascii=False, indent=2)
        if lpath:
            os.makedirs(lpath, exist_ok=True)
            with open(os.path.join(lpath, "group_manifest.json"), "w", encoding="utf-8") as mf:
                mf.write(payload)
            return

        fid = (group_rec.get("drive_folder_id") or "").strip()
        service = self.get_drive_service(interactive=interactive)
        if not service or not fid:
            raise RuntimeError("Google Drive non connesso.")
        self._upsert_drive_text(
            service,
            "group_manifest.json",
            fid,
            payload,
            mime_type="application/json",
        )

    def get_member_role(
        self,
        manifest: Dict[str, Any],
        email: str = "",
        name: str = "",
        local_fallback_role: str = "user",
    ) -> str:
        """Risolve il ruolo ('admin' | 'user') di un utente rispetto al manifest."""
        e_low = (email or "").strip().lower()
        n_low = (name or "").strip().lower()
        c_email = (manifest.get("created_by_email") or "").strip().lower()
        c_name = (manifest.get("created_by") or "").strip().lower()
        if (e_low and c_email and e_low == c_email) or (n_low and c_name and n_low == c_name):
            return "admin"

        members = manifest.get("members") or []
        if e_low:
            for m in members:
                if isinstance(m, dict) and (m.get("email") or "").strip().lower() == e_low:
                    return normalize_role(m.get("role"))
        if n_low:
            for m in members:
                if isinstance(m, dict):
                    if (m.get("name") or "").strip().lower() == n_low:
                        return normalize_role(m.get("role"))
                elif isinstance(m, str) and m.strip().lower() == n_low:
                    return "admin" if (c_name and n_low == c_name) else "user"

        return normalize_role(local_fallback_role)

    def is_admin(
        self,
        group_rec: Dict[str, Any],
        manifest: Optional[Dict[str, Any]] = None,
        author: str = "",
    ) -> bool:
        mf = manifest if manifest is not None else self.read_group_manifest(group_rec)
        ident = self._current_author_identity(author)
        role = self.get_member_role(
            mf,
            email=ident["email"],
            name=ident["name"],
            local_fallback_role=group_rec.get("role") or "user",
        )
        return role == "admin"

    def can_delete_note(
        self,
        group_rec: Dict[str, Any],
        note_meta: Optional[Dict[str, Any]] = None,
        manifest: Optional[Dict[str, Any]] = None,
        author: str = "",
    ) -> bool:
        mf = manifest if manifest is not None else self.read_group_manifest(group_rec)
        if self.is_admin(group_rec, mf, author=author):
            return True
        perms = mf.get("permissions") if isinstance(mf.get("permissions"), dict) else DEFAULT_GROUP_PERMISSIONS
        if perms.get("users_can_delete_any"):
            return True
        if perms.get("users_can_delete_own") and isinstance(note_meta, dict):
            ident = self._current_author_identity(author)
            cur_email = (ident.get("email") or "").strip().lower()
            cur_name = (ident.get("name") or "").strip().lower()
            cb_email = (note_meta.get("created_by_email") or "").strip().lower()
            cb_name = (note_meta.get("created_by_name") or "").strip().lower()
            if cur_email and cb_email and cur_email == cb_email:
                return True
            if cur_name and cb_name and cur_name == cb_name:
                return True
        return False

    def update_group_color(
        self,
        group_rec: Dict[str, Any],
        color_hex: str,
        author: str = "",
    ) -> str:
        clean_color = normalize_hex_color(color_hex)
        manifest = self.read_group_manifest(group_rec)
        if not self.is_admin(group_rec, manifest, author=author):
            raise PermissionError("Solo un Amministratore può cambiare il colore del gruppo.")
        manifest["color"] = clean_color
        manifest["updated_at_utc"] = _utc_iso()
        self._save_group_manifest(group_rec, manifest, interactive=True)
        if group_rec.get("id"):
            db.update_flow_group_color(int(group_rec["id"]), clean_color)
        return clean_color

    def set_member_role(
        self,
        group_rec: Dict[str, Any],
        target_identifier: str,
        new_role: str,
        author: str = "",
    ) -> Dict[str, Any]:
        manifest = self.read_group_manifest(group_rec)
        if not self.is_admin(group_rec, manifest, author=author):
            raise PermissionError("Solo un Amministratore può modificare i ruoli dei membri.")
        target_key = (target_identifier or "").strip().lower()
        if not target_key:
            raise ValueError("Identificativo membro non valido.")
        target_role = normalize_role(new_role)
        members = manifest.get("members") or []
        found_idx: Optional[int] = None
        for idx, m in enumerate(members):
            if not isinstance(m, dict):
                continue
            m_email = (m.get("email") or "").strip().lower()
            m_name = (m.get("name") or "").strip().lower()
            if target_key in (m_email, m_name):
                found_idx = idx
                break
        if found_idx is None:
            raise ValueError(f"Membro non trovato nel gruppo: {target_identifier}")

        current_role = normalize_role(members[found_idx].get("role"))
        if current_role == "admin" and target_role != "admin":
            admin_count = sum(
                1 for m in members if isinstance(m, dict) and normalize_role(m.get("role")) == "admin"
            )
            if admin_count <= 1:
                raise ValueError("Il gruppo deve avere almeno un Amministratore.")

        members[found_idx]["role"] = target_role
        manifest["updated_at_utc"] = _utc_iso()
        self._save_group_manifest(group_rec, manifest, interactive=True)

        ident = self._current_author_identity(author)
        cur_keys = {k for k in (ident["email"].lower(), ident["name"].lower()) if k}
        if target_key in cur_keys and group_rec.get("id"):
            db.update_flow_group_role(int(group_rec["id"]), target_role)
        return manifest

    def update_group_permissions(
        self,
        group_rec: Dict[str, Any],
        permissions: Dict[str, bool],
        author: str = "",
    ) -> Dict[str, bool]:
        manifest = self.read_group_manifest(group_rec)
        if not self.is_admin(group_rec, manifest, author=author):
            raise PermissionError("Solo un Amministratore può modificare i permessi del gruppo.")
        cur_perms = dict(DEFAULT_GROUP_PERMISSIONS)
        if isinstance(manifest.get("permissions"), dict):
            for k in DEFAULT_GROUP_PERMISSIONS:
                if k in manifest["permissions"]:
                    cur_perms[k] = bool(manifest["permissions"][k])
        for k in DEFAULT_GROUP_PERMISSIONS:
            if k in permissions:
                cur_perms[k] = bool(permissions[k])
        manifest["permissions"] = cur_perms
        manifest["updated_at_utc"] = _utc_iso()
        self._save_group_manifest(group_rec, manifest, interactive=True)
        return dict(cur_perms)

    def read_group_manifest(self, group_rec: Dict[str, Any]) -> Dict[str, Any]:
        """Legge group_manifest.json dal gruppo (Locale o Drive) e lo normalizza allo schema v3."""
        fallback_color = normalize_hex_color(group_rec.get("color"), DEFAULT_GROUP_COLOR)
        raw_manifest: Dict[str, Any] = {
            "schema_version": 3,
            "group_name": group_rec.get("group_name") or "Gruppo",
            "color": fallback_color,
            "subjects": ["Generale"],
            "members": [],
            "permissions": dict(DEFAULT_GROUP_PERMISSIONS),
            "notes_meta": {},
            "updated_at_utc": "",
        }
        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath:
            manifest_path = os.path.join(lpath, "group_manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        raw_manifest.update(data)
                except Exception:
                    pass
        else:
            fid = (group_rec.get("drive_folder_id") or "").strip()
            service = self.get_drive_service(interactive=False)
            if service and fid:
                try:
                    m_item = self._find_drive_item(service, "group_manifest.json", parent_id=fid)
                    if m_item:
                        data = json.loads(self._download_drive_text(service, m_item["id"]))
                        if isinstance(data, dict):
                            raw_manifest.update(data)
                except Exception:
                    pass

        manifest = normalize_group_manifest(
            raw_manifest,
            group_name=group_rec.get("group_name") or "Gruppo",
            fallback_color=fallback_color,
        )
        if group_rec.get("id"):
            try:
                gid = int(group_rec["id"])
                if normalize_hex_color(group_rec.get("color")) != manifest["color"]:
                    db.update_flow_group_color(gid, manifest["color"])
                ident = self._current_author_identity()
                resolved_role = self.get_member_role(
                    manifest,
                    email=ident["email"],
                    name=ident["name"],
                    local_fallback_role=group_rec.get("role") or "user",
                )
                if normalize_role(group_rec.get("role")) != resolved_role:
                    db.update_flow_group_role(gid, resolved_role)
            except Exception:
                pass
        return manifest

    def create_group_subject(self, group_rec: Dict[str, Any], subject_name: str) -> str:
        """Crea una nuova cartella materia/canale nel gruppo e aggiorna group_manifest.json."""
        subj_dir = sanitize_filename((subject_name or "").strip() or "Generale")
        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath:
            os.makedirs(os.path.join(lpath, subj_dir), exist_ok=True)
            manifest = self.read_group_manifest(group_rec)
            subjs = set(manifest.get("subjects") or [])
            subjs.add(subj_dir)
            manifest["subjects"] = sorted(subjs)
            manifest["updated_at_utc"] = _utc_iso()
            with open(os.path.join(lpath, "group_manifest.json"), "w", encoding="utf-8") as mf:
                json.dump(manifest, mf, ensure_ascii=False, indent=2)
            return subj_dir

        fid = (group_rec.get("drive_folder_id") or "").strip()
        service = self.get_drive_service(interactive=True)
        if not service or not fid:
            raise RuntimeError("Google Drive non connesso.")
        self._ensure_drive_folder(service, subj_dir, parent_id=fid)
        manifest = self.read_group_manifest(group_rec)
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
        return subj_dir

    def list_group_notes(self, group_rec: Dict[str, Any], current_author: str = "") -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        author_norm = (current_author or "").strip()
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
                locks: Dict[str, Dict[str, Any]] = {}
                for fn in files:
                    if fn.endswith(".md.lock"):
                        try:
                            with open(os.path.join(full_subj, fn), "r", encoding="utf-8") as lf:
                                ld = json.load(lf)
                            if self._is_lock_valid(ld):
                                locks[fn[:-5]] = ld
                        except Exception:
                            pass
                for fn in sorted(files):
                    if fn.endswith(".md"):
                        full_md = os.path.join(full_subj, fn)
                        try:
                            mtime = datetime.fromtimestamp(os.path.getmtime(full_md), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except Exception:
                            mtime = ""
                        lock_d = locks.get(fn)
                        lock_auth = (lock_d.get("author") or "").strip() if lock_d else ""
                        results.append({
                            "subject": entry,
                            "filename": fn,
                            "rel_path": f"{entry}/{fn}",
                            "title": os.path.splitext(fn)[0].replace("_", " "),
                            "local_path": full_md,
                            "file_id": "",
                            "modified_at": mtime,
                            "lock_info": lock_d,
                            "lock_author": lock_auth,
                            "locked_by_other": bool(lock_d and lock_auth != author_norm),
                            "locked_by_me": bool(lock_d and lock_auth == author_norm),
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
                        if self._is_lock_valid(ld):
                            locks[item["name"][:-5]] = ld
                    except Exception:
                        pass
            for item in f_list:
                if item["name"].endswith(".md"):
                    lock_d = locks.get(item["name"])
                    lock_auth = (lock_d.get("author") or "").strip() if lock_d else ""
                    results.append({
                        "subject": fname,
                        "filename": item["name"],
                        "rel_path": f"{fname}/{item['name']}",
                        "title": os.path.splitext(item["name"])[0].replace("_", " "),
                        "local_path": "",
                        "file_id": item["id"],
                        "modified_at": item.get("modifiedTime") or "",
                        "lock_info": lock_d,
                        "lock_author": lock_auth,
                        "locked_by_other": bool(lock_d and lock_auth != author_norm),
                        "locked_by_me": bool(lock_d and lock_auth == author_norm),
                    })
        return results

    def get_group_board_data(self, group_rec: Dict[str, Any], current_author: str = "") -> Dict[str, Any]:
        """Restituisce riepilogo completo per la Bacheca & Stato Sbobine del gruppo."""
        manifest = self.read_group_manifest(group_rec)
        notes = self.list_group_notes(group_rec, current_author=current_author)
        notes_meta = manifest.get("notes_meta") if isinstance(manifest.get("notes_meta"), dict) else {}
        for n in notes:
            rel = n.get("rel_path") or f"{n.get('subject')}/{n.get('filename')}"
            m = notes_meta.get(rel) or {}
            # Per note locali, leggi anche eventuale frontmatter se manca nel manifest
            if not m and n.get("local_path") and os.path.exists(n["local_path"]):
                try:
                    with open(n["local_path"], "r", encoding="utf-8") as lf:
                        head = lf.read(1200)
                    fm, _ = parse_md_frontmatter(head)
                    m = fm
                except Exception:
                    pass
            for k in _AUTHOR_META_KEYS:
                if m.get(k) and not n.get(k):
                    n[k] = m[k]
        subj_set = set(manifest.get("subjects") or [])
        for n in notes:
            if n.get("subject"):
                subj_set.add(n["subject"])
        lpath = (group_rec.get("local_fallback_path") or "").strip()
        if lpath and os.path.isdir(lpath):
            for entry in os.listdir(lpath):
                if not entry.startswith(".") and os.path.isdir(os.path.join(lpath, entry)):
                    subj_set.add(entry)
        active_locks = [n for n in notes if n.get("lock_info")]
        ident = self._current_author_identity(current_author)
        cur_role = self.get_member_role(
            manifest,
            email=ident["email"],
            name=ident["name"],
            local_fallback_role=group_rec.get("role") or "user",
        )
        members_detailed = manifest.get("members") or []
        members_names = [
            (m.get("name") or m.get("email") or "Membro") if isinstance(m, dict) else str(m)
            for m in members_detailed
        ]
        return {
            "group_id": group_rec.get("id"),
            "group_name": group_rec.get("group_name") or manifest.get("group_name") or "Gruppo",
            "color": manifest.get("color") or DEFAULT_GROUP_COLOR,
            "mode": "local" if lpath else "drive",
            "source": lpath or (group_rec.get("drive_folder_id") or ""),
            "members": members_names,
            "members_detailed": members_detailed,
            "permissions": dict(manifest.get("permissions") or DEFAULT_GROUP_PERMISSIONS),
            "current_user_role": cur_role,
            "is_admin": (cur_role == "admin"),
            "subjects": sorted(subj_set) or ["Generale"],
            "notes": notes,
            "active_locks": active_locks,
            "updated_at_utc": manifest.get("updated_at_utc") or "",
        }

    def download_note(self, group_rec: Dict[str, Any], note_item: Dict[str, Any]) -> Tuple[str, str]:
        title = note_item.get("title") or "Nota Gruppo"
        local_p = note_item.get("local_path") or ""
        raw_md = ""
        if local_p and os.path.exists(local_p):
            with open(local_p, "r", encoding="utf-8") as f:
                raw_md = f.read()
        else:
            file_id = note_item.get("file_id") or ""
            service = self.get_drive_service(interactive=True)
            if not service or not file_id:
                raise RuntimeError("Impossibile scaricare la nota dal gruppo.")
            raw_md = self._download_drive_text(service, file_id)
        fm, body = parse_md_frontmatter(raw_md)
        for k in _AUTHOR_META_KEYS:
            if fm.get(k):
                note_item[k] = fm[k]
        return (title, body)


flow_groups = FlowGroupsManager()
