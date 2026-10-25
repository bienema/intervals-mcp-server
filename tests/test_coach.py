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
