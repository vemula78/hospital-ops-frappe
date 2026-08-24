from __future__ import annotations

"""Manual fallback verification for Phase 2 (Quick Capture / Waiting For /
Meeting Record) and Phase 3 (the CSR module), for use when ``bench
run-tests`` cannot run the real ``test_*.py`` suites on this shared
evaluation site.

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

import inspect

import frappe
from frappe.client import set_value as client_set_value
from frappe.utils import add_days, flt, strip_html, today

from hospital_ops.hospital_ops.doctype.csr_fund_event.csr_fund_event import (
    CSRFundEvent,
    preview_warnings,
)
from hospital_ops.hospital_ops.doctype.csr_opportunity.csr_opportunity import (
    convert_to_project,
)
from hospital_ops.hospital_ops.doctype.csr_project.csr_project import (
    get_project_financials,
)
from hospital_ops.hospital_ops.doctype.csr_reporting_obligation.csr_reporting_obligation import (
    get_obligation_state,
    mark_verified,
)
from hospital_ops.hospital_ops.doctype.meeting_record.meeting_record import (
    create_todo_from_decision,
)
from hospital_ops.hospital_ops.doctype.quick_capture.quick_capture import (
    discard,
    process_into_todo,
)
from hospital_ops.hospital_ops.doctype.waiting_for.waiting_for import log_follow_up
from hospital_ops.hospital_ops.permissions import get_doc_for_action
from hospital_ops.hospital_ops.report.csr_project_financials.csr_project_financials import (
    execute as report_execute,
    get_data as report_get_data,
)
from hospital_ops.hospital_ops.data_boundary import find_participant_identifier_fields
from hospital_ops.hospital_ops.doctype.research_study.research_study import (
    complete_milestone,
    get_study_standing,
)
from hospital_ops.hospital_ops.doctype.research_ethics_submission.research_ethics_submission import (
    record_decision,
)
from hospital_ops.hospital_ops.report.research_ethics_register.research_ethics_register import (
    execute as research_report_execute,
)
from hospital_ops.hospital_ops.research_ethics import ethics_standing
from hospital_ops.hospital_ops.build_publish import (
    SIGN_PREREQUISITES,
    accessibility_checklist,
    missing_for_publication,
    sign_readiness,
    sign_status,
    uat_coverage,
)
from hospital_ops.hospital_ops.doctype.hospital_sign.hospital_sign import (
    add_design,
    record_accessibility_check,
)
from hospital_ops.hospital_ops.doctype.hospital_web_page.hospital_web_page import (
    get_page_state,
    record_step,
)
from hospital_ops.hospital_ops.doctype.software_project_record.software_project_record import (
    add_requirement,
    record_release,
    record_uat_result,
)
from hospital_ops.hospital_ops.report.build_and_publish_status.build_and_publish_status import (
    execute as build_publish_report_execute,
)
from hospital_ops.hospital_ops.report.weekly_review.weekly_review import (
    execute as weekly_review_execute,
)

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


def _throws_message(label: str, fn) -> str:
    """Like ``_expect_throws``, but hands back the refusal text.

    Several Phase 3 invariants are not "it was refused" but "it was refused
    *and said which figures*" — a refusal with no numbers in it cannot be
    acted on, and the acknowledgement the user has to repeat back is that
    exact text.
    """
    try:
        fn()
    except frappe.ValidationError as exc:
        _check(label, True)
        return strip_html(str(exc))
    _check(label, False)
    return ""


def run_phase2_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _quick_capture_checks()
        _waiting_for_checks()
        _meeting_record_checks()
        _p31_existence_oracle_checks()
        _p32_state_guard_checks()
        _p32_full_state_machine_checks()
        _set_value_bypass_check()
        _p2_lock_order_checks()
        _p22_lock_call_pattern_check()
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
        {"doctype": "Quick Capture", "capture_text": "A stray thought"}
    ).insert()
    discard(discarded.name)
    discarded.reload()
    _check("quick_capture: discard() marks the capture Discarded", discarded.status == "Discarded")
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


def _p31_existence_oracle_checks() -> None:
    """Codex audit P3-1: a missing document and an existing-but-unauthorized
    one must raise the identical PermissionError, so a caller cannot use the
    response to tell which case it is (and so enumerate the sequential
    naming series).
    """
    doc = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "For the P3-1 existence-oracle check"}
    ).insert()

    message_for_missing = None
    try:
        get_doc_for_action("Quick Capture", "CAP-99999", ptype="write")
    except frappe.PermissionError as exc:
        message_for_missing = str(exc)

    message_for_unauthorized = None
    frappe.set_user("Guest")
    try:
        get_doc_for_action("Quick Capture", doc.name, ptype="write")
    except frappe.PermissionError as exc:
        message_for_unauthorized = str(exc)
    finally:
        frappe.set_user("Administrator")

    _check(
        "p3-1: a missing name raises PermissionError (not DoesNotExistError)",
        message_for_missing is not None,
    )
    _check(
        "p3-1: an existing-but-unauthorized name raises PermissionError",
        message_for_unauthorized is not None,
    )
    _check(
        "p3-1: both raise the identical message — no existence oracle",
        message_for_missing is not None and message_for_missing == message_for_unauthorized,
    )


def _p32_state_guard_checks() -> None:
    """Codex audit P3-2: read_only in the doctype JSON is a UI hint only.
    Once the stored status is Processed/Discarded, a direct save must not be
    able to change status or the processed_into pointer, and only an
    allow-listed doctype may ever be named in processed_into_doctype.
    """
    doc = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "For the P3-2 state-guard checks"}
    ).insert()
    process_into_todo(doc.name)
    doc.reload()
    _check(
        "p3-2: the whitelisted path still processes a capture (positive control)",
        doc.status == "Processed" and bool(doc.processed_into),
    )

    doc.status = "Open"
    _expect_throws(
        "p3-2: a direct save flipping Processed back to Open is refused",
        doc.save,
    )

    other = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "For the disallowed-doctype check"}
    ).insert()
    other.processed_into_doctype = "User"
    _expect_throws(
        "p3-2: a disallowed processed_into_doctype is refused",
        other.save,
    )


def _p2_lock_order_checks() -> None:
    """Codex audit P2-1/P2-2: genuine concurrency is not practical to drive
    on this bench (a single request-response process per bench execute
    call), so this is the documented fallback the audit asked for — a
    code-level assertion that the row lock is taken, by reading the actual
    source, before either method inserts its ToDo. See also the README's
    "Concurrency guarantees" note for the reasoning this stands in for.
    """
    quick_capture_src = inspect.getsource(process_into_todo)
    lock_at = quick_capture_src.find("for_update=True")
    insert_at = quick_capture_src.find('"doctype": "ToDo"')
    _check(
        "p2-1: process_into_todo locks the row (for_update) before inserting the ToDo",
        lock_at != -1 and insert_at != -1 and lock_at < insert_at,
    )

    meeting_record_src = inspect.getsource(create_todo_from_decision)
    lock_at2 = meeting_record_src.find("for_update=True")
    insert_at2 = meeting_record_src.find('"doctype": "ToDo"')
    _check(
        "p2-2: create_todo_from_decision locks a row (for_update) before inserting the ToDo",
        lock_at2 != -1 and insert_at2 != -1 and lock_at2 < insert_at2,
    )
    _check(
        "p2-2: the locked read targets Meeting Decision, not just the parent",
        'frappe.db.get_value("Meeting Decision"' in meeting_record_src,
    )


def _p32_full_state_machine_checks() -> None:
    """Codex re-audit of P3-2: the original guard only fired once the
    *stored* status was already Processed/Discarded, which left two holes —
    a document could be inserted already "born" Processed with a forged
    pointer, and an existing Open row could be direct-saved straight to
    Processed with a forged pointer, never touching process_into_todo at
    all. Both are covered here, plus the end-to-end positive control.
    """
    _expect_throws(
        "p3-2: inserting a capture already Processed with a forged pointer is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "Quick Capture",
                "capture_text": "Forged at birth",
                "status": "Processed",
                "processed_into_doctype": "ToDo",
                "processed_into": "SOME-FAKE-TODO",
            }
        ).insert(),
    )

    doc = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "Try to self-process via direct save"}
    ).insert()
    doc.status = "Processed"
    doc.processed_into_doctype = "ToDo"
    doc.processed_into = "SOME-FAKE-TODO"
    _expect_throws(
        "p3-2: a direct save forging Open -> Processed (never touching process_into_todo) is refused",
        doc.save,
    )

    fresh = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "Still works end to end"}
    ).insert()
    result = process_into_todo(fresh.name)
    fresh.reload()
    _check(
        "p3-2: process_into_todo still processes a capture end-to-end (positive control)",
        fresh.status == "Processed" and fresh.processed_into == result["todo"],
    )


def _set_value_bypass_check() -> None:
    """Verifies (rather than assumes) whether frappe.client.set_value — the
    REST-whitelisted endpoint, distinct from frappe.db.set_value — bypasses
    controller validate().

    Read from the container's actual v16 source, apps/frappe/frappe/
    client.py: set_value() does `doc = frappe.get_doc(doctype, name)` then
    `doc.update(values)` then `doc.save()` (client.py:207-215 in this
    container's checkout) — save() is exactly what runs validate(), so this
    endpoint does NOT bypass it. Only frappe.db.set_value (a raw UPDATE, not
    REST-whitelisted, not used anywhere in this app) bypasses the
    controller. This check proves that on the actual installed version
    rather than trusting the source reading alone.
    """
    doc = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "For the set_value bypass check"}
    ).insert()
    process_into_todo(doc.name)
    doc.reload()
    _check("set_value: capture is Processed before the attempt (setup)", doc.status == "Processed")

    _expect_throws(
        "set_value: frappe.client.set_value cannot flip a Processed capture's status "
        "(it calls doc.save(), which runs validate() — confirmed against this container's "
        "apps/frappe/frappe/client.py, not assumed)",
        lambda: client_set_value("Quick Capture", doc.name, "status", "Open"),
    )


def _p22_lock_call_pattern_check() -> None:
    """Codex re-audit of P2-2: not just "some for_update=True appears before
    the insert in the source" but the *actual* call pattern — a locking
    read of frappe.db.get_value("Meeting Decision", row_name, "todo",
    for_update=True), and that the refusal is driven by the value that read
    returned, not by a stale doc/row snapshot.
    """
    meeting = frappe.get_doc(
        {
            "doctype": "Meeting Record",
            "meeting_title": "P2-2 lock call pattern",
            "held_on": today(),
            "decisions": [{"decision": "Check the lock call pattern"}],
        }
    ).insert()
    row_name = meeting.decisions[0].name

    first = create_todo_from_decision(meeting.name, row_name)

    calls = []
    original_get_value = frappe.db.get_value

    def _recording_get_value(*args, **kwargs):
        calls.append((args, kwargs))
        return original_get_value(*args, **kwargs)

    frappe.db.get_value = _recording_get_value
    try:
        try:
            create_todo_from_decision(meeting.name, row_name)
            second_error_message = None
        except frappe.ValidationError as exc:
            second_error_message = str(exc)
    finally:
        frappe.db.get_value = original_get_value

    locking_call_on_child = any(
        len(args) >= 3
        and args[0] == "Meeting Decision"
        and args[1] == row_name
        and args[2] == "todo"
        and kwargs.get("for_update") is True
        for args, kwargs in calls
    )
    _check(
        "p2-2: the second call issues get_value(\"Meeting Decision\", row_name, \"todo\", for_update=True)",
        locking_call_on_child,
    )
    _check(
        "p2-2: the refusal names the exact ToDo the locked read found",
        second_error_message is not None and first["todo"] in second_error_message,
    )


# ---------------------------------------------------------------------------
# Phase 3 — CSR
# ---------------------------------------------------------------------------
#
# Same shape as Phase 2: plain assertions, every negative carrying a positive
# control in the same block, everything rolled back at the end whatever
# happens. Synthetic data only — the funders and projects below are invented,
# and §4.3 holds throughout: nothing here is beneficiary-level.


def run_phase3_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _csr_amount_and_draft_checks()
        _csr_override_pattern_checks()
        _csr_reversal_checks()
        _csr_no_cancel_checks()
        _csr_project_state_checks()
        _csr_opportunity_checks()
        _csr_obligation_checks()
        _csr_report_parity_checks()
        _csr_no_stored_total_checks()
        _csr_lock_order_checks()
        _csr_audit_p1_immutability_checks()
        _csr_audit_p2_locked_state_checks()
        _csr_audit_p3_precision_and_tie_checks()
    finally:
        frappe.db.rollback()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        frappe.throw(f"Failures: {_FAIL}")


def _funder(name: str) -> str:
    return frappe.get_doc({"doctype": "CSR Funder", "funder_name": name}).insert().name


def _project(title: str, sanctioned: float, tranches=None, funder=None) -> str:
    return (
        frappe.get_doc(
            {
                "doctype": "CSR Project",
                "project_title": title,
                "funder": funder or _funder(f"Synthetic Funder for {title}"),
                "sanctioned_amount": sanctioned,
                "sanctioned_on": add_days(today(), -30),
                "tranches": tranches or [],
            }
        )
        .insert()
        .name
    )


def _event(project: str, kind: str, amount: float, **extra):
    doc = frappe.get_doc(
        {
            "doctype": "CSR Fund Event",
            "csr_project": project,
            "kind": kind,
            "amount": amount,
            "occurred_on": today(),
            **extra,
        }
    )
    doc.insert()
    return doc


def _submitted(project: str, kind: str, amount: float, **extra):
    doc = _event(project, kind, amount, **extra)
    doc.submit()
    return doc


def _csr_amount_and_draft_checks() -> None:
    """Amount must be positive, and a draft counts for nothing."""
    project = _project("Amount and draft checks", 1_000_000)

    _expect_throws(
        "fund_event: an amount of zero is refused",
        lambda: _event(project, "Receipt", 0),
    )
    _expect_throws(
        "fund_event: a negative amount is refused (direction lives in the kind)",
        lambda: _event(project, "Receipt", -5_000),
    )

    accepted = _event(project, "Receipt", 100_000)
    _check(
        "fund_event: a positive amount is accepted (positive control)",
        accepted.docstatus == 0 and flt(accepted.amount, 2) == 100_000.00,
    )

    drafted = get_project_financials(project)
    _check(
        "financials: a draft receipt counts for nothing",
        drafted["received"] == 0.0 and drafted["spent"] == 0.0 and drafted["balance"] == 0.0,
    )

    accepted.submit()
    submitted = get_project_financials(project)
    _check(
        "financials: the same receipt counts once submitted (positive control)",
        submitted["received"] == 100_000.00 and submitted["balance"] == 100_000.00,
    )

    # A second draft on a project that already has submitted events: the draft
    # must still be invisible, which is the case a "count everything" bug
    # would pass the first assertion on and fail here.
    _event(project, "Receipt", 777)
    _check(
        "financials: a draft alongside submitted events is still excluded",
        get_project_financials(project)["received"] == 100_000.00,
    )


def _csr_override_pattern_checks() -> None:
    """Refuse once with the figures; accept the exact acknowledgement; refuse
    a stale one after the amount changes."""
    project = _project("Override pattern checks", 1_000_000)
    _submitted(project, "Receipt", 200_000)

    overspend = _event(project, "Expenditure", 250_000)
    message = _throws_message(
        "override: an expenditure above what was received is refused",
        overspend.submit,
    )
    _check(
        "override: the refusal names both figures",
        "₹2,50,000.00" in message and "₹2,00,000.00" in message,
    )
    _check(
        "override: nothing was written — the event is still a draft",
        frappe.db.get_value("CSR Fund Event", overspend.name, "docstatus") == 0,
    )
    _check(
        "override: and the figures did not move",
        get_project_financials(project)["spent"] == 0.0,
    )

    acknowledgement = preview_warnings(project, "Expenditure", 250_000)["acknowledgement"]
    _check(
        "override: preview_warnings offers the same acknowledgement text",
        acknowledgement and acknowledgement in message,
    )

    overspend.reload()
    overspend.override_confirmed = 1
    overspend.override_acknowledges = acknowledgement
    _expect_throws(
        "override: a confirmation with no reason is refused (enforced in the domain)",
        overspend.submit,
    )

    overspend.reload()
    overspend.override_confirmed = 1
    overspend.override_reason = "Equipment supplier invoice fell due before tranche 2 landed."
    overspend.override_acknowledges = acknowledgement
    overspend.submit()
    _check(
        "override: the exact three override fields are accepted (positive control)",
        frappe.db.get_value("CSR Fund Event", overspend.name, "docstatus") == 1
        and get_project_financials(project)["spent"] == 250_000.00,
    )

    # The amount changes; the acknowledgement does not. The figures in the
    # stale string no longer describe what would happen, so it is refused.
    stale = _event(project, "Expenditure", 260_000)
    stale.override_confirmed = 1
    stale.override_reason = "Replaying the earlier acknowledgement."
    stale.override_acknowledges = acknowledgement
    stale_message = _throws_message(
        "override: replaying a stale acknowledgement after the amount changed is refused",
        stale.submit,
    )
    _check(
        "override: the refusal restates the warning as it now reads",
        "₹5,10,000.00" in stale_message,
    )

    fresh = preview_warnings(project, "Expenditure", 260_000)["acknowledgement"]
    stale.reload()
    stale.override_confirmed = 1
    stale.override_reason = "Second supplier invoice, same reason."
    stale.override_acknowledges = fresh
    stale.submit()
    _check(
        "override: the refreshed acknowledgement is accepted (positive control)",
        get_project_financials(project)["spent"] == 510_000.00,
    )

    # Receipts above the sanction warn too, and are overridable in the same way.
    sanction_project = _project("Receipt above sanction", 100_000)
    over_receipt = _event(sanction_project, "Receipt", 150_000)
    receipt_message = _throws_message(
        "override: a receipt above the sanction is refused until acknowledged",
        over_receipt.submit,
    )
    _check(
        "override: that refusal names the sanction and the receipt",
        "₹1,50,000.00" in receipt_message and "₹1,00,000.00" in receipt_message,
    )
    over_receipt.reload()
    over_receipt.override_confirmed = 1
    over_receipt.override_reason = "Funder released the full grant in one instalment."
    over_receipt.override_acknowledges = preview_warnings(
        sanction_project, "Receipt", 150_000
    )["acknowledgement"]
    over_receipt.submit()
    _check(
        "override: acknowledged, the over-sanction receipt is recorded (positive control)",
        get_project_financials(sanction_project)["received"] == 150_000.00,
    )

    # A project that is already over its sanction keeps warning on every later
    # entry, because the warning describes the *resulting* figures rather than
    # the delta. That is deliberate — the state is what needs acknowledging —
    # and it is asserted here so nobody "fixes" it into a delta check.
    later = _event(sanction_project, "Expenditure", 1_000)
    still_warned = _throws_message(
        "override: a later entry on an already-over-sanction project warns again "
        "(the warning describes the resulting state, not the delta)",
        later.submit,
    )
    _check(
        "override: and that warning still names the standing over-sanction figure",
        "₹1,50,000.00" in still_warned,
    )

    # An override answering nothing must not be recordable as though a warning
    # had been weighed. On a clean project there is nothing to answer.
    quiet_project = _project("No-warning checks", 100_000)
    _submitted(quiet_project, "Receipt", 50_000)
    quiet = _event(quiet_project, "Expenditure", 1_000)
    quiet.override_confirmed = 1
    quiet.override_reason = "No warning here."
    quiet.override_acknowledges = "invented"
    _expect_throws(
        "override: an override on an entry that produces no warning is refused",
        quiet.submit,
    )
    quiet.reload()
    quiet.submit()
    _check(
        "override: the same entry submits cleanly with no override (positive control)",
        get_project_financials(quiet_project)["spent"] == 1_000.00,
    )


def _csr_reversal_checks() -> None:
    project = _project("Reversal checks", 500_000)
    receipt = _submitted(project, "Receipt", 100_000)

    _expect_throws(
        "reversal: a reversal naming nothing is refused",
        lambda: _event(project, "Receipt Reversal", 10_000),
    )
    _expect_throws(
        "reversal: a non-reversal naming an event is refused",
        lambda: _event(project, "Receipt", 10_000, reverses=receipt.name),
    )
    _expect_throws(
        "reversal: an Expenditure Reversal cannot reverse a Receipt (kind mismatch)",
        lambda: _event(project, "Expenditure Reversal", 10_000, reverses=receipt.name),
    )

    other_project = _project("Reversal checks — other project", 500_000)
    _expect_throws(
        "reversal: a reversal cannot point at an event on another project",
        lambda: _event(other_project, "Receipt Reversal", 10_000, reverses=receipt.name),
    )

    draft_receipt = _event(project, "Receipt", 50_000)
    _expect_throws(
        "reversal: a reversal of an unsubmitted event is refused",
        lambda: _event(project, "Receipt Reversal", 10_000, reverses=draft_receipt.name),
    )

    too_big = _event(project, "Receipt Reversal", 150_000, reverses=receipt.name)
    ceiling_message = _throws_message(
        "reversal: a reversal above the original amount is refused",
        too_big.submit,
    )
    _check(
        "reversal: the refusal names the original and what remains",
        "₹1,00,000.00" in ceiling_message,
    )

    _submitted(project, "Receipt Reversal", 40_000, reverses=receipt.name)
    partial = get_project_financials(project)
    _check(
        "reversal: a partial reversal is accepted and the figures reflect it",
        partial["received"] == 60_000.00 and partial["balance"] == 60_000.00,
    )

    remainder_message = _throws_message(
        "reversal: a second reversal above what remains of the same event is refused",
        _event(project, "Receipt Reversal", 70_000, reverses=receipt.name).submit,
    )
    _check(
        "reversal: that refusal names the ₹60,000.00 still reversible",
        "₹60,000.00" in remainder_message,
    )

    _submitted(project, "Receipt Reversal", 60_000, reverses=receipt.name)
    _check(
        "reversal: reversing exactly the remainder is accepted (positive control)",
        get_project_financials(project)["received"] == 0.0,
    )


def _csr_no_cancel_checks() -> None:
    project = _project("No-cancel checks", 200_000)
    event = _submitted(project, "Receipt", 50_000)

    message = _throws_message(
        "ledger: cancelling a submitted fund event is refused",
        event.cancel,
    )
    _check(
        "ledger: the refusal points at the reversal entry instead",
        "Receipt Reversal" in message,
    )
    _check(
        "ledger: the event is still submitted and still counts",
        frappe.db.get_value("CSR Fund Event", event.name, "docstatus") == 1
        and get_project_financials(project)["received"] == 50_000.00,
    )

    _submitted(project, "Receipt Reversal", 50_000, reverses=event.name)
    _check(
        "ledger: the reversal route works and nets the figure to zero (positive control)",
        get_project_financials(project)["received"] == 0.0,
    )
    _check(
        "ledger: and both entries remain in the trail",
        frappe.db.count("CSR Fund Event", {"csr_project": project, "docstatus": 1}) == 2,
    )


def _csr_project_state_checks() -> None:
    closed = _project("Closed project checks", 100_000)
    _submitted(closed, "Receipt", 100_000)
    spend = _submitted(closed, "Expenditure", 10_000)
    _check(
        "project state: an Active project accepts expenditure (positive control)",
        get_project_financials(closed)["spent"] == 10_000.00,
    )

    frappe.db.set_value("CSR Project", closed, "status", "Closed")

    blocked = _event(closed, "Expenditure", 5_000)
    blocked.override_confirmed = 1
    blocked.override_reason = "Trying to force it through."
    blocked.override_acknowledges = "anything at all"
    _expect_throws(
        "project state: a Closed project refuses expenditure even with the override fields set",
        blocked.submit,
    )
    _submitted(closed, "Expenditure Reversal", 4_000, reverses=spend.name)
    _check(
        "project state: a Closed project still accepts a correcting reversal",
        get_project_financials(closed)["spent"] == 6_000.00,
    )

    cancelled = _project("Cancelled project checks", 100_000)
    ok_before = _submitted(cancelled, "Receipt", 20_000)
    _check(
        "project state: the project accepted a receipt before cancellation (positive control)",
        get_project_financials(cancelled)["received"] == 20_000.00,
    )
    frappe.db.set_value("CSR Project", cancelled, "status", "Cancelled")

    _expect_throws(
        "project state: a Cancelled project refuses a receipt",
        _event(cancelled, "Receipt", 1_000).submit,
    )
    _expect_throws(
        "project state: a Cancelled project refuses an expenditure",
        _event(cancelled, "Expenditure", 1_000).submit,
    )
    _expect_throws(
        "project state: a Cancelled project refuses even a reversal",
        _event(
            cancelled, "Receipt Reversal", 1_000, reverses=ok_before.name
        ).submit,
    )


def _csr_opportunity_checks() -> None:
    funder = _funder("Synthetic Funder for the pipeline")

    declined = frappe.get_doc(
        {
            "doctype": "CSR Opportunity",
            "funder": funder,
            "title": "Paediatric cath lab consumables",
            "stage": "In Discussion",
        }
    ).insert()
    declined.stage = "Declined"
    _expect_throws("opportunity: Declined with no reason is refused", declined.save)
    declined.reload()
    declined.stage = "Declined"
    declined.decline_reason = "Funder redirected its CSR budget to education."
    declined.save()
    _check(
        "opportunity: Declined with a reason is accepted (positive control)",
        frappe.db.get_value("CSR Opportunity", declined.name, "stage") == "Declined",
    )

    _expect_throws(
        "opportunity: converting a non-Sanctioned opportunity is refused",
        lambda: convert_to_project(declined.name),
    )

    opportunity = frappe.get_doc(
        {
            "doctype": "CSR Opportunity",
            "funder": funder,
            "title": "Neonatal ventilators",
            "stage": "Sanctioned",
            "expected_amount": 2_500_000,
            "decision_on": today(),
        }
    ).insert()

    result = convert_to_project(opportunity.name)
    project = frappe.get_doc("CSR Project", result["csr_project"])
    opportunity.reload()
    _check(
        "opportunity: convert_to_project creates the project and stamps the pointer",
        opportunity.converted_to == project.name
        and project.funder == funder
        and flt(project.sanctioned_amount, 2) == 2_500_000.00
        and project.status == "Active",
    )

    second_message = _throws_message(
        "opportunity: a second conversion is refused",
        lambda: convert_to_project(opportunity.name),
    )
    _check(
        "opportunity: the refusal names the project already created",
        project.name in second_message,
    )
    _check(
        "opportunity: and no second project was created",
        frappe.db.count("CSR Project", {"project_title": "Neonatal ventilators"}) == 1,
    )

    opportunity.reload()
    opportunity.converted_to = None
    _expect_throws(
        "opportunity: clearing converted_to by a direct save is refused",
        opportunity.save,
    )
    _expect_throws(
        "opportunity: creating one already converted is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "CSR Opportunity",
                "funder": funder,
                "title": "Born converted",
                "stage": "Sanctioned",
                "converted_to": project.name,
            }
        ).insert(),
    )


def _csr_obligation_checks() -> None:
    project = _project("Obligation checks", 300_000)

    overdue = frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Utilisation certificate for the first tranche",
            "due_on": add_days(today(), -1),
        }
    ).insert()
    due_today = frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Quarterly progress note",
            "due_on": today(),
        }
    ).insert()
    future = frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Annual impact summary",
            "due_on": add_days(today(), 1),
        }
    ).insert()

    _check(
        "obligation: due yesterday with nothing submitted is overdue",
        get_obligation_state(overdue.name)["overdue"] is True,
    )
    _check(
        "obligation: due today is not yet overdue",
        get_obligation_state(due_today.name)["overdue"] is False,
    )
    _check(
        "obligation: due tomorrow is not overdue",
        get_obligation_state(future.name)["overdue"] is False,
    )

    overdue.submitted_on = add_days(today(), -2)
    overdue.save()
    _check(
        "obligation: once submitted it is no longer overdue, however late the due date",
        get_obligation_state(overdue.name)["overdue"] is False,
    )
    _check(
        "obligation: overdue is derived, so the same row reads overdue as of an earlier date",
        get_obligation_state(future.name, as_of=add_days(today(), 5))["overdue"] is True,
    )

    _expect_throws(
        "obligation: verifying something with nothing submitted is refused",
        lambda: mark_verified(future.name),
    )
    _expect_throws(
        "obligation: creating one already verified is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "CSR Reporting Obligation",
                "csr_project": project,
                "description": "Born verified",
                "due_on": today(),
                "verified_by": "Administrator",
                "verified_on": today(),
            }
        ).insert(),
    )
    _expect_throws(
        "obligation: a verification date with no verifier is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "CSR Reporting Obligation",
                "csr_project": project,
                "description": "Date but nobody",
                "due_on": today(),
                "verified_on": today(),
            }
        ).insert(),
    )
    _expect_throws(
        "obligation: an unknown verifier is refused",
        lambda: mark_verified(overdue.name, verified_by="nobody@example.invalid"),
    )

    verified = mark_verified(overdue.name)
    state = get_obligation_state(overdue.name)
    _check(
        "obligation: mark_verified records a named person and a date (positive control)",
        verified["verified_by"] == "Administrator"
        and state["verified"] is True
        and state["verified_on"] == today(),
    )

    double_message = _throws_message(
        "obligation: a second verification is refused",
        lambda: mark_verified(overdue.name),
    )
    _check(
        "obligation: the refusal names who verified it",
        "Administrator" in double_message,
    )

    overdue.reload()
    overdue.verified_by = None
    _expect_throws(
        "obligation: clearing the verifier by a direct save is refused",
        overdue.save,
    )


def _csr_report_parity_checks() -> None:
    """The report and the document method must agree, because they share the
    one summing function. A second implementation is exactly how the two
    drift, so this asserts they have not."""
    project = _project(
        "Report parity checks",
        1_000_000,
        tranches=[
            {"expected_on": add_days(today(), -10), "expected_amount": 400_000},
            {"expected_on": add_days(today(), 30), "expected_amount": 600_000},
        ],
    )
    _submitted(project, "Receipt", 250_000)
    _submitted(project, "Expenditure", 100_000)
    frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Late utilisation certificate",
            "due_on": add_days(today(), -3),
        }
    ).insert()

    method = get_project_financials(project)
    _check(
        "financials: received/spent/balance computed from the ledger",
        method["received"] == 250_000.00
        and method["spent"] == 100_000.00
        and method["balance"] == 150_000.00,
    )
    _check(
        "financials: the first tranche is overdue and short by ₹1,50,000",
        method["tranches"][0]["overdue"] is True
        and method["tranches"][0]["shortfall"] == 150_000.00,
    )
    _check(
        "financials: the future tranche is not overdue",
        method["tranches"][1]["overdue"] is False,
    )
    _check(
        "financials: the obligation is reported overdue",
        method["obligations"][0]["overdue"] is True,
    )

    _columns, rows = report_execute({})
    row = next(r for r in rows if r["csr_project"] == project)
    _check(
        "report: the portfolio report agrees with the document method, figure for figure",
        row["received"] == method["received"]
        and row["spent"] == method["spent"]
        and row["balance"] == method["balance"]
        and row["sanctioned"] == method["sanctioned"],
    )
    _check(
        "report: and counts the same overdue tranche and report",
        row["overdue_tranches"] == 1 and row["overdue_reports"] == 1,
    )
    _check(
        "report: the funder filter still returns the project (positive control)",
        any(
            r["csr_project"] == project
            for r in report_execute({"funder": row["funder"]})[1]
        ),
    )
    _check(
        "report: a different funder's filter excludes it",
        not any(
            r["csr_project"] == project
            for r in report_execute({"funder": _funder("Unrelated funder")})[1]
        ),
    )


def _csr_no_stored_total_checks() -> None:
    """No aggregate is stored — asserted against the real table columns, the
    same way the reference build asserted it against information_schema."""
    forbidden = {
        "received",
        "received_total",
        "spent",
        "spent_total",
        "balance",
        "grand_total",
        "total_received",
        "total_spent",
    }
    columns = {row[0] for row in frappe.db.sql("DESCRIBE `tabCSR Project`")}
    _check(
        "no stored aggregate: CSR Project has no received/spent/balance column",
        not (columns & forbidden),
    )
    _check(
        "no stored aggregate: it does carry the sanction, which is a declaration not a rollup",
        "sanctioned_amount" in columns,
    )

    tranche_columns = {row[0] for row in frappe.db.sql("DESCRIBE `tabCSR Tranche`")}
    _check(
        "no stored aggregate: CSR Tranche stores no received figure and no overdue flag",
        not (tranche_columns & {"received", "received_to_date", "overdue", "status"}),
    )

    obligation_columns = {
        row[0] for row in frappe.db.sql("DESCRIBE `tabCSR Reporting Obligation`")
    }
    _check(
        "no stored aggregate: CSR Reporting Obligation stores no overdue flag",
        "overdue" not in obligation_columns,
    )


def _csr_lock_order_checks() -> None:
    """Genuine concurrency is not drivable from a single bench process (the
    same limitation Phase 2 documented), so this is the code-level stand-in
    the audit asked for: the FOR UPDATE read of the project row must come
    before anything is summed, and the sums themselves must be locking reads
    — a non-locking read after the lock is served from the pre-lock snapshot
    under REPEATABLE READ, which is the hole the Phase 2 re-audit found.
    """
    source = inspect.getsource(CSRFundEvent._check_against_locked_project)
    lock_at = source.find("for_update=True")
    totals_at = source.find("project_event_totals(")
    _check(
        "p3-lock: the project row is locked (for_update) before any sum is taken",
        lock_at != -1 and totals_at != -1 and lock_at < totals_at,
    )
    _check(
        "p3-lock: the totals read inside the lock is itself a locking read",
        "project_event_totals(self.csr_project, for_update=True)" in source,
    )

    ceiling_source = inspect.getsource(CSRFundEvent._check_reversal_ceiling)
    _check(
        "p3-lock: the reversal ceiling sums prior reversals with FOR UPDATE",
        "FOR UPDATE" in ceiling_source,
    )

    convert_source = inspect.getsource(convert_to_project)
    lock_at2 = convert_source.find("for_update=True")
    insert_at2 = convert_source.find('"doctype": "CSR Project"')
    _check(
        "p3-lock: convert_to_project locks its own row before inserting the project",
        lock_at2 != -1 and insert_at2 != -1 and lock_at2 < insert_at2,
    )

    verify_source = inspect.getsource(mark_verified)
    lock_at3 = verify_source.find("for_update=True")
    save_at3 = verify_source.find("doc.save()")
    _check(
        "p3-lock: mark_verified locks its own row before stamping the verification",
        lock_at3 != -1 and save_at3 != -1 and lock_at3 < save_at3,
    )


# ---------------------------------------------------------------------------
# Codex Phase 3 audit — six findings
# ---------------------------------------------------------------------------
#
# Two P1s were claimed and both are disproven against this container's actual
# frappe v16 source; the probes below prove it on the running site rather than
# on a reading. The four remaining findings were valid and are fixed; each has
# its check here.


def _csr_audit_p1_immutability_checks() -> None:
    """P1-a and P1-b: a submitted event can be neither deleted nor mutated.

    **P1-a claimed** that the JSON's ``delete: 1`` makes submitted rows
    deletable, bypassing the cancel refusal. It does not.
    ``apps/frappe/frappe/model/delete_doc.py:289-297`` refuses any submittable
    doctype whose ``docstatus.is_submitted()`` — "Submitted Record cannot be
    deleted. You must Cancel it first", ``raise_exception=True`` — and since
    ``before_cancel``/``on_cancel`` throw, cancellation is impossible, so the
    deletion is transitively impossible. An ``on_trash`` guard was added
    anyway as defence in depth.

    **P1-b claimed** that ``frappe.client.set_value`` can mutate a submitted
    row's amount, kind or project. It cannot: ``client.py:207-215`` ends in
    ``doc.save()``, ``document.py:597-598`` routes a submitted save through
    ``validate_update_after_submit()``, and
    ``base_document.py:1270-1305`` throws ``frappe.UpdateAfterSubmitError``
    for any field changed without ``allow_on_submit`` — and no field on this
    doctype has it.
    """
    project = _project("Immutability checks", 500_000)
    event = _submitted(project, "Receipt", 100_000)

    _expect_throws(
        "p1-a: deleting a submitted fund event is refused (core delete_doc.py:289-297)",
        lambda: frappe.delete_doc("CSR Fund Event", event.name),
    )
    _check(
        "p1-a: the event survived the deletion attempt and still counts",
        frappe.db.exists("CSR Fund Event", event.name)
        and get_project_financials(project)["received"] == 100_000.00,
    )

    draft = _event(project, "Receipt", 5_000)
    frappe.delete_doc("CSR Fund Event", draft.name)
    _check(
        "p1-a: a draft still deletes normally, and removes nothing from the figures "
        "(positive control)",
        not frappe.db.exists("CSR Fund Event", draft.name)
        and get_project_financials(project)["received"] == 100_000.00,
    )

    _expect_throws(
        "p1-b: frappe.client.set_value cannot change a submitted event's amount "
        "(UpdateAfterSubmitError)",
        lambda: client_set_value("CSR Fund Event", event.name, "amount", 999_999),
    )
    _expect_throws(
        "p1-b: nor its kind",
        lambda: client_set_value("CSR Fund Event", event.name, "kind", "Expenditure"),
    )
    _expect_throws(
        "p1-b: nor the project it belongs to",
        lambda: client_set_value("CSR Fund Event", event.name, "csr_project", "CSRP-00001"),
    )
    _check(
        "p1-b: the stored row is untouched after all three attempts",
        flt(frappe.db.get_value("CSR Fund Event", event.name, "amount"), 2) == 100_000.00
        and frappe.db.get_value("CSR Fund Event", event.name, "kind") == "Receipt"
        and frappe.db.get_value("CSR Fund Event", event.name, "csr_project") == project,
    )

    # Positive control for the mechanism itself: the same endpoint edits a
    # DRAFT freely, so the refusals above are the submitted state doing the
    # work rather than set_value being broken.
    editable = _event(project, "Receipt", 1_000)
    client_set_value("CSR Fund Event", editable.name, "amount", 2_000)
    _check(
        "p1-b: the same endpoint edits a draft freely (positive control)",
        flt(frappe.db.get_value("CSR Fund Event", editable.name, "amount"), 2) == 2_000.00,
    )

    _check(
        "p1-a: on_trash refuses a submitted event in this app's own code, not only core's",
        "docstatus == 1" in inspect.getsource(CSRFundEvent.on_trash),
    )

    # frappe.db.set_value is a raw UPDATE and genuinely bypasses all of this.
    # It is not REST-whitelisted and is reachable only by server-side code or
    # a bench console, which no app-level guard can prevent — the same class of
    # residual as "whoever has the MariaDB root password can edit the table".
    # Asserted here so the residual is visible rather than assumed away.
    frappe.db.set_value("CSR Fund Event", event.name, "amount", 123, update_modified=False)
    _check(
        "p1-b: frappe.db.set_value DOES bypass the controller — documented residual, "
        "not REST-reachable",
        flt(frappe.db.get_value("CSR Fund Event", event.name, "amount"), 2) == 123.00,
    )
    frappe.db.set_value("CSR Fund Event", event.name, "amount", 100_000, update_modified=False)


def _csr_audit_p2_locked_state_checks() -> None:
    """P2-a: the project state must be decided on the locked read.

    P2-b: the report must list projects with get_list, not get_all.
    """
    project = _project("Locked state checks", 200_000)
    _submitted(project, "Receipt", 200_000)

    # The draft is created while the project is Active; the project is then
    # closed underneath it, exactly as a concurrent Close would. The submit
    # must see the current state, not the one that held when the draft was
    # written.
    pending = _event(project, "Expenditure", 10_000)
    frappe.db.set_value("CSR Project", project, "status", "Closed")
    _expect_throws(
        "p2-a: a draft written while Active is refused once the project is Closed "
        "underneath it",
        pending.submit,
    )

    frappe.db.set_value("CSR Project", project, "status", "Active")
    pending.reload()
    pending.submit()
    _check(
        "p2-a: the same draft submits once the project is Active again (positive control)",
        get_project_financials(project)["spent"] == 10_000.00,
    )

    source = inspect.getsource(CSRFundEvent._check_against_locked_project)
    lock_at = source.find("for_update=True")
    state_at = source.find("self._check_project_state(")
    _check(
        "p2-a: the state refusal runs after the locking read, not before it",
        lock_at != -1 and state_at != -1 and lock_at < state_at,
    )
    _check(
        "p2-a: and it is fed the status from that locked read, not a fresh query",
        "self._check_project_state(project.status)" in source
        and "get_value" not in inspect.getsource(CSRFundEvent._check_project_state),
    )

    report_source = inspect.getsource(report_get_data)
    after_get_list = report_source.split("frappe.get_list(")
    _check(
        "p2-b: the report lists projects with get_list, which applies permission "
        "query conditions",
        len(after_get_list) == 2 and after_get_list[1].lstrip().startswith('"CSR Project"'),
    )
    _check(
        "p2-b: and no longer with get_all, which ignores them",
        not any(
            part.lstrip().startswith('"CSR Project"')
            for part in report_source.split("frappe.get_all(")[1:]
        ),
    )


def _csr_audit_p3_precision_and_tie_checks() -> None:
    """P3-a: sub-paise amounts. P3-b: tied tranche dates."""
    project = _project("Precision checks", 100_000)

    _expect_throws(
        "p3-a: an amount of 0.004 is refused — it would display as ₹0.00 in every total",
        lambda: _event(project, "Receipt", 0.004),
    )
    _expect_throws(
        "p3-a: 0.005 is refused too (rounding it would change the figure entered)",
        lambda: _event(project, "Receipt", 0.005),
    )
    _expect_throws(
        "p3-a: and three decimals on a real amount is refused",
        lambda: _event(project, "Receipt", 1_000.123),
    )
    accepted = _submitted(project, "Receipt", 100.25)
    _check(
        "p3-a: two decimal places are accepted and land exactly (positive control)",
        get_project_financials(project)["received"] == 100.25,
    )
    _check(
        "p3-a: one paise is a legitimate amount",
        flt(accepted.amount, 2) == 100.25,
    )

    # Two tranches expected on the same day: with date alone, which one
    # carries the shortfall depended on row arrival order. It must now follow
    # document order — the first row on the form is filled first.
    tied_date = add_days(today(), -5)
    tied = _project(
        "Tied tranche dates",
        300_000,
        tranches=[
            {"expected_on": tied_date, "expected_amount": 100_000, "note": "first row"},
            {"expected_on": tied_date, "expected_amount": 200_000, "note": "second row"},
        ],
    )
    _submitted(tied, "Receipt", 100_000)

    states = get_project_financials(tied)["tranches"]
    _check(
        "p3-b: tied dates allocate in document order — the first row is satisfied",
        states[0]["received_to_date"] == 100_000.00 and states[0]["shortfall"] == 0.0,
    )
    _check(
        "p3-b: and the second row carries the whole shortfall",
        states[1]["shortfall"] == 200_000.00 and states[1]["overdue"] is True,
    )
    _check(
        "p3-b: the first row is therefore not overdue despite the past date",
        states[0]["overdue"] is False,
    )

    repeated = [
        tuple(state["shortfall"] for state in get_project_financials(tied)["tranches"])
        for _repeat in range(3)
    ]
    _check(
        "p3-b: repeated reads of unchanged data give the identical allocation",
        len(set(repeated)) == 1,
    )

    _columns, rows = report_execute({})
    row = next(r for r in rows if r["csr_project"] == tied)
    _check(
        "p3-b: the report counts the same single overdue tranche",
        row["overdue_tranches"] == 1,
    )


# ---------------------------------------------------------------------------
# Phase 4 — Research
# ---------------------------------------------------------------------------
#
# Same shape as Phases 2 and 3: plain assertions, every negative carrying a
# positive control, everything rolled back at the end. §4.3 holds throughout
# — no participant identifier appears in any fixture here, and
# ``_research_participant_identifier_guard_check`` makes that a test rather
# than a hope.


def run_phase4_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _research_milestone_checks()
        _research_milestone_direct_save_guard_checks()
        _research_ethics_decision_checks()
        _research_ethics_state_machine_checks()
        _research_ethics_standing_checks()
        _research_report_parity_checks()
        _research_participant_identifier_guard_check()
    finally:
        frappe.db.rollback()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        frappe.throw(f"Failures: {_FAIL}")


def _study(title: str, status: str = "Active", milestones=None) -> str:
    return (
        frappe.get_doc(
            {
                "doctype": "Research Study",
                "study_title": title,
                "principal_investigator": "Dr. Synthetic Investigator",
                "department": "Cardiology",
                "status": status,
                "commenced_on": today(),
                "milestones": milestones or [],
            }
        )
        .insert()
        .name
    )


def _submission(study: str, committee: str = "IEC", submitted_on=None) -> str:
    return (
        frappe.get_doc(
            {
                "doctype": "Research Ethics Submission",
                "study": study,
                "committee": committee,
                "submitted_on": submitted_on or today(),
            }
        )
        .insert()
        .name
    )


def _research_milestone_checks() -> None:
    study = _study(
        "Milestone checks",
        milestones=[{"description": "Submit protocol to IEC", "due_on": add_days(today(), 7)}],
    )
    doc = frappe.get_doc("Research Study", study)
    row_name = doc.milestones[0].name

    _expect_throws(
        "milestone: an unknown row name is refused",
        lambda: complete_milestone(study, "not-a-real-row"),
    )

    result = complete_milestone(study, row_name)
    doc.reload()
    _check(
        "milestone: complete_milestone stamps today's date (positive control)",
        str(doc.milestones[0].completed_on) == today() and result["completed_on"] == str(today()),
    )

    message = _throws_message(
        "milestone: completing an already-completed milestone is refused",
        lambda: complete_milestone(study, row_name),
    )
    _check(
        "milestone: the refusal names the date it was completed on",
        str(today()) in message,
    )

    terminated = _study(
        "Terminated study milestone checks",
        status="Terminated",
        milestones=[{"description": "Never gets here", "due_on": today()}],
    )
    terminated_doc = frappe.get_doc("Research Study", terminated)
    _expect_throws(
        "milestone: completion on a Terminated study is refused",
        lambda: complete_milestone(terminated, terminated_doc.milestones[0].name),
    )

    completed_status = _study(
        "Completed study milestone checks",
        status="Completed",
        milestones=[{"description": "Also never gets here", "due_on": today()}],
    )
    completed_doc = frappe.get_doc("Research Study", completed_status)
    _expect_throws(
        "milestone: completion on a Completed study is refused",
        lambda: complete_milestone(completed_status, completed_doc.milestones[0].name),
    )

    active = _study(
        "Active study milestone checks",
        status="Active",
        milestones=[{"description": "Completes fine", "due_on": today()}],
    )
    active_doc = frappe.get_doc("Research Study", active)
    complete_milestone(active, active_doc.milestones[0].name)
    active_doc.reload()
    _check(
        "milestone: completion on an Active study is accepted (positive control)",
        str(active_doc.milestones[0].completed_on) == today(),
    )


def _research_milestone_direct_save_guard_checks() -> None:
    """Codex Phase 4 audit, High: nothing stopped a direct parent save from
    setting a milestone's completed_on itself, bypassing complete_milestone's
    once-only rule and its Terminated/Completed refusal entirely — the same
    class of gap as Phase 2's P3-2. validate() now guards it."""
    study = _study(
        "Direct-save guard checks",
        milestones=[{"description": "Guarded milestone", "due_on": today()}],
    )
    doc = frappe.get_doc("Research Study", study)
    row_name = doc.milestones[0].name

    doc.milestones[0].completed_on = today()
    _expect_throws(
        "milestone guard: a direct save flipping completed_on (empty -> set) is refused",
        doc.save,
    )
    doc.reload()
    _check(
        "milestone guard: and nothing was written",
        doc.milestones[0].completed_on is None,
    )

    complete_milestone(study, row_name)
    doc.reload()
    _check(
        "milestone guard: complete_milestone itself still works after the guard "
        "(positive control)",
        str(doc.milestones[0].completed_on) == today(),
    )

    doc.milestones[0].completed_on = None
    _expect_throws(
        "milestone guard: a direct save clearing a stored completed_on is refused",
        doc.save,
    )
    doc.reload()
    _check(
        "milestone guard: and the completion survived the attempt",
        str(doc.milestones[0].completed_on) == today(),
    )

    doc.milestones = [m for m in doc.milestones if m.name != row_name]
    _expect_throws(
        "milestone guard: deleting a completed milestone row is refused — that "
        "would erase completion history",
        doc.save,
    )
    doc.reload()
    _check(
        "milestone guard: the completed row is still present",
        any(m.name == row_name for m in doc.milestones),
    )

    doc.append("milestones", {"description": "A second, incomplete milestone", "due_on": today()})
    doc.save()
    doc.reload()
    _check(
        "milestone guard: adding a new incomplete milestone via a normal save still "
        "works (positive control)",
        len(doc.milestones) == 2
        and any(m.description == "A second, incomplete milestone" for m in doc.milestones),
    )

    new_row_name = next(
        m.name for m in doc.milestones if m.description == "A second, incomplete milestone"
    )
    doc.milestones = [m for m in doc.milestones if m.name != new_row_name]
    doc.save()
    doc.reload()
    _check(
        "milestone guard: removing an incomplete milestone via a normal save still "
        "works (positive control)",
        len(doc.milestones) == 1,
    )

    forged = frappe.get_doc(
        {
            "doctype": "Research Study",
            "study_title": "Born with a forged completion",
            "status": "Active",
            "milestones": [
                {"description": "Forged at birth", "due_on": today(), "completed_on": today()}
            ],
        }
    )
    _expect_throws(
        "milestone guard: a study cannot be created with a milestone already completed",
        forged.insert,
    )


def _research_ethics_decision_checks() -> None:
    study = _study("Ethics decision checks")
    submission = _submission(study)

    _expect_throws(
        "ethics: Approved with no valid_until is refused",
        lambda: record_decision(submission, "Approved"),
    )
    _check(
        "ethics: and nothing was written — still Pending",
        frappe.db.get_value("Research Ethics Submission", submission, "decision") == "Pending",
    )

    result = record_decision(submission, "Approved", valid_until=add_days(today(), 365))
    _check(
        "ethics: Approved with valid_until is accepted (positive control)",
        result["decision"] == "Approved" and result["valid_until"] == str(add_days(today(), 365)),
    )

    message = _throws_message(
        "ethics: a second decision on the same submission is refused",
        lambda: record_decision(submission, "Rejected", decision_note="Too late"),
    )
    _check(
        "ethics: the refusal names the first decision and its date",
        "Approved" in message and str(today()) in message,
    )

    rejected_submission = _submission(study, committee="Second IEC")
    _expect_throws(
        "ethics: Rejected with no decision_note is refused",
        lambda: record_decision(rejected_submission, "Rejected"),
    )
    rejected_result = record_decision(
        rejected_submission, "Rejected", decision_note="Insufficient community engagement plan."
    )
    _check(
        "ethics: Rejected with a decision_note is accepted (positive control)",
        rejected_result["decision"] == "Rejected",
    )


def _research_ethics_state_machine_checks() -> None:
    """Codex-style lesson from Phase 2 (P3-2): read_only in the JSON is a UI
    hint only. A direct save must not move decision/decided_on/valid_until,
    and a document must not be insertable already decided."""
    study = _study("Ethics state machine checks")

    _expect_throws(
        "ethics: inserting a submission already Approved is refused",
        lambda: frappe.get_doc(
            {
                "doctype": "Research Ethics Submission",
                "study": study,
                "committee": "IEC",
                "submitted_on": today(),
                "decision": "Approved",
                "decided_on": today(),
                "valid_until": add_days(today(), 365),
            }
        ).insert(),
    )

    submission = _submission(study)
    doc = frappe.get_doc("Research Ethics Submission", submission)
    doc.decision = "Approved"
    doc.decided_on = today()
    doc.valid_until = add_days(today(), 365)
    _expect_throws(
        "ethics: a direct save flipping Pending -> Approved is refused (state machine)",
        doc.save,
    )

    doc.reload()
    result = record_decision(submission, "Approved", valid_until=add_days(today(), 365))
    _check(
        "ethics: the whitelisted record_decision path still works end-to-end "
        "(positive control)",
        result["decision"] == "Approved"
        and frappe.db.get_value("Research Ethics Submission", submission, "decision")
        == "Approved",
    )

    doc.reload()
    doc.decision_reference = "Forged reference"
    _expect_throws(
        "ethics: a direct save changing decision_reference after the decision is refused",
        doc.save,
    )


def _research_ethics_standing_checks() -> None:
    """none -> pending -> approved -> expired, and the renewal path."""
    study = _study("Ethics standing lifecycle")

    _check(
        "standing: a fresh study with no submissions reads none",
        ethics_standing(study)["status"] == "none",
    )

    submission = _submission(study)
    _check(
        "standing: a Pending submission reads pending",
        ethics_standing(study)["status"] == "pending"
        and ethics_standing(study)["submission"] == submission,
    )

    record_decision(submission, "Approved", valid_until=add_days(today(), 30))
    approved_standing = ethics_standing(study)
    _check(
        "standing: an Approved decision with a future valid_until reads approved",
        approved_standing["status"] == "approved"
        and approved_standing["submission"] == submission,
    )

    expired_as_of = add_days(today(), 31)
    expired_standing = ethics_standing(study, as_of=expired_as_of)
    _check(
        "standing: as_of after valid_until reads expired",
        expired_standing["status"] == "expired"
        and expired_standing["submission"] == submission,
    )
    _check(
        "standing: and reading as_of today still reads approved (derived, not stored)",
        ethics_standing(study)["status"] == "approved",
    )

    renewal = _submission(study, committee="IEC", submitted_on=expired_as_of)
    _check(
        "standing: a fresh Pending renewal on an expired study reads pending, not "
        "expired (the renewal path)",
        ethics_standing(study, as_of=expired_as_of)["status"] == "pending"
        and ethics_standing(study, as_of=expired_as_of)["submission"] == renewal,
    )

    get_study_standing_result = get_study_standing(study)
    _check(
        "standing: the whitelisted get_study_standing agrees with ethics_standing directly",
        get_study_standing_result["status"] == ethics_standing(study)["status"],
    )


def _research_report_parity_checks() -> None:
    """The report and ethics_standing must agree, because they share the one
    function — a second implementation is exactly how the two would drift."""
    approved_study = _study("Report parity — approved")
    approved_submission = _submission(approved_study)
    record_decision(approved_submission, "Approved", valid_until=add_days(today(), 60))

    pending_study = _study("Report parity — pending")
    _submission(pending_study)

    none_study = _study("Report parity — none")

    rejected_study = _study("Report parity — rejected")
    rejected_submission = _submission(rejected_study)
    record_decision(rejected_submission, "Rejected", decision_note="Not viable.")

    _columns, rows = research_report_execute({})
    by_name = {row["study"]: row for row in rows}

    for study, expected_status in (
        (approved_study, "approved"),
        (pending_study, "pending"),
        (none_study, "none"),
        (rejected_study, "none"),
    ):
        standing = ethics_standing(study)
        _check(
            f"report: {expected_status} study's report row agrees with ethics_standing",
            by_name[study]["standing"] == standing["status"] == expected_status
            and by_name[study]["latest_submission"] == standing.get("submission"),
        )

    approved_row = by_name[approved_study]
    _check(
        "report: days_to_expiry is computed and positive for the approved study",
        approved_row["days_to_expiry"] == 60,
    )


def _research_participant_identifier_guard_check() -> None:
    """§4.3, as a test rather than a hope: no field on any Hospital Ops
    doctype may look like a research-participant identifier, patient field,
    diagnosis, consent flag or enrolment marker — including one bolted on
    later as a Custom Field.

    The positive control proves the scan mechanism itself works: run
    unfiltered (no module restriction) on a site where the `healthcare` app
    is installed, and it must find Patient-shaped fields. A scan that always
    returns empty would make the Hospital Ops assertion below meaningless.
    """
    unfiltered_matches = find_participant_identifier_fields()
    _check(
        "data boundary: the unfiltered scan finds participant-shaped fields "
        "elsewhere on this site (positive control — proves the scan works)",
        len(unfiltered_matches) > 0
        and any(m["doctype"] == "Patient" for m in unfiltered_matches),
    )

    hospital_ops_matches = find_participant_identifier_fields(module="Hospital Ops")
    _check(
        "data boundary: zero participant-identifier-shaped fields anywhere in the "
        "Hospital Ops module (§4.3)",
        len(hospital_ops_matches) == 0,
    )
    if hospital_ops_matches:
        print("data boundary violations:", hospital_ops_matches)


# ---------------------------------------------------------------------------
# Phase 5 — Build & Publish (signage, website, software)
# ---------------------------------------------------------------------------
#
# Same shape as Phases 2, 3 and 4: plain assertions, every negative carrying a
# positive control, everything rolled back at the end. This phase is
# gate-heavy, so most negatives assert the *content* of the refusal too — a
# refusal that does not name the missing prerequisite leaves the user guessing,
# and the thing they guess is usually to record the step somewhere else.


def run_phase5_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _sign_sequence_gate_checks()
        _sign_note_and_waiver_checks()
        _sign_chronological_order_checks()
        _sign_supersede_checks()
        _sign_design_immutability_checks()
        _sign_draft_and_foreign_design_checks()
        _sign_waiver_semantics_checks()
        _sign_append_only_checks()
        _sign_accessibility_checks()
        _web_publication_gate_checks()
        _web_publication_date_bounding_checks()
        _web_step_direct_save_guard_checks()
        _software_requirement_and_uat_checks()
        _software_release_gate_checks()
        _software_release_terminal_checks()
        _software_recorded_fact_immutability_checks()
        _build_publish_report_parity_checks()
    finally:
        frappe.db.rollback()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        frappe.throw(f"Failures: {_FAIL}")


# -- fixtures ---------------------------------------------------------------


def _sign(reference: str, **extra) -> str:
    return (
        frappe.get_doc(
            {
                "doctype": "Hospital Sign",
                "reference": reference,
                "purpose": extra.pop("purpose", "Synthetic wayfinding sign"),
                "building": extra.pop("building", "Block A"),
                "floor": extra.pop("floor", "Ground"),
                **extra,
            }
        )
        .insert()
        .name
    )


def _sign_event(sign, design, step, outcome="Passed", occurred_on=None, note=None, submit=True):
    doc = frappe.get_doc(
        {
            "doctype": "Hospital Sign Event",
            "sign": sign,
            "design": design,
            "step": step,
            "outcome": outcome,
            "occurred_on": occurred_on or today(),
            "note": note,
        }
    )
    doc.insert()
    if submit:
        doc.submit()
    return doc


def _pass_chain(sign, design, start_offset=0):
    """Content Verification → Approval → Print Proof, each a day apart."""
    for index, step in enumerate(("Content Verification", "Approval", "Print Proof")):
        _sign_event(sign, design, step, occurred_on=add_days(today(), start_offset + index))


def _web_page(title: str) -> str:
    return (
        frappe.get_doc(
            {"doctype": "Hospital Web Page", "page_title": title, "url_path": "/synthetic"}
        )
        .insert()
        .name
    )


def _software(title: str, **extra) -> str:
    return (
        frappe.get_doc(
            {"doctype": "Software Project Record", "project_title": title, **extra}
        )
        .insert()
        .name
    )


# -- signage ----------------------------------------------------------------


def _sign_sequence_gate_checks() -> None:
    """SIG-002's existence gate: a step passes only when everything before it
    has cleared for the current design, and the refusal names what is missing."""
    sign = _sign("SYN-SEQ-1")
    first = add_design(sign)
    _check(
        "signage: the first design is version 1 and needs no supersede reason "
        "(positive control)",
        first["version_number"] == 1 and first["superseded"] is None,
    )
    _check(
        "signage: a sign with no cleared steps derives status Planned",
        sign_status(sign) == "Planned",
    )

    message = _throws_message(
        "signage: a passing Production before anything else is refused",
        lambda: _sign_event(sign, first["design"], "Production"),
    )
    for missing in ("Content Verification", "Approval", "Print Proof"):
        _check(
            f"signage: the Production refusal names the missing {missing} step",
            missing in message,
        )
    _check(
        "signage: the Production refusal says the prerequisite was not recorded",
        "Not recorded" in message,
    )

    _expect_throws(
        "signage: a passing Approval before Content Verification is refused",
        lambda: _sign_event(sign, first["design"], "Approval"),
    )
    _expect_throws(
        "signage: a passing Installation before Production is refused",
        lambda: _sign_event(sign, first["design"], "Installation"),
    )

    _sign_event(sign, first["design"], "Content Verification", occurred_on=add_days(today(), 0))
    _check(
        "signage: Content Verification passes with nothing before it (positive control)",
        sign_readiness(sign)["steps"]["Content Verification"]["state"] == "passed",
    )
    _sign_event(sign, first["design"], "Approval", occurred_on=add_days(today(), 1))
    _sign_event(sign, first["design"], "Print Proof", occurred_on=add_days(today(), 2))
    _check(
        "signage: with the chain cleared there are no production blockers",
        sign_readiness(sign)["production_blockers"] == [],
    )

    _sign_event(sign, first["design"], "Production", occurred_on=add_days(today(), 3))
    _check(
        "signage: Production passes once the chain is cleared (positive control), and "
        "status derives to In Production",
        sign_status(sign) == "In Production",
    )

    _sign_event(sign, first["design"], "Installation", occurred_on=add_days(today(), 4))
    _check(
        "signage: Installation passes after Production and status derives to Installed",
        sign_status(sign) == "Installed",
    )


def _sign_note_and_waiver_checks() -> None:
    sign = _sign("SYN-NOTE-1")
    design = add_design(sign)["design"]

    _expect_throws(
        "signage: a Failed step with no note is refused",
        lambda: _sign_event(sign, design, "Content Verification", outcome="Failed"),
    )
    _expect_throws(
        "signage: a Waived step with no note is refused",
        lambda: _sign_event(sign, design, "Content Verification", outcome="Waived"),
    )

    _sign_event(
        sign, design, "Content Verification", outcome="Failed", note="Ward name misspelt"
    )
    _check(
        "signage: a Failed step with a note is recorded (positive control) and does not clear",
        sign_readiness(sign)["steps"]["Content Verification"]["state"] == "failed",
    )
    _expect_throws(
        "signage: Approval is still refused while Content Verification stands failed",
        lambda: _sign_event(sign, design, "Approval"),
    )

    _sign_event(
        sign,
        design,
        "Content Verification",
        outcome="Waived",
        note="Wording carried over verbatim from the approved v0 sign",
    )
    _check(
        "signage: a Waived step with a note clears the gate the way a pass does",
        sign_readiness(sign)["steps"]["Content Verification"]["state"] == "waived",
    )
    _sign_event(sign, design, "Approval")
    _check(
        "signage: Approval passes on the strength of a waiver (positive control)",
        sign_readiness(sign)["steps"]["Approval"]["state"] == "passed",
    )


def _sign_chronological_order_checks() -> None:
    """SIG-002's chronological half — sequenceViolation in the reference. An
    approval dated after a production event is indistinguishable, once
    recorded, from a correctly sequenced pair."""
    sign = _sign("SYN-ORDER-1")
    design = add_design(sign)["design"]

    _sign_event(sign, design, "Content Verification", occurred_on=add_days(today(), 5))

    message = _throws_message(
        "signage: an Approval dated before the Content Verification it depends on is refused",
        lambda: _sign_event(sign, design, "Approval", occurred_on=add_days(today(), 3)),
    )
    _check(
        "signage: the order refusal names both dates",
        str(add_days(today(), 3)) in message and str(add_days(today(), 5)) in message,
    )

    _sign_event(sign, design, "Approval", occurred_on=add_days(today(), 5))
    _check(
        "signage: an Approval on the same day as the verification is accepted "
        "(positive control — only strictly-before is refused)",
        sign_readiness(sign)["steps"]["Approval"]["state"] == "passed",
    )

    _expect_throws(
        "signage: a Print Proof dated before its Approval is refused",
        lambda: _sign_event(sign, design, "Print Proof", occurred_on=add_days(today(), 4)),
    )
    _sign_event(sign, design, "Print Proof", occurred_on=add_days(today(), 6))
    _expect_throws(
        "signage: Production dated before the Print Proof it depends on is refused",
        lambda: _sign_event(sign, design, "Production", occurred_on=add_days(today(), 5)),
    )
    _sign_event(sign, design, "Production", occurred_on=add_days(today(), 7))
    _check(
        "signage: Production dated after every prerequisite is accepted (positive control)",
        sign_status(sign) == "In Production",
    )


def _sign_supersede_checks() -> None:
    sign = _sign("SYN-SUP-1")
    v1 = add_design(sign)["design"]
    _pass_chain(sign, v1)
    _check(
        "signage: v1 is ready for production before it is superseded (positive control)",
        sign_readiness(sign)["production_blockers"] == [],
    )

    _expect_throws(
        "signage: adding a second design without a supersede reason is refused",
        lambda: add_design(sign, content_text="v2 wording"),
    )

    result = add_design(sign, content_text="v2 wording", supersede_reason="Department renamed")
    v2 = result["design"]
    _check(
        "signage: the second design is version 2 and supersedes version 1",
        result["version_number"] == 2 and result["superseded_version"] == 1,
    )
    _check(
        "signage: the supersede notice says approvals do not carry forward",
        "do not carry forward" in result["notice"],
    )
    _check(
        "signage: v1's supersede reason is stored on the superseded design",
        frappe.db.get_value("Hospital Sign Design", v1, "supersede_reason") == "Department renamed",
    )

    readiness = sign_readiness(sign)
    _check(
        "signage: v1's approval reads for_an_older_design against v2",
        readiness["steps"]["Approval"]["state"] == "for_an_older_design",
    )
    message = _throws_message(
        "signage: Production on v2 is refused — v1's approvals do not authorise it",
        lambda: _sign_event(sign, v2, "Production"),
    )
    _check(
        "signage: the refusal explains the steps were against a superseded design",
        "superseded" in message,
    )

    not_current = _throws_message(
        "signage: a passing Production naming the superseded v1 is refused outright",
        lambda: _sign_event(sign, v1, "Production"),
    )
    _check(
        "signage: the not-current-design refusal names the current version",
        "version 2" in not_current,
    )
    _expect_throws(
        "signage: a passing Installation naming the superseded v1 is refused too",
        lambda: _sign_event(sign, v1, "Installation"),
    )

    failed = _sign_event(
        sign, v1, "Production", outcome="Failed", note="Print shop rejected the old artwork"
    )
    _check(
        "signage: a FAILED Production on the superseded v1 is still recordable "
        "(positive control — the record may need to say the old design failed)",
        failed.docstatus == 1,
    )

    _expect_throws(
        "signage: a design cannot be inserted directly, bypassing add_design",
        lambda: frappe.get_doc(
            {"doctype": "Hospital Sign Design", "sign": sign, "version_number": 99}
        ).insert(),
    )

    superseded_sign = _sign("SYN-SUP-2")
    s_v1 = add_design(superseded_sign)["design"]
    _pass_chain(superseded_sign, s_v1)
    _sign_event(superseded_sign, s_v1, "Production", occurred_on=add_days(today(), 3))
    _sign_event(superseded_sign, s_v1, "Installation", occurred_on=add_days(today(), 4))
    _check(
        "signage: the sign reads Installed before the supersede (positive control)",
        sign_status(superseded_sign) == "Installed",
    )
    add_design(superseded_sign, supersede_reason="Corrected department name")
    _check(
        "signage: superseding an installed design drops the derived status back to Planned "
        "— nothing to hand-downgrade, because the status was never stored",
        sign_status(superseded_sign) == "Planned",
    )


def _sign_design_immutability_checks() -> None:
    """Codex Phase 5 audit, High: the direct-save guard covered version_number
    and the supersede pair but not the artwork itself, so approve v1 →
    direct-save new content_text → pass Production produced a sign that was not
    the sign anybody approved. A design version is now immutable after insert."""
    sign = _sign("SYN-IMMUT-1")
    first = add_design(sign, content_text="Cardiology — Second Floor")
    design = first["design"]

    def _edit_content():
        doc = frappe.get_doc("Hospital Sign Design", design)
        doc.content_text = "Cardiology — Third Floor"
        doc.save()

    message = _throws_message(
        "design: the wording cannot be edited even before anything is approved",
        _edit_content,
    )
    _check(
        "design: the refusal says changed artwork is a new version",
        "new version" in message,
    )
    _check(
        "design: the wording is unchanged after the refused edit",
        frappe.db.get_value("Hospital Sign Design", design, "content_text")
        == "Cardiology — Second Floor",
    )

    # …and after approval, which is the sequence the audit named.
    _pass_chain(sign, design)
    _expect_throws(
        "design: the wording cannot be edited after the design has been approved",
        _edit_content,
    )

    def _edit_artwork():
        doc = frappe.get_doc("Hospital Sign Design", design)
        doc.print_ready = "/files/swapped-artwork.pdf"
        doc.save()

    _expect_throws(
        "design: the print-ready artwork cannot be swapped under an approval",
        _edit_artwork,
    )

    other = _sign("SYN-IMMUT-2")

    def _reparent():
        doc = frappe.get_doc("Hospital Sign Design", design)
        doc.sign = other
        doc.save()

    _expect_throws(
        "design: a design cannot be re-parented onto another sign — its events were "
        "validated against it where it was",
        _reparent,
    )
    _check(
        "design: the design still belongs to its original sign after the refused re-parent",
        frappe.db.get_value("Hospital Sign Design", design, "sign") == sign,
    )

    _check(
        "design: Production still passes on the untouched approved design "
        "(positive control — the guard freezes the artwork, it does not break the gate)",
        _sign_event(
            sign, design, "Production", occurred_on=add_days(today(), 3)
        ).docstatus
        == 1,
    )

    second = add_design(
        sign, content_text="Cardiology — Third Floor", supersede_reason="Moved to third floor"
    )
    _check(
        "design: changed wording goes in as a new version instead (positive control)",
        second["version_number"] == 2
        and frappe.db.get_value("Hospital Sign Design", second["design"], "content_text")
        == "Cardiology — Third Floor",
    )


def _sign_draft_and_foreign_design_checks() -> None:
    sign = _sign("SYN-DRAFT-1")
    design = add_design(sign)["design"]

    _sign_event(sign, design, "Content Verification", occurred_on=today())

    # A draft is somebody thinking, not a step that happened — every gate reads
    # docstatus = 1 only, exactly as the CSR ledger does.
    draft_approval = _sign_event(
        sign, design, "Approval", occurred_on=add_days(today(), 1), submit=False
    )
    _check(
        "signage: a drafted Approval does not appear in the derived step states",
        sign_readiness(sign)["steps"]["Approval"]["state"] == "missing",
    )
    _expect_throws(
        "signage: a drafted Approval does not authorise a Print Proof",
        lambda: _sign_event(sign, design, "Print Proof", occurred_on=add_days(today(), 2)),
    )

    draft_approval.submit()
    _sign_event(sign, design, "Print Proof", occurred_on=add_days(today(), 2))
    _check(
        "signage: submitting that same Approval authorises the Print Proof "
        "(positive control — it is the docstatus that mattered, not the row)",
        sign_readiness(sign)["steps"]["Print Proof"]["state"] == "passed",
    )

    other = _sign("SYN-DRAFT-2")
    other_design = add_design(other)["design"]
    message = _throws_message(
        "signage: an event naming a design that belongs to a different sign is refused",
        lambda: _sign_event(sign, other_design, "Content Verification"),
    )
    _check(
        "signage: the foreign-design refusal names both signs",
        other in message and sign in message,
    )
    _check(
        "signage: an event naming this sign's own design is accepted (positive control)",
        _sign_event(other, other_design, "Content Verification").docstatus == 1,
    )


def _sign_waiver_semantics_checks() -> None:
    """A waiver is the *authorised exception* — the one sanctioned way past a
    gate — so it clears that gate exactly as a pass does, and stays in the trail
    carrying its mandatory reason. That is deliberate, documented behaviour, and
    it is pinned here so a later change to sign_blockers cannot quietly drop it."""
    sign = _sign("SYN-WAIVE-1")
    design = add_design(sign)["design"]
    _pass_chain(sign, design)

    _sign_event(
        sign,
        design,
        "Production",
        outcome="Waived",
        occurred_on=add_days(today(), 3),
        note="Produced in-house before this register existed; no vendor job to record",
    )
    _check(
        "signage: a waived Production is a cleared gate, so installation has no blockers",
        sign_readiness(sign)["installation_blockers"] == [],
    )
    _check(
        "signage: a waived Production carries the sign to In Production, as a pass does",
        sign_status(sign) == "In Production",
    )

    _sign_event(sign, design, "Installation", occurred_on=add_days(today(), 4))
    _check(
        "signage: a waived Production authorises a passing Installation (positive control)",
        sign_status(sign) == "Installed",
    )

    _check(
        "signage: Installation is a prerequisite for nothing — waiving it authorises no "
        "further step, because there is no further step",
        all("Installation" not in prereqs for prereqs in SIGN_PREREQUISITES.values()),
    )


def _sign_append_only_checks() -> None:
    sign = _sign("SYN-APPEND-1")
    design = add_design(sign)["design"]
    submitted = _sign_event(sign, design, "Content Verification")

    _expect_throws(
        "signage: a submitted sign event cannot be cancelled",
        lambda: frappe.get_doc("Hospital Sign Event", submitted.name).cancel(),
    )
    _expect_throws(
        "signage: a submitted sign event cannot be deleted",
        lambda: frappe.delete_doc("Hospital Sign Event", submitted.name),
    )
    _check(
        "signage: the submitted event is still there after both refusals",
        frappe.db.get_value("Hospital Sign Event", submitted.name, "docstatus") == 1,
    )

    draft = _sign_event(sign, design, "Approval", submit=False)
    frappe.delete_doc("Hospital Sign Event", draft.name)
    _check(
        "signage: a draft sign event deletes normally (positive control — a draft "
        "counts for nothing in any gate)",
        not frappe.db.exists("Hospital Sign Event", draft.name),
    )


def _sign_accessibility_checks() -> None:
    sign = _sign("SYN-ACC-1")
    design = add_design(sign)["design"]

    _expect_throws(
        "accessibility: a Not Met verdict with no note is refused",
        lambda: record_accessibility_check(sign, "Contrast", "Not Met", design=design),
    )
    _expect_throws(
        "accessibility: a Not Applicable verdict with no note is refused",
        lambda: record_accessibility_check(sign, "Multilingual", "Not Applicable", design=design),
    )

    record_accessibility_check(sign, "Readability", "Met", design=design)
    _check(
        "accessibility: a Met verdict needs no note (positive control)",
        accessibility_checklist(sign)["met"] == 1,
    )

    message = _throws_message(
        "accessibility: a second verdict for the same criterion, design and day is refused",
        lambda: record_accessibility_check(sign, "Readability", "Met", design=design),
    )
    _check(
        "accessibility: the duplicate refusal says to record the correction on the day "
        "it was re-checked",
        "re-checked" in message,
    )

    record_accessibility_check(
        sign, "Readability", "Not Met", design=design, checked_on=add_days(today(), 1),
        note="Reads badly under the new corridor lighting",
    )
    checklist = accessibility_checklist(sign)
    _check(
        "accessibility: a later verdict on a different day supersedes the earlier one "
        "(positive control)",
        checklist["not_met"] == 1 and checklist["met"] == 0,
    )
    _check(
        "accessibility: the five criteria nobody judged read Not Checked, never Met",
        checklist["not_checked"] == 5 and checklist["complete"] is False,
    )


# -- website ----------------------------------------------------------------


def _web_publication_gate_checks() -> None:
    page = _web_page("Synthetic outpatient timings")
    _check(
        "website: a page with no steps derives status Not Started",
        get_page_state(page)["status"] == "Not Started",
    )

    message = _throws_message(
        "website: Publication with nothing recorded is refused",
        lambda: record_step(page, "Publication"),
    )
    for missing in ("No draft", "No review", "No approval"):
        _check(f"website: the refusal names that there is {missing.lower()}", missing in message)

    record_step(page, "Draft", occurred_on=today())
    _check("website: the latest step is the state", get_page_state(page)["status"] == "Draft")
    _expect_throws(
        "website: Publication with only a draft is refused",
        lambda: record_step(page, "Publication"),
    )

    record_step(page, "Review", occurred_on=today())
    _check(
        "website: a review dated on the same day as the draft is accepted "
        "(positive control — on/after the latest draft)",
        get_page_state(page)["status"] == "Review",
    )
    _expect_throws(
        "website: Publication with a draft and a review but no approval is refused",
        lambda: record_step(page, "Publication"),
    )

    record_step(page, "Approval", occurred_on=today())
    same_day = _throws_message(
        "website: Publication is refused when the review and the approval share a date",
        lambda: record_step(page, "Publication"),
    )
    _check(
        "website: the same-date refusal explains a shared date cannot show the order",
        "shared date" in same_day,
    )

    record_step(page, "Approval", occurred_on=add_days(today(), 1))
    _check(
        "website: an approval dated the day after the review is accepted (positive control)",
        get_page_state(page)["publication_blockers"] == [],
    )
    record_step(page, "Publication", occurred_on=add_days(today(), 1))
    _check(
        "website: Publication succeeds once draft, review and approval are in order",
        get_page_state(page)["status"] == "Publication",
    )

    record_step(page, "Draft", occurred_on=add_days(today(), 2))
    stale = _throws_message(
        "website: a later draft makes the review and approval stale, refusing republication",
        lambda: record_step(page, "Publication", occurred_on=add_days(today(), 3)),
    )
    _check("website: the staleness refusal says a later draft was recorded", "stale" in stale)
    _check(
        "website: the page reads Draft again after being pulled back "
        "(the latest step is the state, positive control)",
        get_page_state(page)["status"] == "Draft",
    )


def _web_publication_date_bounding_checks() -> None:
    """Every publication check bounds its lookups to the publication's own
    date. That cuts both ways, and both directions are asserted here."""
    page = _web_page("Synthetic bounded page")
    record_step(page, "Draft", occurred_on=today())
    record_step(page, "Review", occurred_on=today())
    record_step(page, "Approval", occurred_on=add_days(today(), 1))
    record_step(page, "Publication", occurred_on=add_days(today(), 1))
    _check(
        "website: the page publishes with draft, review and approval in order "
        "(positive control)",
        get_page_state(page)["status"] == "Publication",
    )

    # A later draft moves the page's *current* state and blocks the next
    # publication — but it must not reach back and invalidate the one that
    # already happened, which was correct against everything on or before its
    # own date.
    record_step(page, "Draft", occurred_on=add_days(today(), 5))
    steps = get_page_state(page)["steps"]
    _check(
        "website: a draft dated after a publication does not retroactively invalidate it",
        missing_for_publication(steps, publish_on=add_days(today(), 1)) == [],
    )
    _check(
        "website: that same later draft does block the next publication "
        "(positive control — the bound is what differs, not the rule)",
        missing_for_publication(steps) != [],
    )

    backdated = _web_page("Synthetic backdated page")
    record_step(backdated, "Draft", occurred_on=today())
    record_step(backdated, "Review", occurred_on=today())
    record_step(backdated, "Approval", occurred_on=add_days(today(), 5))
    message = _throws_message(
        "website: a publication backdated before its approval is refused",
        lambda: record_step(backdated, "Publication", occurred_on=add_days(today(), 1)),
    )
    _check(
        "website: the refusal says no approval is recorded on or before that date",
        "on or before" in message,
    )
    record_step(backdated, "Publication", occurred_on=add_days(today(), 5))
    _check(
        "website: the same publication dated on the approval day is accepted "
        "(positive control)",
        get_page_state(backdated)["status"] == "Publication",
    )


def _web_step_direct_save_guard_checks() -> None:
    """The publication gate lives in record_step; a step typed into the grid
    would bypass it entirely. Same class of gap as Phase 4's milestone
    direct-save finding."""
    page = _web_page("Synthetic guard page")

    def _append_directly():
        doc = frappe.get_doc("Hospital Web Page", page)
        doc.append("steps", {"step": "Publication", "occurred_on": today()})
        doc.save()

    _expect_throws(
        "website: a step appended by a direct save is refused", _append_directly
    )
    _check(
        "website: nothing was written by the refused direct save",
        get_page_state(page)["steps"] == [],
    )

    record_step(page, "Draft")
    _check(
        "website: record_step writes the step (positive control — the guard blocks the "
        "bypass, not the sanctioned path)",
        len(get_page_state(page)["steps"]) == 1,
    )

    def _edit_directly():
        doc = frappe.get_doc("Hospital Web Page", page)
        doc.steps[0].note = "quietly rewritten"
        doc.save()

    _expect_throws("website: editing a recorded step by a direct save is refused", _edit_directly)

    def _delete_directly():
        doc = frappe.get_doc("Hospital Web Page", page)
        doc.steps = []
        doc.save()

    _expect_throws("website: deleting a recorded step by a direct save is refused", _delete_directly)

    _expect_throws(
        "website: a page cannot be created with steps already on it",
        lambda: frappe.get_doc(
            {
                "doctype": "Hospital Web Page",
                "page_title": "Born published",
                "steps": [{"step": "Publication", "occurred_on": today()}],
            }
        ).insert(),
    )


# -- software ---------------------------------------------------------------


def _software_requirement_and_uat_checks() -> None:
    project = _software("Synthetic UAT project")
    requirement = add_requirement(project, "Ward list filters by consultant")["requirement"]
    _check(
        "software: add_requirement writes the row with its agreed date (positive control)",
        frappe.db.get_value("Software Requirement Item", requirement, "agreed_on") is not None,
    )

    def _append_directly():
        doc = frappe.get_doc("Software Project Record", project)
        doc.append("requirements", {"description": "Snuck in", "agreed_on": today()})
        doc.save()

    _expect_throws(
        "software: a requirement appended by a direct save is refused", _append_directly
    )

    def _edit_directly():
        doc = frappe.get_doc("Software Project Record", project)
        doc.requirements[0].description = "Quietly reworded after it passed"
        doc.save()

    _expect_throws("software: rewording an agreed requirement is refused", _edit_directly)

    _expect_throws(
        "software: a UAT result cannot be inserted directly, bypassing record_uat_result",
        lambda: frappe.get_doc(
            {
                "doctype": "Software UAT Result",
                "software_project": project,
                "requirement": requirement,
                "tested_on": today(),
                "result": "Passed",
            }
        ).insert(),
    )
    _expect_throws(
        "software: a Failed UAT result with no note is refused",
        lambda: record_uat_result(project, requirement, "Failed"),
    )
    _expect_throws(
        "software: a UAT result against another project's requirement row is refused",
        lambda: record_uat_result(project, "not-a-real-row", "Passed"),
    )

    result = record_uat_result(
        project, requirement, "Failed", note="Filter returns every consultant"
    )
    _check(
        "software: a Failed UAT result with a note is recorded (positive control)",
        frappe.db.get_value("Software UAT Result", result["name"], "result") == "Failed",
    )


def _software_release_gate_checks() -> None:
    project = _software("Synthetic release project")

    message = _throws_message(
        "software: releasing a project with no requirements is refused",
        lambda: record_release(project),
    )
    _check(
        "software: the empty-project refusal says nothing to test is not everything tested",
        "not the same as everything tested" in message,
    )

    agreed = add_days(today(), 2)
    requirement = add_requirement(project, "Discharge letter prints on one page", agreed_on=agreed)[
        "requirement"
    ]

    blocked = _throws_message(
        "software: releasing with an untested requirement is refused",
        lambda: record_release(project),
    )
    _check(
        "software: the refusal names the untested requirement",
        "Discharge letter prints on one page" in blocked,
    )
    _check(
        "software: the refusal says no UAT result is recorded against it",
        "No UAT result is recorded" in blocked,
    )

    record_uat_result(project, requirement, "Passed", tested_on=agreed)
    stale = _throws_message(
        "software: a passing UAT dated the same day the requirement was agreed does not "
        "count — the gate is a result newer than the agreement",
        lambda: record_release(project),
    )
    _check(
        "software: the refusal explains the passing result predates the agreement",
        "predate" in stale,
    )
    _check(
        "software: the uncovered requirement is still uncovered (derived, not stored)",
        len(uat_coverage(project)["uncovered"]) == 1,
    )

    record_uat_result(project, requirement, "Passed", tested_on=add_days(agreed, 1))
    _check(
        "software: a passing UAT newer than the agreement covers the requirement "
        "(positive control)",
        len(uat_coverage(project)["covered"]) == 1
        and uat_coverage(project)["uncovered"] == [],
    )

    released = record_release(project)
    _check(
        "software: record_release stamps the status and the date once the gate is clear",
        released["status"] == "Released" and released["released_on"] == str(today()),
    )


def _software_release_terminal_checks() -> None:
    project = _software("Synthetic terminal project")
    requirement = add_requirement(project, "Session times out after 30 minutes")["requirement"]
    record_uat_result(project, requirement, "Passed", tested_on=add_days(today(), 1))
    record_release(project)

    already = _throws_message(
        "software: a second record_release is refused",
        lambda: record_release(project),
    )
    _check(
        "software: the second-release refusal names the date it was already released on",
        "already released on" in already and str(today()) in already,
    )

    _expect_throws(
        "software: adding a requirement to a Released project is refused",
        lambda: add_requirement(project, "Added after the fact"),
    )
    _expect_throws(
        "software: recording a UAT result on a Released project is refused",
        lambda: record_uat_result(project, requirement, "Passed"),
    )

    def _un_release():
        doc = frappe.get_doc("Software Project Record", project)
        doc.status = "Active"
        doc.save()

    _expect_throws(
        "software: un-releasing by a direct save is refused — Released is terminal",
        _un_release,
    )
    _check(
        "software: the project is still Released after the refused direct save",
        frappe.db.get_value("Software Project Record", project, "status") == "Released",
    )

    _expect_throws(
        "software: a project cannot be created already Released",
        lambda: _software("Born released", status="Released"),
    )

    def _release_by_save():
        doc = frappe.get_doc("Software Project Record", _software("Synthetic bypass project"))
        doc.status = "Released"
        doc.save()

    _expect_throws(
        "software: setting the status to Released by a direct save is refused — "
        "it would skip the UAT gate entirely",
        _release_by_save,
    )

    abandoned = _software("Synthetic abandoned project")
    abandoned_requirement = add_requirement(abandoned, "Never delivered")["requirement"]
    abandoned_doc = frappe.get_doc("Software Project Record", abandoned)
    abandoned_doc.status = "Abandoned"
    abandoned_doc.save()
    _check(
        "software: Abandoned can be set by a direct save (positive control — only the "
        "release path is guarded)",
        frappe.db.get_value("Software Project Record", abandoned, "status") == "Abandoned",
    )
    _expect_throws(
        "software: recording a UAT result on an Abandoned project is refused",
        lambda: record_uat_result(abandoned, abandoned_requirement, "Passed"),
    )
    _expect_throws(
        "software: releasing an Abandoned project is refused",
        lambda: record_release(abandoned),
    )


def _software_recorded_fact_immutability_checks() -> None:
    """The release gate compares a passing result's date against the day the
    requirement was agreed. Both halves of that comparison have to be fixed, or
    the gate can be satisfied after the fact by editing either one."""
    project = _software("Synthetic immutability project")
    requirement = add_requirement(project, "Referral letter carries the clinic code")[
        "requirement"
    ]
    result = record_uat_result(
        project, requirement, "Passed", tested_on=add_days(today(), 1)
    )["name"]
    _check(
        "software: the requirement is covered before anything is tampered with "
        "(positive control)",
        len(uat_coverage(project)["covered"]) == 1,
    )

    def _edit_agreed_on():
        doc = frappe.get_doc("Software Project Record", project)
        doc.requirements[0].agreed_on = add_days(today(), 30)
        doc.save()

    _expect_throws(
        "software: a requirement's agreed date cannot be edited after a passing UAT",
        _edit_agreed_on,
    )
    _check(
        "software: the requirement is still covered after the refused edit — the gate "
        "cannot be un-satisfied by moving the goalposts either",
        len(uat_coverage(project)["covered"]) == 1,
    )

    def _edit_result():
        doc = frappe.get_doc("Software UAT Result", result)
        doc.result = "Failed"
        doc.note = "Actually it never worked"
        doc.save()

    message = _throws_message(
        "software: a recorded UAT verdict cannot be edited", _edit_result
    )
    _check(
        "software: the refusal says to record a further result instead",
        "further result" in message,
    )

    def _edit_tested_on():
        doc = frappe.get_doc("Software UAT Result", result)
        doc.tested_on = add_days(today(), 60)
        doc.save()

    _expect_throws(
        "software: a UAT result's tested-on date cannot be edited — it is half of the "
        "release gate's comparison",
        _edit_tested_on,
    )
    _check(
        "software: the stored verdict and date are unchanged after both refusals",
        frappe.db.get_value("Software UAT Result", result, "result") == "Passed"
        and str(frappe.db.get_value("Software UAT Result", result, "tested_on"))
        == str(add_days(today(), 1)),
    )
    _check(
        "software: a further result records normally (positive control — corrections go "
        "in as new rows, not edits)",
        record_uat_result(
            project, requirement, "Failed", tested_on=add_days(today(), 2),
            note="Regressed in the next build",
        )["result"]
        == "Failed",
    )


# -- report parity ----------------------------------------------------------


def _build_publish_report_parity_checks() -> None:
    """The report and the controllers must never disagree — both go through
    build_publish, and this is the test that says so."""
    sign = _sign("SYN-REPORT-1")
    design = add_design(sign)["design"]
    _pass_chain(sign, design)

    page = _web_page("Synthetic report page")
    record_step(page, "Draft")

    project = _software("Synthetic report project")
    requirement = add_requirement(project, "Report parity requirement")["requirement"]
    record_uat_result(project, requirement, "Passed", tested_on=add_days(today(), 1))

    columns, rows = build_publish_report_execute({})
    _check("report: it returns columns", len(columns) == 6)

    by_record = {row["record"]: row for row in rows}

    _check(
        "report: the sign row carries the derived status, matching sign_status()",
        by_record[sign]["status"] == sign_status(sign) == "Planned",
    )
    # The report names the blockers of the next gate that has not cleared: a
    # sign whose chain is cleared is waiting on Production, and repeating the
    # already-cleared prerequisites back at the reader helps nobody.
    readiness = sign_readiness(sign)
    _check(
        "report: with production unblocked, the sign row names what installation still "
        "needs, matching sign_readiness()",
        readiness["production_blockers"] == []
        and by_record[sign]["blockers"] == "\n".join(readiness["installation_blockers"])
        and "Production" in by_record[sign]["blockers"],
    )

    installed = _sign("SYN-REPORT-2")
    installed_design = add_design(installed)["design"]
    _pass_chain(installed, installed_design)
    _sign_event(installed, installed_design, "Production", occurred_on=add_days(today(), 3))
    _sign_event(installed, installed_design, "Installation", occurred_on=add_days(today(), 4))
    _, rows_with_installed = build_publish_report_execute({})
    installed_row = {row["record"]: row for row in rows_with_installed}[installed]
    _check(
        "report: a fully installed sign shows no blockers at all (positive control — "
        "the parity assertion above is not passing on an always-empty column)",
        installed_row["blockers"] == "" and installed_row["status"] == "Installed",
    )

    _check(
        "report: the page row carries the derived status and its publication blockers",
        by_record[page]["status"] == "Draft"
        and by_record[page]["blockers"]
        == "\n".join(get_page_state(page)["publication_blockers"]),
    )
    _check(
        "report: the page's blockers are non-empty while review and approval are missing "
        "(positive control — the parity assertion is not passing on two empty strings)",
        by_record[page]["blockers"] != "",
    )

    _check(
        "report: the software row counts covered requirements, matching uat_coverage()",
        by_record[project]["detail"] == "1 of 1 requirement(s) have a passing UAT result"
        and by_record[project]["blockers"] == "",
    )

    signage_only, signage_rows = build_publish_report_execute({"area": "Signage"})
    _check(
        "report: the area filter narrows the listing to signage only",
        all(row["record"].startswith("SIGN-") for row in signage_rows)
        and any(row["record"] == sign for row in signage_rows),
    )


# ---------------------------------------------------------------------------
# Phase 6 — Weekly Review, dashboard/notification fixtures, over-receipt hook
# ---------------------------------------------------------------------------
#
# Same shape as every prior phase: plain assertions, every negative carrying
# a positive/negative control in the same block, everything rolled back at
# the end. The Weekly Review checks seed one item per section and assert
# both that it appears tagged with the right section AND that a resolved /
# processed / published counterpart does not — the report must be unable to
# disagree with the module it reports on. Run alongside run_phase2_tests
# through run_phase5_tests for full regression (see docs/operations.md).


def run_phase6_tests() -> None:
    _PASS.clear()
    _FAIL.clear()
    try:
        _weekly_review_quick_capture_checks()
        _weekly_review_waiting_for_checks()
        _weekly_review_csr_obligation_checks()
        _weekly_review_csr_tranche_checks()
        _weekly_review_ethics_checks()
        _weekly_review_hospital_sign_checks()
        _weekly_review_web_page_checks()
        _weekly_review_software_checks()
        _over_receipt_guard_checks()
        _dashboard_fixture_checks()
        _notification_fixture_checks()
    finally:
        frappe.db.rollback()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        frappe.throw(f"Failures: {_FAIL}")


# -- Weekly Review: one seed per section, plus its negative control ---------


def _weekly_review_quick_capture_checks() -> None:
    open_capture = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "Phase 6 weekly review — open capture"}
    ).insert()
    processed_capture = frappe.get_doc(
        {"doctype": "Quick Capture", "capture_text": "Phase 6 weekly review — processed capture"}
    ).insert()
    process_into_todo(processed_capture.name)

    _columns, rows = weekly_review_execute({"section": "Quick Captures"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: an Open Quick Capture appears in the Quick Captures section",
        open_capture.name in records,
    )
    _check(
        "weekly review: a Processed capture (negative control) does not appear",
        processed_capture.name not in records,
    )
    _check(
        "weekly review: every row in this section is tagged Quick Captures",
        rows and all(row["section"] == "Quick Captures" for row in rows),
    )


def _weekly_review_waiting_for_checks() -> None:
    contact = frappe.get_doc(
        {"doctype": "Contact", "first_name": "Phase 6 Weekly Review Contact"}
    ).insert()

    due = frappe.get_doc(
        {
            "doctype": "Waiting For",
            "waiting_on": contact.name,
            "subject": "Phase 6 weekly review — due a follow-up",
            "delegated_on": add_days(today(), -10),
            "promised_on": add_days(today(), -1),
        }
    ).insert()
    resolved = frappe.get_doc(
        {
            "doctype": "Waiting For",
            "waiting_on": contact.name,
            "subject": "Phase 6 weekly review — resolved",
            "delegated_on": add_days(today(), -10),
            "promised_on": add_days(today(), -1),
        }
    ).insert()
    resolved.status = "Resolved"
    resolved.save()
    not_due = frappe.get_doc(
        {
            "doctype": "Waiting For",
            "waiting_on": contact.name,
            "subject": "Phase 6 weekly review — not due yet",
            "delegated_on": today(),
            "promised_on": add_days(today(), 5),
        }
    ).insert()

    _columns, rows = weekly_review_execute({"section": "Waiting For"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: a Waiting item whose follow_up_on has arrived appears",
        due.name in records,
    )
    _check(
        "weekly review: a Resolved item (negative control) does not appear, even though "
        "follow_up_on has passed",
        resolved.name not in records,
    )
    _check(
        "weekly review: a Waiting item not yet due (negative control) does not appear",
        not_due.name not in records,
    )


def _weekly_review_csr_obligation_checks() -> None:
    project = _project("Weekly review — CSR obligation checks", 100_000)

    overdue = frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Phase 6 weekly review — overdue obligation",
            "due_on": add_days(today(), -5),
        }
    ).insert()
    submitted = frappe.get_doc(
        {
            "doctype": "CSR Reporting Obligation",
            "csr_project": project,
            "description": "Phase 6 weekly review — submitted obligation",
            "due_on": add_days(today(), -5),
            "submitted_on": add_days(today(), -6),
        }
    ).insert()

    _columns, rows = weekly_review_execute({"section": "CSR Reporting Obligations"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: an overdue CSR Reporting Obligation appears",
        overdue.name in records,
    )
    _check(
        "weekly review: a submitted obligation (negative control) does not appear, even "
        "though due_on has passed",
        submitted.name not in records,
    )

    # Parity: the report must agree with the module's own helper for the same record.
    _check(
        "weekly review: get_obligation_state agrees the overdue obligation is overdue "
        "(parity between the report and the module)",
        get_obligation_state(overdue.name)["overdue"] is True,
    )
    _check(
        "weekly review: get_obligation_state agrees the submitted obligation is not "
        "overdue (parity, negative side)",
        get_obligation_state(submitted.name)["overdue"] is False,
    )


def _weekly_review_csr_tranche_checks() -> None:
    project = _project(
        "Weekly review — CSR tranche checks",
        1_000_000,
        tranches=[
            {"expected_on": add_days(today(), -20), "expected_amount": 200_000},
            {"expected_on": add_days(today(), 20), "expected_amount": 300_000},
        ],
    )
    _columns, rows = weekly_review_execute({"section": "CSR Tranches"})
    matching = [row for row in rows if project in row["title"]]
    _check(
        "weekly review: exactly the one past-due, unreceived tranche appears for this "
        "project (the future tranche does not)",
        len(matching) == 1,
    )

    fully_paid_project = _project(
        "Weekly review — CSR tranche fully paid",
        200_000,
        tranches=[{"expected_on": add_days(today(), -20), "expected_amount": 200_000}],
    )
    _submitted(fully_paid_project, "Receipt", 200_000)
    _columns2, rows2 = weekly_review_execute({"section": "CSR Tranches"})
    _check(
        "weekly review: a fully-received tranche (negative control) does not appear",
        not any(fully_paid_project in row["title"] for row in rows2),
    )


def _weekly_review_ethics_checks() -> None:
    expiring_study = _study("Weekly review — ethics expiring soon")
    record_decision(_submission(expiring_study), "Approved", valid_until=add_days(today(), 30))

    safe_study = _study("Weekly review — ethics not yet due")
    record_decision(_submission(safe_study), "Approved", valid_until=add_days(today(), 200))

    expired_study = _study("Weekly review — ethics expired")
    record_decision(_submission(expired_study), "Approved", valid_until=add_days(today(), -1))

    _columns, rows = weekly_review_execute({"section": "Research Ethics"})
    titles = {row["title"] for row in rows}

    exp_title = frappe.db.get_value("Research Study", expiring_study, "study_title")
    safe_title = frappe.db.get_value("Research Study", safe_study, "study_title")
    expired_title = frappe.db.get_value("Research Study", expired_study, "study_title")

    _check(
        "weekly review: a study whose ethics approval expires within 60 days appears",
        exp_title in titles,
    )
    _check(
        "weekly review: a study not due to expire for 200 days (negative control) does "
        "not appear",
        safe_title not in titles,
    )
    _check(
        "weekly review: an already-expired ethics approval appears",
        expired_title in titles,
    )


def _weekly_review_hospital_sign_checks() -> None:
    due_sign = _sign("P6-SIGN-DUE", next_inspection_on=add_days(today(), -3))
    _columns, rows = weekly_review_execute({"section": "Hospital Signs"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: a sign whose next_inspection_on is in the past appears",
        due_sign in records,
    )

    clear_sign = _sign("P6-SIGN-CLEAR", next_inspection_on=add_days(today(), 90))
    design = add_design(clear_sign)["design"]
    _pass_chain(clear_sign, design)
    _sign_event(clear_sign, design, "Production", occurred_on=add_days(today(), 3))
    _sign_event(clear_sign, design, "Installation", occurred_on=add_days(today(), 4))
    _columns2, rows2 = weekly_review_execute({"section": "Hospital Signs"})
    records2 = {row["record"] for row in rows2}
    _check(
        "weekly review: a fully installed sign with its inspection not yet due (negative "
        "control) does not appear",
        clear_sign not in records2,
    )


def _weekly_review_web_page_checks() -> None:
    draft_only = _web_page("Phase 6 weekly review — draft only")
    record_step(draft_only, "Draft", occurred_on=add_days(today(), -5))
    _columns, rows = weekly_review_execute({"section": "Hospital Web Pages"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: a page with only a Draft recorded appears (review and approval "
        "still missing)",
        draft_only in records,
    )

    complete_page = _web_page("Phase 6 weekly review — fully cleared")
    record_step(complete_page, "Draft", occurred_on=add_days(today(), -5))
    record_step(complete_page, "Review", occurred_on=add_days(today(), -4))
    record_step(complete_page, "Approval", occurred_on=add_days(today(), -3))
    _columns2, rows2 = weekly_review_execute({"section": "Hospital Web Pages"})
    records2 = {row["record"] for row in rows2}
    _check(
        "weekly review: a page with draft, review and approval correctly ordered "
        "(negative control) does not appear",
        complete_page not in records2,
    )


def _weekly_review_software_checks() -> None:
    gap_project = _software("Phase 6 weekly review software — gap")
    add_requirement(gap_project, "Requirement never tested")
    _columns, rows = weekly_review_execute({"section": "Software Project Records"})
    records = {row["record"] for row in rows}
    _check(
        "weekly review: an Active project with an untested requirement appears",
        gap_project in records,
    )

    covered_project = _software("Phase 6 weekly review software — covered")
    req = add_requirement(covered_project, "Requirement that gets tested")
    record_uat_result(covered_project, req["requirement"], "Passed", tested_on=add_days(today(), 1))
    _columns2, rows2 = weekly_review_execute({"section": "Software Project Records"})
    records2 = {row["record"] for row in rows2}
    _check(
        "weekly review: an Active project whose only requirement passed UAT (negative "
        "control) does not appear",
        covered_project not in records2,
    )

    abandoned_project = _software(
        "Phase 6 weekly review software — abandoned with a gap", status="Abandoned"
    )
    add_requirement(abandoned_project, "Requirement nobody will ever test now")
    _columns3, rows3 = weekly_review_execute({"section": "Software Project Records"})
    records3 = {row["record"] for row in rows3}
    _check(
        "weekly review: a non-Active (Abandoned) project never appears here, even with an "
        "untested requirement — this section is scoped to Active projects only",
        abandoned_project not in records3,
    )


# -- Over-receipt hook: the Phase 1 gate condition ---------------------------


_OVER_RECEIPT_MARKER = "[hospital_ops over-receipt guard]"


def _p6_supplier() -> str:
    supplier_group = (
        frappe.db.get_value("Supplier Group", {"name": ["!=", "All Supplier Groups"]})
        or "All Supplier Groups"
    )
    return (
        frappe.get_doc(
            {
                "doctype": "Supplier",
                "supplier_name": f"Phase 6 Test Supplier {frappe.generate_hash(length=8)}",
                "supplier_group": supplier_group,
            }
        )
        .insert()
        .name
    )


def _p6_item(is_stock_item: int, allowance: float | None = None) -> str:
    code = f"P6-{'STOCK' if is_stock_item else 'NONSTOCK'}-{frappe.generate_hash(length=10)}"
    doc = {
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": "Hospital Equipment & Consumables",
        "stock_uom": "Nos",
        "is_stock_item": is_stock_item,
    }
    if allowance is not None:
        doc["over_delivery_receipt_allowance"] = allowance
    return frappe.get_doc(doc).insert().name


def _p6_po(item_code: str, qty: float, company: str, warehouse: str | None = None):
    row = {"item_code": item_code, "qty": qty, "rate": 100, "schedule_date": today()}
    if warehouse:
        row["warehouse"] = warehouse
    po = frappe.get_doc(
        {
            "doctype": "Purchase Order",
            "company": company,
            "supplier": _p6_supplier(),
            "schedule_date": today(),
            "items": [row],
        }
    )
    po.insert()
    po.submit()
    return po


def _p6_receipt(item_code: str, qty: float, po, po_item_name: str, company: str, warehouse=None):
    row = {
        "item_code": item_code,
        "qty": qty,
        "rate": 100,
        "purchase_order": po.name,
        "purchase_order_item": po_item_name,
    }
    if warehouse:
        row["warehouse"] = warehouse
    return frappe.get_doc(
        {
            "doctype": "Purchase Receipt",
            "company": company,
            "supplier": po.supplier,
            "posting_date": today(),
            "items": [row],
        }
    )


def _over_receipt_guard_checks() -> None:
    """The Phase 1 gate condition, closed by ``purchase_receipt_guard.py``.

    Evidence this reproduces: erpnext-phase1-gate.md §3.1 — 4 received
    against 2 ordered on a fixed-asset (non-stock) line, submitted without a
    warning. Every scenario here is inside the rollback, using freshly
    minted synthetic Items and Suppliers rather than the Phase 1 console
    masters — SWO-DEFIB-001 et al. are real, but items and suppliers created
    fresh in-test do not depend on that console session's state surviving
    unmodified.
    """
    company = "SSSIHMS Whitefield Ops"
    if not frappe.db.exists("Company", company):
        _check(
            "over-receipt guard: SSSIHMS Whitefield Ops company exists on this site "
            "(Phase 1 gate master data) — required for the rest of this group",
            False,
        )
        return

    warehouse = frappe.db.get_value("Warehouse", {"company": company}, "name")

    # 1. Within the ordered qty: accepted.
    item = _p6_item(is_stock_item=0)
    po = _p6_po(item, 5, company)
    within = _p6_receipt(item, 5, po, po.items[0].name, company)
    within.insert()
    within.submit()
    _check(
        "over-receipt guard: a non-stock receipt within the ordered qty is accepted",
        within.docstatus == 1,
    )

    # 2. Over-receipt: refused, naming the item, the ordered qty and what was received.
    item2 = _p6_item(is_stock_item=0)
    po2 = _p6_po(item2, 2, company)
    over = _p6_receipt(item2, 3, po2, po2.items[0].name, company)
    over.insert()
    message = _throws_message(
        "over-receipt guard: a non-stock over-receipt (3 against 2 ordered) is refused",
        over.submit,
    )
    _check(
        "over-receipt guard: the refusal names the item code",
        item2 in message,
    )
    for figure in ("2.0", "3.0"):
        _check(f"over-receipt guard: the refusal names the figure {figure}", figure in message)
    _check(
        "over-receipt guard: the refusal carries this app's marker (distinguishing it "
        "from core's own guard)",
        _OVER_RECEIPT_MARKER in message,
    )
    _check(
        "over-receipt guard: nothing was written — the receipt is still a draft",
        frappe.db.get_value("Purchase Receipt", over.name, "docstatus") == 0,
    )

    # 3. Exactly at the limit: accepted (positive control on the boundary itself).
    item3 = _p6_item(is_stock_item=0)
    po3 = _p6_po(item3, 4, company)
    exact = _p6_receipt(item3, 4, po3, po3.items[0].name, company)
    exact.insert()
    exact.submit()
    _check(
        "over-receipt guard: a non-stock receipt exactly at the ordered qty is accepted "
        "(positive control on the boundary)",
        exact.docstatus == 1,
    )

    # 4. A configured item-level allowance: inside tolerance accepted, outside refused.
    item4 = _p6_item(is_stock_item=0, allowance=10)
    po4 = _p6_po(item4, 10, company)
    po4_item_name = po4.items[0].name
    within_allowance = _p6_receipt(item4, 11, po4, po4_item_name, company)
    within_allowance.insert()
    within_allowance.submit()
    _check(
        "over-receipt guard: a receipt within a configured 10% item allowance (11 vs "
        "10 ordered) is accepted",
        within_allowance.docstatus == 1,
    )

    outside_allowance = _p6_receipt(item4, 1, po4, po4_item_name, company)  # totals 12 vs 10
    outside_message = _throws_message(
        "over-receipt guard: a further receipt taking the total to 20% over (outside "
        "the configured 10% allowance) is refused",
        outside_allowance.submit,
    )
    _check(
        "over-receipt guard: that refusal names the configured allowance",
        "10.0%" in outside_message,
    )

    # 5. A stock-item over-receipt is still refused — by CORE, not by this hook.
    stock_item = _p6_item(is_stock_item=1)
    po5 = _p6_po(stock_item, 2, company, warehouse=warehouse)
    stock_over = _p6_receipt(stock_item, 5, po5, po5.items[0].name, company, warehouse=warehouse)
    stock_over.insert()
    core_message = _throws_message(
        "over-receipt guard: a stock-item over-receipt (5 vs 2 ordered) is still refused",
        stock_over.submit,
    )
    _check(
        "over-receipt guard: that refusal is core's own — this app's marker is absent, "
        "so the hook did not fire for a stock item",
        _OVER_RECEIPT_MARKER not in core_message,
    )

    # 6. A receipt with no purchase_order reference at all: untouched.
    item6 = _p6_item(is_stock_item=0)
    no_po = frappe.get_doc(
        {
            "doctype": "Purchase Receipt",
            "company": company,
            "supplier": _p6_supplier(),
            "posting_date": today(),
            "items": [{"item_code": item6, "qty": 100, "rate": 10}],
        }
    )
    no_po.insert()
    no_po.submit()
    _check(
        "over-receipt guard: a receipt with no purchase_order reference at all is "
        "untouched (positive control — zero-cost path)",
        no_po.docstatus == 1,
    )


# -- Dashboard and Notification fixtures -------------------------------------


def _dashboard_fixture_checks() -> None:
    _check(
        "dashboard: the Hospital Ops Dashboard exists",
        bool(frappe.db.exists("Dashboard", "Hospital Ops")),
    )
    card_names = [
        "Open Quick Captures",
        "Waiting For (Status Waiting)",
        "Draft CSR Fund Events",
        "Active Research Studies",
    ]
    for card in card_names:
        _check(f"dashboard: Number Card '{card}' exists", bool(frappe.db.exists("Number Card", card)))

    _check(
        "dashboard: the Quick Captures Opened chart exists",
        bool(frappe.db.exists("Dashboard Chart", "Quick Captures Opened")),
    )

    if frappe.db.exists("Dashboard", "Hospital Ops"):
        dashboard = frappe.get_doc("Dashboard", "Hospital Ops")
        linked = {row.card for row in dashboard.cards}
        _check(
            "dashboard: the Hospital Ops Dashboard links all four Number Cards",
            set(card_names) <= linked,
        )
        _check(
            "dashboard: the Hospital Ops Dashboard links the Quick Captures Opened chart",
            any(row.chart == "Quick Captures Opened" for row in dashboard.charts),
        )


def _notification_fixture_checks() -> None:
    """Existence and condition-string checks — not delivery (no SMTP on this
    site by design; see ADR note in README and notification_setup.py)."""
    expected = {
        "Waiting For Follow-up Arrived": ("Waiting For", 'doc.status=="Waiting"'),
        "CSR Reporting Obligation Due Soon": ("CSR Reporting Obligation", "not doc.submitted_on"),
        "Research Ethics Submission Expiring": (
            "Research Ethics Submission",
            'doc.decision=="Approved"',
        ),
        "Hospital Sign Inspection Due": ("Hospital Sign", ""),
    }
    for name, (doctype, condition) in expected.items():
        exists = frappe.db.exists("Notification", name)
        _check(f"notification: {name} exists", bool(exists))
        if not exists:
            continue
        row = frappe.db.get_value(
            "Notification",
            name,
            ["document_type", "channel", "enabled", "condition"],
            as_dict=True,
        )
        _check(f"notification: {name} targets {doctype}", row.document_type == doctype)
        _check(
            f"notification: {name} is channel System Notification (SMTP deliberately "
            "unconfigured on this site)",
            row.channel == "System Notification",
        )
        _check(f"notification: {name} is enabled", bool(row.enabled))
        _check(
            f"notification: {name} carries the expected condition, so a resolved/decided "
            "record does not fire",
            (row.condition or "") == condition,
        )
