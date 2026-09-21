"""Snapshots and diffs — turning a register's *state* into *events*.

Every register in this project publishes what is true now. None of them
publishes what changed: there is no "licences granted this week" feed, no
webhook, no ``?since=`` parameter. ESMA's ``ae_lastUpdate`` is per record and
only tells you *that* something changed, never what; the EBA regenerates its
golden copy nightly and overwrites it; FINMA's CSV is replaced in place; GISA
ships a new monthly file with no changelog.

So the change data has to be reconstructed, and the only honest way to do that
is to keep the answers and compare them:

    eufinreg --source finma --snapshot snapshots/finma-2026-08-16.jsonl
    # …a week later…
    eufinreg --source finma --snapshot snapshots/finma-2026-08-23.jsonl
    eufinreg --diff snapshots/finma-2026-08-16.jsonl snapshots/finma-2026-08-23.jsonl

The file format is deliberately boring: newline-delimited JSON, one metadata
object on the first line, one row per line after it, rows sorted by key so the
file is byte-stable for the same data. A ``.sha256`` sidecar is written next to
it — the same pattern the EBA uses for its own golden copy, and for the same
reason: a snapshot you cannot verify is not evidence of anything.

Identity is the hard part. A diff is only meaningful if you can say *this* row
is the same entity as *that* row, which needs a key the register itself
guarantees. Sources declare one (``Source.key_columns``); where a register has
no stable identifier — GISA publishes trade licences with no holder and no
licence number — the fallback is a hash of the whole row, which still detects
appearances and disappearances but can never report a modification.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Bumped when the on-disk shape changes in a way older readers cannot handle.
FORMAT_VERSION = 1

#: Columns that describe the fetch rather than the entity, and so must never
#: make a row look "changed".
VOLATILE_COLUMNS = frozenset({"timestamp", "_vintage", "rating", "isHistoryMatch"})


def _key_part(value: Any) -> str:
    """One key column's contribution.

    ``None`` becomes empty rather than the string ``"None"``: several rows with
    a missing identifier must fall back to content hashing, not collapse into
    one entity called None.
    """
    if value is None:
        return ""
    return str(value).strip()


def row_key(row: Mapping[str, Any], key_columns: Sequence[str]) -> str:
    """Stable identity for one row.

    With declared key columns this is their values joined; without them it is a
    digest of the whole row. The fallback is deliberately blunt: a register that
    gives you no identifier gives you no way to tell "this entity changed" from
    "one entity left and another arrived", and pretending otherwise would invent
    change events that never happened.
    """
    if key_columns:
        parts = [_key_part(row.get(name)) for name in key_columns]
        if any(parts):
            # The parts are escaped before joining. Without that, a value
            # containing the separator can impersonate a different entity —
            # ("x\x1fy", "z") and ("x", "y\x1fz") would produce one key, and a
            # change event would be attributed to the wrong company.
            return "\x1f".join(part.replace("\x1f", "\\x1f") for part in parts)
    canonical = json.dumps(
        {k: str(v) for k, v in sorted(row.items()) if k not in VOLATILE_COLUMNS},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass
class Snapshot:
    """One register, as it answered at one moment."""

    source: str
    rows: list[dict[str, Any]]
    key_columns: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def index(self) -> dict[str, dict[str, Any]]:
        """``{key: row}``. Duplicate keys keep the first row and are counted."""
        out: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for row in self.rows:
            key = row_key(row, self.key_columns)
            if key in out:
                duplicates += 1
                continue
            out[key] = dict(row)
        if duplicates:
            self.meta["duplicate_keys"] = duplicates
        return out


def write_snapshot(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    key_columns: Sequence[str] = (),
    meta: Mapping[str, Any] | None = None,
    compress: bool = False,
) -> tuple[Path, str]:
    """Write ``rows`` as a snapshot. Returns ``(path, body_sha256)``.

    Rows are sorted by key and serialised with sorted columns, so two runs over
    unchanged data produce byte-identical files. That is what makes "nothing
    changed" a checkable claim rather than an impression.

    ``compress`` appends ``.gz`` and writes gzip with a fixed mtime, so the
    output stays byte-stable. It matters more than it sounds: the Austrian trade
    licence register is 607 MB of JSONL per snapshot and compresses tenfold, and
    an archive is a thing you keep for years.

    Two digests, deliberately:

    * ``_meta.body_sha256`` covers the **rows only**, so it is the same number
      whether or not the file is compressed and whatever the header says.
      :func:`read_snapshot` checks this one.
    * the ``.sha256`` sidecar covers the **file exactly as written**, so
      ``sha256sum -c`` works without this tool. An archive you can only verify
      with the software that wrote it is not much of an archive.
    """
    target = Path(path)
    if compress and target.suffix != ".gz":
        target = target.with_name(target.name + ".gz")
    target.parent.mkdir(parents=True, exist_ok=True)

    materialised = [dict(row) for row in rows]
    keyed = sorted(((row_key(row, key_columns), row) for row in materialised), key=lambda kv: kv[0])
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for _, row in keyed)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    header = {
        "_meta": {
            "format_version": FORMAT_VERSION,
            "source": source,
            "key_columns": list(key_columns),
            "row_count": len(materialised),
            "body_sha256": digest,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(dict(meta) if meta else {}),
        }
    }
    document = (json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n" + body).encode(
        "utf-8"
    )
    if target.suffix == ".gz":
        buffer = io.BytesIO()
        # mtime=0: gzip stamps the current time into its header by default,
        # which would make every rebuild of identical data a different file.
        with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(document)
        document = buffer.getvalue()

    # Write, flush to disk, then rename. A snapshot half-written by a run that
    # was killed mid-download would otherwise sit in the archive looking like a
    # register that lost 40% of its entries overnight.
    scratch = target.with_name(target.name + ".partial")
    with scratch.open("wb") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(scratch, target)
    target.with_suffix(target.suffix + ".sha256").write_text(
        f"{hashlib.sha256(document).hexdigest()}  {target.name}\n", encoding="utf-8"
    )
    return target, digest


def read_snapshot(path: str | Path) -> Snapshot:
    """Read a snapshot back, verifying its recorded digest.

    Compression is detected from the bytes rather than the file name, so a
    renamed file still reads.
    """
    source_path = Path(path)
    raw = source_path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    lines = raw.decode("utf-8").splitlines()
    if not lines:
        raise ValueError(f"{source_path} is empty")

    first = json.loads(lines[0])
    if not isinstance(first, dict) or "_meta" not in first:
        raise ValueError(
            f"{source_path} does not start with a snapshot header — was it written by --snapshot?"
        )
    meta = dict(first["_meta"])
    rows = [json.loads(line) for line in lines[1:] if line.strip()]

    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    recorded = meta.get("body_sha256")
    if recorded and recorded != digest:
        raise ValueError(
            f"{source_path} fails its own checksum: recorded {recorded[:16]}…, "
            f"computed {digest[:16]}…. The file has been edited or truncated."
        )
    return Snapshot(
        source=str(meta.get("source", "")),
        rows=rows,
        key_columns=tuple(meta.get("key_columns", ())),
        meta=meta,
    )


@dataclass
class Change:
    """One difference between two snapshots."""

    kind: str  # "added" | "removed" | "changed"
    key: str
    columns: tuple[str, ...] = ()
    row: dict[str, Any] = field(default_factory=dict)
    previous: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Render as a flat row, so a diff writes to CSV like anything else."""
        out: dict[str, Any] = {
            "_change": self.kind,
            # The key joins its parts with an ASCII unit separator so that a
            # value containing the display separator cannot forge a collision;
            # that byte is invisible in a terminal, so print something readable.
            "_key": self.key.replace("\x1f", " · "),
            "_changed_columns": " | ".join(self.columns),
        }
        out.update(self.row or self.previous)
        for name in self.columns:
            out[f"{name}__was"] = self.previous.get(name, "")
        return out


