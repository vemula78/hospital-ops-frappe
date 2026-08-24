### Hospital Ops

Hospital administration workspace for SSSIHMS Whitefield

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch n
bench install-app hospital_ops
```

### Phase 2 — Quick Capture, Waiting For, Meeting Record

Three doctypes that restore the daily-driver GTD workflow from the Next.js
prototype (`src/server/domain/capture.ts`, `waiting.ts`, `meetings.ts`).
Roles are `System Manager` only for now — this is a single-user evaluation
and a role scheme is deliberately deferred.

**Quick Capture** (`CAP-.#####`) — a frictionless inbox. `capture_text` is
the only thing the user has to type; `status` (Open / Processed / Discarded)
tracks what happened to it, and `processed_into_doctype` / `processed_into`
(a Dynamic Link) record what it became, once it becomes something. The list
view sorts oldest-first (`sort_field: creation`, ascending) — the point of an
inbox is emptying it, not admiring the newest arrival.

- `process_into_todo(name, description=None)` — whitelisted. Creates a ToDo
  (with `reference_type`/`reference_name` pointing back at the capture) and
  marks the capture Processed in the same call, so the pair can never be left
  half-done. Refuses on anything not currently `Open` (already `Processed`
  or `Discarded`).
- `discard(name)` — whitelisted. Marks a capture Discarded. Added during the
  second Codex audit round: once the P3-2 re-fix made a plain save unable
  to move `status` at all without a flagged path (see below), the
  previously unremarkable "edit status to Discarded and save" workflow
  needed a sanctioned path of its own — not part of the original brief, but
  required to keep discarding possible at all once that fix landed.

**Waiting For** (`WF-.#####`) — the delegation register: who owes me what.
`waiting_on` is a `Contact`. `follow_up_on` defaults to `promised_on` when
left empty. A child table, `follow_ups` (`Waiting For Follow Up`:
`followed_up_on`, `note`, `new_promised_on`), keeps a history of what was
said on each chase rather than overwriting the one promise on file.

- **Invariant**: `promised_on` must be on/after `delegated_on`, enforced in
  `validate()`. This is the exact rule that caught a real bug in the source
  app — it stays a hard `CHECK`-style refusal, not a warning.
- `log_follow_up(name, note=None, new_promised_on=None)` — whitelisted.
  Appends a `follow_ups` row and, only when a new promise was actually given,
  moves the parent's `promised_on` and `follow_up_on` to match — in the same
  call. Refused once the item's `status` is no longer `Waiting`.
- List view sorts by `follow_up_on` ascending — the register answers "who do
  I chase today", not "what did they promise".

**Meeting Record** (`MTG-.#####`) — decisions and outcomes, not transcripts.
`decisions` is a child table (`Meeting Decision`: `decision`, `owner_name`,
`due_on`, `todo`).

- `create_todo_from_decision(name, row_name)` — whitelisted. Creates a ToDo
  from one decision row and stamps its name onto the row's `todo` field, so a
  decision either gets a ToDo linked to it or the call fails outright.
  Refuses if the row already has a `todo`, or if `row_name` does not exist on
  the meeting.

Every whitelisted method loads its document and checks permission through
`hospital_ops/hospital_ops/permissions.py::get_doc_for_action` (see the Codex
audit fixes below) rather than trusting the doctype-level grant alone.
Server-side tests (`test_*.py` per doctype, using
`frappe.tests.IntegrationTestCase` — Frappe v16 renamed `FrappeTestCase`)
cover both the negative case and its positive control for each invariant.

#### Codex audit fixes (post-commit `89587f5`/`d869c41`)

An independent Codex audit over those two commits found four issues, all
fixed in place:

- **P2-1 — TOCTOU in `process_into_todo`.** Two concurrent calls could both
  read `status = "Open"` before either wrote back, both insert a ToDo, and
  both mark the capture Processed. Fixed: a locking read,
  `frappe.db.get_value("Quick Capture", name, "status", for_update=True)`,
  taken before the re-check, and the re-check acts on that locked value —
  not the doc loaded earlier. **CLOSED**, confirmed by re-audit.
