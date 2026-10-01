from __future__ import annotations

import pandas as pd
import pytest

from ig_prices import store
from tests.conftest import bars


def test_price_path_matches_the_existing_store_layout(tmp_path):
    path = store.price_path(tmp_path, "oil", "BZUSD")
    assert path == tmp_path / "oil" / "1hour" / "BZUSD.parquet"


@pytest.mark.parametrize("symbol", ["../escape", "a/b", "", "   "])
def test_price_path_rejects_symbols_that_would_escape_the_store(tmp_path, symbol):
    with pytest.raises(ValueError):
        store.price_path(tmp_path, "oil", symbol)


def test_price_path_allows_fmp_punctuation(tmp_path):
    assert store.price_path(tmp_path, "sp500", "BRK.B").name == "BRK.B.parquet"
    assert store.price_path(tmp_path, "indexes", "^GSPC").name == "^GSPC.parquet"


def test_read_missing_file_is_empty_not_an_error(tmp_path):
    assert store.read(tmp_path / "nothing.parquet").empty


def test_round_trip(tmp_path):
    path = store.price_path(tmp_path, "oil", "BZUSD")
    cleaned = store.clean(bars(["2026-09-18 10:00:00", "2026-09-18 09:00:00"]))
    cleaned["symbol"] = "BZUSD"

    store.write(path, cleaned)
    reloaded = store.read(path)

    assert len(reloaded) == 2
    assert list(reloaded.columns) == store.COLUMNS
    assert reloaded.index.name == "timestamp"
    assert reloaded.index.is_monotonic_increasing


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = store.price_path(tmp_path, "oil", "BZUSD")
    store.write(path, store.clean(bars(["2026-09-18 10:00:00"])))
    assert list(path.parent.iterdir()) == [path]


def test_clean_sorts_oldest_first_and_types_numerics():
    cleaned = store.clean(bars(["2026-09-18 11:00:00", "2026-09-18 09:00:00"]))
    assert cleaned.index.is_monotonic_increasing
    assert cleaned["close"].dtype == float
    assert cleaned["volume"].dtype == float


def test_clean_drops_inconsistent_ohlc_bars():
    raw = bars(["2026-09-18 09:00:00", "2026-09-18 10:00:00"])
    raw.loc[0, "high"] = 1.0  # high below low/open/close

    cleaned = store.clean(raw)

    assert len(cleaned) == 1
    assert cleaned.index[0] == pd.Timestamp("2026-09-18 10:00:00")


def test_clean_drops_unparseable_timestamps():
    raw = bars(["2026-09-18 09:00:00", "not-a-date"])
    assert len(store.clean(raw)) == 1


def test_clean_handles_a_missing_volume_column():
    raw = bars(["2026-09-18 09:00:00"]).drop(columns=["volume"])
    assert store.clean(raw)["volume"].iloc[0] == 0.0


def test_clean_of_empty_input_has_the_stored_shape():
    cleaned = store.clean(pd.DataFrame())
    assert cleaned.empty
    assert list(cleaned.columns) == store.COLUMNS


def test_merge_prefers_fetched_values_for_a_bar_that_was_still_forming():
    stored = store.clean(bars(["2026-09-18 09:00:00"], close=100.0))
    fetched = store.clean(bars(["2026-09-18 09:00:00"], close=105.0))

    merged = store.merge(stored, fetched)

    assert len(merged) == 1
    assert merged["close"].iloc[0] == 105.0


def test_merge_keeps_both_sides_and_sorts():
    stored = store.clean(bars(["2026-09-18 09:00:00"]))
    fetched = store.clean(bars(["2026-09-18 11:00:00", "2026-09-18 10:00:00"]))

    merged = store.merge(stored, fetched)

    assert len(merged) == 3
    assert merged.index.is_monotonic_increasing


def test_merge_with_empty_sides():
    stored = store.clean(bars(["2026-09-18 09:00:00"]))
    assert len(store.merge(stored, store.empty())) == 1
    assert len(store.merge(store.empty(), stored)) == 1
    assert store.merge(store.empty(), store.empty()).empty
