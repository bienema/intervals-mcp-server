"""Google Calendar sync: intervals.icu -> dedicated Google calendar.

Auth: Service Account (JSON in env GOOGLE_SA_JSON). The target calendar must be
shared with the service account e-mail ("Änderungen an Terminen vornehmen").
Every managed event carries extendedProperties.private {source=coach, icu_key, hash}
so the sync is idempotent and only touches its own events.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta

from .api import IntervalsAPI
from .facts import plan_totals, render_actual, render_plan, split_description
from .scoring import Zones
from .workout import parse_workout

BASE = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
COLORS = {"🟢": "10", "🟡": "5", "🔴": "11", "planned": "9", "missed": "8", "note": "3", "done": "2"}
_SCORE = re.compile(r"Score (\d+)/100 (🟢|🟡|🔴)")
DEBRIEF_MARK = "Coach-Debrief"


class GCal:
    def __init__(self, calendar_id: str, session=None, dry_run: bool = False):
        self.url = BASE.format(cal=calendar_id)
        self.dry_run = dry_run
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            info = json.loads(os.environ["GOOGLE_SA_JSON"])
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            session = AuthorizedSession(creds)
        self.s = session

    def managed(self, time_min: str, time_max: str) -> dict[str, dict]:
        out, token = {}, None
        while True:
            params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                      "maxResults": 2500, "privateExtendedProperty": "source=coach"}
            if token:
                params["pageToken"] = token
            r = self.s.get(self.url, params=params)
            r.raise_for_status()
            data = r.json()
            for ev in data.get("items", []):
                key = ev.get("extendedProperties", {}).get("private", {}).get("icu_key")
                if key:
                    out[key] = ev
            token = data.get("nextPageToken")
            if not token:
                return out

    def _do(self, method: str, url: str, body: dict | None = None):
        if self.dry_run:
            print(f"[dry-run] {method} {body.get('summary') if body else url}")
            return None
        r = self.s.request(method, url, json=body)
        r.raise_for_status()
        return r.json() if r.content else None

    def insert(self, body: dict):
        return self._do("POST", self.url, body)

    def update(self, event_id: str, body: dict):
        # PUT replaces the whole event (needed when switching all-day <-> timed)
        return self._do("PUT", f"{self.url}/{event_id}", body)

    def delete(self, event_id: str):
        return self._do("DELETE", f"{self.url}/{event_id}")


# -- event builders -------------------------------------------------------------------
def _allday(d: str) -> dict:
    nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    return {"start": {"date": d}, "end": {"date": nxt}}


def _timed(start_local: str, seconds: float, tz: str) -> dict:
    st = datetime.fromisoformat(start_local[:19])
    en = st + timedelta(seconds=max(seconds, 600))
    return {"start": {"dateTime": st.isoformat(), "timeZone": tz},
            "end": {"dateTime": en.isoformat(), "timeZone": tz}}


def _coach_message(msgs: list[dict]) -> str | None:
    hits = [m.get("content") or "" for m in msgs if DEBRIEF_MARK in (m.get("content") or "")]
    return hits[-1] if hits else None


def _title(prefix: str, name: str) -> str:
    return f"{prefix} · {name}" if prefix != "✅" else f"✅ {name}"


def _done_parts(act: dict, msg: str | None, tol: float) -> tuple[str, str, str]:
    """(title prefix, description body, color) for a completed activity."""
    if msg:
        body = "\n".join(msg.splitlines()[1:]).strip()   # drop "🏃 Coach-Debrief · ..." header
        m = _SCORE.search(msg)
        if m:
            return f"✅ {m.group(2)} {m.group(1)}", body, COLORS[m.group(2)]
        return "✅", body, COLORS["done"]
    return "✅", render_actual(act, [], [], tol) + "\n\n(Debriefing folgt)", COLORS["done"]


def build_events(api: IntervalsAPI, cfg: dict, today: date) -> dict[str, dict]:
    tz = cfg.get("timezone", "Europe/Berlin")
    tol = cfg["pace_tolerance"]
    types = cfg["jobs"]["sport_types"]
    g = cfg.get("gcal", {})
    back, ahead = g.get("days_back", 7), g.get("days_ahead", 14)
    oldest, newest = (today - timedelta(days=back)).isoformat(), (today + timedelta(days=ahead)).isoformat()
    zones = Zones.from_settings(api.run_settings())

    acts = [a for a in api.activities(oldest, today.isoformat()) if a.get("type") in types]
    by_event = {a["paired_event_id"]: a for a in acts if a.get("paired_event_id")}
    msg_cache: dict[str, str | None] = {}

    def msg_for(a: dict) -> str | None:
        aid = str(a["id"])
        if aid not in msg_cache:
            msg_cache[aid] = _coach_message(api.messages(aid))
        return msg_cache[aid]

    out: dict[str, dict] = {}
    for e in api.events(oldest, newest, "WORKOUT"):
        if (e.get("type") or "Run") not in types:
            continue
        d = (e.get("start_date_local") or "")[:10]
        desc = e.get("description") or ""
        name = e.get("name") or "Lauf"
        plan_txt = render_plan(desc)
        act = by_event.get(e["id"])
        if act:
            prefix, body, color = _done_parts(act, msg_for(act), tol)
            out[f"event-{e['id']}"] = {
                "summary": _title(prefix, name), "colorId": color,
                "description": f"{body}\n\n— Plan —\n{plan_txt}".strip(),
                **_timed(act.get("start_date_local", f"{d}T07:00:00"),
                         act.get("elapsed_time") or act.get("moving_time") or 3600, tz)}
        elif d < today.isoformat():
            out[f"event-{e['id']}"] = {"summary": f"⏭️ {name} (nicht absolviert)", "colorId": COLORS["missed"],
                                       "description": plan_txt, **_allday(d)}
        else:
            dist, dur = plan_totals(parse_workout(desc, "easy"), zones)
            intro, _ = split_description(desc)
            head = f"🏃 {name}"
            if dist:
                head += f" · ≈{dist / 1000:.0f} km"
            if dur:
                head += f" · {dur / 60:.0f} min"
            text = "\n".join(intro) + ("\n\n" if intro else "") + plan_txt
            out[f"event-{e['id']}"] = {"summary": head, "colorId": COLORS["planned"],
                                       "description": text.strip(), **_allday(d)}

    for a in acts:
        if a.get("paired_event_id"):
            continue
        prefix, body, color = _done_parts(a, msg_for(a), tol)
        km = (a.get("distance") or 0) / 1000
        out[f"activity-{a['id']}"] = {
            "summary": _title(prefix, f"{a.get('name') or 'Lauf'} · {km:.1f} km".replace(".", ",")),
            "colorId": color, "description": body,
            **_timed(a["start_date_local"], a.get("elapsed_time") or a.get("moving_time") or 3600, tz)}

    for n in api.events(oldest, newest, "NOTE"):
        if (n.get("name") or "").startswith("Wochenrückblick"):
            d = (n.get("start_date_local") or "")[:10]
            out[f"note-{n['id']}"] = {"summary": f"📊 {n['name']}", "colorId": COLORS["note"],
                                      "description": n.get("description") or "", **_allday(d)}
    return out


def _hash(body: dict) -> str:
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def sync(api: IntervalsAPI, cal: GCal, cfg: dict, today: date) -> dict[str, int]:
    g = cfg.get("gcal", {})
    want = build_events(api, cfg, today)
    tmin = f"{(today - timedelta(days=g.get('days_back', 7) + 1)).isoformat()}T00:00:00Z"
    tmax = f"{(today + timedelta(days=g.get('days_ahead', 14) + 1)).isoformat()}T23:59:59Z"
    have = cal.managed(tmin, tmax)
    stats = {"neu": 0, "geändert": 0, "gelöscht": 0, "unverändert": 0}
    for key, body in want.items():
        h = _hash(body)
        body = {**body, "extendedProperties": {"private": {"source": "coach", "icu_key": key, "hash": h}}}
        cur = have.get(key)
        if cur is None:
            cal.insert(body)
            stats["neu"] += 1
        elif cur.get("extendedProperties", {}).get("private", {}).get("hash") != h:
            cal.update(cur["id"], body)
            stats["geändert"] += 1
        else:
            stats["unverändert"] += 1
    for key, cur in have.items():
        if key.startswith("event-") and key not in want:   # planned workout removed in intervals.icu
            cal.delete(cur["id"])
            stats["gelöscht"] += 1
    print(f"[calendar] {stats}")
    return stats
