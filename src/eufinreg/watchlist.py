"""Watchlists — the thing that turns eighteen register clients into a product.

Nobody's job is "read a list of 2,938 Swiss authorisation holders". People's
jobs are "these 40 counterparties are on our books; tell me when one of them
stops being licensed". A watchlist is that list, and everything downstream —
change scoping, alerting, evidence — hangs off it.

Matching is the honest part. Two kinds, never blurred:

``identifier``
    The row and the watched entity share a value in a column the register
    guarantees: a Swiss UID, an LEI, an EBA ``EntityCode``, a EUDAMED SRN.
    Punctuation and case are normalised away, so ``CHE-101.329.561`` in your
    list matches ``CHE101329561`` in a register that prints it differently.
``name``
    No shared identifier exists, and the *normalised* names are equal —
    casefolded, de-accented, punctuation removed, legal-form suffix dropped.
    This is a **weaker** claim and is labelled as one on every row it produces.

There is deliberately no fuzzy matching. A product that silently decides
``Vontobel Holding AG`` and ``Bank Vontobel AG`` are the same company produces
confident nonsense, and in this domain confident nonsense is worse than a gap.
Unmatched entities are reported instead — see :meth:`Watchlist.coverage`.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Legal-form tokens stripped from the *end* of a name before comparing.
#: Only suffixes, and only whole tokens: "Holding" stays, because "X Holding AG"
#: and "X AG" are different legal entities and merging them would be a bug with
#: legal consequences.
LEGAL_FORMS = frozenset(
    {
        # German-speaking, which is where this tool is pointed
        "ag",
        "gmbh",
        "mbh",
        "ug",
        "kg",
        "ohg",
        "gbr",
        "kgaa",
        "eg",
        "reg",
        "genossenschaft",
        "se",
        # French / Italian / Spanish
        "sa",
        "sagl",
        "sarl",
        "sas",
        "sprl",
        "scs",
        "sca",
        "snc",
        "eurl",
        "gie",
        "srl",
        "spa",
        # English-speaking
        "plc",
        "ltd",
        "limited",
        "inc",
        "llc",
        "lp",
        "llp",
        # Benelux, Nordic, Central and Eastern European
        "nv",
        "bv",
        "cv",
        "oy",
        "oyj",
        "ab",
        "as",
        "asa",
        "aps",
        "kb",
        "hb",
        "ans",
        "ba",
        "coop",
        "kft",
        "sro",
        "zoo",
        "doo",
        "dd",
        "ood",
        "ead",
        "ky",
        "ei",
    }
)

#: An identifier must be at least this long before it is trusted as a match key,
#: so that a stray "1" or "CH" in a column cannot join two unrelated entities.
MIN_IDENTIFIER_LENGTH = 6
#: Digits-only comparison is looser still (it lets CHE-101.329.561 match a bare
#: 101329561) and so demands more of them.
MIN_DIGITS_LENGTH = 8

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalise_identifier(value: str) -> set[str]:
    """Every form of an identifier that should be considered the same value.

    ``'CHE-101.329.561'`` → ``{'CHE101329561', '101329561'}``. The digits-only
    variant is what lets a list written from a FINMA export match a register
    that stores the number bare, and it is length-gated so that short codes do
    not collide.
    """
    text = (value or "").strip()
    if not text:
        return set()
    variants: set[str] = set()
    alnum = "".join(ch for ch in text.upper() if ch.isalnum())
    if len(alnum) >= MIN_IDENTIFIER_LENGTH:
        variants.add(alnum)
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= MIN_DIGITS_LENGTH:
        variants.add(digits)
    return variants


def normalise_name(value: str) -> str:
    """Casefold, de-accent, drop punctuation and a trailing legal form.

    ``'Bank Vontobel AG'`` → ``'bank vontobel'``, and ``'Alpha S.p.A.'`` →
    ``'alpha'``, because dropping punctuation first turns a dotted legal form
    into single-letter tokens that have to be rejoined before they can be
    recognised. Returns ``''`` for anything that normalises to nothing, which
    never matches.

    The rejoining is why a name genuinely ending in initials (``'Studio A B'``)
    loses them. That is a deliberate trade: it applies to both sides of every
    comparison, and a false *negative* here is reported as an uncovered entity
    rather than mis-attributed to somebody else.
    """
    text = unicodedata.normalize("NFKD", (value or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text).casefold()
    tokens = _SPACE.sub(" ", text).strip().split()

    while tokens:
        if tokens[-1] in LEGAL_FORMS:
            tokens.pop()
            continue
        joined = _join_trailing_initials(tokens)
        if joined is None:
            break
        del tokens[-joined:]
    return " ".join(tokens)


def _join_trailing_initials(tokens: Sequence[str]) -> int | None:
    """How many trailing single-letter tokens spell a legal form, if any."""
    for count in (4, 3, 2):
        tail = tokens[-count:]
        if len(tail) == count and all(len(t) == 1 for t in tail) and "".join(tail) in LEGAL_FORMS:
            return count
    return None


@dataclass(frozen=True)
class Match:
    """Why a row is considered to be about a watched entity."""

    entity_id: str
    entity_label: str
    kind: str  # "identifier" | "name"
    column: str
    value: str

    @property
    def confident(self) -> bool:
        return self.kind == "identifier"


@dataclass
class WatchedEntity:
    """One thing somebody cares about."""

    id: str
    label: str = ""
    identifiers: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    #: Restrict to these source keys. Empty means "wherever it turns up".
    sources: tuple[str, ...] = ()
    note: str = ""

    def identifier_keys(self) -> set[str]:
        keys: set[str] = set()
        for value in self.identifiers:
            keys |= normalise_identifier(value)
        return keys

    def name_keys(self) -> set[str]:
        keys = {normalise_name(name) for name in (*self.names, self.label)}
        return {key for key in keys if key}

    def covers(self, source: str) -> bool:
        return not self.sources or source in self.sources


@dataclass
class Watchlist:
    """A named set of entities, plus the indexes that make matching cheap."""

    name: str = "watchlist"
    entities: list[WatchedEntity] = field(default_factory=list)
    _by_identifier: dict[str, WatchedEntity] = field(default_factory=dict, repr=False)
    _by_name: dict[str, WatchedEntity] = field(default_factory=dict, repr=False)
    #: Identifier or name keys claimed by more than one entity: ambiguous, so
    #: matched by none of them. Surfaced rather than silently resolved.
    collisions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self._by_identifier, self._by_name, self.collisions = {}, {}, []
        for entity in self.entities:
            for key in entity.identifier_keys():
                self._claim(self._by_identifier, key, entity)
            for key in entity.name_keys():
                self._claim(self._by_name, key, entity)

    def _claim(self, index: dict[str, WatchedEntity], key: str, entity: WatchedEntity) -> None:
        held = index.get(key)
        if held is None:
            index[key] = entity
        elif held.id != entity.id:
            # Two watched entities claiming one key means the list is wrong.
            # Dropping the key is the only answer that cannot mis-attribute.
            index.pop(key, None)
            self.collisions.append(key)

    # -- matching ----------------------------------------------------------

    def match_row(
        self,
        row: Mapping[str, Any],
        *,
        source: str,
        identifier_columns: Sequence[str] = (),
        name_columns: Sequence[str] = (),
    ) -> Match | None:
        """Return why this row is about a watched entity, or ``None``.

        Identifier columns are tried first and in the order the source declares
        them, so the strongest available claim wins.
        """
        for column in identifier_columns:
            raw = str(row.get(column, "") or "").strip()
            if not raw:
                continue
            for key in normalise_identifier(raw):
                entity = self._by_identifier.get(key)
                if entity is not None and entity.covers(source):
                    return Match(entity.id, entity.label or entity.id, "identifier", column, raw)
        for column in name_columns:
            raw = str(row.get(column, "") or "").strip()
            if not raw:
                continue
            # A flattened cell can hold several names joined with " | ".
            for part in raw.split(" | "):
                entity = self._by_name.get(normalise_name(part))
                if entity is not None and entity.covers(source):
                    return Match(entity.id, entity.label or entity.id, "name", column, part)
        return None

    def scope(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source: str,
        identifier_columns: Sequence[str] = (),
        name_columns: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep only the rows about watched entities, annotated with why."""
        out: list[dict[str, Any]] = []
        for row in rows:
            match = self.match_row(
                row,
                source=source,
                identifier_columns=identifier_columns,
                name_columns=name_columns,
            )
            if match is None:
                continue
            out.append(
                {
                    "_entity_id": match.entity_id,
                    "_entity_label": match.entity_label,
                    "_match": match.kind,
                    "_match_column": match.column,
                    "_match_value": match.value,
                    **dict(row),
                }
            )
        return out

    def coverage(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source: str,
        identifier_columns: Sequence[str] = (),
        name_columns: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Which watched entities this source can actually see.

        The failure mode this exists to prevent: a watchlist that quietly
        monitors 31 of the 40 entities somebody believes it is monitoring. An
        entity nobody can find is not "no news", it is no coverage.
        """
        found: dict[str, str] = {}
        for row in rows:
            match = self.match_row(
                row,
                source=source,
                identifier_columns=identifier_columns,
                name_columns=name_columns,
            )
            if match is not None:
                found.setdefault(match.entity_id, match.kind)
        applicable = [e for e in self.entities if e.covers(source)]
        return {
            "source": source,
            "watched": len(applicable),
            "matched": len(found),
            "by_identifier": sum(1 for kind in found.values() if kind == "identifier"),
            "by_name": sum(1 for kind in found.values() if kind == "name"),
            "unmatched": sorted(e.id for e in applicable if e.id not in found),
            "collisions": sorted(set(self.collisions)),
        }

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Watchlist:
        """Read a watchlist from JSON or CSV, chosen by extension."""
        source_path = Path(path)
        text = source_path.read_text(encoding="utf-8-sig")
        if source_path.suffix.lower() == ".csv":
            return cls.from_csv(text, name=source_path.stem)
        return cls.from_json(text, name=source_path.stem)

    @classmethod
    def from_json(cls, text: str, *, name: str = "watchlist") -> Watchlist:
        payload = json.loads(text)
        if isinstance(payload, list):
            payload = {"entities": payload}
        if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
            raise ValueError(
                "a watchlist is {'name': …, 'entities': [ … ]} or a bare list of entities"
            )
        entities = [
            _entity_from_mapping(item, index) for index, item in enumerate(payload["entities"])
        ]
        return cls(name=str(payload.get("name") or name), entities=_unique(entities))

    @classmethod
    def from_csv(cls, text: str, *, name: str = "watchlist") -> Watchlist:
        """One entity per line: ``id,label,identifiers,names,sources,note``.

        Multi-valued cells are pipe-separated, because a compliance team's list
        arrives as a spreadsheet far more often than as JSON.
        """
        rows = list(csv.DictReader(io.StringIO(text)))
        entities = [
            _entity_from_mapping(
                {
                    key: (
                        _split_cell(value) if key in ("identifiers", "names", "sources") else value
                    )
                    for key, value in row.items()
                    if key
                },
                index,
            )
            for index, row in enumerate(rows)
        ]
        return cls(name=name, entities=_unique(entities))

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "entities": [
                    {
                        "id": e.id,
                        "label": e.label,
                        "identifiers": list(e.identifiers),
                        "names": list(e.names),
                        "sources": list(e.sources),
                        "note": e.note,
                    }
                    for e in self.entities
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


def _unique(entities: Sequence[WatchedEntity]) -> list[WatchedEntity]:
    """Reject a list that names one id twice.

    Two entries sharing an id makes every downstream answer ambiguous: an event
    is attributed to "acme", the coverage report counts "acme" once, and which
    of the two rows it meant is unanswerable. That is exactly the confident
    wrong answer this project refuses to produce, and the fix in the input file
    is trivial.
    """
    seen: dict[str, int] = {}
    for position, entity in enumerate(entities, start=1):
        if entity.id in seen:
            raise ValueError(
                f"watchlist entry {position} reuses the id {entity.id!r} from entry "
                f"{seen[entity.id]}; ids must be unique so an event can be attributed"
            )
        seen[entity.id] = position
    return list(entities)


def _split_cell(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _entity_from_mapping(item: Any, index: int) -> WatchedEntity:
    if isinstance(item, str):
        return WatchedEntity(id=item.strip() or f"entity-{index + 1}", label=item.strip())
    if not isinstance(item, Mapping):
        raise ValueError(f"watchlist entry {index + 1} is {type(item).__name__}, not an object")
    label = str(item.get("label") or item.get("name") or "").strip()
    identifiers = tuple(_split_cell(item.get("identifiers") or item.get("identifier") or []))
    names = tuple(_split_cell(item.get("names") or []))
    if label and label not in names:
        names = (label, *names)
    entity_id = str(item.get("id") or "").strip() or _slug(label) or f"entity-{index + 1}"
    if not identifiers and not names:
        raise ValueError(
            f"watchlist entry {entity_id!r} has neither an identifier nor a name, "
            f"so nothing could ever match it"
        )
    return WatchedEntity(
        id=entity_id,
        label=label or entity_id,
        identifiers=identifiers,
        names=names,
        sources=tuple(_split_cell(item.get("sources") or [])),
        note=str(item.get("note") or ""),
    )


def _slug(text: str) -> str:
    normalised = normalise_name(text)
    return re.sub(r"\s+", "-", normalised)[:48]
