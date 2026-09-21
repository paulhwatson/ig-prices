"""Live hourly candles from IG's Lightstreamer feed.

The REST backfill in prices/ig.py draws on a weekly allowance; this does not.
CHART:<epic>:HOUR pushes a consolidated hourly candle, so once a symbol is
backfilled this keeps it current for free, indefinitely.

Only candles marked CONS_END=1 are stored. IG republishes the in-progress
candle on every tick, and writing those would fill the store with partial
bars that disagree with the settled one - the store would rather have a gap
than a bar that was never real.

Not MARKET:<epic>, which IG rejects outright as of 2026-09-21 (Lightstreamer
error 21), and not trading_ig: authentication is three headers and the
lightstreamerEndpoint the login already returns (see IGClient).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import pandas as pd
from lightstreamer.client import LightstreamerClient, Subscription, SubscriptionListener

from prices.ig import IGClient, IGError

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
        self._stop.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._ls is not None:
            self._ls.disconnect()
            logger.info("candle stream disconnected")

    def _handle(self, epic: str, values: dict[str, str | None]) -> None:
        candle = parse_candle(epic, values, self._timezone)
        if candle is None:
            return
        try:
            self._on_candle(candle)
        except Exception:  # noqa: BLE001 - a bad write must not kill the feed
            logger.exception("failed to store candle for %s", epic)


class _CandleListener(SubscriptionListener):
    def __init__(self, stream: HourlyCandleStream):
        self._stream = stream

    def onItemUpdate(self, update) -> None:
        # Item name is CHART:<epic>:HOUR.
        epic = update.getItemName().split(":", 1)[1].rsplit(":", 1)[0]
        self._stream._handle(epic, {f: update.getValue(f) for f in STREAM_FIELDS})

    def onSubscriptionError(self, code, message) -> None:
        logger.error("candle stream subscription error: code=%s message=%s", code, message)

    def onUnsubscription(self) -> None:
        logger.warning("candle stream unsubscribed")
