from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os
import re
import subprocess
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
        INSERT OR IGNORE INTO settings (key, value) VALUES ('remote_url', '');
        CREATE TABLE IF NOT EXISTS agent_status (
            project TEXT PRIMARY KEY,
            task    TEXT NOT NULL,
            status  TEXT NOT NULL,
            url     TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


init_db()


def ensure_table_column(conn, table_name: str, column_name: str, column_def: str):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


def init_org_data():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS org_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('ceo', 'manager', 'member')),
            team TEXT,
            manager_id INTEGER,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (manager_id) REFERENCES org_users(id)
        );
        CREATE TABLE IF NOT EXISTS org_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'planned',
            priority INTEGER NOT NULL DEFAULT 2,
            due_date TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            assignee_id INTEGER NOT NULL,
            parent_task_id INTEGER,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            FOREIGN KEY (created_by) REFERENCES org_users(id),
            FOREIGN KEY (assignee_id) REFERENCES org_users(id),
            FOREIGN KEY (parent_task_id) REFERENCES org_tasks(id)
        );
        CREATE TABLE IF NOT EXISTS org_weekly_focus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            focus TEXT NOT NULL,
            support_needed TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, week_start),
            FOREIGN KEY (user_id) REFERENCES org_users(id)
        );
        CREATE TABLE IF NOT EXISTS org_work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            today_done TEXT NOT NULL,
            next_plan TEXT NOT NULL,
            blockers TEXT DEFAULT '',
            progress INTEGER DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'submitted',
            review_note TEXT DEFAULT '',
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id, user_id, log_date),
            FOREIGN KEY (task_id) REFERENCES org_tasks(id),
            FOREIGN KEY (user_id) REFERENCES org_users(id),
            FOREIGN KEY (reviewed_by) REFERENCES org_users(id)
        );
    """)

    ensure_table_column(conn, "org_users", "title", "TEXT DEFAULT ''")
    ensure_table_column(conn, "org_users", "demo_login_id", "TEXT")
    ensure_table_column(conn, "org_users", "demo_password", "TEXT")

    demo_seed_version = "2026-03-09-demo-login-v1"
    seeded_version = conn.execute(
        "SELECT value FROM settings WHERE key = 'org_demo_seed_version'"
    ).fetchone()

    if not seeded_version or seeded_version["value"] != demo_seed_version:
        demo_users = [
            (1, "민아 한", "ceo", "대표", "경영", None, 1, "ceo", "1111"),
            (2, "지훈 박", "manager", "마케팅 팀장", "마케팅", 1, 2, "mktlead", "2222"),
            (3, "서연 이", "manager", "운영 팀장", "운영", 1, 3, "opslead", "3333"),
            (4, "유진 최", "member", "마케팅 팀원", "마케팅", 2, 4, "mkt01", "4444"),
            (5, "다온 정", "member", "마케팅 팀원", "마케팅", 2, 5, "mkt02", "5555"),
            (6, "현우 한", "member", "운영 팀원", "운영", 3, 6, "ops01", "6666"),
            (7, "나래 윤", "member", "운영 팀원", "운영", 3, 7, "ops02", "7777"),
        ]
        for user in demo_users:
            conn.execute(
                """
                INSERT INTO org_users
                    (id, name, role, title, team, manager_id, sort_order, demo_login_id, demo_password)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role,
                    title = excluded.title,
                    team = excluded.team,
                    manager_id = excluded.manager_id,
                    sort_order = excluded.sort_order,
                    demo_login_id = excluded.demo_login_id,
                    demo_password = excluded.demo_password
                """,
                user,
            )

        today = date.today()
        week_start = get_week_start(today)
        demo_tasks = [
            (1, "3월 핵심 캠페인 총괄", "대표가 마케팅 팀장에게 위임한 핵심 과제입니다.", "in_progress", 1, str(today + timedelta(days=2)), 1, 2, None, "수요일 오후 중간 점검"),
            (2, "고객사 제안서 템플릿 정리", "제안서 공통 구조와 메시지를 표준화합니다.", "planned", 2, str(today + timedelta(days=1)), 2, 4, 1, "샘플 2종 포함"),
            (3, "광고 성과 리포트 초안", "지난주 성과를 대표 공유용 1차안으로 정리합니다.", "review", 1, str(today), 2, 5, 1, "팀장 검토 대기"),
            (4, "운영 체크리스트 개편", "운영팀 반복 업무를 주간 체크리스트로 정리합니다.", "in_progress", 2, str(today + timedelta(days=3)), 1, 3, None, "누락 단계 없는지 확인"),
            (5, "정산 누락 건 확인", "정산 누락 원인을 파악하고 재발 방지안을 적습니다.", "blocked", 1, str(today - timedelta(days=1)), 3, 6, 4, "회계 자료 회신 대기"),
            (6, "파트너사 공지 일정표 작성", "다음 주 공지 배포 일정과 담당자를 정리합니다.", "planned", 3, str(today + timedelta(days=4)), 3, 7, 4, "공유 캘린더 반영 예정"),
        ]
        for task in demo_tasks:
            conn.execute(
                """
                INSERT INTO org_tasks
                    (id, title, description, status, priority, due_date, created_by, assignee_id, parent_task_id, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    status = excluded.status,
                    priority = excluded.priority,
                    due_date = excluded.due_date,
                    created_by = excluded.created_by,
                    assignee_id = excluded.assignee_id,
                    parent_task_id = excluded.parent_task_id,
                    notes = excluded.notes
                """,
                task,
            )

        demo_focus = [
            (4, week_start, "제안서 템플릿을 마무리하고 대표 보고용 샘플까지 준비", "우수 사례 링크가 필요함"),
            (5, week_start, "광고 리포트 초안을 제출하고 팀장 피드백 반영", ""),
            (6, week_start, "정산 누락 원인 파악과 재발 방지 체크리스트 작성", "회계팀 자료 확인 필요"),
            (7, week_start, "파트너사 공지 일정표 확정과 담당자 확인", ""),
        ]
        for focus in demo_focus:
            conn.execute(
                """
                INSERT INTO org_weekly_focus (user_id, week_start, focus, support_needed, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, week_start) DO UPDATE SET
                    focus = excluded.focus,
                    support_needed = excluded.support_needed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                focus,
            )

        demo_logs = [
            (3, 5, str(today), "광고 리포트 핵심 수치를 정리하고 초안 문구를 작성", "디자인 반영 후 팀장 검토 요청", "", 80, "submitted", "", None, None),
            (5, 6, str(today), "정산 누락 원인을 1차 확인하고 거래처 목록을 비교", "회신 자료 도착 즉시 재대조", "자료 회신 지연", 45, "submitted", "", None, None),
        ]
        for log in demo_logs:
            conn.execute(
                """
                INSERT INTO org_work_logs
                    (task_id, user_id, log_date, today_done, next_plan, blockers, progress, review_status, review_note, reviewed_by, reviewed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, user_id, log_date) DO UPDATE SET
                    today_done = excluded.today_done,
                    next_plan = excluded.next_plan,
                    blockers = excluded.blockers,
                    progress = excluded.progress,
                    review_status = excluded.review_status,
                    review_note = excluded.review_note,
                    reviewed_by = excluded.reviewed_by,
                    reviewed_at = excluded.reviewed_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                log,
            )

        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('org_demo_seed_version', ?)",
            (demo_seed_version,),
        )

    conn.commit()
    conn.close()


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


class OrgTaskCreate(BaseModel):
    actor_id: int
    title: str
    assignee_id: int
    due_date: Optional[str] = None
    priority: int = 2
    description: str = ""
    parent_task_id: Optional[int] = None


class OrgTaskUpdate(BaseModel):
    actor_id: int
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[int] = None
    notes: Optional[str] = None


class OrgWeeklyFocusUpsert(BaseModel):
    actor_id: int
    focus: str
    support_needed: str = ""
    week_start: Optional[str] = None


class OrgWorkLogUpsert(BaseModel):
    actor_id: int
    task_id: int
    today_done: str
    next_plan: str
    blockers: str = ""
    progress: int = 0
    log_date: Optional[str] = None


class OrgWorkLogReview(BaseModel):
    actor_id: int
    review_status: str
    review_note: str = ""


class DemoLoginRequest(BaseModel):
    login_id: str
    password: str


def get_week_start(d=None):
    if d is None:
        d = date.today()
    elif isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return str(d - timedelta(days=d.weekday()))


init_org_data()


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
    Claude Code squad-team 로그 디렉토리를 읽어 오늘 투두에 자동 임포트/업데이트.
    - *_todo.md의 - [x] 항목: DB에 없으면 done으로 추가, 있으면 done으로 업데이트
    - *_todo.md의 - [ ] 항목: DB에 없으면 todo로 추가, 있으면 스킵 (수동 체크 보존)
    """
    if not logs_path:
        logs_path = r"X:\01_work\_agents\squad-team\logs"

    today = str(date.today())
    results = {"imported": [], "updated": [], "skipped": [], "project": None, "error": None}

    if not os.path.isdir(logs_path):
        results["error"] = f"경로를 찾을 수 없음: {logs_path}"
        return results

    conn = get_db()

    # 오늘 이미 있는 task: {title: (id, status)} 로 저장
    existing = {
        row["title"]: {"id": row["id"], "status": row["status"]}
        for row in conn.execute("SELECT id, title, status FROM tasks WHERE date = ?", (today,)).fetchall()
    }

    # current_task.md 에서 프로젝트명 추출
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

    # todo 디렉토리의 *_todo.md 파싱
    todo_dir = os.path.join(logs_path, "todo")
    if not os.path.isdir(todo_dir):
        results["error"] = "todo 디렉토리 없음"
        conn.close()
        return results

    priority_map = {"기획팀": 1, "실무팀": 1, "검수팀": 2, "소비자팀": 2, "리뷰팀": 3}

    for fname in sorted(os.listdir(todo_dir)):
        if not fname.endswith("_todo.md"):
            continue
        team = fname.replace("_todo.md", "")
        priority = priority_map.get(team, 2)
        fpath = os.path.join(todo_dir, fname)

        with open(fpath, encoding="utf-8") as f:
            content = f.read()

        # 진척도 추출
        progress = None
        for line in content.splitlines():
            if "진척도" in line:
                progress = line.strip()
                break

        # 체크박스 항목 파싱
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ["):
                continue
            if line.startswith("  ") and not line.startswith("- ["):
                continue

            is_done = stripped.startswith("- [x]") or stripped.startswith("- [X]")
            title_raw = stripped[6:].strip()
            title = f"[{team}] {title_raw}"

            if title in existing:
                # 이미 존재: [x] 완료 표시면 → DB도 done으로 업데이트
                if is_done and existing[title]["status"] != "done":
                    conn.execute(
                        "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                        (datetime.now().isoformat(), existing[title]["id"]),
                    )
                    existing[title]["status"] = "done"
                    results["updated"].append(title)
                else:
                    results["skipped"].append(title)
            else:
                # 신규 추가
                status = "done" if is_done else "todo"
                completed_at = datetime.now().isoformat() if is_done else None
                conn.execute(
                    "INSERT INTO tasks (date, title, estimated_minutes, priority, status, completed_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (today, title, 30, priority, status, completed_at, f"출처: {fname} | {progress or ''}"),
                )
                existing[title] = {"id": -1, "status": status}
                results["imported"].append({"title": title, "status": status, "team": team})

    conn.commit()
    conn.close()
    return results


class SyncLogsUpload(BaseModel):
    log_files: dict   # {"팀명_todo.md": "내용", ...}
    current_task: Optional[str] = ""


@app.post("/api/sync-logs-upload")
def sync_logs_upload(payload: SyncLogsUpload):
    """브라우저에서 업로드한 _todo.md 파일 내용을 파싱해 DB에 반영"""
    today = str(date.today())
    results = {"imported": [], "updated": [], "skipped": [], "project": None}

    conn = get_db()
    existing = {
        row["title"]: {"id": row["id"], "status": row["status"]}
        for row in conn.execute("SELECT id, title, status FROM tasks WHERE date = ?", (today,)).fetchall()
    }

    # current_task에서 프로젝트명 추출
    project_name = None
    for line in (payload.current_task or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("|") and not line.startswith("-"):
            project_name = line
            break
    results["project"] = project_name

    priority_map = {"기획팀": 1, "실무팀": 1, "검수팀": 2, "소비자팀": 2, "리뷰팀": 3}

    for fname, content in sorted(payload.log_files.items()):
        if not fname.endswith("_todo.md"):
            continue
        team = fname.replace("_todo.md", "")
        priority = priority_map.get(team, 2)

        progress = None
        for line in content.splitlines():
            if "진척도" in line:
                progress = line.strip()
                break

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ["):
                continue
            is_done = stripped.startswith("- [x]") or stripped.startswith("- [X]")
            title_raw = stripped[6:].strip()
            title = f"[{team}] {title_raw}"

            if title in existing:
                if is_done and existing[title]["status"] != "done":
                    conn.execute(
                        "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                        (datetime.now().isoformat(), existing[title]["id"]),
                    )
                    existing[title]["status"] = "done"
                    results["updated"].append(title)
                else:
                    results["skipped"].append(title)
            else:
                status = "done" if is_done else "todo"
                completed_at = datetime.now().isoformat() if is_done else None
                conn.execute(
                    "INSERT INTO tasks (date, title, estimated_minutes, priority, status, completed_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (today, title, 30, priority, status, completed_at, f"출처: {fname} | {progress or ''}"),
                )
                existing[title] = {"id": -1, "status": status}
                results["imported"].append({"title": title, "status": status, "team": team})

    conn.commit()
    conn.close()
    return results


@app.post("/api/sync-git")
def sync_git():
    """
    로컬 git 레포 스캔 → 오늘 활동 기반으로 task 완료 자동 업데이트.
    - 오늘 커밋 있음 → '[project] 이어서: ...' 태스크 → done
    - 미커밋 수 감소(0 포함) → '[project] 미커밋 변경사항 N개' → done
    - Claude Code 세션 오늘 활동 → '[project] 이어서' 태스크 status in_progress 표시용 flag
    """
    SCAN_ROOTS = [
        r"X:\01_work",
        r"X:\01_work\_agents",
        r"X:\01_work\hive-media",
        r"X:\01_work\my-project",
        r"C:\Users\USER\Desktop",
    ]
    MAX_DEPTH = 3
    today = str(date.today())
    today_dt = f"{today} 00:00:00"
    updated = []
    scanned = []

    def git_run(args, cwd):
        try:
            r = subprocess.run(
                ["git"] + args, cwd=cwd,
                capture_output=True, text=True, timeout=6,
                encoding="utf-8", errors="replace"
            )
            return r.stdout.strip()
        except Exception:
            return ""

    def find_repos(root):
        repos = []
        if not os.path.isdir(root):
            return repos
        try:
            for dirpath, dirnames, _ in os.walk(root):
                depth = dirpath.replace(root, "").count(os.sep)
                if depth >= MAX_DEPTH:
                    dirnames.clear()
                    continue
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d not in ("node_modules", "venv", "__pycache__", ".git")
                ]
                if ".git" in os.listdir(dirpath):
                    repos.append(dirpath)
                    dirnames.clear()
        except (PermissionError, OSError):
            pass
        return repos

    conn = get_db()

    # 오늘 todo 상태 task 가져오기 → 프로젝트별 그룹핑
    tasks_today = conn.execute(
        "SELECT id, title, status FROM tasks WHERE date = ? AND status = 'todo'",
        (today,)
    ).fetchall()

    proj_tasks: dict = {}
    for t in tasks_today:
        m = re.match(r'^\[([^\]]+)\]', t["title"])
        if m:
            proj = m.group(1).lower()
            proj_tasks.setdefault(proj, []).append(dict(t))

    # 레포 스캔 (seen으로 중복 방지)
    seen_paths = set()
    for root in SCAN_ROOTS:
        for repo_path in find_repos(root):
            if repo_path in seen_paths:
                continue
            seen_paths.add(repo_path)

            repo_name = os.path.basename(repo_path).lower()
            # 프로젝트명 정규화 (lotto-app ↔ lotto_app 등 매칭)
            norm = repo_name.replace("-", "").replace("_", "")

            # 이 레포와 연결된 task 목록 찾기
            matched_proj = None
            for proj_key in proj_tasks:
                proj_norm = proj_key.replace("-", "").replace("_", "")
                if proj_norm == norm:
                    matched_proj = proj_key
                    break

            # git 상태 조회
            commits_today = git_run(["log", f"--since={today_dt}", "--oneline", "--no-merges"], repo_path)
            dirty_out = git_run(["status", "--short"], repo_path)
            dirty_count = len([l for l in dirty_out.splitlines() if l.strip()])
            has_commit = bool(commits_today.strip())

            scanned.append({
                "repo": repo_name,
                "commits_today": has_commit,
                "dirty": dirty_count,
                "matched": matched_proj,
            })

            if matched_proj is None:
                continue

            for task in proj_tasks[matched_proj]:
                task_id = task["id"]
                title = task["title"]
                should_done = False

                # 오늘 커밋 있으면 "이어서" 작업 완료 처리
                if has_commit and "이어서:" in title:
                    should_done = True

                # 미커밋 변경사항 태스크: 원래 N개보다 줄었거나 0개면 완료
                if "미커밋 변경사항" in title and "정리/커밋" in title:
                    m2 = re.search(r"(\d+)개", title)
                    if m2:
                        orig = int(m2.group(1))
                        if dirty_count == 0 or dirty_count < orig:
                            should_done = True

                if should_done:
                    conn.execute(
                        "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                        (datetime.now().isoformat(), task_id),
                    )
                    updated.append(title)

    conn.commit()
    conn.close()
    return {"updated": updated, "count": len(updated), "scanned": scanned}


@app.post("/api/open-claude")
def open_claude(proj: str):
    """프로젝트명으로 레포를 찾아 Claude Code 터미널 열기"""
    SCAN_ROOTS = [
        r"X:\01_work",
        r"X:\01_work\_agents",
        r"X:\01_work\hive-media",
        r"X:\01_work\my-project",
        r"C:\Users\USER\Desktop",
    ]
    MAX_DEPTH = 3
    norm_proj = proj.lower().replace("-", "").replace("_", "").replace(" ", "")

    target_path = None
    seen = set()
    for root in SCAN_ROOTS:
        if target_path or not os.path.isdir(root):
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if dirpath in seen:
                    dirnames.clear(); continue
                seen.add(dirpath)
                depth = len(os.path.relpath(dirpath, root).split(os.sep))
                if depth > MAX_DEPTH:
                    dirnames.clear(); continue
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d not in ("node_modules", "venv", "__pycache__", ".git")
                ]
                if ".git" in filenames or ".git" in os.listdir(dirpath):
                    repo_name = os.path.basename(dirpath).lower()
                    norm_repo = repo_name.replace("-", "").replace("_", "").replace(" ", "")
                    if norm_repo == norm_proj:
                        target_path = dirpath
                        break
        except (PermissionError, OSError):
            pass

    if not target_path:
        return {"ok": False, "error": f"경로를 찾을 수 없음: {proj}"}

    CLAUDE_CMD = r"C:\Users\USER\AppData\Roaming\npm\claude.cmd"

    # 1) 새 탭 열기 + claude 실행 (CLAUDECODE 환경변수 제거하여 중첩 세션 오류 우회)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    try:
        subprocess.Popen(
            f'wt new-tab -d "{target_path}" -- cmd /k "{CLAUDE_CMD}"',
            shell=True,
            env=env
        )
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

    # 2) WT 창 강제 포커스 (최소화 복원 + 앞으로 가져오기)
    ps = r"""
$sig = @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
Add-Type -MemberDefinition $sig -Name 'Win32' -Namespace 'WinAPI' -ErrorAction SilentlyContinue
$wt = Get-Process -Name 'WindowsTerminal' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wt) {
    [WinAPI.Win32]::ShowWindow($wt.MainWindowHandle, 9)        # SW_RESTORE
    Start-Sleep -Milliseconds 200
    [WinAPI.Win32]::SetForegroundWindow($wt.MainWindowHandle)
}
"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
        shell=False
    )

    return {"ok": True, "path": target_path, "method": "wt"}


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
        """클로드 프로젝트 디렉토리명 → 사람이 읽기 쉬운 프로젝트명
        X--01-work-hive-media-chunking-english → chunking-english
        G---------01-work-hive-media-auto-blog → auto-blog
        C--Users-USER → 기타
        """
        import re as _re
        # 드라이브 접두어(단일 대문자 + 연속 하이픈) 제거
        cleaned = _re.sub(r'^[A-Z]-+', '', d)
        # 알려진 경로 접두어 제거 (긴 것 우선)
        for pfx in [
            '01-work-hive-media-',
            '01-work-my-project-',
            '01-work--agents-',
            '01-work-agents-',
            '01-work-',
        ]:
            if cleaned.startswith(pfx):
                result = cleaned[len(pfx):]
                return result if result else cleaned
        if not cleaned or cleaned == 'Users-USER':
            return '기타'
        return cleaned

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

    result = {
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

    import json as _json2, urllib.request as _ur

    # 로컬 캐시 저장
    conn_c = get_db()
    conn_c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('claude_usage_cache', ?)",
                   (_json2.dumps(result),))
    conn_c.commit()

    # 원격 서버에도 push
    remote_row = conn_c.execute("SELECT value FROM settings WHERE key='remote_url'").fetchone()
    conn_c.close()
    remote_url = remote_row["value"].strip() if remote_row else ""
    if remote_url:
        try:
            req = _ur.Request(
                f"{remote_url}/api/claude-usage-push",
                data=_json2.dumps(result).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req, timeout=3)
        except Exception:
            pass

    return result


@app.get("/api/settings/remote-url")
def get_remote_url():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='remote_url'").fetchone()
    conn.close()
    return {"url": row["value"] if row else ""}

@app.post("/api/settings/remote-url")
def set_remote_url(url: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('remote_url', ?)", (url,))
    conn.commit()
    conn.close()
    return {"url": url}


@app.post("/api/claude-usage-push")
def claude_usage_push(payload: dict):
    """로컬에서 push한 claude 사용량을 DB에 캐시"""
    import json as _json4
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('claude_usage_cache', ?)",
                 (_json4.dumps(payload),))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/claude-usage-cached")
def claude_usage_cached():
    """캐시된 claude 사용량 반환 (클라우드 서버용)"""
    import json as _json3
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='claude_usage_cache'").fetchone()
    conn.close()
    if not row:
        return {"error": "캐시 없음",
                "month_tokens": 0, "today_tokens": 0, "month_total": {},
                "today_total": {}, "used_pct": 0, "remain_pct": 100,
                "by_project": [], "month_label": date.today().strftime("%Y년 %m월")}
    return _json3.loads(row["value"])



ORG_ROLE_LABELS = {"ceo": "대표", "manager": "팀장", "member": "팀원"}
ORG_STATUS_LABELS = {
    "planned": "예정",
    "in_progress": "진행중",
    "blocked": "막힘",
    "review": "검토요청",
    "done": "완료",
}
PRIORITY_LABELS = {1: "긴급", 2: "중요", 3: "일반"}


def get_org_user(conn, user_id: int):
    return conn.execute("SELECT * FROM org_users WHERE id = ?", (user_id,)).fetchone()


def get_visible_org_user_ids(conn, user_row):
    if user_row["role"] == "ceo":
        rows = conn.execute("SELECT id FROM org_users ORDER BY sort_order, id").fetchall()
        return [row["id"] for row in rows]
    if user_row["role"] == "manager":
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM org_users WHERE id = ?
                UNION ALL
                SELECT u.id
                FROM org_users u
                JOIN descendants d ON u.manager_id = d.id
            )
            SELECT id FROM descendants
            ORDER BY id
            """,
            (user_row["id"],),
        ).fetchall()
        return [row["id"] for row in rows]
    return [user_row["id"]]


def get_all_org_users(conn):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM org_users ORDER BY CASE role WHEN 'ceo' THEN 1 WHEN 'manager' THEN 2 ELSE 3 END, sort_order, id"
        ).fetchall()
    ]


