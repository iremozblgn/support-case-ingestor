from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_manager import close_context, get_browser_entrypoint_diagnostics
from support_portal.browser_scraper import (
    SupportPortalBrowserScrapeError,
    fetch_cases_via_browser_detailed,
)


def main() -> int:
    settings = get_settings()
    browser_entrypoint = get_browser_entrypoint_diagnostics(settings)
    print(f"resolved_browser_profile_dir: {browser_entrypoint.get('resolved_browser_profile_dir')}")
    print(f"persistent_context_used: {browser_entrypoint.get('persistent_context_used')}")
    print(f"browser_executable_name: {browser_entrypoint.get('browser_executable_name')}")
    print(f"browser_headless: {browser_entrypoint.get('browser_headless')}")
    print(f"profile_exists_before_launch: {browser_entrypoint.get('profile_exists_before_launch')}")
    print(f"profile_owner_pid_if_any: {browser_entrypoint.get('profile_owner_pid_if_any')}")
    print(f"profile_lease_path: {browser_entrypoint.get('profile_lease_path')}")
    try:
        result = fetch_cases_via_browser_detailed(settings)
    except SupportPortalBrowserScrapeError as exc:
        print(f"browser_scrape_error: {exc}")
        return 1
    try:
        print(f"current_url: {result.current_url}")
        print(f"page_title: {result.page_title}")
        print(f"login_detected: {result.login_detected}")
        print(f"row_count: {result.row_count}")
        print(f"extraction_method_used: {result.extraction_method_used}")
        print(f"visible_case_numbers: {json.dumps(result.visible_case_numbers[:20], ensure_ascii=False)}")
        if result.diagnostics:
            print(f"session_reused: {result.diagnostics.get('session_reused')}")
            print(f"login_page_detected: {result.diagnostics.get('login_page_detected')}")
            print(f"login_attempted: {result.diagnostics.get('login_attempted')}")
            print(f"login_page_url: {result.diagnostics.get('login_page_url')}")
            print(f"login_page_title: {result.diagnostics.get('login_page_title')}")
            print(f"username_field_found: {result.diagnostics.get('username_field_found')}")
            print(f"password_field_found: {result.diagnostics.get('password_field_found')}")
            print(f"submit_button_found: {result.diagnostics.get('submit_button_found')}")
            print(f"login_submit_clicked: {result.diagnostics.get('login_submit_clicked')}")
            print(f"login_successful: {result.diagnostics.get('login_successful')}")
            print(f"post_login_url: {result.diagnostics.get('post_login_url')}")
            print(f"post_login_title: {result.diagnostics.get('post_login_title')}")
            print(f"login_requires_manual_intervention: {result.diagnostics.get('login_requires_manual_intervention')}")
            print(f"manual_login_used: {result.diagnostics.get('manual_login_used')}")
            print(f"final_authenticated: {result.diagnostics.get('final_authenticated')}")
            print(f"authenticated_via_session: {result.diagnostics.get('authenticated_via_session')}")
            print(f"authenticated_via_visible_cases: {result.diagnostics.get('authenticated_via_visible_cases')}")
            print(f"authenticated_via_url: {result.diagnostics.get('authenticated_via_url')}")
            print(f"authenticated_via_title: {result.diagnostics.get('authenticated_via_title')}")
            print(f"manual_login_reason: {result.diagnostics.get('manual_login_reason')}")
            print(f"auth_check_reason: {result.diagnostics.get('auth_check_reason')}")
            print(f"auto_login_skipped_reason: {result.diagnostics.get('auto_login_skipped_reason')}")
            print(f"auth_confidence_score: {result.diagnostics.get('auth_confidence_score')}")
            print(f"auth_state_confidence: {result.diagnostics.get('auth_state_confidence')}")
            print(
                "auth_decision_path: "
                + json.dumps(result.diagnostics.get("auth_decision_path", []), ensure_ascii=False)
            )
            print(
                f"extraction_succeeded_before_manual_fallback: {result.diagnostics.get('extraction_succeeded_before_manual_fallback')}"
            )
            print(
                f"manual_fallback_suppressed_due_to_authenticated_state: {result.diagnostics.get('manual_fallback_suppressed_due_to_authenticated_state')}"
            )
            print(f"final_url: {result.diagnostics.get('final_url')}")
            print(f"final_page_title: {result.diagnostics.get('final_page_title')}")
            print(f"target_case_view: {result.diagnostics.get('target_case_view')}")
            print(f"selected_case_view: {result.diagnostics.get('selected_case_view')}")
            print(f"target_view_not_selected: {result.diagnostics.get('target_view_not_selected')}")
            print(f"case_view_selection_attempted: {result.diagnostics.get('case_view_selection_attempted')}")
            print(f"case_view_selection_successful: {result.diagnostics.get('case_view_selection_successful')}")
            print(f"list_picker_found: {result.diagnostics.get('list_picker_found')}")
            print(f"list_picker_opened: {result.diagnostics.get('list_picker_opened')}")
            print(f"list_picker_open_method: {result.diagnostics.get('list_picker_open_method')}")
            print(f"available_list_views: {json.dumps(result.diagnostics.get('available_list_views', []), ensure_ascii=False)}")
            print(f"selected_view_before: {result.diagnostics.get('selected_view_before')}")
            print(f"selected_view_after: {result.diagnostics.get('selected_view_after')}")
            print(f"target_view_clicked: {result.diagnostics.get('target_view_clicked')}")
            print(f"target_view_click_method: {result.diagnostics.get('target_view_click_method')}")
            print(f"empty_cases_view_detected: {result.diagnostics.get('empty_cases_view_detected')}")
            print(f"empty_view_recovery_attempted: {result.diagnostics.get('empty_view_recovery_attempted')}")
            print(f"empty_view_recovery_successful: {result.diagnostics.get('empty_view_recovery_successful')}")
            print(
                "visible_list_view_labels: "
                + json.dumps(result.diagnostics.get("visible_list_view_labels", []), ensure_ascii=False)
            )
            print(f"selected_tab_or_list_label: {result.diagnostics.get('selected_tab_or_list_label')}")
            print(f"no_items_to_display_present: {result.diagnostics.get('no_items_to_display_present')}")
            print(f"cases_action_required_present: {result.diagnostics.get('cases_action_required_present')}")
            print(f"all_cases_present: {result.diagnostics.get('all_cases_present')}")
            print(f"my_company_cases_present: {result.diagnostics.get('my_company_cases_present')}")
            print(
                "closed_cases_submitted_for_my_company_present: "
                f"{result.diagnostics.get('closed_cases_submitted_for_my_company_present')}"
            )
            print(f"visible_case_number_count: {result.diagnostics.get('visible_case_number_count')}")
            print(
                "case_numbers_after_selection: "
                + json.dumps(result.diagnostics.get("case_numbers_after_selection", []), ensure_ascii=False)
            )
            print(f"no_items_after_selection: {result.diagnostics.get('no_items_after_selection')}")
            print(
                "visible_case_numbers_found: "
                + json.dumps(result.diagnostics.get("visible_case_numbers_found", result.visible_case_numbers), ensure_ascii=False)
            )
            print(
                "visible_case_numbers_after_view_selection: "
                + json.dumps(result.diagnostics.get("visible_case_numbers_after_view_selection", []), ensure_ascii=False)
            )
            print(f"structured_rows_detected: {result.diagnostics.get('structured_rows_detected')}")
            print(f"structured_rows_parsed: {result.diagnostics.get('structured_rows_parsed')}")
            print(f"structured_rows_failed: {result.diagnostics.get('structured_rows_failed')}")
            print(f"extraction_strategy_used: {result.diagnostics.get('extraction_strategy_used')}")
            print(
                "extracted_case_fields: "
                + json.dumps(result.diagnostics.get("extracted_case_fields", {}), ensure_ascii=False)
            )
            print(f"parsed_row_blocks: {result.diagnostics.get('parsed_row_blocks')}")
            print(f"row_block_preview: {result.diagnostics.get('row_block_preview')}")
            print(
                "subject_candidates: "
                + json.dumps(result.diagnostics.get("subject_candidates", []), ensure_ascii=False)
            )
            print(
                "rejected_subject_candidates: "
                + json.dumps(result.diagnostics.get("rejected_subject_candidates", []), ensure_ascii=False)
            )
            print(
                "extracted_field_sources: "
                + json.dumps(result.diagnostics.get("extracted_field_sources", {}), ensure_ascii=False)
            )
            print(f"row_text_preview: {result.diagnostics.get('row_text_preview')}")
            print(f"parsed_cases_from_text: {result.diagnostics.get('parsed_cases_from_text')}")
            print(f"container_text_length: {result.diagnostics.get('container_text_length')}")
            print(f"extracted_raw_cases: {result.diagnostics.get('extracted_raw_cases')}")
            print(f"unique_cases_after_dedup: {result.diagnostics.get('unique_cases_after_dedup')}")
            print(f"duplicate_cases_removed: {result.diagnostics.get('duplicate_cases_removed')}")
            print(
                "auth_state_domain_counts: "
                + json.dumps(result.diagnostics.get("auth_state_domain_counts", {}), ensure_ascii=False)
            )
            print(
                "auth_stage_history: "
                + json.dumps(result.diagnostics.get("auth_stage_history", []), ensure_ascii=False)
            )
            print(f"auth_stage_transition_count: {result.diagnostics.get('auth_stage_transition_count')}")
            print(f"last_auth_stage: {result.diagnostics.get('last_auth_stage')}")
            print(f"last_safe_url_host: {result.diagnostics.get('last_safe_url_host')}")
            print(f"auth_timeout_stage: {result.diagnostics.get('auth_timeout_stage')}")
            print(
                "duplicate_case_numbers: "
                + json.dumps(result.diagnostics.get("duplicate_case_numbers", []), ensure_ascii=False)
            )
        print(f"parsed_case_count: {len(result.cases)}")
        if result.diagnostics:
            print("selector_counts:")
            print(json.dumps(result.diagnostics.get("selector_counts", {}), indent=2, ensure_ascii=False))
        print("first_10_cases:")
        for case in result.cases[:10]:
            print(json.dumps(case, ensure_ascii=False))

        notifications_would_still_work = any(
            case.get("case_number") and case.get("subject")
            for case in result.cases
        )
        print(f"notifications_would_still_work: {notifications_would_still_work}")
        return 0
    finally:
        close_context(reason="test_browser_scrape")


if __name__ == "__main__":
    raise SystemExit(main())
