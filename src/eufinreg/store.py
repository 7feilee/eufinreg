"""The snapshot store — the part that makes this a system rather than a script.

A register answers "what is true now". Running that once gives you a CSV;
running it every day and keeping the answers gives you the only copy of the past
that exists, because every register here overwrites its own file. The store is
where those answers live, and its design goals are boring on purpose:

* **Files are the system of record.** Plain, sorted, checksummed JSONL plus the
  untouched response bytes that produced it. Any index or database is
  rebuildable from them; they are not rebuildable from anything.
* **Every artefact can be verified without this tool.** `sha256sum -c` works on
  the sidecars. A store you can only read with the software that wrote it is not
  an archive.
* **A run that fails leaves nothing half-written.** Snapshots are written to a
  temporary file and renamed into place, and concurrent runs are refused by a
  lock rather than allowed to interleave.

Layout::

    store/
      eufinreg-store.json                 descriptor: format version, created
      .lock                               held for the duration of an ingest
      finma/
        snapshots/20260816T040012Z.jsonl
        snapshots/20260816T040012Z.jsonl.sha256
        raw/20260816T040012Z/00001_finma.csv + manifest.jsonl
        changes/20260816T040012Z.jsonl    events against the previous snapshot

Timestamps are UTC and the filename sorts chronologically, which is the whole
reason for that format.
"""

from __future__ import annotations

import calendar
import gzip
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .snapshot import Change, Snapshot, read_snapshot, write_snapshot

#: Bumped when the on-disk layout changes in a way an older reader cannot handle.
STORE_FORMAT = 1

DESCRIPTOR = "eufinreg-store.json"
LOCK = ".lock"

#: A lock older than this is assumed to belong to a run that died. Ingests are
#: long (the EBA download alone is 19 MB) but not this long.
STALE_LOCK_SECONDS = 6 * 3600


