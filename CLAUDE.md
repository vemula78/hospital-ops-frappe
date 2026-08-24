# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`hospital_ops` — a custom Frappe v16 app for SSSIHMS Whitefield, built as an ERPNext
conversion of a Next.js/PostgreSQL hospital administration workspace. It covers the
work ERPNext has no answer for: CSR fund tracking, research ethics, signage, website
publication, software UAT, and a GTD capture/delegation core.

**The reference implementation is `~/Documents/Productivity app`** (Next.js/Drizzle,
26 domain modules, 91 tables). Treat it as read-only. Its `CLAUDE.md` section "Rules
that came from real defects" is the behavioural contract this port honours — read it
before changing an invariant, because most of them were bought with a real bug.

`docs/erpnext-conversion-plan.md` is the scope document: what ERPNext absorbs, what
gets rebuilt here, and what was deliberately dropped (the audit hash-chain, bigint
paise, offline capture). `docs/erpnext-phase1-gate.md` records what ERPNext could
*not* do natively, with evidence.

**Data boundary (§4.3), enforced by a test, not a convention:** no patient data, no
research-participant identifiers, no individual CSR beneficiary evidence.
`data_boundary.py` scans every DocField and Custom Field in this module for
participant-shaped names and must return zero.

## The layout rule — read before moving any file

```
hospital_ops/                  ← package: hooks.py, modules.txt, patches/, config/,
                                 fixtures/, public/, templates/, www/
  hospital_ops/                ← MODULE dir "Hospital Ops": doctype/, report/,
                                 and every helper module
```

`frappe.model.sync.sync_for` does `get_module(app + "." + module)` and scans
`<that dir>/doctype/`. **Doctypes must live in the module dir.** Everything in the
code is imported as `hospital_ops.hospital_ops.X`, which only resolves with this shape.

This was got wrong twice. A flattening that left `doctype/` at package level with an
empty module dir made `sync_for` find nothing for module "Hospital Ops", and a single
`bench migrate` **dropped all 23 DocType records as orphaned**. Before that, the
running container only worked because a two-level deploy extract had duplicated the
whole package inside the module dir, which is what the imports had been resolving to.
If `docs/operations.md` or any older note tells you to extract a tarball twice, it is
stale — that workaround existed to feed the duplicate.

Verify a layout change with a real editable build before deploying:

```bash
python3 -m venv /tmp/fc && /tmp/fc/bin/pip install -q flit_core
/tmp/fc/bin/python -c "import flit_core.buildapi as b,tempfile; print(b.build_editable(tempfile.mkdtemp()))"
```

A clean `bench get-app` of this repo must work — the container is fed pre-built trees,
so packaging defects hide there indefinitely.

## The deployment target is shared, and one part of it is a hospital's public website

Live at `https://erp.sssihms.org`, site `frontend`, Frappe v16.31 / ERPNext v16.32.1,
Docker (`~/frappe_docker/pwd.yml`) on an Azure VM.

```bash
ssh -i ~/Downloads/sssihms-web-vm2023_key.pem -p 2222 -o ServerAliveInterval=60 azureuser@20.219.253.136
sudo docker exec -u frappe -w /home/frappe/frappe-bench frappe_docker-backend-1 bench --site frontend <cmd>
```

- **Never reboot the VM, never `docker compose down`, never touch Apache or the `db`
  container.** The same box serves `sssihms.org` and `whitefield.sssihms.org`.
- 17 apps are installed and other people (and other agent sessions) work here. Touch
  nothing outside `hospital_ops` except the sanctioned `Purchase Receipt` hook.
- Restart order is load-bearing: `backend` first, then `frontend` **immediately** —
  frontend's nginx caches backend's container IP, so backend alone yields 502s.
- Long-lived gunicorn workers never re-read an editable install. After any code change
  or `install-app`, restart or every HTTP request 500s with `ModuleNotFoundError`
  while every `bench console`/`execute` check passes. **Always verify over real HTTP,
  not only the console.**

## Commands

```bash
# Deploy from the repo ROOT (a tarball built from the wrong cwd is silently near-empty)
git archive --format=tar --prefix=hospital_ops/ HEAD | gzip > /tmp/ho.tar.gz
scp -P 2222 -i ~/Downloads/sssihms-web-vm2023_key.pem /tmp/ho.tar.gz azureuser@20.219.253.136:/tmp/
# then, per container (backend queue-short queue-long scheduler websocket):
sudo docker cp /tmp/ho.tar.gz frappe_docker-$c-1:/tmp/
sudo docker exec -u frappe -w /home/frappe/frappe-bench/apps frappe_docker-$c-1 sh -c \
  'rm -rf hospital_ops && tar -xzf /tmp/ho.tar.gz && cd /home/frappe/frappe-bench && env/bin/pip install -q -e apps/hospital_ops'
```

