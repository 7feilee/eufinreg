#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""eufinreg-solo — one-file version of eufinreg, no clone and no venv required.

    uv run https://raw.githubusercontent.com/CHANGE-ME/eufinreg/main/scripts/eufinreg_solo.py --help

Pulls structured lists of licensed and registered companies from the EU public
registers. Any industry that needs a licence has a regulator holding a company
list more complete than any commercial database, because being on it is a
condition of trading:

  * ESMA Registers A2A (read-only Apache Solr, no authentication)
      https://registers.esma.europa.eu/publication/helpApp
  * ESMA interim MiCA register (weekly CSV files, MiCA Art. 109/110)
      https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/markets-crypto-assets-regulation-mica
  * EBA PSD2 register (nightly JSON golden copy with a SHA-256, machine-readable
      by law under Commission Implementing Regulation (EU) 2019/410)
      https://euclid.eba.europa.eu/register/pir/registerDownload
  * EUDAMED (medical devices, MDR/IVDR) — 48,893 manufacturers, importers and
      authorised representatives, plus the 70 notified bodies
      https://ec.europa.eu/tools/eudamed
  * CTIS (clinical trials, Reg. 536/2014) — who sponsors trials in the EU
      https://euclinicaltrials.eu/ctis-public/search

BaFin's and FMA's own company databases sit behind a portal whose robots.txt is
``Disallow: /``, so this script does not touch it — even though the portal does
offer per-search CSV/XML/Excel export links for a human to click. See the
project README for the evidence and for what the EU-level route covers instead.

Licensed != hiring, licensed != operating, registered address != office.
Read the README's limitations section before using the output for anything.

This file is a self-contained subset of the full package. It intentionally
duplicates logic rather than importing it, so that the single URL above is all
anyone needs.
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import io
import json
import re
import socket
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import requests

VERSION = "0.4.0"
DEFAULT_USER_AGENT = (
    f"eufinreg-solo/{VERSION} (+https://github.com/CHANGE-ME/eufinreg; public-register client)"
)
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
SOLR_BASE = "https://registers.esma.europa.eu/solr"
MICA_BASE = "https://www.esma.europa.eu/sites/default/files/2024-12"
SEP = " | "

# name -> (kind, target, notes). Verified live; see the README for counts.
SOURCES: dict[str, tuple[str, str, str]] = {
    "upreg": (
        "solr",
        "esma_registers_upreg",
        "MiFID firms, AIFMs, UCITS mancos, crowdfunding providers, trading venues",
    ),
    "bench-entities": ("solr", "esma_registers_bench_entities", "Benchmark administrators"),
    "funds": ("solr", "esma_registers_funds", "AIF / EuSEF / EuVECA funds"),
    "mmf": ("solr", "esma_registers_mmf04", "Money market funds"),
    "saris": ("solr", "esma_registers_saris_new", "Suspensions and removals"),
    "mica-casp": ("csv", "CASPS.csv", "MiCA crypto-asset service providers (Art. 109)"),
    "mica-art": ("csv", "ARTZZ.csv", "MiCA asset-referenced token issuers"),
    "mica-emt": ("csv", "EMTWP.csv", "MiCA e-money token issuers"),
    "mica-other": ("csv", "OTHER.csv", "MiCA white papers, other crypto-assets"),
    "mica-ncasp": ("csv", "NCASP.csv", "MiCA NON-COMPLIANT entities — not a licence list"),
    "eba-psd": (
        "eba",
        "PSDMD",
        "EBA PSD2 register: payment + e-money institutions, agents, branches",
    ),
    "eudamed-eo": (
        "eudamed",
        "api/eos",
        "EUDAMED economic operators: EU medical device makers, importers, reps",
    ),
    "eudamed-nb": ("eudamed", "api/ses/", "EUDAMED notified bodies (MDR/IVDR)"),
    "ctis": ("ctis", "search", "EU clinical trials and their sponsors (Reg. 536/2014)"),
}

