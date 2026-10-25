"""Detect optical-HR artifacts so they don't distort the score.

Two rules:
1. Spikes: HR jumps > spike_bpm between consecutive samples (<= 3 s apart);
   the surrounding window is masked.
2. Implausible load: HR at/above (LTHR - margin) while running slower than the
   Z1 pace limit for longer than min_seconds (classic cadence lock), unless the
   athlete ran hard within the last lookback_s seconds.
"""
from __future__ import annotations


def artifact_mask(time: list, hr: list, speed: list, lthr: float, z1_speed: float,
                  hard_speed: float | None = None, spike_bpm: float = 20, window_s: float = 10,
                  margin: float = 5, min_seconds: float = 30, lookback_s: float = 180) -> list[bool]:
    """Return list[bool], True = sample is an artifact (exclude)."""
    n = len(hr)
    bad = [False] * n
    if n == 0:
        return bad
    t = time if len(time) == n else list(range(n))
    v = speed if len(speed) == n else [None] * n

    # rule 1: spikes
    for i in range(1, n):
        if hr[i] and hr[i - 1] and (t[i] - t[i - 1]) <= 3 and abs(hr[i] - hr[i - 1]) > spike_bpm:
            lo, hi = t[i] - window_s, t[i] + window_s
            j = i
            while j >= 0 and t[j] >= lo:
                bad[j] = True
                j -= 1
            j = i
            while j < n and t[j] <= hi:
                bad[j] = True
                j += 1

    # rule 2: high HR at easy speed, sustained - but not right after hard running
    # (HR legitimately stays high during recovery jogs)
    hard_speed = hard_speed or z1_speed * 1.25
    recent_hard = [False] * n
    last_hard_t = None
    for i in range(n):
        if v[i] is not None and v[i] >= hard_speed:
            last_hard_t = t[i]
        recent_hard[i] = last_hard_t is not None and t[i] - last_hard_t <= lookback_s
    run_start = None
    for i in range(n + 1):
        cond = i < n and hr[i] and v[i] is not None and v[i] > 0.5 \
            and hr[i] >= lthr - margin and v[i] < z1_speed and not recent_hard[i]
        if cond and run_start is None:
            run_start = i
        elif not cond and run_start is not None:
            if t[i - 1] - t[run_start] >= min_seconds:
                for k in range(run_start, i):
                    bad[k] = True
            run_start = None

    for i in range(n):
        if not hr[i]:
            bad[i] = True
    return bad


def artifact_share(mask: list[bool]) -> float:
    return sum(mask) / len(mask) if mask else 0.0
