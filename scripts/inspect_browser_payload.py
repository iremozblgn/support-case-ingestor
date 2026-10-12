from __future__ import annotations

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings


def main() -> int:
    settings = get_settings()
    path = pathlib.Path(settings.support_portal_browser_parity_message_path)
    if not path.exists():
        raise SystemExit(f"Payload file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    payload_size = len(raw.encode("utf-8"))
    payload_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    truncated = not raw.strip().endswith(("}", "]"))

    print(f"payload_file: {path}")
    print(f"payload_size_bytes: {payload_size}")
    print(f"payload_sha256: {payload_hash}")
    print(f"payload_truncated_guess: {truncated}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"json_valid: false")
        print(f"json_error: {exc}")
        return 1

    print("json_valid: true")
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        print("actions_present: false")
        return 1

    action = actions[0]
    params = action.get("params", {}) if isinstance(action, dict) else {}
    query_input = params.get("queryInput", {}) if isinstance(params, dict) else {}
    variables = query_input.get("variables", {}) if isinstance(query_input, dict) else {}

    print(f"descriptor: {action.get('descriptor')}")
    print(f"callingDescriptor: {action.get('callingDescriptor')}")
    print(f"query_length: {len(query_input.get('query', ''))}")
    print("variables:")
    print(json.dumps(variables, indent=2, ensure_ascii=False))
    print("whereClause:")
    print(json.dumps(variables.get("where") or variables.get("whereClause"), indent=2, ensure_ascii=False))
    print("orderBy:")
    print(json.dumps(variables.get("orderBy"), indent=2, ensure_ascii=False))
    print("pretty_payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
