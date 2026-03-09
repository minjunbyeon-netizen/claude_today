#!/usr/bin/env python3
"""
Daily Focus 아침 루틴
1. 컴퓨터의 git 프로젝트 스캔 (최근 활동 기반)
2. 맥락을 바탕으로 오늘 작업 질문
3. 답변을 Daily Focus API에 자동 등록
"""
import os
import subprocess
import requests
import sys
from datetime import date, timedelta

API_BASE = "http://localhost:8888"

# ── 스캔할 루트 디렉토리 (필요에 따라 추가) ──────────────────
SCAN_ROOTS = [
    r"G:\내 드라이브\01_work",
    r"C:\Users\USER\Desktop",
    r"C:\Users\USER\Documents",
]
MAX_DEPTH = 4  # 탐색 깊이

PRIORITY_LABELS = {"1": "높음", "2": "보통", "3": "낮음"}


# ─────────────────────────────────────────────────────────────
# Git 스캔
# ─────────────────────────────────────────────────────────────

def find_git_repos(root):
    repos = []
    try:
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.replace(root, "").count(os.sep)
            if depth >= MAX_DEPTH:
                dirnames.clear()
                continue
            # 숨김 폴더, 가상환경, node_modules 제외
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__", ".git")
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


def recent_commits(repo, days=2):
    since = (date.today() - timedelta(days=days)).isoformat()
    out = git_cmd(["log", f"--since={since}", "--oneline", "--no-merges", "-8"], repo)
    return [l.strip() for l in out.splitlines() if l.strip()]


def uncommitted(repo):
    out = git_cmd(["status", "--short"], repo)
    return len([l for l in out.splitlines() if l.strip()])


def current_branch(repo):
    return git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def scan_active_projects():
    """최근 활동이 있는 프로젝트만 반환"""
    active = []
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for repo in find_git_repos(root):
            commits = recent_commits(repo)
            dirty = uncommitted(repo)
            if commits or dirty:
                active.append({
                    "path": repo,
                    "name": os.path.basename(repo),
                    "commits": commits,
                    "uncommitted": dirty,
                    "branch": current_branch(repo),
                })
    # 커밋 많은 순 정렬
    active.sort(key=lambda x: len(x["commits"]), reverse=True)
    return active


# ─────────────────────────────────────────────────────────────
# Daily Focus API
# ─────────────────────────────────────────────────────────────

