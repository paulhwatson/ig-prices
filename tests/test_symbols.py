from __future__ import annotations

from pathlib import Path

import pytest

from ig_prices.symbols import load_symbol_groups


def write_symbols(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "symbols.toml"
    path.write_text(body)
    return path


OIL = '[groups.oil]\ninstruments = [{ symbol = "BZUSD", ig_epic = "CC.D.LCO.USS.IP" }]\n'


def test_groups_load_with_their_instruments(tmp_path):
    groups = load_symbol_groups(write_symbols(tmp_path, OIL))
    instrument = groups.get("oil").instruments[0]
    assert instrument.symbol == "BZUSD"
    assert instrument.ig_epic == "CC.D.LCO.USS.IP"


def test_symbols_property_gives_the_fmp_symbols(tmp_path):
    groups = load_symbol_groups(
        write_symbols(
            tmp_path,
            '[groups.fx]\ninstruments = [\n'
            '  { symbol = "EURUSD", ig_epic = "CS.D.EURUSD.TODAY.IP" },\n'
            '  { symbol = "GBPUSD", ig_epic = "CS.D.GBPUSD.TODAY.IP" },\n]\n',
        )
    )
    assert groups.get("fx").symbols == ("EURUSD", "GBPUSD")


def test_an_instrument_without_an_ig_epic_is_rejected(tmp_path):
    """The gate: no epic means nobody has checked it is streamable on IG."""
    with pytest.raises(RuntimeError, match="no ig_epic"):
        load_symbol_groups(
            write_symbols(tmp_path, '[groups.etfs]\ninstruments = [{ symbol = "SPY" }]\n')
        )


def test_an_empty_ig_epic_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="no ig_epic"):
        load_symbol_groups(
            write_symbols(
                tmp_path, '[groups.etfs]\ninstruments = [{ symbol = "SPY", ig_epic = "  " }]\n'
            )
        )


def test_two_symbols_on_one_epic_are_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="mapped to two symbols"):
        load_symbol_groups(
            write_symbols(
                tmp_path,
                '[groups.a]\ninstruments = [{ symbol = "ESUSD", ig_epic = "IX.D.SPTRD.DAILY.IP" }]\n'
                '[groups.b]\ninstruments = [{ symbol = "^GSPC", ig_epic = "IX.D.SPTRD.DAILY.IP" }]\n',
            )
        )


def test_duplicate_symbols_are_collapsed(tmp_path):
    groups = load_symbol_groups(
        write_symbols(
            tmp_path,
            '[groups.oil]\ninstruments = [\n'
            '  { symbol = "BZUSD", ig_epic = "CC.D.LCO.USS.IP" },\n'
            '  { symbol = "BZUSD", ig_epic = "CC.D.LCO.USS.IP" },\n]\n',
        )
    )
    assert groups.get("oil").symbols == ("BZUSD",)


def test_whitespace_is_trimmed(tmp_path):
    groups = load_symbol_groups(
        write_symbols(
            tmp_path,
            '[groups.oil]\ninstruments = [{ symbol = " BZUSD ", ig_epic = " CC.D.LCO.USS.IP " }]\n',
        )
    )
    instrument = groups.get("oil").instruments[0]
    assert (instrument.symbol, instrument.ig_epic) == ("BZUSD", "CC.D.LCO.USS.IP")


def test_an_unknown_instrument_key_is_rejected_rather_than_ignored(tmp_path):
    with pytest.raises(RuntimeError, match="unknown key"):
        load_symbol_groups(
            write_symbols(
                tmp_path,
                '[groups.oil]\ninstruments = ['
                '{ symbol = "BZUSD", ig_epic = "CC.D.LCO.USS.IP", noatoll = 1 }]\n',
            )
        )


def test_epic_for_finds_the_mapping(tmp_path):
    groups = load_symbol_groups(write_symbols(tmp_path, OIL))
    assert groups.get("oil").epic_for("BZUSD") == "CC.D.LCO.USS.IP"
    assert groups.get("oil").epic_for("NOPE") is None


def test_all_instruments_spans_every_group(tmp_path):
    groups = load_symbol_groups(
        write_symbols(
            tmp_path,
            OIL + '[groups.fx]\ninstruments = [{ symbol = "EURUSD", ig_epic = "CS.D.EURUSD.TODAY.IP" }]\n',
        )
    )
    assert [(g, i.symbol) for g, i in groups.all_instruments()] == [
        ("oil", "BZUSD"), ("fx", "EURUSD"),
    ]


def test_disabled_groups_are_excluded_from_a_default_run(tmp_path):
    groups = load_symbol_groups(
        write_symbols(
            tmp_path,
            OIL + '[groups.old]\nenabled = false\n'
            'instruments = [{ symbol = "SPY", ig_epic = "X.Y.Z" }]\n',
        )
    )
    assert [g.name for g in groups.enabled()] == ["oil"]
    assert groups.get("old").symbols == ("SPY",)  # still reachable via --group


def test_an_unknown_group_names_the_ones_that_exist(tmp_path):
    groups = load_symbol_groups(write_symbols(tmp_path, OIL))
    with pytest.raises(RuntimeError, match="oil"):
        groups.get("metals")


def test_a_group_with_no_instruments_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="no instruments"):
        load_symbol_groups(write_symbols(tmp_path, "[groups.oil]\ninstruments = []\n"))


def test_a_file_with_no_groups_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="no groups"):
        load_symbol_groups(write_symbols(tmp_path, "# nothing here\n"))


def test_a_malformed_instrument_list_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="not a table"):
        load_symbol_groups(write_symbols(tmp_path, "[groups.oil]\ninstruments = [1, 2]\n"))


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        load_symbol_groups(tmp_path / "absent.toml")
