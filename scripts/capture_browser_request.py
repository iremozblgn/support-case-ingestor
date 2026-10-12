from __future__ import annotations

import json
import pathlib
import sys
from urllib.parse import parse_qsl
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from support_portal.browser_parity import normalize_browser_request_snapshot


def _decode_ansi_c_quoted(value: str) -> str:
    decoded_chars: list[str] = []
    index = 0
    length = len(value)
    escapes = {
        "\\": "\\",
        "'": "'",
        '"': '"',
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    while index < length:
        char = value[index]
        if char != "\\":
            decoded_chars.append(char)
            index += 1
            continue

        index += 1
        if index >= length:
            decoded_chars.append("\\")
            break

        escape_char = value[index]
        if escape_char == "x" and index + 2 < length:
            hex_value = value[index + 1:index + 3]
            try:
                decoded_chars.append(chr(int(hex_value, 16)))
                index += 3
                continue
            except ValueError:
                decoded_chars.append("\\x")
                index += 1
                continue
        if escape_char == "u" and index + 4 < length:
            hex_value = value[index + 1:index + 5]
            try:
                decoded_chars.append(chr(int(hex_value, 16)))
                index += 5
                continue
            except ValueError:
                decoded_chars.append("\\u")
                index += 1
                continue
        if escape_char in escapes:
            decoded_chars.append(escapes[escape_char])
        else:
            decoded_chars.append(escape_char)
        index += 1
    return "".join(decoded_chars)


def _join_line_continuations(raw_text: str) -> str:
    lines = raw_text.splitlines()
    joined_parts: list[str] = []
    pending = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        joined_parts.append(pending + stripped)
        pending = ""
    if pending:
        joined_parts.append(pending)
    return "\n".join(part for part in joined_parts if part.strip())


def _tokenize_curl_command(raw_text: str) -> list[str]:
    text = _join_line_continuations(raw_text).strip()
    tokens: list[str] = []
    length = len(text)
    index = 0

    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break

        if text[index] == "$" and index + 1 < length and text[index + 1] == "'":
            index += 2
            chunk: list[str] = []
            while index < length:
                char = text[index]
                if char == "'":
                    index += 1
                    break
                if char == "\\" and index + 1 < length:
                    chunk.append(char)
                    index += 1
                    chunk.append(text[index])
                    index += 1
                    continue
                chunk.append(char)
                index += 1
            tokens.append(_decode_ansi_c_quoted("".join(chunk)))
            continue

        if text[index] in {"'", '"'}:
            quote = text[index]
            index += 1
            chunk: list[str] = []
            while index < length:
                char = text[index]
                if char == quote:
                    index += 1
                    break
                if quote == '"' and char == "\\" and index + 1 < length:
                    index += 1
                    chunk.append(text[index])
                    index += 1
                    continue
                chunk.append(char)
                index += 1
            tokens.append("".join(chunk))
            continue

        chunk = []
        while index < length and not text[index].isspace():
            char = text[index]
            if char == "\\" and index + 1 < length:
                index += 1
                chunk.append(text[index])
                index += 1
                continue
            chunk.append(char)
            index += 1
        tokens.append("".join(chunk))

    return tokens


def _parse_curl_command(raw_text: str) -> dict[str, Any]:
    tokens = _tokenize_curl_command(raw_text)
    if not tokens or tokens[0] != "curl":
        raise ValueError("Input does not look like a curl command")

    url = ""
    method = "POST"
    headers: dict[str, str] = {}
    raw_body = ""
    detected_data_raw = False

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-H", "--header"}:
            index += 1
            if index >= len(tokens):
                break
            header = tokens[index]
            if ":" in header:
                name, value = header.split(":", 1)
                headers[name.strip()] = value.strip()
        elif token in {"-b", "--cookie"}:
            index += 1
            if index >= len(tokens):
                break
            headers["Cookie"] = tokens[index]
        elif token in {"--data-raw", "--data", "--data-binary", "--data-ascii", "--data-urlencode"}:
            index += 1
            if index >= len(tokens):
                break
            raw_body = tokens[index]
            detected_data_raw = token == "--data-raw"
            method = "POST"
        elif token in {"-X", "--request"}:
            index += 1
            if index >= len(tokens):
                break
            method = tokens[index].upper()
        elif token.startswith("http://") or token.startswith("https://"):
            url = token
        index += 1

    if not url:
        raise ValueError("Could not find request URL in curl command")

    body_fields = list(parse_qsl(raw_body, keep_blank_values=True))
    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": raw_body,
        "body_fields": body_fields,
        "metadata": {
            "parser_mode_used": "curl_bash",
            "detected_data_raw": detected_data_raw,
            "raw_body_length": len(raw_body),
            "parsed_form_field_names": [name for name, _ in body_fields],
        },
    }