- **P3-1 — existence oracle.** `frappe.get_doc` on a missing name raises
  `DoesNotExistError`; `frappe.has_permission(..., throw=True)` on an
  existing-but-unauthorized one raises `PermissionError` — distinguishable,
  and the sequential `CAP-`/`WF-`/`MTG-` names make enumeration cheap. All
  three whitelisted methods now go through `get_doc_for_action`, which
  raises the identical `PermissionError` with the identical message for
  both cases, so a caller cannot tell "does not exist" from "not allowed to
  touch." The permission checks it wraps were already correct and are
  unchanged. **CLOSED**, confirmed by re-audit.
- **P2-2 — TOCTOU in `create_todo_from_decision`, still open after the first
  fix.** Locking the *parent* row and then re-reading `row.todo` via a plain
  `doc.reload()` was not enough: under MariaDB/InnoDB REPEATABLE READ, a
  non-locking read taken after a lock can still be served from the
  transaction's pre-lock snapshot, so two concurrent callers could still
  both see `todo` empty. Fixed by locking the *exact row being raced*:
  `frappe.db.get_value("Meeting Decision", row_name, "todo",
  for_update=True)`. A `FOR UPDATE` read always returns the latest
  *committed* version, and the already-has-a-ToDo refusal is now driven by
  that value, not by the doc's in-memory child row. The parent write (`doc.
  save()`) still happens afterwards through the doc as before.
  **Concurrency guarantee, tested by code review, not by driving genuine
  concurrency**: `bench execute` runs one request per process on this
  bench, so there is no practical way to fire two overlapping calls in the
  test environment. `tests_runner.py`'s `_p22_lock_call_pattern_check`
  monkeypatches `frappe.db.get_value` to record the actual call a second
  concurrent-shaped call makes, and asserts it is exactly
  `get_value("Meeting Decision", row_name, "todo", for_update=True)` and
  that the refusal names the ToDo that call found — not merely that some
  `for_update=True` string appears before the insert in the source. If this
  ever needs a real test, it needs two separate DB connections issuing
  overlapping transactions, which the request-per-process test harness here
  cannot drive.
- **P3-2 — the processed pointer was only UI-read-only, and the first fix
  was incomplete.** `read_only` in the doctype JSON is a form hint, not
  server-side enforcement. The first fix only guarded *updates* once the
  *stored* status was already `Processed`/`Discarded`, which left two
  holes: (a) a document could be **inserted** already "born" `Processed`
  with a forged pointer, never touching `process_into_todo`; (b) an
  existing `Open` row's status could be direct-saved straight to
  `Processed` with a forged pointer, because the guard only fired once the
  stored status was already terminal. `QuickCapture.validate()` now runs
  one of two complete branches: on insert, `status` must be `Open` (or
  unset) and both `processed_into*` fields empty; on update, *any* change
  to `status`/`processed_into_doctype`/`processed_into` relative to the
  currently stored row is refused — not only when the stored status happens
  to already be terminal. Both branches are skipped only when
  `self.flags.via_process_method` is set, which happens only inside
  `process_into_todo`, immediately before its own `save()`. The
  `processed_into_doctype` allow-list (`"ToDo"`, `"Task"`, `"Waiting For"`,
  `"Meeting Record"`) is checked unconditionally, flag or not, so the
  flag-guarded path cannot be used to smuggle in an unlisted doctype. Other
  fields (e.g. `capture_text`) stay freely editable throughout.

  **`frappe.client.set_value` verdict**: the second-round audit raised, as a
  separate claim, that `frappe.client.set_value` (the REST-whitelisted
  endpoint) bypasses controller `validate()`. Checked against this
  container's actual installed source, `apps/frappe/frappe/client.py`
  (v16.31): `set_value()` does `doc = frappe.get_doc(doctype, name)` (line
  207 for a non-child doctype), `doc.update(values)` (line 208), then
  `doc.save()` (line 215) — `save()` is exactly what runs `validate()`, so
  **this claim is incorrect for `frappe.client.set_value`**. It is
  `frappe.db.set_value` (a raw `UPDATE`, not REST-whitelisted, not called
  anywhere in this app) that bypasses the controller — the same distinction
  `staff_credential.py`'s `verify()` in the sibling app documents for its
  own use of `frappe.db.set_value`. `tests_runner.py`'s
  `_set_value_bypass_check` confirms this on the live site: calling
  `frappe.client.set_value("Quick Capture", <processed capture>, "status",
  "Open")` is refused by the P3-2 guard above, exactly as a normal
  `doc.save()` would be.

**Known obstruction on the shared evaluation site**: `bench run-tests` for
`Waiting For` and `Meeting Record` fails during `IntegrationTestCase`'s own
fixture bootstrap (`setUpClass` infers the doctype from the test module's
name and calls `make_test_records`, which walks their Link fields — `Contact`
for Waiting For, `ToDo` via the decisions child table for Meeting Record —
and that walk reaches erpnext's Company/Fiscal Year test fixtures). On this
box a *real* `Fiscal Year 2025-2026` exists from concurrent ERPNext
configuration work happening on the same site, and it overlaps the synthetic
`_Test Fiscal Year 2025` erpnext's fixture tries to create — unrelated to
this app, and present with or without `--skip-before-tests` (that flag only
skips erpnext's own `before_tests` hook, not this per-doctype fixture walk).
`Quick Capture` has no such Link fields and runs cleanly under `run-tests`.
`hospital_ops/hospital_ops/tests_runner.py` re-checks the same invariants
(the original 15 plus one more for the new `discard()` method — 7 Quick
Capture + 5 Waiting For + 4 Meeting Record, each negative case paired with
its positive control — plus 16 more added across the two audit rounds: 3
for the P3-1 existence oracle, 3 for the first-round P3-2 guard, 2 for the
first-round P2 lock-order source check, 3 for the second-round P3-2 complete
state machine, 2 for the `frappe.client.set_value` verdict, and 3 for the
second-round P2-2 exact lock call pattern) with plain assertions instead of
`IntegrationTestCase`, which never triggers that walk, rolling back
regardless of outcome:

```bash
bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase2_tests
```

All 32 passed. The real `test_*.py` suites remain the primary tests and
should be re-run with `bench run-tests` once the shared site's Fiscal Year
conflict is resolved by whoever owns that configuration.

### Phase 3 — CSR

The largest custom build of the conversion: a port of the Next.js
application's `src/server/domain/csr.ts` (2,708 lines) and its
`csr_project_financials` view. Six doctypes, module `Hospital Ops`, all
`track_changes: 1`, `System Manager` only.

Every name is prefixed `CSR ` deliberately. `trust_compliance` is installed
on the same site and already owns `Fund`, `Fund Transfer`, `Trust Donation`
and `Grant Utilisation`; each name below was checked free before creation.

| Doctype | Naming | Kind | What it is |
| --- | --- | --- | --- |
| `CSR Funder` | `CSRF-#####` | normal | The organisation offering or giving money |
| `CSR Opportunity` | `CSRO-#####` | normal | The pipeline, before money exists |
| `CSR Project` | `CSRP-#####` | normal | The funded project, with a `tranches` child table |
| `CSR Tranche` | — | child | One expected instalment: date and amount only |
| `CSR Fund Event` | `CSRE-#####` | **submittable** | The money ledger |
| `CSR Reporting Obligation` | `CSRR-#####` | normal | What we owe the funder, and who checked it was delivered |

