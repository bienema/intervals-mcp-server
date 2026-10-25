"""The three coach jobs: debrief (after each session), briefing (morning), weekly review."""
from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import llm
from .api import IntervalsAPI
from .facts import render_actual
from .scoring import ScoreResult, Zones, fmt_pace, score_activity, score_line
from .workout import parse_workout, planned_duration

DEBRIEF_MARK = "Coach-Debrief"
TAGESFORM_MARK = "Tagesform (auto):"
KNOWN_TYPES = ("easy", "recovery", "long", "interval", "threshold", "mp", "test")


def today_local(cfg: dict) -> date:
    return datetime.now(ZoneInfo(cfg.get("timezone", "Europe/Berlin"))).date()


def workout_type(event: dict | None, steps) -> str | None:
    if not event:
        return None
    for t in event.get("tags") or []:
        if t.lower() in KNOWN_TYPES:
            return t.lower()
    # untagged: infer from structure
    return "interval" if any(s.role == "work" for s in steps) else "easy"


# =============================================================================
# Debrief
# =============================================================================
def _debrief_prompt(res: ScoreResult, event: dict | None, act: dict, facts: str = "") -> str:
    payload = {
        "ist_daten": facts,
        "einheit": event.get("name") if event else act.get("name"),
        "typ": res.workout_type,
        "plan": (event or {}).get("description", ""),
        "score": res.total,
        "kriterien": [{"kriterium": c.label, "punkte": c.points, "max": c.weight, "detail": c.detail}
                      for c in res.criteria],
        "messwerte": res.metrics,
        "hinweise": res.notes,
    }
    return (
        "Schreibe das Debriefing zu dieser Laufeinheit. Der Score und die Punkte stehen "
        "schon darüber, wiederhole sie nicht. Format, genau drei Zeilen:\n"
        "Gut: <1-2 Punkte mit Zahlen>\n"
        "Verbessern: <1-2 Punkte mit Zahlen, oder 'nichts Wesentliches'>\n"
        "Konsequenz: <konkret für die nächsten Einheiten, oder 'keine Änderung'>\n"
        "RPE/Feel (1-10 / 1-5) nur zur Einordnung nutzen, falls vorhanden.\n\n"
        f"Daten:\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
    )


def _debrief_fallback(res: ScoreResult) -> str:
    good = [c for c in res.criteria if c.value is not None and c.value >= 0.85]
    weak = [c for c in res.criteria if c.value is not None and c.value < 0.7]
    g = ", ".join(f"{c.label} ({c.detail})" for c in good) or "–"
    w = ", ".join(f"{c.label} ({c.detail})" for c in weak) or "nichts Wesentliches"
    return f"Gut: {g}\nVerbessern: {w}\nKonsequenz: siehe Wochenrückblick"


def debrief(api: IntervalsAPI, cfg: dict, activity_id: str | None = None, force: bool = False) -> None:
    jc = cfg["jobs"]
    settings = api.run_settings()
    zones = Zones.from_settings(settings)
    if activity_id:
        acts = [api.activity(activity_id)]
    else:
        t = today_local(cfg)
        acts = api.activities((t - timedelta(days=jc["debrief_lookback_days"])).isoformat(), t.isoformat())
    for a in acts:
        aid = str(a["id"])
        if a.get("type") not in jc["sport_types"] or (a.get("moving_time") or 0) < jc["min_moving_minutes"] * 60:
            continue
        if not force and any(DEBRIEF_MARK in (m.get("content") or "") for m in api.messages(aid)):
            continue
        act = api.activity(aid)
        event = api.event(act["paired_event_id"]) if act.get("paired_event_id") else None
        steps = parse_workout((event or {}).get("description", ""), "easy")
        wtype = workout_type(event, steps)
        if wtype:
            steps = parse_workout(event.get("description", ""), wtype)
        ivs = api.intervals(aid)
        res = score_activity(wtype or "unplanned", steps if event else [], act,
                             ivs, api.streams(aid), zones, cfg)
        if wtype == "test":
            res.notes.append("Testlauf: kein Score. Schwellenwerte bitte im Chat auswerten lassen.")
        facts = render_actual(act, ivs, steps if event else [], cfg["pace_tolerance"])
        text = llm.complete(_debrief_prompt(res, event, act, facts), cfg) or _debrief_fallback(res)
        title = event["name"] if event else f"{act.get('name', 'Lauf')} (ungeplant)"
        body = [f"🏃 {DEBRIEF_MARK} · {title}", score_line(res), "", "Ist-Daten", facts, "", text]
        if res.notes:
            body += [""] + [f"ℹ️ {n}" for n in res.notes]
        api.post_message(aid, "\n".join(body))
        print(f"[debrief] {aid} {title}: {res.total}")


