"""VIES — the EU VAT number validation service, i.e. "is this counterparty real".

A VAT number is the one identifier a company cannot fake for long: it is issued
by a tax authority, and misusing one has consequences no marketing page does.
VIES is the Commission's service for checking one, and it is the reference point
for the intra-EU reverse-charge rule — under Council Regulation (EU) 904/2010 a
supplier is expected to verify a customer's VAT number, and VIES is how.

* REST API: ``https://ec.europa.eu/taxation_customs/vies/rest-api`` — no
  authentication, no key.
* ``GET /ms/{country}/vat/{number}`` validates one number.
* ``GET /check-status`` reports which member states' systems are answering.

**It is a validator, not a directory.** There is no "list every VAT number in
Germany" and there should not be. This source therefore refuses to be archived
on a schedule (:attr:`bulk_readable` is false) and exists for two jobs: checking
a counterparty at onboarding, and producing evidence that the check was made.

The trap, and it is the one that matters:

    ``userError`` distinguishes **INVALID** from **MS_UNAVAILABLE**.

A member state's system being down is not the same as a number being wrong, and
a client that reads ``isValid: false`` without reading ``userError`` will record
a live company as unregistered. This source raises a warning on every
unavailable answer rather than letting it pass as a negative result.

Names and addresses come back only for numbers whose member state chooses to
disclose them; several return ``---`` by policy even for valid numbers, which is
a "we will not say", not a "no such company".
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

BASE_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api"
DOCS_URL = "https://ec.europa.eu/taxation_customs/vies/"

#: A VAT number as people write it: country prefix, then the national number.
#: Austria's carries a literal "U" that is part of the number, not the country.
_VAT = re.compile(r"^\s*([A-Za-z]{2})[\s-]*([0-9A-Za-z .\-]+)\s*$")

#: Northern Ireland trades under an EU VAT prefix post-Brexit; VIES answers for
#: it, and a client that filters on "EU member state" would wrongly reject it.
EXTRA_PREFIXES = ("XI", "EL")


def parse_vat(text: str) -> tuple[str, str]:
    """``'ATU12345678'`` → ``('AT', 'U12345678')``.

    Accepts spaces, dots and hyphens, because that is how VAT numbers appear on
    invoices and in the spreadsheets people paste from.
    """
    match = _VAT.match(text or "")
    number = re.sub(r"[\s.\-]", "", match.group(2)).upper() if match else ""
    # A national VAT number always contains digits and is at most 13 characters.
    # Without those two checks "Acme GmbH" parses as country "AC", number
    # "MEGMBH" and produces a confident request for a company that cannot exist.
    if not match or not any(ch.isdigit() for ch in number) or not 2 <= len(number) <= 13:
        raise ValueError(
            f"{text!r} is not a VAT number this can parse; expected a two-letter country "
            f"prefix followed by the national number, e.g. DE123456789 or ATU12345678"
        )
    return match.group(1).upper(), number


def parse_targets(raw_query: str | None) -> list[str]:
    """``--query`` → the VAT numbers to check.

    Accepts ``vat=DE123456789``, a bare ``DE123456789``, and comma-separated
    lists of either, because a compliance list arrives as a column of numbers.
    """
    text = (raw_query or "").strip()
    if not text:
        return []
    values: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" in item:
            name, _, value = item.partition("=")
            if name.strip().lower() not in ("vat", "vat_number", "number"):
                raise ValueError(
                    f"unknown vies criterion {name.strip()!r}; use vat=DE123456789 "
                    f"or just the number"
                )
            item = value.strip()
        if item:
            values.append(item)
    return values


def flatten_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One validation answer → one flat row, VIES's own field names kept."""
    row: dict[str, Any] = {}
    for name, value in payload.items():
        if isinstance(value, Mapping):
            for inner, inner_value in value.items():
                row[f"{name}.{inner}"] = "" if inner_value is None else inner_value
        elif isinstance(value, list):
            row[name] = " | ".join(str(v) for v in value)
        else:
            row[name] = "" if value is None else value
    return row


