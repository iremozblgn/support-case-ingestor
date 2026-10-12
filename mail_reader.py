import imaplib
import email
import logging
from email.header import decode_header
from typing import List, Dict, Any, Optional
import re

def read_emails(imap_host: str, imap_port: int, username: str, password: str,
                mailbox: str = 'INBOX', unseen_only: bool = True) -> List[Dict[str, Any]]:
    """
    Connect to IMAP and read emails.
    """
    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(username, password)
        mail.select(mailbox)

        # Search for messages
        if unseen_only:
            status, messages = mail.search(None, 'UNSEEN')
        else:
            status, messages = mail.search(None, 'ALL')

        email_list = []
        for msg_id in messages[0].split():
            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            email_body = msg_data[0][1]
            email_message = email.message_from_bytes(email_body)

            # Extract details
            message_id = email_message.get('Message-ID')
            subject = decode_header(email_message.get('Subject', ''))[0][0]
            if isinstance(subject, bytes):
                subject = subject.decode()
            sender = email_message.get('From', '')

            # Extract body
            body_text = extract_body_text(email_message)

            email_list.append({
                'message_id': message_id,
                'subject': subject,
                'sender': sender,
                'body_text': body_text
            })

        mail.logout()
        return email_list
    except Exception as e:
        logging.error(f"Error reading emails: {e}")
        return []

def extract_body_text(email_message) -> str:
    """
    Extract plain text body from email.
    """
    if email_message.is_multipart():
        for part in email_message.walk():
            if part.get_content_type() == 'text/plain':
                return part.get_payload(decode=True).decode('utf-8', errors='ignore')
            elif part.get_content_type() == 'text/html':
                html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                return strip_html(html)
    else:
        if email_message.get_content_type() == 'text/plain':
            return email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
        elif email_message.get_content_type() == 'text/html':
            html = email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
            return strip_html(html)
    return ''

def strip_html(html: str) -> str:
    """
    Simple HTML tag stripping.
    """
    return re.sub(r'<[^>]+>', '', html)