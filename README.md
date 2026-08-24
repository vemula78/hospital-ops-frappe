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
