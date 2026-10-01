"""IG REST client: epic verification and hourly history.

Two jobs:

1. Verify an epic is streamable, not one of the delayed share/ETF markets -
   `instrument.streamingPricesAvailable` answers it.
2. Serve hourly bars for every configured instrument. IG is the basis they
   are actually traded on, which is the one that has to be right.

History here is bounded and metered. IG keeps roughly a
year of hourly bars (probed 2026-09-21: one year back returns data, two years
back returns nothing) and charges every returned point against a weekly
allowance - 10,000 on this account, not resetting on retry. So `hourly_bars`
takes an explicit budget and stops when it would exceed it, rather than
discovering the limit by hitting it.

Deliberately not `trading_ig`: this needs a handful of endpoints, and the
library's market-navigation calls are 404 on this API key anyway (IG has the
tree disabled here), so the dependency would buy nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from ig_prices.settings import IGCredentials

logger = logging.getLogger(__name__)

DEMO_BASE_URL = "https://demo-api.ig.com/gateway/deal"
LIVE_BASE_URL = "https://api.ig.com/gateway/deal"

# IG's /markets endpoint takes a comma-separated list, capped at 50.
_MAX_EPICS_PER_CALL = 50

HOURLY_RESOLUTION = "HOUR"

# IG returns the same HTTP 403 for two unrelated limits, told apart only by
# the error code in the body. One is a burst limit that clears in seconds; the
# other is the weekly data budget that does not reset on retry. Conflating
# them either wastes a week's allowance or gives up on a two-second pause.
_RATE_LIMIT_CODES = (
    "error.public-api.exceeded-api-key-allowance",
    "error.public-api.exceeded-account-allowance",
)
_DATA_ALLOWANCE_CODES = (
    "error.public-api.exceeded-account-historical-data-allowance",
    "error.public-api.historical-data-allowance-exceeded",
)
_MAX_RATE_LIMIT_RETRIES = 5
# IG's /prices pages at 20 bars by default. Not following the pages silently
# returns a fifth of a window and looks like sparse data rather than a bug.
_PAGE_SIZE = 500
_MAX_PAGES_PER_CHUNK = 20


class IGError(RuntimeError):
    """Any IG request that could not be completed."""


class AllowanceExceededError(IGError):
    """The weekly historical-data allowance is spent, or would be by this run.

    Not retryable: the allowance is weekly and does not reset on retry, so a
    caller that retries on this only burns what is left.
    """


class RateLimitedError(IGError):
    """IG's per-minute request cap. Clears on its own within seconds."""


@dataclass(frozen=True)
class Allowance:
    remaining: int
    total: int
    expiry_seconds: int

    @property
    def expiry_days(self) -> float:
        return self.expiry_seconds / 86400


@dataclass(frozen=True)
class MarketStatus:
    epic: str
    streamable: bool
    delay_minutes: int
    market_status: str
    name: str
    instrument_type: str


