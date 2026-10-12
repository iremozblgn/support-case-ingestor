from __future__ import annotations

from typing import Any

from support_portal.fetch_strategies.base import BaseGraphQLStrategy, StrategyRequestSpec


class GraphQLMinimalStrategy(BaseGraphQLStrategy):
    key = "minimal"
    name = "GraphQLMinimalStrategy"
    query_filename = "case_minimal.graphql"

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
        return [
            self._make_spec(
                page_size=page_size,
                after=after,
                where_clause={"IsClosed": {"eq": False}},
                order_by=order_by,
                where_mode="open_cases",
            ),
            self._make_spec(
                page_size=page_size,
                after=after,
                where_clause=None,
                order_by=order_by,
                where_mode="all_cases",
            ),
        ]
