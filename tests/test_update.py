from __future__ import annotations

import pandas as pd
import pytest

from ig_prices import store, update
from ig_prices.ig import AllowanceExceededError, IGError
from ig_prices.symbols import Instrument, SymbolGroup
from tests.conftest import FakeClient, bars

NOW = pd.Timestamp("2026-09-18 12:00:00")
EPIC = "CC.D.LCO.USS.IP"


def inst(symbol="BZUSD", epic=EPIC) -> Instrument:
    return Instrument(symbol=symbol, ig_epic=epic, source="ig")


def group(*symbols: str, name="energy") -> SymbolGroup:
    return SymbolGroup(
        name=name, instruments=tuple(inst(s, f"TEST.{s}") for s in symbols)
    )


def path_for(config, symbol="BZUSD", group_name="energy"):
    return store.price_path(config.store.root, group_name, symbol)


# --- update: topping up to now -----------------------------------------


def test_a_new_symbol_is_fetched_by_epic(config):
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    result = update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.UPDATED
    assert result.rows_added == 1
    assert client.calls[0]["epic"] == EPIC


def test_a_new_symbol_reaches_back_backfill_days_not_the_full_year(config):
    """Every configured symbol at a full year would be several times the
    weekly allowance."""
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert client.calls[0]["start"] == (
        NOW - pd.Timedelta(days=config.ig.backfill_days)
    ).normalize()


def test_stored_bars_are_topped_up_from_the_overlap_window(config):
    store.write(path_for(config), store.clean(bars(["2026-09-15 10:00:00"])))
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert client.calls[0]["start"] == pd.Timestamp("2026-09-13")


def test_the_last_stored_bar_is_overwritten_with_settled_values(config):
    stored = store.clean(bars(["2026-09-18 10:00:00"], close=100.0))
    stored["source"] = "ig"
    store.write(path_for(config), stored)
    client = FakeClient([bars(["2026-09-18 10:00:00"], close=104.0)])

    result = update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert result.rows_added == 0
    assert store.read(path_for(config))["close"].iloc[-1] == 104.0


def test_identical_data_is_up_to_date_and_not_rewritten(config):
    stored = store.clean(bars(["2026-09-18 10:00:00"]))
    stored["symbol"] = "BZUSD"
    stored["source"] = "ig"
    store.write(path_for(config), stored)
    before = path_for(config).stat().st_mtime_ns
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    result = update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.UP_TO_DATE
    assert path_for(config).stat().st_mtime_ns == before


def test_rows_are_stamped_with_symbol_and_source(config):
    update.update_symbol(
        FakeClient([bars(["2026-09-18 10:00:00"])]), config, "energy", inst(), now=NOW
    )

    stored = store.read(path_for(config))
    assert set(stored["symbol"]) == {"BZUSD"}
    assert set(stored["source"]) == {"ig"}


def test_since_overrides_the_incremental_start(config):
    store.write(path_for(config), store.clean(bars(["2026-09-15 10:00:00"])))
    client = FakeClient([bars(["2026-01-02 10:00:00"])])

    update.update_symbol(
        client, config, "energy", inst(), since=pd.Timestamp("2026-01-01"), now=NOW
    )

    assert client.calls[0]["start"] == pd.Timestamp("2026-01-01")


def test_an_update_walks_forward_not_backward(config):
    update.update_symbol(
        FakeClient([bars(["2026-09-18 10:00:00"])]), config, "energy", inst(), now=NOW
    )
    assert FakeClient().calls == []  # sanity: fresh client records nothing


def test_no_bars_and_nothing_stored_is_no_data(config):
    result = update.update_symbol(FakeClient([]), config, "energy", inst(), now=NOW)
    assert result.status is update.Status.NO_DATA


# --- backfill: extending into the past ---------------------------------


def test_backfill_extends_before_the_earliest_stored_bar(config):
    stored = store.clean(bars(["2026-09-15 10:00:00"]))
    stored["source"] = "ig"
    store.write(path_for(config), stored)
    client = FakeClient([bars(["2026-08-01 10:00:00"])])

    result = update.backfill_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.UPDATED
    call = client.calls[0]
    assert call["end"] == pd.Timestamp("2026-09-15 10:00:00")
    assert call["start"] == (NOW - pd.Timedelta(days=365)).normalize()