`CSR Funder` is deliberately **not** an ERPNext Customer: Customer drags
receivables accounting and an AR-ageing story that has nothing to do with a
sanction letter. This follows `trust_compliance`'s Trust Donor precedent.

§4.3 holds throughout: a funder and a project are organisations and
programmes. No individual beneficiary appears in any doctype, any field or
any test fixture.

#### The ledger, and why it cannot be cancelled

`CSR Fund Event` is submittable, and submission is what makes an entry count.
Frappe's submitted document supplies what the reference build's Postgres
`BEFORE UPDATE OR DELETE` triggers supplied: once submitted, the row cannot be
edited. What Frappe does not supply is a refusal to *cancel*, so the
controller adds one — **`before_cancel` and `on_cancel` both throw**.

A correction is a **reversal entry**, never a cancellation and never an edit.
Cancelling would silently remove the original from every figure and leave no
record that it had ever been recorded; the trail has to show the mistake and
the fix, not the corrected state alone. The refusal message names the
reversal kind to use and the event to point it at.

- **Only submitted events count.** Every sum filters `docstatus = 1`. A draft
  counts for nothing, including a draft sitting on a project that already has
  submitted events.
- **Direction lives in the `kind`, never in the sign.** `amount` must be
  greater than zero on all four kinds (`Receipt`, `Expenditure`,
  `Receipt Reversal`, `Expenditure Reversal`).
