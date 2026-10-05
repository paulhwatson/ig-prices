# ig-prices — hourly OHLCV collection from IG

A standalone collector that pulls hourly bars from IG and stores them as
parquet, one file per symbol. Self-contained — no dependency on
`trading/market_data_module`, and in particular no `nautilus_trader` import, so
it starts in under a second and can run unattended.

It writes into the **existing** price tree so hourly data sits beside the 1min
and daily stores that are already there:

```
/Users/Paul/trading_data/prices/
├── energy/1hour/BZUSD.parquet       <- this app
├── etfs/1min/...                    <- existing market_data module
└── sp500/daily/...                  <- existing market_data module
```

## Why IG only

This used to collect from Financial Modeling Prep. FMP was dropped entirely,
for three reasons:

1. **It wouldn't sell four of these at all.** WTI, gasoline, natural gas and
   heating oil all return HTTP 402 on that plan — and two of them are legs of
   the pairs in `../oil-pair`.
2. **It quotes on a different scale.** Gasoline is ~2.77 from FMP and ~32778 on
   IG. Any model fitted across a mix of the two is arithmetically meaningless.
   IG is the basis these instruments are actually executed on, which is the one
   that has to be right.
3. **It has less data where it overlaps.** FMP's cash indices cover market
   hours only, ~7 bars a day. IG's index CFDs trade nearly around the clock,
   ~16 bars a day.

Every stored row still records its `source`, and the store refuses to merge
bars from different feeds (`SourceMismatchError`). That guard is kept because
this store sits alongside another tool's FMP-sourced 1min/daily files — a bar
that cannot say where it came from is a trap once two vendors are in one tree.

Symbol names are kept from the FMP era so existing readers keep working. They
name the series, not the vendor: every file here is IG data.

## What is collected

Only instruments **streamable on IG** — `streamingPricesAvailable = true`. IG
streams its own derived markets (commodities, spot metals, FX, index CFDs) at
zero delay, and does not stream shares or ETFs, which come back
`streamingPricesAvailable = false` with `delayTime = 20`. That is why there are
no equities or ETFs here.

Every entry in `config/symbols.toml` names its IG epic, the loader rejects any
that doesn't, and `ig-prices verify` re-checks all 32 against IG.

## Two ways in, and why both exist

| | what it does | cost |
|---|---|---|
| `ig-prices stream` | subscribes to `CHART:<epic>:HOUR` and stores each candle as it closes | **nothing** |
| `ig-prices backfill` | extends history further into the past | weekly allowance |
| `ig-prices update` | tops a symbol up to now | weekly allowance |

Once the stream is running, history only ever grows forward for free, and the
metered REST calls are needed only to deepen the past. The only scheduled job
is the stream; run `ig-prices backfill` by hand when you want more history.

### IG's limits, measured 2026-09-21

- **History stops at about a year.** One year back returns bars; two years back
  returns nothing.
- **10,000 data points a week**, weekly and not reset by retry. A near-24h
  instrument is ~115 hourly bars a week, so a year is ~6,000 points for one
  symbol and ~170,000 for all 32 — about seventeen weeks of allowance (the
  softs trade ~9 hours a day, so ~2,300 points a year each). Hence
  `backfill_days` (90) for a first reach, and weekly deepening after that.
- **A separate per-minute request cap** returns the same HTTP 403 as a spent
  allowance, told apart only by the error code in the body. The client retries
  the burst limit with backoff and stops dead on the real allowance — treating
  them alike either wastes a week's budget or gives up on a two-second pause.
- **`/prices` pages at 20 bars.** Not following the pages returns a fifth of
  each window and looks like sparse data rather than a bug.
- **`^VIX` streams but has no history**: IG answers
  `unauthorised.access.to.equity.exception`. It fills forward only.

### Timestamps

IG stamps REST bars in the **account's local time** (at 17:47 London the newest
hourly bar is 17:00, not 16:00) while the streaming feed sends **epoch UTC**.
Candles are converted into `ig.timezone` before storage so streamed and
backfilled bars for the same hour share a timestamp instead of landing an hour
apart. Stored timestamps are naive, in that zone.

