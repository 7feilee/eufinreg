"""Writers. CSV/JSON/JSONL need nothing beyond the standard library."""

from __future__ import annotations

import csv
import io
import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from .flatten import column_order, scalar

FORMATS = ("csv", "json", "jsonl", "xlsx")


@contextmanager
def open_output(path: str | None):
    """Yield a writable text stream — stdout when ``path`` is ``None`` or ``-``."""
    if path in (None, "-"):
        yield sys.stdout
        return
    target = Path(str(path))
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        yield handle


def write_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    fmt: str = "csv",
    path: str | None = None,
    columns: Sequence[str] | None = None,
) -> int:
    """Write ``rows`` and return the number written."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    cols = list(columns) if columns is not None else column_order(rows)

    if fmt == "xlsx":
        return _write_xlsx(rows, cols, path)

    with open_output(path) as handle:
        if fmt == "csv":
            return _write_csv(rows, cols, handle)
        if fmt == "jsonl":
            return _write_jsonl(rows, handle)
        return _write_json(rows, handle)


def rows_to_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
    """Same CSV, as a string — for callers that serve it rather than save it."""
    buffer = io.StringIO()
    _write_csv(rows, list(columns) if columns is not None else column_order(rows), buffer)
    return buffer.getvalue()


def _write_csv(rows: Sequence[Mapping[str, Any]], cols: Sequence[str], handle: TextIO) -> int:
    writer = csv.DictWriter(handle, fieldnames=list(cols), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: scalar(row.get(name, "")) for name in cols})
    return len(rows)


def _write_jsonl(rows: Sequence[Mapping[str, Any]], handle: TextIO) -> int:
    for row in rows:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _write_json(rows: Sequence[Mapping[str, Any]], handle: TextIO) -> int:
    json.dump(list(rows), handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    return len(rows)


def _write_xlsx(rows: Sequence[Mapping[str, Any]], cols: Sequence[str], path: str | None) -> int:
    try:
        from openpyxl import Workbook
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
        raise SystemExit("xlsx output needs the optional dependency: uv sync --extra xlsx") from exc
    if path in (None, "-"):
        raise SystemExit("xlsx output needs a file path: use --output entities.xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "entities"
    sheet.append(list(cols))
    for row in rows:
        sheet.append([scalar(row.get(name, "")) for name in cols])
    workbook.save(str(path))
    return len(rows)
