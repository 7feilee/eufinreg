"""Registry of the public registers this tool knows how to read."""

from __future__ import annotations

from .base import Query, Source
from .eba_psd import EBA_SOURCES
from .esma_mica import MICA_SOURCES
from .esma_solr import SOLR_SOURCES, solr_source_for_core

ALL_SOURCES: tuple[Source, ...] = (*SOLR_SOURCES, *MICA_SOURCES, *EBA_SOURCES)

_BY_KEY = {source.key: source for source in ALL_SOURCES}

DEFAULT_SOURCE = "upreg"


class UnknownSource(KeyError):
    pass


def get_source(key: str) -> Source:
    """Look up a source by CLI name.

    ``solr:<core>`` addresses any ESMA Solr core directly, including ones this
    project has never seen — useful when ESMA adds a register and this package
    has not been updated yet.
    """
    if key.startswith("solr:"):
        return solr_source_for_core(key.split(":", 1)[1])
    try:
        source = _BY_KEY[key]
    except KeyError as exc:
        known = ", ".join(sorted(_BY_KEY))
        raise UnknownSource(f"unknown source {key!r}; known sources: {known}, solr:<core>") from exc
    # The sources are singletons; make sure nobody inherits a stale download.
    reset = getattr(source, "reset", None)
    if callable(reset):
        reset()
    return source


def source_keys() -> list[str]:
    return [source.key for source in ALL_SOURCES]


__all__ = [
    "ALL_SOURCES",
    "DEFAULT_SOURCE",
    "Query",
    "Source",
    "UnknownSource",
    "get_source",
    "source_keys",
]
