from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from storage.database import init_database
from storage.repository import SupportPortalRepository, utcnow


def main() -> int:
    settings = get_settings()
    session_factory = init_database(settings.database_url)
    with session_factory() as session:
        repo = SupportPortalRepository(session)
        summary = repo.notification_status_summary()
        pending_records = repo.list_pending_notification_cases()
        eligible = repo.list_cases_eligible_for_retry(
            now=utcnow(),
            max_attempts=settings.mail_max_retry_attempts,
        )
        failures = repo.recent_notification_failures(limit=10)

        print(f"total_cases: {summary.total_cases}")
        print(f"SENT: {summary.sent_count}")
        print(f"PENDING: {summary.pending_count}")
        print(f"SKIPPED_BASELINE: {summary.skipped_baseline_count}")
        print(f"FAILED: {summary.failed_count}")
        print(f"RETRYING: {summary.retrying_count}")
        print(f"eligible_for_retry: {len(eligible)}")
        print(f"pending_records_listed: {len(pending_records)}")
        print("pending_case_numbers:")
        for record in pending_records:
            print(record.case_number)
        print("recent_failures:")
        for record in failures:
            print(
                {
                    "case_number": record.case_number,
                    "status": record.notification_status,
                    "attempt_count": record.notification_attempt_count,
                    "last_attempt_at": record.notification_last_attempt_at.isoformat() if record.notification_last_attempt_at else None,
                    "last_error": record.notification_last_error,
                    "next_retry_at": record.notification_next_retry_at.isoformat() if record.notification_next_retry_at else None,
                }
            )
        print("cases_eligible_for_retry:")
        for record in eligible:
            print(
                {
                    "case_number": record.case_number,
                    "status": record.notification_status,
                    "attempt_count": record.notification_attempt_count,
                    "next_retry_at": record.notification_next_retry_at.isoformat() if record.notification_next_retry_at else None,
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
