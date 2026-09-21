"""The inputs nobody sends on purpose.

Every case here is one that was found by probing the real code rather than
imagined: each one either produced a wrong answer, a crash at write time, or a
way to lose an archive. They live together because they cut across modules — the
common thread is "what does this do when the data is hostile, empty, or just
unusual", which is precisely what a scheduled job meets at 04:12 and a
hand-driven CLI never does.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from eufinreg.config import Config
from eufinreg.evidence import unsafe_member, verify_bundle
from eufinreg.filters import FieldFilter, apply_filters
from eufinreg.flatten import column_order, scalar
from eufinreg.snapshot import diff_snapshots, read_snapshot, row_key, write_snapshot
from eufinreg.store import Store
from eufinreg.watchlist import WatchedEntity, Watchlist, normalise_identifier, normalise_name


class TestIdentityCannotBeForged:
    """A key is an identity claim. Two entities must never share one by accident."""

    def test_a_value_containing_the_separator_cannot_impersonate_another_row(self):
        # Without escaping, ("x\x1fy", "z") and ("x", "y\x1fz") produce the same
        # key — and a licence withdrawal is then attributed to the wrong company.
        left = row_key({"a": "x\x1fy", "b": "z"}, ("a", "b"))
        right = row_key({"a": "x", "b": "y\x1fz"}, ("a", "b"))
        assert left != right

    def test_none_does_not_become_the_string_none(self):
        # Otherwise every row with a missing identifier collapses into one
        # entity named "None", and the diff reports one company churning.
        assert row_key({"a": None}, ("a",)).startswith("sha256:")
        assert row_key({"a": None}, ("a",)) != row_key({"a": "None"}, ("a",))

    def test_rows_with_no_key_at_all_stay_distinct(self):
        first = row_key({"a": None, "name": "Alpha"}, ("a",))
        second = row_key({"a": None, "name": "Beta"}, ("a",))
        assert first != second

    def test_a_present_key_still_wins_over_the_content_hash(self):
        assert row_key({"a": "CHE-1", "noise": "x"}, ("a",)) == row_key(
            {"a": "CHE-1", "noise": "y"}, ("a",)
        )


class TestValuesThatBreakWriters:
    def test_bytes_never_reach_the_csv_writer(self):
        # A register sending an undecodable field would otherwise write
        # b'\xff\xfe…' into the CSV, or raise at json.dumps time.
        assert scalar(b"abc") == "abc"
        assert scalar(b"\xff\xfe") == "��"

    def test_an_empty_result_set_still_writes_a_valid_snapshot(self, tmp_path):
        path, digest = write_snapshot(tmp_path / "empty.jsonl", [], source="finma")
        snapshot = read_snapshot(path)
        assert snapshot.rows == []
        assert digest  # the digest of nothing is still a digest

    def test_diffing_against_an_empty_snapshot_reports_every_row_as_added(self, tmp_path):
        write_snapshot(tmp_path / "a.jsonl", [], source="finma", key_columns=("UID",))
        write_snapshot(
            tmp_path / "b.jsonl", [{"UID": "CHE-1"}], source="finma", key_columns=("UID",)
        )
        changes = diff_snapshots(
            read_snapshot(tmp_path / "a.jsonl"), read_snapshot(tmp_path / "b.jsonl")
        )
        assert [c.kind for c in changes] == ["added"]

    def test_ragged_rows_produce_the_union_of_columns(self):
        assert column_order([{"a": 1}, {"b": 2}, {"a": 3, "c": 4}]) == ["a", "b", "c"]


class TestRetentionCannotEmptyAnArchive:
    """Deleting an archive is not undoable, so the guards are refusals."""

    def test_a_negative_retention_window_is_refused(self):
        # It would put the cutoff in the future and delete everything.
        with pytest.raises(ValueError, match="retention_days must be positive"):
            Config.from_mapping({"retention_days": -5})

    def test_keep_last_below_one_is_refused(self):
        with pytest.raises(ValueError, match="keep_last must be at least 1"):
            Config.from_mapping({"keep_last": 0})

    def test_zero_retention_means_keep_everything_not_delete_everything(self):
        assert Config.from_mapping({"retention_days": 0}).retention_days is None

    def test_prune_refuses_a_negative_window_whoever_calls_it(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        store.add_snapshot("finma", [{"UID": "CHE-1"}], stamp="20260101T000000Z")
        with pytest.raises(ValueError, match="cutoff in the future"):
            store.prune("finma", keep_days=-1)

    def test_pruning_never_removes_the_last_snapshot(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        for day in range(1, 5):
            store.add_snapshot("finma", [{"UID": "CHE-1"}], stamp=f"2026010{day}T000000Z")
        store.prune("finma", keep_days=1, keep_last=0, now=4_102_444_800)  # year 2100
        assert len(store.snapshots("finma")) >= 1


class TestBundleMemberNames:
    """A bundle is opened by somebody else, so the names are checked too."""

    def test_traversal_names_are_recognised(self):
        assert unsafe_member("../escape.txt")
        assert unsafe_member("/etc/passwd")
        assert unsafe_member("raw/../../x")
        assert not unsafe_member("raw/00001_finma.csv")
        assert not unsafe_member("receipt.json")

    def test_verification_rejects_a_traversal_member_even_with_a_valid_digest(self, tmp_path):
        import hashlib

        payload = b"hi"
        path = tmp_path / "b.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "MANIFEST.json",
                json.dumps(
                    {
                        "files": [
                            {"name": "../escape.txt", "sha256": hashlib.sha256(payload).hexdigest()}
                        ]
                    }
                ),
            )
            archive.writestr("../escape.txt", payload)
        ok, problems = verify_bundle(path)
        assert not ok
        assert "escape the extraction directory" in problems[0]


class TestTextHandling:
    def test_substring_matching_is_case_and_accent_aware_the_way_users_expect(self):
        rows, _ = apply_filters([{"n": "Zürich"}], contains=["ZÜRICH"])
        assert rows

    def test_a_field_filter_can_match_the_empty_string(self):
        # "which rows have no LEI" is a real question.
        assert FieldFilter.parse("lei=").matches({"lei": ""})
        assert not FieldFilter.parse("lei=").matches({"lei": "5299001"})

    def test_dotted_turkish_capitals_fold_like_everything_else(self):
        assert normalise_name("İstanbul AG") == normalise_name("istanbul ag")

    def test_an_identifier_of_pure_punctuation_is_not_a_key(self):
        assert normalise_identifier("---.---") == set()

    def test_a_name_that_normalises_to_nothing_never_matches(self):
        # "AG" alone is a legal form, not a name. Indexing it would make every
        # German company match every other one.
        watchlist = Watchlist(entities=[WatchedEntity(id="x", label="AG")])
        assert watchlist.match_row({"n": "GmbH"}, source="s", name_columns=("n",)) is None
        assert watchlist.match_row({"n": "AG"}, source="s", name_columns=("n",)) is None


class TestHostileXml:
    """The SOAP source parses XML somebody else controls."""

    def test_an_entity_bomb_does_not_expand(self):
        from eufinreg.sources.ch_uid import parse_response

        bomb = (
            '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><r>&c;</r>'
        )
        assert parse_response(bomb) == []

    def test_an_external_entity_is_refused_rather_than_fetched(self):
        from eufinreg.http import FetchError
        from eufinreg.sources.ch_uid import parse_response

        with pytest.raises(FetchError, match="cannot parse"):
            parse_response(
                '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                "<r>&x;</r>"
            )

    def test_malformed_xml_is_a_fetch_error_not_a_traceback(self):
        from eufinreg.http import FetchError
        from eufinreg.sources.ch_uid import parse_response

        with pytest.raises(FetchError):
            parse_response("<not-closed>")


class TestStoreRobustness:
    def test_an_unreadable_run_record_does_not_hide_the_rest(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        store.write_run({"run_id": "20260101T000000Z-aaaaaa", "ok": 1})
        (store.runs_dir / "20260102T000000Z-bbbbbb.json").write_text("{truncated", encoding="utf-8")
        runs = store.runs()
        assert len(runs) == 2
        assert any(r.get("unreadable") for r in runs)

    def test_a_run_id_cannot_write_outside_the_runs_directory(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        path = store.write_run({"ok": 1}, run_id="../../escape")
        assert path.parent == store.runs_dir

    def test_the_runs_directory_is_not_mistaken_for_a_source(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        store.write_run({"run_id": "r1"})
        store.add_snapshot("finma", [{"UID": "CHE-1"}], stamp="20260101T000000Z")
        assert store.sources() == ["finma"]

    def test_a_stray_file_in_a_snapshots_directory_is_ignored(self, tmp_path):
        store = Store(tmp_path / "store")
        store.init()
        store.add_snapshot("finma", [{"UID": "CHE-1"}], stamp="20260101T000000Z")
        (store.snapshots_dir("finma") / "notes.txt").write_text("hi", encoding="utf-8")
        assert len(store.snapshots("finma")) == 1


class TestSourceClientsUnderStrangeInput:
    """Every parser meets an empty file, a missing block or a shape nobody planned."""

    def test_a_header_only_csv_is_zero_rows_not_a_crash(self):
        from eufinreg.sources.finma import parse_csv
        from eufinreg.sources.gisa import GEWERBE

        assert parse_csv('"Name";"UID"\n') == []
        assert parse_csv("") == []
        assert GEWERBE.parse("nuts2,gewerbeart\n") == []
        assert GEWERBE.parse("") == []

    def test_a_nul_byte_in_a_cell_survives_to_the_row(self):
        from eufinreg.sources.finma import parse_csv

        rows = parse_csv('"Name";"UID"\n"a\x00b";"1"\n')
        assert len(rows) == 1

    def test_ted_keeps_an_unexpected_nested_object_as_json(self):
        # Not a Python repr: --inspect has to be able to show it and a reader
        # has to be able to parse it.
        from eufinreg.sources.ted import flatten_notice

        assert flatten_notice({"x": {"y": {"z": 1}}})["x"] == '{"z": 1}'

    def test_ted_counts_zero_winners_without_dividing_by_anything(self):
        from eufinreg.sources.ted import flatten_notice

        row = flatten_notice({"winner-name": {"deu": []}})
        assert row["winner_count"] == 0 and row["winner-name"] == ""

    def test_gleif_survives_a_record_with_no_attributes(self):
        from eufinreg.sources.gleif import flatten_record

        assert flatten_record({"id": "L1"}) == {"lei": "L1"}

    def test_a_watchlist_may_be_empty_but_not_ambiguous(self):
        assert Watchlist.from_json("[]").entities == []
        with pytest.raises(ValueError, match="reuses the id"):
            Watchlist.from_json('[{"id":"a","label":"A"},{"id":"a","label":"B"}]')

    def test_a_watchlist_that_is_not_a_list_or_object_says_so(self):
        with pytest.raises(ValueError, match="a watchlist is"):
            Watchlist.from_json("42")


class TestDuplicateKeysAreReported:
    """A register issuing one identifier twice must not silently shrink a diff."""

    def _snapshot(self, rows):
        from eufinreg.snapshot import Snapshot

        return Snapshot(source="s", rows=rows, key_columns=("UID",))

    def test_the_index_keeps_the_first_and_counts_the_rest(self):
        snapshot = self._snapshot([{"UID": "1", "n": "a"}, {"UID": "1", "n": "b"}])
        assert len(snapshot.index()) == 1
        assert snapshot.meta["duplicate_keys"] == 1

    def test_the_summary_says_the_counts_are_understated(self):
        from eufinreg.snapshot import summarise

        new = self._snapshot([{"UID": "1", "n": "a"}, {"UID": "1", "n": "b"}])
        old = self._snapshot([{"UID": "1", "n": "a"}])
        changes = diff_snapshots(old, new)
        text = summarise(changes, old=old, new=new)
        assert "duplicate identifiers" in text
        assert "UID" in text

    def test_a_clean_snapshot_says_nothing_about_duplicates(self):
        from eufinreg.snapshot import summarise

        snapshot = self._snapshot([{"UID": "1"}, {"UID": "2"}])
        assert "duplicate" not in summarise([], old=snapshot, new=snapshot)
