"""Parse intervals.icu workout text (the syntax the coach writes) into planned steps.

Supported step lines:
  - 10m 5:55-6:20/km pace      - 20s 3:50/km pace      - 70s intensity=rest
  - 1km 4:00-4:05/km pace      - 400mtr 3:45/km pace   - 1m30s free
Section headers are any non-step line; "Name Nx" starts a repeat block.
Lines before the first step (briefing text) are ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

WARMUP_NAMES = ("warmup", "warm-up", "einlaufen", "warm up")
COOLDOWN_NAMES = ("cooldown", "cool-down", "auslaufen", "cool down")

_DUR = re.compile(r"^(?:(\d+)h)?(?:(\d+)m(?!tr))?(?:(\d+)s)?$")
_DIST = re.compile(r"^(\d+(?:\.\d+)?)(km|mtr)$")
_PACE = re.compile(r"(\d{1,2}):(\d{2})(?:-(\d{1,2}):(\d{2}))?/km\s+pace")
_REPEAT = re.compile(r"(\d+)\s*x\s*$", re.I)


@dataclass
class Step:
    section: str            # warmup | main | cooldown
    role: str               # work | rest | steady | stride | wu | cd
    duration_s: float | None = None
    distance_m: float | None = None
    pace_fast: float | None = None   # s/km, faster bound
    pace_slow: float | None = None   # s/km, slower bound
    rep: int | None = None           # repetition index inside a block
    block: int | None = None         # repeat block id


def _secs(mm: str, ss: str) -> int:
    return int(mm) * 60 + int(ss)


def parse_step(line: str) -> dict | None:
    body = line.strip()[1:].strip()
    if not body:
        return None
    first = body.split()[0]
    out: dict = {"rest": "intensity=rest" in body}
    m = _DUR.match(first)
    d = _DIST.match(first)
    if d:
        out["distance_m"] = float(d.group(1)) * (1000 if d.group(2) == "km" else 1)
    elif m and any(m.groups()):
        h, mi, s = (int(x) if x else 0 for x in m.groups())
        out["duration_s"] = h * 3600 + mi * 60 + s
    else:
        return None
    p = _PACE.search(body)
    if p:
        a = _secs(p.group(1), p.group(2))
        b = _secs(p.group(3), p.group(4)) if p.group(3) else a
        out["pace_fast"], out["pace_slow"] = min(a, b), max(a, b)
    return out


def parse_workout(description: str, workout_type: str) -> list[Step]:
    """Expand the description into a flat list of planned steps."""
    steps: list[Step] = []
    section, reps, block_lines, block_id = "main", 1, [], 0
    stride_types = {"easy", "recovery", "long"}

    def flush():
        nonlocal block_lines, block_id
        if not block_lines:
            return
        block_id += 1
        in_repeat = reps > 1
        for r in range(reps):
            for st in block_lines:
                if st["rest"]:
                    role = "rest"
                elif section == "warmup":
                    role = "wu"
                elif section == "cooldown":
                    role = "cd"
                elif in_repeat and st.get("pace_fast"):
                    role = "stride" if workout_type in stride_types else "work"
                else:
                    role = "steady"
                steps.append(Step(section=section, role=role,
                                  duration_s=st.get("duration_s"), distance_m=st.get("distance_m"),
                                  pace_fast=st.get("pace_fast"), pace_slow=st.get("pace_slow"),
                                  rep=r + 1 if in_repeat else None, block=block_id))
        block_lines = []

    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            st = parse_step(line)
            if st:
                block_lines.append(st)
            continue
        # header line: close previous block, detect section + repeats
        flush()
        low = line.lower()
        rm = _REPEAT.search(line)
        reps = int(rm.group(1)) if rm else 1
        if any(low.startswith(n) for n in WARMUP_NAMES):
            section = "warmup"
        elif any(low.startswith(n) for n in COOLDOWN_NAMES):
            section = "cooldown"
        else:
            section = "main"
    flush()
    return steps


def planned_duration(steps: list[Step], default_pace: float = 330.0) -> float:
    """Planned moving time in seconds (distance steps estimated via target pace)."""
    total = 0.0
    for s in steps:
        if s.duration_s:
            total += s.duration_s
        elif s.distance_m:
            pace = (s.pace_fast + s.pace_slow) / 2 if s.pace_fast else default_pace
            total += s.distance_m / 1000 * pace
    return total
