from __future__ import annotations

"""One-off creation of the Phase 6 Dashboard and Number Cards.

Run once, by hand, via ``bench execute
hospital_ops.hospital_ops.dashboard_setup.ensure_phase6_number_cards_and_dashboard``
— **not** wired to ``after_migrate``, because these records are exported as
fixtures (``hooks.py``) once created, and after that a plain `bench migrate`
installs them from the fixture JSON on any other bench. Running this again is
harmless (every insert is guarded by ``frappe.db.exists`` first), which is
what makes it safe to leave as ordinary app code rather than deleting it
after first use.

**Why four of these are plain Number Cards and one figure is not a card at
all.** Three are a straight ``frappe.get_all`` count and one narrows a status
filter only (see the note on ``Waiting For (Status Waiting)`` below).
"CSR Reporting Obligations overdue" is deliberately **not** a Number Card:
overdue is derived (``csr_financials.obligation_overdue`` — due_on in the
past AND nothing submitted), and a static Number Card's ``filters_json`` can
only express a fixed comparison against a field, not a boolean computed from
two fields together. Presenting an approximation of that figure as though it
were the true count is exactly the "plausible but unreconciled number" this
app's rules elsewhere refuse to produce — so it is skipped as a card
entirely. The true, current count is only ever shown by the Weekly Review
report's CSR Reporting Obligations section, which calls the same
``get_obligation_state`` helper this app already uses everywhere else.
"""

import frappe

NUMBER_CARDS: list[dict] = [
    {
        "label": "Open Quick Captures",
        "document_type": "Quick Capture",
        "type": "Document Type",
        "function": "Count",
        "filters_json": '[["Quick Capture","status","=","Open"]]',
        "is_public": 1,
    },
    {
        # Deliberate approximation, documented rather than hidden: the brief
        # asks for "status=Waiting with follow_up_on <= today if
        # filterable". A static Number Card's filters_json is a fixed value,
        # not a live expression re-evaluated on each dashboard load — a
        # hardcoded date would be correct today and wrong tomorrow. So this
        # card counts every item still Waiting, not only those due a chase
        # right now. The Weekly Review report's "Waiting For" section is the
        # one place the true, date-bounded figure is shown (it reuses the
        # same follow_up_on <= today() filter at read time, every time).
        "label": "Waiting For (Status Waiting)",
        "document_type": "Waiting For",
        "type": "Document Type",
        "function": "Count",
        "filters_json": '[["Waiting For","status","=","Waiting"]]',
        "is_public": 1,
    },
    {
        "label": "Draft CSR Fund Events",
        "document_type": "CSR Fund Event",
        "type": "Document Type",
        "function": "Count",
        "filters_json": '[["CSR Fund Event","docstatus","=",0]]',
        "is_public": 1,
    },
    {
        "label": "Active Research Studies",
        "document_type": "Research Study",
        "type": "Document Type",
        "function": "Count",
        "filters_json": '[["Research Study","status","=","Active"]]',
        "is_public": 1,
    },
]

DASHBOARD_NAME = "Hospital Ops"

#: core's ``Dashboard`` doctype declares ``charts`` mandatory (``reqd: 1`` in
#: ``dashboard.json``) even though this dashboard's real content is its four
#: Number Cards — a cards-only Dashboard is refused with ``MandatoryError:
#: [Dashboard, Hospital Ops]: charts`` (confirmed against this container's
#: actual insert, not assumed). Rather than fight the framework with a bogus
#: placeholder, this is one genuinely useful time series: how many captures
#: are landing in the inbox per week, which is exactly the "is the queue
#: growing or shrinking" question the Weekly Review report answers with a
#: number but never a trend.
DASHBOARD_CHART_NAME = "Quick Captures Opened"


def ensure_phase6_number_cards_and_dashboard() -> dict:
    created = {"number_cards": [], "chart": None, "dashboard": None}

    for spec in NUMBER_CARDS:
        if frappe.db.exists("Number Card", spec["label"]):
            continue
        doc = frappe.get_doc({"doctype": "Number Card", **spec})
        doc.insert(ignore_permissions=True)
        created["number_cards"].append(doc.name)

    if not frappe.db.exists("Dashboard Chart", DASHBOARD_CHART_NAME):
        chart = frappe.get_doc(
            {
                "doctype": "Dashboard Chart",
                "chart_name": DASHBOARD_CHART_NAME,
                "chart_type": "Count",
                "document_type": "Quick Capture",
                "based_on": "creation",
                "timeseries": 1,
                "timespan": "Last Quarter",
                "time_interval": "Weekly",
                "type": "Line",
                "filters_json": "[]",
                "is_public": 1,
                "module": "Hospital Ops",
            }
        )
        chart.insert(ignore_permissions=True)
        created["chart"] = chart.name

    if not frappe.db.exists("Dashboard", DASHBOARD_NAME):
        dashboard = frappe.get_doc(
            {
                "doctype": "Dashboard",
                "dashboard_name": DASHBOARD_NAME,
                "module": "Hospital Ops",
                "charts": [{"chart": DASHBOARD_CHART_NAME}],
                "cards": [{"card": spec["label"]} for spec in NUMBER_CARDS],
            }
        )
        dashboard.insert(ignore_permissions=True)
        created["dashboard"] = dashboard.name

    frappe.db.commit()
    return created
