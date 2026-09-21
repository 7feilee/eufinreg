"""Run configuration — what an operator sets once instead of typing every night.

A scheduled job has more settings than a command line wants to carry: which
registers, under which selection, into which store, with what retention, under
whose User-Agent. Those belong in a file that can be reviewed and version
controlled, not in a cron line nobody can read.

The format is **JSON**, deliberately. TOML would be friendlier, but ``tomllib``
only arrives in Python 3.11 and this package supports 3.10; adding a parser
dependency to read a config file is a bad trade for a project whose whole
premise is one runtime dependency.

A minimal file::

    {
      "store": "./eufinreg-store",
      "user_agent": "acme-compliance/1.0 (+https://acme.example; ops@acme.example)",
      "preset": "dach",
      "retention_days": 400,
      "watchlists": ["watchlists/counterparties.json"]
    }
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .http import DEFAULT_USER_AGENT

#: Named source sets. A preset is an opinion about which registers answer a
#: given market's questions — the reason this tool has a shape rather than just
#: a list of eighteen endpoints.
PRESETS: dict[str, tuple[tuple[str, dict[str, str]], ...]] = {
    # The DACH licence picture, and the reason for each entry:
    "dach": (
        ("finma", {}),  # CH — the only route to a Swiss licence
        ("gisa", {}),  # AT — every active trade licence
        ("gisa-codes", {}),  # AT — the labels for the codes above
        ("upreg", {}),  # EU — where BaFin- and FMA-supervised firms actually live
        ("eba-psd", {}),  # EU — BaFin's and the FMA's ZAG/payment filings
        ("mica-casp", {}),  # EU — crypto-asset providers, incl. DE and AT
    ),
    # Everything that can be scheduled at all, for a completeness run.
    "all": (),
}


@dataclass
class SourceSpec:
    """One register in a scheduled run, with the selection it runs under."""

    key: str
    select: str = ""
    query: str = ""
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "select": self.select, "query": self.query}


@dataclass
class Config:
    """Everything a scheduled run needs, and nothing it does not."""

    store: Path = Path("./eufinreg-store")
    user_agent: str = DEFAULT_USER_AGENT
    delay: float = 1.0
    timeout: float = 60.0
    retries: int = 5
    sources: list[SourceSpec] = field(default_factory=list)
    #: Delete snapshots older than this, subject to ``keep_last``. ``None``
    #: keeps everything, which is the right default for an archive whose value
    #: is its length.
    retention_days: int | None = None
    keep_last: int = 2
    watchlists: list[Path] = field(default_factory=list)
    #: Serve rows the source flags as personal data. Off unless someone says so.
    allow_personal_data: bool = False
    capture_raw: bool = True
    #: gzip snapshots. On by default: the Austrian register alone is 607 MB of
    #: JSONL per snapshot and compresses tenfold, and an archive is kept for
    #: years. The digest of the logical rows is unaffected either way.
    compress: bool = True

    @classmethod
    def load(cls, path: str | Path) -> Config:
        source_path = Path(path)
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ValueError(f"{source_path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"{source_path} must contain a JSON object")
        config = cls.from_mapping(payload, base=source_path.parent)
        return config

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, base: Path | None = None) -> Config:
        base = base or Path.cwd()
        unknown = set(payload) - {
            "store",
            "user_agent",
            "delay",
            "timeout",
            "retries",
            "sources",
            "preset",
            "retention_days",
            "keep_last",
            "watchlists",
            "allow_personal_data",
            "capture_raw",
            "compress",
        }
        if unknown:
            # A typo in a config file is otherwise a setting that silently never
            # applies, which is the worst kind of misconfiguration.
            raise ValueError(f"unknown config key(s): {', '.join(sorted(unknown))}")

        sources = [_spec(item) for item in payload.get("sources", [])]
        preset = str(payload.get("preset") or "")
        if preset:
            sources = resolve_preset(preset) + [
                s for s in sources if s.key not in {p.key for p in resolve_preset(preset)}
            ]
        return cls(
            store=_path(payload.get("store", "./eufinreg-store"), base),
            user_agent=str(payload.get("user_agent") or DEFAULT_USER_AGENT),
            delay=float(payload.get("delay", 1.0)),
            timeout=float(payload.get("timeout", 60.0)),
            retries=int(payload.get("retries", 5)),
            sources=sources,
            retention_days=_retention(payload.get("retention_days")),
            keep_last=_keep_last(payload.get("keep_last", 2)),
            watchlists=[_path(p, base) for p in payload.get("watchlists", [])],
            allow_personal_data=bool(payload.get("allow_personal_data", False)),
            capture_raw=bool(payload.get("capture_raw", True)),
            compress=bool(payload.get("compress", True)),
        )

    def enabled_sources(self) -> list[SourceSpec]:
        return [spec for spec in self.sources if spec.enabled]


def resolve_preset(name: str) -> list[SourceSpec]:
    """``'dach'`` → the source specs that answer a DACH licence question."""
    key = name.strip().lower()
    if key == "all":
        from .sources import ALL_SOURCES  # local import: avoids a cycle at import time

        return [SourceSpec(key=s.key) for s in ALL_SOURCES if s.bulk_readable]
    try:
        entries = PRESETS[key]
    except KeyError as exc:
        raise ValueError(
            f"unknown preset {name!r}; known presets: {', '.join(sorted(PRESETS))}"
        ) from exc
    return [SourceSpec(key=source_key, **options) for source_key, options in entries]


def _spec(item: Any) -> SourceSpec:
    if isinstance(item, str):
        return SourceSpec(key=item)
    if not isinstance(item, Mapping) or not item.get("key"):
        raise ValueError('each source is "key" or {"key": …, "select": …, "query": …}')
    return SourceSpec(
        key=str(item["key"]),
        select=str(item.get("select") or ""),
        query=str(item.get("query") or ""),
        enabled=bool(item.get("enabled", True)),
    )


def _retention(value: Any) -> int | None:
    """``retention_days`` → days to keep, or ``None`` for "keep everything".

    A negative value is refused rather than clamped: it would put the cutoff in
    the future and delete every snapshot outside ``keep_last``, which is a typo
    that costs an archive.
    """
    if value in (None, "", 0):
        return None
    days = int(value)
    if days < 0:
        raise ValueError(
            f"retention_days must be positive or 0/null for 'keep everything'; got {days}"
        )
    return days


def _keep_last(value: Any) -> int:
    """The floor on how many snapshots survive pruning. Never below one.

    ``keep_last: 0`` combined with any retention window would empty a source
    entirely, and the whole point of the floor is that the age rule can never
    delete the last snapshot standing.
    """
    keep = int(value)
    if keep < 1:
        raise ValueError(
            f"keep_last must be at least 1, so pruning can never empty a source; got {keep}"
        )
    return keep


def _path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path)


def merge_cli(config: Config, args: Any, *, source_keys: Sequence[str] = ()) -> Config:
    """Command-line arguments win over the file, which wins over the defaults."""
    if getattr(args, "store", None):
        config.store = Path(args.store)
    if getattr(args, "user_agent", None):
        config.user_agent = args.user_agent
    if getattr(args, "delay", None) is not None:
        config.delay = args.delay
    if getattr(args, "preset", None):
        config.sources = resolve_preset(args.preset)
    if source_keys:
        config.sources = [SourceSpec(key=key) for key in source_keys]
    if getattr(args, "retention_days", None) is not None:
        config.retention_days = args.retention_days
    return config
