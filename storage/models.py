from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CaseRecord(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    case_number: Mapped[str | None] = mapped_column(String(64), index=True)
    subject: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(100))
    priority: Mapped[str | None] = mapped_column(String(100))
    product_name: Mapped[str | None] = mapped_column(String(255))
    created_date: Mapped[str | None] = mapped_column(String(100))
    closed_date: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[str | None] = mapped_column(String(255))
    account: Mapped[str | None] = mapped_column(String(255))
    contact: Mapped[str | None] = mapped_column(String(255))
    record_type: Mapped[str | None] = mapped_column(String(255))
    owner_id: Mapped[str | None] = mapped_column(String(100))
    case_owner: Mapped[str | None] = mapped_column(String(255))
    action_required: Mapped[bool | None] = mapped_column(Boolean)
    is_partner_customer: Mapped[bool | None] = mapped_column(Boolean)
    raw_json: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    notification_status: Mapped[str | None] = mapped_column(String(32), index=True)
    notification_attempt_count: Mapped[int | None] = mapped_column(Integer, default=0)
    notification_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_attempt_key: Mapped[str | None] = mapped_column(String(128), index=True)
    notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_last_error: Mapped[str | None] = mapped_column(Text)
    notification_next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(64), index=True)
    case_number: Mapped[str | None] = mapped_column(String(64), index=True)
    channel: Mapped[str | None] = mapped_column(String(50), default="email")
    recipient: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    attempt_key: Mapped[str | None] = mapped_column(String(128), index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(50))
    error: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LegacyEventRecord(Base):
    __tablename__ = "legacy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    case_number: Mapped[str | None] = mapped_column(String(64))
    source_file: Mapped[str | None] = mapped_column(String(500))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
