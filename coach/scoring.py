"""Deterministic success score (0-100) per workout type.

Weights and tolerances come from config/coach.yaml. Every criterion yields a
value in [0, 1] or None (not measurable). The total is re-normalised over the
measurable criteria, so a missing HR strap does not punish the athlete.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .hrfilter import artifact_mask, artifact_share
from .workout import Step, planned_duration


# -- helpers ------------------------------------------------------------------
def lin(x: float, full: float, zero: float) -> float:
    """1.0 at `full`, 0.0 at `zero`, linear in between (works in both directions)."""
    if full == zero:
        return 1.0 if x == full else 0.0
    t = (x - zero) / (full - zero)
    return max(0.0, min(1.0, t))


def volume_score(ratio: float) -> float:
    if 0.95 <= ratio <= 1.15:
        return 1.0
    return lin(ratio, 0.95, 0.6) if ratio < 0.95 else lin(ratio, 1.15, 1.5)


def pace_of(iv: dict) -> float | None:
    sp = iv.get("average_speed") or 0
    return 1000.0 / sp if sp > 0.3 else None


def in_corridor(pace: float, st: Step, tol: float) -> bool:
    return st.pace_fast * (1 - tol) <= pace <= st.pace_slow * (1 + tol)


def fmt_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "–"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


@dataclass
class Zones:
    lthr: float
    hr_bounds: list[float]          # upper bound per zone, Z1..Z7
    threshold_pace: float           # s/km

    @classmethod
    def from_settings(cls, s: dict) -> "Zones":
        tp = s.get("threshold_pace") or 0
        return cls(lthr=s.get("lthr") or 0, hr_bounds=list(s.get("hr_zones") or []),
                   threshold_pace=1000.0 / tp if tp else 250.0)

    def hr_cap(self, zone: str) -> float:
        idx = int(zone.upper().lstrip("Z")) - 1
        return self.hr_bounds[idx]

    @property
    def z1_pace(self) -> float:
        """Slowest bound of pace Z2 (= Z1 limit) in s/km; intervals.icu default 77.5 %."""
        return self.threshold_pace / 0.775


@dataclass
class Criterion:
    key: str
    label: str
    weight: float
    value: float | None
    detail: str = ""

    @property
    def points(self) -> float | None:
        return None if self.value is None else round(self.value * self.weight, 1)


@dataclass
class ScoreResult:
    workout_type: str
    total: int | None
    ampel: str
    criteria: list[Criterion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def ampel_for(total: int | None, cfg: dict) -> str:
    if total is None:
        return "⚪"
    a = cfg["ampel"]
    return "🟢" if total >= a["green"] else "🟡" if total >= a["yellow"] else "🔴"


# -- matching planned steps to recorded intervals -------------------------------
def match(steps: list[Step], ivs: list[dict]) -> tuple[list[tuple[Step, dict]], bool]:
    """Return (pairs, certain). 1:1 if counts agree, else WORK laps <-> work steps."""
    ivs = sorted(ivs, key=lambda i: i.get("start_index", 0))
    if steps and len(ivs) == len(steps):
        return list(zip(steps, ivs)), True
    work_steps = [s for s in steps if s.role in ("work", "stride")]
    work_ivs = [i for i in ivs if (i.get("type") or "").upper() == "WORK"]
    return list(zip(work_steps, work_ivs)), False


# -- stream helpers ---------------------------------------------------------------
class Streams:
    def __init__(self, raw: dict, zones: Zones, cfg: dict):
        self.t = raw.get("time") or []
        self.hr = raw.get("heartrate") or []
        self.v = raw.get("velocity_smooth") or []
        f = cfg["hr_filter"]
        self.mask = artifact_mask(self.t, self.hr, self.v, zones.lthr, 1000.0 / zones.z1_pace,
                                  hard_speed=1000.0 / (zones.threshold_pace / 0.943),
                                  spike_bpm=f["spike_bpm"], margin=f["margin_bpm"],
                                  min_seconds=f["min_seconds"])
        self.artifact_share = artifact_share(self.mask)

    def valid_hr(self, start: int = 0, end: int | None = None, skip_s: float = 0) -> list[float]:
        end = len(self.hr) if end is None else min(end, len(self.hr))
        t0 = self.t[start] if self.t and start < len(self.t) else 0
        out = []
        for i in range(start, end):
            if self.mask[i]:
                continue
            if skip_s and self.t and self.t[i] - t0 < skip_s:
                continue
            out.append(self.hr[i])
        return out


# -- main entry -------------------------------------------------------------------
def score_activity(workout_type: str, steps: list[Step], activity: dict, ivs: list[dict],
                   streams_raw: dict, zones: Zones, cfg: dict) -> ScoreResult:
    tol = cfg["pace_tolerance"]
    weights = cfg["weights"].get(workout_type)
    st = Streams(streams_raw, zones, cfg)
    moving = activity.get("moving_time") or 0
    dist = activity.get("distance") or 0
    notes: list[str] = []
    metrics = {
        "distance_km": round(dist / 1000, 2),
        "moving_min": round(moving / 60, 1),
        "avg_pace": fmt_pace(moving / (dist / 1000)) if dist else "–",
        "avg_hr": activity.get("average_heartrate"),
        "decoupling_pct": activity.get("decoupling"),
        "rpe": activity.get("icu_rpe"),
        "feel": activity.get("feel"),
        "hr_artifact_pct": round(st.artifact_share * 100, 1),
    }
    hr_usable = bool(st.hr) and st.artifact_share <= cfg["hr_filter"]["max_artifact_share"]
    if st.hr and not hr_usable:
        notes.append(f"Pulsdaten zu {metrics['hr_artifact_pct']} % unplausibel, Pulskriterien ausgesetzt.")
    elif st.artifact_share > 0.02:
        notes.append(f"{metrics['hr_artifact_pct']} % der Pulswerte als Sensor-Ausreißer ignoriert.")

    if weights is None or not steps:
        notes.append("Kein geplantes Workout bzw. unbekannter Typ, daher kein Score.")
        return ScoreResult(workout_type, None, "⚪", [], notes, metrics)

    pairs, certain = match(steps, ivs)
    needs_laps = any(s.role in ("work", "stride") for s in steps) or workout_type in ("threshold", "mp")
    if not ivs and needs_laps:
        notes.append("Keine Rundendaten vorhanden, rundenbasierte Kriterien ausgesetzt.")
    elif ivs and not certain and needs_laps:
        notes.append("Runden passen nicht 1:1 zum Plan, Zuordnung nur über Arbeitsintervalle.")
    plan_s = planned_duration(steps, zones.z1_pace * 1.08)
    W = lambda k: weights.get(k, 0)  # noqa: E731
    crit: list[Criterion] = []
    dec = activity.get("decoupling")
    dec_val = lin(abs(dec), cfg["decoupling_full"], cfg["decoupling_zero"]) if dec is not None else None
    dec_detail = f"{dec:.1f} %" if dec is not None else "nicht verfügbar"

    work_pairs = [(s, i) for s, i in pairs if s.role in ("work", "steady") and s.pace_fast and pace_of(i)]

    if workout_type in ("easy", "recovery", "long"):
        cap_zone = cfg["hr_caps"][workout_type]
        cap = zones.hr_cap(cap_zone)
        hrs = st.valid_hr(skip_s=300) if hr_usable else []
        frac = sum(h <= cap for h in hrs) / len(hrs) if hrs else None
        hr_val = lin(frac, 0.95, 0.70) if frac is not None else None
        hr_key = "hr_cap" if workout_type != "long" else "hr_base"
        crit.append(Criterion(hr_key, f"Puls ≤ {cap:.0f} ({cap_zone})", W(hr_key), hr_val,
                              f"{frac * 100:.0f} % der Zeit" if frac is not None else "keine Pulsdaten"))
        # volume
        ratio = moving / plan_s if plan_s else None
        crit.append(Criterion("volume", "Umfang", W("volume"),
                              volume_score(ratio) if ratio else None,
                              f"{moving / 60:.0f} von {plan_s / 60:.0f} min" if ratio else ""))
        crit.append(Criterion("decoupling", "Entkopplung", W("decoupling"), dec_val, dec_detail))
        if workout_type in ("easy", "recovery"):
            stride = [(s, i) for s, i in pairs if s.role == "stride"]
            s_time = sum(i.get("moving_time") or 0 for _, i in stride)
            s_dist = sum(i.get("distance") or 0 for _, i in stride)
            base_pace = (moving - s_time) / ((dist - s_dist) / 1000) if dist - s_dist > 0 else None
            targets = [s.pace_fast for s in steps if s.role in ("steady", "wu", "cd") and s.pace_fast]
            limit = min(targets) if targets else zones.z1_pace
            if base_pace:
                v = 1.0 if base_pace >= limit * (1 - tol) else lin(base_pace, limit * (1 - tol), limit * 0.90)
                crit.append(Criterion("pace", "Pace nicht zu schnell", W("pace"), v,
                                      f"{fmt_pace(base_pace)} (Grenze {fmt_pace(limit)})"))
        else:  # long: finish
            main = [s for s in steps if s.section == "main" and s.pace_fast]
            last_block = main[-1].block if main else None
            fin = [(s, i) for s, i in pairs if s.block == last_block and s.block != main[0].block] if main else []
            if fin and certain:
                ok = sum(in_corridor(pace_of(i), s, tol) for s, i in fin if pace_of(i)) / len(fin)
                crit.append(Criterion("finish", "Finish nach Plan", W("finish"), ok,
                                      ", ".join(fmt_pace(pace_of(i)) for _, i in fin)))
            else:
                crit.append(Criterion("finish", "Finish nach Plan", W("finish"), None, "kein separates Finish geplant"))

    elif workout_type == "interval":
        work = [(s, i) for s, i in pairs if s.role == "work" and pace_of(i)]
        planned_reps = sum(s.role == "work" for s in steps)
        paces = [pace_of(i) for _, i in work]
        if work:
            ok = sum(in_corridor(p, s, tol) for (s, _), p in zip(work, paces)) / len(work)
            crit.append(Criterion("corridor", "Wiederholungen im Korridor", W("corridor"), ok,
                                  ", ".join(fmt_pace(p) for p in paces)))
            cv = statistics.pstdev(paces) / statistics.mean(paces) if len(paces) > 1 else 0
            crit.append(Criterion("consistency", "Konstanz", W("consistency"), lin(cv, 0.02, 0.06),
                                  f"Streuung {cv * 100:.1f} %"))
        done = sum(1 for s, i in work if not s.duration_s or (i.get("moving_time") or 0) >= 0.8 * s.duration_s)
        crit.append(Criterion("completion", "Wiederholungen absolviert", W("completion"),
                              done / planned_reps if planned_reps else None, f"{done}/{planned_reps}"))
        rests = [(s, i) for s, i in pairs if s.role == "rest" and s.duration_s]
        if rests and certain:
            ok = sum(abs((i.get("elapsed_time") or i.get("moving_time") or 0) - s.duration_s) <= 0.25 * s.duration_s
                     for s, i in rests) / len(rests)
            crit.append(Criterion("recovery", "Pausen eingehalten", W("recovery"), ok, f"{ok * 100:.0f} %"))
        else:
            crit.append(Criterion("recovery", "Pausen eingehalten", W("recovery"), None, "nicht zuordenbar"))

    elif workout_type in ("threshold", "mp"):
        tw = sum(i.get("moving_time") or 0 for _, i in work_pairs)
        in_t = sum((i.get("moving_time") or 0) for s, i in work_pairs if in_corridor(pace_of(i), s, tol))
        crit.append(Criterion("corridor", "Zeit im Pace-Korridor", W("corridor"),
                              in_t / tw if tw else None,
                              ", ".join(fmt_pace(pace_of(i)) for _, i in work_pairs)))
        plan_work = sum(s.duration_s or 0 for s in steps if s.role in ("work", "steady") and s.pace_fast)
        crit.append(Criterion("volume", "Umfang Hauptteil", W("volume"),
                              volume_score(tw / plan_work) if plan_work and tw else None,
                              f"{tw / 60:.0f} von {plan_work / 60:.0f} min" if plan_work else ""))
        hr_work = []
        if hr_usable:
            for _, i in work_pairs:
                hr_work += st.valid_hr(i.get("start_index", 0), i.get("end_index"))
        if workout_type == "threshold":
            paces = [pace_of(i) for _, i in work_pairs]
            if len(hr_work) > 20:
                h = len(hr_work) // 2
                drift = (statistics.mean(hr_work[h:]) - statistics.mean(hr_work[:h])) / statistics.mean(hr_work[:h])
                crit.append(Criterion("hr_drift", "Pulsdrift", W("hr_drift"), lin(drift, 0.03, 0.08),
                                      f"{drift * 100:+.1f} %"))
            else:
                crit.append(Criterion("hr_drift", "Pulsdrift", W("hr_drift"), None, "keine Pulsdaten"))
            cv = statistics.pstdev(paces) / statistics.mean(paces) if len(paces) > 1 else None
            crit.append(Criterion("consistency", "Konstanz", W("consistency"),
                                  lin(cv, 0.02, 0.06) if cv is not None else None,
                                  f"Streuung {cv * 100:.1f} %" if cv is not None else "nur ein Block"))
        else:
            crit.append(Criterion("decoupling", "Entkopplung", W("decoupling"), dec_val, dec_detail))
            cap = zones.hr_cap("Z4")
            frac = sum(x <= cap for x in hr_work) / len(hr_work) if hr_work else None
            crit.append(Criterion("hr", f"Puls ≤ {cap:.0f} im MP-Teil", W("hr"),
                                  lin(frac, 0.9, 0.6) if frac is not None else None,
                                  f"{frac * 100:.0f} %" if frac is not None else "keine Pulsdaten"))

    measurable = [c for c in crit if c.value is not None and c.weight]
    wsum = sum(c.weight for c in measurable)
    total = round(sum(c.value * c.weight for c in measurable) / wsum * 100) if wsum else None
    return ScoreResult(workout_type, total, ampel_for(total, cfg), crit, notes, metrics)


def score_line(res: ScoreResult) -> str:
    if res.total is None:
        return "Score: –"
    parts = [f"{c.label} {c.points:g}/{c.weight:g}" for c in res.criteria if c.value is not None]
    return f"Score {res.total}/100 {res.ampel} · " + " | ".join(parts)
