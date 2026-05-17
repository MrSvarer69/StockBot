"""Structured JSON logging. Append-only per-run files plus stderr for humans.

Every order request and broker response should be logged through here so the
audit trail is complete (risk-officer requirement).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k
            not in (
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "taskName",
                "message",
            )
        }
        if extras:
            payload["extra"] = extras
        return json.dumps(payload, default=str)


def setup_logging(
    *,
    log_dir: Path | str = "data/ops/logs",
    run_id: str | None = None,
    level: int = logging.INFO,
) -> Path:
    """Configure root logger. Returns the path to the run's JSONL file.

    Adds two handlers: a JSON file handler (append-only) and a stderr stream
    handler for human-readable progress. Safe to call once per process.
    """
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(log_dir) / f"{run_id}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    stream_handler.setLevel(level)
    root.addHandler(stream_handler)

    return log_path