class IGClient:
    def __init__(
        self,
        credentials: IGCredentials,
        session: requests.Session | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 30,
        requests_per_minute: int = 30,
        run_budget: int | None = None,
    ):
        self._credentials = credentials
        self._session = session or requests.Session()
        self._base_url = base_url or (
            DEMO_BASE_URL if credentials.acc_type == "demo" else LIVE_BASE_URL
        )
        self._timeout = timeout_seconds
        self._auth: dict[str, str] | None = None
        self._lightstreamer_endpoint: str | None = None
        self.last_allowance: Allowance | None = None
        self._min_request_interval = 60.0 / max(requests_per_minute, 1)
        self._last_request_at = 0.0
        # Budget is shared across every symbol in a run, not per symbol: the
        # weekly allowance is one pool and four instruments draw on it.
        self.run_budget = run_budget
        self.points_spent = 0

    def login(self) -> None:
        response = self._session.post(
            f"{self._base_url}/session",
            json={
                "identifier": self._credentials.username,
                "password": self._credentials.password,
            },
            headers={**self._headers(version="2"), "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if not response.ok:
            raise IGError(f"IG login failed: HTTP {response.status_code} ({response.text[:200]})")

        try:
            self._auth = {
                "CST": response.headers["CST"],
                "X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"],
            }
        except KeyError as exc:
            raise IGError(f"IG login response is missing {exc} - cannot authenticate") from exc

        try:
            self._lightstreamer_endpoint = response.json().get("lightstreamerEndpoint")
        except ValueError:
            self._lightstreamer_endpoint = None

    @property
    def lightstreamer_endpoint(self) -> str | None:
        """Where the streaming feed lives, as the login response reports it."""
        return self._lightstreamer_endpoint

    @property
    def stream_password(self) -> str:
        """The composite token IG's Lightstreamer expects as the password."""
        if self._auth is None:
            raise IGError("not logged in")
        return f"CST-{self._auth['CST']}|XST-{self._auth['X-SECURITY-TOKEN']}"

    def market_status(self, epics: list[str]) -> dict[str, MarketStatus]:
        """Streamability for each epic, batched into as few calls as IG allows.

        Epics IG does not recognise are simply absent from the result; the
        caller decides whether that is an error, since an epic that has been
        retired is a config problem rather than a request problem.
        """
        if self._auth is None:
            self.login()

        statuses: dict[str, MarketStatus] = {}
        for batch in _batched(epics, _MAX_EPICS_PER_CALL):
            response = self._session.get(
                f"{self._base_url}/markets",
                params={"epics": ",".join(batch)},
                headers={**self._headers(version="2"), **(self._auth or {})},
                timeout=self._timeout,
            )
            if not response.ok:
                raise IGError(
                    f"IG /markets failed: HTTP {response.status_code} ({response.text[:200]})"
                )

            try:
                details = response.json()["marketDetails"]
            except (ValueError, KeyError) as exc:
                raise IGError(f"unexpected IG /markets response: {response.text[:200]}") from exc

            for market in details:
                instrument = market.get("instrument", {})
                snapshot = market.get("snapshot", {})
                epic = instrument.get("epic", "")
                statuses[epic] = MarketStatus(
                    epic=epic,
                    streamable=bool(instrument.get("streamingPricesAvailable")),
                    delay_minutes=int(snapshot.get("delayTime") or 0),
                    market_status=str(snapshot.get("marketStatus") or ""),
                    name=str(instrument.get("name") or ""),
                    instrument_type=str(instrument.get("type") or ""),
                )

        return statuses

    def hourly_bars(
        self,
        epic: str,
        start: "pd.Timestamp",
        end: "pd.Timestamp",
        chunk_days: int = 7,
        budget: int | None = None,
        min_reserve: int = 0,
        newest_first: bool = False,
    ) -> "pd.DataFrame":
        """Hourly bars for [start, end], as a DataFrame with a `date` column.

        `budget` caps how many data points this call may draw from the weekly
        allowance; `min_reserve` is how much of the allowance must be left
        untouched. Both are checked before each request, because the allowance
        is only knowable from a response - so the only safe way to respect it
        is to stop early, never to probe the limit.

        Returns whatever was retrieved before a limit was reached, so a
        partial backfill is still written rather than thrown away.

        `newest_first` walks the windows backwards. That matters when extending
        history: the budget will run out partway, and taking the newest windows
        first keeps the stored series contiguous with what is already there
        instead of leaving a hole in the middle.
        """
        import pandas as pd

        if self._auth is None:
            self.login()

        rows: list[dict] = []
        budget = budget if budget is not None else self.run_budget

        chunks = _chunk_days(start, end, chunk_days)
        if newest_first:
            chunks = list(reversed(chunks))

        for chunk_start, chunk_end in chunks:
            if budget is not None and self.points_spent >= budget:
                logger.warning(
                    "%s: run budget of %d point(s) is spent; %s onwards not fetched",
                    epic, budget, chunk_start.date(),
                )
                break

            allowance = self.last_allowance
            if allowance is not None and allowance.remaining <= min_reserve:
                raise AllowanceExceededError(
                    f"{epic}: IG allowance down to {allowance.remaining} of "
                    f"{allowance.total}, at or below the {min_reserve} reserve "
                    f"(resets in {allowance.expiry_days:.1f} days)"
                )

            try:
                chunk_rows = self._fetch_chunk(epic, chunk_start, chunk_end)
            except (AllowanceExceededError, RateLimitedError):
                # Keep what we already paid for: the caller writes the partial
                # backfill and picks up from there on the next run.
                if rows:
                    logger.warning(
                        "%s: stopped early at %s, keeping %d bar(s) already fetched",
                        epic, chunk_start.date(), len(rows),
                    )
                    break
                raise

            rows.extend(chunk_rows)
            self.points_spent += len(chunk_rows)

        frame = pd.DataFrame(rows)
        if not frame.empty:
            # IG stamps bars "yyyy/MM/dd HH:mm:ss".
            frame["date"] = pd.to_datetime(frame["date"], format="%Y/%m/%d %H:%M:%S", errors="coerce")
            frame = frame.dropna(subset=["date"]).drop_duplicates(subset="date")
        return frame

    def _fetch_chunk(self, epic: str, start: "pd.Timestamp", end: "pd.Timestamp") -> list[dict]:
        """Every page of one window, flattened into store-shaped rows."""
        rows: list[dict] = []
        page = 1
        while page <= _MAX_PAGES_PER_CHUNK:
            payload = self._get_prices(epic, start, end, page=page)
            self.last_allowance = _parse_allowance(payload) or self.last_allowance

            for bar in payload.get("prices") or []:
                close = _mid(bar.get("closePrice"))
                if close is None:
                    continue
                rows.append({
                    "date": bar.get("snapshotTime"),
                    "open": _mid(bar.get("openPrice")),
                    "high": _mid(bar.get("highPrice")),
                    "low": _mid(bar.get("lowPrice")),
                    "close": close,
                    "volume": bar.get("lastTradedVolume") or 0,
                })

            page_data = (payload.get("metadata") or {}).get("pageData") or {}
            total_pages = int(page_data.get("totalPages") or 1)
            if page >= total_pages:
                break
            page += 1

        return rows

    def allowance(self) -> Allowance | None:
        """The allowance as of the last history call, without spending more."""
        return self.last_allowance

    def prime_allowance(self, epic: str) -> Allowance | None:
        """Learn the remaining allowance before committing to a run.

        There is no free way to read it - it only comes back in the metadata of
        a history response - so this asks for a single data point. Without it
        the first chunk of a run is fetched blind, and a reserve meant to keep
        headroom for manual work gets overshot by a whole window before anyone
        notices it was already spent.
        """
        if self._auth is None:
            self.login()
        try:
            payload = self._get_prices_raw(epic, {"resolution": HOURLY_RESOLUTION, "max": 1})
        except IGError as exc:
            logger.warning("could not read the IG allowance up front: %s", exc)
            return None
        self.last_allowance = _parse_allowance(payload) or self.last_allowance
        self.points_spent += 1
        return self.last_allowance

    def _get_prices_raw(self, epic: str, params: dict) -> dict:
        self._throttle()
        response = self._session.get(
            f"{self._base_url}/prices/{epic}",
            params=params,
            headers={**self._headers(version="3"), **(self._auth or {})},
            timeout=self._timeout,
        )
        if not response.ok:
            raise IGError(f"{epic}: HTTP {response.status_code} ({response.text[:200]})")
        try:
            return response.json()
        except ValueError as exc:
            raise IGError(f"{epic}: unparseable IG /prices response") from exc

    def _get_prices(
        self, epic: str, start: "pd.Timestamp", end: "pd.Timestamp", page: int = 1
    ) -> dict:
        """One window of bars, retrying through IG's per-minute burst limit."""
        for attempt in range(1, _MAX_RATE_LIMIT_RETRIES + 1):
            self._throttle()
            response = self._session.get(
                f"{self._base_url}/prices/{epic}",
                params={
                    "resolution": HOURLY_RESOLUTION,
                    "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "pageSize": _PAGE_SIZE,
                    "pageNumber": page,
                },
                headers={**self._headers(version="3"), **(self._auth or {})},
                timeout=self._timeout,
            )

            if response.status_code == 403:
                body = response.text[:300]
                if any(code in body for code in _DATA_ALLOWANCE_CODES):
                    raise AllowanceExceededError(
                        f"{epic}: IG weekly historical-data allowance is spent ({body})"
                    )
                if any(code in body for code in _RATE_LIMIT_CODES):
                    backoff = min(2.0 ** attempt, 60.0)
                    logger.warning(
                        "%s: IG per-minute request cap hit (attempt %d/%d), waiting %.0fs",
                        epic, attempt, _MAX_RATE_LIMIT_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise IGError(f"{epic}: IG refused the history request ({body})")

            if not response.ok:
                raise IGError(
                    f"{epic}: IG /prices failed: HTTP {response.status_code} "
                    f"({response.text[:200]})"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise IGError(f"{epic}: unparseable IG /prices response") from exc

        raise RateLimitedError(
            f"{epic}: still rate-limited after {_MAX_RATE_LIMIT_RETRIES} attempts"
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _headers(self, version: str) -> dict[str, str]:
        return {
            "X-IG-API-KEY": self._credentials.api_key,
            "Accept": "application/json; charset=UTF-8",
            "Version": version,
        }


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _chunk_days(start, end, chunk_days: int):
    """Split [start, end] into windows of at most chunk_days."""
    import pandas as pd

    if start > end:
        return []
    chunks = []
    span = pd.Timedelta(days=chunk_days)
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + span, end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end
    return chunks


def _parse_allowance(payload: dict) -> Allowance | None:
    raw = (payload.get("metadata") or {}).get("allowance") or {}
    if "remainingAllowance" not in raw:
        return None
    return Allowance(
        remaining=int(raw.get("remainingAllowance", 0)),
        total=int(raw.get("totalAllowance", 0)),
        expiry_seconds=int(raw.get("allowanceExpiry", 0)),
    )


def _mid(price: dict | None) -> float | None:
    """IG quotes each OHLC point as {bid, ask, lastTraded}; the store holds one
    series, so bars are carried as the mid. lastTraded is None on these CFDs."""
    if not price:
        return None
    bid, ask = price.get("bid"), price.get("ask")
    if bid is None or ask is None:
        return bid if ask is None else ask
    return (float(bid) + float(ask)) / 2
