"""Evidence bundles — what a register said, when, and the bytes it said it in.

A compliance file does not need a CSV. It needs to answer, in two years, three
questions that a CSV cannot: *what was checked*, *when*, and *how do I know this
has not been edited since*. Every ingredient already exists in this project —
raw response bodies with a manifest, canonical snapshots with a SHA-256 — so a
bundle is mostly a matter of putting them in one file and refusing to leave
anything out.

A bundle is a plain ZIP containing::

    receipt.json      what was asked, of whom, when, and what came back
    rows.json         the matching row(s), verbatim from the snapshot
    rows.csv          the same, for humans
    raw/…             the untouched response bytes + the fetch manifest
    MANIFEST.json     every member with its SHA-256
    README.txt        what this is, and — importantly — what it is not

Two deliberate properties:

* **Reproducible.** Member timestamps come from the snapshot, not from the
  clock, so building the same bundle from the same snapshot twice produces
  byte-identical output with the same digest. A digest that changes every time
  you rebuild proves nothing.
* **The publisher's own disclaimer travels with it.** Every one of these
  registers says, in its own words, that it does not guarantee its contents. A
  receipt that quietly drops that sentence misrepresents the source, so
  ``receipt.json`` carries it and ``README.txt`` repeats it.

What this is *not*: a signature, a trusted timestamp, or a legal attestation.
It proves internal consistency and nothing more — anyone holding the bundle
could have built it. Cryptographic signing and an RFC 3161 timestamp are the
obvious next step and are deliberately not faked here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .flatten import column_order, scalar
from .store import SnapshotRef, parse_stamp

BUNDLE_FORMAT = 1

README = """\
eufinreg evidence bundle
========================

This bundle records what a public register answered at one moment, and the
untouched bytes it answered with.

  receipt.json   what was asked, of whom, when; the register's own disclaimer
  rows.json      the matching record(s), exactly as they appear in the snapshot
  rows.csv       the same rows, flattened for reading
  raw/           the response bodies as received, plus manifest.jsonl
  MANIFEST.json  SHA-256 of every file above

To check the bundle has not been altered:

  python3 -c "import hashlib,json,zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); \\
    m=json.loads(z.read('MANIFEST.json')); \\
    print(all(hashlib.sha256(z.read(f['name'])).hexdigest()==f['sha256'] \\
              for f in m['files']))" BUNDLE.zip

WHAT THIS IS NOT
----------------
It is not signed and not timestamped by a third party. It demonstrates internal
consistency — the rows match the snapshot, the snapshot matches its checksum —
not that anyone other than the holder produced it.

