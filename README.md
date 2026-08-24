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

Every whitelisted method re-checks `frappe.has_permission(..., doc=doc,
throw=True)` on the loaded document rather than trusting the doctype-level
grant alone. Server-side tests (`test_*.py` per doctype, using
`frappe.tests.IntegrationTestCase` — Frappe v16 renamed `FrappeTestCase`)
cover both the negative case and its positive control for each invariant.

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
`hospital_ops/hospital_ops/tests_runner.py` re-checks the same 15 invariants
(6 Quick Capture + 5 Waiting For + 4 Meeting Record assertions, each negative
case paired with its positive control) with plain assertions instead of
`IntegrationTestCase`, which never triggers that walk, rolling back
regardless of outcome:

```bash
bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase2_tests
```

All 15 passed. The real `test_*.py` suites remain the primary tests and
should be re-run with `bench run-tests` once the shared site's Fiscal Year
conflict is resolved by whoever owns that configuration.

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
