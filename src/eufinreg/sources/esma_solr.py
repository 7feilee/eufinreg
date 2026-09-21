"""ESMA Registers application-to-application (A2A) interface.

ESMA exposes its public registers as read-only Apache Solr cores. There is no
authentication, no API key and no documented quota; the endpoint shape is::

    https://registers.esma.europa.eu/solr/<core>/select?q=...&wt=json

Documented at https://registers.esma.europa.eu/publication/helpApp

Paging uses Solr ``cursorMark`` (verified supported by the live endpoint), which
is stable under concurrent index updates in a way that ``start`` offsets are not.
A deterministic ``sort`` ending in the unique ``id`` field is required for
cursors, so that is what we send.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..flatten import FlattenConfig
from ..http import Fetcher
from .base import Query, Source

SOLR_BASE = "https://registers.esma.europa.eu/solr"
HELP_URL = "https://registers.esma.europa.eu/publication/helpApp"

#: Solr caps a single response well below this in practice; 1000 is the value
#: ESMA itself uses in every documented example.
MAX_PAGE_SIZE = 1000


@dataclass
class SolrSource(Source):
    """A single Solr core."""

    key: str = ""
    core: str = ""
    title: str = ""
    docs_url: str = HELP_URL
    enum_fields: tuple[str, ...] = ()
    block_structured: bool = False
    select_help: str = ""
    key_columns: tuple[str, ...] = ("id",)
    identifier_columns: tuple[str, ...] = ("id",)
    name_columns: tuple[str, ...] = ()
    expected_fields: tuple[str, ...] = ("id",)
    #: ESMA publishes no update cadence for the A2A endpoint; ae_lastUpdate
    #: is per record. Claiming a number here would be inventing one.
    cadence_hours: float | None = None
    #: ``entity_type`` values kept by default (block-structured cores only).
    default_doc_types: tuple[str, ...] = ()
    #: ``entity_type`` values added by ``--include-history``.
    history_doc_types: tuple[str, ...] = ()
    #: Parent field a ``--select`` value is matched against.
    select_field: str = ""
    #: Cursor paging requires a deterministic sort ending in the unique ``id``.
    #: For block-structured cores, leading with ``_root_`` makes each entity's
    #: parent and children contiguous in the stream instead of scattering them
    #: across pages — which is what lets ``--inspect`` sample whole entities.
    sort: str = "id asc"
    base_url: str = SOLR_BASE
    multi_value_fields: dict[str, str] = field(default_factory=dict)

    # -- URL -------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.core}/select"

    def flatten_config(self) -> FlattenConfig:
        return FlattenConfig()

    # -- query construction ----------------------------------------------

    def build_q(self, query: Query) -> str:
        """Translate ``--select`` / ``--query`` into Solr's ``q``.

        For block-structured cores, selecting on a *parent* field while also
        wanting the children back requires a join: the ``{!join}`` pulls the
        matching parents' ids and maps them onto every document whose ``_root_``
        points at them. This is the pattern ESMA's own examples use.
        """
        if query.raw_query:
            return query.raw_query
        if query.select and self.select_field:
            value = _quote_term(query.select)
            if self.block_structured:
                return f"{{!join from=id to=_root_}}{self.select_field}:{value}"
            return f"{self.select_field}:{value}"
        return "*:*"

    def build_fq(self, query: Query) -> list[str]:
        if not self.block_structured:
            return []
        types = list(self.default_doc_types)
        if query.include_history:
            types.extend(t for t in self.history_doc_types if t not in types)
        if not types:
            return []
        return [f"entity_type:({' OR '.join(types)})"]

    # -- reading ----------------------------------------------------------

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        rows = max(1, min(query.page_size, MAX_PAGE_SIZE))
        params: dict[str, Any] = {
            "q": self.build_q(query),
            "wt": "json",
            "rows": rows,
            "sort": self.sort,
            "cursorMark": "*",
        }
        fq = self.build_fq(query)
        if fq:
            params["fq"] = fq

        seen = 0
        page = 0
        cursor = "*"
        while True:
            page += 1
            params["cursorMark"] = cursor
            payload = fetcher.get_json(self.url, params, label=f"{self.key}-p{page:04d}")
            response = payload.get("response") or {}
            docs = response.get("docs") or []
            for doc in docs:
                yield doc
                seen += 1
                if query.max_docs and seen >= query.max_docs:
                    return
            next_cursor = payload.get("nextCursorMark")
            if not docs or not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        """Ask Solr to facet the field — exact over the whole core, one request."""
        params: dict[str, Any] = {
            "q": self.build_q(query),
            "wt": "json",
            "rows": 0,
            "facet": "true",
            "facet.field": field_name,
            "facet.limit": -1,
            "facet.mincount": 1,
        }
        fq = self.build_fq(query)
        if fq:
            params["fq"] = fq
        payload = fetcher.get_json(self.url, params, label=f"{self.key}-facet")
        buckets = ((payload.get("facet_counts") or {}).get("facet_fields") or {}).get(
            field_name
        ) or []
        pairs = [
            (str(buckets[i]), int(buckets[i + 1]))
            for i in range(0, len(buckets) - 1, 2)
            if buckets[i] is not None
        ]
        pairs.sort(key=lambda kv: (-kv[1], kv[0]))
        note = (
            "values come from the Solr index, which lower-cases these fields; "
            "matching is case-insensitive, so querying MIF and mif is equivalent"
        )
        return pairs, note


def _quote_term(value: str) -> str:
    """Quote a Solr term when it contains anything the parser would eat."""
    if value and all(c.isalnum() or c in "-_." for c in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# Concrete cores.
#
# Every core name, field name and default below was checked against the live
# endpoint. Cores that were not verified are deliberately absent — use
# ``--source solr:<core>`` to reach them.
# --------------------------------------------------------------------------

UPREG = SolrSource(
    key="upreg",
    identifier_columns=("ae_lei", "id"),
    name_columns=("ae_entityName", "ae_commercialName"),
    expected_fields=("ae_entityName", "ae_entityTypeCode", "ae_status", "_root_"),
    core="esma_registers_upreg",
    title="ESMA register of authorised entities (MiFID firms, AIFMs, UCITS mancos, "
    "crowdfunding providers, trading venues)",
    block_structured=True,
    select_field="ae_entityTypeCode",
    sort="_root_ asc,id asc",
    default_doc_types=("ae", "aeActivity", "aeNotHostMmbSt"),
    history_doc_types=("aeActivityHistory",),
    enum_fields=(
        "ae_entityTypeCode",
        "ae_entityTypeLabel",
        "ae_status",
        "ae_officeType",
        "ae_competentAuthority",
        "ae_homeMemberState",
        "ac_serviceName",
        "ac_status",
    ),
    select_help="entity type code, e.g. MIF, AIF, UCI, CSP — run --list-values ae_entityTypeCode",
)

BENCH_ENTITIES = SolrSource(
    key="bench-entities",
    core="esma_registers_bench_entities",
    title="Benchmark administrators (BMR)",
)

FUNDS = SolrSource(
    key="funds",
    core="esma_registers_funds",
    title="AIF / EuSEF / EuVECA funds",
)

MMF = SolrSource(
    key="mmf",
    core="esma_registers_mmf04",
    title="Money market funds (MMFR)",
)

SARIS = SolrSource(
    key="saris",
    core="esma_registers_saris_new",
    title="Suspensions and removals of financial instruments",
)

SOLR_SOURCES = (UPREG, BENCH_ENTITIES, FUNDS, MMF, SARIS)


def solr_source_for_core(core: str) -> SolrSource:
    """Escape hatch: address any core by name, with no baked-in assumptions."""
    if not core.startswith("esma_registers_"):
        core = f"esma_registers_{core}"
    return SolrSource(key=f"solr:{core}", core=core, title=f"ad-hoc Solr core {core}")
