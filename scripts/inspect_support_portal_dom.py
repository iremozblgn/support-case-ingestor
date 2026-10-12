from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_scraper import (
    _detect_login,
    _manual_login_if_needed,
    _scroll_page_for_virtualized_rows,
    _wait_for_cases_ready,
    inspect_support_portal_dom,
)


def main() -> int:
    settings = get_settings()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"playwright_import_error: {exc}")
        return 1

    snapshot_html = pathlib.Path("debug") / "dom_snapshot.html"
    snapshot_png = pathlib.Path("debug") / "dom_snapshot.png"
    snapshot_html.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            settings.support_portal_browser_profile_dir,
            headless=settings.support_portal_browser_headless,
            args=["--start-maximized", "--no-sandbox"],
            viewport=None,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_navigation_timeout(settings.request_timeout_seconds * 1000)
            page.goto(settings.cases_url, wait_until="domcontentloaded", timeout=settings.request_timeout_seconds * 1000)
            _wait_for_cases_ready(page, timeout_seconds=90)
            if _detect_login(page) and not settings.support_portal_browser_headless:
                _manual_login_if_needed(page, cases_url=settings.cases_url)
                _wait_for_cases_ready(page, timeout_seconds=90)
            _scroll_page_for_virtualized_rows(page)
            diagnostics = inspect_support_portal_dom(page)
            snapshot_html.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(snapshot_png), full_page=True)

            print(f"current_url: {page.url}")
            print(f"page_title: {page.title()}")
            print(f"login_detected: {_detect_login(page)}")
            print(f"dom_snapshot_html: {snapshot_html}")
            print(f"dom_snapshot_png: {snapshot_png}")
            print("selector_counts:")
            print(json.dumps(diagnostics.get("selector_counts", {}), indent=2, ensure_ascii=False))
            print("role_structures:")
            print(
                json.dumps(
                    {
                        "table_count": diagnostics.get("table_count"),
                        "grid_count": diagnostics.get("grid_count"),
                        "row_count": diagnostics.get("row_count"),
                        "tbody_tr_count": diagnostics.get("tbody_tr_count"),
                        "div_role_row_count": diagnostics.get("div_role_row_count"),
                        "lightning_datatable_count": diagnostics.get("lightning_datatable_count"),
                        "article_count": diagnostics.get("article_count"),
                        "records_lwc_detail_panel_count": diagnostics.get("records_lwc_detail_panel_count"),
                        "forcegenerated_flexipage_count": diagnostics.get("forcegenerated_flexipage_count"),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            print("first_20_visible_texts:")
            for text in diagnostics.get("first_20_visible_texts", []):
                print(f"- {text}")
            print("first_20_links:")
            for link in diagnostics.get("case_related_anchors", [])[:20]:
                print(json.dumps(link, ensure_ascii=False))
            print("visible_case_numbers:")
            print(json.dumps(diagnostics.get("visible_case_numbers", []), ensure_ascii=False))
            print("top_candidate_container_class_names:")
            print(json.dumps(diagnostics.get("top_candidate_container_class_names", []), ensure_ascii=False))
            print("selector_samples:")
            print(json.dumps(diagnostics.get("selector_samples", {}), indent=2, ensure_ascii=False))
            return 0
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
