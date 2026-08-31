"""Unit tests for the process-wide logging configuration."""

import json
import logging

import pytest
import structlog

from worker.logs import configure_logging


@pytest.fixture
def emit(capsys, monkeypatch):
    """Configure logging for a format, log one event, return the rendered line."""

    def _emit(log_format, event="Something happened.", **context):
        monkeypatch.setenv("RTD_LOG_FORMAT", log_format)
        configure_logging()
        structlog.get_logger("tests").info(event, **context)
        logging.getLogger().handlers[0].flush()
        return capsys.readouterr().out.strip()

    yield _emit
    structlog.reset_defaults()


def test_json_format_renders_a_parsable_object(emit):
    assert json.loads(emit("json", build_pk=42))["build_pk"] == 42


def test_json_format_renames_event_to_message(emit):
    # New Relic displays the ``message`` field as the log line.
    payload = json.loads(emit("json", event="Build complete."))
    assert payload["message"] == "Build complete."
    assert "event" not in payload


def test_json_format_includes_level_and_timestamp(emit):
    payload = json.loads(emit("json"))
    assert payload["level"] == "info"
    assert payload["timestamp"].endswith("Z")


def test_json_format_includes_the_traceback(capsys, monkeypatch):
    monkeypatch.setenv("RTD_LOG_FORMAT", "json")
    configure_logging()
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("tests").exception("Build failed.")
    payload = json.loads(capsys.readouterr().out.strip())
    assert "ValueError: boom" in payload["exception"]
    structlog.reset_defaults()


def test_console_format_is_not_json(emit):
    assert "Something happened." in emit("console")


def test_console_format_has_no_timestamp(emit):
    """Dev reads these under a compose prefix; the timestamp is just noise."""
    assert "timestamp" not in emit("console", build_pk=42)


def test_stdlib_loggers_are_rendered_too(capsys, monkeypatch):
    """Records from third-party loggers (slumber, urllib3) get the same shape."""
    monkeypatch.setenv("RTD_LOG_FORMAT", "json")
    configure_logging()
    logging.getLogger("urllib3").warning("Retrying.")
    assert json.loads(capsys.readouterr().out.strip())["message"] == "Retrying."
    structlog.reset_defaults()
