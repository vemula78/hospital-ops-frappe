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

**That locking is only load-bearing through ``complete_milestone`` itself**
(Codex audit finding, High). Nothing stopped a caller from loading the
Research Study, setting a milestone row's ``completed_on`` directly and
calling ``doc.save()`` — the once-only rule and the Terminated/Completed
refusal both live in ``complete_milestone``, and a direct parent save never
goes near it. Same class of defect as Phase 2's P3-2: ``read_only`` in the
child doctype's JSON is a UI hint, not a guard. ``validate()`` is the actual
guard: it compares every milestone row's ``completed_on`` against the
document's own pre-save state (``get_doc_before_save()``) and refuses any
change unless ``self.flags.completing_milestone`` is set — a flag only
``complete_milestone`` sets. Editing the plan (adding or removing an
*incomplete* milestone) stays unguarded, because that is normal use;
deleting a row that was already completed is refused outright, because that
would erase completion history rather than edit a plan.
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
    def validate(self) -> None:
        self._guard_milestone_completion()

    def _guard_milestone_completion(self) -> None:
        """``completed_on`` moves only through ``complete_milestone``.

        On insert there is no "before" document, so every row must simply
        arrive with an empty ``completed_on`` — a study cannot be born with
        completed milestones. On update, every *surviving* row is compared
        against the value it held immediately before this save; any row that
        disappeared is checked too, because a completed row that vanishes has
        had its history erased just as surely as one whose date was edited.
        """
        if self.flags.completing_milestone:
            return

        before = self.get_doc_before_save()

        if before is None:
            for row in self.milestones:
                if row.completed_on:
                    frappe.throw(
                        _(
                            "A study cannot be created with a milestone already completed. "
                            "Milestone completion is set only by complete_milestone()."
                        ),
                        title=_("Research Study"),
                    )
            return

        before_by_name = {row.name: row for row in (before.milestones or [])}

        for row in self.milestones:
            prior = before_by_name.get(row.name)
            prior_completed_on = (prior.completed_on if prior else None) or None
            current_completed_on = row.completed_on or None
            if str(current_completed_on or "") != str(prior_completed_on or ""):
                frappe.throw(
                    _(
                        "Milestone completion is set only by complete_milestone() and "
                        "cannot be changed by a direct save."
                    ),
                    title=_("Research Study"),
                )

        current_names = {row.name for row in self.milestones}
        for prior in before.milestones or []:
            if prior.name not in current_names and prior.completed_on:
                frappe.throw(
                    _(
                        "Milestone {0} was completed on {1} and cannot be deleted — that "
                        "would erase the completion history rather than edit the plan."
                    ).format(prior.name, prior.completed_on),
                    title=_("Research Study"),
                )


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
    doc.flags.completing_milestone = True
    doc.save()

    return {"study": doc.name, "row_name": row_name, "completed_on": str(row.completed_on)}


@frappe.whitelist()
def get_study_standing(name: str) -> dict:
    """The derived ethics standing for one study (read-only, no lock)."""
    doc = get_doc_for_action("Research Study", name, ptype="read")
    return ethics_standing(doc.name)
