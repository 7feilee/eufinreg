"""GISA — every active trade licence in Austria (Gewerbeinformationssystem Austria).

Austria requires a *Gewerbeberechtigung* to trade, and GISA is the federal
register of them. The search front end at ``gisa.gv.at/search`` is an ASP.NET
WebForms application bound to ``__VIEWSTATE`` and ``__EVENTVALIDATION`` — the
same shape as BaFin's portal, and just as unautomatable. **But the ministry also
publishes the register as open data**, through a small REST service on the same
host, under CC BY 4.0:

* Catalogue entry: https://www.data.gv.at/katalog/dataset/e49a1510-9d93-4277-8467-48a1efc9f046
* Endpoint: ``GET /gisa-svc-public/GisaPublicV2.svc/ogd/<dataset>/<format>``
* No authentication, no key. ``gisa.gv.at`` serves no ``robots.txt`` (HTTP 404).

Verified 2026-08-16: **1,030,111 active trade licences**, monthly vintage.

Four traps, all silent:

* **The ``/csv`` path does not return CSV.** ``stat03`` comes back as a 7-Zip
  archive: ``Content-Type: application/application/x-7z-compressed`` (yes, with
  the prefix doubled) and ``Content-Disposition: attachment;
  filename=OgdAufrechteGewerbeberechtigung_2026.08.csv.7z``. The smaller
  ``stat02`` code list at the same kind of URL *is* plain ``text/csv``. This
  source decides by the archive's magic bytes rather than by the path or the
  declared type, so either behaviour works.
* **The published column names are lower case** (``nuts1``, ``gewerbeart``,
  ``rechtswirksam``) while the field documentation on data.gv.at spells them in
  upper case. A case-sensitive client filtering on the documented spelling gets
  zero rows and no error; ``--query`` here accepts either spelling, and names a
  column that exists in neither rather than returning nothing.
* **``gewerbeart`` means two different things in the two datasets.** In the
  licence file (``stat03``) it is a numeric code; in the code list (``stat02``)
  it is the German label. Joining the two on the column name gives an empty join.
* **The catalogue's ``byte_size`` is wrong by an order of magnitude.** It claims
  22.7 MB; the archive is 9.2 MB and expands to **211 MB / 1.03 million rows**.

Because there is no server-side filter, ``--select`` and ``--query`` here are
applied **while the CSV is being parsed**, before rows are materialised. That is
not politeness — the download is the same either way — it is the difference
between a few thousand rows in memory and a million.

**No names.** The publisher strips them: *"Es werden die aufrechten
Gewerbeberechtigungen ohne personenbezogene Daten zur Verfügung gestellt."* You
get what the licence is, where it is and when it took effect — not who holds it.
680,000 of the licence holders are natural persons, which is presumably why.
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError, strip_nul
from ..profile import count_values
from .base import Query, Source

SERVICE_BASE = "https://www.gisa.gv.at/gisa-svc-public/GisaPublicV2.svc/ogd"
CATALOGUE = "https://www.data.gv.at/katalog/dataset/e49a1510-9d93-4277-8467-48a1efc9f046"

#: 7-Zip's file magic. The declared Content-Type is unreliable, the path even
#: more so, so this is what decides whether to decompress.
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"

#: § 5 GewO licence categories, as documented by the publisher on data.gv.at.
GEWERBEART_LABELS = {
    "1": "reglementiertes Gewerbe",
    "2": "freies Gewerbe",
    "3": "nicht bewilligungspflichtiges gebundenes Gewerbe",
    "4": "Handwerk",
    "5": "konzessioniertes Gewerbe",
    "6": "bewilligungspflichtiges gebundenes Gewerbe",
    "7": "Teilgewerbe",
    "8": "Nebengewerbe",
    "9": "gewerbliche Ausübung eines Patentes",
}

#: ``--select`` shorthands for the categories that anyone actually asks for.
SELECT_ALIASES = {
    "reglementiert": "1",
    "regulated": "1",
    "frei": "2",
    "free": "2",
    "gebunden": "3",
    "handwerk": "4",
    "craft": "4",
    "konzessioniert": "5",
    "licensed": "5",
    "concession": "5",
    "bewilligungspflichtig": "6",
    "teilgewerbe": "7",
    "nebengewerbe": "8",
    "patent": "9",
}

ADRESS_ART_LABELS = {"1": "Standort", "2": "Weitere Betriebsstätte", "3": "Integrierter Betrieb"}
INHABER_LABELS = {"1": "natürliche Person", "2": "juristische Person"}

#: NUTS-2 is the Bundesland. Stable, official, and the one thing every Austrian
#: reader wants to filter on.
NUTS2_LABELS = {
    "AT11": "Burgenland",
    "AT12": "Niederösterreich",
    "AT13": "Wien",
    "AT21": "Kärnten",
    "AT22": "Steiermark",
    "AT31": "Oberösterreich",
    "AT32": "Salzburg",
    "AT33": "Tirol",
    "AT34": "Vorarlberg",
}

_VINTAGE = re.compile(r"_(\d{4}\.\d{2})\.")


def vintage_from_disposition(disposition: str | None) -> str:
    """``'attachment; filename=Ogd…_2026.08.csv.7z'`` → ``'2026.08'``.

    The endpoint carries no version parameter and the body has no header row for
    it: the data vintage is only ever stated in ``Content-Disposition``. Losing
    it means you cannot tell two downloads apart, which matters the moment you
    start diffing snapshots.
    """
    if not disposition:
        return ""
    match = _VINTAGE.search(disposition)
    return match.group(1) if match else ""


def decompress_7z(payload: bytes) -> tuple[str, bytes]:
    """Return ``(member_name, member_bytes)`` from a single-member 7z archive.

    7-Zip is not in the standard library and this is the only source that needs
    it, so it is an optional dependency rather than a cost everybody pays.
    """
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise FetchError(
            "GISA serves this dataset as a 7-Zip archive and Python has no 7z support "
            "in the standard library. Install the extra: `uv sync --extra at` "
            "(or `pip install py7zr`).",
            url=SERVICE_BASE,
        ) from exc

    with tempfile.TemporaryDirectory(prefix="eufinreg-gisa-") as workdir:
        archive_path = os.path.join(workdir, "download.7z")
        with open(archive_path, "wb") as handle:
            handle.write(payload)
        with py7zr.SevenZipFile(archive_path) as archive:
            names = [n for n in archive.getnames() if not n.endswith("/")]
            if not names:
                raise FetchError("GISA returned an empty 7z archive", url=SERVICE_BASE)
            archive.extract(path=workdir, targets=names[:1])
        member_path = os.path.join(workdir, names[0])
        with open(member_path, "rb") as handle:
            return names[0], handle.read()


@dataclass
class GisaOgdSource(Source):
    """One GISA open-data dataset."""

    key: str = ""
    dataset: str = ""
    key_columns: tuple[str, ...] = ()
    identifier_columns: tuple[str, ...] = ()
    name_columns: tuple[str, ...] = ()
    expected_fields: tuple[str, ...] = ("gewerbeschluessel", "gewerbewortlaut", "gewerbeart")
    jurisdiction: str = "AT"
    #: A monthly vintage, stated only in the Content-Disposition filename.
    cadence_hours: float | None = 720.0
    data_licence: str = "CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/"
    title: str = ""
    docs_url: str = CATALOGUE
    enum_fields: tuple[str, ...] = ()
    block_structured: bool = False
    select_help: str = ""
    #: True where ``--select`` maps onto the ``gewerbeart`` code.
    selectable_gewerbeart: bool = False
    base_url: str = SERVICE_BASE
    _cache: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: Vintage of the last download, read out of ``Content-Disposition``.
    vintage: str = ""
    #: ``(kept, scanned)`` for the last parse, so the caller can see the filter working.
    scanned: tuple[int, int] | None = None

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.dataset}/csv"

    def reset(self) -> None:
        self._cache.clear()
        self.vintage = ""
        self.scanned = None

    # -- query construction -------------------------------------------------

    def build_filters(self, query: Query) -> dict[str, str]:
        """``--select`` / ``--query`` → lower-cased ``column: value`` equality tests."""
        filters: dict[str, str] = {}
        if query.select and query.select.strip().upper() != "ALL":
            if not self.selectable_gewerbeart:
                raise ValueError(f"{self.key} does not support --select; use --query column=value")
            token = query.select.strip().lower()
            code = SELECT_ALIASES.get(token, token)
            if code not in GEWERBEART_LABELS:
                raise ValueError(
                    f"unknown gewerbeart {query.select!r}; use a code 1-9 or one of: "
                    f"{', '.join(sorted(SELECT_ALIASES))}"
                )
            filters["gewerbeart"] = code
        for chunk in (query.raw_query or "").split("&"):
            if not chunk.strip():
                continue
            if "=" not in chunk:
                raise ValueError(
                    f"--query for {self.key} expects column=value pairs, e.g. "
                    f"'nuts2=AT13&gewerbeart=5'; got {chunk!r}"
                )
            name, _, value = chunk.partition("=")
            filters[name.strip().lower()] = value.strip()
        return filters

    # -- reading ------------------------------------------------------------

    def _download(self, fetcher: Fetcher) -> str:
        response = fetcher.get(self.url, label=self.key)
        payload = response.content
        self.vintage = vintage_from_disposition(response.headers.get("Content-Disposition"))
        if payload.startswith(SEVENZIP_MAGIC):
            _, payload = decompress_7z(payload)
        return strip_nul(payload.decode("utf-8-sig", errors="replace"))

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        filters = self.build_filters(query)
        cache_key = (
            tuple(sorted(filters.items())),
            query.max_docs,
            bool(query.extra.get("no_derived")),
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        text = self._download(fetcher)
        rows = self.parse(
            text,
            filters=filters,
            max_rows=query.max_docs,
            derive=not query.extra.get("no_derived"),
        )
        self._cache[cache_key] = rows
        return rows

    def parse(
        self,
        text: str,
        *,
        filters: Mapping[str, str] | None = None,
        max_rows: int | None = None,
        derive: bool = True,
    ) -> list[dict[str, Any]]:
        """Parse the CSV, dropping non-matching rows **as they are read**.

        Materialising 1.03 million dicts and filtering afterwards costs a couple
        of gigabytes for an answer that is usually a few thousand rows.
        """
        tests = {name.lower(): value.lower() for name, value in (filters or {}).items()}
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, Any]] = []
        scanned = 0
        unknown_columns = [
            name
            for name in tests
            if reader.fieldnames and name not in {f.lower() for f in reader.fieldnames if f}
        ]
        if unknown_columns:
            raise ValueError(
                f"{self.key}: no such column(s) {', '.join(sorted(unknown_columns))}; "
                f"this dataset has: {', '.join(f for f in (reader.fieldnames or []) if f)}"
            )

        for raw in reader:
            scanned += 1
            row = {
                (name or "").strip(): (value or "").strip()
                for name, value in raw.items()
                if name and isinstance(value, str)
            }
            if not any(row.values()):
                continue
            lowered = {name.lower(): value.lower() for name, value in row.items()}
            if any(lowered.get(name, "") != value for name, value in tests.items()):
                continue
            if derive:
                row.update(self._derive(row))
            rows.append(row)
            if max_rows and len(rows) >= max_rows:
                break
        self.scanned = (len(rows), scanned)
        return rows

    def _derive(self, row: Mapping[str, str]) -> dict[str, str]:
        """Label the numeric codes, and stamp the vintage onto every row.

        The codes are documented by the publisher; nothing here is invented. The
        vintage is on the row rather than in a log line because a snapshot that
        cannot say which month it is from is not a snapshot.
        """
        extra: dict[str, str] = {}
        if self.vintage:
            extra["_vintage"] = self.vintage
        pairs = (
            ("gewerbeart", "gewerbeart_label", GEWERBEART_LABELS),
            ("adress_art", "adress_art_label", ADRESS_ART_LABELS),
            ("inhaber_pers_art", "inhaber_pers_art_label", INHABER_LABELS),
            ("nuts2", "nuts2_label", NUTS2_LABELS),
        )
        for column, target, labels in pairs:
            value = (row.get(column) or "").strip()
            # Only label what is actually a code. `gewerbeart` holds codes in the
            # licence file and German labels in the code list, so a blind lookup
            # would add an always-empty column to one of the two datasets.
            label = labels.get(value.upper()) or labels.get(value)
            if label:
                extra[target] = label
        return extra

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        if not self.scanned:
            return []
        kept, scanned = self.scanned
        if kept == scanned:
            return []
        return [
            f"kept {kept:,} of {scanned:,} rows; the filter was applied while parsing, "
            f"so the rows you did not ask for were never materialised"
        ]

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        pairs = count_values(rows, field_name)
        note = f"counted from all {len(rows):,} row(s) that passed the filter, not a sample"
        if self.vintage:
            note += f"; data vintage {self.vintage}"
        return pairs, note


GEWERBE = GisaOgdSource(
    key="gisa",
    dataset="stat03",
    title=(
        "GISA — every active Austrian trade licence (Gewerbeberechtigung), by municipality, "
        "trade code and category; no holder names"
    ),
    selectable_gewerbeart=True,
    expected_fields=("nuts2", "gewerbeart", "gewerbewortlaut", "rechtswirksam"),
    enum_fields=(
        "gewerbeart",
        "gewerbeart_label",
        "nuts2",
        "nuts2_label",
        "adress_art",
        "inhaber_pers_art",
    ),
    select_help=(
        "licence category: ALL (default), a gewerbeart code 1-9, or an alias "
        "(reglementiert, frei, konzessioniert, handwerk, teilgewerbe, nebengewerbe, patent). "
        "Applied while parsing — there is no server-side filter."
    ),
)

CODES = GisaOgdSource(
    key="gisa-codes",
    dataset="stat02",
    key_columns=("gewerbeschluessel",),
    title=(
        "GISA trade-code list (Gewerbeschlüssel + standardised wording), the labels for "
        "the codes in --source gisa"
    ),
    enum_fields=("gewerbeart", "ist_historisch"),
    select_help="not supported — use --query gewerbeart='freies Gewerbe' (labels, not codes, here)",
)

GISA_SOURCES = (GEWERBE, CODES)
