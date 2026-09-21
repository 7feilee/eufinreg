"""TED — who is buying with public money, and who won.

Public procurement is the strongest leading indicator in this repository. A
contract award is published because the money is not the buyer's own, it names
the winning company, and it happens **months before** the hiring, the factory or
the press release that follows from it. Where a licence register tells you who
*may* trade, TED tells you who just *got paid to*.

* API: ``POST https://api.ted.europa.eu/v3/notices/search`` — no authentication.
* Query language: TED's own expert syntax, e.g.
  ``notice-type IN (can-standard) AND buyer-country IN (DEU AUT) AND
  publication-date>=today(-30)``.
* Coverage: EU and EEA. **Switzerland is not in TED** — it is not an EEA member
  and its procurement is published nationally (simap.ch). A DACH answer from
  this source is therefore two-thirds complete by construction, which is worth
  saying out loud rather than discovering later.

Three things to know:

* **``limit`` is capped at 250**, and the API says so: ``Value (300) of parameter
  'limit' exceeds maximum allowed value (250)``. Refreshing to see a register
  state its own limits is becoming a theme.
* **Only ``POST`` works.** ``GET /v3/notices/search`` answers
  ``405 Request method 'GET' is not supported``, and the neighbouring
  ``/fields`` endpoint *does* require an ``Authorization`` header while
  ``/search`` does not — so an auth failure on one endpoint says nothing about
  the other.
* **Text fields are multilingual objects**: ``{"deu": ["Polizei Berlin"]}``.
  There is no single "name" string. This source picks a language in a documented
  order and joins the rest rather than silently taking whichever key came first.

``winner-name`` repeats once per lot, so a notice awarding six lots to three
companies lists six names. They are de-duplicated here, in order, and the
original count is kept in ``winner_count``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
DOCS_URL = "https://docs.ted.europa.eu/api/index.html"

#: Stated by the API in its own 400 response.
MAX_LIMIT = 250

#: TED uses ISO 3166-1 **alpha-3**, not the alpha-2 every other source here
#: uses. Mixing them up returns zero notices and no error.
COUNTRY_ALPHA3 = {
    "de": "DEU",
    "at": "AUT",
    "fr": "FRA",
    "it": "ITA",
    "nl": "NLD",
    "be": "BEL",
    "pl": "POL",
    "es": "ESP",
    "lu": "LUX",
    "li": "LIE",
    "no": "NOR",
    "ie": "IRL",
    "cz": "CZE",
}
#: Switzerland is deliberately absent: TED does not cover it.
DACH_ALPHA3 = ("DEU", "AUT")

#: Enough to be useful, small enough not to pull a novel per notice. `links`
#: alone is ~2 KB of PDF URLs in 24 languages and is excluded on purpose.
DEFAULT_FIELDS = (
    "publication-number",
    "publication-date",
    "notice-type",
    "buyer-name",
    "buyer-country",
    "winner-name",
    "notice-title",
    "classification-cpv",
    "deadline-receipt-request",
)

#: Which language to prefer when a field is a multilingual object. English
#: first because it is the only one present across all member states; German
#: next because this tool is pointed at DACH.
LANGUAGE_ORDER = ("eng", "deu", "fra", "ita", "nld")


def resolve_countries(select: str | None) -> list[str]:
    """``--select`` → alpha-3 country codes for ``buyer-country``."""
    if select is None or not select.strip() or select.strip().upper() == "ALL":
        return []
    token = select.strip().lower()
    if token == "dach":
        return list(DACH_ALPHA3)
    codes: list[str] = []
    for part in select.replace(";", ",").split(","):
        item = part.strip().lower()
        if not item:
            continue
        if len(item) == 3 and item.isalpha():
            codes.append(item.upper())
        elif item in COUNTRY_ALPHA3:
            codes.append(COUNTRY_ALPHA3[item])
        else:
            raise ValueError(
                f"--select takes ISO-3166 alpha-3 codes (DEU, AUT), a two-letter code this "
                f"knows how to convert, or 'dach'; got {part.strip()!r}"
            )
    return codes


def build_query(select: str | None, raw_query: str | None) -> str:
    """Combine ``--select`` and ``--query`` into one TED expert query."""
    clauses: list[str] = []
    countries = resolve_countries(select)
    if countries:
        clauses.append(f"buyer-country IN ({' '.join(countries)})")
    if raw_query and raw_query.strip():
        clauses.append(f"({raw_query.strip()})")
    if not clauses:
        # TED requires a query; asking for the entire archive is neither useful
        # nor polite, so make the caller say what they want.
        raise ValueError(
            "ted needs a query. Try --select dach, or --query "
            '"notice-type IN (can-standard) AND publication-date>=today(-7)".'
        )
    return " AND ".join(clauses)


def pick_language(value: Mapping[str, Any]) -> str:
    """A multilingual field → one string, preferring a documented language order."""
    for language in LANGUAGE_ORDER:
        if value.get(language):
            return _join(value[language])
    for candidate in value.values():
        if candidate:
            return _join(candidate)
    return ""


def _join(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, Mapping):
        # A shape TED has not used before. Keep it as JSON so it shows up in
        # --inspect rather than as a Python repr nothing can parse.
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def dedupe(values: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


def flatten_notice(record: Mapping[str, Any]) -> dict[str, Any]:
    """One notice → one flat row, TED's own field names kept."""
    row: dict[str, Any] = {}
    for name, value in record.items():
        if name == "links":
            continue  # ~2 KB of PDF URLs in 24 languages, per notice
        if isinstance(value, Mapping):
            text = pick_language(value)
            if name == "winner-name":
                parts = dedupe([p.strip() for p in text.split(" | ")])
                row["winner_count"] = len(parts)
                row[name] = " | ".join(parts)
            else:
                row[name] = text
        elif isinstance(value, list):
            row[name] = " | ".join(str(v) for v in dedupe([str(v) for v in value]))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            row[name] = "" if value is None else value
        else:
            row[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return row


@dataclass
class TedSource(Source):
    """TED notice search."""

    key: str = "ted"
    title: str = (
        "TED — EU public procurement notices: who is buying with public money and which "
        "company won. EU/EEA only, so no Switzerland"
    )
    docs_url: str = DOCS_URL
    jurisdiction: str = "EU"
    key_columns: tuple[str, ...] = ("publication-number",)
    identifier_columns: tuple[str, ...] = ()
    name_columns: tuple[str, ...] = ("winner-name", "buyer-name")
    expected_fields: tuple[str, ...] = ("publication-number", "publication-date", "notice-type")
    enum_fields: tuple[str, ...] = ("notice-type", "buyer-country", "classification-cpv")
    multi_value_fields: dict[str, str] = field(
        default_factory=lambda: {"winner-name": " | ", "classification-cpv": " | "}
    )
    cadence_hours: float | None = 24.0
    disclaimer: str = (
        "TED republishes notices as submitted by contracting authorities; the Publications "
        "Office does not warrant their accuracy or completeness."
    )
    select_help: str = (
        "buyer country: 'dach' (DEU AUT — TED does not cover Switzerland), an alpha-3 code, "
        "or a comma-separated list. ANDed with --query, which takes TED's expert syntax."
    )
    probe_query: str = "notice-type IN (can-standard) AND publication-date>=today(-30)"
    search_url: str = SEARCH_URL
    fields: tuple[str, ...] = DEFAULT_FIELDS
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: ``(collected, total)`` when a run stopped before the whole result set.
    shortfall: tuple[int, int] | None = None

    def reset(self) -> None:
        self._rows.clear()
        self.shortfall = None

    # -- reading -----------------------------------------------------------

    def build_body(self, query: Query, *, page: int, limit: int) -> dict[str, Any]:
        return {
            "query": build_query(query.select, query.raw_query),
            "fields": list(self.fields),
            "limit": limit,
            "page": page,
            "paginationMode": "PAGE_NUMBER",
        }

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._rows:
            return self._rows[cache_key]

        limit = max(1, min(query.page_size, MAX_LIMIT))
        rows: list[dict[str, Any]] = []
        total: int | None = None
        page = 1

        while True:
            body = self.build_body(query, page=page, limit=limit)
            payload = fetcher.post_json(self.search_url, body, label=f"{self.key}-p{page:04d}")
            if not isinstance(payload, Mapping):
                raise FetchError(
                    f"{self.search_url} returned {type(payload).__name__}, not a result object",
                    url=self.search_url,
                )
            if payload.get("message") and not payload.get("notices"):
                raise FetchError(
                    f"TED rejected the query: {payload['message']}", url=self.search_url
                )

            if total is None:
                total = payload.get("totalNoticeCount")
            notices = payload.get("notices") or []
            for notice in notices:
                if isinstance(notice, Mapping):
                    rows.append(flatten_notice(notice))
                if query.max_docs and len(rows) >= query.max_docs:
                    self._rows[cache_key] = rows
                    return rows

            if not notices or (isinstance(total, int) and len(rows) >= total):
                break
            page += 1

        if isinstance(total, int) and len(rows) < total and not query.max_docs:
            self.shortfall = (len(rows), total)
        self._rows[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.shortfall:
            got, total = self.shortfall
            notes.append(
                f"TED returned {got:,} of {total:,} matching notices. Narrow the window with "
                f"publication-date>=today(-N) rather than paging further."
            )
        return notes

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        separator = self.multi_value_fields.get(field_name)
        pairs = count_values(rows, field_name, split=separator)
        note = f"counted from all {len(rows):,} fetched notice(s)"
        if separator:
            note += f"; cells split on {separator.strip()!r}"
        return pairs, note


TED = TedSource()

TED_SOURCES = (TED,)
