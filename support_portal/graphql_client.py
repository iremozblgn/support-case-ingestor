from __future__ import annotations

import logging
import json
import hashlib
import random
import re
import time
from urllib.parse import urlencode
from pprint import pformat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from support_portal.browser_parity import BrowserParityRequest, BrowserParityRequestBuilder
from support_portal.fetch_strategies import STRATEGY_REGISTRY, BaseGraphQLStrategy
from support_portal.graphql_payloads import build_aura_form_payload
from support_portal.graphql_query_utils import list_graphql_field_names, remove_graphql_field
from support_portal.parser import (
    parse_case_connection,
    parse_case_edges,
    parse_page_info,
    parse_total_count,
)
from support_portal.session import create_support_portal_session, mask_secret, sanitize_headers
from services.runtime_state import RuntimeHealthState


class SupportPortalGraphQLError(Exception):
    """Base error for SupportPortal GraphQL failures."""


class SupportPortalAuthError(SupportPortalGraphQLError):
    """Authentication or session related failure."""


class SupportPortalRequestError(SupportPortalGraphQLError):
    """Transport level failure."""


class SupportPortalResponseError(SupportPortalGraphQLError):
    """Unexpected or invalid HTTP/JSON response."""


class SupportPortalParsingError(SupportPortalGraphQLError):
    """Unexpected response structure."""


class SupportPortalSessionExpiredError(SupportPortalAuthError):
    """Session expired or invalid credentials/token/cookie."""


class SupportPortalFieldUndefinedError(SupportPortalGraphQLError):
    """Schema validation failure caused by undefined GraphQL fields."""

    def __init__(
        self,
        *,
        strategy_name: str,
        undefined_fields: list[str],
        message: str,
    ) -> None:
        super().__init__(message)
        self.strategy_name = strategy_name
        self.undefined_fields = undefined_fields
        self.full_message = message


class SupportPortalZeroResultsError(SupportPortalGraphQLError):
    """All fallback strategies completed but produced zero cases."""

    def __init__(self, message: str = "All GraphQL fetch attempts returned zero cases") -> None:
        super().__init__(message)


@dataclass(slots=True)
class GraphQLPageResult:
    payload: dict[str, Any]
    page_number: int
    status_code: int
    total_count: int
    page_info: dict[str, Any]
    case_count: int
    requested_after: str | None
    start_cursor: str | None
    end_cursor: str | None
    has_next_page: bool
    first_case_id: str | None
    last_case_id: str | None
    case_ids: list[str]
    strategy_name: str
    where_mode: str
    adaptive_retries: int
    stripped_fields: list[str]
    degraded_mode: bool
    final_query_length: int


@dataclass(slots=True)
class PaginationSummary:
    total_unique_cases: int
    total_pages_fetched: int
    duplicate_pages_detected: int
    stop_reason: str


@dataclass(slots=True)
class FetchStrategySummary:
    fetch_strategy_used: str | None
    fallbacks_attempted: list[str]
    undefined_fields_seen: list[str]
    minimal_mode_used: bool
    attempted_fetches: list[dict[str, Any]]
    adaptive_retries: int
    stripped_fields: list[str]
    degraded_mode: bool
    final_query_length: int


