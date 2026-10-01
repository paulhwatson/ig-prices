"""Config (config/*.toml) and credential (.env) loading.

Credentials are read from .env, falling back to ~/trading/.env: the IG demo
login already exists there for the market_data module, and copying a secret
into a second file is a good way to end up with one of them stale.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "ig-prices.toml"
DEFAULT_SYMBOLS_PATH = REPO_ROOT / "config" / "symbols.toml"

_FALLBACK_ENV_PATH = Path.home() / "trading" / ".env"

# The whole point of this app. IG also serves SECOND/1MINUTE/5MINUTE candles
# and finer REST resolutions, but the store layout and the schedule are both
# built around hourly bars, so it is a constant rather than a setting.
INTERVAL = "1hour"


@dataclass(frozen=True)
class StoreConfig:
    root: Path = Path.home() / "trading_data" / "prices"


@dataclass(frozen=True)
class IGConfig:
    # IG serves roughly a year of hourly history and no more - probed
    # 2026-09-21: one year back returns bars, two years back returns nothing.
    max_history_days: int = 365
    # How far back a fresh ig-sourced symbol reaches on its first run. Not the
    # same as max_history_days: a near-24h commodity is ~115 bars a week, so a
    # year for four instruments is ~24,000 points against a 10,000/week
    # allowance. This is sized so all four fit in one week, and repeat runs
    # with --since extend the history a quarter at a time.
    backfill_days: int = 90
    # One REST call per window. IG pages its response, so this only has to be
    # small enough to keep each response manageable.
    chunk_days: int = 7
    # Hard ceiling on data points a single run may draw from the weekly
    # allowance (10,000/week on this account). A backfill that would exceed
    # it stops and says so rather than silently spending the week's budget.
    max_points_per_run: int = 6000
    # Refuse to start a run when fewer than this many points remain, so a
    # scheduled job can never leave nothing for a manual backfill.
    min_allowance_reserve: int = 500
    requests_per_minute: int = 30
    timeout_seconds: int = 30
    # IG stamps REST bars in the account's own local time, not UTC: at 17:47
    # London the newest hourly bar is 17:00, not 16:00. The streaming feed
    # instead sends epoch-UTC milliseconds, so candles must be converted into
    # this zone before they are stored - otherwise streamed bars land an hour
    # away from backfilled ones in the same file.
    timezone: str = "Europe/London"


@dataclass(frozen=True)
class UpdateConfig:
    overlap_days: int = 2


@dataclass(frozen=True)
class Config:
    store: StoreConfig
    update: UpdateConfig
    ig: IGConfig


@dataclass(frozen=True)
class IGCredentials:
    username: str
    password: str
    api_key: str
    acc_number: str = ""
    acc_type: str = "demo"


# acc_number is only needed for the Lightstreamer feed, which uses it as the
# stream username; the REST endpoints do not ask for it.
_IG_ENV_VARS = ("IG_DEMO_USERNAME", "IG_DEMO_PASSWORD", "IG_DEMO_API_KEY")


def _load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if _FALLBACK_ENV_PATH.exists():
        # Does not override anything already set - .env and the real
        # environment both win over the fallback.
        load_dotenv(_FALLBACK_ENV_PATH)


def load_ig_credentials() -> IGCredentials:
    """IG demo credentials, for verifying streamability only.

    Demo rather than live: this only reads market metadata, which is identical
    on both, and a read-only job has no business holding live credentials.
    """
    _load_env()

    missing = [name for name in _IG_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing IG credentials: {', '.join(missing)}. These are only needed "
            f"for `ig-prices verify`; set them in .env or {_FALLBACK_ENV_PATH}."
        )

    return IGCredentials(
        username=os.environ["IG_DEMO_USERNAME"],
        password=os.environ["IG_DEMO_PASSWORD"],
        api_key=os.environ["IG_DEMO_API_KEY"],
        acc_number=os.environ.get("IG_DEMO_ACC_NUMBER", ""),
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        raise RuntimeError(f"{path} does not exist.")

    with path.open("rb") as f:
        data = tomllib.load(f)

    try:
        store_data = dict(data.get("store", {}))
        if "root" in store_data:
            store_data["root"] = Path(store_data["root"]).expanduser()

        store = StoreConfig(**store_data)
        update = UpdateConfig(**data.get("update", {}))
        ig = IGConfig(**data.get("ig", {}))
    except TypeError as exc:
        raise RuntimeError(f"{path} is malformed: {exc}") from exc

    if update.overlap_days < 0:
        raise RuntimeError(f"{path}: update.overlap_days must not be negative")

    if ig.chunk_days < 1:
        raise RuntimeError(f"{path}: ig.chunk_days must be at least 1")
    if ig.max_points_per_run < 1:
        raise RuntimeError(f"{path}: ig.max_points_per_run must be at least 1")

    return Config(store=store, update=update, ig=ig)