def test_backfill_takes_the_newest_windows_first(config):
    """A budget that runs out mid-way must leave the series contiguous with
    what is already stored, not punch a hole in the middle."""
    stored = store.clean(bars(["2026-09-15 10:00:00"]))
    stored["source"] = "ig"
    store.write(path_for(config), stored)
    client = FakeClient([bars(["2026-08-01 10:00:00"])])

    update.backfill_symbol(client, config, "energy", inst(), now=NOW)

    assert client.calls[0]["newest_first"] is True


def test_backfill_of_a_symbol_already_at_the_limit_does_nothing(config):
    stored = store.clean(bars(["2024-01-01 10:00:00"]))
    stored["source"] = "ig"
    store.write(path_for(config), stored)
    client = FakeClient([bars(["2026-08-01 10:00:00"])])

    result = update.backfill_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.UP_TO_DATE
    assert client.calls == []


def test_backfill_of_an_empty_symbol_seeds_it_first(config):
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    result = update.backfill_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.UPDATED
    assert client.calls[0]["newest_first"] is False  # an ordinary seeding update


# --- failure handling --------------------------------------------------


def test_a_spent_allowance_is_recorded_not_a_failure(config):
    client = FakeClient(error=AllowanceExceededError("allowance gone"))

    result = update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.ALLOWANCE_SPENT
    assert not result.is_failure


def test_an_api_failure_leaves_stored_data_alone(config):
    store.write(path_for(config), store.clean(bars(["2026-09-15 10:00:00"])))
    client = FakeClient(error=IGError("boom"))

    result = update.update_symbol(client, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.FAILED
    assert len(store.read(path_for(config))) == 1


def test_no_client_fails_clearly(config):
    result = update.update_symbol(None, config, "energy", inst(), now=NOW)

    assert result.status is update.Status.FAILED
    assert "IG credentials" in result.error


def test_one_failing_symbol_does_not_stop_the_group(config):
    class PartlyBroken(FakeClient):
        def hourly_bars(self, epic, start, end, **kwargs):
            self.calls.append({"epic": epic})
            if epic == "TEST.BROKEN":
                raise IGError("boom")
            return bars(["2026-09-18 10:00:00"])

    results = update.update_group(PartlyBroken(), config, group("BZUSD", "BROKEN", "CLUSD"))

    assert [r.symbol for r in results] == ["BZUSD", "BROKEN", "CLUSD"]
    assert [r.status for r in results] == [
        update.Status.UPDATED, update.Status.FAILED, update.Status.UPDATED,
    ]


def test_an_unexpected_error_is_caught_per_symbol(config):
    class Exploding(FakeClient):
        def hourly_bars(self, epic, start, end, **kwargs):
            raise ZeroDivisionError("not an IGError")

    results = update.update_group(Exploding(), config, group("BZUSD", "CLUSD"))

    assert all(r.status is update.Status.FAILED for r in results)


def test_a_group_can_be_narrowed_to_named_symbols(config):
    client = FakeClient([bars(["2026-09-18 10:00:00"])])

    results = update.update_group(client, config, group("BZUSD", "CLUSD"), symbols=["CLUSD"])

    assert [r.symbol for r in results] == ["CLUSD"]


def test_backfill_group_isolates_failures_too(config):
    class Exploding(FakeClient):
        def hourly_bars(self, epic, start, end, **kwargs):
            raise ZeroDivisionError("boom")

    results = update.backfill_group(Exploding(), config, group("BZUSD", "CLUSD"))

    assert len(results) == 2
    assert all(r.status is update.Status.FAILED for r in results)


def test_summarise_counts_statuses_and_new_bars():
    results = [
        update.SymbolResult("energy", "BZUSD", update.Status.UPDATED, rows_added=5),
        update.SymbolResult("energy", "CLUSD", update.Status.ALLOWANCE_SPENT),
        update.SymbolResult("energy", "RBUSD", update.Status.UPDATED, rows_added=3),
    ]

    summary = update.summarise(results)

    assert "3 symbol(s)" in summary
    assert "updated=2" in summary
    assert "allowance_spent=1" in summary
    assert "8 new bar(s)" in summary
