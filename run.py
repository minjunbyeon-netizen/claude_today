"""Local launcher for Daily Focus."""

import os
import subprocess
import sys
import threading
import time
import webbrowser
import ctypes

from runtime_config import get_app_host, get_app_port, get_base_url, should_open_browser


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONUTF8", "1")
configure_stdio()


def acquire_singleton() -> object | None:
    if os.name != "nt":
        return object()
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\DailyFocusRun")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return None
    return handle


def start_server() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            get_app_host(),
            "--port",
            str(get_app_port()),
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


def start_notifier() -> None:
    import notifier

    notifier.run()


if __name__ == "__main__":
    mutex = acquire_singleton()
    if mutex is None:
        print("Daily Focus launcher already running")
        raise SystemExit(0)

    base_url = get_base_url()
    print(f"Starting Daily Focus on {base_url}")

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    time.sleep(2)
    if should_open_browser():
        webbrowser.open(base_url)
        print(f"Browser opened: {base_url}")
    else:
        print("Browser auto-open skipped")

    start_notifier()
