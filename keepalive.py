"""Keep Daily Focus running in the background on Windows."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import ctypes
from pathlib import Path

from runtime_config import get_base_url

CHECK_INTERVAL_SECONDS = 30
STARTUP_GRACE_SECONDS = 12


def acquire_singleton() -> object | None:
    if os.name != "nt":
        return object()
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\DailyFocusKeepalive")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return None
    return handle


def app_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{get_base_url()}/health", timeout=4) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False
    except Exception:
        return False


def launch_stack() -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("APP_OPEN_BROWSER", "0")
    root = Path(__file__).resolve().parent
    subprocess.Popen(
        [sys.executable, str(root / "run.py")],
        cwd=str(root),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main() -> None:
    mutex = acquire_singleton()
    if mutex is None:
        return

    last_launch_at = 0.0
    while True:
        healthy = app_is_healthy()
        now = time.time()
        if not healthy and now - last_launch_at >= STARTUP_GRACE_SECONDS:
            launch_stack()
            last_launch_at = now
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
