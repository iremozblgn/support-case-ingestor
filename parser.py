import re
from typing import Dict, Any, Optional, List

def parse_support_portal_text(raw_text: str, source_file: str) -> Dict[str, Any]:
    """
    Parse SupportPortal notification text into structured JSON using line-based extraction.
    """
    # Split text into lines, trim whitespace, ignore empty lines
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    # Initialize fields
    case_number: Optional[str] = None
    subject: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    product_name: Optional[str] = None
    open_date: Optional[str] = None
    assigned_to: Optional[str] = None
    case_url: Optional[str] = None

    # Process each line
    for line in lines:
        # Case number
        if not case_number:
            if (line.startswith('Case Number:') or line.startswith('Case #:') or line.startswith('Case:')):
                case_number = clean_value(line.split(':', 1)[1])
            elif re.match(r'^\d+$', line):
                case_number = line

        # Severity
        if not severity:
            if line.startswith('Severity'):
                if ':' in line:
                    after_colon = clean_value(line.split(':', 1)[1])
                    match = re.search(r'\d+', after_colon)
                    severity = match.group(0) if match else after_colon
                else:
                    match = re.search(r'\d+', line)
                    severity = match.group(0) if match else None

        # Open date
        if not open_date:
            if line.startswith('Open Date:'):
                open_date = clean_value(line.split(':', 1)[1])
            elif re.match(r'\d{1,2}/\d{1,2}/\d{4}, \d{1,2}:\d{2} (AM|PM)', line):
                open_date = line

        # Subject
        if not subject:
            if line.startswith('Subject:'):
                subject = clean_value(line.split(':', 1)[1])
            elif '[' in line and ']' in line:
                subject = line

        # Status
        if not status:
            if line.startswith('Status:'):
                status_value = clean_value(line.split(':', 1)[1])
                status = dedupe_repeated_phrase(status_value)
            elif 'Awaiting Customer' in line:
                status = 'Awaiting Customer'
            elif line in ['Open', 'Closed', 'Awaiting Customer', 'Waiting on Customer']:
                status = line

        # Product name
        if not product_name:
            if line.startswith('Product Name:'):
                product_name = clean_value(line.split(':', 1)[1])
            elif line.startswith('Product:'):
                product_name = clean_value(line.split(':', 1)[1])
            elif line in ['Web Security', 'SupportPortal DLP', 'SupportPortal Web Security']:
                product_name = line

        # Assigned to
        if not assigned_to:
            if (line.startswith('Assigned To:') or line.startswith('Recipient:') or
                line.startswith('Contact Name:') or line.startswith('Contact:')):
                assigned_to = clean_value(line.split(':', 1)[1])

        # Case URL
        if not case_url:
            if line.startswith('Case URL:') or line.startswith('URL:'):
                case_url = clean_value(line.split(':', 1)[1])
            elif line.startswith('http://') or line.startswith('https://'):
                case_url = line

    # Determine action_required
    action_required = (
        'action required' in raw_text.lower() or
        status in ['Awaiting Customer', 'Waiting on Customer']
    )

    return {
        'source': 'support_portal',
        'case_number': case_number,
        'subject': subject,
        'status': status,
        'severity': severity,
        'product_name': product_name,
        'open_date': open_date,
        'action_required': action_required,
        'assigned_to': assigned_to,
        'case_url': case_url,
        'raw_text': raw_text,
        'source_file': source_file
    }

def clean_value(value: str) -> str:
    """
    Strip leading/trailing whitespace and collapse repeated internal whitespace to single spaces.
    """
    return re.sub(r'\s+', ' ', value.strip())

def dedupe_repeated_phrase(value: str) -> str:
    """
    If value is two identical halves, reduce to one half; otherwise return cleaned value.
    """
    cleaned = clean_value(value)
    words = cleaned.split()
    if len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return ' '.join(words[:half])
    return cleaned