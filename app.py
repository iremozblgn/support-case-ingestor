from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from config import get_settings
from services.monitor import RunExitCategory
from services.healthcheck import HealthcheckServer
from services.monitor import SupportPortalMonitorService
from storage.database import init_database
from utils import configure_logging


def main() -> int:
    try:
        settings = get_settings()
    except ValueError as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("configuration_error=true detail=%s", exc)
        return 16
    configure_logging(settings.log_level)
    logging.info("Starting ingestion mode: %s", settings.ingest_mode)
    logging.info("Starting app with config: %s", settings.masked_config())

    if settings.ingest_mode == "graphql":
        log_graphql_startup()
        validate_graphql_startup(settings)
        init_database(settings.database_url)
        service = SupportPortalMonitorService(settings)
        healthcheck = maybe_start_healthcheck(settings, service)
        try:
            run_result = service.run()
        finally:
            if healthcheck is not None:
                healthcheck.stop()
        return _graphql_exit_code(settings, run_result)

    if settings.ingest_mode == "portal":
        logging.info("Using portal ingestion mode")
        logging.info("Browser automation enabled")
        process_portal(settings)
        return 0

    if settings.ingest_mode == "imap":
        logging.info("Using IMAP ingestion mode")
        process_emails(settings)
        return 0

    if settings.ingest_mode == "file":
        logging.info("Using file ingestion mode")
        process_files(settings)
        return 0

    raise SystemExit(f"Unknown INGEST_MODE: {settings.ingest_mode}")


def log_graphql_startup() -> None:
    logging.info("Using SupportPortal Aura GraphQL client")
    logging.info("browser_automation_enabled=true")
    logging.info("browser_fallback_enabled=true")


def _graphql_exit_code(settings: Any, run_result: dict[str, Any] | None) -> int:
    if not settings.run_once:
        return 0
    if not run_result:
        logging.error("run_once_expected_result_missing=true")
        return 17

    exit_category = str(run_result.get("exit_category") or "")
    exit_code_map = {
        RunExitCategory.SUCCESS: 0,
        RunExitCategory.SUCCESS_NO_NEW_CASES: 0,
        RunExitCategory.OPERATOR_ACTION_REQUIRED: 11,
        RunExitCategory.AUTHENTICATION_FAILURE: 12,
        RunExitCategory.PROFILE_BUSY: 13,
        RunExitCategory.EXTRACTION_FAILURE: 14,
        RunExitCategory.NOTIFICATION_FAILURE: 15,
        RunExitCategory.CONFIGURATION_ERROR: 10,
        RunExitCategory.UPSTREAM_RUNTIME_FAILURE: 16,
    }
    exit_code = exit_code_map.get(exit_category, 16)
    if exit_code != 0:
        logging.error(
            "run_once_failed=true run_exit_reason=%s exit_category=%s exit_code=%s",
            run_result.get("run_exit_reason"),
            exit_category,
            exit_code,
        )
    else:
        logging.info(
            "run_once_completed=true run_exit_reason=%s exit_category=%s exit_code=%s",
            run_result.get("run_exit_reason"),
            exit_category,
            exit_code,
        )
    return exit_code


def maybe_start_healthcheck(settings, service: SupportPortalMonitorService) -> HealthcheckServer | None:
    if not settings.healthcheck_enabled:
        return None
    server = HealthcheckServer(
        host=settings.healthcheck_host,
        port=settings.healthcheck_port,
        runtime_state=service.runtime_state,
    )
    server.start()
    return server


def validate_graphql_startup(settings) -> None:
    missing = []
    if not settings.support_portal_graphql_url:
        missing.append("SUPPORT_PORTAL_GRAPHQL_URL")
    if not settings.support_portal_cookie:
        missing.append("SUPPORT_PORTAL_COOKIE")
    if not settings.support_portal_aura_context:
        missing.append("SUPPORT_PORTAL_AURA_CONTEXT")
    if not settings.support_portal_aura_token:
        missing.append("SUPPORT_PORTAL_AURA_TOKEN")

    if not missing:
        return

    for variable_name in missing:
        logging.error("%s missing", variable_name)
    logging.error("GraphQL mode cannot start")
    raise SystemExit(16)


