from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.graphql_client import SupportPortalGraphQLClient
from support_portal.parser import parse_case_edges, parse_total_count
from utils.logging_config import configure_logging


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    print("[INFO] Starting ingestion mode: graphql")
    print("[INFO] Using SupportPortal Aura GraphQL client")
    print("[INFO] Browser automation disabled")
    settings.validate_graphql()
    client = SupportPortalGraphQLClient(settings=settings)

    results = client.fetch_all_case_pages(page_size=settings.page_size)
    if not results:
        print("HTTP status: n/a")
        print("totalCount: 0")
        print("parsed cases: 0")
        print("first 5 cases:")
        return 0

    result = results[0]
    cases = []
    for page in results:
        cases.extend(parse_case_edges(page.payload, base_case_url=settings.portal_case_base_url))
    total_count = parse_total_count(result.payload)
    summary = client.last_pagination_summary

    print(f"HTTP status: {result.status_code}")
    print(f"totalCount: {total_count}")
    print(f"parsed cases: {len(cases)}")
    if summary:
        print(f"pages_fetched: {summary.total_pages_fetched}")
        print(f"unique_cases: {summary.total_unique_cases}")
        print(f"duplicate_pages_detected: {summary.duplicate_pages_detected}")
        print(f"stop_reason: {summary.stop_reason}")
    print(f"fetch_strategy_used: {client.last_fetch_strategy_summary.fetch_strategy_used}")
    print(f"fallbacks_attempted: {client.last_fetch_strategy_summary.fallbacks_attempted}")
    print(f"undefined_fields_seen: {client.last_fetch_strategy_summary.undefined_fields_seen}")
    print(f"minimal_mode_used: {client.last_fetch_strategy_summary.minimal_mode_used}")
    print("first 5 cases:")
    for case in cases[:5]:
        print(f"- {case.get('case_number')} | {case.get('subject')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"GraphQL fetch failed: {exc}", file=sys.stderr)
        raise
