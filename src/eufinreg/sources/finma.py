"""FINMA — the Swiss financial-market authorisation holders (Switzerland).

Switzerland is not in the EEA, so none of the EU-level registers this project
already reads contain a Swiss licence. FINMA supervises the lot — banks,
insurers, securities firms, fund management companies, portfolio managers,
trustees, trading venues — and publishes the whole authorisation list as a
**file at a stable URL**, which is the same interface shape as the MiCA CSVs and
a better one than any search form.

* Landing page:
  https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/
* Interface: ``GET`` a CSV. No authentication, no key, no session.
* ``robots.txt``: ``www.finma.ch/robots.txt`` on 2026-08-16 is ``Allow: /`` with
  six ``Disallow`` rules — ``/suche/``, ``/sitemap/``, ``/error/``,
  ``/sitecore/media library/`` and the **insurance-intermediary register
  search**. The published files live under ``/~/media/…``, the public media
  alias, which no rule matches. Contrast BaFin, whose whole portal host is
  ``Disallow: /``.

Two things about the URL:

* The links on FINMA's page carry ``?sc_lang=en&hash=…`` — a Sitecore media
  hash. It is **not required**: the bare path returns the same file, so this
  source does not have to scrape the page for a hash that changes when the file
  does.
* ``Last-Modified`` is the real vintage. On 2026-08-16 it was
  ``Sun, 16 Aug 2026 03:05:12 GMT``, i.e. regenerated that morning.

The file is one row per **authorisation**, not per institution: Zürcher
Kantonalbank appears twice, once as ``Bank`` and once as ``Custodian bank``.
2,938 rows on 2026-08-16 describe 2,744 distinct UIDs. This source therefore
emits the rows block-structured (an institution parent, its authorisations as
children) so the standard flattener collapses them into one row per institution
— the same treatment the ESMA ``upreg`` core gets, for the same reason.

The ``UID`` column is the join key that makes this data worth more than it looks:
it is the Swiss federal business identification number, so every row can be
looked up in the UID register (``--source ch-uid``) for legal form, commercial
register status and VAT status.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..flatten import INTERNAL_FIELDS, FlattenConfig
from ..http import Fetcher
from ..profile import count_values
from .base import Query, Source

LANDING = "https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/"

#: The master list: every authorisation holder with its UID. Other files on the
#: same page are per-category XLSX/PDF (``beh`` securities firms, ``vu``
#: insurers, ``sro`` self-regulatory organisations …) and carry a few extra
#: columns each; this one is the only CSV that spans every category.
UID_CSV_URL = "https://www.finma.ch/en/~/media/finma/dokumente/bewilligungstraeger/csv/uid.csv"

#: Semicolon-separated, quoted, UTF-8 with a BOM. Verified 2026-08-16.
DELIMITER = ";"

#: ``--select`` shorthands, matched against ``AuthorisationTypeEN``. Anything
#: else is taken as a case-insensitive substring of that column, so a category
#: this map does not know still works.
SELECT_ALIASES = {
    "bank": "bank",
    "banks": "bank",
    "insurer": "insur",
    "insurers": "insur",
    "portfolio-manager": "portfolio manager",
    "pm": "portfolio manager",
    "trustee": "trustee",
    "fund": "fund management company",
    "securities-firm": "securities firm",
    "trading-venue": "trading venue",
    "sro": "self-regulatory",
    "supervisory-organisation": "supervisory organisation",
}

_UID_DIGITS = re.compile(r"\d+")


def uid_digits(uid: str) -> str:
    """``'CHE-101.329.561'`` → ``'101329561'``.

    The printed UID is punctuated; the UID register's own web service wants the
    nine digits. Emitting both is what lets ``finma`` rows be fed straight into
    ``--source ch-uid``.
    """
    return "".join(_UID_DIGITS.findall(uid or ""))


def parse_csv(text: str, *, delimiter: str = DELIMITER) -> list[dict[str, str]]:
    """Parse FINMA's CSV into dicts, keeping its column names verbatim."""
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {
            (name or "").strip(): (value or "").strip()
            for name, value in raw.items()
            if name and isinstance(value, str)
        }
        if any(row.values()):
            rows.append(row)
    return rows


def matches_select(row: Mapping[str, str], needles: list[str]) -> bool:
    if not needles:
        return True
    haystack = " ".join(
        str(row.get(name, "")).lower() for name in ("AuthorisationTypeEN", "AuthorisationTypeDE")
    )
    return any(needle in haystack for needle in needles)


