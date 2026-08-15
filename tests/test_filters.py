from __future__ import annotations

import pytest

from eufinreg.filters import FieldFilter, apply_filters, row_contains

ROWS = [
    {"ae_entityName": "Bybit EU GmbH", "ae_homeMemberState": "AT", "services": "a | c | j"},
    {"ae_entityName": "Bitpanda GmbH", "ae_homeMemberState": "AT", "services": "a | c"},
    {"ae_entityName": "Coinbase Europe", "ae_homeMemberState": "IE", "services": "a"},
]


class TestFieldFilterParsing:
    def test_simple(self):
        f = FieldFilter.parse("ae_homeMemberState=AT")
        assert (f.name, f.value) == ("ae_homeMemberState", "AT")

    def test_value_may_contain_equals_signs(self):
        assert FieldFilter.parse("note=a=b").value == "a=b"

    def test_empty_value_is_allowed(self):
        """Selecting rows where a field is blank is a legitimate query."""
        assert FieldFilter.parse("ae_lei=").value == ""

    def test_missing_equals_is_rejected(self):
        with pytest.raises(ValueError, match="NAME=VALUE"):
            FieldFilter.parse("ae_homeMemberState")

    def test_missing_name_is_rejected(self):
        with pytest.raises(ValueError, match="field name"):
            FieldFilter.parse("=AT")


class TestContains:
    def test_matches_any_field_case_insensitively(self):
        assert row_contains(ROWS[0], "bybit")
        assert row_contains(ROWS[0], "AT")

    def test_no_match(self):
        assert not row_contains(ROWS[0], "kraken")

    def test_several_needles_are_anded(self):
        kept, _ = apply_filters(ROWS, contains=["gmbh", "at"])
        assert len(kept) == 2

    def test_matching_a_child_value_keeps_the_whole_entity_row(self):
        kept, _ = apply_filters(ROWS, contains=["j"])
        assert [r["ae_entityName"] for r in kept] == ["Bybit EU GmbH"]


class TestFieldMatching:
    def test_exact_match_is_case_insensitive(self):
        kept, _ = apply_filters(ROWS, field_filters=[FieldFilter("ae_homeMemberState", "at")])
        assert len(kept) == 2

    def test_partial_values_do_not_match(self):
        kept, _ = apply_filters(ROWS, field_filters=[FieldFilter("ae_entityName", "Bybit")])
        assert kept == []

    def test_combined_with_contains(self):
        kept, _ = apply_filters(
            ROWS, contains=["gmbh"], field_filters=[FieldFilter("ae_homeMemberState", "AT")]
        )
        assert len(kept) == 2


class TestDiagnostics:
    def test_a_typo_in_the_field_name_is_reported(self):
        _, report = apply_filters(ROWS, field_filters=[FieldFilter("ae_homeMemberStat", "AT")])
        assert any("does not exist" in w for w in report.warnings())

    def test_a_wrong_value_gets_a_different_hint(self):
        _, report = apply_filters(ROWS, field_filters=[FieldFilter("ae_homeMemberState", "ZZ")])
        warnings = report.warnings()
        assert any("--list-values" in w for w in warnings)
        assert not any("does not exist" in w for w in warnings)

    def test_a_successful_filter_is_silent(self):
        _, report = apply_filters(ROWS, field_filters=[FieldFilter("ae_homeMemberState", "AT")])
        assert report.warnings() == []

    def test_empty_input_is_not_reported_as_a_filter_problem(self):
        _, report = apply_filters([], contains=["anything"])
        assert report.warnings() == []


def test_limit_stops_early():
    kept, report = apply_filters(ROWS, limit=2)
    assert len(kept) == 2
    assert report.output_rows == 2


def test_no_filters_keeps_everything():
    kept, _ = apply_filters(ROWS)
    assert len(kept) == 3
