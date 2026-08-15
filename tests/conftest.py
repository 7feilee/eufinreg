"""Shared fixtures. Every test in this suite is offline by construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eufinreg.http import Fetcher

DATA = Path(__file__).parent / "data"


def load_json(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


def load_text(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8-sig")


@pytest.fixture
def fetcher() -> Fetcher:
    """A Fetcher that never really sleeps and never really waits.

    ``Fetcher.slept`` already records every delay, so the fake only advances a
    virtual clock — appending here too would double-count.
    """
    clock = {"t": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    return Fetcher(
        delay=0.0,
        retries=2,
        backoff_base=1.0,
        jitter=0.0,
        sleep=fake_sleep,
        monotonic=lambda: clock["t"],
        user_agent="eufinreg-tests/0",
    )


@pytest.fixture(autouse=True)
def _reset_source_caches():
    """Sources are module-level singletons; no test may inherit another's data."""
    from eufinreg.sources import ALL_SOURCES

    def reset_all() -> None:
        for source in ALL_SOURCES:
            reset = getattr(source, "reset", None)
            if callable(reset):
                reset()

    reset_all()
    yield
    reset_all()


@pytest.fixture
def upreg_pages() -> list[dict]:
    return [load_json(f"upreg_page{n}.json") for n in (1, 2, 3)]