def get_all_org_tasks(conn):
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                t.*,
                assignee.name AS assignee_name,
                assignee.role AS assignee_role,
                assignee.team AS assignee_team,
                creator.name AS created_by_name,
                parent.title AS parent_task_title
            FROM org_tasks t
            JOIN org_users assignee ON assignee.id = t.assignee_id
            JOIN org_users creator ON creator.id = t.created_by
            LEFT JOIN org_tasks parent ON parent.id = t.parent_task_id
            ORDER BY t.due_date, t.priority, t.id
            """
        ).fetchall()
    ]


def get_all_org_work_logs(conn, log_date: Optional[str] = None):
    query = """
        SELECT
            l.*,
            u.name AS user_name,
            t.title AS task_title,
            reviewer.name AS reviewer_name
        FROM org_work_logs l
        JOIN org_users u ON u.id = l.user_id
        JOIN org_tasks t ON t.id = l.task_id
        LEFT JOIN org_users reviewer ON reviewer.id = l.reviewed_by
    """
    params = []
    if log_date:
        query += " WHERE l.log_date = ?"
        params.append(log_date)
    query += " ORDER BY l.log_date DESC, l.created_at DESC"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_org_weekly_focus_rows(conn, week_start: str):
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT f.*, u.name AS user_name, u.role AS user_role, u.team AS user_team
            FROM org_weekly_focus f
            JOIN org_users u ON u.id = f.user_id
            WHERE f.week_start = ?
            ORDER BY u.sort_order, u.id
            """,
            (week_start,),
        ).fetchall()
    ]


