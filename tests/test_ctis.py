"""CTIS: the mandatory searchCriteria, and the 10,000-record paging window."""

from __future__ import annotations

import json

import pytest
import responses

from eufinreg.http import FetchError
from eufinreg.sources.base import Query
from eufinreg.sources.ctis import (
    CTIS,
    MAX_PAGE_SIZE,
    RESULT_WINDOW,
    SEARCH_URL,
    SORT_PROPERTY,
    build_criteria,
    flatten_trial,
)

from .conftest import load_json


def _mock_pages() -> None:
    for n in (1, 2):
        responses.add(responses.POST, SEARCH_URL, json=load_json(f"ctis_page{n}.json"))


def _body(call) -> dict:
    return json.loads(call.request.body)


def _truncated_page(*, returned: int, total: int) -> dict:
    """A page that reports more records than paging can ever reach."""
    page = load_json("ctis_page1.json")
    page["data"] = page["data"][:returned]
    page["pagination"] = {
        "totalRecords": total,
        "currentPage": 1,
        "totalPages": 1,
        "nextPage": False,
        "prevPage": False,
    }
    return page


class TestSearchCriteria:
    def test_empty_query_still_produces_a_criteria_object(self):
        # Sending only `pagination` returns HTTP 200 with totalRecords 0 — a
        # well-formed "no results" for a request that should match everything.
        assert build_criteria(None) == {}
        assert build_criteria("   ") == {}

    def test_plain_text_becomes_the_portals_free_text_search(self):
        assert build_criteria("CAR-T") == {"containAll": "CAR-T"}

    def test_a_json_object_is_passed_through_as_the_whole_criteria(self):
        assert build_criteria('{"containAny": "a b"}') == {"containAny": "a b"}

    def test_malformed_json_is_reported_not_treated_as_free_text(self):
        with pytest.raises(ValueError, match="does not parse"):
            build_criteria('{"containAll": ')

    def test_only_a_leading_brace_switches_to_raw_criteria(self):
        # Bracketed or quoted text stays a free-text search, because searching
        # for "[cancer]" is a reasonable thing to want.
        assert build_criteria('["cancer"]') == {"containAll": '["cancer"]'}

    @responses.activate
    def test_criteria_are_always_sent(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query()))
        assert "searchCriteria" in _body(responses.calls[0])

    @responses.activate
    def test_query_reaches_the_wire(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query(raw_query="cancer")))
        assert _body(responses.calls[0])["searchCriteria"] == {"containAll": "cancer"}


class TestFlattening:
    def test_list_fields_are_pipe_joined(self):
        row = flatten_trial({"trialCountries": ["Spain:11", "France:3"]})
        assert row["trialCountries"] == "Spain:11 | France:3"

    def test_empty_list_entries_are_dropped_from_the_join(self):
        assert flatten_trial({"ageRangeSecondary": [""]})["ageRangeSecondary"] == ""

    def test_nulls_become_empty_strings(self):
        assert flatten_trial({"shortTitle": None})["shortTitle"] == ""

    def test_scalars_are_kept_verbatim(self):
        row = flatten_trial({"ctNumber": "2023-509723-41-00", "ctStatus": 11})
        assert row["ctNumber"] == "2023-509723-41-00"
        assert row["ctStatus"] == 11

    def test_an_unexpected_nested_object_is_kept_as_json(self):
        assert flatten_trial({"extra": {"a": 1}})["extra"] == '{"a": 1}'


class TestRequestShape:
    @responses.activate
    def test_paging_is_one_based(self, fetcher):
        # CTIS pages from 1; starting at 0 silently skips the first page.
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query()))
        assert _body(responses.calls[0])["pagination"]["page"] == 1
        assert _body(responses.calls[1])["pagination"]["page"] == 2

    @responses.activate
    def test_page_size_is_clamped(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query(page_size=5000)))
        assert _body(responses.calls[0])["pagination"]["size"] == MAX_PAGE_SIZE

    @responses.activate
    def test_sort_is_deterministic(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query()))
        assert _body(responses.calls[0])["sort"]["property"] == SORT_PROPERTY

    def test_select_is_rejected_with_a_pointer_to_query(self):
        with pytest.raises(ValueError, match="does not support --select"):
            CTIS.build_body(Query(select="pharma"), page=1, size=10)


class TestPaging:
    @responses.activate
    def test_all_pages_are_followed(self, fetcher):
        _mock_pages()
        rows = list(CTIS.iter_records(fetcher, Query()))
        assert len(rows) == 4
        assert len({row["ctNumber"] for row in rows}) == 4

    @responses.activate
    def test_paging_stops_when_next_page_is_false(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query()))
        assert len(responses.calls) == 2

    @responses.activate
    def test_max_docs_stops_early(self, fetcher):
        _mock_pages()
        assert len(list(CTIS.iter_records(fetcher, Query(max_docs=1)))) == 1
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_non_object_response_is_reported(self, fetcher):
        responses.add(responses.POST, SEARCH_URL, json=["nope"])
        with pytest.raises(FetchError, match="not a result object"):
            list(CTIS.iter_records(fetcher, Query()))


class TestTruncationWarning:
    @responses.activate
    def test_a_complete_result_set_warns_about_nothing(self, fetcher):
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query()))
        assert CTIS.warnings() == []

    @responses.activate
    def test_the_paging_window_shortfall_is_reported(self, fetcher):
        # The live register: 12,229 matches, 10,000 reachable, no error raised.
        responses.add(responses.POST, SEARCH_URL, json=_truncated_page(returned=2, total=12229))
        rows = list(CTIS.iter_records(fetcher, Query()))
        assert len(rows) == 2
        warning = " ".join(CTIS.warnings())
        assert "2 of 12229" in warning
        assert f"{RESULT_WINDOW:,}" in warning

    @responses.activate
    def test_max_docs_is_not_mistaken_for_truncation(self, fetcher):
        # The user asked for fewer rows; that is not the register hiding any.
        _mock_pages()
        list(CTIS.iter_records(fetcher, Query(max_docs=1)))
        assert CTIS.warnings() == []

    @responses.activate
    def test_value_counts_carry_the_shortfall_note(self, fetcher):
        responses.add(responses.POST, SEARCH_URL, json=_truncated_page(returned=2, total=12229))
        _, note = CTIS.count_values(fetcher, Query(), "sponsorType")
        assert "only 2 of 12229" in (note or "")


class TestCountValues:
    @responses.activate
    def test_counts_cover_every_fetched_record(self, fetcher):
        _mock_pages()
        pairs, note = CTIS.count_values(fetcher, Query(), "sponsorType")
        assert sum(count for _, count in pairs) == 4
        assert "not a sample" in (note or "")

    @responses.activate
    def test_country_cells_are_split(self, fetcher):
        _mock_pages()
        pairs, note = CTIS.count_values(fetcher, Query(), "trialCountries")
        assert all(" | " not in value for value, _ in pairs)
        assert "split on" in (note or "")

    @responses.activate
    def test_the_search_is_only_run_once_per_query(self, fetcher):
        _mock_pages()
        CTIS.count_values(fetcher, Query(), "sponsorType")
        CTIS.count_values(fetcher, Query(), "trialPhase")
        assert len(responses.calls) == 2  # the two pages, fetched once
