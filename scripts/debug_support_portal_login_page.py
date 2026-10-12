from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_manager import close_context, get_browser_entrypoint_diagnostics, get_or_create_context
from support_portal.browser_scraper import (
    LOGIN_FAILURE_HTML_PATH,
    LOGIN_FAILURE_SCREENSHOT_PATH,
    LOGIN_PAGE_HTML_PATH,
    LOGIN_PAGE_SCREENSHOT_PATH,
    LOGIN_STEP_FAILURE_HTML_PATH,
    LOGIN_STEP_FAILURE_SCREENSHOT_PATH,
    _capture_named_artifacts,
    _classify_auth_surface,
    _collect_visible_auth_surface,
    _detect_auth_shell_signals,
    _is_authenticated_cases_page,
    _attempt_support_portal_auto_login_from_current_page,
    _redact_url_for_logs,
    _safe_page_title,
)
from support_portal.debug_artifacts import cleanup_debug_artifacts


def _print_classification(page: object, *, auth_stage: str) -> None:
    surface = _collect_visible_auth_surface(page)
    shell = _detect_auth_shell_signals(page)
    classification = _classify_auth_surface(page, auth_surface=surface, shell_signals=shell)
    print(f"auth_stage: {auth_stage}")
    print(f"current_url_redacted: {classification.get('current_url_redacted')}")
    print(f"page_title: {classification.get('page_title')}")
    print(f"login_form_visible: {classification.get('login_form_visible')}")
    print(f"login_error_visible: {classification.get('login_error_visible')}")
    print(f"login_error_text: {classification.get('login_error_text')}")
    print(f"captcha_detected: {classification.get('captcha_detected')}")
    print(f"captcha_provider: {classification.get('captcha_provider')}")
    print(f"captcha_visible: {classification.get('captcha_visible')}")
    print(f"captcha_actionable: {classification.get('captcha_actionable')}")
    print(f"captcha_detection_source: {classification.get('captcha_detection_source')}")
    print(f"captcha_detection_rule: {classification.get('captcha_detection_rule')}")
    print(f"captcha_detection_match: {classification.get('captcha_detection_match')}")
    print(f"captcha_selector_used: {classification.get('captcha_selector_used')}")
    print(f"captcha_frame_url_redacted: {classification.get('captcha_frame_url_redacted')}")
    print(f"mfa_detected: {classification.get('mfa_detected')}")
    print(f"mfa_type: {classification.get('mfa_type')}")
    print(f"invalid_credentials_detected: {classification.get('invalid_credentials_detected')}")
    print(f"account_locked_detected: {classification.get('account_locked_detected')}")
    print(f"password_expired_detected: {classification.get('password_expired_detected')}")
    print(f"public_shell_detected: {classification.get('public_shell_detected')}")
    print(f"authenticated_detected: {classification.get('authenticated_detected')}")
    print(f"classification: {classification.get('classification')}")
    print(f"classification_reason: {classification.get('classification_reason')}")
    print(f"classification_evidence: {classification.get('classification_evidence')}")
    print(
        "visible_headings: "
        + json.dumps(surface.get("headings", []), ensure_ascii=False)
    )
    print(
        "visible_buttons: "
        + json.dumps(surface.get("buttons", []), ensure_ascii=False)
    )
    print(
        "visible_errors: "
        + json.dumps(surface.get("error_texts", []), ensure_ascii=False)
    )
    print(
        "visible_iframes_redacted: "
        + json.dumps(
            [_redact_url_for_logs(frame.get("src")) for frame in surface.get("visible_iframes", [])],
            ensure_ascii=False,
        )
    )


