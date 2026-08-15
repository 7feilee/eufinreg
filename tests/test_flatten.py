"""The parent/child flattening is the easiest thing in this project to get
subtly wrong, so it gets the most tests."""

from __future__ import annotations

from eufinreg.flatten import (
    FlattenConfig,
    column_order,
    flatten,
    flatten_auto,
    passthrough,
    scalar,
)

PARENT = {
    "id": "ae42",
    "type_s": "parent",
    "entity_type": "ae",
    "_root_": "ae42",
    "ae_entityName": "Catam Asset Management AG",
    "ae_entityTypeCode": "MIF",
    "_version_": 1861996368975888384,
}
CHILD_A = {
    "id": "aeActivity1",
    "type_s": "child",
    "entity_type": "aeActivity",
    "_root_": "ae42",
    "ac_serviceName": "Investment advice",
    "ac_status": "Active",
}
CHILD_B = {
    "id": "aeActivity2",
    "type_s": "child",
    "entity_type": "aeActivity",
    "_root_": "ae42",
    "ac_serviceName": "Portfolio management",
    "ac_status": "Active",
}
HISTORY = {
    "id": "aeActivityHistory9",
    "type_s": "child",
    "entity_type": "aeActivityHistory",
    "_root_": "ae42",
    "ac_serviceName": "Dealing on own account",
    "ac_status": "Withdrawn",
}


def test_one_row_per_entity():
    rows = flatten([PARENT, CHILD_A, CHILD_B])
    assert len(rows) == 1
    assert rows[0]["ae_entityName"] == "Catam Asset Management AG"


def test_children_collapse_into_pipe_joined_columns():
    row = flatten([PARENT, CHILD_A, CHILD_B])[0]
    assert row["aeActivity_ac_serviceName"] == "Investment advice | Portfolio management"
    assert row["aeActivity_ac_status"] == "Active | Active"
    assert row["aeActivity_count"] == "2"


def test_child_order_is_preserved():
    row = flatten([PARENT, CHILD_B, CHILD_A])[0]
    assert row["aeActivity_ac_serviceName"] == "Portfolio management | Investment advice"


def test_child_buckets_are_kept_apart_by_entity_type():
    row = flatten([PARENT, CHILD_A, HISTORY])[0]
    assert row["aeActivity_ac_serviceName"] == "Investment advice"
    assert row["aeActivityHistory_ac_serviceName"] == "Dealing on own account"
    assert row["aeActivity_count"] == "1"
    assert row["aeActivityHistory_count"] == "1"


def test_children_may_arrive_before_their_parent():
    """Solr sorts by id, so 'aeActivity1' precedes 'ae42' — order must not matter."""
    row = flatten([CHILD_A, CHILD_B, PARENT])[0]
    assert row["ae_entityName"] == "Catam Asset Management AG"
    assert row["aeActivity_count"] == "2"


def test_children_split_across_pages_still_join():
    """The real failure mode: a parent's children straddle a page boundary."""
    page1 = [PARENT, CHILD_A]
    page2 = [CHILD_B]
    rows = flatten([*page1, *page2])
    assert len(rows) == 1
    assert rows[0]["aeActivity_count"] == "2"


def test_orphan_children_are_flagged_not_dropped():
    rows = flatten([CHILD_A, CHILD_B])
    assert len(rows) == 1
    assert rows[0]["_orphan"] == "true"
    assert rows[0]["_root_"] == "ae42"
    assert rows[0]["aeActivity_count"] == "2"


def test_multiple_entities_keep_first_seen_order():
    other_parent = {**PARENT, "id": "ae1", "_root_": "ae1", "ae_entityName": "Zeta AG"}
    rows = flatten([PARENT, CHILD_A, other_parent])
    assert [r["ae_entityName"] for r in rows] == ["Catam Asset Management AG", "Zeta AG"]


def test_internal_solr_fields_are_dropped():
    row = flatten([PARENT])[0]
    assert "_version_" not in row


def test_duplicate_parents_are_not_silently_lost():
    twin = {**PARENT, "id": "ae42-dup", "ae_entityName": "Impostor AG"}
    row = flatten([PARENT, twin])[0]
    assert row["ae_entityName"] == "Catam Asset Management AG"
    assert row["duplicateParent_ae_entityName"] == "Impostor AG"


def test_doc_without_root_falls_back_to_its_own_id():
    lone = {"id": "x1", "type_s": "parent", "name": "No root here"}
    rows = flatten([lone])
    assert len(rows) == 1
    assert rows[0]["name"] == "No root here"


def test_unknown_new_fields_survive():
    """Schema drift: a field nobody has ever seen must still reach the output."""
    row = flatten([{**PARENT, "ae_brandNewField": "hello"}])[0]
    assert row["ae_brandNewField"] == "hello"


def test_flatten_auto_passes_through_flat_data():
    flat = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert flatten_auto(flat) == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_flatten_auto_flattens_when_a_parent_is_present():
    rows = flatten_auto([PARENT, CHILD_A])
    assert rows[0]["aeActivity_count"] == "1"


def test_flatten_auto_can_be_forced():
    """A page of pure children must still flatten when the source says so."""
    rows = flatten_auto([CHILD_A], block_structured=True)
    assert rows[0]["_orphan"] == "true"


def test_custom_separator():
    cfg = FlattenConfig(separator=";")
    row = flatten([PARENT, CHILD_A, CHILD_B], cfg)[0]
    assert row["aeActivity_ac_serviceName"] == "Investment advice;Portfolio management"


class TestScalar:
    def test_none_becomes_empty_string(self):
        assert scalar(None) == ""

    def test_list_is_joined(self):
        assert scalar(["a", "b"]) == "a | b"

    def test_nested_mapping_becomes_json(self):
        assert scalar({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'

    def test_bool_is_lowercase(self):
        assert scalar(True) == "true"

    def test_zero_is_not_swallowed(self):
        assert scalar(0) == "0"


def test_column_order_is_the_union_across_ragged_rows():
    rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
    assert column_order(rows) == ["a", "b", "c"]


def test_passthrough_drops_internal_fields():
    assert passthrough([PARENT])[0].get("_version_") is None
