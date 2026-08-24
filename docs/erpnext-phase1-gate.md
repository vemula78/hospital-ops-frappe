# ERPNext Phase 1 gate — configuration only, no custom code

Run on 24-Aug-2026 against the evaluation instance **https://erp.sssihms.org**
(ERPNext v16.32.1 / Frappe v16.31, site `frontend`, MariaDB, Docker on the Azure VM).
Everything below was created through `bench --site frontend console` under a new
company; nothing outside that company was written to, and no `bench migrate` was run.

**All data is synthetic.** Fictional vendors, `example.test` addresses, no patient data,
no real staff names.

Phase 1 is the go/no-go gate defined in the conversion plan: *if configuring ERPNext's
Buying module does not visibly beat the procurement module already built and tested,
the rest of the plan will not pay for itself either.* The comparison below is against
`src/server/domain/procurement.ts` (3,237 lines) as built, not from memory.

---

## 1. What was configured

### Company and accounting base

| Thing | Value |
| --- | --- |
| Company | `SSSIHMS Whitefield Ops` (abbr `SWO`), INR, India |
| Chart of accounts | Standard with Numbers — **95 accounts created automatically** |
| Cost centres | `SSSIHMS Whitefield Ops - SWO`, `Main - SWO` (automatic) |
| Warehouses | `Stores - SWO`, `All Warehouses - SWO`, `Work In Progress - SWO`, `Finished Goods - SWO`, `Goods In Transit - SWO` (all automatic) |
| Fiscal year | `2026-2027` already existed on the site; nothing to create |

The "one Warehouse (stores)" in the brief did not need creating — company creation
supplies five.

### Master data

| Doctype | Records |
| --- | --- |
| Item Group | `Hospital Equipment & Consumables` |
| UOM | `Box` (the site had no `Box` UOM) |
| Asset Category | `Medical Equipment (Ops Eval)` — fixed asset `1710 - Capital Equipment - SWO`, accumulated depreciation `1780`, depreciation expense `5203`, CWIP `1790`. A separate `Medical Equipment` category belonging to the `trust_compliance` demo was left untouched. |
| Item | `SWO-DEFIB-001` Biphasic Defibrillator with AED mode — `is_fixed_asset=1`, `auto_create_assets=1` |
| Item | `SWO-ECGEL-001` ECG Electrodes, disposable (pack of 50) — consumable, UOM Box |
| Item | `SWO-DEFPAD-001` Defibrillator adult pads (pair) — consumable, UOM Nos |
| Supplier Group | `All Supplier Groups` → `Hospital Suppliers` (the site had **no** supplier groups at all) |
| Address Template | `India` (the site had none; `Address.on_update` refuses to save without one) |
| Supplier | `Medequip Traders (Synthetic)` — address `Medequip Traders Office-Billing`, contact `Anil Synthetic-Medequip Traders (Synthetic)`, `sales@medequip.example.test` |
| Supplier | `Sai Surgical Supplies (Synthetic)` — address `Sai Surgical Supplies Office-Billing`, contact `Bhavana Synthetic-Sai Surgical Supplies (Synthetic)`, `desk@saisurgical.example.test` |
| Location | `Cardiology OT` |

### The buying flow, one case end to end

| Step | Document | Result read back |
| --- | --- | --- |
| Material Request | `MAT-MR-2026-00001` "Defibrillator for Cardiology OT" — 2 × `SWO-DEFIB-001`, 4 × `SWO-DEFPAD-001` | submitted, status `Pending` |
| RFQ to both suppliers | `PUR-RFQ-2026-00001` | submitted, 2 supplier rows, 2 item rows (`send_email=0`, so nothing left the box) |
| Supplier Quotation A | `PUR-SQTN-2026-00001` Medequip — defib ₹4,25,000, pads ₹4,800 → **₹8,69,200** | submitted |
| Supplier Quotation B | `PUR-SQTN-2026-00002` Sai Surgical — defib ₹3,98,500, pads ₹5,600 → **₹8,19,400** | submitted |
| Comparison | `Supplier Quotation Comparison` query report | ran; flagged the cheapest supplier **per item** (`min: 1` on Sai Surgical for the defibrillator, on Medequip for the pads) |
| Purchase Order | `PUR-ORD-2026-00001` to Sai Surgical (cheaper overall), ₹8,19,400 | submitted, `To Receive and Bill`; MR moved to `Ordered`, `per_ordered` 100 % |
| Purchase Receipt 1 (partial) | `MAT-PRE-2026-00001`, 21-Aug — 1 defib + 2 pads | submitted; **PO `per_received` 50 %, still `To Receive and Bill`**; MR status `Partially Received` |
| Purchase Receipt 2 (remainder) | `MAT-PRE-2026-00002`, 23-Aug — 1 defib + 2 pads | submitted; **PO `per_received` 100 %, status `To Bill`**; MR status `Received` |

