"""Human-readable workout plan (Runna style) and actual facts per section."""
from __future__ import annotations

import re

from .scoring import Zones, in_corridor, match, pace_of
from .workout import COOLDOWN_NAMES, WARMUP_NAMES, Step, parse_step

_REPEAT = re.compile(r"\s*(\d+)\s*x\s*$", re.I)
GENERIC = ("main", "hauptteil", "main set", "set")


# -- formatting ------------------------------------------------------------------
def fmt_pace(sec: float | None) -> str:
    if not sec:
        return "–"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}/km"


def fmt_dist(m: float | None) -> str:
    if not m:
        return "–"
    if m < 1000:
        return f"{m:.0f} m"
    return f"{m / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " km"


def fmt_dur(s: float | None) -> str:
    if not s:
        return "–"
    s = int(round(s))
    if s <= 120 and s % 60:
        return f"{s} s"
    if s % 60 == 0 and s < 5 * 3600:
        return f"{s // 60} min"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d} h" if h else f"{m}:{sec:02d} min"


def _human_step(line: str) -> str:
    st = parse_step(line)
    if not st:
        return line.lstrip("- ").strip()
    amount = fmt_dist(st["distance_m"]) if st.get("distance_m") else fmt_dur(st.get("duration_s"))
    if st["rest"]:
        return f"{amount} Pause"
    if st.get("pace_fast"):
        a, b = st["pace_fast"], st["pace_slow"]
        pace = fmt_pace(a) if a == b else f"{fmt_pace(a)[:-3]}–{fmt_pace(b)}"
        return f"{amount} @ {pace}"
    rest = line.lstrip("- ").split(None, 1)
    return f"{amount} {rest[1]}" if len(rest) > 1 and "free" not in rest[1] else f"{amount} locker"


# -- plan --------------------------------------------------------------------------
def split_description(desc: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Return (text lines before the structure, [(header, step_lines), ...])."""
    lines = [ln.rstrip() for ln in (desc or "").splitlines()]
    first_step = next((i for i, ln in enumerate(lines) if ln.strip().startswith("-")), None)
    if first_step is None:
        return [ln for ln in lines if ln.strip()], []
    hdr = next((i for i in range(first_step - 1, -1, -1) if lines[i].strip()), None)
    cut = hdr if hdr is not None else first_step
    intro = [ln for ln in lines[:cut] if ln.strip()]
    groups: list[tuple[str, list[str]]] = []
    header = lines[hdr].strip() if hdr is not None else "Main"
    steps: list[str] = []
    for ln in lines[first_step:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("-"):
            steps.append(s)
        else:
            if steps:
                groups.append((header, steps))
            header, steps = s, []
    if steps:
        groups.append((header, steps))
    return intro, groups


def render_plan(desc: str) -> str:
    _, groups = split_description(desc)
    out: list[str] = []
    for header, steps in groups:
        low = header.lower()
        rm = _REPEAT.search(header)
        reps = int(rm.group(1)) if rm else 1
        label = _REPEAT.sub("", header).strip()
        human = [_human_step(s) for s in steps]
        if any(low.startswith(n) for n in WARMUP_NAMES):
            out.append("Aufwärmen: " + ", ".join(human))
        elif any(low.startswith(n) for n in COOLDOWN_NAMES):
            out.append("Auslaufen: " + ", ".join(human))
        elif reps > 1:
            out.append(f"{reps} Wiederholungen von:" if label.lower() in GENERIC
                       else f"{reps}× {label}:")
            out += [f"• {h}" for h in human]
        else:
            prefix = "" if label.lower() in GENERIC else f"{label}: "
            out.append(prefix + ", ".join(human))
        out.append("")
    return "\n".join(out).strip()


def plan_totals(steps: list[Step], zones: Zones) -> tuple[float, float]:
    """Estimated (distance_m, duration_s) of the plan."""
    dist = dur = 0.0
    easy = zones.z1_pace * 1.08
    for s in steps:
        pace = (s.pace_fast + s.pace_slow) / 2 if s.pace_fast else (easy if s.role != "rest" else None)
        if s.distance_m:
            dist += s.distance_m
            dur += s.distance_m / 1000 * (pace or 600)
        elif s.duration_s:
            dur += s.duration_s
            if pace:
                dist += s.duration_s / pace * 1000
    return dist, dur


# -- actual -------------------------------------------------------------------------
def render_actual(act: dict, ivs: list[dict], steps: list[Step], tol: float, max_lines: int = 30) -> str:
    dist = act.get("distance") or 0
    mov = act.get("moving_time") or 0
    parts = [fmt_dist(dist), fmt_dur(mov)]
    if dist:
        parts.append(f"Ø {fmt_pace(mov / (dist / 1000))}")
    if act.get("average_heartrate"):
        hr = f"Ø HF {act['average_heartrate']:.0f}"
        if act.get("max_heartrate"):
            hr += f" (max {act['max_heartrate']:.0f})"
        parts.append(hr)
    if act.get("total_elevation_gain"):
        parts.append(f"↑ {act['total_elevation_gain']:.0f} hm")
    if act.get("average_cadence"):
        parts.append(f"Kadenz {act['average_cadence'] * 2:.0f} spm")
    if act.get("icu_training_load"):
        parts.append(f"Load {act['icu_training_load']:.0f}")
    if act.get("icu_rpe"):
        parts.append(f"RPE {act['icu_rpe']}")
    lines = ["Gesamt: " + " · ".join(parts)]

    def lap(label: str, iv: dict, st: Step | None = None) -> str:
        p = pace_of(iv)
        bits = [label, fmt_dist(iv.get("distance")), fmt_pace(p)]
        if iv.get("average_heartrate"):
            hr = f"HF {iv['average_heartrate']:.0f}"
            if iv.get("max_heartrate"):
                hr += f"/{iv['max_heartrate']:.0f}"
            bits.append(hr)
        line = " · ".join(bits)
        if st is not None and st.pace_fast and p and st.role in ("work", "steady", "stride"):
            line += " ✓" if in_corridor(p, st, tol) else f" ✗ (Soll {fmt_pace(st.pace_fast)})"
        return line

    pairs, certain = match(steps, ivs) if steps else ([], False)
    laps: list[str] = []
    if pairs and certain:
        block_reps: dict[int, int] = {}
        for s in steps:
            if s.rep:
                block_reps[s.block] = max(block_reps.get(s.block, 0), s.rep)
        for st, iv in pairs:
            if st.role == "rest":
                continue
            label = {"wu": "Aufwärmen", "cd": "Auslaufen", "steady": "Dauerlauf"}.get(st.role)
            if not label:
                label = f"Wdh {st.rep}/{block_reps.get(st.block, st.rep)}"
            laps.append(lap(label, iv, st))
    elif pairs:
        laps = [lap(f"Intervall {k}", iv, st) for k, (st, iv) in enumerate(pairs, 1)]
    else:
        work = [iv for iv in sorted(ivs, key=lambda i: i.get("start_index", 0))
                if (iv.get("type") or "").upper() == "WORK"]
        if 1 < len(work) <= 40:
            laps = [lap(f"Intervall {k}", iv) for k, iv in enumerate(work, 1)]
    if laps:
        lines.append("Abschnitte:")
        lines += [f"• {ln}" for ln in laps[:max_lines]]
        if len(laps) > max_lines:
            lines.append(f"• … {len(laps) - max_lines} weitere")
    return "\n".join(lines)
