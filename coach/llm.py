"""Text generation for debrief, briefing and weekly review.

Providers (config llm.provider):
  gemini     - Google AI Studio free tier (default, key: GEMINI_API_KEY)
  anthropic  - Claude API, paid (key: ANTHROPIC_API_KEY)
  none       - no LLM, rule-based texts only

Every failure returns None so the caller falls back to rule-based text;
the pipeline never breaks because of text generation.
"""
from __future__ import annotations

import os

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# No personal names: on the free tier Google may use inputs to improve its models.
SYSTEM = (
    "Du bist Lauftrainer eines ambitionierten Marathonläufers (Ziel: {goal}). Du schreibst "
    "kurze, konkrete Texte auf Deutsch für intervals.icu, die auf dem Handy gelesen werden. "
    "Kein Markdown, keine Sternchen, keine Überschriften mit #, keine Floskeln. Nutze nur die "
    "gelieferten Zahlen, erfinde nichts. Bei Schmerzen oder Beschwerden: Belastung senken, bei "
    "Ruhe-, Nacht- oder punktuellem Knochenschmerz ärztliche Abklärung empfehlen."
)


def _clean(text: str) -> str:
    return text.replace("**", "").replace("__", "").strip()


def _gemini(prompt: str, system: str, llm: dict) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("[llm] GEMINI_API_KEY fehlt, nutze Fallback")
        return None
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": llm.get("max_tokens", 2048), "temperature": 0.4},
    }
    for model in llm.get("gemini_models", ["gemini-flash-latest", "gemini-flash-lite-latest"]):
        try:
            r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=60,
                              headers={"x-goog-api-key": key})
            if r.status_code in (404, 429, 500, 503):
                print(f"[llm] {model}: HTTP {r.status_code}, versuche nächstes Modell")
                continue
            r.raise_for_status()
            cands = r.json().get("candidates") or []
            parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if text.strip():
                return _clean(text)
            print(f"[llm] {model}: leere Antwort ({cands[0].get('finishReason') if cands else 'keine'})")
        except Exception as e:  # noqa: BLE001
            print(f"[llm] {model}: Fehler {e}")
    return None


def _anthropic(prompt: str, system: str, llm: dict) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[llm] ANTHROPIC_API_KEY fehlt, nutze Fallback")
        return None
    try:
        r = requests.post(ANTHROPIC_URL, timeout=60, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={
            "model": llm.get("anthropic_model", "claude-haiku-5-5"),
            "max_tokens": min(llm.get("max_tokens", 2048), 1500),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        })
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return _clean(text) or None
    except Exception as e:  # noqa: BLE001
        print(f"[llm] Claude-Fehler: {e}")
        return None


def complete(prompt: str, cfg: dict) -> str | None:
    llm = cfg.get("llm", {})
    provider = (llm.get("provider") or "none").lower()
    system = SYSTEM.format(goal=cfg.get("goal", ""))
    if provider == "gemini":
        return _gemini(prompt, system, llm)
    if provider == "anthropic":
        return _anthropic(prompt, system, llm)
    return None
