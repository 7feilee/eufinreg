# Changelog

Dates are the day the behaviour was verified against the live registers.

## Unreleased

### Added — observability

* **`eufinreg.observability`**: metrics, phase timing, run ids and a logger that
  renders the same events as human lines or as JSON.
* **Every HTTP attempt is counted** where it happens, in the fetcher: requests,
  retries, failures, bytes, status codes, hosts, the five slowest requests, and
  **time waiting kept separate from time on the wire** — a lot of waiting is
  politeness working, the reverse is a register in trouble.
* **Phase timings per source** (`fetch` / `flatten` / `write` / `diff`). "It took
  90 seconds" is not a diagnosis; "88 of them in fetch" is.
* **`eufinreg runs`** — every ingest now writes a record next to the data it
  produced: per-source outcome, timings, HTTP counters, warnings and the tail of
  a traceback when something failed unexpectedly.
* **`--log-format json`** on the operational verbs, with a `run_id` stamped into
  the logs, the run record *and* the snapshot metadata.
* **`GET /api/metrics`** in Prometheus text, including archive gauges
  (`archive_sources`, `archive_stale`, `archive_rows`) and per-route API counters.
* **`/api/health` now reports the writer**, not just the reader: the last run's
  outcome makes the service `degraded`, because the job filling the archive is
  what usually breaks. The web UI's status line shows it too.
* **`HEAD` is answered** — `BaseHTTPRequestHandler` returns 501 without it, and a
  load balancer's health check would mark the node down.
* A `tests/test_server.py` suite against a real socket with a stubbed service:
  status codes, headers, bearer auth, traversal, and the Prometheus body.
* **`-4` / `--ipv4` on `ingest` and `serve`**, not just on `doctor` and the
  reading CLI. ESMA's CDN publishes AAAA records and Python has no Happy
  Eyeballs, so a nightly run on a host without an IPv6 route stalled per request
  with no way to say so.

Measured on one live DACH run afterwards: 115 requests, 0 retries, 66.7 MB in,
**16.4 s on the wire against 107.7 s waiting** — the run is 87% politeness, which
is the design working rather than a register being slow. Of `upreg`'s 118
seconds, all 118 are `fetch` across 108 requests; of `gisa`'s 30, nineteen are
`write`, gzipping 607 MB. Neither is visible from a wall-clock total.

### Fixed — edge cases

Each of these was found by probing the real code, and each produced a wrong
answer rather than an error:

* **A source failing in an unplanned way aborted the entire run.** Only
  `FetchError`, `ValueError` and `OSError` were caught, but schema drift arrives
  as a `KeyError` or `TypeError` inside a parser — so one register changing shape
  stopped every register after it, breaking the isolation the pipeline promises.
  Now any exception fails only its own source; `KeyboardInterrupt` still
  propagates.
* **Identity could be forged across a diff.** A key column containing the
  `\x1f` separator let one entity impersonate another (`("x\x1fy", "z")` and
  `("x", "y\x1fz")` produced one key), which would attribute a licence
  withdrawal to the wrong company. Parts are escaped before joining.
* **`None` in a key column became the string `"None"`**, collapsing every row
  with a missing identifier into one entity. It now falls back to content
  hashing, as intended.
* **A `Retry-After` header was honoured without limit.** `Retry-After: 86400`
  would have parked a nightly ingest for a day; capped at 300 s.
* **Raw-capture labels went into filenames unsanitised**, so a source key with a
  slash could write outside the directory it was given.
* **Evidence bundles are checked for unsafe member names**, on build and on
  verify. A member called `../../x` verifies fine against its own digest and is
  still an attack on whoever extracts it.
* **`retention_days: -5` and `keep_last: 0` were accepted**, and between them
  would empty an archive. Both are now refused, and `prune` refuses a negative
  window whatever calls it.
* **Duplicate identifiers in one snapshot were silently ignored** by the diff.
  They are now counted and reported in the summary and as a run warning.
* **A watchlist naming one id twice** is refused rather than resolved
  arbitrarily.
* **`bytes` values reached the CSV writer verbatim**; they are now decoded.
* **TED's unexpected nested objects** were rendered as Python reprs instead of
  JSON.

## 0.5.0 — 2026-08-16

The release that turns a register client into something that can be run
unattended and pointed at a market. See [docs/OPERATIONS.md](docs/OPERATIONS.md)
for the runbook and [docs/BUSINESS.md](docs/BUSINESS.md) for the niche it is
shaped around: **DACH licence monitoring for compliance teams**.

### Added — DACH registers

* **`finma`** — FINMA authorisation holders (Switzerland). A CSV at a stable URL,
  regenerated daily. 2,938 authorisations across 37 licence types, collapsed to
  one row per institution, with the Swiss UID as a join key. Switzerland is
  outside the EEA, so no EU-level register contains a Swiss licence.
* **`ch-uid`** — the Swiss UID register (BFS) over its public SOAP service, no
  authentication. Legal form, seat, commercial-register and VAT status, plus
  VAT-group and headquarters/branch relationships. **Warns when its undocumented
  30-record ceiling truncates a search**, and handles `GetByUID` returning an
  array (UBS AG comes back as two seats).
* **`gisa`** / **`gisa-codes`** — every active Austrian trade licence
  (1,030,111 on 2026-08-16), CC BY 4.0. Decompresses the 7-Zip archive the
  `/csv` path actually returns, and filters **during the parse** so a million
  rows never land in memory. Needs the new `at` extra (`py7zr`).
