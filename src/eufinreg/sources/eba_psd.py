"""EBA register of payment and electronic money institutions (PSD2).

Unlike most public registers, this one is machine-readable **because the law
says so**: Commission Implementing Regulation (EU) 2019/410 and Commission
Delegated Regulation (EU) 2019/411 require the EBA to publish the register
electronically, and the EBA does it as a nightly "golden copy" download.

* Landing page: https://euclid.eba.europa.eu/register/pir/registerDownload
* File metadata: ``GET /register/api/filemetadata`` → JSON naming the current
  ZIP, its size, its SHA-256 and the generation timestamp.
* The ZIP holds one JSON document plus a ``.sha256`` sidecar for it.

Both checksums are verified here. They are the whole reason the EBA publishes
them, and a truncated download of a 20 MB file otherwise fails as a confusing
JSON parse error a long way from the cause.

Document model, which is where the surprises are::

    {"CA_OwnerID": "IE_CBI",                     # supervising authority
     "EntityCode": "IE_CBI!C58301",
     "EntityType": "PSD_PI",
     "Properties": [{"ENT_NAM": "Fire Financial Services Limited"},
                    {"ENT_ADD": "Dogpatch Labs, Custom House Quay"},
                    {"ENT_AUT": ["2018-07-02"]}],   # str OR list, one key each
     "Services":   [{"AT": ["PS_03A", "PS_05B"]},   # keyed by ISO-2 country
                    {"IE": ["PS_03A", "PS_05B"]}],
     "__EBA_EntityVersion": "20260814223344848"}

``Properties`` is a list of single-key objects rather than one object, values
are sometimes strings and sometimes lists, and ``Services`` is the passporting
map — the set of member states the institution may serve, which is the only
place the real footprint is recorded.

Field names are the register's own codes. Nothing is renamed; the metadata
endpoint supplies the human labels as separate derived columns.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..http import Fetcher, FetchError
from ..profile import count_values
from .base import Query, Source

REGISTER_BASE = "https://euclid.eba.europa.eu/register"
FILE_METADATA_URL = f"{REGISTER_BASE}/api/filemetadata"
METADATA_URL = f"{REGISTER_BASE}/pir-api/metadata"
DOWNLOAD_PAGE = f"{REGISTER_BASE}/pir/registerDownload"

#: Entity types that are institutions in their own right. Agents (~322k of the
#: ~329k records) and branches hang off one of these, and the register's own
#: search excludes them by default; so does this source. ``--select ALL``
#: brings them back.
INSTITUTION_TYPES = (
    "PSD_PI",
    "PSD_EPI",
    "PSD_EMI",
    "PSD_EEMI",
    "PSD_AISP",
    "PSD_EXC",
    "PSD_ENL",
)
#: Types that describe something belonging to an institution rather than an
#: institution itself. Reachable, but never in the default selection.
DEPENDENT_TYPES = ("PSD_AG", "PSD_BR")

#: Short forms accepted by ``--select``, in the order they are documented.
SELECTABLE_CODES = tuple(
    code.removeprefix("PSD_") for code in (*INSTITUTION_TYPES, *DEPENDENT_TYPES)
)


def _properties(record: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the list-of-single-key-objects into one mapping.

    Verified across all 328,965 records in the 2026-08-16 file: no record
    repeats a property code, so a plain merge loses nothing. Empty objects
    appear in the search API's version of the same records and are skipped.
    """
    merged: dict[str, Any] = {}
    for entry in record.get("Properties") or []:
        if isinstance(entry, Mapping):
            merged.update(entry)
    return merged


