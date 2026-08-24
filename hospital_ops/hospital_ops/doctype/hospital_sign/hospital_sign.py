from __future__ import annotations

"""Hospital Sign — the register (SIG-001).

**There is no status column, deliberately.** The reference
(``src/server/domain/signage.ts``) stored one and paid for it: ``addDesign``
had to hand-downgrade ``status``, ``installedOn`` and ``photographDocumentId``
back to Planned inside the supersede transaction, because a supersede has no
workflow event of its own for the status to derive a *backward* move from, and
without that the sign kept claiming "Installed" for artwork nobody had
approved. Computing status on read removes the second writer entirely — see
``build_publish.sign_status``.

Everything decided about a sign lives in its events. This controller is
therefore almost empty; the rules are in ``Hospital Sign Event`` (the gates),
``Hospital Sign Design`` (versioning and supersede) and
``Hospital Sign Accessibility Check`` (SIG-005).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.build_publish import (
    accessibility_checklist,
    current_design,
    sign_readiness,
)
from hospital_ops.hospital_ops.permissions import get_doc_for_action


class HospitalSign(Document):
    def validate(self) -> None:
        for field, label in (("width_mm", _("Width")), ("height_mm", _("Height"))):
            value = self.get(field)
            if value is not None and value != "" and int(value) <= 0:
                frappe.throw(
                    _("{0} must be greater than zero, or left blank.").format(label),
                    title=_("Hospital Sign"),
                )


@frappe.whitelist()
def get_sign_state(name: str) -> dict:
    """The derived readiness and accessibility checklist for one sign.

    Read-only, so no lock: it decides nothing. The authoritative checks are
    the ones inside ``Hospital Sign Event.before_submit``, taken under the
    sign lock.
    """
    doc = get_doc_for_action("Hospital Sign", name, ptype="read")
    state = sign_readiness(doc.name)
    state["accessibility"] = accessibility_checklist(doc.name)
    return state


@frappe.whitelist()
def add_design(
    name: str,
    content_text: str | None = None,
    print_ready: str | None = None,
    supersede_reason: str | None = None,
) -> dict:
    """Adds a print-ready design, superseding the one it replaces (SIG-003).

    The sign is the aggregate and it is locked first. The version number is
    ``max + 1``, a read-then-write against a *count*: under REPEATABLE READ two
    concurrent calls would each see only their own row, which is exactly how
    two uploads both claimed version 1 in the reference's documents module. The
    unique constraint on (sign, version) would abort one of them with a
    constraint error rather than assigning the next number, which is not the
    same thing — so the lock does the work and the constraint is the backstop.

    The reason is required whenever there is a live design to replace, and it
    is required *here* rather than only in the form: a superseded design with
    no stated reason leaves nobody able to say whether version 3 fixed a typo
    or corrected a department name, and it is the second of those that must
    never be printed from version 2.

    **Approvals do not carry forward.** Readiness is keyed to the current
    design, so adding one puts the sign back behind every gate — which is the
    behaviour that makes the gate mean anything. The returned notice says so.
    """
    doc = get_doc_for_action("Hospital Sign", name, ptype="write")

    # The lock comes first, and every read that the decision depends on is
    # taken after it with FOR UPDATE — a plain read would be served from this
    # transaction's pre-lock snapshot (the Phase 2 re-audit lesson).
    locked = frappe.db.get_value(
        "Hospital Sign", doc.name, ["name"], as_dict=True, for_update=True
    )
    if not locked:
        frappe.throw(_("{0} is not a sign.").format(name), title=_("Hospital Sign"))

    previous = current_design(locked.name, for_update=True)
    reason = (supersede_reason or "").strip()
    if previous and not reason:
        frappe.throw(
            _(
                "Version {0} is the live design of this sign. Adding another supersedes it, "
                "and superseding needs a reason: without one nobody can say whether the new "
                "version fixed a typo or corrected a department name, and it is the second "
                "of those that must never be printed from the old artwork."
            ).format(previous.version_number),
            title=_("Hospital Sign Design"),
        )
    if not previous and reason:
        frappe.throw(
            _("This sign has no live design, so there is nothing to supersede."),
            title=_("Hospital Sign Design"),
        )

    highest = (
        frappe.db.sql(
            """
            SELECT COALESCE(MAX(version_number), 0)
            FROM `tabHospital Sign Design`
            WHERE sign = %s
            FOR UPDATE
            """,
            (locked.name,),
        )[0][0]
        or 0
    )

    design = frappe.get_doc(
        {
            "doctype": "Hospital Sign Design",
            "sign": locked.name,
            "version_number": int(highest) + 1,
            "content_text": content_text,
            "print_ready": print_ready,
        }
    )
    design.flags.adding_design = True
    design.insert()

    if previous:
        old = frappe.get_doc("Hospital Sign Design", previous.name)
        old.flags.adding_design = True
        old.superseded_by = design.name
        old.supersede_reason = reason
        old.save()

    return {
        "sign": locked.name,
        "design": design.name,
        "version_number": design.version_number,
        "superseded": previous.name if previous else None,
        "superseded_version": previous.version_number if previous else None,
        "notice": _(
            "Version {0} is now the live design. Approvals do not carry forward: content "
            "verification and approval are needed again for this version before anything "
            "can be printed."
        ).format(design.version_number)
        if previous
        else _("Version 1 is the live design."),
    }


@frappe.whitelist()
def record_accessibility_check(
    name: str,
    criterion: str,
    verdict: str,
    design: str | None = None,
    checked_on: str | None = None,
    note: str | None = None,
) -> dict:
    """Records one accessibility verdict (SIG-005)."""
    doc = get_doc_for_action("Hospital Sign", name, ptype="write")
    check = frappe.get_doc(
        {
            "doctype": "Hospital Sign Accessibility Check",
            "sign": doc.name,
            "design": design,
            "criterion": criterion,
            "verdict": verdict,
            "checked_on": checked_on or today(),
            "note": note,
        }
    ).insert()
    return {"name": check.name, "sign": doc.name, "criterion": criterion, "verdict": verdict}
