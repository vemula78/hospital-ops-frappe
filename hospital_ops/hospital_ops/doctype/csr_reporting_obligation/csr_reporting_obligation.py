from __future__ import annotations

"""What the hospital owes the funder, and who checked that it was delivered.

**Evidence is verified by a person, never inferred** (reference §9.4). An
upload is not verification and neither is a submitted date; someone has to
say "I looked at this". So the verification fields cannot be set on insert
and cannot be set by a direct save — only ``mark_verified`` sets them, once,
under a locking read of its own row.

**Overdue is derived** (``due_on`` in the past with nothing submitted) and is
stored nowhere. A stored flag would be wrong the moment a backdated
submission is entered.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.csr_financials import obligation_overdue
from hospital_ops.hospital_ops.permissions import get_doc_for_action

VERIFICATION_FIELDS = ("verified_by", "verified_on")


class CSRReportingObligation(Document):
    def validate(self) -> None:
        self._check_verification_pair()
        self._guard_verification_fields()

    def _check_verification_pair(self) -> None:
        # A verification date with nobody attached to it is exactly the
        # "inferred from an upload" failure the rule exists to prevent.
        if self.verified_on and not self.verified_by:
            frappe.throw(
                _("A verification date needs the person who verified it."),
                title=_("CSR Reporting Obligation"),
            )

    def _guard_verification_fields(self) -> None:
        """read_only in the JSON is a UI hint; this is the actual guard."""
        if self.flags.verifying:
            return

        if self.is_new():
            if self.verified_by or self.verified_on:
                frappe.throw(
                    _(
                        "An obligation cannot be created already verified. Create it, then "
                        "verify it with mark_verified() once someone has actually checked it."
                    ),
                    title=_("CSR Reporting Obligation"),
                )
            return

        stored = frappe.db.get_value(
            "CSR Reporting Obligation", self.name, list(VERIFICATION_FIELDS), as_dict=True
        )
        for field in VERIFICATION_FIELDS:
            current = self.get(field) or None
            previous = (stored.get(field) if stored else None) or None
            if str(current or "") != str(previous or ""):
                frappe.throw(
                    _(
                        "{0} is set by mark_verified() and cannot be changed by a direct save."
                    ).format(_(self.meta.get_label(field))),
                    title=_("CSR Reporting Obligation"),
                )

    @property
    def is_overdue(self) -> bool:
        return obligation_overdue(self.due_on, self.submitted_on)


@frappe.whitelist()
def mark_verified(name: str, verified_by: str | None = None, verified_on: str | None = None) -> dict:
    """Records that a named person checked this obligation was delivered.

    Refused twice over: nothing to verify until something was submitted, and
    never a second time. The locking read is what makes "never a second time"
    true under concurrency — a second caller blocks until the first commits,
    then reads the stamp the first one wrote.
    """
    doc = get_doc_for_action("CSR Reporting Obligation", name, ptype="write")

    already = frappe.db.get_value(
        "CSR Reporting Obligation", doc.name, ["verified_by", "verified_on"],
        as_dict=True, for_update=True,
    )
    if already and (already.verified_by or already.verified_on):
        frappe.throw(
            _("This obligation was already verified by {0} on {1}.").format(
                already.verified_by, already.verified_on
            ),
            title=_("CSR Reporting Obligation"),
        )

    if not doc.submitted_on:
        frappe.throw(
            _(
                "Nothing has been submitted against this obligation yet, so there is nothing "
                "to verify."
            ),
            title=_("CSR Reporting Obligation"),
        )

    verifier = verified_by or frappe.session.user
    if not frappe.db.exists("User", verifier):
        frappe.throw(
            _("{0} is not a user, so cannot be recorded as the verifier.").format(verifier),
            title=_("CSR Reporting Obligation"),
        )

    doc.flags.verifying = True
    doc.verified_by = verifier
    doc.verified_on = verified_on or today()
    doc.save()

    return {
        "name": doc.name,
        "verified_by": doc.verified_by,
        "verified_on": str(doc.verified_on),
    }


@frappe.whitelist()
def get_obligation_state(name: str, as_of: str | None = None) -> dict:
    """The derived view of one obligation. Nothing here is stored."""
    doc = get_doc_for_action("CSR Reporting Obligation", name, ptype="read")
    return {
        "name": doc.name,
        "csr_project": doc.csr_project,
        "due_on": str(doc.due_on),
        "submitted_on": str(doc.submitted_on) if doc.submitted_on else None,
        "verified_by": doc.verified_by,
        "verified_on": str(doc.verified_on) if doc.verified_on else None,
        "overdue": obligation_overdue(doc.due_on, doc.submitted_on, as_of),
        "verified": bool(doc.verified_by and doc.verified_on),
    }
