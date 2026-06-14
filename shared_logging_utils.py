import json
import logging
import os
import re
from datetime import datetime, timezone
from logging import Logger
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


DEFAULT_FIELDS = {
    "node": None,
    "event": "log",
    "correlation_id": None,
    "peer": None,
    "username": None,
    "version": None,
    "range": None,
    "result": None,
}
SENSITIVE_NAMES = ("password", "token", "signature", "private_key", "public_key", "secret", "key")
SENSITIVE_COMMANDS = {"REGISTER", "REPLIC", "TAKEOVER", "MESSAGE", "PUBKEY_RES"}
MAX_STRING_LENGTH = 180
DEFAULT_MAX_BYTES = 1 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 1


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def configured_log_dir() -> Path:
    return Path(os.environ.get("FLOCK_LOG_DIR", repo_root() / "logs")).expanduser().resolve()


def configured_level() -> int:
    level_name = os.environ.get("FLOCK_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_NAMES)


def _truncate(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return f"{value[:80]}...[truncated {len(value) - 120} chars]...{value[-40:]}"


def summarize_command(command: str | None) -> dict[str, Any]:
    if not command:
        return {"command": None}

    verb = command.split(" ", 1)[0]
    summary: dict[str, Any] = {"command": verb}
    parts = command.split(" ")

    if verb in SENSITIVE_COMMANDS:
        summary["payload"] = "[redacted]"
        if len(parts) > 1:
            summary["username"] = parts[1]
        if verb in {"REGISTER", "REPLIC", "TAKEOVER"} and len(parts) > 4:
            summary["version"] = parts[4]
        return summary

    if len(command) > MAX_STRING_LENGTH:
        summary["payload"] = _truncate(command)
    else:
        summary["payload"] = command
    return summary


def sanitize(value: Any, field_name: str | None = None) -> Any:
    if field_name and _is_sensitive_name(field_name):
        return "[redacted]"

    if isinstance(value, dict):
        return {str(key): sanitize(item, str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, str):
        if value.split(" ", 1)[0] in SENSITIVE_COMMANDS:
            return summarize_command(value)
        if re.search(r"(BEGIN .*PRIVATE KEY|password=|token=|signature=)", value, re.IGNORECASE):
            return "[redacted]"
        return _truncate(value)
    return value


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        flock_fields = getattr(record, "flock", {}) or {}
        component = getattr(record, "component", None) or record.name.replace("flock.", "")
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "component": component,
            **DEFAULT_FIELDS,
        }
        payload.update(sanitize(flock_fields))

        if "message" not in payload:
            payload["message"] = sanitize(record.getMessage())
        if record.exc_info:
            payload["exception"] = sanitize(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_logger(name: str, log_filename: str) -> Logger:
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(configured_level())
    logger.propagate = False

    logs_dir = configured_log_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = JsonLineFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(configured_level())
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        logs_dir / log_filename,
        maxBytes=int(os.environ.get("FLOCK_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)),
        backupCount=int(os.environ.get("FLOCK_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT)),
        encoding="utf-8",
    )
    file_handler.setLevel(configured_level())
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def log_event(logger: Logger, level: int | str, event: str, **fields: Any) -> None:
    numeric_level = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logger.log(numeric_level, event, extra={"flock": {"event": event, **fields}})