def can_assign_org_task(conn, actor_row, assignee_id: int):
    if actor_row["role"] == "member":
        return False
    return assignee_id in get_visible_org_user_ids(conn, actor_row)


def can_manage_org_task(conn, actor_row, task_row):
    visible_ids = get_visible_org_user_ids(conn, actor_row)
    if actor_row["role"] == "member":
        return task_row["assignee_id"] == actor_row["id"]
    return task_row["assignee_id"] in visible_ids or task_row["created_by"] == actor_row["id"]


def make_org_summary(actor, tasks, logs_today, visible_users):
    open_tasks = [task for task in tasks if task["status"] != "done"]
    overdue = [task for task in open_tasks if task["due_date"] < str(date.today())]
    blocked = [task for task in open_tasks if task["status"] == "blocked"]
    review = [task for task in tasks if task["status"] == "review"]
    return {
        "actor_name": actor["name"],
        "actor_role": actor["role"],
        "visible_user_count": len(visible_users),
        "total_tasks": len(tasks),
        "open_tasks": len(open_tasks),
        "done_tasks": len([task for task in tasks if task["status"] == "done"]),
        "overdue_tasks": len(overdue),
        "blocked_tasks": len(blocked),
        "review_tasks": len(review),
        "today_log_count": len(logs_today),
    }


