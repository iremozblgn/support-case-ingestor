# Support Portal Ingestor

`support_portal_ingestor` can now ingest Support Portal Salesforce cases directly from the hidden Aura GraphQL endpoint instead of depending on Playwright and HTML scraping for the main workflow. The legacy portal mode is still available.

## Modes

- `INGEST_MODE=graphql`: Primary mode. Uses `requests.Session` against Salesforce Aura `executeGraphQL`.
- `INGEST_MODE=portal`: Legacy Playwright flow.
- `INGEST_MODE=imap` and `INGEST_MODE=file`: Legacy auxiliary flows retained for compatibility.

## Project Layout

```text
support_portal_ingestor/
  app.py
  config.py
  support_portal/
  notifications/
  services/
  storage/
  utils/
  scripts/test_graphql_fetch.py
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in the Support Portal session values.

## GraphQL Configuration

Required for `INGEST_MODE=graphql`:

- `SUPPORT_PORTAL_GRAPHQL_URL`
- `SUPPORT_PORTAL_COOKIE`
- `SUPPORT_PORTAL_AURA_CONTEXT`
- `SUPPORT_PORTAL_AURA_TOKEN`

Optional browser replay settings:

- `SUPPORT_PORTAL_BROWSER_PARITY_MODE=true`
- `SUPPORT_PORTAL_BROWSER_PARITY_MESSAGE_PATH=debug/browser_message.json`

Default endpoint:

```text
https://support.example.com/s/sfsites/aura?r=16&aura.RecordUi.executeGraphQL=1
```

Useful defaults:

- `CASE_STATUS_FILTER=Awaiting Customer`
- `PAGE_SIZE=20`
- `RUN_ONCE=true`
- `CHECK_INTERVAL_SECONDS=300`
- `DATABASE_URL=sqlite:///support_portal_cases.db`

## How To Capture The Working Request

1. Open Chrome DevTools on the Support Portal support portal.
2. Load the case list page, usually `/s/case/Case/Default`.
3. In the Network tab, filter for `aura.RecordUi.executeGraphQL`.
4. Open the working request and copy:
   - the full `Cookie` header
   - the `aura.context` form field
   - the `aura.token` form field
5. Copy the raw `message` form field into `debug/browser_message.json`.
6. Paste the other values into `.env`.

You can also capture the same request in Postman if DevTools is easier for your workflow.

## Running

One-shot GraphQL run:

```bash
python app.py
```

Polling mode:

```bash
RUN_ONCE=false CHECK_INTERVAL_SECONDS=300 python app.py
```

Direct GraphQL smoke test:

```bash
python scripts/test_graphql_fetch.py
```

Inspect captured browser payload:

```bash
python scripts/inspect_browser_payload.py
```

Legacy portal mode:

```bash
INGEST_MODE=portal python app.py
```

## Email Notifications

Set:

- `SEND_ENABLED=true`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `TO_EMAIL`

The subject format is:

```text
[SUPPORT_PORTAL][NEW CASE] {case_number} - {subject}
```

If `SEND_ENABLED=false`, the monitor still stores cases but skips email delivery.

## Storage

The monitor stores data through SQLAlchemy in:

- `cases`
- `notification_logs`

SQLite is the default. PostgreSQL works by setting `DATABASE_URL` to a PostgreSQL connection string.

## Logging

Logs go to:

- console
- `logs/app.log`

The logs include request lifecycle, pagination, parsed case counts, deduplication decisions, and email status.

## Common Errors

- `401` or `403`: the Support Portal session has expired or the cookies are no longer valid.
- Invalid `aura.token`: refresh the request from Chrome DevTools or Postman.
- Empty results: the current `CASE_STATUS_FILTER` or custom where clause may be too restrictive.
- SMTP failure: verify TLS settings, credentials, and the recipient address.
- Non-JSON or malformed Aura response: Support Portal may have changed the request shape or the session may be redirecting to login.

## Notes About Secrets

- Do not commit cookies, tokens, or SMTP credentials.
- The app masks sensitive Support Portal values in startup logs.
- Avoid pasting full `Cookie` or `aura.token` values into tickets or chat logs.

## Known Limitations

- The GraphQL call still depends on a live authenticated Salesforce session.
- Session refresh is manual; when cookies or Aura tokens expire you must recapture them.
- New-case notification is keyed to first discovery of a case record, not every later update.
