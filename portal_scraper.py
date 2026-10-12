import json
import os
import sqlite3
from typing import Any

from config import CASES_URL, PORTAL_URL
from mailer import send_case_email
from utils import clean_text


CASES_DB_PATH = "cases.db"


def debug_browser():
    from playwright.sync_api import sync_playwright
    import time

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=300,
            args=["--start-maximized"],
        )

        context = browser.new_context(no_viewport=True)
        context.set_default_navigation_timeout(120000)
        page = context.new_page()

        try:
            print("🌐 Opening URL:", PORTAL_URL)
            try:
                page.goto(
                    PORTAL_URL,
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception:
                print("⚠️ Goto failed, retrying with commit...")
                page.goto(PORTAL_URL, timeout=0, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            print("✅ Page loaded")
            print("After goto URL:", page.url)
            print("🔗 Current URL:", page.url)
            print("⏳ Waiting for manual inspection...")
            time.sleep(60)
        except Exception as e:
            print("❌ ERROR during navigation:", str(e))
            time.sleep(60)

        input("🛑 Press ENTER to close browser...")


def _safe_nested_value(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalize_response_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    if "data" in payload and isinstance(payload.get("data"), dict):
        return payload

    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        return None

    first_action = actions[0]
    if not isinstance(first_action, dict):
        return None

    data = _safe_nested_value(first_action, ["returnValue", "data"])
    if isinstance(data, dict):
        return {"data": data}

    return None


def find_case_response(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    best_response = None
    best_edge_count = 0

    for index, response in enumerate(responses, start=1):
        try:
            data = response.get("data", {})
            if (
                "uiapi" in data
                and "query" in data["uiapi"]
                and "Case" in data["uiapi"]["query"]
                and data["uiapi"]["query"]["Case"]["edges"]
            ):
                edges = data["uiapi"]["query"]["Case"]["edges"]
                if not isinstance(edges, list) or not edges:
                    continue

                print(f"Found {len(edges)} cases in response {index}")
                if len(edges) > best_edge_count:
                    best_edge_count = len(edges)
                    best_response = response
            else:
                result = response.get("result")
                if result == []:
                    continue
        except Exception as e:
            print(f"Response selection error in response {index}: {e}")

    if best_response is None:
        print("No valid case data found")
        return None

    print("Selected this response for extraction")
    return best_response


def extract_cases(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []

    for edge in edges:
        node = edge.get("node", {}) if isinstance(edge, dict) else {}
        case = {
            "case_id": node.get("Id"),
            "case_number": _safe_nested_value(node, ["CaseNumber", "value"]),
            "subject": _safe_nested_value(node, ["Subject", "value"]),
            "created_by": _safe_nested_value(node, ["CreatedBy", "Name", "value"]),
            "account": _safe_nested_value(node, ["Account", "Name", "value"]),
            "description": _safe_nested_value(node, ["Description", "value"]),
        }

        if not case["case_number"]:
            continue

        cases.append(case)

    return cases


def init_cases_db(db_path: str = CASES_DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cases(
                case_number TEXT PRIMARY KEY,
                case_id TEXT,
                subject TEXT,
                created_by TEXT,
                account TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_cases_to_db(cases: list[dict[str, Any]], db_path: str = CASES_DB_PATH) -> tuple[int, int]:
    init_cases_db(db_path)
    inserted = 0
    duplicates = 0

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        for case in cases:
            case_number = case.get("case_number")
            if not case_number:
                continue

            try:
                cursor.execute(
                    """
                    INSERT INTO cases (
                        case_number, case_id, subject, created_by, account, description
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_number,
                        case.get("case_id"),
                        case.get("subject"),
                        case.get("created_by"),
                        case.get("account"),
                        case.get("description"),
                    ),
                )
                case["is_new"] = True
                inserted += 1
                print(f"[NEW] {case_number} | Subject: {case.get('subject') or ''}")
            except sqlite3.IntegrityError:
                case["is_new"] = False
                duplicates += 1
                print(f"[DUPLICATE] {case_number} skipped")

        conn.commit()
    finally:
        conn.close()

    print("total cases parsed:", len(cases))
    print("new cases inserted:", inserted)
    print("duplicates skipped:", duplicates)
    return inserted, duplicates


def scrape_portal(portal_url, username, password, headless=False):
    from playwright.sync_api import sync_playwright

    if not portal_url:
        raise ValueError("PORTAL_URL is required")
    if not CASES_URL:
        raise ValueError("CASES_URL is required")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=300,
            args=["--start-maximized"],
        )

        auth_path = "auth.json"
        context_kwargs = {"no_viewport": True}
        if os.path.exists(auth_path):
            context_kwargs["storage_state"] = auth_path

        context = browser.new_context(**context_kwargs)
        context.set_default_navigation_timeout(120000)
        page = context.new_page()
        captured_json_responses: list[dict[str, Any]] = []
        network_events: list[dict[str, Any]] = []

        def handle_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                try:
                    data = response.body()
                except Exception:
                    return

                text = data.decode("utf-8", errors="ignore")
                network_events.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "body_preview": text[:500],
                    }
                )

                try:
                    payload = json.loads(text)
                except Exception:
                    return

                normalized = _normalize_response_payload(payload)
                if normalized is None:
                    return

                data_section = normalized.get("data", {})
                if not isinstance(data_section, dict):
                    return

                if (
                    "uiapi" in data_section
                    and "Case" in text
                    and "edges" in text
                ):
                    captured_json_responses.append(normalized)
            except Exception as e:
                print("Response error:", str(e))

        page.on("response", handle_response)

        try:
            print("🌐 Opening login page:", portal_url)
            try:
                page.goto(
                    portal_url,
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception:
                print("⚠️ Goto failed, retrying with commit...")
                page.goto(portal_url, timeout=0, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            print("After goto URL:", page.url)

            input("Login ol ve ENTER bas")
            print("Login done, current URL:", page.url)
            context.storage_state(path="auth.json")
            print("Session saved")

            context.close()
            context = browser.new_context(storage_state="auth.json")
            context.set_default_navigation_timeout(120000)
            page = context.new_page()
            page.on("response", handle_response)

            if not page.url.startswith("https://support.example.com/s/"):
                page.goto(
                    "https://support.example.com/s/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(5000)

            print("➡️ Navigating to cases page")
            page.goto(
                "https://support.example.com/s/case/Case/Default",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(10000)

            best_response = find_case_response(captured_json_responses)
            if best_response is None:
                return []

            edges = _safe_nested_value(best_response, ["data", "uiapi", "query", "Case", "edges"])
            if not isinstance(edges, list) or not edges:
                print("No valid case data found")
                return []

            cases = extract_cases(edges)
            print("total extracted cases:", len(cases))
            if cases:
                print("first extracted case:", cases[0])
                for case in cases:
                    print(
                        f"CaseNumber: {case.get('case_number')} | "
                        f"Subject: {case.get('subject')} | "
                        f"Account: {case.get('account')}"
                    )

            save_cases_to_db(cases)
            new_cases = [case for case in cases if case.get("is_new") is True]
            print(f"New cases found: {len(new_cases)}")

            for case in cases:
                if case.get("is_new"):
                    print(f"🆕 New case found: {case.get('case_number')}")
                else:
                    print(f"ℹ️ Skipped (not new): {case.get('case_number')}")

            for case in new_cases:
                case["description"] = clean_text(case.get("description"))
                case["subject"] = clean_text(case.get("subject"))
                try:
                    send_case_email(case)
                    print(f"📧 Email sent for case {case.get('case_number')}")
                except Exception as e:
                    print(f"❌ Email failed for case {case.get('case_number')}: {str(e)}")

            print("captured network events:", len(network_events))
            return cases
        finally:
            browser.close()
