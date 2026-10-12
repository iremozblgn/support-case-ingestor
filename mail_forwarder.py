import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any

def forward_email(smtp_host: str, smtp_port: int, username: str, password: str,
                  use_tls: bool, to_email: str, from_email: Optional[str],
                  original_subject: str, original_sender: str, body_text: str,
                  parsed_summary: Optional[Dict[str, Any]] = None) -> bool:
    """
    Forward email via SMTP.
    """
    try:
        msg = MIMEMultipart()
        msg['From'] = from_email or username
        msg['To'] = to_email
        msg['Subject'] = f"Fwd: {original_subject}"

        # Build body
        body = f"Original Sender: {original_sender}\n\n"
        if parsed_summary:
            body += f"Parsed Case: {parsed_summary.get('case_number', 'N/A')}\n"
            body += f"Status: {parsed_summary.get('status', 'N/A')}\n"
            body += f"Action Required: {parsed_summary.get('action_required', False)}\n\n"
        body += f"Original Body:\n{body_text}"

        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(smtp_host, smtp_port)
        if use_tls:
            server.starttls()
        server.login(username, password)
        server.sendmail(msg['From'], to_email, msg.as_string())
        server.quit()

        logging.info(f"Forwarded email to {to_email}")
        return True
    except Exception as e:
        logging.error(f"Error forwarding email: {e}")
        return False