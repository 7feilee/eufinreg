"""The snapshot store: layout, locking, retention, verification, staleness."""

from __future__ import annotations

import json

import pytest

from eufinreg.snapshot import Change
from eufinreg.store import (
    STALE_LOCK_SECONDS,
    Store,
    StoreLocked,
    parse_stamp,
    utc_stamp,
)

ROWS = [
    {"UID": "CHE-1", "Name": "Alpha AG"},
    {"UID": "CHE-2", "Name": "Beta SA"},
]
DAY = 86_400


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "store").init() and Store(tmp_path / "store")


class TestStamps:
    def test_stamps_sort_chronologically(self):
        assert utc_stamp(1_000_000) < utc_stamp(2_000_000)

    def test_a_stamp_round_trips_as_utc(self):
        # time.mktime would read the stamp as local time and be silently wrong
        # by the offset — and differently either side of a DST change.
        assert parse_stamp(utc_stamp(1_700_000_000)) == 1_700_000_000

    def test_a_bad_stamp_is_none_not_an_exception(self):
        assert parse_stamp("not-a-stamp") is None


class TestLayout:
    def test_init_writes_a_descriptor(self, store):
        descriptor = json.loads((store.root / "eufinreg-store.json").read_text(encoding="utf-8"))
        assert descriptor["format"] == 1

    def test_a_newer_store_format_is_refused(self, store):
        (store.root / "eufinreg-store.json").write_text('{"format": 99}', encoding="utf-8")
        with pytest.raises(ValueError, match="newer eufinreg"):
            store.check_format()

    def test_a_snapshot_lands_where_the_docs_say(self, store):
        ref = store.add_snapshot("finma", ROWS, key_columns=("UID",), stamp="20260816T040000Z")
        assert ref.path == store.root / "finma" / "snapshots" / "20260816T040000Z.jsonl"
        assert ref.sidecar.exists()
        assert ref.row_count == 2

    def test_listing_reads_headers_not_rows(self, store):
        store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        refs = store.snapshots("finma")
        assert [r.stamp for r in refs] == ["20260816T040000Z"]
        assert refs[0].row_count == 2 and refs[0].digest

    def test_snapshots_come_back_oldest_first(self, store):
        for stamp in ("20260818T040000Z", "20260816T040000Z", "20260817T040000Z"):
            store.add_snapshot("finma", ROWS, stamp=stamp)
        assert store.snapshots("finma")[0].stamp == "20260816T040000Z"
        assert store.latest("finma").stamp == "20260818T040000Z"
        assert store.previous("finma").stamp == "20260817T040000Z"

    def test_an_existing_stamp_is_never_overwritten(self, store):
        store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        with pytest.raises(FileExistsError, match="Refusing to overwrite"):
            store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")

    def test_only_source_directories_are_listed(self, store):
        store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        (store.root / "notes").mkdir()
        assert store.sources() == ["finma"]

    def test_a_partial_write_leaves_nothing_behind(self, store):
        ref = store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        assert not list(ref.path.parent.glob("*.partial"))


class TestChanges:
    def test_a_quiet_run_still_writes_a_file(self, store):
        # "We looked and nothing moved" must be distinguishable from "the run
        # never happened", which an absent file cannot express.
        path = store.write_changes("finma", "20260816T040000Z", [], against="20260815T040000Z")
        assert path.exists()
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]
        assert header["counts"] == {"added": 0, "changed": 0, "removed": 0}
        assert header["against"] == "20260815T040000Z"

    def test_events_are_readable_back_with_their_snapshot(self, store):
        change = Change(kind="removed", key="CHE-1", previous={"UID": "CHE-1", "Name": "Alpha AG"})
        store.write_changes("finma", "20260816T040000Z", [change])
        events = store.read_changes("finma")
        assert len(events) == 1
        assert events[0]["_change"] == "removed"
        assert events[0]["_snapshot"] == "20260816T040000Z"
        assert events[0]["_source"] == "finma"

    def test_since_filters_by_date_prefix(self, store):
        store.write_changes("finma", "20260801T040000Z", [Change(kind="added", key="a")])
        store.write_changes("finma", "20260816T040000Z", [Change(kind="added", key="b")])
        assert len(store.read_changes("finma", since="20260810")) == 1


