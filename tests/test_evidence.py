"""Evidence bundles: reproducible, self-verifying, and honest about their limits."""

from __future__ import annotations

import json
import zipfile

import pytest

from eufinreg.evidence import build_bundle, verify_bundle
from eufinreg.sources import get_source
from eufinreg.store import Store

ROWS = [
    {"UID": "CHE-101.329.561", "Name": "UBS AG", "City": "Zürich"},
    {"UID": "CHE-108.954.607", "Name": "Zürcher Kantonalbank", "City": "Zürich"},
]


@pytest.fixture
def ref(tmp_path):
    store = Store(tmp_path / "store")
    store.init()
    reference = store.add_snapshot(
        "finma",
        ROWS,
        key_columns=("UID", "Name"),
        meta={"select": "bank", "query": "", "user_agent": "acme/1.0"},
        stamp="20260816T040000Z",
    )
    raw = store.raw_dir("finma", "20260816T040000Z")
    raw.mkdir(parents=True)
    (raw / "00001_finma.csv").write_text(
        '"Name";"UID"\n"UBS AG";"CHE-101.329.561"\n', encoding="utf-8"
    )
    (raw / "manifest.jsonl").write_text('{"url": "https://www.finma.ch/…"}\n', encoding="utf-8")
    return reference


def _build(tmp_path, ref, **kwargs):
    return build_bundle(
        tmp_path / "receipt.zip",
        source=get_source("finma"),
        ref=ref,
        rows=ROWS[:1],
        subject={"query": "CHE-101.329.561", "matched_on": "identifier"},
        **kwargs,
    )


class TestContents:
    def test_the_bundle_carries_rows_raw_bytes_and_a_manifest(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        with zipfile.ZipFile(bundle.path) as archive:
            names = set(archive.namelist())
        assert {"receipt.json", "rows.json", "rows.csv", "README.txt", "MANIFEST.json"} <= names
        assert "raw/00001_finma.csv" in names

    def test_the_receipt_records_the_selection_the_snapshot_was_taken_under(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        assert bundle.receipt["snapshot"]["selection"]["select"] == "bank"
        assert bundle.receipt["snapshot"]["body_sha256"] == ref.digest

    def test_the_publishers_disclaimer_travels_with_it(self, tmp_path, ref):
        # A receipt that drops the publisher's own caveat misrepresents it.
        bundle = _build(tmp_path, ref)
        assert bundle.receipt["register"]["publisher_disclaimer"]

    def test_it_says_it_is_not_signed(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        assert bundle.receipt["assurance"]["signed"] is False
        assert bundle.receipt["assurance"]["trusted_timestamp"] is False

    def test_missing_raw_bytes_are_declared_not_omitted_silently(self, tmp_path, ref):
        bundle = _build(tmp_path, ref, include_raw=False)
        assert bundle.receipt["raw_response"]["included"] is False
        assert "omitted by request" in bundle.receipt["raw_response"]["reason"]

    def test_a_snapshot_without_raw_capture_says_so(self, tmp_path, ref):
        for path in sorted(ref.raw_dir.iterdir()):
            path.unlink()
        ref.raw_dir.rmdir()
        bundle = _build(tmp_path, ref)
        assert "never archived" in bundle.receipt["raw_response"]["reason"]


class TestReproducibility:
    def test_the_same_inputs_produce_the_same_bytes(self, tmp_path, ref):
        first = _build(tmp_path, ref)
        first_bytes = first.path.read_bytes()
        first.path.unlink()
        second = _build(tmp_path, ref)
        # A digest that changes on every rebuild proves nothing, so member
        # timestamps come from the snapshot rather than from the clock.
        assert second.path.read_bytes() == first_bytes
        assert second.digest == first.digest

    def test_a_checksum_sidecar_is_written(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        sidecar = bundle.path.with_suffix(bundle.path.suffix + ".sha256")
        assert bundle.digest in sidecar.read_text(encoding="utf-8")


class TestVerification:
    def test_a_fresh_bundle_verifies(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        ok, problems = verify_bundle(bundle.path)
        assert ok and not problems

    def test_a_tampered_member_is_caught(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        tampered = tmp_path / "tampered.zip"
        with zipfile.ZipFile(bundle.path) as source, zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "rows.json":
                    data = json.dumps([{"UID": "CHE-000.000.000", "Name": "Forged AG"}]).encode()
                target.writestr(info, data)
        ok, problems = verify_bundle(tampered)
        assert not ok
        assert any("rows.json" in problem for problem in problems)

    def test_an_added_file_is_caught(self, tmp_path, ref):
        bundle = _build(tmp_path, ref)
        with zipfile.ZipFile(bundle.path, "a") as archive:
            archive.writestr("extra.txt", "smuggled")
        ok, problems = verify_bundle(bundle.path)
        assert not ok
        assert any("not in the manifest" in problem for problem in problems)

    def test_something_that_is_not_a_bundle_is_rejected(self, tmp_path):
        path = tmp_path / "plain.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "hi")
        ok, problems = verify_bundle(path)
        assert not ok and "MANIFEST.json is missing" in problems[0]