@dataclass
class ViesSource(Source):
    """The Commission's VAT validation service."""

    key: str = "vies"
    title: str = (
        "EU VAT number validation (VIES) — check one counterparty's VAT registration; "
        "validator, not a directory"
    )
    docs_url: str = DOCS_URL
    jurisdiction: str = "EU"
    key_columns: tuple[str, ...] = ("countryCode", "vatNumber")
    identifier_columns: tuple[str, ...] = ("vatNumber",)
    name_columns: tuple[str, ...] = ("name",)
    expected_fields: tuple[str, ...] = ("isValid", "vatNumber", "requestDate")
    enum_fields: tuple[str, ...] = ("isValid", "userError", "countryCode", "availability")
    #: A live service. "Cadence" here means how long an answer stays worth
    #: reusing, not how often a file is regenerated.
    cadence_hours: float | None = 24.0
    #: There is no population to archive: the service answers one number at a
    #: time, by design, and a scheduled crawl of it would be abuse.
    bulk_readable: bool = False
    disclaimer: str = (
        "The Commission states that VIES is a search engine over national databases, not a "
        "database of its own; the data is supplied by the member states and its accuracy is "
        "their responsibility. A valid answer confirms registration, not solvency, activity "
        "or identity."
    )
    personal_data: str = (
        "A sole trader's VAT registration carries their name and address, so a check can "
        "return personal data even though the subject is a business."
    )
    select_help: str = (
        "STATUS reports which member states' systems are answering right now. "
        "Otherwise use --query 'vat=DE123456789' (repeatable, comma-separated)."
    )
    probe_query: str = "vat=DE123456789"
    base_url: str = BASE_URL
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: Countries whose system was unavailable during the last run.
    unavailable: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self._rows.clear()
        self.unavailable = []

    # -- reading -----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._rows:
            return self._rows[cache_key]

        if query.select and query.select.strip().upper() in ("STATUS", "CHECK-STATUS"):
            rows = self._status(fetcher)
            self._rows[cache_key] = rows
            return rows

        targets = parse_targets(query.raw_query)
        if not targets:
            raise ValueError(
                "vies validates one VAT number at a time and has no 'list everything' mode. "
                "Give it something: --query 'vat=DE123456789', or --select STATUS to see "
                "which member states are answering."
            )

        rows: list[dict[str, Any]] = []
        for target in targets:
            country, number = parse_vat(target)
            payload = fetcher.get_json(
                f"{self.base_url}/ms/{country}/vat/{number}", label=f"{self.key}-{country}"
            )
            if not isinstance(payload, Mapping):
                raise FetchError(
                    f"VIES returned {type(payload).__name__} for {target}", url=self.base_url
                )
            row = flatten_check(payload)
            row.setdefault("countryCode", country)
            # The whole point: an unavailable member state is not a negative
            # answer, and must never be filed as one.
            if str(row.get("userError", "")).upper() in (
                "MS_UNAVAILABLE",
                "SERVICE_UNAVAILABLE",
                "TIMEOUT",
                "MS_MAX_CONCURRENT_REQ",
            ):
                self.unavailable.append(f"{country} ({row['userError']})")
            rows.append(row)
            if query.max_docs and len(rows) >= query.max_docs:
                break

        self._rows[cache_key] = rows
        return rows

    def _status(self, fetcher: Fetcher) -> list[dict[str, Any]]:
        """One row per member state: is its system answering right now?"""
        payload = fetcher.get_json(f"{self.base_url}/check-status", label=f"{self.key}-status")
        if not isinstance(payload, Mapping):
            raise FetchError("VIES check-status did not return an object", url=self.base_url)
        rows = [
            {
                "countryCode": entry.get("countryCode", ""),
                "availability": entry.get("availability", ""),
                "vow_available": (payload.get("vow") or {}).get("available", ""),
            }
            for entry in payload.get("countries", [])
            if isinstance(entry, Mapping)
        ]
        self.unavailable = [
            r["countryCode"] for r in rows if str(r["availability"]).lower() != "available"
        ]
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        if not self.unavailable:
            return []
        return [
            f"VIES could not reach {', '.join(sorted(set(self.unavailable)))} — those answers "
            f"are 'we do not know', not 'not registered'. Filing them as invalid would record "
            f"a live company as unregistered; retry rather than conclude."
        ]

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        return count_values(rows, field_name), f"counted from {len(rows)} check(s)"


VIES = ViesSource()

VIES_SOURCES = (VIES,)
