from __future__ import annotations

"""Closes the Phase 1 gate condition: ERPNext silently accepts over-receipt
on FIXED-ASSET (non-stock) Purchase Order lines.

Evidence (``docs/erpnext-phase1-gate.md`` §3.1): 4 received against 2 ordered
on a fixed-asset item, submitted without a murmur, while the same over-receipt
on a *stock* item was correctly refused. The reason, read from this
container's own ``erpnext`` checkout
(``erpnext/controllers/status_updater.py::fetch_items_with_pending_qty``,
lines ~392-412): the query that finds "qty received exceeds qty ordered"
candidates joins ``Item`` and filters ``is_stock_item == 1`` before the
allowance check ever runs. A non-stock line — every capital purchase in this
workspace, because ``Fixed Asset Item must be a non-stock item`` — never
enters that query at all, so it never gets that check, allowance or refusal.

This hook is the ``before_submit`` guard that query's WHERE clause is
missing, for non-stock items only. It deliberately mirrors core's tolerance
logic (``erpnext/controllers/status_updater.py::get_allowance_for`` and
``check_overflow_with_allowance``) rather than inventing a stricter rule:
the item-level ``over_delivery_receipt_allowance`` overrides the
``Stock Settings`` global one when set, and the refusal fires only once the
overflow percentage exceeds the allowance by more than the same ``0.01``
float-rounding epsilon core uses. An authorised over-receipt is mirrored too
(``role_allowed_to_over_deliver_receive``) — see ``_ALLOWED_OVER_RECEIVE_ROLE``.

**MUST NOT fire for stock items** — core already guards those, and a second,
differently-worded guard risks disagreeing with core's own tolerance the
moment the two are compared. **MUST cost nothing when there is nothing to
check** — this hook runs via ``doc_events`` for every Purchase Receipt in
every app on this bench, several times a day once real use starts, so a
receipt with no PO-linked row does zero queries before returning.

**Which quantity field this compares (Codex audit, High, fixed).** The first
cut summed ``Purchase Receipt Item.qty``, which is labelled *Accepted
Quantity* (``purchase_receipt_item.json``: ``"qty": {"label": "Accepted
Quantity"}``) — it excludes rejected units. Core's own over-receipt bookkeeping
does not: ``purchase_receipt.py``'s ``status_updater`` config
(``PurchaseReceipt.__init__``, ~lines 162-171) feeds the Purchase Order Item
accumulator (``target_dt: "Purchase Order Item"``, ``target_field:
"received_qty"``) from ``source_field: "received_qty"`` on Purchase Receipt
Item, compared against ``target_ref_field: "qty"`` (the *ordered* qty) on the
Purchase Order Item — i.e. core compares **Received Quantity** (accepted +
rejected), not Accepted Quantity, against what was ordered. Summing ``qty``
here undercounted a receipt with rejected units: 4 received as 2 accepted + 2
rejected against 2 ordered would have read as "2 vs 2", missing the
over-receipt entirely. This hook now sums ``received_qty`` to match.

**Is ``received_qty`` reliably populated on a non-stock row by the time this
hook runs? Verified, not assumed.**
``buying_controller.py::validate_accepted_rejected_qty`` runs for *every* row
of *every* Purchase Receipt regardless of ``is_stock_item`` (`for d in
self.get("items")`, no stock-item guard) — it is called from
``BuyingController.validate()`` (~line 60), which every Purchase Receipt
runs via ``super().validate()`` before ``before_submit`` ever fires (Frappe's
own document lifecycle runs the full ``validate()`` chain ahead of
``before_submit`` hooks). That method **always** leaves ``received_qty``
populated and internally consistent before this hook sees the document: if
the caller left it at its default of 0, it is set to
``flt(qty) + flt(rejected_qty)``; if the caller supplied a value that
disagrees with ``qty + rejected_qty``, core throws its own
``QtyMismatchError`` first ("Received Qty must be equal to Accepted +
Rejected Qty"), so submission never reaches this hook with an inconsistent
row. This means ``received_qty`` on an accepted-only, no-rejection row (every
scenario ``run_phase6_tests`` builds) equals ``qty`` exactly — confirmed by
``_over_receipt_guard_checks``' explicit assertion, not inferred — and the
defensive fallback below (``received_qty or (qty + rejected_qty)``) exists
only as belt-and-braces for a row this app cannot observe being reached any
other way, mirroring core's own formula rather than inventing one.
"""

