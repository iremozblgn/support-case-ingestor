from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from storage.models import CaseRecord, LegacyEventRecord, NotificationLog


NOTIFICATION_STATUS_PENDING = "PENDING"
NOTIFICATION_STATUS_SENDING = "SENDING"
NOTIFICATION_STATUS_SENT = "SENT"
NOTIFICATION_STATUS_FAILED = "FAILED"
NOTIFICATION_STATUS_RETRYING = "RETRYING"
NOTIFICATION_STATUS_SKIPPED_EXISTING = "SKIPPED_EXISTING"
NOTIFICATION_STATUS_SKIPPED_FILTERED = "SKIPPED_FILTERED"
NOTIFICATION_STATUS_SKIPPED_BASELINE = "SKIPPED_BASELINE"

RETRYABLE_NOTIFICATION_STATUSES = {
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENDING,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_RETRYING,
}


@dataclass(slots=True)
class NotificationStatusSummary:
    total_cases: int
    sent_count: int
    pending_count: int
    failed_count: int
    retrying_count: int
    skipped_baseline_count: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SupportPortalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_case(self, case_id: str | None, case_number: str | None) -> CaseRecord | None:
        filters = []
        if case_id:
            filters.append(CaseRecord.case_id == case_id)
        if case_number:
            filters.append(CaseRecord.case_number == case_number)
        if not filters:
            return None
        stmt = select(CaseRecord).where(or_(*filters))
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert_case(self, case_payload: dict[str, Any], event_hash: str) -> tuple[CaseRecord, bool]:
        record = self.get_case(case_payload.get("case_id"), case_payload.get("case_number"))
        is_new = record is None
        now = utcnow()
        raw_json = json.dumps(case_payload.get("raw", {}), ensure_ascii=False)

        if record is None:
            record = CaseRecord(
                case_id=case_payload.get("case_id"),
                case_number=case_payload.get("case_number"),
                first_seen_at=now,
                notification_status=NOTIFICATION_STATUS_PENDING,
                notification_attempt_count=0,
            )
            self.session.add(record)

        record.case_id = case_payload.get("case_id")
        record.case_number = case_payload.get("case_number")
        record.subject = case_payload.get("subject")
        record.description = case_payload.get("description")
        record.status = case_payload.get("status")
        record.priority = case_payload.get("priority")
        record.product_name = case_payload.get("product_name")
        record.created_date = case_payload.get("created_date")
        record.closed_date = case_payload.get("closed_date")
        record.created_by = case_payload.get("created_by")
        record.account = case_payload.get("account")
        record.contact = case_payload.get("contact")
        record.record_type = case_payload.get("record_type")
        record.owner_id = case_payload.get("owner_id")
        record.case_owner = case_payload.get("case_owner")
        record.action_required = case_payload.get("action_required")
        record.is_partner_customer = case_payload.get("is_partner_customer")
        record.raw_json = raw_json
        record.last_seen_at = now
        record.event_hash = event_hash
        if record.notification_status is None:
            self.sync_case_notification_state_from_logs(record)
        return record, is_new

    def log_notification(
        self,
        *,
        case_id: str | None,
        case_number: str | None,
        email: str | None,
        status: str,
        attempt_key: str | None = None,
        message_id: str | None = None,
        error_message: str | None = None,
    ) -> NotificationLog:
        timestamp = utcnow()
        log = NotificationLog(
            case_id=case_id,
            case_number=case_number,
            channel="email",
            recipient=email,
            email=email,
            attempt_key=attempt_key,
            message_id=message_id,
            status=status,
            error=error_message,
            error_message=error_message,
            sent_at=timestamp,
            created_at=timestamp,
        )
        self.session.add(log)
        return log

    def has_sent_notification(
        self,
        *,
        case_id: str | None,
        case_number: str | None,
        email: str | None,
        attempt_key: str | None = None,
    ) -> bool:
        filters = []
        if case_id:
            filters.append(NotificationLog.case_id == case_id)
        if case_number:
            filters.append(NotificationLog.case_number == case_number)
        if not filters:
            return False

        conditions = [or_(*filters), NotificationLog.status.in_(("sent", "SENT"))]
        if email:
            conditions.append(NotificationLog.email == email)
        if attempt_key:
            conditions.append(
                or_(
                    NotificationLog.attempt_key == attempt_key,
                    NotificationLog.attempt_key.is_(None),
                )
            )
        stmt = select(NotificationLog).where(*conditions)
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def has_sent_attempt_key(self, attempt_key: str | None) -> bool:
        if not attempt_key:
            return False
        stmt = select(NotificationLog).where(
            NotificationLog.attempt_key == attempt_key,
            NotificationLog.status.in_(("sent", "SENT")),
        )
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def sync_case_notification_state_from_logs(self, record: CaseRecord) -> CaseRecord:
        if record.notification_status and record.notification_status != NOTIFICATION_STATUS_SENDING:
            return record
        if self.has_sent_notification(
            case_id=record.case_id,
            case_number=record.case_number,
            email=None,
        ):
            record.notification_status = NOTIFICATION_STATUS_SENT
            if record.notification_sent_at is None:
                record.notification_sent_at = utcnow()
            record.notification_attempt_key = None
            record.notification_attempt_started_at = None
        elif record.notification_status == NOTIFICATION_STATUS_SENDING:
            record.notification_status = NOTIFICATION_STATUS_RETRYING
            if not record.notification_last_error:
                record.notification_last_error = "Recovered interrupted SENDING state"
        return record

    def mark_notification_pending(self, record: CaseRecord) -> CaseRecord:
        if record.notification_status == NOTIFICATION_STATUS_SENT:
            return record
        record.notification_status = NOTIFICATION_STATUS_PENDING
        if record.notification_attempt_count is None:
            record.notification_attempt_count = 0
        return record

    def mark_notification_filtered(self, record: CaseRecord, reason: str) -> CaseRecord:
        record.notification_status = NOTIFICATION_STATUS_SKIPPED_FILTERED
        record.notification_last_error = reason
        record.notification_next_retry_at = None
        record.notification_attempt_key = None
        record.notification_attempt_started_at = None
        return record

    def mark_notification_skipped_existing(self, record: CaseRecord) -> CaseRecord:
        if record.notification_status != NOTIFICATION_STATUS_SENT:
            record.notification_status = NOTIFICATION_STATUS_SKIPPED_EXISTING
        return record

    def mark_notification_skipped_baseline(self, record: CaseRecord, *, reason: str) -> CaseRecord:
        if record.notification_status != NOTIFICATION_STATUS_PENDING:
            return record
        record.notification_status = NOTIFICATION_STATUS_SKIPPED_BASELINE
        record.notification_last_error = reason
        record.notification_next_retry_at = None
        record.notification_attempt_started_at = None
        record.notification_attempt_key = None
        return record

    def begin_notification_attempt(self, record: CaseRecord) -> CaseRecord:
        attempt_key = record.notification_attempt_key or self._build_notification_attempt_key(record)
        record.notification_status = NOTIFICATION_STATUS_SENDING
        record.notification_attempt_count = int(record.notification_attempt_count or 0) + 1
        record.notification_last_attempt_at = utcnow()
        record.notification_attempt_started_at = record.notification_last_attempt_at
        record.notification_attempt_key = attempt_key
        return record

    def mark_notification_sent(self, record: CaseRecord) -> CaseRecord:
        record.notification_status = NOTIFICATION_STATUS_SENT
        record.notification_sent_at = utcnow()
        record.notification_last_error = None
        record.notification_next_retry_at = None
        record.notification_attempt_started_at = None
        record.notification_attempt_key = None
        return record

    def mark_notification_failed(
        self,
        record: CaseRecord,
        *,
        error_message: str,
        next_retry_at: datetime | None,
    ) -> CaseRecord:
        record.notification_status = NOTIFICATION_STATUS_FAILED
        record.notification_last_error = error_message
        record.notification_next_retry_at = next_retry_at
        record.notification_attempt_started_at = None
        return record

    def mark_notification_retrying(self, record: CaseRecord) -> CaseRecord:
        record.notification_status = NOTIFICATION_STATUS_RETRYING
        return record

    def is_notification_retry_eligible(self, record: CaseRecord, *, now: datetime, max_attempts: int) -> bool:
        if (record.notification_status or "") not in RETRYABLE_NOTIFICATION_STATUSES:
            return False
        if int(record.notification_attempt_count or 0) >= max_attempts:
            return False
        if record.notification_status == NOTIFICATION_STATUS_SENDING and record.notification_attempt_started_at:
            return True
        if record.notification_next_retry_at and record.notification_next_retry_at > now:
            return False
        return True

    def list_cases_eligible_for_retry(self, *, now: datetime, max_attempts: int) -> list[CaseRecord]:
        stmt = (
            select(CaseRecord)
            .where(
                CaseRecord.notification_status.in_(tuple(RETRYABLE_NOTIFICATION_STATUSES)),
                (CaseRecord.notification_attempt_count.is_(None))
                | (CaseRecord.notification_attempt_count < max_attempts),
                (CaseRecord.notification_next_retry_at.is_(None))
                | (CaseRecord.notification_next_retry_at <= now),
            )
            .order_by(CaseRecord.notification_next_retry_at.asc().nullsfirst(), CaseRecord.last_seen_at.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_cases_by_notification_status(self, status: str) -> list[CaseRecord]:
        stmt = (
            select(CaseRecord)
            .where(CaseRecord.notification_status == status)
            .order_by(CaseRecord.case_number.asc().nullslast(), CaseRecord.id.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_pending_notification_cases(self) -> list[CaseRecord]:
        return self.list_cases_by_notification_status(NOTIFICATION_STATUS_PENDING)

    def baseline_pending_notifications(self, *, reason: str) -> list[CaseRecord]:
        records = self.list_pending_notification_cases()
        for record in records:
            self.mark_notification_skipped_baseline(record, reason=reason)
        return records

    def recent_notification_failures(self, *, limit: int = 10) -> list[CaseRecord]:
        stmt = (
            select(CaseRecord)
            .where(CaseRecord.notification_status == NOTIFICATION_STATUS_FAILED)
            .order_by(CaseRecord.notification_last_attempt_at.desc().nullslast(), CaseRecord.last_seen_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def notification_status_summary(self) -> NotificationStatusSummary:
        total_cases = int(self.session.execute(select(func.count()).select_from(CaseRecord)).scalar_one() or 0)
        counts_stmt = select(CaseRecord.notification_status, func.count()).group_by(CaseRecord.notification_status)
        rows = self.session.execute(counts_stmt).all()
        counts = {status or "": count for status, count in rows}
        return NotificationStatusSummary(
            total_cases=total_cases,
            sent_count=int(counts.get(NOTIFICATION_STATUS_SENT, 0)),
            pending_count=int(counts.get(NOTIFICATION_STATUS_PENDING, 0)),
            failed_count=int(counts.get(NOTIFICATION_STATUS_FAILED, 0)),
            retrying_count=int(counts.get(NOTIFICATION_STATUS_RETRYING, 0)),
            skipped_baseline_count=int(counts.get(NOTIFICATION_STATUS_SKIPPED_BASELINE, 0)),
        )

    def case_record_to_payload(self, record: CaseRecord) -> dict[str, Any]:
        raw_payload: dict[str, Any] = {}
        if record.raw_json:
            try:
                raw_payload = json.loads(record.raw_json)
            except json.JSONDecodeError:
                raw_payload = {}
        return {
            "case_id": record.case_id,
            "case_number": record.case_number,
            "subject": record.subject,
            "description": record.description,
            "status": record.status,
            "priority": record.priority,
            "product_name": record.product_name,
            "created_date": record.created_date,
            "closed_date": record.closed_date,
            "created_by": record.created_by,
            "account": record.account,
            "contact": record.contact,
            "record_type": record.record_type,
            "owner_id": record.owner_id,
            "case_owner": record.case_owner,
            "action_required": record.action_required,
            "is_partner_customer": record.is_partner_customer,
            "raw": raw_payload,
        }

    def build_message_id(self, record: CaseRecord, email: str | None) -> str:
        stable_case_key = record.case_id or record.case_number or str(record.id)
        stable_attempt_key = record.notification_attempt_key or self._build_notification_attempt_key(record)
        recipient = (email or "unknown").replace("@", "_at_")
        return f"<support_portal-{stable_case_key}-{stable_attempt_key}-{recipient}@local.support_portal.ingestor>"

    def _build_notification_attempt_key(self, record: CaseRecord) -> str:
        stable_case_key = record.case_id or record.case_number or str(record.id or uuid.uuid4())
        return f"{stable_case_key}-{uuid.uuid4().hex[:12]}"

    def is_duplicate_legacy_event(self, event_hash: str | None = None, message_id: str | None = None) -> bool:
        stmt = select(LegacyEventRecord)
        if message_id:
            stmt = stmt.where(LegacyEventRecord.message_id == message_id)
        elif event_hash:
            stmt = stmt.where(LegacyEventRecord.event_hash == event_hash)
        else:
            return False
        return self.session.execute(stmt).scalar_one_or_none() is not None

    def save_legacy_event(
        self,
        *,
        event_hash: str | None,
        case_number: str | None,
        source_file: str | None,
        message_id: str | None = None,
    ) -> LegacyEventRecord:
        record = LegacyEventRecord(
            event_hash=event_hash,
            case_number=case_number,
            source_file=source_file,
            message_id=message_id,
        )
        self.session.add(record)
        return record
