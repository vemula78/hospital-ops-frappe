from __future__ import annotations

"""Derived state for Build & Publish — the single place signage readiness,
web-page publishability and UAT coverage are worked out.

Ported from ``src/server/domain/signage.ts`` (``getSignReadiness``,
``sequenceViolation``, ``targetsCurrentDesign``), ``website.ts``
(``missingForPublication``, ``latestStepOn``) and ``software.ts``
(``traceabilityWithin``, ``readinessFor``).

The rule all three modules share, and the reason they live in one file: **the
state is derived from the trail, never stored.** A sign's status is computed
from its submitted events against its *current* design; a page's status is its
latest step; a project's UAT coverage is computed from its results. A stored
column would be wrong the moment somebody records a proof approval a week
after it happened, or supersedes a design, and then nobody knows which of the
two to believe.

Two things every caller must respect:

- **Only submitted sign events count.** Every read filters ``docstatus = 1``,
  the same rule ``csr_financials`` applies to the money ledger: a draft is
  somebody thinking, not a step that happened.
- **``for_update=True`` when a decision depends on the answer.** MariaDB runs
  Frappe at REPEATABLE READ, so a plain read taken after a lock is served from
  the snapshot this transaction opened *before* the lock was granted — i.e. it
  misses exactly the concurrent write the lock had just waited for. That was
  the Phase 2 re-audit finding; the same reasoning applies here, where every
  gate is a check against a *set* of events that no unique index can express.
"""

import frappe
from frappe import _
from frappe.utils import getdate

# ---------------------------------------------------------------------------
# Signage
# ---------------------------------------------------------------------------

#: SIG-002's workflow, in the order the plan states it.
SIGN_STEPS = (
    "Content Verification",
    "Approval",
    "Print Proof",
    "Production",
    "Installation",
)

#: What must already have *cleared* (passed or waived) for the current design
#: before a step may pass. The reference gated only production and
#: installation on existence; this build gates every step on everything before
#: it, which is what the Phase 5 brief specifies — see the README for why.
SIGN_PREREQUISITES = {
    "Content Verification": (),
    "Approval": ("Content Verification",),
    "Print Proof": ("Content Verification", "Approval"),
    "Production": ("Content Verification", "Approval", "Print Proof"),
    "Installation": ("Content Verification", "Approval", "Print Proof", "Production"),
}

#: SIG-002's *chronological* half — ``sequenceViolation`` in signage.ts, ported
#: with its prerequisite map intact.
#:
#: A step dated before the step it depends on is refused. Existence alone is
#: not enough: an approval dated 10 July and a production event dated 1 July
#: both pass an existence check and are, once recorded, indistinguishable from
#: a correctly sequenced pair. And checking each prerequisite *individually*
#: is not enough either — a production event postdating content verification,
#: approval and print proof separately used to pass even when those three were
#: out of order against each other (print proof dated before the approval it
#: must follow), so approval is checked against content verification and print
#: proof against approval at the moment each is recorded (Codex audit on the
#: reference, round 2, defect 2). Installation depends only on production,
#: because production's own date was already checked against everything
#: beneath it when *it* was recorded.
SIGN_ORDER_PREREQUISITES = {
    "Content Verification": (),
    "Approval": ("Content Verification",),
    "Print Proof": ("Approval",),
    "Production": ("Content Verification", "Approval", "Print Proof"),
    "Installation": ("Production",),
}

#: SIG-005's six criteria. Six separate judgements: a sign can fail exactly one.
SIGN_ACCESSIBILITY_CRITERIA = (
    "Readability",
    "Contrast",
    "Symbols",
    "Viewing Distance",
    "Multilingual",
    "Mounting Height",
)

#: Outcomes that clear a gate. A waiver is an authorised exception and clears
#: it the same way a pass does; it stays in the trail carrying its reason,
#: which is why the reason is mandatory on the event.
CLEARING_OUTCOMES = ("Passed", "Waived")