def build_org_dashboard(conn, actor_row):
    today = str(date.today())
    week_start = get_week_start(today)
    visible_ids = set(get_visible_org_user_ids(conn, actor_row))
    users = [user for user in get_all_org_users(conn) if user["id"] in visible_ids]
    tasks = [
        task
        for task in get_all_org_tasks(conn)
        if task["assignee_id"] in visible_ids or task["created_by"] in visible_ids
    ]
    logs_today = [log for log in get_all_org_work_logs(conn, today) if log["user_id"] in visible_ids]
    weekly_focus = [focus for focus in get_org_weekly_focus_rows(conn, week_start) if focus["user_id"] in visible_ids]

    open_tasks = [task for task in tasks if task["status"] != "done"]
    my_tasks = [task for task in tasks if task["assignee_id"] == actor_row["id"]]
    my_open_tasks = [task for task in my_tasks if task["status"] != "done"]
    missing_logs = []
    for user in users:
        if user["role"] == "ceo":
            continue
        user_open = [task for task in open_tasks if task["assignee_id"] == user["id"]]
        has_today_log = any(log["user_id"] == user["id"] for log in logs_today)
        if user_open and not has_today_log:
            missing_logs.append({
                "user_id": user["id"],
                "name": user["name"],
                "team": user["team"],
                "open_task_count": len(user_open),
            })

    highlights = []
    if actor_row["role"] == "ceo":
        highlights.append(f"오늘 볼 핵심: 지연 업무 {len([task for task in open_tasks if task['due_date'] < today])}건")
        highlights.append(f"업무일지 미작성자 {len(missing_logs)}명")
        highlights.append(f"검토 대기 업무 {len([task for task in tasks if task['status'] == 'review'])}건")
    elif actor_row["role"] == "manager":
        direct_reports = [user for user in users if user.get("manager_id") == actor_row["id"]]
        highlights.append(f"직접 관리 팀원 {len(direct_reports)}명")
        highlights.append(f"오늘 검토할 업무일지 {len([log for log in logs_today if log['review_status'] in ('submitted', 'needs_update') and log['user_id'] != actor_row['id']])}건")
        highlights.append(f"지연 업무 {len([task for task in open_tasks if task['due_date'] < today])}건")
    else:
        overdue = [task for task in my_open_tasks if task["due_date"] < today]
        due_today = [task for task in my_open_tasks if task["due_date"] == today]
        highlights.append(f"오늘 끝내야 할 일 {len(due_today)}건")
        highlights.append(f"밀린 일 {len(overdue)}건")
        highlights.append(f"오늘 업무일지 {len([log for log in logs_today if log['user_id'] == actor_row['id']])}건 작성")

    teams = []
    if actor_row["role"] == "ceo":
        managers = [user for user in users if user["role"] == "manager"]
        for manager in managers:
            team_user_ids = set(get_visible_org_user_ids(conn, manager)) - {manager["id"]}
            team_tasks = [task for task in tasks if task["assignee_id"] in team_user_ids]
            team_logs = [log for log in logs_today if log["user_id"] in team_user_ids]
            teams.append({
                "manager_id": manager["id"],
                "manager_name": manager["name"],
                "team": manager["team"],
                "open_tasks": len([task for task in team_tasks if task["status"] != "done"]),
                "overdue_tasks": len([task for task in team_tasks if task["status"] != "done" and task["due_date"] < today]),
                "today_logs": len(team_logs),
            })

    reportees = []
    if actor_row["role"] == "manager":
        direct_reports = [user for user in users if user.get("manager_id") == actor_row["id"]]
        for reportee in direct_reports:
            user_tasks = [task for task in tasks if task["assignee_id"] == reportee["id"]]
            reportees.append({
                "user_id": reportee["id"],
                "name": reportee["name"],
                "team": reportee["team"],
                "open_tasks": len([task for task in user_tasks if task["status"] != "done"]),
                "overdue_tasks": len([task for task in user_tasks if task["status"] != "done" and task["due_date"] < today]),
                "has_today_log": any(log["user_id"] == reportee["id"] for log in logs_today),
            })

    my_weekly_focus = next((focus for focus in weekly_focus if focus["user_id"] == actor_row["id"]), None)
    reminders = []
    if actor_row["role"] == "member":
        ordered_tasks = sorted(
            my_open_tasks,
            key=lambda task: (task["due_date"] > today, task["due_date"], task["priority"]),
        )
        for task in ordered_tasks[:5]:
            if task["due_date"] < today:
                kind = "지연"
            elif task["due_date"] == today:
                kind = "오늘"
            else:
                kind = "예정"
            reminders.append({
                "task_id": task["id"],
                "kind": kind,
                "title": task["title"],
                "due_date": task["due_date"],
            })

    review_queue = []
    if actor_row["role"] in ("ceo", "manager"):
        review_queue = [
            log for log in logs_today
            if log["user_id"] != actor_row["id"] and log["review_status"] in ("submitted", "needs_update")
        ]

    return {
        "today": today,
        "week_start": week_start,
        "summary": make_org_summary(actor_row, tasks, logs_today, users),
        "highlights": highlights,
        "teams": teams,
        "reportees": reportees,
        "missing_logs": missing_logs,
        "review_queue": review_queue,
        "my_weekly_focus": my_weekly_focus,
        "reminders": reminders,
        "tasks": tasks,
        "weekly_focus": weekly_focus,
        "logs_today": logs_today,
        "visible_users": users,
        "assignee_options": [user for user in users if user["role"] != "ceo" or actor_row["role"] == "ceo"],
    }


