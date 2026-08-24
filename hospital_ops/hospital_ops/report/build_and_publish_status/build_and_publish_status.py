from __future__ import annotations

"""Build & Publish, all three modules on one page.

Every signage row carries its derived status and what is blocking the next
step; every page carries what a publication would still be missing; every
software project carries how many of its requirements have a passing UAT
result newer than the day they were agreed.

The report shares its arithmetic with the controllers — ``build_publish`` is
the only place any of it is worked out, for the same reason
``csr_financials.totals_from_kind_rows`` is the only place ledger rows become
figures: a second implementation is how two views of the same record disagree,
and here the second view is the one somebody reads before deciding to print
four hundred signs.

Listings use ``frappe.get_list``, not ``frappe.get_all`` (the Phase 3 P2-b
lesson: ``get_all`` ignores permission query conditions and User Permission
restrictions entirely, so a user who could not open a single sign would still
read its readiness off this report). Every related lookup below is keyed to
names that call already permitted.
"""

import frappe
from frappe import _

from hospital_ops.hospital_ops.build_publish import (
    accessibility_checklist,
    missing_for_publication,
    page_status,
    sign_readiness,
    uat_coverage,
)


def execute(filters: dict | None = None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
    return [
        {"fieldname": "area", "label": _("Area"), "fieldtype": "Data", "width": 100},
        {"fieldname": "record", "label": _("Record"), "fieldtype": "Data", "width": 110},
        {"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 240},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 120},
        {"fieldname": "detail", "label": _("Detail"), "fieldtype": "Data", "width": 200},
        {"fieldname": "blockers", "label": _("Blockers"), "fieldtype": "Small Text", "width": 420},
    ]


def get_data(filters) -> list[dict]:
    area = filters.get("area")
    rows: list[dict] = []
    if area in (None, "", "Signage"):
        rows.extend(_signage_rows())
    if area in (None, "", "Website"):
        rows.extend(_website_rows())
    if area in (None, "", "Software"):
        rows.extend(_software_rows())
    return rows


def _signage_rows() -> list[dict]:
    signs = frappe.get_list(
        "Hospital Sign",
        fields=["name", "reference", "purpose", "building", "floor"],
        order_by="reference asc",
    )
    rows = []
    for sign in signs:
        readiness = sign_readiness(sign.name)
        checklist = accessibility_checklist(sign.name)

        # The blockers worth naming are those of the next step that has not
        # cleared. Listing installation's when production has not happened
        # repeats production's own list back at the reader.
        blockers = readiness["production_blockers"] or readiness["installation_blockers"]

        rows.append(
            {
                "area": _("Signage"),
                "record": sign.name,
                "title": "{0} — {1}".format(sign.reference, (sign.purpose or "").strip()),
                "status": readiness["status"],
                "detail": _("version {0}; accessibility {1}/6 met, {2} not checked").format(
                    readiness["design_version"] or "—",
                    checklist["met"],
                    checklist["not_checked"],
                )
                if readiness["design_version"]
                else _("no design yet"),
                "blockers": "\n".join(blockers),
            }
        )
    return rows


def _website_rows() -> list[dict]:
    pages = frappe.get_list(
        "Hospital Web Page",
        fields=["name", "page_title", "url_path", "owner_name"],
        order_by="page_title asc",
    )
    rows = []
    for page in pages:
        steps = frappe.db.sql(
            """
            SELECT step, occurred_on, note
            FROM `tabHospital Web Page Step`
            WHERE parent = %s AND parenttype = 'Hospital Web Page'
            ORDER BY idx ASC
            """,
            (page.name,),
            as_dict=True,
        )
        missing = missing_for_publication(steps)
        rows.append(
            {
                "area": _("Website"),
                "record": page.name,
                "title": page.page_title,
                "status": page_status(steps),
                "detail": page.url_path or _("no path recorded"),
                "blockers": "\n".join(missing),
            }
        )
    return rows


def _software_rows() -> list[dict]:
    projects = frappe.get_list(
        "Software Project Record",
        fields=["name", "project_title", "status", "released_on"],
        order_by="project_title asc",
    )
    rows = []
    for project in projects:
        coverage = uat_coverage(project.name)
        rows.append(
            {
                "area": _("Software"),
                "record": project.name,
                "title": project.project_title,
                "status": project.status,
                "detail": _("{0} of {1} requirement(s) have a passing UAT result").format(
                    len(coverage["covered"]), coverage["requirements"]
                ),
                "blockers": "\n".join(coverage["blockers"]),
            }
        )
    return rows
