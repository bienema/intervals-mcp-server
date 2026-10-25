"""Minimal intervals.icu API client for the coach module.

Auth: HTTP Basic, user "API_KEY", password = personal API key.
Endpoints verified against the intervals.icu OpenAPI spec.
"""
from __future__ import annotations

import os
from typing import Any

import requests

BASE = "https://intervals.icu/api/v1"


class IntervalsAPI:
    def __init__(self, athlete_id: str, api_key: str | None = None, dry_run: bool = False):
        self.athlete_id = athlete_id
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.auth = ("API_KEY", api_key or os.environ["INTERVALS_API_KEY"])

    # -- low level --------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, f"{BASE}{path}", timeout=30, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def _write(self, method: str, path: str, json: Any) -> Any:
        if self.dry_run:
            print(f"[dry-run] {method} {path}\n{json}\n")
            return None
        return self._req(method, path, json=json)

    # -- read ---------------------------------------------------------------
    def activities(self, oldest: str, newest: str) -> list[dict]:
        return self._req("GET", f"/athlete/{self.athlete_id}/activities",
                         params={"oldest": oldest, "newest": newest})

    def activity(self, activity_id: str) -> dict:
        return self._req("GET", f"/activity/{activity_id}")

    def intervals(self, activity_id: str) -> list[dict]:
        data = self._req("GET", f"/activity/{activity_id}/intervals") or {}
        return data.get("icu_intervals") or []

    def streams(self, activity_id: str, types=("time", "heartrate", "velocity_smooth")) -> dict[str, list]:
        data = self._req("GET", f"/activity/{activity_id}/streams.json",
                         params={"types": ",".join(types)}) or []
        return {s["type"]: s.get("data") or [] for s in data}

    def messages(self, activity_id: str) -> list[dict]:
        return self._req("GET", f"/activity/{activity_id}/messages") or []

    def events(self, oldest: str, newest: str, category: str | None = None) -> list[dict]:
        params = {"oldest": oldest, "newest": newest}
        if category:
            params["category"] = category
        return self._req("GET", f"/athlete/{self.athlete_id}/events", params=params) or []

    def event(self, event_id: int) -> dict:
        return self._req("GET", f"/athlete/{self.athlete_id}/events/{event_id}")

    def wellness(self, oldest: str, newest: str) -> list[dict]:
        return self._req("GET", f"/athlete/{self.athlete_id}/wellness",
                         params={"oldest": oldest, "newest": newest}) or []

    def run_settings(self) -> dict:
        for s in self._req("GET", f"/athlete/{self.athlete_id}/sport-settings") or []:
            if "Run" in (s.get("types") or []):
                return s
        raise RuntimeError("Keine Lauf-Sporteinstellungen in intervals.icu gefunden")

    # -- write --------------------------------------------------------------
    def post_message(self, activity_id: str, content: str) -> Any:
        return self._write("POST", f"/activity/{activity_id}/messages", {"content": content})

    def create_note(self, day: str, name: str, description: str) -> Any:
        return self._write("POST", f"/athlete/{self.athlete_id}/events", {
            "category": "NOTE", "start_date_local": f"{day}T00:00:00",
            "name": name, "description": description,
        })

    def update_event_description(self, event_id: int, description: str) -> Any:
        return self._write("PUT", f"/athlete/{self.athlete_id}/events/{event_id}",
                           {"description": description})
