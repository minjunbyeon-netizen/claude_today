from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import json
import sqlite3
import os
import re
import shutil
import subprocess
import secrets
import httpx
from datetime import date, datetime, timedelta, timezone

# ── KST (UTC+9) 헬퍼 ─────────────────────────────────────────────────
_KST = timezone(timedelta(hours=9))

def kst_today() -> str:
    """오늘 날짜를 KST 기준 YYYY-MM-DD 로 반환 (서버 timezone 무관)."""
    return datetime.now(_KST).strftime("%Y-%m-%d")

def kst_now() -> str:
    """현재 시각을 KST 기준 YYYY-MM-DD HH:MM:SS 로 반환."""
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

def kst_now_dt() -> datetime:
    """KST 기준 현재 datetime (timezone-naive) 반환."""
    return datetime.now(_KST).replace(tzinfo=None)

app = FastAPI()

# --- Load .env if present ---
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# --- Auth Config ---
# codeteam fix: token_hex(32) 기본값은 재시작마다 새 키 생성 → 세션 무효화.
# .env 또는 환경변수에 SECRET_KEY가 없으면 즉시 종료하여 운영자가 반드시 설정하도록 강제.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<64-char hex> to your .env file. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
ALLOWED_GITHUB_USERS = set(
    u.strip() for u in os.getenv("ALLOWED_GITHUB_USERS", "").split(",") if u.strip()
)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")
from urllib.parse import urlparse as _urlparse
_BASE_PATH = _urlparse(BASE_URL).path.rstrip("/")  # "" locally, "/daily-focus" on droplet

