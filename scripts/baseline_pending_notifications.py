from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from storage.database import init_database
from storage.repository import (
    SupportPortalRepository,
    NOTIFICATION_STATUS_SKIPPED_BASELINE,
    utcnow,
)


BASELINE_REASON = "Baselined without sending before production enablement"


def _print_records(records: list[object]) -> None:
    for record in records:
        print(
            {
                "case_number": getattr(record, "case_number", None),
                "status": getattr(record, "notification_status", None),
                "attempt_count": getattr(record, "notification_attempt_count", None),
                "last_error": getattr(record, "notification_last_error", None),
                "next_retry_at": (
                    getattr(record, "notification_next_retry_at", None).isoformat()
                    if getattr(record, "notification_next_retry_at", None)
                    else None
                ),
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baseline existing pending notifications so they will never be sent or retried."
    )
    parser.add_argument("--apply", action="store_true", help="Persist the baseline status changes.")
    args = parser.parse_args()

    settings = get_settings()
    session_factory = init_database(settings.database_url)

    with session_factory() as session:
        repo = SupportPortalRepository(session)
        pending_records = repo.list_pending_notification_cases()
        before_summary = repo.notification_status_summary()
        before_eligible = repo.list_cases_eligible_for_retry(
            now=utcnow(),
            max_attempts=settings.mail_max_retry_attempts,
        )
        status_counts_before = {
            "SENT": before_summary.sent_count,
            "PENDING": before_summary.pending_count,
            "SKIPPED_BASELINE": before_summary.skipped_baseline_count,
            "FAILED": before_summary.failed_count,
            "RETRYING": before_summary.retrying_count,
        }

        print(f"mode: {'APPLY' if args.apply else 'DRY_RUN'}")
        print(f"database_path: {settings.database_url}")
        print("pending_query_source: SupportPortalRepository.list_pending_notification_cases")
        print(f"baseline_status: {NOTIFICATION_STATUS_SKIPPED_BASELINE}")
        print(f"baseline_reason: {BASELINE_REASON}")
        print(f"total_pending_count: {len(pending_records)}")
        print(f"pending_summary_count: {before_summary.pending_count}")
        print(f"pending_rows_found: {len(pending_records)}")
        print(f"status_counts_before: {status_counts_before}")
        print(
            "pending_case_numbers: "
            + str([record.case_number for record in pending_records])
        )
        print("current_notification_statuses:")
        _print_records(pending_records)
        print(f"eligible_for_retry_before: {len(before_eligible)}")

        if not args.apply:
            session.rollback()
            print("db_changes_applied: False")
            return 0

        updated_records = repo.baseline_pending_notifications(reason=BASELINE_REASON)
        session.commit()

        after_summary = repo.notification_status_summary()
        after_eligible = repo.list_cases_eligible_for_retry(
            now=utcnow(),
            max_attempts=settings.mail_max_retry_attempts,
        )

        print("db_changes_applied: True")
        print(f"baselined_count: {len(updated_records)}")
        print(f"pending_after: {after_summary.pending_count}")
        print(f"{NOTIFICATION_STATUS_SKIPPED_BASELINE}: {after_summary.skipped_baseline_count}")
        print(f"eligible_for_retry_after: {len(after_eligible)}")
        print(f"sent_preserved: {after_summary.sent_count == before_summary.sent_count}")
        print(f"failed_preserved: {after_summary.failed_count == before_summary.failed_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
