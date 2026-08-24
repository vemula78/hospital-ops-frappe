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
float-rounding epsilon core uses.

**MUST NOT fire for stock items** — core already guards those, and a second,
differently-worded guard risks disagreeing with core's own tolerance the
moment the two are compared. **MUST cost nothing when there is nothing to
check** — this hook runs via ``doc_events`` for every Purchase Receipt in
every app on this bench, several times a day once real use starts, so a
receipt with no PO-linked row does zero queries before returning.
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


def guard_non_stock_over_receipt(doc, method=None) -> None:
    """``before_submit`` on Purchase Receipt (wired via ``hooks.py doc_events``)."""

    # Cheapest possible exit: no query at all when nothing on this document
    # references a Purchase Order line. This is the common case once real use
    # starts and most receipts have no PO at all, or are stock-item receipts
    # core already covers.
    po_item_names = {row.purchase_order_item for row in doc.items if row.purchase_order_item}
    if not po_item_names:
        return

    # This doc's own qty per PO Item row, summed across any rows that (oddly
    # but validly) repeat the same PO line twice on one receipt.
    this_doc_qty_by_po_item: dict[str, float] = {}
    item_code_by_po_item: dict[str, str] = {}
    for row in doc.items:
        if not row.purchase_order_item:
            continue
        this_doc_qty_by_po_item[row.purchase_order_item] = flt(
            this_doc_qty_by_po_item.get(row.purchase_order_item, 0.0)
        ) + flt(row.qty)
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
        # found (see csr_fund_event.py's docstring).
        already_received = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(pri.qty), 0)
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
