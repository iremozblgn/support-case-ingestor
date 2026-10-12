from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from support_portal.graphql_query_utils import (
    list_graphql_field_names,
    parse_graphql_document,
    remove_graphql_field,
)


REQUIRED_FIELDS = {
    "Id",
    "CaseNumber",
    "Subject",
    "Status",
    "Priority",
    "CreatedDate",
}


def load_browser_query() -> str:
    path = ROOT / "debug" / "browser_message.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["actions"][0]["params"]["queryInput"]["query"]


def main() -> int:
    query = load_browser_query()
    simulated_undefined_fields = [
        "Account",
        "Contact",
        "Product_Name__c",
        "Action_Required__c",
        "isPartnerCustomer__c",
    ]

    print(f"original_query_length: {len(query)}")
    print(f"original_field_count: {len(list_graphql_field_names(query))}")

    mutated_query = query
    for field_name in simulated_undefined_fields:
        mutated_query = remove_graphql_field(mutated_query, field_name)

    parse_graphql_document(mutated_query)
    remaining_fields = set(list_graphql_field_names(mutated_query))

    print(f"mutated_query_length: {len(mutated_query)}")
    print(f"mutated_field_count: {len(remaining_fields)}")
    print(f"removed_fields: {simulated_undefined_fields}")
    print(
        "required_fields_present: "
        f"{sorted(field for field in REQUIRED_FIELDS if field in remaining_fields)}"
    )
    print(
        "required_fields_missing: "
        f"{sorted(field for field in REQUIRED_FIELDS if field not in remaining_fields)}"
    )
    print(
        "undefined_fields_still_present: "
        f"{sorted(field for field in simulated_undefined_fields if field in remaining_fields)}"
    )

    if any(field not in remaining_fields for field in REQUIRED_FIELDS):
        raise SystemExit("Required fields were removed unexpectedly")
    if any(field in remaining_fields for field in simulated_undefined_fields):
        raise SystemExit("Undefined fields were not removed correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
