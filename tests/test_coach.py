"""Tests for the coach module (no network)."""
from datetime import date
from pathlib import Path

import yaml

from coach import jobs
from coach.hrfilter import artifact_mask
from coach.scoring import Zones, score_activity, score_line
from coach.workout import parse_workout, planned_duration

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "coach.yaml").read_text())
ZONES = Zones(lthr=180, hr_bounds=[152, 161, 170, 179, 184, 189, 193], threshold_pace=250)

EASY = """Ziel: Test. Locker.

Warmup
- 10m 5:55-6:20/km pace

Main
- 25m 5:45-6:10/km pace

Steigerungen 4x
- 20s 3:50-4:00/km pace
- 70s intensity=rest

Cooldown
- 4m 6:00-6:30/km pace"""

INTERVAL = """Ziel: VO2max.

Warmup
- 12m 5:00-5:30/km pace

Main 6x
- 3m 3:42-3:48/km pace
- 2m intensity=rest

Cooldown
- 10m 5:30-6:00/km pace"""


def stream(n, hr, speed):
    return {"time": list(range(n)), "heartrate": [hr] * n, "velocity_smooth": [speed] * n}


# -- parser ---------------------------------------------------------------------
def test_parse_easy_with_strides():
    steps = parse_workout(EASY, "easy")
    assert [s.role for s in steps] == ["wu", "steady"] + ["stride", "rest"] * 4 + ["cd"]
    assert steps[1].pace_fast == 345 and steps[1].pace_slow == 370
    assert planned_duration(steps) == 10 * 60 + 25 * 60 + 4 * 90 + 4 * 60


def test_parse_interval_repeats():
    steps = parse_workout(INTERVAL, "interval")
    assert sum(s.role == "work" for s in steps) == 6
    assert sum(s.role == "rest" for s in steps) == 6
    assert steps[1].rep == 1 and steps[-2].rep == 6


def test_parse_distance_steps():
    steps = parse_workout("Main 5x\n- 1km 4:00-4:05/km pace\n- 400mtr 5:30/km pace", "interval")
    assert steps[0].distance_m == 1000 and steps[1].distance_m == 400


# -- hr filter ------------------------------------------------------------------
def test_filter_flags_cadence_lock_but_not_recovery_jog():
    z1 = 1000 / ZONES.z1_pace
    # 0-299 easy jog HR 140 | 300-399 cadence lock 195 at easy speed
    t = list(range(700))
    hr = [140] * 300 + [195] * 100 + [140] * 300
    v = [2.8] * 700
    mask = artifact_mask(t, hr, v, 180, z1)
    assert all(mask[300:400]) and not any(mask[:290])
    # hard rep (fast) followed by slow jog with legitimately high HR -> not flagged
    hr2 = [178] * 700
    v2 = [4.3] * 180 + [2.6] * 120 + [4.3] * 400
    mask2 = artifact_mask(t, hr2, v2, 180, z1, hard_speed=1000 / (250 / 0.943))
    assert not any(mask2[180:300])


# -- scoring ------------------------------------------------------------------------
def test_easy_run_perfect_score():
    steps = parse_workout(EASY, "easy")
    dur = planned_duration(steps)
    act = {"moving_time": dur, "distance": dur / 355 * 1000, "decoupling": 3.0}
    res = score_activity("easy", steps, act, [], stream(int(dur), 140, 2.8), ZONES, CFG)
    assert res.total >= 95 and res.ampel == "🟢", score_line(res)


def test_easy_run_too_hard_is_penalised():
    steps = parse_workout(EASY, "easy")
    dur = planned_duration(steps)
    act = {"moving_time": dur, "distance": dur / 290 * 1000, "decoupling": 8.0}
    res = score_activity("easy", steps, act, [], stream(int(dur), 165, 3.45), ZONES, CFG)
    assert res.total < 70 and res.ampel == "🔴", score_line(res)


def _interval_ivs(work_pace):
    ivs, idx = [{"type": "RECOVERY", "start_index": 0, "moving_time": 720, "average_speed": 3.2}], 720
    for p in work_pace:
        ivs.append({"type": "WORK", "start_index": idx, "end_index": idx + 180, "moving_time": 180,
                    "average_speed": 1000 / p})
        ivs.append({"type": "RECOVERY", "start_index": idx + 180, "moving_time": 120, "elapsed_time": 120,
                    "average_speed": 2.5})
        idx += 300
    ivs.append({"type": "RECOVERY", "start_index": idx, "moving_time": 600, "average_speed": 3.0})
    return ivs


