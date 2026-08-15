"""ESMA Solr source: query construction, cursorMark paging, faceting."""

from __future__ import annotations

import responses

from eufinreg.flatten import flatten_auto
from eufinreg.sources import get_source
from eufinreg.sources.base import Query
from eufinreg.sources.esma_solr import UPREG, solr_source_for_core

from .conftest import load_json

URL = "https://registers.esma.europa.eu/solr/esma_registers_upreg/select"


class TestQueryConstruction:
    def test_default_is_match_all(self):
        assert UPREG.build_q(Query()) == "*:*"

    def test_select_becomes_a_block_join(self):
        # Selecting on a parent field while still retrieving children needs the
        # join; this is the pattern ESMA's own examples use.
        assert UPREG.build_q(Query(select="CSP")) == (
            "{!join from=id to=_root_}ae_entityTypeCode:CSP"
        )

    def test_raw_query_is_passed_through_untouched(self):
        q = "ae_homeMemberState:austria"
        assert UPREG.build_q(Query(raw_query=q)) == q

    def test_raw_query_wins_over_select(self):
        assert UPREG.build_q(Query(select="CSP", raw_query="*:*")) == "*:*"

    def test_values_needing_quoting_are_quoted(self):
        assert UPREG.build_q(Query(select="two words")) == (
            '{!join from=id to=_root_}ae_entityTypeCode:"two words"'
        )

    def test_default_filter_excludes_history(self):
        assert UPREG.build_fq(Query()) == ["entity_type:(ae OR aeActivity OR aeNotHostMmbSt)"]

    def test_include_history_adds_the_history_doc_type(self):
        fq = UPREG.build_fq(Query(include_history=True))
        assert fq == ["entity_type:(ae OR aeActivity OR aeNotHostMmbSt OR aeActivityHistory)"]

    def test_flat_cores_get_no_doc_type_filter(self):
        core = solr_source_for_core("esma_registers_mmf04")
        assert core.build_fq(Query()) == []
        assert core.build_q(Query(select="ignored")) == "*:*"


class TestPaging:
    @responses.activate
    def test_cursor_mark_walks_every_page(self, fetcher, upreg_pages):
        for page in upreg_pages:
            responses.add(responses.GET, URL, json=page, status=200)
        docs = list(UPREG.iter_records(fetcher, Query(page_size=4)))
        assert len(docs) == 8
        assert len(responses.calls) == 3

    @responses.activate
    def test_cursor_is_echoed_back_on_the_next_request(self, fetcher, upreg_pages):
        for page in upreg_pages:
            responses.add(responses.GET, URL, json=page, status=200)
        list(UPREG.iter_records(fetcher, Query(page_size=4)))
        assert "cursorMark=%2A" in responses.calls[0].request.url  # '*'
        assert "cursorMark=CURSOR2" in responses.calls[1].request.url

    @responses.activate
    def test_paging_stops_when_the_cursor_stops_moving(self, fetcher):
        page = {"response": {"numFound": 1, "docs": [{"id": "a"}]}, "nextCursorMark": "SAME"}
        responses.add(responses.GET, URL, json=page, status=200)
        responses.add(responses.GET, URL, json={**page, "nextCursorMark": "SAME"}, status=200)
        docs = list(UPREG.iter_records(fetcher, Query(page_size=10)))
        # First page returns the doc, second page repeats the cursor -> stop.
        assert len(docs) == 2
        assert len(responses.calls) == 2

    @responses.activate
    def test_missing_next_cursor_stops_cleanly(self, fetcher):
        responses.add(responses.GET, URL, json={"response": {"docs": [{"id": "a"}]}}, status=200)
        assert len(list(UPREG.iter_records(fetcher, Query()))) == 1

    @responses.activate
    def test_empty_response_is_not_an_error(self, fetcher):
        responses.add(
            responses.GET, URL, json={"response": {"numFound": 0, "docs": []}}, status=200
        )
        assert list(UPREG.iter_records(fetcher, Query())) == []

    @responses.activate
    def test_a_response_missing_the_response_key_does_not_explode(self, fetcher):
        responses.add(responses.GET, URL, json={"responseHeader": {"status": 0}}, status=200)
        assert list(UPREG.iter_records(fetcher, Query())) == []

    @responses.activate
    def test_max_docs_stops_early(self, fetcher, upreg_pages):
        for page in upreg_pages:
            responses.add(responses.GET, URL, json=page, status=200)
        docs = list(UPREG.iter_records(fetcher, Query(page_size=4, max_docs=3)))
        assert len(docs) == 3
        assert len(responses.calls) == 1

    @responses.activate
    def test_block_cores_sort_by_root_so_entities_stay_contiguous(self, fetcher):
        responses.add(responses.GET, URL, json={"response": {"docs": []}}, status=200)
        list(UPREG.iter_records(fetcher, Query()))
        assert "sort=_root_+asc%2Cid+asc" in responses.calls[0].request.url

    @responses.activate
    def test_flat_cores_sort_by_id_alone(self, fetcher):
        core = solr_source_for_core("esma_registers_mmf04")
        flat_url = "https://registers.esma.europa.eu/solr/esma_registers_mmf04/select"
        responses.add(responses.GET, flat_url, json={"response": {"docs": []}}, status=200)
        list(core.iter_records(fetcher, Query()))
        assert "sort=id+asc" in responses.calls[0].request.url

    @responses.activate
    def test_page_size_is_capped_at_the_documented_maximum(self, fetcher):
        responses.add(responses.GET, URL, json={"response": {"docs": []}}, status=200)
        list(UPREG.iter_records(fetcher, Query(page_size=999_999)))
        assert "rows=1000" in responses.calls[0].request.url


