# The niche

> **DACH licence monitoring for compliance teams.** Watch a list of entities
> across the German, Austrian and Swiss licence registers plus the EU-level ones
> that actually carry DACH firms. Get told when a licence changes. Get a receipt
> you can put in a file.

That sentence is the product, and the software is shaped like it: `ingest`
keeps the archive, `watch` scopes it to the entities somebody cares about,
`receipt` turns one row into evidence. Everything else in this repository is
plumbing underneath those three verbs.

---

## Why this niche and not a bigger one

**Not "European company data".** That market has incumbents with sales teams,
and the data underneath is the same free registers. Competing there means
competing on coverage breadth, which is the one axis where a small tool loses.

**Not "KYC/AML screening".** Sanctions and PEP screening is a solved,
consolidated market. This is adjacent and deliberately outside it.

**DACH licence monitoring, though, is a real seam:**

* **Three regulators, two legal orders, one economic area.** Switzerland is not
  in the EEA, so no EU-level register contains a Swiss licence — a vendor whose
  coverage stops at the EU border cannot answer a Swiss question at all, and
  most stop there.
* **The three countries fail in *different* directions.** Austria publishes its
  entire trade-licence register as open data; Switzerland exposes the federal
  business register over an unauthenticated web service and FINMA publishes a
  daily CSV; Germany's federal registers sit behind `Disallow: /` or a 2.9 GB
  bulk export. Knowing that the German answer has to be routed through ESMA and
  the EBA is a week of work to discover and thirty seconds to use. **That
  routing is the product.**
* **The joins are non-obvious and free.** FINMA publishes the Swiss UID; the UID
  register turns it into legal form, seat, commercial-register status, VAT-group
  membership. Two free sources, one join, an answer neither gives alone.

Narrow enough that the whole surface can be *known*, wide enough that the
question comes up daily in every regulated firm operating across those three
countries.

## Who buys it

The buyer is a compliance, risk or vendor-management function inside a regulated
firm — the people who already keep a spreadsheet of counterparties and check it
by hand, quarterly, by opening four websites.

| Job to be done | Why the DACH shape matters |
|---|---|
| Ongoing due diligence on counterparties and outsourced providers | The list spans Germany, Austria and Switzerland, so it spans three regulators |
| Onboarding and re-verifying intermediaries and brokers | A Swiss intermediary and a German one are two entirely different lookups |
| Third-party/ICT provider oversight under DORA | The register of information is maintained continuously, not once |
| Watching your *own* entries | Your ESMA record is filed by your national authority and nobody tells you when it is wrong — you find out when a counterparty's onboarding rejects you |
| Subcontractor licence checks (AT) | 1.03 M trade licences, category and dormancy included, CC BY 4.0 |

*(Regulatory timelines move; check the current state of any obligation before
quoting it at a customer. The underlying job — "is this counterparty still
licensed, and can I show that I checked" — does not move.)*

## What is scarce (it is not the data)

**The registers publish state and destroy history.** The EBA regenerates its
golden copy nightly and overwrites it. FINMA replaces `uid.csv` every morning.
ESMA overwrites the MiCA CSVs weekly. GISA ships a new monthly file. EUDAMED,
CTIS and the Swiss UID register are live databases with no "as of" parameter at
all. Not one publishes a changelog, a `?since=`, or a feed.

> "Was this firm licensed on 4 March 2026, and for what?" is a question no
> register can answer — and no amount of money buys the answer retroactively.
> Only whoever was already writing it down has it.

That archive compounds from the day it starts and cannot be acquired later. A
better-funded competitor still has to wait a year to have a year of it.

**Change is the signal; state is the substrate.** Nobody's job is to read 2,938
Swiss authorisation holders. The job is *this counterparty lost its
authorisation* — the most valuable event in the dataset and the one the
registers announce least loudly: a row simply stops appearing tomorrow.

**Evidence, not answers.** A compliance file needs to show what was checked,
when, and that it has not been edited since. `receipt` produces exactly that,
reproducibly, with the raw response bytes inside it.

## What is built

| Capability | Command | State |
|---|---|---|
| 21 register clients, EU + DE/AT/CH, traps handled | `eufinreg --source …` | shipped |
| Nightly archive: snapshots, raw bytes, retention, locking | `eufinreg ingest` | shipped |
| Column-level change events | `eufinreg watch`, `--diff` | shipped |
| Watchlists with identifier/name confidence and coverage reporting | `eufinreg watch --coverage` | shipped |
| Reproducible, self-verifying evidence bundles | `eufinreg receipt` | shipped |
| Interface-drift detection against the live registers | `eufinreg doctor` | shipped |
| Read-only API + UI, archive-backed, personal-data gated | `eufinreg serve` | shipped |
| Notification delivery (email, Slack, webhook) | — | not built |
| Hosted multi-tenant service, billing | — | not built |
| Cryptographic signing / RFC 3161 timestamping of bundles | — | not built, and not faked |
| LEI-based cross-register identity | `--source gleif` | shipped |
| VAT verification, procurement leads | `--source vies` / `--source ted` | shipped |
| Resolution *without* a shared identifier | — | **the honest gap** |

Entity resolution is the gap that decides whether this is a product or a script,
and it is smaller than it was. Adding GLEIF closed the tractable half: an LEI on
a watchlist now matches the same entity in ESMA's `upreg`, in the MiCA register
and in GLEIF itself, because all three publish that identifier — and GLEIF
carries the entity's *national* register number (`HRB 204159` in Germany, a
SIREN in France) on top of it. FINMA's UID does the same job for Switzerland
against the UID register.

