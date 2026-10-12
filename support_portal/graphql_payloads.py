from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_PAGE_SIZE = 20
DEFAULT_WHERE_CLAUSE = {"Status": {"eq": "Awaiting Customer"}}
DEFAULT_ORDER_BY = {"CaseNumber": {"order": "DESC"}}
DEFAULT_QUERY_PATH = Path(__file__).resolve().parents[1] / "queries" / "case_full.graphql"
CASE_FIELDS = DEFAULT_QUERY_PATH.read_text(encoding="utf-8").strip()


def build_case_query_variables(
    page_size: int = DEFAULT_PAGE_SIZE,
    after: str | None = None,
    where_clause: dict[str, Any] | None = None,
    order_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "pageSize": page_size,
        "after": after,
        "orderBy": order_by or DEFAULT_ORDER_BY,
    }
    if where_clause is not None:
        variables["whereClause"] = where_clause
    return variables


def build_execute_graphql_message(
    *,
    action_id: str,
    query: str = CASE_FIELDS,
    variables: dict[str, Any] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    after: str | None = None,
    where_clause: dict[str, Any] | None = None,
    order_by: dict[str, Any] | None = None,
    page_uri: str | None = None,
) -> dict[str, Any]:
    request_variables = variables or build_case_query_variables(
        page_size=page_size,
        after=after,
        where_clause=where_clause,
        order_by=order_by,
    )
    params: dict[str, Any] = {
        "queryInput": {
            "query": query,
            "variables": request_variables,
        }
    }

    return {
        "actions": [
            {
                "id": action_id,
                "descriptor": "aura://RecordUiController/ACTION$executeGraphQL",
                "callingDescriptor": "UNKNOWN",
                "params": params,
            }
        ]
    }


def build_aura_form_payload(
    *,
    aura_context: str,
    aura_token: str,
    action_id: str,
    query: str = CASE_FIELDS,
    variables: dict[str, Any] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    after: str | None = None,
    where_clause: dict[str, Any] | None = None,
    order_by: dict[str, Any] | None = None,
    page_uri: str | None = None,
) -> dict[str, str]:
    message = build_execute_graphql_message(
        action_id=action_id,
        query=query,
        variables=variables,
        page_size=page_size,
        after=after,
        where_clause=where_clause,
        order_by=order_by,
        page_uri=page_uri,
    )
    return {
        "message": json.dumps(message, separators=(",", ":")),
        "aura.context": aura_context,
        "aura.pageURI": page_uri or "/s/case/Case/Default",
        "aura.token": aura_token,
    }
