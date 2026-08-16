"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .filters import FieldFilter, apply_filters
from .flatten import column_order, flatten_auto
from .http import DEFAULT_USER_AGENT, Fetcher, FetchError, RawRecorder, force_ipv4
from .output import FORMATS, write_rows
from .profile import profile_records, render_profile, render_values
from .sources import ALL_SOURCES, DEFAULT_SOURCE, Query, UnknownSource, get_source

EPILOG = """\
examples:
  # what can I read?
  eufinreg --list-sources

  # what fields does this register actually serve today?
  eufinreg --source upreg --inspect 200

  # what values may I filter on? (guessing wrong gives you 0 rows, silently)
  eufinreg --source upreg --list-values ae_entityTypeCode
  eufinreg --source mica-casp --list-enums

  # one row per licensed entity, activities collapsed into pipe-joined columns
  eufinreg --source upreg --select CSP -o crowdfunding.csv

  # BaFin-supervised entities, keeping the untouched responses for forensics
  eufinreg --source upreg --field ae_competentAuthority="Federal Financial Supervisory Authority (BaFin)" \\
           --raw ./raw -o bafin.csv

  # BaFin-supervised payment and e-money institutions, which upreg does not cover
  eufinreg --source eba-psd --field CA_OwnerID=DE_BAFIN -o bafin-payments.csv

  # schema-drift-proof search across every field
  eufinreg --source mica-casp --contains bybit --format json
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eufinreg",
        description=(
            "Fetch structured lists of licensed financial entities from the EU public "
            "registers (ESMA Registers A2A, ESMA interim MiCA register, EBA PSD2 register)."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        metavar="KEY",
        help=f"register to read (default: {DEFAULT_SOURCE}); "
        f"'solr:<core>' reaches any ESMA Solr core",
    )

    modes = parser.add_argument_group("modes")
    modes.add_argument("--list-sources", action="store_true", help="list known registers and exit")
    modes.add_argument(
        "--inspect",
        nargs="?",
        type=int,
        const=50,
        metavar="N",
        help="sample N records (default 50) and report the field names that really "
        "exist, how often they are populated, and example values",
    )
    modes.add_argument(
        "--list-values",
        action="append",
        default=[],
        metavar="FIELD",
        help="print the distinct values of FIELD with counts (repeatable)",
    )
    modes.add_argument(
        "--list-enums",
        action="store_true",
        help="print distinct values for every field this source treats as an enumeration",
    )

    selection = parser.add_argument_group("selection (pushed to the server where supported)")
    selection.add_argument("--select", metavar="VALUE", help="source-specific server-side selector")
    selection.add_argument(
        "--query", metavar="Q", help="raw source-native query; passed through untouched"
    )
    selection.add_argument(
        "--include-history",
        action="store_true",
        help="also fetch historic/withdrawn child records where the source has them",
    )

    filtering = parser.add_argument_group("filtering (client-side, applied after flattening)")
    filtering.add_argument(
        "--contains",
        action="append",
        default=[],
        metavar="TEXT",
        help="keep rows where TEXT appears in ANY field (case-insensitive, repeatable, ANDed)",
    )
    filtering.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="keep rows where NAME equals VALUE exactly (case-insensitive, repeatable)",
    )
    filtering.add_argument("--limit", type=int, metavar="N", help="stop after N output rows")

    out = parser.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="PATH", help="write to PATH (default: stdout)")
    out.add_argument(
        "--format", choices=FORMATS, default="csv", help="output format (default: csv)"
    )
    out.add_argument(
        "--raw",
        metavar="DIR",
        help="also save every untouched response body to DIR, with a manifest.jsonl",
    )
    out.add_argument(
        "--no-flatten",
        action="store_true",
        help="emit records exactly as received instead of one row per entity",
    )
    out.add_argument(
        "--no-derived",
        action="store_true",
        help="do not add derived helper columns (e.g. ac_serviceCode_normalised)",
    )

    net = parser.add_argument_group("network etiquette")
    net.add_argument(
        "--delay", type=float, default=1.0, metavar="SEC", help="pause between requests (default 1)"
    )
    net.add_argument(
        "--page-size", type=int, default=500, metavar="N", help="records per request (default 500)"
    )
    net.add_argument(
        "--max-docs", type=int, metavar="N", help="stop pulling after N records from the wire"
    )
    net.add_argument("--retries", type=int, default=5, metavar="N", help="retries (default 5)")
    net.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SEC",
        help="per-request timeout (default 60)",
    )
    net.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        metavar="UA",
        help="please change this to your own repository or contact address",
    )
    net.add_argument(
        "-4",
        "--ipv4",
        action="store_true",
        help="resolve hostnames to IPv4 only, like curl -4; use this if requests hang on "
        "a network without a working IPv6 route (ESMA's CDN publishes AAAA records)",
    )
    net.add_argument("-q", "--quiet", action="store_true", help="suppress progress on stderr")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_sources:
        print(render_sources())
        return 0

    try:
        source = get_source(args.source)
    except UnknownSource as exc:
        parser.error(str(exc))

    try:
        field_filters = [FieldFilter.parse(text) for text in args.field]
    except ValueError as exc:
        parser.error(str(exc))

    log = _make_logger(args.quiet)
    if args.ipv4:
        force_ipv4()
    recorder = RawRecorder(args.raw) if args.raw else None
    fetcher = Fetcher(
        user_agent=args.user_agent,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        recorder=recorder,
    )
    if args.user_agent == DEFAULT_USER_AGENT:
        log(
            "note: using the default User-Agent. Please set --user-agent to your own "
            "repository or contact address."
        )

    query = Query(
        select=args.select,
        raw_query=args.query,
        include_history=args.include_history,
        max_docs=args.max_docs,
        page_size=args.page_size,
        extra={"no_derived": args.no_derived},
    )

    try:
        if args.list_enums or args.list_values:
            return _run_list_values(source, fetcher, query, args, log)
        if args.inspect is not None:
            return _run_inspect(source, fetcher, query, args, log)
        return _run_fetch(source, fetcher, query, args, field_filters, log)
    except FetchError as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # A source rejecting a --select / --query it cannot express. That is a
        # usage error, and deserves the message rather than a traceback.
        print(f"eufinreg: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("eufinreg: interrupted", file=sys.stderr)
        return 130
    finally:
        fetcher.close()


# -- modes -----------------------------------------------------------------


def _run_list_values(source, fetcher, query, args, log) -> int:
    fields = list(args.list_values)
    if args.list_enums:
        if not source.enum_fields:
            log(
                f"source {source.key!r} declares no enum fields; "
                f"use --inspect to find candidates, then --list-values FIELD"
            )
        fields = [f for f in source.enum_fields if f not in fields] + fields
    if not fields:
        return 1

    chunks = []
    for name in fields:
        pairs, note = source.count_values(fetcher, query, name)
        chunks.append(render_values(name, pairs, source=source.key, note=note))
    print("\n".join(chunks))
    return 0


def _run_inspect(source, fetcher, query, args, log) -> int:
    sample_size = max(1, args.inspect)
    probe = Query(**{**query.__dict__, "max_docs": _probe_size(query, sample_size)})
    log(f"sampling up to {probe.max_docs} record(s) from {source.key}…")
    records = list(source.iter_records(fetcher, probe))

    raw_total, raw_profiles = profile_records(records[:sample_size])
    print(
        render_profile(
            raw_total,
            raw_profiles,
            title=f"{source.key} — fields as received from the register\n  {source.docs_url}",
        )
    )

    if source.block_structured or any("type_s" in r for r in records):
        rows = flatten_auto(records, source.flatten_config())
        flat_total, flat_profiles = profile_records(rows[:sample_size])
        print(
            render_profile(
                flat_total,
                flat_profiles,
                title=f"{source.key} — columns after flattening to one row per entity",
            )
        )
    return 0


def _run_fetch(source, fetcher, query, args, field_filters, log) -> int:
    log(f"fetching {source.key} ({source.title})…")
    records = list(source.iter_records(fetcher, query))
    log(f"received {len(records)} record(s)")

    if args.no_flatten:
        rows: list[dict[str, Any]] = [dict(r) for r in records]
    else:
        rows = flatten_auto(
            records,
            source.flatten_config(),
            block_structured=True if source.block_structured else None,
        )
        if source.block_structured:
            log(f"flattened into {len(rows)} entity row(s)")

    filtered, report = apply_filters(
        rows, contains=args.contains, field_filters=field_filters, limit=args.limit
    )
    for warning in report.warnings():
        log(f"warning: {warning}")

    columns = column_order(rows) if rows else []
    written = write_rows(filtered, fmt=args.format, path=args.output, columns=columns)
    log(f"wrote {written} row(s){' to ' + args.output if args.output else ''}")
    return 0


# -- helpers ---------------------------------------------------------------


def _probe_size(query: Query, sample_size: int) -> int:
    """Pull a few extra documents so a block-structured core yields whole entities."""
    return min(query.max_docs or 10_000, max(sample_size * 4, sample_size + 50))


def render_sources() -> str:
    width = max(len(s.key) for s in ALL_SOURCES)
    lines = ["known registers:", ""]
    for source in ALL_SOURCES:
        lines.append(f"  {source.key.ljust(width)}  {source.title}")
        lines.append(f"  {' '.ljust(width)}  docs: {source.docs_url}")
        if source.select_help:
            lines.append(f"  {' '.ljust(width)}  --select: {source.select_help}")
    lines += [
        "",
        "  solr:<core>     any ESMA Solr core by name, e.g. solr:esma_registers_priii_documents",
        "",
    ]
    return "\n".join(lines)


def _make_logger(quiet: bool):
    def log(message: str) -> None:
        if not quiet:
            print(f"eufinreg: {message}", file=sys.stderr)

    return log


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