The prices were deliberately crossed — Sai Surgical is cheaper on the capital item and
dearer on the consumable — so the comparison report had to do real work rather than pick
one winner on every line.

### Assets

Both defibrillator units were created automatically by the Purchase Receipts, one per
receipt, each tied back to the receipt that produced it:

| Asset | From | Amount | Location | Status |
| --- | --- | --- | --- | --- |
| `ACC-ASS-2026-00002` | `MAT-PRE-2026-00001` | ₹3,98,500 | Cardiology OT | Submitted |
| `ACC-ASS-2026-00003` | `MAT-PRE-2026-00002` | ₹3,98,500 | Cardiology OT | Submitted |

Custodian left empty (no synthetic Employee was created). Submitting each Asset also
generated an `Asset Movement` of purpose *Receipt* — `ACC-ASM-2026-00159`,
`ACC-ASM-2026-00160` — without being asked.

### Projects

`PROJ-0004` "OT Signage Refresh (Synthetic)", 01-Sep → 31-Oct-2026, percent-complete
method *Task Completion*.

| Task | Milestone | Depends on |
| --- | --- | --- |
| `TASK-2026-00001` Survey existing OT signage and photograph gaps | — | — |
| `TASK-2026-00002` Approve signage design pack | **yes** | `TASK-2026-00001` |
| `TASK-2026-00003` Install and verify signage in Cardiology OT | — | `TASK-2026-00002` |

Roll-up verified: 0 % → **33.33 %** after task 1 → **66.67 %** after task 2. Closing
`TASK-2026-00003` while its predecessor was open was refused:
*"Cannot complete task TASK-2026-00003 as its dependant task TASK-2026-00002 are not
completed / cancelled."*

### Maintenance sketch

`Asset Maintenance Team` **Biomedical Engineering (Synthetic)**; `Asset Maintenance`
record **`ACC-ASS-2026-00002`** against the first defibrillator with two scheduled tasks
(Quarterly preventive service and battery test; Annual output energy calibration — AMC
vendor). Two `Asset Maintenance Log` rows were spawned automatically
(`ACC-AML-2026-00001`, `ACC-AML-2026-00002`).

---

## 2. Feature-by-feature against `procurement.ts`

