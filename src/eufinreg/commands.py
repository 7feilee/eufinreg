"""The operational verbs: ingest, watch, receipt, store, doctor, serve.

The flag-only CLI (``eufinreg --source upreg -o x.csv``) is the *reading* tool
and is unchanged — it is documented, people have it in scripts, and breaking it
to make room for subcommands would be a poor trade. These verbs sit alongside
it: if the first argument is one of them, it wins; otherwise the legacy parser
runs exactly as before.

Exit codes are part of the interface, because these run under cron:

===  ==========================================================================
0    everything asked for succeeded
1    at least one register failed, or a check did not pass
2    usage, configuration or input error — nothing was attempted
3    the store is locked by another run
===  ==========================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, merge_cli, resolve_preset
from .evidence import build_bundle, verify_bundle
from .flatten import column_order, flatten_auto
from .http import DEFAULT_USER_AGENT, Fetcher, force_ipv4
from .observability import Logger, new_run_id
from .output import FORMATS, write_rows
from .pipeline import ingest, report
from .sources import ALL_SOURCES, Query, UnknownSource, get_source
from .store import Store, StoreLocked
from .watchlist import Watchlist, normalise_identifier, normalise_name

VERBS = ("ingest", "watch", "receipt", "store", "runs", "doctor", "serve")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3


def is_verb(argv: Sequence[str] | None) -> bool:
    return bool(argv) and argv[0] in VERBS


# -- shared arguments ------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, store: bool = True) -> None:
    if store:
        parser.add_argument(
            "--store",
            metavar="DIR",
            help="snapshot store directory (default: ./eufinreg-store, or the config's)",
        )
    parser.add_argument("--config", metavar="PATH", help="JSON run configuration")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress on stderr")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON on stdout")
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="stderr format: human lines (default) or one JSON object per line, "
        "each carrying the run id",
    )


def _load_config(args: argparse.Namespace, *, source_keys: Sequence[str] = ()) -> Config:
    config = Config.load(args.config) if getattr(args, "config", None) else Config()
    return merge_cli(config, args, source_keys=source_keys)


def _logger(args: argparse.Namespace, *, run_id: str = "") -> Logger:
    """One logger, two renderings. The events are identical either way."""
    return Logger(
        fmt=getattr(args, "log_format", "text") or "text",
        quiet=bool(getattr(args, "quiet", False)),
        run_id=run_id,
    )


# -- ingest ----------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    run_id = new_run_id()
    log = _logger(args, run_id=run_id)
    if getattr(args, "ipv4", False):
        force_ipv4()
    try:
        config = _load_config(args, source_keys=args.source)
        if args.no_raw:
            config.capture_raw = False
        if not config.sources:
            config.sources = resolve_preset("dach")
            log("no sources configured; using the 'dach' preset")
    except (OSError, ValueError) as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if config.user_agent == DEFAULT_USER_AGENT:
        log(
            "note: using the default User-Agent. Set user_agent in the config before "
            "running this on a schedule — an unattended anonymous client is what gets "
            "IP ranges blocked."
        )

    store = Store(config.store)
    started = time.time()
    try:
        with store:
            log.event(
                f"ingesting {len(config.enabled_sources())} source(s) into {store.root}",
                run_id=run_id,
                sources=[spec.key for spec in config.enabled_sources()],
                store=str(store.root),
            )
            results = ingest(store, config, force=args.force, run_id=run_id, log=log)
            summary = report(results, run_id=run_id)
            summary["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
            summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # Written inside the lock: a run record is part of the run.
            store.write_run(summary, run_id=run_id)
            store.prune_runs()
    except StoreLocked as exc:
        log.error(str(exc))
        return EXIT_LOCKED
    except (OSError, ValueError) as exc:
        log.error(str(exc))
        return EXIT_USAGE

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        http = summary["http"]
        log.event(
            f"done: {summary['ok']}/{summary['sources']} ok, {summary['failed']} failed, "
            f"{summary['skipped']} skipped, "
            f"+{summary['added']} ~{summary['changed']} -{summary['removed']} "
            f"in {summary['seconds']}s",
            run_id=run_id,
            **{k: summary[k] for k in ("ok", "failed", "skipped", "added", "changed", "removed")},
        )
        log.event(
            f"http: {http['requests']} request(s), {http['retries']} retried, "
            f"{http['failures']} failed, {http['bytes_in'] / 1e6:.1f} MB in, "
            f"{http['seconds_requesting']:.1f}s on the wire, "
            f"{http['seconds_waiting']:.1f}s waiting",
            **http,
        )
        if summary["phases"]:
            breakdown = " ".join(f"{k}={v}s" for k, v in sorted(summary["phases"].items()))
            log.event(f"phases: {breakdown}", **summary["phases"])
    return EXIT_FAILED if summary["failed"] else EXIT_OK


# -- watch -----------------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    log = _logger(args)
    try:
        config = _load_config(args, source_keys=args.source)
        paths = [Path(p) for p in args.watchlist] or config.watchlists
        if not paths:
            print(
                "eufinreg: no watchlist given. Pass --watchlist FILE, or set "
                '"watchlists" in the config.',
                file=sys.stderr,
            )
            return EXIT_USAGE
        watchlist = _merge_watchlists(paths)
    except (OSError, ValueError) as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE

    store = Store(config.store)
    sources = args.source or store.sources()
    if not sources:
        print(
            f"eufinreg: {store.root} holds no snapshots yet — run 'eufinreg ingest' first.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    log(f"watching {len(watchlist.entities)} entit(ies) across {len(sources)} source(s)")
    if watchlist.collisions:
        log(
            f"warning: {len(watchlist.collisions)} key(s) are claimed by more than one watched "
            f"entity and were dropped — they cannot be attributed: "
            f"{', '.join(sorted(set(watchlist.collisions))[:5])}"
        )

    events: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for key in sources:
        try:
            source = get_source(key)
        except UnknownSource:
            continue
        if not source.identifier_columns and not source.name_columns:
            # A reference table (trade codes) or a register published without
            # holders (GISA). Reporting "0 of 4 matched" here would read as a
            # coverage failure rather than a category error.
            log(f"{key}: not entity-shaped — nothing in it can be matched to a watchlist")
            continue
        scoped = watchlist.scope(
            store.read_changes(key, since=args.since or ""),
            source=key,
            identifier_columns=source.identifier_columns,
            name_columns=source.name_columns,
        )
        events.extend(scoped)
        if args.coverage:
            latest = store.latest(key)
            if latest is not None:
                coverage.append(
                    watchlist.coverage(
                        latest.load().rows,
                        source=key,
                        identifier_columns=source.identifier_columns,
                        name_columns=source.name_columns,
                    )
                )

    for entry in coverage:
        note = (
            f"{entry['source']}: matched {entry['matched']}/{entry['watched']} watched entit(ies)"
        )
        if entry["by_name"]:
            note += f" ({entry['by_name']} by name only — weaker than an identifier match)"
        if entry["unmatched"]:
            note += f"; not found: {', '.join(entry['unmatched'][:8])}"
        log(note)

    if args.json:
        print(json.dumps({"events": events, "coverage": coverage}, ensure_ascii=False, indent=2))
        return EXIT_OK

    columns = column_order(events) if events else []
    written = write_rows(events, fmt=args.format, path=args.output, columns=columns)
    log(
        f"{written} event(s) affecting watched entities{' → ' + args.output if args.output else ''}"
    )
    if not events:
        log("no changes affecting the watchlist in this window")
    return EXIT_OK


def _merge_watchlists(paths: Sequence[Path]) -> Watchlist:
    merged = Watchlist(name=paths[0].stem if len(paths) == 1 else "merged")
    seen: set[str] = set()
    for path in paths:
        for entity in Watchlist.load(path).entities:
            if entity.id in seen:
                continue
            seen.add(entity.id)
            merged.entities.append(entity)
    merged.reindex()
    return merged


# -- receipt ---------------------------------------------------------------


def cmd_receipt(args: argparse.Namespace) -> int:
    log = _logger(args)
    if args.verify:
        ok, problems = verify_bundle(args.verify)
        for problem in problems:
            print(f"eufinreg: {problem}", file=sys.stderr)
        log("bundle verified: every member matches its recorded digest" if ok else "bundle FAILED")
        return EXIT_OK if ok else EXIT_FAILED

    try:
        config = _load_config(args)
        source = get_source(args.source)
    except (OSError, ValueError) as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except UnknownSource as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE

    store = Store(config.store)
    refs = store.snapshots(args.source)
    if not refs:
        print(
            f"eufinreg: no snapshots of {args.source!r} in {store.root}. "
            f"A receipt is issued from the archive, not from a live fetch — "
            f"run 'eufinreg ingest --source {args.source}' first.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    ref = next((r for r in refs if r.stamp == args.snapshot), None) if args.snapshot else refs[-1]
    if ref is None:
        print(
            f"eufinreg: no snapshot {args.snapshot!r}; have {', '.join(r.stamp for r in refs)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    snapshot = ref.load()
    rows, how = _find_subject(snapshot.rows, args.match, source)
    if not rows:
        print(
            f"eufinreg: nothing in the {ref.stamp} snapshot of {args.source} matches "
            f"{args.match!r}. Identifiers are matched on {', '.join(source.identifier_columns) or '—'}, "
            f"names on {', '.join(source.name_columns) or '—'}.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    out = Path(args.output or f"receipt-{args.source}-{ref.stamp}.zip")
    bundle = build_bundle(
        out,
        source=source,
        ref=ref,
        rows=rows,
        subject={"query": args.match, "matched_on": how, "rows": len(rows)},
        include_raw=not args.no_raw,
        query={"select": ref.meta.get("select", ""), "query": ref.meta.get("query", "")},
    )
    log(
        f"wrote {bundle.path} — {len(rows)} row(s) from the {ref.stamp} snapshot, "
        f"matched on {how}, bundle sha256 {bundle.digest[:16]}…"
    )
    if how == "name":
        log("warning: matched by name, not by identifier — a weaker claim, and the receipt says so")
    if args.json:
        print(
            json.dumps(
                {"path": str(bundle.path), "sha256": bundle.digest, "files": bundle.files},
                ensure_ascii=False,
                indent=2,
            )
        )
    return EXIT_OK


def _find_subject(
    rows: Sequence[dict[str, Any]], needle: str, source: Any
) -> tuple[list[dict], str]:
    """Identifier match first, name match second, and say which happened."""
    wanted_ids = normalise_identifier(needle)
    if wanted_ids:
        hits = [
            row
            for row in rows
            if any(
                normalise_identifier(str(row.get(column, "") or "")) & wanted_ids
                for column in source.identifier_columns
            )
        ]
        if hits:
            return hits, "identifier"
    wanted_name = normalise_name(needle)
    if wanted_name:
        hits = [
            row
            for row in rows
            if any(
                any(
                    normalise_name(part) == wanted_name
                    for part in str(row.get(column, "") or "").split(" | ")
                )
                for column in source.name_columns
            )
        ]
        if hits:
            return hits, "name"
    return [], ""


# -- runs ------------------------------------------------------------------


def cmd_runs(args: argparse.Namespace) -> int:
    """What happened on previous ingests, and where the time and errors went."""
    log = _logger(args)
    try:
        config = _load_config(args)
    except (OSError, ValueError) as exc:
        log.error(str(exc))
        return EXIT_USAGE

    store = Store(config.store)
    records = store.runs(limit=args.limit)
    if not records:
        log.event(f"no run history in {store.root} — records are written by 'eufinreg ingest'")
        return EXIT_OK

    if args.run_id or args.last:
        record = store.run(args.run_id) if args.run_id else records[-1]
        if record is None:
            log.error(f"no run {args.run_id!r}; try 'eufinreg runs' for the list")
            return EXIT_USAGE
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_FAILED if record.get("failed") else EXIT_OK

    if args.json:
        print(json.dumps({"runs": records}, ensure_ascii=False, indent=2))
        return EXIT_OK

    rows = []
    for record in records:
        http = record.get("http") or {}
        rows.append(
            [
                record.get("run_id", ""),
                record.get("finished_at", "") or record.get("written_at", ""),
                f"{record.get('ok', 0)}/{record.get('sources', 0)}",
                record.get("failed", 0),
                f"+{record.get('added', 0)} ~{record.get('changed', 0)} -{record.get('removed', 0)}",
                http.get("requests", 0),
                http.get("retries", 0),
                f"{record.get('seconds', 0)}s",
            ]
        )
    print(_table(["run", "finished", "ok", "failed", "changes", "reqs", "retries", "took"], rows))
    return EXIT_FAILED if any(r.get("failed") for r in records) else EXIT_OK


# -- store -----------------------------------------------------------------


def cmd_store(args: argparse.Namespace) -> int:
    log = _logger(args)
    try:
        config = _load_config(args)
    except (OSError, ValueError) as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE
    store = Store(config.store)

    if args.action == "status":
        cadence = {s.key: s.cadence_hours for s in ALL_SOURCES}
        rows = store.status(cadence)
        if args.json:
            print(
                json.dumps(
                    {"store": str(store.root), "sources": rows}, ensure_ascii=False, indent=2
                )
            )
        else:
            print(
                _table(
                    ["source", "snapshots", "latest", "rows", "age_h", "cadence_h", "state"],
                    [
                        [
                            r["source"],
                            r["snapshots"],
                            r["latest"] or "—",
                            r["rows"],
                            "—" if r["age_hours"] is None else f"{r['age_hours']:.1f}",
                            "—" if r["cadence_hours"] is None else f"{r['cadence_hours']:.0f}",
                            "STALE" if r["stale"] else "ok",
                        ]
                        for r in rows
                    ],
                )
            )
        return EXIT_FAILED if any(r["stale"] for r in rows) else EXIT_OK

    if args.action == "list":
        for key in [args.target] if args.target else store.sources():
            for ref in store.snapshots(key):
                print(f"{key}\t{ref.stamp}\t{ref.row_count}\t{ref.digest[:16]}")
        return EXIT_OK

    if args.action == "verify":
        results = store.verify(args.target)
        bad = [(ref, message) for ref, ok, message in results if not ok]
        for ref, message in bad:
            print(f"eufinreg: {ref.source}/{ref.stamp}: {message}", file=sys.stderr)
        log(f"verified {len(results)} snapshot(s), {len(bad)} problem(s)")
        return EXIT_FAILED if bad else EXIT_OK

    if args.action == "prune":
        if args.keep_days is None:
            print("eufinreg: prune needs --keep-days N", file=sys.stderr)
            return EXIT_USAGE
        total = 0
        for key in [args.target] if args.target else store.sources():
            removed = store.prune(
                key, keep_days=args.keep_days, keep_last=args.keep_last, dry_run=args.dry_run
            )
            total += len(removed)
            for path in removed:
                log(f"{'would remove' if args.dry_run else 'removed'} {path}")
        log(f"{total} snapshot(s) {'would be ' if args.dry_run else ''}removed")
        return EXIT_OK

    print(f"eufinreg: unknown store action {args.action!r}", file=sys.stderr)
    return EXIT_USAGE


# -- doctor ----------------------------------------------------------------


def _state(check: Mapping[str, Any]) -> str:
    """Three outcomes, not two. ``empty`` is a working read of a bare register."""
    if not check["ok"]:
        return "FAIL"
    return "empty" if check.get("empty") else "ok"


def cmd_doctor(args: argparse.Namespace) -> int:
    """Ask every register whether it is still the shape this client expects.

    This is the one command that must run against the live services: it exists
    precisely to catch the thing the offline test suite cannot, which is a
    register quietly changing its field names or its paging behaviour.
    """
    log = _logger(args)
    if getattr(args, "ipv4", False):
        force_ipv4()
    keys = args.source or [s.key for s in ALL_SOURCES]
    checks: list[dict[str, Any]] = []

    for key in keys:
        started = time.monotonic()
        entry: dict[str, Any] = {"source": key, "ok": False, "missing": [], "rows": 0, "note": ""}
        try:
            source = get_source(key)
        except UnknownSource as exc:
            entry["note"] = str(exc)
            checks.append(entry)
            continue

        fetcher = Fetcher(
            user_agent=args.user_agent, delay=args.delay, timeout=args.timeout, retries=args.retries
        )
        try:
            query = Query(raw_query=source.probe_query or None, max_docs=args.sample)
            records = list(source.iter_records(fetcher, query))
            rows = flatten_auto(
                records,
                source.flatten_config(),
                block_structured=True if source.block_structured else None,
            )
            present: set[str] = set()
            for row in rows:
                present.update(row.keys())

            # A register can be correctly read and legitimately contain nothing.
            # Where the source can name its columns independently of its rows,
            # the header is the shape, and an empty register is a fact about the
            # market rather than a fault in this client.
            columns = tuple(source.probe_columns(fetcher, query)) if not rows else ()
            if columns:
                present = set(columns)

            missing = [name for name in source.expected_fields if name not in present]
            entry.update(
                rows=len(rows),
                missing=missing,
                ok=bool(rows or columns) and not missing,
                note="",
            )
            if not rows and not columns:
                # Nothing came back and nothing can be said about the shape.
                # "Fields are missing" would be the wrong diagnosis: there was
                # no response to miss them from.
                entry["note"] = (
                    "the register returned no rows for the probe and no columns — "
                    "so there is no evidence the read worked at all"
                )
            elif missing:
                entry["note"] = (
                    f"missing expected field(s): {', '.join(missing)} — the register has "
                    f"changed shape and this client's output cannot be trusted"
                )
            elif columns:
                entry["note"] = (
                    f"the register is empty — no entries published. The read worked: "
                    f"all {len(columns)} columns are still served, including every "
                    f"field this client depends on"
                )
                entry["empty"] = True
        except Exception as exc:  # deliberately broad: a drift check that stops at the
            # first surprise is not a drift check. Schema drift usually arrives
            # as a KeyError inside a parser, which is exactly what this is for.
            entry["note"] = f"{type(exc).__name__}: {exc}"
        finally:
            fetcher.close()
            entry["seconds"] = round(time.monotonic() - started, 1)
        checks.append(entry)
        log(f"{key}: {_state(entry)} ({entry['rows']} rows) {entry['note']}")

    failed = [c for c in checks if not c["ok"]]
    if args.json:
        print(
            json.dumps(
                {"checked": len(checks), "failed": len(failed), "checks": checks},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            _table(
                ["source", "state", "rows", "secs", "note"],
                [
                    [
                        c["source"],
                        _state(c),
                        c["rows"],
                        c.get("seconds", ""),
                        c["note"][:70],
                    ]
                    for c in checks
                ],
            )
        )
    return EXIT_FAILED if failed else EXIT_OK


# -- serve -----------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve  # imported here: nothing else needs http.server

    try:
        config = _load_config(args)
    except (OSError, ValueError) as exc:
        print(f"eufinreg: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "ipv4", False):
        force_ipv4()
    if args.allow_personal_data:
        config.allow_personal_data = True
    for name in ("timeout", "retries"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config, name, value)
    return serve(
        port=args.port,
        host=args.host,
        log=_logger(args),
        config=config,
        token=args.token,
    )


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eufinreg",
        description="Operational commands: keep an archive of public registers, watch entities "
        "in it, and issue evidence from it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="COMMAND")

    p = verbs.add_parser("ingest", help="fetch every configured register into the store")
    _add_common(p)
    p.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="KEY",
        help="only this source (repeatable); overrides the config",
    )
    p.add_argument("--preset", metavar="NAME", help="source set: dach, all")
    p.add_argument("--user-agent", metavar="UA", help="identify yourself; required in production")
    p.add_argument("--delay", type=float, metavar="SEC", help="pause between requests")
    p.add_argument(
        "--retention-days",
        type=int,
        metavar="N",
        help="prune snapshots older than N days after a successful run",
    )
    p.add_argument("--no-raw", action="store_true", help="do not archive the raw response bodies")
    p.add_argument(
        "--force",
        action="store_true",
        help="fetch even when the archive entry is newer than the register's own "
        "publication cadence (by default a monthly file is not re-downloaded "
        "nightly)",
    )
    p.add_argument(
        "-4",
        "--ipv4",
        action="store_true",
        help="resolve IPv4 only. ESMA's CDN publishes AAAA records and Python has no Happy "
        "Eyeballs, so on a network without an IPv6 route a nightly run stalls for minutes "
        "per request",
    )
    p.set_defaults(func=cmd_ingest)

    p = verbs.add_parser("watch", help="report changes affecting a watchlist")
    _add_common(p)
    p.add_argument(
        "--watchlist",
        action="append",
        default=[],
        metavar="PATH",
        help="JSON or CSV watchlist (repeatable)",
    )
    p.add_argument("--source", action="append", default=[], metavar="KEY")
    p.add_argument(
        "--since",
        metavar="STAMP",
        help="only snapshots at or after this stamp (a date prefix such as 20260801 works)",
    )
    p.add_argument(
        "--coverage",
        action="store_true",
        help="also report which watched entities each register can actually see",
    )
    p.add_argument("-o", "--output", metavar="PATH")
    p.add_argument("--format", choices=FORMATS, default="csv")
    p.set_defaults(func=cmd_watch)

    p = verbs.add_parser("receipt", help="build an evidence bundle from the archive")
    _add_common(p)
    p.add_argument("--source", metavar="KEY", help="which register")
    p.add_argument("--match", metavar="VALUE", help="an identifier or a name")
    p.add_argument("--snapshot", metavar="STAMP", help="a specific snapshot (default: the newest)")
    p.add_argument("--no-raw", action="store_true", help="omit the raw response bodies")
    p.add_argument("--verify", metavar="BUNDLE.zip", help="check a bundle instead of building one")
    p.add_argument("-o", "--output", metavar="PATH")
    p.set_defaults(func=cmd_receipt)

    p = verbs.add_parser("runs", help="what previous ingests did, and how they went")
    _add_common(p)
    p.add_argument("run_id", nargs="?", metavar="RUN_ID", help="show one run in full")
    p.add_argument("--last", action="store_true", help="show the most recent run in full")
    p.add_argument("--limit", type=int, default=20, metavar="N", help="how many runs to list")
    p.set_defaults(func=cmd_runs)

    p = verbs.add_parser("store", help="inspect and maintain the archive")
    p.add_argument("action", choices=("status", "list", "verify", "prune"))
    p.add_argument("target", nargs="?", metavar="SOURCE")
    _add_common(p)
    p.add_argument("--keep-days", type=int, metavar="N")
    p.add_argument("--keep-last", type=int, default=2, metavar="N")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_store)

    p = verbs.add_parser("doctor", help="check every register is alive and unchanged in shape")
    _add_common(p, store=False)
    p.add_argument("--source", action="append", default=[], metavar="KEY")
    p.add_argument(
        "--sample",
        type=int,
        default=25,
        metavar="N",
        help="records to pull per register (default 25)",
    )
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT, metavar="UA")
    p.add_argument("--delay", type=float, default=1.0, metavar="SEC")
    p.add_argument("--timeout", type=float, default=60.0, metavar="SEC")
    p.add_argument("--retries", type=int, default=2, metavar="N")
    p.add_argument(
        "-4",
        "--ipv4",
        action="store_true",
        help="resolve IPv4 only; ESMA's CDN publishes AAAA records and Python has "
        "no Happy Eyeballs, so a network without an IPv6 route stalls",
    )
    p.set_defaults(func=cmd_doctor)

    p = verbs.add_parser("serve", help="run the read-only API and web UI")
    _add_common(p)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="default 127.0.0.1; binding wider shares your User-Agent with strangers",
    )
    p.add_argument("--token", metavar="TOKEN", help="require this bearer token on /api routes")
    p.add_argument(
        "--allow-personal-data",
        action="store_true",
        help="serve rows a source flags as personal data (off by default)",
    )
    # No argparse defaults on these four: a default would silently outrank the
    # config file, which is the one place an operator expects them to live.
    p.add_argument("--user-agent", metavar="UA")
    p.add_argument("--delay", type=float, metavar="SEC")
    p.add_argument("--timeout", type=float, metavar="SEC")
    p.add_argument("--retries", type=int, metavar="N")
    p.add_argument(
        "-4", "--ipv4", action="store_true", help="resolve IPv4 only; see 'ingest --help'"
    )
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("eufinreg: interrupted", file=sys.stderr)
        return 130


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in cells)) if cells else len(headers[i])
        for i in range(len(headers))
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()
    rule = "  ".join("-" * w for w in widths)
    body = [
        "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip() for row in cells
    ]
    return "\n".join([line, rule, *body])


__all__ = ["VERBS", "build_parser", "is_verb", "main"]
