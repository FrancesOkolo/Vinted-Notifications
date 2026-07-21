import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

# Create logs directory if it doesn't exist
os.makedirs("logs", exist_ok=True)


# Custom filter to exclude specific log messages
class ExcludeFilter(logging.Filter):
    def filter(self, record):
        # Filter out APScheduler executor logs about running jobs
        if record.name == "apscheduler.executors.default" and (
            "Running job" in record.getMessage()
            or "executed successfully" in record.getMessage()
        ):
            return False

        # Filter out APScheduler scheduler logs about job management
        if record.name == "apscheduler.scheduler" and (
            "Added job" in record.getMessage()
            or "Adding job tentatively" in record.getMessage()
            or "Removed job" in record.getMessage()
            or "Scheduler started" in record.getMessage()
            or "skipped: maximum number of running instances reached"
            in record.getMessage()
        ):
            return False

        # Filter out httpx HTTP request logs
        if record.name == "httpx" and "HTTP Request:" in record.getMessage():
            return False

        # Filter out log refresh requests from the web UI
        if record.name == "werkzeug" and "GET /api/logs" in record.getMessage():
            return False

        return True


_TELEGRAM_TOKEN_PATTERN = re.compile(
    r"(?i)(api\.telegram\.org/bot|\bbot)(\d{6,12}:[A-Za-z0-9_-]{20,})"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@")


def redact_secrets(value):
    """Remove known credential shapes before a message reaches any handler."""
    text = str(value)
    text = _TELEGRAM_TOKEN_PATTERN.sub(r"\1[REDACTED]", text)
    return _URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", text)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        # Resolve %-style arguments first, then clear them so the sanitized
        # message cannot be combined with the original secrets by Formatter.
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        return True


# Configure the root logger
def configure_root_logger():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)

    # File handler for all logs
    file_handler = RotatingFileHandler(
        "logs/vinted.log", maxBytes=10 * 1024 * 1024, backupCount=5  # 10MB
    )
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_formatter)

    # Redact before filtering so neither the terminal nor the rotating file can
    # receive Telegram tokens or proxy credentials from exception text.
    secret_filter = SecretRedactionFilter()
    exclude_filter = ExcludeFilter()
    console_handler.addFilter(secret_filter)
    console_handler.addFilter(exclude_filter)
    file_handler.addFilter(secret_filter)
    file_handler.addFilter(exclude_filter)

    # Add handlers to root logger
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


# Get a logger for a specific module
def get_logger(name):
    if not logging.getLogger().handlers:
        configure_root_logger()
    return logging.getLogger(name)
