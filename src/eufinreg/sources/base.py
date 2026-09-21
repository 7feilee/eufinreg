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
    """A public register that can be read machine-to-machine.

    Beyond "how do I read it", a source also declares the things an *operator*
    needs to run it unattended: how often the register actually changes, what it
    says about its own reliability, whether its rows are about people, and which
    fields must still exist for the client to be considered working.
    """

    #: CLI name.
    key: str = ""
    #: One-line human description.
    title: str = ""
    #: Where the interface is documented (or where its absence is documented).
    docs_url: str = ""
    #: Jurisdiction the register covers: ``EU``, ``DE``, ``AT``, ``CH``.
    #: Used to scope a run to a market rather than to a list of source keys.
    jurisdiction: str = "EU"
    #: Fields worth enumerating with ``--list-enums``. Verified against live data.
    enum_fields: tuple[str, ...] = ()
    #: Fields whose cells pack several values behind a separator.
    multi_value_fields: Mapping[str, str] = {}
    #: True when the wire format mixes parent and child records in one array.
    block_structured: bool = False
    #: Valid values for ``--select``, when the source has a fixed set.
    select_help: str = ""
    #: Columns that identify an entity across two fetches, for ``--snapshot`` /
    #: ``--diff``. Empty means the register publishes no stable identifier, and
    #: rows will be matched on their content instead — see
    #: :func:`eufinreg.snapshot.row_key`.
    key_columns: tuple[str, ...] = ()
    #: Columns that carry an identifier a *user* would recognise and search by
    #: (a UID, an LEI, an entity code). What a watchlist matches on, in order of
    #: preference. A subset of, or overlapping with, ``key_columns``.
    identifier_columns: tuple[str, ...] = ()
    #: Columns holding the entity's name, for the lower-confidence fallback
    #: match when no identifier is shared.
    name_columns: tuple[str, ...] = ()

    #: How often the register itself changes, in hours, as *documented or
    #: observed* — not how often you should poll. ``None`` where the operator
    #: publishes no cadence and none could be inferred. Drives staleness
    #: reporting in the store; see :mod:`eufinreg.store`.
    cadence_hours: float | None = 24.0
    #: The register's own words about what its data is worth. Reproduced in
    #: evidence bundles, because a receipt that omits the publisher's disclaimer
    #: is a misleading receipt.
    disclaimer: str = ""
    #: The licence the register publishes its data under, where it states one.
    #: Empty means *unstated*, which is not the same as permissive — see
    #: docs/OPERATIONS.md before redistributing anything from such a source.
    data_licence: str = ""
    #: Non-empty when rows are, or may be, about identifiable people. Gates what
    #: the HTTP API will serve without an explicit acknowledgement — see
    #: :mod:`eufinreg.policy`.
    personal_data: str = ""
    #: Fields that must still be present for this client to be working. Checked
    #: by ``eufinreg doctor`` against the live register; a register that drops
    #: one of these has changed shape and the output cannot be trusted.
    expected_fields: tuple[str, ...] = ()
    #: False where the register has no "list everything" mode. Such a source
    #: cannot be snapshotted or scheduled — it answers questions, it does not
    #: publish a population — and ``ingest`` refuses it rather than storing a
    #: partial answer that looks like a register.
    bulk_readable: bool = True
    #: A minimal request that should always return something, used by
    #: ``eufinreg doctor`` to check the interface is still alive and still the
    #: shape this client expects.
    probe_query: str = ""

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

    def personal_data_for(self, select: str | None = None) -> str:
        """The personal-data note that applies to *this selection*, if any.

        Source-level by default. Overridden where the answer depends on what was
        asked for — the EBA register is 98% agent records that are largely
        natural persons, but only if you ask for them.
        """
        return self.personal_data

    def warnings(self) -> list[str]:
        """Things the caller must be told about the result set just produced.

        Overridden by sources that can return an incomplete answer without the
        register reporting an error — a deep-paging window, for instance. The
        CLI prints these to stderr, because a silently truncated register is
        indistinguishable from a small one.
        """
        return []

    def describe(self) -> str:
        return f"{self.key:<14} {self.title}"


def merge_counts(pairs: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    for value, count in pairs:
        merged[value] = merged.get(value, 0) + count
    return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
