from __future__ import annotations

"""Derived ethics standing — the single place a study's ethics position is worked out.

Ported from the Next.js application's ``getEthicsStanding``
(``src/server/domain/research.ts``). The same rule survives the port intact:
**ethics approval is a period with an expiry, and every submission is a row.**
There is no ``has_ethics_approval`` boolean anywhere — a study's standing is
computed from its ``Research Ethics Submission`` rows every time it is asked
for, so it cannot go stale the way a stored flag would.

Four states, and the precedence between them is the point:

- ``approved`` — a submitted Approved decision whose ``valid_until`` has not
  passed as of ``as_of``.
- ``pending`` — a submission is awaiting a decision. This is checked **before**
  ``expired``, deliberately: a study whose old approval lapsed but which has
  since filed a renewal is "awaiting a decision", not "expired and forgotten"
  — the renewal path this function exists to support.
- ``expired`` — an Approved decision exists, its ``valid_until`` (the latest
  one, if there is more than one) is before ``as_of``, and nothing newer is
  pending.
- ``none`` — nothing has been approved and nothing is pending (no submissions
  at all, or every submission was Rejected).

One function. The Research Study form/report and any future notification all
call this rather than each re-deriving it, for the same reason
``csr_financials.totals_from_kind_rows`` is the only place ledger rows become
figures: a second implementation is how two views of the same study disagree.

**Timezone note (Codex Phase 4 audit, low):** ``as_of`` defaults to
``frappe.utils.today()``, i.e. the **site's** timezone (Asia/Kolkata for this
single-site deployment), not a per-workspace timezone the way the reference
``research.ts`` computed it. That is deliberate and acceptable here because
this site has exactly one timezone; a future multi-timezone deployment of
this app would need to revisit ``as_of`` to take a caller-supplied timezone
rather than trusting the site default.
"""

import frappe
from frappe.utils import getdate, today


def ethics_standing(study: str, as_of: str | None = None) -> dict:
    as_of_date = getdate(as_of or today())

    submissions = frappe.get_all(
        "Research Ethics Submission",
        filters={"study": study},
        fields=["name", "decision", "valid_until", "decided_on", "submitted_on"],
        order_by="submitted_on desc, creation desc",
    )

    approved = [row for row in submissions if row.decision == "Approved"]
    current = [
        row for row in approved if row.valid_until and getdate(row.valid_until) >= as_of_date
    ]
    if current:
        # Most recently decided current approval governs.
        operative = max(current, key=lambda row: getdate(row.decided_on or row.submitted_on))
        return {
            "status": "approved",
            "as_of": str(as_of_date),
            "study": study,
            "submission": operative.name,
            "valid_until": str(operative.valid_until),
        }

    pending = [row for row in submissions if row.decision == "Pending"]
    if pending:
        return {
            "status": "pending",
            "as_of": str(as_of_date),
            "study": study,
            "submission": pending[0].name,
        }

    if approved:
        latest = max(approved, key=lambda row: getdate(row.valid_until) if row.valid_until else getdate("1900-01-01"))
        return {
            "status": "expired",
            "as_of": str(as_of_date),
            "study": study,
            "submission": latest.name,
            "valid_until": str(latest.valid_until) if latest.valid_until else None,
        }

    return {"status": "none", "as_of": str(as_of_date), "study": study}
