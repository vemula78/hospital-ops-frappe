from __future__ import annotations

"""The organisation offering or giving money.

Deliberately not an ERPNext Customer: Customer drags receivables accounting,
a party-type ledger and an AR ageing story that has nothing to do with a
sanction letter. This mirrors trust_compliance's Trust Donor precedent — a
plain master, linked from the documents that need it.

§4.3: a funder is an organisation. No individual beneficiary ever appears in
this module, in any doctype, in any field.
"""

from frappe.model.document import Document


class CSRFunder(Document):
    pass
