"""The Swiss UID register: SOAP envelopes, the 30-record ceiling, GetByUID's array."""

from __future__ import annotations

import pytest
import responses

from eufinreg.http import FetchError
from eufinreg.sources.base import Query
from eufinreg.sources.ch_uid import (
    ACTION_BASE,
    CH_UID,
    RESULT_CAP,
    SERVICE_URL,
    build_getbyuid_envelope,
    build_search_envelope,
    format_uid,
    parse_criteria,
    parse_response,
)

from .conftest import DATA


def load_xml(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def _mock(name: str) -> None:
    responses.add(responses.POST, SERVICE_URL, body=load_xml(name), content_type="text/xml")


class TestCriteria:
    def test_bare_text_is_taken_as_an_organisation_name(self):
        assert parse_criteria("Nestle", None) == {"organisationName": "Nestle"}

    def test_select_is_the_canton(self):
        assert parse_criteria(None, "zh") == {"cantonAbbreviation": "ZH"}

    def test_pairs_are_mapped_onto_the_request_fields(self):
        assert parse_criteria("zip=8001&town=Zürich", None) == {
            "swissZipCode": "8001",
            "town": "Zürich",
        }

    def test_an_unknown_criterion_is_named_rather_than_ignored(self):
        # Silently dropping it would produce a wider result set than asked for,
        # which is the worst possible failure for a compliance lookup.
        with pytest.raises(ValueError, match="unknown ch-uid criterion"):
            parse_criteria("sector=banking", None)

    def test_uid_is_kept_separate_because_it_changes_the_operation(self):
        assert parse_criteria("uid=CHE-101.329.561", None) == {"uid": "CHE-101.329.561"}


class TestEnvelopes:
    def test_the_search_envelope_carries_the_config_block(self):
        envelope = build_search_envelope({"organisationName": "UBS"}, max_records=30)
        assert "<b:maxNumberOfRecords>30</b:maxNumberOfRecords>" in envelope
        assert "<a:organisationName>UBS</a:organisationName>" in envelope

    def test_address_criteria_are_nested(self):
        envelope = build_search_envelope({"cantonAbbreviation": "ZH"}, max_records=5)
        assert "<a:address><a:cantonAbbreviation>ZH</a:cantonAbbreviation></a:address>" in envelope

    def test_values_are_escaped(self):
        envelope = build_search_envelope({"organisationName": "A & <B>"}, max_records=1)
        assert "A &amp; &lt;B&gt;" in envelope

    def test_getbyuid_sends_the_nine_digits(self):
        assert "<c:uidOrganisationId>101329561</c:uidOrganisationId>" in build_getbyuid_envelope(
            "CHE-101.329.561"
        )

    def test_a_malformed_uid_is_rejected_before_the_request(self):
        with pytest.raises(ValueError, match="nine digits"):
            build_getbyuid_envelope("CHE-101.329")


class TestParsing:
    def test_the_uid_is_reassembled_in_the_printed_form(self):
        assert format_uid("CHE", "101329561") == "CHE-101.329.561"

    def test_getbyuid_returns_every_seat_not_the_first(self):
        # CHE-101.329.561 is UBS AG, which the register holds twice: the Zürich
        # and Basel seats. Reading element [0] silently picks one.
        rows = parse_response(load_xml("ch_uid_getbyuid.xml"))
        assert len(rows) == 2
        assert {row["address.cantonAbbreviation"] for row in rows} == {"ZH", "BS"}
        assert {row["uid"] for row in rows} == {"CHE-101.329.561"}

    def test_paths_are_dotted_and_unprefixed(self):
        row = parse_response(load_xml("ch_uid_getbyuid.xml"))[0]
        assert row["organisationIdentification.organisationName"] == "UBS AG"
        assert row["uidregInformation.uidregStatusEnterpriseDetail"] == "3"
        assert not any(name.startswith("organisation.") for name in row)

    def test_repeated_blocks_are_joined_not_overwritten(self):
        # A company carries several OtherOrganisationId blocks (CH.HR, CH.EHRAID).
        row = parse_response(load_xml("ch_uid_getbyuid.xml"))[0]
        assert " | " in row["organisationIdentification.OtherOrganisationId.organisationIdCategory"]

    def test_search_items_keep_their_rating(self):
        rows = parse_response(load_xml("ch_uid_search.xml"))
        assert rows and rows[0]["rating"]

    def test_no_match_is_an_empty_list_not_an_error(self):
        assert parse_response(load_xml("ch_uid_empty.xml")) == []

    def test_a_soap_fault_is_surfaced(self):
        fault = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            "<s:Fault><faultstring>too many results</faultstring></s:Fault>"
            "</s:Body></s:Envelope>"
        )
        with pytest.raises(FetchError, match="too many results"):
            parse_response(fault)


class TestRequests:
    @responses.activate
    def test_search_sends_the_soap_action_and_content_type(self, fetcher):
        _mock("ch_uid_search.xml")
        list(CH_UID.iter_records(fetcher, Query(select="AI")))
        request = responses.calls[0].request
        assert request.headers["SOAPAction"] == f'"{ACTION_BASE}/Search"'
        assert request.headers["Content-Type"].startswith("text/xml")

    @responses.activate
    def test_a_uid_query_switches_to_getbyuid(self, fetcher):
        _mock("ch_uid_getbyuid.xml")
        list(CH_UID.iter_records(fetcher, Query(raw_query="uid=CHE-101.329.561")))
        assert responses.calls[0].request.headers["SOAPAction"] == f'"{ACTION_BASE}/GetByUID"'

    def test_a_search_with_no_criteria_is_refused(self, fetcher):
        with pytest.raises(ValueError, match="no 'list everything' mode"):
            list(CH_UID.iter_records(fetcher, Query()))

    @responses.activate
    def test_page_size_is_clamped_to_the_ceiling(self, fetcher):
        _mock("ch_uid_search.xml")
        list(CH_UID.iter_records(fetcher, Query(select="ZH", page_size=1000)))
        body = responses.calls[0].request.body
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert f"<b:maxNumberOfRecords>{RESULT_CAP}</b:maxNumberOfRecords>" in text


class TestCeilingWarning:
    @responses.activate
    def test_a_short_answer_warns_about_nothing(self, fetcher):
        _mock("ch_uid_search.xml")
        list(CH_UID.iter_records(fetcher, Query(select="AI", page_size=10)))
        assert CH_UID.warnings() == []

    @responses.activate
    def test_hitting_the_ceiling_is_reported(self, fetcher):
        # The live service answers a canton-wide search with exactly 30 records,
        # no total and no cursor — indistinguishable from "30 matches" unless
        # something says so.
        _mock("ch_uid_search.xml")
        list(CH_UID.iter_records(fetcher, Query(select="AI", page_size=2)))
        assert "undocumented ceiling" in " ".join(CH_UID.warnings())

    @responses.activate
    def test_getbyuid_is_never_reported_as_capped(self, fetcher):
        _mock("ch_uid_getbyuid.xml")
        list(CH_UID.iter_records(fetcher, Query(raw_query="uid=CHE-101.329.561", page_size=2)))
        assert CH_UID.warnings() == []
