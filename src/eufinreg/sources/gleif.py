"""GLEIF — the Legal Entity Identifier, and the key that joins everything else.

Every other register in this project is a list of who may do *something*. This
one is a list of who *is*: 3,403,760 legal entities worldwide, each with a
20-character LEI, a legal name, a registered address, a legal form, a status —
and, crucially, **the number it holds in its own national commercial register**.

That last field is why this source exists. ESMA's ``upreg`` and the MiCA CSVs
already carry an LEI, so this turns them into resolvable entities rather than
strings. And ``entity.registeredAs`` on a German record is the *Handelsregister*
number (``HRB 204159``) with ``entity.registeredAt.id`` naming the court's
authority code — a bridge to the German commercial register that BaFin's own
portal, being ``Disallow: /``, does not give you.

* API: ``https://api.gleif.org/api/v1/lei-records`` — JSON:API, no
  authentication, ``robots.txt`` is ``Disallow:`` (i.e. everything allowed).
* Data licence: **CC0** — GLEIF places the LEI data in the public domain, which
  makes it the only register here that is unambiguously redistributable.
* Cadence: a golden copy is published daily; the response's
  ``meta.goldenCopy.publishDate`` states which one you are reading.

**This register is the exception that proves the project's thesis.** Everything
else here publishes state and destroys history; GLEIF publishes *deltas* —
IntraDay, LastDay, LastWeek and LastMonth files listing exactly which records
changed. If every regulator did that, half this repository would be unnecessary.

Two limits, and GLEIF states both of them out loud, which is worth noting given
how many registers in this project do not:

* ``page[size]`` above 200 → HTTP 400, *"The page.size must be between 1 and
  200."* (Contrast EUDAMED, which silently caps at 300 and returns HTTP 200.)
* Page-based paging stops at **10,000 results** → HTTP 400 naming the limit.
  Every DACH country is over it — Germany alone has 254,108 — so a whole-country
  pull is not something this interface can do. Narrow the filter, or take the
  golden-copy bulk file: see :data:`GOLDEN_COPY_INDEX`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

API_URL = "https://api.gleif.org/api/v1/lei-records"
DOCS_URL = "https://www.gleif.org/en/lei-data/gleif-api"

#: The daily bulk files, for the whole-population pulls the API refuses. One
#: request returns an index of every current file: the full LEI set (3.4 M
#: records, 476 MB CSV), the relationship records, and — uniquely among the
#: registers here — day, week and month *delta* files.
GOLDEN_COPY_INDEX = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes?format=json"

#: Stated by the API in its own 400 response. Not guessed.
MAX_PAGE_SIZE = 200
RESULT_WINDOW = 10_000

COUNTRY_FILTER = "filter[entity.legalAddress.country]"

#: ``--select`` shorthands. DACH includes Liechtenstein: it shares Switzerland's
#: financial market in practice, and ESMA's own data lists Liechtenstein firms
#: under the FMA in Vaduz.
SELECT_ALIASES = {
    "dach": "DE,AT,CH,LI",
    "de": "DE",
    "at": "AT",
    "ch": "CH",
    "li": "LI",
}


def resolve_countries(select: str | None) -> str:
    """``--select`` → the value of the country filter, or ``''`` for no filter."""
    if select is None or not select.strip() or select.strip().upper() == "ALL":
        return ""
    token = select.strip().lower()
    if token in SELECT_ALIASES:
        return SELECT_ALIASES[token]
    codes = [part.strip().upper() for part in select.replace(";", ",").split(",") if part.strip()]
    bad = [code for code in codes if len(code) != 2 or not code.isalpha()]
    if bad:
        raise ValueError(
            f"--select takes ISO-3166 two-letter country codes or an alias "
            f"({', '.join(sorted(SELECT_ALIASES))}); got {', '.join(bad)}"
        )
    return ",".join(codes)


def parse_raw_query(raw: str | None) -> dict[str, str]:
    """``'filter[entity.status]=ACTIVE&…'`` → request parameters.

    A bare ``key=value`` without the ``filter[…]`` wrapper is wrapped, so
    ``--query 'entity.status=ACTIVE'`` works and reads better.
    """
    params: dict[str, str] = {}
    for chunk in (raw or "").split("&"):
        if not chunk.strip():
            continue
        if "=" not in chunk:
            raise ValueError(
                f"--query for gleif expects key=value pairs, e.g. "
                f"'entity.status=ACTIVE' or 'filter[entity.legalAddress.city]=Zug'; got {chunk!r}"
            )
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip()
        if not name.startswith("filter[") and not name.startswith("page["):
            name = f"filter[{name}]"
        params[name] = value
    return params


def flatten_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One LEI record → one flat row, dotted paths, GLEIF's own names kept.

    Nothing is renamed and no field list is hardcoded: a block GLEIF adds
    tomorrow turns up as a new column in ``--inspect`` rather than disappearing.
    """
    row: dict[str, Any] = {"lei": record.get("id", "")}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for name, child in value.items():
                walk(child, f"{path}.{name}" if path else name)
        elif isinstance(value, list):
            if not value:
                return
            if all(not isinstance(v, (Mapping, list)) for v in value):
                row[path] = " | ".join(str(v) for v in value if v not in (None, ""))
            else:
                row[path] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        elif value is not None:
            row[path] = value

    walk(record.get("attributes") or {}, "")
    return row


