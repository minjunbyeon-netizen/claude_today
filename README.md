# Daily Focus

Personal task management + Claude Code usage dashboard.
Runs locally on **port 8888**.

---

## Stack

- **Backend**: Python, FastAPI, SQLite (`data/focus.db`)
- **Frontend**: Single-file HTML/CSS/JS (no build step)
- **Runner**: Uvicorn with hot-reload

---

## Features

| Feature | Description |
|---|---|
| Today's tasks | Add, complete, carry over tasks by date |
| Weekly goals | Set and track weekly objectives |
| Claude usage | Real-time token usage across all Claude Code sessions |
| Agent status | Live status feed from other Claude Code sessions (project, task, status) |
| CC button | Open Claude Code for any project directly from the dashboard |
| VIEW button | Jump to a project's web homepage (URL reported by the session) |
| Morning checkin | Auto-checkin via `morning.py` on startup |

---

## Quick Start

```bash
# 1. Create virtual environment
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2. Run
.venv/Scripts/python -m uvicorn app:app --host 0.0.0.0 --port 8888 --reload
```

Or double-click **`start.bat`**.

Open: http://localhost:8888

---

## Agent Status API

Other Claude Code sessions report their status here.

**POST** `/api/agent-status`

```json
{
  "project": "my-project",
  "task": "short task summary in English",
  "status": "start",
  "url": "http://localhost:8001"
}
```

- `status`: `start` | `step` | `done`
- `url`: project web homepage (optional, enables VIEW button)

**GET** `/api/agent-status` — returns all active sessions.

---

## DB Schema (SQLite)

```
tasks           - daily tasks (date, title, status, priority, minutes)
weekly_goals    - weekly objectives
checkins        - morning check-in log
settings        - token_limit, remote_url
agent_status    - live session status from other Claude Code instances
```

---

## Project Structure

```
daily-focus/
  app.py          - FastAPI backend + all API routes
  run.py          - entry point
  morning.py      - morning checkin automation
  morning_ai.py   - AI-assisted morning summary
  notifier.py     - desktop notification scheduler
  static/
    index.html    - entire frontend (CSS + JS inline)
    favicon.svg
  data/
    focus.db      - SQLite database (gitignored)
  log/
    YYYY-MM-DD.log - daily work log (gitignored)
```

---

## Migration Notes

- DB file (`data/focus.db`) is **not** committed — copy it manually when moving.
- `data/` and `log/` are gitignored.
- Port changed from 8000 → **8888** (to avoid conflict with Org Focus on 8001).
- Korean text in curl POST body causes encoding errors in Git Bash — always use English for `task` field.

---

## Related

- **Org Focus Dashboard**: `../org-focus/` — org-level task dashboard on port 8001
- **GitHub**: https://github.com/minjunbyeon-netizen/claude_today
