"""EUDAMED — the EU medical device database (MDR 2017/745, IVDR 2017/746).

The same idea as the financial registers, in a different industry: placing a
medical device on the EU market requires registering as an *economic operator*,
so EUDAMED holds a list of every manufacturer, importer, authorised
representative and system/procedure-pack producer that does — with an address,
an email and a phone number. On 2026-08-16 that was **48,893 organisations**.

* Public site: https://ec.europa.eu/tools/eudamed
* Interface: the JSON API the public site itself runs on. No authentication.
* ``robots.txt``: ``ec.europa.eu/robots.txt`` has 201 ``Disallow`` rules for
  ``*`` on 2026-08-16 and **none** of them matches ``/tools/eudamed/``.

Two endpoints are wrapped here:

``api/eos``
    Economic operators — the company directory.
``api/ses/``
    Supervising entities, i.e. notified bodies. The bodies that certify the
    devices, cross-linked to their NANDO notifications.

Paging is Spring Data (``page``/``size``/``sort``) with one trap: **``size`` is
silently capped at 300.** Asking for 1000 returns 300 and reports no error, so a
client that trusts its own page size and stops when it gets "less than it asked
for" will read the first page and conclude it is done.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

BASE_URL = "https://ec.europa.eu/tools/eudamed"
PUBLIC_SITE = "https://ec.europa.eu/tools/eudamed"

#: Verified 2026-08-16: ``size=500`` and ``size=1000`` both return 300 records
#: with HTTP 200 and no warning. This is the real ceiling.
MAX_PAGE_SIZE = 300

#: ULIDs are unique and lexicographically sortable, which is what makes deep
#: paging stable. ``sort=eudamedIdentifier,ASC`` returns HTTP 500; ``name`` is
#: not unique. Verified against the live endpoint.
SORT = "ulid,ASC"

#: ``--select`` values for economic operators, and the live counts that add up
#: to the 48,893 total (2026-08-16).
ACTOR_TYPES = {
    "manufacturer": "refdata.actor-type.manufacturer",
    "importer": "refdata.actor-type.importer",
    "authorised-representative": "refdata.actor-type.authorised-representative",
    "system-procedure-pack-producer": "refdata.actor-type.system-procedure-pack-producer",
}
#: Short aliases, because nobody wants to type the long ones.
ACTOR_ALIASES = {
    "mf": "manufacturer",
    "manufacturers": "manufacturer",
    "im": "importer",
    "importers": "importer",
    "ar": "authorised-representative",
    "authorized-representative": "authorised-representative",
    "pr": "system-procedure-pack-producer",
    "sppp": "system-procedure-pack-producer",
}


def resolve_actor_types(select: str | None) -> list[str]:
    """``--select`` → the ``actorTypeCode`` values to push at the server.

    Accepts ``ALL`` (or nothing), a short alias, a bare name, or the full
    ``refdata.actor-type.*`` code; comma-separate to combine.
    """
    if select is None or not select.strip() or select.strip().upper() == "ALL":
        return []
    codes: list[str] = []
    unknown: list[str] = []
    for part in select.replace(";", ",").split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token.startswith("refdata.actor-type."):
            code = token
        else:
            name = ACTOR_ALIASES.get(token, token)
            code = ACTOR_TYPES.get(name, "")
            if not code:
                unknown.append(part.strip())
                continue
        if code not in codes:
            codes.append(code)
    if unknown:
        raise ValueError(
            f"unknown actor type(s) {', '.join(unknown)}; "
            f"try ALL or any of {', '.join(ACTOR_TYPES)}"
        )
    return codes


def _texts(value: Any) -> str:
    """Join a EUDAMED multilingual ``{"texts": [{"text": …}]}`` block."""
    if not isinstance(value, Mapping):
        return ""
    return " | ".join(
        str(entry.get("text"))
        for entry in value.get("texts") or []
        if isinstance(entry, Mapping) and entry.get("text")
    )


def _flatten_nested(name: str, value: Any) -> dict[str, Any]:
    """Turn one nested EUDAMED field into flat columns.

    The known shapes get useful columns. Anything unrecognised is preserved as
    compact JSON under its own name rather than dropped, so a field EUDAMED adds
    tomorrow shows up in ``--inspect`` instead of disappearing.
    """
    if name in ("names", "abbreviatedNames"):
        return {name: _texts(value)}
    if name == "legislationLinks" and isinstance(value, list):
        codes, links = [], []
        for entry in value:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("legislationCode"):
                codes.append(str(entry["legislationCode"]))
            if entry.get("link"):
                links.append(str(entry["link"]))
        return {"legislationCodes": " | ".join(codes), "legislationLinks": " | ".join(links)}
    if isinstance(value, Mapping):
        # The reference-data pattern: {"code": …} plus optional extras.
        if "code" in value:
            out: dict[str, Any] = {name: value.get("code")}
            for extra in ("srnCode", "category", "name"):
                if value.get(extra) is not None:
                    out[f"{name}_{extra}"] = value[extra]
            return out
        if "isoCode" in value:
            return {name: value.get("isoCode"), f"{name}_name": value.get("name")}
    return {name: json.dumps(value, ensure_ascii=False, sort_keys=True)}


def flatten_actor(record: Mapping[str, Any]) -> dict[str, Any]:
    """One EUDAMED actor record → one flat row, field names kept verbatim."""
    row: dict[str, Any] = {}
    for name, value in record.items():
        if value is None:
            row[name] = ""
        elif isinstance(value, (str, int, float, bool)):
            row[name] = value
        elif isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
            row[name] = " | ".join(str(v) for v in value)
        else:
            row.update(_flatten_nested(name, value))
    return row


@dataclass
class EudamedSource(Source):
    """One paginated EUDAMED actor endpoint."""

    key: str = ""
    path: str = ""
    title: str = ""
    docs_url: str = PUBLIC_SITE
    enum_fields: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ("eudamedIdentifier",)
    identifier_columns: tuple[str, ...] = ("eudamedIdentifier", "srn")
    name_columns: tuple[str, ...] = ("name",)
    expected_fields: tuple[str, ...] = ("name", "eudamedIdentifier")
    cadence_hours: float | None = 24.0
    personal_data: str = ""
    multi_value_fields: dict[str, str] = field(default_factory=dict)
    block_structured: bool = False
    select_help: str = ""
    #: True where ``--select`` maps onto ``actorTypeCode``.
    selectable_actor_types: bool = False
    base_url: str = BASE_URL
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.path}"

    def reset(self) -> None:
        self._rows.clear()

    # -- query construction ------------------------------------------------

    def build_params(self, query: Query) -> dict[str, Any]:
        """Base query parameters, before paging.

        ``--query`` is passed through as raw ``key=value`` pairs, which is how
        you reach a filter this package does not model (``countryIso2Code=DE``,
        for instance).
        """
        params: dict[str, Any] = {"languageIso2Code": "en"}
        if self.selectable_actor_types:
            codes = resolve_actor_types(query.select)
            if codes:
                params["actorTypeCode"] = ",".join(codes)
        elif query.select:
            raise ValueError(f"{self.key} does not support --select; use --query or --field")
        for name, value in _parse_raw_query(query.raw_query):
            params[name] = value
        return params

    # -- reading -----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._rows:
            return self._rows[cache_key]

        params = self.build_params(query)
        size = max(1, min(query.page_size, MAX_PAGE_SIZE))
        params.update({"size": size, "sort": SORT})

        rows: list[dict[str, Any]] = []
        page = 0
        expected: int | None = None
        while True:
            params["page"] = page
            payload = fetcher.get_json(self.url, params, label=f"{self.key}-p{page:04d}")
            if not isinstance(payload, Mapping):
                raise FetchError(
                    f"{self.url} returned {type(payload).__name__}, not a page object",
                    url=self.url,
                )
            if expected is None:
                expected = payload.get("totalElements")
            content = payload.get("content") or []
            for record in content:
                if isinstance(record, Mapping):
                    rows.append(flatten_actor(record))
                if query.max_docs and len(rows) >= query.max_docs:
                    self._rows[cache_key] = rows
                    return rows
            # `last` is authoritative; the empty-page check is the backstop for
            # the day it stops being sent.
            if payload.get("last") is True or not content:
                break
            page += 1
            total_pages = payload.get("totalPages")
            if isinstance(total_pages, int) and page >= total_pages:
                break

        self._rows[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        separator = self.multi_value_fields.get(field_name)
        pairs = count_values(rows, field_name, split=separator)
        note = f"counted from all {len(rows)} fetched record(s), not a sample"
        if separator:
            note += f"; cells split on {separator.strip()!r}"
        return pairs, note


def _parse_raw_query(raw: str | None) -> list[tuple[str, str]]:
    """``'countryIso2Code=DE&actorStatus=active'`` → ``[(name, value), …]``."""
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split("&"):
        if not chunk.strip():
            continue
        if "=" not in chunk:
            raise ValueError(
                f"--query for EUDAMED expects raw url parameters, e.g. "
                f"'countryIso2Code=DE'; got {chunk!r}"
            )
        name, _, value = chunk.partition("=")
        pairs.append((name.strip(), value.strip()))
    return pairs


ECONOMIC_OPERATORS = EudamedSource(
    key="eudamed-eo",
    expected_fields=("name", "eudamedIdentifier", "actorType", "countryIso2Code"),
    personal_data=(
        "Nearly every one of the 48,893 organisations carries an email address and a "
        "phone number. Some are sole traders, where the company contact is a person. "
        "Those details were published so patients and regulators can identify who is "
        "responsible for a device — not as a marketing list."
    ),
    path="api/eos",
    title="EUDAMED economic operators — EU medical device manufacturers, importers, "
    "authorised representatives and system/procedure-pack producers",
    selectable_actor_types=True,
    enum_fields=(
        "actorType",
        "actorType_srnCode",
        "actorStatus",
        "countryIso2Code",
        "countryName",
        "countryType",
        "roleName",
    ),
    select_help=(
        "actor type: ALL (default), or any of manufacturer, importer, "
        "authorised-representative, system-procedure-pack-producer "
        "(aliases mf/im/ar/sppp); comma-separate to combine. Pushed server-side."
    ),
)

NOTIFIED_BODIES = EudamedSource(
    key="eudamed-nb",
    path="api/ses/",
    title="EUDAMED notified bodies (MDR/IVDR), cross-linked to their NANDO notifications",
    enum_fields=(
        "actorType",
        "countryIso2Code",
        "countryName",
        "legislationCodes",
        "nbStatus",
    ),
    multi_value_fields={"legislationCodes": " | ", "legislationLinks": " | "},
)

EUDAMED_SOURCES = (ECONOMIC_OPERATORS, NOTIFIED_BODIES)
