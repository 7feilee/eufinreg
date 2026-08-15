"""Client-side filtering.

Deliberately client-side, for two reasons:

1. It behaves identically for every source, whether the wire format is Solr or
   a CSV file.
2. Substring matching across *all* fields survives schema drift — if ESMA renames
   ``ae_entityName`` tomorrow, ``--contains bybit`` keeps working while any
   hardcoded field name stops.

Server-side selection is still available where the register supports it
(``--select`` and ``--query`` on Solr cores) and is much cheaper. Use those to
narrow, then these to refine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .flatten import scalar


@dataclass
class FieldFilter:
    name: str
    value: str

    @classmethod
    def parse(cls, text: str) -> FieldFilter:
        if "=" not in text:
            raise ValueError(f"--field expects NAME=VALUE, got {text!r}")
        name, _, value = text.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--field expects a field name before '=', got {text!r}")
        return cls(name=name, value=value.strip())

    def matches(self, row: Mapping[str, Any]) -> bool:
        return scalar(row.get(self.name, "")).strip().casefold() == self.value.casefold()


@dataclass
class FilterReport:
    """What the filters did, so a zero-row result is explainable."""

    input_rows: int = 0
    output_rows: int = 0
    missing_fields: list[str] = field(default_factory=list)

    def warnings(self) -> list[str]:
        messages = []
        for name in self.missing_fields:
            messages.append(
                f"field {name!r} does not exist in any fetched record — "
                f"it will match nothing. Run --inspect to see the real field names."
            )
        if self.input_rows and not self.output_rows and not self.missing_fields:
            messages.append(
                f"all {self.input_rows} record(s) were filtered out. "
                f"Values are matched exactly (case-insensitively); "
                f"use --list-values FIELD to see what is actually in the data."
            )
        return messages


def row_contains(row: Mapping[str, Any], needle: str) -> bool:
    folded = needle.casefold()
    return any(folded in scalar(value).casefold() for value in row.values())


def apply_filters(
    rows: Sequence[Mapping[str, Any]],
    *,
    contains: Iterable[str] = (),
    field_filters: Iterable[FieldFilter] = (),
    limit: int | None = None,
) -> tuple[list[Mapping[str, Any]], FilterReport]:
    """Apply substring and exact-field filters. All conditions are ANDed."""
    needles = [n for n in contains if n]
    filters = list(field_filters)
    report = FilterReport(input_rows=len(rows))

    if filters:
        known: set[str] = set()
        for row in rows:
            known.update(row.keys())
        report.missing_fields = [f.name for f in filters if f.name not in known]

    result: list[Mapping[str, Any]] = []
    for row in rows:
        if not all(row_contains(row, needle) for needle in needles):
            continue
        if not all(f.matches(row) for f in filters):
            continue
        result.append(row)
        if limit and len(result) >= limit:
            break

    report.output_rows = len(result)
    return result, report
