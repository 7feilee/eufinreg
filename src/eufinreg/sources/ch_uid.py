"""The Swiss UID register (Switzerland) — the federal business identification register.

Every Swiss enterprise, association, foundation, authority and sole trader has a
UID (``CHE-101.329.561``), assigned by the Federal Statistical Office. The
register behind it records the legal name, legal form, seat, commercial-register
status, VAT status and VAT-group membership — and the FSO exposes it as a
**public SOAP web service with no authentication at all**.

* Service: ``https://www.uid-wse.admin.ch/V5.0/PublicServices.svc``
* WSDL: same URL with ``?wsdl`` (or ``?singleWsdl`` for the schema inline).
  ``BasicHttpBinding``, i.e. SOAP 1.1: ``text/xml`` plus a quoted ``SOAPAction``.
* Operations: ``Search``, ``GetByUID``, ``ValidateUID``, ``ValidateVatNumber``,
  ``GetOrganisationSample``. This source wraps the first two.
* ``robots.txt``: absent (HTTP 404) on both ``uid.admin.ch`` and
  ``uid-wse.admin.ch``, verified 2026-08-16.

**This is a lookup interface, not a directory.** Two limits decide how it can be
used, and neither of them reports itself:

* ``Search`` returns **at most 30 records**, whatever ``maxNumberOfRecords``
  says. Verified 2026-08-16: a canton-wide search asking for 1,000 returned 30;
  a postcode search asking for 2,000 returned 30. There is no total count, no
  cursor, no offset and no error — so "30 results" and "exactly 30 matches" are
  indistinguishable unless you are told. This source raises a warning whenever it
  hits the cap.
* ``GetByUID`` returns an **array**, not a record. ``CHE-101.329.561`` (UBS AG)
  comes back as two organisations — the Zürich and Basel seats, same UID,
  different commercial-register ID and address. A client that reads element
  ``[0]`` silently picks one of them.

Which makes it the natural companion to ``--source finma``: FINMA publishes the
Swiss authorisation holders with their UID, and this service turns a UID into
legal status. The ``uid`` column here is formatted exactly like FINMA's, so the
two join directly.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

SERVICE_URL = "https://www.uid-wse.admin.ch/V5.0/PublicServices.svc"
WSDL_URL = f"{SERVICE_URL}?wsdl"
ACTION_BASE = "http://www.uid.admin.ch/xmlns/uid-wse/IPublicServices"

NS_WSE = "http://www.uid.admin.ch/xmlns/uid-wse"
NS_WSE5 = "http://www.uid.admin.ch/xmlns/uid-wse/5"
NS_SHARED = "http://www.uid.admin.ch/xmlns/uid-wse-shared/2"
NS_ECH97 = "http://www.ech.ch/xmlns/eCH-0097/5"

#: The undocumented ceiling on ``Search``. Nothing in the response says so.
RESULT_CAP = 30

#: ``--query key=value`` names → where they go in the search request. ``uid``
#: is special: it switches the whole call to ``GetByUID``.
CRITERIA_FIELDS = {
    "name": "organisationName",
    "organisationname": "organisationName",
    "canton": "cantonAbbreviation",
    "zip": "swissZipCode",
    "plz": "swissZipCode",
    "town": "town",
    "ort": "town",
    "street": "street",
    "legalform": "legalForm",
    "country": "countryIdISO2",
}
#: Criteria that belong inside the nested ``<address>`` element.
ADDRESS_FIELDS = {"cantonAbbreviation", "swissZipCode", "town", "street", "countryIdISO2"}

_DIGITS = re.compile(r"\d+")


def format_uid(category: str, digits: str) -> str:
    """``('CHE', '101329561')`` → ``'CHE-101.329.561'``.

    The wire format splits the UID into a category and nine bare digits; every
    human-facing system, FINMA's CSV included, prints the punctuated form. This
    source emits the punctuated form so the two join without a transform.
    """
    number = "".join(_DIGITS.findall(digits or ""))
    if len(number) != 9:
        return f"{category}-{number}" if number else ""
    return f"{category}-{number[0:3]}.{number[3:6]}.{number[6:9]}"


def uid_digits(text: str) -> str:
    """Any UID spelling → the nine digits the service expects."""
    return "".join(_DIGITS.findall(text or ""))


def parse_criteria(raw_query: str | None, select: str | None) -> dict[str, str]:
    """``--query`` / ``--select`` → the search criteria this source understands.

    ``--query`` takes ``key=value&key=value``; a value with no ``=`` is treated
    as an organisation name, which is what people mean when they type one.
    ``--select`` is the canton abbreviation, because canton is the one criterion
    that partitions the register usefully.
    """
    criteria: dict[str, str] = {}
    if select and select.strip():
        criteria["cantonAbbreviation"] = select.strip().upper()

    text = (raw_query or "").strip()
    if not text:
        return criteria
    if "=" not in text:
        criteria["organisationName"] = text
        return criteria
    for chunk in text.split("&"):
        if not chunk.strip():
            continue
        name, _, value = chunk.partition("=")
        key = name.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "uid":
            criteria["uid"] = value
            continue
        mapped = CRITERIA_FIELDS.get(key)
        if mapped is None:
            raise ValueError(
                f"unknown ch-uid criterion {name.strip()!r}; try one of: "
                f"uid, {', '.join(sorted(set(CRITERIA_FIELDS)))}"
            )
        criteria[mapped] = value
    return criteria


def build_search_envelope(criteria: Mapping[str, str], *, max_records: int) -> str:
    """The ``Search`` SOAP 1.1 envelope."""
    parts: list[str] = []
    address = [
        f"<a:{name}>{_escape(value)}</a:{name}>"
        for name, value in criteria.items()
        if name in ADDRESS_FIELDS
    ]
    if "organisationName" in criteria:
        parts.append(
            f"<a:organisationName>{_escape(criteria['organisationName'])}</a:organisationName>"
        )
    if address:
        parts.append("<a:address>" + "".join(address) + "</a:address>")
    if "legalForm" in criteria:
        parts.append(f"<a:legalForm>{_escape(criteria['legalForm'])}</a:legalForm>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<Search xmlns="{NS_WSE}" xmlns:a="{NS_WSE5}" xmlns:b="{NS_SHARED}">'
        f"<searchParameters><a:uidEntitySearchParameters>{''.join(parts)}"
        f"</a:uidEntitySearchParameters></searchParameters>"
        f"<config><b:searchMode>Normal</b:searchMode>"
        f"<b:maxNumberOfRecords>{int(max_records)}</b:maxNumberOfRecords>"
        f"<b:searchNameAndAddressHistory>false</b:searchNameAndAddressHistory></config>"
        f"</Search></s:Body></s:Envelope>"
    )


def build_getbyuid_envelope(uid: str) -> str:
    """The ``GetByUID`` SOAP 1.1 envelope."""
    digits = uid_digits(uid)
    if len(digits) != 9:
        raise ValueError(f"a Swiss UID has nine digits; got {uid!r}")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<GetByUID xmlns="{NS_WSE}" xmlns:c="{NS_ECH97}"><uid>'
        f"<c:uidOrganisationIdCategorie>CHE</c:uidOrganisationIdCategorie>"
        f"<c:uidOrganisationId>{digits}</c:uidOrganisationId>"
        f"</uid></GetByUID></s:Body></s:Envelope>"
    )


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _local(tag: str) -> str:
    return tag.split("}")[-1]


#: Wrapper elements that carry no information — ``Search`` returns
#: ``organisation/organisation/…`` and ``GetByUID`` returns
#: ``organisationType/organisation/…`` for the identical payload. Dropping them
#: from the path is what makes one flattener work for both replies.
PATH_WRAPPERS = frozenset({"organisationType", "organisation"})


def flatten_organisation(element: ET.Element) -> dict[str, Any]:
    """One organisation element → one flat row.

    Keys are the dotted element path, so nothing is renamed and nothing collides:
    ``address.town``, ``uidregInformation.uidregStatusEnterpriseDetail``,
    ``commercialRegisterInformation.commercialRegisterStatus``. Repeated paths —
    a company can carry several ``OtherOrganisationId`` blocks, several
    ``groupRelationship`` blocks — are joined with ``' | '`` rather than
    overwriting each other.

    Nothing here hardcodes a field list, so a block the FSO adds tomorrow turns
    up as a new column in ``--inspect`` instead of disappearing.
    """
    row: dict[str, Any] = {}

    def walk(node: ET.Element, path: str) -> None:
        name = _local(node.tag)
        step = "" if name in PATH_WRAPPERS else name
        current = ".".join(part for part in (path, step) if part)
        children = list(node)
        if children:
            for child in children:
                walk(child, current)
            return
        text = (node.text or "").strip()
        if not text:
            return
        key = current or name
        row[key] = f"{row[key]} | {text}" if row.get(key) else text

    walk(element, "")

    category = row.pop("organisationIdentification.uid.uidOrganisationIdCategorie", "")
    number = row.pop("organisationIdentification.uid.uidOrganisationId", "")
    # The UID is the one thing worth deriving: the wire splits it in two, and
    # every other register (FINMA's CSV included) prints it punctuated.
    return {"uid": format_uid(str(category), str(number)), **row}


def parse_response(xml_text: str) -> list[dict[str, Any]]:
    """Pull every organisation out of a ``Search`` or ``GetByUID`` reply."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(
            f"UID register returned XML this client cannot parse: {exc}", url=SERVICE_URL
        ) from exc

    fault = next((e for e in root.iter() if _local(e.tag) == "Fault"), None)
    if fault is not None:
        detail = " ".join((e.text or "").strip() for e in fault.iter() if (e.text or "").strip())
        raise FetchError(f"UID register returned a SOAP fault: {detail}", url=SERVICE_URL)

    rows: list[dict[str, Any]] = []
    for item in root.iter():
        name = _local(item.tag)
        if name == "uidEntitySearchResultItem":
            organisation = next((c for c in item if _local(c.tag) == "organisation"), None)
            row = flatten_organisation(organisation) if organisation is not None else {}
            for extra in ("rating", "isHistoryMatch"):
                node = next((c for c in item if _local(c.tag) == extra), None)
                if node is not None and (node.text or "").strip():
                    row[extra] = node.text.strip()
            rows.append(row)
        elif name == "organisationType":
            rows.append(flatten_organisation(item))
    return rows


