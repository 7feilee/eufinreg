"""VIES: parsing VAT numbers, and never filing "system down" as "not registered"."""

from __future__ import annotations

import pytest
import responses

from eufinreg.sources.base import Query
from eufinreg.sources.vies import BASE_URL, VIES, parse_targets, parse_vat

from .conftest import load_json


def _mock_check(country: str = "DE", number: str = "123456789", **overrides) -> None:
    payload = {**load_json("vies_check.json"), **overrides}
    responses.add(responses.GET, f"{BASE_URL}/ms/{country}/vat/{number}", json=payload)


def _mock_status() -> None:
    responses.add(responses.GET, f"{BASE_URL}/check-status", json=load_json("vies_status.json"))


class TestParsing:
    def test_a_plain_number_splits_into_country_and_number(self):
        assert parse_vat("DE123456789") == ("DE", "123456789")

    def test_austrias_u_belongs_to_the_number_not_the_country(self):
        assert parse_vat("ATU12345678") == ("AT", "U12345678")

    def test_punctuation_and_spacing_are_tolerated(self):
        # This is how VAT numbers appear on invoices and in pasted spreadsheets.
        assert parse_vat(" de 123.456-789 ") == ("DE", "123456789")

    def test_something_that_is_not_a_vat_number_says_so(self):
        with pytest.raises(ValueError, match="not a VAT number"):
            parse_vat("Acme GmbH")

    def test_a_prefix_with_no_number_is_rejected(self):
        with pytest.raises(ValueError, match=r"not a VAT number"):
            parse_vat("DE")

    def test_targets_accept_both_spellings_and_lists(self):
        assert parse_targets("vat=DE123456789") == ["DE123456789"]
        assert parse_targets("DE123456789, ATU12345678") == ["DE123456789", "ATU12345678"]

    def test_an_unknown_criterion_is_named(self):
        with pytest.raises(ValueError, match="unknown vies criterion"):
            parse_targets("name=Acme")


class TestChecks:
    @responses.activate
    def test_a_check_returns_one_flat_row(self, fetcher):
        _mock_check()
        rows = list(VIES.iter_records(fetcher, Query(raw_query="vat=DE123456789")))
        assert len(rows) == 1
        assert rows[0]["vatNumber"] == "123456789"
        assert rows[0]["countryCode"] == "DE"
        assert "viesApproximate.name" in rows[0]

    @responses.activate
    def test_several_numbers_are_checked_in_order(self, fetcher):
        _mock_check("DE", "123456789")
        _mock_check("AT", "U12345678")
        rows = list(VIES.iter_records(fetcher, Query(raw_query="DE123456789,ATU12345678")))
        assert len(rows) == 2
        assert [call.request.url.split("/")[-3] for call in responses.calls] == ["DE", "AT"]

    def test_it_refuses_to_be_a_directory(self, fetcher):
        with pytest.raises(ValueError, match="no 'list everything' mode"):
            list(VIES.iter_records(fetcher, Query()))

    def test_it_is_not_archivable(self):
        # A scheduled crawl of a validation service is abuse, not ingestion.
        assert VIES.bulk_readable is False


class TestUnavailability:
    @responses.activate
    def test_an_invalid_number_is_not_a_warning(self, fetcher):
        _mock_check(userError="INVALID")
        list(VIES.iter_records(fetcher, Query(raw_query="vat=DE123456789")))
        assert VIES.warnings() == []

    @responses.activate
    def test_an_unavailable_member_state_is_flagged_loudly(self, fetcher):
        # The failure this exists to prevent: recording a live company as
        # unregistered because a national system was down for ten minutes.
        _mock_check(userError="MS_UNAVAILABLE", isValid=False)
        list(VIES.iter_records(fetcher, Query(raw_query="vat=DE123456789")))
        warning = " ".join(VIES.warnings())
        assert "not 'not registered'" in warning
        assert "DE" in warning

    @responses.activate
    def test_a_timeout_answer_is_treated_the_same_way(self, fetcher):
        _mock_check(userError="TIMEOUT", isValid=False)
        list(VIES.iter_records(fetcher, Query(raw_query="vat=DE123456789")))
        assert VIES.warnings()


class TestStatus:
    @responses.activate
    def test_status_lists_one_row_per_member_state(self, fetcher):
        _mock_status()
        rows = list(VIES.iter_records(fetcher, Query(select="STATUS")))
        assert rows and {"countryCode", "availability", "vow_available"} <= set(rows[0])

    @responses.activate
    def test_status_flags_the_ones_that_are_down(self, fetcher):
        _mock_status()
        list(VIES.iter_records(fetcher, Query(select="STATUS")))
        assert "XX" in " ".join(VIES.warnings())
