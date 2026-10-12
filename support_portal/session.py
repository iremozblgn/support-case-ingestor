from __future__ import annotations

from typing import Any

import requests


def mask_secret(value: str | None, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def build_support_portal_headers(base_url: str, cookie: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://support.example.com",
        "Referer": base_url,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def create_support_portal_session(base_url: str, cookie: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(build_support_portal_headers(base_url=base_url, cookie=cookie))
    return session


def sanitize_headers(headers: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(headers)
    if "Cookie" in sanitized:
        sanitized["Cookie"] = mask_secret(str(sanitized["Cookie"]))
    return sanitized
