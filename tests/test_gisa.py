"""GISA: the /csv path that returns 7-Zip, and filtering during the parse."""

from __future__ import annotations

import pytest
import responses

from eufinreg.sources.base import Query
from eufinreg.sources.gisa import (
    CODES,
    GEWERBE,
    SEVENZIP_MAGIC,
    vintage_from_disposition,
)

from .conftest import DATA


def load_bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _mock_licences(*, compressed: bool = True, vintage: str = "2026.08") -> None:
    suffix = "csv.7z" if compressed else "csv"
    responses.add(
        responses.GET,
        GEWERBE.url,
        body=load_bytes(f"gisa_stat03.{suffix}"),
        # Verified live, doubled prefix and all. The source must not trust it.
        content_type="application/application/x-7z-compressed" if compressed else "text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename=OgdAufrechteGewerbeberechtigung_{vintage}.{suffix}"
            )
        },
    )


def _mock_codes() -> None:
    responses.add(
        responses.GET,
        CODES.url,
        body=load_bytes("gisa_stat02.csv"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=OgdGewerbeschluessel_2026.08.csv"},
    )


class TestUrls:
    def test_the_licence_dataset_is_stat03(self):
        assert GEWERBE.url.endswith("/ogd/stat03/csv")

    def test_the_code_list_is_stat02(self):
        assert CODES.url.endswith("/ogd/stat02/csv")


class TestVintage:
    def test_the_vintage_comes_from_the_content_disposition(self):
        # It is the only place the data's month is stated: no URL parameter, no
        # column, no header.
        assert (
            vintage_from_disposition(
                "attachment; filename=OgdAufrechteGewerbeberechtigung_2026.08.csv.7z"
            )
            == "2026.08"
        )

    def test_a_missing_header_is_not_an_error(self):
        assert vintage_from_disposition(None) == ""

    @responses.activate
    def test_the_vintage_is_stamped_on_every_row(self, fetcher):
        _mock_codes()
        rows = list(CODES.iter_records(fetcher, Query()))
        assert all(row["_vintage"] == "2026.08" for row in rows)


class TestSevenZip:
    def test_the_fixture_really_is_an_archive(self):
        assert load_bytes("gisa_stat03.csv.7z").startswith(SEVENZIP_MAGIC)

    @responses.activate
    def test_the_archive_is_decompressed(self, fetcher):
        pytest.importorskip("py7zr")
        _mock_licences(compressed=True)
        rows = list(GEWERBE.iter_records(fetcher, Query()))
        assert rows and rows[0]["gewerbewortlaut"]

    @responses.activate
    def test_plain_csv_at_the_same_url_still_works(self, fetcher):
        # Magic bytes decide, not the path and not the Content-Type — so the day
        # the ministry stops compressing, nothing here breaks.
        _mock_licences(compressed=False)
        rows = list(GEWERBE.iter_records(fetcher, Query()))
        assert rows and rows[0]["nuts2"] == "AT11"


class TestParsing:
    @responses.activate
    def test_columns_are_lower_case_as_served(self, fetcher):
        # data.gv.at documents them upper case; the file disagrees.
        _mock_licences(compressed=False)
        row = next(iter(GEWERBE.iter_records(fetcher, Query())))
        assert "gewerbeart" in row
        assert "GEWERBEART" not in row

    @responses.activate
    def test_codes_are_labelled_from_the_publishers_own_list(self, fetcher):
        _mock_licences(compressed=False)
        rows = list(GEWERBE.iter_records(fetcher, Query()))
        licensed = [r for r in rows if r["gewerbeart"] == "5"]
        assert licensed and licensed[0]["gewerbeart_label"] == "konzessioniertes Gewerbe"
        assert rows[0]["nuts2_label"] == "Burgenland"

    @responses.activate
    def test_no_derived_leaves_the_file_as_published(self, fetcher):
        _mock_licences(compressed=False)
        row = next(iter(GEWERBE.iter_records(fetcher, Query(extra={"no_derived": True}))))
        assert "gewerbeart_label" not in row
        assert "_vintage" not in row

    @responses.activate
    def test_the_code_list_carries_labels_not_codes_in_the_same_column(self, fetcher):
        # gewerbeart is a numeric code in stat03 and a German label in stat02.
        # Joining the two datasets on the column name yields nothing.
        _mock_codes()
        row = next(iter(CODES.iter_records(fetcher, Query())))
        assert row["gewerbeart"] == "freies Gewerbe"
        assert "gewerbeart_label" not in row


class TestFilteringDuringParse:
    @responses.activate
    def test_select_maps_an_alias_onto_the_code(self, fetcher):
        _mock_licences(compressed=False)
        rows = list(GEWERBE.iter_records(fetcher, Query(select="konzessioniert")))
        assert rows and all(row["gewerbeart"] == "5" for row in rows)

    @responses.activate
    def test_query_filters_on_any_column(self, fetcher):
        _mock_licences(compressed=False)
        rows = list(GEWERBE.iter_records(fetcher, Query(raw_query="nuts2=AT13")))
        assert rows and all(row["nuts2"] == "AT13" for row in rows)

    @responses.activate
    def test_the_kept_and_scanned_counts_are_reported(self, fetcher):
        _mock_licences(compressed=False)
        list(GEWERBE.iter_records(fetcher, Query(raw_query="nuts2=AT13")))
        warning = " ".join(GEWERBE.warnings())
        assert "kept" in warning and "while parsing" in warning

    @responses.activate
    def test_an_unfiltered_read_warns_about_nothing(self, fetcher):
        _mock_licences(compressed=False)
        list(GEWERBE.iter_records(fetcher, Query()))
        assert GEWERBE.warnings() == []

    @responses.activate
    def test_the_documented_upper_case_spelling_also_filters(self, fetcher):
        # data.gv.at documents NUTS2; the file serves nuts2. Matching case
        # sensitively would return zero rows and look like a broken endpoint.
        _mock_licences(compressed=False)
        rows = list(GEWERBE.iter_records(fetcher, Query(raw_query="NUTS2=AT13")))
        assert rows and all(row["nuts2"] == "AT13" for row in rows)

    @responses.activate
    def test_a_column_that_does_not_exist_is_an_error_not_an_empty_result(self, fetcher):
        _mock_licences(compressed=False)
        with pytest.raises(ValueError, match="no such column"):
            list(GEWERBE.iter_records(fetcher, Query(raw_query="bundesland=Wien")))

    @responses.activate
    def test_an_unknown_select_names_the_valid_values(self, fetcher):
        _mock_licences(compressed=False)
        with pytest.raises(ValueError, match="unknown gewerbeart"):
            list(GEWERBE.iter_records(fetcher, Query(select="lizenziert")))

    def test_the_code_list_rejects_select(self, fetcher):
        with pytest.raises(ValueError, match="does not support --select"):
            CODES.build_filters(Query(select="frei"))


class TestCountValues:
    @responses.activate
    def test_counts_carry_the_vintage(self, fetcher):
        _mock_licences(compressed=False)
        pairs, note = GEWERBE.count_values(fetcher, Query(), "gewerbeart_label")
        assert pairs
        assert "2026.08" in (note or "")

    @responses.activate
    def test_the_file_is_downloaded_once_per_filter(self, fetcher):
        _mock_licences(compressed=False)
        GEWERBE.count_values(fetcher, Query(), "nuts2")
        GEWERBE.count_values(fetcher, Query(), "gewerbeart")
        assert len(responses.calls) == 1
