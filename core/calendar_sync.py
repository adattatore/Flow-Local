"""
core/calendar_sync.py
Modulo calendario Local-First (SQLite) con sincronizzazione bidirezionale opzionale su Google Calendar.

Funzionalità:
- Letture, ricerche e briefing istantanei (< 1 ms) su SQLite (`calendar_events` in `flow_hub.db`)
- Creazione, modifica e cancellazione locale immediata anche offline o senza credenziali Google
- Sincronizzazione asincrona in background verso/da Google Calendar API v3 (OAuth 2.0)
- Esposizione automatica dei collegamenti temporali con le note di studio (`calendar_note_links`)
"""

import os
import re
import time
import calendar
import threading
import datetime
from typing import Optional, List, Dict, Any, Tuple, Callable

try:
    from zoneinfo import ZoneInfo

    def get_tz(name: str):
        try:
            return ZoneInfo(name)
        except Exception:
            return datetime.timezone(datetime.timedelta(hours=1))
except Exception:
    def get_tz(name: str):
        return datetime.timezone(datetime.timedelta(hours=1))

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

from database.db_manager import db

SCOPES = ['https://www.googleapis.com/auth/calendar']
DEFAULT_TIMEZONE = 'Europe/Rome'

# Risoluzione percorsi file credenziali e token
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CREDENTIALS_PATH = os.path.join(PROJECT_ROOT, "credentials.json")
DEFAULT_TOKEN_PATH = os.path.join(PROJECT_ROOT, "token_calendar.json")

ITALIAN_WEEKDAYS = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
ITALIAN_MONTHS = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]

CATEGORY_COLORS = {
    "Personale": "#6366F1",
    "Università": "#38BDF8",
    "Esame": "#EF4444",
    "Studio": "#10B981",
    "Lavoro": "#F59E0B",
}


