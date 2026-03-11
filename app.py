from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import json
import sqlite3
import os
import re
import subprocess
from datetime import date, datetime, timedelta

app = FastAPI()
DB_PATH = "data/focus.db"
AI_SPEED_FACTOR = 0.42
REPO_SCAN_ROOTS = [r"C:\work", r"C:\Users\USER\Desktop"]
REPO_SCAN_MAX_DEPTH = 2
REPO_SCAN_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
WORKSPACE_WATCH_ROOT = r"C:\work"
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
    """)
    conn.commit()
    conn.close()


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
    if not activity_rows and target_date == str(date.today()):
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
    delta = datetime.now() - parsed
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
    today_start = f"{date.today().isoformat()} 00:00:00"

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

    today = str(date.today())
    now = datetime.now().isoformat()
    title = workspace_discovery_task_title(project_name)
    note = f"자동 감지된 C:\\work 폴더: {folder_path}"
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

    now = datetime.now().isoformat(timespec="seconds")
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
    now = datetime.now().isoformat(timespec="seconds")
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
    task["can_open_claude"] = bool(repo_snapshot)
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
            "last_refreshed_at": datetime.now().isoformat(timespec="seconds"),
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

    if not latest_manual and target_date == str(date.today()):
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

    if target_date == str(date.today()):
        due_at = datetime.strptime(f"{target_date} {morning_time}", "%Y-%m-%d %H:%M")
        minutes_until = int((due_at - datetime.now()).total_seconds() // 60)
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
    estimated_minutes: int = 15
    priority: int = 2
    date: Optional[str] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    estimated_minutes: Optional[int] = None
    priority: Optional[int] = None
    notes: Optional[str] = None


class TaskDecisionRequest(BaseModel):
    action: str
    note: Optional[str] = ""
    split_titles: list[str] = []


class MorningBriefTimeRequest(BaseModel):
    time: str


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
    return build_day_snapshot(d)


@app.get("/api/ops-brief")
def get_ops_brief(d: Optional[str] = None):
    if not d:
        d = str(date.today())
    snapshot = build_day_snapshot(d)
    return snapshot["brief"]


@app.get("/api/morning-brief")
def get_morning_brief(d: Optional[str] = None):
    if not d:
        d = str(date.today())
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
    target_date = payload.date or str(date.today())
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
        confirmed_at = datetime.now().isoformat()
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
        task.date = str(date.today())
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (date, title, estimated_minutes, priority, updated_at) VALUES (?, ?, ?, ?, ?)",
        (task.date, task.title, task.estimated_minutes, task.priority, datetime.now().isoformat()),
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
        updates["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*updates.values(), task_id))
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
    now = datetime.now().isoformat()
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
    checkins = build_checkin_feed(conn, d)
    conn.close()
    return checkins


@app.get("/api/yesterday-undone")
def yesterday_undone():
    yesterday = str(date.today() - timedelta(days=1))
    conn = get_db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over') AND COALESCE(task_state, 'active') = 'active'",
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
        "SELECT * FROM tasks WHERE date = ? AND status NOT IN ('done', 'carried_over') AND COALESCE(task_state, 'active') = 'active'",
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
        logs_path = r"C:\work\squad-team\logs"

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
    SCAN_ROOTS = REPO_SCAN_ROOTS
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
                        (datetime.now().isoformat(), task_id),
                    )
                    updated.append(title)

    conn.commit()
    conn.close()
    return {"updated": updated, "count": len(updated), "scanned": scanned}


@app.post("/api/open-claude")
def open_claude(proj: str, prompt: str = ""):
    """프로젝트명으로 레포를 찾아 Claude Code 터미널 열기"""
    repo = find_repo_for_project(proj)
    if not repo or not repo.get("path"):
        return {"ok": False, "error": f"경로를 찾을 수 없음: {proj}"}
    target_path = repo["path"]

    CLAUDE_CMD = r"C:\Users\USER\AppData\Roaming\npm\claude.cmd"

    # 1) 새 탭 열기 + claude 실행 (CLAUDECODE 환경변수 제거하여 중첩 세션 오류 우회)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    prompt_clean = summarize_agent_text(clean_utf8_text(prompt), limit=1200)
    claude_command = subprocess.list2cmdline([CLAUDE_CMD, prompt_clean]) if prompt_clean else subprocess.list2cmdline([CLAUDE_CMD])
    try:
        subprocess.Popen(
            ["wt", "new-tab", "-d", target_path, "--", "cmd", "/k", claude_command],
            shell=False,
            env=env,
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

    return {"ok": True, "path": target_path, "method": "wt", "prompted": bool(prompt_clean)}


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
