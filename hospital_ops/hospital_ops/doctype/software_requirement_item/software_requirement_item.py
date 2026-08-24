from __future__ import annotations

"""One agreed requirement of a software project (SFT-002).

Rows arrive only through ``Software Project Record.add_requirement()``; the
guard lives on the parent, because a child row is written by a parent save and
``validate()`` here cannot see the parent's pre-save state.
"""

from frappe.model.document import Document


class SoftwareRequirementItem(Document):
    pass
