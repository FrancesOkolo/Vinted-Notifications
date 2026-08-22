import io
import logging

import logger as app_logger


def _log_from_child(worker_id, line_count):
    child_logger = logging.getLogger(f"multiprocess-test.worker-{worker_id}")
    for line_number in range(line_count):
        child_logger.info(
            "worker=%s line=%s payload=%s",
            worker_id,
            line_number,
            "x" * 32,
        )


def _log_secrets_from_child():
    child_logger = logging.getLogger("multiprocess-test.secrets")
    child_logger.error(
        "telegram=https://api.telegram.org/"
        "bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd/getMe "
        "proxy=https://alice:supersecret@example.test/path"
    )


def _all_rotated_text(log_path):
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(log_path.parent.glob(f"{log_path.name}*"))
    )


def test_logged_processes_use_one_rotating_writer(tmp_path):
    log_path = tmp_path / "vinted.log"
    app_logger.stop_logging_listener()
    log_queue = app_logger.start_logging_listener(
        log_path=log_path,
        max_bytes=1_200,
        backup_count=10,
        console_stream=io.StringIO(),
    )

    processes = [
        app_logger.LoggedProcess(
            log_queue,
            target=_log_from_child,
            args=(worker_id, 12),
        )
        for worker_id in range(3)
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert not process.is_alive()
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        app_logger.stop_logging_listener()

    combined = _all_rotated_text(log_path)
    for worker_id in range(3):
        for line_number in range(12):
            assert f"worker={worker_id} line={line_number}" in combined

    rotated_files = list(tmp_path.glob("vinted.log*"))
    assert len(rotated_files) > 1
    assert len(rotated_files) <= 11


def test_child_records_are_redacted_before_file_delivery(tmp_path):
    log_path = tmp_path / "vinted.log"
    app_logger.stop_logging_listener()
    log_queue = app_logger.start_logging_listener(
        log_path=log_path,
        console_stream=io.StringIO(),
    )
    process = app_logger.LoggedProcess(log_queue, target=_log_secrets_from_child)

    try:
        process.start()
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        app_logger.stop_logging_listener()

    contents = log_path.read_text(encoding="utf-8")
    assert "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd" not in contents
    assert "alice:supersecret" not in contents
    assert "bot[REDACTED]" in contents
    assert "https://[REDACTED]@example.test/path" in contents


def test_spawn_child_never_installs_a_rotating_file_handler(monkeypatch):
    root_logger = logging.getLogger()
    app_logger.stop_logging_listener()
    app_logger._remove_vinted_handlers(root_logger)
    monkeypatch.setattr(
        app_logger.multiprocessing.current_process(),
        "name",
        "SpawnProcess-1",
    )

    app_logger.configure_root_logger()

    vinted_handlers = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, app_logger._HANDLER_MARKER, False)
    ]
    try:
        assert len(vinted_handlers) == 1
        assert isinstance(vinted_handlers[0], logging.StreamHandler)
        assert not isinstance(vinted_handlers[0], app_logger.RotatingFileHandler)
    finally:
        app_logger._remove_vinted_handlers(root_logger)


def test_standalone_file_handler_honors_environment_log_path(tmp_path, monkeypatch):
    log_path = tmp_path / "standalone" / "tool.log"
    root_logger = logging.getLogger()
    app_logger.stop_logging_listener()
    app_logger._remove_vinted_handlers(root_logger)
    monkeypatch.setenv("VN_LOG_PATH", str(log_path))
    monkeypatch.delenv("VN_DISABLE_FILE_LOGGING", raising=False)
    monkeypatch.setattr(
        app_logger.multiprocessing.current_process(),
        "name",
        "MainProcess",
    )

    app_logger.configure_root_logger()
    try:
        logging.getLogger("standalone-tool-test").info("redirected log record")
        for handler in root_logger.handlers:
            handler.flush()
    finally:
        app_logger._remove_vinted_handlers(root_logger)

    assert "redirected log record" in log_path.read_text(encoding="utf-8")


def test_file_logging_can_be_disabled_for_tools(tmp_path, monkeypatch):
    log_path = tmp_path / "must-not-exist.log"
    root_logger = logging.getLogger()
    app_logger.stop_logging_listener()
    app_logger._remove_vinted_handlers(root_logger)
    monkeypatch.setenv("VN_LOG_PATH", str(log_path))
    monkeypatch.setenv("VN_DISABLE_FILE_LOGGING", "true")
    monkeypatch.setattr(
        app_logger.multiprocessing.current_process(),
        "name",
        "MainProcess",
    )

    app_logger.configure_root_logger()
    try:
        vinted_handlers = [
            handler
            for handler in root_logger.handlers
            if getattr(handler, app_logger._HANDLER_MARKER, False)
        ]
        assert len(vinted_handlers) == 1
        assert isinstance(vinted_handlers[0], logging.StreamHandler)
        assert not isinstance(vinted_handlers[0], app_logger.RotatingFileHandler)
    finally:
        app_logger._remove_vinted_handlers(root_logger)

    assert not log_path.exists()