The neighbouring 1min and daily stores hold naive *exchange*-local times from
another tool, so anything reading across the whole tree has to localise rather
than assume one zone.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .
```

No `.env` is needed on this machine: `ig_prices/settings.py` falls back to
`~/trading/.env`, which already has the `IG_DEMO_*` login. Elsewhere,
`cp .env.example .env` and fill it in. Demo credentials — this app only reads
market data, so it has no business holding live ones.

## Usage

```bash
./.venv/bin/python -m ig_prices.main stream                          # live candles, free
./.venv/bin/python -m ig_prices.main backfill                        # deepen history
./.venv/bin/python -m ig_prices.main update                          # top up to now
./.venv/bin/python -m ig_prices.main update --group energy --symbol BZUSD --since 2025-01-01
./.venv/bin/python -m ig_prices.main groups                          # what's configured
./.venv/bin/python -m ig_prices.main verify                          # re-check streamability
./.venv/bin/python -m ig_prices.main show --group energy --symbol BZUSD
```

`update`/`backfill` exit non-zero if any symbol failed; a spent allowance is
not a failure, it's a "come back next week". `verify` exits non-zero if any
epic has stopped streaming. Logs go to `logs/ig-prices.log` (rotating, 10MB × 5).

## Scheduling

```bash
./scripts/install_schedule.sh install-stream    # the collector, kept alive
./scripts/install_schedule.sh status
```

The stream is a long-lived subscription under `KeepAlive`, so launchd restarts
it if the socket drops. The weekly backfill is no longer scheduled: run
`./scripts/run_backfill.sh` by hand, at most once a week since the allowance it
spends is a weekly pool. `./scripts/install_schedule.sh uninstall` removes an
old weekly agent.

launchd rather than cron: a cron job on macOS runs without Full Disk Access and
dies silently when the machine sleeps, whereas a launchd agent catches up after
a wake.

## Adding an instrument

1. Find its IG epic — `search_markets` in `../oil-pair`, or IG's platform. It
   must be a rolling contract (`DFB`/`TODAY`), not a dated expiry.
2. Add it to the right group in `config/symbols.toml`.
3. `ig-prices verify` — confirms IG streams it.
4. `ig-prices update --group <group> --symbol <symbol>` — confirms IG serves it.

An entry with no `ig_epic` is refused at load time, so nothing can quietly
start being collected that nobody has checked. Two symbols on one epic are
refused too.

## How a fetch works

1. Read what's already stored.
2. Pick a window — `--since` if given; for `update`, from just before the last
   stored bar; for `backfill`, the span immediately before the earliest one.
3. Split it into `chunk_days` windows. `backfill` walks them **newest first**,
   so a budget that runs out mid-way leaves the series contiguous with what is
   already stored rather than punching a hole in the middle.
4. Clean: parse timestamps, coerce to float, drop bars failing OHLC consistency.
5. Merge, **fetched values winning** on an overlap. The most recent stored bar
   was usually still forming when written, so it has to be replaced with the
   settled version, not discarded as a duplicate.
6. Write to a temp file and rename, so an interrupted run can't leave a
   half-written parquet where a good one used to be.

One symbol's failure never ends the run. The allowance is read once up front,
so the reserve is honoured from the first chunk rather than one window late.

## Stored format

`<root>/<group>/1hour/<symbol>.parquet`, indexed by `timestamp`, columns
`open, high, low, close, volume, symbol, source` (all float bar the last two).
Bars are carried as the mid of IG's bid and ask OHLC.

Only completed candles are stored from the stream (`CONS_END=1`): IG
republishes the in-progress candle on every tick, and storing those would fill
the store with partial bars that disagree with the settled one.

## Tests

```bash
./.venv/bin/python -m pytest
```

Network is never touched — `IGClient` takes an injectable session, and the
update/stream tests use fakes.