def _parse_raw_http_request(raw_text: str) -> dict[str, Any]:
    lines = raw_text.splitlines()
    if not lines:
        raise ValueError("Request text is empty")

    request_line = lines[0].strip()
    parts = request_line.split()
    if len(parts) < 2:
        raise ValueError("First line does not look like an HTTP request line")
    method, path = parts[0], parts[1]

    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines[1:]:
        if in_body:
            body_lines.append(line)
            continue
        if not line.strip():
            in_body = True
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip()] = value.strip()

    origin = headers.get("Origin", "")
    host = headers.get("Host", "")
    if path.startswith("http"):
        url = path
    else:
        scheme = "https"
        base = origin or (f"{scheme}://{host}" if host else "")
        url = f"{base}{path}"

    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": "\n".join(body_lines).strip(),
        "metadata": {
            "parser_mode_used": "raw_http",
            "detected_data_raw": False,
            "raw_body_length": len("\n".join(body_lines).strip()),
            "parsed_form_field_names": [
                name for name, _ in parse_qsl("\n".join(body_lines).strip(), keep_blank_values=True)
            ],
        },
    }


def _load_request_input(path: pathlib.Path) -> dict[str, Any]:
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"Input file is empty: {path}")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        stripped = raw_text.lstrip()
        if stripped.startswith("curl "):
            return _parse_curl_command(raw_text)
        return _parse_raw_http_request(raw_text)

    if isinstance(parsed, dict):
        parsed.setdefault("metadata", {})
        parsed["metadata"].setdefault("parser_mode_used", "json")
        body_text = str(parsed.get("body") or parsed.get("postData") or parsed.get("raw_body") or "")
        parsed["metadata"].setdefault("detected_data_raw", False)
        parsed["metadata"].setdefault("raw_body_length", len(body_text))
        parsed["metadata"].setdefault(
            "parsed_form_field_names",
            [name for name, _ in parse_qsl(body_text, keep_blank_values=True)],
        )
        return parsed
    raise ValueError("Input JSON must be an object")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    input_path = pathlib.Path(argv[0]) if argv else pathlib.Path("debug") / "browser_request_input.json"
    output_path = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path("debug") / "browser_request_full.json"

    raw_payload = _load_request_input(input_path)
    snapshot = normalize_browser_request_snapshot(raw_payload)
    normalized = {
        "snapshot": {
            "method": snapshot.method,
            "url": snapshot.url,
            "headers": snapshot.headers,
            "header_order": snapshot.header_order,
            "query_params": snapshot.query_params,
            "body_fields": snapshot.body_fields,
            "raw_body": snapshot.raw_body,
            "metadata": snapshot.metadata,
        }
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"saved_snapshot: {output_path}")
    print(f"method: {snapshot.method}")
    print(f"url: {snapshot.url}")
    print(f"headers: {len(snapshot.headers)}")
    print(f"query_params: {len(snapshot.query_params)}")
    print(f"body_fields: {len(snapshot.body_fields)}")
    print(f"detected_data_raw: {bool(snapshot.metadata.get('detected_data_raw'))}")
    print(f"raw_body_length: {snapshot.metadata.get('raw_body_length', len(snapshot.raw_body))}")
    print(f"parser_mode_used: {snapshot.metadata.get('parser_mode_used', 'unknown')}")
    print("parsed_form_field_names:")
    for field_name in snapshot.metadata.get("parsed_form_field_names", []):
        print(f"- {field_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
