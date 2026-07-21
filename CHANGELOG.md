# Changelog

## Unreleased

These changes are committed locally on `frances/current-working-version` and
have not been pushed or deployed.

### Scraper reliability

- Pace large query sets across the refresh window instead of sending one large
  startup burst.
- Add persisted cooldown protection for repeated Vinted `403`/`429` responses.
- Add scraper heartbeat, stalled/blocked detection, recovery reporting, and
  durable pending-notification handling.
- Avoid a false stalled-scraper alert immediately after a clean restart.
- Keep local Telegram testing compatible with a live server through
  `VN_TELEGRAM_POLLING=false` or `--telegram-send-only`.

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

### Deployment impact

- No new production dependency.
- No new database schema migration.
- Existing `data/`, `logs/`, and `/root/vinted.env` continue to be used.
- Network-facing Web UI startup now requires `VN_WEB_USERNAME`,
  `VN_WEB_PASSWORD`, and a persistent `VN_SECRET_KEY`.
- RSS now requires `VN_RSS_TOKEN` whenever it is enabled.
- The recommended Docker launch binds ports to loopback, uses a read-only root
  filesystem, limits Docker logs, and is accessed through an encrypted SSH
  tunnel unless an HTTPS reverse proxy is configured.
- Full automated suite: 56 tests passing, one optional real-browser smoke test
  passing, and 50% measured coverage against a 48% minimum.

### Security and release quality

- Add CSP nonces, secure browser headers, strict session cookies, CSRF
  enforcement, request-size limits, and safe text rendering for dynamic UI
  content.
- Redact Telegram tokens and proxy credentials from logs.
- Temporarily throttle repeated incorrect Web UI credentials.
- Fail closed when a network-facing Web UI lacks credentials or a persistent
  session key, and when RSS lacks its access token.
- Keep runtime data private to the unprivileged container account.
- Add pinned Black, Ruff, coverage, and Playwright development tooling.
- Enforce formatting, linting, dependency auditing, a coverage floor, and a
  real-browser CSP/XSS/responsiveness smoke test in CI.
- Gate Docker Hub release publishing on both the Python suite and browser smoke
  test.
