from __future__ import annotations

"""One-off creation of the Phase 6 Notification records.

Run once via ``bench execute
hospital_ops.hospital_ops.notification_setup.ensure_phase6_notifications`` —
same idempotent, guarded-by-``frappe.db.exists`` shape as
``dashboard_setup.py``, and for the same reason: these four records are
exported as fixtures once created, so a second run (or a fresh bench that
already has the fixture JSON installed) does nothing.

**System-Notification-only, deliberately (ADR-worthy, recorded here rather
than silently decided).** All four use ``channel = "System Notification"``,
which posts to this site's in-app notification bell — no outbound email.
SMTP is not configured on this site and this build does not configure it:
turning these into email alerts is a separate decision for the site owner to
make later (mail server, sender identity, delivery monitoring), not something
to default into as a side effect of shipping a report phase.

Each notification's ``condition`` exists so a record that no longer needs
attention does not keep firing on the day its date field matches:

- Waiting For: only while ``status == "Waiting"`` — Resolved/Cancelled items
  are done, whatever their `follow_up_on` says.
- CSR Reporting Obligation: only while nothing has been ``submitted_on`` —
  once something is submitted the obligation is no longer building towards
  being overdue, even if the reminder date matches.
- Research Ethics Submission: only ``decision == "Approved"`` — a submission
  that was Rejected or is still Pending has no ``valid_until`` to expire.
- Hospital Sign: no additional condition — a next inspection date matters
  regardless of the sign's derived status.
"""

import frappe

NOTIFICATIONS: list[dict] = [
    {
        "name": "Waiting For Follow-up Arrived",
        "document_type": "Waiting For",
        "channel": "System Notification",
        "event": "Days After",
        "date_changed": "follow_up_on",
        "days_in_advance": 0,
        "condition_type": "Python",
        "condition": 'doc.status=="Waiting"',
        "subject": "Waiting For due a follow-up: {{ doc.name }}",
        "message": (
            "<p>{{ doc.name }} ({{ doc.subject }}) was due for a follow-up on "
            "{{ doc.follow_up_on }}. Waiting on {{ doc.waiting_on }}.</p>"
        ),
        "recipients": [{"receiver_by_role": "System Manager"}],
    },
    {
        "name": "CSR Reporting Obligation Due Soon",
        "document_type": "CSR Reporting Obligation",
        "channel": "System Notification",
        "event": "Days Before",
        "date_changed": "due_on",
        "days_in_advance": 7,
        "condition_type": "Python",
        "condition": "not doc.submitted_on",
        "subject": "CSR Reporting Obligation due in 7 days: {{ doc.name }}",
        "message": (
            "<p>{{ doc.name }} ({{ doc.description }}) on {{ doc.csr_project }} is due "
            "{{ doc.due_on }} and nothing has been submitted yet.</p>"
        ),
        "recipients": [{"receiver_by_role": "System Manager"}],
    },
    {
        "name": "Research Ethics Submission Expiring",
        "document_type": "Research Ethics Submission",
        "channel": "System Notification",
        "event": "Days Before",
        "date_changed": "valid_until",
        "days_in_advance": 60,
        "condition_type": "Python",
        "condition": 'doc.decision=="Approved"',
        "subject": "Research ethics approval expires in 60 days: {{ doc.name }}",
        "message": (
            "<p>{{ doc.name }} for study {{ doc.study }} ({{ doc.committee }}) expires on "
            "{{ doc.valid_until }}. A renewal submission should be underway.</p>"
        ),
        "recipients": [{"receiver_by_role": "System Manager"}],
    },
    {
        "name": "Hospital Sign Inspection Due",
        "document_type": "Hospital Sign",
        "channel": "System Notification",
        "event": "Days After",
        "date_changed": "next_inspection_on",
        "days_in_advance": 0,
        "condition_type": "Python",
        "condition": "",
        "subject": "Hospital Sign inspection due: {{ doc.name }}",
        "message": (
            "<p>{{ doc.name }} ({{ doc.reference }}) was due its next inspection on "
            "{{ doc.next_inspection_on }}.</p>"
        ),
        "recipients": [{"receiver_by_role": "System Manager"}],
    },
]


def ensure_phase6_notifications() -> list[str]:
    created = []
    for spec in NOTIFICATIONS:
        if frappe.db.exists("Notification", spec["name"]):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Notification",
                "is_standard": 0,
                "enabled": 1,
                **spec,
            }
        )
        doc.insert(ignore_permissions=True)
        created.append(doc.name)
    frappe.db.commit()
    return created
