"""CTIS — the EU Clinical Trials Information System (Regulation 536/2014).

Since 2022 every clinical trial run in the EU/EEA must be authorised through
CTIS, and the public portal publishes the result. Read as a company list, it
answers a question no commercial database answers as well: **who is actually
running clinical trials in Europe, in which countries, on what** — sponsors are
pharmaceutical companies, CROs, hospitals and universities, and the sponsor
type is a field.

* Public portal: https://euclinicaltrials.eu/ctis-public/search
* Interface: ``POST /ctis-public-api/search``, no authentication. The portal's
  own JS bundle names it, along with an asynchronous CSV export
  (``/ctis-public-api/search/download``, which returns a ``taskId`` to poll and
  is *not* used here) and ``/ct-public-api-services/services``.

Two things to know before trusting a row count:

* ``searchCriteria`` must be present, even empty. Send only ``pagination`` and
  the API answers ``200`` with ``totalRecords: 0`` — a perfectly well-formed
  "no results" for a request that should have matched everything.
* **Paging stops at 10,000 records** while ``totalRecords`` keeps reporting the
  true total. Verified: page 100 at size 100 returns a full page, page 101
  returns none; page 20 at size 500 is fine, page 21 is empty. This source
  raises the shortfall as a warning rather than quietly returning a prefix.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

SEARCH_URL = "https://euclinicaltrials.eu/ctis-public-api/search"
PUBLIC_SITE = "https://euclinicaltrials.eu/ctis-public/search"

#: Verified working; 500 keeps a full pull to 20 requests.
MAX_PAGE_SIZE = 500

#: The deep-paging window. Records beyond this are simply unreachable through
#: the search endpoint, whatever ``totalRecords`` claims.
RESULT_WINDOW = 10_000

#: Unique per trial, so paging on it is deterministic.
SORT_PROPERTY = "ctNumber"

#: Cells that pack several values into one field.
MULTI_VALUE = {"trialCountries": " | ", "therapeuticAreas": " | "}


def build_criteria(raw_query: str | None) -> dict[str, Any]:
    """``--query`` → the ``searchCriteria`` object.

    A value starting with ``{`` is parsed and passed through as the whole
    criteria object, which is the escape hatch for filters this package does not
    model. Anything else is the portal's own free-text search (``containAll``) —
    including a value that merely looks JSON-ish, since searching for
    ``[cancer]`` is a reasonable thing to want.
    """
    if not raw_query or not raw_query.strip():
        return {}
    text = raw_query.strip()
    if not text.startswith("{"):
        return {"containAll": text}
    try:
        # A JSON document starting with '{' always decodes to an object.
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"--query looks like JSON but does not parse: {exc}") from exc


def flatten_trial(record: Mapping[str, Any]) -> dict[str, Any]:
    """One trial record → one flat row, CTIS field names kept verbatim."""
    row: dict[str, Any] = {}
    for name, value in record.items():
        if value is None:
            row[name] = ""
        elif isinstance(value, (str, int, float, bool)):
            row[name] = value
        elif isinstance(value, list):
            row[name] = " | ".join(str(v) for v in value if v not in (None, ""))
        else:
            row[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return row


@dataclass
class CtisSource(Source):
    """The CTIS public trial search."""

    key: str = "ctis"
    title: str = (
        "EU Clinical Trials Information System — authorised trials and their sponsors "
        "(Regulation 536/2014)"
    )
    docs_url: str = PUBLIC_SITE
    enum_fields: tuple[str, ...] = (
        "sponsorType",
        "trialPhase",
        "trialCountries",
        "therapeuticAreas",
        "ctStatus",
        "trialRegion",
        "resultsFirstReceived",
    )
    multi_value_fields: dict[str, str] = field(default_factory=lambda: dict(MULTI_VALUE))
    block_structured: bool = False
    select_help: str = (
        "not supported — CTIS has no server-side selector this package models; "
        "use --query for free text or a raw searchCriteria JSON object"
    )
    search_url: str = SEARCH_URL
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: Set when the deep-paging window cut a result set short. Read by the CLI.
    shortfall: tuple[int, int] | None = None

    def reset(self) -> None:
        self._rows.clear()
        self.shortfall = None

    def build_body(self, query: Query, *, page: int, size: int) -> dict[str, Any]:
        """The POST body. ``searchCriteria`` is mandatory even when empty."""
        if query.select:
            raise ValueError(
                f"{self.key} does not support --select; use --query 'some text' for the "
                f'portal\'s free-text search, or --query \'{{"containAll": "…"}}\''
            )
        return {
            "pagination": {"page": page, "size": size},
            "sort": {"property": SORT_PROPERTY, "direction": "ASC"},
            "searchCriteria": build_criteria(query.raw_query),
        }

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._rows:
            return self._rows[cache_key]

        size = max(1, min(query.page_size, MAX_PAGE_SIZE))
        rows: list[dict[str, Any]] = []
        total: int | None = None
        page = 1  # CTIS pages are 1-based
        while True:
            body = self.build_body(query, page=page, size=size)
            payload = fetcher.post_json(self.search_url, body, label=f"{self.key}-p{page:04d}")
            if not isinstance(payload, Mapping):
                raise FetchError(
                    f"{self.search_url} returned {type(payload).__name__}, not a result object",
                    url=self.search_url,
                )
            pagination = payload.get("pagination") or {}
            if total is None and isinstance(pagination.get("totalRecords"), int):
                total = pagination["totalRecords"]
            data = payload.get("data") or []
            for record in data:
                if isinstance(record, Mapping):
                    rows.append(flatten_trial(record))
                if query.max_docs and len(rows) >= query.max_docs:
                    self._rows[cache_key] = rows
                    return rows
            if not data or not pagination.get("nextPage"):
                break
            page += 1

        # Paging past 10,000 returns an empty page, not an error. Without this
        # check the caller sees a clean run that quietly dropped the tail.
        if total is not None and len(rows) < total and not query.max_docs:
            self.shortfall = (len(rows), total)
        self._rows[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        """Anything the caller must be told about the last result set."""
        if self.shortfall is None:
            return []
        got, total = self.shortfall
        return [
            f"CTIS returned {got} of {total} matching trials — its search stops at "
            f"{RESULT_WINDOW:,} records however you page. Narrow the result set with "
            f"--query to reach the rest."
        ]

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        separator = self.multi_value_fields.get(field_name)
        pairs = count_values(rows, field_name, split=separator)
        note = f"counted from all {len(rows)} fetched record(s), not a sample"
        if separator:
            note += f"; cells split on {separator.strip()!r}"
        if self.shortfall:
            note += f"; NOTE: only {self.shortfall[0]} of {self.shortfall[1]} trials were reachable"
        return pairs, note


CTIS = CtisSource()

CTIS_SOURCES = (CTIS,)
