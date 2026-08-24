# Independent audit brief — hospital_ops

Hand this to a fresh session. It is self-contained; do not assume any prior context,
and **do not take the claims below as fact — they are what was asserted, and your job
is to test them.**

## What exists

`hospital_ops`, a custom Frappe v16 app, built over six phases as a conversion of a
Next.js/PostgreSQL hospital administration workspace to ERPNext.

- **Repo (source of truth):** `~/Documents/hospital-ops-frappe`, pushed to
  `github.com/vemula78/hospital-ops-frappe` (public). 32 commits on `main`.
- **Reference implementation being ported (READ-ONLY, do not modify):**
  `~/Documents/Productivity app` — a Next.js/Drizzle app, 26 domain modules, 91 tables,
  1,074 tests. Its `CLAUDE.md` section "Rules that came from real defects" is the
  behavioural contract the port was supposed to honour.
- **Live instance:** `https://erp.sssihms.org`, site `frontend`, ERPNext v16.32.1 /
  Frappe v16.31, Docker on an Azure VM. Credentials and infrastructure notes are in
  `~/Documents/trust-compliance-demo-docs/` (do not commit or echo them).

Modules delivered: Quick Capture / Waiting For / Meeting Record (Phase 2); CSR funder,
opportunity, project, submittable fund-event ledger, obligations (Phase 3); Research
studies and ethics submissions (Phase 4); signage, web pages, software UAT (Phase 5);
Weekly Review report, dashboard, notification fixtures, and a Purchase Receipt
over-receipt guard (Phase 6).

## Access

```
ssh -i ~/Downloads/sssihms-web-vm2023_key.pem -p 2222 -o ServerAliveInterval=60 azureuser@20.219.253.136
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 bench --site frontend <cmd>
```

## Hard constraints — read twice

1. **This VM also serves the hospital's public WordPress site** (`sssihms.org`,
   `whitefield.sssihms.org`). Never reboot it, never `docker compose down`, never touch
   Apache or the `db` container.
2. **Do not swap the container image.** A rebuilt image `trust-compliance:hrms-v5` exists
   on the VM but the swap is deliberately on hold because people may be testing. Leave
   the stack on `hrms-v4`.
3. If you must restart containers, restart `backend` then **immediately** `frontend`
   (frontend's nginx caches backend's IP; backend alone causes 502s). Prefer not to.
4. **Other people and other agent sessions are working on this site.** 16 apps are
   installed. Touch nothing outside `hospital_ops`. In particular do not disturb
   `sssihms_password_vault` (a credential store) or `trust_compliance`.
5. All test data must be synthetic and rolled back. No patient data, ever.

## What has already been done — so don't just repeat it

Six independent static-analysis audits (Codex CLI, read-only) ran across the phases:
17 findings raised, 15 fixed, 2 disproven with Frappe core source cited. Those passes
covered: whitelisted-method permissions, SQL injection, TOCTOU/locking order, submittable
immutability, state machines on the insert path, fixture scoping, report permission
filtering, and reversal-shape validation.

**The gap is that all of it was static review plus tests written by the same agents who
wrote the code.** That is where you should concentrate.

## Your brief — verify the verification

### 1. Mutation-test the suite (highest value)

There are ~402 self-reported passing checks
(`bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase2_tests`,
and `..._phase3_`, `4`, `5`, `6`). They are claimed to be rollback-always with a positive
control on every negative.

Break the code deliberately, one invariant at a time, and confirm the suite goes red.
Suggested mutations: remove the `for_update=True` from a locked read; invert a `>=` to `>`
in the website publication gate; delete the `override_acknowledges` equality check; drop
the `docstatus = 1` filter from a ledger sum; remove the design-immutability guard; make
`ethics_standing` return `approved` unconditionally. **A mutation that does not turn the
suite red is a hole in the tests, and is the finding.** Restore each mutation before the
next (work on a scratch copy in the container, never commit).

### 2. Drive real concurrency

Every phase asserts its locking with *source-order checks*, not by running two
overlapping transactions — this was disclosed as a known limitation each time. Actually
test it. Two simultaneous requests (two HTTP calls, or two `bench console` sessions with
explicit transactions) against: the same CSR expenditure at the overspend boundary; two
`add_design` calls on one sign; two `record_decision` calls on one ethics submission; two
Purchase Receipts over-receiving the same PO line. Confirm exactly one wins each time.

### 3. Test as a non-Administrator

Everything was tested as Administrator, and Frappe special-cases Administrator to hold
every role. Create a restricted user and probe: can it read `CSR Fund Event` rows or
`Weekly Review` output it should not? Do the whitelisted methods leak record existence
through distinguishable errors? Does the report's `get_list` actually filter? Delete the
user afterwards.

### 4. Behaviour against the reference, not against the tests

Pick the invariants in `~/Documents/Productivity app/CLAUDE.md` and check the Frappe port
honours them in *behaviour*: no stored aggregates; corrections are reversals not edits;
derived state rather than status columns; evidence verified by a person; no default on a
classifying field. Where the port deliberately diverged (money is Frappe Currency, not
bigint paise; the audit hash-chain was dropped for Frappe's `Version`) confirm the
divergence is documented rather than accidental.

### 5. The Purchase Receipt guard — blast radius

`hospital_ops` hooks `before_submit` on `Purchase Receipt`, so it runs for **every app's**
purchase receipts on this shared site. Try hard to make it refuse a legitimate receipt:
returns, amendments, multi-line POs, UOM conversions, rejected quantities, the configured
over-receipt allowance and the authorized-role bypass. A false positive here breaks other
people's work, which is worse than the hole it closes.

### 6. Docs vs reality

`README.md`, `docs/operations.md`, `docs/erpnext-conversion-plan.md`,
`docs/erpnext-phase1-gate.md`. Do the claimed tallies match a real run? Does the
documented deploy procedure actually work as written? Is anything claimed closed that is
not?

## Deliverable

For each finding: severity (P1/P2/P3), file:line, the exact triggering input or sequence,
why the existing tests miss it, and your confidence. Report everything you find, including
low-severity and uncertain items — filtering happens later, not by you. Where you checked
something and it genuinely holds, say so in one line so the coverage is visible.

If you disprove a claim made in the repo's own docs or commit messages, say so explicitly
with evidence — two earlier audit findings were themselves wrong, and being able to show
that is part of the job.
