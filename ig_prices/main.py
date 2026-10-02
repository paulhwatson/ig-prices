"""Command line entry point.

    python -m ig_prices.main stream                      # live candles, costs no allowance
    python -m ig_prices.main update                      # top up to now
    python -m ig_prices.main backfill                    # extend history further back
    python -m ig_prices.main update --group energy --symbol BZUSD --since 2025-01-01
    python -m ig_prices.main groups
    python -m ig_prices.main verify                      # re-check IG streamability
    python -m ig_prices.main show --group energy --symbol BZUSD

Exits non-zero if any symbol failed, so an unattended run surfaces a problem
instead of logging it into a file nobody reads.
"""

from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from ig_prices import store, update, verify as verify_module
from ig_prices.ig import IGClient, IGError
from ig_prices.logging_setup import configure_logging
from ig_prices.settings import INTERVAL, load_config, load_ig_credentials
from ig_prices.symbols import SymbolGroup, load_symbol_groups

logger = logging.getLogger("ig_prices")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level)

    try:
        config = load_config()
        groups = load_symbol_groups()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    if args.command == "groups":
        return _cmd_groups(groups)
    if args.command == "verify":
        return _cmd_verify(groups)
    if args.command == "stream":
        return _cmd_stream(config, groups, args)
    if args.command == "backfill":
        return _cmd_fetch(config, groups, args, backfill=True)
    if args.command == "show":
        return _cmd_show(config, groups, args)
    return _cmd_fetch(config, groups, args, backfill=False)


def _cmd_groups(groups) -> int:
    for group in groups.groups.values():
        state = "" if group.enabled else " (disabled)"
        logger.info(
            "%s%s: %d instrument(s) - %s",
            group.name, state, len(group.instruments), group.description,
        )
        for instrument in group.instruments:
            logger.info(
                "    %-10s %-24s %-4s %s",
                instrument.symbol, instrument.ig_epic, instrument.source, instrument.ig_name,
            )
    return 0


def _cmd_verify(groups) -> int:
    """Re-ask IG whether every configured epic is still streamable."""
    try:
        credentials = load_ig_credentials()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    client = IGClient(credentials)
    try:
        results = verify_module.verify(groups, client)
    except IGError as exc:
        logger.error("%s", exc)
        return 1

    verify_module.report(results)
    return 1 if any(result.is_problem for result in results) else 0


def _cmd_show(config, groups, args) -> int:
    try:
        groups.get(args.group)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    for symbol in args.symbol:
        path = store.price_path(config.store.root, args.group, symbol)
        bars = store.read(path)
        if bars.empty:
            logger.info("%s/%s: nothing stored (%s)", args.group, symbol, path)
            continue
        logger.info(
            "%s/%s: %d bar(s), %s to %s (%s)",
            args.group, symbol, len(bars), bars.index.min(), bars.index.max(), path,
        )
    return 0


