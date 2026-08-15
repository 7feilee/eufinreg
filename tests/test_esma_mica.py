"""MiCA CSV register: file quirks and the free-text-enum normalisation."""

from __future__ import annotations

import responses

from eufinreg.sources.base import Query
from eufinreg.sources.esma_mica import (
    CASP,
    MICA_SERVICE_LETTERS,
    normalise_services,
    parse_csv,
    service_letter,
    split_service_cell,
)

from .conftest import load_text

CSV_URL = "https://www.esma.europa.eu/sites/default/files/2024-12/CASPS.csv"


def _register_csv(body: str | None = None) -> None:
    responses.add(
        responses.GET,
        CSV_URL,
        body=(body if body is not None else load_text("casps_sample.csv")),
        status=200,
        content_type="text/csv",
    )


class TestCsvQuirks:
    def test_records_survive_values_containing_newlines(self):
        text = load_text("casps_sample.csv")
        # The fixture is a real capture: more physical lines than records.
        assert text.count("\n") > len(parse_csv(text))

    def test_trailing_comma_in_the_header_does_not_create_a_column(self):
        rows = parse_csv(load_text("casps_sample.csv"))
        assert "" not in rows[0]

    def test_documented_fields_are_present(self):
        rows = parse_csv(load_text("casps_sample.csv"))
        for name in (
            "ae_competentAuthority",
            "ae_lei_name",
            "ac_serviceCode",
            "ac_serviceCode_cou",
        ):
            assert name in rows[0]

    def test_rows_with_more_fields_than_the_header_are_kept(self):
        rows = parse_csv("a,b\n1,2,3,4\n")
        assert rows[0]["a"] == "1"
        assert rows[0]["_extra_columns"] == "3 | 4"

    def test_blank_lines_are_skipped(self):
        assert parse_csv("a,b\n1,2\n,\n") == [{"a": "1", "b": "2"}]

    def test_empty_file_is_not_an_error(self):
        assert parse_csv("") == []


class TestServiceSplitting:
    def test_documented_pipe_separator(self):
        cell = "a. providing custody | c. exchange of crypto-assets for funds"
        assert len(split_service_cell(cell)) == 2

    def test_capital_i_used_as_a_pipe(self):
        cell = (
            "e. execution of orders for crypto-assets on behalf of clients "
            "I g. reception and transmission of orders for crypto-assets on behalf of clients"
        )
        assert normalise_services(cell) == "e|g"

    def test_comma_before_a_letter_marker(self):
        cell = (
            "e. execution of orders for crypto-assets on behalf of clients, "
            "g. reception and transmission of orders for crypto-assets on behalf of clients"
        )
        assert normalise_services(cell) == "e|g"

    def test_slash_separated_free_text(self):
        cell = (
            "Reception and transmission of client orders/ Execution of orders on behalf of "
            "clients/ Exchange between crypto assets and fiat currency/ Exchange between "
            "crypto assets/ Providing custody and administration of crypto-assets on behalf "
            "of clients/ Providing transfer services for crypto-assets on behalf of clients"
        )
        # 'and fiat currency' is (c); the bare 'between crypto assets' is (d).
        assert normalise_services(cell) == "a|c|d|e|g|j"

    def test_full_stop_butted_against_the_next_marker(self):
        cell = (
            "g. reception and transmission of orders for crypto-assets on behalf of clients."
            "j. providing transfer services for crypto-assets on behalf of clients."
        )
        assert normalise_services(cell) == "g|j"

    def test_embedded_newline_separator(self):
        assert normalise_services("c. exchange of crypto-assets for funds\nj. transfer") == "c|j"

    def test_empty_cell(self):
        assert split_service_cell("") == []
        assert normalise_services("") == ""


class TestServiceLetter:
    def test_letter_prefix_variants(self):
        for text in (
            "a. providing custody",
            "a.providing custody",
            "a.\tproviding custody",
            "(a) providing custody",
            "A. Providing Custody",
        ):
            assert service_letter(text) == "a"

    def test_keyword_fallback_without_a_letter(self):
        assert service_letter("Providing portfolio management on crypto-assets") == "i"
        assert service_letter("operation of a trading platform for crypto-assets") == "b"

    def test_unmappable_text_returns_none(self):
        assert service_letter("something entirely else") is None

    def test_unmappable_fragments_are_reported_not_swallowed(self):
        assert normalise_services("a. custody | something entirely else") == "a|?"

    def test_letters_are_deduplicated_and_sorted(self):
        cell = "c. exchange for funds | a. custody | c. exchange of crypto-assets for funds"
        assert normalise_services(cell) == "a|c"

    def test_every_documented_letter_round_trips(self):
        for letter, description in MICA_SERVICE_LETTERS.items():
            assert service_letter(f"{letter}. {description}") == letter


class TestSource:
    @responses.activate
    def test_fetches_and_derives_the_normalised_column(self, fetcher):
        _register_csv()
        rows = list(CASP.iter_records(fetcher, Query()))
        assert rows
        assert all("ac_serviceCode_normalised" in row for row in rows)
        bybit = next(r for r in rows if r["ae_lei_name"] == "Bybit EU GmbH")
        assert bybit["ac_serviceCode_normalised"] == "a|c|d|f|j"
        # The original value is never modified.
        assert "providing custody" in bybit["ac_serviceCode"]

    @responses.activate
    def test_no_derived_switches_the_extra_column_off(self, fetcher):
        _register_csv()
        rows = list(CASP.iter_records(fetcher, Query(extra={"no_derived": True})))
        assert "ac_serviceCode_normalised" not in rows[0]

    @responses.activate
    def test_max_docs_truncates(self, fetcher):
        _register_csv()
        rows = list(CASP.iter_records(fetcher, Query(max_docs=2)))
        assert len(rows) == 2

    @responses.activate
    def test_passporting_countries_are_counted_individually(self, fetcher):
        _register_csv()
        pairs, note = CASP.count_values(fetcher, Query(), "ae_homeMemberState")
        assert dict(pairs).get("AT", 0) >= 2
        assert "full CSV" in (note or "")

    @responses.activate
    def test_service_codes_are_counted_as_fragments_not_whole_cells(self, fetcher):
        _register_csv()
        pairs, note = CASP.count_values(fetcher, Query(), "ac_serviceCode")
        # Whole-cell counting would give one bucket per firm; fragments give more.
        assert len(pairs) > 3
        assert "free-text" in (note or "")

    @responses.activate
    def test_the_file_is_only_downloaded_once_per_run(self, fetcher):
        _register_csv()
        list(CASP.iter_records(fetcher, Query()))
        CASP.count_values(fetcher, Query(), "ae_homeMemberState")
        assert len(responses.calls) == 1
