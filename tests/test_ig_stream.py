"""Candle parsing and storage for the live IG feed.

The critical rule: only a completed candle (CONS_END=1) is stored. IG
republishes the in-progress candle on every tick, so storing those would fill
the store with partial bars that disagree with the settled one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ig_prices import store
from ig_prices.collect import CandleWriter
from ig_prices.ig_stream import Candle, parse_candle
from ig_prices.symbols import Instrument

EPIC = "CC.D.RB.USS.IP"
# UTM is epoch-UTC milliseconds; 16:00 UTC is 17:00 in London.
UTM = str(int(pd.Timestamp("2026-09-21 16:00:00", tz="UTC").value // 1_000_000))


def values(cons_end="1", **overrides):
    v = {
        "BID_OPEN": "32778", "BID_HIGH": "32791", "BID_LOW": "32422", "BID_CLOSE": "32542",
        "OFR_OPEN": "32798", "OFR_HIGH": "32811", "OFR_LOW": "32442", "OFR_CLOSE": "32562",
        "LTV": "541", "CONS_END": cons_end, "UTM": UTM,
    }
    v.update(overrides)
    return v


def test_a_completed_candle_is_parsed_as_the_mid():
    candle = parse_candle(EPIC, values())

    assert candle is not None
    assert candle.open == pytest.approx((32778 + 32798) / 2)
    assert candle.close == pytest.approx((32542 + 32562) / 2)
    assert candle.volume == 541.0
    assert candle.timestamp == pd.Timestamp("2026-09-21 17:00:00")


def test_candles_are_stamped_in_igs_local_time_not_utc():
    """The bug this guards: IG's REST bars (which the backfill stores) are in
    the account's local time, while the stream sends epoch UTC. Storing the
    raw UTC would put streamed bars an hour away from backfilled ones in the
    same file."""
    assert parse_candle(EPIC, values()).timestamp == pd.Timestamp("2026-09-21 17:00:00")
    assert parse_candle(EPIC, values(), "UTC").timestamp == pd.Timestamp("2026-09-21 16:00:00")


def test_an_in_progress_candle_is_not_stored():
    """The whole point: IG republishes the forming candle on every tick."""
    assert parse_candle(EPIC, values(cons_end="0")) is None


def test_a_candle_missing_an_ohlc_component_is_not_stored():
    assert parse_candle(EPIC, values(BID_CLOSE=None, OFR_CLOSE=None)) is None


def test_a_candle_with_no_timestamp_is_not_stored():
    assert parse_candle(EPIC, values(UTM=None)) is None


def test_one_sided_quotes_fall_back_to_the_side_that_is_present():
    candle = parse_candle(EPIC, values(OFR_CLOSE=None))
    assert candle.close == pytest.approx(32542.0)


def test_a_missing_volume_is_zero_not_a_failure():
    assert parse_candle(EPIC, values(LTV=None)).volume == 0.0


# --- writing -----------------------------------------------------------


def instruments():
    return [("energy_ig", Instrument(symbol="RBUSD", ig_epic=EPIC, source="ig"))]


def candle(ts="2026-09-21 17:00:00", close=32552.0):
    return Candle(
        epic=EPIC, timestamp=pd.Timestamp(ts),
        open=close - 10, high=close + 10, low=close - 20, close=close, volume=541.0,
    )


def test_a_candle_is_written_to_its_symbols_file(tmp_path):
    writer = CandleWriter(tmp_path, instruments())

    writer(candle())

    stored = store.read(store.price_path(tmp_path, "energy_ig", "RBUSD"))
    assert len(stored) == 1
    assert stored["source"].iloc[0] == "ig"
    assert stored["symbol"].iloc[0] == "RBUSD"


def test_candles_accumulate_across_hours(tmp_path):
    writer = CandleWriter(tmp_path, instruments())

    writer(candle("2026-09-21 17:00:00"))
    writer(candle("2026-09-21 18:00:00"))

    stored = store.read(store.price_path(tmp_path, "energy_ig", "RBUSD"))
    assert len(stored) == 2
    assert stored.index.is_monotonic_increasing


def test_a_republished_candle_replaces_the_stored_one(tmp_path):
    writer = CandleWriter(tmp_path, instruments())

    writer(candle(close=32552.0))
    writer(candle(close=32600.0))  # same hour, settled values

    stored = store.read(store.price_path(tmp_path, "energy_ig", "RBUSD"))
    assert len(stored) == 1
    assert stored["close"].iloc[0] == pytest.approx(32600.0)


def test_a_candle_for_an_unconfigured_epic_is_ignored(tmp_path):
    writer = CandleWriter(tmp_path, instruments())

    writer(Candle(epic="X.Y.Z", timestamp=pd.Timestamp("2026-09-21 17:00:00"),
                  open=1, high=2, low=0.5, close=1.5, volume=1))

    assert not (tmp_path / "energy_ig").exists()


def test_the_writer_reports_the_epics_it_covers(tmp_path):
    assert CandleWriter(tmp_path, instruments()).epics == [EPIC]


def test_a_streamed_candle_will_not_overwrite_fmp_sourced_bars(tmp_path):
    """The scales differ by ~10,000x; combining them silently would be worse
    than failing."""
    path = store.price_path(tmp_path, "energy_ig", "RBUSD")
    existing = store.clean(pd.DataFrame([{
        "date": "2026-09-21 16:00:00", "open": 2.7, "high": 2.8,
        "low": 2.6, "close": 2.77, "volume": 1,
    }]))
    existing["symbol"] = "RBUSD"
    existing["source"] = "fmp"
    store.write(path, existing)

    writer = CandleWriter(tmp_path, instruments())
    with pytest.raises(store.SourceMismatchError):
        writer(candle())
