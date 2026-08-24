from __future__ import annotations

"""Hospital Sign Accessibility Check — SIG-005's checklist, one row per verdict.

Two rules, both ported from ``recordAccessibilityCheck`` in signage.ts:

- **Not Met and Not Applicable carry a reason.** ``Not Applicable`` especially:
  it is the verdict that silently passes a sign.
- **One verdict per criterion per design per day.** A second one on the same
  day is a correction that would silently overwrite the first, so it is
  refused rather than applied — record the correction on the day it was
  re-checked.

The unchecked criteria are the finding, and they live in
``build_publish.accessibility_checklist`` rather than here: all six are
reported whether or not anybody judged them, because reporting silence as
compliance is the cheapest way to fake an accessibility target.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class HospitalSignAccessibilityCheck(Document):
    def validate(self) -> None:
        self._check_note_present()
        self._check_design_belongs_to_sign()
        self._check_not_duplicate()

    def _check_note_present(self) -> None:
        if self.verdict in ("Not Met", "Not Applicable") and not (self.note or "").strip():
            frappe.throw(
                _(
                    "A {0} verdict needs a note. {1}"
                ).format(
                    _(self.verdict),
                    _("Not Applicable is the verdict that silently passes a sign.")
                    if self.verdict == "Not Applicable"
                    else _("A failure nobody described cannot be fixed."),
                ),
                title=_("Hospital Sign Accessibility Check"),
            )

    def _check_design_belongs_to_sign(self) -> None:
        if not self.design:
            return
        design_sign = frappe.db.get_value("Hospital Sign Design", self.design, "sign")
        if design_sign != self.sign:
            frappe.throw(
                _("Design {0} belongs to sign {1}, not {2}.").format(
                    self.design, design_sign, self.sign
                ),
                title=_("Hospital Sign Accessibility Check"),
            )

    def _check_not_duplicate(self) -> None:
        existing = frappe.db.sql(
            """
            SELECT name FROM `tabHospital Sign Accessibility Check`
            WHERE sign = %(sign)s
              AND COALESCE(design, '') = %(design)s
              AND criterion = %(criterion)s
              AND checked_on = %(checked_on)s
              AND name != %(name)s
            LIMIT 1
            """,
            {
                "sign": self.sign,
                "design": self.design or "",
                "criterion": self.criterion,
                "checked_on": self.checked_on,
                "name": self.name or "",
            },
        )
        if existing:
            frappe.throw(
                _(
                    "{0} was already judged for this design on {1} ({2}). A second verdict on "
                    "the same day would silently overwrite the first — record the correction "
                    "on the day it was re-checked."
                ).format(_(self.criterion), self.checked_on, existing[0][0]),
                title=_("Hospital Sign Accessibility Check"),
            )
