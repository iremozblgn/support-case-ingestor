from __future__ import annotations

import unittest

from support_portal.browser_scraper import (
    _auth_state_from_signals,
    _classify_auth_surface,
    _detect_auth_stage,
    _detect_login_blocker_reason,
    _detect_visible_captcha_state,
    _is_authenticated_cases_page,
)


class FakePage:
    def __init__(
        self,
        *,
        url: str,
        title: str,
        html: str = "",
        auth_surface: dict[str, object] | None = None,
        login_candidates: list[dict[str, str]] | None = None,
        dom: dict[str, object] | None = None,
    ) -> None:
        self.url = url
        self._title = title
        self._html = html
        self._auth_surface = auth_surface or {}
        self._login_candidates = login_candidates or []
        self._dom = dom or {
            "visible_case_numbers": [],
            "row_count": 0,
            "tbody_tr_count": 0,
            "case_related_anchors": [],
            "first_20_visible_texts": [],
            "top_candidate_container_class_names": [],
        }

    class _Locator:
        def __init__(self, visible: bool = False, text: str = "") -> None:
            self._visible = visible
            self._text = text
            self.first = self

        def count(self) -> int:
            return 1 if self._visible else 0

        def is_visible(self) -> bool:
            return self._visible

        def is_enabled(self) -> bool:
            return self._visible

        def inner_text(self, timeout: int = 0) -> str:
            return self._text

    def locator(self, selector: str):
        selector_lower = selector.lower()
        login_form_visible = bool(self._auth_surface.get("login_form_visible"))
        body_text = " ".join(
            [
                *[str(item) for item in self._auth_surface.get("headings", [])],
                *[str(item) for item in self._auth_surface.get("buttons", [])],
                *[str(item) for item in self._auth_surface.get("visible_texts", [])],
                *[str(item) for item in self._auth_surface.get("error_texts", [])],
            ]
        )
        if selector_lower == "body":
            return self._Locator(True, body_text)
        if "password" in selector_lower:
            return self._Locator(login_form_visible)
        if "button[type=\"submit\"]" in selector_lower:
            return self._Locator("Login" in self._auth_surface.get("buttons", []))
        return self._Locator(False)

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html

    def evaluate(self, script: str):
        if "login_candidates" in script:
            return {"login_candidates": self._login_candidates}
        if "visible_selector_matches" in script:
            return self._auth_surface
        if "visible_case_numbers" in script and "tbody_tr_count" in script:
            return self._dom
        raise AssertionError("Unexpected evaluate script")


def _surface(
    *,
    headings: list[str] | None = None,
    buttons: list[str] | None = None,
    visible_texts: list[str] | None = None,
    error_texts: list[str] | None = None,
    visible_iframes: list[dict[str, str]] | None = None,
    visible_selector_matches: list[dict[str, object]] | None = None,
    login_form_visible: bool = False,
) -> dict[str, object]:
    return {
        "headings": headings or [],
        "buttons": buttons or [],
        "visible_texts": visible_texts or [],
        "error_texts": error_texts or [],
        "visible_iframes": visible_iframes or [],
        "visible_selector_matches": visible_selector_matches or [],
        "login_form_visible": login_form_visible,
        "password_input_visible": login_form_visible,
        "username_input_visible": login_form_visible,
        "shadow_roots_exist": False,
    }


