"""Live hourly candles from IG's Lightstreamer feed.

The REST backfill in ig_prices/ig.py draws on a weekly allowance; this does not.
CHART:<epic>:HOUR pushes a consolidated hourly candle, so once a symbol is
backfilled this keeps it current for free, indefinitely.

Only candles marked CONS_END=1 are stored. IG republishes the in-progress
candle on every tick, and writing those would fill the store with partial
bars that disagree with the settled one - the store would rather have a gap
than a bar that was never real.

Not MARKET:<epic>, which IG rejects outright as of 2026-09-21 (Lightstreamer
error 21), and not trading_ig: authentication is three headers and the
lightstreamerEndpoint the login already returns (see IGClient).

The feed can die without the process noticing: IG drops the subscription
every few minutes and the client usually resubscribes on its own, but on
2026-10-02 it carried on cycling while no data arrived for four hours. So
wait() watches two things - the subscription staying lost, and prices going
quiet while markets are open - and raises StreamStalled, so the process exits
and launchd restarts it with a fresh login.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import pandas as pd
from lightstreamer.client import LightstreamerClient, Subscription, SubscriptionListener

from ig_prices.ig import IGClient, IGError

logger = logging.getLogger(__name__)

# Bid and ask OHLC, the consolidation marker, and the candle's start time.
STREAM_FIELDS = [
    "BID_OPEN", "BID_HIGH", "BID_LOW", "BID_CLOSE",
    "OFR_OPEN", "OFR_HIGH", "OFR_LOW", "OFR_CLOSE",
    "LTV", "CONS_END", "UTM",
]


@dataclass(frozen=True)
class Candle:
    epic: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_row(self) -> dict:
        return {
            "date": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def _mid(bid: str | None, ask: str | None) -> float | None:
    """Mid of the two sides, matching how the REST backfill stores bars."""
    if bid is None and ask is None:
        return None
    if bid is None:
        return float(ask)
    if ask is None:
        return float(bid)
    return (float(bid) + float(ask)) / 2


DEFAULT_TIMEZONE = "Europe/London"

# How long the subscription may stay lost before giving up on the client
# restoring it. Normal drops are restored within seconds.
RESUBSCRIBE_GRACE_SECONDS = 5 * 60
# In-progress candles are republished on every tick, so with ~30 instruments
# open a quarter of an hour of silence means the feed is dead, not quiet.
STALL_SECONDS = 15 * 60
HEALTH_CHECK_INTERVAL_SECONDS = 30


class StreamStalled(IGError):
    """The feed stopped delivering and was not going to recover by itself."""


def markets_expected_open(now: pd.Timestamp) -> bool:
    """False over the weekend close, when silence is normal.

    `now` is in IG's local time. FX, the last to close and first to open,
    trades 22:00 Sunday to 22:00 Friday London time; an hour's margin at the
    reopen gives the first ticks time to arrive.
    """
    weekday, hour = now.weekday(), now.hour
    if weekday == 4:  # Friday
        return hour < 22
    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday
        return hour >= 23
    return True


def parse_candle(
    epic: str, values: dict[str, str | None], timezone: str = DEFAULT_TIMEZONE
) -> Candle | None:
    """A completed candle, or None if this update is not one.

    Returns None for an in-progress candle (CONS_END != 1) and for one missing
    any OHLC component, rather than storing a partial bar.

    UTM is epoch-UTC milliseconds, but IG's REST bars - which the backfill
    stores - are stamped in the account's local time. The candle is converted
    into that zone before the tz is dropped, so streamed and backfilled bars
    for the same hour share a timestamp instead of sitting an hour apart.
    """
    if str(values.get("CONS_END") or "0") != "1":
        return None

    opens = _mid(values.get("BID_OPEN"), values.get("OFR_OPEN"))
    highs = _mid(values.get("BID_HIGH"), values.get("OFR_HIGH"))
    lows = _mid(values.get("BID_LOW"), values.get("OFR_LOW"))
    closes = _mid(values.get("BID_CLOSE"), values.get("OFR_CLOSE"))
    if None in (opens, highs, lows, closes):
        return None

    utm = values.get("UTM")
    if utm is None:
        return None

    started_at = pd.Timestamp(int(utm), unit="ms", tz="UTC").tz_convert(timezone)

    return Candle(
        epic=epic,
        timestamp=started_at.tz_localize(None),
        open=opens, high=highs, low=lows, close=closes,
        volume=float(values.get("LTV") or 0),
    )


class HourlyCandleStream:
    """Subscribes to hourly candles and hands each completed one to a sink."""

    def __init__(
        self, client: IGClient, epics: list[str], on_candle, timezone: str = DEFAULT_TIMEZONE
    ):
        self._client = client
        self._epics = list(epics)
        self._on_candle = on_candle
        self._timezone = timezone
        self._ls: LightstreamerClient | None = None
        self._stop = threading.Event()
        # Monotonic times, written from Lightstreamer's threads. Not yet
        # subscribed counts as lost, so a subscription that never lands is
        # caught by the same grace period.
        self._last_update_at = time.monotonic()
        self._unsubscribed_at: float | None = time.monotonic()

    def start(self) -> None:
        if self._client.lightstreamer_endpoint is None:
            self._client.login()
        endpoint = self._client.lightstreamer_endpoint
        if not endpoint:
            raise IGError("IG login did not return a lightstreamerEndpoint")
        if not self._client._credentials.acc_number:
            raise IGError(
                "IG_DEMO_ACC_NUMBER is not set - the Lightstreamer feed uses it "
                "as the stream username"
            )

        self._ls = LightstreamerClient(endpoint, None)
        self._ls.connectionDetails.setUser(self._client._credentials.acc_number)
        self._ls.connectionDetails.setPassword(self._client.stream_password)
        self._ls.connect()

        subscription = Subscription(
            mode="MERGE",
            items=[f"CHART:{epic}:HOUR" for epic in self._epics],
            fields=STREAM_FIELDS,
        )
        subscription.addListener(_CandleListener(self))
        self._ls.subscribe(subscription)
        logger.info("subscribed to hourly candles: %s", ", ".join(self._epics))

    def wait(self, timeout: float | None = None) -> None:
        """Blocks until stop(), or the timeout; raises StreamStalled if the
        feed dies first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            interval = HEALTH_CHECK_INTERVAL_SECONDS if remaining is None else min(remaining, HEALTH_CHECK_INTERVAL_SECONDS)
            if self._stop.wait(interval):
                return
            problem = self.health_problem(time.monotonic(), pd.Timestamp.now(tz=self._timezone))
            if problem is not None:
                raise StreamStalled(problem)

    def health_problem(self, now: float, wall_now: pd.Timestamp) -> str | None:
        """Why the feed should be restarted, or None if it looks alive."""
        unsubscribed_at = self._unsubscribed_at
        if unsubscribed_at is not None and now - unsubscribed_at > RESUBSCRIBE_GRACE_SECONDS:
            return f"subscription lost for {now - unsubscribed_at:.0f}s and not restored"
        silent_for = now - self._last_update_at
        if silent_for > STALL_SECONDS and markets_expected_open(wall_now):
            return f"no price updates for {silent_for:.0f}s while markets are open"
        return None

    def stop(self) -> None:
        self._stop.set()
        if self._ls is not None:
            self._ls.disconnect()
            logger.info("candle stream disconnected")

    def _subscribed(self) -> None:
        lost_at, self._unsubscribed_at = self._unsubscribed_at, None
        if lost_at is not None:
            logger.info("candle stream subscribed (after %.0fs)", time.monotonic() - lost_at)

    def _unsubscribed(self) -> None:
        self._unsubscribed_at = time.monotonic()
        logger.warning("candle stream unsubscribed")

    def _handle(self, epic: str, values: dict[str, str | None]) -> None:
        self._last_update_at = time.monotonic()
        candle = parse_candle(epic, values, self._timezone)
        if candle is None:
            return
        try:
            self._on_candle(candle)
        except Exception:  # noqa: BLE001 - a bad write must not kill the feed
            logger.exception("failed to store candle for %s", epic)


