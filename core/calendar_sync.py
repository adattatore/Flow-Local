"""
core/calendar_sync.py
Modulo per l'integrazione bidirezionale con Google Calendar in FlowLocal e Whis.

Funzionalità:
- Autenticazione OAuth 2.0 tramite InstalledAppFlow e salvataggio token in token_calendar.json
- Creazione appuntamenti su calendario primario (orari o tutto il giorno con fuso Europe/Rome)
- Lettura e formattazione impegni imminenti o per una specifica data
- Verifica dello stato di connessione per la dashboard Hub e l'HUD di Whis
"""

import os
import re
import datetime
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
from typing import Optional, List, Dict, Any, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar']
DEFAULT_TIMEZONE = 'Europe/Rome'

# Risoluzione percorsi file credenziali e token
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CREDENTIALS_PATH = os.path.join(PROJECT_ROOT, "credentials.json")
DEFAULT_TOKEN_PATH = os.path.join(PROJECT_ROOT, "token_calendar.json")

ITALIAN_WEEKDAYS = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
ITALIAN_MONTHS = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]


class GoogleCalendarManager:
    """Manager per l'accesso e la sincronizzazione con Google Calendar API v3."""

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
        timezone: str = DEFAULT_TIMEZONE
    ):
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self.token_path = token_path or DEFAULT_TOKEN_PATH
        self.timezone = timezone
        self._tz = get_tz(self.timezone)
        self._service = None

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
        try:
            creds = self.get_credentials(interactive=False)
            return creds is not None and creds.valid
        except Exception:
            return False

    def get_credentials(self, interactive: bool = True) -> Optional[Credentials]:
        """
        Recupera o rinnova le credenziali OAuth 2.0.
        Se interactive=True e le credenziali non sono valide, avvia il flusso da browser locale.
        """
        self._resolve_paths()
        creds = None

        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            except Exception as e:
                print(f"[GoogleCalendar] Errore lettura token esistente: {e}")
                creds = None

        # Se le credenziali esistono ma sono scadute, tenta il refresh silenzioso
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

        # Se non valide e richiesta autenticazione interattiva
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

            # Salva il nuovo token generato
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
        Tenta l'autenticazione o verifica lo stato corrente.
        Restituisce una tupla (successo: bool, messaggio: str).
        """
        try:
            if not self.has_credentials_file() and not self.has_token_file():
                return False, "File 'credentials.json' mancante nella directory del progetto."

            service = self.get_service(interactive=interactive)
            # Test rapido di chiamata per verificare l'accesso effettivo
            service.calendarList().get(calendarId='primary').execute()
            self._service = service
            return True, "Google Calendar connesso e autorizzato con successo."
        except Exception as e:
            return False, str(e)

    def create_event(
        self,
        summary: str,
        date_str: str,
        start_time_str: str = "09:00",
        duration_minutes: int = 60,
        description: str = ""
    ) -> Dict[str, Any]:
        """
        Crea un evento sul calendario primario con timezone Europe/Rome.

        :param summary: Titolo dell'evento.
        :param date_str: Data in formato 'YYYY-MM-DD'.
        :param start_time_str: Ora di inizio 'HH:MM', oppure 'all_day' / '' per eventi di intera giornata.
        :param duration_minutes: Durata in minuti per eventi orari (default 60).
        :param description: Descrizione opzionale dell'evento.
        :return: Dict contenente i dettagli dell'evento creato da Google.
        """
        service = self.get_service(interactive=True)

        summary_clean = (summary or "").strip() or "Nuovo Appuntamento"
        date_clean = (date_str or "").strip()

        # Verifica formato data YYYY-MM-DD
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
            # Per Google Calendar, la data di fine degli eventi all-day è il giorno successivo
            next_day = event_date + datetime.timedelta(days=1)
            event_body = {
                'summary': summary_clean,
                'description': description or "Creato da Whis per FlowLocal",
                'start': {
                    'date': event_date.isoformat(),
                    'timeZone': self.timezone,
                },
                'end': {
                    'date': next_day.isoformat(),
                    'timeZone': self.timezone,
                },
            }
        else:
            time_clean = start_time_str.strip()
            # Gestione casi come "16" -> "16:00"
            if re_match := datetime.datetime.strptime(time_clean, "%H:%M") if ":" in time_clean else None:
                start_hour = re_match.hour
                start_min = re_match.minute
            else:
                try:
                    start_hour = int(time_clean)
                    start_min = 0
                except Exception:
                    start_hour = 9
                    start_min = 0

            start_dt = datetime.datetime(
                event_date.year,
                event_date.month,
                event_date.day,
                start_hour,
                start_min,
                tzinfo=self._tz
            )
            dur = max(5, int(duration_minutes or 60))
            end_dt = start_dt + datetime.timedelta(minutes=dur)

            event_body = {
                'summary': summary_clean,
                'description': description or "Creato da Whis per FlowLocal",
                'start': {
                    'dateTime': start_dt.isoformat(),
                    'timeZone': self.timezone,
                },
                'end': {
                    'dateTime': end_dt.isoformat(),
                    'timeZone': self.timezone,
                },
            }

        created = service.events().insert(calendarId='primary', body=event_body).execute()
        return created

    def get_calendar_list(self) -> List[Dict[str, Any]]:
        """Restituisce l'elenco dei calendari disponibili nell'account Google collegato."""
        service = self.get_service(interactive=False)
        try:
            cals_result = service.calendarList().list(minAccessRole='reader').execute()
            return cals_result.get('items', [])
        except Exception as e:
            print(f"[GoogleCalendar] Avviso recupero lista calendari: {e}")
            return [{"id": "primary", "summary": "Personale", "primary": True, "selected": True}]

    def get_upcoming_events(
        self,
        days: int = 7,
        target_date: Optional[str] = None,
        calendar_id: Optional[str] = None,
        include_all_selected: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Recupera gli impegni per una data specifica o per i prossimi `days` giorni.
        Se `calendar_id` non è specificato, interroga tutti i calendari attivi/selezionati
        (inclusi calendari universitari o secondari come 'Università').

        :param days: Numero di giorni da recuperare (default 7).
        :param target_date: Stringa data opzionale 'YYYY-MM-DD'. Se fornita, filtra esclusivamente quel giorno.
        :param calendar_id: ID specifico del calendario da interrogare (opzionale).
        :param include_all_selected: Se True, interroga tutti i calendari selezionati dell'account.
        :return: Lista di dizionari formattati con data, ora, titolo, luogo, calendario e stringa riassuntiva.
        """
        service = self.get_service(interactive=False)

        if target_date:
            try:
                parts = [int(p) for p in target_date.strip().split("-")]
                d = datetime.date(parts[0], parts[1], parts[2])
                t_min = datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=self._tz).isoformat()
                t_max = datetime.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=self._tz).isoformat()
            except Exception as e:
                raise ValueError(f"Data target '{target_date}' non valida (YYYY-MM-DD): {e}")
        else:
            now = datetime.datetime.now(self._tz)
            t_min = now.isoformat()
            t_max = (now + datetime.timedelta(days=max(1, days))).isoformat()

        # Determinazione calendari da interrogare
        calendars_to_query: List[Dict[str, Any]] = []
        if calendar_id:
            calendars_to_query.append({"id": calendar_id, "summary": "Calendario"})
        elif include_all_selected:
            all_cals = self.get_calendar_list()
            for c in all_cals:
                cid = c.get('id')
                if not cid:
                    continue
                # Include i calendari primari, quelli selezionati o non nascosti
                is_selected = c.get('selected') is True or c.get('primary') is True
                if is_selected and not c.get('hidden', False):
                    calendars_to_query.append(c)

            if not calendars_to_query:
                calendars_to_query.append({"id": "primary", "summary": "Personale", "primary": True})
        else:
            calendars_to_query.append({"id": "primary", "summary": "Personale", "primary": True})

        formatted_events: List[Dict[str, Any]] = []

        for cal in calendars_to_query:
            cal_id = cal.get('id', 'primary')
            raw_cal_name = cal.get('summary', 'Personale')
            cal_display_name = "Personale" if cal.get('primary') else raw_cal_name

            try:
                events_result = service.events().list(
                    calendarId=cal_id,
                    timeMin=t_min,
                    timeMax=t_max,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
            except Exception as e:
                print(f"[GoogleCalendar] Avviso lettura calendario '{cal_display_name}' ({cal_id}): {e}")
                continue

            raw_items = events_result.get('items', [])
            for item in raw_items:
                summary = item.get('summary') or '(Senza Titolo)'
                location = (item.get('location') or '').strip()
                start_info = item.get('start', {})
                end_info = item.get('end', {})

                sort_key = ""
                if 'dateTime' in start_info:
                    # Evento con orario
                    start_raw = start_info['dateTime']
                    end_raw = end_info.get('dateTime', '')
                    sort_key = start_raw
                    try:
                        s_dt = datetime.datetime.fromisoformat(start_raw).astimezone(self._tz)
                        dow = ITALIAN_WEEKDAYS[s_dt.weekday()]
                        day_str = f"{dow} {s_dt.day:02d}/{s_dt.month:02d}"
                        time_start = s_dt.strftime("%H:%M")
                        if end_raw:
                            e_dt = datetime.datetime.fromisoformat(end_raw).astimezone(self._tz)
                            time_end = e_dt.strftime("%H:%M")
                            time_str = f"{time_start} - {time_end}"
                        else:
                            time_str = time_start
                    except Exception:
                        day_str = start_raw[:10]
                        time_str = start_raw[11:16]

                    loc_tag = f" ({location})" if location else ""
                    cal_tag = f" [{cal_display_name}]" if cal_display_name != "Personale" else ""
                    formatted_events.append({
                        "id": item.get('id'),
                        "calendar_id": cal_id,
                        "calendar_name": cal_display_name,
                        "summary": summary,
                        "location": location,
                        "date": day_str,
                        "time": time_str,
                        "sort_key": sort_key,
                        "is_all_day": False,
                        "formatted": f"• {day_str}, {time_str}: {summary}{loc_tag}{cal_tag}",
                        "raw": item
                    })
                elif 'date' in start_info:
                    # Evento tutto il giorno
                    d_raw = start_info['date']
                    sort_key = f"{d_raw}T00:00:00"
                    try:
                        p = [int(x) for x in d_raw.split("-")]
                        d_obj = datetime.date(p[0], p[1], p[2])
                        dow = ITALIAN_WEEKDAYS[d_obj.weekday()]
                        day_str = f"{dow} {d_obj.day:02d}/{d_obj.month:02d}"
                    except Exception:
                        day_str = d_raw

                    loc_tag = f" ({location})" if location else ""
                    cal_tag = f" [{cal_display_name}]" if cal_display_name != "Personale" else ""
                    formatted_events.append({
                        "id": item.get('id'),
                        "calendar_id": cal_id,
                        "calendar_name": cal_display_name,
                        "summary": summary,
                        "location": location,
                        "date": day_str,
                        "time": "Tutto il giorno",
                        "sort_key": sort_key,
                        "is_all_day": True,
                        "formatted": f"• {day_str} (Tutto il giorno): {summary}{loc_tag}{cal_tag}",
                        "raw": item
                    })

        # Ordina tutti gli eventi cronologicamente
        formatted_events.sort(key=lambda ev: ev.get("sort_key", ""))
        return formatted_events

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> bool:
        """Elimina un evento da Google Calendar dato il suo ID univoco e il calendario di appartenenza."""
        service = self.get_service(interactive=False)
        try:
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            return True
        except Exception as e:
            if calendar_id != "primary":
                try:
                    service.events().delete(calendarId='primary', eventId=event_id).execute()
                    return True
                except Exception:
                    pass
            print(f"[GoogleCalendar] Errore cancellazione evento {event_id} (cal={calendar_id}): {e}")
            raise

    def find_events_by_query(
        self,
        query: str,
        target_date: Optional[str] = None,
        days: int = 14
    ) -> List[Dict[str, Any]]:
        """
        Cerca eventi imminenti per parola chiave (es. 'Fisica', 'Dentista')
        oppure per orario (es. '16:00', '16', 'pomeriggio').
        """
        events = self.get_upcoming_events(days=days, target_date=target_date)
        if not query or not query.strip():
            return events

        q_clean = query.strip().lower()
        # Normalizzazione orari es. "16" -> "16:00", "4" -> "16:00" o "04:00"
        time_tokens = []
        if match_hour := re.search(r'\b(?:alle\s+)?(\d{1,2})(?::(\d{2}))?\b', q_clean):
            h = int(match_hour.group(1))
            m = int(match_hour.group(2)) if match_hour.group(2) else 0
            time_tokens.append(f"{h:02d}:{m:02d}")
            if h <= 12:
                time_tokens.append(f"{h + 12:02d}:{m:02d}")

        matches: List[Dict[str, Any]] = []
        for ev in events:
            summary = (ev.get("summary") or "").lower()
            time_str = (ev.get("time") or "").lower()

            # Match per parola chiave nel titolo
            if q_clean in summary:
                matches.append(ev)
                continue

            # Match per orario
            for tt in time_tokens:
                if tt in time_str:
                    matches.append(ev)
                    break

        return matches

    def get_day_briefing(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Restituisce il resoconto completo e strutturato degli impegni per la giornata specificata."""
        events = self.get_upcoming_events(days=1, target_date=target_date)
        return {
            "date": target_date or datetime.date.today().isoformat(),
            "count": len(events),
            "events": events
        }


# Istanza singleton centralizzata
calendar_manager = GoogleCalendarManager()
