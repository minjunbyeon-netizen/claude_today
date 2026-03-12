# Daily Focus Migration README

This file is intentionally ASCII-only to reduce future encoding problems.

## Project summary

This project is a FastAPI-based CC / VIEW monitoring dashboard.

Current MVP includes:

- daily task list
- Claude Code usage tracking
- CC button shortcuts
- VIEW button shortcuts
- session activity feed
- check-in history

## Main files

- `app.py`: main FastAPI app and task / Claude APIs
- `run.py`: standard local launcher
- `runtime_config.py`: shared host / port / base URL settings
- `start.bat`: Windows entry point
- `static/index.html`: main UI shell
- `static/app.js`: frontend logic
- `static/styles.css`: frontend styling
- `data/focus.db`: SQLite database

## Runtime

Checked-in default local port:

- `8001`

Runtime override:

- `APP_PORT`
- optional `APP_BASE_URL` if the browser URL must differ from localhost

Current launcher behavior:

- `start.bat` -> `python run.py`
- `run.py` starts `uvicorn app:app --host 0.0.0.0 --port %APP_PORT%`
- `start.bat` forces `PYTHONUTF8=1` and defaults `APP_PORT` to `8001`
- helper scripts now read the same port from `runtime_config.py`

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

- daily task dashboard
- CC / VIEW project shortcuts
- Claude usage tracking
- session feed
- check-in save
- carry-over flow

Separation note:

- `C:\work\daily-focus` is the CC / VIEW dashboard.
- role-based org task UI belongs in `C:\work\org-focus`.

## Encoding warning

Known issue:

- Some existing Python comments and some UI text have already been damaged by Korean encoding problems.
- Future edits should use UTF-8 consistently.

Recommended rules after moving:

- save source files as `UTF-8`
- avoid mixed editors with legacy Korean code pages
- confirm terminal/editor encoding before bulk edits
- keep migration notes in ASCII when possible if environment is unstable
- keep `.editorconfig` checked in and enabled in the editor

## Move checklist

1. Copy the whole project folder, including `data/`, `static/`, and `.venv/` if you want the same local environment.
2. Verify Python and dependencies on the new machine.
3. Start with `start.bat` or `python run.py`.
4. Open `http://localhost:8001` unless `APP_PORT` was overridden.
5. Confirm demo login and guest preview still work.
6. Confirm `data/focus.db` is being read correctly.
7. Standardize one dev port and stop any old background uvicorn process.

## Suggested first cleanup after move

1. Fix encoding-damaged Korean text in `app.py` and UI strings.
2. Pick one permanent local port.
3. Add a real README in Korean only after UTF-8 is stable.
4. Keep `.editorconfig` or editor settings for UTF-8 enforcement.
