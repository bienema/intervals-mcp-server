# Coach-Modul – Umsetzungsleitfaden

Automatisches Debriefing nach jedem Lauf, Tagesform-Briefing am Morgen und Wochenrückblick
am Sonntag. Läuft kostenlos über GitHub Actions, schreibt direkt in intervals.icu, Texte
kommen aus dem Gemini Free Tier.

| Job | Wann | Ergebnis in intervals.icu |
|---|---|---|
| `debrief` | alle 2 h | Kommentar mit Score + Debriefing an jeder neuen Laufaktivität |
| `briefing` | täglich 05:00 (Winter) / 06:00 (Sommer) | Zeile „Tagesform (auto)“ im heutigen Workout |
| `weekly` | sonntags 19:00 / 20:00 | Notiz „Wochenrückblick KW xx“ im Kalender |
| `calendar` | nach jedem Job | Google-Kalender „Training“: Plan, Briefing, Debriefing mit Score im Titel |

## 1. Gemini-API-Key holen (kostenlos)
1. https://aistudio.google.com öffnen, mit Google-Konto anmelden.
2. Links „Get API key“ → „Create API key“ → Key kopieren.
3. Kein Abrechnungskonto verknüpfen. Ohne Billing bleibt das Projekt im Free Tier und es kann nichts kosten.

Hinweis: Im Free Tier darf Google Eingaben zur Verbesserung seiner Modelle nutzen. Das Modul
schickt deshalb nur Trainingsdaten (Pace, Puls, Plan), keinen Namen.

## 2. Dateien ins Repo
Im Terminal (nicht per Doppelklick entpacken, sonst geht der versteckte Ordner `.github` verloren):
```
cd ~/Pfad/zu/deinem-repo
unzip -n ~/Downloads/coach-modul.zip -d .
ls coach config .github/workflows
```
`-n` überschreibt keine bestehende Datei. Ergebnis:
```
coach/                      neu
config/coach.yaml           neu
tests/test_coach.py         neu
.github/workflows/coach.yml neu
COACH.md                    neu
```

## 3. Lokal testen
```
python3 -m pip install requests pyyaml pytest
python3 -m pytest tests/test_coach.py -q        # erwartet: 18 passed
```

## 4. Hochladen
```
git add coach config/coach.yaml tests/test_coach.py .github/workflows/coach.yml COACH.md
git commit -m "Coach-Modul: Debriefing, Briefing, Wochenrückblick (Gemini)"
git push
```

## 5. Secrets in GitHub
Repo auf github.com → Settings → Secrets and variables → Actions → „New repository secret“:
- `GEMINI_API_KEY` – der Key aus Schritt 1
- `INTERVALS_API_KEY` – sollte schon vorhanden sein, sonst intervals.icu-Key eintragen

## 6. Erster Testlauf (schreibt nichts)
github.com → Tab „Actions“ → links „coach“ → „Run workflow“
- Job: `debrief`
- Args: `--dry-run`

Im Log erscheint der Text, der geschrieben würde. Passt alles, denselben Lauf ohne `--dry-run`
starten. Ab dann laufen die Jobs automatisch.

Achtung bei öffentlichem Repo: Action-Logs sind öffentlich. Im Normalbetrieb stehen dort nur
Aktivitäts-ID, Titel und Score; `--dry-run` zeigt den kompletten Text. Nur kurz zum Testen nutzen.

## Google Kalender (optional, kostenlos)
Termine im eigenen Kalender „Training“ (7 Tage zurück, 14 Tage voraus):
- geplant: „🏃 Long Run · ≈15 km · 90 min“ (ganztägig), Beschreibung = Briefing + Plan
- erledigt: „✅ 🟢 92 · Long Run“ zur tatsächlichen Laufzeit, Beschreibung = Score, Ist-Daten, Debriefing, Plan
- verpasst: „⏭️ Long Run (nicht absolviert)“
- sonntags: „📊 Wochenrückblick KW xx“

Einrichtung:
1. console.cloud.google.com → Projekt anlegen (ohne Abrechnung) → „Google Calendar API“ aktivieren
2. IAM und Verwaltung → Dienstkonten → Dienstkonto erstellen → Schlüssel → JSON herunterladen
3. calendar.google.com → neuen Kalender „Training“ → Einstellungen → für das Dienstkonto
   freigeben mit „Änderungen an Terminen vornehmen“ → Kalender-ID kopieren
4. GitHub-Secrets: `GOOGLE_SA_JSON` (kompletter Inhalt der JSON-Datei), `GCAL_CALENDAR_ID`
5. Test: Actions → coach → Run workflow → Job `calendar`, Args `--dry-run`

## Manuelle Läufe
| Zweck | Job | Args |
|---|---|---|
| Debriefing für eine Aktivität neu erzeugen | `debrief` | `--activity i123456 --force` |
| Wochenrückblick einer bestimmten Woche | `weekly` | `--date 2026-11-01` |
| Briefing für ein Datum | `briefing` | `--date 2026-10-27` |
| Nur Kalender abgleichen | `calendar` | – |
| Nur anzeigen statt schreiben | beliebig | `--dry-run` anhängen |

## Einstellungen (config/coach.yaml)
- `llm.provider`: `gemini` (Standard, kostenlos), `none` (nur Regeltexte) oder `anthropic` (kostenpflichtig)
- `weights`, `pace_tolerance`, `ampel`: Score-Logik, identisch mit den Coach-Regeln im Claude-Projekt
- Schwellen und Zonen stehen bewusst nicht hier; sie werden live aus intervals.icu gelesen

## Verhalten im Fehlerfall
- Gemini nicht erreichbar oder Tageslimit erreicht: zweites Modell wird probiert, sonst Regeltext.
  Score und Zahlen sind davon nie betroffen, sie werden immer im Code berechnet.
- Ein Debriefing wird pro Aktivität nur einmal geschrieben (Erkennung über „Coach-Debrief“).
- Ungeplante Läufe bekommen ein Debriefing ohne Score.

## Dateien
```
coach/api.py        intervals.icu-API
coach/workout.py    liest die Workout-Struktur aus der Beschreibung
coach/hrfilter.py   erkennt Sensor-Ausreißer im Puls
coach/scoring.py    Score-Engine (deterministisch)
coach/llm.py        Gemini / Claude / keine – mit Fallback
coach/facts.py      Plan im Klartext (Runna-Stil) und Ist-Daten pro Abschnitt
coach/gcal.py       Google-Kalender-Sync
coach/jobs.py       debrief / briefing / weekly
coach/run.py        Kommandozeile
```
