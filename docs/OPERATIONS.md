# Running this in production

The thing being operated is an **archive**. Its value is that it is unbroken:
a night missed is a day of licence history that cannot be recovered, because
every register here overwrites its own file and none of them publishes a
changelog. Everything below follows from that.

---

## 1. Install

```bash
uv tool install "eufinreg[at] @ git+https://github.com/7feilee/eufinreg"
eufinreg --version
```

The `at` extra is 7-Zip support, needed only for the Austrian trade-licence
register. Without it every other source works and `gisa` fails with a message
naming the extra.

## 2. Configure

Copy [`examples/eufinreg.config.json`](../examples/eufinreg.config.json) and
change two things before anything else:

```json
{
  "store": "/var/lib/eufinreg",
  "user_agent": "acme-compliance/1.0 (+https://acme.example/bot; ops@acme.example)",
  "preset": "dach",
  "retention_days": 1100,
  "watchlists": ["/etc/eufinreg/counterparties.json"]
}
```

**The User-Agent is not decoration.** These are small public-sector deployments
with no rate limiting to protect themselves. An operator who sees unusual
traffic and can find out who to contact will email you; one who cannot will
block a netblock. Put a real contact address in it.

`preset: "dach"` is Germany, Austria and Switzerland answered properly — FINMA
and GISA directly, the German and Austrian federal registers via the EU-level
ones that actually carry them. `"all"` archives every source that can be
archived. Relative paths resolve against the config file, not the working
directory, because cron runs from somewhere unpredictable.

## 3. Schedule

```bash
eufinreg ingest --config /etc/eufinreg/eufinreg.config.json --json
```

Crontab and systemd unit files are in [`examples/`](../examples). Both schedule
04:12 UTC rather than 04:00, because every cron job in the world runs on the
hour, and the systemd timer sets `Persistent=true` so a host that was down
catches up instead of leaving a hole.

Exit codes, which is what a scheduler actually reads:

| Code | Meaning | What to do |
|---|---|---|
| `0` | everything succeeded | nothing |
| `1` | at least one register failed | read the report; one bad register does not stop the others |
| `2` | configuration or usage error | nothing was attempted; fix the config |
| `3` | the store is locked | a previous run is still going, or died — see below |

`--json` writes a machine-readable run report to stdout; append it to a JSONL
file and you have a operational history alongside the data history.

## 4. What a run does

```
for each configured source:
    skip?  ── if the archive entry is newer than half the register's own
              publication cadence, nothing can have changed. --force overrides
    fetch  ── one request at a time, delay between, retries with backoff
    write  ── snapshot: canonical sorted JSONL, gzipped, + .sha256, then renamed
    write  ── raw: the untouched response bytes + manifest.jsonl
    diff   ── against the previous snapshot, column by column
    write  ── changes: one file per run, even when nothing changed
    prune  ── snapshots past retention, never below keep_last
```

Sources are isolated: ESMA being down at 04:00 is not a reason to skip FINMA.
The run continues, the exit code is `1`, and the report names what failed.

**Run it nightly regardless of what it contains.** GISA is a monthly file and
the skip rule means 29 of 30 nights cost nothing — no request to the ministry,
no 60 MB duplicate in the archive. FINMA is daily and is fetched daily. ESMA
declares no cadence, so it is always fetched; guessing one would mean missing
changes. The half-cadence threshold leaves room for a scheduler's jitter, so a
nightly timer that fires 23.9 hours later still refreshes a daily file.

**Snapshots are gzipped** (`"compress": false` to turn it off). It is a tenfold
saving on the register that needs it most: Austria's 1.03 M trade licences are
607 MB of JSONL and 58 MB compressed. The digest of the logical rows is
unaffected, and the `.sha256` sidecar still checks with `sha256sum -c`.

## 5. Watch — the daily output

```bash
eufinreg watch --config … --since $(date -u -d yesterday +%Y%m%d) -o changes.csv
eufinreg watch --config … --coverage        # who can actually be seen
```

Every event row carries `_entity_id`, `_match` (`identifier` or `name`) and
`_match_column`, so a reader can tell a certain match from a plausible one
without asking. **Run `--coverage` when a watchlist changes.** The failure mode
it exists to prevent is a list that quietly monitors 31 of the 40 entities
somebody believes it monitors; an entity nobody can find is not "no news", it is
no coverage.

Watchlists are JSON or CSV — compliance teams' lists arrive as spreadsheets:

```csv
id,label,identifiers,names,sources
ubs,UBS AG,CHE-101.329.561,UBS AG|UBS Switzerland AG,finma|ch-uid
```

## 6. Evidence

```bash
eufinreg receipt --config … --source finma --match CHE-101.329.561 -o receipt.zip
eufinreg receipt --verify receipt.zip
```

