from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


VOLATILE_HEADERS = {
    "content-length",
    "host",
}

COOKIE_HEADER = "cookie"
STRICT_RUNTIME_COOKIE_NAMES = {
    "sid",
    "sid_client",
    "__secure-has-sid",
}


def parse_cookie_header(raw_cookie: str | None) -> dict[str, str]:
    if not raw_cookie:
        return {}

    cookies: dict[str, str] = {}
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies[name] = value.strip()
    return cookies


def format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name)


def _normalize_headers(raw_headers: Any) -> tuple[dict[str, str], list[str]]:
    if isinstance(raw_headers, dict):
        return (
            {str(name): str(value) for name, value in raw_headers.items()},
            [str(name) for name in raw_headers],
        )

    headers: dict[str, str] = {}
    header_order: list[str] = []
    if isinstance(raw_headers, list):
        for entry in raw_headers:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("key") or "").strip()
            value = str(entry.get("value") or "")
            if not name:
                continue
            headers[name] = value
            header_order.append(name)
    return headers, header_order


def _normalize_body_fields(raw_body: str | None, parsed_fields: Any = None) -> tuple[list[tuple[str, str]], str]:
    if isinstance(parsed_fields, dict):
        fields = [(str(name), str(value)) for name, value in parsed_fields.items()]
        return fields, urlencode(fields)
    if isinstance(parsed_fields, list):
        fields: list[tuple[str, str]] = []
        for entry in parsed_fields:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                fields.append((str(entry[0]), str(entry[1])))
            elif isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("key") or "").strip()
                if name:
                    fields.append((name, str(entry.get("value") or "")))
        return fields, urlencode(fields)

    body_text = raw_body or ""
    return [(name, value) for name, value in parse_qsl(body_text, keep_blank_values=True)], body_text


@dataclass(slots=True)
class BrowserRequestSnapshot:
    method: str
    url: str
    headers: dict[str, str]
    header_order: list[str]
    query_params: list[tuple[str, str]]
    body_fields: list[tuple[str, str]]
    raw_body: str
    cookies: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def lower_headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}


@dataclass(slots=True)
class BrowserParityDiff:
    parity_score: int
    missing_headers: list[str]
    changed_headers: dict[str, dict[str, str]]
    changed_query_params: dict[str, dict[str, str]]
    changed_cookies: dict[str, dict[str, str]]
    body_length_change: dict[str, int]
    changed_graphql_variables: dict[str, dict[str, Any]]
    changed_body_fields: dict[str, dict[str, str]]


@dataclass(slots=True)
class BrowserParityRequest:
    method: str
    url: str
    headers: dict[str, str]
    body_fields: list[tuple[str, str]]
    diff: BrowserParityDiff | None

    @property
    def encoded_body(self) -> str:
        return urlencode(self.body_fields)


def normalize_browser_request_snapshot(raw_payload: dict[str, Any]) -> BrowserRequestSnapshot:
    if "snapshot" in raw_payload and isinstance(raw_payload["snapshot"], dict):
        raw_payload = raw_payload["snapshot"]

    url = str(raw_payload.get("url") or raw_payload.get("request_url") or "").strip()
    if not url:
        raise ValueError("Browser request snapshot is missing a URL")

    method = str(raw_payload.get("method") or "POST").upper()
    headers, header_order = _normalize_headers(raw_payload.get("headers") or {})
    if isinstance(raw_payload.get("header_order"), list):
        configured_order = [str(name) for name in raw_payload["header_order"] if str(name)]
        if configured_order:
            header_order = configured_order
    raw_cookie = headers.get("Cookie") or headers.get("cookie") or raw_payload.get("cookie") or ""
    cookies = parse_cookie_header(raw_cookie)

    split_url = urlsplit(url)
    if isinstance(raw_payload.get("query_params"), list):
        query_params = [
            (str(entry[0]), str(entry[1]))
            for entry in raw_payload["query_params"]
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]
    else:
        query_params = [(name, value) for name, value in parse_qsl(split_url.query, keep_blank_values=True)]

    body_fields, raw_body = _normalize_body_fields(
        raw_payload.get("body") or raw_payload.get("postData") or raw_payload.get("raw_body"),
        raw_payload.get("body_fields"),
    )

    metadata = raw_payload.get("metadata") if isinstance(raw_payload.get("metadata"), dict) else {}
    return BrowserRequestSnapshot(
        method=method,
        url=urlunsplit((split_url.scheme, split_url.netloc, split_url.path, "", split_url.fragment)),
        headers=headers,
        header_order=header_order,
        query_params=query_params,
        body_fields=body_fields,
        raw_body=raw_body,
        cookies=cookies,
        metadata=metadata,
    )