def sign_designs(sign: str, for_update: bool = False) -> list[dict]:
    """Every design of a sign, newest version first."""
    suffix = " FOR UPDATE" if for_update else ""
    return frappe.db.sql(
        """
        SELECT name, version_number, superseded_by, supersede_reason, content_text
        FROM `tabHospital Sign Design`
        WHERE sign = %s
        ORDER BY version_number DESC
        """
        + suffix,
        (sign,),
        as_dict=True,
    )


def current_design(sign: str, for_update: bool = False) -> dict | None:
    """The live (not superseded) design of a sign, if there is one."""
    for row in sign_designs(sign, for_update=for_update):
        if not row.superseded_by:
            return row
    return None


def sign_events(sign: str, for_update: bool = False) -> list[dict]:
    """Every *submitted* event of a sign, oldest first.

    ``docstatus = 1`` only: the trail is append-only by submission, exactly
    like the CSR ledger, and a draft counts for nothing.
    """
    suffix = " FOR UPDATE" if for_update else ""
    return frappe.db.sql(
        """
        SELECT name, design, step, occurred_on, outcome, actor_name, note
        FROM `tabHospital Sign Event`
        WHERE sign = %s AND docstatus = 1
        ORDER BY occurred_on ASC, creation ASC
        """
        + suffix,
        (sign,),
        as_dict=True,
    )


def sign_step_states(
    sign: str, design: dict | None = None, events: list[dict] | None = None
) -> dict[str, dict]:
    """Per-step state for one sign, keyed to its **current** design.

    A step recorded against a design that has since been superseded reads
    ``for_an_older_design`` rather than counting — it is not that nobody
    approved anything, it is that nobody approved *this*. That is the whole
    reason readiness is keyed to the current design, and it is what makes
    superseding reset the chain.
    """
    if events is None:
        events = sign_events(sign)

    states: dict[str, dict] = {}
    for step in SIGN_STEPS:
        for_step = [event for event in events if event.step == step]

        if not for_step:
            states[step] = {
                "step": step,
                "state": "missing",
                "reason": _("Not recorded."),
                "occurred_on": None,
            }
            continue

        # An event naming no design cannot be attributed to one, so it does not
        # count either — but `design` is required on every event, so this only
        # arises for rows written before that rule existed.
        for_current = (
            [event for event in for_step if event.design == design.name] if design else []
        )

        if not for_current:
            other = for_step[-1]
            states[step] = {
                "step": step,
                "state": "for_an_older_design",
                "reason": (
                    _(
                        "Recorded on {0}, but against a design that has since been "
                        "superseded — not version {1}."
                    ).format(other.occurred_on, design.version_number)
                    if design
                    else _(
                        "Recorded on {0}, but this sign has no live design to attribute it to."
                    ).format(other.occurred_on)
                ),
                "occurred_on": None,
            }
            continue

        latest = for_current[-1]
        if latest.outcome == "Waived":
            states[step] = {
                "step": step,
                "state": "waived",
                "reason": _("Waived on {0}: {1}").format(
                    latest.occurred_on, latest.note or _("no reason recorded")
                ),
                "occurred_on": latest.occurred_on,
            }
        elif latest.outcome == "Failed":
            states[step] = {
                "step": step,
                "state": "failed",
                "reason": _("Failed on {0}: {1}").format(
                    latest.occurred_on, latest.note or _("no detail recorded")
                ),
                # Not a clearing date: a failure clears nothing.
                "occurred_on": None,
            }
        else:
            states[step] = {
                "step": step,
                "state": "passed",
                "reason": _("Passed on {0} for version {1}{2}.").format(
                    latest.occurred_on,
                    design.version_number,
                    _(" by {0}").format(latest.actor_name) if latest.actor_name else "",
                ),
                "occurred_on": latest.occurred_on,
            }

    return states


def sign_blockers(step: str, states: dict[str, dict], design: dict | None) -> list[str]:
    """What stops ``step`` from passing right now, named.

    Named rather than counted: "not ready" without the reason leaves the user
    guessing what to fix, and the thing they guess is usually to record the
    step anyway somewhere else.
    """
    blockers: list[str] = []
    if design is None:
        blockers.append(
            _("There is no live design. Nothing can proceed until one is added (SIG-003).")
        )
    for prerequisite in SIGN_PREREQUISITES.get(step, ()):
        state = states[prerequisite]
        if state["state"] in ("passed", "waived"):
            continue
        blockers.append("{0}: {1}".format(prerequisite, state["reason"]))
    return blockers


