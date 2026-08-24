from __future__ import annotations

"""Software Project Record — requirements, UAT, and a one-shot release.

Ported from ``addRequirement``, ``recordUatResult`` and ``markReleaseDeployed``
in ``src/server/domain/software.ts``.

**The project is the aggregate, and all three whitelisted methods lock it.**
Release readiness is a check against a *set* of requirements and their UAT
results, which no unique index can express: under REPEATABLE READ two
concurrent writers each see only their own effect, which is how two
expenditures both skipped their warnings in the CSR ledger. ``add_requirement``
and ``record_uat_result`` take the same lock as ``record_release`` on the same
key, so neither can commit a disqualifying row while a release is mid-flight
and neither can be missed by a release that started after it committed. Every
read the decision depends on is a ``FOR UPDATE`` read taken *after* the lock.

**Released is terminal and is set only by ``record_release``.** Enforced in
``validate()`` and not only by ``read_only`` in the JSON (the Phase 2 P3-2
lesson), covering the insert path too: a project cannot be *born* Released,
and once it is Released neither the status nor the release date can move by a
direct save. A release that can be un-released is not a release; a correction
is a new project record, not an edit to this one.

**A requirement with no passing UAT result blocks the release, and a project
with no requirements at all blocks it too.** An empty denominator is the
cheapest way to fake a green board — the same rule the CSR ledger applies to
an unconfigured evidence scope, and the reference's "not tested is never
passed".
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.build_publish import requirement_rows, uat_coverage
from hospital_ops.hospital_ops.permissions import get_doc_for_action

#: Fields only ``record_release`` may move.
RELEASE_FIELDS = ("status", "released_on")


class SoftwareProjectRecord(Document):
    def validate(self) -> None:
        self._guard_release_state()
        self._guard_requirements()

    def _guard_release_state(self) -> None:
        if self.flags.releasing:
            return

        if self.is_new():
            if self.status == "Released":
                frappe.throw(
                    _(
                        "A project cannot be created already Released. Create it Active, then "
                        "release it with record_release() once every requirement actually has "
                        "a passing UAT result."
                    ),
                    title=_("Software Project Record"),
                )
            if self.released_on:
                frappe.throw(
                    _("A new project cannot carry a release date — record_release() stamps it."),
                    title=_("Software Project Record"),
                )
            return

        stored = frappe.db.get_value(
            "Software Project Record", self.name, list(RELEASE_FIELDS), as_dict=True
        )
        if not stored:
            return

        changed = [
            field
            for field in RELEASE_FIELDS
            if str(self.get(field) or "") != str(stored.get(field) or "")
        ]
        if not changed:
            return

        if stored.status == "Released":
            frappe.throw(
                _(
                    "{0} was released on {1}. Released is terminal: neither the status nor "
                    "the release date can be changed, by a direct save or otherwise. A "
                    "release that can be un-released is not a release."
                ).format(self.name, stored.released_on),
                title=_("Software Project Record"),
            )

        if self.status == "Released" or "released_on" in changed:
            frappe.throw(
                _(
                    "Released is set only by record_release(), which refuses unless every "
                    "requirement has a passing UAT result newer than the day it was agreed. "
                    "A direct save would skip that gate entirely."
                ),
                title=_("Software Project Record"),
            )

    def _guard_requirements(self) -> None:
        """Requirements arrive only through ``add_requirement``.

        That method is where the project lock and the Released refusal live;
        a row typed into the grid would bypass both. Editing an existing row's
        text is refused for the same reason the release gate exists: the gate
        compares a passing UAT result against the day the requirement was
        agreed, and rewriting the requirement after it passed would leave the
        pass attached to something nobody agreed to.
        """
        if self.flags.adding_requirement:
            return

        before = self.get_doc_before_save()

        if before is None:
            if self.requirements:
                frappe.throw(
                    _(
                        "A project cannot be created with requirements already on it. "
                        "Requirements are added by add_requirement(), under a lock on the "
                        "project."
                    ),
                    title=_("Software Project Record"),
                )
            return

        before_by_name = {row.name: row for row in (before.requirements or [])}

        for row in self.requirements:
            prior = before_by_name.get(row.name)
            if prior is None:
                frappe.throw(
                    _(
                        "A requirement cannot be added by a direct save — add_requirement() "
                        "is the only path, because it is what refuses adding to a Released "
                        "project and what serialises against a release in flight."
                    ),
                    title=_("Software Project Record"),
                )
            if (row.description or "") != (prior.description or "") or str(
                row.agreed_on or ""
            ) != str(prior.agreed_on or ""):
                frappe.throw(
                    _(
                        "A requirement's wording and agreed date cannot be changed: the "
                        "release gate compares each passing UAT result against them, and "
                        "rewriting one afterwards would leave the pass attached to something "
                        "nobody agreed to. Add a further requirement instead."
                    ),
                    title=_("Software Project Record"),
                )


def _lock_project(name: str) -> frappe._dict:
    """The locked read every decision below is taken on.

    The lock comes first and the decisive values — ``status`` above all — come
    out of that same read rather than from a query taken before it. Reading the
    status ahead of the lock was a Codex Phase 3 finding (P2-a): a Release
    committed in the gap between the two reads would be invisible to the
    refusal, and a prohibited row would land against a project that was, by the
    time it committed, released.
    """
    locked = frappe.db.get_value(
        "Software Project Record",
        name,
        ["name", "status", "released_on"],
        as_dict=True,
        for_update=True,
    )
    if not locked:
        frappe.throw(_("{0} is not a software project.").format(name), title=_("Software Project Record"))
    return locked


@frappe.whitelist()
def add_requirement(name: str, description: str, agreed_on: str | None = None) -> dict:
    """Adds one agreed requirement (SFT-002), under the project lock."""
    doc = get_doc_for_action("Software Project Record", name, ptype="write")
    locked = _lock_project(doc.name)

    if locked.status == "Released":
        frappe.throw(
            _(
                "{0} was released on {1}. A requirement added now was not part of what was "
                "released, and adding it would make the release read as though it had "
                "delivered something nobody tested. Record a new project for the next "
                "version."
            ).format(locked.name, locked.released_on),
            title=_("Software Project Record"),
        )

    if not (description or "").strip():
        frappe.throw(
            _("A requirement needs a description."), title=_("Software Project Record")
        )

    doc.reload()
    doc.append(
        "requirements",
        {"description": description.strip(), "agreed_on": agreed_on or today()},
    )
    doc.flags.adding_requirement = True
    doc.save()

    row = doc.requirements[-1]
    return {
        "project": doc.name,
        "requirement": row.name,
        "description": row.description,
        "agreed_on": str(row.agreed_on),
    }


@frappe.whitelist()
def record_uat_result(
    name: str,
    requirement: str,
    result: str,
    tested_on: str | None = None,
    tester_name: str | None = None,
    note: str | None = None,
) -> dict:
    """Records one UAT verdict (SFT-005), under the project lock."""
    doc = get_doc_for_action("Software Project Record", name, ptype="write")
    locked = _lock_project(doc.name)

    if locked.status in ("Released", "Abandoned"):
        frappe.throw(
            _(
                "{0} is {1}. A UAT result recorded now cannot change what was released or "
                "abandoned, and recording one would make the trail read as though the "
                "decision had been taken with it in hand."
            ).format(locked.name, _(locked.status)),
            title=_("Software Project Record"),
        )

    if result not in ("Passed", "Failed"):
        frappe.throw(_("{0} is not a UAT result.").format(result), title=_("Software Project Record"))

    # The requirement must belong to *this* project: a bare row-name lookup
    # would attach one project's test evidence to another's requirement, which
    # is the same class of hole the reference's tenancy rule closed.
    rows = {row.name: row for row in requirement_rows(locked.name, for_update=True)}
    row = rows.get(requirement)
    if row is None:
        frappe.throw(
            _("{0} is not a requirement of {1}.").format(requirement, locked.name),
            title=_("Software Project Record"),
        )

    uat = frappe.get_doc(
        {
            "doctype": "Software UAT Result",
            "software_project": locked.name,
            "requirement": requirement,
            "requirement_description": row.description,
            "tested_on": tested_on or today(),
            "result": result,
            "tester_name": tester_name,
            "note": note,
        }
    )
    uat.flags.recording_result = True
    uat.insert()

    return {
        "project": locked.name,
        "name": uat.name,
        "requirement": requirement,
        "result": result,
        "tested_on": str(uat.tested_on),
    }


@frappe.whitelist()
def record_release(name: str, released_on: str | None = None) -> dict:
    """Releases a project, once (SFT-006).

    Refused unless every requirement has at least one passing UAT result dated
    **after** the day the requirement was agreed. Not overridable: unlike a
    cross-stage money comparison, "nobody tested this" is not a thing that
    genuinely happens and has to be accepted.

    A project with no requirements at all is refused too. Nothing to test is
    not the same as everything tested, and an empty denominator is the cheapest
    way to make a release read green.
    """
    doc = get_doc_for_action("Software Project Record", name, ptype="write")
    locked = _lock_project(doc.name)

    if locked.status == "Released":
        frappe.throw(
            _("{0} was already released on {1}.").format(locked.name, locked.released_on),
            title=_("Software Project Record"),
        )
    if locked.status == "Abandoned":
        frappe.throw(
            _(
                "{0} is Abandoned. Abandoning is a recorded decision not to deliver; set it "
                "back to Active before releasing it."
            ).format(locked.name),
            title=_("Software Project Record"),
        )

    coverage = uat_coverage(locked.name, for_update=True)

    if coverage["requirements"] == 0:
        frappe.throw(
            _(
                "{0} has no requirements recorded, so there is nothing a release could be "
                "said to deliver. Nothing to test is not the same as everything tested."
            ).format(locked.name),
            title=_("Software Project Record"),
        )

    if coverage["blockers"]:
        frappe.throw(
            _(
                "{0} cannot be released. {1} of {2} requirement(s) have no passing UAT "
                "result recorded after the day they were agreed:\n\n{3}"
            ).format(
                locked.name,
                len(coverage["uncovered"]),
                coverage["requirements"],
                "\n".join(coverage["blockers"]),
            ),
            title=_("Software Project Record"),
        )

    doc.reload()
    doc.flags.releasing = True
    doc.status = "Released"
    doc.released_on = released_on or today()
    doc.save()

    return {
        "project": doc.name,
        "status": doc.status,
        "released_on": str(doc.released_on),
        "requirements": coverage["requirements"],
    }


@frappe.whitelist()
def get_project_state(name: str) -> dict:
    """UAT coverage for one project (read-only, no lock)."""
    doc = get_doc_for_action("Software Project Record", name, ptype="read")
    state = uat_coverage(doc.name)
    state["status"] = doc.status
    state["released_on"] = str(doc.released_on) if doc.released_on else None
    return state
