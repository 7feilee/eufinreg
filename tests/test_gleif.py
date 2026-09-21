"""GLEIF: the identity key, and a 10,000-result ceiling the API states out loud."""

from __future__ import annotations

import pytest
import responses

from eufinreg.sources.base import Query
from eufinreg.sources.gleif import (
    API_URL,
    GLEIF,
    MAX_PAGE_SIZE,
    RESULT_WINDOW,
    flatten_record,
    parse_raw_query,
    resolve_countries,
)

from .conftest import load_json


def _mock_pages(pages: int = 2, total: int = 6) -> None:
    for n in range(1, pages + 1):
        payload = load_json(f"gleif_page{n}.json")
        payload["meta"]["pagination"]["total"] = total
        responses.add(responses.GET, API_URL, json=payload)


def _params(call) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(call.request.url).query).items()}


class TestSelection:
    def test_dach_includes_liechtenstein(self):
        # It shares Switzerland's market in practice, and ESMA lists Vaduz firms.
        assert resolve_countries("dach") == "DE,AT,CH,LI"

    def test_a_list_of_codes_is_pushed_as_one_filter(self):
        assert resolve_countries("de, at") == "DE,AT"

    def test_all_means_no_filter(self):
        assert resolve_countries("ALL") == ""
        assert resolve_countries(None) == ""

    def test_a_three_letter_code_is_rejected_with_a_pointer(self):
        # TED uses alpha-3 and GLEIF uses alpha-2; mixing them silently returns
        # nothing, so this refuses rather than querying for a country that is
        # not there.
        with pytest.raises(ValueError, match="two-letter country codes"):
            resolve_countries("DEU")

    def test_a_bare_filter_is_wrapped(self):
        assert parse_raw_query("entity.status=ACTIVE") == {"filter[entity.status]": "ACTIVE"}

    def test_an_explicit_filter_is_left_alone(self):
        assert parse_raw_query("filter[entity.legalAddress.city]=Zug") == {
            "filter[entity.legalAddress.city]": "Zug"
        }

    def test_a_pair_without_an_equals_is_an_error(self):
        with pytest.raises(ValueError, match="key=value"):
            parse_raw_query("ACTIVE")


class TestFlattening:
    def test_the_lei_is_lifted_to_the_front(self):
        row = flatten_record({"id": "5299001", "attributes": {"entity": {"status": "ACTIVE"}}})
        assert next(iter(row)) == "lei"
        assert row["lei"] == "5299001"

    def test_paths_are_dotted(self):
        record = load_json("gleif_page1.json")["data"][0]
        row = flatten_record(record)
        assert row["entity.legalName.name"]
        assert row["entity.legalAddress.country"] == "LI"

    def test_scalar_lists_are_joined(self):
        row = flatten_record({"id": "x", "attributes": {"entity": {"a": ["one", "two"]}}})
        assert row["entity.a"] == "one | two"

    def test_a_list_of_objects_is_kept_as_json(self):
        row = flatten_record({"id": "x", "attributes": {"names": [{"name": "A"}]}})
        assert row["names"] == '[{"name": "A"}]'

    def test_empty_lists_do_not_become_columns(self):
        assert "entity.otherNames" not in flatten_record(
            {"id": "x", "attributes": {"entity": {"otherNames": []}}}
        )


class TestRequests:
    @responses.activate
    def test_the_country_filter_is_pushed_server_side(self, fetcher):
        _mock_pages()
        list(GLEIF.iter_records(fetcher, Query(select="LI")))
        assert _params(responses.calls[0])["filter[entity.legalAddress.country]"] == "LI"

    @responses.activate
    def test_page_size_is_clamped_to_the_documented_cap(self, fetcher):
        # GLEIF answers 400 above 200 and says so; asking for more is pointless.
        _mock_pages()
        list(GLEIF.iter_records(fetcher, Query(select="LI", page_size=1000)))
        assert _params(responses.calls[0])["page[size]"] == str(MAX_PAGE_SIZE)

    @responses.activate
    def test_paging_follows_the_total(self, fetcher):
        _mock_pages(pages=2, total=6)
        rows = list(GLEIF.iter_records(fetcher, Query(select="LI", page_size=3)))
        assert len(rows) == 6
        assert len({row["lei"] for row in rows}) == 6

    @responses.activate
    def test_max_docs_stops_early(self, fetcher):
        _mock_pages()
        assert len(list(GLEIF.iter_records(fetcher, Query(select="LI", max_docs=2)))) == 2
        assert len(responses.calls) == 1

    @responses.activate
    def test_an_error_document_is_surfaced_not_ignored(self, fetcher):
        responses.add(
            responses.GET,
            API_URL,
            json={
                "errors": [{"status": "400", "detail": "The page.size must be between 1 and 200."}]
            },
        )
        from eufinreg.http import FetchError

        with pytest.raises(FetchError, match=r"page\.size must be between"):
            list(GLEIF.iter_records(fetcher, Query(select="LI")))

    @responses.activate
    def test_the_golden_copy_vintage_is_recorded(self, fetcher):
        _mock_pages()
        list(GLEIF.iter_records(fetcher, Query(select="LI")))
        assert GLEIF.vintage


class TestCeiling:
    @responses.activate
    def test_a_complete_answer_warns_about_nothing(self, fetcher):
        _mock_pages(pages=2, total=6)
        list(GLEIF.iter_records(fetcher, Query(select="LI", page_size=3)))
        assert GLEIF.warnings() == []

    @responses.activate
    def test_the_ceiling_is_reported_with_a_way_out(self, fetcher):
        # Every DACH country is over 10,000 on its own, so this is the normal
        # case rather than an edge case.
        _mock_pages(pages=2, total=254_108)
        list(GLEIF.iter_records(fetcher, Query(select="DE", page_size=3)))
        warning = " ".join(GLEIF.warnings())
        assert "254,108" in warning
        assert f"{RESULT_WINDOW:,}" in warning
        assert "goldencopy.gleif.org" in warning

    @responses.activate
    def test_the_shortfall_is_noted_on_value_counts_too(self, fetcher):
        _mock_pages(pages=2, total=254_108)
        _, note = GLEIF.count_values(fetcher, Query(select="DE", page_size=3), "entity.status")
        assert "were reachable" in (note or "")


class TestMetadata:
    def test_it_declares_the_lei_as_its_identifier(self):
        # This is the join key the rest of the project needs: ESMA and MiCA both
        # carry an LEI, so a watchlist entry with one matches across registers.
        assert GLEIF.identifier_columns == ("lei",)
        assert GLEIF.key_columns == ("lei",)

    def test_it_is_cc0(self):
        assert "CC0" in GLEIF.data_licence
