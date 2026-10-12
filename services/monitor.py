from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from support_portal.graphql_client import (
    SupportPortalGraphQLClient,
    SupportPortalFieldUndefinedError,
    SupportPortalRequestError,
    SupportPortalResponseError,
    SupportPortalSessionExpiredError,
    SupportPortalZeroResultsError,
)
from support_portal.browser_scraper import (
    SupportPortalBrowserScrapeError,
    fetch_cases_via_browser_detailed,
)
from support_portal.parser import parse_case_edges
from notifications.mailer import SupportPortalMailer
from storage.database import create_session_factory
from storage.repository import (
    SupportPortalRepository,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_RETRYING,
    NOTIFICATION_STATUS_SENT,
    utcnow,
)
from services.runtime_state import RuntimeHealthState
from utils.hashing import generate_case_event_hash


class RunExitCategory(StrEnum):
    SUCCESS = "success"
    SUCCESS_NO_NEW_CASES = "success_no_new_cases"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    AUTHENTICATION_FAILURE = "authentication_failure"
    EXTRACTION_FAILURE = "extraction_failure"
    PROFILE_BUSY = "profile_busy"
    NOTIFICATION_FAILURE = "notification_failure"
    CONFIGURATION_ERROR = "configuration_error"
    UPSTREAM_RUNTIME_FAILURE = "upstream_runtime_failure"


@dataclass(slots=True)
class RunCycleResult:
    summary: dict[str, Any]

    @property
    def exit_category(self) -> str:
        return str(self.summary.get("exit_category") or RunExitCategory.UPSTREAM_RUNTIME_FAILURE)