# Block-structured Solr cores: parents and children share one flat docs array.
UPREG_DOC_TYPES = ("ae", "aeActivity", "aeNotHostMmbSt")
UPREG_HISTORY_TYPES = ("aeActivityHistory",)

ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "upreg": (
        "ae_entityTypeCode",
        "ae_status",
        "ae_officeType",
        "ae_competentAuthority",
        "ae_homeMemberState",
        "ac_serviceName",
    ),
    "mica-casp": ("ae_competentAuthority", "ae_homeMemberState", "ac_serviceCode_cou"),
    "mica-art": ("ae_competentAuthority", "ae_homeMemberState"),
    "mica-emt": ("ae_competentAuthority", "ae_homeMemberState"),
    "mica-other": ("ae_competentAuthority", "ae_homeMemberState"),
    "mica-ncasp": ("ae_competentAuthority", "ae_homeMemberState"),
    "eba-psd": ("EntityType", "CA_OwnerID", "ENT_COU_RES", "ENT_SER", "ENT_SER_COU"),
    "eudamed-eo": ("actorType", "actorStatus", "countryIso2Code", "countryName"),
    "eudamed-nb": ("actorType", "countryIso2Code", "legislationCodes"),
    "ctis": ("sponsorType", "trialPhase", "trialCountries", "therapeuticAreas"),
}
MULTI_VALUE = {
    "ac_serviceCode_cou": "|",
    "ae_offerCode_cou": "|",
    "ae_DTI": "|",
    "ENT_SER": "|",
    "ENT_SER_COU": "|",
    "trialCountries": "|",
    "therapeuticAreas": "|",
}

# CTIS (clinical trials). searchCriteria must be present even when empty, or the
# API answers 200 with totalRecords 0. Paging also stops dead at 10,000 records
# while totalRecords keeps reporting the true total — hence the warning below.
CTIS_SEARCH = "https://euclinicaltrials.eu/ctis-public-api/search"
CTIS_MAX_PAGE = 500
CTIS_WINDOW = 10_000

# EBA PSD2 register. The download is mandated machine-readable by Commission
# Implementing Regulation (EU) 2019/410; the file metadata endpoint names the
# current nightly ZIP and its SHA-256.
EBA_REGISTER = "https://euclid.eba.europa.eu/register"
EBA_FILE_METADATA = f"{EBA_REGISTER}/api/filemetadata"
# Agents alone are ~322k of the ~329k records, so they are out unless asked for.
EBA_INSTITUTIONS = ("PSD_PI", "PSD_EPI", "PSD_EMI", "PSD_EEMI", "PSD_AISP", "PSD_EXC", "PSD_ENL")

# EUDAMED (medical devices). Two traps, neither of which reports an error:
# `size` is silently capped at 300, and `languageIso2Code` is a filter, not a
# display setting — omit it and api/eos returns HTTP 500 while api/ses/ returns
# one row per language (70 notified bodies become 1,890).
EUDAMED_BASE = "https://ec.europa.eu/tools/eudamed"
EUDAMED_MAX_PAGE = 300
EUDAMED_SORT = "ulid,ASC"  # unique; sorting on eudamedIdentifier returns 500
EUDAMED_ACTOR_TYPES = {
    "manufacturer": "refdata.actor-type.manufacturer",
    "importer": "refdata.actor-type.importer",
    "authorised-representative": "refdata.actor-type.authorised-representative",
    "system-procedure-pack-producer": "refdata.actor-type.system-procedure-pack-producer",
    "mf": "refdata.actor-type.manufacturer",
    "im": "refdata.actor-type.importer",
    "ar": "refdata.actor-type.authorised-representative",
    "sppp": "refdata.actor-type.system-procedure-pack-producer",
}


# ---------------------------------------------------------------- http ----

_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def force_ipv4() -> None:
    """The equivalent of ``curl -4``. Python has no Happy Eyeballs, so a host
    with AAAA records on a network with no IPv6 route stalls for minutes."""

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(int(value.strip())))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return None if parsed is None else max(0.0, parsed.timestamp() - time.time())