def diff_snapshots(
    old: Snapshot,
    new: Snapshot,
    *,
    ignore: Iterable[str] = VOLATILE_COLUMNS,
) -> list[Change]:
    """Compare two snapshots of the same source.

    Column-level, not row-level: a changed row reports *which* columns moved, so
    "renamed" and "lost its authorisation" are not the same event.
    """
    if old.source and new.source and old.source != new.source:
        raise ValueError(
            f"refusing to diff {old.source!r} against {new.source!r} — different registers"
        )
    skip = set(ignore)
    before, after = old.index(), new.index()

    changes: list[Change] = []
    for key, row in after.items():
        if key not in before:
            changes.append(Change(kind="added", key=key, row=row))
            continue
        previous = before[key]
        moved = tuple(
            sorted(
                name
                for name in set(previous) | set(row)
                if name not in skip and str(previous.get(name, "")) != str(row.get(name, ""))
            )
        )
        if moved:
            changes.append(
                Change(kind="changed", key=key, columns=moved, row=row, previous=previous)
            )
    for key, previous in before.items():
        if key not in after:
            changes.append(Change(kind="removed", key=key, previous=previous))

    order = {"added": 0, "changed": 1, "removed": 2}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.key))
    return changes


def summarise(changes: Sequence[Change], *, old: Snapshot, new: Snapshot) -> str:
    """One-paragraph human summary, for stderr and for the API."""
    counts = {
        kind: sum(1 for c in changes if c.kind == kind) for kind in ("added", "changed", "removed")
    }
    note = ""
    if not new.key_columns:
        note = (
            "\n  note: this source declares no stable key, so rows are matched by content — "
            "a modified row shows up as one removal plus one addition."
        )
    duplicates = int(new.meta.get("duplicate_keys", 0) or 0)
    if duplicates:
        # The register issued one identifier to several rows. The diff kept the
        # first of each and ignored the rest, so the counts above understate it.
        note += (
            f"\n  note: {duplicates} row(s) in the newer snapshot share a key with an earlier "
            f"row and were not compared. The register is issuing duplicate identifiers on "
            f"{', '.join(new.key_columns)}."
        )
    return (
        f"{new.source or 'snapshot'}: {counts['added']} added, {counts['changed']} changed, "
        f"{counts['removed']} removed "
        f"({old.meta.get('row_count', len(old.rows))} → "
        f"{new.meta.get('row_count', len(new.rows))} rows, "
        f"{old.meta.get('captured_at', '?')} → {new.meta.get('captured_at', '?')})"
        f"{note}"
    )