@dataclass
class ChUidSource(Source):
    """The UID register's public web service."""

    key: str = "ch-uid"
    title: str = (
        "Swiss UID register (BFS) — legal name, legal form, seat, commercial-register "
        "and VAT status for any Swiss entity; lookup only, 30 results per search"
    )
    docs_url: str = "https://www.uid.admin.ch/"
    jurisdiction: str = "CH"
    identifier_columns: tuple[str, ...] = ("uid",)
    name_columns: tuple[str, ...] = (
        "organisationIdentification.organisationName",
        "organisationIdentification.organisationLegalName",
    )
    expected_fields: tuple[str, ...] = (
        "uid",
        "organisationIdentification.organisationName",
        "address.cantonAbbreviation",
    )
    #: A live database. It is a lookup service, so "cadence" means how long
    #: an answer stays worth reusing, not how often a file is regenerated.
    cadence_hours: float | None = 24.0
    #: There is no population to snapshot here — the service answers
    #: searches and refuses to list. Scheduling it would archive whatever
    #: 30 rows one query happened to return and call it a register.
    bulk_readable: bool = False
    #: A public company that has existed for a very long time, used only
    #: to check the service is up and still the shape this client expects.
    probe_query: str = "uid=CHE-101.329.561"
    personal_data: str = (
        "The register covers sole traders alongside companies, so a result row can be "
        "a named individual at a home address. Lookups are targeted by construction; "
        "bulk collection is not what this interface is for."
    )
    enum_fields: tuple[str, ...] = (
        "organisationIdentification.legalForm",
        "address.cantonAbbreviation",
        "uidregInformation.uidregStatusEnterpriseDetail",
        "commercialRegisterInformation.commercialRegisterStatus",
        "vatRegisterInformation.vatStatus",
    )
    block_structured: bool = False
    select_help: str = (
        "canton abbreviation (ZH, BE, VD …), pushed into the search request. "
        "Combine with --query 'name=…' / 'zip=8001' / 'town=…'; --query 'uid=CHE-…' "
        "switches to GetByUID, which has no result cap."
    )
    service_url: str = SERVICE_URL
    _cache: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    #: Set when a search came back at the cap, i.e. probably truncated.
    capped: int | None = None

    def reset(self) -> None:
        self._cache.clear()
        self.capped = None

    # -- reading -----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        cache_key = (query.select, query.raw_query, query.max_docs)
        if cache_key in self._cache:
            return self._cache[cache_key]

        criteria = parse_criteria(query.raw_query, query.select)
        if not criteria:
            raise ValueError(
                "ch-uid has no 'list everything' mode — the service only answers searches. "
                "Give it something: --query 'uid=CHE-101.329.561', --query 'name=UBS', "
                "--select ZH, or --query 'zip=8001'."
            )

        if "uid" in criteria:
            envelope = build_getbyuid_envelope(criteria["uid"])
            action = f"{ACTION_BASE}/GetByUID"
            label = f"{self.key}-getbyuid"
            capped_at = None
        else:
            requested = min(max(1, query.page_size), RESULT_CAP)
            envelope = build_search_envelope(criteria, max_records=requested)
            action = f"{ACTION_BASE}/Search"
            label = f"{self.key}-search"
            capped_at = requested

        xml_text = fetcher.post_soap(self.service_url, envelope, action=action, label=label)
        rows = parse_response(xml_text)
        if capped_at is not None and len(rows) >= capped_at:
            self.capped = len(rows)
        if query.max_docs:
            rows = rows[: query.max_docs]
        self._cache[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        yield from self._load(fetcher, query)

    def warnings(self) -> list[str]:
        if self.capped is None:
            return []
        return [
            f"the UID register returned {self.capped} record(s), which is its undocumented "
            f"ceiling — it reports no total and no cursor, so there may be more. Narrow the "
            f"search (add --query 'zip=…' or 'town=…'), or look entities up individually with "
            f"--query 'uid=CHE-…'."
        ]

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        pairs = count_values(rows, field_name)
        note = f"counted from the {len(rows)} record(s) this search returned"
        if self.capped:
            note += f"; the search hit the {RESULT_CAP}-record ceiling, so this is not the register"
        return pairs, note


CH_UID = ChUidSource()

CH_UID_SOURCES = (CH_UID,)
