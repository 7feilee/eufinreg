"""Field discovery (``--inspect``) and value discovery (``--list-values``).

Both exist for the same reason: the schema of a public register is whatever the
register happens to be serving today. Documented field lists go stale, national
authorities add columns, and a wrong guess produces zero results with no error.
So rather than trusting this project's README, ask the live data.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .flatten import scalar


@dataclass
class FieldProfile:
    name: str
    present: int
    non_empty: int
    distinct: int
    samples: list[str]

    def coverage(self, total: int) -> float:
        return (self.present / total * 100) if total else 0.0


def profile_records(
    records: Sequence[Mapping[str, Any]],
    *,
    max_samples: int = 3,
    sample_width: int = 60,
) -> tuple[int, list[FieldProfile]]:
    """Return ``(record_count, profiles)`` sorted by how often a field appears.

    ``present`` counts records where the key exists at all; ``non_empty`` counts
    those where it also has a value. The gap between them matters: a field that
    is always present but always blank is a field the register is not populating.
    """
    total = len(records)
    present: Counter[str] = Counter()
    non_empty: Counter[str] = Counter()
    values: dict[str, OrderedDict[str, None]] = {}
    distinct: dict[str, set[str]] = {}

    for record in records:
        for name, raw in record.items():
            present[name] += 1
            text = scalar(raw)
            if text == "":
                continue
            non_empty[name] += 1
            distinct.setdefault(name, set()).add(text)
            bucket = values.setdefault(name, OrderedDict())
            if len(bucket) < max_samples:
                bucket.setdefault(_truncate(text, sample_width), None)

    profiles = [
        FieldProfile(
            name=name,
            present=count,
            non_empty=non_empty.get(name, 0),
            distinct=len(distinct.get(name, ())),
            samples=list(values.get(name, ())),
        )
        for name, count in present.items()
    ]
    profiles.sort(key=lambda p: (-p.present, p.name))
    return total, profiles


def render_profile(total: int, profiles: Sequence[FieldProfile], *, title: str) -> str:
    """Human-readable table. Plain text on purpose — greppable, pipeable."""
    if total == 0:
        return f"{title}\n  (no records sampled — the filter matched nothing)\n"

    name_w = max(len("field"), *(len(p.name) for p in profiles))
    lines = [
        title,
        f"  sampled {total} record(s)",
        "",
        f"  {'field'.ljust(name_w)}  {'present':>7}  {'filled':>6}  {'distinct':>8}  samples",
        f"  {'-' * name_w}  {'-' * 7}  {'-' * 6}  {'-' * 8}  {'-' * 40}",
    ]
    for p in profiles:
        pct = f"{p.coverage(total):.0f}%"
        samples = " ; ".join(p.samples) if p.samples else "(always empty)"
        lines.append(
            f"  {p.name.ljust(name_w)}  {pct:>7}  {p.non_empty:>6}  {p.distinct:>8}  {samples}"
        )
    lines.append("")
    return "\n".join(lines)


def count_values(
    records: Iterable[Mapping[str, Any]],
    field: str,
    *,
    split: str | None = None,
) -> list[tuple[str, int]]:
    """Count distinct values of ``field``.

    ``split`` splits multi-valued cells (the MiCA CSVs pipe-pack several
    authorisations into one cell) so that the counts are per authorisation
    rather than per unique combination.
    """
    counter: Counter[str] = Counter()
    for record in records:
        text = scalar(record.get(field, ""))
        if text == "":
            continue
        parts = [p.strip() for p in text.split(split)] if split else [text.strip()]
        for part in parts:
            if part:
                counter[part] += 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def render_values(
    field: str,
    pairs: Sequence[tuple[str, int]],
    *,
    source: str,
    note: str | None = None,
) -> str:
    if not pairs:
        return (
            f"{source} — distinct values of {field!r}\n"
            f"  (none found; is {field!r} a real field? run --inspect to list the real ones)\n"
        )
    width = max(len(str(count)) for _, count in pairs)
    lines = [f"{source} — distinct values of {field!r} ({len(pairs)} found)"]
    if note:
        lines.append(f"  note: {note}")
    lines.append("")
    lines.extend(f"  {str(count).rjust(width)}  {value}" for value, count in pairs)
    lines.append("")
    return "\n".join(lines)


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