* Documented and deliberately **not** wrapped: Zefix (documented REST API, HTTP
  401, host-wide `Disallow: /`) and BASG/MiA (clean REST API behind a token
  hardcoded in its public JS bundle).

### Added — upstream sources (identity, verification, public money)

Not licence lists: three other kinds of compulsory filing, chosen because each
sits *earlier* on the chain than a licence does.

* **`gleif`** — the LEI register: 3,403,760 legal entities, **CC0**, no key.
  The `lei` joins ESMA and MiCA rows to a real entity, and `entity.registeredAs`
  carries the firm's own **Handelsregister number** — a route to the German
  commercial-register identifier that BaFin's `Disallow: /` portal denies.
  Enforces and reports GLEIF's two stated limits (200 per page, 10,000 results),
  and points at the daily bulk file when a country exceeds them. It is also the
  only register in this project that **publishes its own deltas**.
* **`vies`** — EU VAT validation. Declares itself non-archivable, and
  **distinguishes `INVALID` from `MS_UNAVAILABLE`**: a member state's system
  being down is not a company being unregistered, and filing it as one is the
  failure this source exists to prevent.
* **`ted`** — EU public procurement notices with the winning company named,
  the earliest signal in the repository. Handles TED's alpha-3 country codes
  (everything else here uses alpha-2), its 250-row limit, its multilingual
  field objects, and `winner-name` repeating once per lot. **No Switzerland**:
  TED is EU/EEA only, which is the same gap FINMA fills on the licensing side.

### Added — the operational verbs

* **`eufinreg ingest`** — scheduled fetch into a snapshot store: canonical
  sorted JSONL with a SHA-256, the untouched response bytes, column-level change
  events, retention, and a lock. One failing register does not stop the run.
* **`eufinreg watch`** — change events scoped to a watchlist of entities, with
  identifier vs name match confidence on every row and a `--coverage` report
  naming the entities no register can see.
* **`eufinreg receipt`** — reproducible, self-verifying evidence bundles: the
  row, the raw response bytes, the checksums, the register's own disclaimer.
  `--verify` checks one.
* **`eufinreg store`** — `status` (freshness against each register's cadence),
  `list`, `verify`, `prune`.
* **`eufinreg doctor`** — checks every register is alive and still has the fields
  this client depends on. The one command that must reach the live services.
* **`eufinreg serve`** — now archive-backed: with a store configured it answers
  from the archive and contacts no register unless asked with `?live=1`.
* Exit codes are part of the interface: `0` ok, `1` something failed, `2` usage,
  `3` store locked.

### Added — earlier in this cycle

* `--snapshot` / `--diff` on the reading CLI, and the `eufinreg.snapshot` module.
* The read-only HTTP API and single-file web UI (`eufinreg.service`,
  `eufinreg.server`, `eufinreg/web/index.html`).
* JSON run configuration with a `dach` preset, and example crontab/systemd units
  in [`examples/`](examples).

### Changed

* Sources now declare operational metadata: jurisdiction, refresh cadence, data
  licence, publisher disclaimer, personal-data note, identifier and name columns,
  the fields `doctor` checks, and whether the register can be archived at all.
* **Personal-data gating is in code, not in a README warning.** The HTTP API
  refuses `eba-psd --select ALL` and `eudamed-eo` with `403` unless started with
  `--allow-personal-data`. The gate is per selection: EBA institutions are
  companies, EBA agents are people.
* Binding the server to anything other than loopback without `--token` is
  refused rather than warned about.
* Snapshots are written then renamed, with an fsync, so a killed run cannot leave
  a truncated archive entry.
* **Snapshots are gzipped** with a fixed mtime, so they stay byte-stable while
  taking a tenth of the space — Austria's 1.03 M trade licences go from 607 MB
  to 58 MB. `_meta.body_sha256` still covers the logical rows, and the `.sha256`
  sidecar now covers the file exactly as written, so `sha256sum -c` works
  without this tool. (It did not before: the sidecar recorded the body digest
  against the file name, and the file also contains the header line.)
* **A register is not re-fetched while its archive entry is newer than half its
  own publication cadence.** GISA is a monthly file; downloading it nightly was
  29 pointless requests and 29 identical 58 MB snapshots a month. `--force`
  overrides. Sources that declare no cadence (ESMA) are always fetched.
* Archived raw bodies get a sensible extension even when the register's
  `Content-Type` is malformed (`application/application/x-7z-compressed` → `.7z`).
* `Fetcher` gained `post_soap()` and raw `data`/`headers` for SOAP registers.

### Documentation

* The README's premise is restated as a test that can be applied to any source —
  **was this published because somebody wanted to, or because they were obliged
  to?** — with the three properties that follow (coverage, earliness,
  comparability), a ranking of sources by what a lie would cost the publisher,
  and a nine-category map of where the next source comes from.

### Compatibility

* The flag-only CLI (`eufinreg --source upreg -o x.csv`) is unchanged. Verbs are
  additive: the first argument decides which parser runs, and there is a test
  that the old form still works.
* New optional extra `at` for the Austrian 7-Zip archive. `--all-extras` in CI.

## 0.4.0 and earlier

ESMA Solr A2A, the interim MiCA CSV register, the EBA PSD2 golden copy with
checksum verification, EUDAMED and CTIS; `--inspect`, `--list-values`, `--raw`,
and the interface documentation in the README that the rest of this is built on.
