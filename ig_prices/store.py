"""Parquet store for hourly bars.

The layout mirrors the existing 1min/daily stores under trading_data/prices, so
hourly data drops in beside them and anything that already reads that tree
finds it:

    <root>/<group>/1hour/<symbol>.parquet

Timestamps are naive and in IG's account-local time (Europe/London), which is
how IG stamps its REST bars; streamed candles are converted into the same zone
before storage. The neighbouring 1min and daily stores hold naive exchange-local
times from another tool, so anything reading across the whole tree has to
localise rather than assume one zone.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from ig_prices.settings import INTERVAL

logger = logging.getLogger(__name__)


class SourceMismatchError(RuntimeError):
    """Refusing to combine bars from two different feeds in one file.

    Two vendors quote the same instrument on different scales - gasoline is
    ~32778 on IG and was ~2.77 from FMP - so appending one to the other yields
    a series that looks plausible and is arithmetically meaningless. Switching
    a symbol's source is a deliberate act: delete the file, or store it under a
    different symbol.
    """

COLUMNS = ["open", "high", "low", "close", "volume", "symbol", "source"]
_NUMERIC = ["open", "high", "low", "close", "volume"]

# Symbols become filenames. Some carry '.' or '^' (^FTSE), both fine in a
# filename, but a separator or a parent reference is not.
_FORBIDDEN = ("/", "\\", "..", "\x00")


def price_path(root: Path, group: str, symbol: str) -> Path:
    _check_name(group, "group")
    _check_name(symbol, "symbol")
    return root / group / INTERVAL / f"{symbol}.parquet"


def read(path: Path) -> pd.DataFrame:
    """Stored bars, or an empty frame if nothing is stored yet.

    "No file" is the normal state for a symbol added to the config today, so it
    is not treated as an error.
    """
    if not path.exists():
        return empty()

    try:
        stored = pd.read_parquet(path)
    except Exception as exc:  # a truncated file from a killed run
        logger.warning("%s is unreadable (%s) - treating as empty", path, exc)
        return empty()

    if stored.empty:
        return empty()

    return clean(stored)


def write(path: Path, bars: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename, so an interrupted run cannot leave a half-written
    # parquet where a good one used to be.
    temp_path = path.with_suffix(".parquet.tmp")
    bars.to_parquet(temp_path)
    temp_path.replace(path)


def empty() -> pd.DataFrame:
    frame = pd.DataFrame(columns=COLUMNS)
    frame.index = pd.DatetimeIndex([], name="timestamp")
    return frame


def merge(stored: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    """Combine stored and freshly fetched bars, newest values winning.

    The most recent stored bar was usually still forming when it was first
    written, so on an overlap the fetched copy is the settled one and must
    replace what is there - not be discarded as a duplicate.

    Raises SourceMismatchError if the two sides came from different feeds.
    """
    _check_same_source(stored, fetched)
    # A file written before the source column existed carries none. Adopt the
    # incoming source rather than leave half the file unlabelled. This is why
    # _check_same_source runs first: it is a migration, not a merge of two
    # different feeds.
    stored = _adopt_source(stored, fetched)

    if stored.empty:
        combined = fetched
    elif fetched.empty:
        combined = stored
    else:
        combined = pd.concat([stored, fetched])

    if combined.empty:
        return empty()

    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def _adopt_source(stored: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if stored.empty or _sources(stored):
        return stored
    incoming = _sources(fetched)
    if len(incoming) != 1:
        return stored
    stored = stored.copy()
    stored["source"] = next(iter(incoming))
    return stored


def _check_same_source(stored: pd.DataFrame, fetched: pd.DataFrame) -> None:
    stored_sources = _sources(stored)
    fetched_sources = _sources(fetched)
    if not stored_sources or not fetched_sources:
        return
    if stored_sources != fetched_sources:
        raise SourceMismatchError(
            f"stored bars came from {sorted(stored_sources)} but the new bars came "
            f"from {sorted(fetched_sources)}; these are on different price scales "
            f"and must not be combined"
        )


def _sources(bars: pd.DataFrame) -> set[str]:
    if bars.empty or "source" not in bars.columns:
        return set()
    return {s for s in bars["source"].dropna().unique()}


def clean(bars: pd.DataFrame) -> pd.DataFrame:
    """Parse, type and sanity-check raw bars into the stored shape.

    The OHLC checks are the ones the market_data module applies to 1min data:
    A feed occasionally serves a bar whose high is below its close, and a bad
    bar is worse than a missing one for anything fitting a model on this.
    """
    if bars.empty:
        return empty()

    bars = bars.copy()

    if "date" in bars.columns:
        bars = bars.rename(columns={"date": "timestamp"})

    if "timestamp" in bars.columns:
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
        bars = bars.dropna(subset=["timestamp"])
        bars = bars.set_index("timestamp")
    elif isinstance(bars.index, pd.DatetimeIndex):
        bars.index.name = "timestamp"
    else:
        logger.warning("bars have no timestamp column or datetime index - discarding")
        return empty()

    if not isinstance(bars.index, pd.DatetimeIndex):
        return empty()

    missing = [column for column in ["open", "high", "low", "close"] if column not in bars.columns]
    if missing:
        logger.warning("bars are missing %s - discarding", ", ".join(missing))
        return empty()

    if "volume" not in bars.columns:
        bars["volume"] = 0.0

    for column in _NUMERIC:
        # float, not whatever pandas infers: volume arrives as an int for some
        # symbols and a float for others, and a column whose parquet dtype
        # changes from one write to the next breaks readers of the whole tree.
        bars[column] = pd.to_numeric(bars[column], errors="coerce").astype(float)

    bars = bars.dropna(subset=["open", "high", "low", "close"])
    bars["volume"] = bars["volume"].fillna(0.0)

    before = len(bars)
    bars = bars[
        (bars["high"] >= bars["low"])
        & (bars["high"] >= bars["close"])
        & (bars["high"] >= bars["open"])
        & (bars["low"] <= bars["close"])
        & (bars["low"] <= bars["open"])
    ]
    dropped = before - len(bars)
    if dropped:
        logger.warning("dropped %d bar(s) failing OHLC consistency checks", dropped)

    if "symbol" not in bars.columns:
        bars["symbol"] = pd.NA

    if "source" not in bars.columns:
        bars["source"] = pd.NA

    bars = bars[COLUMNS]
    bars.index.name = "timestamp"
    return bars.sort_index()


def _check_name(value: str, kind: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"empty {kind}")
    if any(token in value for token in _FORBIDDEN):
        raise ValueError(f"invalid {kind}: {value!r}")
