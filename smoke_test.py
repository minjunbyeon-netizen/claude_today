#!/usr/bin/env python3
"""Basic smoke checks for the daily-focus app."""

from __future__ import annotations

import json
import sqlite3
import sys
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

import app


SMOKE_TASK_TITLE = "[smoke-test] temporary task"
SMOKE_CHECKIN_NOTE = "smoke checkin"
SMOKE_PROJECT = "smoke-project"
SMOKE_SURROGATE_PROJECT = "smoke-surrogate"
SMOKE_GOAL_DATE = "2099-12-31"
SMOKE_WEEK_START = "2099-12-28"
SMOKE_WEEK_GOAL = "[smoke-test] weekly goal"
SMOKE_DISCOVERY_PROJECT = "smoke-discovery-workspace"
SMOKE_DISCOVERY_TASK = f"[{SMOKE_DISCOVERY_PROJECT}] 새 폴더 감지 - 목표와 첫 작업 정의"
SMOKE_DISCOVERY_PATH = Path(r"C:\work") / SMOKE_DISCOVERY_PROJECT
SMOKE_SPLIT_TITLES = [
    "[smoke-test] split child 1",
    "[smoke-test] split child 2",
]


def cleanup() -> None:
    conn = sqlite3.connect(app.DB_PATH)
    cur = conn.cursor()
    task_ids = [row[0] for row in cur.execute("SELECT id FROM tasks WHERE title = ?", (SMOKE_TASK_TITLE,)).fetchall()]
    for title in SMOKE_SPLIT_TITLES:
        task_ids.extend(row[0] for row in cur.execute("SELECT id FROM tasks WHERE title = ?", (title,)).fetchall())
    if task_ids:
        cur.execute(
            f"DELETE FROM task_decisions WHERE task_id IN ({','.join('?' for _ in task_ids)})",
            task_ids,
        )
    cur.execute("DELETE FROM tasks WHERE title = ?", (SMOKE_TASK_TITLE,))
    for title in SMOKE_SPLIT_TITLES:
        cur.execute("DELETE FROM tasks WHERE title = ?", (title,))
    cur.execute("DELETE FROM checkins WHERE note = ?", (SMOKE_CHECKIN_NOTE,))
    cur.execute("DELETE FROM agent_activity WHERE project = ?", (SMOKE_PROJECT,))
    cur.execute("DELETE FROM agent_status WHERE project = ?", (SMOKE_PROJECT,))
    cur.execute("DELETE FROM agent_activity WHERE project = ?", (SMOKE_SURROGATE_PROJECT,))
    cur.execute("DELETE FROM agent_status WHERE project = ?", (SMOKE_SURROGATE_PROJECT,))
    cur.execute("DELETE FROM daily_goal_plans WHERE date = ?", (SMOKE_GOAL_DATE,))
    cur.execute("DELETE FROM weekly_goals WHERE goal = ?", (SMOKE_WEEK_GOAL,))
    cur.execute("DELETE FROM tasks WHERE title = ?", (SMOKE_DISCOVERY_TASK,))
    cur.execute("DELETE FROM workspace_projects WHERE project_name = ?", (SMOKE_DISCOVERY_PROJECT,))
    conn.commit()
    conn.close()
    if SMOKE_DISCOVERY_PATH.exists():
        shutil.rmtree(SMOKE_DISCOVERY_PATH, ignore_errors=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cleanup()
    client = TestClient(app.app)
    results: dict[str, dict] = {}
    original_morning_time = "09:00"

    def record(name: str, ok: bool, **extra) -> None:
        results[name] = {"ok": ok, **extra}

    try:
        root = client.get("/")
        record("root", root.status_code == 200, status=root.status_code)
        root_text = root.text
        record(
            "subpath_safe",
            "fetch('/api/" not in root_text
            and "fetch(`/api/" not in root_text
            and 'href="/static/' not in root_text,
        )

        today = client.get("/api/today")
        today_json = today.json()
        today_stats = today_json.get("stats") or {}
        today_brief = today_json.get("brief") or {}
        today_morning = today_json.get("morning") or {}
        record(
            "today",
            today.status_code == 200
            and "monitor" in today_stats
            and "next_actions" in today_brief
            and "objective_title" in today_brief
            and "recommended_queue" in today_brief
            and "today_goal" in today_morning
            and "weekly" in today_morning,
            status=today.status_code,
            stats=today_stats,
            brief_headline=today_brief.get("headline", ""),
        )

        ops_brief = client.get("/api/ops-brief")
        record(
            "ops_brief",
            ops_brief.status_code == 200 and "headline" in ops_brief.json() and "objective_title" in ops_brief.json(),
            status=ops_brief.status_code,
        )

        morning_brief = client.get("/api/morning-brief")
        record(
            "morning_brief",
            morning_brief.status_code == 200
            and "today_goal" in morning_brief.json()
            and "weekly" in morning_brief.json(),
            status=morning_brief.status_code,
        )

        week = client.get("/api/week")
        record("week", week.status_code == 200, status=week.status_code)

        current_morning_time = client.get("/api/settings/morning-brief-time")
        current_morning_json = current_morning_time.json()
        original_morning_time = current_morning_json.get("time", "09:00")
        updated_morning_time = client.post("/api/settings/morning-brief-time", json={"time": "09:15"})
        updated_morning_json = updated_morning_time.json()
        record(
            "morning_time_setting",
            current_morning_time.status_code == 200
            and updated_morning_time.status_code == 200
            and updated_morning_json.get("time") == "09:15",
            get_status=current_morning_time.status_code,
            post_status=updated_morning_time.status_code,
        )

        goal_update = client.post(
            "/api/today-goal",
            json={"date": SMOKE_GOAL_DATE, "use_recommended": True},
        )
        goal_json = goal_update.json()
        goal_state = goal_json.get("today_goal") or {}
        record(
            "today_goal_update",
            goal_update.status_code == 200
            and goal_state.get("confirmed") is True
            and goal_state.get("date") == SMOKE_GOAL_DATE,
            status=goal_update.status_code,
        )

        week_goal_create = client.post(
            "/api/week/goals",
            json={"goal": SMOKE_WEEK_GOAL, "week_start": SMOKE_WEEK_START},
        )
        week_goal_json = week_goal_create.json() if week_goal_create.status_code == 200 else {}
        week_goal_id = week_goal_json.get("id")
        week_goal_patch = (
            client.patch(f"/api/week/goals/{week_goal_id}", json={"done": 1})
            if week_goal_id
            else None
        )
        week_goal_delete = (
            client.delete(f"/api/week/goals/{week_goal_id}")
            if week_goal_id
            else None
        )
        record(
            "weekly_goal_crud",
            week_goal_create.status_code == 200
            and week_goal_patch is not None
            and week_goal_patch.status_code == 200
            and week_goal_delete is not None
            and week_goal_delete.status_code == 200,
            create=week_goal_create.status_code,
            patch=week_goal_patch.status_code if week_goal_patch else None,
            delete=week_goal_delete.status_code if week_goal_delete else None,
        )

        client.get("/api/today")
        SMOKE_DISCOVERY_PATH.mkdir(exist_ok=True)
        app.invalidate_workspace_project_cache()
        discovery_today = client.get("/api/today")
        discovery_tasks = discovery_today.json().get("tasks", [])
        discovery_task = next((task for task in discovery_tasks if task.get("title") == SMOKE_DISCOVERY_TASK), None)
        record(
            "workspace_discovery",
            discovery_today.status_code == 200 and discovery_task is not None,
            status=discovery_today.status_code,
        )

        checkins = client.get("/api/checkins")
        record("checkins", checkins.status_code == 200, status=checkins.status_code, count=len(checkins.json()))

        agent_status = client.get("/api/agent-status")
        record("agent_status", agent_status.status_code == 200, status=agent_status.status_code, count=len(agent_status.json()))

        claude_usage = client.get("/api/claude-usage")
        record(
            "claude_usage",
            claude_usage.status_code == 200 and "month_tokens" in claude_usage.json(),
            status=claude_usage.status_code,
        )

        created = client.post("/api/tasks", json={"title": SMOKE_TASK_TITLE, "estimated_minutes": 15, "priority": 2})
        created_json = created.json()
        patched = client.patch(f"/api/tasks/{created_json['id']}", json={"status": "done"})
        deleted = client.delete(f"/api/tasks/{created_json['id']}")
        record(
            "task_crud",
            created.status_code == 200 and patched.status_code == 200 and deleted.status_code == 200,
            created=created.status_code,
            patched=patched.status_code,
            deleted=deleted.status_code,
        )

        decision_created = client.post("/api/tasks", json={"title": SMOKE_TASK_TITLE, "estimated_minutes": 20, "priority": 2})
        decision_task = decision_created.json()
        hold_res = client.post(f"/api/tasks/{decision_task['id']}/decision", json={"action": "hold"})
        continue_res = client.post(f"/api/tasks/{decision_task['id']}/decision", json={"action": "continue"})
        split_res = client.post(
            f"/api/tasks/{decision_task['id']}/decision",
            json={"action": "split", "split_titles": [title.replace("[smoke-test] ", "") for title in SMOKE_SPLIT_TITLES]},
        )
        carry_res = client.post(f"/api/tasks/{decision_task['id']}/decision", json={"action": "carry"})
        record(
            "task_decision",
            decision_created.status_code == 200
            and hold_res.status_code == 200
            and continue_res.status_code == 200
            and split_res.status_code == 200
            and carry_res.status_code == 200,
            created=decision_created.status_code,
            hold=hold_res.status_code,
            continue_status=continue_res.status_code,
            split=split_res.status_code,
            carry=carry_res.status_code,
        )

        checkin_post = client.post("/api/checkin", json={"note": SMOKE_CHECKIN_NOTE})
        record("checkin_create", checkin_post.status_code == 200, status=checkin_post.status_code)

        agent_post = client.post(
            "/api/agent-status",
            json={"project": SMOKE_PROJECT, "task": "smoke step", "status": "step", "url": ""},
        )
        agent_rows = client.get("/api/agent-status").json()
        record(
            "agent_post",
            agent_post.status_code == 200 and any(row["project"] == SMOKE_PROJECT for row in agent_rows),
            status=agent_post.status_code,
        )

        surrogate_payload = (
            '{"project":"%s","task":"\\\\udcec broken","status":"step","url":""}'
            % SMOKE_SURROGATE_PROJECT
        )
        surrogate_post = client.post(
            "/api/agent-status",
            content=surrogate_payload,
            headers={"Content-Type": "application/json"},
        )
        surrogate_rows = client.get("/api/agent-status").json()
        record(
            "agent_post_surrogate",
            surrogate_post.status_code == 200
            and any(row["project"] == SMOKE_SURROGATE_PROJECT for row in surrogate_rows),
            status=surrogate_post.status_code,
        )

        demo_accounts = client.get("/api/org/demo-accounts")
        demo_login = client.post("/api/org/demo-login", json={"login_id": "ceo", "password": "1111"})
        org_state = client.get("/api/org/state", params={"user_id": 1})
        record(
            "org_demo",
            demo_accounts.status_code == 200 and demo_login.status_code == 200 and org_state.status_code == 200,
            demo_accounts=demo_accounts.status_code,
            demo_login=demo_login.status_code,
            org_state=org_state.status_code,
        )
    finally:
        client.post("/api/settings/morning-brief-time", json={"time": original_morning_time})
        app.invalidate_workspace_project_cache()
        cleanup()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