- **A reversal cannot exceed what remains of what it reverses.** `reverses`
  must name a *submitted* event of the matching kind on the *same* project,
  and the amount is checked against the original less the submitted reversals
  already pointing at it. Without this, a reversal larger than its original
  produces a negative figure that no cross-stage comparison catches — every
  comparison asks whether one figure *exceeds* another, and a negative one
  never does.

#### The overridable-warning pattern

Cross-stage comparisons are **warnings, not constraints**. Spending ahead of
a tranche and receiving above a sanction both genuinely happen; a database
constraint here would eventually force someone to enter a false figure. So:

1. Submitting refuses once, naming the exact figures — e.g.
   `Spent ₹2,50,000.00 against ₹2,00,000.00 received — the difference is
   being carried by the institution.` Nothing is written; the event stays a
   draft and the figures do not move.
2. To record it anyway the entry must carry **all three** of
   `override_confirmed`, a non-empty `override_reason`, and
   `override_acknowledges` set to the **exact warning string** the refusal
   issued (multiple warnings joined by ` | `).
3. Change the amount and the string changes, so a **stale acknowledgement is
   refused** — the confirmation is bound to the entry that produced it.

All three are enforced in the controller, not only in the form: a direct API
call must not be able to record an override with no reason. An override on an
entry that produces no warning is also refused — an override answering
nothing must not be recorded as though a warning had been weighed.

The warnings describe the **resulting state**, not the delta, so a project
already above its sanction keeps warning on every later entry. That is
deliberate and asserted in the suite, so it does not get "fixed" into a delta
check.

`preview_warnings(csr_project, kind, amount)` (whitelisted, read-only, no
lock) hands the desk the same acknowledgement text before a refusal has been
provoked.

Two states are **not** overridable, because they are decisions already taken
rather than figures that disagree: an `Expenditure` on a **Closed** project,
and *any* event on a **Cancelled** one. A Closed project still accepts a
correcting reversal.

#### Lock order

`before_submit` takes
`frappe.db.get_value("CSR Project", …, for_update=True)` **first** — that row
is the lock every submitter on the project contends for — and only then sums
the ledger. The sums are themselves `FOR UPDATE` reads, because under
MariaDB/InnoDB REPEATABLE READ a *non-locking* read after the lock is served
from the snapshot taken before the lock was granted, i.e. it would miss the
very concurrent expenditure the lock had just waited for. This is exactly the
hole the Phase 2 P2-2 re-audit found, applied here pre-emptively.
`convert_to_project` and `mark_verified` take the same shape on their own
rows, and each decides on what the *locked read* returned rather than on the
document loaded earlier.

Same limitation as Phase 2: genuine concurrency is not drivable from a
single-request-per-process bench, so the guarantee is asserted by
source-level checks on the call order (`_csr_lock_order_checks`).