@dataclass
class GleifSource(Source):
    """The GLEIF LEI record API."""

    key: str = "gleif"
    title: str = (
        "GLEIF Legal Entity Identifier register — legal name, address, legal form, status "
        "and the entity's own national commercial-register number (CC0)"
    )
    docs_url: str = DOCS_URL
    jurisdiction: str = "EU"
    key_columns: tuple[str, ...] = ("lei",)
    identifier_columns: tuple[str, ...] = ("lei",)
    name_columns: tuple[str, ...] = ("entity.legalName.name",)
    expected_fields: tuple[str, ...] = (
        "lei",
        "entity.legalName.name",
        "entity.status",
        "registration.status",
    )
    enum_fields: tuple[str, ...] = (
        "entity.status",
        "registration.status",
        "entity.legalAddress.country",
        "entity.legalForm.id",
        "entity.category",
        "entity.registeredAt.id",
        "registration.corroborationLevel",
    )
    #: A golden copy is published daily; records carry their own last-update date.
    cadence_hours: float | None = 24.0
    data_licence: str = "CC0 1.0 — https://www.gleif.org/en/meta/lei-data-terms-of-use"
    disclaimer: str = (
        "GLEIF does not verify the accuracy of the data submitted by LEI issuers beyond the "
        "corroboration level recorded on each record; see registration.corroborationLevel."
    )
    select_help: str = (
        "country: an ISO-3166 code, a comma-separated list, or an alias "
        "(dach = DE,AT,CH,LI). Pushed server-side. Note the 10,000-result ceiling — "
        "every DACH country is over it on its own."
    )
    probe_query: str = "entity.legalAddress.country=LI"
    api_url: str = API_URL
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: ``(collected, total)`` when the 10,000-result ceiling truncated an answer.
    shortfall: tuple[int, int] | None = None
    #: Which daily golden copy the answers came from, as the API reports it.
    vintage: str = ""

    def reset(self) -> None:
        self._rows.clear()
        self.shortfall = None
        self.vintage = ""

    # -- reading -----------------------------------------------------------

    def build_params(self, query: Query) -> dict[str, str]:
        params: dict[str, str] = {}
        countries = resolve_countries(query.select)
        if countries:
            params[COUNTRY_FILTER] = countries
        params.update(parse_raw_query(query.raw_query))
        return params

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._rows:
            return self._rows[cache_key]

        params = self.build_params(query)
        size = max(1, min(query.page_size, MAX_PAGE_SIZE))
        rows: list[dict[str, Any]] = []
        total: int | None = None
        page = 1

        while True:
            payload = fetcher.get_json(
                self.api_url,
                {**params, "page[size]": size, "page[number]": page},
                label=f"{self.key}-p{page:04d}",
            )
            if not isinstance(payload, Mapping):
                raise FetchError(
                    f"{self.api_url} returned {type(payload).__name__}, not a JSON:API document",
                    url=self.api_url,
                )
            if payload.get("errors"):
                detail = "; ".join(
                    str(e.get("detail") or e.get("title")) for e in payload["errors"]
                )
                raise FetchError(f"GLEIF rejected the request: {detail}", url=self.api_url)

            meta = payload.get("meta") or {}
            if not self.vintage:
                self.vintage = str((meta.get("goldenCopy") or {}).get("publishDate", ""))
            if total is None:
                total = int((meta.get("pagination") or {}).get("total", 0) or 0)

            data = payload.get("data") or []
            for record in data:
                if isinstance(record, Mapping):
                    rows.append(flatten_record(record))
                if query.max_docs and len(rows) >= query.max_docs:
                    self._rows[cache_key] = rows
                    return rows

            if not data or len(rows) >= (total or 0):
                break
            if len(rows) + size > RESULT_WINDOW:
                # The next request would cross the ceiling and be refused with a
                # 400. Stop here and report the shortfall rather than fail.
                break
            page += 1

        if total is not None and len(rows) < total and not query.max_docs:
            self.shortfall = (len(rows), total)
        self._rows[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        if self.shortfall is None:
            return []
        got, total = self.shortfall
        return [
            f"GLEIF returned {got:,} of {total:,} matching records — page-based paging stops "
            f"at {RESULT_WINDOW:,}. Narrow the filter (a city, a legal form, "
            f"entity.status=ACTIVE), or take the daily bulk file: {GOLDEN_COPY_INDEX}"
        ]

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        pairs = count_values(rows, field_name)
        note = f"counted from the {len(rows):,} record(s) fetched"
        if self.vintage:
            note += f"; golden copy of {self.vintage}"
        if self.shortfall:
            note += f"; NOTE: only {self.shortfall[0]:,} of {self.shortfall[1]:,} were reachable"
        return pairs, note


GLEIF = GleifSource()

GLEIF_SOURCES = (GLEIF,)
