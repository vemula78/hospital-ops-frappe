from __future__ import annotations

"""Research Study — administered, never enrolled.

**§4.3 first.** ``principal_investigator`` is free-text Data, deliberately not
a Link to any person or patient record: a study is tracked administratively
here (title, investigator name, department, ethics status), never who is
enrolled in it. There is no participant table anywhere in this module and no
field that could hold an identifier — see
``tests_runner.py::_research_participant_identifier_guard_check`` for the
scan that makes this a test rather than a hope.

Milestones follow the Phase 2 lesson exactly: the row being decided on is
locked with ``for_update=True`` *before* the completion is written, and the
UPDATE itself is conditioned on ``completed_on is null`` so a stale read
cannot silently overwrite an earlier completion.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.permissions import get_doc_for_action
from hospital_ops.hospital_ops.research_ethics import ethics_standing

#: Studies in these states are finished or stopped; nothing further is due.
CLOSED_STATUSES = ("Completed", "Terminated")


class ResearchStudy(Document):
    pass


@frappe.whitelist()
def complete_milestone(name: str, row_name: str) -> dict:
    """Marks one milestone row complete, today, once.

    The child row is locked (``for_update=True``) ahead of the read that
    decides whether it is already completed — the Phase 2 lesson: a plain
    read after some other lock is served from the pre-lock snapshot under
    REPEATABLE READ, so the lock has to sit on the row actually being decided
    on, not on the parent.
    """
    doc = get_doc_for_action("Research Study", name, ptype="write")

    if doc.status in CLOSED_STATUSES:
        frappe.throw(
            _(
                "{0} is {1}. Milestones on a finished or stopped study cannot be completed."
            ).format(doc.name, _(doc.status)),
            title=_("Research Study"),
        )

    row = next((m for m in doc.milestones if m.name == row_name), None)
    if row is None:
        frappe.throw(
            _("Milestone row {0} was not found on {1}.").format(row_name, name),
            title=_("Research Study"),
        )

    locked_completed_on = frappe.db.get_value(
        "Research Study Milestone", row_name, "completed_on", for_update=True
    )
    if locked_completed_on:
        frappe.throw(
            _("This milestone was already completed on {0}.").format(locked_completed_on),
            title=_("Research Study"),
        )

    row.completed_on = today()
    doc.save()

    return {"study": doc.name, "row_name": row_name, "completed_on": str(row.completed_on)}


@frappe.whitelist()
def get_study_standing(name: str) -> dict:
    """The derived ethics standing for one study (read-only, no lock)."""
    doc = get_doc_for_action("Research Study", name, ptype="read")
    return ethics_standing(doc.name)
