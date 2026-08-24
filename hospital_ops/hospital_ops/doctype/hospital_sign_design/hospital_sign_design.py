from __future__ import annotations

"""Hospital Sign Design — a versioned print-ready artwork (SIG-003).

Two things are set only by ``Hospital Sign.add_design()``: the version number
and the supersede pair. Both are guarded in ``validate()`` rather than only by
``read_only`` in the JSON — the Phase 2 P3-2 lesson is that ``read_only`` is a
UI hint, and a direct API call goes nowhere near the form.

The version number matters because it is a ``max + 1`` assignment against a
count, which only holds under the sign lock ``add_design`` takes. The supersede
pair matters because superseding is what resets the workflow: readiness is
keyed to the current design, so marking one superseded puts the sign back
behind every gate. Being able to set (or clear) that by hand would let a sign
be walked back into "approved" without anybody approving anything.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: Fields only ``add_design`` may write.
MANAGED_FIELDS = ("version_number", "superseded_by", "supersede_reason")


class HospitalSignDesign(Document):
    def validate(self) -> None:
        self._guard_managed_fields()
        self._check_supersede_shape()

    def _guard_managed_fields(self) -> None:
        if self.flags.adding_design:
            return

        if self.is_new():
            frappe.throw(
                _(
                    "A design is added through add_design() on the sign, not created "
                    "directly: the version number is assigned under a lock on the sign, and "
                    "replacing a live design requires a supersede reason that a direct "
                    "insert would skip."
                ),
                title=_("Hospital Sign Design"),
            )

        stored = frappe.db.get_value(
            "Hospital Sign Design", self.name, list(MANAGED_FIELDS), as_dict=True
        )
        for field in MANAGED_FIELDS:
            current = self.get(field) or None
            previous = (stored.get(field) if stored else None) or None
            if str(current or "") != str(previous or ""):
                frappe.throw(
                    _(
                        "{0} is set by add_design() and cannot be changed by a direct save."
                    ).format(_(self.meta.get_label(field))),
                    title=_("Hospital Sign Design"),
                )

    def _check_supersede_shape(self) -> None:
        """A superseded design says why, and a reason with nothing to explain
        is not recorded as though something had been."""
        if self.superseded_by and not (self.supersede_reason or "").strip():
            frappe.throw(
                _("A superseded design must carry the reason it was replaced."),
                title=_("Hospital Sign Design"),
            )
        if (self.supersede_reason or "").strip() and not self.superseded_by:
            frappe.throw(
                _("A supersede reason without a superseding version explains nothing."),
                title=_("Hospital Sign Design"),
            )
        if self.superseded_by == self.name:
            frappe.throw(
                _("A design cannot supersede itself."), title=_("Hospital Sign Design")
            )
