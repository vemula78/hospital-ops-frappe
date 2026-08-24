from __future__ import annotations

"""The pipeline before the money exists.

An opportunity becomes a CSR Project exactly once. ``convert_to_project``
takes a locking read of its own row first, so two concurrent conversions
cannot both find ``converted_to`` empty and both create a project — the same
read-then-write race that Phase 2's ``process_into_todo`` guards against, and
the same one the reference implementation's advisory lock exists for.

``converted_to`` is ``read_only`` in the JSON, which is a UI hint and nothing
more (Codex audit P3-2). The guard in ``validate`` is what actually stops a
direct save from forging the pointer.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today

from hospital_ops.hospital_ops.permissions import get_doc_for_action


class CSROpportunity(Document):
    def validate(self) -> None:
        self._check_decline_reason()
        self._guard_converted_to()

    def _check_decline_reason(self) -> None:
        if self.stage == "Declined" and not (self.decline_reason or "").strip():
            frappe.throw(
                _("Say why this opportunity was declined."),
                title=_("CSR Opportunity"),
            )

    def _guard_converted_to(self) -> None:
        """Only convert_to_project may set this pointer, and only once."""
        if self.flags.converting:
            return

        if self.is_new():
            if self.converted_to:
                frappe.throw(
                    _("An opportunity cannot be created already converted."),
                    title=_("CSR Opportunity"),
                )
            return

        stored = frappe.db.get_value("CSR Opportunity", self.name, "converted_to")
        if (self.converted_to or None) != (stored or None):
            frappe.throw(
                _(
                    "Converted To is set by convert_to_project() and cannot be changed by a "
                    "direct save."
                ),
                title=_("CSR Opportunity"),
            )


@frappe.whitelist()
def convert_to_project(name: str, sanction_reference: str | None = None) -> dict:
    """Turns a Sanctioned opportunity into a CSR Project, exactly once."""
    doc = get_doc_for_action("CSR Opportunity", name, ptype="write")

    # Lock this row before deciding, and decide on what the *locked read*
    # returned — not on the possibly-stale value loaded into `doc`. A second
    # caller blocks here until the first commits, then reads the pointer the
    # first one wrote and is refused.
    already = frappe.db.get_value("CSR Opportunity", doc.name, "converted_to", for_update=True)
    if already:
        frappe.throw(
            _("This opportunity was already converted into {0}.").format(already),
            title=_("CSR Opportunity"),
        )

    if doc.stage != "Sanctioned":
        frappe.throw(
            _("Only a Sanctioned opportunity can be converted; this one is {0}.").format(
                doc.stage
            ),
            title=_("CSR Opportunity"),
        )

    if flt(doc.expected_amount) <= 0:
        frappe.throw(
            _(
                "Record the sanctioned amount on the opportunity before converting it — a "
                "project cannot be created with no sanction figure."
            ),
            title=_("CSR Opportunity"),
        )

    project = frappe.get_doc(
        {
            "doctype": "CSR Project",
            "project_title": doc.title,
            "funder": doc.funder,
            "sanctioned_amount": doc.expected_amount,
            "sanctioned_on": doc.decision_on or today(),
            "sanction_reference": sanction_reference,
            "status": "Active",
        }
    ).insert()

    doc.flags.converting = True
    doc.db_set("converted_to", project.name)

    return {"opportunity": doc.name, "csr_project": project.name}
