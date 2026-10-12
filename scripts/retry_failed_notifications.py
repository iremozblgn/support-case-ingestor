from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from notifications.mailer import SupportPortalMailer
from storage.database import init_database
from storage.repository import SupportPortalRepository, NOTIFICATION_STATUS_SENT, utcnow


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry failed SupportPortal notifications safely.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually send retries. Without this flag the script runs in dry-run mode.",
    )
    args = parser.parse_args()
    settings = get_settings()
    session_factory = init_database(settings.database_url)
    mailer = SupportPortalMailer(settings)
    with session_factory() as session:
        repo = SupportPortalRepository(session)
        eligible = repo.list_cases_eligible_for_retry(
            now=utcnow(),
            max_attempts=settings.mail_max_retry_attempts,
        )
        print(f"eligible_count: {len(eligible)}")
        for record in eligible:
            repo.sync_case_notification_state_from_logs(record)
            if record.notification_status == NOTIFICATION_STATUS_SENT:
                print(f"skip_sent: {record.case_number}")
                continue
            case = repo.case_record_to_payload(record)
            if not args.apply:
                print(
                    {
                        "case_number": record.case_number,
                        "mode": "dry_run",
                        "current_status": record.notification_status,
                        "attempt_count": record.notification_attempt_count,
                    }
                )
                continue

            repo.mark_notification_retrying(record)
            result = mailer.send_new_case_email(case, repo, record=record)
            print(
                {
                    "case_number": record.case_number,
                    "final_status": result.final_status,
                    "sent": result.sent,
                    "skipped": result.skipped,
                    "retry_scheduled": result.retry_scheduled,
                    "error": result.error_message,
                }
            )
        if args.apply:
            session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
