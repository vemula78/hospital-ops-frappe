from __future__ import annotations

"""Hospital Sign Event — SIG-002's workflow, gated and append-only.

Ported from ``recordWorkflowEvent`` in ``src/server/domain/signage.ts``. Three
refusals, and **none of them is overridable at this level** — the authorised
exception is a ``Waived`` outcome on the step itself, which carries its own
reason and stays in the trail. A blanket "record it anyway" here would let a
sign be produced with no record of what was skipped, and once four hundred
signs are printed the money is spent.

1. **Existence.** A step may only pass when every step before it has passed or
   been waived *for the current design*: Content Verification → Approval →
   Print Proof → Production → Installation. The refusal names the missing
   prerequisites, because "not ready" without the reason leaves the user
   guessing what to fix.

2. **Order.** The sequence is chronological too, not merely a matter of which
   rows exist — ``sequenceViolation`` in the reference, ported with its
   prerequisite map intact (``build_publish.SIGN_ORDER_PREREQUISITES``). An
   approval dated 10 July and a production event dated 1 July both pass an
   existence check and are, once recorded, indistinguishable from a correctly
   sequenced pair.

3. **Current design.** A *passing* Production or Installation event naming a
   superseded design is refused outright. A non-passing event on an old design
   stays recordable — the record may genuinely need to say that a superseded
   design failed — but a passing one is what makes the sign read as produced or
   installed, and doing that on the strength of artwork that was never itself
   verified, approved and proofed is the failure this refusal exists to
   prevent. In the reference this was a Codex round-2 finding: such an event
   used to skip the order check and slip through rather than being refused.

The trail is append-only by submission, exactly like ``CSR Fund Event``:
``before_cancel``/``on_cancel`` refuse, and ``on_trash`` refuses for a
submitted row. A correction is a further event, never a cancellation — the
trail must show the mistake and the correction, not the corrected state alone.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from hospital_ops.hospital_ops.build_publish import (
    SIGN_PREREQUISITES,
    current_design,
    sign_blockers,
    sign_events,
    sign_sequence_violation,
    sign_step_states,
)


class HospitalSignEvent(Document):
    # -- lifecycle ------------------------------------------------------------

    def validate(self) -> None:
        self._check_note_present()
        self._check_design_belongs_to_sign()

    def before_submit(self) -> None:
        self._check_against_locked_sign()

    def before_cancel(self) -> None:
        self._refuse_cancel()

    def on_cancel(self) -> None:
        # Belt and braces, the CSR Fund Event pattern: before_cancel is the
        # earliest refusal, on_cancel is the one the brief names. Either
        # raising rolls the whole cancellation back.
        self._refuse_cancel()

    def on_trash(self) -> None:
        """A submitted event cannot be deleted; a draft can.

        Frappe core already refuses deleting a submitted document, and because
        cancellation is refused above this is transitively impossible today.
        The branch costs nothing and keeps the invariant true in this app's own
        code rather than depending on core keeping that behaviour forever. A
        draft counts for nothing in any gate, so removing one removes nothing.
        """
        if self.docstatus == 1:
            frappe.throw(
                _(
                    "A submitted sign event cannot be deleted. The workflow trail is "
                    "append-only: record a further event instead."
                ),
                title=_("Hospital Sign Event"),
            )

    def _refuse_cancel(self) -> None:
        frappe.throw(
            _(
                "A submitted sign event cannot be cancelled. Record a further {0} event "
                "instead — the trail has to show what was recorded and what corrected it, "
                "not the corrected state alone."
            ).format(self.step),
            title=_("Hospital Sign Event"),
        )

    # -- shape validation -----------------------------------------------------

    def _check_note_present(self) -> None:
        if self.outcome in ("Failed", "Waived") and not (self.note or "").strip():
            frappe.throw(
                _(
                    "A {0} step needs a note. A failure nobody described cannot be acted on, "
                    "and a waiver with no reason is indistinguishable from a step nobody did."
                ).format(_(self.outcome)),
                title=_("Hospital Sign Event"),
            )

    def _check_design_belongs_to_sign(self) -> None:
        design_sign = frappe.db.get_value("Hospital Sign Design", self.design, "sign")
        if design_sign != self.sign:
            frappe.throw(
                _("Design {0} belongs to sign {1}, not {2}.").format(
                    self.design, design_sign, self.sign
                ),
                title=_("Hospital Sign Event"),
            )

    # -- submit-time gates ----------------------------------------------------

    def _check_against_locked_sign(self) -> None:
        """Every gate, decided on one locked read of the sign's trail.

        The order is load-bearing and so is the source of the values. The
        ``for_update=True`` read of the sign comes first: it is the lock every
        submitter on this sign contends for. Everything after it — the live
        design, the events, therefore every step state — is read with
        ``FOR UPDATE`` as well, because MariaDB's REPEATABLE READ would
        otherwise serve a non-locking read from the snapshot this transaction
        opened *before* the lock was granted, i.e. it would miss exactly the
        concurrent event the lock had just waited for. That is the Phase 2
        re-audit hole, and every gate here is a check against a *set* of
        events, which no unique index can express.
        """
        locked = frappe.db.get_value(
            "Hospital Sign", self.sign, ["name"], as_dict=True, for_update=True
        )
        if not locked:
            frappe.throw(_("{0} is not a sign.").format(self.sign), title=_("Hospital Sign Event"))

        design = current_design(locked.name, for_update=True)
        events = sign_events(locked.name, for_update=True)
        states = sign_step_states(locked.name, design, events)

        if self.outcome != "Passed":
            # A failure or a waiver is a fact about what happened, not a claim
            # of progress. It is recordable at any point, including against a
            # design that has since been superseded — the record may need to
            # say that version 1 failed. Only a *pass* is gated.
            return

        targets_current = design is not None and self.design == design.name

        if not targets_current:
            # Production and Installation are refused outright: they are what
            # make the sign read as produced or installed.
            self._refuse_not_current_design(design)
            # Anything else against a superseded design has its own,
            # disconnected timeline and is left recordable without gating —
            # ``targetsCurrentDesign`` in the reference exists for exactly this
            # case. It clears nothing, because every state above is keyed to
            # the current design.
            return

        self._check_prerequisites(states, design)
        self._check_order(states)

    def _refuse_not_current_design(self, design) -> None:
        """A passing Production or Installation event must name the design
        whose readiness was actually verified."""
        if self.step not in ("Production", "Installation"):
            return
        frappe.throw(
            _(
                "{0} names design {1}, which is not this sign's current design{2}. A passing "
                "{0} event must target the design whose readiness was verified, not a "
                "superseded one — otherwise the sign reads as produced from artwork nobody "
                "verified, approved or proofed."
            ).format(
                self.step,
                self.design,
                _(" (version {0})").format(design.version_number) if design else "",
            ),
            title=_("Hospital Sign Event"),
        )

    def _check_prerequisites(self, states, design) -> None:
        blockers = sign_blockers(self.step, states, design)
        if not blockers:
            return
        frappe.throw(
            _(
                "{0} cannot pass yet: {1} of the {2} step(s) it depends on are not "
                "cleared for the current design:\n\n{3}"
            ).format(
                self.step,
                len(blockers),
                len(SIGN_PREREQUISITES[self.step]),
                "\n".join(blockers),
            ),
            title=_("Hospital Sign Event"),
        )

    def _check_order(self, states) -> None:
        violation = sign_sequence_violation(self.step, self.occurred_on, states)
        if violation:
            frappe.throw(violation, title=_("Hospital Sign Event"))
