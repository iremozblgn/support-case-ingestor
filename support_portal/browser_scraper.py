from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from support_portal.debug_artifacts import capture_page_artifacts, cleanup_debug_artifacts, should_write_debug_artifacts
from support_portal.browser_manager import (
    SupportPortalBrowserManagerError,
    acquire_scrape_lease,
    close_context,
    get_browser_entrypoint_diagnostics,
    get_context_health,
    get_or_create_context,
    release_scrape_lease,
)
from support_portal.parser import OPTIONAL_CASE_FIELDS


CASE_NUMBER_PATTERN = re.compile(r"\b0\d{7,}\b")
LIGHTWEIGHT_CASE_NUMBER_PATTERN = re.compile(r"\b0\d{7}\b|\b\d{8}\b")
DATE_TIME_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4},\s*\d{1,2}:\d{2}\s*(?:AM|PM)\b", flags=re.IGNORECASE)
DEFAULT_WAIT_SECONDS = 60
DOM_POLL_SECONDS = 90
FAILURE_SCREENSHOT_PATH = Path("debug") / "support_portal_scrape_failure.png"
FAILURE_HTML_PATH = Path("debug") / "support_portal_scrape_failure.html"
LOGIN_FAILURE_SCREENSHOT_PATH = Path("debug") / "support_portal_login_failure.png"
LOGIN_FAILURE_HTML_PATH = Path("debug") / "support_portal_login_failure.html"
LOGIN_PAGE_SCREENSHOT_PATH = Path("debug") / "support_portal_login_page.png"
LOGIN_PAGE_HTML_PATH = Path("debug") / "support_portal_login_page.html"
LOGIN_STEP_FAILURE_SCREENSHOT_PATH = Path("debug") / "support_portal_login_step_failure.png"
LOGIN_STEP_FAILURE_HTML_PATH = Path("debug") / "support_portal_login_step_failure.html"
EMPTY_VIEW_SCREENSHOT_PATH = Path("debug") / "support_portal_empty_cases_view.png"
EMPTY_VIEW_HTML_PATH = Path("debug") / "support_portal_empty_cases_view.html"

USERNAME_SELECTORS = (
    'input[type="email"]',
    'input[type="text"]',
    'input[name*="username" i]',
    'input[name*="user" i]',
    'input[name*="email" i]',
    'input[id*="username" i]',
    'input[id*="user" i]',
    'input[id*="email" i]',
    'input[placeholder*="Email" i]',
    'input[placeholder*="Username" i]',
    'input[placeholder*="email" i]',
    'input[placeholder*="username" i]',
    'input[aria-label*="Email" i]',
    'input[aria-label*="Username" i]',
    'input[autocomplete="username"]',
)
NEXT_SELECTORS = (
    'input[value*="Next" i]',
    'input[value*="Continue" i]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name*="password" i]',
    'input[id*="password" i]',
    'input[placeholder*="password" i]',
    'input[aria-label*="Password" i]',
    'input[autocomplete="current-password"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'input[value*="Login" i]',
    'input[value*="Sign In" i]',
)
SUBMIT_BUTTON_TEXT_PATTERNS = ("Login", "Log In", "Sign In")
NEXT_BUTTON_TEXT_PATTERNS = ("Next", "Continue")
KNOWN_CASE_VIEW_LABELS = (
    "Closed Cases Submitted for My Company",
    "Cases - Action Required",
    "Action Required Cases",
    "My Company's Cases",
    "My Company's Case",
    "All Cases",
)

GLOBAL_HEADER_PREFIXES = (
    "cases - action required",
    "cases",
    "refresh",
    "export list",
    "sort by",
    "column actions",
    "case information",
    "create case",
    "all cases",
    "my company's cases",
    "my customer's cases",
    "find answers with smart search",
)
KNOWN_RECORD_TYPES = {"technical support"}
KNOWN_STATUS_SNIPPETS = (
    "awaiting customer",
    "response received from customer",
    "action required",
    "closed",
    "new",
    "open",
    "pending",
    "in progress",
)
PRODUCT_NORMALIZATION_MAP = {
    "web security": "Web Security",
    "data loss prevention (dlp)": "Data Loss Prevention (DLP)",
    "dlp": "Data Loss Prevention (DLP)",
    "support_portal dspm": "SupportPortal DSPM",
    "email security": "Email Security",
    "casb": "CASB",
    "swg": "SWG",
    "ngfw": "NGFW",
    "endpoint": "Endpoint",
}

AUTH_STAGE_SUPPORT_PORTAL_PUBLIC_SHELL = "support_portal_public_shell"
AUTH_STAGE_SUPPORT_PORTAL_LOGIN_FORM = "support_portal_login_form"
AUTH_STAGE_OKTA_USERNAME = "okta_username_stage"
AUTH_STAGE_OKTA_PASSWORD = "okta_password_stage"
AUTH_STAGE_OKTA_MFA = "okta_mfa_stage"
AUTH_STAGE_OKTA_ERROR = "okta_error_stage"
AUTH_STAGE_SAML_REDIRECT = "saml_redirect_stage"
AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING = "support_portal_redirect_loading"
AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL = "authenticated_support_portal_stage"
AUTH_STAGE_UNKNOWN = "unknown_auth_stage"

OKTA_USERNAME_BUTTON_PATTERNS = ("İleri", "Ileri", "Next", "Continue", "Devam")
OKTA_PASSWORD_BUTTON_PATTERNS = ("Doğrula", "Dogrula", "Verify", "Sign In", "Login", "Log In", "Oturum Aç", "Oturum Ac")
OKTA_KEEP_SIGNED_IN_LABELS = ("Oturumumu açık tut", "Oturumumu acik tut", "Keep me signed in", "Keep me signed-in")


class SupportPortalBrowserScrapeError(Exception):
    """Browser scraping fallback failed."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "browser_error",
        operator_action_required: bool = False,
        login_failure_reason: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.operator_action_required = operator_action_required
        self.login_failure_reason = login_failure_reason
        self.diagnostics = diagnostics or {}


@dataclass(slots=True)
class BrowserScrapeResult:
    cases: list[dict[str, Any]]
    extraction_method_used: str
    current_url: str
    page_title: str
    login_detected: bool
    visible_case_numbers: list[str]
    row_count: int
    screenshot_path: str | None = None
    html_path: str | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass(slots=True)
class CaptchaDetectionResult:
    detected: bool
    provider: str | None = None
    visible: bool = False
    actionable: bool = False
    selector: str | None = None
    frame_url_redacted: str | None = None
    reason: str = ""
    source: str = ""
    rule: str = ""
    match: str = ""


def _looks_like_login(url: str) -> bool:
    lowered = url.lower()
    return "login" in lowered or "signin" in lowered


def _detect_login(page: Any) -> bool:
    if _looks_like_login(page.url):
        return True
    for selector in (
        'input[type="password"]',
        'input[name="password"]',
        'input[id*="password"]',
        'button[type="submit"]',
    ):
        locator = page.locator(selector)
        try:
            if locator.count() and locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _extract_case_id(case_url: str | None) -> str | None:
    if not case_url:
        return None
    match = re.search(r"/case/([A-Za-z0-9]{15,18})", case_url, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/s/case/([A-Za-z0-9]{15,18})", case_url, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/([A-Za-z0-9]{15,18})(?:$|[?#])", case_url, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _extract_case_id_from_text(text: str | None) -> str | None:
    normalized = str(text or "")
    match = re.search(r"\b(500[a-zA-Z0-9]{12,15})\b", normalized)
    if match:
        return match.group(1)
    return None


def _normalize_header_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _capture_failure_artifacts(page: Any) -> tuple[str | None, str | None]:
    return _capture_named_artifacts(
        page,
        screenshot_path=FAILURE_SCREENSHOT_PATH,
        html_path=FAILURE_HTML_PATH,
        failure=True,
    )


def _capture_named_artifacts(
    page: Any,
    *,
    screenshot_path: Path,
    html_path: Path,
    settings: Any | None = None,
    failure: bool = False,
) -> tuple[str | None, str | None]:
    return capture_page_artifacts(
        page,
        screenshot_path=screenshot_path,
        html_path=html_path,
        settings=settings,
        failure=failure,
    )


def _save_selector_screenshot(page: Any, attempt_number: int, *, settings: Any | None = None) -> str | None:
    if settings is not None and not should_write_debug_artifacts(settings):
        return None
    path = Path("debug") / f"selector_attempt_{attempt_number}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return None


def _wait_for_cases_ready(page: Any, *, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    selectors = (
        "table tbody tr",
        ".slds-table tbody tr",
        '[role="grid"] [role="row"]',
        'a[href*="/s/case/"]',
        'a[href*="/case/"]',
        '.forceCommunityRecordListDesktop',
        '.slds-card',
    )

    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_seconds * 1000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    while time.time() < deadline:
        if _detect_login(page):
            return
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() > 0:
                    return
            except Exception:
                continue
        page.wait_for_timeout(1000)


def _scroll_page_for_virtualized_rows(page: Any, *, passes: int = 5) -> None:
    for _ in range(passes):
        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            pass
        try:
            page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight * 0.85));")
        except Exception:
            pass
        page.wait_for_timeout(1200)


def _is_interactive_operator_session(settings: Any) -> bool:
    return bool(
        getattr(settings, "support_portal_interactive_bootstrap_enabled", True)
        and not getattr(settings, "support_portal_browser_headless", False)
        and sys.stdin.isatty()
    )


def _wait_for_operator_auth_completion(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    cases_url: str,
    failure_reason: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    timeout_seconds = max(30, int(getattr(settings, "support_portal_operator_wait_timeout_seconds", 300) or 300))
    deadline = time.time() + timeout_seconds
    logger.warning(
        "operator_action_required=true reason=%s interactive_bootstrap=true wait_timeout_seconds=%s",
        failure_reason or "manual_auth_required",
        timeout_seconds,
    )
    poll_count = 0
    while time.time() < deadline:
        poll_count += 1
        current_shell = _detect_auth_shell_signals(page)
        visible_case_numbers = _safe_visible_case_numbers(page)
        if _is_authenticated_cases_page(page) or visible_case_numbers or current_shell.get("authenticated_nav_visible"):
            logger.info("operator_bootstrap_authenticated=true poll_count=%s", poll_count)
            return
        try:
            if poll_count % 5 == 1:
                page.goto(cases_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            logger.debug("Operator bootstrap navigation retry failed", exc_info=True)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        _wait_for_cases_ready(page, timeout_seconds=10)
        current_shell = _detect_auth_shell_signals(page)
        visible_case_numbers = _safe_visible_case_numbers(page)
        if _is_authenticated_cases_page(page) or visible_case_numbers or current_shell.get("authenticated_nav_visible"):
            logger.info("operator_bootstrap_authenticated=true poll_count=%s", poll_count)
            return
        page.wait_for_timeout(1000)

    raise SupportPortalBrowserScrapeError(
        f"Operator bootstrap window expired before SupportPortal became authenticated. "
        f"login_failure_reason={failure_reason or 'operator_wait_timeout'}",
        category="operator_action_required",
        operator_action_required=True,
        login_failure_reason=failure_reason or "operator_wait_timeout",
        diagnostics=(diagnostics or {}),
    )


def _manual_cases_visibility_retry(page: Any) -> None:
    print("Cases page is open but no visible case rows were detected.")
    print("Navigate in the opened browser until the SupportPortal case rows you want are visibly loaded, then press ENTER in terminal.")
    try:
        input()
    except EOFError as exc:
        raise SupportPortalBrowserScrapeError(
            "Manual case visibility confirmation required, but no interactive terminal input is available."
        ) from exc
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


def _page_text_preview(page: Any, limit: int = 4000) -> str:
    try:
        body = page.locator("body")
        if body.count():
            text = body.first.inner_text(timeout=5000)
            return _normalize_line(text)[:limit]
    except Exception:
        return ""
    return ""


def _safe_page_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _safe_visible_case_numbers(page: Any) -> list[str]:
    try:
        dom = _dom_diagnostics(page)
    except Exception:
        return []
    return [str(item) for item in dom.get("visible_case_numbers", []) if str(item).strip()]


def _detect_auth_shell_signals(page: Any) -> dict[str, Any]:
    preview = _page_text_preview(page, limit=5000)
    preview_lower = preview.lower()
    visible_case_numbers = _safe_visible_case_numbers(page)
    dom = {}
    try:
        dom = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              const loginCandidates = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'))
                .filter((el) => isVisible(el))
                .map((el) => ({
                  text: textOf(el),
                  href: (el.getAttribute && el.getAttribute('href')) || '',
                  ariaLabel: (el.getAttribute && el.getAttribute('aria-label')) || '',
                }))
                .filter((item) => /log\\s*in|sign\\s*in/i.test(`${item.text} ${item.href} ${item.ariaLabel}`));
              return {
                login_candidates: loginCandidates.slice(0, 20),
              };
            }
            """
        )
    except Exception:
        dom = {"login_candidates": []}

    login_candidates = dom.get("login_candidates", []) if isinstance(dom, dict) else []
    login_link_visible = bool(login_candidates) or "log in" in preview_lower or "log in →" in preview_lower or "sign in" in preview_lower
    public_case_shell_detected = (
        ("case information" in preview_lower or "create case" in preview_lower or "all cases" in preview_lower)
        and not visible_case_numbers
    )
    authenticated_nav_visible = "partner tools" in preview_lower
    logged_out_shell_detected = bool(login_link_visible and public_case_shell_detected)

    return {
        "login_link_visible": bool(login_link_visible),
        "logged_out_shell_detected": bool(logged_out_shell_detected),
        "public_case_shell_detected": bool(public_case_shell_detected),
        "authenticated_nav_visible": bool(authenticated_nav_visible),
        "login_candidates": login_candidates,
    }


