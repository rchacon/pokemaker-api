"""JSON logs, one object per line — easy to search in CloudWatch Logs Insights."""

from __future__ import annotations

import json
import logging

_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload |= {k: v for k, v in vars(record).items() if k not in _STANDARD}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("botocore", "aiobotocore", "httpcore", "httpcore2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
