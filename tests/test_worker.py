"""The long-polling worker's startup, which is now a single point of failure.

Promoting the worker from a cron to a resident process moved where a transient
Google outage hurts. A Scheduler run that died just meant "try again in ten
minutes"; a resident process that dies at boot hits Heroku's restart backoff,
which doubles up to twenty minutes - so one 429 from Sheets took the bot off
the air far longer than the outage that caused it. Observed live on
2026-08-14, minutes after the worker went in.
"""
import pytest

import worker


class Boom(Exception):
    pass


def test_startup_retries_a_transient_failure(monkeypatch):
    attempts = []
    slept = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise Boom("Sheets 429")
        return "services"

    monkeypatch.setattr(worker.time, 'sleep', slept.append)
    assert worker.connect_with_retry(flaky) == "services"
    assert len(attempts) == 3
    assert slept, 'a retry storm must be paced, not spun'


def test_startup_backoff_grows_but_is_capped(monkeypatch):
    slept = []
    calls = []

    def always_failing():
        calls.append(1)
        if len(calls) <= 12:
            raise Boom("still down")
        return "services"

    monkeypatch.setattr(worker.time, 'sleep', slept.append)
    worker.connect_with_retry(always_failing)
    assert slept == sorted(slept), 'backoff must not shrink'
    assert max(slept) <= worker.STARTUP_BACKOFF_MAX_SECONDS
    # Capped rather than unbounded: a doubling delay with no ceiling turns a
    # ten-minute outage into an hour of silence after it has ended.
    assert slept[-1] == worker.STARTUP_BACKOFF_MAX_SECONDS


def test_startup_never_gives_up(monkeypatch):
    # Deliberately no attempt limit. Exiting hands control to Heroku's restart
    # backoff, which is the very thing that made a 429 cost twenty minutes;
    # staying alive and retrying on our own schedule is strictly better.
    calls = []

    def fails_a_lot():
        calls.append(1)
        if len(calls) <= 50:
            raise Boom("long outage")
        return "up at last"

    monkeypatch.setattr(worker.time, 'sleep', lambda s: None)
    assert worker.connect_with_retry(fails_a_lot) == "up at last"


def test_startup_stops_on_a_configuration_error(monkeypatch):
    # A missing credential is not transient: retrying it forever would hide a
    # deploy mistake behind a dyno that looks alive but never works.
    def misconfigured():
        raise worker.FatalStartupError("GOOGLE_TOKEN_JSON is not set")

    monkeypatch.setattr(worker.time, 'sleep', lambda s: None)
    with pytest.raises(worker.FatalStartupError):
        worker.connect_with_retry(misconfigured)
