"""Windows notification loop for Daily Focus."""

import time
import urllib.request
import json
from datetime import datetime, timedelta

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

# ── Foundation OS 스케줄러 상태 ───────────────────────────────────
_LAST_BLOCK_NOTIFIED = ""        # 중복 알림 방지
_LAST_AUTO_JUDGE_DATE = ""       # 자동 판정 중복 방지
_LAST_GTREVIEW_DATE = ""         # 구태우 리뷰 중복 방지
_LAST_MONTHLY_DATE = ""          # 월간 회고 중복 방지


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


# ══════════════════════════════════════════════════════════════════
# FOUNDATION OS 웹훅 (Phase 3)
# ══════════════════════════════════════════════════════════════════

def _foundation_notify(title: str, message: str, send_telegram: bool = True) -> None:
    """윈도우 알림 + Telegram 동시 전송."""
    notify_message(title, message)
    if send_telegram:
        try:
            _try_telegram_send(f"[{title}]\n{message}")
        except Exception:
            pass


def _get_stack_status() -> str:
    """스택 현황 한 줄 요약."""
    try:
        url = f"{get_base_url()}/api/foundation/stack-status"
        with urllib.request.urlopen(url, timeout=3) as resp:
            d = json.loads(resp.read())
        stacks = d.get("stacks", [])
        parts = [f"{s['stack']}({s['count']}/{s['limit']})" for s in stacks]
        return " | ".join(parts)
    except Exception:
        return ""


def _get_boot_confirmed() -> bool:
    """오늘 Daily Boot 완료 여부."""
    try:
        url = f"{get_base_url()}/api/daily-boot/today"
        with urllib.request.urlopen(url, timeout=3) as resp:
            d = json.loads(resp.read())
        return bool(d.get("confirmed"))
    except Exception:
        return False


def maybe_foundation_block_notify() -> None:
    """시간 블록 전환 감지 → 알림 발송."""
    global _LAST_BLOCK_NOTIFIED
    now_hm = datetime.now().strftime("%H:%M")

    # 블록별 트리거 시각
    triggers = {
        "09:55": ("Daily Boot 준비", "10:00부터 블록 1 시작. 오늘 Task 1개 + 완료 기준을 확인하세요."),
        "10:00": ("블록 1 시작 — 카카오 OFF", "성장 제품 집중 시간. 13:00까지 메시지 확인 금지."),
        "13:00": ("점심 + 메시지 해제", "블록 1 종료. 오늘 결과물 1개를 기록하세요."),
        "14:00": ("블록 2 시작 — 수익 제품", "클라이언트 납품 집중. 새 개발 시작 금지."),
        "16:00": ("블록 3-A — 내일 도면", "내일 Task 1개 확정 + 완료기준 + 예상막힘을 기록하세요."),
        "17:30": ("블록 3-C — 실험 제품", "블록 1 결과물 있으면 실험 진행 가능. 30분 타이머 시작."),
    }

    key = f"{datetime.now().date().isoformat()}_{now_hm}"
    if _LAST_BLOCK_NOTIFIED == key:
        return

    if now_hm in triggers:
        title, msg = triggers[now_hm]
        stack_info = _get_stack_status()
        if stack_info:
            msg += f"\n스택 현황: {stack_info}"
        _foundation_notify(f"Foundation OS — {title}", msg, send_telegram=True)
        _LAST_BLOCK_NOTIFIED = key


