from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.graphql_client import SupportPortalGraphQLClient, SupportPortalZeroResultsError
from support_portal.parser import parse_case_edges
from utils.logging_config import configure_logging


def notifications_would_still_work(case: dict[str, object]) -> bool:
    return bool(case.get("case_id") and case.get("case_number") and case.get("subject"))


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate_graphql()

    client = SupportPortalGraphQLClient(settings=settings)
    pages = []
    zero_results_error: SupportPortalZeroResultsError | None = None
    try:
        pages = client.fetch_all_case_pages(
            page_size=settings.page_size,
            where_clause=settings.case_where_clause,
            order_by=settings.case_order_by,
        )
    except SupportPortalZeroResultsError as exc:
        zero_results_error = exc

    cases: list[dict[str, object]] = []
    for page in pages:
        cases.extend(parse_case_edges(page.payload, base_case_url=settings.portal_case_base_url))

    print("attempted_fetches:")
    for attempt in client.last_fetch_strategy_summary.attempted_fetches:
        print(
            f"- strategy={attempt['strategy']} where_mode={attempt['where_mode']} "
            f"totalCount={attempt['totalCount']} parsed_cases={attempt['parsed_cases']} "
            f"result={attempt['result']} adaptive_retries={attempt['adaptive_retries']} "
            f"stripped_fields={attempt['stripped_fields']}"
        )
    print(f"final_strategy: {client.last_fetch_strategy_summary.fetch_strategy_used}")
    print(f"fallbacks_attempted: {client.last_fetch_strategy_summary.fallbacks_attempted}")
    print(f"undefined_fields_seen: {client.last_fetch_strategy_summary.undefined_fields_seen}")
    print(f"minimal_mode_used: {client.last_fetch_strategy_summary.minimal_mode_used}")
    print(f"adaptive_retries: {client.last_fetch_strategy_summary.adaptive_retries}")
    print(f"stripped_fields: {client.last_fetch_strategy_summary.stripped_fields}")
    print(f"degraded_mode: {client.last_fetch_strategy_summary.degraded_mode}")
    print(f"final_query_length: {client.last_fetch_strategy_summary.final_query_length}")
    print("first 5 cases:")
    for case in cases[:5]:
        print(
            f"- {case.get('case_number')} | {case.get('subject')} | "
            f"missing_fields={case.get('missing_fields')}"
        )

    combined_missing_fields = sorted(
        {
            field
            for case in cases[:5]
            for field in (case.get("missing_fields") or [])
        }
    )
    print(f"missing_fields_seen_in_first_5: {combined_missing_fields}")
    print(
        "notifications_would_still_work: "
        f"{any(notifications_would_still_work(case) for case in cases)}"
    )
    if zero_results_error is not None:
        print(f"zero_results_error: {zero_results_error}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fetch strategy test failed: {exc}", file=sys.stderr)
        raise