| Task | Command |
| --- | --- |
| Run one phase suite | `bench --site frontend execute hospital_ops.hospital_ops.tests_runner.run_phase3_tests` |
| Full battery | phases 2–6; expect **402 checks, 0 failures** |
| Apply schema changes | `bench --site <site> migrate --skip-search-index` |
| Clear caches | `bench --site <site> clear-cache` |
| Health after deploy | `curl -s https://erp.sssihms.org/api/method/ping` → `pong`, `/login` → 200 |

`tests_runner.py` is a hand-rolled harness, not `bench run-tests`, for two reasons:
`IntegrationTestCase` walks Link fields into ERPNext's Company/Fiscal Year fixtures and
collides with the real ones on a shared site, and ERPNext's `before_tests` bootstraps a
fiscal year that aborts the run. **Never run `bench run-tests` against `frontend`.** Use
`testspv.local` for clean-install checks; note its Phase 4 data-boundary *positive
control* fails there legitimately, because no healthcare app exists to match.

Every check rolls back regardless of outcome. Every negative assertion carries its
positive control in the same block — a refusal test that passes because nothing
happened at all is the failure mode these guard against.

## Architecture

**Derived state, never stored status columns.** Sign status, web-page publication
readiness, ethics standing, tranche overdueness and UAT coverage are all computed at
read time from their events (`build_publish.py`, `research_ethics.py`,
`csr_financials.py`). A stored status is wrong the moment a backdated record arrives.
Deriving sign status also deleted a whole writer the reference needed — a supersede has
no event of its own to move status backwards, so the reference had to hand-downgrade it.

**One derivation function, two callers.** A report and its API method must call the same
function (`csr_financials.totals_from_kind_rows`, `tranche_states`, `ethics_standing`,
`sign_readiness`, `missing_for_publication`, `uat_coverage`), so the desk and the API
cannot disagree. Weekly Review reuses all of them rather than re-deriving.

**Append-only ledgers are submittable doctypes.** `CSR Fund Event` and
`Hospital Sign Event`: submitted rows are immutable, `before_cancel`/`on_cancel`/`on_trash`
all refuse, and a correction is a reversal *entry*. Direction lives in a `kind` field,
never in the sign of an amount, and a reversal cannot exceed what remains of its target.
No aggregate is ever stored — a test reads `DESCRIBE` to assert no `received`/`spent`
column exists.

**Locked reads must feed the decision.** Take `frappe.db.get_value(..., for_update=True)`
**first**, then make the decision from that value — pass it as a parameter so a stale one
cannot be supplied by accident (`_check_against_locked_project`). Under MariaDB
REPEATABLE READ a plain read *after* a lock is still served from the pre-lock snapshot,
so `doc.reload()` does not make it safe; lock the exact row being raced, including child
rows. This was found three times in three phases.

**State machines belong in `validate()`, covering the insert path.** `read_only` in a
doctype JSON is a UI hint only. Guard direct saves by comparing against
`get_doc_before_save()` and permit the sanctioned transition only under an internal flag
(`self.flags.via_process_method`) set inside the whitelisted method. Insert-born
already-terminal records must be refused too.

**Overridable warnings, not constraints.** Where a cross-stage comparison genuinely
happens in real life (spending above receipts), refuse once naming the exact figures,
then accept a confirmation carrying a reason *and the exact warning string it answers*,
so a changed amount invalidates a stale acknowledgement. The warning string must be
built deterministically (a local `format_inr`, not a site-configurable formatter).

**Permissions and listings.** Every whitelisted method goes through
`permissions.get_doc_for_action`, which collapses missing-record and unauthorized into
one identical error so record names cannot be enumerated. Reports list their root
records with `frappe.get_list` (permission-aware); `frappe.get_all` is acceptable only
for child/related rows already keyed to permitted parents, and says so in a comment.

**The `Purchase Receipt` hook** (`purchase_receipt_guard.py`) is the one thing this app
wires into another app's documents, closing an ERPNext hole where over-receipt is
silently accepted on non-stock fixed-asset lines. It runs for every app's receipts on
this site, so: zero queries when no row carries a `purchase_order_item`, it sums
`received_qty` against PO `qty` (core's own bookkeeping — not `qty`, which is
accepted-only), and it mirrors core's allowance tolerance and authorized-role bypass. A
false positive here breaks other people's work.

## Durability

The app is baked into `trust-compliance:hrms-v5` (mirrored in ACR). Anything **not** in
that image lives only in a container's writable layer and dies on recreate — currently
`sssihms_password_vault` and `nabh_qms`. The entrypoint also **rewrites
`sites/apps.txt` from the image on start**, so such apps must be re-registered, not just
re-copied. `bench get-app` fails for private repos inside the containers (no
credentials); ship a `git archive` of a known commit instead.

## Auditing

`docs/independent-audit-brief.md` is a self-contained brief for a fresh auditor. Six
Codex passes covered the static ground (17 findings, 15 fixed, 2 disproven with core
source cited). The known remaining gap is that concurrency is asserted by source-order
inspection rather than by driving two overlapping transactions, and every suite was
written by the agent that wrote the code it tests.
