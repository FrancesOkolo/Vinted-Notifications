# Changelog

## Unreleased

### Telegram item sharing

- Add Share on WhatsApp and Copy item link actions to Vinted item alerts while
  preserving the existing Open Vinted and unsubscribe/resubscribe controls.
- Share a short canonical public listing URL without tracking parameters, and
  leave status, health, and version-notification links unchanged.

### Scraper session and logging recovery

- Replace independent per-query jobs with one central, coalescing dispatcher
  and a shared capacity plan. Fast searches receive priority without starving
  Normal searches, while requested cadences are lengthened when necessary to
  fit the safe aggregate budget.
- Enforce at least 12 seconds from completion of one catalogue/authentication
  request to the start of the next, plus positive jitter; retries and cookie
  refreshes consume the same gate.
- Rebuild an expired Vinted catalogue session once after HTTP 401, then open
  the shared scraper cooldown if the fresh session is also rejected.
- Open one bounded global cooldown on the first HTTP 429 response instead of
  sleeping inside every queued per-query job and starving the scheduler.
- Keep per-query failures from being counted as failed whole-query sweeps, and
  fail allowlist country lookups safely without terminating item processing.
- Treat one HTTP 403 that remains after a fresh-session retry as a confirmed
  block and open protection immediately without testing more queries.
- After an expired HTTP 403 cooldown, make one recovery probe and escalate
  repeated rejection from 5 minutes to 30 minutes, 2 hours, then 8 hours.
- Log one warning per open cooldown instead of repeating it on every dispatcher
  tick.
- Route all Windows child-process logs through one parent-owned rotating file
  writer so rollover cannot freeze when several processes reach 10 MiB.

### AI deal-evaluator hardening

- Keep AI ratings asynchronous without spawning an unbounded thread per item.
  A single serialized worker now uses a durable leased SQLite queue, bounded
  retries, and crash-safe handoff to the Telegram outbox.
- Preserve each item's immediate alert even when OpenAI is slow or unavailable,
  and keep a later AI verdict behind its parent alert.
- Send AI follow-ups only to recipients whose primary Telegram alert was
  confirmed delivered. Mixed-recipient retries reuse one cached verdict, while
  pause, unsubscribe, revocation, and exhausted deliveries remain distinct.
- Default the evaluator to `gpt-5.6-terra`, keep browsing disabled, distinguish
  retryable API failures from permanent ones, and avoid truncating escaped HTML
  entities into malformed Telegram messages.
- Document the optional OpenAI environment settings and the listing metadata
  sent for evaluation.

### Live editing safeguards

- Apply Normal/Fast polling changes to the live scheduler without requiring an
  application restart.
- Refresh Edit-query data when the modal opens and reject a stale browser tab
  before it can overwrite newer URL, name, polling, quiet-hours, or deal-rating
  settings.
- Save query details, preferences, and AI membership in one SQLite transaction.
- Enforce the five-active-Fast-query safety cap inside the same SQLite write
  transaction for Add, Edit, single Resume, and bulk Resume operations.

### Priority monitoring and deal guidance

- Add per-query Normal and Fast monitoring modes. The selected values are base
  cadence requests; the central safety plan may lengthen either cadence to keep
  total Vinted traffic inside the shared budget.
- Allow selected priority queries to keep scraping and notify immediately
  during quiet hours, while all ordinary queries remain paused.
- Coalesce overdue work and keep one scraper worker so priority searches do not
  create a second concurrent request stream or a cold-start burst.
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