A bundle carries the row, the raw response bytes it came from, the snapshot's
checksum, the register's own disclaimer, and a manifest with a SHA-256 per
member. It is **reproducible** — building it twice from the same snapshot gives
identical bytes — and it says plainly that it is neither signed nor
timestamped by a third party. Do not let anyone describe it as more than that.

Receipts come from the archive, never from a live fetch. That is the point: an
evidence artefact whose subject could have changed between the check and the
file is not evidence of anything.

## 7. Observability

Three things are recorded on every run, and they answer different questions.

**Logs.** Human lines by default; `--log-format json` emits one object per line
with the same messages plus structured fields and a `run_id`:

```bash
eufinreg ingest --config … --log-format json | tee -a /var/log/eufinreg/ingest.jsonl
{"level":"info","message":"finma: 2827 rows, no change (0.3s, 1 request(s))","run_id":"20260816T122849Z-a8cdb9",…}
{"by_host":{"www.finma.ch":1},"bytes_in":464960,"message":"http: 1 request(s), 0 retried, …","requests":1,…}
{"fetch":0.249,"flatten":0.01,"write":0.024,"diff":0.039,"message":"phases: …",…}
```

The `run_id` is stamped into the logs, the run record **and the snapshot
metadata**, so "why did Tuesday's numbers move" is one grep rather than an
archaeology project.

**Run history.** Every ingest writes a record next to the data it produced:

```bash
eufinreg runs                 # the last 20 runs, one line each
eufinreg runs --last          # the newest in full, as JSON
eufinreg runs 20260816T122849Z-a8cdb9
```

```
run                      finished              ok   failed  changes   reqs  retries  took
20260816T124809Z-403095  2026-08-16T12:50:44Z  6/6  0       +0 ~0 -0  115   0        154.8s
```

The per-source breakdown is where it earns its keep. From that run:

| Source | Total | fetch | write | Requests |
|---|---|---|---|---|
| `upreg` | 118.4 s | 117.9 s | 0.3 s | 108 |
| `gisa` | 29.6 s | 8.3 s | **19.1 s** | 1 |
| `eba-psd` | 5.9 s | 5.8 s | 0.1 s | 3 |

`upreg` is not slow — it is 108 requests × the one-second delay, and the whole
run used 16.4 s of actual wire time. `gisa` spends most of its time in `write`,
which is gzipping 607 MB. Neither of those is visible from a wall-clock total,
and they call for opposite responses.

Each record carries per-source phase timings (`fetch` / `flatten` / `write` /
`diff`), HTTP counters by host and status, the five slowest requests, the
warnings each source raised, and the tail of a traceback when something failed
unexpectedly. That last one matters: "it failed last night" is not a bug report.

**Metrics.** `GET /api/metrics` is Prometheus text:

```
eufinreg_http_requests_total 115
eufinreg_http_retries_total 0
eufinreg_http_bytes_in_total 66714831
eufinreg_http_seconds_total 16.354
eufinreg_wait_seconds_total 107.703
eufinreg_http_responses_total{status="200"} 115
eufinreg_http_requests_by_host_total{host="registers.esma.europa.eu"} 108
eufinreg_archive_sources 6
eufinreg_archive_stale 0
eufinreg_archive_rows 1054511
eufinreg_api_requests_total{route="/api/rows",outcome="ok"} 42
```

Two of those deserve a note. **`wait_seconds` is separate from `http_seconds`**
because they mean opposite things: a lot of waiting with little wire time is
politeness working as designed; the reverse is a register in trouble. And
**`archive_stale` is the number to alert on** — it compares each source's newest
snapshot against that register's own publication cadence, not against your cron
schedule.

## 8. Monitoring

| Check | Command | Alert when |
|---|---|---|
| Archive freshness | `eufinreg store status` | exit code `1`, i.e. a source is past twice its register's cadence |
| Interface drift | `eufinreg doctor` | exit code `1` — a register changed shape and output cannot be trusted |
| Archive integrity | `eufinreg store verify` | exit code `1` — a snapshot no longer matches its own checksum |
| Service health | `GET /api/health` | `status != "ok"`, `stale` non-empty, or `last_run.failed > 0` |
| Retry pressure | `eufinreg_http_retries_total` | it climbs — a register is rate-limiting or wobbling before it fails outright |
| A run that stopped happening | `eufinreg runs` / `eufinreg_archive_stale` | no new run id, or a source past twice its cadence |

`doctor` is the one command that must reach the live registers; it is what the
offline test suite cannot do. Run it weekly. It checks each source still returns
rows and still has the fields this client depends on, and names the missing ones
when it does not.

Staleness is measured against the *register's* cadence, not your schedule: GISA
is a monthly file, so a 30-hour-old snapshot is fine; the EBA regenerates
nightly, so a 30-hour-old one is late.

