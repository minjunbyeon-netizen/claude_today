"""
daily-focus watchdog — 60초마다 서버 상태 확인, 죽으면 자동 재시작
실행: python watchdog.py (백그라운드 headless로 Task Scheduler가 관리)
"""
import os
import sys
import time
import socket
import subprocess
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("APP_PORT", "8001"))
CHECK_INTERVAL = 60  # seconds
LOG_FILE = os.path.join(ROOT, "log", "watchdog.log")


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def is_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def restart() -> None:
    NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    DETACHED  = getattr(subprocess, "DETACHED_PROCESS", 8)
    PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

    # kill existing
    result = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, creationflags=NO_WINDOW
    )
    for line in result.stdout.splitlines():
        if f":{PORT}" in line and "LISTEN" in line:
            pid = line.strip().split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, creationflags=NO_WINDOW)

    time.sleep(1)

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("APP_OPEN_BROWSER", "0")
    subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        creationflags=NO_WINDOW | DETACHED,
        close_fds=True,
    )


if __name__ == "__main__":
    log("watchdog started")
    while True:
        if not is_alive(PORT):
            log(f"port {PORT} not responding — restarting server")
            restart()
            time.sleep(5)
            if is_alive(PORT):
                log("server restarted successfully")
            else:
                log("restart attempt may have failed — will retry next cycle")
        time.sleep(CHECK_INTERVAL)
