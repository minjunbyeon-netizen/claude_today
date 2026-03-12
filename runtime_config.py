"""Shared runtime configuration for local Daily Focus scripts."""

from __future__ import annotations

import os

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8001
DEFAULT_BROWSER_HOST = "localhost"
DEFAULT_SCHEME = "http"
DEFAULT_OPEN_BROWSER = True


def _coerce_port(raw_value: str | None) -> int:
    try:
        port = int(raw_value or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def get_app_host() -> str:
    return os.environ.get("APP_HOST", DEFAULT_HOST)


def get_app_port() -> int:
    return _coerce_port(os.environ.get("APP_PORT"))


def get_browser_host() -> str:
    return os.environ.get("APP_BROWSER_HOST", DEFAULT_BROWSER_HOST)


def get_scheme() -> str:
    return os.environ.get("APP_SCHEME", DEFAULT_SCHEME)


def get_base_url() -> str:
    explicit = os.environ.get("APP_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"{get_scheme()}://{get_browser_host()}:{get_app_port()}"


def should_open_browser() -> bool:
    raw = os.environ.get("APP_OPEN_BROWSER")
    if raw is None:
        return DEFAULT_OPEN_BROWSER
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}
