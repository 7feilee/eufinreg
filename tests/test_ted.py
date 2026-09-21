"""TED: alpha-3 country codes, multilingual fields, and winners repeated per lot."""

from __future__ import annotations

import json

import pytest
import responses

from eufinreg.sources.base import Query
from eufinreg.sources.ted import (
    MAX_LIMIT,
    SEARCH_URL,
    TED,
    build_query,
    flatten_notice,
    pick_language,
    resolve_countries,
)

from .conftest import load_json


def _mock_pages(pages: int = 2, total: int = 6) -> None:
    for n in range(1, pages + 1):
        payload = load_json(f"ted_page{n}.json")
        payload["totalNoticeCount"] = total
        responses.add(responses.POST, SEARCH_URL, json=payload)


def _body(call) -> dict:
    return json.loads(call.request.body)


class TestQueryBuilding:
    def test_dach_is_germany_and_austria_only(self):
        # TED covers the EU and EEA. Switzerland is in neither, and pretending
        # otherwise would quietly return a two-thirds answer.
        assert resolve_countries("dach") == ["DEU", "AUT"]

    def test_two_letter_codes_are_converted(self):
        # Every other source here uses alpha-2; TED uses alpha-3, and the wrong
        # one returns zero notices with no error.
        assert resolve_countries("de,at") == ["DEU", "AUT"]

    def test_alpha_three_passes_through(self):
        assert resolve_countries("DEU") == ["DEU"]

    def test_an_unknown_code_is_refused(self):
        with pytest.raises(ValueError, match="alpha-3"):
            resolve_countries("germany")

    def test_select_and_query_are_anded(self):
        query = build_query("dach", "notice-type IN (can-standard)")
        assert query == "buyer-country IN (DEU AUT) AND (notice-type IN (can-standard))"

    def test_an_empty_request_is_refused_rather_than_fetching_everything(self):
        with pytest.raises(ValueError, match="ted needs a query"):
            build_query(None, None)


class TestMultilingualFields:
    def test_english_is_preferred(self):
        assert (
            pick_language({"deu": ["Polizei Berlin"], "eng": ["Berlin Police"]}) == "Berlin Police"
        )

    def test_german_is_next(self):
        assert pick_language({"deu": ["Polizei Berlin"], "fra": ["Police"]}) == "Polizei Berlin"

    def test_an_unexpected_language_is_still_returned(self):
        # Better one value in Maltese than an empty column.
        assert pick_language({"mlt": ["Xi ħaġa"]}) == "Xi ħaġa"

    def test_an_empty_block_is_an_empty_string(self):
        assert pick_language({}) == ""


class TestFlattening:
    def test_winners_are_deduplicated_and_counted(self):
        # A notice awarding six lots to three companies lists six names.
        row = flatten_notice(
            {
                "publication-number": "1-2026",
                "winner-name": {"deu": ["A GmbH", "B AG", "A GmbH", "C SA", "B AG", "B AG"]},
            }
        )
        assert row["winner-name"] == "A GmbH | B AG | C SA"
        assert row["winner_count"] == 3

    def test_the_link_block_is_dropped(self):
        # ~2 KB of PDF URLs in 24 languages, per notice.
        row = flatten_notice({"publication-number": "1", "links": {"pdf": {"DEU": "http://x"}}})
        assert "links" not in row

    def test_a_real_notice_keeps_ted_field_names(self):
        notice = load_json("ted_page1.json")["notices"][0]
        row = flatten_notice(notice)
        assert row["publication-number"]
        assert row["buyer-country"]


class TestRequests:
    @responses.activate
    def test_the_query_reaches_the_wire(self, fetcher):
        _mock_pages()
        list(TED.iter_records(fetcher, Query(select="dach")))
        assert "buyer-country IN (DEU AUT)" in _body(responses.calls[0])["query"]

    @responses.activate
    def test_limit_is_clamped_to_the_documented_cap(self, fetcher):
        _mock_pages()
        list(TED.iter_records(fetcher, Query(select="dach", page_size=1000)))
        assert _body(responses.calls[0])["limit"] == MAX_LIMIT

    @responses.activate
    def test_paging_is_page_number_based_and_one_indexed(self, fetcher):
        _mock_pages(pages=2, total=6)
        list(TED.iter_records(fetcher, Query(select="dach", page_size=3)))
        assert _body(responses.calls[0])["page"] == 1
        assert _body(responses.calls[0])["paginationMode"] == "PAGE_NUMBER"
        assert _body(responses.calls[1])["page"] == 2

    @responses.activate
    def test_all_notices_are_collected(self, fetcher):
        _mock_pages(pages=2, total=6)
        rows = list(TED.iter_records(fetcher, Query(select="dach", page_size=3)))
        assert len(rows) == 6
        assert len({row["publication-number"] for row in rows}) == 6

    @responses.activate
    def test_a_rejected_query_is_reported_with_teds_own_message(self, fetcher):
        from eufinreg.http import FetchError

        responses.add(
            responses.POST,
            SEARCH_URL,
            json={
                "message": "Value (300) of parameter 'limit' exceeds maximum allowed value (250)"
            },
        )
        with pytest.raises(FetchError, match="exceeds maximum allowed"):
            list(TED.iter_records(fetcher, Query(select="dach")))

    @responses.activate
    def test_an_incomplete_pull_is_reported(self, fetcher):
        # TED stopping short of its own totalNoticeCount is the case that must
        # not pass silently: the run looks clean and the tail is missing.
        _mock_pages(pages=1, total=50_000)
        responses.add(responses.POST, SEARCH_URL, json={"notices": [], "totalNoticeCount": 50_000})
        list(TED.iter_records(fetcher, Query(select="dach", page_size=3)))
        warning = " ".join(TED.warnings())
        assert "50,000" in warning and "publication-date" in warning


class TestCountValues:
    @responses.activate
    def test_cpv_codes_are_split(self, fetcher):
        _mock_pages(pages=2, total=6)
        pairs, note = TED.count_values(
            fetcher, Query(select="dach", page_size=3), "classification-cpv"
        )
        assert all(" | " not in value for value, _ in pairs)
        assert "split on" in (note or "")
