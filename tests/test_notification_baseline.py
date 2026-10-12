from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage.database import init_database
from storage.models import CaseRecord
from storage.repository import (
    SupportPortalRepository,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENT,
    NOTIFICATION_STATUS_SKIPPED_BASELINE,
    utcnow,
)


BASELINE_REASON = "Baselined without sending before production enablement"


class NotificationBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "baseline.db"
        self.session_factory = init_database(f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_case(self, session, *, case_number: str, notification_status: str) -> CaseRecord:
        record = CaseRecord(
            case_id=case_number,
            case_number=case_number,
            notification_status=notification_status,
            notification_attempt_count=1,
        )
        session.add(record)
        session.flush()
        return record

    def test_dry_run_listing_does_not_modify_db(self) -> None:
        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            self._seed_case(session, case_number="1001", notification_status=NOTIFICATION_STATUS_PENDING)
            session.commit()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            pending = repo.list_cases_by_notification_status(NOTIFICATION_STATUS_PENDING)
            self.assertEqual(len(pending), 1)
            session.rollback()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            pending = repo.list_cases_by_notification_status(NOTIFICATION_STATUS_PENDING)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].notification_status, NOTIFICATION_STATUS_PENDING)

    def test_inspect_style_and_baseline_style_pending_queries_match(self) -> None:
        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            self._seed_case(session, case_number="1001", notification_status=NOTIFICATION_STATUS_PENDING)
            self._seed_case(session, case_number="1002", notification_status=NOTIFICATION_STATUS_PENDING)
            self._seed_case(session, case_number="1003", notification_status=NOTIFICATION_STATUS_SENT)
            session.commit()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            summary = repo.notification_status_summary()
            pending_records = repo.list_pending_notification_cases()
            self.assertEqual(summary.pending_count, len(pending_records))
            self.assertEqual([record.case_number for record in pending_records], ["1001", "1002"])

    def test_apply_marks_only_pending(self) -> None:
        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            pending = self._seed_case(session, case_number="1001", notification_status=NOTIFICATION_STATUS_PENDING)
            sent = self._seed_case(session, case_number="1002", notification_status=NOTIFICATION_STATUS_SENT)
            failed = self._seed_case(session, case_number="1003", notification_status=NOTIFICATION_STATUS_FAILED)
            session.commit()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            updated = repo.baseline_pending_notifications(reason=BASELINE_REASON)
            session.commit()
            self.assertEqual([record.case_number for record in updated], ["1001"])

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            pending_record = repo.get_case("1001", "1001")
            sent_record = repo.get_case("1002", "1002")
            failed_record = repo.get_case("1003", "1003")
            assert pending_record is not None
            assert sent_record is not None
            assert failed_record is not None
            self.assertEqual(pending_record.notification_status, NOTIFICATION_STATUS_SKIPPED_BASELINE)
            self.assertEqual(pending_record.notification_last_error, BASELINE_REASON)
            self.assertEqual(sent_record.notification_status, NOTIFICATION_STATUS_SENT)
            self.assertEqual(failed_record.notification_status, NOTIFICATION_STATUS_FAILED)

    def test_baselined_records_are_not_eligible_for_retry(self) -> None:
        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            self._seed_case(session, case_number="1001", notification_status=NOTIFICATION_STATUS_PENDING)
            session.commit()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            repo.baseline_pending_notifications(reason=BASELINE_REASON)
            session.commit()

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            eligible = repo.list_cases_eligible_for_retry(now=utcnow(), max_attempts=3)
            self.assertEqual(eligible, [])
            summary = repo.notification_status_summary()
            self.assertEqual(summary.pending_count, 0)
            self.assertEqual(summary.skipped_baseline_count, 1)


if __name__ == "__main__":
    unittest.main()
