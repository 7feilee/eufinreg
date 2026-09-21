"""Snapshots and diffs — the change data the registers do not publish."""

from __future__ import annotations

import json

import pytest

from eufinreg.snapshot import (
    diff_snapshots,
    read_snapshot,
    row_key,
    summarise,
    write_snapshot,
)

ROWS = [
    {"UID": "CHE-1", "Name": "Alpha AG", "City": "Zürich", "type": "Bank"},
    {"UID": "CHE-2", "Name": "Beta SA", "City": "Genève", "type": "Trustee"},
    {"UID": "CHE-3", "Name": "Gamma Sagl", "City": "Lugano", "type": "Portfolio manager"},
]
KEYS = ("UID", "Name")


def _write(tmp_path, rows, name="snap.jsonl", **kwargs):
    return write_snapshot(tmp_path / name, rows, source="finma", key_columns=KEYS, **kwargs)[0]


class TestFileFormat:
    def test_the_header_records_what_was_asked_for(self, tmp_path):
        path = _write(tmp_path, ROWS, meta={"select": "bank"})
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]
        assert header["source"] == "finma"
        assert header["row_count"] == 3
        assert header["select"] == "bank"
        assert header["key_columns"] == list(KEYS)

    def test_the_sidecar_is_checkable_with_sha256sum(self, tmp_path):
        # It covers the file exactly as written, not the logical body, so that
        # `sha256sum -c` works without this tool.
        import hashlib

        path, _ = write_snapshot(tmp_path / "snap.jsonl", ROWS, source="finma", key_columns=KEYS)
        sidecar = path.with_suffix(path.suffix + ".sha256").read_text(encoding="utf-8")
        assert hashlib.sha256(path.read_bytes()).hexdigest() in sidecar
        assert path.name in sidecar

    def test_the_body_digest_is_independent_of_the_header_and_compression(self, tmp_path):
        plain, plain_digest = write_snapshot(
            tmp_path / "a.jsonl", ROWS, source="finma", key_columns=KEYS
        )
        gz, gz_digest = write_snapshot(
            tmp_path / "b.jsonl", ROWS, source="finma", key_columns=KEYS, compress=True
        )
        assert plain_digest == gz_digest
        assert gz.name.endswith(".jsonl.gz")
        assert read_snapshot(gz).rows == read_snapshot(plain).rows

    def test_compression_is_byte_stable(self, tmp_path):
        # gzip stamps the current time into its header unless told not to, which
        # would make every rebuild of identical data a different file.
        first, _ = write_snapshot(tmp_path / "a.jsonl", ROWS, source="finma", compress=True)
        second, _ = write_snapshot(tmp_path / "b.jsonl", ROWS, source="finma", compress=True)
        assert first.read_bytes() == second.read_bytes()

    def test_a_compressed_snapshot_is_much_smaller(self, tmp_path):
        rows = [{"n": str(i), "kind": "freies Gewerbe", "town": "Wien"} for i in range(2000)]
        plain, _ = write_snapshot(tmp_path / "a.jsonl", rows, source="gisa")
        gz, _ = write_snapshot(tmp_path / "b.jsonl", rows, source="gisa", compress=True)
        assert gz.stat().st_size * 4 < plain.stat().st_size

    def test_the_same_data_produces_the_same_bytes(self, tmp_path):
        # Order-independent, so "nothing changed" is checkable rather than felt.
        a = _write(tmp_path, ROWS, name="a.jsonl")
        b = _write(tmp_path, list(reversed(ROWS)), name="b.jsonl")
        assert a.read_bytes().split(b"\n", 1)[1] == b.read_bytes().split(b"\n", 1)[1]

    def test_a_round_trip_keeps_every_row(self, tmp_path):
        snapshot = read_snapshot(_write(tmp_path, ROWS))
        assert len(snapshot.rows) == 3
        assert snapshot.key_columns == KEYS

    def test_an_edited_snapshot_is_rejected(self, tmp_path):
        path = _write(tmp_path, ROWS)
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace("Zürich", "Zug")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="fails its own checksum"):
            read_snapshot(path)

    def test_a_file_that_is_not_a_snapshot_says_so(self, tmp_path):
        path = tmp_path / "rows.jsonl"
        path.write_text('{"UID": "CHE-1"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="does not start with a snapshot header"):
            read_snapshot(path)


class TestKeys:
    def test_declared_columns_are_used(self):
        assert row_key({"UID": "CHE-1", "Name": "Alpha"}, ("UID", "Name")).startswith("CHE-1")

    def test_a_row_with_empty_key_columns_falls_back_to_content(self):
        assert row_key({"UID": "", "Name": ""}, ("UID", "Name")).startswith("sha256:")

    def test_content_keys_ignore_the_fetch_metadata(self):
        # _vintage changes every month even when the row does not.
        a = row_key({"nuts2": "AT13", "_vintage": "2026.07"}, ())
        b = row_key({"nuts2": "AT13", "_vintage": "2026.08"}, ())
        assert a == b


class TestDiff:
    def test_additions_removals_and_column_level_changes(self, tmp_path):
        changed = [dict(ROWS[0], City="Zug"), ROWS[1], {"UID": "CHE-4", "Name": "Delta AG"}]
        old = read_snapshot(_write(tmp_path, ROWS, name="old.jsonl"))
        new = read_snapshot(_write(tmp_path, changed, name="new.jsonl"))
        changes = {c.kind: c for c in diff_snapshots(old, new)}

        assert changes["added"].row["Name"] == "Delta AG"
        assert changes["removed"].previous["Name"] == "Gamma Sagl"
        assert changes["changed"].columns == ("City",)
        assert changes["changed"].previous["City"] == "Zürich"

    def test_an_unchanged_register_produces_no_events(self, tmp_path):
        old = read_snapshot(_write(tmp_path, ROWS, name="old.jsonl"))
        new = read_snapshot(_write(tmp_path, ROWS, name="new.jsonl"))
        assert diff_snapshots(old, new) == []

    def test_volatile_columns_do_not_manufacture_changes(self, tmp_path):
        old = read_snapshot(_write(tmp_path, [dict(ROWS[0], _vintage="2026.07")], name="o.jsonl"))
        new = read_snapshot(_write(tmp_path, [dict(ROWS[0], _vintage="2026.08")], name="n.jsonl"))
        assert diff_snapshots(old, new) == []

    def test_two_different_registers_are_not_compared(self, tmp_path):
        old = read_snapshot(_write(tmp_path, ROWS, name="old.jsonl"))
        path = write_snapshot(tmp_path / "other.jsonl", ROWS, source="upreg")[0]
        with pytest.raises(ValueError, match="different registers"):
            diff_snapshots(old, read_snapshot(path))

    def test_a_change_row_shows_the_previous_value(self, tmp_path):
        old = read_snapshot(_write(tmp_path, ROWS, name="old.jsonl"))
        new = read_snapshot(
            _write(tmp_path, [dict(ROWS[0], City="Zug"), *ROWS[1:]], name="n.jsonl")
        )
        row = next(c for c in diff_snapshots(old, new) if c.kind == "changed").as_row()
        assert row["City"] == "Zug"
        assert row["City__was"] == "Zürich"
        assert "\x1f" not in row["_key"]


class TestSummary:
    def test_a_keyless_source_is_flagged(self, tmp_path):
        # GISA publishes licences with no holder and no licence number, so a
        # modification cannot be told from a removal plus an addition.
        path = write_snapshot(tmp_path / "g.jsonl", [{"nuts2": "AT13"}], source="gisa")[0]
        snapshot = read_snapshot(path)
        assert "no stable key" in summarise([], old=snapshot, new=snapshot)

    def test_a_keyed_source_is_not_flagged(self, tmp_path):
        snapshot = read_snapshot(_write(tmp_path, ROWS))
        assert "no stable key" not in summarise([], old=snapshot, new=snapshot)