class SupportPortalGraphQLClient:
    def __init__(
        self,
        settings: Any,
        session: requests.Session | None = None,
        runtime_state: RuntimeHealthState | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.session = session or create_support_portal_session(
            base_url=settings.support_portal_graphql_url,
            cookie=settings.support_portal_cookie,
        )
        self.last_pagination_summary: PaginationSummary | None = None
        self.last_fetch_strategy_summary = FetchStrategySummary(
            fetch_strategy_used=None,
            fallbacks_attempted=[],
            undefined_fields_seen=[],
            minimal_mode_used=False,
            attempted_fetches=[],
            adaptive_retries=0,
            stripped_fields=[],
            degraded_mode=False,
            final_query_length=0,
        )
        self.runtime_state = runtime_state or RuntimeHealthState()
        self.browser_parity_builder = BrowserParityRequestBuilder(settings)
        self._adaptive_required_fields = {
            "Id",
            "CaseNumber",
            "Subject",
            "Status",
            "Priority",
            "CreatedDate",
        }
        self._adaptive_degraded_fields = {
            "Account",
            "Contact",
            "Product_Name__c",
            "Action_Required__c",
            "Partners_Customer_Account__c",
            "Partners_Customer_Account__r",
            "Case_Owner__c",
            "isPartnerCustomer__c",
            "isCaseContactisUser__c",
        }
        self._adaptive_retry_limit = 12

    def fetch_cases_page(
        self,
        *,
        page_number: int = 1,
        after: str | None = None,
        page_size: int | None = None,
        where_clause: dict[str, Any] | None = None,
        order_by: dict[str, Any] | None = None,
        strategy: BaseGraphQLStrategy | None = None,
        request_spec: Any | None = None,
    ) -> GraphQLPageResult:
        active_strategy = strategy or self._instantiate_strategy("full")
        active_request_spec = request_spec or active_strategy.build_request_specs(
            settings=self.settings,
            page_size=page_size or self.settings.page_size,
            after=after,
            requested_where_clause=where_clause,
            order_by=order_by,
            browser_where_clause=self._load_browser_where_clause(),
        )[0]
        explicit_page_size = page_size is not None
        active_request_spec.final_query_length = len(active_request_spec.query)

        while True:
            payload = self._build_request_payload(
                aura_context=self.settings.support_portal_aura_context,
                aura_token=self.settings.support_portal_aura_token,
                action_id=f"{page_number};a",
                page_size=page_size or self.settings.page_size,
                explicit_page_size=explicit_page_size,
                after=after,
                where_clause=where_clause,
                order_by=order_by,
                page_uri=self.settings.support_portal_page_uri,
                strategy=active_strategy,
                request_spec=active_request_spec,
            )
            self.logger.info(
                "Starting GraphQL request for page %s after=%s strategy=%s where_mode=%s",
                page_number,
                after,
                active_strategy.name,
                active_request_spec.where_mode,
            )
            outgoing_request = self.browser_parity_builder.build_request(
                generated_url=self.settings.support_portal_graphql_url,
                session_headers=dict(self.session.headers),
                form_payload=payload,
            )
            self._log_request_debug(outgoing_request)

            try:
                response = self._post_with_retry(
                    request=outgoing_request,
                    page_number=page_number,
                    strategy=active_strategy,
                )
                break
            except SupportPortalFieldUndefinedError as exc:
                removed_fields = self._adapt_query_for_undefined_fields(
                    request_spec=active_request_spec,
                    undefined_fields=exc.undefined_fields,
                    strategy_name=active_strategy.name,
                )
                if not removed_fields:
                    raise

        self.logger.info("GraphQL response status=%s page=%s", response.status_code, page_number)
        self.logger.debug("Aura raw response preview=%s", response.text[:4000])
        response_json = getattr(response, "_support_portal_json")

        try:
            case_connection = parse_case_connection(response_json)
            page_info = parse_page_info(response_json)
            total_count = parse_total_count(response_json)
            extracted_cases = parse_case_edges(
                response_json,
                base_case_url=self.settings.portal_case_base_url,
            )
        except ValueError as exc:
            raise SupportPortalParsingError(str(exc)) from exc

        edges = case_connection.get("edges") or []
        case_ids = [case.get("case_id") for case in extracted_cases if case.get("case_id")]
        start_cursor = page_info.get("startCursor")
        end_cursor = page_info.get("endCursor")
        has_next_page = bool(page_info.get("hasNextPage"))
        first_case_id = case_ids[0] if case_ids else None
        last_case_id = case_ids[-1] if case_ids else None
        self.logger.info(
            "Extracted cases from Aura response page=%s count=%s",
            page_number,
            len(extracted_cases),
        )
        self.logger.info(
            "Aura result summary page=%s totalCount=%s edges=%s",
            page_number,
            total_count,
            len(edges) if isinstance(edges, list) else 0,
        )
        self.logger.info(
            "Pagination page=%s requested_after=%s startCursor=%s endCursor=%s hasNextPage=%s edge_count=%s first_case_id=%s last_case_id=%s",
            page_number,
            after,
            start_cursor,
            end_cursor,
            has_next_page,
            len(extracted_cases),
            first_case_id,
            last_case_id,
        )
        return GraphQLPageResult(
            payload=response_json,
            page_number=page_number,
            status_code=response.status_code,
            total_count=total_count,
            page_info=page_info,
            case_count=len(edges) if isinstance(edges, list) else 0,
            requested_after=after,
            start_cursor=start_cursor,
            end_cursor=end_cursor,
            has_next_page=has_next_page,
            first_case_id=first_case_id,
            last_case_id=last_case_id,
            case_ids=case_ids,
            strategy_name=active_strategy.name,
            where_mode=active_request_spec.where_mode,
            adaptive_retries=active_request_spec.adaptive_retries,
            stripped_fields=list(active_request_spec.stripped_fields),
            degraded_mode=active_request_spec.degraded_mode,
            final_query_length=active_request_spec.final_query_length,
        )

    def fetch_all_case_pages(
        self,
        *,
        page_size: int | None = None,
        where_clause: dict[str, Any] | None = None,
        order_by: dict[str, Any] | None = None,
    ) -> list[GraphQLPageResult]:
        strategies = self._resolve_strategy_order()
        fallbacks_attempted: list[str] = []
        undefined_fields_seen: list[str] = []
        last_field_error: SupportPortalFieldUndefinedError | None = None
        attempted_fetches: list[dict[str, Any]] = []
        browser_where_clause = self._load_browser_where_clause()
        total_adaptive_retries = 0
        all_stripped_fields: list[str] = []
        any_degraded_mode = False

        for strategy_index, strategy in enumerate(strategies):
            request_specs = strategy.build_request_specs(
                settings=self.settings,
                page_size=page_size or self.settings.page_size,
                after=None,
                requested_where_clause=where_clause,
                order_by=order_by,
                browser_where_clause=browser_where_clause,
            )
            for spec_index, request_spec in enumerate(request_specs):
                try:
                    pages = self._fetch_all_case_pages_with_strategy(
                        strategy=strategy,
                        request_spec=request_spec,
                        page_size=page_size,
                        where_clause=where_clause,
                        order_by=order_by,
                    )
                    total_count = pages[0].total_count if pages else 0
                    parsed_case_count = sum(
                        len(
                            parse_case_edges(
                                page.payload,
                                base_case_url=self.settings.portal_case_base_url,
                            )
                        )
                        for page in pages
                    )
                    attempted_fetches.append(
                        {
                            "strategy": strategy.name,
                            "where_mode": request_spec.where_mode,
                            "totalCount": total_count,
                            "parsed_cases": parsed_case_count,
                            "result": "success",
                            "adaptive_retries": request_spec.adaptive_retries,
                            "stripped_fields": list(request_spec.stripped_fields),
                            "degraded_mode": request_spec.degraded_mode,
                            "final_query_length": request_spec.final_query_length,
                        }
                    )
                    total_adaptive_retries += request_spec.adaptive_retries
                    for field_name in request_spec.stripped_fields:
                        if field_name not in all_stripped_fields:
                            all_stripped_fields.append(field_name)
                    any_degraded_mode = any_degraded_mode or request_spec.degraded_mode
                    if (
                        self.settings.support_portal_enable_strategy_fallback
                        and total_count == 0
                        and parsed_case_count == 0
                    ):
                        has_more_attempts = (
                            strategy_index < len(strategies) - 1
                            or spec_index < len(request_specs) - 1
                        )
                        if has_more_attempts:
                            self.logger.warning(
                                "GraphQL strategy returned zero cases, trying next fallback: strategy=%s where_mode=%s",
                                strategy.name,
                                request_spec.where_mode,
                            )
                            fallbacks_attempted.append(f"{strategy.name}[{request_spec.where_mode}]")
                            continue
                        self.last_fetch_strategy_summary = FetchStrategySummary(
                            fetch_strategy_used=strategy.name,
                            fallbacks_attempted=fallbacks_attempted,
                            undefined_fields_seen=sorted(set(undefined_fields_seen)),
                            minimal_mode_used=strategy.key == "minimal",
                            attempted_fetches=attempted_fetches,
                            adaptive_retries=total_adaptive_retries,
                            stripped_fields=list(all_stripped_fields),
                            degraded_mode=any_degraded_mode,
                            final_query_length=request_spec.final_query_length,
                        )
                        if self.browser_parity_builder.snapshot is not None:
                            self.logger.warning(
                                "possible_context_mismatch=true reason=browser_parity_snapshot_present_and_replay_returned_zero_results"
                            )
                        raise SupportPortalZeroResultsError()

                    self.last_fetch_strategy_summary = FetchStrategySummary(
                        fetch_strategy_used=strategy.name,
                        fallbacks_attempted=fallbacks_attempted,
                        undefined_fields_seen=sorted(set(undefined_fields_seen)),
                        minimal_mode_used=strategy.key == "minimal",
                        attempted_fetches=attempted_fetches,
                        adaptive_retries=total_adaptive_retries,
                        stripped_fields=list(all_stripped_fields),
                        degraded_mode=any_degraded_mode,
                        final_query_length=request_spec.final_query_length,
                    )
                    if strategy.key == "minimal" and self.settings.support_portal_fail_on_minimal_only:
                        raise SupportPortalResponseError(
                            "GraphQL minimal strategy succeeded, but SUPPORT_PORTAL_FAIL_ON_MINIMAL_ONLY=true"
                        )
                    return pages
                except SupportPortalFieldUndefinedError as exc:
                    last_field_error = exc
                    undefined_fields_seen.extend(exc.undefined_fields)
                    attempted_fetches.append(
                        {
                            "strategy": strategy.name,
                            "where_mode": request_spec.where_mode,
                            "totalCount": 0,
                            "parsed_cases": 0,
                            "result": "field_undefined",
                            "adaptive_retries": request_spec.adaptive_retries,
                            "stripped_fields": list(request_spec.stripped_fields),
                            "degraded_mode": request_spec.degraded_mode,
                            "final_query_length": request_spec.final_query_length,
                        }
                    )
                    total_adaptive_retries += request_spec.adaptive_retries
                    for field_name in request_spec.stripped_fields:
                        if field_name not in all_stripped_fields:
                            all_stripped_fields.append(field_name)
                    any_degraded_mode = any_degraded_mode or request_spec.degraded_mode
                    self.logger.warning(
                        "GraphQL strategy failed due to undefined fields: strategy=%s undefined_fields=%s message=%s",
                        exc.strategy_name,
                        exc.undefined_fields,
                        exc.full_message,
                    )
                    is_last_attempt = (
                        strategy_index == len(strategies) - 1
                        and spec_index == len(request_specs) - 1
                    )
                    if not self.settings.support_portal_enable_strategy_fallback or is_last_attempt:
                        self.last_fetch_strategy_summary = FetchStrategySummary(
                            fetch_strategy_used=exc.strategy_name,
                            fallbacks_attempted=fallbacks_attempted,
                            undefined_fields_seen=sorted(set(undefined_fields_seen)),
                            minimal_mode_used=exc.strategy_name == "GraphQLMinimalStrategy",
                            attempted_fetches=attempted_fetches,
                            adaptive_retries=total_adaptive_retries,
                            stripped_fields=list(all_stripped_fields),
                            degraded_mode=any_degraded_mode,
                            final_query_length=request_spec.final_query_length,
                        )
                        raise SupportPortalResponseError(
                            "All configured GraphQL fetch strategies failed due to undefined fields. "
                            f"Last strategy={exc.strategy_name}. Fields={exc.undefined_fields}. "
                            f"Message={exc.full_message}"
                        ) from exc
                    fallbacks_attempted.append(f"{exc.strategy_name}[{request_spec.where_mode}]")

        if last_field_error is not None:
            raise SupportPortalResponseError(last_field_error.full_message) from last_field_error
        self.last_fetch_strategy_summary = FetchStrategySummary(
            fetch_strategy_used=None,
            fallbacks_attempted=fallbacks_attempted,
            undefined_fields_seen=sorted(set(undefined_fields_seen)),
            minimal_mode_used=False,
            attempted_fetches=attempted_fetches,
            adaptive_retries=total_adaptive_retries,
            stripped_fields=list(all_stripped_fields),
            degraded_mode=any_degraded_mode,
            final_query_length=0,
        )
        if self.browser_parity_builder.snapshot is not None:
            self.logger.warning(
                "possible_context_mismatch=true reason=browser_parity_snapshot_present_and_all_replay_attempts_returned_zero_results"
            )
        raise SupportPortalZeroResultsError()

    def _fetch_all_case_pages_with_strategy(
        self,
        *,
        strategy: BaseGraphQLStrategy,
        request_spec: Any,
        page_size: int | None,
        where_clause: dict[str, Any] | None,
        order_by: dict[str, Any] | None,
    ) -> list[GraphQLPageResult]:
        pages: list[GraphQLPageResult] = []
        after: str | None = None
        page_number = 1
        max_pages = max(1, self.settings.max_pages_per_cycle)
        seen_cursors: set[str] = set()
        seen_case_ids: set[str] = set()
        seen_page_signatures: set[tuple[str | None, str | None, tuple[str, ...]]] = set()
        previous_end_cursor: str | None = None
        duplicate_pages_detected = 0
        stop_reason = "hasNextPage=false"

        while True:
            result = self.fetch_cases_page(
                page_number=page_number,
                after=after,
                page_size=page_size,
                where_clause=where_clause,
                order_by=order_by,
                strategy=strategy,
                request_spec=request_spec,
            )
            pages.append(result)
            self.logger.info(
                "Fetched GraphQL page=%s cases=%s total=%s has_next=%s strategy=%s where_mode=%s",
                result.page_number,
                result.case_count,
                result.total_count,
                result.has_next_page,
                strategy.name,
                request_spec.where_mode,
            )
            page_signature = (
                result.first_case_id,
                result.last_case_id,
                tuple(result.case_ids),
            )
            new_case_ids = [case_id for case_id in result.case_ids if case_id not in seen_case_ids]

            if result.end_cursor and result.end_cursor == previous_end_cursor:
                duplicate_pages_detected += 1
                stop_reason = "endCursor repeated"
                break
            if result.end_cursor and result.end_cursor in seen_cursors:
                duplicate_pages_detected += 1
                stop_reason = "cursor repeated"
                break
            if page_signature in seen_page_signatures:
                duplicate_pages_detected += 1
                stop_reason = "page signature repeated"
                break
            if result.case_ids and not new_case_ids:
                duplicate_pages_detected += 1
                stop_reason = "no new unique case IDs discovered"
                break

            seen_page_signatures.add(page_signature)
            seen_case_ids.update(result.case_ids)
            if result.end_cursor:
                seen_cursors.add(result.end_cursor)
            previous_end_cursor = result.end_cursor

            if result.total_count and len(seen_case_ids) >= result.total_count:
                stop_reason = "fetched_cases >= totalCount"
                break
            if page_number >= max_pages:
                stop_reason = f"max_pages_per_cycle reached ({max_pages})"
                break
            if not result.has_next_page:
                stop_reason = "hasNextPage=false"
                break

            after = result.end_cursor
            if not after:
                stop_reason = "hasNextPage=true but endCursor missing"
                break
            page_number += 1

        self.last_pagination_summary = PaginationSummary(
            total_unique_cases=len(seen_case_ids),
            total_pages_fetched=len(pages),
            duplicate_pages_detected=duplicate_pages_detected,
            stop_reason=stop_reason,
        )
        self.logger.info(
            "Pagination summary: total_unique_cases=%s total_pages_fetched=%s duplicate_pages_detected=%s stop_reason=%s strategy=%s where_mode=%s",
            self.last_pagination_summary.total_unique_cases,
            self.last_pagination_summary.total_pages_fetched,
            self.last_pagination_summary.duplicate_pages_detected,
            self.last_pagination_summary.stop_reason,
            strategy.name,
            request_spec.where_mode,
        )
        return pages

    def _post_with_retry(
        self,
        *,
        request: BrowserParityRequest,
        page_number: int,
        strategy: BaseGraphQLStrategy,
    ) -> requests.Response:
        max_retries = max(0, self.settings.session_max_retries)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = self.session.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    data=request.body_fields,
                    timeout=self.settings.request_timeout_seconds,
                )
                response_json = self._parse_response_json(response, strategy=strategy)
                self._mark_request_success(http_status=response.status_code)
                response._support_portal_json = response_json  # type: ignore[attr-defined]
                return response
            except SupportPortalFieldUndefinedError as exc:
                self._mark_request_failure(
                    session_invalid=False,
                    http_status=getattr(exc, "http_status", None),
                    aura_error=exc.full_message,
                )
                raise
            except requests.RequestException as exc:
                wrapped_error = SupportPortalRequestError(f"Network/transport error: {exc}")
                last_error = wrapped_error
                self._mark_request_failure(
                    session_invalid=False,
                    http_status=None,
                    aura_error=str(wrapped_error),
                )
                if attempt >= max_retries:
                    self.logger.error(
                        "Max retries exceeded for page=%s after=%s",
                        page_number,
                        None,
                    )
                    break
                delay = self._compute_retry_delay(attempt)
                self.runtime_state.record_retry()
                self.logger.warning(
                    "retrying request page=%s attempt=%s/%s delay=%.2fs reason=%s",
                    page_number,
                    attempt + 1,
                    max_retries,
                    delay,
                    wrapped_error,
                )
                time.sleep(delay)
            except (SupportPortalSessionExpiredError, SupportPortalRequestError, SupportPortalResponseError) as exc:
                last_error = exc
                session_invalid = isinstance(exc, SupportPortalSessionExpiredError)
                http_status = getattr(exc, "http_status", None)
                aura_error = str(exc)
                self._mark_request_failure(
                    session_invalid=session_invalid,
                    http_status=http_status,
                    aura_error=aura_error,
                )
                if attempt >= max_retries:
                    self.logger.error(
                        "Max retries exceeded for page=%s after=%s",
                        page_number,
                        None,
                    )
                    break
                delay = self._compute_retry_delay(attempt)
                self.runtime_state.record_retry()
                self.logger.warning(
                    "retrying request page=%s attempt=%s/%s delay=%.2fs reason=%s",
                    page_number,
                    attempt + 1,
                    max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)

        if last_error is None:
            raise SupportPortalRequestError("GraphQL request failed without a captured error")
        raise last_error

    def _parse_response_json(
        self,
        response: requests.Response,
        *,
        strategy: BaseGraphQLStrategy,
    ) -> dict[str, Any]:
        self._raise_for_auth_failure(response)

        if response.status_code >= 400:
            error = SupportPortalResponseError(
                f"GraphQL endpoint returned HTTP {response.status_code}: {response.text[:500]}"
            )
            error.http_status = response.status_code  # type: ignore[attr-defined]
            raise error

        try:
            response_json = response.json()
        except ValueError as exc:
            error = SupportPortalResponseError("GraphQL endpoint returned non-JSON content")
            error.http_status = response.status_code  # type: ignore[attr-defined]
            raise error from exc

        self.logger.debug("Aura parsed response preview=%s", pformat(self._trim_payload(response_json)))
        self._raise_for_aura_errors(response_json, strategy_name=strategy.name)
        return response_json

    def _compute_retry_delay(self, attempt: int) -> float:
        base = max(1, self.settings.session_retry_base_seconds)
        maximum = max(base, self.settings.session_retry_max_seconds)
        delay = min(maximum, base * (2 ** attempt))
        if self.settings.session_jitter_enabled:
            delay += random.uniform(0, min(1.0, delay * 0.25))
        return delay

    def _mark_request_success(self, *, http_status: int | None) -> None:
        recovered_from_invalid, recovered_from_degraded = self.runtime_state.mark_request_success(
            http_status=http_status
        )
        if recovered_from_invalid or recovered_from_degraded:
            self.logger.info("session recovered; returning to healthy state")

    def _mark_request_failure(
        self,
        *,
        session_invalid: bool,
        http_status: int | None,
        aura_error: str | None,
    ) -> None:
        became_invalid, entered_degraded = self.runtime_state.mark_request_failure(
            http_status=http_status,
            aura_error=aura_error,
            session_invalid=session_invalid,
        )
        if entered_degraded:
            self.logger.warning("entering degraded mode")
        if became_invalid:
            self.logger.error("session became invalid")

    def _raise_for_auth_failure(self, response: requests.Response) -> None:
        if response.status_code in {401, 403}:
            error = SupportPortalSessionExpiredError(
                "SESSION_EXPIRED_OR_INVALID: "
                f"Authentication failed with HTTP {response.status_code}. "
                "Refresh SUPPORT_PORTAL_COOKIE, SUPPORT_PORTAL_AURA_CONTEXT, SUPPORT_PORTAL_AURA_TOKEN from DevTools."
            )
            error.http_status = response.status_code  # type: ignore[attr-defined]
            raise error

        lowered = response.text.lower()
        auth_markers = [
            "invalid_session_id",
            "session expired",
            "access denied",
            "aura.token",
            "login",
            "serveractionerror",
        ]
        if any(marker in lowered for marker in auth_markers) and "uiapi" not in lowered:
            error = SupportPortalSessionExpiredError(
                "SESSION_EXPIRED_OR_INVALID: "
                "SupportPortal session appears expired or unauthorized. "
                "Refresh SUPPORT_PORTAL_COOKIE, SUPPORT_PORTAL_AURA_CONTEXT, SUPPORT_PORTAL_AURA_TOKEN from DevTools."
            )
            error.http_status = response.status_code  # type: ignore[attr-defined]
            raise error

        content_type = response.headers.get("content-type", "").lower()
        text = response.text.lstrip()
        if "<html" in text.lower() or ("text/html" in content_type and "json" not in content_type):
            error = SupportPortalSessionExpiredError(
                "SESSION_EXPIRED_OR_INVALID: received HTML instead of JSON. "
                "Refresh SUPPORT_PORTAL_COOKIE, SUPPORT_PORTAL_AURA_CONTEXT, SUPPORT_PORTAL_AURA_TOKEN from DevTools."
            )
            error.http_status = response.status_code  # type: ignore[attr-defined]
            raise error

    def _raise_for_aura_errors(self, payload: dict[str, Any], *, strategy_name: str) -> None:
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions:
            top_level_keys = ", ".join(sorted(payload.keys())) or "<none>"
            raise SupportPortalResponseError(
                "Aura response does not contain actions. "
                f"Top-level keys: {top_level_keys}. "
                "This usually means the session cookie, aura.context, or aura.token is stale."
            )

        first_action = actions[0]
        if not isinstance(first_action, dict):
            raise SupportPortalResponseError("Aura response has invalid action payload")

        errors = first_action.get("error") or []
        if errors:
            message = self._join_error_messages(errors)
            undefined_fields = self._extract_undefined_fields(message)
            if undefined_fields:
                raise SupportPortalFieldUndefinedError(
                    strategy_name=strategy_name,
                    undefined_fields=undefined_fields,
                    message=message,
                )
            lowered = message.lower()
            if (
                "auth" in lowered
                or "token" in lowered
                or "invalid_session_id" in lowered
                or "serveractionerror" in lowered
            ):
                error = SupportPortalSessionExpiredError(
                    "SESSION_EXPIRED_OR_INVALID: "
                    f"{message}. Refresh SUPPORT_PORTAL_COOKIE, SUPPORT_PORTAL_AURA_CONTEXT, SUPPORT_PORTAL_AURA_TOKEN from DevTools."
                )
                error.http_status = None  # type: ignore[attr-defined]
                raise error
            raise SupportPortalResponseError(message)

        return_value = first_action.get("returnValue")
        if isinstance(return_value, dict):
            gql_errors = return_value.get("errors") or []
            if gql_errors:
                message = self._join_error_messages(gql_errors)
                undefined_fields = self._extract_undefined_fields(message)
                if undefined_fields:
                    raise SupportPortalFieldUndefinedError(
                        strategy_name=strategy_name,
                        undefined_fields=undefined_fields,
                        message=message,
                    )
                lowered = message.lower()
                if "invalid_session_id" in lowered or "token" in lowered:
                    error = SupportPortalSessionExpiredError(
                        "SESSION_EXPIRED_OR_INVALID: "
                        f"{message}. Refresh SUPPORT_PORTAL_COOKIE, SUPPORT_PORTAL_AURA_CONTEXT, SUPPORT_PORTAL_AURA_TOKEN from DevTools."
                    )
                    error.http_status = None  # type: ignore[attr-defined]
                    raise error
                raise SupportPortalResponseError(
                    f"Aura GraphQL validation/runtime errors: {message}"
                )

    def _payload_preview(self, payload: dict[str, str]) -> dict[str, Any]:
        preview = dict(payload)
        if "aura.context" in preview:
            preview["aura.context"] = mask_secret(preview["aura.context"])
        if "aura.token" in preview:
            preview["aura.token"] = mask_secret(preview["aura.token"])
        if "message" in preview:
            preview["message"] = f"{preview['message'][:1200]}..." if len(preview["message"]) > 1200 else preview["message"]
        return preview

    def _build_request_payload(
        self,
        *,
        aura_context: str,
        aura_token: str,
        action_id: str,
        page_size: int,
        explicit_page_size: bool,
        after: str | None,
        where_clause: dict[str, Any] | None,
        order_by: dict[str, Any] | None,
        page_uri: str | None,
        strategy: BaseGraphQLStrategy,
        request_spec: Any,
    ) -> dict[str, str]:
        if (
            self.settings.browser_parity_strict
            and self.browser_parity_builder.snapshot is not None
            and strategy.use_browser_payload
        ):
            return self._build_request_from_snapshot_strict(
                aura_context=aura_context,
                aura_token=aura_token,
                action_id=action_id,
                request_spec=request_spec,
            )

        payload = build_aura_form_payload(
            aura_context=aura_context,
            aura_token=aura_token,
            action_id=action_id,
            query=request_spec.query,
            variables=request_spec.variables,
            page_size=page_size,
            after=after,
            where_clause=where_clause,
            order_by=order_by,
            page_uri=page_uri,
        )
        if not strategy.use_browser_payload:
            return payload

        raw_message = self.settings.support_portal_browser_parity_message
        file_message = self._load_browser_message_from_file()
        if file_message:
            raw_message = file_message
        if not raw_message:
            self.logger.warning(
                "Browser parity mode enabled but no browser payload file/message is available; "
                "falling back to generated payload."
            )
            return payload

        try:
            raw_json = json.loads(raw_message)
        except json.JSONDecodeError:
            self.logger.warning(
                "SUPPORT_PORTAL_BROWSER_PARITY_MESSAGE is not valid JSON; using it verbatim."
            )
            payload["message"] = raw_message
            return payload

        actions = raw_json.get("actions")
        if isinstance(actions, list) and actions and isinstance(actions[0], dict):
            original_action = json.loads(json.dumps(actions[0]))
            actions[0]["id"] = action_id
            query_input = actions[0].get("params", {}).get("queryInput")
            if isinstance(query_input, dict):
                query_input["query"] = request_spec.query
                variables = query_input.get("variables")
                if isinstance(variables, dict):
                    updated_variables = {
                        **variables,
                        **request_spec.variables,
                    }
                    if not explicit_page_size and "pageSize" in updated_variables and "pageSize" not in variables:
                        updated_variables.pop("pageSize", None)
                    query_input["variables"] = updated_variables
            self._log_payload_diff(original_action, actions[0])
        payload["message"] = json.dumps(raw_json, separators=(",", ":"))
        return payload

    def _build_request_from_snapshot_strict(
        self,
        *,
        aura_context: str,
        aura_token: str,
        action_id: str,
        request_spec: Any,
    ) -> dict[str, str]:
        snapshot = self.browser_parity_builder.snapshot
        if snapshot is None:
            raise SupportPortalResponseError("Strict browser parity requested, but no snapshot is loaded")

        payload = {name: value for name, value in snapshot.body_fields}
        message_raw = payload.get("message")
        snapshot_variables_preserved = True

        if not message_raw:
            raise SupportPortalResponseError("Strict browser parity snapshot is missing message body field")

        try:
            message_json = json.loads(message_raw)
        except json.JSONDecodeError as exc:
            raise SupportPortalResponseError("Strict browser parity snapshot message is not valid JSON") from exc

        actions = message_json.get("actions")
        if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
            raise SupportPortalResponseError("Strict browser parity snapshot message missing actions[0]")

        action = actions[0]
        original_message_query = (
            action.get("params", {})
            .get("queryInput", {})
            .get("query")
        )

        query_input = action.get("params", {}).get("queryInput")
        if not isinstance(query_input, dict):
            raise SupportPortalResponseError("Strict browser parity snapshot missing queryInput")

        should_apply_adaptive_query = bool(
            getattr(request_spec, "adaptive_retries", 0)
            or getattr(request_spec, "stripped_fields", [])
        )
        if should_apply_adaptive_query and request_spec.query != original_message_query:
            query_input["query"] = request_spec.query
            self.logger.warning(
                "STRICT PARITY MODE ACTIVE with adaptive query mutation strategy=%s",
                getattr(request_spec, "where_mode", "configured"),
            )

        payload["message"] = json.dumps(message_json, separators=(",", ":"))
        if aura_token:
            payload["aura.token"] = aura_token
        if aura_context:
            payload["aura.context"] = aura_context

        self.logger.info("STRICT PARITY MODE ACTIVE")
        self.logger.info("STRICT PARITY USING CAPTURED VARIABLES")
        self.logger.info("STRICT PARITY USING CAPTURED REFERER")
        self.logger.info("STRICT PARITY USING CAPTURED PAGE URI")
        self.logger.info(
            "strict_parity_enabled=true snapshot_variables_preserved=%s snapshot_referer_preserved=true snapshot_pageuri_preserved=true",
            snapshot_variables_preserved,
        )
        return payload

    def _load_browser_message_from_file(self) -> str | None:
        path = Path(self.settings.support_portal_browser_parity_message_path)
        if not path.exists():
            self.logger.warning("Browser parity payload file not found: %s", path)
            return None

        raw_message = path.read_text(encoding="utf-8").strip()
        if not raw_message:
            self.logger.warning("Browser parity payload file is empty: %s", path)
            return None

        payload_length = len(raw_message.encode("utf-8"))
        payload_hash = hashlib.sha256(raw_message.encode("utf-8")).hexdigest()
        truncated = not raw_message.endswith("}") and not raw_message.endswith("]")
        self.logger.info(
            "Loaded browser parity payload file=%s bytes=%s sha256=%s truncated=%s",
            path,
            payload_length,
            payload_hash,
            truncated,
        )

        try:
            payload_json = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            raise SupportPortalResponseError(
                f"Browser parity payload file is not valid JSON: {path} ({exc})"
            ) from exc

        query_input = (
            payload_json.get("actions", [{}])[0]
            .get("params", {})
            .get("queryInput")
        )
        if not isinstance(query_input, dict):
            raise SupportPortalResponseError(
                f"Browser parity payload file missing actions[0].params.queryInput: {path}"
            )

        self.logger.info(
            "Browser parity payload queryInput present keys=%s",
            sorted(query_input.keys()),
        )
        return raw_message

    def _log_request_debug(self, request: BrowserParityRequest) -> None:
        self._log_browser_parity_diff(request)
        if self.settings.support_portal_debug_raw_request:
            body_fields = dict(request.body_fields)
            self.logger.debug("Aura final URL=%s", request.url)
            self.logger.debug("Aura headers (raw)=%s", request.headers)
            self.logger.debug("Aura cookies (raw)=%s", request.headers.get("Cookie", ""))
            self.logger.debug("Aura form body (raw)=%s", request.encoded_body)
            self.logger.debug("Aura message JSON (raw)=%s", body_fields.get("message"))
            self.logger.debug("Aura context (raw)=%s", body_fields.get("aura.context"))
            self.logger.debug("Aura pageURI=%s", body_fields.get("aura.pageURI"))
            self.logger.debug("Aura token exists=%s", bool(body_fields.get("aura.token")))
            return

        self.logger.debug(
            "Aura request url=%s headers=%s payload=%s",
            request.url,
            sanitize_headers(dict(request.headers)),
            self._payload_preview(dict(request.body_fields)),
        )

    def _log_browser_parity_diff(self, request: BrowserParityRequest) -> None:
        if request.diff is None:
            return
        self.logger.info(
            "Browser parity diff parity_score=%s missing_headers=%s changed_headers=%s changed_query_params=%s changed_cookies=%s body_length_change=%s changed_graphql_variables=%s changed_body_fields=%s",
            request.diff.parity_score,
            request.diff.missing_headers,
            sorted(request.diff.changed_headers),
            sorted(request.diff.changed_query_params),
            sorted(request.diff.changed_cookies),
            request.diff.body_length_change,
            sorted(request.diff.changed_graphql_variables),
            sorted(request.diff.changed_body_fields),
        )

    def _log_payload_diff(self, browser_action: dict[str, Any], outgoing_action: dict[str, Any]) -> None:
        differences = self._diff_dicts(browser_action, outgoing_action)
        if differences:
            self.logger.info("Browser vs Python payload differences: %s", differences)
        else:
            self.logger.info("Browser vs Python payload differences: no changes")

    def _diff_dicts(
        self,
        browser_value: Any,
        outgoing_value: Any,
        *,
        path: str = "",
    ) -> list[str]:
        differences: list[str] = []
        if isinstance(browser_value, dict) and isinstance(outgoing_value, dict):
            for key in sorted(set(browser_value) | set(outgoing_value)):
                next_path = f"{path}.{key}" if path else key
                if key not in browser_value:
                    differences.append(f"{next_path}: added={outgoing_value[key]!r}")
                    continue
                if key not in outgoing_value:
                    differences.append(f"{next_path}: removed")
                    continue
                differences.extend(
                    self._diff_dicts(browser_value[key], outgoing_value[key], path=next_path)
                )
            return differences

        if isinstance(browser_value, list) and isinstance(outgoing_value, list):
            if len(browser_value) != len(outgoing_value):
                differences.append(
                    f"{path}: list_length {len(browser_value)} -> {len(outgoing_value)}"
                )
            for index, (browser_item, outgoing_item) in enumerate(
                zip(browser_value, outgoing_value, strict=False)
            ):
                next_path = f"{path}[{index}]"
                differences.extend(
                    self._diff_dicts(browser_item, outgoing_item, path=next_path)
                )
            return differences

        if browser_value != outgoing_value:
            differences.append(f"{path}: {browser_value!r} -> {outgoing_value!r}")
        return differences

    def _trim_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions:
            return payload

        first_action = actions[0]
        if not isinstance(first_action, dict):
            return payload

        trimmed = {
            "actions": [
                {
                    "id": first_action.get("id"),
                    "state": first_action.get("state"),
                    "error": first_action.get("error"),
                    "returnValueKeys": sorted((first_action.get("returnValue") or {}).keys())
                    if isinstance(first_action.get("returnValue"), dict)
                    else None,
                }
            ]
        }
        return trimmed

    def _resolve_strategy_order(self) -> list[BaseGraphQLStrategy]:
        strategies: list[BaseGraphQLStrategy] = []
        for key in self.settings.support_portal_fetch_strategy_order:
            normalized = key.strip().lower()
            strategy_cls = STRATEGY_REGISTRY.get(normalized)
            if strategy_cls is None:
                raise ValueError(f"Unknown SUPPORT_PORTAL_FETCH_STRATEGY_ORDER entry: {key}")
            strategies.append(strategy_cls())
        if not strategies:
            raise ValueError("No fetch strategies configured")
        if not self.settings.support_portal_enable_strategy_fallback:
            return [strategies[0]]
        return strategies

    def _instantiate_strategy(self, key: str) -> BaseGraphQLStrategy:
        strategy_cls = STRATEGY_REGISTRY[key]
        return strategy_cls()

    def _load_browser_where_clause(self) -> dict[str, Any] | None:
        raw_message = self.settings.support_portal_browser_parity_message
        file_message = self._load_browser_message_from_file()
        if file_message:
            raw_message = file_message
        if not raw_message:
            return None
        try:
            raw_json = json.loads(raw_message)
        except json.JSONDecodeError:
            return None

        query_input = (
            raw_json.get("actions", [{}])[0]
            .get("params", {})
            .get("queryInput")
        )
        if not isinstance(query_input, dict):
            return None
        variables = query_input.get("variables")
        if not isinstance(variables, dict):
            return None
        where_clause = variables.get("whereClause")
        return where_clause if isinstance(where_clause, dict) else None

    def _adapt_query_for_undefined_fields(
        self,
        *,
        request_spec: Any,
        undefined_fields: list[str],
        strategy_name: str,
    ) -> list[str]:
        if request_spec.adaptive_retries >= self._adaptive_retry_limit:
            self.logger.error(
                "Adaptive field stripping retry limit reached: strategy=%s retry_number=%s limit=%s",
                strategy_name,
                request_spec.adaptive_retries,
                self._adaptive_retry_limit,
            )
            return []

        removable_fields = [
            field_name
            for field_name in undefined_fields
            if field_name not in self._adaptive_required_fields
        ]
        if not removable_fields:
            self.logger.error(
                "Adaptive field stripping found no removable fields: strategy=%s undefined_fields=%s",
                strategy_name,
                undefined_fields,
            )
            return []

        removed_fields: list[str] = []
        updated_query = request_spec.query
        for field_name in removable_fields:
            candidate_query = remove_graphql_field(updated_query, field_name)
            if candidate_query != updated_query:
                updated_query = candidate_query
                removed_fields.append(field_name)

        if not removed_fields:
            self.logger.error(
                "Adaptive field stripping could not mutate query: strategy=%s undefined_fields=%s",
                strategy_name,
                undefined_fields,
            )
            return []

        request_spec.query = updated_query
        request_spec.adaptive_retries += 1
        request_spec.final_query_length = len(updated_query)
        for field_name in removed_fields:
            if field_name not in request_spec.stripped_fields:
                request_spec.stripped_fields.append(field_name)

        degraded_fields = [
            field_name
            for field_name in request_spec.stripped_fields
            if field_name in self._adaptive_degraded_fields
        ]
        request_spec.degraded_mode = bool(degraded_fields)
        request_spec.missing_fields = degraded_fields

        remaining_fields = list_graphql_field_names(updated_query)
        self.logger.warning(
            "Adaptive GraphQL field stripping applied: strategy=%s retry_number=%s removed_fields=%s resulting_query_length=%s fields_remaining=%s",
            strategy_name,
            request_spec.adaptive_retries,
            removed_fields,
            request_spec.final_query_length,
            len(remaining_fields),
        )
        if request_spec.degraded_mode:
            self.logger.warning(
                "Adaptive GraphQL degraded mode enabled: strategy=%s degraded_mode=true missing_fields=%s",
                strategy_name,
                request_spec.missing_fields,
            )
        return removed_fields

    def _join_error_messages(self, errors: list[Any]) -> str:
        return "; ".join(
            str(error.get("message") or error)
            for error in errors
            if isinstance(error, dict) or error
        )

    def _extract_undefined_fields(self, message: str) -> list[str]:
        undefined_fields: list[str] = []
        for path_match in re.findall(r"FieldUndefined@\[(.*?)\]", message):
            field_name = path_match.split("/")[-1].strip(" '\"")
            if field_name and field_name not in undefined_fields:
                undefined_fields.append(field_name)
        for field_match in re.findall(r"Field '([^']+)'", message):
            if field_match and field_match not in undefined_fields:
                undefined_fields.append(field_match)
        for field_match in re.findall(r"field name '([^']+)'", message, flags=re.IGNORECASE):
            if field_match and field_match not in undefined_fields:
                undefined_fields.append(field_match)
        return undefined_fields