class TestLocking:
    def test_a_second_run_is_refused(self, store):
        store.acquire_lock(now=1000.0)
        with pytest.raises(StoreLocked, match="held by pid"):
            Store(store.root).acquire_lock(now=1000.0)
        store.release_lock()

    def test_a_stale_lock_is_reclaimed(self, store):
        # A run killed mid-download must not block every future run until
        # somebody notices and deletes a file by hand.
        store.acquire_lock(now=1000.0)
        Store(store.root).acquire_lock(now=1000.0 + STALE_LOCK_SECONDS + 1)

    def test_the_context_manager_releases(self, store):
        with Store(store.root):
            assert (store.root / ".lock").exists()
        assert not (store.root / ".lock").exists()


class TestVerify:
    def test_a_healthy_store_verifies(self, store):
        store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        assert all(ok for _, ok, _ in store.verify())

    def test_an_edited_snapshot_is_caught(self, store):
        ref = store.add_snapshot("finma", ROWS, stamp="20260816T040000Z")
        lines = ref.path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace("Alpha AG", "Alpha SA")
        ref.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        results = store.verify("finma")
        assert not results[0][1]
        assert "checksum" in results[0][2]


class TestPrune:
    def _fill(self, store, days: int) -> None:
        for offset in range(days):
            store.add_snapshot("finma", ROWS, stamp=utc_stamp(1_700_000_000 + offset * DAY))

    def test_old_snapshots_go_and_recent_ones_stay(self, store):
        self._fill(store, 10)
        now = 1_700_000_000 + 10 * DAY
        removed = store.prune("finma", keep_days=5, keep_last=2, now=now)
        assert removed
        assert len(store.snapshots("finma")) == 10 - len(removed)

    def test_keep_last_wins_over_the_age_rule(self, store):
        # Deleting an archive is not undoable; the floor is absolute.
        self._fill(store, 3)
        now = 1_700_000_000 + 999 * DAY
        store.prune("finma", keep_days=1, keep_last=2, now=now)
        assert len(store.snapshots("finma")) == 2

    def test_dry_run_deletes_nothing(self, store):
        self._fill(store, 6)
        now = 1_700_000_000 + 6 * DAY
        planned = store.prune("finma", keep_days=1, keep_last=2, now=now, dry_run=True)
        assert planned
        assert len(store.snapshots("finma")) == 6

    def test_raw_bodies_go_with_their_snapshot(self, store):
        stamp = utc_stamp(1_700_000_000)
        store.add_snapshot("finma", ROWS, stamp=stamp)
        raw = store.raw_dir("finma", stamp)
        raw.mkdir(parents=True)
        (raw / "00001_finma.csv").write_text("x", encoding="utf-8")
        store.add_snapshot("finma", ROWS, stamp=utc_stamp(1_700_000_000 + DAY))
        store.add_snapshot("finma", ROWS, stamp=utc_stamp(1_700_000_000 + 2 * DAY))
        store.prune("finma", keep_days=1, keep_last=2, now=1_700_000_000 + 3 * DAY)
        assert not raw.exists()


class TestStatus:
    def test_staleness_is_measured_against_the_registers_cadence(self, store):
        stamp = utc_stamp(1_700_000_000)
        store.add_snapshot("finma", ROWS, stamp=stamp)
        store.add_snapshot("gisa", ROWS, stamp=stamp)
        three_days_later = 1_700_000_000 + 3 * DAY
        status = {
            entry["source"]: entry
            for entry in store.status({"finma": 24.0, "gisa": 720.0}, now=three_days_later)
        }
        # A nightly file three days old is late; a monthly one is not.
        assert status["finma"]["stale"] is True
        assert status["gisa"]["stale"] is False

    def test_a_source_with_no_declared_cadence_is_never_stale(self, store):
        store.add_snapshot("upreg", ROWS, stamp=utc_stamp(1_000_000))
        entry = store.status({"upreg": None}, now=1_700_000_000)[0]
        assert entry["stale"] is False
        assert entry["cadence_hours"] is None
