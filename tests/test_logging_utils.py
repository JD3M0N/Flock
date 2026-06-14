import json
import logging
from logging.handlers import RotatingFileHandler

from shared_logging_utils import configure_logger, log_event, sanitize, summarize_command


REQUIRED_FIELDS = {
    "timestamp",
    "level",
    "component",
    "node",
    "event",
    "correlation_id",
    "peer",
    "username",
    "version",
    "range",
    "result",
}


def read_log_line(path):
    for handler in logging.getLogger("flock.test").handlers:
        handler.flush()
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_json_lines_include_required_fields_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_LOG_DIR", str(tmp_path))
    logger = configure_logger("flock.test", "test.log")

    log_event(
        logger,
        "INFO",
        "register_accepted",
        node="node-a",
        peer="10.0.0.2",
        username="alice",
        version=7,
        range={"lower": 1, "upper": 9},
        result="stored",
    )

    line = read_log_line(tmp_path / "test.log")
    assert REQUIRED_FIELDS <= set(line)
    assert line["component"] == "test"
    assert line["event"] == "register_accepted"
    assert line["node"] == "node-a"
    assert line["username"] == "alice"
    assert line["result"] == "stored"


def test_log_level_and_directory_are_environment_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("FLOCK_LOG_LEVEL", "WARNING")
    logger = configure_logger("flock.test.env", "env.log")

    logger.info("hidden")
    logger.warning("visible")
    for handler in logger.handlers:
        handler.flush()

    lines = (tmp_path / "env.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "visible"


def test_configure_logger_uses_rotating_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOCK_LOG_DIR", str(tmp_path))
    logger = configure_logger("flock.test.rotation", "rotation.log")

    assert any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers)


def test_sensitive_values_are_redacted_and_long_payloads_are_summarized():
    cleaned = sanitize({
        "password": "secret",
        "token": "abc",
        "public_key": "pub",
        "safe": "x" * 240,
    })

    assert cleaned["password"] == "[redacted]"
    assert cleaned["token"] == "[redacted]"
    assert cleaned["public_key"] == "[redacted]"
    assert "[truncated" in cleaned["safe"]

    summary = summarize_command("REGISTER alice 127.0.0.1 5000 9 public signature")
    assert summary == {
        "command": "REGISTER",
        "payload": "[redacted]",
        "username": "alice",
        "version": "9",
    }
