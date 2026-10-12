from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support_portal.browser_scraper import (
    SupportPortalBrowserScrapeError,
    _maybe_apply_okta_keep_signed_in,
    _attempt_support_portal_auto_login,
    _submit_support_portal_credentials,
    _wait_for_login_completion,
    _wait_for_operator_auth_completion,
    ensure_support_portal_authenticated,
)


class FakePage:
    def __init__(self, url: str = "https://support.example.com/s/case/Case/Default") -> None:
        self.url = url
        self._title = "Case List"
        self.goto_calls: list[str] = []
        self.waits: list[int] = []

    def goto(self, url: str, **_: object) -> None:
        self.url = url
        self.goto_calls.append(url)

    def wait_for_load_state(self, *_: object, **__: object) -> None:
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    def title(self) -> str:
        return self._title


class FakeCheckbox:
    def __init__(self, checked: bool = False) -> None:
        self._checked = checked
        self.checked_calls = 0
        self.unchecked_calls = 0

    def is_checked(self) -> bool:
        return self._checked

    def check(self, timeout: int = 0) -> None:
        self._checked = True
        self.checked_calls += 1

    def uncheck(self, timeout: int = 0) -> None:
        self._checked = False
        self.unchecked_calls += 1


class FakeField:
    def __init__(self) -> None:
        self.filled: list[str] = []
        self.pressed: list[str] = []

    def fill(self, value: str) -> None:
        self.filled.append(value)

    def press(self, key: str) -> None:
        self.pressed.append(key)


