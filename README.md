# Annarbor Tennis Club

Local Flask + SQLite web app for squad management, auto-created appointments, voting, role assignment, doubles matchmaking, guests, and leaderboard.

## Features

- Account signup/login (stored in DB), plus seeded admin account:
  - Username: `admin`
  - Password: `annarbor`
- Squad tab (admin CRUD + member counters and ratings)
- Appointment tab with:
  - Auto weekly appointment creation schedule (configurable)
  - Voting, waitlist ordering, and late cancellation handling
  - Auto close/finalization and notification outbox
  - Ball carrier/reserver selection with separate counters and tie-break rules
  - Reservation logic that confirms players in `4, 8, 12, ...` groups, with a `3-player => 1 court` exception
  - Guest add/toggle and detailed appointment inspection
- Match Making tab:
  - Uses open-source OR-Tools CP-SAT optimization
  - 3-game doubles planning
  - Uses only the confirmed roster; overflow stays on the waitlist
  - Guest plays once (closest-skill anchor)
  - Pair/opponent history penalties to reduce repeats
  - Tuesday night to Wednesday night result-input window (independent per user)
  - Ranking updates apply automatically after the one-day result window closes
- Leaderboard tab:
  - Rating points and history
  - Match result submission updates ratings
- Settings tab (admin):
  - Scheduler/court/role/notification configs
  - QA fake-time (`qa_now_iso`) for simulation/testing

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open: [http://127.0.0.1:5050](http://127.0.0.1:5050)

## Automated Tests

```bash
source .venv/bin/activate
pytest -q
```

## QA Simulation Script

This script seeds random members, advances fake time, and generates sample votes.

```bash
source .venv/bin/activate
python scripts/qa_seed.py
```

## Feature Test Scripts

```bash
python scripts/test_result_window_feature.py
python scripts/run_10_week_discord_test.py
python scripts/reset_seed_members.py
```

## Notes for PythonAnywhere Later

- Point WSGI entry to `app` in `app.py`.
- Set env var `SECRET_KEY`.
- Keep SQLite file (`club.db`) in a writable path.
- For real notifications, configure Email or Discord webhook in Settings.