import frappe
from frappe import _
from frappe.utils import flt

#: Distinguishes a refusal raised by this hook from one raised by core's own
#: over-receipt guard (mirrors the marker pattern ``on_trash`` already uses
#: elsewhere in this app to prove which code raised what — see
#: ``tests_runner.py``'s stock-item probe, which asserts this string's
#: *absence* rather than assuming which guard fired).
GUARD_MARKER = "[hospital_ops over-receipt guard]"

#: The 0.01 float-rounding epsilon core's own ``check_overflow_with_allowance``
#: applies before treating an overflow percentage as real. Kept identical so
#: this hook does not refuse a receipt core's own tolerance would have passed.
OVERFLOW_EPSILON = 0.01


def _received_qty(row) -> float:
    """The Received Quantity (accepted + rejected) core itself compares
    against the ordered qty — see the module docstring for the file:line
    evidence and why the fallback is defensive rather than load-bearing."""
    received = flt(row.received_qty)
    if received:
        return received
    return flt(row.qty) + flt(row.rejected_qty)


def guard_non_stock_over_receipt(doc, method=None) -> None:
    """``before_submit`` on Purchase Receipt (wired via ``hooks.py doc_events``)."""

    # Cheapest possible exit: no query at all when nothing on this document
    # references a Purchase Order line. This is the common case once real use
    # starts and most receipts have no PO at all, or are stock-item receipts
    # core already covers.
    po_item_names = {row.purchase_order_item for row in doc.items if row.purchase_order_item}
    if not po_item_names:
        return

    # This doc's own received qty per PO Item row, summed across any rows
    # that (oddly but validly) repeat the same PO line twice on one receipt.
    this_doc_qty_by_po_item: dict[str, float] = {}
    item_code_by_po_item: dict[str, str] = {}
    for row in doc.items:
        if not row.purchase_order_item:
            continue
        this_doc_qty_by_po_item[row.purchase_order_item] = flt(
            this_doc_qty_by_po_item.get(row.purchase_order_item, 0.0)
        ) + _received_qty(row)
        item_code_by_po_item[row.purchase_order_item] = row.item_code

    # One query for every item code referenced, to find which are non-stock.
    # Batched rather than per-row: this is the one query this hook allows
    # itself to spend on a document that has PO-linked rows at all.
    item_codes = list({code for code in item_code_by_po_item.values() if code})
    if not item_codes:
        return
    stock_flags = frappe.get_all(
        "Item",
        filters={"item_code": ["in", item_codes]},
        fields=["item_code", "is_stock_item", "over_delivery_receipt_allowance"],
    )
    stock_flag_by_item = {row.item_code: row for row in stock_flags}

    non_stock_po_items = [
        po_item
        for po_item, item_code in item_code_by_po_item.items()
        if not stock_flag_by_item.get(item_code, frappe._dict()).get("is_stock_item")
    ]
    if not non_stock_po_items:
        return

    global_allowance = flt(
        frappe.get_single_value("Stock Settings", "over_delivery_receipt_allowance") or 0
    )
    # Mirrors core's check_overflow_with_allowance: a user holding this
    # configured role may exceed the allowance, with a warning rather than a
    # refusal (status_updater.py::warn_about_bypassing_with_role).
    allowed_over_receive_role = frappe.get_single_value(
        "Stock Settings", "role_allowed_to_over_deliver_receive"
    )
    user_may_bypass = bool(
        allowed_over_receive_role and allowed_over_receive_role in frappe.get_roles()
    )

    for po_item in non_stock_po_items:
        item_code = item_code_by_po_item[po_item]
        this_doc_qty = flt(this_doc_qty_by_po_item[po_item])

        # Lock the PO row first, per house discipline (csr_fund_event.py's
        # "for_update read comes first" pattern): every submitter of a
        # receipt against the same PO line contends for this lock, so the
        # sum taken after it reflects every receipt that has already
        # committed, not a pre-lock snapshot.
        po_row = frappe.db.get_value(
            "Purchase Order Item",
            po_item,
            ["qty", "parent", "item_code"],
            as_dict=True,
            for_update=True,
        )
        if not po_row:
            # The row named on this receipt no longer exists on the PO
            # (should not happen through the UI, but a direct API caller
            # could try) — nothing to compare against, so there is nothing
            # this guard can refuse.
            continue

        ordered_qty = flt(po_row.qty)
        if ordered_qty <= 0:
            continue

        # Post-lock read of everything already received against this PO
        # line from OTHER submitted receipts. FOR UPDATE here too: MariaDB's
        # default REPEATABLE READ would otherwise serve this from the
        # snapshot opened before the PO row lock was granted, which is
        # exactly the concurrent-receipt hole the CSR ledger's own re-audit
        # found (see csr_fund_event.py's docstring). Summed as
        # received_qty (accepted + rejected), matching core — see the
        # module docstring.
        already_received = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(pri.received_qty), 0)
                FROM `tabPurchase Receipt Item` pri
                INNER JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
                WHERE pri.purchase_order_item = %s
                  AND pr.docstatus = 1
                  AND pr.name != %s
                FOR UPDATE
                """,
                (po_item, doc.name),
            )[0][0]
        )

        total_after_this_receipt = flt(already_received + this_doc_qty)

        item_allowance = flt(
            stock_flag_by_item.get(item_code, frappe._dict()).get(
                "over_delivery_receipt_allowance"
            )
            or 0
        )
        allowance = item_allowance if item_allowance else global_allowance

        overflow_percent = (
            flt((total_after_this_receipt - ordered_qty) / ordered_qty * 100)
            if ordered_qty
            else 0.0
        )

        if overflow_percent - allowance > OVERFLOW_EPSILON:
            max_allowed = flt(ordered_qty * (100 + allowance) / 100)

            if user_may_bypass:
                # Mirrors status_updater.py::warn_about_bypassing_with_role —
                # a warning, not a refusal, for a user holding the configured
                # role. Nothing here writes anything; the document proceeds.
                frappe.msgprint(
                    _(
                        "{0} Item {1} on {2}: received quantity {3} exceeds the allowed {4}, "
                        "ignored because you hold the {5} role (Stock Settings' "
                        "role_allowed_to_over_deliver_receive)."
                    ).format(
                        GUARD_MARKER,
                        item_code,
                        po_row.parent,
                        total_after_this_receipt,
                        max_allowed,
                        allowed_over_receive_role,
                    ),
                    indicator="orange",
                    alert=True,
                )
                continue

            frappe.throw(
                _(
                    "{0} Item {1} on {2}: ordered {3}, already received {4}, this receipt "
                    "adds {5} — total {6} exceeds the allowed {7}{8}. Non-stock (fixed-asset) "
                    "lines do not pass through ERPNext's own stock-ledger over-receipt check "
                    "(erpnext-phase1-gate.md §3.1), so this app enforces it here instead."
                ).format(
                    GUARD_MARKER,
                    item_code,
                    po_row.parent,
                    ordered_qty,
                    already_received,
                    this_doc_qty,
                    total_after_this_receipt,
                    max_allowed,
                    _(" (allowance {0}%)").format(allowance) if allowance else "",
                ),
                title=_("Over Receipt"),
            )
