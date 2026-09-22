"""The read path, with the parts a long-running process needs that a CLI does not.

The CLI is a one-shot: it fetches, writes, exits. Putting a web front end on the
same code changes three things, and all three are about not becoming a nuisance
to the registers:

* **Caching.** A browser makes a request every time somebody clicks. Without a
  cache, one impatient user re-downloads the EBA's 19 MB golden copy ten times a
  minute. Answers are cached per (source, selection) for a TTL that reflects how
  often the register actually changes — nightly for the EBA, weekly for MiCA,
  monthly for GISA.
* **Serialisation.** Concurrent HTTP handlers must not turn into concurrent
  register requests. One lock, one outbound request at a time — the same rule
  the CLI follows by being single-threaded, made explicit.
* **Bounded work.** A public-facing endpoint has to be able to say no. Row
  limits are enforced server-side, and sources that cannot answer cheaply are
  marked so the UI can warn before the click rather than after.

Everything here is transport and policy. No register-specific logic lives in
this module — that all stays in :mod:`eufinreg.sources`, which is what makes the
API a thin shell over the CLI rather than a second implementation.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .filters import FieldFilter, apply_filters
from .flatten import column_order, flatten_auto
from .http import DEFAULT_USER_AGENT, Fetcher
from .observability import Metrics
from .sources import ALL_SOURCES, Query, UnknownSource, get_source
from .store import Store


class PersonalDataRefused(PermissionError):
    """The selection asked for rows the source flags as being about people.

    Not a bug and not a permissions failure in the usual sense: the CLI can
    fetch these, and an operator can enable them deliberately. What must not
    happen is an open HTTP endpoint serving 322,314 named individuals because
    nobody thought about it.
    """


#: How long an answer stays fresh, by source, in seconds. These follow the
#: registers' own publication cadence — there is no point re-fetching GISA daily
#: when it is regenerated monthly, and no point caching a UID lookup for a week.
CACHE_TTL = {
    "eba-psd": 6 * 3600,  # regenerated nightly
    "mica-casp": 12 * 3600,  # weekly
    "mica-art": 12 * 3600,
    "mica-emt": 12 * 3600,
    "mica-other": 12 * 3600,
    "mica-ncasp": 12 * 3600,
    "finma": 6 * 3600,  # regenerated daily, early morning
    "gisa": 24 * 3600,  # monthly vintage
    "gisa-codes": 24 * 3600,
    "ch-uid": 900,  # a lookup, not a bulk file
    "vies": 900,  # a validator; an answer goes stale as soon as a firm deregisters
    "gleif": 6 * 3600,  # a golden copy is published daily
    "ted": 6 * 3600,  # notices publish on working days
}
DEFAULT_TTL = 3600

#: Sources whose cheapest answer is still a large download. The UI shows this
#: before the request rather than after.
EXPENSIVE = {
    "eba-psd": "downloads a 19 MB archive and parses 217 MB of JSON",
    "gisa": "downloads a 9 MB archive and parses 211 MB / 1.03 M rows",
    "eudamed-eo": "pages through ~49,000 organisations, 164 requests",
    "funds": "212,000 documents",
    "saris": "218,000 documents",
    "gleif": (
        "pages 200 records at a time and stops at 10,000 — every DACH country is "
        "larger than that on its own"
    ),
}


@dataclass
class CacheEntry:
    rows: list[dict[str, Any]]
    columns: list[str]
    warnings: list[str]
    fetched_at: float
    expires_at: float
    #: Where the rows came from: ``"live"`` (a register) or ``"store"`` (the
    #: archive). Reported on every answer, because an operator debugging a wrong
    #: number needs to know which of the two they are looking at.
    origin: str = "live"
    #: The snapshot stamp, when the answer came from the archive.
    snapshot: str = ""


@dataclass
class RegisterService:
    """Cached, serialised access to every source, for a long-running process."""

    user_agent: str = DEFAULT_USER_AGENT
    delay: float = 1.0
    timeout: float = 60.0
    retries: int = 5
    max_rows: int = 5000
    #: When set, answers come from the archive and no register is contacted
    #: unless a caller explicitly asks for a live read. This is the production
    #: posture: the ingest talks to registers, the API talks to the store.
    store: Store | None = None
    #: Serve rows a source flags as personal data. Off unless an operator says
    #: otherwise — see :class:`PersonalDataRefused`.
    allow_personal_data: bool = False
    #: Allow a caller to bypass the store and hit the register. Off when a store
    #: is configured, because that is what the store is for.
    allow_live: bool = True
    #: Everything this process did to a register, since it started. Each
    #: request-scoped Fetcher merges its counters in here when it closes, so the
    #: totals survive the client that produced them.
    metrics: Metrics = field(default_factory=Metrics)
    _cache: dict[tuple[Any, ...], CacheEntry] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _clock: Any = time.time
    _served: Counter[str] = field(default_factory=Counter, repr=False)

    # -- catalogue ---------------------------------------------------------

    def catalogue(self) -> list[dict[str, Any]]:
        """Everything the UI needs to build its own controls, from the sources."""
        return [
            {
                "key": source.key,
                "title": source.title,
                "docs_url": source.docs_url,
                "select_help": source.select_help,
                "enum_fields": list(source.enum_fields),
                "key_columns": list(source.key_columns),
                # The UI needs to know which column is the name and which is the
                # identifier, or it can only dump every column and hope.
                "identifier_columns": list(source.identifier_columns),
                "name_columns": list(source.name_columns),
                "block_structured": bool(source.block_structured),
                "expensive": EXPENSIVE.get(source.key, ""),
                "cache_ttl": CACHE_TTL.get(source.key, DEFAULT_TTL),
                "jurisdiction": source.jurisdiction,
                "cadence_hours": source.cadence_hours,
                "data_licence": source.data_licence,
                "personal_data": source.personal_data,
                "bulk_readable": source.bulk_readable,
                "archived": bool(self.store and self.store.latest(source.key)),
            }
            for source in ALL_SOURCES
        ]

    # -- reading -----------------------------------------------------------

    def rows(
        self,
        source_key: str,
        *,
        select: str | None = None,
        query: str | None = None,
        contains: Sequence[str] = (),
        fields: Sequence[str] = (),
        limit: int | None = None,
        max_docs: int | None = None,
        live: bool = False,
    ) -> dict[str, Any]:
        """One answer: rows from the archive or the register, then local filters.

        The cache key deliberately excludes ``contains``/``field``/``limit``,
        because those are applied locally: filtering an answer already in hand
        costs nothing and hits no register.
        """
        source = self._source(source_key)
        note = source.personal_data_for(select)
        if note and not self.allow_personal_data:
            raise PersonalDataRefused(note)

        entry = self._answer(source_key, select=select, query=query, max_docs=max_docs, live=live)
        field_filters = [FieldFilter.parse(text) for text in fields]
        filtered, report = apply_filters(
            entry.rows,
            contains=list(contains),
            field_filters=field_filters,
            limit=min(limit or self.max_rows, self.max_rows),
        )
        return {
            "source": source_key,
            "rows": filtered,
            "columns": entry.columns,
            "total_rows": len(entry.rows),
            "returned_rows": len(filtered),
            "truncated": len(filtered) < len(entry.rows),
            "warnings": [*entry.warnings, *report.warnings()],
            "origin": entry.origin,
            "snapshot": entry.snapshot,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.fetched_at)),
            "cached_until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.expires_at)),
        }

    def changes(
        self, source_key: str, *, since: str = "", limit: int | None = None
    ) -> dict[str, Any]:
        """Recorded change events for one source, newest snapshot last.

        Only ever from the archive: a change is a statement about two points in
        time, and there is no live equivalent to fall back to.
        """
        if self.store is None:
            raise ValueError(
                "no store is configured, so there is no change history to read. "
                "Run 'eufinreg ingest' on a schedule and start the server with --store."
            )
        self._source(source_key)  # validates the key
        events = self.store.read_changes(source_key, since=since)
        if limit:
            events = events[-limit:]
        return {"source": source_key, "since": since, "count": len(events), "events": events}

    def values(
        self,
        source_key: str,
        field_name: str,
        *,
        select: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """``--list-values`` over HTTP, straight from the source implementation."""
        source = self._source(source_key)
        with self._lock:
            fetcher = self._fetcher()
            try:
                pairs, note = source.count_values(
                    fetcher, Query(select=select, raw_query=query), field_name
                )
            finally:
                self._harvest(fetcher)
                fetcher.close()
        return {
            "source": source_key,
            "field": field_name,
            "values": [{"value": value, "count": count} for value, count in pairs],
            "note": note or "",
        }

    # -- internals ---------------------------------------------------------

    def _source(self, key: str):
        try:
            return get_source(key)
        except UnknownSource as exc:
            # KeyError's own str() would wrap the message in quotes; the handler
            # reads .args[0] so the client sees the sentence, not a repr.
            raise KeyError(exc.args[0] if exc.args else f"unknown source {key!r}") from exc

    def _fetcher(self) -> Fetcher:
        return Fetcher(
            user_agent=self.user_agent,
            delay=self.delay,
            timeout=self.timeout,
            retries=self.retries,
        )

    def _harvest(self, fetcher: Fetcher) -> None:
        """Fold a finished client's counters into the process totals.

        Fetchers are per-request and short-lived; the numbers an operator wants
        are per-process. Merging on close is the only point where both exist.
        """
        self.metrics.merge(fetcher.metrics)

    def observe(self, route: str, outcome: str = "ok") -> None:
        """Count one served request, by route and outcome."""
        self._served[f"{route} {outcome}"] += 1

    def metrics_snapshot(self) -> dict[str, Any]:
        payload = self.metrics.as_dict()
        payload["served"] = dict(self._served)
        payload["cache_entries"] = len(self._cache)
        payload["cached_rows"] = sum(len(entry.rows) for entry in self._cache.values())
        archive = self.store_status()
        payload["archive_sources"] = len(archive)
        payload["archive_stale"] = sum(1 for entry in archive if entry["stale"])
        payload["archive_rows"] = sum(entry.get("rows", 0) for entry in archive)
        return payload

    def last_run(self) -> dict[str, Any]:
        """The most recent ingest, so /api/health can report on the writer too.

        A read API that only reports on itself is half a health check: the thing
        that usually breaks is the job filling the archive, not the process
        serving it.
        """
        if self.store is None:
            return {}
        runs = self.store.runs(limit=1)
        if not runs:
            return {}
        record = runs[-1]
        return {
            "run_id": record.get("run_id", ""),
            "finished_at": record.get("finished_at", "") or record.get("written_at", ""),
            "ok": record.get("ok", 0),
            "failed": record.get("failed", 0),
            "sources": record.get("sources", 0),
            "seconds": record.get("seconds", 0),
            "requests": (record.get("http") or {}).get("requests", 0),
            "retries": (record.get("http") or {}).get("retries", 0),
        }

    def _from_store(
        self, source_key: str, *, select: str | None, query: str | None
    ) -> CacheEntry | None:
        """The archived answer, when the archive was taken under this selection.

        A snapshot of ``--select bank`` cannot answer a question about every
        institution, so a mismatched selection falls through to a live read
        rather than quietly returning a subset.
        """
        if self.store is None:
            return None
        ref = self.store.latest(source_key)
        if ref is None:
            return None
        if str(ref.meta.get("select", "") or "") != (select or ""):
            return None
        if str(ref.meta.get("query", "") or "") != (query or ""):
            return None
        rows = ref.load().rows
        now = self._clock()
        return CacheEntry(
            rows=rows,
            columns=column_order(rows),
            warnings=[str(w) for w in ref.meta.get("warnings", [])],
            fetched_at=now,
            expires_at=now + CACHE_TTL.get(source_key, DEFAULT_TTL),
            origin="store",
            snapshot=ref.stamp,
        )

    def _answer(
        self,
        source_key: str,
        *,
        select: str | None,
        query: str | None,
        max_docs: int | None,
        live: bool = False,
    ) -> CacheEntry:
        cache_key = (source_key, select or "", query or "", max_docs or 0, bool(live))
        now = self._clock()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now:
            return cached

        if self.store is not None and not live:
            archived = self._from_store(source_key, select=select, query=query)
            if archived is not None:
                self._cache[cache_key] = archived
                return archived
            if not self.allow_live:
                raise ValueError(
                    f"{source_key} has no archived snapshot for this selection and live reads "
                    f"are disabled. Run 'eufinreg ingest --source {source_key}' first."
                )

        source = self._source(source_key)
        # One outbound conversation at a time, whatever the web server does with
        # threads. Two browser tabs must not become two paging loops.
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > self._clock():
                return cached
            fetcher = self._fetcher()
            try:
                request = Query(select=select, raw_query=query, max_docs=max_docs)
                records = list(source.iter_records(fetcher, request))
                warnings = list(source.warnings())
            finally:
                self._harvest(fetcher)
                fetcher.close()

        rows = flatten_auto(
            records,
            source.flatten_config(),
            block_structured=True if source.block_structured else None,
        )
        now = self._clock()
        entry = CacheEntry(
            rows=rows,
            columns=column_order(rows),
            warnings=warnings,
            fetched_at=now,
            expires_at=now + CACHE_TTL.get(source_key, DEFAULT_TTL),
        )
        self._cache[cache_key] = entry
        return entry

    def cache_state(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [
            {
                "source": key[0],
                "select": key[1],
                "query": key[2],
                "rows": len(entry.rows),
                "origin": entry.origin,
                "snapshot": entry.snapshot,
                "age_seconds": int(now - entry.fetched_at),
                "expires_in": int(entry.expires_at - now),
            }
            for key, entry in self._cache.items()
        ]

    def purge(self) -> int:
        removed = len(self._cache)
        self._cache.clear()
        return removed

    def store_status(self) -> list[dict[str, Any]]:
        """Per-source archive freshness, for the readiness endpoint.

        Staleness is measured against the *register's* cadence, not a polling
        schedule: a monthly file 30 hours old is fine, a nightly one is not.
        """
        if self.store is None:
            return []
        cadence = {source.key: source.cadence_hours for source in ALL_SOURCES}
        return self.store.status(cadence, now=self._clock())


def parse_query_string(params: Mapping[str, list[str]]) -> dict[str, Any]:
    """``urllib.parse.parse_qs`` output → the arguments :meth:`RegisterService.rows` takes."""
    first = {name: values[0] for name, values in params.items() if values}
    limit = first.get("limit")
    max_docs = first.get("max_docs")
    return {
        "select": first.get("select") or None,
        "query": first.get("query") or None,
        "contains": params.get("contains", []),
        "fields": params.get("field", []),
        "limit": int(limit) if limit and limit.isdigit() else None,
        "max_docs": int(max_docs) if max_docs and max_docs.isdigit() else None,
        "live": first.get("live", "") in ("1", "true", "yes"),
    }
