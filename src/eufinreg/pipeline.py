"""The scheduled path: fetch → snapshot → diff → record → prune.

This is what runs at 04:00 and what everything else reads. Its requirements are
different from the CLI's, and they are the ordinary requirements of anything
that runs unattended:

* **One failing register does not fail the run.** ESMA being down at 04:00 is
  not a reason to skip FINMA. Each source is isolated; the exit code reports
  that something failed, the report says which.
* **Every run leaves a complete record**, including "nothing changed" — an
  absent change file cannot be distinguished from a run that never happened.
* **Nothing is fetched that cannot be archived.** A source with no "list
  everything" mode is refused with a reason rather than snapshotted from
  whatever one query returned.
* **The raw bytes are kept alongside the rows**, because in six months the
  question will be "did the register say that, or did we parse it wrong".
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import Config, SourceSpec
from .flatten import column_order, flatten_auto
from .http import Fetcher, RawRecorder
from .observability import Metrics, Phases, new_run_id
from .snapshot import diff_snapshots
from .sources import Query, UnknownSource, get_source
from .store import Store, utc_stamp

#: Re-fetch a register only once its archive entry is this fraction of the
#: register's own publication cadence old. A monthly file re-downloaded every
#: night is 29 pointless 9 MB requests a month against a public service, and
#: 29 identical 300 MB snapshots in the archive. The fraction rather than the
#: whole cadence leaves room for a scheduler's jitter: a nightly job at 04:12
#: with a randomised delay must not skip because it ran 23.9 hours later.
REFETCH_FRACTION = 0.5


@dataclass
class IngestResult:
    """What happened to one register in one run."""

    source: str
    ok: bool = False
    skipped: str = ""
    stamp: str = ""
    rows: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    #: The tail of the traceback when something unexpected failed. Kept in the
    #: run record because "it failed last night" is not a bug report.
    traceback: str = ""
    seconds: float = 0.0
    #: Where the time went: fetch, flatten, snapshot, diff. A source that took
    #: 90 seconds is not a diagnosis; 88 of them in `fetch` is.
    phases: dict[str, float] = field(default_factory=dict)
    #: What the HTTP client did on this source's behalf.
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source}: skipped — {self.skipped}"
        if not self.ok:
            return f"{self.source}: FAILED — {self.error}"
        requests = self.metrics.get("requests", 0)
        traffic = f", {requests} request(s)" if requests else ""
        if self.unchanged:
            return f"{self.source}: {self.rows} rows, no change ({self.seconds:.1f}s{traffic})"
        return (
            f"{self.source}: {self.rows} rows, "
            f"+{self.added} ~{self.changed} -{self.removed} ({self.seconds:.1f}s{traffic})"
        )


def ingest_source(
    store: Store,
    spec: SourceSpec,
    config: Config,
    *,
    stamp: str | None = None,
    now: float | None = None,
    force: bool = False,
    run_id: str = "",
    log: Callable[[str], None] = lambda _: None,
) -> IngestResult:
    """Fetch one register into the store, and diff it against the last run."""
    started = time.monotonic()
    result = IngestResult(source=spec.key)
    phases = Phases()

    try:
        source = get_source(spec.key)
    except UnknownSource as exc:
        result.error = str(exc)
        return result

    if not source.bulk_readable:
        result.skipped = (
            f"{spec.key} answers searches, it does not publish a population — "
            f"archiving one query's worth of rows would misrepresent it"
        )
        return result

    if not force and source.cadence_hours:
        existing = store.latest(spec.key)
        age = existing.age_hours(now) if existing else None
        threshold = source.cadence_hours * REFETCH_FRACTION
        if age is not None and age < threshold:
            result.skipped = (
                f"the archive entry is {age:.1f} h old and this register publishes about "
                f"every {source.cadence_hours:.0f} h — nothing can have changed. "
                f"Use --force to fetch anyway."
            )
            result.stamp = existing.stamp if existing else ""
            result.rows = existing.row_count if existing else 0
            return result

    stamp = stamp or utc_stamp(now)
    recorder = RawRecorder(store.raw_dir(spec.key, stamp)) if config.capture_raw else None
    fetcher = Fetcher(
        user_agent=config.user_agent,
        delay=config.delay,
        timeout=config.timeout,
        retries=config.retries,
        recorder=recorder,
    )

    try:
        log(f"{spec.key}: fetching…")
        query = Query(select=spec.select or None, raw_query=spec.query or None)
        with phases.measure("fetch"):
            records = list(source.iter_records(fetcher, query))
        warnings = list(source.warnings())
        with phases.measure("flatten"):
            rows = flatten_auto(
                records,
                source.flatten_config(),
                block_structured=True if source.block_structured else None,
            )

        previous = store.latest(spec.key)
        with phases.measure("write"):
            ref = store.add_snapshot(
                spec.key,
                rows,
                key_columns=source.key_columns,
                meta={
                    "select": spec.select,
                    "query": spec.query,
                    "user_agent": config.user_agent,
                    "title": source.title,
                    "jurisdiction": source.jurisdiction,
                    "docs_url": source.docs_url,
                    "cadence_hours": source.cadence_hours,
                    "data_licence": source.data_licence,
                    "columns": column_order(rows)[:200],
                    "warnings": warnings,
                    "run_id": run_id,
                },
                stamp=stamp,
                compress=config.compress,
            )

        changes = []
        with phases.measure("diff"):
            if previous is not None:
                current = ref.load()
                changes = diff_snapshots(previous.load(), current)
                duplicates = int(current.meta.get("duplicate_keys", 0) or 0)
                if duplicates:
                    warnings.append(
                        f"{duplicates} row(s) share a key with an earlier row and were not "
                        f"compared — {spec.key} is issuing duplicate "
                        f"{', '.join(source.key_columns) or 'identifiers'}"
                    )
            store.write_changes(
                spec.key, stamp, changes, against=previous.stamp if previous else ""
            )

        result.ok = True
        result.stamp = stamp
        result.rows = ref.row_count
        result.added = sum(1 for c in changes if c.kind == "added")
        result.changed = sum(1 for c in changes if c.kind == "changed")
        result.removed = sum(1 for c in changes if c.kind == "removed")
        result.unchanged = previous is not None and not changes
        result.warnings = warnings

        if config.retention_days:
            pruned = store.prune(
                spec.key,
                keep_days=config.retention_days,
                keep_last=config.keep_last,
                now=now,
            )
            if pruned:
                log(f"{spec.key}: pruned {len(pruned)} snapshot(s) past retention")
    except Exception as exc:  # deliberately broad — see below
        # *Any* failure, not just the ones we thought of. A register that
        # changes shape surfaces as a KeyError or a TypeError inside a parser,
        # and catching only the expected exceptions meant one register's schema
        # drift aborted the whole nightly run — breaking the isolation this
        # module promises. KeyboardInterrupt and SystemExit still propagate,
        # because those are the operator talking.
        result.error = f"{type(exc).__name__}: {exc}"
        result.traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[
            -2000:
        ]
    finally:
        fetcher.close()
        result.seconds = time.monotonic() - started
        result.phases = phases.as_dict()
        result.metrics = fetcher.metrics.as_dict()

    return result


def ingest(
    store: Store,
    config: Config,
    *,
    now: float | None = None,
    force: bool = False,
    run_id: str = "",
    log: Callable[[str], None] = lambda _: None,
) -> list[IngestResult]:
    """Run every configured source into ``store``, one after another.

    Sequential on purpose: two registers being fetched at once is still two
    outbound conversations, and the etiquette this project follows does not have
    an exception for "but they are different servers".
    """
    stamp = utc_stamp(now)
    run_id = run_id or new_run_id(now)
    results: list[IngestResult] = []
    for spec in config.enabled_sources():
        result = ingest_source(
            store, spec, config, stamp=stamp, now=now, force=force, run_id=run_id, log=log
        )
        log(result.summary())
        for warning in result.warnings:
            log(f"{spec.key}: warning: {warning}")
        results.append(result)
    return results


def report(results: Sequence[IngestResult], *, run_id: str = "") -> dict[str, Any]:
    """A machine-readable run summary, for logs and for exit-code decisions."""
    totals = Metrics()
    for result in results:
        totals.merge(_metrics_from(result.metrics))
    phases: dict[str, float] = {}
    for result in results:
        for name, seconds in result.phases.items():
            phases[name] = round(phases.get(name, 0.0) + seconds, 3)
    return {
        "run_id": run_id,
        "http": totals.as_dict(),
        "phases": phases,
        "sources": len(results),
        "ok": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok and not r.skipped),
        "skipped": sum(1 for r in results if r.skipped),
        "rows": sum(r.rows for r in results),
        "added": sum(r.added for r in results),
        "changed": sum(r.changed for r in results),
        "removed": sum(r.removed for r in results),
        "seconds": round(sum(r.seconds for r in results), 1),
        "results": [
            {
                "source": r.source,
                "ok": r.ok,
                "skipped": r.skipped,
                "stamp": r.stamp,
                "rows": r.rows,
                "added": r.added,
                "changed": r.changed,
                "removed": r.removed,
                "error": r.error,
                "traceback": r.traceback or None,
                "warnings": r.warnings,
                "seconds": round(r.seconds, 1),
                "phases": r.phases,
                "http": r.metrics,
            }
            for r in results
        ],
    }


def _metrics_from(payload: Mapping[str, Any]) -> Metrics:
    """Rebuild a :class:`Metrics` from the dict a result carries, for summing."""
    metrics = Metrics()
    if not payload:
        return metrics
    metrics.requests = int(payload.get("requests", 0) or 0)
    metrics.retries = int(payload.get("retries", 0) or 0)
    metrics.failures = int(payload.get("failures", 0) or 0)
    metrics.bytes_in = int(payload.get("bytes_in", 0) or 0)
    metrics.seconds_requesting = float(payload.get("seconds_requesting", 0.0) or 0.0)
    metrics.seconds_waiting = float(payload.get("seconds_waiting", 0.0) or 0.0)
    metrics.by_status.update(payload.get("by_status") or {})
    metrics.by_host.update(payload.get("by_host") or {})
    metrics.by_outcome.update(payload.get("by_outcome") or {})
    metrics.slowest = [
        (float(s["seconds"]), str(s["method"]), str(s["url"]))
        for s in (payload.get("slowest") or [])
        if isinstance(s, Mapping)
    ]
    return metrics
