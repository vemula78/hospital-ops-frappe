# Converting the Hospital Operations Workspace to ERPNext

Plan, 23-Aug-2026. Written against the built application, not a sketch of it.

## The one fact that should drive the timing

Production holds **1 user, 1 workspace, 1 inbox item and 5 audit events**. No
documents, no files, no CSR projects, no procurement requests.

There is no data-migration problem today. There will be one after a few months
of real use, and it would be the most expensive part of the whole exercise —
91 tables of hospital records with an audit chain over them. **If this
conversion is going to happen, now is the cheapest it will ever be.** That is
the argument for deciding quickly, not for deciding yes.

## What exists, measured

|                   |                                               |
| ----------------- | --------------------------------------------- |
| Domain modules    | 26                                            |
| Tables            | 91                                            |
| Domain logic      | 24,133 lines                                  |
| Application total | 64,498 lines                                  |
| Tests             | 1,074 (24,683 lines) + 15 Playwright journeys |
| Migrations        | 30                                            |
| Pages             | 38                                            |

Largest domains by weight: procurement (3,237 lines / 15 tables), CSR (2,708 /
13), maintenance (1,910 / 3), research (1,861 / 7), consumables (1,234 / 4),
signage (1,104), software (1,046).

## The strategic call

**A one-to-one port is the worst available option.** It would re-implement
24,000 lines of domain logic in Python, fight ERPNext's conventions the whole
way, and discard 1,074 tests — to arrive at the same product with less
assurance than it has now.

The conversion is only worth doing if it _deletes_ work rather than
translating it. Sorted by that test, the 26 modules fall into three buckets.

### Bucket 1 — Adopt ERPNext, delete our code (~55% of domain logic)

These are not ports. The ERPNext module already does the job and our code goes
away entirely.

| Ours                                                        | ERPNext                                                                         | Note                                                                                                                        |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `procurement.ts` (3,237 ln)                                 | Material Request → RFQ → Supplier Quotation → Purchase Order → Purchase Receipt | ERPNext's core competence. Quotation comparison, partial receipt and stage tracking are all native. The single biggest win. |
| `maintenance.ts` (1,910 ln)                                 | Asset Maintenance, Asset Repair, Asset Maintenance Log                          | Service contracts map to Asset Maintenance Team + schedule                                                                  |
| Assets                                                      | Asset, Asset Movement, Location, Custodian                                      | Native register with custodian and location                                                                                 |
| `projects.ts` + `tasks.ts` (1,517 ln)                       | Project, Task, Task depends-on                                                  | Milestones become Tasks flagged as milestones                                                                               |
| `people.ts`                                                 | Contact, Supplier, Address                                                      | Native                                                                                                                      |
| `consumables.ts` (1,234 ln)                                 | Item, Item Price, Supplier Quotation history                                    | **Caveat below**                                                                                                            |
| `notifications.ts` + `notification-delivery.ts` + `notify/` | Notification, Email Queue, Notification Settings                                | Replaces the email work built this week                                                                                     |
| `jobs/` queue                                               | Scheduler + RQ background jobs                                                  | Native, durable, already running on your instance                                                                           |
| `identity.ts` + `mfa.ts`                                    | Users, Roles, built-in 2FA                                                      | Native                                                                                                                      |
| `search.ts` (883 ln)                                        | `__global_search`                                                               | Native                                                                                                                      |
| `reports.ts`                                                | Query Report, Script Report, Dashboard                                          | Native                                                                                                                      |
| `export.ts`                                                 | Data Export, backups                                                            | Native                                                                                                                      |

**Consumables caveat.** This module is deliberately _not_ an inventory system —
the authoritative stock figure lives in the pharmacy system. ERPNext will want
to be an inventory system. Use Item + Item Price for price history and keep
the Stock module switched off, or you will end up maintaining a second, wrong
stock ledger.

### Bucket 2 — Rebuild as custom doctypes (~45%)

Genuinely custom domains with no ERPNext equivalent. These get rebuilt, not
translated — and they are much smaller in Frappe because the framework
supplies the CRUD, list views, permissions, comments and change history.

| Ours                                      | Approach                                                                                                                                                                                                |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CSR (2,708 ln / 13 tables)                | **Reuse your own `trust_compliance` app patterns** — Fund, Fund Transfer, Grant Utilisation and Donor are close analogues already in production. Use _submittable_ doctypes for the ledger (see below). |
| `research.ts` (1,861 ln)                  | Study + Ethics Submission doctypes; ethics decisions through the Workflow engine                                                                                                                        |
| `signage.ts` (1,104 ln)                   | Sign + Sign Design doctypes; the stage gates are exactly what Frappe Workflow expresses                                                                                                                 |
| `software.ts` (1,046 ln)                  | Project + custom UAT Result child table                                                                                                                                                                 |
| `website.ts`                              | Web Page Review doctype with a workflow                                                                                                                                                                 |
| `documents.ts` (958 ln)                   | File (native) + a Controlled Document doctype carrying expiry and review dates                                                                                                                          |
| `meetings.ts`, `waiting.ts`, `capture.ts` | Small doctypes; Waiting For could extend ToDo                                                                                                                                                           |
| `review.ts`                               | A Page or Script Report, not a doctype                                                                                                                                                                  |
| `assistant.ts`                            | Custom page; lowest priority                                                                                                                                                                            |