class TestFacets:
    @responses.activate
    def test_facets_are_parsed_into_value_count_pairs(self, fetcher):
        responses.add(responses.GET, URL, json=load_json("upreg_facet.json"), status=200)
        pairs, note = UPREG.count_values(fetcher, Query(), "ae_entityTypeCode")
        assert pairs[:2] == [("mif", 7194), ("aif", 3786)]
        assert "case-insensitive" in (note or "")

    @responses.activate
    def test_facet_request_asks_for_every_value(self, fetcher):
        responses.add(responses.GET, URL, json=load_json("upreg_facet.json"), status=200)
        UPREG.count_values(fetcher, Query(), "ae_entityTypeCode")
        url = responses.calls[0].request.url
        assert "facet=true" in url and "facet.limit=-1" in url and "rows=0" in url

    @responses.activate
    def test_a_field_with_no_facet_bucket_returns_empty(self, fetcher):
        responses.add(responses.GET, URL, json={"facet_counts": {"facet_fields": {}}}, status=200)
        pairs, _ = UPREG.count_values(fetcher, Query(), "ae_nope")
        assert pairs == []


@responses.activate
def test_end_to_end_pages_then_flattens_into_entities(fetcher, upreg_pages):
    for page in upreg_pages:
        responses.add(responses.GET, URL, json=page, status=200)
    docs = list(UPREG.iter_records(fetcher, Query(page_size=4)))
    rows = flatten_auto(docs, UPREG.flatten_config(), block_structured=True)
    # 8 documents from a real capture describe 3 licensed entities.
    assert len(rows) == 3
    assert all(row.get("ae_entityTypeCode") == "CSP" for row in rows)
    names = [row["ae_entityName"] for row in rows]
    assert "Vauraus Suomi Oyj" in names
    # Its three activities were split across two pages and still joined up.
    vauraus = next(r for r in rows if r["ae_entityName"] == "Vauraus Suomi Oyj")
    assert vauraus["aeActivity_count"] == "3"


def test_ad_hoc_core_lookup_accepts_short_and_full_names():
    assert get_source("solr:esma_registers_funds").core == "esma_registers_funds"
    assert get_source("solr:funds").core == "esma_registers_funds"