def _redact_url_for_logs(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return str(url or "").split("?", 1)[0].split("#", 1)[0]


def _safe_url_host_and_path(url: str | None) -> tuple[str, str]:
    if not url:
        return "", ""
    try:
        parts = urlsplit(url)
        return (parts.netloc or "").lower(), parts.path or ""
    except Exception:
        return "", str(url or "").split("?", 1)[0].split("#", 1)[0]


def _auth_surface_summary(page: Any, *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    surface = _collect_visible_auth_surface(page)
    shell = _detect_auth_shell_signals(page)
    stage_info = _detect_auth_stage(page, auth_surface=surface, shell_signals=shell)
    visible_case_numbers = _safe_visible_case_numbers(page)
    iframe_hosts: list[str] = []
    for frame in surface.get("visible_iframes", []):
        host, _ = _safe_url_host_and_path(frame.get("src"))
        if host and host not in iframe_hosts:
            iframe_hosts.append(host)
    return {
        "current_auth_hostname": stage_info.get("host", ""),
        "current_auth_path": _safe_url_host_and_path(page.url)[1],
        "current_auth_safe_url": stage_info.get("safe_url", ""),
        "current_auth_page_title": stage_info.get("page_title", ""),
        "current_detected_auth_stage": stage_info.get("stage", AUTH_STAGE_UNKNOWN),
        "auth_stage_history": list((diagnostics or {}).get("auth_stage_history", [])),
        "visible_input_types": dict(surface.get("visible_input_types", {})),
        "username_input_count": int(surface.get("username_input_count", 0) or 0),
        "password_input_count": int(surface.get("password_input_count", 0) or 0),
        "visible_headings": list(surface.get("headings", []))[:20],
        "visible_enabled_buttons": list(surface.get("visible_enabled_buttons", surface.get("buttons", [])))[:20],
        "iframe_hostnames": iframe_hosts[:10],
        "shadow_root_count": int(surface.get("shadow_root_count", 0) or 0),
        "authenticated_identity_visible": bool(shell.get("authenticated_nav_visible")),
        "real_case_rows_visible": bool(visible_case_numbers),
    }


def _resolve_auth_credentials(settings: Any) -> dict[str, Any]:
    support_portal_username = (getattr(settings, "support_portal_username", None) or "").strip()
    support_portal_password = (getattr(settings, "support_portal_password", None) or "").strip()
    portal_username = (getattr(settings, "portal_username", None) or "").strip()
    portal_password = (getattr(settings, "portal_password", None) or "").strip()

    source = "missing"
    username = ""
    password = ""
    warnings: list[str] = []

    if support_portal_username and support_portal_password:
        source = "support_portal"
        username = support_portal_username
        password = support_portal_password
    elif support_portal_username or support_portal_password:
        warnings.append("support_portal_pair_incomplete")
    elif portal_username and portal_password:
        source = "portal_fallback"
        username = portal_username
        password = portal_password
    elif portal_username or portal_password:
        warnings.append("portal_pair_incomplete")

    if support_portal_username and portal_username and support_portal_username != portal_username:
        warnings.append("conflicting_usernames")
    if getattr(settings, "support_portal_username", None) and getattr(settings, "support_portal_username", None) != support_portal_username:
        warnings.append("support_portal_username_whitespace_trimmed")
    if getattr(settings, "portal_username", None) and getattr(settings, "portal_username", None) != portal_username:
        warnings.append("portal_username_whitespace_trimmed")
    if getattr(settings, "support_portal_password", None) and getattr(settings, "support_portal_password", None) != support_portal_password:
        warnings.append("support_portal_password_whitespace_trimmed")
    if getattr(settings, "portal_password", None) and getattr(settings, "portal_password", None) != portal_password:
        warnings.append("portal_password_whitespace_trimmed")

    return {
        "username": username or None,
        "password": password or None,
        "credential_source": source,
        "username_available": bool(username),
        "password_available": bool(password),
        "username_length": len(username),
        "password_length": len(password),
        "credential_warnings": warnings,
    }


def _is_visible_enabled(locator: Any) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible() and locator.first.is_enabled()
    except Exception:
        return False


def _collect_visible_auth_surface(page: Any) -> dict[str, Any]:
    preset = getattr(page, "_auth_surface", None)
    if isinstance(preset, dict):
        return dict(preset)

    try:
        surface = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const norm = (text) => (text || '').replace(/\\s+/g, ' ').trim();
              const dedupe = (items, limit = 80) => {
                const out = [];
                for (const item of items.map((item) => norm(item)).filter(Boolean)) {
                  if (!out.includes(item)) out.push(item);
                  if (out.length >= limit) break;
                }
                return out;
              };
              const roots = [];
              const stack = [document];
              while (stack.length) {
                const root = stack.pop();
                if (!root || roots.includes(root)) continue;
                roots.push(root);
                const all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                for (const el of all) {
                  if (el.shadowRoot) stack.push(el.shadowRoot);
                }
              }
              const queryAll = (selector) => {
                const out = [];
                for (const root of roots) {
                  if (!root.querySelectorAll) continue;
                  for (const el of Array.from(root.querySelectorAll(selector))) out.push(el);
                }
                return out;
              };
              const textOf = (el) => norm(el.innerText || el.textContent || el.value || el.getAttribute?.('aria-label') || el.getAttribute?.('title') || '');
              const headings = dedupe(
                queryAll('h1, h2, h3, [role="heading"]').filter((el) => isVisible(el)).map((el) => textOf(el)),
                25
              );
              const buttons = dedupe(
                queryAll('button, a[href], input[type="submit"], input[type="button"], [role="button"], [role="link"]')
                  .filter((el) => isVisible(el))
                  .map((el) => textOf(el)),
                40
              );
              const visibleTexts = dedupe(
                queryAll('body *')
                  .filter((el) => isVisible(el))
                  .map((el) => textOf(el))
                  .filter((text) => text.length <= 280),
                160
              );
              const errors = dedupe(
                queryAll('[role="alert"], [aria-live="assertive"], [aria-live="polite"], .slds-form-element__help, .slds-notify, .slds-notify_alert, .error, .errors, .form-error, .login-error, .message-error')
                  .filter((el) => isVisible(el))
                  .map((el) => textOf(el)),
                40
              );
              const visibleFrames = queryAll('iframe')
                .filter((el) => isVisible(el))
                .map((el) => ({
                  src: el.getAttribute('src') || '',
                  title: el.getAttribute('title') || '',
                  name: el.getAttribute('name') || '',
                  id: el.getAttribute('id') || '',
                }))
                .slice(0, 25);
              const inputTypes = {};
              const usernameInputs = [];
              const passwordInputs = [];
              for (const input of queryAll('input')) {
                if (!isVisible(input)) continue;
                const type = (input.getAttribute('type') || 'text').toLowerCase();
                inputTypes[type] = (inputTypes[type] || 0) + 1;
                const autocomplete = (input.getAttribute('autocomplete') || '').toLowerCase();
                const placeholder = textOf(input);
                const ariaLabel = (input.getAttribute('aria-label') || '').toLowerCase();
                if (type === 'password') {
                  passwordInputs.push(input);
                } else if (type === 'email' || autocomplete === 'username' || ariaLabel.includes('user') || placeholder.toLowerCase().includes('user')) {
                  usernameInputs.push(input);
                }
              }
              const visibleButtonsEnabled = dedupe(
                queryAll('button, [role="button"], input[type="submit"], input[type="button"], a[role="button"]')
                  .filter((el) => isVisible(el) && !(el.hasAttribute && el.hasAttribute('disabled')))
                  .map((el) => textOf(el) || el.getAttribute?.('value') || '')
              ).slice(0, 60);
              const visibleSelectorMatches = [];
              const selectorMap = [
                ['iframe[src*="google.com/recaptcha"]', 'recaptcha_iframe'],
                ['iframe[src*="recaptcha.net"]', 'recaptcha_iframe'],
                ['iframe[title*="recaptcha" i]', 'recaptcha_title_iframe'],
                ['.g-recaptcha', 'recaptcha_widget'],
                ['.grecaptcha-badge', 'recaptcha_badge'],
                ['iframe[src*="hcaptcha.com"]', 'hcaptcha_iframe'],
                ['.h-captcha', 'hcaptcha_widget'],
                ['iframe[src*="arkoselabs.com"]', 'arkose_iframe'],
                ['iframe[src*="funcaptcha.com"]', 'funcaptcha_iframe'],
                ['[id*="arkose" i]', 'arkose_id'],
                ['[class*="arkose" i]', 'arkose_class'],
              ];
              for (const [selector, label] of selectorMap) {
                const matches = queryAll(selector).filter((el) => isVisible(el));
                if (matches.length) {
                  visibleSelectorMatches.push({
                    selector,
                    label,
                    count: matches.length,
                    text: textOf(matches[0]),
                  });
                }
              }
              const visiblePasswordInputs = queryAll('input[type="password"]').filter((el) => isVisible(el)).length;
              const visibleUsernameInputs = queryAll('input[type="text"], input[type="email"], input[autocomplete="username"]').filter((el) => isVisible(el)).length;
              return {
                headings,
                buttons,
                visible_texts: visibleTexts,
                error_texts: errors,
                visible_iframes: visibleFrames,
                visible_selector_matches: visibleSelectorMatches,
                login_form_visible: visiblePasswordInputs > 0 && visibleUsernameInputs > 0,
                password_input_visible: visiblePasswordInputs > 0,
                username_input_visible: visibleUsernameInputs > 0,
                shadow_roots_exist: roots.length > 1,
                shadow_root_count: roots.length > 1 ? roots.length - 1 : 0,
                visible_input_types: inputTypes,
                username_input_count: usernameInputs.length || visibleUsernameInputs,
                password_input_count: passwordInputs.length || visiblePasswordInputs,
                visible_enabled_buttons: visibleButtonsEnabled,
              };
            }
            """
        )
        if isinstance(surface, dict):
            return surface
    except Exception:
        pass

    return {
        "headings": [],
        "buttons": [],
        "visible_texts": [],
        "error_texts": [],
        "visible_iframes": [],
        "visible_selector_matches": [],
        "login_form_visible": False,
        "password_input_visible": False,
        "username_input_visible": False,
        "shadow_roots_exist": False,
        "shadow_root_count": 0,
        "visible_input_types": {},
        "username_input_count": 0,
        "password_input_count": 0,
        "visible_enabled_buttons": [],
    }


def _collect_cookie_domain_counts(context: Any) -> dict[str, int]:
    try:
        cookies = context.cookies()
    except Exception:
        return {}
    counts: dict[str, int] = {
        "support.example.com": 0,
        "fp.okta.com": 0,
        "salesforce_related": 0,
    }
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain.endswith("support.example.com"):
            counts["support.example.com"] += 1
        elif domain.endswith("fp.okta.com") or domain.endswith(".okta.com"):
            counts["fp.okta.com"] += 1
        elif "salesforce" in domain or domain.endswith(".my.site.com") or domain.endswith(".force.com"):
            counts["salesforce_related"] += 1
    return counts


def _captcha_default_diagnostics() -> dict[str, Any]:
    return {
        "captcha_detection_source": "",
        "captcha_detection_rule": "",
        "captcha_detection_match": "",
        "captcha_detection_visible": False,
        "captcha_detection_actionable": False,
        "captcha_provider_detected": "",
        "captcha_frame_url_redacted": "",
        "captcha_selector_used": "",
    }


def _detect_visible_captcha_state(page: Any, auth_surface: dict[str, Any] | None = None) -> CaptchaDetectionResult:
    surface = auth_surface or _collect_visible_auth_surface(page)
    provider_rules = (
        ("recaptcha", "google.com/recaptcha"),
        ("recaptcha", "recaptcha.net"),
        ("hcaptcha", "hcaptcha.com"),
        ("funcaptcha", "funcaptcha.com"),
        ("arkose", "arkoselabs.com"),
    )
    for frame in surface.get("visible_iframes", []):
        raw_src = str(frame.get("src") or "")
        raw_title = str(frame.get("title") or "")
        haystack = f"{raw_src} {raw_title}".lower()
        for provider, pattern in provider_rules:
            if pattern in haystack:
                return CaptchaDetectionResult(
                    detected=True,
                    provider=provider,
                    visible=True,
                    actionable=True,
                    frame_url_redacted=_redact_url_for_logs(raw_src),
                    reason="visible_provider_iframe",
                    source="iframe_url",
                    rule=pattern,
                    match=raw_src or raw_title,
                )

    for selector_match in surface.get("visible_selector_matches", []):
        selector = str(selector_match.get("selector") or "")
        label = str(selector_match.get("label") or "")
        if selector:
            provider = (
                "hcaptcha" if "hcaptcha" in label else
                "funcaptcha" if "funcaptcha" in label else
                "arkose" if "arkose" in label else
                "recaptcha"
            )
            return CaptchaDetectionResult(
                detected=True,
                provider=provider,
                visible=True,
                actionable=True,
                selector=selector,
                reason="visible_widget_selector",
                source="selector",
                rule=label or selector,
                match=str(selector_match.get("text") or selector),
            )

    explicit_text_rules = (
        ("i'm not a robot", "explicit_robot_checkbox"),
        ("i am not a robot", "explicit_robot_checkbox"),
        ("complete the captcha", "explicit_captcha_prompt"),
        ("complete captcha", "explicit_captcha_prompt"),
        ("verify you are human", "explicit_human_verification_prompt"),
        ("select all images", "image_puzzle_prompt"),
        ("hcaptcha", "provider_text"),
        ("recaptcha", "provider_text"),
    )
    for source_name, values in (
        ("visible_text", surface.get("visible_texts", [])),
        ("visible_error", surface.get("error_texts", [])),
        ("heading", surface.get("headings", [])),
    ):
        for value in values:
            lowered = str(value).lower()
            for phrase, rule in explicit_text_rules:
                if phrase in lowered:
                    return CaptchaDetectionResult(
                        detected=True,
                        provider="hcaptcha" if "hcaptcha" in phrase else "recaptcha" if "recaptcha" in phrase else None,
                        visible=True,
                        actionable=True,
                        reason="explicit_visible_captcha_text",
                        source=source_name,
                        rule=rule,
                        match=str(value),
                    )

    return CaptchaDetectionResult(detected=False, reason="no_visible_captcha_evidence")


def _captcha_result_to_diagnostics(result: CaptchaDetectionResult) -> dict[str, Any]:
    return {
        "captcha_detection_source": result.source,
        "captcha_detection_rule": result.rule,
        "captcha_detection_match": result.match,
        "captcha_detection_visible": result.visible,
        "captcha_detection_actionable": result.actionable,
        "captcha_provider_detected": result.provider or "",
        "captcha_frame_url_redacted": result.frame_url_redacted or "",
        "captcha_selector_used": result.selector or "",
    }


def _match_visible_phrase(values: list[str], patterns: tuple[str, ...]) -> str | None:
    for value in values:
        lowered = str(value).lower()
        for pattern in patterns:
            if pattern in lowered:
                return str(value)
    return None


def _classify_auth_surface(
    page: Any,
    *,
    auth_surface: dict[str, Any] | None = None,
    shell_signals: dict[str, Any] | None = None,
    timeout_reason: str | None = None,
    password_submission_attempted: bool = False,
) -> dict[str, Any]:
    surface = auth_surface or _collect_visible_auth_surface(page)
    shell = shell_signals or _detect_auth_shell_signals(page)
    visible_texts = [str(item) for item in surface.get("visible_texts", [])]
    error_texts = [str(item) for item in surface.get("error_texts", [])]
    headings = [str(item) for item in surface.get("headings", [])]
    buttons = [str(item) for item in surface.get("buttons", [])]
    captcha = _detect_visible_captcha_state(page, auth_surface=surface)
    visible_corpus = error_texts + headings + visible_texts + buttons
    html_preview = ""
    try:
        html_preview = page.content()[:200000].lower()
    except Exception:
        html_preview = ""

    result = {
        "auth_stage": "authenticated" if _is_authenticated_cases_page(page) else "login_or_transition",
        "current_url_redacted": _redact_url_for_logs(page.url),
        "page_title": _safe_page_title(page),
        "login_form_visible": bool(surface.get("login_form_visible")),
        "login_error_visible": bool(error_texts),
        "login_error_text": error_texts[0] if error_texts else "",
        "captcha_detected": captcha.detected,
        "captcha_provider": captcha.provider or "",
        "captcha_visible": captcha.visible,
        "captcha_actionable": captcha.actionable,
        "mfa_detected": False,
        "mfa_type": "",
        "invalid_credentials_detected": False,
        "password_submission_attempted": bool(password_submission_attempted),
        "credential_error_visible": bool(error_texts),
        "credential_error_text_safe": error_texts[0] if error_texts else "",
        "invalid_credentials_evidence": "",
        "account_locked_detected": False,
        "password_expired_detected": False,
        "public_shell_detected": bool(shell.get("logged_out_shell_detected") or shell.get("public_case_shell_detected")),
        "authenticated_detected": _is_authenticated_cases_page(page),
        "classification": "",
        "classification_reason": "",
        "classification_evidence": "",
    }
    result.update(_captcha_result_to_diagnostics(captcha))

    invalid_match = _match_visible_phrase(
        visible_corpus,
        (
            "oturum açılamıyor",
            "oturum acilamiyor",
            "unable to sign in",
            "sign in failed",
            "incorrect password",
            "invalid username or password",
            "incorrect username or password",
            "authentication failed",
            "password verification failed",
        ),
    )
    if result["authenticated_detected"]:
        result["classification"] = "authenticated_success"
        result["classification_reason"] = "authenticated_cases_page_detected"
        result["classification_evidence"] = "visible_authenticated_shell"
        return result
    if invalid_match and password_submission_attempted:
        result["invalid_credentials_detected"] = True
        result["classification"] = "invalid_credentials"
        result["classification_reason"] = "visible_invalid_credentials_text"
        result["classification_evidence"] = invalid_match
        result["invalid_credentials_evidence"] = invalid_match
        return result

    account_locked_match = _match_visible_phrase(
        visible_corpus,
        ("account locked", "hesap kilitlendi", "hesabınız kilitlendi"),
    )
    if account_locked_match:
        result["account_locked_detected"] = True
        result["classification"] = "account_locked"
        result["classification_reason"] = "visible_account_locked_text"
        result["classification_evidence"] = account_locked_match
        return result

    password_expired_match = _match_visible_phrase(
        visible_corpus,
        ("password expired", "şifrenizin süresi doldu", "sifrenizin suresi doldu"),
    )
    if password_expired_match:
        result["password_expired_detected"] = True
        result["classification"] = "password_expired"
        result["classification_reason"] = "visible_password_expired_text"
        result["classification_evidence"] = password_expired_match
        return result

    application_match = _match_visible_phrase(
        visible_corpus,
        ("app is not assigned", "application is not assigned", "not assigned to this app"),
    )
    if application_match:
        result["classification"] = "application_not_assigned"
        result["classification_reason"] = "visible_application_assignment_text"
        result["classification_evidence"] = application_match
        return result

    if captcha.detected and captcha.visible and captcha.actionable:
        result["classification"] = "captcha_detected"
        result["classification_reason"] = captcha.reason or "visible_captcha_detected"
        result["classification_evidence"] = captcha.match or captcha.selector or captcha.frame_url_redacted or captcha.rule
        return result

    mfa_match = _match_visible_phrase(
        visible_corpus,
        (
            "okta verify",
            "approve the sign-in",
            "push notification",
            "enter a code",
            "verification code",
            "device challenge",
            "kimliğiniz doğrulanıyor",
            "kimliginiz dogrulaniyor",
            "doğrulama kodu",
            "dogrulama kodu",
            "bildirimi onaylayın",
            "bildirimi onaylayin",
            "oturum açma isteği",
            "oturum acma istegi",
        ),
    )
    if mfa_match:
        result["mfa_detected"] = True
        result["mfa_type"] = "okta_verify" if "okta" in mfa_match.lower() else "interactive_mfa"
        result["classification"] = "mfa_detected"
        result["classification_reason"] = "visible_mfa_text"
        result["classification_evidence"] = mfa_match
        return result

    if "device-challenge-poll" in html_preview or "cancel-authenticator-challenge" in html_preview:
        evidence = "device-challenge-poll" if "device-challenge-poll" in html_preview else "cancel-authenticator-challenge"
        result["mfa_detected"] = True
        result["mfa_type"] = "okta_device_challenge"
        result["classification"] = "okta_device_challenge"
        result["classification_reason"] = "okta_device_challenge_html_marker"
        result["classification_evidence"] = evidence
        return result

    if result["public_shell_detected"]:
        result["classification"] = "public_logged_out_shell"
        result["classification_reason"] = "public_shell_signals_detected"
        result["classification_evidence"] = "log_in_visible_and_public_case_shell"
        return result

    if timeout_reason:
        result["classification"] = "login_timeout"
        result["classification_reason"] = timeout_reason
        result["classification_evidence"] = timeout_reason
        return result

    return result


def _detect_auth_stage(
    page: Any,
    *,
    auth_surface: dict[str, Any] | None = None,
    shell_signals: dict[str, Any] | None = None,
    password_submission_attempted: bool = False,
) -> dict[str, Any]:
    surface = auth_surface or _collect_visible_auth_surface(page)
    shell = shell_signals or _detect_auth_shell_signals(page)
    host, path = _safe_url_host_and_path(page.url)
    classification = _classify_auth_surface(
        page,
        auth_surface=surface,
        shell_signals=shell,
        password_submission_attempted=password_submission_attempted,
    )
    visible_texts = " ".join(str(item) for item in surface.get("visible_texts", []))
    headings = " ".join(str(item) for item in surface.get("headings", []))
    buttons = " ".join(str(item) for item in surface.get("buttons", []))
    visible_corpus = f"{visible_texts} {headings} {buttons}".lower()
    username_visible = bool(surface.get("username_input_visible"))
    password_visible = bool(surface.get("password_input_visible"))
    url_lower = (page.url or "").lower()

    stage = AUTH_STAGE_UNKNOWN
    if _is_authenticated_cases_page(page):
        stage = AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL
    elif "okta.com" in host:
        if classification.get("mfa_detected"):
            stage = AUTH_STAGE_OKTA_MFA
        elif classification.get("classification") in {
            "invalid_credentials",
            "account_locked",
            "password_expired",
            "application_not_assigned",
            "access_denied",
        }:
            stage = AUTH_STAGE_OKTA_ERROR
        elif password_visible:
            stage = AUTH_STAGE_OKTA_PASSWORD
        elif username_visible and not password_visible:
            stage = AUTH_STAGE_OKTA_USERNAME
        elif "samlrequest=" in url_lower or "samlresponse=" in url_lower or "/sso/saml" in path.lower():
            stage = AUTH_STAGE_SAML_REDIRECT
        elif "loading" in visible_corpus or "redirect" in visible_corpus or "redirecting" in visible_corpus:
            stage = AUTH_STAGE_SAML_REDIRECT
        elif "verify" in visible_corpus or "doğrula" in visible_corpus or "dogrula" in visible_corpus:
            stage = AUTH_STAGE_OKTA_PASSWORD
        else:
            stage = AUTH_STAGE_UNKNOWN
    elif "samlrequest=" in url_lower or "samlresponse=" in url_lower or "/sso/saml" in path.lower():
        stage = AUTH_STAGE_SAML_REDIRECT
    elif "support.example.com" in host and ("/s/" == path.lower() or path.lower() == "/s"):
        if shell.get("authenticated_nav_visible"):
            stage = AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL
        elif "loading" in visible_corpus or "redirect" in visible_corpus or "redirecting" in visible_corpus:
            stage = AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING
        else:
            stage = AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING
    elif shell.get("logged_out_shell_detected") or shell.get("public_case_shell_detected"):
        stage = AUTH_STAGE_SUPPORT_PORTAL_PUBLIC_SHELL
    elif _looks_like_login(page.url) or surface.get("login_form_visible") or (username_visible and password_visible):
        stage = AUTH_STAGE_SUPPORT_PORTAL_LOGIN_FORM
    elif classification.get("mfa_detected"):
        stage = AUTH_STAGE_OKTA_MFA
    elif password_submission_attempted and classification.get("classification"):
        stage = AUTH_STAGE_OKTA_ERROR

    return {
        "stage": stage,
        "host": host,
        "safe_url": _redact_url_for_logs(page.url),
        "page_title": _safe_page_title(page),
        "username_input_visible": username_visible,
        "password_input_visible": password_visible,
        "classification": classification.get("classification", ""),
        "shell_signals": shell,
    }


def _record_auth_stage(diagnostics: dict[str, Any], page: Any, stage_info: dict[str, Any]) -> None:
    history = diagnostics.setdefault("auth_stage_history", [])
    stage = stage_info.get("stage") or AUTH_STAGE_UNKNOWN
    if not history or history[-1] != stage:
        history.append(stage)
    diagnostics["auth_stage_transition_count"] = len(history)
    diagnostics["last_auth_stage"] = stage
    diagnostics["last_safe_url_host"] = stage_info.get("host", "")
    diagnostics["last_page_title"] = stage_info.get("page_title", "")


def _update_post_submit_diagnostics(
    page: Any,
    diagnostics: dict[str, Any],
    *,
    auth_surface: dict[str, Any] | None = None,
    shell_signals: dict[str, Any] | None = None,
) -> None:
    surface = auth_surface or _collect_visible_auth_surface(page)
    shell = shell_signals or _detect_auth_shell_signals(page)
    host, path = _safe_url_host_and_path(page.url)
    visible_case_numbers = _safe_visible_case_numbers(page)
    diagnostics["post_submit_url_host"] = host
    diagnostics["post_submit_safe_path"] = path
    diagnostics["post_submit_title"] = _safe_page_title(page)
    diagnostics["post_submit_visible_headings"] = list(surface.get("headings", []))[:20]
    diagnostics["post_submit_visible_buttons"] = list(surface.get("visible_enabled_buttons", surface.get("buttons", [])))[:20]
    diagnostics["authenticated_identity_visible"] = bool(shell.get("authenticated_nav_visible"))
    diagnostics["real_case_rows_visible"] = bool(visible_case_numbers)


def _is_strong_public_logged_out_shell(
    *,
    shell_signals: dict[str, Any],
    diagnostics: dict[str, Any],
) -> bool:
    return bool(
        shell_signals.get("login_link_visible")
        and not shell_signals.get("authenticated_nav_visible")
        and not diagnostics.get("authenticated_identity_visible")
        and not diagnostics.get("real_case_rows_visible")
    )


def _click_visible_login_link(page: Any) -> bool:
    def _safe_login_target(locator: Any) -> bool:
        try:
            text = (locator.first.inner_text(timeout=1000) or "").lower()
        except Exception:
            text = ""
        return "employee" not in text and "yardım" not in text and "help" not in text and "reset" not in text

    role_candidates = (
        ("link", re.compile(r"^\s*log\s*in\b", re.IGNORECASE)),
        ("button", re.compile(r"^\s*log\s*in\b", re.IGNORECASE)),
        ("link", re.compile(r"^\s*sign\s*in\b", re.IGNORECASE)),
        ("button", re.compile(r"^\s*sign\s*in\b", re.IGNORECASE)),
    )
    for role, pattern in role_candidates:
        try:
            locator = page.get_by_role(role, name=pattern)
            if locator.count() > 0 and locator.first.is_visible() and _safe_login_target(locator):
                locator.first.click(timeout=5000)
                return True
        except Exception:
            continue
    text_patterns = (r"^\s*Log\s*In\b", r"^\s*Sign\s*In\b")
    for pattern in text_patterns:
        try:
            locator = page.get_by_text(re.compile(pattern, re.IGNORECASE))
            if locator.count() > 0 and locator.first.is_visible() and _safe_login_target(locator):
                locator.first.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _is_timeout_exception(exc: Exception) -> bool:
    message = str(exc).lower()
    name = exc.__class__.__name__.lower()
    return "timeout" in name or "timed out" in message


def _normalize_view_label(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _matches_case_view_label(candidate: str, labels: tuple[str, ...]) -> bool:
    normalized_candidate = _normalize_view_label(candidate).lower()
    if not normalized_candidate:
        return False
    for label in labels:
        for variant in _case_view_label_variants(label):
            normalized_variant = _normalize_view_label(variant).lower()
            if normalized_variant and normalized_variant in normalized_candidate:
                return True
    return False


def _case_view_candidate_labels(settings: Any) -> tuple[str, ...]:
    target_view = _normalize_view_label(settings.support_portal_target_case_view)
    aliases = [_normalize_view_label(label) for label in settings.support_portal_target_case_view_aliases if _normalize_view_label(label)]
    ordered: list[str] = []

    def push(value: str) -> None:
        if value and value not in ordered:
            ordered.append(value)

    push(target_view)
    for alias in aliases:
        lowered = alias.lower()
        if "action required" in lowered and alias != target_view:
            push(alias)
    for alias in aliases:
        if alias not in ordered:
            push(alias)
    return tuple(ordered)


def _actionable_case_view_candidate_labels(settings: Any) -> tuple[str, ...]:
    target_view = _normalize_view_label(settings.support_portal_target_case_view)
    ordered = [target_view] if target_view else []
    for label in (
        "Closed Cases Submitted for My Company",
        "Cases - Action Required",
        "Action Required Cases",
        "My Company's Cases",
        "My Company's Case",
        "All Cases",
    ):
        normalized = _normalize_view_label(label)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    for label in _case_view_candidate_labels(settings):
        if label not in ordered:
            ordered.append(label)
    return tuple(ordered or [target_view])


def _acceptable_case_view_labels(settings: Any) -> tuple[str, ...]:
    target_view = _normalize_view_label(settings.support_portal_target_case_view)
    labels: list[str] = []
    for label in _actionable_case_view_candidate_labels(settings):
        normalized = _normalize_view_label(label)
        if not normalized:
            continue
        if normalized.lower() == "all cases" and target_view.lower() != "all cases":
            continue
        if normalized not in labels:
            labels.append(normalized)
    return tuple(labels or [target_view])


def _case_view_label_variants(label: str) -> tuple[str, ...]:
    normalized = _normalize_view_label(label)
    variants = [normalized]
    if normalized.endswith("Cases"):
        variants.append(normalized[:-1])
    if normalized.endswith("Case"):
        variants.append(normalized + "s")
    if "Company's Cases" in normalized:
        variants.append(normalized.replace("Company's Cases", "Company's Case"))
    if "Company's Case" in normalized:
        variants.append(normalized.replace("Company's Case", "Company's Cases"))
    deduped: list[str] = []
    for value in variants:
        if value and value not in deduped:
            deduped.append(value)
    return tuple(deduped)


def _resolve_selected_view_label(candidates: list[str], available_views: list[str]) -> str | None:
    normalized_candidates = [_normalize_view_label(candidate) for candidate in candidates if _normalize_view_label(candidate)]
    normalized_views = [_normalize_view_label(view) for view in available_views if _normalize_view_label(view)]

    for candidate in normalized_candidates:
        for view in normalized_views:
            if view and view.lower() in candidate.lower():
                return view

    for candidate in normalized_candidates:
        lowered = candidate.lower()
        if lowered.startswith("all cases"):
            return "All Cases"
        if "my company's case" in lowered:
            return "My Company's Case"
        if "my company's cases" in lowered:
            return "My Company's Cases"
        if "action required" in lowered:
            return candidate

    return normalized_candidates[0] if normalized_candidates else None


def _is_empty_cases_view(page: Any) -> bool:
    preview = _page_text_preview(page, limit=5000).lower()
    return "no items to display" in preview


def _collect_list_view_runtime_snapshot(page: Any, settings: Any) -> dict[str, Any]:
    snapshot = page.evaluate(
        """
        () => {
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const norm = (text) => (text || '').replace(/\\s+/g, ' ').trim();
          const roots = [];
          const stack = [document];
          while (stack.length) {
            const root = stack.pop();
            if (!root || roots.includes(root)) continue;
            roots.push(root);
            const nodeList = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
            for (const el of nodeList) {
              if (el.shadowRoot) stack.push(el.shadowRoot);
            }
          }
          const queryAll = (selector) => {
            const items = [];
            for (const root of roots) {
              if (!root.querySelectorAll) continue;
              for (const el of Array.from(root.querySelectorAll(selector))) {
                items.push(el);
              }
            }
            return items;
          };
          const textOf = (el) => norm(el.innerText || el.textContent || el.getAttribute?.('title') || el.getAttribute?.('aria-label') || el.getAttribute?.('data-value') || '');
          const dedupe = (values) => {
            const out = [];
            for (const value of values.map((value) => norm(value)).filter(Boolean)) {
              if (!out.includes(value)) out.push(value);
            }
            return out;
          };

          const selectedCandidates = dedupe(
            queryAll('[aria-selected="true"], [aria-current="page"], [aria-checked="true"], [role="tab"][aria-selected="true"], [role="option"][aria-selected="true"], [role="menuitemradio"][aria-checked="true"], [role="menuitemcheckbox"][aria-checked="true"], .slds-is-selected, lst-list-view-picker .slds-page-header__title, lst-list-view-picker .triggerLink, lst-list-view-picker button, lightning-combobox button')
              .filter((el) => isVisible(el))
              .map((el) => textOf(el))
          );
          const visibleButtons = dedupe(
            queryAll('button, [role="button"], a[href]')
              .filter((el) => isVisible(el))
              .map((el) => textOf(el))
              .filter((text) => text && (text.includes('Cases') || text.includes('Case') || /list/i.test(text) || /refresh/i.test(text) || /action required/i.test(text)))
          ).slice(0, 80);
          const visibleMenuItems = dedupe(
            queryAll('[role="menuitem"], lightning-menu-item, lightning-base-combobox-item, [role="option"], [data-value], [title], span.slds-truncate')
              .filter((el) => isVisible(el))
              .map((el) => textOf(el))
              .filter((text) => text && (text.includes('Cases') || text.includes('Case') || /action required/i.test(text) || /all cases/i.test(text) || /company/i.test(text)))
          ).slice(0, 120);
          const visibleOptions = dedupe(
            queryAll('[role="option"], lightning-base-combobox-item, li, option')
              .filter((el) => isVisible(el))
              .map((el) => textOf(el))
              .filter((text) => text && (text.includes('Cases') || text.includes('Case') || /action required/i.test(text) || /all cases/i.test(text) || /company/i.test(text)))
          ).slice(0, 120);
          const ariaListLabels = dedupe(
            queryAll('[aria-label]')
              .filter((el) => isVisible(el))
              .map((el) => el.getAttribute('aria-label') || '')
              .filter((text) => /list/i.test(text))
          ).slice(0, 60);
          const casesTexts = dedupe(
            queryAll('body *, lst-list-view-picker *, lightning-base-combobox-item *, lightning-menu-item *')
              .filter((el) => isVisible(el))
              .map((el) => textOf(el))
              .filter((text) => text && (text.includes('Cases') || text.includes('Case')))
          ).slice(0, 120);
          const pickerNodes = queryAll('lst-list-view-picker, lightning-combobox, [aria-haspopup="listbox"], [aria-haspopup="true"]');
          const pickerFound = pickerNodes.some((el) => isVisible(el));
          return {
            list_picker_found: pickerFound,
            selected_view_text_candidates: selectedCandidates,
            visible_buttons: visibleButtons,
            visible_menuitems: visibleMenuItems,
            visible_combobox_options: visibleOptions,
            aria_list_labels: ariaListLabels,
            cases_text_nodes: casesTexts,
          };
        }
        """
    )
    visible_case_numbers = _safe_visible_case_numbers(page)
    available_views = []
    for source in (
        snapshot.get("visible_menuitems", []),
        snapshot.get("visible_combobox_options", []),
        snapshot.get("visible_buttons", []),
        snapshot.get("cases_text_nodes", []),
    ):
        for value in source:
            normalized = _normalize_view_label(value)
            if (
                normalized
                and ("cases" in normalized.lower() or re.search(r"\bcase\b", normalized.lower()) is not None)
                and normalized not in available_views
            ):
                available_views.append(normalized)
    selected_before = _resolve_selected_view_label(snapshot.get("selected_view_text_candidates", []), available_views)
    return {
        "list_picker_found": bool(snapshot.get("list_picker_found")),
        "visible_buttons": snapshot.get("visible_buttons", []),
        "visible_menuitems": snapshot.get("visible_menuitems", []),
        "visible_combobox_options": snapshot.get("visible_combobox_options", []),
        "aria_list_labels": snapshot.get("aria_list_labels", []),
        "cases_text_nodes": snapshot.get("cases_text_nodes", []),
        "available_list_views": available_views,
        "selected_view_text_candidates": snapshot.get("selected_view_text_candidates", []),
        "selected_view_before": selected_before,
        "selected_view_after": selected_before,
        "current_selected_view_text": selected_before,
        "case_numbers_after_selection": visible_case_numbers,
        "no_items_after_selection": _is_empty_cases_view(page),
    }


def _extract_case_view_diagnostics(page: Any, settings: Any) -> dict[str, Any]:
    runtime = _collect_list_view_runtime_snapshot(page, settings)
    visible_text_preview = _page_text_preview(page, limit=7000)
    visible_case_numbers = _safe_visible_case_numbers(page)
    selected_label = runtime.get("selected_view_after") or runtime.get("selected_view_before")
    visible_labels = runtime.get("available_list_views", [])[:30]
    return {
        "current_url": page.url,
        "page_title": _safe_page_title(page),
        "visible_list_view_labels": visible_labels,
        "selected_tab_or_list_label": selected_label,
        "selected_case_view": selected_label,
        "no_items_to_display_present": "No items to display" in visible_text_preview,
        "cases_action_required_present": "Cases - Action Required" in visible_text_preview,
        "all_cases_present": "All Cases" in visible_text_preview,
        "my_company_cases_present": (
            "My Company's Cases" in visible_text_preview or "My Company's Case" in visible_text_preview
        ),
        "closed_cases_submitted_for_my_company_present": "Closed Cases Submitted for My Company" in visible_text_preview,
        "visible_case_number_count": len(visible_case_numbers),
        "visible_case_numbers_after_view_selection": visible_case_numbers,
    }


def _try_click_case_view_label(page: Any, label: str) -> bool:
    escaped = re.escape(label)
    patterns = (
        ("get_by_role", {"role": "tab", "name": re.compile(escaped, re.IGNORECASE)}),
        ("get_by_role", {"role": "button", "name": re.compile(escaped, re.IGNORECASE)}),
        ("get_by_role", {"role": "link", "name": re.compile(escaped, re.IGNORECASE)}),
        ("get_by_role", {"role": "menuitem", "name": re.compile(escaped, re.IGNORECASE)}),
        ("get_by_role", {"role": "option", "name": re.compile(escaped, re.IGNORECASE)}),
        ("get_by_text", {"text": re.compile(escaped, re.IGNORECASE)}),
    )
    for method, params in patterns:
        try:
            locator = (
                page.get_by_role(params["role"], name=params["name"])
                if method == "get_by_role"
                else page.get_by_text(params["text"])
            )
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _try_click_list_view_controls(page: Any, label: str) -> bool:
    control_selectors = (
        '[aria-label*="List View" i]',
        'button[title*="List View" i]',
        '[role="combobox"]',
        'lst-list-view-picker .triggerLink',
        'lst-list-view-picker .pickerChevron button',
        'lst-list-view-picker .slds-page-header__title',
    )
    for selector in control_selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                if _try_click_case_view_label(page, label):
                    return True
                option = page.get_by_role("option", name=re.compile(re.escape(label), re.IGNORECASE))
                if option.count() > 0 and option.first.is_visible():
                    option.first.click(timeout=5000)
                    return True
                menu_item = page.get_by_role("menuitem", name=re.compile(re.escape(label), re.IGNORECASE))
                if menu_item.count() > 0 and menu_item.first.is_visible():
                    menu_item.first.click(timeout=5000)
                    return True
                text_locator = page.locator(f'text="{label}"')
                if text_locator.count() > 0 and text_locator.first.is_visible():
                    text_locator.first.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False


def _focus_and_press_picker_keys(page: Any, locator: Any, *, keys: tuple[str, ...]) -> str | None:
    try:
        locator.focus(timeout=5000)
        for key in keys:
            locator.press(key, timeout=5000)
            page.wait_for_timeout(700)
        return "+".join(keys)
    except Exception:
        return None


def _click_runtime_list_view_item(page: Any, labels: tuple[str, ...]) -> str | None:
    label_list = [label for label in labels if _normalize_view_label(label)]
    if not label_list:
        return None
    return page.evaluate(
        """
        (labels) => {
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const norm = (text) => (text || '').replace(/\\s+/g, ' ').trim();
          const roots = [];
          const stack = [document];
          while (stack.length) {
            const root = stack.pop();
            if (!root || roots.includes(root)) continue;
            roots.push(root);
            const nodeList = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
            for (const el of nodeList) {
              if (el.shadowRoot) stack.push(el.shadowRoot);
            }
          }
          const selectors = [
            'lightning-base-combobox-item',
            'lightning-menu-item',
            '[role="option"]',
            '[role="menuitem"]',
            '[data-value]',
            '[title]',
            'span.slds-truncate',
            'a',
            'button',
          ];
          const all = [];
          for (const root of roots) {
            if (!root.querySelectorAll) continue;
            for (const selector of selectors) {
              for (const el of Array.from(root.querySelectorAll(selector))) {
                if (!all.includes(el)) all.push(el);
              }
            }
          }
          const textValues = (el) => [
            norm(el.innerText || el.textContent || ''),
            norm(el.getAttribute?.('title') || ''),
            norm(el.getAttribute?.('aria-label') || ''),
            norm(el.getAttribute?.('data-value') || ''),
          ].filter(Boolean);
          const clickEl = (el) => {
            const target = el.querySelector?.('a,button,[role="option"],[role="menuitem"],span.slds-truncate') || el;
            target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
            target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
            target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
            target.click();
          };

          for (const label of labels) {
            const normalized = norm(label).toLowerCase();
            for (const el of all) {
              if (!isVisible(el)) continue;
              const values = textValues(el).map((value) => value.toLowerCase());
              if (values.some((value) => value === normalized)) {
                clickEl(el);
                return label;
              }
            }
          }
          for (const label of labels) {
            const normalized = norm(label).toLowerCase();
            for (const el of all) {
              if (!isVisible(el)) continue;
              const values = textValues(el).map((value) => value.toLowerCase());
              if (values.some((value) => value.includes(normalized) || normalized.includes(value))) {
                clickEl(el);
                return label;
              }
            }
          }
          return null;
        }
        """,
        label_list,
    )


def _open_list_view_picker(page: Any, settings: Any, logger: logging.Logger) -> tuple[bool, str | None]:
    target_view = _normalize_view_label(settings.support_portal_target_case_view)
    candidate_controls = (
        ("current_view_button", f'text="{target_view}"'),
        ("picker_trigger_link", "lst-list-view-picker .triggerLink"),
        ("picker_chevron_button", "lst-list-view-picker .pickerChevron button"),
        ("aria_haspopup_listbox", 'button[aria-haspopup="listbox"]'),
        ("aria_haspopup_true", 'button[aria-haspopup="true"]'),
        ("list_view_aria_label", '[aria-label*="List View" i]'),
    )
    for method_name, selector in candidate_controls:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                snapshot = _collect_list_view_runtime_snapshot(page, settings)
                if snapshot.get("visible_menuitems") or snapshot.get("visible_combobox_options"):
                    return True, method_name
        except Exception:
            continue

    key_controls = (
        ("picker_arrowdown", "lst-list-view-picker .triggerLink", ("ArrowDown",)),
        ("picker_enter", "lst-list-view-picker .triggerLink", ("Enter",)),
        ("picker_space", "lst-list-view-picker .triggerLink", (" ",)),
        ("picker_chevron_arrowdown", "lst-list-view-picker .pickerChevron button", ("ArrowDown",)),
        ("picker_chevron_enter", "lst-list-view-picker .pickerChevron button", ("Enter",)),
    )
    for method_name, selector, keys in key_controls:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                key_method = _focus_and_press_picker_keys(page, locator.first, keys=keys)
                if key_method:
                    snapshot = _collect_list_view_runtime_snapshot(page, settings)
                    if snapshot.get("visible_menuitems") or snapshot.get("visible_combobox_options"):
                        return True, method_name
        except Exception:
            continue

    logger.debug("Unable to open runtime list-view picker with known interactions")
    return False, None


def _select_target_view_from_open_picker(page: Any, settings: Any, logger: logging.Logger) -> tuple[bool, str | None]:
    labels = _actionable_case_view_candidate_labels(settings)
    for label in labels:
        for variant in _case_view_label_variants(label):
            exact_patterns = (
                ("menuitem_exact", page.get_by_role("menuitem", name=re.compile(rf"^{re.escape(variant)}$", re.IGNORECASE))),
                ("option_exact", page.get_by_role("option", name=re.compile(rf"^{re.escape(variant)}$", re.IGNORECASE))),
                ("text_exact", page.get_by_text(re.compile(rf"^{re.escape(variant)}$", re.IGNORECASE))),
            )
            for method_name, locator in exact_patterns:
                try:
                    if locator.count() > 0 and locator.first.is_visible():
                        locator.first.click(timeout=5000)
                        return True, f"{method_name}:{variant}"
                except Exception:
                    continue

            partial_selectors = (
                ("lightning_base_combobox_item", f'lightning-base-combobox-item:has-text("{variant}")'),
                ("lightning_menu_item", f'lightning-menu-item:has-text("{variant}")'),
                ("role_option_partial", f'[role="option"]:has-text("{variant}")'),
                ("role_menuitem_partial", f'[role="menuitem"]:has-text("{variant}")'),
                ("data_value_partial", f'[data-value*="{variant}" i]'),
                ("title_partial", f'[title*="{variant}" i]'),
                ("truncate_partial", f'span.slds-truncate:has-text("{variant}")'),
            )
            for method_name, selector in partial_selectors:
                try:
                    locator = page.locator(selector)
                    if locator.count() > 0 and locator.first.is_visible():
                        locator.first.click(timeout=5000)
                        return True, f"{method_name}:{variant}"
                except Exception:
                    continue
            try:
                runtime_clicked_variant = _click_runtime_list_view_item(page, (variant,))
                if runtime_clicked_variant:
                    return True, f"runtime_dom_click:{runtime_clicked_variant}"
            except Exception:
                continue

    search_selectors = (
        'input[role="combobox"]',
        'input[type="search"]',
        'input[placeholder*="List" i]',
        'input[placeholder*="Search lists" i]',
    )
    for selector in search_selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                for label in labels:
                    for variant in _case_view_label_variants(label):
                        locator.first.fill("")
                        locator.first.fill(variant, timeout=5000)
                        page.wait_for_timeout(1000)
                        option = page.get_by_role("option", name=re.compile(re.escape(variant), re.IGNORECASE))
                        if option.count() > 0 and option.first.is_visible():
                            option.first.click(timeout=5000)
                            return True, f"search_then_option:{variant}"
                        menu_item = page.get_by_role("menuitem", name=re.compile(re.escape(variant), re.IGNORECASE))
                        if menu_item.count() > 0 and menu_item.first.is_visible():
                            menu_item.first.click(timeout=5000)
                            return True, f"search_then_menuitem:{variant}"
                        runtime_clicked_variant = _click_runtime_list_view_item(page, (variant,))
                        if runtime_clicked_variant:
                            return True, f"search_then_runtime_dom_click:{runtime_clicked_variant}"
        except Exception:
            continue

    logger.debug("Runtime list-view picker opened but target view was not selectable")
    return False, None


def _try_refresh_cases_view(page: Any) -> bool:
    refresh_patterns = (
        ("button", re.compile(r"refresh", re.IGNORECASE)),
        ("link", re.compile(r"refresh", re.IGNORECASE)),
    )
    for role, pattern in refresh_patterns:
        try:
            locator = page.get_by_role(role, name=pattern)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                return True
        except Exception:
            continue
    try:
        locator = page.get_by_text(re.compile(r"refresh", re.IGNORECASE))
        if locator.count() > 0 and locator.first.is_visible():
            locator.first.click(timeout=5000)
            return True
    except Exception:
        return False
    return False


def _count_visible_case_row_signals(page: Any) -> int:
    diagnostics = _dom_diagnostics(page)
    counts = [
        int(diagnostics.get("tbody_tr_count") or 0),
        int(diagnostics.get("row_count") or 0),
        len(diagnostics.get("visible_case_numbers", []) or []),
        len(diagnostics.get("case_related_anchors", []) or []),
    ]
    return max(counts)


def _resolve_runtime_view_order(runtime_views: list[str], settings: Any) -> list[str]:
    preferred = list(_actionable_case_view_candidate_labels(settings))
    ordered: list[str] = []
    for candidate in preferred:
        for runtime_view in runtime_views:
            if runtime_view in ordered:
                continue
            if _matches_case_view_label(runtime_view, (candidate,)):
                ordered.append(runtime_view)
    for runtime_view in runtime_views:
        if runtime_view not in ordered:
            ordered.append(runtime_view)
    return ordered


def _open_and_select_runtime_view(page: Any, settings: Any, logger: logging.Logger, view_label: str) -> tuple[bool, str | None]:
    picker_opened, open_method = _open_list_view_picker(page, settings, logger)
    if not picker_opened:
        return False, open_method
    clicked, click_method = _select_target_view_from_open_picker(
        page,
        _RuntimeViewSettings(settings, view_label),
        logger,
    )
    return clicked, click_method or open_method


class _RuntimeViewSettings:
    def __init__(self, settings: Any, target_view: str) -> None:
        self.support_portal_target_case_view = target_view
        self.support_portal_target_case_view_aliases = (target_view,)


def ensure_cases_list_view(page: Any, settings: Any, logger: logging.Logger) -> dict[str, Any]:
    target_view = _normalize_view_label(settings.support_portal_target_case_view)
    acceptable_labels = _acceptable_case_view_labels(settings)
    diagnostics = {
        "target_case_view": target_view,
        "selected_case_view": None,
        "case_view_selection_attempted": False,
        "case_view_selection_successful": False,
        "empty_cases_view_detected": False,
        "empty_view_recovery_attempted": False,
        "empty_view_recovery_successful": False,
        "visible_case_numbers_after_view_selection": [],
        "visible_list_view_labels": [],
        "selected_tab_or_list_label": None,
        "no_items_to_display_present": False,
        "cases_action_required_present": False,
        "all_cases_present": False,
        "my_company_cases_present": False,
        "closed_cases_submitted_for_my_company_present": False,
        "visible_case_number_count": 0,
        "list_picker_found": False,
        "list_picker_opened": False,
        "list_picker_open_method": None,
        "available_list_views": [],
        "selected_view_before": None,
        "selected_view_after": None,
        "target_view_clicked": False,
        "target_view_click_method": None,
        "case_numbers_after_selection": [],
        "no_items_after_selection": False,
        "target_view_not_selected": False,
    }

    _wait_for_cases_ready(page, timeout_seconds=settings.support_portal_session_check_timeout_seconds)
    page.wait_for_timeout(1500)
    runtime_before = _collect_list_view_runtime_snapshot(page, settings)
    diagnostics.update(runtime_before)
    initial_diag = _extract_case_view_diagnostics(page, settings)
    diagnostics.update(initial_diag)
    diagnostics["selected_view_before"] = runtime_before.get("selected_view_before")
    diagnostics["selected_view_after"] = runtime_before.get("selected_view_before")

    attempted_views: list[dict[str, Any]] = []
    initial_row_count = _count_visible_case_row_signals(page)
    if initial_row_count > 0:
        diagnostics["case_view_selection_successful"] = True
        diagnostics["selected_case_view"] = diagnostics.get("selected_case_view") or diagnostics.get("selected_view_before")
        attempted_views.append(
            {
                "view": diagnostics.get("selected_case_view"),
                "row_count": initial_row_count,
                "reason": "initial_view_has_rows",
            }
        )
    else:
        diagnostics["case_view_selection_attempted"] = True
        runtime_views = runtime_before.get("available_list_views", [])
        candidate_views = _resolve_runtime_view_order(runtime_views, settings)
        for view_label in candidate_views:
            clicked, click_method = _open_and_select_runtime_view(page, settings, logger, view_label)
            if not clicked:
                attempted_views.append({"view": view_label, "row_count": 0, "reason": "not_clickable"})
                continue

            diagnostics["list_picker_opened"] = True
            diagnostics["target_view_clicked"] = True
            diagnostics["target_view_click_method"] = click_method
            diagnostics["selected_case_view"] = view_label
            page.wait_for_timeout(2500)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            _try_refresh_cases_view(page)
            page.wait_for_timeout(2000)
            row_count = _count_visible_case_row_signals(page)
            selected_runtime = _collect_list_view_runtime_snapshot(page, settings)
            selected_diag = _extract_case_view_diagnostics(page, settings)
            diagnostics.update(selected_runtime)
            diagnostics.update(selected_diag)
            diagnostics["selected_view_after"] = selected_runtime.get("selected_view_after")
            diagnostics["case_numbers_after_selection"] = selected_runtime.get("case_numbers_after_selection", [])
            diagnostics["no_items_after_selection"] = selected_runtime.get("no_items_after_selection", False)
            attempted_views.append(
                {
                    "view": view_label,
                    "row_count": row_count,
                    "reason": "selected_with_rows" if row_count > 0 else "selected_but_empty",
                }
            )
            if row_count > 0:
                diagnostics["case_view_selection_successful"] = True
                diagnostics["selected_case_view"] = selected_diag.get("selected_case_view") or view_label
                diagnostics["empty_cases_view_detected"] = False
                break

    diagnostics["empty_cases_view_detected"] = bool(diagnostics.get("no_items_to_display_present"))
    if diagnostics["empty_cases_view_detected"] and not diagnostics.get("visible_case_numbers_after_view_selection"):
        diagnostics["empty_view_recovery_attempted"] = True
        _try_refresh_cases_view(page)
        page.wait_for_timeout(5000)
        picker_opened, open_method = _open_list_view_picker(page, settings, logger)
        if picker_opened:
            diagnostics["list_picker_opened"] = True
            diagnostics["list_picker_open_method"] = diagnostics.get("list_picker_open_method") or open_method
            selection_success, click_method = _select_target_view_from_open_picker(page, settings, logger)
            if selection_success:
                diagnostics["target_view_clicked"] = True
                diagnostics["target_view_click_method"] = diagnostics.get("target_view_click_method") or click_method
                page.wait_for_timeout(1500)
        try:
            page.mouse.wheel(0, 900)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        try:
            page.reload(wait_until="domcontentloaded", timeout=settings.support_portal_page_load_timeout_seconds * 1000)
        except Exception:
            logger.debug("Cases page reload during empty-view recovery failed", exc_info=True)
        page.wait_for_timeout(2500)
        picker_opened, open_method = _open_list_view_picker(page, settings, logger)
        if picker_opened:
            diagnostics["list_picker_opened"] = True
            diagnostics["list_picker_open_method"] = diagnostics.get("list_picker_open_method") or open_method
            selection_success, click_method = _select_target_view_from_open_picker(page, settings, logger)
            if selection_success:
                diagnostics["target_view_clicked"] = True
                diagnostics["target_view_click_method"] = diagnostics.get("target_view_click_method") or click_method
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                _try_refresh_cases_view(page)
                page.wait_for_timeout(2000)
        recovery_diag = _extract_case_view_diagnostics(page, settings)
        diagnostics.update(recovery_diag)
        runtime_recovery = _collect_list_view_runtime_snapshot(page, settings)
        diagnostics.update(runtime_recovery)
        diagnostics["selected_view_after"] = runtime_recovery.get("selected_view_after")
        diagnostics["case_numbers_after_selection"] = runtime_recovery.get("case_numbers_after_selection", [])
        diagnostics["no_items_after_selection"] = runtime_recovery.get("no_items_after_selection", False)
        diagnostics["empty_view_recovery_successful"] = bool(diagnostics.get("visible_case_numbers_after_view_selection"))

    if attempted_views:
        diagnostics["attempted_views"] = attempted_views
        diagnostics["available_views"] = runtime_before.get("available_list_views", [])
        diagnostics["view_selection_reason"] = next(
            (entry["reason"] for entry in reversed(attempted_views) if entry.get("row_count", 0) > 0),
            attempted_views[-1]["reason"],
        )

    diagnostics["target_view_not_selected"] = (
        diagnostics.get("selected_case_view") is not None
        and not _matches_case_view_label(str(diagnostics.get("selected_case_view")), acceptable_labels)
    )
    if not diagnostics.get("selected_case_view"):
        diagnostics["selected_case_view"] = (
            diagnostics.get("selected_view_after")
            or diagnostics.get("selected_view_before")
            or diagnostics.get("current_selected_view_text")
        )
    if not diagnostics.get("selected_tab_or_list_label"):
        diagnostics["selected_tab_or_list_label"] = diagnostics.get("selected_case_view")
    diagnostics["target_view_not_selected"] = (
        diagnostics.get("selected_case_view") is not None
        and not _matches_case_view_label(str(diagnostics.get("selected_case_view")), acceptable_labels)
    )

    logger.info(
        "cases_view_diagnostics current_url=%s page_title=%s target_case_view=%s selected_case_view=%s list_picker_found=%s list_picker_opened=%s list_picker_open_method=%s target_view_clicked=%s target_view_click_method=%s target_view_not_selected=%s no_items_to_display=%s cases_action_required_present=%s all_cases_present=%s my_company_cases_present=%s closed_cases_submitted_for_my_company_present=%s visible_case_number_count=%s available_list_views=%s",
        diagnostics.get("current_url"),
        diagnostics.get("page_title"),
        diagnostics.get("target_case_view"),
        diagnostics.get("selected_case_view"),
        diagnostics.get("list_picker_found"),
        diagnostics.get("list_picker_opened"),
        diagnostics.get("list_picker_open_method"),
        diagnostics.get("target_view_clicked"),
        diagnostics.get("target_view_click_method"),
        diagnostics.get("target_view_not_selected"),
        diagnostics.get("no_items_to_display_present"),
        diagnostics.get("cases_action_required_present"),
        diagnostics.get("all_cases_present"),
        diagnostics.get("my_company_cases_present"),
        diagnostics.get("closed_cases_submitted_for_my_company_present"),
        diagnostics.get("visible_case_number_count"),
        diagnostics.get("available_list_views"),
    )
    return diagnostics


def _is_authenticated_cases_page(page: Any) -> bool:
    shell = _detect_auth_shell_signals(page)
    url = page.url or ""
    title = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    if _detect_login(page) or shell.get("login_link_visible") or shell.get("logged_out_shell_detected"):
        return False
    if shell.get("authenticated_nav_visible"):
        return True
    if shell.get("public_case_shell_detected") and not shell.get("authenticated_nav_visible"):
        return False
    if _safe_visible_case_numbers(page):
        return True
    body_preview = _page_text_preview(page, limit=1500).lower()
    authenticated_markers = ("partner tools", "my company's case", "my company's cases", "action required")
    return "/s/case/case" in url.lower() and any(marker in body_preview for marker in authenticated_markers)


def _auth_state_from_signals(
    *,
    session_reused: bool = False,
    current_url: str = "",
    page_title: str = "",
    visible_case_numbers: list[str] | None = None,
    extracted_raw_cases: int = 0,
    successful_authenticated_navigation: bool = False,
    login_link_visible: bool = False,
    logged_out_shell_detected: bool = False,
    public_case_shell_detected: bool = False,
    authenticated_nav_visible: bool = False,
) -> dict[str, Any]:
    visible_case_numbers = visible_case_numbers or []
    normalized_url = (current_url or "").lower()
    normalized_title = (page_title or "").lower()
    weak_auth_signals_ignored = []
    blocker_reason = None
    if login_link_visible:
        blocker_reason = "login_link_visible"
    elif _looks_like_login(normalized_url):
        blocker_reason = "login_url_visible"
    elif normalized_title == "login":
        blocker_reason = "login_title_visible"
    elif logged_out_shell_detected or public_case_shell_detected:
        blocker_reason = "logged_out_public_shell"

    if blocker_reason:
        if "/s/case/case" in (current_url or "").lower():
            weak_auth_signals_ignored.append("url")
        if "case list" in (page_title or "").lower():
            weak_auth_signals_ignored.append("title")
        if public_case_shell_detected:
            weak_auth_signals_ignored.append("public_case_shell")
        return {
            "authenticated_via_session": False,
            "authenticated_via_visible_cases": False,
            "authenticated_via_url": False,
            "authenticated_via_title": False,
            "auth_state_confidence": 0,
            "auth_confidence_score": 0,
            "final_authenticated": False,
            "login_link_visible": bool(login_link_visible),
            "logged_out_shell_detected": bool(logged_out_shell_detected or public_case_shell_detected),
            "public_case_shell_detected": bool(public_case_shell_detected),
            "auth_rejected_reason": blocker_reason,
            "weak_auth_signals_ignored": weak_auth_signals_ignored,
        }
    authenticated_via_session = bool(session_reused)
    authenticated_via_url = False
    authenticated_via_title = False
    authenticated_via_visible_cases = bool(visible_case_numbers) or extracted_raw_cases > 0
    authenticated_via_nav = bool(authenticated_nav_visible)
    confidence = sum(
        1
        for flag in (
            authenticated_via_session,
            authenticated_via_nav,
            authenticated_via_visible_cases,
            bool(successful_authenticated_navigation),
        )
        if flag
    )
    final_authenticated = confidence > 0 and (
        authenticated_via_visible_cases
        or authenticated_via_nav
        or authenticated_via_session
        or bool(successful_authenticated_navigation)
    )
    if authenticated_via_session and not (
        authenticated_via_visible_cases
        or authenticated_via_nav
        or bool(successful_authenticated_navigation)
    ):
        final_authenticated = False
        weak_auth_signals_ignored.append("session_without_strong_signals")
        confidence = 0
    return {
        "authenticated_via_session": authenticated_via_session,
        "authenticated_via_visible_cases": authenticated_via_visible_cases,
        "authenticated_via_url": authenticated_via_url,
        "authenticated_via_title": authenticated_via_title,
        "auth_state_confidence": confidence,
        "auth_confidence_score": confidence,
        "final_authenticated": final_authenticated,
        "login_link_visible": bool(login_link_visible),
        "logged_out_shell_detected": bool(logged_out_shell_detected),
        "public_case_shell_detected": bool(public_case_shell_detected),
        "auth_rejected_reason": "",
        "weak_auth_signals_ignored": weak_auth_signals_ignored,
    }


def _looks_like_manual_auth_required(page: Any, *, password_submission_attempted: bool = False) -> bool:
    classification = _classify_auth_surface(page, password_submission_attempted=password_submission_attempted)
    return bool(classification.get("classification"))


def _detect_login_blocker_reason(page: Any, *, password_submission_attempted: bool = False) -> str | None:
    classification = _classify_auth_surface(page, password_submission_attempted=password_submission_attempted)
    return str(classification.get("classification") or "") or None


def _iter_login_contexts(page: Any) -> list[tuple[str, Any]]:
    contexts: list[tuple[str, Any]] = [("page", page)]
    try:
        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            contexts.append((f"frame[{index}]", frame))
    except Exception:
        pass
    return contexts


def _find_login_field_in_contexts(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 0) -> tuple[str | None, Any | None, bool]:
    for context_name, context in _iter_login_contexts(page):
        locator, found = _find_first_visible_locator(context, selectors, timeout_ms=timeout_ms)
        if found and locator is not None:
            return context_name, locator, True
    return None, None, False


def _find_role_button_in_contexts(page: Any, role_names: tuple[str, ...]) -> tuple[str | None, Any | None, bool]:
    for context_name, context in _iter_login_contexts(page):
        for text in role_names:
            try:
                candidate = context.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.IGNORECASE))
                if _is_visible_enabled(candidate):
                    return context_name, candidate.first, True
            except Exception:
                continue
            try:
                candidate = context.get_by_role("button", name=re.compile(re.escape(text), re.IGNORECASE))
                if _is_visible_enabled(candidate):
                    return context_name, candidate.first, True
            except Exception:
                continue
            try:
                candidate = context.get_by_role("link", name=re.compile(re.escape(text), re.IGNORECASE))
                if _is_visible_enabled(candidate):
                    return context_name, candidate.first, True
            except Exception:
                continue
            try:
                candidate = context.get_by_role("button", name=re.compile(re.escape(text.replace(" ", "")), re.IGNORECASE))
                if _is_visible_enabled(candidate):
                    return context_name, candidate.first, True
            except Exception:
                continue
    return None, None, False


def _find_checkbox_in_contexts(page: Any, labels: tuple[str, ...]) -> tuple[str | None, Any | None, bool]:
    for context_name, context in _iter_login_contexts(page):
        for label in labels:
            patterns = (
                ("role", lambda: context.get_by_role("checkbox", name=re.compile(re.escape(label), re.IGNORECASE))),
                ("label", lambda: context.get_by_label(re.compile(re.escape(label), re.IGNORECASE))),
                ("text", lambda: context.get_by_text(re.compile(re.escape(label), re.IGNORECASE))),
            )
            for _kind, factory in patterns:
                try:
                    locator = factory()
                    if _is_visible_enabled(locator):
                        return context_name, locator.first, True
                except Exception:
                    continue
    return None, None, False


def _find_first_visible_locator(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 0) -> tuple[Any | None, bool]:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                candidate = locator.first
                if timeout_ms > 0:
                    candidate.wait_for(state="visible", timeout=timeout_ms)
                elif candidate.is_visible() and candidate.is_enabled():
                    return candidate, True
                if timeout_ms > 0:
                    return candidate, True
        except Exception:
            continue
    return None, False


def _discover_auth_field_with_retries(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    diagnostics: dict[str, Any],
    selectors: tuple[str, ...],
    field_kind: str,
    initial_stage: str,
    timeout_seconds: int,
) -> tuple[str | None, Any | None, bool]:
    started_at = time.time()
    deadline = time.time() + max(1, int(timeout_seconds))
    attempts = 0
    last_stage = initial_stage
    diagnostics.setdefault("field_discovery_attempts", {})
    diagnostics.setdefault("field_discovery_elapsed_seconds", {})
    diagnostics.setdefault("field_stage_changed_during_wait", {})
    diagnostics.setdefault("field_discovery_final_stage", {})

    while True:
        attempts += 1
        context_name, locator, found = _find_login_field_in_contexts(page, selectors, timeout_ms=0)
        stage_info = _detect_auth_stage(
            page,
            password_submission_attempted=bool(diagnostics.get("password_submission_attempted")),
        )
        _record_auth_stage(diagnostics, page, stage_info)
        last_stage = stage_info["stage"]
        if found and locator is not None:
            diagnostics["field_discovery_attempts"][field_kind] = attempts
            diagnostics["field_discovery_elapsed_seconds"][field_kind] = round(max(0.0, time.time() - started_at), 3)
            diagnostics["field_stage_changed_during_wait"][field_kind] = last_stage != initial_stage
            diagnostics["field_discovery_final_stage"][field_kind] = last_stage
            return context_name, locator, True
        if last_stage != initial_stage:
            break
        if time.time() >= deadline:
            break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=250)
        except Exception:
            logger.debug("Field discovery domcontentloaded wait failed", exc_info=True)
        page.wait_for_timeout(250)

    diagnostics["field_discovery_attempts"][field_kind] = attempts
    diagnostics["field_discovery_elapsed_seconds"][field_kind] = round(max(0.0, time.time() - started_at), 3)
    diagnostics["field_stage_changed_during_wait"][field_kind] = last_stage != initial_stage
    diagnostics["field_discovery_final_stage"][field_kind] = last_stage
    return None, None, False


def _maybe_apply_okta_keep_signed_in(page: Any, settings: Any, diagnostics: dict[str, Any]) -> None:
    requested = bool(getattr(settings, "support_portal_okta_keep_signed_in", True))
    diagnostics["okta_keep_signed_in_requested"] = requested
    diagnostics.setdefault("okta_keep_signed_in_visible", False)
    diagnostics.setdefault("okta_keep_signed_in_applied", False)
    context_name, checkbox, found = _find_checkbox_in_contexts(page, OKTA_KEEP_SIGNED_IN_LABELS)
    diagnostics["okta_keep_signed_in_visible"] = found
    if not found or checkbox is None:
        return
    try:
        current_state = checkbox.is_checked()
    except Exception:
        current_state = None
    try:
        if requested and current_state is False:
            checkbox.check(timeout=5000)
            diagnostics["okta_keep_signed_in_applied"] = True
        elif not requested and current_state is True:
            checkbox.uncheck(timeout=5000)
            diagnostics["okta_keep_signed_in_applied"] = True
        else:
            diagnostics["okta_keep_signed_in_applied"] = current_state == requested
    except Exception:
        diagnostics["okta_keep_signed_in_applied"] = False


def _mask_username(username: str | None) -> str:
    if not username:
        return ""
    if len(username) <= 4:
        return "*" * len(username)
    return f"{username[:2]}***{username[-2:]}"


def _credentials_available(settings: Any) -> bool:
    return bool(getattr(settings, "support_portal_username", None) and getattr(settings, "support_portal_password", None))


def _is_operator_action_reason(reason: str | None) -> bool:
    return str(reason or "") in {"mfa_detected", "okta_device_challenge", "captcha_detected"}


def _is_recoverable_manual_auth_reason(reason: str | None) -> bool:
    normalized = str(reason or "")
    if normalized in {
        "mfa_detected",
        "okta_device_challenge",
        "captcha_detected",
        "public_logged_out_shell",
        "auto_login_disabled",
        "credentials_missing",
        "login_timeout",
        "returned_to_logged_out_shell",
    }:
        return True
    return normalized.startswith("login_page_navigation_failed:")


def _wait_for_auth_transition(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    diagnostics: dict[str, Any],
    previous_stage: str,
    timeout_seconds: int,
    password_submission_attempted: bool,
) -> dict[str, Any]:
    deadline = time.time() + max(3, int(timeout_seconds))
    previous_url = _redact_url_for_logs(page.url)
    previous_title = _safe_page_title(page)
    while time.time() < deadline:
        stage_info = _detect_auth_stage(page, password_submission_attempted=password_submission_attempted)
        _record_auth_stage(diagnostics, page, stage_info)
        if stage_info["stage"] != previous_stage:
            return stage_info
        if stage_info["safe_url"] != previous_url or stage_info["page_title"] != previous_title:
            return stage_info
        try:
            page.wait_for_load_state("domcontentloaded", timeout=500)
        except Exception:
            pass
        page.wait_for_timeout(500)
    diagnostics["auth_timeout_stage"] = previous_stage
    return _detect_auth_stage(page, password_submission_attempted=password_submission_attempted)


def _submit_support_portal_credentials(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    cases_url: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = dict(diagnostics or {})
    credentials = _resolve_auth_credentials(settings)
    username = credentials["username"]
    password = credentials["password"]
    diagnostics.update(
        {
            "credential_source": credentials["credential_source"],
            "username_available": credentials["username_available"],
            "password_available": credentials["password_available"],
            "username_length": credentials["username_length"],
            "password_length": credentials["password_length"],
            "credential_warnings": credentials["credential_warnings"],
        }
    )

    diagnostics["login_page_url"] = page.url
    diagnostics["login_page_title"] = _safe_page_title(page)
    diagnostics["login_current_url"] = page.url
    diagnostics["login_page_reached"] = bool(_detect_login(page))
    diagnostics.setdefault("field_discovery_attempts", {})
    diagnostics.setdefault("field_discovery_elapsed_seconds", {})
    diagnostics.setdefault("field_stage_changed_during_wait", {})
    diagnostics.setdefault("field_discovery_final_stage", {})
    stage_info = _detect_auth_stage(page)
    _record_auth_stage(diagnostics, page, stage_info)
    diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
    diagnostics["support_portal_login_form_reached"] = stage_info["stage"] == AUTH_STAGE_SUPPORT_PORTAL_LOGIN_FORM
    diagnostics["okta_username_stage_reached"] = stage_info["stage"] == AUTH_STAGE_OKTA_USERNAME
    diagnostics["okta_password_stage_reached"] = stage_info["stage"] == AUTH_STAGE_OKTA_PASSWORD
    if stage_info["stage"] in {AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL, AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING, AUTH_STAGE_SAML_REDIRECT}:
        diagnostics["login_step"] = "wait_for_login_completion"
        login_completion = _wait_for_login_completion(
            page,
            settings,
            logger,
            cases_url=cases_url,
            login_url_before_submit=diagnostics["login_page_url"],
            login_title_before_submit=diagnostics["login_page_title"],
            diagnostics=diagnostics,
        )
        diagnostics.update(login_completion)
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        return diagnostics

    diagnostics["login_step"] = "locate_username"
    username_context_name = None
    username_field = None
    username_found = False
    password_context_name, password_field, password_found = _discover_auth_field_with_retries(
        page,
        settings,
        logger,
        diagnostics=diagnostics,
        selectors=PASSWORD_SELECTORS,
        field_kind="password",
        initial_stage=stage_info["stage"],
        timeout_seconds=3,
    )
    if stage_info["stage"] != AUTH_STAGE_OKTA_PASSWORD:
        username_context_name, username_field, username_found = _discover_auth_field_with_retries(
            page,
            settings,
            logger,
            diagnostics=diagnostics,
            selectors=USERNAME_SELECTORS,
            field_kind="username",
            initial_stage=stage_info["stage"],
            timeout_seconds=8,
        )
        if not username_found:
            stage_info = _detect_auth_stage(
                page,
                password_submission_attempted=bool(diagnostics.get("password_submission_attempted")),
            )
            _record_auth_stage(diagnostics, page, stage_info)
            diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
            diagnostics["okta_username_stage_reached"] = diagnostics.get("okta_username_stage_reached") or stage_info["stage"] == AUTH_STAGE_OKTA_USERNAME
            diagnostics["okta_password_stage_reached"] = diagnostics.get("okta_password_stage_reached") or stage_info["stage"] == AUTH_STAGE_OKTA_PASSWORD
    else:
        diagnostics["field_discovery_final_stage"]["username"] = stage_info["stage"]
        diagnostics["field_stage_changed_during_wait"]["username"] = False
        diagnostics["field_discovery_attempts"]["username"] = 0
        diagnostics["field_discovery_elapsed_seconds"]["username"] = 0.0
    diagnostics["username_field_found"] = username_found
    diagnostics["password_field_found"] = password_found
    diagnostics["login_frame_used"] = bool(username_context_name and username_context_name != "page") or bool(
        password_context_name and password_context_name != "page"
    )
    logger.info("username_field_found=%s", username_found)

    if username_found and username_field is not None and username:
        diagnostics["login_step"] = "fill_username"
        username_field.fill("")
        username_field.fill(username)
        diagnostics["username_filled"] = True
    elif stage_info["stage"] == AUTH_STAGE_OKTA_USERNAME:
        diagnostics["login_requires_manual_intervention"] = True
        diagnostics["auth_check_reason"] = "username_field_not_found"
        diagnostics["auto_login_skipped_reason"] = diagnostics.get("auto_login_skipped_reason") or "username_field_not_found"
        diagnostics["login_failure_reason"] = _detect_login_blocker_reason(page) or "username_field_not_found"
        diagnostics["post_submit_classification"] = diagnostics["login_failure_reason"]
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_STEP_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_STEP_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        return diagnostics
    elif stage_info["stage"] in {AUTH_STAGE_SAML_REDIRECT, AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING, AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL}:
        diagnostics["login_step"] = "wait_for_login_completion"
        login_completion = _wait_for_login_completion(
            page,
            settings,
            logger,
            cases_url=cases_url,
            login_url_before_submit=diagnostics["login_page_url"],
            login_title_before_submit=diagnostics["login_page_title"],
            diagnostics=diagnostics,
        )
        diagnostics.update(login_completion)
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        return diagnostics

    diagnostics["login_step"] = "locate_password_or_next"
    diagnostics["login_frame_used"] = diagnostics["login_frame_used"] or bool(password_context_name and password_context_name != "page")

    if not password_found:
        stage_info = _detect_auth_stage(
            page,
            password_submission_attempted=bool(diagnostics.get("password_submission_attempted")),
        )
        _record_auth_stage(diagnostics, page, stage_info)
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        if stage_info["stage"] == AUTH_STAGE_OKTA_USERNAME:
            diagnostics["okta_username_stage_reached"] = True
            _maybe_apply_okta_keep_signed_in(page, settings, diagnostics)
        diagnostics["login_step"] = "locate_next"
        next_context_name, next_locator, next_found = _find_login_field_in_contexts(page, NEXT_SELECTORS, timeout_ms=1000)
        if not next_found:
            next_context_name, next_locator, next_found = _find_role_button_in_contexts(page, OKTA_USERNAME_BUTTON_PATTERNS)
        diagnostics["next_button_found"] = next_found
        diagnostics["login_frame_used"] = diagnostics["login_frame_used"] or bool(next_context_name and next_context_name != "page")
        if next_found and next_locator is not None:
            try:
                next_locator.click(timeout=5000)
                diagnostics["next_clicked"] = True
                diagnostics["okta_username_submitted"] = True
                diagnostics["login_step"] = "next_clicked"
                _wait_for_auth_transition(
                    page,
                    settings,
                    logger,
                    diagnostics=diagnostics,
                    previous_stage=stage_info["stage"],
                    timeout_seconds=20,
                    password_submission_attempted=False,
                )
            except Exception:
                diagnostics["next_clicked"] = False
        diagnostics["login_current_url"] = page.url
        diagnostics["login_step"] = "locate_password"
        next_stage = _detect_auth_stage(page, password_submission_attempted=False)
        _record_auth_stage(diagnostics, page, next_stage)
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        if next_stage["stage"] in {AUTH_STAGE_SAML_REDIRECT, AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING, AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL}:
            diagnostics["login_step"] = "wait_for_login_completion"
            login_completion = _wait_for_login_completion(
                page,
                settings,
                logger,
                cases_url=cases_url,
                login_url_before_submit=diagnostics["login_page_url"],
                login_title_before_submit=diagnostics["login_page_title"],
                diagnostics=diagnostics,
            )
            diagnostics.update(login_completion)
            diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
            return diagnostics
        password_context_name, password_field, password_found = _discover_auth_field_with_retries(
            page,
            settings,
            logger,
            diagnostics=diagnostics,
            selectors=PASSWORD_SELECTORS,
            field_kind="password",
            initial_stage=next_stage["stage"],
            timeout_seconds=15,
        )
        diagnostics["login_frame_used"] = diagnostics["login_frame_used"] or bool(password_context_name and password_context_name != "page")
        diagnostics["password_field_found"] = password_found
        if password_found:
            diagnostics["okta_password_stage_reached"] = True

    diagnostics["password_field_found"] = password_found
    logger.info("password_field_found=%s", password_found)

    if not password_found or password_field is None:
        diagnostics["login_requires_manual_intervention"] = True
        diagnostics["auth_check_reason"] = "password_field_not_found"
        diagnostics["auto_login_skipped_reason"] = diagnostics.get("auto_login_skipped_reason") or "password_field_not_found"
        diagnostics["login_failure_reason"] = _detect_login_blocker_reason(page) or "password_field_not_found"
        diagnostics["post_submit_classification"] = diagnostics["login_failure_reason"]
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_STEP_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_STEP_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        return diagnostics

    diagnostics["login_step"] = "fill_password"
    password_field.fill("")
    password_field.fill(password)
    diagnostics["password_filled"] = True
    diagnostics["okta_password_stage_reached"] = diagnostics.get("okta_password_stage_reached") or stage_info["stage"] == AUTH_STAGE_OKTA_PASSWORD
    _maybe_apply_okta_keep_signed_in(page, settings, diagnostics)

    diagnostics["login_step"] = "locate_submit"
    submit_context_name, submit_locator, submit_found = _find_login_field_in_contexts(page, SUBMIT_SELECTORS, timeout_ms=5000)
    if not submit_found:
        submit_context_name, submit_locator, submit_found = _find_role_button_in_contexts(page, OKTA_PASSWORD_BUTTON_PATTERNS)
    diagnostics["login_frame_used"] = diagnostics["login_frame_used"] or bool(submit_context_name and submit_context_name != "page")
    diagnostics["submit_button_found"] = submit_found

    if submit_found and submit_locator is not None:
        diagnostics["login_step"] = "submit_click"
        submit_locator.click()
        diagnostics["login_submit_clicked"] = True
        diagnostics["submit_clicked"] = True
    else:
        try:
            diagnostics["login_step"] = "submit_enter"
            password_field.press("Enter")
            diagnostics["login_submit_clicked"] = True
            diagnostics["submit_clicked"] = True
        except Exception:
            diagnostics["login_submit_clicked"] = False
            diagnostics["submit_clicked"] = False

    diagnostics["password_submission_attempted"] = bool(diagnostics["login_submit_clicked"])
    diagnostics["okta_password_submitted"] = bool(diagnostics["login_submit_clicked"])
    logger.info("login_submit_clicked=%s", diagnostics["login_submit_clicked"])
    diagnostics["login_step"] = "wait_for_login_completion"
    login_completion = _wait_for_login_completion(
        page,
        settings,
        logger,
        cases_url=cases_url,
        login_url_before_submit=diagnostics["login_page_url"],
        login_title_before_submit=diagnostics["login_page_title"],
        diagnostics=diagnostics,
    )
    diagnostics.update(login_completion)
    diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
    diagnostics["post_login_url"] = page.url
    diagnostics["post_login_title"] = _safe_page_title(page)
    diagnostics["login_current_url"] = page.url
    diagnostics["post_submit_classification"] = str(
        login_completion.get("classification")
        or login_completion.get("login_failure_reason")
        or ("authenticated_success" if login_completion.get("login_successful") else "")
    )
    post_login_shell = _detect_auth_shell_signals(page)
    diagnostics.update(
        {
            k: post_login_shell.get(k)
            for k in ("login_link_visible", "logged_out_shell_detected", "public_case_shell_detected", "authenticated_nav_visible")
        }
    )
    diagnostics["login_successful"] = bool(login_completion.get("login_successful"))
    diagnostics["final_authenticated"] = bool(login_completion.get("final_authenticated") or diagnostics["login_successful"])
    diagnostics["login_success_check_result"] = bool(login_completion.get("login_success_check_result"))
    diagnostics["login_requires_manual_intervention"] = bool(
        login_completion.get("login_requires_manual_intervention")
        or (
            not diagnostics["login_successful"]
            and (_looks_like_manual_auth_required(page) or _detect_login(page) or diagnostics.get("login_link_visible"))
        )
    )
    if diagnostics["login_successful"]:
        diagnostics["auth_check_reason"] = "authenticated_cases_page_detected"
        diagnostics["login_step"] = "login_success"
        diagnostics["authenticated_support_portal_detected"] = True
        diagnostics["authenticated_detected"] = True
    elif diagnostics["submit_button_found"] and diagnostics["login_submit_clicked"]:
        diagnostics["auth_check_reason"] = diagnostics["auth_check_reason"] or "login_submit_completed_but_auth_not_verified"
        diagnostics["login_failure_reason"] = (
            diagnostics["login_failure_reason"]
            or _detect_login_blocker_reason(page, password_submission_attempted=True)
            or ("returned_to_logged_out_shell" if diagnostics.get("login_link_visible") else "unknown_login_flow")
        )
    else:
        diagnostics["auth_check_reason"] = diagnostics["auth_check_reason"] or "submit_action_unavailable"
        diagnostics["login_failure_reason"] = diagnostics["login_failure_reason"] or "submit_button_not_found"
    if not diagnostics["login_successful"]:
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_STEP_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_STEP_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
    logger.info(
        "post_submit_classification=%s login_successful=%s",
        diagnostics.get("post_submit_classification"),
        diagnostics["login_successful"],
    )
    return diagnostics


def _wait_for_login_completion(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    cases_url: str,
    login_url_before_submit: str,
    login_title_before_submit: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.time() + max(5, int(settings.support_portal_login_timeout_seconds))
    transition_reason = "login_timeout"
    diagnostics = diagnostics or {}
    diagnostics.setdefault("stable_public_shell_poll_count", 0)
    last_public_shell_signature = None

    while time.time() < deadline:
        current_url = page.url
        current_title = _safe_page_title(page)
        shell = _detect_auth_shell_signals(page)
        auth_surface = _collect_visible_auth_surface(page)
        classification = _classify_auth_surface(
            page,
            auth_surface=auth_surface,
            shell_signals=shell,
            password_submission_attempted=True,
        )
        stage_info = _detect_auth_stage(
            page,
            auth_surface=auth_surface,
            shell_signals=shell,
            password_submission_attempted=True,
        )
        _record_auth_stage(diagnostics, page, stage_info)
        diagnostics.update(_auth_surface_summary(page, diagnostics=diagnostics))
        _update_post_submit_diagnostics(
            page,
            diagnostics,
            auth_surface=auth_surface,
            shell_signals=shell,
        )

        if _is_authenticated_cases_page(page) or shell.get("authenticated_nav_visible"):
            return {
                "login_successful": True,
                "final_authenticated": True,
                "post_login_url": current_url,
                "post_login_title": current_title,
                "login_success_check_result": True,
                "login_failure_reason": "",
                "auth_check_reason": "authenticated_support_portal_shell_detected",
                "login_requires_manual_intervention": False,
                "classification": "authenticated_success",
                "post_submit_classification": "authenticated_success",
                "authenticated_support_portal_detected": True,
                "authenticated_detected": True,
                "public_shell_detected": False,
                "stable_public_shell_poll_count": int(diagnostics.get("stable_public_shell_poll_count") or 0),
                **_captcha_default_diagnostics(),
            }

        if stage_info["stage"] in {
            AUTH_STAGE_SAML_REDIRECT,
            AUTH_STAGE_SUPPORT_PORTAL_REDIRECT_LOADING,
            AUTH_STAGE_OKTA_USERNAME,
            AUTH_STAGE_OKTA_PASSWORD,
            AUTH_STAGE_SUPPORT_PORTAL_LOGIN_FORM,
        }:
            if stage_info["stage"] == AUTH_STAGE_SAML_REDIRECT:
                diagnostics["saml_redirect_detected"] = True
            transition_reason = (
                "saml_redirect_in_progress"
                if stage_info["stage"] == AUTH_STAGE_SAML_REDIRECT
                else f"{stage_info['stage']}_in_progress"
            )
            diagnostics["stable_public_shell_poll_count"] = 0
            last_public_shell_signature = None
            page.wait_for_timeout(500)
            continue

        blocker_reason = str(classification.get("classification") or "")
        if blocker_reason:
            if blocker_reason == "public_logged_out_shell":
                strong_public_shell = _is_strong_public_logged_out_shell(shell_signals=shell, diagnostics=diagnostics)
                public_shell_signature = (
                    stage_info["stage"],
                    current_title,
                    shell.get("login_link_visible"),
                    shell.get("authenticated_nav_visible"),
                    diagnostics.get("authenticated_identity_visible"),
                    diagnostics.get("real_case_rows_visible"),
                    _redact_url_for_logs(current_url),
                )
                stable_signature = last_public_shell_signature == public_shell_signature
                if strong_public_shell and stable_signature:
                    diagnostics["stable_public_shell_poll_count"] = int(diagnostics.get("stable_public_shell_poll_count") or 0) + 1
                elif strong_public_shell:
                    diagnostics["stable_public_shell_poll_count"] = 1
                else:
                    diagnostics["stable_public_shell_poll_count"] = 0
                last_public_shell_signature = public_shell_signature if strong_public_shell else None
                transition_reason = "returned_to_logged_out_shell"
                if not strong_public_shell or int(diagnostics.get("stable_public_shell_poll_count") or 0) < 3:
                    page.wait_for_timeout(500)
                    continue
            return {
                "login_successful": False,
                "final_authenticated": False,
                "post_login_url": current_url,
                "post_login_title": current_title,
                "login_success_check_result": False,
                "login_failure_reason": blocker_reason,
                "auth_check_reason": str(classification.get("classification_reason") or "login_blocker_detected"),
                "login_requires_manual_intervention": True,
                "classification": blocker_reason,
                "classification_reason": classification.get("classification_reason", ""),
                "classification_evidence": classification.get("classification_evidence", ""),
                "login_form_visible": classification.get("login_form_visible", False),
                "login_error_visible": classification.get("login_error_visible", False),
                "login_error_text": classification.get("login_error_text", ""),
                "mfa_detected": classification.get("mfa_detected", False),
                "mfa_type": classification.get("mfa_type", ""),
                "invalid_credentials_detected": classification.get("invalid_credentials_detected", False),
                "account_locked_detected": classification.get("account_locked_detected", False),
                "password_expired_detected": classification.get("password_expired_detected", False),
                "public_shell_detected": classification.get("public_shell_detected", False),
                "authenticated_detected": classification.get("authenticated_detected", False),
                "password_submission_attempted": True,
                "credential_error_visible": classification.get("credential_error_visible", False),
                "credential_error_text_safe": classification.get("credential_error_text_safe", ""),
                "invalid_credentials_evidence": classification.get("invalid_credentials_evidence", ""),
                "stable_public_shell_poll_count": int(diagnostics.get("stable_public_shell_poll_count") or 0),
                **_captcha_result_to_diagnostics(_detect_visible_captcha_state(page, auth_surface=auth_surface)),
            }

        login_page_still_visible = _detect_login(page)
        if login_page_still_visible:
            inputs_gone = False
            try:
                _, username_still_visible = _find_first_visible_locator(page, USERNAME_SELECTORS, timeout_ms=0)
                _, password_still_visible = _find_first_visible_locator(page, PASSWORD_SELECTORS, timeout_ms=0)
                inputs_gone = not username_still_visible and not password_still_visible
            except Exception:
                inputs_gone = False

            url_changed = current_url != login_url_before_submit
            title_changed = current_title and current_title != login_title_before_submit
            if inputs_gone or url_changed or title_changed:
                transition_reason = "login_page_transition_detected"
            diagnostics["stable_public_shell_poll_count"] = 0
            last_public_shell_signature = None
            page.wait_for_timeout(500)
            continue

        if shell.get("login_link_visible"):
            transition_reason = "returned_to_logged_out_shell"
            diagnostics["stable_public_shell_poll_count"] = 0
            last_public_shell_signature = None
            page.wait_for_timeout(500)
            continue

        diagnostics["stable_public_shell_poll_count"] = 0
        last_public_shell_signature = None
        page.wait_for_timeout(500)

    diagnostics["auth_timeout_stage"] = diagnostics.get("last_auth_stage") or AUTH_STAGE_UNKNOWN
    timeout_classification = _classify_auth_surface(
        page,
        timeout_reason=transition_reason,
        password_submission_attempted=True,
    )
    return {
        "login_successful": False,
        "final_authenticated": False,
        "post_login_url": page.url,
        "post_login_title": _safe_page_title(page),
        "login_success_check_result": False,
        "login_failure_reason": str(timeout_classification.get("classification") or transition_reason),
        "auth_check_reason": str(timeout_classification.get("classification_reason") or transition_reason),
        "login_requires_manual_intervention": True,
        "classification": str(timeout_classification.get("classification") or ""),
        "classification_reason": str(timeout_classification.get("classification_reason") or ""),
        "classification_evidence": str(timeout_classification.get("classification_evidence") or ""),
        "login_form_visible": bool(timeout_classification.get("login_form_visible")),
        "login_error_visible": bool(timeout_classification.get("login_error_visible")),
        "login_error_text": str(timeout_classification.get("login_error_text") or ""),
        "mfa_detected": bool(timeout_classification.get("mfa_detected")),
        "mfa_type": str(timeout_classification.get("mfa_type") or ""),
        "invalid_credentials_detected": bool(timeout_classification.get("invalid_credentials_detected")),
        "account_locked_detected": bool(timeout_classification.get("account_locked_detected")),
        "password_expired_detected": bool(timeout_classification.get("password_expired_detected")),
        "public_shell_detected": bool(timeout_classification.get("public_shell_detected")),
        "authenticated_detected": bool(timeout_classification.get("authenticated_detected")),
        "password_submission_attempted": True,
        "credential_error_visible": bool(timeout_classification.get("credential_error_visible")),
        "credential_error_text_safe": str(timeout_classification.get("credential_error_text_safe") or ""),
        "invalid_credentials_evidence": str(timeout_classification.get("invalid_credentials_evidence") or ""),
        "stable_public_shell_poll_count": int(diagnostics.get("stable_public_shell_poll_count") or 0),
        **_captcha_result_to_diagnostics(
            _detect_visible_captcha_state(page, auth_surface=_collect_visible_auth_surface(page))
        ),
    }


def _run_support_portal_auth_state_machine(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    cases_url: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.time() + max(10, int(settings.support_portal_login_timeout_seconds))
    while time.time() < deadline:
        stage_info = _detect_auth_stage(
            page,
            password_submission_attempted=bool(diagnostics.get("password_submission_attempted")),
        )
        _record_auth_stage(diagnostics, page, stage_info)
        stage = stage_info["stage"]

        if stage == AUTH_STAGE_AUTHENTICATED_SUPPORT_PORTAL:
            diagnostics["login_successful"] = True
            diagnostics["final_authenticated"] = True
            diagnostics["authenticated_support_portal_detected"] = True
            diagnostics["authenticated_detected"] = True
            diagnostics["public_shell_detected"] = False
            diagnostics["post_submit_classification"] = "authenticated_success"
            return diagnostics

        if stage == AUTH_STAGE_SUPPORT_PORTAL_PUBLIC_SHELL:
            diagnostics["public_shell_detected"] = True
            diagnostics["public_shell_login_transition_attempted"] = True
            clicked = _click_visible_login_link(page)
            diagnostics["public_shell_login_transition_method"] = "visible_login_control" if clicked else "goto_login_url"
            if not clicked:
                page.goto(
                    settings.support_portal_login_url,
                    wait_until="domcontentloaded",
                    timeout=settings.support_portal_page_load_timeout_seconds * 1000,
                )
            _wait_for_auth_transition(
                page,
                settings,
                logger,
                diagnostics=diagnostics,
                previous_stage=stage,
                timeout_seconds=20,
                password_submission_attempted=False,
            )
            diagnostics["support_portal_login_form_reached"] = True
            continue

        if stage in {AUTH_STAGE_SUPPORT_PORTAL_LOGIN_FORM, AUTH_STAGE_OKTA_USERNAME, AUTH_STAGE_OKTA_PASSWORD}:
            submit_result = _submit_support_portal_credentials(
                page,
                settings,
                logger,
                cases_url=cases_url,
                diagnostics=diagnostics,
            )
            diagnostics.update(submit_result)
            return diagnostics

        if stage == AUTH_STAGE_SAML_REDIRECT:
            diagnostics["saml_redirect_detected"] = True
            _wait_for_auth_transition(
                page,
                settings,
                logger,
                diagnostics=diagnostics,
                previous_stage=stage,
                timeout_seconds=30,
                password_submission_attempted=True,
            )
            continue

        if stage == AUTH_STAGE_OKTA_MFA:
            diagnostics["mfa_detected"] = True
            diagnostics["login_failure_reason"] = diagnostics.get("login_failure_reason") or "mfa_detected"
            diagnostics["post_submit_classification"] = "mfa_detected"
            diagnostics["login_requires_manual_intervention"] = True
            return diagnostics

        if stage == AUTH_STAGE_OKTA_ERROR:
            failure_reason = stage_info.get("classification") or "unknown_okta_error"
            diagnostics["login_failure_reason"] = failure_reason
            diagnostics["post_submit_classification"] = failure_reason
            diagnostics["login_requires_manual_intervention"] = _is_recoverable_manual_auth_reason(failure_reason)
            return diagnostics

        page.wait_for_timeout(500)

    diagnostics["auth_timeout_stage"] = diagnostics.get("last_auth_stage") or AUTH_STAGE_UNKNOWN
    diagnostics["login_failure_reason"] = diagnostics.get("login_failure_reason") or "login_timeout"
    diagnostics["post_submit_classification"] = diagnostics.get("post_submit_classification") or diagnostics["login_failure_reason"]
    diagnostics["login_requires_manual_intervention"] = True
    return diagnostics


def _attempt_support_portal_auto_login_from_current_page(
    page: Any,
    settings: Any,
    logger: logging.Logger,
    *,
    cases_url: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return _run_support_portal_auth_state_machine(
        page,
        settings,
        logger,
        cases_url=cases_url,
        diagnostics=diagnostics,
    )


def _navigate_to_cases_after_auth(page: Any, settings: Any, logger: logging.Logger, *, cases_url: str, diagnostics: dict[str, Any]) -> None:
    current_url = (page.url or "").lower()
    if "/s/case/case" in current_url:
        return
    diagnostics["post_auth_navigation_to_cases_attempted"] = True
    page.goto(cases_url, wait_until="domcontentloaded", timeout=settings.support_portal_page_load_timeout_seconds * 1000)
    _wait_for_cases_ready(page, timeout_seconds=settings.support_portal_session_check_timeout_seconds)
    diagnostics["post_auth_navigation_to_cases_succeeded"] = "/s/case/case" in (page.url or "").lower()


def _attempt_support_portal_auto_login(page: Any, settings: Any, logger: logging.Logger, *, cases_url: str) -> dict[str, Any]:
    credentials = _resolve_auth_credentials(settings)
    diagnostics = {
        "auto_login_enabled": bool(settings.support_portal_auto_login_enabled),
        "credentials_available": bool(credentials["username_available"] and credentials["password_available"]),
        "credential_source": credentials["credential_source"],
        "username_available": credentials["username_available"],
        "password_available": credentials["password_available"],
        "username_length": credentials["username_length"],
        "password_length": credentials["password_length"],
        "credential_warnings": credentials["credential_warnings"],
        "login_attempted": False,
        "login_successful": False,
        "final_authenticated": False,
        "login_requires_manual_intervention": False,
        "login_frame_used": False,
        "login_page_url": "",
        "login_page_title": "",
        "login_step": "",
        "login_current_url": "",
        "username_field_found": False,
        "username_filled": False,
        "next_button_found": False,
        "next_clicked": False,
        "password_field_found": False,
        "password_filled": False,
        "submit_button_found": False,
        "login_submit_clicked": False,
        "submit_clicked": False,
        "post_login_url": "",
        "post_login_title": "",
        "login_success_check_result": False,
        "login_failure_reason": "",
        "auth_check_reason": "",
        "login_link_visible": False,
        "logged_out_shell_detected": False,
        "public_case_shell_detected": False,
        "auth_rejected_reason": "",
        "weak_auth_signals_ignored": [],
        "classification": "",
        "classification_reason": "",
        "classification_evidence": "",
        "login_form_visible": False,
        "login_error_visible": False,
        "login_error_text": "",
        "mfa_detected": False,
        "mfa_type": "",
        "invalid_credentials_detected": False,
        "account_locked_detected": False,
        "password_expired_detected": False,
        "public_shell_detected": False,
        "authenticated_detected": False,
        "public_shell_login_transition_attempted": False,
        "public_shell_login_transition_method": "",
        "login_page_reached": False,
        "post_submit_classification": "",
        "auto_login_skipped_reason": "",
        "support_portal_login_form_reached": False,
        "okta_username_stage_reached": False,
        "okta_username_submitted": False,
        "okta_keep_signed_in_visible": False,
        "okta_keep_signed_in_requested": bool(getattr(settings, "support_portal_okta_keep_signed_in", True)),
        "okta_keep_signed_in_applied": False,
        "okta_password_stage_reached": False,
        "okta_password_submitted": False,
        "password_submission_attempted": False,
        "credential_error_visible": False,
        "credential_error_text_safe": "",
        "invalid_credentials_evidence": "",
        "saml_redirect_detected": False,
        "authenticated_support_portal_detected": False,
        "post_auth_navigation_to_cases_attempted": False,
        "post_auth_navigation_to_cases_succeeded": False,
        "auth_stage_history": [],
        "auth_stage_transition_count": 0,
        "last_auth_stage": "",
        "last_safe_url_host": "",
        "last_page_title": "",
        "auth_timeout_stage": "",
        "post_submit_url_host": "",
        "post_submit_safe_path": "",
        "post_submit_title": "",
        "post_submit_visible_headings": [],
        "post_submit_visible_buttons": [],
        "stable_public_shell_poll_count": 0,
        "current_auth_hostname": "",
        "current_auth_path": "",
        "current_auth_safe_url": "",
        "current_auth_page_title": "",
        "current_detected_auth_stage": "",
        "visible_input_types": {},
        "username_input_count": 0,
        "password_input_count": 0,
        "visible_headings": [],
        "visible_enabled_buttons": [],
        "iframe_hostnames": [],
        "shadow_root_count": 0,
        "authenticated_identity_visible": False,
        "real_case_rows_visible": False,
        "field_discovery_attempts": {},
        "field_discovery_elapsed_seconds": {},
        "field_stage_changed_during_wait": {},
        "field_discovery_final_stage": {},
    }
    diagnostics.update(_captcha_default_diagnostics())
    if not settings.support_portal_auto_login_enabled:
        diagnostics["auth_check_reason"] = "auto_login_disabled"
        diagnostics["auto_login_skipped_reason"] = "auto_login_disabled"
        logger.info("auto_login_enabled=false credentials_available=%s", diagnostics["credentials_available"])
        return diagnostics
    if not diagnostics["credentials_available"]:
        logger.warning("login_attempt_started=false reason=missing_credentials")
        diagnostics["auth_check_reason"] = "credentials_missing"
        diagnostics["auto_login_skipped_reason"] = "credentials_missing"
        return diagnostics

    logger.info(
        "auto_login_enabled=%s credentials_available=%s",
        diagnostics["auto_login_enabled"],
        diagnostics["credentials_available"],
    )
    diagnostics["login_attempted"] = True
    logger.info("login_attempt_started=true username_masked=%s", _mask_username(credentials["username"]))
    return _attempt_support_portal_auto_login_from_current_page(
        page,
        settings,
        logger,
        cases_url=cases_url,
        diagnostics=diagnostics,
    )


def ensure_support_portal_authenticated(page: Any, settings: Any, logger: logging.Logger) -> dict[str, Any]:
    cases_url = settings.cases_url or f"{(settings.portal_url or 'https://support.example.com/s').rstrip('/')}/case/Case/Default?tabset-836f8=1"
    diagnostics = {
        "session_reused": False,
        "auto_login_enabled": bool(settings.support_portal_auto_login_enabled),
        "credentials_available": _credentials_available(settings),
        "login_page_detected": False,
        "login_link_visible": False,
        "logged_out_shell_detected": False,
        "public_case_shell_detected": False,
        "login_attempted": False,
        "login_successful": False,
        "login_requires_manual_intervention": False,
        "manual_login_used": False,
        "final_authenticated": False,
        "final_url": "",
        "final_page_title": "",
        "authenticated_via_session": False,
        "authenticated_via_visible_cases": False,
        "authenticated_via_url": False,
        "authenticated_via_title": False,
        "login_page_url": "",
        "login_page_title": "",
        "username_field_found": False,
        "password_field_found": False,
        "submit_button_found": False,
        "login_submit_clicked": False,
        "post_login_url": "",
        "post_login_title": "",
        "auth_check_reason": "",
        "auth_confidence_score": 0,
        "manual_login_reason": None,
        "auto_login_skipped_reason": None,
        "auth_rejected_reason": "",
        "weak_auth_signals_ignored": [],
        "auth_state_confidence": 0,
        "auth_decision_path": [],
        "extraction_succeeded_before_manual_fallback": False,
        "manual_fallback_suppressed_due_to_authenticated_state": False,
        "classification": "",
        "classification_reason": "",
        "classification_evidence": "",
        "login_form_visible": False,
        "login_error_visible": False,
        "login_error_text": "",
        "mfa_detected": False,
        "mfa_type": "",
        "invalid_credentials_detected": False,
        "account_locked_detected": False,
        "password_expired_detected": False,
        "public_shell_detected": False,
        "authenticated_detected": False,
        "public_shell_login_transition_attempted": False,
        "public_shell_login_transition_method": "",
        "login_page_reached": False,
        "post_submit_classification": "",
        "support_portal_login_form_reached": False,
        "okta_username_stage_reached": False,
        "okta_username_submitted": False,
        "okta_keep_signed_in_visible": False,
        "okta_keep_signed_in_requested": bool(getattr(settings, "support_portal_okta_keep_signed_in", True)),
        "okta_keep_signed_in_applied": False,
        "okta_password_stage_reached": False,
        "okta_password_submitted": False,
        "password_submission_attempted": False,
        "credential_error_visible": False,
        "credential_error_text_safe": "",
        "invalid_credentials_evidence": "",
        "saml_redirect_detected": False,
        "authenticated_support_portal_detected": False,
        "post_auth_navigation_to_cases_attempted": False,
        "post_auth_navigation_to_cases_succeeded": False,
        "auth_stage_history": [],
        "auth_stage_transition_count": 0,
        "last_auth_stage": "",
        "last_safe_url_host": "",
        "last_page_title": "",
        "auth_timeout_stage": "",
        "post_submit_url_host": "",
        "post_submit_safe_path": "",
        "post_submit_title": "",
        "post_submit_visible_headings": [],
        "post_submit_visible_buttons": [],
        "stable_public_shell_poll_count": 0,
        "current_auth_hostname": "",
        "current_auth_path": "",
        "current_auth_safe_url": "",
        "current_auth_page_title": "",
        "current_detected_auth_stage": "",
        "visible_input_types": {},
        "username_input_count": 0,
        "password_input_count": 0,
        "visible_headings": [],
        "visible_enabled_buttons": [],
        "iframe_hostnames": [],
        "shadow_root_count": 0,
        "authenticated_identity_visible": False,
        "real_case_rows_visible": False,
        "field_discovery_attempts": {},
        "field_discovery_elapsed_seconds": {},
        "field_stage_changed_during_wait": {},
        "field_discovery_final_stage": {},
    }
    diagnostics.update(_captcha_default_diagnostics())

    diagnostics["auth_decision_path"].append("session_reuse_check")
    page.goto(cases_url, wait_until="domcontentloaded", timeout=settings.support_portal_page_load_timeout_seconds * 1000)
    _wait_for_cases_ready(page, timeout_seconds=settings.support_portal_session_check_timeout_seconds)
    initial_shell = _detect_auth_shell_signals(page)
    diagnostics.update({k: initial_shell.get(k) for k in ("login_link_visible", "logged_out_shell_detected", "public_case_shell_detected")})

    if _is_authenticated_cases_page(page):
        diagnostics["session_reused"] = True
        diagnostics["final_url"] = page.url
        diagnostics["final_page_title"] = _safe_page_title(page)
        diagnostics["auth_check_reason"] = "session_reuse_authenticated_cases_page"
        diagnostics.update(
            _auth_state_from_signals(
                session_reused=True,
                current_url=diagnostics["final_url"],
                page_title=diagnostics["final_page_title"],
                login_link_visible=bool(diagnostics.get("login_link_visible")),
                logged_out_shell_detected=bool(diagnostics.get("logged_out_shell_detected")),
                public_case_shell_detected=bool(diagnostics.get("public_case_shell_detected")),
                authenticated_nav_visible=bool(initial_shell.get("authenticated_nav_visible")),
            )
        )
        logger.info("session_reused=true")
        return diagnostics

    diagnostics["auth_decision_path"].append("auth_detection")
    diagnostics["login_page_detected"] = bool(_detect_login(page) or diagnostics.get("logged_out_shell_detected"))
    diagnostics["login_page_url"] = page.url
    diagnostics["login_page_title"] = _safe_page_title(page)
    logger.info("login_page_detected=%s", diagnostics["login_page_detected"])

    diagnostics["auth_decision_path"].append("optional_auto_login")
    auto_login_diagnostics = _attempt_support_portal_auto_login(page, settings, logger, cases_url=cases_url)
    diagnostics.update(auto_login_diagnostics)
    if not settings.support_portal_auto_login_enabled:
        diagnostics["auto_login_skipped_reason"] = "auto_login_disabled"
    elif not settings.support_portal_username or not settings.support_portal_password:
        diagnostics["auto_login_skipped_reason"] = "credentials_missing"

    if auto_login_diagnostics.get("login_successful"):
        _navigate_to_cases_after_auth(page, settings, logger, cases_url=cases_url, diagnostics=diagnostics)
        diagnostics["final_url"] = page.url
        diagnostics["final_page_title"] = _safe_page_title(page)
        diagnostics.update(
            _auth_state_from_signals(
                session_reused=diagnostics["session_reused"],
                current_url=diagnostics["final_url"],
                page_title=diagnostics["final_page_title"],
                successful_authenticated_navigation=True,
                login_link_visible=bool(diagnostics.get("login_link_visible")),
                logged_out_shell_detected=bool(diagnostics.get("logged_out_shell_detected")),
                public_case_shell_detected=bool(diagnostics.get("public_case_shell_detected")),
                authenticated_nav_visible=bool(_detect_auth_shell_signals(page).get("authenticated_nav_visible")),
            )
        )
        return diagnostics

    diagnostics["auth_decision_path"].append("authenticated_verification")
    visible_case_numbers_found = _safe_visible_case_numbers(page)
    diagnostics["visible_case_numbers_found"] = visible_case_numbers_found
    verification_shell = _detect_auth_shell_signals(page)
    diagnostics.update({k: verification_shell.get(k) for k in ("login_link_visible", "logged_out_shell_detected", "public_case_shell_detected")})
    authenticated_signals = _auth_state_from_signals(
        session_reused=diagnostics["session_reused"],
        current_url=page.url,
        page_title=_safe_page_title(page),
        visible_case_numbers=visible_case_numbers_found,
        login_link_visible=bool(diagnostics.get("login_link_visible")),
        logged_out_shell_detected=bool(diagnostics.get("logged_out_shell_detected")),
        public_case_shell_detected=bool(diagnostics.get("public_case_shell_detected")),
        authenticated_nav_visible=bool(verification_shell.get("authenticated_nav_visible")),
    )
    diagnostics.update(authenticated_signals)
    if diagnostics["final_authenticated"]:
        _navigate_to_cases_after_auth(page, settings, logger, cases_url=cases_url, diagnostics=diagnostics)
        diagnostics["login_requires_manual_intervention"] = False
        diagnostics["manual_fallback_suppressed_due_to_authenticated_state"] = True
        diagnostics["auth_check_reason"] = diagnostics.get("auth_check_reason") or "visible_case_signals_detected"
        return diagnostics

    actual_login_blocker = (
        _looks_like_manual_auth_required(page)
        or _detect_login(page)
        or bool(diagnostics.get("logged_out_shell_detected"))
        or bool(diagnostics.get("public_case_shell_detected"))
    )
    failure_reason = (
        diagnostics.get("login_failure_reason")
        or diagnostics.get("auto_login_skipped_reason")
        or diagnostics.get("auth_check_reason")
    )
    manual_fallback_reason = failure_reason or ("public_logged_out_shell" if diagnostics.get("public_shell_detected") else None)
    manual_fallback_allowed = bool(
        (
            diagnostics.get("public_shell_detected")
            and diagnostics.get("auto_login_skipped_reason") in {"auto_login_disabled", "credentials_missing"}
        )
        or (
            diagnostics.get("login_attempted")
            and _is_recoverable_manual_auth_reason(manual_fallback_reason)
        )
    )
    diagnostics["login_requires_manual_intervention"] = bool(
        actual_login_blocker
        and not diagnostics["final_authenticated"]
        and not visible_case_numbers_found
        and manual_fallback_allowed
        and (
            diagnostics.get("login_attempted")
            or diagnostics.get("auto_login_skipped_reason") in {"auto_login_disabled", "credentials_missing"}
        )
        and not diagnostics.get("login_successful")
    )

    if diagnostics["login_requires_manual_intervention"]:
        diagnostics["auth_decision_path"].append("optional_manual_fallback")
        diagnostics["manual_login_reason"] = manual_fallback_reason or "login_page_or_manual_auth_required"
        logger.warning("login_requires_manual_intervention=true")
        logger.info(
            "manual_fallback_reason=%s auto_login_skipped_reason=%s login_attempted=%s final_authenticated=%s",
            diagnostics["manual_login_reason"],
            diagnostics.get("auto_login_skipped_reason"),
            diagnostics.get("login_attempted"),
            diagnostics.get("final_authenticated"),
        )
        failure_reason = diagnostics["manual_login_reason"]
        failure_category = "operator_action_required" if _is_operator_action_reason(failure_reason) else "authentication_failed"
        if settings.support_portal_browser_headless:
            screenshot_path, html_path = _capture_named_artifacts(
                page,
                screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
                html_path=LOGIN_FAILURE_HTML_PATH,
                settings=settings,
                failure=True,
            )
            raise SupportPortalBrowserScrapeError(
                f"Manual login required in the Playwright/Chrome for Testing browser, but browser is headless. "
                f"login_failure_reason={failure_reason} screenshot={screenshot_path} html={html_path}",
                category=failure_category,
                operator_action_required=_is_operator_action_reason(failure_reason),
                login_failure_reason=str(failure_reason),
                diagnostics=diagnostics,
            )
        if not settings.support_portal_manual_login_fallback or not _is_interactive_operator_session(settings):
            screenshot_path, html_path = _capture_named_artifacts(
                page,
                screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
                html_path=LOGIN_FAILURE_HTML_PATH,
                settings=settings,
                failure=True,
            )
            raise SupportPortalBrowserScrapeError(
                f"Automatic login could not complete in the Playwright/Chrome for Testing browser and "
                f"interactive bootstrap is unavailable. login_failure_reason={failure_reason} "
                f"screenshot={screenshot_path} html={html_path}",
                category=failure_category,
                operator_action_required=_is_operator_action_reason(failure_reason),
                login_failure_reason=str(failure_reason),
                diagnostics=diagnostics,
            )
        _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        _wait_for_operator_auth_completion(
            page,
            settings,
            logger,
            cases_url=cases_url,
            failure_reason=failure_reason,
            diagnostics=diagnostics,
        )
        diagnostics["manual_login_used"] = True
        _wait_for_cases_ready(page, timeout_seconds=settings.support_portal_login_timeout_seconds)
        if not _is_authenticated_cases_page(page):
            screenshot_path, html_path = _capture_named_artifacts(
                page,
                screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
                html_path=LOGIN_FAILURE_HTML_PATH,
                settings=settings,
                failure=True,
            )
            raise SupportPortalBrowserScrapeError(
                f"Login still not authenticated after interactive bootstrap in the Playwright/Chrome for Testing browser. "
                f"login_failure_reason={failure_reason} screenshot={screenshot_path} html={html_path}",
                category=failure_category,
                operator_action_required=_is_operator_action_reason(failure_reason),
                login_failure_reason=str(failure_reason),
                diagnostics=diagnostics,
            )
    elif actual_login_blocker and not diagnostics["final_authenticated"]:
        failure_reason = manual_fallback_reason or diagnostics.get("auth_check_reason") or "authentication_failed"
        screenshot_path, html_path = _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        raise SupportPortalBrowserScrapeError(
            f"Automatic login could not establish authenticated SupportPortal session. "
            f"login_failure_reason={failure_reason} screenshot={screenshot_path} html={html_path}",
            category="operator_action_required" if _is_operator_action_reason(failure_reason) else "authentication_failed",
            operator_action_required=_is_operator_action_reason(failure_reason),
            login_failure_reason=str(failure_reason),
            diagnostics=diagnostics,
        )

    diagnostics["final_url"] = page.url
    diagnostics["final_page_title"] = _safe_page_title(page)
    diagnostics["post_login_url"] = page.url
    diagnostics["post_login_title"] = diagnostics["final_page_title"]
    diagnostics.update(
        _auth_state_from_signals(
            session_reused=diagnostics["session_reused"],
            current_url=diagnostics["final_url"],
            page_title=diagnostics["final_page_title"],
            successful_authenticated_navigation=diagnostics["manual_login_used"] or diagnostics["login_successful"],
            login_link_visible=bool(diagnostics.get("login_link_visible")),
            logged_out_shell_detected=bool(diagnostics.get("logged_out_shell_detected")),
            public_case_shell_detected=bool(diagnostics.get("public_case_shell_detected")),
            authenticated_nav_visible=bool(_detect_auth_shell_signals(page).get("authenticated_nav_visible")),
        )
    )
    diagnostics["login_requires_manual_intervention"] = bool(
        diagnostics["login_requires_manual_intervention"] and not diagnostics["final_authenticated"]
    )
    if not diagnostics["final_authenticated"]:
        screenshot_path, html_path = _capture_named_artifacts(
            page,
            screenshot_path=LOGIN_FAILURE_SCREENSHOT_PATH,
            html_path=LOGIN_FAILURE_HTML_PATH,
            settings=settings,
            failure=True,
        )
        raise SupportPortalBrowserScrapeError(
            f"Unable to establish authenticated SupportPortal session. "
            f"login_failure_reason={diagnostics.get('login_failure_reason') or diagnostics.get('auth_check_reason')} "
            f"screenshot={screenshot_path} html={html_path}",
            category="authentication_failed",
            operator_action_required=_is_operator_action_reason(diagnostics.get("login_failure_reason")),
            login_failure_reason=str(diagnostics.get("login_failure_reason") or diagnostics.get("auth_check_reason")),
            diagnostics=diagnostics,
        )
    return diagnostics


def _validate_auth_state_consistency(
    diagnostics: dict[str, Any],
    *,
    parsed_case_count: int,
    visible_case_numbers: list[str],
    current_url: str,
    page_title: str,
) -> dict[str, Any]:
    extracted_raw_cases = int(diagnostics.get("extracted_raw_cases") or parsed_case_count or 0)
    updated = dict(diagnostics)
    shell_signals = {
        "login_link_visible": bool(updated.get("login_link_visible")),
        "logged_out_shell_detected": bool(updated.get("logged_out_shell_detected")),
        "public_case_shell_detected": bool(updated.get("public_case_shell_detected")),
        "authenticated_nav_visible": bool(updated.get("authenticated_nav_visible")),
    }
    updated.update(
        _auth_state_from_signals(
            session_reused=bool(updated.get("session_reused")),
            current_url=current_url,
            page_title=page_title,
            visible_case_numbers=visible_case_numbers,
            extracted_raw_cases=extracted_raw_cases,
            successful_authenticated_navigation=bool(updated.get("login_successful") or updated.get("manual_login_used")),
            **shell_signals,
        )
    )
    if _looks_like_login(current_url) or (page_title or "").strip().lower() == "login":
        updated["final_authenticated"] = False
        updated["login_requires_manual_intervention"] = True
        updated["auth_rejected_reason"] = "login_page_visible"
    if parsed_case_count > 0:
        updated["final_authenticated"] = True
        updated["login_requires_manual_intervention"] = False
        updated["extraction_succeeded_before_manual_fallback"] = True
        updated["manual_fallback_suppressed_due_to_authenticated_state"] = True
        updated["auth_check_reason"] = "parsed_cases_detected"
    if visible_case_numbers:
        updated["final_authenticated"] = True
        updated["login_requires_manual_intervention"] = False
        updated["auth_check_reason"] = updated.get("auth_check_reason") or "visible_case_numbers_detected"
    if updated.get("final_authenticated"):
        updated["login_requires_manual_intervention"] = False
    return updated


def _dom_diagnostics(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          };
          const preview = (text) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
          const regex = /\\b0\\d{7,}\\b/g;
          const visibleTexts = Array.from(document.querySelectorAll('body *'))
            .filter((el) => isVisible(el))
            .map((el) => preview(el.innerText || el.textContent || ''))
            .filter(Boolean);
          const numbers = [];
          for (const text of visibleTexts) {
            const matches = text.match(regex);
            if (!matches) continue;
            for (const match of matches) {
              if (!numbers.includes(match)) numbers.push(match);
            }
          }
          const anchors = Array.from(document.querySelectorAll('a[href]')).map((a) => ({
            href: a.getAttribute('href') || '',
            text: preview(a.innerText || a.textContent || ''),
            visible: isVisible(a),
          }));
          const classCounts = {};
          for (const el of Array.from(document.querySelectorAll('body *'))) {
            const className = typeof el.className === 'string' ? el.className.trim() : '';
            if (!className) continue;
            for (const token of className.split(/\\s+/)) {
              if (!token) continue;
              classCounts[token] = (classCounts[token] || 0) + 1;
            }
          }
          return {
            table_count: document.querySelectorAll('table').length,
            grid_count: document.querySelectorAll('[role="grid"]').length,
            row_count: document.querySelectorAll('[role="row"]').length,
            tbody_tr_count: document.querySelectorAll('tbody tr').length,
            div_role_row_count: document.querySelectorAll('div[role="row"]').length,
            lightning_datatable_count: document.querySelectorAll('lightning-datatable, c-case-custom-datatable').length,
            article_count: document.querySelectorAll('article').length,
            records_lwc_detail_panel_count: document.querySelectorAll('records-lwc-detail-panel').length,
            forcegenerated_flexipage_count: document.querySelectorAll('[class*="forcegenerated-flexipage"]').length,
            case_related_anchors: anchors.filter((a) => a.href.includes('/s/case/') || a.href.includes('/case/') || /^\\/500/i.test(a.href)),
            visible_case_numbers: numbers,
            first_20_visible_texts: visibleTexts.slice(0, 20),
            top_candidate_container_class_names: Object.entries(classCounts).sort((a, b) => b[1] - a[1]).slice(0, 20),
          };
        }
        """
    )


