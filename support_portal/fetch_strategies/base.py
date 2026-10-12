from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from support_portal.graphql_payloads import DEFAULT_ORDER_BY


QUERIES_DIR = Path(__file__).resolve().parents[2] / "queries"


def load_query_text(filename: str) -> str:
    path = QUERIES_DIR / filename
    return path.read_text(encoding="utf-8").strip()


@dataclass(slots=True)
class StrategyRequestSpec:
    query: str
    variables: dict[str, Any]
    use_browser_payload: bool
    where_mode: str
    adaptive_retries: int = 0
    stripped_fields: list[str] = field(default_factory=list)
    degraded_mode: bool = False
    final_query_length: int = 0
    missing_fields: list[str] = field(default_factory=list)


class BaseGraphQLStrategy:
    key = ""
    name = ""
    query_filename = ""
    use_browser_payload = False

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
                where_clause=requested_where_clause,
                order_by=order_by,
                where_mode="configured",
            )
        ]

    def _make_spec(
        self,
        *,
        page_size: int,
        after: str | None,
        where_clause: dict[str, Any] | None,
        order_by: dict[str, Any] | None,
        where_mode: str,
    ) -> StrategyRequestSpec:
        variables = {
            "pageSize": page_size,
            "after": after,
            "orderBy": order_by or DEFAULT_ORDER_BY,
        }
        if where_clause is not None:
            variables["whereClause"] = where_clause
        return StrategyRequestSpec(
            query=self.query_text,
            variables=variables,
            use_browser_payload=self.use_browser_payload,
            where_mode=where_mode,
        )

    @property
    def query_text(self) -> str:
        return load_query_text(self.query_filename)
