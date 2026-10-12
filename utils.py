import hashlib


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\n", " ").replace("\t", " ")
    text = " ".join(text.split())
    return text.strip()


def generate_event_hash(case: dict) -> str:
    """
    Generate a stable unique hash for each case to support deduplication.
    """

    case_number = case.get("case_number", "")
    subject = case.get("subject", "")
    created_by = case.get("created_by", "")

    unique_string = f"{case_number}-{subject}-{created_by}"
    return hashlib.md5(unique_string.encode("utf-8")).hexdigest()
