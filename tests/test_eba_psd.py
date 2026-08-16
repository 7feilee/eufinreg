"""EBA PSD2 register: checksum discipline, the odd document model, selection."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
import responses

from eufinreg.http import FetchError
from eufinreg.sources.base import Query
from eufinreg.sources.eba_psd import (
    FILE_METADATA_URL,
    INSTITUTION_TYPES,
    METADATA_URL,
    PSD,
    _entity_version_iso,
    _properties,
    _services,
    extract_records,
    parse_selection,
)

from .conftest import load_bytes, load_json

ZIP_URL = (
    "https://euclid.eba.europa.eu/register/downloads/PSDMD/29990101/download-PSDMD-209901010000.zip"
)


def _register(*, archive: bytes | None = None, meta: dict | None = None) -> None:
    """Wire up the three requests a full run makes."""
    responses.add(
        responses.GET,
        FILE_METADATA_URL,
        json=meta if meta is not None else load_json("eba_filemetadata.json"),
        status=200,
    )
    responses.add(
        responses.GET,
        ZIP_URL,
        body=archive if archive is not None else load_bytes("eba_psd_goldencopy.zip"),
        status=200,
        content_type="application/zip",
    )
    responses.add(
        responses.GET,
        METADATA_URL,
        json=load_json("eba_metadata.json"),
        status=200,
    )


def _rebuild_zip(records: list[dict], *, sidecar: str | None = None) -> tuple[bytes, dict]:
    """A golden copy carrying ``records``, plus matching file metadata."""
    member = "download-PSDMD-209901010000.json"
    body = json.dumps([[{"__EBA_Disclaimer": {}}], records], ensure_ascii=False).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(member, body)
        bundle.writestr(
            f"{member}.sha256",
            sidecar if sidecar is not None else hashlib.sha256(body).hexdigest(),
        )
    archive = buffer.getvalue()
    meta = dict(load_json("eba_filemetadata.json"))
    meta["sha256_hash"] = hashlib.sha256(archive).hexdigest()
    return archive, meta


class TestDocumentModel:
    def test_properties_are_merged_out_of_single_key_objects(self):
        record = {"Properties": [{"ENT_NAM": "Acme"}, {"ENT_COU_RES": "DE"}]}
        assert _properties(record) == {"ENT_NAM": "Acme", "ENT_COU_RES": "DE"}

    def test_empty_property_objects_are_skipped(self):
        # The search API serves the same records with padding objects in place.
        record = {"Properties": [{}, {"ENT_NAM": "Acme"}, {}]}
        assert _properties(record) == {"ENT_NAM": "Acme"}

    def test_missing_blocks_are_not_an_error(self):
        assert _properties({}) == {}
        assert _services({}) == {}

    def test_services_accept_both_a_list_and_a_bare_string(self):
        record = {"Services": [{"DE": ["PS_010", "PS_020"]}, {"FR": "PS_060"}]}
        assert _services(record) == {"DE": ["PS_010", "PS_020"], "FR": ["PS_060"]}

    def test_services_deduplicate_within_a_country(self):
        record = {"Services": [{"DE": ["PS_010", "PS_010"]}]}
        assert _services(record) == {"DE": ["PS_010"]}

    def test_entity_version_becomes_a_timestamp(self):
        assert _entity_version_iso("20260814223344848") == "2026-08-14T22:33:44Z"

    def test_unexpected_version_shapes_pass_through_untouched(self):
        assert _entity_version_iso("not-a-stamp") == "not-a-stamp"
        assert _entity_version_iso(None) == ""


class TestExtractRecords:
    def test_finds_the_entity_array_inside_the_nested_wrapper(self):
        payload = [[{"__EBA_Disclaimer": {}}], [{"EntityCode": "X", "EntityType": "PSD_PI"}]]
        assert extract_records(payload) == [{"EntityCode": "X", "EntityType": "PSD_PI"}]

    def test_picks_the_longest_candidate_array(self):
        payload = [
            [{"EntityCode": "A", "EntityType": "PSD_PI"}],
            [
                {"EntityCode": "B", "EntityType": "PSD_PI"},
                {"EntityCode": "C", "EntityType": "PSD_EMI"},
            ],
        ]
        assert [r["EntityCode"] for r in extract_records(payload)] == ["B", "C"]

    def test_a_document_without_entities_yields_nothing(self):
        assert extract_records([[{"__EBA_Disclaimer": {}}]]) == []


class TestSelection:
    def test_default_excludes_agents_and_branches(self):
        assert parse_selection(None) == INSTITUTION_TYPES
        assert "PSD_AG" not in parse_selection(None)

    def test_all_means_no_filter(self):
        assert parse_selection("ALL") is None
        assert parse_selection("all") is None

    def test_bare_suffixes_and_full_codes_are_equivalent(self):
        assert parse_selection("EMI") == parse_selection("PSD_EMI") == ("PSD_EMI",)

    def test_codes_can_be_combined(self):
        assert parse_selection("PI,EMI") == ("PSD_PI", "PSD_EMI")

    def test_all_wins_over_a_narrower_code_in_the_same_list(self):
        assert parse_selection("PI,ALL") is None

    def test_blank_falls_back_to_the_default(self):
        assert parse_selection("  ") == INSTITUTION_TYPES


class TestChecksums:
    @responses.activate
    def test_a_matching_archive_is_accepted(self, fetcher):
        _register()
        assert list(PSD.iter_records(fetcher, Query(select="ALL")))

    @responses.activate
    def test_a_corrupt_archive_is_rejected_by_its_advertised_hash(self, fetcher):
        meta = dict(load_json("eba_filemetadata.json"))
        meta["sha256_hash"] = "0" * 64
        _register(meta=meta)
        with pytest.raises(FetchError, match="SHA-256"):
            list(PSD.iter_records(fetcher, Query()))

    @responses.activate
    def test_the_inner_sidecar_is_checked_too(self, fetcher):
        archive, meta = _rebuild_zip(
            [{"EntityCode": "X", "EntityType": "PSD_PI", "Properties": []}],
            sidecar="0" * 64,
        )
        _register(archive=archive, meta=meta)
        with pytest.raises(FetchError, match="SHA-256"):
            list(PSD.iter_records(fetcher, Query(select="ALL")))

    @responses.activate
    def test_a_hashless_file_metadata_is_not_fatal(self, fetcher):
        # Nothing forces the EBA to keep publishing the hash; losing it should
        # cost the verification, not the download.
        meta = dict(load_json("eba_filemetadata.json"))
        meta["sha256_hash"] = ""
        _register(meta=meta)
        assert list(PSD.iter_records(fetcher, Query(select="ALL")))

    @responses.activate
    def test_a_body_that_is_not_a_zip_fails_clearly(self, fetcher):
        meta = dict(load_json("eba_filemetadata.json"))
        meta["sha256_hash"] = hashlib.sha256(b"<html>maintenance</html>").hexdigest()
        _register(archive=b"<html>maintenance</html>", meta=meta)
        with pytest.raises(FetchError, match="not a readable ZIP"):
            list(PSD.iter_records(fetcher, Query()))


class TestDownloadUrl:
    def test_url_is_built_from_the_registers_own_metadata(self):
        assert PSD.download_url(load_json("eba_filemetadata.json")) == ZIP_URL

    def test_a_metadata_document_naming_no_file_is_an_error(self):
        with pytest.raises(FetchError, match="names no download"):
            PSD.download_url({"golden_copy_path_context": "https://example.invalid/"})


class TestFlattening:
    @responses.activate
    def test_one_row_per_entity_with_the_registers_own_field_names(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL")))
        assert len(rows) == 8
        row = next(r for r in rows if r["EntityType"] == "PSD_PI" and r["CA_OwnerID"] == "DE_BAFIN")
        assert row["ENT_NAM"] == "Boerse Stuttgart Digital Custody GmbH"
        assert row["ENT_COU_RES"] == "DE"
        assert row["EntityCode"].startswith("DE_BAFIN!")

    @responses.activate
    def test_multi_valued_properties_are_pipe_joined_not_dropped(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL")))
        # One Bulgarian firm is filed under both a Cyrillic and a Latin name.
        multi = next(r for r in rows if " | " in str(r.get("ENT_NAM", "")))
        assert "Octis Pay OOD" in multi["ENT_NAM"]

    @responses.activate
    def test_passporting_is_preserved_in_three_shapes(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL")))
        row = next(r for r in rows if r["ENT_NAM"] == "Boerse Stuttgart Digital Custody GmbH")
        countries = row["ENT_SER_COU"].split(" | ")
        assert "DE" in countries
        assert row["ENT_SER_COU_count"] == str(len(countries))
        # The per-country detail survives, so a union cannot hide an asymmetry.
        assert f"{countries[0]}=" in row["ENT_SER_BY_COU"]
        assert set(row["ENT_SER"].split(" | ")) <= {
            code
            for part in row["ENT_SER_BY_COU"].split(" | ")
            for code in part.split("=")[1].split(",")
        }

    @responses.activate
    def test_records_without_services_still_produce_the_columns(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="EXC")))
        assert rows
        assert rows[0]["ENT_SER"] == ""
        assert rows[0]["ENT_SER_COU_count"] == "0"


class TestDerivedLabels:
    @responses.activate
    def test_labels_come_from_the_registers_metadata_endpoint(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL")))
        row = next(r for r in rows if r["CA_OwnerID"] == "DE_BAFIN")
        assert row["CA_OwnerName"] == "Federal Financial Supervisory Authority"
        assert row["EntityTypeLabel"] in ("Payments institution", "Agent")
        assert row["__EBA_EntityVersion_iso"].endswith("Z")

    @responses.activate
    def test_service_codes_are_labelled(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL")))
        row = next(r for r in rows if r["ENT_SER"])
        assert row["ENT_SER_label"]
        assert len(row["ENT_SER_label"].split(" | ")) == len(row["ENT_SER"].split(" | "))

    @responses.activate
    def test_no_derived_skips_the_labels_and_the_extra_request(self, fetcher):
        _register()
        rows = list(PSD.iter_records(fetcher, Query(select="ALL", extra={"no_derived": True})))
        assert "CA_OwnerName" not in rows[0]
        assert "EntityTypeLabel" not in rows[0]
        # …but never at the cost of the passporting columns.
        assert "ENT_SER_COU" in rows[0]
        assert METADATA_URL not in [call.request.url for call in responses.calls]

    @responses.activate
    def test_an_unknown_code_yields_an_empty_label_not_a_crash(self, fetcher):
        archive, meta = _rebuild_zip(
            [
                {
                    "CA_OwnerID": "ZZ_NEW",
                    "EntityCode": "ZZ_NEW!1",
                    "EntityType": "PSD_FUTURE",
                    "Properties": [{"ENT_NAM": "Tomorrow Ltd"}],
                    "__EBA_EntityVersion": "20260101000000000",
                }
            ]
        )
        _register(archive=archive, meta=meta)
        row = next(iter(PSD.iter_records(fetcher, Query(select="ALL"))))
        assert row["ENT_NAM"] == "Tomorrow Ltd"
        assert row["EntityTypeLabel"] == ""
        assert row["CA_OwnerName"] == ""


class TestSourceBehaviour:
    @responses.activate
    def test_the_default_selection_leaves_the_agents_out(self, fetcher):
        _register()
        types = {row["EntityType"] for row in PSD.iter_records(fetcher, Query())}
        assert "PSD_AG" not in types
        assert "PSD_BR" not in types
        assert "PSD_PI" in types

    @responses.activate
    def test_max_docs_truncates(self, fetcher):
        _register()
        assert len(list(PSD.iter_records(fetcher, Query(select="ALL", max_docs=3)))) == 3

    @responses.activate
    def test_the_archive_is_downloaded_once_per_selection(self, fetcher):
        _register()
        list(PSD.iter_records(fetcher, Query()))
        PSD.count_values(fetcher, Query(), "EntityType")
        PSD.count_values(fetcher, Query(), "CA_OwnerID")
        assert [call.request.url for call in responses.calls].count(ZIP_URL) == 1

    @responses.activate
    def test_counts_split_the_multi_valued_service_column(self, fetcher):
        _register()
        pairs, note = PSD.count_values(fetcher, Query(select="ALL"), "ENT_SER")
        # Whole-cell counting would bucket each distinct combination separately.
        assert all(" | " not in value for value, _ in pairs)
        assert "not a sample" in (note or "")

    @responses.activate
    def test_counts_report_which_slice_they_cover(self, fetcher):
        _register()
        _, note = PSD.count_values(fetcher, Query(), "EntityType")
        assert "PSD_PI" in (note or "")

    @responses.activate
    def test_an_empty_register_fails_loudly_rather_than_writing_zero_rows(self, fetcher):
        archive, meta = _rebuild_zip([])
        _register(archive=archive, meta=meta)
        with pytest.raises(FetchError, match="no entity records"):
            list(PSD.iter_records(fetcher, Query()))

    @responses.activate
    def test_file_metadata_that_is_not_an_object_is_reported(self, fetcher):
        responses.add(responses.GET, FILE_METADATA_URL, json=["unexpected"], status=200)
        with pytest.raises(FetchError, match="describing the current download"):
            list(PSD.iter_records(fetcher, Query()))
