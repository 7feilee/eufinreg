from __future__ import annotations

from eufinreg.profile import count_values, profile_records, render_profile, render_values

RECORDS = [
    {"name": "Alpha", "lei": "123", "status": "Active", "note": ""},
    {"name": "Beta", "lei": "", "status": "Active", "note": ""},
    {"name": "Gamma", "status": "Inactive", "extra": "surprise"},
]


class TestProfile:
    def test_counts_records(self):
        total, _ = profile_records(RECORDS)
        assert total == 3

    def test_present_and_filled_are_counted_separately(self):
        _, profiles = profile_records(RECORDS)
        by_name = {p.name: p for p in profiles}
        assert by_name["lei"].present == 2
        assert by_name["lei"].non_empty == 1

    def test_a_field_only_one_record_has_is_still_reported(self):
        _, profiles = profile_records(RECORDS)
        assert "extra" in {p.name for p in profiles}

    def test_always_empty_fields_are_visible(self):
        _, profiles = profile_records(RECORDS)
        note = next(p for p in profiles if p.name == "note")
        assert note.present == 2 and note.non_empty == 0
        assert "always empty" in render_profile(3, profiles, title="t")

    def test_sorted_by_how_common_the_field_is(self):
        _, profiles = profile_records(RECORDS)
        assert profiles[0].name in {"name", "status"}

    def test_distinct_counts(self):
        _, profiles = profile_records(RECORDS)
        assert next(p for p in profiles if p.name == "status").distinct == 2

    def test_samples_are_capped_and_truncated(self):
        records = [{"x": "y" * 200} for _ in range(10)]
        _, profiles = profile_records(records, max_samples=2, sample_width=20)
        assert len(profiles[0].samples) == 1  # only one distinct value
        assert len(profiles[0].samples[0]) <= 20

    def test_empty_input_renders_a_useful_message(self):
        total, profiles = profile_records([])
        assert "matched nothing" in render_profile(total, profiles, title="t")


class TestCountValues:
    def test_counts_and_sorts_descending(self):
        assert count_values(RECORDS, "status") == [("Active", 2), ("Inactive", 1)]

    def test_blank_values_are_not_counted(self):
        assert count_values(RECORDS, "lei") == [("123", 1)]

    def test_unknown_field_yields_nothing(self):
        assert count_values(RECORDS, "nope") == []

    def test_multi_value_cells_can_be_split(self):
        rows = [{"s": "a|b"}, {"s": "b|c"}]
        assert count_values(rows, "s", split="|") == [("b", 2), ("a", 1), ("c", 1)]

    def test_ties_are_broken_alphabetically(self):
        rows = [{"s": "z"}, {"s": "a"}]
        assert count_values(rows, "s") == [("a", 1), ("z", 1)]


class TestRenderValues:
    def test_lists_values_with_counts(self):
        out = render_values("status", [("Active", 2)], source="upreg", note="hi")
        assert "Active" in out and "2" in out and "hi" in out

    def test_empty_result_suggests_inspect(self):
        out = render_values("nope", [], source="upreg")
        assert "--inspect" in out
