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

### Items and interface

- Add server-side item search, price filters, sorting, and pagination.
- Add relative timestamps, recent-item badges, lazy-loaded images, and image
  fallbacks.
- Improve phone, tablet, and desktop layouts and navigation.
- Add a persistent light/dark theme toggle.
- Add a responsive Dashboard health summary with a link to detailed health
  information on Configuration.

### Deployment impact

- No new production dependency.
- No new required environment variable.
- No new database schema migration.
- Existing `data/`, `logs/`, and `/root/vinted.env` continue to be used.
- Full automated suite: 43 tests passing.
