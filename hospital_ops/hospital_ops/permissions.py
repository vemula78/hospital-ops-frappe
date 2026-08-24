from __future__ import annotations

"""Shared permission helper for Phase 2 whitelisted methods (Codex audit,
finding P3-1).

``frappe.get_doc(doctype, name)`` raises ``DoesNotExistError`` for a missing
name, and ``frappe.has_permission(..., throw=True)`` raises
``PermissionError`` for an existing-but-unauthorized one. The two exceptions
are distinguishable, which turns "does this name exist" into a cheap
existence oracle against the sequential naming series used here
(``CAP-00001``, ``WF-00001``, ``MTG-00001``: enumerable by counting up).

``get_doc_for_action`` collapses both cases into the identical
``PermissionError`` with the identical message, so a caller cannot tell a
missing document from one they are not allowed to touch.
"""

import frappe
from frappe import _
from frappe.model.document import Document


def get_doc_for_action(doctype: str, name: str, ptype: str = "write") -> Document:
    message = _("You do not have permission to access this {0}.").format(doctype)

    try:
        doc = frappe.get_doc(doctype, name)
    except frappe.DoesNotExistError:
        frappe.throw(message, frappe.PermissionError)
        raise  # unreachable; frappe.throw raises, this satisfies type checkers

    if not frappe.has_permission(doctype=doctype, ptype=ptype, doc=doc):
        frappe.throw(message, frappe.PermissionError)

    return doc
