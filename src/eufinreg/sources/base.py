"""Common shape for every register this tool can read."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..flatten import FlattenConfig
from ..http import Fetcher


@dataclass
class Query:
    """Everything the user asked for, before any source-specific translation."""

    #: Source-specific server-side selector (e.g. an ESMA entity type code).
    select: str | None = None
    #: Raw, source-native query string. Escape hatch; passed through untouched.
    raw_query: str | None = None
    #: Include withdrawn/historic child records where the source distinguishes them.
    include_history: bool = False
    #: Hard cap on documents pulled from the wire (0/None = no cap).
    max_docs: int | None = None
    #: Documents requested per page.
    page_size: int = 500

    extra: dict[str, Any] = field(default_factory=dict)


class Source(ABC):
    """A public register that can be read machine-to-machine."""

    #: CLI name.
    key: str = ""
    #: One-line human description.
    title: str = ""
    #: Where the interface is documented (or where its absence is documented).
    docs_url: str = ""
    #: Fields worth enumerating with ``--list-enums``. Verified against live data.
    enum_fields: tuple[str, ...] = ()
    #: Fields whose cells pack several values behind a separator.
    multi_value_fields: Mapping[str, str] = {}
    #: True when the wire format mixes parent and child records in one array.
    block_structured: bool = False
    #: Valid values for ``--select``, when the source has a fixed set.
    select_help: str = ""

    def flatten_config(self) -> FlattenConfig:
        return FlattenConfig()

    @abstractmethod
    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        """Yield raw records exactly as the register serves them."""

    @abstractmethod
    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        """Return ``(value_counts, note)`` for ``field_name``.

        Sources that can answer this server-side (Solr facets) should, because
        it is exact over the whole register rather than over a sample.
        """

    def describe(self) -> str:
        return f"{self.key:<14} {self.title}"


def merge_counts(pairs: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for value, count in pairs:
        merged[value] = merged.get(value, 0) + count
    return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
