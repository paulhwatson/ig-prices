from __future__ import annotations

import pytest

from ig_prices import main as cli
from ig_prices.symbols import Instrument, SymbolGroup, SymbolGroups


def groups() -> SymbolGroups:
    return SymbolGroups(
        groups={
            "oil": SymbolGroup(name="oil", instruments=(
                Instrument("BZUSD", "CC.D.LCO.USS.IP"), Instrument("BNO", "TEST.BNO"))),
            "old": SymbolGroup(name="old", instruments=(
                Instrument("SPY", "TEST.SPY"),), enabled=False),
        }
    )


def parse(argv: list[str]):
    return cli._build_parser().parse_args(argv)


def test_no_subcommand_behaves_like_a_bare_update():
    args = parse([])
    assert args.command == "update"
    assert args.group is None
    assert args.symbol is None
    assert args.full is False


def test_a_default_run_covers_every_enabled_group():
    targets = cli._targets(groups(), parse(["update"]))
    assert [group.name for group, _ in targets] == ["oil"]
    assert targets[0][1] is None  # the whole group


def test_group_narrows_the_run_and_reaches_disabled_groups():
    targets = cli._targets(groups(), parse(["update", "--group", "old"]))
    assert [group.name for group, _ in targets] == ["old"]


def test_symbol_narrows_within_a_group():
    targets = cli._targets(groups(), parse(["update", "--group", "oil", "--symbol", "BNO"]))
    assert targets[0][1] == ["BNO"]


def test_symbol_is_repeatable():
    args = parse(["update", "--group", "oil", "--symbol", "BNO", "--symbol", "BZUSD"])
    assert cli._targets(groups(), args)[0][1] == ["BNO", "BZUSD"]


def test_symbol_without_group_is_rejected_as_ambiguous():
    with pytest.raises(RuntimeError, match="--symbol needs --group"):
        cli._targets(groups(), parse(["update", "--symbol", "BNO"]))


def test_an_unknown_group_is_rejected():
    with pytest.raises(RuntimeError, match="unknown group"):
        cli._targets(groups(), parse(["update", "--group", "nope"]))


def test_log_level_is_honoured_on_either_side_of_the_subcommand():
    """argparse writes a subparser's default over a value the parent already
    parsed unless the subparser uses SUPPRESS - this is that regression."""
    assert parse(["--log-level", "DEBUG", "update"]).log_level == "DEBUG"
    assert parse(["update", "--log-level", "DEBUG"]).log_level == "DEBUG"
    assert parse(["update"]).log_level == "INFO"
    assert parse([]).log_level == "INFO"


def test_show_rejects_an_unknown_group(config, caplog):
    result = cli._cmd_show(config, groups(), parse(["show", "--group", "nope", "--symbol", "SPY"]))
    assert result == 2


def test_show_reports_a_symbol_with_nothing_stored(config):
    args = parse(["show", "--group", "oil", "--symbol", "BZUSD"])
    assert cli._cmd_show(config, groups(), args) == 0


def test_backfill_accepts_the_same_narrowing_as_update():
    assert parse(["backfill"]).command == "backfill"
    assert parse(["backfill", "--group", "energy"]).group == "energy"
    assert parse(["backfill", "--group", "energy", "--symbol", "BZUSD"]).symbol == ["BZUSD"]