class Fetcher:
    """Sequential, throttled, retrying HTTP GET with an identifiable UA."""

    def __init__(
        self,
        user_agent: str,
        delay: float,
        timeout: float,
        retries: int,
        raw_dir: str | None,
        verbose: bool,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip"})
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.raw_dir = raw_dir
        self.verbose = verbose
        self._last = None
        self._n = 0
        if raw_dir:
            import pathlib

            pathlib.Path(raw_dir).mkdir(parents=True, exist_ok=True)

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"eufinreg-solo: {message}", file=sys.stderr)

    def _throttle(self) -> None:
        if self._last is not None and self.delay > 0:
            remaining = self.delay - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)

    def _record(self, response: requests.Response, label: str) -> None:
        if not self.raw_dir:
            return
        import pathlib

        self._n += 1
        directory = pathlib.Path(self.raw_dir)
        ext = ".json" if "json" in (response.headers.get("Content-Type") or "") else ".txt"
        (directory / f"{self._n:05d}_{label}{ext}").write_bytes(response.content)
        with (directory / "manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "seq": self._n,
                        "url": response.url,
                        "status": response.status_code,
                        "content_type": response.headers.get("Content-Type"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "bytes": len(response.content),
                        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def get(
        self, url: str, params: dict[str, Any] | None = None, label: str = "response"
    ) -> requests.Response:
        return self.request("GET", url, params, label=label)

    def post_json(self, url: str, body: Any, label: str = "response") -> Any:
        """A couple of registers put their search behind POST. Still a read."""
        return self.request("POST", url, json_body=body, label=label).json()

    def request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        label: str = "response",
    ) -> requests.Response:
        last = "no attempt"
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, timeout=(10.0, self.timeout)
                )
            except requests.RequestException as exc:
                self._last = time.monotonic()
                last = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError)):
                    last += " (try --ipv4 if your network has no IPv6 route)"
                if attempt >= self.retries:
                    break
                time.sleep(min(2**attempt, 60))
                continue
            self._last = time.monotonic()
            if response.status_code in RETRY_STATUS:
                last = f"HTTP {response.status_code}"
                if attempt >= self.retries:
                    break
                wait = parse_retry_after(response.headers.get("Retry-After"))
                self._log(f"{last}, retrying in {wait if wait is not None else 2**attempt:.0f}s")
                time.sleep(wait if wait is not None else min(2**attempt, 60))
                continue
            self._record(response, label)
            if not response.ok:
                raise SystemExit(f"eufinreg-solo: HTTP {response.status_code} for {response.url}")
            return response
        raise SystemExit(f"eufinreg-solo: giving up on {url} ({last})")


# ------------------------------------------------------------- reading ----


def quote_term(value: str) -> str:
    if value and all(c.isalnum() or c in "-_." for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def solr_params(core: str, args) -> dict[str, Any]:
    if args.query:
        q = args.query
    elif args.select and core == "esma_registers_upreg":
        q = f"{{!join from=id to=_root_}}ae_entityTypeCode:{quote_term(args.select)}"
    else:
        q = "*:*"
    params: dict[str, Any] = {"q": q, "wt": "json"}
    if core == "esma_registers_upreg":
        types = list(UPREG_DOC_TYPES)
        if args.include_history:
            types += list(UPREG_HISTORY_TYPES)
        params["fq"] = f"entity_type:({' OR '.join(types)})"
    return params


def iter_solr(fetcher: Fetcher, core: str, args) -> Iterator[dict[str, Any]]:
    url = f"{SOLR_BASE}/{core}/select"
    params = solr_params(core, args)
    # Leading with _root_ keeps each entity's parent and children contiguous;
    # cursor paging only needs the sort to end in the unique id field.
    sort = "_root_ asc,id asc" if core == "esma_registers_upreg" else "id asc"
    params.update({"rows": max(1, min(args.page_size, 1000)), "sort": sort})
    cursor, page, seen = "*", 0, 0
    while True:
        page += 1
        params["cursorMark"] = cursor
        payload = fetcher.get(url, params, label=f"solr-p{page:04d}").json()
        docs = (payload.get("response") or {}).get("docs") or []
        fetcher._log(f"page {page}: {len(docs)} doc(s)")
        for doc in docs:
            yield doc
            seen += 1
            if args.max_docs and seen >= args.max_docs:
                return
        nxt = payload.get("nextCursorMark")
        if not docs or not nxt or nxt == cursor:
            return
        cursor = nxt


def read_mica_csv(fetcher: Fetcher, filename: str, args) -> list[dict[str, Any]]:
    response = fetcher.get(f"{MICA_BASE}/{filename}", label=filename)
    if response.encoding is None or "charset" not in (response.headers.get("Content-Type") or ""):
        response.encoding = "utf-8-sig"
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(response.text)):
        row: dict[str, Any] = {}
        for name, value in raw.items():
            if name is None:
                extra = [v for v in (value or []) if v]
                if extra:
                    row["_extra_columns"] = SEP.join(extra)
                continue
            name = name.strip()
            if name:
                row[name] = (value or "").strip()
        if any(row.values()):
            if "ac_serviceCode" in row and not args.no_derived:
                row["ac_serviceCode_normalised"] = normalise_services(row["ac_serviceCode"])
            rows.append(row)
        if args.max_docs and len(rows) >= args.max_docs:
            break
    return rows