def test_interval_score_good_and_bad():
    steps = parse_workout(INTERVAL, "interval")
    act = {"moving_time": 3000, "distance": 10000}
    good = score_activity("interval", steps, act, _interval_ivs([225, 226, 224, 226, 225, 227]),
                          stream(3000, 170, 3.5), ZONES, CFG)
    assert good.total >= 95, score_line(good)
    bad = score_activity("interval", steps, act, _interval_ivs([225, 232, 240, 250]),
                         stream(3000, 170, 3.5), ZONES, CFG)
    assert bad.total < 70, score_line(bad)


def test_unusable_hr_suspends_hr_criteria():
    steps = parse_workout(EASY, "easy")
    dur = planned_duration(steps)
    act = {"moving_time": dur, "distance": dur / 355 * 1000, "decoupling": 3.0}
    s = stream(int(dur), 196, 2.8)          # whole run "at max HR" at easy speed = sensor error
    res = score_activity("easy", steps, act, [], s, ZONES, CFG)
    hr = next(c for c in res.criteria if c.key == "hr_cap")
    assert hr.value is None and res.total is not None and any("unplausibel" in n for n in res.notes)


def test_unplanned_run_has_no_score():
    res = score_activity("unplanned", [], {"moving_time": 1800, "distance": 5000}, [],
                         stream(1800, 140, 2.8), ZONES, CFG)
    assert res.total is None


# -- readiness -----------------------------------------------------------------------
def test_readiness_traffic_light():
    d = date(2026, 10, 27)
    base = [{"id": f"2026-10-{20 + i:02d}", "hrv": 60, "restingHR": 45, "ctl": 40, "atl": 42} for i in range(7)]
    ok = base + [{"id": "2026-10-27", "hrv": 61, "restingHR": 45, "sleepSecs": 27000, "ctl": 40, "atl": 45}]
    assert jobs.readiness(ok, d)[0] == "🟢"
    bad = base + [{"id": "2026-10-27", "hrv": 45, "restingHR": 53, "sleepSecs": 18000, "ctl": 40, "atl": 45}]
    assert jobs.readiness(bad, d)[0] == "🔴"


# -- job smoke test with a fake API -----------------------------------------------------
class FakeAPI:
    def __init__(self):
        self.posted, self.updated, self.notes = [], [], []

    def run_settings(self):
        return {"lthr": 180, "hr_zones": ZONES.hr_bounds, "threshold_pace": 4.0}

    def activities(self, *_):
        return [{"id": "i1", "type": "Run", "moving_time": 2400}]

    def activity(self, _):
        return {"id": "i1", "type": "Run", "moving_time": 2580, "distance": 7270, "decoupling": 2.0,
                "paired_event_id": 9, "name": "Morgenlauf"}

    def event(self, _):
        return {"id": 9, "name": "Claude Test", "tags": ["easy"], "description": EASY}

    def messages(self, _):
        return []

    def intervals(self, _):
        return []

    def streams(self, _):
        return stream(2580, 140, 2.8)

    def post_message(self, aid, content):
        self.posted.append((aid, content))


def test_debrief_job_posts_once(monkeypatch):
    monkeypatch.setattr(jobs.llm, "complete", lambda *a, **k: None)   # fallback text
    api = FakeAPI()
    jobs.debrief(api, CFG)
    assert len(api.posted) == 1
    content = api.posted[0][1]
    assert "Coach-Debrief · Claude Test" in content and "Score " in content and "Gut:" in content
    api.messages = lambda _: [{"content": content}]
    jobs.debrief(api, CFG)
    assert len(api.posted) == 1      # idempotent


