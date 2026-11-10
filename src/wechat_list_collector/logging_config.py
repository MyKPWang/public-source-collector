from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

from .config import Settings


LOGGER = logging.getLogger("public_source_collector")
LOGGER.addHandler(logging.NullHandler())


class JsonFormatter(logging.Formatter):
    """Emit small structured records without request parameters or credentials."""

    fields = (
        "run_id", "feed_id", "feed_name", "source_slug", "endpoint_key", "operation", "count", "status",
        "accepted_count", "rejected_count", "new_count", "updated_count", "failed_endpoint_count", "alert_count",
        "reason_code", "wait_seconds", "cooldown_until", "duration_ms", "error_message",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: Settings) -> logging.Logger:
    # The collector owns this named logger. Some embedded HTTP test clients and
    # host applications call logging.disable(), which would otherwise silently
    # suppress the audit log even when handlers are correctly configured.
    logging.disable(logging.NOTSET)
    logger = LOGGER
    logger.setLevel(getattr(logging, settings.log_level))
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