def utc_stamp(now: float | None = None) -> str:
    """``20260816T040012Z`` — sortable, filesystem-safe, unambiguous."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def parse_stamp(stamp: str) -> float | None:
    """``'20260816T040012Z'`` → a POSIX timestamp, or ``None``.

    ``calendar.timegm`` rather than ``time.mktime``: the stamp is UTC, and
    mktime would read it as local time and be wrong by the offset — silently,
    and differently either side of a DST change.
    """
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")))
    except ValueError:
        return None


class StoreLocked(RuntimeError):
    """Another ingest holds the lock. Not an error worth retrying in a loop."""


@dataclass(frozen=True)
class SnapshotRef:
    """One snapshot on disk, described by its header alone.

    Listing a store must not mean parsing every row of every snapshot, so only
    the first line of each file is read.
    """

    source: str
    path: Path
    stamp: str
    captured_at: str = ""
    row_count: int = 0
    digest: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def raw_dir(self) -> Path:
        return self.path.parent.parent / "raw" / self.stamp

    @property
    def sidecar(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".sha256")

    def load(self) -> Snapshot:
        """Read the whole snapshot, verifying its digest."""
        return read_snapshot(self.path)

    def age_hours(self, now: float | None = None) -> float | None:
        when = parse_stamp(self.stamp)
        if when is None:
            return None
        return max(0.0, ((now if now is not None else time.time()) - when) / 3600.0)


@dataclass
class Store:
    """A directory of dated register snapshots."""

    root: Path
    _lock_fd: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # -- lifecycle ---------------------------------------------------------

    def init(self) -> Path:
        """Create the store if it is not there. Idempotent."""
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = self.root / DESCRIPTOR
        if not descriptor.exists():
            descriptor.write_text(
                json.dumps(
                    {
                        "format": STORE_FORMAT,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return self.root

    def check_format(self) -> None:
        descriptor = self.root / DESCRIPTOR
        if not descriptor.exists():
            return
        found = json.loads(descriptor.read_text(encoding="utf-8")).get("format")
        if isinstance(found, int) and found > STORE_FORMAT:
            raise ValueError(
                f"{self.root} was written by a newer eufinreg (store format {found}, "
                f"this build understands {STORE_FORMAT}). Upgrade rather than risk "
                f"writing a mixed store."
            )

    # -- locking -----------------------------------------------------------

    def acquire_lock(self, *, now: float | None = None) -> None:
        """Refuse to run two ingests over one store.

        Cron overlaps are the normal way this happens: a slow run is still
        downloading when the next one starts, and both write snapshots for the
        same day. A lock file with a PID and a timestamp is enough, and a stale
        one is reclaimed rather than requiring manual cleanup.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / LOCK
        if path.exists():
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held = {}
            started = float(held.get("started_at", 0) or 0)
            age = (now if now is not None else time.time()) - started
            if age < STALE_LOCK_SECONDS:
                raise StoreLocked(
                    f"{path} is held by pid {held.get('pid', '?')} since "
                    f"{held.get('started_iso', '?')}. If that run is gone, delete the file."
                )
            path.unlink(missing_ok=True)
        self._lock_fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(
            self._lock_fd,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": now if now is not None else time.time(),
                    "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                }
            ).encode("utf-8"),
        )

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        (self.root / LOCK).unlink(missing_ok=True)

    def __enter__(self) -> Store:
        self.init()
        self.check_format()
        self.acquire_lock()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release_lock()

    # -- paths -------------------------------------------------------------

    def source_dir(self, source: str) -> Path:
        return self.root / source

    def snapshots_dir(self, source: str) -> Path:
        return self.source_dir(source) / "snapshots"

    def raw_dir(self, source: str, stamp: str) -> Path:
        return self.source_dir(source) / "raw" / stamp

    def changes_dir(self, source: str) -> Path:
        return self.source_dir(source) / "changes"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def sources(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and entry.name != "runs"
            and (entry / "snapshots").is_dir()
        )

    # -- reading -----------------------------------------------------------

    def snapshots(self, source: str) -> list[SnapshotRef]:
        """Every snapshot of ``source``, oldest first. Reads headers only."""
        directory = self.snapshots_dir(source)
        if not directory.is_dir():
            return []
        refs: list[SnapshotRef] = []
        # Both, because a store can hold snapshots written before compression
        # was enabled and after. The stamp is the name minus every suffix.
        paths = [*directory.glob("*.jsonl"), *directory.glob("*.jsonl.gz")]
        for path in sorted(paths, key=lambda p: _stamp_of(p)):
            meta = _read_header(path)
            refs.append(
                SnapshotRef(
                    source=source,
                    path=path,
                    stamp=_stamp_of(path),
                    captured_at=str(meta.get("captured_at", "")),
                    row_count=int(meta.get("row_count", 0) or 0),
                    digest=str(meta.get("body_sha256", "")),
                    meta=meta,
                )
            )
        return refs

    def latest(self, source: str) -> SnapshotRef | None:
        refs = self.snapshots(source)
        return refs[-1] if refs else None

    def previous(self, source: str) -> SnapshotRef | None:
        refs = self.snapshots(source)
        return refs[-2] if len(refs) > 1 else None

    # -- writing -----------------------------------------------------------

    def add_snapshot(
        self,
        source: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        key_columns: Sequence[str] = (),
        meta: Mapping[str, Any] | None = None,
        stamp: str | None = None,
        compress: bool = False,
    ) -> SnapshotRef:
        """Write one snapshot into the store and return its reference."""
        stamp = stamp or utc_stamp()
        directory = self.snapshots_dir(source)
        directory.mkdir(parents=True, exist_ok=True)
        existing = [
            p for p in (directory / f"{stamp}.jsonl", directory / f"{stamp}.jsonl.gz") if p.exists()
        ]
        if existing:
            raise FileExistsError(
                f"{existing[0]} already exists — two runs in the same second, or a stamp "
                f"reused. Refusing to overwrite an archived answer."
            )
        path, digest = write_snapshot(
            directory / f"{stamp}.jsonl",
            rows,
            source=source,
            key_columns=key_columns,
            meta=meta,
            compress=compress,
        )
        header = _read_header(path)
        return SnapshotRef(
            source=source,
            path=path,
            stamp=stamp,
            captured_at=str(header.get("captured_at", "")),
            row_count=int(header.get("row_count", 0) or 0),
            digest=digest,
            meta=header,
        )

    def write_changes(
        self,
        source: str,
        stamp: str,
        changes: Sequence[Change],
        *,
        against: str = "",
    ) -> Path:
        """Record the events between the previous snapshot and this one.

        Written even when there are none: "we looked and nothing moved" is a
        fact worth being able to prove later, and an absent file cannot
        distinguish that from a run that never happened.
        """
        directory = self.changes_dir(source)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stamp}.jsonl"
        counts = {kind: sum(1 for c in changes if c.kind == kind) for kind in ORDERED_KINDS}
        header = {
            "_meta": {
                "source": source,
                "snapshot": stamp,
                "against": against,
                "counts": counts,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        }
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
            for change in changes:
                handle.write(json.dumps(change.as_row(), ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def read_changes(self, source: str, *, since: str = "") -> list[dict[str, Any]]:
        """Every recorded event for ``source``, oldest first.

        ``since`` is a stamp prefix (``20260816``), so a date is a valid filter
        without any date parsing on the caller's side.
        """
        directory = self.changes_dir(source)
        if not directory.is_dir():
            return []
        events: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.jsonl")):
            if since and path.stem < since:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines[1:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                row["_snapshot"] = path.stem
                row["_source"] = source
                events.append(row)
        return events

    # -- run history -------------------------------------------------------

    def write_run(self, record: Mapping[str, Any], *, run_id: str = "") -> Path:
        """Keep the report of one ingest, next to the data it produced.

        The archive answers "what did the register say"; this answers "what did
        we do, and how did it go" — which is the other half of any question that
        starts with "why does Tuesday look wrong". Kept as one file per run
        because that is greppable, appendable and impossible to corrupt halfway.
        """
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        identifier = run_id or str(record.get("run_id") or utc_stamp())
        path = self.runs_dir / f"{_safe_name(identifier)}.json"
        payload = dict(record)
        payload.setdefault("run_id", identifier)
        payload.setdefault("written_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def runs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Past runs, newest last. Run ids sort chronologically by construction."""
        if not self.runs_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A truncated run record must not hide the rest of the history.
                records.append({"run_id": path.stem, "unreadable": True})
                continue
            if isinstance(payload, dict):
                payload.setdefault("run_id", path.stem)
                records.append(payload)
        return records[-limit:] if limit else records

    def run(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs_dir / f"{_safe_name(run_id)}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def prune_runs(self, keep: int = 500) -> list[Path]:
        """Run records are small, but not infinite. Oldest go first."""
        if not self.runs_dir.is_dir():
            return []
        paths = sorted(self.runs_dir.glob("*.json"))
        doomed = paths[: max(0, len(paths) - keep)]
        for path in doomed:
            path.unlink(missing_ok=True)
        return doomed

    # -- maintenance -------------------------------------------------------

    def verify(self, source: str | None = None) -> list[tuple[SnapshotRef, bool, str]]:
        """Recompute every snapshot's digest. The archive's own audit."""
        results: list[tuple[SnapshotRef, bool, str]] = []
        for key in [source] if source else self.sources():
            for ref in self.snapshots(key):
                try:
                    ref.load()
                except (OSError, ValueError) as exc:
                    results.append((ref, False, str(exc)))
                else:
                    results.append((ref, True, "ok"))
        return results

    def prune(
        self,
        source: str,
        *,
        keep_days: int | None = None,
        keep_last: int = 2,
        now: float | None = None,
        dry_run: bool = False,
    ) -> list[Path]:
        """Delete snapshots older than ``keep_days``, keeping the newest ones.

        Two guards, because deleting an archive is not undoable: ``keep_last``
        is never violated whatever the age rule says, and the raw bodies are
        removed with their snapshot rather than orphaned.
        """
        refs = self.snapshots(source)
        if keep_days is not None and keep_days < 0:
            raise ValueError(
                f"prune needs a positive window; {keep_days} would put the cutoff in the "
                f"future and delete everything outside keep_last"
            )
        keep_last = max(1, keep_last)
        if keep_days is None or len(refs) <= keep_last:
            return []
        cutoff = (now if now is not None else time.time()) - keep_days * 86400
        removed: list[Path] = []
        candidates = refs[: max(0, len(refs) - keep_last)]
        for ref in candidates:
            when = parse_stamp(ref.stamp)
            if when is None or when >= cutoff:
                continue
            removed.append(ref.path)
            if dry_run:
                continue
            ref.path.unlink(missing_ok=True)
            ref.sidecar.unlink(missing_ok=True)
            _remove_tree(ref.raw_dir)
        return removed

    # -- reporting ---------------------------------------------------------

    def status(
        self, cadence: Mapping[str, float | None] | None = None, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Per-source freshness, for a health endpoint or a morning check.

        ``stale`` compares the newest snapshot's age against the *register's*
        cadence, not against a polling schedule: a monthly file that is 30 hours
        old is fine, a nightly one that is 30 hours old is not.
        """
        cadence = cadence or {}
        out: list[dict[str, Any]] = []
        for source in self.sources():
            refs = self.snapshots(source)
            latest = refs[-1] if refs else None
            age = latest.age_hours(now) if latest else None
            expected = cadence.get(source)
            # One cadence of slack: a daily file fetched at 04:00 is not late at
            # 04:00 the next day, and alerting as if it were trains people to
            # ignore the alert.
            stale = bool(age is not None and expected and age > expected * 2)
            out.append(
                {
                    "source": source,
                    "snapshots": len(refs),
                    "latest": latest.stamp if latest else "",
                    "captured_at": latest.captured_at if latest else "",
                    "rows": latest.row_count if latest else 0,
                    "age_hours": round(age, 1) if age is not None else None,
                    "cadence_hours": expected,
                    "stale": stale,
                }
            )
        return out


ORDERED_KINDS = ("added", "changed", "removed")


def _safe_name(value: str) -> str:
    """A run id lands in a filename; keep it to characters that cannot escape."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(value))
    return cleaned.strip("-.")[:80] or "run"


def _stamp_of(path: Path) -> str:
    """``20260816T041200Z.jsonl.gz`` → ``20260816T041200Z``."""
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _read_header(path: Path) -> dict[str, Any]:
    """The ``_meta`` object from a snapshot's first line, or ``{}``.

    Reads only as far as the first newline for a plain file. A compressed one
    has to be opened through gzip, which still stops at the first line rather
    than inflating 600 MB to answer "how many rows".
    """
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                first = handle.readline()
        else:
            with path.open("r", encoding="utf-8") as handle:
                first = handle.readline()
    except OSError:
        return {}
    try:
        payload = json.loads(first)
    except ValueError:
        return {}
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    return dict(meta) if isinstance(meta, dict) else {}


def _remove_tree(path: Path) -> None:
    if not path.is_dir():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink(missing_ok=True)
    path.rmdir()