def test_briefing_and_weekly_smoke(monkeypatch):
    monkeypatch.setattr(jobs.llm, "complete", lambda *a, **k: None)
    api = FakeAPI()
    d = date(2026, 11, 1)  # Sunday
    well = [{"id": f"2026-10-{26 + i:02d}", "hrv": 60, "restingHR": 45, "ctl": 40, "atl": 42,
             "sleepSecs": 26000} for i in range(6)] + [{"id": "2026-11-01", "hrv": 58, "restingHR": 46,
                                                        "ctl": 41, "atl": 44, "sleepSecs": 25000}]
    api.wellness = lambda *a: well
    api.events = lambda o, n, c=None: ([{"id": 9, "name": "Long Run", "type": "Run", "tags": ["long"],
                                          "description": "Ziel: Grundlage.\n\nMain\n- 90m 5:40-6:10/km pace"}]
                                        if c == "WORKOUT" else [])
    api.update_event_description = lambda eid, desc: api.updated.append(desc)
    api.create_note = lambda day, name, desc: api.notes.append((day, name, desc))
    api.activities = lambda *a: [{"id": "i1", "type": "Run", "distance": 18000, "moving_time": 5400,
                                  "start_date_local": "2026-11-01T09:00:00", "name": "Long Run"}]
    api.messages = lambda _: [{"content": "🏃 Coach-Debrief · Long Run\nScore 88/100 🟢 · x"}]

    jobs.briefing(api, CFG, day=d)
    assert api.updated and api.updated[0].startswith("Tagesform (auto): 🟢")
    assert "Ziel: Grundlage." in api.updated[0]

    jobs.weekly(api, CFG, day=d)
    day, name, desc = api.notes[0]
    assert day == "2026-11-01" and name == "Wochenrückblick KW 44"
    assert "18.0 km" in desc and "Score-Schnitt: 88" in desc


# -- llm provider ---------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_gemini_falls_through_models_and_strips_markdown(monkeypatch):
    from coach import llm
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if "flash-latest" in url and "lite" not in url:
            return _Resp(404, {})
        return _Resp(200, {"candidates": [{"content": {"parts": [
            {"text": "intern", "thought": True}, {"text": "Gut: **Pace** sauber"}]}}]})

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.complete("p", {"llm": {"provider": "gemini"}, "goal": "Sub 3"})
    assert out == "Gut: Pace sauber" and len(calls) == 2


def test_llm_without_key_or_provider_returns_none(monkeypatch):
    from coach import llm
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm.complete("p", {"llm": {"provider": "gemini"}}) is None
    assert llm.complete("p", {"llm": {"provider": "none"}}) is None


# -- facts / plan rendering --------------------------------------------------------------
PYRAMID = """Ziel: Tempohärte.
Zielwerte: siehe unten

Warmup
- 1km 5:00-5:30/km pace
- 90s intensity=rest

Block 1 4x
- 600mtr 3:50/km pace
- 90s intensity=rest

Block 2 3x
- 600mtr 3:45/km pace
- 90s intensity=rest

Cooldown
- 1km 5:30-6:00/km pace"""


def test_render_plan_runna_style():
    from coach.facts import render_plan, split_description
    txt = render_plan(PYRAMID)
    assert "Aufwärmen: 1 km @ 5:00–5:30/km, 90 s Pause" in txt
    assert "4× Block 1:\n• 600 m @ 3:50/km\n• 90 s Pause" in txt
    assert "Auslaufen: 1 km @ 5:30–6:00/km" in txt
    intro, groups = split_description(PYRAMID)
    assert intro == ["Ziel: Tempohärte.", "Zielwerte: siehe unten"] and len(groups) == 4


def test_render_actual_lists_reps_with_check():
    from coach.facts import render_actual
    steps = parse_workout(PYRAMID, "interval")
    ivs, idx = [], 0
    for s in steps:
        pace = 330 if s.role in ("wu", "cd") else (s.pace_fast + 2 if s.pace_fast else 600)
        if s.block == 2 and s.rep == 4 and s.role == "work":
            pace = 245                                    # one rep too slow
        dist = s.distance_m or (s.duration_s / pace * 1000)
        ivs.append({"start_index": idx, "distance": dist, "average_speed": 1000 / pace,
                    "average_heartrate": 165, "max_heartrate": 172,
                    "type": "WORK" if s.role == "work" else "RECOVERY"})
        idx += 100
    act = {"distance": 8000, "moving_time": 3000, "average_heartrate": 152, "max_heartrate": 178,
           "total_elevation_gain": 40, "average_cadence": 83, "icu_training_load": 62, "icu_rpe": 7}
    txt = render_actual(act, ivs, steps, 0.025)
    assert txt.startswith("Gesamt: 8 km · 50 min · Ø 6:15/km · Ø HF 152 (max 178)")
    assert "• Wdh 1/4 · 600 m · 3:52/km · HF 165/172 ✓" in txt
    assert "• Wdh 4/4 · 600 m · 4:05/km · HF 165/172 ✗ (Soll 3:50/km)" in txt
    assert "Pause" not in txt


