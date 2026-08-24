from __future__ import annotations

"""One step of a page's publication workflow (WEB-002).

Rows rather than a status column, so a page that was published, found wrong
and returned to Draft keeps its history. The guard that keeps rows arriving
only through ``record_step`` lives on the parent (``Hospital Web Page``),
because a child row is written by a parent save and ``validate()`` on the
child cannot see what the parent's pre-save state was.
"""

from frappe.model.document import Document


class HospitalWebPageStep(Document):
    pass