class SupportPortalMonitorService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.session_factory = create_session_factory(settings.database_url)
        self.runtime_state = RuntimeHealthState()
        self.client = SupportPortalGraphQLClient(settings=settings, runtime_state=self.runtime_state)
        self.mailer = SupportPortalMailer(settings=settings)

    def run(self) -> dict[str, Any] | None:
        if self.settings.run_once:
            return self._run_once()

        while True:
            self.run_cycle()
            time.sleep(self.settings.check_interval_seconds)

    def _run_once(self) -> dict[str, Any]:
        started_monotonic = time.monotonic()
        started_at = datetime.now(UTC)
        self.logger.info("batch_mode=true")
        self.logger.info("run_once=true")
        self.logger.info("scheduled_run_started_at=%s", started_at.isoformat())

        run_exit_reason = "run_once_completed"
        cycle_summary = self.run_cycle()
        if not cycle_summary.get("success", False):
            run_exit_reason = str(cycle_summary.get("stop_reason") or "run_once_failed")
            self._log_batch_failure_signals(cycle_summary)
        elif cycle_summary.get("exit_category") == RunExitCategory.SUCCESS_NO_NEW_CASES:
            run_exit_reason = "run_once_completed_no_new_cases"

        finished_at = datetime.now(UTC)
        diagnostics = {
            "batch_mode": True,
            "run_once": True,
            "scheduled_run_started_at": started_at.isoformat(),
            "scheduled_run_finished_at": finished_at.isoformat(),
            "run_duration_seconds": round(time.monotonic() - started_monotonic, 3),
            "run_exit_reason": run_exit_reason,
            "successful_cycles": cycle_summary.get("successful_cycles", 0),
            "failed_cycles": cycle_summary.get("failed_cycles", 0),
            "auth_failures": cycle_summary.get("auth_failures", 0),
            "notifications_sent": cycle_summary.get("notifications_sent", 0),
            "notifications_failed": cycle_summary.get("notifications_failed", 0),
            "cycle_stop_reason": cycle_summary.get("stop_reason"),
            "cycle_success": cycle_summary.get("success", False),
            "exit_category": cycle_summary.get("exit_category"),
            "operator_action_required": cycle_summary.get("operator_action_required", False),
        }
        self.logger.info("scheduled_run_finished_at=%s", diagnostics["scheduled_run_finished_at"])
        self.logger.info("run_duration_seconds=%s", diagnostics["run_duration_seconds"])
        self.logger.info("run_exit_reason=%s", diagnostics["run_exit_reason"])
        self.logger.info("successful_cycles=%s", diagnostics["successful_cycles"])
        self.logger.info("failed_cycles=%s", diagnostics["failed_cycles"])
        self.logger.info("auth_failures=%s", diagnostics["auth_failures"])
        self.logger.info("notifications_sent=%s", diagnostics["notifications_sent"])
        self.logger.info("notifications_failed=%s", diagnostics["notifications_failed"])
        self.logger.info("scheduled_run_finished=true")
        self.logger.info("monitor_exit_reason=%s", run_exit_reason)
        self.logger.info("run_final_summary=%s", diagnostics)
        return diagnostics

    def run_cycle(self) -> dict[str, Any]:
        self.logger.info("Starting SupportPortal GraphQL monitoring cycle")
        try:
            pages = self.client.fetch_all_case_pages(
                page_size=self.settings.page_size,
                where_clause=self.settings.case_where_clause,
                order_by=self.settings.case_order_by,
            )
        except SupportPortalSessionExpiredError as exc:
            self.logger.error("%s", exc)
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason="SESSION_EXPIRED_OR_INVALID",
                success=False,
                exit_category=RunExitCategory.AUTHENTICATION_FAILURE,
                operator_action_required=True,
            )
            self._log_operator_action_required(
                reason="session_invalid",
                detail=str(exc),
            )
            self.logger.warning("Notifications suppressed while session is invalid.")
            self.logger.info("Cycle summary: %s", summary)
            return summary
        except (SupportPortalRequestError, SupportPortalResponseError) as exc:
            self.logger.error("Request cycle failed: %s", exc)
            browser_summary = self._attempt_browser_fallback(
                graph_stop_reason="REQUEST_FAILED",
                browser_success_reason="GRAPHQL_REQUEST_FAILED_BROWSER_FALLBACK_SUCCESS",
            )
            if browser_summary is not None:
                return browser_summary
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason="REQUEST_FAILED",
                success=False,
                exit_category=RunExitCategory.UPSTREAM_RUNTIME_FAILURE,
            )
            self.logger.info("Cycle summary: %s", summary)
            return summary
        except SupportPortalZeroResultsError as exc:
            self.logger.error("Zero-result fallback exhaustion: %s", exc)
            browser_summary = self._attempt_browser_fallback(
                graph_stop_reason="ZERO_RESULTS_ALL_FALLBACKS_EXHAUSTED",
                browser_success_reason="GRAPHQL_ZERO_RESULTS_BROWSER_FALLBACK_SUCCESS",
            )
            if browser_summary is not None:
                return browser_summary
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason="ZERO_RESULTS_ALL_FALLBACKS_EXHAUSTED",
                success=False,
                exit_category=RunExitCategory.EXTRACTION_FAILURE,
            )
            self.logger.info("Cycle summary: %s", summary)
            return summary
        except SupportPortalFieldUndefinedError as exc:
            self.logger.error("Field undefined failure: %s", exc)
            browser_summary = self._attempt_browser_fallback(
                graph_stop_reason="FIELD_UNDEFINED_FATAL",
                browser_success_reason="GRAPHQL_FIELD_UNDEFINED_BROWSER_FALLBACK_SUCCESS",
            )
            if browser_summary is not None:
                return browser_summary
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason="FIELD_UNDEFINED_FATAL",
                success=False,
                exit_category=RunExitCategory.UPSTREAM_RUNTIME_FAILURE,
            )
            self.logger.info("Cycle summary: %s", summary)
            return summary

        if self.client.last_pagination_summary:
            pagination = self.client.last_pagination_summary
            self.logger.info(
                "Monitor pagination summary unique_cases=%s pages=%s duplicate_pages=%s stop_reason=%s",
                pagination.total_unique_cases,
                pagination.total_pages_fetched,
                pagination.duplicate_pages_detected,
                pagination.stop_reason,
            )

        parsed_pages: list[list[dict[str, Any]]] = []
        for page in pages:
            parsed_cases = parse_case_edges(page.payload, base_case_url=self.settings.portal_case_base_url)
            parsed_pages.append(parsed_cases)
            self.logger.info(
                "Parsed page=%s cases=%s totalCount=%s",
                page.page_number,
                len(parsed_cases),
                page.total_count,
            )
        summary = self._process_normalized_case_batches(
            case_batches=parsed_pages,
            pages_fetched=self.client.last_pagination_summary.total_pages_fetched if self.client.last_pagination_summary else len(pages),
            stop_reason=self.client.last_pagination_summary.stop_reason if self.client.last_pagination_summary else "completed",
        )
        self.logger.info("Cycle summary: %s", summary)
        return summary

    def _attempt_browser_fallback(
        self,
        *,
        graph_stop_reason: str,
        browser_success_reason: str,
    ) -> dict[str, Any] | None:
        if not self.settings.support_portal_browser_scrape_fallback_enabled:
            self.logger.info("browser_fallback_skipped_reason=disabled graph_stop_reason=%s", graph_stop_reason)
            return None
        self.logger.info("entering_browser_fallback=true graph_stop_reason=%s", graph_stop_reason)
        self.logger.info("browser_fallback_started=true")
        try:
            browser_result = fetch_cases_via_browser_detailed(self.settings)
        except SupportPortalBrowserScrapeError as browser_exc:
            self._log_browser_auth_issue(browser_exc)
            self.logger.exception("browser_fallback_exception=%s", browser_exc)
            self.logger.info("browser_fallback_finished=true")
            self.logger.info("browser_fallback_cases_found=0")
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason=f"BROWSER_FALLBACK_{getattr(browser_exc, 'category', 'FAILED').upper()}",
                success=False,
                exit_category=self._map_browser_error_category(browser_exc),
                operator_action_required=bool(getattr(browser_exc, "operator_action_required", False)),
            )
            self.logger.info("Cycle summary: %s", summary)
            return summary
        except Exception as browser_exc:  # pragma: no cover - defensive logging path
            self._log_browser_auth_issue(browser_exc)
            self.logger.exception("browser_fallback_exception=%s", browser_exc)
            self.logger.info("browser_fallback_finished=true")
            self.logger.info("browser_fallback_cases_found=0")
            summary = self._build_summary(
                total_fetched=0,
                total_unique_cases=0,
                new_cases=0,
                existing_cases=0,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=0,
                pages_fetched=0,
                stop_reason="BROWSER_FALLBACK_FAILED",
                success=False,
                exit_category=RunExitCategory.UPSTREAM_RUNTIME_FAILURE,
            )
            self.logger.info("Cycle summary: %s", summary)
            return summary

        self.logger.info("browser_fallback_finished=true")
        browser_cases = browser_result.cases
        self.logger.info("browser_fallback_cases_found=%s", len(browser_cases))
        if browser_cases:
            self.logger.warning(
                "possible_context_mismatch=true graph_stop_reason=%s browser_cases_found=%s extraction_method_used=%s current_url=%s",
                graph_stop_reason,
                len(browser_cases),
                browser_result.extraction_method_used,
                browser_result.current_url,
            )
            self.client.last_fetch_strategy_summary.fetch_strategy_used = "BrowserScrapeFallback"
            return self._process_normalized_case_batches(
                case_batches=[browser_cases],
                pages_fetched=1,
                stop_reason=browser_success_reason,
            )
        self.logger.warning(
            "browser_fallback_skipped_reason=no_cases_found graph_stop_reason=%s extraction_method_used=%s current_url=%s",
            graph_stop_reason,
            browser_result.extraction_method_used,
            browser_result.current_url,
        )
        return None

    def _process_normalized_case_batches(
        self,
        *,
        case_batches: list[list[dict[str, Any]]],
        pages_fetched: int,
        stop_reason: str,
    ) -> dict[str, Any]:
        total_fetched = sum(len(batch) for batch in case_batches)
        total_unique_cases = len(
            {
                case.get("case_id") or case.get("case_number")
                for batch in case_batches
                for case in batch
                if case.get("case_id") or case.get("case_number")
            }
        )
        new_cases = 0
        existing_cases = 0
        notifications_sent = 0
        notifications_failed = 0
        skipped_due_to_filter = 0
        sample_case: dict[str, Any] | None = None
        sample_case_status = "new"

        if self.settings.dry_run:
            with self.session_factory() as session:
                repo = SupportPortalRepository(session)
                for parsed_cases in case_batches:
                    for case in parsed_cases:
                        existing_record = repo.get_case(case.get("case_id"), case.get("case_number"))
                        if existing_record is None:
                            new_cases += 1
                            allowed, _ = self._should_notify_case(case)
                            if not allowed:
                                skipped_due_to_filter += 1
                            if sample_case is None:
                                sample_case = case
                                sample_case_status = "new"
                        else:
                            existing_cases += 1
                            if sample_case is None:
                                sample_case = case
                                sample_case_status = "existing"

            summary = self._build_summary(
                total_fetched=total_fetched,
                total_unique_cases=total_unique_cases,
                new_cases=new_cases,
                existing_cases=existing_cases,
                notifications_sent=0,
                notifications_failed=0,
                skipped_due_to_filter=skipped_due_to_filter,
                pages_fetched=pages_fetched,
                stop_reason=stop_reason,
                success=True,
                exit_category=RunExitCategory.SUCCESS if new_cases > 0 else RunExitCategory.SUCCESS_NO_NEW_CASES,
            )
            self.logger.info("DRY_RUN enabled; no DB writes or emails will be performed.")
            if sample_case is not None:
                self.logger.info(
                    "DRY_RUN sample case status=%s case_number=%s case_id=%s",
                    sample_case_status,
                    sample_case.get("case_number"),
                    sample_case.get("case_id"),
                )
            return summary

        with self.session_factory() as session:
            repo = SupportPortalRepository(session)
            for parsed_cases in case_batches:
                for case in parsed_cases:
                    event_hash = generate_case_event_hash(case)
                    record, is_new = repo.upsert_case(case, event_hash=event_hash)
                    if is_new:
                        new_cases += 1
                        if sample_case is None:
                            sample_case = case
                            sample_case_status = "new"
                        self.logger.info(
                            "New case discovered case_number=%s case_id=%s",
                            case.get("case_number"),
                            case.get("case_id"),
                        )
                        if not self.settings.email_test_mode:
                            allowed, filter_reason = self._should_notify_case(case)
                            if not allowed:
                                skipped_due_to_filter += 1
                                repo.mark_notification_filtered(record, f"Filtered out: {filter_reason}")
                                self.logger.info(
                                    "skipped_due_to_filter case_number=%s case_id=%s reason=%s",
                                    case.get("case_number"),
                                    case.get("case_id"),
                                    filter_reason,
                                )
                                repo.log_notification(
                                    case_id=case.get("case_id"),
                                    case_number=case.get("case_number"),
                                    email=self.settings.to_email,
                                    status="SKIPPED_FILTERED",
                                    error_message=f"Filtered out: {filter_reason}",
                                )
                            else:
                                attempt = self.mailer.send_new_case_email(case, repo, record=record)
                                if attempt.sent:
                                    notifications_sent += 1
                                    self.logger.info(
                                        "notification_sent case_number=%s case_id=%s",
                                        case.get("case_number"),
                                        case.get("case_id"),
                                    )
                                elif not attempt.skipped:
                                    notifications_failed += 1
                    else:
                        existing_cases += 1
                        if sample_case is None:
                            sample_case = case
                            sample_case_status = "existing"
                        repo.sync_case_notification_state_from_logs(record)
                        allowed, filter_reason = self._should_notify_case(case)
                        if not allowed:
                            if record.notification_status in {NOTIFICATION_STATUS_PENDING, NOTIFICATION_STATUS_FAILED, NOTIFICATION_STATUS_RETRYING}:
                                repo.mark_notification_filtered(record, f"Filtered out: {filter_reason}")
                            self.logger.info(
                                "skipped_existing_case case_number=%s case_id=%s reason=%s",
                                case.get("case_number"),
                                case.get("case_id"),
                                filter_reason,
                            )
                        elif record.notification_status == NOTIFICATION_STATUS_SENT:
                            repo.mark_notification_skipped_existing(record)
                            self.logger.info(
                                "skipped_existing_case case_number=%s case_id=%s notification_status=%s",
                                case.get("case_number"),
                                case.get("case_id"),
                                record.notification_status,
                            )
                        elif repo.is_notification_retry_eligible(
                            record,
                            now=utcnow(),
                            max_attempts=self.settings.mail_max_retry_attempts,
                        ):
                            repo.mark_notification_retrying(record)
                            attempt = self.mailer.send_new_case_email(case, repo, record=record)
                            if attempt.sent:
                                notifications_sent += 1
                                self.logger.info(
                                    "notification_sent case_number=%s case_id=%s",
                                    case.get("case_number"),
                                    case.get("case_id"),
                                )
                            elif not attempt.skipped:
                                notifications_failed += 1
                        else:
                            self.logger.info(
                                "skipped_existing_case case_number=%s case_id=%s notification_status=%s",
                                case.get("case_number"),
                                case.get("case_id"),
                                record.notification_status,
                            )

            if self.settings.email_test_mode and self.settings.send_enabled and sample_case is not None:
                test_summary = self._build_summary(
                    total_fetched=total_fetched,
                    total_unique_cases=total_unique_cases,
                    new_cases=new_cases,
                    existing_cases=existing_cases,
                    notifications_sent=notifications_sent,
                    notifications_failed=notifications_failed,
                    skipped_due_to_filter=skipped_due_to_filter,
                    pages_fetched=pages_fetched,
                    stop_reason=stop_reason,
                    success=True,
                    exit_category=RunExitCategory.SUCCESS if new_cases > 0 else RunExitCategory.SUCCESS_NO_NEW_CASES,
                )
                test_summary["sample_case_status"] = sample_case_status
                if self.mailer.send_test_cycle_email(sample_case=sample_case, repo=repo, summary=test_summary):
                    notifications_sent += 1
                else:
                    notifications_failed += 1

            session.commit()

        exit_category = RunExitCategory.SUCCESS
        if notifications_failed:
            exit_category = RunExitCategory.NOTIFICATION_FAILURE
        elif new_cases == 0:
            exit_category = RunExitCategory.SUCCESS_NO_NEW_CASES

        return self._build_summary(
            total_fetched=total_fetched,
            total_unique_cases=total_unique_cases,
            new_cases=new_cases,
            existing_cases=existing_cases,
            notifications_sent=notifications_sent,
            notifications_failed=notifications_failed,
            skipped_due_to_filter=skipped_due_to_filter,
            pages_fetched=pages_fetched,
            stop_reason=stop_reason,
            success=True,
            exit_category=exit_category,
        )

    def _build_summary(
        self,
        *,
        total_fetched: int,
        total_unique_cases: int,
        new_cases: int,
        existing_cases: int,
        notifications_sent: int,
        notifications_failed: int,
        skipped_due_to_filter: int,
        pages_fetched: int,
        stop_reason: str,
        success: bool,
        exit_category: RunExitCategory,
        operator_action_required: bool = False,
    ) -> dict[str, Any]:
        self.runtime_state.mark_cycle_result(success=success)
        snapshot = self.runtime_state.snapshot()
        return {
            "total_fetched": total_fetched,
            "total_unique_cases": total_unique_cases,
            "new_cases": new_cases,
            "existing_cases": existing_cases,
            "notifications_sent": notifications_sent,
            "notifications_failed": notifications_failed,
            "skipped_existing_cases": existing_cases,
            "skipped_due_to_filter": skipped_due_to_filter,
            "pages_fetched": pages_fetched,
            "stop_reason": stop_reason,
            "success": success,
            "exit_category": str(exit_category),
            "operator_action_required": operator_action_required,
            "uptime_seconds": snapshot.uptime_seconds,
            "successful_cycles": snapshot.successful_cycles,
            "failed_cycles": snapshot.failed_cycles,
            "auth_failures": snapshot.auth_failures,
            "retries_attempted": snapshot.retries_attempted,
            "fetch_strategy_used": self.client.last_fetch_strategy_summary.fetch_strategy_used,
            "fallbacks_attempted": self.client.last_fetch_strategy_summary.fallbacks_attempted,
            "undefined_fields_seen": self.client.last_fetch_strategy_summary.undefined_fields_seen,
            "minimal_mode_used": self.client.last_fetch_strategy_summary.minimal_mode_used,
            "adaptive_retries": self.client.last_fetch_strategy_summary.adaptive_retries,
            "stripped_fields": self.client.last_fetch_strategy_summary.stripped_fields,
            "degraded_mode": self.client.last_fetch_strategy_summary.degraded_mode,
            "final_query_length": self.client.last_fetch_strategy_summary.final_query_length,
        }

    def _log_batch_failure_signals(self, cycle_summary: dict[str, Any]) -> None:
        stop_reason = str(cycle_summary.get("stop_reason") or "")
        if "SESSION_EXPIRED_OR_INVALID" in stop_reason:
            self._log_operator_action_required(
                reason="session_invalid",
                detail=stop_reason,
            )
            return
        if "BROWSER" in stop_reason and cycle_summary.get("auth_failures", 0):
            self._log_operator_action_required(
                reason="browser_authentication_required",
                detail=stop_reason,
            )

    def _log_browser_auth_issue(self, exc: Exception) -> None:
        category = getattr(exc, "category", "")
        if category == "profile_busy":
            return
        diagnostics = getattr(exc, "diagnostics", {}) or {}
        if diagnostics:
            self.logger.error(
                "browser_auth_diagnostics post_submit_url_host=%s post_submit_safe_path=%s post_submit_title=%s post_submit_visible_headings=%s post_submit_visible_buttons=%s auth_stage_history=%s last_auth_stage=%s stable_public_shell_poll_count=%s authenticated_identity_visible=%s real_case_rows_visible=%s login_successful=%s final_authenticated=%s",
                diagnostics.get("post_submit_url_host"),
                diagnostics.get("post_submit_safe_path"),
                diagnostics.get("post_submit_title"),
                diagnostics.get("post_submit_visible_headings"),
                diagnostics.get("post_submit_visible_buttons"),
                diagnostics.get("auth_stage_history"),
                diagnostics.get("last_auth_stage"),
                diagnostics.get("stable_public_shell_poll_count"),
                diagnostics.get("authenticated_identity_visible"),
                diagnostics.get("real_case_rows_visible"),
                diagnostics.get("login_successful"),
                diagnostics.get("final_authenticated"),
            )
        message = str(exc).lower()
        if "mfa_detected" in message:
            self.logger.error("mfa_detected=true")
            self._log_operator_action_required(
                reason="mfa_detected",
                detail=str(exc),
            )
            return
        if any(marker in message for marker in ("manual login required", "login_failure_reason", "session", "auth")):
            self._log_operator_action_required(
                reason="browser_authentication_required",
                detail=str(exc),
            )

    def _map_browser_error_category(self, exc: Exception) -> RunExitCategory:
        category = getattr(exc, "category", "")
        if category == "operator_action_required":
            return RunExitCategory.OPERATOR_ACTION_REQUIRED
        if category == "authentication_failed":
            return RunExitCategory.AUTHENTICATION_FAILURE
        if category == "profile_busy":
            return RunExitCategory.PROFILE_BUSY
        if category == "extraction_failure":
            return RunExitCategory.EXTRACTION_FAILURE
        return RunExitCategory.UPSTREAM_RUNTIME_FAILURE

    def _log_operator_action_required(self, *, reason: str, detail: str) -> None:
        self.logger.error("operator_action_required=true reason=%s detail=%s", reason, detail)

    def _should_notify_case(self, case: dict[str, Any]) -> tuple[bool, str]:
        priority = (case.get("priority") or "").strip()
        status = (case.get("status") or "").strip()
        product = (case.get("product_name") or "").strip()

        if self.settings.notification_allowed_priorities and priority not in self.settings.notification_allowed_priorities:
            return False, f"priority '{priority or '<empty>'}' not allowed"
        if self.settings.notification_allowed_statuses and status not in self.settings.notification_allowed_statuses:
            return False, f"status '{status or '<empty>'}' not allowed"
        if self.settings.notification_allowed_products and product not in self.settings.notification_allowed_products:
            return False, f"product '{product or '<empty>'}' not allowed"
        return True, "matched filters"
