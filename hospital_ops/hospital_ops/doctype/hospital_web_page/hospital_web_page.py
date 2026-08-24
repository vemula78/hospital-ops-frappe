from __future__ import annotations

"""Hospital Web Page — WEB-002's publication workflow.

Ported from ``recordWorkflowEvent`` and ``missingForPublication`` in
``src/server/domain/website.ts``.

**Publication state is derived from the steps, never stored.** The latest step
wins, so a page that was published and then pulled back to Draft reads as
Draft, and recording a late approval moves the page without anybody editing a
second field. A stored status column would be wrong the moment somebody
entered an approval a week after it happened, and then nobody would know which
of the two to believe.

**A Publication step is refused while the workflow is incomplete, and the
refusal names exactly what is missing.** There is deliberately no override:
unlike a cross-stage money comparison, "nobody has reviewed this" is not a
thing that genuinely happens and has to be accepted — it is a thing to go and
get. The rules themselves live in ``build_publish.missing_for_publication``,
so the form, the whitelisted method and the report cannot disagree about them.

**Steps arrive only through ``record_step``.** ``read_only`` on the child
fields is a UI hint (the Phase 2 P3-2 lesson); ``validate()`` is the guard, and
it follows ``Research Study``'s pattern exactly — compare every row against
the document's own pre-save state via ``get_doc_before_save()``, and refuse any
addition, edit or deletion unless ``flags.recording_step`` is set. A step that
could be typed into the grid would bypass the publication gate entirely, which
is the whole thing this doctype exists to enforce.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from hospital_ops.hospital_ops.build_publish import (
    WEB_STEPS,
    missing_for_publication,
    page_state,
)
from hospital_ops.hospital_ops.permissions import get_doc_for_action


class HospitalWebPage(Document):
    def validate(self) -> None:
        self._guard_steps()

    def _guard_steps(self) -> None:
        if self.flags.recording_step:
            return

        before = self.get_doc_before_save()

        if before is None:
            if self.steps:
                frappe.throw(
                    _(
                        "A page cannot be created with workflow steps already on it. Steps "
                        "are added only by record_step(), which is where the publication "
                        "gate lives."
                    ),
                    title=_("Hospital Web Page"),
                )
            return

        before_by_name = {row.name: row for row in (before.steps or [])}

        for row in self.steps:
            prior = before_by_name.get(row.name)
            if prior is None:
                frappe.throw(
                    _(
                        "A workflow step cannot be added by a direct save — record_step() is "
                        "the only path, because it is what refuses a Publication while the "
                        "draft, review and approval are not in order."
                    ),
                    title=_("Hospital Web Page"),
                )
            if (
                row.step != prior.step
                or str(row.occurred_on or "") != str(prior.occurred_on or "")
                or (row.note or "") != (prior.note or "")
            ):
                frappe.throw(
                    _(
                        "Workflow steps are a record of what happened and cannot be edited "
                        "by a direct save. Record a further step instead."
                    ),
                    title=_("Hospital Web Page"),
                )

        current_names = {row.name for row in self.steps}
        for prior in before.steps or []:
            if prior.name not in current_names:
                frappe.throw(
                    _(
                        "The {0} step recorded on {1} cannot be deleted — that would erase "
                        "the trail rather than correct it."
                    ).format(prior.step, prior.occurred_on),
                    title=_("Hospital Web Page"),
                )


@frappe.whitelist()
def record_step(
    name: str, step: str, occurred_on: str | None = None, note: str | None = None
) -> dict:
    """Records one step of the publication workflow (WEB-002).

    The page is the aggregate and it is locked first: publishability is a check
    against the *set* of its steps, which no unique index can express. Under
    REPEATABLE READ two concurrent writers would each see only their own
    effect, which is how two uploads both claimed version 1 in the reference's
    documents module — so the steps the decision is made on are re-read with
    ``FOR UPDATE`` after the lock, not taken from the document already in
    memory.

    Backdating is deliberately **not** refused, matching the reference: the
    dates are what make the order checks meaningful, and a step recorded late
    is the normal case this workflow was built to survive. What a backdated
    step cannot do is *retroactively authorise* a publication — every
    publication check bounds its lookups to the publication's own date.
    """
    doc = get_doc_for_action("Hospital Web Page", name, ptype="write")

    if step not in WEB_STEPS:
        frappe.throw(_("{0} is not a workflow step.").format(step), title=_("Hospital Web Page"))

    occurred_on = occurred_on or today()

    locked = frappe.db.get_value(
        "Hospital Web Page", doc.name, ["name"], as_dict=True, for_update=True
    )
    if not locked:
        frappe.throw(_("{0} is not a web page.").format(name), title=_("Hospital Web Page"))

    existing = frappe.db.sql(
        """
        SELECT step, occurred_on
        FROM `tabHospital Web Page Step`
        WHERE parent = %s AND parenttype = 'Hospital Web Page'
        ORDER BY idx ASC
        FOR UPDATE
        """,
        (locked.name,),
        as_dict=True,
    )

    if step == "Publication":
        missing = missing_for_publication(existing, publish_on=occurred_on)
        if missing:
            frappe.throw(
                _(
                    "This page cannot be published on {0}. {1} thing(s) are missing:\n\n{2}"
                ).format(occurred_on, len(missing), "\n".join(missing)),
                title=_("Hospital Web Page"),
            )

    doc.reload()
    doc.append("steps", {"step": step, "occurred_on": occurred_on, "note": note})
    doc.flags.recording_step = True
    doc.save()

    state = page_state(doc.name)
    return {
        "page": doc.name,
        "step": step,
        "occurred_on": str(occurred_on),
        "status": state["status"],
        "publication_blockers": state["publication_blockers"],
    }


@frappe.whitelist()
def get_page_state(name: str) -> dict:
    """The derived publication state for one page (read-only, no lock)."""
    doc = get_doc_for_action("Hospital Web Page", name, ptype="read")
    return page_state(doc.name)
