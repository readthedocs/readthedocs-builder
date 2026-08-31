"""
Logging for the whole process — the worker and the build it runs.

Configured once at import of ``worker.celery``; the runner emits through the
same root logger, so a build's lines and the worker's interleave in one stream.
"""

import logging.config
import os

import structlog
from structlog.processors import _figure_out_exc_info


SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.UnicodeDecoder(),
]


def json_format() -> bool:
    """Whether to render JSON instead of the dev console format."""
    return os.environ.get("RTD_LOG_FORMAT", "console") == "json"


def _configure_logging(pre_render_processors=None):
    """
    Point stdlib logging and ``structlog`` at a single renderer.

    ``pre_render_processors`` run inside the ``ProcessorFormatter`` while
    ``_record`` is still on the event dict — that's what the worker's New Relic
    processor needs.
    """
    if json_format():
        render = [
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # ``JSONRenderer`` won't format a traceback on its own the way
            # ``ConsoleRenderer`` does; without this ``log.exception()`` ships
            # a bare ``exc_info: true`` and the stack is lost.
            structlog.processors.format_exc_info,
            # New Relic displays the ``message`` field as the log line.
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ]
    else:
        # No timestamp: dev reads these interleaved under a compose prefix
        render = [structlog.dev.ConsoleRenderer()]

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structlog": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        *(pre_render_processors or []),
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        *render,
                    ],
                    # Give records from non-structlog loggers the same shape.
                    "foreign_pre_chain": SHARED_PROCESSORS,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "structlog",
                },
            },
            "root": {
                "handlers": ["console"],
                "level": os.environ.get("RTD_LOG_LEVEL", "INFO"),
            },
        }
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class NewRelicProcessor:
    """
    Add the fields New Relic uses to link a log line to its APM transaction.
    """

    def __call__(self, logger, method_name, event_dict):
        try:
            from newrelic.api.log import format_exc_info
            from newrelic.api.time_trace import get_linking_metadata
        except ImportError:
            # ``newrelic`` is the optional ``observability`` extra; dev omits it.
            return event_dict

        record = event_dict.get("_record")
        if record is None:
            return event_dict

        event_dict.update(get_linking_metadata())
        event_dict.update(
            {
                "thread.id": record.thread,
                "thread.name": record.threadName,
                "process.id": record.process,
                "process.name": record.processName,
                "file.name": record.pathname,
                "line.number": record.lineno,
            }
        )

        # structlog doesn't populate ``record.exc_info``, so fall back to the
        # event dict.
        exc_info = event_dict.pop("exc_info", None)
        if record.exc_info:
            event_dict.update(format_exc_info(record.exc_info))
        elif exc_info:
            event_dict.update(format_exc_info(_figure_out_exc_info(exc_info)))

        return event_dict


def configure_logging():
    _configure_logging(pre_render_processors=[NewRelicProcessor()])
