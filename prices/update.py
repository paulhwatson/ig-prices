"""Incremental pulls: work out what each symbol is missing, fetch only that.

Everything comes from IG, whose history is metered - see prices/ig.py. So a run
is bounded by a budget as well as by dates, and stopping early with a partial
result is normal rather than a failure.

Two directions of travel:

- `update_group` tops a symbol up to now. Cheap, and mostly unnecessary once
  `prices stream` is running, since that fills forward for free.
- `backfill_group` extends a symbol further into the past, as much as the
  week's allowance permits. Run repeatedly, it deepens the history a slice at
  a time.

A run touches every configured symbol, so one bad symbol must not take the
rest of the run with it. Each symbol is isolated: its failure is recorded in
the summary and the run carries on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pandas as pd

from prices import store
from prices.ig import AllowanceExceededError, IGError
from prices.settings import Config
from prices.symbols import Instrument, SymbolGroup

logger = logging.getLogger(__name__)


class Status(str, Enum):
    UPDATED = "updated"
    UP_TO_DATE = "up_to_date"
    NO_DATA = "no_data"
    ALLOWANCE_SPENT = "allowance_spent"
    FAILED = "failed"


@dataclass(frozen=True)
class SymbolResult:
    group: str
    symbol: str
    status: Status
    rows_added: int = 0
    rows_total: int = 0
    last_timestamp: pd.Timestamp | None = None
    first_timestamp: pd.Timestamp | None = None
    error: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status is Status.FAILED


def update_symbol(
    client,
    config: Config,
    group: str,
    instrument: Instrument,
    since: pd.Timestamp | None = None,
    full: bool = False,
    now: pd.Timestamp | None = None,
) -> SymbolResult:
    """Bring one symbol up to date, fetching only what is missing."""
    now = now or pd.Timestamp.now()
    path = store.price_path(config.store.root, group, instrument.symbol)
    stored = store.read(path)

    start = _start_from(stored=stored, config=config, since=since, full=full, now=now)
    if start > now:
        return _unchanged(group, instrument.symbol, stored, Status.UP_TO_DATE)

    return _fetch_and_store(
        client=client, config=config, group=group, instrument=instrument,
        stored=stored, path=path, start=start, end=now, newest_first=False,
    )


def backfill_symbol(
    client,
    config: Config,
    group: str,
    instrument: Instrument,
    now: pd.Timestamp | None = None,
) -> SymbolResult:
    """Extend one symbol further back, as far as the budget allows.

    Fetches the windows immediately before what is already stored, newest
    first, so a budget that runs out mid-way leaves the series contiguous.
    """
    now = now or pd.Timestamp.now()
    path = store.price_path(config.store.root, group, instrument.symbol)
    stored = store.read(path)

    target = (now - pd.Timedelta(days=config.ig.max_history_days)).normalize()
    if stored.empty:
        # Nothing yet: an ordinary update seeds it before there is anything
        # to extend backwards from.
        return update_symbol(client, config, group, instrument, now=now)

    earliest = stored.index.min()
    if earliest <= target:
        return _unchanged(group, instrument.symbol, stored, Status.UP_TO_DATE)

    return _fetch_and_store(
        client=client, config=config, group=group, instrument=instrument,
        stored=stored, path=path, start=target, end=earliest, newest_first=True,
    )


def _fetch_and_store(
    client, config: Config, group: str, instrument: Instrument,
    stored: pd.DataFrame, path: Path,
    start: pd.Timestamp, end: pd.Timestamp, newest_first: bool,
) -> SymbolResult:
    symbol = instrument.symbol
    if client is None:
        return SymbolResult(
            group=group, symbol=symbol, status=Status.FAILED,
            rows_total=len(stored), last_timestamp=_last(stored),
            error="no IG client available - IG credentials are required",
        )

    try:
        fetched = client.hourly_bars(
            epic=instrument.ig_epic, start=start, end=end,
            chunk_days=config.ig.chunk_days,
            min_reserve=config.ig.min_allowance_reserve,
            newest_first=newest_first,
        )
    except AllowanceExceededError as exc:
        logger.warning("%s/%s: %s", group, symbol, exc)
        return _unchanged(group, symbol, stored, Status.ALLOWANCE_SPENT, error=str(exc))
    except IGError as exc:
        logger.error("%s/%s: %s", group, symbol, exc)
        return _unchanged(group, symbol, stored, Status.FAILED, error=str(exc))

    fetched = store.clean(fetched)
    if not fetched.empty:
        fetched["symbol"] = symbol
        fetched["source"] = instrument.source

    if fetched.empty and stored.empty:
        return SymbolResult(group=group, symbol=symbol, status=Status.NO_DATA)

    combined = store.merge(stored, fetched)
    if combined.equals(stored):
        return _unchanged(group, symbol, combined, Status.UP_TO_DATE)

    store.write(path, combined)
    remaining = getattr(getattr(client, "last_allowance", None), "remaining", None)
    logger.info(
        "%s/%s: +%d bar(s), %d stored, %s to %s%s",
        group, symbol, len(combined) - len(stored), len(combined),
        _first(combined), _last(combined),
        f", allowance left {remaining}" if remaining is not None else "",
    )
    return SymbolResult(
        group=group, symbol=symbol, status=Status.UPDATED,
        rows_added=len(combined) - len(stored), rows_total=len(combined),
        last_timestamp=_last(combined), first_timestamp=_first(combined),
    )


def update_group(
    client,
    config: Config,
    group: SymbolGroup,
    symbols: list[str] | None = None,
    since: pd.Timestamp | None = None,
    full: bool = False,
) -> list[SymbolResult]:
    return _run_group(
        group, symbols, "updating",
        lambda instrument: update_symbol(
            client, config, group.name, instrument, since=since, full=full
        ),
    )


def backfill_group(
    client, config: Config, group: SymbolGroup, symbols: list[str] | None = None
) -> list[SymbolResult]:
    return _run_group(
        group, symbols, "backfilling",
        lambda instrument: backfill_symbol(client, config, group.name, instrument),
    )


def _run_group(group: SymbolGroup, symbols, verb: str, run) -> list[SymbolResult]:
    wanted = set(symbols) if symbols else None
    targets = [i for i in group.instruments if wanted is None or i.symbol in wanted]
    results = []

    logger.info("%s group %s (%d symbol(s))", verb, group.name, len(targets))
    for instrument in targets:
        try:
            results.append(run(instrument))
        except Exception as exc:  # noqa: BLE001 - one symbol must not end the run
            logger.exception("%s/%s: unexpected failure", group.name, instrument.symbol)
            results.append(
                SymbolResult(
                    group=group.name, symbol=instrument.symbol,
                    status=Status.FAILED, error=str(exc),
                )
            )
    return results


def summarise(results: list[SymbolResult]) -> str:
    counts: dict[Status, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    bars = sum(result.rows_added for result in results)
    parts = [
        f"{status.value}={count}"
        for status, count in sorted(counts.items(), key=lambda kv: kv[0].value)
    ]
    return f"{len(results)} symbol(s): {', '.join(parts)}; {bars} new bar(s)"


def _start_from(
    stored: pd.DataFrame,
    config: Config,
    since: pd.Timestamp | None,
    full: bool,
    now: pd.Timestamp,
) -> pd.Timestamp:
    """Where to start fetching.

    An explicit --since always wins. Otherwise a symbol with stored data is
    topped up from just before its last bar (see store.merge for why the
    overlap is needed), and a fresh symbol reaches back backfill_days - not
    the full year, which for every configured symbol at once would be several
    times the weekly allowance.
    """
    if since is not None:
        return since.normalize()

    if full or stored.empty:
        days = min(config.ig.backfill_days, config.ig.max_history_days)
        return (now - pd.Timedelta(days=days)).normalize()

    return (stored.index.max() - pd.Timedelta(days=config.update.overlap_days)).normalize()


def _unchanged(group, symbol, stored, status, error="") -> SymbolResult:
    return SymbolResult(
        group=group, symbol=symbol, status=status, rows_total=len(stored),
        last_timestamp=_last(stored), first_timestamp=_first(stored), error=error,
    )


def _last(bars: pd.DataFrame) -> pd.Timestamp | None:
    return None if bars.empty else bars.index.max()


def _first(bars: pd.DataFrame) -> pd.Timestamp | None:
    return None if bars.empty else bars.index.min()
