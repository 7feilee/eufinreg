# eufinreg

**Keep, watch and evidence the European public licence registers** — with the
German, Austrian and Swiss ones treated as first-class, because a DACH question
cannot be answered from EU-level data alone.

```bash
eufinreg ingest  --config eufinreg.config.json     # nightly: archive every register
eufinreg watch   --watchlist counterparties.json   # what changed for the entities I care about
eufinreg receipt --source finma --match CHE-101.329.561 -o receipt.zip   # prove what it said
```

Every register in here publishes what is true **now** and overwrites what was
true yesterday. None of them publishes a changelog, a `?since=` or a feed. So
"was this firm licensed on 4 March, and for what?" is a question no register can
answer — only an archive can, and only if somebody started it. That is what this
is for.

**The premise underneath**, stated as a test you can apply to any source:

> **Was this data produced because the publisher *wanted* to, or because they
> were *obliged* to?**

LinkedIn, a company website and a job board are all voluntary, motivated and
edited: you see what somebody chose to show, with unknown coverage and
unmeasurable bias. A register is the opposite — filing is a legal condition of
trading, in a prescribed format, with a penalty for not filing. Three properties
follow, and they are the whole reason this project exists:

* **coverage approaches the full population** — you cannot opt out and stay legal;
* **it is early** — the approval happens *before* the business does, so it leads
  every voluntary signal;
* **it is comparable** — the law prescribes the format, which is why it can be
  parsed at all.

So BaFin's database beats LinkedIn not because it is more authoritative, but
because **no licensed institution can choose not to appear in it.**

| | |
|---|---|
| **Registers** | 21 sources — licences: ESMA, EBA PSD2, MiCA, EUDAMED, CTIS · **FINMA (CH)**, **Swiss UID register**, **GISA (AT)**; upstream: **GLEIF**, **VIES**, **TED** |
| **Runtime dependencies** | one (`requests`); `py7zr` only for the Austrian archive, `openpyxl` only for `--format xlsx` |
| **Tests** | 633, fully offline; plus a weekly live [drift check](.github/workflows/drift.yml) against every register |
| **Docs** | [Operations runbook](docs/OPERATIONS.md) · [Architecture](docs/ARCHITECTURE.md) · [The niche](docs/BUSINESS.md) · [Changelog](CHANGELOG.md) |

> **Name note:** the package started as a financial-registers client and still
> carries the name. It now covers medical devices and pharma too, and reaches
> outside the EU. A rename is pending; the CLI is stable in the meantime.

Four things this does that a plain register client does not:

* **It documents the interfaces**, including the ones that silently lie to you —
  a `/csv` path that returns a 7-Zip archive, a search that stops at 30 results
  and says nothing, a language parameter that is really a filter.
* **It keeps time.** `ingest` writes canonical, checksummed snapshots plus the
  untouched response bytes; `watch` and `--diff` turn two of them into
  column-level change events.
* **It knows who you are watching.** A watchlist is matched on identifiers where
  the registers share one and on normalised names where they do not — and every
  row says which, because a confident wrong match is worse than a gap.
* **It refuses things.** Personal-data selections are gated in code, not in a
  warning; the server will not bind to a public interface without a token; and
  three perfectly usable endpoints are documented here and deliberately not
  wrapped.
* **It says what it did.** Structured logs with a run id, per-phase timings,
  HTTP counters by host and status, a persisted record of every run, and
  Prometheus metrics including how stale each part of the archive is. One
  register failing — for any reason, including one nobody anticipated — never
  stops the others.

The point of this repository is only half the code. The other half is the
**interface documentation below**, which is the result of reading the regulators'
help pages and then probing the live endpoints to check what they actually do.
Public registers tend to have interfaces that are undocumented, half-documented,
or documented somewhere nobody links to.

Everything in the "How the interfaces work" section was verified against the
live services: the ESMA sections on **2026-08-15**, everything else — including
every DACH section — on **2026-08-16**. Counts will drift; the shapes should not.

---

## TL;DR — what has an interface and what does not