def _submit_credentials_for_debug(page: object, settings: object) -> dict[str, object]:
    return _attempt_support_portal_auto_login_from_current_page(
        page,
        settings,
        logger=logging.getLogger(__name__),
        cases_url=settings.cases_url or f"{(settings.portal_url or 'https://support.example.com/s').rstrip('/')}/case/Case/Default?tabset-836f8=1",
        diagnostics={
            "auto_login_enabled": bool(settings.support_portal_auto_login_enabled),
            "credentials_available": bool(settings.support_portal_username and settings.support_portal_password),
            "login_attempted": True,
            "auth_stage_history": [],
            "auth_stage_transition_count": 0,
            "last_auth_stage": "",
            "last_safe_url_host": "",
            "last_page_title": "",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit", action="store_true", help="Submit configured credentials and inspect the post-submit state.")
    args = parser.parse_args()

    settings = get_settings()
    cleanup_debug_artifacts(settings)
    browser_entrypoint = get_browser_entrypoint_diagnostics(settings)
    print(f"resolved_browser_profile_dir: {browser_entrypoint.get('resolved_browser_profile_dir')}")
    print(f"persistent_context_used: {browser_entrypoint.get('persistent_context_used')}")
    print(f"browser_executable_name: {browser_entrypoint.get('browser_executable_name')}")
    print(f"browser_headless: {browser_entrypoint.get('browser_headless')}")
    print(f"profile_exists_before_launch: {browser_entrypoint.get('profile_exists_before_launch')}")
    print(f"profile_owner_pid_if_any: {browser_entrypoint.get('profile_owner_pid_if_any')}")
    print(f"profile_lease_path: {browser_entrypoint.get('profile_lease_path')}")
    context, _ = get_or_create_context(settings)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.set_default_navigation_timeout(settings.support_portal_page_load_timeout_seconds * 1000)
        page.goto(settings.support_portal_login_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_PAGE_SCREENSHOT_PATH,
            html_path=LOGIN_PAGE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        print(f"screenshot: {LOGIN_PAGE_SCREENSHOT_PATH}")
        print(f"html: {LOGIN_PAGE_HTML_PATH}")
        _print_classification(page, auth_stage="initial_login_page")

        if not args.submit:
            return 0
        if not settings.support_portal_username or not settings.support_portal_password:
            print("submit_debug_error: missing_credentials")
            return 1

        login_url_before_submit = page.url
        login_title_before_submit = _safe_page_title(page)
        submit_result = _submit_credentials_for_debug(page, settings)
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_STEP_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_STEP_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        print(f"post_submit_reference_url_redacted: {_redact_url_for_logs(login_url_before_submit)}")
        print(f"post_submit_reference_title: {login_title_before_submit}")
        print(f"post_submit_screenshot: {LOGIN_STEP_FAILURE_SCREENSHOT_PATH}")
        print(f"post_submit_html: {LOGIN_STEP_FAILURE_HTML_PATH}")
        print(f"final_failure_screenshot: {LOGIN_FAILURE_SCREENSHOT_PATH}")
        print(f"final_failure_html: {LOGIN_FAILURE_HTML_PATH}")
        safe_login_completion = {
            key: submit_result.get(key)
            for key in (
                "credential_source",
                "username_available",
                "password_available",
                "username_length",
                "password_length",
                "public_shell_login_transition_attempted",
                "support_portal_login_form_reached",
                "okta_username_stage_reached",
                "okta_username_submitted",
                "okta_keep_signed_in_visible",
                "okta_keep_signed_in_requested",
                "okta_keep_signed_in_applied",
                "okta_password_stage_reached",
                "okta_password_submitted",
                "login_successful",
                "final_authenticated",
                "login_failure_reason",
                "auth_check_reason",
                "classification",
                "classification_reason",
                "classification_evidence",
                "captcha_detection_source",
                "captcha_detection_rule",
                "captcha_detection_match",
                "captcha_detection_visible",
                "captcha_detection_actionable",
                "captcha_provider_detected",
                "captcha_frame_url_redacted",
                "captcha_selector_used",
                "mfa_detected",
                "mfa_type",
                "invalid_credentials_detected",
                "account_locked_detected",
                "password_expired_detected",
                "public_shell_detected",
                "authenticated_detected",
                "saml_redirect_detected",
                "authenticated_support_portal_detected",
                "auth_stage_history",
                "auth_stage_transition_count",
                "last_auth_stage",
                "last_safe_url_host",
                "last_page_title",
                "auth_timeout_stage",
                "post_submit_url_host",
                "post_submit_safe_path",
                "post_submit_title",
                "post_submit_visible_headings",
                "post_submit_visible_buttons",
                "stable_public_shell_poll_count",
                "authenticated_identity_visible",
                "real_case_rows_visible",
                "post_auth_navigation_to_cases_attempted",
                "post_auth_navigation_to_cases_succeeded",
            )
        }
        print(f"login_completion: {json.dumps(safe_login_completion, ensure_ascii=False)}")
        _print_classification(page, auth_stage="post_submit")
        print(f"is_authenticated_cases_page: {_is_authenticated_cases_page(page)}")
        return 0
    finally:
        close_context(reason="debug_support_portal_login_page")


if __name__ == "__main__":
    raise SystemExit(main())
