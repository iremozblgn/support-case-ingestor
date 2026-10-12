from __future__ import annotations

from typing import Any


OPTIONAL_CASE_FIELDS = (
    "Account",
    "Product_Name__c",
    "Action_Required__c",
    "Contact",
    "Partners_Customer_Account__c",
)


def safe_get(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def safe_value(data: Any, *path: str) -> Any:
    target = safe_get(data, *path)
    if isinstance(target, dict):
        display_value = target.get("displayValue")
        if display_value not in (None, ""):
            return display_value
        return target.get("value")
    return target


def safe_bool_value(data: Any, *path: str) -> bool | None:
    value = safe_value(data, *path)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def parse_case_connection(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Response does not contain Aura actions")

    return_value = safe_get(actions[0], "returnValue", "data", "uiapi", "query", "Case")
    if not isinstance(return_value, dict):
        raise ValueError("Response does not contain uiapi.query.Case")
    return return_value


def parse_page_info(payload: dict[str, Any]) -> dict[str, Any]:
    case_conn = parse_case_connection(payload)
    page_info = case_conn.get("pageInfo")
    if not isinstance(page_info, dict):
        raise ValueError("Response does not contain pageInfo")
    return page_info


def parse_total_count(payload: dict[str, Any]) -> int:
    case_conn = parse_case_connection(payload)
    total_count = case_conn.get("totalCount")
    return int(total_count or 0)


def build_case_link(case_id: str | None, base_url: str) -> str | None:
    if not case_id:
        return None
    return f"{base_url.rstrip('/')}/{case_id}"


def normalize_case_node(node: dict[str, Any], base_case_url: str) -> dict[str, Any]:
    case_id = node.get("Id")
    missing_fields = _detect_missing_fields(node)
    account_name = (
        safe_value(node, "Account", "Name")
        or safe_value(node, "Partners_Customer_Account__r", "Name")
        or safe_value(node, "Partners_Customer_Account__c")
    )
    normalized = {
        "case_id": case_id,
        "case_number": safe_value(node, "CaseNumber"),
        "subject": safe_value(node, "Subject"),
        "description": safe_value(node, "Description"),
        "status": safe_value(node, "Status"),
        "priority": safe_value(node, "Priority"),
        "product_name": safe_value(node, "Product_Name__c"),
        "created_date": safe_value(node, "CreatedDate"),
        "closed_date": safe_value(node, "ClosedDate"),
        "created_by": safe_value(node, "CreatedBy", "Name"),
        "account": account_name,
        "contact": safe_value(node, "Contact", "Name"),
        "record_type": safe_value(node, "RecordType", "Name"),
        "owner_id": safe_value(node, "OwnerId"),
        "case_owner": safe_value(node, "Case_Owner__c"),
        "action_required": safe_bool_value(node, "Action_Required__c"),
        "is_partner_customer": safe_bool_value(node, "isPartnerCustomer__c"),
        "is_case_contact_user": safe_bool_value(node, "isCaseContactisUser__c"),
        "last_modified_date": (
            safe_value(node, "LastModifiedDate")
            or safe_value(node, "LastViewedDate")
            or safe_value(node, "LastReferencedDate")
        ),
        "case_url": build_case_link(case_id, base_case_url),
        "missing_fields": missing_fields,
        "metadata": {
            "missing_fields": missing_fields,
        },
        "raw": node,
    }
    return normalized


def _detect_missing_fields(node: dict[str, Any]) -> list[str]:
    missing_fields: list[str] = []
    if "Account" not in node:
        missing_fields.append("Account")
    if "Product_Name__c" not in node:
        missing_fields.append("Product_Name__c")
    if "Action_Required__c" not in node:
        missing_fields.append("Action_Required__c")
    if "Contact" not in node:
        missing_fields.append("Contact")
    if "Partners_Customer_Account__c" not in node and "Partners_Customer_Account__r" not in node:
        missing_fields.append("Partners_Customer_Account__c")
    return missing_fields


def parse_case_edges(payload: dict[str, Any], base_case_url: str) -> list[dict[str, Any]]:
    case_conn = parse_case_connection(payload)
    edges = case_conn.get("edges")
    if not isinstance(edges, list):
        raise ValueError("Response does not contain edges")

    cases: list[dict[str, Any]] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        normalized = normalize_case_node(node, base_case_url=base_case_url)
        if normalized.get("case_number") or normalized.get("case_id"):
            cases.append(normalized)
    return cases
