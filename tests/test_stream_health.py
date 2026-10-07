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


# --- more instruments than one connection may carry -------------------------


def test_split_respects_the_per_connection_cap():
    epics = [f"E{i}" for i in range(48)]

    batches = ig_stream.split_epics(epics, max_per_connection=30)

    assert len(batches) == 2
    assert all(len(batch) <= 30 for batch in batches)
    assert sorted(e for batch in batches for e in batch) == sorted(epics)


def test_a_config_that_fits_one_connection_stays_on_one():
    epics = [f"E{i}" for i in range(30)]

    assert ig_stream.split_epics(epics, max_per_connection=30) == [epics]


def test_split_deals_round_robin_so_every_connection_gets_round_the_clock_fx():
    """In config order the FX pairs come last; dealt in order they'd all land
    on the final connection, leaving the others silent every night."""
    epics = [f"SOFT{i}" for i in range(38)] + [f"FX{i}" for i in range(10)]

    batches = ig_stream.split_epics(epics, max_per_connection=30)

    assert all(any(e.startswith("FX") for e in batch) for batch in batches)


def test_the_real_config_fits_under_the_cap():
    from ig_prices.symbols import load_symbol_groups

    epics = [i.ig_epic for _, i in load_symbol_groups().all_instruments()]
    batches = ig_stream.split_epics(epics)

    assert all(len(batch) <= ig_stream.MAX_ITEMS_PER_CONNECTION for batch in batches)


def test_a_stall_on_any_connection_is_reported_with_which_one(clock):
    streams = ig_stream.CandleStreams(
        client=None, epics=[f"E{i}" for i in range(4)], on_candle=lambda candle: None, max_per_connection=2,
    )
    for s in streams._streams:
        s._subscribed()
    assert streams.health_problem(clock(), WEEKDAY) is None

    clock.now += STALL_SECONDS + 1
    streams._streams[0]._handle("E0", {})  # only connection 1 hears anything

    problem = streams.health_problem(clock(), WEEKDAY)
    assert problem is not None and problem.startswith("connection 2 of 2")
