import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv


load_dotenv()


def send_case_email(case):
    try:
        if not isinstance(case, dict):
            print("Invalid case format:", case)
            raise TypeError(f"Expected dict, got {type(case)}")

        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_username = os.getenv("SMTP_USERNAME")
        smtp_password = os.getenv("SMTP_PASSWORD")
        smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        to_email = os.getenv("TO_EMAIL") or os.getenv("FORWARD_TO_EMAIL")

        if not smtp_host or not smtp_username or not smtp_password or not to_email:
            raise ValueError("SMTP configuration is incomplete")

        case_number = case.get("case_number", "N/A")
        case_subject = case.get("subject", "No Subject")

        subject = f"[SUPPORT_PORTAL][NEW CASE] {case_number} - {case_subject}"

        body = f"""Case Number: {case_number}
Account: {case.get('account')}
Created By: {case.get('created_by')}

Description: {case.get('description')}

Portal Link: https://support.example.com/s/case/{case.get('case_id')}
"""

        msg = MIMEMultipart()
        msg["From"] = smtp_username
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        print(f"📤 Email sent for case {case_number}")
    except Exception as e:
        print(f"❌ send_case_email error: {e}")