# IG caps how many items one Lightstreamer connection may subscribe: 32 hourly
# candles subscribed fine, 48 were refused outright with "Subscription limit
# exceeded" (2026-10-07), leaving the collector storing nothing. The cap is per
# connection, not per account - a second connection took 16 more at once - so
# instruments are spread over as many connections as needed, each held well
# under it.
MAX_ITEMS_PER_CONNECTION = 30


def split_epics(epics: list[str], max_per_connection: int = MAX_ITEMS_PER_CONNECTION) -> list[list[str]]:
    """Deal epics round-robin into the fewest connections that respect the cap.

    Round-robin rather than in config order so every connection carries a
    share of each group. That matters for the stall check: FX trades around
    the clock, so a connection holding some FX is never legitimately silent
    for long on a weekday, whereas one holding only softs or grains would be
    silent most of every night and restart itself in a loop.
    """
    if max_per_connection < 1:
        raise ValueError("max_per_connection must be at least 1")
    count = max(1, -(-len(epics) // max_per_connection))
    return [epics[i::count] for i in range(count) if epics[i::count]]


class CandleStreams:
    """HourlyCandleStream over as many connections as the per-connection cap needs.

    Same start / wait / stop as a single stream. Each connection keeps its own
    watchdog, and wait() raises StreamStalled if any one of them stalls - the
    process then exits and launchd restarts the lot.
    """

    def __init__(
        self, client: IGClient, epics: list[str], on_candle, timezone: str = DEFAULT_TIMEZONE,
        max_per_connection: int = MAX_ITEMS_PER_CONNECTION,
    ):
        self._streams = [
            HourlyCandleStream(client, batch, on_candle, timezone=timezone)
            for batch in split_epics(list(epics), max_per_connection)
        ]
        self._timezone = timezone
        self._stop = threading.Event()

    @property
    def connections(self) -> int:
        return len(self._streams)

    def start(self) -> None:
        for stream in self._streams:
            stream.start()

    def health_problem(self, now: float, wall_now: pd.Timestamp) -> str | None:
        for number, stream in enumerate(self._streams, start=1):
            problem = stream.health_problem(now, wall_now)
            if problem is not None:
                return f"connection {number} of {len(self._streams)}: {problem}"
        return None

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            interval = HEALTH_CHECK_INTERVAL_SECONDS if remaining is None else min(remaining, HEALTH_CHECK_INTERVAL_SECONDS)
            if self._stop.wait(interval):
                return
            problem = self.health_problem(time.monotonic(), pd.Timestamp.now(tz=self._timezone))
            if problem is not None:
                raise StreamStalled(problem)

    def stop(self) -> None:
        self._stop.set()
        for stream in self._streams:
            stream.stop()


class _CandleListener(SubscriptionListener):
    def __init__(self, stream: HourlyCandleStream):
        self._stream = stream

    def onItemUpdate(self, update) -> None:
        # Item name is CHART:<epic>:HOUR.
        epic = update.getItemName().split(":", 1)[1].rsplit(":", 1)[0]
        self._stream._handle(epic, {f: update.getValue(f) for f in STREAM_FIELDS})

    def onSubscription(self) -> None:
        self._stream._subscribed()

    def onSubscriptionError(self, code, message) -> None:
        logger.error("candle stream subscription error: code=%s message=%s", code, message)

    def onUnsubscription(self) -> None:
        self._stream._unsubscribed()