def resolve_select(select: str | None) -> list[str]:
    """``--select`` → lower-cased substrings to match on the authorisation type."""
    if select is None or not select.strip() or select.strip().upper() == "ALL":
        return []
    needles = []
    for part in select.replace(";", ",").split(","):
        token = part.strip().lower()
        if token:
            needles.append(SELECT_ALIASES.get(token, token))
    return needles


def entity_key(row: Mapping[str, str]) -> str:
    """Grouping key: the UID, falling back to name + city.

    84 of 2,938 rows on 2026-08-16 carry no UID — mostly foreign representative
    offices. Falling back to the name keeps them as their own institution rather
    than collapsing all of them into one empty-keyed row.
    """
    uid = (row.get("UID") or "").strip()
    if uid:
        return uid
    return f"{row.get('Name', '')}|{row.get('City', '')}".strip("|") or "?"


@dataclass
class FinmaSource(Source):
    """FINMA's list of authorisation holders, as published."""

    key: str = "finma"
    title: str = (
        "FINMA authorisation holders (Switzerland) — banks, insurers, securities firms, "
        "fund managers, portfolio managers, trustees, trading venues"
    )
    docs_url: str = LANDING
    jurisdiction: str = "CH"
    key_columns: tuple[str, ...] = ("UID", "Name")
    identifier_columns: tuple[str, ...] = ("UID", "uid_digits")
    name_columns: tuple[str, ...] = ("Name",)
    expected_fields: tuple[str, ...] = ("Name", "UID", "authorisation_AuthorisationTypeEN")
    #: Observed regenerated daily in the early morning (Last-Modified
    #: 03:05 UTC on 2026-08-16). FINMA publishes no cadence statement.
    cadence_hours: float | None = 24.0
    enum_fields: tuple[str, ...] = (
        "authorisation_AuthorisationTypeEN",
        "authorisation_AuthorisationTypeDE",
        "City",
    )
    block_structured: bool = True
    select_help: str = (
        "authorisation type: ALL (default), an alias (bank, insurer, portfolio-manager, "
        "trustee, fund, securities-firm, trading-venue, sro), or any substring of "
        "AuthorisationTypeEN. Filtered after the download — the interface is one file."
    )
    url: str = UID_CSV_URL
    _cache: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def reset(self) -> None:
        self._cache.clear()

    def flatten_config(self) -> FlattenConfig:
        # The parent/child markers are synthesised here rather than served by
        # FINMA, so they are bookkeeping and do not belong in the output.
        return FlattenConfig(
            child_group_field="entity_type",
            drop_fields=frozenset({*INTERNAL_FIELDS, "id", "type_s", "entity_type", "_root_"}),
        )

    # -- reading -----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs, query.extra.get("no_derived"))
        if cache_key in self._cache:
            return self._cache[cache_key]
        if query.raw_query:
            raise ValueError(
                f"{self.key} has no server-side query — the interface is a single CSV. "
                f"Use --select for the authorisation type and --field / --contains for the rest."
            )

        needles = resolve_select(query.select)
        text = fetcher.get_text(self.url, label=self.key)
        rows = parse_csv(text)

        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        derived = not query.extra.get("no_derived")
        for index, row in enumerate(rows):
            if not matches_select(row, needles):
                continue
            key = entity_key(row)
            if key not in seen:
                seen.add(key)
                parent: dict[str, Any] = {
                    "id": key,
                    "type_s": "parent",
                    "entity_type": "institution",
                    "_root_": key,
                    "Name": row.get("Name", ""),
                    "City": row.get("City", ""),
                    "UID": row.get("UID", ""),
                }
                if derived:
                    parent["uid_digits"] = uid_digits(row.get("UID", ""))
                docs.append(parent)
            child = {
                "id": f"{key}#{index}",
                "type_s": "child",
                "entity_type": "authorisation",
                "_root_": key,
            }
            for name, value in row.items():
                if name.startswith("AuthorisationType"):
                    child[name] = value
            docs.append(child)
            if query.max_docs and len(docs) >= query.max_docs:
                break

        self._cache[cache_key] = docs
        return docs

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        docs = self._load(fetcher, query)
        # The flattened column names are what --inspect shows, so accept both
        # those and the raw CSV names rather than making the caller guess.
        plain = field_name.split("authorisation_", 1)[-1]
        pairs = count_values(docs, field_name) or count_values(docs, plain)
        note = (
            "counted from the whole file; a row is one authorisation, so an institution "
            "holding several is counted once per authorisation"
        )
        return pairs, note


FINMA = FinmaSource()

FINMA_SOURCES = (FINMA,)
