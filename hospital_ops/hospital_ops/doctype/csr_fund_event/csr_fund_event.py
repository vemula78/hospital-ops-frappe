from __future__ import annotations

"""The CSR money ledger — append-only, by submission.

Ported from ``src/server/domain/csr.ts`` (``recordFundEvent``) and the
append-only triggers in the reference schema. Frappe's submittable document
supplies what the Postgres ``BEFORE UPDATE OR DELETE`` triggers supplied
there: once submitted, the row cannot be edited. What Frappe does *not*
supply is the refusal to cancel, so this controller adds it.

Five invariants, each of which was a real defect in the reference build:

1. **Only submitted events count.** Every sum filters ``docstatus = 1``. A
   draft is somebody thinking, not a movement of money.
2. **A correction is a reversal entry, never a cancellation.** ``on_cancel``
   throws. Cancelling would silently remove the original from every figure
   and leave no record that it had ever been recorded — the trail must show
   the mistake and the correction, not the corrected state alone.
3. **Direction lives in the kind, never in the sign.** ``amount`` must be
   greater than zero on every kind.
4. **A reversal cannot exceed what remains of what it reverses.** Checked
   under the project lock against the *submitted* reversals already pointing
   at the same original. Without it a refund larger than the receipt produces
   a negative figure that no cross-stage comparison catches, because every
   comparison asks whether one figure *exceeds* another and a negative one
   never does.
5. **Cross-stage comparisons are overridable warnings, not constraints.**
   Spending more than has been received genuinely happens — the institution
   carries the difference until the next tranche lands. So the submit is
   refused once with the figures named, and a confirmation is accepted only
   when it carries a reason and repeats the exact warning string back. Change
   the amount and the string changes, so a stale confirmation is refused.

Two states are *not* overridable: an expenditure on a Closed project, and any
event at all on a Cancelled one. Those are decisions already taken, not
figures that disagree.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from hospital_ops.hospital_ops.csr_financials import (
    KIND_SIGNS,
    REVERSAL_OF,
    format_inr,
    project_event_totals,
)

#: How the warnings a refusal issued are stored on the confirming entry.
WARNING_JOINER = " | "


class CSRFundEvent(Document):
    # -- lifecycle ------------------------------------------------------------

    def validate(self) -> None:
        self._check_amount_positive()
        self._check_reversal_shape()

    def before_submit(self) -> None:
        self._check_against_locked_project()

    def before_cancel(self) -> None:
        self._refuse_cancel()

    def on_cancel(self) -> None:
        # Belt and braces: before_cancel is the earliest refusal, on_cancel is
        # the one the build brief names. Either raising rolls the whole
        # cancellation back.
        self._refuse_cancel()

    def on_trash(self) -> None:
        """Defence in depth against deleting a submitted entry.

        Frappe core already refuses this — ``delete_doc.py:289-297`` throws
        "Submitted Record cannot be deleted. You must Cancel it first" for any
        submittable doctype whose ``docstatus.is_submitted()`` — and because
        cancellation is itself refused above, deleting a submitted event is
        transitively impossible today. This ``if`` costs one branch and keeps
        the invariant true in this app's own code rather than depending on
        core keeping that behaviour forever. A *draft* deletes normally: a
        draft counts for nothing, so removing one removes nothing.
        """
        if self.docstatus == 1:
            frappe.throw(
                _(
                    "A submitted fund event cannot be deleted. The ledger is append-only: "
                    "record a reversal entry instead."
                ),
                title=_("CSR Fund Event"),
            )

    def _refuse_cancel(self) -> None:
        frappe.throw(
            _(
                "A submitted fund event cannot be cancelled. Record a {0} entry pointing at "
                "{1} instead — a correction is a reversal, never a cancellation, so the "
                "trail shows both what was recorded and what corrected it."
            ).format(_(self._reversal_kind_for_self()), self.name),
            title=_("CSR Fund Event"),
        )

    def _reversal_kind_for_self(self) -> str:
        for reversal, original in REVERSAL_OF.items():
            if original == self.kind:
                return reversal
        return "reversing"

    # -- shape validation -----------------------------------------------------

    def _check_amount_positive(self) -> None:
        if flt(self.amount) <= 0:
            frappe.throw(
                _(
                    "The amount must be greater than zero. Direction is recorded in the Kind "
                    "({0}), never in the sign of the amount."
                ).format(self.kind),
                title=_("CSR Fund Event"),
            )

        # Sub-paise precision is a silent falsehood: 0.004 is a real submitted
        # event that every total displays as ₹0.00, so the ledger and the page
        # disagree and the page is the one anybody reads.
        if flt(self.amount) != flt(self.amount, 2):
            frappe.throw(
                _(
                    "Amounts are rupees and paise; more than two decimal places would be "
                    "invisible in every total. {0} was entered."
                ).format(self.amount),
                title=_("CSR Fund Event"),
            )

    def _check_reversal_shape(self) -> None:
        expected_original = REVERSAL_OF.get(self.kind)

        if not expected_original:
            if self.reverses:
                frappe.throw(
                    _("Only a Reversal points at another event; {0} must not.").format(
                        _(self.kind)
                    ),
                    title=_("CSR Fund Event"),
                )
            return

        if not self.reverses:
            frappe.throw(
                _("A {0} must name the event it reverses.").format(_(self.kind)),
                title=_("CSR Fund Event"),
            )

        if self.reverses == self.name:
            frappe.throw(
                _("An event cannot reverse itself."), title=_("CSR Fund Event")
            )

        original = frappe.db.get_value(
            "CSR Fund Event",
            self.reverses,
            ["kind", "csr_project", "docstatus", "amount"],
            as_dict=True,
        )
        if not original:
            frappe.throw(
                _("{0} is not a fund event.").format(self.reverses),
                title=_("CSR Fund Event"),
            )
        if original.docstatus != 1:
            frappe.throw(
                _(
                    "{0} is not submitted, so there is nothing to reverse — a draft counts "
                    "for nothing."
                ).format(self.reverses),
                title=_("CSR Fund Event"),
            )
        if original.kind != expected_original:
            frappe.throw(
                _("A {0} must reverse a {1}; {2} is a {3}.").format(
                    _(self.kind), _(expected_original), self.reverses, _(original.kind)
                ),
                title=_("CSR Fund Event"),
            )
        if original.csr_project != self.csr_project:
            frappe.throw(
                _(
                    "{0} belongs to project {1}, not {2}. A reversal stays on the project it "
                    "corrects."
                ).format(self.reverses, original.csr_project, self.csr_project),
                title=_("CSR Fund Event"),
            )

    # -- submit-time checks ---------------------------------------------------

    def _check_against_locked_project(self) -> None:
        """Every submit-time decision, taken on one locked read of the project.

        The order below is load-bearing, and the *source* of the values is as
        load-bearing as the order. The ``for_update=True`` read comes first: it
        is the lock every submitter on this project contends for. Everything
        after it — the Closed/Cancelled refusals, the sanction figure the
        warnings compare against — is decided on the row that read returned,
        never on a value fetched before the lock was granted.

        That last point is a Codex Phase 3 finding (P2-a). The status used to
        be read *ahead* of the lock, so a Close or Cancel committed in the gap
        between the two reads would be invisible to the refusal and a
        prohibited event would submit against a project that was, by the time
        it landed, closed.

        The sums that follow are themselves locking reads, because InnoDB's
        REPEATABLE READ would otherwise serve a non-locking read from the
        snapshot this transaction took before the lock was granted — i.e. it
        would miss the concurrent expenditure the lock had just waited for.
        That is the hole the Phase 2 re-audit found, and the same reasoning
        applies to the status: a plain re-read after the lock would not be
        enough, which is why the status comes out of the locking read itself
        rather than from a second query.
        """
        project = frappe.db.get_value(
            "CSR Project",
            self.csr_project,
            ["name", "sanctioned_amount", "status"],
            as_dict=True,
            for_update=True,
        )
        if not project:
            frappe.throw(
                _("{0} is not a CSR project.").format(self.csr_project),
                title=_("CSR Fund Event"),
            )

        self._check_project_state(project.status)

        if self.reverses:
            self._check_reversal_ceiling()

        before = project_event_totals(self.csr_project, for_update=True)
        after = self._apply_self(before)

        warnings = cross_stage_warnings(flt(project.sanctioned_amount, 2), after)
        self._resolve_warnings(warnings)

    def _check_project_state(self, status: str) -> None:
        """Refusals that are decisions already taken, not figures that
        disagree — so neither is overridable.

        ``status`` is passed in from the locked read rather than fetched here,
        so this cannot be called with a pre-lock value by accident.
        """
        if status == "Cancelled":
            frappe.throw(
                _(
                    "Project {0} is Cancelled. No fund event can be recorded against it, and "
                    "this is not overridable."
                ).format(self.csr_project),
                title=_("CSR Fund Event"),
            )

        if status == "Closed" and self.kind == "Expenditure":
            frappe.throw(
                _(
                    "Project {0} is Closed. Further expenditure cannot be recorded against "
                    "it, and this is not overridable — reopen the project if the spend is "
                    "genuine. (A reversal correcting an earlier entry is still allowed.)"
                ).format(self.csr_project),
                title=_("CSR Fund Event"),
            )

    def _check_reversal_ceiling(self) -> None:
        original_amount = flt(
            frappe.db.get_value("CSR Fund Event", self.reverses, "amount"), 2
        )
        already_reversed = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM `tabCSR Fund Event`
                WHERE reverses = %s AND docstatus = 1 AND name != %s
                FOR UPDATE
                """,
                (self.reverses, self.name),
            )[0][0],
            2,
        )
        remaining = flt(original_amount - already_reversed, 2)

        if flt(self.amount, 2) > remaining:
            frappe.throw(
                _(
                    "{0} cannot be reversed against {1}: it was {2} and {3} has already been "
                    "reversed, so only {4} remains reversible."
                ).format(
                    format_inr(self.amount),
                    self.reverses,
                    format_inr(original_amount),
                    format_inr(already_reversed),
                    format_inr(remaining),
                ),
                title=_("CSR Fund Event"),
            )

    def _apply_self(self, before: dict[str, float]) -> dict[str, float]:
        """The figures this event would produce, were it to submit.

        The row is not in the database as submitted yet — ``before_submit``
        runs ahead of the write — so its own effect is added here rather than
        re-queried. Same arithmetic, same direction table.
        """
        after = dict(before)
        bucket, sign = KIND_SIGNS[self.kind]
        after[bucket] = flt(after[bucket] + sign * flt(self.amount), 2)
        after["balance"] = flt(after["received"] - after["spent"], 2)
        return after

    def _resolve_warnings(self, warnings: list[str]) -> None:
        acknowledged = (self.override_acknowledges or "").strip()
        reason = (self.override_reason or "").strip()

        if not warnings:
            if self.override_confirmed:
                # An override answering nothing is either a stale form or a
                # habit; either way it must not be recorded as if a warning
                # had been considered.
                frappe.throw(
                    _("This entry produces no warning, so there is nothing to override."),
                    title=_("CSR Fund Event"),
                )
            return

        canonical = WARNING_JOINER.join(warnings)

        if not self.override_confirmed:
            frappe.throw(
                _(
                    "{0}\n\nThis is allowed, but it must be acknowledged. To record it anyway, "
                    "tick Override Confirmed, give a reason, and set Override Acknowledges to "
                    "exactly:\n{1}"
                ).format(canonical, canonical),
                title=_("CSR Fund Event"),
            )

        # Enforced in the controller, not only in the form: a direct API call
        # must not be able to record an override with no reason. An exception
        # without a reason is not an exception, it is an unexplained figure.
        if not reason:
            frappe.throw(
                _("Say why this is being recorded despite the warning."),
                title=_("CSR Fund Event"),
            )

        if acknowledged != canonical:
            frappe.throw(
                _(
                    "The entry changed since the warning was shown, so the acknowledgement no "
                    "longer answers it. The warning now reads:\n{0}"
                ).format(canonical),
                title=_("CSR Fund Event"),
            )