### Bucket 3 — Drop deliberately, and say so

Each of these was built for a threat model that a single-user internal tool on
the hospital network does not have. Dropping them is most of the speed gain.

1. **The HMAC audit hash-chain.** Frappe's `Version` doctype records every
   field change with author and timestamp, but it is _not_ tamper-evident —
   no chain, no head anchor. For an internal tool this is very likely
   sufficient, and the chain was arguably over-built. **Decide this
   explicitly** rather than discovering it later: if CSR statutory reporting
   needs tamper evidence, it is custom work on top of Frappe.
2. **Append-only database triggers.** Frappe discourages DB triggers. The
   idiomatic replacement is _better_ than it first appears: a **submittable
   doctype** (docstatus 0/1/2) makes a submitted record immutable, and a
   correction is Cancel + Amend — which is precisely the "reversal, never an
   edit" rule the triggers enforce, supplied by the framework.
3. **"Never store an aggregate."** ERPNext stores `grand_total` on every
   document. Fighting this is fighting the framework; accept stored
   aggregates and rely on ERPNext's own recalculation hooks.
4. **`bigint` paise.** Frappe Currency is a fixed-precision decimal with
   well-tested rounding. Different model, not a worse one.
5. **The 1,074-test suite and 15 Playwright journeys.** None port. Expect to
   rewrite a far smaller suite (`FrappeTestCase`) covering the custom
   doctypes only — the adopted ERPNext modules are already tested upstream.
6. **Offline PWA capture.** Lost unless rebuilt.
7. **Everything deployed this week** — Azure Blob storage, SMTP digests,
   backup timers, the restore drill. Frappe supplies all four (File storage,
   Email Queue, `bench backup`, scheduler). This work does not carry over,
   which is worth knowing before you decide.

## Things that will bite, named in advance

- **Read-then-write races.** Twelve modules take `pg_advisory_xact_lock`
  before a read-then-write, because the checks are against a _sum or a count_
  that no unique index can express — two expenditures both skipping their
  warning, two deliveries together over-accepting. Frappe's equivalents are
  row locks (`for_update=True`) and document locks. **The races do not
  disappear with the framework**; the CSR fund ledger and delivery acceptance
  both need this thought through again, not assumed solved.
- **MariaDB, not Postgres.** The existing ERPNext instance is MariaDB. The two
  views (`csr_project_financials`, `quotation_financials`) and the half-even
  rounding spelled out in their SQL do not carry over as written.
- **Single site, not multi-workspace.** Every domain read takes a workspace
  from the auth context. In Frappe a site _is_ the tenant. This simplifies the
  code and removes a whole class of bug — but it means the workspace concept
  disappears rather than translating.
- **Naming collisions.** ERPNext already has a `Task`, a `Project` and an
  `Asset`. Custom doctypes need a prefix.

## Phasing

Each phase is independently useful and independently abandonable. Do not
big-bang this.

| Phase | Work                                                                                                                                | Why here                                                                                                                          |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| **0** | Scaffold the app (`bench new-app hospital_ops`), decide the module list and naming prefix, confirm it installs on the existing site | One day. Confirms the ground before committing                                                                                    |
| **1** | **Configure only, no code**: Suppliers/Contacts, Projects + Tasks, Assets, the Buying flow                                          | Replaces the largest and most complex domain (procurement) with configuration. If this phase disappoints, stop — the case is gone |
| **2** | Small custom doctypes: Waiting For, Capture/Inbox, Meetings                                                                         | Fast, restores the daily-driver workflow                                                                                          |
| **3** | **CSR**, using submittable doctypes for the ledger and `trust_compliance` as the template — **done, 24-Aug-2026** (see below)                                         | The largest custom build; do it once the framework is familiar                                                                    |
| **4** | Research + ethics workflow — **done, 24-Aug-2026** (see README)                                                                     | Self-contained                                                                                                                    |
| **5** | Build & Publish: Signage, Website, Software                                                                                         | Self-contained; workflow-engine heavy                                                                                             |
| **6** | Reports, dashboards, weekly review, notification config                                                                             | Cheap once the data model exists                                                                                                  |
| **7** | Decommission the Next.js app and `ops.sssihms.org`                                                                                  | Only after 1–6 are in real use                                                                                                    |

