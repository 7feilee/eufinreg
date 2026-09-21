"""Turn a flat array of mixed parent/child documents into one row per entity.

ESMA's Solr cores return parents and children **in the same flat ``docs``
array**, distinguished only by marker fields::

    {"id": "ae42",          "type_s": "parent", "entity_type": "ae",
     "_root_": "ae42",      "ae_entityName": "Catam Asset Management AG", ...}
    {"id": "aeActivity147", "type_s": "child",  "entity_type": "aeActivity",
     "_root_": "ae42",      "ac_serviceName": "Reception and transmission of orders", ...}

Callers almost always want "one licensed entity per row", with the child records
collapsed into pipe-joined summary columns. That is what :func:`flatten` does.

Design notes, because this is the part that is easy to get wrong:

* Grouping is by ``_root_``, **not** by ``id`` — a parent's ``_root_`` points at
  itself, which is what makes the single-pass grouping work.
* Order is preserved: entities come out in the order their first document was
  seen, and each collapsed column preserves the order of its children.
* Children are bucketed by ``entity_type`` so that activities and activity
  history do not get mixed into the same column.
* Orphan children (children whose parent is not in the response — common when
  you page through with a filter that excludes parents) are **not dropped**.
  They produce a row flagged with ``_orphan = True`` so the count still adds up.
* Nothing is renamed and no field list is hardcoded. If ESMA adds a field it
  simply shows up as a new column.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Separator used when collapsing several child values into one cell.
DEFAULT_SEPARATOR = " | "

#: Solr bookkeeping fields that carry no register information.
INTERNAL_FIELDS = frozenset({"_version_", "collectorParent"})


def scalar(value: Any, *, separator: str = DEFAULT_SEPARATOR) -> str:
    """Render any JSON value as a single CSV-safe cell.

    Solr returns multi-valued fields as lists and, very occasionally, nested
    objects. Rather than guess, lists become separator-joined strings and dicts
    become compact JSON so that no information is silently lost.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # A register that sends an undecodable field would otherwise put
        # b'\xff\xfe…' through the CSV writer verbatim. Decode what can be
        # decoded and keep the rest visible rather than crashing at write time.
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return separator.join(scalar(v, separator=separator) for v in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


@dataclass
class FlattenConfig:
    """How to recognise parents, children and their grouping key."""

    root_field: str = "_root_"
    id_field: str = "id"
    parent_marker_field: str = "type_s"
    parent_marker_value: str = "parent"
    child_group_field: str = "entity_type"
    separator: str = DEFAULT_SEPARATOR
    #: Also emit a ``<group>_count`` column per child bucket.
    emit_counts: bool = True
    drop_fields: frozenset[str] = field(default_factory=lambda: INTERNAL_FIELDS)

    def is_parent(self, doc: Mapping[str, Any]) -> bool:
        marker = doc.get(self.parent_marker_field)
        return marker == self.parent_marker_value

    def group_of(self, doc: Mapping[str, Any]) -> str:
        value = doc.get(self.child_group_field)
        return str(value) if value not in (None, "") else "child"

    def key_of(self, doc: Mapping[str, Any]) -> str:
        """Grouping key. Falls back to ``id`` so a doc without ``_root_`` still
        produces its own row instead of collapsing everything into one."""
        for candidate in (self.root_field, self.id_field):
            value = doc.get(candidate)
            if value not in (None, ""):
                return str(value)
        return ""


def flatten(
    docs: Iterable[Mapping[str, Any]],
    config: FlattenConfig | None = None,
) -> list[dict[str, str]]:
    """Collapse a mixed parent/child document stream into one row per entity."""
    cfg = config or FlattenConfig()

    parents: dict[str, dict[str, Any]] = {}
    children: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    order: list[str] = []

    for doc in docs:
        key = cfg.key_of(doc)
        if key not in children:
            children[key] = {}
            order.append(key)
        if cfg.is_parent(doc):
            if key in parents:
                # Two parents sharing a root should not happen; keep the first
                # and fold the second in as a child bucket so nothing is lost.
                children[key].setdefault("duplicateParent", []).append(doc)
            else:
                parents[key] = dict(doc)
        else:
            children[key].setdefault(cfg.group_of(doc), []).append(doc)

    rows: list[dict[str, str]] = []
    for key in order:
        parent = parents.get(key)
        row: dict[str, str] = {}
        if parent is None:
            row["_orphan"] = "true"
            row[cfg.root_field] = key
        else:
            for name, value in parent.items():
                if name in cfg.drop_fields:
                    continue
                row[name] = scalar(value, separator=cfg.separator)
        for group, docs_in_group in children[key].items():
            if cfg.emit_counts:
                row[f"{group}_count"] = str(len(docs_in_group))
            for name in _child_field_order(docs_in_group):
                if name in cfg.drop_fields or name in (cfg.root_field, cfg.child_group_field):
                    continue
                joined = cfg.separator.join(
                    scalar(child.get(name), separator=cfg.separator) for child in docs_in_group
                )
                row[f"{group}_{name}"] = joined
        rows.append(row)
    return rows


def passthrough(
    docs: Iterable[Mapping[str, Any]],
    *,
    separator: str = DEFAULT_SEPARATOR,
    drop_fields: Iterable[str] = INTERNAL_FIELDS,
) -> list[dict[str, str]]:
    """Render already-flat records as strings, without any grouping."""
    dropped = frozenset(drop_fields)
    return [
        {
            name: scalar(value, separator=separator)
            for name, value in doc.items()
            if name not in dropped
        }
        for doc in docs
    ]


def flatten_auto(
    docs: Iterable[Mapping[str, Any]],
    config: FlattenConfig | None = None,
    *,
    block_structured: bool | None = None,
) -> list[dict[str, str]]:
    """Flatten if the data is block-structured, otherwise pass it through.

    ``block_structured=None`` autodetects by looking for at least one parent
    document. That keeps the tool usable against Solr cores nobody has profiled
    yet: a flat core is emitted as-is instead of being mangled into orphan rows.
    """
    cfg = config or FlattenConfig()
    materialised = list(docs)
    if block_structured is None:
        block_structured = any(cfg.is_parent(doc) for doc in materialised)
    if not block_structured:
        return passthrough(materialised, separator=cfg.separator, drop_fields=cfg.drop_fields)
    return flatten(materialised, cfg)


def _child_field_order(docs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Union of the child field names, first-seen order (schema-drift safe)."""
    seen: dict[str, None] = {}
    for doc in docs:
        for name in doc:
            seen.setdefault(name, None)
    return list(seen)


def column_order(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Stable union of every column across rows, in first-seen order.

    Rows are ragged by design (different entity types carry different child
    buckets), so the CSV writer needs the union rather than the first row's keys.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for name in row:
            seen.setdefault(name, None)
    return list(seen)


def iter_rows_as_strings(
    rows: Iterable[Mapping[str, Any]], columns: Sequence[str]
) -> Iterator[dict[str, str]]:
    """Fill in missing columns with '' so every row has the same shape."""
    for row in rows:
        yield {name: scalar(row.get(name, "")) for name in columns}
