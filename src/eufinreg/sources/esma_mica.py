"""ESMA interim MiCA register (Articles 109 & 110 MiCA).

There is no API here — ESMA publishes the register as a handful of CSV files
that are regenerated weekly. That *is* the machine-to-machine interface, and it
is a perfectly good one: stable URLs, stable file names, ``Last-Modified``
headers, and an accompanying CSV that documents every field.

Landing page:
https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/markets-crypto-assets-regulation-mica

The ``2024-12`` path segment is the Drupal upload folder, not the content date.
The files are overwritten in place; check ``Last-Modified`` (exposed in the
``--raw`` manifest) for the real vintage.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher
from ..profile import count_values
from .base import Query, Source

MICA_BASE = "https://www.esma.europa.eu/sites/default/files/2024-12"
MICA_LANDING = (
    "https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/"
    "markets-crypto-assets-regulation-mica"
)
FIELD_DESCRIPTIONS_URL = f"{MICA_BASE}/Description_of_the_fields_in_the_interim_MiCA_register.csv"

#: MiCA Art. 3(1)(16) crypto-asset services, keyed by the letter used in the
#: Regulation. National authorities are supposed to file the letter form into
#: ``ac_serviceCode``; in practice they file free text. See ``normalise_services``.
MICA_SERVICE_LETTERS = {
    "a": "providing custody and administration of crypto-assets on behalf of clients",
    "b": "operation of a trading platform for crypto-assets",
    "c": "exchange of crypto-assets for funds",
    "d": "exchange of crypto-assets for other crypto-assets",
    "e": "execution of orders for crypto-assets on behalf of clients",
    "f": "placing of crypto-assets",
    "g": "reception and transmission of orders for crypto-assets on behalf of clients",
    "h": "providing advice on crypto-assets",
    "i": "providing portfolio management on crypto-assets",
    "j": "providing transfer services for crypto-assets on behalf of clients",
}

# Distinctive keywords, checked in order, used only when a cell carries no
# letter prefix. Order matters, and the overlaps are the whole problem:
# "exchange between crypto assets and fiat currency" is (c), while "exchange
# between crypto assets" is (d) — so every fiat/funds phrasing must be tested
# before the bare crypto-to-crypto one.
_SERVICE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("c", "and fiat"),
    ("c", "for fiat"),
    ("c", "for funds"),
    ("d", "for other crypto"),
    ("d", "between crypto assets"),
    ("d", "between crypto-assets"),
    ("b", "trading platform"),
    ("a", "custody and administration"),
    ("j", "transfer services"),
    ("i", "portfolio management"),
    ("h", "advice"),
    ("g", "reception and transmission"),
    ("e", "execution of orders"),
    ("f", "placing"),
)

_LETTER_PREFIX = re.compile(r"^\(?([a-j])[).\s\t]", re.IGNORECASE)

# Every separator observed in the live file. The lookahead-guarded ones only
# split where a MiCA letter marker follows, so that a comma or an "I" inside a
# service description is left alone.
_SEPARATORS = re.compile(
    r"""
      \s*\|\s*                       # the documented separator
    | [\r\n]+                        # embedded newlines
    | \s*/\s*                        # slash-separated lists
    | \s*\bI\s+(?=[a-j][.)\s])       # capital I standing in for a pipe
    | \s*[,;]\s*(?=[a-j][.)]\s)      # comma/semicolon before a letter marker
    | (?<=[a-z])\s*\.\s*(?=[a-j][.)]\s)  # "…clients.g. reception…"
    """,
    re.VERBOSE,
)


def split_service_cell(cell: str) -> list[str]:
    """Split one ``ac_serviceCode`` cell into individual service statements.

    The documented separator is ``|``. Observed in the live file: ``|``, embedded
    newlines, a capital ``I`` standing in for the pipe, ``/``, commas, and a full
    stop butted straight against the next letter marker.
    """
    if not cell:
        return []
    normalised = _SEPARATORS.sub("|", cell)
    return [part.strip(" \t.;,") for part in normalised.split("|") if part.strip(" \t.;,")]


def service_letter(fragment: str) -> str | None:
    """Map one free-text service statement onto its MiCA letter, or ``None``."""
    text = fragment.strip()
    if not text:
        return None
    match = _LETTER_PREFIX.match(text)
    if match:
        return match.group(1).lower()
    lowered = " ".join(text.lower().split())
    if len(lowered) == 1 and lowered in MICA_SERVICE_LETTERS:
        return lowered
    for letter, keyword in _SERVICE_KEYWORDS:
        if keyword in lowered:
            return letter
    return None


def normalise_services(cell: str) -> str:
    """Pipe-joined, de-duplicated, sorted MiCA service letters found in ``cell``.

    Fragments that cannot be mapped are reported as ``?`` so that a normalisation
    miss is visible in the output rather than silently swallowed.
    """
    letters: list[str] = []
    unknown = False
    for fragment in split_service_cell(cell):
        letter = service_letter(fragment)
        if letter is None:
            unknown = True
        elif letter not in letters:
            letters.append(letter)
    result = "|".join(sorted(letters))
    if unknown:
        result = f"{result}|?" if result else "?"
    return result


@dataclass
class MicaCsvSource(Source):
    """One CSV file of the interim MiCA register."""

    key: str = ""
    filename: str = ""
    title: str = ""
    docs_url: str = MICA_LANDING
    enum_fields: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ()
    identifier_columns: tuple[str, ...] = ("ae_lei",)
    name_columns: tuple[str, ...] = ("ae_lei_name", "ae_commercial_name")
    expected_fields: tuple[str, ...] = ("ae_lei_name", "ae_homeMemberState")
    #: ESMA states weekly. Verified: the file is overwritten in place, so
    #: Last-Modified is the only vintage there is.
    cadence_hours: float | None = 168.0
    disclaimer: str = (
        "The crypto-asset white papers listed in ESMA's register have not been reviewed "
        "or approved by any competent authority in any Member State of the European Union. "
        "The offeror and/or issuer of the crypto-asset is solely responsible for the "
        "content of each crypto-asset white paper."
    )
    block_structured: bool = False
    multi_value_fields: dict[str, str] = field(default_factory=dict)
    #: ``{new_column: (source_column, callable)}`` — additive, never destructive.
    derived: dict[str, tuple[str, Any]] = field(default_factory=dict)
    base_url: str = MICA_BASE
    _cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _headers: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.filename}"

    def reset(self) -> None:
        """Drop the downloaded file.

        These source objects are module-level singletons, so without this a
        long-lived process would keep serving whatever it downloaded first.
        :func:`eufinreg.sources.get_source` calls it on every lookup.
        """
        self._cache.clear()
        self._headers.clear()

    # -- reading ----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        if self.url in self._cache:
            return self._cache[self.url]
        text = fetcher.get_text(self.url, label=self.key)
        rows, header = parse_csv(text)
        if not query.extra.get("no_derived"):
            rows = [self._augment(row) for row in rows]
        self._cache[self.url] = rows
        self._headers[self.url] = header
        return rows

    def _augment(self, row: dict[str, Any]) -> dict[str, Any]:
        for new_column, (source_column, transform) in self.derived.items():
            if source_column in row:
                row[new_column] = transform(row.get(source_column) or "")
        return row

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        rows = self._load(fetcher, query)
        for index, row in enumerate(rows):
            if query.max_docs and index >= query.max_docs:
                return
            yield row

    def probe_columns(self, fetcher: Fetcher, query: Query) -> tuple[str, ...]:
        """The CSV header, which exists whether or not the file has any rows."""
        self._load(fetcher, query)  # cached; no second request
        return self._headers.get(self.url, ())

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        separator = self.multi_value_fields.get(field_name)
        if field_name == "ac_serviceCode":
            # Counting the raw cells is useless — every authority spells them
            # differently. Count the fragments instead.
            pairs = count_values(
                (
                    {"f": frag}
                    for row in rows
                    for frag in split_service_cell(row.get(field_name, ""))
                ),
                "f",
            )
            note = (
                "fragments split out of a free-text field; the same service appears "
                "under many spellings — see the ac_serviceCode_normalised column"
            )
            return pairs, note
        pairs = count_values(rows, field_name, split=separator)
        note = "counted from the full CSV, not a sample"
        if separator:
            note += f"; cells split on {separator!r}"
        return pairs, note


def parse_csv(text: str) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Parse a MiCA CSV into ``(rows, header)``, tolerating its real-world quirks.

    The header is returned separately because it survives an empty file: ESMA's
    asset-referenced token register is a header and nothing else, and that is a
    correct answer, not a broken read. ``doctor`` checks the shape against the
    header when there are no rows to check it against.

    * a UTF-8 BOM (handled by the caller's ``utf-8-sig`` decoding);
    * a trailing comma in the header, producing one nameless column;
    * rows with more fields than the header — those land in ``_extra_columns``
      instead of being dropped;
    * values containing embedded newlines (handled by :mod:`csv` itself).
    """
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    header: tuple[str, ...] = tuple(
        name.strip() for name in (reader.fieldnames or []) if name and name.strip()
    )
    for raw in reader:
        row: dict[str, Any] = {}
        for name, value in raw.items():
            if name is None:
                overflow = [v for v in (value or []) if v]
                if overflow:
                    row["_extra_columns"] = " | ".join(overflow)
                continue
            name = name.strip()
            if not name:
                continue  # nameless column from the trailing comma
            row[name] = (value or "").strip()
        if any(v for v in row.values()):
            rows.append(row)
    return rows, header


