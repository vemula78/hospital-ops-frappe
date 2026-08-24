from __future__ import annotations

"""Software UAT Result — one dated verdict against one agreed requirement
(SFT-005).

A standalone doctype rather than a child of the requirement, because Frappe
supports neither nested child tables nor a Link to a child row. That is the
right shape anyway: one requirement collects many results over time, and each
is a dated fact rather than a property of the requirement.

Results arrive only through ``Software Project Record.record_uat_result()``,
which is where the project lock and the Released/Abandoned refusals live. A
direct insert would bypass both — the Phase 2 P3-2 lesson again — so
``validate()`` refuses one, and every field is a record of what happened and
cannot be edited afterwards.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: Everything on this doctype is a record of what happened.
RECORDED_FIELDS = (
    "software_project",
    "requirement",
    "tested_on",
    "result",
    "tester_name",
    "note",
)


class SoftwareUATResult(Document):
    def validate(self) -> None:
        self._guard_direct_write()
        self._check_note_present()

    def _guard_direct_write(self) -> None:
        if self.flags.recording_result:
            return

        if self.is_new():
            frappe.throw(
                _(
                    "A UAT result is recorded through record_uat_result() on the project, not "
                    "created directly: that is where the project is locked, where a Released "
                    "or Abandoned project is refused, and where the requirement is checked to "
                    "belong to the project."
                ),
                title=_("Software UAT Result"),
            )

        stored = frappe.db.get_value(
            "Software UAT Result", self.name, list(RECORDED_FIELDS), as_dict=True
        )
        for field in RECORDED_FIELDS:
            current = self.get(field) or None
            previous = (stored.get(field) if stored else None) or None
            if str(current or "") != str(previous or ""):
                frappe.throw(
                    _(
                        "{0} records what happened on the day it was tested and cannot be "
                        "changed. Record a further result instead."
                    ).format(_(self.meta.get_label(field))),
                    title=_("Software UAT Result"),
                )

    def _check_note_present(self) -> None:
        if self.result == "Failed" and not (self.note or "").strip():
            frappe.throw(
                _(
                    "A Failed result needs a note. A failure with no detail cannot be acted "
                    "on, and it is the result most likely to be entered in a hurry."
                ),
                title=_("Software UAT Result"),
            )
