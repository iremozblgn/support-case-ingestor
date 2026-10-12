import requests
import logging
from typing import Dict, Any, Optional

def send_payload(payload: Dict[str, Any], api_url: Optional[str], api_token: Optional[str]) -> None:
    """
    Send POST request to portal API.
    """
    if not api_url:
        logging.warning("PORTAL_API_URL not set, skipping send.")
        return

    headers = {'Content-Type': 'application/json'}
    if api_token:
        headers['Authorization'] = f'Bearer {api_token}'

    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            logging.info(f"Successfully sent payload for case {payload.get('case_number')}")
        else:
            logging.error(f"Failed to send payload: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        logging.error(f"Error sending payload: {e}")