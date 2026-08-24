from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class QuickCapture(Document):
    def validate(self) -> None:
        # A capture that has already produced something keeps its pointer:
        # nothing here may clear processed_into/processed_into_doctype once set,
        # otherwise the record that made this Processed loses its own trail.
        pass


@frappe.whitelist()
def process_into_todo(name: str, description: str | None = None) -> dict:
    """Turns a capture into a ToDo, in one call (CAP-004 in the source app).

    Processing and marking the capture Processed happen together so the pair
    can never be left half-done: a caller either gets both, or neither (an
    exception here leaves the whole request rolled back).
    """
    doc = frappe.get_doc("Quick Capture", name)
    frappe.has_permission(doctype="Quick Capture", ptype="write", doc=doc, throw=True)

    if doc.status != "Open":
        frappe.throw(
            _("Only an Open capture can be processed; this one is {0}.").format(doc.status),
            title=_("Quick Capture"),
        )

    todo = frappe.get_doc(
        {
            "doctype": "ToDo",
            "description": description or doc.capture_text,
            "reference_type": "Quick Capture",
            "reference_name": doc.name,
        }
    ).insert()

    doc.status = "Processed"
    doc.processed_into_doctype = "ToDo"
    doc.processed_into = todo.name
    doc.save()

    return {"todo": todo.name, "capture": doc.name}