def _services(record: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{country: [service codes]}`` from the ``Services`` block.

    Values arrive as a list for most records and as a bare string for some, so
    both are normalised. Only institutions carry this block at all.
    """
    out: dict[str, list[str]] = {}
    for entry in record.get("Services") or []:
        if not isinstance(entry, Mapping):
            continue
        for country, codes in entry.items():
            values = codes if isinstance(codes, (list, tuple)) else [codes]
            bucket = out.setdefault(str(country), [])
            for code in values:
                text = str(code).strip()
                if text and text not in bucket:
                    bucket.append(text)
    return out


def _entity_version_iso(stamp: Any) -> str:
    """``20260814223344848`` → ``2026-08-14T22:33:44Z``.

    The register stamps every record with a 17-digit local version number.
    Anything that is not that shape is passed through untouched rather than
    guessed at.
    """
    text = str(stamp or "").strip()
    if len(text) != 17 or not text.isdigit():
        return text
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}T{text[8:10]}:{text[10:12]}:{text[12:14]}Z"


def parse_selection(select: str | None) -> tuple[str, ...] | None:
    """Translate ``--select`` into a tuple of entity types, or ``None`` for all.

    Accepts ``ALL``, ``INSTITUTIONS``, a full code (``PSD_EMI``), a bare
    suffix (``EMI``), and comma-separated combinations of those.
    """
    if select is None or not select.strip():
        return INSTITUTION_TYPES
    wanted: list[str] = []
    for part in select.replace(";", ",").split(","):
        token = part.strip().upper().replace("-", "_")
        if not token:
            continue
        if token == "ALL":
            return None
        if token in ("INSTITUTION", "INSTITUTIONS"):
            wanted.extend(t for t in INSTITUTION_TYPES if t not in wanted)
            continue
        code = token if token.startswith("PSD_") else f"PSD_{token}"
        if code not in wanted:
            wanted.append(code)
    return tuple(wanted) or INSTITUTION_TYPES


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _check_digest(payload: bytes, expected: str, *, what: str, url: str) -> None:
    """Compare a payload against a published digest.

    ``expected`` may be a bare hex digest or a whole ``sha256sum`` line —
    ``<digest>  <filename>`` — because that is the conventional content of a
    ``.sha256`` sidecar and the register is free to switch between them. Taking
    the first field rather than the whole string is the difference between a
    working checksum and a "the download is truncated" error on a file that is
    perfectly intact.
    """
    expected = (expected or "").strip().split()[0].lower() if (expected or "").strip() else ""
    if not expected:
        return
    actual = _sha256(payload)
    if actual != expected:
        raise FetchError(
            f"{what} failed its SHA-256 check (register says {expected}, "
            f"got {actual}) — the download is truncated or corrupt, retry",
            url=url,
        )


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Pull the entity array out of the downloaded document.

    The file is ``[[{disclaimer}], [ …entities… ]]``. Rather than index into
    that shape, pick the longest nested list whose members look like entity
    records — so a re-ordered or re-wrapped file keeps working, and a file that
    genuinely stopped containing entities fails loudly instead of silently
    yielding nothing.
    """
    best: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            entities = [
                item
                for item in node
                if isinstance(item, Mapping) and "EntityCode" in item and "EntityType" in item
            ]
            if len(entities) > len(best):
                best = [dict(item) for item in entities]
            stack.extend(item for item in node if isinstance(item, list))
    return best


@dataclass
class EbaPsdSource(Source):
    """The EBA PSD2 register, read from its nightly golden-copy download."""

    key: str = "eba-psd"
    title: str = (
        "EBA register of payment and e-money institutions (PSD2, Art. 15) — "
        "official daily machine-readable download"
    )
    docs_url: str = DOWNLOAD_PAGE
    key_columns: tuple[str, ...] = ("EntityCode",)
    identifier_columns: tuple[str, ...] = ("EntityCode",)
    name_columns: tuple[str, ...] = ("ENT_NAM",)
    expected_fields: tuple[str, ...] = ("EntityCode", "EntityType", "CA_OwnerID", "ENT_NAM")
    #: National authorities are required to update at least daily and the
    #: golden copy is regenerated nightly.
    cadence_hours: float | None = 24.0
    disclaimer: str = (
        "Unlike national registers under PSD2, this Register has no legal significance "
        "and confers no rights in law. […] responsibility for the accuracy of that "
        "information lies with the competent authorities at national level."
    )
    personal_data: str = (
        "--select ALL returns 322,314 agent records that are largely natural persons, "
        "named in full with an address, processed under Regulation (EU) 2018/1725. "
        "The default selection excludes them."
    )
    enum_fields: tuple[str, ...] = (
        "EntityType",
        "EntityTypeLabel",
        "CA_OwnerID",
        "CA_OwnerName",
        "ENT_COU_RES",
        "ENT_SER",
        "ENT_SER_COU",
        "ENT_EXC",
        "DER_CHI_ENT_AUT",
    )
    multi_value_fields: dict[str, str] = field(
        default_factory=lambda: {
            "ENT_SER": " | ",
            "ENT_SER_COU": " | ",
            "ENT_EXC": " | ",
        }
    )
    block_structured: bool = False
    select_help: str = (
        "entity type: ALL, INSTITUTIONS (the default — excludes the ~322k agents "
        f"and branches), or any of {', '.join(SELECTABLE_CODES)}; "
        "comma-separate to combine"
    )
    file_metadata_url: str = FILE_METADATA_URL
    metadata_url: str = METADATA_URL
    _rows: dict[tuple[Any, ...], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _labels: dict[str, dict[str, str]] | None = field(default=None, repr=False)

    def personal_data_for(self, select: str | None = None) -> str:
        """Only the agent records raise the question, and only if asked for.

        The default selection is institutions, which are companies. ``--select
        ALL`` (or an explicit ``AG``) brings back 322,314 records that are
        largely named individuals with an address, and that is a different
        processing decision — so it is gated separately rather than blanketing
        the whole source.
        """
        types = parse_selection(select)
        if types is None or any(code in DEPENDENT_TYPES for code in types):
            return self.personal_data
        return ""

    def reset(self) -> None:
        """Drop everything downloaded. Sources are module-level singletons."""
        self._rows.clear()
        self._labels = None

    # -- download ---------------------------------------------------------

    def file_metadata(self, fetcher: Fetcher) -> dict[str, Any]:
        """The register's own description of the current golden copy."""
        payload = fetcher.get_json(self.file_metadata_url, label=f"{self.key}-filemetadata")
        if not isinstance(payload, Mapping):
            raise FetchError(
                f"{self.file_metadata_url} did not return an object describing the "
                f"current download (got {type(payload).__name__})",
                url=self.file_metadata_url,
            )
        return dict(payload)

    def download_url(self, meta: Mapping[str, Any]) -> str:
        base = str(meta.get("golden_copy_path_context") or "").strip()
        relative = str(meta.get("latest_version_relative_zip_path") or "").strip()
        if not relative:
            raise FetchError(
                "file metadata names no download (latest_version_relative_zip_path is missing)",
                url=self.file_metadata_url,
            )
        if not base:
            base = f"{REGISTER_BASE}/downloads/PSDMD/"
        return f"{base.rstrip('/')}/{relative.lstrip('/')}"

    def _download(self, fetcher: Fetcher) -> tuple[Any, dict[str, Any]]:
        """Fetch, verify and parse the golden copy. Returns ``(payload, meta)``."""
        meta = self.file_metadata(fetcher)
        url = self.download_url(meta)
        archive = fetcher.get(url, label=f"{self.key}-goldencopy").content
        _check_digest(archive, str(meta.get("sha256_hash") or ""), what="the ZIP", url=url)

        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = [n for n in bundle.namelist() if n.lower().endswith(".json")]
                if not names:
                    raise FetchError(
                        f"{url} contains no .json member (found: {bundle.namelist()})", url=url
                    )
                name = names[0]
                body = bundle.read(name)
                sidecar = f"{name}.sha256"
                if sidecar in bundle.namelist():
                    _check_digest(
                        body,
                        bundle.read(sidecar).decode("ascii", "replace"),
                        what=f"{name}",
                        url=url,
                    )
        except zipfile.BadZipFile as exc:
            raise FetchError(f"{url} is not a readable ZIP archive: {exc}", url=url) from exc

        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except ValueError as exc:
            raise FetchError(f"the JSON inside {url} does not parse: {exc}", url=url) from exc
        return payload, meta

    # -- labels -----------------------------------------------------------

    def labels(self, fetcher: Fetcher) -> dict[str, dict[str, str]]:
        """Code → label maps, taken from the register's own metadata document.

        Nothing here is hardcoded: the entity types, authority names, service
        codes and exclusion codes all come from the endpoint, so a new code
        appears with its label rather than as an unexplained string.
        """
        if self._labels is not None:
            return self._labels
        payload = fetcher.get_json(self.metadata_url, label=f"{self.key}-metadata")
        document = payload[0] if isinstance(payload, list) and payload else payload
        maps: dict[str, dict[str, str]] = {}
        if isinstance(document, Mapping):
            for code_list in document.get("CodeLists") or []:
                if not isinstance(code_list, Mapping):
                    continue
                values = {
                    str(v.get("CodeValue")): str(v.get("CodeValueDisplay") or "")
                    for v in code_list.get("Values") or []
                    if isinstance(v, Mapping) and v.get("CodeValue")
                }
                if values:
                    maps[str(code_list.get("CodeListCode"))] = values
            services = {
                str(v.get("CodeValue")): str(v.get("CodeValueDisplay") or "")
                for v in document.get("ServiceDefinition") or []
                if isinstance(v, Mapping) and v.get("CodeValue")
            }
            if services:
                maps["ServiceDefinition"] = services
        self._labels = maps
        return maps

    # -- flattening -------------------------------------------------------

    def flatten_record(
        self,
        record: Mapping[str, Any],
        *,
        labels: Mapping[str, Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        """One entity record → one flat row, keeping the register's own names."""
        row: dict[str, Any] = {
            "CA_OwnerID": record.get("CA_OwnerID", ""),
            "EntityCode": record.get("EntityCode", ""),
            "EntityType": record.get("EntityType", ""),
        }
        properties = _properties(record)
        for name, value in properties.items():
            row[name] = " | ".join(str(v) for v in value) if isinstance(value, list) else value

        services = _services(record)
        countries = sorted(services)
        codes: list[str] = []
        for country in countries:
            for code in services[country]:
                if code not in codes:
                    codes.append(code)
        row["ENT_SER"] = " | ".join(sorted(codes))
        row["ENT_SER_COU"] = " | ".join(countries)
        row["ENT_SER_COU_count"] = str(len(countries))
        row["ENT_SER_BY_COU"] = " | ".join(
            f"{country}={','.join(services[country])}" for country in countries
        )
        row["__EBA_EntityVersion"] = record.get("__EBA_EntityVersion", "")

        if labels is not None:
            row["__EBA_EntityVersion_iso"] = _entity_version_iso(row["__EBA_EntityVersion"])
            row["EntityTypeLabel"] = _label(labels, "ENT_TYP_PSD", row["EntityType"])
            row["CA_OwnerName"] = _label(labels, "RDL_COM_AUT_PSD", row["CA_OwnerID"])
            row["ENT_COU_RES_label"] = _label(
                labels, "RDL_COU_COD_EEA", properties.get("ENT_COU_RES")
            )
            row["ENT_SER_label"] = " | ".join(
                _label(labels, "ServiceDefinition", code) or code for code in sorted(codes)
            )
            excluded = properties.get("ENT_EXC")
            if excluded is not None:
                values = excluded if isinstance(excluded, list) else [excluded]
                row["ENT_EXC_label"] = " | ".join(
                    _label(labels, "ENUM_ENT_EXC", code) or str(code) for code in values
                )
        return row

    # -- reading ----------------------------------------------------------

    def _load(self, fetcher: Fetcher, query: Query) -> list[dict[str, Any]]:
        types = parse_selection(query.select)
        derived = not query.extra.get("no_derived")
        cache_key = (types, derived)
        if cache_key in self._rows:
            return self._rows[cache_key]

        payload, meta = self._download(fetcher)
        records = extract_records(payload)
        if not records:
            raise FetchError(
                f"the golden copy generated {meta.get('timestamp')!r} contains no "
                f"entity records — the file format has changed; rerun with --raw "
                f"and inspect the response",
                url=self.download_url(meta),
            )

        wanted = None if types is None else frozenset(types)
        # Fetched after the big download so that a metadata outage cannot cost
        # the caller a 20 MB retry.
        labels = self.labels(fetcher) if derived else None

        rows = [
            self.flatten_record(record, labels=labels)
            for record in records
            if wanted is None or record.get("EntityType") in wanted
        ]
        self._rows[cache_key] = rows
        return rows

    def iter_records(self, fetcher: Fetcher, query: Query) -> Iterator[dict[str, Any]]:
        for index, row in enumerate(self._load(fetcher, query)):
            if query.max_docs and index >= query.max_docs:
                return
            yield row

    def count_values(
        self, fetcher: Fetcher, query: Query, field_name: str
    ) -> tuple[list[tuple[str, int]], str | None]:
        rows = self._load(fetcher, query)
        separator = self.multi_value_fields.get(field_name)
        pairs = count_values(rows, field_name, split=separator)
        selected = parse_selection(query.select)
        scope = "the whole register" if selected is None else f"entity types {', '.join(selected)}"
        note = f"counted from every downloaded record ({scope}), not a sample"
        if separator:
            note += f"; cells split on {separator.strip()!r}"
        return pairs, note


def _label(labels: Mapping[str, Mapping[str, str]], code_list: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return (labels.get(code_list) or {}).get(str(value), "")


PSD = EbaPsdSource()

EBA_SOURCES = (PSD,)
