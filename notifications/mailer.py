from __future__ import annotations

import logging
import random
import smtplib
import socket
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from html import escape
from typing import Any

from storage.models import CaseRecord
from storage.repository import (
    SupportPortalRepository,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENT,
    utcnow,
)


@dataclass(slots=True)
class NotificationAttemptResult:
    final_status: str
    sent: bool
    skipped: bool
    retry_scheduled: bool
    error_message: str | None = None


class SupportPortalMailer:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)

    def send_new_case_email(
        self,
        case: dict[str, Any],
        repo: SupportPortalRepository,
        *,
        record: CaseRecord,
    ) -> NotificationAttemptResult:
        email = self.settings.to_email
        if record.notification_status == NOTIFICATION_STATUS_SENT:
            repo.mark_notification_skipped_existing(record)
            repo.log_notification(
                case_id=case.get("case_id"),
                case_number=case.get("case_number"),
                email=email,
                status=NOTIFICATION_STATUS_SENT,
                attempt_key=record.notification_attempt_key,
                error_message="Notification already sent",
            )
            return NotificationAttemptResult(
                final_status=NOTIFICATION_STATUS_SENT,
                sent=False,
                skipped=True,
                retry_scheduled=False,
            )

        repo.mark_notification_pending(record)

        if not self.settings.send_enabled:
            self.logger.info("Email sending disabled, leaving case %s as pending", case.get("case_number"))
            repo.log_notification(
                case_id=case.get("case_id"),
                case_number=case.get("case_number"),
                email=email,
                status=NOTIFICATION_STATUS_PENDING,
                attempt_key=record.notification_attempt_key,
                error_message="SEND_ENABLED is false",
            )
            return NotificationAttemptResult(
                final_status=record.notification_status or NOTIFICATION_STATUS_PENDING,
                sent=False,
                skipped=True,
                retry_scheduled=True,
                error_message="SEND_ENABLED is false",
            )

        if repo.has_sent_notification(
            case_id=case.get("case_id"),
            case_number=case.get("case_number"),
            email=email,
            attempt_key=record.notification_attempt_key,
        ):
            repo.mark_notification_sent(record)
            repo.log_notification(
                case_id=case.get("case_id"),
                case_number=case.get("case_number"),
                email=email,
                status=NOTIFICATION_STATUS_SENT,
                attempt_key=record.notification_attempt_key,
                error_message="Notification already sent",
            )
            return NotificationAttemptResult(
                final_status=NOTIFICATION_STATUS_SENT,
                sent=False,
                skipped=True,
                retry_scheduled=False,
            )

        repo.begin_notification_attempt(record)
        repo.session.flush()
        config_error = self._validate_mail_configuration(email=email)
        if config_error is not None:
            return self._handle_send_failure(
                repo=repo,
                record=record,
                case=case,
                email=email,
                error_message=config_error,
            )

        message_id = repo.build_message_id(record, email)
        message = self._build_message(case, email=email, message_id=message_id)

        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.mail_send_timeout_seconds,
            ) as server:
                if self.settings.smtp_use_tls:
                    server.starttls()
                if self.settings.smtp_username and self.settings.smtp_password:
                    server.login(self.settings.smtp_username, self.settings.smtp_password)
                server.send_message(message)
        except Exception as exc:
            self.logger.warning(
                "Failed to send email for case %s: %s",
                case.get("case_number"),
                self._clean_error_message(exc),
            )
            return self._handle_send_failure(
                repo=repo,
                record=record,
                case=case,
                email=email,
                exception=exc,
            )

        self.logger.info("Email sent for case %s to %s", case.get("case_number"), email)
        repo.mark_notification_sent(record)
        repo.log_notification(
            case_id=case.get("case_id"),
            case_number=case.get("case_number"),
            email=email,
            attempt_key=record.notification_attempt_key,
            message_id=message_id,
            status=NOTIFICATION_STATUS_SENT,
            error_message=None,
        )
        repo.session.flush()
        return NotificationAttemptResult(
            final_status=NOTIFICATION_STATUS_SENT,
            sent=True,
            skipped=False,
            retry_scheduled=False,
        )

    def send_test_cycle_email(
        self,
        *,
        sample_case: dict[str, Any],
        repo: SupportPortalRepository,
        summary: dict[str, Any],
    ) -> bool:
        email = self.settings.to_email
        if not self.settings.send_enabled:
            self.logger.info("Email sending disabled, skipping test email")
            repo.log_notification(
                case_id=sample_case.get("case_id"),
                case_number=sample_case.get("case_number"),
                email=email,
                status="SKIPPED",
                error_message="SEND_ENABLED is false",
            )
            return False

        config_error = self._validate_mail_configuration(email=email)
        if config_error is not None:
            repo.log_notification(
                case_id=sample_case.get("case_id"),
                case_number=sample_case.get("case_number"),
                email=email,
                status=NOTIFICATION_STATUS_FAILED,
                error_message=config_error,
            )
            return False

        message = EmailMessage()
        message["From"] = self.settings.smtp_username or email
        message["To"] = email
        message["Subject"] = (
            f"[SUPPORT_PORTAL][TEST] {sample_case.get('case_number') or 'UNKNOWN'} - "
            f"{sample_case.get('subject') or 'No Subject'}"
        )
        message.set_content(self._build_test_body(sample_case, summary))
        message.add_alternative(self._build_test_html_body(sample_case, summary), subtype="html")

        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.mail_send_timeout_seconds,
            ) as server:
                if self.settings.smtp_use_tls:
                    server.starttls()
                if self.settings.smtp_username and self.settings.smtp_password:
                    server.login(self.settings.smtp_username, self.settings.smtp_passwofrƒd)
                server.send_message(message)
        except Exception as exc:
            self.logger.warning(
                "Failed to send test email for case %s: %s",
                sample_case.get("case_number"),
                self._clean_error_message(exc),
            )
            repo.log_notification(
                case_id=sample_case.get("case_id"),
                case_number=sample_case.get("case_number"),
                email=email,
                status=NOTIFICATION_STATUS_FAILED,
                error_message=self._clean_error_message(exc),
            )
            return False

        self.logger.info("Test email sent for cycle using case %s to %s", sample_case.get("case_number"), email)
        repo.log_notification(
            case_id=sample_case.get("case_id"),
            case_number=sample_case.get("case_number"),
            email=email,
            status=NOTIFICATION_STATUS_SENT,
            error_message=None,
        )
        return True

    def _validate_mail_configuration(self, *, email: str | None) -> str | None:
        if not email:
            return "TO_EMAIL is not configured"
        if not self.settings.smtp_host:
            return "SMTP_HOST is not configured"
        return None

    def _build_message(self, case: dict[str, Any], *, email: str, message_id: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.settings.smtp_username or email
        message["To"] = email
        message["Message-ID"] = message_id
        message["Subject"] = (
            f"[SUPPORT_PORTAL][NEW CASE] {case.get('case_number') or 'UNKNOWN'} - "
            f"{case.get('subject') or 'No Subject'}"
        )
        message.set_content(self._build_body(case))
        message.add_alternative(self._build_html_body(case), subtype="html")
        return message

    def _handle_send_failure(
        self,
        *,
        repo: SupportPortalRepository,
        record: CaseRecord,
        case: dict[str, Any],
        email: str | None,
        exception: Exception | None = None,
        error_message: str | None = None,
    ) -> NotificationAttemptResult:
        cleaned_error = error_message or self._clean_error_message(exception)
        next_retry_at = self._compute_next_retry_at(
            attempt_count=int(record.notification_attempt_count or 0),
            exception=exception,
        )
        repo.mark_notification_failed(
            record,
            error_message=cleaned_error,
            next_retry_at=next_retry_at,
        )
        repo.log_notification(
            case_id=case.get("case_id"),
            case_number=case.get("case_number"),
            email=email,
            attempt_key=record.notification_attempt_key,
            message_id=repo.build_message_id(record, email),
            status=NOTIFICATION_STATUS_FAILED,
            error_message=cleaned_error,
        )
        repo.session.flush()
        return NotificationAttemptResult(
            final_status=record.notification_status or NOTIFICATION_STATUS_FAILED,
            sent=False,
            skipped=False,
            retry_scheduled=next_retry_at is not None,
            error_message=cleaned_error,
        )

    def _compute_next_retry_at(self, *, attempt_count: int, exception: Exception | None) -> Any | None:
        if attempt_count >= self.settings.mail_max_retry_attempts:
            return None

        delay = self.settings.mail_retry_base_seconds * (
            self.settings.mail_retry_backoff_multiplier ** max(0, attempt_count - 1)
        )
        if self.settings.mail_retry_jitter:
            delay = max(1, int(delay * random.uniform(0.75, 1.25)))

        if self._is_permanent_auth_error(exception):
            delay = max(delay, self.settings.mail_retry_base_seconds * self.settings.mail_retry_backoff_multiplier)

        return utcnow() + timedelta(seconds=delay)

    def _is_permanent_auth_error(self, exc: Exception | None) -> bool:
        if exc is None:
            return False
        return isinstance(exc, smtplib.SMTPAuthenticationError)

    def _clean_error_message(self, exc: Exception | None) -> str:
        if exc is None:
            return "Unknown mail send failure"
        if isinstance(exc, socket.timeout):
            return f"SMTP timeout after {self.settings.mail_send_timeout_seconds}s"
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return "SMTP authentication failed"
        if isinstance(exc, smtplib.SMTPConnectError):
            return "SMTP connection failed"
        if isinstance(exc, smtplib.SMTPServerDisconnected):
            return "SMTP server disconnected"
        if isinstance(exc, smtplib.SMTPResponseException):
            code = getattr(exc, "smtp_code", "unknown")
            message = getattr(exc, "smtp_error", b"")
            normalized = message.decode(errors="ignore") if isinstance(message, bytes) else str(message)
            return f"SMTP response error {code}: {normalized.strip() or exc.__class__.__name__}"
        if isinstance(exc, OSError):
            return str(exc)
        return exc.__class__.__name__

    def _build_body(self, case: dict[str, Any]) -> str:
        lines = [
            f"Case Number: {case.get('case_number') or ''}",
            f"Subject: {case.get('subject') or ''}",
            f"Status: {case.get('status') or ''}",
            f"Priority: {case.get('priority') or ''}",
            f"Product: {case.get('product_name') or ''}",
            f"Account: {case.get('account') or ''}",
            f"Created By: {case.get('created_by') or ''}",
            f"Created Date: {case.get('created_date') or ''}",
            f"Action Required: {self._display_action_required(case.get('action_required'))}",
            "",
            "Description:",
            case.get("description") or "",
        ]
        case_url = case.get("case_url")
        if case_url:
            lines.extend(["", f"Case Link: {case_url}"])
        return "\n".join(lines)

    def _build_test_body(self, case: dict[str, Any], summary: dict[str, Any]) -> str:
        lines = [
            "This is EMAIL_TEST_MODE output.",
            "",
            f"total_fetched: {summary.get('total_fetched')}",
            f"total_unique_cases: {summary.get('total_unique_cases')}",
            f"new_cases: {summary.get('new_cases')}",
            f"existing_cases: {summary.get('existing_cases')}",
            f"notifications_sent: {summary.get('notifications_sent')}",
            f"notifications_failed: {summary.get('notifications_failed')}",
            f"skipped_existing_cases: {summary.get('skipped_existing_cases')}",
            f"skipped_due_to_filter: {summary.get('skipped_due_to_filter')}",
            f"pages_fetched: {summary.get('pages_fetched')}",
            f"stop_reason: {summary.get('stop_reason')}",
            f"sample_case_status: {summary.get('sample_case_status')}",
            "",
            "Sample case:",
            self._build_body(case),
        ]
        return "\n".join(lines)

    def _build_html_body(self, case: dict[str, Any]) -> str:
        priority = case.get("priority") or "Unknown"
        badge_color = self._priority_color(priority)
        case_url = case.get("case_url")
        description = escape(case.get("description") or "").replace("\n", "<br>")
        rows = [
            ("Case Number", case.get("case_number")),
            ("Subject", case.get("subject")),
            ("Status", case.get("status")),
            ("Priority", f"<span style=\"display:inline-block;padding:4px 10px;border-radius:999px;background:{badge_color};color:#ffffff;font-weight:700;\">{escape(priority)}</span>"),
            ("Product", case.get("product_name")),
            ("Account", case.get("account")),
            ("Created By", case.get("created_by")),
            ("Created Date", case.get("created_date")),
            ("Action Required", self._display_action_required(case.get("action_required"))),
        ]
        table_rows = "\n".join(
            f"<tr><td style=\"padding:10px 12px;font-weight:600;border-bottom:1px solid #e5e7eb;width:180px;\">{escape(label)}</td>"
            f"<td style=\"padding:10px 12px;border-bottom:1px solid #e5e7eb;\">{value if label == 'Priority' else escape(str(value or ''))}</td></tr>"
            for label, value in rows
        )
        case_link = (
            f"<p style=\"margin:0 0 20px;\"><a href=\"{escape(case_url)}\" "
            "style=\"color:#0057b8;font-weight:600;text-decoration:none;\">Open case in SupportPortal Support</a></p>"
            if case_url
            else ""
        )
        return f"""
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:24px;background:#f3f6fb;font-family:Arial,Helvetica,sans-serif;color:#102a43;">
    <div style="max-width:760px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(16,42,67,0.08);">
      <div style="background:linear-gradient(135deg,#0057b8,#0b8fcb);padding:24px 28px;color:#ffffff;">
        <div style="font-size:12px;letter-spacing:1.4px;text-transform:uppercase;opacity:0.85;">SupportPortal Support</div>
        <h1 style="margin:8px 0 0;font-size:26px;line-height:1.2;">New Case Alert</h1>
        <p style="margin:10px 0 0;font-size:15px;opacity:0.92;">{escape(case.get('case_number') or 'UNKNOWN')} - {escape(case.get('subject') or 'No Subject')}</p>
      </div>
      <div style="padding:28px;">
        {case_link}
        <table role="presentation" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;background:#fdfefe;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
          {table_rows}
        </table>
        <div style="margin-top:24px;">
          <div style="font-size:13px;font-weight:700;color:#486581;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:8px;">Description</div>
          <div style="padding:18px;background:#f7fafc;border:1px solid #e2e8f0;border-radius:12px;font-size:14px;line-height:1.6;">{description or '<em>No description provided.</em>'}</div>
        </div>
      </div>
    </div>
  </body>
</html>
""".strip()

    def _build_test_html_body(self, case: dict[str, Any], summary: dict[str, Any]) -> str:
        summary_rows = [
            ("total_fetched", summary.get("total_fetched")),
            ("total_unique_cases", summary.get("total_unique_cases")),
            ("new_cases", summary.get("new_cases")),
            ("existing_cases", summary.get("existing_cases")),
            ("notifications_sent", summary.get("notifications_sent")),
            ("notifications_failed", summary.get("notifications_failed")),
            ("skipped_existing_cases", summary.get("skipped_existing_cases")),
            ("skipped_due_to_filter", summary.get("skipped_due_to_filter")),
            ("pages_fetched", summary.get("pages_fetched")),
            ("stop_reason", summary.get("stop_reason")),
            ("sample_case_status", summary.get("sample_case_status")),
        ]
        summary_table = "\n".join(
            f"<tr><td style=\"padding:8px 10px;font-weight:600;border-bottom:1px solid #e5e7eb;\">{escape(label)}</td><td style=\"padding:8px 10px;border-bottom:1px solid #e5e7eb;\">{escape(str(value))}</td></tr>"
            for label, value in summary_rows
        )
        return f"""
<!DOCTYPE html>
<html>
  <body style="font-family:Arial,Helvetica,sans-serif;padding:24px;background:#f8fafc;color:#102a43;">
    <h1 style="margin-top:0;">SupportPortal Test Cycle Summary</h1>
    <table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;">
      {summary_table}
    </table>
    <h2>Sample case</h2>
    {self._build_html_body(case)}
  </body>
</html>
""".strip()

    def _display_action_required(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    def _priority_color(self, priority: str) -> str:
        normalized = (priority or "").lower()
        if "1" in normalized:
            return "#c81e1e"
        if "2" in normalized:
            return "#e76f00"
        if "3" in normalized:
            return "#0f766e"
        return "#1d4ed8"
