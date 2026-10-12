from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.fetch_strategies import STRATEGY_REGISTRY
from support_portal.graphql_client import SupportPortalGraphQLClient


def main() -> int:
    settings = get_settings()
    client = SupportPortalGraphQLClient(settings=settings)
    strategy = STRATEGY_REGISTRY["full"]()
    request_spec = strategy.build_request_specs(
        settings=settings,
        page_size=settings.page_size,
        after=None,
        requested_where_clause=settings.case_where_clause,
        order_by=settings.case_order_by,
        browser_where_clause=client._load_browser_where_clause(),
    )[0]
    payload = client._build_request_payload(
        aura_context=settings.support_portal_aura_context,
        aura_token=settings.support_portal_aura_token,
        action_id="1;a",
        page_size=settings.page_size,
        explicit_page_size=False,
        after=None,
        where_clause=settings.case_where_clause,
        order_by=settings.case_order_by,
        page_uri=settings.support_portal_page_uri,
        strategy=strategy,
        request_spec=request_spec,
    )
    parity_request = client.browser_parity_builder.build_request(
        generated_url=settings.support_portal_graphql_url,
        session_headers=dict(client.session.headers),
        form_payload=payload,
    )
    diff = parity_request.diff
    if diff is None:
        print("parity_score: 0")
        print("missing_headers: []")
        print("mutated_values: {}")
        print("request_body_differences: {}")
        print("snapshot_loaded: false")
        return 0

    print(f"parity_score: {diff.parity_score}")
    print(f"missing_headers: {json.dumps(diff.missing_headers, ensure_ascii=False)}")
    print(
        "mutated_values: "
        + json.dumps(
            {
                "changed_headers": diff.changed_headers,
                "changed_query_params": diff.changed_query_params,
                "changed_cookies": diff.changed_cookies,
                "changed_graphql_variables": diff.changed_graphql_variables,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(
        "request_body_differences: "
        + json.dumps(
            {
                "body_length_change": diff.body_length_change,
                "changed_body_fields": diff.changed_body_fields,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if settings.browser_parity_strict and diff.changed_graphql_variables.get("whereClause"):
        print("STRICT PARITY VIOLATION: whereClause mutated")
    changed_headers_lower = {name.lower(): value for name, value in diff.changed_headers.items()}
    if settings.browser_parity_strict and "referer" in changed_headers_lower:
        print("STRICT PARITY VIOLATION: referer mutated")
    if settings.browser_parity_strict and "aura.pageURI" in diff.changed_body_fields:
        print("STRICT PARITY VIOLATION: aura.pageURI mutated")
    print(f"strict_parity_enabled: {settings.browser_parity_strict}")
    print(f"snapshot_variables_preserved: {not bool(diff.changed_graphql_variables)}")
    print(f"snapshot_referer_preserved: {'referer' not in changed_headers_lower}")
    print(f"snapshot_pageuri_preserved: {'aura.pageURI' not in diff.changed_body_fields}")
    print(f"outgoing_url: {parity_request.url}")
    print(f"outgoing_body_length: {len(parity_request.encoded_body)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
