from __future__ import annotations

"""Manual fallback verification for Phase 2 (Quick Capture / Waiting For /
Meeting Record), for use when ``bench run-tests`` cannot run the real
``test_*.py`` suites on this shared evaluation site.

Why this exists: ``frappe.tests.IntegrationTestCase.setUpClass`` infers the
doctype under test from the test module's name (``test_waiting_for`` ->
"Waiting For") and calls ``make_test_records("Waiting For")``, which walks
every Link field — ``waiting_on -> Contact`` for Waiting For, the child
table's ``todo -> ToDo`` for Meeting Record — to build fixture records. On
this box that walk reaches erpnext's Company/Fiscal Year test fixtures,
which try to create a synthetic "_Test Fiscal Year 2025" that overlaps with
a *real* Fiscal Year 2025-2026 created by concurrent ERPNext configuration
work happening on the same site (a different agent, same box) — unrelated to
anything in this app, and it fails identically with or without
``--skip-before-tests`` because that flag only skips erpnext's explicit
``before_tests`` hook, not this per-doctype fixture walk that lives inside
the test-case base class itself.

This module re-checks the same invariants the real test suites cover, using
plain assertions instead of ``IntegrationTestCase`` subclasses, which never
triggers that walk. It is not a substitute for the real suites — it is the
documented fallback (see the build brief) for this specific, shared-site
obstruction. Everything it creates is rolled back at the end regardless of
outcome, pass or fail.
"""

import frappe
from frappe.utils import add_days, today

from hospital_ops.hospital_ops.doctype.meeting_record.meeting_record import (
    create_todo_from_decision,
)
from hospital_ops.hospital_ops.doctype.quick_capture.quick_capture import (
    process_into_todo,
)
from hospital_ops.hospital_ops.doctype.waiting_for.waiting_for import log_follow_up

_PASS = []
_FAIL = []


def _check(label: str, condition: bool) -> None:
    (_PASS if condition else _FAIL).append(label)
    print(("PASS " if condition else "FAIL ") + label)


def _expect_throws(label: str, fn) -> None:
    try:
        fn()
    except frappe.ValidationError:
        _check(label, True)
    else:
        _check(label, False)


def run_phase2_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _quick_capture_checks()
        _waiting_for_checks()
        _meeting_record_checks()
    finally:
        frappe.db.rollback()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        frappe.throw(f"Failures: {_FAIL}")


def _quick_capture_checks() -> None:
    doc = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "Call the pharmacy about stock"}
    ).insert()
    _check("quick_capture: default status is Open", doc.status == "Open")
    _check("quick_capture: unlinked at capture time", not doc.processed_into)

    result = process_into_todo(doc.name)
    todo = frappe.get_doc("ToDo", result["todo"])
    _check(
        "quick_capture: process_into_todo links the ToDo back",
        todo.reference_type == "Quick Capture" and todo.reference_name == doc.name,
    )
    doc.reload()
    _check(
        "quick_capture: processing marks Processed and stamps processed_into",
        doc.status == "Processed" and doc.processed_into == todo.name,
    )
    _expect_throws(
        "quick_capture: processing an already-processed capture is refused",
        lambda: process_into_todo(doc.name),
    )

    discarded = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "A stray thought", "status": "Discarded"}
    ).insert()
    _expect_throws(
        "quick_capture: processing a Discarded capture is refused",
        lambda: process_into_todo(discarded.name),
    )


def _waiting_for_checks() -> None:
    contact = frappe.get_doc(
        {"doctype": "Contact", "first_name": "Fallback Test Contact"}
    ).insert()

    ok = frappe.get_doc(
        {
            "doctype": "Waiting For",
            "waiting_on": contact.name,
            "subject": "Quotation for the CT scanner AMC",
            "delegated_on": today(),
            "promised_on": add_days(today(), 7),
        }
    ).insert()
    _check(
        "waiting_for: promised_on on/after delegated_on is accepted",
        ok.status == "Waiting",
    )
    _check(
        "waiting_for: follow_up_on defaults to promised_on",
        str(ok.follow_up_on) == str(ok.promised_on),
    )

    _expect_throws(
        "waiting_for: promised_on before delegated_on is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "Waiting For",
                "waiting_on": contact.name,
                "subject": "Bad dates",
                "delegated_on": today(),
                "promised_on": add_days(today(), -1),
            }
        ).insert(),
    )

    new_date = add_days(today(), 14)
    result = log_follow_up(ok.name, note="They asked for another week", new_promised_on=new_date)
    ok.reload()
    _check(
        "waiting_for: log_follow_up moves promised_on and follow_up_on",
        str(result["promised_on"]) == str(new_date)
        and str(result["follow_up_on"]) == str(new_date)
        and len(ok.follow_ups) == 1,
    )

    ok.status = "Resolved"
    ok.save()
    _expect_throws(
        "waiting_for: follow-up on a Resolved item is refused",
        lambda: log_follow_up(ok.name, note="Too late"),
    )


def _meeting_record_checks() -> None:
    meeting = frappe.get_doc(
        {
            "doctype": "Meeting Record",
            "meeting_title": "Cardiology weekly review",
            "held_on": today(),
            "attendees": "Dr. Varyani, Dr. Kini",
            "decisions": [
                {
                    "decision": "Order two additional Cath Lab guidewires",
                    "owner_name": "Praveen",
                    "due_on": add_days(today(), 3),
                }
            ],
        }
    ).insert()
    row_name = meeting.decisions[0].name
    _check("meeting_record: fresh decision row has no ToDo yet", not meeting.decisions[0].todo)

    result = create_todo_from_decision(meeting.name, row_name)
    todo = frappe.get_doc("ToDo", result["todo"])
    meeting.reload()
    _check(
        "meeting_record: create_todo_from_decision stamps the row and links back",
        meeting.decisions[0].todo == todo.name
        and todo.reference_type == "Meeting Record"
        and todo.reference_name == meeting.name,
    )

    _expect_throws(
        "meeting_record: a second ToDo for the same decision is refused",
        lambda: create_todo_from_decision(meeting.name, row_name),
    )
    _expect_throws(
        "meeting_record: an unknown decision row is refused",
        lambda: create_todo_from_decision(meeting.name, "not-a-real-row"),
    )
