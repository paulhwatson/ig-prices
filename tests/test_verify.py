from __future__ import annotations

from ig_prices.ig import MarketStatus
from ig_prices.symbols import Instrument, SymbolGroup, SymbolGroups
from ig_prices.verify import Verdict, verify


def status(epic, streamable=True, delay=0):
    return MarketStatus(
        epic=epic, streamable=streamable, delay_minutes=delay,
        market_status="TRADEABLE", name=epic, instrument_type="COMMODITIES",
    )


class FakeIG:
    def __init__(self, statuses):
        self._statuses = statuses
        self.asked = None

    def market_status(self, epics):
        self.asked = list(epics)
        return {e: s for e, s in self._statuses.items() if e in epics}


def groups(*pairs):
    return SymbolGroups(groups={
        "g": SymbolGroup(
            name="g", instruments=tuple(Instrument(sym, epic) for sym, epic in pairs)
        )
    })


def test_a_streamable_instrument_passes():
    results = verify(groups(("BZUSD", "CC.D.LCO.USS.IP")), FakeIG({"CC.D.LCO.USS.IP": status("CC.D.LCO.USS.IP")}))

    assert results[0].verdict is Verdict.STREAMABLE
    assert not results[0].is_problem


def test_a_delayed_market_is_flagged():
    """IG serves shares and ETFs at a 20-minute delay: exactly what this
    app is meant to keep out of the store."""
    ig = FakeIG({"SI.D.FXAUS.DAILY.IP": status("SI.D.FXAUS.DAILY.IP", streamable=False, delay=20)})

    results = verify(groups(("FXA", "SI.D.FXAUS.DAILY.IP")), ig)

    assert results[0].verdict is Verdict.NOT_STREAMABLE
    assert results[0].is_problem


def test_an_epic_ig_no_longer_knows_is_flagged():
    results = verify(groups(("OLD", "GONE.EPIC")), FakeIG({}))

    assert results[0].verdict is Verdict.UNKNOWN_EPIC
    assert results[0].is_problem


def test_every_configured_epic_is_checked():
    ig = FakeIG({})
    verify(groups(("A", "E.A"), ("B", "E.B")), ig)
    assert ig.asked == ["E.A", "E.B"]


def test_results_carry_the_group_and_symbol():
    ig = FakeIG({"E.A": status("E.A")})
    result = verify(groups(("A", "E.A")), ig)[0]
    assert (result.group, result.symbol, result.ig_epic) == ("g", "A", "E.A")