| Capability in the Next.js module | ERPNext, natively | Verdict |
| --- | --- | --- |
| **Multi-quotation comparison** (`buildComparison`, PRO-003/004) | `Supplier Quotation Comparison` report tabulated both quotes and marked the cheapest supplier per item. | **Partially met** — it compares unit price only; there is no document-level "who is cheapest overall", no landed/tax-inclusive normalisation, and no TCO. |
| **Comparison refuses to mix specification versions** (`issueSpecification` freeze) | Nothing. There is no specification object; the RFQ description is free text and can be edited per quotation. | **Not met** — the guarantee that two quotes answer the same frozen text does not exist. |
| **TCO over an explicit horizon, `not_computable` when a vendor quoted no AMC rate** (`computeTco`, PRO-004) | Nothing. AMC/warranty are not quotation fields. | **Not met** — the single most decision-relevant number in a capital purchase has no home. |
| **Comparable total computed, vendor's stated total kept beside it, difference reported** (§20.3) | ERPNext stores `grand_total` and there is no "vendor said X" field to disagree with it. | **Not met by design** — the plan already accepts stored aggregates (Bucket 3, item 3). |
| **Frozen comparison snapshot an approval can cite** (`takeComparisonSnapshot`) | Nothing equivalent. A submitted PO is immutable, but the comparison behind it is not captured. | **Not met** — recoverable with a custom doctype, not with configuration. |
| **Partial deliveries, progress derived not stored** (`recordDelivery`, §10.2) | Two Purchase Receipts against one PO; `per_received` went 0 → 50 → 100 and PO status `To Receive and Bill` → `To Bill`. Per-item `received_qty` maintained on both PO and MR. | **Met, and better** — the roll-up reaches the Material Request too, which our module does not do. |
| **Over-receipt refusal** (`exceeds_order`) | Refused for the **stock** item: *"This document is over limit by Qty 3.0 for item SWO-DEFPAD-001."* **Accepted silently for the fixed-asset item** — 3 more units against 1 outstanding took the PO to `received_qty` 4 of 2 ordered, `per_received` 66.67 %, no warning. | **Partially met — and this is the serious one.** See §3. |
| **Discrepancy note mandatory when accepted < delivered** | Purchase Receipt has separate `received_qty` / `accepted_qty` / `rejected_qty` and a rejected warehouse, but no mandatory reason text. | **Partially met** — the quantities are modelled better than ours; the *reason* is not enforced. |
| **Quotation-vs-PO mismatch warning** | Off by default here (`Buying Settings.maintain_same_rate = 0`); a PO priced 25 % above the quotation it was made from was accepted silently. With the flag on it refused precisely: *"Row #1: Rate must be same as Supplier Quotation: PUR-SQTN-2026-00002 (498125.0 / 398500.0)"*. | **Partially met** — the check exists and is accurate, but it is a global on/off (`Stop` or `Warn`), not our refuse-then-accept-with-a-mandatory-reason-and-its-own-audit-action pattern. |
| **Stage tracking, derived** (`RequestProgress.stage`, ten states) | Status fields on each document (`Pending`/`Ordered`/`Partially Received`/`Received`, `To Receive and Bill`/`To Bill`) plus the `Procurement Tracker` report, which joins MR → PO → receipt on one line. | **Partially met** — the states exist and are maintained, but they are *stored* per document, not one derived stage per request; there is no `specifying` / `evaluating` / `awaiting_approval` / `commissioning` notion at all. |
| **Ageing by stage** (`stageEnteredOn`, `ageingDays`, PRO-009) | Not present. `Purchase Order Analysis` gives pending qty and amount; `Procurement Tracker` gives expected vs actual delivery date; `Delayed Order Report` flags late orders. No days-in-current-stage anywhere. | **Not met** — the closest native artefact is delivery lateness, which is a different question. |
| **Commissioning readiness gate: cannot complete while a required step lacks evidence, unless a waiver with a reason exists** (PRO-008, §10.5) | Nothing. Asset goes `Draft` → `Submitted` on an `available_for_use_date` and no checklist. | **Not met** — this is genuinely custom work, as the plan anticipated. |
| **Asset created from the delivery, with custodian and location** | Automatic, one Asset per received unit, linked to the Purchase Receipt, priced from it, placed at `Cardiology OT`, with an `Asset Movement` audit row. | **Met, and better** — we build this by hand; ERPNext does it on submit. |
| **Nothing here orders anything** (§4.2 — the official system stays authoritative) | ERPNext *is* an ordering system. A submitted PO is a real purchase order with GL consequences. | **Different by design** — see §3; this is a governance question, not a defect. |
| Contacts / Suppliers / Addresses (`people.ts`) | Native, with the link-table pattern letting one contact serve several parties. | **Met** |
| Projects, Tasks, milestones, dependencies, percent-complete (`projects.ts` + `tasks.ts`, 1,517 lines) | Native. Dependency enforcement fired without configuration. | **Met** |
| Service contracts / maintenance schedule (`maintenance.ts`, 1,910 lines) | `Asset Maintenance` + `Asset Maintenance Team` + auto-generated `Asset Maintenance Log`. | **Met at the sketch level** — not probed deeply; periodicity, next-due generation and completion logging all exist. |

---

## 3. What ERPNext could not do, or did awkwardly

Recorded in the order they were hit. Several of these cost real time.

**1. Over-receipt is not refused on fixed-asset items.** The `Over Receipt/Delivery
Allowance` in Stock Settings is `0`, and it correctly refused an over-receipt of the
consumable. The same receipt over-receiving the *defibrillator* — 3 units against 1
outstanding — submitted without a murmur and left the PO reading `received_qty` 4 of 2
ordered. The guard evidently lives in the stock-ledger path, which a non-stock
fixed-asset line never enters. **Every capital purchase in this workspace is a
fixed-asset item**, so this is exactly the case `recordDelivery`'s `exceeds_order` check
was written for, and ERPNext does not cover it. It is fixable (a `before_submit` hook on
Purchase Receipt), but that is custom code in a phase whose premise is configuration only.