def serialize_org_user(row):
    data = dict(row)
    data["role_label"] = ORG_ROLE_LABELS.get(data["role"], data["role"])
    data["title"] = data.get("title") or data["role_label"]
    data.pop("demo_password", None)
    return data


def serialize_demo_account(row):
    data = serialize_org_user(row)
    data["demo_password"] = row["demo_password"]
    return data


def serialize_org_task(task):
    task = dict(task)
    task["status_label"] = ORG_STATUS_LABELS.get(task["status"], task["status"])
    task["priority_label"] = PRIORITY_LABELS.get(task["priority"], str(task["priority"]))
    return task


def serialize_org_work_log(log):
    return dict(log)


@app.get("/api/org/users")
def org_users():
    conn = get_db()
    users = [serialize_org_user(row) for row in get_all_org_users(conn)]
    conn.close()
    return users


@app.get("/api/org/demo-accounts")
def org_demo_accounts():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT *
        FROM org_users
        WHERE demo_login_id IS NOT NULL AND demo_password IS NOT NULL
        ORDER BY CASE role WHEN 'ceo' THEN 1 WHEN 'manager' THEN 2 ELSE 3 END, sort_order, id
        """
    ).fetchall()
    accounts = [serialize_demo_account(row) for row in rows]
    conn.close()
    return accounts


@app.post("/api/org/demo-login")
def org_demo_login(payload: DemoLoginRequest):
    conn = get_db()
    row = conn.execute(
        """
        SELECT *
        FROM org_users
        WHERE demo_login_id = ? AND demo_password = ?
        """,
        (payload.login_id.strip(), payload.password.strip()),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "테스트 계정 정보가 맞지 않습니다.")
    return {"user": serialize_org_user(row)}


@app.get("/api/org/state")
def org_state(user_id: int):
    conn = get_db()
    actor = get_org_user(conn, user_id)
    if not actor:
        conn.close()
        raise HTTPException(404, "User not found")
    dashboard = build_org_dashboard(conn, actor)
    conn.close()
    return {
        "user": serialize_org_user(actor),
        "today": dashboard["today"],
        "week_start": dashboard["week_start"],
        "summary": dashboard["summary"],
        "highlights": dashboard["highlights"],
        "teams": dashboard["teams"],
        "reportees": dashboard["reportees"],
        "missing_logs": dashboard["missing_logs"],
        "review_queue": [serialize_org_work_log(log) for log in dashboard["review_queue"]],
        "reminders": dashboard["reminders"],
        "my_weekly_focus": dashboard["my_weekly_focus"],
        "tasks": [serialize_org_task(task) for task in dashboard["tasks"]],
        "weekly_focus": dashboard["weekly_focus"],
        "logs_today": [serialize_org_work_log(log) for log in dashboard["logs_today"]],
        "visible_users": [serialize_org_user(user) for user in dashboard["visible_users"]],
        "assignee_options": [serialize_org_user(user) for user in dashboard["assignee_options"]],
    }


@app.post("/api/org/tasks")
def create_org_task(task: OrgTaskCreate):
    conn = get_db()
    actor = get_org_user(conn, task.actor_id)
    if not actor:
        conn.close()
        raise HTTPException(404, "Actor not found")
    if not can_assign_org_task(conn, actor, task.assignee_id):
        conn.close()
        raise HTTPException(403, "You cannot assign work to that user")
    due_date = task.due_date or str(date.today())
    cur = conn.execute(
        """
        INSERT INTO org_tasks (title, description, priority, due_date, created_by, assignee_id, parent_task_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task.title, task.description, task.priority, due_date, task.actor_id, task.assignee_id, task.parent_task_id),
    )
    conn.commit()
    created = conn.execute("SELECT * FROM org_tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(created)


@app.patch("/api/org/tasks/{task_id}")
def update_org_task(task_id: int, update: OrgTaskUpdate):
    conn = get_db()
    actor = get_org_user(conn, update.actor_id)
    task = conn.execute("SELECT * FROM org_tasks WHERE id = ?", (task_id,)).fetchone()
    if not actor or not task:
        conn.close()
        raise HTTPException(404, "Not found")
    if not can_manage_org_task(conn, actor, task):
        conn.close()
        raise HTTPException(403, "You cannot update this task")

    updates = {}
    for key in ("status", "title", "description", "due_date", "priority", "notes"):
        value = getattr(update, key)
        if value is not None:
            updates[key] = value

    if actor["role"] == "member":
        forbidden = {"title", "description", "due_date", "priority"} & set(updates)
        if forbidden:
            conn.close()
            raise HTTPException(403, "Members can only update status and notes")

    if update.status == "done" and task["status"] != "done":
        updates["completed_at"] = datetime.now().isoformat()
    elif update.status and update.status != "done" and task["status"] == "done":
        updates["completed_at"] = None

    if updates:
        sets = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"UPDATE org_tasks SET {sets} WHERE id = ?", (*updates.values(), task_id))
        conn.commit()
    updated = conn.execute("SELECT * FROM org_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.post("/api/org/weekly-focus")
def upsert_org_weekly_focus(payload: OrgWeeklyFocusUpsert):
    conn = get_db()
    actor = get_org_user(conn, payload.actor_id)
    if not actor:
        conn.close()
        raise HTTPException(404, "Actor not found")
    week_start = payload.week_start or get_week_start()
    conn.execute(
        """
        INSERT INTO org_weekly_focus (user_id, week_start, focus, support_needed, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, week_start) DO UPDATE SET
            focus = excluded.focus,
            support_needed = excluded.support_needed,
            updated_at = CURRENT_TIMESTAMP
        """,
        (payload.actor_id, week_start, payload.focus, payload.support_needed),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM org_weekly_focus WHERE user_id = ? AND week_start = ?",
        (payload.actor_id, week_start),
    ).fetchone()
    conn.close()
    return dict(row)


@app.post("/api/org/worklogs")
def upsert_org_worklog(payload: OrgWorkLogUpsert):
    conn = get_db()
    actor = get_org_user(conn, payload.actor_id)
    task = conn.execute("SELECT * FROM org_tasks WHERE id = ?", (payload.task_id,)).fetchone()
    if not actor or not task:
        conn.close()
        raise HTTPException(404, "Not found")
    if task["assignee_id"] != payload.actor_id:
        conn.close()
        raise HTTPException(403, "You can only write logs for your own tasks")

    progress = max(0, min(100, payload.progress))
    log_date = payload.log_date or str(date.today())
    conn.execute(
        """
        INSERT INTO org_work_logs
            (task_id, user_id, log_date, today_done, next_plan, blockers, progress, review_status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', CURRENT_TIMESTAMP)
        ON CONFLICT(task_id, user_id, log_date) DO UPDATE SET
            today_done = excluded.today_done,
            next_plan = excluded.next_plan,
            blockers = excluded.blockers,
            progress = excluded.progress,
            review_status = 'submitted',
            updated_at = CURRENT_TIMESTAMP
        """,
        (payload.task_id, payload.actor_id, log_date, payload.today_done, payload.next_plan, payload.blockers, progress),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM org_work_logs WHERE task_id = ? AND user_id = ? AND log_date = ?",
        (payload.task_id, payload.actor_id, log_date),
    ).fetchone()
    conn.close()
    return dict(row)


@app.post("/api/org/worklogs/{worklog_id}/review")
def review_org_worklog(worklog_id: int, payload: OrgWorkLogReview):
    conn = get_db()
    actor = get_org_user(conn, payload.actor_id)
    worklog = conn.execute(
        "SELECT l.*, t.assignee_id, t.created_by FROM org_work_logs l JOIN org_tasks t ON t.id = l.task_id WHERE l.id = ?",
        (worklog_id,),
    ).fetchone()
    if not actor or not worklog:
        conn.close()
        raise HTTPException(404, "Not found")
    if actor["role"] == "member":
        conn.close()
        raise HTTPException(403, "Members cannot review logs")
    if worklog["assignee_id"] == actor["id"]:
        conn.close()
        raise HTTPException(403, "You cannot review your own log")
    task_like = {"assignee_id": worklog["assignee_id"], "created_by": worklog["created_by"]}
    if not can_manage_org_task(conn, actor, task_like):
        conn.close()
        raise HTTPException(403, "You cannot review this log")

    review_status = payload.review_status
    if review_status not in ("approved", "needs_update"):
        conn.close()
        raise HTTPException(400, "Invalid review status")

    conn.execute(
        "UPDATE org_work_logs SET review_status = ?, review_note = ?, reviewed_by = ?, reviewed_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (review_status, payload.review_note, payload.actor_id, datetime.now().isoformat(), worklog_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM org_work_logs WHERE id = ?", (worklog_id,)).fetchone()
    conn.close()
    return dict(updated)


class AgentStatusReport(BaseModel):
    project: str
    task: str
    status: str  # start | step | done
    url: Optional[str] = ""


@app.post("/api/agent-status")
def post_agent_status(report: AgentStatusReport):
    conn = get_db()
    ensure_table_column(conn, "agent_status", "url", "TEXT DEFAULT ''")
    conn.execute(
        """INSERT INTO agent_status (project, task, status, url, updated_at)
           VALUES (?, ?, ?, ?, datetime('now','localtime'))
           ON CONFLICT(project) DO UPDATE SET
             task=excluded.task, status=excluded.status, url=excluded.url, updated_at=excluded.updated_at""",
        (report.project, report.task, report.status, report.url or "")
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/agent-status")
def get_agent_status():
    conn = get_db()
    ensure_table_column(conn, "agent_status", "url", "TEXT DEFAULT ''")
    rows = conn.execute(
        """SELECT project, task, status, url, updated_at,
                  CAST((julianday('now','localtime') - julianday(updated_at)) * 1440 AS INTEGER) AS minutes_ago
           FROM agent_status
           ORDER BY updated_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


app.mount("/static", StaticFiles(directory="static"), name="static")