**The go/no-go gate is the end of Phase 1.** If configuring ERPNext's Buying
module does not visibly beat the procurement module already built and tested,
the rest of the plan will not pay for itself either.

## Recommendation

Two things are true at once and both should be said.

**The conversion has a real case.** ERPNext genuinely absorbs procurement,
assets, maintenance, projects, contacts, notifications, jobs, auth, search and
reporting — roughly 55% of the domain logic, including the single most complex
module. You already run the instance, you already have five Frappe apps, and
`trust_compliance` is a working precedent for the hardest custom piece. A
Frappe rebuild of the remaining 45% would be substantially smaller than 24,000
lines, because the framework supplies the parts that took the longest here.

**But be clear about what the speed actually came from.** Most of the elapsed
time on this build went into the guardrails in Bucket 3 — the audit chain,
append-only enforcement, race handling, tenant isolation, and three rounds of
independent audit — not into CRUD. Rebuilding on ERPNext is faster largely
because _those guardrails are being dropped_, and only partly because Frappe
supplies scaffolding. A Frappe rebuild carrying the same guardrails would not
be dramatically quicker. That is a legitimate trade for an internal
single-user tool; it is not a free one, and it should be made knowingly.

**Suggested decision:** run Phase 0 and Phase 1 only, timeboxed. Configure the
Buying flow against a real procurement case and compare it directly with
`/procurement` as built. That is a small, bounded experiment that answers the
question with evidence instead of estimation — and it is the same gate that
tells you to stop.

## What is _not_ recommended

- Running both systems in parallel long-term. Two systems of record for the
  same procurement request is the failure mode this application was built to
  end.
- Porting the audit chain to Frappe before deciding whether Version is
  sufficient. Decide first.
- Starting with CSR because it is the most interesting. It is the largest
  custom build and the worst place to learn the framework's grain.

---

*24-Aug-2026: this plan moved here from the productivity-app repo. The ERPNext
conversion is a separate project in this repository; the Next.js application
repo stays untouched and is used read-only as the behavioural reference.*

## Phase 3 — done, 24-Aug-2026

Built and live on `erp.sssihms.org`. Six doctypes, all prefixed `CSR ` and
each name verified free on the site before creation (`trust_compliance` owns
`Fund`, `Trust Donation` and `Grant Utilisation` on the same bench):
**CSR Funder**, **CSR Opportunity**, **CSR Project**, **CSR Tranche** (child),
**CSR Fund Event** (submittable), **CSR Reporting Obligation**, plus the
**CSR Project Financials** Script Report. The README has the full behaviour
notes; the short version of what was carried over from `csr.ts` and what was
not:

**Carried over.** The append-only ledger (Frappe submission in place of the
Postgres `BEFORE UPDATE OR DELETE` triggers, with cancellation refused so a
correction is a reversal entry); direction in the `kind` rather than the sign;
reversal ceilings; the overridable-warning pattern with its exact-string
acknowledgement; computed-not-stored financials with one summing function
shared by the document method and the report; derived tranche and obligation
overdueness; evidence verified by a named person; the
lock-before-read-before-write ordering, with the sums taken as locking reads.

**Deliberately not carried over.** Bigint-paise money (Frappe `Currency` per
the Bucket 3 decision, with `flt(x, 2)` rounding at every comparison); the
audit hash chain (Frappe `Version` via `track_changes`); the workspace/tenancy
dimension (single-tenant here); and the reference module's
evidence-completeness snapshots, outcome metrics, proposals, communications
log and draft-report generator — none of which the Phase 3 brief scoped, and
all of which are additive later if the desk asks for them.

**Still outstanding, honestly.** Concurrency is asserted by source-level
lock-order checks rather than by driving two overlapping transactions, because
a `bench execute` is one request per process. That is the same limitation
Phase 2 recorded, not a new one.

**Audited.** Codex reviewed the phase: two P1 claims (submitted rows
deletable; `frappe.client.set_value` mutating submitted rows) were disproven
against the container's frappe v16 source and probed live — see the README for
the file:line evidence. Four findings were valid and are fixed: the project
status is now decided on the locked read rather than a pre-lock one; the
report lists projects with `get_list` so permission query conditions apply;
amounts with more than two decimal places are refused; and tranches sharing a
date allocate by child `idx` rather than arrival order. The one residual
recorded honestly is `frappe.db.set_value`, a raw `UPDATE` reachable only from
server-side code or a console, which no app-level guard can prevent.

**Verification.** `run_phase3_tests` 111 passed / 0 failed;
`run_phase2_tests` 32 passed / 0 failed (no regression);
`bench run-tests --app hospital_ops --doctype "Quick Capture"` 5 of 5.
All CSR record counts back to zero after the runs.
