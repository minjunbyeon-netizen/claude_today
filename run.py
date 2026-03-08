"""
Daily Focus 실행기
- FastAPI 서버 시작 (포트 8001)
- 브라우저 자동 오픈
- 2시간 알림 스케줄러 시작
"""
import subprocess
import sys
import threading
import time
import webbrowser
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def start_server():
    subprocess.run(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"],
        creationflags=0,
    )


def start_notifier():
    import notifier
    notifier.run()


if __name__ == "__main__":
    print("Daily Focus 시작 중...")

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    time.sleep(2)
    webbrowser.open("http://localhost:8001")
    print("브라우저가 열렸습니다. http://localhost:8001")

    start_notifier()
