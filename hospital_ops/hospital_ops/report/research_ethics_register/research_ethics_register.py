from __future__ import annotations

"""Every study with its derived ethics standing, one row each.

Shares its arithmetic with ``research_ethics.ethics_standing`` — the report
and the study form/whitelisted method must never disagree, so both call the
one function. Studies are listed with ``frappe.get_list`` (permission-aware —
the Phase 3 P2-b lesson: ``frappe.get_all`` ignores permission query
conditions and User Permission restrictions entirely) and every related
lookup below is keyed to the names that call already permitted.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from hospital_ops.hospital_ops.research_ethics import ethics_standing


def execute(filters: dict | None = None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
    return [
        {
            "fieldname": "study",
            "label": _("Study"),
            "fieldtype": "Link",
            "options": "Research Study",
            "width": 110,
        },
        {"fieldname": "study_title", "label": _("Title"), "fieldtype": "Data", "width": 240},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 90},
        {"fieldname": "standing", "label": _("Ethics Standing"), "fieldtype": "Data", "width": 110},
        {
            "fieldname": "latest_submission",
            "label": _("Latest Submission"),
            "fieldtype": "Link",
            "options": "Research Ethics Submission",
            "width": 130,
        },
        {"fieldname": "valid_until", "label": _("Valid Until"), "fieldtype": "Date", "width": 100},
        {
            "fieldname": "days_to_expiry",
            "label": _("Days To Expiry"),
            "fieldtype": "Int",
            "width": 110,
        },
    ]


def get_data(filters) -> list[dict]:
    study_filters = {}
    if filters.get("status"):
        study_filters["status"] = filters.get("status")

    # get_list, not get_all — the same lesson as CSR Project Financials
    # (Phase 3 P2-b): get_all ignores permission query conditions and User
    # Permission restrictions, so a user who could not open a single study
    # would still read its ethics standing off this report.
    studies = frappe.get_list(
        "Research Study",
        filters=study_filters,
        fields=["name", "study_title", "status"],
        order_by="study_title asc",
    )
    if not studies:
        return []

    as_of = getdate(today())
    rows = []
    for study in studies:
        standing = ethics_standing(study.name, as_of=str(as_of))
        valid_until = standing.get("valid_until")
        days_to_expiry = (getdate(valid_until) - as_of).days if valid_until else None
        rows.append(
            {
                "study": study.name,
                "study_title": study.study_title,
                "status": study.status,
                "standing": standing["status"],
                "latest_submission": standing.get("submission"),
                "valid_until": valid_until,
                "days_to_expiry": days_to_expiry,
            }
        )
    return rows