## 9. Serving

```bash
eufinreg serve --config … --port 8000
```

With a store configured, the API answers from the archive and **contacts no
register**; `?live=1` opts back in per request. Binding to anything other than
loopback without `--token` is refused rather than warned about — an open proxy
in front of a regulator's server is how everyone's access gets withdrawn.

`http.server` is a development server. In front of a team, put a real one
(gunicorn/uvicorn behind nginx) or serve pre-rendered snapshots as static files,
which the store's layout makes entirely viable.

## 10. Personal data

Two sources are flagged, and the flag is enforced in code rather than in a
README warning:

* **`eba-psd --select ALL`** — 322,314 agent records that are largely named
  individuals with an address, processed under Regulation (EU) 2018/1725. The
  default selection is institutions and is not flagged, because institutions are
  companies.
* **`eudamed-eo`** — an email address and a phone number for nearly every one of
  48,893 organisations, published so patients and regulators can identify who is
  responsible for a device. That is not the same purpose as a marketing list,
  and in most member states using it as one is unlawful.

The HTTP API refuses these with `403` unless started with
`--allow-personal-data`. The CLI does not gate them — it is operated by a person
who typed the command — but the store keeps whatever you ingest, so decide
before scheduling, not after.

GDPR aside: **"it was published" is not a lawful basis under Article 6.** Decide
yours before you fetch.

## 11. Data licences

| Source | Licence |
|---|---|
| `gisa`, `gisa-codes` | **CC BY 4.0** — attribute BMWET / data.gv.at if you republish |
| everything else | not stated by the publisher; their own terms apply |

"Unstated" is not "permissive". Reading a register for your own compliance
purposes is one thing; redistributing it commercially is another, and needs a
per-source answer before you do it.

## 12. Failure playbook

**`eufinreg: … is held by pid N since …` (exit 3).**
A previous run is still going or died. Locks older than six hours are reclaimed
automatically. If the process is gone and you cannot wait, delete
`<store>/.lock`.

**One source failed, the rest are fine.**
Normal. Re-run just that source: `eufinreg ingest --config … --source finma`.
A missed *day* matters; a missed *hour* does not.

**A source failed and the message is not obvious.**
Read the run record: `eufinreg runs --last` carries the per-source error, the
tail of the traceback, and the phase timings. A failure in `fetch` is the
register; a failure in `flatten` is this project's parser meeting a shape it did
not expect — which is the same thing `doctor` reports, arriving the hard way.

**`doctor` reports missing fields.**
The register changed shape. Do not paper over it: the flattened output is now
missing something downstream code may depend on. Re-capture a fixture with
`--raw`, update the source, update the test.

**`store verify` fails.**
A snapshot no longer matches its recorded digest — filesystem corruption, or
someone edited it. Restore from backup. The archive is the asset; back it up
like one.

**A register starts returning 403 to this client.**
Stop. Check `robots.txt` and the operator's terms, raise the delay, and put a
contact address in the User-Agent if there is not one there already. This
project reads BaFin's data from the EBA rather than from BaFin for exactly this
reason.

## 13. Sizing

Measured on the live registers with the `dach` preset, 2026-08-16 — one
complete run, wall clock 4 min 1 s:

| Source | Wire | Rows | Snapshot on disk | Raw kept | Fetched |
|---|---|---|---|---|---|
| `finma` | 465 KB CSV | 2,827 | 97 KB | 465 KB | daily |
| `gisa` | 9.2 MB `.7z` → 607 MB JSONL | 1,030,111 | **58 MB** | 9.2 MB | monthly |
| `gisa-codes` | 107 KB CSV | 907 | 29 KB | 107 KB | monthly |
| `upreg` | ~100 requests | 13,930 | 2.6 MB | 30 MB | every run (no declared cadence) |
| `eba-psd` | 19 MB ZIP → 217 MB JSON | 6,407 | 1.1 MB | 19 MB | daily |
| `mica-casp` | 161 KB CSV | 329 | 42 KB | 161 KB | weekly |

Two costs to budget, and they behave differently:

* **Snapshots** grow with retention, but the skip rule means each source is only
  archived as often as it actually changes. A year of DACH at these sizes is
  roughly 2 GB — dominated by GISA's twelve monthly snapshots.
* **Raw bodies** are the same size every time and are what an evidence bundle
  carries. If disk is tight, `"capture_raw": false` halves the archive and costs
  you the ability to prove what the register actually sent.

`retention_days` plus `keep_last` bounds both, and `keep_last` is absolute — the
age rule can never delete the last snapshots standing.

Peak memory is the number to watch, not disk: `eba-psd` parses a 217 MB JSON
document and peaks around **1.4 GB RSS**, and `gisa` holds a 607 MB string while
parsing. Give the host 4 GB.