#### Nothing is stored that can be computed

There is no `received`, `spent` or `balance` column anywhere, and the suite
asserts it against the real table columns (`DESCRIBE`), the way the reference
build asserted it against `information_schema`. A stored total is a dual
write, and a dual write eventually disagrees with its rows.

`hospital_ops/hospital_ops/csr_financials.py::totals_from_kind_rows` is the
**only** code that turns ledger rows into figures. Two callers share it:

- `CSR Project.get_project_financials(csr_project)` — whitelisted. Sanctioned,
  received, spent, balance, per-tranche expected-vs-received with derived
  overdue flags, and the obligations with derived overdue flags.
- the **CSR Project Financials** Script Report (module `Hospital Ops`, filters
  on funder and status) — the portfolio view, one grouped pass over the whole
  ledger.

One function, two callers, so the document view and the desk view are
incapable of showing different numbers. The reference build's lesson was a
closure threshold computed outside a transaction and recomputed inside it,
which let a record be written whose stored state disagreed with the check
that permitted it.

**Derived state, not status columns.** Tranche overdueness is computed at
read time (receipts applied in `expected_on` order against the cumulative
expectation; overdue = past its date and short). Obligation overdueness is
computed (`due_on` in the past, nothing submitted). A stored flag is wrong
the moment a backdated receipt or submission is entered, and then nobody
knows which of the two to believe.

**Money.** Frappe `Currency` (decimal) per the plan's Bucket 3 decision — the
reference build's bigint-paise representation is deliberately *not* ported.
Float traps are guarded by rounding every comparison and every returned
figure through `flt(x, 2)`. The rupee formatter in `csr_financials.format_inr`
is hand-written rather than `frappe.utils.fmt_money` on purpose: its output is
compared character-for-character against a stored acknowledgement, so it must
not depend on a site-level number-format setting that could change between
the refusal and the confirmation.

#### Evidence is verified by a person

`CSR Reporting Obligation.verified_by` / `verified_on` cannot be set on
insert and cannot be set by a direct save — `read_only` in the JSON is a UI
hint, and `validate()` is the actual guard (the Phase 2 P3-2 lesson).
`mark_verified(name, verified_by=None, verified_on=None)` is the only path:
it refuses when nothing has been submitted yet, refuses an unknown verifier,
and refuses a second verification on the strength of its locking read. A
verification date with nobody attached to it is refused outright — that is
precisely the "inferred from an upload" failure the rule exists to prevent.

#### Opportunities convert once

`convert_to_project(name, sanction_reference=None)` is whitelisted. It locks
its own row, refuses if `converted_to` is already set (naming the project),
refuses a stage other than `Sanctioned`, refuses a missing sanction figure,
creates the `CSR Project`, and stamps `converted_to`. `Declined` requires a
`decline_reason`.

#### Codex audit of Phase 3

Six findings: two P1s, both **disproven** against this container's actual
frappe v16 source and then probed live; four valid, all fixed.

- **P1-a — claimed: submitted events are deletable via `frappe.delete_doc`,
  because the doctype JSON grants delete, bypassing the cancel refusal.**
  **False.** `apps/frappe/frappe/model/delete_doc.py:289-297` refuses any
  submittable doctype whose `docstatus.is_submitted()` — *"Submitted Record
  cannot be deleted. You must Cancel it first"*, with `raise_exception=True`
  — and the check sits ahead of the `on_trash` hook. Because
  `before_cancel`/`on_cancel` throw, cancellation is impossible, so deletion
  of a submitted event is **transitively** impossible. Probed live: deleting
  a submitted event is refused and the event still counts; a **draft**
  deletes normally, which is correct, since a draft counts for nothing.
  Regardless of the verdict, `on_trash` now throws when `docstatus == 1` —
  one `if`, defence in depth, so the invariant lives in this app's code and
  not only in core's.
