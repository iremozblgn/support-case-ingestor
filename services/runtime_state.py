from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class RuntimeSnapshot:
    status: str
    last_successful_request_at: str | None
    consecutive_failures: int
    session_invalid_since: str | None
    last_http_status: int | None
    last_aura_error: str | None
    uptime_seconds: int
    successful_cycles: int
    failed_cycles: int
    auth_failures: int
    retries_attempted: int


class RuntimeHealthState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._last_successful_request_at: str | None = None
        self._consecutive_failures = 0
        self._session_invalid_since: str | None = None
        self._last_http_status: int | None = None
        self._last_aura_error: str | None = None
        self._successful_cycles = 0
        self._failed_cycles = 0
        self._auth_failures = 0
        self._retries_attempted = 0

    def record_retry(self) -> None:
        with self._lock:
            self._retries_attempted += 1

    def mark_request_success(self, *, http_status: int | None) -> tuple[bool, bool]:
        with self._lock:
            recovered_from_invalid = self._session_invalid_since is not None
            recovered_from_degraded = self._consecutive_failures > 0
            self._last_successful_request_at = utcnow_iso()
            self._consecutive_failures = 0
            self._session_invalid_since = None
            self._last_http_status = http_status
            self._last_aura_error = None
            return recovered_from_invalid, recovered_from_degraded

    def mark_request_failure(
        self,
        *,
        http_status: int | None,
        aura_error: str | None,
        session_invalid: bool,
    ) -> tuple[bool, bool]:
        with self._lock:
            became_invalid = False
            entered_degraded = self._consecutive_failures == 0
            self._consecutive_failures += 1
            self._last_http_status = http_status
            self._last_aura_error = aura_error
            if session_invalid:
                self._auth_failures += 1
                if self._session_invalid_since is None:
                    self._session_invalid_since = utcnow_iso()
                    became_invalid = True
            return became_invalid, entered_degraded

    def mark_cycle_result(self, *, success: bool) -> None:
        with self._lock:
            if success:
                self._successful_cycles += 1
            else:
                self._failed_cycles += 1

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            if self._session_invalid_since:
                status = "invalid_session"
            elif self._consecutive_failures > 0:
                status = "degraded"
            else:
                status = "healthy"
            return RuntimeSnapshot(
                status=status,
                last_successful_request_at=self._last_successful_request_at,
                consecutive_failures=self._consecutive_failures,
                session_invalid_since=self._session_invalid_since,
                last_http_status=self._last_http_status,
                last_aura_error=self._last_aura_error,
                uptime_seconds=int(time.monotonic() - self._started_monotonic),
                successful_cycles=self._successful_cycles,
                failed_cycles=self._failed_cycles,
                auth_failures=self._auth_failures,
                retries_attempted=self._retries_attempted,
            )

    def as_health_payload(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "status": snapshot.status,
            "last_successful_request_at": snapshot.last_successful_request_at,
            "consecutive_failures": snapshot.consecutive_failures,
            "uptime_seconds": snapshot.uptime_seconds,
            "session_invalid_since": snapshot.session_invalid_since,
            "last_http_status": snapshot.last_http_status,
            "last_aura_error": snapshot.last_aura_error,
            "successful_cycles": snapshot.successful_cycles,
            "failed_cycles": snapshot.failed_cycles,
            "auth_failures": snapshot.auth_failures,
            "retries_attempted": snapshot.retries_attempted,
        }
