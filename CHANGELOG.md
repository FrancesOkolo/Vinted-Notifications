# Changelog

## Unreleased

### Priority monitoring and deal guidance

- Add per-query Normal and Fast monitoring modes. Fast searches use a safe,
  serialized 90-second schedule while ordinary searches retain the configured
  refresh interval.
- Allow selected priority queries to keep scraping and notify immediately
  during quiet hours, while all ordinary queries remain paused.
- Stagger per-query schedules and keep one scraper worker so priority searches
  do not create a second concurrent request stream or a cold-start burst.
- Add optional per-query Excellent and Good listing-price ceilings. Telegram
  alerts are labelled Excellent, Good, or Don't Buy (above the chosen limit),
  with an explicit reminder that postage, buyer fees, condition, and
  authenticity are not included in the rating.
- Treat missing prices, invalid thresholds, and currency mismatches as Not
  Rated without dropping the notification.

### Reliability, subscriptions, and configuration

- Acknowledge durable Telegram alerts per recipient so a failure for one user
  does not resend the same alert to users who already received it.
- Commit item discovery and its Telegram outbox row in one SQLite transaction,
  so a database failure cannot mark an item seen before its alert is durable.
- Treat Telegram approval lookup failures as temporary delivery failures rather
  than deleting the queued recipient as though their access was revoked.
- Defer queued alerts when eligibility cannot be read, reject corrupt
  recipient data without guessing another recipient, and stop safely when an
  outbox retry-state write fails.
- Prevent overlapping instances of the scheduled outbox job in one process.
- Preserve shared queries and item history after a Telegram user unsubscribes,
  including when the last subscriber leaves, so the same message can be used to
  resubscribe later.
- Assign legacy orphaned queries to the primary Telegram account only once,
  preventing an intentional unsubscribe from being reversed at the next start.
- Apply overnight quiet hours according to the selected day on which the quiet
  period starts, and initialise weekday defaults consistently for new and
  existing databases.
- Support `+` in banword rules to require multiple title terms in any order
  (for example, `empty+box`).
- Keep Configuration's scrape-duration estimate aligned with the scraper's
  actual pacing calculation.

### Security and upgrades

- Prevent Telegram test failures, malformed API replies, and remote error text
  from exposing the bot token in the browser or logs.
- Match database migrations by their exact source version, reject ambiguous
  migration paths, and stop if a migration does not advance the version.

## 1.1.1 - 2026-07-30

### Notifications and scraper reliability

- Keep the Telegram outbox scheduler alive after a stalled or cancelled send,
  and recheck a query's current pause/subscription state before delivery.
- Retry transient connection resets with a fresh Vinted session.
- Add configurable quiet-hour weekdays and an IANA timezone selector.
- Show Quiet mode directly in the Dashboard health summary.

### Navigation

- Add First and Last controls to paginated query and item results.

## 1.1.0 - 2026-07-22

### Scraper reliability

- Pace large query sets across the refresh window instead of sending one large
  startup burst.
- Add persisted cooldown protection for repeated Vinted `403`/`429` responses.
- Add scraper heartbeat, stalled/blocked detection, recovery reporting, and
  durable pending-notification handling.
- Avoid a false stalled-scraper alert immediately after a clean restart.
- Allow a second test instance to send Telegram notifications without polling
  commands through `VN_TELEGRAM_POLLING=false` or `--telegram-send-only`.

### Telegram

- Turn notification subscription actions into an Unsubscribe / Resubscribe
  toggle for each recipient.

### Items and interface

- Add a bulk action to pause selected queries across filtered and paginated
  query results.
- Add server-side item search, price filters, sorting, and pagination.
- Add relative timestamps, recent-item badges, lazy-loaded images, and image
  fallbacks.
- Improve phone, tablet, and desktop layouts and navigation.
- Add a persistent light/dark theme toggle.
- Add a responsive Dashboard health summary with a link to detailed health
  information on Configuration.

### Security and release quality

- Add CSP nonces, secure browser headers, strict session cookies, CSRF
  enforcement, request-size limits, and safe text rendering for dynamic UI
  content.
- Redact Telegram tokens and proxy credentials from logs.
- Temporarily throttle repeated incorrect Web UI credentials.
- Fail closed when a network-facing Web UI lacks credentials or a persistent
  session key, and when RSS lacks its access token.
- Keep runtime data separate from application source and private to the runtime
  account.
- Add pinned Black, Ruff, coverage, and Playwright development tooling.
- Enforce formatting, linting, dependency auditing, a coverage floor, and a
  real-browser CSP/XSS/responsiveness smoke test in CI.
- Gate Docker Hub release publishing on both the Python suite and browser smoke
  test.

### Deployment impact

- No new production dependency.
- Existing databases are upgraded automatically by idempotent startup
  migrations.
- Web UI startup on a network-facing address requires `VN_WEB_USERNAME`,
  `VN_WEB_PASSWORD`, and a persistent `VN_SECRET_KEY`.
- RSS requires `VN_RSS_TOKEN` whenever it is enabled.
- Both direct Windows operation and the optional Docker setup keep `data/`,
  `logs/`, and secret environment files outside version control.
