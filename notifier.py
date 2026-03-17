"""Windows notification loop for Daily Focus."""

import time
import urllib.request
import json
from datetime import datetime

import schedule

from runtime_config import get_base_url


def get_morning_summary() -> str:
    """strategic-brief API를 호출해서 오늘 목표 + 지금 할 것 TOP1을 반환."""
    try:
        url = f"{get_base_url()}/api/strategic-brief"
        with urllib.request.urlopen(url, timeout=3) as response:
            d = json.loads(response.read())
        goal = (d.get("today") or {}).get("goal", "")
        now_task = (d.get("recommendation") or {}).get("now") or {}
        title = now_task.get("title", "")
        if goal and title:
            return f"오늘 목표: {goal}\n지금 할 것: {title}"
        if goal:
            return f"오늘 목표: {goal}"
        if title:
            return f"지금 할 것: {title}"
    except Exception:
        pass
    return "오늘 집중 시간입니다"

CHECK_TIMES = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]
MONITOR_INTERVAL_MINUTES = 5
_LAST_MONITOR_SIGNATURE = ""
_LAST_MORNING_BRIEF_DATE = ""


def notify() -> None:
    try:
        from plyer import notification

        notification.notify(
            title="Daily Focus - Check-in time",
            message=f"Two hours passed. Update your work log -> {get_base_url()}",
            app_name="Daily Focus",
            timeout=15,
        )
    except Exception as error:
        print(f"[Notification failed] {error}")


def notify_message(title: str, message: str) -> None:
    try:
        from plyer import notification

        notification.notify(
            title=title,
            message=message,
            app_name="Daily Focus",
            timeout=15,
        )
    except Exception as error:
        print(f"[Notification failed] {error}")


def monitor_attention() -> None:
    global _LAST_MONITOR_SIGNATURE

    try:
        with urllib.request.urlopen(f"{get_base_url()}/api/today", timeout=5) as response:
            payload = json.loads(response.read())
    except Exception as error:
        print(f"[Monitor check failed] {error}")
        return

    tasks = payload.get("tasks", [])
    flagged = [
        task for task in tasks
        if task.get("attention_level") in {"watch", "stale", "critical"}
    ]
    flagged.sort(
        key=lambda task: {
            "critical": 3,
            "stale": 2,
            "watch": 1,
        }.get(task.get("attention_level"), 0),
        reverse=True,
    )
    signature = "|".join(
        f"{task.get('id')}:{task.get('attention_level')}:{task.get('attention_minutes')}"
        for task in flagged[:5]
    )
    if not signature:
        _LAST_MONITOR_SIGNATURE = ""
        return
    if signature == _LAST_MONITOR_SIGNATURE:
        return

    top = flagged[0]
    summary = f"{len(flagged)}개 체크 필요"
    detail = top.get("title", "")
    reason = top.get("attention_reason", "")
    notify_message("Daily Focus - Attention", f"{summary}\n{detail}\n{reason}".strip())
    _LAST_MONITOR_SIGNATURE = signature


def maybe_notify_morning_brief() -> None:
    global _LAST_MORNING_BRIEF_DATE

    try:
        with urllib.request.urlopen(f"{get_base_url()}/api/morning-brief", timeout=5) as response:
            payload = json.loads(response.read())
    except Exception as error:
        print(f"[Morning brief check failed] {error}")
        return

    goal = payload.get("today_goal", {}) or {}
    target_date = goal.get("date") or datetime.now().date().isoformat()
    if target_date != datetime.now().date().isoformat():
        return
    if _LAST_MORNING_BRIEF_DATE == target_date:
        return

    morning_time = str(payload.get("time") or "09:00")
    now_label = datetime.now().strftime("%H:%M")
    if now_label < morning_time:
        return

    weekly = payload.get("weekly", {}) or {}
    effective_goal = goal.get("effective_goal") or goal.get("recommended_goal") or "오늘 목표를 확정하세요"
    # strategic-brief 기반 메시지 우선, 실패 시 기존 방식으로 폴백
    strategic_msg = get_morning_summary()
    if strategic_msg and strategic_msg != "오늘 집중 시간입니다":
        message = strategic_msg
    elif goal.get("confirmed"):
        message = f"오늘 목표: {effective_goal}"
    else:
        message = f"추천 목표: {effective_goal}\n앱에서 오늘 목표를 확정하세요"

    if weekly.get("total_goals"):
        message += f"\n이번 주 목표 {weekly.get('done_goals', 0)}/{weekly.get('total_goals', 0)} 완료"
    elif weekly.get("recommended_week_goal"):
        message += f"\n{weekly.get('recommended_week_goal')}"

    notify_message("Daily Focus - Morning Brief", message.strip())
    # Telegram 전송 시도 (실패해도 무시)
    try:
        _try_telegram_send(message.strip())
    except Exception:
        pass
    _LAST_MORNING_BRIEF_DATE = target_date


def _try_telegram_send(text: str) -> None:
    """Telegram 설정이 있으면 메시지 전송."""
    try:
        url = f"{get_base_url()}/api/settings/telegram"
        with urllib.request.urlopen(url, timeout=3) as resp:
            cfg = json.loads(resp.read())
    except Exception:
        return
    if not (cfg.get("enabled") and cfg.get("bot_token") and cfg.get("chat_id")):
        return
    token = cfg["bot_token"]
    chat_id = cfg["chat_id"]
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception:
        pass


_LAST_EOD_DATE = ""


def maybe_send_eod_report() -> None:
    """18:00 이후 하루 1회 EOD 리포트를 Telegram으로 전송."""
    global _LAST_EOD_DATE
    today = datetime.now().date().isoformat()
    if _LAST_EOD_DATE == today:
        return
    now_label = datetime.now().strftime("%H:%M")
    if now_label < "18:00":
        return
    try:
        url = f"{get_base_url()}/api/eod-report/send"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        _LAST_EOD_DATE = today
    except Exception:
        pass


def run() -> None:
    for check_time in CHECK_TIMES:
        schedule.every().day.at(check_time).do(notify)
    schedule.every(MONITOR_INTERVAL_MINUTES).minutes.do(monitor_attention)
    schedule.every(1).minutes.do(maybe_notify_morning_brief)
    schedule.every(5).minutes.do(maybe_send_eod_report)
    print(f"[Notifier running] Check-in times: {', '.join(CHECK_TIMES)}")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run()
