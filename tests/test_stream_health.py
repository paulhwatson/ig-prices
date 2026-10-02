"""The stream's watchdog. On 2026-10-02 the feed kept cycling unsubscribe and
resubscribe while no data arrived for four hours, and the process sat there
alive and useless - launchd only restarts it if it exits."""

from __future__ import annotations

import pandas as pd
import pytest

from ig_prices import ig_stream
from ig_prices.ig_stream import (
    RESUBSCRIBE_GRACE_SECONDS,
    STALL_SECONDS,
    HourlyCandleStream,
    StreamStalled,
    markets_expected_open,
)

# A Wednesday afternoon and a Saturday, London time.
WEEKDAY = pd.Timestamp("2026-10-07 15:00", tz="Europe/London")
SATURDAY = pd.Timestamp("2026-10-10 15:00", tz="Europe/London")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ig_stream.time, "monotonic", c)
    return c


@pytest.fixture
def stream(clock):
    s = HourlyCandleStream(client=None, epics=["CC.D.LCO.USS.IP"], on_candle=lambda candle: None)
    s._subscribed()
    return s


def tick(stream):
    stream._handle("CC.D.LCO.USS.IP", {"CONS_END": "0"})


def test_a_live_subscribed_feed_is_healthy(stream, clock):
    clock.now += STALL_SECONDS - 1
    tick(stream)
    clock.now += STALL_SECONDS - 1
    assert stream.health_problem(clock.now, WEEKDAY) is None


def test_a_brief_unsubscribe_that_is_restored_is_healthy(stream, clock):
    stream._unsubscribed()
    clock.now += 10
    stream._subscribed()
    clock.now += RESUBSCRIBE_GRACE_SECONDS + 1
    tick(stream)
    assert stream.health_problem(clock.now, WEEKDAY) is None


def test_a_subscription_lost_past_the_grace_period_is_a_stall(stream, clock):
    stream._unsubscribed()
    clock.now += RESUBSCRIBE_GRACE_SECONDS + 1
    tick(stream)  # even with data still arriving, the subscription is gone
    assert "subscription lost" in stream.health_problem(clock.now, WEEKDAY)


def test_a_subscription_that_never_lands_is_a_stall(clock):
    s = HourlyCandleStream(client=None, epics=["CC.D.LCO.USS.IP"], on_candle=lambda candle: None)
    clock.now += RESUBSCRIBE_GRACE_SECONDS + 1
    assert "subscription lost" in s.health_problem(clock.now, WEEKDAY)


def test_silence_while_markets_are_open_is_a_stall(stream, clock):
    # The 2026-10-02 failure: subscribed, but nothing arriving.
    clock.now += STALL_SECONDS + 1
    assert "no price updates" in stream.health_problem(clock.now, WEEKDAY)


def test_silence_over_the_weekend_is_not_a_stall(stream, clock):
    clock.now += 24 * 3600
    assert stream.health_problem(clock.now, SATURDAY) is None


@pytest.mark.parametrize(
    "when, is_open",
    [
        ("2026-10-09 21:59", True),   # Friday, before FX closes
        ("2026-10-09 22:00", False),  # Friday close
        ("2026-10-10 12:00", False),  # Saturday
        ("2026-10-11 22:30", False),  # Sunday, FX reopening - first ticks still arriving
        ("2026-10-11 23:00", True),   # Sunday, margin over
        ("2026-10-12 03:00", True),   # Monday overnight
    ],
)
def test_markets_expected_open(when, is_open):
    assert markets_expected_open(pd.Timestamp(when, tz="Europe/London")) is is_open


def test_wait_raises_when_the_feed_stalls(stream, clock, monkeypatch):
    clock.now += STALL_SECONDS + 1
    monkeypatch.setattr(ig_stream, "HEALTH_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(ig_stream, "markets_expected_open", lambda now: True)
    with pytest.raises(StreamStalled):
        stream.wait(timeout=5)


def test_wait_returns_when_stopped(stream, monkeypatch):
    monkeypatch.setattr(ig_stream, "HEALTH_CHECK_INTERVAL_SECONDS", 0.01)
    stream._stop.set()
    stream.wait()
