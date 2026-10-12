import logging
import json
import time

from config import CHECK_INTERVAL_SECONDS
from portal_scraper import scrape_portal


def monitor_cases(portal_url, username, password):
    """
    Monitor SupportPortal portal and extract cases.
    
    Args:
        portal_url: Portal login URL
        username: Portal username
        password: Portal password
    """
    logging.info("Starting case monitor")

    while True:
        try:
            print("--- Cycle Start ---")
            print("🔄 Checking for new cases...")
            logging.info("Starting new case monitoring cycle")

            cases = scrape_portal(portal_url, username, password, headless=False)

            if cases:
                logging.info(f"Found {len(cases)} cases")
                print(json.dumps(cases, indent=2))

                new_cases = [case for case in cases if case.get("is_new") is True]
                print(f"✅ {len(new_cases)} new cases found")
                logging.info(f"New cases found in cycle: {len(new_cases)}")
            else:
                print("⚠️ No valid response found")
                logging.warning("No cases found or extraction failed")

        except Exception as e:
            print(f"❌ Error occurred: {e}")
            logging.exception("Case monitoring cycle failed")

        print("--- Cycle End ---")
        time.sleep(CHECK_INTERVAL_SECONDS)