def sign_sequence_violation(step: str, occurred_on, states: dict[str, dict]) -> str | None:
    """SIG-002's chronological half — ``sequenceViolation`` in signage.ts."""
    occurred = getdate(occurred_on)
    for prerequisite in SIGN_ORDER_PREREQUISITES.get(step, ()):
        state = states[prerequisite]
        if state["state"] not in ("passed", "waived"):
            continue
        if state["occurred_on"] is None:
            continue
        if occurred < getdate(state["occurred_on"]):
            return _(
                "{0} is dated {1}, before {2} which was recorded on {3} — it cannot come "
                "before the step it depends on."
            ).format(step, occurred, prerequisite, state["occurred_on"])
    return None


def sign_status(sign: str, states: dict[str, dict] | None = None, design=None) -> str:
    """A sign's status, derived from its events — never stored.

    This is where the port improves on the reference. There, ``sign.status``
    was a stored column that ``recordWorkflowEvent`` advanced and ``addDesign``
    had to hand-downgrade in the same transaction, because a supersede has no
    workflow event of its own to derive a backward move from. Computing it on
    read removes the second writer entirely: supersede the design and the
    status falls back to Planned by itself, because Installed was only ever a
    statement about the design that is no longer live.
    """
    if states is None:
        design = current_design(sign)
        states = sign_step_states(sign, design)
    if design is None:
        return "Planned"
    if states["Installation"]["state"] in ("passed", "waived"):
        return "Installed"
    if states["Production"]["state"] in ("passed", "waived"):
        return "In Production"
    return "Planned"


def sign_readiness(sign: str, for_update: bool = False) -> dict:
    """Everything derived about one sign, from one read of its trail."""
    design = current_design(sign, for_update=for_update)
    events = sign_events(sign, for_update=for_update)
    states = sign_step_states(sign, design, events)
    return {
        "sign": sign,
        "design": design.name if design else None,
        "design_version": design.version_number if design else None,
        "status": sign_status(sign, states, design),
        "steps": states,
        "production_blockers": sign_blockers("Production", states, design),
        "installation_blockers": sign_blockers("Installation", states, design),
    }


def accessibility_checklist(sign: str) -> dict:
    """SIG-005's checklist, all six criteria, checked or not.

    An unchecked criterion reads ``Not Checked`` and counts against
    ``complete``. Reporting silence as compliance — or computing a percentage
    over only the criteria somebody got round to — is the empty-denominator
    trick that makes an accessibility target meaningless.
    """
    rows = frappe.db.sql(
        """
        SELECT criterion, verdict, checked_on, note
        FROM `tabHospital Sign Accessibility Check`
        WHERE sign = %s
        ORDER BY checked_on ASC, creation ASC
        """,
        (sign,),
        as_dict=True,
    )

    criteria = []
    for criterion in SIGN_ACCESSIBILITY_CRITERIA:
        matching = [row for row in rows if row.criterion == criterion]
        if not matching:
            criteria.append(
                {
                    "criterion": criterion,
                    "verdict": "Not Checked",
                    "reason": _(
                        "Nobody has judged this criterion. That is not the same as meeting it."
                    ),
                    "checked_on": None,
                }
            )
            continue
        latest = matching[-1]
        criteria.append(
            {
                "criterion": criterion,
                "verdict": latest.verdict,
                "reason": (
                    _("Met, checked on {0}.").format(latest.checked_on)
                    if latest.verdict == "Met"
                    else _("{0}, on {1}: {2}").format(
                        _(latest.verdict), latest.checked_on, latest.note or _("no detail recorded")
                    )
                ),
                "checked_on": str(latest.checked_on),
            }
        )

    def count(verdict: str) -> int:
        return len([row for row in criteria if row["verdict"] == verdict])

    return {
        "criteria": criteria,
        "met": count("Met"),
        "not_met": count("Not Met"),
        "not_applicable": count("Not Applicable"),
        "not_checked": count("Not Checked"),
        "complete": count("Not Met") == 0 and count("Not Checked") == 0,
    }