# =============================================================================
# Briefing (morning): readiness line + briefing if the workout has none
# =============================================================================
def readiness(wellness: list[dict], day: date) -> tuple[str, str]:
    by_day = {w["id"]: w for w in wellness}
    cur = by_day.get(day.isoformat(), {})
    prev = [by_day[(day - timedelta(days=i)).isoformat()] for i in range(1, 8)
            if (day - timedelta(days=i)).isoformat() in by_day]
    base_hrv = statistics.mean([w["hrv"] for w in prev if w.get("hrv")] or [0]) or None
    base_rhr = statistics.mean([w["restingHR"] for w in prev if w.get("restingHR")] or [0]) or None
    hrv, rhr, sleep = cur.get("hrv"), cur.get("restingHR"), cur.get("sleepSecs")
    tsb = (cur["ctl"] - cur["atl"]) if cur.get("ctl") is not None and cur.get("atl") is not None else None

    red = yellow = 0
    if hrv and base_hrv:
        red += hrv < 0.85 * base_hrv
        yellow += 0.85 * base_hrv <= hrv < 0.93 * base_hrv
    if rhr and base_rhr:
        red += rhr >= base_rhr + 7
        yellow += base_rhr + 4 <= rhr < base_rhr + 7
    if tsb is not None:
        red += tsb < -30
        yellow += -30 <= tsb < -20
    if sleep:
        yellow += sleep < 6 * 3600
    status = "🔴" if red else "🟡" if yellow else "🟢"
    parts = []
    if hrv:
        parts.append(f"HRV {hrv:.0f}" + (f" (Ø7 {base_hrv:.0f})" if base_hrv else ""))
    if rhr:
        parts.append(f"Ruhepuls {rhr:.0f}" + (f" (Ø7 {base_rhr:.0f})" if base_rhr else ""))
    if sleep:
        parts.append(f"Schlaf {sleep / 3600:.1f} h")
    if tsb is not None:
        parts.append(f"TSB {tsb:.0f}")
    hint = {"🟢": "", "🟡": " – Intensität nach Gefühl, lieber am unteren Ende.",
            "🔴": " – heute locker oder Pause, Qualität verschieben."}[status]
    return status, f"{TAGESFORM_MARK} {status} " + ", ".join(parts or ["keine Wellnessdaten"]) + hint


def _briefing_prompt(event: dict, wtype: str, zones: Zones) -> str:
    return (
        "Schreibe ein Briefing für diese Laufeinheit, genau vier Zeilen ohne Leerzeilen:\n"
        "Ziel: <physiologischer Zweck, 1 Satz>\nInhalt: <Aufbau kurz>\n"
        "Zielwerte: <Pace- und Pulsbereiche>\nErfolg: <woran der Score misst>\n\n"
        f"Typ: {wtype}\nName: {event.get('name')}\nStruktur:\n{event.get('description', '')}\n"
        f"Schwellenpace {fmt_pace(zones.threshold_pace)}, Schwellenpuls {zones.lthr:.0f}, "
        f"Pulszonen-Obergrenzen Z1-Z7: {zones.hr_bounds}"
    )


def briefing(api: IntervalsAPI, cfg: dict, day: date | None = None) -> None:
    day = day or today_local(cfg)
    events = [e for e in api.events(day.isoformat(), day.isoformat(), "WORKOUT")
              if (e.get("type") or "Run") in cfg["jobs"]["sport_types"]]
    if not events:
        print("[briefing] heute kein Lauf geplant")
        return
    zones = Zones.from_settings(api.run_settings())
    _, form_line = readiness(api.wellness((day - timedelta(days=8)).isoformat(), day.isoformat()), day)
    for e in events:
        desc = e.get("description") or ""
        lines = [ln for ln in desc.splitlines() if not ln.startswith(TAGESFORM_MARK)]
        if not any(ln.startswith("Ziel:") for ln in lines):
            steps = parse_workout(desc, "easy")
            wtype = workout_type(e, steps) or "easy"
            brief = llm.complete(_briefing_prompt(e, wtype, zones), cfg)
            if brief:
                lines = [ln for ln in brief.splitlines() if ln.strip()] + [""] + lines
        new_desc = "\n".join([form_line] + lines)
        if new_desc != desc:
            api.update_event_description(e["id"], new_desc)
            print(f"[briefing] {e['name']}: aktualisiert")


# =============================================================================
# Weekly review
# =============================================================================
_SCORE_RE = re.compile(r"Score (\d+)/100")


