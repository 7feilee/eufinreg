"""Registry of the public registers this tool knows how to read."""

from __future__ import annotations

from .base import Query, Source
from .ch_uid import CH_UID_SOURCES
from .ctis import CTIS_SOURCES
from .eba_psd import EBA_SOURCES
from .esma_mica import MICA_SOURCES
from .esma_solr import SOLR_SOURCES, solr_source_for_core
from .eudamed import EUDAMED_SOURCES
from .finma import FINMA_SOURCES
from .gisa import GISA_SOURCES
from .gleif import GLEIF_SOURCES
from .ted import TED_SOURCES
from .vies import VIES_SOURCES

ALL_SOURCES: tuple[Source, ...] = (
    *SOLR_SOURCES,
    *MICA_SOURCES,
    *EBA_SOURCES,
    *EUDAMED_SOURCES,
    *CTIS_SOURCES,
    # DACH national registers, which the EU-level ones do not reach:
    *FINMA_SOURCES,
    *CH_UID_SOURCES,
    *GISA_SOURCES,
    # Cross-cutting: identity, verification, and public money as a leading
    # indicator. None of these is a licence list; all three are disclosures
    # somebody is obliged to make.
    *GLEIF_SOURCES,
    *VIES_SOURCES,
    *TED_SOURCES,
)

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
