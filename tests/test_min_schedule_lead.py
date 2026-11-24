from datetime import datetime, timedelta, timezone

import pytest

from telegram_mcp import runtime


def _in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_default_has_no_minimum(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS", raising=False)
    dt, error = runtime.parse_schedule_date(_in(60))
    assert error is None and dt is not None


def test_too_close_is_rejected_and_names_the_limit(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS", "172800")
    dt, error = runtime.parse_schedule_date(_in(3600))
    assert dt is None
    assert "172800" in error and "TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS" in error


def test_far_enough_is_accepted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS", "172800")
    dt, error = runtime.parse_schedule_date(_in(172800 + 600))
    assert error is None and dt is not None


def test_unix_timestamp_is_checked_too(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS", "3600")
    soon = int((datetime.now(timezone.utc) + timedelta(seconds=600)).timestamp())
    dt, error = runtime.parse_schedule_date(soon)
    assert dt is None and "3600" in error


@pytest.mark.parametrize("bad", ["abc", "-5", "1.5"])
def test_malformed_value_fails_closed(monkeypatch, bad):
    monkeypatch.setenv("TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS", bad)
    dt, error = runtime.parse_schedule_date(_in(10**6))
    assert dt is None and "TELEGRAM_MIN_SCHEDULE_LEAD_SECONDS" in error
