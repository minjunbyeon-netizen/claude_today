#!/usr/bin/env python3
"""
Daily Focus - 아침 자동 루틴 (API 없음, 완전 자동)

더블클릭 한 번으로:
1. 활성 프로젝트 스캔 (git 활동 기준)
2. CLAUDE.md / 투두파일 / git 커밋에서 할 일 추출
3. Daily Focus에 자동 등록
4. 브라우저 오픈
"""

import os
import subprocess
import requests
import webbrowser
import time
import sys
from datetime import date, timedelta

# Windows 터미널 한글/특수문자 인코딩 문제 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

API_BASE = "http://localhost:8001"

SCAN_ROOTS = [
    r"G:\내 드라이브\01_work",
    r"G:\내 드라이브\01_work\_agents",
    r"C:\Users\USER\Desktop",
    r"C:\Users\USER\Documents",
]
MAX_DEPTH = 4

PRIORITY_MAP = {
    "기획팀": 1, "실무팀": 1,
    "검수팀": 2, "소비자팀": 2,
    "리뷰팀": 3,
}


# ── git 스캔 ──────────────────────────────────────────────────

def find_git_repos(root):
    repos = []
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


def git_cmd(args, cwd):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return ""


def scan_active_projects():
    active = []
    since = (date.today() - timedelta(days=2)).isoformat()
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for repo in find_git_repos(root):
            commits_raw = git_cmd(["log", f"--since={since}", "--oneline", "--no-merges", "-6"], repo)
            dirty_raw = git_cmd(["status", "--short"], repo)
            commits = [l for l in commits_raw.splitlines() if l.strip()]
            dirty = len([l for l in dirty_raw.splitlines() if l.strip()])
            if commits or dirty:
                active.append({
                    "path": repo,
                    "name": os.path.basename(repo),
                    "commits": commits,
                    "uncommitted": dirty,
                })
    active.sort(key=lambda x: len(x["commits"]), reverse=True)
    return active


# ── 파일 읽기 ────────────────────────────────────────────────

def read_safe(path, max_chars=3000):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(max_chars)
    except Exception:
        return ""


# ── 할 일 추출 ───────────────────────────────────────────────

def extract_from_todo_md(content, team_name, path_prefix):
    """*_todo.md에서 미완료 항목 추출"""
    tasks = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- [ ]"):
            continue
        title_raw = stripped[6:].strip()
        if not title_raw:
            continue
        priority = PRIORITY_MAP.get(team_name, 2)
        tasks.append({
            "title": f"[{path_prefix}] {title_raw}",
            "minutes": 30,
            "priority": priority,
        })
    return tasks


def extract_from_commits(commits, project_name):
    """git 커밋 메시지에서 오늘 이어서 할 작업 힌트 추출"""
    if not commits:
        return []
    # 가장 최근 커밋 1개를 "이어서 작업" 항목으로
    latest = commits[0]
    # 해시 제거 (첫 단어)
    msg = " ".join(latest.split()[1:]) if " " in latest else latest
    return [{
        "title": f"[{project_name}] 이어서: {msg[:60]}",
        "minutes": 60,
        "priority": 2,
    }]


def extract_from_current_task(content, project_name):
    """current_task.md에서 작업명 추출"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("|") and not stripped.startswith("-"):
            return [{
                "title": f"[{project_name}] {stripped[:80]}",
                "minutes": 60,
                "priority": 1,
            }]
    return []


def gather_tasks_from_project(project):
    path = project["path"]
    name = project["name"]
    tasks = []
    seen_titles = set()

    def add(t):
        if t["title"] not in seen_titles:
            seen_titles.add(t["title"])
            tasks.append(t)

    # 1) logs/todo/*.md 미완료 항목
    todo_dir = os.path.join(path, "logs", "todo")
    if os.path.isdir(todo_dir):
        for fname in sorted(os.listdir(todo_dir)):
            if fname.endswith("_todo.md"):
                team = fname.replace("_todo.md", "")
                content = read_safe(os.path.join(todo_dir, fname))
                for t in extract_from_todo_md(content, team, name):
                    add(t)

    # 2) logs/status/current_task.md
    ct_path = os.path.join(path, "logs", "status", "current_task.md")
    if os.path.isfile(ct_path) and not tasks:
        content = read_safe(ct_path)
        for t in extract_from_current_task(content, name):
            add(t)

    # 3) 미완료 항목이 없으면 git 커밋 기반 "이어서" 항목 추가
    if not tasks and project["commits"]:
        for t in extract_from_commits(project["commits"], name):
            add(t)

    # 4) 미커밋 변경사항이 있으면 "커밋 필요" 항목 추가
    if project["uncommitted"] >= 5:
        add({
            "title": f"[{name}] 미커밋 변경사항 {project['uncommitted']}개 정리/커밋",
            "minutes": 20,
            "priority": 1,
        })

    return tasks


# ── Daily Focus API ──────────────────────────────────────────

def server_running():
    try:
        return requests.get(f"{API_BASE}/api/today", timeout=2).status_code == 200
    except Exception:
        return False


def start_server():
    import sys
    script_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "warning"],
        cwd=script_dir,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def add_task(title, minutes=30, priority=2):
    try:
        r = requests.post(f"{API_BASE}/api/tasks", json={
            "title": title, "estimated_minutes": minutes, "priority": priority
        }, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def get_existing_titles():
    try:
        r = requests.get(f"{API_BASE}/api/today", timeout=3)
        data = r.json()
        return {t["title"] for t in data.get("tasks", [])}
    except Exception:
        return set()


# ── 메인 ─────────────────────────────────────────────────────

def main():
    print()
    print("=" * 52)
    print(f"  Daily Focus  |  {date.today().strftime('%Y.%m.%d')}")
    print("=" * 52)

    # 서버 시작
    if not server_running():
        print("  서버 시작 중...")
        start_server()
        for _ in range(10):
            time.sleep(1)
            if server_running():
                break
        else:
            print("  [오류] 서버를 시작할 수 없습니다.")
            return
    print("  서버 연결 OK")

    # 기존 등록된 제목 (중복 방지)
    existing = get_existing_titles()

    # 프로젝트 스캔
    print("  프로젝트 스캔 중...", end="", flush=True)
    projects = scan_active_projects()
    print(f" {len(projects)}개 발견")
    print()

    total = 0
    for project in projects:
        tasks = gather_tasks_from_project(project)
        if not tasks:
            continue

        print(f"  [{project['name']}]  커밋 {len(project['commits'])}개 / 미커밋 {project['uncommitted']}개")
        for task in tasks:
            if task["title"] in existing:
                print(f"    - (중복 스킵) {task['title'][:55]}")
                continue
            ok = add_task(task["title"], task["minutes"], task["priority"])
            if ok:
                existing.add(task["title"])
                total += 1
                print(f"    + {task['title'][:55]}")
        print()

    print("=" * 52)
    print(f"  완료 -- {total}개 등록")
    print("=" * 52)
    print()

    # 브라우저 열기
    time.sleep(0.5)
    webbrowser.open(API_BASE)


if __name__ == "__main__":
    main()
