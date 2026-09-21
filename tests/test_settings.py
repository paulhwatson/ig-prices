from __future__ import annotations

from pathlib import Path

import pytest

from prices.settings import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SYMBOLS_PATH,
    INTERVAL,
    load_config,
)
from prices.symbols import load_symbol_groups


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(body)
    return path


def test_the_shipped_config_loads():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.store.root.is_absolute()
    assert config.ig.backfill_days > 0
    assert config.ig.max_history_days > 0


def test_the_shipped_symbols_load_and_are_non_empty():
    groups = load_symbol_groups(DEFAULT_SYMBOLS_PATH)
    assert groups.enabled()
    for group in groups.groups.values():
        assert group.symbols, f"{group.name} has no symbols"


def test_everything_shipped_is_sourced_from_ig():
    """One vendor, one price scale - the whole point of dropping FMP."""
    groups = load_symbol_groups(DEFAULT_SYMBOLS_PATH)
    sources = {i.source for _, i in groups.all_instruments()}
    assert sources == {"ig"}


def test_the_oil_pair_legs_are_all_configured():
    """Brent, WTI and gasoline on one basis is what makes ../oil-pair
    modellable; FMP would not sell two of them at all."""
    groups = load_symbol_groups(DEFAULT_SYMBOLS_PATH)
    epics = {i.ig_epic for _, i in groups.all_instruments()}
    assert {"CC.D.LCO.USS.IP", "CC.D.CL.USS.IP", "CC.D.RB.USS.IP"} <= epics


def test_every_shipped_instrument_names_an_ig_epic():
    """The point of the config: nothing is collected unless someone has
    identified the IG market it corresponds to."""
    groups = load_symbol_groups(DEFAULT_SYMBOLS_PATH)
    for group_name, instrument in groups.all_instruments():
        assert instrument.ig_epic, f"{group_name}/{instrument.symbol} has no epic"


def test_missing_config_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        load_config(tmp_path / "absent.toml")


def test_defaults_apply_to_omitted_sections(tmp_path):
    config = load_config(write_config(tmp_path, '[store]\nroot = "/tmp/x"\n'))
    assert config.ig.chunk_days > 0
    assert config.update.overlap_days > 0


def test_store_root_expands_a_home_relative_path(tmp_path):
    config = load_config(write_config(tmp_path, '[store]\nroot = "~/somewhere"\n'))
    assert "~" not in str(config.store.root)
    assert config.store.root.is_absolute()


def test_an_unknown_key_is_rejected_rather_than_ignored(tmp_path):
    with pytest.raises(RuntimeError, match="malformed"):
        load_config(write_config(tmp_path, "[ig]\nnonsense = 1\n"))


@pytest.mark.parametrize(
    "body",
    [
        "[ig]\nchunk_days = 0\n",
        "[ig]\nmax_points_per_run = 0\n",
        "[update]\noverlap_days = -1\n",
    ],
)
def test_out_of_range_values_are_rejected(tmp_path, body):
    with pytest.raises(RuntimeError):
        load_config(write_config(tmp_path, body))


def test_the_interval_is_hourly():
    assert INTERVAL == "1hour"