class GoogleCalendarManager:
    """
    Manager Local-First per FlowTime e Whis con sync opzionale bidirezionale verso Google Calendar API v3.
    Tutte le operazioni di lettura/ricerca leggono da SQLite in < 1 ms senza bloccarsi sulla rete.
    """

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self.token_path = token_path or DEFAULT_TOKEN_PATH
        self.timezone = timezone
        self._tz = get_tz(self.timezone)
        self._service = None
        self._sync_lock = threading.Lock()
        self._is_syncing = False
        self._last_sync_ts = 0.0
        self._last_sync_error: str = ""
        self._sync_listeners: List[Callable[[bool, str], None]] = []

    def is_local_ready(self) -> bool:
        """Il calendario locale SQLite è sempre pronto e attivo senza configurazioni esterne."""
        return True

    def add_sync_listener(self, callback: Callable[[bool, str], None]):
        """Registra un callback invocato al termine di una sincronizzazione con Google."""
        if callback not in self._sync_listeners:
            self._sync_listeners.append(callback)

    def _notify_sync_listeners(self, ok: bool, msg: str):
        for cb in list(self._sync_listeners):
            try:
                cb(ok, msg)
            except Exception:
                pass

    def _resolve_paths(self):
        """Assicura che vengano controllati sia il percorso root del progetto che la directory corrente."""
        if not os.path.exists(self.credentials_path):
            cwd_creds = os.path.join(os.getcwd(), "credentials.json")
            if os.path.exists(cwd_creds):
                self.credentials_path = cwd_creds

        if not os.path.exists(self.token_path):
            cwd_token = os.path.join(os.getcwd(), "token_calendar.json")
            if os.path.exists(cwd_token):
                self.token_path = cwd_token

    def has_credentials_file(self) -> bool:
        """Verifica se il file credentials.json di Google OAuth è presente."""
        self._resolve_paths()
        return os.path.exists(self.credentials_path)

    def has_token_file(self) -> bool:
        """Verifica se il file token_calendar.json è presente."""
        self._resolve_paths()
        return os.path.exists(self.token_path)

    def is_connected(self) -> bool:
        """Restituisce True se esiste un token valido o rinnovabile per Google Calendar."""
        if not GOOGLE_LIBS_AVAILABLE:
            return False
        try:
            creds = self.get_credentials(interactive=False)
            return creds is not None and creds.valid
        except Exception:
            return False

    def get_credentials(self, interactive: bool = True) -> Optional[Any]:
        """
        Recupera o rinnova le credenziali OAuth 2.0.
        Se interactive=True e le credenziali non sono valide, avvia il flusso da browser locale.
        """
        if not GOOGLE_LIBS_AVAILABLE:
            raise RuntimeError("Librerie Google Calendar API non installate nell'ambiente.")

        self._resolve_paths()
        creds = None

        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            except Exception as e:
                print(f"[GoogleCalendar] Errore lettura token esistente: {e}")
                creds = None

        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(self.token_path, "w", encoding="utf-8") as token_file:
                        token_file.write(creds.to_json())
                except Exception as e:
                    print(f"[GoogleCalendar] Impossibile rinnovare il token: {e}")
                    creds = None
            else:
                creds = None

        if not creds or not creds.valid:
            if not interactive:
                return None

            if not os.path.exists(self.credentials_path):
                raise FileNotFoundError(
                    f"File '{os.path.basename(self.credentials_path)}' non trovato in {os.path.dirname(self.credentials_path)}. "
                    "Scarica il file OAuth client secrets dalla Google Cloud Console (Desktop App) e salvalo come 'credentials.json'."
                )

            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())

        return creds

    def get_service(self, interactive: bool = True):
        """Ottiene l'istanza del client Google Calendar API v3."""
        creds = self.get_credentials(interactive=interactive)
        if not creds:
            raise PermissionError("Autenticazione Google Calendar non disponibile o non autorizzata.")
        return build('calendar', 'v3', credentials=creds)

    def authenticate(self, interactive: bool = True) -> Tuple[bool, str]:
        """
        Tenta l'autenticazione, verifica l'accesso effettivo e avvia subito la prima sincronizzazione.
        """
        try:
            if not GOOGLE_LIBS_AVAILABLE:
                return False, "Dipendenze Google API mancanti."
            if not self.has_credentials_file() and not self.has_token_file():
                return False, "File 'credentials.json' mancante nella directory del progetto."

            service = self.get_service(interactive=interactive)
            service.calendarList().get(calendarId='primary').execute()
            self._service = service
            self.sync_in_background(force=True)
            return True, "Google Calendar connesso e sincronizzato con FlowTime."
        except Exception as e:
            return False, str(e)

    def get_calendar_list(self, service=None) -> List[Dict[str, Any]]:
        """Restituisce l'elenco dei calendari disponibili nell'account Google collegato."""
        srv = service or self.get_service(interactive=False)
        try:
            cals_result = srv.calendarList().list(minAccessRole='reader').execute()
            return cals_result.get('items', [])
        except Exception as e:
            print(f"[GoogleCalendar] Avviso recupero lista calendari: {e}")
            return [{"id": "primary", "summary": "Personale", "primary": True, "selected": True}]

    # -------------------------------------------------------------------------
    # FORMATTAZIONE & NORMALIZZAZIONE DATE/ORARI
    # -------------------------------------------------------------------------
    @staticmethod
    def _parse_date_and_times(
        date_str: str,
        start_time_str: str = "09:00",
        duration_minutes: int = 60,
    ) -> Tuple[datetime.date, bool, str, str]:
        date_clean = (date_str or "").strip()
        try:
            d_parts = [int(p) for p in date_clean.split("-")]
            event_date = datetime.date(d_parts[0], d_parts[1], d_parts[2])
        except Exception as e:
            raise ValueError(f"Formato data '{date_str}' non valido. Usare 'YYYY-MM-DD'. Dettaglio: {e}")

        is_all_day = (
            not start_time_str
            or start_time_str.strip().lower() in ("all_day", "allday", "tutto il giorno", "tutto_il_giorno", "giornata")
        )
        if is_all_day:
            return event_date, True, "", ""

        time_clean = start_time_str.strip()
        if ":" in time_clean:
            parsed_t = datetime.datetime.strptime(time_clean[:5], "%H:%M")
            start_hour, start_min = parsed_t.hour, parsed_t.minute
        else:
            try:
                start_hour = int(time_clean)
                start_min = 0
            except Exception:
                start_hour, start_min = 9, 0

        start_dt = datetime.datetime(event_date.year, event_date.month, event_date.day, start_hour, start_min)
        dur = max(5, int(duration_minutes or 60))
        end_dt = start_dt + datetime.timedelta(minutes=dur)
        return event_date, False, start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")

    def _format_db_event(self, row: Dict[str, Any], include_links: bool = True) -> Dict[str, Any]:
        """Converte una riga SQLite `calendar_events` nel dizionario unificato per Whis e UI."""
        ev_date = row.get("event_date") or ""
        try:
            p = [int(x) for x in ev_date.split("-")]
            d_obj = datetime.date(p[0], p[1], p[2])
            dow = ITALIAN_WEEKDAYS[d_obj.weekday()]
            day_str = f"{dow} {d_obj.day:02d}/{d_obj.month:02d}"
        except Exception:
            day_str = ev_date

        item_type = (row.get("item_type") or "event").strip().lower()
        if item_type != "todo":
            item_type = "event"
        is_completed = bool(row.get("is_completed"))
        recurrence = (row.get("recurrence") or "none").strip() or "none"

        is_all_day = bool(row.get("is_all_day"))
        start_t = (row.get("start_time") or "").strip()
        end_t = (row.get("end_time") or "").strip()
        if item_type == "todo":
            time_str = "Completato" if is_completed else "Da fare"
            sort_key = f"{ev_date}T00:00:00"
        elif is_all_day:
            time_str = "Tutto il giorno"
            sort_key = f"{ev_date}T00:00:00"
        else:
            time_str = f"{start_t} - {end_t}" if (start_t and end_t) else (start_t or "09:00")
            sort_key = f"{ev_date}T{start_t or '09:00'}:00"

        summary = row.get("title") or "(Senza Titolo)"
        location = (row.get("location") or "").strip()
        category = (row.get("category") or "Personale").strip()
        subject = (row.get("subject") or "").strip()
        cal_display = (row.get("google_calendar_name") or category or "Personale").strip()
        linked_notes = db.get_notes_for_event(row["id"]) if include_links else []

        loc_tag = f" ({location})" if location else ""
        cal_tag = f" [{cal_display}]" if cal_display and cal_display != "Personale" else ""
        notes_tag = f" [Note collegate: {', '.join(n['title'] for n in linked_notes[:2])}]" if linked_notes else ""

        return {
            "id": str(row["id"]),
            "local_id": row["id"],
            "google_event_id": row.get("google_event_id") or "",
            "calendar_id": row.get("google_calendar_id") or "primary",
            "calendar_name": cal_display,
            "summary": summary,
            "description": row.get("description") or "",
            "location": location,
            "category": category,
            "subject": subject,
            "color": row.get("color") or CATEGORY_COLORS.get(category, "#6366F1"),
            "event_date": ev_date,
            "date": day_str,
            "time": time_str,
            "start_time": start_t,
            "end_time": end_t,
            "sort_key": sort_key,
            "is_all_day": is_all_day,
            "item_type": item_type,
            "is_completed": is_completed,
            "recurrence": recurrence,
            "source": row.get("source") or "local",
            "sync_status": row.get("sync_status") or "local_only",
            "linked_notes": linked_notes,
            "formatted": f"• {day_str}, {time_str}: {summary}{loc_tag}{cal_tag}{notes_tag}",
            "raw": row,
        }

    @staticmethod
    def _expand_recurrence_dates(base_date: datetime.date, recurrence: str) -> List[datetime.date]:
        rec = (recurrence or "none").strip().lower()
        dates = [base_date]
        if rec == "daily":
            for i in range(1, 7):
                dates.append(base_date + datetime.timedelta(days=i))
        elif rec == "weekly":
            for i in range(1, 8):
                dates.append(base_date + datetime.timedelta(days=7 * i))
        elif rec == "monthly":
            for m_off in range(1, 6):
                total_m = (base_date.month - 1) + m_off
                y = base_date.year + total_m // 12
                m = (total_m % 12) + 1
                max_d = calendar.monthrange(y, m)[1]
                dates.append(datetime.date(y, m, min(base_date.day, max_d)))
        return dates

    # -------------------------------------------------------------------------
    # API LOCAL-FIRST (< 1 ms) CON PUSH/SYNC ASINCRONO GOOGLE
    # -------------------------------------------------------------------------
    def create_event(
        self,
        summary: str,
        date_str: str,
        start_time_str: str = "09:00",
        duration_minutes: int = 60,
        description: str = "",
        location: str = "",
        category: str = "Personale",
        subject: str = "",
        color: Optional[str] = None,
        push_to_google: bool = True,
        item_type: str = "event",
        is_completed: bool = False,
        recurrence: str = "none",
    ) -> Dict[str, Any]:
        """
        Crea istantaneamente un evento o To-Do nel calendario locale SQLite e, se Google Calendar
        è configurato (per gli eventi), avvia il push asincrono in background senza bloccare Whis/UI.
        """
        summary_clean = (summary or "").strip() or "Nuovo Appuntamento"
        itype = "todo" if (item_type or "").strip().lower() == "todo" else "event"
        rec_clean = (recurrence or "none").strip().lower() or "none"

        if itype == "todo":
            parts = [int(p) for p in (date_str or "").strip().split("-")]
            event_date = datetime.date(parts[0], parts[1], parts[2])
            is_all_day, start_t, end_t = True, "", ""
            sync_status = "local_only"
        else:
            event_date, is_all_day, start_t, end_t = self._parse_date_and_times(
                date_str=date_str,
                start_time_str=start_time_str,
                duration_minutes=duration_minutes,
            )
            sync_status = "pending_push" if (push_to_google and self.has_token_file()) else "local_only"

        cat_clean = (category or "Personale").strip()
        color_clean = color or CATEGORY_COLORS.get(cat_clean, "#6366F1")

        occ_dates = self._expand_recurrence_dates(event_date, rec_clean)
        first_local_id = None
        for idx, occ_d in enumerate(occ_dates):
            lid = db.add_calendar_event(
                title=summary_clean,
                event_date=occ_d.isoformat(),
                start_time=start_t,
                end_time=end_t,
                is_all_day=is_all_day,
                description=description or ("To-Do creato in FlowTime" if itype == "todo" else "Creato in FlowTime"),
                location=location,
                category=cat_clean,
                subject=subject,
                color=color_clean,
                source="local",
                google_calendar_id="primary",
                google_calendar_name=cat_clean,
                sync_status=sync_status,
                item_type=itype,
                is_completed=bool(is_completed) if idx == 0 else False,
                recurrence=rec_clean,
            )
            if first_local_id is None:
                first_local_id = lid

        if itype != "todo" and push_to_google and self.has_token_file():
            self.sync_in_background(force=True)

        row = db.get_calendar_event_by_id(first_local_id) if first_local_id else None
        return self._format_db_event(row) if row else {"id": str(first_local_id or ""), "summary": summary_clean}

    def update_event(
        self,
        event_id: Any,
        summary: Optional[str] = None,
        date_str: Optional[str] = None,
        start_time_str: Optional[str] = None,
        end_time_str: Optional[str] = None,
        is_all_day: Optional[bool] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        category: Optional[str] = None,
        subject: Optional[str] = None,
        color: Optional[str] = None,
        item_type: Optional[str] = None,
        is_completed: Optional[bool] = None,
        recurrence: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Aggiorna un evento locale e pianifica il sync verso Google se collegato."""
        try:
            local_id = int(event_id)
        except Exception:
            return None

        existing = db.get_calendar_event_by_id(local_id)
        if not existing:
            return None

        fields: Dict[str, Any] = {}
        if summary is not None:
            fields["title"] = summary.strip() or existing["title"]
        if date_str is not None:
            fields["event_date"] = date_str.strip()
        if item_type is not None:
            itype = "todo" if item_type.strip().lower() == "todo" else "event"
            fields["item_type"] = itype
            if itype == "todo":
                is_all_day = True
        if is_completed is not None:
            fields["is_completed"] = bool(is_completed)
        if recurrence is not None:
            fields["recurrence"] = recurrence.strip().lower() or "none"
        if is_all_day is not None:
            fields["is_all_day"] = bool(is_all_day)
            if is_all_day:
                fields["start_time"] = ""
                fields["end_time"] = ""
        if start_time_str is not None and not fields.get("is_all_day", False):
            fields["start_time"] = start_time_str.strip()
        if end_time_str is not None and not fields.get("is_all_day", False):
            fields["end_time"] = end_time_str.strip()
        if description is not None:
            fields["description"] = description.strip()
        if location is not None:
            fields["location"] = location.strip()
        if category is not None:
            fields["category"] = category.strip()
            if color is None:
                fields["color"] = CATEGORY_COLORS.get(fields["category"], existing.get("color", "#6366F1"))
        if color is not None:
            fields["color"] = color.strip() or existing.get("color", "#6366F1")
        if subject is not None:
            fields["subject"] = subject.strip()

        eff_type = fields.get("item_type", existing.get("item_type") or "event")
        if eff_type != "todo" and self.has_token_file() and (
            existing.get("google_event_id") or existing.get("sync_status") == "pending_push"
        ):
            fields["sync_status"] = "pending_push"

        db.update_calendar_event(local_id, **fields)
        if fields.get("sync_status") == "pending_push" and self.has_token_file():
            self.sync_in_background(force=True)

        updated = db.get_calendar_event_by_id(local_id)
        return self._format_db_event(updated) if updated else None

    def toggle_todo(self, event_id: Any) -> bool:
        """Inverte lo stato completato di un To-Do locale."""
        try:
            local_id = int(event_id)
        except Exception:
            return False
        return db.toggle_calendar_todo(local_id)

    def get_upcoming_events(
        self,
        days: int = 7,
        target_date: Optional[str] = None,
        calendar_id: Optional[str] = None,
        include_all_selected: bool = True,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Recupera gli impegni istantaneamente (< 1 ms) dal database locale SQLite.
        Se Google Calendar ha un token configurato, avvia in background l'eventuale refresh asincrono.
        """
        if target_date:
            try:
                parts = [int(p) for p in target_date.strip().split("-")]
                d = datetime.date(parts[0], parts[1], parts[2])
                start_d = d.isoformat()
                end_d = d.isoformat()
            except Exception as e:
                raise ValueError(f"Data target '{target_date}' non valida (YYYY-MM-DD): {e}")
        else:
            today = datetime.date.today()
            start_d = today.isoformat()
            end_d = (today + datetime.timedelta(days=max(1, int(days) - 1))).isoformat()

        rows = db.get_calendar_events(start_date=start_d, end_date=end_d, category=category)
        if calendar_id:
            rows = [r for r in rows if (r.get("google_calendar_id") or "primary") == calendar_id]

        # Refresh asincrono non bloccante in background (ogni 120s)
        if self.has_token_file() and (time.time() - self._last_sync_ts) > 120.0:
            self.sync_in_background(force=False)

        formatted = [self._format_db_event(r) for r in rows]
        formatted.sort(key=lambda ev: ev.get("sort_key", ""))
        return formatted

    def delete_event(self, event_id: Any, calendar_id: str = "primary") -> bool:
        """
        Elimina istantaneamente un evento dal calendario locale SQLite.
        Se era sincronizzato con Google Calendar, lo marca `pending_delete` e lo rimuove in background.
        """
        local_id: Optional[int] = None
        if isinstance(event_id, int) or (isinstance(event_id, str) and event_id.strip().isdigit()):
            local_id = int(event_id)
        else:
            # Ricerca eventuale per google_event_id
            all_rows = db.search_calendar_events(query="", limit=500)
            for r in all_rows:
                if r.get("google_event_id") == str(event_id):
                    local_id = r["id"]
                    break

        if local_id is None:
            return False

        ok = db.delete_calendar_event(local_id, hard_delete=False)
        if ok and self.has_token_file():
            self.sync_in_background(force=True)
        return ok

    def find_events_by_query(
        self,
        query: str,
        target_date: Optional[str] = None,
        days: int = 14,
    ) -> List[Dict[str, Any]]:
        """
        Cerca eventi locali per parola chiave (titolo, materia, luogo) oppure per orario (es. '16:00', '16').
        Eseguita al 100% in SQLite (< 1 ms).
        """
        events = self.get_upcoming_events(days=days, target_date=target_date)
        if not query or not query.strip():
            return events

        q_clean = query.strip().lower()
        time_tokens = []
        if match_hour := re.search(r'\b(?:alle\s+)?(\d{1,2})(?::(\d{2}))?\b', q_clean):
            h = int(match_hour.group(1))
            m = int(match_hour.group(2)) if match_hour.group(2) else 0
            time_tokens.append(f"{h:02d}:{m:02d}")
            if h <= 12:
                time_tokens.append(f"{h + 12:02d}:{m:02d}")

        matches: List[Dict[str, Any]] = []
        for ev in events:
            haystack = " ".join([
                ev.get("summary") or "",
                ev.get("description") or "",
                ev.get("location") or "",
                ev.get("subject") or "",
                ev.get("category") or "",
            ]).lower()
            time_str = (ev.get("time") or "").lower()

            if q_clean in haystack:
                matches.append(ev)
                continue

            for tt in time_tokens:
                if tt in time_str:
                    matches.append(ev)
                    break

        return matches

    def get_day_briefing(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Restituisce il resoconto completo e strutturato degli impegni e delle note per la giornata."""
        d_str = target_date or datetime.date.today().isoformat()
        events = self.get_upcoming_events(days=1, target_date=d_str)
        notes = db.get_notes_for_date(d_str)
        return {
            "date": d_str,
            "count": len(events),
            "events": events,
            "notes": notes,
        }

    # -------------------------------------------------------------------------
    # SINCRONIZZAZIONE BIDIREZIONALE CON GOOGLE CALENDAR (BACKGROUND)
    # -------------------------------------------------------------------------
    def _build_google_event_body(self, row: Dict[str, Any]) -> Dict[str, Any]:
        ev_date = datetime.date.fromisoformat(row["event_date"])
        summary = row.get("title") or "Nuovo Appuntamento"
        desc = row.get("description") or "Creato da FlowTime"
        loc = row.get("location") or ""

        if row.get("is_all_day"):
            next_day = ev_date + datetime.timedelta(days=1)
            body = {
                'summary': summary,
                'description': desc,
                'start': {'date': ev_date.isoformat(), 'timeZone': self.timezone},
                'end': {'date': next_day.isoformat(), 'timeZone': self.timezone},
            }
        else:
            st = (row.get("start_time") or "09:00").strip()
            et = (row.get("end_time") or "").strip()
            sh, sm = [int(x) for x in st.split(":")[:2]]
            start_dt = datetime.datetime(ev_date.year, ev_date.month, ev_date.day, sh, sm, tzinfo=self._tz)
            if et and ":" in et:
                eh, em = [int(x) for x in et.split(":")[:2]]
                end_dt = datetime.datetime(ev_date.year, ev_date.month, ev_date.day, eh, em, tzinfo=self._tz)
                if end_dt <= start_dt:
                    end_dt = start_dt + datetime.timedelta(hours=1)
            else:
                end_dt = start_dt + datetime.timedelta(hours=1)

            body = {
                'summary': summary,
                'description': desc,
                'start': {'dateTime': start_dt.isoformat(), 'timeZone': self.timezone},
                'end': {'dateTime': end_dt.isoformat(), 'timeZone': self.timezone},
            }

        if loc:
            body['location'] = loc
        return body

    def sync_with_google(self, days_back: int = 14, days_forward: int = 90) -> Tuple[bool, str]:
        """
        Esegue una sincronizzazione bidirezionale completa tra SQLite locale e Google Calendar API v3:
        1. Invia a Google gli eventi locali `pending_push` ed elimina quelli `pending_delete`.
        2. Importa/aggiorna in SQLite gli eventi di tutti i calendari Google selezionati.
        """
        if not GOOGLE_LIBS_AVAILABLE:
            return False, "Librerie Google API non disponibili."
        if not self.has_token_file():
            return False, "Google Calendar non connesso (modalità solo locale)."

        if not self._sync_lock.acquire(blocking=False):
            return False, "Sincronizzazione già in corso."

        self._is_syncing = True
        try:
            service = self.get_service(interactive=False)

            # Fase 1: Push modifiche locali verso Google
            pushed = 0
            deleted = 0
            for pending in db.get_pending_sync_events():
                status = pending.get("sync_status")
                g_id = (pending.get("google_event_id") or "").strip()
                cal_id = (pending.get("google_calendar_id") or "primary").strip()

                if status == "pending_delete":
                    if g_id:
                        try:
                            service.events().delete(calendarId=cal_id, eventId=g_id).execute()
                        except Exception as e_del:
                            # Se 404/410 su Google, è già stato eliminato
                            if "404" not in str(e_del) and "410" not in str(e_del):
                                print(f"[GoogleCalendar Sync] Avviso delete {g_id}: {e_del}")
                    db.delete_calendar_event(pending["id"], hard_delete=True)
                    deleted += 1

                elif status == "pending_push":
                    body = self._build_google_event_body(pending)
                    try:
                        if g_id:
                            updated = service.events().patch(calendarId=cal_id, eventId=g_id, body=body).execute()
                            new_gid = updated.get("id", g_id)
                        else:
                            created = service.events().insert(calendarId=cal_id, body=body).execute()
                            new_gid = created.get("id", "")
                        if new_gid:
                            db.update_calendar_event(
                                pending["id"],
                                google_event_id=new_gid,
                                sync_status="synced",
                            )
                            pushed += 1
                    except Exception as e_push:
                        print(f"[GoogleCalendar Sync] Errore push evento #{pending['id']}: {e_push}")

            # Fase 2: Pull eventi da Google Calendar verso SQLite
            today = datetime.date.today()
            start_d = today - datetime.timedelta(days=max(0, days_back))
            end_d = today + datetime.timedelta(days=max(1, days_forward))
            t_min = datetime.datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=self._tz).isoformat()
            t_max = datetime.datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=self._tz).isoformat()

            calendars_to_query: List[Dict[str, Any]] = []
            for c in self.get_calendar_list(service=service):
                cid = c.get("id")
                if not cid:
                    continue
                if (c.get("selected") is True or c.get("primary") is True) and not c.get("hidden", False):
                    calendars_to_query.append(c)
            if not calendars_to_query:
                calendars_to_query.append({"id": "primary", "summary": "Personale", "primary": True})

            seen_keys: List[tuple] = []
            imported = 0

            for cal in calendars_to_query:
                cal_id = cal.get("id", "primary")
                raw_name = cal.get("summary", "Personale")
                cal_display = "Personale" if cal.get("primary") else raw_name
                cat_guess = "Università" if "uni" in cal_display.lower() else "Personale"

                try:
                    res = service.events().list(
                        calendarId=cal_id,
                        timeMin=t_min,
                        timeMax=t_max,
                        singleEvents=True,
                        orderBy='startTime',
                        maxResults=250,
                    ).execute()
                except Exception as e_list:
                    print(f"[GoogleCalendar Sync] Avviso lettura '{cal_display}': {e_list}")
                    continue

                for item in res.get("items", []):
                    gid = item.get("id")
                    if not gid:
                        continue
                    seen_keys.append((gid, cal_id))

                    start_info = item.get("start", {})
                    end_info = item.get("end", {})
                    summary = item.get("summary") or "(Senza Titolo)"
                    desc = item.get("description") or ""
                    loc = item.get("location") or ""

                    if "dateTime" in start_info:
                        s_dt = datetime.datetime.fromisoformat(start_info["dateTime"]).astimezone(self._tz)
                        ev_date_str = s_dt.date().isoformat()
                        st_str = s_dt.strftime("%H:%M")
                        if "dateTime" in end_info:
                            e_dt = datetime.datetime.fromisoformat(end_info["dateTime"]).astimezone(self._tz)
                            et_str = e_dt.strftime("%H:%M")
                        else:
                            et_str = ""
                        all_day = False
                    elif "date" in start_info:
                        ev_date_str = start_info["date"][:10]
                        st_str = ""
                        et_str = ""
                        all_day = True
                    else:
                        continue

                    db.upsert_google_event(
                        google_event_id=gid,
                        google_calendar_id=cal_id,
                        google_calendar_name=cal_display,
                        title=summary,
                        event_date=ev_date_str,
                        start_time=st_str,
                        end_time=et_str,
                        is_all_day=all_day,
                        description=desc,
                        location=loc,
                        category=cat_guess,
                    )
                    imported += 1

            pruned = db.prune_deleted_google_events(start_d.isoformat(), end_d.isoformat(), seen_keys)
            self._last_sync_ts = time.time()
            self._last_sync_error = ""
            msg = f"Sync completata: {imported} importati, {pushed} inviati, {deleted + pruned} rimossi."
            self._notify_sync_listeners(True, msg)
            return True, msg
        except Exception as e:
            self._last_sync_error = str(e)
            self._notify_sync_listeners(False, str(e))
            return False, str(e)
        finally:
            self._is_syncing = False
            self._sync_lock.release()

    def sync_in_background(self, force: bool = False):
        """Avvia la sincronizzazione Google Calendar in un thread daemon non bloccante."""
        if not GOOGLE_LIBS_AVAILABLE or not self.has_token_file():
            return
        if self._is_syncing:
            return
        if not force and (time.time() - self._last_sync_ts) < 60.0:
            return

        def _worker():
            self.sync_with_google()

        threading.Thread(target=_worker, daemon=True, name="FlowTimeGoogleSync").start()


# Istanza singleton centralizzata
calendar_manager = GoogleCalendarManager()