def maybe_run_auto_judge() -> None:
    """매일 17:00 자동 판정 실행."""
    global _LAST_AUTO_JUDGE_DATE
    now_hm = datetime.now().strftime("%H:%M")
    today = datetime.now().date().isoformat()

    if now_hm < "17:00" or _LAST_AUTO_JUDGE_DATE == today:
        return

    try:
        url = f"{get_base_url()}/api/foundation/auto-judge/run"
        req = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())

        summary = d.get("summary", {})
        killed_exp = d.get("experiments_killed", [])
        msg = (
            f"[자동 판정 완료]\n"
            f"FILL {summary.get('fill',0)}개 | KILL {summary.get('kill',0)}개 | CALL {summary.get('call',0)}개"
        )
        if killed_exp:
            names = ", ".join(e["name"] for e in killed_exp)
            msg += f"\n실험 자동 KILL: {names}"
            # #50 — 실험 KILL 즉시 별도 Telegram 알림
            kill_msg = f"[실험 자동 KILL]\n{names}\n7일 데드라인 초과 + 아웃풋 없음으로 종료 처리"
            _try_telegram_send(kill_msg)

        _foundation_notify("Foundation OS — 17:00 자동 판정", msg, send_telegram=True)
        _LAST_AUTO_JUDGE_DATE = today
    except Exception as e:
        print(f"[auto-judge failed] {e}")


def maybe_gtreview_notify() -> None:
    """매주 목요일 16:00 구태우 리뷰 알림."""
    global _LAST_GTREVIEW_DATE
    now = datetime.now()
    today = now.date().isoformat()
    now_hm = now.strftime("%H:%M")

    # 목요일(weekday=3), 16:00
    if now.weekday() != 3 or now_hm < "16:00" or _LAST_GTREVIEW_DATE == today:
        return

    msg = "구태우 리뷰 세션 (30분)\n[10분] 이번 주 만든 것 시연\n[15분] 사용자 관점 피드백\n[5분] 반영/버림 즉시 판정"
    _foundation_notify("Foundation OS — 구태우 리뷰", msg, send_telegram=True)
    _LAST_GTREVIEW_DATE = today


def maybe_monthly_retro_notify() -> None:
    """매월 마지막 금요일 09:00 월간 회고 알림."""
    global _LAST_MONTHLY_DATE
    now = datetime.now()
    today = now.date().isoformat()
    now_hm = now.strftime("%H:%M")

    # 금요일(weekday=4), 09:00, 다음 주도 같은 달이면 아직 마지막 금요일 아님
    if now.weekday() != 4 or now_hm < "09:00" or _LAST_MONTHLY_DATE == today:
        return
    next_friday = now.date() + timedelta(days=7)
    if next_friday.month == now.month:
        return  # 마지막 금요일 아님

    try:
        url = f"{get_base_url()}/api/foundation/auto-judge"
        with urllib.request.urlopen(url, timeout=5) as resp:
            d = json.loads(resp.read())
        kill_count = d.get("summary", {}).get("kill", 0)
        kill_suffix = f"\nKILL 후보 {kill_count}개 — 앱에서 확인하세요" if kill_count else ""
    except Exception:
        kill_suffix = ""

    msg = f"월간 회고 시간 (1시간)\n이번 달 시스템화 목록 확인\n여전히 수작업으로 남은 것 점검\nKILL 확정 + 다음 달 북극성 재확인{kill_suffix}"
    _foundation_notify("Foundation OS — 월간 회고", msg, send_telegram=True)
    _LAST_MONTHLY_DATE = today


def run() -> None:
    for check_time in CHECK_TIMES:
        schedule.every().day.at(check_time).do(notify)
    schedule.every(MONITOR_INTERVAL_MINUTES).minutes.do(monitor_attention)
    schedule.every(1).minutes.do(maybe_notify_morning_brief)
    schedule.every(5).minutes.do(maybe_send_eod_report)
    # ── Foundation OS 스케줄러 (Phase 3) ──────────────────────────
    schedule.every(1).minutes.do(maybe_foundation_block_notify)  # 시간 블록 전환
    schedule.every(1).minutes.do(maybe_run_auto_judge)           # 17:00 자동 판정
    schedule.every(1).minutes.do(maybe_gtreview_notify)          # 목요일 구태우 리뷰
    schedule.every(5).minutes.do(maybe_monthly_retro_notify)     # 마지막 금요일 회고
    print(f"[Notifier running] Check-in times: {', '.join(CHECK_TIMES)}")
    print("[Foundation OS] 시간 블록 웹훅 활성화: 09:55/10:00/13:00/14:00/16:00/17:00/17:30")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run()
