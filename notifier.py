"""
2시간마다 Windows 토스트 알림을 보내는 스케줄러
체크인 시각: 09:00, 11:00, 13:00, 15:00, 17:00, 19:00
"""
import schedule
import time

CHECK_TIMES = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]


def notify():
    try:
        from plyer import notification
        notification.notify(
            title="Daily Focus - 체크인 시간!",
            message="2시간 경과. 작업 현황을 기록하세요 -> http://localhost:8888",
            app_name="Daily Focus",
            timeout=15,
        )
    except Exception as e:
        print(f"[알림 실패] {e}")


def run():
    for t in CHECK_TIMES:
        schedule.every().day.at(t).do(notify)
    print(f"[알림 스케줄러] 체크인 시각: {', '.join(CHECK_TIMES)}")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run()
