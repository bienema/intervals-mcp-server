"""CLI entry point.

  python -m coach.run debrief [--activity i123] [--force] [--dry-run]
  python -m coach.run briefing [--date 2026-10-27] [--dry-run]
  python -m coach.run weekly   [--date 2026-11-01] [--dry-run]
  python -m coach.run calendar [--dry-run]
  python -m coach.run auto     # picks the job from the GitHub Actions cron
"""
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import yaml

from . import jobs
from .api import IntervalsAPI

CRON_JOBS = {  # must match .github/workflows/coach.yml
    "*/30 * * * *": "debrief",
    "0 4 * * *": "briefing",
    "0 18 * * 0": "weekly",
}


def load_cfg() -> dict:
    path = Path(os.environ.get("COACH_CONFIG", Path(__file__).resolve().parent.parent / "config" / "coach.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("job", choices=["debrief", "briefing", "weekly", "calendar", "auto"])
    p.add_argument("--activity")
    p.add_argument("--date", type=date.fromisoformat)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    cfg = load_cfg()
    job = a.job
    if job == "auto":
        job = CRON_JOBS.get(os.environ.get("CRON_SCHEDULE", ""), "debrief")
    api = IntervalsAPI(os.environ.get("ATHLETE_ID") or cfg["athlete_id"], dry_run=a.dry_run)

    if job == "debrief":
        jobs.debrief(api, cfg, activity_id=a.activity, force=a.force)
    elif job == "briefing":
        jobs.briefing(api, cfg, day=a.date)
    elif job == "weekly":
        jobs.weekly(api, cfg, day=a.date)

    # every job ends with a calendar sync (if configured), so Google is always current
    cal_id = os.environ.get("GCAL_CALENDAR_ID")
    if os.environ.get("GOOGLE_SA_JSON") and cal_id and cfg.get("gcal", {}).get("enabled", True):
        from .gcal import GCal, sync
        try:
            sync(api, GCal(cal_id, dry_run=a.dry_run), cfg, jobs.today_local(cfg))
        except Exception as e:  # noqa: BLE001 - calendar problems must not hide coach results
            print(f"[calendar] Fehler: {e}")
            if job == "calendar":
                raise
    elif job == "calendar":
        raise SystemExit("GOOGLE_SA_JSON oder GCAL_CALENDAR_ID fehlt")


if __name__ == "__main__":
    main()
