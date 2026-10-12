from __future__ import annotations

from typing import Any

from support_portal.fetch_strategies.base import BaseGraphQLStrategy, StrategyRequestSpec


class GraphQLSafeStrategy(BaseGraphQLStrategy):
    key = "safe"
    name = "GraphQLSafeStrategy"
    query_filename = "case_safe.graphql"

    def build_request_specs(
        self,
        *,
        settings: Any,
        page_size: int,
        after: str | None,
        requested_where_clause: dict[str, Any] | None,
        order_by: dict[str, Any] | None,
        browser_where_clause: dict[str, Any] | None,
    ) -> list[StrategyRequestSpec]:
        mode = settings.support_portal_safe_where_mode
        where_clause = self._resolve_where_clause(
            settings=settings,
            mode=mode,
            requested_where_clause=requested_where_clause,
            browser_where_clause=browser_where_clause,
        )
        return [
            self._make_spec(
                page_size=page_size,
                after=after,
                where_clause=where_clause,
                order_by=order_by,
                where_mode=mode,
            )
        ]

    def _resolve_where_clause(
        self,
        *,
        settings: Any,
        mode: str,
        requested_where_clause: dict[str, Any] | None,
        browser_where_clause: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if mode == "browser":
            return browser_where_clause or requested_where_clause
        if mode == "open_cases":
            return {"IsClosed": {"eq": False}}
        if mode == "all_cases":
            return None
        if mode == "status":
            return {"Status": {"eq": settings.case_status_filter}}
        raise ValueError(f"Unsupported SUPPORT_PORTAL_SAFE_WHERE_MODE: {mode}")