- **P1-b — claimed: `frappe.client.set_value` / `frappe.db.set_value` can
  mutate a submitted row's amount, kind or project.** **False for
  `frappe.client.set_value`**, which is the REST-whitelisted one:
  `client.py:207-215` ends in `doc.save()`; `document.py:597-598` routes a
  submitted save through `validate_update_after_submit()`; and
  `base_document.py:1270-1305` throws `frappe.UpdateAfterSubmitError` for any
  field changed without `allow_on_submit`. No field on `CSR Fund Event` has
  `allow_on_submit`. Probed live on amount, kind and `csr_project` — all
  three refused, the stored row untouched, with the same endpoint editing a
  **draft** freely as the positive control.

  **`frappe.db.set_value` genuinely does bypass all of it** — it is a raw
  `UPDATE` that runs no controller. It is **not** REST-whitelisted and is
  reachable only from server-side code or a bench console. No app-level guard
  can prevent it; this is the same class of residual as *"whoever holds the
  MariaDB root password can edit the table"*, and it is asserted in the suite
  so the residual stays visible rather than being assumed away. This app
  calls `frappe.db.set_value` nowhere in the CSR module.
- **P2-a — valid, fixed.** The project's status was read *before* the project
  lock and never re-read, so a Close or Cancel committed in the gap was
  invisible and a prohibited event could submit against a project that was
  closed by the time it landed. The status and the sanction figure now come
  out of the one locking `get_value` and are passed into the refusal, which
  no longer queries at all — a plain re-read after the lock would not have
  been enough, for the same REPEATABLE READ reason as P2-2 in Phase 2.
- **P2-b — valid, fixed.** The report listed projects with `frappe.get_all`,
  which sets `ignore_permissions` and skips permission query conditions and
  User Permissions alike. It now uses `frappe.get_list`. The child-table and
  event queries stay `get_all` deliberately: they are keyed to the project
  names `get_list` already permitted.
- **P3-a — valid, fixed.** Amounts with more than two decimal places were
  accepted, so `0.004` was a real submitted event that every total displayed
  as ₹0.00 — the ledger and the page disagreeing, with the page being the one
  anybody reads. `validate()` now refuses `amount != flt(amount, 2)`.
- **P3-b — valid, fixed.** Tranches sharing an `expected_on` allocated in
  arrival order, so which one carried the shortfall could change between two
  reads of unchanged data. The child `idx` is now the secondary sort key in
  both `tranche_states` and the report query, so ties resolve in document
  order.

#### Tests

```bash
bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase3_tests
```

**111 passed, 0 failed** on the live site (85 for the build, 26 more for the
audit findings), every negative carrying its positive control in the same
block, everything rolled back at the end whatever happens. `run_phase2_tests`
still reports 32 passed / 0 failed, and `bench run-tests --app hospital_ops
--doctype "Quick Capture"` still passes 5 of 5.

### Phase 4 — Research

A port of the Next.js application's `research.ts` study/milestone/ethics
slice — **done, 24-Aug-2026**. Three doctypes, module `Hospital Ops`, `System
Manager` only.

| Doctype | Naming | Kind | What it is |
| --- | --- | --- | --- |
| `Research Study` | `RSTU-#####` | normal | The study, administratively — title, investigator name, department, status, a `milestones` child table |
| `Research Study Milestone` | — | child | One milestone: description, due date, completed date |
| `Research Ethics Submission` | `RETH-#####` | normal | One submission to a committee and its decision |

