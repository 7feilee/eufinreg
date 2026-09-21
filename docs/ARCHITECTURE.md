# Frontend / backend structure

`eufinreg` started as a CLI: one process, one question, one CSV. This document
is about what happens when the same code has to serve a browser — what changes,
what must not change, and which parts of it exist today.

**Status.** All five layers below are implemented and covered by tests as of
0.5.0. What is *not* built is listed honestly at the end — notification
delivery, multi-tenancy, and signing.

---

## The one constraint everything follows from

These registers are small public-sector deployments. ESMA publishes no rate
limit; EUDAMED is an application backend serving a UI that a handful of people
use at a time; the GISA endpoint hands out a 9 MB archive per request. None of
them has a quota system that will protect them from you — which means the
protection has to be on this side.

A CLI satisfies that by being what it is: one process, sequential requests, a
1-second delay, then exit. **A web front end does not get that for free.** Every
click is a request, every user multiplies it, and the naive shape — browser →
API → register — turns a tool into an unattended crawler with a shared
User-Agent.

So the architecture is not "add a REST layer to the CLI". It is:

> **Fetching is scheduled. Serving is local. The API never waits on a
> register.**

Everything below is a consequence.

---

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ 5. ingest (scheduled)          eufinreg ingest  ←── cron / systemd   │
│    writes snapshots + raw bodies + change events; the ONLY thing     │
│    that talks to a register on a schedule rather than on demand      │
└───────────────┬──────────────────────────────────────────────────────┘
                │ snapshot store (JSONL + .sha256 + raw/manifest.jsonl)
┌───────────────▼──────────────────────────────────────────────────────┐
│ 1. eufinreg.sources     register knowledge. Endpoints, paging, the   │
│    (+ eufinreg.flatten)  traps, the flattening. Knows nothing about  │
│                          HTTP servers, caches or users.              │
├──────────────────────────────────────────────────────────────────────┤
│ 2. eufinreg.service     long-running-client policy: cache with a     │
│                          per-source TTL, one outbound request at a   │
│                          time, bounded result sets, catalogue.       │
├──────────────────────────────────────────────────────────────────────┤
│ 3. eufinreg.server      HTTP. Routes, content types, status codes.   │
│                          Nothing register-specific. ~150 lines.      │
├──────────────────────────────────────────────────────────────────────┤
│ 4. eufinreg/web/        one HTML file. Talks only to the JSON API,   │
│    index.html            so it is replaceable and not privileged.    │
└──────────────────────────────────────────────────────────────────────┘
```

The layering rule is testable and worth keeping: **no register name, URL, field
name or quirk may appear above layer 1.** `service.py` mentions source *keys*
(for cache TTLs and the "this one is expensive" list) and nothing else. That is
what makes the CLI and the API the same product rather than two implementations
that drift.

### 1. Sources

Unchanged by any of this. `Source.iter_records()` yields records; `count_values()`
answers value questions server-side where the register can. Adding FINMA, the
Swiss UID register and GISA required no change to layers 2–4 — they appeared in
the API and the UI automatically, including their `--select` help text and their
enum fields, because the catalogue is generated from the source objects.

### 2. Service — the part a CLI does not need

Three properties, three reasons:

| Property | Why |
|---|---|
| **Cache, TTL per source** | The registers' own cadence: EBA nightly → 6 h, MiCA weekly → 12 h, GISA monthly → 24 h, `ch-uid` (a live lookup) → 15 min. Caching a monthly file for an hour is 700 pointless downloads a month. |
| **One outbound request at a time** | A `threading.Lock` around the fetch, with a double-check inside it. Four browser tabs asking the same question produce **one** register request, not four paging loops. There is a test for this. |
| **Bounded answers** | `max_rows` is enforced in the service, not the handler. A public endpoint must be able to say no. |

Client-side filters (`contains`, `field`, `limit`) are deliberately **not** part
of the cache key: they are applied to a cached answer, so refining a search
costs nothing and reaches no register.

### 3. Server

`http.server.ThreadingHTTPServer`. No framework — this project has one runtime
dependency and a browser is not a reason to add five. It binds to loopback on
purpose: exposing it to a network means many people sharing one User-Agent,
which is precisely the thing that gets an IP range blocked.

```
GET /                     the UI
GET /api/health           version + archive freshness per source; degraded if stale
GET /api/sources          catalogue, generated from the source objects
GET /api/rows?source=…    &select= &query= &contains= &field= &limit= &format=csv&live=1
GET /api/values?source=…&field=…
GET /api/changes?source=…&since=…    recorded events, archive only
GET /api/cache            what is cached and for how long
```

Error codes carry meaning, because at 2am the difference matters:
`400` you asked for something this source cannot express · `401` no bearer token
when one is required · **`403` this selection is flagged as personal data and
the server was not started with `--allow-personal-data`** · `404` no such source
· **`502` the register said no** — with `url` and `upstream_status` in the body,
so "our server is broken" and "ESMA is down" are distinguishable.

Two production behaviours live here rather than in a deployment guide, because a
guide is advice and code is a rule: binding to anything but loopback **without**
`--token` is refused outright, and `/api/health` reports per-source archive
staleness measured against each register's own cadence.

### 4. Frontend

One file, no build step, no CDN, works offline. It renders three things the CLI
prints to stderr and that a prettier UI would have dropped:

* **warnings**, at the top, in a colour — CTIS's 10,000-record truncation, the
  UID register's 30-record ceiling, GISA's kept-of-scanned count;
* **provenance** — when the answer was fetched and until when it is cached, on
  every result;
* **cost, before the click** — "this register downloads a 19 MB archive and
  parses 217 MB of JSON".

A register browser that hides truncation is worse than no register browser, and
that is a UI decision as much as a backend one.

### 5. Ingest — `eufinreg ingest`

The piece that makes the rest coherent, and the reason the read path is allowed
to be simple.

```
timer (04:12 UTC, randomised)
  └─ eufinreg ingest --config /etc/eufinreg/eufinreg.config.json --json
       lock the store, then for each configured source, sequentially:
         fetch  → one request at a time, delay between, retries with backoff
         write  → snapshot: canonical sorted JSONL + .sha256, written then renamed
         write  → raw: untouched response bytes + manifest.jsonl
         diff   → against the previous snapshot, column by column
         write  → changes: one file per run, even when nothing changed
         prune  → past retention, never below keep_last
       one failing source does not stop the others; exit 1 says one did