def process_portal(settings) -> None:
    from case_monitor import monitor_cases

    if not all([settings.portal_url, settings.portal_username, settings.portal_password]):
        logging.error("Portal configuration incomplete.")
        return
    monitor_cases(settings.portal_url, settings.portal_username, settings.portal_password)


def process_files(settings) -> None:
    from parser import parse_support_portal_text
    from sender import send_payload
    from storage import init_db, is_duplicate, save_event
    from utils import generate_event_hash

    init_db(settings.sqlite_path)
    input_dir = Path(settings.input_dir)
    if not input_dir.exists():
        logging.error("Input directory %s does not exist.", settings.input_dir)
        return

    txt_files = list(input_dir.glob("*.txt"))
    if not txt_files:
        logging.info("No .txt files found in input directory.")
        return

    for txt_file in txt_files:
        try:
            raw_text = txt_file.read_text(encoding="utf-8")
            payload = parse_support_portal_text(raw_text, str(txt_file))
            event_hash = generate_event_hash(payload)

            if is_duplicate(event_hash, settings.sqlite_path):
                logging.info("Skipping duplicate event from %s", txt_file)
                continue

            print(json.dumps(payload, indent=2))

            if settings.send_enabled:
                send_payload(payload, settings.portal_api_url, settings.portal_api_token)

            save_event(event_hash, payload.get("case_number"), str(txt_file), settings.sqlite_path)
        except Exception:
            logging.exception("Error processing %s", txt_file)


def process_emails(settings) -> None:
    from mail_forwarder import forward_email
    from mail_reader import read_emails
    from parser import parse_support_portal_text
    from storage import init_db, is_duplicate, save_event

    init_db(settings.sqlite_path)

    if not all(
        [
            settings.imap_host,
            settings.imap_username,
            settings.imap_password,
            settings.smtp_host,
            settings.smtp_username,
            settings.smtp_password,
        ]
    ):
        logging.error("IMAP and SMTP configuration incomplete.")
        return

    emails = read_emails(
        settings.imap_host,
        settings.imap_port,
        settings.imap_username,
        settings.imap_password,
        settings.imap_mailbox,
        settings.imap_unseen_only,
    )
    keywords = [k.strip().lower() for k in settings.support_portal_subject_keywords.split(",")]

    for email_data in emails:
        try:
            sender = email_data["sender"].lower()
            subject = email_data["subject"].lower()
            if settings.support_portal_sender_filter.lower() not in sender:
                continue
            if not any(keyword in subject for keyword in keywords):
                continue

            message_id = email_data.get("message_id")
            dedup_hash = hashlib.sha256(
                f"{sender}{subject}{email_data['body_text']}".encode("utf-8")
            ).hexdigest()
            if message_id and is_duplicate("", settings.sqlite_path, message_id):
                logging.info("Skipping duplicate email %s", message_id)
                continue
            if is_duplicate(dedup_hash, settings.sqlite_path):
                logging.info("Skipping duplicate email hash %s", dedup_hash)
                continue

            payload = parse_support_portal_text(
                email_data["body_text"],
                f"email:{message_id or dedup_hash}",
            )
            print(json.dumps(payload, indent=2))

            if settings.send_enabled:
                forward_email(
                    settings.smtp_host,
                    settings.smtp_port,
                    settings.smtp_username,
                    settings.smtp_password,
                    settings.smtp_use_tls,
                    settings.forward_to_email,
                    settings.forward_from_email,
                    email_data["subject"],
                    email_data["sender"],
                    email_data["body_text"],
                    payload,
                )

            save_event(
                dedup_hash,
                payload.get("case_number"),
                f"email:{message_id or dedup_hash}",
                settings.sqlite_path,
                message_id,
            )
        except Exception:
            logging.exception("Error processing email")


if __name__ == "__main__":
    raise SystemExit(main())