It is also not a statement of fact about the entity. It is a copy of what a
register published. Every publisher here disclaims responsibility for the
accuracy of its own contents; the wording is in receipt.json, and the register's
live interface is the authority if a decision depends on the answer.
"""


@dataclass
class Bundle:
    path: Path
    digest: str
    files: list[dict[str, Any]]
    receipt: dict[str, Any]


def build_bundle(
    out_path: str | Path,
    *,
    source: Any,
    ref: SnapshotRef,
    rows: Sequence[Mapping[str, Any]],
    subject: Mapping[str, Any] | None = None,
    include_raw: bool = True,
    query: Mapping[str, Any] | None = None,
) -> Bundle:
    """Write an evidence bundle for ``rows`` taken from snapshot ``ref``."""
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    receipt = {
        "bundle_format": BUNDLE_FORMAT,
        "tool": {"name": "eufinreg", "version": __version__},
        "subject": dict(subject or {}),
        "register": {
            "source": getattr(source, "key", ""),
            "title": getattr(source, "title", ""),
            "jurisdiction": getattr(source, "jurisdiction", ""),
            "documentation": getattr(source, "docs_url", ""),
            "data_licence": getattr(source, "data_licence", "") or "not stated by the publisher",
            "publisher_disclaimer": getattr(source, "disclaimer", "")
            or "The publisher states no disclaimer in this interface; its own terms apply.",
            "personal_data_note": getattr(source, "personal_data", ""),
            "documented_cadence_hours": getattr(source, "cadence_hours", None),
        },
        "snapshot": {
            "stamp": ref.stamp,
            "captured_at": ref.captured_at,
            "row_count_total": ref.row_count,
            "body_sha256": ref.digest,
            "key_columns": list(ref.meta.get("key_columns", ())),
            "selection": {
                "select": ref.meta.get("select", ""),
                "query": ref.meta.get("query", ""),
                "user_agent": ref.meta.get("user_agent", ""),
            },
        },
        "result": {
            "rows_returned": len(rows),
            "query": dict(query or {}),
        },
        "assurance": {
            "signed": False,
            "trusted_timestamp": False,
            "note": (
                "Internal consistency only: the rows are reproduced from a snapshot whose "
                "checksum is recorded above. Not a signature and not third-party evidence "
                "of time."
            ),
        },
    }

    raw_files: list[tuple[str, bytes]] = []
    if include_raw and ref.raw_dir.is_dir():
        for path in sorted(ref.raw_dir.rglob("*")):
            if path.is_file():
                raw_files.append(
                    (f"raw/{path.relative_to(ref.raw_dir).as_posix()}", path.read_bytes())
                )
    # A bundle that quietly omits the response bytes looks exactly like one that
    # never had them. Say which, in the receipt, every time.
    receipt["raw_response"] = {
        "included": bool(raw_files),
        "files": len(raw_files),
        "reason": (
            ""
            if raw_files
            else (
                "omitted by request"
                if not include_raw
                else "the snapshot was taken without raw capture, so the response bytes "
                "were never archived"
            )
        ),
    }

    columns = column_order(rows) if rows else []
    members: list[tuple[str, bytes]] = [
        ("receipt.json", _json_bytes(receipt)),
        ("rows.json", _json_bytes(list(rows))),
        ("rows.csv", _csv_bytes(rows, columns)),
        ("README.txt", README.encode("utf-8")),
        *raw_files,
    ]

    unsafe = [name for name, _ in members if unsafe_member(name)]
    if unsafe:
        raise ValueError(
            f"refusing to build a bundle with member name(s) that would escape on "
            f"extraction: {', '.join(unsafe[:3])}"
        )
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "files": [
            {"name": name, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            for name, blob in members
        ],
    }
    manifest_bytes = _json_bytes(manifest)
    digest = hashlib.sha256(manifest_bytes).hexdigest()

    # Fix the member timestamps to the snapshot's, so the same inputs always
    # produce the same bytes. Reproducibility is the point of the exercise.
    when = parse_stamp(ref.stamp) or time.time()
    date_time = time.gmtime(when)[:6]

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, blob in (*members, ("MANIFEST.json", manifest_bytes)):
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, blob)

    target.with_suffix(target.suffix + ".sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    return Bundle(path=target, digest=digest, files=manifest["files"], receipt=receipt)


def unsafe_member(name: str) -> bool:
    """True for a ZIP entry name that would escape the directory it unpacks into.

    A bundle is meant to be opened by somebody else. A member called
    ``../../.ssh/authorized_keys`` verifies perfectly well against its own
    digest and is still an attack, so the name is checked as well as the bytes.
    """
    if not name or name.startswith(("/", "\\")) or ":" in name.split("/")[0][1:2]:
        return True
    parts = name.replace("\\", "/").split("/")
    return any(part in ("..", "") for part in parts[:-1]) or ".." in parts


def verify_bundle(path: str | Path) -> tuple[bool, list[str]]:
    """Recompute every member digest, and check the names are safe to extract."""
    problems: list[str] = []
    with zipfile.ZipFile(Path(path)) as archive:
        names = set(archive.namelist())
        if "MANIFEST.json" not in names:
            return False, ["MANIFEST.json is missing — this is not an eufinreg bundle"]
        manifest = json.loads(archive.read("MANIFEST.json"))
        listed = {entry["name"] for entry in manifest.get("files", [])}
        for entry in manifest.get("files", []):
            name = entry["name"]
            if unsafe_member(name):
                problems.append(f"{name}: member name would escape the extraction directory")
                continue
            if name not in names:
                problems.append(f"{name}: listed in the manifest but missing from the bundle")
                continue
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            if actual != entry["sha256"]:
                problems.append(f"{name}: sha256 {actual[:16]}… does not match the manifest")
        for extra in sorted(names - listed - {"MANIFEST.json"}):
            problems.append(f"{extra}: present in the bundle but not in the manifest")
    return not problems, problems


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _csv_bytes(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: scalar(row.get(name, "")) for name in columns})
    return buffer.getvalue().encode("utf-8")
