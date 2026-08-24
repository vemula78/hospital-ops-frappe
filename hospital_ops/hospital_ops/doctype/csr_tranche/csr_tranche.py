from __future__ import annotations

"""One expected instalment of a sanction.

Carries only what was *expected*. Whether it has arrived, is short, or is
overdue is derived from the submitted ledger at read time — see
``csr_financials.tranche_states``. Nothing about receipt state is stored here.
"""

from frappe.model.document import Document


class CSRTranche(Document):
    pass
