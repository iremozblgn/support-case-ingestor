from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from app import _graphql_exit_code
from config import REPO_ROOT, resolve_repo_path
from support_portal.browser_scraper import _auth_state_from_signals
from services.monitor import RunExitCategory
from storage.models import CaseRecord
from storage.repository import SupportPortalRepository, utcnow


class DummySettings:
    run_once = True


class AuthStateTests(unittest.TestCase):
    def test_login_url_overrides_session_reuse(self) -> None:
        state = _auth_state_from_signals(
            session_reused=True,
            current_url="https://support.example.com/s/login/",
            page_title="Login",
            visible_case_numbers=[],
            successful_authenticated_navigation=False,
            login_link_visible=False,
            logged_out_shell_detected=False,
            public_case_shell_detected=False,
            authenticated_nav_visible=False,
        )
        self.assertFalse(state["final_authenticated"])
        self.assertEqual(state["auth_rejected_reason"], "login_url_visible")


class ExitCodeTests(unittest.TestCase):
    def test_success_without_new_cases_maps_to_0(self) -> None:
        exit_code = _graphql_exit_code(
            DummySettings(),
            {
                "cycle_success": True,
                "exit_category": str(RunExitCategory.SUCCESS_NO_NEW_CASES),
                "run_exit_reason": "run_once_completed_no_new_cases",
            },
        )
        self.assertEqual(exit_code, 0)

    def test_configuration_error_maps_to_10(self) -> None:
        exit_code = _graphql_exit_code(
            DummySettings(),
            {
                "cycle_success": False,
                "exit_category": str(RunExitCategory.CONFIGURATION_ERROR),
                "run_exit_reason": "configuration_error",
            },
        )
        self.assertEqual(exit_code, 10)


class RetryEligibilityTests(unittest.TestCase):
    def test_stale_sending_state_is_retry_eligible(self) -> None:
        repo = SupportPortalRepository.__new__(SupportPortalRepository)
        record = CaseRecord(
            notification_status="SENDING",
            notification_attempt_count=1,
            notification_attempt_started_at=utcnow() - timedelta(minutes=10),
        )
        self.assertTrue(
            SupportPortalRepository.is_notification_retry_eligible(
                repo,
                record,
                now=utcnow(),
                max_attempts=3,
            )
        )


class ProfileResolutionTests(unittest.TestCase):
    def test_relative_profile_resolves_against_repo_root(self) -> None:
        resolved = resolve_repo_path(".playwright-support_portal-profile")
        self.assertEqual(resolved, (REPO_ROOT / ".playwright-support_portal-profile").resolve())

    def test_absolute_profile_preserved(self) -> None:
        absolute = Path("/tmp/support_portal-profile")
        self.assertEqual(resolve_repo_path(str(absolute)), absolute.resolve())


if __name__ == "__main__":
    unittest.main()
