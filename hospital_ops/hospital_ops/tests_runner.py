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
