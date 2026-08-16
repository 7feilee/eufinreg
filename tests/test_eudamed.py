"""EUDAMED: the language trap, the silent page cap, and actor-type selection."""

from __future__ import annotations

import pytest
import responses

from eufinreg.http import FetchError
from eufinreg.sources.base import Query
from eufinreg.sources.eudamed import (
    ACTOR_TYPES,
    ECONOMIC_OPERATORS,
    MAX_PAGE_SIZE,
    NOTIFIED_BODIES,
    SORT,
    _parse_raw_query,
    flatten_actor,
    resolve_actor_types,
)

from .conftest import load_json

EO_URL = "https://ec.europa.eu/tools/eudamed/api/eos"
NB_URL = "https://ec.europa.eu/tools/eudamed/api/ses/"


def _mock_eo() -> None:
    for n in (1, 2):
        responses.add(responses.GET, EO_URL, json=load_json(f"eudamed_eo_page{n}.json"))


def _mock_nb() -> None:
    responses.add(responses.GET, NB_URL, json=load_json("eudamed_nb.json"))


def _params(call) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(call.request.url).query).items()}


class TestActorTypeSelection:
    def test_no_selection_means_no_server_side_filter(self):
        assert resolve_actor_types(None) == []
        assert resolve_actor_types("ALL") == []
        assert resolve_actor_types("  ") == []

    def test_bare_names_map_to_refdata_codes(self):
        assert resolve_actor_types("manufacturer") == ["refdata.actor-type.manufacturer"]

    def test_short_aliases_work(self):
        assert resolve_actor_types("ar") == ["refdata.actor-type.authorised-representative"]
        assert resolve_actor_types("sppp") == ["refdata.actor-type.system-procedure-pack-producer"]

    def test_american_spelling_is_accepted(self):
        assert resolve_actor_types("authorized-representative") == [
            "refdata.actor-type.authorised-representative"
        ]

    def test_full_codes_pass_through(self):
        assert resolve_actor_types("refdata.actor-type.importer") == ["refdata.actor-type.importer"]

    def test_codes_combine_and_deduplicate(self):
        assert resolve_actor_types("mf,importer,manufacturer") == [
            "refdata.actor-type.manufacturer",
            "refdata.actor-type.importer",
        ]

    def test_an_unknown_type_is_rejected_rather_than_silently_ignored(self):
        # Silently dropping it would return the whole register and look like a
        # filter that matched everything.
        with pytest.raises(ValueError, match="unknown actor type"):
            resolve_actor_types("wholesaler")

    def test_every_documented_type_resolves(self):
        for name, code in ACTOR_TYPES.items():
            assert resolve_actor_types(name) == [code]


class TestFlattening:
    def test_reference_data_objects_become_their_code(self):
        row = flatten_actor(
            {
                "actorType": {
                    "code": "refdata.actor-type.importer",
                    "srnCode": "IM",
                    "category": "ECONOMIC_ENTITY",
                }
            }
        )
        assert row["actorType"] == "refdata.actor-type.importer"
        assert row["actorType_srnCode"] == "IM"
        assert row["actorType_category"] == "ECONOMIC_ENTITY"

    def test_multilingual_name_blocks_are_joined(self):
        row = flatten_actor({"names": {"texts": [{"text": "Acme GmbH"}, {"text": "Acme Ltd"}]}})
        assert row["names"] == "Acme GmbH | Acme Ltd"

    def test_legislation_links_split_into_codes_and_urls(self):
        row = flatten_actor(
            {
                "legislationLinks": [
                    {
                        "legislationCode": "refdata.applicable-legislation.mdr",
                        "link": "https://example.invalid/1",
                    },
                    {"legislationCode": "refdata.applicable-legislation.ivdr", "link": None},
                ]
            }
        )
        assert row["legislationCodes"].endswith("mdr | refdata.applicable-legislation.ivdr")
        assert row["legislationLinks"] == "https://example.invalid/1"

    def test_nulls_become_empty_strings(self):
        assert flatten_actor({"postbox": None})["postbox"] == ""

    def test_an_unrecognised_nested_field_is_kept_as_json_not_dropped(self):
        row = flatten_actor({"somethingNew": {"a": 1, "b": [2, 3]}})
        assert row["somethingNew"] == '{"a": 1, "b": [2, 3]}'

    def test_scalar_lists_are_pipe_joined(self):
        assert flatten_actor({"tags": ["a", "b"]})["tags"] == "a | b"


