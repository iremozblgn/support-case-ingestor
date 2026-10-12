from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from support_portal.graphql_payloads import (
    DEFAULT_ORDER_BY,
    DEFAULT_PAGE_SIZE,
    DEFAULT_WHERE_CLAUSE,
)
from support_portal.session import mask_secret


load_dotenv()


REPO_ROOT = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {name}: {raw!r}")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _json_env(name: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = os.getenv(name)
    if not raw:
        return default
    return json.loads(raw)


def _csv_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def resolve_repo_path(path_value: str | None) -> Path:
    candidate = Path(path_value or ".playwright-support_portal-profile").expanduser()
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


@dataclass(slots=True)
class Settings:
    ingest_mode: str
    database_url: str
    support_portal_graphql_url: str
    support_portal_cookie: str | None
    support_portal_aura_context: str
    support_portal_aura_token: str
    support_portal_page_uri: str
    support_portal_debug_raw_request: bool
    support_portal_browser_parity_mode: bool
    browser_parity_strict: bool
    support_portal_browser_parity_message: str | None
    support_portal_browser_parity_message_path: str
    support_portal_browser_request_snapshot_path: str
    support_portal_fetch_strategy_order: tuple[str, ...]
    support_portal_enable_strategy_fallback: bool
    support_portal_fail_on_minimal_only: bool
    support_portal_safe_where_mode: str
    support_portal_browser_scrape_fallback_enabled: bool
    support_portal_browser_headless: bool
    support_portal_browser_profile_dir: str
    support_portal_auto_login_enabled: bool
    support_portal_okta_keep_signed_in: bool
    support_portal_username: str | None
    support_portal_password: str | None
    support_portal_login_url: str
    support_portal_login_timeout_seconds: int
    support_portal_session_check_timeout_seconds: int
    support_portal_manual_login_fallback: bool
    support_portal_interactive_bootstrap_enabled: bool
    support_portal_operator_wait_timeout_seconds: int
    support_portal_browser_idle_timeout_seconds: int
    support_portal_page_load_timeout_seconds: int
    support_portal_case_extraction_timeout_seconds: int
    support_portal_browser_retry_attempts: int
    support_portal_target_case_view: str
    support_portal_target_case_view_aliases: tuple[str, ...]
    case_status_filter: str
    page_size: int
    max_pages_per_cycle: int
    case_where_clause: dict[str, Any] | None
    case_order_by: dict[str, Any]
    check_interval_seconds: int
    run_once: bool
    send_enabled: bool
    email_test_mode: bool
    dry_run: bool
    notification_allowed_priorities: tuple[str, ...]
    notification_allowed_products: tuple[str, ...]
    notification_allowed_statuses: tuple[str, ...]
    session_max_retries: int
    session_retry_base_seconds: int
    session_retry_max_seconds: int
    session_jitter_enabled: bool
    healthcheck_enabled: bool
    healthcheck_host: str
    healthcheck_port: int
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_use_tls: bool
    mail_send_timeout_seconds: int
    mail_max_retry_attempts: int
    mail_retry_base_seconds: int
    mail_retry_backoff_multiplier: int
    mail_retry_jitter: bool
    to_email: str | None
    log_level: str
    request_timeout_seconds: int
    debug_artifacts_enabled: bool
    debug_artifact_retention_days: int
    portal_url: str | None
    portal_username: str | None
    portal_password: str | None
    cases_url: str | None
    portal_api_url: str | None
    portal_api_token: str | None
    input_dir: str
    sqlite_path: str
    imap_host: str | None
    imap_port: int
    imap_username: str | None
    imap_password: str | None
    imap_mailbox: str
    imap_unseen_only: bool
    support_portal_sender_filter: str
    support_portal_subject_keywords: str
    forward_to_email: str | None
    forward_from_email: str | None

    @property
    def portal_case_base_url(self) -> str:
        base_url = (self.portal_url or "https://support.example.com/s").rstrip("/")
        return f"{base_url}/case"

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def resolved_browser_profile_dir(self) -> Path:
        return resolve_repo_path(self.support_portal_browser_profile_dir)

    def masked_config(self) -> dict[str, Any]:
        return {
            "ingest_mode": self.ingest_mode,
            "database_url": self.database_url,
            "support_portal_graphql_url": self.support_portal_graphql_url,
            "support_portal_cookie": mask_secret(self.support_portal_cookie),
            "support_portal_aura_context": mask_secret(self.support_portal_aura_context),
            "support_portal_aura_token": mask_secret(self.support_portal_aura_token),
            "support_portal_debug_raw_request": self.support_portal_debug_raw_request,
            "support_portal_browser_parity_mode": self.support_portal_browser_parity_mode,
            "browser_parity_strict": self.browser_parity_strict,
            "support_portal_browser_parity_message_path": self.support_portal_browser_parity_message_path,
            "support_portal_browser_request_snapshot_path": self.support_portal_browser_request_snapshot_path,
            "support_portal_fetch_strategy_order": self.support_portal_fetch_strategy_order,
            "support_portal_enable_strategy_fallback": self.support_portal_enable_strategy_fallback,
            "support_portal_fail_on_minimal_only": self.support_portal_fail_on_minimal_only,
            "support_portal_safe_where_mode": self.support_portal_safe_where_mode,
            "support_portal_browser_scrape_fallback_enabled": self.support_portal_browser_scrape_fallback_enabled,
            "browser_automation_enabled": self.support_portal_browser_scrape_fallback_enabled,
            "browser_fallback_enabled": self.support_portal_browser_scrape_fallback_enabled,
            "support_portal_browser_headless": self.support_portal_browser_headless,
            "support_portal_browser_profile_dir": self.support_portal_browser_profile_dir,
            "resolved_browser_profile_dir": str(self.resolved_browser_profile_dir),
            "support_portal_auto_login_enabled": self.support_portal_auto_login_enabled,
            "support_portal_okta_keep_signed_in": self.support_portal_okta_keep_signed_in,
            "support_portal_login_url": self.support_portal_login_url,
            "support_portal_login_timeout_seconds": self.support_portal_login_timeout_seconds,
            "support_portal_session_check_timeout_seconds": self.support_portal_session_check_timeout_seconds,
            "support_portal_manual_login_fallback": self.support_portal_manual_login_fallback,
            "support_portal_browser_idle_timeout_seconds": self.support_portal_browser_idle_timeout_seconds,
            "support_portal_page_load_timeout_seconds": self.support_portal_page_load_timeout_seconds,
            "support_portal_case_extraction_timeout_seconds": self.support_portal_case_extraction_timeout_seconds,
            "support_portal_browser_retry_attempts": self.support_portal_browser_retry_attempts,
            "support_portal_target_case_view": self.support_portal_target_case_view,
            "support_portal_target_case_view_aliases": self.support_portal_target_case_view_aliases,
            "send_enabled": self.send_enabled,
            "email_test_mode": self.email_test_mode,
            "dry_run": self.dry_run,
            "notification_allowed_priorities": self.notification_allowed_priorities,
            "notification_allowed_products": self.notification_allowed_products,
            "notification_allowed_statuses": self.notification_allowed_statuses,
            "session_max_retries": self.session_max_retries,
            "session_retry_base_seconds": self.session_retry_base_seconds,
            "session_retry_max_seconds": self.session_retry_max_seconds,
            "session_jitter_enabled": self.session_jitter_enabled,
            "healthcheck_enabled": self.healthcheck_enabled,
            "healthcheck_host": self.healthcheck_host,
            "healthcheck_port": self.healthcheck_port,
            "mail_send_timeout_seconds": self.mail_send_timeout_seconds,
            "mail_max_retry_attempts": self.mail_max_retry_attempts,
            "mail_retry_base_seconds": self.mail_retry_base_seconds,
            "mail_retry_backoff_multiplier": self.mail_retry_backoff_multiplier,
            "mail_retry_jitter": self.mail_retry_jitter,
            "to_email": self.to_email,
            "debug_artifacts_enabled": self.debug_artifacts_enabled,
            "debug_artifact_retention_days": self.debug_artifact_retention_days,
        }

    def validate_graphql(self) -> None:
        missing = []
        if not self.support_portal_graphql_url:
            missing.append("SUPPORT_PORTAL_GRAPHQL_URL")
        if not self.support_portal_cookie:
            missing.append("SUPPORT_PORTAL_COOKIE")
        if not self.support_portal_aura_context:
            missing.append("SUPPORT_PORTAL_AURA_CONTEXT")
        if not self.support_portal_aura_token:
            missing.append("SUPPORT_PORTAL_AURA_TOKEN")
        if missing:
            raise ValueError(f"GraphQL mode requires: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    status_filter = os.getenv("CASE_STATUS_FILTER", "Awaiting Customer")
    support_portal_username = os.getenv("SUPPORT_PORTAL_USERNAME")
    support_portal_password = os.getenv("SUPPORT_PORTAL_PASSWORD")
    portal_username = os.getenv("PORTAL_USERNAME")
    portal_password = os.getenv("PORTAL_PASSWORD")
    selected_username: str | None = None
    selected_password: str | None = None
    if support_portal_username and support_portal_password:
        selected_username = support_portal_username
        selected_password = support_portal_password
    elif support_portal_username or support_portal_password:
        selected_username = support_portal_username
        selected_password = support_portal_password
    elif portal_username and portal_password:
        selected_username = portal_username
        selected_password = portal_password
    elif portal_username or portal_password:
        selected_username = portal_username
        selected_password = portal_password
    return Settings(
        ingest_mode=os.getenv("INGEST_MODE", "graphql").strip().lower(),
        database_url=os.getenv("DATABASE_URL", "sqlite:///support_portal_cases.db"),
        support_portal_graphql_url=os.getenv(
            "SUPPORT_PORTAL_GRAPHQL_URL",
            "https://support.example.com/s/sfsites/aura?r=16&aura.RecordUi.executeGraphQL=1",
        ),
        support_portal_cookie=os.getenv("SUPPORT_PORTAL_COOKIE"),
        support_portal_aura_context=os.getenv("SUPPORT_PORTAL_AURA_CONTEXT", ""),
        support_portal_aura_token=os.getenv("SUPPORT_PORTAL_AURA_TOKEN", ""),
        support_portal_page_uri=os.getenv("SUPPORT_PORTAL_PAGE_URI", "/s/case/Case/Default"),
        support_portal_debug_raw_request=_bool("SUPPORT_PORTAL_DEBUG_RAW_REQUEST", False),
        support_portal_browser_parity_mode=_bool("SUPPORT_PORTAL_BROWSER_PARITY_MODE", False),
        browser_parity_strict=_bool("BROWSER_PARITY_STRICT", True),
        support_portal_browser_parity_message=os.getenv("SUPPORT_PORTAL_BROWSER_PARITY_MESSAGE"),
        support_portal_browser_parity_message_path=os.getenv(
            "SUPPORT_PORTAL_BROWSER_PARITY_MESSAGE_PATH",
            str(Path("debug") / "browser_message.json"),
        ),
        support_portal_browser_request_snapshot_path=os.getenv(
            "SUPPORT_PORTAL_BROWSER_REQUEST_SNAPSHOT_PATH",
            str(Path("debug") / "browser_request_full.json"),
        ),
        support_portal_fetch_strategy_order=_csv_list("SUPPORT_PORTAL_FETCH_STRATEGY_ORDER")
        or ("full", "safe", "minimal"),
        support_portal_enable_strategy_fallback=_bool("SUPPORT_PORTAL_ENABLE_STRATEGY_FALLBACK", True),
        support_portal_fail_on_minimal_only=_bool("SUPPORT_PORTAL_FAIL_ON_MINIMAL_ONLY", False),
        support_portal_safe_where_mode=os.getenv("SUPPORT_PORTAL_SAFE_WHERE_MODE", "all_cases").strip().lower(),
        support_portal_browser_scrape_fallback_enabled=_bool("SUPPORT_PORTAL_BROWSER_SCRAPE_FALLBACK_ENABLED", True),
        support_portal_browser_headless=_bool("SUPPORT_PORTAL_BROWSER_HEADLESS", False),
        support_portal_browser_profile_dir=os.getenv(
            "SUPPORT_PORTAL_BROWSER_PROFILE_DIR",
            ".playwright-support_portal-profile",
        ),
        support_portal_auto_login_enabled=_bool("SUPPORT_PORTAL_AUTO_LOGIN_ENABLED", True),
        support_portal_okta_keep_signed_in=_bool("SUPPORT_PORTAL_OKTA_KEEP_SIGNED_IN", True),
        support_portal_username=selected_username,
        support_portal_password=selected_password,
        support_portal_login_url=os.getenv("SUPPORT_PORTAL_LOGIN_URL", "https://support.example.com/s/login/"),
        support_portal_login_timeout_seconds=_int("SUPPORT_PORTAL_LOGIN_TIMEOUT_SECONDS", 90),
        support_portal_session_check_timeout_seconds=_int("SUPPORT_PORTAL_SESSION_CHECK_TIMEOUT_SECONDS", 30),
        support_portal_manual_login_fallback=_bool("SUPPORT_PORTAL_MANUAL_LOGIN_FALLBACK", True),
        support_portal_interactive_bootstrap_enabled=_bool("SUPPORT_PORTAL_INTERACTIVE_BOOTSTRAP_ENABLED", True),
        support_portal_operator_wait_timeout_seconds=_int("SUPPORT_PORTAL_OPERATOR_WAIT_TIMEOUT_SECONDS", 300),
        support_portal_browser_idle_timeout_seconds=_int("SUPPORT_PORTAL_BROWSER_IDLE_TIMEOUT_SECONDS", 600),
        support_portal_page_load_timeout_seconds=_int("SUPPORT_PORTAL_PAGE_LOAD_TIMEOUT_SECONDS", 90),
        support_portal_case_extraction_timeout_seconds=_int("SUPPORT_PORTAL_CASE_EXTRACTION_TIMEOUT_SECONDS", 90),
        support_portal_browser_retry_attempts=_int("SUPPORT_PORTAL_BROWSER_RETRY_ATTEMPTS", 2),
        support_portal_target_case_view=os.getenv(
            "SUPPORT_PORTAL_TARGET_CASE_VIEW",
            "Closed Cases Submitted for My Company",
        ).strip(),
        support_portal_target_case_view_aliases=_csv_list("SUPPORT_PORTAL_TARGET_CASE_VIEW_ALIASES")
        or (
            "Closed Cases Submitted for My Company",
            "Cases - Action Required",
            "Action Required Cases",
            "My Company's Cases",
            "My Company's Case",
            "All Cases",
        ),
        case_status_filter=status_filter,
        page_size=_int("PAGE_SIZE", DEFAULT_PAGE_SIZE),
        max_pages_per_cycle=_int("MAX_PAGES_PER_CYCLE", 3),
        case_where_clause=_json_env("SUPPORT_PORTAL_WHERE_CLAUSE", {}) or None,
        case_order_by=_json_env("SUPPORT_PORTAL_ORDER_BY", DEFAULT_ORDER_BY),
        check_interval_seconds=_int("CHECK_INTERVAL_SECONDS", 300),
        run_once=_bool("RUN_ONCE", False),
        send_enabled=_bool("SEND_ENABLED", False),
        email_test_mode=_bool("EMAIL_TEST_MODE", True),
        dry_run=_bool("DRY_RUN", False),
        notification_allowed_priorities=_csv_list("NOTIFICATION_ALLOWED_PRIORITIES"),
        notification_allowed_products=_csv_list("NOTIFICATION_ALLOWED_PRODUCTS"),
        notification_allowed_statuses=_csv_list("NOTIFICATION_ALLOWED_STATUSES"),
        session_max_retries=_int("SESSION_MAX_RETRIES", 5),
        session_retry_base_seconds=_int("SESSION_RETRY_BASE_SECONDS", 5),
        session_retry_max_seconds=_int("SESSION_RETRY_MAX_SECONDS", 300),
        session_jitter_enabled=_bool("SESSION_JITTER_ENABLED", True),
        healthcheck_enabled=_bool("HEALTHCHECK_ENABLED", False),
        healthcheck_host=os.getenv("HEALTHCHECK_HOST", "127.0.0.1"),
        healthcheck_port=_int("HEALTHCHECK_PORT", 8080),
        smtp_host=os.getenv("SMTP_HOST"),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_username=os.getenv("SMTP_USERNAME"),
        smtp_password=os.getenv("SMTP_PASSWORD"),
        smtp_use_tls=_bool("SMTP_USE_TLS", True),
        mail_send_timeout_seconds=_int("MAIL_SEND_TIMEOUT_SECONDS", 30),
        mail_max_retry_attempts=_int("MAIL_MAX_RETRY_ATTEMPTS", 3),
        mail_retry_base_seconds=_int("MAIL_RETRY_BASE_SECONDS", 60),
        mail_retry_backoff_multiplier=_int("MAIL_RETRY_BACKOFF_MULTIPLIER", 2),
        mail_retry_jitter=_bool("MAIL_RETRY_JITTER", True),
        to_email=os.getenv("TO_EMAIL") or os.getenv("FORWARD_TO_EMAIL"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 60),
        debug_artifacts_enabled=_bool("DEBUG_ARTIFACTS_ENABLED", False),
        debug_artifact_retention_days=_int("DEBUG_ARTIFACT_RETENTION_DAYS", 3),
        portal_url=os.getenv("PORTAL_URL", "https://support.example.com/s"),
        portal_username=portal_username,
        portal_password=portal_password,
        cases_url=os.getenv("SUPPORT_PORTAL_CASES_URL") or os.getenv("CASES_URL") or "https://support.example.com/s/case/Case/Default?tabset-836f8=1",
        portal_api_url=os.getenv("PORTAL_API_URL"),
        portal_api_token=os.getenv("PORTAL_API_TOKEN"),
        input_dir=os.getenv("INPUT_DIR", "samples"),
        sqlite_path=os.getenv("SQLITE_PATH", "events.db"),
        imap_host=os.getenv("IMAP_HOST"),
        imap_port=_int("IMAP_PORT", 993),
        imap_username=os.getenv("IMAP_USERNAME"),
        imap_password=os.getenv("IMAP_PASSWORD"),
        imap_mailbox=os.getenv("IMAP_MAILBOX", "INBOX"),
        imap_unseen_only=_bool("IMAP_UNSEEN_ONLY", True),
        support_portal_sender_filter=os.getenv("SUPPORT_PORTAL_SENDER_FILTER", "support_portal"),
        support_portal_subject_keywords=os.getenv(
            "SUPPORT_PORTAL_SUBJECT_KEYWORDS",
            "case,action required,support",
        ),
        forward_to_email=os.getenv("FORWARD_TO_EMAIL"),
        forward_from_email=os.getenv("FORWARD_FROM_EMAIL"),
    )


settings = get_settings()

# Compatibility exports for existing modules.
PORTAL_API_URL = settings.portal_api_url
PORTAL_API_TOKEN = settings.portal_api_token
SEND_ENABLED = settings.send_enabled
INPUT_DIR = settings.input_dir
SQLITE_PATH = settings.sqlite_path
INGEST_MODE = settings.ingest_mode
IMAP_HOST = settings.imap_host
IMAP_PORT = settings.imap_port
IMAP_USERNAME = settings.imap_username
IMAP_PASSWORD = settings.imap_password
IMAP_MAILBOX = settings.imap_mailbox
IMAP_UNSEEN_ONLY = settings.imap_unseen_only
SUPPORT_PORTAL_SENDER_FILTER = settings.support_portal_sender_filter
SUPPORT_PORTAL_SUBJECT_KEYWORDS = settings.support_portal_subject_keywords
SMTP_HOST = settings.smtp_host
SMTP_PORT = settings.smtp_port
SMTP_USERNAME = settings.smtp_username
SMTP_PASSWORD = settings.smtp_password
SMTP_USE_TLS = settings.smtp_use_tls
FORWARD_TO_EMAIL = settings.forward_to_email
FORWARD_FROM_EMAIL = settings.forward_from_email
PORTAL_URL = settings.portal_url
CASES_URL = settings.cases_url
PORTAL_USERNAME = settings.portal_username
PORTAL_PASSWORD = settings.portal_password
CHECK_INTERVAL_SECONDS = settings.check_interval_seconds
DATABASE_URL = settings.database_url
SUPPORT_PORTAL_GRAPHQL_URL = settings.support_portal_graphql_url
SUPPORT_PORTAL_COOKIE = settings.support_portal_cookie
SUPPORT_PORTAL_AURA_CONTEXT = settings.support_portal_aura_context
SUPPORT_PORTAL_AURA_TOKEN = settings.support_portal_aura_token
SUPPORT_PORTAL_PAGE_URI = settings.support_portal_page_uri
SUPPORT_PORTAL_FETCH_STRATEGY_ORDER = settings.support_portal_fetch_strategy_order
SUPPORT_PORTAL_ENABLE_STRATEGY_FALLBACK = settings.support_portal_enable_strategy_fallback
SUPPORT_PORTAL_FAIL_ON_MINIMAL_ONLY = settings.support_portal_fail_on_minimal_only
SUPPORT_PORTAL_SAFE_WHERE_MODE = settings.support_portal_safe_where_mode
BROWSER_PARITY_STRICT = settings.browser_parity_strict
CASE_STATUS_FILTER = settings.case_status_filter
PAGE_SIZE = settings.page_size
MAX_PAGES_PER_CYCLE = settings.max_pages_per_cycle
CASE_WHERE_CLAUSE = settings.case_where_clause or DEFAULT_WHERE_CLAUSE
CASE_ORDER_BY = settings.case_order_by or DEFAULT_ORDER_BY
