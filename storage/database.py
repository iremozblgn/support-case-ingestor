from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from storage.models import Base


def create_db_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_db_engine(database_url)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def init_database(database_url: str) -> sessionmaker[Session]:
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    _ensure_case_columns(engine)
    _ensure_notification_logs_columns(engine)
    _backfill_notification_state(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _ensure_case_columns(engine) -> None:
    inspector = inspect(engine)
    if "cases" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("cases")}
    statements: list[str] = []

    if "notification_status" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_status VARCHAR(32)")
    if "notification_attempt_count" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_attempt_count INTEGER DEFAULT 0")
    if "notification_last_attempt_at" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_last_attempt_at DATETIME")
    if "notification_attempt_started_at" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_attempt_started_at DATETIME")
    if "notification_attempt_key" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_attempt_key VARCHAR(128)")
    if "notification_sent_at" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_sent_at DATETIME")
    if "notification_last_error" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_last_error TEXT")
    if "notification_next_retry_at" not in columns:
        statements.append("ALTER TABLE cases ADD COLUMN notification_next_retry_at DATETIME")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_notification_logs_columns(engine) -> None:
    inspector = inspect(engine)
    if "notification_logs" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("notification_logs")}
    statements: list[str] = []

    if "email" not in columns:
        statements.append("ALTER TABLE notification_logs ADD COLUMN email VARCHAR(255)")
    if "attempt_key" not in columns:
        statements.append("ALTER TABLE notification_logs ADD COLUMN attempt_key VARCHAR(128)")
    if "message_id" not in columns:
        statements.append("ALTER TABLE notification_logs ADD COLUMN message_id VARCHAR(255)")
    if "error_message" not in columns:
        statements.append("ALTER TABLE notification_logs ADD COLUMN error_message TEXT")
    if "created_at" not in columns:
        statements.append("ALTER TABLE notification_logs ADD COLUMN created_at DATETIME")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _backfill_notification_state(engine) -> None:
    inspector = inspect(engine)
    if "cases" not in inspector.get_table_names():
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE cases
                SET notification_attempt_count = COALESCE(notification_attempt_count, 0)
                WHERE notification_attempt_count IS NULL
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE cases
                SET notification_status = 'RETRYING',
                    notification_last_error = COALESCE(notification_last_error, 'Recovered stale SENDING state')
                WHERE notification_status = 'SENDING'
                """
            )
        )
        if "notification_logs" in inspector.get_table_names():
            connection.execute(
                text(
                    """
                    UPDATE cases
                    SET
                      notification_status = 'SENT',
                      notification_sent_at = COALESCE(
                        notification_sent_at,
                        (
                          SELECT MAX(notification_logs.sent_at)
                          FROM notification_logs
                          WHERE UPPER(notification_logs.status) = 'SENT'
                            AND (
                              (cases.case_id IS NOT NULL AND notification_logs.case_id = cases.case_id)
                              OR
                              (cases.case_number IS NOT NULL AND notification_logs.case_number = cases.case_number)
                            )
                        )
                      ),
                      notification_last_error = NULL,
                      notification_next_retry_at = NULL
                    WHERE notification_status IS NULL
                      AND EXISTS (
                        SELECT 1
                        FROM notification_logs
                        WHERE UPPER(notification_logs.status) = 'SENT'
                          AND (
                            (cases.case_id IS NOT NULL AND notification_logs.case_id = cases.case_id)
                            OR
                            (cases.case_number IS NOT NULL AND notification_logs.case_number = cases.case_number)
                          )
                      )
                    """
                )
            )

        connection.execute(
            text(
                """
                UPDATE cases
                SET notification_status = 'SKIPPED_EXISTING'
                WHERE notification_status IS NULL
                """
            )
        )
