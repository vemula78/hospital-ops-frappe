from __future__ import annotations

"""Research Ethics Submission — one submission, one decision, taken once.

Ported from ``recordEthicsDecision`` in the reference ``research.ts``. The
decision is **one-shot**: renewal is a new submission row, never an edit to
this one, so the whole record is a period with a start and (once decided) an
expiry rather than a mutable status. Two guards make that true:

- ``validate()`` enforces the state machine server-side (the Phase 2 P3-2
  lesson: ``read_only`` in the JSON is a UI hint only). A document cannot be
  *born* already decided, and once it exists, ``decision``/``decided_on``/
  ``decision_reference``/``valid_until``/``decision_note`` cannot move by a
  direct save — only ``record_decision`` (via its internal flag) may set them.
- ``record_decision`` itself takes a locked read of its own ``decision``
  before deciding whether there is anything left to decide, so two concurrent
  decisions on the same submission cannot both read Pending and both commit.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.permissions import get_doc_for_action

#: Fields only ``record_decision`` may set.
DECIDED_FIELDS = ("decision", "decided_on", "decision_reference", "valid_until", "decision_note")


class ResearchEthicsSubmission(Document):
    def validate(self) -> None:
        self._guard_decision_fields()

    def _guard_decision_fields(self) -> None:
        if self.flags.deciding:
            return

        if self.is_new():
            if self.decision and self.decision != "Pending":
                frappe.throw(
                    _(
                        "A submission cannot be created already decided. Create it Pending, "
                        "then decide it with record_decision() once the committee has actually "
                        "decided."
                    ),
                    title=_("Research Ethics Submission"),
                )
            if self.decided_on or self.decision_reference or self.valid_until or self.decision_note:
                frappe.throw(
                    _(
                        "A new submission cannot carry a decided date, reference, expiry or "
                        "decision note — those are set only by record_decision()."
                    ),
                    title=_("Research Ethics Submission"),
                )
            return

        stored = frappe.db.get_value(
            "Research Ethics Submission", self.name, list(DECIDED_FIELDS), as_dict=True
        )
        for field in DECIDED_FIELDS:
            current = self.get(field) or None
            previous = (stored.get(field) if stored else None) or None
            if str(current or "") != str(previous or ""):
                frappe.throw(
                    _(
                        "{0} is set by record_decision() and cannot be changed by a direct save."
                    ).format(_(self.meta.get_label(field))),
                    title=_("Research Ethics Submission"),
                )


@frappe.whitelist()
def record_decision(
    name: str,
    decision: str,
    decided_on: str | None = None,
    decision_reference: str | None = None,
    valid_until: str | None = None,
    decision_note: str | None = None,
) -> dict:
    """Records the committee's decision on one submission, once.

    Locked before anything is read, so two concurrent decisions on the same
    submission serialise rather than both reading Pending and both
    committing — the second caller's own locked read sees what the first
    caller already wrote.
    """
    doc = get_doc_for_action("Research Ethics Submission", name, ptype="write")

    if decision not in ("Approved", "Rejected"):
        frappe.throw(
            _("{0} is not a decision that can be recorded.").format(decision),
            title=_("Research Ethics Submission"),
        )

    locked = frappe.db.get_value(
        "Research Ethics Submission",
        doc.name,
        ["decision", "decided_on"],
        as_dict=True,
        for_update=True,
    )
    if locked and locked.decision != "Pending":
        frappe.throw(
            _("Already decided: {0} on {1}.").format(_(locked.decision), locked.decided_on),
            title=_("Research Ethics Submission"),
        )

    if decision == "Approved" and not valid_until:
        frappe.throw(
            _(
                "An Approved decision requires Valid Until. An approval that never expires is "
                "how renewals get missed."
            ),
            title=_("Research Ethics Submission"),
        )
    if decision == "Rejected" and not (decision_note or "").strip():
        frappe.throw(
            _("A Rejected decision requires a Decision Note."),
            title=_("Research Ethics Submission"),
        )

    doc.flags.deciding = True
    doc.decision = decision
    doc.decided_on = decided_on or today()
    doc.decision_reference = decision_reference
    doc.valid_until = valid_until if decision == "Approved" else None
    doc.decision_note = decision_note
    doc.save()

    return {
        "name": doc.name,
        "study": doc.study,
        "decision": doc.decision,
        "decided_on": str(doc.decided_on),
        "valid_until": str(doc.valid_until) if doc.valid_until else None,
    }
