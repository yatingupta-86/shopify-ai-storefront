"""
Structured logging + Sentry error tracking for Mera Shelf.

Usage:
    from observability import get_logger, init_sentry
    log = get_logger(__name__)
    log.info("product.enriched", product_id=123, auto_publish=True)
"""

import logging
import os
import json
import time


class _JsonFormatter(logging.Formatter):
    """Formats every log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # Merge any extra kwargs passed via log.info("msg", extra={...})
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits structured JSON to stdout."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.DEBUG if os.environ.get("LOG_LEVEL") == "DEBUG" else logging.INFO)
    return logger


def init_sentry():
    """Initialise Sentry if SENTRY_DSN is set. Safe no-op if not."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        return
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=0.2,   # capture 20% of requests for performance tracing
        send_default_pii=False,    # don't send customer PII to Sentry
        environment=os.environ.get("ENVIRONMENT", "production"),
    )