def api_add_task(title, minutes=30, priority=2):
    try:
        r = requests.post(f"{API_BASE}/api/tasks", json={
            "title": title, "estimated_minutes": minutes, "priority": priority
        }, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def api_add_goal(goal):
    try:
        r = requests.post(f"{API_BASE}/api/week/goals", json={"goal": goal}, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def check_server():
    try:
        r = requests.get(f"{API_BASE}/api/today", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# 대화 헬퍼
# ─────────────────────────────────────────────────────────────

def ask(prompt, default=None, choices=None):
    """입력 받기. default 있으면 엔터로 선택 가능."""
    suffix = ""
    if choices:
        suffix = f" ({'/'.join(choices)})"
    if default is not None:
        suffix += f" [기본값: {default}]"
    full = f"  → {prompt}{suffix}: "
    while True:
        ans = input(full).strip()
        if not ans and default is not None:
            return str(default)
        if not ans:
            continue
        if choices and ans not in choices:
            print(f"    ! {'/'.join(choices)} 중에서 선택하세요")
            continue
        return ans


def yn(prompt):
    return ask(prompt, choices=["y", "n"]) == "y"


def hr():
    print("  " + "─" * 46)


def section(title):
    print(f"\n{'━'*50}")
    print(f"  {title}")
    print(f"{'━'*50}")


# ─────────────────────────────────────────────────────────────
# 메인 루틴
# ─────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print(f"""
╔══════════════════════════════════════════════════╗
║         Daily Focus  아침 루틴                  ║
║         {today.strftime('%Y년 %m월 %d일')}                       ║
╚══════════════════════════════════════════════════╝""")

    # 서버 연결 확인
    if not check_server():
        print("""
  [!] Daily Focus 서버에 연결할 수 없습니다.
      start.bat 을 먼저 실행하세요.
      (http://localhost:8888)
""")
        sys.exit(1)

    # ── 1단계: 프로젝트 스캔 ──────────────────────────────
    section("1단계 | 최근 작업 프로젝트 스캔 중...")
    projects = scan_active_projects()

    if not projects:
        print("\n  최근 활동한 git 프로젝트를 찾지 못했습니다.")
        print(f"  스캔 경로: {', '.join(SCAN_ROOTS)}\n")
    else:
        print(f"\n  최근 2일 이내 활동 프로젝트 {len(projects)}개 발견:\n")
        for i, p in enumerate(projects, 1):
            branch_info = f"  [{p['branch']}]" if p["branch"] else ""
            print(f"  {i}. {p['name']}{branch_info}")
            print(f"     경로: {p['path']}")
            if p["commits"]:
                for c in p["commits"][:3]:
                    print(f"     └─ {c}")
            if p["uncommitted"]:
                print(f"     └─ 미커밋 변경사항 {p['uncommitted']}개")
            print()

    # ── 2단계: 프로젝트별 오늘 계획 확인 ─────────────────
    section("2단계 | 오늘 작업 계획")
    tasks_added = 0

    # 감지된 프로젝트 기반 질문
    for p in projects:
        hr()
        print(f"\n  [{p['name']}] 프로젝트")
        if p["commits"]:
            print(f"  최근 커밋: {p['commits'][0]}")
        if p["uncommitted"]:
            print(f"  미커밋 변경사항: {p['uncommitted']}개")

        cont = yn(f"\n  오늘 [{p['name']}] 작업을 계속하시나요?")
        if not cont:
            continue

        print(f"\n  [{p['name']}] 에서 오늘 할 작업을 입력하세요.")
        print("  (빈 줄 입력 시 다음으로 넘어갑니다)\n")

        while True:
            title = input("  작업명 (빈 줄=완료): ").strip()
            if not title:
                break
            title_full = f"[{p['name']}] {title}"
            mins = ask("예상 시간(분)", 30)
            pri = ask("우선순위", 2, ["1", "2", "3"])
            ok = api_add_task(title_full, int(mins), int(pri))
            if ok:
                print(f"    ✓ 등록됨: {title_full} ({mins}분, 우선순위 {PRIORITY_LABELS[str(pri)]})\n")
                tasks_added += 1
            else:
                print("    ✗ 등록 실패\n")

    # ── 3단계: 추가 작업 (새 프로젝트 or 기타) ───────────
    hr()
    extra = yn("\n  위 외에 오늘 추가로 할 작업이 있나요?")
    if extra:
        print("\n  추가 작업을 입력하세요. (빈 줄 입력 시 완료)\n")
        while True:
            title = input("  작업명 (빈 줄=완료): ").strip()
            if not title:
                break
            mins = ask("예상 시간(분)", 30)
            pri = ask("우선순위", 2, ["1", "2", "3"])
            ok = api_add_task(title, int(mins), int(pri))
            if ok:
                print(f"    ✓ 등록됨 ({mins}분, 우선순위 {PRIORITY_LABELS[str(pri)]})\n")
                tasks_added += 1

    # ── 4단계: 주간 목표 ──────────────────────────────────
    section("3단계 | 이번 주 목표")
    add_goal = yn("  이번 주 목표를 추가하시겠어요?")
    goals_added = 0
    if add_goal:
        print("  (빈 줄 입력 시 완료)\n")
        while True:
            goal = input("  목표: ").strip()
            if not goal:
                break
            if api_add_goal(goal):
                print(f"    ✓ 추가됨\n")
                goals_added += 1

    # ── 완료 요약 ─────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════╗
║  완료!                                          ║
║  작업 {tasks_added}개 등록  |  주간 목표 {goals_added}개 추가           ║
║  http://localhost:8888 에서 확인하세요           ║
╚══════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
