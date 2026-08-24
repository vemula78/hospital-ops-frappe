from __future__ import annotations

"""Computed CSR figures — the single place money is added up.

Ported from the Next.js application's ``src/server/domain/csr.ts`` and the
``csr_project_financials`` SQL view. Two rules from that build survive intact
and are the reason this module exists at all:

**Never store an aggregate.** There is no ``received_total`` and no
``spent_total`` column on CSR Project. Every figure here is summed from the
submitted CSR Fund Event rows at read time. A stored total is a dual write,
and a dual write eventually disagrees with its own rows.

**One summing function, two callers.** ``totals_from_kind_rows`` is the only
code that turns ledger rows into received/spent/balance. The whitelisted
per-project method and the "CSR Project Financials" report both feed it, so
the desk view and the document view cannot drift apart — the ``…Within``
lesson from the reference implementation, where a threshold checked outside a
transaction and recomputed inside it disagreed.

**Only submitted events count.** ``docstatus = 1`` in every query below. A
draft is somebody thinking, not a movement of money.
"""

import frappe
from frappe.utils import flt, getdate, today

#: kind -> (bucket, sign). Direction lives in the kind, never in the sign of
#: the amount (amount is validated > 0 on every event).
KIND_SIGNS: dict[str, tuple[str, int]] = {
    "Receipt": ("received", 1),
    "Receipt Reversal": ("received", -1),
    "Expenditure": ("spent", 1),
    "Expenditure Reversal": ("spent", -1),
}

REVERSAL_OF: dict[str, str] = {
    "Receipt Reversal": "Receipt",
    "Expenditure Reversal": "Expenditure",
}


def format_inr(value: float) -> str:
    """Indian-grouped rupees, deterministically.

    Deliberately not ``frappe.utils.fmt_money``: this string is compared
    character-for-character against a stored acknowledgement (see CSR Fund
    Event), so it must not depend on a site-level number-format setting that
    could change between the refusal and the confirmation.
    """
    amount = flt(value, 2)
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def totals_from_kind_rows(rows) -> dict[str, float]:
    """The only place ledger rows become figures.

    ``rows`` is any iterable of ``(kind, total)`` pairs — one grouped SQL row
    per event kind. Unknown kinds are ignored rather than silently folded into
    a bucket: widening the Select means widening ``KIND_SIGNS`` too, and a
    kind that reaches here without a mapping would otherwise vanish from every
    figure with no trace.
    """
    buckets = {"received": 0.0, "spent": 0.0}
    for kind, total in rows:
        mapping = KIND_SIGNS.get(kind)
        if not mapping:
            continue
        bucket, sign = mapping
        buckets[bucket] += sign * flt(total)

    received = flt(buckets["received"], 2)
    spent = flt(buckets["spent"], 2)
    return {"received": received, "spent": spent, "balance": flt(received - spent, 2)}


def project_event_totals(csr_project: str, for_update: bool = False) -> dict[str, float]:
    """Received/spent/balance for one project, from one grouped pass.

    ``for_update`` adds ``FOR UPDATE`` to the scan, and the submit path uses
    it. The project row is already locked by then, which serialises the
    submitters — but InnoDB's default REPEATABLE READ means a *non-locking*
    read in this transaction would still be served from the snapshot taken
    before that lock was granted, i.e. it could miss the very expenditure the
    lock was waited on. A locking read reads the latest committed rows. The
    same trick, for the same reason, as trust_compliance's Grant Utilisation
    outstanding-balance query.
    """
    if for_update:
        # Row-level rather than grouped: MySQL/MariaDB will not attach FOR
        # UPDATE to an aggregated result set, and one project's ledger is
        # small enough that summing the rows in Python costs nothing. It goes
        # through the identical summing function, so the figures are the same
        # figures.
        rows = frappe.db.sql(
            """
            SELECT kind, amount
            FROM `tabCSR Fund Event`
            WHERE csr_project = %s AND docstatus = 1
            FOR UPDATE
            """,
            (csr_project,),
        )
    else:
        rows = frappe.db.sql(
            """
            SELECT kind, SUM(amount) AS total
            FROM `tabCSR Fund Event`
            WHERE csr_project = %s AND docstatus = 1
            GROUP BY kind
            """,
            (csr_project,),
        )
    return totals_from_kind_rows(rows)


def portfolio_event_totals() -> dict[str, dict[str, float]]:
    """The same figures for every project, keyed by project name.

    One grouped pass over the whole ledger rather than N queries, then the
    *same* ``totals_from_kind_rows`` per project.
    """
    rows = frappe.db.sql(
        """
        SELECT csr_project, kind, SUM(amount) AS total
        FROM `tabCSR Fund Event`
        WHERE docstatus = 1
        GROUP BY csr_project, kind
        """,
    )
    by_project: dict[str, list[tuple[str, float]]] = {}
    for project, kind, total in rows:
        by_project.setdefault(project, []).append((kind, total))
    return {
        project: totals_from_kind_rows(kind_rows) for project, kind_rows in by_project.items()
    }


def tranche_states(tranches, received: float, as_of: str | None = None) -> list[dict]:
    """Per-tranche expected-vs-received, derived at read time.

    Receipts are not attributed to an individual tranche — they are applied in
    ``expected_on`` order against the cumulative expectation, which is how a
    part payment or a backdated receipt reads correctly without anyone
    re-tagging anything. ``overdue`` is computed here and stored nowhere: a
    stored flag is wrong the moment a backdated receipt is entered, and then
    nobody knows which of the two to believe.
    """
    as_of_date = getdate(as_of or today())
    ordered = sorted(tranches, key=lambda row: getdate(row.expected_on))

    states = []
    cumulative_expected = 0.0
    for row in ordered:
        cumulative_expected = flt(cumulative_expected + flt(row.expected_amount), 2)
        received_to_date = min(flt(received, 2), cumulative_expected)
        shortfall = flt(cumulative_expected - received_to_date, 2)
        overdue = getdate(row.expected_on) < as_of_date and shortfall > 0
        states.append(
            {
                "tranche": row.name,
                "expected_on": str(row.expected_on),
                "expected_amount": flt(row.expected_amount, 2),
                "cumulative_expected": cumulative_expected,
                "received_to_date": received_to_date,
                "shortfall": shortfall,
                "overdue": overdue,
            }
        )
    return states


def obligation_overdue(due_on, submitted_on, as_of: str | None = None) -> bool:
    """Overdue is derived: past its due date and not yet submitted."""
    if submitted_on:
        return False
    return getdate(due_on) < getdate(as_of or today())
