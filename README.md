# eufinreg

Pull structured lists of **licensed and registered companies** out of the EU
public registers — as CSV/JSON, from the command line, with one runtime
dependency.

**The premise.** Any industry that needs a licence, a registration, a
certification, a subsidy or an approval has a regulator sitting on a company
directory more complete than any commercial database — because inclusion is a
legal condition of trading, not a marketing decision. Those directories are
public, free, structured and, more often than people assume, machine-readable.
This tool reads them.

> **Name note:** the package started as a financial-registers client and still
> carries the name. It now covers medical devices too, and the sector survey
> below maps the rest. A rename is pending; the CLI is stable in the meantime.

The point of this repository is only half the code. The other half is the
**interface documentation below**, which is the result of reading the regulators'
help pages and then probing the live endpoints to check what they actually do.
Public registers tend to have interfaces that are undocumented, half-documented,
or documented somewhere nobody links to.

Everything in the "How the interfaces work" section was verified against the
live services: the ESMA sections on **2026-08-15**, everything else on
**2026-08-16**. Counts will drift; the shapes should not.

---

## TL;DR — what has an interface and what does not

| Register | Sector | Official machine interface? | What this tool does |
|---|---|---|---|
| **ESMA Registers** (MiFID investment firms, AIFMs, UCITS management companies, crowdfunding providers, trading venues, benchmark administrators, funds, MMFs…) | Finance | **Yes** — a read-only Apache Solr A2A endpoint, no authentication, [documented by ESMA](https://registers.esma.europa.eu/publication/helpApp) | Reads it directly, pages with `cursorMark`, flattens parent/child blocks into one row per entity |
| **ESMA interim MiCA register** (CASPs, ART issuers, EMT issuers, white papers, non-compliant entities) | Finance | **Yes, sort of** — five stable CSV URLs regenerated weekly. No query API, but a documented, machine-readable bulk download | Downloads and parses them, normalises the messiest field |
| **EBA PSD2 register** (payment institutions, e-money institutions, AISPs, their agents and branches) | Finance | **Yes, and it is required to be** — [Commission Implementing Regulation (EU) 2019/410](https://eur-lex.europa.eu/eli/reg_impl/2019/410/oj) obliges the EBA to publish it electronically. A nightly JSON "golden copy" with a published SHA-256 | Reads the file-metadata endpoint, downloads the archive, **verifies both checksums**, flattens it, labels the codes from the register's own metadata |
| **EUDAMED** (medical device manufacturers, importers, authorised representatives, procedure-pack producers, notified bodies) | Medical devices | **Yes** — the JSON API the public site runs on, no authentication, not excluded by `robots.txt`. 48,893 organisations **with email and phone** | Pages it with a unique sort key, always sends the mandatory language parameter, flattens the nested reference-data blocks |
| **CTIS** (authorised clinical trials and their sponsors) | Pharma / CRO | **Yes** — `POST /ctis-public-api/search`, no authentication. 12,229 trials, sponsor and sponsor type per trial | Pages it, and **warns when the register's 10,000-record window silently truncates the answer** |
| **BaFin Unternehmensdatenbank / ZAG register / VGV register** (Germany) | Finance | **A human-facing one, yes; an automatable one, no** — the result pages do offer CSV/XML/Excel export links, but the whole portal host is `robots.txt: Disallow: /`. See [the evidence](#bafin-germany--an-export-button-behind-a-blanket-robots-ban) | Does **not** touch the portal. Shows you the exact export URLs to click yourself, and routes automation to the ESMA and EBA data that covers BaFin-supervised entities |
| **FMA Unternehmensdatenbank** (Austria) | Finance | **No** — see [the evidence](#fma-austria--no-interface-at-all) | Same routing, no export to click |

Eight further registers across chemicals, aviation, energy, automotive and
non-EU finance were probed on 2026-08-16 and are documented — with what was
actually found — in
[The same trick in other industries](#the-same-trick-in-other-industries).

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
| Medical devices | [NANDO](https://webgate.ec.europa.eu/single-market-compliance-space/) | Notified bodies across all CE-marking directives, not just devices | SPA on `webgate.ec.europa.eu`; for devices, EUDAMED's `api/ses/` already returns the NANDO notification URLs per body | surface |
| Pharma / CDMO | [EudraGMDP](https://eudragmdp.ema.europa.eu/) | Every GMP manufacturing and import authorisation in the EEA — i.e. the contract-manufacturing industry | **None found.** Redirects to `/inspections?key=public`, an Apache Struts app (`.do` actions, `jsessionid` in the URL, prototype.js/scriptaculous). Same shape as BaFin's portal | surface |
| Pharma / CRO | [CTIS](https://euclinicaltrials.eu/ctis-public/search) | 12,229 authorised clinical trials and their sponsors | **JSON API, no auth** — [wrapped](#ctis--who-runs-clinical-trials-in-europe). There is also an **asynchronous** CSV export (`POST /search/download` → `{"taskId": …}` to poll), not used here | probed |
| Chemicals | [ECHA CHEM](https://chem.echa.europa.eu/) | REACH registrants — every company that registered a substance | **JSON API, no auth**, but substance-first: `GET /api-substance/v1/substance?searchText=…`. `searchText` is mandatory (blank → HTTP 400 `[searchText must not be blank]`), and registrants hang off the substance detail, so a company directory means walking substances. No OpenAPI document at the usual paths | probed |
| Energy | [MaStR](https://www.marktstammdatenregister.de/MaStR/Datendownload) (BNetzA) | Every German electricity/gas market participant and generation unit | **Open bulk data.** A daily full export at a predictable URL (`Gesamtdatenexport_YYYYMMDD_<v>.zip`), XML, explicitly licensed **Datenlizenz Deutschland – Namensnennung 2.0**. It is ~2.9 GB | probed |
| Aviation | [EASA](https://www.easa.europa.eu/en/domains/aircraft-products/continuing-airworthiness-organisations/foreign-part-145-organisations) | Part-145 / Part-147 / Part-CAMO / Part-CAO approvals — MRO and airworthiness organisations | Dataset pages with a UI export button; the lists render client-side and no stable machine URL was found from the markup. XLSX snapshots exist under `/sites/default/files/datasets/`. **Note the scope trap:** EASA directly approves *third-country* organisations; EU-based ones are approved by national aviation authorities and are not on these lists | surface |
| Finance (UK) | [FCA Financial Services Register](https://register.fca.org.uk/) | Every FCA-authorised firm and individual | **Official documented API, key required.** `GET /services/V0.1/Firm/{FRN}` returns HTTP 403 `{"Success":"false", "Sorry, this page is not available. Missing Headers."}` without the `X-Auth-Email` / `X-Auth-Key` headers. Registration is free; this project ships no key | probed |
| Finance (LU/NL/CH) | CSSF, DNB, FINMA | Luxembourg funds and managers; Dutch supervised institutions; Swiss authorised institutions | Not established. The commonly cited CSSF entity-search URL 404s; DNB's and FINMA's register pages serve HTML | surface |
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
  to go looking.

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
* BaFin states its own database is updated daily. That is the one advantage of
  clicking its export by hand over reading the EBA's copy of the same filings.

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
`eufinreg/0.4.0 (+https://github.com/CHANGE-ME/eufinreg; public-register client)`.
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

`tests/data/eba_psd_goldencopy.zip` is a real golden copy cut down to eight
records chosen to cover the awkward cases (multi-valued names, a string-valued
`Services` entry, an agent, a branch, an entity with no services). Its SHA-256
is baked into `eba_filemetadata.json`, so if you regenerate the archive you must
regenerate that digest too — which is the point: the checksum tests would
otherwise pass vacuously.

---

## 中文速览

**核心前提。** 凡是需要许可、注册、认证、补贴或审批才能经营的行业，监管机构
手里都有一份比任何商业数据库都完整的企业名录——因为"在册"是合法经营的前提，
不是企业的营销选择。这些名录公开、免费、结构化，而且比多数人以为的更常带有
机器接口。本项目就是读这些名录的。（项目名 `eufinreg` 是金融时代留下的，
现已扩展到医疗器械，改名待办。）

**结论先行。** 四个已实现的登记库都有官方机器接口：**ESMA** 的 Solr A2A
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
