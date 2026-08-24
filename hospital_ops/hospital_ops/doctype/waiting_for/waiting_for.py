from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today


class WaitingFor(Document):
    def validate(self) -> None:
        self._check_promised_after_delegated()
        self._default_follow_up_on()

    def _check_promised_after_delegated(self) -> None:
        # This exact rule caught a real bug in the source app (waiting.ts):
        # a promise dated before the delegation itself is nonsensical and must
        # be refused rather than silently accepted.
        if self.promised_on and getdate(self.promised_on) < getdate(self.delegated_on):
            frappe.throw(
                _("The promised date ({0}) cannot be before the delegated date ({1}).").format(
                    self.promised_on, self.delegated_on
                ),
                title=_("Waiting For"),
            )

    def _default_follow_up_on(self) -> None:
        # follow_up_on defaults to promised_on when the caller leaves it empty
        # — the register's chase date should not require the user to think
        # about it twice when a promise has already been given.
        if not self.follow_up_on and self.promised_on:
            self.follow_up_on = self.promised_on


@frappe.whitelist()
def log_follow_up(name: str, note: str | None = None, new_promised_on: str | None = None) -> dict:
    """Logs a follow-up and, when a new promise was given, moves the parent's
    promised_on and follow_up_on to match — atomically, in one call.

    Refused once the item is no longer Waiting: a follow-up on something
    already Resolved or Cancelled has nothing left to chase.
    """
    doc = frappe.get_doc("Waiting For", name)
    frappe.has_permission(doctype="Waiting For", ptype="write", doc=doc, throw=True)

    if doc.status != "Waiting":
        frappe.throw(
            _("Only an item still Waiting can have a follow-up logged; this one is {0}.").format(
                doc.status
            ),
            title=_("Waiting For"),
        )

    followed_up_on = today()
    doc.append(
        "follow_ups",
        {
            "followed_up_on": followed_up_on,
            "note": note,
            "new_promised_on": new_promised_on,
        },
    )

    if new_promised_on:
        doc.promised_on = new_promised_on
        doc.follow_up_on = new_promised_on

    doc.save()

    return {
        "name": doc.name,
        "promised_on": doc.promised_on,
        "follow_up_on": doc.follow_up_on,
    }
