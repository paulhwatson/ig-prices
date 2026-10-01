"""Check that every configured instrument is still streamable on IG.

The config claims each symbol maps to a streamable IG epic. That claim can go
stale - IG retires epics and changes what it streams - so this re-asks IG and
reports anything that no longer holds. It is a separate command, not part of
the hourly update, so collection never depends on an IG session being available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ig_prices.ig import IGClient, MarketStatus
from ig_prices.symbols import SymbolGroups

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    STREAMABLE = "streamable"
    NOT_STREAMABLE = "not_streamable"
    UNKNOWN_EPIC = "unknown_epic"


@dataclass(frozen=True)
class VerificationResult:
    group: str
    symbol: str
    ig_epic: str
    verdict: Verdict
    status: MarketStatus | None = None

    @property
    def is_problem(self) -> bool:
        return self.verdict is not Verdict.STREAMABLE


def verify(groups: SymbolGroups, client: IGClient) -> list[VerificationResult]:
    instruments = groups.all_instruments()
    epics = [instrument.ig_epic for _, instrument in instruments]

    logger.info("checking %d epic(s) against IG", len(epics))
    statuses = client.market_status(epics)

    results = []
    for group_name, instrument in instruments:
        status = statuses.get(instrument.ig_epic)
        if status is None:
            verdict = Verdict.UNKNOWN_EPIC
        elif status.streamable:
            verdict = Verdict.STREAMABLE
        else:
            verdict = Verdict.NOT_STREAMABLE

        results.append(
            VerificationResult(
                group=group_name,
                symbol=instrument.symbol,
                ig_epic=instrument.ig_epic,
                verdict=verdict,
                status=status,
            )
        )

    return results


def report(results: list[VerificationResult]) -> None:
    for result in sorted(results, key=lambda r: (r.is_problem, r.group, r.symbol)):
        status = result.status
        if result.verdict is Verdict.STREAMABLE and status:
            logger.info(
                "  %-10s %-10s %-24s streamable, %s, delay %dm  (%s)",
                result.group, result.symbol, result.ig_epic,
                status.market_status.lower() or "unknown", status.delay_minutes, status.name,
            )
        elif result.verdict is Verdict.NOT_STREAMABLE and status:
            logger.error(
                "  %-10s %-10s %-24s NOT STREAMABLE (delay %dm) - %s",
                result.group, result.symbol, result.ig_epic, status.delay_minutes, status.name,
            )
        else:
            logger.error(
                "  %-10s %-10s %-24s UNKNOWN EPIC - IG did not return this market",
                result.group, result.symbol, result.ig_epic,
            )

    problems = [r for r in results if r.is_problem]
    if problems:
        logger.error(
            "%d of %d instrument(s) are no longer streamable on IG - remove them "
            "from config/symbols.toml or find the replacement epic",
            len(problems), len(results),
        )
    else:
        logger.info("all %d instrument(s) are streamable on IG", len(results))
