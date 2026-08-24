from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class MeetingRecord(Document):
    pass


@frappe.whitelist()
def create_todo_from_decision(name: str, row_name: str) -> dict:
    """Creates a ToDo from one decision row and stamps its name back onto the
    row, atomically — a decision either gets a ToDo linked to it, or the call
    fails and nothing changes.
    """
    doc = frappe.get_doc("Meeting Record", name)
    frappe.has_permission(doctype="Meeting Record", ptype="write", doc=doc, throw=True)

    row = next((d for d in doc.decisions if d.name == row_name), None)
    if row is None:
        frappe.throw(
            _("Decision row {0} was not found on {1}.").format(row_name, name),
            title=_("Meeting Record"),
        )

    if row.todo:
        frappe.throw(
            _("A ToDo ({0}) already exists for this decision.").format(row.todo),
            title=_("Meeting Record"),
        )

    todo = frappe.get_doc(
        {
            "doctype": "ToDo",
            "description": row.decision,
            "date": row.due_on,
            "reference_type": "Meeting Record",
            "reference_name": doc.name,
        }
    ).insert()

    row.todo = todo.name
    doc.save()

    return {"todo": todo.name, "meeting": doc.name, "row_name": row_name}