```

With that running, the read path stops touching registers: `serve --store …`
answers from the archive and `?live=1` is an explicit opt-in. The cache in layer
2 remains for the live path and for installations with no store yet.

**Store shape.** Files first, database second — the files are the record of what
a register actually said, and they are the thing you can hand to an auditor:

```
store/
  eufinreg-store.json           format version, created_at
  .lock                         held for the duration of a run
  finma/
    snapshots/20260816T041200Z.jsonl        sorted rows, _meta with body_sha256
    snapshots/20260816T041200Z.jsonl.sha256
    raw/20260816T041200Z/00001_finma.csv + manifest.jsonl
    changes/20260816T041200Z.jsonl          added / changed / removed
```

Four properties worth naming, because each prevents a specific failure:

* **Timestamped UTC filenames that sort chronologically** — `parse_stamp` uses
  `calendar.timegm`, not `mktime`, so the archive does not shift by an hour
  twice a year.
* **Write-then-rename with an fsync** — a run killed mid-download cannot leave a
  truncated snapshot that reads as a register losing 40% of its entries.
* **A lock with a PID and a start time**, reclaimed after six hours — cron
  overlap is the normal way two runs collide.
* **A changes file every run, including empty ones** — "we looked and nothing
  moved" is a fact worth being able to prove; an absent file cannot express it.

A database (SQLite is enough for DACH volumes: ~1.1 M GISA rows, 2.8 k FINMA,
14 k ESMA entities) would be an **index over** those files, rebuildable from
them, never the system of record.

---

## Identity, which is the hard part

A diff is only meaningful if you can say two rows are the same entity. Sources
declare `key_columns`:

| Source | Key | Note |
|---|---|---|
| `upreg`, funds, … | `id` | ESMA's own document id |
| `eba-psd` | `EntityCode` | e.g. `IE_CBI!C58301` |
| `eudamed-eo/-nb` | `eudamedIdentifier` | the SRN |
| `ctis` | `ctNumber` | trial, not company |
| `finma` | `UID`, `Name` | 84 of 2,938 rows carry no UID |
| `mica-casp` | `ae_lei`, `ae_lei_name` | LEI is frequently empty |
| `gleif` | `lei` | the strongest key here — it joins the two above |
| `ted` | `publication-number` | a notice, not a company |
| `vies` | *(none)* | a check, not a record; not archivable |
| `ch-uid` | *(none)* | one UID legitimately returns several seats |
| `gisa` | *(none)* | licences are published without holders |

Where there is no key, `row_key()` falls back to a hash of the row's content.
That still detects appearances and disappearances but **can never report a
modification** — and the tooling says so out loud rather than inventing events.
Cross-register identity (is FINMA's `CHE-101.329.561` the same firm as this
ESMA record?) is a separate, harder problem, and the answer is to use identifiers
the registers themselves publish rather than to invent one:

* **LEI** joins `upreg`, `mica-casp` and `gleif` — and GLEIF carries the entity's
  *national* register number on top (`entity.registeredAs`), which is how a
  German record reaches the Handelsregister.
* **UID** joins `finma` and `ch-uid` for Switzerland.
* **VAT** is a check rather than a join: `vies` confirms an identifier still
  belongs to a live registration.

Where nothing is shared, matching falls back to normalised names and says so on
the row. Nothing here guesses.

---

## Observability, and where it lives

The rule is the same one the rest of the layering follows: **measure where the
knowledge is, aggregate upwards.**

| Layer | What it knows | What it records |
|---|---|---|
| `eufinreg.http` | retries, waits, status codes, bytes | every attempt, into a `Metrics` on the fetcher |
| `eufinreg.pipeline` | what a run is trying to do | phase timings per source; harvests the fetcher's counters |
| `eufinreg.store` | what happened before | one JSON run record per ingest, beside the data |
| `eufinreg.service` | what a long-running process has done since boot | merges each request-scoped fetcher's counters on close |
| `eufinreg.server` | who asked for what | per-route, per-outcome counters; `/api/metrics` |

Nothing re-derives a number a lower layer already had — a count that exists in
two places eventually disagrees with itself, and then neither can be trusted.

Three properties worth keeping:

* **`run_id` is threaded through everything** — logs, run record, and the
  snapshot's own `_meta`. Given a row that looks wrong, you can find the run that
  wrote it; given a run, you can find every row it wrote.
* **Waiting is counted separately from wire time.** They mean opposite things:
  waiting is politeness working as designed, wire time is the register's own
  speed. One number would hide both.
* **Health reports the writer.** `/api/health` is `degraded` when the last ingest
  failed or a source is past twice its register's cadence — because the job
  filling the archive is what usually breaks, not the process serving it.

The one deliberate omission is a metrics *backend*. `Metrics` is a few counters
and a bounded list; it renders as Prometheus text on request and is otherwise
free. A client library, a registry and a push gateway would together be larger
than this project.

## Failure isolation, tested as a promise

The pipeline's docstring says one failing register does not stop the run. That
was true only for the exceptions somebody had thought of — and schema drift, the
most likely failure, arrives as a `KeyError` inside a parser. The catch is now
`Exception`, so *any* failure fails only its own source and lands in the run
record with a traceback; `KeyboardInterrupt` and `SystemExit` still propagate,
because those are the operator talking. There is a parametrised test asserting
exactly that, because a promise in a docstring is not a promise.

## What production needs that this does not have

Listed as gaps rather than pretended away:

1. **A real HTTP server.** `http.server` is a development server. Put gunicorn/
   uvicorn behind nginx, or serve pre-rendered snapshots as static files — the
   store's layout makes that entirely viable.
2. **Multi-tenancy.** One bearer token, one archive, one config. Fine for a team;
   not a hosted service.
3. **Notification delivery.** `watch` writes a CSV or JSON; nothing emails it,
   posts it to Slack or calls a webhook. That is the next thing to build.
4. **Signing and trusted timestamps.** Evidence bundles are self-verifying and
   reproducible, and say plainly that they are neither signed nor
   third-party-timestamped. Faking that language would be worse than lacking it.
5. **Cross-register entity resolution.** Identifier and normalised-name matching,
   with the kind labelled on every row and unmatched entities reported. Anything
   cleverer needs to earn its false positives.
6. **A `robots.txt` conscience for our own surface.** If this is ever public, it
   should not become the thing that gets scraped in place of the registers.

Solved since the first draft of this document, and worth naming because each was
listed here as a gap: the scheduled ingest and store, archive-backed reads,
personal-data gating in code, and staleness in the health endpoint.

## Non-goals

* Concurrency in the fetch path. Ever.
* A second implementation of any register in the API layer.
* Serving personal data by default (see 4 above).
* Hiding a register's limits behind a nicer UI. The truncation warnings are the
  product, not the rough edges.