**2. A fixed-asset Item must be a non-stock item.** `Fixed Asset Item must be a non-stock
item.` The consequence is not cosmetic: capital equipment never appears in the stock
ledger, so "where is it and how many are there" is answered by the Asset register and
"what did we consume" by the stock ledger, and the two do not reconcile with each other.
Item 1 above is a direct consequence of this split.

**3. Assets are not created from a receipt unless a non-obvious Item checkbox is set.**
With `is_fixed_asset=1`, `asset_category`, `asset_naming_series` and `asset_location` all
correctly filled, the first Purchase Receipt submitted cleanly and created **no Asset and
no warning**. The missing field was `auto_create_assets` on the Item. A silent no-op on
the step that matters most is the worst possible failure mode; the receipt had to be
cancelled, the Item corrected, and the receipt redone.

**4. Purchase Order accepts a future date; Purchase Receipt refuses one.** A PO dated
27-Aug submitted happily on 24-Aug. Every attempt to receive against it then failed —
first *"Posting Date cannot be future date"*, then *"Posting Date 23-08-2026 cannot be
before Purchase Order date"*. A PO dated ahead is simply unreceivable until that date
arrives, with no hint at PO time that this will happen. The PO had to be cancelled,
deleted and recreated at 19-Aug.

**5. Accounting is forced on a workflow that is not accounting.** Two Purchase Receipts
produced **8 live GL entries** across `1410 Stock In Hand`, `1710 Capital Equipment`,
`2210 Stock Received But Not Billed` and `2211 Asset Received But Not Billed`, plus 2
Stock Ledger Entries. Nobody in this workspace wants a hospital-side capital ledger — the
official finance system is authoritative — and yet the buying flow cannot be used without
generating one, complete with a *Stock Received But Not Billed* liability that will sit
open forever because no Purchase Invoice is ever going to be raised. Every PO status is
also a *billing* status (`To Receive and Bill`, `To Bill`), so the workspace permanently
displays an accounting state it has no intention of clearing.

**6. It orders things.** `procurement.ts` was deliberately built so that no code path can
place an order (§4.2). ERPNext's PO is a real purchase order with a print format, a
supplier portal and email delivery attached. Nothing forces it to be sent, but the
guardrail has changed from *impossible* to *don't press that*. Worth a deliberate
decision, not a discovery.

**7. Missing default records on this site.** The instance had no Supplier Groups at all
and no Address Template, so the first Supplier insert and the first Address insert both
failed with errors that name a Setup page rather than the missing record. That is this
site's history rather than ERPNext's fault, but it is what a fresh company on this
instance actually encounters.

**8. `Asset Maintenance` is named by the asset.** The maintenance record's `name` is
`ACC-ASS-2026-00002` — identical to the Asset's. Harmless until you are reading a link
and cannot tell which doctype you are in.

**9. No specification object anywhere.** RFQ carries free-text `message_for_supplier` and
per-item descriptions, editable after issue. The entire PRO-002 discipline — a frozen
specification version, quotations that name the version they answer, a comparison that
refuses to mix versions — has no configuration-level home. This is the largest single
piece of `procurement.ts` that does not survive.

**10. The comparison report is thinner than it looks.** It is a flat per-item price list
with a `min` marker. It does not total by supplier, does not normalise tax treatment,
does not carry warranty or AMC, and cannot express "cheaper on the capital item, dearer
on the consumable" as a decision. The actual choice in this case — Sai Surgical, on the
overall total — was made by reading two `grand_total` values, not by the report.

---

## 4. Recommendation

**Go — conditionally, and with the conditions written down rather than assumed.**

What Phase 1 actually proved: the *transactional spine* of procurement is real and free.
Material Request → RFQ → two Supplier Quotations → Purchase Order → two partial Purchase
Receipts → two Assets at a Location with movement records, with per-item quantity
roll-up propagating correctly all the way back to the Material Request, was configured in
one session with no custom code. Projects, tasks, dependencies and percent-complete came
for nothing. Asset Maintenance is a credible home for service contracts. That is a
straight deletion of the delivery-tracking, asset-creation, contacts and projects code —
and it is better than what we built, because the roll-up reaches further and the Asset
Movement trail was not something we had at all.