# -- google calendar sync ------------------------------------------------------------------
class FakeCal:
    def __init__(self, have=None):
        self.have, self.calls = have or {}, []

    def managed(self, *_):
        return self.have

    def insert(self, body):
        self.calls.append(("insert", body))

    def update(self, eid, body):
        self.calls.append(("update", eid, body))

    def delete(self, eid):
        self.calls.append(("delete", eid))


def _cal_api():
    api = FakeAPI()
    api.activities = lambda *a: [
        {"id": "i1", "type": "Run", "paired_event_id": 9, "start_date_local": "2026-10-26T07:05:00",
         "elapsed_time": 2700, "distance": 7000, "moving_time": 2600},
        {"id": "i2", "type": "Run", "start_date_local": "2026-10-25T11:24:49", "elapsed_time": 8600,
         "distance": 23400, "moving_time": 8546}]
    api.messages = lambda aid: ([{"content": "🏃 Coach-Debrief · Claude Test\nScore 92/100 🟢 · x\n\nIst-Daten\nGesamt: 7 km"}]
                                if aid == "i1" else [])
    api.events = lambda o, n, c=None: {
        "WORKOUT": [{"id": 9, "type": "Run", "name": "Claude Test", "start_date_local": "2026-10-26T00:00:00",
                     "description": EASY},
                    {"id": 10, "type": "Run", "name": "Long Run", "start_date_local": "2026-10-31T00:00:00",
                     "description": "Ziel: Grundlage.\n\nMain\n- 90m 5:40-6:10/km pace"}],
        "NOTE": [{"id": 11, "name": "Wochenrückblick KW 43", "start_date_local": "2026-10-25T00:00:00",
                  "description": "Umfang: 35 km"}]}.get(c, [])
    return api


def test_calendar_sync_builds_expected_events():
    from coach.gcal import sync
    cal = FakeCal()
    stats = sync(_cal_api(), cal, CFG, date(2026, 10, 26))
    bodies = {c[1]["extendedProperties"]["private"]["icu_key"]: c[1] for c in cal.calls}
    assert stats["neu"] == 4
    done = bodies["event-9"]
    assert done["summary"] == "✅ 🟢 92 · Claude Test" and done["colorId"] == "10"
    assert done["start"]["dateTime"] == "2026-10-26T07:05:00" and "— Plan —" in done["description"]
    planned = bodies["event-10"]
    assert planned["summary"].startswith("🏃 Long Run · ≈") and "90 min" in planned["summary"]
    assert planned["description"].startswith("Ziel: Grundlage.")
    assert planned["start"]["dateTime"] == "2026-10-31T10:00:00"          # Saturday default
    assert planned["end"]["dateTime"] == "2026-10-31T11:30:00"
    assert bodies["activity-i2"]["summary"] == "✅ Lauf · 23,4 km"
    assert bodies["note-11"]["summary"] == "📊 Wochenrückblick KW 43"


def test_calendar_sync_is_idempotent_and_deletes_removed_plans():
    from coach.gcal import sync
    cal = FakeCal()
    sync(_cal_api(), cal, CFG, date(2026, 10, 26))
    have = {c[1]["extendedProperties"]["private"]["icu_key"]: {**c[1], "id": f"g{k}"}
            for k, c in enumerate(cal.calls)}
    have["event-99"] = {"id": "gone", "extendedProperties": {"private": {"icu_key": "event-99"}}}
    cal2 = FakeCal(have)
    stats = sync(_cal_api(), cal2, CFG, date(2026, 10, 26))
    assert stats == {"neu": 0, "geändert": 0, "gelöscht": 1, "unverändert": 4}
    assert cal2.calls == [("delete", "gone")]


def test_planned_slot_weekday_default_and_explicit_time():
    from coach.gcal import _planned_slot
    tue = _planned_slot({"start_date_local": "2026-10-27T00:00:00"}, "2026-10-27", 2700, CFG, "Europe/Berlin")
    assert tue["start"]["dateTime"] == "2026-10-27T16:30:00" and tue["end"]["dateTime"] == "2026-10-27T17:15:00"
    own = _planned_slot({"start_date_local": "2026-10-27T07:15:00"}, "2026-10-27", 2700, CFG, "Europe/Berlin")
    assert own["start"]["dateTime"] == "2026-10-27T07:15:00"