def weekly(api: IntervalsAPI, cfg: dict, day: date | None = None) -> None:
    day = day or today_local(cfg)
    mon = day - timedelta(days=day.weekday())
    sun = mon + timedelta(days=6)
    kw = mon.isocalendar().week
    name = f"Wochenrückblick KW {kw}"
    if any(n.get("name") == name for n in api.events(sun.isoformat(), sun.isoformat(), "NOTE")):
        print(f"[weekly] {name} existiert schon")
        return

    types = cfg["jobs"]["sport_types"]
    planned = [e for e in api.events(mon.isoformat(), sun.isoformat(), "WORKOUT")
               if (e.get("type") or "Run") in types]
    plan_min = sum(planned_duration(parse_workout(e.get("description", ""), "easy")) / 60
                   or (e.get("moving_time") or 0) / 60 for e in planned)
    acts = [a for a in api.activities(mon.isoformat(), sun.isoformat()) if a.get("type") in types]
    sessions = []
    for a in acts:
        score = None
        for m in api.messages(str(a["id"])):
            hit = _SCORE_RE.search(m.get("content") or "")
            if hit and DEBRIEF_MARK in m["content"]:
                score = int(hit.group(1))
        sessions.append({"datum": a.get("start_date_local", "")[:10], "name": a.get("name"),
                         "km": round((a.get("distance") or 0) / 1000, 1),
                         "min": round((a.get("moving_time") or 0) / 60), "score": score,
                         "rpe": a.get("icu_rpe")})
    scores = [s["score"] for s in sessions if s["score"] is not None]

    well = api.wellness((mon - timedelta(days=28)).isoformat(), sun.isoformat())
    wk = [w for w in well if w["id"] >= mon.isoformat()]
    before = [w for w in well if w["id"] < mon.isoformat()]
    avg = lambda rows, k: round(statistics.mean([r[k] for r in rows if r.get(k)]), 1) \
        if any(r.get(k) for r in rows) else None  # noqa: E731
    last = wk[-1] if wk else {}
    first = wk[0] if wk else {}
    load = {
        "ctl_start": first.get("ctl"), "ctl_ende": last.get("ctl"), "atl_ende": last.get("atl"),
        "tsb_ende": round(last["ctl"] - last["atl"], 1) if last.get("ctl") is not None and last.get("atl") is not None else None,
        "hrv_woche": avg(wk, "hrv"), "hrv_4wochen_davor": avg(before, "hrv"),
        "ruhepuls_woche": avg(wk, "restingHR"), "ruhepuls_4wochen_davor": avg(before, "restingHR"),
        "schlaf_h_woche": round(avg(wk, "sleepSecs") / 3600, 1) if avg(wk, "sleepSecs") else None,
        "notizen": [w.get("comments") for w in wk if w.get("comments")],
    }
    nxt = [{"datum": e.get("start_date_local", "")[:10], "name": e.get("name")}
           for e in api.events((sun + timedelta(days=1)).isoformat(), (sun + timedelta(days=7)).isoformat(), "WORKOUT")]
    summary = {
        "woche": f"KW {kw} ({mon.isoformat()} bis {sun.isoformat()})",
        "geplant": {"einheiten": len(planned), "minuten": round(plan_min)},
        "absolviert": {"einheiten": len(acts), "km": round(sum(s["km"] for s in sessions), 1),
                       "minuten": sum(s["min"] for s in sessions)},
        "einheiten": sessions, "score_schnitt": round(statistics.mean(scores)) if scores else None,
        "belastung_erholung": load, "naechste_woche_geplant": nxt,
    }
    head = (f"Umfang: {summary['absolviert']['km']} km / {summary['absolviert']['minuten']} min "
            f"({len(acts)} von {len(planned)} geplanten Einheiten)\n"
            f"Score-Schnitt: {summary['score_schnitt'] if scores else '–'}"
            + (f" · CTL {load['ctl_ende']:.0f}, TSB {load['tsb_ende']:.0f}" if load["tsb_ende"] is not None else ""))
    prompt = (
        "Schreibe den Wochenrückblick. Die Kopfzeilen mit Umfang und Score stehen schon darüber. "
        "Maximal 8 kurze Zeilen in dieser Reihenfolge: Einordnung der Woche, Belastung und Erholung "
        "(CTL/TSB, HRV und Ruhepuls gegenüber den 4 Wochen davor), Auffälligkeiten oder Beschwerden, "
        "dann 'Vorschlag nächste Woche:' mit 1-3 konkreten Punkten bezogen auf die bereits geplanten "
        "Einheiten. Der Vorschlag wird nicht automatisch umgesetzt.\n\n"
        f"Daten:\n{json.dumps(summary, ensure_ascii=False, indent=1)}"
    )
    text = llm.complete(prompt, cfg) or "Details siehe Einheiten. Vorschlag nächste Woche: im Chat besprechen."
    api.create_note(sun.isoformat(), name, f"{head}\n\n{text}")
    print(f"[weekly] {name} angelegt")