def cross_stage_warnings(sanctioned: float, after: dict[str, float]) -> list[str]:
    """The warnings a set of figures produces (reference: CSR-004).

    Every one of these is a warning and none is a constraint. A database
    constraint here would eventually force someone to enter a false figure:
    money genuinely arrives above the sanction, and the institution genuinely
    carries a spend before the tranche lands.
    """
    warnings: list[str] = []

    if sanctioned and flt(after["received"], 2) > flt(sanctioned, 2):
        warnings.append(
            "Received {0} against a sanction of {1}.".format(
                format_inr(after["received"]), format_inr(sanctioned)
            )
        )
    if flt(after["spent"], 2) > flt(after["received"], 2):
        warnings.append(
            "Spent {0} against {1} received — the difference is being carried by the "
            "institution.".format(format_inr(after["spent"]), format_inr(after["received"]))
        )
    return warnings


@frappe.whitelist()
def preview_warnings(csr_project: str, kind: str, amount: float) -> dict:
    """What warnings an entry *would* produce, without writing anything.

    Lets the desk show the acknowledgement text before a refusal has been
    provoked. It reads without a lock deliberately — it decides nothing, and
    the authoritative check is the one inside ``before_submit``.
    """
    from hospital_ops.hospital_ops.permissions import get_doc_for_action

    project = get_doc_for_action("CSR Project", csr_project, ptype="read")
    if kind not in KIND_SIGNS:
        frappe.throw(_("{0} is not a fund event kind.").format(kind))

    before = project_event_totals(project.name)
    after = dict(before)
    bucket, sign = KIND_SIGNS[kind]
    after[bucket] = flt(after[bucket] + sign * flt(amount), 2)
    after["balance"] = flt(after["received"] - after["spent"], 2)

    warnings = cross_stage_warnings(flt(project.sanctioned_amount, 2), after)
    return {
        "warnings": warnings,
        "acknowledgement": WARNING_JOINER.join(warnings),
        "figures": after,
    }