**§4.3 is the sharpest rule here.** `principal_investigator` is free-text
`Data`, deliberately not a Link to any person/patient record. There is no
participant table anywhere in this module, and `data_boundary.py`'s
`find_participant_identifier_fields()` scans every `DocField` and `Custom
Field` on every doctype in module `Hospital Ops` for
`participant|patient|mrn|diagnos|consent|enrol|subject_id`, case-insensitive
— asserted at zero in the test suite, with a positive control proving the
scan itself works (an unfiltered pass matches the installed `healthcare`
app's `Patient` fields).

#### Milestones complete once, following the Phase 2 lesson exactly

`complete_milestone(name, row_name)` locks the **child row** itself
(`for_update=True` on `Research Study Milestone`, not the parent) before
deciding whether `completed_on` is already set — the Phase 2 P2-2 lesson: a
lock on the parent and a plain re-read of the child afterwards can still
serve a pre-lock snapshot under REPEATABLE READ. Completion is refused
outright on a `Terminated` or `Completed` study.

#### The ethics decision is one-shot; renewal is a new row

`record_decision(name, decision, decided_on=None, decision_reference=None,
valid_until=None, decision_note=None)` takes a locked read of its own
`decision` before deciding there is anything left to decide, refuses unless
still `Pending` (naming the decision and date already recorded), requires
`valid_until` on `Approved` (an approval that never expires is how renewals
get missed) and `decision_note` on `Rejected`.

`validate()` enforces the same state machine server-side — the Phase 2 P3-2
lesson that `read_only` in the JSON is a UI hint only:

- A submission cannot be **created** already decided.
- Once it exists, `decision`/`decided_on`/`decision_reference`/`valid_until`/
  `decision_note` cannot move by a direct save; only `record_decision` (via
  its own internal flag) may set them.

A renewal is a **new** `Research Ethics Submission` row, never an edit to the
old one — the same "every submission is a row" decision the reference
`research.ts` made, ported unchanged.

#### Ethics standing is derived, never stored

`research_ethics.ethics_standing(study, as_of=None)` is the single function
that turns a study's submission rows into one of four states — `approved`,
`pending`, `expired`, `none` — and nothing is stored. Precedence is the
point: **`pending` is checked before `expired`**, which is what makes the
renewal path work — a study whose old approval lapsed but which has since
filed a fresh submission reads `pending`, not `expired`-and-forgotten. Both
the whitelisted `get_study_standing(name)` (read-only) and the **Research
Ethics Register** report call this one function, so the study form and the
desk report cannot show different standings for the same study — the same
"one function, two callers" shape as `csr_financials.totals_from_kind_rows`.

The report lists studies with `frappe.get_list` (permission-aware — the
Phase 3 P2-b lesson) and shows, per study: standing, latest submission, valid
until, and days-to-expiry (negative = already expired).

`as_of` defaults to the **site's** timezone (`frappe.utils.today()`,
Asia/Kolkata here), not a per-workspace timezone the way the reference
`research.ts` computed it — acceptable for this single-site deployment, but
a future multi-timezone deployment would need to revisit it.

#### Milestone completion cannot be forged by a direct save

Codex's Phase 4 audit found that nothing stopped a caller from loading the
Research Study, setting a milestone row's `completed_on` directly and
calling `doc.save()` — the once-only rule and the Terminated/Completed
refusal both lived only in `complete_milestone`, the same class of gap as
Phase 2's P3-2. `ResearchStudy.validate()` now compares every milestone row
against the document's pre-save state (`get_doc_before_save()`) and refuses
any change to `completed_on` unless `self.flags.completing_milestone` is
set — a flag only `complete_milestone` sets. A study cannot be *created*
with a milestone already completed either. Adding or removing an
*incomplete* milestone stays unguarded, since editing the plan is normal
use; deleting a row that was already completed is refused outright, because
that erases completion history.

#### Tests

```bash
bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase4_tests
```

**32 passed, 0 failed** on the live site: milestone completion (once, twice,
wrong row, Terminated/Completed/Active study), the ethics decision one-shot
pattern and its state machine, the full standing lifecycle (none → pending →
approved → expired, then a fresh renewal reading pending again), report/
standing parity across a set of studies, and the §4.3 scan with its positive
control. `run_phase2_tests` (32/32) and `run_phase3_tests` (111/111) both
re-ran clean after this phase's migration, confirming no regression.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/hospital_ops
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
