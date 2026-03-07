from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os
from datetime import date, datetime, timedelta

app = FastAPI()
DB_PATH = "data/focus.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs("data", exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            estimated_minutes INTEGER DEFAULT 30,
            priority INTEGER DEFAULT 2,
            status TEXT DEFAULT 'todo',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            notes TEXT,
            carry_from INTEGER
        );
        CREATE TABLE IF NOT EXISTS weekly_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            goal TEXT NOT NULL,
            done INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkin_time TEXT DEFAULT CURRENT_TIMESTAMP,
            date TEXT NOT NULL,
            note TEXT,
            completed_count INTEGER,
            total_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO settings (key, value) VALUES ('token_limit', '2000000000');
    """)
    conn.commit()
    conn.close()


init_db()


# --- Models ---
class TaskCreate(BaseModel):
    title: str
    estimated_minutes: int = 30
    priority: int = 2
    date: Optional[str] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    estimated_minutes: Optional[int] = None
    priority: Optional[int] = None
    notes: Optional[str] = None


class GoalCreate(BaseModel):
    goal: str
    week_start: Optional[str] = None


class GoalUpdate(BaseModel):
    done: Optional[int] = None
    goal: Optional[str] = None


class CheckinCreate(BaseModel):
    note: Optional[str] = ""


def get_week_start(d=None):
    if d is None:
        d = date.today()
    elif isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return str(d - timedelta(days=d.weekday()))


# --- Routes ---
@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api/today")
def get_today(d: Optional[str] = None):
    if not d:
        d = str(date.today())
    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id", (d,)
    ).fetchall()
    conn.close()
    tasks = [dict(t) for t in tasks]
    total = len(tasks)
    done = sum(1 for t in tasks if t["status"] == "done")
    total_min = sum(t["estimated_minutes"] for t in tasks)
    done_min = sum(t["estimated_minutes"] for t in tasks if t["status"] == "done")
    return {
        "date": d,
        "tasks": tasks,
        "stats": {"total": total, "done": done, "total_minutes": total_min, "done_minutes": done_min},
    }


@app.post("/api/tasks")
def create_task(task: TaskCreate):
    if not task.date:
        task.date = str(date.today())
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (date, title, estimated_minutes, priority) VALUES (?, ?, ?, ?)",
        (task.date, task.title, task.estimated_minutes, task.priority),
    )
    conn.commit()
    new = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(new)


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, update: TaskUpdate):
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "Not found")
    updates = {k: v for k, v in update.dict().items() if v is not None}
    if update.status == "done" and task["status"] != "done":
        updates["completed_at"] = datetime.now().isoformat()
    elif update.status == "todo":
        updates["completed_at"] = None
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*updates.values(), task_id))
        conn.commit()
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/week")
def get_week(week_start: Optional[str] = None):
    if not week_start:
        week_start = get_week_start()
    week_end = str(datetime.strptime(week_start, "%Y-%m-%d").date() + timedelta(days=6))
    conn = get_db()
    goals = conn.execute(
        "SELECT * FROM weekly_goals WHERE week_start = ? ORDER BY id", (week_start,)
    ).fetchall()
    tasks = conn.execute(
        "SELECT date, status, estimated_minutes FROM tasks WHERE date >= ? AND date <= ?",
        (week_start, week_end),
    ).fetchall()
    conn.close()
    daily = {}
    for t in tasks:
        d = t["date"]
        if d not in daily:
            daily[d] = {"total": 0, "done": 0}
        daily[d]["total"] += 1
        if t["status"] == "done":
            daily[d]["done"] += 1
    return {"week_start": week_start, "goals": [dict(g) for g in goals], "daily": daily}


@app.post("/api/week/goals")
def add_goal(goal: GoalCreate):
    if not goal.week_start:
        goal.week_start = get_week_start()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO weekly_goals (week_start, goal) VALUES (?, ?)",
        (goal.week_start, goal.goal),
    )
    conn.commit()
    new = conn.execute("SELECT * FROM weekly_goals WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(new)


@app.patch("/api/week/goals/{goal_id}")
def update_goal(goal_id: int, update: GoalUpdate):
    conn = get_db()
    updates = {k: v for k, v in update.dict().items() if v is not None}
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE weekly_goals SET {sets} WHERE id = ?", (*updates.values(), goal_id))
        conn.commit()
    updated = conn.execute("SELECT * FROM weekly_goals WHERE id = ?", (goal_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.delete("/api/week/goals/{goal_id}")
def delete_goal(goal_id: int):
    conn = get_db()
    conn.execute("DELETE FROM weekly_goals WHERE id = ?", (goal_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/checkin")
def do_checkin(checkin: CheckinCreate):
    today = str(date.today())
    conn = get_db()
    tasks = conn.execute("SELECT * FROM tasks WHERE date = ?", (today,)).fetchall()
    done = sum(1 for t in tasks if t["status"] == "done")
    total = len(tasks)
    conn.execute(
        "INSERT INTO checkins (date, note, completed_count, total_count) VALUES (?, ?, ?, ?)",
        (today, checkin.note, done, total),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "done": done, "total": total}


@app.get("/api/checkins")
def get_checkins(d: Optional[str] = None):
    if not d:
        d = str(date.today())
    conn = get_db()
    checkins = conn.execute(
        "SELECT * FROM checkins WHERE date = ? ORDER BY checkin_time", (d,)
    ).fetchall()
    conn.close()
    return [dict(c) for c in checkins]


@app.get("/api/yesterday-undone")
def yesterday_undone():
    yesterday = str(date.today() - timedelta(days=1))
    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over')",
        (yesterday,),
    ).fetchall()
    conn.close()
    return [dict(t) for t in tasks]


@app.post("/api/carry-over")
def carry_over():
    today = str(date.today())
    yesterday = str(date.today() - timedelta(days=1))
    conn = get_db()
    undone = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over')",
        (yesterday,),
    ).fetchall()
    count = 0
    for task in undone:
        conn.execute(
            "INSERT INTO tasks (date, title, estimated_minutes, priority, carry_from) VALUES (?, ?, ?, ?, ?)",
            (today, task["title"], task["estimated_minutes"], task["priority"], task["id"]),
        )
        conn.execute("UPDATE tasks SET status = 'carried_over' WHERE id = ?", (task["id"],))
        count += 1
    conn.commit()
    conn.close()
    return {"carried": count}


@app.post("/api/sync-logs")
def sync_logs(logs_path: Optional[str] = None):
    """
    Claude Code squad-team 로그 디렉토리를 읽어 오늘 투두에 자동 임포트.
    - current_task.md에서 프로젝트명 추출
    - *_todo.md에서 미완료 항목(- [ ]) 추출
    - 완료 항목(- [x])은 done 상태로 임포트
    - 이미 존재하는 제목은 중복 추가 안 함
    """
    if not logs_path:
        logs_path = r"G:\내 드라이브\01_work\_agents\squad-team\logs"

    today = str(date.today())
    results = {"imported": [], "skipped": [], "project": None, "error": None}

    if not os.path.isdir(logs_path):
        results["error"] = f"경로를 찾을 수 없음: {logs_path}"
        return results

    conn = get_db()

    # 오늘 이미 있는 task 제목 수집 (중복 방지)
    existing = {
        row["title"]
        for row in conn.execute("SELECT title FROM tasks WHERE date = ?", (today,)).fetchall()
    }

    # 1) current_task.md 에서 프로젝트명 추출
    current_task_path = os.path.join(logs_path, "status", "current_task.md")
    project_name = None
    if os.path.isfile(current_task_path):
        with open(current_task_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("|") and not line.startswith("-"):
                    project_name = line
                    break
    results["project"] = project_name

    # 2) todo 디렉토리의 *_todo.md 파싱
    todo_dir = os.path.join(logs_path, "todo")
    if not os.path.isdir(todo_dir):
        results["error"] = "todo 디렉토리 없음"
        conn.close()
        return results

    # 팀별 우선순위 매핑
    priority_map = {"기획팀": 1, "실무팀": 1, "검수팀": 2, "소비자팀": 2, "리뷰팀": 3}

    for fname in sorted(os.listdir(todo_dir)):
        if not fname.endswith("_todo.md"):
            continue
        team = fname.replace("_todo.md", "")
        priority = priority_map.get(team, 2)
        fpath = os.path.join(todo_dir, fname)

        with open(fpath, encoding="utf-8") as f:
            content = f.read()

        # 작업명 추출
        task_section = None
        for line in content.splitlines():
            if line.startswith("## 작업명"):
                task_section = True
                continue
            if task_section and line.strip():
                task_section = line.strip()
                break

        # 진척도 추출
        progress = None
        for line in content.splitlines():
            if "진척도" in line:
                progress = line.strip()
                break

        # 체크박스 항목 파싱 (최상위 항목만, 서브항목 제외)
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ["):
                continue
            # 서브항목(들여쓰기) 제외
            if line.startswith("  ") and not line.startswith("- ["):
                continue

            is_done = stripped.startswith("- [x]") or stripped.startswith("- [X]")
            title_raw = stripped[6:].strip()  # "- [ ] " 또는 "- [x] " 제거

            # 팀 prefix 붙이기
            title = f"[{team}] {title_raw}"

            if title in existing:
                results["skipped"].append(title)
                continue

            status = "done" if is_done else "todo"
            completed_at = datetime.now().isoformat() if is_done else None

            conn.execute(
                "INSERT INTO tasks (date, title, estimated_minutes, priority, status, completed_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (today, title, 30, priority, status, completed_at, f"출처: {fname} | {progress or ''}"),
            )
            existing.add(title)
            results["imported"].append({"title": title, "status": status, "team": team})

    conn.commit()
    conn.close()
    return results


@app.get("/api/settings/token-limit")
def get_token_limit():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='token_limit'").fetchone()
    conn.close()
    return {"limit": int(row["value"]) if row else 2_000_000_000}


@app.post("/api/settings/token-limit")
def set_token_limit(limit: int):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('token_limit', ?)", (str(limit),))
    conn.commit()
    conn.close()
    return {"limit": limit}


@app.get("/api/claude-usage")
def claude_usage():
    """
    ~/.claude/projects/ 내 JSONL 파일에서 오늘 실제 토큰 사용량 집계.
    각 assistant 메시지의 usage 필드: input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens
    """
    import json as _json

    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_dir):
        return {"error": "~/.claude/projects 없음", "total": {}, "by_project": {}}

    today_prefix  = date.today().isoformat()          # "2026-03-07"
    month_prefix  = date.today().strftime("%Y-%m")    # "2026-03"

    # 한도 조회
    conn_s = get_db()
    row = conn_s.execute("SELECT value FROM settings WHERE key='token_limit'").fetchone()
    conn_s.close()
    token_limit = int(row["value"]) if row else 2_000_000_000

    today_total  = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    month_total  = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    by_project: dict = {}

    def scan_jsonl(filepath, project_name):
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or month_prefix not in line:
                        continue
                    try:
                        d = _json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp", "")
                    if not ts.startswith(month_prefix):
                        continue
                    msg = d.get("message", {})
                    if msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage", {})
                    if not usage:
                        continue

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cc  = usage.get("cache_creation_input_tokens", 0)
                    cr  = usage.get("cache_read_input_tokens", 0)

                    # 월간 누적
                    month_total["input"]        += inp
                    month_total["output"]       += out
                    month_total["cache_create"] += cc
                    month_total["cache_read"]   += cr

                    # 오늘 누적
                    if ts.startswith(today_prefix):
                        today_total["input"]        += inp
                        today_total["output"]       += out
                        today_total["cache_create"] += cc
                        today_total["cache_read"]   += cr

                    if project_name not in by_project:
                        by_project[project_name] = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "turns": 0}
                    p = by_project[project_name]
                    p["input"]        += inp
                    p["output"]       += out
                    p["cache_create"] += cc
                    p["cache_read"]   += cr
                    p["turns"]        += 1
        except Exception:
            pass

    def proj_name_from_dir(d: str) -> str:
        """G---------01-work-hive-media-auto-blog → auto-blog, C--Users-USER → 기타"""
        # 연속 하이픈 구분자 기준으로 마지막 의미있는 세그먼트 추출
        parts = [p for p in d.replace("--", "\x00").split("\x00") if p.strip("-")]
        if not parts:
            return d
        last = parts[-1].strip("-")
        # chunking-english 처럼 하이픈 포함 이름 처리
        return last if last else d

    for proj_dir in os.listdir(projects_dir):
        proj_path = os.path.join(projects_dir, proj_dir)
        if not os.path.isdir(proj_path):
            continue
        pname = proj_name_from_dir(proj_dir)
        for root, _, files in os.walk(proj_path):
            for fname in files:
                if fname.endswith(".jsonl"):
                    scan_jsonl(os.path.join(root, fname), pname)

    month_tokens = sum(month_total.values())
    today_tokens = sum(today_total.values())
    used_pct     = round(month_tokens / token_limit * 100, 1) if token_limit else 0

    sorted_projects = sorted(
        [{"name": k, **v} for k, v in by_project.items()],
        key=lambda x: -(x["input"] + x["output"] + x["cache_create"] + x["cache_read"])
    )

    return {
        "month_tokens": month_tokens,
        "today_tokens": today_tokens,
        "month_total":  month_total,
        "today_total":  today_total,
        "token_limit":  token_limit,
        "used_pct":     used_pct,
        "remain_pct":   round(100 - used_pct, 1),
        "by_project":   sorted_projects,
        "month_label":  date.today().strftime("%Y년 %m월"),
    }


app.mount("/static", StaticFiles(directory="static"), name="static")
