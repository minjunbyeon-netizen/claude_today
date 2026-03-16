"""Headless server restart — no terminal window. Run with: python restart_server.py"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PORT = int(os.environ.get("APP_PORT", "8001"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 8)


def kill_port(port: int) -> None:
    result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if f":{port}" in line and "LISTEN" in line:
            pid = line.strip().split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)


def start_server() -> None:
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
    kill_port(PORT)
    time.sleep(1)
    start_server()
    print(f"Server restarted on port {PORT} (headless)")