_COMMON_ENUMS = ("ae_competentAuthority", "ae_homeMemberState")

CASP = MicaCsvSource(
    key="mica-casp",
    filename="CASPS.csv",
    key_columns=("ae_lei", "ae_lei_name"),
    title="MiCA authorised crypto-asset service providers (Art. 109)",
    enum_fields=(
        *_COMMON_ENUMS,
        "ac_serviceCode",
        "ac_serviceCode_normalised",
        "ac_serviceCode_cou",
    ),
    multi_value_fields={
        "ac_serviceCode_cou": "|",
        "ac_serviceCode": "|",
        "ac_serviceCode_normalised": "|",
    },
    derived={"ac_serviceCode_normalised": ("ac_serviceCode", normalise_services)},
)

ART = MicaCsvSource(
    key="mica-art",
    filename="ARTZZ.csv",
    title="MiCA asset-referenced token issuers",
    enum_fields=(*_COMMON_ENUMS, "ae_credit_institution"),
    multi_value_fields={"wp_url_cou": "|", "ae_DTI": "|"},
)

EMT = MicaCsvSource(
    key="mica-emt",
    filename="EMTWP.csv",
    title="MiCA e-money token issuers",
    enum_fields=(
        *_COMMON_ENUMS,
        "ae_exemption48_4",
        "ae_exemption48_5",
        "ae_authorisation_other_emt",
    ),
    multi_value_fields={"ae_DTI": "|"},
)

OTHER = MicaCsvSource(
    key="mica-other",
    filename="OTHER.csv",
    title="MiCA white papers for crypto-assets other than ART/EMT",
    enum_fields=_COMMON_ENUMS,
    multi_value_fields={"ae_offerCode_cou": "|", "ae_DTI": "|"},
)

NCASP = MicaCsvSource(
    key="mica-ncasp",
    filename="NCASP.csv",
    title="MiCA non-compliant entities (Art. 110) — NOT a licence list",
    enum_fields=(*_COMMON_ENUMS, "ae_infrigment"),
)

MICA_SOURCES = (CASP, ART, EMT, OTHER, NCASP)
