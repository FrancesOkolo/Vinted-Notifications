import atexit
import logging
import multiprocessing
import os
import re
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

DEFAULT_LOG_PATH = os.path.join("logs", "vinted.log")
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
_HANDLER_MARKER = "_vinted_notifications_handler"

_listener = None
_listener_handlers = ()
_logging_queue = None
_owns_logging_queue = False


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


def _mark_handler(handler):
    setattr(handler, _HANDLER_MARKER, True)
    return handler


def _add_filters(handler):
    # Queue handlers redact before a record is serialized into the
    # multiprocessing queue. Listener-side handlers apply the same filters as
    # a defence in depth and preserve the previous direct-handler behaviour.
    handler.addFilter(SecretRedactionFilter())
    handler.addFilter(ExcludeFilter())
    return handler


def _formatter():
    return logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def _file_logging_disabled():
    value = os.getenv("VN_DISABLE_FILE_LOGGING", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolved_log_path(log_path=None):
    if log_path is not None:
        return os.fspath(log_path)
    return os.getenv("VN_LOG_PATH", "").strip() or DEFAULT_LOG_PATH


def _console_handler(stream=None):
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    return _add_filters(_mark_handler(handler))


def _file_handler(
    log_path=None,
    max_bytes=DEFAULT_MAX_BYTES,
    backup_count=DEFAULT_BACKUP_COUNT,
):
    log_path = _resolved_log_path(log_path)
    log_directory = os.path.dirname(os.path.abspath(log_path))
    os.makedirs(log_directory, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    return _add_filters(_mark_handler(handler))


def _remove_vinted_handlers(root_logger):
    for handler in list(root_logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _queue_handler(log_queue):
    handler = QueueHandler(log_queue)
    handler.setLevel(logging.INFO)
    # Redact and exclude before QueueHandler.prepare() copies and serializes the
    # record, keeping secrets out of the inter-process queue as well as files.
    return _add_filters(_mark_handler(handler))


def configure_root_logger():
    """Configure direct console/file logging for a single-process caller.

    The production entry point replaces these handlers with a parent-owned
    QueueListener before it creates children. Spawned children initially get a
    console-only fallback, then ``LoggedProcess.run`` replaces it with a
    QueueHandler before invoking the process target.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if multiprocessing.current_process().name == "MainProcess":
        root_logger.addHandler(_console_handler())
        if not _file_logging_disabled():
            root_logger.addHandler(_file_handler())
    else:
        # Never let a Windows-spawn child open the rotating file directly.
        root_logger.addHandler(_console_handler())


def configure_process_logging(log_queue):
    """Route one child process into the parent's central logging queue."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    _remove_vinted_handlers(root_logger)
    root_logger.addHandler(_queue_handler(log_queue))


def start_logging_listener(
    log_queue=None,
    *,
    log_path=None,
    max_bytes=DEFAULT_MAX_BYTES,
    backup_count=DEFAULT_BACKUP_COUNT,
    console_stream=None,
):
    """Start the sole console/file writer and route the parent through it.

    Only the parent calls this function. All child records arrive through a
    multiprocessing queue, so exactly one RotatingFileHandler owns the Windows
    file handle and performs rollover.
    """
    global _listener, _listener_handlers, _logging_queue, _owns_logging_queue

    if _listener is not None:
        return _logging_queue

    if log_queue is None:
        log_queue = multiprocessing.Queue()
        _owns_logging_queue = True
    else:
        _owns_logging_queue = False

    console_handler = _console_handler(console_stream)
    listener_handlers = [console_handler]
    if not _file_logging_disabled():
        listener_handlers.append(_file_handler(log_path, max_bytes, backup_count))
    listener = QueueListener(
        log_queue,
        *listener_handlers,
        respect_handler_level=True,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    _remove_vinted_handlers(root_logger)
    root_logger.addHandler(_queue_handler(log_queue))

    _logging_queue = log_queue
    _listener_handlers = tuple(listener_handlers)
    _listener = listener
    listener.start()
    return log_queue


def stop_logging_listener():
    """Flush queued records and release the listener-owned file handle."""
    global _listener, _listener_handlers, _logging_queue, _owns_logging_queue

    listener = _listener
    if listener is None:
        return

    root_logger = logging.getLogger()
    _remove_vinted_handlers(root_logger)

    try:
        listener.stop()
    finally:
        for handler in _listener_handlers:
            try:
                handler.close()
            except Exception:
                pass

        if _owns_logging_queue and _logging_queue is not None:
            try:
                _logging_queue.close()
                _logging_queue.join_thread()
            except (OSError, ValueError):
                pass

        _listener = None
        _listener_handlers = ()
        _logging_queue = None
        _owns_logging_queue = False


class LoggedProcess(multiprocessing.Process):
    """Process that switches to central queue logging before its target runs."""

    def __init__(self, log_queue, *args, **kwargs):
        self.log_queue = log_queue
        super().__init__(*args, **kwargs)

    def run(self):
        configure_process_logging(self.log_queue)
        super().run()


def get_logger(name):
    if not logging.getLogger().handlers:
        configure_root_logger()
    return logging.getLogger(name)


atexit.register(stop_logging_listener)
