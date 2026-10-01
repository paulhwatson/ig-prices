from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ig_prices.settings import Config, IGConfig, StoreConfig, UpdateConfig


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        store=StoreConfig(root=tmp_path / "store"),
        update=UpdateConfig(overlap_days=2),
        ig=IGConfig(
            max_history_days=365, backfill_days=30, chunk_days=7,
            max_points_per_run=1000, min_allowance_reserve=100,
        ),
    )


def bars(timestamps: list[str], close: float = 100.0, symbol: str = "TEST") -> pd.DataFrame:
    """Raw feed-shaped bars: a `date` column, newest first, string prices."""
    return pd.DataFrame(
        [
            {
                "date": timestamp,
                "open": close,
                "low": close - 1,
                "high": close + 1,
                "close": close,
                "volume": 1000,
            }
            for timestamp in timestamps
        ]
    )


class FakeClient:
    """Stands in for IGClient: returns canned bars, records what was asked."""

    def __init__(self, frames: list[pd.DataFrame] | None = None, error: Exception | None = None):
        self._frames = list(frames or [])
        self._error = error
        self.calls: list[dict] = []
        self.last_allowance = type("A", (), {"remaining": 9000})()
        self.points_spent = 0

    def hourly_bars(self, epic, start, end, **kwargs):
        self.calls.append({"epic": epic, "start": start, "end": end, **kwargs})
        if self._error:
            raise self._error
        if not self._frames:
            return pd.DataFrame()
        return self._frames.pop(0)
