from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_manager import close_context, get_or_create_context
from support_portal.browser_scraper import (
    EMPTY_VIEW_HTML_PATH,
    EMPTY_VIEW_SCREENSHOT_PATH,
    _acceptable_case_view_labels,
    _collect_list_view_runtime_snapshot,
    _extract_case_view_diagnostics,
    _matches_case_view_label,
    _open_list_view_picker,
    _select_target_view_from_open_picker,
    _try_refresh_cases_view,
    ensure_support_portal_authenticated,
)
from support_portal.debug_artifacts import capture_page_artifacts


BEFORE_SCREENSHOT = pathlib.Path("debug") / "list_view_picker_before.png"
BEFORE_HTML = pathlib.Path("debug") / "list_view_picker_before.html"
OPEN_SCREENSHOT = pathlib.Path("debug") / "list_view_picker_open.png"
OPEN_HTML = pathlib.Path("debug") / "list_view_picker_open.html"
AFTER_SCREENSHOT = pathlib.Path("debug") / "list_view_after_selection.png"
AFTER_HTML = pathlib.Path("debug") / "list_view_after_selection.html"


def _print_snapshot(title: str, snapshot: dict[str, object]) -> None:
    print(title)
    print(f"current_selected_view_text: {snapshot.get('current_selected_view_text')}")
    print(f"available_list_views: {json.dumps(snapshot.get('available_list_views', []), ensure_ascii=False)}")
    print(f"visible_buttons: {json.dumps(snapshot.get('visible_buttons', []), ensure_ascii=False)}")
    print(f"visible_menuitems: {json.dumps(snapshot.get('visible_menuitems', []), ensure_ascii=False)}")
    print(f"visible_combobox_options: {json.dumps(snapshot.get('visible_combobox_options', []), ensure_ascii=False)}")
    print(f"aria_list_labels: {json.dumps(snapshot.get('aria_list_labels', []), ensure_ascii=False)}")
    print(f"cases_text_nodes: {json.dumps(snapshot.get('cases_text_nodes', []), ensure_ascii=False)}")


def main() -> int:
    settings = get_settings()
    acceptable_labels = _acceptable_case_view_labels(settings)
    context, _ = get_or_create_context(settings)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.set_default_navigation_timeout(settings.support_portal_page_load_timeout_seconds * 1000)
        page.set_default_timeout(settings.support_portal_case_extraction_timeout_seconds * 1000)
        ensure_support_portal_authenticated(page, settings, __import__("logging").getLogger(__name__))
        cases_url = settings.cases_url or f"{(settings.portal_url or 'https://support.example.com/s').rstrip('/')}/case/Case/Default?tabset-836f8=1"
        page.goto(cases_url, wait_until="domcontentloaded", timeout=settings.support_portal_page_load_timeout_seconds * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        before_diag = _collect_list_view_runtime_snapshot(page, settings)
        before_diag.update(_extract_case_view_diagnostics(page, settings))
        capture_page_artifacts(page, screenshot_path=BEFORE_SCREENSHOT, html_path=BEFORE_HTML, failure=True)
        _print_snapshot("before", before_diag)

        opened, open_method = _open_list_view_picker(page, settings, __import__("logging").getLogger(__name__))
        print(f"list_picker_opened: {opened}")
        print(f"list_picker_open_method: {open_method}")
        page.wait_for_timeout(1500)
        open_diag = _collect_list_view_runtime_snapshot(page, settings)
        open_diag.update(_extract_case_view_diagnostics(page, settings))
        capture_page_artifacts(page, screenshot_path=OPEN_SCREENSHOT, html_path=OPEN_HTML, failure=True)
        _print_snapshot("open", open_diag)

        clicked, click_method = _select_target_view_from_open_picker(page, settings, __import__("logging").getLogger(__name__)) if opened else (False, None)
        print(f"target_view_clicked: {clicked}")
        print(f"target_view_click_method: {click_method}")
        if clicked:
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            _try_refresh_cases_view(page)
            deadline_ms = 30000
            page.wait_for_timeout(min(deadline_ms, 3000))

        after_diag = _collect_list_view_runtime_snapshot(page, settings)
        after_diag.update(_extract_case_view_diagnostics(page, settings))
        capture_page_artifacts(page, screenshot_path=AFTER_SCREENSHOT, html_path=AFTER_HTML, failure=True)
        _print_snapshot("after", after_diag)
        print(f"selected_view_before: {before_diag.get('selected_view_before')}")
        print(f"selected_view_after: {after_diag.get('selected_view_after')}")
        print(f"case_numbers_after_selection: {json.dumps(after_diag.get('case_numbers_after_selection', []), ensure_ascii=False)}")
        print(f"no_items_after_selection: {after_diag.get('no_items_after_selection')}")
        print(
            "target_view_not_selected: "
            + str(
                not after_diag.get("selected_view_after")
                or not _matches_case_view_label(str(after_diag.get("selected_view_after")), acceptable_labels)
            )
        )
        print(f"artifacts_before: {BEFORE_SCREENSHOT} {BEFORE_HTML}")
        print(f"artifacts_open: {OPEN_SCREENSHOT} {OPEN_HTML}")
        print(f"artifacts_after: {AFTER_SCREENSHOT} {AFTER_HTML}")
        print(f"empty_view_artifacts_reference: {EMPTY_VIEW_SCREENSHOT_PATH} {EMPTY_VIEW_HTML_PATH}")
        return 0
    finally:
        close_context(reason="debug_support_portal_list_views")


if __name__ == "__main__":
    raise SystemExit(main())