_AUTH_PUBLIC_PATHS = {"/login", "/auth/github", "/auth/callback", "/logout", "/health",
                      "/api/agent-status", "/api/coding-report", "/api/coding-reports"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    user = request.session.get("user")
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return RedirectResponse(f"{_BASE_PATH}/login")
    return await call_next(request)
DB_PATH = "data/focus.db"
AI_SPEED_FACTOR = 0.42
# codeteam fix: 하드코딩된 Windows 절대경로 및 사용자명을 환경변수로 대체
#   REPO_SCAN_ROOTS: 쉼표 구분 경로 목록. 기본값 = ~/work, ~/Desktop
#   WORKSPACE_WATCH_ROOT: 단일 경로. 기본값 = ~/work
_default_work = str(Path.home() / "work")
_default_desktop = str(Path.home() / "Desktop")
REPO_SCAN_ROOTS = [
    p.strip()
    for p in os.getenv("REPO_SCAN_ROOTS", f"{_default_work},{_default_desktop}").split(",")
    if p.strip()
]
REPO_SCAN_MAX_DEPTH = 2
REPO_SCAN_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
WORKSPACE_WATCH_ROOT = os.getenv("WORKSPACE_WATCH_ROOT", _default_work)
WORKSPACE_SCAN_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "$Recycle.Bin",
    "System Volume Information",
}
_REPO_SNAPSHOT_CACHE = {"expires_at": None, "data": {}}
_WORKSPACE_PROJECT_CACHE = {"expires_at": None, "data": {}}
_LEARNING_MODEL_CACHE = {"expires_at": None, "data": None}
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
AGENT_STATUS_FALLBACKS = {
    "start": "세션 시작",
    "step": "작업 진행 중",
    "done": "세션 완료",
    "warn": "확인 필요",
}
TASK_STATE_LABELS = {
    "active": "",
    "hold": "보류",
    "split": "분해됨",
}
TASK_DECISION_ACTIONS = {"continue", "hold", "carry", "split"}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    return str(row["value"] or default)


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )


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
            task_state TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            notes TEXT,
            carry_from INTEGER,
            decision_note TEXT
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
        INSERT OR IGNORE INTO settings (key, value) VALUES ('morning_brief_time', '09:00');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('workspace_backfill_completed', '0');
        CREATE TABLE IF NOT EXISTS agent_status (
            project TEXT PRIMARY KEY,
            task    TEXT NOT NULL,
            status  TEXT NOT NULL,
            url     TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS agent_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            url TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS coding_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team        TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            project     TEXT DEFAULT '',
            issues_count INTEGER DEFAULT 0,
            fixed_count  INTEGER DEFAULT 0,
            report_text  TEXT DEFAULT '',
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS task_learning_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL UNIQUE,
            project_key TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            heuristic_minutes INTEGER NOT NULL,
            actual_minutes INTEGER NOT NULL,
            ratio REAL NOT NULL,
            completed_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS task_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            note TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_goal_plans (
            date TEXT PRIMARY KEY,
            recommended_goal TEXT NOT NULL,
            user_goal TEXT DEFAULT '',
            confirmed INTEGER DEFAULT 0,
            confirmed_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS workspace_projects (
            project_name TEXT PRIMARY KEY,
            folder_path TEXT NOT NULL,
            is_repo INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_task_created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            confirmed INTEGER DEFAULT 0,
            confirmed_by TEXT,
            confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            confirmed INTEGER DEFAULT 0,
            confirmed_by TEXT,
            confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            project_name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            priority INTEGER DEFAULT 2,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS request_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT DEFAULT 'info',
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            ref_type TEXT DEFAULT '',
            ref_id INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS request_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS date_alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date TEXT NOT NULL,
            message TEXT NOT NULL,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            acknowledged_at TEXT DEFAULT NULL
        );
    """)
    conn.commit()
    conn.close()
    # 업로드 폴더 생성
    os.makedirs("data/uploads", exist_ok=True)


def ensure_table_column(conn, table_name: str, column_name: str, column_def: str):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
        conn.commit()


def ensure_base_schema() -> None:
    conn = get_db()
    ensure_table_column(conn, "tasks", "updated_at", "TEXT")
    ensure_table_column(conn, "tasks", "task_state", "TEXT")
    ensure_table_column(conn, "tasks", "decision_note", "TEXT")
    ensure_table_column(conn, "requests", "project_name", "TEXT DEFAULT ''")
    ensure_table_column(conn, "requests", "description", "TEXT DEFAULT ''")
    # ── Foundation OS 컬럼 (Phase 1) ──────────────────────────────
    ensure_table_column(conn, "tasks", "stack", "TEXT DEFAULT ''")          # 수익/성장/실험/없음
    ensure_table_column(conn, "tasks", "completion_criteria", "TEXT DEFAULT ''")  # 완료 기준 한 문장
    ensure_table_column(conn, "tasks", "output_note", "TEXT DEFAULT ''")    # 완료 시 아웃풋 기록
    ensure_table_column(conn, "tasks", "priority_quadrant", "TEXT DEFAULT ''")    # 즉시/스프린트/자동화/동결
    # ── Foundation OS 신규 테이블 (Phase 1) ──────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            output_criteria TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            output_note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fkc_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            verdict TEXT NOT NULL,
            reason TEXT DEFAULT '',
            auto INTEGER DEFAULT 0,
            judged_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_boot_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            yesterday_done TEXT DEFAULT '',
            today_task_id INTEGER,
            completion_criteria TEXT DEFAULT '',
            start_point TEXT DEFAULT '',
            expected_blocker TEXT DEFAULT '',
            confirmed INTEGER DEFAULT 0,
            confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS time_block_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            block TEXT NOT NULL,
            output_note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('morning_brief_time', '09:00')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('workspace_watch_seeded', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('workspace_backfill_completed', '0')")
    conn.execute(
        """
        UPDATE tasks
        SET updated_at = COALESCE(updated_at, completed_at, created_at, datetime('now','localtime'))
        WHERE updated_at IS NULL OR updated_at = ''
        """
    )
    conn.execute(
        """
        UPDATE tasks
        SET task_state = CASE
            WHEN status = 'done' THEN COALESCE(task_state, 'active')
            WHEN status = 'carried_over' THEN 'active'
            ELSE COALESCE(NULLIF(task_state, ''), 'active')
        END
        WHERE task_state IS NULL OR task_state = ''
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            note TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_goal_plans (
            date TEXT PRIMARY KEY,
            recommended_goal TEXT NOT NULL,
            user_goal TEXT DEFAULT '',
            confirmed INTEGER DEFAULT 0,
            confirmed_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_projects (
            project_name TEXT PRIMARY KEY,
            folder_path TEXT NOT NULL,
            is_repo INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            auto_task_created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def clean_utf8_text(value, limit: Optional[int] = None) -> str:
    text = str(value or "").replace("\x00", "")
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    if limit is not None:
        text = text[:limit]
    return text


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def text_quality_score(value: str) -> float:
    text = str(value or "")
    if not text:
        return -999.0

    score = 0.0
    for ch in text:
        code = ord(ch)
        if ch == "?":
            score -= 1.8
        elif ch == "\ufffd":
            score -= 3.0
        elif "가" <= ch <= "힣":
            score += 2.4
        elif ch.isascii() and (ch.isalnum() or ch in "`~!@#$%^&*()-_=+[]{};:'\",.<>/\\|"):
            score += 1.6
        elif ch.isspace():
            score += 0.3
        elif 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0x0370 <= code <= 0x052F:
            score -= 2.1
        else:
            score -= 0.7

    score -= text.count("??") * 1.2
    return score / max(len(text), 1)


def repair_mojibake_text(value: str) -> str:
    original = collapse_spaces(clean_utf8_text(value))
    best = original
    best_score = text_quality_score(best)

    for encoding in ("cp949", "euc-kr", "latin1"):
        try:
            candidate = original.encode(encoding, errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            continue
        candidate = collapse_spaces(candidate)
        if not candidate:
            continue
        candidate_score = text_quality_score(candidate)
        if candidate_score > best_score + 0.05:
            best = candidate
            best_score = candidate_score

    return best


def looks_corrupted_text(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False

    suspicious = 0
    for ch in text:
        code = ord(ch)
        if ch == "?":
            suspicious += 2
        elif ch == "\ufffd":
            suspicious += 4
        elif 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0x0370 <= code <= 0x052F:
            suspicious += 2

    question_ratio = text.count("?") / max(len(text), 1)
    return question_ratio >= 0.08 or suspicious >= max(6, len(text) // 10) or text_quality_score(text) < -0.15


def summarize_agent_text(value: str, limit: int = 240) -> str:
    text = collapse_spaces(value)
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def compact_agent_task_preview(value: str, limit: int = 120) -> str:
    text = collapse_spaces(value)
    if not text:
        return ""

    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    text = text.replace("**", " ").replace("__", " ")
    text = re.sub(r"\s+", " ", text)

    for marker in (" --- ", " | # | ", " |---|", " | 1 | "):
        if marker in text:
            text = text.split(marker, 1)[0]
    if " 저는 " in text:
        head, tail = text.split(" 저는 ", 1)
        if len(head.strip()) >= 24:
            text = head
        else:
            text = f"{head.strip()} {tail.strip()}".strip()

    text = collapse_spaces(text)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if sentences:
        preview = sentences[0]
        if len(preview) < 36 and len(sentences) > 1:
            preview = f"{preview} {sentences[1]}".strip()
    else:
        preview = text

    preview = re.split(r"\s+\d+\.\s+", preview, maxsplit=1)[0].strip()
    preview = collapse_spaces(preview)
    if len(preview) > limit:
        preview = preview[: limit - 3].rstrip() + "..."
    return preview


def parse_local_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def project_name_from_claude_dir(dir_name: str) -> str:
    cleaned = re.sub(r"^[A-Z]-+", "", dir_name or "")
    for prefix in (
        "01-work-hive-media-",
        "01-work-my-project-",
        "01-work--agents-",
        "01-work-agents-",
        "01-work-",
        "work-",
    ):
        if cleaned.startswith(prefix):
            result = cleaned[len(prefix):]
            return result if result else cleaned
    if not cleaned or cleaned == "Users-USER":
        return "기타"
    return cleaned


def extract_claude_message_text(message: dict) -> str:
    content = (message or {}).get("content")
    if isinstance(content, str):
        return collapse_spaces(content)
    if not isinstance(content, list):
        return ""

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = collapse_spaces(item.get("text", ""))
            if text:
                parts.append(text)
    return collapse_spaces(" ".join(parts))


def iter_claude_project_dirs(project: str) -> list[Path]:
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return []

    norm = normalize_project_key(project)
    if not norm:
        return []

    matches = []
    for path in CLAUDE_PROJECTS_DIR.iterdir():
        if not path.is_dir():
            continue
        derived = project_name_from_claude_dir(path.name)
        raw_norm = normalize_project_key(path.name)
        derived_norm = normalize_project_key(derived)
        if norm == derived_norm or raw_norm.endswith(norm) or norm in raw_norm:
            matches.append(path)
    return matches


def recover_task_from_claude_logs(project: str, updated_at: Optional[str], status: str, limit: int = 240) -> str:
    target = parse_local_datetime(updated_at)
    best_text = ""
    best_delta = None

    for project_dir in iter_claude_project_dirs(project):
        jsonl_files = sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        for filepath in jsonl_files[:6]:
            try:
                with filepath.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except Exception:
                            continue

                        message = payload.get("message", {})
                        role = message.get("role", "")
                        if status == "done" and role != "assistant":
                            continue
                        if status == "step" and role not in {"user", "assistant"}:
                            continue

                        text = summarize_agent_text(extract_claude_message_text(message), limit=limit)
                        if not text or looks_corrupted_text(text):
                            continue

                        timestamp = parse_local_datetime(payload.get("timestamp"))
                        if target and timestamp:
                            delta = abs((timestamp - target).total_seconds())
                            if delta > 7200:
                                continue
                        else:
                            delta = 0

                        if best_delta is None or delta < best_delta:
                            best_text = text
                            best_delta = delta
            except OSError:
                continue

        if best_text:
            break

    return best_text


def normalize_agent_status_task(task: str, status: str, project: str = "", updated_at: Optional[str] = None, limit: int = 240) -> str:
    text = compact_agent_task_preview(repair_mojibake_text(task), limit=min(limit, 120))
    if text and not looks_corrupted_text(text):
        return text

    recovered = recover_task_from_claude_logs(project, updated_at, status, limit=limit)
    if recovered:
        preview = compact_agent_task_preview(recovered, limit=min(limit, 120))
        if preview and not looks_corrupted_text(preview):
            return preview
        return recovered

    return AGENT_STATUS_FALLBACKS.get(status, "세션 업데이트")


def repair_agent_status_table(conn, table_name: str, time_column: str) -> int:
    if not table_exists(conn, table_name):
        return 0

    rows = conn.execute(
        f"SELECT rowid AS _rowid, project, task, status, url, {time_column} AS event_time FROM {table_name}"
    ).fetchall()
    updates = []
    for row in rows:
        project = summarize_agent_text(repair_mojibake_text(row["project"]), limit=160)
        status = summarize_agent_text(clean_utf8_text(row["status"]).lower(), limit=40) or "step"
        task = normalize_agent_status_task(
            row["task"],
            status=status,
            project=project,
            updated_at=row["event_time"],
            limit=240,
        )
        url = summarize_agent_text(clean_utf8_text(row["url"]), limit=500)
        if project != row["project"] or task != row["task"] or status != row["status"] or url != (row["url"] or ""):
            updates.append((project, task, status, url, row["_rowid"]))

    for project, task, status, url, rowid in updates:
        conn.execute(
            f"UPDATE {table_name} SET project = ?, task = ?, status = ?, url = ? WHERE rowid = ?",
            (project, task, status, url, rowid),
        )

    return len(updates)


def repair_agent_status_storage() -> None:
    conn = get_db()
    try:
        updated = 0
        updated += repair_agent_status_table(conn, "agent_status", "updated_at")
        updated += repair_agent_status_table(conn, "agent_activity", "created_at")
        if updated:
            conn.commit()
    finally:
        conn.close()


def build_checkin_feed(conn, target_date: str):
    manual_rows = conn.execute(
        "SELECT * FROM checkins WHERE date = ? ORDER BY checkin_time DESC",
        (target_date,),
    ).fetchall()

    activity_rows = []
    synthetic = False
    if table_exists(conn, "agent_activity"):
        activity_rows = conn.execute(
            """SELECT project, task, status, url, created_at
               FROM agent_activity
               WHERE date(created_at) = ?
               ORDER BY created_at DESC
               LIMIT 40""",
            (target_date,),
        ).fetchall()

    # Show the latest known CC session state until fresh activity events start building up.
    if not activity_rows and target_date == kst_today():
        activity_rows = conn.execute(
            """SELECT project, task, status, url, updated_at AS created_at
               FROM agent_status
               WHERE updated_at >= datetime('now','localtime','-36 hours')
               ORDER BY updated_at DESC
               LIMIT 20"""
        ).fetchall()
        synthetic = True

    feed = []

    for row in manual_rows:
        item = dict(row)
        item["kind"] = "manual"
        item["timestamp"] = row["checkin_time"]
        feed.append(item)

    for row in activity_rows:
        item = dict(row)
        item["kind"] = "agent"
        item["timestamp"] = row["created_at"]
        item["synthetic"] = synthetic
        feed.append(item)

    feed.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return feed


def normalize_project_key(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", (value or "").strip().lower())


def extract_task_project(title: str) -> str:
    match = re.match(r"^\[([^\]]+)\]", title or "")
    return match.group(1).strip() if match else ""


def strip_task_project(title: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", title or "").strip()


init_db()
ensure_base_schema()
repair_agent_status_storage()


def tokenize_text(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9가-힣]{2,}", value or "")
    }


def round_minutes(value: float) -> int:
    rounded = int(round(float(value) / 5.0) * 5)
    return max(5, min(120, rounded))


def parse_progress_percent(*values: Optional[str]) -> Optional[int]:
    for value in values:
        if not value:
            continue
        match = re.search(r"(\d{1,3})\s*%", value)
        if match:
            return max(0, min(100, int(match.group(1))))
    return None


def minutes_since(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    parsed = parse_local_datetime(value)
    if parsed is None:
        return None
    delta = kst_now_dt() - parsed
    return max(0, int(delta.total_seconds() // 60))


def parse_local_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def clamp_number(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def infer_task_kind(title: str) -> str:
    text = strip_task_project(title)
    lowered = text.lower()
    if re.search(r"미커밋 변경사항\s*\d+개", text):
        return "cleanup"
    if any(keyword in lowered for keyword in ("fix", "오류", "버그", "null", "검증", "체크")):
        return "fix"
    if any(keyword in lowered for keyword in ("style", "css", "문구", "오타", "spacing", "ui", "ux")):
        return "polish"
    if any(keyword in lowered for keyword in ("feat", "추가", "api", "hook", "sync", "연동", "로그", "리포트", "import", "export", "화면", "페이지")):
        return "feature"
    if any(keyword in lowered for keyword in ("dashboard", "대시보드", "풀스택", "fullstack", "앱", "android", "migration", "마이그레이션", "권한", "구조", "통합", "배포")):
        return "heavy"
    if "이어서:" in text or text.startswith("이어"):
        return "continue"
    return "general"


def git_run(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def iter_repo_dirs(root: str, depth: int = 0):
    if depth > REPO_SCAN_MAX_DEPTH or not os.path.isdir(root):
        return
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir() or entry.name in REPO_SCAN_SKIP_DIRS or entry.name.startswith("."):
                    continue
                repo_git_dir = os.path.join(entry.path, ".git")
                if os.path.isdir(repo_git_dir):
                    yield entry.path
                    continue
                if depth < REPO_SCAN_MAX_DEPTH:
                    yield from iter_repo_dirs(entry.path, depth + 1)
    except (PermissionError, OSError):
        return


def collect_repo_snapshots() -> dict[str, dict]:
    now = datetime.now()
    expires_at = _REPO_SNAPSHOT_CACHE["expires_at"]
    if expires_at and expires_at > now:
        return _REPO_SNAPSHOT_CACHE["data"]

    snapshots: dict[str, dict] = {}
    today_start = f"{kst_today()} 00:00:00"

    for root in REPO_SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for repo_dir in iter_repo_dirs(root):
            repo_name = os.path.basename(repo_dir)
            repo_key = normalize_project_key(repo_name)
            dirty_count = len(
                [line for line in git_run(["status", "--short"], repo_dir).splitlines() if line.strip()]
            )
            commits_today = [
                line
                for line in git_run(
                    ["log", f"--since={today_start}", "--oneline", "--no-merges", "-8"],
                    repo_dir,
                ).splitlines()
                if line.strip()
            ]
            snapshots[repo_key] = {
                "name": repo_name,
                "path": repo_dir,
                "dirty_count": dirty_count,
                "commit_count_today": len(commits_today),
            }

    _REPO_SNAPSHOT_CACHE["expires_at"] = now + timedelta(seconds=60)
    _REPO_SNAPSHOT_CACHE["data"] = snapshots
    return snapshots


def collect_workspace_projects() -> dict[str, dict]:
    now = datetime.now()
    expires_at = _WORKSPACE_PROJECT_CACHE["expires_at"]
    if expires_at and expires_at > now:
        return _WORKSPACE_PROJECT_CACHE["data"]

    snapshots: dict[str, dict] = {}
    root = WORKSPACE_WATCH_ROOT
    if os.path.isdir(root):
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if (
                        not entry.is_dir()
                        or entry.name.startswith(".")
                        or entry.name in WORKSPACE_SCAN_SKIP_DIRS
                    ):
                        continue
                    key = normalize_project_key(entry.name)
                    if not key:
                        continue
                    snapshots[key] = {
                        "name": entry.name,
                        "path": entry.path,
                        "is_repo": os.path.isdir(os.path.join(entry.path, ".git")),
                    }
        except (PermissionError, OSError):
            snapshots = {}

    _WORKSPACE_PROJECT_CACHE["expires_at"] = now + timedelta(seconds=60)
    _WORKSPACE_PROJECT_CACHE["data"] = snapshots
    return snapshots


def find_repo_for_project(project: str, repo_snapshots: Optional[dict[str, dict]] = None) -> Optional[dict]:
    norm = normalize_project_key(project)
    if not norm:
        return None

    snapshots = repo_snapshots or collect_repo_snapshots()
    if norm in snapshots:
        return snapshots[norm]

    exact_candidates = []
    fuzzy_candidates = []
    for key, snapshot in snapshots.items():
        if key == norm:
            exact_candidates.append(snapshot)
        elif key.endswith(norm) or norm.endswith(key):
            fuzzy_candidates.append((0, snapshot))
        elif norm in key or key in norm:
            diff = abs(len(key) - len(norm))
            fuzzy_candidates.append((diff + 1, snapshot))

    if exact_candidates:
        return exact_candidates[0]
    if fuzzy_candidates:
        fuzzy_candidates.sort(key=lambda item: (item[0], item[1].get("name", "")))
        return fuzzy_candidates[0][1]

    workspace_projects = collect_workspace_projects()
    if norm in workspace_projects:
        return workspace_projects[norm]

    workspace_candidates = []
    for key, snapshot in workspace_projects.items():
        if key.endswith(norm) or norm.endswith(key):
            workspace_candidates.append((0, snapshot))
        elif norm in key or key in norm:
            diff = abs(len(key) - len(norm))
            workspace_candidates.append((diff + 1, snapshot))
    if workspace_candidates:
        workspace_candidates.sort(key=lambda item: (item[0], item[1].get("name", "")))
        return workspace_candidates[0][1]
    return None


def invalidate_workspace_project_cache() -> None:
    _WORKSPACE_PROJECT_CACHE["expires_at"] = None
    _WORKSPACE_PROJECT_CACHE["data"] = {}


def workspace_discovery_task_title(project_name: str) -> str:
    return f"[{project_name}] 새 폴더 감지 - 목표와 첫 작업 정의"


def ensure_workspace_inbox_task(conn, project_name: str, folder_path: str) -> bool:
    prefix = f"[{project_name}]%"
    existing = conn.execute(
        "SELECT 1 FROM tasks WHERE title LIKE ? LIMIT 1",
        (prefix,),
    ).fetchone()
    if existing:
        return False

    today = kst_today()
    now = kst_now()
    title = workspace_discovery_task_title(project_name)
    note = f"자동 감지된 작업 폴더: {folder_path}"  # codeteam fix: 하드코딩된 경로 문자열 제거
    conn.execute(
        """
        INSERT INTO tasks (
            date, title, estimated_minutes, priority, status, task_state, updated_at, notes
        ) VALUES (?, ?, ?, ?, 'todo', 'active', ?, ?)
        """,
        (today, title, 20, 2, now, note),
    )
    return True


def backfill_workspace_inbox_tasks(conn) -> list[dict[str, object]]:
    if get_setting(conn, "workspace_backfill_completed", "0") == "1":
        return []

    now = kst_now()
    rows = conn.execute(
        """
        SELECT project_name, folder_path
        FROM workspace_projects
        WHERE active = 1
        ORDER BY project_name
        """
    ).fetchall()
    created: list[dict[str, object]] = []

    for row in rows:
        project_name = row["project_name"]
        folder_path = row["folder_path"]
        if ensure_workspace_inbox_task(conn, project_name, folder_path):
            conn.execute(
                """
                UPDATE workspace_projects
                SET auto_task_created_at = COALESCE(auto_task_created_at, ?)
                WHERE project_name = ?
                """,
                (now, project_name),
            )
            created.append({"name": project_name, "path": folder_path})

    set_setting(conn, "workspace_backfill_completed", "1")
    conn.commit()
    return created


def sync_workspace_projects(conn) -> list[dict[str, object]]:
    snapshots = collect_workspace_projects()
    now = kst_now()
    seeded = get_setting(conn, "workspace_watch_seeded", "0") == "1"
    existing_rows = {
        row["project_name"]: dict(row)
        for row in conn.execute("SELECT * FROM workspace_projects").fetchall()
    }
    baseline_only = (not seeded) or (not existing_rows)
    created: list[dict[str, object]] = []

    if existing_rows:
        conn.execute("UPDATE workspace_projects SET active = 0")

    for snapshot in snapshots.values():
        project_name = snapshot["name"]
        existing = existing_rows.get(project_name)
        if existing:
            conn.execute(
                """
                UPDATE workspace_projects
                SET folder_path = ?, is_repo = ?, active = 1, last_seen_at = ?
                WHERE project_name = ?
                """,
                (
                    snapshot["path"],
                    1 if snapshot.get("is_repo") else 0,
                    now,
                    project_name,
                ),
            )
            continue

        conn.execute(
            """
            INSERT INTO workspace_projects (
                project_name, folder_path, is_repo, active, discovered_at, last_seen_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                project_name,
                snapshot["path"],
                1 if snapshot.get("is_repo") else 0,
                now,
                now,
            ),
        )

        if not baseline_only and ensure_workspace_inbox_task(conn, project_name, snapshot["path"]):
            conn.execute(
                """
                UPDATE workspace_projects
                SET auto_task_created_at = ?
                WHERE project_name = ?
                """,
                (now, project_name),
            )
            created.append(snapshot)

    if not seeded:
        set_setting(conn, "workspace_watch_seeded", "1")

    conn.commit()
    created.extend(backfill_workspace_inbox_tasks(conn))
    return created


def get_agent_status_map(conn) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT project, task, status, url, updated_at,
               CAST((julianday('now','localtime') - julianday(updated_at)) * 1440 AS INTEGER) AS minutes_ago
        FROM agent_status
        ORDER BY updated_at DESC
        """
    ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        key = normalize_project_key(row["project"])
        if key and key not in result:
            result[key] = dict(row)
    return result


def evaluate_task_attention(task: dict, agent_snapshot: Optional[dict]) -> dict[str, object]:
    if task.get("status") == "done":
        return {
            "attention_level": "done",
            "attention_label": "완료",
            "attention_reason": "",
            "attention_minutes": 0,
        }

    progress = int(task.get("progress_pct") or 0)
    last_task_minutes = minutes_since(task.get("updated_at") or task.get("created_at"))
    last_agent_minutes = None
    if agent_snapshot and agent_snapshot.get("minutes_ago") is not None:
        try:
            last_agent_minutes = max(0, int(agent_snapshot.get("minutes_ago") or 0))
        except (TypeError, ValueError):
            last_agent_minutes = None

    signal_candidates = [value for value in (last_task_minutes, last_agent_minutes) if value is not None]
    attention_minutes = min(signal_candidates) if signal_candidates else None
    cycle_minutes = max(
        15,
        int(
            task.get("display_minutes")
            or task.get("smart_estimated_minutes")
            or task.get("estimated_minutes")
            or 30
        ),
    )
    watch_after = max(45, min(150, cycle_minutes * 2))
    stale_after = max(120, min(300, cycle_minutes * 4))
    critical_after = max(240, min(480, cycle_minutes * 6))

    level = "ok"
    label = "정상"
    reason = "최근 업데이트 있음"

    if attention_minutes is None:
        if progress <= 10:
            level = "watch"
            label = "주시"
            reason = "아직 연결된 활동 신호가 없습니다"
        else:
            reason = "진행률 기반 추정만 있습니다"
    elif attention_minutes <= 20:
        reason = f"{attention_minutes}분 전 최신화"
    elif progress <= 10 and attention_minutes >= critical_after:
        level = "critical"
        label = "긴급 체크"
        reason = f"{attention_minutes}분째 진척이 없습니다"
    elif progress < 35 and attention_minutes >= stale_after:
        level = "stale"
        label = "정체"
        reason = f"{attention_minutes}분째 작업 신호가 멈췄습니다"
    elif progress < 70 and attention_minutes >= max(stale_after + 60, critical_after - 60):
        level = "stale"
        label = "정체"
        reason = f"{attention_minutes}분째 마무리 신호가 없습니다"
    elif attention_minutes >= watch_after:
        level = "watch"
        label = "주시"
        reason = f"{attention_minutes}분째 업데이트가 없습니다"

    return {
        "attention_level": level,
        "attention_label": label,
        "attention_reason": reason,
        "attention_minutes": attention_minutes if attention_minutes is not None else -1,
    }


def estimate_ai_minutes(task: dict, repo_snapshot: Optional[dict]) -> int:
    raw_minutes = max(5, int(task.get("estimated_minutes") or 30))
    title = strip_task_project(task.get("title", ""))
    lowered = title.lower()
    base = 8

    cleanup_match = re.search(r"미커밋 변경사항\s*(\d+)개", title)
    if cleanup_match:
        dirty_count = int(cleanup_match.group(1))
        if repo_snapshot:
            dirty_count = max(dirty_count, repo_snapshot.get("dirty_count", dirty_count))
        base = 4 + min(28, dirty_count * 2)
    else:
        quick_keywords = ("fix", "오류", "버그", "null", "spacing", "css", "문구", "오타", "버튼", "링크", "정리", "체크", "검증")
        medium_keywords = ("feat", "추가", "api", "hook", "sync", "연동", "페이지", "화면", "리포트", "로그", "필터", "쿼리", "import", "export")
        heavy_keywords = ("전면", "풀스택", "fullstack", "마이그레이션", "migration", "리팩터링", "구조", "통합", "권한", "로그인", "배포", "dashboard", "대시보드", "android", "앱", "개발")

        if "이어서:" in title:
            base += 6
        if any(keyword in lowered for keyword in quick_keywords):
            base += 4
        if any(keyword in lowered for keyword in medium_keywords):
            base += 10
        if any(keyword in lowered for keyword in heavy_keywords):
            base += 18

        separators = title.count("+") + title.count("/") + title.count("·") + title.count("—") + title.count(",")
        if " 및 " in title:
            separators += 1
        base += min(10, separators * 3)

        token_count = len(tokenize_text(title))
        if token_count > 8:
            base += 5

    if task.get("priority") == 1:
        base += 3
    if repo_snapshot and repo_snapshot.get("dirty_count", 0) >= 8:
        base += 4

    corrected_raw = max(5, raw_minutes * AI_SPEED_FACTOR)
    return round_minutes(max(base, corrected_raw))


def invalidate_learning_model_cache() -> None:
    _LEARNING_MODEL_CACHE["expires_at"] = None
    _LEARNING_MODEL_CACHE["data"] = None


def build_learning_model(conn) -> dict[str, object]:
    now = datetime.now()
    expires_at = _LEARNING_MODEL_CACHE["expires_at"]
    if expires_at and expires_at > now and _LEARNING_MODEL_CACHE["data"] is not None:
        return _LEARNING_MODEL_CACHE["data"]

    rows = conn.execute(
        """
        SELECT project_key, task_kind, ratio, actual_minutes
        FROM task_learning_samples
        WHERE completed_at >= datetime('now','localtime','-120 days')
        ORDER BY completed_at DESC
        """
    ).fetchall()

    project_kind: dict[tuple[str, str], dict[str, float]] = {}
    kind_stats: dict[str, dict[str, float]] = {}
    global_ratio_sum = 0.0
    global_actual_sum = 0.0
    global_count = 0

    for row in rows:
        key = (row["project_key"], row["task_kind"])
        if key not in project_kind:
            project_kind[key] = {"ratio_sum": 0.0, "actual_sum": 0.0, "count": 0}
        project_kind[key]["ratio_sum"] += float(row["ratio"] or 1.0)
        project_kind[key]["actual_sum"] += float(row["actual_minutes"] or 0)
        project_kind[key]["count"] += 1

        if row["task_kind"] not in kind_stats:
            kind_stats[row["task_kind"]] = {"ratio_sum": 0.0, "actual_sum": 0.0, "count": 0}
        kind_stats[row["task_kind"]]["ratio_sum"] += float(row["ratio"] or 1.0)
        kind_stats[row["task_kind"]]["actual_sum"] += float(row["actual_minutes"] or 0)
        kind_stats[row["task_kind"]]["count"] += 1

        global_ratio_sum += float(row["ratio"] or 1.0)
        global_actual_sum += float(row["actual_minutes"] or 0)
        global_count += 1

    model = {
        "project_kind": {
            key: {
                "ratio": value["ratio_sum"] / value["count"],
                "actual_minutes": value["actual_sum"] / value["count"],
                "count": value["count"],
            }
            for key, value in project_kind.items()
        },
        "kind": {
            key: {
                "ratio": value["ratio_sum"] / value["count"],
                "actual_minutes": value["actual_sum"] / value["count"],
                "count": value["count"],
            }
            for key, value in kind_stats.items()
        },
        "global": {
            "ratio": (global_ratio_sum / global_count) if global_count else 1.0,
            "actual_minutes": (global_actual_sum / global_count) if global_count else 0.0,
            "count": global_count,
        },
    }
    _LEARNING_MODEL_CACHE["expires_at"] = now + timedelta(seconds=45)
    _LEARNING_MODEL_CACHE["data"] = model
    return model


def apply_learning_minutes(task: dict, heuristic_minutes: int, learning_model: Optional[dict[str, object]]) -> tuple[int, str, int, int, str]:
    task_kind = infer_task_kind(task.get("title", ""))
    project_key = normalize_project_key(extract_task_project(task.get("title", "")))
    if not learning_model:
        return heuristic_minutes, "휴리스틱", 0, 0, task_kind

    project_stats = learning_model.get("project_kind", {}).get((project_key, task_kind))
    kind_stats = learning_model.get("kind", {}).get(task_kind)
    global_stats = learning_model.get("global", {})

    ratio_parts: list[tuple[float, float, int]] = []
    if project_stats and project_stats.get("count"):
        ratio_parts.append((float(project_stats["ratio"]), 0.55, int(project_stats["count"])))
    if kind_stats and kind_stats.get("count"):
        ratio_parts.append((float(kind_stats["ratio"]), 0.30, int(kind_stats["count"])))
    if global_stats and global_stats.get("count"):
        ratio_parts.append((float(global_stats["ratio"]), 0.15, int(global_stats["count"])))

    if not ratio_parts:
        return heuristic_minutes, "휴리스틱", 0, 0, task_kind

    total_weight = sum(weight for _, weight, _ in ratio_parts)
    learned_ratio = sum(ratio * weight for ratio, weight, _ in ratio_parts) / total_weight
    evidence = sum(min(1.0, count / 6.0) * weight for _, weight, count in ratio_parts) / total_weight
    strength = clamp_number(0.08 + evidence * 0.32, 0.08, 0.45)
    blended_ratio = 1.0 + ((learned_ratio - 1.0) * strength)
    learned_minutes = round_minutes(heuristic_minutes * clamp_number(blended_ratio, 0.65, 1.45))
    confidence = int(round(strength * 100))
    sample_count = 0
    if project_stats and project_stats.get("count"):
        sample_count = int(project_stats["count"])
    elif kind_stats and kind_stats.get("count"):
        sample_count = int(kind_stats["count"])
    elif global_stats and global_stats.get("count"):
        sample_count = int(global_stats["count"])
    source = "학습 예측" if confidence >= 45 else "보정 예측"
    return learned_minutes, source, confidence, sample_count, task_kind


def record_task_learning_sample(conn, task: dict) -> None:
    if task.get("status") != "done" or not task.get("completed_at"):
        return

    existing = conn.execute(
        "SELECT 1 FROM task_learning_samples WHERE task_id = ?",
        (task["id"],),
    ).fetchone()
    if existing:
        return

    created_at = parse_local_datetime(task.get("created_at"))
    completed_at = parse_local_datetime(task.get("completed_at"))
    if created_at is None or completed_at is None or completed_at <= created_at:
        return

    heuristic_minutes = estimate_ai_minutes(task, None)
    elapsed_minutes = max(5, int((completed_at - created_at).total_seconds() // 60))
    cap_minutes = max(20, int(max(task.get("estimated_minutes") or 0, heuristic_minutes) * 1.7))
    actual_minutes = int(clamp_number(elapsed_minutes, 5, cap_minutes))
    ratio = clamp_number(actual_minutes / max(heuristic_minutes, 5), 0.45, 1.55)

    conn.execute(
        """
        INSERT OR IGNORE INTO task_learning_samples
        (task_id, project_key, task_kind, heuristic_minutes, actual_minutes, ratio, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            normalize_project_key(extract_task_project(task.get("title", ""))),
            infer_task_kind(task.get("title", "")),
            heuristic_minutes,
            actual_minutes,
            ratio,
            task.get("completed_at"),
        ),
    )


def backfill_task_learning_samples() -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM task_learning_samples")
        rows = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'done'
              AND completed_at IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            record_task_learning_sample(conn, dict(row))
        conn.commit()
        invalidate_learning_model_cache()
    finally:
        conn.close()


backfill_task_learning_samples()


def estimate_task_progress(task: dict, repo_snapshot: Optional[dict], agent_snapshot: Optional[dict]) -> tuple[int, str]:
    if task.get("status") == "done":
        return 100, "완료"

    title = strip_task_project(task.get("title", ""))
    title_tokens = tokenize_text(title)
    note_pct = parse_progress_percent(title, task.get("notes"))
    candidates: list[tuple[int, str]] = []
    if note_pct is not None:
        candidates.append((note_pct, "노트"))

    cleanup_match = re.search(r"미커밋 변경사항\s*(\d+)개", title)
    if cleanup_match and repo_snapshot:
        original_dirty = max(1, int(cleanup_match.group(1)))
        current_dirty = max(0, int(repo_snapshot.get("dirty_count", original_dirty)))
        reduced = max(0, original_dirty - min(current_dirty, original_dirty))
        if current_dirty == 0:
            candidates.append((100, "git 정리 완료"))
        else:
            candidates.append((round(reduced / original_dirty * 100), f"git {original_dirty}->{current_dirty}"))
    elif agent_snapshot:
        base = {"start": 28, "step": 62, "done": 100, "warn": 40}.get(agent_snapshot.get("status"), 18)
        overlap = len(title_tokens & tokenize_text(agent_snapshot.get("task", "")))
        allows_project_level_match = "이어서:" in title or title.startswith("이어")
        if overlap:
            base += min(20, overlap * 7)
        elif allows_project_level_match:
            base += 8
        else:
            base = min(base, 35)
        if repo_snapshot and repo_snapshot.get("commit_count_today", 0) > 0:
            base += min(18, repo_snapshot["commit_count_today"] * 5)
            if repo_snapshot.get("dirty_count", 0) == 0:
                base = max(base, 88)
        minutes_ago = int(agent_snapshot.get("minutes_ago") or 0)
        if minutes_ago > 180 and base < 100:
            base = max(0, base - 30)
        elif minutes_ago > 60 and base < 100:
            base = max(0, base - 12)
        if overlap or allows_project_level_match or repo_snapshot:
            if not overlap and not allows_project_level_match and agent_snapshot.get("status") == "done":
                base = min(base, 45)
            candidates.append((min(100, base), "CC 세션"))
    elif repo_snapshot:
        commit_count = repo_snapshot.get("commit_count_today", 0)
        dirty_count = repo_snapshot.get("dirty_count", 0)
        if commit_count > 0:
            commit_progress = min(75, 28 + commit_count * 11)
            if dirty_count == 0:
                commit_progress = max(commit_progress, 90)
            candidates.append((commit_progress, "오늘 커밋"))
        elif "이어서:" in title and dirty_count > 0:
            candidates.append((18, "작업 흔적"))

    if not candidates:
        return 0, "대기"

    progress, source = max(candidates, key=lambda item: item[0])
    return max(0, min(100, progress)), source


def enrich_task_for_dashboard(
    task: dict,
    repo_snapshots: dict[str, dict],
    agent_map: dict[str, dict],
    learning_model: Optional[dict[str, object]] = None,
) -> dict:
    project_key = normalize_project_key(extract_task_project(task.get("title", "")))
    repo_snapshot = repo_snapshots.get(project_key)
    agent_snapshot = agent_map.get(project_key)
    heuristic_minutes = estimate_ai_minutes(task, repo_snapshot)
    smart_minutes, estimate_source, estimate_confidence, estimate_samples, task_kind = apply_learning_minutes(
        task,
        heuristic_minutes,
        learning_model,
    )
    progress_pct, progress_source = estimate_task_progress(task, repo_snapshot, agent_snapshot)
    remaining_minutes = 0
    if progress_pct < 100:
        remaining_minutes = max(0, round(smart_minutes * (100 - progress_pct) / 100))
        if remaining_minutes and remaining_minutes < 5:
            remaining_minutes = 5

    task["raw_estimated_minutes"] = task.get("estimated_minutes", heuristic_minutes)
    task["heuristic_minutes"] = heuristic_minutes
    task["smart_estimated_minutes"] = smart_minutes
    task["display_minutes"] = smart_minutes
    task["estimate_source"] = estimate_source
    task["estimate_confidence"] = estimate_confidence
    task["estimate_samples"] = estimate_samples
    task["task_kind"] = task_kind
    task["progress_pct"] = progress_pct
    task["progress_source"] = progress_source
    task["remaining_minutes"] = remaining_minutes
    task["is_active_now"] = bool(agent_snapshot and int(agent_snapshot.get("minutes_ago") or 9999) <= 15)
    task["repo_path"] = repo_snapshot.get("path") if repo_snapshot else ""
    _task_project = extract_task_project(task.get("title", ""))
    task["can_open_claude"] = bool(repo_snapshot) or bool(
        _task_project and find_repo_for_project(_task_project, repo_snapshots)
    )
    task["task_state"] = task_state_of(task)
    task["task_state_label"] = task_state_label_of(task)
    task["is_actionable"] = is_task_actionable(task)
    task.update(evaluate_task_attention(task, agent_snapshot))
    return task


def format_minutes_label(value: int) -> str:
    minutes = max(0, int(value or 0))
    hours, remain = divmod(minutes, 60)
    if hours and remain:
        return f"{hours}시간 {remain}분"
    if hours:
        return f"{hours}시간"
    return f"{remain}분"


def resolve_task_remaining_minutes(task: dict) -> int:
    if task.get("remaining_minutes") is not None:
        return max(0, int(task.get("remaining_minutes") or 0))
    fallback = (
        task.get("display_minutes")
        or task.get("smart_estimated_minutes")
        or task.get("estimated_minutes")
        or 0
    )
    return max(0, int(fallback))


def task_state_of(task: dict) -> str:
    return str(task.get("task_state") or "active")


def is_task_parked(task: dict) -> bool:
    return task.get("status") == "carried_over" or task_state_of(task) in {"hold", "split"}


def is_task_actionable(task: dict) -> bool:
    return task.get("status") not in {"done", "carried_over"} and task_state_of(task) == "active"


def task_state_label_of(task: dict) -> str:
    if task.get("status") == "carried_over":
        return "이월됨"
    return TASK_STATE_LABELS.get(task_state_of(task), "")


def build_task_stats(tasks: list[dict]) -> dict[str, object]:
    workload_tasks = [task for task in tasks if not is_task_parked(task)]
    total = len(workload_tasks)
    done = sum(1 for t in workload_tasks if t["status"] == "done")
    total_min = sum(t["display_minutes"] for t in workload_tasks)
    progress_min = round(sum(t["display_minutes"] * (t["progress_pct"] / 100) for t in workload_tasks))
    remaining_min = max(0, total_min - progress_min)
    progress_pct = round(progress_min / total_min * 100) if total_min else 0
    actionable_tasks = [task for task in tasks if is_task_actionable(task)]
    watch_count = sum(1 for t in actionable_tasks if t.get("attention_level") == "watch")
    stale_count = sum(1 for t in actionable_tasks if t.get("attention_level") == "stale")
    critical_count = sum(1 for t in actionable_tasks if t.get("attention_level") == "critical")
    active_now_count = sum(1 for t in actionable_tasks if t.get("is_active_now"))
    return {
        "total": total,
        "done": done,
        "total_minutes": total_min,
        "done_minutes": progress_min,
        "progress_minutes": progress_min,
        "remaining_minutes": remaining_min,
        "progress_pct": progress_pct,
        "monitor": {
            "watch_count": watch_count,
            "stale_count": stale_count,
            "critical_count": critical_count,
            "needs_attention": watch_count + stale_count + critical_count,
            "active_now_count": active_now_count,
            "auto_refresh_seconds": 20,
            "last_refreshed_at": kst_now(),
        },
    }


def select_focus_task(tasks: list[dict]) -> Optional[dict]:
    candidates = [
        task for task in tasks
        if is_task_actionable(task) and int(task.get("progress_pct") or 0) < 100
    ]
    if not candidates:
        candidates = [task for task in tasks if is_task_actionable(task)]
    if not candidates:
        return None

    non_critical = [task for task in candidates if task.get("attention_level") != "critical"]
    target_pool = non_critical or candidates

    def sort_key(task: dict) -> tuple:
        remaining = resolve_task_remaining_minutes(task)
        return (
            0 if task.get("is_active_now") else 1,
            task.get("priority", 2),
            remaining,
            -int(task.get("progress_pct") or 0),
            task.get("id", 0),
        )

    return min(target_pool, key=sort_key)


def build_task_launch_package(task: dict, reason: str, objective_title: str = "") -> dict[str, object]:
    project = extract_task_project(task.get("title", ""))
    task_title = strip_task_project(task.get("title", "추천 작업"))
    remaining_label = format_minutes_label(resolve_task_remaining_minutes(task))
    progress_pct = int(task.get("progress_pct") or 0)
    prompt_parts = [
        f"프로젝트 {project}에서 다음 작업을 이어서 진행해줘.",
        f"현재 추천 작업: {task_title}",
        f"현재 진행률: {progress_pct}%",
        f"예상 남은 시간: {remaining_label}",
        f"추천 이유: {reason}",
    ]
    if objective_title:
        prompt_parts.append(f"오늘의 목적: {objective_title}")
    if task.get("attention_reason"):
        prompt_parts.append(f"참고 상태: {task.get('attention_reason')}")
    prompt_parts.append("먼저 현재 상태를 3줄 이내로 정리하고, 바로 실행할 가장 작은 다음 단계부터 진행해줘.")
    launch_prompt = " ".join(collapse_spaces(part) for part in prompt_parts if part)
    command_preview = f'claude "{project} · {task_title} 이어서 진행"'
    return {
        "project": project,
        "can_launch": bool(task.get("can_open_claude")),
        "launch_prompt": launch_prompt,
        "command_preview": command_preview,
    }


def build_recommended_queue(tasks: list[dict], focus_task: Optional[dict]) -> list[dict[str, str]]:
    todo_tasks = [task for task in tasks if is_task_actionable(task)]
    queue: list[dict[str, str]] = []
    seen: set[int] = set()

    def push(task: Optional[dict], tone: str, detail: str, objective_title: str = "") -> None:
        if not task or task.get("id") in seen:
            return
        seen.add(task["id"])
        payload = {
            "task_id": task.get("id"),
            "tone": tone,
            "title": strip_task_project(task.get("title", "추천 작업")),
            "detail": detail,
        }
        payload.update(build_task_launch_package(task, detail, objective_title))
        queue.append(payload)

    critical = next((task for task in todo_tasks if task.get("attention_level") == "critical"), None)
    stale = next((task for task in todo_tasks if task.get("attention_level") == "stale"), None)
    nearly_done = next(
        (
            task for task in todo_tasks
            if 65 <= int(task.get("progress_pct") or 0) < 100 and resolve_task_remaining_minutes(task) <= 35
        ),
        None,
    )
    quick_win = next(
        (
            task for task in sorted(
                todo_tasks,
                key=lambda item: ((item.get("priority") or 9), resolve_task_remaining_minutes(item)),
            )
            if 0 < resolve_task_remaining_minutes(task) <= 25 and int(task.get("progress_pct") or 0) < 100
        ),
        None,
    )

    if critical:
        push(
            critical,
            "critical",
            f"{critical.get('attention_minutes', 0)}분째 멈춰 있어 먼저 결정이 필요한 작업입니다.",
        )
    elif stale:
        push(
            stale,
            "stale",
            f"{stale.get('attention_minutes', 0)}분째 신호가 없어 재가동이 필요한 작업입니다.",
        )

    if focus_task:
        push(
            focus_task,
            "focus",
            f"지금 가장 효율적인 집중 후보입니다. 남은 {format_minutes_label(resolve_task_remaining_minutes(focus_task))} 정도를 기준으로 잡았습니다.",
        )

    if nearly_done:
        push(
            nearly_done,
            "focus",
            "완료선이 가까워서 먼저 닫으면 전체 진척 체감이 크게 올라갑니다.",
        )

    if quick_win:
        push(
            quick_win,
            "watch",
            "짧게 끝낼 수 있는 작업이라 흐름을 만들기 좋습니다.",
        )

    # fallback: 위 조건 중 아무것도 안 걸려도 우선순위 상위 태스크를 채워줌
    for task in sorted(todo_tasks, key=lambda t: ((t.get("priority") or 9), t.get("id", 0))):
        if len(queue) >= 3:
            break
        push(task, "focus", f"우선순위 {task.get('priority', '-')}순 작업입니다.")

    return queue[:3]


def build_ops_brief(conn, target_date: str, tasks: list[dict], stats: dict[str, object]) -> dict[str, object]:
    todo_tasks = [task for task in tasks if is_task_actionable(task)]
    done_tasks = [task for task in tasks if task.get("status") == "done"]
    critical_tasks = [task for task in todo_tasks if task.get("attention_level") == "critical"]
    stale_tasks = [task for task in todo_tasks if task.get("attention_level") == "stale"]
    watch_tasks = [task for task in todo_tasks if task.get("attention_level") == "watch"]
    active_tasks = [task for task in todo_tasks if task.get("is_active_now")]
    focus_task = select_focus_task(tasks)
    latest_manual = conn.execute(
        "SELECT note, checkin_time FROM checkins WHERE date = ? ORDER BY checkin_time DESC LIMIT 1",
        (target_date,),
    ).fetchone()
    headline = (
        f"오늘 {stats['done']}/{stats['total']} 완료 · "
        f"{stats['progress_pct']}% 진행 · 남은 {format_minutes_label(stats['remaining_minutes'])}"
    )

    if stats["total"] == 0:
        objective_title = "오늘의 목적부터 세우기"
        objective_detail = "가장 중요한 3개를 먼저 등록해서 오늘 작업판의 중심을 만드는 게 좋습니다."
        focus_message = "오늘 작업이 아직 비어 있습니다. 가장 중요한 3개부터 등록해 흐름을 시작하세요."
    elif active_tasks:
        top_active = active_tasks[0]
        objective_title = f"{strip_task_project(top_active.get('title', '현재 작업'))} 끝까지 밀기"
        objective_detail = "이미 흐름이 붙은 작업을 먼저 닫는 편이 가장 효율적입니다."
        focus_message = (
            f"지금은 {strip_task_project(top_active.get('title', '현재 작업'))}에 흐름이 있습니다. "
            f"새 일보다 남은 {format_minutes_label(resolve_task_remaining_minutes(top_active))}를 먼저 밀어주세요."
        )
    elif critical_tasks:
        objective_title = "멈춘 작업 정리"
        objective_detail = "오래 멈춘 작업을 계속, 보류, 분해 중 하나로 먼저 정리해야 다음 흐름이 열립니다."
        focus_message = (
            f"{len(critical_tasks)}개 작업이 오래 멈췄습니다. "
            "계속할 일 1개만 남기고 나머지는 보류, 분해, 이월 중 하나로 빨리 결정하는 편이 좋습니다."
        )
    elif focus_task:
        objective_title = f"{strip_task_project(focus_task.get('title', '집중 작업'))} 전진"
        objective_detail = "지금 기준으로 가장 효율이 좋은 한 가지를 끝까지 미는 전략입니다."
        focus_message = (
            f"지금 가장 효율적인 다음 수는 {strip_task_project(focus_task.get('title', '집중 작업'))}입니다. "
            "진행 중인 범위를 더 키우지 말고 한 번 끝까지 밀어보세요."
        )
    else:
        objective_title = "완료 1개 추가 만들기"
        objective_detail = "오늘 흐름은 안정적이니, 체감 성과를 올릴 작은 완료 1개를 더 만드는 단계입니다."
        focus_message = "오늘 흐름은 안정적입니다. 완료 1개를 더 만들면 체감 진척이 크게 올라갑니다."

    recommended_queue = build_recommended_queue(tasks, focus_task)
    for item in recommended_queue:
        if item.get("launch_prompt") and objective_title:
            item["launch_prompt"] = f"{item['launch_prompt']} 오늘의 큰 목적은 {objective_title}입니다."

    next_actions: list[dict[str, str]] = []
    if critical_tasks:
        top = critical_tasks[0]
        next_actions.append({
            "tone": "critical",
            "title": "정체 작업 먼저 결정",
            "detail": (
                f"{strip_task_project(top.get('title', '정체 작업'))}이 "
                f"{top.get('attention_minutes', 0)}분째 멈췄습니다. "
                "10분 안에 계속, 보류, 분해 중 하나로 정리하세요."
            ),
        })
    elif stale_tasks:
        top = stale_tasks[0]
        next_actions.append({
            "tone": "stale",
            "title": "멈춘 작업 재가동",
            "detail": (
                f"{strip_task_project(top.get('title', '멈춘 작업'))} 쪽 신호가 "
                f"{top.get('attention_minutes', 0)}분째 없습니다. 작은 다음 단계 1개로 다시 시작하세요."
            ),
        })

    if focus_task:
        next_actions.append({
            "tone": "focus",
            "title": "다음 집중 작업",
            "detail": (
                f"{strip_task_project(focus_task.get('title', '집중 작업'))}에 "
                f"{format_minutes_label(resolve_task_remaining_minutes(focus_task))} 정도를 배정하면 좋습니다."
            ),
        })

    if stats["remaining_minutes"] >= 180:
        next_actions.append({
            "tone": "watch",
            "title": "오늘 범위 줄이기",
            "detail": (
                f"남은 분량이 {format_minutes_label(stats['remaining_minutes'])}입니다. "
                "오늘 꼭 끝낼 3개만 남기고 나머지는 내일 후보로 내려두는 편이 안전합니다."
            ),
        })
    elif stats["done"] == 0 and stats["total"] > 0:
        next_actions.append({
            "tone": "watch",
            "title": "첫 완료 1개 만들기",
            "detail": "작게라도 완료 1개를 먼저 만들면 전체 진행률과 집중감이 같이 올라갑니다.",
        })

    if not latest_manual and target_date == kst_today():
        next_actions.append({
            "tone": "note",
            "title": "체크인 남기기",
            "detail": "막힌 점과 다음 집중 포인트를 한 줄만 적어도 이후 판단 속도가 빨라집니다.",
        })

    wins: list[dict[str, str]] = []
    for task in done_tasks[:3]:
        wins.append({
            "title": strip_task_project(task.get("title", "완료 작업")),
            "detail": "완료됨",
        })
    if not wins:
        for task in active_tasks[:2]:
            wins.append({
                "title": strip_task_project(task.get("title", "진행 작업")),
                "detail": task.get("attention_reason") or "지금 흐름이 유지되고 있습니다",
            })

    risks: list[dict[str, str]] = []
    for task in (critical_tasks + stale_tasks + watch_tasks)[:3]:
        risks.append({
            "task_id": task.get("id"),
            "tone": task.get("attention_level") or "watch",
            "title": strip_task_project(task.get("title", "주의 작업")),
            "detail": task.get("attention_reason") or "상태 확인 필요",
        })

    latest_note = ""
    latest_note_time = ""
    if latest_manual:
        latest_note = collapse_spaces(latest_manual["note"] or "")
        latest_note_time = latest_manual["checkin_time"] or ""

    # ── [A] 시간 인텔리전스 ──────────────────────────────────────
    now = kst_now_dt()
    work_end_hour = 19  # 오후 7시 마감 가정
    remaining_work_min = max(0, (work_end_hour * 60) - (now.hour * 60 + now.minute))
    task_rem = int(stats.get("remaining_minutes") or 0)
    forecast_end = ""
    if task_rem > 0:
        fcast = now + timedelta(minutes=task_rem)
        if fcast.date() == now.date():
            suffix = "오후" if fcast.hour >= 12 else "오전"
            h = fcast.hour if fcast.hour <= 12 else fcast.hour - 12
            forecast_end = f"{suffix} {h}:{fcast.strftime('%M')} 마감 예상"
        else:
            forecast_end = "오늘 내 완료 어려움"
    overload = task_rem > remaining_work_min and remaining_work_min > 0

    # ── [B] 이월 추적 ─────────────────────────────────────────────
    carry_info = []
    for t in todo_tasks:
        if not t.get("carry_from"):
            continue
        origin_id = t["carry_from"]
        origin_date = t.get("date", kst_today())
        for _ in range(30):
            row = conn.execute(
                "SELECT id, carry_from, date FROM tasks WHERE id=?", (origin_id,)
            ).fetchone()
            if not row or not row["carry_from"]:
                origin_date = row["date"] if row else origin_date
                break
            origin_id = row["carry_from"]
            origin_date = row["date"]
        try:
            days = (datetime.now(_KST).date() - datetime.strptime(origin_date, "%Y-%m-%d").date()).days
        except Exception:
            days = 1
        carry_info.append({"task": t, "days": max(1, days)})
    carry_info.sort(key=lambda x: -x["days"])

    # ── [C] 오늘 CC 세션 ─────────────────────────────────────────
    today_cc = []
    try:
        cc_rows = conn.execute(
            "SELECT project, task, status, created_at FROM agent_activity "
            "WHERE date(created_at,'localtime')=? ORDER BY created_at DESC LIMIT 30",
            (kst_today(),),
        ).fetchall()
        seen_p: set[str] = set()
        for row in cc_rows:
            p = (row["project"] or "").strip()
            if p and p not in seen_p:
                seen_p.add(p)
                today_cc.append({"project": p, "task": row["task"] or "", "status": row["status"] or ""})
        today_cc = today_cc[:5]
    except Exception:
        pass

    # ── [D] THE COMMAND ───────────────────────────────────────────
    cmd_task = None
    cmd_reason = ""
    nearly_done = next(
        (
            t for t in todo_tasks
            if 65 <= int(t.get("progress_pct") or 0) < 100
            and resolve_task_remaining_minutes(t) <= 35
        ),
        None,
    )
    if active_tasks:
        cmd_task = active_tasks[0]
        rem = resolve_task_remaining_minutes(cmd_task)
        cmd_reason = f"흐름 유지 중 · 남은 {format_minutes_label(rem)}"
    elif nearly_done:
        cmd_task = nearly_done
        rem = resolve_task_remaining_minutes(cmd_task)
        cmd_reason = f"거의 완료 · {format_minutes_label(rem)} 더하면 닫힘"
    elif focus_task:
        cmd_task = focus_task
        rem = resolve_task_remaining_minutes(cmd_task)
        cmd_reason = f"오늘 최우선 · 남은 {format_minutes_label(rem)}"
    elif todo_tasks:
        cmd_task = todo_tasks[0]
        cmd_reason = "다음 대기 작업"

    command = None
    if cmd_task:
        pkg = build_task_launch_package(cmd_task, cmd_reason)
        command = {
            "task_id": cmd_task.get("id"),
            "title": strip_task_project(cmd_task.get("title", "작업")),
            "project": pkg.get("project", ""),
            "reason": cmd_reason,
            "progress_pct": int(cmd_task.get("progress_pct") or 0),
            "remaining_label": format_minutes_label(resolve_task_remaining_minutes(cmd_task)),
            "can_launch": pkg.get("can_launch", False),
            "launch_prompt": pkg.get("launch_prompt", ""),
            "command_preview": pkg.get("command_preview", ""),
        }

    # ── [E] SEQUENCE ──────────────────────────────────────────────
    cmd_id = cmd_task.get("id") if cmd_task else -1
    sequence = []
    for t in todo_tasks:
        if t.get("id") == cmd_id:
            continue
        if t.get("attention_level") == "critical":
            continue
        rem = resolve_task_remaining_minutes(t)
        sequence.append({
            "task_id": t.get("id"),
            "title": strip_task_project(t.get("title", "작업")),
            "remaining_label": format_minutes_label(rem),
            "progress_pct": int(t.get("progress_pct") or 0),
        })
        if len(sequence) >= 3:
            break

    # ── [F] DECISIONS ─────────────────────────────────────────────
    decisions = []
    seen_dec: set[int] = set()
    for ci in carry_info[:3]:
        t = ci["task"]
        tid = t.get("id")
        if tid in seen_dec:
            continue
        seen_dec.add(tid)
        decisions.append({
            "task_id": tid,
            "title": strip_task_project(t.get("title", "이월 작업")),
            "label": f"{ci['days']}일째 이월",
            "urgency": "high" if ci["days"] >= 3 else "normal",
            "can_launch": bool(t.get("can_open_claude")),
        })
    for t in critical_tasks[:3]:
        tid = t.get("id")
        if tid in seen_dec:
            continue
        seen_dec.add(tid)
        mins = t.get("attention_minutes", 0)
        decisions.append({
            "task_id": tid,
            "title": strip_task_project(t.get("title", "정체 작업")),
            "label": f"{mins}분째 정체",
            "urgency": "high",
            "can_launch": bool(t.get("can_open_claude")),
        })
    decisions = decisions[:4]

    return {
        "headline": headline,
        "objective_title": objective_title,
        "objective_detail": objective_detail,
        "focus_message": focus_message,
        "next_actions": next_actions[:3],
        "recommended_queue": recommended_queue,
        "wins": wins[:3],
        "risks": risks[:3],
        "latest_note": latest_note,
        "latest_note_time": latest_note_time,
        "command": command,
        "sequence": sequence,
        "decisions": decisions,
        "time_status": {
            "current_time": now.strftime("%H:%M"),
            "remaining_work_min": remaining_work_min,
            "remaining_task_min": task_rem,
            "forecast_end": forecast_end,
            "overload": overload,
            "done": stats.get("done", 0),
            "total": stats.get("total", 0),
        },
        "cc_sessions": today_cc,
    }


def normalize_brief_time(value: str) -> str:
    raw = collapse_spaces(value)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise HTTPException(400, "Time must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(400, "Time must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def build_week_goal_summary(conn, target_date: str, tasks: list[dict], brief: dict[str, object]) -> dict[str, object]:
    week_start = get_week_start(target_date)
    week_end = str(datetime.strptime(week_start, "%Y-%m-%d").date() + timedelta(days=6))
    goal_rows = conn.execute(
        "SELECT * FROM weekly_goals WHERE week_start = ? ORDER BY id",
        (week_start,),
    ).fetchall()
    week_tasks = conn.execute(
        "SELECT * FROM tasks WHERE date >= ? AND date <= ? ORDER BY date, priority, id",
        (week_start, week_end),
    ).fetchall()

    goal_items = [dict(row) for row in goal_rows]
    total_goals = len(goal_items)
    done_goals = sum(1 for row in goal_items if int(row.get("done") or 0) == 1)
    goal_progress_pct = round(done_goals / total_goals * 100) if total_goals else 0

    week_total_tasks = 0
    week_done_tasks = 0
    daily: dict[str, dict[str, int]] = {}
    for row in week_tasks:
        item = dict(row)
        date_key = item["date"]
        if date_key not in daily:
            daily[date_key] = {"total": 0, "done": 0}
        if not is_task_parked(item):
            daily[date_key]["total"] += 1
            week_total_tasks += 1
            if item["status"] == "done":
                daily[date_key]["done"] += 1
                week_done_tasks += 1

    task_progress_pct = round(week_done_tasks / week_total_tasks * 100) if week_total_tasks else 0

    recommended_week_goal = ""
    if not goal_items:
        recommended_week_goal = collapse_spaces(
            f"이번 주는 {brief.get('objective_title', '핵심 작업')} 중심으로 3개 목표를 먼저 확정하세요."
        )

    return {
        "week_start": week_start,
        "week_end": week_end,
        "goals": goal_items,
        "done_goals": done_goals,
        "total_goals": total_goals,
        "goal_progress_pct": goal_progress_pct,
        "week_done_tasks": week_done_tasks,
        "week_total_tasks": week_total_tasks,
        "task_progress_pct": task_progress_pct,
        "daily": daily,
        "recommended_week_goal": recommended_week_goal,
    }


def build_today_goal_summary(conn, target_date: str, brief: dict[str, object], weekly_summary: dict[str, object]) -> dict[str, object]:
    recommended_goal = collapse_spaces(
        brief.get("objective_title")
        or brief.get("focus_message")
        or "오늘 가장 중요한 목표를 먼저 정리하세요."
    )
    row = conn.execute(
        "SELECT * FROM daily_goal_plans WHERE date = ?",
        (target_date,),
    ).fetchone()
    if row:
        current = dict(row)
        if not int(current.get("confirmed") or 0) and current.get("recommended_goal") != recommended_goal:
            conn.execute(
                """
                UPDATE daily_goal_plans
                SET recommended_goal = ?, updated_at = CURRENT_TIMESTAMP
                WHERE date = ?
                """,
                (recommended_goal, target_date),
            )
            conn.commit()
            current["recommended_goal"] = recommended_goal
        goal_row = current
    else:
        conn.execute(
            """
            INSERT INTO daily_goal_plans (date, recommended_goal, user_goal, confirmed, updated_at)
            VALUES (?, ?, '', 0, CURRENT_TIMESTAMP)
            """,
            (target_date, recommended_goal),
        )
        conn.commit()
        goal_row = {
            "date": target_date,
            "recommended_goal": recommended_goal,
            "user_goal": "",
            "confirmed": 0,
            "confirmed_at": None,
        }

    confirmed = bool(int(goal_row.get("confirmed") or 0))
    user_goal = collapse_spaces(goal_row.get("user_goal") or "")
    effective_goal = user_goal or recommended_goal
    question = (
        f"추천 목표는 '{recommended_goal}' 입니다. 오늘 이 방향으로 갈까요?"
        if not confirmed
        else f"오늘 확정된 목표는 '{effective_goal}' 입니다."
    )
    weekly_hint = ""
    if weekly_summary.get("total_goals"):
        weekly_hint = (
            f"이번 주 목표 {weekly_summary['done_goals']}/{weekly_summary['total_goals']} 완료"
        )
    elif weekly_summary.get("recommended_week_goal"):
        weekly_hint = weekly_summary["recommended_week_goal"]

    return {
        "date": target_date,
        "recommended_goal": recommended_goal,
        "user_goal": user_goal,
        "effective_goal": effective_goal,
        "confirmed": confirmed,
        "confirmed_at": goal_row.get("confirmed_at"),
        "question": question,
        "weekly_hint": weekly_hint,
    }


def build_morning_board(
    conn,
    target_date: str,
    tasks: list[dict],
    stats: dict[str, object],
    brief: dict[str, object],
) -> dict[str, object]:
    weekly_summary = build_week_goal_summary(conn, target_date, tasks, brief)
    today_goal = build_today_goal_summary(conn, target_date, brief, weekly_summary)
    morning_time = normalize_brief_time(get_setting(conn, "morning_brief_time", "09:00"))
    due_at = None
    minutes_until = None
    is_due = False
    time_status = ""

    if target_date == kst_today():
        due_at = datetime.strptime(f"{target_date} {morning_time}", "%Y-%m-%d %H:%M")
        minutes_until = int((due_at - kst_now_dt()).total_seconds() // 60)
        is_due = minutes_until <= 0
        if minutes_until > 0:
            hours = minutes_until // 60
            mins = minutes_until % 60
            if hours:
                time_status = f"오늘 아침 브리핑까지 {hours}시간 {mins}분"
            else:
                time_status = f"오늘 아침 브리핑까지 {mins}분"
        else:
            elapsed = abs(minutes_until)
            hours = elapsed // 60
            mins = elapsed % 60
            if hours:
                time_status = f"아침 브리핑 시간이 지난 지 {hours}시간 {mins}분"
            else:
                time_status = f"아침 브리핑 시간이 지난 지 {mins}분"
    else:
        due_at = datetime.strptime(f"{target_date} {morning_time}", "%Y-%m-%d %H:%M")
        time_status = f"{target_date} 기준 아침 브리핑 시간 {morning_time}"

    return {
        "time": morning_time,
        "time_status": time_status,
        "due_at": due_at.isoformat() if due_at else "",
        "minutes_until": minutes_until,
        "is_due": is_due,
        "needs_confirmation": not today_goal.get("confirmed"),
        "today_goal": today_goal,
        "weekly": weekly_summary,
        "prompt": (
            "오늘 가장 중요한 목표를 확정하고, 이번 주 목표와 연결되는지 확인하세요."
        ),
    }


def build_day_snapshot(target_date: str) -> dict[str, object]:
    conn = get_db()
    try:
        sync_workspace_projects(conn)
        rows = conn.execute(
            "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id", (target_date,)
        ).fetchall()
        repo_snapshots = collect_repo_snapshots()
        agent_map = get_agent_status_map(conn)
        learning_model = build_learning_model(conn)
        tasks = [enrich_task_for_dashboard(dict(row), repo_snapshots, agent_map, learning_model) for row in rows]
        stats = build_task_stats(tasks)
        brief = build_ops_brief(conn, target_date, tasks, stats)
        morning = build_morning_board(conn, target_date, tasks, stats, brief)
        return {
            "date": target_date,
            "tasks": tasks,
            "stats": stats,
            "brief": brief,
            "morning": morning,
        }
    finally:
        conn.close()


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

        today = datetime.now(_KST).date()
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
    estimated_minutes: int = 15
    priority: int = 2
    date: Optional[str] = None
    # Foundation OS 필드
    stack: Optional[str] = None
    completion_criteria: Optional[str] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    estimated_minutes: Optional[int] = None
    priority: Optional[int] = None
    notes: Optional[str] = None
    # Foundation OS 필드
    stack: Optional[str] = None
    completion_criteria: Optional[str] = None
    output_note: Optional[str] = None
    priority_quadrant: Optional[str] = None


class TaskDecisionRequest(BaseModel):
    action: str
    note: Optional[str] = ""
    split_titles: list[str] = []


class MorningBriefTimeRequest(BaseModel):
    time: str


class TelegramSettingsRequest(BaseModel):
    bot_token: str
    chat_id: str
    enabled: Optional[bool] = True


class TodayGoalUpdate(BaseModel):
    date: Optional[str] = None
    user_goal: str = ""
    use_recommended: bool = False


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
        d = datetime.now(_KST).date()
    elif isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return str(d - timedelta(days=d.weekday()))


init_org_data()


# --- Auth Routes ---
@app.get("/health")
def health():
    return {"status": "ok"}


def _parse_handoff_completed(handoff_dir: str):
    import glob as _glob
    files = sorted(_glob.glob(os.path.join(handoff_dir, "readme-*.md")), reverse=True)
    if not files:
        return {"items": [], "file": ""}
    latest = files[0]
    items = []
    in_completed = False
    with open(latest, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("## Completed"):
                in_completed = True; continue
            if in_completed:
                if line.startswith("##"): break
                if line.startswith("- "): items.append(line[2:].strip())
    return {"items": items[:3], "file": os.path.basename(latest)}

@app.get("/api/last-log")
def get_last_log():
    handoff_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handoff")
    return _parse_handoff_completed(handoff_dir)

@app.get("/api/project-last-log")
def get_project_last_log(proj: str):
    # sanitize: only allow alphanumeric, dash, underscore, dot
    import re
    if not re.match(r'^[\w\-\.]+$', proj):
        return {"items": [], "file": ""}
    handoff_dir = os.path.join(r"C:\work", proj, "handoff")
    if not os.path.isdir(handoff_dir):
        return {"items": [], "file": ""}
    return _parse_handoff_completed(handoff_dir)

@app.get("/login")
def login_page(error: Optional[str] = None):
    return FileResponse("static/login.html")


@app.get("/auth/github")
def auth_github():
    if not GITHUB_CLIENT_ID:
        return HTMLResponse("GITHUB_CLIENT_ID not configured", status_code=500)
    redirect_uri = f"{BASE_URL}/auth/callback"
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=read:user"
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(f"{_BASE_PATH}/login?error=cancelled")
    redirect_uri = f"{BASE_URL}/auth/callback"
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "code": code,
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        token = token_resp.json()
        access_token = token.get("access_token")
        if not access_token:
            return RedirectResponse(f"{_BASE_PATH}/login?error=token_failed")
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        info = user_resp.json()

    login = info.get("login", "")
    if ALLOWED_GITHUB_USERS and login not in ALLOWED_GITHUB_USERS:
        return RedirectResponse(f"{_BASE_PATH}/login?error=forbidden")

    request.session["user"] = {
        "login": login,
        "name": info.get("name") or login,
        "picture": info.get("avatar_url", ""),
    }
    return RedirectResponse(f"{_BASE_PATH}/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(f"{_BASE_PATH}/login")


# --- Routes ---
@app.get("/")
def root():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/me")
def get_me(request: Request):
    user = request.session.get("user", {})
    return {"login": user.get("login", ""), "name": user.get("name", ""), "picture": user.get("picture", "")}


@app.get("/api/tasks/{task_id}/comments")
def get_comments(task_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, task_id, author, body, confirmed, confirmed_by, confirmed_at, created_at FROM task_comments WHERE task_id=? ORDER BY created_at ASC",
        (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class CommentIn(BaseModel):
    body: str


@app.post("/api/tasks/{task_id}/comments")
def post_comment(task_id: int, payload: CommentIn, request: Request):
    user = request.session.get("user", {})
    author = user.get("login") or user.get("name") or "unknown"
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="body required")
    conn = get_db()
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body) VALUES (?,?,?)",
        (task_id, author, body)
    )
    conn.commit()
    row = conn.execute("SELECT id, task_id, author, body, confirmed, confirmed_by, confirmed_at, created_at FROM task_comments WHERE rowid=last_insert_rowid()").fetchone()
    conn.close()
    return dict(row)


@app.patch("/api/tasks/{task_id}/comments/{comment_id}/confirm")
def confirm_comment(task_id: int, comment_id: int, request: Request):
    user = request.session.get("user", {})
    confirmed_by = user.get("login") or user.get("name") or "unknown"
    conn = get_db()
    conn.execute(
        "UPDATE task_comments SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=? AND task_id=?",
        (confirmed_by, comment_id, task_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/projects/{proj}/comments")
def get_project_comments(proj: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, project, author, body, confirmed, confirmed_by, confirmed_at, created_at FROM project_comments WHERE project=? ORDER BY created_at ASC",
        (proj,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects/{proj}/comments")
def post_project_comment(proj: str, payload: CommentIn, request: Request):
    user = request.session.get("user", {})
    author = user.get("login") or user.get("name") or "unknown"
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="body required")
    conn = get_db()
    conn.execute(
        "INSERT INTO project_comments (project, author, body) VALUES (?,?,?)",
        (proj, author, body)
    )
    conn.commit()
    row = conn.execute("SELECT id, project, author, body, confirmed, confirmed_by, confirmed_at, created_at FROM project_comments WHERE rowid=last_insert_rowid()").fetchone()
    conn.close()
    return dict(row)


@app.patch("/api/projects/{proj}/comments/{comment_id}/confirm")
def confirm_project_comment(proj: str, comment_id: int, request: Request):
    user = request.session.get("user", {})
    confirmed_by = user.get("login") or user.get("name") or "unknown"
    conn = get_db()
    conn.execute(
        "UPDATE project_comments SET confirmed=1, confirmed_by=?, confirmed_at=datetime('now') WHERE id=? AND project=?",
        (confirmed_by, comment_id, proj)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/today")
def get_today(d: Optional[str] = None):
    if not d:
        d = kst_today()
    return build_day_snapshot(d)


@app.get("/api/ops-brief")
def get_ops_brief(d: Optional[str] = None):
    if not d:
        d = kst_today()
    snapshot = build_day_snapshot(d)
    return snapshot["brief"]


@app.get("/api/morning-brief")
def get_morning_brief(d: Optional[str] = None):
    if not d:
        d = kst_today()
    snapshot = build_day_snapshot(d)
    return snapshot["morning"]


@app.get("/api/settings/morning-brief-time")
def get_morning_brief_time():
    conn = get_db()
    try:
        value = normalize_brief_time(get_setting(conn, "morning_brief_time", "09:00"))
        return {"time": value}
    finally:
        conn.close()


@app.post("/api/settings/morning-brief-time")
def update_morning_brief_time(payload: MorningBriefTimeRequest):
    value = normalize_brief_time(payload.time)
    conn = get_db()
    try:
        set_setting(conn, "morning_brief_time", value)
        conn.commit()
        return {"time": value}
    finally:
        conn.close()


@app.post("/api/today-goal")
def update_today_goal(payload: TodayGoalUpdate):
    target_date = payload.date or kst_today()
    custom_goal = collapse_spaces(payload.user_goal or "")

    if not payload.use_recommended and not custom_goal:
        raise HTTPException(400, "Goal text is required")

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id",
            (target_date,),
        ).fetchall()
        repo_snapshots = collect_repo_snapshots()
        agent_map = get_agent_status_map(conn)
        learning_model = build_learning_model(conn)
        tasks = [enrich_task_for_dashboard(dict(row), repo_snapshots, agent_map, learning_model) for row in rows]
        stats = build_task_stats(tasks)
        brief = build_ops_brief(conn, target_date, tasks, stats)
        weekly_summary = build_week_goal_summary(conn, target_date, tasks, brief)
        today_goal = build_today_goal_summary(conn, target_date, brief, weekly_summary)

        stored_goal = "" if payload.use_recommended else custom_goal
        confirmed_at = kst_now()
        conn.execute(
            """
            INSERT INTO daily_goal_plans (date, recommended_goal, user_goal, confirmed, confirmed_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                recommended_goal = excluded.recommended_goal,
                user_goal = excluded.user_goal,
                confirmed = 1,
                confirmed_at = excluded.confirmed_at,
                updated_at = excluded.updated_at
            """,
            (
                target_date,
                today_goal["recommended_goal"],
                stored_goal,
                confirmed_at,
                confirmed_at,
            ),
        )
        conn.commit()
        return build_morning_board(conn, target_date, tasks, stats, brief)
    finally:
        conn.close()


@app.post("/api/tasks")
def create_task(task: TaskCreate):
    if not task.date:
        task.date = kst_today()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (date, title, estimated_minutes, priority, updated_at, stack, completion_criteria) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task.date, task.title, task.estimated_minutes, task.priority, kst_now(),
         task.stack or "", task.completion_criteria or ""),
    )
    log_activity(conn, "task_create", {"id": cur.lastrowid, "title": task.title, "date": task.date})
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
        updates["completed_at"] = kst_now()
    elif update.status == "todo":
        updates["completed_at"] = None
    if updates:
        updates["updated_at"] = kst_now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*updates.values(), task_id))
        log_activity(conn, "task_update", {"id": task_id, "changes": {k: v for k, v in updates.items() if k != "updated_at"}})
        conn.commit()
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if updated and updated["status"] == "done":
        record_task_learning_sample(conn, dict(updated))
        conn.commit()
        invalidate_learning_model_cache()
    conn.close()
    return dict(updated)


@app.post("/api/tasks/{task_id}/decision")
def decide_task(task_id: int, decision: TaskDecisionRequest):
    action = (decision.action or "").strip().lower()
    if action not in TASK_DECISION_ACTIONS:
        raise HTTPException(400, "Unsupported action")

    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        raise HTTPException(404, "Not found")

    task_dict = dict(task)
    now = kst_now()
    note = collapse_spaces(decision.note or "")
    payload: dict[str, object] = {}
    created_tasks: list[dict[str, object]] = []

    if action == "continue":
        conn.execute(
            """
            UPDATE tasks
            SET task_state = 'active',
                priority = 1,
                decision_note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (note, now, task_id),
        )
    elif action == "hold":
        conn.execute(
            """
            UPDATE tasks
            SET task_state = 'hold',
                decision_note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (note, now, task_id),
        )
    elif action == "carry":
        next_date = (
            datetime.strptime(task_dict["date"], "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()
        cur = conn.execute(
            """
            INSERT INTO tasks (
                date, title, estimated_minutes, priority, status, task_state,
                updated_at, notes, carry_from, decision_note
            ) VALUES (?, ?, ?, ?, 'todo', 'active', ?, ?, ?, ?)
            """,
            (
                next_date,
                task_dict["title"],
                task_dict["estimated_minutes"],
                task_dict["priority"],
                now,
                task_dict.get("notes"),
                task_id,
                note,
            ),
        )
        created_task = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        created_tasks.append(dict(created_task))
        conn.execute(
            """
            UPDATE tasks
            SET status = 'carried_over',
                decision_note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (note, now, task_id),
        )
        payload["next_date"] = next_date
    elif action == "split":
        project = extract_task_project(task_dict["title"])
        base_title = strip_task_project(task_dict["title"])
        split_titles = [collapse_spaces(title) for title in (decision.split_titles or []) if collapse_spaces(title)]
        if not split_titles:
            split_titles = [
                f"{base_title} - 범위 정리",
                f"{base_title} - 실행",
            ]
        per_minutes = max(5, round(max(10, int(task_dict["estimated_minutes"] or 20)) / max(len(split_titles), 1)))
        for raw_title in split_titles:
            full_title = f"[{project}] {raw_title}" if project else raw_title
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    date, title, estimated_minutes, priority, status, task_state,
                    updated_at, notes, carry_from, decision_note
                ) VALUES (?, ?, ?, ?, 'todo', 'active', ?, ?, ?, ?)
                """,
                (
                    task_dict["date"],
                    full_title,
                    per_minutes,
                    task_dict["priority"],
                    now,
                    task_dict.get("notes"),
                    task_id,
                    note,
                ),
            )
            created_task = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
            created_tasks.append(dict(created_task))
        conn.execute(
            """
            UPDATE tasks
            SET task_state = 'split',
                decision_note = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (note, now, task_id),
        )
        payload["created_count"] = len(created_tasks)

    payload["created_tasks"] = [task["id"] for task in created_tasks]
    conn.execute(
        """
        INSERT INTO task_decisions (task_id, action, note, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, action, note, json.dumps(payload, ensure_ascii=False), now),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return {
        "ok": True,
        "task": dict(updated) if updated else None,
        "created_tasks": created_tasks,
        "action": action,
    }


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_db()
    task = conn.execute("SELECT title, date FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    log_activity(conn, "task_delete", {"id": task_id, "title": task["title"] if task else "", "date": task["date"] if task else ""})
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/calendar")
def get_calendar_activity(year: int = 0, month: int = 0):
    """달력용 월별 활동 데이터 — done 작업 + CC 활동을 날짜별로 반환."""
    today = datetime.now(_KST).date()
    if not year:
        year = today.year
    if not month:
        month = today.month
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    month_start = f"{year}-{month:02d}-01"
    month_end   = f"{year}-{month:02d}-{last_day:02d}"

    conn = get_db()
    try:
        # done 작업 집계
        done_rows = conn.execute(
            "SELECT date, COUNT(*) as cnt FROM tasks "
            "WHERE date >= ? AND date <= ? AND status = 'done' GROUP BY date",
            (month_start, month_end),
        ).fetchall()
        done_map = {r["date"]: r["cnt"] for r in done_rows}

        # CC 활동 집계
        cc_rows = conn.execute(
            "SELECT date(created_at) as day, COUNT(*) as cnt FROM agent_activity "
            "WHERE date(created_at) >= ? AND date(created_at) <= ? GROUP BY day",
            (month_start, month_end),
        ).fetchall()
        cc_map = {r["day"]: r["cnt"] for r in cc_rows}

        # 합산
        all_days = set(done_map) | set(cc_map)
        daily = {
            d: {"done": done_map.get(d, 0), "cc": cc_map.get(d, 0)}
            for d in all_days
        }
        return {"year": year, "month": month, "daily": daily, "server_today": str(today)}
    finally:
        conn.close()


@app.get("/api/week")
def get_week(week_start: Optional[str] = None):
    if not week_start:
        week_start = get_week_start()
    week_end = str(datetime.strptime(week_start, "%Y-%m-%d").date() + timedelta(days=6))
    conn = get_db()
    sync_workspace_projects(conn)
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
    return {"week_start": week_start, "goals": [dict(g) for g in goals], "daily": daily, "server_today": kst_today()}


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
    today = kst_today()
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
        d = kst_today()
    conn = get_db()
    checkins = build_checkin_feed(conn, d)
    conn.close()
    return checkins


@app.get("/api/yesterday-undone")
def yesterday_undone():
    yesterday = str(datetime.now(_KST).date() - timedelta(days=1))
    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over') AND COALESCE(task_state, 'active') = 'active'",
        (yesterday,),
    ).fetchall()
    conn.close()
    return [dict(t) for t in tasks]


@app.post("/api/carry-over")
def carry_over():
    today = kst_today()
    yesterday = str(datetime.now(_KST).date() - timedelta(days=1))
    conn = get_db()
    undone = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over') AND COALESCE(task_state, 'active') = 'active'",
        (yesterday,),
    ).fetchall()
    count = 0
    for task in undone:
        cur = conn.execute(
            "INSERT INTO tasks (date, title, estimated_minutes, priority, carry_from) VALUES (?, ?, ?, ?, ?)",
            (today, task["title"], task["estimated_minutes"], task["priority"], task["id"]),
        )
        conn.execute("UPDATE tasks SET status = 'carried_over' WHERE id = ?", (task["id"],))
        log_activity(conn, "task_carry_over", {"new_id": cur.lastrowid, "from_id": task["id"], "title": task["title"], "date": today})
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
        logs_path = r"C:\work\squad-team\logs"

    today = kst_today()
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
                        (kst_now(), existing[title]["id"]),
                    )
                    existing[title]["status"] = "done"
                    results["updated"].append(title)
                else:
                    results["skipped"].append(title)
            else:
                # 신규 추가
                status = "done" if is_done else "todo"
                completed_at = kst_now() if is_done else None
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
    today = kst_today()
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
                        (kst_now(), existing[title]["id"]),
                    )
                    existing[title]["status"] = "done"
                    results["updated"].append(title)
                else:
                    results["skipped"].append(title)
            else:
                status = "done" if is_done else "todo"
                completed_at = kst_now() if is_done else None
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
    SCAN_ROOTS = REPO_SCAN_ROOTS
    MAX_DEPTH = 3
    today = kst_today()
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
        "SELECT id, title, status FROM tasks WHERE date = ? AND status IN ('todo', 'carried_over')",
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
                        (kst_now(), task_id),
                    )
                    updated.append(title)

    conn.commit()
    conn.close()
    return {"updated": updated, "count": len(updated), "scanned": scanned}


WIDGET_VBS = str(Path(__file__).parent / "widget" / "widget-start-silent.vbs")

# ═══════════════════════════════════════════════════════════════════
# 구과장님 요청 — Requests API
# ═══════════════════════════════════════════════════════════════════

class RequestCreate(BaseModel):
    title: str
    body: str = ""
    project_name: str = ""
    description: str = ""
    priority: int = 2

class RequestUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    project_name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None


@app.get("/api/requests")
def list_requests():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.*, COUNT(a.id) as attach_count FROM requests r "
            "LEFT JOIN request_attachments a ON a.request_id = r.id "
            "GROUP BY r.id ORDER BY r.created_at DESC"
        ).fetchall()
        return {"requests": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/requests")
async def create_request(payload: RequestCreate, http_req: Request):
    user = http_req.session.get("user", {})
    author = user.get("login", "unknown") if isinstance(user, dict) else str(user)
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO requests (title, body, project_name, description, priority, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (payload.title.strip(), payload.body, payload.project_name.strip(), payload.description.strip(), payload.priority, author),
        )
        req_id = cur.lastrowid
        # 알림 자동 생성
        conn.execute(
            "INSERT INTO notifications (type, title, body, ref_type, ref_id) VALUES (?,?,?,?,?)",
            ("request", f"새 요청: {payload.title}", f"작성자: {author}", "request", req_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.patch("/api/requests/{req_id}")
def update_request(req_id: int, payload: RequestUpdate, http_req: Request):
    user = http_req.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    conn = get_db()
    try:
        row = conn.execute("SELECT created_by FROM requests WHERE id=?", (req_id,)).fetchone()
        if not row:
            raise HTTPException(404, "요청을 찾을 수 없습니다")
        # 소유자 또는 관리자(ALLOWED_GITHUB_USERS 첫 번째)만 수정 가능
        admins = list(ALLOWED_GITHUB_USERS)[:1]
        if row["created_by"] and login and row["created_by"] != login and login not in admins:
            raise HTTPException(403, "본인이 작성한 요청만 수정할 수 있습니다")
        updates = {k: v for k, v in payload.dict().items() if v is not None}
        if not updates:
            raise HTTPException(400, "변경사항 없음")
        updates["updated_at"] = kst_now()
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE requests SET {sets} WHERE id=?", (*updates.values(), req_id))
        conn.commit()
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.delete("/api/requests/{req_id}")
def delete_request(req_id: int, http_req: Request):
    user = http_req.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    conn = get_db()
    try:
        row = conn.execute("SELECT created_by FROM requests WHERE id=?", (req_id,)).fetchone()
        if not row:
            raise HTTPException(404, "요청을 찾을 수 없습니다")
        admins = list(ALLOWED_GITHUB_USERS)[:1]
        if row["created_by"] and login and row["created_by"] != login and login not in admins:
            raise HTTPException(403, "본인이 작성한 요청만 삭제할 수 있습니다")
        # 첨부파일 물리 삭제
        atts = conn.execute("SELECT filename FROM request_attachments WHERE request_id=?", (req_id,)).fetchall()
        for a in atts:
            fp = Path(f"data/uploads/{req_id}/{a['filename']}")
            if fp.exists():
                fp.unlink()
        conn.execute("DELETE FROM request_attachments WHERE request_id=?", (req_id,))
        conn.execute("DELETE FROM requests WHERE id=?", (req_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/requests/{req_id}/attachments")
async def upload_attachment(req_id: int, file: UploadFile = File(...)):
    upload_dir = Path(f"data/uploads/{req_id}")
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 안전한 파일명 생성 (타임스탬프 prefix)
    safe_name = f"{int(datetime.now().timestamp())}_{file.filename.replace('/', '_').replace('..', '_')}"
    dest = upload_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO request_attachments (request_id, filename, original_name, file_size) VALUES (?,?,?,?)",
            (req_id, safe_name, file.filename, len(content)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM request_attachments WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.get("/api/requests/{req_id}/attachments")
def list_attachments(req_id: int):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM request_attachments WHERE request_id=? ORDER BY created_at DESC", (req_id,)
        ).fetchall()
        return {"attachments": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.delete("/api/requests/{req_id}/attachments/{att_id}")
def delete_attachment(req_id: int, att_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM request_attachments WHERE id=? AND request_id=?", (att_id, req_id)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        fp = Path(f"data/uploads/{req_id}/{row['filename']}")
        if fp.exists():
            fp.unlink()
        conn.execute("DELETE FROM request_attachments WHERE id=?", (att_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/requests/{req_id}/attachments/{att_id}/download")
def download_attachment(req_id: int, att_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM request_attachments WHERE id=? AND request_id=?", (att_id, req_id)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        fp = Path(f"data/uploads/{req_id}/{row['filename']}")
        if not fp.exists():
            raise HTTPException(404, "파일 없음")
        return FileResponse(str(fp), filename=row["original_name"])
    finally:
        conn.close()


# ── Replies ──────────────────────────────────────────────────────────

class ReplyCreate(BaseModel):
    body: str

@app.get("/api/requests/{req_id}/replies")
def list_replies(req_id: int):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM request_replies WHERE request_id=? ORDER BY created_at ASC",
            (req_id,),
        ).fetchall()
        return {"replies": [dict(r) for r in rows]}
    finally:
        conn.close()

@app.post("/api/requests/{req_id}/replies")
def create_reply(req_id: int, payload: ReplyCreate, http_req: Request):
    user = http_req.session.get("user", {})
    author = user.get("login", "unknown") if isinstance(user, dict) else str(user)
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "내용 없음")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO request_replies (request_id, body, created_by) VALUES (?,?,?)",
            (req_id, body, author),
        )
        conn.execute(
            "UPDATE requests SET updated_at=? WHERE id=?",
            (kst_now(), req_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM request_replies WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()

@app.delete("/api/requests/{req_id}/replies/{reply_id}")
def delete_reply(req_id: int, reply_id: int, http_req: Request):
    user = http_req.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    conn = get_db()
    try:
        row = conn.execute("SELECT created_by FROM request_replies WHERE id=? AND request_id=?", (reply_id, req_id)).fetchone()
        if not row:
            raise HTTPException(404, "댓글을 찾을 수 없습니다")
        admins = list(ALLOWED_GITHUB_USERS)[:1]
        if row["created_by"] and login and row["created_by"] != login and login not in admins:
            raise HTTPException(403, "본인이 작성한 댓글만 삭제할 수 있습니다")
        conn.execute("DELETE FROM request_replies WHERE id=? AND request_id=?", (reply_id, req_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── 이미지 업로드 (클립보드 붙여넣기용) ──────────────────────────────

@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    allowed = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(400, "이미지 파일만 업로드 가능합니다")
    img_dir = Path("data/uploads/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    safe_name = f"{int(datetime.now().timestamp()*1000)}.{ext}"
    dest = img_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    return {"url": f"/uploads/images/{safe_name}"}


# ── Date Alarms API ─────────────────────────────────────────────────

class AlarmCreate(BaseModel):
    target_date: str
    message: str

@app.get("/api/alarms")
def list_alarms(request: Request, date: Optional[str] = None):
    user = request.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    conn = get_db()
    if date:
        rows = conn.execute(
            "SELECT * FROM date_alarms WHERE target_date = ? AND (created_by = ? OR created_by = '') ORDER BY created_at",
            (date, login)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM date_alarms WHERE created_by = ? OR created_by = '' ORDER BY target_date, created_at",
            (login,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/alarms/today")
def list_today_alarms(request: Request):
    user = request.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    today = kst_today()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM date_alarms WHERE target_date = ? AND acknowledged_at IS NULL AND (created_by = ? OR created_by = '') ORDER BY created_at",
        (today, login)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/alarms")
def create_alarm(data: AlarmCreate, request: Request):
    user = request.session.get("user", {})
    login = user.get("login", "") if isinstance(user, dict) else ""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO date_alarms (target_date, message, created_by) VALUES (?, ?, ?)",
        (data.target_date, data.message, login)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM date_alarms WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

@app.patch("/api/alarms/{alarm_id}/ack")
def ack_alarm(alarm_id: int, request: Request):
    conn = get_db()
    row = conn.execute("SELECT * FROM date_alarms WHERE id = ?", (alarm_id,)).fetchone()
    if not row:
        raise HTTPException(404, "알람을 찾을 수 없습니다")
    now = kst_now()
    conn.execute(
        "UPDATE date_alarms SET acknowledged_at = ? WHERE id = ?",
        (now, alarm_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM date_alarms WHERE id = ?", (alarm_id,)).fetchone()
    conn.close()
    return dict(row)

@app.delete("/api/alarms/{alarm_id}")
def delete_alarm(alarm_id: int, request: Request):
    conn = get_db()
    conn.execute("DELETE FROM date_alarms WHERE id = ?", (alarm_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── /requests 페이지 ─────────────────────────────────────────────────

@app.get("/requests")
def requests_page():
    return FileResponse(
        "static/requests.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ═══════════════════════════════════════════════════════════════════
# 알림 API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/notifications")
def get_notifications(limit: int = 50):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
        return {"notifications": [dict(r) for r in rows], "unread": unread}
    finally:
        conn.close()


@app.post("/api/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notif_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/notifications/read-all")
def mark_all_notifications_read():
    conn = get_db()
    try:
        conn.execute("UPDATE notifications SET is_read=1")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/launch-widget")
def launch_widget():
    """Electron 위젯 실행 또는 이미 실행 중이면 창 앞으로 꺼내기."""
    # 1. 이미 실행 중인지 확인 (electron.exe 중 daily-focus-widget 포함)
    activate_ps = r"""
$w = Get-Process -Name electron -ErrorAction SilentlyContinue |
     Where-Object { $_.MainWindowHandle -ne 0 } |
     Select-Object -First 1
if ($w) {
    $code = '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);'
    Add-Type -MemberDefinition $code -Name W32e -Namespace U32e -ErrorAction SilentlyContinue
    [U32e.W32e]::ShowWindow($w.MainWindowHandle, 9) | Out-Null
    [U32e.W32e]::SetForegroundWindow($w.MainWindowHandle) | Out-Null
    Write-Output "activated"
} else {
    Write-Output "notfound"
}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", activate_ps],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.stdout.strip() == "activated":
            return {"ok": True, "method": "activated"}
    except Exception:
        pass

    # 2. VBS로 무소음 실행
    if not os.path.exists(WIDGET_VBS):
        return {"ok": False, "error": f"widget-start-silent.vbs 없음: {WIDGET_VBS}"}
    try:
        subprocess.Popen(
            ["wscript.exe", WIDGET_VBS],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return {"ok": True, "method": "launched"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def _is_claude_active(proj: str, cutoff_minutes: int = 10) -> bool:
    """~/.claude/projects/ JSONL 수정 시간으로 활성 CC 세션 감지.
    proj 이름이 폴더명에 포함(C--work-daily-focus 등)되고
    cutoff_minutes 이내에 수정된 파일이 있으면 True.
    """
    import time
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return False
    cutoff = time.time() - cutoff_minutes * 60
    # 프로젝트명 정규화: daily-focus → daily-focus (폴더명: C--work-daily-focus)
    proj_norm = proj.lower().replace("_", "-")
    for folder in claude_dir.iterdir():
        if not folder.is_dir():
            continue
        if proj_norm not in folder.name.lower():
            continue
        for jsonl in folder.glob("*.jsonl"):
            if jsonl.stat().st_mtime > cutoff:
                return True
    return False


@app.post("/api/open-claude")
def open_claude(proj: str, prompt: str = ""):
    """프로젝트명으로 레포를 찾아 Claude Code 터미널 열기.
    - 활성 CC 세션 감지: ~/.claude/projects/ JSONL 수정시간 (최근 10분)
    - 활성 중 → WindowsTerminal 창 앞으로 꺼내기
    - 비활성 → 새 cmd 창에서 해당 폴더 이동 후 claude 실행
    """
    repo = find_repo_for_project(proj)
    if not repo or not repo.get("path"):
        return {"ok": False, "error": f"경로를 찾을 수 없음: {proj}"}
    target_path = repo["path"]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # ── 1. 활성 세션 감지 (JSONL 기반) ──────────────────────────────
    if _is_claude_active(proj):
        # Windows Terminal 또는 타이틀이 있는 cmd 창 앞으로 꺼내기
        activate_ps = r"""
$code = '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);'
Add-Type -MemberDefinition $code -Name W32 -Namespace U32 -ErrorAction SilentlyContinue
# WindowsTerminal 우선, 없으면 cmd
$w = Get-Process -Name WindowsTerminal -ErrorAction SilentlyContinue |
     Sort-Object StartTime -Descending | Select-Object -First 1
if (-not $w) {
    $w = Get-Process -Name cmd -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
}
if ($w -and $w.MainWindowHandle -ne 0) {
    [U32.W32]::ShowWindow($w.MainWindowHandle, 9) | Out-Null
    [U32.W32]::SetForegroundWindow($w.MainWindowHandle) | Out-Null
    Write-Output "activated"
} else {
    Write-Output "notfound"
}
"""
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", activate_ps],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.stdout.strip() == "activated":
                return {"ok": True, "path": target_path, "method": "activated"}
        except Exception:
            pass

    # ── 2. 새 cmd 창 열기: 해당 폴더 + claude 실행 ────────────────
    try:
        subprocess.Popen(
            ["cmd", "/k", f"title CC:{proj} & claude"],
            cwd=target_path,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as ex:
        return {"ok": False, "error": f"실행 실패: {str(ex)}"}

    return {"ok": True, "path": target_path, "method": "cmd_new"}


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
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_dir):
        return {"error": "~/.claude/projects 없음", "total": {}, "by_project": {}}

    today_prefix  = kst_today()          # "2026-03-07"
    month_prefix  = datetime.now(_KST).strftime("%Y-%m")    # "2026-03"

    # 한도 조회
    conn_s = get_db()
    row_limit   = conn_s.execute("SELECT value FROM settings WHERE key='token_limit'").fetchone()
    row_sess    = conn_s.execute("SELECT value FROM settings WHERE key='session_token_limit'").fetchone()
    row_week    = conn_s.execute("SELECT value FROM settings WHERE key='weekly_token_limit'").fetchone()
    row_sonnet  = conn_s.execute("SELECT value FROM settings WHERE key='weekly_sonnet_limit'").fetchone()
    conn_s.close()
    token_limit          = int(row_limit["value"])  if row_limit   else 2_000_000_000
    session_token_limit  = int(row_sess["value"])   if row_sess    else 450_000_000
    weekly_token_limit   = int(row_week["value"])   if row_week    else 3_000_000_000
    weekly_sonnet_limit  = int(row_sonnet["value"]) if row_sonnet  else 2_120_000_000

    now_utc        = _dt.now(_tz.utc)
    window_5h_start = now_utc - _td(hours=5)
    window_7d_start = now_utc - _td(days=7)

    today_total   = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    month_total   = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    session_total = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    weekly_total  = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    weekly_sonnet = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    by_project: dict = {}

    # Track oldest timestamps in each window (for reset time calculation)
    session_oldest_ts: list = []  # mutable container
    weekly_oldest_ts:  list = []

    def scan_jsonl(filepath, project_name):
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = _json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp", "")
                    if not ts:
                        continue
                    msg = d.get("message", {})
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage", {})
                    if not usage:
                        continue

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cc  = usage.get("cache_creation_input_tokens", 0)
                    cr  = usage.get("cache_read_input_tokens", 0)

                    # 월간 누적
                    if ts.startswith(month_prefix):
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

                    # 시간대별 윈도우 집계
                    try:
                        ts_utc = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        continue

                    if ts_utc >= window_7d_start:
                        weekly_total["input"]        += inp
                        weekly_total["output"]       += out
                        weekly_total["cache_create"] += cc
                        weekly_total["cache_read"]   += cr
                        if not weekly_oldest_ts or ts_utc < weekly_oldest_ts[0]:
                            if weekly_oldest_ts:
                                weekly_oldest_ts[0] = ts_utc
                            else:
                                weekly_oldest_ts.append(ts_utc)
                        model = msg.get("model", "")
                        if "sonnet" in model.lower():
                            weekly_sonnet["input"]        += inp
                            weekly_sonnet["output"]       += out
                            weekly_sonnet["cache_create"] += cc
                            weekly_sonnet["cache_read"]   += cr

                    if ts_utc >= window_5h_start:
                        session_total["input"]        += inp
                        session_total["output"]       += out
                        session_total["cache_create"] += cc
                        session_total["cache_read"]   += cr
                        if not session_oldest_ts or ts_utc < session_oldest_ts[0]:
                            if session_oldest_ts:
                                session_oldest_ts[0] = ts_utc
                            else:
                                session_oldest_ts.append(ts_utc)
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

    month_tokens  = sum(month_total.values())
    today_tokens  = sum(today_total.values())
    session_toks  = sum(session_total.values())
    weekly_toks   = sum(weekly_total.values())
    sonnet_toks   = sum(weekly_sonnet.values())
    used_pct      = round(month_tokens  / token_limit         * 100, 1) if token_limit         else 0
    session_pct   = round(session_toks  / session_token_limit * 100, 1) if session_token_limit else 0
    weekly_pct    = round(weekly_toks   / weekly_token_limit  * 100, 1) if weekly_token_limit  else 0
    sonnet_pct    = round(sonnet_toks   / weekly_sonnet_limit * 100, 1) if weekly_sonnet_limit else 0

    # Reset time: how many minutes until oldest token in window rolls off
    def reset_min(oldest_list, window_td):
        if not oldest_list:
            return None
        roll_off = oldest_list[0] + window_td
        delta = int((roll_off - now_utc).total_seconds() / 60)
        return max(0, delta)

    session_reset = reset_min(session_oldest_ts, _td(hours=5))
    weekly_reset  = reset_min(weekly_oldest_ts,  _td(days=7))

    sorted_projects = sorted(
        [{"name": k, **v} for k, v in by_project.items()],
        key=lambda x: -(x["input"] + x["output"] + x["cache_create"] + x["cache_read"])
    )

    result = {
        "month_tokens":  month_tokens,
        "today_tokens":  today_tokens,
        "month_total":   month_total,
        "today_total":   today_total,
        "token_limit":   token_limit,
        "used_pct":      used_pct,
        "remain_pct":    round(100 - used_pct, 1),
        # 세션/주간 지표
        "session_tokens":         session_toks,
        "session_pct":            session_pct,
        "session_token_limit":    session_token_limit,
        "session_reset_min":      session_reset,
        "weekly_tokens":          weekly_toks,
        "weekly_pct":             weekly_pct,
        "weekly_token_limit":     weekly_token_limit,
        "weekly_reset_min":       weekly_reset,
        "weekly_sonnet_tokens":   sonnet_toks,
        "weekly_sonnet_pct":      sonnet_pct,
        "weekly_sonnet_limit":    weekly_sonnet_limit,
        "by_project":   sorted_projects,
        "month_label":  datetime.now(_KST).strftime("%Y년 %m월"),
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
                "by_project": [], "month_label": datetime.now(_KST).strftime("%Y년 %m월")}
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
    overdue = [task for task in open_tasks if task["due_date"] < kst_today()]
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
    today = kst_today()
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
    due_date = task.due_date or kst_today()
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
        updates["completed_at"] = kst_now()
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
    log_date = payload.log_date or kst_today()
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
        (review_status, payload.review_note, payload.actor_id, kst_now(), worklog_id),
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
    project = summarize_agent_text(repair_mojibake_text(report.project), limit=160)
    status = summarize_agent_text(clean_utf8_text(report.status).lower(), limit=40) or "step"
    task = normalize_agent_status_task(report.task, status=status, project=project, limit=240)
    normalized_url = summarize_agent_text(clean_utf8_text(report.url), limit=500)
    existing = conn.execute(
        "SELECT task, status, url FROM agent_status WHERE project = ?",
        (project,),
    ).fetchone()
    changed = (
        existing is None
        or existing["task"] != task
        or existing["status"] != status
        or (existing["url"] or "") != normalized_url
    )
    conn.execute(
        """INSERT INTO agent_status (project, task, status, url, updated_at)
           VALUES (?, ?, ?, ?, datetime('now','localtime'))
           ON CONFLICT(project) DO UPDATE SET
              task=excluded.task, status=excluded.status, url=excluded.url, updated_at=excluded.updated_at""",
        (project, task, status, normalized_url)
    )
    if changed:
        conn.execute(
            """INSERT INTO agent_activity (project, task, status, url, created_at)
               VALUES (?, ?, ?, ?, datetime('now','localtime'))""",
            (project, task, status, normalized_url),
        )

    # ── 오늘 태스크 자동 동기화 ──────────────────────────────────────
    today = kst_today()
    proj_key = normalize_project_key(project)

    # 오늘 날짜 태스크 중 같은 프로젝트 항목 검색 (수동 + 자동 모두)
    existing_task = conn.execute(
        """SELECT id, notes, status FROM tasks
           WHERE date = ? AND lower(title) LIKE ?
           ORDER BY id DESC LIMIT 1""",
        (today, f"%[{project.lower()}]%"),
    ).fetchone()

    if status == "done":
        # 자동 생성 태스크만 완료 처리 (수동 태스크는 건드리지 않음)
        if existing_task and existing_task["notes"] == "auto" and existing_task["status"] != "done":
            conn.execute(
                """UPDATE tasks SET status='done', completed_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime') WHERE id=?""",
                (existing_task["id"],),
            )
    elif status in ("start", "step"):
        if existing_task:
            # 자동 생성 태스크면 task 내용 최신화
            if existing_task["notes"] == "auto":
                short = task[:60] if task else "작업 중"
                new_title = f"[{project}] 이어서: {short}"
                conn.execute(
                    "UPDATE tasks SET title=?, updated_at=datetime('now','localtime') WHERE id=?",
                    (new_title, existing_task["id"]),
                )
        else:
            # 오늘 해당 프로젝트 태스크 없음 → 자동 생성
            short = task[:60] if task else "작업 중"
            auto_title = f"[{project}] 이어서: {short}"
            conn.execute(
                """INSERT INTO tasks (date, title, estimated_minutes, priority, status, notes, updated_at)
                   VALUES (?, ?, 30, 2, 'todo', 'auto', datetime('now','localtime'))""",
                (today, auto_title),
            )
    # ─────────────────────────────────────────────────────────────────

    conn.commit()
    conn.close()
    return {"ok": True}


@app.patch("/api/agent-status/url")
def patch_agent_url(body: dict):
    project = (body.get("project") or "").strip()
    url     = (body.get("url") or "").strip()
    if not project:
        raise HTTPException(status_code=400, detail="project required")
    conn = get_db()
    ensure_table_column(conn, "agent_status", "url", "TEXT DEFAULT ''")
    existing = conn.execute("SELECT project FROM agent_status WHERE project = ?", (project,)).fetchone()
    if existing:
        conn.execute("UPDATE agent_status SET url = ? WHERE project = ?", (url, project))
    else:
        conn.execute(
            "INSERT INTO agent_status (project, task, status, url, updated_at) VALUES (?, '', 'step', ?, datetime('now','localtime'))",
            (project, url)
        )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/agent-status")
def get_agent_status():
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    conn = get_db()
    ensure_table_column(conn, "agent_status", "url", "TEXT DEFAULT ''")
    rows = conn.execute(
        """SELECT project, task, status, url, updated_at,
                  CAST((julianday('now','localtime') - julianday(updated_at)) * 1440 AS INTEGER) AS minutes_ago
           FROM agent_status
           ORDER BY updated_at DESC"""
    ).fetchall()
    conn.close()

    # DB에 있는 프로젝트명 목록
    db_projects = {r["project"] for r in rows}
    # 정규화된 이름으로도 매칭 (06_koo ↔ 06-koo 중복 방지)
    db_normalized = {_re.sub(r'[-_]', '', p).lower() for p in db_projects}
    result = [dict(r) for r in rows]

    # 6시간 이상 업데이트 없는 항목은 status를 idle로 표시 (DB 변경 없이 응답만)
    for r in result:
        if r.get('minutes_ago', 0) > 360:
            r['status'] = 'idle'

    # JSONL 파일 기반으로 최근 24h 내 활동한 프로젝트 보완 (hook 미보고 세션 포함)
    # C:/work에 실제 존재하는 폴더만 대상으로 함 (삭제된 폴더 제외)
    try:
        projects_dir = os.path.expanduser("~/.claude/projects")
        work_folders = set(os.listdir("C:/work"))
        work_folders_norm = {_re.sub(r'[-_]', '', f).lower(): f for f in work_folders}
        cutoff = _dt.now() - _td(hours=24)
        live_cutoff = _dt.now() - _td(minutes=10)  # 10분 이내 = is_live

        def _clean_proj(d: str) -> str:
            cleaned = _re.sub(r'^[A-Z]-+', '', d)
            for pfx in ['01-work-hive-media-', '01-work-my-project-',
                        '01-work--agents-', '01-work-agents-', '01-work-', 'work-']:
                if cleaned.startswith(pfx):
                    cleaned = cleaned[len(pfx):]
                    break
            return cleaned if cleaned and cleaned != 'Users-USER' else ''

        for proj_dir in os.listdir(projects_dir):
            proj_path = os.path.join(projects_dir, proj_dir)
            if not os.path.isdir(proj_path):
                continue
            pname = _clean_proj(proj_dir)
            if not pname:
                continue
            pname_norm = _re.sub(r'[-_]', '', pname).lower()
            if pname not in work_folders and pname_norm not in work_folders_norm:
                continue
            if pname not in work_folders and pname_norm in work_folders_norm:
                pname = work_folders_norm[pname_norm]
            # Find most recently modified JSONL
            latest_mtime = None
            for root, _, files in os.walk(proj_path):
                for fname in files:
                    if fname.endswith(".jsonl"):
                        fpath = os.path.join(root, fname)
                        try:
                            mtime = _dt.fromtimestamp(os.path.getmtime(fpath))
                            if mtime >= cutoff and (latest_mtime is None or mtime > latest_mtime):
                                latest_mtime = mtime
                        except OSError:
                            pass
            if not latest_mtime:
                continue
            minutes_ago = int((_dt.now() - latest_mtime).total_seconds() / 60)
            is_live = latest_mtime >= live_cutoff
            if pname in db_projects or pname_norm in db_normalized:
                for i, r in enumerate(result):
                    r_norm = _re.sub(r'[-_]', '', r['project']).lower()
                    if r['project'] == pname or r_norm == pname_norm:
                        # is_live 항상 갱신, minutes_ago는 더 최신인 경우만 교체
                        result[i]['is_live'] = is_live
                        if r.get('minutes_ago', 9999) > minutes_ago + 5:
                            result[i].update({
                                "updated_at": latest_mtime.strftime("%Y-%m-%d %H:%M"),
                                "minutes_ago": minutes_ago,
                                "from_jsonl": True,
                            })
                        break
                continue
            result.append({
                "project": pname,
                "task": "JSONL activity detected",
                "status": "step",
                "url": "",
                "updated_at": latest_mtime.strftime("%Y-%m-%d %H:%M"),
                "minutes_ago": minutes_ago,
                "is_live": is_live,
                "from_jsonl": True,
            })
    except Exception:
        pass

    # C:/work 폴더 직접 스캔 — DB/JSONL에 없는 폴더도 전부 표시
    try:
        work_dir = "C:/work"
        already = {_re.sub(r'[-_]', '', r['project']).lower() for r in result}
        skip = {'log', 'setting', 'config', 'data', 'tools'}
        for entry in os.listdir(work_dir):
            if entry.upper() == "CLAUDE.MD":
                continue
            full = os.path.join(work_dir, entry)
            if not os.path.isdir(full):
                continue
            if entry.lower() in skip:
                continue
            entry_norm = _re.sub(r'[-_]', '', entry).lower()
            if entry_norm in already:
                continue
            result.append({
                "project": entry,
                "task": "",
                "status": "idle",
                "url": "",
                "updated_at": "",
                "minutes_ago": 999999,
                "from_work_scan": True,
            })
    except Exception:
        pass

    result.sort(key=lambda x: x.get("minutes_ago", 99999))

    # ── 프로젝트별 최근 보고 이력 3개 주입 ──────────────────────────
    try:
        conn2 = get_db()
        # 한 번에 전체 이력 가져와서 Python에서 프로젝트별 분류
        activity_rows = conn2.execute(
            """SELECT project, task, status, created_at
               FROM agent_activity
               WHERE task != '' AND task IS NOT NULL
               ORDER BY created_at DESC
               LIMIT 500"""
        ).fetchall()
        conn2.close()
        # project -> [last 3 entries]
        _act_map: dict = {}
        for row in activity_rows:
            p = row["project"]
            if p not in _act_map:
                _act_map[p] = []
            if len(_act_map[p]) < 3:
                _act_map[p].append({
                    "task": row["task"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                })
        for r in result:
            r["recent_activity"] = _act_map.get(r["project"], [])
    except Exception:
        for r in result:
            if "recent_activity" not in r:
                r["recent_activity"] = []

    return result


# ── Strategic Brief API ─────────────────────────────────────────────────────

@app.get("/api/strategic-brief")
async def get_strategic_brief():
    """전략적 비서: 어제/오늘/지금 할 것 브리핑"""
    today = kst_today()
    yesterday = str(datetime.now(_KST).date() - timedelta(days=1))
    now = kst_now_dt()

    conn = get_db()
    try:
        # ── 어제 데이터 ──
        yd_rows = conn.execute(
            "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id", (yesterday,)
        ).fetchall()
        yd_tasks = [dict(row) for row in yd_rows]
        yd_done = [t for t in yd_tasks if t.get("status") == "done"]
        yd_carry = [t for t in yd_tasks if t.get("status") not in ("done", "carried_over")]

        # 어제 CC 세션 (agent_activity)
        cc_rows = conn.execute(
            "SELECT project, task, status, created_at FROM agent_activity "
            "WHERE date(created_at) = ? ORDER BY created_at DESC LIMIT 20",
            (yesterday,),
        ).fetchall()
        cc_sessions = [dict(row) for row in cc_rows]

        yesterday_block = {
            "date": yesterday,
            "completed_tasks": [
                {
                    "title": strip_task_project(t.get("title", "")),
                    "project": (t.get("title", "").split("]")[0].lstrip("[") if "[" in t.get("title", "") else ""),
                    "minutes": t.get("estimated_minutes", 0),
                }
                for t in yd_done
            ],
            "total_done": len(yd_done),
            "cc_sessions": [
                {
                    "project": s.get("project", ""),
                    "task": s.get("task", ""),
                    "status": s.get("status", ""),
                }
                for s in cc_sessions
            ],
            "carry_over": [
                {
                    "title": strip_task_project(t.get("title", "")),
                    "project": (t.get("title", "").split("]")[0].lstrip("[") if "[" in t.get("title", "") else ""),
                }
                for t in yd_carry
            ],
        }

        # ── 오늘 데이터 ──
        today_rows = conn.execute(
            "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id", (today,)
        ).fetchall()
        repo_snapshots = collect_repo_snapshots()
        agent_map = get_agent_status_map(conn)
        learning_model = build_learning_model(conn)
        today_tasks = [
            enrich_task_for_dashboard(dict(row), repo_snapshots, agent_map, learning_model)
            for row in today_rows
        ]
        stats = build_task_stats(today_tasks)

        in_progress = [t for t in today_tasks if t.get("is_active_now")]
        remaining = [t for t in today_tasks if is_task_actionable(t)]

        # 오늘 목표
        goal_row = conn.execute(
            "SELECT user_goal, recommended_goal, confirmed FROM daily_goal_plans WHERE date = ?",
            (today,),
        ).fetchone()
        goal_text = ""
        if goal_row:
            goal_text = goal_row["user_goal"] or goal_row["recommended_goal"] or ""

        # 완료 예상 시각
        total_remaining_minutes = stats.get("remaining_minutes", 0)
        forecast_dt = now + timedelta(minutes=total_remaining_minutes)
        completion_forecast = f"{forecast_dt.strftime('%H:%M')} 완료 예상" if total_remaining_minutes > 0 else "오늘 할 일 완료"

        today_block = {
            "date": today,
            "goal": goal_text,
            "remaining_tasks": [
                {
                    "title": strip_task_project(t.get("title", "")),
                    "project": (t.get("title", "").split("]")[0].lstrip("[") if "[" in t.get("title", "") else ""),
                    "priority": t.get("priority", 2),
                    "est_minutes": resolve_task_remaining_minutes(t),
                    "pct": t.get("progress_pct", 0),
                }
                for t in remaining
            ],
            "in_progress": [
                {
                    "title": strip_task_project(t.get("title", "")),
                    "project": (t.get("title", "").split("]")[0].lstrip("[") if "[" in t.get("title", "") else ""),
                }
                for t in in_progress
            ],
            "total_remaining_minutes": total_remaining_minutes,
            "completion_forecast": completion_forecast,
        }

        # ── 추천 ──
        focus_task = select_focus_task(today_tasks)
        rec_queue = build_recommended_queue(today_tasks, focus_task)

        def _urgency(tone: str) -> str:
            if tone in ("critical",):
                return "critical"
            if tone in ("stale",):
                return "high"
            return "normal"

        now_rec: dict = {}
        queue_rec: list = []

        if rec_queue:
            top = rec_queue[0]
            est = resolve_task_remaining_minutes(next(
                (t for t in today_tasks if t.get("id") == top.get("task_id")), {}
            )) if top.get("task_id") else 30
            urgency = _urgency(top.get("tone", "normal"))

            # attention_minutes 기반 reason 보강
            raw_task = next((t for t in today_tasks if t.get("id") == top.get("task_id")), {})
            attn_min = raw_task.get("attention_minutes", 0)
            if attn_min and urgency in ("critical", "high"):
                reason = f"{attn_min}분 정체 중. 오늘 안에 끝내려면 지금 시작해야 합니다"
            elif urgency == "normal" and est <= 30:
                reason = f"약 {est}분이면 완료 가능. 지금 시작을 권장합니다"
            else:
                reason = top.get("detail", "지금 집중하기 좋은 작업입니다")

            now_rec = {
                "task_id": top.get("task_id"),
                "title": top.get("title", ""),
                "project": raw_task.get("title", "").split("]")[0].lstrip("[") if "[" in raw_task.get("title", "") else "",
                "reason": reason,
                "urgency": urgency,
                "command": top.get("launch_prompt", ""),
                "est_minutes": est,
            }

            for item in rec_queue[1:3]:
                raw = next((t for t in today_tasks if t.get("id") == item.get("task_id")), {})
                queue_rec.append({
                    "task_id": item.get("task_id"),
                    "title": item.get("title", ""),
                    "project": raw.get("title", "").split("]")[0].lstrip("[") if "[" in raw.get("title", "") else "",
                    "reason": item.get("detail", ""),
                    "urgency": _urgency(item.get("tone", "normal")),
                    "command": item.get("launch_prompt", ""),
                    "est_minutes": resolve_task_remaining_minutes(raw),
                })

        remaining_count = len(remaining)
        if remaining_count == 0:
            rec_message = "오늘 모든 작업이 완료되었습니다."
        elif total_remaining_minutes <= 0:
            rec_message = f"오늘 {remaining_count}개 남았습니다."
        else:
            rec_message = f"오늘 {remaining_count}개 남았습니다. 지금 집중하면 {forecast_dt.strftime('%H:%M')} 전에 끝납니다."

        recommendation_block = {
            "now": now_rec,
            "queue": queue_rec,
            "message": rec_message,
        }

        return {
            "yesterday": yesterday_block,
            "today": today_block,
            "recommendation": recommendation_block,
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# UI Prefs — server-side persistence (replaces localStorage)
# ─────────────────────────────────────────────────────────────

class UiPrefsRequest(BaseModel):
    prefs: dict


@app.get("/api/ui-prefs")
def get_ui_prefs():
    conn = get_db()
    try:
        raw = get_setting(conn, "ui_prefs", "{}")
        try:
            prefs = json.loads(raw)
        except Exception:
            prefs = {}
        return prefs
    finally:
        conn.close()


@app.post("/api/ui-prefs")
def save_ui_prefs(payload: UiPrefsRequest):
    conn = get_db()
    try:
        set_setting(conn, "ui_prefs", json.dumps(payload.prefs, ensure_ascii=False))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# User Activity Log
# ─────────────────────────────────────────────────────────────

def log_activity(conn, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO user_activity (action, detail) VALUES (?, ?)",
        (action, json.dumps(detail, ensure_ascii=False)),
    )


@app.get("/api/user-activity")
def get_user_activity(limit: int = 100):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, action, detail, created_at FROM user_activity ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "action": r["action"],
                "detail": json.loads(r["detail"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# WF Groups (project folder groupings) — server-side persistence
# ─────────────────────────────────────────────────────────────

class WfGroupsRequest(BaseModel):
    groups: list


@app.get("/api/wf-groups")
def get_wf_groups():
    conn = get_db()
    try:
        raw = get_setting(conn, "wf_groups", "[]")
        try:
            groups = json.loads(raw)
        except Exception:
            groups = []
        return {"groups": groups}
    finally:
        conn.close()


@app.post("/api/wf-groups")
def save_wf_groups(payload: WfGroupsRequest):
    conn = get_db()
    try:
        set_setting(conn, "wf_groups", json.dumps(payload.groups, ensure_ascii=False))
        log_activity(conn, "wf_groups_save", {"group_count": len(payload.groups)})
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# Telegram helper
# ─────────────────────────────────────────────────────────────

def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Send a plain-text message via Telegram Bot API. Returns True on success."""
    import urllib.request as _ur
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Telegram settings API
# ─────────────────────────────────────────────────────────────

@app.get("/api/settings/telegram")
def get_telegram_settings():
    conn = get_db()
    try:
        return {
            "bot_token": get_setting(conn, "telegram_bot_token", ""),
            "chat_id": get_setting(conn, "telegram_chat_id", ""),
            "enabled": get_setting(conn, "telegram_enabled", "0") == "1",
        }
    finally:
        conn.close()


@app.post("/api/settings/telegram")
def update_telegram_settings(payload: TelegramSettingsRequest):
    conn = get_db()
    try:
        set_setting(conn, "telegram_bot_token", payload.bot_token.strip())
        set_setting(conn, "telegram_chat_id", payload.chat_id.strip())
        set_setting(conn, "telegram_enabled", "1" if payload.enabled else "0")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/telegram-test")
def test_telegram_send():
    conn = get_db()
    try:
        token = get_setting(conn, "telegram_bot_token", "")
        chat_id = get_setting(conn, "telegram_chat_id", "")
    finally:
        conn.close()
    if not token or not chat_id:
        raise HTTPException(400, "Telegram bot_token / chat_id not configured")
    ok = send_telegram_message(token, chat_id, "[Daily Focus] Telegram 연결 테스트 성공")
    if not ok:
        raise HTTPException(502, "Telegram send failed — check token and chat_id")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# End-of-day report
# ─────────────────────────────────────────────────────────────

def build_eod_report(conn, target_date: str) -> dict:
    """오늘 마감 리포트 데이터 빌드."""
    tasks_rows = conn.execute(
        "SELECT * FROM tasks WHERE date = ? ORDER BY priority, id", (target_date,)
    ).fetchall()
    tasks = [dict(r) for r in tasks_rows]

    done = [t for t in tasks if t.get("status") == "done"]
    todo = [t for t in tasks if t.get("status") not in ("done", "carried_over")]
    carried = [t for t in tasks if t.get("status") == "carried_over"]

    done_min = sum(t.get("estimated_minutes", 30) for t in done)

    goal_row = conn.execute(
        "SELECT user_goal, recommended_goal, confirmed FROM daily_goal_plans WHERE date = ?",
        (target_date,),
    ).fetchone()
    goal_text = ""
    if goal_row:
        goal_text = goal_row["user_goal"] or goal_row["recommended_goal"] or ""

    cc_rows = conn.execute(
        "SELECT project, task, status FROM agent_activity WHERE date(created_at) = ? ORDER BY created_at",
        (target_date,),
    ).fetchall()
    cc_sessions = [dict(r) for r in cc_rows]

    cc_projects = {}
    for s in cc_sessions:
        p = s.get("project", "?")
        cc_projects[p] = cc_projects.get(p, 0) + 1

    return {
        "date": target_date,
        "goal": goal_text,
        "done_count": len(done),
        "todo_count": len(todo),
        "carried_count": len(carried),
        "done_minutes": done_min,
        "done_tasks": [{"title": strip_task_project(t["title"]), "minutes": t.get("estimated_minutes", 30)} for t in done],
        "remaining_tasks": [{"title": strip_task_project(t["title"])} for t in todo],
        "cc_sessions_count": len(cc_sessions),
        "cc_projects": [{"project": k, "count": v} for k, v in cc_projects.items()],
    }


def format_eod_telegram(report: dict) -> str:
    lines = [f"[Daily Focus] {report['date']} 마감 리포트"]
    if report.get("goal"):
        lines.append(f"오늘 목표: {report['goal']}")
    lines.append(f"완료: {report['done_count']}개 ({report['done_minutes']}분)")
    if report.get("remaining_tasks"):
        lines.append(f"미완료: {report['todo_count']}개")
        for t in report["remaining_tasks"][:3]:
            lines.append(f"  - {t['title']}")
        if report["todo_count"] > 3:
            lines.append(f"  ... 외 {report['todo_count'] - 3}개")
    if report.get("cc_sessions_count"):
        lines.append(f"CC 세션: {report['cc_sessions_count']}회")
    return "\n".join(lines)


@app.get("/api/eod-report")
def get_eod_report(d: Optional[str] = None):
    target_date = d or kst_today()
    conn = get_db()
    try:
        return build_eod_report(conn, target_date)
    finally:
        conn.close()


@app.post("/api/eod-report/send")
def send_eod_report():
    conn = get_db()
    try:
        token = get_setting(conn, "telegram_bot_token", "")
        chat_id = get_setting(conn, "telegram_chat_id", "")
        enabled = get_setting(conn, "telegram_enabled", "0") == "1"
        if not (enabled and token and chat_id):
            raise HTTPException(400, "Telegram not configured or disabled")
        report = build_eod_report(conn, kst_today())
    finally:
        conn.close()
    text = format_eod_telegram(report)
    ok = send_telegram_message(token, chat_id, text)
    if not ok:
        raise HTTPException(502, "Telegram send failed")
    return {"ok": True, "text": text}


# ─────────────────────────────────────────────────────────────
# Project health score
# ─────────────────────────────────────────────────────────────

def compute_project_health(conn) -> list[dict]:
    """프로젝트별 건강 점수 계산 (0~100)."""
    cutoff_7d = str(datetime.now(_KST).date() - timedelta(days=7))
    cutoff_30d = str(datetime.now(_KST).date() - timedelta(days=30))
    today_str = kst_today()

    # 프로젝트 추출: tasks 테이블에서 [proj] 패턴 + workspace_projects
    proj_set: set[str] = set()

    for row in conn.execute(
        "SELECT DISTINCT title FROM tasks WHERE date >= ?", (cutoff_30d,)
    ).fetchall():
        title = row[0] or ""
        if title.startswith("[") and "]" in title:
            proj_set.add(title.split("]")[0].lstrip("[").strip())

    wp_rows = conn.execute("SELECT project_name FROM workspace_projects WHERE active = 1").fetchall()
    for r in wp_rows:
        if r[0]:
            proj_set.add(r[0].strip())

    results = []
    for proj in sorted(proj_set):
        if not proj:
            continue

        # tasks 30d
        all_tasks = conn.execute(
            "SELECT status, date FROM tasks WHERE title LIKE ? AND date >= ?",
            (f"[{proj}]%", cutoff_30d),
        ).fetchall()
        all_tasks = [dict(r) for r in all_tasks]

        done_7d = sum(1 for t in all_tasks if t["status"] == "done" and t["date"] >= cutoff_7d)
        todo_count = sum(1 for t in all_tasks if t["status"] == "todo" and t["date"] <= today_str)
        stale_count = sum(1 for t in all_tasks if t["status"] == "todo" and t["date"] < today_str)

        # CC activity 7d
        cc_7d = conn.execute(
            "SELECT COUNT(*) FROM agent_activity WHERE project = ? AND date(created_at) >= ?",
            (proj, cutoff_7d),
        ).fetchone()[0] or 0

        # last activity
        last_row = conn.execute(
            "SELECT MAX(date(created_at)) FROM agent_activity WHERE project = ?", (proj,)
        ).fetchone()
        last_cc_date = last_row[0] if last_row and last_row[0] else None
        last_task_row = conn.execute(
            "SELECT MAX(date) FROM tasks WHERE title LIKE ? AND status = 'done'", (f"[{proj}]%",)
        ).fetchone()
        last_task_date = last_task_row[0] if last_task_row and last_task_row[0] else None

        last_active = max(filter(None, [last_cc_date, last_task_date]), default=None)
        days_idle = (datetime.now(_KST).date() - date.fromisoformat(last_active)).days if last_active else 99

        # Score calculation
        score = 60
        score += min(done_7d * 5, 25)      # 최근 완료 (max +25)
        score += min(cc_7d * 3, 15)         # CC 활동 (max +15)
        score -= min(stale_count * 8, 30)   # 밀린 작업 패널티
        score -= min(days_idle * 2, 30)     # 비활성 패널티
        score = max(0, min(100, score))

        if score >= 75:
            status = "good"
        elif score >= 45:
            status = "warn"
        else:
            status = "poor"

        results.append({
            "project": proj,
            "score": score,
            "status": status,
            "done_7d": done_7d,
            "stale_count": stale_count,
            "cc_7d": cc_7d,
            "days_idle": days_idle,
        })

    results.sort(key=lambda x: x["score"])
    return results


@app.get("/api/project-health")
def get_project_health():
    conn = get_db()
    try:
        return {"projects": compute_project_health(conn)}
    finally:
        conn.close()


_HTML_CANDIDATES = ["index.html", "index.htm"]

def _find_project_folder(proj: str) -> Optional[str]:
    snapshot = find_repo_for_project(proj)
    if snapshot:
        return snapshot.get("path")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT folder_path FROM workspace_projects WHERE project_name = ?",
            (proj,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def _find_project_html(folder: str) -> Optional[str]:
    """프로젝트 폴더에서 열 HTML 파일을 찾아 반환. index.html 우선, 없으면 최근 수정 파일."""
    # 1순위: index.html / index.htm
    for name in _HTML_CANDIDATES:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    # 2순위: 폴더 루트의 .html 파일 중 가장 최근 수정된 것
    try:
        html_files = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith(".html") and os.path.isfile(os.path.join(folder, f))
        ]
        if html_files:
            return max(html_files, key=os.path.getmtime)
    except OSError:
        pass
    return None


@app.post("/api/open-project-html")
def open_project_html(proj: str):
    """프로젝트 폴더의 HTML 파일을 OS 기본 브라우저로 열기."""
    import subprocess
    folder = _find_project_folder(proj)
    if not folder:
        raise HTTPException(status_code=404, detail="project folder not found")
    target = _find_project_html(folder)
    if not target:
        raise HTTPException(status_code=404, detail="no HTML file found in project folder")
    try:
        os.startfile(target)
    except AttributeError:
        subprocess.Popen(["xdg-open", target])
    return {"ok": True, "path": target}


# ═══════════════════════════════════════════════════════════════════
# FOUNDATION OS API  (Phase 2 + Phase 4 판정 엔진)
# ═══════════════════════════════════════════════════════════════════

STACK_LIMIT = 3  # 스택당 동시 진행 최대 3개

_STACK_LABELS = {"수익": "revenue", "성장": "growth", "실험": "experiment"}
_TIME_BLOCKS = [
    ("block1",  "10:00", "13:00", "블록 1 — 성장 제품"),
    ("lunch",   "13:00", "14:00", "점심 + 메시지 확인"),
    ("block2",  "14:00", "16:00", "블록 2 — 수익 제품"),
    ("block3a", "16:00", "17:00", "블록 3-A — 내일 도면"),
    ("block3b", "17:00", "17:30", "블록 3-B — 자동 판정"),
    ("block3c", "17:30", "18:00", "블록 3-C — 실험 제품"),
]


def _current_time_block() -> dict:
    """현재 KST 시각 기준 활성 시간 블록 반환."""
    now_hm = datetime.now(_KST).strftime("%H:%M")
    for key, start, end, label in _TIME_BLOCKS:
        if start <= now_hm < end:
            return {"key": key, "label": label, "start": start, "end": end, "active": True}
    if now_hm < "10:00":
        return {"key": "pre", "label": "Daily Boot 준비 시간", "start": "00:00", "end": "10:00", "active": False}
    return {"key": "done", "label": "하루 종료", "start": "18:00", "end": "24:00", "active": False}


def _run_auto_judge(conn) -> list[dict]:
    """
    Foundation OS Section 6: 자동 판정 엔진
    로그 → 아웃풋 → 귀속 순서로 모든 active 작업 평가.
    결과를 fkc_log에 저장하고 리스트로 반환.
    """
    today = kst_today()
    three_days_ago = (datetime.now(_KST) - timedelta(days=3)).strftime("%Y-%m-%d")

    active_tasks = conn.execute(
        "SELECT id, title, stack, output_note, updated_at, created_at FROM tasks "
        "WHERE status != 'done' AND (task_state IS NULL OR task_state != 'split') "
        "AND date <= ?",
        (today,),
    ).fetchall()

    results = []
    for t in active_tasks:
        tid = t["id"]
        verdict = "fill"
        reason = ""

        # STEP 1: 로그 체크 — 오늘 activity 또는 업데이트가 있는가?
        has_today_log = bool(conn.execute(
            "SELECT 1 FROM agent_activity WHERE created_at >= ? LIMIT 1",
            (today,),
        ).fetchone())
        task_updated_today = (t["updated_at"] or "")[:10] == today

        if not has_today_log and not task_updated_today:
            verdict = "kill"
            reason = "오늘 로그 없음"
        else:
            # STEP 2: 아웃풋 체크 — 3일간 output_note 기록이 있는가?
            has_output = bool(t["output_note"] and t["output_note"].strip())
            recent_done = conn.execute(
                "SELECT 1 FROM tasks WHERE status='done' AND completed_at >= ? AND "
                "(title LIKE ? OR stack = ?) LIMIT 1",
                (three_days_ago, f"%{(t['title'] or '')[:20]}%", t["stack"] or ""),
            ).fetchone()
            if not has_output and not recent_done:
                verdict = "kill"
                reason = "3일간 아웃풋 없음"
            else:
                # STEP 3: 귀속 체크 — 북극성(수익/성장)에 연결되는가?
                stack = (t["stack"] or "").strip()
                if stack in ("수익", "성장"):
                    verdict = "fill"
                    reason = f"북극성 연결 ({stack} 스택)"
                elif stack == "실험":
                    verdict = "call"
                    reason = "실험 스택 — 7일 데드라인 확인 필요"
                else:
                    verdict = "call"
                    reason = "스택 미지정 — 북극성 연결 불명확"

        # 오늘 이미 자동 판정된 경우 덮어쓰지 않음
        already = conn.execute(
            "SELECT 1 FROM fkc_log WHERE task_id=? AND auto=1 AND date(judged_at)=?",
            (tid, today),
        ).fetchone()
        if not already:
            conn.execute(
                "INSERT INTO fkc_log (task_id, verdict, reason, auto) VALUES (?,?,?,1)",
                (tid, verdict, reason),
            )
        results.append({"task_id": tid, "title": t["title"], "verdict": verdict, "reason": reason})

    conn.commit()
    return results


def _auto_kill_expired_experiments(conn) -> list[dict]:
    """실험 제품 7일 데드라인 초과 + 아웃풋 없으면 자동 KILL."""
    today = kst_today()
    expired = conn.execute(
        "SELECT id, name FROM experiments WHERE status='active' AND deadline < ? AND (output_note='' OR output_note IS NULL)",
        (today,),
    ).fetchall()
    killed = []
    for exp in expired:
        conn.execute("UPDATE experiments SET status='killed', updated_at=? WHERE id=?", (kst_now(), exp["id"]))
        killed.append({"id": exp["id"], "name": exp["name"]})
    if killed:
        conn.commit()
    return killed


# ── API 09: 스택 현황 ──────────────────────────────────────────────
@app.get("/api/foundation/stack-status")
def foundation_stack_status():
    today = kst_today()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT stack, COUNT(*) as cnt FROM tasks "
            "WHERE status != 'done' AND stack != '' AND stack IS NOT NULL AND date <= ?",
            (today,),
        ).fetchall()
        counts = {r["stack"]: r["cnt"] for r in rows}
        stacks = ["수익", "성장", "실험"]
        result = []
        for s in stacks:
            cnt = counts.get(s, 0)
            result.append({"stack": s, "count": cnt, "limit": STACK_LIMIT, "over": cnt >= STACK_LIMIT})
        return {"stacks": result, "total_active": sum(counts.values())}
    finally:
        conn.close()


# ── API 10: FKC 판정 저장 ──────────────────────────────────────────
class FKCRequest(BaseModel):
    verdict: str   # fill / kill / call
    reason: Optional[str] = ""


@app.post("/api/tasks/{task_id}/fkc")
def post_task_fkc(task_id: int, body: FKCRequest):
    if body.verdict not in ("fill", "kill", "call"):
        raise HTTPException(400, "verdict must be fill, kill, or call")
    conn = get_db()
    try:
        task = conn.execute("SELECT id, title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "task not found")
        conn.execute(
            "INSERT INTO fkc_log (task_id, verdict, reason, auto) VALUES (?,?,?,0)",
            (task_id, body.verdict, body.reason or "", ),
        )
        # KILL이면 작업 상태도 변경
        if body.verdict == "kill":
            conn.execute(
                "UPDATE tasks SET task_state='split', decision_note=?, updated_at=? WHERE id=?",
                (f"KILL: {body.reason}", kst_now(), task_id),
            )
        conn.commit()
        return {"ok": True, "task_id": task_id, "verdict": body.verdict}
    finally:
        conn.close()


# ── API 11: 자동 판정 결과 조회 ────────────────────────────────────
@app.get("/api/foundation/auto-judge")
def get_auto_judge():
    today = kst_today()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT f.task_id, f.verdict, f.reason, f.judged_at, t.title "
            "FROM fkc_log f LEFT JOIN tasks t ON t.id=f.task_id "
            "WHERE date(f.judged_at)=? ORDER BY f.judged_at DESC",
            (today,),
        ).fetchall()
        items = [dict(r) for r in rows]
        summary = {"fill": 0, "kill": 0, "call": 0}
        for it in items:
            v = it.get("verdict", "")
            if v in summary:
                summary[v] += 1
        return {"date": today, "items": items, "summary": summary}
    finally:
        conn.close()


# ── API 12: 자동 판정 실행 ─────────────────────────────────────────
@app.post("/api/foundation/auto-judge/run")
def run_auto_judge():
    conn = get_db()
    try:
        results = _run_auto_judge(conn)
        killed_exp = _auto_kill_expired_experiments(conn)
        summary = {"fill": 0, "kill": 0, "call": 0}
        for r in results:
            v = r.get("verdict", "")
            if v in summary:
                summary[v] += 1
        return {"ok": True, "results": results, "summary": summary, "experiments_killed": killed_exp}
    finally:
        conn.close()


# ── API 13: 실험 목록 조회 ─────────────────────────────────────────
@app.get("/api/experiments")
def get_experiments():
    today = kst_today()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            if d.get("deadline") and d["status"] == "active":
                try:
                    dl = date.fromisoformat(d["deadline"])
                    td = date.fromisoformat(today)
                    d["days_left"] = (dl - td).days
                except Exception:
                    d["days_left"] = None
            else:
                d["days_left"] = None
            items.append(d)
        return {"experiments": items}
    finally:
        conn.close()


# ── API 14: 실험 생성 ──────────────────────────────────────────────
class ExperimentCreate(BaseModel):
    name: str
    output_criteria: str
    deadline: Optional[str] = None   # YYYY-MM-DD, 없으면 오늘+7일


@app.post("/api/experiments")
def create_experiment(body: ExperimentCreate):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    if not body.output_criteria.strip():
        raise HTTPException(400, "output_criteria required")
    conn = get_db()
    try:
        deadline = body.deadline or (
            datetime.now(_KST) + timedelta(days=7)
        ).strftime("%Y-%m-%d")
        cur = conn.execute(
            "INSERT INTO experiments (name, output_criteria, deadline) VALUES (?,?,?)",
            (body.name.strip(), body.output_criteria.strip(), deadline),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "deadline": deadline}
    finally:
        conn.close()


# ── API 15: 실험 액션 (complete / kill / promote) ──────────────────
class ExperimentAction(BaseModel):
    action: str       # complete / kill / promote
    output_note: Optional[str] = ""


@app.post("/api/experiments/{exp_id}/action")
def experiment_action(exp_id: int, body: ExperimentAction):
    if body.action not in ("complete", "kill", "promote"):
        raise HTTPException(400, "action must be complete, kill, or promote")
    conn = get_db()
    try:
        exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
        if not exp:
            raise HTTPException(404, "experiment not found")
        now = kst_now()
        if body.action == "complete":
            conn.execute(
                "UPDATE experiments SET status='completed', output_note=?, updated_at=? WHERE id=?",
                (body.output_note or "", now, exp_id),
            )
        elif body.action == "kill":
            conn.execute("UPDATE experiments SET status='killed', updated_at=? WHERE id=?", (now, exp_id))
        elif body.action == "promote":
            conn.execute("UPDATE experiments SET status='promoted', updated_at=? WHERE id=?", (now, exp_id))
        conn.commit()
        return {"ok": True, "id": exp_id, "action": body.action}
    finally:
        conn.close()


# ── API 16: Daily Boot 오늘 조회 ───────────────────────────────────
@app.get("/api/daily-boot/today")
def get_daily_boot_today():
    today = kst_today()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM daily_boot_log WHERE date=?", (today,)).fetchone()
        if not row:
            return {"date": today, "confirmed": False, "record": None}
        return {"date": today, "confirmed": bool(row["confirmed"]), "record": dict(row)}
    finally:
        conn.close()


# ── API 17: Daily Boot 저장 ────────────────────────────────────────
class DailyBootCreate(BaseModel):
    yesterday_done: Optional[str] = ""
    today_task_id: Optional[int] = None
    completion_criteria: str
    start_point: Optional[str] = ""
    expected_blocker: Optional[str] = ""


@app.post("/api/daily-boot")
def post_daily_boot(body: DailyBootCreate):
    if not body.completion_criteria.strip():
        raise HTTPException(400, "completion_criteria required")
    today = kst_today()
    now = kst_now()
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO daily_boot_log
               (date, yesterday_done, today_task_id, completion_criteria,
                start_point, expected_blocker, confirmed, confirmed_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (today, body.yesterday_done or "", body.today_task_id,
             body.completion_criteria.strip(), body.start_point or "",
             body.expected_blocker or "", now),
        )
        conn.commit()
        return {"ok": True, "date": today}
    finally:
        conn.close()


# ── API 18: 현재 시간 블록 ─────────────────────────────────────────
@app.get("/api/foundation/time-block/current")
def get_current_time_block():
    block = _current_time_block()
    now_str = datetime.now(_KST).strftime("%H:%M")
    return {**block, "now": now_str}


# ── API 19: 내일 도면 저장 ─────────────────────────────────────────
class TomorrowBlueprint(BaseModel):
    today_task_id: Optional[int] = None
    completion_criteria: str
    start_point: Optional[str] = ""
    expected_blocker: Optional[str] = ""


@app.post("/api/foundation/tomorrow-blueprint")
def post_tomorrow_blueprint(body: TomorrowBlueprint):
    if not body.completion_criteria.strip():
        raise HTTPException(400, "completion_criteria required")
    # 내일 날짜로 daily_boot_log에 미리 저장
    tomorrow = (datetime.now(_KST) + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO daily_boot_log
               (date, today_task_id, completion_criteria, start_point, expected_blocker, confirmed)
               VALUES (?,?,?,?,?,0)""",
            (tomorrow, body.today_task_id, body.completion_criteria.strip(),
             body.start_point or "", body.expected_blocker or ""),
        )
        conn.commit()
        return {"ok": True, "date": tomorrow}
    finally:
        conn.close()


class CodingReportPayload(BaseModel):
    team: str          # "codingteam1" | "codingteam2"
    session_id: str
    project: Optional[str] = ""
    issues_count: int = 0
    fixed_count: int = 0
    report_text: str = ""


@app.post("/api/coding-report")
def post_coding_report(body: CodingReportPayload):
    """코딩1팀/2팀이 야간 작업 완료 후 최종 보고서를 전송하는 웹훅 엔드포인트."""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO coding_reports (team, session_id, project, issues_count, fixed_count, report_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
            (body.team, body.session_id, body.project, body.issues_count, body.fixed_count, body.report_text),
        )
        # agent_status도 done으로 업데이트
        conn.execute(
            """INSERT INTO agent_status (project, task, status, url, updated_at)
               VALUES (?, ?, 'done', '', datetime('now','localtime'))
               ON CONFLICT(project) DO UPDATE SET
                  task=excluded.task, status='done', updated_at=excluded.updated_at""",
            (body.team, f"night run done — {body.fixed_count} fixed / {body.issues_count} issues"),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/coding-reports")
def get_coding_reports(limit: int = 20):
    """최근 코딩팀 보고서 목록 반환."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, team, session_id, project, issues_count, fixed_count, created_at
               FROM coding_reports ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="data/uploads"), name="uploads")

# SessionMiddleware must be added LAST so it executes FIRST (outermost layer),
# making request.session available to auth_middleware above.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="df_session", max_age=86400 * 30)
