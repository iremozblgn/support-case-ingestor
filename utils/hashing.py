from __future__ import annotations

import hashlib
from typing import Any


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).replace("\n", " ").replace("\t", " ").split()).strip()


def generate_case_event_hash(case: dict[str, Any]) -> str:
    case_id = clean_text(case.get("case_id"))
    case_number = clean_text(case.get("case_number"))
    subject = clean_text(case.get("subject"))
    status = clean_text(case.get("status"))
    last_modified = clean_text(
        case.get("last_modified_date")
        or case.get("updated_date")
        or case.get("created_date")
    )
    unique_string = "|".join([case_id, case_number, subject, status, last_modified])
    return hashlib.sha256(unique_string.encode("utf-8")).hexdigest()


def generate_event_hash(case: dict[str, Any]) -> str:
    return generate_case_event_hash(case)