What it also proved: the *judgement* half of `procurement.ts` is not in ERPNext at any
setting. Frozen specifications, TCO with an honest `not_computable`, a comparison
snapshot an approval can cite, commissioning readiness with evidence and waivers, ageing
by derived stage — none of these are configuration gaps that a checkbox closes. They are
Phase 2+ custom doctypes, and the plan's estimate that procurement is "the single biggest
win" is right about volume and optimistic about completeness. Call it 60 % of the module
absorbed, not 100 %.

Two conditions on the go:

1. **The over-receipt hole on fixed-asset items must be closed before any real use.** It
   is silent, it affects exactly the class of purchase this workspace exists for, and it
   needs a `before_submit` hook — i.e. the "no custom code" premise does not survive
   first contact.
2. **Decide explicitly what to do about the forced accounting.** Either accept a
   permanently unclearable *Received But Not Billed* balance as cosmetic noise, or agree
   that Purchase Invoices will be entered to close it, or disable perpetual inventory for
   this company. Leaving it undecided means the workspace shows accounting figures nobody
   owns.

If both are acceptable, proceed to Phase 2. If the second one is not — if a hospital-side
capital ledger that disagrees with the official finance system is unacceptable at any
size — then stop here, because it is not removable from the Buying flow.

**Two sentences:** ERPNext configuration replaced the delivery-tracking, asset-register,
contact and project halves of `procurement.ts` outright and did several things better
than the built version, at the cost of an accounting ledger nobody asked for and a silent
over-receipt hole on exactly the item class that matters. The evaluative half —
specifications, TCO, comparison snapshots, commissioning evidence, stage ageing — is
absent and must be rebuilt, so the recommendation is **go**, with the over-receipt hook
and the accounting decision as explicit gate conditions rather than Phase 2 surprises.

---

## 5. Phase 1 evidence

All figures read back from the live site on 24-Aug-2026, filtered to company
`SSSIHMS Whitefield Ops` (or, where the doctype has no company field, to the synthetic
records created here).

```
$ bench --site frontend console   # counts via frappe.db.count(dt, {'company': CO})

COUNTS
{
 "Material Request": 1,
 "Request for Quotation": 1,
 "Supplier Quotation": 2,
 "Purchase Order": 1,
 "Purchase Receipt": 2,
 "Asset": 2,
 "Asset Movement": 2,
 "Asset Maintenance": 1,
 "Project": 1,
 "Warehouse": 5,
 "Task (project PROJ-0004)": 3,
 "Supplier (synthetic)": 2,
 "Item (SWO-)": 3,
 "Contact (example.test)": 2,
 "Address": 2,
 "Asset Maintenance Log": 2,
 "GL Entry": 16,
 "Stock Ledger Entry": 4
}

DOCS
Material Request      [{'name': 'MAT-MR-2026-00001',  'status': 'Received'}]
Request for Quotation [{'name': 'PUR-RFQ-2026-00001', 'status': 'Submitted'}]
Supplier Quotation    [{'name': 'PUR-SQTN-2026-00001','status': 'Submitted'},
                       {'name': 'PUR-SQTN-2026-00002','status': 'Submitted'}]
Purchase Order        [{'name': 'PUR-ORD-2026-00001', 'status': 'To Bill'}]
Purchase Receipt      [{'name': 'MAT-PRE-2026-00001', 'status': 'To Bill'},
                       {'name': 'MAT-PRE-2026-00002', 'status': 'To Bill'}]
Asset                 [{'name': 'ACC-ASS-2026-00002', 'status': 'Submitted'},
                       {'name': 'ACC-ASS-2026-00003', 'status': 'Submitted'}]
Project               [{'name': 'PROJ-0004',          'status': 'Open'}]
Asset Maintenance     [{'name': 'ACC-ASS-2026-00002', 'asset_name': 'ACC-ASS-2026-00002',
                        'maintenance_team': 'Biomedical Engineering (Synthetic)'}]
Tasks  [{'name': 'TASK-2026-00003', 'status': 'Open',      'is_milestone': 0},
        {'name': 'TASK-2026-00002', 'status': 'Completed', 'is_milestone': 1},
        {'name': 'TASK-2026-00001', 'status': 'Completed', 'is_milestone': 0}]
```

