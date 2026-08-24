from __future__ import annotations

"""Hospital Sign Design — a versioned print-ready artwork (SIG-003).

**A design version is immutable once it exists.** The artwork, the words and
the sign it belongs to are fixed at insert; changed artwork is a *new version*
through ``add_design``, which is the entire point of versioning. This is the
reference's own model (``signDesigns`` rows are inserted and superseded, never
edited) and it closes a hole the first cut of this port left open (Codex Phase
5 audit, High): the guard covered ``version_number`` and the supersede pair but
not ``content_text``, ``print_ready`` or ``sign``, so the sequence

    approve v1  →  direct-save new content_text/print_ready  →  pass Production

produced a sign that was not the sign anybody approved, with a workflow trail
that read as though it were. Freezing only *after* an approval would be the
weaker fix and a more complicated one: it makes the rule conditional on state
the design does not own, and it still allows the edit in the window before
approval, where "the artwork the proof was cut from" is already meaningful.
Unconditional immutability needs no such reasoning.

Re-parenting is the same defect wearing a different hat. Changing ``sign``
would move a design under a sign whose events were validated against a design
that is no longer there — and every gate is keyed to the current design of a
sign, so the two would silently disagree.

Two fields stay writable, and only through ``add_design``'s flag:
``superseded_by`` and ``supersede_reason``. Superseding is what resets the
workflow — readiness is keyed to the current design, so marking one superseded
puts the sign back behind every gate — and being able to set or clear that by
hand would let a sign be walked back into "approved" without anybody approving
anything.

All of it is enforced in ``validate()`` rather than by ``read_only`` in the
JSON: the Phase 2 P3-2 lesson is that ``read_only`` is a UI hint, and a direct
API call goes nowhere near the form.
"""

import frappe
from frappe import _
from frappe.model.document import Document

#: Fixed at insert. Not even ``add_design`` changes these on an existing row,
#: so the refusal is unconditional rather than flag-gated.
IMMUTABLE_FIELDS = ("sign", "version_number", "content_text", "print_ready")

#: Writable, but only through ``add_design``'s flag.
SUPERSEDE_FIELDS = ("superseded_by", "supersede_reason")


class HospitalSignDesign(Document):
    def validate(self) -> None:
        self._guard_fields()
        self._check_supersede_shape()

    def _guard_fields(self) -> None:
        if self.is_new():
            if self.flags.adding_design:
                return
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
            "Hospital Sign Design",
            self.name,
            list(IMMUTABLE_FIELDS + SUPERSEDE_FIELDS),
            as_dict=True,
        )

        def changed(field: str) -> bool:
            current = self.get(field) or None
            previous = (stored.get(field) if stored else None) or None
            return str(current or "") != str(previous or "")

        for field in IMMUTABLE_FIELDS:
            if not changed(field):
                continue
            frappe.throw(
                _(
                    "{0} cannot be changed: a design version is a fixed record of what was "
                    "verified, approved and proofed. Changed artwork is a new version — add "
                    "it with add_design(), which supersedes this one and puts the sign back "
                    "behind every gate."
                ).format(_(self.meta.get_label(field))),
                title=_("Hospital Sign Design"),
            )

        if self.flags.adding_design:
            return

        for field in SUPERSEDE_FIELDS:
            if not changed(field):
                continue
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
