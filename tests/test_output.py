from __future__ import annotations

import csv
import json

import pytest

from eufinreg.output import write_rows

ROWS = [{"a": "1", "b": "x"}, {"a": "2", "c": "only here"}]


def test_csv_uses_the_union_of_columns(tmp_path):
    out = tmp_path / "o.csv"
    write_rows(ROWS, fmt="csv", path=str(out))
    with open(out, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0] == {"a": "1", "b": "x", "c": ""}
    assert rows[1] == {"a": "2", "b": "", "c": "only here"}


def test_csv_quotes_values_containing_the_delimiter(tmp_path):
    out = tmp_path / "o.csv"
    write_rows([{"a": "x,y"}], fmt="csv", path=str(out))
    assert '"x,y"' in out.read_text(encoding="utf-8")


def test_non_ascii_survives_the_round_trip(tmp_path):
    out = tmp_path / "o.csv"
    write_rows([{"name": "Société Générale"}], fmt="csv", path=str(out))
    assert "Société Générale" in out.read_text(encoding="utf-8")


def test_json_output(tmp_path):
    out = tmp_path / "o.json"
    write_rows(ROWS, fmt="json", path=str(out))
    assert json.loads(out.read_text(encoding="utf-8")) == ROWS


def test_jsonl_output(tmp_path):
    out = tmp_path / "o.jsonl"
    write_rows(ROWS, fmt="jsonl", path=str(out))
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_missing_parent_directory_is_created(tmp_path):
    out = tmp_path / "nested" / "deep" / "o.csv"
    write_rows(ROWS, fmt="csv", path=str(out))
    assert out.exists()


def test_empty_result_still_writes_a_header(tmp_path):
    out = tmp_path / "o.csv"
    write_rows([], fmt="csv", path=str(out), columns=["a", "b"])
    assert out.read_text(encoding="utf-8").strip() == "a,b"


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="unknown format"):
        write_rows(ROWS, fmt="parquet")


def test_xlsx_to_stdout_is_refused():
    pytest.importorskip("openpyxl")
    with pytest.raises(SystemExit, match="file path"):
        write_rows(ROWS, fmt="xlsx", path=None)


def test_xlsx_round_trip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "o.xlsx"
    write_rows(ROWS, fmt="xlsx", path=str(out))
    sheet = openpyxl.load_workbook(out).active
    assert [cell.value for cell in sheet[1]] == ["a", "b", "c"]
