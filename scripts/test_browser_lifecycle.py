from __future__ import annotations

import json
import pathlib
import sys
import tempfile
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from support_portal.browser_manager import (
    SupportPortalBrowserManagerError,
    close_context,
    get_browser_entrypoint_diagnostics,
    get_context_health,
    get_or_create_context,
    is_context_alive,
)


def main() -> int:
    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="support_portal-browser-lifecycle-") as tmp_dir:
        lifecycle_settings = replace(
            settings,
            support_portal_browser_profile_dir=tmp_dir,
            support_portal_browser_headless=settings.support_portal_browser_headless,
            support_portal_browser_idle_timeout_seconds=600,
        )

        try:
            print("production_profile_reference:")
            print(json.dumps(get_browser_entrypoint_diagnostics(settings), ensure_ascii=False))
            context1, health1 = get_or_create_context(lifecycle_settings)
            page1 = context1.pages[0] if context1.pages else context1.new_page()
            print(f"browser_context_created: {health1.get('browser_context_created')}")
            print(f"browser_context_reused: {health1.get('browser_context_reused')}")
            print(f"browser_pid: {health1.get('browser_pid')}")
            print(f"browser_context_alive: {health1.get('browser_context_alive')}")
            print(f"active_pages_count: {health1.get('active_pages_count')}")
            print(f"eval_result_1: {page1.evaluate('1+1')}")

            context2, health2 = get_or_create_context(lifecycle_settings)
            page2 = context2.pages[0] if context2.pages else context2.new_page()
            print(f"same_context_reused: {context1 is context2}")
            print(f"browser_context_reused_second_call: {health2.get('browser_context_reused')}")
            print(f"eval_result_2: {page2.evaluate('2+2')}")
            print(f"context_alive_second_call: {is_context_alive()}")
            print("health_after_reuse:")
            print(json.dumps(get_context_health(), ensure_ascii=False))

            close_context(reason="lifecycle_test_mid_close")
            print(f"context_alive_after_close: {is_context_alive()}")

            context3, health3 = get_or_create_context(lifecycle_settings)
            page3 = context3.pages[0] if context3.pages else context3.new_page()
            print(f"browser_context_created_after_relaunch: {health3.get('browser_context_created')}")
            print(f"browser_context_relaunch_required: {health3.get('browser_context_relaunch_required')}")
            print(f"eval_result_3: {page3.evaluate('3+3')}")
            print("health_after_relaunch:")
            print(json.dumps(get_context_health(), ensure_ascii=False))
            return 0
        except SupportPortalBrowserManagerError as exc:
            print(f"browser_lifecycle_error: {exc}")
            return 1
        finally:
            close_context(reason="lifecycle_test_final_close")


if __name__ == "__main__":
    raise SystemExit(main())