def load_browser_request_snapshot(path: str | Path) -> BrowserRequestSnapshot | None:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return None

    raw_text = snapshot_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return None

    snapshot_json = json.loads(raw_text)
    return normalize_browser_request_snapshot(snapshot_json)


def _message_to_variables(raw_message: str | None) -> dict[str, Any]:
    if not raw_message:
        return {}
    try:
        message_json = json.loads(raw_message)
    except json.JSONDecodeError:
        return {}

    query_input = (
        message_json.get("actions", [{}])[0]
        .get("params", {})
        .get("queryInput", {})
    )
    variables = query_input.get("variables")
    return variables if isinstance(variables, dict) else {}


def _fields_to_dict(body_fields: list[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in body_fields}


class BrowserParityRequestBuilder:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.snapshot = load_browser_request_snapshot(settings.support_portal_browser_request_snapshot_path)

    def build_request(
        self,
        *,
        generated_url: str,
        session_headers: dict[str, str],
        form_payload: dict[str, str],
    ) -> BrowserParityRequest:
        if self.snapshot is None:
            return BrowserParityRequest(
                method="POST",
                url=generated_url,
                headers=dict(session_headers),
                body_fields=list(form_payload.items()),
                diff=None,
            )

        snapshot = self.snapshot
        snapshot_headers = snapshot.headers
        snapshot_headers_lower = snapshot.lower_headers
        session_headers_lower = {name.lower(): value for name, value in session_headers.items()}
        outgoing_headers: dict[str, str] = {}

        for header_name in snapshot.header_order or snapshot_headers.keys():
            lowered = header_name.lower()
            if lowered in VOLATILE_HEADERS:
                continue
            if lowered == COOKIE_HEADER:
                continue
            if header_name in snapshot_headers:
                outgoing_headers[header_name] = snapshot_headers[header_name]

        for header_name, header_value in session_headers.items():
            if header_name.lower() in VOLATILE_HEADERS:
                continue
            if header_name.lower() in snapshot_headers_lower:
                continue
            if header_name not in outgoing_headers:
                outgoing_headers[header_name] = header_value

        runtime_cookie_header = session_headers_lower.get(COOKIE_HEADER) or self.settings.support_portal_cookie or ""
        runtime_cookies = parse_cookie_header(runtime_cookie_header)
        merged_cookies = dict(snapshot.cookies)
        if self.settings.browser_parity_strict:
            for cookie_name, cookie_value in runtime_cookies.items():
                if cookie_name.lower() in STRICT_RUNTIME_COOKIE_NAMES:
                    merged_cookies[cookie_name] = cookie_value
        else:
            merged_cookies.update(runtime_cookies)
        if merged_cookies:
            cookie_key = next(
                (name for name in snapshot_headers if name.lower() == COOKIE_HEADER),
                "Cookie",
            )
            outgoing_headers[cookie_key] = format_cookie_header(merged_cookies)

        outgoing_body_fields = self._merge_body_fields(snapshot=snapshot, form_payload=form_payload)
        outgoing_url = self._merge_url(snapshot=snapshot, generated_url=generated_url)
        diff = self.diff_request(
            snapshot=snapshot,
            outgoing_url=outgoing_url,
            outgoing_headers=outgoing_headers,
            outgoing_body_fields=outgoing_body_fields,
        )
        return BrowserParityRequest(
            method=snapshot.method or "POST",
            url=outgoing_url,
            headers=outgoing_headers,
            body_fields=outgoing_body_fields,
            diff=diff,
        )

    def _merge_url(self, *, snapshot: BrowserRequestSnapshot, generated_url: str) -> str:
        if self.settings.browser_parity_strict:
            split_url = urlsplit(snapshot.url)
            query_string = urlencode(snapshot.query_params)
            return urlunsplit((split_url.scheme, split_url.netloc, split_url.path, query_string, split_url.fragment))
        return generated_url

    def _merge_body_fields(
        self,
        *,
        snapshot: BrowserRequestSnapshot,
        form_payload: dict[str, str],
    ) -> list[tuple[str, str]]:
        captured_fields = _fields_to_dict(snapshot.body_fields)
        merged_fields: list[tuple[str, str]] = []
        seen_names: set[str] = set()

        for name, captured_value in snapshot.body_fields:
            merged_value = form_payload.get(name, captured_value)
            merged_fields.append((name, merged_value))
            seen_names.add(name)

        for name, value in form_payload.items():
            if name not in seen_names:
                merged_fields.append((name, value))

        if self.settings.browser_parity_strict:
            return merged_fields

        body_dict = dict(captured_fields)
        body_dict.update(form_payload)
        return list(body_dict.items())

    def diff_request(
        self,
        *,
        snapshot: BrowserRequestSnapshot,
        outgoing_url: str,
        outgoing_headers: dict[str, str],
        outgoing_body_fields: list[tuple[str, str]],
    ) -> BrowserParityDiff:
        snapshot_headers = snapshot.lower_headers
        outgoing_headers_lower = {name.lower(): value for name, value in outgoing_headers.items()}
        missing_headers = [
            name
            for name in snapshot_headers
            if name not in outgoing_headers_lower and name not in VOLATILE_HEADERS
        ]
        changed_headers = {
            name: {
                "captured": snapshot_headers[name],
                "outgoing": outgoing_headers_lower[name],
            }
            for name in snapshot_headers
            if name in outgoing_headers_lower
            and snapshot_headers[name] != outgoing_headers_lower[name]
            and name not in VOLATILE_HEADERS
        }

        outgoing_query = {
            name: value
            for name, value in parse_qsl(urlsplit(outgoing_url).query, keep_blank_values=True)
        }
        snapshot_query = {name: value for name, value in snapshot.query_params}
        changed_query_params = {
            name: {
                "captured": snapshot_query.get(name, ""),
                "outgoing": outgoing_query.get(name, ""),
            }
            for name in sorted(set(snapshot_query) | set(outgoing_query))
            if snapshot_query.get(name) != outgoing_query.get(name)
        }

        outgoing_cookies = parse_cookie_header(outgoing_headers.get("Cookie") or outgoing_headers.get("cookie"))
        changed_cookies = {
            name: {
                "captured": snapshot.cookies.get(name, ""),
                "outgoing": outgoing_cookies.get(name, ""),
            }
            for name in sorted(set(snapshot.cookies) | set(outgoing_cookies))
            if snapshot.cookies.get(name) != outgoing_cookies.get(name)
        }

        outgoing_fields = _fields_to_dict(outgoing_body_fields)
        snapshot_fields = _fields_to_dict(snapshot.body_fields)
        changed_body_fields = {
            name: {
                "captured": snapshot_fields.get(name, ""),
                "outgoing": outgoing_fields.get(name, ""),
            }
            for name in sorted(set(snapshot_fields) | set(outgoing_fields))
            if snapshot_fields.get(name) != outgoing_fields.get(name)
        }

        snapshot_variables = _message_to_variables(snapshot_fields.get("message"))
        outgoing_variables = _message_to_variables(outgoing_fields.get("message"))
        changed_graphql_variables = {
            name: {
                "captured": snapshot_variables.get(name),
                "outgoing": outgoing_variables.get(name),
            }
            for name in sorted(set(snapshot_variables) | set(outgoing_variables))
            if snapshot_variables.get(name) != outgoing_variables.get(name)
        }

        checks = [
            not missing_headers,
            not changed_headers,
            not changed_query_params,
            not changed_cookies,
            not changed_body_fields,
            not changed_graphql_variables,
        ]
        parity_score = int((sum(1 for check in checks if check) / len(checks)) * 100)
        return BrowserParityDiff(
            parity_score=parity_score,
            missing_headers=missing_headers,
            changed_headers=changed_headers,
            changed_query_params=changed_query_params,
            changed_cookies=changed_cookies,
            body_length_change={
                "captured": len(snapshot.raw_body),
                "outgoing": len(urlencode(outgoing_body_fields)),
            },
            changed_graphql_variables=changed_graphql_variables,
            changed_body_fields=changed_body_fields,
        )
