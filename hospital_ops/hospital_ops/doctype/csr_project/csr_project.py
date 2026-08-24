from __future__ import annotations

"""A funded CSR project.

**No stored totals.** There is no ``received``, no ``spent`` and no
``balance`` field on this doctype, and there will not be one. Every figure is
computed from the submitted CSR Fund Event rows by
``hospital_ops.hospital_ops.csr_financials`` — one summing function shared
with the "CSR Project Financials" report, so the two cannot disagree.

Tranche state is derived for the same reason: a stored "overdue" flag is
wrong the moment a backdated receipt is entered, and then nobody knows which
of the two to believe.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from hospital_ops.hospital_ops.csr_financials import (
    obligation_overdue,
    project_event_totals,
    tranche_states,
)
from hospital_ops.hospital_ops.permissions import get_doc_for_action


class CSRProject(Document):
    def validate(self) -> None:
        self._check_sanctioned_amount()
        self._check_tranche_dates()

    def _check_sanctioned_amount(self) -> None:
        if flt(self.sanctioned_amount) <= 0:
            frappe.throw(
                _("The sanctioned amount must be greater than zero."),
                title=_("CSR Project"),
            )

    def _check_tranche_dates(self) -> None:
        # A tranche expected before the sanction itself is a data-entry error;
        # it would also make the cumulative-expected ordering meaningless.
        for row in self.tranches or []:
            if flt(row.expected_amount) <= 0:
                frappe.throw(
                    _("Tranche {0}: the expected amount must be greater than zero.").format(
                        row.idx
                    ),
                    title=_("CSR Project"),
                )
            if self.sanctioned_on and getdate(row.expected_on) < getdate(self.sanctioned_on):
                frappe.throw(
                    _(
                        "Tranche {0} is expected on {1}, before the sanction date {2}."
                    ).format(row.idx, row.expected_on, self.sanctioned_on),
                    title=_("CSR Project"),
                )


@frappe.whitelist()
def get_project_financials(csr_project: str) -> dict:
    """Every figure for one project, computed, in one pass over the ledger.

    Shares ``totals_from_kind_rows`` with the CSR Project Financials report.
    One function, two callers: the document view and the portfolio view are
    incapable of showing different numbers for the same project.
    """
    doc = get_doc_for_action("CSR Project", csr_project, ptype="read")

    totals = project_event_totals(doc.name)
    sanctioned = flt(doc.sanctioned_amount, 2)

    obligations = frappe.get_all(
        "CSR Reporting Obligation",
        filters={"csr_project": doc.name},
        fields=["name", "description", "due_on", "submitted_on", "verified_by", "verified_on"],
        order_by="due_on asc",
    )
    for obligation in obligations:
        obligation["overdue"] = obligation_overdue(
            obligation["due_on"], obligation["submitted_on"]
        )

    return {
        "csr_project": doc.name,
        "project_title": doc.project_title,
        "funder": doc.funder,
        "status": doc.status,
        "sanctioned": sanctioned,
        "received": totals["received"],
        "spent": totals["spent"],
        "balance": totals["balance"],
        "unreceived_sanction": flt(sanctioned - totals["received"], 2),
        "tranches": tranche_states(doc.tranches or [], totals["received"]),
        "obligations": obligations,
        "derivations": {
            "received": "Submitted Receipts less submitted Receipt Reversals.",
            "spent": "Submitted Expenditures less submitted Expenditure Reversals.",
            "balance": "Received less spent. Drafts count for nothing.",
            "tranche_overdue": (
                "Expected on a past date and the receipts to date fall short of the "
                "cumulative expectation up to that tranche."
            ),
        },
    }
