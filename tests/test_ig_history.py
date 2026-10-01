"""IG historical fetching: pagination, the two different 403s, and budgets."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from ig_prices.ig import AllowanceExceededError, IGClient, IGError, RateLimitedError
from ig_prices.settings import IGCredentials

CREDENTIALS = IGCredentials(username="u", password="p", api_key="k", acc_number="A1")
START = pd.Timestamp("2026-09-01")
END = pd.Timestamp("2026-09-08")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload)
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, gets=None):
        self._gets = list(gets or [])
        self.requests = []

    def post(self, url, json=None, headers=None, timeout=None):
        return FakeResponse(
            payload={"lightstreamerEndpoint": "https://stream.example"},
            headers={"CST": "c", "X-SECURITY-TOKEN": "t"},
        )

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params})
        return self._gets.pop(0) if self._gets else FakeResponse(payload=prices_page([]))


def bar(hour: int, close: float = 100.0):
    return {
        "snapshotTime": f"2026/09/01 {hour:02d}:00:00",
        "openPrice": {"bid": close - 1, "ask": close + 1},
        "highPrice": {"bid": close, "ask": close + 2},
        "lowPrice": {"bid": close - 2, "ask": close},
        "closePrice": {"bid": close - 1, "ask": close + 1},
        "lastTradedVolume": 10,
    }


def prices_page(bars, page=1, total_pages=1, remaining=9000):
    return {
        "prices": bars,
        "metadata": {
            "allowance": {
                "remainingAllowance": remaining, "totalAllowance": 10000,
                "allowanceExpiry": 604800,
            },
            "pageData": {"pageSize": 500, "pageNumber": page, "totalPages": total_pages},
        },
    }


def client(gets, **kw):
    session = FakeSession(gets)
    c = IGClient(CREDENTIALS, session=session, requests_per_minute=60_000, **kw)
    return c, session


def test_bars_are_carried_as_the_mid_of_bid_and_ask():
    c, _ = client([FakeResponse(payload=prices_page([bar(10, close=100.0)]))])

    bars = c.hourly_bars("E", START, END, chunk_days=7)

    assert bars.iloc[0]["close"] == pytest.approx(100.0)  # (99 + 101) / 2
    assert bars.iloc[0]["date"] == pd.Timestamp("2026-09-01 10:00:00")


def test_every_page_of_a_window_is_followed():
    """IG pages at 20 by default; stopping at page one silently returns a
    fifth of the window and looks like sparse data rather than a bug."""
    c, session = client([
        FakeResponse(payload=prices_page([bar(h) for h in range(3)], page=1, total_pages=3)),
        FakeResponse(payload=prices_page([bar(h) for h in range(3, 6)], page=2, total_pages=3)),
        FakeResponse(payload=prices_page([bar(h) for h in range(6, 9)], page=3, total_pages=3)),
    ])

    bars = c.hourly_bars("E", START, END, chunk_days=7)

    assert len(bars) == 9
    assert [r["params"]["pageNumber"] for r in session.requests] == [1, 2, 3]


def test_a_large_page_size_is_requested():
    c, session = client([FakeResponse(payload=prices_page([bar(1)]))])
    c.hourly_bars("E", START, END, chunk_days=7)
    assert session.requests[0]["params"]["pageSize"] > 20


def test_the_range_is_split_into_chunks():
    c, session = client([FakeResponse(payload=prices_page([bar(1)]))] * 4)
    c.hourly_bars("E", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-22"), chunk_days=7)
    assert len(session.requests) == 3


def test_the_run_budget_stops_further_chunks():
    """The budget is the whole run's, not one symbol's: four instruments draw
    on one weekly pool."""
    c, session = client(
        [FakeResponse(payload=prices_page([bar(h) for h in range(10)]))] * 5,
        run_budget=10,
    )

    c.hourly_bars("E", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-10-01"), chunk_days=7)

    assert len(session.requests) == 1  # first chunk spends the budget


def test_the_budget_carries_across_symbols():
    c, session = client(
        [FakeResponse(payload=prices_page([bar(h) for h in range(10)]))] * 6,
        run_budget=10,
    )

    c.hourly_bars("E1", START, END, chunk_days=7)
    before = len(session.requests)
    c.hourly_bars("E2", START, END, chunk_days=7)

    assert len(session.requests) == before  # nothing left for the second symbol


def test_a_rate_limit_is_retried_not_treated_as_a_spent_allowance(monkeypatch):
    """IG returns 403 for both; only the error code tells them apart, and
    giving up on a burst limit wastes a week of waiting."""
    monkeypatch.setattr("ig_prices.ig.time.sleep", lambda _: None)
    c, session = client([
        FakeResponse(status_code=403, text='{"errorCode":"error.public-api.exceeded-api-key-allowance"}'),
        FakeResponse(payload=prices_page([bar(1)])),
    ])

    bars = c.hourly_bars("E", START, END, chunk_days=7)

    assert len(bars) == 1
    assert len(session.requests) == 2


def test_a_spent_data_allowance_is_not_retried(monkeypatch):
    monkeypatch.setattr("ig_prices.ig.time.sleep", lambda _: None)
    c, session = client([
        FakeResponse(
            status_code=403,
            text='{"errorCode":"error.public-api.exceeded-account-historical-data-allowance"}',
        )
    ])

    with pytest.raises(AllowanceExceededError):
        c.hourly_bars("E", START, END, chunk_days=7)

    assert len(session.requests) == 1


def test_persistent_rate_limiting_eventually_gives_up(monkeypatch):
    monkeypatch.setattr("ig_prices.ig.time.sleep", lambda _: None)
    c, _ = client(
        [FakeResponse(status_code=403, text='{"errorCode":"error.public-api.exceeded-api-key-allowance"}')] * 10
    )

    with pytest.raises(RateLimitedError):
        c.hourly_bars("E", START, END, chunk_days=7)


def test_bars_already_paid_for_survive_a_limit(monkeypatch):
    """Discarding them would mean paying the allowance twice for the same data."""
    monkeypatch.setattr("ig_prices.ig.time.sleep", lambda _: None)
    c, _ = client([
        FakeResponse(payload=prices_page([bar(1), bar(2)])),
        FakeResponse(
            status_code=403,
            text='{"errorCode":"error.public-api.exceeded-account-historical-data-allowance"}',
        ),
    ])

    bars = c.hourly_bars("E", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-22"), chunk_days=7)

    assert len(bars) == 2


def test_the_allowance_is_reported_from_the_response():
    c, _ = client([FakeResponse(payload=prices_page([bar(1)], remaining=8407))])

    c.hourly_bars("E", START, END, chunk_days=7)

    assert c.allowance().remaining == 8407
    assert c.allowance().expiry_days == pytest.approx(7.0)


def test_a_min_reserve_stops_before_the_allowance_is_gone():
    c, _ = client([FakeResponse(payload=prices_page([bar(1)], remaining=100))] * 4)

    with pytest.raises(AllowanceExceededError, match="reserve"):
        c.hourly_bars(
            "E", pd.Timestamp("2026-09-01"), pd.Timestamp("2026-10-01"),
            chunk_days=7, min_reserve=500,
        )


def test_duplicate_timestamps_across_pages_are_dropped():
    c, _ = client([
        FakeResponse(payload=prices_page([bar(1)], page=1, total_pages=2)),
        FakeResponse(payload=prices_page([bar(1)], page=2, total_pages=2)),
    ])

    assert len(c.hourly_bars("E", START, END, chunk_days=7)) == 1


def test_an_unparseable_timestamp_is_dropped():
    broken = bar(1)
    broken["snapshotTime"] = "not a date"
    c, _ = client([FakeResponse(payload=prices_page([broken, bar(2)]))])

    assert len(c.hourly_bars("E", START, END, chunk_days=7)) == 1


def test_a_non_403_failure_raises():
    c, _ = client([FakeResponse(status_code=500, text="boom")])

    with pytest.raises(IGError, match="/prices failed"):
        c.hourly_bars("E", START, END, chunk_days=7)


def test_login_exposes_the_stream_endpoint_and_password():
    c, _ = client([])
    c.login()

    assert c.lightstreamer_endpoint == "https://stream.example"
    assert c.stream_password == "CST-c|XST-t"


def test_priming_reads_the_allowance_for_a_single_point():
    """Without it the first chunk of a run is fetched blind and the reserve is
    overshot by a whole window."""
    c, session = client([FakeResponse(payload=prices_page([bar(1)], remaining=8000))])

    allowance = c.prime_allowance("E")

    assert allowance.remaining == 8000
    assert session.requests[0]["params"]["max"] == 1
    assert c.points_spent == 1


def test_priming_that_fails_does_not_raise():
    c, _ = client([FakeResponse(status_code=500, text="boom")])
    assert c.prime_allowance("E") is None