# ---------------------------------------------------------------------------
# Website
# ---------------------------------------------------------------------------

WEB_STEPS = ("Draft", "Review", "Approval", "Publication")


def latest_step_on(steps: list, step: str, until=None):
    """The most recent occurrence of ``step``, optionally bounded (inclusive).

    Scans rather than trusting row order — ``latestStepOn`` in website.ts does
    the same, for the same reason: rows arrive in insertion order, which is not
    date order once anything is backdated.
    """
    latest = None
    for row in steps:
        if row.get("step") != step:
            continue
        occurred = getdate(row.get("occurred_on"))
        if until is not None and occurred > getdate(until):
            continue
        if latest is None or occurred > latest:
            latest = occurred
    return latest


def missing_for_publication(steps: list, publish_on=None) -> list[str]:
    """What is missing before this page may be published (WEB-002).

    A recorded step is a step that happened and passed; a review that found
    problems is recorded as a return to Draft, with the note saying why. Three
    rules follow, and all three are computed against the *set* of steps rather
    than a "have they both happened" flag:

    - **A draft has to exist.** There is nothing to review otherwise.
    - **The review must be of the latest draft.** A later Draft means the
      content has changed since, so a review dated before that draft no longer
      counts — it reviewed a version of the page that no longer exists.
    - **The approval must come strictly after the review.** Dates here are
      calendar dates with no time component, so a review and an approval
      recorded on the *same* date do not demonstrate the required order
      either — they are simply unordered as far as this system can observe.
      ``>=`` (not ``>``) refuses that case alongside the plainly-reversed one.
      This is an audit fix on the reference and it is kept exactly.

    ``publish_on`` bounds every lookup: a step dated after the day the page
    went live did not clear that publication, and accepting it would let the
    trail read as though it had.
    """
    missing: list[str] = []

    draft_on = latest_step_on(steps, "Draft", publish_on)
    review_on = latest_step_on(steps, "Review", publish_on)
    approval_on = latest_step_on(steps, "Approval", publish_on)

    if draft_on is None:
        missing.append(
            _("No draft is recorded{0}. There is nothing to review or approve.").format(
                _(" on or before {0}").format(getdate(publish_on)) if publish_on else ""
            )
        )

    review_ok = False
    if review_on is None:
        missing.append(
            _("No review is recorded{0} (WEB-002).").format(
                _(" on or before {0}").format(getdate(publish_on)) if publish_on else ""
            )
        )
    elif draft_on is not None and review_on < draft_on:
        missing.append(
            _(
                "The review on {0} is stale: a later draft was recorded on {1}, and nobody "
                "has reviewed the content since."
            ).format(review_on, draft_on)
        )
    else:
        review_ok = True

    approval_ok = False
    if approval_on is None:
        missing.append(
            _("No approval is recorded{0} (WEB-002).").format(
                _(" on or before {0}").format(getdate(publish_on)) if publish_on else ""
            )
        )
    elif draft_on is not None and approval_on < draft_on:
        missing.append(
            _(
                "The approval on {0} is stale: a later draft was recorded on {1}, and nobody "
                "has approved the content since."
            ).format(approval_on, draft_on)
        )
    else:
        approval_ok = True

    # Order only makes sense to check once both are individually current — a
    # stale or missing step is already named above, and complaining about the
    # order of a stale date would just be a second complaint about one event.
    if review_ok and approval_ok and review_on >= approval_on:
        missing.append(
            _(
                "The review and the approval are both dated {0}; a shared date cannot show "
                "that the review happened before the approval, and WEB-002 requires review "
                "before approval."
            ).format(review_on)
            if review_on == approval_on
            else _(
                "The review on {0} happened after the approval on {1}; WEB-002 requires "
                "review before approval."
            ).format(review_on, approval_on)
        )

    return missing


