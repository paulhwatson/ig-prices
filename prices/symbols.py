"""Instrument group loading (config/symbols.toml).

Every instrument must name the IG epic it corresponds to - it is both the
thing collected and the thing checked. This app only collects prices for
instruments streamable on IG, and an unverifiable claim of streamability is no
use, so a config entry without an epic is rejected rather than quietly
collected. `prices verify` re-checks them all against IG.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from prices.settings import DEFAULT_SYMBOLS_PATH


# IG is the only source today. The field is kept because every stored row
# records it: the store sits alongside another tool's FMP-sourced 1min/daily
# files, and a bar that cannot say where it came from is a trap once two
# vendors' scales are in the same tree.
SOURCES = ("ig", "fmp")


@dataclass(frozen=True)
class Instrument:
    symbol: str      # store filename; a name for the series, not the vendor
    ig_epic: str     # IG epic, the thing that is actually streamable
    ig_name: str = ""
    source: str = "ig"


@dataclass(frozen=True)
class SymbolGroup:
    name: str
    instruments: tuple[Instrument, ...]
    description: str = ""
    enabled: bool = True

    def by_source(self, source: str) -> tuple[Instrument, ...]:
        return tuple(i for i in self.instruments if i.source == source)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(instrument.symbol for instrument in self.instruments)

    def epic_for(self, symbol: str) -> str | None:
        for instrument in self.instruments:
            if instrument.symbol == symbol:
                return instrument.ig_epic
        return None


@dataclass(frozen=True)
class SymbolGroups:
    groups: dict[str, SymbolGroup] = field(default_factory=dict)

    def get(self, name: str) -> SymbolGroup:
        if name not in self.groups:
            known = ", ".join(sorted(self.groups)) or "none"
            raise RuntimeError(f"unknown group {name!r}. Configured groups: {known}")
        return self.groups[name]

    def enabled(self) -> list[SymbolGroup]:
        return [g for g in self.groups.values() if g.enabled]

    def all_instruments(self) -> list[tuple[str, Instrument]]:
        return [
            (group.name, instrument)
            for group in self.groups.values()
            for instrument in group.instruments
        ]


def load_symbol_groups(path: Path = DEFAULT_SYMBOLS_PATH) -> SymbolGroups:
    if not path.exists():
        raise RuntimeError(f"{path} does not exist.")

    with path.open("rb") as f:
        data = tomllib.load(f)

    groups: dict[str, SymbolGroup] = {}
    for name, raw in data.get("groups", {}).items():
        if not isinstance(raw, dict):
            raise RuntimeError(f"{path}: group {name!r} must be a table")

        # A group-level source is the default for its instruments; an
        # instrument can still name its own.
        group_source = str(raw.get("source", "ig"))
        if group_source not in SOURCES:
            raise RuntimeError(
                f"{path}: group {name!r} has unknown source {group_source!r}; "
                f"expected one of {', '.join(SOURCES)}"
            )

        groups[name] = SymbolGroup(
            name=name,
            instruments=_parse_instruments(path, name, raw.get("instruments", []), group_source),
            description=raw.get("description", ""),
            enabled=bool(raw.get("enabled", True)),
        )

    if not groups:
        raise RuntimeError(f"{path}: no groups configured")

    _check_epics_are_unique(path, groups)
    return SymbolGroups(groups=groups)


def _parse_instruments(
    path: Path, group: str, raw: object, group_source: str
) -> tuple[Instrument, ...]:
    if not isinstance(raw, list):
        raise RuntimeError(f"{path}: group {group!r} needs an `instruments` list")

    instruments: list[Instrument] = []
    seen: set[str] = set()

    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"{path}: group {group!r} has an instrument that is not a table: {entry!r}"
            )

        symbol = str(entry.get("symbol", "")).strip()
        ig_epic = str(entry.get("ig_epic", "")).strip()

        if not symbol:
            raise RuntimeError(f"{path}: group {group!r} has an instrument with no symbol")
        if not ig_epic:
            # The whole point of the config: no epic means nobody has checked
            # that this is streamable on IG, so it does not get collected.
            raise RuntimeError(
                f"{path}: {group}/{symbol} has no ig_epic. Only instruments that "
                f"are streamable on IG are collected - find the epic and run "
                f"`prices verify` before adding it."
            )

        source = str(entry.get("source", group_source))
        if source not in SOURCES:
            raise RuntimeError(
                f"{path}: {group}/{symbol} has unknown source {source!r}; "
                f"expected one of {', '.join(SOURCES)}"
            )

        unknown = set(entry) - {"symbol", "ig_epic", "ig_name", "source"}
        if unknown:
            raise RuntimeError(
                f"{path}: {group}/{symbol} has unknown key(s): {', '.join(sorted(unknown))}"
            )

        # A duplicate would mean fetching and writing the same file twice in
        # one run, so collapse it here rather than trusting the config.
        if symbol in seen:
            continue
        seen.add(symbol)

        instruments.append(
            Instrument(
                symbol=symbol,
                ig_epic=ig_epic,
                ig_name=str(entry.get("ig_name", "")),
                source=source,
            )
        )

    if not instruments:
        raise RuntimeError(f"{path}: group {group!r} has no instruments")

    return tuple(instruments)


def _check_epics_are_unique(path: Path, groups: dict[str, SymbolGroup]) -> None:
    """Two symbols on one epic would store the same IG market twice under
    different names, which is nearly always a copy-paste mistake."""
    owners: dict[str, str] = {}
    for group in groups.values():
        for instrument in group.instruments:
            previous = owners.get(instrument.ig_epic)
            if previous:
                raise RuntimeError(
                    f"{path}: {instrument.ig_epic} is mapped to two symbols "
                    f"({previous} and {group.name}/{instrument.symbol})"
                )
            owners[instrument.ig_epic] = f"{group.name}/{instrument.symbol}"