| Register | Sector | Official machine interface? | What this tool does |
|---|---|---|---|
| **ESMA Registers** (MiFID investment firms, AIFMs, UCITS management companies, crowdfunding providers, trading venues, benchmark administrators, funds, MMFs…) | Finance | **Yes** — a read-only Apache Solr A2A endpoint, no authentication, [documented by ESMA](https://registers.esma.europa.eu/publication/helpApp) | Reads it directly, pages with `cursorMark`, flattens parent/child blocks into one row per entity |
| **ESMA interim MiCA register** (CASPs, ART issuers, EMT issuers, white papers, non-compliant entities) | Finance | **Yes, sort of** — five stable CSV URLs regenerated weekly. No query API, but a documented, machine-readable bulk download | Downloads and parses them, normalises the messiest field |
| **EBA PSD2 register** (payment institutions, e-money institutions, AISPs, their agents and branches) | Finance | **Yes, and it is required to be** — [Commission Implementing Regulation (EU) 2019/410](https://eur-lex.europa.eu/eli/reg_impl/2019/410/oj) obliges the EBA to publish it electronically. A nightly JSON "golden copy" with a published SHA-256 | Reads the file-metadata endpoint, downloads the archive, **verifies both checksums**, flattens it, labels the codes from the register's own metadata |
| **EUDAMED** (medical device manufacturers, importers, authorised representatives, procedure-pack producers, notified bodies) | Medical devices | **Yes** — the JSON API the public site runs on, no authentication, not excluded by `robots.txt`. 48,893 organisations **with email and phone** | Pages it with a unique sort key, always sends the mandatory language parameter, flattens the nested reference-data blocks |
| **CTIS** (authorised clinical trials and their sponsors) | Pharma / CRO | **Yes** — `POST /ctis-public-api/search`, no authentication. 12,229 trials, sponsor and sponsor type per trial | Pages it, and **warns when the register's 10,000-record window silently truncates the answer** |
| **FINMA authorisation holders** (Switzerland) | Finance | **Yes** — a CSV at a stable URL, regenerated daily, [robots-allowed](#finma-switzerland--the-authorisation-list-as-a-daily-csv). Switzerland is outside the EEA, so none of the EU registers above contain a Swiss licence | Reads it, collapses 2,938 authorisations into 2,744 institutions, and emits the UID so the rows join to the register below |
| **Swiss UID register** (BFS, Switzerland) | All sectors | **Yes** — a public SOAP web service, no authentication, [published WSDL](#the-swiss-uid-register--a-public-soap-service-with-a-silent-ceiling). Legal form, seat, commercial-register status, VAT status | Wraps `Search` and `GetByUID`, and **warns when the undocumented 30-record ceiling truncates a search** |
| **GISA** (Gewerbeinformationssystem Austria) | All sectors | **Yes, as open data** — the search UI is an unautomatable ASP.NET form, but the ministry publishes the whole register under CC BY 4.0. [1,030,111 active trade licences](#gisa-austria--a-million-trade-licences-in-a-7-zip-archive) | Downloads it, decompresses the 7-Zip archive the `/csv` path actually returns, and filters **while parsing** so a million rows never land in memory |
| **GLEIF LEI register** | All sectors | **Yes** — a documented JSON:API, no key, plus daily bulk *and delta* files. **CC0** | Reads it. The `lei` joins ESMA and MiCA rows to a real entity, and `entity.registeredAs` carries the firm's own **Handelsregister number** |
| **VIES** (EU VAT validation) | All sectors | **Yes** — a REST endpoint per member state, no key. A validator, not a directory | Checks one number at a time, and **distinguishes "invalid" from "that member state's system is down"** |
| **TED** (EU public procurement) | All sectors | **Yes** — `POST /v3/notices/search`, no key, TED's own query language | Reads award notices with the winning company named. EU/EEA only, so **no Switzerland** |
| **BaFin Unternehmensdatenbank / ZAG register / VGV register** (Germany) | Finance | **A human-facing one, yes; an automatable one, no** — the result pages do offer CSV/XML/Excel export links, but the whole portal host is `robots.txt: Disallow: /`. See [the evidence](#bafin-germany--an-export-button-behind-a-blanket-robots-ban) | Does **not** touch the portal. Shows you the exact export URLs to click yourself, and routes automation to the ESMA and EBA data that covers BaFin-supervised entities |
| **FMA Unternehmensdatenbank** (Austria) | Finance | **No** — see [the evidence](#fma-austria--no-interface-at-all) | Same routing, no export to click |
| **Zefix** (Swiss commercial register) | All sectors | **Documented REST API, key required** — and the host's `robots.txt` is `Disallow: /`. See [the evidence](#zefix-and-basg--found-documented-deliberately-not-wrapped) | Not touched. The UID register above answers most of the same questions without a key |

Eleven further registers across chemicals, aviation, energy, automotive,
pharma and non-EU finance were probed on 2026-08-16 and are documented — with
what was actually found — in
[The same trick in other industries](#the-same-trick-in-other-industries).

**On DACH specifically.** The three countries fail in different directions, and
the pattern is the opposite of what most people expect: **Austria and
Switzerland publish files; Germany publishes a portal.** Austria's entire
trade-licence register is open data under CC BY 4.0, and Switzerland exposes the
federal business register over an unauthenticated web service — while the German
federal registers that matter sit behind `Disallow: /` or a 2.9 GB bulk export.
The German answer therefore has to be routed through the EU-level registers,
which is why the [BaFin section](#bafin-germany--an-export-button-behind-a-blanket-robots-ban)
ends in a routing table rather than a client.

No HTML scraping happens anywhere in this project.

---

## Install / run

Nothing to clone, no virtualenv to manage — the single-file script carries its
own dependency metadata ([PEP 723](https://peps.python.org/pep-0723/)):

```bash
uv run https://raw.githubusercontent.com/7feilee/eufinreg/main/scripts/eufinreg_solo.py --help
```

As a project:

```bash
git clone https://github.com/7feilee/eufinreg && cd eufinreg
uv sync                     # creates .venv from the committed uv.lock
uv run eufinreg --list-sources
```

As a tool, without cloning:

```bash
uv tool install "eufinreg[at] @ git+https://github.com/7feilee/eufinreg"
eufinreg --list-sources
```

---

## The two interfaces

`eufinreg` is one binary with two personalities, and they do not interfere.

**A reading tool** — flags only, unchanged since 0.4:

```bash
eufinreg --source finma --select bank -o swiss-banks.csv
```

**An archive you run** — verbs, added in 0.5:

| Command | What it does | Exit codes |
|---|---|---|
| `eufinreg ingest` | Fetch every configured register into the snapshot store: rows, raw bytes, change events, retention. One failing register does not stop the run. | `0` ok · `1` a source failed · `2` config · `3` locked |
| `eufinreg watch` | Change events scoped to a watchlist, with match confidence per row and `--coverage` for the entities nobody can see. | `0` · `2` no watchlist / empty store |
| `eufinreg receipt` | A reproducible evidence bundle for one entity: the row, the raw response bytes, the checksums, the publisher's disclaimer. `--verify` checks one. | `0` · `1` no match / bundle failed |
| `eufinreg store` | `status` (freshness vs each register's cadence), `list`, `verify`, `prune`. | `0` · `1` stale or corrupt |
| `eufinreg runs` | What previous ingests did: phase timings, requests, retries, per-source errors and the tail of any traceback. | `0` · `1` a past run failed |
| `eufinreg doctor` | Ask every register whether it is still the shape this client expects. The one command that must touch the live services. | `0` · `1` drift |
| `eufinreg serve` | Read-only JSON API + web UI, answering from the archive. | |

### Five minutes to a running archive

```bash
mkdir -p /etc/eufinreg && cp examples/eufinreg.config.json /etc/eufinreg/
# edit two lines: "store", and a "user_agent" with a real contact address

eufinreg ingest --config /etc/eufinreg/eufinreg.config.json
# eufinreg: finma: 2827 rows, +0 ~0 -0 (3.1s)
# eufinreg: gisa: 1030111 rows, +0 ~0 -0 (7.4s)
# eufinreg: done: 6/6 ok, 0 failed, 0 skipped in 41.2s

eufinreg store status --config /etc/eufinreg/eufinreg.config.json
# source      snapshots  latest            rows     age_h  cadence_h  state
# finma       1          20260816T041200Z  2827     0.1    24         ok
# gisa        1          20260816T041200Z  1030111  0.1    720        ok
```

**Every run is recorded**, so a question about last Tuesday has an answer:

```bash
eufinreg runs                          # one line per run: ok/failed, changes, requests, time
eufinreg runs --last                   # the newest in full: phases, HTTP counters, tracebacks
eufinreg ingest --config … --log-format json   # same events, one JSON object per line
curl localhost:8000/api/metrics        # Prometheus text, including archive staleness
```

```
eufinreg: upreg: 13930 rows, +0 ~0 -0 (118.4s, 108 request(s))
eufinreg: http: 115 request(s), 0 retried, 0 failed, 66.7 MB in, 16.4s on the wire, 107.7s waiting
eufinreg: phases: fetch=132.9s write=19.5s flatten=2.4s diff=0.0s
```

A `run_id` ties the logs, the run record and the snapshot metadata together. And
those two numbers are separate on purpose: a full DACH run spends **107.7 s
waiting and 16.4 s on the wire** — it is 87% politeness, which is the design
working rather than a register being slow.

Then schedule it — [`examples/eufinreg.crontab`](examples/eufinreg.crontab) and
[`examples/eufinreg-ingest.timer`](examples/eufinreg-ingest.timer) — and read
[docs/OPERATIONS.md](docs/OPERATIONS.md), which covers monitoring, retention,
personal-data policy, data licences and the failure playbook.

**A day later, the output somebody actually reads:**

```bash
eufinreg watch --config /etc/eufinreg/eufinreg.config.json --coverage -o changes.csv
# eufinreg: finma: matched 3/4 watched entit(ies) (1 by name only — weaker than
#           an identifier match); not found: ghost-ag
# eufinreg: 2 event(s) affecting watched entities → changes.csv
```

```
_change  _entity_id  _match      _changed_columns                        Name
changed  ubs         identifier  authorisation_AuthorisationTypeEN|count UBS AG
changed  six         name        City                                    SIX Swiss Exchange AG
```

---

## The three things to run before you trust any output

Public register schemas change without notice, and a wrong field name or a
wrong enum value returns **zero rows with no error** — which reads exactly like
a broken endpoint. So:

```bash
# 1. Which fields really exist right now, how often are they populated,
#    and what do the values look like?
uv run eufinreg --source upreg --inspect 200

# 2. Which values may I actually filter on?
uv run eufinreg --source upreg --list-values ae_entityTypeCode
uv run eufinreg --source upreg --list-enums

# 3. Keep the untouched responses, so you can tell your bug from their bug.
uv run eufinreg --source upreg --select CSP --raw ./raw -o crowdfunding.csv
```

`--raw DIR` writes every response body byte-for-byte plus a `manifest.jsonl`
recording URL, status, `Content-Type`, `Last-Modified` and fetch time.

---

## Usage

```bash
uv run eufinreg [--source KEY] [mode] [selection] [filters] [output] [etiquette]
```

### Common recipes

```bash
# Every EU crowdfunding service provider (ECSPR), one row each,
# authorised services collapsed into pipe-joined columns
uv run eufinreg --source upreg --select CSP -o ecsp.csv

# All MiCA-authorised crypto-asset service providers
uv run eufinreg --source mica-casp -o casps.csv

# Austrian-supervised MiCA CASPs, as JSON
uv run eufinreg --source mica-casp --field ae_homeMemberState=AT --format json

# Entities supervised by BaFin (exact match on the full authority name)
uv run eufinreg --source upreg \
  --field 'ae_competentAuthority=Federal Financial Supervisory Authority (BaFin)' \
  -o bafin-supervised.csv

# Every EU payment / e-money institution, with its passporting footprint
uv run eufinreg --source eba-psd -o psd-institutions.csv

# BaFin-supervised payment and e-money institutions — the ZAG register,
# which upreg does not contain
uv run eufinreg --source eba-psd --field CA_OwnerID=DE_BAFIN -o bafin-zag.csv

# Only e-money institutions, and only those passported into Germany
uv run eufinreg --source eba-psd --select EMI --contains DE -o emi.csv

# The agents too — 322k rows, mostly natural persons; read the disclaimers first
uv run eufinreg --source eba-psd --select ALL -o psd-everything.csv

# Every EU medical device manufacturer (31,931), with address, email and phone
uv run eufinreg --source eudamed-eo --select manufacturer -o device-makers.csv

# German importers of medical devices — both filters pushed server-side
uv run eufinreg --source eudamed-eo --select importer \
  --query 'countryIso2Code=DE' -o de-importers.csv

# The 70 notified bodies that certify those devices, with their NANDO links
uv run eufinreg --source eudamed-nb -o notified-bodies.csv

# Who sponsors CAR-T trials in the EU, and in which countries
uv run eufinreg --source ctis --query 'CAR-T' -o car-t-trials.csv

# Which sponsor types run trials at all — counted over the whole result set
uv run eufinreg --source ctis --query 'oncology' --list-values sponsorType

# Schema-drift-proof: match a string anywhere in the record
uv run eufinreg --source mica-casp --contains bybit --format json

# Investment firms licensed for portfolio management, including withdrawn history
uv run eufinreg --source upreg --select MIF --include-history \
  --contains "portfolio management" -o mif-pm.csv

# Raw Solr query, for anything the flags do not cover
uv run eufinreg --source upreg --query 'ae_homeMemberState:austria' -o at.csv

# A core this package has never heard of
uv run eufinreg --source solr:esma_registers_priii_documents --inspect 20

# Excel, via the one optional dependency
uv sync --extra xlsx
uv run eufinreg --source mica-casp --format xlsx -o casps.xlsx
```

### DACH recipes

```bash
# Every FINMA-authorised institution in Switzerland, one row each,
# authorisations collapsed — 2,938 authorisations, 2,744 institutions
uv run eufinreg --source finma -o swiss-licences.csv

# Just the banks; --select takes an alias or any substring of the type
uv run eufinreg --source finma --select bank -o swiss-banks.csv
uv run eufinreg --source finma --select portfolio-manager -o swiss-pm.csv

# What licence categories exist at all, counted over the whole file
uv run eufinreg --source finma --list-values authorisation_AuthorisationTypeEN

# Take one of those UIDs to the federal business register: legal form,
# seat, commercial-register status, VAT status, VAT-group membership
uv run eufinreg --source ch-uid --query 'uid=CHE-101.329.561' --format json

# Search it instead — but read the warning it prints
uv run eufinreg --source ch-uid --select ZG --query 'town=Zug'

# Every active Austrian trade licence — 1,030,111 rows, so filter at parse time
uv sync --extra at        # Austria ships it as a 7-Zip archive
uv run eufinreg --source gisa --select konzessioniert -o at-konzessionen.csv

# Licensed trades in Vienna only, both filters applied during the parse
uv run eufinreg --source gisa --query 'nuts2=AT13&gewerbeart=5' -o wien.csv

# Which trades are most common, counted over the whole register
uv run eufinreg --source gisa --list-values gewerbewortlaut | head -20

# The code list that labels them (907 codes, a plain CSV, no extras needed)
uv run eufinreg --source gisa-codes -o gewerbeschluessel.csv

# German entities: BaFin's portal is robots-banned, so go via the EU registers
uv run eufinreg --source eba-psd --field CA_OwnerID=DE_BAFIN -o de-payments.csv
uv run eufinreg --source upreg \
  --field 'ae_competentAuthority=Federal Financial Supervisory Authority (BaFin)' \
  -o de-investment-firms.csv
```

### Upstream of the licence: identity, verification, public money

The three sources above are not licence lists. They sit at different points on
the chain that *ends* in a job advert — and each is an obligation somebody
cannot skip.

```bash
# Identity. The LEI is the key that turns an ESMA or MiCA row into an entity —
# and on German records it carries the company's own Handelsregister number.
uv run eufinreg --source gleif --select LI -o liechtenstein-entities.csv
uv run eufinreg --source gleif --query 'entity.legalAddress.city=Zug&entity.status=ACTIVE' \
  -o zug-active.csv

# Verification. Tax authorities are the party nobody lies to twice.
uv run eufinreg --source vies --query 'vat=DE123456789,ATU12345678'
uv run eufinreg --source vies --select STATUS      # which member states are answering

# Public money — the earliest signal in this repository. An award names the
# winner months before the hiring that follows from it.
uv run eufinreg --source ted --select dach \
  --query 'notice-type IN (can-standard) AND publication-date>=today(-7)' \
  -o dach-awards.csv

# Who keeps winning: count the winners over the whole result set
uv run eufinreg --source ted --select dach \
  --query 'notice-type IN (can-standard) AND publication-date>=today(-30)' \
  --list-values winner-name | head -20
```

Note what each one is *not*. `vies` refuses to be archived on a schedule — it
validates one number and a scheduled crawl of it would be abuse. `gleif` stops
at 10,000 results and says so, so a whole-country pull needs its bulk file. And
`ted` is a rolling window: diffing two snapshots of "the last 7 days" reports
notices ageing out as removals, which is noise rather than news.

### Keeping time by hand

`ingest` is the managed version of this, and what you should schedule. The
underlying primitives are on the reading tool too, for one-off comparisons that
do not deserve a store:

```bash
# Take a dated, checksummed snapshot (sorted JSONL + a .sha256 sidecar)
uv run eufinreg --source finma --snapshot snapshots/finma-2026-08-16.jsonl

# …a week later, take another, then ask what moved
uv run eufinreg --source finma --snapshot snapshots/finma-2026-08-23.jsonl
uv run eufinreg --diff snapshots/finma-2026-08-16.jsonl \
                       snapshots/finma-2026-08-23.jsonl -o changes.csv
# → eufinreg: finma: 4 added, 2 changed, 1 removed (2827 → 2830 rows, …)

# Browse every register in a browser. Read-only, loopback-only, one outbound
# request at a time, answers from the archive when a store is configured.
uv run eufinreg serve --config eufinreg.config.json --port 8000
# → http://127.0.0.1:8000/  and  /api/sources
```

`--diff` never touches the network. That is the point of it: the comparison is
between two things you already have, and it stays reproducible for as long as
you keep the files. Snapshots taken this way are byte-identical for unchanged
data, so "nothing moved" is a checkable claim rather than an impression.

### Flags that matter

| Flag | Effect |
|---|---|
| `--inspect [N]` | Sample N records (default 50), print real field names, fill rate, distinct counts, sample values. For block-structured sources it prints the profile **twice**: as received, and after flattening. |
| `--list-values FIELD` | Distinct values of `FIELD` with counts. On Solr this is a **facet query** — exact over the whole core, one request, not a sample. |
| `--list-enums` | `--list-values` for every field the source treats as an enumeration. |
| `--raw DIR` | Save untouched response bodies + `manifest.jsonl`. |
| `--contains TEXT` | Case-insensitive substring across **all** fields. Repeatable, ANDed. Survives field renames. |
| `--field NAME=VALUE` | Case-insensitive **exact** match on one field. Repeatable, ANDed. Warns loudly if `NAME` does not exist in any fetched record. |
| `--select VALUE` | Source-specific selector. On Solr it is pushed down into `q` and is genuinely cheap; on a bulk-file source such as `eba-psd` it filters after the download. Run `--list-sources` for each source's accepted values. |
| `--query Q` | Raw source-native query, passed through untouched: a Solr `q` on the ESMA cores, `key=value&key=value` URL parameters on EUDAMED. |
| `--snapshot PATH` | Write the result as a canonical, sorted JSONL snapshot with a `body_sha256` and a `.sha256` sidecar. Byte-stable for unchanged data, so "nothing changed" is checkable. |
| `--diff OLD NEW` | Compare two snapshots and report added / removed / changed rows, **column by column**. Offline: no register is contacted. |
| `--serve [PORT]` | Run the read-only JSON API and web UI (default 8000, loopback only). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). |
| `--no-flatten` | Emit records exactly as received. |
| `--no-derived` | Drop the added helper columns (`ac_serviceCode_normalised`, the `eba-psd` `*_label` columns). Also skips the extra request that fetches the labels. |
| `--delay SEC` | Pause between requests. Default **1.0 s**. |
| `--user-agent UA` | **Please set this.** See [Polite use](#polite-use). |
| `-4`, `--ipv4` | Resolve IPv4 only, like `curl -4`. See [Troubleshooting](#troubleshooting). |

Filtering happens **after** flattening, so `--contains "portfolio management"`
matches an entity because of one of its *child* activity records, and you still
get the whole entity row back.

---

## How the interfaces work

### ESMA Registers — application-to-application (A2A) Solr endpoint

ESMA publishes its registers as read-only Apache Solr cores.

* **Endpoint:** `https://registers.esma.europa.eu/solr/<core>/select`
* **Authentication:** none. No API key, no registration.
* **Rate limits:** none documented. Which is not permission to hammer it.
* **ESMA's own documentation:** <https://registers.esma.europa.eu/publication/helpApp>
* ESMA also publishes a Python package, `esma_data_py`, at
  <https://github.com/European-Securities-Markets-Authority> (announced 2025-04-01).

#### Query parameters

Standard Solr `/select` parameters; the useful subset:

| Parameter | Meaning |
|---|---|
| `q` | Query. **Required.** `*:*` for everything. |
| `fq` | Filter query. Does not affect scoring, cached separately. Repeatable. |
| `wt` | `json` or `xml`. |
| `rows` | Page size. ESMA's own examples use `1000`. |
| `start` | Offset paging. Works, but see `cursorMark`. |
| `cursorMark` | Cursor paging. **Verified working.** Start with `*`, then send back the `nextCursorMark` from the previous response. Requires a `sort` whose last key is the unique `id` field. Stable while the index is being updated; deep `start` offsets are not. |
| `sort` | e.g. `id asc`. |
| `fl` | Comma-separated field list to return. |
| `facet=true` + `facet.field` | Value enumeration with counts. Repeatable. `facet.limit=-1` for all, `facet.mincount=1` to hide empties. This is what `--list-values` uses. |
| `indent=true` | Pretty-print. |
| `group=true`, `group.field`, `group.limit` | Grouping, per ESMA's examples. |

#### Query semantics — the parts that will bite you

Verified against the live `esma_registers_upreg` core:

* **There is no default search field.** A bare term matches nothing —
  `q=Bybit` returns `numFound: 0` even for a name that exists elsewhere in
  ESMA's data. You must always write `field:value`. This is why `--contains`
  in this tool is client-side.
* **Matching is case-insensitive.** `ae_entityTypeCode:CSP` and
  `ae_entityTypeCode:csp` both return 259. Facet output comes back
  **lower-cased**, because that is how the index stores it — so
  `--list-values ae_entityTypeCode` prints `mif`, not `MIF`, while the
  documents themselves contain `MIF`.
* **Name fields are whole-string fields, not tokenised text.**
  `ae_entityName:"Catam Asset Management AG"` → 1 hit;
  `ae_entityName:catam*` → 1 hit. Prefix wildcards work; do not expect
  free-text word matching.
* `fq=entity_type:(ae OR aeActivity)` → 52,759 documents (13,930 + 38,829),
  i.e. boolean grouping inside a field works as expected.
* **`_root_` is sortable, and sorting on it is the trick worth knowing.**
  `sort=_root_ asc,id asc` still satisfies the cursor requirement (it ends in
  the unique `id`) but makes every entity's parent and children **contiguous in
  the result stream**, instead of `id asc` putting all 13,930 parents first and
  all the children afterwards. This tool uses it for `upreg`; without it,
  sampling the first N documents shows you nothing but parents.

#### Cores

Verified live, with document counts on 2026-08-15:

| Core | Contents | Docs |
|---|---|---|
| `esma_registers_upreg` | Authorised entities: MiFID firms, AIFMs, UCITS management companies, crowdfunding providers, trading venues, SIs, ARMs, APAs | 101,964 |
| `esma_registers_funds` | AIF / EuSEF / EuVECA funds | 212,341 |
| `esma_registers_saris_new` | Suspensions and removals of instruments | 217,819 |
| `esma_registers_bench_entities` | Benchmark administrators (BMR) | 28,134 |
| `esma_registers_mmf04` | Money market funds | 671 |

Additional cores named in ESMA's documentation but **not** probed here — the
first eight from the A2A help page, the rest from the databases-and-registers
index:
`esma_registers_sanctions`, `esma_registers_mifid_shsexs`,
`esma_registers_coder58`, `esma_registers_bench_benchmarks`,
`esma_registers_stsre`, `esma_registers_priii_documents`,
`esma_registers_priii_securities`, `esma_registers_funds_cbdif`,
`esma_registers_firds`, `esma_registers_fitrs_files`, `esma_registers_radar`,
`esma_registers_eusef`. Reach any of them with `--source solr:<core>`.

There is **no MiCA core.** `esma_registers_mica`, `esma_registers_mica_casp`,
`esma_registers_casp` and `esma_registers_mica_wp` all return HTTP 404. MiCA
lives in the CSV register described further below.

#### The `upreg` document model — parents and children in one flat array

This is the structural trap. A single `docs` array contains both entity records
and their child records, distinguished only by marker fields:

```jsonc
// parent — the licensed entity
{"id": "ae42", "type_s": "parent", "entity_type": "ae", "_root_": "ae42",
 "ae_entityTypeCode": "MIF", "ae_entityName": "Catam Asset Management AG",
 "ae_lei": "529900RUQ6E2Z710XY62", "ae_status": "Active",
 "ae_competentAuthority": "Finanzmarktaufsicht (FMA)", "ae_homeMemberState": "LIECHTENSTEIN",
 "ae_headOfficeAddress": "Landstrasse 34, 9494 Schaan", "ae_officeType": "Head office",
 "ae_authorisationNotificationDate": "2006-09-12T00:00:00Z", "ae_dbId": 42}

// child — one authorised service of that entity
{"id": "aeActivity147", "type_s": "child", "entity_type": "aeActivity", "_root_": "ae42",
 "ac_serviceName": "Reception and transmission of orders", "ac_status": "Active",
 "ac_authorisationNotificationDate": "2006-09-12T00:00:00Z", "ac_authorisationEndDate": ""}
```

Document type distribution across the whole core:

| `entity_type` | Meaning | Docs |
|---|---|---|
| `ae` | The licensed entity (always `type_s: parent`) | 13,930 |
| `aeActivity` | A currently authorised service | 38,829 |
| `aeActivityHistory` | A superseded/withdrawn service record | 48,545 |
| `aeNotHostMmbSt` | Host member state notification (passporting) | 659 |
| `lastModification` | A single bookkeeping document | 1 |

**So: 101,964 documents describe 13,930 entities.** Fetching the core and
counting rows gives you a number that is off by 7×. `eufinreg` groups on
`_root_`, keeps the parent's fields as the row, and collapses each child bucket
into pipe-joined columns plus a count:

```
ae_entityName            → Catam Asset Management AG
aeActivity_count         → 4
aeActivity_ac_serviceName→ Reception and transmission of orders | Portfolio management | …
aeActivity_ac_status     → Active | Active | …
```

Children whose parent is not in the response are **not dropped** — they emerge
as a row flagged `_orphan=true`, so the arithmetic still works.

Selecting on a parent field while still retrieving the children needs a Solr
join, which is the pattern ESMA's own examples use:

```
q  = {!join from=id to=_root_}ae_entityTypeCode:CSP
fq = entity_type:(ae OR aeActivity OR aeNotHostMmbSt)
→ numFound 1,647   (259 parents + their children)
```

`--select CSP` builds exactly that.

#### `upreg` field reference

Parent (`entity_type: ae`) fields:

| Field | Meaning |
|---|---|
| `ae_entityTypeCode` / `ae_entityTypeLabel` | What kind of licence — see the enum below |
| `ae_entityName` | Legal name as notified by the national authority |
| `ae_commercialName` | Trading name (frequently empty) |
| `ae_lei` / `ae_headOfficeLei` | Legal Entity Identifier (frequently empty) |
| `ae_competentAuthority` | The national authority that granted/notified the licence |
| `ae_homeMemberState` / `ae_hostMemberState` | Home state; host state for branches |
| `ae_officeType` | `Head office` or `Branch` |
| `ae_headOfficeAddress` / `ae_branchAddress` | **Registered** address, not necessarily an office |
| `ae_status` | `Active` / `Inactive` |
| `ae_authorisationNotificationDate` (+`Str`) | ISO timestamp; the `…Str` twin is `dd/mm/yyyy` |
| `ae_authorisationWithdrawalEndDateStr` | Withdrawal date, where applicable |
| `ae_lastUpdate` (+`Str`) | Last change to the record |
| `ae_website`, `ae_legalform`, `ae_comment` | Often empty |
| `ae_dbId` | ESMA internal numeric id |
| `timestamp` | Solr **indexing** time, not a data date |

Child (`entity_type: aeActivity`) fields: `ac_serviceName`, `ac_status`,
`ac_authorisationNotificationDate` (+`Str`), `ac_authorisationEndDate`,
`ac_comment`.

#### `upreg` enumerations (live facet counts, 2026-08-15)

`ae_entityTypeCode` — 13,930 parents:

| Code | Label | Count |
|---|---|---|
| `MIF` | investment firm | 7,194 |
| `AIF` | AIFM | 3,786 |
| `UCI` | UCITS management company | 1,705 |
| `EVC` | EuVECA manager | 480 |
| `CSP` | European crowdfunding service provider (ECSPR) | 259 |
| `MIT` | multilateral trading facility | 143 |
| `MIR` | regulated market | 135 |
| `MIS` | systematic internaliser | 112 |
| `MIE` | *(label not exposed for this code)* | 32 |
| `MIO` | organised trading facility | 32 |
| `MIA` | approved reporting mechanism | 20 |
| `ESF` | EuSEF manager | 17 |
| `MIP` | approved publication arrangement | 15 |

`ae_status`: `active` 11,611 · `inactive` 2,226 · *(empty)* 93 —
**inactive entities are in the register**; filter them out yourself if you want
only live licences.

`ae_officeType`: `head office` 12,649 · `branch` 1,281.

`ac_status` on current activities: `active` 38,829 (only value).

`ae_competentAuthority` — 12 largest of many:

| Authority | Entities |
|---|---|
| Federal Financial Supervisory Authority (BaFin) | 2,815 |
| Commission de Surveillance du Secteur Financier (CSSF) | 1,213 |
| Autorité des Marchés Financiers (AMF) | 1,195 |
| Comisión Nacional del Mercado de Valores (CNMV) | 1,137 |
| Netherlands Authority for the Financial Markets (AFM) | 667 |
| Austrian Financial Market Authority (FMA) | 661 |
| Polish Financial Supervisory Authority (KNF) | 626 |
| Banca d'Italia | 599 |
| Central Bank of Ireland (CBI) | 578 |
| Finansinspektionen (FI) | 467 |
| Cyprus Securities and Exchange Commission (CySEC) | 422 |
| Autorité de Contrôle Prudentiel et de Résolution (ACPR) | 387 |

`ae_homeMemberState` — Germany 2,845 · France 1,631 · Luxembourg 1,238 ·
Spain 1,147 · Italy 771 · Netherlands 669 · Austria 668 · Poland 626 ·
Ireland 591 · Sweden 459 · Cyprus 430 · Denmark 367 · … Note this is the
*state*, spelled out in full and lower-cased in the index — not an ISO code.

`ac_serviceName` — the MiFID Annex I services, 15 largest:
investment advice 5,530 · reception and transmission of orders 5,123 ·
portfolio management 4,626 · execution of orders 3,835 · dealing on own
account 3,011 · placing without firm commitment 2,720 · underwriting or placing
on firm commitment 2,049 · safekeeping and administration 1,788 · research and
financial analysis 1,655 · UCITS management 1,387 · foreign exchange services
1,175 · advice on capital structure 1,076 · granting credits or loans 877 ·
services related to underwriting 660 · operation of an MTF 304.

Run `--list-enums` rather than trusting this table; it is a snapshot.

---

### ESMA interim MiCA register — the CSV bulk download

MiCA Articles 109 and 110 require ESMA to keep a central register. There is no
query API; ESMA regenerates five CSV files, **weekly**, at stable URLs.

* **Landing page:** <https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/markets-crypto-assets-regulation-mica>
* **Field documentation:** [`Description_of_the_fields_in_the_interim_MiCA_register.csv`](https://www.esma.europa.eu/sites/default/files/2024-12/Description_of_the_fields_in_the_interim_MiCA_register.csv)
  — genuinely useful, one row per field per template, with format notes.

| Source key | File | Contents |
|---|---|---|
| `mica-casp` | [`CASPS.csv`](https://www.esma.europa.eu/sites/default/files/2024-12/CASPS.csv) | Authorised crypto-asset service providers |
| `mica-art` | `ARTZZ.csv` | Asset-referenced token issuers |
| `mica-emt` | `EMTWP.csv` | E-money token issuers |
| `mica-other` | `OTHER.csv` | White papers for other crypto-assets |
| `mica-ncasp` | `NCASP.csv` | **Non-compliant** entities (Art. 110) — the opposite of a licence list |

Two things about those URLs:

* The `2024-12` path segment is a Drupal upload folder, **not** the content
  date. The files are overwritten in place. On 2026-08-15 `CASPS.csv` carried
  `Last-Modified: Wed, 12 Aug 2026 08:47:01 GMT`. `--raw` records that header.
* A `HEAD` request returns `Content-Length: 0`. Use `GET`. (`CASPS.csv` was
  161,380 bytes.)

#### CASP fields

`ae_competentAuthority`, `ae_homeMemberState` (ISO-2), `ae_lei_name` (legal
name), `ae_lei`, `ae_lei_cou_code`, `ae_commercial_name`, `ae_address`,
`ae_website`, `ae_website_platform`, `ac_authorisationNotificationDate`
(`dd/mm/yyyy`), `ac_authorisationEndDate`, `ac_serviceCode`,
`ac_serviceCode_cou`, `ac_comments`, `ac_lastupdate`.

`ac_serviceCode_cou` is the **passporting** list: pipe-separated ISO-2 codes of
the member states the CASP notified. This is what makes a CASP relevant outside
its home state.

#### `ac_serviceCode` is a documented enum that is not an enum

The field description says the permissible values are the MiCA Art. 3(1)(16)
services `a.` through `j.`. What the file actually contains, because 27
national authorities each type it by hand: `a.\tproviding custody…`,
`a.providing custody…` (no space), `a. providing custody… .` (trailing full
stop), `providing custody and administration…` (no letter at all),
`Providing Custody…` (capitalised), `d. exchange of crypto-assets for other`
(truncated), `…clients I c. exchange…` (a capital **I** where a pipe belongs),
services separated by `/` or `,`, and cells with embedded newlines.

Counting the raw cells produces ~80 "distinct services" for what are really 10.
So:

* `--list-values ac_serviceCode` splits the cell on all of those separators and
  counts **fragments**, not whole cells;
* the parser adds a derived column **`ac_serviceCode_normalised`** with the
  deduplicated letter codes (`a|c|d|e|j`), mapping by letter prefix first and
  distinctive keyword second. Fragments it cannot map appear as `?` rather than
  being silently dropped. Disable with `--no-derived`. The original column is
  never modified.

Normalised over the 329 records in the 2026-08-12 file, the real distribution of
the ten services is:

| Service | | Providers |
|---|---|---|
| `a` | custody and administration of crypto-assets | 220 |
| `j` | transfer services for crypto-assets | 205 |
| `c` | exchange of crypto-assets for funds | 183 |
| `e` | execution of orders | 170 |
| `d` | exchange of crypto-assets for other crypto-assets | 150 |
| `g` | reception and transmission of orders | 93 |
| `i` | portfolio management on crypto-assets | 56 |
| `h` | advice on crypto-assets | 44 |
| `f` | placing of crypto-assets | 36 |
| `b` | operation of a trading platform | 21 |

`CASPS.csv` also has a **trailing comma in its header row** (one nameless
column) and values containing newlines — 386 physical lines for 329 records.
Anything that splits on `\n` will produce garbage.

---

### EBA PSD2 register — the one that is machine-readable by law

Article 15 PSD2 requires the EBA to run a central register of payment and
e-money institutions, and [Commission Implementing Regulation (EU) 2019/410](https://eur-lex.europa.eu/eli/reg_impl/2019/410/oj)
plus [Commission Delegated Regulation (EU) 2019/411](https://eur-lex.europa.eu/eli/reg_del/2019/411/oj)
specify how. The result is the most cleanly machine-readable register of the
four here: a full JSON dump, regenerated nightly, with a published checksum.

* **Landing page:** <https://euclid.eba.europa.eu/register/pir/registerDownload>
* **Authentication:** none.
* **`robots.txt`:** absent (HTTP 404) — no crawl restriction, unlike BaFin's portal.
* **Update cadence:** national authorities update at least daily; the file
  observed on 2026-08-16 was stamped `Sun Aug 16 00:00:16 UTC 2026`.

#### Two requests to the current file

`GET https://euclid.eba.europa.eu/register/api/filemetadata` →

```json
{"latest_version_relative_zip_path": "20260816/download-PSDMD-202608160000.zip",
 "latest_version_relative_zip_size": "19817208",
 "sha256_hash": "b5bc6f70267b68b29908c2392670c117339fd384464cdb5cda397692b77b8f6a",
 "timestamp": "Sun Aug 16 00:00:16 UTC 2026",
 "golden_copy_path_context": "https://euclid.eba.europa.eu/register/downloads/PSDMD/"}
```

Concatenate `golden_copy_path_context` + `latest_version_relative_zip_path` and
`GET` that. **Do not guess the path** — the date segment changes nightly.

The 19 MB ZIP holds two members: a 217 MB JSON document and a `.sha256` sidecar
for it. `eufinreg` verifies **both** digests — the ZIP against `sha256_hash` and
the JSON against its sidecar — because a truncated download of a 217 MB document
otherwise surfaces as a JSON parse error thousands of lines from the cause.
A mismatch is a hard error, not a warning.

A third endpoint, `GET /register/pir-api/metadata` (130 KB), returns the
register's own data dictionary: property codes with labels, the entity-type
code list, the authority code list, the 14 service codes and the exclusion
codes. `eufinreg` fetches it to add the `*_label` columns, so no code table is
hardcoded in this project. `--no-derived` skips both the columns and the request.

The dictionary is not quite complete: on 2026-08-16 the authority code `LV_LV`
appeared on 60 institutions but was absent from the `RDL_COM_AUT_PSD` code list,
so those rows get an empty `CA_OwnerName`. An unknown code always yields a blank
label here rather than a dropped row or a crash — `--inspect` shows it as a fill
rate below 100% on the label column, next to a fill rate of 100% on the code
column it was derived from.

#### The document model

```jsonc
{"CA_OwnerID": "IE_CBI",                              // supervising authority
 "EntityCode": "IE_CBI!C58301",
 "EntityType": "PSD_PI",
 "Properties": [{"ENT_NAM": "Fire Financial Services Limited"},
                {"ENT_ADD": "Dogpatch Labs, Custom House Quay"},
                {"ENT_AUT": ["2018-07-02"]}],         // str OR list, one key each
 "Services":   [{"AT": ["PS_03A", "PS_05B"]},         // keyed by ISO-2 country
                {"IE": ["PS_03A", "PS_05B"]}],
 "__EBA_EntityVersion": "20260814223344848"}
```

Three traps:

* **`Properties` is a list of single-key objects**, not one object. Values are
  a string most of the time and a list some of the time — 4,990 records carry
  several `ENT_NAM` values (a firm filed under both a Cyrillic and a Latin
  name), 5,138 several `ENT_ADD`, 2,313 two `ENT_AUT` dates. Reading only the
  first element quietly loses the rest. Verified across all 328,965 records: no
  record repeats a property code, so merging the objects is lossless.
* **`Services` is the passporting map** and the only place the real geographic
  reach is recorded. Only the 4,656 institution records carry it; its values are
  a list for most and a bare string for some.
* **The top level is `[[{disclaimer}], [ …entities… ]]`** — a two-element array
  of arrays. `eufinreg` searches for the entity array rather than indexing into
  that shape.

`eufinreg` emits one row per record, property codes verbatim, plus four columns
built from `Services`:

| Column | Meaning |
|---|---|
| `ENT_SER` | Union of the service codes, deduplicated |
| `ENT_SER_COU` | ISO-2 states the institution may serve |
| `ENT_SER_COU_count` | How many — the headline passporting number |
| `ENT_SER_BY_COU` | `AT=PS_010,PS_020 \| DE=…`, so a union cannot hide an asymmetry |

#### Entity types (live counts, 2026-08-16)

| Code | Label | Records |
|---|---|---|
| `PSD_AG` | Agent | 322,314 |
| `PSD_EPI` | Exempted payment institution | 2,758 |
| `PSD_EXC` | Service provider excluded from the scope of PSD2 | 1,659 |
| `PSD_PI` | Payment institution | 1,011 |
| `PSD_EMI` | Electronic-money institution | 427 |
| `PSD_ENL` | Entitled under national law | 336 |
| `PSD_BR` | Branch | 244 |
| `PSD_AISP` | Account information service provider | 129 |
| `PSD_EEMI` | Exempted electronic-money institution | 87 |

**328,965 records, of which 322,558 are agents and branches.** So `--select`
defaults to institutions only — the seven types above the `PSD_BR` line, 6,407
records — which is also what the register's own search UI does. `--select ALL`
brings the rest back.

Institutions by supervising authority, 14 largest:

| Authority | `CA_OwnerID` | Institutions |
|---|---|---|
| Polish Financial Supervision Authority | `PL_PFSA` | 2,497 |
| **Federal Financial Supervisory Authority (BaFin)** | `DE_BAFIN` | **1,037** |
| Central Bank of Ireland | `IE_CBI` | 382 |
| De Nederlandsche Bank | `NL_DNB` | 296 |
| Autorité de contrôle prudentiel et de résolution | `FR_ACPR` | 240 |
| Czech National Bank | `CZ_CNB` | 207 |
| Banca d'Italia | `IT_BI` | 194 |
| Bank of Lithuania | `LT_BL` | 191 |
| Finansinspektionen | `SE_FINA` | 184 |
| Danish FSA | `DK_DFSA` | 147 |
| Banco de España | `ES_BE` | 142 |
| Finanssivalvonta | `FI_FIN-FSA` | 127 |
| Malta Financial Services Authority | `MT_MFSA` | 92 |
| Austrian Financial Market Authority | `AT_FMA` | 87 |

#### Service codes

`ES_010` issuing, distribution and redemption of electronic money ·
`PS_010` cash placed on a payment account · `PS_020` cash withdrawals ·
`PS_03A`/`PS_03B`/`PS_03C` direct debits / payment transactions / credit
transfers · `PS_04A`/`PS_04B`/`PS_04C` the same three covered by a credit line ·
`PS_05A` issuing of payment instruments · `PS_05B` acquiring of payment
transactions · `PS_060` money remittance · `PS_070` payment initiation ·
`PS_080` account information.

Run `--list-enums` rather than trusting that list.

#### Costs and caveats

* Every run downloads the whole 19 MB archive; `--select` filters **after** the
  download, it does not shrink it. Parsing the 217 MB JSON peaks around
  **1.4 GB of RSS** and takes a couple of seconds. Write the output to a file
  and reuse it rather than re-running.
* **`PSD_AG` records are largely natural persons** — sole traders and
  individuals acting as agents, named in full with an address. They fall under
  [Regulation (EU) 2018/1725](https://eur-lex.europa.eu/eli/reg/2018/1725/oj),
  and the register says so in its own disclaimer block. Excluded by default here
  for that reason as much as for the row count.
* Agents carry a `DER_CHI_ENT_AUT` status and **195,920 of the 322,314 are
  `Inactive`** — withdrawn agents stay in the file. Institutions have no such
  column; they carry `ENT_AUT` dates instead.
* `PSD_EXC` and `PSD_ENL` are **not authorisations**. They record providers
  outside PSD2's scope (limited networks, digital-content billing) and providers
  entitled under national law. 942 of BaFin's 1,037 records are `PSD_EXC`;
  treating that number as "German payment institutions" overstates it by 12×.
  The real BaFin figures are **82 payment institutions and 13 e-money
  institutions**.
* The EBA's disclaimer: "this Register has no legal significance and confers no
  rights in law… responsibility for the accuracy of that information lies with
  the competent authorities at national level."

#### The rest of EUCLID, and why this project stops here

The same host serves the **Credit Institutions Register** (CIR — the EU banks
list). It has no bulk download: `/register/cir-api/filemetadata` returns 404 and
`/register/downloads/CIRMD/` does not exist. Its only interface is
`POST /register/api/search/entities`, which takes a **raw MongoDB query
document** (`{"$and":[{"_payload.CA_OwnerID":"DE_BAFIN"}]}`) and is neither
documented nor advertised. It works — and on 2026-08-16 it answered that query
with PSD records, not credit institutions, so it is also not the endpoint the
CIR UI uses for its own searches.

`eufinreg` does not build on it. Shipping a Mongo query builder aimed at a
regulator's backend is a different kind of thing from reading a published file,
and an undocumented endpoint can change shape without anyone owing you notice.
If you need EU credit institutions today, the honest answers are the CIR web UI
at <https://euclid.eba.europa.eu/register/cir/search> or the ECB's
[list of supervised entities](https://www.bankingsupervision.europa.eu/framework/supervised-banks/html/index.en.html),
published as PDF and XLSX at a URL whose date segment changes each release.

---

### EUDAMED — the medical device industry, with contact details

Same premise, different industry. Under [MDR 2017/745](https://eur-lex.europa.eu/eli/reg/2017/745/oj)
and [IVDR 2017/746](https://eur-lex.europa.eu/eli/reg/2017/746/oj), you cannot
place a medical device on the EU market without registering in EUDAMED as an
*economic operator*. So EUDAMED is a near-complete directory of the European
medical device industry — **48,893 organisations on 2026-08-16** — and unlike the
financial registers it carries an email address and a phone number for almost
every one.

* **Public site:** <https://ec.europa.eu/tools/eudamed>
* **Interface:** the JSON API the public site itself runs on. No authentication,
  no key, no registration.
* **`robots.txt`:** `ec.europa.eu/robots.txt` carries 201 `Disallow` rules for
  `*`; none of them matches `/tools/eudamed/`. (Contrast BaFin below.)
* **Documented by the Commission?** No. This is an application backend, not a
  published API — treat the field list as observed, not promised, and run
  `--inspect` before trusting it.

| Source key | Endpoint | Contents | Records |
|---|---|---|---|
| `eudamed-eo` | `api/eos` | Economic operators — the company directory | 48,893 |
| `eudamed-nb` | `api/ses/` | Notified bodies, cross-linked to their NANDO notifications | 70 |

#### `languageIso2Code` is not a display setting — it is a filter

This is the trap, and it is a bad one:

| Request | `totalElements` |
|---|---|
| `api/ses/?page=0&size=1` | **1,890** |
| `api/ses/?page=0&size=1&languageIso2Code=en` | **70** |

Omit the parameter and the endpoint returns **one row per notified body per
language**. A 300-record page contains 12 distinct organisations, each repeated
about 25 times with `countryName` in Bulgarian, English, German, Croatian and so
on — same `uuid`, same `ulid`, different language. There are 70 notified bodies;
1,890 is 70 × 27 languages. Nothing in the response says so.

On `api/eos` the same omission is at least loud: it returns **HTTP 500**.

`eufinreg` always sends `languageIso2Code=en`, and there is a test asserting it.

#### `size` is silently capped at 300

`size=500` and `size=1000` both return exactly 300 records, with HTTP 200 and no
warning. A client that asks for 1000, receives 300, and concludes "fewer than I
asked for, so that must be everything" will report 300 medical device companies
in Europe. `eufinreg` clamps `--page-size` to 300 and pages on the `last` flag.

#### Paging needs `sort=ulid,ASC`

Deep paging is only stable on a unique key. Verified on the live endpoint:

* `sort=eudamedIdentifier,ASC` → **HTTP 500**
* `sort=name,ASC` → works, but names are not unique
* `sort=ulid,ASC` → works; ULIDs are unique and lexicographically ordered

Page 160 (`size=300`, offset 48,000) returns a full page, so there is no hidden
deep-paging ceiling. A full `eudamed-eo` pull is 164 requests — about three
minutes at the default 1 s delay.

#### Actor types (live counts, 2026-08-16)

| `--select` | `actorTypeCode` | Organisations |
|---|---|---|
| `manufacturer` (`mf`) | `refdata.actor-type.manufacturer` | 31,931 |
| `importer` (`im`) | `refdata.actor-type.importer` | 12,363 |
| `authorised-representative` (`ar`) | `refdata.actor-type.authorised-representative` | 2,938 |
| `system-procedure-pack-producer` (`sppp`) | `refdata.actor-type.system-procedure-pack-producer` | 1,661 |

Those four sum to exactly 48,893, which is how you know the type list is
complete. `--select` is pushed server-side, so it is genuinely cheap; an unknown
value is an error rather than a silently ignored filter.

`--query` takes raw URL parameters for filters this package does not model:

```bash
uv run eufinreg --source eudamed-eo --query 'countryIso2Code=DE' -o de-devices.csv
```

#### Fields

Scalar fields keep EUDAMED's own names: `name`, `eudamedIdentifier` (the Single
Registration Number, also as `srn`), `countryIso2Code`, `geographicalAddress`,
`postalZone`, `cityName`, `electronicMail`, `telephone`, `dateOfRegistration`,
`versionNumber`, `latestVersion`.

The nested blocks are flattened: reference-data objects become their `code`
(`actorType`, `actorStatus`) plus `_srnCode` / `_category` companions;
multilingual `names` blocks are pipe-joined; and a notified body's
`legislationLinks` split into `legislationCodes` (`…mdr`, `…ivdr`, `…mdd`,
`…ivdd`, `…aimdd`) and the matching NANDO URLs. Anything nested that this
package does not recognise is preserved as compact JSON under its own name
rather than dropped, so a new field shows up in `--inspect`.

Of the 70 notified bodies, 52 are MDR-designated and 19 IVDR-designated; Italy
and Germany host 11 each, and five are in Türkiye.

---

### CTIS — who runs clinical trials in Europe

Since 2022, [Regulation 536/2014](https://eur-lex.europa.eu/eli/reg/2014/536/oj)
requires every clinical trial in the EU/EEA to be authorised through CTIS, and
the public portal publishes the result. Read as a *company* list rather than a
science list, it answers something no commercial database answers as well: who
is actually running trials in Europe, where, on what — with `sponsor` and
`sponsorType` as fields.

* **Public portal:** <https://euclinicaltrials.eu/ctis-public/search>
* **Interface:** `POST https://euclinicaltrials.eu/ctis-public-api/search`. No
  authentication. The portal's own JS names it, along with an asynchronous CSV
  export and a second host, `/ct-public-api-services/services`.
* **Records:** 12,229 trials on 2026-08-16.

Request body — all three keys matter:

```json
{"pagination":    {"page": 1, "size": 500},
 "sort":          {"property": "ctNumber", "direction": "ASC"},
 "searchCriteria": {}}
```

#### `searchCriteria` is mandatory even when empty

Send only `pagination` and the API replies `200` with `totalRecords: 0`. Not an
error, not a hint — a perfectly well-formed "no results" for a request that
should have matched all 12,229 trials. Add `"searchCriteria": {}` and the same
request returns everything.

#### Paging stops at 10,000 while `totalRecords` says 12,229

The search sits behind a result window and there is no error when you hit it:

| Request | Records returned |
|---|---|
| page 100, size 100 (records 9,901–10,000) | 100 |
| page 101, size 100 (records 10,001+) | **0** |
| page 20, size 500 (records 9,501–10,000) | 500 |
| page 21, size 500 | **0** |

A client that pages until it gets an empty page collects exactly 10,000 rows and
has no way to know 2,229 are missing — the run looks clean. `eufinreg` compares
what it collected against `totalRecords` and says so:

```
eufinreg: received 10000 record(s)
eufinreg: warning: CTIS returned 10000 of 12229 matching trials — its search
          stops at 10,000 records however you page. Narrow the result set with
          --query to reach the rest.
```

The workaround is to partition: run several narrower `--query` searches, each
under 10,000 matches, and concatenate.

#### Selecting

`--select` is not supported — CTIS has no server-side selector this package
models, and inventing one would mean guessing at `searchCriteria` keys.
`--query` covers it instead:

```bash
uv run eufinreg --source ctis --query 'CAR-T'                     # free text
uv run eufinreg --source ctis --query '{"containAll": "vaccine"}' # raw criteria
```

A value starting with `{` is parsed and sent as the whole `searchCriteria`
object; anything else becomes `containAll`, the portal's own free-text search.

#### Fields

`ctNumber` (unique, and the sort key), `ctStatus`, `ctTitle`, `shortTitle`,
`conditions`, `trialCountries` (pipe-joined `Country:siteCount`),
`decisionDate`, `decisionDateOverall`, `therapeuticAreas`, **`sponsor`**,
**`sponsorType`**, `trialPhase`, `product`, `ageGroup`, `gender`,
`totalNumberEnrolled`, `primaryEndPoint`, `endPoint`, `resultsFirstReceived`,
`lastUpdated`, `lastPublicationUpdate`.

Over the 10,000 reachable trials, `sponsorType` splits as pharmaceutical company
5,363 · hospital/clinic 3,057 · educational institution 502 ·
laboratory/research facility 355 · patient organisation 323 — and note the
`Pharmaceutical company, Pharmaceutical company` bucket with 163 rows, which is
a co-sponsored trial and *not* a separate category. The largest single sponsors
are Merck Sharp & Dohme (253), Novartis (195), AstraZeneca (194),
F. Hoffmann-La Roche (189) and Pfizer (124).

---

### FINMA (Switzerland) — the authorisation list as a daily CSV

Switzerland is not in the EEA. Nothing in ESMA's, the EBA's or MiCA's registers
contains a Swiss licence, so a DACH question that stops at the EU border stops
two-thirds of the way through. FINMA supervises the lot — banks, insurers,
securities firms, fund management companies, portfolio managers, trustees,
trading venues — and publishes the whole authorisation list as a file.

* **Landing page:** <https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/>
* **The file:** `https://www.finma.ch/en/~/media/finma/dokumente/bewilligungstraeger/csv/uid.csv`
* **Authentication:** none. **`robots.txt`:** `Allow: /`, with six `Disallow`
  rules — `/suche/`, `/sitemap/`, `/error/`, `/sitecore/media library/` and,
  notably, the *insurance-intermediary register search*. The published files sit
  under `/~/media/…`, the public media alias, which no rule matches.
* **Vintage:** `Last-Modified` was `Sun, 16 Aug 2026 03:05:12 GMT` on
  2026-08-16 — regenerated that morning.

**The `hash=` parameter is not required.** FINMA's own links look like
`…/uid.csv?sc_lang=en&hash=3FA0C019755229678488CD7CFD5B70D5` — a Sitecore media
hash. The bare path returns the same bytes, which matters: a client that needed
the hash would have to scrape the landing page for a value that changes whenever
the file does.

Semicolon-separated, quoted, UTF-8 with a BOM, seven columns:

```
"Name";"City";"AuthorisationTypeDE";"AuthorisationTypeFR";"AuthorisationTypeIT";"AuthorisationTypeEN";"UID"
"UBS AG";"Zürich";"Bank";"Banque";"Banca";"Bank";"CHE-101.329.561"
```

**One row is one authorisation, not one institution.** Zürcher Kantonalbank
appears twice — `Bank` and `Custodian bank`. 2,938 rows on 2026-08-16 describe
**2,744 distinct UIDs**, so counting rows overstates the Swiss financial sector
by 7%. `eufinreg` emits the rows block-structured and lets the standard
flattener collapse them, exactly as it does for ESMA's `upreg` core:

```
Name                                   → Zürcher Kantonalbank
authorisation_count                    → 2
authorisation_AuthorisationTypeEN      → Bank | Custodian bank
uid_digits                             → 108954607
```

Live distribution of the 37 authorisation types (2026-08-16), 12 largest:

| Authorisation | Count |
|---|---|
| Portfolio manager | 1,448 |
| Manager of collective assets | 342 |
| Bank | 209 |
| Raiffeisen bank | 208 |
| Trustee | 156 |
| Non-life insurer | 80 |
| Representatives of foreign collective investment schemes (CISA) | 68 |
| Fund management company | 54 |
| Foreign bank representative office | 39 |
| Foreign securities firm representative office | 36 |
| Securities firm | 34 |
| Custodian bank | 29 |

84 rows carry **no UID** — mostly foreign representative offices. This source
keys those on name + city rather than collapsing them into one empty-keyed row.

**What else is on that page, and why it is not wrapped.** FINMA also publishes
per-category XLSX and PDF (`beh` securities firms, `vu` insurers, `bourses`
trading venues, `sro` self-regulatory organisations, `grfinig`, `fintech` …),
which carry a few extra columns each. `uid.csv` is the only file that spans
every category, and it is the one with the join key. The second CSV on the page,
`uvvreg.csv`, is the insurance-intermediary register — but it contains **only
two ID columns** (`Registernummer`, `WebRegNr`, 11,979 rows) with no names, and
the search UI that would resolve them is the one path FINMA's `robots.txt`
disallows. So that register is documented here and left alone.

---

### The Swiss UID register — a public SOAP service with a silent ceiling

Every Swiss enterprise, association, foundation and sole trader has a UID
(`CHE-101.329.561`), assigned by the Federal Statistical Office. The register
behind it is exposed as a **public SOAP web service with no authentication**.

* **Service:** `https://www.uid-wse.admin.ch/V5.0/PublicServices.svc`
* **WSDL:** `?wsdl`, or `?singleWsdl` for the schema inline. `BasicHttpBinding`
  → SOAP 1.1: `Content-Type: text/xml` plus a **quoted** `SOAPAction` header.
* **Operations:** `Search`, `GetByUID`, `ValidateUID`, `ValidateVatNumber`,
  `GetOrganisationSample`. This project wraps the first two.
* **`robots.txt`:** absent (HTTP 404) on both `uid.admin.ch` and
  `uid-wse.admin.ch`.

`Search` accepts organisation name, person name, address (street, town,
`swissZipCode`, `municipalityId`, **`cantonAbbreviation`**), legal form, and
UID-register / commercial-register / VAT-register status filters.

#### `Search` returns at most 30 records and does not mention it

The `searchConfiguration` block has a `maxNumberOfRecords` field, so a client
naturally assumes it means something. Verified 2026-08-16:

| Request | `maxNumberOfRecords` | Records returned |
|---|---|---|
| canton `AI` (Appenzell Innerrhoden — the smallest canton) | 1000 | **30** |
| postcode `9050` | 2000 | **30** |

There is no total count in the response, no cursor, no offset parameter and no
error. A canton with thousands of businesses answers with thirty, and "thirty
results" is indistinguishable from "thirty matches". `eufinreg` clamps the
request to 30 and **raises a warning whenever the answer comes back at the
ceiling**, because that is the only signal there is.

Which makes this a **lookup interface, not a directory**. Used as intended — one
entity at a time, by UID — it is excellent.

#### `GetByUID` returns an array, and sometimes it has two elements

```
GetByUIDResult
  organisationType   ← UBS AG, Bahnhofstrasse 45, 8001 Zürich, canton ZH
  organisationType   ← UBS AG, Aeschenvorstadt 1, 4051 Basel, canton BS
```

Same UID, same name, two commercial-register entries (different `CH.EHRAID`).
A client that reads element `[0]` silently picks one of a company's seats. The
field is typed `ArrayOfOrganisationType`; nothing in the response says how many
to expect.

#### What a record carries

Verified against `CHE-101.329.561`:

* `organisationIdentification` — UID, `organisationName`, `organisationLegalName`,
  `legalForm` (a numeric eCH code), and `OtherOrganisationId` blocks:
  `CH.HR` (commercial register number), `CH.EHRAID`, `CH.ESTVID` (tax);
* `address` — street, house number, town, `swissZipCode`, `municipalityId`,
  `cantonAbbreviation`, `EGID` (federal building identifier), `dateOfLastCheck`;
* `uidregInformation` — `uidregStatusEnterpriseDetail`, `uidregPublicStatus`;
* `commercialRegisterInformation` — status, entry status, enterprise type,
  name translations;
* `vatRegisterInformation` — VAT status, entry/liquidation dates, and `uidVat`,
  which is the **VAT group head** where the entity is a member;
* `groupRelationship` — VAT-group and headquarters/branch relationships, with
  the counterpart's UID and its role. A corporate-structure edge, for free.

`eufinreg` flattens this to dotted paths (`address.town`,
`commercialRegisterInformation.commercialRegisterStatus`), joining repeated
paths with `|` rather than overwriting them, and derives exactly one column: a
`uid` in the printed `CHE-101.329.561` form — which is the form FINMA's CSV
uses, so the two sources join with no transform.

**The status codes are numeric and this project does not invent labels for
them.** `uidregStatusEnterpriseDetail` is `1`–`7`, `commercialRegisterStatus`
`1`–`3`, `vatStatus` `1`–`3`. The WSDL enumerates the values without
documenting their meaning; the meanings live in the eCH-0108 standard. Guessing
would be worse than leaving them as codes.

One data quirk worth knowing: address fields can contain **embedded newlines**
(`street` = `"Aeschenvorstadt\r\n4051 Basel"` on one UBS record). The CSV writer
quotes them correctly; a hand-rolled one splitting on `\n` will not.

---

### GISA (Austria) — a million trade licences in a 7-Zip archive

Austria requires a *Gewerbeberechtigung* to trade, and GISA is the federal
register of them. Its public search at `gisa.gv.at/search` is an ASP.NET
WebForms application bound to `__VIEWSTATE` and `__EVENTVALIDATION` — the same
category as BaFin's portal, and just as unautomatable.

**But the ministry publishes the register as open data anyway**, through a small
REST service on the same host:

* **Catalogue:** <https://www.data.gv.at/katalog/dataset/e49a1510-9d93-4277-8467-48a1efc9f046>
* **Endpoint:** `GET https://www.gisa.gv.at/gisa-svc-public/GisaPublicV2.svc/ogd/<dataset>/<format>`
* **Licence:** **CC BY 4.0**, stated in the catalogue.
* **`robots.txt`:** none (HTTP 404). No authentication.

| Dataset | Contents | Served as |
|---|---|---|
| `stat03` → `--source gisa` | Every active trade licence — **1,030,111** on 2026-08-16 | 9.2 MB `.7z` → 211 MB CSV |
| `stat02` → `--source gisa-codes` | The 907 trade codes and their standardised wording | 107 KB plain CSV |
| `stat03hist` | Monthly historical statistics | `.7z` |

#### Four traps, all silent

1. **The `/csv` path does not return CSV.** `stat03` comes back as a 7-Zip
   archive with `Content-Type: application/application/x-7z-compressed` — yes,
   the prefix is doubled — and `Content-Disposition: attachment;
   filename=OgdAufrechteGewerbeberechtigung_2026.08.csv.7z`. The `stat02` code
   list at the same kind of URL *is* plain `text/csv`. This project decides by
   the file's **magic bytes**, so both work and neither depends on a header the
   service gets wrong.
2. **The column names are lower case in the file and upper case in the
   documentation.** data.gv.at documents `NUTS2`, `GEWERBEART`, `RECHTSWIRKSAM`;
   the file serves `nuts2`, `gewerbeart`, `rechtswirksam`. A case-sensitive
   filter on the documented spelling matches nothing, with no error. `--query`
   here accepts either, and rejects a name that exists in neither.
3. **`gewerbeart` means two different things in the two datasets.** In the
   licence file it is a numeric code (`5`); in the code list it is the German
   label (`konzessioniertes Gewerbe`). Joining them on the column name yields an
   empty join.
4. **The catalogue's `byte_size` is wrong by an order of magnitude** — it claims
   22.7 MB for a file that expands to 211 MB.

#### The data

Fourteen columns: `nuts1`, `nuts2`, `nuts3`, `lau1` (Bezirk), `lau2` (Gemeinde),
`adress_art`, `gewerbeschluessel`, `gewerbewortlaut`, `gewerbeart`,
`postleitzahl`, `ortschaft`, `rechtswirksam` (effective date), `ruhend_von`
(dormant since), `inhaber_pers_art`. `eufinreg` adds `gewerbeart_label`,
`adress_art_label`, `inhaber_pers_art_label`, `nuts2_label` (the Bundesland) and
`_vintage` — all from the publisher's own documented code lists, all suppressed
by `--no-derived`.

Live distribution, 2026-08-16:

| `gewerbeart` | | Licences |
|---|---|---|
| `2` | freies Gewerbe | 651,977 |
| `1` | reglementiertes Gewerbe | 353,564 |
| `5` | konzessioniertes Gewerbe | 22,130 |
| `8` | Nebengewerbe | 1,171 |
| *(empty)* | — | 1,091 |
| `9` | gewerbliche Ausübung eines Patentes | 146 |
| `3`,`4`,`6`,`7` | the remaining categories | 32 combined |

By Bundesland: Wien 216,731 · Niederösterreich 209,836 · Oberösterreich 158,320
· Steiermark 139,962 · Tirol 89,851 · Salzburg 72,515 · Kärnten 65,986 ·
Vorarlberg 40,216 · Burgenland 36,694. **8,291 licences are dormant**
(`ruhend_von` set) and 642 of the 907 trade codes are actually in use.
The largest single trade is `Personenbetreuung` (74,668).

#### No names, and why that matters

The publisher states it plainly: *"Es werden die aufrechten
Gewerbeberechtigungen ohne personenbezogene Daten zur Verfügung gestellt."*
**700,453 of the 1,030,111 licences are held by natural persons**, which is
presumably the reason. So this is a licence register without licence holders:
excellent for market structure, density, category mix and dormancy; useless as a
company directory on its own. It is also why `gisa` declares no key columns —
without an identifier, `--diff` can report that a licence appeared or
disappeared, but never that one *changed*, and it says so rather than inventing
the difference.

#### Cost, and filtering during the parse

One request, 9.2 MB down, 211 MB and 1.03 M rows parsed. There is no
server-side filter, so `--select` and `--query` are applied **while the CSV is
being read**, before rows are materialised — the difference between a few
thousand dicts and a million. The run reports what it did:

```
eufinreg: received 5116 record(s)
eufinreg: warning: kept 5,116 of 1,030,111 rows; the filter was applied while
          parsing, so the rows you did not ask for were never materialised
```

Reading the archive needs `py7zr` (`uv sync --extra at`), because 7-Zip is not
in the Python standard library. Everything else in this project, including the
`gisa-codes` list, works without it.

---

### GLEIF — the identity key, and the one register that publishes deltas

Every other register here answers "who may do X". GLEIF answers "who is this":
**3,403,760 legal entities** worldwide, each with a 20-character LEI, a legal
name, an address, a legal form, a status — and the number the entity holds in
**its own national commercial register**.

* **API:** `https://api.gleif.org/api/v1/lei-records` — JSON:API, no key.
  `robots.txt` is `User-agent: * / Disallow:` — an empty Disallow, i.e. allow all.
* **Licence: CC0.** The only source in this project that is unambiguously
  redistributable.
* **Bulk + deltas:** `https://goldencopy.gleif.org/api/v2/golden-copies/publishes?format=json`
  indexes the daily files — the full set (476 MB CSV), the relationship records
  (484,565 parent/child links) and **IntraDay / LastDay / LastWeek / LastMonth
  delta files**.

That last point deserves emphasis, because it is the exception that proves this
project's thesis. Every other register publishes state and destroys history;
**GLEIF publishes what changed**. Its LastDay file on 2026-08-16 held 29,341
changed records in 3.75 MB. If the EBA and ESMA did the same, half of
`eufinreg`'s snapshot machinery would be unnecessary.

#### Why it matters here: the German register bridge

A German LEI record carries:

```
entity.registeredAt.id  → RA000221          (an authority code — a specific Amtsgericht)
entity.registeredAs     → HRB 204159        (the Handelsregister number)
```

BaFin's portal is `Disallow: /` and handelsregister.de is a form. This is a
route to the German commercial-register identifier for 254,108 German entities,
published under CC0, by a body whose whole purpose is that identifiers be
shareable. Note the caveat before joining on it: **`HRB 204159` is unique only
within its court**, which is what `registeredAt.id` is for.

#### Two limits, and GLEIF states both

| Limit | What happens |
|---|---|
| `page[size]` > 200 | HTTP 400: *"The page.size must be between 1 and 200."* |
| more than 10,000 results | HTTP 400 naming the ceiling |

Contrast EUDAMED, which silently caps at 300 and returns HTTP 200. An API that
tells you its own limits is rarer than it should be, and it is the difference
between a wrong answer and an error.

The ceiling matters in practice: **every DACH country is over it.** Germany
alone has 254,108 LEIs, Switzerland 27,997, Liechtenstein 15,423. So
`--select DE` is truncated at 10,000, this tool says so, and the honest routes
are a narrower filter (`--query 'entity.legalAddress.city=Zug'`) or the bulk
file.

Live counts, 2026-08-16: `DE` 254,108 · `CH` 27,997 · `LI` 15,423 · worldwide
3,403,760.

---

### VIES — the number nobody lies to the tax authority about

A VAT registration is issued by a tax authority and misusing one has
consequences that a company website does not. Under Council Regulation (EU)
904/2010 a supplier is expected to verify a customer's VAT number for
intra-Community supply, and VIES is how.

* **REST:** `https://ec.europa.eu/taxation_customs/vies/rest-api`
* `GET /ms/{country}/vat/{number}` validates one number, no key.
* `GET /check-status` reports which member states' systems are answering.

**It is a validator, not a directory** — there is no "list every VAT number in
Germany", and there should not be. This source therefore declares itself
non-archivable: a scheduled crawl of a validation service is abuse.

#### The trap: `userError` separates two very different answers

```json
{"isValid": false, "userError": "INVALID"}          ← the number is wrong
{"isValid": false, "userError": "MS_UNAVAILABLE"}   ← the country's system is down
```

A client that reads `isValid` without reading `userError` records a live company
as unregistered because a national system was down for ten minutes. `eufinreg`
raises a warning on every unavailable answer rather than letting it pass as a
negative result:

```
eufinreg: warning: VIES could not reach DE (MS_UNAVAILABLE) — those answers are
          'we do not know', not 'not registered'. Filing them as invalid would
          record a live company as unregistered; retry rather than conclude.
```

Names and addresses come back only where the member state chooses to disclose
them; several return `---` by policy even for valid numbers, which is a "we will
not say", not a "no such company".

---

### TED — public money, and the earliest signal here

A contract award is published because the money is not the buyer's own, it names
the winning company, and it happens months before the hiring that follows from
it. Where a licence register says who *may* trade, TED says who just *got paid
to*.

* **API:** `POST https://api.ted.europa.eu/v3/notices/search` — no key.
* **Query language:** TED's own expert syntax:
  `notice-type IN (can-standard) AND buyer-country IN (DEU AUT) AND publication-date>=today(-30)`

Four things this project learned the hard way:

* **`GET` does not work** — `405 Request method 'GET' is not supported`. And the
  neighbouring `/fields` endpoint *does* demand an `Authorization` header while
  `/search` does not, so an auth failure on one says nothing about the other.
* **`limit` caps at 250**, stated in the error: *"Value (300) of parameter
  'limit' exceeds maximum allowed value (250)"*.
* **Country codes are ISO alpha-3** (`DEU`, `AUT`), not the alpha-2 every other
  source here uses. The wrong one returns zero notices and no error, so
  `--select de,at` converts them for you.
* **Text fields are multilingual objects** — `{"deu": ["Polizei Berlin"]}`.
  There is no single "name" string. This tool picks a language in a documented
  order rather than taking whichever key came first.

`winner-name` repeats **once per lot**, so a notice awarding six lots to three
companies lists six names. They are de-duplicated, in order, with the original
count kept in `winner_count`.

**Switzerland is not in TED.** It is not an EEA member and publishes
procurement nationally (simap.ch). A DACH answer from this source is
two-thirds complete by construction — which is the same gap FINMA fills on the
licensing side.

---

### Zefix and BASG — found, documented, deliberately not wrapped

Two DACH interfaces that exist and that this project does not use. Both
decisions are about the operator's evident intent, not about difficulty.

**Zefix** (the Swiss central business-name index, `zefix.admin.ch`) publishes a
REST API at `/ZefixPublicREST/api/v1/…`. Every endpoint tried on 2026-08-16 —
`company/search`, `legalForm`, `community` — returned **HTTP 401**, and
`zefix.admin.ch/robots.txt` is:

```
User-agent: *
Disallow: /
```

…with narrow `Allow:` exceptions for Googlebot and Bingbot on five landing
pages. A documented API behind a credential, on a host that excludes automated
clients from every path. Credentials are obtainable from the operator; this
project ships none, and the UID register above answers most of the same
questions without one.

**BASG / Medikamente Info Austria** (`medikamente.basg.gv.at`) is the Austrian
medicines register — marketing authorisations and their holders, i.e. the
Austrian pharma industry. It is an Angular application over a clean REST API:
`POST /api/api/v1/medication/search` with `page`/`size`, plus `export`,
`last-update` and `ctl-filter` endpoints, all discoverable from the main bundle.
It answers:

```
HTTP 401  Unauthorized - Invalid API token
```

The token is hardcoded in the public JS bundle, so "extracting" it is trivial.
That is precisely why it is not extracted here: a shared secret shipped to every
browser is still an operator saying *ask me first*. The endpoint list is
recorded above so that anyone who does ask knows what they are asking for.

---

### BaFin (Germany) — an export button behind a blanket robots ban

> **Corrected on 2026-08-16.** Earlier versions of this file said no CSV, XML or
> Excel export exists in BaFin's markup. That was wrong: it does, on the result
> pages rather than the empty search forms. The conclusion — that this project
> will not automate the portal — is unchanged, but for a different and better
> reason. The evidence is below.

BaFin runs three separate Struts applications, one per register:

| Register | Application | Search action |
|---|---|---|
| Unternehmensdatenbank (institutions) | `portal.mvp.bafin.de/database/InstInfo/` | `sucheForm.do` |
| ZAG-Instituts-Register (§§ 43, 44 ZAG) | `portal.mvp.bafin.de/database/ZahlInstInfo/` | `suche.do` |
| Vertraglich gebundene Vermittler (tied agents) | `portal.mvp.bafin.de/database/VGVInfo/` | `vermittlerSucheForm.do` |

**The export exists.** Every result page is rendered by
[displaytag](https://displaytag.sourceforge.net/) and ends with a line reading
`Exportoptionen: CSV | XML | Excel`. The links are the search URL plus displaytag's
export parameters — `6578706f7274=1` (hex for `export`) and `d-<tableId>-e=<n>`,
where `n` is 1 for CSV, 3 for XML, 5 for Excel:

```
https://portal.mvp.bafin.de/database/InstInfo/sucheForm.do
  ?sucheButtonInstitut=Suche&institutName=Deutsche+Bank
  &6578706f7274=1&d-4012550-e=1
```

That returns `Content-Type: text/csv`, semicolon-separated, 20 columns:

```
NAME;BAK NR;REG NR;BAFIN-ID;LEI;NATIONALE IDENTIFIKATIONSNUMMER…;PLZ;ORT;STRASSE;
LAND;GATTUNG;SCHLICHTUNGSSTELLE;HANDELSNAMEN;ZWEIGNIEDERLASSUNG IN DEUTSCHLAND;…;
ERLAUBNISSE/ZULASSUNG/TÄTIGKEITEN;ERTEILUNGSDATUM;ENDE AM;ENDEGRUND
```

It is genuinely the KWG data nothing else has — `GATTUNG` (`CRR-Kreditinstitut`,
`SSM-Institut`, `freigestelltes Kreditinstitut`, `grenzüberschreitender
Dienstleister … § 53b KWG`) and one row per permission with its grant date, end
date and end reason, cited to the KWG paragraph. The rows are ragged in the same
way `upreg` is: the first row of an institution carries the identity columns and
the following rows carry only the permission columns.

**And it is off limits to robots.** `https://portal.mvp.bafin.de/robots.txt`:

```
User-agent: *
Disallow: /
```

That is the whole file. Not a rate limit, not a partial exclusion — the operator
excluding every automated client from every path on the host. The table id
(`d-4012550`) is also opaque and would have to be read out of the HTML first,
which means parsing the page you were told not to fetch.

**So: use it by hand, not from a script.** Run the search in a browser, click
`CSV`. For anything automated, the EU-level registers carry the same firms and
welcome the traffic.

What the EU-level route gives you, and what it still misses:

| BaFin-supervised | Where | How |
|---|---|---|
| Investment firms, AIFMs, UCITS management companies | `upreg` | `--field 'ae_competentAuthority=Federal Financial Supervisory Authority (BaFin)'` — 2,815 entities |
| Payment institutions, e-money institutions, AISPs, exempted providers | `eba-psd` | `--field CA_OwnerID=DE_BAFIN` — 1,037 records (82 PI, 13 EMI, 942 excluded-from-scope) |
| Crypto-asset service providers | `mica-casp` | `--field ae_homeMemberState=DE` |
| **Credit institutions / banks under the KWG** | — | Not in any of them. BaFin's own export, or the EBA CIR / ECB list |
| **Insurers under the VAG** | — | Not in any of them. BaFin's own database, or EIOPA's registers |
| **Tied agents (*vertraglich gebundene Vermittler*)** | — | Not in any of them. BaFin's `VGVInfo` export only |

Note that the ZAG gap is now closed: `eba-psd` is BaFin's own ZAG data, filed by
BaFin with the EBA, published under an interface built for automation.

### FMA (Austria) — no interface at all

* <https://www.fma.gv.at/unternehmensdatenbank-suche/> returns **HTTP 403** to
  any non-browser client — an explicit signal that programmatic access is not
  wanted. No API, no documented export, and no FMA company-database dataset
  found on data.gv.at.
* Route instead: `upreg` with
  `ae_competentAuthority=Austrian Financial Market Authority (FMA)` (661
  entities), `mica-casp` with `ae_homeMemberState=AT`, and `eba-psd` with
  `CA_OwnerID=AT_FMA` (87 records — 8 payment institutions, 1 e-money
  institution, 78 excluded-from-scope providers).
* Remaining gap, same as Germany: Austrian banks and insurers are not in any of
  these. Unlike BaFin, the FMA offers no export to click either.
* The FMA *does* publish on data.gv.at — but regulations and forms, not the
  company database. What Austria publishes as data is the trade-licence register
  ([GISA](#gisa-austria--a-million-trade-licences-in-a-7-zip-archive)), which
  covers a million businesses and none of the FMA's supervised entities.

### Finding a national register's data: the two catalogues that answer

Both DACH open-data portals expose a documented search API, and both are worth
one request before assuming a register has no interface:

| Portal | API | Note |
|---|---|---|
| **data.gv.at** (Austria) | `GET https://www.data.gv.at/api/hub/search/search?q=…&filter=dataset&limit=…` | Piveau. Returns full DCAT records including each distribution's `access_url`, `format` and licence. **This is how the GISA endpoint was found** — the URL appears nowhere on gisa.gv.at itself. |
| **GovData** (Germany) | `GET https://ckan.govdata.de/api/3/action/package_search?q=…` | Standard CKAN v3. Searching `Erlaubnis OR Zulassung OR Konzession` on 2026-08-16 returned 59 datasets — almost entirely Länder-level PDFs, which is itself the finding: Germany's federal licence registers are not published as data. |

The Austrian query that mattered, reproduced in full:

```bash
curl -s 'https://www.data.gv.at/api/hub/search/search?q=gewerbeberechtigungen&filter=dataset&limit=20' \
  | python3 -c 'import json,sys; [print(d["title"].get("de"), [x["access_url"] for x in d["distributions"]]) for d in json.load(sys.stdin)["result"]["results"]]'
```

---

## The same trick in other industries

Finance is not special. Wherever the law makes a licence, registration,
certification or approval a condition of trading, the regulator ends up holding
the authoritative company list for that industry — and usually publishes it.
EUDAMED above is the medical-device instance of exactly the same pattern.

Below is what an afternoon of probing found on **2026-08-16**. The "Depth"
column is deliberate: *probed* means requests were made against the interface
and the shapes checked, *surface* means the landing page was fetched and the
obvious machine endpoints tried, nothing more. Do not read a surface row as a
verdict.

| Industry | Register | What it lists | Interface found | Depth |
|---|---|---|---|---|
| Medical devices | [EUDAMED](https://ec.europa.eu/tools/eudamed) | 48,893 manufacturers, importers, authorised reps, pack producers + 70 notified bodies | **JSON API, no auth** — [wrapped](#eudamed--the-medical-device-industry-with-contact-details) | probed |
| All CE-marked sectors | [NANDO / SMCS](https://webgate.ec.europa.eu/single-market-compliance-space/) | Notified bodies across *every* CE-marking directive — machinery, PPE, lifts, pressure equipment, construction products, not just devices | Not established. A modular Angular app: the routes are in `main-*.js` (`/notified-bodies/by-country`, `/by-legislation`, `/free-search`), but `modules` in its config object is empty in the main bundle, so the API base is loaded at runtime and the feature code is in chunks not referenced from `index.html`. Worth another hour. Meanwhile EUDAMED's `api/ses/` already returns the NANDO notification URL for each *device* notified body | surface |
| Pharma / CDMO | [EudraGMDP](https://eudragmdp.ema.europa.eu/) | Every GMP manufacturing and import authorisation in the EEA — i.e. the contract-manufacturing industry | **None found.** Redirects to `/inspections?key=public`, an Apache Struts app (`.do` actions, `jsessionid` in the URL, prototype.js/scriptaculous). Same shape as BaFin's portal | surface |
| Pharma / CRO | [CTIS](https://euclinicaltrials.eu/ctis-public/search) | 12,229 authorised clinical trials and their sponsors | **JSON API, no auth** — [wrapped](#ctis--who-runs-clinical-trials-in-europe). There is also an **asynchronous** CSV export (`POST /search/download` → `{"taskId": …}` to poll), not used here | probed |
| Chemicals | [ECHA CHEM](https://chem.echa.europa.eu/) | REACH registrants — every company that registered a substance | **JSON API, no auth**, but substance-first: `GET /api-substance/v1/substance?searchText=…`. `searchText` is mandatory (blank → HTTP 400 `[searchText must not be blank]`), and registrants hang off the substance detail, so a company directory means walking substances. No OpenAPI document at the usual paths | probed |
| Energy | [MaStR](https://www.marktstammdatenregister.de/MaStR/Datendownload) (BNetzA) | Every German electricity/gas market participant and generation unit | **Open bulk data.** A daily full export at a predictable URL (`Gesamtdatenexport_YYYYMMDD_<v>.zip`), XML, explicitly licensed **Datenlizenz Deutschland – Namensnennung 2.0**. It is ~2.9 GB | probed |
| Aviation | [EASA](https://www.easa.europa.eu/en/domains/aircraft-products/continuing-airworthiness-organisations/foreign-part-145-organisations) | Part-145 / Part-147 / Part-CAMO / Part-CAO approvals — MRO and airworthiness organisations | Dataset pages with a UI export button; the lists render client-side and no stable machine URL was found from the markup. XLSX snapshots exist under `/sites/default/files/datasets/`. **Note the scope trap:** EASA directly approves *third-country* organisations; EU-based ones are approved by national aviation authorities and are not on these lists | surface |
| Finance (UK) | [FCA Financial Services Register](https://register.fca.org.uk/) | Every FCA-authorised firm and individual | **Official documented API, key required.** `GET /services/V0.1/Firm/{FRN}` returns HTTP 403 `{"Success":"false", "Sorry, this page is not available. Missing Headers."}` without the `X-Auth-Email` / `X-Auth-Key` headers. Registration is free; this project ships no key | probed |
| Finance (CH) | [FINMA](https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/) | 2,938 Swiss authorisations across 37 licence types, with UIDs | **Daily CSV at a stable URL** — [wrapped](#finma-switzerland--the-authorisation-list-as-a-daily-csv). *Corrected on 2026-08-16: an earlier version of this table said no interface was established for FINMA. That was wrong — the register's own page links a CSV and an XLSX per category.* | probed |
| All sectors (CH) | [UID register](https://www.uid.admin.ch/) (BFS) | Every Swiss legal entity: legal form, seat, commercial-register and VAT status | **Public SOAP service, no auth** — [wrapped](#the-swiss-uid-register--a-public-soap-service-with-a-silent-ceiling), 30-record ceiling documented | probed |
| All sectors (AT) | [GISA](https://www.data.gv.at/katalog/dataset/e49a1510-9d93-4277-8467-48a1efc9f046) | 1,030,111 active Austrian trade licences, CC BY 4.0 | **Open-data REST endpoint** — [wrapped](#gisa-austria--a-million-trade-licences-in-a-7-zip-archive); the search UI is a ViewState-bound WebForms app and is not touched | probed |
| Commercial register (CH) | [Zefix](https://www.zefix.admin.ch/) | Swiss commercial register | Documented REST API, **HTTP 401** without credentials, host-wide `Disallow: /`. [Not wrapped](#zefix-and-basg--found-documented-deliberately-not-wrapped) | probed |
| Pharma (AT) | [BASG / MiA](https://medikamente.basg.gv.at/) | Austrian marketing authorisations and their holders | Clean REST API, **`401 Unauthorized - Invalid API token`**; the token is hardcoded in the public JS bundle. [Deliberately not lifted](#zefix-and-basg--found-documented-deliberately-not-wrapped) | probed |
| All sectors (global) | [GLEIF](https://www.gleif.org/en/lei-data/gleif-api) | 3,403,760 legal entities: LEI, legal name, address, legal form, national register number | **JSON:API, no key, CC0** — [wrapped](#gleif--the-identity-key-and-the-one-register-that-publishes-deltas). Daily bulk **and delta** files | probed |
| All sectors (EU) | [VIES](https://ec.europa.eu/taxation_customs/vies/) | VAT registration validity, with name and address where the member state discloses them | **REST, no key** — [wrapped](#vies--the-number-nobody-lies-to-the-tax-authority-about). A validator, not a directory | probed |
| All sectors (EU/EEA) | [TED](https://ted.europa.eu/) | Public procurement notices, with the winning company named | **JSON API, no key** — [wrapped](#ted--public-money-and-the-earliest-signal-here). Alpha-3 country codes, 250-row limit, no Switzerland | probed |
| Finance (LU/NL) | CSSF, DNB | Luxembourg funds and managers; Dutch supervised institutions | Not established. The commonly cited CSSF entity-search URL 404s; DNB's register pages serve HTML | surface |
| Automotive | [KBA](https://www.kba.de/) | Type-approval holders | Not established; the type-approval pages serve HTML | surface |

One of these is worth implementing next, and one deliberately is not:

* **ECHA** is valuable but shaped wrong for this tool: it answers "who
  registered this substance", not "list the registrants". Turning that into a
  company directory means iterating substances, which is a lot of requests
  against a public service and needs a design decision about politeness before
  any code.
* **MaStR** is genuine open government data under an open licence, and this tool
  still should not wrap it as-is: `eufinreg` buffers a whole result set in memory
  before flattening, and a 2.9 GB XML export needs streaming. Use it directly, or
  a purpose-built tool. The URL and licence are recorded here so you do not have
  to go looking. (GISA is the same shape one order of magnitude smaller — 211 MB
  — which is why it *is* wrapped, and why it filters during the parse rather
  than after it.)

The three lessons from adding the DACH registers, since they generalise:

1. **Check the open-data catalogue before concluding there is no interface.**
   GISA's endpoint is not linked from gisa.gv.at anywhere; it exists only in the
   data.gv.at record. A register whose own site offers nothing but a form may
   still be publishing itself somewhere else.
2. **"No API" and "no interface" are different claims.** FINMA has no API. It
   has a CSV regenerated every morning at a URL that does not move, which is a
   better interface than most APIs.
3. **A 401 is an answer.** Zefix and BASG both have working, well-shaped
   endpoints that return 401. Both are documented here and neither is used —
   including the one whose token is sitting in a public JS bundle.
4. **Rank a source by what a lie would cost the publisher**, not by how official
   it looks. Falsifying a prospectus is a crime; falsifying a licence
   application costs the licence; falsifying a commercial-register filing is an
   administrative offence; falsifying a company website costs nothing at all.
   Every source in this project sits in the top three bands, and that — not the
   `.gov` in the domain — is why the output is worth something.

### Where the next source comes from: nine kinds of obligation

The survey above is not a list of favourites; it is one pass over a structure.
Any economic activity leaves a trail of *compulsory* filings before it leaves a
voluntary one, and they sort into nine kinds. Four questions find them:

1. **What had to happen first?** Idea → funding → incorporation → licence →
   site → permits → certification → hiring → the job advert you were reading.
   The advert is second from the end. Everything upstream of it is earlier *and*
   on the record.
2. **Who could have said no?** A body with the power to refuse must keep a list
   of who it approved, or it cannot enforce. **Approval power implies a
   register.**
3. **Whose money is it?** Public money carries a publication duty. That makes it
   the earliest reliable signal of expansion.
4. **Who has an opponent?** Adversarial procedures — litigation, merger review,
   patent opposition, insolvency — put facts on the record that no vendor would
   sell you, because the other side wanted them there.

| | Kind of obligation | In this project |
|---|---|---|
| **A** | Existence and ownership — commercial registers, LEI, beneficial ownership | `gleif` (and via it, the German *Handelsregister* number) |
| **B** | Operating licences — the industry directories | `upreg` `eba-psd` `mica-*` `finma` `gisa` `eudamed` `ctis` |
| **C** | Technology and IP — patents, trademarks, standards bodies | not implemented; EPO's OPS needs a free key, EUIPO likewise |
| **D** | Public money — procurement, grants, subsidies, state aid | `ted`; CORDIS and the German *Förderkatalog* are not implemented |
| **E** | Physical facilities — emission permits, planning, environmental consent | not implemented; German BImSchG notices are Länder-level, E-PRTR is EU-wide open data |
| **F** | Money and tax — filings, prospectuses, VAT | `vies`; the Bundesanzeiger's annual accounts are not machine-readable |
| **G** | Adversarial procedures — courts, merger review, insolvency, recalls | not implemented; EU merger decisions are the richest free market analysis there is |
| **H** | People — visa sponsor lists, professional registers, association members | not implemented; the Dutch IND recognised-sponsor list is the cleanest example |
| **I** | Cross-border — FDI screening, export control, trade data | not implemented |

Two honest caveats about the whole method:

* **Mandatory ≠ complete.** These sources are blind to the informal economy, to
  what happens inside a company, to intent, and — importantly — to *approved but
  abandoned*. An approval tells you somebody meant to do something. It does not
  tell you they did it. Cross-check against a second layer (accounts, awards,
  imports) before believing a plan happened.
* **A list is not an outcome.** This method has visible progress, verifiable
  results and no social cost, which makes it the most enjoyable form of
  procrastination available. The list is only worth the use you make of it.

### How to check one of these yourself

The method that produced the table, in order — it is the same every time:

1. **Open the register in a browser and watch the network tab.** Public-sector
   search UIs are overwhelmingly SPAs calling a JSON backend. That backend is
   the interface, whether or not anyone documented it.
2. **If there is no network tab handy, read the JS bundle.** Angular and React
   builds keep their endpoint list in a config object —
   `grep -oE '"api/[a-zA-Z0-9_/{}-]+"' main.*.js` found every EUDAMED endpoint in
   one command, and the CTIS bundle names its API hosts in plain text.
3. **Look for a bulk download before you look for a query API.** MiCA, the EBA
   PSD2 register and MaStR are all "just a file at a stable URL", which is a
   better interface than a paginated search for most purposes.
4. **Check `robots.txt` before writing any client.** It is what separates
   EUDAMED (allowed) from BaFin (`Disallow: /`) — and it is the difference
   between a tool and a nuisance.
5. **Verify the totals add up.** EUDAMED's four actor types sum to exactly its
   reported total, which is how you know nothing is being hidden by a default
   filter. When they do not add up, you have found the filter you did not know
   about — like `languageIso2Code`.

---

## Limitations and disclaimers

Read this before putting the output in front of anyone.

**A licence is not a business signal.**

* **Licensed ≠ hiring.** Nothing in these registers says anything about
  headcount, revenue, activity level, or whether anyone works there.
* **Licensed ≠ actually operating.** An authorisation can sit unused for years.
  `ae_status=Inactive` covers 2,226 of 13,930 `upreg` entities, but "Active"
  only means the licence is current, not that the business is.
* **The licensed entity is often not the operating entity.** Groups license one
  subsidiary and trade through others. `ae_officeType=Branch` (1,281 records)
  marks establishments of a firm licensed elsewhere.
* **Passporting hides the map.** A firm licensed in one member state may serve
  all of them without appearing under those states. In MiCA data the honest
  footprint is `ac_serviceCode_cou`; in the EBA PSD2 data it is `ENT_SER_COU`,
  not `ae_homeMemberState` or `ENT_COU_RES`.
* **Registered address ≠ office address.** `ae_headOfficeAddress`, MiCA's
  `ae_address`, PSD2's `ENT_ADD` and EUDAMED's `geographicalAddress` are the
  addresses notified to a regulator. Expect law firms, company formation agents
  and holding-company letterboxes.
* **A registration is not an establishment.** EUDAMED's economic operators
  include non-EU manufacturers who register in order to sell into the EU — the
  first page sorted by `ulid` includes companies in Taiwan, Australia and the
  United States. `countryIso2Code` is where the operator is, not where the work
  is. That is also why every non-EU manufacturer needs an
  `authorised-representative`, and why that 2,938-row table is the one that maps
  onto actual EU presence.
* **`mica-ncasp` is the opposite of a licence list.** It names entities ESMA or
  an NCA considers non-compliant. Do not merge it into a "licensed firms" table.
* **Nor are `PSD_EXC` and `PSD_ENL` licences.** They record providers *outside*
  PSD2's scope and providers entitled under national law. They are 1,995 of the
  6,407 institution records — and 942 of BaFin's 1,037.
* **CTIS is a trial list, not a company list.** One sponsor appears once per
  trial, so counting rows counts trials; deduplicate on `sponsor` before you
  count organisations. And roughly a third of sponsors are hospitals and
  universities, not industry — `sponsorType` is the field that separates them.
  `Pharmaceutical company, Pharmaceutical company` is a co-sponsored trial, not
  a distinct type.
* **FINMA's file is one row per authorisation.** 2,938 rows, 2,744
  institutions. Counting rows overstates the Swiss financial sector; this tool
  collapses them, but if you read the CSV yourself, group on `UID` first — and
  remember 84 rows have no UID at all.
* **`gisa` is a licence register with no licence holders.** Austria publishes it
  without personal data by design. It will tell you that there are 22,130
  concession-bound trades and where they are; it will not tell you whose they
  are, and no join can recover that from this file.
* **`ch-uid` is a lookup, not a list.** Its search stops at 30 records with no
  total and no error. Any "all companies in canton X" you build out of it is
  wrong, and this tool warns you every time the ceiling is hit.
* **A snapshot is evidence of what a register said, not of what is true.**
  `--diff` reports that a row moved. Whether it moved because a licence was
  withdrawn or because a national authority fixed a typo, the register does not
  say — and neither does this.

**Personal data.**

* `eba-psd --select ALL` returns 322,314 agent records, **largely natural
  persons** named in full with an address, processed under
  [Regulation (EU) 2018/1725](https://eur-lex.europa.eu/eli/reg/2018/1725/oj).
  They are excluded from the default selection deliberately. If you pull them,
  you have taken on a controller's obligations; storing or republishing them is
  your decision to justify, not the register's.
* 195,920 of those agents are flagged `DER_CHI_ENT_AUT=Inactive`. A withdrawn
  agent staying in the file is not a current business relationship.
* **`eudamed-eo` carries an email address and a phone number for nearly every
  one of its 48,893 organisations.** Most are company mailboxes; some are sole
  traders, where the "company" contact *is* a person. Those details are
  published so that patients, regulators and buyers can identify who is
  responsible for a device. That is the purpose they were collected for, and it
  is not the same purpose as a marketing list. Bulk unsolicited mail to a
  regulatory contact address is, at best, a fast way to get a register's API
  closed to everyone — and in most member states it is also unlawful.
* A licence register is a *lawful basis* question, not just an availability
  question. "It was published" is not by itself a legal basis for processing it
  under GDPR Article 6. Decide yours before you fetch, not after.

**Data quality.**

* Every register here is compiled from submissions by national competent
  authorities (and, for MiCA, the EBA). Spelling, completeness and timeliness
  vary by authority. `ae_lei` is frequently empty. `ac_serviceCode` is free text
  in an enum-shaped field. In `eba-psd`, one Polish authority accounts for 2,497
  of the 6,407 institutions and Spain for 107,944 of the agents — the per-country
  shape of the data reflects filing practice as much as market structure.
* Fields disappear and appear. That is why `--inspect` exists and why nothing in
  this tool hardcodes a field list. `eba-psd` goes further and takes its code
  labels from the register's own metadata endpoint at run time.

**Update frequency.**

* MiCA CSVs: ESMA states **weekly**. Check `Last-Modified` for the real vintage.
* `upreg`: ESMA publishes no update cadence for the A2A endpoint. Use the
  per-record `ae_lastUpdate`, not the `timestamp` field — `timestamp` is the
  Solr indexing time (uniformly `2026-04-09` on sampled documents, i.e. the last
  full reindex).
* `eba-psd`: regenerated **nightly**; national authorities are required to
  update at least daily. The `timestamp` in the file metadata is the generation
  time, and each record carries its own `__EBA_EntityVersion`.
* `eudamed-eo` / `eudamed-nb`: live, no published cadence. Each record carries
  `versionNumber` and `latestVersion`; economic operators also carry
  `dateOfRegistration`.
* `ctis`: live. Per-record `lastUpdated` and `lastPublicationUpdate` are the
  dates to trust.
* `finma`: the file observed on 2026-08-16 carried
  `Last-Modified: Sun, 16 Aug 2026 03:05:12 GMT` — regenerated that morning.
  Treat it as **daily**, and check the header rather than trusting that.
* `ch-uid`: live. Records carry `address.dateOfLastCheck`, which is when the
  FSO last verified the address, not when the entity last changed.
* `gisa`: **monthly**. The vintage is only ever stated in the
  `Content-Disposition` filename (`…_2026.08.csv.7z`), which is why this tool
  lifts it into a `_vintage` column instead of letting it evaporate.
* BaFin states its own database is updated daily. That is the one advantage of
  clicking its export by hand over reading the EBA's copy of the same filings.

**None of them publishes a changelog.** No `?since=`, no feed, no webhook, and
in most cases yesterday's file is simply gone — the EBA overwrites nightly,
FINMA every morning, ESMA weekly. If you need to know *what changed*, the only
way to have that answer tomorrow is to have taken a snapshot today. That is what
`--snapshot` is for, and [docs/BUSINESS.md](docs/BUSINESS.md) argues it is the
most valuable thing in this repository.

**The regulators' own disclaimers.**

* BaFin: "a liability of BaFin for completeness and correctness of information
  is excluded."
* ESMA, on the MiCA register: "The crypto-asset white papers listed in ESMA's
  register have not been reviewed or approved by any competent authority in any
  Member State of the European Union. The offeror and/or issuer of the
  crypto-asset is solely responsible for the content of each crypto-asset white
  paper."
* EBA, on the PSD2 register: "unlike national registers under PSD2, this
  Register has no legal significance and confers no rights in law… responsibility
  for the accuracy of that information lies with the competent authorities at
  national level."
* This project adds no verification of its own. It is a transport. If a
  regulatory decision depends on the answer, confirm it in the authority's own
  interface.

---

## Polite use

These are small public-sector deployments funded by nobody's ad revenue.

* Default pause between requests is **1.0 second**. Raise it, don't lower it.
* Retries use exponential backoff (1 s, 2 s, 4 s … capped at 60 s) with jitter,
  and always obey `Retry-After` when the server sends one.
* Requests are sequential. There is no concurrency anywhere in this project and
  adding some would be a mistake.
* Prefer `--select` / `--query` (server-side, one request) over pulling the
  whole core and filtering locally. On `eba-psd` there is nothing to push down —
  the interface is a single file — so `--select` saves you memory, not their
  bandwidth.
* Cache. If you need the data twice today, write it to a file the first time.
  This matters most for `eba-psd` (19 MB per run, changes once a night) and
  `eudamed-eo` (164 requests for a full pull).
* Respect `robots.txt`. It is why this project reads BaFin's data from the EBA
  rather than from BaFin, and why it reads EUDAMED directly.
* An undocumented application backend — EUDAMED's, and most of the ones in the
  sector survey — is a courtesy, not a contract. It is there to serve a web UI
  used by a few people at a time. `--select` and `--query` push filters at the
  server precisely so you can ask for the 2,938 rows you want instead of the
  48,893 you do not.

**Change the User-Agent.** The default is
`eufinreg/0.5.0 (+https://github.com/7feilee/eufinreg; public-register client)`.
Set it to something that identifies *you*, so an operator who sees unusual
traffic can find out who to contact instead of blocking a range:

```bash
uv run eufinreg --source upreg \
  --user-agent "acme-research/1.0 (+https://github.com/acme/registry-tools; ops@acme.example)"
```

The tool prints a note to stderr for as long as you leave the default in place.

---

## Troubleshooting

**Requests to `www.esma.europa.eu` hang, but `curl` is instant.** Its CDN
publishes AAAA records. `curl` implements Happy Eyeballs and falls back to IPv4
in milliseconds; Python does not — `urllib3` walks the address list in order and
waits out the full connect timeout on each dead IPv6 address. If your network
has no working IPv6 route, pass `-4`:

```bash
uv run eufinreg -4 --source mica-casp -o casps.csv
```

`registers.esma.europa.eu` is IPv4-only and is unaffected.

**Zero rows and no error.** That is almost always a wrong field name or a wrong
value, not a broken endpoint. `--field` warns when the name is unknown; for the
value, run `--list-values FIELD`. Note that Solr `q`/`--query` has no default
search field, so `--query Bybit` legitimately matches nothing — write
`--query 'ae_entityName:bybit*'`.

**Memory.** The whole result set is buffered before flattening, so that a
filter can match on a child record and still return the entity row. The whole
`upreg` core is ~100k documents — narrow it with `--select` when you can, or cap
it with `--max-docs`. `eba-psd` is the heavy one: the download expands to a
217 MB JSON document and parsing peaks around **1.4 GB of RSS**, whatever
`--select` you pass, because the file is served whole.

**`eba-psd` says the download failed its SHA-256 check.** The register publishes
the digest and this tool enforces it, so a mismatch means the bytes you got are
not the bytes the EBA meant to send — almost always a truncated transfer or an
intercepting proxy. Retrying is the right move. If it persists, run with
`--raw ./raw` and compare against the `.sha256` the register serves alongside
the archive.

**`eba-psd` returns fewer rows than the register's web UI reports.** The default
selection is institutions only. Add `--select ALL` for the agents and branches,
which are ~98% of the records.

**A hand-rolled EUDAMED client returns 300 rows, or 27 copies of everything.**
Both are the same class of bug and neither reports an error:

* `size` is silently capped at **300**, so asking for 1000 and stopping when you
  get fewer than you asked for reads exactly one page.
* `languageIso2Code` is mandatory in practice. `api/eos` returns HTTP 500
  without it; `api/ses/` returns one row per language, turning 70 notified
  bodies into 1,890 near-identical rows sharing 70 `uuid`s.

`eufinreg` handles both. If you are writing your own client, those are the two
things to get right — and `--raw` will show you what the wire actually said.

**`ctis` says it returned fewer trials than it matched.** That warning is real
and the data really is incomplete: the search cannot reach past record 10,000
however you page. Split the query into narrower `--query` searches, each
matching under 10,000, and concatenate the results.

**A hand-rolled CTIS client returns zero rows.** `searchCriteria` is mandatory.
Omit it and the API answers HTTP 200 with `totalRecords: 0`, which reads exactly
like "nothing matched" rather than "you left out a required key". Send
`"searchCriteria": {}`.

---

## Frontend, backend, and what changes when a browser is involved

`--serve` runs a read-only JSON API and a single-file web UI over exactly the
same sources the CLI reads. The layering — and the reason the browser must never
sit directly in front of a regulator's server — is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. The short version:

| Layer | Knows about |
|---|---|
| `eufinreg.sources` | the registers. Endpoints, paging, traps, flattening. |
| `eufinreg.service` | being a *long-running* client: cache with a per-source TTL, **one outbound request at a time**, bounded result sets. |
| `eufinreg.server` | HTTP. Routes, content types, status codes. ~150 lines, standard library only. |
| `eufinreg/web/index.html` | the browser. Talks only to the JSON API, so it is replaceable. |

No register name, URL or field name appears above the first layer — which is why
adding FINMA, the Swiss UID register and GISA made them appear in the API and
the UI with no changes to either.

Above all of it sits the scheduled ingest, which is the only thing that talks to
a register on a timetable. **With a store configured, the API answers from the
archive and contacts no register at all** (`?live=1` opts back in per request).
That inversion — fetching is scheduled, serving is local — is the whole reason
a browser can be put in front of this without it becoming an unattended crawler.

Three things the UI shows that a prettier one would have dropped: the
**truncation warnings**, the **provenance** (fetched at / cached until, or which
snapshot) on every answer, and the **cost before the click** ("this register
downloads a 19 MB archive"). A register browser that hides truncation is worse
than no register browser.

Two rules are enforced in code rather than documented as advice: the server
refuses to bind to a non-loopback interface without `--token`, and any selection
a source flags as personal data returns `403` unless it was started with
`--allow-personal-data`. See [docs/OPERATIONS.md](docs/OPERATIONS.md#10-personal-data).

## The niche

**DACH licence monitoring for compliance teams**: watch a list of entities across
the German, Austrian and Swiss registers plus the EU-level ones that actually
carry DACH firms; get told when a licence changes; get a receipt you can put in a
file. The three verbs above are that product, and
**[docs/BUSINESS.md](docs/BUSINESS.md)** is the reasoning — why this seam and not
a bigger one, who buys it, what is scarce (a time series, a change feed and an
audit trail — not the data), how it is priced, and what would kill it.

The short version of *why DACH*: Switzerland is outside the EEA, so a vendor
whose coverage stops at the EU border cannot answer a Swiss question at all —
and the three countries fail in different directions, with Austria and
Switzerland publishing files while Germany publishes a portal. Knowing that the
German answer has to be routed through ESMA and the EBA is a week of work to
discover and thirty seconds to use.

## Development

```bash
uv sync --all-extras        # install from the committed lockfile
uv run pytest               # tests are fully offline
uv run ruff check .
uv run ruff format --check .
uv lock --check             # is the lockfile current?
```

Every test mocks HTTP with [`responses`](https://github.com/getsentry/responses).
Nothing in the suite touches the network, so it cannot be rate-limited, cannot
fail because a register changed its schema overnight, and runs in CI on
Python 3.10–3.13.

That is also its limit: an offline suite pins *this project's* behaviour and can
never notice that a register renamed a field. That job belongs to
`eufinreg doctor`, which does reach the live services and is scheduled weekly in
[`.github/workflows/drift.yml`](.github/workflows/drift.yml). It checks each
source still returns rows and still carries the fields declared in
`Source.expected_fields`, and exits non-zero when one is missing — at which point
the fix is to re-capture a fixture with `--raw`, update the source, and update
the test.

The fixtures in `tests/data/` are trimmed captures of real responses. When a
register changes shape, re-capture with `--raw` and update the fixtures — the
tests are there to pin *this* project's behaviour, not to monitor ESMA.

`tests/data/eba_psd_goldencopy.zip` is a real golden copy cut down to eight
records chosen to cover the awkward cases (multi-valued names, a string-valued
`Services` entry, an agent, a branch, an entity with no services). Its SHA-256
is baked into `eba_filemetadata.json`, so if you regenerate the archive you must
regenerate that digest too — which is the point: the checksum tests would
otherwise pass vacuously.

The DACH fixtures are the same idea: `gisa_stat03.csv.7z` is a real 7-Zip
archive (so the magic-byte detection and the decompression are actually
exercised, not stubbed), `ch_uid_getbyuid.xml` is the real two-seat UBS reply
that proves `GetByUID` returns an array, and `finma_uid.csv` keeps the
semicolon delimiter, the BOM, an institution with two authorisations and two
rows with no UID.

Optional extras: `--extra xlsx` for `--format xlsx`, `--extra at` for
`--source gisa` (7-Zip). CI installs `--all-extras`, so both paths are tested;
without `py7zr` the GISA source raises a message naming the extra instead of a
`ModuleNotFoundError`.

---

## 中文速览

**核心前提。** 凡是需要许可、注册、认证、补贴或审批才能经营的行业，监管机构
手里都有一份比任何商业数据库都完整的企业名录——因为"在册"是合法经营的前提，
不是企业的营销选择。这些名录公开、免费、结构化，而且比多数人以为的更常带有
机器接口。本项目就是读这些名录的。（项目名 `eufinreg` 是金融时代留下的，
现已扩展到医疗器械，改名待办。）

**结论先行。** 已实现的登记库都有官方机器接口（欧盟层面 5 类 + DACH 三国 3 个
国家级登记册，共 18 个 source）：**ESMA** 的 Solr A2A
（无需认证、无需 API key）、**MiCA** 每周重新生成的 5 个固定 URL 的 CSV、
**EBA 的 PSD2 支付/电子货币机构登记册**——这个最规范，因为欧盟实施条例
(EU) 2019/410 直接**以法律形式要求**它可机读：每晚重新生成一份 JSON 全量快照，
并公布 SHA-256 校验值（本工具会强制校验，不匹配直接报错）——以及新增的
**EUDAMED**（欧盟医疗器械数据库）。

**EUDAMED 是"同一个套路换个行业"的最好例子。** 按 MDR 2017/745 / IVDR
2017/746，产品要进欧盟市场就必须在 EUDAMED 注册为"经济经营者"，所以它等于
一份近乎完整的欧洲医疗器械行业企业名录：2026-08-16 共 **48,893 家**
（制造商 31,931、进口商 12,363、欧盟授权代表 2,938、系统/程序包生产商 1,661，
四者相加正好等于总数），而且**几乎每家都带邮箱和电话**；另有 70 家公告机构
（notified body），附 NANDO 链接。接口就是官网自己用的 JSON API，无需认证，
`ec.europa.eu/robots.txt` 的 201 条 `Disallow` 规则没有一条命中 `/tools/eudamed/`。

**关于 BaFin，先前版本的说法是错的，这一版已更正。** BaFin 的
Unternehmensdatenbank / ZAG 登记册 / 绑定代理人登记册确实**有**导出功能：
搜索结果页底部写着 `Exportoptionen: CSV | XML | Excel`，链接就是搜索 URL 加上
displaytag 的导出参数（`6578706f7274=1` 是 `export` 的十六进制，`d-<id>-e=1`
是 CSV）。导出的 CSV 有 20 列，含 `GATTUNG`（机构类别）和逐条许可的授予日期、
终止日期与终止原因——这是别处拿不到的 KWG 数据。

**但是** `portal.mvp.bafin.de/robots.txt` 的全部内容是
`User-agent: * / Disallow: /`：运营方明确拒绝一切自动化访问整个站点。
所以本项目**不碰这个门户**，只在文档里写清楚人工点击的确切 URL；需要自动化时
走欧盟层面的数据：BaFin 监管的投资公司等 2,815 家在 `upreg`，
BaFin 监管的支付与电子货币机构 1,037 家（其中真正持牌的是 82 家支付机构、
13 家电子货币机构）在 `eba-psd`。德国的银行（KWG）、保险（VAG）和绑定代理人
仍然只有 BaFin 自己有。FMA（奥地利）连导出都没有，对非浏览器客户端直接返回 403。

**新增 DACH（德奥瑞）三国的国家级登记册，结论与直觉相反：奥地利和瑞士发布
文件，德国只发布门户。**

* **FINMA（瑞士金融市场监管局）**——瑞士不在欧洲经济区，前面所有欧盟登记册里
  都没有瑞士牌照。FINMA 把全部持牌机构做成一个**固定 URL 的 CSV，每天早上重新
  生成**（2026-08-16 观测到 `Last-Modified` 为当天 03:05 UTC），`robots.txt`
  允许抓取。页面上的 `?hash=…` 是 Sitecore 媒体哈希，**不是必需的**，去掉照样
  返回同一份文件。共 2,938 条授权、37 种牌照类型，但**一行是一项授权而不是一家
  机构**（苏黎世州立银行出现两次：Bank + Depotbank），去重后是 2,744 家。本工具
  按 UID 归组折叠。文件带 **UID（瑞士企业识别号）**，可直接与下面的登记册联接。
* **瑞士 UID 登记册（联邦统计局）**——**公开 SOAP 服务，完全无需认证**
  （`uid-wse.admin.ch/V5.0/PublicServices.svc`，WSDL 公开，SOAP 1.1）。能查到
  法律形式、注册地址、州、商业登记状态、增值税状态，甚至**增值税集团成员关系和
  总部/分支关系**。两个静默陷阱：一是 `Search` **最多只返回 30 条**，
  `maxNumberOfRecords` 写 1000 也是 30，且不报错、不给总数、无游标——
  "30 条结果"和"正好 30 条匹配"无法区分，本工具每次触顶都会告警；二是
  `GetByUID` 返回的是**数组**，UBS AG（CHE-101.329.561）会返回苏黎世和巴塞尔
  两个注册席位，只读 `[0]` 会悄悄丢掉一个。
* **GISA（奥地利营业执照信息系统）**——网页查询是绑定 `__VIEWSTATE` 的
  ASP.NET WebForms，无法自动化；但主管部门**以 CC BY 4.0 开放数据的形式发布了
  整个登记册**：`GisaPublicV2.svc/ogd/stat03/csv`，2026-08-16 共
  **1,030,111 条在册营业执照**。四个坑：(1) 路径写着 `/csv`，返回的其实是
  **7-Zip 压缩包**，`Content-Type` 还写成了重复前缀的
  `application/application/x-7z-compressed`（本工具靠文件魔数判断，不信路径也
  不信头）；(2) 文件里的列名是**小写**，而 data.gv.at 的字段文档写的是大写，
  按文档拼写过滤会得到 0 行且不报错；(3) `gewerbeart` 在执照表里是数字代码、在
  代码表里是德文标签，按列名联接必然为空；(4) 目录里的 `byte_size` 少报了
  一个数量级（实际解压后 211 MB）。**该数据集不含持有人姓名**——发布方明确说明
  "不提供个人数据"，因为 103 万条里有 70 万条的持有人是自然人。
* **两个"找到了但故意不用"的接口：** 瑞士商业登记 **Zefix** 有文档齐全的 REST
  API，但无凭据返回 401，且整站 `robots.txt` 是 `Disallow: /`；奥地利药品登记
  **BASG/MiA** 的 REST API 很规整，但需要 API token——而这个 token 就硬编码在
  公开的 JS bundle 里。正因为"拿出来"太容易，本项目才不拿：把 token 发给每一个
  浏览器，仍然是运营方在说"先来问我一声"。
* **找国家级登记册的正确姿势：先查开放数据门户的 API。** GISA 的那个 endpoint
  在 gisa.gv.at 上一个链接都没有，只存在于 data.gv.at 的元数据里
  （`/api/hub/search/search?q=…`，Piveau）。德国 GovData 是标准 CKAN
  （`ckan.govdata.de/api/3/action/package_search`），搜"许可/批准/特许"返回的
  59 个数据集几乎全是各州的 PDF——这本身就是结论：德国联邦级的牌照登记册没有以
  数据形式发布。

**新增三个"上游"数据源（2026-08-16 实测，全部无需认证）：**

* **`gleif`（全球法人识别编码 LEI）**——3,403,760 家法人实体，含法定名称、
  地址、法律形式、状态，以及**该实体在本国商业登记簿里的编号**：德国记录里
  `entity.registeredAs` 就是 `HRB 204159`，`entity.registeredAt.id` 指明是哪个
  地方法院。BaFin 门户禁爬、handelsregister.de 只有表单，这是目前拿到德国
  商业登记号的可行路径，而且**数据是 CC0**（本项目里唯一可无条件再分发的源）。
  更重要的是：**GLEIF 是这个项目里唯一自己发布"变化"的登记库**——每日全量
  之外还有 IntraDay/LastDay/LastWeek/LastMonth 增量文件（8-16 当天的日增量
  是 3.75 MB、29,341 条）。如果各监管机构都这么做，本项目一半的快照机制都
  不必存在。两个限制它都**主动说明**：`page[size]` 超过 200 报 400 并写明
  上限，结果超过 10,000 条也报 400 并写明——对比 EUDAMED 静默截断到 300 且
  返回 200。注意 DACH 每个国家都超过 1 万条（德 254,108、瑞 27,997、
  列 15,423），所以整国拉取必须走批量文件或缩小过滤条件。
* **`vies`（欧盟增值税号验证）**——税务机关是"没人敢骗第二次"的那一方。
  按 (EU) 904/2010，跨境供货方本来就应当验证对方税号。**它是验证器不是名录**，
  因此本项目声明它不可被排程归档（对验证服务做定时爬取属于滥用）。
  最关键的坑：`isValid: false` 有两种完全不同的含义——`userError: INVALID`
  是"号码不对"，`userError: MS_UNAVAILABLE` 是"该成员国系统当前不可用"。
  只读 `isValid` 的客户端会把一家正常公司记成"未注册"，本工具对后者一律告警。
* **`ted`（欧盟公共采购公告）**——**本仓库里最靠前的领先指标**：中标公告写明
  中标企业名称，而它发生在随之而来的招聘之前好几个月。四个坑：只支持 `POST`
  （`GET` 返回 405，而隔壁 `/fields` 反而要求鉴权）；`limit` 上限 250 且错误
  信息写明；国家代码是 **ISO alpha-3**（DEU/AUT），用两位码会得到 0 条且不报错；
  文本字段是多语言对象 `{"deu": [...]}`，没有单一的 name 字段。另外
  `winner-name` **按标段重复**，六个标段三家公司会出现六个名字（已去重并保留
  `winner_count`）。**TED 不覆盖瑞士**——瑞士不在欧洲经济区，采购公告发在
  simap.ch，所以这个源对 DACH 天然只有三分之二，正好和 FINMA 补的那块互补。

**判断一个数据源值不值得信，看的是"造假的代价"，不是域名后缀。** 伪造招股书
是刑事责任；伪造牌照申请会被吊销资质；伪造商业登记是行政处罚；公司官网和
社交媒体上写什么则毫无后果。本项目所有源都落在前三档——这才是产出可用的原因。
同一个道理反过来说：**不要问"哪里有信息"，要问"谁被法律强制说真话"。**

**这套方法的两个诚实边界：**强制披露源对非正式经济、企业内部决策、真实意图，
以及"已批准但决定不做"一律盲目——审批只说明意图，不说明执行，需要用第二层
数据（年报、中标、设备进口）交叉验证；另外，名单本身不是成果，它有明确进度、
可验证、还令人愉悦，因此是最优质的拖延形式。

**登记册只发布"现在"，不发布"变化"。** 这些机构没有一个提供变更日志、
`?since=` 参数或推送：EBA 每晚覆盖、FINMA 每天早上覆盖、ESMA 每周覆盖、
GISA 每月换一份文件，昨天的版本直接消失。所以本版新增两个模式：
`--snapshot` 写出**规范化、排序、带 SHA-256 的快照**（同样的数据必然产生同样的
字节，"没有变化"因此是可验证的事实），`--diff` 把两个快照按列级比对成
新增/变更/删除。`--diff` **完全不联网**。另有 `--serve` 起一个只读的 JSON API
和单文件网页界面（仅监听本地回环、按各登记册更新节奏缓存、同一时刻只允许一个
对外请求）——为什么必须这样，见 `docs/ARCHITECTURE.md`；为什么"时间序列"才是
这个仓库里最值钱的东西，见 `docs/BUSINESS.md`。

**最容易踩的三个坑：**

1. `upreg` 一个扁平数组里混着父记录和子记录，101,964 条文档其实只有 13,930
   家持牌实体，直接数行数会多出 7 倍。本工具按 `_root_` 归组，子记录折叠成
   管道分隔列。
2. MiCA 的 `ac_serviceCode` 规范上是 a–j 的枚举，实际是 27 国监管机构手打的
   自由文本（制表符、缺空格、用大写 `I` 当竖线、`/` 和逗号当分隔符、单元格内
   带换行）。直接去重会得到约 80 个"服务种类"，实际只有 10 个。工具给出
   `ac_serviceCode_normalised` 派生列，映射不上的标 `?` 而不是悄悄丢掉。
3. `eba-psd` 共 328,965 条记录，其中 322,558 条是代理人和分支机构——**代理人
   大多是自然人**，受 (EU) 2018/1725 约束，且有 195,920 条状态为 `Inactive`。
   所以默认只返回 6,407 家机构，要全量得显式加 `--select ALL`。另外
   `Properties` 是"每个对象只有一个键"的数组、值可能是字符串也可能是列表，
   `Services` 按 ISO-2 国家代码分组——那才是真正的护照通行（passporting）范围。
4. EUDAMED 有两个不报错的坑：一是 `size` **静默封顶 300**，你请求 1000 也只
   给 300，"拿到的比要的少"在这里不代表已经取完；二是 `languageIso2Code`
   名义上是显示语言，实际是过滤条件——不带它，`api/eos` 直接 HTTP 500，
   `api/ses/` 则按语言逐条复制，70 家公告机构变成 1,890 行（同一个 `uuid`
   重复约 27 次，只有 `countryName` 的语言不同）。翻页必须用
   `sort=ulid,ASC`（`eudamedIdentifier` 排序会 500，`name` 不唯一）。
5. CTIS（欧盟临床试验数据库，`POST /ctis-public-api/search`，无需认证，
   共 12,229 项试验，带 `sponsor` 和 `sponsorType`）也有两个静默坑：
   `searchCriteria` 即使为空也必须传，否则返回 200 且 `totalRecords: 0`——
   看起来像"没匹配到"，其实是少了必填字段；翻页在 **第 10,000 条截断**，
   而 `totalRecords` 照旧报 12,229，翻到空页就停的客户端会不知不觉少 2,229 条。
   本工具会拿实际条数和 `totalRecords` 对比并**明确告警**，让你用 `--query`
   拆成多个小于一万条的查询。另外 CTIS 是"试验表"不是"公司表"，一个申办方
   有几项试验就出现几行，统计公司数前要先按 `sponsor` 去重；约三分之一的
   申办方是医院和大学，不是企业。

**跑之前先跑这三条**（对应 `--inspect` / `--list-values` / `--raw`）：

```bash
uv run eufinreg --source upreg --inspect 200            # 当前真实字段
uv run eufinreg --source upreg --list-enums             # 真实枚举取值
uv run eufinreg --source mica-casp --raw ./raw -o casps.csv   # 留一份原始响应
```

**如果卡住不动：**`www.esma.europa.eu` 的 CDN 有 AAAA 记录，而 Python 没有
Happy Eyeballs（curl 有），在没有 IPv6 出口的网络上会一路等超时。加 `-4` 即可。
`eba-psd` 每次都要下 19 MB、解析后峰值内存约 1.4 GB，结果请存成文件重复使用。

**其他行业同理**（README 有一张 2026-08-16 实测表）：化学品
**ECHA CHEM** 有 JSON API 但以物质为中心，`searchText` 必填，注册人挂在物质
详情下；德国能源 **MaStR** 每日发布约 2.9 GB 的 XML 全量导出，采用
Datenlizenz Deutschland 开放许可（本工具不包装它——2.9 GB 需要流式解析，
与本项目"全量载入内存再扁平化"的架构不兼容）；制药 **EudraGMDP** 和德国
**BaFin** 一样是老式 Struts `.do` 应用，没找到接口；英国 **FCA** 有官方 API
但需免费申请 key。

**免责要点：**持牌 ≠ 在招人，持牌 ≠ 在实际经营，持牌实体 ≠ 运营实体，
注册地址 ≠ 办公地址，**注册地 ≠ 经营地**（EUDAMED 里有大量为进入欧盟市场而
注册的非欧盟制造商，真正对应欧盟落地的是那 2,938 家授权代表）；
`mica-ncasp` 是"不合规实体"名单，`PSD_EXC` / `PSD_ENL`
是"不属于 PSD2 范围"和"依国内法有权经营"，这三类都别混进持牌表。
**EUDAMED 的邮箱电话是为了让患者和监管机构找到器械责任人而公布的，
不是营销名单**——群发邮件在多数成员国违法，也是让公共 API 对所有人关闭的
最快方式；"数据是公开的"本身不构成 GDPR 第 6 条的处理合法性基础。
BaFin、ESMA、EBA 各自都声明不对数据完整性与正确性负责——EBA 更直接写明
该登记册"不具法律效力，也不创设任何法律权利"。

---

## Licence

MIT — see [LICENSE](LICENSE). The register data itself belongs to ESMA, the EBA,
the European Commission and the national competent authorities and carries their
terms, not this project's.