def read_eba_psd(fetcher: Fetcher, args) -> list[dict[str, Any]]:
    """The EBA PSD2 register, from its nightly checksum-verified golden copy.

    Properties arrive as a list of single-key objects and Services as a map
    keyed by ISO-2 country — the latter is the passporting footprint, and the
    only place the real geographic reach of an institution is recorded.
    """
    import hashlib
    import zipfile

    meta = fetcher.get(EBA_FILE_METADATA, label="eba-filemetadata").json()
    base = (meta.get("golden_copy_path_context") or f"{EBA_REGISTER}/downloads/PSDMD/").rstrip("/")
    relative = meta.get("latest_version_relative_zip_path") or ""
    if not relative:
        raise SystemExit("eufinreg-solo: EBA file metadata names no download")
    url = f"{base}/{relative.lstrip('/')}"

    archive = fetcher.get(url, label="eba-goldencopy").content
    expected = str(meta.get("sha256_hash") or "").strip().lower()
    actual = hashlib.sha256(archive).hexdigest()
    if expected and actual != expected:
        raise SystemExit(
            f"eufinreg-solo: {url} failed its SHA-256 check "
            f"(register says {expected}, got {actual}) — truncated download, retry"
        )

    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [n for n in bundle.namelist() if n.lower().endswith(".json")]
        if not names:
            raise SystemExit(f"eufinreg-solo: {url} contains no .json member")
        body = bundle.read(names[0])
        sidecar = f"{names[0]}.sha256"
        if sidecar in bundle.namelist():
            want = bundle.read(sidecar).decode("ascii", "replace").strip().lower()
            if want and hashlib.sha256(body).hexdigest() != want:
                raise SystemExit(f"eufinreg-solo: {names[0]} failed its SHA-256 check")

    payload = json.loads(body.decode("utf-8-sig"))
    records: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:  # the entity array is nested; find it rather than index into it
        node = stack.pop()
        if isinstance(node, list):
            found = [x for x in node if isinstance(x, dict) and "EntityCode" in x]
            if len(found) > len(records):
                records = found
            stack.extend(x for x in node if isinstance(x, list))

    wanted = (args.select or "").strip().upper()
    if wanted == "ALL":
        selected = None
    elif wanted:
        selected = {
            code if code.startswith("PSD_") else f"PSD_{code}"
            for code in (part.strip() for part in wanted.split(","))
            if code
        }
    else:
        selected = set(EBA_INSTITUTIONS)

    rows: list[dict[str, Any]] = []
    for record in records:
        if selected is not None and record.get("EntityType") not in selected:
            continue
        row: dict[str, Any] = {
            "CA_OwnerID": record.get("CA_OwnerID", ""),
            "EntityCode": record.get("EntityCode", ""),
            "EntityType": record.get("EntityType", ""),
        }
        for entry in record.get("Properties") or []:
            for name, value in (entry or {}).items():
                row[name] = SEP.join(str(v) for v in value) if isinstance(value, list) else value
        services: OrderedDict[str, list[str]] = OrderedDict()
        for entry in record.get("Services") or []:
            for country, codes in (entry or {}).items():
                bucket = services.setdefault(country, [])
                for code in codes if isinstance(codes, list) else [codes]:
                    if code and code not in bucket:
                        bucket.append(code)
        countries = sorted(services)
        row["ENT_SER"] = SEP.join(sorted({c for codes in services.values() for c in codes}))
        row["ENT_SER_COU"] = SEP.join(countries)
        row["ENT_SER_COU_count"] = str(len(countries))
        row["ENT_SER_BY_COU"] = SEP.join(f"{c}={','.join(services[c])}" for c in countries)
        row["__EBA_EntityVersion"] = record.get("__EBA_EntityVersion", "")
        rows.append(row)
        if args.max_docs and len(rows) >= args.max_docs:
            break
    return rows