def page_status(steps: list) -> str:
    """The latest step is the state (WEB-002).

    Ordered by the day it happened and then by the order it was entered, so a
    page published and later returned to Draft reads as Draft — which is the
    whole reason the workflow is rows rather than a status column.
    """
    if not steps:
        return "Not Started"
    ordered = sorted(
        list(enumerate(steps)), key=lambda pair: (getdate(pair[1].get("occurred_on")), pair[0])
    )
    return ordered[-1][1].get("step")


def page_state(page: str) -> dict:
    """Everything derived about one page, from one read of its steps."""
    steps = frappe.db.sql(
        """
        SELECT step, occurred_on, note
        FROM `tabHospital Web Page Step`
        WHERE parent = %s AND parenttype = 'Hospital Web Page'
        ORDER BY idx ASC
        """,
        (page,),
        as_dict=True,
    )
    blockers = missing_for_publication(steps)
    return {
        "page": page,
        "status": page_status(steps),
        "steps": steps,
        "publication_blockers": blockers,
        "can_publish": not blockers,
    }


# ---------------------------------------------------------------------------
# Software
# ---------------------------------------------------------------------------


def uat_results(project: str, for_update: bool = False) -> list[dict]:
    suffix = " FOR UPDATE" if for_update else ""
    return frappe.db.sql(
        """
        SELECT name, requirement, tested_on, result, tester_name, note
        FROM `tabSoftware UAT Result`
        WHERE software_project = %s
        ORDER BY tested_on ASC, creation ASC
        """
        + suffix,
        (project,),
        as_dict=True,
    )


def requirement_rows(project: str, for_update: bool = False) -> list[dict]:
    suffix = " FOR UPDATE" if for_update else ""
    return frappe.db.sql(
        """
        SELECT name, description, agreed_on, idx
        FROM `tabSoftware Requirement Item`
        WHERE parent = %s AND parenttype = 'Software Project Record'
        ORDER BY idx ASC
        """
        + suffix,
        (project,),
        as_dict=True,
    )


def uat_coverage(project: str, for_update: bool = False) -> dict:
    """Which requirements are covered by a passing UAT result, and which are not.

    **A requirement with no UAT result reads "not tested", never "passed".**
    An empty denominator is the cheapest way to fake a green board — the same
    rule the CSR ledger applies to an unconfigured evidence scope.

    A passing result only counts if it is *newer than the day the requirement
    was agreed*. A pass recorded against the requirement as it stood before it
    was agreed tested something else; keeping that pass would let a requirement
    be changed after the fact and stay green.
    """
    requirements = requirement_rows(project, for_update=for_update)
    results = uat_results(project, for_update=for_update)

    covered, uncovered = [], []
    for row in requirements:
        agreed = getdate(row.agreed_on)
        passing = [
            result
            for result in results
            if result.requirement == row.name
            and result.result == "Passed"
            and getdate(result.tested_on) > agreed
        ]
        any_result = [result for result in results if result.requirement == row.name]

        if passing:
            covered.append(
                {
                    "requirement": row.name,
                    "description": row.description,
                    "agreed_on": str(row.agreed_on),
                    "reason": _("{0} passing result(s) recorded after {1}.").format(
                        len(passing), agreed
                    ),
                }
            )
            continue

        if not any_result:
            reason = _(
                "No UAT result is recorded against it, so it has not been shown to work."
            )
        elif not [result for result in any_result if result.result == "Passed"]:
            reason = _("{0} result(s) recorded, none of them passing.").format(len(any_result))
        else:
            reason = _(
                "Every passing result predates the day the requirement was agreed ({0}), so "
                "none of them tested what was agreed."
            ).format(agreed)

        uncovered.append(
            {
                "requirement": row.name,
                "description": row.description,
                "agreed_on": str(row.agreed_on),
                "reason": reason,
            }
        )

    return {
        "project": project,
        "requirements": len(requirements),
        "covered": covered,
        "uncovered": uncovered,
        "blockers": [
            "{0}: {1}".format(row["description"], row["reason"]) for row in uncovered
        ],
    }
