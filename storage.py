import sqlite3
from datetime import datetime
from typing import Optional

def init_db(db_path: str) -> None:
    """
    Initialize SQLite database and create events table if not exists.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            event_hash TEXT,
            message_id TEXT UNIQUE,
            case_number TEXT,
            processed_at TEXT,
            source_file TEXT
        )
    ''')
    # Add message_id column if not exists (for migration)
    try:
        cursor.execute('ALTER TABLE events ADD COLUMN message_id TEXT UNIQUE')
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()

def is_duplicate(event_hash: str, db_path: str = 'events.db', message_id: Optional[str] = None) -> bool:
    """
    Check if event_hash or message_id already exists in database.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if message_id:
        cursor.execute('SELECT 1 FROM events WHERE message_id = ?', (message_id,))
    else:
        cursor.execute('SELECT 1 FROM events WHERE event_hash = ?', (event_hash,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_event(event_hash: str, case_number: Optional[str], source_file: str, db_path: str = 'events.db', message_id: Optional[str] = None) -> None:
    """
    Save processed event to database.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    processed_at = datetime.now().isoformat()
    cursor.execute('''
        INSERT INTO events (event_hash, message_id, case_number, processed_at, source_file)
        VALUES (?, ?, ?, ?, ?)
    ''', (event_hash, message_id, case_number, processed_at, source_file))
    conn.commit()
    conn.close()