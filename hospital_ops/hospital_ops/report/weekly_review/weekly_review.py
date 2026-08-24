from __future__ import annotations

"""The weekly review — a portfolio walk, not a per-record list.

Ported from the Next.js application's ``src/server/domain/review.ts``
(``getWeeklyReview``). That module's guiding rule survives the port intact and
is the reason this report exists at all: **every figure states how it was
arrived at.** Each row below carries a ``how_computed`` string naming the
exact function this app already uses to derive that state, so the report
cannot disagree with the module it reports on — the same "one function, many
callers" discipline as ``csr_financials.totals_from_kind_rows`` and
``build_publish.sign_readiness``.

No ``ref_doctype``: this is a walk across eight different doctypes, not a
report scoped to one. Every base listing goes through ``frappe.get_list``, not
``frappe.get_all`` (Phase 3 finding P2-b) so permission query conditions and
User Permission restrictions apply to a user who is not System Manager.

Sections, in the order ``review.ts`` walks them: open Quick Captures, Waiting
For items due a chase, CSR Reporting Obligations overdue, CSR tranches
overdue, ethics approvals expiring or expired, Hospital Signs blocked or due
inspection, Hospital Web Pages missing something for publication, and
Software Project Records (Active) with a requirement lacking a passing UAT
result. Every section reuses the module's own derivation function; none of
the arithmetic here is a second implementation.

**Deviation, recorded rather than silently worked around:** the brief calls
for "no ref_doctype", but core Frappe's ``Report`` doctype declares
``ref_doctype`` mandatory (``reqd: 1`` in ``report.json``) — inserting a
Report with ``ref_doctype`` unset is refused by the framework itself, not a
choice this app made. ``weekly_review.json`` sets ``ref_doctype`` to
``Quick Capture`` as a nominal anchor only, matching the same accommodation
"Build and Publish Status" already made for ``Hospital Sign`` despite
covering Website and Software too. It gates nothing: every section below
lists through its own doctype's ``frappe.get_list`` (permission-aware in its
own right), and the report itself is restricted to System Manager.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from hospital_ops.hospital_ops.csr_financials import portfolio_event_totals, tranche_states
from hospital_ops.hospital_ops.doctype.csr_reporting_obligation.csr_reporting_obligation import (
    get_obligation_state,
)
from hospital_ops.hospital_ops.research_ethics import ethics_standing
from hospital_ops.hospital_ops.build_publish import (
    missing_for_publication,
    sign_readiness,
    uat_coverage,
)

#: The 60-day ethics look-ahead this report uses (no equivalent build-plan
#: requirement id exists for Phase 6; chosen to match the CSR-obligation
#: notification's own 7-day-ahead reasoning, scaled to how far in advance an
#: ethics renewal actually needs lead time).
ETHICS_LOOK_AHEAD_DAYS = 60


def execute(filters: dict | None = None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
    return [
        {"fieldname": "section", "label": _("Section"), "fieldtype": "Data", "width": 200},
        {"fieldname": "record", "label": _("Record"), "fieldtype": "Data", "width": 110},
        {"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 260},
        {"fieldname": "detail", "label": _("Detail"), "fieldtype": "Small Text", "width": 260},
        {
            "fieldname": "how_computed",
            "label": _("How Computed"),
            "fieldtype": "Small Text",
            "width": 320,
        },
    ]


def get_data(filters) -> list[dict]:
    section = filters.get("section")
    rows: list[dict] = []
    if section in (None, "", "Quick Captures"):
        rows.extend(_quick_capture_rows())
    if section in (None, "", "Waiting For"):
        rows.extend(_waiting_for_rows())
    if section in (None, "", "CSR Reporting Obligations"):
        rows.extend(_csr_obligation_rows())
    if section in (None, "", "CSR Tranches"):
        rows.extend(_csr_tranche_rows())
    if section in (None, "", "Research Ethics"):
        rows.extend(_ethics_rows())
    if section in (None, "", "Hospital Signs"):
        rows.extend(_hospital_sign_rows())
    if section in (None, "", "Hospital Web Pages"):
        rows.extend(_hospital_web_page_rows())
    if section in (None, "", "Software Project Records"):
        rows.extend(_software_rows())
    return rows


# ---------------------------------------------------------------------------
# Quick Captures — the queue to empty, oldest first.
# ---------------------------------------------------------------------------


def _quick_capture_rows() -> list[dict]:
    captures = frappe.get_list(
        "Quick Capture",
        filters={"status": "Open"},
        fields=["name", "capture_text", "creation"],
        order_by="creation asc",
    )
    return [
        {
            "section": _("Quick Captures"),
            "record": capture.name,
            "title": (capture.capture_text or "").split("\n")[0][:120],
            "detail": _("Captured {0}").format(capture.creation),
            "how_computed": _(
                "Quick Capture with status = Open, oldest first (creation ascending)."
            ),
        }
        for capture in captures
    ]


# ---------------------------------------------------------------------------
# Waiting For — items whose follow_up_on has arrived, most overdue first.
# ---------------------------------------------------------------------------


def _waiting_for_rows() -> list[dict]:
    items = frappe.get_list(
        "Waiting For",
        filters={"status": "Waiting", "follow_up_on": ["<=", today()]},
        fields=["name", "subject", "waiting_on", "follow_up_on"],
        order_by="follow_up_on asc",
    )
    return [
        {
            "section": _("Waiting For"),
            "record": item.name,
            "title": item.subject,
            "detail": _("Waiting on {0}; follow up was due {1}").format(
                item.waiting_on, item.follow_up_on
            ),
            "how_computed": _(
                "Waiting For with status = Waiting and follow_up_on on or before today, "
                "ordered oldest follow_up_on first (most overdue first)."
            ),
        }
        for item in items
    ]


# ---------------------------------------------------------------------------
# CSR Reporting Obligations overdue — derived, via get_obligation_state().
# ---------------------------------------------------------------------------


def _csr_obligation_rows() -> list[dict]:
    # A due_on in the past is the only candidate set that can possibly be
    # overdue (obligation_overdue also requires nothing submitted); the exact
    # verdict for each candidate is then asked of the module's own helper
    # rather than re-derived here, so this report cannot disagree with it.
    candidates = frappe.get_list(
        "CSR Reporting Obligation",
        filters={"due_on": ["<", today()]},
        fields=["name", "csr_project", "description", "due_on"],
        order_by="due_on asc",
    )
    rows = []
    for candidate in candidates:
        state = get_obligation_state(candidate.name)
        if not state["overdue"]:
            continue
        rows.append(
            {
                "section": _("CSR Reporting Obligations"),
                "record": candidate.name,
                "title": "{0} ({1})".format(candidate.description, candidate.csr_project),
                "detail": _("Due {0}, nothing submitted").format(candidate.due_on),
                "how_computed": _(
                    "csr_reporting_obligation.get_obligation_state() — overdue means due_on is "
                    "past and nothing has been submitted."
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CSR Tranches overdue — derived, via csr_financials.tranche_states().
# ---------------------------------------------------------------------------


def _csr_tranche_rows() -> list[dict]:
    projects = frappe.get_list(
        "CSR Project", fields=["name", "project_title"], order_by="project_title asc"
    )
    if not projects:
        return []

    names = [project.name for project in projects]
    totals_by_project = portfolio_event_totals()
    tranches_by_project = _tranches_by_project(names)

    rows = []
    for project in projects:
        received = totals_by_project.get(project.name, {"received": 0.0}).get("received", 0.0)
        states = tranche_states(tranches_by_project.get(project.name, []), received)
        for state in states:
            if not state["overdue"]:
                continue
            rows.append(
                {
                    "section": _("CSR Tranches"),
                    "record": state["tranche"],
                    "title": "{0} ({1})".format(project.project_title, project.name),
                    "detail": _("Expected {0}: short by {1}").format(
                        state["expected_on"], state["shortfall"]
                    ),
                    "how_computed": _(
                        "csr_financials.tranche_states() — cumulative expected against "
                        "submitted receipts, allocated in document order."
                    ),
                }
            )
    return rows


def _tranches_by_project(names: list[str]) -> dict[str, list]:
    rows = frappe.get_all(
        "CSR Tranche",
        filters={"parenttype": "CSR Project", "parent": ["in", names]},
        fields=["name", "parent", "idx", "expected_on", "expected_amount"],
        order_by="parent asc, expected_on asc, idx asc",
    )
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.parent, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# Research ethics — expiring within the look-ahead, or already expired.
# ---------------------------------------------------------------------------


def _ethics_rows() -> list[dict]:
    approved_submissions = frappe.get_list(
        "Research Ethics Submission",
        filters={"decision": "Approved"},
        fields=["study"],
    )
    studies = sorted({row.study for row in approved_submissions})
    if not studies:
        return []

    titles = {
        row.name: row.study_title
        for row in frappe.get_list(
            "Research Study", filters={"name": ["in", studies]}, fields=["name", "study_title"]
        )
    }
    horizon = add_days(today(), ETHICS_LOOK_AHEAD_DAYS)

    rows = []
    for study in studies:
        standing = ethics_standing(study)
        if standing["status"] == "expired":
            rows.append(
                {
                    "section": _("Research Ethics"),
                    "record": standing["submission"],
                    "title": titles.get(study, study),
                    "detail": _("Expired {0}").format(standing["valid_until"]),
                    "how_computed": _(
                        "research_ethics.ethics_standing() — the latest Approved decision's "
                        "valid_until has passed with nothing newer pending."
                    ),
                }
            )
        elif standing["status"] == "approved" and getdate(standing["valid_until"]) <= getdate(
            horizon
        ):
            rows.append(
                {
                    "section": _("Research Ethics"),
                    "record": standing["submission"],
                    "title": titles.get(study, study),
                    "detail": _("Expires {0} (within {1} days)").format(
                        standing["valid_until"], ETHICS_LOOK_AHEAD_DAYS
                    ),
                    "how_computed": _(
                        "research_ethics.ethics_standing() — approved, valid_until within "
                        "{0} days."
                    ).format(ETHICS_LOOK_AHEAD_DAYS),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Hospital Signs — next step blocked, or next inspection due.
# ---------------------------------------------------------------------------


def _hospital_sign_rows() -> list[dict]:
    signs = frappe.get_list(
        "Hospital Sign",
        fields=["name", "reference", "purpose", "next_inspection_on"],
        order_by="reference asc",
    )
    rows = []
    for sign in signs:
        readiness = sign_readiness(sign.name)
        blockers = readiness["production_blockers"] or readiness["installation_blockers"]
        inspection_due = sign.next_inspection_on and getdate(sign.next_inspection_on) <= getdate(
            today()
        )
        if not blockers and not inspection_due:
            continue

        details = []
        if blockers:
            details.append(_("Blocked: {0}").format("; ".join(blockers)))
        if inspection_due:
            details.append(_("Next inspection was due {0}").format(sign.next_inspection_on))

        rows.append(
            {
                "section": _("Hospital Signs"),
                "record": sign.name,
                "title": "{0} — {1}".format(sign.reference, (sign.purpose or "").strip()),
                "detail": " | ".join(details),
                "how_computed": _(
                    "build_publish.sign_readiness()/sign_blockers() for the next step, plus "
                    "next_inspection_on compared to today."
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Hospital Web Pages — non-empty missing_for_publication().
# ---------------------------------------------------------------------------


def _hospital_web_page_rows() -> list[dict]:
    pages = frappe.get_list(
        "Hospital Web Page",
        fields=["name", "page_title", "url_path"],
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
        if not missing:
            continue
        rows.append(
            {
                "section": _("Hospital Web Pages"),
                "record": page.name,
                "title": page.page_title,
                "detail": "; ".join(missing),
                "how_computed": _(
                    "build_publish.missing_for_publication() over this page's recorded steps."
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Software Project Records (Active) — requirements lacking a passing UAT.
# ---------------------------------------------------------------------------


def _software_rows() -> list[dict]:
    projects = frappe.get_list(
        "Software Project Record",
        filters={"status": "Active"},
        fields=["name", "project_title"],
        order_by="project_title asc",
    )
    rows = []
    for project in projects:
        coverage = uat_coverage(project.name)
        if not coverage["uncovered"]:
            continue
        rows.append(
            {
                "section": _("Software Project Records"),
                "record": project.name,
                "title": project.project_title,
                "detail": _("{0} of {1} requirement(s) lack a passing UAT result").format(
                    len(coverage["uncovered"]), coverage["requirements"]
                ),
                "how_computed": _(
                    "build_publish.uat_coverage() — a requirement counts only with a passing "
                    "result dated after the day it was agreed."
                ),
            }
        )
    return rows
