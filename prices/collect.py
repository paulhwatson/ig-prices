"""Writes streamed candles into the parquet store, one symbol at a time.

Each completed candle is merged into its symbol's file rather than buffered,
so a collector killed at any moment loses at most the candle in flight. An
hourly candle arriving once an hour makes that cheap.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from prices import store
from prices.ig_stream import Candle
from prices.symbols import Instrument

logger = logging.getLogger(__name__)


class CandleWriter:
    def __init__(self, root: Path, instruments: list[tuple[str, Instrument]]):
        # Epic is what the stream reports; the store is keyed by symbol.
        self._by_epic = {
            instrument.ig_epic: (group, instrument) for group, instrument in instruments
        }
        self._root = root

    @property
    def epics(self) -> list[str]:
        return list(self._by_epic)

    def __call__(self, candle: Candle) -> None:
        target = self._by_epic.get(candle.epic)
        if target is None:
            logger.warning("candle for unconfigured epic %s - ignoring", candle.epic)
            return

        group, instrument = target
        path = store.price_path(self._root, group, instrument.symbol)

        fetched = store.clean(pd.DataFrame([candle.as_row()]))
        if fetched.empty:
            logger.warning("%s/%s: candle failed validation", group, instrument.symbol)
            return
        fetched["symbol"] = instrument.symbol
        fetched["source"] = instrument.source

        stored = store.read(path)
        combined = store.merge(stored, fetched)
        if combined.equals(stored):
            return

        store.write(path, combined)
        logger.info(
            "%s/%s: stored hourly candle %s close=%.4f (%d bar(s) total)",
            group, instrument.symbol, candle.timestamp, candle.close, len(combined),
        )