class AuthClassificationTests(unittest.TestCase):
    def test_normal_support_portal_login_form_is_not_captcha(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            auth_surface=_surface(
                headings=["Login"],
                buttons=["Login", "Reset your password here", "Are you an employee? Login here"],
                visible_texts=["Username", "Password"],
                login_form_visible=True,
            ),
        )
        captcha = _detect_visible_captcha_state(page)
        classification = _classify_auth_surface(page)
        self.assertFalse(captcha.detected)
        self.assertFalse(classification["captcha_detected"])
        self.assertFalse(classification["mfa_detected"])

    def test_hidden_script_captcha_word_does_not_trigger_captcha(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            html='<meta content="https://www.google.com/recaptcha/">',
            auth_surface=_surface(
                headings=["Login"],
                buttons=["Login"],
                visible_texts=["Username", "Password"],
                login_form_visible=True,
            ),
        )
        self.assertIsNone(_detect_login_blocker_reason(page))
        self.assertFalse(_classify_auth_surface(page)["captcha_detected"])

    def test_okta_device_challenge_html_is_mfa_not_captcha(self) -> None:
        page = FakePage(
            url="https://example.okta.com/signin/device-challenge",
            title="Sign In",
            html="<script>device-challenge-poll</script>",
            auth_surface=_surface(visible_texts=["Signing in..."]),
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "okta_device_challenge")
        self.assertTrue(classification["mfa_detected"])
        self.assertFalse(classification["captcha_detected"])

    def test_visible_recaptcha_iframe_triggers_captcha(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            auth_surface=_surface(
                visible_iframes=[{"src": "https://www.google.com/recaptcha/api2/anchor?k=test", "title": "reCAPTCHA"}],
            ),
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "captcha_detected")
        self.assertEqual(classification["captcha_provider"], "recaptcha")

    def test_visible_hcaptcha_widget_triggers_captcha(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            auth_surface=_surface(
                visible_selector_matches=[{"selector": ".h-captcha", "label": "hcaptcha_widget", "count": 1, "text": ""}],
            ),
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "captcha_detected")
        self.assertEqual(classification["captcha_provider"], "hcaptcha")

    def test_visible_oturum_acilamiyor_requires_password_submission_for_invalid_credentials(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            auth_surface=_surface(
                error_texts=["Oturum açılamıyor"],
                buttons=["Login"],
                login_form_visible=True,
            ),
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "")
        classification_after_password = _classify_auth_surface(page, password_submission_attempted=True)
        self.assertEqual(classification_after_password["classification"], "invalid_credentials")
        self.assertTrue(classification_after_password["invalid_credentials_detected"])

    def test_okta_username_stage_is_detected_without_invalid_credentials(self) -> None:
        page = FakePage(
            url="https://fp.okta.com/app/salesforce/example/sso/saml",
            title="Oturum Aç",
            auth_surface=_surface(
                headings=["Oturum Aç"],
                buttons=["İleri", "Oturumumu açık tut"],
                visible_texts=["Username"],
                login_form_visible=False,
            ) | {"username_input_visible": True, "password_input_visible": False},
        )
        classification = _classify_auth_surface(page)
        stage = _detect_auth_stage(page, auth_surface=page._auth_surface)
        self.assertEqual(classification["classification"], "")
        self.assertEqual(stage["stage"], "okta_username_stage")

    def test_okta_password_stage_detected(self) -> None:
        page = FakePage(
            url="https://fp.okta.com/signin/verify/password",
            title="Verify",
            auth_surface=_surface(
                headings=["Password"],
                buttons=["Doğrula"],
                visible_texts=["Şifre"],
                login_form_visible=False,
            ) | {"password_input_visible": True, "username_input_visible": False},
        )
        stage = _detect_auth_stage(page, auth_surface=page._auth_surface)
        self.assertEqual(stage["stage"], "okta_password_stage")

    def test_saml_redirect_stage_detected_without_logging_query(self) -> None:
        page = FakePage(
            url="https://fp.okta.com/app/salesforce/example/sso/saml?SAMLRequest=secret",
            title="Redirecting",
            auth_surface=_surface(visible_texts=["Redirecting"]),
        )
        classification = _classify_auth_surface(page)
        stage = _detect_auth_stage(page, auth_surface=page._auth_surface)
        self.assertEqual(stage["stage"], "saml_redirect_stage")
        self.assertNotIn("SAMLRequest", stage["safe_url"])
        self.assertFalse(classification["invalid_credentials_detected"])

    def test_support_portal_home_loading_is_redirect_loading_stage(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/",
            title="Home",
            auth_surface=_surface(visible_texts=["Loading"]),
        )
        stage = _detect_auth_stage(page, auth_surface=page._auth_surface)
        self.assertEqual(stage["stage"], "support_portal_redirect_loading")

    def test_public_cases_shell_is_unauthenticated(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/case/Case/Default",
            title="Case List",
            auth_surface=_surface(
                buttons=["Log In"],
                visible_texts=["All Cases", "0 items", "Case Information"],
            ),
            login_candidates=[{"text": "Log In", "href": "/s/login/", "ariaLabel": ""}],
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "public_logged_out_shell")
        auth_state = _auth_state_from_signals(
            session_reused=False,
            current_url=page.url,
            page_title=page.title(),
            visible_case_numbers=[],
            login_link_visible=True,
            logged_out_shell_detected=True,
            public_case_shell_detected=True,
            authenticated_nav_visible=False,
        )
        self.assertFalse(auth_state["final_authenticated"])

    def test_authenticated_page_with_cases_is_authenticated(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/case/Case/Default",
            title="Case List",
            auth_surface=_surface(
                visible_texts=["Partner Tools", "Closed Cases Submitted for My Company"],
            ),
            dom={
                "visible_case_numbers": ["01234567"],
                "row_count": 1,
                "tbody_tr_count": 1,
                "case_related_anchors": [{"href": "/500ABC", "text": "01234567", "visible": True}],
                "first_20_visible_texts": ["01234567"],
                "top_candidate_container_class_names": [],
            },
        )
        self.assertTrue(_is_authenticated_cases_page(page))

    def test_authenticated_home_with_partner_tools_is_authenticated(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/",
            title="Home",
            auth_surface=_surface(
                visible_texts=["Partner Tools", "Welcome"],
            ),
            dom={
                "visible_case_numbers": [],
                "row_count": 0,
                "tbody_tr_count": 0,
                "case_related_anchors": [],
                "first_20_visible_texts": ["Partner Tools", "Welcome"],
                "top_candidate_container_class_names": [],
            },
        )
        self.assertTrue(_is_authenticated_cases_page(page))
        stage = _detect_auth_stage(page, auth_surface=page._auth_surface)
        self.assertEqual(stage["stage"], "authenticated_support_portal_stage")

    def test_visible_mfa_page_is_not_captcha(self) -> None:
        page = FakePage(
            url="https://example.okta.com/signin/verify/okta/push",
            title="Verify",
            auth_surface=_surface(
                visible_texts=["Okta Verify", "Approve the sign-in", "Push notification"],
            ),
        )
        classification = _classify_auth_surface(page)
        self.assertEqual(classification["classification"], "mfa_detected")
        self.assertTrue(classification["mfa_detected"])
        self.assertFalse(classification["captcha_detected"])

    def test_login_timeout_classification(self) -> None:
        page = FakePage(
            url="https://support.example.com/s/login/",
            title="Login",
            auth_surface=_surface(
                headings=["Login"],
                buttons=["Login"],
                login_form_visible=True,
            ),
        )
        classification = _classify_auth_surface(page, timeout_reason="login_timeout")
        self.assertEqual(classification["classification"], "login_timeout")


if __name__ == "__main__":
    unittest.main()
