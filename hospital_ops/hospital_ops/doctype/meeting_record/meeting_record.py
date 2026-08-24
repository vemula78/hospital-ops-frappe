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

    row = next((d for d in doc.decisions if d.name == row_name), None)
    if row is None:
        frappe.throw(
            _("Decision row {0} was not found on {1}.").format(row_name, name),
            title=_("Meeting Record"),
        )

    # Lock the exact row being raced, not the parent (Codex re-audit,
    # P2-2): locking the parent and then re-reading `row.todo` via a plain
    # `doc.reload()` is not enough — under MariaDB/InnoDB REPEATABLE READ, a
    # non-locking read taken after the lock can still be served from the
    # transaction's pre-lock snapshot, so two concurrent callers could both
    # still see `todo` empty. A `FOR UPDATE` read always returns the latest
    # *committed* version and locks precisely the "Meeting Decision" row in
    # question, so a second concurrent call blocks here until the first
    # commits, then sees that commit's `todo` rather than a stale snapshot.
    locked_todo = frappe.db.get_value("Meeting Decision", row_name, "todo", for_update=True)
    if locked_todo:
        frappe.throw(
            _("A ToDo ({0}) already exists for this decision.").format(locked_todo),
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
