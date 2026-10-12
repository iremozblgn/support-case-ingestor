from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_manager import close_context, get_browser_entrypoint_diagnostics
from support_portal.browser_scraper import SupportPortalBrowserScrapeError, fetch_cases_via_browser_detailed


def _safe_case_rows_visible(diagnostics: dict[str, object], parsed_case_count: int) -> bool:
    return bool(
        diagnostics.get("visible_case_number_count")
        or diagnostics.get("visible_case_numbers_found")
        or diagnostics.get("unique_cases_after_dedup")
        or parsed_case_count > 0
    )


def _launch_once(settings, *, wait_for_operator: bool) -> dict[str, object]:
    result = fetch_cases_via_browser_detailed(settings)
    diagnostics = result.diagnostics or {}
    return {
        "profile_path": str(settings.resolved_browser_profile_dir),
        "login_attempted": bool(diagnostics.get("login_attempted")),
        "session_reused": bool(diagnostics.get("session_reused")),
        "authenticated": bool(diagnostics.get("final_authenticated")),
        "public_shell_detected": bool(diagnostics.get("public_shell_detected")),
        "auth_stage_history": diagnostics.get("auth_stage_history", []),
        "real_case_rows_visible": _safe_case_rows_visible(diagnostics, len(result.cases)),
        "auth_state_flush_completed": False,
        "context_close_completed": False,
        "parsed_case_count": len(result.cases),
        "diagnostics": diagnostics,
    }


def main() -> int:
    settings = get_settings()
    browser_entrypoint = get_browser_entrypoint_diagnostics(settings)
    print(f"canonical_profile_path: {browser_entrypoint.get('resolved_browser_profile_dir')}")

    report: dict[str, object] = {
        "canonical_profile_path": browser_entrypoint.get("resolved_browser_profile_dir"),
        "first_launch_profile_path": "",
        "second_launch_profile_path": "",
        "same_profile_path": False,
        "first_run_login_attempted": False,
        "first_run_authenticated": False,
        "first_run_real_case_rows_visible": False,
        "first_run_auth_stage_history": [],
        "first_run_auth_state_flush_completed": False,
        "first_run_context_close_completed": False,
        "second_run_session_reused": False,
        "second_run_login_attempted": False,
        "second_run_authenticated": False,
        "second_run_public_shell_detected": False,
        "second_run_real_case_rows_visible": False,
        "second_run_auth_stage_history": [],
        "session_persistence_result": "failed",
        "session_persistence_failure_reason": "",
    }

    try:
        first = _launch_once(settings, wait_for_operator=True)
        report["first_launch_profile_path"] = first["profile_path"]
        report["first_run_login_attempted"] = first["login_attempted"]
        report["first_run_authenticated"] = first["authenticated"]
        report["first_run_real_case_rows_visible"] = first["real_case_rows_visible"]
        report["first_run_auth_stage_history"] = first["auth_stage_history"]
    except SupportPortalBrowserScrapeError as exc:
        report["session_persistence_failure_reason"] = f"first_launch_failed:{getattr(exc, 'category', 'browser_error')}"
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        close_context(reason="session_persistence_first_close")
        report["first_run_auth_state_flush_completed"] = True
        report["first_run_context_close_completed"] = True

    time.sleep(2)

    try:
        second = _launch_once(settings, wait_for_operator=False)
        report["second_launch_profile_path"] = second["profile_path"]
        report["same_profile_path"] = report["first_launch_profile_path"] == report["second_launch_profile_path"]
        report["second_run_session_reused"] = second["session_reused"]
        report["second_run_login_attempted"] = second["login_attempted"]
        report["second_run_authenticated"] = second["authenticated"]
        report["second_run_public_shell_detected"] = second["public_shell_detected"]
        report["second_run_real_case_rows_visible"] = second["real_case_rows_visible"]
        report["second_run_auth_stage_history"] = second["auth_stage_history"]
    except SupportPortalBrowserScrapeError as exc:
        report["second_launch_profile_path"] = str(settings.resolved_browser_profile_dir)
        report["same_profile_path"] = report["first_launch_profile_path"] == report["second_launch_profile_path"]
        report["session_persistence_failure_reason"] = f"second_launch_failed:{getattr(exc, 'category', 'browser_error')}"
        print(json.dumps(report, ensure_ascii=False))
        return 1
    finally:
        close_context(reason="session_persistence_second_close")

    if (
        report["same_profile_path"]
        and report["first_run_authenticated"]
        and report["first_run_real_case_rows_visible"]
        and report["first_run_context_close_completed"]
        and report["second_run_session_reused"]
        and not report["second_run_login_attempted"]
        and report["second_run_authenticated"]
        and report["second_run_real_case_rows_visible"]
    ):
        report["session_persistence_result"] = "passed"
    else:
        report["session_persistence_failure_reason"] = report["session_persistence_failure_reason"] or "session_not_reused"

    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["session_persistence_result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