def read_eudamed(fetcher: Fetcher, path: str, args) -> list[dict[str, Any]]:
    """One paginated EUDAMED actor endpoint.

    ``languageIso2Code`` is mandatory (see EUDAMED_BASE above) and ``size`` is
    clamped, because the server caps it at 300 without saying so.
    """
    params: dict[str, Any] = {
        "languageIso2Code": "en",
        "size": max(1, min(args.page_size, EUDAMED_MAX_PAGE)),
        "sort": EUDAMED_SORT,
    }
    if args.select and args.select.strip().upper() != "ALL":
        codes = []
        for part in args.select.replace(";", ",").split(","):
            token = part.strip().lower()
            if not token:
                continue
            code = token if token.startswith("refdata.") else EUDAMED_ACTOR_TYPES.get(token)
            if not code:
                raise SystemExit(
                    f"eufinreg-solo: unknown actor type {part.strip()!r}; try ALL, "
                    f"manufacturer, importer, authorised-representative or "
                    f"system-procedure-pack-producer"
                )
            if code not in codes:
                codes.append(code)
        if codes:
            params["actorTypeCode"] = ",".join(codes)
    if args.query:
        for chunk in args.query.split("&"):
            if "=" not in chunk:
                raise SystemExit(
                    f"eufinreg-solo: --query for EUDAMED expects url parameters, got {chunk!r}"
                )
            name, _, value = chunk.partition("=")
            params[name.strip()] = value.strip()

    rows: list[dict[str, Any]] = []
    page = 0
    while True:
        params["page"] = page
        payload = fetcher.get(f"{EUDAMED_BASE}/{path}", params, f"eudamed-p{page:04d}").json()
        content = payload.get("content") or []
        for record in content:
            row: dict[str, Any] = {}
            for name, value in record.items():
                if value is None:
                    row[name] = ""
                elif isinstance(value, (str, int, float, bool)):
                    row[name] = value
                elif isinstance(value, dict) and "code" in value:
                    row[name] = value["code"]
                    for extra in ("srnCode", "category"):
                        if value.get(extra) is not None:
                            row[f"{name}_{extra}"] = value[extra]
                elif isinstance(value, dict) and "texts" in value:
                    row[name] = SEP.join(
                        str(t.get("text")) for t in value["texts"] or [] if t.get("text")
                    )
                elif name == "legislationLinks" and isinstance(value, list):
                    row["legislationCodes"] = SEP.join(
                        str(e.get("legislationCode")) for e in value if e.get("legislationCode")
                    )
                    row["legislationLinks"] = SEP.join(
                        str(e.get("link")) for e in value if e.get("link")
                    )
                else:
                    row[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
            rows.append(row)
            if args.max_docs and len(rows) >= args.max_docs:
                return rows
        if payload.get("last") is True or not content:
            return rows
        page += 1
        total = payload.get("totalPages")
        if isinstance(total, int) and page >= total:
            return rows


def read_ctis(fetcher: Fetcher, args) -> list[dict[str, Any]]:
    """CTIS trial search. `--query` is free text, or a raw searchCriteria object."""
    criteria: dict[str, Any] = {}
    if args.query and args.query.strip():
        text = args.query.strip()
        criteria = json.loads(text) if text.startswith("{") else {"containAll": text}
    if args.select:
        raise SystemExit("eufinreg-solo: ctis has no --select; use --query")

    size = max(1, min(args.page_size, CTIS_MAX_PAGE))
    rows: list[dict[str, Any]] = []
    total = None
    page = 1  # 1-based
    while True:
        body = {
            "pagination": {"page": page, "size": size},
            "sort": {"property": "ctNumber", "direction": "ASC"},
            "searchCriteria": criteria,
        }
        payload = fetcher.post_json(CTIS_SEARCH, body, f"ctis-p{page:04d}")
        info = payload.get("pagination") or {}
        if total is None:
            total = info.get("totalRecords")
        data = payload.get("data") or []
        for record in data:
            row = {}
            for name, value in record.items():
                if value is None:
                    row[name] = ""
                elif isinstance(value, list):
                    row[name] = SEP.join(str(v) for v in value if v not in (None, ""))
                elif isinstance(value, (str, int, float, bool)):
                    row[name] = value
                else:
                    row[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
            rows.append(row)
            if args.max_docs and len(rows) >= args.max_docs:
                return rows
        if not data or not info.get("nextPage"):
            break
        page += 1
    if total and len(rows) < total and not args.max_docs and not args.quiet:
        print(
            f"eufinreg-solo: warning: CTIS returned {len(rows)} of {total} matching trials "
            f"— its search stops at {CTIS_WINDOW:,} records however you page. "
            f"Narrow it with --query to reach the rest.",
            file=sys.stderr,
        )
    return rows


def read_records(fetcher: Fetcher, kind: str, target: str, args) -> list[dict[str, Any]]:
    """Read a whole source, whichever kind of interface it happens to have."""
    if kind == "solr":
        return list(iter_solr(fetcher, target, args))
    if kind == "csv":
        return read_mica_csv(fetcher, target, args)
    if kind == "eba":
        return read_eba_psd(fetcher, args)
    if kind == "eudamed":
        return read_eudamed(fetcher, target, args)
    if kind == "ctis":
        return read_ctis(fetcher, args)
    raise SystemExit(f"eufinreg-solo: unknown source kind {kind!r}")


# ----------------------------------------------- MiCA service normalising --
# The field description says ac_serviceCode is the enum a..j of MiCA Art.
# 3(1)(16). In the live file it is free text typed by 27 national authorities.

_LETTER_PREFIX = re.compile(r"^\(?([a-j])[).\s\t]", re.IGNORECASE)
_SEPARATORS = re.compile(
    r"\s*\|\s*|[\r\n]+|\s*/\s*|\s*\bI\s+(?=[a-j][.)\s])"
    r"|\s*[,;]\s*(?=[a-j][.)]\s)|(?<=[a-z])\s*\.\s*(?=[a-j][.)]\s)"
)
_KEYWORDS = (
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


def split_service_cell(cell: str) -> list[str]:
    if not cell:
        return []
    return [p.strip(" \t.;,") for p in _SEPARATORS.sub("|", cell).split("|") if p.strip(" \t.;,")]


def service_letter(fragment: str) -> str | None:
    match = _LETTER_PREFIX.match(fragment.strip())
    if match:
        return match.group(1).lower()
    lowered = " ".join(fragment.lower().split())
    for letter, keyword in _KEYWORDS:
        if keyword in lowered:
            return letter
    return None


def normalise_services(cell: str) -> str:
    letters: list[str] = []
    unknown = False
    for fragment in split_service_cell(cell):
        letter = service_letter(fragment)
        if letter is None:
            unknown = True
        elif letter not in letters:
            letters.append(letter)
    result = "|".join(sorted(letters))
    return (f"{result}|?" if result else "?") if unknown else result


# ------------------------------------------------------------ flatten -----


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return SEP.join(scalar(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


DROP = {"_version_", "collectorParent"}


def flatten(docs: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Group a mixed parent/child array by _root_ into one row per entity."""
    docs = list(docs)
    if not any(d.get("type_s") == "parent" for d in docs):
        return [{k: scalar(v) for k, v in d.items() if k not in DROP} for d in docs]

    parents: dict[str, dict[str, Any]] = {}
    children: dict[str, dict[str, list[dict[str, Any]]]] = {}
    order: list[str] = []
    for doc in docs:
        key = str(doc.get("_root_") or doc.get("id") or "")
        if key not in children:
            children[key] = {}
            order.append(key)
        if doc.get("type_s") == "parent" and key not in parents:
            parents[key] = doc
        elif doc.get("type_s") == "parent":
            children[key].setdefault("duplicateParent", []).append(doc)
        else:
            children[key].setdefault(str(doc.get("entity_type") or "child"), []).append(doc)

    rows: list[dict[str, str]] = []
    for key in order:
        parent = parents.get(key)
        row: dict[str, str] = {}
        if parent is None:
            row["_orphan"], row["_root_"] = "true", key
        else:
            row.update({k: scalar(v) for k, v in parent.items() if k not in DROP})
        for group, group_docs in children[key].items():
            row[f"{group}_count"] = str(len(group_docs))
            names: dict[str, None] = {}
            for child in group_docs:
                for name in child:
                    names.setdefault(name, None)
            for name in names:
                if name in DROP or name in ("_root_", "entity_type"):
                    continue
                row[f"{group}_{name}"] = SEP.join(scalar(c.get(name)) for c in group_docs)
        rows.append(row)
    return rows


# ------------------------------------------------------------- modes ------


def profile(records: list[dict[str, Any]], title: str) -> str:
    if not records:
        return f"{title}\n  (nothing sampled)\n"
    present, filled, distinct, samples = Counter(), Counter(), {}, {}
    for record in records:
        for name, raw in record.items():
            present[name] += 1
            text = scalar(raw)
            if not text:
                continue
            filled[name] += 1
            distinct.setdefault(name, set()).add(text)
            bucket = samples.setdefault(name, OrderedDict())
            if len(bucket) < 3:
                short = " ".join(text.split())
                bucket.setdefault(short if len(short) <= 60 else short[:59] + "…", None)
    width = max(len(n) for n in present)
    lines = [
        title,
        f"  sampled {len(records)} record(s)",
        "",
        f"  {'field'.ljust(width)}  present  filled  distinct  samples",
        f"  {'-' * width}  -------  ------  --------  {'-' * 40}",
    ]
    for name, count in sorted(present.items(), key=lambda kv: (-kv[1], kv[0])):
        pct = f"{count / len(records) * 100:.0f}%"
        shown = " ; ".join(samples.get(name, ())) or "(always empty)"
        lines.append(
            f"  {name.ljust(width)}  {pct:>7}  {filled[name]:>6}  "
            f"{len(distinct.get(name, ())):>8}  {shown}"
        )
    return "\n".join(lines) + "\n"


def solr_facet(fetcher: Fetcher, core: str, field: str, args) -> list[tuple[str, int]]:
    params = solr_params(core, args)
    params.update(
        {"rows": 0, "facet": "true", "facet.field": field, "facet.limit": -1, "facet.mincount": 1}
    )
    payload = fetcher.get(f"{SOLR_BASE}/{core}/select", params, label="facet").json()
    buckets = ((payload.get("facet_counts") or {}).get("facet_fields") or {}).get(field) or []
    pairs = [(str(buckets[i]), int(buckets[i + 1])) for i in range(0, len(buckets) - 1, 2)]
    return sorted(pairs, key=lambda kv: (-kv[1], kv[0]))


def local_counts(rows: list[dict[str, Any]], field: str) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    split = MULTI_VALUE.get(field)
    for row in rows:
        text = scalar(row.get(field, "")).strip()
        if not text:
            continue
        if field == "ac_serviceCode":
            parts = split_service_cell(text)
        elif split:
            parts = [p.strip() for p in text.split(split)]
        else:
            parts = [text]
        for part in parts:
            if part:
                counter[part] += 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


@contextmanager
def open_out(path: str | None):
    """Write to ``path``, or to stdout when it is None — without closing stdout."""
    if not path:
        yield sys.stdout
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        yield handle


def print_values(field: str, pairs: list[tuple[str, int]]) -> None:
    if not pairs:
        print(f"\ndistinct values of {field!r}: none found (is that a real field? run --inspect)\n")
        return
    width = max(len(str(c)) for _, c in pairs)
    print(f"\ndistinct values of {field!r} ({len(pairs)} found)\n")
    for value, count in pairs:
        print(f"  {str(count).rjust(width)}  {value}")
    print()


# --------------------------------------------------------------- main -----


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eufinreg-solo",
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="sources: " + ", ".join(SOURCES) + ", solr:<core>",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--source", default="upreg")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument(
        "--inspect",
        nargs="?",
        type=int,
        const=50,
        metavar="N",
        help="sample N records and report the fields that really exist",
    )
    parser.add_argument("--list-values", action="append", default=[], metavar="FIELD")
    parser.add_argument("--list-enums", action="store_true")
    parser.add_argument(
        "--select",
        metavar="VALUE",
        help="upreg: entity type code, e.g. CSP. eba-psd: ALL, or codes such as "
        "PI/EMI/AISP (default: institutions only, excluding ~322k agents)",
    )
    parser.add_argument("--query", metavar="Q", help="raw Solr query")
    parser.add_argument("--include-history", action="store_true")
    parser.add_argument("--contains", action="append", default=[], metavar="TEXT")
    parser.add_argument("--field", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--limit", type=int, metavar="N")
    parser.add_argument("-o", "--output", metavar="PATH")
    parser.add_argument("--format", choices=("csv", "json", "jsonl"), default="csv")
    parser.add_argument("--raw", metavar="DIR", help="save untouched responses here")
    parser.add_argument("--no-flatten", action="store_true")
    parser.add_argument("--no-derived", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-docs", type=int)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="please change this to your own repo or contact address",
    )
    parser.add_argument("-4", "--ipv4", action="store_true", help="IPv4 only, like curl -4")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def resolve(source: str) -> tuple[str, str]:
    if source.startswith("solr:"):
        core = source.split(":", 1)[1]
        return "solr", core if core.startswith("esma_registers_") else f"esma_registers_{core}"
    if source not in SOURCES:
        raise SystemExit(
            f"eufinreg-solo: unknown source {source!r}; known: {', '.join(SOURCES)}, solr:<core>"
        )
    kind, target, _ = SOURCES[source]
    return kind, target


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_sources:
        width = max(len(k) for k in SOURCES)
        for key, (_, _, note) in SOURCES.items():
            print(f"  {key.ljust(width)}  {note}")
        print(f"  {'solr:<core>'.ljust(width)}  any ESMA Solr core by name")
        return 0

    if args.ipv4:
        force_ipv4()
    if args.user_agent == DEFAULT_USER_AGENT and not args.quiet:
        print(
            "eufinreg-solo: please set --user-agent to your own repo or contact address",
            file=sys.stderr,
        )

    kind, target = resolve(args.source)
    fetcher = Fetcher(
        args.user_agent, args.delay, args.timeout, args.retries, args.raw, not args.quiet
    )

    fields = list(args.list_values)
    if args.list_enums:
        fields = [f for f in ENUM_FIELDS.get(args.source, ()) if f not in fields] + fields
        if not fields:
            print(
                f"eufinreg-solo: no enum fields known for {args.source!r}; use --inspect",
                file=sys.stderr,
            )
    if fields:
        rows = [] if kind == "solr" else read_records(fetcher, kind, target, args)
        for name in fields:
            pairs = (
                solr_facet(fetcher, target, name, args)
                if kind == "solr"
                else local_counts(rows, name)
            )
            print_values(name, pairs)
        return 0

    if args.inspect is not None:
        size = max(1, args.inspect)
        args.max_docs = min(args.max_docs or 10_000, max(size * 4, size + 50))
        records = read_records(fetcher, kind, target, args)
        print(profile(records[:size], f"{args.source} — fields as received"))
        if any(r.get("type_s") for r in records):
            print(
                profile(
                    flatten(records)[:size],
                    f"{args.source} — columns after flattening to one row per entity",
                )
            )
        return 0

    records = read_records(fetcher, kind, target, args)
    if not args.quiet:
        print(f"eufinreg-solo: received {len(records)} record(s)", file=sys.stderr)
    rows: list[dict[str, Any]] = records if args.no_flatten else flatten(records)

    pairs = []
    for item in args.field:
        if "=" not in item:
            raise SystemExit(f"eufinreg-solo: --field expects NAME=VALUE, got {item!r}")
        name, _, value = item.partition("=")
        pairs.append((name.strip(), value.strip()))
    if pairs:
        known = {k for row in rows for k in row}
        for name, _ in pairs:
            if name not in known and not args.quiet:
                print(
                    f"eufinreg-solo: warning: field {name!r} does not exist in any record "
                    f"— it will match nothing. Run --inspect.",
                    file=sys.stderr,
                )

    kept = []
    for row in rows:
        values = [scalar(v).casefold() for v in row.values()]
        if not all(any(n.casefold() in v for v in values) for n in args.contains):
            continue
        if not all(scalar(row.get(n, "")).strip().casefold() == v.casefold() for n, v in pairs):
            continue
        kept.append(row)
        if args.limit and len(kept) >= args.limit:
            break

    columns: list[str] = []
    for row in rows:
        for name in row:
            if name not in columns:
                columns.append(name)

    with open_out(args.output) as handle:
        if args.format == "csv":
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in kept:
                writer.writerow({c: scalar(row.get(c, "")) for c in columns})
        elif args.format == "jsonl":
            for row in kept:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            json.dump(kept, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    if not args.quiet:
        print(f"eufinreg-solo: wrote {len(kept)} row(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
