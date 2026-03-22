import logging
from logging import Logger
from pathlib import Path


LOG_FORMAT = "[%(asctime)s] %(levelname)s:%(name)s: %(message)s"


def configure_logger(name: str, log_filename: str) -> Logger:
    """Build a logger that writes to both stdout and a file under the repo logs directory."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    logs_dir = Path(__file__).resolve().parents[1] / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(logs_dir / log_filename, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger
