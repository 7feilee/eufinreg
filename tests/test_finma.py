"""FINMA: one row per authorisation, collapsed into one row per institution."""

from __future__ import annotations

import pytest
import responses

from eufinreg.flatten import flatten_auto
from eufinreg.sources.base import Query
from eufinreg.sources.finma import (
    FINMA,
    UID_CSV_URL,
    entity_key,
    parse_csv,
    resolve_select,
    uid_digits,
)

from .conftest import load_text


def _mock(text: str | None = None) -> None:
    responses.add(
        responses.GET,
        UID_CSV_URL,
        body=text if text is not None else load_text("finma_uid.csv"),
        content_type="text/csv",
    )


def _rows(fetcher, query: Query | None = None) -> list[dict]:
    records = list(FINMA.iter_records(fetcher, query or Query()))
    return flatten_auto(records, FINMA.flatten_config(), block_structured=True)


class TestUrl:
    @responses.activate
    def test_the_sitecore_hash_is_not_sent(self, fetcher):
        # FINMA's own links carry ?sc_lang=en&hash=…; the bare path serves the
        # same file, which is what keeps this source out of the business of
        # scraping a page for a hash that changes when the data does.
        _mock()
        _rows(fetcher)
        assert responses.calls[0].request.url == UID_CSV_URL


class TestParsing:
    def test_the_file_is_semicolon_separated(self):
        rows = parse_csv(load_text("finma_uid.csv"))
        assert rows[0]["Name"]
        assert "AuthorisationTypeEN" in rows[0]

    def test_a_comma_reader_would_see_one_column(self):
        # The fixture is real: comma-splitting it produces a single column whose
        # name is the whole header. That is the failure this delimiter prevents.
        rows = parse_csv(load_text("finma_uid.csv"), delimiter=",")
        assert len(rows[0]) == 1

    def test_uid_digits_strips_the_punctuation(self):
        assert uid_digits("CHE-101.329.561") == "101329561"
        assert uid_digits("") == ""


class TestEntityGrouping:
    @responses.activate
    def test_one_row_per_institution_not_per_authorisation(self, fetcher):
        _mock()
        rows = _rows(fetcher)
        assert len(rows) < len(parse_csv(load_text("finma_uid.csv")))
        assert len({row["UID"] or row["Name"] for row in rows}) == len(rows)

    @responses.activate
    def test_several_authorisations_collapse_into_one_cell(self, fetcher):
        _mock()
        multi = [r for r in _rows(fetcher) if int(r["authorisation_count"]) > 1]
        assert multi, "the fixture should contain an institution with two authorisations"
        assert " | " in multi[0]["authorisation_AuthorisationTypeEN"]

    def test_a_row_without_a_uid_keeps_its_own_identity(self):
        # 84 of 2,938 live rows have no UID. Keying on the empty string would
        # collapse all of them into one institution.
        a = entity_key({"Name": "Alpha SA", "City": "Genève", "UID": ""})
        b = entity_key({"Name": "Beta SA", "City": "Genève", "UID": ""})
        assert a != b

    @responses.activate
    def test_synthetic_markers_do_not_reach_the_output(self, fetcher):
        _mock()
        row = _rows(fetcher)[0]
        assert not {"id", "type_s", "entity_type", "_root_"} & set(row)


class TestSelect:
    def test_aliases_and_free_substrings_both_work(self):
        assert resolve_select("bank") == ["bank"]
        assert resolve_select("pm") == ["portfolio manager"]
        assert resolve_select("custodian") == ["custodian"]

    def test_all_and_empty_mean_no_filter(self):
        assert resolve_select("ALL") == []
        assert resolve_select(None) == []

    @responses.activate
    def test_select_narrows_the_result(self, fetcher):
        _mock()
        everything = _rows(fetcher)
        FINMA.reset()
        responses.reset()
        _mock()
        venues = _rows(fetcher, Query(select="trading-venue"))
        assert 0 < len(venues) < len(everything)
        assert all(
            "trading venue" in row["authorisation_AuthorisationTypeEN"].lower() for row in venues
        )

    @responses.activate
    def test_a_raw_query_is_refused_with_a_pointer(self, fetcher):
        _mock()
        with pytest.raises(ValueError, match="no server-side query"):
            _rows(fetcher, Query(raw_query="Name=UBS"))


class TestCountValues:
    @responses.activate
    def test_counts_are_per_authorisation(self, fetcher):
        _mock()
        pairs, note = FINMA.count_values(fetcher, Query(), "authorisation_AuthorisationTypeEN")
        assert pairs
        assert "one authorisation" in (note or "")

    @responses.activate
    def test_the_raw_column_name_also_works(self, fetcher):
        _mock()
        pairs, _ = FINMA.count_values(fetcher, Query(), "AuthorisationTypeEN")
        assert pairs

    @responses.activate
    def test_the_file_is_downloaded_once_per_query(self, fetcher):
        _mock()
        FINMA.count_values(fetcher, Query(), "City")
        FINMA.count_values(fetcher, Query(), "AuthorisationTypeEN")
        assert len(responses.calls) == 1
