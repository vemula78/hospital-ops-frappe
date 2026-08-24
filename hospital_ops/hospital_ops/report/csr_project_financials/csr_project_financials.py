from __future__ import annotations

"""The portfolio view of every CSR project's money.

Deliberately shares its arithmetic with
``CSR Project.get_project_financials``: both call
``csr_financials.totals_from_kind_rows``, which is the only code in the app
that turns ledger rows into received/spent/balance. The reference build's
lesson was that a figure computed in two places eventually disagrees with
itself — a closure check computed outside a transaction and recomputed inside
it let a record be written whose stored state disagreed with the check that
permitted it. One function, two callers, no drift.

Nothing here is read from a stored total, because no stored total exists.
"""

import frappe
from frappe import _
from frappe.utils import flt

from hospital_ops.hospital_ops.csr_financials import (
    obligation_overdue,
    portfolio_event_totals,
    tranche_states,
)

EMPTY_TOTALS = {"received": 0.0, "spent": 0.0, "balance": 0.0}


def execute(filters: dict | None = None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
    return [
        {
            "fieldname": "csr_project",
            "label": _("Project"),
            "fieldtype": "Link",
            "options": "CSR Project",
            "width": 120,
        },
        {"fieldname": "project_title", "label": _("Title"), "fieldtype": "Data", "width": 240},
        {
            "fieldname": "funder",
            "label": _("Funder"),
            "fieldtype": "Link",
            "options": "CSR Funder",
            "width": 130,
        },
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 90},
        {
            "fieldname": "sanctioned",
            "label": _("Sanctioned"),
            "fieldtype": "Currency",
            "width": 130,
        },
        {"fieldname": "received", "label": _("Received"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "spent", "label": _("Spent"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "balance", "label": _("Balance"), "fieldtype": "Currency", "width": 130},
        {
            "fieldname": "unreceived_sanction",
            "label": _("Sanction Not Yet Received"),
            "fieldtype": "Currency",
            "width": 170,
        },
        {
            "fieldname": "overdue_tranches",
            "label": _("Overdue Tranches"),
            "fieldtype": "Int",
            "width": 130,
        },
        {
            "fieldname": "overdue_reports",
            "label": _("Overdue Reports"),
            "fieldtype": "Int",
            "width": 130,
        },
    ]


def get_data(filters) -> list[dict]:
    project_filters = {}
    if filters.get("funder"):
        project_filters["funder"] = filters.get("funder")
    if filters.get("status"):
        project_filters["status"] = filters.get("status")

    projects = frappe.get_all(
        "CSR Project",
        filters=project_filters,
        fields=["name", "project_title", "funder", "status", "sanctioned_amount"],
        order_by="sanctioned_on desc",
    )
    if not projects:
        return []

    names = [project.name for project in projects]
    totals_by_project = portfolio_event_totals()
    tranches_by_project = _tranches_by_project(names)
    overdue_reports = _overdue_reports_by_project(names)

    rows = []
    for project in projects:
        totals = totals_by_project.get(project.name, EMPTY_TOTALS)
        sanctioned = flt(project.sanctioned_amount, 2)
        states = tranche_states(tranches_by_project.get(project.name, []), totals["received"])
        rows.append(
            {
                "csr_project": project.name,
                "project_title": project.project_title,
                "funder": project.funder,
                "status": project.status,
                "sanctioned": sanctioned,
                "received": totals["received"],
                "spent": totals["spent"],
                "balance": totals["balance"],
                "unreceived_sanction": flt(sanctioned - totals["received"], 2),
                "overdue_tranches": sum(1 for state in states if state["overdue"]),
                "overdue_reports": overdue_reports.get(project.name, 0),
            }
        )
    return rows


def _tranches_by_project(names: list[str]) -> dict[str, list]:
    rows = frappe.get_all(
        "CSR Tranche",
        filters={"parenttype": "CSR Project", "parent": ["in", names]},
        fields=["name", "parent", "expected_on", "expected_amount"],
        order_by="parent asc, expected_on asc",
    )
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.parent, []).append(row)
    return grouped


def _overdue_reports_by_project(names: list[str]) -> dict[str, int]:
    rows = frappe.get_all(
        "CSR Reporting Obligation",
        filters={"csr_project": ["in", names]},
        fields=["csr_project", "due_on", "submitted_on"],
    )
    counts: dict[str, int] = {}
    for row in rows:
        if obligation_overdue(row.due_on, row.submitted_on):
            counts[row.csr_project] = counts.get(row.csr_project, 0) + 1
    return counts