What remains unsolved is resolution where **no** identifier is shared, and the
answer there is deliberate: fall back to normalised names, **label which kind of
match happened on every row**, and report the entities nobody could find. Sell it
as "linked where a shared identifier exists, flagged where it does not" — never
as magic.

One strategic note hiding in GLEIF: it is the only source here that publishes
**delta files**. If regulators followed suit, the archive's moat would shrink —
the risk named at the bottom of this document, visible in miniature.

## How it is sold

Open source is the distribution channel, not a giveaway. The people who evaluate
a compliance data vendor are the people who will read `sources/eba_psd.py`
before they read a brochure, and this repository is the credential: it documents
the traps, refuses the endpoints it should refuse, and says out loud where its
own matching is weak.

| Tier | What | Price shape |
|---|---|---|
| **Self-hosted** | Everything in this repository. Run your own archive. | Free |
| **Watch** | Hosted archive + daily change notifications for your watchlist | per watched entity / month, with a floor |
| **Evidence** | Receipts on demand, retained and retrievable | per bundle, or an allowance |
| **History** | Query the archive back in time; bulk extracts for analysis | annual, per register |

The unit economics are unusual and worth stating plainly: **cost is per
register, revenue is per customer.** One daily 9 MB GISA pull serves every
Austrian customer there will ever be. The marginal cost of the thousandth
watched entity is a row in a table.

Land with Watch on a handful of counterparties, expand by entity count and by
register, and let Evidence pull the buyer from "useful" to "in our process".

## Why it is defensible

1. **The archive.** Time-ordered, checksummed, and impossible to backfill.
2. **The routing knowledge.** Which register actually answers a German question,
   and why the obvious one cannot — and, upstream of that, which *kind* of
   obligation produces the answer at all: identity (GLEIF), tax (VIES), public
   money (TED), licence (the rest). It is in the README because being right in
   public is the marketing.
3. **The refusals.** Not lifting BASG's bundled API token, not touching Zefix or
   BaFin's portal, gating personal data in code. A vendor that scrapes what it
   was asked not to is one operator complaint away from losing the data — and
   its customers' access with it.

None of those is a patent. Together they are about a year of head start and a
reputation that a bigger competitor has to earn rather than buy.

## What would kill it

* **A regulator ships the feed itself.** Best outcome for the world, worst for
  the business. Unlikely soon: the EBA has had a machine-readable mandate since
  2019 and still overwrites its file nightly.
* **An interface closes.** EUDAMED's and CTIS's backends are undocumented
  courtesies, not contracts. Mitigations: 18 sources, so no single closure is
  fatal; `doctor` catches drift within a week; and the archive keeps its value
  even after the tap stops.
* **Data licensing.** GISA is explicitly CC BY 4.0. The rest is unstated, which
  is not permissive. Redistribution needs a per-source review, and the answer
  will sometimes be "attribute and link, do not mirror".
* **GDPR.** The EBA agent records and EUDAMED's contact details are personal
  data. A product that resells them as a lead list deserves to fail. Watch and
  Evidence are defensible because they are about *organisations* under a
  specific legitimate interest; bulk resale is not, and blurring the two to make
  a slide look better is how a compliance vendor becomes a compliance incident.
* **Nobody wants it.** The counter-evidence is that people do this by hand
  today. Test it by selling Watch to five firms before building the billing
  system.

## The one-sentence version

The registers give away today's answer and delete yesterday's; this sells the
timeline, the alert when it moves, and a receipt you can put in a file — for the
one region where three regulators, two legal orders and a million trade licences
make that genuinely hard.
