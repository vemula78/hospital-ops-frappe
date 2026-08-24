from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from hospital_ops.hospital_ops.permissions import get_doc_for_action


class MeetingRecord(Document):
    pass


@frappe.whitelist()
def create_todo_from_decision(name: str, row_name: str) -> dict:
    """Creates a ToDo from one decision row and stamps its name back onto the
    row, atomically — a decision either gets a ToDo linked to it, or the call
    fails and nothing changes.
    """
    doc = get_doc_for_action("Meeting Record", name, ptype="write")

    # Lock the parent row before reading the decision row's todo field below
    # (Codex audit, P2-2): without this, two concurrent calls for the same
    # row can both read an empty `todo`, both insert a ToDo, and both stamp
    # the row — leaving one of the two ToDos orphaned. The read below takes
    # the row lock; a second concurrent call blocks here until the first
    # call's transaction commits, and the reload afterwards then sees that
    # commit's `todo` rather than the stale value read before the lock.
    frappe.db.get_value("Meeting Record", name, "modified", for_update=True)
    doc.reload()

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