class TestRequestShape:
    @responses.activate
    def test_the_language_parameter_is_always_sent(self, fetcher):
        # Without it api/eos returns HTTP 500 and api/ses/ silently returns one
        # row per language — 70 notified bodies become 1,890.
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))
        assert _params(responses.calls[0])["languageIso2Code"] == "en"

    @responses.activate
    def test_notified_bodies_also_send_the_language(self, fetcher):
        _mock_nb()
        list(NOTIFIED_BODIES.iter_records(fetcher, Query()))
        assert _params(responses.calls[0])["languageIso2Code"] == "en"

    @responses.activate
    def test_page_size_is_clamped_to_the_real_ceiling(self, fetcher):
        # The server caps at 300 silently; asking for 1000 and believing the
        # answer is how you conclude a 48,893-row register has 300 rows.
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query(page_size=1000)))
        assert _params(responses.calls[0])["size"] == str(MAX_PAGE_SIZE)

    @responses.activate
    def test_paging_uses_the_unique_sort_key(self, fetcher):
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))
        assert _params(responses.calls[0])["sort"] == SORT

    @responses.activate
    def test_select_is_pushed_to_the_server(self, fetcher):
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query(select="importer")))
        assert _params(responses.calls[0])["actorTypeCode"] == "refdata.actor-type.importer"

    @responses.activate
    def test_raw_query_adds_arbitrary_server_side_filters(self, fetcher):
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query(raw_query="countryIso2Code=DE")))
        assert _params(responses.calls[0])["countryIso2Code"] == "DE"

    def test_raw_query_must_look_like_url_parameters(self):
        with pytest.raises(ValueError, match="raw url parameters"):
            _parse_raw_query("countryIso2Code:DE")

    def test_raw_query_parsing(self):
        assert _parse_raw_query("a=1&b=2") == [("a", "1"), ("b", "2")]
        assert _parse_raw_query(None) == []

    def test_notified_bodies_reject_a_select_they_cannot_express(self):
        with pytest.raises(ValueError, match="does not support --select"):
            NOTIFIED_BODIES.build_params(Query(select="manufacturer"))


class TestPaging:
    @responses.activate
    def test_all_pages_are_followed(self, fetcher):
        _mock_eo()
        rows = list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))
        assert len(rows) == 6
        assert len({row["ulid"] for row in rows}) == 6

    @responses.activate
    def test_paging_stops_on_the_last_page_flag(self, fetcher):
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))
        assert len(responses.calls) == 2

    @responses.activate
    def test_max_docs_stops_mid_page_without_fetching_more(self, fetcher):
        _mock_eo()
        rows = list(ECONOMIC_OPERATORS.iter_records(fetcher, Query(max_docs=2)))
        assert len(rows) == 2
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_single_page_source_makes_one_request(self, fetcher):
        _mock_nb()
        rows = list(NOTIFIED_BODIES.iter_records(fetcher, Query()))
        assert len(rows) == 4
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_non_page_response_is_reported_clearly(self, fetcher):
        responses.add(responses.GET, EO_URL, json=["unexpected"])
        with pytest.raises(FetchError, match="not a page object"):
            list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))

    @responses.activate
    def test_results_are_reused_within_a_run(self, fetcher):
        _mock_eo()
        list(ECONOMIC_OPERATORS.iter_records(fetcher, Query()))
        ECONOMIC_OPERATORS.count_values(fetcher, Query(), "countryIso2Code")
        ECONOMIC_OPERATORS.count_values(fetcher, Query(), "actorType")
        assert len(responses.calls) == 2  # the two pages, fetched once


class TestCountValues:
    @responses.activate
    def test_counts_come_from_every_fetched_record(self, fetcher):
        _mock_eo()
        pairs, note = ECONOMIC_OPERATORS.count_values(fetcher, Query(), "countryIso2Code")
        assert sum(count for _, count in pairs) == 6
        assert "not a sample" in (note or "")

    @responses.activate
    def test_multi_valued_columns_are_split(self, fetcher):
        _mock_nb()
        pairs, note = NOTIFIED_BODIES.count_values(fetcher, Query(), "legislationCodes")
        assert all(" | " not in value for value, _ in pairs)
        assert "split on" in (note or "")