class SequencedPage(FakePage):
    def __init__(self, states: list[dict[str, object]]) -> None:
        super().__init__(url=str(states[0]["url"]))
        self._states = states
        self._index = 0
        self._title = str(states[0]["title"])

    def advance(self) -> None:
        if self._index + 1 < len(self._states):
            self._index += 1
            self.url = str(self._states[self._index]["url"])
            self._title = str(self._states[self._index]["title"])

    @property
    def state(self) -> dict[str, object]:
        return self._states[self._index]


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "support_portal_auto_login_enabled": True,
        "support_portal_okta_keep_signed_in": True,
        "support_portal_username": "operator@example.com",
        "support_portal_password": "redacted",
        "portal_username": None,
        "portal_password": None,
        "support_portal_page_load_timeout_seconds": 5,
        "support_portal_login_timeout_seconds": 5,
        "support_portal_operator_wait_timeout_seconds": 30,
        "support_portal_manual_login_fallback": True,
        "support_portal_browser_headless": False,
        "support_portal_interactive_bootstrap_enabled": True,
        "portal_url": "https://support.example.com/s",
        "cases_url": "https://support.example.com/s/case/Case/Default?tabset-836f8=1",
        "support_portal_login_url": "https://support.example.com/s/login/",
        "support_portal_session_check_timeout_seconds": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SupportPortalAutoLoginFlowTests(unittest.TestCase):
    def test_public_shell_attempts_auto_login_when_credentials_available(self) -> None:
        page = FakePage()
        settings = _settings()
        logger = logging.getLogger("test")

        with (
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=[
                {"stage": "support_portal_public_shell", "host": "support.example.com", "safe_url": "https://support.example.com/s/case/Case/Default", "page_title": "Case List"},
                {"stage": "support_portal_login_form", "host": "support.example.com", "safe_url": "https://support.example.com/s/login/", "page_title": "Login"},
            ]),
            patch("support_portal.browser_scraper._click_visible_login_link", return_value=True),
            patch("support_portal.browser_scraper._wait_for_auth_transition", return_value={"stage": "support_portal_login_form"}),
            patch("support_portal.browser_scraper._submit_support_portal_credentials", return_value={
                "public_shell_login_transition_attempted": True,
                "public_shell_login_transition_method": "visible_login_control",
                "login_attempted": True,
                "login_successful": True,
                "login_submit_clicked": True,
                "username_field_found": True,
                "password_field_found": True,
                "login_requires_manual_intervention": False,
                "post_submit_classification": "authenticated_success",
            }) as submit_mock,
        ):
            diagnostics = _attempt_support_portal_auto_login(page, settings, logger, cases_url=settings.cases_url)

        self.assertTrue(diagnostics["public_shell_login_transition_attempted"])
        self.assertEqual(diagnostics["public_shell_login_transition_method"], "visible_login_control")
        self.assertTrue(diagnostics["login_attempted"])
        self.assertTrue(diagnostics["login_successful"])
        self.assertFalse(diagnostics["login_requires_manual_intervention"])
        submit_mock.assert_called_once()

    def test_public_shell_auto_login_disabled_requires_manual_reason(self) -> None:
        page = FakePage()
        settings = _settings(support_portal_auto_login_enabled=False)

        with (
            patch("support_portal.browser_scraper._wait_for_cases_ready", return_value=None),
            patch("support_portal.browser_scraper._detect_auth_shell_signals", return_value={
                "login_link_visible": True,
                "logged_out_shell_detected": True,
                "public_case_shell_detected": True,
                "authenticated_nav_visible": False,
            }),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", return_value=False),
            patch("support_portal.browser_scraper._detect_login", return_value=False),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", return_value=[]),
            patch("support_portal.browser_scraper._looks_like_manual_auth_required", return_value=True),
            patch("support_portal.browser_scraper._capture_named_artifacts", return_value=("shot.png", "page.html")),
            patch("support_portal.browser_scraper._is_interactive_operator_session", return_value=False),
        ):
            with self.assertRaises(SupportPortalBrowserScrapeError) as exc_info:
                ensure_support_portal_authenticated(page, settings, logging.getLogger("test"))

        self.assertEqual(exc_info.exception.login_failure_reason, "auto_login_disabled")

    def test_public_shell_missing_credentials_requires_manual_reason(self) -> None:
        page = FakePage()
        settings = _settings(support_portal_password=None)

        with (
            patch("support_portal.browser_scraper._wait_for_cases_ready", return_value=None),
            patch("support_portal.browser_scraper._detect_auth_shell_signals", return_value={
                "login_link_visible": True,
                "logged_out_shell_detected": True,
                "public_case_shell_detected": True,
                "authenticated_nav_visible": False,
            }),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", return_value=False),
            patch("support_portal.browser_scraper._detect_login", return_value=False),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", return_value=[]),
            patch("support_portal.browser_scraper._looks_like_manual_auth_required", return_value=True),
            patch("support_portal.browser_scraper._capture_named_artifacts", return_value=("shot.png", "page.html")),
            patch("support_portal.browser_scraper._is_interactive_operator_session", return_value=False),
        ):
            with self.assertRaises(SupportPortalBrowserScrapeError) as exc_info:
                ensure_support_portal_authenticated(page, settings, logging.getLogger("test"))

        self.assertEqual(exc_info.exception.login_failure_reason, "credentials_missing")
        self.assertEqual(exc_info.exception.diagnostics.get("credential_source"), "missing")

    def test_public_shell_login_success_marks_authenticated(self) -> None:
        page = FakePage()
        settings = _settings()
        logger = logging.getLogger("test")

        with (
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=[
                {"stage": "support_portal_public_shell", "host": "support.example.com", "safe_url": "https://support.example.com/s/case/Case/Default", "page_title": "Case List"},
                {"stage": "support_portal_login_form", "host": "support.example.com", "safe_url": "https://support.example.com/s/login/", "page_title": "Login"},
            ]),
            patch("support_portal.browser_scraper._click_visible_login_link", return_value=False),
            patch("support_portal.browser_scraper._wait_for_auth_transition", return_value={"stage": "support_portal_login_form"}),
            patch("support_portal.browser_scraper._submit_support_portal_credentials", return_value={
                "public_shell_login_transition_attempted": True,
                "public_shell_login_transition_method": "goto_login_url",
                "login_attempted": True,
                "login_successful": True,
                "final_authenticated": True,
                "login_submit_clicked": True,
                "username_field_found": True,
                "password_field_found": True,
                "login_requires_manual_intervention": False,
                "post_submit_classification": "authenticated_success",
            }),
        ):
            diagnostics = _attempt_support_portal_auto_login(page, settings, logger, cases_url=settings.cases_url)

        self.assertTrue(diagnostics["login_successful"])
        self.assertEqual(diagnostics["post_submit_classification"], "authenticated_success")

    def test_public_shell_login_invalid_credentials_keeps_specific_reason(self) -> None:
        page = FakePage()
        settings = _settings()
        logger = logging.getLogger("test")

        with (
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=[
                {"stage": "support_portal_public_shell", "host": "support.example.com", "safe_url": "https://support.example.com/s/case/Case/Default", "page_title": "Case List"},
                {"stage": "support_portal_login_form", "host": "support.example.com", "safe_url": "https://support.example.com/s/login/", "page_title": "Login"},
            ]),
            patch("support_portal.browser_scraper._click_visible_login_link", return_value=False),
            patch("support_portal.browser_scraper._wait_for_auth_transition", return_value={"stage": "support_portal_login_form"}),
            patch("support_portal.browser_scraper._submit_support_portal_credentials", return_value={
                "public_shell_login_transition_attempted": True,
                "public_shell_login_transition_method": "goto_login_url",
                "login_attempted": True,
                "login_successful": False,
                "login_failure_reason": "invalid_credentials",
                "login_requires_manual_intervention": True,
                "post_submit_classification": "invalid_credentials",
            }),
        ):
            diagnostics = _attempt_support_portal_auto_login(page, settings, logger, cases_url=settings.cases_url)

        self.assertEqual(diagnostics["login_failure_reason"], "invalid_credentials")
        self.assertEqual(diagnostics["post_submit_classification"], "invalid_credentials")

    def test_public_shell_login_mfa_marks_operator_action(self) -> None:
        page = FakePage()
        settings = _settings()
        logger = logging.getLogger("test")

        with (
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=[
                {"stage": "support_portal_public_shell", "host": "support.example.com", "safe_url": "https://support.example.com/s/case/Case/Default", "page_title": "Case List"},
                {"stage": "support_portal_login_form", "host": "support.example.com", "safe_url": "https://support.example.com/s/login/", "page_title": "Login"},
            ]),
            patch("support_portal.browser_scraper._click_visible_login_link", return_value=False),
            patch("support_portal.browser_scraper._wait_for_auth_transition", return_value={"stage": "support_portal_login_form"}),
            patch("support_portal.browser_scraper._submit_support_portal_credentials", return_value={
                "public_shell_login_transition_attempted": True,
                "public_shell_login_transition_method": "goto_login_url",
                "login_attempted": True,
                "login_successful": False,
                "login_failure_reason": "mfa_detected",
                "mfa_detected": True,
                "login_requires_manual_intervention": True,
                "post_submit_classification": "mfa_detected",
            }),
        ):
            diagnostics = _attempt_support_portal_auto_login(page, settings, logger, cases_url=settings.cases_url)

        self.assertEqual(diagnostics["login_failure_reason"], "mfa_detected")
        self.assertTrue(diagnostics["mfa_detected"])

    def test_keep_signed_in_visible_applies_requested_state(self) -> None:
        settings = _settings(support_portal_okta_keep_signed_in=True)
        checkbox = FakeCheckbox(checked=False)
        diagnostics: dict[str, object] = {}
        with patch("support_portal.browser_scraper._find_checkbox_in_contexts", return_value=("page", checkbox, True)):
            _maybe_apply_okta_keep_signed_in(FakePage(), settings, diagnostics)
        self.assertTrue(diagnostics["okta_keep_signed_in_visible"])
        self.assertTrue(diagnostics["okta_keep_signed_in_applied"])
        self.assertEqual(checkbox.checked_calls, 1)

    def test_keep_signed_in_absent_continues_safely(self) -> None:
        settings = _settings(support_portal_okta_keep_signed_in=True)
        diagnostics: dict[str, object] = {}
        with patch("support_portal.browser_scraper._find_checkbox_in_contexts", return_value=(None, None, False)):
            _maybe_apply_okta_keep_signed_in(FakePage(), settings, diagnostics)
        self.assertFalse(diagnostics["okta_keep_signed_in_visible"])
        self.assertFalse(diagnostics["okta_keep_signed_in_applied"])

    def test_submit_credentials_treats_password_stage_without_username_as_valid(self) -> None:
        page = FakePage("https://fp.okta.com/signin/verify/password")
        page._title = "Verify"
        settings = _settings()
        password_field = FakeField()
        with (
            patch("support_portal.browser_scraper._detect_login", return_value=False),
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=[
                {"stage": "okta_password_stage", "host": "fp.okta.com", "safe_url": "https://fp.okta.com/signin/verify/password", "page_title": "Verify"},
            ]),
            patch("support_portal.browser_scraper._auth_surface_summary", return_value={}),
            patch("support_portal.browser_scraper._discover_auth_field_with_retries", side_effect=[
                ("page", password_field, True),
            ]),
            patch("support_portal.browser_scraper._find_login_field_in_contexts", return_value=(None, None, False)),
            patch("support_portal.browser_scraper._find_role_button_in_contexts", return_value=(None, None, False)),
            patch("support_portal.browser_scraper._maybe_apply_okta_keep_signed_in"),
            patch("support_portal.browser_scraper._wait_for_login_completion", return_value={
                "login_successful": True,
                "login_success_check_result": True,
                "login_requires_manual_intervention": False,
                "classification": "authenticated_success",
            }),
            patch("support_portal.browser_scraper._detect_auth_shell_signals", return_value={}),
        ):
            diagnostics = _submit_support_portal_credentials(page, settings, logging.getLogger("test"), cases_url=settings.cases_url)
        self.assertTrue(diagnostics["password_field_found"])
        self.assertFalse(diagnostics["username_field_found"])
        self.assertTrue(diagnostics["okta_password_stage_reached"])
        self.assertTrue(diagnostics["login_successful"])

    def test_submit_credentials_waits_through_saml_redirect_without_username_failure(self) -> None:
        page = FakePage("https://fp.okta.com/app/salesforce/example/sso/saml")
        settings = _settings()
        with (
            patch("support_portal.browser_scraper._detect_login", return_value=False),
            patch("support_portal.browser_scraper._detect_auth_stage", return_value={
                "stage": "saml_redirect_stage",
                "host": "fp.okta.com",
                "safe_url": "https://fp.okta.com/app/salesforce/example/sso/saml",
                "page_title": "Redirecting",
            }),
            patch("support_portal.browser_scraper._auth_surface_summary", return_value={}),
            patch("support_portal.browser_scraper._wait_for_login_completion", return_value={
                "login_successful": True,
                "login_success_check_result": True,
                "login_requires_manual_intervention": False,
                "classification": "authenticated_success",
            }),
        ):
            diagnostics = _submit_support_portal_credentials(page, settings, logging.getLogger("test"), cases_url=settings.cases_url)
        self.assertTrue(diagnostics["login_successful"])
        self.assertNotEqual(diagnostics.get("login_failure_reason"), "username_field_not_found")

    def test_username_field_not_found_does_not_enter_manual_bootstrap(self) -> None:
        page = FakePage()
        settings = _settings()
        with (
            patch("support_portal.browser_scraper._wait_for_cases_ready", return_value=None),
            patch("support_portal.browser_scraper._detect_auth_shell_signals", return_value={
                "login_link_visible": False,
                "logged_out_shell_detected": False,
                "public_case_shell_detected": False,
                "authenticated_nav_visible": False,
            }),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", return_value=False),
            patch("support_portal.browser_scraper._detect_login", return_value=True),
            patch("support_portal.browser_scraper._attempt_support_portal_auto_login", return_value={
                "login_attempted": True,
                "login_successful": False,
                "login_failure_reason": "username_field_not_found",
                "login_requires_manual_intervention": True,
                "auth_stage_history": ["support_portal_login_form", "okta_username_stage"],
                "last_auth_stage": "okta_username_stage",
                "last_safe_url_host": "fp.okta.com",
                "last_page_title": "Sign In",
                "current_auth_hostname": "fp.okta.com",
                "current_auth_path": "/signin/identifier",
                "current_detected_auth_stage": "okta_username_stage",
                "visible_enabled_buttons": ["İleri"],
                "visible_headings": ["Oturum Aç"],
                "field_discovery_attempts": {"username": 4},
                "field_discovery_final_stage": {"username": "okta_username_stage"},
            }),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", return_value=[]),
            patch("support_portal.browser_scraper._capture_named_artifacts", return_value=("shot.png", "page.html")),
            patch("support_portal.browser_scraper._is_interactive_operator_session", return_value=True),
            patch("support_portal.browser_scraper._wait_for_operator_auth_completion") as wait_mock,
        ):
            with self.assertRaises(SupportPortalBrowserScrapeError) as exc_info:
                ensure_support_portal_authenticated(page, settings, logging.getLogger("test"))
        wait_mock.assert_not_called()
        self.assertEqual(exc_info.exception.login_failure_reason, "username_field_not_found")
        self.assertEqual(exc_info.exception.diagnostics.get("last_auth_stage"), "okta_username_stage")

    def test_operator_wait_exits_immediately_when_authenticated(self) -> None:
        page = FakePage()
        settings = _settings()
        logger = logging.getLogger("test")

        with (
            patch("support_portal.browser_scraper._detect_auth_shell_signals", side_effect=[
                {"authenticated_nav_visible": False},
                {"authenticated_nav_visible": True},
            ]),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", side_effect=[[], ["01234567"]]),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", side_effect=[False, False]),
            patch("support_portal.browser_scraper._wait_for_cases_ready", return_value=None),
        ):
            _wait_for_operator_auth_completion(
                page,
                settings,
                logger,
                cases_url=settings.cases_url,
                failure_reason="mfa_detected",
            )

        self.assertLessEqual(len(page.waits), 1)
        self.assertLessEqual(len(page.goto_calls), 1)

    def test_wait_for_login_completion_ignores_transient_public_shell_until_authenticated(self) -> None:
        states = [
            {
                "url": "https://support.example.com/s/case/Case/Default",
                "title": "Case List",
                "shell": {
                    "login_link_visible": True,
                    "logged_out_shell_detected": True,
                    "public_case_shell_detected": True,
                    "authenticated_nav_visible": False,
                },
                "surface": {"headings": ["All Cases"], "buttons": ["Log In"], "visible_enabled_buttons": ["Log In"]},
                "stage": "support_portal_public_shell",
                "classification": {
                    "classification": "public_logged_out_shell",
                    "classification_reason": "public_shell_signals_detected",
                    "classification_evidence": "log_in_visible_and_public_case_shell",
                    "public_shell_detected": True,
                    "authenticated_detected": False,
                    "credential_error_visible": False,
                    "credential_error_text_safe": "",
                    "invalid_credentials_evidence": "",
                },
                "visible_case_numbers": [],
                "authenticated_page": False,
            },
            {
                "url": "https://support.example.com/s/",
                "title": "Home",
                "shell": {
                    "login_link_visible": False,
                    "logged_out_shell_detected": False,
                    "public_case_shell_detected": False,
                    "authenticated_nav_visible": True,
                },
                "surface": {"headings": ["Welcome"], "buttons": [], "visible_enabled_buttons": []},
                "stage": "authenticated_support_portal_stage",
                "classification": {
                    "classification": "authenticated_success",
                    "classification_reason": "authenticated_cases_page_detected",
                    "classification_evidence": "visible_authenticated_shell",
                    "public_shell_detected": False,
                    "authenticated_detected": True,
                    "credential_error_visible": False,
                    "credential_error_text_safe": "",
                    "invalid_credentials_evidence": "",
                },
                "visible_case_numbers": [],
                "authenticated_page": True,
            },
        ]
        page = SequencedPage(states)
        diagnostics: dict[str, object] = {"auth_stage_history": []}

        def shell_side_effect(*_: object, **__: object) -> dict[str, object]:
            return dict(page.state["shell"])

        def surface_side_effect(*_: object, **__: object) -> dict[str, object]:
            return dict(page.state["surface"])

        def classify_side_effect(*_: object, **__: object) -> dict[str, object]:
            return dict(page.state["classification"])

        def stage_side_effect(*_: object, **__: object) -> dict[str, object]:
            return {
                "stage": page.state["stage"],
                "host": "support.example.com",
                "safe_url": str(page.state["url"]),
                "page_title": str(page.state["title"]),
            }

        def wait_side_effect(ms: int) -> None:
            page.waits.append(ms)
            page.advance()

        page.wait_for_timeout = wait_side_effect

        with (
            patch("support_portal.browser_scraper._detect_auth_shell_signals", side_effect=shell_side_effect),
            patch("support_portal.browser_scraper._collect_visible_auth_surface", side_effect=surface_side_effect),
            patch("support_portal.browser_scraper._classify_auth_surface", side_effect=classify_side_effect),
            patch("support_portal.browser_scraper._detect_auth_stage", side_effect=stage_side_effect),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", side_effect=lambda *_args, **_kwargs: list(page.state["visible_case_numbers"])),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", side_effect=lambda *_args, **_kwargs: bool(page.state["authenticated_page"])),
            patch("support_portal.browser_scraper._auth_surface_summary", return_value={}),
            patch("support_portal.browser_scraper._detect_login", return_value=False),
        ):
            result = _wait_for_login_completion(
                page,
                _settings(support_portal_login_timeout_seconds=2),
                logging.getLogger("test"),
                cases_url="https://support.example.com/s/case/Case/Default?tabset-836f8=1",
                login_url_before_submit="https://support.example.com/s/login/",
                login_title_before_submit="Login",
                diagnostics=diagnostics,
            )

        self.assertTrue(result["login_successful"])
        self.assertTrue(result["final_authenticated"])
        self.assertEqual(result["classification"], "authenticated_success")
        self.assertEqual(diagnostics.get("stable_public_shell_poll_count"), 1)

    def test_wait_for_login_completion_requires_stable_public_shell_before_failure(self) -> None:
        state = {
            "shell": {
                "login_link_visible": True,
                "logged_out_shell_detected": True,
                "public_case_shell_detected": True,
                "authenticated_nav_visible": False,
            },
            "surface": {"headings": ["All Cases"], "buttons": ["Log In"], "visible_enabled_buttons": ["Log In"]},
            "classification": {
                "classification": "public_logged_out_shell",
                "classification_reason": "public_shell_signals_detected",
                "classification_evidence": "log_in_visible_and_public_case_shell",
                "public_shell_detected": True,
                "authenticated_detected": False,
                "credential_error_visible": False,
                "credential_error_text_safe": "",
                "invalid_credentials_evidence": "",
            },
        }
        page = FakePage()
        diagnostics: dict[str, object] = {"auth_stage_history": []}

        with (
            patch("support_portal.browser_scraper._detect_auth_shell_signals", return_value=state["shell"]),
            patch("support_portal.browser_scraper._collect_visible_auth_surface", return_value=state["surface"]),
            patch("support_portal.browser_scraper._classify_auth_surface", return_value=state["classification"]),
            patch("support_portal.browser_scraper._detect_auth_stage", return_value={
                "stage": "support_portal_public_shell",
                "host": "support.example.com",
                "safe_url": page.url,
                "page_title": page.title(),
            }),
            patch("support_portal.browser_scraper._safe_visible_case_numbers", return_value=[]),
            patch("support_portal.browser_scraper._is_authenticated_cases_page", return_value=False),
            patch("support_portal.browser_scraper._auth_surface_summary", return_value={}),
            patch("support_portal.browser_scraper._detect_login", return_value=False),
            patch("support_portal.browser_scraper.time.time", side_effect=[0, 0.1, 0.2, 0.3, 0.4]),
        ):
            result = _wait_for_login_completion(
                page,
                _settings(support_portal_login_timeout_seconds=1),
                logging.getLogger("test"),
                cases_url="https://support.example.com/s/case/Case/Default?tabset-836f8=1",
                login_url_before_submit="https://support.example.com/s/login/",
                login_title_before_submit="Login",
                diagnostics=diagnostics,
            )

        self.assertFalse(result["login_successful"])
        self.assertEqual(result["classification"], "public_logged_out_shell")
        self.assertEqual(result["stable_public_shell_poll_count"], 3)
        self.assertEqual(diagnostics.get("stable_public_shell_poll_count"), 3)


if __name__ == "__main__":
    unittest.main()
