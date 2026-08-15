# eufinreg

Pull structured lists of **licensed financial entities** out of the EU public
registers — as CSV/JSON, from the command line, with one runtime dependency.

The point of this repository is only half the code. The other half is the
**interface documentation below**, which is the result of reading ESMA's A2A
help pages and then probing the live endpoints to check what they actually do.
Public registers tend to have interfaces that are undocumented, half-documented,
or documented somewhere nobody links to.

Everything in the "How the interfaces work" section was verified against the
live services on **2026-08-15**. Counts will drift; the shapes should not.

---

## TL;DR — what has an interface and what does not

| Register | Official machine interface? | What this tool does |
|---|---|---|
| **ESMA Registers** (MiFID investment firms, AIFMs, UCITS management companies, crowdfunding providers, trading venues, benchmark administrators, funds, MMFs…) | **Yes** — a read-only Apache Solr A2A endpoint, no authentication, [documented by ESMA](https://registers.esma.europa.eu/publication/helpApp) | Reads it directly, pages with `cursorMark`, flattens parent/child blocks into one row per entity |
| **ESMA interim MiCA register** (CASPs, ART issuers, EMT issuers, white papers, non-compliant entities) | **Yes, sort of** — five stable CSV URLs regenerated weekly. No query API, but a documented, machine-readable bulk download | Downloads and parses them, normalises the messiest field |
| **BaFin Unternehmensdatenbank** (Germany) | **No** — see [the evidence](#bafin-germany--no-official-interface) | Does **not** scrape it. Routes you to the ESMA data that covers BaFin-supervised entities, and tells you what that route misses |
| **FMA Unternehmensdatenbank** (Austria) | **No** — see [the evidence](#fma-austria--no-official-interface) | Same |

No HTML scraping happens anywhere in this project.

---

## Install / run

Nothing to clone, no virtualenv to manage — the single-file script carries its
own dependency metadata ([PEP 723](https://peps.python.org/pep-0723/)):

```bash
uv run https://raw.githubusercontent.com/CHANGE-ME/eufinreg/main/scripts/eufinreg_solo.py --help
```

As a project:

```bash
git clone https://github.com/CHANGE-ME/eufinreg && cd eufinreg
uv sync                     # creates .venv from the committed uv.lock
uv run eufinreg --list-sources
```

As a tool, without cloning:

```bash
uv tool install git+https://github.com/CHANGE-ME/eufinreg
eufinreg --list-sources
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

### Flags that matter

| Flag | Effect |
|---|---|
| `--inspect [N]` | Sample N records (default 50), print real field names, fill rate, distinct counts, sample values. For block-structured sources it prints the profile **twice**: as received, and after flattening. |
| `--list-values FIELD` | Distinct values of `FIELD` with counts. On Solr this is a **facet query** — exact over the whole core, one request, not a sample. |
| `--list-enums` | `--list-values` for every field the source treats as an enumeration. |
| `--raw DIR` | Save untouched response bodies + `manifest.jsonl`. |
| `--contains TEXT` | Case-insensitive substring across **all** fields. Repeatable, ANDed. Survives field renames. |
| `--field NAME=VALUE` | Case-insensitive **exact** match on one field. Repeatable, ANDed. Warns loudly if `NAME` does not exist in any fetched record. |
| `--select VALUE` | Server-side selector, pushed down (Solr `q`). Cheap. |
| `--query Q` | Raw source-native query, passed through untouched. |
| `--no-flatten` | Emit records exactly as received. |
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

### BaFin (Germany) — no official interface

Checked on 2026-08-15:

* The Unternehmensdatenbank at <https://portal.mvp.bafin.de/database/InstInfo/>
  is an Apache Struts application. The search form posts to `sucheForm.do`;
  results are HTML tables. No JSON, XML, CSV or Excel export link exists in the
  markup; there is no `/api`, no OpenAPI document, no `.json` endpoint.
* BaFin's [Datenbanken & Übersichten](https://www.bafin.de/DE/PublikationenDaten/Datenbanken/Datenbanken_node.html)
  index lists 24 databases. None advertises a machine-readable export, an API,
  a web service or an open-data feed for the company database.
* BaFin's MVP-Portal is a *reporting submission* portal for supervised firms —
  authenticated upload of XML filings. It is not a data-retrieval API.

**Conclusion: no official machine interface exists**, so this project does not
scrape it. HTML scraping of a `.do` search form would break on the next
framework upgrade and would be indistinguishable from abuse in BaFin's logs.

What to use instead, and what it costs you:

* BaFin-supervised **investment firms, AIFMs and UCITS management companies**
  are in `upreg` — 2,815 entities under
  `ae_competentAuthority=Federal Financial Supervisory Authority (BaFin)`.
* BaFin-authorised **CASPs** are in `mica-casp` under
  `ae_homeMemberState=DE`.
* **Not covered by either:** German credit institutions and banks licensed
  under the KWG, insurers under the VAG, payment and e-money institutions under
  the ZAG, and tied agents (*vertraglich gebundene Vermittler*). Those exist
  only in BaFin's own HTML databases. If you need them, the honest answer today
  is manual export or a written request to BaFin.

### FMA (Austria) — no official interface

* <https://www.fma.gv.at/unternehmensdatenbank-suche/> returns **HTTP 403** to
  any non-browser client — an explicit signal that programmatic access is not
  wanted. No API, no documented export, and no FMA company-database dataset
  found on data.gv.at.
* Route instead: `upreg` with
  `ae_competentAuthority=Austrian Financial Market Authority (FMA)` (661
  entities), plus `mica-casp` with `ae_homeMemberState=AT`.
* Same gap as Germany: Austrian banks, insurers and payment institutions are
  not in the ESMA data.

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
  footprint is `ac_serviceCode_cou`, not `ae_homeMemberState`.
* **Registered address ≠ office address.** `ae_headOfficeAddress` and MiCA's
  `ae_address` are the addresses notified to a regulator. Expect law firms,
  company formation agents and holding-company letterboxes.
* **`mica-ncasp` is the opposite of a licence list.** It names entities ESMA or
  an NCA considers non-compliant. Do not merge it into a "licensed firms" table.

**Data quality.**

* Every register here is compiled from submissions by national competent
  authorities (and, for MiCA, the EBA). Spelling, completeness and timeliness
  vary by authority. `ae_lei` is frequently empty. `ac_serviceCode` is free text
  in an enum-shaped field.
* Fields disappear and appear. That is why `--inspect` exists and why nothing in
  this tool hardcodes a field list.

**Update frequency.**

* MiCA CSVs: ESMA states **weekly**. Check `Last-Modified` for the real vintage.
* `upreg`: ESMA publishes no update cadence for the A2A endpoint. Use the
  per-record `ae_lastUpdate`, not the `timestamp` field — `timestamp` is the
  Solr indexing time (uniformly `2026-04-09` on sampled documents, i.e. the last
  full reindex).
* BaFin states its own database is updated daily — irrelevant here, since it has
  no interface.

**The regulators' own disclaimers.**

* BaFin: "a liability of BaFin for completeness and correctness of information
  is excluded."
* ESMA, on the MiCA register: "The crypto-asset white papers listed in ESMA's
  register have not been reviewed or approved by any competent authority in any
  Member State of the European Union. The offeror and/or issuer of the
  crypto-asset is solely responsible for the content of each crypto-asset white
  paper."
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
  whole core and filtering locally.
* Cache. If you need the data twice today, write it to a file the first time.

**Change the User-Agent.** The default is
`eufinreg/0.1.0 (+https://github.com/CHANGE-ME/eufinreg; public-register client)`.
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
it with `--max-docs`.

---

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

The fixtures in `tests/data/` are trimmed captures of real responses. When a
register changes shape, re-capture with `--raw` and update the fixtures — the
tests are there to pin *this* project's behaviour, not to monitor ESMA.

---

## 中文速览

**结论先行。** ESMA 有官方机器接口（Solr A2A，无需认证，无需 API key），
是这四个登记库里唯一真正可编程访问的；MiCA 登记册是每周重新生成的 5 个固定
URL 的 CSV，算半个接口；**BaFin 和 FMA 都没有官方接口**——BaFin 的
Unternehmensdatenbank 是 Struts `.do` 的 HTML 应用，FMA 对非浏览器客户端直接
返回 403。所以本项目**不爬 HTML**，改走 ESMA 的数据（BaFin 监管实体 2,815 家、
FMA 监管实体 661 家都在 `upreg` 里），并在上面明确写出这条路线覆盖不到什么
（德奥的银行、保险、支付机构不在 ESMA 数据里）。

**最容易踩的两个坑：**

1. `upreg` 一个扁平数组里混着父记录和子记录，101,964 条文档其实只有 13,930
   家持牌实体，直接数行数会多出 7 倍。本工具按 `_root_` 归组，子记录折叠成
   管道分隔列。
2. MiCA 的 `ac_serviceCode` 规范上是 a–j 的枚举，实际是 27 国监管机构手打的
   自由文本（制表符、缺空格、用大写 `I` 当竖线、`/` 和逗号当分隔符、单元格内
   带换行）。直接去重会得到约 80 个"服务种类"，实际只有 10 个。工具给出
   `ac_serviceCode_normalised` 派生列，映射不上的标 `?` 而不是悄悄丢掉。

**跑之前先跑这三条**（对应 `--inspect` / `--list-values` / `--raw`）：

```bash
uv run eufinreg --source upreg --inspect 200            # 当前真实字段
uv run eufinreg --source upreg --list-enums             # 真实枚举取值
uv run eufinreg --source mica-casp --raw ./raw -o casps.csv   # 留一份原始响应
```

**如果卡住不动：**`www.esma.europa.eu` 的 CDN 有 AAAA 记录，而 Python 没有
Happy Eyeballs（curl 有），在没有 IPv6 出口的网络上会一路等超时。加 `-4` 即可。

**免责要点：**持牌 ≠ 在招人，持牌 ≠ 在实际经营，持牌实体 ≠ 运营实体，
注册地址 ≠ 办公地址；`mica-ncasp` 是"不合规实体"名单，别混进持牌表。
BaFin 和 ESMA 各自都声明不对数据完整性与正确性负责。

---

## Licence

MIT — see [LICENSE](LICENSE). The register data itself belongs to ESMA and the
national competent authorities and carries their terms, not this project's.
