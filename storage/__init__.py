from __future__ import annotations

from storage.database import create_session_factory, init_database
from storage.repository import SupportPortalRepository

__all__ = [
    "SupportPortalRepository",
    "create_session_factory",
    "init_database",
    "init_db",
    "is_duplicate",
    "save_event",
]


def init_db(db_path: str) -> None:
    database_url = db_path if "://" in db_path else f"sqlite:///{db_path}"
    init_database(database_url)


def is_duplicate(event_hash: str, db_path: str = "events.db", message_id: str | None = None) -> bool:
    database_url = db_path if "://" in db_path else f"sqlite:///{db_path}"
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        repo = SupportPortalRepository(session)
        return repo.is_duplicate_legacy_event(event_hash=event_hash, message_id=message_id)


def save_event(
    event_hash: str,
    case_number: str | None,
    source_file: str,
    db_path: str = "events.db",
    message_id: str | None = None,
) -> None:
    database_url = db_path if "://" in db_path else f"sqlite:///{db_path}"
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        repo = SupportPortalRepository(session)
        repo.save_legacy_event(
            event_hash=event_hash,
            case_number=case_number,
            source_file=source_file,
            message_id=message_id,
        )
        session.commit()
