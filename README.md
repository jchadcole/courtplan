# CourtPlan: AI-powered tournament scheduler

Licensed under the MIT License (see LICENSE).

A web app for running large, multi-venue AAU basketball tournaments. Tournament admins build and
publish conflict-free schedules; site directors run their venue on game day; coaches manage their
teams' scheduling preferences and get live updates; parents follow a public live schedule.

## Run it

```bash
pip install -r requirements.txt
python seed.py                       # optional demo: 3 venues, 13 courts, 96 teams
uvicorn app.main:app --port 8000     # open http://localhost:8000
```

Demo accounts (password `demo-password-1`): `admin@demo.test` (platform admin), `director@demo.test`
(site director, North Fieldhouse), `coach@demo.test` (coach with two teams). On a fresh database the
first account you register becomes the platform admin.

Optional: set `ANTHROPIC_API_KEY` to let Claude read coaches' free-text requests and write the
director briefing. Without it, a built-in rule parser and rule-based briefing are used.
Set `TOURNEY_SECURE_COOKIES=1` when serving over HTTPS. `TOURNEY_DB` sets the SQLite path.

Run tests with `python -m pytest -q`.

## How it maps to the best-practices documents

| Practice | Where |
| --- | --- |
| Multi-team coach trap: no double-booking, travel buffer (default 75 min) across venues | hard constraint in `engine.Scheduler.feasible` |
| Per-coach rule profiles (e.g. "75-minute gap for Coach Smith") | Teams tab, Coach rule profiles |
| Venue zoning by division | each division gets a home venue (set it, or it's balanced by capacity) |
| Strict rest windows | minimum rest between a team's games (default 60 min, one full slot) |
| 85/15 rule | ~15% of slots held back as staggered catch-up slots; utilization shown per venue |
| Rolling 10-minute buffer | games on the hour: 50-minute game + 10-minute buffer |
| Centralized master control, dynamic slot shifting | Live control: report a court running late; later games shift only as far as buffers can't absorb, teams are notified, new conflicts are flagged, and Auto-repair re-places just the affected games |
| Public, mobile, live delivery with QR codes | Publish tab: public link + printable QR; page updates live (Server-Sent Events) |
| Push notifications | in-app and browser notifications to affected teams; announcements to everyone |
| Site directors | director role scoped to one venue: delays, scores, announcements |
| Referee zoning | crews stay at one venue and work one court for a 4-hour block |
| Balanced pools (RPI) | optional team rating seeds snake-drafted pools; same-club teams split across pools |
| Standings and tie-breakers | wins, head-to-head (two-way), point differential, points allowed |
| Bulk data entry | CSV import: `division,team,club,coach_name,coach_email,rating` |
| Pool-to-bracket advancement | Brackets tab: single-elimination brackets (byes to top seeds, optional 3rd-place game) seeded by pool finish or division record. Bracket games are scheduled after pool play with rest built in, seeds fill when pool play ends, winners advance as scores come in, and an unbreakable tie alerts the director to pick seeds |
| Hands-on schedule editing | Schedule tab: drag games between courts and times, or to and from the unplaced tray. While dragging, cells shade green, amber or red with the reason, and the same teams' and coach's games are outlined. Dragged games lock; "Re-optimize unlocked" re-plans around them. A rule can be waived for one game (for example a back-to-back on purpose) |

## Architecture

* `app/engine.py`: the scheduler. Generates pool round-robins, builds the slot grid, then runs a
  randomized greedy construction with restarts, an ejection repair for games that don't fit, and
  simulated annealing to cut soft costs (venue bouncing, off-home-venue games, missed preferences,
  long idle gaps, catch-up slots used). Hard rules are never relaxed. `validate()` re-checks any
  schedule on real times, so it also catches problems introduced by delays or manual moves.
* `app/brackets.py`: bracket structure, seeding from standings, tie detection and advancement.
* `app/ai.py`: Claude helpers (preference parsing with structured output, director briefing) with
  deterministic fallbacks.
* `app/main.py`: FastAPI JSON API, SSE streams, public endpoints and static UI.
* `app/auth.py`: scrypt password hashing, server-side sessions in HttpOnly SameSite cookies, CSRF
  tokens on every write, login throttling, role checks (platform admin, tournament admin, site
  director, team manager). Team managers only see their own teams' preferences.
* `static/`: dependency-free single-page UI with a strict Content-Security-Policy.
* SQLite storage; every tournament's data is isolated by `tournament_id`. Databases from v1 are migrated
  in place on startup (bracket games can have undecided teams).

## Not built yet

Double elimination and consolation brackets, touch drag and drop on phones, SMS delivery (e.g. Twilio) for alerts, registration/payments,
historical RPI import across circuit stops, and multi-instance deployment (SSE is in-process).
