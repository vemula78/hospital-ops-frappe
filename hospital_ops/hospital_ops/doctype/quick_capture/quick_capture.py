from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from hospital_ops.hospital_ops.permissions import get_doc_for_action

#: Only these may appear in processed_into_doctype (Codex audit, P3-2). A
#: Dynamic Link field's `options` (the doctype-selector field) restricts
#: nothing server-side — that JSON attribute only tells the UI which link
#: options field to read, so without this check a direct REST save could
#: point a capture at any doctype in the system, including ones the user has
#: no business linking to from an inbox item.
ALLOWED_PROCESSED_INTO_DOCTYPES = ("ToDo", "Task", "Waiting For", "Meeting Record")


class QuickCapture(Document):
    def validate(self) -> None:
        self._restrict_processed_into_doctype()
        self._guard_processed_or_discarded_state()

    def _restrict_processed_into_doctype(self) -> None:
        if self.processed_into_doctype and self.processed_into_doctype not in ALLOWED_PROCESSED_INTO_DOCTYPES:
            frappe.throw(
                _("processed_into_doctype must be one of {0}.").format(
                    ", ".join(ALLOWED_PROCESSED_INTO_DOCTYPES)
                ),
                title=_("Quick Capture"),
            )

    def _guard_processed_or_discarded_state(self) -> None:
        # `read_only` on the JSON fields is a UI hint, not server-side
        # enforcement (Codex audit, P3-2) — a direct REST save can still post
        # any value for these fields. Once the *stored* status is Processed
        # or Discarded, status/processed_into/processed_into_doctype may only
        # change through process_into_todo (which sets the flag below);
        # everything else about the row (e.g. capture_text) stays editable.
        if self.is_new() or self.flags.via_process_method:
            return

        stored = frappe.db.get_value(
            self.doctype,
            self.name,
            ["status", "processed_into_doctype", "processed_into"],
            as_dict=True,
        )
        if not stored or stored.status not in ("Processed", "Discarded"):
            return

        changed = (
            self.status != stored.status
            or self.processed_into_doctype != stored.processed_into_doctype
            or self.processed_into != stored.processed_into
        )
        if changed:
            frappe.throw(
                _(
                    "This capture is {0} and its status or outcome cannot be changed "
                    "directly; that only happens through processing it."
                ).format(stored.status),
                title=_("Quick Capture"),
            )


@frappe.whitelist()
def process_into_todo(name: str, description: str | None = None) -> dict:
    """Turns a capture into a ToDo, in one call (CAP-004 in the source app).

    Processing and marking the capture Processed happen together so the pair
    can never be left half-done: a caller either gets both, or neither (an
    exception here leaves the whole request rolled back).
    """
    doc = get_doc_for_action("Quick Capture", name, ptype="write")

    # Lock the row before re-checking status (Codex audit, P2-1): without
    # this, two concurrent calls can both read "Open" before either writes,
    # both insert a ToDo, and both mark the capture Processed. The read below
    # takes the row lock; a second concurrent call blocks here until the
    # first call's transaction commits, and then sees "Processed" — the
    # locked read, not the doc loaded above — and is refused.
    locked_status = frappe.db.get_value("Quick Capture", name, "status", for_update=True)
    if locked_status != "Open":
        frappe.throw(
            _("Only an Open capture can be processed; this one is {0}.").format(locked_status),
            title=_("Quick Capture"),
        )

    todo = frappe.get_doc(
        {
            "doctype": "ToDo",
            "description": description or doc.capture_text,
            "reference_type": "Quick Capture",
            "reference_name": doc.name,
        }
    ).insert()

    doc.status = "Processed"
    doc.processed_into_doctype = "ToDo"
    doc.processed_into = todo.name
    # Only path that may move a Processed/Discarded capture's status or
    # outcome fields — see _guard_processed_or_discarded_state above.
    doc.flags.via_process_method = True
    doc.save()

    return {"todo": todo.name, "capture": doc.name}