def _cmd_stream(config, groups, args) -> int:
    """Keep ig-sourced symbols current from the live candle feed.

    Runs until interrupted. Costs no historical-data allowance, which is the
    point: the REST backfill is metered, this is not.
    """
    from ig_prices.collect import CandleWriter
    from ig_prices.ig_stream import HourlyCandleStream, StreamStalled

    if args.group:
        try:
            selected = [(groups.get(args.group), None)]
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
    else:
        selected = [(group, None) for group in groups.enabled()]

    instruments = [
        (group.name, instrument)
        for group, _ in selected
        for instrument in group.by_source("ig")
    ]
    if not instruments:
        logger.error("no ig-sourced instruments configured - nothing to stream")
        return 2

    try:
        credentials = load_ig_credentials()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    writer = CandleWriter(config.store.root, instruments)
    client = IGClient(credentials, timeout_seconds=config.ig.timeout_seconds)
    stream = HourlyCandleStream(client, writer.epics, writer, timezone=config.ig.timezone)

    try:
        stream.start()
    except IGError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "streaming hourly candles for %d instrument(s); a candle is stored as it "
        "closes, so the first write lands on the hour",
        len(instruments),
    )
    try:
        stream.wait()
    except StreamStalled as exc:
        # A non-zero exit is the recovery: launchd's KeepAlive restarts the
        # process, which logs in afresh.
        logger.error("%s - exiting so the stream restarts", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        stream.stop()
    return 0


def _cmd_fetch(config, groups, args, backfill: bool) -> int:
    """update tops symbols up to now; backfill extends them further back."""
    try:
        credentials = load_ig_credentials()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    try:
        targets = _targets(groups, args)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    if not targets:
        logger.error("nothing to do - no enabled groups matched")
        return 2

    since = None
    if getattr(args, "since", None):
        try:
            since = pd.Timestamp(args.since)
        except ValueError:
            logger.error("--since is not a date: %r", args.since)
            return 2

    client = IGClient(
        credentials,
        timeout_seconds=config.ig.timeout_seconds,
        requests_per_minute=config.ig.requests_per_minute,
        run_budget=config.ig.max_points_per_run,
    )
    # Read the allowance before doing anything, so the reserve is honoured
    # from the first chunk rather than one window late.
    first_epic = next(
        (i.ig_epic for group, _ in targets for i in group.instruments), None
    )
    if first_epic:
        primed = client.prime_allowance(first_epic)
        if primed is not None and primed.remaining <= config.ig.min_allowance_reserve:
            logger.warning(
                "IG allowance is down to %d of %d, at or below the %d reserve - "
                "nothing to spend until it resets in %.1f day(s)",
                primed.remaining, primed.total,
                config.ig.min_allowance_reserve, primed.expiry_days,
            )
            return 0

    results: list[update.SymbolResult] = []

    logger.info(
        "starting %s %s into %s (budget %d point(s) this run)",
        INTERVAL, "backfill" if backfill else "update",
        config.store.root, config.ig.max_points_per_run,
    )
    for group, symbols in targets:
        if backfill:
            results.extend(update.backfill_group(client, config, group, symbols))
        else:
            results.extend(
                update.update_group(
                    client, config, group, symbols, since=since, full=args.full
                )
            )

    logger.info("finished: %s", update.summarise(results))

    allowance = client.allowance()
    if allowance is not None:
        logger.info(
            "IG allowance: %d of %d left, resets in %.1f day(s); spent this run: %d",
            allowance.remaining, allowance.total, allowance.expiry_days, client.points_spent,
        )

    spent = [r for r in results if r.status is update.Status.ALLOWANCE_SPENT]
    if spent:
        logger.warning(
            "stopped on the IG allowance: %s - re-run after it resets to continue",
            ", ".join(f"{r.group}/{r.symbol}" for r in spent),
        )

    failures = [r for r in results if r.is_failure]
    if failures:
        for failure in failures:
            logger.error("failed: %s/%s - %s", failure.group, failure.symbol, failure.error)
        return 1

    return 0


def _targets(groups, args) -> list[tuple[SymbolGroup, list[str] | None]]:
    """Which (group, symbols) pairs this run covers.

    --symbol without --group is ambiguous once a symbol appears in two groups,
    so it is only accepted alongside --group.
    """
    if args.symbol and not args.group:
        raise RuntimeError("--symbol needs --group, so the store path is unambiguous")

    if args.group:
        group = groups.get(args.group)
        return [(group, list(args.symbol) if args.symbol else None)]

    return [(group, None) for group in groups.enabled()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ig-prices", description=f"Collect {INTERVAL} OHLCV bars from IG."
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    # Shared so --log-level works on either side of the subcommand. SUPPRESS
    # matters: without it argparse writes the subparser's own default over a
    # value the top-level parser already parsed, so `ig-prices --log-level DEBUG
    # update` would silently run at INFO.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    subparsers = parser.add_subparsers(dest="command")
    # `ig-prices` with no subcommand is the unattended case (see the schedule
    # script), so it has to behave exactly like a bare `ig-prices update`.
    parser.set_defaults(command="update", group=None, symbol=None, since=None, full=False)

    update_parser = subparsers.add_parser(
        "update", parents=[common], help="fetch and store new bars"
    )
    update_parser.add_argument("--group", help="only this group (default: every enabled group)")
    update_parser.add_argument(
        "--symbol", action="append", help="only this symbol; repeatable, needs --group"
    )
    update_parser.add_argument(
        "--since", help="fetch from this date (YYYY-MM-DD) instead of topping up"
    )
    update_parser.add_argument(
        "--full", action="store_true", help="re-fetch the full default history, not just the gap"
    )

    backfill_parser = subparsers.add_parser(
        "backfill", parents=[common],
        help="extend history further back, as far as this week's allowance allows",
    )
    backfill_parser.add_argument("--group", help="only this group")
    backfill_parser.add_argument("--symbol", action="append", help="only this symbol; needs --group")

    subparsers.add_parser(
        "groups", parents=[common], help="list configured groups and instruments"
    )

    subparsers.add_parser(
        "verify", parents=[common], help="check every configured epic is streamable on IG"
    )

    stream_parser = subparsers.add_parser(
        "stream", parents=[common],
        help="keep ig-sourced symbols current from IG's live candle feed (no allowance cost)",
    )
    stream_parser.add_argument("--group", help="only this group (default: every enabled group)")

    show_parser = subparsers.add_parser(
        "show", parents=[common], help="summarise what is stored for a symbol"
    )
    show_parser.add_argument("--group", required=True)
    show_parser.add_argument("--symbol", action="append", required=True)

    return parser


if __name__ == "__main__":
    sys.exit(main())
