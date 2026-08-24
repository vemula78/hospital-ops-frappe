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
        # Always enforced, flag or not — the flag-guarded path (process_into_
        # todo) must still be limited to the allow-list (Codex re-audit,
        # P3-2: "the flag-guarded path must still enforce the allow-list").
        self._restrict_processed_into_doctype()

        # `read_only` on the JSON fields is a UI hint, not server-side
        # enforcement — a direct REST save can still post any value for
        # these fields. The two branches below are the complete state
        # machine: a fresh row cannot be born already processed, and an
        # existing row cannot have its outcome edited outside
        # process_into_todo, regardless of what its *current* stored status
        # happens to be (the original guard only fired once the stored
        # status was already terminal, which let Open -> Processed through
        # on a plain save with a forged pointer — Codex re-audit, P3-2).
        if self.is_new():
            self._require_open_and_unprocessed_on_insert()
        else:
            self._guard_status_and_pointer_on_update()

    def _restrict_processed_into_doctype(self) -> None:
        if self.processed_into_doctype and self.processed_into_doctype not in ALLOWED_PROCESSED_INTO_DOCTYPES:
            frappe.throw(
                _("processed_into_doctype must be one of {0}.").format(
                    ", ".join(ALLOWED_PROCESSED_INTO_DOCTYPES)
                ),
                title=_("Quick Capture"),
            )

    def _require_open_and_unprocessed_on_insert(self) -> None:
        # Neither process_into_todo nor discard() ever inserts a new row (see
        # below), so in practice this flag is never set on insert — kept for
        # symmetry with the update branch, in case that ever changes.
        if self.flags.via_process_method:
            return
        if self.status not in (None, "", "Open") or self.processed_into_doctype or self.processed_into:
            frappe.throw(
                _(
                    "A new capture must start Open with no processed-into pointer; "
                    "process it through process_into_todo instead."
                ),
                title=_("Quick Capture"),
            )

    def _guard_status_and_pointer_on_update(self) -> None:
        # `via_process_method` gates *any* direct change to status or the
        # processed_into pointer, not just the ones that would forge an
        # outcome — that is deliberately broader than "block Processed", per
        # the Codex re-audit (P3-2: "refuse ANY change to status ... not
        # only when stored is terminal"). The one consequence to remember:
        # discarding a capture (Open -> Discarded) is *also* a status change,
        # so it can no longer happen through a plain save either — that is
        # why discard() below exists and sets this same flag, mirroring
        # process_into_todo.
        if self.flags.via_process_method:
            return

        stored = frappe.db.get_value(
            self.doctype,
            self.name,
            ["status", "processed_into_doctype", "processed_into"],
            as_dict=True,
        )
        if not stored:
            return

        changed = (
            self.status != stored.status
            or self.processed_into_doctype != stored.processed_into_doctype
            or self.processed_into != stored.processed_into
        )
        if changed:
            frappe.throw(
                _(
                    "status and the processed-into pointer can only change through "
                    "processing (process_into_todo) or discarding (discard) the "
                    "capture, not a direct save."
                ),
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
    # Only path that may move a capture's status or outcome fields — see
    # _guard_status_and_pointer_on_update above.
    doc.flags.via_process_method = True
    doc.save()

    return {"todo": todo.name, "capture": doc.name}


@frappe.whitelist()
def discard(name: str) -> dict:
    """Marks a capture Discarded.

    Added alongside the P3-2 re-fix: once a plain save can no longer move
    `status` at all without going through a flagged path, the previously
    unremarkable "just edit status to Discarded" workflow needs a sanctioned
    path of its own, or discarding a capture would become impossible. Mirrors
    process_into_todo's own lock-then-check-then-flag shape rather than
    inventing a different one.
    """
    doc = get_doc_for_action("Quick Capture", name, ptype="write")

    locked_status = frappe.db.get_value("Quick Capture", name, "status", for_update=True)
    if locked_status != "Open":
        frappe.throw(
            _("Only an Open capture can be discarded; this one is {0}.").format(locked_status),
            title=_("Quick Capture"),
        )

    doc.status = "Discarded"
    doc.flags.via_process_method = True
    doc.save()

    return {"capture": doc.name}