The `GL Entry: 16` / `Stock Ledger Entry: 4` counts include cancelled rows from the
receipt that had to be redone (friction item 3). Filtering `is_cancelled = 0`:

```
live GL entries: 8
 MAT-PRE-2026-00001  1410 - Stock In Hand - SWO                       Dr    11,200
 MAT-PRE-2026-00001  1710 - Capital Equipment - SWO                   Dr 3,98,500
 MAT-PRE-2026-00001  2210 - Stock Received But Not Billed - SWO       Cr    11,200
 MAT-PRE-2026-00001  2211 - Asset Received But Not Billed - SWO       Cr 3,98,500
 MAT-PRE-2026-00002  1410 - Stock In Hand - SWO                       Dr    11,200
 MAT-PRE-2026-00002  1710 - Capital Equipment - SWO                   Dr 3,98,500
 MAT-PRE-2026-00002  2210 - Stock Received But Not Billed - SWO       Cr    11,200
 MAT-PRE-2026-00002  2211 - Asset Received But Not Billed - SWO       Cr 3,98,500
live SLE: 2
```

Partial-receipt progression, read back after each submit:

```
after Purchase Order        : po.status 'To Receive and Bill'  per_received   0.0   mr.status 'Ordered'
after MAT-PRE-2026-00001    : po.status 'To Receive and Bill'  per_received  50.0   mr.status 'Partially Received'
                              SWO-DEFIB-001 ordered 2.0 received 1.0
                              SWO-DEFPAD-001 ordered 4.0 received 2.0
after MAT-PRE-2026-00002    : po.status 'To Bill'              per_received 100.0   mr.status 'Received'
                              SWO-DEFIB-001 ordered 2.0 received 2.0
                              SWO-DEFPAD-001 ordered 4.0 received 4.0
```

Over-receipt probes (each rolled back, nothing committed):

```
over-receipt asset item (3 more vs 1 outstanding) -> ACCEPTED  PO now [('SWO-DEFIB-001', 2.0, 4.0), ...] per_received 66.67
over-receipt stock item (5 more vs 2 outstanding) -> REFUSED: This document is over limit by Qty 3.0
                                                     for item SWO-DEFPAD-001. ... update "Over Receipt/Delivery
                                                     Allowance" in Stock Settings or the Item.
exact remainder                                   -> ACCEPTED  per_received 100.0
```

Quotation-vs-PO rate probe (`Buying Settings.maintain_same_rate` toggled in a rolled-back
transaction; the committed value is still `0`):

```
maintain_same_rate = 0  ->  PO priced 25% above the quotation it was made from: ACCEPTED silently, grand_total 10,24,250
maintain_same_rate = 1  ->  REFUSED: ['Row #1: Rate must be same as Supplier Quotation:
                             PUR-SQTN-2026-00002 (498125.0 / 398500.0)',
                             'Row #2: ... (7000.0 / 5600.0)']
restored maintain_same_rate = 0
```

Project roll-up:

```
after 1/3 complete            -> 33.33  Open
after 2/3 complete (milestone)-> 66.67  Open
closing TASK-2026-00003 early -> Cannot complete task TASK-2026-00003 as its dependant
                                 task TASK-2026-00002 are not completed / cancelled.
```

Supplier Quotation Comparison, `Group by Item` (abridged):

```
SWO-DEFPAD-001  Sai Surgical    4 Nos  22,400   5,600 /unit
                Medequip        4 Nos  19,200   4,800 /unit   <- min
SWO-DEFIB-001   Sai Surgical    2 Nos 7,97,000 3,98,500 /unit  <- min
                Medequip        2 Nos 8,50,000 4,25,000 /unit
```

### Site hygiene

No other company's records were created, modified or deleted. `Buying Settings` and
`Stock Settings` are unchanged (the one toggle was rolled back and re-read as `0`). No
`bench migrate` was run, no container was restarted, and no email left the instance
(`send_email=0` on both RFQ supplier rows). Three documents were cancelled and recreated
during the run — one Purchase Order (date) and one Purchase Receipt (missing
`auto_create_assets`) — and their cancelled GL rows remain in the ledger as
`is_cancelled = 1`, which is why the raw count is 16 and the live count is 8.