def _extract_method_payload(page: Any, method: str) -> list[dict[str, Any]]:
    script_by_method = {
        "structured_lightning_rows": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const rowSelectors = [
                'c-case-custom-datatable tbody tr',
                'lightning-datatable tbody tr',
                'tbody tr[role="row"]',
                'tbody tr',
                'tr[role="row"]'
              ];
              const rows = Array.from(document.querySelectorAll(rowSelectors.join(',')));
              return rows.map((row) => {
                const cells = Array.from(
                  row.querySelectorAll('th[role="rowheader"], td[data-label], td[role="gridcell"], div[role="gridcell"], lightning-primitive-cell-factory[data-label]')
                );
                const mapped = {};
                const cellDetails = [];
                for (const cell of cells) {
                  const label = (cell.getAttribute('data-label') || '').trim();
                  const text = (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim();
                  const value = cell.getAttribute('data-cell-value') || '';
                  const links = Array.from(cell.querySelectorAll('a[href]')).map((link) => ({
                    href: link.href,
                    text: (link.innerText || link.textContent || '').trim(),
                  }));
                  if (label && text) mapped[label] = text;
                  cellDetails.push({ label, text, value, links });
                }
                const links = Array.from(row.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                const text = (row.innerText || row.textContent || '').trim();
                const rect = row.getBoundingClientRect();
                return {
                  texts: text.split('\\n').map((item) => item.trim()).filter(Boolean),
                  mapped,
                  cell_details: cellDetails,
                  links,
                  html: row.innerHTML,
                  visible: isVisible(row),
                  text_length: text.length,
                  bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                  preview: text.replace(/\\s+/g, ' ').trim().slice(0, 240),
                };
              }).filter((row) => Object.keys(row.mapped || {}).length > 0 || row.links.length > 0);
            }
        """,
        "table_rows": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const tables = Array.from(document.querySelectorAll('table'));
              const results = [];
              for (const table of tables) {
                const headers = Array.from(table.querySelectorAll('thead th')).map((cell) => (cell.innerText || cell.textContent || '').trim());
                const rows = Array.from(table.querySelectorAll('tbody tr'));
                for (const row of rows) {
                  const cells = Array.from(row.querySelectorAll('th[role="rowheader"], td[data-label], td,[role="gridcell"]'));
                  const texts = cells.map((cell) => (cell.innerText || cell.textContent || '').trim()).filter(Boolean);
                  const mapped = {};
                  const cellDetails = [];
                  cells.forEach((cell, index) => {
                    const label = (cell.getAttribute('data-label') || headers[index] || '').trim();
                    const text = (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim();
                    const value = cell.getAttribute('data-cell-value') || '';
                    const links = Array.from(cell.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                    if (label && text) mapped[label] = text;
                    cellDetails.push({ label, text, value, links });
                  });
                  const links = Array.from(row.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                  const rect = row.getBoundingClientRect();
                  const rowText = (row.innerText || row.textContent || '').trim();
                  results.push({ texts, mapped, cell_details: cellDetails, links, html: row.innerHTML, visible: isVisible(row), text_length: rowText.length, bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, preview: rowText.replace(/\\s+/g, ' ').trim().slice(0, 180) });
                }
              }
              return results;
            }
        """,
        "lightning_rows": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const rows = Array.from(document.querySelectorAll('[role="grid"] [role="row"], lightning-datatable tbody tr, .slds-table tbody tr, c-case-custom-datatable tbody tr, div[role="row"]'));
              return rows.map((row) => {
                const cells = Array.from(row.querySelectorAll('th[role="rowheader"], [role="gridcell"], td[data-label], td, lightning-primitive-cell-factory[data-label], lightning-base-formatted-text, a[href]'));
                const texts = cells.map((cell) => (cell.innerText || cell.textContent || '').trim()).filter(Boolean);
                const mapped = {};
                const cellDetails = [];
                cells.forEach((cell) => {
                  const label = (cell.getAttribute('data-label') || '').trim();
                  const text = (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim();
                  const value = cell.getAttribute('data-cell-value') || '';
                  const links = Array.from(cell.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                  if (label && text) mapped[label] = text;
                  cellDetails.push({ label, text, value, links });
                });
                const links = Array.from(row.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                const rect = row.getBoundingClientRect();
                const rowText = (row.innerText || row.textContent || '').trim();
                return { texts, mapped, cell_details: cellDetails, links, html: row.innerHTML, visible: isVisible(row), text_length: rowText.length, bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, preview: rowText.replace(/\\s+/g, ' ').trim().slice(0, 180) };
              });
            }
        """,
        "role_grid_rows": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('[role="grid"] [role="row"]')).map((row) => {
                const cells = Array.from(row.querySelectorAll('[role="gridcell"], [role="rowheader"]'));
                const mapped = {};
                const cellDetails = [];
                cells.forEach((cell) => {
                  const label = (cell.getAttribute('data-label') || '').trim();
                  const text = (cell.innerText || cell.textContent || '').trim();
                  if (label) mapped[label] = text;
                  const value = cell.getAttribute('data-cell-value') || '';
                  const links = Array.from(cell.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                  cellDetails.push({ label, text, value, links });
                });
                const texts = cells.map((cell) => (cell.innerText || cell.textContent || '').trim()).filter(Boolean);
                const links = Array.from(row.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                const rect = row.getBoundingClientRect();
                const rowText = (row.innerText || row.textContent || '').trim();
                return { texts, mapped, cell_details: cellDetails, links, html: row.innerHTML, visible: isVisible(row), text_length: rowText.length, bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, preview: rowText.replace(/\\s+/g, ' ').trim().slice(0, 180) };
              });
            }
        """,
        "case_links": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const links = Array.from(document.querySelectorAll('a[href*="/s/case/"], a[href*="/case/"], a[href^="/500"]'));
              return links.map((link) => {
                const container = link.closest('tr,[role="row"],li,.slds-card,.slds-media,.forceCommunityRecordListDesktop,article,c-case-detail') || link.parentElement || link;
                const text = (container.innerText || container.textContent || '').trim();
                const rect = container.getBoundingClientRect();
                return {
                  texts: text.split('\\n').map((item) => item.trim()).filter(Boolean),
                  mapped: {},
                  links: [{ href: link.href, text: (link.innerText || link.textContent || '').trim() }],
                  html: container.innerHTML,
                  visible: isVisible(container),
                  text_length: text.length,
                  bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                  preview: text.replace(/\\s+/g, ' ').trim().slice(0, 180),
                };
              });
            }
        """,
        "cards_lists": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const nodes = Array.from(document.querySelectorAll('li, .slds-card, .slds-media, article, c-case-detail, [class*="forcegenerated-flexipage"]'));
              return nodes.map((node) => {
                const links = Array.from(node.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                const text = (node.innerText || node.textContent || '').trim();
                const rect = node.getBoundingClientRect();
                return {
                  texts: text.split('\\n').map((item) => item.trim()).filter(Boolean),
                  mapped: {},
                  links,
                  html: node.innerHTML,
                  visible: isVisible(node),
                  text_length: text.length,
                  bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                  preview: text.replace(/\\s+/g, ' ').trim().slice(0, 180),
                };
              });
            }
        """,
        "visible_text_case_numbers": """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const regex = /\\b0\\d{7,}\\b/;
              return Array.from(document.querySelectorAll('body *')).filter((node) => isVisible(node)).map((node) => {
                const text = (node.innerText || node.textContent || '').trim();
                if (!regex.test(text)) return null;
                const links = Array.from(node.querySelectorAll('a[href]')).map((link) => ({ href: link.href, text: (link.innerText || link.textContent || '').trim() }));
                const rect = node.getBoundingClientRect();
                return {
                  texts: text.split('\\n').map((item) => item.trim()).filter(Boolean),
                  mapped: {},
                  links,
                  html: node.innerHTML,
                  visible: true,
                  text_length: text.length,
                  bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                  preview: text.replace(/\\s+/g, ' ').trim().slice(0, 180),
                };
              }).filter(Boolean);
            }
        """,
    }
    return page.evaluate(script_by_method[method])


def _derive_case_number(texts: list[str], links: list[dict[str, Any]]) -> str | None:
    for link in links:
        text = str(link.get("text") or "").strip()
        if CASE_NUMBER_PATTERN.fullmatch(text):
            return text
    for text in texts:
        match = CASE_NUMBER_PATTERN.search(str(text or ""))
        if match:
            return match.group(0)
    return None


def _derive_subject(case_number: str | None, texts: list[str], links: list[dict[str, Any]]) -> str | None:
    for link in links:
        text = str(link.get("text") or "").strip()
        if text and text != case_number and not CASE_NUMBER_PATTERN.fullmatch(text):
            return text
    for text in texts:
        candidate = str(text or "").strip()
        if candidate and candidate != case_number and not CASE_NUMBER_PATTERN.fullmatch(candidate):
            return candidate
    return None


def _extract_subject_from_text_block(case_number: str, container_text: str) -> str:
    fallback = f"SupportPortal Case {case_number}"
    normalized = re.sub(r"\s+", " ", container_text).strip()
    if not normalized:
        return fallback

    match = re.search(
        rf"{re.escape(case_number)}\s+(.*?)(?=\s+(?:0\d{{7}}|\d{{8}})\b|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return fallback

    candidate = match.group(1).strip(" -:|")
    if not candidate:
        return fallback

    for marker in (
        "Severity",
        "Open Date",
        "Close Date",
        "Record Type",
        "Status",
        "Product Name",
        "Contact Name",
        "Case Number",
    ):
        marker_index = candidate.find(marker)
        if marker_index > 0:
            candidate = candidate[:marker_index].strip(" -:|")
            break

    return candidate or fallback


def _extract_labeled_value_near_case(case_number: str, container_text: str, label: str) -> str | None:
    normalized = re.sub(r"\s+", " ", container_text).strip()
    match = re.search(
        rf"{re.escape(case_number)}.*?{re.escape(label)}\s+(.+?)(?=\s+(?:Severity|Open Date|Close Date|Record Type|Status|Product Name|Contact Name|Case Number|0\d{{7}}|\d{{8}})\b|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = match.group(1).strip(" -:|")
    return value or None


def _normalize_line(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_global_ui_line(line: str) -> bool:
    normalized = _normalize_line(line).lower()
    if not normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in GLOBAL_HEADER_PREFIXES)


def _looks_like_datetime(line: str) -> bool:
    return bool(DATE_TIME_PATTERN.search(_normalize_line(line)))


def _looks_like_priority(line: str) -> bool:
    normalized = _normalize_line(line)
    return bool(re.match(r"^(Severity|Priority)\s+\d+", normalized, flags=re.IGNORECASE))


def _looks_like_status(line: str) -> bool:
    normalized = _normalize_line(line).lower()
    return any(snippet in normalized for snippet in KNOWN_STATUS_SNIPPETS)


def _looks_like_record_type(line: str) -> bool:
    return _normalize_line(line).lower() in KNOWN_RECORD_TYPES


def _looks_like_case_number(line: str) -> bool:
    return bool(CASE_NUMBER_PATTERN.fullmatch(_normalize_line(line)))


def _looks_like_person_name(line: str) -> bool:
    normalized = _normalize_line(line)
    if not normalized or any(char.isdigit() for char in normalized):
        return False
    if _looks_like_product_name(normalized):
        return False
    if any(token in normalized for token in (" AS", " A.S", " LTD", " LLC", " INC", " CORP", " BANK", " GROUP", " HOLDING")):
        return False
    words = normalized.split()
    return 2 <= len(words) <= 4 and all(word[:1].isupper() for word in words if word)


def _looks_like_product_name(line: str) -> bool:
    normalized = _normalize_line(line)
    lowered = normalized.lower()
    if not normalized:
        return False
    return any(product_key in lowered for product_key in PRODUCT_NORMALIZATION_MAP)


def _looks_like_account_name(line: str) -> bool:
    normalized = _normalize_line(line)
    if not normalized or any(char.isdigit() for char in normalized):
        return False
    if _looks_like_product_name(normalized) or _looks_like_person_name(normalized):
        return False
    account_markers = ("bank", "holding", "group", "as", "a.s", "ltd", "llc", "inc", "corp", "company", "co.")
    lowered = normalized.lower()
    return any(marker in lowered for marker in account_markers)


def _normalize_severity_label(value: str | None) -> str | None:
    normalized = _normalize_line(value)
    if not normalized:
        return None
    match = re.match(r"^(severity|priority)\s*[:\-]?\s*(\d+)$", normalized, flags=re.IGNORECASE)
    if match:
        return f"Severity {match.group(2)}"
    return normalized


def _normalize_subject_text(subject: str | None, *, product_name: str | None = None) -> str | None:
    normalized = _normalize_line(subject)
    if not normalized:
        return None
    normalized = re.sub(r"\s*[|:/-]\s*$", "", normalized)
    normalized = re.sub(r"\s*[-|:/]{2,}\s*", " - ", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    for prefix in ("Cases - Action Required", "Case Information", "All Cases"):
        if normalized.lower().startswith(prefix.lower()):
            normalized = normalized[len(prefix):].strip(" -:|")
    if product_name:
        product = _normalize_line(product_name)
        if product and normalized.lower() == product.lower():
            return None
        if product and normalized.lower().startswith(f"{product.lower()} - "):
            normalized = normalized[len(product) + 3 :].strip()
    return normalized.strip(" -:|") or None


def _normalize_product_name(value: str | None) -> str | None:
    normalized = _normalize_line(value)
    if not normalized:
        return None
    lowered = normalized.lower()
    for key, canonical in PRODUCT_NORMALIZATION_MAP.items():
        if key in lowered:
            return canonical
    return normalized


def _normalize_datetime_value(value: str | None) -> str | None:
    normalized = _normalize_line(value)
    if not normalized:
        return None
    for fmt in ("%m/%d/%Y, %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y, %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(normalized, fmt).isoformat(timespec="minutes")
        except ValueError:
            continue
    return normalized


def _recover_case_id(case: dict[str, Any]) -> str | None:
    for candidate in (
        case.get("case_id"),
        case.get("case_url"),
        ((case.get("raw") or {}).get("container_preview") if isinstance(case.get("raw"), dict) else None),
        " ".join((case.get("raw") or {}).get("row_block_lines", [])) if isinstance((case.get("raw") or {}).get("row_block_lines"), list) else None,
        str((case.get("metadata") or {}).get("row_html_preview") or ""),
    ):
        recovered = _extract_case_id(candidate if isinstance(candidate, str) else None) or _extract_case_id_from_text(candidate if isinstance(candidate, str) else None)
        if recovered:
            return recovered
    raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    for link in raw.get("links", []) if isinstance(raw.get("links"), list) else []:
        if isinstance(link, dict):
            recovered = _extract_case_id(str(link.get("href") or "")) or _extract_case_id_from_text(str(link.get("href") or ""))
            if recovered:
                return recovered
    return None


def normalize_case_fields(case: dict[str, Any]) -> dict[str, Any]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    normalized_fields: list[str] = []
    recovered_case_ids: list[str] = []
    recovered_product_names: list[str] = []
    contact_disambiguations: list[str] = []

    for field in ("case_number", "status", "record_type", "description", "contact", "account", "product_name", "subject", "case_owner", "action_required"):
        value = case.get(field)
        if isinstance(value, str):
            trimmed = _normalize_line(value)
            if trimmed != value:
                normalized_fields.append(field)
            case[field] = trimmed or None

    recovered_case_id = _recover_case_id(case)
    if recovered_case_id and recovered_case_id != case.get("case_id"):
        case["case_id"] = recovered_case_id
        recovered_case_ids.append(recovered_case_id)
        normalized_fields.append("case_id")

    normalized_product = _normalize_product_name(case.get("product_name"))
    if normalized_product and normalized_product != case.get("product_name"):
        case["product_name"] = normalized_product
        recovered_product_names.append(normalized_product)
        normalized_fields.append("product_name")

    if not case.get("product_name"):
        block_lines = raw.get("row_block_lines", []) if isinstance(raw.get("row_block_lines"), list) else []
        for line in block_lines:
            product_candidate = _normalize_product_name(line)
            if product_candidate and _looks_like_product_name(product_candidate):
                case["product_name"] = product_candidate
                recovered_product_names.append(product_candidate)
                normalized_fields.append("product_name")
                break

    if case.get("contact") and not _looks_like_person_name(str(case.get("contact"))):
        if _looks_like_account_name(str(case.get("contact"))):
            if not case.get("account"):
                case["account"] = case["contact"]
                normalized_fields.append("account")
            contact_disambiguations.append(f"moved_contact_to_account:{case['contact']}")
        elif _looks_like_product_name(str(case.get("contact"))):
            if not case.get("product_name"):
                case["product_name"] = _normalize_product_name(case["contact"])
                recovered_product_names.append(str(case["product_name"]))
                normalized_fields.append("product_name")
            contact_disambiguations.append(f"removed_contact_product:{case['contact']}")
        case["contact"] = None
        normalized_fields.append("contact")

    if case.get("account") and _looks_like_person_name(str(case.get("account"))):
        if not case.get("contact"):
            case["contact"] = case["account"]
            normalized_fields.append("contact")
        contact_disambiguations.append(f"moved_account_to_contact:{case['account']}")
        case["account"] = None
        normalized_fields.append("account")

    if not case.get("account"):
        block_lines = raw.get("row_block_lines", []) if isinstance(raw.get("row_block_lines"), list) else []
        for line in block_lines:
            if _looks_like_account_name(line):
                case["account"] = _normalize_line(line)
                normalized_fields.append("account")
                contact_disambiguations.append(f"recovered_account:{case['account']}")
                break

    case["priority"] = _normalize_severity_label(case.get("priority"))
    case["created_date"] = _normalize_datetime_value(case.get("created_date"))
    cleaned_subject = _normalize_subject_text(case.get("subject"), product_name=case.get("product_name"))
    if cleaned_subject != case.get("subject"):
        normalized_fields.append("subject")
    case["subject"] = cleaned_subject or f"SupportPortal Case {case.get('case_number')}"

    if case.get("status"):
        case["status"] = _normalize_line(case["status"]).title() if case["status"].islower() else _normalize_line(case["status"])
    if case.get("record_type"):
        case["record_type"] = _normalize_line(case["record_type"]).title()

    metadata["normalization_applied"] = True
    metadata["normalized_fields"] = sorted(set(normalized_fields))
    metadata["recovered_case_ids"] = recovered_case_ids
    metadata["recovered_product_names"] = recovered_product_names
    metadata["contact_disambiguations"] = contact_disambiguations
    case["metadata"] = metadata
    return case


def _segment_row_blocks_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    cleaned_lines = [_normalize_line(line) for line in lines if _normalize_line(line)]
    case_indices: list[tuple[int, str]] = []
    for index, line in enumerate(cleaned_lines):
        match = CASE_NUMBER_PATTERN.search(line)
        if match:
            case_indices.append((index, match.group(0)))

    blocks: list[dict[str, Any]] = []
    for block_index, (start_index, case_number) in enumerate(case_indices):
        end_index = case_indices[block_index + 1][0] if block_index + 1 < len(case_indices) else len(cleaned_lines)
        block_lines = cleaned_lines[start_index:end_index]
        blocks.append(
            {
                "case_number": case_number,
                "start_index": start_index,
                "end_index": end_index,
                "lines": block_lines,
                "preview": " | ".join(block_lines[:8])[:300],
            }
        )
    return blocks


def _select_case_link(case_number: str, subject: str | None, links: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    normalized_subject = _normalize_line(subject)
    for link in links:
        if not isinstance(link, dict):
            continue
        text = _normalize_line(link.get("text"))
        href = _normalize_line(link.get("href"))
        if text == case_number and re.search(r"/500[a-z0-9]{12,15}", href, flags=re.IGNORECASE):
            return href, _extract_case_id(href)
    if normalized_subject:
        for link in links:
            if not isinstance(link, dict):
                continue
            text = _normalize_line(link.get("text"))
            href = _normalize_line(link.get("href"))
            if text == normalized_subject and re.search(r"/500[a-z0-9]{12,15}", href, flags=re.IGNORECASE):
                return href, _extract_case_id(href)
    for link in links:
        if not isinstance(link, dict):
            continue
        href = _normalize_line(link.get("href"))
        if re.search(r"/500[a-z0-9]{12,15}", href, flags=re.IGNORECASE):
            return href, _extract_case_id(href)
    return None, None


def _parse_cards_list_subject_and_fields(
    block: dict[str, Any],
) -> tuple[str | None, dict[str, Any], list[str], list[str]]:
    lines = block.get("lines", [])
    subject_candidates: list[str] = []
    rejected_subject_candidates: list[str] = []
    parsed_fields: dict[str, Any] = {
        "priority": None,
        "status": None,
        "product_name": None,
        "created_date": None,
        "contact": None,
        "case_owner": None,
        "action_required": None,
        "record_type": None,
    }

    meaningful_lines: list[str] = []
    for raw_line in lines[1:]:
        line = _normalize_line(raw_line)
        if not line:
            continue
        if _is_global_ui_line(line):
            rejected_subject_candidates.append(line)
            continue
        if _looks_like_case_number(line):
            continue
        meaningful_lines.append(line)

    for line in meaningful_lines:
        if parsed_fields["priority"] is None and _looks_like_priority(line):
            parsed_fields["priority"] = line
            continue
        if parsed_fields["created_date"] is None and _looks_like_datetime(line):
            parsed_fields["created_date"] = line
            continue
        if parsed_fields["record_type"] is None and _looks_like_record_type(line):
            parsed_fields["record_type"] = line
            continue
        if parsed_fields["status"] is None and _looks_like_status(line):
            parsed_fields["status"] = line
            if "action required" in line.lower():
                parsed_fields["action_required"] = line
            continue

        subject_candidates.append(line)

    subject: str | None = None
    for candidate in subject_candidates:
        lowered = candidate.lower()
        if _looks_like_priority(candidate) or _looks_like_datetime(candidate) or _looks_like_record_type(candidate) or _looks_like_status(candidate):
            rejected_subject_candidates.append(candidate)
            continue
        if _looks_like_product_name(candidate) and parsed_fields["status"] is not None:
            if parsed_fields["product_name"] is None:
                parsed_fields["product_name"] = candidate
            rejected_subject_candidates.append(candidate)
            continue
        if _looks_like_person_name(candidate):
            if parsed_fields["contact"] is None:
                parsed_fields["contact"] = candidate
            rejected_subject_candidates.append(candidate)
            continue
        subject = candidate
        break

    if subject is None:
        for candidate in subject_candidates:
            if candidate not in rejected_subject_candidates:
                subject = candidate
                break

    if parsed_fields["product_name"] is None:
        for candidate in subject_candidates:
            if candidate == subject:
                continue
            if _looks_like_product_name(candidate):
                parsed_fields["product_name"] = candidate
                break

    if parsed_fields["contact"] is None:
        for candidate in reversed(subject_candidates):
            if candidate == subject:
                continue
            if _looks_like_person_name(candidate):
                parsed_fields["contact"] = candidate
                break

    return subject, parsed_fields, subject_candidates, rejected_subject_candidates


def _extract_cases_from_cards_list_rows(
    candidate_rows: list[dict[str, Any]],
    *,
    base_case_url: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parsed_cases: list[dict[str, Any]] = []
    parsed_row_blocks: list[dict[str, Any]] = []

    for row in candidate_rows:
        if not isinstance(row, dict):
            continue
        blocks = _segment_row_blocks_from_lines(row.get("texts") or [])
        for block in blocks:
            case_number = block["case_number"]
            subject, parsed_fields, subject_candidates, rejected_subject_candidates = _parse_cards_list_subject_and_fields(block)
            case_url, case_id = _select_case_link(case_number, subject, row.get("links") or [])
            if not case_number:
                continue

            extracted_field_sources = {
                "subject": "row_block_subject_candidates" if subject else None,
                "priority": "row_block_priority" if parsed_fields["priority"] else None,
                "status": "row_block_status" if parsed_fields["status"] else None,
                "product_name": "row_block_product_name" if parsed_fields["product_name"] else None,
                "created_date": "row_block_created_date" if parsed_fields["created_date"] else None,
                "contact_name": "row_block_contact_name" if parsed_fields["contact"] else None,
                "action_required": "row_block_status" if parsed_fields["action_required"] else None,
            }

            parsed_row_blocks.append(
                {
                    "case_number": case_number,
                    "row_block_preview": block["preview"],
                    "subject_candidates": subject_candidates,
                    "rejected_subject_candidates": rejected_subject_candidates,
                    "extracted_field_sources": extracted_field_sources,
                }
            )

            parsed_cases.append(
                normalize_case_fields({
                    "case_id": case_id or case_number,
                    "case_number": case_number,
                    "subject": subject or f"SupportPortal Case {case_number}",
                    "description": None,
                    "status": parsed_fields["status"],
                    "priority": parsed_fields["priority"],
                    "product_name": parsed_fields["product_name"],
                    "created_date": parsed_fields["created_date"],
                    "closed_date": None,
                    "created_by": None,
                    "account": None,
                    "contact": parsed_fields["contact"],
                    "record_type": parsed_fields["record_type"],
                    "owner_id": None,
                    "case_owner": parsed_fields["case_owner"],
                    "action_required": parsed_fields["action_required"],
                    "is_partner_customer": None,
                    "is_case_contact_user": None,
                    "last_modified_date": None,
                    "case_url": case_url or (f"{base_case_url.rstrip('/')}/{case_id}" if case_id else None),
                    "missing_fields": list(OPTIONAL_CASE_FIELDS),
                    "metadata": {
                        "missing_fields": list(OPTIONAL_CASE_FIELDS),
                        "source": "browser_scrape",
                        "extraction_method_used": "cards_lists",
                        "source_text_span": {
                            "start": block["start_index"],
                            "end": block["end_index"],
                        },
                        "parsed_row_blocks": len(blocks),
                        "row_block_preview": block["preview"],
                        "subject_candidates": subject_candidates,
                        "rejected_subject_candidates": rejected_subject_candidates,
                        "extracted_field_sources": extracted_field_sources,
                        "extracted_case_fields": {
                            "case_number": True,
                            "case_id": bool(case_id),
                            "subject": bool(subject),
                            "status": bool(parsed_fields["status"]),
                            "product_name": bool(parsed_fields["product_name"]),
                            "priority": bool(parsed_fields["priority"]),
                            "created_date": bool(parsed_fields["created_date"]),
                            "contact_name": bool(parsed_fields["contact"]),
                            "account_name": False,
                        },
                        "row_text_preview": block["preview"],
                    },
                    "raw": {
                        "container_preview": row.get("preview"),
                        "links": row.get("links") or [],
                        "row_block_lines": block["lines"],
                    },
                })
            )

    diagnostics = {
        "parsed_row_blocks": len(parsed_row_blocks),
        "row_block_preview": parsed_row_blocks[0]["row_block_preview"] if parsed_row_blocks else None,
        "subject_candidates": parsed_row_blocks[0]["subject_candidates"] if parsed_row_blocks else [],
        "rejected_subject_candidates": parsed_row_blocks[0]["rejected_subject_candidates"] if parsed_row_blocks else [],
        "extracted_field_sources": parsed_row_blocks[0]["extracted_field_sources"] if parsed_row_blocks else {},
    }
    return parsed_cases, diagnostics


def _normalize_subject_for_dedup(subject: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", str(subject or "")).strip().lower()
    return normalized or None


def _case_richness_score(case: dict[str, Any]) -> tuple[int, int, int, int]:
    populated_fields = 0
    for key in (
        "case_id",
        "case_number",
        "subject",
        "status",
        "priority",
        "product_name",
        "created_date",
        "account",
        "description",
        "case_url",
    ):
        value = case.get(key)
        if value not in (None, ""):
            populated_fields += 1

    normalized_subject = _normalize_subject_for_dedup(case.get("subject"))
    subject_quality = 0
    if normalized_subject:
        if not normalized_subject.startswith("support_portal case "):
            subject_quality = 2
        else:
            subject_quality = 1

    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    source_span = metadata.get("source_text_span")
    source_span_quality = 1 if isinstance(source_span, dict) and source_span.get("end", 0) > source_span.get("start", 0) else 0
    preview_length = len(str((case.get("raw") or {}).get("container_preview") or ""))
    return (populated_fields, subject_quality, source_span_quality, preview_length)


def _source_text_span(case: dict[str, Any]) -> tuple[int, int] | None:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    span = metadata.get("source_text_span")
    if not isinstance(span, dict):
        return None
    start = span.get("start")
    end = span.get("end")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        return (start, end)
    return None


def _spans_overlap(left: tuple[int, int] | None, right: tuple[int, int] | None) -> bool:
    if left is None or right is None:
        return False
    return left[0] < right[1] and right[0] < left[1]


def _cases_should_dedup(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_case_number = str(left.get("case_number") or "").strip()
    right_case_number = str(right.get("case_number") or "").strip()
    if left_case_number and right_case_number and left_case_number == right_case_number:
        return True

    left_case_id = str(left.get("case_id") or "").strip()
    right_case_id = str(right.get("case_id") or "").strip()
    if left_case_id and right_case_id and left_case_id == right_case_id:
        return True

    left_subject = _normalize_subject_for_dedup(left.get("subject"))
    right_subject = _normalize_subject_for_dedup(right.get("subject"))
    if left_subject and right_subject and left_subject == right_subject:
        return True

    return _spans_overlap(_source_text_span(left), _source_text_span(right))


def _deduplicate_scraped_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unique_cases: list[dict[str, Any]] = []
    duplicate_case_numbers: list[str] = []

    for case in cases:
        duplicate_index: int | None = None
        for index, existing in enumerate(unique_cases):
            if _cases_should_dedup(existing, case):
                duplicate_index = index
                case_number = str(case.get("case_number") or existing.get("case_number") or "").strip()
                if case_number and case_number not in duplicate_case_numbers:
                    duplicate_case_numbers.append(case_number)
                if _case_richness_score(case) > _case_richness_score(existing):
                    unique_cases[index] = case
                break
        if duplicate_index is None:
            unique_cases.append(case)

    diagnostics = {
        "extracted_raw_cases": len(cases),
        "unique_cases_after_dedup": len(unique_cases),
        "duplicate_cases_removed": max(0, len(cases) - len(unique_cases)),
        "duplicate_case_numbers": duplicate_case_numbers,
    }
    return unique_cases, diagnostics


def extract_cases_from_visible_container_text(
    candidate_rows: list[dict[str, Any]],
    *,
    base_case_url: str,
) -> tuple[list[dict[str, Any]], list[str], int]:
    if not candidate_rows:
        return [], [], 0

    largest_row = max(
        candidate_rows,
        key=lambda row: len(" ".join(str(text) for text in row.get("texts", []))),
    )
    container_text = " ".join(str(text) for text in largest_row.get("texts", []))
    container_text_length = len(container_text)
    numbers_in_order: list[str] = []
    matches_in_order = list(LIGHTWEIGHT_CASE_NUMBER_PATTERN.finditer(container_text))
    for match in matches_in_order:
        value = match.group(0)
        if value not in numbers_in_order:
            numbers_in_order.append(value)

    cases: list[dict[str, Any]] = []
    for match in matches_in_order:
        case_number = match.group(0)
        subject = _extract_subject_from_text_block(case_number, container_text)
        priority = _extract_labeled_value_near_case(case_number, container_text, "Severity")
        status = _extract_labeled_value_near_case(case_number, container_text, "Status")
        product_name = _extract_labeled_value_near_case(case_number, container_text, "Product Name")
        created_date = _extract_labeled_value_near_case(case_number, container_text, "Open Date")
        account = _extract_labeled_value_near_case(case_number, container_text, "Account")

        case_payload = normalize_case_fields({
            "case_id": case_number,
            "case_number": case_number,
            "subject": subject or f"SupportPortal Case {case_number}",
            "description": None,
            "status": status,
            "priority": priority,
            "product_name": product_name,
            "created_date": created_date,
            "closed_date": None,
            "created_by": None,
            "account": account,
            "contact": None,
            "record_type": None,
            "owner_id": None,
            "case_owner": None,
            "action_required": None,
            "is_partner_customer": None,
            "is_case_contact_user": None,
            "last_modified_date": None,
            "case_url": None,
            "missing_fields": list(OPTIONAL_CASE_FIELDS),
            "metadata": {
                "missing_fields": list(OPTIONAL_CASE_FIELDS),
                "source": "browser_scrape",
                "extraction_method_used": "visible_container_text",
                "container_text_length": container_text_length,
                "source_text_span": {
                    "start": match.start(),
                    "end": match.end(),
                },
            },
            "raw": {
                "container_preview": largest_row.get("preview"),
                "container_text": container_text,
            },
        })
        cases.append(case_payload)

    return cases, numbers_in_order, container_text_length


def _normalize_browser_case(
    row: dict[str, Any],
    *,
    base_case_url: str,
    extraction_method_used: str,
) -> dict[str, Any] | None:
    mapping = row.get("mapped_cells") if isinstance(row.get("mapped_cells"), dict) else {}
    texts = row.get("texts") if isinstance(row.get("texts"), list) else []
    links = row.get("links") if isinstance(row.get("links"), list) else []

    case_url = None
    for link in links:
        if not isinstance(link, dict):
            continue
        href = str(link.get("href") or "").strip()
        if re.search(r"/500[a-z0-9]{12,15}", href, flags=re.IGNORECASE) or "/case/" in href.lower():
            case_url = href
            break

    case_number = (
        mapping.get("case_number")
        or mapping.get("case")
        or _derive_case_number(texts, links)
    )
    if not case_number:
        return None

    subject = (
        mapping.get("subject")
        or mapping.get("case_subject")
        or _derive_subject(case_number, texts, links)
    )
    status = mapping.get("status")
    priority = mapping.get("priority") or mapping.get("severity")
    created_date = mapping.get("created_date") or mapping.get("open_date") or mapping.get("created") or mapping.get("date_created")
    description = mapping.get("description")
    account = mapping.get("account_name") or mapping.get("account") or mapping.get("customer") or mapping.get("company")
    contact = mapping.get("contact_name") or mapping.get("contact")
    product_name = mapping.get("product_name") or mapping.get("product")

    missing_fields = [
        field_name
        for field_name in OPTIONAL_CASE_FIELDS
        if {
            "Account": account,
            "Product_Name__c": product_name,
            "Action_Required__c": None,
            "Contact": contact,
            "Partners_Customer_Account__c": None,
        }.get(field_name) is None
    ]

    case_id = _extract_case_id(case_url)
    return normalize_case_fields({
        "case_id": case_id,
        "case_number": case_number,
        "subject": subject,
        "description": description,
        "status": status,
        "priority": priority,
        "product_name": product_name,
        "created_date": created_date,
        "closed_date": None,
        "created_by": None,
        "account": account,
        "contact": contact,
        "record_type": None,
        "owner_id": None,
        "case_owner": None,
        "action_required": None,
        "is_partner_customer": None,
        "is_case_contact_user": None,
        "last_modified_date": None,
        "case_url": case_url or (f"{base_case_url.rstrip('/')}/{case_id}" if case_id else None),
        "missing_fields": missing_fields,
        "metadata": {
            "missing_fields": missing_fields,
            "source": "browser_scrape",
            "extraction_method_used": extraction_method_used,
            "extracted_case_fields": {
                "case_number": bool(case_number),
                "case_id": bool(case_id),
                "subject": bool(subject),
                "status": bool(status),
                "product_name": bool(product_name),
                "priority": bool(priority),
                "created_date": bool(created_date),
                "contact_name": bool(contact),
                "account_name": bool(account),
            },
            "row_html_preview": str(row.get("html") or "")[:500],
            "row_text_preview": str(row.get("preview") or "")[:240],
        },
        "raw": row,
    })


def _choose_best_extraction(page: Any, *, base_case_url: str, settings: Any | None = None) -> tuple[list[dict[str, Any]], str, list[str], int, dict[str, Any]]:
    methods = ("structured_lightning_rows", "table_rows", "lightning_rows", "role_grid_rows", "case_links", "cards_lists", "visible_text_case_numbers")
    structured_methods = {"structured_lightning_rows", "table_rows", "lightning_rows", "role_grid_rows"}
    best_cases: list[dict[str, Any]] = []
    best_method = "none"
    best_visible_numbers: list[str] = []
    best_row_count = 0
    best_diagnostics: dict[str, Any] = {}

    for index, method in enumerate(methods, start=1):
        _save_selector_screenshot(page, index, settings=settings)
        raw_rows = _extract_method_payload(page, method)
        row_count = len(raw_rows)
        normalized_rows: list[dict[str, Any]] = []
        visible_numbers: list[str] = []
        method_extra_diagnostics: dict[str, Any] = {}

        if method == "cards_lists":
            normalized_rows, method_extra_diagnostics = _extract_cases_from_cards_list_rows(
                raw_rows,
                base_case_url=base_case_url,
            )
            for normalized in normalized_rows:
                case_number = normalized.get("case_number")
                if case_number and case_number not in visible_numbers:
                    visible_numbers.append(case_number)
        else:
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                mapped = {}
                for label, value in (row.get("mapped") or {}).items():
                    normalized_label = _normalize_header_name(str(label))
                    mapped[normalized_label] = str(value or "").strip() or None
                row["mapped_cells"] = mapped
                normalized = _normalize_browser_case(
                    row,
                    base_case_url=base_case_url,
                    extraction_method_used=method,
                )
                if normalized is not None:
                    normalized_rows.append(normalized)
                    case_number = normalized.get("case_number")
                    if case_number and case_number not in visible_numbers:
                        visible_numbers.append(case_number)

        deduped_rows, _ = _deduplicate_scraped_cases(normalized_rows)
        method_diagnostics = {
            "structured_rows_detected": row_count if method in structured_methods else 0,
            "structured_rows_parsed": len(deduped_rows) if method in structured_methods else 0,
            "structured_rows_failed": max(0, row_count - len(deduped_rows)) if method in structured_methods else 0,
            "extraction_strategy_used": method,
            "row_html_preview": str((raw_rows[0] or {}).get("html") or "")[:500] if raw_rows else None,
            "row_text_preview": str((raw_rows[0] or {}).get("preview") or "")[:240] if raw_rows else None,
            "extracted_case_fields": (deduped_rows[0].get("metadata", {}) or {}).get("extracted_case_fields") if deduped_rows else {},
        }
        method_diagnostics.update(method_extra_diagnostics)

        if method in structured_methods and deduped_rows:
            return deduped_rows, method, visible_numbers, row_count, method_diagnostics

        if len(deduped_rows) > len(best_cases):
            best_cases = deduped_rows
            best_method = method
            best_visible_numbers = visible_numbers
            best_row_count = row_count
            best_diagnostics = method_diagnostics

    return best_cases, best_method, best_visible_numbers, best_row_count, best_diagnostics


def inspect_support_portal_dom(page: Any) -> dict[str, Any]:
    diagnostics = _dom_diagnostics(page)
    methods = ("structured_lightning_rows", "table_rows", "lightning_rows", "role_grid_rows", "case_links", "cards_lists", "visible_text_case_numbers")
    diagnostics["selector_counts"] = {}
    diagnostics["selector_samples"] = {}
    for method in methods:
        rows = _extract_method_payload(page, method)
        diagnostics["selector_counts"][method] = len(rows)
        diagnostics["selector_samples"][method] = [
            {
                "visible": bool(row.get("visible")),
                "text_length": row.get("text_length", 0),
                "bounding_box": row.get("bounding_box"),
                "preview": row.get("preview"),
            }
            for row in rows[:10]
            if isinstance(row, dict)
        ]
    return diagnostics


def _fetch_cases_via_browser_once(settings: Any) -> BrowserScrapeResult:
    logger = logging.getLogger(__name__)
    cases_url = settings.cases_url or f"{(settings.portal_url or 'https://support.example.com/s').rstrip('/')}/case/Case/Default?tabset-836f8=1"
    profile_dir = settings.resolved_browser_profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)
    entrypoint_diagnostics = get_browser_entrypoint_diagnostics(settings)
    logger.info("browser_entrypoint_diagnostics=%s", entrypoint_diagnostics)

    try:
        context, browser_health = get_or_create_context(settings, logger)
    except SupportPortalBrowserManagerError as exc:
        raise SupportPortalBrowserScrapeError(
            str(exc),
            category=getattr(exc, "category", "browser_error"),
        ) from exc

    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_navigation_timeout(settings.support_portal_page_load_timeout_seconds * 1000)
        page.set_default_timeout(settings.support_portal_case_extraction_timeout_seconds * 1000)
        try:
            auth_diagnostics = ensure_support_portal_authenticated(page, settings, logger)
            _wait_for_cases_ready(page, timeout_seconds=DEFAULT_WAIT_SECONDS)
            case_view_diagnostics = ensure_cases_list_view(page, settings, logger)
            _scroll_page_for_virtualized_rows(page)
            login_detected = _detect_login(page)

            cases, extraction_method_used, visible_case_numbers, row_count, extraction_diagnostics = _choose_best_extraction(
                page,
                base_case_url=settings.portal_case_base_url,
                settings=settings,
            )
            current_url = page.url
            page_title = _safe_page_title(page)
            diagnostics = inspect_support_portal_dom(page)
            diagnostics.update(extraction_diagnostics)
            diagnostics.update(auth_diagnostics)
            diagnostics.update(case_view_diagnostics)
            diagnostics.update(browser_health)
            diagnostics.update(entrypoint_diagnostics)
            diagnostics["auth_state_domain_counts"] = _collect_cookie_domain_counts(context)

            if not cases:
                candidate_rows = _extract_method_payload(page, "cards_lists")
                text_cases, visible_numbers_from_text, container_text_length = extract_cases_from_visible_container_text(
                    candidate_rows,
                    base_case_url=settings.portal_case_base_url,
                )
                if text_cases:
                    cases = text_cases
                    extraction_method_used = "visible_container_text"
                    visible_case_numbers = visible_numbers_from_text
                    row_count = len(text_cases)
                    diagnostics = diagnostics or {}
                    diagnostics["visible_case_numbers_found"] = visible_numbers_from_text
                    diagnostics["visible_case_numbers_after_view_selection"] = visible_numbers_from_text
                    diagnostics["parsed_cases_from_text"] = len(text_cases)
                    diagnostics["container_text_length"] = container_text_length

            if not cases and not settings.support_portal_browser_headless and sys.stdin.isatty():
                _manual_cases_visibility_retry(page)
                _wait_for_cases_ready(page, timeout_seconds=DOM_POLL_SECONDS)
                _scroll_page_for_virtualized_rows(page)
                cases, extraction_method_used, visible_case_numbers, row_count, extraction_diagnostics = _choose_best_extraction(
                    page,
                    base_case_url=settings.portal_case_base_url,
                    settings=settings,
                )
                current_url = page.url
                page_title = _safe_page_title(page)
                diagnostics = inspect_support_portal_dom(page)
                diagnostics.update(extraction_diagnostics)
                diagnostics.update(auth_diagnostics)
                diagnostics.update(case_view_diagnostics)
                diagnostics.update(get_context_health(logger))
                diagnostics.update(entrypoint_diagnostics)
                diagnostics["auth_state_domain_counts"] = _collect_cookie_domain_counts(context)

            raw_case_count = len(cases)
            cases, dedup_diagnostics = _deduplicate_scraped_cases(cases)
            diagnostics = diagnostics or {}
            diagnostics.update(dedup_diagnostics)
            row_count = len(cases)
            diagnostics["final_url"] = current_url
            diagnostics["final_page_title"] = page_title
            diagnostics["resolved_browser_profile_dir"] = str(profile_dir)
            diagnostics["visible_case_numbers_after_view_selection"] = diagnostics.get(
                "visible_case_numbers_after_view_selection",
                visible_case_numbers,
            )
            diagnostics = _validate_auth_state_consistency(
                diagnostics,
                parsed_case_count=row_count,
                visible_case_numbers=visible_case_numbers,
                current_url=current_url,
                page_title=page_title,
            )
            diagnostics.update(get_context_health(logger))

            logger.info(
                "Browser scrape debug current_url=%s page_title=%s login_detected=%s session_reused=%s login_attempted=%s login_successful=%s manual_login_used=%s final_authenticated=%s auth_decision_path=%s extraction_succeeded_before_manual_fallback=%s manual_fallback_suppressed_due_to_authenticated_state=%s browser_pid=%s browser_uptime_seconds=%s browser_context_reused=%s browser_context_alive=%s active_pages_count=%s visible_case_numbers=%s row_count=%s extraction_method_used=%s extracted_raw_cases=%s unique_cases_after_dedup=%s",
                current_url,
                page_title,
                login_detected,
                diagnostics.get("session_reused"),
                diagnostics.get("login_attempted"),
                diagnostics.get("login_successful"),
                diagnostics.get("manual_login_used"),
                diagnostics.get("final_authenticated"),
                diagnostics.get("auth_decision_path"),
                diagnostics.get("extraction_succeeded_before_manual_fallback"),
                diagnostics.get("manual_fallback_suppressed_due_to_authenticated_state"),
                diagnostics.get("browser_pid"),
                diagnostics.get("browser_uptime_seconds"),
                diagnostics.get("browser_context_reused"),
                diagnostics.get("browser_context_alive"),
                diagnostics.get("active_pages_count"),
                visible_case_numbers[:10],
                row_count,
                extraction_method_used,
                raw_case_count,
                len(cases),
            )

            if not cases:
                if diagnostics.get("final_authenticated"):
                    _capture_named_artifacts(
                        page,
                        screenshot_path=EMPTY_VIEW_SCREENSHOT_PATH,
                        html_path=EMPTY_VIEW_HTML_PATH,
                        settings=settings,
                        failure=True,
                    )
                screenshot_path, html_path = _capture_named_artifacts(
                    page,
                    screenshot_path=FAILURE_SCREENSHOT_PATH,
                    html_path=FAILURE_HTML_PATH,
                    settings=settings,
                    failure=True,
                )
                logger.info("Browser scrape diagnostics: %s", json.dumps(diagnostics, ensure_ascii=False)[:4000])
                logger.error(
                    "Browser scrape failed to extract visible cases current_url=%s page_title=%s login_detected=%s row_count=%s extraction_method_used=%s screenshot=%s",
                    current_url,
                    page_title,
                    login_detected,
                    row_count,
                    extraction_method_used,
                    screenshot_path,
                )
                raise SupportPortalBrowserScrapeError(
                    "No visible cases extracted. "
                    f"selected_case_view={diagnostics.get('selected_case_view')} "
                    f"target_case_view={diagnostics.get('target_case_view')} "
                    f"target_view_not_selected={diagnostics.get('target_view_not_selected')} "
                    f"empty_cases_view_detected={diagnostics.get('empty_cases_view_detected')} "
                    f"screenshot={screenshot_path} html={html_path}",
                    category="extraction_failure",
                )

            return BrowserScrapeResult(
                cases=cases,
                extraction_method_used=extraction_method_used,
                current_url=current_url,
                page_title=page_title,
                login_detected=login_detected,
                visible_case_numbers=visible_case_numbers,
                row_count=row_count,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            if _is_timeout_exception(exc):
                screenshot_path, html_path = _capture_named_artifacts(
                    page,
                    screenshot_path=FAILURE_SCREENSHOT_PATH,
                    html_path=FAILURE_HTML_PATH,
                    settings=settings,
                    failure=True,
                )
                phase = "case_extraction_timeout"
                if "goto" in str(exc).lower() or "navigation" in str(exc).lower():
                    phase = "page_load_timeout"
                close_context(logger, reason=phase)
                raise SupportPortalBrowserScrapeError(
                    f"{phase} after configured timeout. screenshot={screenshot_path} html={html_path}",
                    category="extraction_failure" if phase == "case_extraction_timeout" else "authentication_failed",
                ) from exc
            close_context(logger, reason="scrape_failure")
            raise
    except Exception:
        close_context(logger, reason="scrape_failure")
        raise


def fetch_cases_via_browser_detailed(settings: Any) -> BrowserScrapeResult:
    logger = logging.getLogger(__name__)
    if not acquire_scrape_lease():
        logger.warning("scrape_already_running=true")
        raise SupportPortalBrowserScrapeError("Browser scrape already running", category="profile_busy")

    cleanup_debug_artifacts(settings, logger)
    attempts = max(1, int(getattr(settings, "support_portal_browser_retry_attempts", 2) or 1))
    last_exc: Exception | None = None
    try:
        for attempt_number in range(1, attempts + 1):
            try:
                return _fetch_cases_via_browser_once(settings)
            except SupportPortalBrowserScrapeError as exc:
                last_exc = exc
                retryable = "timeout" in str(exc).lower() and attempt_number < attempts
                logger.warning(
                    "browser_scrape_attempt_failed attempt=%s/%s retryable=%s reason=%s",
                    attempt_number,
                    attempts,
                    retryable,
                    exc,
                )
                if not retryable:
                    raise
                close_context(logger, reason=f"browser_retry_attempt_{attempt_number}")
        if last_exc is not None:
            raise last_exc
        raise SupportPortalBrowserScrapeError("Browser scrape failed without a captured exception", category="browser_error")
    finally:
        release_scrape_lease()


def fetch_cases_via_browser(settings: Any) -> list[dict[str, Any]]:
    result = fetch_cases_via_browser_detailed(settings)
    return result.cases
