# Daily Focus Migration README

This file is intentionally ASCII-only to reduce future encoding problems.

## Project summary

This project is a FastAPI-based internal task dashboard.

Current MVP includes:

- org hierarchy: ceo / manager / member
- task assignment and task status updates
- weekly focus entry
- daily work log entry
- manager / ceo review flow
- guest preview mode
- demo login mode with seeded test users

## Main files

- `app.py`: main FastAPI app, DB init, org logic, demo login API
- `run.py`: standard local launcher
- `start.bat`: Windows entry point
- `static/index.html`: main UI shell
- `static/app.js`: frontend logic
- `static/styles.css`: frontend styling
- `data/focus.db`: SQLite database

## Runtime

Checked-in default local port:

- `8001`

Current launcher behavior:

- `start.bat` -> `python run.py`
- `run.py` starts `uvicorn app:app --host 0.0.0.0 --port 8001`

Important:

- During recent debugging, a manual server was also seen on port `8888`.
- That `8888` process is not the checked-in default launcher.
- After moving, use one fixed port only and remove old manual run habits.

## Dependencies

From `requirements.txt`:

- `fastapi`
- `uvicorn[standard]`
- `schedule`
- `plyer`

## Data

Primary local data file:

- `data/focus.db`

Before moving, make sure this file is copied together with the code.

## Current product state

Working now:

- guest preview
- demo login
- org dashboard by role
- task create / update
- weekly focus save
- work log save
- review approve / needs_update

Not finished yet:

- real auth
- real user/company admin UI
- push/mobile notifications
- reporting/export

## Encoding warning

Known issue:

- Some existing Python comments and some UI text have already been damaged by Korean encoding problems.
- Future edits should use UTF-8 consistently.

Recommended rules after moving:

- save source files as `UTF-8`
- avoid mixed editors with legacy Korean code pages
- confirm terminal/editor encoding before bulk edits
- keep migration notes in ASCII when possible if environment is unstable

## Move checklist

1. Copy the whole project folder, including `data/`, `static/`, and `.venv/` if you want the same local environment.
2. Verify Python and dependencies on the new machine.
3. Start with `start.bat` or `python run.py`.
4. Open `http://localhost:8001`.
5. Confirm demo login and guest preview still work.
6. Confirm `data/focus.db` is being read correctly.
7. Standardize one dev port and stop any old background uvicorn process.

## Suggested first cleanup after move

1. Fix encoding-damaged Korean text in `app.py` and UI strings.
2. Pick one permanent local port.
3. Add a real README in Korean only after UTF-8 is stable.
4. Add `.editorconfig` or editor settings for UTF-8 enforcement.
